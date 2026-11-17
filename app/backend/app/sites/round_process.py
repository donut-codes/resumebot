"""The project round (the file is the program), with Do one plan item and Check one item, for site projects.

`playbooks/sites/project.round.bpmn` is the round: the scout writes a plan (PR-4); the process decides whether the
server can read the site at all (PR-7N, over the published facts `start.needs_browser`: if not, the site is handed to
the owner's browser, PR-7B; and `start.unreadable`: if it refuses to be read, the site is switched off and the owner
told, PR-7U) and otherwise starts the plan at once (PR-6S): the owner is never asked about a plan;
each item is done by its own run of Do one plan item and checked by its own run of Check one item (code: `site.check`
fetches the site and counts); resumebot writes the round's report (PR-12); the scout learns from it (PR-13, Act); and
the process decides whether to run another round or finish (PR-14). No code decides any of it a second time.

- One live run for each project, with the subject `round:<owner>:<project>`. It starts when the project is created
(PRT-2 the owner, PRT-3 the scout) and waits at the scout's step. A resumed project that needs a new plan starts a new
run at PRT-5; a plan that comes in starts one at PRT-6, with the plan's facts; a project that ends cancels its run.
The engine starts the runs of the items and the checks itself (children of the project's run).
- The steps are the actions registered here. They read facts, or call the same `plans`, `loop`, `plan_check` and
  `escalation` functions as ever; the safeguards live in those functions, never in the files.
- The `drive_*` entries are where the code that owns an event hands it to the engine (an item ending, the checks being
  due). Each feeds the event to the run waiting for it, and the file decides from there.

A run that fails is rolled back in its own savepoint (`live.attempt`) and the work is held at that step: nothing falls
back to other code, so an entry returns a value that tells its caller to stop. `retry` runs the entries again from
today's facts (the Inbox's "couldn't ..." task with Try again is `sites/failures.py`, which calls it).

Copied from another project's code (app/PROVENANCE.md). Not here:
Equip, Staff and login calls, the permission card and the rule safeguards (`_refuse_unless_clear`), a tool
runner, the known-fix plan (PR-4P), the drive pass's watchers (`drive_watch`, `drive_not_needed`,
`drive_unanswered`), the rewrite loop (`drive_rewritten`) and `kept.register`.
"""

from __future__ import annotations

import json
import logging
import uuid

from process_engine import live
from process_engine.engine import Engine
from process_engine.errors import ActionError, NotWaiting, RunOver
from process_engine.loader import ProcessLibrary
from process_engine.models import LIVE, RUNNING, WAITING, ProcInstance
from process_engine.registry import ActionRegistry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites.autostart import do_items
from app.sites.models import Plan, PlanItemAction, PlanStatus, PlanTask, PlanTaskStatus, Project

log = logging.getLogger(__name__)

KEY, LABEL = "project_round", "Project round"
ITEM_KEY, ITEM_LABEL = "do_one_item", "Do one plan item"
CHECK_KEY, CHECK_LABEL = "check_one_item", "Check one item"
OPEN = (RUNNING, WAITING)

# Steps the code feeds: the round's, the item's and the check's.
SCOUT_PLANS = "PR-4"
DO_ITEMS, MAKE_CHECKS_STEP, RUN_CHECKS, ACT = "PR-9", "PR-11A", "PR-11", "PR-13"
HAND_OUT_STEP, SCOUT_DOES, SITE_CHECKS = "Hand_out", "Specialist_does", "Site_checks"

# The actions the three files name.
ASSEMBLE, START_PLAN, NEXT_PLAN = "round.assemble_task", "round.start_plan", "round.next_plan"
TURN_SITE_OFF, HAND_TO_BROWSER = "round.turn_site_off", "round.hand_to_browser"
MAKE_CHECKS, WRITE_REPORT, FINISH, RETRY_PLANNER = (
    "round.make_checks", "round.write_report", "round.finish", "round.retry_planner")
HAND_OUT, READ_ITEM, MAKE_CHECK, SITE_CHECK = "item.hand_out", "item.read", "check.make", "site.check"

#: `session.info` keys: the Act task and the next planning task the steps made (handed back to the code that asked), and
#: the planning task the pass found unfinished.
ACT_KEY, NEXT_KEY, PLANNER_KEY = "round_act", "round_next_plan", "round_planner_failed"

#: The actions the three process files name, in a registry of their own (the job-search files have theirs).
registry = ActionRegistry()

#: A test swaps its own library in here. None means the shared one (`app.sites.process`).
_library: ProcessLibrary | None = None


def library() -> ProcessLibrary:
    """The site processes' library, unless a test swapped one in. A file that doesn't load raises `ProcessLoadError`."""
    if _library is not None:
        return _library
    from app.sites import process  # process imports this module's registry

    return process.library()


def subject_of(owner: str, project_id: uuid.UUID) -> str:
    """One open run per project (so two owners or projects never share one)."""
    return f"round:{owner}:{project_id}"


# --- what the code saw ------------------------------------------------------------------------------------------


def _text(value, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


async def plan_facts(session: AsyncSession, project, plan) -> dict:
    """The plan as the file's `plan` reads it: its id and version, and its `do` items."""
    from app.sites.connections import _content_or_none

    content = await _content_or_none(session, project.owner)
    items = []
    for item in await do_items(session, plan):
        spec = content.kinds.get(item.kind or "") if content is not None else None
        items.append({
            "key": item.key, "title": _text(item.title), "kind": item.kind or "",
            "writes": spec is None or spec.risk != "read_only",
        })
    return json.loads(json.dumps({"id": str(plan.id), "version": plan.version, "items": items}, default=str))


async def start_facts(session: AsyncSession, project, plan) -> dict:
    """The facts the file's gateway reads: `unreadable` (`autostart.unreadable_clause`: the plan says the site asks not
    to be read, so nothing can read it; a project past its rounds is `unreadable` too, `autostart.ROUND_CAP`),
    `needs_browser` (`autostart.needs_browser`: the plan says the server cannot read it, its listings only load in a
    browser, but the browser has not been tried; never with `unreadable`) and `why` (the clause that says why, for
    whichever it is). Never both: a site the server cannot read asks the owner nothing. Reads only."""
    from app.sites import autostart

    clause = await autostart.unreadable_clause(session, plan)
    browser = clause is None and await autostart.needs_browser(session, plan)
    return {
        "unreadable": clause is not None, "needs_browser": browser,
        "why": clause or (autostart.BROWSER_CLAUSE if browser else ""),
    }


async def checks_facts(session: AsyncSession, project, data: dict) -> dict:
    """The items that were tried and are to be checked, each with how it ended and what it found: its `status`, the
    reason it went off plan, its `evidence`, `result` and `data`. `site.check` reads them."""
    try:
        plan_id = uuid.UUID(str((data.get("plan") or {}).get("id")))
    except ValueError:
        return {"items": []}
    rows = (await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == plan_id, PlanTask.action == PlanItemAction.do,
            PlanTask.status.in_((PlanTaskStatus.done, PlanTaskStatus.off_plan)), PlanTask.superseded.is_(False),
        ).order_by(PlanTask.position)
    )).all()
    return {"items": [
        {
            "key": t.item_key, "title": _text(t.title), "kind": t.kind or "", "status": t.status.value,
            "outcome_reason": _text(t.outcome_reason, 300), "evidence": _text(t.evidence, 1500),
            "result": t.result or {}, "data": {k: v for k, v in (t.data or {}).items() if k != "handout"},
        }
        for t in rows
    ]}


def _plan_version(instance: ProcInstance) -> int | None:
    return (instance.data or {}).get("plan", {}).get("version")


# --- the actions the file names (what the process runs when it switches) -------------------------------------------


async def _project(session, data: dict) -> Project:
    try:
        project = await session.get(Project, uuid.UUID(str(data.get("project"))))
    except ValueError:
        project = None
    if project is None:
        raise ActionError("no_project", "The project this round is for is gone.")
    return project


async def _plan(session, data: dict) -> Plan:
    ref = data.get("plan")
    try:
        plan = await session.get(Plan, uuid.UUID(str(ref.get("id") if isinstance(ref, dict) else ref)))
    except ValueError:
        plan = None
    if plan is None:
        raise ActionError("no_plan", "The plan this round is working on is gone.")
    return plan


async def _item_task(session, project, plan_id: str, key: str) -> PlanTask:
    task = await session.scalar(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == uuid.UUID(plan_id), PlanTask.item_key == key,
            PlanTask.action == PlanItemAction.do, PlanTask.superseded.is_(False),
        ).order_by(PlanTask.created_at.desc()).limit(1)
    )
    if task is None:
        raise ActionError("no_item", "The item this run is for is gone.")
    return task


async def _check_task(session, project, plan_id: str, key: str) -> PlanTask:
    """The check made for this item with the plan's others (`plan_check._create_plan_checks`)."""
    from app.sites.plan_check import _plan_checks

    task = next((t for t in await _plan_checks(session, project.id, uuid.UUID(plan_id))
                 if (t.data or {}).get("key") == key), None)
    if task is None:
        raise ActionError("no_check", "The check this item waits for isn't there.")
    return task


async def _open_plan_task(session, project) -> PlanTask | None:
    """The project's open planning task ("Write plan vN"), the newest."""
    return await session.scalar(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.action == PlanItemAction.plan, PlanTask.superseded.is_(False),
            PlanTask.status.in_((PlanTaskStatus.todo, PlanTaskStatus.in_progress, PlanTaskStatus.waiting)),
        ).order_by(PlanTask.created_at.desc(), PlanTask.position.desc()).limit(1)
    )


def _item_key(data: dict) -> str:
    return (data.get("item") or {}).get("key") or ""


async def _assemble_action(session, instance, data) -> dict:
    """The plan version the planning task asks the scout for."""
    from app.sites.records import _next_version

    project = await _project(session, data)
    task = await _open_plan_task(session, project)
    version = ((task.context or {}).get("write_version") if task is not None else None) or await _next_version(
        session, project.id)
    return {"version": int(version)}


async def _start_plan_action(session, instance, data) -> dict:
    """PR-6S: the process decided nothing in the plan needs the owner, so it is approved on the app's own account, its
    tasks are made and the trail says "Started without asking: ..., because ...".

    A plan that is no longer awaiting approval was settled before this run came here: only a run moved onto this file
    from the one that asked the owner (`Engine.move_stranded`) can meet one. An approved plan has its tasks, so there
    is nothing to decide and the round goes on to its items. Any other (declined, sent back, replaced) belongs to a
    round that is over, as the old file ended it: the run ends, the project stays as the owner left it (a declined
    plan's project is paused, and Resume asks for a fresh plan: `completion.resume_project`)."""
    from app.sites import plans

    plan = await _plan(session, data)
    await session.refresh(plan)  # one start at a time, as `decide_plan` takes it
    if plan.status == PlanStatus.approved:
        return {"because": "it was started before"}
    if plan.status != PlanStatus.awaiting_approval:
        raise RunOver(f"plan version {plan.version} was {plan.status.value} before this round came to start it")
    because = "it only reads public pages"
    await plans.decide_plan(session, plan, auto_because=because)
    return {"because": because}


async def _hand_to_browser_action(session, instance, data) -> dict:
    """PR-7B: the plan says the server cannot read the site but the browser has not been tried, so the process asks
    nobody: the project ends and the site is set to be read in the owner's browser, which decides
    (`unreadable.site_needs_browser`). Nothing is switched off and the owner is told nothing."""
    from app.sites import unreadable

    project = await _project(session, data)
    why = (data.get("start") or {}).get("why") or ""
    await unreadable.site_needs_browser(session, project, why)
    return {"why": why}


async def _turn_site_off_action(session, instance, data) -> dict:
    """PR-7U: the plan says the site asks not to be read, so the process asks nobody: the project ends, the site is
    switched off and the owner is told once (`unreadable.site_cannot_be_read`)."""
    from app.sites import unreadable

    project = await _project(session, data)
    why = (data.get("start") or {}).get("why") or ""
    await unreadable.site_cannot_be_read(session, project, why)
    return {"why": why}


async def _make_checks_action(session, instance, data) -> dict:
    from app.sites.plan_check import _create_plan_checks

    project = await _project(session, data)
    await _create_plan_checks(session, project, await _plan(session, data))
    return await checks_facts(session, project, data)


async def _write_report_action(session, instance, data) -> dict:
    from app.sites.plan_check import _investigate_count, closable_by_act, write_check_report

    plan = await _plan(session, data)
    report = await write_check_report(session, await _project(session, data), plan)
    checks = data.get("checked") or []
    return {
        "verdict": report.verdict, "on_plan": sum(1 for c in checks if c is True),
        "off_plan": sum(1 for c in checks if c is False), "more": await _investigate_count(session, plan),
        "closable": await closable_by_act(session, plan, report),
    }


async def _next_plan_action(session, instance, data) -> dict:
    from app.sites import loop
    from app.sites.records import report_for_version

    project, plan = await _project(session, data), await _plan(session, data)
    report = await report_for_version(session, project.id, plan.version)
    if report is None:
        raise ActionError("no_report", "The round has no report to ask for the next plan from.")
    task = await loop._loop_task(session, project.id, plan.id, PlanItemAction.plan)
    if task is None:
        task = await loop._create_next_plan_task(session, project, plan, report)
    session.info[NEXT_KEY] = task
    return {"version": int((task.context or {}).get("write_version") or plan.version + 1)}


async def _finish_action(session, instance, data) -> dict:
    from app.sites.loop import _finish_from_report
    from app.sites.records import report_for_version

    project, plan = await _project(session, data), await _plan(session, data)
    report = await report_for_version(session, project.id, plan.version)
    if report is None:
        raise ActionError("no_report", "The round has no report to finish from.")
    await _finish_from_report(session, project, report)
    return {"outcome": (project.resolution or {}).get("outcome") or ""}


async def _retry_planner_action(session, instance, data) -> dict:
    """PR-18: tries the planning task again that the scout didn't finish (the one the drive pass found and handed to the
    step, `drive_planner_failed`)."""
    from app.sites import escalation

    found = session.info.get(PLANNER_KEY)
    task = await session.get(PlanTask, uuid.UUID(found["task"])) if found else None
    if task is None:
        raise ActionError("no_planner_run", "No planning task is waiting to be tried again.")
    found["woken"] = await escalation.planner_run_failed(
        session, await _project(session, data), task, found["why"], found["now"])
    return {}


async def _hand_out_action(session, instance, data) -> dict:
    """The item as it is handed out, from its own task as it stands now."""
    kind = (data.get("item") or {}).get("kind") or ""
    task = None
    if data.get("plan"):
        try:
            task = await _item_task(session, await _project(session, data), str(data["plan"]), _item_key(data))
        except (ActionError, ValueError):
            task = None
    if task is None:
        return {"task_id": "", "kind": kind}
    return {"task_id": str(task.id), "kind": task.kind or kind}


async def _read_item_action(session, instance, data) -> dict:
    """How the item ended, done or off plan, with the scout's own reason."""
    task = await _item_task(session, await _project(session, data), str(data.get("plan")), _item_key(data))
    return {
        "outcome": "done" if task.status == PlanTaskStatus.done else "off_plan",
        "said": _text(task.outcome_reason, 300),
    }


async def _make_check_action(session, instance, data) -> dict:
    task = await _check_task(session, await _project(session, data), str(data.get("plan")), _item_key(data))
    return {"task_id": str(task.id)}


async def _site_check_action(session, instance, data) -> dict:
    """Check is code: `site_check.site_check` (the site skills branch writes it) fetches the site and counts what came
    back, and its verdict is stored as the check's finding. The scout's own word is never the evidence."""
    from app.sites import check_flow, site_check

    project = await _project(session, data)
    check = await _check_task(session, project, str(data.get("plan")), _item_key(data))
    found = await site_check.site_check(session, instance, data)
    on_plan = found.get("on_plan")
    if on_plan is None:
        on_plan = found.get("verdict") == "on_plan"
    difference = _text(found.get("difference"), 300)
    if not on_plan and not difference:
        difference = "It differs from the plan."
    await check_flow.complete_plan_check(session, check, project, bool(on_plan), difference or None)
    return {"on_plan": bool(on_plan), "difference": "" if on_plan else difference}


def _register(name, fn, inputs, outputs, description, source) -> None:
    registry.register(name, fn, inputs=inputs, outputs=outputs, description=description, source=source)


_register(ASSEMBLE, _assemble_action, ["owner", "project"], ["version"],
          "Reads which plan version the scout's planning task asks for. Reads only; the task is made where a round "
          "starts.", "sites/loop.py:_create_next_plan_task; sites/task_context.py:task_view")
_register(START_PLAN, _start_plan_action, ["owner", "project", "plan.id"], ["because"],
          "Approves the plan on resumebot's own account because the process decided nothing in it needs the owner, "
          "makes its tasks (one for each item, in plan order) and says so in the trail.", "sites/plans.py:decide_plan")
_register(TURN_SITE_OFF, _turn_site_off_action, ["owner", "project", "start.why"], ["why"],
          "Asks nobody: the plan says the site asks not to be read, so the project ends, the site is switched off "
          "with the reason recorded and the owner is told once, in a notification, not a decision.",
          "sites/unreadable.py:site_cannot_be_read; sites/where.py:turn_off_unreadable")
_register(HAND_TO_BROWSER, _hand_to_browser_action, ["owner", "project", "start.why"], ["why"],
          "Asks nobody: the plan says the server can't read the site (its listings only load in a browser) but the "
          "browser has not been tried, so the project ends and the site is set to be read in the owner's browser, "
          "which decides. Nothing is switched off and the owner is told nothing.",
          "sites/unreadable.py:site_needs_browser; sites/where.py:hand_to_browser")
_register(MAKE_CHECKS, _make_checks_action, ["owner", "project", "plan.id"], ["items"],
          "Makes one check for each item that was tried (done or off plan, never one that wasn't tried).",
          "sites/plan_check.py:_create_plan_checks; sites/check_flow.py:advance_plan_checks")
_register(WRITE_REPORT, _write_report_action, ["owner", "project", "plan.id", "checked"],
          ["verdict", "on_plan", "off_plan", "more", "closable"],
          "Writes the round's report with no model: how many checks were on plan and off plan, grouped by difference; "
          "iterate when something is off plan or still to look into, else accepted.",
          "sites/plan_check.py:write_check_report, _investigate_count")
_register(NEXT_PLAN, _next_plan_action, ["owner", "project", "plan.id"], ["version"],
          "Makes \"Write plan v(n+1)\" from what the round's review found, once for each version.",
          "sites/loop.py:_create_next_plan_task, advance_after_report")
_register(FINISH, _finish_action, ["owner", "project", "plan.id"], ["outcome"],
          "Finishes the project: marks it done, writes its three-part resolution (the scout's own when it wrote one) "
          "and supersedes open plans.", "sites/loop.py:_finish_from_report; sites/resolution.py:close")
_register(RETRY_PLANNER, _retry_planner_action, ["owner", "project"], [],
          "Tries a planning task again that the scout didn't finish: at once the first time, after 15 minutes the "
          "second, and on the third asks the owner whether to try again, pause or stop.",
          "sites/escalation.py:planner_run_failed, retry_planner_runs")
_register(HAND_OUT, _hand_out_action, ["owner", "project", "plan", "item.key", "item.kind"], ["task_id", "kind"],
          "Reads how an item is handed out and the kind of work it is. Reads only; the hand-out happens when the "
          "scout asks for its next task.", "sites/checkout.py")
_register(READ_ITEM, _read_item_action, ["owner", "project", "plan", "item.key"], ["outcome", "said"],
          "Reads how the item ended, done or off plan, and the scout's own reason. Reads only.",
          "sites/completion.py:complete_task")
_register(MAKE_CHECK, _make_check_action, ["owner", "project", "plan", "item.key"], ["task_id"],
          "Reads the check made for the item with the plan's others. Reads only.", "sites/plan_check.py")
_register(SITE_CHECK, _site_check_action, ["owner", "project", "plan", "item.key"], ["on_plan", "difference"],
          "Check is code: fetches the site itself and counts what came back (postings, how their number compares with "
          "what the page says, titles and links, sample links that open). Says on plan, or off plan with the "
          "difference; the scout's own word is never the evidence.",
          "sites/site_check.py:site_check; sites/check_flow.py:complete_plan_check")


# --- the entries: where the code that owns an event hands it to the engine ---------------------------------------
#
#   projects.create_project, completion.resume_project                     -> drive_opened, drive_restarted
#   plans.propose_plan / revise_plan                                       -> drive_proposed
#   checkout.permit_task                                                   -> drive_hand_out
#   completion.complete_task, checkout.permit_task                         -> drive_item_ended
#   check_flow.advance_plan_checks                                         -> drive_make_checks
#   check_flow.complete_plan_check                                         -> drive_check_ended
#   loop.advance_after_report                                              -> drive_after_report
#   escalation.end_project                                                 -> drive_ended
#   escalation.retry_planner_runs                                          -> drive_planner_failed


async def _attempt(session, project, key: str, what: str, work, *, savepoint: bool = True):
    """`live.attempt` for this project: `subject` is the project's run (so Try again knows which to run again), and the
    round is marked as running too, because an item's or a check's run is one of the round's, so nothing it calls into
    starts the round's own entries a second time."""

    async def inner():
        token = live._inside.set(live._inside.get() | {KEY})
        try:
            return await work()
        finally:
            live._inside.reset(token)

    return await live.attempt(
        session, project.owner, key, what, inner, savepoint=savepoint, subject=subject_of(project.owner, project.id))


async def _attempt_or_restore(session, project, key: str, what: str, work, *held):
    """`_attempt`, and when the work failed, what the entry had changed (`held`: the project, the plan, the task...) is
    read back as it was (`live.restore`), so the work is held at its step and nothing half-done is left. Every entry
    ends this way; each turns the returned attempt (`ok`, `value`) into its own answer."""
    done = await _attempt(session, project, key, what, work)
    if not done.ok:
        await live.restore(session, *held)
    return done


async def _driving(session, project) -> bool:
    """Whether this project's events go to its run: not from inside one of its own steps (an action that calls back into
    the code that owns the event, as the step that approves a plan does)."""
    return not live.inside(KEY)


async def _item_driving(session, project) -> bool:
    return not live.inside(ITEM_KEY)


async def _open_runs(session, project, *, with_pass: bool = False) -> list[ProcInstance]:
    """The project's open live runs of the round (not the items' or checks' children), oldest first."""
    subject = subject_of(project.owner, project.id)
    subjects = (subject, subject + ":pass") if with_pass else (subject,)
    return list((await session.scalars(
        select(ProcInstance).where(
            ProcInstance.process_key == KEY, ProcInstance.mode == LIVE, ProcInstance.status.in_(OPEN),
            ProcInstance.parent_id.is_(None), ProcInstance.subject.in_(subjects),
        ).order_by(ProcInstance.created_at)
    )).all())


async def _live_run(session, project) -> ProcInstance | None:
    return await live.open_live(session, KEY, subject_of(project.owner, project.id))


async def _where(session, instance: ProcInstance) -> dict:
    return await Engine(library(), registry).where_is(session, instance.id)


async def _step_of(session, instance: ProcInstance) -> str | None:
    return (await _where(session, instance))["step"] if instance.status in OPEN else None


async def _waiting_at(session, instance: ProcInstance) -> set[str]:
    """The steps an open run has a wait at (what the code can feed), not the step its token merely sits on."""
    if instance.status not in OPEN:
        return set()
    return {w["step"] for w in (await _where(session, instance))["waiting_for"]}


async def _start_live(
    session, project, start: str, *, plan: dict | None = None, started: dict | None = None, why: str,
) -> ProcInstance:
    """A new run of the round at the start event `start`, setting aside the project's earlier one."""
    engine = live.live_engine(KEY)
    for earlier in await _open_runs(session, project):
        await engine.cancel(session, earlier.id, why)
    data: dict = {"owner": project.owner, "project": str(project.id)}
    if project.site_id is not None:
        data["site"] = str(project.site_id)
    if plan is not None:
        data["plan"] = plan
    if started is not None:
        data["start"] = started
    return live.ensure_ok(await engine.start(
        session, KEY, data, subject=subject_of(project.owner, project.id), start_event=start))


async def drive_opened(session: AsyncSession, project, start: str) -> bool:
    """A project was created (`start` is its PRT) with its first planning task: the engine opens its run and waits at
    the scout's step. True when the run is open."""
    if not await _driving(session, project):
        return False
    await session.flush()

    async def work() -> bool:
        instance = await _start_live(session, project, start, why="the round was asked for again")
        if await _step_of(session, instance) != SCOUT_PLANS:
            raise live.DriveFailed("a new project's round isn't waiting for a plan")
        return True

    done = await _attempt_or_restore(session, project, KEY, "start a project's round", work, project)
    return done.ok


async def drive_restarted(session: AsyncSession, project) -> bool:
    """The owner resumed a paused project that needs a new plan (PRT-5): a fresh planning task was made, and the
    engine starts a new run that waits for it. True when it did."""
    if not await _driving(session, project):
        return False
    await session.flush()

    async def work() -> bool:
        instance = await _start_live(session, project, "PRT-5", why="the round was asked for again")
        if await _step_of(session, instance) != SCOUT_PLANS:
            raise live.DriveFailed("a new plan was asked for, but the round isn't waiting for it")
        return True

    done = await _attempt_or_restore(session, project, KEY, "ask for a new plan", work, project)
    return done.ok


async def drive_proposed(session: AsyncSession, project, plan) -> bool:
    """The scout's plan was stored: the engine takes it (PRT-6) with the facts `start.unreadable` and
    `start.needs_browser`, and the file decides. A plan that says its site asks not to be read asks no one: the site is
    switched off and the owner told (PR-7U), and the run ends at E-unreadable. One that says the server can't read the
    site but a browser might asks no one either: the site is handed to the owner's browser (PR-7B) and the run ends at
    E-browser. Every other plan starts at once (PR-6S: its tasks are made and each item's run waits to be handed out).
    True when the engine took it."""
    if not await _driving(session, project):
        return False
    await session.flush()

    async def work() -> bool:
        asks = await start_facts(session, project, plan)
        instance = await _start_live(
            session, project, "PRT-6", plan=await plan_facts(session, project, plan), started=asks,
            why="a new plan replaced the one the run waited on")
        where = await _step_of(session, instance)
        for fact, end_id in (("unreadable", "E-unreadable"), ("needs_browser", "E-browser")):
            if asks[fact]:
                end = await live.last_end(session, instance) if where is None else None
                if end != end_id:
                    raise live.DriveFailed(f"a plan said its site is '{fact}' and the round ended at '{where or end}'")
                return True
        if where not in (DO_ITEMS, MAKE_CHECKS_STEP):
            raise live.DriveFailed(f"a plan came in and the round is waiting at '{where}', which it shouldn't be")
        return True

    done = await _attempt_or_restore(session, project, KEY, "take a plan", work, project, plan)
    return done.ok


async def drive_make_checks(session: AsyncSession, project, plan) -> list | None:
    """Every item of the approved plan ended (`advance_plan_checks`): the engine's runs of Do one plan item are all done
    and it waits to make the checks. It then starts a run of Check one item for each tried item (code fetches the site
    and counts, `site.check`), and when the last one has ended the run writes the report and hands the round to Act.
    Returns the checks that were made, or None when there is nothing to do: the round isn't waiting there (or has no
    run), another caller made them first, or the run could not and the work is held.

    The read of where the round waits takes no lock, so two callers (a scout that asked again while the first request
    was still making the checks) can both read it waiting here before either has committed. The engine answers the
    second with `NotWaiting` once it holds the run's lock: the step was answered, so there is nothing left for it to
    do, and that is not a failure (`live.attempt` lets it through for the caller to say so)."""
    if not await _driving(session, project):
        return None
    run = await _live_run(session, project)
    if run is None or _plan_version(run) != plan.version or MAKE_CHECKS_STEP not in await _waiting_at(session, run):
        return None
    await session.flush()

    async def work() -> list:
        from app.sites.plan_check import _create_plan_checks, _plan_checks

        await _create_plan_checks(session, project, plan)
        made = await _plan_checks(session, project.id, plan.id)
        facts = await checks_facts(session, project, run.data or {})
        if len(facts["items"]) != len(made):
            raise live.DriveFailed("the checks that were made aren't the items that were tried")
        instance = live.ensure_ok(await live.live_engine(KEY).complete(session, run.id, MAKE_CHECKS_STEP, facts))
        where = await _step_of(session, instance)
        if where not in (RUN_CHECKS, ACT):
            raise live.DriveFailed(f"the checks were made and the round waits at '{where}', which it shouldn't")
        return made

    try:
        done = await _attempt_or_restore(session, project, KEY, "make the round's checks", work, project, plan)
    except NotWaiting:
        # The savepoint undid the attempt: nothing changed and nothing is held. Read the rows back, as for any undo.
        await live.restore(session, project, plan)
        return None
    if not done.ok:
        return None
    return done.value


async def drive_check_ended(
    session: AsyncSession, project, task, on_plan: bool, difference: str | None,
) -> bool | None:
    """A check ended (`check_flow.complete_plan_check`, after the finding was stored): when its run of Check one item
    waits for it, the run decides whether the item is on plan. A code check runs inside its own run, so nothing waits
    for it and this does nothing; it is the entry a `site.check` the code feeds (a deferred step) would use. True when
    it did; None when there is no run waiting or it could not (the work is held)."""
    if task.plan_id is None or live.inside(CHECK_KEY):
        return None
    child = await _live_child(
        session, project, CHECK_KEY, task.plan_id, (task.data or {}).get("key") or task.item_key, steps={SITE_CHECKS})
    if child is None:
        return None
    await session.flush()

    async def work() -> bool:
        instance = live.ensure_ok(await live.live_engine(CHECK_KEY).complete(
            session, child.id, SITE_CHECKS, {"on_plan": bool(on_plan), "difference": _text(difference, 300)}))
        if await live.last_end(session, instance) != ("End_on_plan" if on_plan else "End_off_plan"):
            raise live.DriveFailed("a check ended where a check shouldn't end")
        return True

    done = await _attempt_or_restore(session, project, CHECK_KEY, "decide whether an item is on plan", work, task)
    if not done.ok:
        return None
    return True


async def drive_after_report(session: AsyncSession, project, plan) -> tuple:
    """`advance_after_report` found the round's report. When the round waits for Act, it hands Act out (the task was
    made when it reached the step), waits while Act is open, and when Act ended decides: finish the project or ask
    for the next plan. Returns what `advance_after_report` returns: `(task, reason)`, or `(None, None)` when
    nothing is due (the round isn't at Act, Act is held by someone, or the run could not and the work is held)."""
    from app.sites import loop, resolution
    from app.sites.base import OPEN_STATUSES

    if not await _driving(session, project):
        return None, None
    run = await _live_run(session, project)
    if run is None or _plan_version(run) != plan.version or ACT not in await _waiting_at(session, run):
        return None, None
    made = session.info.pop(ACT_KEY, None)
    if made is not None:
        return made, "act_ready"
    act = await loop._loop_task(session, project.id, plan.id, PlanItemAction.act)
    if act is None or act.status in OPEN_STATUSES:
        return None, None
    await session.flush()

    async def work() -> tuple:
        instance = live.ensure_ok(await live.live_engine(KEY).complete(session, run.id, ACT, {
            "summary": _text(act.evidence, 300), "resolved": resolution.of_act(act) is not None,
        }))
        where = await _step_of(session, instance)
        if where is None:
            if await live.last_end(session, instance) != "E-finished":
                raise live.DriveFailed("Act ended and the round ended where a round shouldn't end")
            return None, "finished"
        if where == SCOUT_PLANS:
            return session.info.pop(NEXT_KEY, None), "plan_ready"
        raise live.DriveFailed(f"Act ended and the round is waiting at '{where}', which it shouldn't be")

    done = await _attempt_or_restore(
        session, project, KEY, "finish the project or ask for the next plan", work, project, plan, act)
    session.info.pop(NEXT_KEY, None)
    if not done.ok:
        return None, None
    return done.value


async def drive_planner_failed(session: AsyncSession, project, task, why: str, now) -> tuple[bool, tuple | None]:
    """The drive pass found a planning task the scout didn't finish (`escalation.retry_planner_runs`). The engine's
    run of the pass decides the scout is asked again (PR-18; the same `planner_run_failed`). The pass is a run of
    its own (`...:pass`), so it never replaces the run that carries the round. Returns `(True, the wake to send)`
    when it did (the wake is None when the run could not and the work is held), `(False, None)` for a project with
    no round."""
    if not await _driving(session, project):
        return False, None
    await session.flush()
    session.info[PLANNER_KEY] = {"task": str(task.id), "why": why, "now": now}

    async def work() -> tuple | None:
        engine = live.live_engine(KEY)
        subject = subject_of(project.owner, project.id) + ":pass"
        for earlier in await _open_runs(session, project, with_pass=True):
            if earlier.subject == subject:
                await engine.cancel(session, earlier.id, "the pass found a planner that didn't finish")
        instance = live.ensure_ok(await engine.start(
            session, KEY, {"owner": project.owner, "project": str(project.id)}, subject=subject,
            start_event="PRT-8"))
        end = await live.last_end(session, instance)
        if not live.finished(instance) or end != "E-retried":
            raise live.DriveFailed(f"the drive pass ended at '{end}', which isn't asking a planner again")
        return session.info[PLANNER_KEY].get("woken")

    done = await _attempt_or_restore(
        session, project, KEY, "try a planner that didn't finish again", work, project, task)
    session.info.pop(PLANNER_KEY, None)
    if not done.ok:
        return True, None
    return True, done.value


async def drive_ended(session: AsyncSession, project) -> None:
    """A project ended outside the loop: the engine's run of it is set aside."""
    if live.inside(KEY):
        return
    run = await _live_run(session, project)
    if run is not None:
        await live.live_engine(KEY).cancel(session, run.id, "the project ended")


# --- Do one plan item ---------------------------------------------------------------------------------------------


async def _live_child(
    session, project, process: str, plan_id, key: str, *, steps: set[str] | None = None,
) -> ProcInstance | None:
    """The engine's open run of `process` for the item `key` of the plan, a child of the project's run, and (when
    `steps` is given) only while it has a wait at one of them."""
    rows = (await session.scalars(
        select(ProcInstance).where(
            ProcInstance.process_key == process, ProcInstance.mode == LIVE, ProcInstance.status.in_(OPEN),
            ProcInstance.subject == subject_of(project.owner, project.id), ProcInstance.parent_id.is_not(None),
        ).order_by(ProcInstance.item_index)
    )).all()
    for child in rows:
        data = child.data or {}
        if str(data.get("plan")) != str(plan_id) or _item_key(data) != key:
            continue
        if steps is None or steps & await _waiting_at(session, child):
            return child
    return None


GONE_ON, HELD = "gone_on", "held"


async def drive_hand_out(session: AsyncSession, project, task, connection_id: uuid.UUID | None = None) -> str:
    """An item is being handed out (`checkout.permit_task`, the one door every hand-out passes through): its run, which
    waits at the first step, reads how it is handed out now and goes on. Returns `GONE_ON` when the run read it and goes
    on to the scout (or had read it already, or has no run: an item this round never started), `HELD` when the run could
    not read it (the step was undone and the item is not handed out)."""
    if task.plan_id is None:
        return GONE_ON
    child = await _live_child(session, project, ITEM_KEY, task.plan_id, task.item_key, steps={HAND_OUT_STEP})
    if child is None:
        return GONE_ON
    await session.flush()

    async def work() -> str:
        instance = live.ensure_ok(await live.live_engine(ITEM_KEY).complete(
            session, child.id, HAND_OUT_STEP, await _hand_out_action(session, child, child.data or {})))
        where = await _step_of(session, instance)
        if where != SCOUT_DOES:
            raise live.DriveFailed(f"an item was handed out and its run is waiting at '{where}', not at the scout")
        return GONE_ON

    done = await _attempt_or_restore(session, project, ITEM_KEY, "hand an item out", work, task)
    if not done.ok:
        return HELD
    return done.value


async def drive_item_ended(session: AsyncSession, project, task) -> bool | None:
    """A do item ended, done or off plan (`complete_task`; or refused by the kind rules, `permit_task`). When the engine
    drives the item and its run waits for the scout, the engine reads how it ended and decides: done, or off plan (a
    finding). True when it did; None when it isn't driving, has no run waiting for this item, or could not (held)."""
    if task.action != PlanItemAction.do or task.plan_id is None or not await _item_driving(session, project):
        return None
    if await drive_hand_out(session, project, task) == HELD:  # an item that ended without passing the hand-out door
        return None
    child = await _live_child(session, project, ITEM_KEY, task.plan_id, task.item_key, steps={SCOUT_DOES})
    if child is None:
        return None
    await session.flush()

    async def work() -> bool:
        outcome = "done" if task.status == PlanTaskStatus.done else "off_plan"
        instance = live.ensure_ok(await live.live_engine(ITEM_KEY).complete(
            session, child.id, SCOUT_DOES, {"outcome": outcome}))
        if await _step_of(session, instance) is not None or await live.last_end(session, instance) not in (
            "End_done", "End_off_plan"
        ):
            raise live.DriveFailed("an item's run ended where an item shouldn't end")
        return True

    done = await _attempt_or_restore(session, project, ITEM_KEY, "decide how an item ended", work, task)
    if not done.ok:
        return None
    return True


async def catch_up(session: AsyncSession, project, plan) -> None:
    """Called where the plan's checks are due (`check_flow.advance_plan_checks`): an item that ended while its run could
    not read it (the step was held, and the scout reported it once and won't again) is read again, from the records of
    how it ended. Cheap when nothing is out of step: one read of the runs that wait."""
    if not await _driving(session, project):
        return
    children = (await session.scalars(select(ProcInstance).where(
        ProcInstance.process_key == ITEM_KEY, ProcInstance.mode == LIVE, ProcInstance.status.in_(OPEN),
        ProcInstance.subject == subject_of(project.owner, project.id), ProcInstance.parent_id.is_not(None),
    ))).all()
    keys = {_item_key(c.data or {}) for c in children if str((c.data or {}).get("plan")) == str(plan.id)}
    if not keys:
        return
    for task in (await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == plan.id, PlanTask.action == PlanItemAction.do,
            PlanTask.superseded.is_(False), PlanTask.item_key.in_(keys),
            PlanTask.status.in_((PlanTaskStatus.done, PlanTaskStatus.off_plan, PlanTaskStatus.skipped)),
        ).order_by(PlanTask.position)
    )).all():
        await drive_item_ended(session, project, task)


# --- the hand-outs ---------------------------------------------------------------------------------------------------


async def _send(session: AsyncSession, wait, task, view) -> None:
    """`agent_handout.hand_out`, except that a step whose instruction or response template file is gone still goes out:
    a task with no stamp is sent exactly as it always was, rather than held for good."""
    from app.sites import agent_handout

    try:
        await agent_handout.hand_out(session, wait, task, view)
    except agent_handout.MissingFile:
        log.warning("the step '%s' names a file that isn't there; its task goes out without the step's stamp",
                    wait.element_id)


async def _item_agent_handout(session: AsyncSession, instance: ProcInstance, wait) -> None:
    """The scout's step of an item is waiting: its task is the record `task.next` hands out. The step says what is sent
    (its role, instruction file and response template go on the task) and the payload is built here too, to see that it
    fits the budget."""
    from app.sites.task_context import task_view

    data = instance.data or {}
    project = await _project(session, data)
    task = await _item_task(session, project, str(data.get("plan")), _item_key(data))

    async def view(s, t):
        return await task_view(s, project, t)

    await _send(session, wait, task, view)


async def _agent_handout(session: AsyncSession, instance: ProcInstance, wait) -> None:
    """The scout's step is waiting. The planning task is the record `task.next` hands out; the Act task is made here,
    when the report is written, as `advance_after_report` always made it."""
    from app.sites import loop
    from app.sites.records import report_for_version
    from app.sites.task_context import task_view

    data = instance.data or {}
    project = await _project(session, data)

    async def view(s, t):
        return await task_view(s, project, t)

    if wait.element_id == SCOUT_PLANS:
        task = await _open_plan_task(session, project)
        if task is None:
            return  # an agent that started the project writes its own plan: there is no planning task to stamp
    elif wait.element_id == ACT:
        plan = await _plan(session, data)
        report = await report_for_version(session, project.id, plan.version)
        if report is None:
            raise ActionError("no_report", "Act waits for a report that isn't written.")
        task = await loop._loop_task(session, project.id, plan.id, PlanItemAction.act)
        if task is None:
            task = await loop._create_act_task(session, project, plan, report)
            session.info[ACT_KEY] = task
    else:
        raise ActionError("no_step", f"resumebot doesn't know how to hand out the step '{wait.element_id}'.")
    await _send(session, wait, task, view)


#: What the engine can't do inside its own run, for all three processes alike: the making of the checks (made where they
#: always were, so the engine hears which items they are for) and the hand-out of an item (read when the item is handed
#: out, by `drive_hand_out`, not when the plan is approved).
_DEFERRED = frozenset({MAKE_CHECKS, HAND_OUT})


async def retry(session: AsyncSession, owner: str, subject: str | None) -> None:
    """Try again, after a failure was held: run what the entries do for the project of `subject` (every active project
    of the owner when none was named) again, from today's facts. A plan waiting to be read is read, a project
    waiting for a plan is opened again, and an approved plan's checks, report and Act are advanced. A second
    failure is held the same way."""
    from app.sites import check_flow

    query = select(Project).where(
        Project.owner == owner, Project.stopped_at.is_(None), Project.finished_at.is_(None))
    try:
        if subject:
            query = query.where(Project.id == uuid.UUID(subject.rsplit(":", 1)[-1]))
    except ValueError:
        pass
    for project in (await session.scalars(query)).all():
        plan = await session.scalar(
            select(Plan).where(Plan.project_id == project.id).order_by(Plan.version.desc()).limit(1))
        run = await _live_run(session, project)
        if plan is not None and plan.status == PlanStatus.awaiting_approval and (
            run is None or _plan_version(run) != plan.version
        ):
            await drive_proposed(session, project, plan)
        elif plan is not None and plan.status == PlanStatus.approved:
            await check_flow.advance_plan_checks(session, project, plan)
        elif plan is None and run is None:
            await drive_opened(session, project, "PRT-2")


live.register_drivable(
    KEY, LABEL, library, handouts={"agent": _agent_handout}, registry=registry,
    deferred=_DEFERRED, retry=retry,
    plain="How a site project's rounds go is decided by the process file: whether the server can read the site (a plan "
          "that says its listings only load in a browser hands the site to your browser; one that says it asks not to "
          "be read switches it off and tells you; neither asks you anything), when the checks are made and the report "
          "written, and whether to finish or run another round. It starts every plan that can be read, asks for the "
          "next one, finishes the project, and tells a scout that didn't finish to try again. It calls Do one plan "
          "item for each item and Check one item for each tried item.",
)
live.register_drivable(
    ITEM_KEY, ITEM_LABEL, library, handouts={"agent": _item_agent_handout}, registry=registry, deferred=_DEFERRED,
    retry=retry,
    plain="How one item of an approved plan is done: the scout takes it, does it as its kind says, and the item ends "
          "done or off plan. Each item's run is one of the round's.",
)
live.register_drivable(
    CHECK_KEY, CHECK_LABEL, library, registry=registry, deferred=_DEFERRED, retry=retry,
    plain="How one tried item is checked: code fetches the site and counts what came back, and says whether the item "
          "was on plan. No agent checks, and the scout's own word is never the evidence. Each check's run is one of "
          "the round's.",
)
