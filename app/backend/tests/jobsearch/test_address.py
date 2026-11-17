"""What a posting's address is (`app/jobsearch/address.py`): one string in, a bool or a string out, no database, no
network. The cases here are the outcomes the three functions give where the old homes were spread over `follow`,
`sourced` and `go_checks`. The big tables (every job system's posting and listing pair, every apply form) stay in
`test_job_system_addresses.py` and `test_apply_rewrite.py`. Every host, tenant and id is made up."""

import pytest

from app.jobsearch import ats
from app.jobsearch.address import is_board_link, is_single_posting, posting_page
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures("example_world")

OWN = "https://www.searchsite.test/about/careers/applications/jobs/results/123456789-program-manager"
SITE = "https://jobs.sitebox.example.test/job/1"

#: (address, owner's hosts) -> (is a board's, is one posting)
ROWS = [
    # a job board the app knows by name: a board's, and one posting only when it has a board's posting shape
    ("https://www.board.test/jobs/view/4242", (), True, True),
    ("https://www.board.test/jobs/search?keywords=analyst", (), True, False),
    ("https://uk.listings.test/viewjob?jk=abc", (), True, True),
    ("https://uk.listings.test/x", (), True, False),
    ("https://notboard.test/x", (), False, False),
    # an employer's own careers site on a board's host: the employer's, one posting each
    (OWN, (), False, True),
    (OWN + "?location=Remote&hl=en_US", (), False, True),
    ("https://www.searchsite.test/about/careers/applications/jobs/results/?q=program%20manager", (), True, False),
    ("https://www.searchsite.test/search?q=program+manager+jobs", (), True, False),
    # capitals: the same host
    ("https://WWW.BOARD.test/jobs/view/4242", (), True, True),
    # the owner's job sites change the board answer only
    (SITE, (), False, True),
    (SITE, ("jobs.sitebox.example.test",), True, True),
    # a job system's embed on an employer's own page names its post; on a board's host the same query is refused
    ("https://www.contoso.example.test/careers?gh_jid=12", (), False, True),
    ("https://www.board.test/company/contoso/?gh_jid=12", (), True, False),
    # not an address, not public, a sign-in
    ("", (), False, False),
    ("   ", (), False, False),
    ("https://localhost/jobs/view/1", (), False, False),
    ("https://www.contoso.example.test/login?gh_jid=12", (), False, False),
    ("https://jobs.contoso.example.test:99999999/x", (), False, False),
    ("ftp://careers.contoso.example.test/jobs/1", (), False, False),
    ("[::1", (), False, False),
]


@pytest.mark.parametrize("link, hosts, board, one", ROWS, ids=[r[0][:70] for r in ROWS])
def test_an_address_is_judged_a_boards_and_one_posting_apart(link, hosts, board, one):
    assert is_board_link(link, frozenset(hosts)) is board
    assert is_single_posting(link) is one


@pytest.mark.parametrize("junk", [None, 7, b"https://x.test/", ["https://x.test/"],
                                  pytest.param("x" * 100_000, id="long"), "http://[::1/x"])
def test_garbage_is_refused_and_never_raises(junk):
    assert is_board_link(junk) is False
    assert is_single_posting(junk) is False
    assert posting_page(junk) == (junk.strip() if isinstance(junk, str) else "")


@pytest.mark.parametrize("link", ["https://WWW.BOARD.test./jobs/view/4242", "board.test/jobs/view/1",
                                  "https://uk.listings.test./x"])
def test_a_trailing_dot_or_a_missing_scheme_is_the_same_board(link):
    assert is_board_link(link) is True


def test_the_owners_hosts_flip_the_board_answer_and_not_the_posting_answer():
    link = "https://jobs.sitebox.example.test/search"
    assert (is_board_link(link), is_board_link(link, {"jobs.sitebox.example.test"})) == (False, True)
    assert is_single_posting(link) is False


@pytest.mark.parametrize("host, named", [
    ("board.test", True), ("www.board.test", True), ("UK.LISTINGS.TEST.", True), ("notboard.test", False),
    ("board.test.example.test", False), ("", False), ("com", False),
])
def test_a_board_named_is_the_host_or_a_subdomain_of_one(host, named):
    assert ats.board_named(host) is named


