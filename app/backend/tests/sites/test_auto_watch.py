"""Watch the job board of every employer with a role in play (docs/PRD.md 29.9): off until the owner switches it on,
then every employer with a role in play is watched (now, and as roles and boards come), the ones it watched are
unwatched when none is in play, and the owner's own word is never undone. Every employer and site is made up; no
network."""

import itertools
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import actions, ats, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.pipeline import board
from app.jobsearch.profile import store as profile_store
from app.models.work import TrailEvent
from app.sites import auto_watch, completion, projects, watch_record, where
from app.sites.models import AVOID, Project, Site
from tests.builders import run_round, stored_by_title
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, reported

_numbers = itertools.count(1)


async def _setting(session, on: bool) -> None:
    """The setting as the owner would have it, without settling anything (a test of the rule asks for that itself)."""
    current = (await profile_store.current(session, OWNER)).settings
    await profile_store.save(
        session, OWNER, "settings", {**current.model_dump(mode="json"), "watch_in_play": on}, by="test")


def _board(slug: str) -> str:
    """The address of a board on a job system the app reads (the registry's, so no system is named here)."""
    found = ats.board_url(EXAMPLE_SYSTEM, {"slug": slug})
    assert found is not None
    return found


async def _employer(session, name, *, careers="", watched=False, gated=False) -> Employer:
    """An employer with a careers page (by default one made from its name; `careers=None` for none)."""
    employer = await employers.find_or_make(session, OWNER, name)
    employer.careers_url = f"https://careers.{name.lower()}.example.test/jobs" if careers == "" else careers
    employer.check_directly, employer.gated = watched, gated
    await session.flush()
    return employer


async def _role(session, employer, status=stages.SECOND_IMPRESSION, **more) -> Opportunity:
    n = next(_numbers)
    role = Opportunity(
        owner=OWNER, slug=f"{employer.key}-{n}", company=employer.name, title=f"Lead {n}", status=status,
        origin="scout", match_key=f"{employer.key}|lead {n}", found=date.today(), employer_id=employer.id, **more)
    session.add(role)
    await session.flush()
    return role


async def _site(session, employer) -> Site:
    return await session.scalar(select(Site).where(Site.owner == OWNER, Site.employer_id == employer.id))


async def _watching(session, *named) -> list[str]:
    """Which of the employers (by name) have their watch on."""
    out = []
    for employer in named:
        site = await _site(session, employer)
        if site is not None and site.on:
            assert employer.check_directly is True, "the employer's watch follows its site's switch"
            out.append(employer.name)
    return out


# --- off until the owner switches it on -----------------------------------------------------------------------------


async def test_it_is_off_for_everyone_until_the_owner_switches_it_on(client):
    async with db.session_factory()() as session:
        assert (await profile_store.current(session, OWNER)).settings.watch_in_play is False, "the default"
        # an owner whose Settings were saved before there was such a setting is off too
        await profile_store.save(session, OWNER, "settings", {"avoid_peak": False}, by="test")
        assert await auto_watch.is_on(session, OWNER) is False
        acme = await _employer(session, "Acme")
        await _role(session, acme)
        await where.seed_employer_sites(session, OWNER)
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}
        assert await _watching(session, acme) == []
        await session.commit()
    body = (await client.get("/api/v1/employers")).json()
    assert (await client.get("/api/v1/sites")).json()["watch_in_play"] is False
    assert [e["name"] for e in body["employers"]] == ["Acme"]


# --- switching it on watches every employer with a role in play right then -------------------------------------------


async def test_switching_it_on_watches_every_employer_with_a_role_in_play_and_no_other(js):
    async with db.session_factory()() as session:
        each_stage = {name: await _employer(session, name) for name in
                      ("Acme", "Borealis", "Cirrus", "Delta", "Echo", "Kilo", "Lima", "Mike")}
        for employer, stage in zip(each_stage.values(), (
            *stages.PRE_APPLY, stages.APPLIED, stages.ACTIVE_DISCUSSION,
        ), strict=True):
            await _role(session, employer, stage)
        ended = await _employer(session, "Foxtrot")  # every role it has is over or stopped
        for stage in (stages.REJECTED, stages.GONE_QUIET, stages.WITHDREW, stages.GATED_OUT):
            await _role(session, ended, stage)
        no_roles = await _employer(session, "Golf")
        no_board = await _employer(session, "Hotel", careers=None)  # a role in play, and no board found yet
        await _role(session, no_board)
        blocked = await _employer(session, "India", gated=True)  # applied before it was blocked
        await _role(session, blocked, stages.APPLIED)
        theirs = await _employer(session, "Juliet", watched=True)  # the owner watched it before there was a setting
        await _role(session, theirs)
        await where.seed_employer_sites(session, OWNER)
        assert await _watching(session, theirs) == ["Juliet"]

        saved = await auto_watch.set_on(session, OWNER, True)
        assert saved == {"on": True, "watched": ["Acme", "Borealis", "Cirrus", "Delta", "Echo", "Kilo", "Lima", "Mike"],
                         "unwatched": []}
        everyone = [*each_stage.values(), ended, no_roles, no_board, blocked, theirs]
        assert await _watching(session, *everyone) == [
            "Acme", "Borealis", "Cirrus", "Delta", "Echo", "Kilo", "Lima", "Mike", "Juliet"]
        assert await _site(session, no_board) is None, "no board, so nothing to switch yet"
        assert await _site(session, blocked) is None, "a blocked employer never has a site"
        for name, employer in each_stage.items():
            site = await _site(session, employer)
            assert site.turned_on_at is not None and watch_record.set_by(employer) == watch_record.BY_PLAY, name
            assert employer.sources["check_directly"]["source"] == "a role in play"
        assert watch_record.set_by(theirs) is None, "the owner's watch was not touched"
        # the setting is the next version of the owner's Settings, with who and why
        first = (await profile_store.history(session, OWNER, "settings"))[0]
        assert (first["saved_by"], first["why"]) == ("owner", "watch every employer with a role in play")
        # asking for what is already so changes and saves nothing
        version = await profile_store.newest_version(session, OWNER, "settings")
        assert await auto_watch.set_on(session, OWNER, True) == {"on": True, "watched": [], "unwatched": []}
        assert await profile_store.newest_version(session, OWNER, "settings") == version


async def test_the_employers_page_says_why_it_is_watched_only_when_a_role_in_play_is_why(js):
    async with db.session_factory()() as session:
        mine = await _employer(session, "Acme")
        yours = await _employer(session, "Borealis")
        await _role(session, mine)
        await _role(session, yours)
        await where.seed_employer_sites(session, OWNER)
        await where.switch(session, OWNER, (await _site(session, yours)).id, True)  # the owner's own switch
        await auto_watch.set_on(session, OWNER, True)

        async def watched_for(employer):
            return (await employers.detail(session, OWNER, employer.id))["site"]["watched_for"]

        assert (await watched_for(mine), await watched_for(yours)) == ("in_play", None)
        await where.switch(session, OWNER, (await _site(session, mine)).id, False)
        assert await watched_for(mine) is None, "it is not watched, so there is nothing to explain"


# --- later: roles, boards and the night ----------------------------------------------------------------------------


async def test_a_role_the_scout_finds_gets_its_employer_watched_without_waiting_for_the_night(js):
    async with db.session_factory()() as session:
        fabrikam = await _employer(session, "Fabrikam")
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        assert await _watching(session, fabrikam) == [], "no role in play yet"
        await session.commit()
    kept = reported("Fabrikam", "Head of Delivery", fit=6, location="Oakfield, ST", work_mode="onsite",
                    comp="$112,500 - $130,000")
    stored = await stored_by_title(await run_round([kept], settings={**PROFILE["settings"], "watch_in_play": True}))
    assert stored["Head of Delivery"].status == stages.LANDING_ZONE
    async with db.session_factory()() as session:
        fabrikam = await employers.find(session, OWNER, "Fabrikam")
        assert await _watching(session, fabrikam) == ["Fabrikam"]


async def test_a_role_the_owner_moves_into_play_gets_its_employer_watched_and_out_of_play_lets_it_go(js):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        role = await _role(session, acme, stages.GATED_OUT)
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        assert await _watching(session, acme) == [], "a role that is gated out is not in play"
        await board.move(session, OWNER, role.id, stages.APPLIED)
        assert await _watching(session, acme) == ["Acme"]
        await board.move(session, OWNER, role.id, stages.ACTIVE_DISCUSSION)
        assert await _watching(session, acme) == ["Acme"], "still in play"
        await board.move(session, OWNER, role.id, stages.WITHDREW)
        assert await _watching(session, acme) == []


@pytest.mark.parametrize("out", [stages.REJECTED, stages.GONE_QUIET])
async def test_a_role_the_owner_marks_rejected_or_gone_quiet_lets_its_employer_go_and_bringing_it_back_watches_again(
        js, out):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        role = await _role(session, acme, stages.ACTIVE_DISCUSSION)
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        assert await _watching(session, acme) == ["Acme"], "in talks is in play"
        await board.move(session, OWNER, role.id, out)
        assert await _watching(session, acme) == [], "an ended role is out of play"
        await board.move(session, OWNER, role.id, stages.ACTIVE_DISCUSSION)
        assert await _watching(session, acme) == ["Acme"], "pulled back into play"


@pytest.mark.parametrize("back", [stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH])
async def test_a_role_pulled_back_by_hand_from_an_ended_stage_is_in_play_and_moves_between_work_stages_change_nothing(
        js, back):
    other = stages.READY_TO_LAUNCH if back == stages.SECOND_IMPRESSION else stages.SECOND_IMPRESSION
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        role = await _role(session, acme, stages.WITHDREW)
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        assert await _watching(session, acme) == [], "withdrawn is out of play"
        await board.move(session, OWNER, role.id, back)
        assert await _watching(session, acme) == ["Acme"], "pulled back to the analyst or the writer is in play"
        await board.move(session, OWNER, role.id, other)
        assert await _watching(session, acme) == ["Acme"], "still in play"
        await board.move(session, OWNER, role.id, stages.GONE_QUIET)
        assert await _watching(session, acme) == []


async def test_the_nights_read_watches_an_employer_the_moment_its_board_is_found(js, monkeypatch):
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=f'<a href="{_board("found-it")}">Jobs</a>')

    monkeypatch.setattr(polite, "make", lambda: Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)),
                                                        per_host_seconds=0))
    async with db.session_factory()() as session:
        await _setting(session, True)
        maple = await _employer(session, "Maple", careers=None)  # no board yet; a role of its names one
        await _role(session, maple, url=f"{_board('maple')}/jobs/17")
        pine = await _employer(session, "Pine")  # a careers page, and no job system found
        await _role(session, pine)
        oak = await _employer(session, "Oak")  # the same, with no role in play
        await _role(session, oak, stages.REJECTED)
        await where.seed_employer_sites(session, OWNER)  # the start
        assert await _site(session, maple) is None and await _watching(session, pine, oak) == ["Pine"]

        found = await actions.resolve_links(session, None, {"owner": OWNER})
        assert found["found"] == 2
        assert (maple.ats, pine.ats, oak.ats) == (EXAMPLE_SYSTEM, EXAMPLE_SYSTEM, None)
        assert await _watching(session, maple, pine, oak) == ["Maple", "Pine"], (
            "Maple's site was made when its board was found, and it was watched in the same step")
        assert "careers.pine.example.test" in seen and "careers.oak.example.test" not in seen, (
            "tonight's careers-page step read the page of the employer the rule had just watched")


async def test_an_employer_that_gains_a_board_is_watched_when_its_site_is_made_whoever_found_it(js):
    async with db.session_factory()() as session:
        await _setting(session, True)
        acme = await _employer(session, "Acme", careers=None)
        await _role(session, acme)
        await where.seed_employer_sites(session, OWNER)
        assert await _site(session, acme) is None
        acme.careers_url = "https://careers.acme.example.test/jobs"  # found by an import, the analyst or the scout
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 1, "skills": 0}
        assert await _watching(session, acme) == ["Acme"]


# --- unwatching: only its own, once no role is in play ---------------------------------------------------------------


async def test_it_unwatches_what_it_watched_once_no_role_is_in_play_and_never_what_the_owner_watched(js):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")  # the setting watches it
        first, second = await _role(session, acme), await _role(session, acme, stages.READY_TO_LAUNCH)
        borealis = await _employer(session, "Borealis")  # the owner switches it on by hand
        b_role = await _role(session, borealis)
        cirrus = await _employer(session, "Cirrus", watched=True)  # watched before the record was kept
        c_role = await _role(session, cirrus)
        await where.seed_employer_sites(session, OWNER)
        await where.switch(session, OWNER, (await _site(session, borealis)).id, True)
        await auto_watch.set_on(session, OWNER, True)
        assert await _watching(session, acme, borealis, cirrus) == ["Acme", "Borealis", "Cirrus"]

        first.status = stages.REJECTED
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}, "one is still in play"
        second.status = stages.GONE_QUIET
        for role in (b_role, c_role):
            role.status = stages.WITHDREW
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": ["Acme"]}
        assert await _watching(session, acme, borealis, cirrus) == ["Borealis", "Cirrus"]
        assert (await _site(session, acme)).on is False and acme.check_directly is False
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}, "nothing more to do"

        # a new role in play brings it back: it is the app's to watch again, since the owner never said otherwise
        await _role(session, acme)
        assert await auto_watch.settle(session, OWNER) == {"watched": ["Acme"], "unwatched": []}


async def test_switching_it_off_stops_new_watching_and_the_ones_watched_go_when_their_roles_leave_play(js):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        a_role = await _role(session, acme)
        borealis = await _employer(session, "Borealis")
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        assert await _watching(session, acme, borealis) == ["Acme"]

        assert await auto_watch.set_on(session, OWNER, False) == {"on": False, "watched": [], "unwatched": []}
        assert await _watching(session, acme, borealis) == ["Acme"], "what it had watched stays watched"
        await _role(session, borealis)
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}, "nothing new is watched"
        assert await _watching(session, acme, borealis) == ["Acme"]

        a_role.status = stages.REJECTED  # its last role leaves play: the setting being off does not keep it watched
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": ["Acme"]}
        assert await _watching(session, acme, borealis) == []


# --- the owner's word is never undone --------------------------------------------------------------------------------


async def test_it_never_switches_back_on_an_employer_the_owner_switched_off(js):
    async with db.session_factory()() as session:
        acme, borealis, cirrus, delta = [await _employer(session, n) for n in ("Acme", "Borealis", "Cirrus", "Delta")]
        for employer in (acme, borealis, cirrus, delta):
            await _role(session, employer)
        old = await _employer(session, "Echo", watched=True)  # on at the start, switched off before any record
        await _role(session, old)
        await where.seed_employer_sites(session, OWNER)
        old_site = await _site(session, old)
        old_site.on, old.check_directly = False, False
        await auto_watch.set_on(session, OWNER, True)
        assert await _watching(session, acme, borealis, cirrus, delta, old) == ["Acme", "Borealis", "Cirrus", "Delta"]
        assert watch_record.set_by(old) is None and old_site.turned_on_at is not None

        await where.switch(session, OWNER, (await _site(session, acme)).id, False)  # the owner, on one it watched
        await where.switch(session, OWNER, (await _site(session, borealis)).id, True)  # and on another, on then off
        await where.switch(session, OWNER, (await _site(session, borealis)).id, False)
        assert watch_record.set_by(acme) == watch_record.set_by(borealis) == watch_record.BY_OWNER
        assert acme.sources["check_directly"]["source"] == "you"
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}
        assert await _watching(session, acme, borealis, cirrus, delta, old) == ["Cirrus", "Delta"]

        # nor does the next setting that is switched off and on, or a new role in play
        await auto_watch.set_on(session, OWNER, False)
        await auto_watch.set_on(session, OWNER, True)
        await _role(session, acme)
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}
        assert await _watching(session, acme, borealis, old) == []


async def test_a_site_the_owner_added_and_switched_off_before_its_employer_was_known_is_left_off(js):
    async with db.session_factory()() as session:
        added = await where.add(session, OWNER, "https://careers.acme.example.test/jobs", "employer")
        await where.switch(session, OWNER, added.id, False)  # theirs; it belongs to no employer yet, so nothing is kept
        acme = await _employer(session, "Acme")  # the same address
        await _role(session, acme)
        await where.seed_employer_sites(session, OWNER)  # the site is Acme's now
        assert (await _site(session, acme)).id == added.id and watch_record.set_by(acme) is None
        await auto_watch.set_on(session, OWNER, True)
        assert await _watching(session, acme) == [], "it was on once and is off: the owner's, though no record says so"


async def test_an_employer_with_two_sites_is_watched_through_one_of_them(js):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        await _role(session, acme)
        await where.seed_employer_sites(session, OWNER)
        first = await _site(session, acme)
        session.add(Site(owner=OWNER, url="https://jobs.acme.example.test/", kind="employer", on=False,
                         employer_id=acme.id, created_at=datetime.now(UTC), added_by="app"))
        await session.flush()
        assert await auto_watch.set_on(session, OWNER, True) == {"on": True, "watched": ["Acme"], "unwatched": []}
        on = (await session.scalars(select(Site).where(Site.employer_id == acme.id, Site.on.is_(True)))).all()
        assert [s.id for s in on] == [first.id]
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}


async def test_the_apps_watch_is_not_the_owners_try_again_for_a_project_that_ended_with_no_skill(js):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        role = await _role(session, acme)
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        site = await _site(session, acme)
        stamped = site.turned_on_at
        assert stamped is not None
        project = await projects.create_project(
            session, OWNER, site.id, "Learn the site", "Its listings can be read", created_by="you")
        project.finished_at = datetime.now(UTC)  # it ended, and left no skill to read the site with
        role.status = stages.REJECTED
        await auto_watch.settle(session, OWNER)
        role.status = stages.SECOND_IMPRESSION
        await auto_watch.settle(session, OWNER)  # in play again, so watched again, by the app
        assert (site.on, site.turned_on_at) == (True, stamped), "no second project is asked for"
        await where.switch(session, OWNER, site.id, False)  # the owner's own off and on is the try again it always was
        await where.switch(session, OWNER, site.id, True)
        assert site.turned_on_at > stamped


async def test_the_owner_can_take_over_a_watch_and_the_app_then_leaves_it_on(js):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        role = await _role(session, acme)
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        site = await _site(session, acme)
        await where.switch(session, OWNER, site.id, False)  # off and on again by hand makes it theirs
        await where.switch(session, OWNER, site.id, True)
        role.status = stages.REJECTED
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}
        assert await _watching(session, acme) == ["Acme"]
        # a fact the owner sets by hand on the employer's record is theirs the same way
        other = await _employer(session, "Borealis")
        await employers.edit(session, OWNER, other.id, {"check_directly": True})
        assert watch_record.set_by(other) == watch_record.BY_OWNER


async def test_it_never_watches_a_blocked_employer_and_unwatches_one_that_was_blocked_after(js):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme", gated=True)  # blocked before it had a site: it never gets one
        await _role(session, acme, stages.APPLIED)
        borealis = await _employer(session, "Borealis")  # blocked after it had a site that was off
        await _role(session, borealis, stages.APPLIED)
        cirrus = await _employer(session, "Cirrus")  # watched, then blocked with its application still open
        applied = await _role(session, cirrus, stages.APPLIED)
        await where.seed_employer_sites(session, OWNER)
        await employers.edit(session, OWNER, borealis.id, {"gated": True, "gated_reason": "no"})
        await auto_watch.set_on(session, OWNER, True)
        assert await _site(session, acme) is None
        assert await _watching(session, acme, borealis, cirrus) == ["Cirrus"]
        await employers.edit(session, OWNER, cirrus.id, {"gated": True, "gated_reason": "no"})
        assert applied.status == stages.APPLIED, "gating leaves an application that is open"
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": ["Cirrus"]}
        assert await _watching(session, acme, borealis, cirrus) == []
        await employers.edit(session, OWNER, cirrus.id, {"gated": False})  # not blocked now, and it has a role in play
        assert await auto_watch.settle(session, OWNER) == {"watched": ["Cirrus"], "unwatched": []}


async def test_it_leaves_a_site_the_app_switched_off_because_it_cannot_be_read_for_the_owner_to_switch_on(js):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        await _role(session, acme)
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        site = await _site(session, acme)
        await where.turn_off_unreadable(session, site, "its listings only load in a browser")
        assert (site.on, site.off_by) == (False, "app")
        assert await auto_watch.settle(session, OWNER) == {"watched": [], "unwatched": []}
        assert await _watching(session, acme) == []
        await where.switch(session, OWNER, site.id, True)  # the owner's word clears the app's marks, and is the owner's
        assert (site.on, site.off_by, watch_record.set_by(acme)) == (True, None, watch_record.BY_OWNER)


async def test_an_employer_whose_site_is_on_the_list_to_avoid_is_skipped_and_the_rest_are_watched(js):
    async with db.session_factory()() as session:
        acme, borealis = await _employer(session, "Acme"), await _employer(session, "Borealis")
        await _role(session, acme)
        await _role(session, borealis)
        await where.seed_employer_sites(session, OWNER)
        await where.add(session, OWNER, "https://careers.acme.example.test", list_name=AVOID)
        await auto_watch.set_on(session, OWNER, True)
        assert await _watching(session, acme, borealis) == ["Borealis"]


# --- the project of a site follows the watch, in the app's own words -------------------------------------------------


async def test_the_app_pauses_the_project_of_a_site_it_unwatches_and_resumes_it_but_never_one_the_owner_paused(js):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        role = await _role(session, acme)
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        site = await _site(session, acme)
        project = await projects.create_project(
            session, OWNER, site.id, "Learn the site", "Its listings can be read", created_by="you")

        async def lines():
            return [(t.actor, t.sentence) for t in (await session.scalars(
                select(TrailEvent).where(TrailEvent.link_id == project.id).order_by(TrailEvent.at))).all()
                if t.event in ("stopped", "resumed")]

        role.status = stages.REJECTED
        await auto_watch.settle(session, OWNER)
        assert (project.stopped_at is not None, project.stop_reason) == (True, where.SWITCHED_OFF_PAUSE)
        assert await lines() == [("app", where.PLAY_OFF_WORDS)], "the owner switched nothing, and is not told so"
        role.status = stages.SECOND_IMPRESSION
        await auto_watch.settle(session, OWNER)
        assert project.stopped_at is None
        assert (await lines())[-1] == ("app", where.PLAY_ON_WORDS)

        await completion.stop_project(session, project, "I will look at this one later")  # the owner's own pause
        role.status = stages.REJECTED
        await auto_watch.settle(session, OWNER)
        role.status = stages.SECOND_IMPRESSION
        await auto_watch.settle(session, OWNER)
        assert await _watching(session, acme) == ["Acme"]
        assert project.stopped_at is not None and project.stop_reason == "I will look at this one later"
        # the owner's own switch still resumes any paused project of the site, whoever paused it, as it always did
        await where.switch(session, OWNER, site.id, False)
        await where.switch(session, OWNER, site.id, True)
        assert (await session.get(Project, project.id)).stopped_at is None


# --- the route, and a rule that fails ---------------------------------------------------------------------------------


async def test_the_screens_switch_saves_the_setting_and_watches_now(client):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        await _role(session, acme)
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
    assert (await client.get("/api/v1/sites")).json()["watch_in_play"] is False
    on = await client.put("/api/v1/employers/watch-in-play", json={"on": True})
    assert (on.status_code, on.json()) == (200, {"on": True})
    listed = (await client.get("/api/v1/employers")).json()
    assert (await client.get("/api/v1/sites")).json()["watch_in_play"] is True
    assert listed["employers"][0]["site"]["on"] is True
    [acme_id] = [e["id"] for e in listed["employers"]]
    site = (await client.get(f"/api/v1/employers/{acme_id}")).json()["site"]
    assert (site["on"], site["watched_for"]) == (True, "in_play")
    assert (await client.put("/api/v1/employers/watch-in-play", json={"on": "yes please"})).status_code == 422
    assert (await client.put("/api/v1/employers/watch-in-play", json={})).status_code == 422
    off = await client.put("/api/v1/employers/watch-in-play", json={"on": False})
    assert (off.json(), (await client.get("/api/v1/sites")).json()["watch_in_play"]) == ({"on": False}, False)
    assert (await client.get(f"/api/v1/employers/{acme_id}")).json()["site"]["on"] is True, "it stays watched"


async def test_blocking_an_employer_on_its_page_takes_it_off_the_watch_at_once_and_letting_it_back_puts_it_on(client):
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        await _role(session, acme, stages.APPLIED)  # an application stays open when its employer is blocked
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)
        acme_id = acme.id
        await session.commit()

    async def watched() -> bool:
        return (await client.get(f"/api/v1/employers/{acme_id}")).json()["site"]["on"]

    assert await watched() is True
    blocked = await client.patch(f"/api/v1/employers/{acme_id}", json={"gated": True, "gated_reason": "no"})
    assert blocked.status_code == 200 and await watched() is False
    assert blocked.json()["check_directly"] is False, "the answer is the employer as it is now, watch included"
    let_back = await client.patch(f"/api/v1/employers/{acme_id}", json={"gated": False})
    assert (await watched(), let_back.json()["check_directly"]) == (True, True)


async def test_a_rule_that_fails_never_costs_the_sites_or_the_night(js, monkeypatch):
    async def broken(session, owner):
        raise RuntimeError("made-up failure")

    monkeypatch.setattr(auto_watch, "settle", broken)
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 1, "skills": 0}
        assert await _site(session, acme) is not None
