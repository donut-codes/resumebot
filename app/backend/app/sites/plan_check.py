"""Plan-driven Check: creating the checks, their results and the round's report.

Copied from another project's code (app/PROVENANCE.md). When Do ends, one check is made for each tried item
(done or off plan; never a skipped one), each carrying that item's data and the plan's own "How to validate". Check is
code, never a model (PRD 29.2): the check's own process step, `site.check`, fetches the site and counts what came back,
and `check_flow.complete_plan_check` stores the finding. When the last one ends the round writes its report (counting
and grouping, no model) and hands on to Act. Not here: checks by a second agent, a "no one else
can check this" note and a `check.by: porter` result (the scout has no tool that writes a check result).
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites.base import PLAN_CHECK_POSITION_BASE, REPORT_FINDINGS_SUMMARY_MAX_CHARS
from app.sites.connections import _content_or_none
from app.sites.models import (
    Plan,
    PlanItem,
    PlanItemAction,
    PlanTask,
    PlanTaskMode,
    PlanTaskStatus,
    Project,
    ReviewReport,
)
from app.sites.records import _record_activity, report_for_version


def is_plan_check(task: PlanTask) -> bool:
    return task.action == PlanItemAction.review_check and bool((task.data or {}).get("plan_check"))


async def _plan_checks(session: AsyncSession, project_id: uuid.UUID, plan_id: uuid.UUID) -> list[PlanTask]:
    rows = (await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id == project_id, PlanTask.plan_id == plan_id,
            PlanTask.action == PlanItemAction.review_check, PlanTask.superseded.is_(False),
        ).order_by(PlanTask.position)
    )).all()
    return [t for t in rows if is_plan_check(t)]


async def _create_plan_checks(session: AsyncSession, project: Project, plan: Plan) -> list[PlanTask]:
    """One check per tried plan item. With nothing tried there is nothing to check: the report is written at once.
    Returns the checks (new ones only, never a second check for an item that has one)."""
    validation = plan.validation or {}
    tried = (await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == plan.id,
            PlanTask.action == PlanItemAction.do,
            PlanTask.status.in_((PlanTaskStatus.done, PlanTaskStatus.off_plan)),
            PlanTask.superseded.is_(False),
        ).order_by(PlanTask.position)
    )).all()
    have = {(t.data or {}).get("key") for t in await _plan_checks(session, project.id, plan.id)}
    checks = []
    for n, item in enumerate(tried):
        if item.item_key in have:
            continue
        task = PlanTask(
            owner=project.owner, project_id=project.id, plan_id=plan.id, plan_version=plan.version,
            position=PLAN_CHECK_POSITION_BASE + n, item_key=f"check-v{plan.version}-{item.item_key}"[:255],
            title=f"Check: {item.title}"[:500], group_label="Check", action=PlanItemAction.review_check,
            kind=item.kind, mode=PlanTaskMode.review, status=PlanTaskStatus.todo,
            data={
                "key": item.item_key, "title": item.title, "kind": item.kind,
                "data": {k: v for k, v in (item.data or {}).items() if k != "handout"},  # (not the item step's stamp)
                "done_means": validation.get("done_means"), "how_to_check": validation.get("how_to_check"),
                "off_plan_means": validation.get("off_plan_means"), "plan_check": True, "checked_by": "code",
            },
        )
        session.add(task)
        checks.append(task)
    await session.flush()
    if checks:
        _record_activity(
            session, project, "app", "review_started",
            f"Every task in plan v{plan.version} has been tried -- resumebot started {len(checks)} "
            f"check{'s' if len(checks) != 1 else ''}, one per item.",
        )
    await session.flush()
    return checks


def _check_difference_summary(differences: list[tuple[str, str]]) -> list[dict]:
    """Off-plan items grouped by identical difference text, each group {difference, items}."""
    groups: dict[str, list[str]] = {}
    for title, difference in differences:
        groups.setdefault(difference, []).append(title)
    return [{"difference": d, "items": titles} for d, titles in groups.items()]


async def _untried_keys(session: AsyncSession, plan: Plan) -> set[str]:
    """The `do` items of the plan that were never tried (F4): no task of theirs ended done or off plan (one skipped, or
    one that never ran, counts as not done)."""
    keys = set((await session.scalars(
        select(PlanItem.key).where(PlanItem.plan_id == plan.id, PlanItem.action == PlanItemAction.do)
    )).all())
    if not keys:
        return set()
    tried = set((await session.scalars(
        select(PlanTask.item_key).where(
            PlanTask.plan_id == plan.id, PlanTask.mode == PlanTaskMode.do, PlanTask.action == PlanItemAction.do,
            PlanTask.status.in_((PlanTaskStatus.done, PlanTaskStatus.off_plan)), PlanTask.superseded.is_(False),
        )
    )).all())
    return keys - tried


async def _investigate_count(session: AsyncSession, plan: Plan) -> int:
    """Items whose work isn't over when the round ends: `investigate` items (never handed out), items of a kind that
    feeds the next plan (a read-only kind: its finding is input for the next plan), and `do` items that were skipped
    or never tried (an item nobody checked is not one that is done, so the round goes again)."""
    rows = (await session.execute(
        select(PlanItem.key, PlanItem.action, PlanItem.kind).where(
            PlanItem.plan_id == plan.id,
            PlanItem.action.in_((PlanItemAction.investigate, PlanItemAction.do)),
        )
    )).all()
    count = 0
    untried = await _untried_keys(session, plan)
    content = await _content_or_none(session, plan.owner)
    for key, action, kind in rows:
        spec = content.kinds.get(kind or "") if content is not None else None
        if (
            action == PlanItemAction.investigate or key in untried
            or (spec is not None and spec.feeds_next_plan)
        ):
            count += 1
    return count


async def looks_only(session: AsyncSession, plan: Plan) -> bool:
    """Every item the round ran only looks (a read-only kind) and none is an `investigate` item still to look into: the
    round changed nothing and left nothing unlooked. Such a round's findings feed the next plan
    (`_investigate_count`), so it never reads "accepted" by itself; its Act may close it instead."""
    rows = (await session.execute(
        select(PlanItem.action, PlanItem.kind).where(
            PlanItem.plan_id == plan.id, PlanItem.action.in_((PlanItemAction.investigate, PlanItemAction.do)),
        )
    )).all()
    content = await _content_or_none(session, plan.owner)
    if not rows or content is None or await _untried_keys(session, plan):
        return False  # an item nobody tried was not looked at
    for action, kind in rows:
        spec = content.kinds.get(kind or "")
        if action == PlanItemAction.investigate or spec is None or spec.risk != "read_only":
            return False
    return True


async def closable_by_act(session: AsyncSession, plan: Plan, report: ReviewReport) -> bool:
    """A round of only looking, with nothing off plan, that the scout may close in Act: it has read the findings and can
    say, in the project's resolution, that its outcome is met. The verdict stays what it is (`iterate`, "J with more to
    do next round"); closing is the scout's explicit choice, against the project's outcome, never the app's reading of
    the counts."""
    return not report.findings_summary and await looks_only(session, plan)


async def write_check_report(session: AsyncSession, project: Project, plan: Plan) -> ReviewReport:
    """The deterministic review report for a plan-driven Check: verdict "accepted" when every check is on plan, else
    "iterate"; "N of M on plan; K off plan."; the off-plan items grouped by their difference; no recommendations (the
    scout writes the next plan). No model."""
    checks = [t for t in await _plan_checks(session, project.id, plan.id) if t.status == PlanTaskStatus.done]
    on_plan = 0
    off: list[tuple[str, str]] = []
    for task in checks:
        finding = (task.findings or [{}])[0]
        title = (task.data or {}).get("title") or task.title
        if finding.get("verdict") == "issue":
            off.append((title, finding.get("note") or "differs from the plan"))
        else:
            on_plan += 1
    total = on_plan + len(off)
    summary = f"{on_plan} of {total} on plan; {len(off)} off plan."
    looking = await _investigate_count(session, plan)
    if looking:
        summary += f" {looking} with more to do next round."
    groups = _check_difference_summary(off)
    findings_summary: Any = None
    if groups:
        lines = [f"{g['difference']} ({', '.join(g['items'])})" for g in groups]
        text = "Off plan: " + "; ".join(lines)
        if len(text) > REPORT_FINDINGS_SUMMARY_MAX_CHARS - 200:
            text = text[: REPORT_FINDINGS_SUMMARY_MAX_CHARS - 200].rstrip() + "…"
        findings_summary = text
    report = await report_for_version(session, project.id, plan.version)
    if report is None:
        report = ReviewReport(owner=project.owner, project_id=project.id, plan_version=plan.version)
        session.add(report)
    # Items still to look into keep the loop going: a round that passed every check but left items unlooked is not
    # "accepted", or Act would finish the project.
    report.verdict = "iterate" if off or looking else "accepted"
    report.summary, report.findings_summary = summary, findings_summary
    _record_activity(
        session, project, "app", "check_report",
        f"resumebot checked the scout's work on “{project.title}”: "
        + ("it all held up." if not off else "some of it needs another try."),
    )
    await session.flush()
    return report
