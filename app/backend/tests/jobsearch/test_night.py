"""The night's run (path 25): the peak window and what it holds back, the analyst following the search, the scout's
night ending at its target or cap or when every site is searched, the finish time handing back what nobody began, the
cover letter setting, and which agent is at work. Everything is made up."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.missions import core as missions
from app.jobsearch.missions.kinds import scout as scout_kind
from app.jobsearch.models import Mission, Opportunity, Packet, SearchRun
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Settings
from app.jobsearch.research import analysis
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from app.jobsearch.writing import packets
from tests.builders import api_url, compose_reply, run_seats, save_documents
from tests.support import OWNER, POSTING_WORDS, load_profile

#: 6:00 Pacific on a summer day: inside the default peak window (5:00 to 11:00 Pacific).
IN_PEAK = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
#: 1:00 Pacific: outside it.
NIGHT = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)


async def _setup(found=(("Fabrikam", 4), ("Initech", 4), ("Hooli", 4))) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={})  # these tests are about peak hours: the default box, ticked
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        for company, fit in found:
            session.add(Opportunity(owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", fit=fit,
                                    origin="scout", match_key=f"{company.lower()}|programs lead",
                                        status=stages.SECOND_IMPRESSION,
                                    found=date.today(), description=POSTING_WORDS))
        await missions.ensure(session, OWNER)
        await session.commit()


def agents_url() -> str:
    return api_url("agents")


async def _mission(session, key) -> Mission:
    return await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == key))


def test_the_peak_window_is_read_in_its_own_zone_and_shown_in_the_owners():
    s = Settings()
    assert night.in_peak(s, IN_PEAK) and not night.in_peak(s, NIGHT)
    assert not night.in_peak(Settings(avoid_peak=False), IN_PEAK), "the box unticked holds nothing"
    winter = datetime(2026, 1, 15, 14, 0, tzinfo=UTC)  # 6:00 Pacific standard time
    assert night.in_peak(s, winter), "daylight saving followed"
    assert night.peak_ends(s, IN_PEAK) == datetime(2026, 7, 1, 18, 0, tzinfo=UTC)
    assert night.last_peak_end(s, IN_PEAK + timedelta(hours=6)) == datetime(2026, 7, 1, 18, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        Settings(peak_zone="Mars/Olympus")


def test_the_peak_window_is_for_weekdays_only_but_is_still_told_on_a_weekend():
    s = Settings()
    saturday = datetime(2026, 7, 4, 13, 0, tzinfo=UTC)  # 6:00 Pacific on a Saturday
    assert not night.in_peak(s, saturday) and night.blackout(s, saturday) is None
    assert night.peak_ends(s, saturday) is None
    assert night.last_peak_end(s, saturday) == datetime(2026, 7, 3, 18, 0, tzinfo=UTC), "the Friday window was the last"
    assert night.in_peak(s, datetime(2026, 7, 3, 13, 0, tzinfo=UTC)), "the Friday before"
    assert night.window_words(s, saturday) == night.window_words(s, IN_PEAK), "the window is still said on a weekend"
    assert night.finish_moment(datetime(2026, 7, 1, 8, 0, tzinfo=UTC), None) is None


async def test_peak_hours_hold_new_claude_work_but_not_the_sweep(js, monkeypatch):
    await _setup()
    monkeypatch.setattr(night, "in_peak", lambda s, now=None: bool(s.avoid_peak))
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, (await analysis.picks(session, OWNER, 1))[0])
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) is None, "the analysis waits for the window to end"
        analyse = await _mission(session, missions.ANALYSE)
        await missions.resume(session, analyse, by="Tester")
        assert await missions.schedule_pass(session, OWNER) == []
        await profile_store.save(session, OWNER, "settings", {"avoid_peak": False}, by="test")
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS, "unticked, the work goes on"


async def test_the_analyst_follows_the_search_and_a_night_with_no_match_ends_when_every_site_is_searched(js):
    """A search finds employers' boards and judges no posting (a posting that passes the gates becomes a role at once),
    so once it has ended and the read of the boards is done there is nothing for a second search to work on."""
    await _setup(found=())
    async with db.session_factory()() as session:
        scout, analyse = await _mission(session, missions.SCOUT), await _mission(session, missions.ANALYSE)
        await missions.resume(session, analyse, by="Tester")
        await missions.change(session, scout, by="Tester", values={"keep_target": 3, "max_sessions": 2})
        entry = await missions.start(session, OWNER, scout, by="you")
        assert not await missions.due(session, analyse), "nothing to follow yet"
        first = await session.get(SearchRun, uuid.UUID(entry.started["search_runs"][0]))
        first.status, first.counts, first.ended_at = "done", {"kept": 2, "reported": 2}, datetime.now(UTC)
        await session.flush()
        knocks = await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert knocks == [] and len(entry.started["search_runs"]) == 1, "no second search: nothing is left to search"
        assert entry.outcome == "done" and entry.started["stopped"] == "searched"
        assert "Stopped: every site searched." in entry.summary
        assert await missions.due(session, analyse), "the search finished: the analyst follows"


def _roles_from(session, search, *statuses) -> None:
    """One role stored by `search` in each of the given stages."""
    for i, status in enumerate(statuses):
        name = f"{search.id}-{i}"
        session.add(Opportunity(owner=OWNER, slug=name, company=f"Company {i}", title="Programs Lead", fit=4,
                                origin="scout", match_key=name, status=status, found=date.today(),
                                source_run=search.id, description=POSTING_WORDS))


async def test_the_scouts_number_of_matches_counts_roles_that_reached_needs_drafted(js):
    """The target is the roles that passed the gates and reached Needs Drafted, not every role the scout kept: a
    search that kept twelve roles, none of them researched yet, leaves the target of three unmet."""
    await _setup(found=())
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        await missions.change(session, scout, by="Tester", values={"keep_target": 3, "max_sessions": 3})
        entry = await missions.start(session, OWNER, scout, by="you")
        first = await session.get(SearchRun, uuid.UUID(entry.started["search_runs"][0]))
        first.status, first.counts, first.ended_at = "done", {"kept": 14, "reported": 14}, datetime.now(UTC)
        _roles_from(session, first, *[stages.SECOND_IMPRESSION] * 12, stages.GATED_OUT, stages.READY_TO_LAUNCH)
        await session.flush()
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.started["done"] == 1 and entry.started["kept"] == 14, "1 match among 14 roles kept"
        assert entry.outcome == "done" and entry.started["stopped"] == "searched", "1 match of 3: not 'target met'"
        assert entry.summary == "Reached 1 of 3 matches in 1 search (14 new roles kept). Stopped: every site searched."

        second = await missions.start(session, OWNER, scout, by="you")
        search = await session.get(SearchRun, uuid.UUID(second.started["search_runs"][0]))
        search.status, search.counts, search.ended_at = "done", {"kept": 4, "reported": 4}, datetime.now(UTC)
        _roles_from(session, search, stages.READY_TO_LAUNCH, stages.READY_TO_LAUNCH, stages.READY_TO_LAUNCH,
                    stages.SECOND_IMPRESSION)
        await session.flush()
        await missions.advance(session, OWNER, scout, second, False, datetime.now(UTC))
        assert second.outcome == "done" and second.started["stopped"] == "target"
        assert second.started["done"] == 3 and second.started["kept"] == 4
        assert second.summary == "Reached 3 matches in 1 search (4 new roles kept). Stopped: target met."
        assert missions.run_view(second, missions.SCOUT)["progress"] == {"done": 3, "target": 3}


async def test_roles_from_other_runs_and_the_tracker_are_not_this_nights_matches(js):
    await _setup(found=())
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        mine = await session.get(SearchRun, uuid.UUID(entry.started["search_runs"][0]))
        earlier = SearchRun(owner=OWNER, status="done")
        session.add(earlier)
        await session.flush()
        _roles_from(session, earlier, stages.READY_TO_LAUNCH, stages.READY_TO_LAUNCH)
        _roles_from(session, mine, stages.READY_TO_LAUNCH)
        session.add(Opportunity(owner=OWNER, slug="old", company="Old", title="Lead", origin="tracker",
                                match_key="old|lead", status=stages.READY_TO_LAUNCH))
        await session.flush()
        assert await scout_kind.matches(session, entry.started["search_runs"]) == 1
        assert await scout_kind.matches(session, []) == 0


async def test_a_scout_run_ends_when_its_search_ended_not_when_the_schedule_pass_noticed(js):
    """Every search run ended an exact multiple of the pass's interval after it
    began, because the pass stamped the run with its own clock."""
    await _setup(found=())
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        # The finish time must not fall inside the ten minutes this test spans, or the run ends "finish" at 06:00.
        later = (datetime.now(night.owner_zone()) + timedelta(hours=2)).strftime("%H:%M")
        await missions.change(session, scout, by="Tester",
                              values={"keep_target": 2, "max_sessions": 1, "finish_by": later})
        entry = await missions.start(session, OWNER, scout, by="you")
        entry.started_at = datetime.now(UTC) - timedelta(minutes=10)
        over = datetime.now(UTC) - timedelta(minutes=4, seconds=12)
        search = await session.get(SearchRun, uuid.UUID(entry.started["search_runs"][0]))
        search.status, search.counts, search.ended_at = "done", {"kept": 2}, over
        _roles_from(session, search, stages.READY_TO_LAUNCH, stages.READY_TO_LAUNCH)  # two matches: the target
        await session.flush()
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "done" and entry.started["stopped"] == "target"
        assert entry.ended_at == over, "the search's end, not the minute the pass looked"

        second = await missions.start(session, OWNER, scout, by="you")
        second.started_at = datetime.now(UTC) - timedelta(minutes=10)
        over = datetime.now(UTC) - timedelta(minutes=3)
        search = await session.get(SearchRun, uuid.UUID(second.started["search_runs"][0]))
        search.status, search.counts, search.ended_at = "done", {"kept": 1}, over
        await session.flush()
        await missions.advance(session, OWNER, scout, second, False, datetime.now(UTC))
        assert second.outcome == "done" and second.started["stopped"] == "cap" and second.ended_at == over


async def test_the_finish_time_hands_back_what_nobody_began(js):
    await _setup()
    async with db.session_factory()() as session:
        analyse = await _mission(session, missions.ANALYSE)
        await missions.change(session, analyse, by="Tester", values={"target": 2, "finish_by": "06:00"})
        entry = await missions.start(session, OWNER, analyse, by="you")
        assert entry.started["target"] == 2 and len(entry.started["roles"]) == 2
        later = night.finish_moment(entry.started_at.replace(tzinfo=UTC), "06:00") + timedelta(minutes=1)
        await missions.advance(session, OWNER, analyse, entry, False, later)
        assert entry.outcome == "done" and entry.started["stopped"] == "finish"
        roles = (await session.scalars(select(Opportunity).where(Opportunity.analysis_state.is_(None)))).all()
        assert len(roles) == 3, "back in the queue for the next night"


async def test_a_packet_without_a_letter_is_never_filed_ready(js):
    await _setup()
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity))).first()
        packet = await packets.start(session, OWNER, role.id)
        await session.commit()
    bare = compose_reply(letter={"paragraphs": []})
    failing = {"claims": [], "failures": []}
    await run_seats({"Compose": bare, "Facts_1": failing, "Fix": {"resume": {}},
                     "Facts_2": failing, "Arbitrate": {"rulings": [], "fix_list": []}})
    async with db.session_factory()() as session:
        stored = await session.get(Packet, packet.id)
    assert stored.status == "held"
    assert any(c["check"] == "A cover letter" and not c["ok"] for c in stored.checks)


async def test_the_heartbeat_says_which_agent_is_at_work(client, monkeypatch):
    await _setup()
    monkeypatch.setattr("app.jobsearch.routes.runner.token_ok", lambda header: True)
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, (await analysis.picks(session, OWNER, 1))[0])
        await session.commit()
    beat = await client.post("/api/v1/agent/heartbeat", json={"session_id": "analyst-abc", "state": "started"})
    assert beat.status_code == 200
    agents = (await client.get(agents_url())).json()
    current = agents[0]["current"]
    assert agents[0]["status"] == "working" and current["role"] == "analyst"
    assert current["task_title"].startswith("Researching Programs Lead at")
    await client.post("/api/v1/agent/heartbeat", json={"session_id": "analyst-abc", "state": "ended"})
    assert (await client.get(agents_url())).json()[0]["status"] != "working"
    peak = (await client.get("/api/v1/missions")).json()["peak"]
    assert peak["avoid"] is True and peak["start"] == "05:00"
    off = await client.patch("/api/v1/missions/numbers", json={"values": {"avoid_peak": False}})
    assert off.json()["peak"]["avoid"] is False


async def test_the_blackout_refuses_claude_work_by_hand_and_tells_the_runner_to_stop(client, monkeypatch):
    await _setup()
    monkeypatch.setattr("app.jobsearch.routes.runner.token_ok", lambda header: True)
    monkeypatch.setattr(night, "peak_ends", lambda s, now=None: datetime.now(UTC) + timedelta(hours=2))
    async with db.session_factory()() as session:
        role_id = str((await session.scalars(select(Opportunity))).first().id)
        ids = {m.key: str(m.id) for m in (await session.scalars(select(Mission))).all()}
    packet = await client.post(f"/api/v1/roles/{role_id}/packet", json={})
    assert packet.status_code == 409 and "peak hours" in packet.json()["detail"]
    assert "uncheck" in packet.json()["detail"]
    assert (await client.post(f"/api/v1/missions/{ids[missions.SCOUT]}/run")).status_code == 409
    assert (await client.post(f"/api/v1/missions/{ids[missions.SWEEP]}/run")).status_code == 200, "no Claude in it"
    beat = await client.post("/api/v1/agent/heartbeat", json={"session_id": "writer-x", "state": "alive"})
    assert beat.json() == {"ok": True, "stop": True}
    await client.patch("/api/v1/missions/numbers", json={"values": {"avoid_peak": False}})
    assert (await client.post(f"/api/v1/roles/{role_id}/packet", json={})).status_code == 200, "unchecked, it runs"
    beat = await client.post("/api/v1/agent/heartbeat", json={"session_id": "writer-x", "state": "alive"})
    assert beat.json()["stop"] is False


def test_the_shipped_packet_template_has_no_none_for_the_letter():
    from pathlib import Path

    template = (Path(packets.__file__).parents[4] / "templates" / "jobsearch.packet.template.md").read_text("utf-8")
    letter_block = template.split("letter:", 1)[1].split("\nonce_over:", 1)[0]
    assert "none" not in letter_block.lower()
    assert "Letter(paragraphs" not in template and "none" not in packets.Letter.model_fields
