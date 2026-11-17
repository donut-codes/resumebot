"""The session pool queues its own work (`pool/core.py`): a free session that finds nothing queued gets the next job
started
for it, in the pool's order, within every limit; the owner can pause, resume and run each kind, and pause all (the
global pause). The roster counts the pool's sessions alone once the old lanes' runners are gone. Everything here is made
up."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app import db
from app.agents import roster
from app.jobsearch import process, stages
from app.jobsearch.actors import BY_OWNER
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import numbers as mission_numbers
from app.jobsearch.models import Opportunity, Packet, SearchRun
from app.jobsearch.pool import core as pool
from app.jobsearch.research import analysis
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from app.jobsearch.writing import packets
from tests.builders import analysis_setup, save_documents
from tests.support import OWNER

KINDS = "/api/v1/agent-kinds"


async def _asks(count: int = 1) -> list[str | None]:
    """A free pool session asks `count` times, as a runner does: what is queued is the answer; with nothing queued, its
    ask is what lets the app's own pass start the next job (`start_queued_job`), and the ask is made again."""
    out: list[str | None] = []
    async with db.session_factory()() as session:
        for _ in range(count):
            kind = await session_kinds.next_kind(session, OWNER)
            if kind is None:
                await session_kinds.hand_out(session, OWNER)  # nothing to claim: it only notes a free session asked
                if await session_kinds.pull(session, OWNER):
                    kind = await session_kinds.next_kind(session, OWNER)
            out.append(kind)
        await session.commit()
    return out


async def _drafted(how_many: int) -> None:
    """Roles in Needs Drafted with no packet."""
    async with db.session_factory()() as session:
        for i in range(how_many):
            session.add(Opportunity(
                owner=OWNER, slug=f"northwind-{i}", company=f"Northwind {i}", title="Programs Lead", fit=5,
                origin="scout", match_key=f"northwind {i}|programs lead", status=stages.READY_TO_LAUNCH,
                url=f"https://jobs.example.test/{i}",
                description="Lead programs for the board. " * 2))
        await session.commit()


async def _count(model, *where) -> int:
    async with db.session_factory()() as session:
        return int(await session.scalar(select(func.count(model.id)).where(*where)) or 0)


async def _packets_done() -> None:
    async with db.session_factory()() as session:
        for row in (await session.scalars(select(Packet))).all():
            row.status = "ready"
            if row.instance_id:  # its process is what holds the seat
                await process.engine().cancel(session, row.instance_id, "a test finishes it")
        await session.commit()


async def _roles_taken() -> None:
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity).where(Opportunity.analysis_state == "waiting"))).all():
            analysis.claim(role)
        await session.commit()


async def _owner_asks_a_packet() -> None:
    """The owner presses Build now on the first of the Ready to Launch roles: one packet, writing."""
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(
            Opportunity.status == stages.READY_TO_LAUNCH).order_by(Opportunity.slug))).first()
        assert role is not None
        await packets.start(session, OWNER, role.id, by=BY_OWNER)
        await session.commit()


async def test_a_free_session_with_nothing_queued_gets_the_next_job_started_in_the_pools_order(js):
    await analysis_setup()
    await _drafted(1)
    assert await _count(Packet) == 0 and await _count(Opportunity, Opportunity.analysis_state.is_not(None)) == 0
    assert await _asks() == [session_kinds.ANALYSIS], (
        "research of an on-deck role comes first: no packet is ever queued")
    assert await _count(Packet) == 0, "a Ready to Launch role with no packet waits for Go or Build now"
    assert await _count(Opportunity, Opportunity.analysis_state == "waiting") == 1
    assert await _count(Opportunity, Opportunity.analysis_state.is_not(None)) == 1, "one job at a time"
    await _roles_taken()
    assert await _asks() == [None], "the owner's rule: one session at a time on a site, and these share one"
    assert await _count(Opportunity, Opportunity.analysis_state == "waiting") == 2, "the next role is queued"
    assert await _count(SearchRun) == 0
    assert await _asks() == [session_kinds.SEARCH], "the research waits its turn on the site, so the session searches"
    assert await _count(Opportunity, Opportunity.analysis_state == "waiting") == 2, "none is piled up behind it"
    assert await _count(SearchRun) == 1
    async with db.session_factory()() as session:
        for run_ in (await session.scalars(select(SearchRun))).all():
            run_.status = "done"
        for role in (await session.scalars(select(Opportunity).where(
                Opportunity.status == stages.SECOND_IMPRESSION))).all():
            role.analysis_state = "done"
        await session.commit()
    assert await _asks() == [None], "nothing is left to start, and a second search is not begun in the day"
    assert await _count(SearchRun) == 1
    assert await _count(Packet) == 0


async def test_a_job_is_never_queued_twice(js):
    await analysis_setup()
    await _drafted(1)
    await _owner_asks_a_packet()
    assert await _asks(2) == [session_kinds.SEAT, session_kinds.SEAT]
    assert await _count(Packet) == 1, "the packet is still waiting for a session: no second one is started"
    await _packets_done()
    assert await _asks(2) == [session_kinds.ANALYSIS, session_kinds.ANALYSIS]
    assert await _count(Opportunity, Opportunity.analysis_state == "waiting") == 1


async def test_a_paused_kind_is_neither_handed_out_nor_queued(js, client):
    await analysis_setup()
    await _drafted(1)
    await _owner_asks_a_packet()
    paused = await client.post(f"{KINDS}/writer/pause")
    assert paused.status_code == 200
    assert next(k for k in paused.json() if k["kind"] == "writer")["paused"] is True
    assert await _asks() == [session_kinds.ANALYSIS], "the writer is skipped"
    await client.post(f"{KINDS}/researcher/pause")
    await _roles_taken()
    assert (await _asks())[0] != session_kinds.ANALYSIS
    assert await _count(Opportunity, Opportunity.analysis_state == "waiting") == 1, "no more research is queued"
    async with db.session_factory()() as session:  # the search round the pool queued meanwhile is over
        for run_ in (await session.scalars(select(SearchRun))).all():
            run_.status = "done"
        await session.commit()
    resumed = await client.post(f"{KINDS}/writer/resume")
    assert next(k for k in resumed.json() if k["kind"] == "writer")["paused"] is False
    assert await _asks() == [session_kinds.SEAT]


async def test_a_queued_job_of_a_paused_kind_is_not_handed_out_on_any_lane(js, client):
    await analysis_setup()
    await _drafted(1)
    await _owner_asks_a_packet()
    await client.post(f"{KINDS}/writer/pause")
    assert await _asks() == [session_kinds.ANALYSIS]


async def test_run_queues_that_kinds_waiting_work_and_the_writer_has_none_to_queue(js, client):
    await analysis_setup()
    await _drafted(3)
    ran = await client.post(f"{KINDS}/writer/run")
    assert ran.status_code == 200 and ran.json() == {"kind": "writer", "queued": 0, "held": None}
    assert await _count(Packet) == 0, "the writer's Run now builds nothing ahead: Go and Build now do"
    assert await _count(Opportunity, Opportunity.analysis_state.is_not(None)) == 0, "only that kind's work"
    ran = await client.post(f"{KINDS}/researcher/run")
    assert ran.json()["queued"] == 3
    assert await _count(Opportunity, Opportunity.analysis_state == "waiting") == 3
    assert (await client.post(f"{KINDS}/nobody/run")).status_code == 404


async def test_run_all_runs_every_kind_and_queues_no_packet(js, client):
    await analysis_setup()
    await _drafted(7)
    ran = (await client.post(f"{KINDS}/run-all")).json()
    assert ran["held"] is None
    assert ran["queued"]["writer"] == 0, "no packet is built ahead of Go"
    assert ran["queued"]["researcher"] == 3
    assert await _count(Packet) == 0


async def test_pause_all_is_the_global_pause_and_resume_all_lifts_it(js, client):
    await analysis_setup()
    await _drafted(1)
    paused = await client.post(f"{KINDS}/pause-all")
    assert paused.status_code == 200
    async with db.session_factory()() as session:
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).paused is True
    assert await _asks(3) == [None, None, None]
    assert await _count(Opportunity, Opportunity.analysis_state.is_not(None)) == 0, "nothing is queued while paused"
    held = await client.post(f"{KINDS}/writer/run")
    assert held.json() == {"kind": "writer", "queued": 0, "held": "the agents are paused"}
    await client.post(f"{KINDS}/resume-all")
    async with db.session_factory()() as session:
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).paused is False
    assert await _asks() == [session_kinds.ANALYSIS]


async def test_peak_hours_hold_the_pool_and_run_alike(js, client, monkeypatch):
    await analysis_setup()
    await _drafted(1)
    monkeypatch.setattr(night, "in_peak", lambda s, now=None: True)
    assert await _asks() == [None]
    assert (await client.post(f"{KINDS}/writer/run")).json() == {"kind": "writer", "queued": 0, "held": "busy hours"}
    assert (await client.post(f"{KINDS}/run-all")).json()["held"] == "busy hours"
    assert await _count(Packet) == 0 and await _count(Opportunity, Opportunity.analysis_state.is_not(None)) == 0


async def test_the_list_says_each_kinds_state_waiting_work_and_files(js, client):
    await analysis_setup()
    await _drafted(2)
    await client.post(f"{KINDS}/title_checker/pause")
    rows = (await client.get(KINDS)).json()
    assert [r["kind"] for r in rows] == ["learner", "pathfinder", "researcher", "writer", "browser_reader",
                                         "title_checker", "search"]
    by = {r["kind"]: r for r in rows}
    assert by["writer"]["waiting"] == 0, "the pool queues no packet"
    assert by["researcher"]["waiting"] == 3
    assert by["title_checker"]["paused"] is True and by["writer"]["paused"] is False
    assert all(set(r) - {"writing"} == {"kind", "label", "purpose", "doing", "paused", "waiting", "health",
                                        "instructions"} for r in rows), "each row says what its kind is for"
    assert [r["kind"] for r in rows if "writing" in r] == ["writer"] and by["writer"]["writing"] == 0
    from app.harness import instructions as files

    for r in rows:
        assert r["instructions"], r["kind"]
        for f in r["instructions"]:
            assert f["name"] in files.names(f["kind_of_file"]), f
    await _owner_asks_a_packet()
    await _asks()
    async with db.session_factory()() as session:
        await roster.note_runner(session, OWNER, {"runner": "session-2", "state": "started", "session_id": "writer-1"},
                                 kind=roster.WRITER)
        await session.commit()
    writer = next(r for r in (await client.get(KINDS)).json() if r["kind"] == "writer")
    assert [d["session"] for d in writer["doing"]] == ["Session 2"] and writer["doing"][0]["task"]


async def test_run_says_what_a_kind_with_nothing_queued_is_blocked_on(js, client):
    await analysis_setup()
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            role.analysis_state = analysis.NO_TEXT
        await session.commit()
    ran = (await client.post(f"{KINDS}/researcher/run")).json()
    assert ran["queued"] == 0 and ran["blocked"] == "3 are waiting for the posting's text"
    assert "blocked" not in (await client.post(f"{KINDS}/title_checker/run")).json(), "nothing is unsourced"
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity).where(Opportunity.slug == "hooli"))).all():
            role.sourced_at, role.analysis_state = None, None  # one role the app holds no posting for yet
        await session.commit()
    scout = (await client.post(f"{KINDS}/title_checker/run")).json()
    assert scout["queued"] == 0, scout
    assert scout["blocked"] == "1 is waiting for the browser to find the employer's posting"


async def _ended_round(hours_ago: float) -> None:
    async with db.session_factory()() as session:
        ended = datetime.now(UTC) - timedelta(hours=hours_ago)
        session.add(SearchRun(owner=OWNER, status="done", started_by="manual", started_at=ended - timedelta(minutes=5),
                              ended_at=ended))
        await session.commit()


async def test_an_idle_pool_starts_a_round_only_when_the_last_one_ended_a_few_hours_ago(js):
    async with db.session_factory()() as session:
        assert await pool._round_can_start(session, OWNER), "no round has ever run"
    await _ended_round(0.1)
    async with db.session_factory()() as session:
        assert not await pool._round_can_start(session, OWNER), "one ended minutes ago: the idle pool waits"
        assert await pool._round_can_start(session, OWNER, idle=False), "the owner's own start does not wait"
        assert await pool.queue_search(session, OWNER) == 0, "an idle search waits"
        assert await pool._round_can_start(session, OWNER, idle=False), "a round for waiting titles or reads does not"


async def test_the_idle_gap_ends_after_the_default_or_the_scout_missions_every_n_hours(js):
    await _ended_round(5)
    async with db.session_factory()() as session:
        assert await pool._round_can_start(session, OWNER), "past the default gap"
        await missions.ensure(session, OWNER)
        mission = await missions.get(session, OWNER, missions.SCOUT)
        assert mission is not None
        mission.schedule = {"kind": "every", "hours": 8}
        await session.flush()
        assert not await pool._round_can_start(session, OWNER), "the mission's own interval is longer"


# --- a packet is built only when the owner asks (Build now) or Go does: the pool never queues one -------------------


async def test_the_owners_own_request_for_a_packet_is_never_limited_by_the_days_list(js, client):
    await analysis_setup()
    async with db.session_factory()() as session:
        await mission_numbers.save(session, OWNER, {"second_impression_size": 1, "ready_to_launch_size": 1}, by="owner")
        await session.commit()
    await _drafted(3)
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.status == stages.READY_TO_LAUNCH))
        assert role is not None
        role_id = role.id
    assert (await client.post(f"/api/v1/roles/{role_id}/packet", json={})).status_code in (200, 201, 202)
    assert await _count(Packet) == 1


async def _one_asked_and_two_waiting() -> None:
    """One packet the owner asked for (its first seat waits for a session), and two Ready to Launch roles."""
    await analysis_setup()
    await _drafted(3)
    async with db.session_factory()() as session:
        await save_documents(session)
        role = (await session.scalars(select(Opportunity).order_by(Opportunity.slug))).first()
        await packets.start(session, OWNER, role.id, by=BY_OWNER)
        await session.commit()


async def test_the_asked_packets_seat_is_handed_out_and_the_pool_starts_no_packet_for_the_others(js):
    await _one_asked_and_two_waiting()
    assert await _asks() == [session_kinds.SEAT], (
        "only the asked packet has a seat: the other two roles have none asked")
    assert await _count(Packet) == 1


async def test_a_packet_seat_the_owner_asked_for_is_handed_out_ahead_of_the_browsers_backlog(
        js, client, monkeypatch):
    """A written packet's seat never ran, because browser sessions (hundreds of
    postings waiting) outranked it at every check-in. The owner's packet seats go first; Run now on the writer
    counts it and wakes a lane."""
    await _one_asked_and_two_waiting()

    async def browser_has_work(session, owner, worker, now=None, *, mine=(), scope=None):
        return [uuid.uuid4()]

    monkeypatch.setattr(browser_queue, "free", browser_has_work)
    assert await _asks() == [session_kinds.SEAT], "the owner's seat waits behind no browser session"
    ran = await client.post(f"{KINDS}/writer/run")
    assert ran.json() == {"kind": "writer", "queued": 1, "held": None}, "Run now counts the seat that waits"
