"""`make_roles` resolves a slice's employers in one pass: the same employer per role as a find / find_or_make per
posting, in at most two statements over the employers. Synthetic data only."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import event, select

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.pipeline import from_posting as role_from_posting
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

NOW = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
COMPANIES = ["Contoso", "Northwind Traders", "Tailspin Toys", "NORTHWIND TRADERS", "Contoso", "Northwind  Traders"]


async def test_a_slice_finds_or_makes_its_employers_like_one_find_each_in_two_statements_over_them(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        contoso = await employers.find_or_make(session, OWNER, "Contoso")
        assert contoso is not None
        contoso.aliases = ["Tailspin Toys"]
        await session.flush()
        for n, company in enumerate(COMPANIES):
            title = f"Programs Director {n}"
            session.add(JobPosting(
                owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=f"p{n}", company=company, title=title,
                match_key=match_key(company, title), location="Remote", work_mode="remote",
                url=f"https://jobs.example.test/{n}", first_seen=NOW, last_seen=NOW, filtered_at=NOW, route="roles",
                links=[]))
        await session.flush()
        seen: list[str] = []
        engine = db.engine().sync_engine

        def noted(_conn, _cursor, statement, _parameters, _context, _many):
            seen.append(" ".join(statement.split()).upper())

        event.listen(engine, "before_cursor_execute", noted)
        try:
            assert await role_from_posting.make_roles(session, OWNER) == 6
        finally:
            event.remove(engine, "before_cursor_execute", noted)
        roles = {r.company + str(i): r.employer_id for i, r in enumerate(
            (await session.scalars(select(Opportunity).order_by(Opportunity.title))).all())}
        northwind = await employers.find(session, OWNER, "Northwind Traders")
        assert northwind is not None and northwind.id != contoso.id
        assert list(roles.values()) == [contoso.id, northwind.id, contoso.id, northwind.id, contoso.id, northwind.id]
        assert len((await session.scalars(select(Employer).where(Employer.owner == OWNER))).all()) == 2
    # the finding of the slice's employers; the other read of the table is the watch list's, which this does not touch
    finding = [s for s in seen if "FROM JS_EMPLOYERS WHERE JS_EMPLOYERS.OWNER = ? AND" in s]
    assert len(finding) <= 2
