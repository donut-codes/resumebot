"""Small shared records the other site-loop modules build on: Activity lines, plan versions, open-work checks and the
one-line text helpers.

What happened is written to `trail_events` (`app.models.work.TrailEvent`):
`_record_activity` writes one (who, kind, text, and the agent's own words kept as the muted detail).
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.work.claim import claim_one
from app.models.work import TrailEvent
from app.sites.base import OPEN_STATUSES
from app.sites.models import (
    Plan,
    PlanItem,
    PlanItemAction,
    PlanStatus,
    PlanTask,
    PlanTaskStatus,
    Project,
    ReviewReport,
)
from app.textutil import cut_at_word, squash
from app.trail.service import APP_WHO

#: `who` as the loop says it -> the trail's actor and the name the Activity page shows.
_ACTORS = {"agent": ("agent", "Scout"), "you": ("person", "You"), "app": ("app", APP_WHO)}
#: The Activity page's own tiers: what needs the owner, what is a result, what is routine.
NEEDS_YOU, RESULT, ROUTINE = "needs_you", "result", "routine"
_RESULT_KINDS = {"plan_auto_started", "project_finished", "act_done", "check_report", "task_done"}
PROJECT_LINK = "project"
#: The longest an Activity sentence runs (the trail's own cap).
SENTENCE_MAX = 140


async def _next_version(session: AsyncSession, project_id: uuid.UUID) -> int:
    current = await session.scalar(select(func.max(Plan.version)).where(Plan.project_id == project_id))
    return (current or 0) + 1


def _one_sentence(text: str, cap: int = 240) -> str:
    """A plan's summary as one line for the trail: whitespace folded, the closing full stop dropped."""
    line = squash(text).rstrip(".")
    return cut_at_word(line, cap)


def _record_activity(
    session: AsyncSession, project: Project, who: str, kind: str, text: str,
    agent_text: str | None = None, detail: str | None = None, task_id: uuid.UUID | None = None,
) -> TrailEvent:
    """One Activity line, in `trail_events`. `text` is always the app's own plain words (cut to 140 characters, at a
    word; when it was cut and there is no other detail, the whole of it is kept as the detail, so nothing is lost);
    `detail` is the muted one-line detail (an off-plan reason, a finished task's one-line result); `agent_text` (the
    agent's own words for this event) is kept as the detail when no `detail` is given. `task_id` is accepted for
    callers' sake and not stored (the trail has no task column)."""
    actor, name = _ACTORS.get(who, ("app", APP_WHO))
    words = (detail or agent_text or "").strip() or None
    sentence = cut_at_word(text.strip(), SENTENCE_MAX)
    if words is None and sentence != text.strip():
        words = text.strip()
    row = TrailEvent(
        owner=project.owner, actor=actor, agent_slug="scout" if actor == "agent" else None, who=name,
        event=kind[:40], sentence=sentence, tier=RESULT if kind in _RESULT_KINDS else ROUTINE, mark="neutral",
        link_kind=PROJECT_LINK, link_id=project.id, detail=words,
    )
    session.add(row)
    return row


def _add_items(session: AsyncSession, plan: Plan, items: list[dict]) -> None:
    for position, row in enumerate(items):
        session.add(PlanItem(
            plan_id=plan.id, position=position, key=row["key"], title=row["title"],
            group_label=row["group"], action=PlanItemAction(row["action"]), kind=row.get("kind") or None,
            data=row.get("data") or {},
        ))


async def _plan_by_version(session: AsyncSession, project_id: uuid.UUID, version: int) -> Plan | None:
    return await session.scalar(select(Plan).where(Plan.project_id == project_id, Plan.version == version))


#: The actions the check phase hands out: a plan check.
_REVIEW_WORK_ACTIONS = (PlanItemAction.review_check,)


async def _open_review_work_exists(session: AsyncSession, project_id: uuid.UUID, plan_id: uuid.UUID) -> bool:
    """Whether any check task for this version is still todo or in_progress -- the review loop's own analogue of "an
    open do-task exists"."""
    row = await session.scalar(
        select(PlanTask.id).where(
            PlanTask.project_id == project_id, PlanTask.plan_id == plan_id,
            PlanTask.action.in_(_REVIEW_WORK_ACTIONS), PlanTask.status.in_(OPEN_STATUSES),
            PlanTask.superseded.is_(False),
        ).limit(1)
    )
    return row is not None


async def _next_todo(
    session: AsyncSession, project_id: uuid.UUID, plan_id: uuid.UUID | None, action: PlanItemAction,
) -> PlanTask | None:
    """The oldest free (todo, not superseded) task of `action` in one plan version (`plan_id` None: the pre-approval
    planning tasks)."""
    return await session.scalar(
        select(PlanTask).where(
            PlanTask.project_id == project_id,
            PlanTask.plan_id == plan_id if plan_id is not None else PlanTask.plan_id.is_(None),
            PlanTask.action == action, PlanTask.status == PlanTaskStatus.todo, PlanTask.superseded.is_(False),
        ).order_by(PlanTask.position, PlanTask.created_at).limit(1)
    )


async def _check_out(session: AsyncSession, task: PlanTask, connection_id: uuid.UUID) -> PlanTask | None:
    """Take `task` (one conditional statement that finds it still `todo` and marks it in progress for the connection),
    or None when another call took it first: the caller hands out nothing this time and the scout asks again."""
    from datetime import UTC, datetime

    return await claim_one(
        session, select(PlanTask).where(PlanTask.id == task.id, PlanTask.status == PlanTaskStatus.todo),
        status=PlanTaskStatus.in_progress, checked_out_by=connection_id, checked_out_at=datetime.now(UTC))


# --- CHECK -> ACT -> LOOP (PDCA) ---------------------------------------------------------------------------------


async def report_for_version(session: AsyncSession, project_id: uuid.UUID, plan_version: int) -> ReviewReport | None:
    return await session.scalar(
        select(ReviewReport).where(ReviewReport.project_id == project_id, ReviewReport.plan_version == plan_version)
    )


def _plain_item_title(title: str) -> str:
    """An item's own name, without a task's wording around it."""
    return title.strip()


#: A plan in one of these states still waits for someone: the owner's yes.
WAITING_PLAN_STATUSES = (PlanStatus.awaiting_approval,)


async def supersede_open_plans(session: AsyncSession, project: Project) -> int:
    """A project that has finished no longer needs its plans decided: a plan still awaiting approval is marked
    superseded so it never shows in the Inbox. Idempotent; returns how many."""
    plans = (await session.scalars(
        select(Plan).where(Plan.project_id == project.id, Plan.status.in_(WAITING_PLAN_STATUSES))
    )).all()
    for plan in plans:
        plan.status = PlanStatus.superseded
    if plans:
        await session.flush()
    return len(plans)

