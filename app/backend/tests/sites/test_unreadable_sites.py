"""A site no means can read is switched off by the app and the owner is told (docs/PRD.md 29.3 and 30.3). The owner
decided that a site where scraping or browser traffic doesn't work is turned off by default and the user notified, and
that no cycles go on something that will fail. Only when neither the server nor the browser can read it: a site the
server cannot read goes to the browser first (`where.hand_to_browser`), and is switched off only if the browser fails
too. The one switch-off (`where.turn_off_unreadable`: why and who recorded, one notification linked to the site, no
task), the words of the row, what the missions skip, and the questions that still need the owner. The ways in have their
own tests: the browser reader (test_read_find_out.py), the scout's `task.escalate` (test_site_tools.py) and a plan that
says so (test_site_round_process.py). Every site, company and sentence is made up."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import reader as br
from app.jobsearch.employers import core as employers
from app.models.work import Notification, Task
from app.sites import autostart, projects, where, where_view
from app.sites.models import BY_BROWSER, Site
from tests.support import load_profile

OWNER = "owner"  # the setting's default: the scout's tools act for it


async def site_for(session, url: str, **more) -> Site:
    site = Site(owner=OWNER, url=url, kind="job_board", created_at=datetime.now(UTC), turned_on_at=datetime.now(UTC),
                **more)
    session.add(site)
    await session.flush()
    return site


async def told(session) -> list[Notification]:
    return list((await session.scalars(
        select(Notification).where(Notification.kind == where.OFF_NOTE_KIND).order_by(Notification.title))).all())


async def open_tasks(session) -> list[Task]:
    return list((await session.scalars(select(Task).where(Task.state == "open").order_by(Task.title))).all())


# --- the switch-off ------------------------------------------------------------------------------------------------


async def test_a_site_that_asks_for_a_sign_in_is_switched_off_and_the_owner_is_told_once_in_plain_words(js):
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, "https://board.example.test/jobs")
        note = await where.turn_off_unreadable(session, site, "It asks for a sign-in.")
        assert (site.on, site.off_by, site.off_note) == (False, "app", "it asks for a sign-in")
        assert site.off_at is not None
        assert note is not None and note.title == (
            "board.example.test needs a sign-in to be read, so resumebot turned it off. Sign in to it in the app's "
            "Chrome, then turn it back on under Where to look.")
        assert (note.kind, note.level, note.link_kind, note.link_id) == (
            "site_turned_off", "attention", "site", site.id)
        assert await open_tasks(session) == [], "a notification, not a decision"
        # a site that is off already is not switched off again
        assert await where.turn_off_unreadable(session, site, "it asks for a sign-in") is None
        assert len(await told(session)) == 1
        # switched on and off again, it is still not told twice
        await where.switch(session, OWNER, site.id, True)
        assert await where.turn_off_unreadable(session, site, "asked me to sign in") is None
        assert len(await told(session)) == 1


@pytest.mark.parametrize("why", [
    "its robots.txt disallows reading / on careers.example.test", "cloudflare verification page", "403 Forbidden page",
    "job system maintenance page (service unavailable)", "ExactHire not an accepted portal", "it asks not to be read"])
async def test_a_site_nothing_the_owner_can_do_would_open_is_switched_off_with_no_notification(js, why):
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, "https://board.example.test/jobs")
        assert await where.turn_off_unreadable(session, site, why) is None
        assert (site.on, site.off_by) == (False, "app") and site.off_note
        assert await told(session) == [] and await open_tasks(session) == []
        rows = {r["label"]: r for r in await where_view.listing(session, OWNER)}
        assert rows["board.example.test"]["status"].startswith("Off: can't be read (")


async def test_a_site_that_cannot_be_learned_and_asks_for_no_sign_in_is_off_with_no_notification(js):
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, "https://board.example.test/jobs")
        assert await where.turn_off_unlearnable(session, site, "the careers page only links to a job board") is None
        assert site.off_note.startswith(where.LEARN_OFF) and await told(session) == []


async def test_the_row_says_off_cant_be_read_with_the_reason_and_the_owners_own_switch_off_says_nothing(js):
    async with db.session_factory()() as session:
        mine = await site_for(session, "https://mine.example.test/jobs")
        theirs = await site_for(session, "https://theirs.example.test/jobs")
        await where.turn_off_unreadable(session, mine, "it asks for a sign-in")
        await where.switch(session, OWNER, theirs.id, False)
        rows = {r["label"]: r for r in await where_view.listing(session, OWNER)}
        assert (rows["mine.example.test"]["state"], rows["mine.example.test"]["status"]) == (
            "off", "Off: can't be read (it asks for a sign-in)")
        assert rows["mine.example.test"]["on"] is False and rows["mine.example.test"]["needs"] is None
        assert (rows["theirs.example.test"]["state"], rows["theirs.example.test"]["status"]) == ("off", "")
        assert (rows["theirs.example.test"]["off_by"], rows["theirs.example.test"]["off_note"]) == (None, None)
        # the owner switching the app's off back on takes the words and the marks away
        await where.switch(session, OWNER, mine.id, True)
        assert (mine.on, mine.off_by, mine.off_note) == (True, None, None)
        assert [r["status"] for r in await where_view.listing(session, OWNER, mine.id)] == ["Starts tonight"]


@pytest.mark.parametrize("said, clause", [
    ("Its listings only load in a browser.", "its listings only load in a browser"),
    ("  asked   me to\nsign in.  ", "asked me to sign in"),
    ("CAPTCHA on every page", "CAPTCHA on every page"),
    ("", "no way to read it was found"),
    (None, "no way to read it was found"),
])
def test_the_reason_is_one_plain_clause(said, clause):
    assert where.clause_of(said) == clause


def test_a_long_reason_is_cut_between_words_to_fit_the_notice():
    said = "its listings load only after a long run of scripts that need a real browser " * 5
    clause = where.clause_of(said)
    assert len(clause) <= where.OFF_REASON_MAX and clause.endswith("…") and "  " not in clause
    assert len(where.sign_in_words("A" * 400)) < 300, "the notification's title is never longer than its column"


# --- what the missions skip -----------------------------------------------------------------------------------------


async def test_every_mission_skips_a_site_the_app_switched_off_as_it_does_any_switched_off_site(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        # the night's read of the employers' boards
        off = await employers.find_or_make(session, OWNER, "Initech")
        kept = await employers.find_or_make(session, OWNER, "Globex")
        for employer, host in ((off, "initech"), (kept, "globex")):
            employer.careers_url, employer.check_directly = f"https://{host}.example.test/careers", True
        await session.flush()
        await where.seed_employer_sites(session, OWNER)
        assert [e.name for e in await where.watched_employers(session, OWNER)] == ["Globex", "Initech"]
        initech = await session.scalar(select(Site).where(Site.employer_id == off.id))
        await where.turn_off_unreadable(session, initech, "it turns apps away")
        assert [e.name for e in await where.watched_employers(session, OWNER)] == ["Globex"]
        # the scout's mission, which starts a project for each site that wants one: only the other one gets it
        started = await autostart.ensure_projects(session, OWNER, by_switch=True)
        globex = await session.scalar(select(Site).where(Site.employer_id == kept.id))
        assert [p.site_id for p in started] == [globex.id]
        # the browser's mission
        phone = await site_for(session, "https://phone.example.test/jobs", read_by=BY_BROWSER)
        other = await site_for(session, "https://other.example.test/jobs", read_by=BY_BROWSER)
        assert {s.id for s in await where.browser_sites(session, OWNER)} == {phone.id, other.id}
        await where.turn_off_unreadable(session, phone, "asked me to sign in")
        assert [s.id for s in await where.browser_sites(session, OWNER)] == [other.id]
        assert list(await br.browser_sites(session, OWNER)) == ["other.example.test"]


# --- what still needs the owner -------------------------------------------------------------------------------------


async def test_a_site_that_needs_the_owner_for_any_other_reason_is_left_on_and_still_says_needs_you(js):
    async with db.session_factory()() as session:
        kinds = ("decision", "site_stuck", "site_failed", "site_escalation")
        for kind in kinds:
            site = await site_for(session, f"https://{kind.replace('_', '-')}.example.test/jobs")
            project = await projects.create_project(session, OWNER, site.id, "Read it", "Read it", created_by="you")
            session.add(Task(owner=OWNER, key=f"ask-{kind}", title="Which page should it read?", kind=kind,
                             link_kind="project", link_id=project.id))
        await session.flush()
        for row in await where_view.listing(session, OWNER):
            assert (row["on"], row["state"], row["status"], row["off_by"]) == (True, "needs_you", "Needs you", None)
            assert row["needs"] == "Which page should it read?"
        assert await told(session) == []
        assert len(await open_tasks(session)) == len(kinds), "none of them is closed"


async def test_a_listing_row_says_a_site_is_read_by_code_only_and_when_the_app_switched_it_off(js):
    from app.sites.models import SiteSkill

    async with db.session_factory()() as session:
        coded = await site_for(session, "https://api.example.test/jobs", on=True)
        browsed = await site_for(session, "https://web.example.test/jobs", on=True)
        broken = await where.add(session, OWNER, "https://board.example.test/jobs")
        await where.turn_off_unreadable(session, broken, "It asks for a sign-in.")
        for site, kind in ((coded, "request"), (browsed, "browser")):
            session.add(SiteSkill(owner=OWNER, site_id=site.id, version=1, recipe={"kind": kind},
                                  created_at=datetime.now(UTC)))
        await session.flush()
        rows = {r["url"]: r for r in await where_view.listing(session, OWNER, with_roles=False)}
    assert rows["https://api.example.test/jobs"]["by_code"] is True
    assert rows["https://web.example.test/jobs"]["by_code"] is False, "a browser recipe is the browser's"
    assert rows["https://board.example.test/jobs"]["by_code"] is False
    assert rows["https://board.example.test/jobs"]["off_at"] is not None
    assert rows["https://api.example.test/jobs"]["off_at"] is None
