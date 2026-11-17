"""Read the employers' boards (docs/PRD.md 28.4 to 28.10), the app's own work before the search, with no Claude: new
links resolved to job systems, every due board read into the posting index, the new postings filtered in code, and
the descriptions read for the candidates whose list left them out. A posting the filter lets through becomes a role at
once (`sift.py`, `pipeline/from_posting.py`); there is no title pass to wait for.

It runs as a background task beside the scheduler and writes its outcome onto the mission's run when it ends; a read
that never ends (the app restarted) is closed by the mission's refresh.

**Each step is a transaction of its own.** A read is long (a quarter of an hour on a quiet night, hours on a big one),
and a transaction that long holds every row it wrote until its end, so anything else that touches those rows waits for
the whole read and may fail. So the read is driven here, not run by the engine in one session: the
engine (`process.stepped_engine`) stops at every step, this driver runs the step's action in a session of its own, hands
the outcome to the engine (`complete`, which moves the process to the next step) and commits, so a step's rows are held
for that step and no longer. The two longest loops name their safe points as they go (`checkpoint`: a board read, a
slice of descriptions), and the same commit is made there. The layer that commits is this driver, never the services:
they flush, as ever.

What a read that stops part-way leaves is whole and is taken up by the next read: every step is the night's work to do
again and does only what is left (a posting not yet filtered is filtered, one without its text is read, a board is read
whole every night), a merge is one unit inside its step, and a screened-out posting keeps its reason on its row. A step
that raised is rolled back, the steps before it stay, and the process is marked failed; a read cut short by a restart
leaves its process open, and the start-up (`end_unfinished`) and the next read close it.

Boards are read to their last page, so a big night can be long; the read has a time limit instead (`LONGEST_MINUTES`,
docs/PRD.md 28.17). It gives every fetcher it makes the same deadline (`polite.READ_BY`), and the loops that read pages,
boards and descriptions look at it between requests: past it nothing new is begun, the board being read keeps the pages
it has and is marked cut short (it closes nothing), and the run ends done with what was read, the rest left for the next
read."""

import asyncio
import contextlib
import copy
import logging
import time
import uuid
from datetime import UTC, datetime

from process_engine.engine import Engine
from process_engine.errors import ActionError
from process_engine.models import RUNNING, WAITING, ProcInstance
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import process
from app.jobsearch.fetch import polite
from app.jobsearch.missions import record
from app.jobsearch.models import Mission, MissionRun
from app.jobsearch.pipeline.quiet import report_lines
from app.jobsearch.postings import read_progress
from app.jobsearch.work import checkpoint, engine_calls, stuck

log = logging.getLogger(__name__)

#: The longest a read goes on reading: past it the read stops, cleanly, as above.
LONGEST_MINUTES = 180
#: What the board feed's part of the read did, as the night's report carries it (`read_night` of the board's reader).
BOARD_FEED_COUNTS = ("requests", "backoffs", "searches", "searches_read", "skipped", "unread", "stopped", "mode",
                     "details", "lists_read", "lists_missed")
#: What the read is given after that to finish what it was doing (the requests in flight, the filter). A run still going
#: after both is taken for lost, and the mission's refresh closes it as failed.
GRACE_MINUTES = 30
LOST_AFTER_MINUTES = LONGEST_MINUTES + GRACE_MINUTES
#: A read the app's restart cut short carries on if it began no longer ago than this (the next night's read is due
#: then); an older one is closed as failed.
RESUME_HOURS = 20

_tasks: set[asyncio.Task] = set()


def _counted(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _span(minutes: int) -> str:
    """A stretch of time in words: "3 hours", "90 minutes"."""
    if minutes % 60:
        return f"{minutes} minutes"
    return _counted(minutes // 60, "hour", "hours")


def summary_words(read: dict, sifted: dict, details: dict | None = None, quiet: dict | None = None) -> str:
    """The run's account, in job-search words: the boards read and what is new on them, what the filter let through
    (each becomes a role), then what it stopped and why, a line for each with its count first.
    The machine's own tallies (every posting seen, the closed ones, reads that failed, descriptions fetched) are in the
    run's numbers, not here. A read whose time ran out says what was left: "Read 41 of 63 employer boards: ...", and
    "The read ran out of time (3 hours): 22 employer boards not read, 3 read in part, 1,204 descriptions not read.
    They wait for the next read." (`read` and `details` are the steps' own answers, `unread`, `cut` and `left`).
    The Applied roles that heard nothing back and moved to Gone Quiet are one line of their own, before the stops
    (`quiet` is the step's answer; the wording is `quiet.report_lines`, and nothing is said of none or of a blank
    number)."""
    employers, unread = int(read.get("employers") or 0), int(read.get("unread") or 0)
    boards = _counted(employers, "employer board", "employer boards")
    lines = [
        f"Read {f'{employers - unread:,} of {boards}' if unread else boards}: "
        f"{_counted(read.get('new', 0), 'new posting', 'new postings')}.",
        f"{sifted.get('candidates', 0):,} passed the filter.",
    ]
    cut, left = int(read.get("cut") or 0), int((details or {}).get("left") or 0)
    short = [f"{_counted(unread, 'employer board', 'employer boards')} not read"] if unread else []
    short += [f"{cut:,} read in part"] if cut else []
    short += [f"{_counted(left, 'description', 'descriptions')} not read"] if left else []
    if short:
        lines.append(f"The read ran out of time ({_span(LONGEST_MINUTES)}): {', '.join(short)}. "
                     "They wait for the next read.")
    lines += report_lines(quiet)
    stopped = sorted((sifted.get("stopped") or {}).items(), key=lambda kv: -kv[1])
    if stopped:
        lines.append("Stopped:")
        lines += [f"{n:,} {why}" for why, n in stopped]
    return "\n".join(lines)


def _subject(owner: str) -> str:
    return f"jobsearch-read:{owner}"


async def being_read(session: AsyncSession, owner: str) -> bool:
    """Whether the night's read of the employers' boards is still going: postings it has not filtered yet are still to
    become roles."""
    return await session.scalar(
        select(MissionRun.id).join(Mission, Mission.id == MissionRun.mission_id)
        .where(Mission.owner == owner, Mission.key == record.READ, MissionRun.outcome == "running").limit(1)
    ) is not None


board_feed_of = record.board_feed_of  # now in missions/record.py (the page's owner door reads it)


async def end_unfinished(session: AsyncSession, owner: str, why: str) -> int:
    """Close the process of a read that never ended (the app restarted in the middle of it, or the task was cancelled):
    its steps were committed one by one, so it is left open at the step it was at, and nothing will go on with it. Never
    called while a read of the owner's is going (one at a time: `missions.refuse_a_second`). Flushes; the caller
    commits. Returns how many were closed."""
    open_ones = (await session.scalars(select(ProcInstance.id).where(
        ProcInstance.process_key == process.READ_BOARDS, ProcInstance.subject == _subject(owner),
        ProcInstance.status.in_((RUNNING, WAITING))))).all()
    for instance_id in open_ones:
        await stuck.cancel_instance(session, instance_id, why, "the night's read")
    await session.flush()
    return len(open_ones)


async def _run_step(engine: Engine, instance_id: uuid.UUID) -> bool:
    """Do the step the read's process waits at, in a session of its own, and commit it with the process's move to the
    next step. False when the process has ended (done, failed or cancelled) and there is none. The step's action is
    given what any action is (the session, the instance, a copy of its data) and may name safe points
    (`checkpoint.reached`), where this session is committed too. An action's named error goes to the engine as one; any
    other failure rolls the step back, marks the process failed (in a session of its own, so it holds) and is raised."""
    step = None
    try:
        async with db.read_session_factory()() as reader:  # where the process waits: no writer's turn for the look
            where = await engine.where_is(reader, instance_id)
            if where["status"] not in (RUNNING, WAITING):
                return False
            waits = [w for w in where["waiting_for"] if w["kind"] == "deferred"]
            if not waits:
                raise RuntimeError(f"the read waits at '{where['step']}', which no code does")
            step = waits[0]["step"]
            action = engine.registry.get(waits[0]["payload"]["action"])
            instance = await reader.get(ProcInstance, instance_id)
            if action is None or instance is None:  # the engine only waits at an action that is registered
                raise RuntimeError(f"the read's step '{step}' has no action to run")
            data = copy.deepcopy(instance.data or {})
        async with db.session_factory()() as session:  # untouched until the step's first statement: that takes the turn
            try:
                async with checkpoint.saving_as_it_goes(session):
                    result = await action.fn(session, instance, data)
            except ActionError as error:
                await session.rollback()
                await engine.complete(session, instance_id, step, error=error.code, error_message=error.message)
            else:
                await engine.complete(session, instance_id, step, result)
            await session.commit()
            return True
    except Exception as exc:
        if step is not None:
            await _fail(engine, instance_id, step, exc)
        raise


async def _fail(engine: Engine, instance_id: uuid.UUID, step: str, exc: Exception) -> None:
    """Mark the read's process failed at the step that raised, with why. Never raises: the failure that is being
    reported matters more than the record of it."""
    try:
        async with db.session_factory()() as session:
            await engine.complete(session, instance_id, step, error="step_failed",
                                  error_message=f"{type(exc).__name__}: {exc}"[:300])
            await session.commit()
    except Exception:
        log.exception("couldn't mark the read's process failed")


async def _open_instance(session: AsyncSession, owner: str) -> uuid.UUID | None:
    return await session.scalar(select(ProcInstance.id).where(
        ProcInstance.process_key == process.READ_BOARDS, ProcInstance.subject == _subject(owner),
        ProcInstance.status.in_((RUNNING, WAITING))).order_by(ProcInstance.created_at.desc()).limit(1))


async def read_boards(owner: str, details_per_night: int, resume: bool = False) -> tuple[str, str, dict]:
    """One night's read, run as its playbook (playbooks/jobsearch/read_boards.bpmn), so every step shows in the
    process view. Returns (outcome, summary, counts). It has `LONGEST_MINUTES` to read in (see the module's docstring):
    when they run out it ends "done" with what it read. Each step is committed when it ends (see the module's
    docstring); a step that raises ends the read by raising, with the steps before it kept. `resume`: the app restarted
    in the middle of the read, so it takes up the process it left open at its step instead of beginning another (the
    sites and texts it had done are on its run: `read_progress.py`); with no process open it begins afresh."""
    token = polite.READ_BY.set(time.monotonic() + LONGEST_MINUTES * 60)  # every fetcher the steps make takes it
    try:
        engine = process.stepped_engine()
        async with db.session_factory()() as session:
            instance_id = await _open_instance(session, owner) if resume else None
            if instance_id is None:
                await end_unfinished(session, owner, "a later read began")
                await read_progress.reset(session)
                instance = await engine.start(
                    session, process.READ_BOARDS, {"owner": owner, "details_per_night": details_per_night},
                    subject=_subject(owner))
                await session.commit()
                instance_id = instance.id
        while await _run_step(engine, instance_id):
            pass
        async with db.session_factory()() as session:
            instance = await session.get(ProcInstance, instance_id)
            if instance is None:
                raise RuntimeError("the read's process is gone")
            data, status, error = dict(instance.data or {}), instance.status, instance.error
    finally:
        polite.READ_BY.reset(token)
    read, sifted, details = data.get("read") or {}, data.get("sifted") or {}, data.get("details") or {}
    filled, warn = int(details.get("filled") or 0), data.get("notices") or {}
    # What the step that moves roles to Gone Quiet answered: nothing when the read failed before it.
    quiet = data.get("quiet")
    went = quiet or {}
    fed = data.get("board_feed") or {}  # the board feed's postings join the same index: they count among the boards'
    read = {**read, "seen": read.get("seen", 0) + int(fed.get("seen") or 0),
            "new": read.get("new", 0) + int(fed.get("new") or 0)}
    counts = {**{k: read.get(k, 0) for k in ("employers", "failed", "seen", "new", "closed", "reopened", "unread",
                                             "cut")},
              "candidates": sifted.get("candidates", 0), "filtered_out": sifted.get("stopped") or {},
              "routes": sifted.get("routes") or {},
              "details": filled, "details_left": int(details.get("left") or 0),
              "faults": read.get("faults") or [], "layoff_notices": warn,
              "followed": int((data.get("followed") or {}).get("followed") or 0),
              "posting_went": int(went.get("went") or 0), "no_reply": int(went.get("silent") or 0),
              "no_reply_days": went.get("days"),
              "board_feed": {key: fed.get(key) for key in BOARD_FEED_COUNTS},
              "instances": [str(instance_id)]}
    # Roles move with no read at all (no Claude, no fetching), so they can have moved on a night whose read failed after
    # the step or found no site to read: both exits say so on a line after their own sentence. The cut to 500 characters
    # is for the failure's sentence alone, so the line is never cut off.
    after = "".join(f"\n{line}" for line in report_lines(quiet))
    if status == "failed":
        return "failed", f"The read failed: {error}"[:500] + after, counts
    if not read.get("employers"):
        nothing = "No switched-on site can be read yet: switch one on, or wait while its job system is found."
        return "done", nothing + after, counts
    words = summary_words(read, sifted, details, quiet)
    if warn.get("added"):
        notices = _counted(warn["added"], "new layoff notice", "new layoff notices")
        words += f"\n{notices} at {_counted(warn['employers'], 'employer', 'employers')}."
    tried = int(read.get("employers") or 0) - int(read.get("unread") or 0)  # the boards the read began
    return ("failed" if tried and read.get("failed") == tried else "done"), words, counts


@db.background_work  # the background limits, though a request started it (`db.background`)
async def _finish(entry_id: uuid.UUID, owner: str, details_per_night: int, resume: bool = False) -> None:
    """The background task: read, then write the outcome on the mission's run. It is launched once that run is saved
    (`missions.start`); a run that isn't there (its transaction was rolled back) is no read."""
    async with db.session_factory()() as session:
        entry = await session.get(MissionRun, entry_id)
    if entry is None or entry.outcome != "running":
        return
    read_progress.READ_RUN.set(entry_id)  # this task's own context: the steps note their progress on the run
    try:
        outcome, summary, counts = await read_boards(owner, details_per_night, resume)
    except Exception as exc:  # the run must end with a reason, whatever went wrong
        log.exception("reading the boards failed")
        outcome, summary, counts = "failed", f"The read failed: {exc}"[:500], {}
    async with db.session_factory()() as session:
        entry = await session.get(MissionRun, entry_id)
        if entry is not None and entry.outcome == "running":
            entry.outcome, entry.summary, entry.ended_at = outcome, summary, datetime.now(UTC)
            entry.started = {**(entry.started or {}), "reading": False, **counts}
            await session.commit()


def _launch(key: tuple[uuid.UUID, str, int, bool]) -> None:
    """The task itself: `engine_calls.start("boards_night", key)` runs this in the program that does the work. `key` is
    (the run's id, the owner, the descriptions a night, whether it carries on a cut-short read)."""
    entry_id, owner, details_per_night, resume = key
    task = asyncio.get_running_loop().create_task(_finish(entry_id, owner, details_per_night, resume))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


engine_calls.register("boards_night", _launch)


def start_in_background(entry_id: uuid.UUID, owner: str, details_per_night: int, resume: bool = False) -> None:
    """Start the read of a saved run: asked of the engine door by name, so no request path makes the task itself."""
    engine_calls.start("boards_night", (entry_id, owner, details_per_night, resume))


async def stop_all() -> None:
    """At shutdown: cancel the reads in flight (their runs are closed by the refresh after the restart)."""
    for task in list(_tasks):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
