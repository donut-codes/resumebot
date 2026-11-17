"""A slow tool call must never freeze the app: the
door noted the scout's call on its connection row (`mcp_server._touch`) inside the tool's own transaction, so one tool
that ran for minutes held that row, and every heartbeat and every other call waited on it holding a pooled connection
until the pool had none left. The database now has one writer, so the thing a slow tool can hold is the writer's turn:
a tool that only reads (`reads=True`) holds none, and a heartbeat is answered while it runs; a tool that writes and
stays open does hold it, and a heartbeat is told the app is busy within the guard's limits instead of queueing (accepted
on SQLite, pending the owner's decision). Synthetic data only."""

import asyncio

import pytest
from sqlalchemy import text

from app import db
from app.agents import roster
from app.errors import Busy
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.work import session_lifecycle
from tests.support import DEFAULT_OWNER as OWNER


async def test_a_heartbeat_is_not_held_behind_a_slow_tool_call_that_only_reads(js, door):
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await session.commit()
    started, answered = asyncio.Event(), asyncio.Event()

    async def slow_tool(session, owner):  # a tool that takes a while, as a re-gate of many roles did live
        await session.execute(text("select 1"))
        started.set()
        # It takes as long as it takes the heartbeat to be answered. A heartbeat held behind this transaction is a
        # heartbeat waiting for a tool that waits for the heartbeat, so neither ends: the tool gives up after 30
        # seconds and the heartbeat's own limit (10) fails the test, with no stopwatch on how quickly it was answered.
        await asyncio.wait_for(answered.wait(), 30)
        return {"ok": True}

    async def heartbeat() -> None:
        await started.wait()
        async with db.session_factory()() as session:
            await session_lifecycle.settle(session, OWNER, {"state": "alive", "session_id": "scout-0001"})
            await session.commit()
        answered.set()

    called, _ = await asyncio.gather(mcp_server.door.call(door, "jobs.slow", slow_tool, reads=True),
                                     asyncio.wait_for(heartbeat(), 10))
    assert called["ok"] is True, "the heartbeat was answered while the tool's transaction was still open"


async def test_a_heartbeat_is_told_busy_while_a_slow_tool_that_writes_holds_the_writers_turn(js, door, monkeypatch):
    """The accepted SQLite behaviour (the freeze shape, as the writer's turn): a tool registered without
    `reads=True` holds the one writer turn from its first statement until it ends, so a heartbeat waits only as long as
    the lifeline's limit and is then told the app is busy, with nothing changed. It does not queue behind the tool."""
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await session.commit()
    monkeypatch.setattr(db, "LIFELINE_LOCK_TIMEOUT_MS", 100)
    started, answered = asyncio.Event(), asyncio.Event()

    async def slow_tool(session, owner):
        await session.execute(text("select 1"))  # a writer's session: this takes the turn
        started.set()
        await asyncio.wait_for(answered.wait(), 30)
        return {"ok": True}

    async def heartbeat() -> None:
        await started.wait()
        try:
            with pytest.raises(Busy):
                async with db.lifeline_session_factory()() as session:
                    await session_lifecycle.settle(session, OWNER, {"state": "alive", "session_id": "scout-0001"})
                    await session.commit()
        finally:
            answered.set()

    called, _ = await asyncio.gather(mcp_server.door.call(door, "jobs.slow", slow_tool),
                                     asyncio.wait_for(heartbeat(), 10))
    assert called["ok"] is True, "the tool itself was not disturbed"
