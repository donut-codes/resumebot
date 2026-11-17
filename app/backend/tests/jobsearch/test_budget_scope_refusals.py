"""The nightly dollar budget (`budget.py`: kept from each session's report, paced across the off hours, a stop at
the budget, the owner's own work never counted), the install's limit on the browser's domains
(`browser_pace.in_scope`: other domains' items are held, not dropped, and go on when it is widened). Every name,
address and number is made up."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import queue as bq
from app.jobsearch.browser import takeable
from app.jobsearch.browser.takeable import Taker
from app.jobsearch.missions import core as missions
from app.jobsearch.models import AgentSpend, BrowserItem, Opportunity
from app.jobsearch.pool import budget
from app.jobsearch.pool import core as pool
from app.jobsearch.profile import schema as profile
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds, session_lifecycle
from app.settings import settings
from app.timeutil import owner_zone
from tests.builders import analysis_setup, browser_world
from tests.jobsearch.test_browser_work import NOW, _role, _sourced
from tests.jobsearch.test_pool_queue import _asks
from tests.support import DEFAULT_OWNER, OWNER, committed

#: The registrable domain of the made-up board `ONE`: what the scope names.
BOARD = "example.test"


def _at(hour: int, minute: int = 0) -> datetime:
    """A moment in the owner's off hours (1 to 7 AM) on a made-up day."""
    return datetime(2026, 10, 19, hour, minute, tzinfo=owner_zone())


@pytest.fixture
def in_the_off_hours(monkeypatch):
    """Spend is the app's own (background) only inside the off hours; the tests do not read the wall clock."""
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: True)
    monkeypatch.setattr(budget, "utc_now", lambda: _at(6, 59).astimezone(UTC))  # the pool's own hold reads this clock


async def _set_budget(dollars: float | None) -> None:
    async with db.session_factory()() as session:
        await pool.set_active_sessions(session, OWNER, None, dollars)
        await session.commit()


async def _spend(session_id: str, kind: str, cost: float, now: datetime | None = None, **tokens) -> AgentSpend | None:
    async with db.session_factory()() as session:
        row = await budget.record(session, OWNER, session_id, kind, {"cost_usd": cost, **tokens}, now)
        await session.commit()
        return row


async def _hold(now: datetime | None = None) -> str | None:
    async with db.session_factory()() as session:
        return await budget.hold(session, OWNER, now)


# --- the budget -----------------------------------------------------------------------------------------------------


async def test_there_is_no_budget_until_the_owner_sets_one(js):
    await analysis_setup()
    async with db.session_factory()() as session:
        assert await budget.budget_of(session, OWNER) is None
    await _set_budget(12)
    async with db.session_factory()() as session:
        assert await budget.budget_of(session, OWNER) == 12.0
        assert (await profile_store.current(session, OWNER)).settings.active_sessions == 1, "the other setting is kept"
    async with db.session_factory()() as session:
        await pool.set_active_sessions(session, OWNER, 2)
        await session.commit()
    async with db.session_factory()() as session:
        assert await budget.budget_of(session, OWNER) == 12.0, "no change leaves the budget as it is"
    await _set_budget(None)
    async with db.session_factory()() as session:
        assert await budget.budget_of(session, OWNER) is None, "clearing it means no limit"


async def test_with_no_budget_the_pool_never_holds_or_paces(js, in_the_off_hours):
    await analysis_setup()
    await _spend("analyst-1", "analyst", 5000.0, _at(1, 0))
    assert await _hold(_at(1, 5)) is None
    assert await _hold(_at(6, 59)) is None


async def test_a_set_budget_still_paces_across_the_window(js, in_the_off_hours):
    await analysis_setup()
    await _set_budget(40)
    await _spend("analyst-1", "analyst", 30.0, _at(1, 0))
    assert "pacing" in (await _hold(_at(1, 30)) or "")


async def test_a_saved_hundred_thousand_reads_as_no_limit(js, in_the_off_hours):
    await analysis_setup()
    async with db.session_factory()() as session:
        current = (await profile_store.current(session, OWNER)).settings
        saved = {**current.model_dump(mode="json"), "nightly_budget": 100000.0}
        await profile_store.save(session, OWNER, "settings", saved, by="owner", why="the old stand-in for no limit")
        await session.commit()
    async with db.session_factory()() as session:
        assert await budget.budget_of(session, OWNER) is None
    await _spend("analyst-1", "analyst", 5000.0, _at(1, 0))
    assert await _hold(_at(1, 5)) is None


def test_the_settings_accept_null_and_a_number_for_the_budget():
    assert profile.Settings(nightly_budget=None).nightly_budget is None
    assert profile.Settings(nightly_budget=25).nightly_budget == 25.0
    assert profile.Settings(nightly_budget=100000).nightly_budget is None


async def test_the_sessions_api_takes_null_and_a_number(js, client):
    await analysis_setup()
    assert (await client.get("/api/v1/agent-kinds/sessions")).json()["budget"] is None
    assert (await client.put("/api/v1/agent-kinds/sessions", json={"budget": 15})).json()["budget"] == 15.0
    assert (await client.put("/api/v1/agent-kinds/sessions", json={"count": 2})).json()["budget"] == 15.0
    assert (await client.put("/api/v1/agent-kinds/sessions", json={"budget": None})).json()["budget"] is None


async def test_a_finished_session_s_cost_is_kept_once_and_a_session_with_no_cost_adds_nothing(js, in_the_off_hours):
    await analysis_setup()
    assert (await _spend("analyst-1", "analyst", 1.25, input_tokens=1000, output_tokens=200)).cost_usd == 1.25
    assert await _spend("analyst-1", "analyst", 1.25) is None, "a heartbeat said twice is counted once"
    assert await _spend("analyst-2", "analyst", 0) is None
    async with db.session_factory()() as session:
        # Its tokens alone give a cost when the result printed none (a rough one, at API prices).
        row = await budget.record(session, OWNER, "writer-1", "writer", {"input_tokens": 1_000_000, "output_tokens": 0})
        assert row is not None and row.cost_usd == pytest.approx(3.0)


async def test_the_ended_heartbeat_carries_the_cost_into_the_night_s_spend(js, in_the_off_hours):
    await analysis_setup()
    async with db.session_factory()() as session:
        await session_lifecycle.settle(session, OWNER, {"state": "ended", "session_id": "analyst-aaaa",
                                             "kind": "analyst",
                                             "spend": {"cost_usd": 2.5, "input_tokens": 10, "output_tokens": 5}})
        await session.commit()
        st = await budget.state(session, OWNER)
    assert st.spent == 2.5 and st.sessions == 1


async def test_the_owner_s_own_work_is_never_counted_against_the_budget(js, in_the_off_hours):
    await analysis_setup()
    await _spend("analyst-bg", "analyst", 3.0)  # the pool's own pull: counted
    await _spend("parser-1", "parser", 5.0)  # a posting the owner pasted: theirs
    await committed(pool.owner_started_mission, OWNER, missions.GO)  # Go: the writer is theirs for the hour
    await _spend("writer-1", "writer", 7.0)
    async with db.session_factory()() as session:
        rows = {r.session_id: r.background for r in (await session.scalars(select(AgentSpend))).all()}
        assert rows == {"analyst-bg": True, "parser-1": False, "writer-1": False}
        assert (await budget.state(session, OWNER)).spent == 3.0


async def test_outside_the_off_hours_nothing_a_session_cost_is_the_app_s_own(js, monkeypatch):
    await analysis_setup()
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    row = await _spend("analyst-1", "analyst", 3.0)
    assert row is not None and row.background is False


async def test_the_budget_is_paced_across_the_window_not_spent_in_the_first_hour(js, in_the_off_hours):
    await analysis_setup()
    await _set_budget(12)
    await _spend("analyst-1", "analyst", 3.5, _at(1, 30))
    # 2 AM: an hour and a half of six in, so $3.00 of the $12 is allowed; $3.50 is ahead of the clock.
    assert "pacing" in (await _hold(_at(2, 0)) or "")
    # 5 AM: 5.5 of 6 hours in, $11 allowed: the pool may go on.
    assert await _hold(_at(5, 0)) is None


async def test_the_pool_stops_starting_background_work_when_the_budget_is_reached(js, in_the_off_hours):
    await analysis_setup()
    await _set_budget(5)
    await _spend("analyst-1", "analyst", 5.0, _at(3, 0))
    assert "reached" in (await _hold(_at(6, 59)) or "")
    # The deck's research is bounded and cheap: the budget does not hold it (the rest of the pool's own work waits).
    assert await _asks() == [session_kinds.ANALYSIS]
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity).where(Opportunity.analysis_state == "waiting"))).all():
            analysis.claim(role)
        await session.commit()
    assert await _asks() == [None], "the search, readers and employers of the pool's own wait for the budget"
    await _set_budget(100)
    assert await _asks() == [session_kinds.SEARCH], "and start once the budget allows them"


async def test_what_the_owner_started_still_goes_when_the_budget_is_reached(js, in_the_off_hours):
    await analysis_setup()
    await _set_budget(5)
    await _spend("analyst-1", "analyst", 5.0, _at(3, 0))
    async with db.session_factory()() as session:
        out = await pool.run_kind(session, OWNER, pool.RESEARCHER)  # Run now on the researcher
        await session.commit()
    assert out["queued"] == 3
    assert await _asks() == [session_kinds.ANALYSIS], (
        "the owner's Run now is handed out, in the same order, at any budget")


async def test_the_seven_oclock_line_says_the_spend_and_what_was_left_undone(js, in_the_off_hours):
    await analysis_setup()
    await _set_budget(5)
    await _spend("analyst-1", "analyst", 5.0, _at(3, 0))
    async with db.session_factory()() as session:
        left = await pool.night_left(session, OWNER, _at(7, 0))
        line = budget.line(await budget.state(session, OWNER, _at(7, 0)), left)
    assert line.startswith("Spent $5.00 of the $5 nightly budget (reached")
    assert "researcher" in line and "3 left" in line, "what the budget left undone is named"


async def test_the_seven_oclock_line_without_a_budget_says_only_the_spend(js, in_the_off_hours):
    await analysis_setup()
    await _spend("analyst-1", "analyst", 5.0, _at(3, 0))
    async with db.session_factory()() as session:
        line = budget.line(await budget.state(session, OWNER, _at(7, 0)), {})
    assert line.startswith("Spent $5.00 in the night")
    assert "budget" not in line and "reached" not in line


# --- the browser's scope ----------------------------------------------------------------------------------------------


async def _queue(*, code_tried: bool = True) -> None:
    async with db.session_factory()() as session:
        await bq.refresh(session, DEFAULT_OWNER, NOW)
        if code_tried:
            for row in (await session.scalars(select(BrowserItem))).all():
                row.code_at = NOW  # the app's code has tried each: only a session can take it
        await session.commit()


async def _ready(*, code: bool = False) -> list[BrowserItem]:
    async with db.session_factory()() as session:
        taker = Taker(DEFAULT_OWNER, bp.WORKER, tuple(bp.WORK[bp.WORKER]), code_first=code, owners_only=False)
        got = await takeable.ask(session, taker, NOW + timedelta(seconds=20))
        return [row for row, _ in got]


async def test_by_default_the_browser_works_on_every_domain(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await _sourced(2)
    await _queue()
    assert {row.kind for row in await _ready()} == {bp.SOURCE, bp.READ, bp.SCAN}
    assert bp.in_scope("anything.example.test")


async def test_a_scope_holds_the_other_domains_items_waiting_and_marked_and_widening_it_releases_them(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _role(1)  # a board role: a source item on ONE
    await _sourced(2)  # a read item on another domain
    await _queue()
    monkeypatch.setattr(settings, "browser_domains", BOARD)
    await _queue()
    ready = await _ready()
    assert {row.kind for row in ready} == {bp.SOURCE, bp.SCAN} and all(bp.in_scope(r.domain) for r in ready)
    async with db.session_factory()() as session:
        waiting = {r.kind: r for r in (await session.scalars(select(BrowserItem))).all()}
    held = waiting[bp.READ]
    assert held.state == bp.WAITING, "held, not dropped"
    assert held.last_note == f"held: browser limited to {BOARD}"
    assert waiting[bp.SOURCE].last_note is None
    async with db.session_factory()() as session:
        assert len(await bq.free(session, DEFAULT_OWNER, bp.WORKER, NOW + timedelta(seconds=20))) == 1
        health = await bq.health(session, DEFAULT_OWNER, bp.WORKER, NOW + timedelta(seconds=20))
    assert f"1 held: browser limited to {BOARD}" in (health or "")
    # Widened: the held item is the app's own again and waits no more.
    monkeypatch.setattr(settings, "browser_domains", "")
    await _queue()
    assert {row.kind for row in await _ready()} == {bp.SOURCE, bp.READ, bp.SCAN}
    async with db.session_factory()() as session:
        assert (await session.scalars(select(BrowserItem.last_note).where(BrowserItem.kind == bp.READ))).one() is None


async def test_a_scope_names_a_domain_and_its_subdomains_only(js, monkeypatch):
    monkeypatch.setattr(settings, "browser_domains", "Example.test, other.test")
    assert bp.in_scope("example.test") and bp.in_scope("www.example.test") and bp.in_scope("other.test")
    assert not bp.in_scope("badexample.test") and not bp.in_scope("example.test.evil.test")


async def test_the_apps_own_code_reads_are_not_limited_by_the_scope(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _sourced(2)  # a read item on another domain, not yet tried by the app's code
    monkeypatch.setattr(settings, "browser_domains", BOARD)
    await _queue(code_tried=False)
    assert [row.kind for row in await _ready(code=True)] == [bp.READ], "code still tries it first"
    assert [r for r in await _ready() if r.kind == bp.READ] == [], "no session takes it while the scope holds it"
