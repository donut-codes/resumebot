"""Three rules that end idle roles (docs/PRD.md 14.7, 15.1.8 and 15.1.9).

This module holds the first two, which are code with no Claude and no fetching:

1. **The posting went.** A role in any status before Applied, or Applied, whose posting the employer took down
   moves to Gone Quiet, "The posting went", with the evidence in its history.
2. **No word came back.** An Applied role with no word for the owner's number of days (`Settings.no_reply_days`) moves
   to Gone Quiet, "No word came back".

(The third, "Still worth it?", moved nothing by itself; its screen and routes are gone.)

**One way to end a role by rule: `end_role`.** It sets the status and the reason on the role, writes the history line
and flushes, so the stage move, the classifier's history and the Pipeline's last-day changes all come from the
before-flush listener on the role (`models._record_stage_moves`), never from a bulk update. The same-employer limit
(34.2) hooks it.

**A rule never moves a role the owner moved by hand since the evidence it acts on** (`hand_since`): the owner's
`role_moved` (`placement.MOVED_EVENT`) and `packet_asked` lines, later than the evidence. A role the owner pulled back
stays back for that closing; a posting that closes again later is a new closing. Rule 2 has no hand line of its own: it
counts from the day the role became Applied, so a hand move into Applied starts the count again. A rule never moves a
role back out of Gone Quiet either: the owner does that by hand.

**Rule 1 reads only what the app read from the employer's own board** (a job system, or the employer's own site; never a
job board, the owner's browser, or the posting the log keeps for a role), and only a read that could have returned the
posting closes it (`postings.store`, docs/PRD.md 39.5): this module reads `closed_at` and adds nothing to it. A role
moves when it has a closed posting that speaks and none that speaks is open or Not checked. The postings of a role are
those linked to it and the employer's unlinked ones that carry one of the role's links (`same_role`): the link the app
followed a role to is not always one a posting was linked by. It reads and writes no link.

One run at a time for an owner (a named lock, `db.transaction_lock`, taken by `run`). **No role row is waited for.** A
rule reads the roles in its stages with nothing locked, decides in memory, and locks only the roles it will move
(`as_read.lock`: id order,
`FOR UPDATE SKIP LOCKED`, each still in the stage it was read in). A role another transaction holds is left for the next
run. (Locking every role in play and waiting, to the end of the step's transaction, is half of a deadlock with the
browser queue's refresh, whose items hold a role `FOR KEY SHARE` in the order they are listed, and it keeps every other
write to those roles waiting.) Days are the owner's days. Services flush; the route (or the night's step) commits."""

import logging
import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import NamedTuple

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app import db, program
from app.jobsearch import as_read, ats, off_turn, stages
from app.jobsearch.employers.core import key_of
from app.jobsearch.models import POSTING_NOT_FOUND, JobPosting, Opportunity, StageMove, posting_not_found
from app.jobsearch.pipeline import placement
from app.jobsearch.postings import coop as cooperating
from app.jobsearch.postings import same_role, unbuilt
from app.jobsearch.postings.coop import Cooperate
from app.jobsearch.profile import store as profile_store
from app.models.work import TrailEvent
from app.timeutil import owner_today, owner_zone
from app.trail import service as trail

log = logging.getLogger(__name__)

#: The owner's lines that stop a rule moving a role on older evidence: a move on the board and an ask for a
#: packet. (Only the move exempts a role from a re-gate and holds it in place: `placement.MOVED_EVENT`.)
HAND_LINES = (placement.MOVED_EVENT, "packet_asked")
#: The history events a rule writes (none is `placement.MOVED_EVENT`, so a rule's own move is never a hand move).
POSTING_WENT_EVENT, NO_REPLY_EVENT = "posting_went", "no_reply"
#: The history line a posting taken down by the employer leaves, when the role's own source said so (`end_gone`).
REMOVED_EVENT = "posting_removed"
#: The stages rule 1 acts on (Active Discussion and every Ended stage are left alone), and the one rule 2 acts on.
RULE_ONE_STAGES = (*stages.PRE_APPLY, stages.APPLIED)
RULE_TWO_STAGE = stages.APPLIED
#: How many ids are asked of the database at once.
CHUNK = 2000


@dataclass
class Result:
    """The step's output, as the playbook's `quiet`."""

    #: Roles moved because the posting went.
    went: int = 0
    #: Applied roles moved because no word came back.
    silent: int = 0
    #: The no-reply number in force; None when blank.
    days: int | None = None


def _speaking() -> list[str]:
    """The postings that speak for a role (R1.1): read by the app from the employer's own board."""
    return [*ats.SYSTEMS, ats.OWN_SITE]


def days_of(raw: int | None) -> int | None:
    """The owner's number of days as the rules read it: None for blank, zero or negative (never). The stored value is
    not bounded by the model (older versions must still load); the bounds are the Missions screen's."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else None


def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


def report_lines(data: dict | None) -> list[str]:
    """The run-report lines for the step's output (`{"went", "silent", "days"}`): rule 2's line only, and nothing when
    its count is zero or the number is blank. The wording is written here and nowhere else."""
    if not data:
        return []
    silent, days = data.get("silent") or 0, days_of(data.get("days"))
    if silent <= 0 or days is None:
        return []
    return [f"{silent} Applied role{'' if silent == 1 else 's'} moved to Gone Quiet: no word in {_days(days)}."]


# --- the pieces every rule uses ---------------------------------------------------------------------------------------


def _utc(moment: datetime) -> datetime:
    """A time with its zone: a naive one (a test database hands them back without) is taken as UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _day(moment: datetime) -> date:
    """The owner's day a time falls on (G10)."""
    return _utc(moment).astimezone(owner_zone()).date()


def _said(day: date) -> str:
    return f"{day:%b} {day.day}"


def _chunks[T](items: Sequence[T]) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), CHUNK):
        yield items[start:start + CHUNK]


async def _lock(session: AsyncSession, owner: str) -> None:
    """One run at a time for an owner (G7): a second waits for the first's save (up to the lock wait, then it is told
    busy), then reads what it left. Held to the end of the transaction (`db.transaction_lock`), and a session that
    already holds it takes it again without waiting."""
    await db.transaction_lock_or_busy(session, f"quiet:{owner}", "a quiet run for you is going; nothing was changed")


async def _settle(session: AsyncSession, owner: str) -> None:
    """A role left play, so its employer's watch follows (G9). The sites import much of the job search."""
    from app.sites import where

    await where.settle_watch(session, owner)


async def end_role(
        session: AsyncSession, owner: str, role: Opportunity, *, reason: str, event: str, sentence: str,
        detail: str | None, to: str = stages.GONE_QUIET, who: str | None = None) -> TrailEvent:
    """The only way a rule ends a role (G2): the status and the reason on the role (the reason before the flush, so the
    listener keeps it), then the history line, which flushes. `to` is an Ended stage: Gone Quiet for a rule, Rejected
    for the mail reader's rejection (reason `they_said_no`). Returns the line it wrote, with the id the mail record
    keeps. The line is the app's, a result, linked to the role (with `who`, the named agent's: the code checker's).
    Nothing here checks that the role may move: the caller has."""
    if to not in stages.ENDED:
        raise ValueError(f"a role ends in {', '.join(stages.ENDED)}, not {to}")
    if reason not in stages.END_REASONS:
        raise ValueError(f"no end reason called {reason}")
    role.ended_reason = reason
    role.status = to
    return await trail.record(
        session, owner, event=event, sentence=sentence, actor="agent" if who else "app", who=who,
        agent_slug="scout" if who else None, tier="result", link=("opportunity", role.id), detail=detail)


async def end_gone(session: AsyncSession, role: Opportunity, link: str, said: str, who: str | None = None) -> bool:
    """End a role because its source said the posting is gone: Gone Quiet with the reason `posting_gone` ("The posting
    went"), and a history line (`REMOVED_EVENT`) that keeps the link and what the page or the answer said. Only a role
    before the application is ended (`stages.PRE_APPLY`); any other is left as it is. A role the owner applied to or is
    in talks about stays where the owner put it (2026-11-05): the app gives up on its posting instead
    (`models.POSTING_NOT_FOUND`), so no reader asks for it again, and the history line says so. Returns whether it
    ended. Only the role's own source says so: a read of it that answered 404 or 410 (`polite.gone`), or the owner's
    browser reporting a page that plainly says so; a block, a wall or a rate limit never does. Services flush; the
    caller commits."""
    from app.jobsearch.research import line  # analysis reads this module; imported here, never at the top

    if role.status in unbuilt.STAGES:
        if not posting_not_found(role):
            role.analysis = {**(role.analysis or {}), POSTING_NOT_FOUND: True}
            label = stages.LABELS.get(role.status, role.status)
            await trail.record(
                session, role.owner, event=REMOVED_EVENT,
                sentence=f"The employer took the posting down; the role stays in {label}.",
                actor="agent" if who else "app", who=who, agent_slug="scout" if who else None, tier="result",
                link=("opportunity", role.id),
                detail=f"The role's source, {link}, says the posting is gone: {(said or '').strip()[:500]} You are in "
                       f"{label}, so the role is not moved, and the app looks for its posting no more.")
            await session.flush()
        return False
    if role.status not in RULE_ONE_STAGES:
        return False
    await line.leave(session, role, line.ROLE_CLOSED)  # research that was done stays done; one in line leaves the line
    await end_role(
        session, role.owner, role, reason=stages.POSTING_GONE, event=REMOVED_EVENT,
        sentence="Gone Quiet: the employer took the posting down.",
        detail=f"The role's source, {link}, says the posting is gone: {(said or '').strip()[:500]}", who=who)
    return True


async def hand_since(session: AsyncSession, owner: str, role_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """The time of each role's latest owner hand line (`HAND_LINES`); absent when
    it has none. A rule leaves a role alone when this is later than its evidence (G4). `placement`'s "ever" (the gates'
    `Hold.MOVED`) is not used: it would exempt every role the owner marked Applied by hand."""
    found: dict[uuid.UUID, datetime] = {}
    for ids in _chunks(list(dict.fromkeys(role_ids))):
        rows = await session.execute(
            select(TrailEvent.link_id, func.max(TrailEvent.at)).where(
                TrailEvent.owner == owner, TrailEvent.actor == "person", TrailEvent.event.in_(HAND_LINES),
                TrailEvent.link_kind == "opportunity", TrailEvent.link_id.in_(ids)).group_by(TrailEvent.link_id))
        found.update({role_id: _utc(at) for role_id, at in rows if role_id is not None and at is not None})
    return found


async def last_matched_mail(
        session: AsyncSession, owner: str, role_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, date]:
    """Each role's latest matched mail, by the mail's own received day, not by when the app read or classed it: the day
    the mail came is what 14.7 counts from, and a mail read late must not shorten the quiet count (R2.3). The mail
    sync keeps each matched message (`MailMessage`, 14.9) with its day and the role it was matched to; mail of no
    kind that matters (`other`) does not count. The day is the owner's day the mail was filed on, and is returned as it
    is: made a moment at noon UTC and read back in the owner's zone it was the next day from UTC+12 on."""
    from app.jobsearch.mail import kinds as mail_kinds
    from app.jobsearch.models import MailMessage

    found: dict[uuid.UUID, date] = {}
    for ids in _chunks(list(dict.fromkeys(role_ids))):
        rows = await session.execute(
            select(MailMessage.role_id, func.max(MailMessage.day)).where(
                MailMessage.owner == owner, MailMessage.role_id.in_(ids), MailMessage.kind != mail_kinds.OTHER)
            .group_by(MailMessage.role_id))
        found.update({role_id: day for role_id, day in rows if role_id is not None})
    return found


async def _roles(session: AsyncSession, owner: str, stage_list: Sequence[str]) -> list[Opportunity]:
    """The owner's roles in these stages as they stand, the oldest first, with nothing locked: a rule decides from this
    and locks only the roles it moves (`_lock_movers`). Anything pending is written first, so the reload cannot drop
    it."""
    await session.flush()
    return await off_turn.scalars_in_slices(
        session, select(Opportunity).where(Opportunity.owner == owner, Opportunity.status.in_(stage_list))
        .order_by(Opportunity.created_at, Opportunity.id).execution_options(populate_existing=True))


async def _lock_movers(
        session: AsyncSession, movers: Sequence[Opportunity], stage_list: Sequence[str]) -> as_read.Held[Opportunity]:
    """Lock the roles a rule will move, in id order and waiting for none (`as_read.lock`). The ones in `Held` are
    current, still in a stage the rule acts on, and the only ones it may write; the rest (another transaction holds
    them, or they moved since the read) are left for the next run."""
    return await as_read.lock(
        session, Opportunity, movers, stands=(Opportunity.status,), where=(Opportunity.status.in_(stage_list),))


# --- rule 1: the posting went -----------------------------------------------------------------------------------------

_POSTING_COLUMNS = load_only(
    JobPosting.id, JobPosting.employer_id, JobPosting.ats, JobPosting.company, JobPosting.title, JobPosting.url,
    JobPosting.apply_url, JobPosting.links, JobPosting.opportunity_id, JobPosting.first_seen, JobPosting.last_seen,
    JobPosting.closed_at, JobPosting.not_checked_at)


def _firm(name: str | None) -> str:
    return key_of(name) or " ".join((name or "").lower().split())


def _same_employer(role: Opportunity, posting: JobPosting) -> bool:
    """Whether a posting belongs to the role's employer: the record it is filed under, or, for a role with no record,
    the company's key (as `same_role.Known` has it)."""
    if role.employer_id is not None:
        return posting.employer_id == role.employer_id
    firm = _firm(role.company)
    return bool(firm) and firm == _firm(posting.company)


def _links(*links: str | None) -> list[str]:
    return [link.strip() for link in links if link and link.strip()]


_ROWID = literal_column(f"{JobPosting.__tablename__}.rowid")


class Mark(NamedTuple):
    """The newest row of the postings table at one moment: its rowid and its id. A row written after the moment has a
    higher rowid, as long as this row is still there (SQLite numbers a new row one above the table's highest, so it can
    go below this one only when the highest rows were deleted since)."""
    rowid: int
    id: uuid.UUID | None


async def _mark(session: AsyncSession) -> Mark:
    """The postings table's newest row now (`Mark(0, None)` for an empty table: every row is then newer)."""
    row = (await session.execute(select(_ROWID, JobPosting.id).order_by(_ROWID.desc()).limit(1))).first()
    return Mark(0, None) if row is None else Mark(row[0], row[1])


async def _speaking_postings(
        session: AsyncSession, owner: str, roles: Sequence[Opportunity], *, since: Mark | None = None
) -> dict[uuid.UUID, dict[uuid.UUID, JobPosting]]:
    """Each role's postings that speak (R1.1, R1.2): those linked to it, and an employer-own posting with no link that
    carries one of the role's links. A posting carries a role's link when the two share a link exactly (trimmed), or
    share a mark (the same host and the same id: a tracking suffix, a title slug or a `www.` does not matter) and the
    posting belongs to the role's employer. A link with no id has no mark and matches only exactly. Read only.

    With `since` only the second half is read, and only the postings with no link written after that mark (by the
    table's rowid, a search, not a scan), matched the same way: what the turn needs to see whether a repost with no link
    was written since the first read. When the mark's row is gone (the highest rows were deleted, so a new row may
    have taken a number at or below it) every unlinked posting is read, as the first read did."""
    by_role: dict[uuid.UUID, dict[uuid.UUID, JobPosting]] = defaultdict(dict)
    pause = cooperating.Cooperate()  # for the loops over the roles and their linked postings
    for ids in () if since is not None else _chunks([role.id for role in roles]):
        for posting in await off_turn.scalars_in_slices(session, select(JobPosting).options(_POSTING_COLUMNS).where(
                JobPosting.owner == owner, JobPosting.ats.in_(_speaking()), JobPosting.opportunity_id.in_(ids)
        ).execution_options(populate_existing=True)):
            await pause()
            if posting.opportunity_id is not None:
                by_role[posting.opportunity_id][posting.id] = posting
    exact: dict[str, set[uuid.UUID]] = defaultdict(set)
    by_mark: dict[str, set[uuid.UUID]] = defaultdict(set)
    by_id = {role.id: role for role in roles}
    for role in roles:
        await pause()  # pure work per role: the loop is given back every few milliseconds
        links = _links(role.url, role.apply_url, role.board_url)
        for link in links:
            exact[link].add(role.id)
        for mark in same_role.marks(links):
            by_mark[mark].add(role.id)
    if not exact and not by_mark:
        return by_role
    coop = Cooperate()
    unlinked = select(JobPosting).options(_POSTING_COLUMNS).where(JobPosting.ats.in_(_speaking()))
    if since is not None and (
            since.id is None or await session.scalar(select(_ROWID).where(JobPosting.id == since.id)) == since.rowid):
        # A handful of rows by the rowid, "no link" checked in memory below. The owner is compared as an expression so
        # that no index on it competes: the planner would search all of the owner's rows by it.
        unlinked = unlinked.where(_ROWID > since.rowid, JobPosting.owner + "" == owner)
    else:
        unlinked = unlinked.where(JobPosting.owner == owner, JobPosting.opportunity_id.is_(None))
    for posting in await off_turn.scalars_in_slices(session, unlinked.execution_options(populate_existing=True)):
        await coop()  # pure work per posting: the loop is given back every few milliseconds
        if posting.opportunity_id is not None:
            continue
        links = _links(*same_role.posting_links(posting))
        carried: set[uuid.UUID] = set()
        for link in links:
            carried |= exact.get(link, set())
        for mark in same_role.marks(links):
            carried |= {rid for rid in by_mark.get(mark, ()) if _same_employer(by_id[rid], posting)}
        for role_id in carried:
            by_role[role_id][posting.id] = posting
    return by_role


def _gone(postings: Iterable[JobPosting]) -> JobPosting | None:
    """The posting that says a role's posting went (R1.3): the latest closed one, when at least one speaking posting is
    closed and none is open or Not checked. None otherwise (a repost leaves a role alone; so does a board nothing is
    checking)."""
    latest: JobPosting | None = None
    latest_at: datetime | None = None
    for posting in postings:
        if posting.closed_at is None:
            return None  # open, or Not checked: nothing says the employer took everything down
        closed = _utc(posting.closed_at)
        if latest_at is None or closed > latest_at:
            latest, latest_at = posting, closed
    return latest


def _board_words(role: Opportunity, posting: JobPosting) -> str:
    """The evidence of a closing, as the role's history says it (R1.6)."""
    company = posting.company or role.company
    board = f"{company}'s own site" if posting.ats == ats.OWN_SITE else f"{company}'s {ats.label(posting.ats)} board"
    closed = posting.closed_at
    seen = f"It was last seen on {_said(_day(posting.last_seen))}" if posting.last_seen else "It was seen before"
    gone = (f" and was not in the read of {_said(_day(closed))}, which could have returned it" if closed else "")
    return (f'{board} no longer lists "{posting.title}". {seen}{gone}. '
            f"The role was in {stages.LABELS.get(role.status, role.status)}.")


async def _move_for_posting(
        session: AsyncSession, owner: str, role: Opportunity, *, at: datetime, words: str,
        hand_at: datetime | None) -> bool:
    """The guard and the move, for a role that is locked and current: its stage (G3) and the owner's hand since the
    evidence (G4). Writes the one line. The only place a role leaves play because its posting went."""
    if role.owner != owner or role.status not in RULE_ONE_STAGES:
        return False
    if hand_at is not None and hand_at > _utc(at):
        return False
    await end_role(
        session, owner, role, reason=stages.POSTING_GONE, event=POSTING_WENT_EVENT,
        sentence="Gone Quiet: the posting went.", detail=words)
    return True


async def _evidence_again(
        session: AsyncSession, owner: str, speaking: dict[uuid.UUID, dict[uuid.UUID, JobPosting]],
        roles: Mapping[uuid.UUID, Opportunity], since: Mark) -> dict[uuid.UUID, JobPosting | None]:
    """Under the turn, the cheap evidence of the roles about to move, read again (the postings that were the evidence
    and the ones linked to the roles now, and a posting with no link written since the first read that carries a
    role's link): each role's `_gone` posting as it stands now, None when the role's postings no longer say it went (a
    repost linked or opened since the read). A posting read before keeps its place in the order, so a tie of two
    closings at one instant picks the same one as the first read did."""
    role_ids = list(roles)
    ids = {pid for rid in role_ids for pid in speaking.get(rid, {})}
    fresh: dict[uuid.UUID, JobPosting] = {}
    for part in _chunks(sorted(ids)):
        for posting in await session.scalars(select(JobPosting).options(_POSTING_COLUMNS).where(
                JobPosting.owner == owner, JobPosting.ats.in_(_speaking()), JobPosting.id.in_(part))
                .execution_options(populate_existing=True)):
            fresh[posting.id] = posting
    linked: dict[uuid.UUID, dict[uuid.UUID, JobPosting]] = defaultdict(dict)
    for part in _chunks(sorted(role_ids)):
        for posting in await session.scalars(select(JobPosting).options(_POSTING_COLUMNS).where(
                JobPosting.owner == owner, JobPosting.ats.in_(_speaking()), JobPosting.opportunity_id.in_(part))
                .order_by(JobPosting.id).execution_options(populate_existing=True)):
            if posting.opportunity_id is not None:
                fresh[posting.id] = posting
                linked[posting.opportunity_id][posting.id] = posting
    carried = await _speaking_postings(session, owner, list(roles.values()), since=since)
    now: dict[uuid.UUID, JobPosting | None] = {}
    for rid in role_ids:
        mine = {pid: fresh[pid] for pid in speaking.get(rid, {}) if pid in fresh}
        for pid, posting in linked.get(rid, {}).items():
            mine.setdefault(pid, posting)
        for pid, posting in carried.get(rid, {}).items():
            mine.setdefault(pid, posting)
        now[rid] = _gone(mine.values())
    return now


async def _rule_one(
        session: AsyncSession, owner: str, *, apart: bool = False) -> tuple[int, frozenset[uuid.UUID]]:
    """Move the roles whose posting the employer took down: how many moved, and the roles it left for the next run
    because another transaction held them (rule 2 leaves those alone too, G12). After the filter (R1.5): the filter
    links a repost to its role, and run before it a role would move the night its repost first appears. Only the roles
    that are due are locked, once they are decided (`_lock_movers`).

    With `apart` the read half (the roles, their postings, the owner's hand lines) is made in a reader's session, which
    takes no writer turn, and the decision in memory. The write half takes the turn for the moves only: the roles
    are locked (and so read again), the cheap evidence is read again under the turn, and only a role that still
    qualifies is written (a repost linked or opened, or a hand move, between the read and the lock keeps the role)."""
    async with off_turn.reading(session, apart=apart) as reader:
        since = await _mark(reader)  # before the first read: a posting written after it is read again under the turn
        roles = await _roles(reader, owner, RULE_ONE_STAGES)
        if not roles:
            return 0, frozenset()
        speaking = await _speaking_postings(reader, owner, roles)
        due = {role.id: gone for role in roles if (gone := _gone(speaking.get(role.id, {}).values())) is not None}
        if not due:
            return 0, frozenset()
        hand = await hand_since(reader, owner, due)
    held = await _lock_movers(session, [role for role in roles if role.id in due], RULE_ONE_STAGES)
    if reader is not session:  # the read was made before the turn: look at the evidence again under it
        due = {rid: gone for rid, gone in (await _evidence_again(session, owner, speaking, held.rows, since)).items()
               if gone is not None}
        hand = await hand_since(session, owner, due)
    moved = 0
    for role in roles:
        posting = due.get(role.id)
        if posting is None or posting.closed_at is None or role not in held:
            continue
        row = held.rows[role.id]
        if await _move_for_posting(
                session, owner, row, at=posting.closed_at, words=_board_words(row, posting),
                hand_at=hand.get(role.id)):
            moved += 1
    return moved, frozenset(held.skipped)


# --- rule 2: no word came back ----------------------------------------------------------------------------------------


async def _became_applied(
        session: AsyncSession, owner: str, role_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """The time each role last moved into Applied (R2.2): a hand move, a mail, or a move back out of Gone Quiet starts
    a new count. A role with no such move (the tracker import writes none) is absent."""
    found: dict[uuid.UUID, datetime] = {}
    for ids in _chunks(role_ids):
        rows = await session.execute(
            select(StageMove.role_id, func.max(StageMove.at)).where(
                StageMove.owner == owner, StageMove.to_status == stages.APPLIED, StageMove.role_id.in_(ids))
            .group_by(StageMove.role_id))
        found.update({role_id: _utc(at) for role_id, at in rows if at is not None})
    return found


def _due(
        roles: Iterable[Opportunity], applied: dict[uuid.UUID, datetime], mail: dict[uuid.UUID, date], today: date,
        days: int) -> list[tuple[Opportunity, date, date | None]]:
    """The roles with no word for `days` days, with the day each became Applied and the day of its last matched mail."""
    due: list[tuple[Opportunity, date, date | None]] = []
    for role in roles:
        became = _day(applied.get(role.id) or role.created_at)
        heard = mail.get(role.id)
        last = max(became, heard) if heard else became
        if (today - last).days >= days:
            due.append((role, became, heard))
    return due


async def _rule_two(
        session: AsyncSession, owner: str, today: date | None, left: Collection[uuid.UUID] = (), *,
        apart: bool = False) -> tuple[int, int | None]:
    """Move the Applied roles that have heard nothing for the owner's number of days: (how many, the number in force).
    The days count from the later of the day the role became Applied (else the day the app first held it) and the day
    of the last mail matched to it, as the mail itself came (R2.1 to R2.3). A role is ended once: rule 1 has moved its
    roles before this reads the stage, and the ones it left because another transaction held them (`left`) are left
    here too, for the next run's rule 1 (G12). Only the roles that are due are locked (`_lock_movers`).

    With `apart` the read half is made in a reader's session and the write half takes the turn for the moves only; the
    roles' Applied days and matched mail are read again under the turn, and only a role still due is written (a mail
    matched since the read keeps the role)."""
    today = today or owner_today()
    async with off_turn.reading(session, apart=apart) as reader:
        settings = (await profile_store.current(reader, owner)).settings
        days = days_of(settings.no_reply_days)
        if days is None:
            return 0, None
        roles = [role for role in await _roles(reader, owner, (RULE_TWO_STAGE,)) if role.id not in left]
        if not roles:
            return 0, days
        ids = [role.id for role in roles]
        applied = await _became_applied(reader, owner, ids)
        mail = await last_matched_mail(reader, owner, ids)
    due = _due(roles, applied, mail, today, days)
    held = await _lock_movers(session, [role for role, _, _ in due], (RULE_TWO_STAGE,))
    mine = [held.rows[role.id] for role, _, _ in due if role in held]
    if reader is not session:  # the read was made before the turn: look at the evidence again under it
        mine_ids = [role.id for role in mine]
        due = _due(mine, await _became_applied(session, owner, mine_ids),
                   await last_matched_mail(session, owner, mine_ids), today, days)
    else:
        due = [(held.rows[role.id], became, heard) for role, became, heard in due if role in held]
    moved = 0
    for role, became, heard in due:
        words = (f"It became Applied on {_said(became)}. "
                 + (f"The last mail matched to it came on {_said(heard)}." if heard
                    else "No mail has been matched to it.")
                 + f" Your number is {_days(days)} (Missions, Read the employers' boards).")
        await end_role(
            session, owner, role, reason=stages.NO_REPLY, event=NO_REPLY_EVENT,
            sentence=f"Gone Quiet: no word came back in {_days(days)}.", detail=words)
        moved += 1
    return moved, days


async def run(session: AsyncSession, owner: str, *, today: date | None = None) -> Result:
    """Rules 1 then 2 under the lock (G7, G12), and the watch settled once at the end (G9). Returns what moved, as the
    playbook's `quiet`.

    Where the transaction may be ended (`db.can_release`) the run holds no transaction while it reads: it takes the
    owner's named lock for the block (a second run is told busy after the lock wait), reads each rule in a reader's
    session, and takes the writer's turn for each rule's moves alone, committed before the next rule reads the stage.
    Anywhere else (a savepoint, a process step) the caller's transaction is the whole and the lock lasts to its end."""
    program.require("engine")
    if not db.can_release(session):
        await _lock(session, owner)
        went, left = await _rule_one(session, owner)
        silent, days = await _rule_two(session, owner, today, left)
        if went or silent:
            await _settle(session, owner)
        return Result(went=went, silent=silent, days=days)
    async with db.named_lock(
            f"quiet:{owner}", wait=db.lock_wait_seconds(), why="a quiet run for you is going; nothing was changed"):
        await db.release(session)
        went, left = await _rule_one(session, owner, apart=True)
        await db.release(session)
        try:
            silent, days = await _rule_two(session, owner, today, left, apart=True)
        except Exception:
            log.exception("moving the Applied roles that heard nothing failed; the roles that went are kept")
            await session.rollback()
            silent, days = 0, None
        await db.release(session)
        if went or silent:
            await _settle(session, owner)
        return Result(went=went, silent=silent, days=days)
