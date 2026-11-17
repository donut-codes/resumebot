"""Read in your browser: the job sites a server cannot read (a sign-in, a bot block), read in the user's everyday Chrome
by a Claude session the user starts (docs/PRD.md 30.8, docs/browser-reader.md). The app cannot start it:
the session starts its own run when it calls in (`browser/leases.py`, `session_run`), so this mission's schedule is
"outside" the app's, and Run now is refused in words that say so. A run is open from the session's first item to its
end, and is closed here when its session went silent."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.browser import leases
from app.jobsearch.missions.kinds.base import Kind, Seed, Start
from app.jobsearch.missions.record import BROWSER, RUNS_ELSEWHERE, MissionError
from app.jobsearch.models import Mission, MissionRun
from app.jobsearch.work.session_kinds import BROWSER_READER


class ReadBrowser(Kind):
    key = BROWSER
    name = "Read in your browser"
    activity = "browser-reading"
    needs_claude = True
    group = BROWSER_READER
    elsewhere = True
    # No posting cap and no run limit (the owner, 2026-10-16): the browser's work is a queue of small items.
    defaults = {}

    def first(self, last_search: datetime | None) -> Seed:
        return Seed(playbook=None, schedule={"kind": "outside"}, settings={}, state="paused")

    def done_when(self, settings: dict) -> str | None:
        return "every posting handed in · every new one opened for its description"

    async def start(self, session: AsyncSession, owner: str, mission: Mission, by: str) -> Start:
        """Nothing the app can start: the session the user starts begins its own run when it calls
        in."""
        raise MissionError(RUNS_ELSEWHERE)

    async def settle(self, session: AsyncSession, entry: MissionRun) -> None:
        """A run no call has touched for three leases is closed with what it had (`leases.settle_run`)."""
        await leases.settle_run(session, entry)

    async def reads_as(self, session: AsyncSession, entry: MissionRun) -> None:
        """The same close on the read's copy; what the session held stays held (`leases.run_as_read`)."""
        await leases.run_as_read(session, entry)


KIND = ReadBrowser()
