"""Go's check in the app's own Chrome, for a page code cannot tell (the owner, 2026-10-16: tabs that opened on a
closed notice). The browser is a fixture here: `browser_page` answers from the `browser` dict."""

import asyncio
import json
import selectors

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import go_checks
from app.jobsearch.browser.code import BrowserUnavailable, Page
from app.jobsearch.fetch import polite
from app.jobsearch.pipeline import board
from app.jobsearch.postings import sourced
from app.models.work import TrailEvent
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.jobsearch.test_go_checks import TODAY, _add, _prepare, _roles, fixture_internet, role
from tests.jobsearch.test_go_posting_match import NOT_AVAILABLE_PAGE, NOT_AVAILABLE_SAYS
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("example_world")

HOST = "https://careers.example.test"
DRAWN = "<html><body><div id='app'>{}</div></body></html>"
CLOSED_PHRASES = [
    "No longer accepting applications", "Not currently accepting applications", "This job is no longer available",
    "The position has been filled", "This posting has expired", "Applications are now closed",
]


def _drawn(words: str) -> str:
    """A drawn page: the words, and enough plain filler that the page is not too short to judge (under 40 words reads
    unknown)."""
    return DRAWN.format(f"<h1>Product Lead</h1><p>{words}</p>" + "<p>Lead the product team and its plans.</p>" * 10)


@pytest.fixture
def browser(monkeypatch):
    """The app's Chrome: `shown[path]` is the `Page` it draws for a link ending so; a missing one is unavailable."""
    shown: dict[str, Page] = {}
    asked: list[str] = []

    async def page(link):
        asked.append(link)
        for path, got in shown.items():
            if link.endswith(path):
                return got
        raise BrowserUnavailable("no such page")

    monkeypatch.setattr(polite, "make", fixture_internet())
    monkeypatch.setattr(go_checks, "start_browser", _chrome_up)
    monkeypatch.setattr(go_checks, "browser_page", page)
    shown["asked"] = asked  # type: ignore[assignment]
    return shown


def _show(browser, path, html, status=200, url=None):
    browser[path] = Page(url=url or f"{HOST}{path}", status=status, html=html)


@pytest.mark.parametrize("phrase", CLOSED_PHRASES)
async def test_a_closed_phrase_drawn_only_in_the_browser_is_closed(phrase):
    assert go_checks.closed_words(phrase) is not None, "the phrase is one Go knows"


@pytest.mark.parametrize("phrase", CLOSED_PHRASES)
async def test_the_browser_check_reads_each_closed_phrase(browser, phrase):
    _show(browser, "/job", _drawn(phrase))
    state, said = await go_checks.browser_verdict(f"{HOST}/job", TODAY)
    assert state == go_checks.CLOSED and "browser" in said


async def test_the_browser_check_reads_a_404_an_open_page_a_sign_in_wall_and_an_unavailable_browser(browser):
    _show(browser, "/missing", "", status=404)
    _show(browser, "/open", _drawn("Lead the product. Apply below."))
    _show(browser, "/wall", _drawn("Sign in"), url="https://www.board.test/authwall?trk=x")
    assert (await go_checks.browser_verdict(f"{HOST}/missing", TODAY))[0] == go_checks.CLOSED
    assert await go_checks.browser_verdict(f"{HOST}/open", TODAY) == (go_checks.OPEN, "")
    assert await go_checks.browser_verdict(f"{HOST}/wall", TODAY) == (go_checks.UNKNOWN, "")
    assert await go_checks.browser_verdict(f"{HOST}/nothing", TODAY) == (go_checks.UNKNOWN, "")


async def test_a_page_that_takes_too_long_in_the_browser_is_unknown(monkeypatch):
    async def slow(link):
        await asyncio.sleep(5)

    monkeypatch.setattr(go_checks, "browser_page", slow)
    monkeypatch.setattr(go_checks, "BROWSER_SECONDS", 0.05)
    assert await go_checks.browser_verdict(f"{HOST}/job", TODAY) == (go_checks.UNKNOWN, "")


class _SkippedWaits(selectors.DefaultSelector):
    """The selector of `_TestClock`: a wait for a timer moves the loop's clock on by that long and returns at once."""

    clock: "_TestClock"

    def select(self, timeout=None):
        if timeout:
            self.clock.now += timeout
        return super().select(0 if timeout else timeout)


class _TestClock(asyncio.SelectorEventLoop):
    """An event loop on the test's own clock: every sleep and timeout takes its seconds on `now`, and none for real."""

    now = 0.0

    def __init__(self) -> None:
        waits = _SkippedWaits()
        waits.clock = self
        super().__init__(waits)

    def time(self) -> float:
        return self.now


class _ShortPage:
    """A DevTools stand-in: the page has loaded at once, shows `SHOWN` characters and never more, and reads as the
    closed posting's page."""

    SHOWN = 150

    async def call(self, method, params=None, session_id=None):
        expression = (params or {}).get("expression", "")
        if expression == browser_code.READY:
            return {"result": {"value": "complete"}}
        if expression == browser_code._TEXT_LENGTH:
            return {"result": {"value": self.SHOWN}}
        return {"result": {"value": json.dumps({"url": "", "status": 200, "html": NOT_AVAILABLE_PAGE})}}


def test_a_loaded_page_that_shows_little_text_is_read_within_the_checks_budget(monkeypatch):
    """A closed posting's page says one line, so the read waits its whole settle and fill time for
    text that never comes, and that was all of the check's budget: eight checks of one such page said unknown."""
    assert _ShortPage.SHOWN < browser_code.FILL_CHARS, "the page never shows what the read waits for"

    async def page(link):
        return await browser_code._read_tab(_ShortPage(), "sid", link)  # type: ignore[arg-type]

    monkeypatch.setattr(go_checks, "browser_page", page)
    clock = _TestClock()
    try:
        state, said = clock.run_until_complete(go_checks.browser_verdict(f"{HOST}/job", TODAY))
    finally:
        clock.close()
    assert clock.now == browser_code.SETTLE_SECONDS + browser_code.FILL_SECONDS, "the read waited both out"
    assert state == go_checks.CLOSED and NOT_AVAILABLE_SAYS in said


def test_the_checks_budget_is_longer_than_the_reads_waits_on_a_loaded_page():
    waits = browser_code.SETTLE_SECONDS + browser_code.FILL_SECONDS
    assert go_checks.BROWSER_READ_SECONDS == waits, "the read's waits, as the check's budget states them"
    assert go_checks.BROWSER_SECONDS == waits + go_checks.BROWSER_LOAD_SECONDS > waits


async def test_a_page_code_cannot_tell_is_checked_in_the_browser_and_a_closed_one_is_ended_and_refilled(js, browser,
                                                                                                          monkeypatch):
    monkeypatch.setattr(board, "APPLY_BATCH_DEFAULT", 2)
    _show(browser, "/a", _drawn("No longer accepting applications"))
    _show(browser, "/b", _drawn("Lead the product. Apply below."))
    _show(browser, "/c", _drawn("Lead the product. Apply below."))
    await _add(role("a", fit=9, apply_url=f"{HOST}/open/a"), role("b", fit=8, apply_url=f"{HOST}/open/b"),
               role("c", fit=7, apply_url=f"{HOST}/open/c"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["b", "c"], "a closed role is replaced by the next in the line"
    assert [s["title"] for s in out.skipped] == ["Product Lead a"]
    assert out.skipped[0]["why"] == "The posting is closed; moved to Gone Quiet."
    roles = await _roles()
    assert (roles["a"].status, roles["a"].ended_reason) == ("gone_quiet", "posting_gone")
    assert roles["b"].go_check == go_checks.OPEN and roles["c"].go_check == go_checks.OPEN


async def test_a_job_board_page_is_checked_in_the_browser_and_one_still_unknown_opens_as_before(js, browser):
    _show(browser, "/jobs/view/1", _drawn("This job is no longer available"))
    await _add(role("a", fit=9, apply_url="https://www.board.test/jobs/view/1"),
               role("b", fit=8, apply_url="https://www.board.test/jobs/view/2"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["b"], "the second page could not be seen, so it opens as it would have"
    roles = await _roles()
    assert roles["a"].status == "gone_quiet"
    assert roles["b"].go_check == go_checks.BROWSER, "and stays a browser check for a session"


async def test_pages_of_different_hosts_are_checked_side_by_side_and_one_host_in_order(js, browser, monkeypatch):
    monkeypatch.setattr(board, "APPLY_BATCH_DEFAULT", 4)
    live, order = 0, []
    peak = 0

    async def page(link):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        order.append(link)
        await asyncio.sleep(0.05)
        live -= 1
        return Page(url=link, status=200, html=_drawn("Apply below."))

    monkeypatch.setattr(go_checks, "browser_page", page)
    await _add(role("a", fit=9, apply_url="https://one.example.test/job/1"),
               role("b", fit=8, apply_url="https://one.example.test/job/2"),
               role("c", fit=7, apply_url="https://two.example.test/job/3"),
               role("d", fit=6, apply_url="https://three.example.test/job/4"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a", "b", "c", "d"], "the batch keeps its order"
    assert peak == 3, "three hosts at once, never two pages of one host"
    assert order.index("https://one.example.test/job/1") < order.index("https://one.example.test/job/2")


async def test_a_role_with_the_systems_own_text_or_a_live_posting_is_open_without_the_browser(js, browser):
    asked = browser["asked"]
    await _add(role("a", fit=9, apply_url=f"{HOST}/live"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a"] and asked == []


async def test_the_pre_check_uses_the_same_browser_check(js, browser):
    _show(browser, "/a", _drawn("Not currently accepting applications"))
    await _add(role("a", fit=9, apply_url=f"{HOST}/open/a"), role("b", fit=8, apply_url=f"{HOST}/live"))
    out = await go_checks.precheck(OWNER)
    assert [r.slug for r in out.ready] == ["b"]
    assert (await _roles())["a"].status == "gone_quiet"


async def test_a_page_that_says_the_opportunity_is_not_available_ends_the_role_in_the_apps_chrome(js, browser):
    """A job system's page, drawn in the browser, that keeps its address and says only "This
    opportunity is currently not available." was not a closed phrase, so every check of it came back without one."""
    _show(browser, "/open/a", NOT_AVAILABLE_PAGE)
    _show(browser, "/open/b", _drawn("Lead the product. Apply below."))
    state, said = await go_checks.browser_verdict(f"{HOST}/open/a", TODAY)
    assert state == go_checks.CLOSED and NOT_AVAILABLE_SAYS in said
    await _add(role("a", fit=9, apply_url=f"{HOST}/open/a"), role("b", fit=8, apply_url=f"{HOST}/open/b"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["b"]
    assert out.skipped[0]["why"] == "The posting is closed; moved to Gone Quiet."
    roles = await _roles()
    assert (roles["a"].status, roles["a"].ended_reason) == ("gone_quiet", "posting_gone")
    assert roles["b"].go_check == go_checks.OPEN
    async with db.session_factory()() as session:
        lines = (await session.scalars(select(TrailEvent).where(TrailEvent.event == go_checks.BROWSER_EVENT))).all()
    assert sorted(t.sentence for t in lines) == ["Checked in the browser: closed.", "Checked in the browser: open."]
    assert any(NOT_AVAILABLE_SAYS in (t.detail or "") for t in lines), "the page's words are the evidence"


async def _chrome_up():
    return ""


async def test_chrome_down_is_started_once_then_the_check_runs_and_results_are_on_the_trail(js, browser, monkeypatch):
    started = []

    async def start():
        started.append(1)
        return ""

    monkeypatch.setattr(go_checks, "start_browser", start)
    _show(browser, "/a", _drawn("No longer accepting applications"))
    _show(browser, "/b", _drawn("Apply below."))
    await _add(role("a", fit=9, apply_url=f"{HOST}/open/a"), role("b", fit=8, apply_url=f"{HOST}/open/b"))
    out = await _prepare()
    assert started == [1] and [r.slug for r in out.ready] == ["b"] 
    assert (await _roles())["a"].status == "gone_quiet"
    async with db.session_factory()() as session:
        said = sorted(t.sentence for t in (await session.scalars(
            select(TrailEvent).where(TrailEvent.event == go_checks.BROWSER_EVENT))).all())
    assert said == ["Checked in the browser: closed.", "Checked in the browser: open."]


async def test_chrome_that_cannot_start_says_so_in_one_line_and_never_passes_silently(js, browser, monkeypatch):
    async def start():
        return "Chrome was not found"

    monkeypatch.setattr(go_checks, "start_browser", start)
    _show(browser, "/a", _drawn("No longer accepting applications"))
    await _add(role("a", fit=9, apply_url=f"{HOST}/open/a"), role("b", fit=8, apply_url=f"{HOST}/open/b"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a", "b"] and browser["asked"] == []
    assert (
        "The closed-posting check couldn't run for 2 roles: Chrome was not found." in out.notes)


def test_go_opens_the_employers_posting_of_a_role_found_on_a_job_board_never_the_boards_page():
    """A role looked up at the read-next mark has its employer's address (`source_url`), and Go opens that: Exboard's
    page is not where the posting is checked (docs/PRD.md 32.3, 24.13)."""
    from types import SimpleNamespace

    exboard = "https://www.board.test/jobs/view/7001/"
    employer = f"{HOST}/jobs/7001"
    for url in (exboard, employer):  # `url` is the employer's once the role is sourced; before, still Exboard's
        looked_up = SimpleNamespace(source_url=employer, source_kind=sourced.EMPLOYER, apply_url=exboard, url=url,
                                    board_url=exboard)
        assert go_checks.link_of(looked_up) == employer
