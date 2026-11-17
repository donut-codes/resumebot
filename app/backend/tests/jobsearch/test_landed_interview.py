"""A role that has been in Active Discussion (the stage that means an interview) is flagged `landed_interview`, so it
is easy to find later. The flag is set by the stage-move hook for every way a role gets there (the owner's move, a role
made in the stage, any code that sets the status, a tracker import new or updated), is read in the role's API view, and
the app never clears it, even when the role ends. Everything is made up."""

from datetime import date

from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.imports import tracker
from app.jobsearch.models import Opportunity
from tests.support import OWNER, tracker_row, tracker_workbook

ACTIVE = stages.ACTIVE_DISCUSSION


def role(slug="a", status=stages.SECOND_IMPRESSION, **more) -> Opportunity:
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": f"Co {slug}", "title": f"Role {slug}", "fit": 3, "origin": "scout",
        "match_key": f"co {slug}|role {slug}", "found": date.today(), "status": status, **more})


async def _make(slug="a", status=stages.SECOND_IMPRESSION):
    async with db.session_factory()() as session:
        made = role(slug, status)
        session.add(made)
        await session.commit()
        return made.id


async def _flag(role_id) -> bool:
    async with db.session_factory()() as session:
        return (await session.get(Opportunity, role_id, populate_existing=True)).landed_interview


async def _move(client, role_id, to, **more):
    return await client.post(f"/api/v1/roles/{role_id}/move", json={"to": to, **more})


async def test_a_role_starts_without_the_flag_and_a_move_short_of_the_stage_leaves_it_off(client):
    role_id = await _make()
    assert await _flag(role_id) is False
    assert (await _move(client, role_id, stages.APPLIED)).status_code == 200
    assert await _flag(role_id) is False


async def test_the_owners_move_into_active_discussion_sets_the_flag(client):
    role_id = await _make()
    assert (await _move(client, role_id, ACTIVE)).status_code == 200
    assert await _flag(role_id) is True


async def test_a_role_made_in_active_discussion_has_the_flag(js):
    assert await _flag(await _make(status=ACTIVE)) is True


async def test_any_code_that_sets_the_status_sets_the_flag(js):
    """The mail reader and the agents change a role's status on the ORM object, with no call of their own."""
    role_id = await _make(status=stages.APPLIED)
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).status = ACTIVE
        await session.commit()
    assert await _flag(role_id) is True


async def test_an_imported_tracker_row_in_active_discussion_has_the_flag_and_one_that_is_not_does_not(js):
    async with db.session_factory()() as session:
        await tracker.import_tracker(session, OWNER, tracker_workbook([
            tracker_row("Acme", "Builder", status="Active Discussion"), tracker_row("Acme", "Maker", status="Applied"),
        ]))
        await session.commit()
        flags = {r.title: r.landed_interview for r in (await session.scalars(select(Opportunity))).all()}
    assert flags == {"Builder": True, "Maker": False}


async def test_an_imported_row_that_moves_an_existing_role_into_the_stage_sets_the_flag(js):
    async with db.session_factory()() as session:
        await tracker.import_tracker(
            session, OWNER, tracker_workbook([tracker_row("Acme", "Builder", status="Applied")]))
        await session.commit()
        await tracker.import_tracker(
            session, OWNER, tracker_workbook([tracker_row("Acme", "Builder", status="Active Discussion")]))
        await session.commit()
        [only] = (await session.scalars(select(Opportunity))).all()
    assert only.status == ACTIVE and only.landed_interview is True


async def test_the_flag_is_never_cleared_when_the_role_moves_on_or_ends_or_goes_back(client):
    role_id = await _make()
    await _move(client, role_id, ACTIVE)
    for to, more in ((stages.REJECTED, {}), (stages.WITHDREW, {"ended_reason": stages.I_PASSED}),
                     (stages.SECOND_IMPRESSION, {}), (stages.APPLIED, {})):
        assert (await _move(client, role_id, to, **more)).status_code == 200
        assert await _flag(role_id) is True, to


async def test_unrelated_changes_to_a_flagged_role_keep_the_flag(js):
    role_id = await _make(status=ACTIVE)
    async with db.session_factory()() as session:
        flagged = await session.get(Opportunity, role_id)
        flagged.status, flagged.title = stages.GONE_QUIET, "Renamed"
        await session.commit()
        flagged.notes = "later"
        await session.commit()
    assert await _flag(role_id) is True


async def test_the_role_view_says_whether_it_landed_an_interview(client):
    plain, landed = await _make("a"), await _make("b", status=ACTIVE)
    assert (await client.get(f"/api/v1/roles/{plain}")).json()["role"]["landed_interview"] is False
    assert (await client.get(f"/api/v1/roles/{landed}")).json()["role"]["landed_interview"] is True
