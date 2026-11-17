"""A role found on a job board, followed to the employer's own posting (docs/PRD.md path 32): two links per role (the
employer's own posting, preferred everywhere, and the job board it was found on, kept for good), the code finder that
matches a role to a listing the app already reads (confident, unsure or none), the check every employer link passes (one
posting, never a front page), the analyst that never sources a role, and the employer's two sites (its website and its
careers page, the careers page learned only where none is kept and never over the owner's). Every name, link and
listing is made up; nothing reaches the network."""

import io
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import openpyxl
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.address import is_board_link
from app.jobsearch.doors import tools
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import follow as ep
from app.jobsearch.employers import resolve
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.research import analysis
from app.models.work import TrailEvent
from app.sites import where_view
from app.sites.models import Site
from tests.builders import analysis_setup, export_workbook, findings, run_round, stored_by_title, submit_findings
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import OWNER, load_profile, reported

pytestmark = pytest.mark.usefixtures("example_world")

BOARD = "https://www.board.test/jobs/view/4242"
OWN = "https://job-boards.feedsys.test/fabrikam/jobs/777"
SYSTEM_BOARD = "https://job-boards.feedsys.test/fabrikam"


def listing(title="Program Manager", location="Austin, TX", work_mode="hybrid", url=OWN, apply_url=None):
    """A listing as the matcher reads one (the index's rows have these attributes)."""
    return SimpleNamespace(title=title, location=location, work_mode=work_mode, url=url, apply_url=apply_url)


# --- the matcher: title -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("a, b", [
    ("Sr. Program Mgr", "Senior Program Manager"),
    ("Program Manager (Remote)", "Program Manager"),
    ("Program Manager - Remote", "Program Manager"),
    ("Program Manager | US", "program manager"),
    ("Analyst II", "Analyst 2"),
    ("Analyst I", "Analyst 1"),
    ("Director of Programs", "Director, Programs"),
    ("Strategy & Operations Lead", "Strategy and Operations Lead"),
    ("VP, Operations", "Vice President Operations"),
    ("Sr. Manager, Service Strategy", "Sr. Manager, Service Strategy Full-time"),
    ("Program Manager", "Program Manager - Full Time"),
])
def test_two_titles_are_one_job_once_the_usual_abbreviations_and_where_tags_are_folded(a, b):
    assert ep.titles_match(a, b) and ep.titles_match(b, a)


@pytest.mark.parametrize("a, b", [
    ("Program Manager", "Senior Program Manager"),
    ("Program Manager (Contract)", "Program Manager"),
    ("Analyst I", "Analyst II"),
    ("Program Manager", "Project Manager"),
    ("Program Manager - Platform", "Program Manager"),
    ("Program Manager", "Program Manager Part-time"),
    ("", ""),
])
def test_titles_that_are_different_jobs_do_not_match(a, b):
    assert not ep.titles_match(a, b)


# --- the matcher: place ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("a, mode_a, b, mode_b, agree", [
    ("Austin, TX", "hybrid", "Austin, Texas, United States", None, True),
    ("Remote, US", "remote", "Remote - United States", None, True),
    ("United States", "remote", "", "remote", True),
    ("Austin, TX; Remote", None, "Remote", None, True),
    ("Austin, TX", "onsite", "Remote", "remote", False),
    ("Remote", "remote", "Dallas, TX", "onsite", False),
    ("Austin, TX", None, "Dallas, TX", None, False),
    ("Lakeside, IL", None, "Lakeside, MO", None, False),
    ("Austin, TX", None, "", None, None),
    ("", None, "Austin, TX", None, None),
    ("United States", None, "Austin, TX", None, None),
])
def test_two_places_agree_when_both_are_remote_or_the_same_city_and_say_nothing_when_one_is_not_stated(
        a, mode_a, b, mode_b, agree):
    assert ep.places_agree(a, mode_a, b, mode_b) is agree


# --- the matcher: a listing among the employer's own -----------------------------------------------------------


def test_one_listing_with_the_same_title_and_a_place_that_agrees_is_confident():
    verdict, found = ep.pick("Program Manager", "Austin, TX", "hybrid", [
        listing("Data Analyst"), listing("Program Manager", url=OWN), listing("Program Manager", "Dallas, TX", "onsite",
                                                                              url="https://x.example.test/dallas")])
    assert verdict == ep.CONFIDENT and found.url == OWN


def test_a_listing_posted_twice_under_one_link_is_still_one():
    verdict, found = ep.pick("Program Manager", "Austin, TX", "hybrid", [listing(), listing(location="Austin, Texas")])
    assert verdict == ep.CONFIDENT and found.url == OWN


@pytest.mark.parametrize("place, mode", [
    ("Dallas, TX", "onsite"),  # the title is there, the place is not
    (None, None),  # the role does not say where
    ("Remote, US", "remote"),  # a remote role against an on-site listing
])
def test_a_listing_with_the_title_but_a_place_that_differs_or_is_not_stated_is_unsure(place, mode):
    assert ep.pick("Program Manager", place, mode, [listing()]) == (ep.UNSURE, None)


def test_two_listings_that_both_fit_are_unsure_and_so_is_a_listing_with_no_link():
    two = [listing(url=OWN), listing(url="https://job-boards.feedsys.test/fabrikam/jobs/778")]
    assert ep.pick("Program Manager", "Austin, TX", "hybrid", two) == (ep.UNSURE, None)
    assert ep.pick("Program Manager", "Austin, TX", "hybrid", [listing(url=None)]) == (ep.UNSURE, None)


def test_no_listing_with_the_title_is_none():
    assert ep.pick("Accountant", "Austin, TX", "hybrid", [listing(), listing("Analyst")]) == (ep.NONE, None)
    assert ep.pick("Accountant", "Austin, TX", "hybrid", []) == (ep.NONE, None)


# --- the two links --------------------------------------------------------------------------------------------------


def test_an_employers_own_careers_page_on_a_boards_host_is_the_employers_not_the_boards():
    """Every address on one big host counted as a job board, so the posting pages of an employer
    whose own careers site lives there were "board only" and no source could be one of them. Only the one posting's
    page is the employer's: the host's search and the careers site's own list are still pages of many postings."""
    posting = "https://www.searchsite.test/about/careers/applications/jobs/results/123456789-program-manager"
    assert not is_board_link(posting)
    assert not is_board_link(posting + "?location=Remote&hl=en_US")
    assert is_board_link("https://www.searchsite.test/search?q=program+manager+jobs")
    assert is_board_link("https://www.searchsite.test/about/careers/applications/jobs/results/?q=program%20manager")
    assert is_board_link("https://www.searchsite.test/about/careers/applications/")
    assert is_board_link("https://www.searchsite.test/about/careers/applications/jobs/results/not-a-number")
    assert is_board_link("https://www.searchsite.test/maps/about/careers/applications/jobs/results/123456789-x")
    assert ep.links_of(SimpleNamespace(url=posting, board_url=None)) == {"employer_url": posting, "board_url": None}


def test_a_job_board_link_is_one_the_app_knows_by_name_or_one_of_the_owners_job_sites():
    assert is_board_link("https://www.board.test/jobs/view/1") and is_board_link("https://uk.listings.test/x")
    assert is_board_link("https://thirdboard.test/job-detail/1")
    assert not is_board_link("https://notboard.test/x") and not is_board_link(None)
    assert not is_board_link(OWN)
    assert not is_board_link("https://jobs.sitebox.example.test/1")
    assert is_board_link("https://jobs.sitebox.example.test/1", {"jobs.sitebox.example.test"})


def test_the_two_links_of_a_role_are_the_employers_and_the_job_boards():
    role = Opportunity(url=OWN, board_url=BOARD)
    assert ep.links_of(role) == {"employer_url": OWN, "board_url": BOARD} and not ep.is_board_only(role)
    only = Opportunity(url=BOARD, board_url=BOARD)
    assert ep.links_of(only) == {"employer_url": None, "board_url": BOARD} and ep.is_board_only(only)
    elsewhere = Opportunity(url=OWN, board_url=None)  # found on the employer's own site
    assert ep.links_of(elsewhere) == {"employer_url": OWN, "board_url": None} and not ep.is_board_only(elsewhere)
    old = Opportunity(url=BOARD, board_url=None)  # found before the board link was kept
    assert ep.links_of(old) == {"employer_url": None, "board_url": BOARD} and ep.is_board_only(old)
    assert not ep.is_board_only(Opportunity(url=None, board_url=None))


def test_a_board_link_that_is_the_employers_own_address_is_the_employers_link_not_a_job_boards():
    """A role stored while its host was counted a job board has that address as both links."""
    both = Opportunity(url=OWN, board_url=OWN)
    assert ep.links_of(both) == {"employer_url": OWN, "board_url": None} and not ep.is_board_only(both)
    assert ep.links_of(both, {"job-boards.feedsys.test"}) == {"employer_url": None, "board_url": OWN}
    assert ep.is_board_only(both, {"job-boards.feedsys.test"}), "still a board while the owner's job sites say so"


async def test_a_site_listed_as_the_employers_own_is_never_one_of_the_job_boards(js):
    async with db.session_factory()() as session:
        for kind, page in (("job_board", "home"), ("employer", "search-results")):
            session.add(Site(owner=OWNER, url=f"https://job-boards.feedsys.test/{page}", kind=kind, on=True,
                             created_at=datetime.now(UTC)))
        session.add(Site(owner=OWNER, url="https://jobs.sitebox.example.test/search", kind="job_board", on=True,
                         created_at=datetime.now(UTC)))
        await session.commit()
        assert await ep.job_board_hosts(session, OWNER) == {"jobs.sitebox.example.test"}


def test_adopting_the_employers_link_keeps_the_job_board_link_even_when_none_was_recorded():
    role = Opportunity(url=BOARD, board_url=None)
    assert ep.adopt(role, OWN) is True
    assert (role.url, role.board_url) == (OWN, BOARD), "the link it was found at is never dropped"
    assert ep.adopt(role, OWN) is False and ep.adopt(role, "") is False and role.url == OWN


# --- the code finder, in the database -------------------------------------------------------------------------


async def _employer(session, name="Fabrikam", **over) -> Employer:
    values = {"ats": "feedsys", "board": {"slug": "fabrikam"}, "board_url": SYSTEM_BOARD, "ats_found_by": "link",
              **over}
    employer = Employer(owner=OWNER, key=employers.key_of(name), name=name, **values)
    session.add(employer)
    await session.flush()
    return employer


async def _listing(session, employer, title="Program Manager", location="Austin, TX", work_mode="hybrid", url=OWN,
                   **over) -> JobPosting:
    now = datetime.now(UTC)
    system, ext_id = over.pop("ats", "feedsys"), over.pop("ext_id", url or title)
    posting = JobPosting(
        owner=OWNER, employer_id=employer.id, ats=system, ext_id=ext_id, company=employer.name, title=title,
        location=location, work_mode=work_mode, url=url, first_seen=now, last_seen=now, **over)
    session.add(posting)
    await session.flush()
    return posting


async def _role(session, employer, title="Program Manager", **over) -> Opportunity:
    values = {
        "owner": OWNER, "slug": title.lower().replace(" ", "-"), "company": employer.name, "title": title,
        "status": stages.SECOND_IMPRESSION, "location": "Austin, TX", "move_type": "hybrid", "url": BOARD,
            "board_url": BOARD,
        "board": "Exboard", "match_key": f"{employer.key}|{title.lower()}", "employer_id": employer.id,
        "found": date.today(), "origin": "scout", **over}
    role = Opportunity(**values)
    session.add(role)
    await session.flush()
    return role


async def _trail(session, role) -> list[TrailEvent]:
    return list((await session.scalars(select(TrailEvent).where(
        TrailEvent.link_id == role.id, TrailEvent.event == "employer_posting_found"))).all())


async def test_a_confident_match_makes_the_employers_posting_the_link_and_keeps_the_job_board_link(js):
    async with db.session_factory()() as session:
        employer = await _employer(session)
        await _listing(session, employer)
        role = await _role(session, employer)
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 1}
        await session.commit()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role.id)
        employer = await session.get(Employer, role.employer_id)
        assert (role.url, role.board_url) == (OWN, BOARD), "the employer's link is read; the board's is kept"
        assert ep.links_of(role) == {"employer_url": OWN, "board_url": BOARD}
        assert employer.careers_url == SYSTEM_BOARD, "the careers site, learned from the job system the posting is on"
        assert employer.sources["careers_url"]["source"] == "its job system's board"
        (said,) = await _trail(session, role)
        assert said.who == "resumebot" and OWN in said.detail and "Exboard" in said.detail
        # nothing is looked at twice: the role has its employer's link now
        assert await ep.follow_roles(session, OWNER) == {"looked": 0, "followed": 0}


async def test_an_unsure_match_or_none_stores_nothing(js):
    async with db.session_factory()() as session:
        employer = await _employer(session)
        await _listing(session, employer, location="Dallas, TX", work_mode="onsite")
        elsewhere = await _role(session, employer)  # the title is there, the place differs: unsure
        nothing = await _role(session, employer, title="Accountant")  # no listing has the title: none
        assert await ep.follow_roles(session, OWNER) == {"looked": 2, "followed": 0}
        await session.commit()
    async with db.session_factory()() as session:
        for role_id in (elsewhere.id, nothing.id):
            role = await session.get(Opportunity, role_id)
            assert (role.url, role.board_url) == (BOARD, BOARD)
            assert await _trail(session, role) == []
        assert (await session.get(Employer, elsewhere.employer_id)).careers_url is None, "nothing learned from it"


async def test_only_the_employers_own_open_listings_count(js):
    async with db.session_factory()() as session:
        employer = await _employer(session)
        other = await _employer(session, "Contoso")
        role = await _role(session, employer)
        # not the employer's own: a job site's listing of it, the same title at another employer, one that closed, and
        # one whose link is a job board's
        await _listing(session, employer, ats="browser", url="https://www.board.test/jobs/view/9", ext_id="a")
        await _listing(session, employer, ats="job_site", url="https://jobs.sitebox.example.test/9", ext_id="b")
        await _listing(session, other, url="https://job-boards.feedsys.test/contoso/jobs/1", ext_id="c")
        await _listing(session, employer, url=OWN, ext_id="d", closed_at=datetime.now(UTC) - timedelta(days=1))
        await _listing(session, employer, url="https://www.listings.test/viewjob?jk=1", ext_id="e")
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 0}
        await _listing(session, employer, url="https://careers.fabrikam.test/jobs/5", ext_id="f", ats="own_site")
        # its own site's saved skill counts
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 1}
        assert role.url == "https://careers.fabrikam.test/jobs/5"


async def test_only_roles_before_applying_that_have_only_a_job_board_link_are_looked_at(js):
    async with db.session_factory()() as session:
        employer = await _employer(session)
        await _listing(session, employer)
        await _role(session, employer, title="Applied Role", status=stages.APPLIED)
        await _role(session, employer, title="Gated Role", status=stages.GATED_OUT)
        await _role(session, employer, title="Own Site Role", url=OWN, board_url=None, board=None)
        found = await _role(session, employer, status=stages.READY_TO_LAUNCH)
        stale = await _role(session, employer, title="Old Way", board_url=None, match_key="fabrikam|old way")
        await _listing(session, employer, title="Old Way", url="https://job-boards.feedsys.test/fabrikam/jobs/2",
                       ext_id="old")
        assert await ep.follow_roles(session, OWNER) == {"looked": 2, "followed": 2}
        assert found.url == OWN
        assert (stale.url, stale.board_url) == ("https://job-boards.feedsys.test/fabrikam/jobs/2", BOARD), (
            "a role found on a board before the link was kept still keeps it")


async def test_a_job_site_the_owner_searches_is_a_job_board_too(js):
    sitebox = "https://jobs.sitebox.example.test/view/12"
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://jobs.sitebox.example.test/search", kind="job_board", on=True,
                         created_at=datetime.now(UTC)))
        employer = await _employer(session)
        await _listing(session, employer)
        role = await _role(session, employer, url=sitebox, board_url=None, board="Sitebox")
        hosts = await ep.job_board_hosts(session, OWNER)
        assert hosts == {"jobs.sitebox.example.test"} and ep.is_board_only(role, hosts)
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 1}
        assert (role.url, role.board_url) == (OWN, sitebox)


# --- the posting's own Apply link -----------------------------------------------------------------------------

APPLY_OWN = "https://careers.fabrikam.test/apply/42"


async def test_an_apply_link_on_the_employers_own_domain_is_taken_before_any_listing_is_looked_at(js):
    async with db.session_factory()() as session:
        employer = await _employer(session, website="https://www.fabrikam.test")
        await _listing(session, employer)  # a listing that would match too, under another link
        role = await _role(session, employer, apply_url=APPLY_OWN)
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 1}
        assert (role.url, role.board_url, role.apply_url) == (APPLY_OWN, BOARD, APPLY_OWN)
        (said,) = await _trail(session, role)
        assert "Apply link" in said.sentence and APPLY_OWN in said.detail and "Exboard" in said.detail
        assert employer.careers_url == SYSTEM_BOARD


async def test_an_apply_form_on_the_employers_job_system_is_followed_as_the_postings_own_page(js):
    """PRD 32.6: the same rewrite every other path makes (`address.posting_page`); the trail names the page."""
    form, page = "https://apply.applysys.test/fabrikam/j/ABC123DEF4/apply", "https://apply.applysys.test/fabrikam/j/ABC123DEF4/"
    async with db.session_factory()() as session:
        employer = await _employer(session, ats="applysys", board={"slug": "fabrikam"},
                                   board_url="https://apply.applysys.test/fabrikam/")
        role = await _role(session, employer, apply_url=form)
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 1}
        assert (role.url, role.board_url, role.apply_url) == (page, BOARD, form)
        (said,) = await _trail(session, role)
        assert page in said.detail and form not in said.detail


@pytest.mark.parametrize("link", [OWN, "https://boards.feedsys.test/fabrikam/jobs/9?gh_src=abc"])
async def test_an_apply_link_on_the_employers_job_system_is_taken_with_no_listing_in_the_index(js, link):
    async with db.session_factory()() as session:
        employer = await _employer(session)  # no website known: a job system's own host needs none
        role = await _role(session, employer, apply_url=link)
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 1}
        assert (role.url, role.board_url) == (link, BOARD)


async def test_an_apply_link_on_the_employers_own_site_gives_an_employer_with_no_board_that_sites_careers_page(js):
    async with db.session_factory()() as session:
        employer = await _employer(session, website="https://www.fabrikam.test", ats=None, board={}, board_url=None,
                                   ats_found_by=None)
        await _role(session, employer, apply_url=APPLY_OWN)
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 1}
        assert employer.careers_url == "https://careers.fabrikam.test/", "the site's root, not the one posting"
        assert employer.sources["careers_url"]["source"] == "its own posting's site"


async def test_an_apply_link_of_a_role_hired_through_a_staffing_firm_gives_the_employer_no_careers_page(js):
    """The tag is on the role's company text ("Fabrikam (via Staffco)" under the employer "Fabrikam"), not in the
    employer's name: the apply link is the staffing firm's own site."""
    async with db.session_factory()() as session:
        employer = await _employer(session, website="https://www.fabrikam.test", ats=None, board={}, board_url=None,
                                   ats_found_by=None)
        role = await _role(session, employer, apply_url=APPLY_OWN, company="Fabrikam (via Staffco)")
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 1}
        assert role.url == APPLY_OWN, "the link is still the role's posting"
        assert employer.careers_url is None


async def test_an_apply_link_a_posting_in_the_index_carries_counts_as_the_roles_own(js):
    async with db.session_factory()() as session:
        employer = await _employer(session, website="https://www.fabrikam.test")
        role = await _role(session, employer)  # the role itself carries none
        posting = await _listing(session, employer, ats="browser", url=BOARD, ext_id="seen-on-exboard",
                                 apply_url=APPLY_OWN)
        posting.opportunity_id = role.id  # the posting from the job board that became the role
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 1}
        assert (role.url, role.board_url) == (APPLY_OWN, BOARD)


@pytest.mark.parametrize("link, why", [
    ("https://www.board.test/jobs/view/4242/apply", "a job board's own apply page"),
    ("https://www.listings.test/applystart?jk=5", "another job board's"),
    ("https://boards.feedsys.test/someoneelse/jobs/1", "another employer's board on the employer's own system"),
    ("https://apply.contoso.test/1", "off the employer's own domain"),
    ("http://localhost/apply", "not a public address"),
    ("https://user:secret@careers.fabrikam.test/apply", "a sign-in in the address"),
])
async def test_an_apply_link_that_is_a_job_board_another_employers_or_not_public_is_refused(js, link, why):
    async with db.session_factory()() as session:
        employer = await _employer(session, website="https://www.fabrikam.test")
        role = await _role(session, employer, apply_url=link)
        assert await ep.follow_roles(session, OWNER) == {"looked": 1, "followed": 0}, why
        assert (role.url, role.board_url, role.apply_url) == (BOARD, BOARD, link)
        assert await _trail(session, role) == []


async def test_a_refused_apply_link_leaves_the_role_to_the_listing_matcher(js):
    async with db.session_factory()() as session:
        employer = await _employer(session, website="https://www.fabrikam.test")
        await _listing(session, employer)
        role = await _role(session, employer, apply_url="https://www.listings.test/applystart?jk=5")
        assert (await ep.follow_roles(session, OWNER))["followed"] == 1
        assert role.url == OWN, "the matcher's listing, not the refused apply link"
        (said,) = await _trail(session, role)
        assert "Apply link" not in said.sentence


async def test_an_apply_link_off_a_domain_the_app_cannot_tie_to_the_employer_is_not_taken(js):
    async with db.session_factory()() as session:
        # Its careers page was kept from its job system's board: that is the system's host, not the employer's domain.
        employer = await _employer(session, careers_url=SYSTEM_BOARD)
        role = await _role(session, employer, apply_url=APPLY_OWN)
        assert (await ep.follow_roles(session, OWNER))["followed"] == 0
        assert role.url == BOARD


async def test_a_round_takes_the_apply_link_a_reported_role_carries(js):
    async with db.session_factory()() as session:
        await _employer(session)
        await session.commit()
    run_id = await run_round([
        reported("Fabrikam", "Platform Lead", url=BOARD, board="Exboard",
                 apply_url="https://boards.feedsys.test/fabrikam/jobs/901"),
        reported("Fabrikam", "Support Lead", url="https://www.listings.test/viewjob?jk=5", board="Exlistings",
                 apply_url="https://www.listings.test/applystart?jk=5"),
    ])
    stored = await stored_by_title(run_id)
    taken, refused = stored["Platform Lead"], stored["Support Lead"]
    assert (taken.url, taken.board_url) == ("https://boards.feedsys.test/fabrikam/jobs/901", BOARD)
    assert (refused.url, refused.board_url) == ("https://www.listings.test/viewjob?jk=5",) * 2


# --- the employer's two sites -------------------------------------------------------------------------------------


async def test_a_careers_page_already_kept_is_not_replaced_and_the_owners_own_edit_never_is(js):
    async with db.session_factory()() as session:
        kept = await _employer(session, careers_url="https://careers.fabrikam.test/")
        mine = await _employer(session, "Contoso")
        await employers.edit(session, OWNER, mine.id, {"careers_url": ""})  # the owner cleared it on purpose
        assert mine.careers_url is None and "careers_url" in mine.owner_fields
        for employer in (kept, mine):
            await _listing(session, employer, url=f"https://job-boards.feedsys.test/{employer.key}/jobs/1")
            await _role(session, employer, url=BOARD + employer.key, board_url=BOARD + employer.key)
        assert (await ep.follow_roles(session, OWNER))["followed"] == 2
        assert kept.careers_url == "https://careers.fabrikam.test/"
        assert mine.careers_url is None, "an owner's edit always wins, even an empty one"
        assert resolve.learn_careers_url_from_board(mine) is False


def test_a_job_system_found_gives_the_employer_its_careers_page_where_it_has_none():
    employer = Employer(owner=OWNER, key="fabrikam", name="Fabrikam", board={}, owner_fields=[], sources={})
    resolve._set(employer, "feedsys", {"slug": "fabrikam"}, "link")
    assert employer.board_url == SYSTEM_BOARD and employer.careers_url == SYSTEM_BOARD
    assert employer.sources["careers_url"]["source"] == "its job system's board"
    resolve._set(employer, "slugsys", {"slug": "fabrikam"}, "link")  # a later find of another system changes no page
    assert employer.careers_url == SYSTEM_BOARD
    partial = Employer(owner=OWNER, key="x", name="X", board={}, owner_fields=[], sources={})
    resolve._set(partial, "hostsys", {"host": "x.hostsys.test"}, "link")  # an address that is not whole names no page
    assert partial.board_url == "https://x.hostsys.test/" and partial.careers_url is None
    theirs = Employer(owner=OWNER, key="y", name="Y", board={}, owner_fields=["careers_url"], sources={})
    resolve._set(theirs, "feedsys", {"slug": "y"}, "link")
    assert theirs.careers_url is None


def test_a_board_that_is_gone_takes_the_careers_page_it_gave_with_it_but_not_one_the_owner_kept():
    taken = Employer(owner=OWNER, key="fabrikam", name="Fabrikam", board={}, owner_fields=[], sources={})
    resolve._set(taken, "feedsys", {"slug": "fabrikam"}, "link")
    resolve.forget(taken, "its board was gone")
    assert taken.ats is None and taken.careers_url is None
    own = Employer(owner=OWNER, key="x", name="X", ats="feedsys", board={"slug": "x"}, board_url=SYSTEM_BOARD,
                   careers_url=SYSTEM_BOARD, owner_fields=["careers_url"], sources={})
    resolve.forget(own, "its board was gone")
    assert own.ats is None and own.careers_url == SYSTEM_BOARD


async def test_the_night_gives_an_employer_whose_system_was_found_earlier_its_careers_page(js):
    async with db.session_factory()() as session:
        employer = await _employer(session)
        assert employer.careers_url is None
        await resolve.from_links(session, OWNER)
        assert employer.careers_url == SYSTEM_BOARD


async def test_the_scout_finding_a_board_fills_an_empty_careers_page_but_not_a_kept_one_nor_a_job_boards(js):
    async with db.session_factory()() as session:
        empty = await _employer(session, "Initech", ats=None, board={}, board_url=None)
        kept = await _employer(session, "Hooli", ats=None, board={}, board_url=None,
                               careers_url="https://careers.hooli.example.test/")
        job_board = await _employer(session, "Umbrella", ats=None, board={}, board_url=None)
        await tools.board_found_reply(session, OWNER, "Initech", "https://job-boards.feedsys.test/initech")
        await tools.board_found_reply(session, OWNER, "Hooli", "https://job-boards.feedsys.test/hooli")
        await tools.board_found_reply(session, OWNER, "Umbrella", "https://www.board.test/company/umbrella/jobs")
        assert empty.careers_url == "https://job-boards.feedsys.test/initech"
        assert empty.sources["careers_url"]["source"] == "found by the scout"
        assert kept.careers_url == "https://careers.hooli.example.test/" and kept.ats == "feedsys"
        assert job_board.careers_url is None, "a job board's page is not the employer's careers site"


# --- a round stores the job board link and follows the role -----------------------------------------------------------


async def test_a_round_keeps_the_job_board_link_and_follows_a_role_to_the_employers_posting(js):
    async with db.session_factory()() as session:
        employer = await _employer(session)
        await _listing(session, employer, "Delivery Lead", "Remote - United States", "remote")
        await session.commit()
    run_id = await run_round([
        reported("Fabrikam", "Delivery Lead", url=BOARD, board="Exboard", location="Remote, US"),
        reported("Fabrikam", "Operations Lead", url="https://www.listings.test/viewjob?jk=88", board="Exlistings"),
        reported("Fabrikam", "Site Lead", url="https://careers.fabrikam.test/jobs/3", board="Fabrikam careers"),
    ])
    stored = await stored_by_title(run_id)
    followed, waiting, own_site = stored["Delivery Lead"], stored["Operations Lead"], stored["Site Lead"]
    assert (followed.url, followed.board_url) == (OWN, BOARD), "followed in the round, the job board link kept"
    assert (waiting.url, waiting.board_url) == ("https://www.listings.test/viewjob?jk=88",) * 2, "no listing: as today"
    assert (own_site.url, own_site.board_url) == ("https://careers.fabrikam.test/jobs/3", None)
    async with db.session_factory()() as session:
        assert (await session.get(Employer, followed.employer_id)).careers_url == SYSTEM_BOARD


# --- preferred everywhere -----------------------------------------------------------------------------------------


async def test_the_employers_link_is_the_one_the_board_the_page_the_export_and_the_packet_brief_give(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        employer = await _employer(session)
        await _listing(session, employer)
        followed = await _role(session, employer)
        waiting = await _role(session, employer, title="Accountant")
        await ep.follow_roles(session, OWNER)
        await session.commit()
    page = (await client.get(f"/api/v1/roles/{followed.id}")).json()["role"]
    assert (page["url"], page["employer_url"], page["board_url"]) == (OWN, OWN, BOARD)
    only = (await client.get(f"/api/v1/roles/{waiting.id}")).json()["role"]
    # with no employer link the job board's is read, as today
    assert (only["url"], only["employer_url"], only["board_url"]) == (BOARD, None, BOARD)
    async with db.session_factory()() as session:
        assert (await role_row.row(session, OWNER, followed.id))["url"] == OWN
        sheet = openpyxl.load_workbook(io.BytesIO(await export_workbook(session, OWNER)))["Tracker"]
        header = [c.value for c in sheet[1]]
        rows = {row[2]: dict(zip(header, row, strict=True)) for row in sheet.iter_rows(min_row=2, values_only=True)}
        assert rows["Program Manager"]["url"] == OWN and rows["Accountant"]["url"] == BOARD
        assert rows["Program Manager"]["board"] == "Exboard", "where it was found stays on the row"


async def test_the_analyst_is_handed_the_roles_links_and_a_role_that_is_not_sourced_is_not_handed_to_it(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        followed = await session.get(Opportunity, ids["Fabrikam"])
        followed.url, followed.board_url = OWN, BOARD
        waiting = await session.get(Opportunity, ids["Initech"])
        waiting.url, waiting.board_url, waiting.description = BOARD, BOARD, None
        waiting.source_url = waiting.source_kind = waiting.sourced_at = None  # a job board's link only: not sourced
        await analysis.start(session, OWNER, waiting)  # not researched: the process leaves it in Found
        await session.commit()
    async with db.session_factory()() as session:
        have = await analysis.brief_part(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]), "task")
        mine = have["role"]
        assert (mine["url"], mine["employer_url"], mine["board_url"]) == (OWN, OWN, BOARD)
        assert "task_now" not in have, "a role with its text is researched"
        waiting = await session.get(Opportunity, ids["Initech"])
        assert (waiting.analysis_state, waiting.sourced_at) == (None, None)
        assert (await analysis.analysis_reply(session, OWNER, session_id="s"))["task"] is None, "none is waiting"


# --- the analyst as the backup, and what it reports is checked -------------------------------------------------


async def _waiting_role(**over):
    """The made-up roles of test_analysis, with Fabrikam's found on a job board and sent to the analyst."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.url, role.board_url, role.board = BOARD, BOARD, "Exboard"
        for name, value in over.items():
            setattr(role, name, value)
        await analysis.start(session, OWNER, role)
        await session.commit()
    return ids["Fabrikam"]


WEBSITE = {"website": "https://www.fabrikam.test"}


async def test_the_analyst_never_sources_a_role_whatever_link_it_sends(js):
    role_id = await _waiting_role()
    body = findings(role_id, employer_posting_url="https://careers.fabrikam.test/jobs/55",
                    employer={"careers_url": "https://careers.fabrikam.test/", **WEBSITE,
                              "sources": {"careers_url": "https://www.fabrikam.test/about"}})
    reply = await submit_findings(body)
    assert reply["taken"] is True
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        employer = await session.get(Employer, role.employer_id)
        assert (role.url, role.board_url) == (BOARD, BOARD), "the link it sent is ignored"
        assert role.source_url != "https://careers.fabrikam.test/jobs/55" and await _trail(session, role) == []
        assert (employer.website, employer.careers_url) == (WEBSITE["website"], "https://careers.fabrikam.test/")
        assert employer.sources["careers_url"]["source"] == "https://www.fabrikam.test/about"


@pytest.mark.parametrize("link", [
    OWN, "https://boards.feedsys.test/fabrikam/jobs/9", "https://jobs.slugsys.test/fabrikam/0b3a1c64-1f1e-4c2a-9a0e-2a7d6f1c9e11",
    "https://fabrikam.wd1.tenantsys.test/en-US/External/job/Austin-TX/Program-Manager_R1234",
    "https://jobs.pathidsys.test/Fabrikam/743999876543210-program-manager"])
def test_one_posting_on_a_job_system_needs_no_website(link):
    assert ep.check_employer_link(link, None) is None


@pytest.mark.parametrize("link", [
    SYSTEM_BOARD, "https://boards.feedsys.test/fabrikam", "https://jobs.slugsys.test/fabrikam",
    "https://fabrikam.wd1.tenantsys.test/en-US/External", "https://jobs.pathidsys.test/Fabrikam",
    "https://careers.fabrikam.test/", "https://careers.fabrikam.test/jobs/search?q=program+manager"])
def test_a_job_systems_board_and_a_careers_front_page_are_refused_for_one_posting(link):
    assert "one posting" in ep.check_employer_link(link, None, website="https://www.fabrikam.test")


@pytest.mark.parametrize("link, said", [
    ("https://www.listings.test/viewjob?jk=5", "a job board"),
    ("https://www.board.test/jobs/view/4242", "a job board"),
    ("https://jobs.contoso.test/jobs/1", "not the employer's own domain"),
    ("https://boards.feedsys.test/someoneelse/jobs/1", "another employer's Feedsys board"),
    ("http://localhost/jobs/1", "not a public web address"),
    ("https://user:secret@careers.fabrikam.test/1", "not a public web address"),
])
async def test_a_job_board_or_a_link_that_is_not_the_employers_is_refused(js, link, said):
    async with db.session_factory()() as session:
        employer = await _employer(session, "Fabrikam")  # the record: its system and its board
        employer.website = WEBSITE["website"]
        await session.commit()
        assert said in ep.check_employer_link(link, employer)


def test_without_the_employers_website_a_link_off_a_job_system_cannot_be_checked():
    assert "send it in employer.website too" in ep.check_employer_link("https://careers.fabrikam.test/jobs/55", None)


async def test_the_careers_page_the_analyst_reports_fills_an_empty_one_only_and_is_never_a_job_boards(js):
    role_id = await _waiting_role()
    reply = await submit_findings(findings(role_id, employer={"careers_url": "https://www.listings.test/cmp/fabrikam/jobs"}))
    assert reply["taken"] is False and "employer.careers_url is on listings.test, a job board" in reply["reason"]
    async with db.session_factory()() as session:
        employer = await _employer(session, "Fabrikam", careers_url="https://careers.fabrikam.test/",
                                   owner_fields=["website"])
        role = await session.get(Opportunity, role_id)
        role.employer_id = employer.id
        await session.commit()
    reply = await submit_findings(findings(role_id, employer={"careers_url": "https://jobs.fabrikam.test/all",
                                                     "website": "https://other.contoso.test"}))
    assert reply["taken"] is True
    async with db.session_factory()() as session:
        employer = await session.get(Employer, (await session.get(Opportunity, role_id)).employer_id)
        assert employer.careers_url == "https://careers.fabrikam.test/", "the one kept stays"
        assert employer.website is None, "the owner's field (even empty) stands"


# --- where a role was found ------------------------------------------------------------------------------------


async def test_a_role_followed_to_its_employer_is_still_found_where_it_was_found(js):
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url="https://www.board.test/jobs", kind="job_board", on=True,
                    created_at=datetime.now(UTC))
        session.add(site)
        employer = await _employer(session)
        await _listing(session, employer)
        role = await _role(session, employer)
        await ep.follow_roles(session, OWNER)
        assert role.url == OWN
        found = await where_view.roles_by_source(session, OWNER, sites=[site], systems=["feedsys"])
        assert found[str(site.id)] == [role.id], "found at Exboard, whatever link it is read from now"
        assert found["feedsys"] == [], "not found at its employer's job system"
