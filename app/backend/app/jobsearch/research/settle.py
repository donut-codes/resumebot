"""A role is settled when its ask, then its score, then its gates have been worked out, in that one order, from what it
holds now (docs/PRD.md 25.6, 39.14, 39.20). Whoever changes research, or a fact the ask, the score or the gates read,
says so here (`roles`, with the cause) and does nothing else: the order, the inputs and what each step skips are this
module's. The start's step "old research asks" is `old_asks`.

The order is a data dependency, not a process order (the process files decide that): the pay gate reads the worked ask
(`gating.check`, `pay_check`), and the score reads it too (`prioritize.facts_of`). The score and the gates read the
role's facts and never each other. What each step reads, so that a new read is added where the step is:

- **The ask** reads the research (the pay band, the market value, the spread, the employer's other bands, and the
  classes of the required items and of the responsibilities: `comp.from_research`), the owner's comp settings and the
  role's target role. It is worked out again only when the research or the method (`comp.METHOD_VERSION`) changed,
  never for a settings change alone: the ask is a property of the research and the method.
- **The score** reads the answers kept on the role, the employer's record (the one the role's link names:
  `prioritize.employer_of`, the rule `rescore` follows too), the classes of the requirements, the worked ask and what
  the posting states: the way of working, the place as a distance, the posted date and the pay (`prioritize.known_of`,
  through `gating.job_facts`), with the hour the app first saw the posting. A role is scored when it is researched, or
  when it holds a score or answers to score from (`prioritize.scoreable`); one that is neither is left as it is.
- **The gates** read the place, the way of working, the pay and the date the role states, the employer's record (the
  one its link names, else the one its company's name does: `EmployerIndex.of`), the classes, the worked ask and the
  owner's answers to the sentence rules (`gating.results`). The gates keep their own exclusions (`regate.one_role`): a
  role past Ready to Launch, one the owner moved by hand and one the old tracker or the employer limit stopped keep
  their stage.

Nothing outside this module settles a role: the gates run from here alone (`regate.one_role`, whose only callers are
`roles` and `gating/age_out.py`, which asks it for the roles a new day has put past the owner's freshness pass: no fact
of such a role changed, so there is no ask or score to work out), and the score from here and from `rescore`, the
whole-log pass after a save of Prioritization, which commits as it goes (`prioritize.work_out`, the same function). A
caller that works a role's score out by itself has left a step of the chain out.

Research the ask cannot be worked out from, and score data that cannot be read, leave that role's ask or score as it
was and say so in the log; the other roles go on (as `rescore` does for a score). A database error and a failure of the
gates propagate: they are not one role's odd data.

The module writes through the session and flushes once at the end; it never commits and never ends the transaction, and
reads nothing but the database, so it can run inside the process engine's call. Only the gates write to a role's history
(`regate_stopped`, `regate_released`, `regate_restated`, each "<cause>, so the gates were checked again."). A caller
that writes its own line about the event writes it before the call, if it must come first."""

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import func, inspect, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import off_turn, stages
from app.jobsearch.fit import comp, freshness, scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.measures.pay_place import hourly_part_time
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import placement
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Profile
from app.jobsearch.research import prioritize

log = logging.getLogger(__name__)

#: The cause the history gives when a new method of working the ask out moved an ask (`old_asks`).
METHOD_CHANGED = "The method that works out the starting expectation changed"
#: How many old asks `old_asks` works out before the writer's turn is let go.
BATCH = 100
#: What a row loaded without (`defer`) or expired is given before it is settled.
_READ = ("analysis", "fit_breakdown", "gate_answers", "gate_facts")


@dataclass(frozen=True)
class Settled:
    """What settling some roles did. `roles` is how many it was given; `scores_moved` how many came out with a different
    score (the percent: `scoring.percent_of`); `gated` the line each role's gates wrote in its history (the role was
    stopped, returned or its stop restated), by the role's id."""

    roles: int
    scores_moved: int
    gated: Mapping[uuid.UUID, str]


async def roles(session: AsyncSession, rows: Iterable[Opportunity], *, cause: str,
                keep_gated_out: bool = False) -> Settled:
    """These roles' research, or a fact the ask, the score or the gates read, changed: bring them consistent. Each
    role's ask is worked out again if its research or the method changed, then its score, then its gates, from what it
    holds now. `cause` is a clause for the history ("The analyst researched it"); the new facts are already set on the
    rows (nothing needs flushing first). The rows are one owner's. With `keep_gated_out` a role in Gated out stays there
    whatever the gates say now (research taken on request, PRD 39.28; an employer split): its ask and score are worked
    out all the same. Safe to run again: nothing changes a second time, and no line is written. One role's odd data is
    left as it was and logged, and the rest are settled; a database error, and a failure of the gates, propagate."""
    rows = list(rows)
    if not rows:
        return Settled(0, 0, {})
    owner = rows[0].owner
    await _load(session, rows)
    rules = await gating.rules(session, owner)
    for role in rows:
        _ask(role, rules.profile)
    moved, gated = await _score_and_gate(session, owner, rows, rules, cause, keep_gated_out)
    await session.flush()
    return Settled(len(rows), moved, gated)


def _is_old(analysis: object) -> bool:
    """Whether research holds an ask an earlier method worked out, or none (`old_asks`)."""
    research = analysis if isinstance(analysis, dict) else {}
    return bool(research.get("required")) and (research.get("ask") or {}).get("method_version") != comp.METHOD_VERSION


async def old_asks(session: AsyncSession, owner: str) -> int:
    """The pass behind a change of `comp.METHOD_VERSION` (the start's step "old research asks", docs/PRD.md 26.5: a
    change of method shows in the numbers it moves). Every researched role whose ask an earlier method worked out, or
    that holds none, has it worked out again; only a role whose ask came out different has its score and gates worked
    out again, since only then can they differ. Returns how many roles it worked an ask for. Safe to run again: a start
    with nothing old reads a list and writes nothing.

    Which roles are old is a read of the researched roles' research in a reader's session (`off_turn.reading`), so a
    start with nothing to change takes no turn. The old ones are then settled `BATCH` at a time, each read again in the
    writer's turn and judged old again, the turn let go between batches (`db.release_if_free`)."""
    async with off_turn.reading(session) as reader:
        found = (await reader.execute(select(Opportunity.id, Opportunity.analysis).where(
            Opportunity.owner == owner, Opportunity.analysis_state == prioritize.RESEARCHED,
            # A superset of `_is_old`, which stays the judge: the database drops the roles with no required items and
            # those already on this method, so their research is never decoded on the loop. The version is an integer
            # (a SQLite integer never equals text).
            func.json_extract(Opportunity.analysis, literal_column("'$.required'")).is_not(None),
            func.json_extract(Opportunity.analysis, literal_column("'$.ask.method_version'")).is_distinct_from(
                comp.METHOD_VERSION)))).all()
    ids = [i for i, analysis in found if _is_old(analysis)]
    del found
    if not ids:
        return 0
    profile = await profile_store.current(session, owner)
    counted = 0
    for start in range(0, len(ids), BATCH):
        old = (await session.scalars(select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.analysis_state == prioritize.RESEARCHED,
            db.among(Opportunity.id, ids[start:start + BATCH])).options(defer(Opportunity.description)))).all()
        old = [r for r in old if _is_old(r.analysis)]
        counted += len(old)
        moved = [role for role in old if _ask(role, profile)]
        if moved:
            await _score_and_gate(session, owner, moved, await gating.rules(session, owner, profile), METHOD_CHANGED,
                                  False)
        await session.flush()
        if start + BATCH < len(ids):
            await db.release_if_free(session)
    return counted


async def _load(session: AsyncSession, rows: list[Opportunity]) -> None:
    """The columns the three steps read, for a row that was loaded without them (a pass that defers the research)."""
    for role in rows:
        unloaded = inspect(role).unloaded
        if missing := [name for name in _READ if name in unloaded]:
            await session.refresh(role, missing)


def _ask(role: Opportunity, profile: Profile) -> bool:
    """The step "ask". Works the starting-comp expectation out from the research the role holds (docs/PRD.md 26) and
    stores it on the analysis with its inputs, unless the stored one was worked out from the same inputs by this method.
    A role that was never researched is left as it is. No inputs (no required item classed), or inputs the method cannot
    work an ask out of (no band and no market value), leave it without one. Research that cannot be read leaves the
    stored ask and says so in the log: a bad row never turns into a failure of the pass that reached it. Returns whether
    the worked ask, the number the score and the gates read, came out different."""
    research = role.analysis
    if not isinstance(research, dict) or not research.get("required"):
        return False
    stored = research.get("ask") if isinstance(research.get("ask"), dict) else None
    was = stored.get("ask") if stored else None
    if hourly_part_time(role.title, role.comp):
        # A fractional or part-time role paid by the hour has no yearly ask (docs/PRD.md 30.4): one stored earlier goes.
        if stored is None:
            return False
        role.analysis = {k: v for k, v in research.items() if k != "ask"}
        return was is not None
    try:
        inputs = comp.from_research(research)
    except (KeyError, TypeError, ValueError, AttributeError):
        log.warning("the research of role %s cannot be read, so its ask stays as it was", role.id, exc_info=True)
        return False
    ask = None
    if inputs is not None:
        if (stored and stored.get("method_version") == comp.METHOD_VERSION
                and stored.get("inputs") == inputs.model_dump(mode="json", by_alias=True)):
            return False
        try:
            ask = comp.compute(inputs, profile, role.target_role)
        except (ValueError, ZeroDivisionError):
            ask = None  # not enough to work an ask out of: none is stored, as for a role that was never asked
    if ask is None and stored is None:
        return False
    role.analysis = {**{k: v for k, v in research.items() if k != "ask"}, **({"ask": ask} if ask is not None else {})}
    return (ask or {}).get("ask") != was


def _score(role: Opportunity, rules: gating.Rules, span, version: int | None, seen: Mapping,
           days: Mapping) -> bool:
    """The step "score". Works the score out from the answers kept on the role and what the ask, the employer and the
    posting give (`prioritize.work_out`), for a role that is researched (scored even with no answers and no fit) or that
    `prioritize.scoreable` says is scored. Score data that cannot be read leaves the score as it was and says so in the
    log. Returns whether its percent came out different."""
    if role.analysis_state != prioritize.RESEARCHED and not prioritize.scoreable(role):
        return False
    was = scoring.percent_of(role, span)
    try:
        worked = prioritize.work_out(role, rules.profile, employer=prioritize.employer_of(role, rules.index.by_id),
                                     version=version, seen=seen.get(role.id), posted=days.get(role.id))
    except Exception:  # one role's odd data never stops the others, as in `prioritize.rescore`
        log.exception("the score of role %s cannot be worked out, so it stays as it was", role.id)
        return False
    return worked and scoring.percent_of(role, span) != was


async def _score_and_gate(session: AsyncSession, owner: str, rows: list[Opportunity], rules: gating.Rules, cause: str,
                          keep_gated_out: bool) -> tuple[int, dict[uuid.UUID, str]]:
    """The steps "score" and "gates" for rows whose ask is worked out, in that order for each: how many scores came out
    different, and the line each gate wrote."""
    from app.jobsearch.gating import regate  # the engine's work: loaded when roles are settled, never at the top

    span = scoring.bounds(rules.profile)
    version = await profile_store.newest_version(session, owner, "scoring")
    seen = await freshness.first_seen_by_role(session, owner, [r.id for r in rows])
    days = await freshness.posted_by_role(session, owner, rows)
    stands = await placement.load(session, owner, rows, placement.Purpose.GATES)  # once for the batch
    moved, gated = 0, {}
    for role in rows:
        moved += _score(role, rules, span, version, seen, days)
        if keep_gated_out and role.status == stages.GATED_OUT:
            continue
        if said := await regate.one_role(session, owner, role, cause=cause, rules=rules,
                                         posted=days.get(role.id), standing=stands[role.id]):
            gated[role.id] = said
    return moved, gated
