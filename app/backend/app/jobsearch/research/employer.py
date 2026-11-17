"""The employer researcher (docs/PRD.md path 36; stage 4, research for gaps, 39.18 and 39.19): one small session at a
time reads public pages for one employer and reports its facts (with where each came from), its job board, the employers
it may be the same as and the roles that may belong to another company. Code decides what to do with the report:

- **Facts** (`check`): an address is public and, for a careers site, on the employer's own domain or a known job
  system; employees is a number; ownership is one of the allowed words; a fact without a source is refused; a refused
  fact leaves the rest of the report standing, and the reply names each with its reason. The facts that pass are set
  the way the analyst's are (`analysis._research`): never over a field the owner set, a careers site only where none
  is kept.
- **The job board** is looked for by the app's own detection (`resolve.py`: the links it holds, the careers page, the
  open feeds) and, for what needs reading, by the address the researcher reports, kept only when it names a job system
  the app knows and that no other employer holds.
- **Duplicates and splits** go through the app's own feature and nothing else (`employer_pairs.propose_merge` and
  `propose_split`): a pair the table in `employer_match` says to merge (a website beside
  names that look alike, a website the owner or this researcher confirmed for both records, a board beside names that
  look alike) merges by itself; anything less certain is not merged and nobody is asked. A proposed split moves the
  roles whose own postings sit on the other employer's own job-system account or confirmed website, or on a site or
  account named for it, and no others, and nobody is asked. A website this
  researcher sets is confirmed (`employer_pairs.website_confirmed`, read from `BY`).
- **The employer's roles are settled again** on what research brought (`refresh_roles`, which is `settle.roles`,
  docs/PRD.md 39.18, 39.14, 39.21): a role the new facts now stop is Gated out with that gate, one only they stopped is
  back in play, and a researched role's score is worked out again from the new facts. A merge or a split anywhere does
  the same for the roles it moves (`employer_records.merge` and `split` call it).

The work is a mission (Research employers, nightly, after the boards are read), a batch of employers in the order of
36.1 (`picks`), each its own process (`playbooks/jobsearch/research_employer.bpmn`) that waits for a session. The
session is the analyst's role with an instruction file of its own (`jobsearch.researcher`), the way Find Gaps is,
and the runner's analysis lane serves it (`work.py`). `jobs.employer` gives the task and `jobs.employer_report`
takes the report. Services flush; the tool or the schedule pass commits.
"""

import logging
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from process_engine.models import COMPLETED
from process_engine.registry import actions as registry
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.errors import DomainError
from app.jobsearch import ats, limits, process, stages
from app.jobsearch.actors import BY_APP
from app.jobsearch.address import is_board_link
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.employers import match as employer_match
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.employers import records as employer_records
from app.jobsearch.employers import resolve
from app.jobsearch.employers.core import EmployerError
from app.jobsearch.fetch import polite
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Profile
from app.jobsearch.profile.scoring import Group
from app.jobsearch.research import analysis, line, settle
from app.jobsearch.work import hand_outs, instruction_files, stuck
from app.sites import avoid, where
from app.timeutil import utc_now
from app.trail import service as trail

log = logging.getLogger(__name__)

#: Who the researcher is called in Activity and on what it remembers.
BY = "Researcher"
#: An employer's research: none yet | waiting for the researcher | done | failed (a session took it and never sent it,
#: or the stuck sweep gave up on it). A failed one is picked again the next night, after the others.
WAITING, DONE, FAILED = "waiting", "done", "failed"
#: How long a session keeps the employer it took before another may take it (the times it may be taken and never
#: answered are `hand_outs.MAX_CLAIMS`). One nobody has taken waits in line however long it takes.
CLAIM_LEASE = timedelta(minutes=20)
#: The facts a report may hold (docs/PRD.md 36.2), and the most roles, candidates and sentences a task or card holds.
FACTS = ("website", "careers_url", "reviews_url", "industry", "sectors", "employees", "ownership", "headquarters",
         "offices", "founded", "ai_native", "events")
ROLES_SHOWN, CANDIDATES_SHOWN, MOST_PROPOSALS = 12, 10, 12
SOURCE_CHARS = analysis.SOURCE_CHARS
#: Where each employer fact a gate or an ask reads is found on the record, by the researcher's field (the app decides
#: `local` itself, from the offices).
FACT_FIELDS = {"years_since_founded": "founded", "employees": "employees", "size_band": "employees",
               "ownership": "ownership",
               "ai_native": "ai_native", "industry": "industry and sectors", "local": "headquarters and offices",
               "work_style": None, "years_since_layoff": None, "layoffs_in_2_years": None}
NOTHING = "No employer is waiting for research. Stop here."


class ResearchError(DomainError):
    status = 422


# --- which employers, and in what order (36.1) ----------------------------------------------------------------------


#: The statuses a role holds that make its employer worth researching (the owner, 2026-10-21: no research on roles that
#: score low): on deck, Ready to Launch, Applied and Active Discussion. An employer with none is not researched.
WORTH_RESEARCH = (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH, stages.APPLIED, stages.ACTIVE_DISCUSSION)
#: Set on the research of an employer queued because a role of it went on deck: it is handed out at any hour, and the
#: deck's read of that role waits for it a short while (`EMPLOYER_WAIT`).
FOR_DECK = "for_deck"
#: Whether a role going on deck queues its employer's research (`queue_for_deck`) and its read waits for it. The tests
#: of the analysis flow run without it (tests/conftest.py); tests/jobsearch/test_deck_employer_research.py turns it on.
DECK_RESEARCH = True
#: The longest the deck's read of a role waits for its employer's research; after it the read goes on without it.
EMPLOYER_WAIT = timedelta(minutes=15)


async def picks(session: AsyncSession, owner: str, limit: int | None, again_after_days: int,
                now: datetime | None = None) -> list[Employer]:
    """The employers the night should research, up to `limit` (None: all): those with a role on deck or later
    (`WORTH_RESEARCH`), not gated and not already with the researcher, and not researched in the last
    `again_after_days`. The order is 36.1's: the one with the most open roles, then the never researched (one that
    failed last time after the rest), then by name."""
    cutoff = (now or utc_now()) - timedelta(days=again_after_days)
    worth = select(Opportunity.employer_id).where(
        Opportunity.owner == owner, Opportunity.employer_id.is_not(None), Opportunity.status.in_(WORTH_RESEARCH))
    rows = (await session.scalars(select(Employer).where(
        Employer.owner == owner, Employer.gated.is_(False), Employer.id.in_(worth),
        or_(Employer.research_state.is_(None), Employer.research_state != WAITING),
        or_(Employer.researched_at.is_(None), Employer.researched_at < cutoff)))).all()
    in_play = set((await session.scalars(select(Opportunity.employer_id).where(
        Opportunity.owner == owner, Opportunity.employer_id.is_not(None), Opportunity.status.in_(stages.OPEN))
        .distinct())).all())

    def key(e: Employer) -> tuple:
        return (e.id not in in_play, -(e.open_roles or 0), e.research_state == FAILED, e.researched_at is not None,
                e.name.lower(), str(e.id))

    return limits.upto(sorted(rows, key=key), limit if limit is not None else limits.NO_LIMIT)


async def start(session: AsyncSession, owner: str, employer: Employer, *, for_deck: bool = False) -> Employer:
    """Put an employer in front of the researcher. One research at a time per employer. `for_deck`: a role of it went
    on deck (`queue_for_deck`)."""
    if employer.research_state == WAITING:
        raise ResearchError("this employer is with the researcher already")
    employer.research_state, employer.research_asked_at, employer.research_claimed_at = WAITING, utc_now(), None
    employer.research = {k: v for k, v in (employer.research or {}).items() if k not in ("claims", FOR_DECK)}
    if for_deck:
        employer.research = {**(employer.research or {}), FOR_DECK: True}
    await session.flush()
    instance = await process.engine().start(
        session, process.RESEARCH_EMPLOYER, {"owner": owner, "employer": str(employer.id)},
        subject=f"jobsearch:employer:{employer.id}")
    employer.research_instance_id = instance.id
    await session.flush()
    return employer


def _needing_deck_research(owner: str, again_after_days: int, now: datetime | None):
    """The where clause of the employers `queue_for_deck` queues: of the roles on deck, not gated, not with the
    researcher, never researched or researched before the cutoff."""
    cutoff = (now or utc_now()) - timedelta(days=again_after_days)
    on_deck = select(Opportunity.employer_id).where(
        Opportunity.owner == owner, Opportunity.employer_id.is_not(None),
        or_(Opportunity.status == stages.SECOND_IMPRESSION, line.marked_next()))
    return (Employer.owner == owner, Employer.gated.is_(False), Employer.id.in_(on_deck),
            or_(Employer.research_state.is_(None), Employer.research_state == DONE),
            or_(Employer.researched_at.is_(None), Employer.researched_at < cutoff))


async def deck_research_ids(session: AsyncSession, owner: str, again_after_days: int,
                            now: datetime | None = None) -> list[uuid.UUID]:
    """The ids `queue_for_deck` would queue, read alone (a reader's session can ask: `off_turn.reading`)."""
    if not DECK_RESEARCH:
        return []
    return list((await session.scalars(select(Employer.id).where(
        *_needing_deck_research(owner, again_after_days, now)))).all())


async def queue_for_deck(session: AsyncSession, owner: str, again_after_days: int, now: datetime | None = None,
                         *, only: Collection[uuid.UUID] | None = None) -> int:
    """Research, at once, of the employers of the roles on deck that have none: never researched or researched more
    than `again_after_days` ago, not gated, not with the researcher already, and not one whose research failed (the
    night picks that again after the others). Handed out at any hour, ahead of the deck's read
    (`session_kinds.next_kind`). `only`: just these employers, each judged afresh by the same rule (ids read earlier
    by `deck_research_ids`). Returns how many were queued."""
    if not DECK_RESEARCH:
        return 0
    query = select(Employer).where(*_needing_deck_research(owner, again_after_days, now))
    if only is not None:
        query = query.where(Employer.id.in_(list(only)))
    rows = (await session.scalars(query)).all()
    for employer in rows:
        await start(session, owner, employer, for_deck=True)
    return len(rows)


async def held_back_ids(session: AsyncSession, owner: str, now: datetime | None = None) -> set[uuid.UUID]:
    """The roles on deck whose employer's research was queued for the deck less than `EMPLOYER_WAIT` ago and is not
    done: their read waits for it a short while, so it can use the employer's record. A failed or late research does
    not hold the read."""
    if not DECK_RESEARCH:
        return set()
    moment = now or utc_now()
    flag = Employer.research[FOR_DECK].as_boolean()
    return set(await session.scalars(
        select(Opportunity.id).join(Employer, Employer.id == Opportunity.employer_id).where(
            Opportunity.owner == owner, Opportunity.analysis_state == analysis.WAITING,
            Employer.research_state == WAITING, flag.is_(True), Employer.research_asked_at > moment - EMPLOYER_WAIT)))


# --- the line a session takes from ----------------------------------------------------------------------------------


def _free(moment: datetime):
    """The employers with the researcher that no session has right now: never taken, or taken longer ago than the
    lease."""
    return or_(Employer.research_claimed_at.is_(None), Employer.research_claimed_at < moment - CLAIM_LEASE)


async def free(session: AsyncSession, owner: str, now: datetime | None = None, *, mine: Collection[uuid.UUID] = (),
               scope: str | None = None) -> list[uuid.UUID]:
    """The employers a session could take now, in the order they would be handed: the one that has waited longest
    first, leaving out the ones a session holds and the ones claimed for another session (`hand_outs.py`), and the ones
    claimed for the caller (`mine`) first. What makes `researcher` the answer to "which work waits"
    (`session_kinds.next_kind`), what a claim holds, and what `claim_next` takes from."""
    query = select(Employer.id).where(Employer.owner == owner, Employer.research_state == WAITING,
                                      _free(now or utc_now()))
    if skip := await hand_outs.held_uuids(session, owner, hand_outs.RESEARCHER):
        query = query.where(Employer.id.not_in(skip))
    return hand_outs.claimed_first((await session.scalars(query.order_by(Employer.research_asked_at))).all(), mine)


async def waiting(session: AsyncSession, owner: str) -> Employer | None:
    """The employer that has waited longest for the researcher."""
    return await session.scalar(select(Employer).where(Employer.owner == owner, Employer.research_state == WAITING)
                                .order_by(Employer.research_asked_at).limit(1))


def _times_taken(employer: Employer) -> int:
    return int((employer.research or {}).get("claims", 0))


async def _drop(session: AsyncSession, employer: Employer) -> None:
    await _give_up(session, employer, "no session managed to send its report")


async def _take(session: AsyncSession, employer: Employer, now: datetime) -> bool:
    employer.research_claimed_at = now
    employer.research = {**(employer.research or {}), "claims": _times_taken(employer) + 1}
    return True


#: What the ledger needs to take an employer for a session (`hand_outs.take_next`).
LINE = hand_outs.Line(free=free, model=Employer, take=_take, taken=_times_taken, give_up=_drop,
                      where=lambda moment: (Employer.research_state == WAITING, _free(moment)))


async def claim_next(session: AsyncSession, owner: str, now: datetime | None = None, *,
                     session_id: str | None = None, claim: str | None = None) -> Employer | None:
    """The employer that has waited longest for the researcher and that no session has, taken by the session that asks
    (`hand_outs.take_next`), or None: the one claimed for it (`claim`) while it is free, and never one claimed for
    another session. An employer whose session died is free again when its lease ends; one taken
    `hand_outs.MAX_CLAIMS` times and never answered is failed instead."""
    taken = await hand_outs.take_next(session, owner, hand_outs.RESEARCHER, [LINE], now=now or utc_now(),
                                      session_id=session_id, claim=claim)
    return taken[0] if taken else None


async def _give_up(session: AsyncSession, employer: Employer, why: str) -> None:
    await stuck.cancel_instance(session, employer.research_instance_id, why, f"the research of employer {employer.id}")
    employer.research_state = FAILED
    await trail.record(session, employer.owner, event="employer_research_failed", actor="agent", who=BY,
                       sentence=f"The researcher didn't finish researching {employer.name}.",
                       link=(employer_records.LINK, employer.id), detail=why)
    await session.flush()


async def settle_stuck(session: AsyncSession, owner: str, now: datetime | None = None,
                       since: datetime | None = None) -> int:
    """A research the researcher never sent fails, so the line moves on: a session took the employer and `stuck_minutes`
    have passed since (counted from `since` when that is later: the end of peak hours). An employer no session has taken
    is waiting in line, not stuck, and stays however long it waits. Returns how many."""

    async def find(cutoff: datetime) -> list[Employer]:
        return list((await session.scalars(select(Employer).where(
            Employer.owner == owner, Employer.research_state == WAITING,
            Employer.research_claimed_at.is_not(None), Employer.research_claimed_at < cutoff))).all())

    async def close(employer: Employer) -> None:
        await _give_up(session, employer, "the researcher didn't send its report")

    return await stuck.settle_stuck(session, now=now, since=since, what="employer research", find=find, close=close)


async def release(session: AsyncSession, owner: str, ids: Iterable[str], why: str) -> int:
    """Send back the employers of a night that was stopped (the finish time, Claude's usage limit) that no session has
    finished: each research is cancelled and the employer is in the line for the next night. Returns how many."""
    rows = (await session.scalars(select(Employer).where(
        Employer.owner == owner, Employer.id.in_([uuid.UUID(i) for i in ids]),
        Employer.research_state == WAITING))).all()
    for employer in rows:
        await stuck.cancel_instance(session, employer.research_instance_id, why,
                                    f"the research of employer {employer.id}")
        employer.research_state, employer.research_instance_id, employer.research_claimed_at = None, None, None
    await session.flush()
    return len(rows)


async def activity(session: AsyncSession, owner: str) -> str:
    """What the Agents page says the researcher is doing."""
    employer = await waiting(session, owner)
    return f"Researching {employer.name}" if employer else "Researching an employer"


def run_words(rows: Sequence[Employer], total: int) -> str:
    """What a night's batch came to, in one sentence for the mission's run: how many were researched and what that
    brought (facts set, job boards found, records merged, splits made), and how many failed. `total` is how
    many were sent; one merged into another record while it waited is not in `rows` and was researched by the other."""
    done = [r for r in rows if r.research_state == DONE]
    came = [(r.research or {}).get("came_to") or {} for r in done]
    brought = [
        (sum(len((r.research or {}).get("set") or []) for r in done), "fact", "facts", "set"),
        (sum(1 for c in came if c.get("board")), "job board", "job boards", "found"),
        (sum(int(c.get("merged") or 0) for c in came), "record", "records", "merged"),
    ]
    said = ", ".join(f"{n} {one if n == 1 else many} {verb}" for n, one, many, verb in brought if n)
    failed = sum(1 for r in rows if r.research_state == FAILED)
    gone = total - len(rows)
    text = f"Researched {len(done)} of {total} employer{'s' if total != 1 else ''}" + (f": {said}" if said else "")
    text += f"; {failed} failed" if failed else ""
    return text + (f"; {gone} merged into another record" if gone else "") + "."


# --- what the owner's gates and asks read ---------------------------------------------------------------------------


def facts_read(profile: Profile, target_role: str | None) -> set[str]:
    """The employer facts the gates and asks that count for a role with this target role read (25.4): an ask's, and a
    statement's of an "any of" or "first match" row (docs/PRD.md 30.9), which the app decides from the record."""
    read = {g.fact for g in profile.gates.lines_for(target_role) if g.kind == "employer_fact" and g.fact}
    # a job dial reads the posting, not an employer's record: only the employer's facts are researched
    items = profile.items_for(target_role)
    asks = [s for item in items for s in (item.members if isinstance(item, Group) else [item])]
    return {fact for fact in read | {a.fact for a in asks if a.fact} if fact in FACT_FIELDS}


def all_facts_read(profile: Profile) -> set[str]:
    """Every employer fact something of the owner's reads, for any role."""
    targets = [None, *(t.id for t in profile.criteria.target_roles)]
    return {fact for target in targets for fact in facts_read(profile, target)}


def lacks(employer: Employer, fact: str, today=None) -> bool:
    """Whether the record holds nothing of this fact yet (`employers.facts` leaves a fact it lacks out; a layoff fact of
    None says it was researched and none found)."""
    known = employers.facts(employer, today)
    return fact not in known or (known[fact] is None and fact != "years_since_layoff")


async def want_facts(session: AsyncSession, owner: str, before: Profile | None, after: Profile) -> int:
    """A gate or an ask that is new reads a fact some employer's record lacks (docs/PRD.md 39.18): the employers of the
    roles in play it reaches that lack the fact are put back in the researcher's line for the next night (the roles it
    reaches wait for the fact; a gate on a fact not yet known passes). A gate or ask that was there before sends no
    one. Returns how many employers."""
    new_for: dict[str | None, set[str]] = {}
    for target in [None, *(t.id for t in after.criteria.target_roles)]:
        new = facts_read(after, target) - (facts_read(before, target) if before is not None else set())
        if new:
            new_for[target] = new
    if not new_for:
        return 0
    rows = (await session.execute(
        select(Opportunity.employer_id, Opportunity.target_role).where(
            Opportunity.owner == owner, Opportunity.employer_id.is_not(None), Opportunity.status.in_(stages.OPEN))
        .distinct())).all()
    known = {e.id: e for e in (await session.scalars(select(Employer).where(
        Employer.owner == owner, Employer.id.in_({r[0] for r in rows}), Employer.gated.is_(False)))).all()}
    again: set[uuid.UUID] = set()
    for employer_id, target in rows:
        employer = known.get(employer_id) if employer_id is not None else None
        wanted = new_for.get(None, set()) | new_for.get(target, set())
        if employer is not None and any(lacks(employer, fact) for fact in wanted):
            again.add(employer.id)
    for employer_id in again:
        if known[employer_id].research_state != WAITING:
            known[employer_id].researched_at = None
    await session.flush()
    return len(again)


def said_for(count: int) -> str | None:
    """The line a save says of the employers it sends back to research: None when none."""
    if not count:
        return None
    return (f"{count:,} employer{'s lack' if count != 1 else ' lacks'} a fact that this reads: the researcher looks "
            "for it tonight.")


# --- the task -------------------------------------------------------------------------------------------------------


def _known(employer: Employer) -> dict:
    """The employer's record as the researcher is shown it: the facts it may fill, each with where it came from, and
    the fields the owner set (which it cannot change)."""
    out = {name: getattr(employer, name) for name in FACTS
           if getattr(employer, name, None) not in (None, [], "unknown")}
    return {"facts": out, "sources": {k: (v.get("source") if isinstance(v, dict) else v)
                                      for k, v in (employer.sources or {}).items() if k in out},
            "owner_set": sorted(set(employer.owner_fields or []) & set(FACTS))}


def _board_of(employer: Employer) -> dict | None:
    """The job system the employer is known to post on, for the task."""
    if not employer.ats:
        return None
    return {"system": ats.label(employer.ats), "address": employer.board_url,
            "whole": ats.complete(employer.ats, resolve.address(employer.board))}


async def _candidates(session: AsyncSession, owner: str, employer: Employer) -> list[dict]:
    """The pairs code found that include this employer and nobody has decided about: the other record, and why the two
    look the same (docs/PRD.md 36.4)."""
    out = []
    for c in await employer_pairs.candidates(session, owner):
        if employer.id not in (c.keep.id, c.away.id):
            continue
        other = c.away if c.keep.id == employer.id else c.keep
        tally = await employer_records.tally(session, owner, other.id)
        out.append({"employer": str(other.id), "name": other.name, "aliases": other.aliases or [],
                    "website": other.website, "careers_url": other.careers_url,
                    "headquarters": other.headquarters, "industry": other.industry, "roles": tally["roles"],
                    "looks_the_same_because": c.reasons,
                    "code_merges_these_on_its_own": c.decision.action == employer_match.MERGE})
    return out[:CANDIDATES_SHOWN]


async def brief(session: AsyncSession, owner: str, employer: Employer) -> dict:
    """The researcher's task for one employer."""
    profile = await profile_store.current(session, owner)
    rows = (await session.scalars(
        select(Opportunity).where(Opportunity.owner == owner, Opportunity.employer_id == employer.id)
        .options(defer(Opportunity.description)).order_by(Opportunity.found.desc(), Opportunity.title)
        .limit(ROLES_SHOWN))).all()
    total = (await employer_records.tally(session, owner, employer.id))["roles"]
    return {
        "employer": {"id": str(employer.id), "name": employer.name, "aliases": employer.aliases or [],
                     **_known(employer), "job_board": _board_of(employer)},
        "roles": [{"id": str(r.id), "title": r.title, "stage": r.status, "place": r.location, "link": r.url,
                   "site": avoid.host_of(r.url)} for r in rows],
        "more_roles": max(total - len(rows), 0),
        "may_be_the_same_as": await _candidates(session, owner, employer),
        "the_owner_reads": sorted(
            ({"fact": fact, "report": FACT_FIELDS.get(fact)} for fact in all_facts_read(profile)),
            key=lambda x: x["fact"]),
        "instructions": await instruction_files.read(session, owner, "instructions", "jobsearch.researcher"),
        "response_template": await instruction_files.read(session, owner, "templates", "jobsearch.researcher.report"),
    }


async def task_reply(session: AsyncSession, owner: str, *, session_id: str | None = None,
                     claim: str | None = None) -> dict:
    """`jobs.employer`: taking the task takes the employer, the one claimed for this session (`claim`), else the one
    that has waited longest and that no other session has, and it is this session's until its lease ends."""
    employer = await claim_next(session, owner, session_id=session_id, claim=claim)
    if employer is None:
        return {"ok": True, "task": None, "note": NOTHING}
    return {"ok": True, **await brief(session, owner, employer)}


# --- the report: what code checks (36.3) ----------------------------------------------------------------------------


@dataclass
class Checked:
    """A report's facts after the check: the ones that stand (by field, as the record would hold them), where each
    came from, and the ones refused, each with why."""

    facts: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    refused: list[dict] = field(default_factory=list)


def _one(name: str, value):
    """One fact through the analyst's own model (its sizes, its words, its addresses): the value as it would be kept,
    else why not."""
    if isinstance(value, bool) and name in ("employees", "founded"):
        return None, "a number"
    try:
        kept = getattr(analysis.EmployerFacts.model_validate({name: value}), name)
    except ValidationError as exc:
        first = exc.errors()[0]
        return None, first["msg"][:200]
    if kept is None and name in ("website", "careers_url", "reviews_url"):
        return None, "not a web address: it starts with https:// and has no sign-in in it"
    return kept, None


def check(employer: Employer, body: dict, hosts: frozenset[str] | set[str] = frozenset()) -> Checked:
    """Check the facts of a report one at a time, so a refused fact leaves the rest standing (docs/PRD.md 36.3). A fact
    without a source is refused. Addresses are public and are not a job board; a careers site is on the employer's own
    domain or a known job system; employees is a number; ownership is one of the allowed words (`unknown` is no
    finding and is left out); the reviews page is a public https address."""
    out = Checked()
    facts, sources = body.get("facts"), body.get("sources")
    reported: dict = facts if isinstance(facts, dict) else {}
    said: dict = sources if isinstance(sources, dict) else {}
    for name in reported:
        if name not in FACTS:
            out.refused.append({"field": str(name)[:40], "reason": f"not a fact you may report ({', '.join(FACTS)})"})
    for name in FACTS:
        value = reported.get(name)
        # events: an empty list with a source is "looked for layoffs and found none", and is kept; without a source it
        # is refused, so a researcher that did not look never marks the record checked
        if value is None or value == "" or (value == [] and name != "events"):
            continue
        if name == "ownership" and value == "unknown":
            continue
        source = " ".join(str(said.get(name) or "").split())[:SOURCE_CHARS]
        if not source:
            out.refused.append({"field": name, "reason": "no source: say where you found it, a web address or words"})
            continue
        kept, why = _one(name, value)
        if why is None and kept is not None and name in ("website", "careers_url", "reviews_url"):
            why = _address_problem(employer, name, kept, out.facts.get("website") or employer.website, hosts)
        if why is not None:
            out.refused.append({"field": name, "reason": why})
            continue
        out.facts[name], out.sources[name] = kept, source
    return out


def _address_problem(employer: Employer, name: str, link: str, website: str | None,
                     hosts: frozenset[str] | set[str]) -> str | None:
    """Why an address cannot be kept, or None when it can."""
    if name == "reviews_url":
        return None if employers.reviews_link(link) else (
            "must be a public https address, as a public web search showed it: leave it out otherwise")
    if not polite.public_url(link):
        return "is not a public web address (it starts with https:// and has no sign-in in it)"
    if name == "website":
        if is_board_link(link, hosts) or employer_pairs.domain_of(link) is None:
            return "is a job board, a job system or a page anyone can have, not the employer's own site"
        return None
    return employer_posting.check_employer_link(link, employer, field="careers_url", website=website, hosts=hosts,
                                                one_posting=False)


async def _board_problem(session: AsyncSession, owner: str, employer: Employer, link: str,
                         hosts: frozenset[str] | set[str]) -> tuple[ats.Board | None, str | None]:
    """The board a reported address names on its job system, or why it cannot be kept."""
    if not polite.public_url(link):
        return None, "is not a public web address (it starts with https:// and has no sign-in in it)"
    if is_board_link(link, hosts):
        return None, "is a job board, not the employer's own job system"
    named = ats.parse(link)
    found = named.board if named else None
    if not found:
        return None, "names no job system the app knows: if it is the employer's careers page, send it as careers_url"
    if resolve.taken(await resolve.held_boards(session, owner), employer, *found):
        return None, f"is another employer's {ats.label(found.system)} board"
    return found, None


def _evidence(raw: dict) -> employer_pairs.Evidence | str:
    """A verdict and its reasons as the pair feature takes them, or why not."""
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in (employer_pairs.SAME, employer_pairs.UNSURE, employer_pairs.DIFFERENT):
        return f"a verdict is one of {employer_pairs.SAME}, {employer_pairs.UNSURE}, {employer_pairs.DIFFERENT}"
    reasons = raw.get("reasons")
    said = tuple(str(r) for r in (reasons if isinstance(reasons, list) else [reasons]) if str(r or "").strip())
    if verdict != employer_pairs.DIFFERENT and not said:
        return "say what you found and where: a reason for the owner to read"
    return employer_pairs.Evidence(verdict, said)


async def _proposals(session: AsyncSession, owner: str, employer: Employer, body: dict) -> tuple[dict, list[dict]]:
    """The report's verdicts on pairs and its splits, checked: what is kept for the settle step (ids and words, no
    objects) and why the rest was refused. A verdict names an employer the task offered; a split names roles that are
    this employer's."""
    refused: list[dict] = []
    kept: dict = {"same_as": [], "split": []}
    offered = {c["employer"] for c in await _candidates(session, owner, employer)}
    for n, raw in enumerate((body.get("same_as") or [])[:MOST_PROPOSALS], 1):
        label = f"same_as {n}"
        raw = raw if isinstance(raw, dict) else {}
        other = str(raw.get("employer") or "")
        evidence = _evidence(raw)
        if other not in offered:
            refused.append({"field": label, "reason": "names an employer the task did not offer: use the ids in "
                                                      "may_be_the_same_as"})
        elif isinstance(evidence, str):
            refused.append({"field": label, "reason": evidence})
        else:
            kept["same_as"].append({"employer": other, "verdict": evidence.verdict, "reasons": list(evidence.reasons)})
    mine = {str(i) for i in (await session.scalars(select(Opportunity.id).where(
        Opportunity.owner == owner, Opportunity.employer_id == employer.id))).all()}
    for n, raw in enumerate((body.get("split") or [])[:MOST_PROPOSALS], 1):
        label = f"split {n}"
        raw = raw if isinstance(raw, dict) else {}
        roles = [str(r) for r in (raw.get("roles") or [])] if isinstance(raw.get("roles"), list) else []
        name = " ".join(str(raw.get("new_name") or "").split())
        said = _evidence({"verdict": employer_pairs.SAME, "reasons": raw.get("reasons")})
        if not roles or not name:
            refused.append({"field": label, "reason": "needs roles (their ids) and new_name (the other company)"})
        elif not set(roles) <= mine:
            refused.append({"field": label, "reason": "names a role that is not this employer's: use the ids in roles"})
        elif isinstance(said, str):
            refused.append({"field": label, "reason": said})
        else:
            kept["split"].append({"roles": roles, "new_name": name, "reasons": list(said.reasons)})
    return kept, refused


async def submit(session: AsyncSession, owner: str, body: dict) -> dict:
    """`jobs.employer_report`: take the researcher's report. A fact that fails the check is refused with why and the
    rest stands (docs/PRD.md 36.3); a report that is not one at all, or is for an employer that is not waiting, is
    refused whole."""
    if not isinstance(body, dict):
        raise ResearchError("the report is an object: {employer, facts, sources, ...}")
    try:
        employer = await session.get(Employer, uuid.UUID(str(body.get("employer"))))
    except ValueError:
        employer = None
    if employer is None or employer.owner != owner:
        raise ResearchError("no such employer: use the id jobs.employer gave")
    if employer.research_state != WAITING:
        raise ResearchError("that employer isn't waiting for research")
    if not any(k in body for k in ("facts", "same_as", "split", "board_url", "notes")):
        raise ResearchError("that is not a report: send the employer, and facts (or an empty facts if you found none)")
    hosts = await employer_posting.job_board_hosts(session, owner)
    checked = check(employer, body, hosts)
    sources = body.get("sources")
    said_of: dict = sources if isinstance(sources, dict) else {}
    refused = list(checked.refused)
    board, wanted = None, " ".join(str(body.get("board_url") or "").split())
    if wanted:
        source = " ".join(str(said_of.get("board_url") or "").split())[:SOURCE_CHARS]
        found, why = await _board_problem(session, owner, employer, wanted, hosts)
        if not source:
            why = why or "no source: say where you found it, a web address or words"
        if found is None or why:
            refused.append({"field": "board_url", "reason": why or "is not a job system the app knows"})
        else:
            board = {"ats": found.system, "board": found.parts, "source": source}
    proposals, problems = await _proposals(session, owner, employer, body)
    refused += problems
    mine = set(employer.owner_fields or [])
    left = [n for n in checked.facts if n in mine or (n == "careers_url" and employer.careers_url)]
    employer.research = {
        **{k: v for k, v in (employer.research or {}).items() if k == "claims"},
        "report": {"facts": checked.facts, "sources": checked.sources, "board": board,
                   "same_as": proposals["same_as"], "split": proposals["split"],
                   "notes": " ".join(str(body.get("notes") or "").split())[:1000]},
        "refused": refused, "at": utc_now().isoformat()}
    await session.flush()
    # The step that looks for the employer's job system reads its pages and the open feeds: the engine stops there and
    # it runs outside the engine's call, with the report saved first and no transaction open while it reads.
    moving = process.reading_engine()
    instance = await moving.complete(session, employer.research_instance_id, process.RESEARCHER_STEP,
                                     {"submitted": True})
    instance = await process.run_deferred(session, moving, instance.id)
    if instance.status != COMPLETED:
        raise ResearchError(instance.error or f"the research's process {instance.status}")
    return {"ok": True, "taken": True, "set": [n for n in checked.facts if n not in left], "left_alone": left,
            "refused": refused}


# --- the process's steps (playbooks/jobsearch/research_employer.bpmn) -----------------------------------------------


async def _employer(session: AsyncSession, data: dict) -> Employer:
    employer = await session.get(Employer, uuid.UUID(data["employer"]))
    if employer is None:
        raise ResearchError("the employer is gone")
    return employer


async def take_facts(session: AsyncSession, employer: Employer) -> list[str]:
    """Set the facts of the checked report on the record, the way the analyst's are: never over a field the owner set,
    a careers site only where none is kept, each with its source. Returns the fields set."""
    report = (employer.research or {}).get("report", {})
    facts = analysis.EmployerFacts.model_validate({**report.get("facts", {}), "sources": report.get("sources", {})})
    done = analysis._research(employer, facts, by=BY)
    employer.research = {**(employer.research or {}), "set": done,
                         "left_alone": [n for n in report.get("facts", {}) if n not in done]}
    await session.flush()
    return done


async def action_take(session, instance, data):
    employer = await _employer(session, data)
    return {"set": len(await take_facts(session, employer))}


async def action_gather(session, instance, data):
    employer = await _employer(session, data)
    return {"ready": employer.research_state == WAITING,
            "candidates": len(await _candidates(session, employer.owner, employer))}


def _wants_a_board(employer: Employer) -> bool:
    """Whether the app may look for the employer's job system: not gated, not the owner's word, and it has none or only
    part of a board."""
    mine = employer.ats_found_by != "owner" and "ats" not in (employer.owner_fields or [])
    return mine and not employer.gated and (employer.ats is None or not ats.complete(
        employer.ats, resolve.address(employer.board)))


async def find_board(session: AsyncSession, owner: str, employer: Employer) -> dict:
    """The employer's job board, from the address the researcher gave and then by the app's own detection (the links it
    holds, the careers page, the open feeds: what code can do). Never over the owner's word. Returns whether the
    employer has a job system now that it had none of before."""
    had = bool(employer.ats and ats.complete(employer.ats, resolve.address(employer.board)))
    given = (employer.research or {}).get("report", {}).get("board")
    if given and _wants_a_board(employer):
        resolve.take(employer, given["ats"], given["board"], "analyst")
    if _wants_a_board(employer):
        await resolve.from_links(session, owner)
        fetch = polite.make()
        try:
            await resolve.complete_boards(session, owner, fetch, limit=1, only={employer.id})
            await resolve.from_pages(session, owner, fetch, limit=1, only={employer.id})
            await resolve.probe(session, owner, fetch, only={employer.id})
        finally:
            await fetch.close()
    found = bool(employer.ats and ats.complete(employer.ats, resolve.address(employer.board)))
    if found and not had:
        await where.seed_employer_sites(session, owner)
    return {"found": found and not had, "has": found}


async def action_board(session, instance, data):
    employer = await _employer(session, data)
    return await find_board(session, employer.owner, employer)


@dataclass
class Pairs:
    """What the researcher's word about pairs and splits came to, and the record that is left (the researched one, or
    the one it was merged into)."""

    employer: Employer
    merged: int = 0
    kept_separate: int = 0
    splits: int = 0
    problems: list[str] = field(default_factory=list)


async def settle_pairs(session: AsyncSession, owner: str, employer: Employer) -> Pairs:
    """The researcher's verdicts, through the app's own feature (docs/PRD.md 36.5, 36.6, 39.19), then the pairs the
    table in `employer_match` says to merge now that the facts just set are on the record (a website this report
    confirmed, with the other record's confirmed too; a website beside alike names), merged without asking. A refusal
    is kept as a problem: the rest goes on."""
    report = (employer.research or {}).get("report", {})
    out = Pairs(employer)
    kept_id = employer.id
    for item in report.get("same_as", []):
        try:
            async with session.begin_nested():
                done = await employer_pairs.propose_merge(
                    session, owner, kept_id, uuid.UUID(item["employer"]),
                    employer_pairs.Evidence(item["verdict"], tuple(item["reasons"])), by=BY)
        except EmployerError as exc:
            out.problems.append(str(exc))
            continue
        if done.outcome == employer_pairs.MERGED and done.employer is not None:
            out.merged, kept_id = out.merged + 1, done.employer.id
        else:
            out.kept_separate += 1
    for item in report.get("split", []):
        try:
            async with session.begin_nested():
                done = await employer_pairs.propose_split(
                    session, owner, kept_id, role_ids=item["roles"], new_name=item["new_name"],
                    evidence=employer_pairs.Evidence(employer_pairs.SAME, tuple(item["reasons"])), by=BY)
        except EmployerError as exc:
            out.problems.append(str(exc))
            continue
        out.splits += done.outcome == employer_pairs.SPLIT_DONE
    # Certain now, by the same table as the night's pass: a website confirmed for both records, or beside names that
    # look alike, or the same board beside names that look the same. Code merges these without asking, whoever noticed;
    # the researcher said "different" of a pair it was handed and that stays so (`candidates` leaves it out).
    for _ in range(employer_pairs.MAX_ROUNDS):
        sure = [c for c in await employer_pairs.candidates(session, owner)
                if c.decision.action == employer_match.MERGE and kept_id in (c.keep.id, c.away.id)]
        if not sure:
            break
        try:
            async with session.begin_nested():
                done = await employer_records.merge(session, owner, sure[0].keep.id, sure[0].away.id,
                                                    why=sure[0].decision.words, by=BY_APP)
        except EmployerError as exc:
            out.problems.append(str(exc))
            break
        if not done.merged:
            break
        out.merged, kept_id = out.merged + 1, done.employer.id
    out.employer = await session.get(Employer, kept_id) or employer
    return out


async def action_pairs(session, instance, data):
    employer = await _employer(session, data)
    pairs = await settle_pairs(session, employer.owner, employer)
    return {"employer": str(pairs.employer.id), "merged": pairs.merged,
            "kept_separate": pairs.kept_separate, "splits": pairs.splits, "problems": pairs.problems}


async def refresh_roles(session: AsyncSession, owner: str, employer_ids: Iterable[uuid.UUID], *, cause: str,
                        keep_gated_out: bool = False) -> settle.Settled:
    """Settle these employers' roles again on the facts their records hold now (`settle.roles`, docs/PRD.md 39.14,
    39.18, 39.21): a role a gate now stops is Gated out with that gate and the versions, one only a gate stopped that no
    longer does is back in play, and a researched role's score is worked out again from the facts. A role the owner
    moved by hand, or at Applied or later, keeps its stage. With `keep_gated_out` a role in Gated out stays there,
    whatever the record says now (a split leaves the roles it moved that are in Gated out for the old record's hard pass
    there, for the owner to move back by hand). Services flush."""
    ids = list(employer_ids)
    query = (select(Opportunity).where(Opportunity.owner == owner, Opportunity.employer_id.in_(ids))
             .options(defer(Opportunity.description)).order_by(Opportunity.created_at, Opportunity.id))
    roles = list((await session.scalars(query)).all()) if ids else []
    return await settle.roles(session, roles, cause=cause, keep_gated_out=keep_gated_out)


async def after_a_change(session: AsyncSession, owner: str, employer_ids: Iterable[uuid.UUID], *, cause: str,
                         keep_gated_out: bool = False) -> None:
    """A merge or a split moved roles between records, or changed a record's facts: the gates and scores of the roles it
    reaches are worked out again (docs/PRD.md 39.19). It runs in a savepoint: a failure is logged and never undoes the
    merge or split, which stand on their own."""
    try:
        async with session.begin_nested():
            await refresh_roles(session, owner, employer_ids, cause=cause, keep_gated_out=keep_gated_out)
    except Exception:
        log.exception("couldn't work the gates and scores out again after %s", cause)


async def action_settle(session, instance, data):
    """Mark the research done, work the gates and scores of the employer's roles out again on what it brought, and say
    what happened in Activity."""
    asked = await session.get(Employer, uuid.UUID(data["employer"]))  # None: it was merged into another record
    survivor = await session.get(Employer, uuid.UUID(data["pairs"]["employer"]))
    employer = survivor or asked
    if employer is None:
        raise ResearchError("the employer is gone")
    owner, now = employer.owner, utc_now()
    await employers.mark_local(session, owner)  # the offices it brought decide whether it is local
    done = await refresh_roles(session, owner, [employer.id], cause=f"{employer.name} was researched")
    if asked is not None:
        asked.research_state, asked.researched_at, asked.research_claimed_at = DONE, now, None
        report = dict(asked.research or {})
    else:  # the record that was researched was merged into another: that one is the researched one now
        report = {}
        employer.researched_at = now
    pairs = data["pairs"]
    summary = {"roles": done.roles, "regated": len(done.gated), "rescored": done.scores_moved,
               "merged": pairs["merged"], "splits": pairs["splits"],
               "board": data["board"]["found"], "problems": pairs["problems"]}
    employer.research = {**(employer.research or {}), **report, "came_to": summary}
    await trail.record(session, owner, event="employer_researched", actor="agent", who=BY,
                       sentence=_sentence(employer, report, summary), link=(employer_records.LINK, employer.id),
                       detail=_detail(report, summary))
    await session.flush()
    return {"roles": done.roles, "regated": len(done.gated), "rescored": done.scores_moved}


def _list(words: list[str]) -> str:
    return ", ".join(w.replace("_", " ") for w in words)


def _sentence(employer: Employer, report: dict, summary: dict) -> str:
    set_now = report.get("set") or []
    parts = [f"set {_list(set_now)}" if set_now else "found no new facts"] if report else []
    if summary["board"]:
        parts.append("found its job board")
    if summary["merged"]:
        parts.append(f"merged {summary['merged']} duplicate{'s' if summary['merged'] != 1 else ''}")
    if summary["splits"]:
        parts.append("moved roles to another employer")
    return f"Researched {employer.name}: {'; '.join(parts)}."


def _detail(report: dict, summary: dict) -> str:
    lines = [f"Set: {_list(report['set'])}." if report.get("set") else "",
             f"Left as they were (yours, or already known): {_list(report['left_alone'])}."
             if report.get("left_alone") else "",
             *(f"Refused {r['field']}: {r['reason']}." for r in report.get("refused") or []),
             *(f"Could not: {p}." for p in summary["problems"]),
             f"The gates ran again on {summary['roles']} role{'s' if summary['roles'] != 1 else ''}: "
             f"{summary['regated']} moved; {summary['rescored']} scores changed." if summary["roles"] else ""]
    return "\n".join(line for line in lines if line)


def _register() -> None:
    source = "backend/app/jobsearch/research/employer.py"
    registry.register(
        "jobs.employer_gather", action_gather, inputs=["owner", "employer"], outputs=["ready", "candidates"],
        description="Checks the employer is still waiting for research and counts the pairs code found that include "
                    "it, which the researcher is asked to decide.", source=source)
    registry.register(
        "jobs.employer_take", action_take, inputs=["owner", "employer"], outputs=["set"],
        description="Sets the facts of the researcher's report that passed the check on the employer's record, each "
                    "with its source: never over a field the owner set, a careers site only where none is kept.",
        source=source)
    registry.register(
        "jobs.employer_board", action_board, inputs=["owner", "employer"], outputs=["found", "has"],
        description="Takes the job board the researcher reported and looks for the rest with the app's own detection "
                    "(the links it holds, the careers page, the open feeds), never over the owner's word.",
        source=source)
    registry.register(
        "jobs.employer_pairs", action_pairs, inputs=["owner", "employer"],
        outputs=["employer", "merged", "kept_separate", "splits", "problems"],
        description="Carries out the researcher's verdicts on pairs and its splits through the app's own merge and "
                    "split (a pair the merge table says to merge merges, the rest stay two employers; roles move to "
                    "another employer only when their own postings show it) and asks nobody, then merges what the "
                    "new facts make certain.",
        source=source)
    registry.register(
        "jobs.employer_settle", action_settle, inputs=["owner", "employer"], outputs=["roles", "regated", "rescored"],
        description="Marks the research done and works the gates and scores of the employer's roles out again on "
                    "the facts it brought.", source=source)


_register()
