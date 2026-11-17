"""The runner's heartbeat and a tool call at the same moment. Every tool call takes
the connection's row first and then the search run's (`mcp_server._call`); the heartbeat took the run's first. Synthetic
data only."""

import asyncio

from httpx import ASGITransport, AsyncClient

from app import db
from app.agents import roster
from app.jobsearch.main import app
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import SearchRun
from app.settings import settings
from tests.support import load_profile


async def test_a_heartbeat_and_a_tool_call_at_the_same_moment_take_the_rows_in_one_order_and_neither_fails(
        js, monkeypatch):
    monkeypatch.setattr("app.jobsearch.routes.runner.token_ok", lambda header: True)
    async with db.session_factory()() as session:
        await load_profile(session, settings.owner)
        started = await run.start_round(session, settings.owner)
        await roster.ensure_roster(session, settings.owner)
        await session.commit()
    holding = asyncio.Event()

    async def tool_call():
        """What `mcp_server._call` does: the connection's row first, then, a moment later, the search run's."""
        async with db.session_factory()() as session:
            await roster.touch(session, settings.owner)
            holding.set()
            await asyncio.sleep(0.6)  # the heartbeat arrives and takes what it takes
            (await session.get(SearchRun, started.id)).summary = "the scout's last report"
            await session.commit()

    async def heartbeat():
        await holding.wait()
        await asyncio.sleep(0.2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/v1/agent/heartbeat", json={"session_id": "scout-abc", "state": "alive"})

    results = await asyncio.gather(tool_call(), heartbeat(), return_exceptions=True)
    assert results[0] is None, f"the tool call failed: {results[0]!r}"
    assert not isinstance(results[1], Exception), f"the heartbeat failed: {results[1]!r}"
    assert results[1].status_code == 200
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, started.id)
        assert row.session_id == "scout-abc" and row.summary == "the scout's last report"
