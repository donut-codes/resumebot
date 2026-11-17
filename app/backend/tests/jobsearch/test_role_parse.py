"""Fill from a posting (What to look for, inside a target role): the owner pastes a job posting and a small session
splits it into the role's fields. Whether an agent can take the work now, the request and its checks, the job's states
and timeouts, the runner being handed the work first, the two tools, and the runner's idle heartbeat. Synthetic text
only."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db
from app.agents import roster
from app.harness import instructions as files
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.main import app
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import ParseJob
from app.jobsearch.work import hours as night
from app.jobsearch.work import housekeeping, job_queue, session_kinds, wake
from app.settings import settings
from tests.builders import (
    MINUTE,
    POSTING_FIELDS,
    POSTING_TEXT,
    api_url,
    parse_get,
    parse_job,
    parse_row,
    parse_runner,
    parse_sweep,
    settle_housekeeping,
)
from tests.support import AUTH, OWNER, TOKEN, load_profile

#: A fixed moment outside every peak window, for the checks that take a clock.
NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
#: 6:00 Pacific on a summer day: inside the default peak window.
IN_PEAK = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
#: What a job asked for while the analyst researches a role says it waits behind (the one session whose length is told).
QUEUED_BEHIND_RESEARCH = ("Queued: the agent is busy researching a role, which takes about 3 minutes. "
                          "This starts as soon as it has finished.")


@pytest.fixture
async def client(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def door():
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


async def _ready(now=None):
    async with db.session_factory()() as session:
        return await role_parse.ready(session, OWNER, now)


async def _queued(now=None):
    async with db.session_factory()() as session:
        return await job_queue.queued(session, OWNER, now)


async def _count() -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(ParseJob))


# --- whether an agent can take the work now ----------


async def test_ready_needs_a_runner_that_checked_in_lately(js):
    assert await _ready(NOW) == (False, "No agent has checked in yet.")
    await parse_runner(now=NOW)
    assert await _ready(NOW) == (False, "No agent has checked in yet."), "a roster row with no stamp is no one"
    await parse_runner(seen=3 * 60 * MINUTE + MINUTE, now=NOW)
    assert await _ready(NOW) == (False, "No agent has checked in for 3 hours.")
    await parse_runner(seen=45 * MINUTE, now=NOW)
    assert await _ready(NOW) == (False, "No agent has checked in for 45 minutes.")
    await parse_runner(seen=60 * MINUTE, now=NOW)
    assert await _ready(NOW) == (False, "No agent has checked in for 1 hour.")
    await parse_runner(seen=30 * 60 * 24 * MINUTE, now=NOW)
    assert await _ready(NOW) == (False, "No agent has checked in for 30 days.")
    await parse_runner(seen=MINUTE, now=NOW)
    assert await _ready(NOW) == (True, None), "a call a minute ago"


async def test_any_sign_of_life_counts_as_checked_in_the_idle_heartbeat_and_what_it_declared_included(js):
    await parse_runner(seen=5 * 60 * MINUTE, checked_in=MINUTE, now=NOW)
    assert (await _ready(NOW))[0] is True, "the runner's idle heartbeat"
    await parse_runner(seen=5 * 60 * MINUTE, declared=2 * MINUTE, now=NOW)
    assert (await _ready(NOW))[0] is True, "it declared itself on start"
    await parse_runner(seen=9 * MINUTE, now=NOW)
    assert (await _ready(NOW))[0] is True
    await parse_runner(seen=11 * MINUTE, now=NOW)
    assert (await _ready(NOW))[0] is False, "ten minutes is as long as a silent runner counts"


async def test_a_paused_agent_cannot_take_it_a_busy_one_queues_it_and_a_session_that_never_ended_is_not_busy(js):
    await parse_runner(seen=MINUTE, paused=True, now=NOW)
    assert await _ready(NOW) == (False, "The agent is paused: it is handed no work until you resume it.")
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="scout", now=NOW)
    assert await _ready(NOW) == (True, None), "busy is no reason to refuse what the owner asks for: it waits its turn"
    assert await _queued(NOW) == "Queued: the agent is busy with a search. This starts as soon as it has finished."
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="parser", now=NOW)
    assert await _ready(NOW) == (True, None) and "busy reading another posting" in await _queued(NOW)
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="analyst", now=NOW)
    assert await _ready(NOW) == (True, None)
    assert await _queued(NOW) == QUEUED_BEHIND_RESEARCH, "a role's research is the one session whose length is told"
    # The kind is set but its session stopped saying it is alive (the runner died): not busy, only quiet.
    await parse_runner(seen=MINUTE, checked_in=30 * MINUTE, kind="reader", now=NOW)
    assert await _ready(NOW) == (True, None) and await _queued(NOW) is None
    await parse_runner(seen=MINUTE, checked_in=MINUTE, now=NOW)  # between two sessions
    assert await _ready(NOW) == (True, None) and await _queued(NOW) is None


async def test_ready_is_false_in_claudes_peak_hours_and_says_until_when_without_the_missions_hint(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={})  # the default: peak hours avoided
        await session.commit()
    await parse_runner(seen=MINUTE, now=IN_PEAK)
    ok, why = await _ready(IN_PEAK)
    assert ok is False and why.startswith("Claude's peak hours: no Claude work until ") and why.endswith(".")
    assert "uncheck" not in why and "Missions" not in why, "one plain sentence"
    await parse_runner(seen=MINUTE, now=NOW)
    assert (await _ready(NOW))[0] is True, "outside the window"
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={"avoid_peak": False})
        await session.commit()
    await parse_runner(seen=MINUTE, now=IN_PEAK)
    assert (await _ready(IN_PEAK))[0] is True, "unticked, the work goes on"


async def test_the_ready_route_says_ready_or_why_not(client, monkeypatch):
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": False, "why": "No agent has checked in yet."}
    await parse_runner(seen=MINUTE)
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": True, "why": None}
    await parse_runner(seen=2 * 60 * MINUTE + MINUTE)
    assert (await client.get("/api/v1/agent/ready")).json() == {
        "ready": False, "why": "No agent has checked in for 2 hours."}
    await parse_runner(seen=MINUTE)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={})
        await session.commit()
    monkeypatch.setattr(night, "peak_ends", lambda s, now=None: datetime.now(UTC) + timedelta(hours=2))
    got = (await client.get("/api/v1/agent/ready")).json()
    assert got["ready"] is False and got["why"].startswith("Claude's peak hours: no Claude work until ")


# --- the request ----------


async def test_a_posting_must_be_at_least_50_characters_and_has_no_most(client):
    await parse_runner(seen=MINUTE)
    for text in ("", "x" * 49, " " * 60, " " * 30 + "x" * 49 + " " * 30):
        assert (await client.post("/api/v1/profile/parse", json={"text": text})).status_code == 422, len(text)
    too_long = await client.post("/api/v1/profile/parse", json={"text": POSTING_TEXT, "role_id": "r" * 81})
    assert too_long.status_code == 422
    assert (await client.post("/api/v1/profile/parse", json={})).status_code == 422
    assert await _count() == 0
    for text in ("x" * 50, "x" * 20000, "x" * 20001, "x" * 300_000, "  " + "x" * 50 + "  "):
        res = await client.post("/api/v1/profile/parse", json={"text": text})
        assert res.status_code == 200 and set(res.json()) == {"id"}, len(text)
    assert await _count() == 5


async def test_a_request_makes_a_waiting_job_and_wakes_the_runner_after_the_commit(client, monkeypatch):
    knocked = []

    async def knock(key, reason=wake.SEARCH_STARTED):
        async with db.session_factory()() as session:  # the job is already saved when the runner is knocked for
            knocked.append((key, reason, await session.get(ParseJob, uuid.UUID(key))))
        return True

    monkeypatch.setattr(wake, "knock", knock)
    await parse_runner(seen=MINUTE)
    res = await client.post("/api/v1/profile/parse", json={"text": f"  {POSTING_TEXT}  ", "role_id": "ops"})
    assert res.status_code == 200
    job_id = res.json()["id"]
    assert [(k, r) for k, r, _ in knocked] == [(job_id, "parse_asked")] and knocked[0][2] is not None
    row = await parse_row(job_id)
    assert (row.owner, row.state, row.role_id, row.text) == (OWNER, "waiting", "ops", POSTING_TEXT.strip())
    assert row.taken_at is None and row.fields is None and row.error is None


async def test_a_request_is_refused_with_why_when_no_agent_can_take_it_and_nothing_is_kept(client, monkeypatch):
    knocked = []

    async def knock(key, reason=wake.SEARCH_STARTED):
        knocked.append(key)
        return True

    monkeypatch.setattr(wake, "knock", knock)
    await parse_runner(seen=3 * 60 * MINUTE)
    res = await client.post("/api/v1/profile/parse", json={"text": POSTING_TEXT})
    assert res.status_code == 409
    why = "No agent has checked in for 3 hours."
    assert res.json() == {"detail": why, "why": why}
    await parse_runner(seen=MINUTE, paused=True)
    assert "paused" in (await client.post("/api/v1/profile/parse", json={"text": POSTING_TEXT})).json()["why"]
    assert await _count() == 0 and knocked == []


async def test_a_request_made_while_the_agent_is_busy_is_queued_and_the_runner_is_woken_for_it(client, monkeypatch):
    knocked = []

    async def knock(key, reason=wake.SEARCH_STARTED):
        knocked.append((key, reason))
        return True

    monkeypatch.setattr(wake, "knock", knock)
    for kind in ("titles", "analyst", "scout"):
        await parse_runner(seen=MINUTE, checked_in=MINUTE, kind=kind)
        res = await client.post("/api/v1/profile/parse", json={"text": POSTING_TEXT})
        assert res.status_code == 200 and set(res.json()) == {"id"}, kind
        assert knocked[-1] == (res.json()["id"], "parse_asked"), "woken, so the runner folds it into its next run"
    assert await _count() == 3
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": True, "why": None}


async def test_a_job_waiting_behind_a_session_says_it_is_queued_until_the_agent_is_between_sessions(client):
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="analyst")
    job_id = (await client.post("/api/v1/profile/parse", json={"text": POSTING_TEXT, "role_id": "ops"})).json()["id"]
    queued = (await parse_get(client, job_id)).json()
    assert queued == {"id": job_id, "state": "waiting", "step": QUEUED_BEHIND_RESEARCH, "role_id": "ops"}
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="titles")
    assert "busy judging titles" in (await parse_get(client, job_id)).json()["step"]
    await parse_runner(seen=MINUTE, checked_in=MINUTE)  # the session ended; the runner is between sessions
    assert (await parse_get(client, job_id)).json()["step"] == "Waiting for an agent"
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="parser")
    reading = await parse_job(role_parse.READING, taken_ago=MINUTE)
    assert (await parse_get(client, reading)).json()["step"] == "Reading the posting", "only a waiting job is queued"
    assert "busy reading another posting" in (await parse_get(client, job_id)).json()["step"]


async def test_a_job_queued_behind_a_long_session_is_not_failed_for_waiting_and_is_handed_out_after_it(client, door):
    """Five minutes to be taken count from when the agent was last at work: a session of 20 minutes is no reason."""
    job_id = await parse_job(age=20 * MINUTE)
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="scout")  # the session has been calling the app all along
    assert (await parse_get(client, job_id)).json()["state"] == "waiting"
    await parse_runner(seen=4 * MINUTE, checked_in=MINUTE)  # it ended 4 minutes ago
    assert (await parse_get(client, job_id)).json()["state"] == "waiting"
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.PARSER
    assert (await mcp_server.jobs_parse_next(door))["id"] == job_id
    await parse_runner(seen=6 * MINUTE, checked_in=MINUTE)  # 6 minutes since the agent was at work: as before
    other = await parse_job(age=20 * MINUTE)
    failed = (await parse_get(client, other)).json()
    assert failed["state"] == "failed" and failed["error"] == role_parse.NOT_TAKEN


# --- the job ----------


async def test_a_job_is_waiting_then_reading_once_a_session_takes_it_then_done_with_the_fields(client, door):
    await parse_runner(seen=MINUTE)
    job_id = (await client.post("/api/v1/profile/parse", json={"text": POSTING_TEXT, "role_id": "ops"})).json()["id"]
    waiting = (await parse_get(client, job_id)).json()
    assert waiting == {"id": job_id, "state": "waiting", "step": "Waiting for an agent", "role_id": "ops"}
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["ok"] is True and taken["id"] == job_id and taken["text"] == POSTING_TEXT.strip()
    reading = (await parse_get(client, job_id)).json()
    assert (reading["state"], reading["step"]) == ("reading", "Reading the posting")
    assert "fields" not in reading and "error" not in reading
    reported = await mcp_server.jobs_parse_report(door, job_id, POSTING_FIELDS)
    assert reported["ok"] is True and reported["taken"] is True
    done = (await parse_get(client, job_id)).json()
    assert (done["state"], done["step"]) == ("done", "Done")
    assert done["fields"] == POSTING_FIELDS and "error" not in done
    assert (await parse_row(job_id)).text == "", "the posting is not kept once it has been read"
    assert (await mcp_server.jobs_parse_next(door))["done"] is True


async def test_an_unknown_job_is_a_404_in_plain_words(client):
    for key in (str(uuid.uuid4()), "not-an-id"):
        res = await parse_get(client, key)
        assert res.status_code == 404 and "gone" in res.json()["detail"]


async def test_a_job_not_taken_in_5_minutes_fails_and_one_not_finished_3_minutes_after_it_was_taken_fails(client):
    young = await parse_job(age=4 * MINUTE)
    old = await parse_job(age=6 * MINUTE)
    quick = await parse_job(role_parse.READING, age=10 * MINUTE, taken_ago=2 * MINUTE)
    slow = await parse_job(role_parse.READING, age=10 * MINUTE, taken_ago=4 * MINUTE)
    assert (await parse_get(client, young)).json()["state"] == "waiting"
    failed = (await parse_get(client, old)).json()
    assert failed["state"] == "failed" and failed["step"] == "Stopped"
    assert failed["error"] == role_parse.NOT_TAKEN and "5 minutes" in failed["error"] and "fields" not in failed
    assert (await parse_get(client, quick)).json()["state"] == "reading", "the 3 minutes count from when it was taken"
    late = (await parse_get(client, slow)).json()
    assert late["state"] == "failed" and late["error"] == role_parse.NOT_FINISHED and "3 minutes" in late["error"]
    assert (await parse_row(old)).state == role_parse.WAITING, "reading a job saves nothing"
    await parse_sweep()
    assert (await parse_row(old)).state == role_parse.FAILED and (await parse_row(slow)).state == role_parse.FAILED
    assert (await parse_row(old)).text == "" and (await parse_row(slow)).text == "", "a failed job keeps no posting"


async def test_a_finished_job_is_kept_for_a_day_and_then_gone(client):
    kept = await parse_job(role_parse.DONE, age=23 * 60 * MINUTE, fields=POSTING_FIELDS)
    gone = await parse_job(role_parse.DONE, age=25 * 60 * MINUTE, fields=POSTING_FIELDS)
    assert (await parse_get(client, kept)).json()["fields"] == POSTING_FIELDS
    assert (await parse_get(client, gone)).status_code == 404
    assert await parse_row(gone) is not None, "reading a job deletes nothing"
    await parse_sweep()
    assert await parse_row(gone) is None and await parse_row(kept) is not None


async def test_a_waiting_job_that_took_too_long_is_failed_for_the_runner_too_so_it_is_never_handed_out(client, door):
    old = await parse_job(age=6 * MINUTE)
    # The app's own pass fails it (the ask only reads: docs/PRD.md 47.5); a session's own call is a sign of the agent.
    await settle_housekeeping(OWNER)
    assert (await client.get("/api/v1/agent/work", headers=AUTH)).json() == {"next": None}
    assert (await parse_row(old)).state == "failed"
    assert (await mcp_server.jobs_parse_next(door))["done"] is True
    reply = await mcp_server.jobs_parse_report(door, old, POSTING_FIELDS)
    assert reply["ok"] is False and "ended" in reply["reason"]


# --- what the runner is handed ----------


async def test_a_waiting_job_is_the_first_work_the_runner_is_handed(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        assert await session_kinds.next_kind(session, OWNER) is None
        await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEARCH
    job_id = await parse_job()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.PARSER == "parser", (
            "the owner is at the screen")
        job = await session.get(ParseJob, uuid.UUID(job_id))
        job.state, job.taken_at = role_parse.READING, datetime.now(UTC)
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEARCH, (
            "a job a session holds is not handed out again")


async def test_the_parser_goes_ahead_of_everything_and_waits_for_nothing_else(js):
    job_id = await parse_job()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.PARSER, "with no search open"
        await session.commit()
    stale = await parse_job(age=6 * MINUTE)
    async with db.session_factory()() as session:
        await housekeeping.settle(session, OWNER)  # failing the late job is the app's own pass, not a look's
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.PARSER
        assert (await session.get(ParseJob, uuid.UUID(stale))).state == "failed", "one that waited too long is not work"
        await session.commit()
    row = await parse_row(job_id)
    assert row.state == "waiting"


async def test_no_parse_work_is_handed_out_when_paused_or_in_peak_hours(js, monkeypatch):
    await parse_job()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    await parse_runner(seen=MINUTE, paused=True)
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) is None, "paused"
    await parse_runner(seen=MINUTE)
    with monkeypatch.context() as peak:
        peak.setattr(night, "in_peak", lambda s, now=None: True)
        async with db.session_factory()() as session:
            assert await session_kinds.next_kind(session, OWNER) is None, "Claude's peak hours"
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.PARSER


async def test_a_job_left_waiting_with_the_runner_between_sessions_wakes_it_with_the_parse_reason(js):
    await parse_job()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        assert await missions.schedule_pass(session, OWNER) == [("waiting", wake.PARSE_ASKED)]
    assert wake.PARSE_ASKED == "parse_asked"


# --- the tools ----------


async def test_a_parser_call_without_the_scouts_token_is_refused_and_reads_nothing(client):
    job_id = await parse_job()
    ctx = SimpleNamespace(headers={"authorization": "Bearer wrong"})
    for call in (mcp_server.jobs_parse_next(ctx), mcp_server.jobs_parse_report(ctx, job_id, POSTING_FIELDS)):
        result = await call
        assert result["ok"] is False and "token" in result["reason"]
    assert (await parse_row(job_id)).state == "waiting" and (await parse_row(job_id)).fields is None


async def test_parse_next_hands_out_the_oldest_waiting_job_and_again_the_one_it_already_gave(client, door):
    first = await parse_job(age=2 * MINUTE)
    second = await parse_job(age=MINUTE)
    one = await mcp_server.jobs_parse_next(door)
    assert one["id"] == first and one["text"] == POSTING_TEXT and "jobs.parse_report" in one["how"]
    assert (await parse_row(first)).state == "reading" and (await parse_row(first)).taken_at is not None
    assert (await parse_row(second)).state == "waiting"
    assert (await mcp_server.jobs_parse_next(door))["id"] == first, "asking again before answering is not a second job"
    await mcp_server.jobs_parse_report(door, first, POSTING_FIELDS)
    assert (await mcp_server.jobs_parse_next(door))["id"] == second
    await mcp_server.jobs_parse_report(door, second, POSTING_FIELDS)
    assert (await mcp_server.jobs_parse_next(door))["done"] is True
    assert (await parse_row(first)).state == (await parse_row(second)).state == "done"


async def test_parse_report_names_every_problem_at_once_and_keeps_nothing_until_it_is_right(client, door):
    job_id = await parse_job()
    await mcp_server.jobs_parse_next(door)
    bad = {"name": "n" * 121, "titles": [f"Title {i}" for i in range(5)], "description": "d" * 1001,
           "responsibilities": 7, "title": "wrong key", "preferred": None}
    reply = await mcp_server.jobs_parse_report(door, job_id, bad)
    assert reply["ok"] is False
    for problem in ("name is 121 characters", "titles has 5", "description is 1001 characters",
                    "responsibilities must be text", "title is not a field"):
        assert problem in reply["reason"], problem
    assert "send jobs.parse_report again" in reply["how_to_fix"]
    assert (await parse_row(job_id)).state == "reading" and (await parse_row(job_id)).fields is None
    assert (await mcp_server.jobs_parse_report(door, job_id, POSTING_FIELDS))["ok"] is True


@pytest.mark.parametrize("fields, problem", [
    ("a string", "fields must be an object"),
    ({"titles": "Operations Manager"}, "titles must be a list of strings"),
    ({"titles": ["fine", 3]}, "titles must be a list of strings"),
    ({"titles": ["t" * 121]}, "a title is 121 characters"),
    ({"compensation": ["not text"]}, "compensation must be text"),
    ({"qualifications": "q" * 1001}, "qualifications is 1001 characters"),
])
def test_the_shape_of_a_report_is_checked(fields, problem):
    with pytest.raises(role_parse.ParseError, match=problem):
        role_parse.check_fields(fields)


def test_the_titles_the_parser_is_told_to_send_are_the_titles_it_accepts():
    """The instruction's "Never more than N" and the cap that refuses a fifth title are the same number."""
    text = files.read("instructions", "jobsearch.parser")
    told = re.search(r"Never more than (\d+)\.", text)
    assert told and int(told.group(1)) == role_parse.MAX_TITLES == 4
    assert f"at most {role_parse.MAX_TITLES}" in role_parse.PARSE_HOW


def test_a_report_is_tidied_text_trimmed_titles_trimmed_and_without_repeats_and_missing_fields_empty():
    assert role_parse.check_fields({
        "name": "  Ops lead ", "titles": [" Ops Lead", "", "ops lead", "Lead of Ops  "], "description": " Runs ops. ",
    }) == {"name": "Ops lead", "titles": ["Ops Lead", "Lead of Ops"], "description": "Runs ops.",
           "responsibilities": "", "qualifications": "", "preferred": "", "compensation": ""}
    limits = role_parse.check_fields({
        "titles": [f"Title {i}" for i in range(4)], "description": "d" * 1000, "name": "n" * 120})
    assert len(limits["titles"]) == 4 and len(limits["description"]) == 1000 and len(limits["name"]) == 120


async def test_parse_report_for_a_job_that_is_not_there_or_has_ended_is_refused_so_the_session_stops(client, door):
    for job_id in (str(uuid.uuid4()), "nonsense"):
        refused = await mcp_server.jobs_parse_report(door, job_id, POSTING_FIELDS)
        assert "no parse job has that id" in refused["reason"]
    job_id = await parse_job()
    await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, job_id, POSTING_FIELDS)
    again = await mcp_server.jobs_parse_report(door, job_id, {**POSTING_FIELDS, "name": "Another"})
    assert again["ok"] is False and "stop" in again["reason"]
    assert (await parse_row(job_id)).fields["name"] == POSTING_FIELDS["name"], "the first answer stands"


async def test_a_report_with_nothing_in_it_fails_the_job_in_words_for_the_owner(client, door):
    job_id = await parse_job()
    await mcp_server.jobs_parse_next(door)
    empty = {"name": " ", "titles": [], "description": "", "responsibilities": "", "qualifications": "",
             "preferred": "", "compensation": ""}
    assert (await mcp_server.jobs_parse_report(door, job_id, empty))["ok"] is True
    got = (await parse_get(client, job_id)).json()
    assert got["state"] == "failed" and got["error"] == role_parse.NOTHING_FOUND and "fields" not in got


# --- the roster ----------


async def test_the_idle_heartbeat_checks_the_runner_in_without_touching_when_it_last_called_the_app(client):
    assert (await client.post("/api/v1/agent/heartbeat", json={"state": "idle"})).status_code == 401
    old = datetime.now(UTC) - timedelta(hours=5)
    await parse_runner(seen=timedelta(hours=5), checked_in=timedelta(hours=5), kind="reader")
    beat = await client.post("/api/v1/agent/heartbeat", json={"session_id": "runner-idle", "state": "idle"},
                             headers=AUTH)
    assert beat.json() == {"ok": True, "stop": False}
    async with db.session_factory()() as session:
        conn = await roster.connection_by_slug(session, OWNER, roster.SCOUT)
        assert conn.checked_in_at > datetime.now(UTC) - MINUTE and conn.session_kind is None
        # The retry of a failed wake stops when the scout has called the app since: a ping is not such a call.
        assert abs(conn.last_seen_at - old) < MINUTE
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": True, "why": None}


async def test_a_parser_session_is_a_kind_the_heartbeat_and_the_agents_page_know(client):
    await parse_runner(seen=MINUTE)
    started = await client.post("/api/v1/agent/heartbeat", json={"session_id": "parser-abc", "state": "started"},
                                headers=AUTH)
    assert started.json() == {"ok": True, "stop": False}
    async with db.session_factory()() as session:
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).session_kind == "parser"
    agent = (await client.get(api_url("agents"))).json()[0]
    assert agent["status"] == "working" and agent["current"]["role"] == "parser"
    assert agent["current"]["task_title"] == "Reading a posting you pasted"
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": True, "why": None}, "busy, but not refused"
    async with db.session_factory()() as session:
        assert "busy reading another posting" in await job_queue.queued(session, OWNER)
    await client.post("/api/v1/agent/heartbeat", json={"session_id": "parser-abc", "state": "ended"}, headers=AUTH)
    assert (await client.get("/api/v1/agent/ready")).json()["ready"] is True
    async with db.session_factory()() as session:
        assert await job_queue.queued(session, OWNER) is None
    assert (await client.get(api_url("agents"))).json()[0]["status"] == "idle"


async def test_the_runner_is_told_parser_while_a_job_waits_and_nothing_once_it_is_taken(client, door):
    await parse_job()
    assert (await client.get("/api/v1/agent/work", headers=AUTH)).json()["next"] == "parser"
    await mcp_server.jobs_parse_next(door)
    assert (await client.get("/api/v1/agent/work", headers=AUTH)).json() == {"next": None}


# --- two sessions at once ----------


async def test_a_job_one_session_took_is_not_handed_to_a_second_session_asking_while_it_works(js):
    await parse_job(kind=role_parse.RANK, age=timedelta(minutes=2))
    await parse_job(kind=role_parse.RANK, age=timedelta(minutes=1))
    async with db.session_factory()() as session:
        mine = await role_parse.take_next(session, OWNER, session_id="parser-a")
        other = await role_parse.take_next(session, OWNER, session_id="parser-b")
        again = await role_parse.take_next(session, OWNER, session_id="parser-a")
        await session.commit()
    assert other["id"] != mine["id"], "a second session is given its own job, not the one already being ranked"
    assert again["id"] == mine["id"], "the session that took a job is told it again, not handed another"
    async with db.session_factory()() as session:
        third = await role_parse.take_next(session, OWNER, session_id="parser-c")
    assert third.get("done") is True, "with every job held by a session, a new one is told none is waiting"
