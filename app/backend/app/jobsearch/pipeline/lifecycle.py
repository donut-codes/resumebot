"""Where a role stands before it competes for a place: one function of its facts.

`stage_of` is the whole rule, and `settle` is the one way it is applied: every turn of the pipeline (`pipeline.run`,
which new data, a finished read, a rank and Launch all call) runs every role that is still being got ready through it.
Nothing else sets a role's stage on its way from Followup needed to First Impression, and the stage a role is in is
always the answer to its facts, so no role can sit in a stage that has no next step:

- **Followup needed**: there is no usable text of the posting (none, or a copy under `cleansed.MIN_TEXT` characters, a
  title and a few header lines) or a read said there is none (`analysis.NO_TEXT`). Its exit: a read brings the text.
- **Landing Zone**: a raw copy is held and has no cleansed copy yet. `settle` makes the cleansed copy first (from the
  raw bytes, else from the stored text as a `no_raw` copy), so a role is here only for the moment between the two.
- **Cleansed Data**: a whole cleansed copy and no rank. Its exit: the Haiku rank, and the hard gates (`pipeline.gate`).
- **First Impression**: a rank, or a Second Impression's read, and not a place at the top (`pipeline.rebalance` decides
  those: the best N read roles are Ready to Launch, the next N on deck).
- **Second Impression and Ready to Launch** stay where `rebalance` put them while the role is read; a role there with no
  read is back in First Impression, unless something holds it where it is (`Facts.held`, from `placement`: a read of it
  is under way, it is the owner's own and the import's catch-up reads it, Launch opened it, it is a read favorite, or
  the owner moved it by hand and it is still where they put it). Ready to Launch needs more (the owner, 2026-11-03,
  docs/PRD.md 24.13): a read that graded something of the posting (`Facts.graded`). A role there with no read, or one
  whose read graded nothing, leaves for the stage its facts give, whatever holds it (but a read under way or the
  owner's own data, which stand until their read is done), and its history says why.

Services flush; the caller commits, except `settle`, which ends the caller's transaction between its read and its
moves (`db.release_if_free`: nothing where the caller is inside a savepoint or a process step)."""

import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, load_only

from app import db
from app.jobsearch import as_read, off_turn, stages
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import placement
from app.jobsearch.postings import cleansed, raw_copies
from app.jobsearch.research import analysis
from app.trail import service as trail

#: The statuses this function places a role in, and the ones it looks at: the getting-ready stages and the two places
#: `rebalance` hands out.
PLACED = (stages.FOLLOWUP_NEEDED, stages.LANDING_ZONE, stages.CLEANSED_DATA, stages.FIRST_IMPRESSION)
LOOKED_AT = (*PLACED, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)


@dataclass(frozen=True)
class Facts:
    """What decides a role's stage."""

    status: str
    #: The size of the current cleansed copy's text; None with no copy. `stage_of` asks only whether it is under
    #: `cleansed.MIN_TEXT`, so `settle` gives `0` or `MIN_TEXT` (`cleansed.short_ids`) and never counts characters.
    copy: int | None = None
    #: A raw copy is held (and so a cleansed one can be made from it).
    raw: bool = False
    #: A read said the posting's text could not be had (`analysis.NO_TEXT`).
    no_text: bool = False
    ranked: bool = False
    #: It has a Second Impression (`analysis.reads`).
    read: bool = False
    #: And that read graded something of the posting (`analysis.graded_read`): a place in Ready to Launch stands on it.
    graded: bool = True
    #: Something holds it where it is: the owner moved it by hand and it is still where they put it, Launch opened it,
    #: the owner starred it and it is read, their own data placed it or a read of it is under way (`placement`, for
    #: `Purpose.STAGE`). Never at Followup needed or Landing Zone, which code moves a role through at once.
    held: bool = False


def stage_of(f: Facts) -> str:
    """The stage a role is in, from its facts. A status this function does not place is returned as it is."""
    if f.status not in LOOKED_AT:
        return f.status
    if f.held:  # the owner's move, Launch's opening, a star, the owner's own data, a read under way
        return f.status
    if f.status in (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH) and f.read and (
            f.graded or f.status == stages.SECOND_IMPRESSION):
        return f.status
    if f.read:
        return stages.FIRST_IMPRESSION
    if f.ranked:
        return stages.FIRST_IMPRESSION
    if f.no_text or (f.copy is None and not f.raw):
        return stages.FOLLOWUP_NEEDED
    if f.copy is None:
        return stages.LANDING_ZONE
    return stages.CLEANSED_DATA if f.copy >= cleansed.MIN_TEXT else stages.FOLLOWUP_NEEDED


#: How many roles one transaction makes copies for: a pass that must make thousands commits after each batch, so no
#: transaction holds more than a batch's rows (each batch is whole on its own, so a pass cut short is finished by the
#: next).
WRITE_BATCH = 40
#: How many stage moves one settle turn writes: each slice is locked, written and committed on its own, so no turn
#: holds the writer for a pass's thousands (a role moved is whole on its own; a pass cut short is finished by the next).
MOVE_SLICE = 15
#: The columns a settle's read of the roles never reads (`placement.load` reads `gate_facts`, not these); a deferred
#: column is never read from a role a reader loaded, which is detached when it closes.
UNREAD = (defer(Opportunity.fit_breakdown), defer(Opportunity.gate_answers), defer(Opportunity.analysis_made_under))
#: The only columns a settle's reader reads of a role: `placement.load` for `Purpose.STAGE` (`placement.HONOURED`: the
#: star, opened, placed, own and reading holds), `stage_of`'s facts and `as_read.lock`'s `STANDS`. A role a reader
#: loaded is detached when it closes, so nothing else may be read from it; when the caller's own session reads, the
#: roles keep every column but the deferred ones (`UNREAD`).
SETTLE_COLUMNS = (Opportunity.id, Opportunity.status, Opportunity.analysis_state, Opportunity.analysis_claimed_at,
                  Opportunity.favorited_at, Opportunity.go_opened_at, Opportunity.go_skip, Opportunity.origin)
#: What a change decided from a read of a role must find unchanged in it (`as_read.lock`'s `stands`): its place, and
#: whether a read of it is under way or taken by a session (`analysis_claimed_at`, which `placement` and the queue
#: ask). A role another transaction holds, or one of these has changed in since (the owner moved it, a session took its
#: read), is left for the next turn, so a decision made with no transaction open never writes over what happened
#: meanwhile.
STANDS = (Opportunity.status, Opportunity.analysis_state, Opportunity.analysis_claimed_at)


async def cleanse_missing(session: AsyncSession, owner: str, role_ids: Iterable[uuid.UUID]) -> int:
    """Make the cleansed copy of each of these roles that has none: from the raw copy its posting holds, else from the
    text it holds, entered as a `no_raw` copy (`raw_copies.keep_legacy`). A role with no text gets none. Returns how
    many copies were made."""
    ids = list(role_ids)
    if not ids:
        return 0
    pairs = {(source, ext) for source, ext in (await session.execute(select(JobPosting.employer_id, JobPosting.ext_id)
                                                                    .where(JobPosting.owner == owner,
                                                                           JobPosting.opportunity_id.in_(ids)))).all()}
    made = (await raw_copies.recleanse(session, owner, only=pairs))["rebuilt"]
    return made + await raw_copies.keep_legacy(session, owner, role_ids=ids)


async def _in_batches(session: AsyncSession, ids: list[uuid.UUID],
                      make: Callable[[list[uuid.UUID]], Awaitable[int]]) -> int:
    """`make` for each batch of `WRITE_BATCH` of these role ids, the transaction ended after each (`db.release_if_free`,
    which a caller inside a savepoint or a process step does not allow: there the batches share its transaction).
    Returns the total `make` answered."""
    done = 0
    for at in range(0, len(ids), WRITE_BATCH):
        done += await make(ids[at:at + WRITE_BATCH])
        await db.release_if_free(session)
    return done


async def settle(session: AsyncSession, owner: str, ranked: set[uuid.UUID]) -> int:
    """Run every role in the getting-ready stages, and in the two places `rebalance` gives, through `stage_of`. `ranked`
    are the roles with a rank; which have a Second Impression and which something holds in place is asked of
    `placement` once, before the loop. Returns how many changed stage.

    Read, then decide, then move: the roles, their holds, their copies
    and which copies are under `cleansed.MIN_TEXT` are read; the copies a role lacks and the whole texts its postings
    hold are made first, in batches of `WRITE_BATCH` roles each committed, and only when there are any; the transaction
    is ended; every stage is decided from what was read, with no query; and only a role still as it was read
    (`as_read.lock`, on `STANDS`) is moved, under the lock of its row."""
    # Every read is made apart from the writer's turn (`off_turn.reading`: a reader's session where the caller holds
    # nothing, else the caller's own): the roles, their holds and their copies are many rows, which would
    # hold the file's one turn up to the first batch of copies. The turn is
    # taken for a batch of copies and for the moves, nothing else.
    async with off_turn.reading(session) as reader:
        query = select(Opportunity).where(Opportunity.owner == owner, Opportunity.status.in_(LOOKED_AT))
        if reader is not session:  # detached when it closes: only the columns a settle reads (`SETTLE_COLUMNS`)
            query = query.options(load_only(*SETTLE_COLUMNS))
        else:
            query = query.options(defer(Opportunity.analysis), defer(Opportunity.description), *UNREAD)
        roles = await off_turn.scalars_in_slices(reader, query)
        if not roles:
            return 0
        places = await placement.load(reader, owner, roles, placement.Purpose.STAGE)
        copies = await cleansed.current_for_roles(reader, owner)
    lacking = [r.id for r in roles if r.id not in copies and r.status in PLACED]
    if lacking and await _in_batches(session, lacking, lambda ids: cleanse_missing(session, owner, ids)):
        async with off_turn.reading(session) as reader:  # when one was made: a role with no text gets none
            copies = await cleansed.current_for_roles(reader, owner)
    async with off_turn.reading(session) as reader:
        short = await cleansed.short_ids(reader, [copies[r.id] for r in roles if r.id in copies])
    thin_roles = [r.id for r in roles if r.id in copies and r.status in PLACED and copies[r.id] in short]
    if thin_roles and await _in_batches(session, thin_roles,
                                        lambda ids: raw_copies.keep_whole_text(session, owner, ids)):
        async with off_turn.reading(session) as reader:  # a posting read since holds the whole text
            copies = await cleansed.current_for_roles(reader, owner)
            short = await cleansed.short_ids(reader, [copies[r.id] for r in roles if r.id in copies])
    missing = [r.id for r in roles if r.id not in copies]
    raw: set[uuid.UUID | None] = set()
    if missing:
        async with off_turn.reading(session) as reader:
            raw = {i for (i,) in (await reader.execute(select(JobPosting.opportunity_id).where(
                JobPosting.opportunity_id.in_(missing), raw_copies.has_raw(JobPosting)).distinct())).all()}
    await db.release_if_free(session)
    moves: list[tuple[Opportunity, str]] = []
    for role in roles:
        copy = copies.get(role.id)
        target = stage_of(Facts(
            status=role.status, copy=None if copy is None else 0 if copy in short else cleansed.MIN_TEXT,
            raw=role.id in raw, no_text=role.analysis_state == analysis.NO_TEXT, ranked=role.id in ranked,
            read=places[role.id].read, graded=places[role.id].graded, held=places[role.id].held is not None))
        if target != role.status:
            moves.append((role, target))
    if not moves:
        return 0
    moved = 0
    for at in range(0, len(moves), MOVE_SLICE):  # a turn per slice: the file's one writer is never held for them all
        part = moves[at:at + MOVE_SLICE]
        held = await as_read.lock(session, Opportunity, [role for role, _ in part], stands=STANDS,
                                  defer=(Opportunity.analysis, Opportunity.description))
        for read, target in part:
            if (role := held.rows.get(read.id)) is not None:  # locked and as read: written as the lock gave it
                was, role.status = role.status, target
                moved += 1
                if was == stages.READY_TO_LAUNCH:
                    await _say_left_ready(session, owner, role, target, places[role.id])
        await session.flush()
        await db.release_if_free(session)
    return moved


async def _say_left_ready(session: AsyncSession, owner: str, role: Opportunity, target: str,
                          place: placement.Standing) -> None:
    """The history line of a role that leaves Ready to Launch because it is not read: no Second Impression, or one that
    graded nothing of the posting, or no text of the posting to read. It comes back once it is read, if it still
    ranks."""
    if role.analysis_state == analysis.NO_TEXT:
        why = "the posting's text is missing"
    elif place.read:
        why = "its Second Impression graded nothing of the posting"
    else:
        why = "it has no Second Impression"
    await trail.record(
        session, owner, event="left_ready_unread", actor="app", who=trail.APP_WHO, link=("opportunity", role.id),
        sentence=f"{role.title} at {role.company} left Ready to Launch for {stages.LABELS.get(target, target)}: {why}.",
        detail="A role in Ready to Launch stands on a Second Impression that graded its posting. It is read first, and "
               "comes back once it is read if it still ranks.")
