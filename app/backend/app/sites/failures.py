"""A site round that failed in a way nobody expected: the run is held where it was and the Inbox says so, once, with
Try again.

Written for this app, trimmed to the
site library. The engine's `live.attempt` has already undone the step's changes when `held` runs; nothing falls back
to other code and no run is cancelled. The hooks are the engine's `on_drive_failure` and `on_drive_success`
(`app.jobsearch.process` points them here for the three site processes and leaves every other process as it was: a
job-search process that fails is still only logged).

- **One Inbox task for each project** (key `site-failed:<project>`, kind `site_failed`): "resumebot couldn't <what>:
  <why>", with Try again and Leave it. A second failure of the same project updates that task (reopening it if it was
  closed).
- **Try again** runs `round_process.retry` for the project's subject, from today's facts. A retry that fails again is
  held the same way, so the task is back with the new reason.
- **Leave it** closes the task, and the same failure is not asked about again until the project's round has worked
  once. A different failure is a new question.
- **It closes by itself** when a later attempt for the same project works (`worked`).

Plain words only: the reason never carries an id, a path or an exception name. Services flush; the route that owns the
request commits.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from process_engine import live
from process_engine.errors import ActionError, EngineError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.fetch import polite
from app.models.work import TASK_DONE, TASK_OPEN, Task
from app.sites import round_process
from app.sites.models import Plan, PlanStatus, Project
from app.sites.records import NEEDS_YOU, PROJECT_LINK
from app.textutil import one_line, squash
from app.trail import service as trail

log = logging.getLogger(__name__)

KIND = "site_failed"
TRY_AGAIN, LEAVE_IT, WORKED = "try_again", "leave_it", "worked"
GENERIC_WHY = "something unexpected got in the way"
WHAT_MAX, WHY_MAX = 120, 160
#: The processes whose failures come here: the project round and the two it calls.
PROCESSES = (round_process.KEY, round_process.ITEM_KEY, round_process.CHECK_KEY)

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
#: An exception's own name, in a message that was written for a log; a path.
_EXCEPTION_NAME = re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception|Failed)\b:?\s*")
_PATH = re.compile(r"(?:[A-Za-z]:)?(?:/|\\)[\w./\\-]+")
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|['\"]\s*['\"]")

OPTIONS = [
    {"id": TRY_AGAIN, "label": "Try again", "description": "resumebot runs it once more."},
    {"id": LEAVE_IT, "label": "Leave it", "description": "Nothing more happens until it comes up again."},
]


def is_site_process(key: str) -> bool:
    return key in PROCESSES


def project_id_of(subject: str | None) -> uuid.UUID | None:
    """The project a round subject (`round:<owner>:<project>`, with or without the pass's `:pass`) is for."""
    found = _UUID.findall(subject or "")
    try:
        return uuid.UUID(found[-1]) if found else None
    except ValueError:
        return None


def task_key(process: str, project_id: uuid.UUID | None) -> str:
    return f"site-failed:{project_id or process}"


def plain(text: object, limit: int) -> str:
    """Words for the owner: whitespace folded, no id, path or exception name."""
    out = _EXCEPTION_NAME.sub("", _PATH.sub("", _UUID.sub("", squash(text))))
    out = squash(_EMPTY_BRACKETS.sub("", out))
    return one_line(out, limit)


def what_of(process: str, what: str | None) -> str:
    """What the process was trying to do, as the owner reads it ("take a plan"). A process that didn't say is named by
    its label."""
    text = plain(what, WHAT_MAX)
    if text:
        return text
    spec = live.drivable(process)
    return f"carry on with “{spec.label}”" if spec is not None else "carry on"


def why_of(exc: BaseException) -> str:
    """Why it failed, in plain words. The engine's own errors are written for people and scrubbed; anything else (a bug,
    a database error) is never quoted: its text is in the log."""
    if isinstance(exc, TimeoutError):
        return "something took too long"
    if isinstance(exc, OSError):
        return "something it needed couldn't be reached"
    if isinstance(exc, EngineError | live.DriveFailed | ActionError):
        text = plain(str(exc), WHY_MAX).rstrip(". ")
        if text:
            return text
    return GENERIC_WHY


def _title(what: str, why: str) -> str:
    return f"resumebot couldn't {what}: {why}"[:300]


async def held_projects(session: AsyncSession, owner: str, project_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these projects have a round held by a failure: an open `site_failed` task is about them. Nothing is
    offered to the scout for these (it would only fail again); the owner's Try again or Leave it decides."""
    if not project_ids:
        return set()
    return set((await session.scalars(
        select(Task.link_id).where(
            Task.owner == owner, Task.kind == KIND, Task.state == TASK_OPEN, Task.link_kind == PROJECT_LINK,
            Task.link_id.in_(project_ids),
        )
    )).all())


async def _row(session: AsyncSession, owner: str, key: str) -> Task | None:
    return await session.scalar(select(Task).where(Task.owner == owner, Task.key == key))


async def _detail(session: AsyncSession, project_id: uuid.UUID | None) -> str:
    project = await session.get(Project, project_id) if project_id is not None else None
    base = "Try again runs it once more, from what is true now."
    return f"{one_line(project.title, 120)}. {base}" if project is not None else base


async def held(
    session: AsyncSession, owner: str, process: str, what: str, exc: BaseException, subject: str | None = None,
) -> None:
    """`on_drive_failure`: the run is held. Record it in Activity and keep the one task for the project. Flushes; the
    caller commits."""
    what_text, why = what_of(process, what), why_of(exc)
    title = _title(what_text, why)
    project_id = project_id_of(subject)
    key = task_key(process, project_id)
    link = (PROJECT_LINK, project_id) if project_id is not None else None
    row = await _row(session, owner, key)
    if row is not None and row.state == TASK_OPEN:
        row.title, row.detail = title, await _detail(session, project_id)
        await session.flush()
        return
    if row is not None and row.answer == LEAVE_IT and row.title == title:
        return  # "Leave it" was the answer to this same failure and nothing has worked since
    if row is None:
        row = Task(
            owner=owner, key=key, title=title, kind=KIND, detail=await _detail(session, project_id), options=OPTIONS,
            recommended=TRY_AGAIN, link_kind=link[0] if link else None, link_id=link[1] if link else None,
        )
        session.add(row)
    else:  # it closed, and the round failed again: the same task is a new question
        row.title, row.detail = title, await _detail(session, project_id)
        row.state, row.answer, row.note, row.done_at, row.snoozed_until = TASK_OPEN, None, None, None, None
        row.options, row.recommended, row.created_at = OPTIONS, TRY_AGAIN, datetime.now(UTC)
    await session.flush()
    await trail.record(
        session, owner, event="process_failed", actor="app", sentence=title, tier=NEEDS_YOU, mark="off", link=link,
    )


async def worked(
    session: AsyncSession, owner: str, process: str, what: str, subject: str | None = None,
) -> None:
    """`on_drive_success`: a later attempt for the project worked, so its task closes by itself, and a "Leave it" no
    longer stands (a failure after this is news). Cheap when there is no task: one indexed read."""
    row = await _row(session, owner, task_key(process, project_id_of(subject)))
    if row is None:
        return
    if row.state == TASK_OPEN:
        row.state, row.done_at, row.answer = TASK_DONE, datetime.now(UTC), WORKED
        row.note = "It worked on a later try."
    elif row.answer == LEAVE_IT:
        row.answer = WORKED
    await session.flush()


async def _read_ahead(session: AsyncSession, project_id: uuid.UUID | None) -> dict:
    """What the project's checks read, when Try again may run them (`check_flow.read_ahead`): read before the retry's
    savepoint, with no transaction open."""
    from app.sites import check_flow  # it reads the round's run, which reads this module

    project = await session.get(Project, project_id) if project_id is not None else None
    if project is None:
        return {}
    plan = await session.scalar(
        select(Plan).where(Plan.project_id == project.id).order_by(Plan.version.desc()).limit(1))
    if plan is None or plan.status != PlanStatus.approved:
        return {}
    return await check_flow.read_ahead(session, project, plan)


async def apply_answer(session: AsyncSession, task: Task, option: str) -> list[uuid.UUID]:
    """The owner's answer to the task (`inbox.service.answer` has marked it done). Leave it changes nothing; Try again
    runs the project's retry. Returns the ids of the projects to wake the scout for."""
    if option == LEAVE_IT:
        return []
    project_id = task.link_id
    subject = round_process.subject_of(task.owner, project_id) if project_id is not None else None
    try:
        answers = await _read_ahead(session, project_id)  # before the savepoint, which cannot be released
        async with session.begin_nested():
            with polite.read_ahead(answers):
                await round_process.retry(session, task.owner, subject)
    except Exception as exc:  # a retry that breaks is held like any other failure
        log.exception("trying a site round again (%s) failed", subject)
        await held(session, task.owner, round_process.KEY, "try that again", exc, subject)
    return [project_id] if project_id is not None else []
