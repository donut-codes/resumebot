"""The board's vocabulary (docs/PRD.md path 7) and moving a role from one column to another.

Columns are the statuses (`stages.LANES`): Followup needed, Landing Zone,
Cleansed Data, First Impression, Second Impression, Ready to Launch, Applied and Active Discussion, and Closed (a gate
stopped it, or it ended), which is read by its reason (`closed_reason_key`). What each column shows, its roles in the
owner's order a page at a time, is `role_row` (`role_row.columns`), as is every role's row. This module names the
lanes and the Closed reasons, says how many roles Ready to Launch holds (`ready_size`) and which of them Go may be
offered (`ready_line`), and moves a role: a move changes the role's status and is written to the trail, so a role's
history (path 11) has it. Services flush; the route commits."""

import uuid

from sqlalchemy import ColumnElement, and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch import stages
from app.jobsearch.actors import actor_name
from app.jobsearch.gating import check as gating
from app.jobsearch.models import Opportunity, StageMove
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import ended, favorites, placement
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.timeutil import utc_now
from app.trail import service as trail

#: The Added links column: the jobs the owner added by their link that nothing has named yet
#: (`role_from_posting.waiting_added_where`), not roles yet; every other posting that passes the gates becomes a role.
#: Its key is not a lane (`stages.LANES`).
ADDED_LINKS = "added_links"
ADDED_LINKS_LABEL = "Added links"
#: The owner's starred roles as one list, whatever their stage: not a lane either.
FAVORITES = "favorites"
FAVORITES_LABEL = "Favorites"
#: How many roles Ready to Launch holds, and so the day's batch, when the owner sets none
#: (`Settings.ready_to_launch_size`).
APPLY_BATCH_DEFAULT = pipeline.DAILY_DEFAULT


def ready_size(settings) -> int:
    """How many roles Ready to Launch holds, the day's batch: the owner's Ready to Launch size, else the default."""
    size = settings.ready_to_launch_size
    return size if size is not None and size > 0 else APPLY_BATCH_DEFAULT


#: The Closed lane's key, and its reasons in the three groups the dashboard shows: a gate stopped it, how it ended, the
#: owner's own call. A role's reason is `closed_reason_key`.
CLOSED = "closed"
GATED_OUT_REASON = "gated_out"
EXPIRED, REJECTED, RECEIVED_OFFER = "expired", "rejected", "received_offer"
NOT_MY_FIELD, WRONG_LEVEL, EMPLOYER, PAY, LOCATION_OR_MODE, OTHER = (
    "not_my_field", "wrong_level", "employer", "pay", "location_or_mode", "other")
CLOSED_LABELS = {
    GATED_OUT_REASON: "Gated out", EXPIRED: "Expired", REJECTED: "Rejected", RECEIVED_OFFER: "Received offer",
    NOT_MY_FIELD: "Not my field", WRONG_LEVEL: "Wrong level", EMPLOYER: "Not this employer or industry", PAY: "Pay",
    LOCATION_OR_MODE: "Location or mode", OTHER: "Other",
}
_REASON_KEY = {
    stages.THEY_SAID_NO: REJECTED, stages.POSTING_GONE: EXPIRED, stages.RECEIVED_OFFER: RECEIVED_OFFER,
    stages.NOT_MY_FIELD: NOT_MY_FIELD, stages.WRONG_LEVEL: WRONG_LEVEL, stages.THE_EMPLOYER: EMPLOYER,
    stages.PAY_TOO_LOW: PAY, stages.LOCATION_OR_MODE: LOCATION_OR_MODE,
}


def closed_reason_key(status: str, ended_reason: str | None) -> str:
    """The Closed reason a closed role is read under: Gated out for a role a gate stopped; for an ended role the
    reason it
    was ended for (the employer's no is Rejected, a posting that came down is Expired), else Other."""
    if status == stages.GATED_OUT:
        return GATED_OUT_REASON
    return _REASON_KEY.get(ended_reason or "", OTHER)


def closed_reason_where(reason: str) -> ColumnElement[bool]:
    """The roles `closed_reason_key` puts under `reason`, as the condition of a statement (a Closed lane read by its
    reason takes only those, in the database): a role a gate stopped is Gated out, an ended role is under the reason its
    `ended_reason` maps to, and the rest of the ended are Other. No role is under a word that is no reason."""
    if reason == GATED_OUT_REASON:
        return Opportunity.status == stages.GATED_OUT
    ended = Opportunity.status != stages.GATED_OUT
    if reason == OTHER:
        return and_(ended, or_(Opportunity.ended_reason.is_(None), Opportunity.ended_reason.not_in(list(_REASON_KEY))))
    under = [why for why, key in _REASON_KEY.items() if key == reason]
    return and_(ended, Opportunity.ended_reason.in_(under)) if under else false()


LABELS = stages.LANE_LABELS
STAGE_LABELS = {**LABELS, ADDED_LINKS: "Added links", **stages.GROUP_LABELS}
#: Where the owner moves a role by hand (docs/PRD.md 23.8), in the order of the process: back to First Impression or on
#: to Second Impression, Ready to Launch, they applied, they're in talks, the employer turned them down, it went quiet,
#: or they withdrew. A move works from any status to any of these. Gated out is the gates' to decide; email and time
#: passing move roles to Rejected and Gone Quiet on their own as well.
MOVES_TO = (stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH, stages.APPLIED,
            stages.ACTIVE_DISCUSSION, stages.REJECTED, stages.GONE_QUIET, stages.WITHDREW)


class BoardError(DomainError):
    """A move that can't be made, in words."""

    status = 409


class NoSuchColumn(BoardError):
    status = 404


class NoSuchRole(BoardError):
    status = 404


#: The history line of a move to Ready to Launch that was not made because the role has no Second Impression that
#: graded its posting (the owner, 2026-11-03: nothing unread sits in Ready to Launch): the role runs its impressions
#: first and the move is made once it is read (`favorites.place_asked`).
READY_ASKED_EVENT = "ready_asked"


def unconfirmed(rows: list[Opportunity]) -> set[uuid.UUID]:
    """The roles of `rows` (Application is ready) that must not be offered yet (the owner, 2026-10-17): those waiting
    on their research again (their text changed, so their gates are not confirmed). They stay in Application is ready,
    marked `role_row.RECHECKING_WORDS`; the batch, Go and the 6:30 mission pass them over until the research is back."""
    return {r.id for r in rows if r.analysis_state == analysis.WAITING}


async def ready_line(session: AsyncSession, owner: str) -> tuple[list[Opportunity], int]:
    """The roles of Ready to Launch in the board's order (the day's list), and how many roles the day holds
    (`Settings.ready_to_launch_size`, else `APPLY_BATCH_DEFAULT`). What Go walks to find the roles that pass its checks
    (`go_checks.py`); a role still being re-checked (`unconfirmed`) is not in it, and neither is one with no Second
    Impression that graded its posting (the owner, 2026-11-03): Launch opens no role that is not read, and one it meets
    leaves Ready to Launch at the pipeline's next turn (`lifecycle.settle`)."""
    rows = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.status == stages.READY_TO_LAUNCH))).all()
    held = unconfirmed(list(rows))
    graded = await analysis.reads_graded(session, [r.id for r in rows])
    rows = [r for r in rows if r.id not in held and graded.get(r.id)]
    ordered = await pipeline.ordered(session, owner, list(rows))
    return ordered, ready_size((await profile_store.current(session, owner)).settings)


async def move(session: AsyncSession, owner: str, role_id: uuid.UUID, to: str, note: str | None = None,
               ended_reason: str | None = None, allowed: tuple[str, ...] = MOVES_TO, *,
               defer_reads: bool = False) -> dict:
    """Move a role to another column, from any stage to any of `MOVES_TO` (docs/PRD.md 23.8). Bringing a screened-out
    role back keeps the gate's reason in its notes. A move to an Ended stage records why the role ended (docs/PRD.md
    31.7): the reason given, else what the app can tell (`ended.reason_for`); a move to any other stage ends no reason,
    and leaving an Ended stage clears it. The move is the owner's hand on the role, which no re-run moves it from
    (`placement`, read from the history line written below: `placement.MOVED_EVENT`).

    What a move does to the role's work is nothing more than its status, which is all the analyst's and the writer's
    queues read. A role moved to Ready to Launch needs no packet: Go builds it (`apply_go`). Starting an agent (Claude
    work, held in peak hours) is never part of a move.

    **One exception** (the owner, 2026-11-03, docs/PRD.md 24.13): no role sits in Ready to Launch without a Second
    Impression that graded its posting, so a move there of a role that has none is not made. The role stays where it is
    (a role past the application goes to First Impression), `Opportunity.ready_asked_at` marks the ask, and the role
    runs its impressions first as a star does (`favorites.ask_reads`, then `favorites.advance` every turn); the move
    is made once it is read (`favorites.place_asked`). Any other move of the role withdraws the ask. Returns what was
    asked for the role, as `favorites.fast_track` does: `source`, `rank`, `research` (what `analysis.ask` said, or
    None), and `asked` (the move was held for the read); the route wakes what was asked. With `defer_reads` the
    move is held and marked (`asked` is True) and the reads themselves are left to the caller: the page's route asks
    the engine for them (the ask `role.read_first`, which runs `favorites.ask_reads`)."""
    if to not in allowed:
        raise BoardError(f"a role can be moved to {', '.join(allowed)}, not {to}")
    try:
        ended.check(ended_reason)
    except ended.ReasonError as exc:
        raise BoardError(str(exc)) from exc
    role = await session.get(Opportunity, role_id)
    if role is None or role.owner != owner:
        raise NoSuchRole("no such role")
    was = role.status
    done: dict = {"asked": False, "source": False, "rank": False, "research": None}
    if was == to:
        return done
    unread = to == stages.READY_TO_LAUNCH and not (await analysis.reads_graded(session, [role.id])).get(role.id)
    target = (was if was in stages.PRE_APPLY else stages.FIRST_IMPRESSION) if unread else to
    if was == stages.GATED_OUT and role.screened_out_reason:
        role.notes = "; ".join(x for x in (role.notes, f"gated out first: {role.screened_out_reason}") if x)
    role.status = target
    role.ready_asked_at = utc_now() if unread else None
    if was == stages.GATED_OUT:  # the old stop no longer holds for a role out of Gated out: its reason goes
        for column, value in gating.ROLE_CLEARED.items():
            setattr(role, column, value)
    role.ended_reason = await ended.reason_for(session, role, target, ended_reason)
    if note and note.strip():
        role.notes = "; ".join(x for x in (role.notes, note.strip()) if x)
    if unread and target == was:  # nothing moved: the line says what was asked, and it is not a hand move
        event = READY_ASKED_EVENT
        sentence = f"You asked for {role.title} at {role.company} in Ready to Launch: it is read first."
    else:
        event = placement.MOVED_EVENT
        sentence = f"You moved {role.title} at {role.company} from {LABELS.get(was, was)} to {LABELS[target]}."
        if unread:
            sentence = sentence[:-1] + ": you asked for Ready to Launch, and it is read first."
    if role.ended_reason in stages.END_REASON_PHRASES:
        sentence = sentence[:-1] + f" ({stages.END_REASON_PHRASES[role.ended_reason]})."
    await trail.record(
        session, owner, event=event, actor="person", who=await actor_name(session, owner),
        sentence=sentence, link=("opportunity", role.id), detail=(
            "It has no Second Impression that graded its posting, and no role without one sits in Ready to Launch. The "
            "move is made once it is read." if unread else note.strip() if note and note.strip() else None),
    )
    await session.flush()
    if unread:
        done["asked"] = True
        if not defer_reads:  # the page leaves the reads to the engine (the ask `role.read_first`)
            await session.refresh(role, ["analysis"])
            done.update(await favorites.ask_reads(session, owner, role))
    from app.jobsearch.fit import classifier  # they read the stages this module names
    from app.jobsearch.pipeline import employer_limit  # they read the stages this module names

    if was in classifier.REACHED or target in classifier.REACHED:
        await employer_limit.settle(session, owner)  # an application began or ended: the employer's limit follows
    if (was in stages.OPEN) != (target in stages.OPEN):  # into or out of play: its employer's watch follows (PRD 29.9)
        from app.sites import where  # the sites import much of the job search

        await where.settle_watch(session, owner)
    return done


async def move_back(session: AsyncSession, owner: str, role_id: uuid.UUID, *, defer_reads: bool = False) -> dict:
    """Move a closed role back (the Closed review): to the first status it held before it
    was closed, else to First Impression. Its Closed reason and a gate's stop go, so it flows on from there (a gate that
    still stops it closes it again at its next check). Refused (409) for a role that is not closed."""
    role = await session.get(Opportunity, role_id)
    if role is None or role.owner != owner:
        raise NoSuchRole("no such role")
    if role.status not in stages.CLOSED:
        raise BoardError(f"{role.title} at {role.company} is not closed")
    left = (await session.scalars(
        select(StageMove.from_status).where(StageMove.role_id == role.id, StageMove.to_status.in_(stages.CLOSED),
                                            StageMove.from_status.is_not(None))
        .order_by(StageMove.at.desc()).limit(1))).first()
    to = left if left in stages.PRE_READY or left == stages.READY_TO_LAUNCH else stages.FIRST_IMPRESSION
    return await move(session, owner, role_id, to, note="moved back from Closed", allowed=stages.STAGES,
                      defer_reads=defer_reads)
