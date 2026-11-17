"""The re-gate (docs/PRD.md 39.14 and 39.25): saving Hard pass, gating or ungating an employer and adding or removing a
site to avoid check every posting in the log and every role again, in code, with un-gating, and say in one line what
they changed. Everything here is made up."""

import asyncio
import contextlib
import math
import re
import time
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event, func, select, update

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.actors import BY_APP
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import regate, sift
from app.jobsearch.imports import tracker
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Employer, JobPosting, Opportunity, Packet
from app.jobsearch.pipeline import employer_limit, placement
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.writing import packets
from app.models.work import TrailEvent
from app.trail import service as trail
from tests.builders import analysis_setup, findings, save_documents, submit_findings
from tests.support import (
    EXAMPLE_SYSTEM,
    FEW_ARGUMENTS,
    FIVE,
    OWNER,
    STATEMENT_ARGUMENT_LIMIT,
    answered,
    bare,
    bound_arguments,
    load_profile,
    tracker_row,
    tracker_workbook,
)

NOW = datetime(2026, 10, 12, 5, 30, tzinfo=UTC)
TODAY = date(2026, 10, 12)
GATES = "/api/v1/profile/gates"


def gates(floor: int, **more) -> dict:
    """Remote work only, and at least `floor` a year on the posted maximum."""
    return {"location": {"remote": {"on": True}}, "compensation": {"amount": floor, "against": "maximum"}, **more}


def posting(employer: Employer, ext: str, top: int | None, **more) -> JobPosting:
    """A posting that is remote and pays up to `top` (none: no pay posted), as a board listed it."""
    return JobPosting(**{
        "owner": OWNER, "employer_id": employer.id, "ats": EXAMPLE_SYSTEM, "ext_id": ext, "company": employer.name,
        "title": f"Programs Director {ext}", "match_key": match_key(employer.name, f"Programs Director {ext}"),
        "first_seen": NOW, "last_seen": NOW, "links": [], "location": "Remote", "work_mode": "remote",
        "comp_min": top - 10_000 if top else None, "comp_max": top, "url": f"https://jobs.example.test/{ext}", **more})


def role(slug: str, status: str, top: int | None = None, **more) -> Opportunity:
    """A role that is remote and pays up to `top` (none: no pay posted)."""
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": "Fabrikam", "title": f"Lead {slug}", "fit": 3, "origin": "scout",
        "status": status, "match_key": f"fabrikam|lead {slug}", "found": TODAY, "location": "Remote",
        "move_type": "remote", "comp": f"${top - 10_000:,}-${top:,}" if top else None, **more})


async def history(session, row: Opportunity) -> list[TrailEvent]:
    return list((await session.scalars(select(TrailEvent).where(
        TrailEvent.link_kind == "opportunity", TrailEvent.link_id == row.id).order_by(TrailEvent.at))).all())


async def by_slug(session) -> dict[str, Opportunity]:
    return {r.slug: r for r in (await session.scalars(select(Opportunity))).all()}


async def by_ext(session) -> dict[str, JobPosting]:
    return {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}


async def judge(session, cause: str = "a test set it up") -> regate.Report:
    """Judge what is there, under the Hard pass in force: the filter for the postings nobody filtered, the re-gate for
    the roles."""
    await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)
    return await regate.run(session, OWNER, cause=cause, today=TODAY)


# --- saving Hard pass ----------------------------------------------------------------------------------------------


async def test_loosening_a_gate_sends_back_what_it_stopped_and_tightening_stops_what_fails_in_one_save(js, client):
    """The lowest pay at 95,000, then 50,000, then 150,000: each save is one request, with no click after it."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))  # Hard pass, version 1
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([posting(fab, "low", 80_000), posting(fab, "mid", 100_000), posting(fab, "ok", 200_000),
                         posting(fab, "none", None)])
        session.add_all([role("low", stages.SECOND_IMPRESSION, 80_000), role("mid", stages.SECOND_IMPRESSION, 100_000),
                         role("ok", stages.SECOND_IMPRESSION, 200_000), role("none", stages.SECOND_IMPRESSION)])
        await session.flush()
        report = await judge(session)
        await session.commit()
        assert (report.roles_stopped, report.postings_stopped) == (1, 0), "the filter stopped the posting itself"
        posts = await by_ext(session)
        assert (posts["low"].stop_gate, posts["low"].stop_gate_id, posts["low"].stop_hard_pass) == (
            "compensation", "compensation", 1)
        assert posts["mid"].filtered_by is None and posts["mid"].route == "roles"

    loosened = await client.put(GATES, json={"data": gates(50_000), "why": "lower the floor"})
    assert loosened.status_code == 200 and loosened.json()["version"] == 2
    assert loosened.json()["said"] == "Re-checked 4 postings and 4 roles: 2 now pass.", "one posting and one role"
    async with db.session_factory()() as session:
        posts, roles = await by_ext(session), await by_slug(session)
        low = posts["low"]
        assert (low.filtered_by, low.route, low.judged_by, low.stop_gate, low.stop_gate_id, low.stop_hard_pass) == (
            None, "roles", None, None, None, None), "on to become a role, with no stop"
        assert low.route_why is None
        assert roles["low"].status == stages.SECOND_IMPRESSION and roles["low"].screened_out_reason is None
        assert (roles["low"].screened_out_by, roles["low"].screened_out_step) == (None, None)
        lines = [e for e in await history(session, roles["low"]) if e.event == "regate_released"]
        assert len(lines) == 1 and lines[0].sentence == (
            "Back in Second Impression: the gate that stopped it no longer does.")
        assert "Hard pass version 2 was saved" in (lines[0].detail or ""), "the history says what changed"
        assert "It was Gated out: Pay must" in (lines[0].detail or ""), "and what it was"
        assert roles["ok"].status == stages.SECOND_IMPRESSION

    tightened = await client.put(GATES, json={"data": gates(150_000), "why": "raise the floor"})
    assert tightened.status_code == 200 and tightened.json()["version"] == 3
    assert tightened.json()["said"] == "Re-checked 4 postings and 4 roles: 4 newly stopped.", "two postings, two roles"
    async with db.session_factory()() as session:
        posts, roles = await by_ext(session), await by_slug(session)
        for ext in ("low", "mid"):
            assert (posts[ext].filtered_by, posts[ext].stop_gate, posts[ext].stop_gate_id, posts[ext].judged_by,
                    posts[ext].stop_hard_pass) == (
                "Pay must be at least $150,000 a year, on the posted maximum.", "compensation", "compensation",
                "gate", 3), ext
            assert posts[ext].route is None and posts[ext].judged_by_version == 1
        assert posts["ok"].filtered_by is None and posts["none"].filtered_by is None, "passes until proven failed"
        for slug in ("low", "mid"):
            row = roles[slug]
            assert (row.status, row.screened_out_by, row.screened_out_step, row.screened_out_gate_id) == (
                stages.GATED_OUT, "compensation", "gate", "compensation"), slug
            assert (row.screened_out_hard_pass, row.screened_out_criteria) == (3, 1)
            assert row.screened_out_reason.startswith("Pay must be at least $150,000 a year, on the posted maximum")
            stopped = [e for e in await history(session, row) if e.event == "regate_stopped"][-1]
            assert stopped.sentence.startswith("Gated out: Pay must be at least $150,000")
            assert "Hard pass version 3 was saved" in (stopped.detail or "") and "passed them before" in (
                stopped.detail or "")
        assert roles["ok"].status == roles["none"].status == stages.SECOND_IMPRESSION


async def test_a_posting_a_tab_said_yes_to_goes_on_to_become_a_role_when_the_tabs_own_gate_stops_it_no_more(js, client):
    """The title check named the tab (the posting holds it) and the tab's own lowest pay stopped it: when that gate
    loosens it goes on to become a role, as every posting that passes the gates does, and keeps the tab (docs/PRD.md
    39.14: it was back with a reader before the title pass was retired)."""
    tab = {"by_role": {"builders": {"compensation": {"amount": 120_000, "against": "maximum"}}}}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000, **tab))  # the made-up profile has a "builders" tab
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([
            posting(fab, "yes", 100_000, filtered_at=NOW, route="reader", judged_by="titles", judged_by_version=1,
                    target_role="builders", route_why="title looks right: made up"),
            posting(fab, "other", 100_000, filtered_at=NOW, route="reader", judged_by="titles", judged_by_version=1,
                    target_role="analysts")])
        await session.flush()
        report = await regate.run(session, OWNER, cause="a test set it up", today=TODAY)
        await session.commit()
        assert (report.postings_stopped, report.roles_stopped) == (1, 0), "only the posting the tab named"
        held = (await by_ext(session))["yes"]
        assert (held.filtered_by, held.stop_gate, held.stop_gate_id, held.judged_by, held.target_role) == (
            "Pay must be at least $120,000 a year, on the posted maximum.", "compensation", "compensation:builders",
            "gate", "builders")
    loosened = await client.put(GATES, json={"data": gates(50_000, by_role={"builders": {"compensation": {
        "amount": 90_000, "against": "maximum"}}})})
    assert loosened.json()["said"] == "Re-checked 2 postings and 0 roles: 1 now pass."
    async with db.session_factory()() as session:
        back = (await by_ext(session))["yes"]
        assert (back.filtered_by, back.route, back.judged_by, back.target_role, back.stop_gate) == (
            None, "roles", None, "builders", None), "a role is made of it: the tab already said yes"
        assert back.judged_by_version is None and back.route_why is None


async def test_a_save_that_changes_nothing_says_so(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([posting(fab, "ok", 200_000), role("ok", stages.SECOND_IMPRESSION, 200_000)])
        await session.flush()
        await judge(session)
        await session.commit()
    saved = await client.put(GATES, json={"data": gates(94_000), "why": "a little lower"})
    assert saved.json()["said"] == "Re-checked 1 posting and 1 role: nothing changed."
    unchanged = await client.put(GATES, json={"data": gates(94_000), "why": "the same"})
    assert bare(unchanged.json()) == {"saved": False, "version": None}, (
        "nothing was saved, so nothing was checked again")


async def test_a_whole_log_is_checked_in_seconds_with_a_few_statements_for_the_postings(js):
    """A real log is far larger than this fixture (docs/PRD.md 39.14). Here, 6,000 postings and 300 roles: the
    postings are read once and written in slices of `regate.CHUNK`, a few statements each, so the statements that touch
    them grow with the slices and never with the postings."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([posting(fab, str(n), 60_000 + (n % 100) * 2_000, filtered_at=NOW, route="titles")
                         for n in range(6_000)])
        session.add_all([role(str(n), stages.SECOND_IMPRESSION, 60_000 + (n % 100) * 2_000) for n in range(300)])
        await session.flush()
        first = await regate.run(session, OWNER, cause="a test set it up", today=TODAY)
        assert first.postings_stopped > 900 and first.roles_stopped > 40, "under 95,000 is 18 in every 100"
        await session.commit()
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(160_000), by="test")
        seen: list[str] = []

        def noted(_conn, _cursor, statement, _parameters, _context, _many):
            if "js_postings" in statement:
                seen.append(statement)

        engine = db.engine().sync_engine
        event.listen(engine, "before_cursor_execute", noted)
        started = time.perf_counter()
        try:
            report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        finally:
            event.remove(engine, "before_cursor_execute", noted)
        took = time.perf_counter() - started
        await session.commit()
    assert report.postings == 6_000 and report.roles == 300
    assert report.postings_stopped > 1_500 and report.roles_stopped > 80, "under 160,000 is 50 in every 100"
    slices = math.ceil(6_000 / regate.CHUNK)
    assert len(seen) <= 1 + 3 * slices, f"{len(seen)} statements touched the postings: one read, three a slice at most"
    assert took < 30, f"{took:.1f} seconds"


# --- the roles no re-run moves, and the packet of one that is stopped ---------------------------------------------


async def test_a_ready_to_apply_role_a_tightened_gate_stops_goes_to_gated_out_with_its_packet_kept(js, client):
    """docs/PRD.md 39.25: a role at Applied or later, and a role the owner moved by hand, stay where they are."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        rows = {
            "ready": role("ready", stages.READY_TO_LAUNCH, 100_000),
            "applied": role("applied", stages.APPLIED, 100_000),
            # Another employer: two open applications at one employer would hold the roles beside them (PRD 34.2).
            "talks": role("talks", stages.ACTIVE_DISCUSSION, 100_000, company="Contoso"),
            "found": role("found", stages.SECOND_IMPRESSION, 100_000),
            "by-hand": role("by-hand", stages.READY_TO_LAUNCH, 100_000),
            "by-the-mission": role("by-the-mission", stages.READY_TO_LAUNCH, 100_000),
        }
        session.add_all(rows.values())
        await session.flush()
        session.add(Packet(owner=OWNER, role_id=rows["ready"].id, version=1, status="ready", brief={}, resume={},
                           letter={}, comp={}, reasoning={}, checks=[]))
        # The owner moved one role on the board (23.8); the nightly mission asked for a packet for the other.
        await trail.record(session, OWNER, event="role_moved", sentence="You moved it.", actor="person",
                           link=("opportunity", rows["by-hand"].id))
        await trail.record(session, OWNER, event="packet_asked", sentence="The packets mission asked for a packet.",
                           actor="app", link=("opportunity", rows["by-the-mission"].id))
        await session.commit()
    tightened = await client.put(GATES, json={"data": gates(150_000)})
    assert tightened.json()["said"] == "Re-checked 0 postings and 3 roles: 3 newly stopped."
    async with db.session_factory()() as session:
        roles = await by_slug(session)
        assert roles["ready"].status == stages.GATED_OUT and roles["ready"].screened_out_by == "compensation"
        packet = (await session.scalars(select(Packet).where(Packet.role_id == roles["ready"].id))).one()
        assert (packet.status, packet.version) == ("ready", 1), "the packet is kept as it was"
        stop = [e for e in await history(session, roles["ready"]) if e.event == "regate_stopped"][0]
        assert "Its packet is kept." in (stop.detail or "")
        assert {r: roles[r].status for r in ("found", "by-the-mission")} == {
            "found": stages.GATED_OUT, "by-the-mission": stages.GATED_OUT}, "the mission's ask is not the owner's hand"
        assert roles["applied"].status == stages.APPLIED and roles["talks"].status == stages.ACTIVE_DISCUSSION
        assert roles["by-hand"].status == stages.READY_TO_LAUNCH, "a role the owner moved by hand stays"
        assert roles["applied"].screened_out_reason is None
    # Loosened again, what the gate stopped goes back to where it was.
    await client.put(GATES, json={"data": gates(50_000)})
    async with db.session_factory()() as session:
        roles = await by_slug(session)
        assert roles["ready"].status == stages.READY_TO_LAUNCH, "a ready packet: back to Ready to Apply"
        assert roles["found"].status == stages.SECOND_IMPRESSION
        assert roles["by-the-mission"].status == stages.READY_TO_LAUNCH, "back to the stage it was taken from"


async def test_an_ask_for_a_packet_is_written_down_as_the_owners_or_the_apps_and_is_no_hand_move(js):
    """The nightly packets mission asks for packets too, and its asks used to be written down as the owner's."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        mine, theirs = role("mine", stages.SECOND_IMPRESSION, 100_000), role("theirs", stages.READY_TO_LAUNCH, 100_000)
        session.add_all([mine, theirs])
        await session.flush()
        await packets.start(session, OWNER, mine.id)
        await packets.start(session, OWNER, theirs.id, by=BY_APP)
        assert not (await placement.one(session, OWNER, mine, placement.Purpose.GATES)).held
        assert not (await placement.one(session, OWNER, theirs, placement.Purpose.GATES)).held
        asked = {e.link_id: (e.actor, e.who) for e in (await session.scalars(
            select(TrailEvent).where(TrailEvent.event == "packet_asked"))).all()}
        assert asked[theirs.id] == ("app", trail.APP_WHO) and asked[mine.id][0] == "person"


async def test_a_packet_request_is_not_a_hand_move_and_a_board_drag_is(js):
    """A packet request is not a stage move: the role is re-gated and moves out on a failing gate. A role the owner
    dragged on the board stays exempt."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(150_000))
        asked = role("asked", stages.READY_TO_LAUNCH, 100_000)
        dragged = role("dragged", stages.READY_TO_LAUNCH, 100_000)
        session.add_all([asked, dragged])
        await session.flush()
        await trail.record(session, OWNER, event="packet_asked", sentence="You asked for a packet.", actor="person",
                           link=("opportunity", asked.id))
        await trail.record(session, OWNER, event="role_moved", sentence="You moved it.", actor="person",
                           link=("opportunity", dragged.id))
        assert not (await placement.one(session, OWNER, asked, placement.Purpose.GATES)).held
        assert (await placement.one(session, OWNER, dragged, placement.Purpose.GATES)).held
        await regate.run(session, OWNER, cause="test")
        roles = await by_slug(session)
        assert roles["asked"].status == stages.GATED_OUT
        assert roles["dragged"].status == stages.READY_TO_LAUNCH


# --- where a role goes back to --------------------------------------------------------------------------------------


async def test_a_role_goes_back_to_the_stage_a_gate_took_it_from_and_its_history_says_why(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000), scoring=FIVE)
        breakdown = scoring.compute(answered("met", "met", "not"), await profile_store.current(session, OWNER))
        researched = {"analysis_state": "done", "fit": breakdown["total"], "fit_breakdown": breakdown}  # 80%
        waiting = {"fit": breakdown["total"], "fit_breakdown": breakdown}
        session.add_all([role("researched", stages.SECOND_IMPRESSION, 80_000, **researched),
                         role("found", stages.SECOND_IMPRESSION, 80_000, **waiting),
                         role("asked", stages.READY_TO_LAUNCH, 80_000)])
        await session.flush()
        asked = (await by_slug(session))["asked"]
        session.add(Packet(owner=OWNER, role_id=asked.id, version=1, status="writing", brief={}, resume={}, letter={},
                           comp={}, reasoning={}, checks=[]))
        report = await regate.run(session, OWNER, cause="a test set it up", today=TODAY)
        assert report.roles_stopped == 3
        await session.commit()
    await client.put(GATES, json={"data": gates(50_000)})
    async with db.session_factory()() as session:
        roles = await by_slug(session)
        # A score never moves a role: each goes back to the stage the gate took it from (a packet being written keeps
        # Ready to Launch), and a researched role waits on deck for the pipeline to promote it.
        assert roles["researched"].status == stages.SECOND_IMPRESSION, "researched, back on deck"
        assert roles["found"].status == stages.SECOND_IMPRESSION, "not researched: the analyst's move"
        assert roles["asked"].status == stages.READY_TO_LAUNCH, "its packet is being written"
        for slug, stage in (("researched", "Second Impression"), ("found", "Second Impression"),
                            ("asked", "Ready to Launch")):
            lines = [e for e in await history(session, roles[slug]) if e.event == "regate_released"]
            assert [e.sentence for e in lines] == [f"Back in {stage}: the gate that stopped it no longer does."], slug


# --- an employer, a site to avoid ----------------------------------------------------------------------------------


async def test_gating_and_ungating_an_employer_check_the_log_the_same_way(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000))
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        hooli = await employers.find_or_make(session, OWNER, "Hooli")
        session.add_all([posting(fab, "fab", 100_000), posting(hooli, "hooli", 100_000),
                         role("fab", stages.SECOND_IMPRESSION, 100_000)])
        await session.flush()
        await judge(session)
        await session.commit()
        fab_id = fab.id
    gated = await client.patch(f"/api/v1/employers/{fab_id}", json={"gated": True, "gated_reason": "never again"})
    assert gated.status_code == 200
    assert gated.json()["said"] == "Re-checked 2 postings and 1 role: 2 newly stopped."
    async with db.session_factory()() as session:
        posts, roles = await by_ext(session), await by_slug(session)
        assert (posts["fab"].filtered_by, posts["fab"].stop_gate, posts["fab"].stop_gate_id) == (
            "an employer you never work for", "employer", "employer")
        assert posts["hooli"].filtered_by is None
        assert (roles["fab"].status, roles["fab"].screened_out_reason) == (
            stages.GATED_OUT, "employer gated: never again")
    back = await client.patch(f"/api/v1/employers/{fab_id}", json={"gated": False})
    assert back.json()["said"] == "Re-checked 2 postings and 1 role: 2 now pass."
    async with db.session_factory()() as session:
        posts, roles = await by_ext(session), await by_slug(session)
        assert posts["fab"].filtered_by is None and posts["fab"].route == "roles"
        assert roles["fab"].status == stages.SECOND_IMPRESSION


async def test_adding_and_removing_a_site_to_avoid_check_the_log_the_same_way(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000))
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        on_the_site = posting(fab, "on", 100_000, url="https://careers.listings.example.test/jobs/1")
        session.add_all([on_the_site, posting(fab, "off", 100_000), role("on", stages.SECOND_IMPRESSION, 100_000)])
        await session.flush()
        await judge(session)
        await session.commit()
    added = await client.post("/api/v1/sites", json={"url": "https://listings.example.test/", "list": "avoid"})
    assert added.status_code == 200 and added.json()["said"] == "Re-checked 2 postings and 1 role: 1 newly stopped."
    async with db.session_factory()() as session:
        posts = await by_ext(session)
        assert (posts["on"].filtered_by, posts["on"].stop_gate, posts["on"].stop_gate_id) == (
            "a site you avoid", "site", "listings.example.test")
        assert posts["off"].filtered_by is None
        on = (await by_slug(session))["on"]
        assert on.status == stages.SECOND_IMPRESSION, "a role is not a posting: its site stops nothing"
    removed = await client.delete(f"/api/v1/sites/{added.json()['id']}")
    assert removed.json() == {"ok": True, "said": "Re-checked 2 postings and 1 role: 1 now pass."}
    async with db.session_factory()() as session:
        assert (await by_ext(session))["on"].filtered_by is None


# --- stops made before they were data; the stops no gate made -------------------------------------------------------


async def test_a_stop_made_before_stops_were_data_is_worked_out_again_by_the_first_regate(js):
    """docs/PRD.md 39.12: a gate's stop with no step, no line and no version is worked out again from what is stored and
    takes the versions in force; the retired title check's own stop is never undone (and no longer stamped)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([
            posting(fab, "old", 80_000, filtered_at=NOW, filtered_by="the place", judged_by="gate"),
            posting(fab, "title", 80_000, filtered_at=NOW, filtered_by="not your kind of job: x", judged_by="titles"),
            posting(fab, "gone", 200_000, filtered_at=NOW, filtered_by="the place", judged_by="gate"),
            role("old", stages.GATED_OUT, 80_000, screened_out_reason="location: not remote, local or secondary",
                 screened_out_by="other"),
            role("cleared", stages.GATED_OUT, 200_000, screened_out_reason="pay under your amount",
                 screened_out_by="other"),
        ])
        await session.flush()
        report = await regate.run(session, OWNER, cause="the first", today=TODAY)
        await session.commit()
        assert (report.postings_released, report.postings_restated, report.roles_returned, report.roles_restated) == (
            1, 1, 1, 1)
        posts, roles = await by_ext(session), await by_slug(session)
        old = posts["old"]
        assert (old.filtered_by, old.stop_gate, old.stop_gate_id, old.stop_hard_pass, old.judged_by_version) == (
            "Pay must be at least $95,000 a year, on the posted maximum.", "compensation", "compensation", 1, 1)
        assert posts["title"].filtered_by == "not your kind of job: x", "the title check's stop is not a gate's to undo"
        assert (posts["title"].stop_gate, posts["title"].stop_hard_pass, posts["title"].judged_by_version) == (
            None, None, None), "and is left as it was kept: nothing stamps it"
        assert posts["gone"].filtered_by is None and posts["gone"].route == "roles", "no gate stops it now"
        assert (roles["old"].status, roles["old"].screened_out_by, roles["old"].screened_out_step,
                roles["old"].screened_out_gate_id, roles["old"].screened_out_hard_pass) == (
            stages.GATED_OUT, "compensation", "gate", "compensation", 1)
        # It was stored already Gated out, so no stage it left is known: it goes back to First Impression.
        assert roles["cleared"].status == stages.FIRST_IMPRESSION and roles["cleared"].screened_out_step is None
        # After the first re-gate every gate-stopped posting and every Gated out role holds its step, its gate and both
        # versions (docs/PRD.md 39.12).
        for stopped in (p_ for p_ in posts.values() if p_.filtered_by is not None and p_.judged_by == "gate"):
            assert None not in (stopped.judged_by, stopped.stop_gate, stopped.judged_by_version,
                                stopped.stop_hard_pass), stopped.ext_id
        for out in (r for r in roles.values() if r.status == stages.GATED_OUT):
            assert None not in (out.screened_out_step, out.screened_out_by, out.screened_out_gate_id,
                                out.screened_out_hard_pass, out.screened_out_criteria), out.slug


async def test_a_role_abroad_with_no_way_of_working_closes_at_the_place_gate_at_the_first_start_after_the_rule(js):
    """The owner, 2026-10-23: a place that fails remote (their country only), hybrid and in person (their towns only)
    with no way of working stated is a fail. Migration 0112 unstamps the place gate's stops, so the start's re-gate
    (`regate.catch_up`) checks every role again: the open role abroad closes with the gate's reason and a line in its
    history; one a way of working could rescue, and one the owner moved by hand, stay."""
    place = {"remote": {"on": True, "locations": ["United States"]},
             "in_person": {"on": True, "locations": ["Oakfield"]}}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"location": place})
        session.add_all([
            role("abroad", stages.FIRST_IMPRESSION, location="Tokyo, Japan", move_type=None),
            role("rescued", stages.FIRST_IMPRESSION, location="Dallas, TX", move_type=None),
            role("hand", stages.FIRST_IMPRESSION, location="Tokyo, Japan", move_type=None),
            # A stop the place gate made, as migration 0112 leaves it: judged under no Hard pass.
            role("stopped", stages.GATED_OUT, location="Lisbon, Portugal", move_type="onsite",
                 screened_out_reason="Work must be remote or in person.", screened_out_by="location",
                 screened_out_step="gate", screened_out_gate_id="location"),
        ])
        await session.flush()
        await trail.record(session, OWNER, event="role_moved", actor="person", who="Owner", sentence="You moved it.",
                           link=("opportunity", (await by_slug(session))["hand"].id))
        await session.commit()
    async with db.session_factory()() as session:
        report = await regate.catch_up(session, OWNER)
        await session.commit()
        assert report is not None and report.roles_stopped == 1
        rows = await by_slug(session)
        abroad = rows["abroad"]
        assert (abroad.status, abroad.screened_out_by, abroad.screened_out_step) == (
            stages.GATED_OUT, "location", "gate")
        assert "fails whether remote, hybrid or in person" in abroad.screened_out_reason
        lines = [e for e in await history(session, abroad) if e.event == regate.STOPPED_EVENT]
        assert len(lines) == 1 and lines[0].sentence.startswith("Gated out: Work must be remote")
        assert rows["rescued"].status == stages.FIRST_IMPRESSION, "remote in the owner's country could rescue it"
        assert rows["hand"].status == stages.FIRST_IMPRESSION, "a role the owner moved by hand stays"
        assert rows["stopped"].screened_out_hard_pass is not None, "a stop that holds is stamped again"
        assert await regate.catch_up(session, OWNER) is None, "nothing stale is left"


async def test_a_posting_the_title_check_stopped_the_owner_asked_for_or_a_role_was_made_from_is_left_alone(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        made = role("made", stages.SECOND_IMPRESSION, 80_000)
        session.add(made)
        await session.flush()
        session.add_all([
            posting(fab, "title", 80_000, filtered_at=NOW, filtered_by="not your kind of job: x", judged_by="titles"),
            posting(fab, "asked", 80_000, filtered_at=NOW, route="reader", judged_by="you", owner_override=True),
            posting(fab, "linked", 80_000, filtered_at=NOW, opportunity_id=made.id),
            posting(fab, "unfiltered", 80_000),
        ])
        await session.flush()
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        posts = await by_ext(session)
        assert report.postings == 1, "only the title check's stop was in play, and it is left as it is"
        assert posts["title"].filtered_by == "not your kind of job: x"
        assert posts["asked"].filtered_by is None and posts["asked"].route == "reader", "read anyway: never moved"
        assert posts["linked"].filtered_by is None, "a posting a role was made from is the role's"
        assert posts["unfiltered"].filtered_at is None, "nobody has filtered it: the next filter does"


async def test_a_role_the_title_check_screened_out_stays_gated_out_when_the_gates_are_checked_again(js):
    """The title check's stop of a role (its step is `titles`) is no gate's to undo: a gate does not read What to look
    for, so no re-gate returns the role though no gate stops it now (`regate.KEPT_STEPS`, docs/PRD.md 39.14). A role a
    gate stopped that passes now goes back, beside it."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        session.add_all([
            role("screened", stages.GATED_OUT, 200_000, screened_out_by="kind", screened_out_step="titles",
                 screened_out_reason="judged again: the title no longer matches what to look for"),
            role("by-a-gate", stages.GATED_OUT, 200_000, screened_out_reason="pay under your amount",
                 screened_out_by="other")])
        await session.flush()
        report = await regate.run(session, OWNER, cause="Hard pass was saved", today=TODAY)
        await session.commit()
        roles = await by_slug(session)
    assert report.roles_left == 1 and report.roles_returned == 1
    assert (roles["screened"].status, roles["screened"].screened_out_step) == (stages.GATED_OUT, "titles")
    assert roles["by-a-gate"].status == stages.FIRST_IMPRESSION, "a gate's stop that no longer holds is returned"


async def test_a_stop_no_re_gate_restamps_does_not_make_the_catch_up_run_at_every_start(js):
    """Gated out roles the retired title check had stopped (its step is `titles`, judged
    under no or an old Hard pass) were counted by `stale` though a re-gate leaves them as they are (`KEPT_STEPS`) and so
    never stamps them: the count never fell and `catch_up` ran a whole re-gate at every start. `stale` counts only
    what a re-gate would write the current version to: a gate's stop under an older Hard pass, and one that holds no
    step at all, still count."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        session.add_all([
            role("title-check", stages.GATED_OUT, 200_000, screened_out_by="kind", screened_out_step="titles",
                 screened_out_reason="the title no longer matches", screened_out_hard_pass=None),
            role("older-title-check", stages.GATED_OUT, 200_000, screened_out_by="kind", screened_out_step="titles",
                 screened_out_reason="the title no longer matches", screened_out_hard_pass=0)])
        await session.flush()
        assert await regate.stale(session, OWNER) == 0, "nothing a re-gate would restamp is left"
        assert await regate.catch_up(session, OWNER) is None
        session.add_all([
            role("by-a-gate", stages.GATED_OUT, 50_000, screened_out_by="compensation", screened_out_step="gate",
                 screened_out_reason="pay under your amount", screened_out_hard_pass=0),
            role("no-step", stages.GATED_OUT, 50_000, screened_out_by="other", screened_out_reason="pay",
                 screened_out_hard_pass=None)])
        await session.flush()
        assert await regate.stale(session, OWNER) == 2, "a gate's old stop and a stop with no step are still found"


async def test_a_role_the_old_trackers_own_screening_put_in_gated_out_is_never_moved_by_a_regate(js):
    """No gate of this app stopped it, so no gate of this app can say it no longer holds."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        sheet = tracker_workbook([tracker_row("Initech", "Lead", status="rejected", fit="REJECTED - trajectory gate")])
        await tracker.import_tracker(session, OWNER, sheet)
        await session.flush()
        row = next(iter((await by_slug(session)).values()))
        assert (row.status, row.screened_out_step, row.screened_out_by) == (stages.GATED_OUT, "tracker", "other")
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        assert (report.roles, report.roles_left, report.roles_returned) == (0, 1, 0)
        assert row.status == stages.GATED_OUT and row.screened_out_step == "tracker"


# --- sentences the owner wrote -------------------------------------------------------------------------------------


async def test_a_stop_by_a_sentence_rule_no_longer_holds_when_the_rule_is_removed_or_reworded(js):
    """The reader's answer stays on the role as it was given; the rule it stopped the role by is gone or says
    something else now, so the stop does not hold (a new answer is another piece's to ask for, docs/PRD.md 39.15)."""
    travel = {"id": "travel", "kind": "rule", "section": "responsibilities", "text": "Travel of 20% or more."}
    answer = [{"id": "travel", "answer": "fail", "evidence": "Up to 40% travel."}]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000, other=[travel]))
        session.add_all([
            role("kept", stages.GATED_OUT, 100_000, gate_answers=answer, screened_out_reason=travel["text"],
                 screened_out_by="rule", screened_out_step="reader", screened_out_gate_id="travel"),
            role("found", stages.SECOND_IMPRESSION, 100_000, gate_answers=answer),
            role("removed", stages.GATED_OUT, 100_000, gate_answers=[{**answer[0], "id": "gone"}],
                 screened_out_reason="A rule that is gone.", screened_out_by="rule", screened_out_step="reader",
                 screened_out_gate_id="gone"),
        ])
        await session.flush()
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        roles = await by_slug(session)
        assert (report.roles_stopped, report.roles_returned) == (1, 1)
        assert roles["found"].status == stages.GATED_OUT, "a rule that is on, on the answer the role holds"
        assert (roles["found"].screened_out_by, roles["found"].screened_out_step) == ("rule", "reader")
        assert roles["kept"].status == stages.GATED_OUT and roles["kept"].screened_out_gate_id == "travel"
        assert roles["removed"].status == stages.FIRST_IMPRESSION, "its rule is not there: the stop does not hold"
        # The owner words the sentence otherwise: the answers were to the words that are gone, whatever else is saved.
        reworded = {**travel, "text": "Travel of 30% or more."}
        await profile_store.save(session, OWNER, "gates", gates(50_000, other=[reworded]), by="test")
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        assert report.roles_returned == 2
        assert (roles["kept"].status, roles["found"].status) == (stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION)
        assert roles["kept"].gate_answers == [{**answer[0], "says": travel["text"]}], (
            "the answer stays on record, under the words it was given for, unused")
        again = await regate.run(session, OWNER, cause="a test", today=TODAY)
        assert (again.roles_stopped, again.roles_returned) == (0, 0), "and a later re-gate does not read it again"


# --- a corrected class ----------------------------------------------------------------------------------------------


async def test_correcting_a_class_runs_the_roles_gates_again(js, client):
    """docs/PRD.md 39.14: a rule the analyst's classes decide, researched under classes, and then corrected."""
    ids = await analysis_setup()
    rule = {"id": "gaps", "kind": "rule", "section": "qualifications", "text": "Most items are unproven.",
            "from_classes": "U", "share": 80}
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", {"other": [rule]}, by="test")
        for company in ("Fabrikam", "Initech", "Hooli"):
            await analysis.start(session, OWNER, await session.get(Opportunity, ids[company]))
        await session.commit()

    def required(*classes):
        return [{"item": f"Item {n}", "class": c, **({"why": "the risk working group", "from": ["b-risk"]}
                                                      if c in ("P", "T") else {})}
                for n, c in enumerate(classes, start=1)]

    gated = await submit_findings(findings(ids["Fabrikam"], required=required("U", "U", "U", "U", "P")))
    assert gated["stage"] == stages.GATED_OUT, "4 of 5 Unproven is 80%: at the share"
    kept = await submit_findings(findings(ids["Initech"], required=required("U", "U", "U", "P", "P")))
    assert kept["stage"] != stages.GATED_OUT
    await submit_findings(findings(ids["Hooli"], required=required("U", "U", "U", "P", "P")))
    # The owner classes an Unproven item of the first role Proven: 60%, under the share, so the gate lets it go.
    fixed = await client.post(f"/api/v1/roles/{ids['Fabrikam']}/requirements",
                              json={"index": 0, "class": "P", "why": "I led it"})
    assert fixed.status_code == 200
    assert fixed.json()["said"] == "Back in Second Impression: the gate that stopped it no longer does."
    # And an item of the second role that was Proven is Unproven: 80%, so the gate stops it.
    worse = await client.post(f"/api/v1/roles/{ids['Initech']}/requirements",
                              json={"index": 3, "class": "U", "why": "I have not"})
    assert worse.json()["said"].startswith("Gated out: Most items are unproven (4 of 5 required items Unproven, 80%)")
    async with db.session_factory()() as session:
        roles = await by_slug(session)
        assert roles["fabrikam"].status == stages.SECOND_IMPRESSION and roles["fabrikam"].screened_out_step is None
        assert (roles["initech"].status, roles["initech"].screened_out_by, roles["initech"].screened_out_step,
                roles["initech"].screened_out_gate_id) == (stages.GATED_OUT, "rule", "gate", "gaps")
        lines = {e.event for e in await history(session, roles["initech"])}
        assert {"requirement_corrected", "regate_stopped"} <= lines, "the history has the correction and its effect"
        # A role the owner moved by hand does not move for a corrected class.
        await trail.record(session, OWNER, event="role_moved", sentence="You moved it.", actor="person",
                           link=("opportunity", ids["Hooli"]))
        await session.commit()
    hand = await client.post(f"/api/v1/roles/{ids['Hooli']}/requirements",
                             json={"index": 3, "class": "U", "why": "I have not"})
    assert "said" not in hand.json()
    async with db.session_factory()() as session:
        assert (await by_slug(session))["hooli"].status != stages.GATED_OUT


def test_the_line_a_save_says_counts_what_a_re_gate_did():
    report = regate.Report(postings=2_500, roles=100, postings_released=25, roles_returned=5, postings_stopped=4,
                           roles_stopped=1)
    assert report.said == "Re-checked 2,500 postings and 100 roles: 30 now pass, 5 newly stopped."
    assert regate.Report(postings=1, roles=1).said == "Re-checked 1 posting and 1 role: nothing changed."
    assert regate.Report(postings=2, roles=0, postings_stopped=2).said == (
        "Re-checked 2 postings and 0 roles: 2 newly stopped.")


# --- a long run is cut into slices --------------------------------------------------
#
# A re-gate reads first, then writes in slices of `regate.CHUNK` rows, each committed, under a lock that outlives
# the commits.

SLICE = 20


async def _stopped_in_the_log() -> tuple[int, int]:
    """What another session sees committed now: the postings a gate stops and the roles in Gated out. A reader's
    session: a writer's would wait for the turn the run in progress holds."""
    async with db.read_session_factory()() as other:
        posts = await other.scalar(select(func.count()).select_from(JobPosting).where(
            JobPosting.filtered_by.is_not(None)))
        roles = await other.scalar(select(func.count()).select_from(Opportunity).where(
            Opportunity.status == stages.GATED_OUT))
    return int(posts or 0), int(roles or 0)


def _named_locks_held() -> int:
    """How many named locks (`db.named_lock`, `db.transaction_lock`) any session holds in this process now."""
    return sum(1 for _, lock in db._named.values() if lock.locked())


async def _a_log_to_tighten(postings: int, roles: int, owner: str = OWNER, *, floor: int = 95_000) -> None:
    """`postings` passed postings and `roles` roles in Second Impression, paying 60,000 to 158,000, under a floor."""
    async with db.session_factory()() as session:
        await load_profile(session, owner, gates=gates(floor))
        fab = await employers.find_or_make(session, owner, "Fabrikam")
        session.add_all([posting(fab, str(n), 60_000 + (n % 50) * 2_000, owner=owner, filtered_at=NOW, route="titles")
                         for n in range(postings)])
        session.add_all([role(str(n), stages.SECOND_IMPRESSION, 60_000 + (n % 50) * 2_000, owner=owner)
                         for n in range(roles)])
        await session.commit()


async def test_a_regate_commits_in_slices_so_no_transaction_holds_more_than_a_slice(js, monkeypatch, transactions):
    monkeypatch.setattr(regate, "CHUNK", SLICE)
    await _a_log_to_tighten(200, 60)
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(300_000), by="test")
        await session.commit()
    written = {"postings": 0, "roles": 0}
    uncommitted: list[int] = []
    real_bulk, real_check = regate._bulk, regate._check_role

    async def bulk(session, rows):
        """Every earlier slice of postings is committed before the next is written, and none is over a slice."""
        posts, _ = await _stopped_in_the_log()
        uncommitted.append(written["postings"] - posts)
        assert len(rows) <= SLICE
        written["postings"] += len(rows)
        return await real_bulk(session, rows)

    async def check(session, row, *args, **kwargs):
        """The roles a slice stopped are committed before the slice after it begins: at most a slice is open."""
        _, roles = await _stopped_in_the_log()
        uncommitted.append(written["roles"] - roles)
        written["roles"] += 1
        return await real_check(session, row, *args, **kwargs)

    monkeypatch.setattr(regate, "_bulk", bulk)
    monkeypatch.setattr(regate, "_check_role", check)
    transactions.clear()
    async with db.session_factory()() as session:
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
    assert (report.postings_stopped, report.roles_stopped) == (200, 60)
    assert written == {"postings": 200, "roles": 60}
    assert max(uncommitted) < SLICE, f"a transaction held {max(uncommitted)} rows other than its own slice"
    writing = [t for t in transactions.ended if transactions.writes(t)]
    assert len(writing) > 10, f"{len(writing)} transactions wrote: one for each slice of 20 is 13 or more"
    assert await _stopped_in_the_log() == (200, 60)
    assert _named_locks_held() == 0, "the run's lock is let go with the run"


async def _a_writer_gets_the_turn() -> None:
    """Another writer takes the file's one turn now: in a test the turn of this task's own transaction, if still held,
    raises (`db.NestedWriter`) where a task of another request would have waited for it."""
    async with db.session_factory()() as other:
        await other.execute(select(1))
        await other.rollback()


async def test_a_regate_lets_another_writer_in_before_it_reads_each_page_and_while_it_judges(js, monkeypatch):
    """The first real-data rehearsal held one writer turn for 68 seconds: every posting and role was read, and the
    postings judged, inside it. Now a page (`regate.PAGE`) is read, the transaction ended, and only then judged."""
    monkeypatch.setattr(regate, "CHUNK", 10)
    monkeypatch.setattr(regate, "PAGE", 20)
    await _a_log_to_tighten(100, 60)
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(300_000), by="test")
        await session.commit()
    let_in: list[str] = []
    real_page, real_cooperate = regate._role_page, regate.Cooperate

    class Probing(real_cooperate):
        """The judging loop's pace-keeper: at each posting's turn to be judged another writer is let in."""

        async def __call__(self) -> None:
            await _a_writer_gets_the_turn()
            let_in.append("judging")

    async def page(session, owner, rules, report, cause, ids, span):
        """A page of roles is about to be read: the page before it, and the ids, are over and let go."""
        await _a_writer_gets_the_turn()
        let_in.append(len(ids))
        return await real_page(session, owner, rules, report, cause, ids, span)

    monkeypatch.setattr(regate, "Cooperate", Probing)
    monkeypatch.setattr(regate, "_role_page", page)
    async with db.session_factory()() as session:
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
    assert (report.postings_stopped, report.roles_stopped) == (100, 60)
    assert let_in.count("judging") == 100
    pages = [seen for seen in let_in if seen != "judging"]
    assert pages == [20, 20, 20], "the roles were read in pages of 20, and a writer got in before each"


async def test_a_sliced_regate_reports_what_the_one_transaction_run_reports(js, monkeypatch):
    """The same log under two owners: one re-gated as a request does it, committing slice by slice, the other inside
    a savepoint, where nothing can be released and the whole run is one transaction as it always was."""
    monkeypatch.setattr(regate, "CHUNK", 7)
    monkeypatch.setattr(regate, "PAGE", 11)  # and read a page at a time, which changes none of what it reports
    other = "another"
    for owner in (OWNER, other):
        await _a_log_to_tighten(45, 30, owner)
        async with db.session_factory()() as session:
            fab = await employers.find_or_make(session, owner, "Fabrikam")
            session.add_all([
                # Stopped by a gate before and still: its stop is stated again. Stopped and no longer: it is released.
                posting(fab, "old", 80_000, owner=owner, filtered_at=NOW, filtered_by="the place", judged_by="gate"),
                posting(fab, "gone", 200_000, owner=owner, filtered_at=NOW, filtered_by="the place", judged_by="gate"),
                posting(fab, "title", 80_000, owner=owner, filtered_at=NOW, filtered_by="not your kind of job: x",
                        judged_by="titles"),
                role("old", stages.GATED_OUT, 80_000, owner=owner, screened_out_reason="somewhere else",
                     screened_out_by="other"),
                role("cleared", stages.GATED_OUT, 200_000, owner=owner, screened_out_reason="somewhere else",
                     screened_out_by="other"),
            ])
            await session.commit()
        async with db.session_factory()() as session:
            await profile_store.save(session, owner, "gates", gates(120_000), by="test")
            await session.commit()
    async with db.session_factory()() as session:
        sliced = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
    async with db.session_factory()() as session:
        async with session.begin_nested():
            whole = await regate.run(session, other, cause="a test", today=TODAY)
        await session.commit()
    assert sliced.as_dict() == whole.as_dict()
    assert sliced.postings_stopped > 7 and sliced.roles_stopped > 7 and sliced.postings_released == 1
    assert sliced.postings_restated == 1 and sliced.roles_returned == 1 and sliced.roles_restated == 1

    async def rows(owner: str) -> tuple[list, list]:
        async with db.session_factory()() as session:
            posts = (await session.scalars(select(JobPosting).where(JobPosting.owner == owner))).all()
            roles = (await session.scalars(select(Opportunity).where(Opportunity.owner == owner))).all()
            return (sorted((p.ext_id, p.filtered_by, p.stop_gate, p.stop_gate_id, p.stop_hard_pass, p.judged_by,
                            p.judged_by_version, p.route) for p in posts),
                    sorted((r.slug, r.status, r.screened_out_by, r.screened_out_step, r.screened_out_gate_id,
                            r.screened_out_hard_pass, r.screened_out_reason) for r in roles))

    assert await rows(OWNER) == await rows(other)


async def test_a_role_the_owner_moves_during_a_regate_is_not_moved_back(js, monkeypatch):
    """The re-gate plans from what it read; the owner drags a role on the board while it works. The role is
    locked and looked at again just before it is changed, and is where the owner put it (docs/PRD.md 23.8)."""
    monkeypatch.setattr(regate, "CHUNK", SLICE)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000))
        session.add_all([role(slug, stages.SECOND_IMPRESSION, 100_000) for slug in ("a", "b", "dragged")])
        await session.commit()
        await profile_store.save(session, OWNER, "gates", gates(150_000), by="test")
        await session.commit()
    real = regate.as_read.lock
    moved_by_hand: list[bool] = []

    async def the_read_is_done_then_the_owner_moves_it(session, model, *args, **kwargs):
        """The run has read and let go of its turn (a second writer inside its transaction would wait for a turn only
        it could give back): the owner's move is committed here, before the first slice locks its roles."""
        if model is Opportunity and not moved_by_hand:
            moved_by_hand.append(True)
            async with db.session_factory()() as hand:
                moved = (await hand.scalars(select(Opportunity).where(Opportunity.slug == "dragged"))).one()
                moved.status = stages.READY_TO_LAUNCH
                await trail.record(hand, OWNER, event="role_moved", sentence="You moved it.", actor="person",
                                   link=("opportunity", moved.id))
                await hand.commit()
        return await real(session, model, *args, **kwargs)

    monkeypatch.setattr(regate.as_read, "lock", the_read_is_done_then_the_owner_moves_it)
    async with db.session_factory()() as session:
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
    async with db.session_factory()() as session:
        roles = await by_slug(session)
        assert (roles["a"].status, roles["b"].status) == (stages.GATED_OUT, stages.GATED_OUT)
        assert roles["dragged"].status == stages.READY_TO_LAUNCH and roles["dragged"].screened_out_by is None
    assert report.roles_stopped == 2


async def test_two_regates_at_once_never_interleave_and_the_second_reads_the_settings_in_force_after_the_first(
        js, monkeypatch):
    """The second is another save: it waits for the first to end, and then reads the Hard pass in force, which the
    owner saved while the first was running."""
    await _a_log_to_tighten(60, 0)
    events: list[tuple[str, int | None]] = []
    real = regate._postings
    real_rules = gating.rules
    first_read_the_settings = asyncio.Event()

    async def postings(session, owner, rules, report):
        events.append(("starts", rules.hard_pass))
        await asyncio.sleep(0.3)  # long enough for the other one to start, were nothing to stop it
        await real(session, owner, rules, report)
        events.append(("ends", rules.hard_pass))

    async def noted(*args, **kwargs):
        read = await real_rules(*args, **kwargs)
        # Told only once the settings are read: `rules` reads them in several statements, and a save that landed
        # between them (as heavy load lets it) would be read by the first run, which is not what this test is about.
        first_read_the_settings.set()
        return read

    monkeypatch.setattr(regate, "_postings", postings)
    monkeypatch.setattr(gating, "rules", noted)

    async def first() -> None:
        async with db.session_factory()() as session:
            await regate.run(session, OWNER, cause="the first", today=TODAY)
            await session.commit()

    async def second() -> None:
        await first_read_the_settings.wait()
        async with db.session_factory()() as session:
            await profile_store.save(session, OWNER, "gates", gates(130_000), by="test")  # saved during the first
            await regate.run(session, OWNER, cause="the second", today=TODAY)
            await session.commit()

    await asyncio.gather(first(), second())
    assert events == [("starts", 1), ("ends", 1), ("starts", 2), ("ends", 2)], events
    posts, _ = await _stopped_in_the_log()
    assert posts == sum(1 for n in range(60) if 60_000 + (n % 50) * 2_000 < 130_000), "the second one's settings stand"
    assert _named_locks_held() == 0


async def test_a_regate_cut_short_is_finished_by_the_next_catch_up(js, monkeypatch):
    """A run that fails in its third slice leaves the first two committed, the lock let go, and stops under an older
    Hard pass that `catch_up` finds and finishes at the next start or night: the end is what a whole run gives."""
    monkeypatch.setattr(regate, "CHUNK", SLICE)
    await _a_log_to_tighten(100, 0)
    async with db.session_factory()() as session:
        await regate.run(session, OWNER, cause="under the first Hard pass", today=TODAY)
        await profile_store.save(session, OWNER, "gates", gates(300_000), by="test")
        await session.commit()
    real, calls = regate._bulk, []

    async def fails_in_the_third_slice(session, rows):
        calls.append(len(rows))
        if len(calls) == 3:
            raise RuntimeError("the database went away")
        return await real(session, rows)

    monkeypatch.setattr(regate, "_bulk", fails_in_the_third_slice)
    before, _ = await _stopped_in_the_log()
    async with db.session_factory()() as session:
        with pytest.raises(RuntimeError, match="went away"):
            await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.rollback()
        assert await regate.stale(session, OWNER) > 0, "stops under the first Hard pass are left"
    posts, _ = await _stopped_in_the_log()
    assert posts - before == 2 * SLICE, "the two slices before the third were committed, and no more"
    assert _named_locks_held() == 0, "a run that failed let its lock go"
    monkeypatch.setattr(regate, "_bulk", real)
    async with db.session_factory()() as session:
        report = await regate.catch_up(session, OWNER)
        await session.commit()
        assert report is not None and await regate.stale(session, OWNER) == 0
    assert await _stopped_in_the_log() == (100, 0), "every posting pays under 300,000"


async def test_a_regate_that_waits_for_another_holds_nothing_while_it_waits(js, monkeypatch):
    """The second is a Hard pass save: it has flushed its new version when it asks to re-gate, and the first run goes on
    for a minute. It waits with that version committed, no row locked and no connection held: waiting inside its
    transaction was a queue that freezes the app (`db.LOCK_TIMEOUT_MS`)."""
    await _a_log_to_tighten(10, 0)
    parked, go = asyncio.Event(), asyncio.Event()
    # At each wait of the second: is its session in a transaction, and the newest Hard pass another session sees.
    waits: list[tuple[bool, int]] = []
    second: list = []  # the second one's session, once it has one
    real_written, real_lock = regate._written, db.named_lock

    async def written(*args, **kwargs):
        """The first run has read the log and let go of the writer's turn, and holds only the re-gate's own lock."""
        if not parked.is_set():
            parked.set()
            await go.wait()  # the first run goes on until the second has come to wait
        return await real_written(*args, **kwargs)

    @contextlib.asynccontextmanager
    async def watched(key, **kwargs):
        if second:
            async with db.read_session_factory()() as other:  # a reader: it takes no turn
                saved = await profile_store.newest_version(other, OWNER, "gates")
            waits.append((second[0].in_transaction(), saved))
            go.set()
        async with real_lock(key, **kwargs):
            yield

    monkeypatch.setattr(regate, "_written", written)
    monkeypatch.setattr(db, "named_lock", watched)

    async def first() -> None:
        async with db.session_factory()() as session:
            await regate.run(session, OWNER, cause="the first", today=TODAY)
            await session.commit()

    running = asyncio.create_task(first())
    await asyncio.wait_for(parked.wait(), 30)
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(130_000), by="test")  # flushed, as a save leaves it
        second.append(session)
        await asyncio.wait_for(regate.run(session, OWNER, cause="the second", today=TODAY), 30)
        await session.commit()
    await running
    assert waits, "the second one waited"
    assert [held for held, _ in waits] == [False] * len(waits), "a transaction was open while it waited"
    assert {version for _, version in waits} == {2}, "its saved Hard pass was committed before it waited"


# --- a row that changes while the run works is left as the other change made it ---------


async def test_a_posting_that_changes_while_the_regate_works_is_left_as_that_change_made_it(js, monkeypatch):
    """Every posting is read and judged first; the slices are written afterwards, and each asks again for the state the
    read found (docs/PRD.md 39.14). Here the owner reads one anyway, or another check judges it, between the read and
    the write: a posting to be stopped, to be stated again, to be released and to be stamped, each beside one that did
    not change, which is written as the read planned."""
    stop = "Pay must be at least $120,000 a year, on the posted maximum."
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        for suffix in ("", "_too"):
            session.add_all([
                posting(fab, f"new{suffix}", 80_000, filtered_at=NOW, route="titles"),
                posting(fab, f"old{suffix}", 80_000, filtered_at=NOW, filtered_by="the place", judged_by="gate"),
                posting(fab, f"gone{suffix}", 200_000, filtered_at=NOW, filtered_by="the place", judged_by="gate"),
                posting(fab, f"kept{suffix}", 80_000, filtered_at=NOW, filtered_by=stop, judged_by="gate",
                        stop_gate="compensation", stop_gate_id="compensation")])
        await session.commit()
        await profile_store.save(session, OWNER, "gates", gates(120_000), by="test")
        await session.commit()
    real = regate._written
    changed: list[bool] = []

    async def the_log_changes_then_the_first_write(*args, **kwargs):
        """The read is done and its turn let go; the first write is next. The other change is committed in between."""
        if not changed:
            changed.append(True)
            async with db.session_factory()() as other:
                await other.execute(update(JobPosting).where(JobPosting.ext_id.in_(("new", "gone", "kept")))
                                    .values(owner_override=True))
                await other.execute(update(JobPosting).where(JobPosting.ext_id == "old").values(judged_by="titles"))
                await other.commit()
        return await real(*args, **kwargs)

    monkeypatch.setattr(regate, "_written", the_log_changes_then_the_first_write)
    async with db.session_factory()() as session:
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
    assert (report.postings_stopped, report.postings_restated, report.postings_released) == (1, 1, 1)
    async with db.session_factory()() as session:
        posts = await by_ext(session)
    assert posts["new"].filtered_by is None and posts["new"].stop_gate is None, "not stopped: the owner reads it anyway"
    assert posts["new_too"].filtered_by == stop and posts["new_too"].stop_hard_pass == 2
    assert (posts["old"].filtered_by, posts["old"].judged_by) == ("the place", "titles"), "not stated again"
    assert posts["old_too"].filtered_by == stop and posts["old_too"].judged_by == "gate"
    assert posts["gone"].filtered_by == "the place" and posts["gone"].route is None, "not released"
    assert posts["gone_too"].filtered_by is None and posts["gone_too"].route == "roles"
    assert (posts["kept"].stop_hard_pass, posts["kept"].judged_by_version) == (None, None), "not stamped"
    assert (posts["kept_too"].stop_hard_pass, posts["kept_too"].judged_by_version) == (2, 1)


async def _a_writer_holds_the_turn() -> tuple[asyncio.Future, asyncio.Event]:
    """Another writer, in a task of its own, that holds the turn until the event is set (a second writer in the
    caller's own task would be `db.NestedWriter`, not a wait)."""
    holds, let_go = asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with db.session_factory()() as other:
            await other.execute(select(Opportunity.id).limit(1))
            holds.set()
            await let_go.wait()
            await other.rollback()

    task = asyncio.ensure_future(holder())
    await holds.wait()
    return task, let_go


async def _three_roles_a_tighter_floor_stops() -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000))
        session.add_all([role(slug, stages.SECOND_IMPRESSION, 100_000) for slug in ("a", "b", "c")])
        await session.commit()
        await profile_store.save(session, OWNER, "gates", gates(150_000), by="test")
        await session.commit()


async def test_a_regate_waits_for_the_turn_of_another_writer_and_then_stops_every_role(js):
    """A slice never skips a role another transaction holds (the owner dragging it, a hand-back): there is no row held,
    the database is SQLite with one writer, so a writer that has the turn is waited for as a whole and the re-gate then
    checks every role, in its slices, once."""
    await _three_roles_a_tighter_floor_stops()
    holder, let_go = await _a_writer_holds_the_turn()

    async def the_regate() -> regate.Report:
        async with db.session_factory()() as session:
            report = await regate.run(session, OWNER, cause="a test", today=TODAY)
            await session.commit()
            return report

    running = asyncio.ensure_future(the_regate())
    await asyncio.sleep(0.1)
    assert not running.done(), "it waits for the turn"
    let_go.set()
    report = await asyncio.wait_for(running, 30)
    await holder
    async with db.session_factory()() as session:
        roles = await by_slug(session)
    assert [r.status for r in roles.values()] == [stages.GATED_OUT] * 3
    assert report.roles_stopped == 3 and report.roles == 3


async def test_a_regate_that_is_not_given_its_turn_in_time_is_told_busy_and_changes_nothing(js, monkeypatch):
    from app.errors import Busy

    await _three_roles_a_tighter_floor_stops()
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 100)
    holder, let_go = await _a_writer_holds_the_turn()

    async def the_regate() -> regate.Report:
        async with db.session_factory()() as session:
            return await regate.run(session, OWNER, cause="a test", today=TODAY)

    try:
        with pytest.raises(Busy):
            await asyncio.wait_for(asyncio.ensure_future(the_regate()), 10)
    finally:
        let_go.set()
        await holder
    async with db.session_factory()() as session:
        roles = await by_slug(session)
    assert [r.status for r in roles.values()] == [stages.SECOND_IMPRESSION] * 3


# --- locks: a re-gate and the passes never wait for each other's rows in a cycle -----------------------------------


async def _two_roles_one_the_gate_stops() -> dict[str, object]:
    """"low" (pays 100,000) is stopped by a floor of 150,000, "high" (pays 200,000) is not; both are on deck."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000))
        rows = {"low": role("low", stages.SECOND_IMPRESSION, 100_000),
                "high": role("high", stages.SECOND_IMPRESSION, 200_000)}
        session.add_all(rows.values())
        await session.flush()
        ids = {slug: row.id for slug, row in rows.items()}
        await session.commit()
        await profile_store.save(session, OWNER, "gates", gates(150_000), by="test")
        await session.commit()
    return ids


@pytest.mark.parametrize("how", ["dry", "one transaction"])
async def test_a_regate_and_a_pass_that_ask_for_the_roles_in_another_order_take_turns_and_both_finish(js, how):
    """A dry run, or a run inside a savepoint (the start's steps), is one transaction. The pass has the turn first and
    asks for the deck role and then the role the re-gate stopped; the re-gate is asked for meanwhile. What was a cycle
    of waits for rows that the database ended with an error is a queue for the turn now: the re-gate waits for the pass
    and neither is left waiting for the other."""
    ids = await _two_roles_one_the_gate_stops()
    holds_the_turn, order = asyncio.Event(), []

    async def the_regate() -> regate.Report:
        await holds_the_turn.wait()
        async with db.session_factory()() as session:
            if how == "dry":
                report = await regate.run(session, OWNER, cause="a test", today=TODAY, dry_run=True)
                await session.rollback()
            else:
                async with session.begin_nested():
                    report = await regate.run(session, OWNER, cause="a test", today=TODAY)
                await session.commit()
        order.append("the re-gate done")
        return report

    async def the_pass() -> None:
        async with db.session_factory()() as session:
            await session.execute(select(Opportunity.id).where(Opportunity.id == ids["high"]))
            holds_the_turn.set()
            await asyncio.sleep(0.1)  # the re-gate is asked for meanwhile, and waits
            await session.execute(select(Opportunity.id).where(Opportunity.id == ids["low"]))
            order.append("the pass done")
            await session.commit()

    got = await asyncio.wait_for(asyncio.gather(the_regate(), the_pass(), return_exceptions=True), 30)
    assert not [g for g in got if isinstance(g, BaseException)], got
    assert order == ["the pass done", "the re-gate done"] and got[0].roles_stopped == 1
    async with db.session_factory()() as session:
        roles = await by_slug(session)
    assert roles["high"].status == stages.SECOND_IMPRESSION
    assert roles["low"].status == (stages.SECOND_IMPRESSION if how == "dry" else stages.GATED_OUT)


async def test_a_dry_run_in_a_readers_session_takes_no_turn_and_sends_nothing_that_writes(js, monkeypatch):
    """The dry run only plans and reports. Given a reader's session (`db.ReadSession`) it takes no turn: a writer may
    have it at every point of the run and never waits (before: `FOR UPDATE NOWAIT` from another connection got every
    role and posting), and a statement that writes would raise (`db.WriteInReader`). The statements it sent hold no
    INSERT, UPDATE or DELETE."""
    await _a_log_to_tighten(30, 12)
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(300_000), by="test")
        await session.commit()
    real_limit = employer_limit.settle
    free = []

    async def the_turn_is_free() -> bool:
        async with asyncio.timeout(1), db.session_factory()() as other:
            await other.execute(select(Opportunity.id).limit(1))  # a writer's transaction: it has the turn at once
            await other.rollback()
        return True

    async def settle_then_look(*args, **kwargs):
        settled = await real_limit(*args, **kwargs)
        free.append(await the_turn_is_free())
        return settled

    monkeypatch.setattr(employer_limit, "settle", settle_then_look)
    sent: list[str] = []

    def noted(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        sent.append(statement)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        async with db.read_session_factory()() as session:
            report = await regate.run(session, OWNER, cause="a test", today=TODAY, dry_run=True)
            free.append(await the_turn_is_free())  # once the run is over, inside the caller's transaction
            await session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    assert free == [True, True]
    assert (report.postings_stopped, report.roles_stopped) == (30, 12)
    writing = [s for s in sent if re.search(r"\b(INSERT|UPDATE|DELETE)\b", s)]
    assert not writing, writing
    assert await _stopped_in_the_log() == (0, 0), "nothing was kept"


async def test_a_dry_run_reports_what_the_real_run_does_with_the_employer_limit_in_the_way(js):
    """The limit reads the roles as the re-gate leaves them: a role it stops is not on deck to be held, and a role it
    returns is on deck and held (two open applications at the employer). The dry run plans without a write, so the
    limit is given the roles the run changed (`employer_limit.settle(planned=...)`): the report is the real run's."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000))
        session.add_all([
            role("a", stages.APPLIED, 100_000), role("b", stages.ACTIVE_DISCUSSION, 100_000),
            role("stopped", stages.SECOND_IMPRESSION, 100_000), role("deck", stages.SECOND_IMPRESSION, 200_000),
            *[role(f"back{n}", stages.GATED_OUT, 200_000, screened_out_reason="somewhere else", screened_out_by="other")
              for n in range(2)]])
        await session.commit()
        await profile_store.save(session, OWNER, "gates", gates(150_000), by="test")
        await session.commit()
    async with db.session_factory()() as session:
        dry = await regate.run(session, OWNER, cause="a test", today=TODAY, dry_run=True)
        await session.rollback()
    async with db.session_factory()() as session:
        before = {r.slug: (r.status, r.screened_out_by) for r in (await by_slug(session)).values()}
        assert (before["stopped"], before["back0"]) == ((stages.SECOND_IMPRESSION, None), (stages.GATED_OUT, "other"))
        real = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
    assert dry.as_dict() == real.as_dict()
    assert (real.roles_stopped, real.roles_returned) == (4, 2), "one by the gate, three by the limit; two came back"
    async with db.session_factory()() as session:
        after = {r.slug: (r.status, r.screened_out_by) for r in (await by_slug(session)).values()}
    assert after["stopped"][0] == stages.GATED_OUT and after["stopped"][1] != "limit"
    assert [after[slug] for slug in ("deck", "back0", "back1")] == [(stages.GATED_OUT, "limit")] * 3


# --- no statement binds a list of ids that grows with the owner's roles ----------------------------------------------


async def test_the_packets_and_the_stops_of_more_roles_than_a_statement_may_carry_are_read_with_a_few_arguments(js):
    """Found 2026-11-06, before it failed: the re-gate asked for the newest packet of every role in Found, Needs
    Drafted, Ready to Apply and Gated out and for the stage each Gated out one was sent from,
    binding each id, and asyncpg refuses a statement of more than 32,767 arguments ("the number of query arguments
    cannot exceed 32767"): from the next roles on, every re-gate (a save of Hard pass, an employer gated, a site
    avoided, the command line) would fail. More ids than that are asked about, made up; the real ones are found as
    before, and no statement carries the ids."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000))
        ready, writing, stopped = (
            role(slug, stages.READY_TO_LAUNCH, 100_000) for slug in ("ready", "writing", "stopped"))
        session.add_all([ready, writing, stopped])
        await session.flush()
        stopped.status = stages.GATED_OUT  # a gate sent it from Ready to Apply (the stage move is written with it)
        session.add_all([role("came-in-closed", stages.GATED_OUT, 100_000)])  # no move from a stage: it came in closed
        for number, status in ((1, "held"), (2, "ready")):
            session.add(Packet(owner=OWNER, role_id=ready.id, version=number, status=status, brief={}, resume={},
                               letter={}, comp={}, reasoning={}, checks=[]))
        session.add(Packet(owner=OWNER, role_id=writing.id, version=1, status="writing", brief={}, resume={},
                           letter={}, comp={}, reasoning={}, checks=[]))
        await session.commit()
    asked = [uuid.uuid4() for _ in range(STATEMENT_ARGUMENT_LIMIT + 1)] + [ready.id, writing.id, stopped.id]
    async with db.session_factory()() as session:
        with bound_arguments() as bound:
            newest = await regate._latest_packets(session, asked)
            sent_from = await regate._left(session, asked)
    assert newest == {ready.id: "ready", writing.id: "writing"}, "the newest version of each role's packet"
    assert sent_from == {stopped.id: stages.READY_TO_LAUNCH}
    carrying = [n for n in bound if n]  # the turn's `BEGIN IMMEDIATE` is a statement with none
    assert len(carrying) == 2 and max(bound) <= FEW_ARGUMENTS


async def test_the_re_gate_reads_the_roles_with_a_few_arguments_whatever_their_number(js):
    """The same fault as a whole: past the count that the other readers ask for by id (`placement.OWNER_WIDE_AT`), no
    statement of the re-gate's read of its roles carries a list of them. The roles are those in the four stages, as the
    run reads them (a dry run: it plans only), and every one is checked."""
    many = placement.OWNER_WIDE_AT + 1
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000))
        session.add_all([role(f"r{n}", stages.GATED_OUT if n % 2 else stages.SECOND_IMPRESSION, 100_000)
                         for n in range(many)])
        await session.commit()
    async with db.session_factory()() as session:
        rules = await gating.rules(session, OWNER, today=TODAY)
        report = regate.Report()
        with bound_arguments() as bound, session.no_autoflush, regate._planning_only():
            await regate._roles(session, OWNER, rules, report, "a test")
        await session.rollback()
    assert report.roles == many
    assert bound and max(bound) <= FEW_ARGUMENTS
