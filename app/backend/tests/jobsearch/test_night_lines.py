"""What the night left undone and the night's spend line are rows (`js_engine_facts`), written when the owner's off
hours end, so the page shows them without the engine's memory (the split of the app into a page and an engine, the
owner's point 3). Everything here is made up."""

import json
from datetime import UTC, datetime, timedelta

from app import db
from app.jobsearch import facts
from app.jobsearch.pool import core as pool
from tests.builders import analysis_setup
from tests.support import OWNER, pin_clock

ZONE = "America/New_York"


def _at(hour: int, minute: int = 0, day: int = 15) -> datetime:
    """An instant that is this local time in `ZONE` on a June day of 2027 (UTC-4)."""
    return datetime(2027, 6, day, hour, minute, tzinfo=UTC) + timedelta(hours=4)


async def _window_ends() -> None:
    """The schedule's two looks around 7 AM, as its loop makes them: the second finds the window ended, and the loop
    commits what it wrote."""
    async with db.session_factory()() as session:
        assert not await pool.note_window(session, OWNER, _at(6, 55))
        assert await pool.note_window(session, OWNER, _at(7, 0)), "taken as the window ends"
        await session.commit()


async def test_when_the_window_ends_both_lines_are_written_as_facts_another_session_reads(js, monkeypatch):
    pin_clock(monkeypatch, _at(7, 0), ZONE)
    await analysis_setup()
    await _window_ends()
    async with db.read_session_factory()() as other:
        left = json.loads(await facts.get(other, OWNER, "night_left"))
        spend = json.loads(await facts.get(other, OWNER, "night_spend"))
    assert left["day"] == spend["day"] == "2027-06-15"
    assert set(left["left"]) == set(pool.KINDS)
    assert left["left"][pool.RESEARCHER].startswith("At 7:00 AM: 3 left"), left["left"]
    assert spend["line"].startswith("Spent $") and "researcher 3 left" in spend["line"], spend["line"]


async def test_a_look_that_finds_the_window_has_not_ended_takes_no_writers_turn(js, monkeypatch):
    pin_clock(monkeypatch, _at(6, 50), ZONE)
    asked: list[float] = []
    watch = asked.append
    db.turn_watchers.append(watch)
    try:
        async with db.session_factory()() as session:
            assert not await pool.note_window(session, OWNER, _at(6, 50))
            assert not await pool.note_window(session, OWNER, _at(6, 55))
    finally:
        db.turn_watchers.remove(watch)
    assert asked == [], "the look read on a reader: no writer's turn was asked for"


async def test_the_page_shows_both_lines_from_the_rows_with_nothing_kept_in_memory(js, client, monkeypatch):
    assert not hasattr(pool, "_night_left") and not hasattr(pool, "_night_spend"), "the lines live in rows only"
    pin_clock(monkeypatch, _at(7, 0), ZONE)
    await analysis_setup()
    await _window_ends()
    pin_clock(monkeypatch, _at(9, 30), ZONE)
    sessions = (await client.get("/api/v1/agent-kinds/sessions")).json()
    assert sessions["budget_line"].startswith("Spent $") and "researcher 3 left" in sessions["budget_line"], sessions
    health = {k["kind"]: k["health"] for k in (await client.get("/api/v1/agent-kinds")).json()}
    assert health[pool.RESEARCHER].startswith("At 7:00 AM: 3 left"), health


async def test_the_next_day_the_lines_of_the_day_before_are_not_shown(js, client, monkeypatch):
    pin_clock(monkeypatch, _at(7, 0), ZONE)
    await analysis_setup()
    await _window_ends()
    pin_clock(monkeypatch, _at(9, 30, day=16), ZONE)
    assert (await client.get("/api/v1/agent-kinds/sessions")).json()["budget_line"] is None
    health = {k["kind"]: k["health"] for k in (await client.get("/api/v1/agent-kinds")).json()}
    assert not (health[pool.RESEARCHER] or "").startswith("At 7:00 AM"), health
