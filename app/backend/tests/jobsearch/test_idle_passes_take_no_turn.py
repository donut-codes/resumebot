"""Work with nothing to write takes no writer's turn (the owner, 2026-11-10: reading must not lock everything).

A writer's session takes the file's one turn at its first statement, a read too, so a pass that only looks and finds
nothing owed must look on a reader. Here every handler of the work queue (read from the table itself, `handlers.py`: a
handler added later is covered without a change here), the pool's pull and the runner's ask for work are each run on a
database that owes nothing, and any turn asked for (`db.turn_watchers`, `db.turn_ends`) fails the test.

The ones that take a turn when idle today are named in `STILL_TAKES_A_TURN`, each with what it does under the turn. The
test holds both ways: every other one takes no turn, and every named one still does, so a name cannot outlive its fix.
The dict only shrinks: the change that fixes one deletes its name. Every name here is made up, and no test reaches the
network."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.models import RoleRank
from app.jobsearch.postings import cleansed
from app.jobsearch.research import rank
from app.jobsearch.work import dispatcher, session_kinds
from app.jobsearch.work import hours as night
from app.jobsearch.work.queue import handlers, service, topics
from app.settings import settings
from tests.jobsearch.test_rank import _profile, _role
from tests.support import OWNER

#: One entry of the table: run it once, and say what it found to do (None: nothing).
Work = Callable[[], Awaitable[object]]
PULL = "session_kinds.pull"
ASK = "GET /api/v1/agent/work"

#: What takes the writer's turn with nothing owed today, by its name in the table (a handler's own topic, `PULL`,
#: `ASK`), and what it does under that turn. Only ever made shorter: the change that moves the look to a reader deletes
#: the name, and the test then holds it to no turn.
STILL_TAKES_A_TURN: dict[str, str] = {
    handlers.TURN_HANDLER.topic: "the pipeline's turn runs each deck step in a writer's session of its own: six turns "
                                 "for the steps' reads of the roles and the profile in force, and no write",
    handlers.WALK_HANDLER.topic: "the dispatcher's pass reads the hand-outs waiting and the roster in a writer's "
                                 "session to find that no lane is idle",
    topics.TICK_MINUTE: "the minute's housekeeping parts each read under a turn of their own (the open search, the "
                        "late jobs, the tagging job, the catch-up, the leases), then the dispatcher's pass reads, and "
                        "the tick writes when housekeeping and the dispatcher last passed for the health door (a write "
                        "it owes every minute: `handlers.stamp_passes`)",
    topics.TICK_HOUR: "the hour's housekeeping parts each run under a turn of their own (deletes of old jobs and "
                      "claims that match no row, a read of the tabs), and the janitor counts old to-dos on a writer",
}


def _handlers() -> dict[str, handlers.Handler]:
    """Every handler the dispatcher has, once each, by its own topic: the table's, whatever it holds today."""
    every = (handlers.TURN_HANDLER, handlers.WALK_HANDLER, *handlers.HANDLERS.values())
    return {h.topic: h for h in every}


def _run(handler: handlers.Handler) -> handlers.Run:
    """A look the hour's sweep asked for, at one to-do of the handler's own topic. `swept` is empty: the hour tick's
    sweep is a write the hour owes whatever the data holds, and this file is about the looks that find nothing."""
    group = service.Group(owner=OWNER, topic=handler.topic, keys=(topics.ANY,), ids=(), causes=frozenset({"sweep"}),
                          attempts=0, waiting_since=datetime.now(UTC))
    return handlers.Run(OWNER, group, datetime.now(UTC), handlers.Tally())


async def _handle(handler: handlers.Handler) -> object:
    """Run the handler once. What it found to do, or nothing (every entry of the table answers so)."""
    outcome = await handler.run(_run(handler))
    return outcome if outcome.changed or outcome.more else None


async def _pull() -> object:
    """One pull as the work queue runs it: a session of its own, committed."""
    async with db.session_factory()() as session:
        started = await session_kinds.pull(session, OWNER)
        await session.commit()
    return started or None


def _table(client, scout_token) -> dict[str, Work]:
    """Everything this file holds to the rule, by name: each handler, the pull and the ask."""

    async def ask() -> object:
        reply = await client.get("/api/v1/agent/work?only=any&runner=session-1", headers=scout_token)
        assert reply.status_code == 200
        return None if reply.json() == {"next": None} else reply.json()

    table: dict[str, Work] = {
        topic: (lambda h=handler: _handle(h)) for topic, handler in _handlers().items()}
    return {**table, PULL: _pull, ASK: ask}


async def _turns(work: Work) -> list[str]:
    """Run `work`, which must find nothing to do, and say where each writer's turn it asked for came from (the app's
    innermost lines, outermost first); a turn that ended with no ask seen is listed too. Empty: it took none."""
    asked: list[str] = []
    ended: list[float | None] = []

    def watch(_waited: float) -> None:
        inside = [f"{name.relative_to(db.BACKEND_DIR).as_posix()}:{line}" for name, line in db._awaiting_frames()
                  if db.BACKEND_DIR / "app" in name.parents and name.name != "db.py"]
        asked.append(" > ".join(inside[-4:]) or "?")

    def end(_waited: float, held: float | None) -> None:
        ended.append(held)

    db.turn_watchers.append(watch)
    db.turn_ends.append(end)
    try:
        found = await work()
    finally:
        db.turn_watchers.remove(watch)
        db.turn_ends.remove(end)
    assert found is None, f"it found work on a database that owes none: {found}"
    return asked + ["a turn ended that no ask was seen for"] * max(0, len(ended) - len(asked))


@pytest.fixture
async def nothing_owed(js, monkeypatch):
    """A seeded, committed database that owes nothing: the owner's profile and agents, one role that holds its rank at
    the profile in force, a free pool session that asked a moment ago (so the pull looks), by day (so no search is the
    pull's to start), and a wake address (so the dispatcher's pass looks; no lane is idle, so nothing is sent)."""
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    monkeypatch.setattr(settings, "wake_url", "http://wake.example.test")
    dispatcher.forget()
    await _profile()
    role = await _role("ranked", status=stages.FIRST_IMPRESSION)
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        profile = await rank.ensure_profile(session, OWNER)
        copies = await cleansed.current_for_roles(session, OWNER)
        session.add(RoleRank(owner=OWNER, role_id=role, cleansed_id=copies[role], profile_version=profile.version,
                             fit=60, why="A programs role.\nIt fits on duties."))
        await session.commit()
    session_kinds._pool_asked[OWNER] = datetime.now(UTC)
    yield
    dispatcher.forget()


NAMES = (*_handlers(), PULL, ASK)


@pytest.mark.parametrize("name", NAMES)
async def test_with_nothing_owed_it_takes_no_writers_turn(name, nothing_owed, client, scout_token):
    work = _table(client, scout_token)[name]
    await work()  # what the seeding itself left to settle is settled: the look below is the idle one
    session_kinds._pool_asked[OWNER] = datetime.now(UTC)
    turns = await _turns(work)
    if name in STILL_TAKES_A_TURN:
        assert turns, (f"{name} takes no writer's turn with nothing owed any more: delete its name from "
                       f"STILL_TAKES_A_TURN, and this test holds it there")
    else:
        assert turns == [], f"{name} had nothing to write and asked for the writer's turn at: {turns}"


def test_every_name_excused_is_in_the_table():
    assert set(STILL_TAKES_A_TURN) <= set(NAMES), "a name here is of nothing this file runs: delete it"
    assert all(why.strip() for why in STILL_TAKES_A_TURN.values()), "each name says what it does under the turn"


# --- the test of the test ---------------------------------------------------------------------------------------------


async def _reads_on_the_writer(_run: handlers.Run) -> handlers.Outcome:
    async with db.session_factory()() as session:
        await session.scalar(select(RoleRank.id).limit(1))
        await session.commit()
    return handlers.Outcome(cheap=True)


async def _reads_on_a_reader(_run: handlers.Run) -> handlers.Outcome:
    async with db.read_session_factory()() as session:
        await session.scalar(select(RoleRank.id).limit(1))
    return handlers.Outcome(cheap=True)


def _throwaway(run) -> handlers.Handler:
    return handlers.Handler("test.throwaway", None, "db", 9, timedelta(0), run)


async def test_a_handler_that_reads_on_the_writer_is_caught(nothing_owed):
    handler = _throwaway(_reads_on_the_writer)
    turns = await _turns(lambda: _handle(handler))
    assert len(turns) == 1 and "test_idle_passes_take_no_turn" not in turns[0], turns


async def test_a_handler_that_reads_on_a_reader_passes(nothing_owed):
    handler = _throwaway(_reads_on_a_reader)
    assert await _turns(lambda: _handle(handler)) == []
