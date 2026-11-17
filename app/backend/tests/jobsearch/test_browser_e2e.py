"""A site read in the app's browser, end to end through the browser door (docs/PRD.md path 37,
the browser work spec): the Pathfinder's map item works out the site and hands in its runbook, a worker's scan
item follows the runbook, a scan finds the page no longer fits (the runbook is retired and the site is mapped again),
and a site that cannot be mapped five times is switched off by the app until the owner switches it on again. Every
call goes through the door the way the server calls it (a context carrying the browser token), and what is asserted
is what the owner sees: the row's status, the run's line, the notifications. Everything is made up: the site is an
example host and the postings are invented."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch.browser import mcp as browser_mcp
from app.jobsearch.browser import pace as bp
from app.jobsearch.models import BrowserItem, MissionRun
from app.models.work import Notification
from app.sites import skills, where, where_view
from app.sites.models import Lesson, Site
from tests.builders import ONE, browser_world
from tests.jobsearch.example_world import PORTAL, example_world, portal_card  # noqa: F401
from tests.support import browser_recipe

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out", "example_world")

OWNER = "owner"  # the setting's default: the tools act for it

BROWSER_TOKEN = "js_browser_token_e2e_1234567890"
LESSON = "The Search button wants a click, not the Enter key."
NOT_LEARNED = "the session ended without learning it"
DRIFT = "the Date posted menu is gone"


@pytest.fixture
def door(tmp_path, monkeypatch):
    """The browser token file the door checks, the owner the tools act for, and a context carrying the token."""
    token = tmp_path / "browser-token"
    token.write_text(BROWSER_TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr(browser_mcp.settings, "browser_token_file", str(token))
    monkeypatch.setattr(browser_mcp.settings, "owner", OWNER)
    return SimpleNamespace(headers={"authorization": f"Bearer {BROWSER_TOKEN}"})





def posting(n: int) -> dict:
    """One listing as the session reads it off a results page (made up)."""
    return {"site": ONE, "url": f"https://www.{ONE}/jobs/view/{n}", "title": f"Example Role {n}",
            "company": f"Example Co {n}", "snippet": f"A made-up listing, number {n}."}


async def status_of(site_id) -> str:
    """The Status the owner sees on the site's row."""
    async with db.session_factory()() as session:
        [row] = await where_view.listing(session, OWNER, site_id)
    return row["status"]


async def site_state(site_id) -> SimpleNamespace:
    async with db.session_factory()() as session:
        site = await session.get(Site, site_id)
        return SimpleNamespace(on=site.on, learn_tries=site.learn_tries, learn_note=site.learn_note,
                               off_by=site.off_by, off_note=site.off_note)


async def runs() -> list[MissionRun]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(MissionRun).order_by(MissionRun.started_at))).all())


async def run_log() -> list[tuple[str, str]]:
    """Each run in order, as (mode, outcome)."""
    return [(run.started["mode"], run.outcome) for run in await runs()]


async def current_recipe(site_id) -> dict | None:
    async with db.session_factory()() as session:
        skill = await skills.current_skill(session, site_id)
    return skill.recipe if skill is not None else None


async def versions(site_id) -> list[tuple[int, bool]]:
    """Each version of the site's skill as (version, retired)."""
    async with db.session_factory()() as session:
        return [(v.version, v.retired_at is not None) for v in await skills.skills_of_site(session, site_id)]


async def lessons() -> list[tuple[str, str, str]]:
    async with db.session_factory()() as session:
        return [(m.text, m.applies_to, m.source) for m in (await session.scalars(select(Lesson))).all()]


async def notification_count() -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(Notification))


async def owner_switches(site_id, on: bool) -> None:
    """The owner's switch on Where to look, as the page makes it."""
    async with db.session_factory()() as session:
        await where.switch(session, OWNER, site_id, on)
        await session.commit()


@pytest.fixture(autouse=True)
def no_page_gap(monkeypatch):
    """Each step here is a new item on the same site: the gap between pages is the queue's own test's to show."""
    monkeypatch.setattr(bp, "gap", lambda: timedelta(0))


def pathfinder(door) -> SimpleNamespace:
    return SimpleNamespace(headers={**door.headers, "x-session-id": f"pathfinder-{datetime.now(UTC).timestamp()}"})


def worker(door) -> SimpleNamespace:
    return SimpleNamespace(headers={**door.headers, "x-session-id": f"browser_reader-{datetime.now(UTC).timestamp()}"})


async def a_day_later() -> None:
    """The waits of the queue (a failed map waits a day) are over."""
    async with db.session_factory()() as session:
        for row in (await session.scalars(select(BrowserItem))).all():
            row.next_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


async def test_a_site_read_in_the_browser_is_mapped_then_scanned_by_its_runbook_then_switched_off_if_never_mapped(
    js, door,
):
    ids = await browser_world(learned=False, extra_sites=False)
    site_id = ids[ONE]

    # --- 1. nothing mapped yet: a worker has nothing; the Pathfinder is handed the site
    assert await status_of(site_id) == "Read in your browser: not set up yet"
    stop = await browser_mcp.browser_next_item(worker(door))
    assert stop["ok"] is True and stop["stop"] is True
    ctx = pathfinder(door)
    learn = await browser_mcp.browser_next_item(ctx)
    assert learn["item"]["kind"] == "map" and learn["item"]["site"] == ONE and learn["item"]["learn_note"] is None
    assert await status_of(site_id) == "Read in your browser: waiting to be learned"

    # --- 2. the map hands in two postings, then the runbook: kept as version 1, with its lesson, and done
    assert (await browser_mcp.browser_postings(ctx, [posting(1), posting(2)]))["new"] == 2
    today = datetime.now(UTC).date().isoformat()
    kept = await browser_mcp.browser_skill(ctx, ONE, browser_recipe(ONE), page_says=2, lessons=[LESSON])
    assert kept["ok"] is True and kept["kept"] is True and kept["version"] == 1
    assert (await current_recipe(site_id))["kind"] == "browser"
    assert await lessons() == [(LESSON, "job_board", f"learned in the browser {today}")]
    await browser_mcp.browser_hand_back(ctx, learn["item"]["item_id"], "done")
    assert (await browser_mcp.browser_next_item(ctx))["stop"] is True
    learned = (await runs())[-1]
    assert learned.outcome == "done" and learned.summary == "Mapped Listings. Handed in 2 postings, 2 new."

    # --- 3. a worker scans the site by its runbook and hands in one posting
    scanning = worker(door)
    scan = await browser_mcp.browser_next_item(scanning)
    assert scan["item"]["kind"] == "scan" and scan["item"]["runbook"]["kind"] == "browser"
    assert (await browser_mcp.browser_postings(scanning, [posting(3)]))["new"] == 1
    await browser_mcp.browser_hand_back(scanning, scan["item"]["item_id"], "done")
    assert (await browser_mcp.browser_next_item(scanning))["stop"] is True
    assert (await runs())[-1].summary == "Scanned Listings. Handed in 1 posting, 1 new."
    assert await status_of(site_id) == f"Read in your browser: last run {where_view._short_date(learned.started_at)}"

    # --- 4. eight hours on, a scan finds the page no longer fits: the runbook is retired and the owner sees why; the
    #        next map carries the site with that note and the lesson learned before
    async with db.session_factory()() as session:
        row = await session.scalar(select(BrowserItem).where(BrowserItem.kind == "scan"))
        row.done_at = datetime.now(UTC) - timedelta(hours=9)
        await session.commit()
    await a_day_later()
    drifting = worker(door)
    scan = await browser_mcp.browser_next_item(drifting)
    await browser_mcp.browser_hand_back(drifting, scan["item"]["item_id"], "failed", note=DRIFT)
    assert await current_recipe(site_id) is None
    assert await versions(site_id) == [(1, True)]
    assert await status_of(site_id) == f"Learning in your browser: {DRIFT}"
    ctx = pathfinder(door)
    learn = await browser_mcp.browser_next_item(ctx)
    assert (learn["item"]["site"], learn["item"]["learn_note"], learn["item"]["lessons"]) == (ONE, DRIFT, [LESSON])

    # --- 5. five maps end with nothing learned: the app switches the site off once and tells the owner once; the
    #        owner's switch on clears the count and the next map tries again
    for attempt in range(1, 6):
        if attempt > 1:  # the first is the map handed out in step 4
            await a_day_later()
            ctx = pathfinder(door)
            learn = await browser_mcp.browser_next_item(ctx)
        await browser_mcp.browser_hand_back(ctx, learn["item"]["item_id"], "failed", note=NOT_LEARNED)
        state = await site_state(site_id)
        assert state.learn_tries == attempt
        if attempt < 5:
            assert (state.on, state.learn_note) == (True, NOT_LEARNED)
            assert await status_of(site_id) == f"Learning in your browser: {NOT_LEARNED}"
    assert (state.on, state.off_by) == (False, "app")
    assert state.off_note == f"couldn't learn how to read it in your browser ({NOT_LEARNED})"
    assert await status_of(site_id) == f"Off: couldn't learn how to read it in your browser ({NOT_LEARNED})"
    assert await notification_count() == 0, "nothing the owner could do about it: no notification"
    await a_day_later()
    assert (await browser_mcp.browser_next_item(pathfinder(door)))["stop"] is True, "an off site is not visited"

    await owner_switches(site_id, True)
    state = await site_state(site_id)
    assert (state.on, state.learn_tries, state.learn_note, state.off_by, state.off_note) == (True, 0, None, None, None)
    await a_day_later()
    again = await browser_mcp.browser_next_item(pathfinder(door))
    assert (again["item"]["site"], again["item"]["learn_note"]) == (ONE, None)
    assert await notification_count() == 0


async def recorded_portal(site_id) -> str | None:
    async with db.session_factory()() as session:
        return (await session.get(Site, site_id)).portal_url


async def test_a_site_whose_openings_are_on_a_portal_is_mapped_there_scanned_there_and_mapped_again_when_it_moves(
    js, door,
):
    ids = await browser_world(learned=False, extra_sites=False)
    site_id = ids[ONE]

    # --- 1. the careers page lists nothing and links to the employer's listing on a job system: the Pathfinder follows
    #        it, declares it, reads there, and hands in the runbook; the site's row says it is read
    ctx = pathfinder(door)
    learn = await browser_mcp.browser_next_item(ctx)
    assert "portal" not in learn["item"]
    declared = await browser_mcp.browser_postings(
        ctx, [portal_card(1), portal_card(2)], portals=[{"site": ONE, "url": PORTAL}])
    assert declared["ok"] is True and declared["new"] == 2 and declared["portals"] == {ONE: PORTAL}
    kept = await browser_mcp.browser_skill(ctx, ONE, browser_recipe(ONE), page_says=2, lessons=[LESSON])
    assert kept["ok"] is True and kept["portal"] == PORTAL
    await browser_mcp.browser_hand_back(ctx, learn["item"]["item_id"], "done")
    assert await recorded_portal(site_id) == PORTAL

    # --- 2. the scan is sent straight to the portal, and what it reads there is the site's
    scanning = worker(door)
    scan = await browser_mcp.browser_next_item(scanning)
    assert (scan["item"]["site"], scan["item"]["portal"], scan["item"]["link"]) == (ONE, PORTAL, PORTAL)
    assert (await browser_mcp.browser_postings(scanning, [portal_card(2), portal_card(3)]))["new"] == 1
    await browser_mcp.browser_hand_back(scanning, scan["item"]["item_id"], "done")

    # --- 3. the employer moves to another portal: the runbook is retired, and the next map names the old portal
    async with db.session_factory()() as session:
        row = await session.scalar(select(BrowserItem).where(BrowserItem.kind == "scan"))
        row.done_at = datetime.now(UTC) - timedelta(hours=9)
        await session.commit()
    await a_day_later()
    drifting = worker(door)
    scan = await browser_mcp.browser_next_item(drifting)
    await browser_mcp.browser_hand_back(drifting, scan["item"]["item_id"], "failed", note=DRIFT)
    assert await current_recipe(site_id) is None and await recorded_portal(site_id) == PORTAL
    ctx = pathfinder(door)
    learn = await browser_mcp.browser_next_item(ctx)
    assert (learn["item"]["site"], learn["item"]["portal"], learn["item"]["learn_note"]) == (ONE, PORTAL, DRIFT)
    moved = PORTAL.replace("career-page", "careers")
    await browser_mcp.browser_postings(ctx, [portal_card(4)], portals=[{"site": ONE, "url": moved}])
    kept = await browser_mcp.browser_skill(ctx, ONE, browser_recipe(ONE, notes=["Changed."]), page_says=1)
    assert kept["portal"] == moved and await recorded_portal(site_id) == moved
    await browser_mcp.browser_hand_back(ctx, learn["item"]["item_id"], "done")
    assert await versions(site_id) == [(1, True), (2, False)]

    # --- 4. none of this counted against the site: no try, no note, and it is still on
    state = await site_state(site_id)
    assert (state.on, state.learn_tries, state.learn_note) == (True, 0, None)
