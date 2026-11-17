"""The day's look reads a slice's standing and posted days once for the slice, under the writer's turn, instead of once
for each role. The decisions
are the per-role path's, including for a role whose posting changed between the reader pass and the lock. Made up."""

from datetime import date, timedelta

from sqlalchemy import event, select, update

from app import db
from app.jobsearch import as_read, stages
from app.jobsearch.gating import age_out, regate
from app.jobsearch.models import Opportunity
from tests.support import OWNER, load_profile

TODAY = date(2026, 11, 3)
GATES = {"location": {}, "other": [
    {"id": "dial-freshness-must", "kind": "job_fact", "fact": "posted_days", "op": "<=", "value": 15}]}
PER_ROLE = "per-role"
COUNT = 20


def role(owner: str, n: int, days: int) -> Opportunity:
    when = TODAY - timedelta(days=days)
    return Opportunity(
        owner=owner, slug=f"r{n}", company=f"Fabrikam {n}", title=f"Lead {n}", fit=3, origin="scout",
        status=stages.FIRST_IMPRESSION, match_key=f"fabrikam {n}|lead {n}", found=when, posted=when,
        location="Remote", move_type="remote")


async def test_a_slice_reads_once_what_the_per_role_path_reads_for_each_role_and_decides_the_same(js, monkeypatch):
    real_one_role = regate.one_role

    async def one_role(session, owner, row, **kwargs):
        if owner == PER_ROLE:  # the old path: nothing handed over, so each role reads its own
            kwargs.pop("posted", None)
            kwargs.pop("standing", None)
        return await real_one_role(session, owner, row, **kwargs)

    real_lock = as_read.lock

    async def lock(session, model, ids, **kwargs):
        """Between the reader pass and the lock, a posting is re-dated: r0, which failed the pass, turns young."""
        owner = kwargs["where"][0].right.value
        async with db.session_factory()() as other:
            for slug, days in (("r0", 1),):
                when = TODAY - timedelta(days=days)
                await other.execute(update(Opportunity).where(Opportunity.owner == owner, Opportunity.slug == slug)
                                    .values(posted=when, found=when))
            await other.commit()
        return await real_lock(session, model, ids, **kwargs)

    monkeypatch.setattr(regate, "one_role", one_role)
    async with db.session_factory()() as session:
        for owner in (OWNER, PER_ROLE):
            await load_profile(session, owner, gates=GATES)
            session.add_all(role(owner, n, 40) for n in range(COUNT))
        await session.commit()
    monkeypatch.setattr(age_out.as_read, "lock", lock)  # after the set-up: a profile save locks too, with no `where`

    counts: dict[str, int] = {}
    aged = {}
    for owner in (OWNER, PER_ROLE):
        statements: list[str] = []
        noted = lambda _conn, _cursor, statement, *_rest, into=statements: into.append(statement)  # noqa: E731
        engine = db.engine().sync_engine
        event.listen(engine, "before_cursor_execute", noted)
        try:
            async with db.session_factory()() as session:
                aged[owner] = await age_out.run(session, owner, today=TODAY)
                await session.commit()
        finally:
            event.remove(engine, "before_cursor_execute", noted)
        counts[owner] = len(statements)

    assert aged[OWNER] == aged[PER_ROLE]
    assert aged[OWNER].closed == COUNT - 1, "r0 was young at the lock: every other role stops"
    assert counts[PER_ROLE] - counts[OWNER] >= 3 * COUNT, counts  # the reads; the writes are the same
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(Opportunity))).all()
    decided = {o: {r.slug: (r.status, r.screened_out_reason) for r in rows if r.owner == o} for o in (OWNER, PER_ROLE)}
    assert decided[OWNER] == decided[PER_ROLE]
    assert decided[OWNER]["r0"][0] == stages.FIRST_IMPRESSION, "judged on the day it has under the turn"
    assert decided[OWNER]["r2"][0] == stages.GATED_OUT
