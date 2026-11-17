"""The site-project process files (`playbooks/sites/`, docs/PRD.md path 29): the project round, Do one plan item and
Check one item. They load and validate with the vendored engine, the job-search roles and the real instruction and
template files, and the check is code (`site.check`), never an agent. This file checks their shape with the actions
stubbed out: what each step does is tested where its code lives (`test_site_round_process.py`, `test_site_e2e.py`).

They are not part of the job-search library (`app.jobsearch.process` reads only `playbooks/jobsearch/`)."""

from pathlib import Path

import pytest
import yaml
from process_engine.loader import ProcessLibrary
from process_engine.model import AGENT, CALL, END, GATEWAY, SERVICE, START, USER
from process_engine.registry import ActionRegistry

from app.config import settings
from app.harness import instructions as harness_instructions
from app.harness.loader import load_harness
from app.jobsearch import process

SITES = Path(settings.playbooks_path) / "sites"
FILES = ("check_one_item.bpmn", "do_one_item.bpmn", "project.round.bpmn")
ROUND, DO, CHECK = "project_round", "do_one_item", "check_one_item"

#: Every action the three files name. Each is a stub here; the code that owns each step registers the real one.
ACTIONS = {
    "round.assemble_task", "round.start_plan", "round.make_checks", "round.write_report",
    "round.next_plan", "round.finish", "round.retry_planner", "round.turn_site_off",
    "round.hand_to_browser",
    "item.hand_out", "item.read",
    "check.make", "site.check",
}


async def _stub(session, instance, data):
    return {}


def stub_registry() -> ActionRegistry:
    registry = ActionRegistry()
    for name in sorted(ACTIONS):
        registry.register(name, _stub, description=f"Stub for {name} until its step is written.",
                          source="tests/sites/test_site_playbooks.py")
    return registry


def build(registry: ActionRegistry | None = None) -> ProcessLibrary:
    registry = registry or stub_registry()
    library = ProcessLibrary()
    catalog = process.catalog()
    for name in FILES:
        library.load((SITES / name).read_text(encoding="utf-8"), catalog=catalog, registry=registry)
    library.validate()
    return library


def elements(library: ProcessLibrary, key: str, kind: str) -> list:
    return [e for e in library.get(key).elements.values() if e.kind == kind]


def test_the_three_files_and_the_loop_file_are_where_the_settings_say():
    for name in FILES:
        assert (SITES / name).is_file(), name
    assert Path(settings.process_path).is_file(), "the harness reads playbooks/project.pdca.process.yaml"


def test_the_files_load_and_validate_with_the_engine_the_job_search_roles_and_the_real_instruction_files():
    library = build()
    assert library.keys() == [CHECK, DO, ROUND]
    assert "scout" in process.catalog().roles


def test_they_are_not_in_the_job_search_library():
    assert set(process.build_library().keys()).isdisjoint({ROUND, DO, CHECK})


def test_the_stub_actions_are_every_action_the_files_name_and_no_other():
    library = build()
    named = {e.action for key in library.keys() for e in elements(library, key, SERVICE)}
    assert named == ACTIONS
    # a file that named an action nobody registered would be refused: leave one out and see
    short = stub_registry()
    short._actions.pop("site.check")
    with pytest.raises(Exception, match="site.check"):
        build(short)


def test_the_round_calls_do_one_item_and_check_one_item_once_for_each_item():
    calls = elements(build(), ROUND, CALL)
    assert [(e.id, e.called, e.multi.collection, e.multi.element_variable) for e in calls] == [
        ("PR-9", DO, "plan.items", "item"),
        ("PR-11", CHECK, "checks.items", "item"),
    ]


def test_whether_a_plan_starts_is_one_gateway_over_the_published_facts_start_unreadable_and_start_needs_browser():
    rnd = build().get(ROUND)
    gateway = rnd.elements["PR-7N"]
    assert gateway.kind == GATEWAY
    assert not [f for f in rnd.flows.values() if f.condition and "start.needs_owner" in f.condition]
    # a site that cannot be read asks no one: its own way out, to the step that switches it off and tells the owner
    unreadable = [f for f in rnd.flows.values() if f.condition and "start.unreadable" in f.condition]
    assert [(f.id, f.condition, f.source, f.target) for f in unreadable] == [
        ("F_unreadable", "start.unreadable = true", "PR-7N", "PR-7U")]
    # a site the server can't read but the browser has not been tried on goes to the browser, nothing is switched off
    browser = [f for f in rnd.flows.values() if f.condition and "start.needs_browser" in f.condition]
    assert [(f.id, f.condition, f.source, f.target) for f in browser] == [
        ("F_browser", "start.needs_browser = true", "PR-7N", "PR-7B")]
    assert rnd.elements["PR-7B"].action == "round.hand_to_browser" and rnd.elements["PR-7B"].kind == SERVICE
    assert [f.target for f in rnd.flows.values() if f.source == "PR-7B"] == ["E-browser"]
    other = [rnd.flows[f] for f in gateway.outgoing if rnd.flows[f].id not in ("F_unreadable", "F_browser")]
    assert [(f.id, f.is_default, f.target) for f in other] == [("F_starts", True, "PR-6S")]
    assert rnd.elements["PR-7U"].action == "round.turn_site_off" and rnd.elements["PR-7U"].kind == SERVICE
    assert [f.target for f in rnd.flows.values() if f.source == "PR-7U"] == ["E-unreadable"]
    for fact in ("start.unreadable", "start.needs_browser", "start.why", "turned_off.why", "handed.why"):
        assert rnd.data_model.resolve(fact) is not None, fact
    # the owner is never asked about a plan: the round has no user step
    assert elements(build(), ROUND, USER) == []
    # nothing else in the round is a decision about starting: the one reduced step starts a plan without asking
    assert rnd.elements["PR-6S"].action == "round.start_plan"


def test_every_agent_step_is_the_scout_and_names_instruction_and_template_files_that_exist():
    library = build()
    agents = {(key, e.id): (e.role, e.instructions, e.template)
              for key in library.keys() for e in elements(library, key, AGENT)}
    assert agents == {
        (ROUND, "PR-4"): ("scout", "projects.plan.write", "projects.plan.response"),
        (ROUND, "PR-13"): ("scout", "projects.act.learn", "projects.act.response"),
        (DO, "Specialist_does"): ("scout", "projects.do.item", "projects.do.item.response"),
    }
    for _role, instruction, template in agents.values():
        assert instruction in harness_instructions.names("instructions")
        assert template in harness_instructions.names("templates")


def test_check_is_code_one_site_check_step_and_no_agent_check():
    library = build()
    assert elements(library, CHECK, AGENT) == []
    (check,) = [e for e in elements(library, CHECK, SERVICE) if e.action == "site.check"]
    assert check.output == "found"
    assert [e.action for e in elements(library, CHECK, SERVICE)] == ["check.make", "site.check"]
    gateway = [e for e in elements(library, CHECK, GATEWAY)]
    assert [g.id for g in gateway] == ["Gw_found"]
    ends = sorted(e.id for e in elements(library, CHECK, END))
    assert ends == ["End_off_plan", "End_on_plan"]


def test_every_path_of_the_round_can_end_and_every_step_is_reachable_from_a_start():
    # the loader refuses unreachable steps and paths that can't end; this pins the starts and ends the round has
    rnd = build().get(ROUND)
    starts = sorted(e.id for e in rnd.elements.values() if e.kind == START)
    assert starts == ["PRT-2", "PRT-4", "PRT-5", "PRT-6", "PRT-8"]
    ends = sorted(e.id for e in rnd.elements.values() if e.kind == END)
    assert ends == ["E-browser", "E-finished", "E-retried", "E-unreadable"]


def test_the_close_rule_is_kept_the_finish_gateway_and_acts_judging_against_the_projects_outcome():
    rnd = build().get(ROUND)
    finish = rnd.flows["F_finish"]
    assert finish.condition == (
        '(report.verdict = "accepted" and report.more = 0) or (act.resolved = true and report.closable = true)')
    text = (SITES / "project.round.bpmn").read_text(encoding="utf-8")
    assert "Act judges the round against `closing.outcome`, the project's own outcome" in text


def test_the_loop_file_loads_as_the_harness_reads_it_and_names_files_that_exist():
    harness = load_harness(Path(settings.vocab_path), Path(settings.kinds_path), Path(settings.process_path))
    loop = harness.process
    assert loop is not None and loop.process == "project.pdca"
    assert [s.id for s in loop.steps] == [
        "plan.write", "do.item", "check.item", "check.report", "act.learn", "loop.decide", "finish"]
    assert {s.role for s in loop.steps} <= {"scout", "owner", "code"}
    for step in loop.steps:
        if step.instructions:
            assert harness_instructions.short_name("instructions", step.instructions) in harness_instructions.names(
                "instructions")
        if step.template:
            assert harness_instructions.short_name("templates", step.template) in harness_instructions.names(
                "templates")
    assert yaml.safe_load(Path(settings.process_path).read_text(encoding="utf-8"))["process"] == "project.pdca"
