"""Check -> Act -> Loop (PDCA): the Act task, unresolved items and the next plan task.

Copied from another project's code (app/PROVENANCE.md). Act keeps the site's skill
(`skills.keep_skill`) and its lessons instead.

Act judges the round against the project's own outcome: the closing Act task carries `closing.outcome`
(`resolution.closing_hint`), and the scout ends the project only when this round's evidence meets it. Without it the
loop runs extra rounds on work outside the outcome.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.planengine.budget import fit_rows
from app.sites import act_summary, project_summary, resolution, round_process
from app.sites.base import STEP_SUMMARY_MAX_CHARS, PlanEngineError
from app.sites.connections import _content_or_none
from app.sites.lessons import lessons_for_project, lessons_recorded
from app.sites.models import (
    Plan,
    PlanItem,
    PlanItemAction,
    PlanTask,
    PlanTaskMode,
    PlanTaskStatus,
    Project,
    ReviewReport,
    Site,
)
from app.sites.plan_check import _investigate_count, closable_by_act
from app.sites.records import (
    _next_version,
    _record_activity,
    report_for_version,
    supersede_open_plans,
)
from app.sites.skills import keep_skill, skills_of_site
from app.textutil import one_line


async def _loop_task(
    session: AsyncSession, project_id: uuid.UUID, plan_id: uuid.UUID, action: PlanItemAction,
) -> PlanTask | None:
    return await session.scalar(
        select(PlanTask).where(
            PlanTask.project_id == project_id, PlanTask.plan_id == plan_id, PlanTask.action == action,
            PlanTask.superseded.is_(False),
        ).order_by(PlanTask.created_at.desc(), PlanTask.position.desc()).limit(1)
    )


#: Act works from its own round: each item's outcome and evidence, stored whole (up to this many characters each) and
#: handed to the agent within `ACT_ITEMS_BUDGET_CHARS`; the rest are named by key.
ACT_ITEM_EVIDENCE_STORE_CHARS = 1_500


async def round_items(session: AsyncSession, plan: Plan) -> list[dict]:
    """This round's items for Act: `[{key, title, status, outcome, evidence}]`, in plan order. A do item carries how its
    task ended (done / off_plan / not tried), the reason it went off plan and its evidence; an `investigate` item is
    "still to look into" (never handed to a doer)."""
    tasks = {
        t.item_key: t for t in (await session.scalars(
            select(PlanTask).where(
                PlanTask.plan_id == plan.id, PlanTask.action == PlanItemAction.do, PlanTask.superseded.is_(False),
            ).order_by(PlanTask.updated_at)
        )).all()
    }
    items = (await session.scalars(
        select(PlanItem).where(
            PlanItem.plan_id == plan.id, PlanItem.action.in_((PlanItemAction.do, PlanItemAction.investigate)),
        ).order_by(PlanItem.position)
    )).all()
    rows = []
    for item in items:
        task = tasks.get(item.key)
        if item.action == PlanItemAction.investigate:
            status, outcome, evidence = "not tried", "Still to look into.", ""
        elif task is None:
            status, outcome, evidence = "not tried", "", ""
        else:
            status = task.status.value
            outcome = (task.outcome_reason or "").strip()
            evidence = (task.evidence or "").strip()
        rows.append({
            "key": item.key, "title": one_line(item.title, 200), "status": status,
            "outcome": one_line(outcome, ACT_ITEM_EVIDENCE_STORE_CHARS),
            "evidence": one_line(evidence, ACT_ITEM_EVIDENCE_STORE_CHARS),
        })
    return rows


async def site_card(session: AsyncSession, project: Project) -> dict | None:
    """The site a project is for, as a task carries it: `{url, kind, skill: [{version, retired}]}` (the skill so far,
    one row for each version). None for a project with no site."""
    site = await session.get(Site, project.site_id) if project.site_id is not None else None
    if site is None:
        return None
    return {
        "url": site.url, "kind": site.kind,
        "skill": [
            {"version": s.version, "retired": s.retired_at is not None}
            for s in await skills_of_site(session, site.id)
        ],
    }


async def _create_act_task(session: AsyncSession, project: Project, plan: Plan, report: ReviewReport) -> PlanTask:
    guidance = {"plan_act_guidance": one_line(plan.act_guidance, 600)} if plan.act_guidance else {}
    said = (plan.decision_reason or "").strip()  # the owner's words as typed, line breaks kept
    notes = [{"said": said}] if said else []
    site = await site_card(session, project)
    site_part = {"site": site} if site else {}
    # The review accepted it and nothing is left to look into: this Act closes the project, so it writes the project's
    # resolution too (the problem, as the app would say it, to start from, and the outcome to judge the round against).
    # A round of only looking with nothing off plan may be closed by its Act too, when the scout finds nothing more to
    # do: the hint says the resolution is optional there, and without one the project goes on.
    closing = {}
    if report.verdict == "accepted" and not await _investigate_count(session, plan):
        closing = {"closing": await resolution.closing_hint(session, project)}
    elif await closable_by_act(session, plan, report):
        closing = {"closing": {**await resolution.closing_hint(session, project), "optional": True}}
    task = PlanTask(
        owner=project.owner, project_id=project.id, plan_id=plan.id, plan_version=plan.version,
        position=10**9 + 20_000, item_key=f"act-v{plan.version}", title="Learn from the review", group_label="Act",
        action=PlanItemAction.act, mode=PlanTaskMode.plan, status=PlanTaskStatus.todo,
        context={
            # What the round set out to do, so Act judges the round against its intent, not only its item list.
            "plan_summary": one_line(plan.summary, 600),
            "report": {
                "plan_version": report.plan_version, "verdict": report.verdict, "summary": report.summary,
                "findings_summary": report.findings_summary,
            },
            "items": await round_items(session, plan),
            "notes": notes,
            **site_part,
            **closing,
            **guidance,
        },
    )
    session.add(task)
    _record_activity(
        session, project, "app", "act_started",
        f"The review of plan v{plan.version} is written -- resumebot started Act.",
    )
    await session.flush()
    return task


#: The Write-plan context's list of unresolved items: its own share of the agent payload budget.
UNRESOLVED_ITEMS_BUDGET_CHARS = 20_000
UNRESOLVED_FOUND_CHARS = 160
#: A finding the next plan acts on goes through whole: cut at 160 the planner got half a finding and had to escalate.
UNRESOLVED_FINDING_CHARS = 1_000
UNRESOLVED_UNLISTED_KEYS_MAX = 50


async def _plan_check_issues(session: AsyncSession, project: Project, plan: Plan) -> list[dict]:
    """Every off-plan finding of this version's done plan checks, as `[{key, title, note}]`. Checks that share a
    position are listed in the order they finished, then by item key, never in the database's order for ties."""
    rows = list((await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == plan.id,
            PlanTask.action == PlanItemAction.review_check, PlanTask.status == PlanTaskStatus.done,
            PlanTask.superseded.is_(False),
        ).order_by(PlanTask.position, PlanTask.updated_at, PlanTask.item_key)
    )).all())
    items = (await session.scalars(select(PlanItem).where(PlanItem.plan_id == plan.id))).all()
    titles = {i.key: i.title for i in items}
    return [
        {"key": f.get("key"), "title": titles.get(f.get("key"), f.get("key")), "note": f.get("note")}
        for row in rows for f in row.findings or [] if f.get("verdict") == "issue"
    ]


async def unresolved_items(session: AsyncSession, project: Project, plan: Plan) -> dict:
    """What the last check left unresolved, as `{unresolved_items: [{key, title, found}]}` (plus
    `unresolved_unlisted_keys`/`_total` when they don't all fit): every off-plan check, then every `do` item of the plan
    that has no finished task (untried items must not be dropped by the next plan), deduplicated."""
    rows: dict[str, dict] = {}
    for f in await _plan_check_issues(session, project, plan):
        key = f.get("key")
        if key and key not in rows:
            rows[key] = {"key": key, "title": f.get("title") or key, "found": f.get("note")}
    finished = set((await session.scalars(
        select(PlanTask.item_key).where(
            PlanTask.plan_id == plan.id, PlanTask.status == PlanTaskStatus.done, PlanTask.superseded.is_(False),
        )
    )).all())
    tasks_by_key: dict[str, PlanTask] = {}
    for t in (await session.scalars(
        select(PlanTask).where(PlanTask.plan_id == plan.id, PlanTask.mode == PlanTaskMode.do)
        .order_by(PlanTask.updated_at)
    )).all():
        tasks_by_key[t.item_key] = t
    content = await _content_or_none(session, plan.owner)
    for item in (await session.scalars(
        select(PlanItem).where(
            PlanItem.plan_id == plan.id, PlanItem.action.in_((PlanItemAction.do, PlanItemAction.investigate)),
        ).order_by(PlanItem.position)
    )).all():
        if item.key in rows:
            continue
        spec = content.kinds.get(item.kind or "") if content is not None else None
        if item.key in finished and spec is not None and spec.feeds_next_plan:
            # A read-only finding the next plan acts on: pass it on.
            last = tasks_by_key.get(item.key)
            evidence = str(last.evidence or "") if last is not None else ""
            rows[item.key] = {
                "key": item.key, "title": item.title, "found": evidence or "Done; nothing recorded.", "whole": True,
                "result": last.result if last is not None else None,
            }
            continue
        if item.key in finished:
            continue
        if item.action == PlanItemAction.investigate:
            # Never handed out as a task, so nothing finishes it: still open for the next plan.
            rows[item.key] = {"key": item.key, "title": item.title, "found": "Still to look into."}
            continue
        last = tasks_by_key.get(item.key)
        tried = last is not None and last.status == PlanTaskStatus.off_plan and not last.superseded
        found = f"Tried, didn't go to plan: {last.outcome_reason}" if tried and last.outcome_reason else (
            "Tried, didn't go to plan." if tried else "Not done yet."
        )
        rows[item.key] = {"key": item.key, "title": item.title, "found": found}
    ordered = list(rows.values())
    if not ordered:
        return {}

    def render(r: dict) -> dict:
        row = {
            "key": r["key"], "title": one_line(r["title"], 120),
            "found": one_line(str(r["found"] or ""), UNRESOLVED_FINDING_CHARS) if r.get("whole")
            else one_line(str(r["found"] or ""), UNRESOLVED_FOUND_CHARS),
        }
        if r.get("result"):
            row["result"] = r["result"]  # the finding as data, whole
        return row

    listed, rest = fit_rows(ordered, UNRESOLVED_ITEMS_BUDGET_CHARS, render)
    out: dict = {"unresolved_items": listed}
    if rest:
        out["unresolved_unlisted_keys"] = [r["key"] for r in rest[:UNRESOLVED_UNLISTED_KEYS_MAX]]
        out["unresolved_unlisted_total"] = len(rest)
    return out


async def _create_next_plan_task(
    session: AsyncSession, project: Project, plan: Plan, report: ReviewReport,
) -> PlanTask:
    """Verdict "iterate": "Write plan v(n+1)" -- a plan-mode task whose context carries the report's findings, the
    lessons that apply and what is left unresolved. Belongs to the version it revises."""
    lessons = await lessons_for_project(session, project)
    write_version = await _next_version(session, project.id)
    plan_count = int(await session.scalar(
        select(func.count()).select_from(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == plan.id, PlanTask.action == PlanItemAction.plan,
        )
    ) or 0)
    context = {
        "based_on_version": plan.version, "write_version": write_version,
        "review": {
            "verdict": report.verdict, "summary": report.summary, "findings_summary": report.findings_summary,
        },
        "lessons": lessons,
        **await unresolved_items(session, project, plan),
    }
    task = PlanTask(
        owner=project.owner, project_id=project.id, plan_id=plan.id, plan_version=plan.version,
        position=10**9 + 30_000 + plan_count, item_key=f"plan-v{write_version}",
        title=f"Write plan v{write_version}", group_label="Plan", action=PlanItemAction.plan,
        mode=PlanTaskMode.plan, status=PlanTaskStatus.todo, context=context,
    )
    session.add(task)
    _record_activity(
        session, project, "app", "next_plan_started",
        f"resumebot asked for plan v{write_version}, from what the review found.",
    )
    await session.flush()
    return task


async def _finish_from_report(session: AsyncSession, project: Project, report: ReviewReport) -> None:
    if project.finished_at is not None:
        return
    project.finished_at = datetime.now(UTC)
    # What was done, in words: the Act note's own first sentence, else "Finished." Never the check report's counts.
    act = await session.scalar(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.action == PlanItemAction.act,
            PlanTask.status == PlanTaskStatus.done,
        ).order_by(PlanTask.updated_at.desc()).limit(1)
    )
    done_words = project_summary.finished_words(act_summary.done_text(act))
    project.finish_summary = done_words
    # How it ended, in three parts: the closing Act's own when it wrote one, else the app's, never empty.
    closed = await resolution.close(session, project, resolution.ACCEPTED, agent=resolution.of_act(act))
    _record_activity(
        session, project, "app", "project_finished", f"This project is finished. {closed['outcome']}",
        agent_text=done_words,
    )
    await supersede_open_plans(session, project)
    await session.flush()


async def advance_after_report(
    session: AsyncSession, project: Project, plan: Plan,
) -> tuple[PlanTask | None, str | None]:
    """Everything that follows a written review report, idempotent -- the one place that creates Act and the next Plan,
    and ends the project. Called when the check report is written, by `act.complete`, and by every backstop.

    `(task, reason)`: a freshly created or still-open planning step and its wake reason; `(None, "finished")` once the
    project is done; `(None, None)` when nothing is due (the report is in, Act is held by someone, or plan v(n+1) is
    already written)."""
    report = await report_for_version(session, project.id, plan.version)
    if report is None:
        return None, None
    # The round's run decides: it hands Act out (the task is made when it reaches the step), waits while Act is open,
    # and when Act ended finishes the project (`_finish_from_report`) or asks for the next plan
    # (`_create_next_plan_task`). `(None, None)` when nothing is due.
    return await round_process.drive_after_report(session, project, plan)


async def complete_act(
    session: AsyncSession, task: PlanTask, project: Project, summary: dict, resolved: object | None = None,
    skill: dict | None = None,
) -> dict:
    """`act.complete`: ends ACT. Needs the Act task itself held. Then LOOP: the round's run decides, finishing the
    project (the report accepted it and nothing is left, or the scout's resolution says its outcome is met) or asking
    for plan v(n+1). Returns what happened.

    `resolved` is the closing round's `{problem, done, outcome}`: kept on the task and used when this Act finishes the
    project; a round that goes on to another plan leaves it unused. One that is not three plain sentences is refused (so
    the scout can say it again); none at all is fine, the app then writes it. `skill` is the site's saved recipe when
    the round changed it: a new `js_site_skills` row.

    `summary` is the wrap-up in named parts (`act_summary`: planned, done, checks, learned, next), checked here: every
    problem is refused at once."""
    if task.action != PlanItemAction.act:
        raise PlanEngineError("act.complete completes the Act task -- that isn't it")
    if task.project_id != project.id:
        raise PlanEngineError("that task isn't on this project")
    if task.superseded:
        raise PlanEngineError("that task was replaced -- call task.next for your next task")
    if task.status != PlanTaskStatus.in_progress:
        raise PlanEngineError(f"task isn't checked out (status: {task.status.value})")
    text, parts = act_summary.stored(summary)
    kept = resolution.parse(resolved) if resolved else None
    kept_skill = await keep_skill(session, project, skill) if skill else None
    task.status = PlanTaskStatus.done
    task.evidence = text.strip()[:STEP_SUMMARY_MAX_CHARS]
    task.checked_out_by = None
    task.checked_out_at = None
    if kept is not None:
        task.data = {**(task.data or {}), "resolution": kept}
    task.data = {**(task.data or {}), act_summary.DATA_KEY: parts}
    if kept_skill is not None:
        task.data = {**(task.data or {}), "skill_version": kept_skill.version}
    lessons = await lessons_recorded(session, project, task.plan_version)
    _record_activity(
        session, project, "agent", "act_done",
        f"The scout wrote up what it learned about “{project.title}”.",
        agent_text=act_summary.done_text(task), detail=act_summary.done_text(task),
    )  # one line: the summary's sections show on the project page only
    await session.flush()
    plan = await session.get(Plan, task.plan_id)
    next_task, reason = await advance_after_report(session, project, plan)
    return {
        "lessons_recorded": lessons, "next": reason, "next_task_id": str(next_task.id) if next_task else None,
        "skill_version": kept_skill.version if kept_skill is not None else None,
    }
