"""The Coach (docs/PRD.md 15.2.2 to 15.2.10): a session of the `coach` kind takes the prep of an interview, is handed
the posting, the master, the story library and the owner's rules (and none of what is withheld), sends a report that
code checks and sends back until it passes, a second pass reads it as the interviewer, and the report is filed and the
owner
told. Stuck work ends, and an interview that passes while its prep waits lapses. Everything is made up."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.interviews import coach
from app.jobsearch.interviews import core as interviews
from app.jobsearch.models import Interview, InterviewPrep, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import hand_outs, session_kinds
from app.models.work import Notification, TrailEvent
from tests.builders import save_documents
from tests.jobsearch.coach_builders import SOURCE, TODAY, good_report
from tests.support import OWNER, load_profile


@pytest.fixture(autouse=True)
def _database(js, monkeypatch):
    """Every test here runs against the test database, on the day the made-up report is dated (`coach_builders.TODAY`,
    the `checked_on` of its sources): the report's checks read the owner's today, so without this the tests pass only
    on that day and the next."""
    monkeypatch.setattr(coach, "owner_today", lambda: TODAY)


async def _world(*, interview_rules=None, slug="acme-lead") -> tuple:
    """An owner with a master and a story library, a role in Active Discussion with its posting and research, and an
    interview three days ahead; returns (role id, interview id, prep id)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await roster.ensure_roster(session, OWNER)
        await save_documents(session)
        if interview_rules is not None:
            await profile_store.save(session, OWNER, "writing", {
                "voice_rules": [{"id": "plain", "says": "Plain."}], "interview_rules": interview_rules,
                "banned": [{"id": "puff", "phrase": "spearheaded", "why": "puffery"}]}, by="test")
        role = Opportunity(
            owner=OWNER, slug=slug, company="Acme", title="Program Lead", fit=4, origin="scout",
            match_key=f"acme|{slug}", status=stages.ACTIVE_DISCUSSION, found=datetime.now(UTC).date(),
            description="Lead the new program office. Plan the grant calendar. Report to the board.",
            analysis={"ask": {"ask": 105000, "expectation": 105000, "basis": "base", "floor": 95000,
                              "pay_points": [1, 2], "confidence": "medium"},
                      "required": [{"item": "Run programs", "class": "P", "from": ["b-risk"], "why": "led them"}]})
        session.add(role)
        await session.flush()
        interview = await interviews.add(
            session, OWNER, role.id, when=datetime.now(UTC) + timedelta(days=3), round_kind="hiring_manager",
            label="Team chat", people=[{"name": "Sam Ortiz", "title": "Director of Programs"}], notes="")
        prep = (await session.scalars(select(InterviewPrep).where(InterviewPrep.interview_id == interview.id))).one()
        await session.commit()
        return role.id, interview.id, prep.id


async def _take(part="task", prep_id=None):
    async with db.session_factory()() as session:
        reply = await coach.task_reply(session, OWNER, part, prep_id, session_id="s1")
        await session.commit()
        return reply


async def _submit(body):
    async with db.session_factory()() as session:
        reply = await coach.submit(session, OWNER, body)
        await session.commit()
        return reply


async def _prep(prep_id) -> InterviewPrep:
    async with db.session_factory()() as session:
        return await session.get(InterviewPrep, prep_id, populate_existing=True)


def _report() -> dict:
    report = good_report()
    # The made-up master of this world is the builders' one role; the answers cite what it holds.
    report["staples"]["opener"] = [{"text": "I build programs that people use.", "from": ["b-risk"]}]
    report["staples"]["why_here"] = [{"text": "The agency is opening a program office.", "source": SOURCE},
                                     {"text": "I have built programs before.", "from": ["b-kb"]}]
    report["stage_read"]["interviewers"][0]["connection"]["from"] = ["b-kb"]
    report["questions"] = [
        {"question": "Tell me about a launch.", "method": "Can you run a launch?",
         "answer": {"kind": "story", "title": "Risk", "text": "I led the risk group.", "result": "Used each quarter.",
                    "from": ["risk"]}},
        {"question": "Tell me about a crisis.", "method": "Calm?", "gap": "Add one real crisis to the master."}]
    return report


async def test_the_task_holds_what_the_coach_needs_and_none_of_what_is_withheld():
    _, _, prep_id = await _world(interview_rules=[{"id": "r1", "says": "Answer in stories."}])
    task = await _take()
    assert task["mode"] == "write" and task["id"] == str(prep_id)
    assert task["interview"]["people"][0]["name"] == "Sam Ortiz" and task["role"]["company"] == "Acme"
    assert task["rules"]["Rules for interviews"] == ["Answer in stories."]
    assert "Rules for your voice" in task["rules"]
    assert "Rules for ATS screening" not in task["rules"]
    assert task["rules"]["Never write"] == ["spearheaded"]
    assert task["expectation"] == {"expectation": 105000, "basis": "base", "band": None, "confidence": "medium"}
    assert task["instructions"] and task["response_template"]
    posting = await _take("posting", prep_id)
    assert "grant calendar" in posting["posting"]["text"]
    assert posting["requirements"]["required"][0]["item"] == "Run programs"
    resume = await _take("resume", prep_id)
    assert "b-risk" in json.dumps(resume["master"]) and resume["stories"][0]["id"] == "risk"
    everything = json.dumps([task, posting, resume])
    for withheld in ("95000", "95,000", "pat@example.test", "555-010-0199", "pay_points"):
        assert withheld not in everything


async def test_a_report_with_a_claim_that_cites_nothing_is_sent_back_with_every_problem_named():
    _, _, prep_id = await _world()
    await _take()
    bad = _report()
    bad["staples"]["opener"][0]["from"] = []
    bad["open_gaps"] = [{"text": "No action"}]
    reply = await _submit({"id": str(prep_id), "report": bad})
    assert reply["ok"] is False
    text = json.dumps(reply)
    assert "opener" in text and "open gap" in text.lower()
    prep = await _prep(prep_id)
    assert prep.state == "writing" and prep.attempts == 1


async def test_a_report_that_passes_waits_for_the_second_pass_which_files_it_and_tells_the_owner():
    role_id, interview_id, prep_id = await _world()
    await _take()
    sent = await _submit({"id": str(prep_id), "report": _report()})
    assert sent["ok"] is True, sent
    assert sent["taken"] is True
    assert (await _prep(prep_id)).state == "review_waiting"
    review_task = await _take()
    assert review_task["mode"] == "review" and review_task["report"]["core_need"]["text"]
    review = {"summary": "Mostly holds up.", "findings": [
        {"where": "questions: crisis", "problem": "No story for a crisis.", "fix": "Pick one.",
         "action": "Write the crisis story into the master."}]}
    done = await _submit({"id": str(prep_id), "review": review})
    assert done["ok"] is True
    prep = await _prep(prep_id)
    assert prep.state == "ready" and prep.finished_at is not None and prep.review["summary"] == "Mostly holds up."
    gaps = [g["action"] for g in prep.report["open_gaps"]]
    assert "Write the crisis story into the master." in gaps and "Add one real crisis to the master." in gaps
    async with db.session_factory()() as session:
        note = (await session.scalars(select(Notification))).one()
        said = (await session.scalars(
            select(TrailEvent.sentence).where(TrailEvent.event == "interview_prep_ready"))).all()
        view = (await interviews.for_role(session, OWNER, role_id))[0]
    assert note.title == "Your prep for the Hiring manager round at Acme is ready."
    assert (note.link_kind, note.link_id) == ("opportunity", role_id) and len(said) == 1
    assert view["prep"]["state"] == "ready" and view["prep"]["step"] == "Ready"
    quick = view["prep"]["report"]["quick_sheet"]
    assert quick["core_need"].startswith("Build the program office") and quick["story_map"][0]["story"] == "Risk"
    assert view["prep"]["report"]["questions"][0]["answer"]["cites"][0]["text"] == "The risk group"
    assert view["prep"]["review"]["findings"][0]["problem"] == "No story for a crisis."


async def test_three_refused_reports_fail_the_prep_with_what_was_wrong():
    _, _, prep_id = await _world()
    await _take()
    bad = _report()
    bad["staples"]["opener"][0]["from"] = []
    for _ in range(coach.MAX_REFUSALS):
        reply = await _submit({"id": str(prep_id), "report": bad})
    assert reply["ok"] is False and "failed" in json.dumps(reply).lower()
    prep = await _prep(prep_id)
    assert prep.state == "failed" and "opener" in prep.error


async def test_the_next_work_is_a_coach_session_and_not_offered_twice():
    await _world()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.COACH
    await _take()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) is None


async def test_a_session_that_took_it_three_times_and_never_answered_fails_the_prep():
    _, _, prep_id = await _world()
    for _ in range(hand_outs.MAX_CLAIMS):
        await _take()
        async with db.session_factory()() as session:
            prep = await session.get(InterviewPrep, prep_id)
            prep.taken_at = datetime.now(UTC) - coach.CLAIM_LEASE - timedelta(minutes=1)
            await session.commit()
    again = await _take()
    assert again.get("task", ...) is None
    prep = await _prep(prep_id)
    assert prep.state == "failed" and "no session" in prep.error.lower()


async def test_a_write_nobody_finished_fails_and_a_review_nobody_finished_files_the_report_without_it():
    _, _, prep_id = await _world()
    await _take()
    long_ago = datetime.now(UTC) - timedelta(hours=5)
    async with db.session_factory()() as session:
        prep = await session.get(InterviewPrep, prep_id)
        prep.taken_at = long_ago
        assert await coach.settle_stuck(session, OWNER) == 1
        await session.commit()
    assert (await _prep(prep_id)).state == "failed"

    _, _, second = await _world(slug="acme-second")
    await _take()
    await _submit({"id": str(second), "report": _report()})
    await _take()
    async with db.session_factory()() as session:
        prep = await session.get(InterviewPrep, second)
        prep.review_taken_at = long_ago
        assert await coach.settle_stuck(session, OWNER) == 1
        await session.commit()
    prep = await _prep(second)
    assert prep.state == "ready" and "didn't run" in prep.review["summary"]


async def test_a_prep_still_waiting_when_the_interview_time_passes_lapses():
    _, interview_id, prep_id = await _world()
    async with db.session_factory()() as session:
        (await session.get(Interview, interview_id)).when_at = datetime.now(UTC) - timedelta(hours=1)
        assert await coach.lapse(session, OWNER) == 1
        await session.commit()
    assert (await _prep(prep_id)).state == "lapsed"


async def test_prepare_again_hands_the_next_version_the_earlier_report():
    role_id, interview_id, prep_id = await _world()
    await _take()
    await _submit({"id": str(prep_id), "report": _report()})
    await _take()
    await _submit({"id": str(prep_id), "review": {"summary": "Fine.", "findings": []}})
    async with db.session_factory()() as session:
        again = await coach.start(session, OWNER, await session.get(Interview, interview_id))
        await session.commit()
        second = again.id
    assert again.version == 2
    task = await _take()
    assert task["id"] == str(second)
    earlier = await _take("earlier", second)
    assert earlier["earlier"][0]["report"]["core_need"]["text"].startswith("Build the program office")


def test_rules_for_interviews_are_handed_to_the_coach_with_the_voice_rules_and_to_no_other_job():
    from app.jobsearch.profile.documents import JOB_SECTIONS, WRITING_SECTIONS, Writing, rules_for

    assert ("interview", "Rules for interviews") in WRITING_SECTIONS
    assert JOB_SECTIONS["interview"] == ("voice", "interview")
    writing = Writing.model_validate({"voice_rules": [{"id": "v", "says": "Plain."}],
                                      "interview_rules": [{"id": "i", "says": "Stories."}]})
    assert rules_for(writing, "interview")["Rules for interviews"] == ["Stories."]
    for other in ("resume", "letter", "review"):
        assert "Rules for interviews" not in rules_for(writing, other)
