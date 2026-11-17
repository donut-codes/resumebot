"""`postings/hold.py`: a posting's text, its raw copy and the cleansed copy that follows are kept as one act. Each case
asserts what a caller can see afterwards (the posting, its raw row, its copy, its role's to-do); none looks inside.
Everything is made up."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.adapters._common import Detailed
from app.jobsearch.browser import reader as browser_reader
from app.jobsearch.models import CleansedPosting, JobPosting, RawPosting, Todo
from app.jobsearch.postings import dates as posted_dates
from app.jobsearch.postings import hold, raw_copies
from tests.jobsearch.work_queue.support import make_posting, make_role, without_links

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=3)
WORDS = "Lead the programs team and keep the books in order. " * 12
OLD = "A short listing snippet."


async def _a_posting(**fields) -> uuid.UUID:
    role = make_role()
    posting = make_posting(opportunity_id=role.id, ats="employer", ext_id="1", **fields)
    async with db.session_factory()() as session:
        await without_links(session)
        session.add_all([role, posting])
        await session.commit()
    return posting.id


async def _raw(session, posting_id: uuid.UUID) -> list[RawPosting]:
    return list((await session.scalars(select(RawPosting).where(RawPosting.posting_id == posting_id)
                                       .order_by(RawPosting.seq))).all())


def _answer(text: str | None = WORDS, *, raw: bool = True) -> Detailed:
    more = Detailed(text=text)
    if raw:
        more.raw = {"id": 1, "content": text}
    return more


# What each source writes: the reader word, the kind, the content type. The text is put the same way for all five.
SOURCES = {
    "item": (lambda: hold.item({"id": 1, "n": 1}), "browser", "item", "application/json"),
    "page": (lambda: hold.page(b"<p>page</p>", "code:page"), "code:page", "detail", "text/html"),
    "answer": (lambda: hold.answer(_answer()), "adapter:employer", "detail", "application/json"),
    "plain": (lambda: hold.plain("code:text"), "code:text", "detail", "text/plain"),
    "reported": (lambda: hold.reported({"id": 1, "n": 2}, "scout"), "scout", "item", "application/json"),
}


@pytest.mark.parametrize("name", list(SOURCES))
async def test_a_put_with_each_source_leaves_the_text_the_raw_row_and_the_copy_that_hold_it(js, name):
    make, reader, kind, content_type = SOURCES[name]
    posting_id = await _a_posting(text=OLD)
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        assert await hold.hold(session, posting, make(), put=hold.Put(WORDS, EARLIER), now=NOW) == 1
        await session.commit()
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        assert (posting.text, posting.detail_at) == (WORDS, EARLIER)
        [row] = await _raw(session, posting_id)
        assert (row.reader, row.kind, row.content_type) == (reader, kind, content_type)
        assert row.parsed["text"] == WORDS
        copy = await session.get(CleansedPosting, posting.current_cleansed_id)
        assert copy is not None and copy.raw_id == row.id
        topics = {t for t, in (await session.execute(select(Todo.topic))).all()}
        assert "role.settle" in topics, "the posting's role is told that its text has a copy"


async def test_a_hold_with_no_put_makes_the_copy_and_leaves_the_posting_as_it_is(js):
    posting_id = await _a_posting(text=WORDS, detail_at=EARLIER)
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        assert await hold.hold(session, posting, hold.item({"id": 1}), now=NOW) == 1
        assert (posting.text, posting.detail_at) == (WORDS, EARLIER)
        [row] = await _raw(session, posting_id)
        assert row.parsed["text"] == WORDS, "the copy holds the text the posting has"
        await session.refresh(posting)
        assert posting.current_cleansed_id is not None


async def test_a_put_that_keeps_the_date_leaves_the_date_and_changes_the_text(js):
    posting_id = await _a_posting(text=None, detail_at=EARLIER)
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        await hold.hold(session, posting, hold.item({"id": 1}), put=hold.Put(OLD, hold.KEEP), now=NOW)
        assert (posting.text, posting.detail_at) == (OLD, EARLIER)
        await hold.hold(session, posting, hold.item({"id": 2}), put=hold.Put(WORDS, None), now=NOW)
        assert (posting.text, posting.detail_at) == (WORDS, None), "None clears the date"


async def test_a_put_with_no_text_is_refused_and_writes_nothing(js):
    posting_id = await _a_posting(text=OLD)
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        with pytest.raises(ValueError):
            await hold.hold(session, posting, hold.item({"id": 1}), put=hold.Put("", NOW), now=NOW)
        assert posting.text == OLD and await _raw(session, posting_id) == []


async def test_a_plain_source_with_no_text_is_refused(js):
    posting_id = await _a_posting(text=None)
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        with pytest.raises(ValueError):
            await hold.hold(session, posting, hold.plain("code:text"), now=NOW)


async def test_the_same_payload_again_writes_nothing(js):
    posting_id = await _a_posting(text=None)
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        assert await hold.hold(session, posting, hold.item({"id": 1}), put=hold.Put(WORDS, NOW), now=NOW) == 1
        copy = posting.current_cleansed_id
        assert await hold.hold(session, posting, hold.item({"id": 1}), put=hold.Put(WORDS, NOW), now=NOW) == 0
        assert len(await _raw(session, posting_id)) == 1 and posting.current_cleansed_id == copy


async def test_a_copy_text_given_on_purpose_is_what_the_copy_holds_while_the_posting_keeps_its_own(js):
    posting_id = await _a_posting(text=OLD)
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        await hold.hold(session, posting, hold.page(b"<p>page</p>", "code:page"), copy_text=WORDS, now=NOW)
        assert posting.text == OLD
        [row] = await _raw(session, posting_id)
        assert row.parsed["text"] == WORDS


async def test_the_postings_update_is_visible_to_the_copy_built_in_the_same_call(js):
    """A posting whose way of working was just set is parsed with it: the copy is built after the posting's flush."""
    posting_id = await _a_posting(text=OLD)
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        posting.work_mode = "remote"
        await hold.hold(session, posting, hold.item({"id": 1}), put=hold.Put(WORDS, NOW), now=NOW)
        [row] = await _raw(session, posting_id)
        assert row.parsed["work_mode"] == "remote" and row.parsed["text"] == WORDS


async def test_an_answer_with_no_reply_to_keep_writes_nothing(js):
    posting_id = await _a_posting(text=OLD)
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        more = _answer(raw=False)
        assert await hold.hold(session, posting, hold.answer(more), put=hold.Put(WORDS, hold.KEEP),
                               copy_text=more.get("text"), now=NOW) == 0
        assert posting.text == WORDS, "the put is the caller's, whatever the answer kept"
        assert await _raw(session, posting_id) == []




def test_a_sighting_dates_the_posting_from_the_text_it_gives_it():
    """`_fill` dates a posting from the text it hands back as a `Put`, before `hold` puts it."""
    posting = SimpleNamespace(
        posted=None, text=None, detail_at=None, links=[], closed_at=None, not_checked_at=None, location=None,
        work_mode=None, comp=None, apply_url=None, first_seen=NOW, last_seen=NOW)
    description = "Posted 3 days ago. " + WORDS
    sighting = {"location": None, "work_mode": None, "comp": None, "apply_url": None, "posted_text": "",
                "description": description, "snippet": None, "url": "https://jobs.example.test/1"}
    put = browser_reader._fill(posting, sighting, NOW, reopen=False)
    assert put is not None and put.text == description and put.detail_at == NOW
    assert posting.text is None, "the text is put by `hold`, with its copy"
    assert posting.posted is not None and posting.posted == posted_dates.day_of(description, NOW, NOW.date())


def test_the_raw_copy_functions_a_caller_could_pair_with_a_text_are_private():
    assert not hasattr(raw_copies, "keep_report") and not hasattr(raw_copies, "keep_detail")
