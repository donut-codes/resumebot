"""Application packets (docs/PRD.md path 10): asking for one, the checks a packet's words go through, printing it, and
filing the result. The process is playbooks/jobsearch/build_packet.bpmn: the story library's packet writer
(`packet_writer.py`) puts each version together, and its service steps here (`jobs.packet_gather`, `jobs.packet_ready`,
`jobs.packet_held`) start and file it.

A packet is one version (`models.Packet`): the tailored resume, the cover letter, the comp ask worked out by code from
the role's research (`comp.py`), and what the checks found. Every claim cites the master lines it comes from, so a
reader can trace it and the checks can refuse what the master doesn't hold. Services flush; the route or tool that
owns the request commits.
"""

import re
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from process_engine.registry import actions as registry
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.inbox import service as inbox
from app.jobsearch import process, stages
from app.jobsearch.actors import BY_FAVORITE, BY_OWNER, actor_name
from app.jobsearch.fit import comp
from app.jobsearch.measures import pay_place as gates
from app.jobsearch.measures.places import match as place_match
from app.jobsearch.models import Opportunity, Packet, is_unnamed
from app.jobsearch.profile import integrity, never_write, style_rules
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.documents import About, Documents, master_texts
from app.jobsearch.profile.schema import Profile
from app.jobsearch.profile.views import engine_gates
from app.jobsearch.research import staleness
from app.jobsearch.work import stuck
from app.jobsearch.writing import claim_scope
from app.jobsearch.writing import library as story_library
from app.models.work import TrailEvent
from app.trail import service as trail

#: The checks that raise a question about the truth of what the packet claims against the master resume: the owner
#: settles those. Every other blocking check is about wording or form (`owner_failures`).
OWNER_CHECKS = frozenset({
    "Every claim cites the master", "Roles and education are the master's",
    integrity.CERTIFICATIONS_CHECK, integrity.UNSHIPPED_CHECK,
})
#: A bullet longer than this many words is noted (not refused): the owner's format usually wants two lines at most.
LONG_BULLET_WORDS = 45

#: How a Launch skip reason that names a packet failure starts. A packet turning ready makes such a reason untrue; an
#: employer-gap or no-apply-link reason is untouched by it (`browser/go.py` writes the reasons).
PACKET_FAILURE_SKIPS = ("The packet could not be built", "Its packet wasn't finished in time.")


#: The Launch skip reason for a role whose research was out of date (`browser/go.py` writes it when the packet ask is
#: `SentBack`). The research being done again makes it untrue.
SENT_BACK_SKIP = "its research was out of date, so it went back to the analyst first."


def is_sent_back_skip(why: str | None) -> bool:
    return why == SENT_BACK_SKIP


def is_packet_failure_skip(why: str | None) -> bool:
    return bool(why) and why.startswith(PACKET_FAILURE_SKIPS)  # type: ignore[union-attr]


async def stale_go_skips(session: AsyncSession, owner: str) -> tuple[list[Opportunity], list[Opportunity]]:
    """The roles whose Launch skip reason is no longer true: (a) not in Ready to Launch (any reason), (b) in Ready to
    Launch with a packet-failure reason and a ready packet, or a research-sent-back reason and
    current research. `clear-stale-go-skips` clears them."""
    held = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.go_skip.is_not(None), Opportunity.go_skip != ""))).all()
    ready_ids = set((await session.scalars(select(Packet.role_id).where(
        Packet.owner == owner, Packet.status == "ready"))).all())
    left = [r for r in held if r.status != stages.READY_TO_LAUNCH]
    from app.jobsearch.research import analysis  # imported here: it reads this module's neighbours

    fixed = [r for r in held if r.status == stages.READY_TO_LAUNCH and (
        (is_packet_failure_skip(r.go_skip) and r.id in ready_ids)
        or (is_sent_back_skip(r.go_skip) and r.analysis_state == analysis.DONE and not analysis.is_stale(r)))]
    return left, fixed


class PacketError(DomainError):
    """Something that can't be done, in words the owner can act on."""

    status = 409


class NoSuchRole(PacketError):
    status = 404


class SentBack(PacketError):
    """The role's research is out of date, so the app's own pick does not draft it now: it was sent back to the analyst
    (`_final_gates`), or is marked for new research already. Not a failure, and not a slot of the night's packets."""


# --- the packet's shape -------------------------------------------------------------------------------------------


class Cited(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    from_: list[str] = Field(default=[], alias="from")


class RoleCut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    intro: str = ""
    bullets: list[Cited] = []


class Competencies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area: str
    items: list[str] = []


class ResumeCut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header_location: str = ""
    headline: str = ""
    summary: str = ""
    summary_from: list[str] = []
    competencies: list[Competencies] = []
    achievements: list[Cited] = []
    roles: list[RoleCut] = Field(min_length=1)
    education: list[str] = []


class Letter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paragraphs: list[str] = []
    from_: list[str] = Field(default=[], alias="from")




# --- asking for a packet ----------------------------------------------------------------------------------------


async def ask(session: AsyncSession, owner: str, role_id: uuid.UUID, feedback: str = "") -> Packet:
    """The owner's ask for the next version of a role's packet (the route's body, `POST /roles/{id}/packet`, and the
    action door's `role.packet`, PRD path 50.4): refused inside the peak hours the owner chose to avoid, then `start`.
    The caller commits, then wakes the writer (`wake.knock(str(packet.id), reason="packet_asked")`)."""
    from app.jobsearch.work import hours as night  # night reads the profile, which reads this module's neighbours

    await night.refuse_in_blackout(session, owner)
    return await start(session, owner, role_id, feedback)


async def would_ask(session: AsyncSession, owner: str, role_id: uuid.UUID, feedback: str = "") -> dict:
    """What `ask` would do, and nothing done (PRD path 50.7): the same refusals (`PeakHours`, `PacketError`,
    `NoSuchRole`) and the version the packet would be."""
    from app.jobsearch.work import hours as night

    await night.refuse_in_blackout(session, owner)
    role, version = await check_ask(session, owner, role_id, by=BY_OWNER)
    return {"role": str(role.id), "name": f"{role.title} at {role.company}", "version": version,
            "feedback": feedback.strip() or None}


async def check_ask(session: AsyncSession, owner: str, role_id: uuid.UUID, *, by: str) -> tuple[Opportunity, int]:
    """The checks before a packet is asked, and the version it would be: the role is the owner's and named, there is a
    master resume, an ask by the app passes the last gate check, and none is being written for the role."""
    role = await session.get(Opportunity, role_id)
    if role is None or role.owner != owner:
        raise NoSuchRole("no such role")
    if is_unnamed(role):  # nothing is written for an employer nobody has named: research names it first
        raise PacketError("this role's employer isn't named yet, so no packet is written: research names it first")
    docs = await profile_store.owner_documents(session, owner)
    if not docs.resume.roles:
        raise PacketError("there is no master resume to write from yet: add it under My Resume")
    if by != BY_OWNER:
        await _final_gates(session, owner, role)
    writing = await session.scalar(select(Packet).where(
        Packet.owner == owner, Packet.role_id == role_id, Packet.status == "writing"))
    if writing is not None:
        raise PacketError("a packet for this role is being written already")
    version = (await session.scalar(select(func.max(Packet.version)).where(Packet.role_id == role_id)) or 0) + 1
    return role, version


async def start(session: AsyncSession, owner: str, role_id: uuid.UUID, feedback: str = "", *,
                by: str = BY_OWNER) -> Packet:
    """Ask for the next version of a role's packet. One is written at a time per role. `by` is who asks: the owner (the
    default), or the app (`actors.BY_APP`, Launch). The ask is written down as theirs or the app's
    (`packet_asked`) but is no hand move: only a move on the board holds a role where the owner put it, and keeps a
    re-run of the gates from moving it (docs/PRD.md 23.8; the owner, 2026-10-20; `placement`). What the owner asks for
    is kept as typed, whole."""
    role, version = await check_ask(session, owner, role_id, by=by)
    packet = Packet(owner=owner, role_id=role_id, version=version, status="writing", feedback=feedback.strip(),
                    brief={}, resume={}, letter={}, comp={}, reasoning={}, checks=[])
    # The packet and its process are made in a savepoint, so a start that fails leaves no packet "writing" behind.
    async with session.begin_nested():
        session.add(packet)
        try:
            await session.flush()
        except IntegrityError:  # a second ask got in between the check above and this insert (one writing, one version)
            raise PacketError("a packet for this role is being written already") from None
        instance = await process.engine().start(
            session, process.PACKET, {"owner": owner, "packet": str(packet.id), "role": str(role_id)},
            subject=f"jobsearch:packet:{packet.id}",
        )
        packet.instance_id = instance.id
    what = f"asked for changes to the packet for {role.title} at {role.company}" if feedback.strip() else (
        f"asked for a packet for {role.title} at {role.company}")
    mine = by == BY_OWNER
    await trail.record(session, owner, event="packet_asked", actor="person" if mine else "app",
                       who=await actor_name(session, owner) if mine else trail.APP_WHO,
                       sentence=(f"You {what}." if mine else
                                 f"{role.title} at {role.company} is your favorite and is read: the app {what}."
                                 if by == BY_FAVORITE else f"The app {what}."),
                       link=("opportunity", role.id), detail=feedback.strip() or None)
    await session.flush()
    return packet


async def _final_gates(session: AsyncSession, owner: str, role: Opportunity) -> None:
    """The last gate check before the writer drafts (the owner, 2026-10-17): every gate again on the role's facts, with
    the place its posting states put on it (`stated_place.recheck`). A role that now fails is Gated out with its reason
    and is not drafted; one whose place is undecided while its research never answered it goes back to the analyst
    first, and one with the analyst is drafted once its research is back. Raises `PacketError` for each; the Gated out
    move and the requeue stay (the caller commits them). Only the app's own asks come here: the owner's ask for a
    packet is their hand on the role (docs/PRD.md 23.8)."""
    from app.jobsearch.gating import stated_place  # both read this module's neighbours; imported here
    from app.jobsearch.research import analysis  # both read this module's neighbours; imported here

    if role.analysis_state == analysis.WAITING:
        raise SentBack("this role is with the analyst: it is drafted once its research is back")
    if role.analysis_state == analysis.NO_TEXT:
        raise SentBack("this role waits for the posting's text: it is drafted once the text is read and researched")
    if analysis.REANALYSE in (role.analysis or {}) and role.analysis_state == analysis.DONE:
        raise SentBack("this role is marked to be researched again: it is drafted once its research is back")
    # The owner, 2026-10-19: research that is out of date is done again before the packet is written. No stamp is
    # unknown, never stale (`staleness.analysis_reasons`).
    if reasons := await staleness.analysis_reasons(session, role):
        await analysis.requeue_for_inputs(session, role, reasons)
        raise SentBack("its research was out of date, so it went back to the analyst: " + "; ".join(reasons))
    done = await stated_place.recheck(session, owner, role, cause="The writer was about to draft it")
    if done == stated_place.GATED:
        raise PacketError(f"no packet: {role.screened_out_reason or 'it failed a gate'}")
    if done == stated_place.SENT_BACK:
        raise PacketError("no packet yet: the analyst is asked where the work is first")


#: Set in the brief of a packet the owner's Launch in Browser asked for (`browser/go.py`): a free pool session takes
#: these first, outside the pool's queue, the day rule and peak hours (`session_kinds.hand_out`: the Launch rung).
LAUNCH_KEY = "launch"


def launched(packet: Packet) -> bool:
    """Whether the packet was asked for by Launch in Browser."""
    return bool((packet.brief or {}).get(LAUNCH_KEY))


def mark_launched(packet: Packet) -> None:
    """Mark the packet as Launch's. The caller flushes."""
    packet.brief = {**(packet.brief or {}), LAUNCH_KEY: True}



async def owner_asked(session: AsyncSession, owner: str, packet: Packet) -> bool:
    """Whether the owner asked for this packet themselves (their `packet_asked` on the role's history when it was
    made), not the packets mission or the pool: what the pool hands out by day (`session_kinds.ORDER`)."""
    return await session.scalar(select(TrailEvent.id).where(
        TrailEvent.owner == owner, TrailEvent.event == "packet_asked", TrailEvent.actor == "person",
        TrailEvent.link_id == packet.role_id, TrailEvent.at >= packet.created_at - timedelta(minutes=1)
    ).limit(1)) is not None


# --- where a packet is on its way -----------------------------------------------------------------------------------

#: The key of a packet's brief that says which step of build_packet.bpmn it is at (`packet_writer` sets it).
STEP_KEY = "step"
#: A packet's steps, for its building bar (`progress`): asked (waiting for the composer, or for its story library),
#: its facts being checked, being fixed, and done (ready, held for the owner,
#: failed or replaced).
PROGRESS_STEPS = ("asked", "checking", "fixing", "done")
PROGRESS_WORDS = {"asked": "Waiting for the writer", "checking": "Its facts being checked",
                  "fixing": "Being fixed", "ready": "Ready", "held": "Held for you",
                  "failed": "Not written", "replaced": "Replaced by a newer version"}


def progress_of(status: str, step: str | None) -> dict:
    """Where a packet is on its way (`PROGRESS_STEPS`), from its status and the step its brief names, for a building
    bar: `step`, `label` (in words), `done` and `of` (steps done of all of them) and `fraction` (0 to 1)."""
    if status == "writing":
        step = step if step in PROGRESS_STEPS[:-1] else "asked"
        label = PROGRESS_WORDS[step]
    else:
        step, label = "done", PROGRESS_WORDS.get(status, status)
    done, of = PROGRESS_STEPS.index(step), len(PROGRESS_STEPS) - 1
    return {"step": step, "label": label, "done": done, "of": of, "fraction": round(done / of, 2)}


def step_of(packet: Packet) -> str | None:
    found = (packet.brief or {}).get(STEP_KEY)
    return found if isinstance(found, str) else None


def progress(packet: Packet) -> dict:
    """Where the packet is on its way (`progress_of`)."""
    return progress_of(packet.status, step_of(packet))


async def settle_stuck(session: AsyncSession, owner: str, now: datetime | None = None,
                       since: datetime | None = None) -> int:
    """A packet that never finished (in `stuck_minutes`, counted from `since` when that is later: the end of peak
    hours, when work was held) is closed as failed, so it can be asked for again. No notification is raised: the role's
    packet says it failed and offers Build again (the owner, 2026-10-24, docs/PRD.md 45)."""

    async def find(cutoff: datetime) -> list[Packet]:
        return list((await session.scalars(select(Packet).where(
            Packet.owner == owner, Packet.status == "writing", Packet.created_at < cutoff))).all())

    async def close(packet: Packet) -> None:
        await stuck.cancel_instance(session, packet.instance_id, "the packet was not finished in time",
                                    f"packet {packet.id}")
        packet.status = "failed"

    return await stuck.settle_stuck(session, now=now, since=since, what="packet", find=find, close=close)


def printed(value):
    """What a reader of the printed packet sees: no master line ids, and no contact line."""
    if isinstance(value, dict):
        return {k: printed(v) for k, v in value.items() if k not in ("from", "summary_from", "contact")}
    if isinstance(value, list):
        return [printed(v) for v in value]
    return value


# --- the checks -------------------------------------------------------------------------------------------------


def _ids(docs: Documents) -> dict[str, set[str]]:
    r = docs.resume
    lines = {b.id for role in r.roles for b in role.bullets}
    lines |= {x.id for x in [*r.achievements, *r.projects, *r.awards, *r.summary_variants]}
    lines |= {e.id for role in r.roles for e in role.evidence} | {role.id for role in r.roles}
    return {"lines": lines, "roles": {role.id for role in r.roles}, "education": {e.id for e in r.education}}


def _role_places(docs: Documents) -> dict[str, str]:
    """Each role of the master in plain words, by its id: its title at its employer. A check names a role this way and
    never by its id."""
    return {r.id: integrity.role_words(r) for r in docs.resume.roles}


def _under(role_id: str, places: dict[str, str]) -> str:
    """The role a bullet or a scope line sits under, in plain words; a role the master doesn't have by its id."""
    return places.get(role_id) or f"role {role_id!r}"


def _bullet(n: int, under: str) -> str:
    return f"bullet {n} under {under}"


def _pieces(cut: ResumeCut, paragraphs: list[str], letter_from: list[str],
            docs: Documents) -> list[tuple[str, str, tuple[str, ...]]]:
    """Every piece of the packet's own printed words (headline, summary, competencies, achievements, scope lines,
    bullets and letter paragraphs), as (where, text, cited lines). Not what the app prints from the master (the
    employer, title and dates of a role, the degree line)."""
    places = _role_places(docs)
    out = [("headline", cut.headline, ()), ("summary", cut.summary, tuple(cut.summary_from))]
    out += [(f"competencies: {c.area}", " · ".join([c.area, *c.items]), ()) for c in cut.competencies]
    out += [(f"achievement {n}", a.text, tuple(a.from_)) for n, a in enumerate(cut.achievements, 1)]
    for role in cut.roles:
        under = _under(role.role, places)
        out.append((f"scope line under {under}", role.intro, ()))
        out += [(_bullet(n, under), b.text, tuple(b.from_)) for n, b in enumerate(role.bullets, 1)]
    out += [(f"letter paragraph {n}", p, tuple(letter_from)) for n, p in enumerate(paragraphs, 1)]
    return [x for x in out if x[1]]


def _line_texts(docs: Documents) -> dict[str, str]:
    """The own text of each master line a claim can copy whole, by its id."""
    r = docs.resume
    lines = {b.id: b.text for role in r.roles for b in role.bullets}
    lines |= {x.id: x.text for x in [*r.achievements, *r.projects, *r.awards, *r.summary_variants]}
    return lines | {e.id: e.text for role in r.roles for e in role.evidence}


def row(name: str, problems: list[str], blocking: bool = True, passed: str = "") -> dict:
    """One check: {check, ok, blocking, detail, problems}. The detail names the first problems, or says what passed."""
    return {"check": name, "ok": not problems, "blocking": blocking,
            "detail": "; ".join(problems[:12]) + (f"; and {len(problems) - 12} more" if len(problems) > 12
                                                  else "") if problems else passed,
            "problems": list(problems)}


def stored(checks: list[dict]) -> list[dict]:
    """The check rows as a packet keeps them: without the full problem lists."""
    return [{k: v for k, v in c.items() if k != "problems"} for c in checks]


def text_checks(cut: ResumeCut, paragraphs: list[str], letter_from: list[str], docs: Documents) -> list[dict]:
    """The checks that read the packet's own words, as rows, all blocking: nothing the owner bans appears (their
    phrases, the older patterns and the built-in kinds), no certification or licence the master lacks, no work that did
    not ship written as delivered, no line the master marks never in writing used, and the owner's style rules that are
    on. A claim whose whole text is a master line it cites is not held to the style rules: the master is never edited
    to satisfy a rule."""
    pieces = _pieces(cut, paragraphs, letter_from, docs)
    texts = [(where, text) for where, text, _ in pieces]
    resume, writing = docs.resume, docs.writing
    lines = _line_texts(docs)
    exempt = {where for where, text, cites in pieces
              if any(" ".join(lines[i].split()) == " ".join(text.split()) for i in cites if i in lines)}
    places = _role_places(docs)
    citations = [("summary", i) for i in cut.summary_from]
    citations += [(f"achievement {n}", i) for n, a in enumerate(cut.achievements, 1) for i in a.from_]
    citations += [(_bullet(n, _under(role.role, places)), i)
                  for role in cut.roles for n, b in enumerate(role.bullets, 1) for i in b.from_]
    citations += [("the letter", i) for i in letter_from]
    includes = [("the resume's roles", role.role) for role in cut.roles]
    includes += [("the resume's education", e) for e in cut.education]
    yours = len(writing.banned) + len(writing.legacy_patterns)
    style = [f"{m.rule}: {m.where}: {m.snippet!r}" for m in style_rules.find_matches(writing, texts, exempt)]
    return [
        row(never_write.PACKET_CHECK, never_write.find_hits(writing, texts),
            passed=f"checked against {yours} of your phrases and patterns and {len(never_write.BUILT_IN)} built-in "
                   f"checks"),
        row(integrity.CERTIFICATIONS_CHECK, integrity.certification_hits(writing, resume, texts),
            passed="no certification or licence is named that the master doesn't hold"),
        row(integrity.UNSHIPPED_CHECK, integrity.unshipped_hits(texts),
            passed="no work that didn't ship is written as delivered"),
        row(integrity.NEVER_IN_WRITING_CHECK, integrity.never_in_writing_hits(resume, citations, includes, texts),
            passed="no line the master keeps out of writing is used"),
        row(style_rules.PACKET_CHECK, style,
            passed=("none of the style rules you have on was broken" if style_rules.on(writing)
                    else "every style rule is switched off")),
    ]


def _cited_words(docs: Documents) -> dict[str, str]:
    """What each master line says, with its note, by id: the words a drafted line is read against."""
    r = docs.resume
    notes = {b.id: b.note for role in r.roles for b in role.bullets}
    notes |= {x.id: x.note for x in [*r.achievements, *r.projects, *r.awards, *r.summary_variants]}
    return {i: f"{t} {notes.get(i, '')}" for i, t in master_texts(r).items()}


def packet_checks(cut: ResumeCut, paragraphs: list[str], letter_from: list[str], docs: Documents,
                  target_role: str | None = None, *, profile: Profile | None = None,
                  comp_inputs: comp.CompInputs | None = None) -> list[dict]:
    """What the checks find in a packet, as rows ({check, ok, blocking, detail, problems}): every claim cites master
    lines that exist, roles and education are the master's, the text checks, a cover letter, the letter's length by
    the owner's rules, no line repeating its role's descriptor; and, as notes, scope inflation and long bullets. With
    a profile, also whether the pay ask works out from the research (`comp_inputs`). A check names a role by its title
    at its employer, never by its id."""
    ids = _ids(docs)
    places = _role_places(docs)
    checks: list[dict] = []
    missing: list[str] = []
    uncited: list[str] = []
    for where, cited in [(f"achievement {n}", a) for n, a in enumerate(cut.achievements, 1)] + [
            (_bullet(n, _under(r.role, places)), b) for r in cut.roles for n, b in enumerate(r.bullets, 1)]:
        if not cited.from_:
            uncited.append(f"{where} cites no master line")
        missing += [f"{where} cites {i!r}, which the master doesn't have" for i in cited.from_ if i not in ids["lines"]]
    if cut.summary and not cut.summary_from:
        uncited.append("the summary cites no master line")
    missing += [f"the summary cites {i!r}, which the master doesn't have"
                for i in cut.summary_from if i not in ids["lines"]]
    missing += [f"the letter cites {i!r}, which the master doesn't have" for i in letter_from if i not in ids["lines"]]
    checks.append(row("Every claim cites the master", uncited + missing,
                      passed="every line cites master lines that exist"))
    roles = [f"role {r.role!r} isn't in the master" for r in cut.roles if r.role not in ids["roles"]]
    roles += [f"education {e!r} isn't in the master" for e in cut.education if e not in ids["education"]]
    checks.append(row("Roles and education are the master's", roles,
                      passed="employer, title and dates print from the master"))
    checks += text_checks(cut, paragraphs, letter_from, docs)
    checks.append(row("A cover letter", [] if paragraphs else ["a cover letter (every packet has one)"],
                      passed="letter written"))
    if profile is not None:
        # The ask is the research's, not the packet's: when the research cannot work one out (no classed items, no pay
        # figures), the packet is still the owner's, with this said beside it (the role's page says the same).
        try:
            if comp_inputs is None:
                raise ValueError("the role's research has no required items classed yet, so no ask is worked out")
            worked = comp.compute(comp_inputs, profile, target_role)
            checks.append(row("The pay ask works out", [], passed=f"ask ${worked['ask']:,}, range "
                                                                 f"${worked['range'][0]:,} to ${worked['range'][1]:,}"))
        except (ValueError, ZeroDivisionError) as exc:
            checks.append(row("The pay ask works out", [str(exc)], blocking=False))
    low, high = letter_word_limits(docs.writing, target_role)
    if low is not None or high is not None:
        count = sum(len(x.split()) for x in paragraphs)
        said = f"{low} to {high}" if None not in (low, high) else (
            f"at least {low}" if low is not None else f"at most {high}")
        off = bool(paragraphs) and ((low is not None and count < low) or (high is not None and count > high))
        checks.append(row("The letter's length", [f"the letter is {count} words; your rules say {said}"] if off else [],
                          passed=f"the letter is {count} words, within your rules ({said})"))
    checks.append(row(claim_scope.REPEAT_CHECK, _repeated_descriptors(cut, docs, places),
                      passed="no scope line or bullet repeats the role's printed descriptor"))
    words = _cited_words(docs)
    inflated = [f"{where}: {found}" for where, text, cites in _pieces(cut, paragraphs, letter_from, docs)
                for found in claim_scope.inflation(text, " ".join(words.get(i, "") for i in cites))]
    checks.append(row(claim_scope.INFLATION_CHECK, inflated, blocking=False,
                      passed="no line claims more than the master lines it cites"))
    long = [f"{_bullet(n, _under(r.role, places))} is {len(b.text.split())} words" for r in cut.roles
            for n, b in enumerate(r.bullets, 1) if len(b.text.split()) > LONG_BULLET_WORDS]
    checks.append(row("Bullets are short", long, blocking=False, passed=f"no bullet over {LONG_BULLET_WORDS} words"))
    return checks


def worked_ask(inputs: comp.CompInputs | None, profile: Profile, target_role: str | None = None) -> dict | None:
    """The pay ask worked out from the research's inputs, or None when it can't be."""
    if inputs is None:
        return None
    try:
        return comp.compute(inputs, profile, target_role)
    except (ValueError, ZeroDivisionError):
        return None


def _repeated_descriptors(cut: ResumeCut, docs: Documents, places: dict[str, str]) -> list[str]:
    """The scope lines and bullets that only repeat the descriptor the resume already prints under the role's title."""
    by_id = {r.id: r for r in docs.resume.roles}
    out = []
    for rc in cut.roles:
        role = by_id.get(rc.role)
        if role is None or not role.descriptor.strip():
            continue
        under = _under(rc.role, places)
        lines = [(f"scope line under {under}", rc.intro)]
        lines += [(_bullet(n, under), b.text) for n, b in enumerate(rc.bullets, 1)]
        out += [f"{where} repeats the role's descriptor {role.descriptor!r}, which prints already"
                for where, text in lines if text and claim_scope.repeats(role.descriptor, text)]
    return out


_LETTER_RANGE = re.compile(r"\b(\d{2,4})\s*(?:to|-|–|and)\s*(\d{2,4})\s+words\b", re.I)
_LETTER_MAX = re.compile(r"\b(?:no more than|never (?:over|more than|passes?)|at most|up to|not (?:over|more than))"
                         r"\s+(\d{2,4})\s+words\b", re.I)
_LETTER_MIN = re.compile(r"\b(?:at least|no fewer than|no less than)\s+(\d{2,4})\s+words\b", re.I)


def letter_word_limits(writing, target_role: str | None = None) -> tuple[int | None, int | None]:
    """The letter's length the owner's own cover letter rules ask for (Global, and the target role's), as (least, most)
    words, read from their wording ("about 250 to 400 words", "never more than 400 words"). A figure the rules do not
    give is None; with none at all there is no check. The app holds no figure of its own."""
    rules = [*writing.letter_global, *(writing.letter_role_rules.get(target_role or "") or [])]
    lows: list[int] = []
    highs: list[int] = []
    for rule in rules:
        text = rule.says.split("Verify:")[0]
        for a, b in _LETTER_RANGE.findall(text):
            lows.append(min(int(a), int(b)))
            highs.append(max(int(a), int(b)))
        highs += [int(x) for x in _LETTER_MAX.findall(text)]
        lows += [int(x) for x in _LETTER_MIN.findall(text)]
    return (max(lows) if lows else None), (min(highs) if highs else None)



def header_location(role: Opportunity | None, about: About, profile: Profile) -> str:
    """The place the resume's contact line gives, chosen by code from the owner's application info: their own
    location, or their metro location (`About.metro_location`, the place to give for a remote or out-of-town role) when
    they set one and the role is remote, or names a town that is neither theirs nor one listed in their work location
    gate. A role that names no town gets their own location."""
    home, metro = about.location.strip(), about.metro_location.strip()
    if not metro or role is None:
        return home
    where = (role.location or "").strip()
    mode = (role.move_type or "").strip().lower()
    if mode == "remote" or (mode not in ("hybrid", "onsite") and gates.says_remote(where.lower())):
        return metro
    if not where:
        return home
    g = engine_gates(profile)
    mine = g.by_role.get(role.target_role or "")
    listed = [home, *g.hybrid_places, *g.onsite_places, *(mine.hybrid_places if mine else ()),
              *(mine.onsite_places if mine else ())]
    return home if any(place_match(where, name) for name in listed if name) else metro



def master_education(docs: Documents) -> list[str]:
    """The education the resume prints when the writer names none: every entry of the master, in its order, but one
    the master marks never in writing."""
    _, marked = integrity.marked_lines(docs.resume)
    return [e.id for e in docs.resume.education if e.id not in marked]



def render_resume(cut: ResumeCut, docs: Documents, location: str = "") -> dict:
    """The resume as it will print: the packet's choices (headline, summary, competencies, achievements, and for each
    role its scope line and bullets) with every fixed part from code: the owner's name and contact line (the place is
    `location`, `header_location`), each role's employer, title, place, dates and descriptor from the master, and the
    master's education."""
    by_id = {r.id: r for r in docs.resume.roles}
    edu = {e.id: e for e in docs.resume.education}
    out = cut.model_dump(mode="json", by_alias=True)
    out["roles"] = [{
        **rc, "employer": by_id[rc["role"]].employer, "title": by_id[rc["role"]].title,
        "location": by_id[rc["role"]].location, "start": by_id[rc["role"]].start, "end": by_id[rc["role"]].end,
        "descriptor": by_id[rc["role"]].descriptor,
    } for rc in out["roles"] if rc["role"] in by_id]
    out["education"] = [{"id": e, "degree": edu[e].degree, "school": edu[e].school}
                        for e in out["education"] or master_education(docs) if e in edu]
    out["header_location"] = out.get("header_location") or location or docs.about.location
    out["name"] = docs.about.name
    out["contact"] = [x for x in (out["header_location"], docs.about.phone, docs.about.email, *docs.about.links) if x]
    return out


def _finished(text: str) -> str:
    """The text with its end punctuation: a sentence that ends in neither a full stop, a question mark nor an
    exclamation mark gets a full stop. The writer trims a line to fit and drops it about one packet in five; a full
    stop is not a judgement call, so the app puts it back."""
    text = text.rstrip()
    return text if not text or text[-1] in ".!?" else text + "."



_TOKEN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9+#/-]|\.(?=[A-Za-z0-9]))*")
_SENTENCE_END = re.compile(r"(?:\A|[.!?:;]\s+)\Z")


def _spelling_key(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _tokens(text: str) -> list[tuple[re.Match, bool]]:
    """Each word of the text with whether it opens a sentence."""
    return [(m, bool(_SENTENCE_END.search(text[:m.start()]))) for m in _TOKEN.finditer(text)]


def _name_like(token: str, opens: bool) -> bool:
    """A word that is spelled as a tool or product name: capitals inside it, a digit among its letters, a dot inside it,
    or a capital in the middle of a sentence. Plain lowercase words and sentence openers are not."""
    if len(_spelling_key(token)) < 3 or not any(c.isalpha() for c in token):
        return False
    inner = (any(c.isupper() for c in token[1:]) or "." in token
             or (any(c.isdigit() for c in token) and any(c.isalpha() for c in token)))
    return inner or (token[0].isupper() and not opens)


def _names_in(text: str) -> list[tuple[str, str]]:
    """The names a text spells, as (key, spelling): single words, and two name-like words in a row ("Power BI")."""
    found, words = [], _tokens(text)
    for i, (m, opens) in enumerate(words):
        if not _name_like(m.group(), opens):
            continue
        found.append((_spelling_key(m.group()), m.group()))
        if i + 1 < len(words) and text[m.end():words[i + 1][0].start()] == " " and _name_like(
                words[i + 1][0].group(), False):
            found.append((_spelling_key(m.group() + words[i + 1][0].group()),
                          f"{m.group()} {words[i + 1][0].group()}"))
    return found


def _canonical_names(texts: list[str], master: list[str]) -> tuple[dict[str, str], set[str]]:
    """Each name's one spelling, by its key (letters and digits, in lower case): the master's most common spelling when
    it holds the name (unless it also writes the word in plain lower case, when it is not a name), else the packet's
    most common among its own spellings of it. Also the keys that come from the master."""
    from_master: dict[str, Counter] = {}
    plain = {_spelling_key(m.group()) for t in master for m, _ in _tokens(t) if m.group().islower()}
    for text in master:
        for key, spelling in _names_in(text):
            if key not in plain:
                from_master.setdefault(key, Counter())[spelling] += 1
    canon = {k: c.most_common(1)[0][0] for k, c in from_master.items()}
    in_packet: dict[str, Counter] = {}
    for text in texts:
        for key, spelling in _names_in(text):
            if key not in canon:
                in_packet.setdefault(key, Counter())[spelling] += 1
    canon |= {k: c.most_common(1)[0][0] for k, c in in_packet.items() if len(c) > 1}
    return canon, set(from_master)


def _respelled(text: str, canon: dict[str, str]) -> tuple[str, list[str]]:
    """The text with each name in its one spelling, and what changed. A word written wholly in lower case is left
    alone."""
    out, changed, last, words, i = [], [], 0, _tokens(text), 0
    while i < len(words):
        m = words[i][0]
        took = 1
        spelled = m.group()
        if i + 1 < len(words) and text[m.end():words[i + 1][0].start()] == " ":
            pair = f"{m.group()} {words[i + 1][0].group()}"
            if _spelling_key(pair) in canon and _name_like(m.group(), words[i][1]):
                took, spelled, m_end = 2, pair, words[i + 1][0].end()
        end = m.end() if took == 1 else m_end
        want = canon.get(_spelling_key(spelled))
        if want and want != spelled and not spelled.islower():
            out += [text[last:m.start()], want]
            changed.append(f"{spelled!r} as {want!r}")
            last = end
        i += took
    return "".join(out) + text[last:], changed



def mechanical_fixes(cut: ResumeCut, letter: Letter, docs: Documents) -> list[str]:
    """What code puts right before the checks run, each as a sentence naming where: the end punctuation of the summary,
    a scope line, an achievement or a bullet, and one spelling for each tool or product name across the packet (the
    master's spelling wins). These are not judgement calls. The packet is changed in place."""
    places, fixes = _role_places(docs), []

    def stop(where: str, obj, name: str) -> None:
        was = getattr(obj, name)
        if (now := _finished(was)) != was.rstrip():
            fixes.append(f"{where}: added the full stop it was missing")
        setattr(obj, name, now)

    stop("summary", cut, "summary")
    for n, a in enumerate(cut.achievements, 1):
        stop(f"achievement {n}", a, "text")
    for role in cut.roles:
        under = _under(role.role, places)
        stop(f"scope line under {under}", role, "intro")
        for n, b in enumerate(role.bullets, 1):
            stop(_bullet(n, under), b, "text")

    spelt: list[tuple[str, Any, str]] = [("headline", cut, "headline"), ("summary", cut, "summary")]
    spelt += [(f"competencies: {c.area}", c, "area") for c in cut.competencies]
    spelt += [(f"achievement {n}", a, "text") for n, a in enumerate(cut.achievements, 1)]
    for role in cut.roles:
        under = _under(role.role, places)
        spelt.append((f"scope line under {under}", role, "intro"))
        spelt += [(_bullet(n, under), b, "text") for n, b in enumerate(role.bullets, 1)]
    paragraphs = [(f"letter paragraph {n}", n - 1) for n in range(1, len(letter.paragraphs) + 1)]
    items = [(f"competencies: {c.area}", c, i) for c in cut.competencies for i in range(len(c.items))]
    texts = [getattr(h, nm) for _, h, nm in spelt] + [c.items[i] for _, c, i in items] + list(letter.paragraphs)
    canon, _ = _canonical_names(texts, list(_line_texts(docs).values()))
    if not canon:
        return fixes

    def respell(where: str, text: str) -> str:
        after, changed = _respelled(text, canon)
        if changed:
            fixes.append(f"{where}: wrote {', '.join(changed)}, the one spelling used throughout")
        return after

    for where, holder, nm in spelt:
        setattr(holder, nm, respell(where, getattr(holder, nm)))
    for where, c, i in items:
        c.items[i] = respell(where, c.items[i])
    for where, i in paragraphs:
        letter.paragraphs[i] = respell(where, letter.paragraphs[i])
    return fixes


# --- reading packets ---------------------------------------------------------------------------------------------


def failures(checks: list[dict] | None) -> list[dict]:
    return [c for c in checks or [] if c.get("blocking") and not c.get("ok")]


def owner_failures(checks: list[dict] | None) -> list[dict]:
    """The failed checks only the owner can settle: a question of truth about the master resume."""
    return [c for c in failures(checks) if c["check"] in OWNER_CHECKS]


def view(packet: Packet) -> dict:
    return {
        "id": str(packet.id), "role_id": str(packet.role_id), "version": packet.version, "status": packet.status,
        "feedback": packet.feedback, "resume": packet.resume, "letter": packet.letter, "comp": packet.comp,
        "reasoning": packet.reasoning, "checks": packet.checks, "attempts": packet.attempts,
        "created_at": packet.created_at.isoformat() if packet.created_at else None,
        "written_at": packet.written_at.isoformat() if packet.written_at else None,
        # Where it is on its way, for a building bar (`progress`).
        "progress": progress(packet),
    }


async def for_role(session: AsyncSession, owner: str, role_id: uuid.UUID) -> list[dict]:
    rows = (await session.scalars(select(Packet).where(Packet.owner == owner, Packet.role_id == role_id)
                                  .order_by(Packet.version.desc()))).all()
    views = [view(p) for p in rows]
    for packet, shown in zip(rows, views, strict=True):
        if packet.status == "ready":  # only the packet the owner will use: one more read, for one packet
            shown["files_problem"] = await files_problem(session, packet)
    return views


# --- the packet's files in the packet folder (docs/PRD.md path 10.5) ---------------------------------------------

FILES_NOT_SAVED = "packet_files_not_saved"
FILES_SAVED = "packet_files_saved"


def _files_sentence(version: int, saved: bool) -> str:
    """The one sentence of each of the two lines; the lookup matches on it, so the lines are written from here only."""
    if saved:
        return f"Version {version}'s files were saved to your packet folder."
    return f"Version {version}'s files could not be saved to your packet folder."


async def _latest_files_line(session: AsyncSession, packet: Packet) -> TrailEvent | None:
    """The newest line about this version's files on its role (exact sentence, so version 3 is never version 13)."""
    sentences = [_files_sentence(packet.version, True), _files_sentence(packet.version, False)]
    return (await session.scalars(select(TrailEvent).where(
        TrailEvent.owner == packet.owner, TrailEvent.link_kind == "opportunity", TrailEvent.link_id == packet.role_id,
        TrailEvent.event.in_((FILES_NOT_SAVED, FILES_SAVED)), TrailEvent.sentence.in_(sentences),
    ).order_by(TrailEvent.at.desc(), TrailEvent.id.desc()).limit(1))).first()


async def files_problem(session: AsyncSession, packet: Packet) -> str | None:
    """Why the packet's files are not in the packet folder, when the last save of this version failed; else None."""
    line = await _latest_files_line(session, packet)
    if line is None or line.event != FILES_NOT_SAVED:
        return None
    return (line.detail or "").strip() or line.sentence


async def _save_files(session: AsyncSession, packet: Packet, role: Opportunity | None) -> str | None:
    """Save the packet's files and write what came of it: a failure always; a success only where a failure was shown
    before (so the problem clears), never on every first save. Returns the problem, if any. The caller commits."""
    from app.jobsearch.writing import packet_files  # it renders through docs_render, which reads the documents

    problem = await packet_files.save(session, packet, role)
    if problem:
        await trail.record(session, packet.owner, event=FILES_NOT_SAVED, actor="app",
                           sentence=_files_sentence(packet.version, False),
                           link=("opportunity", packet.role_id), detail=problem[:500])
    elif packet_files.enabled() and await files_problem(session, packet) is not None:
        await trail.record(session, packet.owner, event=FILES_SAVED, actor="app",
                           sentence=_files_sentence(packet.version, True), link=("opportunity", packet.role_id))
    return problem


async def save_files_again(session: AsyncSession, owner: str, packet_id: uuid.UUID) -> dict:
    """Write a ready packet's files to the packet folder now (`POST /packets/{id}/save-files`). A failure is kept on
    the packet, not raised: the view says it. The caller commits."""
    from app.jobsearch.writing import packet_files

    packet = await session.get(Packet, packet_id)
    if packet is None or packet.owner != owner:
        raise NoSuchRole("no such packet")
    if packet.status != "ready":
        raise PacketError("only a ready packet's files are saved")
    if not packet_files.enabled():
        raise PacketError("no packet folder is set, so there is nowhere to save the files")
    await _save_files(session, packet, await session.get(Opportunity, packet.role_id))
    await session.flush()
    return {**view(packet), "files_problem": await files_problem(session, packet)}


async def save_failed_files(session: AsyncSession, owner: str, *, dry_run: bool = False) -> tuple[int, int]:
    """Save again every ready packet whose last save failed. Returns (packets tried, failed again); a dry run only
    counts, as (packets, 0). Commits after each packet when it writes."""
    ready = (await session.scalars(select(Packet).where(Packet.owner == owner, Packet.status == "ready")
                                   .order_by(Packet.created_at))).all()
    todo = [p for p in ready if await files_problem(session, p) is not None]
    if dry_run:
        return len(todo), 0
    again = 0
    for packet in todo:
        role = await session.get(Opportunity, packet.role_id)
        if await _save_files(session, packet, role):
            again += 1
        await session.commit()
    return len(todo), again


# --- the process's service steps (registered below, with the engine) ------------------------------------------


async def _packet(session: AsyncSession, data: dict) -> Packet:
    packet = await session.get(Packet, uuid.UUID(data["packet"]))
    if packet is None:
        raise PacketError("the packet this process belongs to isn't there")
    return packet


async def action_gather(session, instance, data):
    """Whether there is a master to write from; a story library is built first when the newest is not from the master
    and rules in force (the compose seat waits for it)."""
    owner = data["owner"]
    docs = await profile_store.owner_documents(session, owner)
    started = await story_library.ensure_current(session, owner) if docs.resume.roles else None
    packet = await _packet(session, data)
    packet.brief = {**(packet.brief or {}), STEP_KEY: "asked"}
    await session.flush()
    return {"ready": bool(docs.resume.roles), "library_started": started is not None}


async def _file(session: AsyncSession, packet: Packet, status: str) -> Opportunity | None:
    earlier = (await session.scalars(select(Packet).where(
        Packet.role_id == packet.role_id, Packet.id != packet.id, Packet.status.in_(("ready", "held"))))).all()
    for old in earlier:
        old.status = "replaced"
    packet.status = status
    return await session.get(Opportunity, packet.role_id)



async def action_ready(session, instance, data):
    packet = await _packet(session, data)
    role = await _file(session, packet, "ready")
    # A packet moves no role: Ready to Launch holds the day's best roles with or without a packet,
    # and a packet asked for any other role is kept for the owner to use.
    if role is not None:
        role.packet_complete = True
        if is_packet_failure_skip(role.go_skip):  # the packet Launch said it could not build is built
            role.go_skip = None
    name = f"{role.title} at {role.company}" if role else "a role"
    await trail.record(session, packet.owner, event="packet_ready", actor="agent", agent_slug="scout", who="Writer",
                       sentence=f"The writer finished version {packet.version} of the packet for {name}; every check "
                                f"passed.", link=("opportunity", packet.role_id))
    await _save_files(session, packet, role)
    await session.flush()
    return {"status": "ready"}


async def action_held(session, instance, data):
    packet = await _packet(session, data)
    role = await _file(session, packet, "held")
    name = f"{role.title} at {role.company}" if role else "a role"
    failed = owner_failures(packet.checks) or failures(packet.checks)
    await trail.record(session, packet.owner, event="packet_held", actor="agent", agent_slug="scout", who="Writer",
                       sentence=f"Version {packet.version} of the packet for {name} was held: "
                                f"{'; '.join(c['check'] for c in failed)}.", link=("opportunity", packet.role_id))
    await inbox.raise_task(session, packet.owner, key=f"packet-held-{packet.id}", kind="packet_held",
                           title=f"A packet needs you: {name}",
                           detail="; ".join(f"{c['check']}: {c['detail']}" for c in failed)[:500],
                           link=("opportunity", packet.role_id))
    await session.flush()
    return {"status": "held"}



_SOURCE = "backend/app/jobsearch/writing/packets.py"
registry.register(
    "jobs.packet_gather", action_gather, inputs=["owner", "packet"], outputs=["ready", "library_started"],
    description="Reads whether there is a master resume to write from, and starts a story library build when the "
                "newest is not from the master and rules in force.", source=_SOURCE)
registry.register(
    "jobs.packet_ready", action_ready, inputs=["packet"], outputs=["status"],
    description="Marks the packet ready.", source=_SOURCE)
registry.register(
    "jobs.packet_held", action_held, inputs=["packet"], outputs=["status"],
    description="Holds a packet a check failed on for the owner, with the problems listed.", source=_SOURCE)
