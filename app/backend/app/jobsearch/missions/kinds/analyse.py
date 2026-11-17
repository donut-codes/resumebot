"""Analyse the finds: the analyst's night (docs/PRD.md 23.3, 39.17). The roles never researched, the ones the
owner moved by hand first and then the best ranked (`analysis.picks`, the deck's order), are put in front of the analyst
one after another, up to the mission's roles a night (-1: all). There is no research bar: a role before Ready has
passed the gates.

The run follows its roles and ends when every one has been researched (or has failed), or is stopped at the finish time
or by Claude's usage limit, which gives back the roles no session began. A catch-up after a tracker import is in this
mission's history too (`catch_up.py`), but it is not the night's own run: its own process says how far it is."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import limits, process, stages
from app.jobsearch.missions import catch_up, record
from app.jobsearch.missions.kinds.base import Batch, Seed, Start, by_words, finish, is_limit, limit
from app.jobsearch.missions.record import ANALYSE, SCOUT
from app.jobsearch.models import Mission, MissionRun, Opportunity
from app.jobsearch.research import analysis, line
from app.jobsearch.work.session_kinds import RESEARCHER


async def release(session: AsyncSession, entry: MissionRun, why: str) -> None:
    """Send the night's roles that no session began back to the research queue (picked again next night). A role the
    owner asked research of in the meantime (`analysis.ask`) is theirs, not the night's: it stays."""
    for rid in (entry.started or {}).get("roles", []):
        role = await session.get(Opportunity, uuid.UUID(rid))
        if role is not None and role.analysis_state == line.WAITING:
            await line.leave(session, role, line.SWEEP, why=why)  # the owner's ask stays in line


class AnalyseFinds(Batch):
    key = ANALYSE
    name = "Analyse the finds"
    activity = "analysing"
    needs_claude = True
    agent = "analyst"
    group = RESEARCHER
    defaults = {"target": limits.NO_LIMIT, "finish_by": "06:45"}

    def first(self, last_search: datetime | None) -> Seed:
        # Paused everywhere at first: each research is a session on the owner's Claude subscription, so the owner turns
        # it on when they want the queue worked (a change from docs/PRD.md 23.11).
        return Seed(playbook=process.ANALYSE, schedule={"kind": "after", "mission": SCOUT},
                    settings=dict(self.defaults), state="paused")

    def check(self, values: dict) -> dict:
        return {"target": limit(values, "target", 1, 1000, "roles to research"), "finish_by": finish(values)}

    def done_when(self, settings: dict) -> str | None:
        target = settings.get("target")
        return f"{f'{target} roles researched' if is_limit(target) else 'every role on deck researched'}" \
               f"{by_words(settings)}"

    async def start(self, session: AsyncSession, owner: str, mission: Mission, by: str) -> Start:
        """The roles never researched, the ones the owner moved by hand first and then the best ranked
        (`analysis.picks`, the deck's order), put in front of the analyst one after another."""
        target = int(mission.settings.get("target") or self.defaults["target"])
        roles = await analysis.picks(session, owner, None if limits.unlimited(target) else target)
        if not roles:
            return Start("done", "Nothing is waiting for research.", {})
        for role in roles:
            await analysis.start(session, owner, role)
        names = ", ".join(f"{r.title} at {r.company}" for r in roles)
        return Start("running", f"Sent {len(roles)} role{'s' if len(roles) != 1 else ''} to the analyst: {names}.", {
            "roles": [str(r.id) for r in roles],
            "instances": [str(r.analysis_instance_id) for r in roles if r.analysis_instance_id],
            "knock": [str(roles[0].id)], "reason": "analysis_asked", "target": target, "done": 0})

    async def settle(self, session: AsyncSession, entry: MissionRun) -> None:
        """A catch-up in this mission's history says how far it is by its own process; the night's own run is over when
        every role it sent has been researched (or has failed)."""
        if catch_up.is_catch_up(entry):
            await catch_up.refresh(session, entry)
            return
        started = dict(entry.started or {})
        if not started.get("roles"):
            return
        rows = (await session.scalars(select(Opportunity).where(
            Opportunity.id.in_([uuid.UUID(i) for i in started["roles"]])))).all()
        finished = [r for r in rows if r.analysis_state != "waiting"]
        entry.started = {**started, "done": len(finished)}
        if len(finished) < len(rows):
            return
        counts: dict[str, int] = {}
        for r in rows:
            word = ("failed" if r.analysis_state == "failed" else "waiting for the posting's text"
                    if r.analysis_state == analysis.NO_TEXT else stages.LABELS.get(r.status, r.status))
            counts[word] = counts.get(word, 0) + 1
        record.end(entry, "failed" if rows and all(r.analysis_state == "failed" for r in rows) else "done",
                   "Researched: " + ", ".join(f"{n} {word}" for word, n in sorted(counts.items())) + ".",
                   record.stop_for(started, len(rows)),
                   at=max((r.analysed_at for r in rows if r.analysed_at is not None), default=None))

    async def release(self, session: AsyncSession, entry: MissionRun, why: str) -> None:
        await release(session, entry, why)


KIND = AnalyseFinds()
