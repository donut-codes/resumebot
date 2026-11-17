"""The app's code tries browser items first, the sessions' page digest, and the time box per item
(the browser work spec, "Code first, a digest, time boxes"; `browser/code.py`, `browser/items.py`). The extractors
read generic fixture pages (an embedded JobPosting present, absent or partial; a board listing's Apply). No test reaches
the network or a browser: the polite fetch answers from a mock and the app's browser is a stand-in. Every name, address
and sentence is made up."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import items as bd
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import queue as bq
from app.jobsearch.browser import reader as br
from app.jobsearch.browser import takeable
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import (
    BrowserAttempt,
    BrowserDomain,
    BrowserItem,
    JobPosting,
    MissionRun,
    Opportunity,
    RawPosting,
)
from app.jobsearch.postings.raw_copies import inflate
from app.jobsearch.research import analysis
from tests.builders import (
    ONE,
    PATHFINDER,
    WORKER,
    WORKER_B,
    browser_card,
    browser_world,
    hand_back,
    in_session,
    items,
    next_item,
    scan_hand_in,
)
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import DEFAULT_OWNER as OWNER
from tests.support import POSTING_WORDS

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out", "example_world")

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
CODE = "code-test0001"
EMPLOYER = "https://boards.feedsys.test/initech1/jobs/401"
CAREERS = "https://careers.northwind{n}.test/jobs/{n}"
BOARD_LINK = "https://www.board.test/jobs/view/{n}"
DESCRIPTION = "<p>" + POSTING_WORDS.replace("\n", "<br>") + "</p>"


def posting_page(title: str | None = "Programs Director 1", company: str | None = "Initech 1",
                 description: str | None = DESCRIPTION, body: str = "") -> str:
    """A page that embeds a schema.org JobPosting with the fields given (None leaves one out)."""
    ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": title,
          "hiringOrganization": {"@type": "Organization", "name": company} if company else None,
          "description": description, "jobLocation": {"@type": "Place", "address": {"addressLocality": "Springdale"}}}
    ld = {k: v for k, v in ld.items() if v is not None}
    return (f'<html><head><title>{title or "Jobs"}</title><script type="application/ld+json">{json.dumps(ld)}'
            f"</script></head><body><h1>{title or 'Jobs'}</h1>{body}</body></html>")


BARE_PAGE = "<html><head><title>Careers</title></head><body><div id='app'>Loading</div></body></html>"


SAFETY_LINK = ('<a class="jobs-apply-button" href="https://www.board.test/safety/go/?url='
               'https%3A%2F%2Fjobs.slugsys.test%2Finitech1%2Fabc-123&amp;trk=x">Apply on company site</a>')


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    """The app's browser is never reached: a test that needs a page says what it shows (`pages`)."""
    shown: dict[str, browser_code.Page] = {}
    asked: list[str] = []

    async def open_page(url: str) -> browser_code.Page:
        asked.append(url)
        if url not in shown:
            raise browser_code.BrowserUnavailable("no browser in tests")
        return shown[url]

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(browser_code, "open_page", open_page)
    monkeypatch.setattr(browser_code, "sleep", no_wait)
    yield type("Pages", (), {"shown": shown, "asked": asked})
    bp.code_running(OWNER, False)


def serve(monkeypatch, pages: dict[str, tuple[int, str]]) -> list[str]:
    """The polite fetch answers each address with (status, page); robots.txt is absent; any other is a 404."""
    seen: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(str(request.url))
        status, body = pages.get(str(request.url), (404, "Not found"))
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0))
    return seen


async def _role(n: int, **fields) -> uuid.UUID:
    async with db.session_factory()() as session:
        role = Opportunity(**{
            "owner": OWNER, "slug": f"role-{n}", "company": f"Initech {n}", "title": f"Programs Director {n}",
            "match_key": match_key(f"Initech {n}", f"Programs Director {n}"), "origin": "scout",
                "status": "second_impression",
            "found": date(2026, 10, n), "url": f"https://www.{ONE}/jobs/view/{1000 + n}", "sourced_at": None,
            **fields})
        session.add(role)
        await session.commit()
        return role.id


async def _sourced(n: int) -> uuid.UUID:
    """A Sourced role with no text: a `read` item at its employer's page."""
    link = CAREERS.format(n=n)
    return await _role(n, url=link, board_url=f"https://www.{ONE}/jobs/view/{1000 + n}", source_url=link,
                       source_kind="employer", sourced_at=NOW, analysis_state=analysis.NO_TEXT)


async def _get(model, key):
    async with db.session_factory()() as session:
        return await session.get(model, key)


async def _heartbeat(sid: str, now: datetime) -> dict:
    return await in_session(sid, lambda s: bd.heartbeat(s, OWNER, now))


# --- the extractors -------------------------------------------------------------------------------------------------


def test_an_embedded_job_posting_gives_its_whole_description_title_and_company():
    facts = browser_code.posting_facts(posting_page())
    assert (facts["title"], facts["company"]) == ("Programs Director 1", "Initech 1")
    assert browser_code.whole_text(facts["text"]) == facts["text"]
    assert "Five years of programs experience" in facts["text"] and "keep the books in order" in facts["text"]


def test_a_page_without_one_gives_nothing_and_nothing_is_guessed_from_its_markup():
    assert browser_code.posting_facts(BARE_PAGE) == {}
    assert browser_code.posting_facts(f"<html><body><h1>Programs Director 1</h1>{DESCRIPTION}</body></html>") == {}


def test_a_partial_job_posting_is_never_taken_as_whole():
    no_text = browser_code.posting_facts(posting_page(description=None))
    assert "text" not in no_text and no_text["title"] == "Programs Director 1"
    short = browser_code.posting_facts(posting_page(description="<p>Lead the programs team.</p>"))
    assert browser_code.whole_text(short.get("text")) is None
    cut = browser_code.posting_facts(posting_page(description=DESCRIPTION + "<p>... see more</p>"))
    assert browser_code.whole_text(cut.get("text")) is None


# --- code first: read -----------------------------------------------------------------------------------------------


async def test_while_code_runs_a_read_waits_for_it_and_its_embedded_posting_is_kept_with_no_session(js, monkeypatch):
    await browser_world(extra_sites=False)
    role_id = await _sourced(1)
    seen = serve(monkeypatch, {CAREERS.format(n=1): (200, posting_page())})
    bp.code_running(OWNER, True)
    got = await next_item(WORKER, NOW)
    assert got.get("item", {}).get("kind") != "read", "no session is given a read before the app's code tries it"
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    assert seen == [CAREERS.format(n=1)], "one polite fetch"
    role = await _get(Opportunity, role_id)
    assert "Five years of programs experience" in (role.description or "")
    row = (await items(kind="read"))[0]
    assert (row.state, row.last_result, row.leased_by) == ("done", "text", None)
    async with db.session_factory()() as session:
        runs = (await session.scalars(select(MissionRun))).all()
    assert all((r.started or {}).get("session_id") != CODE for r in runs), "the app's code opens no run"


async def test_a_page_that_answers_404_is_gone(js, monkeypatch):
    await browser_world(extra_sites=False)
    role_id = await _sourced(1)
    serve(monkeypatch, {CAREERS.format(n=1): (404, "Not found")})
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    row = (await items(kind="read"))[0]
    assert (row.state, row.last_result) == ("done", "gone")
    assert "HTTP 404" in (row.last_note or "") and (await _get(Opportunity, role_id)).status != "found"


async def test_a_page_that_draws_itself_is_read_in_the_apps_browser(js, monkeypatch, no_browser):
    await browser_world(extra_sites=False)
    role_id = await _sourced(1)
    serve(monkeypatch, {CAREERS.format(n=1): (200, BARE_PAGE)})
    no_browser.shown[CAREERS.format(n=1)] = browser_code.Page(CAREERS.format(n=1), 200, posting_page())
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    assert no_browser.asked == [CAREERS.format(n=1)]
    assert "Five years of programs experience" in ((await _get(Opportunity, role_id)).description or "")


async def test_another_jobs_posting_or_no_whole_text_goes_to_a_session_with_no_try_used(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _sourced(1)
    serve(monkeypatch, {CAREERS.format(n=1): (200, posting_page(title="Head Chef"))})
    bp.code_running(OWNER, True)
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    row = (await items(kind="read"))[0]
    assert (row.state, row.tries, row.code_at) == ("waiting", 0, NOW)
    got = await next_item(WORKER, NOW + timedelta(seconds=20))
    assert got["item"]["kind"] == "read", "now a session reads it"
    async with db.session_factory()() as session:
        attempt = (await session.scalars(select(BrowserAttempt).where(BrowserAttempt.session_id == CODE))).one()
    assert attempt.outcome == "no_result" and "passed it on" in attempt.note


async def test_a_refusal_is_a_stop_for_code_and_the_session_reads_it(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _sourced(1)
    serve(monkeypatch, {CAREERS.format(n=1): (403, "Forbidden")})
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    row = (await items(kind="read"))[0]
    assert row.state == "waiting" and row.code_at == NOW and row.tries == 0


async def test_a_retried_item_is_tried_by_code_first_again(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _sourced(1)
    serve(monkeypatch, {CAREERS.format(n=1): (200, BARE_PAGE)})
    await browser_code.code_once(OWNER, CODE, NOW)
    got = await next_item(WORKER, NOW + timedelta(seconds=20))
    await hand_back(WORKER, got["item"]["item_id"], "unreadable", now=NOW + timedelta(seconds=30),
                    note="only a preview shows")
    row = (await items(kind="read"))[0]
    assert row.tries == 1 and row.code_at is None


# --- code first: source on a board ----------------------------------------------------------------------------------


async def test_a_role_sourced_at_its_easy_apply_listing_is_not_queued_for_the_browser_again(js):
    """The listing is the source (the owner, 2026-10-22): once recorded it is neither sourced nor read again."""
    await browser_world(extra_sites=False)
    link = f"https://www.{ONE}/jobs/view/1001"
    await _role(1, url=link, board_url=link, source_url=link, source_kind="board", sourced_at=NOW, description=None,
                analysis_state=analysis.NO_TEXT)
    async with db.session_factory()() as session:
        await bq.refresh(session, OWNER, NOW)
        await session.commit()
    assert await items(kind="source") == [] and await items(kind="read") == []


async def test_a_role_is_followed_to_its_employers_posting_on_deck_or_with_no_text_and_never_between(js):
    """The owner, 2026-10-22: sourcing is the second pass's alone, so no stage before it queued a
    source item (roles in Followup needed and First Impression had one). Changed 2026-11-03 (PRD
    32.6): a role in Followup needed has no text, so nothing ranks it and it never goes on deck: many sat there for
    good. It is sourced too, last in the queue. A role in Landing Zone, Cleansed Data or First Impression holds its
    text, or is about to, and is still not sourced before it goes on deck."""
    await browser_world(extra_sites=False)
    for n, (stage, description) in enumerate((
            ("landing_zone", None), ("cleansed_data", None), ("first_impression", None),
            ("first_impression", POSTING_WORDS)), 1):
        await _role(n, status=stage, description=description)
    followup = await _role(8, status="followup_needed", description=None)
    on_deck = await _role(9, status="second_impression", description=POSTING_WORDS)
    async with db.session_factory()() as session:
        await bq.refresh(session, OWNER, NOW)
        await session.commit()
    queued = {r.role_id: r.priority for r in await items(kind="source")}
    assert queued == {on_deck: 1, followup: bp.BACKLOG}, "on deck, or in Followup needed behind it: no stage between"


async def test_a_source_on_a_job_board_the_owner_lists_is_left_to_a_session_with_no_fetch(js, monkeypatch):
    """Changed 2026-10-22 (the code-first source now looks at every source not on a board it knows by name): code
    passes a listing on the owner's own job site on at once, reading nothing."""
    await browser_world(extra_sites=False)
    await _role(1)  # on the made-up board ONE
    seen = serve(monkeypatch, {})
    bp.code_running(OWNER, True)
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    assert seen == [], "a job board's page is never read by the app's fetch"
    assert (await next_item(WORKER, NOW + timedelta(seconds=20)))["item"]["kind"] == "source"


# --- code first: a page's own text, a source at the employer's page, what is kept of a domain -----------------------


def static_page(title: str = "Programs Director 1", words: str = POSTING_WORDS, extra: str = "") -> str:
    """A posting page that embeds no JobPosting: its text is in the HTML, under its title, with chrome around it."""
    return (f"<html><head><title>{title} | Northwind</title></head><body><nav>Home Careers Log in</nav>"
            f"<main><h1>{title}</h1><p>{words.replace(chr(10), '<br>')}</p>{extra}</main>"
            "<footer>Cookies Privacy</footer></body></html>")


def test_a_page_with_no_embedded_posting_gives_its_main_text_when_its_heading_is_this_job():
    job = browser_code.Job("i", "read", CAREERS.format(n=1), "northwind1.test", "Programs Director 1", False)
    got = browser_code._from_page(static_page(), job)
    assert got is not None and "Five years of programs experience" in got["text"] and "Log in" not in got["text"]
    assert browser_code._from_page(static_page(title="Head Chef"), job) is None, "another job's page"


def test_a_heading_that_carries_the_job_number_after_the_title_is_still_this_job():
    job = browser_code.Job("i", "read", CAREERS.format(n=1), "northwind1.test", "Programs Director 1", False)
    page = static_page().replace("Programs Director 1 | Northwind", "Programs Director 1 - United States - 4401")
    page = page.replace("<h1>Programs Director 1</h1>", "<h1>Northwind</h1>")
    assert browser_code._from_page(page, job) is not None


def test_a_page_that_lists_many_jobs_is_not_this_jobs_text():
    job = browser_code.Job("i", "read", CAREERS.format(n=1), "northwind1.test", "Programs Director 1", False)
    listing = static_page(title="Open roles", extra="<ul><li>Programs Director 1</li><li>Head Chef</li></ul>")
    assert browser_code._from_page(listing, job) is None


def test_a_job_added_by_its_link_is_not_read_from_the_pages_text_alone():
    job = browser_code.Job("i", "read", CAREERS.format(n=1), "northwind1.test", None, True)
    assert browser_code._from_page(static_page(), job) is None, "its title and company must come with the text"


async def test_a_pages_main_text_is_kept_with_the_page_as_the_raw_copy_and_no_session(js, monkeypatch):
    await browser_world(extra_sites=False)
    role_id = await _sourced(1)
    async with db.session_factory()() as session:  # the role's listing, found before its text was read
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id="a-1", company="Initech 1",
            title="Programs Director 1", match_key=match_key("Initech 1", "Programs Director 1"),
            opportunity_id=role_id, url=CAREERS.format(n=1), first_seen=NOW, last_seen=NOW, links=[]))
        await session.commit()
    page = static_page()
    serve(monkeypatch, {CAREERS.format(n=1): (200, page)})
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    assert "Five years of programs experience" in ((await _get(Opportunity, role_id)).description or "")
    assert (await items(kind="read"))[0].state == "done"
    async with db.session_factory()() as session:
        raws = (await session.scalars(select(RawPosting))).all()
        pace = (await session.scalars(select(BrowserDomain).where(BrowserDomain.domain == "northwind1.test"))).one()
    assert [(r.reader, r.content_type) for r in raws] == [("code:page", "text/html")]
    assert inflate(raws[0].body) == page.encode()
    assert pace.page_read == "text"


async def test_a_role_is_sourced_at_its_employers_own_page_when_the_page_holds_the_whole_posting(js, monkeypatch):
    await browser_world(extra_sites=False)
    link = CAREERS.format(n=2)
    role_id = await _role(2, url=link, board_url=None)
    serve(monkeypatch, {link: (200, static_page(title="Programs Director 2"))})
    async with db.session_factory()() as session:
        await bq.refresh(session, OWNER, NOW)
        await session.commit()
    row = (await items(kind="source"))[0]
    assert row.link == link and takeable.code_first(row)
    bp.code_running(OWNER, True)
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    role = await _get(Opportunity, role_id)
    assert (role.source_url, role.source_kind) == (link, "employer")
    assert "Five years of programs experience" in (role.description or "")
    assert (await items(kind="source"))[0].last_result == "found"


async def test_a_page_with_no_text_in_its_html_is_noted_for_its_domain_and_the_next_read_skips_the_fetch(
        js, monkeypatch, no_browser):
    await browser_world(extra_sites=False)
    first, second = CAREERS.format(n=1), "https://careers.northwind1.test/jobs/3"  # one domain, two postings
    await _sourced(1)
    await _role(3, url=second, board_url=None, source_url=second, source_kind="employer", sourced_at=NOW,
                analysis_state=analysis.NO_TEXT)
    seen = serve(monkeypatch, {first: (200, BARE_PAGE), second: (200, BARE_PAGE)})
    for link, n in ((first, 1), (second, 3)):
        no_browser.shown[link] = browser_code.Page(link, 200, posting_page(title=f"Programs Director {n}"))
    bp.code_running(OWNER, True)
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    assert len(seen) == 1 and no_browser.asked == seen, "one fetch, then the browser"
    domain = (await items(kind="read"))[0].domain
    async with db.session_factory()() as session:
        assert await bp.shell_domain(session, OWNER, domain, NOW) is True
        assert await bp.shell_domain(session, OWNER, domain, NOW + timedelta(days=8)) is False, "looked at again"
    assert await browser_code.code_once(OWNER, CODE, NOW + timedelta(seconds=30)) is True
    assert len(seen) == 1, "no second fetch on a domain whose pages draw themselves"
    assert len(no_browser.asked) == 2, "it went straight to the browser"


async def test_a_refusal_is_noted_for_its_domain_and_the_item_goes_to_a_session(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _sourced(1)
    serve(monkeypatch, {CAREERS.format(n=1): (403, "Forbidden")})
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    async with db.session_factory()() as session:
        pace = (await session.scalars(select(BrowserDomain))).one()
        assert (pace.page_read, await bp.shell_domain(session, OWNER, pace.domain, NOW)) == ("refused", False)


# --- the code checker is seen and named -------------------------------------------------------------------------------


# --- the digest -----------------------------------------------------------------------------------------------------


async def test_the_digests_whole_text_can_be_handed_back_by_reference_and_still_passes_the_whole_posting_check(
        js, monkeypatch, no_browser):
    await browser_world(extra_sites=False)
    await _sourced(1)
    serve(monkeypatch, {CAREERS.format(n=1): (200, posting_page(title="Head Chef"))})
    bp.code_running(OWNER, True)
    await browser_code.code_once(OWNER, CODE, NOW)  # code passes it on to a session
    got = await next_item(WORKER, NOW + timedelta(seconds=20))
    item_id = got["item"]["item_id"]
    link = got["item"]["link"]

    async def by_reference(url=None):
        return await in_session(WORKER, lambda s: browser_code.digest_text(s, OWNER, item_id, url))

    with pytest.raises(br.BrowserError, match="no whole text from browser.page_digest"):
        await by_reference()
    no_browser.shown[link] = browser_code.Page(link, 200, posting_page())
    digested = await in_session(WORKER, lambda s: browser_code.page_digest(s, OWNER, link, NOW))
    assert digested["text_whole"] is True
    assert await by_reference() == digested["text"], "the text the page extract captured, read by code"
    with pytest.raises(br.BrowserError, match="no whole text from browser.page_digest"):
        await by_reference("https://careers.other.test/jobs/9")
    with pytest.raises(br.BrowserError, match="not the item you hold"):
        await in_session(WORKER, lambda s: browser_code.digest_text(s, OWNER, "other-item"))
    await hand_back(WORKER, item_id, "text", now=NOW + timedelta(seconds=30), text=await by_reference())
    assert (await items(kind="read"))[0].state == "done"


class _Tools:
    """A DevTools stand-in: the page is complete at once and draws its text after `draws_after` asks."""

    def __init__(self, draws_after: int) -> None:
        self.asked, self.draws_after = 0, draws_after
        self.read = False

    async def call(self, method, params=None, session_id=None):
        expression = (params or {}).get("expression", "")
        if expression == browser_code.READY:
            return {"result": {"value": "complete"}}
        if expression == browser_code._TEXT_LENGTH:
            self.asked += 1
            return {"result": {"value": 900 if self.asked > self.draws_after else 12}}
        self.read = self.asked > self.draws_after
        return {"result": {"value": json.dumps({"url": "https://x.test/", "status": 200, "html": "<p>x</p>"})}}


async def test_a_page_that_is_complete_but_has_not_drawn_is_waited_for_before_it_is_read(monkeypatch):
    monkeypatch.setattr(browser_code, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(browser_code, "FILL_SECONDS", 0.2)
    monkeypatch.setattr(browser_code, "FILL_EVERY", 0.02)
    late = _Tools(draws_after=3)
    await browser_code._read_tab(late, "sid", "https://x.test/")
    assert late.read is True and late.asked == 4, "it asked again until the text showed, then read"
    empty = _Tools(draws_after=10**6)
    await browser_code._read_tab(empty, "sid", "https://x.test/")
    assert 3 <= empty.asked <= 12, "and gives up after FILL_SECONDS"


# --- the time box per item ------------------------------------------------------------------------------------------


async def test_an_item_past_its_time_box_is_handed_back_as_timed_out_and_tried_again(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    got = await next_item(WORKER, NOW)
    assert got["item"]["timebox_minutes"] == 5
    assert (await _heartbeat(WORKER, NOW + timedelta(minutes=4)))["holding"] is True
    late = await _heartbeat(WORKER, NOW + timedelta(minutes=6))
    assert late["holding"] is False and "time box" in late["next"]
    row = (await items(kind="source"))[0]
    assert (row.state, row.tries, row.last_result, row.last_note) == ("waiting", 1, "not_found", "timed out")
    assert row.next_at == NOW + timedelta(minutes=6) + bp.RETRY_AFTER
    with pytest.raises(br.BrowserError, match="time box"):
        await hand_back(WORKER, got["item"]["item_id"], "not_found", now=NOW + timedelta(minutes=7), note="none here")
    when = NOW + timedelta(minutes=6)
    for _ in range(2):  # the next two daily tries time out too
        when += bp.RETRY_AFTER + timedelta(minutes=1)
        again = await next_item(WORKER_B, when)
        assert again["item"]["item_id"] == got["item"]["item_id"], "retried, nothing dropped"
        when += timedelta(minutes=6)
        await _heartbeat(WORKER_B, when)
    row = (await items(kind="source"))[0]
    assert row.state == "stuck" and (await _get(Opportunity, role_id)).browser_note == "timed out"
    async with db.session_factory()() as session:
        outcomes = (await session.scalars(select(BrowserAttempt.note).where(
            BrowserAttempt.outcome == "not_found"))).all()
    assert outcomes == ["timed out"] * 3


async def test_a_read_times_out_as_unreadable_and_a_scan_ends_as_it_stands(js):
    await browser_world(extra_sites=False)
    await _sourced(1)
    got = await next_item(WORKER, NOW)
    assert got["item"]["kind"] == "read"
    await _heartbeat(WORKER, NOW + timedelta(minutes=6))
    assert (await items(kind="read"))[0].last_result == "unreadable"
    assert (await next_item(WORKER_B, NOW + timedelta(seconds=20)))["item"]["kind"] == "scan"
    handed = await scan_hand_in([browser_card(7)], sid=WORKER_B, now=NOW + timedelta(minutes=1))
    assert handed["ok"], handed
    await _heartbeat(WORKER_B, NOW + timedelta(minutes=9))
    assert (await _heartbeat(WORKER_B, NOW + timedelta(minutes=14)))["holding"] is True, "a scan has 15 minutes"
    await _heartbeat(WORKER_B, NOW + timedelta(minutes=16))
    scan = (await items(kind="scan", target=ONE))[0]
    assert (scan.state, scan.last_note) == ("done", "timed out")
    async with db.session_factory()() as session:
        assert (await session.scalars(select(JobPosting))).all(), "what the scan handed in is kept"


async def test_a_result_handed_back_before_anything_timed_the_item_out_is_still_taken(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    got = await next_item(WORKER, NOW)
    reply = await hand_back(WORKER, got["item"]["item_id"], "found", now=NOW + timedelta(minutes=7), url=EMPLOYER)
    assert reply["item"] == "done" and (await _get(Opportunity, role_id)).source_url == EMPLOYER


async def test_a_map_has_no_time_box(js):
    await browser_world(extra_sites=False, learned=False)
    got = await next_item(PATHFINDER, NOW)
    assert got["item"]["kind"] == "map" and "timebox_minutes" not in got["item"]
    for minute in range(9, 91, 9):
        assert (await _heartbeat(PATHFINDER, NOW + timedelta(minutes=minute)))["holding"] is True
    row = (await items(kind="map"))[0]
    assert row.state == "leased" and row.due_by is None


async def test_the_lease_running_out_is_still_no_result_not_a_time_out(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    other = await next_item(WORKER_B, NOW + timedelta(minutes=11))
    assert other["item"]["item_id"] == got["item"]["item_id"]
    async with db.session_factory()() as session:
        row = await session.get(BrowserItem, uuid.UUID(got["item"]["item_id"]))
        assert row is not None and row.tries == 0


async def test_a_timed_out_scan_comes_back_on_the_sites_usual_cadence(js):
    await browser_world(extra_sites=False)
    assert (await next_item(WORKER, NOW))["item"]["kind"] == "scan"
    await _heartbeat(WORKER, NOW + timedelta(minutes=9))
    await _heartbeat(WORKER, NOW + timedelta(minutes=16))
    scan = (await items(kind="scan", target=ONE))[0]
    assert (scan.state, scan.done_at) == ("done", NOW + timedelta(minutes=16))
    back = NOW + timedelta(minutes=16) + bq.SCAN_EVERY
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, back))
    assert (await items(kind="scan", target=ONE))[0].state == "waiting", "no later than a finished scan's next one"


def test_the_bridge_port_the_launcher_wrote_wins(tmp_path, monkeypatch):
    from app.settings import BROWSER_PORT_FILE, DATA_DIR_VARIABLE, JobSearchSettings

    monkeypatch.setenv(DATA_DIR_VARIABLE, str(tmp_path))
    monkeypatch.delenv("SCOUT_BROWSER_PORT", raising=False)
    monkeypatch.delenv("JOBSEARCH_BROWSER_PORT", raising=False)
    assert JobSearchSettings().bridge_port() == 9333
    (tmp_path / BROWSER_PORT_FILE).write_text("51234\n", encoding="utf-8")
    assert JobSearchSettings().bridge_port() == 51234, "a free port the launcher took reaches the app"
    (tmp_path / BROWSER_PORT_FILE).write_text("not a port", encoding="utf-8")
    monkeypatch.setenv("SCOUT_BROWSER_PORT", "9444")
    assert JobSearchSettings().bridge_port() == 9444


async def test_the_digest_holds_no_connection_while_it_waits_for_the_pace_or_the_page(js, monkeypatch, no_browser):
    await browser_world(extra_sites=False)
    await _role(1)
    link = CAREERS.format(n=1)
    no_browser.shown[link] = browser_code.Page(link, 200, posting_page(body=SAFETY_LINK))
    await next_item(WORKER, NOW)
    held: list[tuple[str, int]] = []
    shown_page = browser_code.open_page

    async def waits(_seconds: float) -> None:
        held.append(("pace", db.engine().pool.checkedout()))

    async def loads(url: str):
        held.append(("page", db.engine().pool.checkedout()))
        return await shown_page(url)

    monkeypatch.setattr(browser_code, "sleep", waits)
    monkeypatch.setattr(browser_code, "open_page", loads)
    monkeypatch.setattr(bp, "gap", lambda: timedelta(seconds=9))
    await in_session(WORKER, lambda s: browser_code.page_digest(s, OWNER, link, NOW))
    await in_session(WORKER, lambda s: browser_code.page_digest(s, OWNER, link, NOW))  # the second must wait its turn
    assert ("pace", 0) in held and ("page", 0) in held and all(n == 0 for _, n in held), held


class _Wire:
    """The writer half of a connection to a bridge that takes everything and says nothing."""

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


async def test_a_bridge_that_never_answers_the_handshake_is_unavailable_not_waited_for(monkeypatch):
    """A bridge stuck on an open held every code job's connection for its whole 120 s and every
    digest for 300 s, and each item went on to a session or failed with "it took too long"."""
    import asyncio

    async def silent(host, port):
        return asyncio.StreamReader(), _Wire()

    monkeypatch.setattr(asyncio, "open_connection", silent)
    monkeypatch.setattr(browser_code, "OPEN_SECONDS", 0.05)
    with pytest.raises(browser_code.BrowserUnavailable, match="did not answer"):
        await browser_code._DevTools(1).open()


async def test_go_window_that_cannot_open_lets_go_of_its_connection_to_the_bridge(monkeypatch):
    """A connection left open on the bridge keeps the idle
    check from closing Chrome. A window whose open failed held its socket until the process ended."""
    import asyncio

    ended: list[bool] = []

    class Wire(_Wire):
        def close(self) -> None:
            ended.append(True)

    async def silent(host, port):
        return asyncio.StreamReader(), Wire()

    monkeypatch.setattr(asyncio, "open_connection", silent)
    monkeypatch.setattr(browser_code, "OPEN_SECONDS", 0.05)
    with pytest.raises(browser_code.BrowserUnavailable):
        await browser_code.GoWindow().open()
    assert ended == [True]


async def test_closing_a_devtools_connection_sends_a_websocket_close_frame_first():
    """The owner, 2026-11-14: a connection closed with no close frame stayed "connected" on the bridge, so the app's
    Chrome never closed. A local server stands for the bridge and records what arrives after the handshake."""
    import asyncio

    got: list[bytes] = []
    done = asyncio.Event()

    async def bridge(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
        await writer.drain()
        got.append(await reader.read())  # until the client hangs up
        writer.close()
        done.set()

    server = await asyncio.start_server(bridge, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        tools = browser_code._DevTools(port)
        await tools.open()
        await tools.close()
        async with asyncio.timeout(5):
            await done.wait()
    finally:
        server.close()
    frame = got[0]
    assert frame[0] == 0x88, "a final close frame"
    assert frame[1] == 0x80, "masked, no payload"
    assert len(frame) == 6


async def test_a_command_the_bridge_never_answers_is_unavailable_not_waited_for(monkeypatch):
    import asyncio

    tools = browser_code._DevTools(1)
    tools.reader, tools.writer = asyncio.StreamReader(), _Wire()  # type: ignore[assignment]
    monkeypatch.setattr(browser_code, "ANSWER_SECONDS", 0.05)
    with pytest.raises(browser_code.BrowserUnavailable, match="Target.createTarget: the browser did not answer"):
        await tools.call("Target.createTarget", browser_code.own_tab("about:blank"))
