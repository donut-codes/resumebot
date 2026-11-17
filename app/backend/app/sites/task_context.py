"""What a task is handed: its view and its context.

Copied from another project's code (app/PROVENANCE.md). Not here: the second-look views, the goal's playbook,
a wiki, the standing notes ("told" notes), the doer's model card, sub-task summaries, and a planner's lookup
answers. A planning task also carries the site (its address and kind, and its skill so far, one row for each version).
"""

import copy

from sqlalchemy.ext.asyncio import AsyncSession

from app.harness import instructions as harness_files
from app.harness.kinds import kind_spec, planner_kinds
from app.jobsearch.work import instruction_files
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, TASK_ENVELOPE_RESERVE_CHARS, payload_chars
from app.sites import act_summary, resolution
from app.sites.base import _ESCALATION_KEYS, PLAN_ACTIONS, PLAN_TEMPLATE_NAME, plan_template_for_context
from app.sites.compact import compact_context, compact_lessons
from app.sites.connections import _content_or_none, doer_kinds_for
from app.sites.earlier import earlier_results
from app.sites.lessons import lessons_for_project
from app.sites.loop import site_card
from app.sites.models import PlanItemAction, PlanTask, PlanTaskMode, Project
from app.sites.task_header import guidance_for
from app.textutil import one_line

#: The shapes a planning task's `report_example` shows. Placeholders are in angle brackets; the caller fills them in.
_PLAN_ITEM_EXAMPLE = {
    "key": "<a short stable key>", "title": "<what is done, in plain words>", "group": "Do", "action": "do",
    "kind": "<a kind from context.kinds>", "data": {"<a field the kind requires>": "<its value>"},
}
_VALIDATION_EXAMPLE = {
    "done_means": "<what done looks like for one item>",
    "how_to_check": "<how to check one item: what is fetched and counted>",
    "off_plan_means": "<what going off plan looks like for one item>", "tools_needed": [],
}


def _plan_example(project: Project, based_on: object) -> dict:
    """`plan.propose` (a first plan) or `plan.revise` (`based_on` is the version it revises), as the tools take them."""
    revising = based_on is not None
    example: dict = {"project_id": str(project.id)}
    if revising:
        example["based_on_version"] = based_on
    example["summary"] = "<markdown: what this plan does, in plain words>"
    if revising:
        example["changes"] = "<what is different from the version it revises, and why>"
    example.update({
        "method": "<one paragraph: how every item is done>",
        "problem": "<what is wrong, in 2-3 plain sentences with real numbers>",
        "proposal": ["<what the plan will do, one short bullet>"],
        "items": [copy.deepcopy(_PLAN_ITEM_EXAMPLE)],
        "validation": copy.deepcopy(_VALIDATION_EXAMPLE),
    })
    return example


def finish_hint(project: Project, task: PlanTask) -> dict:
    """The call that ends a planning task and its shape: `report_with` and `report_example`. Write plan ends with
    `plan.propose` (a first plan) or `plan.revise` (a later one, which names the `based_on_version` in the task's
    context); Act ends with `act.complete`. A sub-step is left out: its instructions say how it ends. The project and
    task ids are real; the rest are placeholders."""
    if task.mode != PlanTaskMode.plan:
        return {}
    context = task.context or {}
    if task.action == PlanItemAction.plan:
        based_on = context.get("based_on_version")
        return {
            "report_with": "plan.revise" if based_on else "plan.propose",
            "report_example": _plan_example(project, based_on or None),
        }
    if task.action == PlanItemAction.act:
        example: dict = {
            "project_id": str(project.id), "task_id": str(task.id),
            "summary": {name: f"<{what}>" for name, _label, what in act_summary.PARTS},
            "skill": "<the site's saved recipe, when this round changed it>",
        }
        if context.get("closing"):
            example["resolution"] = {part: f"<{part}, in plain words>" for part in resolution.PARTS}
        return {"report_with": "act.complete", "report_example": example}
    return {}


def _with_finish(view: dict, finish: dict) -> dict:
    """The view with `report_with` and `report_example` straight after the task's own guidance (`where`, `instruction`,
    `template`), before the task and its context."""
    if not finish:
        return view
    head = {k: view[k] for k in ("where", "instruction", "template") if k in view}
    return {**head, **finish, **{k: v for k, v in view.items() if k not in head}}


def task_next_view(task: PlanTask) -> dict:
    """The task shape both `task.next` (MCP) and the agent REST twins hand back. `mode` ("do" | "plan") is what tells
    the caller which kind of session this task is for; an Act task carries its own frozen `context` (the report, the
    round's items, the closing line) instead of a `data` blob."""
    data = task.data
    if data and any(k in data for k in (*_ESCALATION_KEYS, "handout")):
        data = {k: v for k, v in data.items() if k not in _ESCALATION_KEYS and k != "handout"}
    view = {
        "task_id": str(task.id), "key": task.item_key, "title": one_line(task.title, 200),
        "group": task.group_label, "action": task.action.value, "data": data,
        "plan_version": task.plan_version, "mode": task.mode.value,
    }
    if task.kind:
        view["kind"] = task.kind
    asked = (task.data or {}).get("escalation")
    if isinstance(asked, dict) and asked.get("answer"):
        # The task went to the owner (`task.escalate`) and came back: it is handed out again with their answer.
        view["owner_answer"] = {"question": asked.get("question"), "answer": asked["answer"]}
    if task.context is not None:
        view["context"] = compact_context(task.context or {})
    return view


async def task_view(session: AsyncSession, project: Project, task: PlanTask) -> dict:
    """`task_next_view` plus what only a session can look up: a PLAN-mode task's `lessons` (what the app has learned
    that applies here, selected by tag -- see `lessons_for_project`) and the site."""
    view = {**await guidance_for(session, project, task), **task_next_view(task)}
    # A broken content file never stops a task from being handed out.
    content = await _content_or_none(session, task.owner)
    if content is not None and task.kind and task.kind in content.kinds:
        # The scout gets the kind's own file: its data, how it is done, what done means.
        view["kind_spec"] = kind_spec(content.kinds[task.kind])
    if task.mode == PlanTaskMode.do and task.action == PlanItemAction.do and task.plan_id is not None:
        # What the items before it found: a page an earlier item fetched can't be opened from here, so the app hands on
        # what it holds of it (earlier.py).
        if (earlier := await earlier_results(session, task)):
            view["context"] = {**earlier, **(view.get("context") or {})}
    if task.mode == PlanTaskMode.plan:
        context = dict(view.get("context") or {})
        # The call that ends this task and its shape (`report_with`, `report_example`) ride in the view, so what the
        # rest of the context may take is what they leave.
        finish = finish_hint(project, task)
        reserve = TASK_ENVELOPE_RESERVE_CHARS + (payload_chars(finish) if finish else 0)
        context.update(compact_lessons(await lessons_for_project(session, project)))
        if (site := await site_card(session, project)) is not None:
            context["site"] = site
        context["limits"] = {"payload_budget_chars": AGENT_PAYLOAD_BUDGET_CHARS}
        if content is not None and task.action in PLAN_ACTIONS and content.kinds:
            # What the scout can run.
            context["kinds"] = planner_kinds(content, await doer_kinds_for(session, project))
        if task.action == PlanItemAction.plan:
            # The plan template and its version: the full text when it fits what the rest of the context leaves of the
            # budget, otherwise its short form (doc.read has the rest).
            room = AGENT_PAYLOAD_BUDGET_CHARS - reserve - payload_chars(context) - 200
            try:
                in_force = await instruction_files.read(session, task.owner, "templates", PLAN_TEMPLATE_NAME)
            except (harness_files.UnknownName, OSError):
                in_force = None  # the plan template then reads as it always has, from the file or as missing
            context["plan_template"] = plan_template_for_context(room, in_force)
        view["context"] = context
        view = _with_finish(view, finish)
    return view
