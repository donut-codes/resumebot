"""The gates a reader judges (sentence_gates.py): the reader answers each sentence in Other with pass, fail or unknown
and a line from the posting; a fail screens the role out with that gate's sentence as the reason, a pass and an unknown
stop nothing, and the role page shows the reader's answer. Synthetic data only: the made-up profile's one sentence gate
is a travel limit."""

import pytest
from sqlalchemy import select

from app import db
from app.harness import instructions as harness_instructions
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Opportunity, SearchRun
from app.jobsearch.stages import GATED_OUT
from app.jobsearch.stages import LANDING_ZONE as KEPT
from tests.builders import filtered_postings, report_to_run
from tests.support import OWNER, PROFILE, load_profile

TRAVEL = "Travel of 20% or more."


def _read(title, *answers, **more):
    """A report of one posting: its answers to the asks, and `answers` to the sentence gates as
    `(id, answer, evidence)`."""
    role = {"company": "Fabrikam", "title": title, "url": f"https://jobs.example.test/{title.replace(' ', '-')}",
            "fit_evidence": "Biggest gap: none.",
            "fit_breakdown": {"asks": [{"id": "promotion", "answer": "met", "evidence": "A step up."}]}, **more}
    if answers:
        role["gate_answers"] = [{"id": i, "answer": a, "evidence": e} for i, a, e in answers]
    return role


async def _start(readers_n=1, **parts):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, **parts)
        await filtered_postings(session, readers_n)
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        return started.id


async def _finish(run_id, roles):
    await report_to_run(run_id, roles)
    await report_to_run(run_id, [], done=True)
    async with db.session_factory()() as session:
        return {o.title: o for o in (await session.scalars(select(Opportunity))).all()}


async def test_a_failed_sentence_gate_screens_the_role_out_with_that_gate_as_the_reason(js):
    run_id = await _start(3)
    stored = await _finish(run_id, [
        _read("Reader Role 1", ("travel", "fail", "The posting says travel is up to 40%.")),
        _read("Reader Role 2", ("travel", "pass", "The posting says no travel.")),
        _read("Reader Role 3", ("travel", "unknown", "")),
    ])
    out = stored["Reader Role 1"]
    assert (out.status, out.screened_out_reason, out.screened_out_by) == (GATED_OUT, TRAVEL, "rule")
    # The stop is data (docs/PRD.md 39.12): the reader's step, the sentence's id, and the versions it was judged under.
    assert (out.screened_out_step, out.screened_out_gate_id) == ("reader", "travel")
    assert (out.screened_out_hard_pass, out.screened_out_criteria) == (1, 1)
    # The answer is kept under the rule's id and the words it was given for (docs/PRD.md 39.13).
    assert out.gate_answers == [{"id": "travel", "answer": "fail", "evidence": "The posting says travel is up to 40%.",
                                 "says": TRAVEL}]
    assert out.fit_breakdown["answers"], "a screened-out role is kept whole, with its answers"
    assert (stored["Reader Role 2"].status, stored["Reader Role 2"].screened_out_reason) == (KEPT, None)
    assert (stored["Reader Role 3"].status, stored["Reader Role 3"].screened_out_reason) == (KEPT, None)
    assert stored["Reader Role 3"].gate_answers[0]["answer"] == "unknown"
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
    assert row.counts == {"reported": 3, "kept": 2, "screened_out": 1, "duplicates": 0}


async def test_a_gate_the_app_checks_stops_a_role_first_and_is_its_reason(js):
    run_id = await _start(1, gates={**PROFILE["gates"], "employer_names": ["Fabrikam"]})
    stored = await _finish(run_id, [_read("Reader Role 1", ("travel", "fail", "Heavy travel."))])
    out = stored["Reader Role 1"]
    assert (out.status, out.screened_out_by) == (GATED_OUT, "employer"), "the first gate to stop a role is its reason"
    assert out.gate_answers[0]["answer"] == "fail", "the reader's answer is kept all the same"


async def test_a_sentence_gate_in_a_part_the_owner_switched_off_stops_nothing(js):
    run_id = await _start(1, gates={**PROFILE["gates"], "sections_off": ["description"]})
    stored = await _finish(run_id, [_read("Reader Role 1", ("travel", "fail", "Heavy travel."))])
    assert stored["Reader Role 1"].status == KEPT


async def test_the_role_page_shows_the_readers_answer_and_line_not_a_fixed_unknown(client):
    run_id = await _start(3)
    stored = await _finish(run_id, [
        _read("Reader Role 1", ("travel", "fail", "The posting says travel is up to 40%.")),
        _read("Reader Role 2", ("travel", "unknown", "The posting does not mention travel.")),
        _read("Reader Role 3"),
    ])

    async def travel(title):
        body = (await client.get(f"/api/v1/roles/{stored[title].id}")).json()
        return {g["id"]: g for g in body["gates"]}["travel"]

    failed = await travel("Reader Role 1")
    assert failed["result"] == "fail" and failed["checked_by"] == "reader"
    assert failed["fact"] == "The posting says travel is up to 40%."
    unsure = await travel("Reader Role 2")
    assert unsure["result"] == "unknown" and unsure["fact"] == "The posting does not mention travel."
    unjudged = await travel("Reader Role 3")
    assert unjudged["result"] == "unknown" and unjudged["fact"] == "", "no answer: the page says data is needed"


# --- a malformed answer is refused the way a malformed ask's answer is ----------------------------------------------


async def _refused(run_id, role, message):
    with pytest.raises(run.RunError, match=message):
        await report_to_run(run_id, [role])
    async with db.session_factory()() as session:
        assert (await session.get(SearchRun, run_id)).reported == [], "a bad batch saves nothing"


async def test_a_gate_answer_that_is_not_pass_fail_or_unknown_is_refused_and_nothing_is_saved(js):
    run_id = await _start()
    await _refused(run_id, _read("Reader Role 1", ("travel", "yes", "Some travel.")), "is one of pass, fail, unknown")
    await _refused(run_id, _read("Reader Role 1", ("travel", "", "")), "is one of pass, fail, unknown")


async def test_gate_answers_that_are_not_a_list_name_a_gate_that_is_not_there_or_repeat_one_are_refused(js):
    run_id = await _start()
    await _refused(run_id, _read("Reader Role 1", gate_answers="travel: fail"), "gate_answers is a list")
    await _refused(run_id, _read("Reader Role 1", gate_answers={"travel": "fail"}), "gate_answers is a list")
    await _refused(run_id, _read("Reader Role 1", ("nope", "fail", "x")), "not one of the owner's sentence gates")
    await _refused(run_id, _read("Reader Role 1", ("compensation", "fail", "x")), "not one of the owner.s sentence")
    await _refused(run_id, _read("Reader Role 1", ("travel", "pass", "a"), ("travel", "fail", "b")),
                   "answers 'travel' twice")


async def test_a_gate_answer_that_is_not_an_object_or_names_no_gate_is_passed_over_as_an_ask_is(js):
    run_id = await _start()
    role = _read("Reader Role 1")
    role["gate_answers"] = ["travel", None, {"answer": "fail"}, {"id": "", "answer": "fail"},
                            {"id": " travel ", "answer": " FAIL ", "evidence": "  Heavy\n travel.  "}]
    stored = await _finish(run_id, [role])
    out = stored["Reader Role 1"]
    assert out.status == GATED_OUT and out.screened_out_reason == TRAVEL
    assert out.gate_answers == [{"id": "travel", "answer": "fail", "evidence": "Heavy travel.", "says": TRAVEL}], \
        "the stray entries are dropped; the answer is tidied"


async def test_a_long_line_from_the_reader_is_cut(js):
    run_id = await _start()
    stored = await _finish(run_id, [_read("Reader Role 1", ("travel", "pass", "x" * 1000))])
    assert len(stored["Reader Role 1"].gate_answers[0]["evidence"]) == 400


async def test_a_report_with_no_gate_answers_is_as_valid_as_before(js):
    run_id = await _start(4)
    await report_to_run(run_id, [_read(f"Reader Role {n}", gate_answers=empty) for n, empty in
                                 enumerate((None, "", [], {}), start=1)])
    async with db.session_factory()() as session:
        reported = (await session.get(SearchRun, run_id)).reported
    assert len(reported) == 4 and all("gate_answers" not in r for r in reported)


# --- the reader is told where a gate's verdict goes -----------------------------------------------------------------


def test_no_instruction_sends_an_agent_to_fit_evidence_for_a_gate():
    """`fit_evidence` is the one line of the biggest gap the owner reads, never a gate's verdict: an instruction line
    that names it and a gate must be pointing the gate at `gate_answers`."""
    for name in ("jobsearch.scoring", "jobsearch.analyst", "jobsearch.scout"):
        for line in harness_instructions.read("instructions", name).splitlines():
            if "fit_evidence" in line and "gate" in line.lower():
                assert "gate_answers" in line, f"{name}: {line}"
    assert "gate_answers" in harness_instructions.read("instructions", "jobsearch.scoring")
