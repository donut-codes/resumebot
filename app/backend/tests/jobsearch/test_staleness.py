"""A research keeps the versions of what it was made from, and a change to an input marks only the
results that depend on it (`staleness.py`, `requeue.reanalyse_stale`). Every name and number is made up."""

import copy
import uuid

from sqlalchemy import select

from app import db
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, requeue, staleness
from app.jobsearch.work import instruction_files
from app.models.work import TrailEvent
from tests.builders import RESUME, analysis_setup, findings
from tests.support import OWNER, PROFILE

RISK, KB = ["b-risk"], ["b-kb"]


def _required(cites):
    return [{"item": "Five years of programs", "class": "P", "why": "the master has it", "from": cites}]


async def _research(role_id, cites) -> None:
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, role_id))
        await analysis.submit(session, OWNER, findings(role_id, target_role="builders", required=_required(cites)))
        await session.commit()


async def _two():
    """Two researched roles: Fabrikam cites the risk line of the master, Initech the knowledge base line."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        for role_id in ids.values():
            (await session.get(Opportunity, role_id)).target_role = "builders"
        await session.commit()
    await _research(ids["Fabrikam"], RISK)
    await _research(ids["Initech"], KB)
    return ids


async def _reasons(role_id) -> list[str]:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id, populate_existing=True)
        return await staleness.analysis_reasons(session, role)


async def _save(kind, data) -> None:
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, kind, data, by="test")
        await session.commit()


def _resume(**lines) -> dict:
    out = copy.deepcopy(RESUME)
    for bullet in out["roles"][0]["bullets"]:
        if bullet["id"] in lines:
            bullet["text"] = lines[bullet["id"]]
    return out


async def test_a_research_keeps_the_versions_it_was_made_from(js):
    ids = await analysis_setup()
    await _research(ids["Fabrikam"], RISK)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        now = {k: await profile_store.newest_version(session, OWNER, k) for k in ("resume", "criteria", "gates",
                                                                                   "scoring")}
        assert role.analysis_made_under == {**now, "instructions": staleness.SHIPPED_EDITION["jobsearch.analyst"]}, (
            "the shipped analyst file is version 1 of edition 2")
        assert min(now.values()) >= 1


async def test_a_changed_line_nobody_cites_leaves_the_research_fresh(js):
    ids = await _two()
    await _save("resume", _resume(**{"b-kb": "Rebuilt the practice knowledge base"}))
    assert await _reasons(ids["Fabrikam"]) == []


async def test_a_changed_line_stales_only_the_research_that_cites_it(js):
    ids = await _two()
    await _save("resume", _resume(**{"b-kb": "Rebuilt the practice knowledge base"}))
    assert await _reasons(ids["Fabrikam"]) == []
    assert await _reasons(ids["Initech"]) == ["the master resume line b-kb it cites was changed"]


async def test_a_dropped_line_stales_the_research_that_cites_it(js):
    ids = await _two()
    gone = _resume()
    gone["roles"][0]["bullets"] = [b for b in gone["roles"][0]["bullets"] if b["id"] != "b-kb"]
    await _save("resume", gone)
    assert await _reasons(ids["Fabrikam"]) == []
    assert await _reasons(ids["Initech"]) == ["the master resume line b-kb it cites was dropped"]


async def test_a_line_changed_and_changed_back_is_not_stale(js):
    ids = await _two()
    await _save("resume", _resume(**{"b-kb": "Rebuilt the practice knowledge base"}))
    await _save("resume", _resume())
    assert await _reasons(ids["Initech"]) == []


async def test_a_change_to_the_target_role_stales_the_research_but_other_criteria_do_not(js):
    ids = await _two()
    criteria = copy.deepcopy(PROFILE["criteria"])
    await _save("criteria", {**criteria, "positioning": "Said another way."})
    assert await _reasons(ids["Fabrikam"]) == []
    role = {**criteria["target_roles"][0], "qualifications": "A degree and ten years."}
    await _save("criteria", {**criteria, "target_roles": [role]})
    assert await _reasons(ids["Fabrikam"]) == ["What to look for: its target role builders changed"]


async def test_a_target_role_the_code_or_haiku_pass_gave_does_not_stale_the_research_but_the_analysts_does(js):
    """Old: any role with a target role read as out of date when that role changed, whoever matched it. New: only a
    match the analyst or the owner made counts; the free title match and Haiku's pass are no reading of the role."""
    ids = await _two()
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["Fabrikam"])).matched_by = "code"
        (await session.get(Opportunity, ids["Initech"])).matched_by = "analyst"
        await session.commit()
    criteria = copy.deepcopy(PROFILE["criteria"])
    role = {**criteria["target_roles"][0], "qualifications": "A degree and ten years."}
    await _save("criteria", {**criteria, "target_roles": [role]})
    assert await _reasons(ids["Fabrikam"]) == []
    assert await _reasons(ids["Initech"]) == ["What to look for: its target role builders changed"]
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["Fabrikam"])).matched_by = "haiku"
        await session.commit()
    assert await _reasons(ids["Fabrikam"]) == []


async def test_research_made_under_the_first_edition_of_the_analyst_instructions_is_out_of_date(js):
    """The Proven rule was reworded in edition 2 (2026-10-21): a deck read stamped with the old file is read again,
    though the owner never saved a version."""
    ids = await analysis_setup()
    await _research(ids["Fabrikam"], RISK)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.analysis_made_under = {**role.analysis_made_under, "instructions": 1}
        await session.flush()
        assert (await staleness.analysis_reasons(session, role))[0].startswith(
            "the analyst's instructions changed (version 1 to 2)")


async def test_a_new_instruction_file_version_stales_the_research_made_under_the_old_one(js):
    ids = await _two()
    async with db.session_factory()() as session:
        text = await instruction_files.read(session, OWNER, "instructions", "jobsearch.analyst")
        await instruction_files.save(session, OWNER, "instructions", "jobsearch.analyst", text + "\nOne more line.",
                                     "test")
        await session.commit()
    assert (await _reasons(ids["Fabrikam"]))[0].startswith("the analyst's instructions changed (version 2 to 3)")


async def test_research_with_no_versions_kept_is_never_stale_and_never_queued(js):
    ids = await _two()
    async with db.session_factory()() as session:
        for role_id in ids.values():
            (await session.get(Opportunity, role_id)).analysis_made_under = None
        await session.commit()
    await _save("resume", _resume(**{"b-kb": "Rebuilt the practice knowledge base"}))
    for role_id in ids.values():
        assert await _reasons(role_id) == []
    async with db.session_factory()() as session:
        assert await requeue.reanalyse_stale(session, OWNER) == 0


async def test_reanalyse_stale_queues_exactly_the_stale_research_and_says_why(js):
    ids = await _two()
    await _save("resume", _resume(**{"b-kb": "Rebuilt the practice knowledge base"}))
    async with db.session_factory()() as session:
        assert await requeue.reanalyse_stale(session, OWNER, dry_run=True) == 1
        assert (await session.get(Opportunity, ids["Initech"])).analysis_state == analysis.DONE
        assert await requeue.reanalyse_stale(session, OWNER) == 1
        await session.commit()
    async with db.session_factory()() as session:
        states = {k: (await session.get(Opportunity, v, populate_existing=True)).analysis_state for k, v in ids.items()}
        assert states["Initech"] == analysis.WAITING
        assert states["Fabrikam"] == analysis.DONE and states["Hooli"] is None
        lines = (await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == ids["Initech"], TrailEvent.event == "research_requeued"))).all()
        assert len(lines) == 1 and "master resume line b-kb it cites was changed" in lines[0].detail
        assert await requeue.reanalyse_stale(session, OWNER) == 0, "waiting research is not queued twice"


async def test_reanalyse_stale_takes_back_a_researched_role_the_line_dropped_but_not_a_never_researched_one(js):
    ids = await _two()
    await _save("resume", _resume(**{"b-kb": "Rebuilt the practice knowledge base"}))
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Initech"])
        role.analysis_state = None  # taken out of the line by `stop_waiting` before it was fixed
        await session.commit()
    async with db.session_factory()() as session:
        assert await requeue.reanalyse_stale(session, OWNER, dry_run=True) == 1, "Hooli was never researched"
        assert await requeue.reanalyse_stale(session, OWNER) == 1
        await session.commit()
    async with db.session_factory()() as session:
        initech = await session.get(Opportunity, ids["Initech"], populate_existing=True)
        assert initech.analysis_state == analysis.WAITING
        assert (await session.get(Opportunity, ids["Hooli"], populate_existing=True)).analysis_state is None


async def test_redoing_a_research_stamps_the_new_versions_so_it_is_not_queued_again(js):
    ids = await _two()
    await _save("resume", _resume(**{"b-kb": "Rebuilt the practice knowledge base"}))
    async with db.session_factory()() as session:
        await requeue.reanalyse_stale(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        await analysis.submit(session, OWNER, findings(ids["Initech"], target_role="builders", required=_required(KB)))
        await session.commit()
    assert await _reasons(ids["Initech"]) == []


_EDITED = {"b-risk": "Led the risk group"}


async def _hand_out_research(session_id=None) -> Opportunity:
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity).where(Opportunity.analysis_state.is_(None)))).all():
            role.target_role = "builders"
            await analysis.start(session, OWNER, role)
        await session.flush()
        got = await analysis.analysis_reply(session, OWNER, session_id=session_id)
        await session.commit()
        return await session.get(Opportunity, uuid.UUID(got["role"]["id"]), populate_existing=True)


async def _submit_research(role_id, cites) -> None:
    async with db.session_factory()() as session:
        await analysis.submit(session, OWNER, findings(role_id, target_role="builders", required=_required(cites)))
        await session.commit()


async def test_a_research_is_stamped_with_the_versions_at_the_hand_out_not_at_the_submit(js):
    ids = await analysis_setup()
    role = await _hand_out_research()
    async with db.session_factory()() as session:
        handed = await profile_store.newest_version(session, OWNER, "resume")
    await _save("resume", _resume(**_EDITED))  # the owner edits the master while the analyst works
    await _submit_research(role.id, RISK)
    async with db.session_factory()() as session:
        kept = await session.get(Opportunity, role.id, populate_existing=True)
        assert kept.analysis_made_under["resume"] == handed
    assert await _reasons(role.id) == ["the master resume line b-risk it cites was changed"]
    assert ids


async def test_a_research_submitted_with_no_hand_out_stamp_is_stamped_at_the_submit(js):
    ids = await analysis_setup()
    await _research(ids["Fabrikam"], RISK)  # started, never handed out
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        now = await profile_store.newest_version(session, OWNER, "resume")
        assert role.analysis_made_under["resume"] == now


async def test_a_research_handed_out_again_takes_a_fresh_stamp(js):
    await analysis_setup()
    first = await _hand_out_research("s1")
    await _save("resume", _resume(**_EDITED))
    async with db.session_factory()() as session:
        await analysis.release_claims(session, OWNER, "s1")
        await session.commit()
    again = await _hand_out_research("s2")
    assert again.id == first.id
    async with db.session_factory()() as session:
        now = await profile_store.newest_version(session, OWNER, "resume")
    assert again.analysis[analysis.HANDED_UNDER_KEY]["resume"] == now
    await _submit_research(again.id, RISK)
    assert await _reasons(again.id) == []
