"""What every mission's run and row share, and nothing that reaches a mission: the leaf of the missions module
(`core.py`, `kinds/*.py`). It imports the models, the clock, `wording` and the wake reasons, none of which read a
mission, so a kind's home module (`mail/sync.py`, `research/analysis.py`, `browser/leases.py`, ...) may import it at
the top and never meets a cycle.

The keys of the eight missions are written here once. A run is a row of `MissionRun` whose `started` JSON is the
kind's own business (`COMMON_KEYS` are the only keys the module's engine reads); `end` closes one, `after_commit`
runs a sync effect once the transaction that saved the run really commits.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, event
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.harness import wording
from app.jobsearch.models import MissionRun
from app.jobsearch.work import engine_calls
from app.timeutil import aware, utc_now

#: The missions' keys.
SCOUT, ANALYSE, SWEEP = "scout_boards", "analyse_finds", "sweep_stuck"
READ, READ_MAIL, RESEARCH, BROWSER, GO = (
    "read_boards", "read_mail", "research_employers", "read_browser", "open_apply_tabs")
#: In the page's order (plain strings, so the action door can build its enum at import).
KINDS: tuple[str, ...] = (READ, READ_MAIL, RESEARCH, BROWSER, SCOUT, ANALYSE, SWEEP, GO)

#: `MissionRun.started_by` of a run made beside a mission's own, not by its schedule or an owner's Run now: the catch-up
#: after a tracker import, in the history of Analyse the finds. It is never the mission's own run, so it does not count
#: for the schedule, for "one at a time" or for a night's stop.
IMPORT = "import"

#: Why a night's run ended. `target`, `cap` and `limit` are the owner's limits; `searched` is a scout night with nothing
#: left to search; `stalled` a scout night whose last search could do nothing it had work for; `usage` is Claude's
#: subscription out of usage (docs/PRD.md 25.12). `empty` is a research or analysis run that found fewer than its limit.
STOPS = {"target": "target met", "empty": "nothing left in the queue", "peak": "peak hours", "cap": "the session cap",
         "finish": "the finish time", "searched": "every site searched", "limit": "a limit you set",
         "stalled": "the last search did nothing", "usage": "Claude's usage limit"}

#: The refusal for the mission that a session the user starts runs.
RUNS_ELSEWHERE = wording.say("missions", "runs_elsewhere")

class MissionError(DomainError):
    status = 422


class NoSuchMission(MissionError):
    status = 404


class MissionBusy(MissionError):
    """The mission is running already; a second run now would double its work. It says which run, for the words that
    name it."""

    status = 409

    def __init__(self, message: str, *, run_id: uuid.UUID | None = None, started_by: str | None = None,
                 started_at: datetime | None = None) -> None:
        super().__init__(message)
        self.run_id, self.started_by, self.started_at = run_id, started_by, started_at


#: What to knock the runner for once the caller has committed: (id, reason), `loops.schedule.send_knocks`.
Woken = list[tuple[str, str]]

#: The only keys of a run's `started` that the module's engine reads; every other key is its kind's own. Storage is as
#: it always was: no key is renamed or dropped.
COMMON_KEYS = ("target", "done", "stopped", "instances", "knock", "reason", "site_projects", "search_run")


def own_run() -> ColumnElement[bool]:
    """A run that is the mission's own (not the catch-up beside Analyse the finds), for a `where`."""
    return MissionRun.started_by != IMPORT


def end(entry: MissionRun, outcome: str, summary: str, stop: str | None = None, at: datetime | None = None) -> None:
    """Close a run. `at` is when its work really finished: the schedule pass only looks every few minutes, so a run is
    often noticed to be over long after it was. Never before the run began or after now."""
    now = utc_now()
    when, began = aware(at) or now, aware(entry.started_at)
    entry.outcome, entry.ended_at = outcome, min(max(when, began) if began is not None else when, now)
    entry.summary = summary + (f" Stopped: {STOPS[stop]}." if stop else "")
    entry.started = {**(entry.started or {}), "stopped": stop}


def after_commit(session: AsyncSession, fn: Callable[[], None]) -> None:
    """Run `fn` once, when the transaction that holds what the caller just flushed really commits: not at a savepoint's
    release (the real commit is still to come, so `in_nested_transaction` says no), and never if that work is rolled
    back, whether the savepoint it was flushed in rolls back alone or the whole transaction does. `fn` is sync and
    local: launching a background task, noting an ask. Anything that must wait for an async call is the caller's own,
    after its commit (`Ran.wake`)."""
    sync = session.sync_session
    # The innermost transaction, not the root: a savepoint that rolls back on its own takes the flush with it, and only
    # a walk up from the savepoint itself finds it among the transactions a rollback closes.
    begun = sync.get_nested_transaction() or sync.get_transaction()
    spent: list[bool] = []

    def fire(committed: Any) -> None:
        if spent or committed.in_nested_transaction():
            return
        spent.append(True)
        fn()

    def lost(_session: Any, previous: Any) -> None:
        # The transaction this was flushed in, or one it was inside, was rolled back: what it saved is not there.
        t = begun
        while t is not None:
            if t is previous:
                spent.append(True)
                return
            t = t.parent

    event.listen(sync, "after_commit", fire)
    event.listen(sync, "after_soft_rollback", lost)


def wake_of(started: dict) -> Woken:
    """What a run just made says to knock for: the runner's work it asked for (`knock`, with its `reason`), and the site
    projects it started (`site_projects`)."""
    return [(k, started.get("reason", engine_calls.SEARCH_STARTED)) for k in started.get("knock", [])] + [
        (p, engine_calls.SITE_PROJECT) for p in started.get("site_projects", [])]


def progress_words(started: dict) -> str:
    """How far a research or analysis run got: "8 of 15" against a limit, "8" without one."""
    done, target = int(started.get("done") or 0), int(started.get("target") or 0)
    return f"{done} of {target}" if target > 0 else str(done)


def stop_for(started: dict, count: int) -> str:
    """Why a research or analysis run of `count` ended when all of them were done: `target` when it was limited and
    reached its limit, else `empty` (fewer than the limit, or no limit: all there were)."""
    target = int(started.get("target") or 0)
    return "target" if target > 0 and count >= target else "empty"



def board_feed_of(run: MissionRun) -> dict | None:
    """What the board feed's part of this read did, as the read wrote it on its run (`BOARD_FEED_COUNTS`); None when
    it did none. It lives here, not in `boards_night`, so the owner door's night report reads it on the page."""
    return (run.started or {}).get("board_feed") or None
