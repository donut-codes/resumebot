"""Research employers: the employer researcher's night (docs/PRD.md 36.1). After the boards are read, a batch of
employers, each put in front of a small session that reads public pages (`research/employer.py`): those with a role in
play first, then the most open roles, then the never researched, none researched in the last so many days, up to the
mission's employers a night (-1: all). The run follows the batch, ends when every employer is done, and sends back what
nobody began when the finish time comes or Claude's usage runs out."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import limits, process
from app.jobsearch.missions import record
from app.jobsearch.missions.kinds.base import Batch, Seed, Start, by_words, days, finish, is_limit, limit
from app.jobsearch.missions.record import READ, RESEARCH
from app.jobsearch.models import Employer, Mission, MissionRun
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import engine_calls
from app.jobsearch.work.session_kinds import RESEARCHER


class ResearchEmployers(Batch):
    key = RESEARCH
    name = "Research employers"
    activity = "employer-researching"
    needs_claude = True
    agent = "analyst"
    group = RESEARCHER
    defaults = {"target": 25, "again_after_days": 90, "finish_by": "06:45"}

    def first(self, last_search: datetime | None) -> Seed:
        # Paused at first, like the other missions that are Claude's work: the owner turns it on.
        return Seed(playbook=process.RESEARCH_EMPLOYER, schedule={"kind": "after", "mission": READ},
                    settings=dict(self.defaults), state="paused")

    def check(self, values: dict) -> dict:
        return {"target": limit(values, "target", 1, 1000, "employers to research"),
                "again_after_days": days(values, "again_after_days", 1, 3650, "research again after"),
                "finish_by": finish(values)}

    def done_when(self, settings: dict) -> str | None:
        # The employers a night is a limit (-1: no limit); the days are how long before one is looked at again.
        target, again = settings.get("target"), settings.get("again_after_days")
        words = f"{target} employers researched" if is_limit(target) else "every employer not researched lately"
        return f"{words} · none again within {again} {'day' if again == 1 else 'days'}{by_words(settings)}"

    async def start(self, session: AsyncSession, owner: str, mission: Mission, by: str) -> Start:
        """The night's batch of employers (`employer_research.picks`, docs/PRD.md 36.1), each put in front of the
        researcher."""
        target = int(mission.settings.get("target") or self.defaults["target"])
        again = int(mission.settings.get("again_after_days") or self.defaults["again_after_days"])
        rows = await employer_research.picks(session, owner, None if limits.unlimited(target) else target, again)
        if not rows:
            return Start("done", "No employer is waiting for research.", {})
        for employer in rows:
            await employer_research.start(session, owner, employer)
        shown = ", ".join(e.name for e in rows[:10]) + (f" and {len(rows) - 10} more" if len(rows) > 10 else "")
        sent = f"Sent {len(rows)} employer{'s' if len(rows) != 1 else ''} to the researcher: {shown}."
        return Start("running", sent, {
            "employers": [str(e.id) for e in rows],
            "instances": [str(e.research_instance_id) for e in rows if e.research_instance_id],
            "knock": [str(rows[0].id)], "reason": engine_calls.EMPLOYERS_ASKED, "target": target, "done": 0})

    async def settle(self, session: AsyncSession, entry: MissionRun) -> None:
        """Over when every employer sent has been researched (or failed, or was merged into another record)."""
        started = dict(entry.started or {})
        if not started.get("employers"):
            return
        ids = [uuid.UUID(i) for i in started["employers"]]
        rows = (await session.scalars(select(Employer).where(Employer.id.in_(ids)))).all()
        # A record merged into another while it waited is gone: its research was done by the one that is left.
        finished = [r for r in rows if r.research_state != employer_research.WAITING]
        entry.started = {**started, "done": len(finished) + len(ids) - len(rows)}
        if len(finished) + len(ids) - len(rows) < len(ids):
            return
        record.end(entry,
                   "failed" if rows and all(r.research_state == employer_research.FAILED for r in rows) else "done",
                   employer_research.run_words(rows, len(ids)), record.stop_for(started, len(ids)),
                   at=max((r.researched_at for r in rows if r.researched_at is not None), default=None))

    async def release(self, session: AsyncSession, entry: MissionRun, why: str) -> None:
        await employer_research.release(session, entry.owner, (entry.started or {}).get("employers", []), why)


KIND = ResearchEmployers()
