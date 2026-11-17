"""A board the scout names for an employer whose site the app switched off clears the app's reason and reads the board
(docs/PRD.md 28.3, the owner's word of 2026-11-05: clear the off mark). The app switches an employer's site off
when no means can read its careers page and then asks the scout again for the employer's job system; before this, the
board the scout named was saved and never read, because nothing that switches a site on undoes the app's reason. Now
the scout's board answer, and only that, lifts the reason (`where.scout_found_board`) when the board has a reader the
app can run, and switches the site on with it. A site the owner switched off, a blocked employer's, one on a host the
owner avoids, a site the owner's browser reads (the night never reads it, so the board would not be read either), a
board another employer holds, and an answer that names no job system the app can read all stay as they were. Every
employer, site and address is made up; no network."""

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.doors import tools
from app.jobsearch.employers import resolve
from app.models.work import TrailEvent
from app.sites import skills, watch_record, where
from app.sites.models import AUTO, AVOID, BY_BROWSER, Site
from tests.employer_builders import make_employer, make_role
from tests.sites.builders import LABEL_A, SYSTEM_A, SYSTEM_B, UNREAD, board_link, board_of, posting_link, unread_link
from tests.support import OWNER

CAREERS = "https://careers.acme.example.test/jobs"
BOARD = board_link(SYSTEM_A, "acme")
REQUEST = {"kind": "request", "method": "GET", "url": "https://careers.acme.example.test/api/jobs", "items": "results",
           "fields": {"title": "name", "url": "link"}, "url_prefix": "https://careers.acme.example.test"}


async def _site_the_app_gave_up_on(session, gives_up=where.turn_off_unreadable, name="Acme", **employer_fields):
    """An employer the scout answered for with its own careers page, whose site was switched on and then given up on by
    the app: off, with the app's reason. Returns the employer and the site."""
    employer = await make_employer(session, name, careers_url=CAREERS, **employer_fields)
    session.add(make_role(employer, "Programs Lead"))
    await session.flush()
    await tools.board_found_reply(session, OWNER, name, CAREERS)
    await where.seed_employer_sites(session, OWNER)
    site = await session.scalar(select(Site).where(Site.employer_id == employer.id))
    await where.switch(session, OWNER, site.id, True)
    await gives_up(session, site, "its robots.txt disallows reading it")
    assert (site.on, site.off_by) == (False, "app")
    return employer, site


async def _trail_of(session, site) -> list[TrailEvent]:
    return list((await session.scalars(select(TrailEvent).where(TrailEvent.link_id == site.id))).all())


# --- the board is read -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("gives_up", [where.turn_off_unreadable, where.turn_off_unlearnable],
                         ids=["no_means_can_read_it", "could_not_learn_it"])
async def test_a_board_the_scout_names_switches_on_the_site_the_app_gave_up_on_with_the_boards_reader(js, gives_up):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session, gives_up)
        assert [e.name for e in await where.watched_employers(session, OWNER)] == []
        said = await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert said == {"ok": True, "company": "Acme", "system": LABEL_A}
        assert (employer.ats, employer.board) == (SYSTEM_A, board_of("acme"))
        assert (site.on, site.off_by, site.off_note, site.off_at) == (True, None, None, None)
        assert (site.learn_note, site.learn_tries) == (None, 0)
        reader = await skills.current_skill(session, site.id)
        assert reader.recipe["kind"] == "adapter" and reader.recipe["ats"] == SYSTEM_A
        assert employer.check_directly is True, "the employer's watch follows its site"
        assert [e.name for e in await where.watched_employers(session, OWNER)] == ["Acme"], "so the night reads it"
        assert watch_record.set_by(employer) == watch_record.BY_OWNER, "the owner's earlier word is not rewritten"


async def test_the_site_the_scout_switched_back_on_is_on_the_trail_once(js):
    async with db.session_factory()() as session:
        _, site = await _site_the_app_gave_up_on(session)
        assert await _trail_of(session, site) == []
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        [line] = await _trail_of(session, site)
        assert (line.event, line.actor, line.link_kind) == ("site_switched_on", "app", "site")
        assert line.sentence == f"Switched Acme back on: the scout found its job system, {LABEL_A}."
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)  # the same answer again changes nothing
        assert len(await _trail_of(session, site)) == 1
        assert len(await skills.skills_of_site(session, site.id)) == 1


async def test_the_boards_reader_is_the_newest_skill_of_a_site_that_held_a_recipe_of_its_own(js):
    async with db.session_factory()() as session:
        _, site = await _site_the_app_gave_up_on(session)
        await skills.save_skill(session, OWNER, site.id, REQUEST)
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert (await skills.current_skill(session, site.id)).recipe["kind"] == "adapter"
        assert site.on is True


async def test_a_site_whose_board_reader_was_saved_already_gets_no_second_one(js):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        employer.ats, employer.board = SYSTEM_A, board_of("acme")
        await skills.save_skill(session, OWNER, site.id, {"kind": "adapter", "ats": SYSTEM_A,
                                                          "board": {"slug": "acme"}})
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert site.on is True
        assert len(await skills.skills_of_site(session, site.id)) == 1


# --- what the scout's answer does not switch on ----------------------------------------------------------------------


async def test_a_site_the_owner_switched_off_stays_off_when_the_scout_names_a_board(js):
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Acme", careers_url=CAREERS)
        session.add(make_role(employer, "Programs Lead"))
        await where.seed_employer_sites(session, OWNER)
        site = await session.scalar(select(Site).where(Site.employer_id == employer.id))
        await where.switch(session, OWNER, site.id, True)
        await where.switch(session, OWNER, site.id, False)
        assert (site.on, site.off_by, site.off_note) == (False, None, None), "the owner's off carries no mark"
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        await where.seed_employer_sites(session, OWNER)
        assert employer.ats == SYSTEM_A, "the board is kept on the employer"
        assert (site.on, employer.check_directly) == (False, False), "the owner's word on the site stands"
        assert await _trail_of(session, site) == []


async def test_a_site_the_owner_switched_on_again_and_off_again_stays_off_when_the_scout_names_a_board(js):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        await where.switch(session, OWNER, site.id, True)  # the owner's word: the app's reason is cleared
        await where.switch(session, OWNER, site.id, False)
        assert (site.on, site.off_by) == (False, None)
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert (site.on, await skills.current_skill(session, site.id)) == (False, None)


async def test_a_blocked_employers_site_keeps_the_app_reason_when_the_scout_names_a_board(js):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        employer.gated = True
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert (site.on, site.off_by, site.off_note) == (False, "app", "its robots.txt disallows reading it")
        assert await skills.current_skill(session, site.id) is None
        assert employer.check_directly is False


async def test_a_site_on_a_host_the_owner_avoids_keeps_the_app_reason_when_the_scout_names_a_board(js):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        await where.add(session, OWNER, "https://careers.acme.example.test/", list_name=AVOID)
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert (site.on, site.off_by) == (False, "app")
        assert await skills.current_skill(session, site.id) is None
        assert await _trail_of(session, site) == []


# --- an answer that gives the employer no job system the app can read ------------------------------------------------


@pytest.mark.parametrize("answer", [
    {"url": CAREERS},
    {"url": "https://careers.acme.example.test/other"},
    {"not_found": "only an email address"},
], ids=["its_own_careers_page", "another_page_of_its_own", "no_board_to_find"])
async def test_an_answer_that_names_no_job_system_leaves_the_app_reason_standing(js, answer):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        await tools.board_found_reply(session, OWNER, "Acme", **answer)
        assert employer.ats is None
        assert (site.on, site.off_by, site.off_note) == (False, "app", "its robots.txt disallows reading it")
        assert await _trail_of(session, site) == []


async def test_a_board_the_job_system_answered_404_for_is_not_taken_so_the_app_reason_stands(js):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        employer.ats, employer.board = SYSTEM_A, board_of("acme")
        resolve.forget(employer, "the board answered 404")
        assert employer.ats is None
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert employer.ats is None
        assert (site.on, site.off_by) == (False, "app")


async def test_a_job_system_the_owner_set_by_hand_is_not_replaced_and_the_app_reason_stands(js):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        employer.ats, employer.board, employer.ats_found_by = SYSTEM_B, board_of("acme"), "owner"
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert employer.ats == SYSTEM_B
        assert (site.on, site.off_by) == (False, "app")


async def test_a_job_system_the_app_has_no_reader_for_leaves_the_app_reason_standing(js):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        await tools.board_found_reply(session, OWNER, "Acme", unread_link("acme"))
        assert employer.ats == UNREAD, "the board is kept: the browser may read it"
        assert (site.on, site.off_by, site.off_note) == (False, "app", "its robots.txt disallows reading it")
        assert await skills.current_skill(session, site.id) is None


# --- only the scout's board answer lifts the reason ------------------------------------------------------------------


async def test_a_board_found_from_the_employers_links_does_not_lift_the_app_reason(js):
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        session.add(make_role(employer, "Programs Lead 2", url=posting_link(SYSTEM_A, "acme", 1)))
        await session.flush()
        await resolve.from_links(session, OWNER)
        await where.seed_employer_sites(session, OWNER)
        assert employer.ats == SYSTEM_A, "the links named the board"
        assert (site.on, site.off_by) == (False, "app"), "only the scout's answer lifts the reason"


async def test_one_employers_answer_lifts_the_reason_of_its_own_site_only(js):
    async with db.session_factory()() as session:
        _, acme = await _site_the_app_gave_up_on(session)
        other_employer = await make_employer(session, "Globex", careers_url="https://careers.globex.example.test/")
        session.add(make_role(other_employer, "Programs Lead"))
        await session.flush()
        await where.seed_employer_sites(session, OWNER)
        other = await session.scalar(select(Site).where(Site.employer_id == other_employer.id))
        await where.switch(session, OWNER, other.id, True)
        await where.turn_off_unreadable(session, other, "it asks not to be read")
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert (acme.on, other.on, other.off_by) == (True, False, "app")


# --- a site the owner's browser reads is not lifted (the night never reads it) ---------------------------------------


async def _browser_read_site_the_app_gave_up_on(session, how):
    """The app's own site for an employer, handed to the owner's browser (`where.hand_to_browser` for "browser", or an
    `auto` site found to need it), which then failed too: off with the app's reason."""
    employer = await make_employer(session, "Acme", careers_url=CAREERS)
    session.add(make_role(employer, "Programs Lead"))
    await session.flush()
    await tools.board_found_reply(session, OWNER, "Acme", CAREERS)
    await where.seed_employer_sites(session, OWNER)
    site = await session.scalar(select(Site).where(Site.employer_id == employer.id))
    if how == "handed":
        assert await where.hand_to_browser(session, site, "its listings only load in a browser") is True
    else:
        site.read_by, site.read_found = AUTO, BY_BROWSER
    assert where.effective_read(site) == BY_BROWSER
    await where.switch(session, OWNER, site.id, True)
    await where.turn_off_unreadable(session, site, "the browser could not read it either")
    assert (site.on, site.off_by) == (False, "app")
    return employer, site


@pytest.mark.parametrize("how", ["handed", "found"], ids=["handed_to_the_browser", "auto_found_to_need_the_browser"])
async def test_a_site_the_owners_browser_reads_keeps_the_app_reason_when_the_scout_names_a_board(js, how):
    async with db.session_factory()() as session:
        employer, site = await _browser_read_site_the_app_gave_up_on(session, how)
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert employer.ats == SYSTEM_A, "the board is kept on the employer"
        assert (site.on, site.off_by, site.off_note) == (False, "app", "the browser could not read it either")
        assert await skills.current_skill(session, site.id) is None
        assert await _trail_of(session, site) == []
        assert [s.id for s in await where.browser_sites(session, OWNER)] == [], "nothing sends the browser there again"


# --- a board another employer holds is not lifted --------------------------------------------------------------------


async def test_a_board_another_employer_already_holds_is_kept_but_does_not_lift_the_app_reason(js):
    async with db.session_factory()() as session:
        other = await make_employer(session, "Globex")
        resolve.take(other, SYSTEM_A, board_of("talentpool"), "link")
        employer, site = await _site_the_app_gave_up_on(session)
        assert not resolve.owns(employer, SYSTEM_A, board_of("talentpool")), "the board does not carry its name"
        await tools.board_found_reply(session, OWNER, "Acme", board_link(SYSTEM_A, "talentpool"))
        assert employer.ats == SYSTEM_A, "the board is kept: the nightly pass settles whose it is"
        assert (site.on, site.off_by) == (False, "app")
        assert await skills.current_skill(session, site.id) is None
        assert await _trail_of(session, site) == []


async def test_a_board_that_carries_the_employers_name_is_lifted_though_another_employer_holds_it_too(js):
    async with db.session_factory()() as session:
        other = await make_employer(session, "Globex")
        resolve.take(other, SYSTEM_A, board_of("acme"), "link")
        employer, site = await _site_the_app_gave_up_on(session)
        assert resolve.owns(employer, SYSTEM_A, board_of("acme")), "the board is named for the employer"
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert (site.on, site.off_by) == (True, None)


# --- a site that fails again after the lift is lifted again by a fresh answer ----------------------------------------


async def test_a_site_the_app_gave_up_on_again_after_the_lift_is_lifted_again_by_the_scouts_next_answer(js):
    """Each lift is one scout answer, and the scout is handed an employer once a night, so a board the app reads and
    that fails again cycles at most once a night (docs/PRD.md 28.3): no counter, no cap."""
    async with db.session_factory()() as session:
        employer, site = await _site_the_app_gave_up_on(session)
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert site.on is True
        await where.turn_off_unreadable(session, site, "the board answered with something else")
        assert (site.on, site.off_by) == (False, "app")
        assert tools.ASKED not in (employer.sources or {}), "the scout is handed the employer again"
        await tools.board_found_reply(session, OWNER, "Acme", BOARD)
        assert (site.on, site.off_by, site.off_note) == (True, None, None)
        assert len(await _trail_of(session, site)) == 2, "each lift says so on the trail"
