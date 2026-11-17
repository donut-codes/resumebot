"""The re-gate (docs/PRD.md 39.14 and 39.25): every posting in the log and every role checked again against the gates,
in code, from what is stored, with no Claude and no fetching, in seconds.

It runs as part of a save, never at a click: when Hard pass is saved (`profile_routes`), when an employer is gated or
ungated (`employers.edit`) and when a site to avoid is added or removed (`site_routes`); a corrected class re-gates
the one role it is about (`one_role`, from `settle.roles`). It also runs by itself when stops are left that
were judged under an older Hard pass than the one in force, or under none, which a save of Hard pass that changes
nothing does not reach (`catch_up`, run at every start of the app and at the start of the night's read:
`keep_current.py`), and for the roles a new day puts past the owner's freshness pass, the hour tick's own look
(`age_out.py`, PRD 40.6). The check is the one gate check (`gating.check`, 39.11), so a posting or a role is stopped
here for the reason it would be stopped anywhere, as data:
the step, the gate, the line it is and the versions of Hard pass and What to look for it was judged under (39.12).

**A posting.** One the gates now stop is stopped with the gate and the new versions. One a gate stopped that no
longer holds goes back to where it would have been had the gate never stopped it: on to become a role
(`pipeline/from_posting.py`); the classifier rates it again so it takes its place in the queue, and its text is fetched
once the night's descriptions reach it. The retired title check's own stops are not a gate's to undo, and nothing here
returns them; a posting the owner asked to read anyway is never touched. A posting a role was made from is the role's:
its gates are the role's.

**A role.** In any stage before Applied (`MOVABLE`) a role the gates now stop goes to Gated out with the gate and
the versions, and its history says it passed before; a Ready to Launch role keeps its packet (39.25). A Gated out role
whose stop no longer holds goes back to the stage its facts give it (`stage_for`): Ready to Launch with a packet that
is ready, being written or held, else the stage the gate took it from, else First Impression; its history
says why. A role at Applied or later, a role the owner moved by hand (23.8: `placement`, `Purpose.GATES`, ever) and a
role the old tracker's own screening put in Gated out (its step is `tracker`: no gate of this app stopped it) are never
moved by a re-run.

A gate's stop made before they were kept as data holds no step, no line and no versions ("unknown, re-gate"): the
first re-gate works it out again from what is stored and writes them. A sentence rule is checked on the answer the role
holds to its words now; one that was removed no longer holds, and one that is new or reworded passes until it is
answered (39.13 and 39.15).

**It is the one service that commits as it goes**. One transaction over every posting
and every role held rows for a long time, so a run reads first (every posting and role, judged in code), then
writes in slices of `CHUNK` postings or at most `ROLE_CHUNK` roles, each committed (`db.release_if_free`), so the
writers of the rows it changes are never held up for more than a slice. Every slice is whole and carries its own
condition: a posting or role that changed since it was read (the owner moved it, the filter made a role of it) is left
as it is, never overwritten, and a role that is locked by another transaction is looked at again at the end. A run
that stops part-way leaves the slices before it committed, and what it did not reach is found again: a stop judged
under an older Hard pass is `stale`, and `catch_up` runs again at the next start or night; an owner action is asked
again. (An employer gated or ungated has no version to find a cut-short run by: the flag is committed first, and a
crash in the minute after leaves some roles as they were until the owner gates or ungates again, or asks for the
check.)

One run at a time for an owner holds across the commits: a named lock of this process (`db.named_lock`; one process
holds the database), let go when the run ends. Inside a savepoint or the engine's call (the start's steps) nothing
can be committed, so the run is one transaction as before, under the transaction's own lock.

**No step waits for a role row.** A run takes role rows only `FOR UPDATE SKIP LOCKED`, in id order (`as_read.lock`:
its slices, the employer limit, the dial's drop), so a role another transaction holds is left for the next turn and the
re-gate is never half of a cycle of waits with the passes, whatever order they lock in (a deadlock at the dial's lock in
a dry run). **A dry run takes no row lock at all and writes nothing** (PRD 50.7: the same code with the write skipped):
it reads, judges in code and changes the roles it holds in memory only (the session does not flush:
`Session.no_autoflush`), so the employer limit, which reads the roles as the run leaves them, is given those
(`employer_limit.settle(planned=...)`); the report is what a real run reports. The dial's drop is not counted in a
report, so a dry run leaves it out."""

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app import db, program
from app.jobsearch import as_read, judged, off_turn, stages
from app.jobsearch.fit import classifier, freshness, scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import sift
from app.jobsearch.models import JobPosting, Opportunity, Packet, StageMove
from app.jobsearch.pipeline import placement
from app.jobsearch.postings import cleansed
from app.jobsearch.postings.coop import Cooperate
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Profile
from app.jobsearch.states import PacketState, StopGate, StopStep
from app.jobsearch.work import queue
from app.trail import service as trail

#: The steps whose stop of a role no re-run returns: the old tracker's, and the title check's (a gate does not read
#: What to look for, so it can't say the title now matches).
KEPT_STEPS = (StopStep.TRACKER, StopStep.TITLES)
#: The stages a re-run may move a role between: the three before Applied, and Gated out.
MOVABLE = (*stages.PRE_APPLY, stages.GATED_OUT)
#: What a role's history says, as `trail` events.
STOPPED_EVENT, RELEASED_EVENT, RESTATED_EVENT = "regate_stopped", "regate_released", "regate_restated"
#: How many rows one transaction of a run writes: a slice of postings, a slice of roles.
CHUNK = 250
#: How many roles one transaction of a run writes at most: checking a role is code with no wait, so a slice of roles
#: is one unbroken block under the writer's turn, and it is kept smaller than a slice of postings.
ROLE_CHUNK = 50
#: How many rows one transaction of a run reads: a page of postings, a page of roles. A page is read, the transaction
#: ended, and only then is it judged, so the writer's turn is held for the read alone.
PAGE = 250

log = logging.getLogger(__name__)

#: Whether this task is a dry run of the re-gate (`run(dry_run=True)`): it plans only, so nothing is locked, written or
#: said, and the roles it changes are changed in memory (`_roles`, `_check_role`, `_say`).
_planning: ContextVar[bool] = ContextVar("regate_planning", default=False)


@contextlib.contextmanager
def _planning_only() -> Iterator[None]:
    token = _planning.set(True)
    try:
        yield
    finally:
        _planning.reset(token)


@dataclass
class Report:
    """What a re-gate did. `postings` and `roles` are how many were checked (a role at Applied or later, moved by hand
    or stopped by the old tracker is not: `roles_left`); the rest are the rows it changed, by what happened to each."""

    postings: int = 0
    postings_released: int = 0
    postings_stopped: int = 0
    postings_restated: int = 0
    roles: int = 0
    roles_returned: int = 0
    roles_stopped: int = 0
    roles_restated: int = 0
    roles_left: int = 0

    @property
    def now_pass(self) -> int:
        """How many a stop no longer holds for: postings back in line and roles back in play."""
        return self.postings_released + self.roles_returned

    @property
    def newly_stopped(self) -> int:
        """How many a gate stops that no gate stopped before."""
        return self.postings_stopped + self.roles_stopped

    @property
    def said(self) -> str:
        """What the save says, in one line: how many were checked and how many moved."""
        checked = f"Re-checked {_count(self.postings, 'posting')} and {_count(self.roles, 'role')}: "
        moved = [f"{self.now_pass:,} now pass"] if self.now_pass else []
        moved += [f"{self.newly_stopped:,} newly stopped"] if self.newly_stopped else []
        return checked + (", ".join(moved) if moved else "nothing changed") + "."

    def as_dict(self) -> dict:
        return {"said": self.said, "postings": self.postings, "postings_released": self.postings_released,
                "postings_stopped": self.postings_stopped, "postings_restated": self.postings_restated,
                "roles": self.roles, "roles_returned": self.roles_returned, "roles_stopped": self.roles_stopped,
                "roles_restated": self.roles_restated, "roles_left": self.roles_left}


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


async def run(session: AsyncSession, owner: str, *, cause: str, today: date | None = None,
              dry_run: bool = False) -> Report:
    """Check the whole log and every role again against the gates now in force, and change what the check changes.
    `cause` is what happened, in the words a role's history says it ("Hard pass was saved"). Returns what it did
    (`Report.said` is the line a save says). With `dry_run` the same check runs and plans, with no row lock and no
    write (the roles are changed in memory only), so the report is what a real run would do and nothing is kept (PRD
    path 50.7). The savepoint is only a net: should anything be written, it is rolled back."""
    if dry_run:
        nested = await session.begin_nested()
        try:
            with session.no_autoflush, _planning_only():
                return await _run(session, owner, cause=cause, today=today)
        finally:
            await nested.rollback()
    return await _run(session, owner, cause=cause, today=today)


@contextlib.asynccontextmanager
async def _one_at_a_time(session: AsyncSession, owner: str) -> AsyncIterator[None]:
    """One re-gate at a time for an owner: a second waits for the first's end, then reads the settings in force. Where
    the session's transaction can be committed the caller's transaction is ended before anything is waited for (what
    the caller had written, a saved Hard pass, is committed, and its pooled connection is let go: it is not held
    through a wait behind the first run), and the lock (`db.named_lock`) outlives the commits of the run. Inside a
    savepoint or the engine's call the lock is the transaction's own (`db.transaction_lock`), let go when the
    caller's transaction ends, as it always was, and a caller that waits past the lock wait is told busy."""
    program.require("engine")
    key = f"regate:{owner}"
    if not db.can_release(session):
        await db.transaction_lock_or_busy(
            session, key, "the settings are being re-checked against your roles by another save; nothing was changed")
        yield
        return
    await db.release(session)  # before the wait, never after it
    async with db.named_lock(key):
        yield


async def _run(session: AsyncSession, owner: str, *, cause: str, today: date | None = None) -> Report:
    async with _one_at_a_time(session, owner):
        dry = _planning.get()
        rules = await gating.rules(session, owner, today=today)
        await db.release_if_free(session)
        report = Report()
        if not dry:  # open roles in Cleansed Data and Gated out take the towns their postings name, then are judged
            from app.jobsearch.gating import stated_place  # it settles roles, which runs this module's check

            await stated_place.settle_open_places(session, owner)
            await db.release_if_free(session)
        await _postings(session, owner, rules, report)
        moved = await _roles(session, owner, rules, report, cause)
        from app.jobsearch.pipeline import employer_limit  # it reads the gates, which import this module

        settled = await employer_limit.settle(session, owner, planned=moved if dry else None)
        report.roles_stopped += settled.held
        report.roles_returned += settled.returned
        await db.release_if_free(session)
        if not dry:  # a drop is held back, never stopped: nothing to count, so nothing to plan
            from app.jobsearch.pipeline import dial_hold  # it reads the research module, which imports this one

            await dial_hold.settle(session, owner)
            await db.release_if_free(session)
    return report


# --- postings ------------------------------------------------------------------------------------------------------


async def _bulk(session: AsyncSession, rows: list[dict]) -> None:
    """Write these changes to postings by id, a statement for each set of columns."""
    by_columns: dict[tuple, list[dict]] = {}
    for row in rows:
        by_columns.setdefault(tuple(sorted(row)), []).append(row)
    for group in by_columns.values():
        await session.execute(update(JobPosting), group)


def _postings_changed(session: AsyncSession, owner: str, rows: list[dict]) -> None:
    """A posting stopped or restated is looked at again (`posting.roles`: whether it becomes a role), and the deck's
    counts with it. With no rows this only answers the write the tripwire noted."""
    keys = [""] if rows else []
    queue.post_all(session, owner, [(queue.POSTING_ROLES, keys), (queue.DECK_DIRTY, keys)], cause="core:regate._bulk")


def _judgeable(owner: str) -> tuple:
    """What a posting has to be for a re-gate to judge it, as the read asks and as every slice asks again: the owner's,
    through the filter, not a role's, and not one the owner asked to read anyway."""
    return (JobPosting.owner == owner, JobPosting.filtered_at.is_not(None), JobPosting.opportunity_id.is_(None),
            JobPosting.owner_override.is_(False))


def _gate_stopped(owner: str) -> tuple:
    """A judgeable posting whose stop is still a gate's: what a slice that restates or releases it asks again."""
    return (*_judgeable(owner), JobPosting.filtered_by.is_not(None), JobPosting.judged_by == judged.GATE)


@contextlib.asynccontextmanager
async def _reading(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """A session to read plain rows with: a reader's, which takes no writer turn, where the run commits as it goes (it
    sees what is committed, which is all of it: the caller's own transaction was ended before the run began); the
    caller's own where nothing can be committed (a savepoint, the engine's call), so it sees what that transaction
    wrote."""
    if db.can_release(session) and not _planning.get():
        async with db.read_session_factory()() as reader:
            yield reader
    else:
        yield session


def _role_size() -> int:
    """How many roles one slice holds: `CHUNK`, or `ROLE_CHUNK` when that is fewer (both read when called)."""
    return min(CHUNK, ROLE_CHUNK)


def _slices[T](items: list[T], size: int | None = None) -> list[list[T]]:
    size = size or CHUNK
    return [items[start:start + size] for start in range(0, len(items), size)]


async def _written(session: AsyncSession, owner: str, changes: list[dict], where: tuple) -> int:
    """Write these changes to postings (each a row for `_bulk`, with its `id`) a slice at a time, each slice committed:
    only the postings still as the read found them (`where`, judged on the locked row) are written, and one another
    transaction holds is waited for, as the update did. Returns how many were written."""
    done = 0
    for chunk in _slices(changes):
        kept = (await as_read.lock(session, JobPosting, [r["id"] for r in chunk], where=where,
                                   refresh=False)).ids
        written = [r for r in chunk if r["id"] in kept]
        await _bulk(session, written)
        _postings_changed(session, owner, written)
        done += len(kept)
        await db.release_if_free(session)
    return done


async def _postings(session: AsyncSession, owner: str, rules: gating.Rules, report: Report) -> None:
    """Every posting that has been through the filter and is not a role (open or closed: a posting that comes back is
    not judged again, so what it holds has to be right), except one the owner asked to read anyway. They are all read
    and judged first, a page (`PAGE`) at a time, each page read in a transaction that writes nothing and judged after
    it ends; then written in slices (`CHUNK`), each committed. A dry run stops after the judging and counts what the
    slices would write (nothing is locked or changed)."""
    columns = (JobPosting.id, JobPosting.employer_id, JobPosting.company, JobPosting.location, JobPosting.work_mode,
               JobPosting.comp, JobPosting.comp_min, JobPosting.comp_max, JobPosting.posted, JobPosting.first_seen,
               JobPosting.url, JobPosting.apply_url, JobPosting.target_role, JobPosting.filtered_by,
               JobPosting.judged_by, JobPosting.judged_by_version, JobPosting.stop_gate, JobPosting.stop_gate_id,
               JobPosting.stop_hard_pass, JobPosting.title)
    stopped: list[dict] = []
    restated: list[dict] = []
    released: list[uuid.UUID] = []
    stamped: list[uuid.UUID] = []
    coop = Cooperate()
    async with _reading(session) as reader:  # the ids of all of them, then a page at a time: read, then judged
        ids = list((await reader.scalars(select(JobPosting.id).where(*_judgeable(owner)))).all())
    for page in _slices(ids, PAGE):
        async with _reading(session) as reader:
            rows = (await reader.execute(select(*columns).where(*_judgeable(owner), db.among(JobPosting.id, page))
                                         .order_by(JobPosting.id))).all()
        report.postings += len(rows)
        for row in rows:
            await coop()
            stop = gating.check(gating.posting_subject(row, rules.index.of(row.employer_id, row.company)), rules)
            if row.filtered_by is None:
                if stop is not None:  # it passed, and a gate stops it now
                    stopped.append({"id": row.id, **gating.posting_stop_values(stop, rules)})
            elif row.judged_by == judged.GATE:
                if stop is None:
                    released.append(row.id)
                    continue
                values = gating.posting_stop_values(stop, rules)
                now = (values["filtered_by"], values["stop_gate"], values["stop_gate_id"])
                if (row.filtered_by, row.stop_gate, row.stop_gate_id) != now:
                    restated.append({"id": row.id, "filtered_by": now[0], "stop_gate": now[1], "stop_gate_id": now[2]})
                # A gate's stop that holds was judged again, so it says it was judged under what is in force now.
                if row.stop_hard_pass != rules.hard_pass or row.judged_by_version != rules.criteria:
                    stamped.append(row.id)
    if _planning.get():  # every slice would find its postings as they were read, so each would be written whole
        report.postings_stopped, report.postings_restated = len(stopped), len(restated)
        report.postings_released = len(released)
        return
    # The classifier is trained before any posting is written, on the log as it was (it reads the owner's roles and
    # a sample of postings). It only reads, so it reads through a reader and holds no writer turn while it builds.
    model = None
    if released:
        async with _reading(session) as reader:
            model = await classifier.train(reader, owner)
    await db.release_if_free(session)
    # A slice is locked until it commits, and only the postings still as the read found them are written (a posting
    # that changed since, a role made of it, the owner asking to read it, another check judging it, is left alone). A
    # row another transaction holds is waited for, as the update did.
    report.postings_stopped += await _written(
        session, owner, stopped, (*_judgeable(owner), JobPosting.filtered_by.is_(None)))
    report.postings_restated += await _written(session, owner, restated, _gate_stopped(owner))
    report.postings_released = await _release(session, owner, model, released)
    for chunk in _slices(stamped):
        await session.execute(update(JobPosting).where(
            JobPosting.id.in_(chunk), *_gate_stopped(owner),
            or_(JobPosting.stop_hard_pass.is_distinct_from(rules.hard_pass),
                JobPosting.judged_by_version.is_distinct_from(rules.criteria)),
        ).values(stop_hard_pass=rules.hard_pass, judged_by_version=rules.criteria).execution_options(
            synchronize_session=False))
        # The stop stands: only the versions it was judged under are written, which no loop but this re-gate reads.
        queue.post_all(session, owner, [(queue.POSTING_ROLES, [""]), (queue.DECK_DIRTY, [""])],
                       cause="core:regate._postings")
        await db.release_if_free(session)


async def _release(session: AsyncSession, owner: str, model: classifier.Model | None, ids: list[uuid.UUID]) -> int:
    """Postings a gate stopped that no longer is stopped go on to become roles, as every posting that passes the gates
    does (docs/PRD.md 39.14). The classifier rates them (`model`, none before the owner has history), so they take their
    place in the order the roles are made in. A slice is locked and looked at again before it is written: a posting that
    is no longer a gate's stop is left. Returns how many were released."""
    done = 0
    for chunk in _slices(ids):
        batch = list((await as_read.lock(session, JobPosting, chunk, where=_gate_stopped(owner))
                      ).rows.values())
        for posting in batch:
            for column, value in gating.POSTING_CLEARED.items():
                setattr(posting, column, value)
            _, posting.fit_prob = sift.rank(model, posting)
            posting.route, posting.route_why = judged.ROLES, None
            posting.judged_by = posting.judged_by_version = None
            posting.nearest_role = None
        await session.flush()
        done += len(batch)
        await db.release_if_free(session)
    return done


# --- roles ---------------------------------------------------------------------------------------------------------


async def _latest_packets(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The state of each role's newest packet. The ids are one argument, however many (`db.among`): the run asks about
    every role, which can be more than the 32,767 arguments a statement may carry."""
    if not ids:
        return {}
    out: dict[uuid.UUID, str] = {}
    for role_id, status in (await session.execute(
            select(Packet.role_id, Packet.status).where(db.among(Packet.role_id, ids))
            .order_by(Packet.role_id, Packet.version.desc()))).all():
        out.setdefault(role_id, status)
    return out


async def _left(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The stage each of these roles was in when a gate last sent it to Gated out (from its stage moves); none for a
    role that was Gated out when it came in. The ids are one argument, however many (`db.among`): the run asks about
    every Gated out role."""
    if not ids:
        return {}
    out: dict[uuid.UUID, str] = {}
    for role_id, was in (await session.execute(
            select(StageMove.role_id, StageMove.from_status)
            .where(db.among(StageMove.role_id, ids), StageMove.to_status == stages.GATED_OUT,
                   StageMove.from_status.is_not(None))
            .order_by(StageMove.role_id, StageMove.at.desc()))).all():
        if was is not None:
            out.setdefault(role_id, was)
    return out


def stage_for(role: Opportunity, packet: str | None, left: str | None, profile: Profile, span) -> str:
    """The stage a role would be in had no gate stopped it (docs/PRD.md 39.14): Ready to Launch with a packet that
    is ready, being written or held; else the stage the gate took it from when that was before Applied; else First
    Impression."""

    if packet == PacketState.READY:
        return stages.READY_TO_LAUNCH
    if packet in (PacketState.WRITING, PacketState.HELD):
        return stages.READY_TO_LAUNCH
    if left in stages.PRE_APPLY:
        return left
    return stages.FIRST_IMPRESSION


def _say(session: AsyncSession, role: Opportunity, event: str, sentence: str, detail: str) -> None:
    """A line in the role's history, as the app writes one (none in a dry run). It is added, not flushed: the slice
    that checks the role flushes all its lines at once."""
    if _planning.get():
        return
    trail.add(session, role.owner, event=event, sentence=sentence, actor="app", who=trail.APP_WHO,
              link=("opportunity", role.id), detail=detail)


_JUDGE: Any = object()  # `_check_role`'s mark for a role its caller did not judge before


async def _check_role(session: AsyncSession, role: Opportunity, rules: gating.Rules, report: Report, cause: str,
                      packet: str | None, left: str | None, span, copy=None,
                      posted: freshness.PostedDay | None = None, stop: Any = _JUDGE) -> str | None:
    """One role against the gates (on its cleansed copy `copy`, when it has one, and the day it was posted, `posted`:
    `freshness.posted_by_role`): stop it, return it, or restate its stop. The caller has left out the roles no re-run
    may move. `packet` is the state of its newest packet and `left` the stage a gate took it from, for a role that is in
    Gated out. Returns the line it wrote in the role's history, or None when the role was left as it is. In a dry run
    (`_planning`) the role is changed in memory only and no line is written (the caller does not flush). `stop` is the
    gate check's answer when the caller judged the role before it took its slice's write (`_role_slice`); left out, it
    is judged here."""
    employer = rules.index.of(role.employer_id, role.company)
    # A sentence rule is checked on the reader's answer to its words now (`sentence_gates.answer_of`): a reworded rule
    # is a new question, so the answer to its old words stays on the role, unused, and the rule passes until a reader
    # answers it (docs/PRD.md 39.13, 39.14).
    if stop is _JUDGE:
        stop = gating.check(gating.role_subject(role, employer, copy, posted), rules)
    name = f"{role.title} at {role.company}"
    if role.status == stages.GATED_OUT:
        was = role.screened_out_reason or "a gate"
        if stop is None:
            back = stage_for(role, packet, left, rules.profile, span)
            for column, value in gating.ROLE_CLEARED.items():
                setattr(role, column, value)
            role.status = back
            report.roles_returned += 1
            sentence = f"Back in {stages.LABELS[back]}: the gate that stopped it no longer does."
            _say(session, role, RELEASED_EVENT, sentence,
                       f"{cause}, so the gates were checked again. {name} passes them now. It was Gated out: {was}")
            return sentence
        values = gating.role_stop_values(stop, rules)
        changed = [role.screened_out_by, role.screened_out_gate_id, role.screened_out_reason] != [
            values["screened_out_by"], values["screened_out_gate_id"], values["screened_out_reason"]]
        for column, value in values.items():
            setattr(role, column, value)
        if not changed:
            return None
        report.roles_restated += 1
        sentence = f"Still Gated out: {stop.role_words}"
        _say(session, role, RESTATED_EVENT, sentence,
                   f"{cause}, so the gates were checked again. It was Gated out: {was}")
        return sentence
    if stop is None:
        return None
    kept = " Its packet is kept." if role.status == stages.READY_TO_LAUNCH or packet is not None else ""
    gating.stop_role(role, stop, rules)
    report.roles_stopped += 1
    sentence = f"Gated out: {stop.role_words}"
    _say(session, role, STOPPED_EVENT, sentence,
               f"{cause}, so the gates were checked again. {name} passed them before.{kept}")
    return sentence


#: What a role's stage and stop were when the re-gate read it (`as_read.lock`'s `stands`): a role that holds anything
#: else when its slice is locked was moved meanwhile (by the owner, by another check), and is left where it is.
_ROLE_STANDS = (Opportunity.status, Opportunity.screened_out_by, Opportunity.screened_out_step,
                Opportunity.screened_out_gate_id, Opportunity.screened_out_reason)


#: The role's own columns the gates read (`gating.role_subject`), beyond the stage and stop: a role that holds other
#: values when its slice is locked (a reader's answer landed meanwhile) is judged again, locked, on the refreshed row.
_ROLE_READS = ("company", "employer_id", "location", "move_type", "comp", "posted", "found", "target_role", "title",
               "gate_answers", "analysis", "gate_facts", "analysis_state")


def _read_of(role: Opportunity) -> tuple:
    """What the gates read of a role now (`_ROLE_READS`), copied so a refresh in place cannot change it."""
    return tuple(deepcopy(getattr(role, name, None)) for name in _ROLE_READS)


def _seen(role: Opportunity) -> tuple:
    """What a role's stage and stop are now, as the dry run compares them before and after its check (it locks
    nothing)."""
    return tuple(getattr(role, stand.key) for stand in _ROLE_STANDS)


async def _roles(session: AsyncSession, owner: str, rules: gating.Rules, report: Report, cause: str
                 ) -> list[Opportunity]:
    """Every role in a stage before Applied and in Gated out (`MOVABLE`), but the ones no re-run may move. Their ids are
    read first; then a page (`PAGE`) of roles at a time is read, each read a transaction that writes nothing, and
    planned once they are over; each slice of at most `ROLE_CHUNK` roles of the page is locked (a role another
    transaction holds is looked at again at the end), looked at again as the slice finds it, checked and committed. A
    dry run locks nothing and writes nothing: each role is checked as it was read and changed in memory only; the
    roles it changed are returned (for the employer limit, which reads them as the run leaves them), and a real run
    returns none."""
    dry = _planning.get()
    if dry:
        return await _roles_planned(session, owner, rules, report, cause)
    async with _reading(session) as reader:
        ids = list((await reader.scalars(
            select(Opportunity.id).where(Opportunity.owner == owner, Opportunity.status.in_(MOVABLE))
            .order_by(Opportunity.created_at, Opportunity.id))).all())
    span = scoring.bounds(rules.profile)
    later: list[tuple[Opportunity, tuple]] = []  # held by another transaction: looked at again at the end
    for page in _slices(ids, PAGE):
        later += await _role_page(session, owner, rules, report, cause, page, span)
    for batch in _slices(later, _role_size()):  # once more, now that the others have moved on
        for role, _ in await _role_slice(session, owner, rules, report, cause, batch, span):
            log.info("a role stayed locked by another transaction, so the re-gate left it: %s", role.id)
    return []


async def _role_page(session: AsyncSession, owner: str, rules: gating.Rules, report: Report, cause: str,
                     page: list[uuid.UUID], span) -> list[tuple[Opportunity, tuple]]:
    """A page of roles (`PAGE`): read with what the check reads about them, the transaction ended, the ones no re-run
    may move left out, then checked in slices. Returns the roles another transaction held, with what was read."""
    order = {role_id: at for at, role_id in enumerate(page)}
    roles = sorted((await session.scalars(
        select(Opportunity).where(db.among(Opportunity.id, page)).options(defer(Opportunity.description)))).all(),
        key=lambda role: order[role.id])
    await db.release_if_free(session)  # each read of the page is a turn of its own
    ids = [r.id for r in roles]
    async with _reading(session) as reader:  # plain reads: a reader's, which takes no writer turn
        places = await placement.load(reader, owner, roles, placement.Purpose.GATES)
        packets = await _latest_packets(reader, ids)
        lefts = await _left(reader, [r.id for r in roles if r.status == stages.GATED_OUT])
        copies = await cleansed.gate_copies_of(reader, owner, ids)
        days = await freshness.posted_by_role(reader, owner, roles)
    await db.release_if_free(session)
    plan: list[tuple[Opportunity, tuple]] = []
    for role in roles:
        if places[role.id].held or (role.status == stages.GATED_OUT and (
                role.screened_out_step in KEPT_STEPS or role.screened_out_by == StopGate.LIMIT.value)):
            report.roles_left += 1
            continue
        plan.append((role, (packets.get(role.id), lefts.get(role.id), copies.get(role.id), days.get(role.id))))
    size = _role_size()
    return [held for batch in _slices(plan, size)
            for held in await _role_slice(session, owner, rules, report, cause, batch, span)]


async def _role_slice(session: AsyncSession, owner: str, rules: gating.Rules, report: Report, cause: str,
                      batch: list[tuple[Opportunity, tuple]], span) -> list[tuple[Opportunity, tuple]]:
    """A slice of roles locked, looked at again as it finds them, checked and committed. Returns the ones another
    transaction held (or that are gone), with what was read about them."""
    # The gates judge the slice before it is locked, from the roles as the page read them: the lock's stands are the
    # columns a role is moved by, so a role that held them is checked on what it was read as, and the text work (a
    # sentence rule, a place, a pay range, 50 roles of it) is done with no write open (2026-11-12).
    stops = {role.id: gating.check(gating.role_subject(role, rules.index.of(role.employer_id, role.company), copy, day),
                                   rules) for role, (_, _, copy, day) in batch}
    read = {role.id: _read_of(role) for role, _ in batch}
    held = await as_read.lock(session, Opportunity, [role for role, _ in batch], stands=_ROLE_STANDS,
                              where=(Opportunity.owner == owner,), defer=(Opportunity.description,))
    for role, (packet, left, copy, day) in batch:
        if role in held:
            report.roles += 1
            await _check_role(session, role, rules, report, cause, packet, left, span, copy, day,
                              stops[role.id] if _read_of(role) == read[role.id] else _JUDGE)
    await session.flush()
    await db.release_if_free(session)
    busy = set(held.skipped)
    return [(role, data) for role, data in batch if role.id in busy]


async def _roles_planned(session: AsyncSession, owner: str, rules: gating.Rules, report: Report, cause: str
                         ) -> list[Opportunity]:
    """A dry run of `_roles`: every role read at once and checked as it was read, with no lock and no write; the roles
    it changed (in memory) are returned, for the employer limit."""
    read = select(Opportunity).where(Opportunity.owner == owner, Opportunity.status.in_(MOVABLE)).options(
        defer(Opportunity.description)).order_by(Opportunity.created_at, Opportunity.id)
    roles = (await session.scalars(read.execution_options(populate_existing=True))).all()
    places = await placement.load(session, owner, roles, placement.Purpose.GATES)
    packets = await _latest_packets(session, [r.id for r in roles])
    lefts = await _left(session, [r.id for r in roles if r.status == stages.GATED_OUT])
    span = scoring.bounds(rules.profile)
    copies = await cleansed.gate_copies_by_role(session, owner)
    days = await freshness.posted_by_role(session, owner, roles)
    moved = []
    for role in roles:
        if places[role.id].held or (role.status == stages.GATED_OUT and (
                role.screened_out_step in KEPT_STEPS or role.screened_out_by == StopGate.LIMIT.value)):
            report.roles_left += 1
            continue
        seen = _seen(role)
        report.roles += 1
        await _check_role(session, role, rules, report, cause, packets.get(role.id), lefts.get(role.id), span,
                          copies.get(role.id), days.get(role.id))
        if _seen(role) != seen:
            moved.append(role)
    return moved


UNREAD: Any = object()  # `one_role`'s mark for a read its caller did not make


async def one_role(session: AsyncSession, owner: str, role: Opportunity, *, cause: str,
                   rules: gating.Rules | None = None, posted: freshness.PostedDay | None = None,
                   standing: placement.Standing | None = None, packet: str | None = UNREAD,
                   left: str | None = UNREAD, copy=UNREAD) -> str | None:
    """The gates again for one role, as the whole re-gate does it: a corrected class changes the pay the analyst's
    work gives and a rule decided from the classes (docs/PRD.md 26, 39.14), and an employer's facts that research has
    just brought change what the employer rules read (39.18). A role no re-run may move is left. `rules` are the
    owner's rules (`gating.rules`), `posted` the role's posted day (`freshness.posted_by_role`) and `standing` where it
    stands for the gates (`placement.load`, `Purpose.GATES`) when the caller has them to hand for several roles: the
    posted day of one role reads every job board's host again, and its standing is a few reads of its own (together
    some milliseconds a role), which a batch reads once. `packet` (`_latest_packets`),
    `left` (`_left`, read for Gated out roles alone) and `copy` (`cleansed.gate_copies_of`) are the same for a batch: a
    value given, None included (the role has none), is used as it is; left out, they are read for this role. Returns the
    line it wrote in the role's history (the role was stopped, returned or its stop changed), or None. Its caller is
    `settle.roles`, which works the ask and the score out first: no other module calls it to settle a role, but
    `age_out.run`, which asks it for the roles a new day has put past the freshness pass (nothing of theirs changed to
    work out)."""
    if role.status not in MOVABLE or (
            role.status == stages.GATED_OUT
            and (role.screened_out_step in KEPT_STEPS or role.screened_out_by == StopGate.LIMIT.value)):
        return None
    standing = standing or await placement.one(session, owner, role, placement.Purpose.GATES)
    if standing.held:
        return None
    rules = rules or await gating.rules(session, owner)
    if packet is UNREAD:
        packet = (await _latest_packets(session, [role.id])).get(role.id)
    if left is UNREAD:
        left = (await _left(session, [role.id])).get(role.id)
    if copy is UNREAD:
        copy = await cleansed.gate_copy_of(session, owner, role.id)
    said = await _check_role(session, role, rules, Report(), cause, packet, left, scoring.bounds(rules.profile), copy,
                             posted if posted is not None else await freshness.posted_of(session, role))
    await session.flush()
    return said


# --- stops a release left judged under an older Hard pass ----------------------------------------------------------


async def stale(session: AsyncSession, owner: str) -> int:
    """How many stops were judged under an older Hard pass than the one in force, or under none, that a re-gate would
    judge again: a posting a gate stopped, and a Gated out role that no re-run is barred from moving. A save of Hard
    pass that changes nothing makes no new version, so without this the log would keep such stops until the owner next
    changed Hard pass. Counts only the rows a re-gate does write the current version to, so the count falls to nothing
    once it has run: the retired title check's own stops, which a re-gate leaves as they were (`KEPT_STEPS`: counted,
    they would make `catch_up` run a whole re-gate at every start), a role the old
    tracker screened out, one the employer limit stopped and a role the owner moved by hand are not in it."""
    hard_pass = await profile_store.newest_version(session, owner, "gates")
    postings = await session.scalar(select(func.count()).select_from(JobPosting).where(
        JobPosting.owner == owner, JobPosting.filtered_at.is_not(None), JobPosting.opportunity_id.is_(None),
        JobPosting.owner_override.is_(False), JobPosting.filtered_by.is_not(None),
        JobPosting.judged_by == judged.GATE, JobPosting.stop_hard_pass.is_distinct_from(hard_pass))) or 0
    candidates = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.status == stages.GATED_OUT,
        or_(Opportunity.screened_out_step.is_(None),
            Opportunity.screened_out_step.not_in([step.value for step in KEPT_STEPS])),
        Opportunity.screened_out_by.is_distinct_from(StopGate.LIMIT.value),
        Opportunity.screened_out_hard_pass.is_distinct_from(hard_pass))
        .options(defer(Opportunity.analysis), defer(Opportunity.description)))).all()
    places = await placement.load(session, owner, candidates, placement.Purpose.GATES)
    return postings + sum(1 for r in candidates if not places[r.id].held)


async def catch_up(session: AsyncSession, owner: str) -> Report | None:
    """Check the whole log and every role again when stops are judged under an older Hard pass than the one in force, or
    under none (`stale`): what a release leaves, since the stops it finds were made before the version was kept, or by
    older gates. Nothing to do (None) when every stop is current. The same re-gate as a save of Hard pass runs, so it
    stops, returns and restates exactly what that would, and every stop it keeps then says it was judged under the Hard
    pass in force. Run at every start of the app and at the start of the night's read (`keep_current.py`).

    **The check is asked once, in a reader's session.** It reads every stop and every Gated out role (quick warm, slow
    on a file the system has not read yet), which takes no writer turn there; where
    `session` holds something (a savepoint, a process step) it is asked in `session` itself."""
    async with off_turn.reading(session) as reader:
        if not await stale(reader, owner):
            return None
    return await run(session, owner, cause="Some stops were judged under an older Hard pass than the one in force")
