"""Packets: asking for one, the process filing it ready or held, the app's own corrections to the words, the catch-up
for held packets and the HTTP routes. How a packet is written is `test_packet_writer.py`. Everything is made up."""

import copy
import uuid

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.documents import Documents
from app.jobsearch.writing import packets
from app.models.work import Task
from tests.builders import (
    ABOUT,
    FACTS_CLEAN,
    RESUME,
    WRITING,
    compose_reply,
    packet_setup,
    run_seats,
    write_packet,
)
from tests.support import OWNER


async def _start(role_id, feedback="") -> str:
    async with db.session_factory()() as session:
        packet = await packets.start(session, OWNER, role_id, feedback)
        await session.commit()
        return str(packet.id)


def _cut(bullet: str = "Led the risk working group across client engagements.") -> packets.ResumeCut:
    return packets.ResumeCut.model_validate({
        "headline": "Programs Leader", "summary": "Builds programs that people use.", "summary_from": ["b-risk"],
        "competencies": [{"area": "Governance", "items": ["Risk Frameworks"]}],
        "roles": [{"role": "northwind-lead", "bullets": [{"text": bullet, "from": ["b-risk"]}]}]})


def _letter() -> packets.Letter:
    return packets.Letter.model_validate({"paragraphs": ["Dear hiring team,", "I would like to help."]})


def _made_up(resume=RESUME) -> Documents:
    return Documents.model_validate({"resume": resume, "about": ABOUT, "writing": WRITING})


async def test_a_packet_needs_a_master_resume_and_one_is_written_at_a_time(js):
    role_id = await packet_setup(with_resume=False)
    with pytest.raises(packets.PacketError, match="no master resume"):
        await _start(role_id)
    async with db.session_factory()() as session:
        for kind, data in {"resume": RESUME, "about": ABOUT}.items():
            await profile_store.save(session, OWNER, kind, data, by="test")
        await session.commit()
    await _start(role_id)
    with pytest.raises(packets.PacketError, match="being written already"):
        await _start(role_id)


async def test_a_packet_is_written_to_ready_and_the_role_is_marked_packet_complete(js):
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    await write_packet()
    async with db.session_factory()() as session:
        packet = await session.get(Packet, uuid.UUID(packet_id))
        role = await session.get(Opportunity, role_id)
        tasks = (await session.scalars(select(Task).where(Task.kind == "read_packet"))).all()
    assert packet.status == "ready" and all(c["ok"] for c in packet.checks if c["blocking"])
    printed = packet.resume["roles"][0]
    assert (printed["employer"], printed["title"], printed["start"]) == ("Northwind Labs", "Senior Manager", "2022")
    assert packet.resume["name"] == "Pat Example" and packet.resume["education"][0]["degree"] == "MS in Leadership"
    # the app prints the contact line itself (the writer was never handed it)
    assert packet.resume["contact"] == ["Oakfield, ST", "555-010-0199", "pat@example.test"]
    assert packet.comp["ask"] > 0
    assert role.status == "second_impression" and role.packet_complete is True, "a packet moves no role: Go does"
    assert tasks == [], "no read-the-packet task: a packet that passes is ready (path 23.2)"


def test_a_bullet_missing_its_full_stop_gets_one_and_a_sentence_that_ends_in_a_question_mark_is_left_alone():
    """Found 2026-10-20 reviewing drafts: some had a bullet the writer trimmed and left
    without its end punctuation. The app finishes the sentence itself: a full stop is not a judgement call."""
    cut = _cut("Led the risk working group across client engagements")
    cut.summary = "Builds programs that people use"
    cut.roles[0].bullets.append(packets.Cited.model_validate({"text": "Ran the board review?", "from": ["b-risk"]}))
    fixes = packets.mechanical_fixes(cut, _letter(), _made_up())
    assert cut.roles[0].bullets[0].text == "Led the risk working group across client engagements."
    assert cut.summary.endswith(".")
    assert cut.roles[0].bullets[1].text == "Ran the board review?"
    assert any("bullet 1 under Senior Manager at Northwind Labs: added the full stop" in f for f in fixes)


def test_a_tool_name_gets_the_masters_spelling_throughout_the_packet():
    resume = copy.deepcopy(RESUME)
    resume["roles"][0]["bullets"].append({"id": "b-jira", "text": "Ran the Jira board for six teams", "note": ""})
    cut = _cut("Ran the JIRA board for six teams.")
    cut.summary = "Runs the JIRA board for six teams."
    letter = packets.Letter.model_validate({"paragraphs": ["Dear hiring team,", "I keep work visible in JIRA."]})
    fixes = packets.mechanical_fixes(cut, letter, _made_up(resume))
    assert cut.summary == "Runs the Jira board for six teams."
    assert cut.roles[0].bullets[0].text == "Ran the Jira board for six teams."
    assert letter.paragraphs[1] == "I keep work visible in Jira."
    assert any("'JIRA' as 'Jira'" in f for f in fixes)


def test_names_the_master_does_not_hold_take_the_spelling_the_packet_uses_most():
    cut = _cut("Led the risk working group on GitHub.")
    cut.summary = "Builds programs on GitHub and Github, across client engagements."
    packets.mechanical_fixes(cut, _letter(), _made_up())
    assert cut.summary == "Builds programs on GitHub and GitHub, across client engagements."


async def test_a_second_version_carries_the_change_asked_for_and_replaces_the_first(js):
    role_id = await packet_setup()
    await _start(role_id)
    await write_packet()
    await _start(role_id, "lead with the knowledge base")
    seen: list[str] = []

    def compose(system: str) -> dict:
        seen.append(system)
        return compose_reply()

    await run_seats({"Compose": compose, "Facts_1": FACTS_CLEAN})
    assert "lead with the knowledge base" in seen[0] and "THE VERSION THEY ASKED TO CHANGE" in seen[0]
    async with db.session_factory()() as session:
        states = {p.version: p.status for p in (await session.scalars(select(Packet))).all()}
    assert states == {1: "replaced", 2: "ready"}


async def test_the_runner_is_told_a_packet_waits_and_the_runner_holds_the_writer_role(js):
    from app.jobsearch.work import session_kinds

    role_id = await packet_setup()
    async with db.session_factory()() as session:
        conn = await roster.connection_by_slug(session, OWNER, roster.SCOUT)
        assert {roster.ANALYST, roster.WRITER} <= set(conn.roles)
        assert await session_kinds.next_kind(session, OWNER) is None
    await _start(role_id)
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEAT


async def test_the_routes_ask_for_a_packet_and_list_versions_with_no_approve_step(client):
    role_id = await packet_setup()
    asked = await client.post(f"/api/v1/roles/{role_id}/packet", json={})
    assert asked.status_code == 200 and asked.json()["status"] == "writing" and asked.json()["version"] == 1
    again = await client.post(f"/api/v1/roles/{role_id}/packet", json={})
    assert again.status_code == 409
    await write_packet()
    async with db.session_factory()() as session:
        listed = await packets.for_role(session, OWNER, role_id)
    assert [p["status"] for p in listed] == ["ready"] and listed[0]["resume"]["headline"] == "Programs Leader"
    approve = await client.post(f"/api/v1/packets/{listed[0]['id']}/approve")
    assert approve.status_code in (404, 405), "no approve step"
    assert (await client.post(f"/api/v1/roles/{uuid.uuid4()}/packet", json={})).status_code == 404


def test_the_checks_note_a_long_bullet_without_refusing_it():
    checks = packets.packet_checks(_cut("word " * 60), ["Dear team."], ["b-risk"], _made_up())
    long = next(c for c in checks if c["check"] == "Bullets are short")
    assert long["ok"] is False and long["blocking"] is False
