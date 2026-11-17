"""A role in Followup needed is sourced by code only, and a role code cannot get the original posting of is closed as
noise (the owner, 2026-11-13; docs/PRD.md 32.6). Every name, address and sentence is made up, and no test reaches the
network or a browser."""


import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import queue as bq
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.postings import noise
from app.jobsearch.research import analysis
from app.models.work import TrailEvent
from app.trail import service as trail
from tests.builders import browser_world, in_session, items
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.support import DEFAULT_OWNER as OWNER

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out", "made_up_boards")


NOW = datetime(2026, 11, 13, 22, 0, tzinfo=UTC)


CODE = "code-test0002"


CAREERS = "https://careers.northwind{n}.test/jobs/{n}"


BOARDTWO = "https://www.boardtwo.test/viewjob?jk=abc123def45{n}"


BOARDTHREE = "https://www.boardthree.test/job-detail/0b3a1c64-1f1e-4c2a-9a0e-2a7d6f1c9e1{n}"


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    """The app's browser is never reached: a test that needs a page says what it shows."""
    shown: dict[str, browser_code.Page] = {}

    async def open_page(url: str) -> browser_code.Page:
        if url not in shown:
            raise browser_code.BrowserUnavailable("no browser in tests")
        return shown[url]

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(browser_code, "open_page", open_page)
    monkeypatch.setattr(browser_code, "sleep", no_wait)
    yield shown
    bp.code_running(OWNER, False)


def serve(monkeypatch, pages: dict[str, tuple[int, str]]) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        status, body = pages.get(str(request.url), (404, "Not found"))
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0))


async def _role(n: int, **fields) -> uuid.UUID:
    async with db.session_factory()() as session:
        link = fields.pop("url", CAREERS.format(n=n))
        role = Opportunity(**{
            "owner": OWNER, "slug": f"role-{n}", "company": f"Initech {n}", "title": f"Programs Director {n}",
            "match_key": match_key(f"Initech {n}", f"Programs Director {n}"), "origin": "scout",
            "status": "followup_needed", "found": date(2026, 11, 10), "url": link, "board_url": link,
            "sourced_at": None, **fields})
        session.add(role)
        await session.commit()
        return role.id


async def _get(role_id: uuid.UUID) -> Opportunity:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        assert role is not None
        return role


async def _lines(role_id: uuid.UUID, event: str = noise.EVENT) -> list[TrailEvent]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == role_id, TrailEvent.event == event))).all())


async def _queued() -> None:
    await in_session(CODE, lambda s: bq.refresh(s, OWNER, NOW))


async def test_a_role_on_a_board_code_has_no_route_to_is_closed_as_noise_at_once(js):
    """Old: the item went to a session. New: it is the code's alone and the answer is certain (no way to read the
    board's listing), so it is stuck at once and the role is closed as noise, with its reason and a history line."""
    await browser_world(extra_sites=False)
    role_id = await _role(1, url=BOARDTWO.format(n=1))
    await _queued()
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    [item] = await items(kind="source")
    assert (item.state, item.last_result, item.code_only) == ("stuck", "not_found", True)
    role = await _get(role_id)
    assert (role.status, role.ended_reason, role.browser_note) == ("gone_quiet", "noise", None)
    [line] = await _lines(role_id)
    assert "could not be read by code" in (line.detail or "") and line.sentence.startswith("Closed as noise")
    assert line.who == "Code checker"


async def test_a_page_that_is_no_whole_posting_is_tried_three_days_and_then_the_role_is_closed_as_noise(
        js, monkeypatch, no_browser):
    await browser_world(extra_sites=False)
    link = CAREERS.format(n=2)
    page = "<html><body><p>Careers at Northwind. Browse our openings.</p></body></html>"
    serve(monkeypatch, {link: (200, page)})
    no_browser[link] = browser_code.Page(link, 200, page)  # what the app's Chrome shows, if the fetch shows no text
    role_id = await _role(2)
    await _queued()
    for day in range(3):
        assert await browser_code.code_once(OWNER, CODE, NOW + timedelta(days=day)) is True
        [item] = await items(kind="source")
        assert (item.tries, item.last_result) == (day + 1, "not_found"), (item.state, item.last_note, item.next_at)
        assert (await _get(role_id)).status == ("gone_quiet" if day == 2 else "followup_needed")
    assert (await _get(role_id)).ended_reason == "noise"
    assert len(await _lines(role_id)) == 1


async def test_a_try_the_code_could_not_make_uses_no_try_and_closes_nothing(js):
    """Chrome would not open (no browser in the tests): the item rests as a listing's does; the role stays."""
    await browser_world(extra_sites=False)
    role_id = await _role(3, url=BOARDTHREE.format(n=3))
    await _queued()
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    [item] = await items(kind="source")
    assert (item.state, item.tries) == ("waiting", 0)
    assert (await _get(role_id)).status == "followup_needed"


@pytest.mark.parametrize("status, stuck", [(429, False), (403, True)])
async def test_a_rest_or_a_stop_of_the_host_never_closes_the_role(js, no_browser, status, stuck):
    await browser_world(extra_sites=False)
    link = BOARDTHREE.format(n=4)
    role_id = await _role(4, url=link)
    await _queued()
    no_browser[link] = browser_code.Page(link, status, "<html><body>Request denied</body></html>")
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    [item] = await items(kind="source")
    assert item.last_result == "blocked" and item.state == ("stuck" if stuck else "waiting")
    role = await _get(role_id)
    assert role.status == "followup_needed" and role.ended_reason is None


async def _close(role_id: uuid.UUID) -> bool:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        closed = await noise.close(session, role, "no address came of it")
        await session.commit()
        return closed


async def test_a_role_in_followup_needed_is_closed_and_kept_with_its_reason(js):
    await browser_world(extra_sites=False)
    role_id = await _role(6)
    assert await _close(role_id) is True
    role = await _get(role_id)
    assert (role.status, role.ended_reason) == ("gone_quiet", "noise")
    assert [line.event for line in await _lines(role_id)] == ["noise_closed"]


async def test_the_owners_own_roles_are_never_closed_as_noise(js):
    await browser_world(extra_sites=False)
    starred = await _role(7, favorited_at=NOW)
    asked = await _role(8, ready_asked_at=NOW)
    tracker = await _role(9, origin="tracker")
    added = await _role(10, origin="owner")
    opened = await _role(11, go_opened_at=NOW)
    skipped = await _role(12, go_skip="its page was gone")
    researching = await _role(13, analysis={analysis.ASKED_BY: analysis.ASKED_BY_OWNER})
    applied = await _role(14, status="applied")
    packeted = await _role(15)
    placed = await _role(16)
    asked_packet = await _role(17)
    async with db.session_factory()() as session:
        session.add(Packet(owner=OWNER, role_id=packeted, version=1, status="ready", created_at=NOW))
        for role_id, event in ((placed, "role_moved"), (asked_packet, "packet_asked")):
            await trail.record(session, OWNER, event=event, sentence="You did.", actor="person", tier="result",
                               link=("opportunity", role_id))
        await session.commit()
    for role_id in (starred, asked, tracker, added, opened, skipped, researching, applied, packeted, placed,
                    asked_packet):
        assert await _close(role_id) is False, role_id
        assert (await _get(role_id)).ended_reason is None
    assert await _lines(starred) == []
