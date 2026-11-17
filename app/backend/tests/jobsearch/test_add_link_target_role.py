"""A job the owner added by its link is matched to one of their target roles (docs/PRD.md 24.10), so that role's own pay
floor, gates and asks apply to it as they do to a find of the scout's. The analyst does the matching, in the same
findings that name the role: the task lists the target roles, the asks and the gates with the roles each is for; the
findings send `target_role` and answer the asks and rules of that role; the role has it before its score is worked out
and its gates run again. A job that matches none stays on the gates and asks for every role, and its history says so.
Every name, number and link is made up."""

import copy
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import text as posting_text
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.models.work import TrailEvent
from tests.builders import added_link_role, findings, save_documents, submit_findings
from tests.support import LOCATIONS, OWNER, POSTING_WORDS, answered, load_profile

LINK = "https://jobs.example.test/jobs/7001"
BUILDERS = {"id": "builders", "name": "Builders", "titles": ["Director of Programs"],
            "description": "Builds and runs programs: plans them, staffs them and reports on them."}
ANALYSTS = {"id": "analysts", "name": "Analysts", "titles": ["Data Analyst"],
            "description": "Turns numbers into decisions."}
CRITERIA = {"company": "A public agency.", "target_roles": [BUILDERS, ANALYSTS]}
#: One ask for every role (2 points), one for Builders only (2) and one for Analysts only (1): Builders can earn 4,
#: Analysts 3, and a job that matches neither 2.
ASKS = {"asks": [
    {"id": "ask-a", "section": "description", "says": "The work is building programs", "points": 2},
    {"id": "ask-b", "section": "qualifications", "says": "Asks for a degree", "points": 2, "roles": ["builders"]},
    {"id": "ask-c", "section": "qualifications", "says": "Asks for statistics", "points": 1, "roles": ["analysts"]},
]}
#: All's pay: at least $90,000 on the posted minimum. Builders' own tab adds $120,000, and a sentence about nights.
GATES = {
    "location": {"remote": {"on": True}, "hybrid": {"on": True, "locations": LOCATIONS},
                 "in_person": {"on": True, "locations": LOCATIONS}},
    "compensation": {"amount": 90_000, "against": "minimum"},
    "other": [
        {"id": "travel", "kind": "rule", "section": "responsibilities", "text": "Travel of 20% or more."},
        {"id": "on-call", "kind": "rule", "section": "responsibilities", "text": "On call at night.",
         "roles": ["builders"]},
    ],
    "by_role": {"builders": {"compensation": {"amount": 120_000, "against": "minimum"}}},
}
#: A pay that clears All's floor and not Builders': the same job is gated by the role it is matched to.
BETWEEN_THE_FLOORS = {"band": [100000, 105000], "band_source": "the posting"}
ABOVE_BOTH = {"band": [130000, 140000], "band_source": "the posting"}
PLACE = {"location": "Remote, US", "work_mode": "remote", "title": "Director of Programs", "company": "Umbrella Works"}


async def _setup(criteria: dict | None = None) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=copy.deepcopy(CRITERIA if criteria is None else criteria),
                           gates=copy.deepcopy(GATES), scoring=copy.deepcopy(ASKS))
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        await session.commit()


async def _added_and_waiting(client) -> str:
    """A role made from an added link (the shape such roles had before 2026-10-15); nobody can read the posting, so it
    waits for its text unresearched."""
    role_id = await added_link_role(LINK)
    held = await _role(role_id)
    assert held.sourced_at is not None and held.analysis_state == analysis.NO_TEXT, "its link is its source"
    return role_id


async def _added(client) -> tuple[str, dict]:
    """The owner adds a link, the owner's browser brings the posting's text, and the analyst's session is handed the
    role to research; returns the role's id and its task."""
    role_id = await _added_and_waiting(client)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, uuid.UUID(role_id))
        assert await posting_text.take(
            session, role, posting_text.Capture(POSTING_WORDS)) == posting_text.FETCHED
        assert await analysis.resume_waiting(session, OWNER, [role.id]) == 1
        await session.commit()
    return role_id, await _task(role_id)


async def _task(role_id: str) -> dict:
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER)
        await session.commit()
    assert task["role"]["id"] == role_id
    return task


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, uuid.UUID(str(role_id)), populate_existing=True)


async def _lines(role_id, event: str) -> list[TrailEvent]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == uuid.UUID(str(role_id)), TrailEvent.event == event)
            .order_by(TrailEvent.at, TrailEvent.id))).all())


async def _line() -> list:
    async with db.session_factory()() as session:
        return await analysis._eligible(session, OWNER, datetime.now(UTC))


def _rows(role: Opportunity) -> list[str]:
    """The asks the role's score was worked out on."""
    return [row["id"] for row in role.fit_breakdown["asks"]]


# --- the task the analyst is handed -------------------------------------------------------------------------------


async def test_the_task_lists_every_target_role_and_the_asks_and_gates_with_the_roles_each_is_for(client):
    await _setup()
    _, task = await _added(client)
    assert "match_the_role" in task and "target_role" in task["match_the_role"]
    sent = {r["id"]: r for r in task["what_to_look_for"]["target_roles"]}
    assert list(sent) == ["builders", "analysts"], "every target role, in full: not one role's picture"
    assert sent["analysts"]["name"] == "Analysts" and sent["analysts"]["titles"] == ["Data Analyst"]
    assert sent["analysts"]["description"] == "Turns numbers into decisions."
    asks = {a["id"]: a.get("only_for_roles") for s in task["scoring"]["sections"] for a in s["asks"]}
    assert asks == {"ask-a": None, "ask-b": ["builders"], "ask-c": ["analysts"]}
    assert {g["id"]: g.get("only_for_roles") for g in task["gates"]} == {
        "location": None, "compensation": None, "compensation:builders": ["builders"], "travel": None,
        "on-call": ["builders"]}
    assert task["role"]["title"] is None and "name_the_role" in task["role"], "it is still named from the posting"


async def test_a_job_with_no_target_roles_to_match_it_to_is_asked_nothing_more(client):
    await _setup({"company": "A public agency.", "target_roles": []})
    role_id, task = await _added(client)
    assert "match_the_role" not in task
    assert (await submit_findings(findings(role_id, **PLACE, fit_breakdown=answered(), pay=ABOVE_BOTH)))["taken"]
    role = await _role(role_id)
    assert role.target_role is None and role.matched_by_version is None
    assert await _lines(role_id, "role_matched") == [], "there was no target role to match it to: nothing to say"


# --- matched to a target role -------------------------------------------------------------------------------------


async def test_a_job_matched_to_a_role_is_gated_by_that_roles_own_pay_floor_and_scored_with_its_asks(client):
    await _setup()
    role_id, _ = await _added(client)
    reply = await submit_findings(findings(
        role_id, **PLACE, target_role="builders", pay=BETWEEN_THE_FLOORS, fit_breakdown=answered("met", "not", "met")))
    assert reply["taken"] is True and reply["stage"] == stages.GATED_OUT
    role = await _role(role_id)
    assert role.target_role == "builders"
    assert role.matched_by_version == await _criteria_version()
    assert role.status == stages.GATED_OUT and "$120,000" in role.screened_out_reason, (
        "$100,000 clears All's $90,000 and not Builders' own $120,000")
    # The score is Builders' own: its asks, out of what Builders can earn, whatever else the analyst answered.
    assert _rows(role) == ["ask-a", "ask-b"] and role.fit_breakdown["possible"] == 4
    assert role.fit == 2 and role.analysis_state == analysis.DONE
    said = await _lines(role_id, "role_matched")
    assert len(said) == 1 and "Builders" in said[0].sentence and "gates and asks apply" in said[0].sentence
    assert await _line() == [], "matched in the same research: nothing is queued a second time"


async def test_the_same_job_matched_to_a_role_with_no_floor_of_its_own_clears_the_pay_gate_and_is_scored_on_its_asks(
        client):
    await _setup()
    role_id, _ = await _added(client)
    reply = await submit_findings(findings(
        role_id, **PLACE, target_role="analysts", pay=BETWEEN_THE_FLOORS, fit_breakdown=answered("met", "not", "met")))
    # Analysts can earn 3 and this earns all 3: over the 75% drafting bar. On Builders' asks it would be 50%.
    assert reply["taken"] is True and reply["stage"] == stages.READY_TO_LAUNCH
    role = await _role(role_id)
    assert role.target_role == "analysts" and role.screened_out_reason is None
    assert _rows(role) == ["ask-a", "ask-c"] and role.fit_breakdown["possible"] == 3 and role.fit == 3


async def test_the_role_shows_in_the_pipeline_of_the_target_role_it_matched(client):
    await _setup()
    role_id, _ = await _added(client)
    await submit_findings(findings(
        role_id, **PLACE, target_role="analysts", pay=BETWEEN_THE_FLOORS, fit_breakdown=answered("met", "not", "met")))

    async def shown(tab: str) -> list[str]:
        columns = (await client.get("/api/v1/board", params={"role": tab})).json()["columns"]
        return [card["id"] for column in columns for card in column["cards"]]

    assert role_id in await shown("analysts") and role_id not in await shown("builders")


# --- matched to none ----------------------------------------------------------------------------------------------


async def test_a_job_that_matches_no_target_role_uses_the_gates_and_asks_for_every_role_and_says_so(client):
    await _setup()
    role_id, _ = await _added(client)
    reply = await submit_findings(findings(
        role_id, **PLACE, target_role="none", pay=BETWEEN_THE_FLOORS, fit_breakdown=answered("met", "not", "met")))
    # Builders' $120,000 is not this job's: it clears All's $90,000 and scores on ask-a.
    assert reply["taken"] is True and reply["stage"] == stages.READY_TO_LAUNCH
    role = await _role(role_id)
    assert role.target_role is None and role.matched_by == "analyst" and role.screened_out_reason is None
    assert role.matched_by_version == await _criteria_version()
    assert _rows(role) == ["ask-a"] and role.fit_breakdown["possible"] == 2 and role.fit == 2
    said = await _lines(role_id, "role_matched")
    assert [e.sentence for e in said] == [
        "No target role of yours matched this job, so the gates and asks for every role apply."]


async def test_a_job_the_analyst_matched_to_none_or_to_a_role_is_not_offered_the_match_again(client):
    """The analyst's answer is final (the owner, 2026-11-12): researched again, a role it matched, to a role or to none,
    is not asked to be matched again."""
    await _setup()
    role_id, _ = await _added(client)
    await submit_findings(findings(role_id, **PLACE, target_role="none", pay=ABOVE_BOTH, fit_breakdown=answered()))
    async with db.session_factory()() as session:
        await analysis.ask(session, OWNER, uuid.UUID(role_id))
        await session.commit()
    again = await _task(role_id)
    assert "match_the_role" not in again, "the analyst already matched it to none"
    assert (await _role(role_id)).matched_by == "analyst"

    async with db.session_factory()() as session:
        role = await session.get(Opportunity, uuid.UUID(role_id))
        role.matched_by = "haiku"  # as if a later pass of a lower tier had set it: the analyst is asked again
        await session.commit()
    await submit_findings(findings(role_id, **PLACE, target_role="builders", pay=ABOVE_BOTH, fit_breakdown=answered()))
    assert (await _role(role_id)).target_role == "builders"
    async with db.session_factory()() as session:
        await analysis.ask(session, OWNER, uuid.UUID(role_id))
        await session.commit()
    third = await _task(role_id)
    assert "match_the_role" not in third, "a matched job is not matched again"
    assert third["what_to_look_for"]["target_role"]["id"] == "builders"
    assert sorted(g["id"] for g in third["gates"]) == sorted(["location", "compensation", "compensation:builders",
                                                              "travel", "on-call"])


# --- what the analyst must say ------------------------------------------------------------------------------------


async def test_findings_that_say_nothing_of_the_target_role_are_sent_back_and_the_role_is_left_as_it_was(client):
    await _setup()
    role_id, _ = await _added(client)
    sent = findings(role_id, **PLACE, target_role=None, pay=ABOVE_BOTH, fit_breakdown=answered())
    reply = await submit_findings(sent)
    assert reply["taken"] is False and "target_role" in reply["reason"]
    assert "builders, analysts" in reply["reason"] and "or none" in reply["reason"]
    role = await _role(role_id)
    assert (role.target_role, role.analysis_state, role.status) == (None, analysis.WAITING, stages.SECOND_IMPRESSION)
    assert await _lines(role_id, "role_matched") == []
    assert (await submit_findings({**sent, "target_role": "analysts"}))["taken"] is True


async def test_a_target_role_that_is_not_the_owners_is_refused_naming_the_ones_they_have(client):
    await _setup()
    role_id, _ = await _added(client)
    reply = await submit_findings(findings(
        role_id, **PLACE, target_role="interns", pay=ABOVE_BOTH, fit_breakdown=answered()))
    assert reply["taken"] is False
    assert "'interns' is not one of the owner's target roles (builders, analysts)" in reply["reason"]
    assert (await _role(role_id)).target_role is None


async def test_a_posting_nobody_could_read_is_not_researched_and_the_role_is_matched_by_no_one(client):
    await _setup()
    role_id = await _added_and_waiting(client)
    role = await _role(role_id)
    assert role.target_role is None and role.analysis_state == analysis.NO_TEXT and not role.analysis.get("required")
    assert await _lines(role_id, "role_matched") == [], "nothing was read, so nothing was matched"


# --- the scout's finds are matched by the analyst too -------------------------------------------------------------


async def test_a_find_of_the_scout_is_matched_by_the_analyst_which_overrules_an_earlier_pass(client):
    """A scout's find is asked to be matched too, whatever the title check or the small title session said: the
    analyst's answer is final."""
    await _setup()
    async with db.session_factory()() as session:
        roles = {}
        for company, target, by in (("Globex", "analysts", "haiku"), ("Initech", None, None)):
            roles[company] = Opportunity(
                owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", origin="scout",
                match_key=f"{company.lower()}|programs lead", status=stages.SECOND_IMPRESSION, target_role=target,
                matched_by=by, found=date.today(), location="Remote, US", move_type="remote",
                description=POSTING_WORDS, url=f"https://jobs.example.test/{company.lower()}")
            session.add(roles[company])
        await session.flush()
        ids = {company: role.id for company, role in roles.items()}
        for role in roles.values():
            await analysis.start(session, OWNER, role)
        await session.commit()
    for _ in ids:
        async with db.session_factory()() as session:
            task = await analysis.analysis_reply(session, OWNER)
            await session.commit()
        assert "match_the_role" in task and "target_role" in task["match_the_role"]
        looked = task["what_to_look_for"]
        if "target_roles" in looked:  # no earlier pass matched it: every target role is handed
            assert {r["id"] for r in looked["target_roles"]} == {"builders", "analysts"}
        else:  # an earlier pass matched it: that role is handed to confirm or overrule, the others beside it
            assert looked["target_role"]["id"] == "analysts"
            assert {r["id"] for r in task["other_target_roles"]} == {"builders"}
        taken = task["role"]["id"]
        reply = await submit_findings(findings(
            taken, pay=ABOVE_BOTH, target_role="builders", fit_breakdown=answered("met", "not", "met")))
        assert reply["taken"] is True
    for company in ids:
        role = await _role(ids[company])
        assert (role.target_role, role.matched_by) == ("builders", "analyst")
        assert len(await _lines(ids[company], "role_matched")) == 1


async def _criteria_version() -> int:
    async with db.session_factory()() as session:
        return await profile_store.newest_version(session, OWNER, "criteria")
