"""Safe points in the work of a long background read (`boards_night.py`).

A long loop (the descriptions slice by slice) names the places where everything it has written so far is whole:
`await checkpoint.reached()`. (The boards are saved one by one wherever they are read: `postings.finished_first`
releases the session before each wait, `db.release_if_free`.) What happens there is the caller's to say. The driver
of a long read (`saving_as_it_goes`) has the work saved at each, so no row it wrote is held for longer than a slice;
anywhere else nobody has, and `reached` does nothing (the slice's own read releases the session before it begins:
`postings.fill_details`).

So the loops stay services that flush, and the one that owns the session is the one that commits. A safe point is only
named where stopping is harmless: the next read takes up what was not reached, as it takes up what a read that ran out
of time left.
"""

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

#: What to do at a safe point, for the task that set it; None: nothing.
SAVE: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar("checkpoint_save", default=None)


async def reached() -> None:
    """A safe point: everything written so far is whole. Saved if the task's driver asked for it, else nothing."""
    save = SAVE.get()
    if save is not None:
        await save()


@contextlib.asynccontextmanager
async def saving_as_it_goes(session: AsyncSession) -> AsyncIterator[None]:
    """For the work inside the block, a safe point commits `session`. The caller still commits at the end for the part
    after the last safe point."""
    token = SAVE.set(session.commit)
    try:
        yield
    finally:
        SAVE.reset(token)
