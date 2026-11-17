"""An employer's pane reads the five fields it shows for each role, not whole role rows."""
from datetime import date, timedelta

from sqlalchemy import event, select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.models import Opportunity
from tests.employer_builders import make_employer, make_role
from tests.support import OWNER


async def _world(session):
    acme = await make_employer(session, "Acme")
    other = await make_employer(session, "Globex")
    today = date.today()
    roles = [make_role(acme, "Oldest"), make_role(acme, "Newest", status=stages.APPLIED), make_role(acme, "Middle"),
             make_role(acme, "Undated"), make_role(other, "Elsewhere")]
    for role, found, fit in zip(roles, (today - timedelta(days=9), today, today - timedelta(days=3), None, today),
                                (2, 5, 3, 3, 3), strict=True):
        role.found, role.fit = found, fit
    roles[0].description = "long text " * 50
    session.add_all(roles)
    await session.flush()
    return acme


async def test_the_pane_lists_every_role_newest_first_with_the_same_fields_as_whole_rows(js):
    async with db.session_factory()() as session:
        acme = await _world(session)
        whole = (await session.scalars(select(Opportunity).where(Opportunity.employer_id == acme.id)
                                       .order_by(Opportunity.found.desc().nulls_last()))).all()
        old = employers.view(acme, list(whole))["roles"]
        got = (await employers.detail(session, OWNER, acme.id))["roles"]
    assert got == old
    assert [r["title"] for r in got] == ["Newest", "Middle", "Oldest", "Undated"]


async def test_the_role_statement_selects_exactly_five_columns_and_no_role_entity(js):
    async with db.session_factory()() as session:
        acme = await _world(session)
    statement = employers.roles_shown(acme.id)
    assert [c.name for c in statement.selected_columns] == ["id", "title", "status", "fit", "found"]
    assert all(d["type"] is not Opportunity for d in statement.column_descriptions)


async def test_the_role_read_loads_no_description_column(js):
    seen: list[str] = []
    async with db.session_factory()() as session:
        acme = await _world(session)
        engine = session.get_bind()

        def spy(conn, cursor, statement, *rest):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", spy)
        try:
            await employers.detail(session, OWNER, acme.id)
        finally:
            event.remove(engine, "before_cursor_execute", spy)
    roles_reads = [s for s in seen if "FROM js_opportunities" in s and "js_opportunities.employer_id" in s]
    assert roles_reads
    assert all("js_opportunities.description" not in s for s in roles_reads)
