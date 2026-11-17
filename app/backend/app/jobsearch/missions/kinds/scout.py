"""Scout the boards: the night's search for new roles (docs/PRD.md 25.12). It starts a search (`search_run.start_round`,
which a session of the scout runs), starts learning the sites the owner switched on that have no way to be read yet
(docs/PRD.md 29.1), and goes on with another search whenever one ends below the night's number of matches, until its
target of matches or its most searches, the finish time, peak hours or Claude's usage limit stop it, or there is nothing
left to search: no employer whose job board the app doesn't know that the night's searches have not yet handed to the
scout (`_nothing_more`).

The run follows its searches (`search_run`, `search_runs`, `between`, `kept`) and counts the matches: the roles these
searches stored that have passed the gates and reached Needs Drafted. A search's finds are with the analyst before the
next search starts (`analysis.round_waiting`). Outside the night, the pool starts a round the way this mission does
(`start_search`) and starts site learning only through `start_sites`."""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import limits, process, stages
from app.jobsearch.doors import tools as door_tools
from app.jobsearch.missions import boards_night, record
from app.jobsearch.missions import search_run as run
from app.jobsearch.missions.kinds.base import Kind, Seed, Start, by_words, finish, is_limit, limit
from app.jobsearch.missions.record import SCOUT, STOPS, Woken
from app.jobsearch.models import Mission, MissionRun, Opportunity, SearchRun
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work import availability
from app.jobsearch.work import hours as night
from app.jobsearch.work.session_kinds import SEARCH
from app.settings import settings
from app.sites import autostart, read_probe
from app.timeutil import iso_or_none

#: Where a role is once it has passed the gates and reached Ready to Launch (or gone on from it): what the scout's
#: "number of matches" counts. A role still before Ready, or gated out, is not one.
MATCHED = (stages.READY_TO_LAUNCH, stages.APPLIED, stages.ACTIVE_DISCUSSION)
#: What `_nothing_more` says when the night should wait and look again.
WAIT = "wait"


# --- what the scout starts --------------------------------------------------------------------------------------


async def _search(session: AsyncSession, owner: str, focus: str | None, by: str) -> SearchRun:
    # No search is started into Claude's usage limit as a catch-up holds it: nothing hands out work before it lifts
    # (`session_kinds.next_kind`), so the search would sit with no session until the stuck sweep closed it as failed.
    # The schedule waits for the limit to lift (`Scout.hold`); a start that gets here is refused in plain words.
    if (lifts := await availability.limit_until(session, owner)) is not None:
        raise run.RunError(f"{availability.USAGE_LIMIT} holds until {night.when_words(lifts)}, and a search can't run "
                           "before then")
    return await run.start_round(session, owner, started_by="schedule" if by in ("schedule", "pool") else "manual",
                                 focus=focus or None)


async def start_search(session: AsyncSession, owner: str, *, by: str) -> SearchRun:
    """Start a search round the way the Scout mission does: its focus, Claude's usage limit, one at a time. With no
    mission row the round is the owner's plain one. Raises `run.RunError` when it cannot start."""
    mission = await session.scalar(select(Mission).where(Mission.owner == owner, Mission.key == SCOUT))
    if mission is None:
        return await run.start_round(session, owner, started_by="manual")
    return await _search(session, owner, mission.settings.get("focus"), by)


async def start_sites(session: AsyncSession, owner: str) -> list[str]:
    """The night's one chance for a site to get a project (docs/PRD.md 29.1): each site the owner switched on that never
    had one, or whose last project ended with no skill and was switched on again since, up to the owner's sites to
    learn a night (`Settings.site_projects_per_night`; the sites switched on longest ago first, the rest wait for the
    next night). A switch starts nothing; this does, inside the scout mission only. A site whose employer is on a job
    system the server has no reader for goes to the owner's browser first (`read_probe.hand_no_reader`, as the night's
    read does): its project could not succeed and would take a slot from one that can. Returns the new projects' ids;
    the caller wakes the scout for them after its commit."""
    await read_probe.hand_no_reader(session, owner)
    per_night = (await profile_store.current(session, owner)).settings.site_projects_per_night
    made = await autostart.ensure_projects(
        session, owner, limit=None if limits.unlimited(per_night) else per_night, by_switch=True)
    return [str(p.id) for p in made]


async def matches(session: AsyncSession, search_run_ids: list[str]) -> int:
    """The scout's "number of matches" so far: the roles these searches stored that have passed the gates and reached
    Needs Drafted (the analyst's settle step moves a researched role there, or the owner asks for its packet). A role
    still before Ready, or gated out, does not count."""
    if not search_run_ids:
        return 0
    return int(await session.scalar(select(func.count(Opportunity.id)).where(
        Opportunity.source_run.in_([uuid.UUID(i) for i in search_run_ids]), Opportunity.status.in_(MATCHED))) or 0)


# --- the words of a night ---------------------------------------------------------------------------------------


def _sites_words(count: int) -> str:
    return f" Started learning {count} site{'s' if count != 1 else ''}." if count else ""


def _work_ended(started: dict) -> datetime | None:
    """When the last search of a scout run ended (`settle` notes it), or None when it was not noted."""
    when = started.get("work_ended_at")
    try:
        return datetime.fromisoformat(when) if isinstance(when, str) else None
    except ValueError:
        return None


def _matches_words(started: dict) -> str:
    """"12 of 20 matches" while a number of matches is wanted and short of it, else "12 matches"."""
    done, target = int(started.get("done") or 0), int(started.get("target") or 0)
    if 0 < target and done < target:
        return f"{done} of {target} matches"
    return f"{done} {'match' if done == 1 else 'matches'}"


def _reached(started: dict) -> str:
    """What a scout night produced, for its summary: "Reached 12 matches in 3 searches (31 new roles kept)"."""
    searches, kept = len(started.get("search_runs", [])), int(started.get("kept") or 0)
    return (f"Reached {_matches_words(started)} in {searches} {'search' if searches == 1 else 'searches'} "
            f"({kept} new {'role' if kept == 1 else 'roles'} kept)")


async def _stalled(session: AsyncSession, owner: str, started: dict, waiting: list[str]) -> bool:
    """Whether the night's last search had work and did none of it (docs/PRD.md 25.12): an employer that waited when it
    began (`started["waiting"]`) still waits (`waiting`, the employers left now), and it was handed none. An employer
    that got a job system some other way since the search began was not one it could have been handed, and one that
    came after it asked was not one it was meant for. A night that began before the search noted who waited holds
    every employer left against it."""
    began = started.get("waiting")
    left = waiting if began is None else set(waiting) & set(began)
    return bool(left) and not await door_tools.boards_handed_out(session, owner, [started.get("search_run") or ""])


async def _nothing_more(session: AsyncSession, owner: str, entry: MissionRun, waiting: list[str]) -> str | None:
    """Whether another search could do anything, between two: None while an employer is left whose job board the app
    doesn't know and the night's searches have not handed to the scout (`waiting`; each is handed over once a night,
    answered or not: `door_tools.boards_reply`); else `WAIT` while the read of the boards is still adding postings,
    whose employers may be new; else `searched`. `stalled` when the last search was handed none of the employers that
    waited when it began (`_stalled`: its session did nothing). A first search is held to it like any other."""
    if not waiting:
        return WAIT if await boards_night.being_read(session, owner) else "searched"
    return "stalled" if await _stalled(session, owner, entry.started or {}, waiting) else None


class ScoutBoards(Kind):
    key = SCOUT
    name = "Scout the boards"
    activity = "scouting"
    needs_claude = True
    agent = "scout"
    group = SEARCH
    defaults = {"focus": "", "keep_target": 20, "max_sessions": 4, "finish_by": "06:45"}

    def first(self, last_search: datetime | None) -> Seed:
        hours = settings.interval_hours
        # The search's clock carries on from the last search, so upgrading doesn't start one at once.
        return Seed(playbook=process.ROUND,
                    schedule={"kind": "daily", "at": "01:00"} if hours > 0 else {"kind": "on_demand"},
                    settings=dict(self.defaults), state="on" if last_search is not None and hours > 0 else "paused",
                    checked_at=last_search)

    def check(self, values: dict) -> dict:
        focus = " ".join(str(values.get("focus") or "").split())
        return {"focus": focus, "keep_target": limit(values, "keep_target", 1, 200, "number of matches"),
                "max_sessions": limit(values, "max_sessions", 1, 12, "most searches a night"),
                "finish_by": finish(values)}

    def done_when(self, settings: dict) -> str | None:
        target, most = settings.get("keep_target"), settings.get("max_sessions")
        set_ = [f"{target} matches" if is_limit(target) else None,
                f"at most {most} {'search' if most == 1 else 'searches'}" if is_limit(most) else None]
        said = " · ".join(w for w in set_ if w)
        return f"{f'{said}, or ' if said else ''}every site searched{by_words(settings)}"

    async def start(self, session: AsyncSession, owner: str, mission: Mission, by: str) -> Start:
        sites = await start_sites(session, owner)
        try:
            search = await _search(session, owner, mission.settings.get("focus"), by)
        except run.RunError as exc:
            return Start("skipped", f"No search: {exc}.{_sites_words(len(sites))}", {"site_projects": sites})
        target = int(mission.settings.get("keep_target") or self.defaults["keep_target"])
        goal = "" if limits.unlimited(target) else f" (to reach {target} matches)"
        return Start("running", f"Started a search{goal}.{_sites_words(len(sites))}", {
            "search_run": str(search.id), "search_runs": [str(search.id)], "instances": [str(search.instance_id)],
            "knock": [str(search.id)], "target": target, "done": 0, "kept": 0, "site_projects": sites,
            "waiting": await door_tools.boards_waiting(session, owner, None)})

    async def hold(self, session: AsyncSession, owner: str, mission: Mission, now: datetime,
                   due_at: datetime) -> Literal["wait", "skip"] | None:
        """Whether a scout night that is due waits for Claude's usage limit, as a catch-up holds it: the limit lifts
        before the night's finish time, so the night starts when it lifts (it stays due; a search started into the limit
        would only sit with no session until the stuck sweep closed it as failed, ending the night). A limit that
        outlasts the finish time is not waited for: the night is skipped and says why (`_search`), as one that could
        not start always is.

        A night found due after its time has passed (the computer slept through its start) is skipped, as Go's
        morning is: when the finish time has gone by, or it is outside the owner's off hours, nobody would work the
        round it started, and the stuck sweep would close it as failed and hold Run now for two hours."""
        end = night.finish_moment(due_at, mission.settings.get("finish_by"))
        profile = await profile_store.current(session, owner)
        if end is not None and now >= end:
            return "skip"
        if mission.schedule.get("kind") == "daily" and not night.in_off_hours(profile.settings, now):
            # A night on a daily slot found outside the owner's window (an "every N hours" schedule has no slot of its
            # own: the pool's window holds its work). Before the window opens it waits for it (a window later than the
            # slot); one that will not open before the finish time gives the night up.
            opens = night.next_off_hours_start(profile.settings, now)
            return "wait" if end is None or opens < end else "skip"
        lifts = await availability.limit_until(session, owner, now=now)
        if lifts is None:
            return None
        return "wait" if end is None or lifts < end else None

    async def settle(self, session: AsyncSession, entry: MissionRun) -> None:
        """The searches' ends: a search that ends below the target leaves the run waiting for the next, which `tend`
        starts; the target, the last one, or a failed search ends the night: failed when nothing came of the night (no
        role kept, no employer's board answered for), else done once the failed search leaves nothing for the next to
        do (no employer left to hand to the scout, or one that did nothing)."""
        started = dict(entry.started or {})
        if not started.get("search_run") or started.get("between"):
            return
        search = await session.get(SearchRun, uuid.UUID(started["search_run"]))
        if search is None or search.status == "open":
            return
        # The search is over, but its finds are with the analyst (analysis.py: Analyse the finds researches each
        # search's finds before the next search starts): the round is over when they are, and the matches are counted
        # then, since a researched role is what reaches Needs Drafted.
        if await analysis.round_waiting(session, search.id):
            return
        # `kept` is every role the searches stored (a search that failed and stored none has failed); `done` is the
        # matches, the ones among them that reached Needs Drafted, which is what the target counts.
        kept = int(started.get("kept", 0)) + int((search.counts or {}).get("kept") or 0)
        found = await matches(session, started.get("search_runs", []))
        started.update({"kept": kept, "done": found, "between": True, "work_ended_at": iso_or_none(search.ended_at)})
        entry.started = started
        target = int(started.get("target") or 0)
        if search.status != "done":
            # A search that failed ends the night (a failure stop): with no limit on the searches, another after each
            # failure could go on all night. What the earlier searches kept is kept. A night is failed when nothing
            # came of it: no role kept (or matched) and no employer's board answered for by the scout (being handed an
            # employer is the app's doing, not the scout's work). One that did its work goes on past a failed search
            # while employers are left that the night's searches have not handed to the scout, unless the target is
            # met or this search was stalled (`_stalled`): the scouts report no roles, so a night of lookups keeps
            # none, and one failed search of many would end it with the rest waiting for the next night.
            if not (kept > 0 or found > 0
                    or await door_tools.boards_answered_since(session, entry.owner, entry.started_at)):
                record.end(entry, "failed", search.summary or "The search failed.", at=search.ended_at)
                return
            waiting = await door_tools.boards_waiting(session, entry.owner, entry.id)
            if waiting and not (0 < target <= found) and not await _stalled(session, entry.owner, started, waiting):
                return
            why = (search.summary or "it failed").rstrip(". ")
            record.end(entry, "done", f"{_reached(started)}; the last search failed: {why}.", at=search.ended_at)
        elif target > 0 and found >= target:
            record.end(entry, "done", f"{_reached(started)}.", "target", at=search.ended_at)

    async def tend(self, session: AsyncSession, owner: str, mission: Mission, entry: MissionRun, peak: bool,
                   now: datetime) -> Woken:
        """Between searches: another, unless a limit, peak hours or having nothing left to search stop it."""
        started = dict(entry.started or {})
        if not started.get("between"):
            return []
        most = int(mission.settings.get("max_sessions") or 1)
        if not limits.unlimited(most) and len(started.get("search_runs", [])) >= most:
            record.end(entry, "done", f"{_reached(started)}.", "cap", at=_work_ended(started))
            return []
        if peak:
            record.end(entry, "done", f"{_reached(started)}.", "peak", at=_work_ended(started))
            return []
        waiting = await door_tools.boards_waiting(session, owner, entry.id)
        over = await _nothing_more(session, owner, entry, waiting)
        if over == WAIT:
            return []
        if over:
            record.end(entry, "done", f"{_reached(started)}.", over, at=_work_ended(started))
            return []
        if await availability.limit_until(session, owner, now=now) is not None:
            return []  # the next search waits for Claude's usage limit; the finish time still ends the night
        try:
            search = await _search(session, owner, mission.settings.get("focus"), entry.started_by)
        except run.RunError as exc:
            record.end(entry, "done", f"{_reached(started)}; no more searches: {exc}.", at=_work_ended(started))
            return []
        entry.started = {**started, "between": False, "search_run": str(search.id),
                         "search_runs": [*started["search_runs"], str(search.id)],
                         "instances": [*started.get("instances", []), str(search.instance_id)], "waiting": waiting}
        await session.flush()
        return [(str(search.id), "search_started")]

    async def stop(self, session: AsyncSession, entry: MissionRun, stop: Literal["finish", "usage"],
                   note: str = "") -> bool:
        """End a scout night now, with what it has: the finish time came, or Claude's subscription is out of usage
        (docs/PRD.md 25.12). A search still open is wound down (what the scout reported is stored), the finds not yet
        with the analyst's session go back to the queue, and the run is closed done with what the night produced."""
        started = entry.started or {}
        for sid in started.get("search_runs", []):
            search = await session.get(SearchRun, uuid.UUID(sid))
            if search is not None and search.status == "open":
                await run.wind_down(session, search, stop, STOPS[stop])
        for sid in started.get("search_runs", []):
            await analysis.release_round(session, uuid.UUID(sid), f"stopped: {STOPS[stop]}")
        await self.settle(session, entry)  # counts the search that was wound down (and may close the run as failed)
        await session.flush()
        if entry.outcome == "running":
            record.end(entry, "done", f"{_reached(entry.started or {})}.{note}", stop)
        return True

    async def strays(self, session: AsyncSession, owner: str, stop: Literal["finish", "usage"], note: str = "") -> None:
        """A search that no mission started (Search now) is wound down the same way: it keeps what it found."""
        search = await run.open_run(session, owner)
        if search is not None:
            await run.wind_down(session, search, stop, STOPS[stop])


KIND = ScoutBoards()
