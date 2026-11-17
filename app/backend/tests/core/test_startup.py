"""The app's start-up (`main.lifespan`): the data fix-ups every start runs, on a database that an earlier release left
with the things they settle. ASGITransport never sends a lifespan, so no other test has run this: a step that raised
would be a crash loop at every start and the suite would stay green. Everything is made up."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch.fetch import host_stops
from app.jobsearch.fit import comp
from app.jobsearch.loops.startup import run_startup_fixups
from app.jobsearch.main import app
from app.jobsearch.missions import core as mission_service
from app.jobsearch.models import BrowserDomain, Employer, Mission, Opportunity
from app.jobsearch.research import analysis
from app.settings import settings
from app.sites import asks
from tests.builders import WORKER, WORKER_B, browser_world, hand_back, next_item
from tests.jobsearch.test_browser_work import NOW, _role
from tests.support import DEFAULT_OWNER, OWNER, load_profile


async def _state(session) -> dict:
    """What the fix-ups settle, as plain values to compare before and after a second start."""
    employers = (await session.scalars(select(Employer).where(Employer.owner == OWNER).order_by(Employer.name))).all()
    missions = (await session.scalars(select(Mission.key).where(Mission.owner == OWNER).order_by(Mission.key))).all()
    role = await session.scalar(select(Opportunity).where(Opportunity.slug == "old-ask"))
    purposes = await session.execute(select(Mission.key, Mission.purpose).where(Mission.owner == OWNER))
    return {
        "employers": [(e.name, e.gated, sorted(e.aliases or [])) for e in employers],
        "missions": list(missions),
        "purposes": dict(purposes.all()),
        "ask": role.analysis["ask"],
    }


async def test_a_start_settles_what_an_earlier_release_left_and_a_second_start_changes_nothing(
        js, monkeypatch, caplog):
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(settings, "run_schedule", False)
    # The start points the app at the database the settings name; the test keeps the one it has.
    monkeypatch.setattr(db, "configure", lambda url=None: db.engine())
    # ... and the start locks that database first (`instance_lock`), so the settings name the test's own file.
    monkeypatch.setattr(settings, "database_url", db.engine().url.render_as_string(hide_password=False))
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        # A researched role whose expectation an earlier method worked out.
        session.add(Opportunity(
            owner=OWNER, slug="old-ask", company="Fabrikam", title="Director of Programs", fit=4, origin="scout",
            match_key="fabrikam|director of programs", found=date.today(), url="https://x.test/old-ask",
            status="second_impression", analysis_state=analysis.DONE,
            analysis={"required": [{"item": f"p{i}", "class": "P"} for i in range(4)],
                      "pay": {"band": [200000, 260000]}, "ask": {"method_version": comp.METHOD_VERSION - 1}}))
        # A mission an earlier release made, with the words it had then.
        session.add(Mission(owner=OWNER, key="open_apply_tabs", name="Open apply tabs", purpose="Older words.",
                            schedule={"kind": "daily", "at": "03:00"}, state="paused", settings={"count": 5}))
        await session.commit()

    try:
        async with app.router.lifespan_context(app):
            pass
    finally:
        asks.set_ask_owner(None)  # the start wired the site loop's asks to the Inbox; the next test gets the default

    assert "start-up step" not in caplog.text, "a step that fails is logged, and no step failed"
    async with db.session_factory()() as session:
        first = await _state(session)
        assert first["ask"]["method_version"] == comp.METHOD_VERSION, "the old expectation was worked out again"
        assert {"scout_boards", "open_apply_tabs", "sweep_stuck"} <= set(first["missions"]), "the standing work exists"
        assert first["purposes"] == {key: mission_service.kind_of(key).purpose() for key in first["missions"]}, (
            "what each mission is for is the current text, the older mission's included")

        # The same steps again, on what the first start left (a second start is every restart of the app).
        await run_startup_fixups(session, OWNER)
        await session.commit()
        assert await _state(session) == first
        assert await session.scalar(select(func.count()).select_from(Employer).where(Employer.owner == OWNER)) == len(
            first["employers"])
    assert "start-up step" not in caplog.text


@pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")
async def test_two_rate_limit_rests_in_a_row_then_a_start_leave_the_domain_unstopped(js):
    """A 429 or a rate-limit page is a rest, never a stop (`host_stops`, docs/PRD.md path 28). A rest still counts one
    block on the domain, so two in a row leave `blocks` at 2: the start must not read that as a refusal."""
    await browser_world(extra_sites=False)
    await _role(1)
    first = await next_item(WORKER, NOW)
    await hand_back(WORKER, first["item"]["item_id"], "blocked", now=NOW, note="a rate-limit page")
    later = NOW + timedelta(hours=1, seconds=1)
    second = await next_item(WORKER_B, later)
    await hand_back(WORKER_B, second["item"]["item_id"], "blocked", now=later, note="429 too many requests")
    async with db.session_factory()() as session:
        before = await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == "example.test"))
        assert before.blocks == 2 and before.stopped_at is None, "two rests, no stop yet"

        await run_startup_fixups(session, DEFAULT_OWNER)
        await session.commit()

    async with db.session_factory()() as session:
        after = await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == "example.test"))
    assert after.stopped_at is None and after.stopped_for is None, "a rest is no stop, whatever a restart reads"
    assert host_stops.stopped_reason("example.test") is None


async def test_each_start_step_lets_go_of_the_writers_turn_when_it_is_done(js, monkeypatch):
    """The start's steps are transactions of their own, not savepoints of one: what a step wrote is committed, and the
    file's one turn is free, before the next begins (on a large database, one held turn would last a minute)."""
    from app.agents import roster
    from app.jobsearch.profile import store as profile_store

    seen: dict[str, int] = {}
    turns: list[float | None] = []
    first, second = roster.ensure_roster, profile_store.ensure_starter_writing

    async def writes_a_mission(session, owner):
        await first(session, owner)
        session.add(Mission(owner=owner, key="step-one", name="step one", state="on"))
        await session.flush()

    async def looks_from_outside(session, owner):
        async with db.read_session_factory()() as other:  # a reader sees only what is committed
            seen["committed"] = await other.scalar(select(func.count()).select_from(Mission).where(
                Mission.key == "step-one"))
        seen["turns_ended"] = len(turns)
        await second(session, owner)

    monkeypatch.setattr(roster, "ensure_roster", writes_a_mission)
    monkeypatch.setattr(profile_store, "ensure_starter_writing", looks_from_outside)
    db.turn_ends.append(lambda waited, held: turns.append(held))
    try:
        async with db.session_factory()() as session:
            await run_startup_fixups(session, OWNER)
            await session.commit()
    finally:
        db.turn_ends.pop()
    assert seen["committed"] == 1, "what the first step wrote was committed before the second began"
    assert seen["turns_ended"] >= 1, "and its turn was let go"


async def test_a_start_step_that_fails_loses_its_own_writes_only_and_the_others_run(js, monkeypatch, caplog):
    """A step that raises is rolled back to where it last committed and logged, and the steps after it still run."""
    from app.agents import roster
    from app.jobsearch.profile import store as profile_store

    async def writes_then_breaks(session, owner):
        session.add(Mission(owner=owner, key="lost", name="lost", state="on"))
        await session.flush()
        raise RuntimeError("this step broke")

    ran: list[str] = []
    later = profile_store.ensure_starter_writing

    async def counts(session, owner):
        ran.append("later")
        await later(session, owner)

    monkeypatch.setattr(roster, "ensure_roster", writes_then_breaks)
    monkeypatch.setattr(profile_store, "ensure_starter_writing", counts)
    async with db.session_factory()() as session:
        await run_startup_fixups(session, OWNER)
        await session.commit()
    assert ran == ["later"]
    assert "start-up step the roster failed" in caplog.text
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Mission).where(Mission.key == "lost")) == 0
