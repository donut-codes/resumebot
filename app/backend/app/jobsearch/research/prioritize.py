"""Prioritization, the fifth stage (docs/PRD.md 39.20 to 39.22): a role's score is worked out last, by code.

Code takes three things: the answers kept on the role (a reader's, and the analyst's), what research found (the
employer's facts and the classes of the posting's requirements) and the pay ask. A reader gives no score, so a role
holds its answers and no score until it is researched; until then it is listed in the classifier's order
(`queue.queue_key` with the posting's probability), not under a provisional score. The same inputs always give the same
score (`scoring.compute`).

A role's score is worked out when its research or a fact it reads changes, as one step of settling the role
(`settle.roles`: the ask, then the score, then the gates). A save of Prioritization works every score out again from
what is stored, with no Claude, in the same request (`after_save`, called by `profile_store.save`): each score keeps the
version of Prioritization it was worked out under, and the save says in one line what it changed.

An answer is kept under the question's id and the words it was asked in (the analyst's, from its research). A question
the app decides itself from facts never waits on an answer.

Services flush; the route or the command that owns the request commits.
"""

import copy
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import as_read, off_turn
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import comp, freshness, scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.measures import dials
from app.jobsearch.measures.pay_place import role_facts
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Profile
from app.jobsearch.profile.scoring import Group
from app.jobsearch.profile.views import engine_gates
from app.jobsearch.research import requirement_lines
from app.timeutil import owner_today

log = logging.getLogger(__name__)

#: A role's analysis state once the analyst has researched it (`analysis.DONE`; a test keeps the two the same).
RESEARCHED = "done"
#: Who gave an answer, as the role keeps it.
BY_READER, BY_ANALYST = "reader", "analyst"


# --- the inputs of a score ---------------------------------------------------------------------------------------


def facts_of(role: Opportunity, profile: Profile, seen: datetime | None = None,
             posted: freshness.PostedDay | None = None) -> dict:
    """What the app knows of the posting for scoring: its age (in days from the day the role was posted, `posted` as
    `freshness.posted_by_role` gives it, counted as every gate counts it; and in hours from `seen`, when the app first
    saw the posting, if it has that time: `freshness.first_seen_of`), and the likely ask (the worked ask once research
    has one, else the one the posted pay gives)."""
    day = posted or freshness.posted_basis(role.posted, role.found, ())
    known = role_facts({"comp": role.comp, "posted": day.posted, "found": day.found}, engine_gates(profile),
                       owner_today())
    worked = ((role.analysis or {}).get("ask") or {}).get("ask")
    return {"age_days": known["age_days"], "ask": worked if worked is not None else known["ask"],
            **freshness.facts(day.posted, seen, basis=day.basis)}


def employer_of(role: Opportunity, by_id: Mapping[uuid.UUID, Employer]) -> Employer | None:
    """The employer record a role's score reads: the one its link names. A role with no link reads none, whatever its
    company's name matches (the gates look further, `EmployerIndex.of`; the start's employer step links the role). The
    one rule of which record, for every pass that scores (`settle.roles`, `rescore`)."""
    return by_id.get(role.employer_id) if role.employer_id else None


def known_of(role: Opportunity, profile: Profile, employer: Employer | None,
             posted: freshness.PostedDay | None = None) -> dict:
    """The facts the app decides an ask from: the employer record's, and the posting's own that a job dial reads (the
    years asked, the degree, the distance from home ...; `gating.job_facts`). A fact not stated is left out, so the ask
    that reads it earns nothing (docs/PRD.md 46.7)."""
    today = owner_today()
    known = employers.facts(employer, today)
    if not any(a.fact in dials.JOB_FACTS for item in profile.items_for(role.target_role)
               for a in (item.members if isinstance(item, Group) else [item])):
        return known  # no ask reads the posting's own facts: nothing to work out
    return {**known, **gating.job_facts(gating.role_subject(role, employer, None, posted), profile, today)}


def classes_of(role: Opportunity) -> list[str] | None:
    """The analyst's classes (P, T or U) of the role's required items, in order; None while it has not classed any (it
    is not researched, or it was researched with no required item: a posting that was not read has none to class), when
    the asks decided from them wait. An empty list is not "no class": it would decide every requirements ask as if the
    role had been classed (nothing Unproven, not every item Proven)."""
    classes = [str(r.get("class") or "") for r in (role.analysis or {}).get("required") or [] if isinstance(r, dict)]
    # An N line is in no count (docs/PRD.md 26.12).
    return [c for c in classes if c != requirement_lines.NOT_MEASURABLE] or None


def preferred_classes_of(role: Opportunity) -> list[str] | None:
    """The analyst's classes (P, T or U) of the role's preferred items, in order, from the `preferred` list the research
    keeps beside `required` (the same `{item, class, ...}` shape); None while it has not classed any (an analysis kept
    before the analyst classed them, or a posting with no preferred section, holds none: the graded requirements count
    it as empty). The graded requirements read them (`scoring.compute`, "grade the requirements for me")."""
    classes = [str(r.get("class") or "") for r in (role.analysis or {}).get("preferred") or [] if isinstance(r, dict)]
    return [c for c in classes if c != requirement_lines.NOT_MEASURABLE] or None


def responsibility_classes_of(role: Opportunity) -> list[str] | None:
    """The analyst's classes (P, T or U) of the posting's responsibilities, in order, from the `responsibilities` list
    the research keeps beside `required`; None while it holds none (an analysis kept before duties were classed). A
    duty that already stands in as a required item (`from_duties`, a posting with no qualifications) is counted there
    once, not again here. The graded requirements count them as more required items (`scoring.compute`)."""
    classes = [r["class"] for r in comp.responsibilities_of(role.analysis)]
    return classes or None


def _scoreable(breakdown, fit: int | None, state: str | None) -> bool:
    """`scoreable` on a role's stored parts."""
    if scoring.is_asks(breakdown):
        return fit is not None or state == RESEARCHED
    return fit is not None  # scored the earlier way, or from the tracker: a fit on another scale


def scoreable(role: Opportunity) -> bool:
    """Whether the role is one a score is worked out for: it was scored on the asks, or a reader gave its answers, and
    it has a score already or has been researched (a role with answers and neither waits for its research); or it was
    scored the earlier way, or came from the tracker, and so holds a fit on another scale. `work_out` scores the last
    on the asks when the answers it holds, or the facts the app decides from, allow it, and leaves it as it was when
    they do not (the owner has an ask only a reader can answer, and no reader has). Settling a role calls it
    (`settle.roles`), and so does the whole-log pass (`rescore`)."""
    return _scoreable(role.fit_breakdown, role.fit, role.analysis_state)


def work_out(role: Opportunity, profile: Profile, *, employer: Employer | None, version: int | None,
             before: Profile | None = None, seen: datetime | None = None,
             posted: freshness.PostedDay | None = None) -> bool:
    """Work the role's score out from the answers kept on it, the employer's facts, the classes of its requirements, the
    posting's age (`seen`: when the app first saw it; `posted`: the day the role was posted, as the gates count it) and
    the worked ask, and keep both on the role: its answers as they were given, and the score that adds them up with the
    version it was worked out under. `before` is the profile whose words the answers that have none are taken to be for
    (docs/PRD.md 39.26); without it, the present ones.
    Returns False, leaving the role as it was, when there is nothing to score from: no one has answered the asks for a
    role that has asks a reader answers (a role scored the old way keeps its own score until they are, and one scored on
    nothing stays without), or no ask of the owner's can earn anything for a role that holds a fit on another scale.
    Two passes call it with the inputs loaded: `settle.roles` (a role's research or a fact it reads changed) and
    `rescore` (a save of Prioritization); `employer_of` says which employer record they pass."""
    answers = scoring.stamp(scoring.answers_in(role.fit_breakdown), before or profile)
    if role.fit is not None and not scoring.is_asks(role.fit_breakdown) and not scoring.possible(
            profile, role.target_role):
        return False  # a fit on another scale is not replaced by a score of no asks: it would be a share of nothing
    scored = scoring.compute(
        {"answers": answers}, profile, known_of(role, profile, employer, posted), classes=classes_of(role),
        facts=facts_of(role, profile, seen, posted), target_role=role.target_role, version=version,
        preferred=preferred_classes_of(role), responsibilities=responsibility_classes_of(role))
    if scored is None:  # cannot happen: an `answers` list is always the asks kind
        return False
    role.fit_breakdown, role.fit = {"answers": answers, **scored}, scored["total"]
    return True


# --- every score again ---------------------------------------------------------------------------------------------


@dataclass
class Rescored:
    """What working every score out again did."""

    scored: int = 0
    changed: int = 0
    failed: int = 0


#: How many roles one transaction of `rescore` reads, works out and writes.
PAGE = 250


async def rescore(session: AsyncSession, owner: str, profile: Profile, *, before: Profile | None = None,
                  version: int | None = None) -> Rescored:
    """Work every score out again from the answers and facts already stored, with no Claude (docs/PRD.md 39.21). Every
    role whose answers are kept and that holds a score or has been researched; a role with answers and no research keeps
    none. One scored the earlier way (a fit on the old scale, or the tracker's) is scored on the asks too when what it
    holds allows it (`work_out`), and is left as it was when it does not. The answers given before their words were kept
    are taken to be for the words `before` had, so a reworded ask does not silently reuse them. `version` is the
    version of Prioritization in force; each score keeps it. A role that cannot be scored is left as it was and counted
    in `failed`, and the others go on.

    **A page at a time, no turn for the work.** The roles are read and worked out a page (`PAGE`) at a time, and where
    `session` holds nothing yet (`off_turn.reading`) the read and the working out are made in a reader's session and the
    writer's turn is taken only to lock the page and write the scores that changed, committed before the next page
    (thousands of scores in one turn would hold it for seconds). A score is written only if the role still
    holds what it was worked out from; one that changed meanwhile is left for the next time it is found stale. In a
    caller's own transaction (a save of Prioritization, a savepoint) the pages share it, as the whole pass did."""
    async with off_turn.reading(session) as reader:
        version = version if version is not None else await profile_store.newest_version(reader, owner, "scoring")
        found = (await reader.execute(select(Opportunity.id, Opportunity.fit_breakdown, Opportunity.fit)
                                      .where(Opportunity.owner == owner))).all()
    ids = sorted(row.id for row in found if scoring.is_asks(row.fit_breakdown) or row.fit is not None)
    was_span = scoring.bounds(before or profile)
    out = Rescored()
    for start in range(0, len(ids), PAGE):
        page = ids[start:start + PAGE]
        async with off_turn.reading(session) as reader:
            apart = reader is not session
            roles = list((await reader.scalars(select(Opportunity).where(db.among(Opportunity.id, page))
                                               .options(defer(Opportunity.description)))).all())
            employer_ids = {r.employer_id for r in roles if r.employer_id}
            known = {e.id: e for e in (await reader.scalars(
                select(Employer).where(Employer.id.in_(employer_ids)))).all()}
            seen = await freshness.first_seen_by_role(reader, owner, page)
            days = await freshness.posted_by_role(reader, owner, roles)
        stood = {r.id: copy.deepcopy((r.fit_breakdown, r.fit)) for r in roles} if apart else {}
        counted = _work_page(roles, profile, before, version, was_span, known, seen, days, out)
        if apart:  # the roles are detached: the changed scores are written to the rows as the lock reads them
            changed = [r for r in roles if (r.fit_breakdown, r.fit) != stood[r.id]]
            held = await as_read.lock(session, Opportunity, [r.id for r in changed],
                                      defer=(Opportunity.description, Opportunity.analysis))
            for role in changed:
                row = held.rows.get(role.id)
                if row is not None and (row.fit_breakdown, row.fit) == stood[role.id]:
                    row.fit_breakdown, row.fit = role.fit_breakdown, role.fit
                else:  # changed meanwhile: nothing written, so it is not counted as scored or changed
                    counted.pop(role.id, None)
        for moved in counted.values():
            out.scored += 1
            out.changed += moved
        await session.flush()
        if apart:
            await db.release_if_free(session)
    return out


def _work_page(roles: list[Opportunity], profile: Profile, before: Profile | None, version: int | None,
               was_span: tuple[int, int] | None, known: Mapping[uuid.UUID, Employer], seen: Mapping, days: Mapping,
               out: Rescored) -> dict[uuid.UUID, bool]:
    """Work out the score of each of these roles again, in memory. A role that cannot be worked out is counted into
    `out.failed`; the rest are returned as role id -> whether its percent moved, for `rescore` to count once it knows
    which of them it wrote."""
    counted: dict[uuid.UUID, bool] = {}
    for role in roles:
        if not scoreable(role):
            kept = scoring.stamp(scoring.answers_in(role.fit_breakdown), before or profile)
            if role.fit_breakdown != {"answers": kept}:
                role.fit_breakdown = {"answers": kept}
            continue
        was = scoring.percent_of(role, was_span)
        try:
            worked = work_out(role, profile, employer=employer_of(role, known), version=version, before=before,
                              seen=seen.get(role.id), posted=days.get(role.id))
        except Exception:  # one role's odd data never stops the others
            log.exception("couldn't work out the score of role %s again", role.id)
            out.failed += 1
            continue
        if not worked:
            continue
        counted[role.id] = scoring.percent_of(role, None) != was
    return counted


# --- the save ------------------------------------------------------------------------------------------------------


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def say(done: Rescored) -> str:
    """What a save did, in one line: how many scores were worked out again and how many changed. Empty when it did
    nothing to say."""
    parts = []
    if done.scored:
        parts.append(f"Worked out {_n(done.scored, 'score')} again: {done.changed} changed." if done.changed
                     else f"Worked out {_n(done.scored, 'score')} again: none changed.")
    if done.failed:
        parts.append(f"{_n(done.failed, 'score')} could not be worked out again and stays as it was.")
    return " ".join(parts)


async def after_save(session: AsyncSession, owner: str, *, before: Profile, after: Profile, version: int) -> str:
    """What a save of Prioritization sets going, in code, at once (docs/PRD.md 39.21, 39.23), and the one line that says
    what it did: every score is worked out again."""
    done = await rescore(session, owner, after, before=before, version=version)
    return say(done)


# --- scores left under an older Prioritization -----------------------------------------------------------------------


async def stale_scores(session: AsyncSession, owner: str, profile: Profile, version: int) -> int:
    """How many scores were worked out under an older Prioritization than the one in force, or under none (a score kept
    before the version was, as every one a release finds is), or by another arithmetic than this release's
    (`scoring.MATH`: a score keeps the number of the arithmetic that worked it out, and one with none was worked out by
    the one before). A save of Prioritization works every score out again, but one that changes nothing makes no new
    version, so a score kept under an older one would stay. Only the scores `rescore` would write the version and the
    arithmetic to are counted (a role with answers to score from, or none to wait for), so the count falls to nothing
    once it has run."""
    rows = (await session.execute(select(
        Opportunity.fit_breakdown, Opportunity.fit, Opportunity.analysis_state, Opportunity.target_role
    ).where(
        Opportunity.owner == owner,
        # A superset of what the loop counts, which stays the judge: the database drops the roles `_scoreable` cannot
        # take (it needs a fit or a researched role) and those already on this version and arithmetic, so their
        # breakdowns are never decoded on the loop. Integers, never text (a SQLite integer never equals text).
        Opportunity.fit.is_not(None) | (Opportunity.analysis_state == RESEARCHED),
        func.json_extract(Opportunity.fit_breakdown, literal_column("'$.version'")).is_distinct_from(version)
        | func.json_extract(Opportunity.fit_breakdown, literal_column("'$.math'")).is_distinct_from(scoring.MATH),
    ))).all()
    stale = 0
    for row in rows:
        if not _scoreable(row.fit_breakdown, row.fit, row.analysis_state):  # `scoreable`'s own test
            continue
        kept = row.fit_breakdown or {}
        if kept.get("version") == version and kept.get("math") == scoring.MATH:
            continue
        if not scoring.is_asks(row.fit_breakdown) and not scoring.possible(profile, row.target_role):
            continue  # nor does it put a score of no asks in place of a fit on another scale
        stale += 1
    return stale


async def catch_up(session: AsyncSession, owner: str) -> str | None:
    """Work every score out again when some were worked out under an older Prioritization than the one in force, or
    under none (`stale_scores`): what a release leaves. Nothing to do (None) when every score is current. The same
    work as a save of Prioritization does (`after_save`: the scores again), and the one line it says. Run at every
    start of the app and at the start of the night's read (`keep_current.py`)."""
    async with off_turn.reading(session) as reader:  # the check is a read: no turn for it
        profile = await profile_store.current(reader, owner)
        version = await profile_store.newest_version(reader, owner, "scoring")
        if not await stale_scores(reader, owner, profile, version):
            return None
    return await after_save(session, owner, before=profile, after=profile, version=version)
