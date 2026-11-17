"""The kinds of mission at the missions module's interface (`missions.kind_of`, `core.py`, `kinds/*.py`): what each says
of itself, one table for all of them. A mission is added by one file in `kinds/`, so what is asserted here is what any
kind must hold, not what one of them does (the missions' own behaviour is in `test_missions.py` and the tests of each
night's work). Everything is made up."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import record
from app.jobsearch.models import Mission, MissionRun
from tests.support import OWNER, load_profile

#: The kinds the module has so far; the last commit of the move makes this `record.KINDS`.
MOVED = [key for key in record.KINDS if missions.kind_of(key) is not None]


@pytest.mark.parametrize("key", MOVED)
def test_a_kind_is_found_by_its_key_and_its_defaults_are_values_its_own_check_accepts(key):
    kind = missions.kind_of(key)
    assert kind is not None and kind.key == key
    assert kind.check(dict(kind.defaults)) == kind.defaults, "what a mission starts with is what it would store"
    assert missions.check_settings(key, {}) == kind.defaults


@pytest.mark.parametrize("key", MOVED)
def test_a_kinds_first_row_is_a_schedule_the_owner_could_have_set(key):
    kind = missions.kind_of(key)
    seed = kind.first(None)
    if kind.elsewhere:  # its time is set where its session runs, so the schedule check has no word for it
        assert seed.schedule == {"kind": "outside"}
    else:
        assert missions.check_schedule(seed.schedule) == seed.schedule
    assert seed.state in ("on", "paused")


def test_the_sweep_is_the_one_kind_that_goes_first_and_is_the_app_s_own_work():
    assert [k for k in MOVED if missions.kind_of(k).early] == [record.SWEEP]
    sweep = missions.kind_of(record.SWEEP)
    assert not sweep.needs_claude and sweep.agent is None and sweep.group is None
    assert sweep.first(None).schedule == {"kind": "every", "hours": missions.EVERY_LEAST}
    assert "nobody finished" in sweep.purpose() and "{{" not in sweep.purpose()


#: What the title pass, the readers and the old stage names were called when the missions' words described them.
RETIRED_WORDS = ("title", "role tab", "reader", "Found", "the writer")


@pytest.mark.parametrize("key", MOVED)
def test_no_missions_words_describe_the_retired_title_pass_the_readers_or_the_old_stages(key):
    kind = missions.kind_of(key)
    said = " ".join(w for w in (kind.purpose(), kind.done_when(dict(kind.defaults))) if w)
    assert not [word for word in RETIRED_WORDS if word in said], said


async def test_go_gives_a_morning_up_after_its_grace_and_starts_inside_it(js):
    """The morning batch's time passed more than 75 minutes ago (the app was down then): the day is skipped. What
    `hold` says is the kind's own, and core only acts on it (`test_morning_tabs.py` runs it through the pass)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        go = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == record.GO))
        kind = missions.kind_of(record.GO)
        due_at = datetime(2026, 10, 7, 6, 30, tzinfo=UTC)
        assert await kind.hold(session, OWNER, go, due_at + timedelta(minutes=75), due_at) is None
        assert await kind.hold(session, OWNER, go, due_at + timedelta(minutes=76), due_at) == "skip"
    assert kind.group == "writer" and not kind.needs_claude and kind.agent is None
    assert kind.first(None).checked_at is not None, "it counts from now: made in the afternoon, it first runs next day"


def test_what_finishes_the_reads_night_is_said_with_its_limit_or_without():
    read = missions.kind_of(record.READ)
    assert read.done_when({"details_per_night": 150}) == "every known employer's board · up to 150 descriptions read"
    assert read.done_when({"details_per_night": -1}) == "every known employer's board · every description read"
    assert not read.needs_claude and read.agent is None and read.group is None
    assert read.first(None).schedule == {"kind": "daily", "at": "01:00"} and read.first(None).state == "on"


def test_the_mail_read_is_the_analysts_and_the_apps_own_work_and_says_its_night_in_words():
    mail = missions.kind_of(record.READ_MAIL)
    assert not mail.needs_claude, "the reading and the moving are code: peak hours never hold it"
    assert (mail.agent, mail.group) == ("analyst", "researcher")
    assert mail.done_when({}) == "your mailbox, read-only · the mail since the last read"
    seed = mail.first(None)
    assert seed.state == "paused", "paused until a mailbox is connected"
    assert seed.schedule == {"kind": "daily", "at": "05:30"} and seed.playbook == "jobsearch_read_mail"


async def test_the_browser_mission_is_started_by_the_session_on_the_owners_computer_never_by_the_app(js):
    browser = missions.kind_of(record.BROWSER)
    assert browser.elsewhere and browser.needs_claude, "its session is Claude's, so peak hours hold it"
    assert (browser.agent, browser.group, browser.defaults) == (None, "browser_reader", {})
    assert browser.done_when({}) == "every posting handed in · every new one opened for its description"
    assert browser.first(None).schedule == {"kind": "outside"} and browser.first(None).state == "paused"
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        mission = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == record.BROWSER))
        with pytest.raises(missions.MissionError, match="starts on your computer"):
            await missions.start(session, OWNER, mission, "you")
        with pytest.raises(missions.MissionError, match="set on your computer"):
            await missions.change(session, mission, by="Tester", schedule={"kind": "daily", "at": "03:00"})


def test_what_finishes_the_employer_researchs_night_is_said_with_its_limits_or_without():
    research = missions.kind_of(record.RESEARCH)
    assert research.done_when({"target": 25, "again_after_days": 90, "finish_by": "06:00"}) == (
        "25 employers researched · none again within 90 days · by 06:00")
    assert research.done_when({"target": -1, "again_after_days": 1}) == (
        "every employer not researched lately · none again within 1 day")
    assert research.needs_claude and (research.agent, research.group) == ("analyst", "researcher")
    assert research.defaults == {"target": 25, "again_after_days": 90, "finish_by": "06:45"}
    seed = research.first(None)
    assert seed.state == "paused" and seed.schedule == {"kind": "after", "mission": record.READ}
    assert seed.playbook == "jobsearch_research_employer" and seed.settings == research.defaults
    with pytest.raises(missions.MissionError, match="employers to research: 1 to 1000"):
        research.check({**research.defaults, "target": 0})
    with pytest.raises(missions.MissionError, match="research again after: 1 to 3650 days"):
        research.check({**research.defaults, "again_after_days": 0})


def test_what_finishes_the_analysis_night_is_said_with_its_limit_or_without():
    analyse = missions.kind_of(record.ANALYSE)
    assert analyse.done_when({"target": 40, "finish_by": "06:00"}) == "40 roles researched · by 06:00"
    assert analyse.done_when({"target": -1, "finish_by": "06:00"}) == "every role on deck researched · by 06:00"
    assert analyse.done_when({"target": -1}) == "every role on deck researched"
    assert analyse.needs_claude and (analyse.agent, analyse.group) == ("analyst", "researcher")
    assert analyse.defaults == {"target": -1, "finish_by": "06:45"}
    seed = analyse.first(None)
    assert seed.state == "paused" and seed.schedule == {"kind": "after", "mission": record.SCOUT}
    assert seed.playbook == "jobsearch_analyse"
    with pytest.raises(missions.MissionError, match="roles to research: 1 to 1000, or -1 for no limit"):
        analyse.check({**analyse.defaults, "target": 0})
    assert analyse.check({"target": 150, "finish_by": ""}) == {"target": 150, "finish_by": None}


def test_what_finishes_the_scouts_night_is_said_with_its_limits_or_without():
    scout = missions.kind_of(record.SCOUT)
    assert scout.done_when({"keep_target": 20, "max_sessions": 4, "finish_by": "06:00"}) == (
        "20 matches · at most 4 searches, or every site searched · by 06:00")
    assert scout.done_when({"keep_target": -1, "max_sessions": -1, "finish_by": "06:00"}) == (
        "every site searched · by 06:00")
    assert scout.done_when({"keep_target": -1, "max_sessions": 1}) == "at most 1 search, or every site searched"
    assert scout.needs_claude and (scout.agent, scout.group) == ("scout", "search")
    assert scout.defaults == {"focus": "", "keep_target": 20, "max_sessions": 4, "finish_by": "06:45"}
    assert scout.check({"focus": "  data   work ", "keep_target": 5, "max_sessions": -1, "finish_by": "6:00"}) == {
        "focus": "data work", "keep_target": 5, "max_sessions": -1, "finish_by": "06:00"}
    with pytest.raises(missions.MissionError, match="number of matches: 1 to 200, or -1 for no limit"):
        scout.check({**scout.defaults, "keep_target": 0})
    with pytest.raises(missions.MissionError, match="most searches a night: 1 to 12, or -1 for no limit"):
        scout.check({**scout.defaults, "max_sessions": 13})


def test_a_new_install_that_has_searched_before_keeps_searching_from_where_its_clock_was(monkeypatch):
    from app.settings import settings

    scout = missions.kind_of(record.SCOUT)
    monkeypatch.setattr(settings, "interval_hours", 24)
    last = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    assert (scout.first(last).state, scout.first(last).checked_at) == ("on", last), "the clock carries on"
    assert (scout.first(None).state, scout.first(None).checked_at) == ("paused", None)
    assert scout.first(last).schedule == {"kind": "daily", "at": "01:00"}
    assert scout.first(last).playbook == "jobsearch_round"
    monkeypatch.setattr(settings, "interval_hours", 0)
    assert scout.first(last).state == "paused" and scout.first(last).schedule == {"kind": "on_demand"}


# --- the table, and what the rest of the app reads of a mission -------------------------------------------------------


def test_the_table_is_whole():
    from app.jobsearch import process
    from app.jobsearch.work.session_kinds import AGENT_KINDS

    kinds = [missions.kind_of(key) for key in record.KINDS]
    assert None not in kinds, "one kind for each key (and in the page's order: `core._wired` raises when it is not)"
    assert len(set(record.KINDS)) == len(record.KINDS) == 8
    assert len({k.name for k in kinds}) == len({k.activity for k in kinds}) == 8
    assert {k.group for k in kinds} - {None} <= {a.key for a in AGENT_KINDS}
    assert {k.agent for k in kinds} <= {"scout", "analyst", None}
    assert {k.first(None).playbook for k in kinds} - {None} <= set(process.build_library().keys())
    assert {k.key for k in kinds if k.needs_claude} == {record.SCOUT, record.ANALYSE, record.RESEARCH, record.BROWSER}


async def test_ensure_makes_one_row_for_each_kind_and_a_row_of_no_kind_is_left_alone(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        assert await missions.ensure(session, OWNER) == len(record.KINDS)
        session.add(Mission(owner=OWNER, key="custom_work", name="Custom work", purpose="Mine.", settings={"n": 1},
                            schedule={"kind": "on_demand"}, state="paused"))
        await session.flush()
        assert await missions.ensure(session, OWNER) == 0
        custom = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == "custom_work"))
        assert (custom.name, custom.purpose, custom.settings) == ("Custom work", "Mine.", {"n": 1})


async def test_a_sentence_names_a_mission_by_its_work_never_by_its_title(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        for key in record.KINDS:
            kind = missions.kind_of(key)
            said = (await missions.standing(session, OWNER, key)).waits
            assert said == f"Waits: the {kind.activity} mission is paused"
            assert kind.name not in said, "a mission is named by its work, not by its title"
        assert (await missions.standing(session, OWNER, record.SCOUT)).waits == "Waits: the scouting mission is paused"
        assert (await missions.standing(session, OWNER, record.READ)).waits == "Waits: the reading mission is paused"


async def test_standing_says_what_an_install_has_made_and_never_raises(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        before = await missions.standing(session, OWNER, record.RESEARCH)
        assert (before.made, before.on, before.schedule, before.last_run_at) == (False, False, {}, None)
        assert before.settings == missions.kind_of(record.RESEARCH).defaults, "the defaults, none made"
        assert (await missions.standing(session, OWNER, "custom_work")).made is False, "a key of no kind, no error"
        await missions.ensure(session, OWNER)
        read = await missions.standing(session, OWNER, record.READ)
        assert (read.made, read.on, read.when) == (True, True, "every day at 01:00")
        assert read.settings == {"details_per_night": 150}
        research = await missions.standing(session, OWNER, record.RESEARCH)
        assert (research.made, research.on, research.when) == (True, False, "when Read the employers' boards finishes")
        mission = await missions.get(session, OWNER, record.RESEARCH)
        mission.settings = {"target": 7, "again_after_days": 30, "finish_by": None}
        await session.flush()
        again = await missions.standing(session, OWNER, record.RESEARCH)
        assert again.settings == {"target": 7, "again_after_days": 30, "finish_by": None}, "a plain merge"
        assert (again.settings["target"], again.settings["again_after_days"]) == (7, 30), "what the pool reads"
        entry = MissionRun(owner=OWNER, mission_id=mission.id, started_by="schedule", outcome="done", summary="",
                           started={}, started_at=datetime(2026, 10, 7, 1, 0, tzinfo=UTC))
        session.add(entry)
        await session.flush()
        assert (await missions.standing(session, OWNER, record.RESEARCH)).last_run_at == entry.started_at


# --- Run now: what it refuses, in what order, and what it hands back -------------------------------------------------


async def _world(session, *, peak: bool = False) -> None:
    """Every mission made, and the owner's peak hours on for the whole day or off."""
    from app.agents import roster
    from app.jobsearch.profile import store as profile_store

    await load_profile(session, OWNER, settings={"avoid_peak": False})
    await roster.ensure_roster(session, OWNER)
    if peak:
        current = (await profile_store.current(session, OWNER)).settings.model_dump(mode="json")
        await profile_store.save(session, OWNER, "settings", {**current, "avoid_peak": True, "peak_start": "00:00",
                                                              "peak_end": "23:59", "peak_zone": "UTC"}, by="test")
    await missions.ensure(session, OWNER)
    await session.commit()


async def test_run_now_refuses_peak_hours_to_the_missions_that_need_claude_then_a_second_run_then_the_computer(js):
    from app.jobsearch.browser import leases
    from app.jobsearch.work.hours import PeakHours

    async with db.session_factory()() as session:
        await _world(session, peak=True)
        for key in (record.SCOUT, record.ANALYSE, record.RESEARCH, record.BROWSER):
            with pytest.raises(PeakHours):
                await missions.run_now(session, OWNER, await missions.get(session, OWNER, key), by="you")
        sweep = await missions.get(session, OWNER, record.SWEEP)
        assert (await missions.run_now(session, OWNER, sweep, by="you")).run["outcome"] == "done", "the app's own work"
    async with db.session_factory()() as session:
        await _world(session)
        browser = await missions.get(session, OWNER, record.BROWSER)
        with pytest.raises(missions.MissionError, match="starts on your computer") as elsewhere:
            await missions.run_now(session, OWNER, browser, by="you")
        assert elsewhere.value.status == 422
        await leases.session_run(session, OWNER, "s1", datetime.now(UTC))
        with pytest.raises(missions.MissionBusy) as busy:
            await missions.run_now(session, OWNER, browser, by="you")
        assert busy.value.status == 409 and busy.value.started_by == "browser" and busy.value.run_id is not None


async def test_a_catch_up_beside_a_night_never_counts_as_the_night_running(js):
    async with db.session_factory()() as session:
        await _world(session)
        analyse = await missions.get(session, OWNER, record.ANALYSE)
        session.add(MissionRun(owner=OWNER, mission_id=analyse.id, started_by=record.IMPORT, outcome="running",
                               summary="Starting.", started={"catch_up": {}}, started_at=datetime.now(UTC)))
        await session.flush()
        ran = await missions.run_now(session, OWNER, analyse, by="you")
        assert ran.run["started_by"] == "you" and ran.run["outcome"] == "done"


async def test_a_dry_run_raises_what_a_real_one_would_and_writes_no_run(js):
    from app.jobsearch.work.hours import PeakHours

    async with db.session_factory()() as session:
        await _world(session, peak=True)
        scout = await missions.get(session, OWNER, record.SCOUT)
        with pytest.raises(PeakHours):
            await missions.run_now(session, OWNER, scout, by="claude", dry_run=True)
        sweep = await missions.get(session, OWNER, record.SWEEP)
        assert await missions.run_now(session, OWNER, sweep, by="claude", dry_run=True) is None
        assert await session.scalar(select(func.count()).select_from(MissionRun)) == 0
    async with db.session_factory()() as session:
        await _world(session)
        browser = await missions.get(session, OWNER, record.BROWSER)
        with pytest.raises(missions.MissionError, match="starts on your computer"):
            await missions.run_now(session, OWNER, browser, by="claude", dry_run=True)
        assert await session.scalar(select(func.count()).select_from(MissionRun)) == 0


async def test_run_now_hands_back_the_run_as_the_page_reads_it_and_what_to_knock(js):
    async with db.session_factory()() as session:
        await _world(session)
        scout = await missions.get(session, OWNER, record.SCOUT)
        ran = await missions.run_now(session, OWNER, scout, by="you")
        assert (ran.run["outcome"], ran.run["started_by"]) == ("running", "you")
        assert ran.wake[0] == (ran.run["search_run"], "search_started"), "the search's session is knocked for"


async def _theirs() -> bool:
    """Whether the researcher's work is the owner's now (`pool.owner_running`), as another program reads it: in a
    reader's session, which sees only what is committed."""
    from app.jobsearch.pool import core as pool

    async with db.read_session_factory()() as reader:
        return await pool.owner_running(reader, OWNER, "researcher")


async def test_the_owners_ask_is_noted_once_the_run_is_committed_and_not_if_it_is_rolled_back_or_only_released(js):
    async with db.session_factory()() as session:
        await _world(session)
        analyse = await missions.get(session, OWNER, record.ANALYSE)
        await missions.run_now(session, OWNER, analyse, by="you")
        assert not await _theirs(), "not before the commit"
        await session.rollback()
        await session.commit()
        assert not await _theirs(), "a rolled-back start asks for nothing, later commit too"
        analyse = await missions.get(session, OWNER, record.ANALYSE)
        async with session.begin_nested():
            await missions.run_now(session, OWNER, analyse, by="you")
        assert not await _theirs(), "a savepoint's release is not the commit"
        with pytest.raises(RuntimeError):
            async with session.begin_nested():
                await missions.run_now(session, OWNER, analyse, by="you")
                raise RuntimeError("the savepoint is rolled back")
        await session.commit()
        assert await _theirs(), (
            "the one released into the transaction is the owner's at the real commit (the rolled-back one is not what "
            "says so: `test_a_start_rolled_back_with_its_savepoint_alone_asks_for_nothing`)")


async def test_a_start_rolled_back_with_its_savepoint_alone_asks_for_nothing(js):
    """No other start is in the transaction, so anything noted at the real commit could only be the rolled-back one."""
    async with db.session_factory()() as session:
        await _world(session)
        analyse = await missions.get(session, OWNER, record.ANALYSE)
        assert not await _theirs()
        with pytest.raises(RuntimeError):
            async with session.begin_nested():
                await missions.run_now(session, OWNER, analyse, by="you")
                raise RuntimeError("the savepoint is rolled back")
        await session.commit()
        assert not await _theirs(), "a run that is not there is nobody's ask"
        assert await session.scalar(select(func.count()).select_from(MissionRun)) == 0


async def test_a_start_released_into_a_savepoint_that_then_rolls_back_asks_for_nothing(js):
    async with db.session_factory()() as session:
        await _world(session)
        analyse = await missions.get(session, OWNER, record.ANALYSE)
        assert not await _theirs()
        with pytest.raises(RuntimeError):
            async with session.begin_nested():
                async with session.begin_nested():
                    await missions.run_now(session, OWNER, analyse, by="you")
                raise RuntimeError("the outer savepoint is rolled back, and the run with it")
        await session.commit()
        assert not await _theirs(), "a run that is not there is nobody's ask"
        assert await session.scalar(select(func.count()).select_from(MissionRun)) == 0


async def test_a_board_read_started_in_a_savepoint_that_rolls_back_is_never_launched(js, monkeypatch):
    from app.jobsearch.missions import boards_night

    launched = []
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: launched.append(a))
    async with db.session_factory()() as session:
        await _world(session)
        read = await missions.get(session, OWNER, record.READ)
        with pytest.raises(RuntimeError):
            async with session.begin_nested():
                await missions.run_now(session, OWNER, read, by="you")
                raise RuntimeError("the savepoint is rolled back")
        await session.commit()
        assert launched == [], "the read would look for a run that is not there"
        await missions.run_now(session, OWNER, await missions.get(session, OWNER, record.READ), by="you")
        await session.commit()
        assert len(launched) == 1, "and the one that is committed is launched once"


async def test_the_route_notes_the_owners_ask_by_committing_the_same_session(client):
    async with db.session_factory()() as session:
        await _world(session)
        analyse = await missions.get(session, OWNER, record.ANALYSE)
    assert not await _theirs()
    ran = await client.post(f"/api/v1/missions/{analyse.id}/run")
    assert ran.status_code == 200 and ran.json()["started_by"] == "you"
    assert await _theirs()


# --- Claude's usage limit, and a run's mission is what says how it is read --------------------------------------------


async def test_the_usage_limit_ends_a_running_night_of_the_scout_the_analysis_and_the_research_and_no_other_run(js):
    from app.jobsearch.browser import leases
    from app.jobsearch.models import SearchRun

    async with db.session_factory()() as session:
        await _world(session)
        now = datetime.now(UTC)
        runs = {}
        for key, started, by in (
                (record.SCOUT, {"search_runs": [], "target": 20, "done": 0}, "schedule"),
                (record.ANALYSE, {"roles": [], "target": 3, "done": 0}, "schedule"),
                (record.RESEARCH, {"employers": [], "target": 3, "done": 0}, "schedule"),
                (record.READ, {"reading": True}, "schedule"),
                (record.READ_MAIL, {"mail": True}, "schedule")):
            mission = await missions.get(session, OWNER, key)
            runs[key] = MissionRun(owner=OWNER, mission_id=mission.id, started_by=by, outcome="running",
                                   summary="Going.", started=started, started_at=now)
            session.add(runs[key])
        runs[record.BROWSER] = await leases.session_run(session, OWNER, "s1", now)
        analyse = await missions.get(session, OWNER, record.ANALYSE)
        runs["catch_up"] = MissionRun(owner=OWNER, mission_id=analyse.id, started_by=record.IMPORT, outcome="running",
                                      summary="Starting.", started={"catch_up": {}}, started_at=now)
        session.add(runs["catch_up"])
        search = SearchRun(owner=OWNER, status="open", started_by="manual", started_at=now)
        session.add(search)
        await session.flush()
        assert await missions.usage_limit_hit(session, OWNER, "You have hit your limit") == 3
        for key in (record.SCOUT, record.ANALYSE, record.RESEARCH):
            assert (runs[key].outcome, runs[key].started["stopped"]) == ("done", "usage"), key
        for key in (record.READ, record.READ_MAIL, record.BROWSER, "catch_up"):
            assert runs[key].outcome == "running", f"{key} is not Claude's night"
        assert search.status != "open", "a Search now no mission started is wound down too"


async def test_a_run_is_read_by_the_kind_of_its_mission_not_by_a_marker_in_what_it_kept(js, monkeypatch):
    from app.jobsearch.browser import leases
    from app.jobsearch.missions import catch_up

    seen = []

    async def catch_up_refresh(session, entry):
        seen.append(entry.id)

    monkeypatch.setattr(catch_up, "refresh", catch_up_refresh)
    long_ago = datetime.now(UTC) - timedelta(days=1)
    async with db.session_factory()() as session:
        await _world(session)
        analyse = await missions.get(session, OWNER, record.ANALYSE)
        marked = MissionRun(owner=OWNER, mission_id=analyse.id, started_by=record.IMPORT, outcome="running",
                            summary="Starting.", started={"catch_up": {"roles": []}, "roles": [str(uuid.uuid4())]},
                            started_at=datetime.now(UTC))
        browser = await missions.get(session, OWNER, record.BROWSER)
        session.add(marked)
        first = await leases.session_run(session, OWNER, "gone", long_ago)  # sessions that last called a day ago
        second = await leases.session_run(session, OWNER, "gone-too", long_ago)
        await missions.refresh(session, marked)
        assert seen == [marked.id] and marked.outcome == "running", "a catch-up is read by its own process"
        await missions.refresh(session, first)
        assert first.outcome in ("done", "failed") and first.ended_at is not None, "a silent session ends"
        seen_by_read = await missions.detail(session, OWNER, browser.id)
        assert second.outcome == "running", "a read of the mission's history changes no row"
        outcomes = {r["id"]: r["outcome"] for r in seen_by_read["runs"]}
        assert outcomes[str(second.id)] in ("done", "failed"), "and it shows the silent session as ended"
        await missions.refresh(session, second)
        assert second.outcome in ("done", "failed"), "the schedule pass's own step ends it"


def test_the_doors_mission_run_names_every_mission_it_may_run_and_none_it_may_not():
    from app.jobsearch.owner_actions import writes
    from app.jobsearch.owner_actions.registry import REGISTRY

    does = REGISTRY["mission.run"].does
    assert writes.RUNNABLE == tuple(k for k in record.KINDS if k != record.GO)
    for key in writes.RUNNABLE:
        assert key in does, f"{key} is runnable and the description does not say so"
    assert f"never {record.GO}" in does


# --- what the page is sent of each mission ---------------------------------------------------------------------------


async def test_the_list_and_the_detail_say_who_does_each_mission_and_when_its_night_is_done(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        session.add(Mission(owner=OWNER, key="custom_work", name="Custom work", purpose="Mine.", settings={"n": 1},
                            schedule={"kind": "on_demand"}, state="paused"))
        await session.flush()
        listed = await missions.listing(session, OWNER)
        assert [m["key"] for m in listed] == [*record.KINDS, "custom_work"], "a row of no kind lists last"
        for page in listed[:-1]:
            kind = missions.kind_of(page["key"])
            assert (page["needs_claude"], page["agent"]) == (kind.needs_claude, kind.agent), page["key"]
            assert page["runs_elsewhere"] is (page["key"] == record.BROWSER), page["key"]
            assert page["done_when"] == kind.done_when(page["settings"]), page["key"]
        by_key = {m["key"]: m for m in listed}
        assert by_key[record.SCOUT]["done_when"] == (
            "20 matches · at most 4 searches, or every site searched · by 06:45")
        assert by_key[record.SWEEP]["done_when"] is None and by_key[record.GO]["done_when"] is None
        assert (by_key[record.SCOUT]["agent"], by_key[record.ANALYSE]["agent"], by_key[record.READ]["agent"]) == (
            "scout", "analyst", None)
        stray = by_key["custom_work"]
        assert (stray["needs_claude"], stray["agent"], stray["runs_elsewhere"], stray["done_when"]) == (
            True, None, False, None), "nothing says a row of no kind is the app's own work"
        detail = await missions.detail(session, OWNER, uuid.UUID(by_key[record.READ]["id"]))
        assert (detail["needs_claude"], detail["agent"], detail["runs_elsewhere"]) == (False, None, False)
        assert detail["done_when"] == by_key[record.READ]["done_when"]


async def test_a_run_as_the_page_reads_it_carries_no_packets_key_since_no_mission_writes_one(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        mission = await missions.get(session, OWNER, record.SWEEP)
        entry = MissionRun(owner=OWNER, mission_id=mission.id, started_by="schedule", outcome="done", summary="",
                           started={"packets": ["old"]}, started_at=datetime.now(UTC))
        assert "packets" not in missions.run_view(entry, record.SWEEP)
