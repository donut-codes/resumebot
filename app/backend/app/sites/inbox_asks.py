"""The site loop's questions to the owner as Inbox tasks, and the owner's answers routed back (step 5).

`asks.ask_owner` is the loop's one way to ask; `install` points it here at start-up, so an ask becomes a task in
`app.inbox.service` (the same Inbox every other thing that needs the owner uses). Written for this app. Three kinds of
task, each with options (the owner is never asked about a plan: there is no "Go ahead with this plan?"):

- `site_stuck` (card `stuck`, key `stuck-<planning task id>`): the scout could not finish a plan after three tries. Try
  again hands the task back, Pause holds the project, Stop ends it (`escalation`).
- `site_failed` (`failures`): a round was held after an unexpected failure. Try again, Leave it.
- `site_escalation` (card `escalation`, key `escalate-<task id>`, and `escalate-<task id>-<n>` when the same task asks
  again after an answer): the scout's own question (`task.escalate`). Its options are the scout's own
  (`only_you_decide`), a box for the owner's answer (`only_you_know`) or "I did it" (`only_you_can_do`). The answer
  goes on the waiting task (`data.escalation.answer`) and the task goes back to todo.

The route (`app.inbox.routes.answer`) marks the task done first (the owner's word is taken), then calls `apply_answer`.

Services flush, routes commit: nothing here commits. Errors are the domain's own (`PlanEngineError`); the route turns
them into a refusal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.harness import instructions as files
from app.harness.loader import harness
from app.inbox import service
from app.jobsearch import owner_text
from app.jobsearch.work import instruction_files
from app.models.work import TASK_OPEN, Task
from app.sites import asks, completion, escalation, failures, task_header
from app.sites.agent_calls import Wake
from app.sites.base import PlanEngineError
from app.sites.models import PlanTask, PlanTaskStatus, Project
from app.sites.records import NEEDS_YOU, PROJECT_LINK, _record_activity
from app.trail import service as trail

STUCK_KIND, FAILED_KIND = "site_stuck", failures.KIND
#: The scout's own question to the owner (`task.escalate`, card `escalation`).
ESCALATION_KIND = "site_escalation"
#: The kinds of Inbox task that belong to the site loop (the route sends their answers here).
KINDS = (STUCK_KIND, FAILED_KIND, ESCALATION_KIND)


@dataclass
class Applied:
    """What an answer came to. `carried` is False when the round could not carry the decision out (it is held: the task
    is reopened and a `site_failed` task says why). `wakes` are `(project id, Wake)` to send after the commit."""

    carried: bool = True
    wakes: list[tuple[uuid.UUID, Wake]] = field(default_factory=list)


# --- asking -------------------------------------------------------------------------------------------------------


async def ask_inbox(
    session: AsyncSession, project: Project, *, card: str, key: str, title: str, detail: str | None = None,
    options: list[dict] | None = None, recommended: str | None = None, task: PlanTask | None = None,
    **_more: object,
) -> uuid.UUID | None:
    """`asks.ask_owner`, for the Inbox: the ask becomes a task for the owner, once for its key (asked again while it is
    open, it is the same task; a `stuck` ask asked again after it was answered is a new question and reopens it)."""
    kind = {asks.STUCK: STUCK_KIND}.get(card, f"site_{card}"[:40])
    existing = await service.get_task_by_key(session, project.owner, key)
    row = await service.raise_task(
        session, project.owner, key=key, title=title, kind=kind, detail=detail, options=options,
        recommended=recommended, link=(PROJECT_LINK, project.id),
    )
    if existing is not None and card == asks.STUCK and existing.state != TASK_OPEN:
        row.title, row.detail, row.options, row.recommended = title[:300], detail, options, recommended
        row.state, row.answer, row.note, row.done_at, row.snoozed_until = TASK_OPEN, None, None, None, None
        row.created_at = datetime.now(UTC)
        existing = None
    if existing is None:
        await trail.record(
            session, project.owner, event="owner_asked", sentence=title, tier=NEEDS_YOU,
            link=(PROJECT_LINK, project.id), detail=detail,
        )
    await session.flush()
    return row.id


def install() -> None:
    """Wire the loop's asks to the Inbox. Called once at start-up (`jobsearch.main.lifespan`)."""
    asks.set_ask_owner(ask_inbox)


# --- answering ----------------------------------------------------------------------------------------------------


def _id_after(key: str | None, prefix: str) -> uuid.UUID:
    """The id a key names: the 36 characters after `prefix` (an escalation key may carry `-<n>` after it)."""
    try:
        return uuid.UUID((key or "").removeprefix(prefix)[:36])
    except ValueError:
        raise PlanEngineError("that question isn't one this app can answer any more") from None


def reopen(task: Task) -> None:
    """Take an answer back: the decision could not be carried out, so the question stands."""
    task.state, task.answer, task.note, task.done_at = TASK_OPEN, None, None, None


#: What the owner's words are called in the sentence that refuses them, and the instruction file sent beside them.
_ANSWER, _BESIDE = "Your answer", "the file sent with it"


async def _instruction_chars(session: AsyncSession, owner: str, step_id: str | None) -> int:
    """The size of the instruction file the process step `step_id` sends with a task (the owner's newest saved version,
    else the shipped file), or 0 when the step has none."""
    process = harness().process
    step = process.step(step_id) if process is not None and step_id else None
    if step is None or not step.instructions:
        return 0
    try:
        return len(await instruction_files.read(session, owner, "instructions", step.instructions))
    except (files.UnknownName, OSError):
        return 0


async def refuse_over_room(session: AsyncSession, task: Task, note: str | None) -> None:
    """Raise `owner_text.TextTooLarge` (answered 413, with the sentence) when the owner's words, with the instruction
    file the agent is sent beside them, would pass what one reply leaves for the two. Called before the answer is made,
    so a refusal changes nothing. The words go back to the scout on the task it asked about (an escalation); the other
    site questions carry no words."""
    words = (note or "").strip()
    if not words or task.kind != ESCALATION_KIND:
        return
    try:
        asked = await session.get(PlanTask, _id_after(task.key, "escalate-"))
    except PlanEngineError:
        return  # `apply_answer` says so, as it always has
    if asked is None or asked.status != PlanTaskStatus.waiting:
        return  # answered some other way already: nothing is sent
    size = await _instruction_chars(session, task.owner, task_header.step_id_for(asked))
    owner_text.refuse_over_room(words, _ANSWER, beside=(_BESIDE, size) if size else None)


async def apply_answer(session: AsyncSession, task: Task, option: str, note: str | None = None) -> Applied:
    """Carry out the owner's answer to a site task (the task is already marked done with `option`). `note` is the
    owner's own words: the answer typed to the scout's question."""
    if task.kind == STUCK_KIND:
        return await _answer_stuck(session, task, option)
    if task.kind == ESCALATION_KIND:
        return await _answer_escalation(session, task, option, note)
    if task.kind == FAILED_KIND:
        wakes = [(pid, Wake("retried", "plan")) for pid in await failures.apply_answer(session, task, option)]
        return Applied(True, wakes)
    raise PlanEngineError("that isn't a question about a site")


async def _answer_escalation(session: AsyncSession, task: Task, option: str, note: str | None) -> Applied:
    """The owner answered the scout's question (`task.escalate`): the answer goes on the waiting task
    (`data.escalation.answer`) and the task goes back to todo, so the scout is handed it again with what it asked."""
    asked = await session.get(PlanTask, _id_after(task.key, "escalate-"))
    if asked is None or asked.status != PlanTaskStatus.waiting:
        return Applied()  # answered some other way already; the Inbox task only closes
    project = await session.get(Project, asked.project_id)
    if project is None or project.owner != task.owner:
        raise PlanEngineError("that project is gone")
    chosen = next((o for o in task.options or [] if o.get("id") == option), None)
    if (chosen or {}).get("note") and not (note or "").strip():
        raise PlanEngineError("type your answer first")  # the box is the answer (the route undoes the choice)
    escalation = dict((asked.data or {}).get("escalation") or {})
    escalation["answer"] = {"option": option, "label": (chosen or {}).get("label"),
                            "note": (note or "").strip() or None}
    asked.data = {**(asked.data or {}), "escalation": escalation}
    asked.status = PlanTaskStatus.todo
    await session.flush()
    return Applied(True, [(project.id, Wake("answered", str(asked.mode or "do")))])


async def _answer_stuck(session: AsyncSession, task: Task, option: str) -> Applied:
    planning = await session.get(PlanTask, _id_after(task.key, "stuck-"))
    project = await session.get(Project, planning.project_id) if planning is not None else None
    if project is None or project.owner != task.owner:
        raise PlanEngineError("that project is gone")
    if project.finished_at is not None:
        return Applied()
    if option == escalation.STUCK_STOP:
        await escalation.stop_for_good(session, project)
        return Applied()
    if option not in (escalation.STUCK_RETRY, escalation.STUCK_PAUSE):
        raise PlanEngineError("that isn't one of the choices")
    if planning.status == PlanTaskStatus.waiting:  # back to the scout's list with a clean slate
        planning.data = {k: v for k, v in (planning.data or {}).items() if k not in ("escalation", "planner_runs")}
        planning.status = PlanTaskStatus.todo
    if option == escalation.STUCK_PAUSE:
        if project.stopped_at is None:
            await completion.stop_project(session, project, "paused by the owner")
        return Applied()
    _record_activity(
        session, project, "you", "planner_retry", f"You asked resumebot to try “{planning.title[:100]}” again.")
    return Applied(True, [(project.id, Wake("planner_retry", "plan"))])
