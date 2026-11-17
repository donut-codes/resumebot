"""The writes the page program answers now: a reason added to a saved version, the switches on
the job-board reading, on the job systems and on watching the employers in play, an interview corrected or removed,
the missions' numbers and a mission paused, resumed or changed, and the agent kinds' sessions, pauses and resumes.
None of them re-gates, re-ranks, fetches or starts work, so the engine is asked for nothing but a look, and each takes
the writer's turn once, for its write: the reply is read on a reader after the commit. Everything here is made up."""

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.actors import BY_OWNER
from app.jobsearch.factory import build_app
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Interview, Mission, Opportunity
from app.jobsearch.pool import core as pool
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import engine_calls
from app.models.agent import AgentKindPause
from app.settings import settings
from app.timeutil import owner_today
from tests.builders import save_documents
from tests.split.test_page_alone import Told
from tests.split.test_page_reads_take_no_write_lock import turns_recorded
from tests.split.test_route_programs import _keys
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")


def _app():
    """The app the writes are asked of: the page program's own app, which runs no engine."""
    from app.jobsearch import page

    return page.app


@pytest.fixture
async def asked(js, monkeypatch):
    """(a client of the app, the recorder that stands in for the engine), with the profile, the roster and the missions
    made."""
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        await missions.ensure(session, OWNER)
        await session.commit()
    told = Told()
    before = engine_calls._backend
    engine_calls.install(told)
    try:
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as http:
            yield http, told
    finally:
        engine_calls.install(before)


async def _write(http, method: str, url: str, **more):
    """One write as the owner clicks it: it answers 200, and it took the writer's turn once at most. A reply read on the
    writer's session after the commit would take a second turn, which is what this catches."""
    with turns_recorded() as turns:
        reply = await http.request(method, url, **more)
    assert reply.status_code == 200, reply.text
    assert len(turns.ends) <= 1, f"{method} {url} took {len(turns.ends)} turns of the writer"
    return reply.json()


def _asked_nothing_of_the_engine(told) -> None:
    """A nudge is a hint to look that needs no answer; anything else is work the page asked the engine for."""
    assert [call for call in told.calls if call[0] != "nudge"] == []


async def _settings():
    async with db.session_factory()() as session:
        return (await profile_store.current(session, OWNER)).settings


async def test_an_interview_is_corrected_and_removed(asked):
    http, told = asked
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="acme-lead", company="Acme", title="Program Lead", fit=4, origin="scout",
                           match_key="acme|program lead", status=stages.ACTIVE_DISCUSSION, found=owner_today())
        session.add(role)
        await session.flush()
        interview = Interview(owner=OWNER, role_id=role.id, when_at=datetime(2026, 11, 2, 10, 0, tzinfo=UTC),
                              round_kind="hiring_manager", label="Team chat", people=[], notes="")
        session.add(interview)
        await session.commit()
        interview_id = str(interview.id)
    edited = await _write(http, "PATCH", f"/api/v1/interviews/{interview_id}", json={"notes": "Bring the plan."})
    assert edited["id"] == interview_id and edited["notes"] == "Bring the plan."
    assert await _write(http, "DELETE", f"/api/v1/interviews/{interview_id}") == {"ok": True}
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Interview))).all() == []
    _asked_nothing_of_the_engine(told)


async def test_a_mission_is_paused_resumed_and_changed_and_the_numbers_are_saved(asked):
    http, told = asked
    async with db.session_factory()() as session:
        mission_id = str(await session.scalar(
            select(Mission.id).where(Mission.owner == OWNER, Mission.key == missions.SCOUT)))
    paused = await _write(http, "POST", f"/api/v1/missions/{mission_id}/pause", json={"why": "away"})
    assert paused["id"] == mission_id and paused["state"] == "paused"
    resumed = await _write(http, "POST", f"/api/v1/missions/{mission_id}/resume", json={})
    assert resumed["state"] == "on"
    changed = await _write(http, "PATCH", f"/api/v1/missions/{mission_id}",
                           json={"settings": {"focus": "operations"}, "why": "a narrower night"})
    assert "Focus: '' → 'operations'." in [change["what"] for change in changed["changes"]]
    numbers = await _write(http, "PATCH", "/api/v1/missions/numbers",
                           json={"values": {"site_projects_per_night": 12}, "why": "a slow week"})
    assert numbers["saved"] is True and numbers["version"] is not None
    assert (await _settings()).site_projects_per_night == 12
    _asked_nothing_of_the_engine(told)


async def test_the_agent_kinds_sessions_pauses_and_resumes_are_saved(asked):
    http, told = asked
    kind = pool.KINDS[0]
    sessions = await _write(http, "PUT", "/api/v1/agent-kinds/sessions", json={"count": 2})
    assert sessions["count"] == 2 and (await _settings()).active_sessions == 2
    paused_all = await _write(http, "POST", "/api/v1/agent-kinds/pause-all")
    assert kind in {row["kind"] for row in paused_all}
    async with db.session_factory()() as session:
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).paused is True
    await _write(http, "POST", "/api/v1/agent-kinds/resume-all")
    async with db.session_factory()() as session:
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).paused is False
    listed = await _write(http, "POST", f"/api/v1/agent-kinds/{kind}/pause")
    assert next(row for row in listed if row["kind"] == kind)["paused"] is True
    async with db.session_factory()() as session:
        assert (await session.scalars(select(AgentKindPause.kind).where(AgentKindPause.owner == OWNER))).all() == [kind]
    listed = await _write(http, "POST", f"/api/v1/agent-kinds/{kind}/resume")
    assert next(row for row in listed if row["kind"] == kind)["paused"] is False
    async with db.session_factory()() as session:
        assert (await session.scalars(select(AgentKindPause.kind).where(AgentKindPause.owner == OWNER))).all() == []
    _asked_nothing_of_the_engine(told)


async def test_the_switches_on_the_job_systems_and_the_employers_in_play_are_saved(asked):
    http, told = asked
    assert await _write(http, "PUT", "/api/v1/employers/watch-in-play", json={"on": True}) == {"on": True}
    system = EXAMPLE_SYSTEM  # a job system with a reader, on in a new profile
    shown = await _write(http, "PATCH", f"/api/v1/sites/job-systems/{system}", json={"on": False})
    assert "job_systems" in shown
    saved = await _settings()
    assert saved.watch_in_play is True and system in saved.job_systems_off
    _asked_nothing_of_the_engine(told)


async def test_a_reason_is_added_to_the_newest_version_the_owner_saved(asked):
    http, told = asked
    current = await _settings()
    async with db.session_factory()() as session:
        version = await profile_store.save(
            session, OWNER, "settings", {**current.model_dump(mode="json"), "active_sessions": 3}, by=BY_OWNER)
        await session.commit()
    row = await _write(http, "PUT", f"/api/v1/profile/settings/versions/{version}/why", json={"why": "a slow week"})
    assert row["version"] == version and row["why"] == "a slow week" and row["saved_by"] == BY_OWNER
    _asked_nothing_of_the_engine(told)


#: The writes this lane gave to the page, as the manifest names them ("METHOD /path").
FLIPPED = (
    "PUT /api/v1/profile/{kind}/versions/{number}/why",
    "PUT /api/v1/sites/robots-exempt",
    "PATCH /api/v1/sites/job-systems/{key}",
    "PATCH /api/v1/interviews/{interview_id}",
    "DELETE /api/v1/interviews/{interview_id}",
    "PATCH /api/v1/missions/numbers",
    "POST /api/v1/missions/{mission_id}/pause",
    "POST /api/v1/missions/{mission_id}/resume",
    "PATCH /api/v1/missions/{mission_id}",
    "PUT /api/v1/employers/watch-in-play",
    "PUT /api/v1/agent-kinds/sessions",
    "POST /api/v1/agent-kinds/pause-all",
    "POST /api/v1/agent-kinds/resume-all",
    "POST /api/v1/agent-kinds/{kind}/pause",
    "POST /api/v1/agent-kinds/{kind}/resume",
)


def test_the_page_serves_these_writes_and_the_engine_does_not():
    page, engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert sorted(set(FLIPPED) - page) == [], "writes the page does not serve"
    assert sorted(set(FLIPPED) & engine) == [], "writes the engine still serves"
