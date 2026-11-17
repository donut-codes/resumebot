"""The "where you are" header every task an agent receives opens with.

Built from the process file (`playbooks/project.pdca.process.yaml`) and the project's state:
`{project, round, phase, step, step_number, step_count, before, your_job, next}`. Compact on purpose (a few hundred
characters): a small model reads it first and knows its place in the loop.

Copied from another project's code (app/PROVENANCE.md) and moved here (app/PROVENANCE.md, "Site project loop"), trimmed:
the skills a connection holds (`skills_service.slugs_for_where`) and the Teacher's task are not here, and the project is
this app's site project (`app.sites.models`).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.harness import instructions as files
from app.harness.loader import Process, Step, harness
from app.jobsearch.work import instruction_files
from app.sites.models import PlanItemAction, PlanTask, Project, ReviewReport

#: Which process step an agent task is, by its action.
_STEP_BY_ACTION = {
    PlanItemAction.plan: "plan.write",
    PlanItemAction.do: "do.item",
    PlanItemAction.review_check: "check.item",
    PlanItemAction.act: "act.learn",
}


def step_id_for(task: PlanTask) -> str | None:
    """The process step id this task is, or None for a task the loop doesn't name."""
    return _STEP_BY_ACTION.get(task.action)


def round_number(finished_checks: int, project_finished: bool) -> int:
    """Which round of the loop this is: finished checks + 1 (a finished project ended in the round of its last check).
    Always at least 1."""
    return max(finished_checks if project_finished else finished_checks + 1, 1)


async def finished_checks(session: AsyncSession, project: Project) -> int:
    """Rounds whose check has ended: one per plan version with a check report."""
    return int(await session.scalar(
        select(func.count(func.distinct(ReviewReport.plan_version))).where(ReviewReport.project_id == project.id)
    ) or 0)


def _before(process: Process, step: Step) -> str:
    prev = next((s for s in process.steps if s.id != step.id and step.id in s.next), None)
    return f"Before this: {prev.title}." if prev is not None and prev.title else ""


def _after(step: Step, process: Process) -> str:
    titles = [t.title for n in step.next if n != step.id and (t := process.step(n)) is not None and t.title]
    if step.id in step.next:
        titles.insert(0, "the next one")
    if not titles:
        return ""
    joined = titles[0] if len(titles) == 1 else ", ".join(titles[:-1]) + ", or " + titles[-1]
    return f"After this: {joined}."


def where_header(process: Process, step_id: str, project_title: str, round_: int) -> dict | None:
    step = process.step(step_id)
    if step is None:
        return None
    return {
        "project": project_title, "round": round_, "phase": step.phase, "step": step.title or step.id,
        "step_number": process.steps.index(step) + 1, "step_count": len(process.steps),
        "before": _before(process, step), "your_job": step.your_job, "next": _after(step, process),
    }


def _digits(text: str | None) -> str:
    return "".join(c for c in (text or "") if c.isdigit())


def instruction_values(task: PlanTask, project_title: str, round_: int) -> dict[str, object]:
    """The `{{placeholders}}` an instruction may use, for `task`: `project`, `round`, `plan_version`, `item_key`,
    `kind`, `write_version`, `based_on_version` and `plan_call` (the call that ends a plan task)."""
    ctx = task.context or {}
    based_on = ctx.get("based_on_version")
    write_version = ctx.get("write_version") or _digits(task.item_key) or _digits(task.title) or "1"
    plan_call = f"`plan.revise` (based_on_version {based_on})" if based_on else "`plan.propose`"
    return {
        "project": project_title, "round": round_, "plan_version": task.plan_version, "item_key": task.item_key,
        "kind": task.kind or "", "write_version": write_version, "based_on_version": based_on or "",
        "plan_call": plan_call,
    }


async def guidance_for(session: AsyncSession, project: Project, task: PlanTask) -> dict:
    """What opens every task an agent receives: `where` (the header), `instruction` (the step's instruction file,
    rendered for this task) and `template` (the name of its response shape). Each is left out when it doesn't apply or
    can't be built; a broken content file never stops a task."""
    process = harness().process
    step_id = step_id_for(task)
    if process is None or step_id is None:
        return {}
    rnd = round_number(await finished_checks(session, project), project.finished_at is not None)
    where = where_header(process, step_id, project.title, rnd)
    if where is None:
        return {}
    out: dict = {"where": where}
    step = process.step(step_id)
    # When a driven process handed this task out, the step it is in names the instruction and the response template
    # (`sites.agent_handout`); otherwise the loop's own step does, as it always has.
    from app.sites import agent_handout

    instructions = agent_handout.named_instruction(task, step.instructions)
    template = agent_handout.named_template(task, step.template)
    try:
        if instructions:
            values = instruction_values(task, project.title, rnd)
            # The owner's newest saved version of the file, else the shipped one (`jobsearch.work.instruction_files`).
            text = await instruction_files.read(session, project.owner, "instructions", instructions)
            out["instruction"] = files.render(text, values)
        if template:
            out["template"] = files.short_name("templates", template)
    except (files.UnknownName, files.MissingPlaceholder, OSError):
        out.pop("instruction", None)
        out.pop("template", None)
    return out
