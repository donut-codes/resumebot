"""The Agents page lists every runner of the scout (an install with lanes runs five at once: search, two researchers,
the owner's work and the writer), what each is doing and since when, and counts the slots as the runners that are there
and busy as the ones with a session at work. An install with one runner is the agent as it always was. Everything here
is made up."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Opportunity
from app.jobsearch.research import analysis
from app.jobsearch.work import job_queue
from app.jobsearch.writing import packets
from app.models.agent import AgentRunner
from tests.builders import analysis_setup, api_url
from tests.support import OWNER

BEAT = "/api/v1/agent/heartbeat"


async def _beat(client, token, state, session_id="runner-idle", runner=None, lane=None):
    """A heartbeat as the scout sends it: a runner with lanes names itself (`runner`) and, unless it serves the owner's
    work, its `lane`."""
    body = {"session_id": session_id, "state": state, **({"runner": runner} if runner else {}),
            **({"lane": lane} if lane else {})}
    sent = await client.post(BEAT, json=body, headers=token)
    assert sent.status_code == 200, sent.text


async def _scout(client) -> dict:
    return (await client.get(api_url("agents"))).json()[0]


async def _runner(name) -> AgentRunner:
    async with db.session_factory()() as session:
        return await session.scalar(select(AgentRunner).where(AgentRunner.owner == OWNER, AgentRunner.name == name))


async def _quiet_for(name, minutes):
    """The runner was last heard this long ago."""
    async with db.session_factory()() as session:
        row = await session.scalar(select(AgentRunner).where(AgentRunner.owner == OWNER, AgentRunner.name == name))
        row.seen_at = datetime.now(UTC) - timedelta(minutes=minutes)
        await session.commit()


async def _lanes_up(client, token, *, writing=False):
    """The five runners of an install with lanes, as `compose` names them: the search at work on a title session, the
    first researcher at work, the second between sessions, the owner's runner between sessions and the writer at
    work."""
    await _beat(client, token, "started", "titles-000000000001", runner="search", lane="search")
    await _beat(client, token, "started", "analyst-000000000002", runner="analysis-1", lane="analysis")
    await _beat(client, token, "idle", runner="analysis-2", lane="analysis")
    await _beat(client, token, "idle", runner="owner")
    await _beat(client, token, "started", "writer-000000000003", runner="writer", lane="writer")


async def _roster_started() -> None:
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await session.commit()


async def _a_packet_is_written(ids) -> None:
    async with db.session_factory()() as session:
        await packets.start(session, OWNER, ids["Hooli"])
        await session.commit()


async def test_five_lanes_show_each_runners_session_and_the_busy_count_is_those_at_work_of_those_there(
        js, client, scout_token):
    ids = await analysis_setup()
    await _a_packet_is_written(ids)
    await _lanes_up(client, scout_token)
    scout = await _scout(client)
    runners = scout["runners"]
    assert scout["roles_words"] == "Any kind of work" and scout["model_words"] == "A model for each kind of work"
    assert [r["name"] for r in runners] == ["analysis-1", "analysis-2", "owner", "search", "writer"]
    assert [r["label"] for r in runners] == ["Analysis 1", "Analysis 2", "Owner", "Search", "Writer"]
    assert [r["state"] for r in runners] == ["working", "idle", "idle", "working", "working"]
    assert [r["kind"] for r in runners] == ["analyst", None, None, "titles", "writer"]
    assert [r["task"] for r in runners] == [
        "Researching a role", "Idle", "Idle", "Judging titles", "Writing a packet"]
    for r in runners:
        assert datetime.fromisoformat(r["since"]) <= datetime.now(UTC), "since when, for each"
        assert datetime.fromisoformat(r["seen_at"]) <= datetime.now(UTC)
    # Three of the five are at work: that is the busy count, out of the five that are there.
    assert scout["capacity"]["busy"] == 3 and scout["capacity"]["slots"] == 5
    assert scout["capacity"]["free"] is True and scout["capacity"]["words"] == "3 of 5 at work"
    # The agent is working, and the first session at work (by the runners' names) is its headline.
    assert scout["status"] == "working" and scout["availability"] == "On a task"
    assert scout["current"]["task_title"] == "Researching a role" and scout["current"]["role"] == "analyst"
    assert datetime.fromisoformat(scout["capacity"]["since"]) == max(
        datetime.fromisoformat(r["since"]) for r in runners if r["state"] == "working"), "the newest session's start"
    # The detail, which the open agent reads, says the same.
    detail = (await client.get(api_url(f"agents/{scout['connection_id']}"))).json()
    assert [r["name"] for r in detail["runners"]] == [r["name"] for r in runners]


async def test_the_headline_is_the_open_search_when_there_is_one_and_the_researcher_when_there_is_none(
        js, client, scout_token):
    await analysis_setup()
    await _beat(client, scout_token, "idle", runner="search", lane="search")
    await _beat(client, scout_token, "started", "analyst-000000000002", runner="analysis-1", lane="analysis")
    scout = await _scout(client)
    assert scout["status"] == "working" and scout["current"]["role"] == "analyst"
    assert scout["current"]["task_title"] == "Researching a role", "no search is open: the researcher is the headline"
    assert scout["capacity"]["words"] == "1 of 2 at work"
    # With a search open the headline is the search, as it was, and every runner is still listed.
    async with db.session_factory()() as session:
        await run.start_round(session, OWNER, started_by="manual", focus="banks")
        await session.commit()
    scout = await _scout(client)
    assert scout["current"]["task_title"] == "Searching for roles: banks" and scout["current"]["role"] == "scout"
    assert [r["name"] for r in scout["runners"]] == ["analysis-1", "search"]


async def test_an_install_with_one_runner_shows_the_agent_as_it_always_was(js, client, scout_token):
    await _beat(client, scout_token, "idle")
    scout = await _scout(client)
    assert scout["runners"] == [] and scout["capacity"]["slots"] is None, "no slots are counted for one runner"
    assert scout["status"] == "idle" and scout["capacity"]["words"] == "Free" and scout["capacity"]["busy"] == 0
    # the one runner takes a session of its own: the connection's account of it, in the words it always used
    await _beat(client, scout_token, "started", "analyst-000000000001")
    scout = await _scout(client)
    assert scout["runners"] == []
    assert scout["status"] == "working" and scout["current"]["role"] == "analyst"
    assert scout["capacity"] == {"free": False, "busy": 1, "slots": None, "words": "Busy: working on a task",
                                 "since": None}
    # A runner that names itself, and is the only one, is still one runner.
    await _beat(client, scout_token, "ended", "analyst-000000000001", runner="scout")
    assert (await _scout(client))["runners"] == []
    assert (await _runner("scout")).state == "idle"


async def test_a_lane_that_stopped_heartbeating_is_shown_as_not_seen_since_and_is_not_counted(
        js, client, scout_token):
    ids = await analysis_setup()
    await _a_packet_is_written(ids)
    await _lanes_up(client, scout_token)
    await _quiet_for("analysis-1", 45)  # it was at work when it went quiet
    await _quiet_for("writer", 12)
    scout = await _scout(client)
    by_name = {r["name"]: r for r in scout["runners"]}
    for name, minutes in (("analysis-1", 45), ("writer", 12)):
        gone = by_name[name]
        assert gone["state"] == "not_seen" and gone["task"] is None and gone["kind"] is None
        heard = datetime.fromisoformat(gone["seen_at"])
        assert datetime.fromisoformat(gone["since"]) == heard, "since when it was last heard"
        assert timedelta(minutes=minutes - 1) < datetime.now(UTC) - heard < timedelta(minutes=minutes + 1)
    assert by_name["search"]["state"] == "working" and by_name["analysis-2"]["state"] == "idle"
    # Not there, so not a slot: three runners are there and one of them is at work.
    assert scout["capacity"]["slots"] == 3 and scout["capacity"]["busy"] == 1
    assert scout["capacity"]["words"] == "1 of 3 at work"
    # ... and the runners are all listed all the same, in the order the work goes.
    assert [r["name"] for r in scout["runners"]] == ["analysis-1", "analysis-2", "owner", "search", "writer"]
    # It comes back with its next word.
    await _beat(client, scout_token, "alive", "analyst-000000000002", runner="analysis-1", lane="analysis")
    back = {r["name"]: r for r in (await _scout(client))["runners"]}["analysis-1"]
    assert back["state"] == "working" and back["task"] == "Researching a role"


async def test_every_runner_gone_quiet_leaves_the_agent_offline_and_still_lists_them(js, client, scout_token):
    await _lanes_up(client, scout_token)
    for name in ("search", "analysis-1", "analysis-2", "owner", "writer"):
        await _quiet_for(name, 30)
    scout = await _scout(client)
    assert scout["status"] == "offline" and scout["availability"] == "Offline"
    assert [r["state"] for r in scout["runners"]] == ["not_seen"] * 5
    assert scout["capacity"] == {"free": False, "busy": 0, "slots": 0, "words": "Offline", "since": None}


async def test_the_busy_count_follows_the_sessions_starting_and_ending(js, client, scout_token):
    await _roster_started()
    await _beat(client, scout_token, "idle", runner="search", lane="search")
    await _beat(client, scout_token, "idle", runner="analysis-1", lane="analysis")
    await _beat(client, scout_token, "idle", runner="analysis-2", lane="analysis")

    async def count():
        capacity = (await _scout(client))["capacity"]
        return capacity["busy"], capacity["slots"]

    assert await count() == (0, 3)
    await _beat(client, scout_token, "started", "analyst-000000000001", runner="analysis-1", lane="analysis")
    assert await count() == (1, 3)
    await _beat(client, scout_token, "started", "analyst-000000000002", runner="analysis-2", lane="analysis")
    assert await count() == (2, 3)
    await _beat(client, scout_token, "alive", "analyst-000000000001", runner="analysis-1", lane="analysis")
    assert await count() == (2, 3), "a session that is alive is not counted again"
    started = (await _runner("analysis-1")).since
    # a late word of an earlier session does not free the runner that has begun another
    await _beat(client, scout_token, "ended", "analyst-0000000000ff", runner="analysis-1", lane="analysis")
    assert await count() == (2, 3) and (await _runner("analysis-1")).since == started
    await _beat(client, scout_token, "ended", "analyst-000000000001", runner="analysis-1", lane="analysis")
    assert await count() == (1, 3)
    await _beat(client, scout_token, "ended", "analyst-000000000002", runner="analysis-2", lane="analysis")
    assert await count() == (0, 3)
    # the second session of the runner starts: it is at work again, a new session since a new moment
    await _beat(client, scout_token, "started", "analyst-000000000003", runner="analysis-1", lane="analysis")
    assert await count() == (1, 3) and (await _runner("analysis-1")).since >= started
    # a runner whose "started" was missed (the app was down) is at work from its first "alive"
    await _beat(client, scout_token, "alive", "titles-000000000004", runner="search", lane="search")
    assert await count() == (2, 3) and (await _runner("search")).kind == "titles"
    # with every runner at work none is free
    await _beat(client, scout_token, "started", "analyst-000000000005", runner="analysis-2", lane="analysis")
    full = (await _scout(client))["capacity"]
    assert (full["busy"], full["slots"], full["free"], full["words"]) == (3, 3, False, "All 3 at work")
    await _beat(client, scout_token, "ended", "analyst-000000000005", runner="analysis-2", lane="analysis")
    # its idle word says it is between sessions, whatever it had before
    await _beat(client, scout_token, "idle", runner="search", lane="search")
    assert await count() == (1, 3) and (await _runner("search")).session_id is None


async def test_a_lanes_idle_word_is_the_runners_alone_and_the_owner_runners_still_checks_the_agent_in(
        js, client, scout_token):
    await _roster_started()
    # The search, a researcher and the writer say they are up: no job can be handed to them, and they are not a call
    # about any work, so the connection is as it was.
    for runner, lane in (("search", "search"), ("analysis-1", "analysis"), ("writer", "writer")):
        await _beat(client, scout_token, "idle", runner=runner, lane=lane)
    async with db.session_factory()() as session:
        conn = await roster.connection_by_slug(session, OWNER, roster.SCOUT)
        assert conn.checked_in_at is None and conn.last_seen_at is None and conn.session_kind is None
        assert await job_queue.ready(session, OWNER) == (False, "No agent has checked in yet.")
    # The runner that serves the owner's work saying it is up is what makes an agent there to take a job.
    await _beat(client, scout_token, "idle", runner="owner")
    async with db.session_factory()() as session:
        conn = await roster.connection_by_slug(session, OWNER, roster.SCOUT)
        assert conn.checked_in_at is not None and conn.last_seen_at is None, "not a call about any work"
        assert await job_queue.ready(session, OWNER) == (True, None)
    # A lane's session leaves the owner runner's account of its own session alone, as ever.
    await _beat(client, scout_token, "started", "parser-000000000001", runner="owner")
    await _beat(client, scout_token, "ended", "analyst-000000000002", runner="analysis-1", lane="analysis")
    async with db.session_factory()() as session:
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).session_kind == roster.PARSER


async def test_a_researching_session_is_named_for_exactly_the_role_it_took(js, client, scout_token):
    """A claim records the session that made it (the call's `X-Session-Id`, `analysis.claim`), and a runner's session is
    the one its heartbeat named, so each researcher is named for the role that carries its session, however close
    together two sessions started and took theirs. A session that has taken nothing, or whose call named no session,
    is not named for a role."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        for name in ("Fabrikam", "Initech"):
            role = await session.get(Opportunity, ids[name])
            role.url = f"https://careers.{name.lower()}.example.test/jobs/1"
            await analysis.start(session, OWNER, role)
        await session.commit()

    async def take(session_id: str | None, runner: str | None) -> Opportunity:
        async with db.session_factory()() as session:
            role = await analysis.claim_next(session, OWNER, session_id=session_id, runner=runner)
            await session.commit()
            return role

    async def tasks():
        return {r["name"]: r["task"] for r in (await _scout(client))["runners"]}

    await _beat(client, scout_token, "idle", runner="analysis-2", lane="analysis")
    # One session started, and the role it took after: its role.
    await _beat(client, scout_token, "started", "analyst-000000000001", runner="analysis-1", lane="analysis")
    assert (await tasks())["analysis-1"] == "Researching a role", "it has taken nothing yet"
    first = await take("analyst-000000000001", "analysis-1/aaaa")
    assert (await tasks())["analysis-1"] == f"Researching {first.title} at {first.company}"
    # A second session starts and takes the other role: each is named for its own, though both started and took close
    # together (the guess from the time of the claim could not have told).
    await _beat(client, scout_token, "started", "analyst-000000000002", runner="analysis-2", lane="analysis")
    second = await take("analyst-000000000002", "analysis-2/bbbb")
    assert first.id != second.id
    assert await tasks() == {"analysis-1": f"Researching {first.title} at {first.company}",
                             "analysis-2": f"Researching {second.title} at {second.company}"}


async def test_a_runner_not_heard_from_for_a_day_is_dropped(js, client, scout_token):
    await _roster_started()
    await _beat(client, scout_token, "idle", runner="analysis-1", lane="analysis")
    await _beat(client, scout_token, "idle", runner="analysis-2", lane="analysis")
    await _quiet_for("analysis-2", 24 * 60 + 5)
    assert (await _scout(client))["runners"][1]["state"] == "not_seen", "a day is long, but until a word is heard"
    await _beat(client, scout_token, "idle", runner="analysis-1", lane="analysis")
    assert await _runner("analysis-2") is None
    assert (await _scout(client))["runners"] == [], "one runner is left: the agent as it always was"
    await _beat(client, scout_token, "idle", runner="analysis-2", lane="analysis")  # a lane turned back on
    assert [r["name"] for r in (await _scout(client))["runners"]] == ["analysis-1", "analysis-2"]


def test_a_runner_is_named_by_its_name_else_its_lane_else_scout():
    assert roster.runner_name({"runner": "analysis-1", "lane": "analysis"}) == "analysis-1"
    assert roster.runner_name({"lane": "Analysis"}) == "analysis", "an older runner names only its lane"
    assert roster.runner_name({"state": "alive"}) == "scout", "a runner with no lane names none"
    assert roster.runner_name({"runner": "  Search+Owner "}) == "search+owner"
    assert roster.runner_name({"runner": "<b>x</b> y", "lane": "writer"}) == "bxby"
    assert roster.runner_name({"runner": "!!!", "lane": "writer"}) == "writer", "nothing usable falls through"
    assert roster.runner_name({"runner": 7}) == "scout"
    assert len(roster.runner_name({"runner": "a" * 200})) == 60
    assert roster.runner_label("analysis-1") == "Analysis 1"
    assert roster.runner_label("search+owner") == "Search and owner"
