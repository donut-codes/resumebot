"""The posting index (docs/PRD.md 28.7 to 28.9): each read brings it up to date, with first and last seen, closings
and reopenings, links kept; a read cut short can't close anything; a refusal, a changed shape, or a board that had
roles and suddenly answers none is a fault on the employer. Adapters are stand-ins; no network. Synthetic data only."""


import asyncio
import gc
import time
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text

from app import db
from app.jobsearch import adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch.polite import Fetcher, Refused
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting
from app.jobsearch.postings import index as postings
from tests.support import EXAMPLE_SYSTEM, OWNER

NIGHT = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)


@dataclass
class FakeListing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class FakeAdapter:
    """Answers what it is told, one read at a time; an exception in the list is raised instead."""
    answers: list = field(default_factory=list)
    details: dict = field(default_factory=dict)

    async def read(self, fetch, board):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def detail(self, fetch, board, posting):
        return self.details.get(posting["ext_id"], {})


def _posting(ext_id, title, **more):
    return {"ext_id": ext_id, "title": title, "location": "Remote", "work_mode": "remote", "comp": None,
            "comp_min": None, "comp_max": None, "posted": "2026-10-01", "url": f"https://jobs.example.test/{ext_id}",
            "apply_url": None, "text": f"About {title}.", "department": None, **more}


@pytest.fixture
def fake(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: adapter})
    return adapter


async def _acme(session, **more):
    acme = await employers.find_or_make(session, OWNER, "Acme")
    acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": "acme"}
    for k, v in more.items():
        setattr(acme, k, v)
    await session.flush()
    return acme


def _fetch():
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


async def _rows(session):
    return {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}


async def test_reads_keep_first_seen_mark_last_seen_close_and_reopen(js, fake):
    fake.answers = [
        FakeListing([_posting("1", "Director of Programs"), _posting("2", "Head of Delivery")]),
        FakeListing([_posting("1", "Director of Programs", url="https://jobs.example.test/1?src=board")]),
        FakeListing([_posting("1", "Director of Programs"), _posting("2", "Head of Delivery")]),
    ]
    async with db.session_factory()() as session:
        await _acme(session)
        first = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        second = await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=1))
        rows = await _rows(session)
        assert (first["new"], second["new"], second["closed"]) == (2, 0, 1)
        assert rows["1"].first_seen == NIGHT and rows["1"].last_seen == NIGHT + timedelta(days=1)
        assert rows["1"].links == ["https://jobs.example.test/1", "https://jobs.example.test/1?src=board"]
        assert rows["2"].closed_at == NIGHT + timedelta(days=1)
        third = await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=2))
        assert third["reopened"] == 1 and (await _rows(session))["2"].closed_at is None
        assert rows["1"].match_key and rows["1"].company == "Acme" and str(rows["1"].posted) == "2026-10-01"
        acme = await employers.find(session, OWNER, "Acme")
        assert (acme.fetch_ok, acme.open_roles) == (True, 2)


async def test_a_read_cut_short_closes_nothing(js, fake):
    fake.answers = [FakeListing([_posting("1", "A"), _posting("2", "B")]),
                    FakeListing([_posting("1", "A")], truncated=True)]
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        result = await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=1))
        assert result["closed"] == 0 and (await _rows(session))["2"].closed_at is None
        assert (await employers.find(session, OWNER, "Acme")).fetch_note == "stopped before the last page"
        assert result["cut"] == 0, "cut short for another reason than the night's time"


async def test_a_board_not_begun_when_the_nights_time_runs_out_is_left_as_it_was_and_counted(js, fake):
    fake.answers = [FakeListing([_posting("1", "A"), _posting("2", "B")])]
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        late = _fetch()
        late.deadline = time.monotonic() - 1
        result = await postings.refresh(session, OWNER, late, now=NIGHT + timedelta(days=1))
        assert (result["employers"], result["unread"], result["failed"], result["seen"]) == (1, 1, 0, 0)
        assert fake.answers == [], "the board was not asked"
        acme = await employers.find(session, OWNER, "Acme")
        assert acme.fetched_at == NIGHT and acme.fetch_ok is True and acme.open_roles == 2
        assert all(p.closed_at is None for p in (await _rows(session)).values()), "an unread board closes nothing"


async def test_a_board_the_nights_time_ran_out_in_keeps_its_pages_closes_nothing_and_is_counted(js, fake):
    class Late(FakeAdapter):
        async def read(self, fetch, board):
            answer = await super().read(fetch, board)
            fetch.deadline = time.monotonic() - 1  # the time ran out while this board was being read
            return answer

    late_adapter = Late(answers=[FakeListing([_posting("1", "A"), _posting("2", "B")]),
                                 FakeListing([_posting("1", "A")], True, "stopped at 1 of 2 postings (time)")])
    adapters.ADAPTERS[EXAMPLE_SYSTEM] = late_adapter
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        result = await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=1))
        assert (result["cut"], result["unread"], result["closed"]) == (1, 0, 0)
        assert (await _rows(session))["2"].closed_at is None
        assert (await employers.find(session, OWNER, "Acme")).fetch_note == "stopped at 1 of 2 postings (time)"


async def test_every_failure_is_a_fault_on_the_employer(js, fake):
    fake.answers = [Refused("acme.example refused the request (403)"),
                    adapters.AdapterError("feedone changed shape"),
                    FakeListing([_posting("1", "A")]), FakeListing([])]
    async with db.session_factory()() as session:
        await _acme(session)
        refused = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        assert refused["failed"] == 1 and "403" in refused["faults"][0]["why"]
        changed = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        assert "changed shape" in changed["faults"][0]["why"]
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        empty = await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=1))
        acme = await employers.find(session, OWNER, "Acme")
        assert acme.fetch_ok is False and acme.fetch_note == "had 1 open roles, and this read answered none"
        assert acme.fetch_kind == "empty", "the state the next read decides on is its own column"
        assert empty["faults"][0]["employer"] == "Acme"


async def test_one_empty_read_closes_nothing_and_keeps_the_baseline_and_a_second_one_in_a_row_closes_them(js, fake):
    """The empty read closed every posting before the empty check and reset the roles it had to 0, so the next
    empty read looked healthy."""
    fake.answers = [FakeListing([_posting("1", "A"), _posting("2", "B")]), FakeListing([]), FakeListing([]),
                    FakeListing([_posting("3", "C")])]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        first = await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=1))
        assert (first["closed"], [f["kind"] for f in first["faults"]]) == (0, ["empty"])
        assert all(p.closed_at is None for p in (await _rows(session)).values())
        assert (acme.fetch_ok, acme.open_roles) == (False, 2), "the baseline is still what it was"
        second = await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=2))
        assert (second["closed"], [f["kind"] for f in second["faults"]]) == (2, ["empty"])
        assert all(p.closed_at is not None for p in (await _rows(session)).values())
        assert acme.open_roles == 0
        third = await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=3))
        assert third["new"] == 1 and (acme.fetch_ok, acme.open_roles) == (True, 1), "a board that fills again is fine"


async def test_an_adapter_that_fails_in_a_way_nobody_foresaw_fails_its_board_and_not_the_night(js, fake):
    """Only Refused, AdapterError, HTTPError and ValueError were caught; anything else failed the whole night."""
    fake.answers = [KeyError("a field the adapter assumed"), FakeListing([_posting("1", "A")])]
    async with db.session_factory()() as session:
        await _acme(session)
        other = await employers.find_or_make(session, OWNER, "Initech")
        other.ats, other.board = EXAMPLE_SYSTEM, {"slug": "initech"}
        await session.flush()
        result = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        assert (result["employers"], result["failed"], result["new"]) == (2, 1, 1)
        [fault] = result["faults"]
        assert fault["kind"] == "shape" and "a field the adapter assumed" in fault["why"]


def test_a_failed_read_is_classed_by_what_it_says_about_the_site_not_by_its_first_digit():
    """A 429 and a robots.txt that could not be read were `refused`, which retires a skill that works."""
    from app.jobsearch.fetch.polite import RobotsRefused

    kind = postings.fault_kind
    assert kind(adapters.AdapterError("x answered 429", 429)) == "network"
    assert kind(adapters.AdapterError("x answered 408", 408)) == "network"
    assert kind(adapters.AdapterError("x answered 408")) == "shape", "no status: the words are not read"
    assert kind(adapters.AdapterError("x answered 503", 503)) == "network"
    assert kind(adapters.AdapterError("x answered 404", 404)) == "gone"
    assert kind(adapters.AdapterError("x answered 403", 403)) == "refused"
    assert kind(adapters.AdapterError("x changed shape")) == "shape"
    assert kind(RobotsRefused("robots.txt on x asks not to read /", unreachable=True)) == "network"
    assert kind(RobotsRefused("robots.txt on x asks not to read /")) == "refused"
    assert kind(Refused("x refused an earlier request in this run")) == "refused"


async def test_a_description_that_cannot_be_read_is_marked_tried_and_left_out_of_the_next_nights_list(js, fake):
    """Detail_at was set only on success, so a posting that never read was first in line every night."""
    from app.jobsearch import actions

    fake.answers = [FakeListing([_posting("1", "Unreadable", text=None), _posting("2", "Readable", text=None)])]
    fake.details = {"2": {"text": "The whole description."}}

    async def detail(fetch, board, posting):
        if posting["ext_id"] == "1":
            raise adapters.AdapterError("feedone answered 500", 500)
        return fake.details[posting["ext_id"]]

    fake.detail = detail
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        rows = await _rows(session)
        for row in rows.values():
            row.filtered_at = NIGHT
        assert await postings.fill_details(session, _fetch(), list(rows.values())) == 1
        assert rows["1"].detail_at is not None and rows["1"].text is None, "tried, and marked as tried"
        waiting = await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": 5})
        assert waiting == {"filled": 0, "left": 0}, "tonight it asks for nothing: the readable one is read"
        rows["1"].detail_at = datetime.now(UTC) - timedelta(days=postings.DETAIL_RETRY_DAYS + 1)
        await session.flush()
        assert await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": 5}) == {
            "filled": 0, "left": 0}
        assert rows["1"].detail_at > datetime.now(UTC) - timedelta(minutes=1), "some days on, it was tried again"


async def test_a_title_with_odd_spacing_is_keyed_as_a_reported_role_with_that_title_is(js, fake):
    """The index keyed the ATS's raw title, a reported role is keyed with its whitespace folded."""
    fake.answers = [FakeListing([_posting("1", "Director  of\u00a0Programs")])]
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        row = (await _rows(session))["1"]
    assert row.match_key == "acme|director of programs" == match_key("Acme", "Director of Programs")


async def test_gated_employers_and_systems_without_an_adapter_are_never_read(js, fake):
    async with db.session_factory()() as session:
        await _acme(session, gated=True)
        other = await employers.find_or_make(session, OWNER, "Initech")
        other.ats, other.board = "pagesone", {"host": "careers-initech.pagesone.example"}
        result = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
    assert result["employers"] == 0 and fake.answers == []


async def test_details_fill_the_text_a_list_left_out(js, fake):
    fake.answers = [FakeListing([_posting("1", "Director of Programs", text=None, posted=None)])]
    fake.details = {"1": {"text": "The whole description.", "posted": "2026-09-30"}}
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        row = (await _rows(session))["1"]
        assert row.text is None
        assert await postings.fill_details(session, _fetch(), [row]) == 1
        assert row.text == "The whole description." and str(row.posted) == "2026-09-30" and row.detail_at


async def test_a_list_that_gives_only_a_count_of_places_does_not_take_the_places_a_page_named(js, fake):
    """Tenantone's list says "2 Locations" every night. The places a read of the posting's page named stay, as long as
    they are at least as many as the count; a place the list names is the list's."""
    def listed(place):
        return FakeListing([_posting("1", "Director of Programs", location=place, work_mode=None)])

    fake.answers = [listed("2 Locations"), listed("2 Locations"), listed("3 Locations"), listed("Multiple Locations"),
                    listed("Oakfield, ST")]
    named = "Oakfield, ST; Riverton, ST"
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        row = (await _rows(session))["1"]
        assert row.location == "2 Locations"
        row.location = named  # what a read of the posting's page named
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=1))
        assert row.location == named, "two places are the two the list counts"
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=2))
        assert row.location == "3 Locations", "the list counts more than the page named: unknown again"
        row.location = named
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=3))
        assert row.location == named, "a word that is no place does not take them either"
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=4))
        assert row.location == "Oakfield, ST", "a place the list names is the list's"


def test_a_posting_wants_its_places_read_only_when_its_system_names_them_and_its_list_did_not(monkeypatch):
    class Names:
        places = True

    class Lists:
        places = False

    monkeypatch.setattr(adapters, "ADAPTERS", {"tenantone": Names(), EXAMPLE_SYSTEM: Lists()})

    def wants(system, place):
        return postings.wants_places(JobPosting(ats=system, location=place))

    assert wants("tenantone", "26 Locations") and wants("tenantone", None) and wants("tenantone", "")
    assert not wants("tenantone", "Oakfield, ST") and not wants("tenantone", "Remote")
    assert not wants(EXAMPLE_SYSTEM, "26 Locations") and not wants("own_site", None) and not wants("pagesone", "")


async def test_hiring_is_counted_from_the_employers_board(js, fake):
    fake.answers = [FakeListing([_posting("1", "A"), _posting("2", "B")]), FakeListing([_posting("1", "A")])]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        assert await employers.hiring(session, acme) is None, "nothing before its board is read"
        now = datetime.now(UTC)
        await postings.refresh(session, OWNER, _fetch(), now=now - timedelta(days=40))
        await postings.refresh(session, OWNER, _fetch(), now=now - timedelta(days=5))
        assert await employers.hiring(session, acme) == {
            "open": 1, "opened": 0, "closed": 1, "days": 30, "since": (now - timedelta(days=40)).date().isoformat()}


async def test_reading_many_boards_at_once_holds_one_connection_not_one_per_board(js, monkeypatch):
    """The night's read once failed on `QueuePool limit of size 5 overflow 5`: the fetches of many boards run at once
    (`READ_AT_ONCE`) while the step's one session waits, so they must hold no connection of their own."""
    most = 0

    class Adapter:
        async def read(self, fetch, board):
            nonlocal most
            most = max(most, db.engine().pool.checkedout())
            return FakeListing([_posting(board["slug"], f"Role {board['slug']}")])

    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: Adapter()})
    async with db.session_factory()() as session:
        for n in range(40):
            board = await employers.find_or_make(session, OWNER, f"Board {n}")
            board.ats, board.board = EXAMPLE_SYSTEM, {"slug": f"b{n}"}
        await session.flush()
        result = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
    assert result["employers"] == 40 and result["new"] == 40 and result["failed"] == 0
    assert most <= 1, "the reads of the boards held connections of their own"


async def test_descriptions_are_read_as_many_at_once_as_boards_are(js, fake):
    """Docs/PRD.md 28.17: the same width as the boards (`READ_AT_ONCE`), where it was four."""
    asking = 0
    most = 0

    async def detail(fetch, board, posting):
        nonlocal asking, most
        asking += 1
        most = max(most, asking)
        await asyncio.sleep(0.02)
        asking -= 1
        return {"text": f"The whole posting {posting['ext_id']}."}

    fake.detail = detail
    count = postings.READ_AT_ONCE + 6
    fake.answers = [FakeListing([_posting(str(n), f"Director {n}", text=None) for n in range(count)])]
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        rows = list((await _rows(session)).values())
        assert await postings.fill_details(session, _fetch(), rows) == count
    assert postings.READ_AT_ONCE > 4 and most == postings.READ_AT_ONCE


class _Listing:
    """What a board's read hands over: something that can be dropped, so a test can see who still holds it."""

    def __init__(self, n: int) -> None:
        self.n, self.postings = n, bytearray(10_000)


async def test_a_board_read_handed_over_is_not_kept_for_the_rest_of_the_night(js):
    """Docs/PRD.md 28.17: a night's read stores a board as soon as it is read, so the listings of every board are not
    held at once. `finished_first` kept every finished read (its whole list of tasks, each holding its result) until the
    last board was in, so a night held every listing it read.
    Old: all 40 listings alive when the last was handed over. New: the one being handed over, and no other."""
    made: list[weakref.ref] = []

    async def read(n: int) -> _Listing:
        await asyncio.sleep(0.001 * n)
        listing = _Listing(n)
        made.append(weakref.ref(listing))
        return listing

    alive_at_the_last, handed = -1, 0
    async with db.session_factory()() as session:
        async for place, listing in postings.finished_first([read(n) for n in range(40)], session):
            assert listing.n == place
            del listing  # the caller saves a board and goes on to the next: it keeps none
            handed += 1
            if handed == 40:
                gc.collect()
                alive_at_the_last = sum(ref() is not None for ref in made)
    assert len(made) == 40 and alive_at_the_last == 0


async def test_boards_still_being_read_when_the_caller_stops_are_cancelled(js):
    """Reads not handed over when the caller leaves the loop are cancelled, not left running (the finished ones are no
    longer held, so only the ones still going are looked for)."""
    began, ended = [], []

    async def read(n: int) -> int:
        began.append(n)
        try:
            await asyncio.sleep(0 if n == 0 else 30)
            return n
        finally:
            ended.append(n)

    async with db.session_factory()() as session:
        async for _place, _result in postings.finished_first([read(n) for n in range(5)], session):
            break
        await asyncio.sleep(0.05)
    assert sorted(began) == [0, 1, 2, 3, 4] and sorted(ended) == [0, 1, 2, 3, 4], "the four slow reads were cancelled"


async def test_a_posting_that_names_its_own_company_keeps_it_even_when_that_looks_like_an_address(js):
    async with db.session_factory()() as session:
        site = await employers.find_or_make(session, OWNER, "jobs.example.test")
        counts = dict.fromkeys(("seen", "new", "closed", "reopened"), 0)
        listing = type("L", (), {"postings": [_posting("1", "Lead", company="Order.co")], "truncated": False,
                                 "note": None, "asked": None})()
        await postings.store(session, OWNER, listing, source_id=site.id, ats_name="job_site",
                             company="jobs.example.test", now=NIGHT, counts=counts, company_from_postings=True)
        (posting,) = (await session.scalars(select(JobPosting))).all()
        assert posting.company == "Order.co"


async def test_a_board_whose_save_raises_is_its_own_fault_and_the_other_boards_are_kept(js, fake, monkeypatch, caplog):
    real_store = postings.store

    async def store(session, owner, listing, **more):
        if more["company"] == "Acme":
            await session.execute(text("SELECT * FROM no_such_table"))  # a database error
        return await real_store(session, owner, listing, **more)

    fake.answers = [FakeListing([_posting("1", "A")]), FakeListing([_posting("2", "B")])]
    monkeypatch.setattr(postings, "store", store)
    async with db.session_factory()() as session:
        await _acme(session)
        other = await employers.find_or_make(session, OWNER, "Initech")
        other.ats, other.board = EXAMPLE_SYSTEM, {"slug": "initech"}
        await session.flush()
        result = await postings.refresh(session, OWNER, _fetch(), now=NIGHT)
        assert (result["employers"], result["failed"], result["new"]) == (2, 1, 1)
        [fault] = result["faults"]
        assert fault["employer"] == "Acme" and fault["kind"] == "failed"
        assert set(await _rows(session)) == {"2"}
    assert "saving the board of Acme failed" in caplog.text
