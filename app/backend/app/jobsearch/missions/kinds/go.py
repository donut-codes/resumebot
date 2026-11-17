"""Open my apply tabs: the owner's morning batch, the Go button's own path run by the clock (docs/PRD.md path 18). It is
the owner's own work started for them, so neither the off-hours window nor peak hours hold it, and a morning the app
was down for is skipped, not run late."""

from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.missions.kinds.base import Kind, Seed, Start
from app.jobsearch.missions.record import GO
from app.jobsearch.models import Mission
from app.jobsearch.work.session_kinds import WRITER
from app.timeutil import utc_now

#: How long after its time the morning batch may still start (the app was down then); later, the day is skipped.
GRACE = timedelta(minutes=75)


class OpenApplyTabs(Kind):
    key = GO
    name = "Open my apply tabs"
    activity = "tab-opening"
    group = WRITER

    def first(self, last_search) -> Seed:
        # It counts from now: made in the afternoon, it first runs the next morning.
        return Seed(playbook=None, schedule={"kind": "daily", "at": "06:30"}, settings={}, state="on",
                    checked_at=utc_now())

    async def hold(self, session: AsyncSession, owner: str, mission: Mission, now: datetime,
                   due_at: datetime) -> Literal["wait", "skip"] | None:
        """The morning is over once its time passed more than `GRACE` ago: wait for the next one."""
        return "skip" if now - due_at > GRACE else None

    async def start(self, session: AsyncSession, owner: str, mission: Mission, by: str) -> Start:
        """The Go button's own path (`apply_go.start`): it checks the day's roles, builds their packets and opens each
        tab as its packet is done, in the background. A run with nothing in Ready to Launch is "nothing" (a scheduled
        one leaves no run)."""
        from app.jobsearch.browser import go as apply_go  # it reads the browser and the checks, which read this module
        from app.jobsearch.pipeline import board  # it reads the browser and the checks, which read this module

        if apply_go.running(owner):
            return Start("nothing", "Launch in Browser is running already.", {})
        if not (await board.ready_line(session, owner))[0]:
            return Start("nothing", "Nothing was ready to open.", {})
        await apply_go.start(owner)
        return Start("done", "Started Launch in Browser: it builds the packets and opens each tab as its packet is "
                             "done.", {})


KIND = OpenApplyTabs()
