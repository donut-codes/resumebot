"""Whole-table id maps are decoded in slices, and each answers what the plain read answered. Everything here is made up.

A read of every role's id (and the copy it points to, the ranker's background postings, the roles that have a rank)
built thousands of rows in one stretch, which held the event loop for the whole decode; now the loop is given back
after each slice (`off_turn.SLICE` rows)."""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import off_turn, stages
from app.jobsearch.fit import classifier
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting, RoleRank
from app.jobsearch.pipeline import deck
from app.jobsearch.postings import cleansed
from tests.support import OWNER, load_profile

N = 2 * off_turn.SLICE + 20  # three slices: 250, 250, 20
SLICES = 3


def _sleeps_in(monkeypatch, name: str) -> list[tuple[int, int]]:
    """Wrap `off_turn.<name>`: for each call, (rows returned, times the loop was given back during the call)."""
    real, sleeps, seen = getattr(off_turn, name), [0], []
    real_sleep = off_turn.asyncio.sleep

    async def sleep(delay, *args):
        sleeps[0] += 1
        await real_sleep(delay, *args)

    async def spy(*args, **kwargs):
        before = sleeps[0]
        out = await real(*args, **kwargs)
        seen.append((len(out), sleeps[0] - before))
        return out

    monkeypatch.setattr(off_turn.asyncio, "sleep", sleep)
    monkeypatch.setattr(off_turn, name, spy)
    return seen


async def _world(*, ranked: bool) -> dict[uuid.UUID, uuid.UUID]:
    """N roles, each with a posting that names its copy, and a rank when `ranked`; the copy of each role by role id."""
    now, source = datetime.now(UTC), uuid.uuid4()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        roles = [Opportunity(owner=OWNER, slug=f"r{i}", company=f"Firm {i}", title=f"Title {i}", origin="scout",
                             status=stages.FIRST_IMPRESSION, match_key=f"firm {i}|r{i}", found=date.today())
                 for i in range(N)]
        raws = [RawPosting(owner=OWNER, source_id=source, reader="legacy", kind="legacy", ext_id=f"e{i}",
                           fetched_at=now, body=b"x", body_sha256=f"{i:064x}", body_bytes=1, no_raw=True)
                for i in range(N)]
        session.add_all(roles + raws)
        await session.flush()
        copies = [CleansedPosting(owner=OWNER, raw_id=raw.id, cleaner_version=1, text_clean="words") for raw in raws]
        session.add_all(copies)
        await session.flush()
        session.add_all(JobPosting(
            owner=OWNER, employer_id=source, ats="scout", ext_id=f"e{i}", company="Firm", title="Lead",
            match_key=f"e{i}", first_seen=now, last_seen=now, links=[], opportunity_id=roles[i].id,
            current_cleansed_id=copies[i].id) for i in range(N))
        if ranked:
            session.add_all(RoleRank(owner=OWNER, role_id=roles[i].id, cleansed_id=copies[i].id, profile_version=1,
                                     fit=i % 101, why="") for i in range(N))
        await session.commit()
        return {roles[i].id: copies[i].id for i in range(N)}


@pytest.mark.asyncio
async def test_rows_in_slices_answers_the_plain_read_and_gives_the_loop_back_after_each_slice(js, monkeypatch):
    await _world(ranked=False)
    stmt = select(Opportunity.id, Opportunity.slug).order_by(Opportunity.slug)
    async with db.session_factory()() as session:
        plain = [tuple(r) for r in (await session.execute(stmt)).all()]
    calls = []
    real = off_turn.asyncio.sleep

    async def sleep(delay, *args):
        calls.append(1)
        await real(delay, *args)

    monkeypatch.setattr(off_turn.asyncio, "sleep", sleep)
    async with db.session_factory()() as session:
        sliced = await off_turn.rows_in_slices(session, stmt, rows=100)
    assert [tuple(r) for r in sliced] == plain
    assert len(plain) == N and len(calls) == 6


@pytest.mark.asyncio
async def test_current_for_roles_is_decoded_in_slices(js, monkeypatch):
    held = await _world(ranked=False)
    seen = _sleeps_in(monkeypatch, "rows_in_slices")
    async with db.session_factory()() as session:
        got = await cleansed.current_for_roles(session, OWNER)
        plain = {r: c for r, c in (await session.execute(cleansed._current(OWNER))).all()}
    assert got == held == plain
    assert seen == [(N, SLICES)]


@pytest.mark.asyncio
async def test_the_ranked_ids_and_the_newest_ranks_are_decoded_in_slices(js, monkeypatch):
    held = await _world(ranked=True)
    ids = _sleeps_in(monkeypatch, "scalars_in_slices")
    async with db.session_factory()() as session:
        assert await deck.ranked_ids(session, OWNER) == set(held)
    assert ids == [(N, SLICES)]
    rows = _sleeps_in(monkeypatch, "rows_in_slices")
    async with db.session_factory()() as session:
        newest = await deck._newest(session, OWNER)
        fits = {r.role_id: r.fit for r in newest}
        by_slug = {slug: rid for rid, slug in (await session.execute(select(Opportunity.id, Opportunity.slug))).all()}
    assert len(newest) == N and fits == {by_slug[f"r{i}"]: i % 101 for i in range(N)}
    assert rows == [(N, SLICES)]


@pytest.mark.asyncio
async def test_the_background_sample_reads_its_ids_in_slices(js, monkeypatch):
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        session.add_all(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="scout", ext_id=f"b{i}", company="Firm", title=f"Lead {i}",
            match_key=f"b{i}", first_seen=now, last_seen=now, links=[], text=f"words {i}") for i in range(N))
        await session.commit()
    seen = _sleeps_in(monkeypatch, "scalars_in_slices")
    async with db.session_factory()() as session:
        out = await classifier.background(session, OWNER, 10_000.0, 0.0)
    assert seen == [(N, SLICES)]
    assert len(out) == N and {w for _, _, w in out} == {1.0}
    assert sorted(t for t, _, _ in out) == sorted(classifier.text_of(f"Lead {i}", None, f"words {i}") for i in range(N))
