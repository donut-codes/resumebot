"""The analyst's pass of the target-role match (docs/PRD.md 30.5): it is asked to match every role it researches,
whoever matched it before, its answer is final, and it is written only when the findings really give one. Every name is
made up."""

import copy
import uuid

from sqlalchemy import select

from app import db
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work import tagging
from app.models.work import TrailEvent
from tests.builders import analysis_setup, findings
from tests.support import OWNER, PROFILE

HARBOR = {"id": "harbor-ledgering", "name": "Harbor ledgering", "titles": ["Harbor Ledger Clerk"],
          "description": "Keeps the ledgers of a working harbor."}
WHEEL = {"id": "wheelwrighting", "name": "Wheelwrighting", "titles": ["Wheelwright"],
         "description": "Builds and mends wheels."}


async def _setup() -> dict:
    """Three scout roles in Second Impression, and a profile whose target roles are two made-up ones."""
    ids = await analysis_setup()
    criteria = {**copy.deepcopy(PROFILE["criteria"]), "target_roles": [HARBOR, WHEEL]}
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "criteria", criteria, by="test")
        await session.commit()
    return ids


async def _set(role_id, **fields) -> None:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        for key, value in fields.items():
            setattr(role, key, value)
        await session.commit()


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _lines(role_id) -> list[str]:
    async with db.session_factory()() as session:
        return [e.sentence for e in await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == uuid.UUID(str(role_id)), TrailEvent.event == "role_matched"))]


async def _submit(role_id, **over) -> dict:
    """Hand the role to the analyst and send its findings (no pipeline turn: only the reply is read)."""
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        if role.analysis_state is None:
            await analysis.start(session, OWNER, role)
        reply = await analysis.submit(session, OWNER, findings(role_id, **over))
        await session.commit()
        return reply


async def _task(role_id) -> dict:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        if role.analysis_state is None:
            await analysis.start(session, OWNER, role)
        await session.flush()
        return await analysis.brief_part(session, OWNER, role, "task")


async def test_to_match_is_true_unless_the_analyst_matched_the_role_or_the_owner_has_no_target_role(js):
    ids = await _setup()
    async with db.session_factory()() as session:
        profile = await profile_store.current(session, OWNER)
        role = await session.get(Opportunity, ids["Fabrikam"])
        got = {}
        for by in (None, "code", "haiku", "analyst"):
            role.matched_by = by
            got[by] = analysis.to_match(role, profile)
        assert got == {None: True, "code": True, "haiku": True, "analyst": False}
        none_profile = copy.deepcopy(profile)
        none_profile.criteria.target_roles = []
        role.matched_by = None
        assert analysis.to_match(role, none_profile) is False


async def test_the_task_asks_for_the_match_of_a_haiku_matched_role_and_not_of_an_analyst_matched_one(js):
    ids = await _setup()
    await _set(ids["Fabrikam"], target_role="wheelwrighting", matched_by="haiku")
    await _set(ids["Initech"], target_role="wheelwrighting", matched_by="analyst")
    asked = await _task(ids["Fabrikam"])
    assert "match_the_role" in asked and "overrule" in asked["match_the_role"]
    # handed the role it judges as the starting point, the others beside it (PRD 24.10, 30.5)
    assert asked["what_to_look_for"]["target_role"]["id"] == "wheelwrighting"
    assert "target_roles" not in asked["what_to_look_for"] and "confirm" in asked["match_the_role"]
    assert {r["id"] for r in asked["other_target_roles"]} == {"harbor-ledgering"}
    final = await _task(ids["Initech"])
    assert "match_the_role" not in final and final["what_to_look_for"]["target_role"]["id"] == "wheelwrighting"


async def test_findings_with_no_target_role_for_a_scout_role_are_refused_and_change_nothing(js):
    ids = await _setup()
    await _set(ids["Fabrikam"], target_role="wheelwrighting", matched_by="haiku")
    reply = await _submit(ids["Fabrikam"], target_role=None)
    assert reply["taken"] is False and "say which target role" in reply["reason"]
    role = await _role(ids["Fabrikam"])
    assert (role.target_role, role.matched_by) == ("wheelwrighting", "haiku") and await _lines(ids["Fabrikam"]) == []


async def test_a_valid_role_is_stored_as_the_analysts_with_the_version_and_one_trail_line(js):
    ids = await _setup()
    await _set(ids["Fabrikam"], target_role="wheelwrighting", matched_by="haiku")
    assert (await _submit(ids["Fabrikam"], target_role="harbor-ledgering"))["taken"] is True
    role = await _role(ids["Fabrikam"])
    async with db.session_factory()() as session:
        newest = await profile_store.newest_version(session, OWNER, "criteria")
    assert (role.target_role, role.matched_by, role.matched_by_version) == ("harbor-ledgering", "analyst", newest)
    assert len(await _lines(ids["Fabrikam"])) == 1


async def test_an_explicit_none_is_stored_as_the_analysts_with_the_version_and_one_trail_line(js):
    ids = await _setup()
    await _set(ids["Fabrikam"], target_role="wheelwrighting", matched_by="haiku")
    assert (await _submit(ids["Fabrikam"], target_role="none"))["taken"] is True
    role = await _role(ids["Fabrikam"])
    async with db.session_factory()() as session:
        newest = await profile_store.newest_version(session, OWNER, "criteria")
    assert (role.target_role, role.matched_by, role.matched_by_version) == (None, "analyst", newest)
    assert len(await _lines(ids["Fabrikam"])) == 1


async def test_findings_taken_after_three_refusals_with_an_invalid_id_keep_the_haiku_match(js):
    ids = await _setup()
    await _set(ids["Fabrikam"], target_role="wheelwrighting", matched_by="haiku", matched_by_version=1)
    replies = [await _submit(ids["Fabrikam"], target_role="no-such-role") for _ in range(analysis.MAX_REFUSALS)]
    assert [r["taken"] for r in replies] == [False, False, True]
    role = await _role(ids["Fabrikam"])
    assert (role.target_role, role.matched_by, role.matched_by_version) == ("wheelwrighting", "haiku", 1)
    assert await _lines(ids["Fabrikam"]) == []


async def test_the_analysts_match_makes_the_role_count_in_the_trend_of_its_target_role(js):
    """Reproduction: three scout roles with no target role count for none; once the analyst has matched them they count
    for the role it picked."""
    ids = await _setup()
    async with db.session_factory()() as session:
        assert (await tagging.trend(session, OWNER, "harbor-ledgering"))["total"] == 0
    for role_id in ids.values():
        assert (await _submit(role_id, target_role="harbor-ledgering"))["taken"] is True
    async with db.session_factory()() as session:
        assert (await tagging.trend(session, OWNER, "harbor-ledgering"))["total"] == 3
