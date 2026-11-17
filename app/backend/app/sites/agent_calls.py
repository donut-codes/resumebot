"""One body for each agent call that has an MCP tool (the tools themselves call these).

Copied from another project's code (app/PROVENANCE.md). The scout reaches the app through MCP tools, and each
tool does the same work as a function here. Each function takes plain arguments (a session, the rows, the caller's
connection id), does what the call does (the held-task check, the secret scan, the service call, the next task, the
wake that is due) and gives back plain data. What stays at the door: who the caller is and may be, turning an error into
the door's own refusal, the shape of the reply, and the commit. Where a payload leaves the app, `planengine.handouts`
keeps a copy of it, in the door that sends it (`keep_copy`, given the reply the door built); no function here builds a
task view.

Services flush, routes commit: nothing here commits. Errors are the domain's own (`PlanEngineError`, `SecretRejected`,
`TaskNotHeld`). A wake is data (`Wake`), not an action: it has to be sent after the commit that made it true, with
`wake_ops.send` / `send_all`, which knock the scout's door with the reason `site_project`.

Not here (features this app does not have): `task.escalate`, `agent.declare`,
`wiki.write`, `project.note`, a deterministic runner's check result (Check is code) and the plan check doors.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.security import reject_secrets
from app.errors import DomainError
from app.models.agent import AgentConnection
from app.planengine.handouts import sent as keep_handout
from app.sites import act_summary, agent_handout, completion, lessons, loop, plans, round_process
from app.sites.base import WAKE_REASONS
from app.sites.models import Plan, PlanItemAction, PlanStatus, PlanTask, Project


class Wake(NamedTuple):
    """A session to wake once the commit has landed: `reason` and `mode`."""

    reason: str
    mode: str


class TaskNotHeld(DomainError):
    """The task is not checked out to this connection any more: set aside, or restarted. Each door words the refusal
    itself."""

    status = 409


async def require_held(session: AsyncSession, task: PlanTask, connection_id: uuid.UUID) -> None:
    """The task a connection says it is acting from must be checked out to it."""
    if task.checked_out_by != connection_id:
        raise TaskNotHeld("this task isn't checked out to you")


def wake_for_reason(reason: str | None) -> Wake | None:
    """The scout session to wake when a hand-out said `reason`: Act, the next plan, or a retried planning task."""
    return Wake(reason, "plan") if reason in WAKE_REASONS else None


# --- task.next ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NextTask:
    """What checking out the next task came to: the task (None when nothing is handed out), why not, and the session to
    wake. The door builds the task's view, so it can keep a copy of it with its own `via`."""

    task: PlanTask | None
    reason: str | None
    wake: Wake | None


async def next_task(session: AsyncSession, project: Project, connection_id: uuid.UUID) -> NextTask:
    """Check out this project's next task for the connection (resume before new)."""
    task, reason = await completion.next_task_or_reason(session, project, connection_id)
    return NextTask(task, reason, wake_for_reason(reason))


async def keep_copy(
    session: AsyncSession, project: Project, task: PlanTask, reply: dict[str, Any], connection_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Keep a copy of what `task.next` is about to hand the scout (`planengine.handouts`, `via="task_next"`): the
    reply as sent, the task it is for (`task_handouts.plan_task_id`), the step the task was stamped for and the
    instruction and template files that step names. The door calls this with the reply it built, and sends the reply
    it gets back. Recording never stops a hand-out (a copy that can't be kept is logged and dropped)."""
    mark = agent_handout.stamp_of(task)
    conn = await session.get(AgentConnection, connection_id) if connection_id is not None else None
    await keep_handout(
        session, project.owner, None, reply, title=task.title, via="task_next", connection=conn,
        process=round_process.ITEM_KEY if task.action == PlanItemAction.do else round_process.KEY,
        step=mark["step"] if mark else None, plan_task_id=task.id,
        spec={"instructions": mark.get("instructions"), "template": mark.get("template")} if mark else None,
    )
    return reply


# --- task.complete ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Completion:
    """A do-task, completed. `next` is set when the scout asked for its next task. `wakes` are sent in order after the
    commit."""

    task: PlanTask
    next: NextTask | None = None
    wakes: list[Wake] = field(default_factory=list)


async def complete_task(
    session: AsyncSession, task: PlanTask, project: Project, connection_id: uuid.UUID, *, outcome: str,
    reason: str | None = None, evidence: str | None = None, summary: str | None = None,
    result: dict[str, Any] | None = None, tried: list[str] | None = None, want_next: bool = True,
    reason_field: str = "reason",
) -> Completion:
    """Finish a do-task the connection holds: `done` or `off_plan` with the reason, what it found as `result`, what a
    look-only step `tried`. `reason_field` is the name the door calls the off-plan reason, so a secret refusal names the
    field the agent sent."""
    await require_held(session, task, connection_id)
    reject_secrets(
        summary=summary, evidence=evidence, **{reason_field: reason},
        result=json.dumps(result) if result else None, tried=json.dumps(tried) if tried else None,
    )
    updated = await completion.complete_task(
        session, task, project, outcome, reason=reason, evidence=evidence, summary=summary, result=result,
        tried=tried, require_tried=True,
    )
    wakes: list[Wake] = []
    following = None
    if want_next:
        following = await next_task(session, project, connection_id)
        if following.wake is not None:
            wakes.append(following.wake)
    return Completion(updated, following, wakes)


# --- plan.propose, plan.revise -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanSent:
    """A plan version proposed or revised: it may have started on its own, or been held (`started` False: the site can't
    be read, or the round could not take it). `wake` is sent after the commit."""

    plan: Plan
    started: bool
    wake: Wake | None


def _sent(plan: Plan) -> PlanSent:
    """A plan just written, as the door reports it: started when the round approved it, and then the scout is woken to
    do the items."""
    started = plan.status == PlanStatus.approved
    return PlanSent(plan, started, Wake("plan_approved", "do") if started else None)


def _json(value: Any) -> str | None:
    return json.dumps(value) if value else None


async def propose_plan(
    session: AsyncSession, project: Project, *, summary: str, method: str, items: list[dict[str, Any]],
    validation: dict[str, Any] | None = None, act_guidance: str | None = None, problem: str | None = None,
    proposal: list[str] | None = None, dropped: list[dict[str, Any]] | None = None,
) -> PlanSent:
    """Propose plan version 1 (or the next, while none is approved). The round's run starts it unless the site can't be
    read (`round_process.drive_proposed`). Every free-text field is scanned for secrets first."""
    reject_secrets(
        summary=summary, method=method, act_guidance=act_guidance, problem=problem, validation=_json(validation),
        proposal=_json(proposal), dropped=_json(dropped),
    )
    plan = await plans.propose_plan(
        session, project, summary, method, items, validation=validation, act_guidance=act_guidance,
        problem=problem, proposal=proposal, dropped=dropped,
    )
    return _sent(plan)


async def revise_plan(
    session: AsyncSession, project: Project, *, based_on_version: int, summary: str, changes: str,
    items: list[dict[str, Any]], method_changed: bool = False, validation: dict[str, Any] | None = None,
    act_guidance: str | None = None, problem: str | None = None, proposal: list[str] | None = None,
    method: str | None = None, dropped: list[dict[str, Any]] | None = None,
) -> PlanSent:
    """Propose a revision of `based_on_version` (the not-yet-done tasks for what it touches stop being checked out); the
    round's run starts it unless the site can't be read."""
    reject_secrets(
        summary=summary, changes=changes, act_guidance=act_guidance, problem=problem, method=method,
        validation=_json(validation), proposal=_json(proposal), dropped=_json(dropped),
    )
    plan = await plans.revise_plan(
        session, project, based_on_version, summary, changes, method_changed, items,
        validation=validation, act_guidance=act_guidance, problem=problem, proposal=proposal, method=method,
        dropped=dropped,
    )
    return _sent(plan)


# --- act.complete -------------------------------------------------------------------------------------------------


async def complete_act(
    session: AsyncSession, project: Project, task: PlanTask, connection_id: uuid.UUID, *, summary: object,
    resolution: dict[str, str] | None, skill: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Wake | None]:
    """End ACT with the wrap-up in its named parts (`act_summary`: planned, done, checks, learned, next; a missing or
    empty part is refused, every problem at once), on the closing round the `{problem, done, outcome}` resolution, and,
    when the round changed it, the site's skill. Then the loop moves on by itself; returns what happened and the scout
    session to wake for what follows."""
    await require_held(session, task, connection_id)
    parts = act_summary.parse(summary)
    reject_secrets(
        **{f"summary_{k}": v for k, v in parts.items()},
        **{f"resolution_{k}": v for k, v in (resolution if isinstance(resolution, dict) else {}).items()
           if isinstance(v, str)},
        skill=_json(skill),
    )
    outcome = await loop.complete_act(session, task, project, parts, resolution, skill)
    return outcome, wake_for_reason(outcome["next"])


# --- lesson.record, lesson.retire ---------------------------------------------------------------------------------


async def record_lesson(
    session: AsyncSession, project: Project, task: PlanTask, connection_id: uuid.UUID, *, text: str,
    applies_to: str, source: str | None, subject: str | None,
) -> tuple[Any, bool]:
    """Act only: one thing the app should remember, from the task the connection holds. Returns the lesson and whether
    it is new."""
    await require_held(session, task, connection_id)
    reject_secrets(text=text, source=source, subject=subject)
    return await lessons.record_lesson(session, task, project, text, applies_to, source, subject)


async def retire_lesson(
    session: AsyncSession, project: Project, task: PlanTask, connection_id: uuid.UUID, *, lesson_id: str,
    source: str | None,
) -> Any:
    """Act only: this round's evidence contradicts a lesson, so it is retired (kept, hidden, the trail says by what)."""
    await require_held(session, task, connection_id)
    reject_secrets(source=source)
    return await lessons.retire_lesson(session, task, project, lesson_id, source)

