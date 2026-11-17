"""Reads that took the writer's session only by the type of their parameter (the owner, 2026-11-10: reading must not
lock everything). Each route here only reads, so it takes a reader's session (`db.ReadSession`) and asks for no turn.
Two checks: what the route depends on, and a call with the writer's turns recorded (`db.turn_watchers`,
`db.turn_ends`). Every name here is made up, and no test reaches the network."""

import contextlib
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute
from process_engine import versions
from sqlalchemy import func, select

from app import db
from app.agents import shipped_rule
from app.jobsearch.browser import leases as browser_leases
from app.jobsearch.browser.pace import LEASED, READ
from app.jobsearch.models import BrowserAttempt, BrowserItem, Opportunity
from tests.support import OWNER, load_profile

ROUND = "jobsearch_round"
MADE = datetime(2026, 1, 5, 12, tzinfo=UTC)
#: A lease that ran out long ago, and a moment after it for the engine's expiry: fixed, so no test reads the clock.
RAN_OUT = datetime(2020, 1, 1, tzinfo=UTC)
LATER = datetime(2026, 1, 1, tzinfo=UTC)
HOLDER = "browser_reader-0a1b2c3d"
#: (method, full path) of each read this file holds to a reader's session.
READS = [
    ("POST", "/api/v1/profile/roles/from-role/{subject_id}"),
    ("POST", "/api/v1/harness/processes/{key}/validate"),
    ("GET", "/api/v1/agent-kinds"),
    ("GET", "/api/v1/agent/browser/drain"),
    ("GET", "/api/v1/agent/browser/busy"),
]


def _route(method: str, path: str) -> APIRoute:
    """The app's route with this method and full path. The app keeps an included router as one entry of `app.routes`,
    so each entry's own routes are read, with the prefix of the include."""
    from app.jobsearch.main import app

    for included in app.routes:
        router = getattr(included, "original_router", None)
        if router is None:
            continue
        prefix = getattr(included.include_context, "prefix", "") or ""
        for route in router.routes:
            if isinstance(route, APIRoute) and prefix + route.path == path and method in route.methods:
                return route
    raise AssertionError(f"the app has no route {method} {path}")


def _sessions_of(route: APIRoute) -> set:
    """The session dependencies of a route, at any depth."""
    found, todo = set(), list(route.dependant.dependencies)
    while todo:
        one = todo.pop()
        todo.extend(one.dependencies)
        if one.call in (db.get_session, db.get_read_session, db.get_lifeline_session):
            found.add(one.call)
    return found


@contextlib.contextmanager
def _turns():
    """Every writer's turn asked for or ended while the block runs. The listeners only append."""
    seen: list[tuple[str, float]] = []

    def asked(seconds: float) -> None:
        seen.append(("asked", seconds))

    def ended(waited: float, held: float | None) -> None:
        seen.append(("ended", waited))

    db.turn_watchers.append(asked)
    db.turn_ends.append(ended)
    try:
        yield seen
    finally:
        db.turn_watchers.remove(asked)
        db.turn_ends.remove(ended)


@pytest.mark.parametrize(("method", "path"), READS)
def test_the_read_depends_on_the_readers_session_and_not_the_writers(method, path):
    assert _sessions_of(_route(method, path)) == {db.get_read_session}


async def test_the_owners_three_reads_answer_and_ask_for_no_turn(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await shipped_rule.settle(session)
        role = Opportunity(owner=OWNER, slug="read-types-1", company="Initech", title="Programs Director",
                           status="applied", fit=3, match_key="initech|programs director", origin="scout",
                           created_at=MADE)
        session.add(role)
        await session.commit()
        role_id = role.id
    with _turns() as turns:
        kinds = await client.get("/api/v1/agent-kinds")
        draft = await client.post(f"/api/v1/profile/roles/from-role/{role_id}")
        checked = await client.post(f"/api/v1/harness/processes/{ROUND}/validate",
                                    json={"xml": versions.shipped_xml(ROUND)})
    assert [r.status_code for r in (kinds, draft, checked)] == [200, 200, 200]
    assert turns == [], f"a read asked for the writer's turn: {turns}"
    assert isinstance(kinds.json(), list) and kinds.json()
    assert draft.json()["name"] == "Programs Director"
    assert checked.json()["ok"] is True


async def _a_lease_that_ran_out() -> uuid.UUID:
    """One browser item a session holds whose lease ran out long ago and that nobody has given back yet."""
    async with db.session_factory()() as session:
        item = BrowserItem(owner=OWNER, kind=READ, target="a-made-up-posting", domain="example.test",
                           link="https://example.test/jobs/1", state=LEASED, leased_by=HOLDER, lease_until=RAN_OUT,
                           next_at=RAN_OUT, created_at=RAN_OUT)
        session.add(item)
        await session.commit()
        return item.id


async def test_the_drain_and_busy_reads_count_a_lease_that_ran_out_and_give_nothing_back(client, scout_token):
    item_id = await _a_lease_that_ran_out()
    with _turns() as turns:
        drain = await client.get("/api/v1/agent/browser/drain", headers=scout_token)
        busy = await client.get("/api/v1/agent/browser/busy", headers=scout_token)
    assert [r.status_code for r in (drain, busy)] == [200, 200]
    assert turns == [], f"a read asked for the writer's turn: {turns}"
    assert drain.json()["holding"] == 1, "still held: the read gives nothing back"
    assert busy.json() == {"ok": True, "waiting": 0, "leased": 1}
    async with db.read_session_factory()() as session:
        row = await session.get(BrowserItem, item_id)
        assert row is not None and row.state == LEASED and row.leased_by == HOLDER
        assert await session.scalar(select(func.count(BrowserAttempt.id))) == 0, "nothing was logged"
    async with db.session_factory()() as session:  # the engine's minute tick does this (`housekeeping`, `leases`)
        assert await browser_leases.expire(session, OWNER, LATER) == 1
        await session.commit()
    after = await client.get("/api/v1/agent/browser/drain", headers=scout_token)
    assert after.json()["holding"] == 0, "the engine gave it back"
