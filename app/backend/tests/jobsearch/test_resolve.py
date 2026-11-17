"""Which job system each employer posts on (docs/PRD.md 28.1 and 28.2): read from links with no network, then asked of
the open feeds politely (robots.txt, a refusal is a stop), accepted only when the answer names the same employer. All
network is a mock transport. Synthetic data only."""


import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import resolve
from app.jobsearch.fetch.polite import Fetcher, Refused
from app.jobsearch.imports import tracker
from app.jobsearch.models import Opportunity
from tests.support import OWNER, tracker_row, tracker_workbook


async def _roles(rows):
    """Import roles, then give them links, as the scout and the tracker would have."""
    async with db.session_factory()() as session:
        await tracker.import_tracker(session, OWNER, tracker_workbook([tracker_row(c, t) for c, t, _u in rows]))
        await employers.link_roles(session, OWNER)
        for company, title, link in rows:
            role = await session.scalar(select(Opportunity).where(
                Opportunity.company == company, Opportunity.title == title))
            role.url = link
        await session.commit()


async def _employer(session, name):
    return await employers.find_or_make(session, OWNER, name)


async def test_robots_txt_and_a_refusal_are_respected():
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if request.url.path.startswith("/blocked"):
            return httpx.Response(403)
        return httpx.Response(200, json={"ok": True})
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)
    with pytest.raises(Refused, match="robots.txt"):
        await fetch.get("https://site.example/private/jobs")
    assert (await fetch.get("https://site.example/jobs")).json() == {"ok": True}
    with pytest.raises(Refused, match="403"):
        await fetch.get("https://site.example/blocked")
    with pytest.raises(Refused, match="refused an earlier request"):
        await fetch.get("https://site.example/jobs"), "after a refusal the host is left alone for the run"


async def test_an_employer_the_scout_answered_with_its_own_careers_page_is_not_offered_again_as_time_passes(js):
    """Employers were offered again and again because an answer on the employer's own site (the careers
    link the scout was given, sent back) named no job system and left the employer on the list. Since 2026-11-05 no
    timer brings it back either (docs/PRD.md 28.3): only a failed read of its site does."""
    from datetime import timedelta

    from app.jobsearch.doors import tools
    from app.timeutil import utc_now

    await _roles([("Acme", "Director of Programs", "https://www.boardone.test/jobs/view/1")])
    async with db.session_factory()() as session:
        acme = await employers.find(session, OWNER, "Acme")
        acme.careers_url = "https://careers.acme.example/jobs"
        assert [e["company"] for e in (await tools.boards_reply(session, OWNER))["employers"]] == ["Acme"]
        said = await tools.board_found_reply(session, OWNER, "Acme", "https://careers.acme.example/jobs")
        assert said["system"] == "its own site"
        assert (await tools.boards_reply(session, OWNER))["employers"] == [], "answered, so not offered again"
        for days in (31, 400):
            acme.sources = {**acme.sources, tools.ASKED: {"at": (utc_now() - timedelta(days=days)).isoformat()}}
            assert (await tools.boards_reply(session, OWNER))["employers"] == [], f"{days} days on, nothing has failed"


@pytest.mark.parametrize("link", [
    "http://127.0.0.1:8430/api/health", "http://169.254.169.254/latest/meta-data", "http://localhost/jobs",
    "http://0x7f.1/", "http://2130706433/", "http://[::ffff:7f00:1]/", "http://100.64.0.1/", "http://intranet/jobs",
    "https://user:pass@acme.example/jobs", "ftp://acme.example/jobs"])
async def test_a_board_or_careers_page_that_is_not_a_public_address_is_never_kept(js, link):
    from app.jobsearch.doors import tools

    await _roles([("Acme", "Director of Programs", "https://www.boardone.test/jobs/view/1")])
    async with db.session_factory()() as session:
        with pytest.raises(Exception, match="whole address"):
            await tools.board_found_reply(session, OWNER, "Acme", link)
        acme = await employers.find(session, OWNER, "Acme")
        assert acme.careers_url is None and acme.ats is None and acme.board_url is None
        assert employers.learn_careers_url(acme, link, "the analyst") is False
        assert acme.careers_url is None
        assert employers.learn_careers_url(acme, "https://careers.acme.example/jobs", "the analyst") is True
        assert acme.careers_url == "https://careers.acme.example/jobs"


async def test_a_careers_page_the_server_is_refused_is_noted_as_refused_said_who_refused_and_not_asked_again(js):
    """A 403 and a robots.txt that asks not to read the page are the site saying no (docs/PRD.md 28.5): the employer is
    noted as refused, the caller is told which employers and who refused, and the page is a stop, not a retry."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            if request.url.host == "rules.example":
                return httpx.Response(200, text="User-agent: *\nDisallow: /jobs\n")
            return httpx.Response(404)
        if request.url.host == "blocked.example":
            return httpx.Response(403)
        return httpx.Response(200, text="<html>Email us your resume.</html>")
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)
    async with db.session_factory()() as session:
        blocked = await _employer(session, "Blocked Co")
        blocked.careers_url = "https://blocked.example/careers"
        rules = await _employer(session, "Rules Co")
        rules.careers_url = "https://rules.example/jobs"
        plain = await _employer(session, "Plain Co")
        plain.careers_url = "https://plain.example/careers"
        result = await resolve.from_pages(session, OWNER, fetch)
        assert result == {"read": 3, "found": {}, "refused": {blocked.id: resolve.BY_THE_SITE,
                                                              rules.id: resolve.BY_ROBOTS}}
        assert (blocked.fetch_kind, rules.fetch_kind, plain.fetch_kind) == ("refused", "refused", "careers_page")
        assert blocked.fetch_note == "careers page: couldn't be read (blocked.example refused the request (403))"
        assert rules.fetch_note.startswith(
            "careers page: couldn't be read (robots.txt on rules.example asks not to read /jobs")
        assert plain.fetch_note == "careers page: names no job system the app knows"
        asked = len(seen)
        assert await resolve.from_pages(session, OWNER, fetch) == {"read": 0, "found": {}, "refused": {}}
        assert len(seen) == asked, "a refusal is a stop: nothing is asked of the site again"


async def test_an_employer_the_old_night_noted_as_unread_is_read_again_and_one_that_named_no_system_is_not(js):
    """Before refusals had their own kind a page that could not be read, a refusal included, was a `careers_page` with
    this note: it is read again once (the refusal is then noted as one), and a page that named no system stays read."""
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403) if request.url.host == "blocked.example" else httpx.Response(200, text="<p>Hi</p>")
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)
    async with db.session_factory()() as session:
        old = await _employer(session, "Old Co")
        old.careers_url = "https://blocked.example/careers"
        old.fetch_kind = "careers_page"
        old.fetch_note = "careers page: couldn't be read (blocked.example refused the request (403))"
        quiet = await _employer(session, "Quiet Co")
        quiet.careers_url = "https://quiet.example/careers"
        quiet.fetch_kind, quiet.fetch_note = "careers_page", "careers page: names no job system the app knows"
        result = await resolve.from_pages(session, OWNER, fetch)
        assert result == {"read": 1, "found": {}, "refused": {old.id: resolve.BY_THE_SITE}}
        assert old.fetch_kind == "refused" and quiet.fetch_kind == "careers_page"
        assert resolve.page_settled(old) and resolve.page_settled(quiet)
