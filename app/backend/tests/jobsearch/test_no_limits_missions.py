"""No character limit on what the owner writes in the Missions screen (the owner, 2026-10-14: a length limit on what
they
type is "a terrible feature"). The Why beside a mission's numbers, its schedule, its settings, Pause and Resume takes
any length and the History shows it whole; so does the scout's Focus, which a started search hands to the scout as it
was typed. What stays is a count and the floor of what a setting must hold. Synthetic data only."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Mission, MissionChange, ProfileVersion, SearchRun
from tests.support import OWNER, TOKEN, load_profile

LONG = 20_000


@pytest.fixture
def door(scout_token):
    """The scout's context for a tool called on its door, acting for the owner the `client` fixture set (the shared
    `door` fixture sets the setting's default owner instead, which this file's data is not made for)."""
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


def _words(tag: str, n: int = LONG) -> str:
    """About `n` characters of made-up plain sentences, each different, so a cut anywhere shows."""
    out, i = [], 0
    while sum(len(s) + 1 for s in out) < n:
        i += 1
        out.append(f"{tag} sentence {i} says one more thing the owner wants.")
    return " ".join(out)[: n - 1] + "!"  # exactly n characters, and no space at either end for a save to trim


async def _start() -> dict[str, str]:
    """A profile, the roster and the missions, and each mission's id by key."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await roster.ensure_roster(session, OWNER)
        await missions.ensure(session, OWNER)
        await session.commit()
        rows = (await session.scalars(select(Mission).where(Mission.owner == OWNER))).all()
        return {m.key: str(m.id) for m in rows}


async def _changes() -> list[MissionChange]:
    async with db.session_factory()() as session:
        rows = await session.scalars(select(MissionChange).where(MissionChange.owner == OWNER))
        return list(rows.all())


async def test_the_why_beside_the_mission_numbers_is_kept_whole_as_the_settings_versions_why(client):  # 15
    await _start()
    why = _words("why")
    saved = await client.patch("/api/v1/missions/numbers", json={"values": {"site_projects_per_night": 12}, "why": why})
    assert saved.status_code == 200 and saved.json()["saved"] is True, saved.text
    async with db.session_factory()() as session:
        stored = (await session.scalars(select(ProfileVersion).where(
            ProfileVersion.owner == OWNER, ProfileVersion.kind == "settings", ProfileVersion.version == 2))).one()
    assert stored.why == why and len(stored.why) == LONG
    history = (await client.get("/api/v1/profile/settings/history")).json()["versions"]
    assert history[0]["why"] == why, "the History shows it whole"


async def test_a_blank_why_beside_the_mission_numbers_still_says_what_changed(client):  # 15
    await _start()
    saved = await client.patch("/api/v1/missions/numbers", json={
        "values": {"site_projects_per_night": 12}, "why": "  "})
    assert saved.status_code == 200 and saved.json()["saved"] is True, saved.text
    history = (await client.get("/api/v1/profile/settings/history")).json()["versions"]
    assert history[0]["why"].startswith("changed "), history[0]["why"]


async def test_the_why_beside_a_missions_schedule_settings_pause_and_resume_is_kept_whole(client):  # 15
    ids = await _start()
    analyse = ids[missions.ANALYSE]
    why = _words("why")

    schedule = await client.patch(f"/api/v1/missions/{analyse}", json={"schedule": {"kind": "daily", "at": "03:30"},
                                                                     "why": why})
    assert schedule.status_code == 200, schedule.text
    assert schedule.json()["changes"][0]["why"] == why, "the mission's History shows it whole"

    settings = await client.patch(f"/api/v1/missions/{analyse}", json={"settings": {"target": 2}, "why": why + " two"})
    assert settings.status_code == 200, settings.text
    assert settings.json()["changes"][0]["why"] == why + " two"

    paused = await client.post(f"/api/v1/missions/{analyse}/pause", json={"why": why + " three"})
    assert paused.status_code == 200, paused.text
    assert paused.json()["changes"][0]["why"] == why + " three"

    resumed = await client.post(f"/api/v1/missions/{analyse}/resume", json={"why": why + " four"})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["changes"][0]["why"] == why + " four"

    stored = {c.why for c in await _changes()}
    assert {why, why + " two", why + " three", why + " four"} <= stored, "stored whole, not only shown whole"


async def test_the_scouts_focus_saves_reads_back_whole_and_is_what_a_started_search_hands_the_scout(
        client, door, knocks):  # 16
    ids = await _start()
    scout = ids[missions.SCOUT]
    focus = _words("focus")
    saved = await client.patch(f"/api/v1/missions/{scout}", json={"settings": {"focus": focus}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["focus"] == focus and len(saved.json()["settings"]["focus"]) == LONG
    assert (await client.get(f"/api/v1/missions/{scout}")).json()["settings"]["focus"] == focus

    ran = await client.post(f"/api/v1/missions/{scout}/run")
    assert ran.status_code == 200, ran.text
    async with db.session_factory()() as session:
        [search] = (await session.scalars(select(SearchRun).where(SearchRun.owner == OWNER))).all()
    assert search.focus == focus and len(search.focus) == LONG
    handed = await mcp_server.jobs_profile(door)
    assert handed["task"]["focus"] == focus, "the scout is handed the Focus as it was typed"


async def test_a_search_started_by_hand_takes_a_focus_of_any_length_too(js):  # 16
    from app.jobsearch.missions import search_run as run

    await _start()
    focus = _words("focus", 50_000)
    async with db.session_factory()() as session:
        started = await run.start_round(session, OWNER, focus=focus)
        await session.commit()
        assert started.focus == focus


async def test_a_focus_is_still_folded_to_one_line_and_the_other_settings_still_hold_their_floor(client):  # 16
    ids = await _start()
    scout = ids[missions.SCOUT]
    folded = await client.patch(f"/api/v1/missions/{scout}", json={"settings": {"focus": "  banks \n and   credit  "}})
    assert folded.status_code == 200 and folded.json()["settings"]["focus"] == "banks and credit"
    bad = await client.patch(f"/api/v1/missions/{scout}", json={"settings": {"keep_target": 0}})
    assert bad.status_code == 422, "a count still has its floor"

