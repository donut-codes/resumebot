"""The Inbox keeps only what the owner can act on: "Added link" is no employer, a site's switch-off is not announced
when nothing can be done about it, and the app looks again at a site it switched off. Every name and host is made up."""

from datetime import timedelta

import httpx
from sqlalchemy import select

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import ADDED_LINK
from app.models.work import Task
from app.sites import read_probe
from app.timeutil import utc_now
from tests.employer_builders import make_employer, make_site
from tests.support import OWNER, load_profile


def _fetcher(handle) -> Fetcher:
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True), per_host_seconds=0)


async def test_added_link_is_never_an_employer_found_made_or_asked_about_for_a_merge(js):
    async with db.session_factory()() as session:
        assert await employers.find_or_make(session, OWNER, ADDED_LINK) is None
        assert await employers.find_or_make(session, OWNER, "added  LINK") is None
        # a record that got the name before this rule is not a candidate for any merge, whatever site it shares
        await make_employer(session, ADDED_LINK, website="https://www.example-corp.test")
        await make_employer(session, "Initech Health Access Initiative", website="https://example-corp.test/about")
        assert await employers.find(session, OWNER, ADDED_LINK) is None
        assert await employer_pairs.candidates(session, OWNER) == []
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
        assert (await session.scalars(select(Task))).all() == []


async def test_a_site_the_app_gave_up_is_looked_at_again_monthly_and_switched_on_when_it_can_be_read(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        sites = {}
        for name, age in (("open", 40), ("closed", 40), ("recent", 3)):
            employer = await make_employer(session, f"Co {name}")
            site = make_site(employer, f"https://{name}.example.test/jobs", on=False, off_by="app",
                             off_note="it asks not to be read", off_at=utc_now() - timedelta(days=age))
            session.add(site)
            sites[name] = site
        await session.flush()

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                body = "User-agent: *\nDisallow: /" if request.url.host == "closed.example.test" else ""
                return httpx.Response(200, text=body)
            return httpx.Response(200, html="<html><title>Jobs</title><body>" + "Open roles. " * 200 + "</body></html>")

        fetch = _fetcher(handle)
        try:
            assert await read_probe.recheck_unreadable(session, OWNER, fetch) == 1
        finally:
            await fetch.close()
        assert (sites["open"].on, sites["open"].off_by, sites["open"].off_note, sites["open"].off_at) == (
            True, None, None, None)
        assert (sites["closed"].on, sites["closed"].off_by, sites["closed"].off_note) == (
            False, "app", "it asks not to be read")
        assert utc_now() - sites["closed"].off_at < timedelta(minutes=1), "looked at again a month from now"
        assert (sites["recent"].on, sites["recent"].off_at < utc_now() - timedelta(days=2)) == (False, True)
        assert (await session.scalars(select(Task))).all() == []
