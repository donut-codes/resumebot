"""Why a role ended (docs/PRD.md 31.7): an Ended role records the reason it ended (they said no, I passed, the posting
went, an offer received, or unknown), the owner is offered the reasons when they end a role, and what the app can
tell it works out: a rejection is the employer saying no, a posting that closed is a posting that went. Synthetic
roles only."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.imports import tracker
from app.jobsearch.models import JobPosting, Opportunity, StageMove
from app.jobsearch.pipeline import board, ended
from app.models.work import TrailEvent
from tests.support import EXAMPLE_SYSTEM, OWNER, tracker_row, tracker_workbook

NOW = datetime(2026, 10, 9, 5, 30, tzinfo=UTC)
OFFERED = [
    "not_my_field", "wrong_level", "pay_too_low", "location_or_mode", "the_employer", "duplicate", "something_else",
    "they_said_no", "posting_gone", "received_offer"]


def role(slug, **more):
    return Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", fit=3, origin="scout",
                       match_key=f"co {slug}|role {slug}", found=date.today(),
                       **{"status": "second_impression", **more})


async def _make(slug="a", **more):
    """A role the owner has, and its id."""
    async with db.session_factory()() as session:
        made = role(slug, **more)
        session.add(made)
        await session.commit()
        return made.id


async def _row(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id)


async def _posting_of(role_id, closed: bool) -> None:
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(JobPosting(
            owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=f"p-{role_id}", company="Fabrikam",
            title="Lead",
            first_seen=NOW, last_seen=NOW, links=[], opportunity_id=role_id, closed_at=NOW if closed else None))
        await session.commit()


# --- the reasons, and the owner saying one -------------------------------------------------------------------------


def test_the_owner_is_offered_ten_reasons_and_the_old_ones_still_read():
    assert stages.END_REASONS == (
        "they_said_no", "i_passed", "posting_gone", "no_reply", "unknown", "received_offer", "not_my_field",
        "wrong_level", "pay_too_low", "location_or_mode", "the_employer", "duplicate", "something_else", "noise")
    # Old: 13 reasons. New: 14 (PRD 32.6, the owner, 2026-11-13): `noise` is a rule's reason, closed by the app's code
    # for a role whose posting it could not read, so it is stored and shown but never offered to the owner.
    assert "noise" not in stages.OWNER_END_REASONS and "noise" not in stages.PASSED_ON_REASONS
    assert stages.END_REASON_LABELS["noise"] == "Noise"
    assert stages.OWNER_END_REASONS == tuple(OFFERED)
    assert [o["reason"] for o in ended.offered()] == list(stages.OWNER_END_REASONS)
    assert [o["label"] for o in ended.offered()] == [
        "Not my field", "Wrong level", "Pay too low", "Location or work mode", "The employer",
        "Duplicate or already applied", "Something else", "They said no", "Expired", "Received offer"]
    assert stages.END_REASON_LABELS["i_passed"] == "I passed on it", "a stored reason is displayed as before"
    assert ended.check(None) is None and ended.check("  ") is None and ended.check(" I_Passed ") == "i_passed"
    assert ended.check(" Wrong_Level ") == "wrong_level"
    with pytest.raises(ended.ReasonError, match="not because"):
        ended.check("because")


@pytest.mark.parametrize("reason, words, phrase", [
    ("they_said_no", "They said no", "they said no"),
    ("posting_gone", "Expired", "the posting went"),
    ("received_offer", "Received offer", "you received an offer"),
    ("i_passed", "I passed on it", "you passed on it"),
    ("not_my_field", "Not my field", "not my field"),
    ("wrong_level", "Wrong level", "wrong level"),
    ("pay_too_low", "Pay too low", "pay too low"),
    ("location_or_mode", "Location or work mode", "location or work mode"),
    ("the_employer", "The employer", "the employer"),
    ("duplicate", "Duplicate or already applied", "duplicate or already applied"),
    ("something_else", "Something else", "something else")])
async def test_a_role_the_owner_ends_records_the_reason_they_gave_and_the_trail_says_it(client, reason, words, phrase):
    role_id = await _make()
    moved = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "withdrew", "ended_reason": reason})
    assert moved.status_code == 200
    body = moved.json()
    assert (body["status"], body["ended_reason"], body["ended_words"]) == ("withdrew", reason, words)
    assert (await _row(role_id)).ended_reason == reason
    async with db.session_factory()() as session:
        sentence = (await session.scalars(select(TrailEvent.sentence).where(TrailEvent.event == "role_moved"))).one()
    assert sentence == f"You moved Role a at Co a from Second Impression to Withdrew ({phrase})."


async def test_a_reason_that_is_not_one_of_them_is_refused_and_the_role_does_not_move(client):
    role_id = await _make()
    bad = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "withdrew", "ended_reason": "because"})
    assert bad.status_code == 409 and "not because" in bad.json()["detail"]
    assert (await _row(role_id)).status == "second_impression"
    unknown = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "withdrew", "ended_reason": "unknown"})
    assert unknown.status_code == 200 and unknown.json()["ended_reason"] == "unknown"


async def test_the_api_takes_no_word_came_back_from_the_owner_though_the_box_does_not_offer_it(client):
    """`no_reply` is a rule's reason, but the move takes any reason in
    `END_REASONS`, as it does `unknown`: refusing it would need a second list. The refusal still names the three the box
    offers, and a word that is no reason is still refused."""
    role_id = await _make()
    said = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "gone_quiet", "ended_reason": "no_reply"})
    assert said.status_code == 200
    assert (said.json()["status"], said.json()["ended_reason"], said.json()["ended_words"]) == (
        "gone_quiet", "no_reply", "No word came back")
    assert (await _row(role_id)).ended_reason == "no_reply"
    async with db.session_factory()() as session:
        sentence = (await session.scalars(select(TrailEvent.sentence).where(TrailEvent.event == "role_moved"))).one()
    assert sentence == "You moved Role a at Co a from Second Impression to Gone Quiet (no word came back)."
    other = await _make("b")
    bad = await client.post(f"/api/v1/roles/{other}/move", json={"to": "gone_quiet", "ended_reason": "silence"})
    assert bad.status_code == 409
    assert "not silence" in bad.json()["detail"]
    assert "no_reply" not in [o["reason"] for o in ended.offered()]


async def test_with_no_reason_given_a_posting_that_closed_is_a_posting_that_went_and_otherwise_the_owner_passed(client):
    open_id, closed_id = await _make("open"), await _make("closed")
    await _posting_of(closed_id, closed=True)
    inferred = {}
    for name, role_id in (("open", open_id), ("closed", closed_id)):
        moved = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "withdrew"})
        assert moved.status_code == 200
        inferred[name] = moved.json()["ended_reason"]
    assert inferred == {"open": "i_passed", "closed": "posting_gone"}
    open_still = await _make("still-open")
    await _posting_of(open_still, closed=False)
    still = await client.post(f"/api/v1/roles/{open_still}/move", json={"to": "withdrew"})
    assert still.json()["ended_reason"] == "i_passed", "a posting still up is no posting gone"


async def test_withdrawing_with_a_note_and_no_reason_keeps_the_note_and_reads_as_before(client):
    role_id = await _make()
    moved = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "withdrew", "note": "found a better one"})
    assert moved.status_code == 200 and moved.json()["ended_reason"] == "i_passed"
    assert "found a better one" in (await _row(role_id)).notes


async def test_a_role_that_comes_back_into_play_no_longer_has_a_reason(client):
    role_id = await _make()
    await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "withdrew", "ended_reason": "i_passed"})
    back = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "applied", "ended_reason": "i_passed"})
    assert back.status_code == 200 and back.json()["ended_reason"] is None and back.json()["ended_words"] is None
    assert (await _row(role_id)).ended_reason is None, "a reason given for a move that does not end the role is ignored"


async def test_the_board_and_the_role_page_offer_the_reasons_and_an_ended_card_says_why(client):
    role_id = await _make()
    await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "withdrew", "ended_reason": "they_said_no"})
    body = (await client.get("/api/v1/board")).json()
    assert [r["reason"] for r in body["ended_reasons"]] == OFFERED
    card = next(c for col in body["columns"] if col["key"] == "closed" for c in col["cards"])
    assert (card["ended_reason"], card["ended_words"]) == ("they_said_no", "They said no")
    page = (await client.get(f"/api/v1/roles/{role_id}")).json()
    assert [r["reason"] for r in page["ended_reasons"]] == OFFERED
    assert page["role"]["ended_reason"] == "they_said_no"
    playing = await _make("playing")
    assert (await client.get(f"/api/v1/roles/{playing}")).json()["role"]["ended_reason"] is None


# --- what the app can tell by itself ---------------------------------------------------------------------------------


def test_each_ended_stage_says_what_it_can_by_itself():
    assert [stages.default_end_reason(s) for s in stages.STAGES] == [
        *[None] * 8, "they_said_no", "unknown", "unknown", None]
    assert stages.default_end_reason(None) is None


async def test_a_role_that_reaches_an_ended_stage_by_any_route_gets_the_stages_own_reason(js):
    """The reason is set where the status changes (like the stage move), so no code has to remember to."""
    async with db.session_factory()() as session:
        rejected = role("r", status="rejected")
        quiet = role("q", status="gone_quiet")
        found = role("f")
        session.add_all([rejected, quiet, found])
        await session.flush()
        assert (rejected.ended_reason, quiet.ended_reason, found.ended_reason) == ("they_said_no", "unknown", None)
        found.status = "rejected"
        await session.flush()
        assert found.ended_reason == "they_said_no"
        found.status = "applied"
        await session.flush()
        assert found.ended_reason is None, "back in play"
        quiet.status = "second_impression"
        await session.flush()
        assert quiet.ended_reason is None
        explicit = role("e", status="withdrew", ended_reason="i_passed")
        session.add(explicit)
        await session.flush()
        assert explicit.ended_reason == "i_passed", "one that was said is kept"


async def test_the_reason_is_worked_out_from_the_stage_a_closed_posting_and_what_was_said(js):
    async with db.session_factory()() as session:
        made = role("a")
        session.add(made)
        await session.flush()
        assert await ended.reason_for(session, made, stages.APPLIED) is None
        assert await ended.reason_for(session, made, stages.REJECTED) == stages.THEY_SAID_NO
        assert await ended.reason_for(session, made, stages.WITHDREW) == stages.I_PASSED
        assert await ended.reason_for(session, made, stages.GONE_QUIET) == stages.UNKNOWN_REASON
        assert await ended.reason_for(session, made, stages.GONE_QUIET, "i_passed") == stages.I_PASSED, "what was said"
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id="p", company="Fabrikam",
                               title="Lead", first_seen=NOW, last_seen=NOW, links=[], opportunity_id=made.id,
                               closed_at=NOW))
        await session.flush()
        assert await ended.posting_closed(session, made.id) is True
        assert await ended.reason_for(session, made, stages.WITHDREW) == stages.POSTING_GONE
        assert await ended.reason_for(session, made, stages.GONE_QUIET) == stages.POSTING_GONE
        assert await ended.reason_for(session, made, stages.REJECTED) == stages.THEY_SAID_NO, "they said no, still"


async def test_a_tracker_row_the_employer_rejected_says_so_and_the_others_say_only_what_their_status_does(js):
    rows = [tracker_row("Acme", "Rejected Role", status="rejected"), tracker_row("Acme", "Quiet Role", status="dead"),
            tracker_row("Acme", "Set Aside Role", status="deferred"),
            tracker_row("Acme", "Applied Role", status="applied")]
    async with db.session_factory()() as session:
        await tracker.import_tracker(session, OWNER, tracker_workbook(rows))
        reasons = {r.title: (r.status, r.ended_reason) for r in (await session.scalars(select(Opportunity))).all()}
    assert reasons == {
        "Rejected Role": ("rejected", "they_said_no"), "Quiet Role": ("gone_quiet", "unknown"),
        "Set Aside Role": ("withdrew", "unknown"), "Applied Role": ("applied", None)}


def test_the_owner_may_move_a_role_to_any_stage_a_person_decides_in_the_order_of_the_process():
    """docs/PRD.md 23.8: back to First Impression, on to Second Impression or Ready to Launch, and Applied, Active
    Discussion, Rejected, Gone Quiet or Withdrew; the earlier statuses and Gated out are code's own."""
    assert board.MOVES_TO == (
        stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH, stages.APPLIED,
        stages.ACTIVE_DISCUSSION, stages.REJECTED, stages.GONE_QUIET, stages.WITHDREW)


# --- the owner ending a role as Rejected or Gone Quiet by hand (docs/PRD.md 23.8) -----------------------------------


async def _moves_of(role_id) -> set[tuple[str | None, str]]:
    """Every stage change the role has in its history, as (from, to)."""
    async with db.session_factory()() as session:
        rows = await session.execute(select(StageMove.from_status, StageMove.to_status).where(
            StageMove.role_id == role_id))
        return {(frm, to) for frm, to in rows}


@pytest.mark.parametrize("to, label, given, reason, aside", [
    ("rejected", "Rejected", None, "they_said_no", " (they said no)"),
    ("rejected", "Rejected", "i_passed", "i_passed", " (you passed on it)"),
    ("gone_quiet", "Gone Quiet", None, "unknown", ""),
    ("gone_quiet", "Gone Quiet", "they_said_no", "they_said_no", " (they said no)")])
async def test_the_owner_can_mark_a_role_rejected_or_gone_quiet_with_the_reason_kept_and_the_move_in_its_history(
        client, to, label, given, reason, aside):
    role_id = await _make()
    moved = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": to, "ended_reason": given})
    assert moved.status_code == 200
    body = moved.json()
    assert (body["status"], body["status_label"], body["ended_reason"]) == (to, label, reason)
    assert (await _row(role_id)).ended_reason == reason
    assert await _moves_of(role_id) == {(None, "second_impression"), ("second_impression", to)}, (
        "a hand move is a stage change like any")
    async with db.session_factory()() as session:
        sentence = (await session.scalars(select(TrailEvent.sentence).where(TrailEvent.event == "role_moved"))).one()
    assert sentence == f"You moved Role a at Co a from Second Impression to {label}{aside}."


async def test_a_role_gone_quiet_whose_posting_came_down_says_the_posting_went_unless_the_owner_says_otherwise(client):
    closed_id, said_id = await _make("closed"), await _make("said")
    await _posting_of(closed_id, closed=True)
    await _posting_of(said_id, closed=True)
    inferred = await client.post(f"/api/v1/roles/{closed_id}/move", json={"to": "gone_quiet"})
    said = await client.post(f"/api/v1/roles/{said_id}/move", json={"to": "gone_quiet", "ended_reason": "i_passed"})
    assert (inferred.json()["ended_reason"], said.json()["ended_reason"]) == ("posting_gone", "i_passed")


async def test_an_interviewed_role_that_was_turned_down_is_in_rejected_and_remembers_the_interview(client):
    """Found when the owner recorded an interview outcome: a role they interviewed for, then turned down, could not be
    put back in Rejected. The history still says it was in talks."""
    role_id = await _make(status="applied")
    for to in ("active_discussion", "rejected"):
        assert (await client.post(f"/api/v1/roles/{role_id}/move", json={"to": to})).status_code == 200
    row = await _row(role_id)
    assert (row.status, row.ended_reason) == ("rejected", "they_said_no")
    assert await _moves_of(role_id) == {
        (None, "applied"), ("applied", "active_discussion"), ("active_discussion", "rejected")}


async def test_a_role_moves_between_rejected_and_gone_quiet_and_back_into_play_by_hand(client):
    role_id = await _make(status="gone_quiet")
    rejected = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "rejected"})
    assert (rejected.json()["status"], rejected.json()["ended_reason"]) == ("rejected", "they_said_no")
    quiet = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "gone_quiet", "ended_reason": "i_passed"})
    assert (quiet.json()["status"], quiet.json()["ended_reason"]) == ("gone_quiet", "i_passed")
    back = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "active_discussion"})
    assert (back.json()["status"], back.json()["ended_reason"]) == ("active_discussion", None)
    assert (await _row(role_id)).ended_reason is None, "leaving Gone Quiet clears its reason (docs/PRD.md 15.1.10)"


async def test_the_role_page_offers_rejected_and_gone_quiet(client):
    role_id = await _make()
    offered = ["first_impression", "second_impression", "ready_to_launch", "applied", "active_discussion", "rejected",
               "gone_quiet", "withdrew"]
    page = (await client.get(f"/api/v1/roles/{role_id}")).json()
    assert [m["status"] for m in page["moves_to"]] == offered
    assert [m["label"] for m in page["moves_to"]] == [
        "First Impression", "Second Impression", "Ready to Launch", "Applied", "Active Discussion", "Rejected",
        "Gone Quiet", "Withdrew"]
