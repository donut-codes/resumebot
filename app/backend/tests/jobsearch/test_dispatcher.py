"""The dispatcher (docs/PRD.md path 47, step 2): the app checks what work is ready and commands an idle lane with the
item it claimed for it, instead of the lanes asking on a timer; the claims of site projects and of the browser's items;
the fallback check-in that is answered with the claim already made. Synthetic data only; nothing here reaches the
network (the command is recorded, never sent)."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.main import app
from app.jobsearch.models import HandOut
from app.jobsearch.work import dispatcher, hand_outs, housekeeping, session_kinds, wake
from app.settings import settings
from tests.builders import parse_job
from tests.support import OWNER, load_profile

NOW = datetime.now(UTC)


@pytest.fixture
async def world(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(settings, "wake_url", "http://127.0.0.1:8651/wake")
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()


class Sent(list):
    """The commands the dispatcher sent, and the runners that do not accept one (`refuse`)."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse: set[str] = set()


@pytest.fixture
def sent(monkeypatch):
    commands = Sent()

    async def command(runner, kind, claim, item, model=None):
        commands.append({"runner": runner, "kind": kind, "claim": claim, "item": item, "model": model})
        return runner not in commands.refuse

    monkeypatch.setattr(wake, "command", command)
    return commands


async def _idle(*names: str, state: str = "idle") -> None:
    async with db.session_factory()() as session:
        for name in names:
            await roster.note_runner(session, OWNER, {"runner": name, "state": state, "session_id": f"s-{name}"})
        await session.commit()


# --- the pass ----------


async def test_a_pass_commands_each_idle_lane_with_an_item_of_its_own_and_leaves_the_busy_one_alone(world, sent):
    first, second = await parse_job(age=timedelta(minutes=2)), await parse_job()
    await _idle("session-1", "session-2", "session-3")
    await _idle("session-3", state="started")  # in a session: not commanded
    assert await dispatcher.dispatch_pass(OWNER) == 2
    assert [(c["runner"], c["kind"], c["item"]["id"]) for c in sent] == [
        ("session-1", "parser", first), ("session-2", "parser", second)], "an item each, the oldest first"
    async with db.session_factory()() as session:
        assert await hand_outs.count(session, OWNER, session_kinds.PARSER) == 2


async def test_a_pass_does_nothing_without_an_idle_lane_and_a_lane_with_a_claim_waiting_is_not_commanded_again(
        world, sent):
    await parse_job()
    assert await dispatcher.dispatch_pass(OWNER) == 0 and sent == [], "no lane is up"
    await _idle("session-1")
    assert await dispatcher.dispatch_pass(OWNER) == 1
    await parse_job()
    assert await dispatcher.dispatch_pass(OWNER) == 0 and len(sent) == 1, "it has not taken the first yet"


async def test_a_command_the_lane_refuses_gives_its_claim_back_at_once(world, sent):
    job = await parse_job()
    await _idle("session-1")
    sent.refuse.add("session-1")  # busy, or held at Claude's usage limit
    assert await dispatcher.dispatch_pass(OWNER) == 0 and len(sent) == 1
    async with db.session_factory()() as session:
        assert await hand_outs.count(session, OWNER) == 0, "the claim went back, not held for its lease"
        reply = await session_kinds.hand_out(session, OWNER, "session-2")
    assert reply["item"]["id"] == job, "the next ask is handed the item"


async def test_the_claims_of_a_pass_are_one_transaction_so_a_pass_that_fails_midway_keeps_none_of_them(
        world, sent, monkeypatch):
    """`decide` claims for each lane in the one transaction its caller commits, and the commands go only after that
    commit. A claim committed before the pass was whole would be held for its whole lease with no command sent for it
    (the lane would wait for its slow check-in), so the ask of the second lane does not end the first lane's
    transaction as a runner's own ask does."""
    await parse_job(age=timedelta(minutes=2))
    await parse_job()
    await _idle("session-1", "session-2")
    asked: list[str] = []
    real = hand_outs.lock

    async def lock(session, owner, kind):
        asked.append(kind)
        if len(asked) == 2:
            raise RuntimeError("the database went away under the second lane's ask")
        return await real(session, owner, kind)

    monkeypatch.setattr(hand_outs, "lock", lock)
    async with db.session_factory()() as session:
        with pytest.raises(RuntimeError):
            await dispatcher.decide(session, OWNER, ["session-1", "session-2"])
        await session.rollback()
    assert len(asked) == 2, "the first lane was claimed for, and the second lane's ask is the one that failed"
    async with db.session_factory()() as session:
        assert await hand_outs.count(session, OWNER) == 0, "the first lane's claim was never committed"
    assert sent == []


async def test_a_pass_that_found_nothing_waits_a_minute_before_it_looks_again(world, sent):
    await _idle("session-1")
    assert await dispatcher.dispatch_pass(OWNER, NOW) == 0
    await parse_job()
    assert await dispatcher.dispatch_pass(OWNER, NOW + timedelta(seconds=30)) == 0 and sent == [], "quiet"
    assert await dispatcher.dispatch_pass(OWNER, NOW + dispatcher.QUIET) == 1, "a minute on, it looks and finds it"


async def test_a_pass_with_no_wake_address_commands_no_one(world, sent, monkeypatch):
    monkeypatch.setattr(settings, "wake_url", "")
    await parse_job()
    await _idle("session-1")
    assert await dispatcher.dispatch_pass(OWNER) == 0 and sent == []
    async with db.session_factory()() as session:
        assert await hand_outs.count(session, OWNER) == 0, "nothing was claimed for a command that cannot go"


def test_a_pool_lane_is_commanded_on_its_own_port_beside_the_first(monkeypatch):
    monkeypatch.setattr(settings, "wake_url", "http://127.0.0.1:8651/wake")
    assert wake.runner_url("session-1") == "http://127.0.0.1:8651/wake"
    assert wake.runner_url("session-4") == "http://127.0.0.1:8654/wake"
    assert wake.runner_url("analysis-1") is None, "a lane that is not the pool's has no port to command"
    monkeypatch.setattr(settings, "wake_url", "")
    assert wake.runner_url("session-2") is None


# --- the fallback check-in ----------


async def test_the_check_in_of_a_lane_whose_command_was_lost_is_answered_with_the_claim_made_for_it(world, sent):
    job = await parse_job()
    await _idle("session-1")
    await dispatcher.dispatch_pass(OWNER)
    other = await parse_job()
    async with db.session_factory()() as session:
        again = await session_kinds.hand_out(session, OWNER, "session-1")
        await session.commit()
    assert again["claim"] == sent[0]["claim"] and again["item"]["id"] == job, "the claim it was told of, never a kind"
    async with db.session_factory()() as session:
        another = await session_kinds.hand_out(session, OWNER, "session-2")
    assert another["item"]["id"] == other, "and the claim is held for it, not for the next lane"


async def test_a_claim_nobody_takes_lapses_and_the_dispatcher_hands_the_item_to_a_lane_again(world, sent):
    job = await parse_job()
    await _idle("session-1")
    await dispatcher.dispatch_pass(OWNER)
    async with db.session_factory()() as session:
        row = (await session.scalars(select(HandOut))).one()
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await dispatcher.dispatch_pass(OWNER) == 1
    assert [c["item"]["id"] for c in sent] == [job, job]


# --- the housekeeping and the dispatcher share one cycle ----------


async def test_the_dispatcher_claims_what_the_housekeeping_made_ready_without_settling_anything_itself(world, sent):
    await parse_job()
    await _idle("session-1")
    async with db.session_factory()() as session:
        await housekeeping.settle(session, OWNER)
        await session.commit()
    assert await dispatcher.dispatch_pass(OWNER) == 1


async def test_a_lane_started_for_a_command_takes_its_claim_through_its_own_take_next_call(world, sent):
    job = await parse_job()
    await _idle("session-1")
    await dispatcher.dispatch_pass(OWNER)
    async with db.session_factory()() as session:
        got = await role_parse.take_next(session, OWNER, session_id="parser-1", claim=sent[0]["claim"])
        await session.commit()
    assert got["id"] == job


async def test_the_route_answers_a_lanes_check_in_with_the_claim_made_for_it(world, sent, scout_token):
    job = await parse_job()
    await _idle("session-1")
    await dispatcher.dispatch_pass(OWNER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reply = (await client.get("/api/v1/agent/work", params={"only": "any", "runner": "session-1"},
                                  headers=scout_token)).json()
    assert reply["next"] == "parser" and reply["item"]["id"] == job and reply["claim"] == sent[0]["claim"]


# --- a schedule or mission that fires does not wait for the next pass ----------


async def test_knocks_of_a_fired_mission_run_a_pass_at_once_even_inside_the_quiet_minute(world, sent, monkeypatch):
    from app.jobsearch.loops import schedule

    async def knock(*_a, **_k):
        return True

    monkeypatch.setattr(wake, "knock", knock)
    await _idle("session-1")
    assert await dispatcher.dispatch_pass(OWNER, NOW) == 0, "nothing waits: quiet for a minute"
    await parse_job()
    await schedule.send_knocks([("k", wake.SEARCH_STARTED)])
    assert [c["runner"] for c in sent] == ["session-1"], "the knock's pass looked at once and cleared the quiet"


async def test_no_knocks_run_no_pass(world, sent):
    from app.jobsearch.loops import schedule

    await _idle("session-1")
    await parse_job()
    await schedule.send_knocks([])
    assert sent == []


async def test_two_passes_at_once_never_command_one_lane_twice(world, sent):
    import asyncio

    await parse_job()
    await parse_job()
    await _idle("session-1")
    await asyncio.gather(dispatcher.dispatch_pass(OWNER), dispatcher.dispatch_pass(OWNER))
    assert [c["runner"] for c in sent] == ["session-1"]
