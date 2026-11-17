"""The stage and employer-limit settles read the deck's roles without three columns they never read (`fit_breakdown`,
`gate_answers`, `analysis_made_under`): each of them is a JSON decode per row on a deck of thousands, on the event loop.
Where the caller's own session reads (it holds changes), the roles load whole, so what it loaded stays readable.
Made-up data."""

from datetime import date

from sqlalchemy import event, select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import employer_limit, lifecycle
from tests.builders import counted
from tests.support import OWNER, load_profile

UNREAD = ("fit_breakdown", "gate_answers", "analysis_made_under")


async def _seed(*specs: tuple[str, str]) -> dict[str, object]:
    """One role per (slug, status), each with something in the three columns; returns their ids by slug."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        rows = {slug: Opportunity(
            owner=OWNER, slug=slug, company=f"Company {slug}", title=f"Title {slug}", origin="scout", status=status,
            match_key=f"c{slug}|{slug}", found=date.today(), fit_breakdown={"fit": 3}, gate_answers=[{"q": "a"}],
            analysis_made_under={"under": 1}, gate_facts={"facts": 1}, **counted())
            for slug, status in specs}
        session.add_all(rows.values())
        await session.flush()
        ids = {slug: row.id for slug, row in rows.items()}
        await session.commit()
    return ids


class _Statements:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def __enter__(self) -> "_Statements":
        self.engine = db.engine().sync_engine
        event.listen(self.engine, "before_cursor_execute", self._note)
        return self

    def __exit__(self, *_) -> None:
        event.remove(self.engine, "before_cursor_execute", self._note)

    def _note(self, _conn, _cursor, statement, _parameters, _context, _many) -> None:
        self.seen.append(statement)

    def roles_select(self, loaded: str) -> str:
        """The settle's read of the roles: the select of the roles' own columns by owner and status, found by `loaded`,
        a column that settle loads (each loads only the columns it reads, `load_only`)."""
        found = [s for s in self.seen if s.lstrip().upper().startswith("SELECT") and "FROM js_opportunities" in s
                 and "js_opportunities.status IN" in s and f"js_opportunities.{loaded}" in s.split("FROM")[0]]
        assert len(found) == 1, found
        return found[0].split("FROM")[0]


async def test_the_stage_settle_does_not_load_the_three_columns_it_never_reads(js):
    await _seed(("a", stages.FOLLOWUP_NEEDED), ("b", stages.CLEANSED_DATA))
    async with db.session_factory()() as session:
        with _Statements() as watch:
            moved = await lifecycle.settle(session, OWNER, set())
        await session.commit()
    read = watch.roles_select("analysis_claimed_at")
    assert all(f"js_opportunities.{c}" not in read for c in UNREAD), read
    assert all(f"js_opportunities.{c.key}" in read for c in lifecycle.SETTLE_COLUMNS), read
    assert "js_opportunities.gate_facts" not in read, "placement reads no gate facts for the stage rule"
    assert moved == 2, "both read roles go to First Impression, as with the whole load"
    async with db.session_factory()() as session:
        by_slug = {r.slug: r.status for r in await session.scalars(select(Opportunity))}
    assert by_slug == {"a": stages.FIRST_IMPRESSION, "b": stages.FIRST_IMPRESSION}  # as before the defers


async def test_the_employer_limit_settle_does_not_load_the_three_columns_it_never_reads(js):
    ids = await _seed(("a", stages.FIRST_IMPRESSION), ("b", stages.APPLIED))
    async with db.session_factory()() as session:
        with _Statements() as watch:
            done = await employer_limit.settle(session, OWNER)
        await session.commit()
    read = watch.roles_select("screened_out_reason")
    assert all(f"js_opportunities.{c}" not in read for c in UNREAD), read
    assert (done.held, done.returned) == (0, 0)
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, ids["a"])).status == stages.FIRST_IMPRESSION


async def test_the_employer_limit_settle_in_a_session_holding_changes_leaves_a_loaded_roles_columns_readable(js):
    ids = await _seed(("a", stages.FIRST_IMPRESSION))
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["a"])
        assert role.fit_breakdown == {"fit": 3}
        role.title = "Changed here"  # the session holds a change, so the settle reads in it and refreshes its roles
        await employer_limit.settle(session, OWNER)
        assert role.fit_breakdown == {"fit": 3} and role.gate_answers == [{"q": "a"}]
        assert role.analysis_made_under == {"under": 1}
