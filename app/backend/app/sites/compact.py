"""Compact payloads: the agent-facing context budget, and the report and lesson digests.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, fit_rows, payload_chars
from app.sites.base import ACT_ITEM_TEXT_CHARS, ACT_ITEMS_BUDGET_CHARS
from app.sites.lessons import lessons_for_project
from app.sites.models import Project, ReviewReport
from app.textutil import one_line

# --- the agent context budget (see app/planengine/budget.py) ---------------
#
# A task's frozen `context` is stored in full (the project page and the audit trail read it), but
# what an agent is handed is compact: one-line rows within a character budget, and the rest named by
# key so `item.lookup` can fetch full detail for exactly the ones it needs.

CONTEXT_UNLISTED_KEYS_MAX = 150
CONTEXT_TITLE_CHARS = 50


def compact_context(context: dict) -> dict:
    """The compact, budgeted form of a planning task's frozen `context` (Act's own round: `items`, the check `report`).
    Idempotent; never raises the budget to fit."""
    ctx = {k: v for k, v in context.items() if k not in ("report", "review", "items")}
    if isinstance(context.get("items"), list):
        # Act's own round: every item's outcome and evidence, whole, within a budget; the rest by key.
        listed, rest = fit_rows(
            context["items"], ACT_ITEMS_BUDGET_CHARS,
            lambda r: {
                "key": r.get("key"), "title": one_line(r.get("title"), CONTEXT_TITLE_CHARS * 2),
                "status": r.get("status"), "outcome": one_line(r.get("outcome"), ACT_ITEM_TEXT_CHARS),
                "evidence": one_line(r.get("evidence"), ACT_ITEM_TEXT_CHARS),
            },
        )
        ctx["items"] = listed
        if rest:
            ctx["items_unlisted_keys"] = [r.get("key") for r in rest[:CONTEXT_UNLISTED_KEYS_MAX]]
            ctx["items_unlisted_total"] = len(rest)
            ctx["detail_with"] = "item.lookup"
    for key in ("report", "review"):
        if isinstance(context.get(key), dict):
            ctx[key] = compact_report(context[key])
    return ctx


#: PDCA payloads (Plan vN with lessons, the review report, Act). Stored whole; an agent gets one-liners within these
#: budgets and the full text one call away (`review.read`, `lesson.list`).
REPORT_SUMMARY_CHARS = 600
REPORT_FINDINGS_BUDGET_CHARS = 1000
LESSONS_BUDGET_CHARS = 1500
LESSON_LINE_CHARS = 200
LESSON_SOURCE_LINE_CHARS = 100
#: A full-text reply (`review.read`, `lesson.list`) stays inside the agent budget too.
FULL_TEXT_RESERVE_CHARS = 1_500


def _compact_findings_summary(findings) -> Any:
    if isinstance(findings, dict):
        listed, rest = fit_rows(
            list(findings.items()), REPORT_FINDINGS_BUDGET_CHARS, lambda kv: {kv[0]: one_line(kv[1], 200)},
        )
        out: dict = {k: v for row in listed for k, v in row.items()}
        if rest:
            out["_more"] = f"{len(rest)} more causes in review.read"
        return out
    return one_line(findings, REPORT_FINDINGS_BUDGET_CHARS) if findings else findings


def compact_report(report: dict) -> dict:
    """A review report (or the `review`/`report` block of an Act or "Write plan"
    context) for an agent: the summary and findings clipped to one-liners. `detail_with` names the call that returns
    every word."""
    out = {k: v for k, v in report.items() if k not in ("summary", "findings_summary")}
    out["summary"] = one_line(report.get("summary"), REPORT_SUMMARY_CHARS)
    if "findings_summary" in report:
        out["findings_summary"] = _compact_findings_summary(report.get("findings_summary"))
    clipped = (
        len(str(report.get("summary") or "")) > REPORT_SUMMARY_CHARS
        or payload_chars(report.get("findings_summary")) > REPORT_FINDINGS_BUDGET_CHARS
    )
    if clipped:
        out["detail_with"] = "review.read"
    return out


def compact_lessons(lessons: list[dict]) -> dict:
    """{"lessons": [...one-liners within a budget], and, when some were left
    out or clipped, "lessons_unlisted_total" + "lessons_detail_with"}."""
    listed, rest = fit_rows(
        lessons, LESSONS_BUDGET_CHARS,
        lambda r: {
            "id": r.get("id"), "text": one_line(r.get("text"), LESSON_LINE_CHARS), "applies_to": r.get("applies_to"),
            "source": one_line(r.get("source"), LESSON_SOURCE_LINE_CHARS),
        },
    ) if lessons else ([], [])
    out: dict = {"lessons": listed}
    clipped = rest or any(len(str(r.get("text") or "")) > LESSON_LINE_CHARS for r in lessons)
    if rest:
        out["lessons_unlisted_total"] = len(rest)
    if clipped:
        out["lessons_detail_with"] = "lesson.list"
    return out


async def report_read(session: AsyncSession, project: Project, plan_version: int | None = None) -> dict | None:
    """`review.read`: a stored review report in full (latest version unless
    `plan_version` names one) -- what a compact payload's
    `detail_with: "review.read"` points at."""
    query = select(ReviewReport).where(ReviewReport.project_id == project.id)
    if plan_version is not None:
        query = query.where(ReviewReport.plan_version == plan_version)
    report = await session.scalar(query.order_by(ReviewReport.plan_version.desc()).limit(1))
    if report is None:
        return None
    return {
        "plan_version": report.plan_version, "verdict": report.verdict, "summary": report.summary,
        "findings_summary": report.findings_summary,
    }


async def lessons_full(
    session: AsyncSession, project: Project, offset: int = 0, budget: int = AGENT_PAYLOAD_BUDGET_CHARS,
) -> dict:
    """`lesson.list`: the lessons that apply to this project, full text, as many as fit
    the agent budget from `offset` (`next_offset` when there are more)."""
    rows = await lessons_for_project(session, project)
    offset = max(int(offset or 0), 0)
    listed, rest = fit_rows(rows[offset:], budget - FULL_TEXT_RESERVE_CHARS, lambda r: r)
    out: dict = {"lessons": listed, "total": len(rows)}
    if rest:
        out["next_offset"] = offset + len(listed)
    return out
