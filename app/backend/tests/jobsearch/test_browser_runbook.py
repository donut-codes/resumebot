"""The Pathfinder's runbook (the owner, 2026-10-15): a browser recipe holds a markdown runbook written to a fixed
template, with the few fields the reader and the app use (bookmarks, ats, new_since); the older recipe shape is still
read. Every name and address is made up."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app import db
from app.harness import instructions as hi
from app.jobsearch import ats, cli
from app.jobsearch.browser import items as bd
from app.jobsearch.browser import leases as bl
from app.jobsearch.browser import reader as br
from app.jobsearch.models import BrowserItem
from app.sites import skills
from app.sites.models import Site
from tests.builders import ONE, PATHFINDER, TWO, WORKER, browser_world, in_session, lease
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import browser_recipe

pytestmark = pytest.mark.usefixtures("example_world")

OWNER = "owner"
NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
HOST = "https://www.listings.example.test"
PORTAL = "https://acme.wd5.tenantsys.test/Careers"
SMALL = "# Listings runbook\n## 1. Start\n- go to the jobs page\n"


def runbook_recipe(text: str = SMALL, **more) -> dict:
    return {"kind": "browser", "start_url": f"{HOST}/jobs", "runbook": text,
            "bookmarks": [{"phrase": "operations", "place": "remote", "url": f"{HOST}/jobs?q=operations&sort=new"}],
            "ats": "Custom", "new_since": "Sort by newest; stop at the first posting older than the last run", **more}


async def offered(kind: str, host: str, sid: str = PATHFINDER) -> dict:
    """The item a session is sent for this site, as `browser.next_item` sends it."""
    item_id = await lease(kind, host, sid, NOW)

    async def offer(session):
        row = await session.get(BrowserItem, uuid.UUID(item_id))
        entry = await bl.session_run(session, OWNER, sid, NOW)
        entry.started = {**entry.started, "instructed": False}
        return await bd._offer(session, OWNER, row, entry, NOW)

    return await in_session(sid, offer)


async def to_map() -> list[str]:
    async with db.session_factory()() as session:
        return sorted(await br.to_learn(session, OWNER))


async def keep(site_id, recipe) -> None:
    async with db.session_factory()() as session:
        old = await skills.current_skill(session, site_id)
        if old is not None:
            await skills.retire_skill(session, old)
        await skills.save_skill(session, OWNER, site_id, recipe, None)
        await session.commit()


def test_the_template_has_the_eight_sections_and_the_instructions_name_it():
    template = hi.read("templates", "browser.pathfinder.runbook")
    assert "## 8. Unconfirmed" in template and hi.placeholders(template) == set()
    order = [template.index(h) for h in ("## 5. Reading results", "## 6. Finding the original posting",
                                         "## 7. Fallbacks")]
    assert order == sorted(order)
    learner = hi.read("instructions", "browser.learner")
    assert "at least one listing" in learner and "original posting" in learner
    assert "runbook_template" in learner


async def test_a_map_item_carries_the_runbook_template_and_the_runbooks_shape(js):
    await browser_world(learned=False, extra_sites=False)
    got = await offered("map", ONE)
    assert got["item"]["runbook_template"] == hi.read("templates", "browser.pathfinder.runbook")
    assert got["response_template"] == hi.read("templates", "browser.learner.skill")


async def test_a_runbook_recipe_saves_at_six_thousand_characters_and_is_refused_above_that(js):
    ids = await browser_world(learned=False)
    async with db.session_factory()() as session:
        kept = await skills.save_skill(session, OWNER, ids[ONE], runbook_recipe("x" * skills.BROWSER_RUNBOOK_MAX))
        await session.commit()
        assert kept.recipe["runbook"] == "x" * 6_000 and kept.recipe["bookmarks"][0]["place"] == "remote"
    with pytest.raises(ValueError, match=r"runbook: is longer than 6000 characters \(6001\)"):
        skills.parse_recipe(runbook_recipe("x" * 6_001))
    assert any("runbook: is longer than 6000" in p for p in skills.recipe_problems(runbook_recipe("x" * 6_001)))


@pytest.mark.parametrize(("raw", "reason"), [
    (runbook_recipe(bookmarks=[{"phrase": "a", "place": "b", "url": "http://localhost/x"}]), "must be a public"),
    (runbook_recipe(bookmarks=[{"phrase": "a", "place": "b"}]), "is missing"),
    (runbook_recipe(new_since="x" * 301), "new_since"),
    (runbook_recipe(""), "runbook: is empty"),
    ({k: v for k, v in browser_recipe().items() if k != "search"}, "search: is missing"),
    (browser_recipe(ats="Another system"), "go with a runbook"),
])
def test_a_bad_runbook_recipe_is_refused_with_its_reason(raw, reason):
    with pytest.raises(ValueError, match=reason):
        skills.parse_recipe(raw)


def test_an_old_shape_recipe_still_loads_and_a_runbook_needs_none_of_its_parts():
    old = skills.parse_recipe(browser_recipe())
    assert old.runbook is None and old.bookmarks == [] and old.search.how == "boxes"
    # as an older save kept it: none of the new keys
    older = old.model_dump(mode="json", exclude=set(skills.BROWSER_RUNBOOK_FIELDS))
    assert skills.parse_recipe(older).kind == "browser"
    new = skills.parse_recipe(runbook_recipe())
    assert new.search is None and new.ats == "Custom" and new.bookmarks[0].phrase == "operations"


async def test_a_scan_gets_the_runbook_and_its_bookmarks_and_an_old_recipe_as_it_was(js):
    ids = await browser_world()
    await keep(ids[ONE], runbook_recipe())
    full = (await offered("scan", ONE, WORKER))["item"]["runbook"]
    assert full["runbook"].startswith("# Listings runbook")
    assert full["bookmarks"] == [
        {"phrase": "operations", "place": "remote", "url": f"{HOST}/jobs?q=operations&sort=new"}]
    assert "search" not in full, "a runbook goes without its empty old parts"
    old = (await offered("scan", TWO, "browser_reader-other"))["item"]["runbook"]
    assert old["search"]["how"] == "boxes" and not old.get("runbook")
    text = hi.read("instructions", "browser.reader")
    for word in ("runbook", "bookmarks", "new_since", "section 7"):
        assert word in text


async def test_a_site_on_a_job_system_with_a_runbook_is_handed_it_as_a_starting_point(js):
    ids = await browser_world(learned=False)
    await keep(ids[ONE], runbook_recipe(ats=ats.label("tenantsys")))
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://careers.other.example.test/", kind="employer", read_by="browser",
                         portal_url=PORTAL, created_at=NOW))
        await session.commit()
    item = (await offered("map", "careers.other.example.test"))["item"]
    assert item["reuses"] == {"site": ONE, "ats": ats.label("tenantsys"), "runbook": SMALL}
    assert "reuses" not in (await offered("map", TWO, "pathfinder-other"))["item"], "others get none"


async def test_a_runbook_for_another_job_system_is_not_handed_over(js):
    ids = await browser_world(learned=False)
    await keep(ids[ONE], runbook_recipe(ats=ats.label("feedsys")))
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://careers.other.example.test/", kind="employer", read_by="browser",
                         portal_url=PORTAL, created_at=NOW))
        await session.commit()
    assert "reuses" not in (await offered("map", "careers.other.example.test"))["item"]


async def test_relearn_browser_sites_puts_every_browser_site_back_in_the_queue(js, capsys):
    ids = await browser_world()
    assert await cli._relearn_browser_sites(SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert "Listings: in the learn queue" in out and f"{TWO}: in the learn queue" in out
    assert "server-read.example.test: left alone" in out
    async with db.session_factory()() as session:
        for key in (ONE, TWO):
            assert await skills.current_skill(session, ids[key]) is not None, "still reads until the new one is kept"
    assert await to_map() == sorted([ONE, TWO])


async def _mode(mode: str) -> int:
    async with db.session_factory()() as session:
        n = await br.browser_mode_seen(session, OWNER, mode)
        await session.commit()
        return n


async def test_the_first_start_in_the_owners_own_chrome_sends_every_browser_site_back_to_be_learned_again(js):
    from sqlalchemy import select

    from app.models.work import TrailEvent

    ids = await browser_world()
    assert await _mode("profile") == 0, "the separate profile relearns nothing"
    async with db.session_factory()() as session:
        assert all(not s.relearn for s in await br._search_sites(session, OWNER))
    n = await _mode("own")
    assert n >= 2
    async with db.session_factory()() as session:
        for key in (ONE, TWO):
            assert await skills.current_skill(session, ids[key]) is not None, "reads until the new one is kept"
        assert sorted(await br.to_learn(session, OWNER)) == sorted([ONE, TWO])
        lines = (await session.scalars(select(TrailEvent.sentence).where(TrailEvent.event == br.BROWSER_OWN))).all()
        assert lines == [f"The browser jobs now use your own Chrome: {n} sites go back to the Pathfinder "
                         "to be learned again."]
    assert await _mode("own") == 0, "once: the next starts in the owner's Chrome change nothing"
    assert await _mode("profile") == 0
    assert await _mode("own") == n, "a change from the separate profile back to the owner's Chrome learns them again"
    assert await _mode("mine") == 0
