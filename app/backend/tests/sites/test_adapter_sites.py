"""A site whose job system the app already reads in code (`adapters.ADAPTERS`) is read by the app, never the owner's
browser: the server's test of an `auto` site says so without fetching, a refusal or a "needs the browser" finding cannot
hand it over. A system with no reader still goes to the browser. Every employer and host is made up
(example.test); no network."""

import uuid
from datetime import UTC, datetime

import pytest

from app import db
from app.jobsearch import adapters, ats
from app.jobsearch.models import Employer
from app.sites import read_probe, registry, where
from app.sites.models import AUTO, BY_APP, BY_BROWSER, Site
from tests.support import EXAMPLE_SYSTEM, OWNER

#: A job system the registry holds that the app has no reader for in code.
NO_READER = next(
    r.key for r in registry.current().runbooks if r.kind == "job_system" and r.key not in adapters.ADAPTERS)


async def _site(system: str | None, url: str, read_by: str = AUTO) -> uuid.UUID:
    async with db.session_factory()() as session:
        employer = Employer(owner=OWNER, key=f"e-{uuid.uuid4().hex[:6]}", name="Umber", ats=system, board={})
        session.add(employer)
        await session.flush()
        site = Site(owner=OWNER, url=url, kind="employer", on=True, employer_id=employer.id, read_by=read_by,
                    created_at=datetime.now(UTC))
        session.add(site)
        await session.commit()
        return site.id


class NoFetch:
    """A fetcher that fails the test if the server tries to reach the site."""

    async def get(self, *a, **k):
        raise AssertionError("an adapter's system is not tested from the server")

    async def close(self) -> None:
        return None


@pytest.mark.parametrize("system", sorted(adapters.ADAPTERS))
async def test_every_system_with_a_reader_routes_its_sites_to_the_app_without_a_test_fetch(js, system):
    site_id = await _site(system, "https://careers.example.test/jobs")
    async with db.session_factory()() as session:
        site = await session.get(Site, site_id)
        found = await read_probe.settle(session, site, NoFetch())
        assert found is not None and found.read == BY_APP
        assert (site.read_found, site.read_by) == (BY_APP, AUTO)


async def test_a_site_on_a_systems_own_address_is_read_by_the_app_whoever_its_employer_is(js):
    site_id = await _site(None, ats.board_url(EXAMPLE_SYSTEM, {"slug": "Umber"}))
    async with db.session_factory()() as session:
        site = await session.get(Site, site_id)
        assert (await read_probe.settle(session, site, NoFetch())).read == BY_APP


async def test_a_site_with_no_reader_is_still_tested_and_can_go_to_the_browser(js):
    site_id = await _site(NO_READER, "https://careers.example.test/jobs")
    async with db.session_factory()() as session:
        site = await session.get(Site, site_id)
        assert await where.adapter_system(session, site) is None
        assert await where.hand_to_browser(session, site, "it asks for a sign-in") is True
        assert site.read_found == BY_BROWSER


@pytest.mark.parametrize("read_by", [AUTO, BY_APP])
async def test_a_refusal_or_a_browser_finding_cannot_hand_over_a_site_the_app_reads_in_code(js, read_by):
    site_id = await _site(EXAMPLE_SYSTEM, "https://careers.example.test/jobs", read_by)
    async with db.session_factory()() as session:
        site = await session.get(Site, site_id)
        assert await where.hand_to_browser(session, site, "its robots rules turn apps away") is False
        assert site.read_by == read_by and site.read_found is None
