"""A night with no limits (docs/PRD.md 25.12 and 25.13): every count limit takes -1 and nothing else below 0; with the
search limits at -1 the scout searches until every site is searched, and a limit it was given, the finish time, peak
hours or Claude's usage limit stop it; the finish time and Claude's usage limit end a night cleanly with what it found
and leave the rest for the next; each search's finds go to the analyst before the next search starts. Everything is
made up."""

import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from process_engine.models import COMPLETED, ProcInstance
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import actions, stages
from app.jobsearch.doors import tools
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import numbers as N
from app.jobsearch.missions import record
from app.jobsearch.missions import search_run as run
from app.jobsearch.missions.kinds import scout as scout_kind
from app.jobsearch.models import Employer, JobPosting, Mission, MissionRun, Opportunity, SearchRun
from app.jobsearch.postings import index as postings
from app.jobsearch.profile.schema import Settings
from app.jobsearch.research import analysis
from app.jobsearch.stages import LANDING_ZONE as LANDING
from app.jobsearch.stages import READY_TO_LAUNCH as NEEDS_DRAFTED
from app.jobsearch.stages import SECOND_IMPRESSION as FOUND
from app.jobsearch.work import hours as night
from tests.builders import (
    finds_are_sourced,
    save_documents,
)
from tests.employer_builders import make_employer, make_role
from tests.support import EXAMPLE_SYSTEM, FIVE, OWNER, POSTING_WORDS, PROFILE, answered, load_profile, reported


@pytest.fixture(autouse=True)
def the_finds_are_sourced(monkeypatch):
    """What these tests are about is what happens to a search's finds once the analyst's line is planned, so each find
    is Sourced and holds its text by then (`finds_are_sourced`)."""
    finds_are_sourced(monkeypatch)


NOW = datetime.now(UTC)
#: Claude's words when a session ends on the subscription's usage limit.
SAID = "You've hit your session limit · resets 3pm (America/Los_Angeles)"


async def _mission(session, key) -> Mission:
    return await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == key))


async def _night(*, analyse=False, settings=None, **limits_of_scout) -> None:
    """A made-up owner with the missions made, the scout's limits as given (-1 for none), and Analyse the finds on when
    asked."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, settings={**PROFILE["settings"], **(settings or {})})
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        await missions.ensure(session, OWNER)
        if limits_of_scout:
            await missions.change(session, await _mission(session, missions.SCOUT), by="Tester",
                                  values=limits_of_scout)
        if analyse:
            await missions.resume(session, await _mission(session, missions.ANALYSE), by="Tester")
        await session.commit()


def _ends(search: SearchRun, counts: dict | None = None, status: str = "done", summary: str | None = None) -> None:
    """A search over, as the scout's last report leaves it, with the counts that report put on it."""
    search.status, search.counts, search.ended_at = status, counts or {}, datetime.now(UTC)
    search.summary = summary


# --- every count limit takes -1 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("key, name, lowest", [
    (missions.SCOUT, "keep_target", 1), (missions.SCOUT, "max_sessions", 1), (missions.ANALYSE, "target", 1),
    (missions.READ, "details_per_night", 0),
])
def test_every_count_limit_of_a_mission_takes_minus_one_and_nothing_else_below_zero(key, name, lowest):
    assert missions.check_settings(key, {name: -1})[name] == -1
    assert missions.check_settings(key, {name: str(lowest + 1)})[name] == lowest + 1, "a number in its range stands"
    for below in (-2, -10, lowest - 1):
        if below == -1:  # the lowest of this limit is 0: -1 is the one number below it that is allowed
            continue
        with pytest.raises(missions.MissionError, match="or -1 for no limit"):
            missions.check_settings(key, {name: below})
    with pytest.raises(missions.MissionError, match="a whole number, or -1 for no limit"):
        missions.check_settings(key, {name: "lots"})


def test_the_defaults_a_new_install_starts_with_are_unchanged():
    assert missions.kind_of(missions.SCOUT).defaults == {
        "focus": "", "keep_target": 20, "max_sessions": 4, "finish_by": "06:45"}
    assert missions.kind_of(missions.ANALYSE).defaults == {"target": -1, "finish_by": "06:45"}, (
        "no limit: the night runs until Needs Analysis is empty")
    assert missions.kind_of(missions.GO).defaults == {}, "Go has no done conditions: the day's number is a setting"
    assert missions.kind_of(missions.READ).defaults == {"details_per_night": 150}
    assert missions.kind_of(missions.BROWSER).defaults == {}, "no posting cap and no run limit (the owner, 2026-10-16)"


def test_the_sites_a_night_take_minus_one_and_say_so_in_their_help():
    unlimited = {n.name for section in N.NUMBERS.values() for n in section if n.unlimited}
    assert unlimited == {"site_projects_per_night"}, "the other numbers are batches, bars and times"
    for name in unlimited:
        assert N.BY_NAME[name].help.endswith("-1 for no limit.")
        assert N.check({name: -1}) == {name: -1}
        assert getattr(Settings(**{name: -1}), name) == -1
        with pytest.raises(N.NumbersError, match="or -1 for no limit"):
            N.check({name: -2})
        with pytest.raises(ValueError):
            Settings(**{name: -2})
    with pytest.raises(N.NumbersError, match="Sites to learn a night: 1 to 100, or -1 for no limit"):
        N.check({"site_projects_per_night": 0})  # with none, a waiting site could never be told when it starts
    with pytest.raises(ValueError):
        Settings(site_projects_per_night=0)
    assert Settings().site_projects_per_night == missions.SITE_PROJECTS_A_NIGHT == 3
    for name in ("titles_batch", "apply_batch", "drafting_bar", "research_bar", "classifier_trim"):
        with pytest.raises(N.NumbersError):
            N.check({name: -1})
    scout = {f["name"]: f for f in N.fields(N.SCOUT, Settings(site_projects_per_night=-1))}
    assert scout["site_projects_per_night"]["unlimited"] is True and scout["site_projects_per_night"]["value"] == -1


async def test_a_limit_of_minus_one_is_saved_on_a_mission_and_a_lower_number_is_refused(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        await session.commit()
    listed = {m["key"]: m for m in (await client.get("/api/v1/missions")).json()["missions"]}
    scout = listed[missions.SCOUT]["id"]
    saved = await client.patch(f"/api/v1/missions/{scout}", json={
        "settings": {"keep_target": -1, "max_sessions": -1}, "why": "the whole site"})
    assert saved.status_code == 200 and saved.json()["settings"]["keep_target"] == -1
    assert "Keep target: 20 → -1." in saved.json()["changes"][0]["what"]
    refused = await client.patch(f"/api/v1/missions/{scout}", json={"settings": {"max_sessions": -2}})
    assert refused.status_code == 422
    assert "most searches a night: 1 to 12, or -1 for no limit" in refused.json()["detail"]
    numbers = await client.patch("/api/v1/missions/numbers", json={"values": {"site_projects_per_night": -1}})
    assert numbers.status_code == 200
    shown = {n["name"]: n for m in (await client.get("/api/v1/missions")).json()["missions"] for n in m["numbers"]}
    assert shown["site_projects_per_night"]["value"] == -1 and shown["site_projects_per_night"]["unlimited"] is True
    low = await client.patch("/api/v1/missions/numbers", json={"values": {"site_projects_per_night": -5}})
    assert low.status_code == 422 and "1 to 100, or -1 for no limit" in low.json()["detail"]


# --- the night's descriptions -----------------------------------------------------------------------------------------


async def test_descriptions_read_a_night_of_minus_one_reads_every_one(js, monkeypatch):
    seen: list[int] = []

    async def fill(session, fetch, rows, now=None):
        seen.append(len(rows))
        return len(rows)

    class Fetcher:
        async def close(self):
            return None

    monkeypatch.setattr(postings, "fill_details", fill)
    monkeypatch.setattr(polite, "make", lambda *a, **k: Fetcher())
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        employer = uuid.uuid4()
        session.add_all([JobPosting(
            owner=OWNER, employer_id=employer, ats="tenantone", ext_id=f"D-{i}", company="Fabrikam", title=f"Role {i}",
            match_key=f"fabrikam|role {i}", route="titles", first_seen=NOW, last_seen=NOW, filtered_at=NOW, links=[])
            for i in range(7)])
        await session.flush()
        for cap in (3, -1, 0):
            await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": cap})
    assert seen == [3, 7], "a cap of 0 reads none, so nothing is asked for"


async def test_descriptions_are_read_in_slices_and_the_nights_time_stops_the_read_between_them(js, monkeypatch):
    """The clock is looked at between slices of descriptions (docs/PRD.md 28.17); the rest wait for the next read."""
    seen: list[int] = []
    out_of_time = {"after": 1}

    async def fill(session, fetch, rows, now=None):
        seen.append(len(rows))
        if len(seen) == out_of_time["after"]:
            fetch.deadline = time.monotonic() - 1  # the time runs out as this slice is read
        return len(rows)

    class Fetcher:
        deadline = None

        async def close(self):
            return None

    monkeypatch.setattr(postings, "fill_details", fill)
    monkeypatch.setattr(polite, "make", lambda *a, **k: Fetcher())
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        employer = uuid.uuid4()
        session.add_all([JobPosting(
            owner=OWNER, employer_id=employer, ats="tenantone", ext_id=f"D-{i}", company="Fabrikam", title=f"Role {i}",
            match_key=f"fabrikam|role {i}", route="titles", first_seen=NOW, last_seen=NOW, filtered_at=NOW, links=[])
            for i in range(2 * actions.DETAILS_AT_A_TIME + 10)])
        await session.flush()
        every = {"owner": OWNER, "details_per_night": -1}
        size = actions.DETAILS_AT_A_TIME
        assert await actions.read_descriptions(session, None, every) == {"filled": size, "left": size + 10}
        assert seen == [size], "no second slice after the time ran out"
        seen.clear()
        out_of_time["after"] = 99  # the time does not run out
        assert await actions.read_descriptions(session, None, every) == {"filled": 2 * size + 10, "left": 0}
        assert seen == [size, size, 10]


# --- a scout night with no limit --------------------------------------------------------------------------------------


async def test_a_night_waits_while_the_read_of_the_boards_is_still_adding_postings(js):
    await _night(keep_target=-1, max_sessions=-1)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        reading = MissionRun(owner=OWNER, mission_id=(await _mission(session, missions.READ)).id, outcome="running",
                             started_by="schedule", started={"reading": True})
        session.add(reading)
        entry = await missions.start(session, OWNER, scout, by="you")
        _ends(await session.get(SearchRun, uuid.UUID(entry.started["search_run"])), {"kept": 0})
        await session.flush()
        assert await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC)) == []
        assert entry.outcome == "running", "the read of the boards has not finished: more postings may come"
        reading.outcome, reading.ended_at = "done", datetime.now(UTC)
        await session.flush()
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "done" and entry.started["stopped"] == "searched"
        assert len(entry.started["search_runs"]) == 1, "a search finds employers' boards and judges no posting"


async def test_a_search_that_failed_ends_the_night_whatever_the_limits(js):
    await _night(keep_target=-1, max_sessions=-1)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        search = await session.get(SearchRun, uuid.UUID(entry.started["search_run"]))
        _ends(search, status="failed", summary="The scout is stuck.")
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "failed" and entry.summary == "The scout is stuck.", "nothing was kept: failed"


# --- a scout night goes on while employers wait for their job boards to be found (docs/PRD.md 25.12 and 28.3) ---------


async def _unknown_boards(count: int) -> None:
    """`count` employers with a role in play whose job board the app doesn't know."""
    async with db.session_factory()() as session:
        for n in range(count):
            employer = await make_employer(session, f"Employer {n:02d}")
            session.add(make_role(employer, "Programs Lead"))
        await session.commit()


async def _the_scouts_search_ends(session, entry, *, asks: bool, status: str = "done", summary: str | None = None,
                                  answers: int = 0) -> int:
    """The open search of the night ends as the scout's session ends it (`status`: done, or failed); `asks` is whether
    that session called jobs.boards first, and `answers` how many of the employers it was handed it answered (a board
    that is not there, which is an answer too). Returns how many employers it was handed."""
    handed = (await tools.boards_reply(session, OWNER))["employers"] if asks else []
    for employer in handed[:answers]:
        await tools.board_found_reply(session, OWNER, employer["company"], not_found="no board to find")
    _ends(await session.get(SearchRun, uuid.UUID(entry.started["search_run"])), {"kept": 0}, status, summary)
    await session.flush()
    return len(handed)


async def test_a_night_starts_another_search_while_employers_still_wait_for_their_boards(js):
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        assert await _the_scouts_search_ends(session, entry, asks=True) == tools.BOARDS_ASKED
        woken = await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "running" and len(entry.started["search_runs"]) == 2, "2 employers are still to ask"
        assert [reason for _id, reason in woken] == ["search_started"]


async def test_a_night_ends_every_site_searched_once_the_searches_have_been_handed_every_employer(js):
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        await _the_scouts_search_ends(session, entry, asks=True)
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert await _the_scouts_search_ends(session, entry, asks=True) == 2, "the two the first did not take"
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "done" and entry.started["stopped"] == "searched"
        assert len(entry.started["search_runs"]) == 2


async def test_an_employer_the_scout_left_unanswered_does_not_keep_the_night_searching(js):
    """An employer the scout left unanswered over one posting does not keep the night searching."""
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(1)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        assert await _the_scouts_search_ends(session, entry, asks=True) == 1, "handed over, and never answered"
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "done" and entry.started["stopped"] == "searched"
        assert len(entry.started["search_runs"]) == 1


async def test_a_night_whose_last_search_was_handed_no_employer_it_could_have_been_ends_stalled(js):
    """PRD 25.12: a search that had work and did none of it (its sessions are failing) ends a night with no limit."""
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        await _the_scouts_search_ends(session, entry, asks=True)
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert len(entry.started["search_runs"]) == 2
        await _the_scouts_search_ends(session, entry, asks=False)
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "done" and entry.started["stopped"] == "stalled"
        assert len(entry.started["search_runs"]) == 2, "no third search for the same two employers"


async def test_a_night_searches_on_while_the_read_of_the_boards_goes_on_if_employers_wait(js):
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        session.add(MissionRun(owner=OWNER, mission_id=(await _mission(session, missions.READ)).id,
                               outcome="running", started_by="schedule", started={"reading": True}))
        entry = await missions.start(session, OWNER, scout, by="you")
        await _the_scouts_search_ends(session, entry, asks=True)
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "running" and len(entry.started["search_runs"]) == 2, "the read is no reason to wait"


async def test_one_failed_search_does_not_end_a_night_while_employers_wait_that_it_has_not_handed_out(js):
    """The scouts report no roles, so a night of lookups keeps none: one failed search of up to 32 must not end it. The
    search ran out of time after answering for 4 of the 10 employers it was handed."""
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        assert await _the_scouts_search_ends(
            session, entry, asks=True, status="failed", summary="Stuck.", answers=4) == 10
        woken = await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "running" and len(entry.started["search_runs"]) == 2, "2 employers are still to ask"
        assert [reason for _id, reason in woken] == ["search_started"]


async def test_a_night_of_lookups_is_not_failed_when_its_searches_did_their_work_though_it_kept_no_role(js):
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        await _the_scouts_search_ends(session, entry, asks=True, answers=10)
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert await _the_scouts_search_ends(session, entry, asks=True, status="failed", summary="Stuck.") == 2
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "done", "10 employers were answered for: the night did its work"
        assert entry.summary.startswith("Reached 0 matches in 2 searches (0 new roles kept); the last search failed: ")
        assert len(entry.started["search_runs"]) == 2, "none is left to ask about"


async def test_a_night_where_every_search_fails_is_failed_though_the_scout_was_handed_employers_in_each(js):
    """Being handed an employer is the app's doing, not the scout's work: a scout that asks for the employers and then
    dies, search after search, has done nothing, so the night is failed at the first (not after every employer)."""
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        assert await _the_scouts_search_ends(session, entry, asks=True, status="failed", summary="Stuck.") == 10
        assert await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC)) == []
        assert entry.outcome == "failed" and entry.summary == "Stuck."
        assert len(entry.started["search_runs"]) == 1, "no second search for a scout that answers nothing"


async def test_a_search_that_fails_without_being_handed_an_employer_does_not_go_round_again(js):
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        await _the_scouts_search_ends(session, entry, asks=True, answers=10)
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert await _the_scouts_search_ends(session, entry, asks=False, status="failed", summary="Stuck.") == 0
        assert await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC)) == []
        assert entry.outcome == "done" and "the last search failed: Stuck" in entry.summary
        assert len(entry.started["search_runs"]) == 2, "its 2 employers were not handed to it: no third search"


async def test_a_first_search_that_fails_handed_no_employer_fails_the_night_and_starts_no_other(js):
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        await _the_scouts_search_ends(session, entry, asks=False, status="failed", summary="The scout is stuck.")
        assert await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC)) == []
        assert entry.outcome == "failed" and entry.summary == "The scout is stuck."
        assert len(entry.started["search_runs"]) == 1


async def test_a_failed_search_that_reaches_the_target_ends_the_night_though_employers_wait(js, monkeypatch):
    await _night(keep_target=1, max_sessions=-1)
    await _unknown_boards(12)

    async def reached(session, search_run_ids):
        return 1

    monkeypatch.setattr(scout_kind, "matches", reached)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        await _the_scouts_search_ends(session, entry, asks=True, status="failed", summary="Stuck.")
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "done" and len(entry.started["search_runs"]) == 1, "the target is met: no more"


async def test_a_first_search_handed_none_of_the_employers_that_waited_ends_the_night_stalled(js):
    """A first search is held to the same stop as any other, now that each search says who waited at its start."""
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(12)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        await _the_scouts_search_ends(session, entry, asks=False)
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "done" and entry.started["stopped"] == "stalled"
        assert len(entry.started["search_runs"]) == 1


async def test_a_search_is_not_stalled_when_the_employers_that_waited_got_a_job_system_another_way(js):
    """The employers that waited when the search began got a job system before the scout asked, so it was handed none
    (it asked and there were none); one that arrived after it asked is not one it could have been handed."""
    await _night(keep_target=-1, max_sessions=-1)
    await _unknown_boards(13)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        await _the_scouts_search_ends(session, entry, asks=True)
        await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert len(entry.started["search_runs"]) == 2, "3 employers wait as the second search begins"
        for employer in (await session.scalars(select(Employer).where(Employer.owner == OWNER))).all():
            employer.ats = EXAMPLE_SYSTEM
        await session.flush()
        assert await _the_scouts_search_ends(session, entry, asks=True) == 0, "nothing was left to hand it"
        late = await make_employer(session, "Latecomer")
        session.add(make_role(late, "Programs Lead"))
        await session.flush()
        woken = await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC))
        assert entry.outcome == "running" and len(entry.started["search_runs"]) == 3, "the latecomer is still to ask"
        assert [reason for _id, reason in woken] == ["search_started"]


# --- the finish time and Claude's usage limit end a night cleanly -----------------------------------------------------


def _roles_for_the_scout() -> list[dict]:
    return [reported("Fabrikam", "Programs Lead", fit_breakdown=answered("met", "met", "not")),
            reported("Initech", "Programs Lead", fit_breakdown=answered("met", "partly", "not"))]


async def _open_night_with_roles_reported(**limits_of_scout):
    """A scout night whose first search is open at the scout's step with two roles staged and not yet stored."""
    await _night(**limits_of_scout)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        search = await session.get(SearchRun, uuid.UUID(entry.started["search_run"]))
        await run.stage_report(session, search, _roles_for_the_scout(), done=False)
        assert search.status == "open" and len(search.reported) == 2
        await session.commit()
        return entry.id, search.id


async def test_the_finish_time_winds_down_the_open_search_keeping_the_roles_its_readers_reported(js):
    entry_id, search_id = await _open_night_with_roles_reported(keep_target=-1, max_sessions=-1)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await session.get(MissionRun, entry_id)
        later = night.finish_moment(entry.started_at.replace(tzinfo=UTC), "06:45") + timedelta(minutes=1)
        assert await missions.advance(session, OWNER, scout, entry, False, later) == []
        assert entry.outcome == "done" and entry.started["stopped"] == "finish"
        assert entry.summary == "Reached 0 matches in 1 search (2 new roles kept). Stopped: the finish time."
        search = await session.get(SearchRun, search_id)
        assert search.status == "done" and search.counts["stopped"] == "finish" and search.counts["kept"] == 2
        assert "Stopped: the finish time. What it found was kept" in search.summary
        stored = (await session.scalars(select(Opportunity).where(Opportunity.source_run == search_id))).all()
        assert sorted(r.company for r in stored) == ["Fabrikam", "Initech"] and {r.status for r in stored} == {LANDING}
        assert (await session.get(ProcInstance, search.instance_id)).status == COMPLETED
        assert await run.open_run(session, OWNER) is None, "nothing is left open to wake the scout for"


async def test_claudes_usage_limit_ends_the_night_cleanly_keeping_what_was_found(js):
    entry_id, search_id = await _open_night_with_roles_reported(keep_target=-1, max_sessions=-1)
    async with db.session_factory()() as session:
        assert await missions.usage_limit_hit(session, OWNER, SAID) == 1
        entry = await session.get(MissionRun, entry_id)
        assert entry.outcome == "done" and entry.started["stopped"] == "usage"
        assert entry.summary == ("Reached 0 matches in 1 search (2 new roles kept). "
                                 f"Claude said: \"{SAID}\". Stopped: Claude's usage limit.")
        assert missions.run_view(entry, missions.SCOUT)["stopped"] == "Claude's usage limit"
        search = await session.get(SearchRun, search_id)
        assert search.status == "done" and search.counts["stopped"] == "usage"
        assert len((await session.scalars(select(Opportunity).where(Opportunity.source_run == search_id))).all()) == 2
        assert await run.open_run(session, OWNER) is None
        assert await missions.usage_limit_hit(session, OWNER, SAID) == 0, "nothing running: nothing more to end"


async def test_a_session_that_ended_on_the_usage_limit_ends_the_night_through_the_heartbeat(client, monkeypatch):
    monkeypatch.setattr("app.jobsearch.routes.runner.token_ok", lambda header: True)
    entry_id, search_id = await _open_night_with_roles_reported(keep_target=-1, max_sessions=-1)
    alive = await client.post("/api/v1/agent/heartbeat", json={"session_id": "reader-xyz", "state": "alive",
                                                              "usage_limit": SAID})
    assert alive.status_code == 200
    async with db.session_factory()() as session:
        assert (await session.get(MissionRun, entry_id)).outcome == "running", "only an ended session says it"
    ended = await client.post("/api/v1/agent/heartbeat", json={"session_id": "reader-xyz", "state": "ended",
                                                              "usage_limit": SAID})
    assert ended.status_code == 200 and ended.json() == {"ok": True, "stop": False}
    async with db.session_factory()() as session:
        entry = await session.get(MissionRun, entry_id)
        assert entry.outcome == "done" and entry.started["stopped"] == "usage" and SAID in entry.summary
        search = await session.get(SearchRun, search_id)
        assert search.status == "done" and search.counts["stopped"] == "usage"
        assert len((await session.scalars(select(Opportunity).where(Opportunity.source_run == search_id))).all()) == 2


async def test_a_session_that_ended_for_any_other_reason_keeps_what_it_reported_and_the_night_goes_on(
        client, monkeypatch):
    monkeypatch.setattr("app.jobsearch.routes.runner.token_ok", lambda header: True)
    entry_id, search_id = await _open_night_with_roles_reported(keep_target=-1, max_sessions=-1)
    await client.post("/api/v1/agent/heartbeat", json={"session_id": "scout-abc", "state": "alive"})
    ended = await client.post("/api/v1/agent/heartbeat", json={"session_id": "scout-abc", "state": "ended"})
    assert ended.status_code == 200
    async with db.session_factory()() as session:
        assert (await session.get(SearchRun, search_id)).status == "done", "what it reported is kept"
        entry = await session.get(MissionRun, entry_id)
        assert entry.outcome == "running" and not (entry.started or {}).get("stopped"), "the night is not over"


async def test_a_search_with_nothing_reported_that_hits_the_usage_limit_ends_done_not_failed(js):
    await _night(keep_target=-1, max_sessions=-1)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        search = await session.get(SearchRun, uuid.UUID(entry.started["search_run"]))
        await missions.usage_limit_hit(session, OWNER, SAID)
        assert entry.outcome == "done" and entry.started["stopped"] == "usage"
        assert search.status == "done" and search.summary.startswith("The scout reported no roles.")


async def test_a_manual_search_is_wound_down_by_the_usage_limit_too(js):
    await _night(analyse=True)
    async with db.session_factory()() as session:
        search = await run.start_round(session, OWNER, started_by="manual")
        await run.stage_report(session, search, _roles_for_the_scout(), done=False)
        await session.commit()
        assert await missions.usage_limit_hit(session, OWNER, SAID) == 0, "no mission ran it"
        assert search.status == "done" and len((await session.scalars(select(Opportunity))).all()) == 2
        assert (await session.get(ProcInstance, search.instance_id)).status == COMPLETED, (
            "cut short, so none of its finds went to the analyst")
        assert await analysis.round_waiting(session, search.id) == 0


async def test_the_research_that_follows_a_night_does_not_start_after_the_usage_limit(js):
    await _night(analyse=True, keep_target=-1, max_sessions=-1)
    async with db.session_factory()() as session:
        scout, analyse = await _mission(session, missions.SCOUT), await _mission(session, missions.ANALYSE)
        done = await missions.start(session, OWNER, scout, by="you")
        await missions.usage_limit_hit(session, OWNER, SAID)
        assert done.outcome == "done" and not await missions.due(session, analyse), "the analyst would hit it too"
        again = await missions.start(session, OWNER, scout, by="you")
        _ends(await session.get(SearchRun, uuid.UUID(again.started["search_run"])), {"kept": 0})
        await missions.advance(session, OWNER, scout, again, False, datetime.now(UTC))
        assert again.outcome == "done" and again.started["stopped"] == "searched"
        assert await missions.due(session, analyse), "any other end is followed"


async def test_a_research_or_packets_run_running_when_the_limit_is_hit_sends_back_what_nobody_began(js):
    await _night(analyse=True)
    async with db.session_factory()() as session:
        for company, fit in (("Fabrikam", 4), ("Initech", 3)):
            session.add(Opportunity(
                description=POSTING_WORDS,
                owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", fit=fit,
                                    origin="scout", match_key=f"{company.lower()}|programs lead", status=FOUND,
                                    found=NOW.date()))
        await session.flush()
        analyse = await _mission(session, missions.ANALYSE)
        entry = await missions.start(session, OWNER, analyse, by="you")
        assert len(entry.started["roles"]) == 2
        assert await missions.usage_limit_hit(session, OWNER, SAID) == 1
        assert entry.outcome == "done" and entry.started["stopped"] == "usage"
        assert entry.summary.startswith("Got to 0") and entry.summary.endswith("Stopped: Claude's usage limit.")
        roles = (await session.scalars(select(Opportunity))).all()
        assert {r.analysis_state for r in roles} == {None}, "back in the queue for the next night"


# --- each search's finds are researched straight away -----------------------------------------------------------------


async def _search_and_report(roles: list[dict]) -> uuid.UUID:
    """A manual search that reports `roles` and is done, so its process runs to the end of the round."""
    async with db.session_factory()() as session:
        search = await run.start_round(session, OWNER, started_by="manual")
        await run.stage_report(session, search, roles, done=True)
        await session.commit()
        return search.id


def _finds() -> list[dict]:
    return [reported("Fabrikam", "Programs Lead", fit_breakdown=answered("met", "met", "not")),
            reported("Initech", "Programs Lead", fit_breakdown=answered("met", "partly", "not")),
            reported("Hooli", "Programs Lead", fit_breakdown=answered("met", "not", "not"))]


@pytest.fixture
def pages_with_text(monkeypatch):
    """The employers' own pages show the whole posting: the app reads each find's text in code before the analyst is
    sent it, and a role with none is never researched (docs/PRD.md 39.30)."""
    page = "<html><body>" + "".join(f"<p>{line}</p>" for line in POSTING_WORDS.splitlines() if line) + "</body></html>"

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0))


async def _role(session, company) -> Opportunity:
    return await session.scalar(select(Opportunity).where(Opportunity.owner == OWNER, Opportunity.company == company))


async def test_with_analyse_the_finds_on_a_searchs_finds_land_in_landing_zone_and_wait_for_deck(js, pages_with_text):
    """Sonnet reads the roles on deck only (the simplified statuses): a new find is never put in front of the analyst by
    the search that found it, so the round ends at once, and the pipeline moves the find on."""
    await _night(analyse=True)
    search_id = await _search_and_report(_finds())
    async with db.session_factory()() as session:
        search = await session.get(SearchRun, search_id)
        assert search.status == "done"
        instance = await session.get(ProcInstance, search.instance_id)
        assert instance.status == COMPLETED and instance.data["analysing"] == {"on": True, "waiting": 0}
        for company in ("Fabrikam", "Initech", "Hooli"):
            found = await _role(session, company)
            assert (found.status, found.analysis_state) == (LANDING, None), company
        assert await analysis.round_waiting(session, search_id) == 0
        assert await analysis.picks(session, OWNER, None) == [], "none is on deck: the analyst has no find to take"
        assert await analysis.settle_rounds(session, OWNER) == 0


async def test_with_analyse_the_finds_paused_the_search_ends_at_once_and_its_finds_stay_where_they_landed(js):
    await _night(analyse=False)
    search_id = await _search_and_report(_finds())
    async with db.session_factory()() as session:
        search = await session.get(SearchRun, search_id)
        instance = await session.get(ProcInstance, search.instance_id)
        assert instance.status == COMPLETED and instance.data["analysing"] == {"on": False, "waiting": 0}
        fabrikam = await _role(session, "Fabrikam")
        assert (fabrikam.status, fabrikam.analysis_state) == (LANDING, None)
        analyse = await _mission(session, missions.ANALYSE)
        assert analyse.state == "paused"
        assert await analysis.picks(session, OWNER, None) == [], "the mission's own run reads the roles on deck only"


async def _odds(**probs) -> None:
    """The postings the finds were made from, rated by the classifier: `_odds(Fabrikam=0.2)` is Fabrikam's, 20% sure."""
    async with db.session_factory()() as session:
        for number, (company, prob) in enumerate(probs.items()):
            employer = await employers.find_or_make(session, OWNER, company)
            session.add(JobPosting(
                owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=f"P-{number}", company=company,
                title="Programs Lead", match_key=match_key(company, "Programs Lead"),
                url=f"https://jobs.example.test/P-{number}", text="Text.", first_seen=NOW, last_seen=NOW, links=[],
                fit_prob=prob))
        await session.commit()


async def test_no_find_is_sent_in_peak_hours_or_for_a_search_that_was_cut_short(js, monkeypatch):
    await _night(analyse=True, settings={"avoid_peak": True})
    with monkeypatch.context() as peak:
        peak.setattr(night, "in_peak", lambda s, now=None: True)
        search_id = await _search_and_report(_finds())
    async with db.session_factory()() as session:
        instance = await session.get(ProcInstance, (await session.get(SearchRun, search_id)).instance_id)
        assert instance.status == COMPLETED and instance.data["analysing"] == {"on": False, "waiting": 0}
        assert (await _role(session, "Fabrikam")).analysis_state is None

    async with db.session_factory()() as session:
        cut = await run.start_round(session, OWNER, started_by="manual")
        await run.stage_report(session, cut, [reported("Wingtip", "Programs Lead",
                                                       fit_breakdown=answered("met", "met", "not"))], done=False)
        assert await run.wind_down(session, cut, "finish", "the finish time") == "kept"
        await session.commit()
        instance = await session.get(ProcInstance, cut.instance_id)
        assert instance.status == COMPLETED and instance.data["analysing"] == {"on": False, "waiting": 0}
        wingtip = await _role(session, "Wingtip")
        assert wingtip.status == LANDING and wingtip.analysis_state is None, "kept where it landed"


async def test_sending_the_finds_to_the_analyst_never_costs_the_search_its_roles(js, monkeypatch):
    await _night(analyse=True)

    async def boom(session, owner, run_id):
        raise RuntimeError("the analyst's queue broke")

    monkeypatch.setattr(analysis, "plan_round", boom)
    search_id = await _search_and_report(_finds())
    async with db.session_factory()() as session:
        search = await session.get(SearchRun, search_id)
        assert search.status == "done" and (await session.get(ProcInstance, search.instance_id)).status == COMPLETED
        assert len((await session.scalars(select(Opportunity).where(Opportunity.source_run == search_id))).all()) == 3


async def test_the_scout_counts_only_the_roles_that_reached_ready_to_launch_as_its_matches(js, pages_with_text):
    """The matches are the roles of the night's searches that reached Ready to Launch or later (`missions.MATCHED`): a
    find still in Landing Zone is not one, so a night whose searches are done ends with the finds kept and no match."""
    await _night(analyse=True, keep_target=1, max_sessions=-1)
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        search = await session.get(SearchRun, uuid.UUID(entry.started["search_run"]))
        await run.stage_report(session, search, _finds(), done=True)
        await session.commit()
        assert await scout_kind.matches(session, [str(search.id)]) == 0, "three finds, none a match yet"
        await tools.boards_reply(session, OWNER)  # the scout's session was handed the finds' employers, as it is asked
        assert await missions.advance(session, OWNER, scout, entry, False, datetime.now(UTC)) == []
        assert entry.outcome == "done" and entry.started["stopped"] == "searched"
        assert entry.summary == "Reached 0 of 1 matches in 1 search (3 new roles kept). Stopped: every site searched."
        (await _role(session, "Fabrikam")).status = NEEDS_DRAFTED  # the pipeline moved it up to Ready to Launch
        (await _role(session, "Initech")).status = stages.APPLIED
        await session.flush()
        assert await scout_kind.matches(session, [str(search.id)]) == 2, "Ready to Launch and later are matches"


# --- Analyse the finds with no limit ----------------------------------------------------------------------------------


async def test_a_research_run_with_no_limit_researches_every_find_and_is_not_called_target_met(js):
    from tests.builders import analysis_setup

    await analysis_setup()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        analyse = await _mission(session, missions.ANALYSE)
        await missions.change(session, analyse, by="Tester", values={"target": 1})
        one = await missions.start(session, OWNER, analyse, by="you")
        assert len(one.started["roles"]) == 1, "a limit of one: the best find only"
        one.outcome = "done"
        for role in (await session.scalars(select(Opportunity))).all():
            role.analysis_state = None  # back in the queue
        await missions.change(session, analyse, by="Tester", values={"target": -1})
        every = await missions.start(session, OWNER, analyse, by="you")
        assert len(every.started["roles"]) == 3 and every.started["target"] == -1, "every find: no research bar"
        assert missions.run_view(every, missions.ANALYSE)["progress"] is None
    assert record.stop_for({"target": -1}, 9) == "empty" and record.stop_for({"target": 2}, 2) == "target"
    assert record.stop_for({"target": 5}, 2) == "empty"
