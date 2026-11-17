"""The owner asks research of a chosen role (docs/PRD.md 39.28): any stage, Ended and Gated out included. The role is
queued for the analyst through the same process, claims and one-site rule as every research and goes first in the line;
a role researched already is researched again with a line in its history, which names the owner's class corrections that
go with the old research; an Ended, Applied, Active Discussion or Gated out role keeps its stage and its reason, and the
line says what research does to the role's stage by stage; asking twice queues it once; the night's own selection is
the same as before. Every name, number and sentence is made up."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from process_engine.models import ProcInstance
from sqlalchemy import func, select, update

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.imports import tracker
from app.jobsearch.missions import catch_up
from app.jobsearch.missions import core as missions
from app.jobsearch.missions.kinds import analyse
from app.jobsearch.models import MissionRun, Opportunity, SearchRun
from app.jobsearch.research import analysis, line
from app.jobsearch.work import session_kinds
from app.models.work import TrailEvent
from tests.builders import analysis_setup, findings, save_documents, submit_findings
from tests.support import FIVE, OWNER, POSTING_WORDS, PROFILE, load_profile, tracker_row, tracker_workbook

#: A pay under the made-up floor of $95,000: a gate run again on it would stop the role.
FAILS_THE_FLOOR = {"band": [60000, 70000]}
#: The stages a role keeps whatever research finds: none of them is before Ready to Launch, and a re-gate reaches
#: none of them.
KEPT = [stages.REJECTED, stages.GONE_QUIET, stages.WITHDREW, stages.APPLIED, stages.ACTIVE_DISCUSSION]


def _research(role_id) -> str:
    return f"/api/v1/roles/{role_id}/research"


async def _set(role_id, **fields) -> None:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        for column, value in fields.items():
            setattr(role, column, value)
        await session.commit()


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _lines(role_id, event: str | None = None) -> list[TrailEvent]:
    """The role's history, oldest first."""
    async with db.session_factory()() as session:
        query = select(TrailEvent).where(TrailEvent.link_id == role_id).order_by(TrailEvent.at, TrailEvent.id)
        if event:
            query = query.where(TrailEvent.event == event)
        return list((await session.scalars(query)).all())


async def _instances(role_id) -> int:
    async with db.session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(ProcInstance).where(
            ProcInstance.subject == f"jobsearch:analysis:{role_id}")) or 0)


async def _line() -> list:
    """The ids a session would be handed from, in order (`analysis.claim_next` takes the first)."""
    async with db.session_factory()() as session:
        return await analysis._eligible(session, OWNER, datetime.now(UTC))


async def _night_starts(*role_ids) -> None:
    """The night's research puts these roles in line, in this order (the first has waited longest)."""
    async with db.session_factory()() as session:
        for role_id in role_ids:
            await analysis.start(session, OWNER, await session.get(Opportunity, role_id))
        await session.commit()


# --- any stage: queued, researched, and the role stays where it is ---------------------------------------------


@pytest.mark.parametrize("stage", KEPT)
async def test_a_role_outside_found_is_queued_researched_and_keeps_its_stage_whatever_the_gates_say_now(
        client, knocks, stage):
    ids = await analysis_setup()
    role_id = ids["Fabrikam"]
    await _set(role_id, status=stage, ended_reason=stages.default_end_reason(stage))

    asked = await client.post(_research(role_id))
    assert asked.status_code == 200
    body = asked.json()
    assert (body["role"], body["state"], body["already"], body["again"]) == (str(role_id), "waiting", False, False)
    assert "queued for research" in body["said"]
    assert knocks == [(str(role_id), "analysis_asked")], "the scout is woken once, for this role"
    role = await _role(role_id)
    assert role.analysis_state == analysis.WAITING and role.status == stage

    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS, "the analyst's lane has work"
        task = await analysis.analysis_reply(session, OWNER)
        await session.commit()
    assert task["role"]["id"] == str(role_id)

    # A pay that fails the made-up floor: had the gates run again on a role outside Found it would be stopped.
    reply = await submit_findings(findings(role_id, pay=FAILS_THE_FLOOR))
    assert reply["taken"] is True and reply["stage"] == stage
    role = await _role(role_id)
    assert (role.status, role.ended_reason) == (stage, stages.default_end_reason(stage))
    assert (role.analysis_state, role.screened_out_reason) == (analysis.DONE, None)
    assert role.analysis["required"], "the research is recorded on the role"
    events = [e.event for e in await _lines(role_id)]
    assert events.count("research_asked") == 1 and events.count("analysed") == 1
    assert not {"regate_stopped", "regate_released", "regate_restated"} & set(events), "no gate was run on it"
    said = (await _lines(role_id, "analysed"))[0].sentence
    assert f"it stays in {stages.LABELS[stage]}" in said


@pytest.mark.parametrize("band", [FAILS_THE_FLOOR, {"band": [150000, 180000]}], ids=["pay now fails", "pay now passes"])
async def test_a_gated_out_role_is_researched_and_keeps_its_stage_and_its_reason(client, band):
    ids = await analysis_setup()
    role_id = ids["Initech"]
    stop = {"status": stages.GATED_OUT, "screened_out_reason": "Not a place the owner works",
            "screened_out_by": "location", "screened_out_step": "gate", "screened_out_gate_id": "location",
            "screened_out_hard_pass": 1, "screened_out_criteria": 1}
    await _set(role_id, **stop)

    assert (await client.post(_research(role_id))).json()["state"] == "waiting"
    reply = await submit_findings(findings(role_id, pay=band))
    assert reply["taken"] is True and reply["stage"] == stages.GATED_OUT

    role = await _role(role_id)
    assert {k: getattr(role, k) for k in stop} == stop, "the stage and the whole of the stop are as they were"
    assert role.analysis_state == analysis.DONE and role.analysed_at is not None
    assert not [e for e in await _lines(role_id) if e.event.startswith("regate_")]
    assert "it stays in Gated out" in (await _lines(role_id, "analysed"))[0].sentence


async def test_a_role_in_ready_to_launch_is_still_gated_again_after_research_as_it_is_when_the_catch_up_researches_it(
        client):
    """The boundary of the above: the re-gate of a Ready to Launch role after research is unchanged."""
    ids = await analysis_setup()
    await _set(ids["Fabrikam"], status=stages.READY_TO_LAUNCH)
    await client.post(_research(ids["Fabrikam"]))
    reply = await submit_findings(findings(ids["Fabrikam"], pay=FAILS_THE_FLOOR))
    assert reply["stage"] == stages.GATED_OUT
    assert (await _role(ids["Fabrikam"])).screened_out_reason.startswith("Pay must be at least $95,000 a year")


async def test_a_role_before_ready_to_launch_the_owner_asks_for_goes_up_when_there_is_room_as_any_research_does(
        client):
    ids = await analysis_setup()
    await client.post(_research(ids["Fabrikam"]))
    reply = await submit_findings(findings(ids["Fabrikam"]))
    assert reply["stage"] == stages.READY_TO_LAUNCH, "researched, no gate fails and the list has room"


# --- researched again ------------------------------------------------------------------------------------------


async def test_a_role_researched_already_is_researched_again_the_new_research_replaces_the_old_and_history_says_so(
        client):
    ids = await analysis_setup()
    role_id = ids["Fabrikam"]
    await _set(role_id, status=stages.REJECTED, ended_reason=stages.THEY_SAID_NO)
    await client.post(_research(role_id))
    await submit_findings(findings(role_id, notes="First reading."))
    first = await _role(role_id)
    assert first.analysis_state == analysis.DONE and first.analysis["notes"] == "First reading."
    assert "asked_by" not in first.analysis, "the mark goes when the findings are taken"

    again = (await client.post(_research(role_id))).json()
    assert (again["state"], again["already"], again["again"]) == ("waiting", False, True)
    assert "researched again" in again["said"]
    waiting = await _role(role_id)
    assert waiting.analysis_state == analysis.WAITING and waiting.status == stages.REJECTED
    assert waiting.analysis["notes"] == "First reading.", "the old research is still the page's until the new one comes"
    assert await _instances(role_id) == 2, "a new run of the process, beside the one that finished"

    second = findings(role_id, notes="Second reading.", required=[{"item": "One thing", "class": "U"}])
    assert (await submit_findings(second))["taken"] is True
    role = await _role(role_id)
    assert role.analysis_state == analysis.DONE and role.status == stages.REJECTED
    assert role.analysis["notes"] == "Second reading."
    assert [r["item"] for r in role.analysis["required"]] == ["One thing"]
    assert "asked_by" not in role.analysis

    asked = [e.sentence for e in await _lines(role_id, "research_asked")]
    assert asked == ["You asked for research of Programs Lead at Fabrikam.",
                     "You asked for Programs Lead at Fabrikam to be researched again."]
    researched = [e.sentence for e in await _lines(role_id, "analysed")]
    assert len(researched) == 2 and "again" not in researched[0]
    assert "researched Programs Lead at Fabrikam again" in researched[1]
    detail = (await _lines(role_id, "research_asked"))[1].detail
    assert "replaces" in detail and "correction" not in detail, "none of the owner's corrections was there to go"


async def test_the_owners_class_corrections_go_with_the_old_research_and_the_history_of_the_ask_says_so(client):
    ids = await analysis_setup()
    role_id = ids["Fabrikam"]
    await _set(role_id, status=stages.REJECTED, ended_reason=stages.THEY_SAID_NO)
    await client.post(_research(role_id))
    await submit_findings(findings(role_id))

    async def ask_again() -> str:
        assert (await client.post(_research(role_id))).json()["again"] is True
        asked = await _lines(role_id, "research_asked")
        return asked[-1].detail

    fixed = await client.post(f"/api/v1/roles/{role_id}/requirements", json={"index": 0, "class": "U", "why": "no"})
    assert fixed.status_code == 200
    assert [r.get("by") for r in (await _role(role_id)).analysis["required"]] == ["owner", "analyst"]
    assert "your correction of a required item's class go with it" in await ask_again()

    # The analyst has not answered yet: the old research, and the correction, are still the role's page.
    assert (await _role(role_id)).analysis["required"][0]["by"] == "owner"
    await submit_findings(findings(role_id))
    assert [r.get("by") for r in (await _role(role_id)).analysis["required"]] == ["analyst", "analyst"], (
        "the new research replaced the whole of the old, the correction with it")

    for index in (0, 1):
        await client.post(f"/api/v1/roles/{role_id}/requirements", json={"index": index, "class": "T", "why": "no"})
    assert "your 2 corrections of a required item's class go with it" in await ask_again()


# --- what the history of the ask says a research does to the stage, stage by stage ----------------------------


@pytest.mark.parametrize("stage", [*KEPT, stages.GATED_OUT])
async def test_the_history_of_an_ask_says_a_role_outside_found_or_the_gated_stages_is_moved_nowhere(client, stage):
    ids = await analysis_setup()
    await _set(ids["Fabrikam"], status=stage)
    await client.post(_research(ids["Fabrikam"]))
    detail = (await _lines(ids["Fabrikam"], "research_asked"))[0].detail
    assert detail == f"It stays in {stages.LABELS[stage]}: research is recorded on a role there and moves it nowhere."


@pytest.mark.parametrize("stage", analysis.GATED_AGAIN)
async def test_the_history_of_an_ask_of_a_role_the_gates_run_on_again_says_so_and_not_that_it_moves_nowhere(
        client, stage):
    ids = await analysis_setup()
    await _set(ids["Fabrikam"], status=stage)
    await client.post(_research(ids["Fabrikam"]))
    detail = (await _lines(ids["Fabrikam"], "research_asked"))[0].detail
    assert detail.startswith(f"It is in {stages.LABELS[stage]} now: the gates run again after the research")
    assert "Gated out" in detail and "nowhere" not in detail


async def test_the_history_of_an_ask_of_a_found_role_has_no_word_of_the_stage(client):
    ids = await analysis_setup()
    await client.post(_research(ids["Fabrikam"]))
    asked = (await _lines(ids["Fabrikam"], "research_asked"))[0]
    assert asked.detail is None and asked.sentence == "You asked for research of Programs Lead at Fabrikam."


async def test_a_research_that_failed_starts_afresh_when_the_owner_asks_for_it(client):
    ids = await analysis_setup()
    role_id = ids["Hooli"]
    await _set(role_id, analysis_state=analysis.FAILED,
               analysis={"refused": 2, "claims": 3, "unread_asked": True, "notes": "kept"})
    again = (await client.post(_research(role_id))).json()
    assert (again["state"], again["again"]) == ("waiting", False), "nothing was researched, so it is not again"
    role = await _role(role_id)
    assert role.analysis_state == analysis.WAITING
    assert role.analysis == {"notes": "kept", "asked_by": "owner"}, "no refusal, claim or unread mark carries over"


async def test_an_owner_asked_role_the_app_sends_back_to_the_analyst_is_still_owner_asked_and_runs_by_day(client):
    ids = await analysis_setup()
    role_id = ids["Hooli"]
    await _set(role_id, analysis_state=analysis.FAILED, analysis={"asked_by": "owner", "claims": 3, "notes": "kept"})
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, role_id))
        await session.commit()
    role = await _role(role_id)
    assert (role.analysis_state, line.asked_by_owner(role)) == (analysis.WAITING, True), "a restart keeps the ask"
    assert "claims" not in role.analysis, "the rest of a fresh try is still fresh"
    assert role.analysis_claimed_at is None, "no session holds it: the day's pool hands it out"


async def test_a_restart_never_adds_an_owner_ask_to_an_ordinary_role(client):
    ids = await analysis_setup()
    await _set(ids["Hooli"], analysis_state=analysis.FAILED, analysis={"notes": "kept"})
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Hooli"]))
        await session.commit()
    assert not line.asked_by_owner(await _role(ids["Hooli"]))


# --- the owner's asks go first ----------------------------------------------------------------------------------


async def test_the_roles_the_owner_asked_for_go_first_in_the_line_in_the_order_asked(client):
    ids = await analysis_setup()
    await _night_starts(ids["Fabrikam"], ids["Initech"])  # the night's: Fabrikam has waited longest
    assert await _line() == [ids["Fabrikam"], ids["Initech"]]

    await _set(ids["Hooli"], status=stages.REJECTED)
    assert (await client.post(_research(ids["Hooli"]))).json()["already"] is False
    bumped = (await client.post(_research(ids["Initech"]))).json()
    assert (bumped["already"], bumped["again"]) == (True, False) and "goes first" in bumped["said"]
    assert await _line() == [ids["Hooli"], ids["Initech"], ids["Fabrikam"]], "asked, in the order asked; then the rest"

    async with db.session_factory()() as session:
        assert (await analysis.claim_next(session, OWNER)).id == ids["Hooli"], "it waited least and goes first"
        await session.commit()


async def test_the_role_the_agents_page_names_for_the_analyst_is_the_first_in_the_line_an_owners_ask_included(client):
    ids = await analysis_setup()
    await _night_starts(ids["Fabrikam"], ids["Initech"])  # the night's: Fabrikam has waited longest
    async with db.session_factory()() as session:
        assert (await analysis.waiting(session, OWNER)).id == ids["Fabrikam"]
        assert await session_kinds.doing(session, OWNER, roster.ANALYST) == "Researching Programs Lead at Fabrikam"

    await _set(ids["Hooli"], status=stages.REJECTED)
    await client.post(_research(ids["Hooli"]))
    async with db.session_factory()() as session:
        assert (await analysis.waiting(session, OWNER)).id == ids["Hooli"], "the owner's ask is first, as handed out"
        assert await session_kinds.doing(session, OWNER, roster.ANALYST) == "Researching Programs Lead at Hooli"
        assert (await analysis.claim_next(session, OWNER)).id == ids["Hooli"], "the page names the role a session gets"
        await session.commit()


async def test_an_ask_obeys_the_one_site_rule_as_the_rest_of_the_line_does(client):
    ids = await analysis_setup()  # the three roles' links are on one site
    await _night_starts(ids["Fabrikam"], ids["Initech"])
    await _set(ids["Hooli"], status=stages.WITHDREW)
    await client.post(_research(ids["Hooli"]))
    await client.post(_research(ids["Initech"]))
    async with db.session_factory()() as session:
        assert (await analysis.claim_next(session, OWNER)).id == ids["Hooli"]
        await session.commit()
        assert await analysis.claim_next(session, OWNER) is None, "one session on a site at a time, asks included"
    await _set(ids["Fabrikam"], url="https://careers.fabrikam.example.test/1")
    async with db.session_factory()() as session:
        assert (await analysis.claim_next(session, OWNER)).id == ids["Fabrikam"], (
            "Initech is asked for but on the site in use: the next role, on another site, is handed over")
        await session.commit()


async def test_a_role_with_no_link_and_no_posting_text_is_queued_and_waits_for_its_text_unresearched(client):
    ids = await analysis_setup()
    role_id = ids["Hooli"]
    await _set(role_id, status=stages.WITHDREW, url=None, description=None)
    asked = (await client.post(_research(role_id))).json()
    assert asked["state"] == "no_text" and "waits for the posting's text" in asked["said"], "no text anywhere: it waits"
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER)
        await session.commit()
    assert task["task"] is None, "the analyst is never handed a role with no text"
    role = await _role(role_id)
    assert role.analysis_state == analysis.NO_TEXT and not role.analysis.get("required"), "never researched"
    assert role.status == stages.WITHDREW
    assert len(await _lines(role_id, "waiting_for_text")) == 1


# --- asking twice, and a role that is not there -----------------------------------------------------------------


async def test_asking_twice_queues_the_role_once(client, knocks):
    ids = await analysis_setup()
    role_id = ids["Fabrikam"]
    await _set(role_id, status=stages.GONE_QUIET)
    first = (await client.post(_research(role_id))).json()
    held = await _role(role_id)
    second = (await client.post(_research(role_id))).json()
    assert (first["already"], second["already"]) == (False, True)
    assert second["state"] == "waiting" and "queued for research already" in second["said"]
    after = await _role(role_id)
    assert (after.analysis_asked_at, after.analysis_instance_id) == (held.analysis_asked_at, held.analysis_instance_id)
    assert await _instances(role_id) == 1 and len(await _lines(role_id, "research_asked")) == 1
    assert knocks == [(str(role_id), "analysis_asked")], "the second ask wakes no one"


async def test_two_asks_at_the_same_moment_queue_the_role_once(client):
    ids = await analysis_setup()
    role_id = ids["Fabrikam"]
    await _set(role_id, status=stages.REJECTED)
    answers = await asyncio.gather(client.post(_research(role_id)), client.post(_research(role_id)))
    assert [a.status_code for a in answers] == [200, 200]
    assert sorted(a.json()["already"] for a in answers) == [False, True]
    assert await _instances(role_id) == 1 and len(await _lines(role_id, "research_asked")) == 1


async def test_a_role_a_session_has_taken_answers_that_it_is_queued_and_stays_with_that_session(client):
    ids = await analysis_setup()
    role_id = ids["Fabrikam"]
    await _night_starts(role_id)
    async with db.session_factory()() as session:
        taken = await analysis.claim_next(session, OWNER, session_id="analyst-1", runner="r/1")
        await session.commit()
    before = await _role(role_id)
    answer = (await client.post(_research(role_id))).json()
    assert (answer["state"], answer["already"]) == ("waiting", True) and "queued for research already" in answer["said"]
    after = await _role(role_id)
    assert after.analysis_claimed_at == before.analysis_claimed_at and after.analysis["session"] == "analyst-1"
    assert taken.id == role_id and not await _lines(role_id, "research_asked"), "nothing changed, nothing to say"


async def test_a_role_there_is_none_of_is_not_found_and_so_is_another_owners(client):
    await analysis_setup()
    async with db.session_factory()() as session:
        theirs = Opportunity(owner="someone-else", slug="x", company="Acme", title="Lead", origin="scout",
                             match_key="acme|lead", status=stages.REJECTED)
        session.add(theirs)
        await session.commit()
        theirs_id = theirs.id
    gone = await client.post(_research("00000000-0000-4000-8000-000000000000"))
    assert gone.status_code == 404
    other = await client.post(_research(theirs_id))
    assert other.status_code == 404
    assert (await _role(theirs_id)).analysis_state is None, "nothing was queued"
    assert (await client.post(_research("not-an-id"))).status_code == 422


# --- the automatic selection is the same ------------------------------------------------------------------------


async def test_the_nights_own_selection_takes_only_found_roles_never_researched_whoever_has_been_asked_for(client):
    ids = await analysis_setup()
    await _set(ids["Fabrikam"], status=stages.REJECTED)
    await _set(ids["Initech"], status=stages.GATED_OUT, screened_out_reason="Not a place the owner works")

    async def picked() -> list[str]:
        async with db.session_factory()() as session:
            return [r.company for r in await analysis.picks(session, OWNER, None)]

    assert await picked() == ["Hooli"], "an Ended role and a Gated out role are not the night's"
    await client.post(_research(ids["Fabrikam"]))
    await client.post(_research(ids["Initech"]))
    assert await picked() == ["Hooli"], "asking for them changes what the night takes in no way"

    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        mission = next(m for m in (await session.scalars(select(missions.Mission))).all() if m.key == missions.ANALYSE)
        entry = await missions.start(session, OWNER, mission, by="you")
        assert entry.started["roles"] == [str(ids["Hooli"])], "the mission sends the one Found role, nothing else"
        await session.commit()
    assert await picked() == []


# --- what sends a night's roles back to the queue leaves the owner's asks in line -------------------------------


async def test_a_night_that_is_stopped_sends_its_roles_back_but_leaves_the_role_the_owner_asked_for(client):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        mission = next(m for m in (await session.scalars(select(missions.Mission))).all() if m.key == missions.ANALYSE)
        entry = await missions.start(session, OWNER, mission, by="you")
        await session.commit()
    assert (await client.post(_research(ids["Initech"]))).json()["already"] is True
    asked_instance = (await _role(ids["Initech"])).analysis_instance_id
    async with db.session_factory()() as session:
        await analyse.release(session, entry, "stopped at the finish time")
        await session.commit()
    states = {c: (await _role(i)).analysis_state for c, i in ids.items()}
    assert states == {"Fabrikam": None, "Initech": analysis.WAITING, "Hooli": None}
    assert (await _role(ids["Initech"])).analysis_instance_id == asked_instance


async def test_a_search_round_does_not_wait_for_and_does_not_send_back_a_find_the_owner_asked_for(client):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        search = SearchRun(owner=OWNER, status="done")
        session.add(search)
        await session.flush()
        for role_id in ids.values():
            (await session.get(Opportunity, role_id)).source_run = search.id
        await session.commit()
        search_id = search.id
    await _night_starts(*ids.values())
    await client.post(_research(ids["Initech"]))
    async with db.session_factory()() as session:
        assert await analysis.round_waiting(session, search_id) == 2, "the round's own finds, not the owner's"
        assert await analysis.release_round(session, search_id, "the night was stopped") == 2
        await session.commit()
        assert await analysis.round_waiting(session, search_id) == 0
    states = {c: (await _role(i)).analysis_state for c, i in ids.items()}
    assert states == {"Fabrikam": None, "Initech": analysis.WAITING, "Hooli": None}


# --- the catch-up -----------------------------------------------------------------------------------------------


async def test_a_catch_up_leaves_an_ended_role_the_owner_asked_for_and_a_usage_limit_does_not_take_it_back(client):
    rows = [tracker_row(company, "Programs Lead", status=status, fit=5, url=f"https://careers.{company}.example.test/1")
            for company, status in (("Initech", "applied"), ("Northwind", "new"), ("Hooli", "dead"))]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, criteria={**PROFILE["criteria"], "target_roles": []})
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        await tracker.import_tracker(session, OWNER, tracker_workbook(rows))
        await session.execute(update(Opportunity).values(  # Sourced, with their text: only then are they researched
            description=POSTING_WORDS, source_url="https://careers.example.test/jobs/1", source_kind="employer",
            sourced_at=datetime.now(UTC)))
        await employers.settle(session, OWNER)
        started = await catch_up.start(session, OWNER)
        await session.commit()
        ids = {r.company: r.id for r in (await session.scalars(select(Opportunity))).all()}
    state = catch_up.state_of(started.entry)
    assert str(ids["Hooli"]) not in state["roles"], "a role that is not in play is not the catch-up's"
    assert state["inflight"] == [str(ids["Initech"])], "the catch-up has its first role with the analyst"

    # The owner asks for the Ended role, and for the one the catch-up already has with the analyst.
    assert (await client.post(_research(ids["Hooli"]))).json()["already"] is False
    assert (await client.post(_research(ids["Initech"]))).json()["already"] is True
    assert (await _role(ids["Hooli"])).status == stages.GONE_QUIET

    now = datetime.now(UTC)
    limit = {"resets_at": (now + timedelta(hours=3)).isoformat(), "text": "You've hit your session limit"}
    async with db.session_factory()() as session:
        assert await catch_up.note_usage_limit(session, OWNER, limit, now=now) is True
        await session.commit()
    for company in ("Initech", "Hooli"):
        role = await _role(ids[company])
        assert role.analysis_state == analysis.WAITING, f"{company}: the limit takes back the catch-up's own roles only"
        async with db.session_factory()() as session:
            assert (await session.get(ProcInstance, role.analysis_instance_id)).status != "cancelled"
    # The catch-up still hands Hooli to no one: it is not its role, and the owner's ask stands.
    async with db.session_factory()() as session:
        await catch_up.advance(session, OWNER, heavy=False, now=now + timedelta(hours=4))
        await session.commit()
    assert (await _role(ids["Hooli"])).analysis_state == analysis.WAITING
    assert str(ids["Hooli"]) not in catch_up.state_of(await _catch_up_run())["queue"]


async def _catch_up_run():
    async with db.session_factory()() as session:
        return (await session.scalars(select(MissionRun).where(MissionRun.started_by == "import"))).one()
