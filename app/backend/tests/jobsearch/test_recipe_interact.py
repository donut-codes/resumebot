"""Working a listing page the way a visitor would while a recording is made (`jobsearch/browser/recipes/interact.py`,
and
`recipe_record.capture_page` with it). The browser is scripted: no network, no Chrome, a made-up site."""

import json

import pytest

from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser.recipes import interact as recipe_interact
from app.jobsearch.browser.recipes import record as recipe_record

HOST = "careers.example.test"
PAGE = f"https://{HOST}/search"
API = f"https://{HOST}/api/search"
MARKERS = {"search": "'search box'", "next": "'next page'", "filter": "'filter'", "open": "'a job'"}


def _jobs(n: int = 4) -> list[dict]:
    return [{"jobId": f"J{i}", "title": f"Role {i}", "path": f"/job/J{i}", "city": "Remote"} for i in range(n)]


class Page:
    """A scripted browser tab: which steps find an element, what each click makes the page request, the HTML now."""

    def __init__(self, finds: dict[str, dict | None], after: dict[str, list[tuple]] | None = None,
                 html: str = "<html></html>") -> None:
        self.finds, self.after, self.html = finds, after or {}, html
        self.events: list[dict] = []
        self.bodies: dict[str, str] = {}
        self.clicks: list[str] = []
        self.typed: list[str] = []
        self.navigated: list[str] = []
        #: The labels of the controls in a cookie banner the page shows (None: it shows none), and the numbers clicked.
        self.banner: list[str] | None = None
        self.declined: list[int] = []
        self.last: str | None = None
        self.n = 0

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def request(self, kind: str, url: str, status: int, mime: str, body: str | None = None) -> None:
        self.n += 1
        rid = f"r{self.n}"
        self.events += [
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": rid, "type": kind, "request": {"method": "GET", "url": url}}},
            {"method": "Network.responseReceived", "params": {
                "requestId": rid, "type": kind, "response": {"status": status, "mimeType": mime}}},
            {"method": "Network.loadingFinished", "params": {"requestId": rid}}]
        if body is not None:
            self.bodies[rid] = body

    async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        p = params or {}
        if method == "Target.createTarget":
            return {"targetId": "t"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s"}
        if method == "Page.navigate":
            self.navigated.append(p["url"])
            self.request("Document", p["url"], 200, "text/html")
        elif method == "Network.getResponseBody":
            return {"body": self.bodies[p["requestId"]], "base64Encoded": False}
        elif method == "Runtime.evaluate":
            e = p.get("expression", "")
            if e == "document.readyState":
                return {"result": {"value": "complete"}}
            if "consent:find" in e:
                return {"result": {"value": json.dumps(self.banner)}} if self.banner is not None else {}
            if "consent:click" in e:
                self.declined.append(int(e.split('data-rb-consent="')[1].split('"')[0]))
                return {"result": {"value": json.dumps({"clicked": True})}}
            if "outerHTML" in e:
                return {"result": {"value": json.dumps({"url": PAGE, "html": self.html})}}
            if "typed:" in e:
                self.typed.append(e)
                return {"result": {"value": json.dumps({"typed": bool(self.finds.get("search"))})}}
            if "clicked:" in e:
                self.clicks.append(self.last or "")
                for kind, url, status, mime, body in self.after.get(self.last or "", []):
                    self.request(kind, url, status, mime, body)
                return {"result": {"value": json.dumps({"clicked": True})}}
            for step, marker in MARKERS.items():
                if marker in e:
                    found = self.finds.get(step)
                    self.last = step if found else self.last
                    return {"result": {"value": json.dumps(found or {"found": False})}}
        return {}


BOXED = {"search": {"found": True, "what": "search box", "href": None}}


def _link(href: str) -> dict:
    return {"found": True, "what": "x", "href": href}


BOX = {"found": True, "what": "x", "href": None}
ALL = {"search": BOX, "next": _link(f"https://{HOST}/search?from=4"), "filter": BOX,
       "open": _link(f"https://{HOST}/job/J0")}


class _AllowAll:
    """The polite fetcher's robots.txt check, with no network: every address is allowed."""

    async def allows(self, address: str) -> bool:
        return True

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(recipe_record, "Fetcher", _AllowAll)
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(browser_code, "sleep", no_wait)
    monkeypatch.setattr(recipe_record, "SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(recipe_record, "SCROLL_SECONDS", 0.0)
    monkeypatch.setattr(recipe_record, "QUIET_POLLS", 0)
    monkeypatch.setattr(recipe_interact, "ACTION_SETTLE", 0.0)
    monkeypatch.setattr(recipe_interact, "FIND_WAIT", 0.0)


async def _settle(_seconds: float) -> None:
    return None


async def _work(page: Page, **kw):
    return await recipe_interact.work(page, "s", PAGE, new_traffic=lambda: [], settle=_settle, **kw)


async def test_the_search_the_next_page_one_filter_and_one_job_are_done_once_each_in_that_order():
    page = Page(ALL)
    out = await _work(page)
    assert out.done == ["search", "next", "filter", "open"] and page.clicks == out.done and out.stop is None
    assert len(out.snapshots) == 5  # after the load and after each step


async def test_the_search_is_first_run_empty_and_a_keyword_is_typed_only_when_that_brought_no_results():
    page = Page({**ALL, **BOXED})
    out = await recipe_interact.work(page, "s", PAGE, new_traffic=lambda: [], settle=_settle,
                                     keyword=" Chef\n de  Partie ")
    assert out.done[0] == "search" and page.clicks[:2] == ["search", "search"]  # empty search, then the typed one
    assert len(page.typed) == 1 and '"Chef de Partie"' in page.typed[0]
    page = Page({**ALL, **BOXED})
    traffic = [{"kind": "XHR", "status": 200, "url": API}]
    await recipe_interact.work(page, "s", PAGE, new_traffic=lambda: traffic, settle=_settle, keyword="Chef")
    assert page.typed == []  # the empty search brought data: nothing is typed
    page = Page(ALL)
    await _work(page)
    assert page.typed == []  # no keyword given: nothing is ever typed


def test_a_typed_word_is_one_printable_line_of_at_most_eighty_characters():
    assert recipe_interact.clean_keyword("  a\x00b \n c ") == "ab c"
    assert recipe_interact.clean_keyword("x" * 200) == "x" * 80
    assert recipe_interact.clean_keyword("   ") is None and recipe_interact.clean_keyword(None) is None


async def test_a_step_the_page_has_none_of_is_skipped_and_the_rest_go_on():
    out = await _work(Page({"next": ALL["next"]}))
    assert out.done == ["next"] and set(out.skipped) == {"search", "filter", "open"}


async def test_a_recording_never_does_more_than_its_most_actions(monkeypatch):
    monkeypatch.setattr(recipe_interact, "ACTIONS", 2)
    out = await _work(Page(ALL))
    assert out.done == ["search", "next"] and out.skipped["filter"] == "the most actions were done"


async def test_a_recording_stops_when_its_time_is_up(monkeypatch):
    monkeypatch.setattr(recipe_interact, "INTERACT_SECONDS", -1.0)
    out = await _work(Page(ALL))
    assert out.done == [] and set(out.skipped.values()) == {"the time was up"}


async def test_a_link_to_another_site_or_a_private_address_is_never_followed():
    page = Page({"next": _link("https://elsewhere.example.test/x"), "open": _link("http://192.168.0.9/job/1")})
    out = await _work(page)
    assert out.done == [] and out.skipped["next"] == "it goes to another site"
    assert out.skipped["open"] == "its address is not a public one"


async def test_a_link_robots_txt_asks_not_to_read_is_not_followed():
    asked: list[str] = []

    async def no(address: str) -> bool:
        asked.append(address)
        return False

    out = await _work(Page({"next": ALL["next"], "filter": BOX}), allowed=no)
    assert out.done == ["filter"] and out.skipped["next"] == "robots.txt asks not to read it"
    assert asked == [ALL["next"]["href"]]  # a box has no address to ask about


async def test_a_refusal_after_a_step_stops_the_recording():
    page = Page(ALL, after={"next": [("Document", PAGE + "?from=4", 403, "text/html", None)]})
    out = await recipe_interact.work(page, "s", PAGE, new_traffic=lambda: recipe_record.assemble(page.events),
                                     settle=_settle)
    assert out.stop == 403 and out.done == ["search", "next"]


# --- a cookie banner (the owner, 2026-11-04: deny the tracking cookies and move on) ---------------------------------


async def test_a_cookie_banner_is_declined_first_and_takes_none_of_the_recordings_actions():
    page = Page(ALL)
    page.banner = ["Manage preferences", "Accept all", "Reject all"]
    out = await _work(page)
    assert page.declined == [2] and out.declined is True, "the one control that refuses cookies, once"
    assert out.done == ["search", "next", "filter", "open"] and page.clicks == out.done


async def test_a_banner_that_offers_only_accept_is_never_accepted_and_the_steps_go_on_under_it():
    page = Page(ALL)
    page.banner = ["Accept all cookies", "Accept", "Cookie settings"]
    out = await _work(page)
    assert page.declined == [] and out.declined is False
    assert out.done == ["search", "next", "filter", "open"]


@pytest.mark.parametrize("banner", [None, [], "not a list", [None, 3]])
async def test_a_page_with_no_banner_or_a_look_that_gave_nothing_is_left_as_it_is(banner):
    page = Page(ALL)
    page.banner = banner
    out = await _work(page)
    assert page.declined == [] and out.declined is False and len(out.snapshots) == 5


async def test_a_tab_that_went_away_while_the_banner_was_looked_for_is_no_banner():
    class Gone(Page):
        async def call(self, method, params=None, session_id=None):
            if "consent:find" in (params or {}).get("expression", ""):
                raise browser_code.BrowserUnavailable("the tab went")
            return await super().call(method, params, session_id)

    assert await recipe_interact.decline_consent(Gone(ALL), "s") is False


async def test_declining_the_banner_lets_the_page_settle_before_the_steps_begin():
    settled: list[float] = []

    async def settle(seconds: float) -> None:
        settled.append(seconds)

    page = Page(ALL)
    page.banner = ["Decline"]
    await recipe_interact.work(page, "s", PAGE, new_traffic=lambda: [], settle=settle)
    assert page.declined == [0] and len(settled) == 5, "one wait for the banner, one after each of the four steps"


async def test_a_page_whose_cookie_banner_names_recaptcha_is_no_bot_check_and_the_banner_is_declined():
    """A consent manager lists reCAPTCHA among the cookies it explains, and a short page that was only
    its banner read as a bot check, so the recording stopped the site."""
    html = ('<html><body><div id="onetrust-consent-sdk"><p>We use cookies. Boardfour reCAPTCHA keeps bots out.</p>'
            "<button>Reject all</button></div></body></html>")
    page = Page(ALL, html=html)
    page.banner = ["Reject all"]
    raw, stop = await recipe_record.capture_page(PAGE, page)
    assert stop is None and page.declined == [0] and page.clicks == ["search", "next", "filter", "open"]


async def test_a_real_bot_check_behind_a_cookie_banner_still_stops_the_recording():
    html = ('<html><body><div class="cookie-banner">We use cookies.</div><h1>Just a moment...</h1>'
            "<p>Verify you are human</p></body></html>")
    page = Page(ALL, html=html)
    raw, stop = await recipe_record.capture_page(PAGE, page)
    assert (raw, stop) == ([], recipe_record.BOT_WALL_STATUS) and page.clicks == []


# The banner finder is JavaScript the page runs (`consent_find.js`, beside `interact.py`). Its tests run it against a
# stand-in page under Node, in the scout's node tests (`app/scout/consent-find.test.mjs`), not here.


def test_a_refusal_is_a_page_answered_401_403_or_429_or_any_request_to_the_host_answered_429():
    doc = {"kind": "Document", "status": 403, "url": PAGE}
    assert recipe_interact.refused([doc], HOST) == 403
    assert recipe_interact.refused([{"kind": "XHR", "status": 429, "url": API}], HOST) == 429
    assert recipe_interact.refused([{"kind": "XHR", "status": 429, "url": "https://x.example.test/a"}], HOST) is None
    assert recipe_interact.refused([{"kind": "XHR", "status": 403, "url": API}], HOST) is None


# --- the recording --------------------------------------------------------------------------------------------------


async def test_what_a_page_asks_for_only_when_a_visitor_interacts_is_in_the_recording():
    answer = json.dumps({"results": _jobs(4)})
    page = Page(ALL, after={"next": [("XHR", f"{API}?from=4", 200, "application/json", answer)]})
    raw, stop = await recipe_record.capture_page(PAGE, page)
    assert stop is None and [r["url"] for r in raw] == [f"{API}?from=4"]
    assert page.clicks == ["search", "next", "filter", "open"]


async def test_a_refusal_during_the_interaction_is_a_stop_with_nothing_kept():
    page = Page(ALL, after={"filter": [("Document", PAGE, 429, "text/html", None)]})
    raw, stop = await recipe_record.capture_page(PAGE, page)
    assert (raw, stop) == ([], 429)


async def test_data_embedded_in_the_pages_html_comes_with_the_requests():
    state = json.dumps({"search": {"jobs": _jobs(4)}, "pad": "x" * 200})
    html = f"<html><script>appState = {state};</script></html>"
    raw, stop = await recipe_record.capture_page(PAGE, Page({}, html=html))
    assert stop is None and [(b["blob"], b["url"]) for b in raw] == [("var:appState", PAGE)]


async def test_with_interaction_off_a_page_is_only_loaded_and_scrolled():
    page = Page(ALL)
    await recipe_record.capture_page(PAGE, page, interact=False)
    assert page.clicks == []


class LatePage(Page):
    """A slow page: it asks for its settings at once and, only when those are answered a few half-second polls later,
    for the posting (an Hardone or Hardtwo page draws itself from several requests in a row). `late` is the poll at
    which the settings are answered; the posting is asked for then and answered two polls after."""

    def __init__(self, late: int) -> None:
        super().__init__({})
        self.late, self.polls = late, 0
        self.answers = {"settings": json.dumps({"app": {"site": "x"}}),
                        "detail": json.dumps({"requisition": {"title": "Role", "description": "Plan programs. " * 20}})}

    def _send(self, rid: str, url: str) -> None:
        self.events.append({"method": "Network.requestWillBeSent", "params": {
            "requestId": rid, "type": "XHR", "request": {"method": "GET", "url": url}}})

    def _answer(self, rid: str) -> None:
        self.events += [
            {"method": "Network.responseReceived", "params": {
                "requestId": rid, "type": "XHR", "response": {"status": 200, "mimeType": "application/json"}}},
            {"method": "Network.loadingFinished", "params": {"requestId": rid}}]

    async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        if method == "Runtime.evaluate" and (params or {}).get("expression") == "1":
            self.polls += 1
            schedule = {1: lambda: self._send("settings", f"{API}/settings"),
                        self.late: lambda: (self._answer("settings"), self._send("detail", f"{API}/detail")),
                        self.late + 2: lambda: self._answer("detail")}
            schedule.get(self.polls, lambda: None)()
        if method == "Network.getResponseBody" and (params or {}).get("requestId") in self.answers:
            return {"body": self.answers[params["requestId"]], "base64Encoded": False}
        return await super().call(method, params, session_id)


async def test_a_slow_page_is_waited_for_until_its_requests_have_gone_quiet(monkeypatch):
    monkeypatch.setattr(recipe_record, "QUIET_POLLS", 4)
    monkeypatch.setattr(recipe_record, "MOST_POLLS", 40)
    page = LatePage(late=8)
    raw, stop = await recipe_record.capture_page(PAGE, page, interact=False)
    assert stop is None and [r["url"] for r in raw] == [f"{API}/settings", f"{API}/detail"]


async def test_the_wait_for_a_page_that_never_goes_quiet_ends_at_its_most(monkeypatch):
    monkeypatch.setattr(recipe_record, "QUIET_POLLS", 4)
    monkeypatch.setattr(recipe_record, "MOST_POLLS", 6)
    page = LatePage(late=500)  # the settings are never answered
    raw, stop = await recipe_record.capture_page(PAGE, page, interact=False)
    assert stop is None and raw == [] and page.polls == 12  # the most, twice (after the load, after the scroll)


# --- following the employer's page to the board the jobs load on ----------------------------------------------------


def test_a_page_that_only_points_to_its_job_board_gives_that_board_once():
    html = ('<a href="https://www.boardone.test/company/x/jobs">in</a><a href="https://boards.example.test/careers/acme">c</a>'
            '<script>{"u":"https:\\/\\/boards.example.test\\/job-board\\/acme"}</script>')
    assert recipe_interact.board_link(html, "https://careers.acme.test/") == "https://boards.example.test/job-board/acme"


def test_a_careers_path_is_taken_only_when_there_is_no_job_board_and_a_social_or_private_address_never():
    assert recipe_interact.board_link('<a href="https://b.example.test/careers/acme">', "https://x.test/") \
        == "https://b.example.test/careers/acme"
    html = '<a href="https://www.facebook.com/jobs">f</a><a href="https://192.168.0.2/jobs">p</a><a href="https://x.test/jobs">s</a>'
    assert recipe_interact.board_link(html, "https://x.test/") is None


async def test_a_page_with_no_listing_is_followed_to_the_board_it_names_and_not_without_robots_txt(monkeypatch):
    board = "https://boards.example.test/job-board/acme"
    page = Page({}, html=f'<html><a href="{board}">jobs</a></html>')
    raw, stop = await recipe_record.capture_page(PAGE, page)
    assert stop is None and page.navigated == [PAGE, board]

    class Never(_AllowAll):
        async def allows(self, address: str) -> bool:
            return False

    monkeypatch.setattr(recipe_record, "Fetcher", Never)
    page = Page({}, html=f'<html><a href="{board}">jobs</a></html>')
    await recipe_record.capture_page(PAGE, page)
    assert page.navigated == [PAGE]
