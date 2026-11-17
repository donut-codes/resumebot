"""The packet writer (build_packet.bpmn, `packet_writer.py`, `seats.py`): a packet put together from the story library
by its seats, the code steps between them, the gate, the fix and the arbitrator, and filing it. Every seat's answer is
scripted; everything is made up."""

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from process_engine.models import ProcHistory, ProcInstance, ProcWait
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.loops.startup import run_startup_fixups
from app.jobsearch.models import Opportunity, Packet, SeatRun
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import session_lifecycle
from app.jobsearch.writing import library as story_library
from app.jobsearch.writing import packet_writer, packets, seats
from tests.builders import (
    FACTS_CLEAN,
    LIBRARY,
    compose_reply,
    library_built,
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


async def _packet(packet_id: str) -> Packet:
    async with db.session_factory()() as session:
        return await session.get(Packet, uuid.UUID(packet_id))


async def _path(packet_id: str) -> list[str]:
    async with db.session_factory()() as session:
        packet = await session.get(Packet, uuid.UUID(packet_id))
        rows = (await session.scalars(select(ProcHistory).where(ProcHistory.instance_id == packet.instance_id)
                                      .order_by(ProcHistory.seq))).all()
        return [r.to_element for r in rows if r.to_element]


async def test_a_clean_packet_goes_compose_once_over_gate_and_is_filed_ready_from_the_library(js):
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    done = await write_packet()
    assert done == ["Compose", "Facts_1"]
    packet = await _packet(packet_id)
    assert packet.status == "ready"
    bullets = packet.resume["roles"][0]["bullets"]
    # the library line is printed as approved, and cites the master line it rests on, not the library's id
    assert bullets == [{"text": "Led the risk working group across client engagements.", "from": ["b-risk"]}]
    assert packet.resume["summary_from"] == ["b-risk"] and packet.letter["from"] == ["b-risk"]
    assert packet.resume["roles"][0]["employer"] == "Northwind Labs"  # code prints what the master holds
    assert packet.comp["ask"] > 0  # worked out from the role's research
    assert packet.reasoning["problem"] == "They need programs that run."
    assert any(c["check"] == "The facts gate" and c["ok"] and not c["blocking"] for c in packet.checks)
    assert packet.written_at is not None
    path = await _path(packet_id)
    assert path.index("Compose") < path.index("Check_1") < path.index("Facts_1")
    assert "Fix" not in path and "Mark_ready" in path
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        assert role.packet_complete is True


async def test_a_fractional_hourly_roles_packet_carries_no_yearly_ask(js):
    role_id = await packet_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.title, role.comp = f"Fractional {role.title}", "$190-$630 an hour"
        await session.commit()
    packet_id = await _start(role_id)
    await write_packet()
    packet = await _packet(packet_id)
    assert "ask" not in packet.comp and "range" not in packet.comp


async def test_every_seat_is_one_turn_with_its_model_and_the_cost_is_kept(js):
    role_id = await packet_setup()
    await _start(role_id)
    async with db.session_factory()() as session:
        taken = (await seats.take(session, OWNER, "s-1"))["seat"]
        await session.commit()
    assert taken["model"] == "opus" and taken["prompt"] == seats.PROMPT
    # the seat's text is its instructions, its reply shape and its inputs: the library, never the whole master
    assert "# You compose a job application packet" in taken["system"]
    assert "## Your reply" in taken["system"] and "THE RESUME LIBRARY" in taken["system"]
    assert "L001" in taken["system"] and "Led the risk working group" in taken["system"]
    async with db.session_factory()() as session:
        # a second session is handed nothing while the first holds the seat
        assert (await seats.take(session, OWNER, "s-2"))["seat"] is None
        await seats.answer(session, OWNER, taken["id"], {"text": json.dumps(compose_reply()),
                                                         "spend": {"cost_usd": 0.42, "input_tokens": 9,
                                                                   "output_tokens": 3}})
        await session.commit()
    async with db.session_factory()() as session:
        run = await session.get(SeatRun, uuid.UUID(taken["id"]))
        assert run.ok is True and run.cost_usd == 0.42 and run.element == "Compose"
        nxt = (await seats.take(session, OWNER, "s-3"))["seat"]
        assert nxt["model"] == "sonnet" and "# You are the facts gate" in nxt["system"]


async def test_assembly_resolves_ids_drops_a_second_line_of_the_same_claim_and_names_what_does_not_fit():
    data = copy.deepcopy(LIBRARY)
    data["lines"].append({"id": "L009", "kind": "bullet", "role": "northwind-lead", "entry": "b-risk", "work": "risk",
                          "side": "client", "text": "Ran risk reviews for every client engagement", "from": ["b-risk"]})
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    docs = Documents.model_validate({"resume": RESUME, "about": ABOUT})
    problems: list[str] = []
    reply = compose_reply(roles=[{"role": "northwind-lead", "intro": "S-northwind-lead",
                                  "lines": ["L001", "L009", "L404"]}],
                          competencies=[{"area": "X", "items": [{"term": "Knowledge", "backed_by": "L002"}]}])
    draft = packet_writer.assemble(reply, data, docs, problems)
    role = draft["resume"]["roles"][0]
    assert [b["lib"] for b in role["bullets"]] == ["L001"]
    assert role["intro"].startswith("Ran the practice") and role["intro_from"] == ["b-risk"]
    assert any("a second line of one claim" in p for p in problems)
    assert any("'L404'" in p for p in problems)


async def test_an_achievement_that_tells_a_printed_lines_claim_is_dropped():
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    data = copy.deepcopy(LIBRARY)
    # written for its own entry, but it rests on the entry L001 was written for: the same accomplishment
    data["lines"].append({"id": "L010", "kind": "achievement", "role": None, "entry": "a-other", "claim": "risk-led",
                          "text": "Led the risk working group for the practice", "from": ["b-risk"]})
    docs = Documents.model_validate({"resume": RESUME, "about": ABOUT})
    problems: list[str] = []
    draft = packet_writer.assemble(compose_reply(achievements=["L003", "L010"]), data, docs, problems)
    assert [a["lib"] for a in draft["resume"]["achievements"]] == ["L003"]
    assert any("L010" in p and "claim" in p for p in problems)


async def test_a_scope_line_the_rules_always_print_falls_back_to_the_masters_own_words_when_the_library_holds_none():
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    resume = copy.deepcopy(RESUME)
    resume["roles"][0]["intro"] = "Ran the practice's client risk work."
    data = copy.deepcopy(LIBRARY)
    data["lines"] = [ln for ln in data["lines"] if ln["kind"] != "scope"]  # held out when the library was built
    data["rules"]["roles"] = {"northwind-lead": {"min": None, "max": None, "firm_first": False, "scope": True}}
    docs = Documents.model_validate({"resume": resume, "about": ABOUT})
    draft = packet_writer.assemble(compose_reply(), data, docs, [])
    role = draft["resume"]["roles"][0]
    assert role["intro"] == "Ran the practice's client risk work." and role["intro_from"] == ["northwind-lead"]
    # the employer prints just above it: the master's words lose its name
    assert packet_writer.without_employer("Sole leader of Northwind's risk practice, reporting to its COO.",
                                          "Northwind Labs") == "Sole leader of the risk practice, reporting to its COO."
    assert packet_writer.without_employer("Northwind Labs: ran the risk practice", "Northwind Labs") == (
        "Ran the risk practice")
    assert packet_writer.without_employer("At Northwind, ran the risk practice for clients", "Northwind Labs") == (
        "Ran the risk practice for clients")
    assert packet_writer.without_employer("Ran the risk practice at Northwind", "Northwind Labs") == (
        "Ran the risk practice")
    hard, _flags = packet_writer.code_findings(draft, data, docs, None)
    assert not any("prints no scope line" in h for h in hard)


async def test_the_owners_rule_puts_a_roles_firm_side_lines_first_and_its_scope_line_always_prints():
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    data = copy.deepcopy(LIBRARY)
    data["rules"]["roles"] = {"northwind-lead": {"min": 1, "max": 3, "firm_first": True, "scope": True}}
    docs = Documents.model_validate({"resume": RESUME, "about": ABOUT})
    draft = packet_writer.assemble(compose_reply(roles=[{"role": "northwind-lead", "intro": "",
                                                         "lines": ["L001", "L002"]}]), data, docs, [])
    role = draft["resume"]["roles"][0]
    assert [b["lib"] for b in role["bullets"]] == ["L002", "L001"]  # firm before client
    assert role["intro"]  # the rule prints it though the composer chose none


async def test_a_name_the_client_rule_keeps_out_and_a_count_outside_the_owners_fails_the_code_checks():
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    data = copy.deepcopy(LIBRARY)
    data["rules"] = {"protected": ["Initech"], "approved": [],
                     "roles": {"northwind-lead": {"min": 2, "max": 3, "firm_first": False, "scope": False}}}
    docs = Documents.model_validate({"resume": RESUME, "about": ABOUT})
    draft = packet_writer.assemble(compose_reply(summary="Built programs for Initech.", summary_from=["L001"]),
                                   data, docs, [])
    hard, _flags = packet_writer.code_findings(draft, data, docs, None)
    assert any("'Initech'" in h and "client rule" in h for h in hard)
    assert any("has 1 bullets" in h for h in hard)


async def test_an_uncited_letter_paragraph_and_a_universal_word_are_flags_for_the_facts_read_not_refusals():
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    docs = Documents.model_validate({"resume": RESUME, "about": ABOUT})
    letter = {"paragraphs": [{"text": "Dear team,", "from": []},
                             {"text": "Every client I served came back for more work, and the board relied on it.",
                              "from": ["L001"]},
                             {"text": "I would bring the same calm to your programs and your board meetings.",
                              "from": []}]}
    draft = packet_writer.assemble(compose_reply(letter=letter), LIBRARY, docs, [])
    hard, flags = packet_writer.code_findings(draft, LIBRARY, docs, None)
    assert not any("cites no master line" in h for h in hard)
    assert any("paragraph 2 says 'every'" in f for f in flags)
    assert any("paragraph 3" in f and "cites no master line" in f for f in flags)


async def test_a_failed_gate_gets_one_fix_and_a_check_still_failing_goes_to_the_arbitrator(js):
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    failing = {"claims": [], "failures": [{"doc": "resume", "check": "trace", "line": "Builds programs",
                                           "reason": "says more than the master"}], "dismissed": []}
    fixed = {"resume": {"summary": "Builds programs that people use."},
             "letter": {"paragraphs": [{"text": "Dear hiring team,", "from": []},
                                       {"text": "I would like to help you run programs.", "from": ["b-risk"]}]}}
    ruling = {"rulings": [{"id": "F1", "real": True, "status": "open", "why": "wider", "fix": "narrow it"},
                          {"id": "F2", "real": False, "status": "cleared", "why": "rewording"}],
              "fix_list": [{"doc": "resume", "line": "Builds programs", "change": "Builds programs."}]}
    done = await write_packet(Facts_1=failing, Fix=fixed, Facts_2=failing, Arbitrate=ruling, Final_fix=fixed)
    assert done == ["Compose", "Facts_1", "Fix", "Facts_2", "Arbitrate", "Final_fix"]
    packet = await _packet(packet_id)
    assert packet.status == "ready" and packet.resume["summary"] == "Builds programs that people use."
    gate = next(c for c in packet.checks if c["check"] == "The facts gate")
    assert "read 2 times" in gate["detail"] and "ruled 1 of 2 findings real" in gate["detail"]
    assert len(packet.brief["gates"]) == 2 and packet.brief["arbitrator"]["fix_list"]


async def test_a_fix_that_clears_the_gate_never_reaches_the_arbitrator(js):
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    failing = {"claims": [], "failures": [{"doc": "letter", "check": "trace", "line": "I would", "reason": "x"}]}
    fixed = {"letter": {"paragraphs": [{"text": "Dear hiring team,", "from": []},
                                       {"text": "I would like to help you run programs.", "from": ["b-risk"]}]}}
    done = await write_packet(Facts_1=failing, Fix=fixed, Facts_2=FACTS_CLEAN)
    assert done == ["Compose", "Facts_1", "Fix", "Facts_2"]
    assert (await _packet(packet_id)).status == "ready"


async def test_a_final_packet_with_a_failed_check_is_held_for_the_owner(js):
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    banned = compose_reply(headline="Spearheaded programs")
    failing = {"claims": [], "failures": []}
    ruling = {"rulings": [{"id": "F1", "real": True, "status": "open"}], "fix_list": []}
    done = await write_packet(Compose=banned, Facts_1=failing, Fix={"resume": {}},
                              Facts_2=failing, Arbitrate=ruling)
    assert done[-1] == "Arbitrate"  # no fixes: filed as it is
    packet = await _packet(packet_id)
    assert packet.status == "held"
    assert any(c["check"] == "Nothing you ban appears" and not c["ok"] for c in packet.checks)


async def test_a_reply_that_is_not_json_is_a_failed_try_and_three_close_the_packet_as_failed(js):
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    done = await run_seats({"Compose": "I could not do this."})
    assert done == ["Compose"] * seats.MAX_TRIES
    packet = await _packet(packet_id)
    assert packet.status == "failed"
    async with db.session_factory()() as session:
        runs = (await session.scalars(select(SeatRun).where(SeatRun.element == "Compose"))).all()
        assert len(runs) == seats.MAX_TRIES and all(r.ok is False for r in runs)


async def test_a_session_that_ends_without_answering_frees_its_seat_at_once(js):
    role_id = await packet_setup()
    await _start(role_id)
    async with db.session_factory()() as session:
        taken = (await seats.take(session, OWNER, "s-dead"))["seat"]
        await session.commit()
    async with db.session_factory()() as session:
        assert await seats.release(session, OWNER, "s-dead") == 1
        await session.commit()
    async with db.session_factory()() as session:
        again = (await seats.take(session, OWNER, "s-next"))["seat"]
        assert again is not None and again["id"] != taken["id"]


async def test_compose_waits_while_the_library_is_being_built_for_a_newer_master(js):
    role_id = await packet_setup()
    async with db.session_factory()() as session:
        docs = await profile_store.owner_documents(session, OWNER)
        resume = docs.resume.model_dump(mode="json")
        resume["roles"][0]["bullets"][1]["text"] = "Built and ran the practice knowledge base"
        await profile_store.save(session, OWNER, "resume", resume, by="test")  # starts a build
        await session.commit()
    await _start(role_id)
    async with db.session_factory()() as session:
        o = await _next_seat(session)
        assert o is not None and o.instance.process_key == "jobsearch_library_part"  # a library part, not compose
        assert await story_library.usable(session, OWNER) is None
        await library_built(session)
        await session.commit()
    async with db.session_factory()() as session:
        o = await _next_seat(session)
        assert o is not None and o.wait.element_id == "Compose"


async def _next_seat(session):
    """The seat the next session would be handed (its wait and its process), or None."""
    ids = await seats.free(session, OWNER)
    if not ids:
        return None
    wait = await session.get(ProcWait, ids[0])
    return SimpleNamespace(wait=wait, instance=await session.get(ProcInstance, wait.instance_id))


async def test_the_page_limit_is_read_from_the_owners_rules_and_code_cuts_to_it():
    from app.jobsearch.profile.documents import Documents

    docs = Documents.model_validate({"writing": {"resume_global": [
        {"id": "len", "says": "The resume must be at most two pages."}]}})
    assert packet_writer.resume_page_limit(docs) == 2
    assert packet_writer.resume_page_limit(Documents()) is None


async def test_progress_follows_the_step_the_packet_is_at(js):
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    assert packets.progress(await _packet(packet_id))["step"] == "asked"
    await run_seats({"Compose": compose_reply()}, most=1)
    assert packets.progress(await _packet(packet_id))["label"] == "Its facts being checked"


async def test_code_keeps_each_role_within_the_owners_most_and_prints_only_terms_a_printed_line_carries():
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    data = copy.deepcopy(LIBRARY)
    data["rules"]["roles"] = {"northwind-lead": {"min": None, "max": 1, "firm_first": False, "scope": False}}
    docs = Documents.model_validate({"resume": RESUME, "about": ABOUT})
    reply = compose_reply(roles=[{"role": "northwind-lead", "intro": "", "lines": ["L001", "L002"]}],
                          competencies=[{"area": "Work", "items": [
                              {"term": "Risk Working Groups", "backed_by": "L001"},
                              {"term": "Business Development", "backed_by": "L001"},
                              {"term": "Risk Opportunities", "backed_by": "L001"},
                              {"term": "Knowledge Base", "backed_by": "L002"}]}])
    draft = packet_writer.assemble(reply, data, docs, [])
    cut = packet_writer.settle(draft, data, docs)
    role = draft["resume"]["roles"][0]
    assert [b["lib"] for b in role["bullets"]] == ["L001"]  # the owner's most is one: the last goes
    terms = [x["term"] for c in draft["resume"]["competencies"] for x in c["items"]]
    # "Business Development" names a printed line that does not carry it, "Risk Opportunities" only half of it; L002
    # is no longer printed
    assert terms == ["Risk Working Groups"]
    assert any("Business Development" in c for c in cut) and any("Knowledge Base" in c for c in cut)


async def test_competencies_print_only_the_skills_the_library_lists_for_printed_lines():
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    data = copy.deepcopy(LIBRARY)
    for ln in data["lines"]:
        if ln["id"] == "L001":
            ln["skills"] = ["Risk Management", "Governance & Compliance"]
        if ln["id"] == "L002":
            ln["skills"] = ["Knowledge Management"]
    docs = Documents.model_validate({"resume": RESUME, "about": ABOUT})
    reply = compose_reply(competencies=[{"area": "Work", "items": [
        {"term": "risk management", "backed_by": "L001"},
        {"term": "Governance and Compliance", "backed_by": "L001"},
        {"term": "Risk Working Group", "backed_by": "L001"},  # the line carries the words, but it is no listed skill
        {"term": "Knowledge Management", "backed_by": "L002"}]}])  # L002 does not print
    draft = packet_writer.assemble(reply, data, docs, [])
    cut = packet_writer.settle(draft, data, docs)
    terms = [x["term"] for c in draft["resume"]["competencies"] for x in c["items"]]
    assert terms == ["risk management", "Governance and Compliance"]
    assert any("Risk Working Group" in c for c in cut) and any("Knowledge Management" in c for c in cut)


def test_the_composer_reads_each_lines_claim_and_skills():
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    data = copy.deepcopy(LIBRARY)
    data["lines"].append({"id": "L009", "kind": "bullet", "role": "northwind-lead", "entry": "b-risk", "claim": "x",
                          "skills": ["Risk Management"], "text": "Ran risk reviews for client engagements",
                          "from": ["b-risk"]})
    view = packet_writer.library_for_compose(data, Documents.model_validate({"resume": RESUME, "about": ABOUT}))
    lines = {ln["id"]: ln for ln in view["roles"][0]["lines"]}
    assert lines["L001"]["claim"] == lines["L009"]["claim"] == "risk"  # one entry: one claim, whatever the label
    assert lines["L009"]["skills"] == ["Risk Management"] and lines["L002"]["skills"] == []


def test_a_figure_the_master_gives_as_an_estimate_keeps_its_hedge():
    from app.jobsearch.profile.documents import Documents
    from tests.builders import ABOUT, RESUME

    resume = copy.deepcopy(RESUME)
    resume["roles"][0]["bullets"][1]["text"] = ("Built the practice knowledge base; by my estimate it saved two hours "
                                                "a week")
    docs = Documents.model_validate({"resume": resume, "about": ABOUT})
    assert packet_writer.dropped_hedges("x", "Built the knowledge base, saving two hours a week", ["b-kb"], docs)
    assert not packet_writer.dropped_hedges("x", "Built the knowledge base, saving an estimated two hours a week",
                                            ["b-kb"], docs)
    assert not packet_writer.dropped_hedges("x", "Built the practice knowledge base", ["b-kb"], docs)


def test_a_reply_that_corrects_itself_is_read_from_its_last_object():
    text = ('```json\n{"entries": [1]}\n```\n\nWait, that cited an invented id. Corrected full reply below.\n\n'
            '```json\n{"entries": [1, 2, 3]}\n```')
    assert seats.parse_reply(text) == {"entries": [1, 2, 3]}
    # a last block that does not read leaves the one that does
    assert seats.parse_reply('```json\n{"a": 1}\n```\nand then ```json\n{"a": \n```') == {"a": 1}
    assert seats.parse_reply('Here it is: {"a": 2}') == {"a": 2}



# --- a stop of the app (docs/PRD.md 43.6): it ends the lanes' sessions with no word of the end ---------------------

LANE = "session-5"
T0 = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)


async def _heard(state: str, sid: str | None = None, *, runner: str = LANE, owner: str = OWNER) -> None:
    """A heartbeat of a runner, as the app notes it."""
    async with db.session_factory()() as session:
        await roster.note_runner(session, owner, {"runner": runner, "state": state, "session_id": sid})
        await session.commit()


async def _take(sid: str, now: datetime = T0) -> dict | None:
    async with db.session_factory()() as session:
        taken = (await seats.take(session, OWNER, sid, now))["seat"]
        await session.commit()
        return taken


async def _the_app_starts() -> int:
    async with db.session_factory()() as session:
        noted = await seats.note_restart(session, OWNER)
        await session.commit()
        return noted


async def _run(run_id: str) -> SeatRun:
    async with db.session_factory()() as session:
        return await session.get(SeatRun, uuid.UUID(run_id))


async def test_a_seat_whose_session_a_stop_ended_is_free_when_its_runner_is_heard_again_and_no_try_is_counted(js):
    role_id = await packet_setup()
    await _start(role_id)
    await _heard("started", "s-dead")
    taken = await _take("s-dead")
    # the app and the lanes are stopped (no word of the session's end) and started; the lane says it is up
    assert await _the_app_starts() == 1
    await _heard("idle")
    async with db.session_factory()() as session:
        assert len(await seats.free(session, OWNER, T0)) == 1
        assert await seats.running(session, OWNER, T0) == []
    again = await _take("s-next", T0 + timedelta(minutes=1))
    assert again is not None and again["id"] != taken["id"]
    cut = await _run(taken["id"])
    assert (cut.ended_at, cut.ok, cut.error) == (T0 + timedelta(minutes=1), None, seats.STOPPED)


async def test_the_apps_own_start_notes_the_open_seat_runs(js, caplog):
    """The step is in the start's own list (`startup.run_startup_fixups`): without it nothing notes a run, and the seat
    waits out its lease."""
    role_id = await packet_setup()
    await _start(role_id)
    await _heard("started", "s-dead")
    taken = await _take("s-dead")
    async with db.session_factory()() as session:
        await run_startup_fixups(session, OWNER)
        await session.commit()
    assert "seat runs a stop left open failed" not in caplog.text
    await _heard("idle")
    again = await _take("s-next", T0 + timedelta(minutes=1))
    assert again is not None and again["id"] != taken["id"]
    assert (await _run(taken["id"])).error == seats.STOPPED


async def test_a_session_that_lived_through_a_restart_of_the_app_alone_keeps_its_seat_and_answers_it(js):
    role_id = await packet_setup()
    await _start(role_id)
    await _heard("started", "s-live")
    taken = await _take("s-live")
    assert await _the_app_starts() == 1
    await _heard("alive", "s-live")  # the lanes were never stopped: its runner still names it
    assert await _take("s-next", T0 + timedelta(minutes=1)) is None
    async with db.session_factory()() as session:
        reply = await seats.answer(session, OWNER, taken["id"], {"text": json.dumps(compose_reply())})
        await session.commit()
    assert reply == {"ok": True, "taken": True}


async def test_three_stops_in_a_row_at_one_seat_do_not_close_the_packet(js):
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    for n in range(seats.MAX_TRIES):
        await _heard("started", f"s-{n}")
        assert await _take(f"s-{n}", T0 + timedelta(minutes=n)) is not None
        assert await _the_app_starts() == 1
        await _heard("idle")
    assert await _take("s-next", T0 + timedelta(minutes=seats.MAX_TRIES)) is not None
    assert (await _packet(packet_id)).status != "failed"
    async with db.session_factory()() as session:
        runs = (await session.scalars(select(SeatRun).where(SeatRun.element == "Compose"))).all()
    assert len(runs) == seats.MAX_TRIES + 1 and not any(r.ok is False for r in runs)


async def test_a_run_a_stop_left_open_past_its_lease_is_closed_as_cut_short_not_as_a_failed_try(js):
    role_id = await packet_setup()
    await _start(role_id)
    await _heard("started", "s-dead")
    taken = await _take("s-dead")
    assert await _the_app_starts() == 1
    await _heard("idle")  # its lane is up again, and no session takes a seat until the lease is out
    later = T0 + seats.LEASE + timedelta(minutes=1)
    assert await _take("s-next", later) is not None
    cut = await _run(taken["id"])
    assert (cut.ended_at, cut.ok, cut.error) == (later, None, seats.STOPPED)


async def test_a_noted_run_whose_runner_still_names_its_session_past_its_lease_is_a_failed_try_and_counts(js):
    """The session lived through a restart of the app alone and then gave no answer: the stop did not end it, so the
    lease does, as a failed try like any other."""
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    step = seats.LEASE + timedelta(minutes=1)
    for n in range(seats.MAX_TRIES):
        await _heard("started", f"s-{n}", runner=f"session-{n}")
        assert await _take(f"s-{n}", T0 + n * step) is not None
        assert await _the_app_starts() == 1
        # the lanes were never stopped: its runner still names it, now and when the lease is out
        await _heard("alive", f"s-{n}", runner=f"session-{n}")
    assert await _take("s-next", T0 + seats.MAX_TRIES * step) is None
    async with db.session_factory()() as session:
        runs = (await session.scalars(select(SeatRun).where(SeatRun.element == "Compose"))).all()
    assert [(r.ok, r.error) for r in runs] == [(False, "no answer within the lease")] * seats.MAX_TRIES
    assert (await _packet(packet_id)).status == "failed"


async def test_the_start_leaves_a_run_that_ended_and_another_owners_runs_as_they_are(js):
    role_id = await packet_setup()
    await _start(role_id)
    await _heard("started", "s-ended")
    taken = await _take("s-ended")
    async with db.session_factory()() as session:
        assert await seats.release(session, OWNER, "s-ended", T0) == 1
        mine = await session.get(SeatRun, uuid.UUID(taken["id"]))
        theirs = SeatRun(owner="someone-else", wait_id=uuid.uuid4(), instance_id=uuid.uuid4(),
                         process_key=mine.process_key, element=mine.element, model=mine.model, session_id="s-theirs",
                         started_at=T0)
        session.add(theirs)
        await session.commit()
        theirs_id = str(theirs.id)
    await _heard("started", "s-theirs", owner="someone-else")
    assert await _the_app_starts() == 0
    await _heard("idle")
    await _heard("idle", owner="someone-else")
    assert await _take("s-next", T0 + timedelta(minutes=1)) is not None
    ended = await _run(taken["id"])
    assert (ended.ended_at, ended.ok, ended.error) == (T0, False, "the session ended without an answer")
    assert (await _run(theirs_id)).ended_at is None


async def _says(state: str, sid: str | None = None) -> None:
    """A heartbeat of the lane's runner, through the app's own door for it."""
    async with db.session_factory()() as session:
        await session_lifecycle.settle(session, OWNER, {"runner": LANE, "lane": "writer", "state": state,
                                                    "session_id": sid})
        await session.commit()


async def _the_app_starts_anew() -> int:
    """A start of the app in a new process: what the start before it noted is gone with that process."""
    seats._CUT.clear()
    return await _the_app_starts()


async def test_a_seat_a_stop_freed_stays_free_when_the_app_starts_again_before_any_seat_is_taken(js):
    role_id = await packet_setup()
    await _start(role_id)
    await _heard("started", "s-dead")
    taken = await _take("s-dead")
    assert await _the_app_starts_anew() == 1
    await _says("idle")  # the lane is up again: the run is closed as cut short there and then, not at the next take
    cut = await _run(taken["id"])
    assert cut.ended_at is not None and (cut.ok, cut.error) == (None, seats.STOPPED)
    assert await _the_app_starts_anew() == 0  # the app alone starts again, and no session has taken a seat since
    async with db.session_factory()() as session:
        assert len(await seats.free(session, OWNER, T0 + timedelta(minutes=2))) == 1
        assert await seats.running(session, OWNER, T0 + timedelta(minutes=2)) == []
    assert await _take("s-next", T0 + seats.LEASE + timedelta(minutes=1)) is not None
    assert (await _run(taken["id"])).error == seats.STOPPED


async def test_three_stops_each_followed_by_a_second_start_with_no_seat_taken_do_not_close_the_packet(js):
    role_id = await packet_setup()
    packet_id = await _start(role_id)
    step = seats.LEASE + timedelta(minutes=1)
    for n in range(seats.MAX_TRIES):
        await _heard("started", f"s-{n}")
        assert await _take(f"s-{n}", T0 + n * step) is not None
        assert await _the_app_starts_anew() == 1
        await _says("idle")
        assert await _the_app_starts_anew() == 0
    assert await _take("s-next", T0 + seats.MAX_TRIES * step) is not None
    assert (await _packet(packet_id)).status != "failed"
    async with db.session_factory()() as session:
        runs = (await session.scalars(select(SeatRun).where(SeatRun.element == "Compose"))).all()
    assert len(runs) == seats.MAX_TRIES + 1 and not any(r.ok is False for r in runs)


async def test_a_run_a_stop_cut_is_closed_when_its_runner_is_heard_in_another_session(js):
    role_id = await packet_setup()
    await _start(role_id)
    await _heard("started", "s-dead")
    taken = await _take("s-dead")
    assert await _the_app_starts_anew() == 1
    await _says("started", "writer-next")
    cut = await _run(taken["id"])
    assert cut.ended_at is not None and (cut.ok, cut.error) == (None, seats.STOPPED)


async def test_a_session_that_lived_through_a_restart_and_ends_without_an_answer_is_still_a_failed_try(js):
    role_id = await packet_setup()
    await _start(role_id)
    await _heard("started", "writer-live")
    taken = await _take("writer-live")
    assert await _the_app_starts_anew() == 1
    await _says("alive", "writer-live")
    assert (await _run(taken["id"])).ended_at is None
    await _says("ended", "writer-live")
    ended = await _run(taken["id"])
    assert (ended.ok, ended.error) == (False, "the session ended without an answer")


def test_the_fix_seats_answer_on_sonnet_and_the_composer_and_arbitrator_stay_on_opus():
    pw = packet_writer
    elements = (pw.COMPOSE, pw.FIX, pw.FINAL_FIX, pw.ARBITRATE)
    models = {e: seats.SEATS[(pw.process.PACKET, e)].model for e in elements}
    assert models == {pw.COMPOSE: "opus", pw.FIX: "sonnet", pw.FINAL_FIX: "sonnet", pw.ARBITRATE: "opus"}
