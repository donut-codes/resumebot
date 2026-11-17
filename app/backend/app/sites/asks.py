"""The seam where the site loop asks the owner something, and where an ask is settled.

This app's Inbox is `app.inbox.service` Tasks. The loop never builds a Task itself: every ask goes
through `ask_owner`, so the wiring is one function. The owner is never asked about a plan, so there is no plan card.

`ask_owner` is `async (session, project, *, card, key, title, detail, options, recommended, task)`. `card` is `stuck` (a
planning task the scout could not finish after three tries) or `escalation` (the scout's own question, `task.escalate`);
`key` makes the ask once (asked again, it is the same ask). What it returns is the ask's id, or None.

The default only records the ask on the Activity page, as a line that needs the owner, so an ask never vanishes
silently and a test can see what was asked. The default also keeps each ask on `session.info["site_asks"]`, newest
last, for the same reason. The app swaps in the Inbox version at start-up (`set_ask_owner(inbox_asks.ask_inbox)`, step
5: `app.sites.inbox_asks`, which also routes the owner's answer back).

`settle_ask` is the other half: the question being answered some other way (a plan came in after all) closes the Inbox
task that asked it, so a task never outlives its question. It is a plain function over the Inbox's tasks and does
nothing where there is no task.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.work import TASK_DONE, TASK_OPEN, Task, TrailEvent
from app.sites.models import PlanTask, Project
from app.sites.records import NEEDS_YOU, PROJECT_LINK

#: The card the stuck-planner ask uses (the scout's own question is `tools.ESCALATION_CARD`).
STUCK = "stuck"
ASKS_KEY = "site_asks"

AskOwner = Callable[..., Awaitable[uuid.UUID | None]]


async def _record_only(
    session: AsyncSession, project: Project, *, card: str, key: str, title: str, detail: str | None = None,
    options: list[dict] | None = None, recommended: str | None = None, task: PlanTask | None = None,
    **_more: Any,
) -> uuid.UUID | None:
    """The default ask: a line on the Activity page that needs the owner, and a note on the session."""
    asked = session.info.setdefault(ASKS_KEY, [])
    if any(a["key"] == key for a in asked):
        return None
    asked.append({
        "card": card, "key": key, "title": title, "detail": detail, "options": options, "recommended": recommended,
        "project_id": project.id, "task_id": task.id if task is not None else None,
    })
    session.add(TrailEvent(
        owner=project.owner, actor="app", who="resumebot", event="owner_asked", sentence=title[:140], tier=NEEDS_YOU,
        mark="neutral", link_kind=PROJECT_LINK, link_id=project.id, detail=detail,
    ))
    await session.flush()
    return None


_ask: AskOwner = _record_only


def set_ask_owner(fn: AskOwner | None) -> None:
    """Wire the ask to the Inbox (`inbox_asks.install`, at start-up). `None` puts the default back."""
    global _ask
    _ask = fn or _record_only


async def ask_owner(
    session: AsyncSession, project: Project, *, card: str, key: str, title: str, detail: str | None = None,
    options: list[dict] | None = None, recommended: str | None = None, task: PlanTask | None = None, **more: Any,
) -> uuid.UUID | None:
    """Ask the owner something. Services flush, routes commit: this flushes whatever it writes and commits nothing."""
    return await _ask(
        session, project, card=card, key=key, title=title, detail=detail, options=options, recommended=recommended,
        task=task, **more,
    )


async def settle_ask(session: AsyncSession, owner: str, key: str, why: str = "It was decided elsewhere.") -> bool:
    """The question behind `key` was answered somewhere else, so its Inbox task, if still open, is closed (done, with
    `why` as its note and no trail line: the decision wrote its own). True when a task was closed."""
    task = await session.scalar(select(Task).where(Task.owner == owner, Task.key == key, Task.state == TASK_OPEN))
    if task is None:
        return False
    task.state, task.done_at, task.note = TASK_DONE, datetime.now(UTC), why
    await session.flush()
    return True


async def settle_project_asks(session: AsyncSession, project: Project, why: str) -> int:
    """The project ended, so every question it put to the owner that is still open is closed (done, with `why` as its
    note and no trail line: the ending wrote its own). Returns how many were closed."""
    rows = (await session.scalars(select(Task).where(
        Task.owner == project.owner, Task.state == TASK_OPEN, Task.link_kind == PROJECT_LINK,
        Task.link_id == project.id,
    ))).all()
    for task in rows:
        task.state, task.done_at, task.note = TASK_DONE, datetime.now(UTC), why
    if rows:
        await session.flush()
    return len(rows)
