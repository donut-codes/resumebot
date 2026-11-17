"""A posting from a board the app no longer reads is Not checked (docs/PRD.md 39.8): its board stops being read when the
owner switches the employer, the site or its job system off, when the app turns a site off because it cannot be read, or
when the employer is gated. Its open postings are marked, with the day they were last seen, instead of staying open.
They are not closed, no stage works them and the log counts none as open. When the board is read again, each of its
postings is open or closed as that read finds it. Adapters are stand-ins; no network. Everything is made up."""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import actions, adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.employers import not_checked
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.gating import sift, stopped
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.postings import index as postings
from app.jobsearch.postings import role_postings
from app.jobsearch.profile import store as profiles
from app.sites import job_systems, where
from app.sites.models import Site
from tests.builders import ONE, WORLD_NOW, browser_card, browser_world, scan_hand_in
from tests.support import DEFAULT_OWNER, EXAMPLE_SYSTEM, OWNER, load_profile

SEEN = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
LATER = SEEN + timedelta(days=5)


@dataclass
class Listing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class Board:
    """A job system's reader that lists what it is told, once for each read."""

    answers: list = field(default_factory=list)

    async def read(self, fetch, board):
        return self.answers.pop(0)

    async def detail(self, fetch, board, posting):
        return {"text": f"The whole posting for {posting['title']}."}


def _listed(*ext_ids: str, truncated: bool = False) -> Listing:
    return Listing([{"ext_id": e, "title": e, "location": "Remote", "work_mode": "remote", "comp": None,
                     "comp_min": None, "comp_max": None, "posted": None, "url": f"https://jobs.example.test/{e}",
                     "apply_url": None, "text": f"About {e}.", "department": None} for e in ext_ids], truncated)


@pytest.fixture
def boards(monkeypatch):
    """Two job systems the app can read, each a stand-in; every fetch goes through a mock transport."""
    made = {EXAMPLE_SYSTEM: Board(), "feedfour": Board()}
    monkeypatch.setattr(adapters, "ADAPTERS", made)
    monkeypatch.setattr(polite, "make", _fetch)
    return made


def _fetch() -> Fetcher:
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


async def _employer(session, name: str, system: str | None = EXAMPLE_SYSTEM, *, on: bool = True, gated: bool = False):
    """An employer on a job system, with its site to search (on or off), as the app makes them."""
    employer = await employers.find_or_make(session, OWNER, name)
    employer.ats, employer.board, employer.gated = system, {"slug": name.lower()}, gated
    site = Site(owner=OWNER, url=f"https://{name.lower()}.example.test/careers", kind="employer", on=on,
                employer_id=employer.id, label=name, created_at=SEEN, turned_on_at=SEEN if on else None)
    session.add(site)
    await session.flush()
    return employer, site


def _posting(source: uuid.UUID, ext_id: str, company: str = "Acme", **more) -> JobPosting:
    more.setdefault("ats", EXAMPLE_SYSTEM)
    more.setdefault("filtered_at", SEEN)
    more.setdefault("url", f"https://jobs.example.test/{ext_id}")
    return JobPosting(owner=OWNER, employer_id=source, ext_id=ext_id, company=company, title=ext_id,
                      match_key=match_key(company, ext_id), first_seen=SEEN, last_seen=SEEN, links=[], **more)


def _one_of_each(source: uuid.UUID, company: str) -> list[JobPosting]:
    """A board's postings as each queue sees them: waiting for the title check without its text, waiting for a reader,
    never filtered, and stopped by a gate."""
    return [
        _posting(source, f"{company}-title", company, route="titles"),
        _posting(source, f"{company}-reader", company, route="reader", judged_by="titles",
                 text="The whole text. " * 30),
        _posting(source, f"{company}-new", company, filtered_at=None),
        _posting(source, f"{company}-stopped", company, filtered_by="the place", judged_by="gate"),
    ]


async def _queues(session) -> dict[str, int]:
    """How many postings each stage would work now."""
    return {
        "text to fetch": len((await session.scalars(select(JobPosting).where(*postings.needing_text(OWNER)))).all()),
        "gated list": await session.scalar(select(func.count()).select_from(JobPosting).where(
            *stopped.stopped_where(OWNER))),
        "filter": len((await session.scalars(select(JobPosting).where(
            JobPosting.owner == OWNER, JobPosting.is_open, JobPosting.filtered_at.is_(None)))).all()),
    }


async def _rows(session) -> dict[str, JobPosting]:
    return {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}


def _marked(rows: dict[str, JobPosting]) -> set[str]:
    return {n for n, p in rows.items() if p.not_checked_at is not None}


# --- what open means ------------------------------------------------------------------------------------------------


async def test_a_posting_is_open_only_when_it_is_neither_closed_nor_not_checked(js):
    source = uuid.uuid4()
    async with db.session_factory()() as session:
        session.add_all([_posting(source, "open"), _posting(source, "closed", closed_at=SEEN),
                         _posting(source, "unchecked", not_checked_at=SEEN)])
        await session.flush()
        rows = await _rows(session)
        by_query = {p.ext_id for p in (await session.scalars(select(JobPosting).where(JobPosting.is_open))).all()}
    assert by_query == {"open"}
    assert [rows[n].is_open for n in ("open", "closed", "unchecked")] == [True, False, False]


def test_no_query_asks_whether_a_posting_is_open_by_its_closing_alone():
    """Every queue and every count of open postings asks `JobPosting.is_open`, so a posting nothing is checking is in
    none of them. This keeps a new query from asking `closed_at` alone: the places that may are the definition and the
    baselines a read compares itself with (what the board had, whether or not it is read since): a job site's read
    (`site_reads.py`) and a board read for only some of its places (`postings.open_within`)."""
    app = db.BACKEND_DIR / "app"
    allowed = {"jobsearch/models.py", "jobsearch/postings/site_reads.py", "jobsearch/postings/index.py"}
    asking = [path.relative_to(app).as_posix() for path in sorted(app.rglob("*.py"))
              if path.relative_to(app).as_posix() not in allowed and "closed_at.is_(None)" in path.read_text("utf-8")]
    assert asking == []


# --- switching a job system off -------------------------------------------------------------------------------------


async def test_switching_a_job_system_off_marks_its_open_postings_and_takes_them_out_of_every_queue(js, boards):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        acme, _ = await _employer(session, "Acme", EXAMPLE_SYSTEM)
        initech, _ = await _employer(session, "Initech", "feedfour")
        acme.fetched_at = initech.fetched_at = SEEN
        session.add_all([*_one_of_each(acme.id, "Acme"), *_one_of_each(initech.id, "Initech"),
                         _posting(acme.id, "Acme-closed", "Acme", closed_at=SEEN)])
        await session.flush()
        assert await _queues(session) == {"text to fetch": 2, "gated list": 2, "filter": 2}
        assert (await employers.hiring(session, acme))["open"] == 4
        assert len(await employer_posting.own_listings(session, OWNER, acme)) == 4

        await job_systems.switch(session, OWNER, EXAMPLE_SYSTEM, False)
        rows = await _rows(session)
        assert _marked(rows) == {"Acme-title", "Acme-reader", "Acme-new", "Acme-stopped"}, "only its open postings"
        assert rows["Acme-title"].last_seen == SEEN, "the day it was last seen is kept"
        assert rows["Acme-title"].closed_at is None, "nothing says the employer took it down"
        assert rows["Acme-closed"].not_checked_at is None and rows["Acme-closed"].closed_at == SEEN

        assert await _queues(session) == {"text to fetch": 1, "gated list": 1, "filter": 1}, (
            "Acme's postings are in no queue")
        assert (await employers.hiring(session, acme))["open"] == 0, "the log counts none of them as open"
        assert (await employers.hiring(session, initech))["open"] == 4
        assert await employer_posting.own_listings(session, OWNER, acme) == [], "nor offered as a role's own listing"
        looked = await sift.sift(session, OWNER, await profiles.current(session, OWNER))
        assert looked["looked_at"] == 1, "the filter looks at Initech's new posting and not at Acme's"
        assert rows["Acme-new"].filtered_at is None


async def test_switching_it_on_again_marks_nothing_back_and_a_read_sets_each_posting_open_or_closed(js, boards):
    async with db.session_factory()() as session:
        acme, _ = await _employer(session, "Acme", EXAMPLE_SYSTEM)
        session.add_all([_posting(acme.id, n, "Acme") for n in ("a", "b", "c")])
        await session.flush()
        await job_systems.switch(session, OWNER, EXAMPLE_SYSTEM, False)
        await job_systems.switch(session, OWNER, EXAMPLE_SYSTEM, True)
        assert _marked(await _rows(session)) == {"a", "b", "c"}, "they stay as they are until the night reads it"

        boards[EXAMPLE_SYSTEM].answers = [_listed("a", "b")]
        result = await postings.refresh(session, OWNER, _fetch(), only=[acme], now=LATER)
        rows = await _rows(session)
    assert (result["seen"], result["closed"], result["reopened"]) == (2, 1, 0)
    assert (rows["a"].not_checked_at, rows["a"].closed_at, rows["a"].last_seen) == (None, None, LATER)
    assert (rows["b"].not_checked_at, rows["b"].closed_at) == (None, None)
    assert rows["c"].closed_at == LATER and rows["c"].not_checked_at is None, "gone from a whole read: closed"


async def test_a_read_cut_short_cannot_say_so_a_posting_it_did_not_return_stays_not_checked(js, boards):
    async with db.session_factory()() as session:
        acme, _ = await _employer(session, "Acme", EXAMPLE_SYSTEM)
        session.add_all([_posting(acme.id, n, "Acme") for n in ("a", "b")])
        await session.flush()
        await job_systems.switch(session, OWNER, EXAMPLE_SYSTEM, False)
        await job_systems.switch(session, OWNER, EXAMPLE_SYSTEM, True)
        boards[EXAMPLE_SYSTEM].answers = [_listed("a", truncated=True)]
        await postings.refresh(session, OWNER, _fetch(), only=[acme], now=LATER)
        rows = await _rows(session)
    assert rows["a"].not_checked_at is None, "a posting the read saw is open"
    assert rows["b"].not_checked_at is not None and rows["b"].closed_at is None, "one it could not say is as it was"


async def test_settling_again_marks_nothing_twice_and_keeps_the_day_it_was_marked(js, boards):
    async with db.session_factory()() as session:
        acme, _ = await _employer(session, "Acme", EXAMPLE_SYSTEM, on=False)
        session.add_all([_posting(acme.id, "a", "Acme"), _posting(acme.id, "b", "Acme", closed_at=SEEN)])
        await session.flush()
        first = await not_checked.settle(session, OWNER, now=SEEN)
        again = await not_checked.settle(session, OWNER, now=LATER)
        rows = await _rows(session)
    assert (first, again) == (1, 0)
    assert rows["a"].not_checked_at == SEEN, "the day it was marked is not moved"
    assert rows["b"].not_checked_at is None, "a closed posting is closed, not unchecked"


async def test_marking_while_the_nights_read_holds_the_writer_turn_waits_for_it_and_marks_each_posting_once(
        js, boards):
    """There is no posting to hold on SQLite, only the one writer turn, which the night's read has while it writes. The
    owner switching a job system off meanwhile waits for it (a reader sees nothing marked meanwhile) and then marks both
    postings; the read's own last step finds nothing left."""
    async with db.session_factory()() as setup:
        acme, _ = await _employer(setup, "Acme", EXAMPLE_SYSTEM, on=False)
        setup.add_all([_posting(acme.id, "held", "Acme"), _posting(acme.id, "free", "Acme")])
        await setup.commit()
    holding, let_go = asyncio.Event(), asyncio.Event()

    async def night():
        async with db.session_factory()() as read:
            held = await read.scalar(select(JobPosting).where(JobPosting.ext_id == "held"))
            held.last_seen = LATER  # the read is at work on it, and has not committed
            await read.flush()
            holding.set()
            await let_go.wait()
            await read.commit()

    async def owner_marks():
        async with db.session_factory()() as owner:
            marked = await not_checked.settle(owner, OWNER, now=LATER)
            await owner.commit()
            return marked

    night_task = asyncio.get_running_loop().create_task(night())
    await holding.wait()
    marking = asyncio.get_running_loop().create_task(owner_marks())
    await asyncio.sleep(0.3)
    assert not marking.done(), "the marking waits for the turn"
    async with db.read_session_factory()() as reader:
        assert _marked(await _rows(reader)) == set(), "and nothing is marked meanwhile"
    let_go.set()
    assert await asyncio.wait_for(marking, 10) == 2, "both postings, once"
    await night_task
    async with db.session_factory()() as last:
        assert await not_checked.settle(last, OWNER, now=LATER) == 0, "the read's last step finds nothing left"
        await last.commit()
    async with db.session_factory()() as session:
        assert _marked(await _rows(session)) == {"held", "free"}


# --- the other ways a board stops being read ------------------------------------------------------------------------


async def test_switching_off_an_employers_site_or_a_job_board_marks_its_postings_and_so_does_the_app(js, boards):
    async with db.session_factory()() as session:
        acme, acme_site = await _employer(session, "Acme", EXAMPLE_SYSTEM)
        initech, initech_site = await _employer(session, "Initech", "feedfour")
        hooli, _ = await _employer(session, "Hooli", EXAMPLE_SYSTEM)
        board = Site(owner=OWNER, url="https://board.example.test/jobs", kind="job_board", on=True, created_at=SEEN)
        session.add(board)
        await session.flush()
        session.add_all([_posting(acme.id, "acme-1", "Acme"), _posting(initech.id, "initech-1", "Initech"),
                         _posting(hooli.id, "hooli-1", "Hooli"),
                         _posting(board.id, "board-1", "Globex", ats="job_site")])
        await session.flush()

        await where.switch(session, OWNER, acme_site.id, False)  # the owner switches the employer off
        assert _marked(await _rows(session)) == {"acme-1"}

        await where.switch(session, OWNER, board.id, False)  # ... and a job board
        assert _marked(await _rows(session)) == {"acme-1", "board-1"}

        await where.turn_off_unreadable(session, initech_site, "its listings only load in a browser")  # the app does
        assert _marked(await _rows(session)) == {"acme-1", "board-1", "initech-1"}, "hooli's board is still read"


async def test_gating_an_employer_marks_its_postings_and_the_nights_read_marks_a_board_nothing_reads(js, boards):
    async with db.session_factory()() as session:
        acme, _ = await _employer(session, "Acme", EXAMPLE_SYSTEM)
        initech, _ = await _employer(session, "Initech", "feedfour")
        session.add_all([_posting(acme.id, "acme-1", "Acme"), _posting(initech.id, "initech-1", "Initech")])
        await session.flush()

        await employers.edit(session, OWNER, acme.id, {"gated": True, "gated_reason": "never again"})
        assert _marked(await _rows(session)) == {"acme-1"}, "a gated employer's board is no longer read"

        # A source that is no employer's and no site's, after the last switch: nothing reads it, and the night says so.
        session.add(_posting(uuid.uuid4(), "stray-1", "Globex"))
        await session.flush()
        boards["feedfour"].answers = [_listed("initech-1")]
        done = await actions.read_boards(session, None, {"owner": OWNER})
        rows = await _rows(session)
    assert done["not_checked"] == 1 and _marked(rows) == {"acme-1", "stray-1"}, "the night marks what is left"
    assert rows["initech-1"].last_seen > SEEN and rows["initech-1"].not_checked_at is None, "the board it read: open"


async def test_a_board_with_its_site_on_and_its_employer_not_gated_is_read_with_or_without_a_job_system(js, boards):
    async with db.session_factory()() as session:
        acme, _ = await _employer(session, "Acme", EXAMPLE_SYSTEM, gated=True)
        plain, _ = await _employer(session, "Plain", None)  # on its own site: no job system to switch off
        session.add_all([_posting(acme.id, "acme-1", "Acme"), _posting(plain.id, "plain-1", "Plain", ats="own_site")])
        await session.flush()
        assert await not_checked.settle(session, OWNER) == 1
        rows = await _rows(session)
    assert _marked(rows) == {"acme-1"}


async def test_the_postings_of_roles_are_no_boards_and_are_never_marked(js, boards):
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="acme-lead", company="Acme", title="Lead", match_key="acme|lead")
        session.add(role)
        await session.flush()
        made = await role_postings.log_reported(session, {
            "id": role.id, "owner": OWNER, "company": "Acme", "title": "Lead", "match_key": "acme|lead"})
        assert made.ats == "scout" and made.is_open
        assert await not_checked.settle(session, OWNER) == 0
        assert (await _rows(session))[str(role.id)].not_checked_at is None


# --- a site read in the owner's browser -----------------------------------------------------------------------------


async def test_a_site_read_in_the_browser_is_read_while_it_is_on_and_seeing_a_posting_again_opens_it(js):
    sites = await browser_world()
    later = WORLD_NOW + timedelta(days=1)
    await scan_hand_in([browser_card(1)], now=WORLD_NOW)
    async with db.session_factory()() as session:
        [row] = (await session.scalars(select(JobPosting))).all()
        assert await not_checked.settle(session, DEFAULT_OWNER) == 0, "on, and read by the owner's browser"
        await where.switch(session, DEFAULT_OWNER, sites[ONE], False)
        assert row.not_checked_at is not None and row.closed_at is None, "switched off: nobody reads it"
        await where.switch(session, DEFAULT_OWNER, sites[ONE], True)
        assert row.not_checked_at is not None, "switched on: not read yet"
        await session.commit()
    await scan_hand_in([browser_card(1)], sid="browser_reader-later", now=later)
    async with db.session_factory()() as session:
        row = await session.get(JobPosting, row.id)
    assert row.not_checked_at is None and row.last_seen == later, "its own sighting settles it: open"
