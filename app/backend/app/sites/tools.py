"""The bodies of the scout's site tools (docs/PRD.md path 29).

`jobsearch/doors/scout.py` registers one MCP tool for each function here and does the door's part: the bearer token, the
commit, the refusal's shape, the wake sent after the commit. Each body takes `(session, owner, ...)` with plain
arguments and gives back plain data (a list of items is sent in batches of up to `AGENT_PAYLOAD_BUDGET_CHARS`; a
document the owner saved goes whole). A refusal is a `PlanEngineError` (with the one plain line `how_to_fix` when there
is one); the door turns it into `{"ok": false, "reason": ..., "how_to_fix": ...}`.

Copied from another project's code (app/PROVENANCE.md), keeping the source project's
tool names: `plan.propose`, `plan.revise`, `work.next`, `task.next`, `task.complete`, `task.escalate` (to the owner
only), `item.lookup`, `review.read`, `act.complete`, `lesson.list`, `lesson.record`, `lesson.retire` and `doc.read`.
Written for this app: `skill.try`, which runs a candidate recipe once so Do can see a real read. A session starts no
project of its own: only the owner's switched-on sites get one (`autostart.ensure_projects`).

There is no tool that writes a check result. Check is code (`site.check`); a check task is refused here, and nothing
in this module stores a check's finding.

A wake is data: a reply that leaves a session due carries `_wakes` (`[{"project_id", "reason"}]`), which the door
pops and sends after its commit (`jobsearch/work/wake.py`, reason `site_project`). Services flush, the door commits.
"""

import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.agents import roster
from app.agents.security import reject_secrets
from app.harness import HarnessError
from app.harness import instructions as harness_files
from app.jobsearch.fetch import polite
from app.jobsearch.work import hand_outs, instruction_files
from app.models.agent import AgentConnection
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, fit_rows
from app.sites import addresses, agent_calls, asks, compact, connections, failures, skills, unreadable, where
from app.sites.base import (
    OPEN_STATUSES,
    PLAN_ACTIONS,
    WAKE_REASONS,
    PlanEngineError,
    plan_template,
)
from app.sites.models import (
    Plan,
    PlanItem,
    PlanItemAction,
    PlanStatus,
    PlanTask,
    PlanTaskStatus,
    Project,
    Site,
)
from app.sites.plan_check import _plan_checks
from app.sites.records import _record_activity, report_for_version
from app.sites.task_context import task_view
from app.textutil import one_line, squash

#: The wake the runner is sent for site work (`scout.mjs` takes it as site work when the app can't be asked).
WAKE_REASON = "site_project"
#: The card kind a task.escalate ask is raised with (the Inbox's side reads `task.data["escalation"]`).
ESCALATION_CARD = "escalation"

#: What a hand-out that came up empty says, in the scout's words; any other reason is passed on as it is.
NOTHING_TO_HAND_OUT = {
    "stopped": "this project was stopped",
    "finished": "this project is finished",
    "awaiting_approval": "the plan is waiting for the owner's approval",
    "paused": "you are paused, so nothing is handed out",
    "none_left": "no task is free for you right now",
    "review_in_progress": "the app is checking this round",
    "act_ready": "the round was checked and Act is ready",
    "plan_ready": "the next plan is ready to write",
    "plan_approved": "the plan was approved and its items are ready",
    "planner_retry": "the plan is ready to write again",
}

#: task.escalate
ESCALATION_REASONS = (
    "only_you_know", "only_you_decide", "only_you_can_do", unreadable.NEEDS_BROWSER, unreadable.CANNOT_BE_READ)
QUESTION_MAX_CHARS = 2000
CHECKED_MAX, CHECKED_LINE_CHARS = 10, 200
OPTIONS_MIN, OPTIONS_MAX = 2, 8
OPTION_ID_CHARS, OPTION_LABEL_CHARS, OPTION_DESCRIPTION_CHARS = 40, 240, 2000
#: The owner's one choice for a question that has no options of the scout's own: the first opens the note box (the
#: Inbox shows a box for an option that has a `note`), the second just says it is done.
ANSWER_OPTION = {"id": "answer", "label": "Send my answer", "description": "", "note": "Type what you know."}
DID_IT_OPTION = {"id": "did_it", "label": "I did it", "description": ""}
#: item.lookup
ITEM_DETAIL_MAX_KEYS = 25
ITEM_DETAIL_TEXT_CHARS = 800
#: skill.try
TRY_TITLES = 5
TRY_TITLE_CHARS = 100
#: doc.read: the files a site session may read, by name.
DOC_PREFIXES = ("projects.", "agents.scout", "site.")
#: work.next
WORK_PROJECTS_MAX = 20
#: The kind of item that builds a site's skill.
SKILL_KIND = "site.read"


# --- the door's helpers -------------------------------------------------------------------------------------------


def _id(value: object, what: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise PlanEngineError(f"`{what}` is not an id", f"use the {what} a tool gave you") from None


async def _project(session: AsyncSession, owner: str, project_id: object) -> Project:
    project = await session.get(Project, _id(project_id, "project_id"))
    if project is None or project.owner != owner:
        raise PlanEngineError("no such project", "work.next lists your projects")
    return project


async def _task(session: AsyncSession, owner: str, task_id: object) -> PlanTask:
    task = await session.get(PlanTask, _id(task_id, "task_id"))
    if task is None or task.owner != owner:
        raise PlanEngineError("no such task", "task.next hands you a task")
    return task


async def _scout(session: AsyncSession, owner: str) -> AgentConnection:
    return await connections.scout_connection(session, owner) or await roster.ensure_roster(session, owner)


@contextmanager
def _held_refusal() -> Iterator[None]:
    """A call about a task this session no longer holds (set aside, or restarted), as the refusal the scout reads."""
    try:
        yield
    except agent_calls.TaskNotHeld as exc:
        raise PlanEngineError(
            "this task isn't checked out to you -- it was set aside or restarted",
            "end this session; the app wakes a fresh one for the open work",
        ) from exc


def _wakes(reply: dict, project: Project, wakes: list[agent_calls.Wake | None]) -> dict:
    """The sessions due after the commit, left on the reply for the door."""
    due = [w for w in wakes if w is not None]
    if due:
        reply["_wakes"] = [{"project_id": str(project.id), "reason": WAKE_REASON, "why": w.reason} for w in due]
    return reply


async def keep_copy(session: AsyncSession, project: Project, task: PlanTask, view: dict, via: str) -> dict:
    """The task view as sent, with a copy kept (`agent_calls.keep_copy`: `planengine.handouts`, `via="task_next"`,
    `task_handouts.plan_task_id`), so How they work can show what the scout was handed. `via` names the door for the
    log; every site hand-out is recorded as `task_next`."""
    conn = await connections.scout_connection(session, project.owner)
    return await agent_calls.keep_copy(session, project, task, view, conn.id if conn else None)


# --- work.next, task.next -----------------------------------------------------------------------------------------


async def _approved_plans(session: AsyncSession, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    rows = (await session.execute(
        select(Plan.project_id, Plan.id).where(Plan.project_id.in_(project_ids), Plan.status == PlanStatus.approved)
    )).all()
    return {project_id: plan_id for project_id, plan_id in rows}


async def _checks_due(session: AsyncSession, project: Project, plan_id: uuid.UUID) -> bool:
    """An approved plan whose every item has ended but whose checks were never made (the scout's last
    `task.complete` asked for no next task, or its session ended): `task.next` makes them. Nothing else can."""
    plan = await session.get(Plan, plan_id)
    if plan is None or plan.validation is None:
        return False
    open_do = await session.scalar(
        select(PlanTask.id).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == plan_id, PlanTask.action == PlanItemAction.do,
            PlanTask.status.in_(OPEN_STATUSES), PlanTask.superseded.is_(False),
        ).limit(1)
    )
    if open_do is not None or await report_for_version(session, project.id, plan.version) is not None:
        return False
    return not await _plan_checks(session, project.id, plan_id)


async def waiting_projects(session: AsyncSession, owner: str, own: frozenset[uuid.UUID] = frozenset()) -> list[dict]:
    """The owner's open projects with work for the scout, in the projects list's own order: a plan to write, items to
    do, Act to run, or checks to make. Each is `{project, waiting}`; `waiting` names what, in the order it comes. A
    project claimed for a session that has not started on it (`hand_outs.py`, path 47) is left out, except for the
    session whose claim it is (`own`)."""
    claimed = await hand_outs.held_uuids(session, owner, hand_outs.SITE) - own
    query = select(Project).where(Project.owner == owner, Project.stopped_at.is_(None), Project.finished_at.is_(None))
    if claimed:
        query = query.where(Project.id.not_in(claimed))
    open_projects = list((await session.scalars(query.order_by(Project.priority, Project.created_at))).all())
    # A project whose round is held by a failure (an open `site_failed` task) is not offered: it would only fail again
    # and cost a session. The owner's Try again puts it back.
    held = await failures.held_projects(session, owner, [p.id for p in open_projects])
    open_projects = [p for p in open_projects if p.id not in held]
    if not open_projects:
        return []
    ids = [p.id for p in open_projects]
    approved = await _approved_plans(session, ids)
    open_tasks = (await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id.in_(ids), PlanTask.superseded.is_(False),
            PlanTask.status.in_((PlanTaskStatus.todo, PlanTaskStatus.in_progress)),
            PlanTask.action.in_((PlanItemAction.do, PlanItemAction.act, *PLAN_ACTIONS)),
        )
    )).all()
    out: list[dict] = []
    for project in open_projects:
        plan_id = approved.get(project.id)
        # A task of a version that is not the approved one is not the scout's to take.
        mine = [t for t in open_tasks if t.project_id == project.id and (t.plan_id is None or t.plan_id == plan_id)]
        waiting = []
        if any(t.action in PLAN_ACTIONS for t in mine):
            waiting.append("plan")
        if any(t.action == PlanItemAction.do for t in mine):
            waiting.append("items")
        if any(t.action == PlanItemAction.act for t in mine):
            waiting.append("act")
        if not waiting and plan_id is not None and await _checks_due(session, project, plan_id):
            waiting.append("checks")
        if waiting:
            out.append({"project": project, "waiting": waiting, "open": len(mine)})
    return out


async def free(session: AsyncSession, owner: str, now=None, *, mine: Collection[uuid.UUID] = (),
               scope: str | None = None) -> list[uuid.UUID]:
    """The site projects a scout session could take now, as their ids, in the projects list's own order: the ones with
    work waiting (`waiting_projects`), leaving out the ones claimed for another session and keeping the caller's own
    claim (`mine`). What makes `site` the answer to "which work waits" (`jobsearch/work/session_kinds.py`) and what a
    claim
    holds."""
    return hand_outs.claimed_first([w["project"].id for w in await waiting_projects(session, owner, frozenset(mine))],
                                   mine)


async def work_next(session: AsyncSession, owner: str, claim: str | None = None,
                    session_id: str | None = None) -> dict:
    """The scout's projects that have something waiting, each with what: `task.next(project_id)` hands it over. A
    session started for a claim (`claim`, `X-Claim`) is told the project claimed for it, and only that."""
    await _scout(session, owner)
    taken = await hand_outs.take(session, owner, hand_outs.SITE, claim, session_id)
    mine = frozenset(uuid.UUID(i) for i in taken or [])
    waiting = await waiting_projects(session, owner, mine)
    if mine:
        waiting = [w for w in waiting if w["project"].id in mine]
    sites = {
        s.id: s for s in (await session.scalars(
            select(Site).where(Site.id.in_([w["project"].site_id for w in waiting if w["project"].site_id])))
        ).all()
    } if waiting else {}
    rows = [
        {
            "project_id": str(w["project"].id), "title": one_line(w["project"].title, 120),
            "site": {"url": s.url, "kind": s.kind} if (s := sites.get(w["project"].site_id)) else None,
            "waiting": w["waiting"], "open_tasks": w["open"],
        }
        for w in waiting
    ]
    listed, _rest = fit_rows(rows[:WORK_PROJECTS_MAX], AGENT_PAYLOAD_BUDGET_CHARS - 1_000, lambda r: r)
    reply: dict[str, Any] = {"ok": True, "projects": listed}
    if len(rows) > len(listed):
        reply["more"] = len(rows) - len(listed)
    reply["note"] = (
        "call task.next with a project_id for its next task" if listed else "nothing is waiting for you. Stop here"
    )
    return reply


async def task_next(session: AsyncSession, owner: str, project_id: str) -> dict:
    """Check out this project's next task (a task you already hold comes back first). Nothing to hand out is an ordinary
    `reason`, not an error."""
    project = await _project(session, owner, project_id)
    conn = await _scout(session, owner)
    handed = await agent_calls.next_task(session, project, conn.id)
    if handed.task is None:
        reason = handed.reason or "none_left"
        fresh = reason in WAKE_REASONS
        words = NOTHING_TO_HAND_OUT.get(reason, reason)
        if reason in ("awaiting_approval", "none_left") and await session.scalar(
            select(PlanTask.id).where(
                PlanTask.project_id == project.id, PlanTask.status == PlanTaskStatus.waiting,
                PlanTask.superseded.is_(False),
            ).limit(1)
        ):
            words = "your question to the owner is waiting for their answer"
        reply = {
            "ok": False, "reason": words,
            "how_to_fix": (
                "end this session: the app wakes a fresh one for it" if fresh
                else "stop: nothing is waiting for you in this project"
            ),
        }
        return _wakes(reply, project, [handed.wake])
    view = await task_view(session, project, handed.task)
    return {"ok": True, "task": await keep_copy(session, project, handed.task, view, "task_next")}


# --- plan.propose, plan.revise ------------------------------------------------------------------------------------


async def _require_planning_task(session: AsyncSession, project: Project, conn: AgentConnection) -> None:
    held = await session.scalar(
        select(PlanTask.id).where(
            PlanTask.project_id == project.id, PlanTask.action.in_(PLAN_ACTIONS),
            PlanTask.status == PlanTaskStatus.in_progress, PlanTask.checked_out_by == conn.id,
            PlanTask.superseded.is_(False),
        ).limit(1)
    )
    if held is None:
        raise PlanEngineError(
            "you don't hold this project's Write-plan task",
            "call task.next for the project first, then write the plan",
        )


def _plan_reply(project: Project, sent: agent_calls.PlanSent) -> dict:
    plan = sent.plan
    reply: dict[str, Any] = {"ok": True, "plan_version": plan.version, "status": plan.status.value}
    if sent.started:
        reply["started"] = True
        reply["next"] = "the app started this plan without asking, because nothing in it needs the owner"
    elif plan.status == PlanStatus.changes_requested:
        reply["sent_back"] = True
        reply["next"] = "the app sent this plan back before it went anywhere. Your next Write-plan task says why. Stop"
    elif project.finished_at is not None:  # the plan said the server can't read the site (`unreadable.py`)
        reply["ended"] = True
        reply["next"] = ("the server can't read the site, so resumebot ended the project and either handed the site to "
                         "the owner's browser or, if it refuses to be read, switched it off. Stop")
    else:
        reply["next"] = "the owner decides this plan. You have nothing more to do for it"
    return _wakes(reply, project, [sent.wake])


async def plan_propose(
    session: AsyncSession, owner: str, project_id: str, summary: str, method: str, items: list[dict[str, Any]],
    validation: dict[str, Any] | None = None, act_guidance: str | None = None, problem: str | None = None,
    proposal: list[str] | None = None, dropped: list[dict[str, Any]] | None = None,
) -> dict:
    project = await _project(session, owner, project_id)
    conn = await _scout(session, owner)
    await _require_planning_task(session, project, conn)
    sent = await agent_calls.propose_plan(
        session, project, summary=summary, method=method, items=items, validation=validation,
        act_guidance=act_guidance, problem=problem, proposal=proposal, dropped=dropped,
    )
    return _plan_reply(project, sent)


async def plan_revise(
    session: AsyncSession, owner: str, project_id: str, based_on_version: int, summary: str, changes: str,
    items: list[dict[str, Any]], method_changed: bool = False, validation: dict[str, Any] | None = None,
    act_guidance: str | None = None, problem: str | None = None, proposal: list[str] | None = None,
    method: str | None = None, dropped: list[dict[str, Any]] | None = None,
) -> dict:
    project = await _project(session, owner, project_id)
    conn = await _scout(session, owner)
    await _require_planning_task(session, project, conn)
    sent = await agent_calls.revise_plan(
        session, project, based_on_version=based_on_version, summary=summary, changes=changes, items=items,
        method_changed=method_changed, validation=validation, act_guidance=act_guidance,
        problem=problem, proposal=proposal, method=method, dropped=dropped,
    )
    return {**_plan_reply(project, sent), "diff": sent.plan.diff}


# --- task.complete, skill.try -------------------------------------------------------------------------------------


async def _keep_recipe(session: AsyncSession, project: Project, task: PlanTask, outcome: str, recipe: Any) -> int:
    """The recipe a site.read item built, kept as the site's next version of its skill (the version before it is
    retired), so the item's Check, which runs the site's newest skill, reads it. The same recipe as the newest version
    is not a new version. Returns the version."""
    if task.kind != SKILL_KIND or task.action != PlanItemAction.do:
        raise PlanEngineError(
            f"a recipe is kept only by an item of kind {SKILL_KIND}", "leave `recipe` out for this task",
        )
    if outcome != "done":
        raise PlanEngineError(
            "a recipe is kept only with a done item", "finish the item `done`, or leave `recipe` out",
        )
    addresses.reject_recipe_secrets(recipe)
    try:
        parsed = skills.parse_recipe(recipe)
    except ValueError as exc:
        raise PlanEngineError(
            f"`recipe` isn't a recipe the app can run: {exc}", "doc.read('site.skill') shows each kind of recipe",
        ) from exc
    if parsed.kind == "browser":
        raise PlanEngineError(*skills.BROWSER_REFUSAL)
    current = await skills.current_skill(session, project.site_id)
    if current is not None and current.recipe == parsed.model_dump(mode="json", by_alias=True):
        return current.version
    return (await skills.keep_skill(session, project, recipe)).version


async def task_complete(
    session: AsyncSession, owner: str, task_id: str, outcome: str, summary: str, evidence: str,
    off_plan_reason: str | None = None, result: dict[str, Any] | None = None, tried: list[str] | None = None,
    want_next: bool = True, recipe: dict[str, Any] | None = None,
) -> dict:
    """Finish the do-task you hold: `done`, or `off_plan` with its reason. An item that builds the site's skill (kind
    `site.read`) sends the `recipe` it built and tried, which is kept as the site's next version. Unless `want_next` is
    false the reply carries the next task."""
    task = await _task(session, owner, task_id)
    project = await _project(session, owner, task.project_id)
    conn = await _scout(session, owner)
    version = None
    with _held_refusal():
        await agent_calls.require_held(session, task, conn.id)
        # Kept before the item ends: ending the last item can make its checks at once, and they read the newest skill.
        if recipe is not None:
            version = await _keep_recipe(session, project, task, outcome, recipe)
        done = await agent_calls.complete_task(
            session, task, project, conn.id, outcome=outcome, reason=off_plan_reason, evidence=evidence,
            summary=summary, result=result, tried=tried, want_next=want_next, reason_field="off_plan_reason",
        )
    if version is not None:
        done.task.data = {**(done.task.data or {}), "skill_version": version}
    reply: dict[str, Any] = {"task_id": str(done.task.id), "state": done.task.status.value}
    if version is not None:
        reply["skill_version"] = version
    if done.next is not None:
        view = await task_view(session, project, done.next.task) if done.next.task is not None else None
        reply["next"] = (
            {"task": await keep_copy(session, project, done.next.task, view, "task_complete_next"), "reason": None}
            if view is not None
            else {"task": None, "reason": NOTHING_TO_HAND_OUT.get(done.next.reason or "", done.next.reason)}
        )
    return _wakes(reply, project, list(done.wakes))


async def skill_try(session: AsyncSession, owner: str, task_id: str, recipe: dict[str, Any]) -> dict:
    """Run a candidate recipe once, as the app would, and say what came back: the count and the first titles. Nothing is
    kept and no check result is written (Check is code, run later on the skill the item hands over)."""
    task = await _task(session, owner, task_id)
    conn = await _scout(session, owner)
    with _held_refusal():
        await agent_calls.require_held(session, task, conn.id)
    if task.action != PlanItemAction.do or task.kind != SKILL_KIND:
        raise PlanEngineError(
            f"skill.try is for the item that builds the site's skill (kind {SKILL_KIND})", "call it from that task",
        )
    addresses.reject_recipe_secrets(recipe)
    try:
        parsed = skills.parse_recipe(recipe)
    except ValueError as exc:
        raise PlanEngineError(
            f"not a recipe the app can run: {exc}", "doc.read('site.skill') shows each kind of recipe",
        ) from exc
    if parsed.kind == "browser":
        raise PlanEngineError(*skills.BROWSER_REFUSAL)
    await db.release(session)  # nothing is kept: the site is read with no transaction open
    fetch = polite.give_time(polite.make(), skills.TRY_SECONDS)  # a try is a quick look: a long site is cut short
    try:
        listing = await skills.run_recipe(fetch, parsed, enough=skills.VERIFY_ROWS)
    except polite.Refused as exc:
        raise PlanEngineError(
            f"the site asks not to be read ({one_line(exc, 200)})",
            "call task.escalate with the reason cannot_be_read (resumebot switches the site off and tells the owner); "
            "never look for a way round it",
        ) from exc
    except skills.SkillError as exc:
        raise PlanEngineError(f"the recipe read nothing: {one_line(exc, 300)}", "fix the recipe and try again") from exc
    except httpx.HTTPError as exc:
        raise PlanEngineError(
            f"the site did not answer ({type(exc).__name__})", "try again once; if it still fails, finish off_plan",
        ) from exc
    finally:
        await fetch.close()
    postings = listing.postings
    reply: dict[str, Any] = {
        "ok": True, "count": len(postings),
        "titles": [one_line(p.get("title"), TRY_TITLE_CHARS) for p in postings[:TRY_TITLES]],
        "links": [p.get("url") for p in postings[:2] if p.get("url")],
        "truncated": bool(listing.truncated),
    }
    if listing.note:
        reply["note"] = one_line(listing.note, 300)
    if not postings:
        reply["note"] = reply.get("note") or "the recipe read the site and found no postings"
    return reply


# --- task.escalate ------------------------------------------------------------------------------------------------


def _checked(lines: Any) -> list[str]:
    if isinstance(lines, str):
        lines = [lines]
    out = [one_line(x, CHECKED_LINE_CHARS) for x in lines if isinstance(x, str) and squash(x)] if isinstance(
        lines, list) else []
    if not 1 <= len(out) <= CHECKED_MAX:
        raise PlanEngineError(
            f"`checked` is 1 to {CHECKED_MAX} short lines saying where you already looked",
            "name the pages and documents you tried before asking",
        )
    return out


def _options(options: Any) -> list[dict]:
    if not isinstance(options, list) or not OPTIONS_MIN <= len(options) <= OPTIONS_MAX:
        raise PlanEngineError(
            f"`only_you_decide` needs {OPTIONS_MIN} to {OPTIONS_MAX} `options`: [{{id, label, description}}]",
            "give each real choice an id, a label and what will happen",
        )
    out: list[dict] = []
    for o in options:
        if not isinstance(o, dict) or not squash(o.get("id")) or not squash(o.get("label")):
            raise PlanEngineError("each option needs an `id` and a `label`", "options are [{id, label, description}]")
        row = {
            "id": squash(o["id"])[:OPTION_ID_CHARS], "label": squash(o["label"])[:OPTION_LABEL_CHARS],
            "description": squash(o.get("description"))[:OPTION_DESCRIPTION_CHARS],
        }
        if o.get("destructive") is True:
            row["destructive"] = True
        out.append(row)
    if len({o["id"] for o in out}) != len(out):
        raise PlanEngineError("option ids must be different", "give each option its own id")
    return out


async def task_escalate(
    session: AsyncSession, owner: str, task_id: str, question: str, checked: list[str], reason: str,
    options: list[dict[str, Any]] | None = None, recommended: str | None = None,
) -> dict:
    """The task you hold lacks a fact only the owner can give: ask them once and stop. The task waits (not handed out)
    until they answer; the ask goes to the Inbox through `asks.ask_owner`, with a choice the owner can answer:
    `only_you_decide` carries the scout's own options, `only_you_know` a box for their answer and `only_you_can_do` an
    "I did it". An answer hands the task out again. Nothing happens if they never answer."""
    task = await _task(session, owner, task_id)
    project = await _project(session, owner, task.project_id)
    conn = await _scout(session, owner)
    with _held_refusal():
        await agent_calls.require_held(session, task, conn.id)
    if task.action == PlanItemAction.review_check:
        raise PlanEngineError("a check is run by the app, not the scout", "there is nothing to escalate")
    text = squash(question)
    if not text or len(text) > QUESTION_MAX_CHARS:
        raise PlanEngineError(
            f"`question` is what you need, in at most {QUESTION_MAX_CHARS} characters",
            "say it in a few plain sentences",
        )
    if reason not in ESCALATION_REASONS:
        raise PlanEngineError(
            f"`reason` is one of {', '.join(ESCALATION_REASONS)}",
            "only_you_know: a fact no one else can give; only_you_decide: a real trade-off; only_you_can_do: a step "
            "only a person can take; needs_browser: you can't read this site but a browser might (resumebot hands it "
            "to the owner's browser); cannot_be_read: the site refuses to be read (resumebot switches it off)",
        )
    lines = _checked(checked)
    if reason in (unreadable.NEEDS_BROWSER, unreadable.CANNOT_BE_READ):
        return await _server_cannot_read(session, project, reason, text, lines)
    if reason == "only_you_decide":
        choices = _options(options)
        if recommended not in {o["id"] for o in choices}:
            raise PlanEngineError("`recommended` is the id of one of the options", "pick the option you would take")
    else:
        choices = [dict(ANSWER_OPTION if reason == "only_you_know" else DID_IT_OPTION)]
        recommended = choices[0]["id"]
    reject_secrets(
        question=text, recommended=recommended,
        **{f"checked_{i}": line for i, line in enumerate(lines)},
        **{f"option_{o['id']}": f"{o['label']} {o['description']}" for o in choices},
    )
    before = (task.data or {}).get("escalation")
    asked_times = int((before or {}).get("count") or (1 if before else 0)) + 1  # asked again after an answer
    task.data = {**(task.data or {}), "escalation": {
        "reason": reason, "question": text, "checked": lines, "options": choices, "recommended": recommended,
        "count": asked_times, "asked_at": datetime.now(UTC).isoformat(),
    }}
    task.status = PlanTaskStatus.waiting
    task.checked_out_by = None
    task.checked_out_at = None
    _record_activity(
        session, project, "agent", "task_escalated", f"The scout needs you for “{one_line(task.title, 80)}”.",
        agent_text=text,
    )
    await session.flush()
    key = f"escalate-{task.id}" if asked_times == 1 else f"escalate-{task.id}-{asked_times}"
    await asks.ask_owner(
        session, project, card=ESCALATION_CARD, key=key, title=one_line(text, 200),
        detail=f"Looked in: {'; '.join(lines)}.", options=choices, recommended=recommended, task=task,
    )
    return {"ok": True, "asked": "the owner", "next": "the task waits for their answer. Stop now"}


async def _server_cannot_read(
    session: AsyncSession, project: Project, reason: str, why: str, lines: list[str],
) -> dict:
    """`task.escalate` with the reason `needs_browser` or `cannot_be_read`: the server can't read the site. Nobody is
    asked. `needs_browser` (its listings only load in a browser, it asks for a sign-in, a token or a bot check): the
    browser has not been tried, so resumebot ends the project and hands the site to the owner's browser, which decides
    (`unreadable.site_needs_browser`). `cannot_be_read` (its robots rules or terms refuse): nothing can read it, so
    resumebot ends the project, switches the site off and tells the owner once (`unreadable.site_cannot_be_read`).
    `why` is the one plain clause the owner reads ("its listings only load in a browser"); `lines` is where the scout
    looked."""
    if len(why) > where.OFF_REASON_MAX:
        raise PlanEngineError(
            f"for `{reason}`, `question` is why the server can't read the site, as one plain clause of at most "
            f"{where.OFF_REASON_MAX} characters",
            "for example: its listings only load in a browser",
        )
    reject_secrets(question=why, **{f"checked_{i}": line for i, line in enumerate(lines)})
    browser = reason == unreadable.NEEDS_BROWSER
    _record_activity(
        session, project, "agent", "site_to_browser" if browser else "site_unreadable",
        "The scout found that this site needs a browser." if browser else "The scout found this site can't be read.",
        agent_text=f"{why}. Looked in: {'; '.join(lines)}.",
    )
    await session.flush()
    if browser:
        await unreadable.site_needs_browser(session, project, why)
        return {"ok": True, "site_to_browser": True,
                "next": "resumebot handed the site to the owner's browser and ended the project. Stop now"}
    await unreadable.site_cannot_be_read(session, project, why)
    return {"ok": True, "site_turned_off": True,
            "next": "resumebot switched the site off and ended the project. Stop now"}


# --- item.lookup, review.read, lesson.list, doc.read --------------------------------------------------------------


async def item_lookup(session: AsyncSession, owner: str, project_id: str, keys: list[str]) -> dict:
    """Full detail for plan items by key (at most 25): the item as the plan has it and, for one that was tried, its
    status, the reason it went off plan and its evidence."""
    project = await _project(session, owner, project_id)
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise PlanEngineError("`keys` is a list of item keys", "use the keys in your task's context")
    if len(keys) > ITEM_DETAIL_MAX_KEYS:
        raise PlanEngineError(f"at most {ITEM_DETAIL_MAX_KEYS} keys per call", "ask for the keys in several calls")
    plan = await session.scalar(
        select(Plan).where(Plan.project_id == project.id, Plan.status == PlanStatus.approved)
        .order_by(Plan.version.desc()).limit(1)
    ) or await session.scalar(
        select(Plan).where(Plan.project_id == project.id).order_by(Plan.version.desc()).limit(1)
    )
    rows: list[dict] = []
    if plan is not None and keys:
        items = {i.key: i for i in (await session.scalars(
            select(PlanItem).where(PlanItem.plan_id == plan.id, PlanItem.key.in_(keys))
        )).all()}
        tasks = {t.item_key: t for t in (await session.scalars(
            select(PlanTask).where(
                PlanTask.project_id == project.id, PlanTask.plan_id == plan.id, PlanTask.action == PlanItemAction.do,
                PlanTask.superseded.is_(False), PlanTask.item_key.in_(keys),
            )
        )).all()}
        for key in keys:
            item = items.get(key)
            if item is None:
                continue
            row: dict[str, Any] = {
                "key": item.key, "title": item.title, "action": item.action.value, "kind": item.kind, "data": item.data,
                "plan_version": plan.version,
            }
            task = tasks.get(key)
            if task is not None:
                row["status"] = task.status.value
                if task.status in (PlanTaskStatus.off_plan, PlanTaskStatus.done):
                    row["off_plan_reason"] = (task.outcome_reason or "")[:ITEM_DETAIL_TEXT_CHARS]
                    row["evidence"] = (task.evidence or "")[:ITEM_DETAIL_TEXT_CHARS]
            rows.append(row)
    listed, rest = fit_rows(rows, AGENT_PAYLOAD_BUDGET_CHARS - 500, lambda r: r)
    reply: dict[str, Any] = {"ok": True, "items": listed}
    if rest:
        reply["not_returned"] = [r["key"] for r in rest]
    return reply


async def review_read(session: AsyncSession, owner: str, project_id: str, plan_version: int | None = None) -> dict:
    """The full check report for a plan version (the latest when none is named): what compact payloads clip."""
    project = await _project(session, owner, project_id)
    report = await compact.report_read(session, project, plan_version)
    if report is None and plan_version is not None:
        # A version that never ran has no report: hand back the latest rather than a refusal that reads like "none".
        report = await compact.report_read(session, project, None)
    if report is None:
        raise PlanEngineError("no check report yet for this project", "the report is written after the round's checks")
    reply: dict[str, Any] = {"ok": True, "report": report}
    if plan_version is not None and report["plan_version"] != plan_version:
        reply["note"] = f"plan v{plan_version} has no report; this is the latest, for plan v{report['plan_version']}"
    return reply


async def lesson_list(session: AsyncSession, owner: str, project_id: str, offset: int = 0) -> dict:
    """The lessons that apply to this project, full text, newest first."""
    project = await _project(session, owner, project_id)
    return {"ok": True, **await compact.lessons_full(session, project, offset, AGENT_PAYLOAD_BUDGET_CHARS)}


def _doc_names(folder: str) -> list[str]:
    return [n for n in harness_files.names(folder) if n.startswith(DOC_PREFIXES)]


async def doc_read(session: AsyncSession, owner: str, name: str) -> dict:
    """One document by name: an instruction, a response template or a kind of work, as the harness has it."""
    ref = squash(name)
    doc: dict[str, Any] | None = None
    for folder, label in (("instructions", "instruction"), ("templates", "template")):
        short = harness_files.short_name(folder, ref)
        if short in _doc_names(folder):
            text = await instruction_files.read(session, owner, folder, ref)  # the owner's newest saved version
            doc = {"ok": True, "name": short, "type": label, "text": text}
            if short == "site.skill":
                doc["job_systems"] = skills.adapter_systems()
            if short == "projects.plan.response":
                doc["template_version"] = plan_template(text)["version"]
            break
    if doc is None:
        try:
            doc = {"ok": True, "name": ref, "type": "kind", "kind": harness_files.read_kind(ref)}
        except (harness_files.UnknownName, HarnessError):
            known = [*_doc_names("instructions"), *_doc_names("templates")]
            raise PlanEngineError(f"no document named {ref!r}", f"use one of: {', '.join(known)}") from None
    return doc  # the owner's own saved text goes whole


# --- lesson.record, lesson.retire, act.complete -------------------------------------------------------------------


async def lesson_record(
    session: AsyncSession, owner: str, project_id: str, task_id: str, text: str, applies_to: str = "project",
    source: str = "", subject: str | None = None,
) -> dict:
    project = await _project(session, owner, project_id)
    task = await _task(session, owner, task_id)
    conn = await _scout(session, owner)
    with _held_refusal():
        lesson, created = await agent_calls.record_lesson(
            session, project, task, conn.id, text=text, applies_to=applies_to, source=source, subject=subject,
        )
    return {"ok": True, "lesson_id": str(lesson.id), "created": created}


async def lesson_retire(
    session: AsyncSession, owner: str, project_id: str, task_id: str, lesson_id: str, source: str,
) -> dict:
    project = await _project(session, owner, project_id)
    task = await _task(session, owner, task_id)
    conn = await _scout(session, owner)
    with _held_refusal():
        lesson = await agent_calls.retire_lesson(
            session, project, task, conn.id, lesson_id=lesson_id, source=source,
        )
    return {"ok": True, "lesson_id": str(lesson.id), "retired": lesson.retired_at is not None}


async def act_complete(
    session: AsyncSession, owner: str, project_id: str, task_id: str, summary: dict[str, str],
    resolution: dict[str, str] | None = None, skill: dict[str, Any] | None = None,
) -> dict:
    """End Act with its summary in named parts; `resolution` on the closing round; `skill` when the round changed the
    site's recipe (kept as the site's next version). Then the loop moves on by itself."""
    project = await _project(session, owner, project_id)
    task = await _task(session, owner, task_id)
    conn = await _scout(session, owner)
    with _held_refusal():
        outcome, wake = await agent_calls.complete_act(
            session, project, task, conn.id, summary=summary, resolution=resolution, skill=skill,
        )
    return _wakes({"ok": True, **outcome}, project, [wake])
