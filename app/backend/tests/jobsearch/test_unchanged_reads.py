"""A list read that finds a posting again keeps a saved copy of it only when the posting's content changed (the owner,
2026-11-06; docs/PRD.md 28.9): the new item is not kept when the cleaner would be given exactly what it was given for
the newest item, and the posting already holds a copy made at this cleaner version. Everything that is not the cleaner's
input (an applicant count, a link, the order of keys) is no change. The index row is written either way, and the roles
are woken only by a copy. Real index code over synthetic boards; no network."""

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from app import db
from app.jobsearch.adapters import Listing, _posting
from app.jobsearch.adapters._common import Detailed
from app.jobsearch.employers import core as employers
from app.jobsearch.models import CleansedPosting, Employer, JobPosting, RawPosting, Todo
from app.jobsearch.postings import cleanse, hold, raw_copies
from app.jobsearch.postings import index as postings
from tests.jobsearch.work_queue.support import make_role
from tests.support import EXAMPLE_SYSTEM, OWNER

NIGHT = datetime(2026, 11, 5, 5, 0, tzinfo=UTC)
LATER = NIGHT + timedelta(days=1)
TEXT = "Lead the programs team, run the weekly review and report to the board.\nOwn the budget for the programs."
FIELDS = {"title": "Programs Lead", "location": "Remote", "work_mode": "remote", "comp": "$85,000 - $105,000",
          "comp_min": 85000, "comp_max": 105000, "posted": "2026-11-01", "text": TEXT,
          "url": "https://boards.example.test/acme/jobs/1"}
PATHS = pytest.mark.parametrize("sliced", [True, False], ids=["sliced", "whole"])


def _listed(n: int = 1, applicants: int = 0, **fields):
    """A posting as a reader hands it on, with the item it was read from: the item says the fields and an applicant
    count, so a read that changes only the count is other bytes and the same posting."""
    fields = {**FIELDS, **fields}
    return _posting(raw={"id": n, "applicants": applicants, **fields}, ext_id=str(n), **fields)


def _listing(*items) -> Listing:
    return Listing(list(items))


async def _acme(session, owner: str = OWNER) -> Employer:
    acme = await employers.find_or_make(session, owner, "Acme")
    acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": "acme"}
    await session.flush()
    return acme


async def _store(session, acme: Employer, listing, now=NIGHT, *, sliced=True, company: str | None = None,
                 owner: str = OWNER) -> dict:
    counts = dict.fromkeys(("seen", "new", "closed", "reopened"), 0)
    await postings.store(session, owner, listing, source_id=acme.id, ats_name=EXAMPLE_SYSTEM,
                         company=company or acme.name, now=now, counts=counts, sliced=sliced)
    return counts


async def _first_read(sliced: bool, *items) -> tuple[uuid.UUID, uuid.UUID]:
    """Read a board once (the first night), give its posting a role, and clear what that posted: the ids of the
    employer and of the role."""
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _store(session, acme, _listing(*(items or (_listed(),))), sliced=sliced)
        role = make_role()
        session.add(role)
        await session.flush()
        for posting in (await session.scalars(select(JobPosting))).all():
            posting.opportunity_id = role.id
        await session.execute(delete(Todo))
        await session.commit()
        return acme.id, role.id


async def _read_again(acme_id: uuid.UUID, sliced: bool, *items, now=LATER, company: str | None = None) -> dict:
    async with db.session_factory()() as session:
        acme = await session.get(Employer, acme_id)
        assert acme is not None
        counts = await _store(session, acme, _listing(*items), now, sliced=sliced, company=company)
        await session.commit()
        return counts


async def _count(model) -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def _raw() -> list[RawPosting]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(RawPosting).order_by(RawPosting.seq))).all())


async def _copies() -> list[CleansedPosting]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(CleansedPosting).outerjoin(
            RawPosting, RawPosting.id == CleansedPosting.raw_id).order_by(RawPosting.seq))).all())


async def _woken_by_a_copy() -> list[tuple[str, str]]:
    """The to-dos a copy made (`raw_copies._copy_made`) posted: the roles to settle and the pull to rank."""
    async with db.session_factory()() as session:
        return list((await session.execute(select(Todo.topic, Todo.key).where(Todo.cause == "copies.made")
                                           .order_by(Todo.id))).all())


# --- a read that changes nothing the cleaner reads -------------------------------------------------------------------


@PATHS
async def test_a_read_that_changes_nothing_the_cleaner_reads_keeps_no_copy_wakes_no_role_and_still_moves_the_index_row(
        js, sliced):
    """Thousands of raw rows a night were written for postings whose only change
    was a count the cleaner never reads, and each woke the role's settle and its re-rank. The index row still takes
    the read: last seen moves, a posting that was Not checked or closed is open again."""
    acme_id, _role = await _first_read(sliced)
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting))).one()
        posting.closed_at = posting.not_checked_at = NIGHT
        await session.commit()
    counts = await _read_again(acme_id, sliced, _listed(applicants=7))
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting))).one()
        assert (posting.last_seen, posting.closed_at, posting.not_checked_at) == (LATER, None, None)
    assert counts["reopened"] == 1
    assert (await _count(RawPosting), await _count(CleansedPosting)) == (1, 1), "no raw row and no copy"
    assert await _woken_by_a_copy() == [], "no role is woken for a copy that was not made"


@PATHS
async def test_a_volatile_count_that_moves_every_night_adds_no_row_after_the_first(js, sliced):
    acme_id, _role = await _first_read(sliced)
    for night, applicants in enumerate((3, 9, 4, 4), start=1):
        await _read_again(acme_id, sliced, _listed(applicants=applicants), now=NIGHT + timedelta(days=night))
    assert (await _count(RawPosting), await _count(CleansedPosting)) == (1, 1)


# Each input the cleaner reads, changed (a value that is another, one that is gone): a copy is kept and the role woken.
CHANGED = {
    "title": {"title": "Head of Programs"},
    "company": {},  # given by the store's `company`, below
    "location": {"location": "Paris"},
    "work mode": {"work_mode": "hybrid"},
    "comp": {"comp": "$95,000 - $115,000"},
    "comp min": {"comp_min": 95000},
    "comp max": {"comp_max": 115000},
    "posted": {"posted": "2026-10-30"},  # an earlier day: a later one is no change (below)
    "text": {"text": TEXT + "\nTravel is rare."},
    "location gone": {"location": None},
    "work mode gone": {"work_mode": None},
    "comp gone": {"comp": None},
    "comp min gone": {"comp_min": None},
    "comp max gone": {"comp_max": None},
    "posted gone": {"posted": None},
    "text gone": {"text": None},
}


@PATHS
@pytest.mark.parametrize("change", list(CHANGED))
async def test_a_read_that_changes_an_input_of_the_cleaner_keeps_a_copy_and_wakes_the_role(js, sliced, change):
    acme_id, role_id = await _first_read(sliced)
    company = "Acme Corporation" if change == "company" else None
    await _read_again(acme_id, sliced, _listed(applicants=7, **CHANGED[change]), company=company)
    assert (await _count(RawPosting), await _count(CleansedPosting)) == (2, 2), f"{change} is a change"
    first, second = await _copies()
    kept = ("title", "employer", "locations", "work_mode", "pay_min", "pay_max", "posted_date", "text_clean", "marks")
    assert [getattr(first, k) for k in kept] != [getattr(second, k) for k in kept], "the copy says the change"
    assert await _woken_by_a_copy() == [("role.settle", str(role_id)), ("pool.owed", "")]


# --- a later posted day is never a change --------------------------------------------------------------------------


@PATHS
async def test_a_read_that_gives_a_later_posted_day_keeps_no_copy_and_leaves_the_posted_day_where_it_was(js, sliced):
    """A site that stamps the moment of the read as the posted day (or counts
    "Posted Today" from the day of the read) gave a raw row a posting a day, each a day later than the one before.
    A day a posting is known to be posted is never moved later by a later read (docs/PRD.md 28.9, 40.1)."""
    acme_id, _role = await _first_read(sliced)
    for night, day in enumerate(("2026-11-02", "2026-11-03", "2026-11-04"), start=1):
        await _read_again(acme_id, sliced, _listed(posted=day), now=NIGHT + timedelta(days=night))
    assert (await _count(RawPosting), await _count(CleansedPosting)) == (1, 1)
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting))).one()
        assert posting.posted is not None and posting.posted.isoformat() == "2026-11-01"
    assert await _woken_by_a_copy() == []


@PATHS
async def test_a_read_that_gives_an_earlier_posted_day_moves_it_and_keeps_a_copy(js, sliced):
    acme_id, _role = await _first_read(sliced)
    await _read_again(acme_id, sliced, _listed(posted="2026-10-30"))
    assert (await _count(RawPosting), await _count(CleansedPosting)) == (2, 2)
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting))).one()
        assert posting.posted is not None and posting.posted.isoformat() == "2026-10-30"


def test_a_later_posted_day_is_held_at_the_known_one_and_nothing_else_is_touched():
    rows = [_held("item", posted="2026-11-01", text="Lead.")]
    new = {"posted": "2026-11-05", "text": "Lead."}
    assert raw_copies.known_day(rows, new) == {"posted": "2026-11-01", "text": "Lead."}
    assert raw_copies.known_day(rows, {"posted": "2026-10-20"}) == {"posted": "2026-10-20"}
    assert raw_copies.known_day(rows, {"posted": None}) == {"posted": None}
    assert raw_copies.known_day([], new) is new, "nothing known yet"
    assert raw_copies.known_day(rows, {"posted": "Posted 3 Days Ago"}) == {"posted": "Posted 3 Days Ago"}
    assert raw_copies.known_day([_held("detail", posted="2026-10-01")], new)["posted"] == "2026-10-01", "the true day"
    assert _unchanged(rows, raw_copies.known_day(rows, {"posted": "2026-11-05", "text": "Lead."}))


# --- the fold, not the item against the item ------------------------------------------------------------------------


@PATHS
async def test_an_item_equal_to_the_last_item_but_not_to_what_the_copy_was_folded_from_is_kept_once_then_not(
        js, sliced):
    """A detail read lies between the two item reads and its place says other than the item's: the copy takes the
    newest row's place (`_use`: the new item moves after the detail and overrides it), so a read of an item equal to the
    last item still changes the copy once, and the read after it changes nothing."""
    acme_id, _role = await _first_read(sliced, _listed(location="Remote"))
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting))).one()
        posting.location = "Paris"  # what the posting's own page said (`keep_report` keeps the index row's fields)
        await raw_copies._keep_report(session, posting, {"page": 1}, reader="code:text", text=TEXT,
                                     kind=raw_copies.DETAIL, content_type="text/plain", now=NIGHT)
        await session.commit()
    assert (await _copies())[-1].locations == ["Paris"]
    await _read_again(acme_id, sliced, _listed(applicants=1, location="Remote"))
    assert (await _count(RawPosting), await _count(CleansedPosting)) == (3, 3), "the item's place wins again: a copy"
    assert (await _copies())[-1].locations == ["Remote"]
    await _read_again(acme_id, sliced, _listed(applicants=2, location="Remote"), now=LATER + timedelta(days=1))
    assert (await _count(RawPosting), await _count(CleansedPosting)) == (3, 3), "and then nothing"


# --- the first item row, and a copy of this cleaner version ---------------------------------------------------------


async def _a_posting_that_holds(kind: str) -> uuid.UUID:
    """A posting the index knows whose only raw row is a detail answer, or the text it held before copies were kept."""
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _store(session, acme, _listing(_posting(ext_id="1", title="Programs Lead", location="Remote",
                                                      text=TEXT if kind == "legacy" else None)))
        posting = (await session.scalars(select(JobPosting))).one()
        if kind == "legacy":
            assert await raw_copies.keep_legacy(session, OWNER, NIGHT) == 1
        else:
            await raw_copies._keep_report(session, posting, {"page": 1}, reader="code:text", text=TEXT,
                                         kind=raw_copies.DETAIL, content_type="text/plain", now=NIGHT)
        await session.commit()
        return acme.id


@PATHS
@pytest.mark.parametrize("kind", ["detail", "legacy"])
async def test_the_first_item_of_a_posting_is_kept_once_whatever_it_holds_and_a_volatile_change_after_it_is_not(
        js, sliced, kind):
    acme_id = await _a_posting_that_holds(kind)
    assert [r.kind for r in await _raw()] == [kind]
    await _read_again(acme_id, sliced, _listed())
    assert [r.kind for r in await _raw()] == [kind, "item"], "the first item row is kept: the cleaner may need it"
    await _read_again(acme_id, sliced, _listed(), now=LATER + timedelta(days=1))
    await _read_again(acme_id, sliced, _listed(applicants=5), now=LATER + timedelta(days=2))
    assert [r.kind for r in await _raw()] == [kind, "item"], "the same bytes and then a count: nothing"


@PATHS
async def test_a_posting_with_no_copy_of_this_cleaner_version_is_written_as_before(js, sliced, monkeypatch):
    """The rule needs the copy the newest row has: with a new cleaner version none is made, and the read writes."""
    acme_id, _role = await _first_read(sliced)
    monkeypatch.setattr(cleanse, "CLEANER_VERSION", cleanse.CLEANER_VERSION + 1)
    await _read_again(acme_id, sliced, _listed(applicants=7))
    assert (await _count(RawPosting), await _count(CleansedPosting)) == (2, 2)
    assert (await _copies())[-1].cleaner_version == cleanse.CLEANER_VERSION
    await _read_again(acme_id, sliced, _listed(applicants=8), now=LATER + timedelta(days=1))
    assert (await _count(RawPosting), await _count(CleansedPosting)) == (2, 2), "now it has one: nothing"


# --- the readers the rule never touches -----------------------------------------------------------------------------


async def test_a_report_a_detail_answer_and_a_reported_item_are_kept_whatever_they_change(js):
    """Only a list read (`keep_items`, `keep_prepared`) decides by the cleaner's input: the owner's browser, the
    scout, a page capture and a posting's own page are each a read of their own and keep every new answer."""
    await _first_read(True)
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting))).one()
        for n in (1, 2):  # a reported item, a reported page: other bytes, the same fields
            await raw_copies._keep_report(session, posting, {"card": n}, reader="browser", text=TEXT, now=LATER)
            await raw_copies._keep_report(session, posting, {"page": n}, reader="code:page", text=TEXT,
                                         kind=raw_copies.DETAIL, now=LATER)
        for n in (1, 2):
            more = Detailed(text=TEXT)
            more.raw = {"id": 1, "content": TEXT, "views": n}
            assert await hold.hold(session, posting, hold.answer(more), copy_text=TEXT, now=LATER) == 1
        await session.commit()
    assert [r.kind for r in await _raw()] == ["item", "item", "detail", "item", "detail", "detail", "detail"]


# --- a posting that gained a row since the board was read -----------------------------------------------------------


async def test_a_posting_decided_unchanged_that_gained_a_raw_row_since_is_written_with_a_copy_that_folds_it(
        js, monkeypatch):
    """The decision was made on the rows the posting held when the board was prepared: a detail read or a hand-in that
    kept a row before the slice is written means the decision no longer holds, and the posting is written as one whose
    copy must fold the row it has now (`moved`), the sibling of the same case for a changed item."""
    acme_id, _role = await _first_read(True)
    real_keep, real_write = raw_copies.keep_prepared, postings._write_slice
    seen: list[bool] = []
    detail = "The whole description a reader of the posting's own page brought back, in its own words."

    async def keep(session, owner, source_id, pairs, **more):
        seen.extend(bool(getattr(p, "unchanged", False)) for _posting_row, p in pairs)
        return await real_keep(session, owner, source_id, pairs, **more)

    async def write_slice(session, *args, **kwargs):
        await db.release_if_free(session)  # the slice was prepared; the one writer turn is free for the other session
        async with db.session_factory()() as other:  # a reader keeps the posting's page after the item was prepared
            posting = (await other.scalars(select(JobPosting))).one()
            await raw_copies._keep_report(other, posting, {"page": 1}, reader="code:text", text=detail,
                                         kind=raw_copies.DETAIL, content_type="text/plain", now=LATER)
            await other.commit()
        return await real_write(session, *args, **kwargs)

    monkeypatch.setattr(postings, "_write_slice", write_slice)
    monkeypatch.setattr(raw_copies, "keep_prepared", keep)
    await _read_again(acme_id, True, _listed(applicants=7))
    assert seen == [True], "it was decided unchanged against the one row it held"
    raw = await _raw()
    assert [r.kind for r in raw] == ["item", "detail", "item"]
    newest = (await _copies())[-1]
    assert newest.raw_id == raw[-1].id and newest.from_raw == [str(raw[1].id), str(raw[2].id)]


async def test_a_posting_decided_unchanged_and_unmoved_is_not_written_by_the_slice(js, monkeypatch):
    acme_id, _role = await _first_read(True)
    real = raw_copies.keep_prepared
    seen: list[bool] = []

    async def keep(session, owner, source_id, pairs, **more):
        seen.extend(bool(getattr(p, "unchanged", False)) for _posting_row, p in pairs)
        return await real(session, owner, source_id, pairs, **more)

    monkeypatch.setattr(raw_copies, "keep_prepared", keep)
    await _read_again(acme_id, True, _listed(applicants=7))
    assert seen == [True] and await _count(RawPosting) == 1


# --- both saves do the same -----------------------------------------------------------------------------------------


async def test_the_sliced_save_and_the_one_transaction_save_keep_the_same_rows_for_the_same_reads(js):
    """Three nights of one board under two owners, one saved a slice at a time and one in a transaction: a new posting,
    a count moving, a change of place, a count moving again, and a posting that is gone."""
    reads = [
        [_listed(1), _listed(2)],
        [_listed(1, applicants=4), _listed(2, applicants=4)],
        [_listed(1, applicants=5, location="Paris"), _listed(2, applicants=5)],
        [_listed(1, applicants=6, location="Paris")],
    ]
    results = {}
    for owner, sliced in ((OWNER, True), ("another", False)):
        async with db.session_factory()() as session:
            acme = await _acme(session, owner)
            counts = []
            for step, items in enumerate(reads):
                counts.append(dict(await _store(session, acme, _listing(*items), NIGHT + timedelta(days=step),
                                                sliced=sliced, owner=owner)))
                await session.commit()
            raw = (await session.scalars(select(RawPosting).where(RawPosting.owner == owner)
                                         .order_by(RawPosting.seq))).all()
            copies = (await session.scalars(select(CleansedPosting).where(CleansedPosting.owner == owner))).all()
            rows = (await session.scalars(select(JobPosting).where(JobPosting.owner == owner))).all()
            results[owner] = {
                "counts": counts, "raw": [(r.ext_id, r.kind, r.body_sha256) for r in raw],
                "copies": sorted((c.title, c.locations, len(c.from_raw)) for c in copies),
                "postings": sorted((p.ext_id, p.location, p.last_seen, p.closed_at) for p in rows)}
    assert results[OWNER] == results["another"]
    assert [ext for ext, _kind, _sha in results[OWNER]["raw"]] == ["1", "2", "1"], "the first two, the move of place"


# --- the rule itself: what is compared ------------------------------------------------------------------------------


def _held(kind: str, **parsed):
    return raw_copies.Held(uuid.uuid4(), kind, uuid.uuid4().hex, {"title": "Lead", "company": "Acme", **parsed})


def _unchanged(rows, parsed, *, current=True) -> bool:
    return raw_copies.unchanged(rows, {"title": "Lead", "company": "Acme", **parsed},
                                {rows[-1].id} if current and rows else set())


def test_an_item_with_the_same_nine_inputs_is_unchanged_whatever_else_it_says():
    item = _held("item", location="Remote", text="Lead.", comp_min=1, url="https://a.example.test/1", department="X")
    assert _unchanged([item], {"location": "Remote", "text": "Lead.", "comp_min": 1, "url": "https://b.example.test/2",
                               "apply_url": "https://c.example.test", "department": "Y", "extra": 1})


def test_a_pay_that_is_a_float_where_it_was_an_int_is_not_the_same_input():
    """`100000 == 100000.0` in Python, but the cleaner keeps the int and drops the float: pay and no pay."""
    item = _held("item", comp_min=100000)
    assert not _unchanged([item], {"comp_min": 100000.0})
    assert not _unchanged([_held("item", comp_min=1)], {"comp_min": True})
    assert not _unchanged([_held("item", comp_min=100000)], {"comp_min": "100000"})
    assert _unchanged([item], {"comp_min": 100000})


def test_a_value_that_holds_a_float_where_it_held_an_int_is_not_the_same_input_either():
    assert not _unchanged([_held("item", location=[1, 2])], {"location": [1, 2.0]})
    assert not _unchanged([_held("item", location={"a": 1})], {"location": {"a": 1.0}})
    assert _unchanged([_held("item", location=[1, {"a": "b"}])], {"location": [1, {"a": "b"}]})


def test_a_key_that_was_set_and_is_none_now_is_a_change_and_one_never_set_reads_as_none():
    assert not _unchanged([_held("item", location="Remote")], {"location": None})
    assert _unchanged([_held("item")], {"location": None, "work_mode": None})
    assert not _unchanged([_held("item")], {"location": "Remote"})


def test_an_empty_text_is_no_text_as_the_fold_reads_it():
    assert _unchanged([_held("detail", text="Whole."), _held("item", text="")], {"text": None})
    assert not _unchanged([_held("item", text="Whole.")], {"text": ""})


def test_a_posting_with_no_item_row_has_no_first_item_to_compare_with():
    assert not _unchanged([], {})
    assert not _unchanged([_held("detail", text="Whole.")], {"text": "Whole."})
    assert not _unchanged([_held("legacy", text="Whole.")], {"text": "Whole."})


def test_a_newest_row_with_no_copy_of_this_cleaner_version_is_never_unchanged():
    item = _held("item", text="Lead.")
    assert _unchanged([item], {"text": "Lead."}) and not _unchanged([item], {"text": "Lead."}, current=False)
    older = _held("item", text="Lead.")
    assert not raw_copies.unchanged([older, item], {"title": "Lead", "company": "Acme", "text": "Lead."}, {older.id})


def test_the_fold_decides_not_the_last_item_against_the_new_one():
    """The new item moves after the detail row and overrides what it says: equal to the last item is not enough."""
    item, detail = _held("item", location="Remote"), _held("detail", location="Paris")
    assert not _unchanged([item, detail], {"location": "Remote"}), "the copy says Paris, the item says Remote"
    assert _unchanged([item, detail], {"location": "Paris"}), "the same fold again"
    assert _unchanged([detail, _held("item", location="Remote")], {"location": "Remote"}), "item after detail"


def test_a_row_whose_parse_was_not_read_is_never_called_unchanged():
    item = raw_copies.Held(uuid.uuid4(), "item", "x", {}, loaded=False)
    assert not _unchanged([item], {})


# --- why nine, and that the rule is made of the fold ----------------------------------------------------------------


CLEANSE = Path(cleanse.__file__)
RAW_COPIES = Path(raw_copies.__file__)


def test_the_cleaner_reads_exactly_the_keys_the_rule_compares():
    """`cleanse()` is a pure function of the keys of `raw_copies.CLEANER_KEYS`: it reads `parsed` only as
    `parsed.get("<key>")`, and no other key. A new key the cleaner reads must be added to that tuple, or a read that
    changes only it would be called unchanged."""
    tree = ast.parse(CLEANSE.read_text(encoding="utf-8"))
    (function,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "cleanse"]
    read: set[str] = set()
    getters = 0
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Name) and node.func.value.id == "parsed":
            assert node.func.attr == "get" and isinstance(node.args[0], ast.Constant), ast.dump(node)
            read.add(node.args[0].value)
            getters += 1
    names = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == "parsed"]
    assert len(names) == getters, "`parsed` is passed on or read another way: the rule would not see what is read there"
    assert read == set(raw_copies.CLEANER_KEYS), f"the cleaner reads {sorted(read)}"
    assert len(raw_copies.CLEANER_KEYS) == 9


FIXTURES = [
    {"title": "Programs Lead", "company": "Acme", "location": "Remote; Paris; Remote", "work_mode": "remote",
     "comp": "$85,000 - $105,000", "comp_min": 85000, "comp_max": 105000, "posted": "2026-11-01T00:00:00Z",
     "text": TEXT},
    {"title": "  Director \t of Programs ", "company": "Acme", "location": "26 Locations",
     "posted": "Posted 3 Days Ago",
     "text": "Equal opportunity employer, and we do not discriminate against anyone, ever.\nLead programs.\n\n\n"
             "Pay range: $120,000 - $150,000 per year\nTo apply, send your resume."},
    {"title": "Lead", "company": None, "text": ""},
    {"title": "Lead", "company": "Acme", "comp_min": 100000.0, "comp_max": True, "work_mode": "Hybrid"},
]


@pytest.mark.parametrize("fixture", range(len(FIXTURES)))
def test_the_same_inputs_give_the_same_cleansed_answer_whatever_else_the_parse_holds(fixture):
    base = FIXTURES[fixture]
    other = {**base, "url": "https://x.example.test/1", "apply_url": "https://x.example.test/2", "department": "Ops",
             "ext_id": "9", "applicants": 12, "raw": {"a": 1}}
    mine = raw_copies.cleaner_inputs([raw_copies.Held(uuid.uuid4(), "item", "a", base)])
    theirs = raw_copies.cleaner_inputs([raw_copies.Held(uuid.uuid4(), "item", "b", other)])
    assert raw_copies.same_inputs(mine, theirs)
    assert cleanse.cleanse(base) == cleanse.cleanse(other)


def test_the_rule_is_made_of_the_fold_and_the_use_the_copy_is_made_of_and_saves_nothing():
    """It calls `_use` and `_fold` (never a second copy of them), and none of what writes a copy or wakes a role."""
    tree = ast.parse(RAW_COPIES.read_text(encoding="utf-8"))
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}

    def calls(name: str) -> set[str]:
        return {c.func.id if isinstance(c.func, ast.Name) else c.func.attr if isinstance(c.func, ast.Attribute)
                else "" for c in ast.walk(functions[name]) if isinstance(c, ast.Call)}

    assert {"_use"} <= calls("unchanged") and "_fold" in calls("cleaner_inputs")
    for name in ("unchanged", "cleaner_inputs", "same_inputs"):
        assert not calls(name) & {"_row", "build", "add", "_copy_made", "post", "_copy", "_cleaned"}, name
