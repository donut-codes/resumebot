"""Completing tasks and moving a project on: the next task, resume, stop and reorder.

Copied from another project's code (app/PROVENANCE.md). Not here: goal setup's first looks (`holds`, `couldnt`),
the Teacher's rewrite of a skill nobody could run (`equip.loop_back`), tool versions and size disputes on a result's
cap, the connection's role set (the scout takes every step), the approver and a goal run's place in the list. An item
that ends goes to the round's run (`round_process.drive_item_ended`), which reads how it ended and decides.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.harness.kinds import result_problems
from app.harness.loader import Harness
from app.jobsearch.work import queue
from app.models.agent import AgentConnection
from app.sites import round_process
from app.sites.base import _VALID_OUTCOMES, OPEN_STATUSES, PLAN_ITEM_DATA_MAX_BYTES, PlanEngineError
from app.sites.check_flow import advance_plan_checks
from app.sites.checkout import checkout_next_task
from app.sites.connections import _content_or_none
from app.sites.loop import _create_next_plan_task
from app.sites.models import (
    Plan,
    PlanItemAction,
    PlanStatus,
    PlanTask,
    PlanTaskMode,
    PlanTaskStatus,
    Project,
)
from app.sites.records import (
    _REVIEW_WORK_ACTIONS,
    _next_version,
    _open_review_work_exists,
    _plan_by_version,
    _record_activity,
    report_for_version,
)
from app.textutil import squash


async def next_task_or_reason(
    session: AsyncSession, project: Project, connection_id: uuid.UUID,
) -> tuple[PlanTask | None, str | None]:
    """`checkout_next_task`, plus *why* there's nothing to hand out -- None (a task was checked out) | "stopped" |
    "finished" | "awaiting_approval" (no approved plan yet) | "paused" (the connection is paused) | "none_left". Makes
    the checks itself when the last do-task has been tried (the one place in this module that does: the same call
    task.next and task.complete's own `next` reply both go through, so "checks made exactly once, the moment the last
    do-task is tried" holds for either door)."""
    conn = await session.get(AgentConnection, connection_id)
    if conn is not None and conn.paused:
        # Agents page Pause: task.next hands a paused connection nothing at all -- not even a task it already holds --
        # rather than mutate anything.
        return None, "paused"
    task = await checkout_next_task(session, project, connection_id)
    if task is not None:
        return task, None
    if project.stopped_at is not None:
        return None, "stopped"
    if project.finished_at is not None:
        return None, "finished"
    approved = await session.scalar(
        select(Plan).where(Plan.project_id == project.id, Plan.status == PlanStatus.approved)
    )
    if approved is None:
        return None, "awaiting_approval"
    open_do = await session.scalar(
        select(PlanTask.id).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == approved.id,
            PlanTask.action == PlanItemAction.do, PlanTask.status.in_(OPEN_STATUSES),
            PlanTask.superseded.is_(False),
        ).limit(1)
    )
    if open_do is not None:
        # Every remaining do-task is currently checked out by another connection -- nothing free for this one.
        return None, "none_left"
    if approved.validation is None:
        return None, "none_left"  # an old plan with no Check section: the app runs no checks for it
    # The app makes the checks and runs each one (code), writes the report, and starts Act (advance_plan_checks).
    _made, reason = await advance_plan_checks(session, project, approved)
    if reason is not None:
        return None, reason  # "act_ready" / "plan_ready" / "finished": a fresh session takes the next step
    if await _open_review_work_exists(session, project.id, approved.id):
        return None, "none_left"
    if await _open_post_report_task(session, project.id, approved.id) is not None:
        return None, "review_in_progress"
    return None, "none_left"


_POST_REPORT_ORDER = (PlanItemAction.act, PlanItemAction.plan)


async def _open_post_report_task(
    session: AsyncSession, project_id: uuid.UUID, plan_id: uuid.UUID,
) -> PlanTask | None:
    """The first open (todo or held) Act/next-Plan task of this version -- what the scout has to do next once the
    review report is written."""
    for action in _POST_REPORT_ORDER:
        task = await session.scalar(
            select(PlanTask).where(
                PlanTask.project_id == project_id, PlanTask.plan_id == plan_id, PlanTask.action == action,
                PlanTask.status.in_(OPEN_STATUSES), PlanTask.superseded.is_(False),
            ).order_by(PlanTask.position, PlanTask.created_at).limit(1)
        )
        if task is not None:
            return task
    return None


#: A task's `result` is data, never a document: the same byte cap as an item's `data`.
RESULT_MAX_BYTES = PLAN_ITEM_DATA_MAX_BYTES


def _checked_result(
    content: Harness | None, kind_id: str | None, result: Any, *, done: bool, max_bytes: int = RESULT_MAX_BYTES,
) -> dict | None:
    """The `result` to store for a task of `kind_id`, or None when the kind declares none (then whatever was sent is
    ignored). Refuses a result its kind's `result` fields don't accept, in plain words naming the fields; a done task
    must carry every required one."""
    if not kind_id or content is None:
        return None
    spec = content.kinds.get(kind_id)
    if spec is None or not spec.result:
        return None
    problems = result_problems(spec, result, done=done)
    if problems:
        raise PlanEngineError("; ".join(problems))
    if not result:
        return None
    try:
        size = len(json.dumps(result).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise PlanEngineError("`result` isn't JSON-serializable") from exc
    if size > max_bytes:
        raise PlanEngineError(f"`result` is larger than the {max_bytes}-byte cap; keep it to the fields")
    return dict(result)


#: What an off-plan look-only step carries: a short list of what the scout tried.
TRIED_MAX_ITEMS = 8
TRIED_ITEM_MAX_CHARS = 200
TRIED_MISSING_WORDS = (
    "this step only looks, so before it goes off plan, say what you tried: add `tried`, a short list of what you "
    "did to meet its intent (another way, a page, an earlier result, a retry). A look can usually be cleared "
    "inside the step"
)


def clean_tried(tried: Any) -> list[str]:
    """What the scout said it tried, as short plain lines (at most `TRIED_MAX_ITEMS`, each cut to
    `TRIED_ITEM_MAX_CHARS`). A single line is accepted as a list of one. Anything that isn't text is dropped."""
    if isinstance(tried, str):
        tried = [tried]
    if not isinstance(tried, list):
        return []
    out: list[str] = []
    for line in tried:
        text = squash(line)[:TRIED_ITEM_MAX_CHARS] if isinstance(line, str) else ""
        if text and text not in out:
            out.append(text)
    return out[:TRIED_MAX_ITEMS]


def item_only_looks(kind_id: str | None, content: Harness | None) -> bool:
    """Whether the item's kind only reads (a read-only kind): it carries `tried` when it goes off plan."""
    spec = content.kinds.get(kind_id or "") if content is not None else None
    return spec is not None and spec.risk == "read_only"


async def complete_task(
    session: AsyncSession, task: PlanTask, project: Project, outcome: str, reason: str | None = None,
    evidence: str | None = None, summary: str | None = None, result: dict | None = None,
    tried: list[str] | None = None, require_tried: bool = False,
) -> PlanTask:
    """A do-task's own completion (a check ends with `check_flow.complete_plan_check`, Act with `complete_act`, a plan
    with `propose_plan`/`revise_plan`). `outcome` is "done" or "off_plan" ("failure is reductive": going off plan is
    new information, recorded, and the run moves on -- nothing here pauses the project). An off-plan outcome's
    `reason` becomes the `off_plan_reason`.

    `result`: what the task found as data, checked against its kind's `result` fields; a done task of a kind with
    required result fields must carry them.

    `tried`: what a look-only step's scout tried before it gave up. With `require_tried` (the agent doors) an off-plan
    answer on a look-only step with none is refused, saying what to add."""
    if task.status != PlanTaskStatus.in_progress:
        raise PlanEngineError(f"task isn't checked out (status: {task.status.value})")
    if task.action == PlanItemAction.act:
        raise PlanEngineError(
            "the Act task isn't completed with task.complete -- record lessons with lesson.record, "
            "then call act.complete"
        )
    if task.action == PlanItemAction.plan:
        raise PlanEngineError(
            "a 'Write plan' task isn't completed with task.complete -- call plan.revise (or plan.propose "
            "for the first plan), which completes it"
        )
    if task.action in _REVIEW_WORK_ACTIONS:
        raise PlanEngineError("a check is run by the app (it fetches the site and counts), not by the scout")
    if outcome not in _VALID_OUTCOMES:
        raise PlanEngineError("outcome must be done or off_plan")
    off_plan = outcome != "done"
    if off_plan and not (reason and reason.strip()):
        raise PlanEngineError("an off_plan outcome needs an `off_plan_reason` -- what differed from the plan")
    content = await _content_or_none(session, task.owner)
    said_tried = clean_tried(tried)
    if (
        off_plan and require_tried and not said_tried
        and task.action == PlanItemAction.do and item_only_looks(task.kind, content)
    ):
        raise PlanEngineError(TRIED_MISSING_WORDS)
    clean_result = _checked_result(content, task.kind, result, done=not off_plan)

    task.outcome_reason = reason.strip() if off_plan else None
    if off_plan and said_tried:
        task.data = {**(task.data or {}), "tried": said_tried}
    task.evidence = evidence
    task.result = clean_result
    # The scout's own words for this event: its `summary` (what it did, in its own words) when it sent one, falling back
    # to the raw `evidence` quote -- either way this is what the activity log's detail shows, never the app's own text.
    note_text = (summary or evidence or "").strip() or None
    if not off_plan:
        task.status = PlanTaskStatus.done
        # One line per finished task ("Done: <title>") plus the scout's own one-line result as the muted detail.
        _record_activity(
            session, project, "agent", "task_done", f"Done: “{task.title}”",
            agent_text=note_text,
            detail=summary.strip() if summary and summary.strip() else None, task_id=task.id,
        )
    else:
        task.status = PlanTaskStatus.off_plan
        _record_activity(
            session, project, "agent", "task_off_plan", f"The scout found “{task.title}” didn't go to plan.",
            agent_text=note_text,
            detail=f"{task.outcome_reason} Tried: {'; '.join(said_tried)}." if said_tried else task.outcome_reason,
            task_id=task.id,
        )
    await session.flush()
    # The item's run reads how it ended and decides: done, or off plan (a finding). A run that couldn't is held and the
    # Inbox says so.
    await round_process.drive_item_ended(session, project, task)
    # RUN -> CHECK: whether every do-task in this version has now been tried is `next_task_or_reason`'s own question to
    # ask (it's the one place that makes the checks) -- a caller that wants "next" right after this calls it itself.
    return task


async def _declined_plan(session: AsyncSession, project: Project) -> Plan | None:
    """The newest plan version, when it is a declined one. Nothing declines a plan any more (the owner is never asked
    about one); a project holds one only from before that, when declining paused the project (migration 0088 left such
    projects paused), and it is resumed here."""
    newest = await session.scalar(
        select(Plan).where(Plan.project_id == project.id).order_by(Plan.version.desc()).limit(1)
    )
    if newest is not None and newest.status == PlanStatus.declined:
        return newest
    return None


async def resume_project(
    session: AsyncSession, project: Project, who: str = "you", said: str | None = None,
) -> bool:
    """Resume a paused project: its work picks up where it left off. One paused with a declined plan (stored data, see
    `_declined_plan`) also asks for a fresh plan, the same as an iterate. Returns True when a fresh plan was asked for.
    The Activity line says the owner resumed it, unless the app did (`who`, with its own `said`)."""
    said = said or "You resumed this project."
    if project.stopped_at is None:
        # Not paused: a harmless no-op past recording it.
        _record_activity(session, project, who, "resumed", said)
        await session.flush()
        return False
    declined = await _declined_plan(session, project)
    project.stopped_at = None
    project.stop_reason = None
    if declined is None:
        _record_activity(session, project, who, "resumed", said)
        await session.flush()
        return False
    base = (
        await _plan_by_version(session, project.id, declined.based_on_version)
        if declined.based_on_version is not None else None
    )
    report = await report_for_version(session, project.id, base.version) if base is not None else None
    if base is not None and base.status == PlanStatus.approved and report is not None:
        await _create_next_plan_task(session, project, base, report)
    else:
        write_version = await _next_version(session, project.id)
        session.add(PlanTask(
            owner=project.owner, project_id=project.id, plan_id=None, plan_version=0,
            position=0, item_key=f"plan_v{write_version}", title=f"Write plan v{write_version}",
            group_label="Plan", action=PlanItemAction.plan, mode=PlanTaskMode.plan,
            status=PlanTaskStatus.todo,
            context={
                "outcome": project.outcome, "context": f"The owner declined plan v{declined.version}.",
                "requested_by": "owner",
            },
        ))
    await round_process.drive_restarted(session, project)  # the round waits for the new plan
    _record_activity(session, project, who, "resumed", said)
    await session.flush()
    return True


async def stop_project(session: AsyncSession, project: Project, reason: str | None = None) -> Project:
    return await _stop(session, project, reason)


async def _stop(
    session: AsyncSession, project: Project, reason: str | None, activity: tuple[str, str] | None = None,
    who: str = "you",
) -> Project:
    """The owner's Pause (the project page's Pause button): nothing new is handed out and `task.next` refuses with its
    own reason, but every task is kept. A task being worked goes back to waiting (todo, unassigned), and Resume picks
    up where it left off. The Activity line is the owner's, unless the app paused it (`who`, with its `activity`)."""
    if project.stopped_at is not None:
        raise PlanEngineError("this project is already paused")
    project.stopped_at = datetime.now(UTC)
    project.stop_reason = reason or "paused by the owner"
    await session.execute(
        update(PlanTask)
        .where(PlanTask.project_id == project.id, PlanTask.status == PlanTaskStatus.in_progress)
        .values(status=PlanTaskStatus.todo, checked_out_by=None, checked_out_at=None)
    )
    # A task that was being worked is waiting again: work for a site session (nothing is handed out while paused).
    queue.post(session, project.owner, queue.ready("site"), [""], cause="core:completion._stop")
    if activity is None:
        activity = ("stopped", f"You paused this project: {project.stop_reason}")
    _record_activity(session, project, who, activity[0], activity[1])
    await session.flush()
    return project
