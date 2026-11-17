"""The day's look reads a slice's newest packets, stage moves and gate copies once for the slice, not once for each role
(a slice of roles was a statement for each of several tables). Made up."""

from datetime import date, timedelta

from sqlalchemy import event, select

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import age_out
from app.jobsearch.models import Opportunity
from tests.support import OWNER, load_profile

TODAY = date(2026, 11, 3)
GATES = {"location": {}, "other": [
    {"id": "dial-freshness-must", "kind": "job_fact", "fact": "posted_days", "op": "<=", "value": 15}]}
COUNT = 24  # more than one slice of the new size (8), fewer than two of the old (20): three slices, not two


def role(n: int) -> Opportunity:
    when = TODAY - timedelta(days=40)
    return Opportunity(
        owner=OWNER, slug=f"r{n}", company=f"Fabrikam {n}", title=f"Lead {n}", fit=3, origin="scout",
        status=stages.FIRST_IMPRESSION, match_key=f"fabrikam {n}|lead {n}", found=when, posted=when,
        location="Remote", move_type="remote")


async def test_a_slice_reads_packets_and_stage_moves_once_and_the_roles_are_stopped_as_before(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add_all(role(n) for n in range(COUNT))
        await session.commit()

    statements: list[str] = []
    noted = lambda _conn, _cursor, statement, *_rest: statements.append(statement)  # noqa: E731
    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        async with db.session_factory()() as session:
            aged = await age_out.run(session, OWNER, today=TODAY)
            await session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", noted)

    assert aged == age_out.Aged(checked=COUNT, closed=COUNT, left=0)
    assert sum("FROM js_packets" in s for s in statements) == 3, "one read of packets for each slice"
    # the look takes up roles before the application, none Gated out, so a slice asks for no stage moves at all
    assert sum("FROM js_stage_moves" in s for s in statements) <= 3, "never one for each role"
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all()
    assert {r.status for r in rows} == {stages.GATED_OUT}, "every role ends stopped"
