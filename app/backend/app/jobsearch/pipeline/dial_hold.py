"""A Must that waits for its fact holds the role back (docs/PRD.md 46.7).

A dial's Must whose fact the record or the posting does not state yet is not a pass and not a fail: the role is kept
and shown with the fact it waits for, but it is not offered for Second Impression, Ready to Launch or the launch queue
until the fact arrives (`placement` names what it waits for, `Standing.off_deck`; `pipeline.rebalance` gives such a
role no place). A role already in Second Impression or Ready to Launch when a Must begins to wait drops back to First
Impression, its history saying why (`settle`, run when Hard pass is saved: `regate.run`). A role the owner placed by
hand, and one Go opened, stay where they are, as under the employer limit (`placement`, `Purpose.DROP`). Never a stop:
the role is not Gated out and never counted as screened out.

Deterministic code, no Claude. Services flush; the route that owns the request commits."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.jobsearch import as_read, off_turn, stages
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import placement
from app.trail import service as trail

DECK_PLACES = (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)
HELD_EVENT = "dial_waiting"


async def settle(session: AsyncSession, owner: str) -> int:
    """Drop the roles in Second Impression and Ready to Launch whose Must now waits for a fact, or whose work location
    gate cannot be decided, back to First Impression (not those the owner placed by hand, nor one Go opened): the bars
    are `placement`'s (`Purpose.DROP`). Returns how many dropped.

    **It never waits for a role.** The deck is read first, locking nothing, and only the roles to drop are locked
    (`as_read.lock`: in id order, skipping any another transaction holds, as the other passes lock theirs), then looked
    at again as the lock gives them: a role another transaction holds is left for the next turn, and one that no longer
    waits is left where it is. Locking every deck role in this statement's own order waited for rows other passes held
    while it held others, and deadlocked with them."""
    async with off_turn.reading(session) as reader:
        rows = (await reader.scalars(select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.status.in_(DECK_PLACES))
            .options(defer(Opportunity.description), defer(Opportunity.analysis))
            .execution_options(populate_existing=True))).all()
        places = await placement.load(reader, owner, rows, placement.Purpose.DROP)
        drop = [role for role in rows if not (places[role.id].held or places[role.id].off_deck is None)]
    if not drop:
        return 0
    held = await as_read.lock(session, Opportunity, drop,
                              where=(Opportunity.owner == owner, Opportunity.status.in_(DECK_PLACES)),
                              defer=(Opportunity.description, Opportunity.analysis))
    locked = list(held.rows.values())
    places = await placement.load(session, owner, locked, placement.Purpose.DROP)
    dropped = 0
    for role in locked:
        place = places[role.id]
        if place.held or place.off_deck is None:
            continue
        was, role.status = stages.LABELS[role.status], stages.FIRST_IMPRESSION
        dropped += 1
        await trail.record(
            session, owner, event=HELD_EVENT, actor="app", who=trail.APP_WHO, link=("opportunity", role.id),
            sentence=f"Back in First Impression: {place.off_deck}.",
            detail=f"It was in {was}. A Must in Hard pass needs this fact before the role goes on deck; it is "
                   "kept, and moves on when the fact arrives.")
    await session.flush()
    return dropped
