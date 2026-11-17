"""What the loader refuses, in plain words, and that it only ever refuses: `load_process` raises `ProcessLoadError` and
nothing else, whatever it is given."""

import uuid
import xml.etree.ElementTree as ET  # noqa: S405 - builds a tree from text this file wrote itself

import pytest
from support import CATALOG, sample_library, sample_registry

from process_engine import bpmn, feel, loader
from process_engine import registry as registry_module
from process_engine.config import configure
from process_engine.errors import ProcessLoadError
from process_engine.loader import MAX_FILE_BYTES, load_process
from process_engine.model import StartNotFound, parse_duration
from process_engine.registry import ActionRegistry, RegistryError, default_catalog

NS = ('xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:porter="http://porter.local/bpmn/ext" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
MODEL = ('<porter:field path="n" type="number"/><porter:field path="flag" type="boolean"/>'
         '<porter:field path="mode" type="string" enum="a,b"/>')
CARD = 'porter:card="pick_goal"'


def tiny(body: str = f'<bpmn:userTask id="Task" {CARD}/>', flows: str | None = None, start: str = "",
         root_attrs: str = "") -> str:
    """Start -> Task -> End. `body` replaces the Task, `flows` the flows, `start` goes inside the start event."""
    flows = flows if flows is not None else (
        '<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
        '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>')
    ext = f"<bpmn:extensionElements><porter:dataModel>{MODEL}</porter:dataModel></bpmn:extensionElements>"
    return (f'<bpmn:definitions {NS} id="D"><bpmn:process id="P" name="Tiny"{root_attrs}>{ext}'
            f'<bpmn:startEvent id="Start">{start}</bpmn:startEvent>{body}<bpmn:endEvent id="End"/>{flows}'
            '</bpmn:process></bpmn:definitions>')


def refused(xml, *fragments: str) -> ProcessLoadError:
    with pytest.raises(ProcessLoadError) as caught:
        load_process(xml, catalog=CATALOG, registry=sample_registry())
    for fragment in fragments:
        assert fragment in str(caught.value), f"{fragment!r} not in {caught.value}"
    return caught.value


def loads(xml):
    return load_process(xml, catalog=CATALOG, registry=sample_registry())


def gate(*ways: str, default: str | None = None) -> str:
    """Start -> Gate -> End / End2 by the given ways (each `(flow xml)`)."""
    dflt = f' default="{default}"' if default else ""
    return tiny(f'<bpmn:exclusiveGateway id="Gate" name="Which way?"{dflt}/><bpmn:endEvent id="End2"/>',
                flows='<bpmn:sequenceFlow id="f0" sourceRef="Start" targetRef="Gate"/>' + "".join(ways))


def way(flow_id: str, target: str, condition: str | None = None, attrs: str = "") -> str:
    cond = f"<bpmn:conditionExpression>{condition}</bpmn:conditionExpression>" if condition else ""
    return f'<bpmn:sequenceFlow id="{flow_id}" sourceRef="Gate" targetRef="{target}"{attrs}>{cond}</bpmn:sequenceFlow>'


# --- the file itself ---

def test_a_file_with_a_doctype_is_refused_in_words():
    xml = f'<?xml version="1.0"?><!DOCTYPE d []><bpmn:definitions {NS}><bpmn:process id="P"/></bpmn:definitions>'
    error = refused(xml, "DOCTYPE")
    assert "Forbidden" not in str(error) and "defused" not in str(error).lower()


def test_xml_that_does_not_parse_is_refused_with_its_place_and_no_exception_name():
    error = refused("<definitions>\n<process></definitions>", "Not valid XML", "line 2")
    assert "ParseError" not in str(error) and "mismatched" in str(error)
    refused("", "Not valid XML")
    refused("   ", "Not valid XML")


def test_a_file_that_is_too_big_is_refused_before_it_is_read():
    xml = "<!--" + "x" * MAX_FILE_BYTES + "-->"
    refused(xml, "at most 2 MB")
    refused(xml.encode("utf-8"), "at most 2 MB")
    assert len(tiny()) < MAX_FILE_BYTES


@pytest.mark.parametrize("value", [None, 5, ["<a/>"], {"a": 1}, object(), 1.5])
def test_something_that_is_not_text_is_a_refusal_not_a_crash(value):
    with pytest.raises(ProcessLoadError, match="text"):
        load_process(value, catalog=CATALOG)


def test_text_that_cannot_be_stored_is_a_refusal_not_a_crash():
    with pytest.raises(ProcessLoadError, match="can't be stored"):
        load_process("<a>\ud800</a>", catalog=CATALOG)


def test_bytes_that_are_not_xml_are_a_refusal():
    for raw in (b"\xff\xfe\x00\x01", b"\x00" * 50, b"<?xml version='1.0' encoding='nope'?><a/>", b"\x89PNG\r\n"):
        with pytest.raises(ProcessLoadError):
            load_process(raw, catalog=CATALOG)


def test_a_thousand_nested_lanes_load_and_an_enormously_nested_file_is_a_refusal():
    depth = 1500
    opens = "".join(f'<bpmn:lane name="L{i}"><bpmn:childLaneSet>' for i in range(1, depth))
    closes = "</bpmn:childLaneSet></bpmn:lane>" * (depth - 1)
    inner = f'<bpmn:lane name="L{depth}"><bpmn:flowNodeRef>Task</bpmn:flowNodeRef></bpmn:lane>'
    lanes = f"<bpmn:laneSet>{opens}{inner}{closes}</bpmn:laneSet>"
    assert loads(tiny(f'<bpmn:userTask id="Task" {CARD}/>' + lanes)).elements["Task"].lane == f"L{depth}"
    with pytest.raises(ProcessLoadError):
        load_process("<a>" * 30000 + "</a>" * 30000, catalog=CATALOG)


def test_lanes_are_read_in_order_and_the_old_names_still_work():
    lane_set = ET.fromstring(
        '<laneSet><lane name="Outer"><flowNodeRef>A</flowNodeRef><childLaneSet><lane name="Inner">'
        '<flowNodeRef>A</flowNodeRef><flowNodeRef>B</flowNodeRef></lane></childLaneSet>'
        '<flowNodeRef>C</flowNodeRef></lane><lane name=""><flowNodeRef>D</flowNodeRef></lane></laneSet>')
    assert bpmn.lanes_of(lane_set) == {"A": "Inner", "B": "Inner", "C": "Outer"}
    assert bpmn._lanes_of is bpmn.lanes_of and bpmn._local is bpmn.local_name
    assert bpmn.local_name("{ns}name") == "name" and bpmn.local_name("name") == "name"
    assert bpmn.local_name(ET.Comment) == ""  # a comment's tag is a function, not text


# --- the unexpected is still a refusal ---

def test_anything_the_loader_did_not_foresee_is_a_refusal_naming_nothing_scary(monkeypatch):
    def boom(*_args, **_kwargs):
        raise KeyError("surprise")

    monkeypatch.setattr(loader, "_check_gateway", boom)
    with pytest.raises(ProcessLoadError, match=r"could not be checked \(KeyError\)") as caught:
        loads(gate(way("a", "End", "n = 1"), way("b", "End2", "n != 1")))
    assert isinstance(caught.value.__cause__, KeyError)


def test_a_firm_part_and_a_rule_that_are_each_fine_but_long_together_load():
    """The loader used to read "(firm) or (condition)" a second time as one text, which could pass 2,000 characters or
    fail to read; it now joins the two trees it already has."""
    low, high = 10 ** 13, 10 ** 14
    firm = " or ".join(f"n = {low + i}" for i in range(60))
    rule = " or ".join(f"n = {high + i}" for i in range(60))
    assert len(firm) + len(rule) > 2000 > max(len(firm), len(rule))
    gate_xml = gate(way("a", "End", rule, f' porter:changeable="true" porter:firm="{firm}"'), way("b", "End2"),
                    default="b")
    flow = loads(gate_xml).flows["a"]
    assert len(flow.effective) > 2000 and flow.tree.op == "or"
    assert feel.evaluate(flow.tree, {"n": low + 3}) is True       # the firm part holds
    assert feel.evaluate(flow.tree, {"n": high + 5}) is True      # the rule holds
    assert feel.evaluate(flow.tree, {"n": 500}) is False


def test_a_rule_that_is_a_zeebe_expression_beside_a_firm_part_loads():
    xml = gate(way("a", "End", "= n &gt; 5", ' porter:changeable="true" porter:firm="flag"'), way("b", "End2"),
               default="b")
    assert loads(xml).flows["a"].tree is not None


# --- flows ---

def test_a_flow_into_a_start_is_refused():
    flows = ('<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
             '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>'
             '<bpmn:sequenceFlow id="f3" sourceRef="Task" targetRef="Start"/>')
    refused(tiny(flows=flows), "goes into the start 'Start' (Start)", "nothing can come before a start")


def test_a_flow_into_a_boundary_event_is_refused():
    body = (f'<bpmn:userTask id="Task" {CARD}/><bpmn:userTask id="Other" {CARD}/>'
            '<bpmn:boundaryEvent id="B" name="Late" attachedToRef="Task"><bpmn:timerEventDefinition>'
            '<bpmn:timeDuration>PT1H</bpmn:timeDuration></bpmn:timerEventDefinition></bpmn:boundaryEvent>')
    flows = ('<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
             '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="Other"/>'
             '<bpmn:sequenceFlow id="f3" sourceRef="Other" targetRef="B"/>'
             '<bpmn:sequenceFlow id="f4" sourceRef="B" targetRef="End"/>')
    refused(tiny(body, flows), "goes into the boundary event 'Late' (B)", "reached only when the step")
    loads(tiny(body, flows.replace('sourceRef="Other" targetRef="B"', 'sourceRef="Other" targetRef="End"')))


def test_two_flows_with_one_id_are_refused():
    flows = ('<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
             '<bpmn:sequenceFlow id="f1" sourceRef="Task" targetRef="End"/>')
    refused(tiny(flows=flows), "The id 'f1' is used by two flows")


def test_a_flow_with_the_id_of_a_step_is_refused():
    flows = ('<bpmn:sequenceFlow id="Task" sourceRef="Start" targetRef="Task"/>'
             '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>')
    refused(tiny(flows=flows), "The id 'Task' is used by a flow and by the step 'Task' (Task)")


def test_a_flow_with_no_start_or_end_is_refused_in_words():
    flows = ('<bpmn:sequenceFlow id="f1" targetRef="Task"/><bpmn:sequenceFlow id="f2" sourceRef="Task"/>'
             '<bpmn:sequenceFlow id="f3" sourceRef="Task" targetRef="End"/>')
    refused(tiny(flows=flows), "The flow 'f1' has no start (sourceRef)", "The flow 'f2' has no end (targetRef)")


# --- timers ---

TIMER = '<bpmn:timerEventDefinition><bpmn:{kind}>{text}</bpmn:{kind}></bpmn:timerEventDefinition>'
BOUNDARY = '<bpmn:boundaryEvent id="B" name="Late" attachedToRef="Task">{}</bpmn:boundaryEvent>'
BOUNDARY_FLOWS = ('<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
                  '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>'
                  '<bpmn:sequenceFlow id="f3" sourceRef="B" targetRef="End"/>')
CATCH = '<bpmn:intermediateCatchEvent id="Task" name="Wait">{}</bpmn:intermediateCatchEvent>'


def with_boundary(timer: str) -> str:
    return tiny(f'<bpmn:userTask id="Task" {CARD}/>' + BOUNDARY.format(timer), BOUNDARY_FLOWS)


@pytest.mark.parametrize("where", ["boundary", "catch", "start"])
def test_a_date_that_does_not_read_is_refused_and_one_that_does_loads(where):
    def build(text: str) -> str:
        timer = TIMER.format(kind="timeDate", text=text)
        if where == "boundary":
            return with_boundary(timer)
        if where == "catch":
            return tiny(CATCH.format(timer))
        return tiny(start=timer)

    for good in ("2026-12-31T09:00:00", "2026-12-31T09:00:00Z", "2026-12-31T09:00:00+02:00", "2026-12-31"):
        loads(build(good))
    for bad in ("tomorrow", "2026-13-45T00:00:00", "31/12/2026", "2026-12-31T25:00:00"):
        refused(build(bad), f"'{bad}' is not a date and time like 2026-12-31T09:00:00")


@pytest.mark.parametrize("where", ["boundary", "catch"])
def test_a_repeating_timer_cannot_be_waited_on(where):
    timer = TIMER.format(kind="timeCycle", text="R/P1D")
    xml = with_boundary(timer) if where == "boundary" else tiny(CATCH.format(timer))
    refused(xml, "not a repeating timeCycle")


def test_a_repeating_timer_can_start_a_process_and_a_bad_period_cannot():
    loads(tiny(start=TIMER.format(kind="timeCycle", text="R/P1D")))
    loads(tiny(start=TIMER.format(kind="timeDuration", text="PT30M")))
    refused(tiny(start=TIMER.format(kind="timeDuration", text="soon")), "'soon' is not a period")


def test_a_period_has_limits_so_it_cannot_overflow_a_date():
    assert parse_duration("P3650D").days == 3650
    assert parse_duration("PT999999S").total_seconds() == 999999
    for text, fragment in [("PT999999999999H", "number too big"), ("P99999999999D", "number too big"),
                           ("P4000D", "longer than ten years"), ("P999999W", "longer than ten years"),
                           ("P1M", "months and years"), ("", "not a period"), (None, "not a period"),
                           ("PT", "not a period")]:
        with pytest.raises(ValueError, match=fragment) as caught:
            parse_duration(text)
        assert "None" not in str(caught.value)
    refused(with_boundary(TIMER.format(kind="timeDuration", text="PT999999999999H")), "number too big")
    refused(tiny(CATCH.format(TIMER.format(kind="timeDuration", text="P4000D"))), "longer than ten years")


# --- messages ---

def test_a_message_start_or_wait_must_name_its_message():
    named = '<bpmn:messageEventDefinition messageRef="m"/>'
    loads(tiny(start=named))
    loads(tiny(CATCH.format(named)))
    refused(tiny(start="<bpmn:messageEventDefinition/>"), "The start 'Start' (Start) waits on a message but names none")
    refused(tiny(CATCH.format("<bpmn:messageEventDefinition/>")), "The wait 'Wait' (Task) waits on a message but names")
    refused(tiny(CATCH.format('<bpmn:messageEventDefinition messageRef=""/>')), "names none")


# --- starts ---

def test_several_plain_starts_load_because_a_caller_names_the_one_it_wants():
    two = tiny().replace('<bpmn:startEvent id="Start"></bpmn:startEvent>',
                         '<bpmn:startEvent id="Start"/><bpmn:startEvent id="Start2"/>')
    two = two.replace("</bpmn:process>",
                      '<bpmn:sequenceFlow id="f9" sourceRef="Start2" targetRef="Task"/></bpmn:process>')
    defn = loads(two)
    assert defn.start_for(start_event="Start2").id == "Start2"
    with pytest.raises(StartNotFound) as caught:
        defn.start_for()
    assert isinstance(caught.value, KeyError)
    assert str(caught.value) == "an unnamed start: it has more than one, so say which"
    with pytest.raises(StartNotFound) as caught:
        defn.start_for(start_event="Nope")
    assert str(caught.value) == "the id 'Nope'"
    with pytest.raises(StartNotFound) as caught:
        defn.start_for(message="intake.requested")
    assert str(caught.value) == "the message 'intake.requested'"


# --- decisions ---

def test_a_decision_with_one_way_out_has_no_rule_for_the_household_to_change():
    one = tiny('<bpmn:exclusiveGateway id="Gate" name="Only"/>',
               flows='<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Gate"/>'
                     '<bpmn:sequenceFlow id="f2" sourceRef="Gate" targetRef="End"{}/>')
    loads(one.format(""))
    refused(one.format(' porter:changeable="true"'), "has one way out, so there is no rule for the household")
    refused(one.format(' porter:firm="flag"'), "has one way out, so there is no rule for the household")


# --- true or false attributes are read as the schema reads them ---

def test_a_changeable_flag_is_true_false_one_or_zero_and_nothing_else():
    def build(value: str) -> str:
        return gate(way("a", "End", "n &gt; 5", f' porter:changeable="{value}"'), way("b", "End2"), default="b")

    for value, expected in [("true", True), ("1", True), (" true ", True), ("false", False), ("0", False)]:
        assert loads(build(value)).flows["a"].changeable is expected, value
    for value in ("yes", "True", "TRUE", "on", "", "2"):
        refused(build(value), f"The flow 'a': porter:changeable should be true or false, not '{value}'")


def test_the_flags_on_loops_and_boundaries_are_read_the_same_way():
    call = ('<bpmn:callActivity id="Task" calledElement="x"><bpmn:multiInstanceLoopCharacteristics '
            'isSequential="{}" porter:collection="items" porter:elementVariable="it"/></bpmn:callActivity>')
    model_xml = tiny(call.format("1")).replace(MODEL, MODEL + '<porter:field path="items" type="list"/>')
    assert loads(model_xml).elements["Task"].multi.sequential is True
    zero = loads(model_xml.replace('isSequential="1"', 'isSequential="0"'))
    assert zero.elements["Task"].multi.sequential is False
    refused(model_xml.replace('isSequential="1"', 'isSequential="yes"'),
            "isSequential should be true or false, not 'yes'")

    timer = TIMER.format(kind="timeDuration", text="PT1H")
    boundary = BOUNDARY.format(timer)
    base = with_boundary(timer)
    assert boundary in base
    refused(base.replace('attachedToRef="Task"', 'attachedToRef="Task" cancelActivity="false"'), "doesn't interrupt")
    refused(base.replace('attachedToRef="Task"', 'attachedToRef="Task" cancelActivity="0"'), "doesn't interrupt")
    loads(base.replace('attachedToRef="Task"', 'attachedToRef="Task" cancelActivity="1"'))
    refused(base.replace('attachedToRef="Task"', 'attachedToRef="Task" cancelActivity="no"'),
            "cancelActivity should be true or false, not 'no'")


# --- a part of the process that cannot end ---

def test_a_loop_with_no_way_out_is_refused_and_a_loop_with_one_is_not():
    stuck = tiny(f'<bpmn:exclusiveGateway id="Gate" default="fd"/><bpmn:userTask id="A" {CARD}/>'
                 f'<bpmn:userTask id="B" name="Again" {CARD}/>',
                 flows='<bpmn:sequenceFlow id="f0" sourceRef="Start" targetRef="Gate"/>'
                       '<bpmn:sequenceFlow id="fa" sourceRef="Gate" targetRef="A"><bpmn:conditionExpression>n &gt; 5'
                       '</bpmn:conditionExpression></bpmn:sequenceFlow>'
                       '<bpmn:sequenceFlow id="fd" sourceRef="Gate" targetRef="End"/>'
                       '<bpmn:sequenceFlow id="f1" sourceRef="A" targetRef="B"/>'
                       '<bpmn:sequenceFlow id="f2" sourceRef="B" targetRef="A"/>')
    error = refused(stuck, "No path from the step 'A' (A)", "leads to an end")
    assert len(error.problems) == 1

    retry = stuck.replace('<bpmn:sequenceFlow id="f2" sourceRef="B" targetRef="A"/>',
                          '<bpmn:sequenceFlow id="f2" sourceRef="B" targetRef="Gate"/>')
    loads(retry)


def test_a_step_that_can_only_end_by_its_boundary_event_can_end():
    body = (f'<bpmn:userTask id="Task" {CARD}/><bpmn:exclusiveGateway id="Gate" default="back"/>'
            + BOUNDARY.format(TIMER.format(kind="timeDuration", text="PT1H")))
    flows = ('<bpmn:sequenceFlow id="f1" sourceRef="Start" targetRef="Task"/>'
             '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="Gate"/>'
             '<bpmn:sequenceFlow id="back" sourceRef="Gate" targetRef="Task"/>'
             '<bpmn:sequenceFlow id="more" sourceRef="Gate" targetRef="Task"><bpmn:conditionExpression>flag'
             '</bpmn:conditionExpression></bpmn:sequenceFlow>'
             '<bpmn:sequenceFlow id="f3" sourceRef="B" targetRef="End"/>')
    loads(tiny(body, flows))
    refused(tiny(body, flows.replace('sourceRef="B" targetRef="End"', 'sourceRef="B" targetRef="Task"')),
            "leads to an end")


def test_a_process_with_no_end_says_so_once():
    error = refused(tiny().replace('<bpmn:endEvent id="End"/>', "").replace(
        '<bpmn:sequenceFlow id="f2" sourceRef="Task" targetRef="End"/>', ""))
    assert [p for p in error.problems if "no end event" in p]
    assert not [p for p in error.problems if "leads to an end" in p]


# --- the names in the porter namespace ---

def test_a_misspelt_porter_attribute_is_refused_with_a_hint():
    refused(tiny('<bpmn:userTask id="Task" name="Ask" porter:card="pick_goal" porter:outptu="x"/>'),
            "'Ask' has porter:outptu, which the app doesn't read; did you mean porter:output?")
    refused(gate(way("a", "End", "n &gt; 5", ' porter:chaneable="true"'), way("b", "End2"), default="b"),
            "porter:chaneable, which the app doesn't read; did you mean porter:changeable?")
    refused(gate(way("a", "End", "n &gt; 5", ' porter:Firm="flag"'), way("b", "End2"), default="b"),
            "did you mean porter:firm?")
    refused(tiny(root_attrs=' porter:keys="x"'), "'Tiny' has porter:keys", "did you mean porter:key?")


def test_the_porter_namespace_words_about_a_step_are_always_accepted_and_change_no_behaviour():
    words = ' porter:engine="app.thing:fn" porter:harness="do.item" porter:calls="other"'
    plain = tiny(f'<bpmn:userTask id="Task" {CARD}/>')
    marked = tiny(f'<bpmn:userTask id="Task" {CARD}{words}/>')
    assert loads(marked).behaviour_hash == loads(plain).behaviour_hash
    configure(known_attributes=["something_else"])  # even when the app asks for every other name to be refused
    assert loads(marked).behaviour_hash == loads(plain).behaviour_hash


def test_an_attribute_the_app_reads_itself_is_not_the_engines_to_refuse_unless_the_app_lists_them():
    own = tiny(f'<bpmn:userTask id="Task" {CARD} porter:skill="gather" porter:ask="fact"/>')
    loads(own)  # the app has not said which names are its own: only misspellings are refused
    configure(known_attributes=["skill"])
    refused(own, "'Task' has porter:ask, which the app doesn't read")
    loads(own.replace(' porter:ask="fact"', ""))
    configure(known_attributes=["skill", "ask"])
    loads(own)
    # the engine's own are always allowed, and an app name that looks like one of them is not taken for a typo
    configure(known_attributes=["roles"])
    loads(tiny(f'<bpmn:userTask id="Task" {CARD} porter:roles="x"/>'))


def test_names_are_never_a_bare_string():
    for name in ("roles", "card_kinds", "known_attributes"):
        with pytest.raises(TypeError, match=rf"{name} should be a set or a list of names, not the text 'planner'"):
            configure(**{name: "planner"})
    configure(roles=lambda: "planner")
    with pytest.raises(TypeError, match="not the text 'planner'"):
        default_catalog()


def test_an_action_lists_its_inputs_and_outputs_as_names_not_one_string():
    async def fn(session, instance, data):
        return {}

    registry = ActionRegistry()
    for field in ("inputs", "outputs"):
        with pytest.raises(RegistryError, match=rf"{field} should be a list of names, not the text 'item'"):
            registry.register("a.b", fn, description="d", source="s", **{field: "item"})
        with pytest.raises(RegistryError, match="not the text"):
            registry_module.register_action("a.b", fn, description="d", source="s", **{field: "item"})
    assert registry.names() == [] and "a.b" not in registry_module.actions
    registry.register("a.b", fn, inputs=("item",), outputs=["verdict"], description="d", source="s")
    assert registry.get("a.b").inputs == ("item",)


# --- plain words ---

def test_messages_have_no_none_no_wrong_article_and_no_reprs():
    cases = {
        "an unattached boundary": tiny(BOUNDARY.format(TIMER.format(kind="timeDuration", text="PT1H"))
                                       .replace(' attachedToRef="Task"', ""), BOUNDARY_FLOWS),
        "an error end": tiny().replace('<bpmn:endEvent id="End"/>',
                                       '<bpmn:endEvent id="End"><bpmn:errorEventDefinition/></bpmn:endEvent>'),
        "an escalation start": tiny(start="<bpmn:escalationEventDefinition/>"),
        "an error wait": tiny(CATCH.format("<bpmn:errorEventDefinition/>")),
        "a plain boundary": with_boundary(""),
        "an unreadable condition": gate(way("a", "End", 'mode = "it\'s" ==='), way("b", "End2"), default="b"),
        "a plain wait": tiny(CATCH.format("")),
        "a subprocess": tiny('<bpmn:adHocSubProcess id="X" name="Odd"/>'),
    }
    said = {name: refused(xml) for name, xml in cases.items()}
    text = {name: " | ".join(error.problems) for name, error in said.items()}
    for name, message in text.items():
        assert "None" not in message, (name, message)
        assert "\\'" not in message and "\\\\" not in message, (name, message)
        for wrong in (" a error", " a escalation", " a adHoc", " a end ", " a unattached", " a event"):
            assert wrong not in message, (name, message)
    assert "isn't attached to any step" in text["an unattached boundary"]
    assert "is an error end" in text["an error end"]
    assert "starts on an escalation event" in text["an escalation start"]
    assert "waits on an error event" in text["an error wait"]
    assert "waits on a plain event" in text["a plain wait"]
    assert "is a plain boundary event" in text["a plain boundary"]
    assert "is an adHocSubProcess" in text["a subprocess"]
    assert "(mode = \"it's\" ===) can't be read" in text["an unreadable condition"]


# --- the set of owner versions is swapped in whole ---

def test_a_bad_set_of_versions_leaves_the_old_ones_in_place():
    defn = sample_library().get("intake")
    owner = uuid.uuid4()
    loader.set_owner_versions({owner: {"intake": [defn]}}, {owner: {"intake": defn.file_hash}})
    assert loader.owner_active("intake", owner) is defn
    with pytest.raises(AttributeError):
        loader.set_owner_versions({owner: {"intake": [None]}}, {owner: {"intake": "x"}})
    assert loader.owner_active("intake", owner) is defn
    assert loader.owner_of({"owner": str(owner)}, None) == owner
    loader.set_owner_versions({}, {})
    assert loader.owner_active("intake", owner) is None


def test_an_owner_candidate_that_is_not_text_is_skipped():
    configure(owner_candidates=lambda data, subject: [5, None, "not-a-uuid"])
    assert loader.owner_of({}, None) is None
