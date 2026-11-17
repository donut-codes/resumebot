"""`GET /api/v1/agents` is a plain read (the owner, 2026-11-10: the page program runs no work). It takes no writer's
turn and writes no row, whatever state the roster is in: the engine's start makes the roster and brings its shipped
words up to date (`startup.run_startup_fixups`, the step "the roster"). Everything here is made up."""

from sqlalchemy import func, select

from app import db
from app.agents import roster
from app.jobsearch.loops.startup import run_startup_fixups
from app.models.agent import AgentConnection, RoleDef
from tests.support import OWNER

AGENTS = "/api/v1/agents"


async def _asked_while_reading(client) -> tuple[list[float], list[dict]]:
    """The turns asked for while the Agents page is read once, and what it answered."""
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    try:
        answered = await client.get(AGENTS)
    finally:
        db.turn_watchers.remove(asked.append)
    assert answered.status_code == 200, answered.text
    return asked, answered.json()


async def _rows() -> tuple[int, int]:
    async with db.read_session_factory()() as session:
        roles = await session.scalar(select(func.count()).select_from(RoleDef))
        conns = await session.scalar(select(func.count()).select_from(AgentConnection))
    return roles, conns


async def test_the_agents_page_before_the_first_start_takes_no_turn_and_makes_no_row(client):
    asked, agents = await _asked_while_reading(client)
    assert asked == [], "the read asked for the writer's turn"
    assert agents == [], "no roster yet: the engine's start makes it"
    assert await _rows() == (0, 0), "the read made rows"


async def test_the_agents_page_mends_no_older_words_and_takes_no_turn(client):
    async with db.session_factory()() as session:
        conn = await roster.ensure_roster(session, OWNER)
        (await session.get(RoleDef, roster.SCOUT)).about = "Older words."
        conn.description = "An older description."
        await session.commit()
    asked, agents = await _asked_while_reading(client)
    assert asked == [], "the read asked for the writer's turn"
    assert [a["description"] for a in agents] == ["An older description."], "the read shows the row as it stands"
    async with db.read_session_factory()() as session:
        assert (await session.get(RoleDef, roster.SCOUT)).about == "Older words.", "the read mended the role"
        kept = await roster.connection_by_slug(session, OWNER, roster.SCOUT)
        assert kept.description == "An older description.", "the read mended the connection"


async def test_the_start_makes_the_roster_the_agents_page_reads(client):
    async with db.session_factory()() as session:
        await run_startup_fixups(session, OWNER)
        await session.commit()
    asked, agents = await _asked_while_reading(client)
    assert asked == []
    assert [a["name"] for a in agents] == ["Scout"]
    assert agents[0]["roles"] == [roster.SCOUT, roster.ANALYST, roster.WRITER]
    assert agents[0]["description"] == roster.SCOUT_DESCRIPTION
