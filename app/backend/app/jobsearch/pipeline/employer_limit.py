"""At most two open applications per employer (docs/PRD.md 34.2, the owner, 2026-10-26).

An employer's roles at Applied or Active Discussion are its open applications (`classifier.REACHED`). When it has two,
every other role there that is in the deck (First Impression, Second Impression, Ready to Launch) is held out of it:
Gated out, with the reason "Two open applications at this employer: <titles>", kept and shown, never deleted. When an
application ends, or the owner turns the switch off, a held role returns to First Impression and is ranked again, its
history saying why. A role the owner moved by hand, and is still where they put it, is never held (`placement`,
`Purpose.LIMIT`).

The switch is `GateSet.employer_limit` (Hard pass, Company; on by default). Deterministic code, no Claude. `settle` is
run when a role comes into or leaves an application (`board.move`), when Hard pass is saved (`regate.run`) and in the
pipeline's turn before the deck is filled (`pipeline._stages`). The re-gate leaves these stops alone (`is_hold`).

Services flush; the route that owns the request commits."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, load_only

from app.jobsearch import as_read, off_turn, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import classifier
from app.jobsearch.gating import check as gating
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import lifecycle, placement
from app.jobsearch.profile import store as profile_store
from app.jobsearch.states import StopGate, StopStep
from app.trail import service as trail

#: The line of Hard pass that holds a role (`Opportunity.screened_out_gate_id`).
GATE_ID = "employer_limit"
#: How many open applications an employer may have.
LIMIT = 2
#: The roles in the deck: the ones a hold takes out of it.
DECK = (stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)
#: The only columns `settle`'s reader reads of a role (`_key`, `is_hold`, `reason_for`, the hold's words and
#: `placement.load` for `Purpose.LIMIT`); its roles are detached when it closes, and the writes go to the locked rows.
READ_COLUMNS = (Opportunity.id, Opportunity.status, Opportunity.employer_id, Opportunity.company, Opportunity.title,
                Opportunity.screened_out_by, Opportunity.screened_out_reason)
HELD_EVENT, RETURNED_EVENT = "limit_held", "limit_released"


@dataclass
class Settled:
    """What a settle did: roles held, and roles returned to First Impression."""

    held: int = 0
    returned: int = 0


def is_hold(role: Opportunity) -> bool:
    """Whether the role is Gated out by this limit (no re-run of the other gates moves it)."""
    return role.status == stages.GATED_OUT and role.screened_out_by == StopGate.LIMIT.value


def reason_for(titles: list[str]) -> str:
    """The sentence a held role carries: the open applications that fill the employer's places."""
    return f"Two open applications at this employer: {', '.join(titles[:-1])} and {titles[-1]}"


def _key(role: Opportunity) -> str:
    return str(role.employer_id) if role.employer_id else "name:" + employers.key_of(role.company)


def _concerns(role: Opportunity) -> bool:
    """Whether the limit reads the role: an open application, a role in the deck, or one the limit holds."""
    return role.status in (*classifier.REACHED, *DECK) or is_hold(role)


async def settle(session: AsyncSession, owner: str, *, planned: Sequence[Opportunity] | None = None) -> Settled:
    """Hold the deck roles at every employer that has two open applications, and return the held roles at any that no
    longer has, or when the switch is off (or the Company part is). A role the owner placed by hand is never held.

    With `planned` (a dry run of the re-gate: the roles it changed, in memory only, none written) nothing is locked or
    written and no history line is made: the roles are read as the run would have left them, and what would be held
    and returned is counted."""
    async with off_turn.reading(session, apart=planned is None) as reader:
        profile = await profile_store.current(reader, owner)
        on = profile.gates.limits_employers()
        query = select(Opportunity).where(
            Opportunity.owner == owner,
            or_(Opportunity.status.in_((*classifier.REACHED, *DECK)),
                and_(Opportunity.status == stages.GATED_OUT, Opportunity.screened_out_by == StopGate.LIMIT.value)))
        deferred = (defer(Opportunity.description), defer(Opportunity.analysis))
        if planned is None:
            if reader is not session:  # a reader's roles are detached: only the columns the limit reads of them
                query = query.options(load_only(*READ_COLUMNS))
            else:  # the caller's own keep every column it loaded
                query = query.options(*deferred, *lifecycle.UNREAD)
            rows = await off_turn.scalars_in_slices(reader, query.order_by(Opportunity.created_at, Opportunity.id)
                                                    .execution_options(populate_existing=True))
        else:  # the session holds the planned roles as the run left them (read again they would be as stored)
            read = {r.id: r for r in (await reader.scalars(query.options(*deferred))).all()}
            read.update({r.id: r for r in planned})
            rows = sorted((r for r in read.values() if _concerns(r)), key=lambda r: (r.created_at, r.id))
        out = Settled()
        by_employer: dict[str, list[Opportunity]] = {}
        for role in rows:
            by_employer.setdefault(_key(role), []).append(role)
        if not any(is_hold(r) or r.status in DECK for r in rows):
            return out
        # Only a deck role at an employer with its places full is asked of the owner's moves (`placement`): the deck is
        # thousands of roles, and the answer is read for the few that could be held.
        fulls = {key: on and sum(r.status in classifier.REACHED for r in group) >= LIMIT
                 for key, group in by_employer.items()}
        places = await placement.load(
            reader, owner, [r for key, group in by_employer.items() if fulls[key] for r in group if r.status in DECK],
            placement.Purpose.LIMIT)
        plan: list[tuple[Opportunity, str, str, str]] = []  # (role, what, reason, why)
        for key, group in by_employer.items():
            full = fulls[key]
            reason = reason_for(sorted(r.title for r in group if r.status in classifier.REACHED)) if full else ""
            for role in group:
                if full and role.status in DECK and not places[role.id].held:
                    plan.append((role, "hold", reason, ""))
                elif is_hold(role) and full:
                    if role.screened_out_reason != reason:  # a hold in force, with the words it carries, is left alone
                        plan.append((role, "keep", reason, ""))
                elif is_hold(role):
                    plan.append((role, "return", "", "the limit was turned off" if not on else
                                 "there is room at this employer again"))
        if planned is not None:
            return Settled(held=sum(what == "hold" for _, what, _, _ in plan),
                           returned=sum(what == "return" for _, what, _, _ in plan))
        if not plan:
            return out  # nothing to change: no row is locked and the turn holds no transaction id
        hard_pass = await profile_store.newest_version(reader, owner, "gates")
        criteria = await profile_store.newest_version(reader, owner, "criteria")
    # Only the roles that change are locked, in id order, and one another transaction holds is left for the next turn:
    # locking every deck row in this statement's own order deadlocked with writers that took theirs in another.
    held = await as_read.lock(session, Opportunity, [role for role, *_ in plan], stands=(Opportunity.status,),
                              defer=(Opportunity.description, Opportunity.analysis))
    for planned_role, what, reason, why in plan:
        role = held.rows.get(planned_role.id)
        if role is None:
            continue
        if what == "hold":
            was = stages.LABELS[role.status]
            role.status = stages.GATED_OUT
            role.screened_out_reason, role.screened_out_by = reason, StopGate.LIMIT.value
            role.screened_out_step, role.screened_out_gate_id = StopStep.GATE.value, GATE_ID
            role.screened_out_hard_pass, role.screened_out_criteria = hard_pass, criteria
            out.held += 1
            await trail.record(
                session, owner, event=HELD_EVENT, actor="app", who=trail.APP_WHO, link=("opportunity", role.id),
                sentence=f"Gated out: {reason}.", detail=f"It was in {was}. It is kept, and returns when an "
                                                         "application at this employer ends.")
        elif what == "keep":
            role.screened_out_reason = reason  # the applications changed; still held
        else:
            for column, value in gating.ROLE_CLEARED.items():
                setattr(role, column, value)
            role.status = stages.FIRST_IMPRESSION
            out.returned += 1
            await trail.record(
                session, owner, event=RETURNED_EVENT, actor="app", who=trail.APP_WHO,
                link=("opportunity", role.id), sentence=f"Back in First Impression: {why}.",
                detail="It is ranked again with the rest of the deck.")
    await session.flush()
    return out
