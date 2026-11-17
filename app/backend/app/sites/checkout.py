"""Checking out the next task: the item's hand-out and the scout's turn.

Copied from another project's code (app/PROVENANCE.md). The scout takes every step, a site plan only reads, and
Check is code, so what is left is the order work is handed out in (the plan's items in plan order, then Act, then the
next plan), the item's run reading how it is
handed out at the moment it is (`round_process.drive_hand_out`), and a kind that is unknown or switched off ending its
item off plan with the reason in words.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites import round_process
from app.sites.base import PLAN_ACTIONS
from app.sites.connections import _content_or_none
from app.sites.models import Plan, PlanItemAction, PlanStatus, PlanTask, PlanTaskStatus, Project
from app.sites.records import _check_out, _next_todo, _record_activity


async def _resume_held_task(
    session: AsyncSession, project_id: uuid.UUID, plan_id: uuid.UUID, connection_id: uuid.UUID,
) -> PlanTask | None:
    """Resume before taking anything new: a task this agent already holds comes back first, whichever mode it is -- a
    do-task or the Act task. Without this, an agent that lost track of its held task called task.next and checked out a
    second one, leaving two in progress."""
    return await session.scalar(
        select(PlanTask)
        .where(
            PlanTask.project_id == project_id, PlanTask.plan_id == plan_id,
            PlanTask.status == PlanTaskStatus.in_progress, PlanTask.checked_out_by == connection_id,
            PlanTask.superseded.is_(False),
        )
        .order_by(PlanTask.position)
        .limit(1)
    )


async def checkout_next_task(session: AsyncSession, project: Project, connection_id: uuid.UUID) -> PlanTask | None:
    """Atomic, oldest-first checkout of the next do-task from the project's currently *approved* plan version -- never
    a task from a superseded or still-awaiting-approval version, and never anything at all while the project is
    stopped or finished. Once every do-task has been tried and the checks have run, hands out Act, then the next
    "Write plan". A task the connection already holds comes back first, whatever its mode."""
    if project.stopped_at is not None or project.finished_at is not None:
        return None
    approved = await session.scalar(
        select(Plan).where(Plan.project_id == project.id, Plan.status == PlanStatus.approved)
    )
    if approved is None:
        return await _checkout_first_plan_task(session, project, connection_id)
    held = await _resume_held_task(session, project.id, approved.id, connection_id)
    if held is not None:
        return held
    task = await _next_permitted_do_task(session, project, approved.id, connection_id)
    if task is not None:
        return await _check_out(session, task, connection_id)  # None: another call took it first
    # ACT, then (verdict "iterate") the next PLAN.
    for action in (PlanItemAction.act, PlanItemAction.plan):
        parent = await _next_todo(session, project.id, approved.id, action)
        if parent is not None:
            return await _check_out(session, parent, connection_id)
    return None


async def _next_todo_do_task(session: AsyncSession, project_id: uuid.UUID, plan_id: uuid.UUID) -> PlanTask | None:
    return await session.scalar(
        select(PlanTask)
        .where(
            PlanTask.project_id == project_id, PlanTask.plan_id == plan_id,
            PlanTask.status == PlanTaskStatus.todo, PlanTask.action == PlanItemAction.do,
            PlanTask.superseded.is_(False),
        )
        .order_by(PlanTask.position)
        .limit(1)
    )


#: At most this many do-tasks are looked at in one hand-out; an item whose kind is refused ends off plan and the scan
#: moves on to the next.
PERMISSION_SCAN_MAX = 50


async def _next_permitted_do_task(
    session: AsyncSession, project: Project, plan_id: uuid.UUID, connection_id: uuid.UUID | None = None,
) -> PlanTask | None:
    """The next free do-task the app lets the scout run. A task whose kind is unknown or switched off ends off plan with
    the reason in words, and the scan moves on."""
    last = None
    for _ in range(PERMISSION_SCAN_MAX):
        task = await _next_todo_do_task(session, project.id, plan_id)
        if task is None:
            return task
        if task.id == last:
            return None  # still todo after its turn: its run could not read how it is handed out, and the Inbox says so
        last = task.id
        if await permit_task(session, project, task, connection_id):
            return task
    return None


async def _kind_refusal(session: AsyncSession, task: PlanTask) -> str | None:
    """Why the app won't run this item's kind, in words; None when it will. A kind-less item is never refused, and
    nothing is refused while the app ships no kind files."""
    if not task.kind:
        return None
    content = await _content_or_none(session, task.owner)
    if content is None or not content.kinds:
        return None
    spec = content.kinds.get(task.kind)
    if spec is None:
        return "resumebot doesn't know this kind of work, so it didn't run it."
    if not spec.enabled:
        return "This kind of work is switched off, so resumebot didn't run it."
    return None


async def permit_task(
    session: AsyncSession, project: Project, task: PlanTask, connection_id: uuid.UUID | None,
) -> bool:
    """Whether the app lets the scout run this do-task now. True means go ahead. Otherwise the task was dealt with here:
    its run could not read how it is handed out (held, the Inbox says so), or its kind is refused and it ended off plan
    with the reason in plain words.

    The item's run in the project round reads how the item is handed out first, here, at the moment it is."""
    if await round_process.drive_hand_out(session, project, task, connection_id) != round_process.GONE_ON:
        return False  # the run could not read it and the Inbox says so
    why = await _kind_refusal(session, task)
    if why is None:
        return True
    task.status = PlanTaskStatus.off_plan
    task.outcome_reason = why
    _record_activity(session, project, "app", "permission_denied", why, task_id=task.id)
    await session.flush()
    # The item ended off plan without anyone taking it: its run hears of it as it hears of any ending.
    await round_process.drive_item_ended(session, project, task)
    return False


async def _checkout_first_plan_task(
    session: AsyncSession, project: Project, connection_id: uuid.UUID,
) -> PlanTask | None:
    """No approved plan yet: the only work is a project's own "Write plan v1" (action `plan`, no plan row to belong to).
    Resume what this connection holds, then take it."""
    held = await session.scalar(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id.is_(None), PlanTask.action.in_(PLAN_ACTIONS),
            PlanTask.status == PlanTaskStatus.in_progress, PlanTask.checked_out_by == connection_id,
            PlanTask.superseded.is_(False),
        ).limit(1)
    )
    if held is not None:
        return held
    parent = await _next_todo(session, project.id, None, PlanItemAction.plan)
    if parent is not None:
        return await _check_out(session, parent, connection_id)
    return None
