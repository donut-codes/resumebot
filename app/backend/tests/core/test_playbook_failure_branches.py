"""The failed ends, the skipped red-team read and the mail reader's 12-hour wait that the playbooks draw (PRD paths
10 and 43 packet, 14 mail, 15.2 prep, 36 employer, 38 gaps). The failed end is a drawing for the reader of the file:
the app closes a run as failed from outside the engine (the run ends cancelled), so the engine never takes it. These
tests check the drawing is whole, and that the code stays as the drawing says it is (nothing ticks the engine, and only
two places complete a step with `submitted` other than true), so the two cannot drift apart unnoticed."""

import re

import pytest
from process_engine.model import AGENT, BOUNDARY, END, GATEWAY, parse_duration

from app import db
from app.jobsearch import process
from app.jobsearch.mail import sync as mail_sync

APP = db.BACKEND_DIR / "app"

#: (process, agent step, its output, where the step went before the failed way was drawn)
AGENT_STEPS = [
    (process.PACKET, "Compose", "composed", "Check_1"),
    (process.PACKET, "Facts_1", "gate1", "Gw_gate1"),
    (process.PACKET, "Fix", "fixed", "Check_2"),
    (process.PACKET, "Facts_2", "gate2", "Gw_gate2"),
    (process.PACKET, "Arbitrate", "ruling", "Gw_fixes"),
    (process.PACKET, "Final_fix", "final", "File"),
    (process.LIBRARY, "Check_1", "checked", "Gw_failing"),
    (process.LIBRARY, "Fix", "fixed", "Code_2"),
    (process.LIBRARY, "Check_2", "rechecked", "Save_library"),
    (process.FIND_GAPS, "Analyst_finds", "found", "Settle_run"),
    (process.RESEARCH_EMPLOYER, "Researcher_researches", "found", "Take_facts"),
    (process.PREPARE_INTERVIEW, "Coach_writes", "found", "Check_report"),
]


@pytest.mark.parametrize(("key", "step", "output", "went_to"), AGENT_STEPS)
def test_every_agent_step_of_the_five_playbooks_has_a_failed_way_to_the_failed_end(key, step, output, went_to):
    defn = process.build_library().get(key)
    assert defn.elements[step].kind == AGENT
    (out,) = defn.out_flows(step)
    gateway = defn.elements[out.target]
    assert gateway.kind == GATEWAY
    ways = defn.out_flows(gateway.id)
    (default,) = [f for f in ways if f.is_default]
    (failed,) = [f for f in ways if not f.is_default]
    assert default.target == went_to, "the way it answered goes where the step went before"
    assert failed.condition == f"{output}.submitted = false"
    assert defn.elements[failed.target].name == "Closed as failed"
    assert defn.elements[failed.target].kind == END
    assert f"{output}.submitted" in defn.data_model.fields


def test_the_interview_prep_files_without_a_skipped_read():
    defn = process.build_library().get(process.PREPARE_INTERVIEW)
    (out,) = defn.out_flows("Reviewer_reads")
    assert out.target == "Gw_read"
    ways = defn.out_flows("Gw_read")
    assert {f.id for f in ways} == {"F_read_in", "F_read_skipped"}
    assert {f.target for f in ways} == {"File_report"}
    (skipped,) = [f for f in ways if f.id == "F_read_skipped"]
    assert skipped.condition == "read.submitted = false"
    assert next(f for f in ways if f.id == "F_read_in").is_default


def test_the_mail_reader_wait_is_a_twelve_hour_timer_equal_to_the_apps_own():
    defn = process.build_library().get(process.READ_MAIL)
    timer = defn.elements["B_reader_late"]
    assert timer.kind == BOUNDARY and timer.attached_to == "Mail_reader_reads"
    assert timer.trigger.type == "timer" and timer.trigger.timer_kind == "duration"
    assert parse_duration(timer.trigger.timer_text) == mail_sync.MODEL_WAIT
    (out,) = defn.out_flows("B_reader_late")
    assert out.target == "Match_to_roles"


def _python_files():
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def test_nothing_ticks_the_job_search_engine():
    """The 12-hour timer is drawn and never started because nothing ticks the engine. A caller that did would take the
    timer's way without dropping the waiting mail (`give_up_waiting` is the app's own clock), so it must fail here."""
    receivers = {}
    for path in _python_files():
        for m in re.finditer(r"(\w+)\.tick\(", path.read_text(encoding="utf-8")):
            receivers.setdefault(m.group(1), []).append(path.relative_to(APP).as_posix())
    assert set(receivers) <= {"pace"}, f"something ticks an engine: {receivers}"


def test_only_the_two_known_completions_say_submitted_false():
    """The drawn failed ways read `<output>.submitted = false`. Only the interview's skipped read (`file_without`) and
    the mail reader's close (`end_step`, called with false by `sync`) complete a step that way, so every other drawn
    failed way stays unreachable. A new place that does needs the failed way re-checked on purpose."""
    found = set()
    for path in _python_files():
        if re.search(r'"submitted":\s*(?!True\b)\S', path.read_text(encoding="utf-8")):
            found.add(path.relative_to(APP).as_posix())
    assert found == {"jobsearch/interviews/coach.py", "jobsearch/mail/haiku.py"}
