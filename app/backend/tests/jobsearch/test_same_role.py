"""Whether a role is one the owner already has (same_role.py): the same company and title written another way, and the
same posting by its link, are one role; two jobs at one employer, and two postings that merely share a careers page, are
not. Every employer, title and address here is made up."""

import pytest

from app.jobsearch.postings import same_role as sr
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)

pytestmark = pytest.mark.usefixtures("made_up_boards")


def _is_the_same(a: tuple, b: tuple) -> bool:
    """Whether (company, title) `b` is the role `a` is, asked both ways round: a role the owner has, then the report."""
    forward, backward = sr.Known(), sr.Known()
    forward.add("a", *a)
    backward.add("b", *b)
    one, other = forward.find(*b), backward.find(*a)
    assert (one is not None) == (other is not None), "the answer does not depend on which was stored first"
    return one is not None


# --- the same company and title -----------------------------------------------------------------------------------


@pytest.mark.parametrize("a, b", [
    pytest.param(("Northwind", "Director, Programs"), ("Northwind", "Director — Programs"), id="comma, em dash"),
    pytest.param(("Northwind", "Director - Programs"), ("Northwind", "Director – Programs"), id="hyphen, en dash"),
    pytest.param(("Northwind", "Program Manager / Owner"), ("Northwind", "Program Manager, Owner"), id="slash, comma"),
    pytest.param(("Northwind", "Strategy & Operations Lead"), ("Northwind", "Strategy and Operations Lead"),
                 id="ampersand, and"),
    pytest.param(("NORTHWIND", "DIRECTOR OF PROGRAMS"), ("Northwind", "Director of Programs"), id="case"),
    pytest.param(("Northwind", "Director  of\tPrograms "), ("Northwind", "Director of Programs"), id="spacing"),
    pytest.param(("Northwind", "Director of Programs"), ("Northwind", "Director, Programs"), id="of, comma"),
    pytest.param(("Northwind", "Sr. Program Manager"), ("Northwind", "Senior Program Manager"), id="Sr., Senior"),
    pytest.param(("Northwind", "Analyst II"), ("Northwind", "Analyst 2"), id="numeral"),
    pytest.param(("Northwind", "Program Manager (Remote)"), ("Northwind", "Program Manager"), id="where in brackets"),
    pytest.param(("Northwind", "Program Manager - US"), ("Northwind", "Program Manager"), id="where after a dash"),
    pytest.param(("Northwind, Inc.", "Lead"), ("Northwind", "Lead"), id="company suffix"),
    pytest.param(("The Northwind Company", "Lead"), ("Northwind", "Lead"), id="company article and noun"),
    pytest.param(("Northwind (Retail)", "Lead"), ("Northwind", "Lead"), id="company note in brackets"),
    pytest.param(("Northwind Solutions LLC", "Lead"), ("Northwind Solutions", "Lead"), id="company LLC"),
    pytest.param(("Fabrikam (Retail Labs)", "Lead"), ("Fabrikam and Company", "Lead"), id="subsidiary, name tail"),
    pytest.param(("Fabrikam & Co.", "Lead"), ("Fabrikam", "Lead"), id="company ampersand tail"),
    pytest.param(("Northwind", "Manager, AI & People Transformation"),
                 ("Northwind", "Manager, AI and People Transformation"), id="title ampersand"),
    pytest.param(("Northwind", "営業  MR"), ("Northwind", "営業 mr"), id="a title kept whole"),
])
def test_one_role_written_another_way_is_still_the_same_role(a, b):
    assert _is_the_same(a, b)


@pytest.mark.parametrize("a, b", [
    pytest.param(("Northwind", "Director, Programs"), ("Northwind", "Senior Director, Programs"), id="senior"),
    pytest.param(("Northwind", "Product Owner"), ("Northwind", "Senior AI Product Owner"), id="a longer title"),
    pytest.param(("Northwind", "Program Manager"), ("Northwind", "Project Manager"), id="another word"),
    pytest.param(("Northwind", "Analyst I"), ("Northwind", "Analyst II"), id="another level"),
    pytest.param(("Northwind", "Director, Programs"), ("Northwind", "Director, Programs Operations"), id="more title"),
    pytest.param(("Northwind", "Lead"), ("Fabrikam", "Lead"), id="another employer"),
    pytest.param(("Northwind Health", "Lead"), ("Northwind", "Lead"), id="another employer with the same first word"),
    pytest.param(("Northwind", "Analyst (Contract)"), ("Northwind", "Analyst"), id="a bracket that is not a place"),
    pytest.param(("Northwind", "Analyst - Finance"), ("Northwind", "Analyst - Legal"), id="a tag that is not a place"),
    pytest.param(("Northwind", "C++ Developer"), ("Northwind", "C# Developer"), id="C++, C#"),
    pytest.param(("Northwind", "C Developer"), ("Northwind", "C# Developer"), id="C, C#"),
    pytest.param(("Northwind", "営業 MR"), ("Northwind", "研究 MR"), id="titles the fold cannot read"),
    pytest.param(("Northwind", "&"), ("Northwind", "-"), id="titles that fold to nothing"),
])
def test_a_different_job_is_a_different_role(a, b):
    assert not _is_the_same(a, b)


def test_a_role_is_found_by_the_key_it_is_stored_under_even_when_that_is_not_what_its_words_make():
    """A tracker's own key column can be stale (the title was edited after the key was made)."""
    known = sr.Known()
    known.add("role", "Northwind", "Director, Programs", key="northwind|an older title")
    assert known.find("Northwind", "Something Else", key="northwind|an older title") == "role"
    assert known.find("Northwind", "Director — Programs") == "role", "and by the words it has now"
    assert known.find("Northwind", "Something Else") is None


def test_the_role_added_first_answers_when_two_have_a_key():
    known = sr.Known()
    known.add("oldest", "Northwind", "Director, Programs")
    known.add("newer", "Northwind", "Director - Programs")
    assert known.find("Northwind", "Director — Programs") == "oldest"


def test_a_role_with_no_company_or_title_is_found_only_by_what_it_has():
    known = sr.Known()
    known.add("blank", "", "")
    assert known.find("", "") is None and known.find("Northwind", "Lead") is None
    known.add("no title", "Northwind", "")
    assert known.find("Northwind", "Lead") is None and known.find("Fabrikam", "") is None


# --- the same posting ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("links, marks", [
    pytest.param(["https://listings.example.test/jobs/view/3912345678/?refId=x#top"],
                 {"listings.example.test#3912345678"}, id="a last segment that is a number"),
    pytest.param(["https://www.listings.example.test/comm/jobs/view/director-at-northwind-3912345678?trackingId=y"],
                 {"listings.example.test#3912345678"}, id="a slug that ends in the number"),
    pytest.param(["careers.example.test/jobs/7910947-manager-project-management"],
                 {"careers.example.test#7910947"}, id="a slug that starts with the number, no scheme"),
    pytest.param(["https://jobs.example.test/northwind/6B1F0C2E-1234-4ABC-9DEF-0123456789AB"],
                 {"jobs.example.test#6b1f0c2e-1234-4abc-9def-0123456789ab"}, id="a UUID, and not its digits"),
    pytest.param(["https://northwind.wd1.example.test/en-US/Careers/job/Austin/Director-Programs_R-102345"],
                 {"northwind.wd1.example.test#102345"}, id="a requisition after an underscore"),
    pytest.param(["https://northwind.wd1.example.test/en-US/Careers/job/Austin/Director-Programs_R-102345-1"],
                 {"northwind.wd1.example.test#102345"}, id="the same requisition, reposted"),
    pytest.param(["https://northwind.wd1.example.test/Careers/job/Austin/Director-Programs_JR12345"],
                 {"northwind.wd1.example.test#12345"}, id="a requisition of five digits"),
    pytest.param(["https://careers.example.test/company/jobs/JR352235/director-programs/"],
                 {"careers.example.test#352235"}, id="a segment that is a requisition"),
    pytest.param(["https://careers.example.test/global/en/job/202607-123456"],
                 {"careers.example.test#123456"}, id="the number, not the month before it"),
    pytest.param(["https://careers.example.test/job/austin/director-programs/34088/48213"],
                 {"careers.example.test#48213"}, id="the posting's number, not the site's before it"),
    pytest.param(["https://careers.example.test/openings?gh_jid=5551234&gh_src=abc"],
                 {"careers.example.test#5551234"}, id="a parameter that names the job"),
    pytest.param(["https://www.listings.example.test/viewjob?jk=a1b2c3d4e5f60718&from=serp"],
                 {"listings.example.test#a1b2c3d4e5f60718"}, id="a job key that is not a number"),
    pytest.param(["https://recruiting.example.test/TENANT/JobBoard/362B6B1D-F1C3-46F5-9554-4AA90E2BDA64/"
                  "OpportunityDetail?opportunityId=D04B9739-815F-45AC-B632-A38C417BC278"],
                 {"recruiting.example.test#d04b9739-815f-45ac-b632-a38c417bc278"}, id="the job's, not the board's"),
    pytest.param(["https://listings.example.test/jobs/view/3912345678", "https://apply.example.test/apply/77001"],
                 {"listings.example.test#3912345678", "apply.example.test#77001"}, id="each link its own host"),
    pytest.param(["https://careers.example.test/careers"], set(), id="a careers page"),
    pytest.param(["https://listings.example.test/jobs/4821"], set(), id="a number under five digits"),
    pytest.param(["https://careers.example.test/jobs/director-austin-tx-78701"], set(), id="a zip code in a slug"),
    pytest.param(["https://blog.example.test/posts/20261013"], set(), id="a day written out"),
    pytest.param(["https://tenant.example.test/careersection/10000/jobapply.ftl"], set(), id="a section's number"),
    pytest.param(["https://careers.example.test/openings?department=123456&page=2"], set(), id="a filter, a page"),
    pytest.param(["https://careers.example.test/search?job=engineering"], set(), id="a job parameter, a category"),
    pytest.param([None, "", "not a link at all", 12345], set(), id="nothing to read"),
])
def test_what_a_link_names_is_the_id_a_job_system_puts_at_the_end_of_it_on_its_host(links, marks):
    assert sr.marks(links) == marks


LINK = "https://www.listings.example.test/jobs/view/3912345678/"


@pytest.mark.parametrize("company", ["Northwind", "Northwind Holdings", "Northwind, Inc.", "NORTHWIND (Retail)"])
def test_two_roles_with_the_same_posting_link_and_employer_are_one_whatever_their_titles_say(company):
    known = sr.Known()
    known.add("role", "Northwind", "Director, Programs", [LINK])
    other = "https://listings.example.test/comm/jobs/view/director-3912345678?refId=x&trackingId=y"
    assert known.find(company, "Head of Program Delivery", [other]) == "role"


@pytest.mark.parametrize("company", ["Fabrikam", "Northwind Health", "The Contoso Company"])
def test_a_link_across_two_employers_is_not_a_match(company):
    """8 live tracker rows carry another employer's posting link, so a new posting of that link would have been taken
    for the older row's job and never read."""
    known = sr.Known()
    known.add("role", "Northwind", "Director, Programs", [LINK])
    assert known.find(company, "Director, Programs - Delivery", [LINK]) is None
    assert known.find(company, "Head of Program Delivery", [LINK]) is None


def test_a_company_with_no_name_has_no_link_to_match_by():
    known = sr.Known()
    known.add("role", "", "Director, Programs", [LINK])
    assert known.find("", "Head of Program Delivery", [LINK]) is None
    assert known.find("Northwind", "Head of Program Delivery", [LINK]) is None


@pytest.mark.parametrize("links", [
    pytest.param(["https://listings.example.test/jobs/view/3912345679"], id="another id on the host"),
    pytest.param(["https://elsewhere.example.test/jobs/view/3912345678"], id="the id on another host"),
    pytest.param(["https://listings.example.test/jobs"], id="a page with no id"),
    pytest.param([], id="no link"),
])
def test_a_link_that_does_not_name_the_same_posting_does_not_join_two_roles(links):
    known = sr.Known()
    known.add("role", "Northwind", "Director, Programs", [LINK])
    assert known.find("Northwind", "Head of Program Delivery", links) is None


def test_two_roles_that_only_share_a_careers_page_are_two():
    page = ["https://careers.example.test/careers"]
    known = sr.Known()
    known.add("role", "Northwind", "Director, Programs", page)
    assert known.find("Northwind", "Head of Program Delivery", page) is None


# --- a job board's host, and a job the owner added that has no name yet (docs/PRD.md 24.10) ---------------------------

#: What the owner pasted, which research has not named: no employer, a title that is the link.
ADDED = "https://listings.example.test/jobs/view/3912345678"


@pytest.mark.parametrize("host, boards, folded", [
    pytest.param("boardone.test", (), "boardone.test", id="the board itself"),
    pytest.param("uk.boardone.test", (), "boardone.test", id="a country's address of a board the app knows"),
    pytest.param("ca.boardtwo.test", (), "boardtwo.test", id="another board"),
    pytest.param("listings.example.test", (), "listings.example.test", id="a host that is no board"),
    pytest.param("nolinkedin.com", (), "nolinkedin.com", id="a host that only ends the same"),
    pytest.param("jobs.example.test", ("example.test",), "example.test", id="the owner's own job site"),
])
def test_a_job_boards_host_is_the_boards_name_whichever_countrys_address_it_is(host, boards, folded):
    assert sr.board_host(host, boards) == folded


def test_one_job_on_a_board_has_one_mark_whichever_countrys_address_names_it():
    links = ["https://www.boardone.test/jobs/view/3912345678/?trackingId=x",
             "https://ca.boardone.test/jobs/view/3912345678",
             "https://uk.boardone.test/comm/jobs/view/director-3912345678"]
    assert sr.board_marks(links) == {"boardone.test#3912345678"}
    assert sr.marks(links) == {"boardone.test#3912345678", "ca.boardone.test#3912345678", "uk.boardone.test#3912345678"}
    # another host's job with the same number is another job
    assert sr.board_marks([ADDED]) == {"listings.example.test#3912345678"}


def _added_role() -> sr.Known[str]:
    known: sr.Known[str] = sr.Known()
    known.add("added", "Added link", "listings.example.test/jobs/view/3912345678", [ADDED], unnamed=True)
    return known


@pytest.mark.parametrize("company, title, links", [
    pytest.param("Northwind", "Director, Programs", ["https://www.listings.example.test/jobs/view/3912345678?refId=y"],
                 id="the employer's listing of it, under the employer's name"),
    pytest.param("Fabrikam", "Head of Program Delivery", [ADDED, None], id="any employer: the id is all it has"),
    pytest.param("", "", ["https://listings.example.test/comm/jobs/view/director-3912345678"], id="no name at all"),
    pytest.param("Northwind", "Director, Programs", [None, "https://listings.example.test/jobs/view/3912345678"],
                 id="the id in the apply link"),
])
def test_an_added_job_research_has_not_named_is_known_by_its_id_whatever_company_asks(company, title, links):
    assert _added_role().find(company, title, links) == "added"


@pytest.mark.parametrize("links", [
    pytest.param(["https://listings.example.test/jobs/view/3912345679"], id="another id on the host"),
    pytest.param(["https://elsewhere.example.test/jobs/view/3912345678"], id="the id on another host"),
    pytest.param(["https://listings.example.test/jobs"], id="a page with no id"),
    pytest.param([], id="no link"),
])
def test_an_unnamed_job_is_not_matched_by_a_link_that_is_not_its_id(links):
    assert _added_role().find("Northwind", "Director, Programs", links) is None


def test_a_job_that_has_a_name_is_known_by_its_company_and_its_id_together_as_any_role_is():
    """Named, the role is an ordinary one: a link across two employers is no match again (`unnamed` is false)."""
    known: sr.Known[str] = sr.Known()
    known.add("named", "Northwind", "Director, Programs", [ADDED])
    assert known.find("Fabrikam", "Head of Program Delivery", [ADDED]) is None
    assert known.find("Northwind Holdings", "Head of Program Delivery", [ADDED]) == "named"


def test_the_ids_of_an_unnamed_job_on_a_board_are_one_whichever_countrys_address_the_board_is_met_at():
    known: sr.Known[str] = sr.Known()
    known.add("added", "Added link", "boardone.test/jobs/view/3912345678", ["https://boardone.test/jobs/view/3912345678"],
              unnamed=True)
    assert known.find("Northwind", "Director, Programs", ["https://uk.boardone.test/jobs/view/3912345678"]) == "added"
    assert known.find("Northwind", "Director, Programs", ["https://uk.boardone.test/jobs/view/3912345679"]) is None


def test_a_role_the_name_finds_wins_over_an_unnamed_job_with_the_same_id():
    known = _added_role()
    known.add("named", "Northwind", "Director, Programs", ["https://listings.example.test/other/55555"])
    assert known.find("Northwind", "Director, Programs", [ADDED]) == "named", "the company and title are asked first"
