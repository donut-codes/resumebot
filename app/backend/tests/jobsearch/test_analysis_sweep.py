"""The stuck sweep and a long line for the analyst. A night's finds are all put
in line for the analyst at once, and it researches one in a few minutes, so most of a long line waits past the 120
minutes the sweep gives a role. A role nobody has taken is waiting in line, not stuck: the stuck time runs from when a
session took it (`jobs.analysis`), and a role nobody takes stays in line. What the finish time finds
still in line goes back to the research queue, not to failed. And more than one session researches at once: sessions
that ask together are handed different roles, a role whose session went silent is handed to another, and the runner is
told there is research only while a role is there that no session has. Everything is made up."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app import db
from app.agents import roster
from app.errors import Busy
from app.jobsearch.browser import reader as browser_reader
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Mission, Opportunity, SearchRun
from app.jobsearch.research import analysis
from app.jobsearch.work import hand_outs, job_queue, session_kinds
from app.jobsearch.work import hours as night
from tests.builders import analysis_setup, findings, submit_findings
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.lock_notes import NOTE, named_locks_in_the_timeline  # noqa: F401  (a fixture, used by name)
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("example_world")


async def _state(role_id) -> str | None:
    async with db.session_factory()() as session:
        return (await session.get(Opportunity, role_id)).analysis_state


async def _in_line(*names: str, links: dict[str, str] | None = None) -> dict[str, object]:
    """The roles named are put in line for the analyst at once, as a night's round does, each with a link on a site of
    its own unless `links` names one (research is never on one site in two sessions, `analysis.site_of`)."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        for name in names:
            role = await session.get(Opportunity, ids[name])
            role.url = (links or {}).get(name) or f"https://careers.{name.lower()}.example.test/jobs/1"
            await analysis.start(session, OWNER, role)
        await session.commit()
    return ids


async def test_a_role_nobody_has_taken_is_not_failed_however_long_the_line(js):
    ids = await _in_line("Fabrikam", "Initech")
    async with db.session_factory()() as session:
        now = datetime.now(UTC)
        for hours in (3, 5, 12, 23):
            assert await analysis.settle_stuck(session, OWNER, now=now + timedelta(hours=hours)) == 0, hours
        await session.commit()
    assert await _state(ids["Fabrikam"]) == await _state(ids["Initech"]) == analysis.WAITING
    # days on, it is still in line: waiting is never a failure, and it keeps its place
    async with db.session_factory()() as session:
        late = datetime.now(UTC) + timedelta(days=3)
        assert await analysis.settle_stuck(session, OWNER, now=late) == 0
        await session.commit()
    assert await _state(ids["Fabrikam"]) == await _state(ids["Initech"]) == analysis.WAITING


async def test_the_stuck_time_runs_from_when_a_session_took_the_role(js):
    ids = await _in_line("Fabrikam", "Initech")
    async with db.session_factory()() as session:
        taken = await analysis.claim_next(session, OWNER)
        assert taken.id == ids["Fabrikam"], "the one that has waited longest"
        await session.commit()
        now = datetime.now(UTC)
        assert await analysis.settle_stuck(session, OWNER, now=now + timedelta(minutes=119)) == 0, "still within it"
        assert await analysis.settle_stuck(session, OWNER, now=now + timedelta(minutes=125)) == 1
        await session.commit()
    assert await _state(ids["Fabrikam"]) == analysis.FAILED, "a session took it and never sent it"
    assert await _state(ids["Initech"]) == analysis.WAITING, "the one still in line is untouched"


async def test_a_role_taken_and_never_sent_is_handed_over_again_after_its_lease_and_failed_after_the_third_try(js):
    ids = await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        first = (await analysis.claim_next(session, OWNER)).analysis_claimed_at
        assert first is not None
        assert await analysis.claim_next(session, OWNER) is None, "the session that has it keeps it for its lease"
        # the session died: after the lease the role is free again, and each take is counted
        for taken, minutes in ((2, 21), (3, 42)):
            again = await analysis.claim_next(session, OWNER, now=first + timedelta(minutes=minutes))
            assert again is not None and again.analysis["claims"] == taken
        assert await analysis.claim_next(session, OWNER, now=first + timedelta(minutes=63)) is None, "taken 3 times"
        await session.commit()
    assert await _state(ids["Fabrikam"]) == analysis.FAILED, "no session manages to finish it: it is not handed again"


async def test_only_the_task_takes_the_role_not_reading_the_resume(js):
    ids = await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        await analysis.analysis_reply(session, OWNER, "resume")
        assert (await session.get(Opportunity, ids["Fabrikam"])).analysis_claimed_at is None
        task = await analysis.analysis_reply(session, OWNER)  # part "task" is the default
        assert task["role"]["id"] == str(ids["Fabrikam"])
        assert (await session.get(Opportunity, ids["Fabrikam"])).analysis_claimed_at is not None
        await session.commit()


async def test_a_role_put_in_line_again_starts_its_clock_over(js):
    ids = await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        role = await analysis.claim_next(session, OWNER)
        await analysis.settle_stuck(session, OWNER, now=role.analysis_claimed_at + timedelta(hours=3))
        await session.commit()
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert role.analysis_state == analysis.FAILED and role.analysis_claimed_at is not None
        assert role.analysis["claims"] == 1
        await analysis.start(session, OWNER, role)  # the owner sends it on, or the catch-up tries it again
        await session.commit()
        assert role.analysis_state == analysis.WAITING and role.analysis_claimed_at is None
        assert "claims" not in role.analysis, "a fresh try: no session has taken it yet"


async def test_a_researched_role_is_not_swept_and_a_night_of_finds_survives_the_sweep_until_the_finish_time(js):
    """The night as it went wrong: every find in line at once, a session at work on one at a time, the sweep looking
    every few minutes. Nothing a session has not taken fails; what is still in line at the finish time goes back to the
    research queue for the next night (picked again), not to failed (never picked again by itself)."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        analyse = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.ANALYSE))
        await missions.change(session, analyse, by="Tester", values={"target": -1, "finish_by": "06:00"})
        entry = await missions.start(session, OWNER, analyse, by="you")
        assert len(entry.started["roles"]) == 3 and entry.started["target"] == -1, "every find: no research bar"
        await session.commit()
    async with db.session_factory()() as session:  # the first is researched, as the analyst's session does it
        assert (await analysis.claim_next(session, OWNER)).id == ids["Fabrikam"]
        await session.commit()
    await submit_findings(findings(ids["Fabrikam"]))
    async with db.session_factory()() as session:
        assert await analysis.settle_stuck(session, OWNER, now=datetime.now(UTC) + timedelta(hours=5)) == 0
        entry = await session.scalar(select(missions.MissionRun))
        finish = night.finish_moment(entry.started_at.replace(tzinfo=UTC), "06:00") + timedelta(minutes=1)
        await missions.advance(session, OWNER, analyse, entry, False, finish)
        await session.commit()
        assert entry.outcome == "done" and entry.started["stopped"] == "finish"
        initech = await session.get(Opportunity, ids["Initech"])
        assert initech.analysis_state is None and initech.analysis_claimed_at is None, "back in the queue, not failed"
        assert [r.company for r in await analysis.picks(session, OWNER, None)] == ["Initech", "Hooli"], (
            "picked again next night")


# --- more than one session researching at once ------------------------------------------------------------------------


async def _claims_at_once(n: int) -> list[object]:
    """`n` sessions ask for a role at the same moment, each in a transaction of its own: the role each was handed."""

    async def one_session():
        async with db.session_factory()() as session:
            role = await analysis.claim_next(session, OWNER)
            await session.commit()
            return role.id if role is not None else None

    return list(await asyncio.gather(*(one_session() for _ in range(n))))


async def test_sessions_that_ask_together_are_handed_different_roles(js):
    ids = await _in_line("Fabrikam", "Initech")
    got = await _claims_at_once(3)
    assert sorted(g for g in got if g is not None) == sorted([ids["Fabrikam"], ids["Initech"]]), "a role each"
    assert got.count(None) == 1, "the third was told there is none"
    async with db.session_factory()() as session:
        assert await analysis.claim_next(session, OWNER) is None, "and they stay taken once the others are done"


async def test_research_is_never_on_one_site_in_two_sessions_the_next_role_on_another_site_goes_instead(js):
    """The owner's rule (2026-10-13): never research the same site in parallel; parallel work goes to different sites.
    Many roles' links are Exboard's, so one session at a time is on Exboard (whichever country's address), and the
    others take the roles on other sites; a role on a site in use waits, it is not doubled up."""
    links = {"Fabrikam": "https://www.board.test/jobs/view/1", "Initech": "https://uk.board.test/jobs/view/2",
             "Hooli": "https://careers.hooli.example.test/jobs/3"}
    ids = await _in_line("Fabrikam", "Initech", "Hooli", links=links)
    async with db.session_factory()() as session:
        first = await analysis.claim_next(session, OWNER)
        await session.commit()
        second = await analysis.claim_next(session, OWNER)
        await session.commit()
        assert (first.id, second.id) == (ids["Fabrikam"], ids["Hooli"]), "Initech is on Exboard: Hooli goes instead"
        assert await analysis.claim_next(session, OWNER) is None, "the only role left is on a site in use: it waits"
        assert bool(await analysis.free(session, OWNER)) is False, "so the runner is told of no research to start"
        assert await session_kinds.next_kind(session, OWNER) is None
        reply = await analysis.analysis_reply(session, OWNER)
        assert reply["task"] is None and "No role is waiting" in reply["note"], "one started anyway is told to stop"
        await session.commit()
    await submit_findings(findings(ids["Fabrikam"]))  # the session on Exboard is done: the site is free
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS
        third = await analysis.claim_next(session, OWNER)
        await session.commit()
    assert third.id == ids["Initech"] and await _state(ids["Initech"]) == analysis.WAITING


async def test_a_site_is_free_again_when_the_session_on_it_goes_silent(js):
    links = {"Fabrikam": "https://www.board.test/jobs/view/1", "Initech": "https://www.board.test/jobs/view/2"}
    ids = await _in_line("Fabrikam", "Initech", links=links)
    async with db.session_factory()() as session:
        first = await analysis.claim_next(session, OWNER)
        await session.commit()
        assert first.id == ids["Fabrikam"]
        later = first.analysis_claimed_at
        still_there = await analysis.claim_next(session, OWNER, now=later + timedelta(minutes=19))
        assert still_there is None, "Exboard is in use"
        taken = await analysis.claim_next(session, OWNER, now=later + timedelta(minutes=21))
        assert taken.id == ids["Fabrikam"], "its session is gone: the role that waited longest is taken again"


async def test_sessions_that_ask_together_are_never_on_one_site(js):
    links = {"Fabrikam": "https://www.board.test/jobs/view/1", "Initech": "https://www.board.test/jobs/view/2",
             "Hooli": "https://careers.hooli.example.test/jobs/3"}
    ids = await _in_line("Fabrikam", "Initech", "Hooli", links=links)
    got = await _claims_at_once(4)
    assert sorted(g for g in got if g is not None) == sorted([ids["Fabrikam"], ids["Hooli"]]), "one on, one off it"
    assert got.count(None) == 2


def test_a_site_is_its_host_and_a_job_board_is_one_site_whichever_country_it_is_in():
    site = analysis.site_of
    assert site("https://www.BOARD.test/jobs/view/1") == "board.test"
    assert site("https://uk.board.test/jobs/view/2?trk=x") == "board.test", "the same site, another country's"
    assert site("https://www.listings.test/viewjob?jk=1") == site("https://ca.listings.test/viewjob?jk=2")
    assert site("https://www.listings.test/viewjob?jk=1") == "listings.test"
    tenant = site("https://acme.wd5.tenantsys.test/en-US/careers/job/1")
    assert tenant == "acme.wd5.tenantsys.test", "a job system's tenant is a site of its own"
    assert site("https://other.wd5.tenantsys.test/en-US/careers/job/1") != tenant
    assert site("https://boards.feedsys.test/acme/jobs/1") == "boards.feedsys.test"
    assert site("https://jobs.employer.example/p/1", boards=["employer.example"]) == "employer.example", "the owner's"
    assert site(None) is None and site("") is None and site("   ") is None, "a role with no link is on no site"


async def test_the_tool_hands_sessions_that_call_at_the_same_moment_a_role_each(js):
    ids = await _in_line("Fabrikam", "Initech")

    async def a_session_calls_jobs_analysis():
        async with db.session_factory()() as session:
            reply = await analysis.analysis_reply(session, OWNER)
            await session.commit()
            return reply

    one, two, three = await asyncio.gather(*(a_session_calls_jobs_analysis() for _ in range(3)))
    handed = [r["role"]["id"] for r in (one, two, three) if r.get("task", 1) is not None]
    assert sorted(handed) == sorted(str(i) for i in ids.values() if i != ids["Hooli"]), "each role to one session"
    assert sum(1 for r in (one, two, three) if r.get("task", 1) is None) == 1, "the third was told to stop"


async def test_a_role_whose_session_went_silent_is_handed_to_another_after_its_lease(js):
    ids = await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        taken = await analysis.claim_next(session, OWNER)
        await session.commit()
        assert await analysis.claim_next(session, OWNER) is None, "its session has it"
        assert await analysis.claim_next(session, OWNER, now=taken.analysis_claimed_at + timedelta(minutes=19)) is None
        again = await analysis.claim_next(session, OWNER, now=taken.analysis_claimed_at + timedelta(minutes=21))
        assert again.id == ids["Fabrikam"] and again.analysis["claims"] == 2


async def test_a_role_that_cannot_be_closed_does_not_keep_the_next_from_its_session(js, monkeypatch, caplog):
    ids = await _in_line("Fabrikam", "Initech")
    async with db.session_factory()() as session:
        poisoned = await analysis.claim_next(session, OWNER)  # taken, and again and again, and never sent
        poisoned.analysis = {**poisoned.analysis, "claims": hand_outs.MAX_CLAIMS}
        poisoned.analysis_claimed_at = datetime.now(UTC) - analysis.CLAIM_LEASE - timedelta(minutes=1)
        await session.commit()

        async def cannot(*_args):
            raise RuntimeError("the history could not be written")

        monkeypatch.setattr(analysis, "_give_up", cannot)
        got = await analysis.claim_next(session, OWNER)
        await session.commit()
    assert got.id == ids["Initech"], "the session that asked is handed the next role"
    assert await _state(ids["Fabrikam"]) == analysis.FAILED, "and the one nobody can finish is closed all the same"
    assert "couldn't close the analysis" in caplog.text


async def test_the_runner_is_told_of_research_only_while_a_role_is_there_that_no_session_has(js):
    await _in_line("Fabrikam", "Initech")
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS
        first = await analysis.claim_next(session, OWNER)
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS, "the second is still in line"
        second = await analysis.claim_next(session, OWNER)
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) is None, (
            "both are being researched: no second session for either")
        first.analysis_claimed_at = datetime.now(UTC) - analysis.CLAIM_LEASE - timedelta(minutes=1)
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS, (
            "a session that went silent: its role is free")
        assert second.analysis_claimed_at is not None


async def test_the_pool_is_told_of_research_before_the_open_search_and_the_search_when_no_role_is_left(
        js, client, scout_token):
    await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        session.add(SearchRun(owner=OWNER, status="open", started_by="manual"))  # a search that is open
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS, (
            "research comes before the open search")
        await analysis.claim_next(session, OWNER)
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEARCH, (
            "a session has the one role there is")
    got = await client.get("/api/v1/agent/work", params={"only": "analysis"}, headers=scout_token)
    assert got.status_code == 422, "the pool is the only lane"
    assert (await client.get("/api/v1/agent/work", params={"only": "any"})).status_code == 401, "the scout's token"
    assert (await client.get("/api/v1/agent/work", headers=scout_token)).json()["next"] == "search"


async def test_an_analysis_lanes_session_does_not_take_the_scouts_place_on_the_page(js, client, scout_token):
    """A lane's sessions start and end beside the scout's own: the scout is a reader at work, and a lane's session
    ending must not make it look free (a job would be offered to an agent that is busy for hours), nor a lane's
    starting make it look like the analyst."""
    beat = "/api/v1/agent/heartbeat"
    started = await client.post(beat, json={"session_id": "titles-abc", "state": "started"}, headers=scout_token)
    assert started.status_code == 200

    async def scout_kind():
        async with db.session_factory()() as session:
            return (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).session_kind

    assert await scout_kind() == roster.TITLES
    lane = {"session_id": "analyst-0123456789ab", "lane": "analysis"}
    for state in ("started", "alive", "ended"):
        assert (await client.post(beat, json={**lane, "state": state}, headers=scout_token)).status_code == 200
        assert await scout_kind() == roster.TITLES, f"a lane's {state} changes nothing of the scout's session"
    async with db.session_factory()() as session:
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).last_seen_at is not None, "heard from"
    # the same session beating without a lane is the scout's own, as it always was
    await client.post(beat, json={"session_id": "analyst-0123456789ab", "state": "started"}, headers=scout_token)
    assert await scout_kind() == roster.ANALYST
    await client.post(beat, json={"session_id": "analyst-0123456789ab", "state": "ended"}, headers=scout_token)
    assert await scout_kind() is None


async def test_a_job_queues_behind_the_session_that_serves_owner_work_and_not_behind_a_lanes(js, client, scout_token):
    """What the owner asks for at the screen waits its turn behind a session that can take it, and says so
    (`job_queue.queued`); a lane's session (research, the search, the writer) is another process's, so it neither
    makes the line nor refuses the work (`job_queue.ready`)."""
    beat = "/api/v1/agent/heartbeat"

    async def asked():
        async with db.session_factory()() as session:
            return await job_queue.ready(session, OWNER), await job_queue.queued(session, OWNER)

    # The runner that serves the owner's work is up between sessions; a lane researches beside it.
    assert (await client.post(beat, json={"state": "idle"}, headers=scout_token)).status_code == 200
    lane = {"session_id": "analyst-0123456789ab", "lane": "analysis"}
    for state in ("started", "alive"):
        assert (await client.post(beat, json={**lane, "state": state}, headers=scout_token)).status_code == 200
        assert await asked() == ((True, None), None), f"a lane's {state} session is not a reason to queue anything"
    # The same runner takes a session of its own (a posting being split): that is the one the work waits behind.
    own = {"session_id": "parser-0123456789ab", "state": "started"}
    assert (await client.post(beat, json=own, headers=scout_token)).status_code == 200
    ready, queued = await asked()
    assert ready == (True, None), "busy is no reason to refuse what the owner asks for"
    assert queued is not None and "reading another posting" in queued
    # A lane's session ending or starting again changes nothing of that.
    for state in ("ended", "started"):
        await client.post(beat, json={**lane, "state": state}, headers=scout_token)
        assert "reading another posting" in (await asked())[1]


# --- a role is its session's while the session lives ----


async def _claimed_by(role_id) -> tuple[datetime | None, dict]:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        return role.analysis_claimed_at, dict(role.analysis or {})


async def test_a_role_whose_session_was_killed_is_free_in_minutes_not_after_the_lease(js):
    ids = await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        taken = await analysis.claim_next(session, OWNER, session_id="analyst-aaaaaaaaaaaa", runner="analysis-1/0001")
        await session.commit()
        t0 = taken.analysis_claimed_at
        assert taken.analysis["session"] == "analyst-aaaaaaaaaaaa" and taken.analysis["runner"] == "analysis-1/0001"
        assert analysis.SESSION_SILENCE < analysis.CLAIM_LEASE
        assert not bool(await analysis.free(session, OWNER, t0 + timedelta(minutes=2))), "heard from a moment ago"
        silent = t0 + analysis.SESSION_SILENCE + timedelta(seconds=1)
        assert bool(await analysis.free(session, OWNER, silent)), "nothing heard for the silence: its role is free"
        assert silent < t0 + analysis.CLAIM_LEASE, "well inside the lease the role used to be held for"
        again = await analysis.claim_next(session, OWNER, silent, session_id="analyst-bbbbbbbbbbbb")
        assert again.id == ids["Fabrikam"] and again.analysis["claims"] == 2
        assert again.analysis["session"] == "analyst-bbbbbbbbbbbb" and "runner" not in again.analysis


async def test_a_session_that_keeps_saying_it_is_alive_keeps_its_role_however_long_it_takes(js):
    ids = await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        taken = await analysis.claim_next(session, OWNER, session_id="analyst-aaaaaaaaaaaa")
        await session.commit()
        t0 = taken.analysis_claimed_at
        for minute in range(1, 41):  # longer than the lease, a heartbeat every minute
            await analysis.renew_claims(session, OWNER, "analyst-aaaaaaaaaaaa", t0 + timedelta(minutes=minute))
        await session.commit()
        late = t0 + timedelta(minutes=41)
        assert not bool(await analysis.free(session, OWNER, late)), "a live session keeps the role it is on"
        assert await analysis.claim_next(session, OWNER, late, session_id="analyst-bbbbbbbbbbbb") is None
        # another session's heartbeat renews nothing of this one's
        await analysis.renew_claims(session, OWNER, "analyst-bbbbbbbbbbbb", t0 + timedelta(minutes=60))
        await session.commit()
        assert bool(await analysis.free(session, OWNER, t0 + timedelta(minutes=45)))
    assert await _state(ids["Fabrikam"]) == analysis.WAITING


async def test_a_claim_with_no_session_named_still_holds_for_the_lease(js):
    await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        taken = await analysis.claim_next(session, OWNER)  # a call that names no session
        await session.commit()
        t0 = taken.analysis_claimed_at
        assert "session" not in taken.analysis
        assert not bool(await analysis.free(session, OWNER, t0 + analysis.SESSION_SILENCE + timedelta(minutes=1)))
        assert bool(await analysis.free(session, OWNER, t0 + analysis.CLAIM_LEASE + timedelta(minutes=1)))


async def test_the_heartbeat_renews_a_sessions_role_and_its_end_frees_the_role_at_once(js, client, scout_token):
    ids = await _in_line("Fabrikam")
    beat = "/api/v1/agent/heartbeat"
    me = "analyst-0123456789ab"
    async with db.session_factory()() as session:
        reply = await analysis.analysis_reply(session, OWNER, session_id=me, runner="analysis-1/0001")
        assert reply["role"]["id"] == str(ids["Fabrikam"])
        await session.commit()
    old = datetime.now(UTC) - timedelta(minutes=2)
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["Fabrikam"])).analysis_claimed_at = old
        await session.commit()
    assert (await client.post(beat, json={"session_id": me, "state": "alive", "lane": "analysis"},
                              headers=scout_token)).status_code == 200
    renewed, _ = await _claimed_by(ids["Fabrikam"])
    assert renewed is not None and renewed > old + timedelta(minutes=1), "alive: the role is held for another stretch"
    # some other session saying it is alive renews nothing of this one's
    await client.post(beat, json={"session_id": "analyst-ffffffffffff", "state": "alive"}, headers=scout_token)
    assert (await _claimed_by(ids["Fabrikam"]))[0] == renewed
    # it ends without sending the role: free now, with the try still counted
    await client.post(beat, json={"session_id": me, "state": "ended"}, headers=scout_token)
    freed, kept = await _claimed_by(ids["Fabrikam"])
    assert freed is None and kept["claims"] == 1 and "session" not in kept and "runner" not in kept
    async with db.session_factory()() as session:
        assert bool(await analysis.free(session, OWNER))
    assert await _state(ids["Fabrikam"]) == analysis.WAITING


async def test_a_session_that_sent_its_research_has_no_claim_to_free_when_it_ends(js, client, scout_token):
    ids = await _in_line("Fabrikam", "Initech")
    me, other = "analyst-0123456789ab", "analyst-111111111111"
    async with db.session_factory()() as session:
        await analysis.claim_next(session, OWNER, session_id=me)
        await session.commit()
    await submit_findings(findings(ids["Fabrikam"]))
    async with db.session_factory()() as session:
        second = await analysis.claim_next(session, OWNER, session_id=other)
        await session.commit()
    assert second.id == ids["Initech"]
    await client.post("/api/v1/agent/heartbeat", json={"session_id": me, "state": "ended"}, headers=scout_token)
    assert (await _claimed_by(ids["Initech"]))[1]["session"] == other, "the other session's role is untouched"
    assert await _state(ids["Fabrikam"]) != analysis.WAITING


async def test_a_runner_that_starts_again_frees_what_its_killed_sessions_held_and_no_one_elses(js, client, scout_token):
    await _in_line("Fabrikam", "Initech", "Hooli")
    names = ("analysis-1/aaaa0001", "analysis-2/bbbb0002", "analysis-1/cccc0003")
    got = {}
    async with db.session_factory()() as session:
        for i, runner in enumerate(names):
            role = await analysis.claim_next(session, OWNER, session_id=f"analyst-00000000000{i}", runner=runner)
            got[runner] = role.id
        await session.commit()
    beat = "/api/v1/agent/heartbeat"
    # analysis-1 starts again as "cccc0003" (a session of this start already took a role before the report arrived)
    sent = await client.post(beat, json={"session_id": "runner-start", "state": "boot", "runner": "analysis-1",
                                         "boot": "cccc0003"}, headers=scout_token)
    assert sent.status_code == 200 and sent.json()["stop"] is False
    assert (await _claimed_by(got["analysis-1/aaaa0001"]))[0] is None, "its earlier start's role is free"
    assert (await _claimed_by(got["analysis-2/bbbb0002"]))[0] is not None, "another runner's is not"
    assert (await _claimed_by(got["analysis-1/cccc0003"]))[0] is not None, "nor is one of this very start"
    async with db.session_factory()() as session:
        assert bool(await analysis.free(session, OWNER))
    # a start report that names no runner or no start changes nothing
    await client.post(beat, json={"state": "boot", "runner": "analysis-2"}, headers=scout_token)
    await client.post(beat, json={"state": "boot", "boot": "dddd0004"}, headers=scout_token)
    assert (await _claimed_by(got["analysis-2/bbbb0002"]))[0] is not None


# --- a start report the app could not take is refused, so the runner sends it again
# ---------------------------------------------

START_REPORT = {"session_id": "runner-start", "state": "boot", "runner": "analysis-1", "boot": "bbbb0002"}


async def _held_by_an_earlier_start() -> object:
    """A role a session of the runner's earlier start took, and was killed holding: its id."""
    ids = await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        await analysis.claim_next(session, OWNER, session_id="analyst-000000000000", runner="analysis-1/aaaa0001")
        await session.commit()
    return ids["Fabrikam"]


async def test_a_start_report_that_found_the_writer_busy_is_refused_and_sent_again_frees_the_roles(
        js, client, scout_token, monkeypatch):
    held = await _held_by_an_earlier_start()
    monkeypatch.setattr(db, "LIFELINE_LOCK_TIMEOUT_MS", 0)  # the heartbeat's wait for the writer's turn: none

    async def report():  # a task of its own, as a request is
        return await client.post("/api/v1/agent/heartbeat", json=START_REPORT, headers=scout_token)

    async with db.session_factory()() as writer:  # the app's own work is writing
        await writer.execute(text("select 1"))  # a writer's session: its first statement takes the turn
        refused = await asyncio.create_task(report())
        await writer.rollback()
    assert refused.status_code == 503 and refused.headers["Retry-After"], "not a 200: the runner sends it again"
    assert (await _claimed_by(held))[0] is not None, "nothing was changed"
    again = await report()
    assert again.status_code == 200 and again.json()["stop"] is False
    assert (await _claimed_by(held))[0] is None, "the same report, sent again: the earlier start's role is free"


async def test_a_start_report_refused_at_its_second_step_keeps_nothing_of_its_first(
        js, client, scout_token, monkeypatch):
    held = await _held_by_an_earlier_start()
    report = {**START_REPORT, "browser_mode": "profile"}

    async def busy(*_args):
        raise Busy("another runner is checking which browser the jobs use; nothing was changed")

    with monkeypatch.context() as patched:
        patched.setattr(browser_reader, "browser_mode_seen", busy)
        refused = await client.post("/api/v1/agent/heartbeat", json=report, headers=scout_token)
    assert refused.status_code == 503
    assert (await _claimed_by(held))[0] is not None, "the roles freed in the first step were not saved"
    again = await client.post("/api/v1/agent/heartbeat", json=report, headers=scout_token)
    assert again.status_code == 200
    assert (await _claimed_by(held))[0] is None, "the report sent again does both steps"


async def test_a_start_report_whose_steps_fail_some_other_way_is_still_answered_and_the_failures_logged(
        js, client, scout_token, monkeypatch, caplog):
    async def broken(*_args):
        raise RuntimeError("made up")

    monkeypatch.setattr(analysis, "release_runner", broken)
    monkeypatch.setattr(browser_reader, "browser_mode_seen", broken)
    sent = await client.post("/api/v1/agent/heartbeat", json={**START_REPORT, "browser_mode": "profile"},
                             headers=scout_token)
    assert sent.status_code == 200 and sent.json() == {"ok": True, "stop": False}
    assert "couldn't release the roles runner analysis-1 had" in caplog.text
    assert "couldn't note which Chrome the browser jobs use" in caplog.text


async def test_the_tool_takes_the_session_and_runner_a_call_names_in_its_headers(js, scout_token, monkeypatch):
    from types import SimpleNamespace

    from app.jobsearch.doors import scout as mcp_server
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)
    ids = await _in_line("Fabrikam", "Initech")
    headers = {"authorization": scout_token["Authorization"]}
    named = SimpleNamespace(headers={**headers, "x-session-id": "analyst-0123456789ab", "x-runner": "analysis-1/0001"})
    reply = await mcp_server.jobs_analysis(named)
    assert reply["ok"] and reply["role"]["id"] == str(ids["Fabrikam"])
    assert (await _claimed_by(ids["Fabrikam"]))[1]["session"] == "analyst-0123456789ab"
    assert (await _claimed_by(ids["Fabrikam"]))[1]["runner"] == "analysis-1/0001"
    nameless = SimpleNamespace(headers=headers)
    assert (await mcp_server.jobs_analysis(nameless))["role"]["id"] == str(ids["Initech"])
    assert "session" not in (await _claimed_by(ids["Initech"]))[1], "a call that names none holds by the lease"


# --- a take-next call with nothing to take holds nothing ------------------------------


def _locks(seen: list[str]) -> list[str]:
    """The notes of a kind's named lock being taken (the connection row has no lock of its own on SQLite)."""
    return [s for s in seen if s.startswith(NOTE)]


async def test_a_take_next_with_nothing_free_takes_no_lock_on_the_connection_row(js, transactions):
    """Every empty poll took the scout's connection row FOR UPDATE and the
    kind's advisory lock, then worked out that nothing was free. What is free is read first with no lock; with none free
    the call answers at once and holds nothing."""
    await analysis_setup()  # roles, none in line
    async with db.session_factory()() as session:
        transactions.clear()
        assert await analysis.claim_next(session, OWNER) is None
        await session.commit()
    assert transactions.timeline and not _locks(transactions.timeline)


async def test_a_take_next_with_a_role_free_still_decides_under_the_kinds_lock(js, transactions):
    ids = await _in_line("Fabrikam")
    async with db.session_factory()() as session:
        transactions.clear()
        taken = await analysis.claim_next(session, OWNER)
        await session.commit()
    assert taken is not None and taken.id == ids["Fabrikam"]
    seen = transactions.timeline
    assert any(s.startswith(NOTE) for s in seen), "the kind is decided one at a time"


async def test_a_take_next_that_names_a_claim_takes_the_locks_though_nothing_is_free(js, transactions):
    """The session started for a claim must `take` it, whatever the line holds now."""
    import uuid

    await analysis_setup()
    async with db.session_factory()() as session:
        transactions.clear()
        assert await analysis.claim_next(session, OWNER, claim_id=str(uuid.uuid4())) is None
        await session.commit()
    assert _locks(transactions.timeline), "a call that names a claim decides under the locks"


async def test_a_take_next_whose_only_role_is_on_a_site_in_use_still_waits_and_takes_no_lock(js, transactions):
    ids = await _in_line("Fabrikam", "Initech", links={"Fabrikam": "https://www.board.test/jobs/view/1",
                                                       "Initech": "https://uk.board.test/jobs/view/2",
                                                       "Hooli": "https://careers.hooli.example.test/jobs/3"})
    async with db.session_factory()() as session:
        assert (await analysis.claim_next(session, OWNER)).id == ids["Fabrikam"]
        await session.commit()
        transactions.clear()
        assert await analysis.claim_next(session, OWNER) is None, "Initech is on Exboard, in use: it waits"
        await session.commit()
    assert not _locks(transactions.timeline)
