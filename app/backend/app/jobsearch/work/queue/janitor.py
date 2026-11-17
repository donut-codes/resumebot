"""The janitor: the to-dos nothing consumes are deleted once an hour when they
are older than an hour, and the line it logs says how many of each topic there were. That line is the recording of R,
the rate of change the design is sized by ("to-dos posted per topic per hour"); it is never shown in the app. A topic
the dispatcher takes to-dos of (`consume.consumed_topics`) is left to it: it deletes each to-do it handles, and one it
could not handle stays, backing off, never deleted silently. The hour tick calls `prune` (`handlers.py`)."""

import logging
from collections.abc import Collection
from datetime import datetime, timedelta

from sqlalchemy import Delete, Select, delete, func, select

from app import db
from app.jobsearch.models import Todo
from app.jobsearch.work.queue import consume
from app.timeutil import utc_now

log = logging.getLogger(__name__)

#: How long a to-do is kept.
KEEP = timedelta(hours=1)


def counts_statement(cutoff: datetime, keep: Collection[str] = ()) -> Select:
    """How many of each topic are older than the cutoff, less the topics in `keep` (a consumer's)."""
    query = select(Todo.topic, func.count()).where(Todo.posted_at < cutoff).group_by(Todo.topic)
    return query.where(Todo.topic.not_in(sorted(keep))) if keep else query


def delete_statement(cutoff: datetime, keep: Collection[str] = ()) -> Delete:
    query = delete(Todo).where(Todo.posted_at < cutoff)
    return query.where(Todo.topic.not_in(sorted(keep))) if keep else query


async def prune(now: datetime | None = None) -> dict[str, int]:
    """Delete every to-do older than `KEEP`; returns how many of each topic went, and logs them in one line."""
    cutoff = (now or utc_now()) - KEEP
    keep = consume.consumed_topics()
    async with db.session_factory()() as session:
        gone = {topic: n for topic, n in (await session.execute(counts_statement(cutoff, keep))).all()}
        if gone:
            await session.execute(delete_statement(cutoff, keep))
        await session.commit()
    if gone:
        log.info("work-queue to-dos posted in the last hour: %s (%d in all)",
                 ", ".join(f"{topic}={n}" for topic, n in sorted(gone.items())), sum(gone.values()))
    return gone
