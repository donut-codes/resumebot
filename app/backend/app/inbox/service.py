"""Tasks and notifications: raise, list, answer. Everything here flushes; the route commits.

Peace of mind is the rule (a rule kept on purpose): at most `AT_ONCE` things ask for the owner at a time, the rest
wait as one line ("2 more when you're ready"), nothing is nagged, and there are no invented dates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch.actors import actor_name
from app.models.work import TASK_DONE, TASK_OPEN, TASK_SKIPPED, Notification, Task
from app.timeutil import aware, iso_utc, owner_today
from app.trail import service as trail

#: At most this many things ask for the owner at once (the attention budget).
AT_ONCE = 3
NOTIFICATIONS_SHOWN = 20
DONE_SHOWN = 30
OWNER = "owner"


class TaskError(DomainError):
    """A task can't be changed that way; plain words (answered 409, or 404 for a task there is none of)."""

    status = 409


class NoSuchTask(TaskError):
    status = 404


# --- raising -------------------------------------------------------------------------------------


async def raise_task(
    session: AsyncSession, owner: str, *, key: str | None, title: str, kind: str, detail: str | None = None,
    due: date | None = None, assignee: str = OWNER, options: list[dict] | None = None, recommended: str | None = None,
    link: tuple[str, uuid.UUID] | None = None, facts: list[dict] | None = None,
) -> Task:
    """Make a task. With a `key` it is made once: asked again it returns the one that is there. `facts` are what a
    decision rests on, `[{label, value, sub?}]` in plain words, shown side by side."""
    if key:
        found = await session.scalar(select(Task).where(Task.owner == owner, Task.key == key))
        if found is not None:
            return found
    task = Task(
        owner=owner, key=key, title=title[:300], kind=kind, detail=detail, due=due, assignee=assignee,
        options=options, recommended=recommended, facts=facts or None,
        link_kind=link[0] if link else None, link_id=link[1] if link else None,
    )
    session.add(task)
    await session.flush()
    return task


async def notify(
    session: AsyncSession, owner: str, *, key: str | None, kind: str, title: str, body: str | None = None,
    level: str = "info", link: tuple[str, uuid.UUID] | None = None,
) -> Notification:
    """Tell the owner something in a plain sentence. With a `key` it is told once."""
    if key:
        found = await session.scalar(select(Notification).where(Notification.owner == owner, Notification.key == key))
        if found is not None:
            return found
    note = Notification(
        owner=owner, key=key, kind=kind, title=title[:300], body=body, level=level,
        link_kind=link[0] if link else None, link_id=link[1] if link else None,
    )
    session.add(note)
    await session.flush()
    return note


# --- views ----------------------------------------------------------------------------------------


def _link(kind: str | None, ident: uuid.UUID | None) -> dict | None:
    return {"kind": kind, "id": str(ident)} if kind and ident else None


def task_view(task: Task, agent_names: dict[str, str] | None = None) -> dict:
    who = task.assignee
    names = agent_names or {}
    return {
        "id": str(task.id), "title": task.title, "kind": task.kind, "state": task.state, "detail": task.detail,
        "due": task.due.isoformat() if task.due else None,
        "for": {"kind": "owner" if who == OWNER else "agent", "name": "You" if who == OWNER else names.get(who, who)},
        "options": task.options, "recommended": task.recommended, "answer": task.answer, "note": task.note,
        "facts": task.facts or [], "link": _link(task.link_kind, task.link_id),
        "snoozed_until": task.snoozed_until.isoformat() if task.snoozed_until else None,
        "created_at": iso_utc(task.created_at), "done_at": iso_utc(task.done_at) if task.done_at else None,
    }


def notification_view(note: Notification) -> dict:
    return {
        "id": str(note.id), "kind": note.kind, "title": note.title, "body": note.body, "level": note.level,
        "link": _link(note.link_kind, note.link_id), "created_at": iso_utc(note.created_at),
        "read_at": iso_utc(note.read_at) if note.read_at else None,
    }


def _today(today: date | None) -> date:
    return today or owner_today()


def _waiting_order(task: Task):
    """Decisions before plain tasks; then the ones with a date soonest; then oldest first."""
    return (task.options is None, task.due or date.max, task.created_at)


async def inbox(session: AsyncSession, owner: str, today: date | None = None) -> dict:
    """The Inbox: at most `AT_ONCE` things that need the owner, what else is waiting (a count), what the app has to
    tell them, and what is on track."""
    on = _today(today)
    open_tasks = (await session.scalars(
        select(Task).where(Task.owner == owner, Task.state == TASK_OPEN)
    )).all()
    mine = [t for t in open_tasks if t.assignee == OWNER and not (t.snoozed_until and t.snoozed_until > on)]
    mine.sort(key=_waiting_order)
    shown, later = mine[:AT_ONCE], mine[AT_ONCE:]
    others = [t for t in open_tasks if t.assignee != OWNER]
    notes = (await session.scalars(
        select(Notification).where(Notification.owner == owner)
        .order_by(Notification.read_at.is_not(None), Notification.created_at.desc()).limit(NOTIFICATIONS_SHOWN)
    )).all()
    unread = await session.scalar(
        select(func.count()).select_from(Notification)
        .where(Notification.owner == owner, Notification.read_at.is_(None))
    ) or 0
    next_due = min((t.due for t in others if t.due), default=None)
    return {
        "today": on.isoformat(), "count": len(shown),
        "items": [task_view(t) for t in shown], "later_count": len(later),
        "notifications": [notification_view(n) for n in notes], "unread_count": unread,
        "on_track": {"count": len(others), "next_due": next_due.isoformat() if next_due else None},
    }


async def task_list(session: AsyncSession, owner: str, agent_names: dict[str, str], today: date | None = None) -> dict:
    """The Tasks screen: what is for the owner, what is for the agents, and what was finished lately."""
    on = _today(today)
    open_tasks = (await session.scalars(
        select(Task).where(Task.owner == owner, Task.state == TASK_OPEN)
    )).all()
    mine = sorted((t for t in open_tasks if t.assignee == OWNER), key=_waiting_order)
    theirs = sorted((t for t in open_tasks if t.assignee != OWNER), key=_waiting_order)
    done = (await session.scalars(
        select(Task).where(Task.owner == owner, Task.state != TASK_OPEN)
        .order_by(Task.done_at.desc().nulls_last(), Task.created_at.desc()).limit(DONE_SHOWN)
    )).all()
    return {
        "today": on.isoformat(),
        "for_you": [task_view(t, agent_names) for t in mine],
        "for_agents": [task_view(t, agent_names) for t in theirs],
        "done": [task_view(t, agent_names) for t in done],
    }


async def get_task(session: AsyncSession, owner: str, task_id: uuid.UUID) -> Task:
    task = await session.scalar(select(Task).where(Task.owner == owner, Task.id == task_id))
    if task is None:
        raise NoSuchTask("no such task")
    return task


async def get_task_by_key(session: AsyncSession, owner: str, key: str) -> Task | None:
    """The task raised under this key (open or finished), or None."""
    return await session.scalar(select(Task).where(Task.owner == owner, Task.key == key))


# --- changing -------------------------------------------------------------------------------------


async def _finish(session: AsyncSession, owner: str, task: Task, state: str, sentence: str, note: str | None) -> Task:
    if task.state != TASK_OPEN:
        raise TaskError("that task is already finished")
    task.state, task.done_at = state, datetime.now(UTC)
    task.note = (note or "").strip() or None
    await trail.record(
        session, owner, event=f"task_{state}", actor="person", who=await actor_name(session, owner), sentence=sentence,
        link=(task.link_kind, task.link_id) if task.link_kind and task.link_id else None,
        tier="result" if state == TASK_DONE else "routine",
    )
    await session.flush()
    return task


async def mark_done(session: AsyncSession, owner: str, task_id: uuid.UUID, note: str | None = None) -> Task:
    """Done is done: the owner's word is taken (the rule: never follow it with "confirm it's done")."""
    task = await get_task(session, owner, task_id)
    return await _finish(session, owner, task, TASK_DONE, f"You finished: {task.title}", note)


async def skip(session: AsyncSession, owner: str, task_id: uuid.UUID, reason: str | None = None) -> Task:
    task = await get_task(session, owner, task_id)
    return await _finish(session, owner, task, TASK_SKIPPED, f"You set aside: {task.title}", reason)


async def answer(
    session: AsyncSession, owner: str, task_id: uuid.UUID, option: str, note: str | None = None,
) -> tuple[Task, str]:
    """Answer a decision with one of its options, with the owner's own words if they gave some (kept as the task's
    note). Returns the task and the option's label."""
    task = await get_task(session, owner, task_id)
    options = task.options or []
    chosen = next((o for o in options if o.get("id") == option), None)
    if chosen is None:
        raise TaskError("that isn't one of the choices")
    if task.state != TASK_OPEN:
        raise TaskError("that decision is already made")
    task.answer = option
    await _finish(session, owner, task, TASK_DONE, f"You chose \"{chosen.get('label')}\": {task.title}", note)
    return task, str(chosen.get("label") or option)


async def snooze(session: AsyncSession, owner: str, task_id: uuid.UUID, until: date | None) -> Task:
    """Put it off until a day (the "Not now" of a card), or bring it back with None."""
    task = await get_task(session, owner, task_id)
    if task.state != TASK_OPEN:
        raise TaskError("that task is already finished")
    task.snoozed_until = until
    await session.flush()
    return task


async def mark_read(session: AsyncSession, owner: str, note_id: uuid.UUID | None) -> int:
    """Mark one notification read, or all of them with None. Returns how many changed."""
    query = select(Notification).where(Notification.owner == owner, Notification.read_at.is_(None))
    if note_id is not None:
        query = query.where(Notification.id == note_id)
    rows = (await session.scalars(query)).all()
    now = datetime.now(UTC)
    for row in rows:
        row.read_at = now
    await session.flush()
    return len(rows)


async def history(session: AsyncSession, owner: str, before: datetime | None, limit: int) -> dict:
    """Past decisions: tasks the owner answered, finished or set aside, newest first. A record, never a to-do."""
    query = select(Task).where(Task.owner == owner, Task.state != TASK_OPEN, Task.assignee == OWNER)
    if before is not None:
        query = query.where(Task.done_at < aware(before))  # a time with no zone is UTC, as everywhere
    rows = (await session.scalars(query.order_by(Task.done_at.desc().nulls_last()).limit(limit + 1))).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for t in rows:
        labels = {o.get("id"): o.get("label") for o in (t.options or [])}
        decision = labels.get(t.answer) or ("Done" if t.state == TASK_DONE else "Set aside")
        items.append({
            "at": iso_utc(t.done_at or t.created_at), "title": t.title, "decision": decision,
            "decided_by": "You", "why": t.note, "link": _link(t.link_kind, t.link_id),
        })
    return {"items": items, "next_before": items[-1]["at"] if more and items else None}
