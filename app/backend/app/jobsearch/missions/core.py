"""Missions (docs/PRD.md path 18): the standing work as rows the owner can see, pause, run and change. The module is
this file and one file for each kind of mission in `kinds/` (`kinds/base.py` says what a kind is): a mission's facts,
how it starts, how a run of it is brought up to date, how it stops and the words that describe it are in its own file.
This file keeps what the eight share: the schedule and when a mission is due, pause, resume and change, the history,
the schedule pass, and what the page reads. A run is a row of `MissionRun` whose `started` JSON is its kind's own
business; the module reads only `record.COMMON_KEYS` of it.

A run that starts a search or a batch stays running until its work ends. The schedule pass brings it up to date and
saves that (`refresh`); a read of the missions shows it as that would leave it, worked out on a copy, and writes
nothing (`shown`). A new install's missions start paused except the sweep, the read and the morning batch; an install
that has searched before keeps searching. Services flush; the route, the schedule pass or the door commits.

The kinds are brought in on the first use (`_wired`), and the modules whose work the schedule pass walks where the
pass runs, not at the top: they reach a mission through this module's names, so importing them here would close a ring.
"""

import copy
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal, overload

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.agents import roster
from app.jobsearch.actors import BY_APP
from app.jobsearch.missions import numbers as mission_numbers
from app.jobsearch.missions import record
from app.jobsearch.missions.kinds.base import Kind, progress_of
from app.jobsearch.missions.record import (
    ANALYSE,
    BROWSER,
    GO,
    KINDS,
    READ,
    READ_MAIL,
    RESEARCH,
    SCOUT,
    STOPS,
    SWEEP,
    MissionBusy,
    MissionError,
    NoSuchMission,
    Woken,
)
from app.jobsearch.models import Mission, MissionChange, MissionRun, SearchRun
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from app.timeutil import aware, iso_or_none, owner_zone, utc_now

log = logging.getLogger(__name__)

# The module's interface (`core as missions`): the keys, the order and the stops are the leaf's (`record.py`),
# re-exported; the verbs are the owner's controls (routes and the door), the app's clock, and what the rest of the app
# reads of a mission. Not in it: the engine's own steps (`start`, `advance`, `refresh`, `shown`, `due`, `run_view`,
# `check_settings`, `check_schedule`, `next_time`, `schedule_words`). Nothing outside this file calls them; they are
# module-level so this module's own tests can drive each at the smallest layer that can fail (the project's testing
# rules), and a
# caller outside it goes through `run_now`, `schedule_pass`, `change`, `listing` and `detail`.
__all__ = [
    # what the leaf says
    "ANALYSE", "BROWSER", "GO", "KINDS", "READ", "READ_MAIL", "RESEARCH", "SCOUT", "STOPS", "SWEEP",
    "MissionBusy", "MissionError", "NoSuchMission", "Woken",
    # what the module hands back
    "Ran", "Standing",
    # the owner's controls
    "get", "listing", "detail", "pause", "resume", "change", "run_now", "read_run", "refuse_a_second",
    # the app's clock
    "ensure", "settle_after_restart", "schedule_pass", "usage_limit_hit",
    # what the rest of the app reads of a mission
    "kind_of", "standing", "open_run", "SITE_PROJECTS_A_NIGHT", "EVERY_LEAST",
]

#: The longest and shortest "every N hours".
EVERY_MOST, EVERY_LEAST = 24 * 30, 0.25
RUNS_SHOWN = 50
#: The sites that get a project of their own in one night's Scout mission when the owner has not said: the default of
#: their Settings' `site_projects_per_night` (Scout the boards' "Sites to learn a night", -1: no limit; a test keeps
#: the two together).
SITE_PROJECTS_A_NIGHT = 3


@dataclass(frozen=True)
class Ran:
    """What a Run now did: the run as the page reads it, and what to knock the runner for once the caller has committed
    (`loops.schedule.send_knocks`)."""

    run: dict
    wake: Woken


@dataclass(frozen=True)
class Standing:
    """What the rest of the app may read of a mission, in words and values and never the row: whether this install has
    made it and has it on, its schedule and the schedule in words, what a thing that waits for it says while it is
    paused, the settings it runs with (its kind's defaults under what is stored) and when it last ran."""

    made: bool
    on: bool
    schedule: dict
    when: str
    waits: str
    settings: dict
    last_run_at: datetime | None


_wiring: dict[str, Kind] | None = None


def _wired() -> dict[str, Kind]:
    """The eight kinds (`kinds/*.py`), by key, brought in on the first call and not at the top: they reach a mission
    through this module's names, so importing them here would close a ring. They are `record.KINDS`, in its order: a
    table that is not says so here, at the first use, and not in a test that keeps two lists equal."""
    global _wiring
    if _wiring is not None:
        return _wiring
    from app.jobsearch.missions.kinds import (
        analyse,
        go,
        read_boards,
        read_browser,
        read_mail,
        research,
        scout,
        sweep,
    )

    kinds: dict[str, Kind] = {k.key: k for k in (
        read_boards.KIND, read_mail.KIND, research.KIND, read_browser.KIND, scout.KIND, analyse.KIND, sweep.KIND,
        go.KIND)}
    if tuple(kinds) != KINDS:
        raise RuntimeError(f"core._wired holds {', '.join(kinds)} and record.KINDS is {', '.join(KINDS)}: one kind "
                           "for each key, in the page's order")
    _wiring = kinds
    return kinds


def kind_of(key: str) -> Kind | None:
    """The kind of the mission with this key, for its facts (name, defaults, whether it needs Claude, ...); None for a
    row the app has no kind for. Its procedures are the module's own."""
    return _wired().get(key)


def _name(key: str | None) -> str | None:
    """The title of the mission with this key, None for a row of no kind."""
    kind = kind_of(key) if key is not None else None
    return kind.name if kind is not None else None


def _defaults(key: str) -> dict:
    kind = kind_of(key)
    return kind.defaults if kind is not None else {}


def _early(key: str) -> bool:
    """Whether the mission goes first in the schedule pass."""
    kind = kind_of(key)
    return kind.early if kind is not None else False


def _needs_claude(key: str) -> bool:
    """Whether peak hours hold the mission (a row of no kind is held: nothing says it is the app's own work)."""
    kind = kind_of(key)
    return kind.needs_claude if kind is not None else True


# --- the missions at first --------------------------------------------------------------------------------------


async def ensure(session: AsyncSession, owner: str) -> int:
    """Make any of the missions at first that are missing, and bring what each is for up to date. Safe to run again.
    Returns how many it made."""
    have = set((await session.scalars(select(Mission.key).where(Mission.owner == owner))).all())
    last_search = await session.scalar(select(func.max(SearchRun.started_at)).where(SearchRun.owner == owner))
    made = 0
    for key in KINDS:
        if key in have:
            continue
        kind = _wired()[key]
        seed = kind.first(last_search)
        mission = Mission(owner=owner, key=key, name=kind.name, purpose=kind.purpose(), playbook=seed.playbook,
                          schedule=seed.schedule, settings=seed.settings, state=seed.state,
                          checked_at=seed.checked_at)
        session.add(mission)
        await session.flush()
        session.add(MissionChange(owner=owner, mission_id=mission.id, by=BY_APP, what=(
            f"Made it, {'on' if mission.state == 'on' else 'paused'}, {schedule_words(mission.schedule)}."),
            why="one of the missions every install starts with"))
        made += 1
    # A mission gains the done conditions a release added (docs/PRD.md 25.10), with what it had kept: the Claude
    # missions are the ones that have them.
    rows = (await session.scalars(select(Mission).where(Mission.owner == owner))).all()
    for mission in rows:
        kind = kind_of(mission.key)
        if kind is None or not kind.needs_claude:
            continue
        new = check_settings(mission.key, {**kind.defaults, **(mission.settings or {})})
        if new != mission.settings:
            added = sorted(set(new) - set(mission.settings or {}))
            mission.settings = new
            session.add(MissionChange(owner=owner, mission_id=mission.id, by=BY_APP,
                                      what=f"Added done conditions: {', '.join(k.replace('_', ' ') for k in added)}.",
                                      why="missions now say when their night's work is done"))
    # What a mission is for is the app's own words, and nothing lets the owner change them: every start brings an older
    # install's up to date (the way the scout's description is, `roster.ensure_roster`), so the page never keeps
    # describing work a release has changed.
    for mission in rows:
        kind = kind_of(mission.key)
        if kind is not None:
            mission.purpose = kind.purpose()
    await session.flush()
    return made


# --- schedules --------------------------------------------------------------------------------------------------


def _hours_words(hours: float) -> str:
    if hours < 1:
        return f"every {round(hours * 60)} minutes"
    if hours == 1:
        return "every hour"
    if hours % 24 == 0:
        days = int(hours // 24)
        return "every day" if days == 1 else f"every {days} days"
    return f"every {hours:g} hours"


def schedule_words(schedule: dict) -> str:
    kind = schedule.get("kind")
    if kind == "every":
        return _hours_words(float(schedule.get("hours") or 24))
    if kind == "daily":
        return f"every day at {schedule.get('at')}"
    if kind == "after":
        return f"when {_name(schedule.get('mission')) or 'another mission'} finishes"
    if kind == "outside":
        return "when your computer's scheduled session calls in"
    return "only when you run it"


def check_schedule(schedule: dict) -> dict:
    """A schedule as stored, or a MissionError saying what is wrong with it."""
    kind = (schedule or {}).get("kind")
    if kind == "every":
        try:
            hours = float(schedule.get("hours"))
        except (TypeError, ValueError):
            raise MissionError("every how many hours: a number") from None
        if not EVERY_LEAST <= hours <= EVERY_MOST:
            raise MissionError(f"every {EVERY_LEAST:g} to {EVERY_MOST} hours")
        return {"kind": "every", "hours": hours}
    if kind == "daily":
        at = str(schedule.get("at") or "")
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at)
        if not m:
            raise MissionError("a time of day like 02:00")
        return {"kind": "daily", "at": f"{int(m.group(1)):02d}:{m.group(2)}"}
    if kind == "on_demand":
        return {"kind": "on_demand"}
    if kind == "after":
        if schedule.get("mission") not in KINDS:
            raise MissionError(f"after one of: {', '.join(KINDS)}")
        return {"kind": "after", "mission": schedule["mission"]}
    raise MissionError("a schedule is every N hours, daily at a time, after another mission, or only when you run it")


def check_settings(key: str, values: dict) -> dict:
    """A mission's own settings: its done conditions (docs/PRD.md 25.10), and the search's focus. Every count in them is
    a limit that takes -1, no limit (`limits.py`); a key the mission no longer has is dropped. A row of no kind has
    none."""
    kind = kind_of(key)
    if kind is None:
        return {}
    return kind.check({**kind.defaults, **(values or {})})


async def _last_started(session: AsyncSession, mission: Mission) -> datetime | None:
    # The catch-up after an import (catch_up.py) is in the history of Analyse the finds but not on its schedule.
    last_run = await session.scalar(select(func.max(MissionRun.started_at)).where(
        MissionRun.mission_id == mission.id, record.own_run()))
    times = [t for t in (aware(last_run), aware(mission.checked_at)) if t is not None]
    return max(times) if times else None


def next_time(schedule: dict, last: datetime | None, now: datetime | None = None) -> datetime | None:
    """When the schedule next comes up after `last` (None: only when run)."""
    now = now or utc_now()
    kind = schedule.get("kind")
    if kind == "every":
        return now if last is None else last + timedelta(hours=float(schedule.get("hours") or 24))
    if kind == "daily":
        zone = owner_zone()
        hour, minute = (int(x) for x in str(schedule.get("at") or "02:00").split(":"))
        after = (last or now - timedelta(days=1)).astimezone(UTC)
        day = after.astimezone(zone).date()
        # The first time the clock reads `at` after `last`, as instants: on fall-back night a time inside the repeated
        # hour is its first pass only, so a day's run is not done twice.
        return next(m for d in (day, day + timedelta(days=1))
                    if (m := night.wall_moment(d, time(hour, minute), zone)) > after)
    return None


async def _after_ended(session: AsyncSession, mission: Mission) -> datetime | None:
    """When the mission this one follows last finished a run (done, not failed or skipped). A night that stopped
    because Claude's usage ran out is not followed: the research would hit the same limit, and what it would have
    taken waits for the next night."""
    lead = await session.scalar(select(Mission).where(Mission.owner == mission.owner,
                                                      Mission.key == mission.schedule.get("mission")))
    if lead is None:
        return None
    runs = (await session.scalars(select(MissionRun).where(
        MissionRun.mission_id == lead.id, MissionRun.outcome == "done", MissionRun.ended_at.is_not(None))
        .order_by(MissionRun.ended_at.desc()).limit(10))).all()
    return next((aware(r.ended_at) for r in runs if (r.started or {}).get("stopped") != "usage"), None)


async def due(session: AsyncSession, mission: Mission, now: datetime | None = None) -> bool:
    if mission.state != "on":
        return False
    if mission.schedule.get("kind") == "after":
        ended = await _after_ended(session, mission)
        last = await _last_started(session, mission) or aware(mission.created_at)
        return ended is not None and (last is None or ended > last)
    at = next_time(mission.schedule, await _last_started(session, mission), now)
    return at is not None and at <= (now or utc_now())


# --- running one ------------------------------------------------------------------------------------------------


async def start(session: AsyncSession, owner: str, mission: Mission, by: str = "schedule") -> MissionRun | None:
    """Run a mission now. Returns its run, or None when a scheduled sweep found nothing (only `checked_at` moves, so
    the runs list holds what happened, not every look at the clock). A mission of no kind is refused."""
    kind = kind_of(mission.key)
    if kind is None:
        raise NoSuchMission(f"the app has no kind of mission called {mission.key}")
    began = await kind.start(session, owner, mission, by)
    now = utc_now()
    mission.checked_at = now
    if began.outcome == "nothing" and by == "schedule":
        await session.flush()
        return None
    entry = MissionRun(owner=owner, mission_id=mission.id, started_by=by, started_at=now,
                       outcome="done" if began.outcome == "nothing" else began.outcome, summary=began.summary,
                       started=began.state, ended_at=None if began.outcome == "running" else now)
    session.add(entry)
    await session.flush()
    if entry.outcome == "running" and began.launch is not None:
        launch = began.launch
        record.after_commit(session, lambda: launch(entry.id))
    return entry


@overload
async def run_now(session: AsyncSession, owner: str, mission: Mission, *, by: str,
                  dry_run: Literal[False] = ...) -> Ran: ...


@overload
async def run_now(session: AsyncSession, owner: str, mission: Mission, *, by: str,
                  dry_run: Literal[True]) -> None: ...


async def run_now(session: AsyncSession, owner: str, mission: Mission, *, by: str,
                  dry_run: bool = False) -> Ran | None:
    """Run now, whatever the schedule and even when paused (the route's body, `POST /missions/{id}/run`, and the action
    door's `mission.run`, PRD path 50.4). `by` is who asked: `actors.YOU`, or `actors.CLAUDE` through the door.

    It refuses, in this order: Claude work inside the peak hours the owner chose to avoid (`night.PeakHours`, 409; only
    a kind that needs Claude), a mission already running (`MissionBusy`, 409; a catch-up beside the night never counts),
    and the mission the app never starts (`MissionError`, 422). A kind's own skip (no mailbox, no search possible) is
    not a refusal: it is a run that says it was skipped. With `dry_run` it raises the same and starts nothing (it writes
    only what bringing a stale running run up to date writes) and returns None.

    The caller commits, then sends `Ran.wake` (`schedule.send_knocks`): a knock is async and must follow the commit.
    What is sync and local follows the commit by itself: a board read or mail read the start launches, never when the
    transaction rolls back. The owner's ask (its work is theirs, handed out outside the off hours too,
    `pool.owner_started_mission`) is a row of the same transaction: there when the run is, gone when it rolls back."""
    kind = kind_of(mission.key)
    if _needs_claude(mission.key):  # the sweep and the board read are the app's own work, no Claude
        await night.refuse_in_blackout(session, owner)
    await refuse_a_second(session, mission)
    if kind is not None and kind.elsewhere:
        raise MissionError(record.RUNS_ELSEWHERE)
    if dry_run:
        return None
    entry = await start(session, owner, mission, by=by)
    if entry is None:  # only a scheduled sweep that found nothing leaves no run
        raise RuntimeError("a run asked for by hand always leaves its run")
    key = mission.key
    from app.jobsearch.pool import core as pool  # it reads this module

    # The owner's ask is a row of this transaction (`pool.owner_started_mission`): committed with the run, and rolled
    # back with it, a savepoint's rollback too.
    await pool.owner_started_mission(session, owner, key)
    return Ran(run=run_view(entry, key), wake=record.wake_of(entry.started or {}))


async def settle_after_restart(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """At the start: what a restart cut short is taken up or closed, by the kind it belongs to (`Kind.recover`: a board
    read lives in the app's own process, so one marked running was cut short). Returns how many runs it was."""
    now = now or utc_now()
    cut = 0
    for kind in _wired().values():
        cut += await kind.recover(session, owner, now)
    return cut


async def refuse_a_second(session: AsyncSession, mission: Mission) -> None:
    """One run of a mission at a time: Run now on a mission whose last run is still going is refused (a second board
    read, search or night's packets on top of the first would double the work); the schedule never starts one over a
    running one. A run that has in fact finished is brought up to date first. (A catch-up after an import runs beside
    its mission's night, never in its way.)"""
    running = await session.scalar(select(MissionRun).where(
        MissionRun.mission_id == mission.id, MissionRun.outcome == "running",
        record.own_run())
        .order_by(MissionRun.started_at.desc()).limit(1))
    if running is None:
        return
    await refresh(session, running)
    if running.outcome == "running":
        raise MissionBusy(f"{mission.name} is running already", run_id=running.id, started_by=running.started_by,
                          started_at=running.started_at)


async def refresh(session: AsyncSession, entry: MissionRun) -> MissionRun:
    """A running run's progress and outcome, from what it started: its kind says (`Kind.settle`): the searches' ends (a
    search that ends below the target leaves the run waiting for the next, which `advance` starts), every role or
    employer sent for research done, a read's process over. A row of no kind has nothing to settle."""
    if entry.outcome != "running":
        return entry
    mission = await session.get(Mission, entry.mission_id)
    kind = kind_of(mission.key) if mission is not None else None
    if kind is not None:
        await kind.settle(session, entry)
        await session.flush()
    return entry


def _copy(entry: MissionRun) -> MissionRun:
    """The run as a row that is in no session: what a read works out on it is never flushed."""
    return MissionRun(id=entry.id, owner=entry.owner, mission_id=entry.mission_id, started_by=entry.started_by,
                      started_at=entry.started_at, ended_at=entry.ended_at, outcome=entry.outcome,
                      summary=entry.summary, started=copy.deepcopy(entry.started or {}))


async def shown(session: AsyncSession, entry: MissionRun, key: str) -> MissionRun:
    """A run as a read of the missions shows it. `key` is its mission's. A running run is shown as `refresh` would
    leave it (its kind's `reads_as`), worked out on a copy: the owner sees a run that has ended as ended at once, and
    the read writes nothing and changes no row of the session. The schedule pass saves it (`_tick`, `advance`,
    `refresh`). A run that has ended, or a row of no kind, is shown as it is."""
    kind = kind_of(key)
    if entry.outcome != "running" or kind is None:
        return entry
    seen = _copy(entry)
    await kind.reads_as(session, seen)
    return seen


async def usage_limit_hit(session: AsyncSession, owner: str, said: str) -> int:
    """Claude's subscription is out of usage: the runner's heartbeat says a session ended on it (docs/PRD.md 25.12).
    Nothing that needs Claude can go on until the limit resets, so every night running now ends cleanly: a scout night
    keeps what it found (a search still open is wound down) and leaves the rest for the next night; research and
    analysis nobody began go back to their queues. `said` is Claude's own words, kept short in the summary. Returns how
    many runs it ended. The catch-up after a tracker import is not one of them: it is held until the limit lifts, and
    goes on (`catch_up.note_usage_limit`)."""
    text = " ".join((said or "").split())[:200]
    note = f" Claude said: \"{text}\"." if text else ""
    rows = (await session.execute(
        select(MissionRun, Mission).join(Mission, Mission.id == MissionRun.mission_id)
        .where(MissionRun.owner == owner, MissionRun.outcome == "running", record.own_run())
        .order_by(MissionRun.started_at))).all()
    ended = 0
    for entry, mission in rows:
        kind = kind_of(mission.key)
        if kind is not None and await kind.stop(session, entry, "usage", note):
            ended += 1
    # Work of a kind that no run owns (a search that no mission started, Search now) is wound down the same way.
    for kind in _wired().values():
        await kind.strays(session, owner, "usage", note)
    return ended


async def advance(session: AsyncSession, owner: str, mission: Mission, entry: MissionRun, peak: bool,
                  now: datetime) -> Woken:
    """Carry a running night on: brought up to date, stopped at its finish time, or given the kind's turn (the scout's
    next search, the mail reader's wait). Returns what to wake the runner for."""
    await refresh(session, entry)
    if entry.outcome != "running":
        return []
    kind = kind_of(mission.key)
    if kind is None:
        return []
    finish = night.finish_moment(aware(entry.started_at), mission.settings.get("finish_by"))
    if finish is not None and now >= finish:
        await kind.stop(session, entry, "finish")
        return []
    return await kind.tend(session, owner, mission, entry, peak, now)


async def _tick(session: AsyncSession, owner: str, mission: Mission, peak: bool, now: datetime) -> Woken:
    """One mission's turn in the schedule pass: its pause ends if it ran out, a running night is carried on, or a due
    mission starts. Returns what to wake the runner for."""
    until = aware(mission.paused_until)
    if mission.state == "paused" and until is not None and until <= now:
        mission.state, mission.paused_until = "on", None
        session.add(MissionChange(owner=owner, mission_id=mission.id, by=BY_APP, what="Resumed.",
                                  why="the pause ran out"))
    running = await session.scalar(select(MissionRun).where(
        MissionRun.mission_id == mission.id, MissionRun.outcome == "running",
        record.own_run())
        .order_by(MissionRun.started_at.desc()).limit(1))
    if running is not None:
        return await advance(session, owner, mission, running, peak, now)
    # Peak hours hold every mission that needs Claude; the sweep and the read are the app's own work (25.8).
    if peak and _needs_claude(mission.key):
        return []
    if not await due(session, mission, now):
        return []
    kind = kind_of(mission.key)
    if kind is not None:
        due_at = next_time(mission.schedule, await _last_started(session, mission), now) or now
        held = await kind.hold(session, owner, mission, now, due_at)
        if held == "skip":
            mission.checked_at = now  # the day is over: wait for the next one
        if held is not None:
            return []  # "wait": still due, it starts on a later pass
    entry = await start(session, owner, mission, "schedule")
    return record.wake_of(entry.started) if entry is not None else []


async def _note_failed_tick(session: AsyncSession, owner: str, mission_id: uuid.UUID, key: str, now: datetime,
                            exc: Exception) -> None:
    """A mission that raised in the schedule pass: when it was starting, its run is recorded as failed with why, so the
    Missions screen says so and the schedule moves on (a failed start counts as the last start). A running night that
    raised is left as it is; the next pass tries again."""
    try:
        async with session.begin_nested():
            if await session.scalar(select(MissionRun.id).where(
                    MissionRun.mission_id == mission_id, MissionRun.outcome == "running",
                    record.own_run()).limit(1)):
                return
            mission = await session.get(Mission, mission_id)
            if mission is None:
                return
            mission.checked_at = now
            session.add(MissionRun(owner=owner, mission_id=mission_id, started_by="schedule", started_at=now,
                                   ended_at=now, outcome="failed", started={},
                                   summary=f"{_name(key) or key} couldn't start: {exc}"[:500]))
    except Exception:
        log.exception("couldn't record that the mission %s failed", key)


async def schedule_pass(session: AsyncSession, owner: str, now: datetime | None = None) -> Woken:
    """One look at the clock: a pause that has run out ends, and every mission that is due runs. Returns what to wake
    the scout for, as (id, reason). A mission that raises is logged and recorded as a failed run, in a savepoint of its
    own: the others still get their turn. Each mission's turn and each part after them is saved as it ends
    (`db.release_if_free`), so no row one of them writes (a mission's, the browser's queue, a catch-up's) is held for
    the rest of the pass; the caller commits the last."""
    from app.jobsearch.browser import queue as browser_queue  # these read this module, so they are brought in here
    from app.jobsearch.fetch import polite
    from app.jobsearch.missions import catch_up
    from app.jobsearch.research import analysis

    now = now or utc_now()
    knocks: Woken = []
    profile = await profile_store.current(session, owner)
    peak = night.in_peak(profile.settings, now)
    rows = (await session.scalars(select(Mission).where(Mission.owner == owner).order_by(Mission.created_at))).all()
    # The sweep first, so a stuck search is closed before the scout mission looks for an open one.
    for mission in sorted(rows, key=lambda m: not _early(m.key)):
        mission_id, key = mission.id, mission.key  # a rolled-back savepoint expires the row
        try:
            async with session.begin_nested():
                knocks += await _tick(session, owner, mission, peak, now)
        except Exception as exc:
            log.exception("the mission %s failed in the schedule pass", key)
            await _note_failed_tick(session, owner, mission_id, key, now, exc)
        await db.release_if_free(session)
    # A finished search whose finds have all been researched (or sent back) is over: its research step ends.
    try:
        async with session.begin_nested():
            await analysis.settle_rounds(session, owner)
    except Exception:
        log.exception("couldn't end the research of a finished search")
    await db.release_if_free(session)
    # A role that waited for the posting's text and holds it now (the app's own read brought it) goes back in the
    # analyst's line; the runner is woken below like for any work waiting. No savepoint: each role is put in line in a
    # transaction of its own, so none is held for the rest and the read its start makes ahead is made with none open.
    try:
        await analysis.resume_waiting(session, owner)
    except Exception:
        log.exception("couldn't put the roles that waited for their posting's text back in line")
        if db.can_release(session):  # the one role's work in hand is undone; every part before it was saved
            await session.rollback()
    await db.release_if_free(session)
    # The browser's queue: leases that ran out go back, it is made again from the roles and sites, and a stall is
    # raised.
    try:
        async with session.begin_nested():
            await browser_queue.watch(session, owner, now)
    except Exception:
        log.exception("couldn't look after the browser's queue")
    await db.release_if_free(session)
    # The catch-up after a tracker import runs beside the missions (catch_up.py): the next role goes to the analyst, a
    # hold on Claude (peak hours, its usage limit) ends, and a few employers' job systems are looked for.
    try:
        answers = await catch_up.read_ahead(session, owner, now)  # the hand-outs' postings, before the savepoint
        async with session.begin_nested():
            with polite.read_ahead(answers):
                knocks += await catch_up.advance(session, owner, heavy=True, now=now)
    except Exception:
        log.exception("the catch-up after an import failed in the schedule pass")
    await db.release_if_free(session)
    # Work still waiting with the runner between sessions (after peak hours, or a wake it missed): wake it.
    if not knocks and not peak:
        waiting = session_kinds.kind(await session_kinds.next_kind(session, owner) or "")
        conn = await roster.connection_by_slug(session, owner, roster.SCOUT)
        if waiting is not None and (conn is None or conn.session_kind is None):
            knocks.append(("waiting", waiting.wake))
    await session.flush()
    return knocks


# --- the owner's controls ---------------------------------------------------------------------------------------


async def get(session: AsyncSession, owner: str, which: uuid.UUID | str) -> Mission:
    """The owner's mission with this id (the route's) or key (the door's); `NoSuchMission` when there is none. It never
    makes a row: the missions an install starts with are `ensure`'s."""
    if isinstance(which, uuid.UUID):
        mission = await session.get(Mission, which)
        if mission is None or mission.owner != owner:
            raise NoSuchMission("no such mission")
        return mission
    mission = await session.scalar(select(Mission).where(Mission.owner == owner, Mission.key == which))
    if mission is None:
        raise NoSuchMission(f"there is no mission {which}")
    return mission


async def standing(session: AsyncSession, owner: str, key: str) -> Standing:
    """What the rest of the app reads of the mission with this key (`Standing`); it never raises. An install that has
    not made the row is `made=False`: off, with the kind's defaults and no schedule."""
    kind = kind_of(key)
    mission = await session.scalar(select(Mission).where(Mission.owner == owner, Mission.key == key))
    activity = kind.activity if kind is not None else key
    waits = f"Waits: the {activity} mission is paused"
    defaults = dict(kind.defaults) if kind is not None else {}
    if mission is None:
        return Standing(made=False, on=False, schedule={}, when=schedule_words({}), waits=waits, settings=defaults,
                        last_run_at=None)
    last = await session.scalar(select(func.max(MissionRun.started_at)).where(MissionRun.mission_id == mission.id))
    schedule = mission.schedule or {}
    return Standing(made=True, on=mission.state == "on", schedule=schedule, when=schedule_words(schedule), waits=waits,
                    settings={**defaults, **(mission.settings or {})}, last_run_at=last)


async def open_run(session: AsyncSession, owner: str, key: str, *, started_by: str, summary: str, started: dict,
                   at: datetime | None = None) -> MissionRun:
    """A run made outside the kind's own start: the catch-up after an import (in Analyse the finds' history), a
    browser session's. It is running; whoever opened it ends it. Makes the missions if this install has not
    (`ensure`). Flushes."""
    mission = await session.scalar(select(Mission).where(Mission.owner == owner, Mission.key == key))
    if mission is None:
        await ensure(session, owner)
        mission = await get(session, owner, key)
    entry = MissionRun(owner=owner, mission_id=mission.id, started_by=started_by, started_at=at or utc_now(),
                       outcome="running", summary=summary, started=started)
    session.add(entry)
    await session.flush()
    return entry


async def read_run(session: AsyncSession, owner: str, run_id: uuid.UUID) -> dict | None:
    """One run as the page reads it, brought up to date first (the door's follow of a job it started); None when the
    owner has no such run."""
    entry = await session.get(MissionRun, run_id)
    if entry is None or entry.owner != owner:
        return None
    mission = await session.get(Mission, entry.mission_id)
    await refresh(session, entry)
    return run_view(entry, mission.key if mission is not None else "")


def _note(session: AsyncSession, mission: Mission, by: str, what: str, why: str) -> None:
    session.add(MissionChange(owner=mission.owner, mission_id=mission.id, by=by, what=what,
                              why=" ".join((why or "").split())))


async def pause(session: AsyncSession, mission: Mission, *, by: str, until: datetime | None = None,
                why: str = "") -> None:
    if until is not None and aware(until) <= utc_now():
        raise MissionError("pause until a time that hasn't passed")
    until = aware(until)  # a time with no zone is UTC, as everywhere (`timeutil.aware`)
    mission.state, mission.paused_until = "paused", until
    # The owner reads it in their own zone ("3:00 PM", "Oct 14, 3:00 AM"), not as a UTC ISO text.
    _note(session, mission, by, f"Paused{' until ' + night.when_words(until) if until else ''}.", why)
    await session.flush()


async def resume(session: AsyncSession, mission: Mission, *, by: str, why: str = "") -> None:
    mission.state, mission.paused_until = "on", None
    # Counted from now, as a new schedule is: switching a paused mission on in the afternoon doesn't run its night
    # at once.
    mission.checked_at = utc_now()
    _note(session, mission, by, "Resumed.", why)
    await session.flush()


async def change(session: AsyncSession, mission: Mission, *, by: str, schedule: dict | None = None,
                 values: dict | None = None, why: str = "") -> None:
    said = []
    kind = kind_of(mission.key)
    if schedule is not None and kind is not None and kind.elsewhere:
        raise MissionError("this one's time is set on your computer, where the scheduled session runs.")
    if schedule is not None:
        new = check_schedule(schedule)
        if new != mission.schedule:
            said.append(f"Schedule: {schedule_words(mission.schedule)} → {schedule_words(new)}.")
            mission.schedule = new
            # A new schedule counts from now: changing it to "daily at 1:00" in the afternoon doesn't run it at once.
            mission.checked_at = utc_now()
    if values is not None:
        new = check_settings(mission.key, {**mission.settings, **values})
        if new != mission.settings:
            for k in sorted(new):
                if mission.settings.get(k) != new[k]:
                    said.append(f"{k.replace('_', ' ').capitalize()}: {mission.settings.get(k)!r} → {new[k]!r}.")
            mission.settings = new
    if said:
        _note(session, mission, by, " ".join(said), why)
    await session.flush()


# --- what the page reads ----------------------------------------------------------------------------------------


def run_view(entry: MissionRun, key: str) -> dict:
    """A run as the page reads it. `key` is its mission's: how far it got is the kind's to say (the read says it in its
    own words), and a run read without it would lose them."""
    started = entry.started or {}
    kind = kind_of(key)
    return {"id": str(entry.id), "started_by": entry.started_by, "started_at": iso_or_none(entry.started_at),
            "ended_at": iso_or_none(entry.ended_at), "outcome": entry.outcome, "summary": entry.summary,
            "progress": kind.progress(entry) if kind is not None else progress_of(entry),
            "stopped": STOPS.get(started["stopped"]) if isinstance(started.get("stopped"), str) else None,
            "search_run": started.get("search_run"), "instances": started.get("instances", [])}


async def view(session: AsyncSession, mission: Mission, profile_settings=None) -> dict:
    """A mission as the page reads it. `numbers` are the profile settings it governs (`missions/numbers.py`), with their
    values now; the list passes the profile's settings in so they are read once."""
    if profile_settings is None:
        profile_settings = (await profile_store.current(session, mission.owner)).settings
    last = await session.scalar(select(MissionRun).where(MissionRun.mission_id == mission.id)
                                .order_by(MissionRun.started_at.desc()).limit(1))
    if last is not None:
        last = await shown(session, last, mission.key)
    nxt = next_time(mission.schedule, await _last_started(session, mission)) if mission.state == "on" else None
    kind = kind_of(mission.key)
    return {
        "id": str(mission.id), "key": mission.key, "name": mission.name, "purpose": mission.purpose,
        "playbook": mission.playbook, "schedule": mission.schedule, "schedule_words": schedule_words(mission.schedule),
        "settings": mission.settings,
        # What the page needs to say who does it, whether peak hours hold it, whether the app can start it and when its
        # night is done. A row of no kind is held (nothing says it is the app's own work) and says none of the rest.
        "needs_claude": kind.needs_claude if kind is not None else True,
        "agent": kind.agent if kind is not None else None,
        "runs_elsewhere": kind.elsewhere if kind is not None else False,
        "done_when": kind.done_when(mission.settings) if kind is not None else None,
        "state": mission.state, "paused_until": iso_or_none(mission.paused_until),
        "checked_at": iso_or_none(mission.checked_at), "next_run_at": iso_or_none(max(nxt, utc_now()) if nxt else None),
        "last_run": run_view(last, mission.key) if last else None,
        "numbers": mission_numbers.fields(mission.key, profile_settings),
    }


async def listing(session: AsyncSession, owner: str) -> list[dict]:
    rows = (await session.scalars(select(Mission).where(Mission.owner == owner).order_by(Mission.created_at))).all()
    profile = await profile_store.current(session, owner)
    return [await view(session, m, profile.settings)
            for m in sorted(rows, key=lambda m: KINDS.index(m.key) if m.key in KINDS else 99)]


async def detail(session: AsyncSession, owner: str, mission_id: uuid.UUID) -> dict:
    mission = await get(session, owner, mission_id)
    runs = (await session.scalars(select(MissionRun).where(MissionRun.mission_id == mission.id)
                                  .order_by(MissionRun.started_at.desc()).limit(RUNS_SHOWN))).all()
    runs = [await shown(session, entry, mission.key) for entry in runs]
    changes = (await session.scalars(select(MissionChange).where(MissionChange.mission_id == mission.id)
                                     .order_by(MissionChange.at.desc()))).all()
    return {**await view(session, mission), "runs": [run_view(r, mission.key) for r in runs],
            "changes": [{"at": iso_or_none(c.at), "by": c.by, "what": c.what, "why": c.why} for c in changes]}
