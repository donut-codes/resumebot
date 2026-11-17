"""Write and read the trail. Everything here flushes; the caller commits."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.models.work import TrailEvent
from app.timeutil import iso_utc

#: The words the page's three filters mean.
NEEDS_LOOK = ("needs_you",)
HIGHLIGHTS = ("needs_you", "result")
APP_WHO = "resumebot"


def add(
    session: AsyncSession, owner: str, *, event: str, sentence: str, actor: str = "app", who: str | None = None,
    agent_slug: str | None = None, tier: str = "routine", mark: str = "neutral",
    link: tuple[str, uuid.UUID] | None = None, sent_run_id: uuid.UUID | None = None, detail: str | None = None,
) -> TrailEvent:
    """Add one line of Activity to the session without flushing it (the caller flushes, once for many lines).
    `sentence` is plain words, at most 140 characters (cut, never refused)."""
    row = TrailEvent(
        owner=owner, actor=actor, who=who or (APP_WHO if actor == "app" else "Someone"), agent_slug=agent_slug,
        event=event, sentence=sentence.strip()[:140], tier=tier, mark=mark,
        link_kind=link[0] if link else None, link_id=link[1] if link else None, sent_run_id=sent_run_id,
        detail=(detail or None),
    )
    session.add(row)
    return row


async def record(
    session: AsyncSession, owner: str, *, event: str, sentence: str, actor: str = "app", who: str | None = None,
    agent_slug: str | None = None, tier: str = "routine", mark: str = "neutral",
    link: tuple[str, uuid.UUID] | None = None, sent_run_id: uuid.UUID | None = None, detail: str | None = None,
) -> TrailEvent:
    """Add one line of Activity and flush it. `sentence` is plain words, at most 140 characters (cut, never refused)."""
    row = add(session, owner, event=event, sentence=sentence, actor=actor, who=who, agent_slug=agent_slug, tier=tier,
              mark=mark, link=link, sent_run_id=sent_run_id, detail=detail)
    await session.flush()
    return row


def entry(row: TrailEvent) -> dict:
    """One trail entry in the shape the Activity list reads (less what has no meaning here)."""
    words = (row.detail or "").strip()
    return {
        "id": str(row.id), "at": iso_utc(row.at), "who": row.who,
        "actor": row.actor if row.actor in ("app", "agent", "person") else "app",
        "event": row.event, "tier": row.tier, "sentence": row.sentence, "mark": row.mark,
        "details": {"words": words, "evidence": None, "steps": []} if words else None,
        "link": {"kind": row.link_kind, "id": str(row.link_id)} if row.link_kind and row.link_id else None,
        "sent_task_id": str(row.sent_run_id) if row.sent_run_id else None,
    }


class CursorError(DomainError):
    """`before` is neither a time nor a cursor this page gave."""

    status = 422


def _cursor(before: datetime | str) -> tuple[datetime, uuid.UUID | None]:
    """A `before` as (time, line id): the cursor `page` hands out ("<time>|<id>"), or a plain time (no id)."""
    if isinstance(before, datetime):
        return before, None
    when, _, line = before.partition("|")
    try:
        at = datetime.fromisoformat(when)
        return at, uuid.UUID(line) if line else None
    except ValueError:
        raise CursorError("before is a time, or the next_before of the page before") from None


async def page(
    session: AsyncSession, owner: str, *, agent_slug: str | None = None, filter: str = "all",
    before: datetime | str | None = None, limit: int = 20,
) -> dict:
    """Newest first, `limit` at a time; `next_before` is the cursor to ask for the page after this one: the time and the
    id of the last line shown, so lines that share a time are neither repeated nor dropped."""
    query = select(TrailEvent).where(TrailEvent.owner == owner)
    if agent_slug:
        query = query.where(TrailEvent.agent_slug == agent_slug)
    if filter == "needs_look":
        query = query.where(TrailEvent.tier.in_(NEEDS_LOOK))
    elif filter == "highlights":
        query = query.where(TrailEvent.tier.in_(HIGHLIGHTS))
    if before is not None:
        at, line = _cursor(before)
        query = query.where(TrailEvent.at < at if line is None else or_(
            TrailEvent.at < at, and_(TrailEvent.at == at, TrailEvent.id < line)))
    rows = (await session.scalars(query.order_by(TrailEvent.at.desc(), TrailEvent.id.desc()).limit(limit + 1))).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [entry(r) for r in rows],
            "next_before": f"{iso_utc(rows[-1].at)}|{rows[-1].id}" if more and rows else None}
