"""The coverage comparison: after each pass of the pipeline's turn, did the pass
change anything while no to-do for its topic was posted since its previous pass? That is a miss: a writer did not post
(or posted the wrong topic). Counts only, as the passes already return them.

It only logs (`work-queue coverage: ...`, at INFO) and changes nothing; the log is read at the phase's gate ("zero
changed but nobody told"). Work the clock makes (a role aging out of the first 72 hours) has no writer, so the deck's
passes can show up here until the hour tick posts for them (a later phase). The window of a pass is from the start of
its previous pass to its own start: the to-dos its own writes post come after, so they are the next window's. The first
pass after a start has no window and is not compared."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select

from app import db
from app.jobsearch.models import Todo
from app.timeutil import utc_now

log = logging.getLogger(__name__)

#: When each pass last began, by owner and pass.
_began: dict[tuple[str, str], datetime] = {}
#: The misses found, by pass, since the start (what the tests read; the log is what the owner reads).
misses: dict[str, int] = {}


@dataclass(frozen=True)
class Window:
    """Where to look for the to-dos that told a pass: after its previous pass began, up to its own start."""

    previous: datetime | None
    start: datetime


def told_statement(owner: str, topic: str, window: "Window") -> Select:
    """How many to-dos of the topic were posted in the window."""
    return select(func.count()).select_from(Todo).where(
        Todo.owner == owner, Todo.topic == topic, Todo.posted_at > window.previous, Todo.posted_at <= window.start)


def forget() -> None:
    """Drop what is kept in the process (the tests, between cases)."""
    _began.clear()
    misses.clear()


def begin(owner: str, step: str) -> Window:
    """A pass of `step` is about to run: its window, and the mark its next pass counts from."""
    start = utc_now()
    previous = _began.get((owner, step))
    _began[(owner, step)] = start
    return Window(previous, start)


def counted(out: Any) -> int:
    """How much a pass changed, from what it returned: a count, a pair of counts, the list of what it moved, or the
    filter's report (`looked_at`)."""
    if isinstance(out, bool):
        return int(out)
    if isinstance(out, int):
        return out
    if isinstance(out, tuple):
        return sum(counted(part) for part in out)
    if isinstance(out, list | set):
        return len(out)
    if isinstance(out, dict):
        return int(out.get("looked_at") or 0)
    return 0


async def judge(owner: str, step: str, topic: str, window: Window, changed: int) -> bool:
    """Compare what the pass `step` changed with the to-dos of its `topic` posted in its window; True (and a log line)
    when it changed something and none was posted. Does nothing for the first pass."""
    if changed <= 0 or window.previous is None:
        return False
    try:
        async with db.session_factory()() as session:
            told = await session.scalar(told_statement(owner, topic, window))
    except Exception:  # a comparison that fails must never stop the turn it watches
        log.warning("work-queue coverage: the comparison for %s failed", step, exc_info=True)
        return False
    if told:
        return False
    misses[step] = misses.get(step, 0) + changed
    log.info("work-queue coverage: %s changed %d and no %s to-do was posted since its previous pass",
             step, changed, topic)
    return True
