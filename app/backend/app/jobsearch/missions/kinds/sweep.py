"""Sweep up stuck runs: close a search, a packet, a research or a check nobody finished in time as failed, so the next
one can start. The app's own work, with no Claude, and first in the schedule pass: a stuck search is closed before the
scout mission looks for an open one."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.harness import wording
from app.jobsearch.interviews import coach
from app.jobsearch.jobs import check as role_check
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.missions import search_run as run
from app.jobsearch.missions.core import EVERY_LEAST
from app.jobsearch.missions.kinds.base import Kind, Seed, Start
from app.jobsearch.missions.record import SWEEP
from app.jobsearch.models import Mission
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import availability
from app.jobsearch.work import hours as night
from app.jobsearch.writing import packets
from app.settings import settings
from app.sites import escalation


class Sweep(Kind):
    key = SWEEP
    name = "Sweep up stuck runs"
    activity = "sweeping"
    early = True

    def purpose(self) -> str:
        return wording.say("missions", "purpose_sweep_stuck", stuck_minutes=settings.stuck_minutes)

    def first(self, last_search) -> Seed:
        return Seed(playbook=None, schedule={"kind": "every", "hours": EVERY_LEAST}, settings={}, state="on")

    async def start(self, session: AsyncSession, owner: str, mission: Mission, by: str) -> Start:
        # A check or a parse nobody took or finished in time is failed here (reading one over HTTP saves nothing: it
        # only tells the page what it would be).
        await role_parse.expire(session, owner)
        await role_check.expire(session, owner)
        profile = await profile_store.current(session, owner)
        searches = written = researched = employers_stuck = prepared = 0
        handed: list[str] = []
        # Work held for peak hours isn't stuck: it waits, and its time counts from the end of the window. So is work
        # held for Claude's usage limit (as a catch-up met it): nothing is handed out while it holds, so a search or a
        # role that sat through it is counted from the moment it lifted.
        if not night.in_peak(profile.settings):
            floor = await availability.counts_from(session, owner)
            searches = await run.settle_stuck(session, owner, since=floor)
            written = await packets.settle_stuck(session, owner, since=floor)
            researched = await analysis.settle_stuck(session, owner, since=floor)
            prepared = await coach.settle_stuck(session, owner, since=floor)
            employers_stuck = await employer_research.settle_stuck(session, owner, since=floor)
            # A site project's plan the scout held and never finished goes back to it (the third failure asks the
            # owner); the scout is knocked for each project once this run is committed (`state["site_projects"]`).
            woken = await escalation.retry_planner_runs(session)
            handed = list(dict.fromkeys(str(project.id) for project, _reason, _mode in woken))
        if not searches and not written and not researched and not employers_stuck and not prepared and not handed:
            return Start("nothing", "Nothing was stuck.", {})
        parts = []
        if searches:
            parts.append(f"{searches} search{'es' if searches != 1 else ''}")
        if researched:
            parts.append(f"{researched} research{'es' if researched != 1 else ''}")
        if employers_stuck:
            parts.append(f"{employers_stuck} employer research{'es' if employers_stuck != 1 else ''}")
        if written:
            parts.append(f"{written} packet{'s' if written != 1 else ''}")
        if prepared:
            parts.append(f"{prepared} interview prep{'s' if prepared != 1 else ''}")
        summary = f"Closed {' and '.join(parts)} nobody finished, as failed." if parts else ""
        if handed:
            plural = "s" if len(handed) != 1 else ""
            summary = f"{summary} Handed {len(handed)} site plan{plural} nobody finished back to the scout.".strip()
        return Start("done", summary, {"site_projects": handed} if handed else {})


KIND = Sweep()
