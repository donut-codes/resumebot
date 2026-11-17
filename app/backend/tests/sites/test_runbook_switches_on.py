"""A site that gets a runbook is switched on (the owner, 2026-11-05,
docs/PRD.md 29.1): the reader of a job system, a skill a project learned, and a recipe kept for a careers page each
switch on a site nobody has decided about. The owner's own off stays off, so does a reason the app recorded
(`Site.off_by`), a blocked employer's site and one whose host is on the sites to avoid. A site with no runbook is
switched on by nothing. Every employer and site is made up; no network."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import adapters
from app.jobsearch.browser.recipes import html_record, page_run
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.employers import core as employers
from app.sites import auto_watch, autostart, projects, registry, skills, watch_record, where, where_view
from app.sites.models import AVOID, Site, SiteRecording
from tests.support import EXAMPLE_SYSTEM, OWNER

#: A job system the registry holds that the app has no reader for in code.
NO_READER = next(
    r.key for r in registry.current().runbooks if r.kind == "job_system" and r.key not in adapters.ADAPTERS)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
CAREERS = "https://careers.acme.example.test/"
REQUEST = {"kind": "request", "method": "GET", "url": "https://careers.acme.example.test/api/jobs", "items": "results",
           "fields": {"title": "name", "url": "link"}, "url_prefix": "https://careers.acme.example.test"}
HTML = {"kind": "html", "url": CAREERS, "links": 'a[href*="/job/"]', "row": "li.row", "next": "a.pager"}
PAGE = {"kind": "page", "url": CAREERS, "search": "input#q", "links": "a.job", "next": "button.next", "row": "li.row"}


async def _employer(session, name="Acme", *, careers=CAREERS, system=None, board=None, watched=False):
    employer = await employers.find_or_make(session, OWNER, name)
    employer.careers_url, employer.ats, employer.board = careers, system, board or {}
    employer.check_directly = watched
    await session.flush()
    return employer


async def _site_of(session, employer) -> Site:
    await where.seed_employer_sites(session, OWNER)
    return await session.scalar(select(Site).where(Site.owner == OWNER, Site.employer_id == employer.id))


async def _waiting_site(session, **employer_over):
    """An employer's site the app made with no runbook yet: off, never switched on, nobody's word on it."""
    employer = await _employer(session, **employer_over)
    site = await _site_of(session, employer)
    assert (site.on, site.turned_on_at, watch_record.set_by(employer)) == (False, None, None)
    return employer, site


async def _recording(session, site) -> SiteRecording:
    rec = SiteRecording(owner=OWNER, site_id=site.id, requested_at=NOW, state="requested")
    session.add(rec)
    await session.flush()
    return rec


async def _keep(kind, session, site):
    """Keep a recipe for the site the way each of the three ways of making one does."""
    rec = await _recording(session, site)
    if kind == "data":
        return await recipe_record.keep_recipe(session, OWNER, rec, REQUEST, SimpleNamespace(rows=3), NOW)
    if kind == "html":
        return await html_record.keep(session, OWNER, rec, HTML, 3, NOW)
    return await page_run.keep(session, OWNER, rec, PAGE, 3, NOW)


# --- the runbook switches the site on -------------------------------------------------------------------------------


async def test_seeding_a_site_on_a_known_job_system_switches_it_on_with_its_reader(js):
    async with db.session_factory()() as session:
        employer = await _employer(session, system=EXAMPLE_SYSTEM, board={"slug": "acme"})
        site = await _site_of(session, employer)
        assert await skills.current_skill(session, site.id) is not None
        assert site.on is True and site.turned_on_at is not None
        assert employer.check_directly is True, "the employer's watch follows its site's switch"
        assert watch_record.set_by(employer) is None, "nobody set it: not the owner, and not a role in play"
        [row] = await where_view.listing(session, OWNER, site.id)
        assert (row["state"], row["skill"]) == ("working", {"version": 1, "kind": "adapter"})
        assert await autostart.ensure_projects(session, OWNER, by_switch=True) == [], "it works: nothing to learn"


async def test_a_site_whose_job_system_has_no_reader_stays_off_until_it_has_a_runbook(js):
    async with db.session_factory()() as session:
        employer = await _employer(session, system=NO_READER, board={"slug": "acme"})
        site = await _site_of(session, employer)
        assert await skills.current_skill(session, site.id) is None
        assert (site.on, site.turned_on_at, employer.check_directly) == (False, None, False)


async def test_the_app_does_not_unwatch_a_site_it_switched_on_for_its_runbook(js):
    async with db.session_factory()() as session:
        employer = await _employer(session, system=EXAMPLE_SYSTEM, board={"slug": "acme"})
        site = await _site_of(session, employer)
        assert site.on is True
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}
        assert site.on is True, "no role is in play, and the app never recorded a watch for one"


async def test_a_skill_a_project_kept_switches_its_site_on(js):
    async with db.session_factory()() as session:
        employer, site = await _waiting_site(session)
        project = await projects.create_project(session, OWNER, site.id, "Read acme", "Its listings", created_by="you")
        await skills.keep_skill(session, project, REQUEST)
        assert (site.on, employer.check_directly) == (True, True)
        assert site.turned_on_at is not None


@pytest.mark.parametrize("kind", ["data", "html", "page"])
async def test_a_recipe_kept_for_a_careers_page_switches_its_site_on(js, kind):
    async with db.session_factory()() as session:
        employer, site = await _waiting_site(session)
        await _keep(kind, session, site)
        assert (site.on, employer.check_directly) == (True, True)
        assert site.turned_on_at is not None
        assert watch_record.set_by(employer) is None


async def test_a_job_board_the_owner_has_not_switched_on_is_switched_on_by_its_skill(js):
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url="https://jobs.example.test/", kind="job_board", on=False, created_at=NOW)
        session.add(site)
        await session.flush()
        await skills.save_skill(session, OWNER, site.id, REQUEST)
        assert site.on is True and site.turned_on_at is not None


async def test_a_host_recipe_belongs_to_no_site_and_switches_none_on(js):
    async with db.session_factory()() as session:
        _, site = await _waiting_site(session)
        rec = SiteRecording(owner=OWNER, host="careers.acme.example.test", requested_at=NOW, state="requested")
        session.add(rec)
        await session.flush()
        await recipe_record.keep_recipe(session, OWNER, rec, REQUEST, SimpleNamespace(rows=3), NOW)
        assert site.on is False


async def test_a_site_already_on_is_left_as_it_is(js):
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, CAREERS, "employer")
        first = site.turned_on_at
        await skills.save_skill(session, OWNER, site.id, REQUEST)
        assert (site.on, site.turned_on_at) == (True, first)


# --- what the runbook does not switch on -----------------------------------------------------------------------------


async def test_a_site_the_owner_switched_off_stays_off_when_it_gets_a_runbook(js):
    async with db.session_factory()() as session:
        employer = await _employer(session, watched=True)
        site = await _site_of(session, employer)
        assert site.on is True
        await where.switch(session, OWNER, site.id, False)
        await skills.save_skill(session, OWNER, site.id, REQUEST)
        assert (site.on, employer.check_directly) == (False, False)
        # its employer finds its job system later: the reader is saved, and the owner's word still stands
        employer.ats, employer.board = EXAMPLE_SYSTEM, {"slug": "acme"}
        await where.seed_employer_sites(session, OWNER)
        assert site.on is False


async def test_a_site_the_owner_switched_off_before_it_was_ever_on_stays_off(js):
    async with db.session_factory()() as session:
        employer, site = await _waiting_site(session)
        await where.switch(session, OWNER, site.id, False)  # nothing changes, but the owner's word is kept
        assert watch_record.set_by(employer) == watch_record.BY_OWNER
        await _keep("data", session, site)
        assert (site.on, site.turned_on_at) == (False, None)


async def test_a_site_that_was_on_once_and_is_off_with_no_record_is_the_owners_off(js):
    async with db.session_factory()() as session:
        employer, site = await _waiting_site(session)
        site.turned_on_at = NOW  # switched on and off before the app kept who set a watch
        await _keep("html", session, site)
        assert site.on is False


async def test_a_site_the_app_unwatched_for_a_role_that_left_play_stays_off(js):
    async with db.session_factory()() as session:
        employer, site = await _waiting_site(session)
        site.turned_on_at = NOW
        watch_record.note(employer, watch_record.BY_PLAY)
        await _keep("page", session, site)
        assert site.on is False


async def test_a_site_the_app_switched_off_for_a_reason_it_recorded_stays_off(js):
    async with db.session_factory()() as session:
        _, site = await _waiting_site(session)
        site.off_by, site.off_note, site.off_at = "app", "its robots.txt asks not to read /jobs", NOW
        await skills.save_skill(session, OWNER, site.id, REQUEST)
        assert (site.on, site.off_by, site.off_note) == (False, "app", "its robots.txt asks not to read /jobs")


async def test_a_blocked_employers_site_stays_off(js):
    async with db.session_factory()() as session:
        employer, site = await _waiting_site(session)
        employer.gated = True
        await _keep("data", session, site)
        assert (site.on, employer.check_directly) == (False, False)


async def test_a_site_on_a_host_the_owner_avoids_stays_off_and_the_runbook_is_still_saved(js):
    async with db.session_factory()() as session:
        _, site = await _waiting_site(session)
        await where.add(session, OWNER, "https://acme.example.test/", list_name=AVOID)
        saved = await skills.save_skill(session, OWNER, site.id, REQUEST)
        assert site.on is False
        assert (await skills.current_skill(session, site.id)).id == saved.id
