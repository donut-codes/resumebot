"""Loading BPMN into an executable model, and refusing what the engine can't run (path 15.1, 15.2, 15.3)."""

import pytest
from support import CATALOG, fixture_xml, sample_library, sample_registry

from process_engine import registry as registry_module
from process_engine.config import configure
from process_engine.errors import ProcessLoadError
from process_engine.loader import ProcessLibrary, load_process
from process_engine.registry import ActionRegistry, Catalog, RegistryError, default_catalog, register_card_kind
from process_engine.registry import actions as engine_actions

NS = ('xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:porter="http://porter.local/bpmn/ext" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')


def tiny(body: str, model: str = "", flows: str | None = None) -> str:
    """A small process: Start -> (body) -> End. `body` is the elements in between, `flows` the sequence flows."""
    flows = flows if flows is not None else (
        '<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
        '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>')
    ext = f"<bpmn:extensionElements><porter:dataModel>{model}</porter:dataModel></bpmn:extensionElements>"
    return (f'<bpmn:definitions {NS} id="D"><bpmn:process id="P" name="Tiny">{ext}'
            f'<bpmn:startEvent id="Start"/>{body}<bpmn:endEvent id="End"/>{flows}</bpmn:process></bpmn:definitions>')


def refused(xml: str, *fragments: str, registry: ActionRegistry | None = None):
    with pytest.raises(ProcessLoadError) as caught:
        load_process(xml, catalog=CATALOG, registry=registry or sample_registry())
    for fragment in fragments:
        assert fragment in str(caught.value), f"{fragment!r} not in {caught.value}"
    return caught.value


# --- the two samples load, and the page's reader still reads them ---

def test_the_samples_load_with_everything_the_engine_needs():
    library = sample_library()
    intake = library.get("intake")
    assert intake.name == "Intake"
    assert intake.warnings == []
    assert intake.start_events == ["Start_msg", "Start_timer"]
    assert intake.elements["Start_msg"].trigger.message == "intake.requested"
    assert intake.elements["Start_timer"].trigger.timer_kind == "cycle"
    assert intake.elements["Start_timer"].trigger.timer_text == "R/P1D"
    load = intake.elements["Load"]
    assert (load.kind, load.action, load.output, load.lane) == ("service", "plan.load", "plan", "The app")
    approve = intake.elements["Approve"]
    assert (approve.kind, approve.card, approve.lane) == ("user", "plan.approval", "Household owner")
    summary = intake.elements["Summary"]
    assert (summary.kind, summary.role, summary.instructions, summary.template) == (
        "agent", "planner", "summary.write", "summary.response")
    assert summary.lane == "Chief of Staff"
    assert summary.boundaries == ["B_slow"]
    assert intake.elements["B_slow"].trigger.timer_kind == "duration"
    assert intake.elements["B_notify"].trigger.error_code == "notify_failed"
    multi = intake.elements["Look_all"]
    assert (multi.called, multi.multi.sequential, multi.multi.collection, multi.multi.element_variable) == (
        "check.one", False, "plan.items", "item")
    assert multi.out_map == (("verdicts", "look.verdict"),)
    assert intake.elements["Look_head"].in_map == (("item", "plan.headline"),)
    assert intake.elements["Confirm"].trigger.message == "intake.confirmed"
    assert intake.elements["Settle"].trigger.timer_text == "PT1H"
    need = intake.out_flows("Gw_need")
    assert [f.id for f in need] == ["F_ask", "F_empty", "F_go"]
    assert [f.is_default for f in need] == [False, False, True]
    assert need[0].condition.startswith("some i in plan.items")
    assert need[0].documentation == "Ask when any item is high risk or deletes something."
    assert library.get("check.one").elements["Second_look"].role == "doer"




def test_the_page_can_describe_a_loaded_process():
    described = {s["id"]: s for s in sample_library().get("intake").describe()}
    assert described["Gw_need"]["ways_out"][0]["meaning"].startswith("Ask when")
    assert described["Load"]["action"] == "plan.load"


def test_the_same_file_has_the_same_hash_and_a_changed_file_does_not():
    registry = sample_registry()
    a = load_process(fixture_xml("check_one.bpmn"), catalog=CATALOG, registry=registry)
    b = load_process(fixture_xml("check_one.bpmn"), catalog=CATALOG, registry=registry)
    c = load_process(fixture_xml("check_one.bpmn").replace("Look at it", "Look at it closely"),
                     catalog=CATALOG, registry=registry)
    assert a.file_hash == b.file_hash != c.file_hash


# --- what the engine can't run is refused, naming the element ---

@pytest.mark.parametrize("element, fragment", [
    ('<bpmn:parallelGateway id="X" name="Fork"/>', "'Fork' (X) is a parallelGateway"),
    ('<bpmn:inclusiveGateway id="X" name="Some of them"/>', "only exclusive gateways"),
    ('<bpmn:eventBasedGateway id="X"/>', "only exclusive gateways"),
    ('<bpmn:subProcess id="X" name="Inline"/>', "use a call activity"),
    ('<bpmn:scriptTask id="X" name="Compute"/>', "registered action instead"),
    ('<bpmn:manualTask id="X" name="Do it by hand"/>', "'Do it by hand' (X)"),
    ('<bpmn:sendTask id="X"/>', "sendTask"),
    ('<bpmn:businessRuleTask id="X"/>', "businessRuleTask"),
    ('<bpmn:intermediateThrowEvent id="X" name="Shout"/>', "'Shout' (X)"),
    ('<bpmn:complexGateway id="X"/>', "only exclusive gateways"),
])
def test_elements_outside_the_supported_subset_are_refused(element, fragment):
    refused(tiny(element), fragment)


def test_a_file_with_no_process_or_bad_xml_is_refused():
    with pytest.raises(ProcessLoadError, match="Not valid XML"):
        load_process("<bpmn:definitions", catalog=CATALOG)
    with pytest.raises(ProcessLoadError, match="definitions"):
        load_process("<foo/>", catalog=CATALOG)
    with pytest.raises(ProcessLoadError, match="holds one process"):
        load_process(f"<bpmn:definitions {NS}/>", catalog=CATALOG)


def test_entity_expansion_is_refused_not_expanded():
    bomb = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            f'<bpmn:definitions {NS}><bpmn:process id="P">&b;</bpmn:process></bpmn:definitions>')
    with pytest.raises(ProcessLoadError, match="DOCTYPE"):
        load_process(bomb, catalog=CATALOG)


def test_every_problem_is_reported_in_one_pass():
    xml = tiny('<bpmn:serviceTask id="Task" name="Nothing named"/><bpmn:parallelGateway id="G" name="Fork"/>')
    error = refused(xml, "'Nothing named' (Task)", "'Fork' (G)")
    assert len(error.problems) >= 2


# --- binding to code by name ---

def test_service_task_must_name_a_registered_action():
    refused(tiny('<bpmn:serviceTask id="Task" name="Do it"/>'), "'Do it' (Task) is a service task but names no action")
    refused(tiny('<bpmn:serviceTask id="Task" name="Do it" porter:action="no.such"/>'),
            "names the action 'no.such', which isn't registered")


def test_an_action_that_reads_a_field_the_model_does_not_declare_is_refused():
    xml = tiny('<bpmn:serviceTask id="Task" name="Look" porter:action="item.look"/>')
    refused(xml, "the action 'item.look' reads 'item', which the data model doesn't declare")
    load_process(tiny('<bpmn:serviceTask id="Task" name="Look" porter:action="item.look"/>',
                      '<porter:field path="item" type="any"/>'), catalog=CATALOG, registry=sample_registry())


def test_agent_task_names_a_role_instructions_and_template_that_exist():
    ok = '<bpmn:task id="Task" name="Plan" porter:role="planner" porter:instructions="summary.write" ' \
         'porter:template="summary.response"/>'
    loaded = load_process(tiny(ok), catalog=CATALOG, registry=sample_registry())
    assert loaded.elements["Task"].role == "planner"
    # the path a file stores works too and is kept as the short name
    configure(instructions=_Files())  # an app says how a stored path is a name
    stored = ok.replace('"summary.write"', '"instructions/summary.write.instructions.md"')
    assert load_process(tiny(stored), catalog=CATALOG).elements["Task"].instructions == "summary.write"
    refused(tiny('<bpmn:task id="Task" name="Plan"/>'), "a plain task with no role")
    refused(tiny(ok.replace('"planner"', '"wizard"')), "names the role 'wizard', which doesn't exist")
    refused(tiny(ok.replace('"summary.write"', '"nope"')), "the instruction file 'nope', which doesn't exist")
    refused(tiny(ok.replace('"summary.response"', '"nope"')), "the response template 'nope', which doesn't exist")


def test_user_task_names_a_card_that_exists():
    refused(tiny('<bpmn:userTask id="Task" name="Ask"/>'), "names no card")
    refused(tiny('<bpmn:userTask id="Task" name="Ask" porter:card="no.card"/>'),
            "names the card 'no.card', which doesn't exist")
    load_process(tiny('<bpmn:userTask id="Task" name="Ask" porter:card="pick_goal"/>'), catalog=CATALOG)


class _Files:
    """An app's instruction files, as `config.instructions` asks for them."""

    def names(self, kind):
        return {"instructions": ["projects.plan.write"], "templates": ["projects.plan.response"]}[kind]

    def short_name(self, kind, ref):
        return ref.rsplit("/", 1)[-1].removesuffix(".instructions.md").removesuffix(".md")


def test_names_are_checked_against_what_the_app_configured_by_default():
    ok = ('<bpmn:task id="Task" name="Plan" porter:role="planner" porter:instructions="projects.plan.write" '
          'porter:template="projects.plan.response"/>')
    card = '<bpmn:userTask id="Task" porter:card="pick_goal"/>'
    # nothing configured: no role, no file and no card exists
    with pytest.raises(ProcessLoadError, match="planner"):
        load_process(tiny(ok), catalog=default_catalog())
    configure(roles=lambda: ["planner"], card_kinds=["pick_goal"], instructions=_Files())
    load_process(tiny(ok), catalog=default_catalog())
    pathed = ok.replace("projects.plan.write", "instructions/projects.plan.write.md")
    load_process(tiny(pathed), catalog=default_catalog())
    with pytest.raises(ProcessLoadError, match="doesn't exist"):
        load_process(tiny(ok.replace("projects.plan.write", "projects.plan.writ")), catalog=default_catalog())
    load_process(tiny(card), catalog=default_catalog())
    with pytest.raises(ProcessLoadError, match="card 'pick_goal'"):
        load_process(tiny(card), catalog=Catalog(roles=CATALOG.roles))


def test_a_registered_card_kind_counts_and_cannot_be_registered_twice(monkeypatch):
    monkeypatch.setattr(registry_module, "_CARD_KINDS", {})
    configure(card_kinds=["pick_goal"])
    register_card_kind("review.note")
    assert default_catalog().cards == {"pick_goal", "review.note"}
    with pytest.raises(RegistryError):
        register_card_kind("review.note")
    with pytest.raises(RegistryError):
        register_card_kind("pick_goal")


def test_the_default_registry_holds_nothing_the_samples_registered():
    assert "plan.load" not in engine_actions


# --- structure ---

def test_a_process_needs_a_start_an_end_and_every_step_reachable():
    refused(tiny('<bpmn:userTask id="Task" porter:card="pick_goal"/>',
                 flows='<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>'),
            "'Start' (Start) must have exactly one way out", "'Task' (Task) can't be reached from a start")
    refused(f'<bpmn:definitions {NS}><bpmn:process id="P"><bpmn:userTask id="T" porter:card="pick_goal"/>'
            '</bpmn:process></bpmn:definitions>', "no start event", "no end event")
    unreachable = tiny('<bpmn:userTask id="Task" porter:card="pick_goal"/>'
                       '<bpmn:userTask id="Lonely" name="Lonely" porter:card="pick_goal"/>',
                       flows='<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
                             '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>'
                             '<bpmn:sequenceFlow id="f3" sourceRef="Lonely" targetRef="End"/>')
    refused(unreachable, "'Lonely' (Lonely) can't be reached from a start")


def test_a_step_with_two_ways_out_needs_a_decision():
    xml = tiny('<bpmn:userTask id="Task" porter:card="pick_goal"/>',
               flows='<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
                     '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>'
                     '<bpmn:sequenceFlow id="f3" sourceRef="Task" targetRef="End"/>')
    refused(xml, "must have exactly one way out, not 2 (use a decision to split)")


def test_a_flow_to_nowhere_is_refused():
    refused(tiny('<bpmn:userTask id="Task" porter:card="pick_goal"/>',
                 flows='<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
                       '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="Ghost"/>'),
            "joins 'Task' to 'Ghost'")


def test_conditions_belong_on_a_decisions_ways_out_only():
    xml = tiny('<bpmn:userTask id="Task" porter:card="pick_goal"/>',
               flows='<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
                     '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"><bpmn:conditionExpression>true'
                     '</bpmn:conditionExpression></bpmn:sequenceFlow>')
    refused(xml, "has a condition, but only a decision's ways out can")


# --- decisions ---

GATE_MODEL = ('<porter:field path="n" type="number"/><porter:field path="flag" type="boolean"/>'
              '<porter:field path="mode" type="string" enum="a,b"/><porter:field path="t" type="any"/>')


def gate(*ways: str, default: str | None = None, extra: str = "") -> str:
    """Start -> Gate -> two ends, ways = (flow xml...)."""
    dflt = f' default="{default}"' if default else ""
    return tiny(f'<bpmn:exclusiveGateway id="Gate" name="Which way?"{dflt}/><bpmn:endEvent id="End2"/>{extra}',
                GATE_MODEL,
                flows='<bpmn:sequenceFlow id="f0" sourceRef="Start" targetRef="Gate"/>' + "".join(ways))


def way(flow_id: str, target: str, condition: str | None = None, name: str = "") -> str:
    cond = f"<bpmn:conditionExpression>{condition}</bpmn:conditionExpression>" if condition else ""
    return (f'<bpmn:sequenceFlow id="{flow_id}" name="{name}" sourceRef="Gate" targetRef="{target}">'
            f'{cond}</bpmn:sequenceFlow>')


def test_a_decision_with_a_default_and_conditions_on_the_rest_loads():
    loaded = load_process(gate(way("a", "End", "n &gt; 5", "Big"), way("b", "End2"), default="b"), catalog=CATALOG)
    assert [f.is_default for f in loaded.out_flows("Gate")] == [False, True]


def test_a_way_out_with_neither_condition_nor_default_is_refused():
    refused(gate(way("a", "End", "n &gt; 5", "Big"), way("b", "End2", name="Other"), default="a"),
            "the default way out 'Big' must not have a condition", "the way out 'Other' has no condition")
    refused(gate(way("a", "End", "n &gt; 5", "Big"), way("b", "End2", name="Other")),
            "the way out 'Other' has no condition and isn't the default")


def test_a_default_that_does_not_leave_the_decision_is_refused():
    refused(gate(way("a", "End", "n &gt; 5"), way("b", "End2", "n &lt;= 5"), default="zzz"),
            "names 'zzz' as its default, which doesn't leave it")


def test_without_a_default_the_conditions_must_cover_every_case_even_a_field_with_no_value():
    # exact opposites answer for every data: `=` and `!=` never leave a case out, and nor does a yes/no and its `not`
    load_process(gate(way("a", "End", "n = 5"), way("b", "End2", "n != 5")), catalog=CATALOG)
    load_process(gate(way("a", "End", 'mode = "a"'), way("b", "End2", 'mode != "a"')), catalog=CATALOG)
    load_process(gate(way("a", "End", 'mode in ["a"]'), way("b", "End2", 'not(mode in ["a"])')), catalog=CATALOG)
    load_process(gate(way("a", "End", "true"), way("b", "End2", "flag")), catalog=CATALOG)
    cover = "no default way out and its conditions don't visibly cover every case"
    # these look like they cover everything, but a field with no value yet answers null to each of them
    refused(gate(way("a", "End", "n &lt; 5"), way("b", "End2", "n &gt;= 5")), cover)
    refused(gate(way("a", "End", "flag"), way("b", "End2", "not(flag)")), cover)
    refused(gate(way("a", "End", "flag = true"), way("b", "End2", "flag = false")), cover)
    refused(gate(way("a", "End", 'mode = "a"'), way("b", "End2", 'mode = "b"')), cover)
    refused(gate(way("a", "End", "n &lt; 5"), way("b", "End2", "n &gt; 5")), cover)
    # `x = 1` and `x != true` are not opposites, though `1 == True` in Python
    refused(gate(way("a", "End", "t = 1"), way("b", "End2", "t != true")), cover)


def test_a_condition_over_an_undeclared_field_is_refused():
    refused(gate(way("a", "End", "cost &gt; 5", "Pricey"), way("b", "End2"), default="b"),
            "the condition of 'Pricey'", "'cost', which the data model doesn't declare")


def test_a_condition_that_cannot_be_read_or_is_not_yes_or_no_is_refused():
    refused(gate(way("a", "End", "n ===", "Odd"), way("b", "End2"), default="b"), "can't be read")
    refused(gate(way("a", "End", "n + 1", "Number"), way("b", "End2"), default="b"), "not a true/false condition")
    refused(gate(way("a", "End", 'mode = "zzz"', "Typo"), way("b", "End2"), default="b"), 'never "zzz"')


def test_a_decision_with_one_way_out_takes_no_condition():
    refused(tiny('<bpmn:exclusiveGateway id="Gate" name="Only"/>', GATE_MODEL,
                 flows='<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Gate"/>'
                       '<bpmn:sequenceFlow id="f2" sourceRef="Gate" targetRef="End">'
                       '<bpmn:conditionExpression>flag</bpmn:conditionExpression></bpmn:sequenceFlow>'),
            "has one way out, which has a condition")


def test_a_condition_without_its_plain_words_is_a_warning_not_a_failure():
    loaded = load_process(gate(way("a", "End", "n = 5", "Five"), way("b", "End2", "n != 5", "Other")),
                          catalog=CATALOG)
    assert len(loaded.warnings) == 2 and "no plain-words meaning" in loaded.warnings[0]


# --- boundary events, waits, multi-instance ---

def test_boundary_events_are_checked():
    host = '<bpmn:userTask id="Task" porter:card="pick_goal"/>'
    flows = ('<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
             '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>'
             '<bpmn:sequenceFlow id="f3" sourceRef="B" targetRef="End"/>')

    def with_boundary(boundary: str, task: str = host) -> str:
        return tiny(task + boundary, flows=flows)

    timer = '<bpmn:boundaryEvent id="B" name="Late" attachedToRef="Task"><bpmn:timerEventDefinition>' \
            '<bpmn:timeDuration>{}</bpmn:timeDuration></bpmn:timerEventDefinition></bpmn:boundaryEvent>'
    load_process(with_boundary(timer.format("PT30M")), catalog=CATALOG)
    load_process(with_boundary(timer.format("P2DT4H")), catalog=CATALOG)
    refused(with_boundary(timer.format("P1M")), "months and years aren't supported")
    refused(with_boundary(timer.format("soon")), "'soon' is not a period")
    refused(with_boundary(timer.format("PT1H").replace('attachedToRef="Task"', 'attachedToRef="Nope"')),
            "is attached to 'Nope'")
    refused(with_boundary(timer.format("PT1H").replace(">", ' cancelActivity="false">', 1)),
            "only interrupting boundary events are supported")
    refused(with_boundary('<bpmn:boundaryEvent id="B" attachedToRef="Task"><bpmn:signalEventDefinition/>'
                          '</bpmn:boundaryEvent>'), "only timer and error boundary events are supported")
    error = '<bpmn:boundaryEvent id="B" attachedToRef="Task"><bpmn:errorEventDefinition/></bpmn:boundaryEvent>'
    refused(with_boundary(error), "catches an error, which only an action's step can raise")
    refused(with_boundary(timer.format("PT1H"),
                          task='<bpmn:serviceTask id="Task" porter:action="plan.load"/>'),
            "a timer on a step that doesn't wait")


def test_waits_and_starts_are_checked():
    catch = ('<bpmn:intermediateCatchEvent id="Task" name="Wait">{}</bpmn:intermediateCatchEvent>')
    load_process(tiny(catch.format('<bpmn:messageEventDefinition messageRef="m"/>')), catalog=CATALOG)
    refused(tiny(catch.format("<bpmn:signalEventDefinition/>")), "waits on a signal event")
    refused(tiny(catch.format("")), "waits on a plain event")
    with pytest.raises(ProcessLoadError, match="starts on a signal"):
        load_process(tiny('<bpmn:userTask id="Task" porter:card="pick_goal"/>').replace(
            '<bpmn:startEvent id="Start"/>', '<bpmn:startEvent id="Start"><bpmn:signalEventDefinition/>'
            '</bpmn:startEvent>'), catalog=CATALOG)


def test_multi_instance_is_for_call_activities_with_a_list_and_an_item_variable():
    call = ('<bpmn:callActivity id="Task" name="Each" calledElement="x" porter:out="r=a">{}</bpmn:callActivity>')
    model = '<porter:field path="items" type="list"/><porter:field path="r" type="list"/>' \
            '<porter:field path="a" type="any"/><porter:field path="s" type="string"/>'
    ok = '<bpmn:multiInstanceLoopCharacteristics isSequential="true" porter:collection="items" ' \
         'porter:elementVariable="it"/>'
    loaded = load_process(tiny(call.format(ok), model), catalog=CATALOG)
    assert loaded.elements["Task"].multi.sequential is True
    refused(tiny(call.format(ok.replace("items", "s")), model), "which is a string, not a list")
    refused(tiny(call.format(ok.replace("items", "nothing")), model),
            "once per item of 'nothing', which the data model doesn't declare")
    refused(tiny(call.format(ok.replace(' porter:elementVariable="it"', "")), model),
            "names no item variable")
    refused(tiny(call.format("<bpmn:standardLoopCharacteristics/>"), model), "only 'once per item'")
    refused(tiny(f'<bpmn:userTask id="Task" porter:card="pick_goal">{ok}</bpmn:userTask>', model),
            "only a call activity can run once per item")


def test_a_call_hands_over_and_brings_back_only_declared_fields():
    call = '<bpmn:callActivity id="Task" name="Call" calledElement="x" porter:in="a=ghost" porter:out="b=c"/>'
    refused(tiny(call, '<porter:field path="b" type="any"/>'), "hands 'ghost' to the sub-process")
    refused(tiny(call.replace("ghost", "g"), '<porter:field path="g" type="any"/>'), "brings back into 'b'")
    refused(tiny(call.replace("a=ghost", "nonsense")), "'nonsense' should read name=path")


# --- the action registry ---

def test_actions_register_once_and_list_for_the_page():
    registry = ActionRegistry()

    async def fn(session, instance, data):
        return {}

    registry.register("staff.assign", fn, inputs=["skill"], outputs=["agent"],
                      description="Gives a skill to an agent.", source="app/staff.py")
    with pytest.raises(RegistryError, match="already registered"):
        registry.register("staff.assign", fn, description="Again.", source="x")
    assert registry.listing() == [{
        "name": "staff.assign", "description": "Gives a skill to an agent.", "inputs": ["skill"],
        "outputs": ["agent"], "source": "app/staff.py"}]
    assert "staff.assign" in registry and registry.names() == ["staff.assign"]


@pytest.mark.parametrize("kwargs, fragment", [
    ({"name": "Bad Name"}, "not an action name"),
    ({"name": "ok.name", "description": ""}, "plain description"),
    ({"name": "ok.name", "source": ""}, "its source"),
])
def test_actions_must_be_named_described_and_sourced(kwargs, fragment):
    async def fn(session, instance, data):
        return {}

    args = {"description": "Does a thing.", "source": "x.py", **kwargs}
    with pytest.raises(RegistryError, match=fragment):
        ActionRegistry().register(fn=fn, **args)


def test_an_action_must_be_async():
    def sync(session, instance, data):
        return {}

    with pytest.raises(RegistryError, match="async"):
        ActionRegistry().register("a.b", sync, description="x", source="y")


# --- processes calling each other ---

def test_a_library_checks_its_calls():
    registry = sample_registry()
    library = ProcessLibrary()
    library.load(fixture_xml("intake.bpmn"), catalog=CATALOG, registry=registry)
    with pytest.raises(ProcessLoadError, match="calls 'check.one', which isn't a loaded process"):
        library.validate()
    library.load(fixture_xml("check_one.bpmn"), catalog=CATALOG, registry=registry)
    library.validate()
    assert library.keys() == ["check.one", "intake"]


def test_a_library_refuses_fields_the_called_process_does_not_declare():
    registry = sample_registry()
    library = ProcessLibrary()
    library.load(fixture_xml("check_one.bpmn"), catalog=CATALOG, registry=registry)
    changed = fixture_xml("intake.bpmn").replace('porter:in="item=plan.headline"', 'porter:in="nobody=plan.headline"')
    library.load(changed, catalog=CATALOG, registry=registry)
    with pytest.raises(ProcessLoadError, match="hands over 'nobody', which 'check.one' doesn't declare"):
        library.validate()


def test_processes_calling_each_other_in_a_circle_are_refused():
    def loop(key: str, other: str) -> str:
        return (f'<bpmn:definitions {NS} id="D"><bpmn:process id="{key}" porter:key="{key}">'
                f'<bpmn:startEvent id="Start"/><bpmn:callActivity id="Task" calledElement="{other}"/>'
                '<bpmn:endEvent id="End"/>'
                '<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
                '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/></bpmn:process></bpmn:definitions>')

    library = ProcessLibrary()
    library.load(loop("a", "b"), catalog=CATALOG)
    library.load(loop("b", "a"), catalog=CATALOG)
    with pytest.raises(ProcessLoadError, match="call each other in a circle: a -> b -> a"):
        library.validate()


def test_an_old_version_stays_available_to_running_instances():
    registry = sample_registry()
    library = ProcessLibrary()
    old = library.load(fixture_xml("check_one.bpmn"), catalog=CATALOG, registry=registry)
    new = library.load(fixture_xml("check_one.bpmn").replace("Look at it", "Look closer"),
                       catalog=CATALOG, registry=registry)
    assert library.get("check.one") is new
    assert library.get("check.one", old.file_hash) is old
    assert library.get("check.one", "0" * 64) is None
