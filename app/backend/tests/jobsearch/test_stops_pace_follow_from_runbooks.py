"""Stops, pace and the followed boards come from the registry's runbooks, never from a name in code (the generic-sites
design 1.4, 1.6, 2.4): which domain's stop ends at the owner's off hours and is kept under one key for all its hosts,
how fast a domain is read, which boards' page loads the queue holds for their own reader, which boards the code reads
first, and that a runbook that did not load (or a missing one, for a domain the record holds an ending stop for) never
gives a host the generic pace or a stop for good. Every site here is made up (`.test`), with a regional subdomain."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import db
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import takeable
from app.jobsearch.browser.recipes import interact
from app.jobsearch.browser.takeable import Taker
from app.jobsearch.fetch import host_stops, polite
from app.jobsearch.models import BrowserItem
from app.sites import registry
from app.sites.registry import Problem, Registry
from app.sites.schema import Runbook
from tests.support import DEFAULT_OWNER as OWNER

NOW = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)

FEED = "feedboard.test"  # a board whose stop ends at the night, is paced slowly and whose roles are followed
REGIONAL = "uk.feedboard.test"  # one of its regional hosts
PLAIN = "plainboard.test"  # a board with nothing special
EMPLOYER = "careers.example.test"  # no runbook at all


def _doc(key: str, host: str, **over) -> dict:
    doc = {"schema": 1, "key": key, "name": key.title(), "kind": "board", "hosts": [host]}
    doc.update(over)
    return doc


def _registry(*docs: dict, problems: tuple[Problem, ...] = (), present: bool = True) -> Registry:
    return Registry([Runbook.model_validate(d) for d in docs], problems, restricted_present=present, terms=())


@pytest.fixture
def world():
    """A feed board (stop ends, 30 seconds, followed to the employer), a plain board and a job system."""
    reg = _registry(
        _doc("feed", FEED, pace_seconds=30.0, stop="until_next_off_hours", follow_to_employer=True),
        _doc("plain", PLAIN),
        _doc("system", "system.example.test", kind="job_system", order=0, slow_statuses=[429, 503]))
    host_stops.reset()
    with registry.replaced(reg):
        yield reg
    host_stops.reset()


# --- stop_key, ends_with_the_night -----------------------------------------------------------------------------------


@pytest.mark.parametrize("host", [FEED, f"www.{FEED}", REGIONAL, f"jobs.{REGIONAL}", f"{REGIONAL}.", REGIONAL.upper()])
def test_every_host_of_a_domain_whose_stop_ends_is_stopped_under_the_domain(world, host):
    assert host_stops.stop_key(host) == FEED
    assert host_stops.ends_with_the_night(host) is True


@pytest.mark.parametrize("host", [PLAIN, f"www.{PLAIN}", EMPLOYER, "jobs.system.example.test", "notfeedboard.test",
                                  f"{FEED}.other.test"])
def test_every_other_host_is_stopped_under_itself_and_for_good(world, host):
    assert host_stops.stop_key(host) == host_stops.key_of(host)
    assert host_stops.ends_with_the_night(host) is False


async def test_a_stop_at_a_regional_host_is_kept_for_the_domain_and_ends_at_the_next_off_hours_start(js, world):
    by_day = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)  # the made-up owner's off hours are 1 to 7 AM
    host_stops._stopped = {}  # the stops are loaded
    async with db.session_factory()() as session:
        kept = await host_stops.stop(session, OWNER, REGIONAL, by_day, "the site asked to verify a visitor")
        other = await host_stops.stop(session, OWNER, EMPLOYER, by_day, "answered 403")
        await session.commit()
    assert (kept.domain, kept.stopped_until) == (FEED, datetime(2026, 11, 6, 1, 0, tzinfo=UTC))
    assert (other.domain, other.stopped_until) == (EMPLOYER, None), "any other host: for good"
    assert host_stops.stopped_reason(f"www.{FEED}", by_day) and host_stops.stopped_reason(PLAIN, by_day) is None


# --- pace -----------------------------------------------------------------------------------------------------------


def test_a_domain_is_paced_at_its_runbooks_pace_and_never_faster_than_the_page_gap(world):
    assert registry.pace_for_domain(FEED) == 30.0
    assert bp.page_gap(FEED) >= timedelta(seconds=30)
    assert registry.pace_for_domain(PLAIN) == 0.0
    low, high = bp.PAGE_GAP
    assert timedelta(seconds=low) <= bp.page_gap(PLAIN) <= timedelta(seconds=high)
    assert bp.page_gap(None) <= timedelta(seconds=high)


def test_the_fetcher_paces_every_host_under_a_slow_runbook_and_leaves_the_others_at_its_own(world):
    fetch = polite.Fetcher(httpx.AsyncClient(), per_host_seconds=2.0)
    assert fetch._every(FEED) == 30.0 and fetch._every(REGIONAL) == 30.0
    assert fetch._every(PLAIN) == 2.0 and fetch._every(EMPLOYER) == 2.0
    assert fetch.slow_host(PLAIN, 2.0, 10.0) == 4.0 and fetch.slow_host(REGIONAL, 2.0, 60.0) == 60.0


def test_the_pace_of_a_host_is_its_runbooks_under_a_runbook_with_a_pace_and_the_default_elsewhere(world):
    assert polite.pace_of(REGIONAL, 1.0) == 30.0 and polite.pace_of(PLAIN, 1.0) == 1.0
    assert polite.HOST_SECONDS == {}, "the app sets none: only a test does"


# --- the followed boards ---------------------------------------------------------------------------------------------


def test_the_followed_boards_and_the_code_first_boards_are_the_registrys(world):
    assert bp.follow_domains() == (FEED,)
    assert FEED in bp.code_source_domains()
    assert PLAIN not in bp.code_source_domains()
    assert bp.site_name(FEED) == "Feed" and bp.site_name(EMPLOYER) == EMPLOYER


def test_with_no_followed_board_nothing_is_held_for_its_reader():
    with registry.replaced(_registry(_doc("plain", PLAIN))):
        assert bp.follow_domains() == ()
        assert takeable.held_for_feed(_row(PLAIN)) is False


def _row(domain: str, kind: str = bp.SOURCE, priority: int = 2) -> BrowserItem:
    return BrowserItem(id=uuid.uuid4(), owner=OWNER, kind=kind, target=domain, link=f"https://{domain}/jobs/1",
                       domain=domain, state="waiting", tries=0, next_at=NOW, priority=priority, order_at=NOW,
                       code_only=False, created_at=NOW)


def test_the_backlog_of_a_followed_board_is_held_for_its_reader_and_the_owners_item_is_not(world):
    assert takeable.held_for_feed(_row(FEED)) is True
    assert takeable.held_for_feed(_row(FEED, priority=0)) is False
    assert takeable.held_for_feed(_row(PLAIN)) is False


def test_a_source_on_a_followed_board_is_tried_by_code_first_and_one_on_a_plain_board_is_not(world):
    assert takeable.code_first(_row(FEED)) is True
    assert takeable.code_first(_row(PLAIN)) is False  # a board's listing: a session decides


def test_the_boards_are_never_taken_for_where_the_jobs_are_and_a_social_site_neither(world):
    page = ('<a href="https://www.plainboard.test/jobs">x</a><a href="https://facebook.com/acme">y</a>'
            '<a href="https://uk.feedboard.test/jobs">z</a><a href="https://boards.employer.test/jobs">ok</a>')
    assert interact.board_link(page, "https://employer.test/careers") == "https://boards.employer.test/jobs"
    assert "plainboard." in interact.not_boards() and "feedboard." in interact.not_boards()


# --- slow notes -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("note", [
    "the site asked the app to slow down (3 pages)", "sign-in page: try again later", "try again in an hour",
    "HTTP 429 too many requests", "rate limit reached", "a slowing down notice"])
def test_a_note_in_the_words_of_a_slow_down_is_a_rest_not_a_stop(note):
    assert host_stops.is_rate_limit(note)


@pytest.mark.parametrize("note", ["a security check page: the site asked the visitor to verify they are human",
                                  "403 forbidden", "the page shows a captcha"])
def test_a_note_in_the_words_of_a_refusal_is_not_a_rest(note):
    assert not host_stops.is_rate_limit(note)


def test_the_notes_the_codes_own_hand_backs_use_say_what_a_rest_says():
    from app.jobsearch.browser import code as browser_code

    assert host_stops.is_rate_limit(browser_code.signed_out_note("Example Board"))
    assert host_stops.is_rate_limit("the site asked the app to slow down (stopped at 2)")


# --- fail closed ----------------------------------------------------------------------------------------------------


@pytest.fixture
def broken():
    """A registry in which the runbook of `feedboard.test` did not load."""
    reg = _registry(_doc("plain", PLAIN), problems=(Problem("feed.json", "not readable as JSON", (FEED,)),))
    host_stops.reset()
    with registry.replaced(reg):
        yield reg
    host_stops.reset()


def test_a_host_whose_runbook_did_not_load_is_refused_with_a_reason_and_is_not_read(broken):
    assert "did not load" in (host_stops.stopped_reason(REGIONAL) or "")
    assert host_stops.stopped_reason(PLAIN) is None
    fetch = polite.Fetcher(httpx.AsyncClient())
    with pytest.raises(polite.Refused):
        fetch._every(REGIONAL)
    assert fetch._every(PLAIN) == 1.0


def test_a_host_whose_runbook_did_not_load_never_gets_a_stop_for_good(broken):
    assert host_stops.ends_with_the_night(REGIONAL) is True
    assert host_stops.stop_key(REGIONAL) == REGIONAL  # a key only: nothing reads it
    with pytest.raises(registry.RunbookUnavailable):
        registry.pace_for_domain(FEED)


async def test_the_queue_leaves_the_items_of_a_host_whose_runbook_did_not_load_alone(js, broken):
    async with db.session_factory()() as session:
        session.add_all([_row(FEED, kind=bp.SCAN, priority=0), _row(PLAIN, kind=bp.SCAN, priority=0)])
        await session.commit()
    async with db.session_factory()() as session:
        taker = Taker(OWNER, bp.WORKER, (bp.SCAN,), code_first=False, owners_only=False, claimed=frozenset())
        offered = [row.domain for row, _ in await takeable.ask(session, taker, NOW)]
    assert offered == [PLAIN]


def test_a_domain_the_record_holds_an_ending_stop_for_is_still_stopped_for_the_night_with_no_runbook_for_it():
    with registry.replaced(_registry(_doc("plain", PLAIN), present=False)):
        host_stops.reset()
        host_stops._ending.add(FEED)
        try:
            assert host_stops.stop_key(REGIONAL) == FEED and host_stops.ends_with_the_night(REGIONAL) is True
            assert host_stops.stop_key(EMPLOYER) == EMPLOYER and host_stops.ends_with_the_night(EMPLOYER) is False
        finally:
            host_stops.reset()


def test_a_record_of_an_ending_stop_gives_way_to_a_runbook_that_says_otherwise(world):
    host_stops._ending.add(PLAIN)
    assert host_stops.ends_with_the_night(PLAIN) is False and host_stops.stop_key(PLAIN) == PLAIN


# --- the old answers, kept ------------------------------------------------------------------------------------------


def _old_stop_key(host: str, until: frozenset[str]) -> str:
    key = (host or "").strip().lower().rstrip(".").removeprefix("www.")
    for domain in until:
        if key == domain or key.endswith(f".{domain}"):
            return domain
    return key


@pytest.mark.parametrize("host", [
    FEED, f"www.{FEED}", REGIONAL, f"jobs.{REGIONAL}", f"{REGIONAL}.", "", None, PLAIN, f"www.{PLAIN}", EMPLOYER,
    "feedboard.test.evil.test", "xfeedboard.test", "system.example.test", "a.b.c.system.example.test"])
def test_the_registry_answers_what_the_constants_answered(world, host):
    until = frozenset({FEED})
    assert host_stops.stop_key(host) == _old_stop_key(host or "", until)
    assert host_stops.ends_with_the_night(host or "") == (_old_stop_key(host or "", until) in until)
    old_pace = max((s for h, s in {FEED: 30.0}.items() if host and host_stops.domain_of(h) == host), default=0.0)
    assert registry.pace_for_domain(host) == old_pace
