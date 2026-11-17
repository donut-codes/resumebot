"""An agent's ask for work (`GET /api/v1/agent/work`) looks on a reader and takes the writer's turn only to claim (the
owner, 2026-11-10: reading must not lock everything). The route's session is a writer's, so its first statement, a read
too, took the file's one turn: the read of a claim already made for the runner and the walk of every kind's waiting
work ran under it, and five runners asking with nothing waiting each held the writer for reads. Now the look is a
reader's; the kind's lock, the second read of the items and the claim are the writer's, as they were. Every name here
is made up, and no test reaches the network."""

import contextlib
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app import db
from app.jobsearch import off_turn
from app.jobsearch.main import app
from app.jobsearch.work import hand_outs, session_kinds
from app.settings import settings
from tests.builders import parse_job
from tests.support import OWNER, load_profile


@pytest.fixture
async def client(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def writer():
    """What the writers did from here on: `asked`, one entry for each writer's turn asked for (`db.turn_watchers`), and
    `turns`, one for each that ended (`db.turn_ends`)."""
    seen = SimpleNamespace(asked=[], turns=[])

    def asked(waited: float) -> None:
        seen.asked.append(waited)

    def ended(_waited: float, held: float | None) -> None:
        seen.turns.append(held)

    db.turn_watchers.append(asked)
    db.turn_ends.append(ended)
    try:
        yield seen
    finally:
        db.turn_watchers.remove(asked)
        db.turn_ends.remove(ended)


async def _ask(client, scout_token, runner: str = "session-1") -> dict:
    reply = await client.get("/api/v1/agent/work", params={"only": "any", "runner": runner}, headers=scout_token)
    assert reply.status_code == 200
    return reply.json()


def _looked_in(monkeypatch) -> dict[str, list[bool]]:
    """Whether each look of the ask (the claim already made for the runner, the walk) was given a reader's session."""
    seen: dict[str, list[bool]] = {"pending": [], "walk": []}
    pending, walk = hand_outs.pending, session_kinds._walk

    async def spy_pending(session, *args, **more):
        seen["pending"].append(db.is_reader(session))
        return await pending(session, *args, **more)

    async def spy_walk(session, *args, **more):
        seen["walk"].append(db.is_reader(session))
        return await walk(session, *args, **more)

    monkeypatch.setattr(hand_outs, "pending", spy_pending)
    monkeypatch.setattr(session_kinds, "_walk", spy_walk)
    return seen


async def test_an_ask_with_nothing_waiting_asks_for_no_writers_turn(client, scout_token, writer):
    # The ask that begins a standing ask owes a write (the to-do for a pull); the asks while it stands owe none.
    assert await _ask(client, scout_token) == {"next": None}
    writer.asked.clear()
    writer.turns.clear()
    assert await _ask(client, scout_token) == {"next": None}
    assert writer.asked == [] and writer.turns == [], "it only looked, so it never asked for the writer's turn"


async def test_an_ask_that_claims_takes_one_turn_and_walks_on_a_reader(client, scout_token, writer, monkeypatch):
    assert await _ask(client, scout_token) == {"next": None}
    job = await parse_job()
    looked = _looked_in(monkeypatch)
    writer.asked.clear()
    writer.turns.clear()
    asked = await _ask(client, scout_token)
    assert asked["next"] == "parser" and asked["item"]["ids"] == [job]
    assert looked == {"pending": [True], "walk": [True]}, "the claim made already and the walk were read on a reader"
    assert len(writer.asked) == 1 and len(writer.turns) == 1, "one writer's turn: the lock, the items again, the claim"
    async with db.session_factory()() as session:
        assert await hand_outs.count(session, OWNER, session_kinds.PARSER) == 1


async def test_a_claim_already_made_for_the_runner_is_answered_with_no_writers_turn(client, scout_token, writer):
    job = await parse_job()
    first = await _ask(client, scout_token)
    assert first["item"]["ids"] == [job]
    writer.asked.clear()
    writer.turns.clear()
    assert await _ask(client, scout_token) == first, "the same claim again: no session has taken it"
    assert writer.asked == [] and writer.turns == [], "the answer was read, and nothing was written"


async def test_of_two_asks_for_one_item_one_wins_and_the_other_is_told_nothing(client, scout_token, monkeypatch):
    assert await _ask(client, scout_token) == {"next": None}
    job = await parse_job()
    real = off_turn.reading
    others: list[dict] = []

    @contextlib.asynccontextmanager
    async def reading(session, **more):
        async with real(session, **more) as reader:
            yield reader
        if not others:  # the first ask has looked and holds nothing yet: another runner asks, and claims
            others.append({})
            others[0] = await _ask(client, scout_token, "session-2")

    monkeypatch.setattr(off_turn, "reading", reading)
    slow = await _ask(client, scout_token)
    assert others[0]["next"] == "parser" and others[0]["item"]["ids"] == [job], "the ask that claimed first has it"
    assert slow == {"next": None}, "the one that looked before the claim reads the items again and finds none"
    async with db.session_factory()() as session:
        assert await hand_outs.count(session, OWNER, session_kinds.PARSER) == 1
