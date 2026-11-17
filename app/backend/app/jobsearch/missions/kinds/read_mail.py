"""Read the mail: the owner's mailbox read read-only for the mail a job search produces (docs/PRD.md path 14,
`mail/sync.py`). The reading and the moving are code, so peak hours never hold it; only its small model's step waits for
the window. Paused until a mailbox is connected, then daily; no mailbox gives a skipped run, not a failed one.

The read runs in the background as its playbook and writes its own outcome on the run when it ends, or when the mail
reader has answered (`settle`). A reader that had its time (`tend`) is given up on and the process goes on without it; a
run whose steps of code a restart cut short is taken up again."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import process
from app.jobsearch.actors import BY_APP
from app.jobsearch.mail import login as mail_login
from app.jobsearch.mail import sync as mail_sync
from app.jobsearch.missions import core
from app.jobsearch.missions.kinds.base import Kind, Seed, Start
from app.jobsearch.missions.record import READ_MAIL, Woken
from app.jobsearch.models import Mission, MissionRun
from app.jobsearch.work.session_kinds import RESEARCHER
from app.timeutil import owner_today


class ReadMail(Kind):
    key = READ_MAIL
    name = "Read the mail"
    activity = "mail-reading"
    agent = "analyst"
    group = RESEARCHER

    def first(self, last_search: datetime | None) -> Seed:
        # Paused until a mailbox is connected (`mailbox_connected`), then daily, early enough that an interview request
        # is read before the morning.
        return Seed(playbook=process.READ_MAIL, schedule={"kind": "daily", "at": "05:30"}, settings={}, state="paused")

    def done_when(self, settings: dict) -> str | None:
        return "your mailbox, read-only · the mail since the last read"

    async def start(self, session: AsyncSession, owner: str, mission: Mission, by: str) -> Start:
        """Nothing without a mailbox."""
        if await mail_login.find(session, owner) is None:
            return Start("skipped", mail_sync.NO_MAILBOX, {})
        today = owner_today()
        since = await mail_sync.window_start(session, owner, mission.id, today)
        return Start("running", "Reading the mail.",
                     {"mail": True, "since": since.isoformat(), "read_day": today.isoformat()},
                     launch=lambda run_id: mail_sync.start_in_background(run_id, owner, since))

    async def settle(self, session: AsyncSession, entry: MissionRun) -> None:
        """The read of the mail is over when its process is (`mail_sync.settle_run`)."""
        await mail_sync.settle_run(session, entry)

    async def tend(self, session: AsyncSession, owner: str, mission: Mission, entry: MissionRun, peak: bool,
                   now: datetime) -> Woken:
        """The mail reader had its time: go on without it (`mail_sync.give_up_waiting`); else the reader's step ended,
        or a restart cut the steps of code short (`mail_sync.tend`)."""
        if not await mail_sync.give_up_waiting(session, entry, now):
            await mail_sync.tend(session, entry)
        return []


async def mailbox_connected(session: AsyncSession, owner: str) -> None:
    """A mailbox was connected: Read the mail, paused until now, goes on (once: a mission the owner paused on purpose
    after a first run stays paused). It counts from now, so it does not run at once."""
    mission = await session.scalar(select(Mission).where(Mission.owner == owner, Mission.key == READ_MAIL))
    if mission is None or mission.state == "on":
        return
    ever = await session.scalar(select(MissionRun.id).where(MissionRun.mission_id == mission.id).limit(1))
    if ever is None and mission.paused_until is None:
        await core.resume(session, mission, by=BY_APP, why="a mailbox was connected")


async def mailbox_removed(session: AsyncSession, owner: str) -> None:
    """The mailbox was disconnected: with nothing to read, Read the mail is paused."""
    mission = await session.scalar(select(Mission).where(Mission.owner == owner, Mission.key == READ_MAIL))
    if mission is not None and mission.state == "on":
        await core.pause(session, mission, by=BY_APP, why="the mailbox was disconnected")


KIND = ReadMail()
