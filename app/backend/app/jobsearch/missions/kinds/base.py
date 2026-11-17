"""The shape of a kind of mission: the facts about it, and the procedures only the missions module (`core.py`) calls.

A kind is one class and one instance (`KIND`) in its own file, so what is true of a mission, how it starts, how its run
is brought up to date, how it stops and the words that describe it are in one place. `core.py` keeps what the eight
share (the schedule, pause, due, history, the schedule pass, the page's payload) and walks the kinds; it reads a run's
`started` JSON only at `record.COMMON_KEYS`, and every other key is the kind's own.

The facts are public attributes; the procedures are the slots `core` calls and nothing outside the module does (its
tests are the exception: a kind is the seam between `core` and what is particular to one mission, and the tests that
say what any kind must hold call its slots there).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.harness import wording
from app.jobsearch import limits
from app.jobsearch.missions import record
from app.jobsearch.missions.record import MissionError, Woken
from app.jobsearch.models import Mission, MissionRun
from app.jobsearch.work import hours as night

# --- the checks and the words the kinds with done conditions share ---------------------------------------------------


def limit(values: dict, name: str, low: int, high: int, words: str) -> int:
    """A count limit (`limits.py`): -1, no limit, or a whole number in `low` to `high`; nothing else below 0."""
    raw: Any = values.get(name)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise MissionError(f"{words}: a whole number, or -1 for no limit") from None
    if n != limits.NO_LIMIT and not low <= n <= high:
        raise MissionError(f"{words}: {low} to {high}, or -1 for no limit")
    return n


def days(values: dict, name: str, low: int, high: int, words: str) -> int:
    """A number of days: a whole number in `low` to `high`. Not a limit, so -1 is not allowed."""
    raw = values.get(name)
    try:
        n = int(raw) if raw is not None and not isinstance(raw, bool) else None
    except (TypeError, ValueError):
        n = None
    if n is None:
        raise MissionError(f"{words}: a whole number of days")
    if not low <= n <= high:
        raise MissionError(f"{words}: {low} to {high} days")
    return n


def finish(values: dict) -> str | None:
    """The time of day a night is to finish by, as stored ("06:45"), or None when blank."""
    raw = values.get("finish_by")
    if raw in (None, ""):
        return None
    at = night.clock(raw)
    if at is None:
        raise MissionError("finish by: a time of day like 06:00")
    return at.strftime("%H:%M")


def is_limit(n: object) -> bool:
    """A count that is a limit as the owner set it: -1 is no limit (docs/PRD.md 25.12)."""
    return isinstance(n, int | float) and not isinstance(n, bool) and n != limits.NO_LIMIT


def by_words(settings: dict) -> str:
    """" · by 06:00" when the night has a finish time."""
    return f" · by {settings['finish_by']}" if settings.get("finish_by") else ""



def progress_of(entry: MissionRun) -> dict | None:
    """How far a run got against its done condition ("done" of "target"); None against no limit (-1)."""
    started = entry.started or {}
    target = int(started.get("target") or 0)
    return {"done": int(started.get("done") or 0), "target": target} if target > 0 else None


@dataclass(frozen=True)
class Seed:
    """The row a new install starts a mission with."""

    playbook: str | None
    schedule: dict
    settings: dict
    state: Literal["on", "paused"]
    #: The clock the schedule counts from, when it is not the row's making.
    checked_at: datetime | None = None


@dataclass(frozen=True)
class Start:
    """What a kind's start did: how the run begins, in what words, and what it keeps on the run."""

    outcome: Literal["running", "done", "skipped", "failed", "nothing"]
    summary: str
    state: dict
    #: Runs after the transaction that saved the run commits (`record.after_commit`): a background task that looks for
    #: the run by id must not begin before it is there.
    launch: Callable[[uuid.UUID], None] | None = None


class Kind:
    """One subclass for each mission, one instance for each kind."""

    key: str
    name: str
    #: The work, as a sentence names the mission: "Waits: the scouting mission is paused".
    activity: str
    #: Claude's work: peak hours hold it, and Run now refuses inside them.
    needs_claude: bool = False
    #: The robot whose work it is on the page, where one does it. Not the same fact as `group`: Go and the browser have
    #: a group but no robot.
    agent: Literal["scout", "analyst"] | None = None
    #: The `session_kinds.AGENT_KINDS` key whose hand-out the owner's Run now makes theirs.
    group: str | None = None
    #: Started by a session the user starts, never by the app.
    elsewhere = False
    #: First in the schedule pass (a stuck search is closed before the scout looks for an open one).
    early = False
    #: The done conditions it starts with (docs/PRD.md 25.10).
    defaults: dict = {}

    def purpose(self) -> str:
        """What the mission is for, in the app's own words (`templates/wording.missions.yaml`)."""
        return wording.say("missions", f"purpose_{self.key}")

    def first(self, last_search: datetime | None) -> Seed:
        """A new install's row. `last_search` is when this install last searched, None if it never has."""
        raise NotImplementedError

    def check(self, values: dict) -> dict:
        """The done conditions as stored, or a `MissionError` saying what does not fit. A key the mission no longer has
        is dropped."""
        return {}

    def done_when(self, settings: dict) -> str | None:
        """What finishes the mission's night, in one line for the page (docs/PRD.md 25.10); None says nothing."""
        return None

    async def start(self, session: AsyncSession, owner: str, mission: Mission, by: str) -> Start:
        """Begin the work now. Flushes; the caller commits."""
        raise NotImplementedError

    async def hold(self, session: AsyncSession, owner: str, mission: Mission, now: datetime,
                   due_at: datetime) -> Literal["wait", "skip"] | None:
        """A scheduled start that is due: `wait` leaves it due for the next pass, `skip` gives the day up, None starts
        it."""
        return None

    async def settle(self, session: AsyncSession, entry: MissionRun) -> None:
        """Bring a running run up to date from what it started; may close it (`record.end`). The caller flushes."""

    async def reads_as(self, session: AsyncSession, entry: MissionRun) -> None:
        """What `settle` would leave on a run, for a read of the missions: `entry` is a copy of the run that is in no
        session (`core.shown`), so nothing is written. A kind whose `settle` changes a row other than the run says it
        its own way (`read_browser.py`)."""
        await self.settle(session, entry)

    async def tend(self, session: AsyncSession, owner: str, mission: Mission, entry: MissionRun, peak: bool,
                   now: datetime) -> Woken:
        """The schedule pass's turn for a run that is still going and settled. Returns what to knock for."""
        return []

    async def stop(self, session: AsyncSession, entry: MissionRun, stop: Literal["finish", "usage"],
                   note: str = "") -> bool:
        """Give back what nobody began and end the run, because the finish time came or Claude's usage ran out. True
        when the kind took the stop; False when it is not stopped by either, or the run had ended by itself."""
        return False

    async def strays(self, session: AsyncSession, owner: str, stop: Literal["finish", "usage"], note: str = "") -> None:
        """Work of the kind that no run owns, wound down when Claude's usage runs out."""

    async def recover(self, session: AsyncSession, owner: str, now: datetime) -> int:
        """After a restart: take up or close what the restart cut short. Returns how many runs it was."""
        return 0

    def progress(self, entry: MissionRun) -> dict | None:
        """How far a run got against its done condition, as the page reads it; None against no limit."""
        return progress_of(entry)


class Batch(Kind):
    """A night's batch of work handed out one at a time (the roles to research, the employers to research): it ends when
    every one is done, at the finish time, or when Claude's usage runs out, and what no session began is given back to
    its queue for the next night."""

    async def release(self, session: AsyncSession, entry: MissionRun, why: str) -> None:
        """Give back what the run started and nobody has begun."""
        raise NotImplementedError

    async def stop(self, session: AsyncSession, entry: MissionRun, stop: Literal["finish", "usage"],
                   note: str = "") -> bool:
        if stop == "usage":
            await self.settle(session, entry)  # what finished, before the rest goes back
            if entry.outcome != "running":
                return False
            why = f"stopped: {record.STOPS['usage']}"
        else:
            why = "stopped at the finish time"
        started = dict(entry.started or {})
        await self.release(session, entry, why)
        record.end(entry, "done", f"Got to {record.progress_words(started)}.{note}", stop)
        return True
