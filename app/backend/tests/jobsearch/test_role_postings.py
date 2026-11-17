"""Every role has a posting in the log (docs/PRD.md 39.4 and 39.9). A role the owner's tracker holds gets a posting with
the import as its source; a role the scout reports directly enters the log as a posting that says the scout reported it,
unless the log already holds it, which adds no second one. Neither is a board's posting: no read of a board closes it.
The text a reader or the analyst read is kept on the posting where it has none. Everything is made up."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import not_checked
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.imports import tracker
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.postings import index as postings
from app.jobsearch.postings import role_postings
from app.jobsearch.stages import GATED_OUT
from app.jobsearch.stages import LANDING_ZONE as LANDED
from app.timeutil import owner_today
from tests.builders import run_round, stored_by_title
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile, pin_clock, reported, tracker_row, tracker_workbook

SEEN = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)


async def _import(*rows):
    async with db.session_factory()() as session:
        counts = await tracker.import_tracker(session, OWNER, tracker_workbook(list(rows)))
        await session.commit()
    return counts


async def _postings() -> list[JobPosting]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(JobPosting).order_by(JobPosting.title))).all())


async def _roles() -> dict[str, Opportunity]:
    async with db.session_factory()() as session:
        return {r.title: r for r in (await session.scalars(select(Opportunity))).all()}


# --- a role the tracker holds (39.9) --------------------------------------------------------------------------------


async def test_each_role_the_import_makes_has_a_posting_with_the_import_as_its_source_and_again_adds_none(js):
    rows = [
        tracker_row("Initrode", "Director of Programs", url="https://jobs.example.test/1", found="2026-09-01",
                    applyUrl="https://apply.example.test/1", location="Oakfield, ST", moveType="hybrid"),
        tracker_row("Initrode", "Program Manager"),
        tracker_row("Initrode", "Program Manager"),  # the file names it twice: one role, one posting
    ]
    first = await _import(*rows)
    assert first["added"] == 2 and first["repeated"] == 1
    roles, posts = await _roles(), {p.title: p for p in await _postings()}
    assert set(posts) == {"Director of Programs", "Program Manager"}
    lead = posts["Director of Programs"]
    assert (lead.ats, lead.opportunity_id) == ("import", roles["Director of Programs"].id)
    assert lead.employer_id == role_postings.source_id(OWNER, "import"), "filed under the import, not an employer"
    assert lead.ext_id == str(roles["Director of Programs"].id)
    assert (lead.company, lead.match_key) == ("Initrode", "initrode|director of programs")
    assert (lead.url, lead.apply_url, lead.links) == (
        "https://jobs.example.test/1", "https://apply.example.test/1", ["https://jobs.example.test/1"])
    assert (lead.location, lead.work_mode) == ("Oakfield, ST", "hybrid")
    assert lead.first_seen == lead.last_seen == datetime(2026, 9, 1, tzinfo=UTC), "the day the tracker says"
    assert (lead.closed_at, lead.not_checked_at, lead.text) == (None, None, None)
    assert posts["Program Manager"].url is None and posts["Program Manager"].links == []

    second = await _import(*rows)
    assert second["added"] == 0
    assert len(await _postings()) == 2, "importing the file again adds no second posting"


@pytest.mark.parametrize("zone, begins", [
    ("America/Denver", datetime(2026, 9, 1, 6, 0, tzinfo=UTC)),
    ("Pacific/Auckland", datetime(2026, 8, 31, 12, 0, tzinfo=UTC)),
    ("UTC", datetime(2026, 9, 1, 0, 0, tzinfo=UTC)),
])
async def test_a_tracker_role_is_first_seen_when_its_found_day_begins_for_the_owner(js, monkeypatch, zone, begins):
    """Regression case: first seen was 00:00 UTC of the Found day, which is the evening before in
    the Americas, so the posting's seen day (and the role's found day taken from it) was the day before Found. It is the
    instant the owner's Found day begins (Denver is UTC-6 and Auckland UTC+12 on 1 September)."""
    pin_clock(monkeypatch, datetime(2026, 11, 7, 12, 0, tzinfo=UTC), zone)
    await _import(tracker_row("Initrode", "Director of Programs", found="2026-09-01"))
    [posting] = await _postings()
    assert posting.first_seen == posting.last_seen == begins
    assert owner_today(posting.first_seen) == date(2026, 9, 1), "the day it was seen is the day the tracker says"


async def test_a_role_the_app_already_has_gets_no_new_posting_from_the_import(js):
    await _import(tracker_row("Initrode", "Lead"))
    await _import(tracker_row("Initrode", "Lead"), tracker_row("Initrode", "Second"))
    assert sorted(p.title for p in await _postings()) == ["Lead", "Second"]


async def test_no_read_of_a_board_closes_the_posting_of_a_role_and_no_night_marks_it(js, monkeypatch):
    @dataclass
    class Listing:
        postings: list[dict]
        truncated: bool = False
        note: str | None = None

    class Reader:
        async def read(self, fetch, board):
            return Listing([{"ext_id": "9", "title": "Another job", "location": "Remote", "work_mode": "remote",
                             "comp": None, "comp_min": None, "comp_max": None, "posted": None,
                             "url": "https://jobs.example.test/9", "apply_url": None, "text": "x", "department": None}])

    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: Reader()})
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                    per_host_seconds=0)
    await _import(tracker_row("Initrode", "Lead", url="https://jobs.example.test/1"))
    async with db.session_factory()() as session:
        initrode = await employers.find_or_make(session, OWNER, "Initrode")
        initrode.ats, initrode.board = EXAMPLE_SYSTEM, {"slug": "initrode"}
        await session.flush()
        result = await postings.refresh(session, OWNER, fetch, only=[initrode], now=SEEN)
        assert (result["new"], result["closed"]) == (1, 0), "the read of the board closed nothing of the role's"
        # Initrode has no site to search, so nothing reads its board: its own posting is Not checked, the role's is not.
        assert await not_checked.settle(session, OWNER) == 1
        await session.commit()
    posts = {p.title: p for p in await _postings()}
    assert (posts["Lead"].closed_at, posts["Lead"].not_checked_at) == (None, None), "the role's, not the board's"
    assert posts["Another job"].not_checked_at is not None and posts["Another job"].ats == EXAMPLE_SYSTEM


def test_a_posting_keeps_a_roles_way_of_working_only_when_it_is_one():
    role = {"id": uuid.uuid4(), "owner": OWNER, "company": "Acme", "title": "Lead", "match_key": "acme|lead"}
    kept = {way: role_postings.posting_of({**role, "move_type": way}, "scout", SEEN).work_mode
            for way in ("remote", "hybrid", "onsite", "restricted_remote", "unknown", "unverified", None)}
    assert kept == {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite",
                    "restricted_remote": "restricted_remote", "unknown": None, "unverified": None, None: None}


# --- a role the scout reports directly (39.4) -----------------------------------------------------------------------


async def test_a_role_the_scout_reports_enters_the_log_as_a_posting_that_says_the_scout_reported_it(js):
    run_id = await run_round([
        reported("Hooli Labs", "Programs Director", fit=5, location="Remote", comp="$120,000 - $130,000",
                 posted="2026-10-06", apply_url="https://apply.example.test/pd",
                 description="The page the scout read.")])
    roles = await stored_by_title(run_id)
    role = roles["Programs Director"]
    assert role.status == LANDED
    [post] = await _postings()
    assert (post.ats, post.opportunity_id) == ("scout", role.id)
    assert post.employer_id == role_postings.source_id(OWNER, "scout")
    assert (post.company, post.title, post.match_key) == ("Hooli Labs", "Programs Director", role.match_key)
    assert (post.url, post.apply_url) == (role.url, "https://apply.example.test/pd")
    assert (post.location, post.comp, str(post.posted)) == ("Remote", "$120,000 - $130,000", "2026-10-06")
    assert post.text is None and post.detail_at is None, "the scout's reading is a summary: not kept as text"
    assert (post.closed_at, post.not_checked_at) == (None, None)


async def test_a_role_a_gate_screened_out_is_in_the_log_too_and_a_role_reported_twice_adds_one_posting(js):
    run_id = await run_round([
        reported("Globex", "VP Operations"),  # an employer the made-up profile excludes: stored gated out
        reported("Fabrikam", "Head of Delivery", fit=6),
        reported("Fabrikam", "Head of Delivery", fit=8),  # the same role again, in the same report
    ])
    roles = await stored_by_title(run_id)
    assert roles["VP Operations"].status == GATED_OUT and roles["Head of Delivery"].status == LANDED
    posts = await _postings()
    assert sorted(p.title for p in posts) == ["Head of Delivery", "VP Operations"], "one each, every role has one"
    assert {p.opportunity_id for p in posts} == {r.id for r in roles.values()}


async def test_a_role_reported_by_two_searches_has_one_posting(js):
    first = await run_round([reported("Hooli", "Programs Director")])
    second = await run_round([reported("Hooli", "Programs Director"), reported("Initech", "Lead")])
    assert set(await stored_by_title(first)) == {"Programs Director"}
    assert set(await stored_by_title(second)) == {"Lead"}, "the first search's role is already known"
    assert sorted(p.title for p in await _postings()) == ["Lead", "Programs Director"]


async def test_reporting_a_role_the_log_already_holds_adds_no_second_posting_whatever_its_writing(js):
    """The log holds a board's posting of the job: the same company and title in another writing, and, for a second
    job, the same link under another title. The scout's report makes the role from that posting, not beside it."""
    async with db.session_factory()() as session:
        board = uuid.uuid4()
        session.add_all([
            JobPosting(owner=OWNER, employer_id=board, ats=EXAMPLE_SYSTEM, ext_id="1", company="Fabrikam, Inc.",
                       title="Senior Director, Programs",
                       match_key=match_key("Fabrikam, Inc.", "Senior Director, Programs"),
                       url="https://jobs.example.test/fab/1", first_seen=SEEN, last_seen=SEEN, links=[],
                       filtered_at=SEEN),
            JobPosting(owner=OWNER, employer_id=board, ats=EXAMPLE_SYSTEM, ext_id="2", company="Hooli",
                       title="Head of Delivery", match_key=match_key("Hooli", "Head of Delivery"),
                       url="https://jobs.example.test/hooli/2", first_seen=SEEN, last_seen=SEEN, links=[],
                       filtered_at=SEEN)])
        await session.commit()
    run_id = await run_round([
        reported("Fabrikam", "Sr. Director - Programs"),
        reported("Hooli", "Programs Director", url="https://jobs.example.test/hooli/2")])
    roles = await stored_by_title(run_id)
    assert set(roles) == {"Sr. Director - Programs", "Programs Director"}
    posts = {p.ext_id: p for p in await _postings()}
    assert set(posts) == {"1", "2"}, "no second posting for either"
    assert posts["1"].opportunity_id == roles["Sr. Director - Programs"].id
    assert posts["2"].opportunity_id == roles["Programs Director"].id
    assert {p.ats for p in posts.values()} == {EXAMPLE_SYSTEM}


async def test_a_posting_the_log_holds_that_is_another_roles_is_not_taken_and_not_doubled(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        taken = Opportunity(owner=OWNER, slug="a", company="Fabrikam", title="Lead", match_key="fabrikam|lead")
        mine = Opportunity(owner=OWNER, slug="b", company="Fabrikam", title="Other", match_key="fabrikam|other")
        session.add_all([taken, mine])
        await session.flush()
        session.add(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id="1",
                               company="Fabrikam",
                               title="Lead", match_key="fabrikam|lead", first_seen=SEEN, last_seen=SEEN, links=[],
                               opportunity_id=taken.id))
        await session.flush()
        held = await role_postings.log_reported(session, {
            "id": mine.id, "owner": OWNER, "company": "Fabrikam", "title": "Lead", "match_key": "fabrikam|lead"})
        count = await session.scalar(select(func.count()).select_from(JobPosting))
    assert count == 1 and held.opportunity_id == taken.id, "it is the other role's: neither taken nor doubled"
