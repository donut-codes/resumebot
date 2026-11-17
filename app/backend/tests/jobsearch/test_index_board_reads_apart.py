"""A board refresh reads apart from the writer's turn: the count of the postings a partial read speaks for
(`open_within`), the rows the store compares with (`existing`, `heads`) and the parse and copies `_prepare` needs run
on reader sessions, and the writer's turns are the writes alone. The answers are those of a refresh that read in its own
session, and of one run inside a savepoint (where nothing can be read apart). Everything here is made up."""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import db
from app.jobsearch import adapters
from app.jobsearch.adapters import Asked, Listing, _posting
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import CleansedPosting, Employer, JobPosting, RawPosting
from app.jobsearch.postings import index as postings
from tests.conftest import Transactions
from tests.support import EXAMPLE_SYSTEM, OWNER

NIGHT = datetime(2026, 11, 5, 5, 0, tzinfo=UTC)
EARLIER = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
NEW = 100  # more than the slice's 40 postings and 20 rows now, fewer than the 80 postings a slice held before


def _listed(n: int, version: int = 0, location: str = "Remote"):
    item = {"id": n, "title": f"Programs Role {n}", "version": version,
            "content": f"<p>Lead the programs team, number {n}, version {version}, and report to the board.</p>"}
    return _posting(raw=item, ext_id=str(n), title=item["title"], location=location, work_mode="remote",
                    url=f"https://boards.example.test/acme/jobs/{n}", text=item["content"], posted="2026-11-01")


def _asked() -> Asked:
    return Asked(within=lambda p: p.location == "Remote")


def _read_listing() -> Listing:
    listed = [_listed(n, 1) for n in range(4)] + [_listed(n) for n in range(100, 100 + NEW)]
    return Listing(listed, asked=_asked())


@dataclass
class Board:
    answers: list = field(default_factory=list)

    async def read(self, fetch, board):
        return self.answers.pop(0)

    async def detail(self, fetch, board, posting):
        return {}


def _fetch():
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


async def _employer(session, name: str) -> Employer:
    employer = await employers.find_or_make(session, OWNER, name)
    employer.ats, employer.board = EXAMPLE_SYSTEM, {"slug": name.lower()}
    await session.flush()
    # Five open Remote postings (0 to 3 and 6) and two Elsewhere (4, 5), each with a raw item row.
    seed = [_listed(n, 0, "Elsewhere" if n in (4, 5) else "Remote") for n in range(7)]
    await postings.store(session, OWNER, Listing(seed), source_id=employer.id, ats_name=EXAMPLE_SYSTEM,
                         company=name, now=EARLIER, counts=dict.fromkeys(("seen", "new", "closed", "reopened"), 0),
                         sliced=True)
    return employer


async def _reference_open_within(session, source_id, asked) -> int:
    """The count as a Row per posting, the way it was first written."""
    rows = (await session.execute(select(JobPosting.location, JobPosting.work_mode).where(
        JobPosting.owner == OWNER, JobPosting.employer_id == source_id, JobPosting.closed_at.is_(None)))).all()
    return sum(1 for row in rows if asked.covers(row))


async def _dump(session, employer_id) -> dict:
    jobs = (await session.execute(select(
        JobPosting.ext_id, JobPosting.title, JobPosting.location, JobPosting.work_mode, JobPosting.first_seen,
        JobPosting.last_seen, JobPosting.closed_at).where(JobPosting.employer_id == employer_id)
        .order_by(JobPosting.ext_id))).all()
    raws = (await session.execute(select(RawPosting.ext_id, RawPosting.kind, RawPosting.body_sha256).where(
        RawPosting.source_id == employer_id).order_by(RawPosting.seq))).all()
    cleansed = (await session.execute(select(RawPosting.ext_id, CleansedPosting.cleaner_version).join(
        RawPosting, RawPosting.id == CleansedPosting.raw_id).where(RawPosting.source_id == employer_id)
        .order_by(RawPosting.seq))).all()
    employer = await session.get(Employer, employer_id)
    return {"jobs": [tuple(r) for r in jobs], "raws": [tuple(r) for r in raws],
            "cleansed": [tuple(r) for r in cleansed],
            "employer": (employer.open_roles, employer.fetch_ok, employer.fetch_note)}


MARKERS = {"open_within": "count(", "heads": "js_raw_postings.body_sha256", "parse": "js_raw_postings.parsed",
           "current_copies": "js_cleansed_postings.raw_id", "existing": "ORDER BY js_postings.ext_id"}


async def test_a_board_refresh_reads_apart_and_gives_the_answers_it_gave_in_its_own_session(js, monkeypatch,
                                                                                            transactions):
    board = Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: board})
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        birch = await _employer(session, "Birch")
        await session.commit()
        acme_id, birch_id = acme.id, birch.id
    board.answers = [_read_listing(), _read_listing()]

    # The refresh of Acme in its own session, with a spy on which sessions send which statements.
    real_factory = db.read_session_factory
    seen: list[tuple[bool, str]] = []

    def tagged():
        make = real_factory()

        def new(*args, **more):
            reader = make(*args, **more)
            reader.sync_session.info["reader"] = True
            return reader

        return new

    def note(state):
        seen.append((bool(state.session.info.get("reader")), str(state.statement).replace("\n", " ")))

    monkeypatch.setattr(db, "read_session_factory", tagged)
    event.listen(Session, "do_orm_execute", note)
    transactions.clear()
    try:
        async with db.session_factory()() as session:
            first = await postings.refresh(session, OWNER, _fetch(), only=[await session.get(Employer, acme_id)],
                                           now=NIGHT)
            await session.commit()
    finally:
        event.remove(Session, "do_orm_execute", note)
        monkeypatch.setattr(db, "read_session_factory", real_factory)
    writers = [each for each in transactions.ended if Transactions.writes(each)]

    for name, marker in MARKERS.items():
        on_readers = [s for reader, s in seen if reader and marker in s]
        on_writers = [s for reader, s in seen if not reader and marker in s]
        assert on_readers, f"{name} ran on no reader's session"
        if name == "heads":  # `keep_prepared` looks at the heads again in the turn, by design: the first look is apart
            first_look = next(reader for reader, s in seen if marker in s)
            assert first_look, "the board's heads were first read on a writer's session"
            continue
        assert not on_writers, f"{name} ran on a writer's session: {on_writers[0][:200]}"
    assert len(writers) >= math.ceil(NEW / 40)
    assert len(writers) >= math.ceil(NEW * 3 / 20), "the slices are cut at the rows they write"

    # The same board in a savepoint of the caller's session, where nothing can be read apart.
    async with db.session_factory()() as session:
        async with session.begin_nested():
            second = await postings.refresh(session, OWNER, _fetch(), only=[await session.get(Employer, birch_id)],
                                            now=NIGHT)
        await session.commit()

    async with db.session_factory()() as session:
        acme_dump, birch_dump = await _dump(session, acme_id), await _dump(session, birch_id)
        assert acme_dump["employer"][0] == await _reference_open_within(session, acme_id, _asked()) == NEW + 4
    assert acme_dump == birch_dump
    closed_at = {job[0]: job[6] for job in acme_dump["jobs"]}
    assert closed_at["6"] is not None and closed_at["4"] is None  # 6 closed; the Elsewhere posting left alone
    keys = ("employers", "failed", "seen", "new", "closed", "reopened", "faults")
    assert {k: first[k] for k in keys} == {k: second[k] for k in keys}
    assert (first["seen"], first["new"], first["closed"]) == (NEW + 4, NEW, 1) and first["faults"] == []
