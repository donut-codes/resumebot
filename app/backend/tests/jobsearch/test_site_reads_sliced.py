"""A site read by its skill stores its board in slices, as the built-in boards' read does (docs/PRD.md 28.8; the
SQLite port, round 4): the recipe's and the employer's writes are committed first and the board is then written a
slice at a time, so no writer turn holds the whole board. The rows written are the same as the whole store's. Real
index code, a made-up job board behind a mock transport; no network."""

import re
from datetime import UTC, datetime

import httpx
from sqlalchemy import select, text

from app import db
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import JobPosting
from app.jobsearch.postings import index as postings
from app.jobsearch.postings import site_reads
from app.sites import skills, where
from tests.conftest import Transactions
from tests.support import OWNER

NOW = datetime(2026, 11, 5, 5, 0, tzinfo=UTC)
HOST = "jobs.example.test"
COUNT = 200
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
STAMP = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d+)?")
TABLES = ("js_todos", "js_cleansed_postings", "js_raw_postings", "js_postings")
RECIPE = {"kind": "request", "method": "GET", "url": f"https://{HOST}/api/jobs", "items": "data",
          "fields": {"title": "name", "url": "path", "ext_id": "id", "location": "loc"},
          "url_prefix": f"https://{HOST}"}


def _fetch() -> Fetcher:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/jobs":
            return httpx.Response(404)
        rows = [{"id": n, "name": f"Programs Role {n}", "path": f"/jobs/{n}", "loc": "Remote"} for n in range(COUNT)]
        return httpx.Response(200, json={"data": rows})

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(answer)), per_host_seconds=0)


async def _world() -> None:
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, f"https://{HOST}/remote", "job_board")
        await skills.save_skill(session, OWNER, site.id, RECIPE)
        await session.commit()


async def _read(session) -> dict:
    _built_in, reads = await site_reads.plan(session, OWNER, [])
    assert len(reads) == 1
    return await site_reads.refresh(session, OWNER, _fetch(), reads, NOW)


async def _snapshot() -> dict:
    """Every row of the four topics, without the ids and stamps a run makes up, in a fixed order."""
    out = {}
    async with db.session_factory()() as session:
        for table in TABLES:
            where_ = " WHERE topic LIKE 'posting.%'" if table == "js_todos" else ""
            result = await session.execute(text(f"SELECT * FROM {table}{where_}"))
            keys = [k for k in result.keys()
                    if k not in ("id", "seq") and not k.endswith("_id") and not k.endswith("_at")]
            rows = [STAMP.sub("T", UUID.sub("ID", repr([row._mapping[k] for k in keys]))) for row in result.all()]
            # A todo is asked for once a flush (a slice's, where the whole store's is one): the topics and keys are
            # the same, the number of identical asks is not.
            out[table] = sorted(set(rows) if table == "js_todos" else rows)
    return out


async def _wipe() -> None:
    async with db.session_factory()() as session:
        for table in TABLES:
            await session.execute(text(f"DELETE FROM {table}" + (" WHERE topic LIKE 'posting.%'"
                                                                 if table == "js_todos" else "")))
        await session.commit()


async def test_a_sites_board_is_stored_in_slices_and_writes_the_rows_the_whole_store_writes(
        js, monkeypatch, transactions):
    await _world()
    # The whole store, as the code did before: the reference.
    real = postings.store

    async def whole(*args, **kwargs):
        return await real(*args, **{**kwargs, "sliced": False})

    monkeypatch.setattr(postings, "store", whole)
    async with db.session_factory()() as session:
        reference_counts = await _read(session)
        await session.commit()
    reference = await _snapshot()
    assert len(reference["js_postings"]) == COUNT
    monkeypatch.setattr(postings, "store", real)

    # Inside a savepoint nothing is released: the same rows.
    await _wipe()
    async with db.session_factory()() as session:
        async with session.begin_nested():
            nested_counts = await _read(session)
        await session.commit()
    assert await _snapshot() == reference
    assert nested_counts == reference_counts

    # Outside one the board is written in slices, each its own writer turn.
    await _wipe()
    async with db.session_factory()() as session:
        transactions.clear()
        counts = await _read(session)
        await session.commit()
    assert await _snapshot() == reference
    assert counts == reference_counts
    writers = [each for each in transactions.ended if Transactions.writes(each)]
    assert len(writers) >= COUNT // postings.SLICE_SEEN, "the board is saved a slice at a time"
    assert max(len(each) for each in writers) <= 12 * postings.SLICE_SEEN // 8, max(writers, key=len)
    assert max(len(each) for each in writers) < COUNT, "no turn holds the whole board's statements"


async def test_a_slice_that_fails_keeps_the_slices_before_it_and_the_read_goes_on(js, monkeypatch):
    await _world()
    monkeypatch.setattr(postings, "SLICE_SEEN", 40)
    real = postings._write_slice
    calls = []

    async def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("a slice broke")
        return await real(*args, **kwargs)

    monkeypatch.setattr(postings, "_write_slice", failing)
    async with db.session_factory()() as session:
        counts = await _read(session)
        await session.commit()
    assert counts["failed"] == 1 and len(counts["faults"]) == 1
    assert "a slice broke" in counts["faults"][0]["why"] and counts["faults"][0]["kind"] == "failed"
    async with db.session_factory()() as session:
        kept = len((await session.scalars(select(JobPosting))).all())
    assert 0 < kept < COUNT, "the slices before the failed one stay saved"
