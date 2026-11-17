"""Read the employers' boards: the night's read of every known employer's own job board into the posting index, the new
postings filtered in code and the descriptions read for the candidates (docs/PRD.md 28.4 to 28.10). The app's own work,
with no Claude. The read itself is `boards_night.py`, a background task that writes its outcome on the run when it ends;
this kind starts it once the run is saved, brings a run that never ended up to date, and takes up or closes a read the
app's restart cut short."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.missions import boards_night, record
from app.jobsearch.missions.kinds.base import Kind, Seed, Start, is_limit, limit
from app.jobsearch.missions.record import READ
from app.jobsearch.models import Mission, MissionRun
from app.jobsearch.postings import read_progress
from app.settings import settings
from app.timeutil import aware, utc_now


class ReadBoards(Kind):
    key = READ
    name = "Read the employers' boards"
    activity = "reading"
    defaults = {"details_per_night": 150}

    def first(self, last_search: datetime | None) -> Seed:
        return Seed(playbook=None, schedule={"kind": "daily", "at": "01:00"}, settings=dict(self.defaults),
                    state="on")

    def check(self, values: dict) -> dict:
        return {"details_per_night": limit(values, "details_per_night", 0, 2000, "descriptions to read a night")}

    def done_when(self, settings: dict) -> str | None:
        per_night = settings.get("details_per_night")
        return ("every known employer's board · "
                + (f"up to {per_night} descriptions read" if is_limit(per_night) else "every description read"))

    async def start(self, session: AsyncSession, owner: str, mission: Mission, by: str) -> Start:
        """The read runs in the background (`boards_night.py`) and writes its own outcome on this run when it ends. The
        caller launches it once the run is saved: a task that began before the commit could look for a run that isn't
        there."""
        per_night = int(mission.settings.get("details_per_night") or 0)
        return Start("running", "Reading the employers' boards.", {"reading": True, "details_per_night": per_night},
                     launch=lambda run_id: boards_night.start_in_background(run_id, owner, per_night))

    async def settle(self, session: AsyncSession, entry: MissionRun) -> None:
        """A read still marked running past its time limit and the grace it is given is lost (the app may have
        restarted): it is closed as failed."""
        if not (entry.started or {}).get("reading"):
            return
        began = aware(entry.started_at)
        if began is not None and utc_now() - began > timedelta(minutes=boards_night.LOST_AFTER_MINUTES):
            record.end(entry, "failed", "The read never finished (the app may have restarted).")

    async def recover(self, session: AsyncSession, owner: str, now: datetime) -> int:
        """At the start: a board read still marked running was cut short by the restart (it lives in the app's own
        process, `boards_night.py`). It carries on with the sites and job texts it had not reached (its process is open
        at the step it was at, and the run holds what it finished: `read_progress.py`), launched once this transaction
        commits, until it is older than `boards_night.RESUME_HOURS` (the next night's read is due by then): that one is
        closed as failed, and so is the process it left open. With no run to carry on, an open process is closed as
        before. A held start (the schedule off, `stack.py start --hold`: nothing runs by itself) carries nothing on and
        leaves a run it could carry on, and its open process, as they are for the next start that runs the schedule:
        the read fetches the employers' pages. Returns how many runs were cut short."""
        rows = (await session.scalars(
            select(MissionRun).join(Mission, Mission.id == MissionRun.mission_id)
            .where(MissionRun.owner == owner, Mission.key == READ, MissionRun.outcome == "running"))).all()
        cut = [r for r in rows if (r.started or {}).get("reading")]
        resumed = 0
        for entry in cut:
            began = aware(entry.started_at)
            if began is not None and now - began <= timedelta(hours=boards_night.RESUME_HOURS) and not resumed:
                resumed += 1
                if settings.run_schedule:
                    entry.started = {**(entry.started or {}),
                                     "resumed": int((entry.started or {}).get("resumed") or 0) + 1}
                    record.after_commit(session, _resume(entry, owner))
            else:
                record.end(entry, "failed", "The read was cut short: the app restarted while it was running.")
        if not resumed:
            await boards_night.end_unfinished(session, owner, "the app restarted while the read was running")
        await session.flush()
        return len(cut)

    def progress(self, entry: MissionRun) -> dict[str, Any] | None:
        """While it reads: its own words and its sites against the sites there are (`read_progress.label`)."""
        started = entry.started or {}
        if entry.outcome == "running" and started.get("reading"):
            return read_progress.label(started)
        return super().progress(entry)


def _resume(entry: MissionRun, owner: str):
    run_id, per_night = entry.id, int((entry.started or {}).get("details_per_night") or 0)
    return lambda: boards_night.start_in_background(run_id, owner, per_night, True)


KIND = ReadBoards()
