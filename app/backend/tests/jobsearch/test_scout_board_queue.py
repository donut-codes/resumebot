"""Which employers the scout is handed to find a job board for, and in what order (docs/PRD.md 28.3 and 25.12): an
employer with a role in play before one with none, and an employer handed over in a scout night and left unanswered
is not handed over again in that night (it is in the next). An employer the scout answered for is handed over again
only when the app's reading of its site fails for good (28.3, the owner's word of 2026-11-05), never on a timer.
Made-up data."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.doors import tools
from app.jobsearch.missions import record
from app.jobsearch.models import MissionRun
from app.sites import where
from app.sites.models import Site
from tests.builders import mission_of
from tests.employer_builders import make_employer, make_role
from tests.support import OWNER


async def _employer_with_roles(session, name: str, *statuses: str) -> None:
    employer = await make_employer(session, name)
    for n, status in enumerate(statuses):
        session.add(make_role(employer, f"Programs Lead {n}", status))


async def _a_night(session) -> MissionRun:
    """A scout night running: the scout mission's run, as the schedule starts it."""
    scout = await mission_of(session, OWNER, record.SCOUT)
    night = MissionRun(owner=OWNER, mission_id=scout.id, outcome="running", started_by="schedule", started={})
    session.add(night)
    await session.flush()
    return night


async def _asked(session) -> list[str]:
    return [e["company"] for e in (await tools.boards_reply(session, OWNER))["employers"]]


async def test_an_employer_with_a_role_in_play_is_handed_over_before_one_with_more_roles_all_closed(js):
    async with db.session_factory()() as session:
        await _employer_with_roles(session, "Acme", stages.GATED_OUT, stages.REJECTED, stages.WITHDREW)
        await _employer_with_roles(session, "Fabrikam", stages.SECOND_IMPRESSION)
        await session.commit()
        assert await _asked(session) == ["Fabrikam", "Acme"], "in play first, then the most roles"


async def test_an_employer_handed_over_in_a_night_and_not_answered_is_not_handed_over_again_that_night(js):
    async with db.session_factory()() as session:
        await _employer_with_roles(session, "Acme", stages.SECOND_IMPRESSION)
        await _a_night(session)
        assert await _asked(session) == ["Acme"]
        assert await _asked(session) == [], "left unanswered, and still not asked for again tonight"


async def test_an_employer_left_unanswered_in_one_night_is_handed_over_again_in_the_next(js):
    async with db.session_factory()() as session:
        await _employer_with_roles(session, "Acme", stages.SECOND_IMPRESSION)
        first = await _a_night(session)
        assert await _asked(session) == ["Acme"]
        first.outcome, first.ended_at = "done", datetime.now(UTC)
        await _a_night(session)
        assert await _asked(session) == ["Acme"]


async def test_a_search_the_owner_started_by_hand_has_no_night_so_its_employers_are_handed_over_again(js):
    async with db.session_factory()() as session:
        await _employer_with_roles(session, "Acme", stages.SECOND_IMPRESSION)
        assert await _asked(session) == ["Acme"]
        assert await _asked(session) == ["Acme"], "no night to say it was handed over"


# --- an answered employer comes back when the app's reading of its site fails for good (docs/PRD.md 28.3) -------------


async def _answered_with_its_careers_page(session, name: str = "Acme", **fields):
    """An employer with a role in play, handed to the scout, which answered with the employer's own careers page: it
    names no job system, so the app has no board to read and the employer's own site is what it has."""
    employer = await make_employer(session, name, careers_url="https://careers.acme.example/jobs", **fields)
    session.add(make_role(employer, "Programs Lead", stages.SECOND_IMPRESSION))
    await session.flush()
    assert await _asked(session) == [name]
    await tools.board_found_reply(session, OWNER, name, "https://careers.acme.example/jobs")
    assert await _asked(session) == [], "answered, so it waits"
    return employer


async def _its_site_switched_on(session, employer) -> Site:
    await where.seed_employer_sites(session, OWNER)
    site = await session.scalar(select(Site).where(Site.employer_id == employer.id))
    return await where.switch(session, OWNER, site.id, True)


@pytest.mark.parametrize("gives_up", [where.turn_off_unreadable, where.turn_off_unlearnable],
                         ids=["no_means_can_read_it", "could_not_learn_it"])
async def test_an_employer_whose_careers_page_the_app_gave_up_reading_is_handed_over_again(js, gives_up):
    async with db.session_factory()() as session:
        employer = await _answered_with_its_careers_page(session)
        site = await _its_site_switched_on(session, employer)
        assert await _asked(session) == [], "its site reads, so nothing asks again"
        await gives_up(session, site, "its robots.txt disallows reading it")
        assert await _asked(session) == ["Acme"], "the app gave up on the page: the scout looks for another way"


async def test_an_employer_whose_site_the_owner_switches_off_is_not_handed_over_again(js):
    """A page that is switched off is never read and so never fails (28.3)."""
    async with db.session_factory()() as session:
        employer = await _answered_with_its_careers_page(session)
        site = await _its_site_switched_on(session, employer)
        await where.switch(session, OWNER, site.id, False)
        assert await _asked(session) == []


async def test_an_employer_the_scout_found_no_board_for_stays_out_when_its_site_is_given_up(js):
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Acme", careers_url="https://careers.acme.example/jobs")
        session.add(make_role(employer, "Programs Lead", stages.SECOND_IMPRESSION))
        await session.flush()
        assert await _asked(session) == ["Acme"]
        await tools.board_found_reply(session, OWNER, "Acme", not_found="only an email address")
        site = await _its_site_switched_on(session, employer)
        await where.turn_off_unreadable(session, site, "it asks not to be read")
        assert await _asked(session) == [], "what the scout found nothing for stays out for good"


async def test_an_employer_given_up_on_in_the_night_it_was_handed_over_in_is_handed_over_again_the_next_night(js):
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Acme", careers_url="https://careers.acme.example/jobs")
        session.add(make_role(employer, "Programs Lead", stages.SECOND_IMPRESSION))
        first = await _a_night(session)
        assert await _asked(session) == ["Acme"]
        await tools.board_found_reply(session, OWNER, "Acme", "https://careers.acme.example/jobs")
        site = await _its_site_switched_on(session, employer)
        await where.turn_off_unreadable(session, site, "it asks not to be read")
        assert await _asked(session) == [], "handed over once a night, whatever happens to it after"
        first.outcome, first.ended_at = "done", datetime.now(UTC)
        await _a_night(session)
        assert await _asked(session) == ["Acme"]
