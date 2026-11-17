"""What is common to every action of the owner's door (docs/PRD.md path 50.6 to 50.9): the arguments checked, a read
run and rolled back, and a write run as a job.

A write (and a dry run of one) is a row in `js_action_runs`, made and committed before anything is done, so a long
action can be followed with `act.job`. It is done by a task of its own in sessions of its own (never the door's: a
transaction is held only for its own work, and a board-feed read is minutes long). `do` waits up to `WAIT_SECONDS` for
it and answers with the result, else with the job. A real write's work, its Activity line ("Claude (on demand) ...",
actor `claude`, the job id in the detail) and the row's end are one transaction. A dry run's work is rolled back
whatever it did and adds only its row, no Activity line. A second of an action that must not run twice is refused
naming the holder (409) before any row is made, in the writer's turn that makes the row, so two programs cannot both
pass the check; a dry run says it would be refused instead. A restart ends a running row as failed
(`fail_interrupted`).

In the two-program instance the page does not run `do`: it writes the row `pending` and asks the engine (`asked.do`).
The engine takes the row here (`begin`, from the ask `owner_action`): it checks the call in its writer's turn, makes the
row `running`, starts the task and tells the page when the task ends (`_tell_ended`). `do` is what a one-program run
(the tests, `resumebot serve`) calls through the local door of `engine_calls`."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, notice
from app.errors import Busy, Conflict, DomainError, NotFound
from app.jobsearch import actors
from app.jobsearch.models import ActionRun
from app.jobsearch.owner_actions import asked, reading
from app.jobsearch.owner_actions.asked import WAIT_SECONDS  # how long `do` waits for a job before it answers the job
from app.jobsearch.owner_actions.registry import REGISTRY, Action, Args, BadArguments, Ctx, Outcome
from app.jobsearch.work import engine_asks, engine_calls, queue
from app.timeutil import iso_or_none, utc_now
from app.trail import service as trail

log = logging.getLogger(__name__)

#: What a job's row says when the app failed on its own side (the cause is in the log).
APP_FAILED = "the app failed on its side and logged why; this is not a problem with what was sent"

_tasks: set[asyncio.Task] = set()
#: The task of each job this program started, by the job's id: `do` waits on it. Empty for a job another program runs.
_by_job: dict[uuid.UUID, asyncio.Task] = {}


# The read path is `reading.py`'s, which the page imports without the runner. These names stay here for the callers
# that reach them through the runner (the write path below, and the tests).
UnknownAction = reading.UnknownAction
_plain = reading.plain
action_named = reading.action_named
check_args = reading.check_args
listing = reading.listing
read = reading.read
_view = asked.view


# --- writes -----------------------------------------------------------------------------------------------------------


def _said_args(args: Args) -> str:
    return json.dumps(_plain(args.model_dump(mode="json")))[:600]


async def _holder(session: AsyncSession, owner: str, action: Action, args: Args) -> str | None:
    """Who holds what a second run of the action would collide with, in words, or None."""
    if action.one_at_a_time:
        running = await session.scalar(select(ActionRun).where(
            ActionRun.owner == owner, ActionRun.action == action.name, ActionRun.dry_run.is_(False),
            ActionRun.state == "running").order_by(ActionRun.started).limit(1))
        if running is not None:
            return f"{action.name} is running already (job {running.id}, started {iso_or_none(running.started)})"
    if action.conflict is not None:
        return await action.conflict(session, owner, args)
    return None


async def _open(owner: str, action: Action, args: Args, dry_run: bool) -> tuple[uuid.UUID, str | None]:
    """The job's row, committed, after the conflict check made in the same writer's turn (the file's own write lock, so
    a second program waits for it). A real call that collides is refused (409) and leaves no row; a dry run is let
    through and told who holds it."""
    async with db.session_factory()() as session:
        await session.connection()  # begins the transaction: the writer's turn, in which the check and the row are made
        holder = await _holder(session, owner, action, args)
        if holder and not dry_run:
            raise Conflict(holder)
        row = ActionRun(owner=owner, action=action.name, arguments=_plain(args.model_dump(mode="json")),
                        dry_run=dry_run, by=actors.CLAUDE, state="running", started=utc_now())
        session.add(row)
        await session.commit()
        return row.id, holder


async def _progress(job_id: uuid.UUID, line: str) -> None:
    """The job's one progress line, in a short transaction of its own."""
    async with db.session_factory()() as session:
        with queue.exempt("the progress line of a job is read by the screen only"):
            await session.execute(update(ActionRun).where(ActionRun.id == job_id).values(progress=line[:200]))
        await session.commit()


async def _fail(job_id: uuid.UUID, error: str, how_to_fix: str | None = None) -> None:
    async with db.session_factory()() as session:
        row = await session.get(ActionRun, job_id)
        if row is not None and row.state == "running":
            row.state, row.ended = "failed", utc_now()
            row.result = {"error": error, **({"how_to_fix": how_to_fix} if how_to_fix else {})}
            await session.commit()


async def _end_dry(job_id: uuid.UUID, outcome: Outcome) -> None:
    said = f"Nothing was changed (dry run). A real call would {outcome.would}."
    async with db.session_factory()() as session:
        row = await session.get(ActionRun, job_id)
        if row is not None:
            row.state, row.ended = "done", utc_now()
            row.result = _plain({**outcome.result, "said": said, "changed": False})
            await session.commit()


def _tell_ended(owner: str, job_id: uuid.UUID) -> None:
    """The job's row has changed its end (done, failed, or the result of a job that goes on): tell the page, which reads
    the row once and wakes the call that waits for it, and wake the waiters of this process (a one-program run hears its
    own job). A datagram that never comes costs nothing: the waiting call reads the row when its time is out."""
    notice.tell_page(owner, [("action_run", str(job_id))])
    notice.waiters.notify("action_run", [str(job_id)])


@db.background_work
async def _work(owner: str, action: Action, args: Args, dry_run: bool, job_id: uuid.UUID, holder: str | None) -> None:
    """The job's task. It never raises: whatever happens is the row's state. When the row has changed its end the
    page is told, once (`_tell_ended`)."""
    told = False
    try:
        async with db.session_factory()() as session:
            ctx = Ctx(session, owner, dry_run, job_id, progress=lambda line: _progress(job_id, line))
            try:
                outcome = (Outcome({"would_be_refused": holder}, would=f"be refused: {holder}") if holder
                           else await action.run(ctx, args))
            except DomainError as refused:
                if not dry_run:
                    raise
                outcome = Outcome({"would_be_refused": str(refused)}, would=f"be refused: {refused}")
            if dry_run:
                await session.rollback()  # whatever the dry run touched is not kept
                await _end_dry(job_id, outcome)
                return
            sentence = f"{actors.CLAUDE_WHO} {outcome.did}"
            await trail.record(
                session, owner, event=f"claude_{action.name.replace('.', '_')}"[:40], actor=actors.CLAUDE_ACTOR,
                who=actors.CLAUDE_WHO, sentence=sentence, link=outcome.link,
                detail=f"Job {job_id}. Action {action.name}, arguments {_said_args(args)}.")
            row = await session.get(ActionRun, job_id)
            if row is not None:
                row.result = _plain({**outcome.result, "said": outcome.did[:1].upper() + outcome.did[1:] + ".",
                                     "changed": True})
                if not outcome.follow:
                    row.state, row.ended = "done", utc_now()
            await session.commit()
        _tell_ended(owner, job_id)  # the row is saved: the page may read it now, while the wakes below are sent
        told = True
        await _after(outcome.after)
    except asyncio.CancelledError:
        raise
    except DomainError as refused:
        log.info("owner action %s refused: %s", action.name, refused)
        await _fail(job_id, str(refused), getattr(refused, "how_to_fix", None))
    except Exception as exc:
        if db.is_busy(exc):  # nothing was changed: the same call a few seconds later goes through
            await _fail(job_id, str(Busy()), Busy.how_to_fix)
            return
        log.exception("owner action %s failed", action.name)
        await _fail(job_id, APP_FAILED)
    finally:
        if not told:  # a dry run, a refusal, a failure: the row was saved above, so the page reads it now
            _tell_ended(owner, job_id)


async def _after(calls: list[Callable[[], Awaitable[None]]]) -> None:
    """The wake-ups an action left due, sent once its commit has landed. Best effort: a failed wake is logged."""
    for call in calls:
        try:
            await call()
        except Exception:
            log.warning("a wake an action asked for was not sent", exc_info=True)


def _start_task(owner: str, action: Action, args: Args, dry_run: bool, job_id: uuid.UUID, holder: str | None) -> None:
    """Begin the job's task and keep it until it ends. `holder` is who holds what a second run would collide with."""
    task = asyncio.create_task(_work(owner, action, args, dry_run, job_id, holder))
    _tasks.add(task)
    _by_job[job_id] = task

    def ended(finished: asyncio.Task) -> None:
        _tasks.discard(finished)
        _by_job.pop(job_id, None)

    task.add_done_callback(ended)


def _launch(work: tuple[str, Action, Args, bool, uuid.UUID, str | None]) -> None:
    """The task itself: `engine_calls.start("owner_action", work)` runs this in the program that does the work.
    `work` is (the owner, the action, its arguments, whether it is a dry run, the job's id, who holds what a second
    run would collide with)."""
    _start_task(*work)


engine_calls.register("owner_action", _launch)


async def begin(owner: str, job_id: uuid.UUID) -> dict[str, Any]:
    """The engine takes a call the page wrote as a `pending` row (`asked.do`). In one writer's turn it reads the row,
    checks the call as `do` does (the action, its arguments, who holds what it would collide with), and then either
    deletes the row and answers `{"refused": {"status", "reason", "how_to_fix"}}` (a real call that collides, an
    unknown action, bad arguments: no row is kept, as in a one-program run), or makes the row `running`, starts the
    job's task and answers `{"started": true}`. It answers `{"started": false}` for a row that is not there, is the
    owner's no more, or is not `pending` (another look took it): a second call starts nothing. It holds no turn while
    the job runs."""
    from app.jobsearch.owner_actions import onboarding, reads, resume_help, writes  # noqa: F401  (registers every action)

    async with db.session_factory()() as session:
        await session.connection()  # begins the transaction: the writer's turn, where the check and the claim are made
        row = await session.get(ActionRun, job_id)
        if row is None or row.owner != owner or row.state != "pending":
            return {"started": False}
        try:
            action = action_named(row.action, writes=True)
            args = check_args(action, row.arguments)
            holder = await _holder(session, owner, action, args)
            if holder and not row.dry_run:
                raise Conflict(holder)
        except DomainError as refused:
            await session.delete(row)
            await session.commit()
            return {"refused": {"status": refused.status, "reason": str(refused),
                                "how_to_fix": getattr(refused, "how_to_fix", None)}}
        row.state, row.started = "running", utc_now()
        dry_run = row.dry_run
        await session.commit()
    _start_task(owner, action, args, dry_run, job_id, holder)
    return {"started": True}


async def _ask_begin(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """The ask `owner_action`: `args` is `{"job": <the row's id>}`. The session is not used (`begin` makes its own)."""
    try:
        job_id = uuid.UUID(str(args.get("job")))
    except ValueError:
        return {"started": False}
    return await begin(owner, job_id)


async def _ask_follow(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """The ask `owner_action.follow`: bring a running job that follows what it started (a mission's run) up to date, in
    the ask's own session (the engine commits it with the answer). `{"followed": false}` for any other job."""
    try:
        job_id = uuid.UUID(str(args.get("job")))
    except ValueError:
        return {"followed": False}
    row = await session.get(ActionRun, job_id)
    action = REGISTRY.get(row.action) if row is not None else None
    if row is None or row.owner != owner or row.state != "running" or action is None or action.follow is None:
        return {"followed": False}
    await action.follow(session, row)
    return {"followed": True}


engine_asks.register(asked.ASK_DO, _ask_begin)
engine_asks.register(asked.ASK_FOLLOW, _ask_follow)


async def do(owner: str, name: str, raw: dict[str, Any] | None, dry_run: bool = False) -> dict[str, Any]:
    """Run a write action (or a dry run of it) as a job: refuse it when it collides, make its row, start its task, and
    wait up to `WAIT_SECONDS` for it. Answers the result, or `{job, state: running}` when it is not done by then."""
    action = action_named(name, writes=True)
    args = check_args(action, raw)
    job_id, holder = await _open(owner, action, args, dry_run)
    engine_calls.start("owner_action", (owner, action, args, dry_run, job_id, holder))
    task = _by_job.get(job_id)  # None when another program runs the job: its row says how it goes
    done: set[asyncio.Task] = set()
    if task is not None:
        done, _ = await asyncio.wait({task}, timeout=WAIT_SECONDS)
    async with db.session_factory()() as session:
        row = await session.get(ActionRun, job_id)
        assert row is not None
        answer = _view(row)
    if not done and row.state == "running":
        answer["said"] = f"{name} is not done yet; it goes on. Ask act.job with this job's id."
    return answer


async def settle() -> None:
    """Wait for every job's task to end (the tests, and a clean stop)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def stop_all() -> None:
    """At shutdown: cancel the jobs in flight. Their rows are ended as failed by the start that follows."""
    for task in list(_tasks):
        task.cancel()
    await settle()


# --- act.job ----------------------------------------------------------------------------------------------------------


async def job(session: AsyncSession, owner: str, job_id: str | None) -> dict[str, Any]:
    """One job (and, for an action that follows what it started, brought up to date first), or with no id the last
    `JOBS_LISTED` jobs."""
    if not job_id:
        return await asked.listed(session, owner)
    try:
        row = await session.get(ActionRun, uuid.UUID(job_id))
    except ValueError:
        raise BadArguments("a job id is the id a write answered with") from None
    if row is None or row.owner != owner:
        raise NotFound("no such job")
    action = REGISTRY.get(row.action)
    if row.state == "running" and action is not None and action.follow is not None:
        await action.follow(session, row)
    return _view(row)


async def fail_interrupted(session: AsyncSession, owner: str) -> int:
    """At the start: a job still marked running was cut short by the restart (its task lived in the app's process). It
    ends as failed, with why, as a board read does (`missions.settle_after_restart`). Returns how many."""
    rows = (await session.scalars(select(ActionRun).where(
        ActionRun.owner == owner, ActionRun.state == "running"))).all()
    for row in rows:
        row.state, row.ended = "failed", utc_now()
        row.result = {"error": "the app restarted while this was running", "how_to_fix":
                      "look at what it changed (Activity, act.read) before asking it again"}
    await session.flush()
    return len(rows)
