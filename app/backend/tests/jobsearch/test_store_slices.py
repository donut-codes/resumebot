"""A board's save is written a slice at a time (docs/PRD.md 28.8): the postings
and raw rows are read with nothing locked, what needs no row (the hash, the bytes, the cleaner's answer) is made with no
transaction open, and each slice of a board is locked, written and committed before the next, so no row is held for the
board. Real index code and synthetic boards; no network."""

import ast
import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import adapters
from app.jobsearch.adapters import Asked, Listing, _posting
from app.jobsearch.employers import core as employers
from app.jobsearch.models import CleansedPosting, Employer, JobPosting, RawPosting
from app.jobsearch.postings import cleanse, raw_copies
from app.jobsearch.postings import index as postings
from tests.conftest import Transactions
from tests.support import EXAMPLE_SYSTEM, OWNER

NIGHT = datetime(2026, 11, 5, 5, 0, tzinfo=UTC)
NOW_BEFORE = NIGHT - timedelta(days=1)


def _item(n: int, version: int = 0) -> dict:
    return {"id": n, "title": f"Programs Role {n}", "version": version,
            "content": f"<p>Lead the programs team, number {n}, version {version}, and report to the board.</p>"}


def _listed(n: int, version: int = 0, **more):
    item = _item(n, version)
    fields = {"ext_id": str(n), "title": item["title"], "location": "Remote", "work_mode": "remote",
              "url": f"https://boards.example.test/acme/jobs/{n}", "text": item["content"], "posted": "2026-11-01",
              **more}
    return _posting(raw=item, **fields)


def _listing(numbers, version: int = 0, **more) -> Listing:
    return Listing([_listed(n, version) for n in numbers], **more)


def _counts() -> dict:
    return dict.fromkeys(("seen", "new", "closed", "reopened"), 0)


async def _acme(session, name: str = "Acme") -> Employer:
    acme = await employers.find_or_make(session, OWNER, name)
    acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": name.lower()}
    await session.flush()
    return acme


async def _store(session, acme: Employer, listing, now=NIGHT, *, sliced=True, owner=OWNER, counts=None, **more) -> dict:
    counts = counts if counts is not None else _counts()
    await postings.store(session, owner, listing, source_id=acme.id, ats_name=EXAMPLE_SYSTEM, company=acme.name,
                         now=now,
                         counts=counts, sliced=sliced, **more)
    return counts


def _write_transactions(transactions: Transactions) -> list[list[str]]:
    return [each for each in transactions.ended if Transactions.writes(each)]


@dataclass
class Board:
    """A job system that answers what it is told, one read at a time."""

    answers: list = field(default_factory=list)

    async def read(self, fetch, board):
        return self.answers.pop(0)

    async def detail(self, fetch, board, posting):
        return {}


@pytest.fixture
def board(monkeypatch):
    stand_in = Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: stand_in})
    return stand_in


def _fetch():
    import httpx

    from app.jobsearch.fetch.polite import Fetcher

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


# --- the slices ----------------------------------------------------------------------------------------------------


async def test_a_board_of_a_thousand_postings_seen_again_is_saved_in_slices_each_a_few_statements(js, transactions):
    """The night's board store was one transaction for the whole board (a 10,000-posting board held its rows for 24
    seconds). A thousand postings seen again are 13 transactions of at most `SLICE_SEEN`, each a handful of statements:
    the budget, whatever else happens on the loop."""
    async with db.session_factory()() as session:
        acme = await _acme(session)
        session.add_all([JobPosting(owner=OWNER, employer_id=acme.id, ats=EXAMPLE_SYSTEM, ext_id=str(n), company="Acme",
                                    title=f"Programs Role {n}", first_seen=NOW_BEFORE, last_seen=NOW_BEFORE, links=[])
                         for n in range(1000)])
        await session.commit()
        transactions.clear()
        listing = Listing([_posting(ext_id=str(n), title=f"Programs Role {n}", location="Remote") for n in range(1000)])
        counts = await _store(session, acme, listing)
        await session.commit()
    writers = _write_transactions(transactions)
    assert counts["seen"] == 1000 and counts["new"] == 0
    assert len(writers) >= 1000 // postings.SLICE_SEEN, "the board is saved a slice at a time"
    assert max(len(each) for each in writers) <= 9, f"a slice is a few statements: {max(writers, key=len)}"



async def test_a_board_of_new_postings_is_saved_in_slices_of_rows_and_its_copies_are_made_outside_any_transaction(
        js, monkeypatch, transactions):
    """The cleaner's answer is 14 ms a changed posting, most of what a board costs: it is made just before the slice
    that saves it, with no transaction open (so no row waits on it), and a slice writes at most `SLICE_ROWS` rows."""
    seen_in_transaction: list[bool] = []
    real = cleanse.cleanse

    def slow(parsed):
        seen_in_transaction.append(db.in_transaction())
        return real(parsed)

    monkeypatch.setattr(cleanse, "cleanse", slow)
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await session.commit()
        transactions.clear()
        counts = await _store(session, acme, _listing(range(120)))
        await session.commit()
    assert len(seen_in_transaction) == 120 and not any(seen_in_transaction), "no copy is cleaned inside a transaction"
    writers = _write_transactions(transactions)
    assert counts["new"] == 120 and len(writers) >= 120 * 3 // postings.SLICE_ROWS, "each slice writes 40 rows or fewer"
    # The raw rows of a slice are one INSERT (`RawPosting.id` is the insert sentinel), not one `RETURNING seq` each.
    assert max(len(each) for each in writers) <= 9, f"{max(writers, key=len)}"
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 120
        assert await session.scalar(select(func.count()).select_from(RawPosting)) == 120
        assert await session.scalar(select(func.count()).select_from(CleansedPosting)) == 120


async def test_each_slice_is_committed_before_the_next_while_they_are_written(js, monkeypatch):
    monkeypatch.setattr(postings, "SLICE_SEEN", 4)
    committed: list[int] = []
    real = raw_copies.keep_prepared
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await session.commit()

    async def keep(session, *args, **more):
        async with db.read_session_factory()() as other:  # a reader: what is saved as this slice begins to write
            committed.append(await other.scalar(select(func.count()).select_from(JobPosting)))
        return await real(session, *args, **more)

    monkeypatch.setattr(raw_copies, "keep_prepared", keep)
    async with db.session_factory()() as session:
        await _store(session, acme, _listing(range(12)))
        await session.commit()
    assert committed == [0, 4, 8], "every slice but the one being written was committed"


async def test_a_sliced_board_is_saved_as_the_one_transaction_save_does_it(js):
    """The same two reads (new, changed, unchanged, gone, reopened) of the same board under two owners, one saved a
    slice at a time and the other in one transaction: the same postings, raw rows, copies and counts."""
    other = "another"
    first, third = _listing(range(10)), _listing(range(10), version=1)
    second = _listing([0, 1, 2, 3, 4, 5, 6, 7, 20, 21])
    second.postings[4] = _listed(4, version=1)  # one posting changed on the way
    results = {}
    for owner, sliced in ((OWNER, True), (other, False)):
        async with db.session_factory()() as session:
            acme = await employers.find_or_make(session, owner, "Acme")
            acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": "acme"}
            await session.flush()
            counts = []
            for step, listing in enumerate((first, second, third)):
                counts.append(dict(await _store(session, acme, listing, NIGHT + timedelta(days=step), sliced=sliced,
                                                owner=owner)))
                await session.commit()
            rows = (await session.scalars(select(JobPosting).where(JobPosting.owner == owner))).all()
            raw = (await session.scalars(select(RawPosting).where(RawPosting.owner == owner).order_by(
                RawPosting.seq))).all()
            copies = (await session.scalars(select(CleansedPosting).where(CleansedPosting.owner == owner))).all()
            results[owner] = {
                "counts": counts,
                "postings": sorted((p.ext_id, p.title, p.text, p.first_seen, p.last_seen, p.closed_at, p.links)
                                   for p in rows),
                "raw": [(r.ext_id, r.kind, r.body_sha256, r.reader) for r in raw],
                "copies": sorted((c.title, c.text_clean, len(c.from_raw)) for c in copies)}
    assert results[OWNER] == results[other]
    assert [c["new"] for c in results[OWNER]["counts"]] == [10, 2, 0] and results[OWNER]["counts"][2]["reopened"] >= 1


async def test_a_slice_that_fails_is_rolled_back_alone_and_the_slices_before_it_stay_saved(
        js, board, monkeypatch, caplog):
    """The fault unit is the slice: the board is reported failed, the slices saved before it stay, `counts` say what was
    saved, and the other boards go on."""
    monkeypatch.setattr(postings, "SLICE_SEEN", 5)
    calls: list[int] = []
    real = raw_copies.keep_prepared

    async def keep(session, *args, **more):
        calls.append(1)
        saved = await real(session, *args, **more)
        if len(calls) == 2:  # the second slice is written, and then fails
            raise RuntimeError("a database error in the second slice")
        return saved

    monkeypatch.setattr(raw_copies, "keep_prepared", keep)
    board.answers = [_listing(range(12)), Listing([_listed(100)])]
    async with db.session_factory()() as session:
        await _acme(session)
        other = await _acme(session, "Initech")
        other.board = {"slug": "initech"}
        await session.commit()
        result = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        saved = {p.ext_id for p in (await session.scalars(select(JobPosting))).all()}
        raw = await session.scalar(select(func.count()).select_from(RawPosting))
        copies = await session.scalar(select(func.count()).select_from(CleansedPosting))
    assert (result["employers"], result["failed"]) == (2, 1)
    [fault] = result["faults"]
    assert fault["employer"] == "Acme" and fault["kind"] == "failed" and "second slice" in fault["why"]
    assert saved == {"0", "1", "2", "3", "4", "100"}, "the first slice and the other board stay: the second is gone"
    assert (raw, copies) == (6, 6)
    assert (result["seen"], result["new"]) == (6, 6), "what was counted is what was saved"
    assert "saving the board of Acme failed" in caplog.text


async def test_a_database_error_in_a_later_slice_rolls_that_slice_back_alone_and_the_next_board_is_saved(
        js, board, monkeypatch, caplog):
    """A real database error, not a Python one (a raised RuntimeError cannot tell a slice's own savepoint from none):
    while the third slice is written, inside its savepoint, a second row with the key of one of its new postings
    (owner, employer, ext_id) is flushed, so the database refuses it (`IntegrityError`). The slices before it stay
    saved, the board is reported failed, the counts are what was saved, and the next board is saved: with no savepoint
    of the slice's own the failed flush would leave the session waiting for a rollback and the next board would fail
    with it."""
    monkeypatch.setattr(postings, "SLICE_SEEN", 3)
    calls: list[int] = []
    real = raw_copies.keep_prepared
    acme_id: list = []

    async def keep(session, *args, **more):
        calls.append(1)
        if len(calls) == 3:  # the third slice has written its postings; one more with the key of "7" breaks it
            session.add(JobPosting(owner=OWNER, employer_id=acme_id[0], ats=EXAMPLE_SYSTEM, ext_id="7", company="Acme",
                                   title="Programs Role 7", first_seen=NOW_BEFORE, last_seen=NOW_BEFORE, links=[]))
            await session.flush()
        return await real(session, *args, **more)

    monkeypatch.setattr(raw_copies, "keep_prepared", keep)
    board.answers = [_listing(range(9)), Listing([_listed(100)])]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        acme_id.append(acme.id)
        other = await _acme(session, "Initech")
        other.board = {"slug": "initech"}
        await session.commit()
        result = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        saved = {(p.employer_id == acme.id, p.ext_id) for p in (await session.scalars(select(JobPosting))).all()}
        raw = await session.scalar(select(func.count()).select_from(RawPosting))
        copies = await session.scalar(select(func.count()).select_from(CleansedPosting))
    assert (result["employers"], result["failed"]) == (2, 1)
    [fault] = result["faults"]
    assert fault["employer"] == "Acme" and fault["kind"] == "failed" and "IntegrityError" in fault["why"], fault
    assert saved == {(True, str(n)) for n in (0, 1, 2, 3, 4, 5)} | {(False, "100")}, (
        "the first two slices and the other board: the third slice is gone")
    assert (raw, copies) == (7, 7), "a raw row and a copy for each posting this night saved, none for the third slice"
    assert (result["seen"], result["new"]) == (7, 7), "what was counted is what was saved"
    assert "saving the board of Acme failed" in caplog.text


async def test_a_board_that_fails_partway_leaves_what_was_saved_and_its_employer_as_its_last_whole_read_left_it(
        js, board, monkeypatch):
    """What a failed board leaves (docs/PRD.md 28.8): its employer's row says the board was read (`fetched_at`, the
    board's address: they are committed before the postings are written) and keeps `fetch_ok` and `open_roles` as the
    last whole save left them; the postings saved stay, the postings not closed are still open, and the failure is the
    board's fault in the night's answer. The next read closes the rest."""
    monkeypatch.setattr(postings, "CLOSE_SLICE", 2)
    calls: list[int] = []
    real = postings._close_slice

    async def close(session, ids, asked, now):
        calls.append(1)
        if len(calls) == 2:  # the first two postings are closed, then the second slice of the close-out fails
            raise RuntimeError("a database error in the second slice of the close-out")
        return await real(session, ids, asked, now)

    board.answers = [_listing(range(7)), Listing([_listed(100)]), Listing([_listed(100)])]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await session.commit()
        first = await postings.refresh(session, OWNER, _fetch(), now=NOW_BEFORE)
        monkeypatch.setattr(postings, "_close_slice", close)
        second = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        monkeypatch.setattr(postings, "_close_slice", real)
    async with db.session_factory()() as session:
        employer = await session.get(Employer, acme.id)
        assert employer is not None
        after_failure = (employer.fetched_at, employer.fetch_ok, employer.open_roles)
        rows = {p.ext_id: p.closed_at for p in (await session.scalars(select(JobPosting))).all()}
    assert first["failed"] == 0 and second["failed"] == 1 and (second["new"], second["closed"]) == (1, 2)
    [fault] = second["faults"]
    assert fault["employer"] == "Acme" and fault["kind"] == "failed"
    assert after_failure == (NIGHT, True, 7), "read tonight; fetch_ok and open_roles are last night's, not tonight's 1"
    assert "100" in rows and sum(1 for n in range(7) if rows[str(n)] is not None) == 2
    async with db.session_factory()() as session:  # the next night's read closes the rest and brings the row up to date
        acme = await session.get(Employer, acme.id)
        assert acme is not None
        third = await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=1))
        await session.refresh(acme)
        assert third["failed"] == 0 and third["closed"] == 5
        assert (acme.fetch_ok, acme.open_roles) == (True, 1)


async def test_a_posting_that_gained_a_raw_row_since_the_board_was_read_gets_a_copy_that_folds_it(js, monkeypatch):
    """A detail read or a hand-in can keep a row for a posting while the board is being prepared: the copy is made again
    from the rows the posting has when it is written, not from the ones the item was prepared against."""
    real = postings._prepare
    detail = "The whole description a reader of the posting's own page brought back, in its own words."

    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _store(session, acme, _listing([1]))
        await session.commit()

    async def prepare(*args, **more):
        prepared = await real(*args, **more)
        async with db.session_factory()() as other:  # a reader keeps the posting's page, after the item was prepared
            posting = (await other.scalars(select(JobPosting).where(JobPosting.ext_id == "1"))).one()
            await raw_copies._keep_report(other, posting, {"page": 1}, reader="code:text", text=detail,
                                         kind=raw_copies.DETAIL, content_type="text/plain", now=NIGHT)
            await other.commit()
        return prepared

    monkeypatch.setattr(postings, "_prepare", prepare)
    async with db.session_factory()() as session:
        acme = await employers.find(session, OWNER, "Acme")
        await _store(session, acme, _listing([1], version=1), NIGHT + timedelta(days=1))
        await session.commit()
    async with db.session_factory()() as session:
        raw = (await session.scalars(select(RawPosting).order_by(RawPosting.seq))).all()
        assert [r.kind for r in raw] == ["item", "detail", "item"]
        newest = (await session.scalars(select(CleansedPosting).where(CleansedPosting.raw_id == raw[-1].id))).one()
        assert newest.from_raw == [str(raw[1].id), str(raw[2].id)], "the page the reader kept is folded in"
        assert "version 1" in newest.text_clean, "and the newer item's words are the copy's"


async def test_an_item_of_the_same_bytes_kept_since_the_board_was_read_is_not_kept_again(js, monkeypatch):
    real = postings._prepare

    async with db.session_factory()() as session:
        acme = await _acme(session)
        await session.commit()

    async def prepare(*args, **more):
        prepared = await real(*args, **more)
        async with db.session_factory()() as other:  # another read keeps these very bytes first
            posting = (await other.scalars(select(JobPosting).where(JobPosting.ext_id == "1"))).one()
            await raw_copies._keep_report(other, posting, _item(1), reader="browser", text=posting.text,
                                         kind=raw_copies.ITEM, now=NIGHT)
            await other.commit()
        return prepared

    async with db.session_factory()() as session:  # the posting is known, its item is new to the index
        session.add(JobPosting(owner=OWNER, employer_id=acme.id, ats=EXAMPLE_SYSTEM, ext_id="1", company="Acme",
                               title="Programs Role 1", first_seen=NOW_BEFORE, last_seen=NOW_BEFORE, links=[]))
        await session.commit()
    monkeypatch.setattr(postings, "_prepare", prepare)
    async with db.session_factory()() as session:
        await _store(session, acme, _listing([1]))
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(RawPosting)) == 1, "one row, not two"


async def test_the_cleaners_answers_are_held_for_a_slice_at_a_time_and_let_go_once_it_is_written(js, monkeypatch):
    """A first read of a 10,000-posting board held each item's bytes, deflated bytes, parse and cleaned text (about four
    times the listing) until the whole board was written. They are made just before the slice that saves them and let go
    once it is written: what is held is a slice's."""
    monkeypatch.setattr(postings, "SLICE_SEEN", 4)
    items: list = []
    real_listed = postings._listed

    def listed(*args, **more):
        out = real_listed(*args, **more)
        items.extend(out)
        return out

    monkeypatch.setattr(postings, "_listed", listed)
    held: list[int] = []
    real = raw_copies.keep_prepared

    async def keep(session, *args, **more):
        held.append(sum(1 for item in items if item.prepared is not None))
        return await real(session, *args, **more)

    monkeypatch.setattr(raw_copies, "keep_prepared", keep)
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await session.commit()
        counts = await _store(session, acme, _listing(range(12)))
        await session.commit()
    assert counts["new"] == 12 and len(items) == 12
    assert held == [4, 4, 4], f"the answers held when each slice is written: {held}"
    assert not any(item.prepared is not None for item in items), "and none is kept after the board is written"


# --- the close-out --------------------------------------------------------------------------------------------------


async def test_the_postings_a_whole_read_does_not_return_are_closed_in_slices_after_the_rest(js, monkeypatch,
                                                                                               transactions):
    monkeypatch.setattr(postings, "CLOSE_SLICE", 2)
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _store(session, acme, _listing(range(7)))
        await session.commit()
        transactions.clear()
        counts = await _store(session, acme, _listing([0]), NIGHT + timedelta(days=1))
        await session.commit()
        closed = {p.ext_id: p.closed_at for p in (await session.scalars(select(JobPosting))).all()}
    assert counts["closed"] == 6 and closed["0"] is None and all(closed[str(n)] for n in range(1, 7))
    assert len(_write_transactions(transactions)) >= 1 + 3, "the one posting seen, then three slices of the close-out"


async def test_a_read_that_asked_for_only_what_is_new_looks_at_the_postings_it_names_and_closes_nothing(
        js, transactions):
    """A read of only what is new answers a few postings of a board of thousands: it asks for those, never for every
    posting of the board."""
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _store(session, acme, _listing(range(30)))
        await session.commit()
        transactions.clear()
        counts = await _store(session, acme, _listing([3, 4], version=0, asked=Asked(new_only=True)),
                              NIGHT + timedelta(days=1))
        await session.commit()
    assert counts["seen"] == 2 and counts["closed"] == 0
    reads_of_every_posting = [s for s in transactions.timeline if s != Transactions.END and "FROM js_postings" in s
                              and "LIMIT" in s and "ext_id IN" not in s]
    assert not reads_of_every_posting, reads_of_every_posting


# --- who may ask for slices ------------------------------------------------------------------------------------------


def test_the_one_transaction_save_is_the_default_and_the_boards_night_a_site_read_and_boardone_ask_for_slices():
    """A site's read by its skill (`site_reads.py`) saves its board in slices, like the boards' night (`index.py`),
    and so does a followed board's search (its reader is restricted code: `test_board_frozen.py` holds the one call,
    docs/PRD.md path 48). No one else asks."""
    assert inspect.signature(postings.store).parameters["sliced"].default is False
    root = Path(postings.__file__).resolve().parents[2]
    asking = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(k.arg == "sliced" for k in node.keywords):
                asking[path.relative_to(root).as_posix()] = asking.get(path.relative_to(root).as_posix(), 0) + 1
    assert asking == {"jobsearch/postings/index.py": 1, "jobsearch/postings/site_reads.py": 1}, asking


async def test_the_one_transaction_save_ends_no_transaction_of_its_callers(js, transactions):
    async with db.session_factory()() as session:
        acme = await _acme(session)
        transactions.clear()
        counts = await _store(session, acme, _listing(range(200)), sliced=False)
        assert transactions.ended == [], "nothing was saved by the call: the caller's transaction is whole"
        await session.commit()
    assert counts["new"] == 200
