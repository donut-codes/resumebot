"""The folds `same_role` asks of every role and posting are kept between calls (perf/same-role-caches): the company's
key, the title's words, what an address names and a board's host. A cache must change no answer, so the first group
pins what each fold says (the values are what the folds said before there was a cache), the second that a warm call
says the same as a cold one and the cache is what answers it, and the third that every cache is bounded. Every
employer, title and address here is made up."""

import itertools

import pytest

from app.jobsearch.postings import same_role as sr
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)

pytestmark = pytest.mark.usefixtures("made_up_boards")

CACHED = ("_company_key", "_title_words", "_link_marks", "_board_host")


@pytest.fixture
def cold():
    """Every fold unremembered at the start of the test and left so at its end: the caches are the module's, not the
    test's. Only the tests of the caches ask for it; the ones that pin what the folds say run the same on any module."""
    for name in CACHED:
        getattr(sr, name).cache_clear()
    yield
    for name in CACHED:
        getattr(sr, name).cache_clear()


# --- what the folds say ---------------------------------------------------------------------------------------------

WRITINGS = [
    pytest.param("Northwind", "Director, Programs",
                 ("s:northwind|director programs", "l:northwind|director programs"), id="plain"),
    pytest.param("Northwind, Inc.", "Director - Programs",
                 ("s:northwind|director programs", "l:northwind|director programs"), id="suffix, dash"),
    pytest.param("The Northwind Company", "Sr. Program Manager (Remote)",
                 ("s:northwind|senior program manager remote", "l:northwind|senior program manager"),
                 id="a where-tag in brackets"),
    pytest.param("Northwind (Retail)", "Analyst II", ("s:northwind|analyst 2", "l:northwind|analyst 2"), id="numeral"),
    pytest.param("Fabrikam & Co.", "Strategy & Operations Lead",
                 ("s:fabrikam|strategy operations lead", "l:fabrikam|strategy operations lead"), id="ampersands"),
    pytest.param("Northwind", "Program Manager - US",
                 ("s:northwind|program manager us", "l:northwind|program manager"), id="a where-tag after a dash"),
    pytest.param("Northwind", "Analyst Full-time", ("s:northwind|analyst full time", "l:northwind|analyst"),
                 id="a closing Full-time"),
    pytest.param("Northwind", "Analyst (Contract)", ("s:northwind|analyst contract", "l:northwind|analyst contract"),
                 id="a bracket that is not a place"),
    pytest.param("Northwind", "C++ Developer",
                 ("s:northwind|c plus plus developer", "l:northwind|c plus plus developer"), id="C++"),
    pytest.param("Northwind", "C# Developer",
                 ("s:northwind|c sharp developer", "l:northwind|c sharp developer"), id="C#"),
    pytest.param("Northwind", "営業  MR", ("s:northwind|営業 mr", "l:northwind|営業 mr"), id="a title kept whole"),
    pytest.param("北風 Inc", "Director, Programs",
                 ("s:北風 inc|director programs", "l:北風 inc|director programs"), id="a company kept whole"),
    pytest.param("Northwind", "&", ("s:northwind|&", "l:northwind|&"), id="a title that folds to nothing"),
    pytest.param("Northwind USA", "Lead I", ("s:northwind|lead 1", "l:northwind|lead 1"), id="country tail, level I"),
    pytest.param("", "Director, Programs", (), id="no company"),
    pytest.param("Northwind", "", (), id="no title"),
    pytest.param(None, None, (), id="neither"),
]


@pytest.mark.parametrize("company, title, keys", WRITINGS)
def test_a_company_and_title_fold_to_the_keys_they_always_did(company, title, keys):
    assert sr.writings(company, title) == keys


LINKS = [
    pytest.param(["https://www.listings.example.test/comm/jobs/view/director-at-northwind-3912345678?trackingId=y"],
                 {"listings.example.test#3912345678"}, {"listings.example.test#3912345678"},
                 {"example.test#3912345678"}, id="a slug that ends in the id"),
    pytest.param(["https://listings.example.test/jobs/view/3912345678/?refId=x#top",
                  "https://apply.example.test/apply/77001"],
                 {"listings.example.test#3912345678", "apply.example.test#77001"},
                 {"listings.example.test#3912345678", "apply.example.test#77001"},
                 {"example.test#3912345678", "example.test#77001"}, id="two links, two hosts"),
    pytest.param(["https://careers.example.test/openings?gh_jid=5551234&gh_src=abc",
                  "careers.example.test/jobs/7910947-manager"],
                 {"careers.example.test#5551234", "careers.example.test#7910947"},
                 {"careers.example.test#5551234", "careers.example.test#7910947"},
                 {"example.test#5551234", "example.test#7910947"}, id="a parameter, a slug with no scheme"),
    pytest.param(["https://careers.example.test/careers", None, "", "not a link at all", 12345], set(), set(), set(),
                 id="nothing that names a posting"),
    pytest.param(["https://uk.boardone.test/jobs/view/3912345678", "https://www.boardone.test/jobs/view/3912345678/"],
                 {"boardone.test#3912345678", "uk.boardone.test#3912345678"}, {"boardone.test#3912345678"},
                 {"boardone.test#3912345678"}, id="one job on a board, two countries"),
    pytest.param(["https://jobs.example.test/northwind/6B1F0C2E-1234-4ABC-9DEF-0123456789AB"],
                 {"jobs.example.test#6b1f0c2e-1234-4abc-9def-0123456789ab"},
                 {"jobs.example.test#6b1f0c2e-1234-4abc-9def-0123456789ab"},
                 {"example.test#6b1f0c2e-1234-4abc-9def-0123456789ab"}, id="a UUID"),
]


@pytest.mark.parametrize("links, named, on_a_board, on_an_owners_board", LINKS)
def test_links_name_the_marks_they_always_did(links, named, on_a_board, on_an_owners_board):
    assert sr.marks(links) == named
    assert sr.board_marks(links) == on_a_board
    assert sr.board_marks(links, frozenset({"example.test"})) == on_an_owners_board


def test_the_keys_a_role_is_asked_by_are_the_ones_it_always_was_in_the_order_it_always_was():
    keys = sr.Known._keys("Northwind, Inc.", "Director, Programs",
                          ["https://listings.example.test/jobs/view/3912345678"], "northwind|older")
    assert keys == ["x:northwind, inc.|director, programs", "x:northwind|older", "s:northwind|director programs",
                    "l:northwind|director programs", "m:northwind|listings.example.test#3912345678"]
    assert sr.Known._unnamed_keys(["https://uk.boardone.test/jobs/view/3912345678",
                                   "https://ca.boardtwo.test/viewjob?jk=a1b2c3d4e5f60718"]) == [
        "u:boardone.test#3912345678", "u:boardtwo.test#a1b2c3d4e5f60718"]


def test_a_boards_host_is_folded_under_the_boards_it_is_asked_with_not_the_ones_it_was_asked_with_before():
    """The owner's job sites are part of the question: the same host, asked with and without them, is two questions."""
    host, owners = "jobs.example.test", frozenset({"example.test"})
    asked = [(host, owners), (host, ()), (host, owners), ("uk.boardone.test", ()), (host, ())]
    assert [sr.board_host(*one) for one in asked] == ["example.test", host, "example.test", "boardone.test", host]


def test_a_boards_host_is_folded_under_the_first_board_that_covers_it_as_it_always_was():
    """Two job sites of the owner's that both cover a host: the one earlier in the list it is given is the answer."""
    host = "apply.jobs.example.test"
    assert sr.board_host(host, ["jobs.example.test", "example.test"]) == "jobs.example.test"
    assert sr.board_host(host, ["example.test", "jobs.example.test"]) == "example.test"


# --- a warm call says what a cold one did, and the cache is what says it -----------------------------------------


def test_the_same_questions_asked_again_and_in_another_order_get_the_same_answers(cold):
    companies = ["Northwind", "Northwind, Inc.", "The Northwind Company", "Fabrikam & Co.", "北風 Inc", "", None]
    titles = ["Director, Programs", "Sr. Program Manager (Remote)", "C++ Developer", "営業  MR", "&", "", None]
    links = [["https://listings.example.test/jobs/view/3912345678"], ["careers.example.test/jobs/7910947-manager"],
             ["https://careers.example.test/careers", None, 12345], []]
    asked = [(c, t) for c, t in itertools.product(companies, titles)]
    first = [sr.writings(c, t) for c, t in asked]
    first_marks = [(sr.marks(one), sr.board_marks(one)) for one in links]
    assert [sr.writings(c, t) for c, t in reversed(asked)][::-1] == first, "warm, backwards"
    assert [(sr.marks(one), sr.board_marks(one)) for one in reversed(links)][::-1] == first_marks
    for name in CACHED[:3]:
        assert getattr(sr, name).cache_info().hits > 0, f"{name} answered nothing from what it remembered"


def test_a_company_read_twice_is_folded_once(cold):
    assert sr._company_key("Northwind, Inc.") == "northwind"
    seen = sr._company_key.cache_info()
    assert sr._company_key("Northwind, Inc.") == "northwind"
    after = sr._company_key.cache_info()
    assert (after.misses, after.hits) == (seen.misses, seen.hits + 1)


def test_a_title_read_twice_is_folded_once_for_both_of_its_keys(cold):
    assert sr.writings("Northwind", "Director, Programs") == (
        "s:northwind|director programs", "l:northwind|director programs")
    seen = sr._title_words.cache_info()
    assert sr.writings("Fabrikam", "Director, Programs") == (
        "s:fabrikam|director programs", "l:fabrikam|director programs")
    after = sr._title_words.cache_info()
    assert (after.misses, after.hits) == (seen.misses, seen.hits + 1), "another company, the same title"


def test_an_address_read_twice_is_read_once(cold):
    link = "https://listings.example.test/jobs/view/3912345678"
    assert sr.marks([link]) == {"listings.example.test#3912345678"}
    seen = sr._link_marks.cache_info()
    assert sr.board_marks([link, link]) == {"listings.example.test#3912345678"}
    after = sr._link_marks.cache_info()
    assert after.misses == seen.misses and after.hits > seen.hits


def test_a_board_host_asked_twice_is_folded_once(cold):
    assert sr.board_host("uk.boardone.test") == "boardone.test"
    seen = sr._board_host.cache_info()
    assert sr.board_host("uk.boardone.test") == "boardone.test"
    after = sr._board_host.cache_info()
    assert (after.misses, after.hits) == (seen.misses, seen.hits + 1)


def test_what_a_cache_hands_back_cannot_be_changed_by_the_caller(cold):
    """The same value is handed to every caller that asks: it is a string or a frozen set, never a set to add to."""
    link = "https://listings.example.test/jobs/view/3912345678"
    assert isinstance(sr._link_marks(link), frozenset) and isinstance(sr.marks([link]), frozenset)
    assert sr._link_marks(link) == {"listings.example.test#3912345678"}
    assert sr.writings("Northwind", "Lead") == ("s:northwind|lead", "l:northwind|lead")
    assert isinstance(sr._title_words("Lead"), tuple) and isinstance(sr._company_key("Northwind"), str)


# --- every cache is bounded ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", CACHED)
def test_every_fold_that_is_remembered_is_remembered_in_a_bounded_cache(name):
    """No cache holds more than a couple of hundred thousand answers: an unbounded one would grow with every title the
    scout ever read."""
    size = getattr(sr, name).cache_info().maxsize
    assert size is not None and 1_000 <= size <= 200_000


def test_the_title_and_address_caches_hold_a_pass_over_tens_of_thousands_of_roles_twice_over():
    """A pass over roles in order misses every time when the cache is smaller than the pass: the one least recently used
    is always the next one asked. A role has one title and up to three addresses."""
    assert sr._title_words.cache_info().maxsize >= 65_536
    assert sr._link_marks.cache_info().maxsize >= 65_536
    assert sr._company_key.cache_info().maxsize >= 8_192
