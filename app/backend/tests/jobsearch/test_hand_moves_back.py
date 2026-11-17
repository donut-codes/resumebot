"""Moving a role by hand (docs/PRD.md 23.8). The owner can move a role from any stage to First Impression, Second
Impression or Ready to Launch, as well as on to Applied, Active Discussion, Rejected, Gone Quiet or Withdrew. A move is
the role's status and nothing else: it keeps its research and its packets, needs no packet for Ready to Launch, starts
no agent, and no re-run moves the role from where the owner put it. Everything is made up."""

from datetime import date

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import regate
from app.jobsearch.models import Opportunity, Packet, StageMove
from app.jobsearch.pipeline import board, placement
from app.jobsearch.research import analysis
from app.models.work import TrailEvent
from tests.builders import (
    analysis_setup,
    counted,
    findings,
    packet_setup,
    submit_findings,
    write_packet,
)
from tests.support import OWNER, load_profile

FIRST, SECOND, LAUNCH = stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH
GATES = "/api/v1/profile/gates"


def role(slug="a", status=SECOND, **more) -> Opportunity:
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": f"Co {slug}", "title": f"Role {slug}", "fit": 3, "origin": "scout",
        "match_key": f"co {slug}|role {slug}", "found": date.today(), "status": status, **more})


async def _make(slug="a", status=SECOND, packets=(), **more):
    """A role the owner has, with `packets` as (status, version) pairs; returns its id."""
    async with db.session_factory()() as session:
        made = role(slug, status, **more)
        session.add(made)
        await session.flush()
        for packet_status, version in packets:
            session.add(Packet(owner=OWNER, role_id=made.id, version=version, status=packet_status, brief={},
                               resume={}, letter={}, comp={}, reasoning={}, checks=[]))
        await session.commit()
        return made.id


async def _row(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _moves_of(role_id) -> set[tuple[str | None, str]]:
    """Every stage change the role has in its history, as (from, to)."""
    async with db.session_factory()() as session:
        rows = await session.execute(select(StageMove.from_status, StageMove.to_status).where(
            StageMove.role_id == role_id))
        return {(frm, to) for frm, to in rows}


async def _lines(role_id, event="role_moved") -> list[str]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent.sentence).where(
            TrailEvent.link_id == role_id, TrailEvent.event == event))).all())


async def _hand(role_id) -> bool:
    """Whether the owner's hand is on the role, as the gates read it (`placement`: ever, never spent)."""
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        return (await placement.one(session, OWNER, role, placement.Purpose.GATES)).held is placement.Hold.MOVED


async def _packets_of(role_id) -> list[tuple[int, str]]:
    async with db.session_factory()() as session:
        rows = await session.execute(select(Packet.version, Packet.status).where(Packet.role_id == role_id)
                                     .order_by(Packet.version))
        return [(v, s) for v, s in rows]


async def _move(client, role_id, to, **more):
    return await client.post(f"/api/v1/roles/{role_id}/move", json={"to": to, **more})


def _floor(amount: int) -> dict:
    """Hard pass: remote work only, and at least `amount` a year on the posted maximum."""
    return {"location": {"remote": {"on": True}}, "compensation": {"amount": amount, "against": "maximum"}}


# --- what is offered --------------------------------------------------------------------------------------------------


async def test_every_status_a_person_decides_is_offered_in_the_order_of_the_process_and_the_rest_are_not(client):
    role_id = await _make()
    code_moved = (stages.FOLLOWUP_NEEDED, stages.LANDING_ZONE, stages.CLEANSED_DATA)
    offered = [s for s in stages.STAGES if s not in (*code_moved, stages.GATED_OUT)]
    assert list(board.MOVES_TO) == offered == [
        "first_impression", "second_impression", "ready_to_launch", "applied", "active_discussion", "rejected",
        "gone_quiet", "withdrew"]
    labels = ["First Impression", "Second Impression", "Ready to Launch", "Applied", "Active Discussion", "Rejected",
              "Gone Quiet", "Withdrew"]
    on_the_page = (await client.get(f"/api/v1/roles/{role_id}")).json()["moves_to"]
    assert on_the_page == [{"status": s, "label": n} for s, n in zip(offered, labels, strict=True)]
    for not_the_owners in ("gated_out", "landing_zone", "cleansed_data", "followup_needed", "not_a_stage"):
        bad = await _move(client, role_id, not_the_owners)
        assert bad.status_code == 409 and "can be moved to first_impression, second_impression" in bad.json()["detail"]


# --- back to First Impression ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("was", ["second_impression", "ready_to_launch", "applied", "active_discussion", "rejected",
                                 "gone_quiet", "withdrew", "gated_out"])
async def test_a_role_is_pulled_back_to_first_impression_by_hand_from_any_stage_and_the_move_is_in_its_history(
        client, was):
    role_id = await _make(status=was, **({"screened_out_reason": "pay under the floor"} if was == "gated_out" else {}))
    assert ((await _row(role_id)).ended_reason is not None) == (was in stages.ENDED), "an ended role says why"
    moved = await _move(client, role_id, FIRST)
    assert moved.status_code == 200
    body = moved.json()
    assert (body["status"], body["status_label"], body["ended_reason"]) == (FIRST, "First Impression", None)
    row = await _row(role_id)
    assert (row.status, row.ended_reason) == (FIRST, None), "an ended reason goes when the role is in play again"
    assert await _moves_of(role_id) == {(None, was), (was, FIRST)}, "a hand move is a stage change like any"
    assert await _lines(role_id) == [f"You moved Role a at Co a from {board.LABELS[was]} to First Impression."]
    assert await _hand(role_id), "the owner's move is written as theirs, which no re-run moves a role from"


@pytest.mark.parametrize("to", [FIRST, LAUNCH])
async def test_an_ended_reason_belongs_to_the_ended_stages_only(client, to):
    ended_id = await _make(status="withdrew", slug="w")
    assert (await _row(ended_id)).ended_reason is not None
    moved = await _move(client, ended_id, to, ended_reason="i_passed")  # a reason for a move that does not end it
    assert moved.status_code == 200 and moved.json()["ended_reason"] is None
    assert (await _row(ended_id)).ended_reason is None, "none kept, and the one it had is gone"
    assert (await _move(client, ended_id, "rejected")).json()["ended_reason"] == "they_said_no", "ending again says why"


async def test_a_researched_role_pulled_back_to_first_impression_keeps_its_research_and_is_not_researched_again(client):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    assert (await submit_findings(findings(ids["Fabrikam"])))["taken"] is True
    done = await _row(ids["Fabrikam"])
    assert done.analysis_state == analysis.DONE

    assert (await _move(client, ids["Fabrikam"], FIRST)).status_code == 200
    row = await _row(ids["Fabrikam"])
    assert (row.status, row.analysis_state, row.analysed_at) == (FIRST, analysis.DONE, done.analysed_at)
    assert row.analysis == done.analysis and row.analysis["notes"] == "A real programs role.", "research kept"
    async with db.session_factory()() as session:
        assert ids["Fabrikam"] not in {r.id for r in await analysis.picks(session, OWNER, None)}


async def test_a_role_the_owner_puts_on_deck_is_in_the_analysts_line_and_the_move_starts_no_work_of_its_own(client):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["Hooli"])).status = stages.GATED_OUT
        (await session.get(Opportunity, ids["Hooli"])).screened_out_reason = "pay under the floor"
        await session.commit()
        queue = await analysis.picks(session, OWNER, None)
    assert {r.company for r in queue} == {"Fabrikam", "Initech"}, "Hooli is gated out"

    assert (await _move(client, ids["Hooli"], SECOND)).status_code == 200
    row = await _row(ids["Hooli"])
    assert (row.status, row.analysis_state) == (SECOND, None), "the move queues no work of its own"
    assert "gated out first: pay under the floor" in row.notes, "the gate's reason is kept"
    async with db.session_factory()() as session:
        queue = await analysis.picks(session, OWNER, None)
    assert {r.company for r in queue} == {"Fabrikam", "Initech", "Hooli"}


async def test_the_analyst_researches_a_role_the_owner_moved_but_it_stays_where_the_owner_put_it(client):
    """The pipeline sends the best researched roles on deck up to Ready to Launch; a role the owner has put somewhere
    by hand is not moved by it."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["Fabrikam"])).status = FIRST
        await session.commit()
    assert (await _move(client, ids["Fabrikam"], SECOND)).status_code == 200
    for company in ("Fabrikam", "Initech"):
        async with db.session_factory()() as session:
            await analysis.start(session, OWNER, await session.get(Opportunity, ids[company]))
            await session.commit()
        assert (await submit_findings(findings(ids[company])))["taken"] is True
    mine, other = await _row(ids["Fabrikam"]), await _row(ids["Initech"])
    assert other.status == LAUNCH, "a role nobody moved goes up to Ready to Launch once it is read"
    assert (mine.status, mine.analysis_state) == (SECOND, analysis.DONE), "researched, and the owner's move stands"


async def test_a_packet_filed_for_a_role_the_owner_moved_leaves_the_role_where_the_owner_put_it(client):
    """A packet is built on Go or on Build now and moves no role: the owner's move stands, and the packet is kept."""
    role_id = await packet_setup()
    asked = await client.post(f"/api/v1/roles/{role_id}/packet", json={})
    assert asked.status_code == 200 and asked.json()["status"] == "writing"
    assert (await _row(role_id)).status == SECOND, "an ask for a packet moves nothing"
    assert (await _move(client, role_id, FIRST)).status_code == 200
    await write_packet()

    row = await _row(role_id)
    assert (row.status, row.packet_complete) == (FIRST, True), "the owner's move stands; the packet is kept"
    assert await _packets_of(role_id) == [(1, "ready")]
    assert await _moves_of(role_id) == {(None, SECOND), (SECOND, FIRST)}, "the packet made no stage change"
    assert await _lines(role_id) == ["You moved Programs Lead at Globex from Second Impression to First Impression."]


# --- on to Ready to Launch --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("was", ["second_impression", "first_impression", "applied", "active_discussion", "gated_out",
                                 "withdrew"])
async def test_a_read_role_with_no_packet_goes_to_ready_to_launch_from_any_stage_and_the_move_makes_no_packet(
        client, was):
    role_id = await _make(status=was, **counted(),
                          **({"screened_out_reason": "location"} if was == "gated_out" else {}))
    moved = await _move(client, role_id, LAUNCH, note="by hand")
    assert moved.status_code == 200 and moved.json()["status"] == LAUNCH
    row = await _row(role_id)
    assert (row.status, row.packet_complete, row.analysis_state) == (LAUNCH, False, analysis.DONE)
    assert await _packets_of(role_id) == [], "a move starts no writer: Go builds the packet"
    assert await _moves_of(role_id) == {(None, was), (was, LAUNCH)}
    assert "by hand" in row.notes and ("gated out first: location" in row.notes) == (was == "gated_out")
    assert await _hand(role_id)


async def test_a_ready_to_launch_role_pulled_back_keeps_its_packet(client):
    role_id = await _make(status=LAUNCH, packets=[("ready", 1)], packet_complete=True, analysis_state=analysis.DONE)
    moved = await _move(client, role_id, SECOND)
    assert moved.status_code == 200 and moved.json()["status"] == SECOND
    row = await _row(role_id)
    assert (row.status, row.packet_complete, row.analysis_state) == (SECOND, True, analysis.DONE)
    assert await _packets_of(role_id) == [(1, "ready")], "the packet is kept as it was"
    assert await _lines(role_id) == ["You moved Role a at Co a from Ready to Launch to Second Impression."]


async def test_a_gated_out_role_moved_back_by_hand_keeps_why_it_was_gated_and_no_re_run_moves_it_again(client):
    """docs/PRD.md 39: a re-run never moves a role the owner moved by hand. A gate the roles fail still fails after the
    owner moved them, and the roles nobody moved are the ones it goes on holding."""
    names = ("to-first", "to-second", "to-ready", "control")
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=_floor(95_000))
        rows = {s: role(s, SECOND, comp="$70,000-$80,000", location="Remote", move_type="remote",
                        **(counted() if s == "to-ready" else {})) for s in names}
        session.add_all(rows.values())
        await session.flush()
        report = await regate.run(session, OWNER, cause="a test set it up")
        await session.commit()
        ids = {s: r.id for s, r in rows.items()}
    assert report.roles_stopped == 4, "pay under the floor stops them all"
    for slug, to in (("to-first", FIRST), ("to-second", SECOND), ("to-ready", LAUNCH)):
        moved = await _move(client, ids[slug], to)
        assert moved.status_code == 200, moved.text
        assert "gated out first: Pay must" in (await _row(ids[slug])).notes, "the gate's reason is kept"

    tightened = await client.put(GATES, json={"data": _floor(96_000), "why": "raise the floor"})
    assert tightened.status_code == 200 and tightened.json()["version"] == 2
    after = {s: (await _row(i)).status for s, i in ids.items()}
    assert after == {"to-first": FIRST, "to-second": SECOND, "to-ready": LAUNCH, "control": stages.GATED_OUT}
    held = (await _row(ids["control"])).screened_out_reason
    assert "96,000" in held, "the re-gate ran, and went on holding the one nobody moved"


async def test_moving_a_role_to_the_stage_it_is_in_changes_nothing(client):
    role_id = await _make(status=FIRST)
    same = await _move(client, role_id, FIRST)
    assert same.status_code == 200 and same.json()["status"] == FIRST
    assert await _lines(role_id) == [] and await _moves_of(role_id) == {(None, FIRST)}
