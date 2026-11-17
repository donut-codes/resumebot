"""Proposing, revising and starting plan versions.

Copied from another project's code (app/PROVENANCE.md). Not here: Equip (a plan waiting for a Teacher to write the
skills it needs), standing notes, the owner's decision on a plan (go ahead, send it back, pause, stop:
the owner is never asked about a plan), the destructive-kind permission rules approval used to write, and the plan's
`stuck` record (the round budget is the only going-in-circles rule, and it is the app's, `autostart.ROUND_CAP`, never
the owner's question). A plan starts by the process file itself (`round_process.drive_proposed`, step PR-6S), never by a
rule in this module.
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.work import queue
from app.sites import asks, round_process
from app.sites.base import (
    LOOP_ACTIONS,
    OPEN_STATUSES,
    PLAN_ACTIONS,
    PLAN_METHOD_MAX_CHARS,
    PlanDiff,
    PlanEngineError,
    plan_template,
)
from app.sites.coverage import _account_for_everything
from app.sites.models import (
    Plan,
    PlanItem,
    PlanItemAction,
    PlanStatus,
    PlanTask,
    PlanTaskMode,
    PlanTaskStatus,
    Project,
)
from app.sites.records import (
    _add_items,
    _next_version,
    _one_sentence,
    _plan_by_version,
    _record_activity,
)
from app.sites.validation import (
    _validate_act_guidance,
    _validate_items,
    _validate_kinds,
    _validate_problem_proposal,
    _validate_validation,
)


async def _complete_planning_task(session: AsyncSession, project: Project) -> None:
    """A project's own "Write plan v1" task closes the moment its holder actually proposes a plan."""
    tasks = (await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id.is_(None),
            PlanTask.action.in_(PLAN_ACTIONS), PlanTask.status.in_(OPEN_STATUSES),
        )
    )).all()
    for task in tasks:
        task.status = PlanTaskStatus.done
        task.checked_out_by = None
        task.checked_out_at = None
        # The plan came after all: a "couldn't finish" ask about this task has nothing left to ask.
        await asks.settle_ask(session, project.owner, f"stuck-{task.id}", "The plan came in after all.")
    if tasks:
        await session.flush()


async def propose_plan(
    session: AsyncSession, project: Project, summary: str, method: str, items: list[dict],
    validation: dict | None = None, act_guidance: str | None = None, problem: str | None = None,
    proposal: list[str] | None = None, dropped: list[dict] | None = None,
) -> Plan:
    """A new plan must carry its Problem, Proposal and Check section (`validation`), and every `do` item must name a
    kind: a plan without one is refused, saying what is missing. `dropped`: the unresolved items this plan leaves out on
    purpose, `[{key, reason}]`; every key of the Write-plan task's `unresolved_items` must be an item or in `dropped`.
    Raises `PlanEngineError`."""
    if not summary or not summary.strip():
        raise PlanEngineError("a plan needs a summary")
    if not method or not method.strip():
        raise PlanEngineError("a plan needs a method: how every item is done")
    _validate_items(items)
    await _validate_kinds(session, project, items)
    clean_validation = _validate_validation(validation, required=True)
    clean_problem, clean_proposal = _validate_problem_proposal(problem, proposal)
    clean_act = _validate_act_guidance(act_guidance)
    clean_dropped = await _account_for_everything(session, project, items, dropped)
    version = await _next_version(session, project.id)
    plan = Plan(
        owner=project.owner, project_id=project.id, version=version,
        status=PlanStatus.awaiting_approval, summary=summary.strip(), method=method.strip(),
        validation=clean_validation, act_guidance=clean_act, problem=clean_problem, proposal=clean_proposal,
        dropped=clean_dropped, template_version=plan_template()["version"],
    )
    session.add(plan)
    await session.flush()
    _add_items(session, plan, items)
    _record_activity(
        session, project, "agent", "plan_proposed", f"The scout drew up a plan for “{project.title}”.",
        agent_text=summary.strip(),
    )
    await session.flush()
    await _complete_planning_task(session, project)
    await round_process.drive_proposed(session, project, plan)  # the round's run takes the plan
    return plan


def _diff_items(old_items: list[PlanItem], new_items: list[dict]) -> PlanDiff:
    old_by_key = {i.key: i for i in old_items}
    new_by_key = {r["key"]: r for r in new_items}
    added = sorted(set(new_by_key) - set(old_by_key))
    removed = sorted(set(old_by_key) - set(new_by_key))
    changed = sorted(
        key for key in (set(old_by_key) & set(new_by_key))
        if (
            old_by_key[key].title != new_by_key[key]["title"]
            or old_by_key[key].group_label != new_by_key[key]["group"]
            or old_by_key[key].action.value != new_by_key[key]["action"]
            or (bool(new_by_key[key].get("kind")) and old_by_key[key].kind != new_by_key[key]["kind"])
            or (old_by_key[key].data or {}) != (new_by_key[key].get("data") or {})
        )
    )
    return PlanDiff(added=added, removed=removed, changed=changed)


async def revise_plan(
    session: AsyncSession, project: Project, based_on_version: int, summary: str, changes: str,
    method_changed: bool, items: list[dict], validation: dict | None = None, act_guidance: str | None = None,
    problem: str | None = None, proposal: list[str] | None = None, method: str | None = None,
    dropped: list[dict] | None = None,
) -> Plan:
    """A revision accounts for everything left, as `propose_plan` does (`dropped`). A revision's Problem is what the
    last round found, so it never inherits the old one: it needs a fresh `problem` and `proposal`, and its own `method`
    (how this version's items are done). A revision keeps the Check and Act sections of the version it revises unless it
    sends new ones; a version with no Check section to keep needs one sent."""
    based_on = await _plan_by_version(session, project.id, based_on_version)
    if based_on is None:
        raise PlanEngineError(f"no plan version {based_on_version} on this project to revise")
    # Every version writes its own Do method.
    if method is not None and len(method.strip()) > PLAN_METHOD_MAX_CHARS:
        raise PlanEngineError(f"`method` is longer than the {PLAN_METHOD_MAX_CHARS}-character cap")
    if not (method and method.strip()):
        raise PlanEngineError(
            "a revision needs its own `method`: how this version's items are done (the Do section). "
            "Write it fresh for this version; never carry text over from an older one"
        )
    clean_method = method.strip()
    method_changed = bool(method_changed) or clean_method != based_on.method
    clean_validation = _validate_validation(validation, required=False)
    clean_act = _validate_act_guidance(act_guidance)
    if clean_validation is None:
        clean_validation = based_on.validation
    if clean_validation is None:
        _validate_validation(None, required=True)
    clean_problem, clean_proposal = _validate_problem_proposal(problem, proposal)
    if clean_act is None:
        clean_act = based_on.act_guidance
    if not changes or not changes.strip():
        raise PlanEngineError("a revision needs `changes`: what changes and why")
    if not summary or not summary.strip():
        raise PlanEngineError("a plan needs a summary")
    _validate_items(items)
    await _validate_kinds(session, project, items)
    clean_dropped = await _account_for_everything(session, project, items, dropped)

    old_items = list((await session.scalars(select(PlanItem).where(PlanItem.plan_id == based_on.id))).all())
    diff = _diff_items(old_items, items)

    version = await _next_version(session, project.id)
    new_plan = Plan(
        owner=project.owner, project_id=project.id, version=version,
        status=PlanStatus.awaiting_approval, summary=summary.strip(), method=clean_method,
        based_on_version=based_on_version, changes=changes.strip(), method_changed=bool(method_changed),
        diff=diff.as_dict(), validation=clean_validation, act_guidance=clean_act,
        problem=clean_problem, proposal=clean_proposal, dropped=clean_dropped,
        template_version=plan_template()["version"],
    )
    session.add(new_plan)
    await session.flush()
    _add_items(session, new_plan, items)

    # Supersede the based-on version's not-yet-done tasks: every touched item when the method itself didn't change, or
    # *all* remaining tasks of that version when it did -- a method change pauses the whole project's outstanding work
    # until the revision is re-approved, not just the items that visibly differ.
    stmt = (
        update(PlanTask)
        .where(
            PlanTask.plan_id == based_on.id,
            PlanTask.status.in_(OPEN_STATUSES),
            # The loop's own steps (check, act, plan) are never "superseded by a new plan": they are how a plan gets
            # revised.
            PlanTask.action.notin_(LOOP_ACTIONS),
        )
        .values(superseded=True)
    )
    if not method_changed:
        stmt = stmt.where(PlanTask.item_key.in_(diff.touched_keys)) if diff.touched_keys else None
    if stmt is not None:
        with queue.exempt("tasks superseded by a new plan are work taken away: nothing is made ready"):
            await session.execute(stmt)

    # PDCA: the "Write plan v(n+1)" task (and any sub-tasks the scout added to it) ends here.
    for plan_task in (await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == based_on.id,
            PlanTask.action.in_(PLAN_ACTIONS), PlanTask.status.in_(OPEN_STATUSES),
        )
    )).all():
        plan_task.status = PlanTaskStatus.done
        plan_task.checked_out_by = None
        plan_task.checked_out_at = None
    await _complete_planning_task(session, project)

    _record_activity(
        session, project, "agent", "plan_proposed", f"The scout changed its plan for “{project.title}”.",
        agent_text=changes.strip(),
    )
    await session.flush()
    await round_process.drive_proposed(session, project, new_plan)  # the round's run takes the plan
    return new_plan


async def decide_plan(session: AsyncSession, plan: Plan, auto_because: str) -> Plan:
    """Start a plan awaiting approval: the app approves it on its own account because it asks nothing of the owner
    (`round_process.start_plan`, step PR-6S). It says why, and is recorded as "Started without asking". The owner is
    never asked about a plan, so there is no other decision."""
    await session.refresh(plan)  # one start at a time: the write transaction is the one writer
    if plan.status != PlanStatus.awaiting_approval:
        raise PlanEngineError(f"plan version {plan.version} isn't awaiting approval (status: {plan.status.value})")
    plan.decided_at = datetime.now(UTC)
    project = await session.get(Project, plan.project_id)
    await _approve_plan(session, plan, project, auto_because)
    await session.flush()
    return plan


async def _approve_plan(session: AsyncSession, plan: Plan, project: Project | None, auto_because: str) -> None:
    """The app's own approval: why it could start without asking is `auto_because`."""
    # Approving a later version supersedes any still-awaiting-approval or previously-approved version of this project --
    # only one plan version is ever "the approved one" a checkout may run against.
    others = list((
        await session.scalars(
            select(Plan).where(Plan.project_id == plan.project_id, Plan.id != plan.id,
                               Plan.status.in_((PlanStatus.approved, PlanStatus.awaiting_approval)))
        )
    ).all())
    for other in others:
        other.status = PlanStatus.superseded

    plan.status = PlanStatus.approved
    # In the plan's own order: a task's position is its hand-out order.
    items = list((await session.scalars(
        select(PlanItem).where(PlanItem.plan_id == plan.id).order_by(PlanItem.position)
    )).all())
    for position, item in enumerate(items):
        # An investigate item is never handed out; the review reads it from PlanItem, so it gets no task row.
        if item.action == PlanItemAction.investigate:
            continue
        status = PlanTaskStatus.skipped if item.action == PlanItemAction.skip else PlanTaskStatus.todo
        session.add(PlanTask(
            owner=plan.owner, project_id=plan.project_id, plan_id=plan.id, plan_version=plan.version,
            position=position, item_key=item.key, title=item.title, group_label=item.group_label,
            action=item.action, kind=item.kind, data=item.data, status=status, mode=PlanTaskMode.do,
        ))
    if project is not None:
        _record_activity(
            session, project, "app", "plan_auto_started",
            f"Started without asking: {_one_sentence(plan.summary)}, because {auto_because}.",
        )
