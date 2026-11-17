"""When the scout fails, and when the owner ends a project: the two things the round's file leaves to code.

Written for this app. What is here:

- `end_project` / `stop_for_good`: the owner chose Stop, so the project ends with a resolution line. Every open task is
  kept and skipped, open plans are set aside, the project's run is set aside (`round_process.drive_ended`).
- `planner_run_failed` / `retry_planner_runs`: a planning task the scout held past `PLAN_TASK_STUCK_MINUTES` without
  finishing is a failed run. The drive pass hands it to the round's run (`round_process.drive_planner_failed`, step
  PR-18), which calls `planner_run_failed`: the first failure hands the task back at once, the second after 15 minutes,
  the third asks the owner (`asks.ask_owner`, card `stuck`: Try again, Pause or Stop).

The owner's answer to the stuck ask (Try again, Pause, Stop) is carried out by `inbox_asks` (`site_stuck` Inbox task).
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import jsonsql
from app.sites import resolution, round_process
from app.sites.asks import STUCK, ask_owner, settle_project_asks
from app.sites.base import OPEN_STATUSES, PlanEngineError
from app.sites.models import PlanTask, PlanTaskMode, PlanTaskStatus, Project
from app.sites.records import _record_activity, supersede_open_plans
from app.textutil import one_line
from app.timeutil import iso_utc

#: A planning task the scout has held this long without finishing is a failed run.
PLAN_TASK_STUCK_MINUTES = 30
PLANNER_RETRY_WAIT = timedelta(minutes=15)
PLANNER_RETRIES = 2
STUCK_RETRY, STUCK_PAUSE, STUCK_STOP = "retry", "pause", "stop"
OWNER_STOP_REASON = "Stopped by you."


async def end_project(
    session: AsyncSession, project: Project, reason: str, activity: tuple[str, str, str] | None = None,
    *, ending: str | None = None, closes_asks: str = "The project ended.",
) -> None:
    """The owner chose Stop: it ends here. Every open task is skipped (kept, never deleted), the project is finished
    with the reason, and nothing more is handed out or woken. `activity` is `(who, kind, text)` for the trail line
    when it wasn't the owner that ended it; its text is how it began ("This project ended."), and the project's
    outcome follows it on the same line. No agent closes a project that ends this way, so the app writes its
    resolution here from the records. Every question it put to the owner that is still open is closed with `closes_asks`
    as its note."""
    if project.finished_at is not None:
        return
    await round_process.drive_ended(session, project)  # the project's run is set aside
    for row in (await session.scalars(
        select(PlanTask).where(PlanTask.project_id == project.id, PlanTask.status.in_(OPEN_STATUSES))
    )).all():
        row.status = PlanTaskStatus.skipped
        row.checked_out_by = None
        row.checked_out_at = None
    project.finished_at = datetime.now(UTC)
    project.finish_summary = reason
    closed = await resolution.close(session, project, ending or resolution.STOPPED, reason)
    who, kind, lead = activity or ("you", "stopped", "You stopped this project.")
    _record_activity(session, project, who, kind, f"{lead} {closed['outcome']}")
    await supersede_open_plans(session, project)
    await settle_project_asks(session, project, closes_asks)
    await session.flush()


async def stop_for_good(session: AsyncSession, project: Project) -> None:
    """The owner's Stop: the project ends, here, with a resolution line (ending `stopped`: "Stopped. Still open: ... It
    is with you now."), unlike Pause, which only holds it. Every open task is kept and skipped and open plans are set
    aside. Refused for a project that is finished."""
    if project.finished_at is not None:
        raise PlanEngineError("this project is already finished")
    await end_project(
        session, project, OWNER_STOP_REASON,
        activity=("you", "stopped", "You stopped this project."), ending=resolution.STOPPED,
    )


async def planner_run_failed(
    session: AsyncSession, project: Project, task: PlanTask, why: str, now: datetime | None = None,
) -> tuple[str, str] | None:
    """A planner run on `task` failed (its session ended without finishing, or it held the task past
    `PLAN_TASK_STUCK_MINUTES`). The app retries; the owner isn't told until the third failure: the first hands the task
    back at once, the second after 15 minutes, the third asks the owner with what was tried. Returns `(reason, mode)` to
    wake now, or None."""
    now = now or datetime.now(UTC)
    state = dict((task.data or {}).get("planner_runs") or {})
    failed = int(state.get("failed") or 0) + 1
    state["failed"] = failed
    state["tried"] = [*(state.get("tried") or []), f"{now.strftime('%Y-%m-%d %H:%M')} UTC: {one_line(why, 160)}"][-5:]
    state.pop("retry_at", None)
    task.data = {**(task.data or {}), "planner_runs": state}
    task.checked_out_by = None
    task.checked_out_at = None
    if failed <= PLANNER_RETRIES:
        if failed == 1:
            task.status = PlanTaskStatus.todo
        else:
            state["retry_at"] = iso_utc(now + PLANNER_RETRY_WAIT)
            task.data = {**task.data, "planner_runs": state}
            task.status = PlanTaskStatus.waiting  # no card: it comes back by itself
        _record_activity(
            session, project, "app", "planner_retry",
            f"The scout didn't finish “{one_line(task.title, 120)}”; resumebot will try again"
            + ("." if failed == 1 else " in 15 minutes."), task_id=task.id,
        )
        await session.flush()
        return ("planner_retry", "plan") if failed == 1 else None
    tried = [*state["tried"]]
    question = f"I couldn't finish “{one_line(task.title, 120)}” after {failed} tries."
    task.data = {**task.data, "escalation": {
        "question": question, "checked": tried, "to": "owner", "stuck": True, "reason": "stuck",
        "options": [
            {"id": STUCK_RETRY, "label": "Try again", "description": "resumebot hands it to the scout once more."},
            {"id": STUCK_PAUSE, "label": "Pause", "description": "Nothing more happens until you resume the project."},
            {"id": STUCK_STOP, "label": "Stop", "description": "resumebot ends the project here."},
        ],
        "recommended": STUCK_RETRY, "asked_at": iso_utc(now),
    }}
    task.status = PlanTaskStatus.waiting
    _record_activity(session, project, "app", "escalated", question, task_id=task.id)
    await session.flush()
    await ask_owner(
        session, project, card=STUCK, key=f"stuck-{task.id}", title=question, detail="; ".join(tried),
        options=task.data["escalation"]["options"], recommended=STUCK_RETRY, task=task,
    )
    return None


async def retry_planner_runs(
    session: AsyncSession, now: datetime | None = None,
) -> list[tuple[Project, str, str]]:
    """The drive pass's planner upkeep: a planning task held past `PLAN_TASK_STUCK_MINUTES` is a failed run (the round's
    run decides the planner is asked again and does it: `planner_run_failed`), and one waiting out its 15 minutes goes
    back to todo. Returns the `(project, reason, mode)` wakes to send after the commit. Idempotent."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=PLAN_TASK_STUCK_MINUTES)
    wakes: list[tuple[Project, str, str]] = []
    held = (await session.scalars(
        select(PlanTask).where(
            PlanTask.mode == PlanTaskMode.plan, PlanTask.status == PlanTaskStatus.in_progress,
            PlanTask.checked_out_at.isnot(None), PlanTask.checked_out_at < cutoff, PlanTask.superseded.is_(False),
        )
    )).all()
    for task in held:
        project = await session.get(Project, task.project_id)
        if project is None or project.stopped_at is not None or project.finished_at is not None:
            continue
        why = f"no answer after {PLAN_TASK_STUCK_MINUTES} minutes"
        driven, woken = await round_process.drive_planner_failed(session, project, task, why, now)
        if not driven:
            woken = await planner_run_failed(session, project, task, why, now)
        if woken is not None:
            wakes.append((project, *woken))
    waiting = (await session.scalars(
        select(PlanTask).where(
            PlanTask.mode == PlanTaskMode.plan, PlanTask.status == PlanTaskStatus.waiting,
            PlanTask.superseded.is_(False), jsonsql.has(PlanTask.data, "planner_runs"),
        )
    )).all()
    for task in waiting:
        retry_at = ((task.data or {}).get("planner_runs") or {}).get("retry_at")
        if not retry_at or datetime.fromisoformat(retry_at) > now:
            continue
        project = await session.get(Project, task.project_id)
        if project is None or project.stopped_at is not None or project.finished_at is not None:
            continue
        runs = {k: v for k, v in task.data["planner_runs"].items() if k != "retry_at"}
        task.data = {**task.data, "planner_runs": runs}
        task.status = PlanTaskStatus.todo
        wakes.append((project, "planner_retry", "plan"))
    if held or waiting:
        await session.flush()
    return wakes
