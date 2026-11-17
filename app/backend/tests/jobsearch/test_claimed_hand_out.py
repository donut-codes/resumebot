"""The app hands out work and claims the exact item (docs/PRD.md path 47, step 1). An ask for work (`GET /agent/work`)
names a kind and claims the items for the runner that asked, in the same transaction; the session started for it names
the claim (`X-Claim`) and its own take-next call returns exactly those items. Two asks never get the same item, and an
ask only reads: settling steps, failing late jobs and starting the pool's next job are the app's own pass
(`housekeeping.settle`). Synthetic data only."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.jobs import check as role_check
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.main import app
from app.jobsearch.missions import catch_up
from app.jobsearch.models import (
    HandOut,
    ParseJob,
    SearchRun,
)
from app.jobsearch.pool import pull as pool_pull
from app.jobsearch.research import analysis
from app.jobsearch.work import hand_outs, session_kinds, tagging
from app.settings import settings
from tests.builders import parse_job, settle_housekeeping
from tests.lock_notes import NOTE, named_locks_in_the_timeline  # noqa: F401  (a fixture, used by name)
from tests.support import OWNER, load_profile

NOW = datetime.now(UTC)


@pytest.fixture
async def client(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _ask(client, scout_token, **params) -> dict:
    return (await client.get("/api/v1/agent/work", params=params, headers=scout_token)).json()


async def _in_session(body):
    """Run `body(session)` the way a tool call does: its own transaction, committed."""
    async with db.session_factory()() as session:
        out = await body(session)
        await session.commit()
        return out


# --- parser ----------


async def test_two_asks_are_never_told_the_same_parse_job_and_a_session_gets_the_job_claimed_for_it(
        client, scout_token):
    older, newer = await parse_job(age=timedelta(minutes=2)), await parse_job()
    first, second = await _ask(client, scout_token), await _ask(client, scout_token)
    assert [first["next"], second["next"]] == ["parser", "parser"]
    assert first["item"]["id"] == older and second["item"]["id"] == newer, "each ask was handed a job of its own"
    assert (await _ask(client, scout_token))["next"] is None, "both are claimed: nothing is left to hand out"
    # The second ask's session starts first and takes its own job, not the oldest.
    got = await _in_session(lambda s: role_parse.take_next(s, OWNER, session_id="parser-2", claim=second["claim"]))
    assert got["id"] == newer
    got = await _in_session(lambda s: role_parse.take_next(s, OWNER, session_id="parser-1", claim=first["claim"]))
    assert got["id"] == older, "and the first is not told nothing for having been later"


async def test_a_parse_job_claimed_for_a_session_is_not_taken_by_one_that_names_no_claim(client, scout_token):
    job = await parse_job()
    claimed = await _ask(client, scout_token)
    assert claimed["item"]["ids"] == [job]
    got = await _in_session(lambda s: role_parse.take_next(s, OWNER, session_id="parser-x"))
    assert got.get("done"), "a session of no claim finds nothing that another session was started for"
    got = await _in_session(lambda s: role_parse.take_next(s, OWNER, session_id="parser-y", claim=claimed["claim"]))
    assert got["id"] == job


async def test_asks_made_at_once_are_told_a_parse_job_once(client, scout_token):
    await parse_job()
    answers = await asyncio.gather(*(_ask(client, scout_token) for _ in range(5)))
    assert sorted(a["next"] or "" for a in answers).count("parser") == 1
    async with db.session_factory()() as session:
        assert await hand_outs.count(session, OWNER, session_kinds.PARSER) == 1


async def test_an_ask_for_work_reads_its_walk_before_it_holds_anything_and_claims_in_its_own_short_transaction(
        client, scout_token, transactions):
    """The ask's walk (the rungs, their limits and gates) is Python between reads, a second or so a poll; the claim is
    one insert. The walk's transaction ends before the kind's lock is taken and the claim is written, so nothing is
    held through the walk and the items are read again under the lock (docs/PRD.md 47)."""
    job = await parse_job()
    transactions.clear()
    asked = await _ask(client, scout_token)
    assert asked["next"] == "parser" and asked["item"]["id"] == job
    seen = transactions.timeline
    lock = next(i for i, s in enumerate(seen) if s.startswith(NOTE))
    assert transactions.END in seen[:lock], "the transaction of the walk ended before the claim's lock was taken"
    walk = seen[:seen.index(transactions.END)]
    assert walk and not transactions.writes(walk), "and the walk only read"
    assert any("INSERT INTO js_hand_outs" in s for s in seen[lock:]), "the claim was made after the lock"
    async with db.session_factory()() as session:
        assert await hand_outs.count(session, OWNER, session_kinds.PARSER) == 1


# --- search ----------


async def test_the_open_search_is_claimed_for_one_ask_and_its_session_start_takes_the_claim(client, scout_token):
    async with db.session_factory()() as session:
        session.add(SearchRun(owner=OWNER, status="open", started_by="manual"))
        await session.commit()
    first = await _ask(client, scout_token)
    assert first["next"] == "search" and first["claim"]
    assert (await _ask(client, scout_token))["next"] is None, "a second runner is not sent to it"
    started = await client.post("/api/v1/agent/heartbeat", headers=scout_token, json={
        "session_id": "scout-aaaaaaaaaaaa", "state": "started", "claim": first["claim"]})
    assert started.status_code == 200
    async with db.session_factory()() as session:
        row = await session.scalar(select(HandOut).where(HandOut.id == uuid.UUID(first["claim"])))
        assert row.session_id == "scout-aaaaaaaaaaaa" and row.taken_at is not None


# --- a claim that nobody takes lapses; the ask only reads ----------


async def test_a_claim_no_session_takes_lapses_with_its_lease_and_the_item_is_handed_out_again(client, scout_token):
    job = await parse_job()
    assert (await _ask(client, scout_token))["item"]["id"] == job
    async with db.session_factory()() as session:
        row = (await session.scalars(select(HandOut))).one()
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    again = await _ask(client, scout_token)
    assert again["next"] == "parser" and again["item"]["id"] == job


async def test_an_ask_settles_nothing_expires_nothing_and_starts_nothing(client, scout_token, monkeypatch):
    # Everything the ask used to do on the way is waiting to be done: a late job, an open search, a catch-up.
    late = await parse_job(age=timedelta(hours=2))
    async with db.session_factory()() as session:
        session.add(SearchRun(owner=OWNER, status="open", started_by="manual"))
        await session.commit()

    def forbidden(name):
        async def refuse(*args, **kwargs):
            raise AssertionError(f"the ask ran {name}")
        return refuse

    for module, name in ((role_parse, "expire"), (role_check, "expire"), (tagging, "settle"),
                         (pool_pull, "start_next"),
                         (analysis, "settle_stuck")):
        monkeypatch.setattr(module, name, forbidden(f"{module.__name__}.{name}"))
    monkeypatch.setattr(roster, "resume_if_stale", lambda *a, **k: (_ for _ in ()).throw(AssertionError("resume")))
    from app.jobsearch.browser import leases as browser_leases
    from app.jobsearch.browser import queue as browser_queue
    from app.jobsearch.gaps import drafts as gap_drafts
    from app.jobsearch.gaps import runs as find_gaps
    for module, name in ((catch_up, "advance"), (browser_leases, "expire"), (browser_queue, "refresh"),
                         (gap_drafts, "expire"), (find_gaps, "expire")):
        monkeypatch.setattr(module, name, forbidden(f"{module.__name__}.{name}"))
    for params in ({}, {"only": "any", "runner": "session-1"}):
        await _ask(client, scout_token, **params)  # none of them settles, expires or starts anything
    # The late job was not failed by asking: the housekeeping fails it.
    async with db.session_factory()() as session:
        assert (await session.get(ParseJob, uuid.UUID(late))).state == "waiting"


async def test_the_housekeeping_does_what_the_ask_no_longer_does(client, scout_token):
    late = await parse_job(age=timedelta(hours=2))
    async with db.session_factory()() as session:
        session.add(HandOut(owner=OWNER, kind="parser", items=[], lease_until=NOW - timedelta(hours=3),
                            created_at=NOW - timedelta(hours=3)))
        await session.commit()
    did = await settle_housekeeping(OWNER)
    assert {"search_steps", "jobs", "browser", "tagging", "catch_up", "queued", "claims"} <= set(did), did
    async with db.session_factory()() as session:
        assert (await session.get(ParseJob, uuid.UUID(late))).state == "failed"
        assert await hand_outs.count(session, OWNER) == 0, "the lapsed claim was removed"
