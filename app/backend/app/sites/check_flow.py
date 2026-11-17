"""Advancing and completing plan checks.

Copied from another project's code (app/PROVENANCE.md). The checks are code (`site.check`), run by the round's own
process (`playbooks/sites/check_one_item.bpmn`), so no agent takes a check: there is no runner's door for checks, no
payload for a checker and no Act/plan sub-task completion here. What is: `advance_plan_checks`, which makes the checks
once Do has ended and hands the round
on, and `complete_plan_check`, which stores a check's finding.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.fetch import polite
from app.sites import round_process, site_check
from app.sites.base import CHECK_DIFFERENCE_MAX_CHARS, OPEN_STATUSES, PlanEngineError
from app.sites.loop import advance_after_report
from app.sites.models import Plan, PlanItemAction, PlanTask, PlanTaskStatus, Project
from app.sites.plan_check import _plan_checks, is_plan_check
from app.sites.records import _open_review_work_exists, _record_activity, report_for_version
from app.textutil import squash


async def advance_plan_checks(
    session: AsyncSession, project: Project, plan: Plan,
) -> tuple[PlanTask | None, str | None]:
    """Everything due in a plan-driven Check, idempotent: make the checks once Do has ended, write the report once every
    check is done, and hand on to Act (`advance_after_report`). `(task, reason)`: the planning step the report led to,
    or `(None, None)` when nothing is due (the round isn't waiting to make the checks, or Act/Plan is held).

    The checks run inside the round's run (a process step in a savepoint), which cannot end its transaction: when they
    may be due (Do has ended and no report is written), the site is read first, with no transaction open
    (`site_check.read_ahead`), and the checks take what was read."""
    if plan.validation is None:
        return None, None
    with polite.read_ahead(await read_ahead(session, project, plan)):
        return await _advance(session, project, plan)


async def read_ahead(session: AsyncSession, project: Project, plan: Plan) -> dict:
    """What the round's checks read (`site_check.read_ahead`), read now with no transaction open, when they may be due:
    the plan has a Check section, Do has ended and no report is written. Empty otherwise, or when the session cannot be
    released here (a caller inside a savepoint reads ahead before it)."""
    if plan.validation is None or not db.can_release(session):
        return {}
    if await _open_do(session, project, plan) is not None:
        return {}
    if await report_for_version(session, project.id, plan.version) is not None:
        return {}
    return await site_check.read_ahead(session, project)


async def _open_do(session: AsyncSession, project: Project, plan: Plan):
    return await session.scalar(
        select(PlanTask.id).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == plan.id, PlanTask.action == PlanItemAction.do,
            PlanTask.status.in_(OPEN_STATUSES), PlanTask.superseded.is_(False),
        ).limit(1)
    )


async def _advance(session: AsyncSession, project: Project, plan: Plan) -> tuple[PlanTask | None, str | None]:
    await round_process.catch_up(session, project, plan)  # an item whose ending was held is read again
    if await _open_do(session, project, plan) is not None:
        return None, None
    if await report_for_version(session, project.id, plan.version) is None:
        if not await _plan_checks(session, project.id, plan.id):
            # The round's run makes the checks, runs each (code), and writes the report itself once the last one has
            # ended (or at once when nothing was tried).
            made = await round_process.drive_make_checks(session, project, plan)
            if made is None:
                return None, None  # the round isn't waiting to make them, or couldn't and the Inbox says so
        elif await _open_review_work_exists(session, project.id, plan.id):
            return None, None
    return await advance_after_report(session, project, plan)


async def complete_plan_check(
    session: AsyncSession, task: PlanTask, project: Project, on_plan: Any, difference: str | None = None,
) -> PlanTask:
    """A plan check's result: `on_plan` (bool) and, when it is off plan, `difference` (what differs, in the plan's "off
    plan means" terms; at most `CHECK_DIFFERENCE_MAX_CHARS`). Stored as the usual finding (verdict ok or issue, note =
    difference). Called by the check's own step (`site.check`); the round writes the report once every check has
    ended."""
    if not is_plan_check(task):
        raise PlanEngineError("that isn't a plan check")
    if task.superseded:
        raise PlanEngineError("that task was replaced")
    if not isinstance(on_plan, bool):
        raise PlanEngineError("a check ends with `on_plan`: true or false")
    text = squash(difference)
    if not on_plan and not text:
        raise PlanEngineError("an off-plan check needs a `difference`: how it differs from the plan")
    if len(text) > CHECK_DIFFERENCE_MAX_CHARS:
        raise PlanEngineError(f"the difference is longer than the {CHECK_DIFFERENCE_MAX_CHARS}-character cap")
    key = (task.data or {}).get("key") or task.item_key
    title = (task.data or {}).get("title") or task.title
    task.findings = [{
        "key": key, "verdict": "ok" if on_plan else "issue", "cause": None, "note": None if on_plan else text,
    }]
    task.status = PlanTaskStatus.done
    task.checked_out_by = None
    task.checked_out_at = None
    _record_activity(
        session, project, "app", "check_on_plan" if on_plan else "check_off_plan",
        f"On plan: {title}" if on_plan else f"Off plan: {title} — {text}", task_id=task.id,
    )
    await session.flush()
    # A check the code feeds (not the one that runs inside its own run) is read by its run: it decides on plan or not.
    await round_process.drive_check_ended(session, project, task, on_plan, text or None)
    return task
