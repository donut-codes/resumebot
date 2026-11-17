"""The imported step tags survive in the engine's reader: the tagged `<documentation>` and the extension
elements are accepted, carried on each step, left out of the behaviour hash, and never dropped."""

from support import CATALOG, sample_registry

from process_engine.loader import load_process

#: A file as the upstream project exports it (tagged documentation, `pa:` elements), with the bindings the engine runs.
EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:porter="http://porter.local/bpmn/ext"
    xmlns:pa="http://processatlas.local/bpmn/extensions"
    xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
    targetNamespace="http://processatlas.local/bpmn">
  <bpmn:process id="Onboard" name="Onboard a supplier" porter:key="onboard">
    <bpmn:extensionElements>
      <porter:dataModel>
        <porter:field path="item" type="object"/>
        <porter:field path="item.risk" type="string" enum="low,medium,high"/>
        <porter:field path="look" type="object"/>
        <porter:field path="look.verdict" type="string" enum="ok,bad"/>
      </porter:dataModel>
    </bpmn:extensionElements>
    <bpmn:startEvent id="Start" name="Start"/>
    <bpmn:serviceTask id="Look" name="Look at the supplier" porter:action="item.look" porter:output="look">
      <bpmn:documentation id="Look_instructions">Open the supplier file and read it.</bpmn:documentation>
      <bpmn:documentation id="Look_commands">grep -i licence supplier.txt</bpmn:documentation>
      <bpmn:documentation id="Look_prompts">Is the licence current?</bpmn:documentation>
      <bpmn:extensionElements>
        <pa:owner name="Procurement lead"/>
        <pa:role name="Buyer"/>
        <pa:role name="Reviewer"/>
        <pa:person name="Dana Reyes"/>
        <pa:software name="ERP"/>
        <pa:software name="Supplier portal"/>
        <pa:document name="Supplier policy"/>
        <pa:control reference="A.9.4" framework="ISO 27001"/>
        <pa:control reference="CC6.1"/>
        <pa:control framework="no reference, so skipped"/>
        <pa:future-thing name="an element this reader doesn't know"/>
        <camunda:other name="another tool's"/>
      </bpmn:extensionElements>
    </bpmn:serviceTask>
    <bpmn:exclusiveGateway id="Gw" name="Good enough?" default="F_no">
      <bpmn:documentation id="Gw_criteria">
        Good when the licence is current and the insurance covers us.
      </bpmn:documentation>
      <bpmn:documentation id="Gw_instructions">Decide from the verdict.</bpmn:documentation>
    </bpmn:exclusiveGateway>
    <bpmn:userTask id="Ask" name="Ask the owner" porter:card="pick_goal">
      <bpmn:documentation>An untagged note, which is read as the instructions.</bpmn:documentation>
    </bpmn:userTask>
    <bpmn:endEvent id="End" name="Done"/>
    <bpmn:sequenceFlow id="F1" sourceRef="Start" targetRef="Look"/>
    <bpmn:sequenceFlow id="F2" sourceRef="Look" targetRef="Gw"/>
    <bpmn:sequenceFlow id="F_yes" name="Yes" sourceRef="Gw" targetRef="End">
      <bpmn:documentation>It is.</bpmn:documentation>
      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">look.verdict = "ok"</bpmn:conditionExpression>
    </bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="F_no" name="No" sourceRef="Gw" targetRef="Ask"/>
    <bpmn:sequenceFlow id="F4" sourceRef="Ask" targetRef="End"/>
  </bpmn:process>
</bpmn:definitions>
"""


def load(xml: str = EXPORT):
    return load_process(xml, catalog=CATALOG, registry=sample_registry())


def test_a_process_wiki_export_loads_and_the_engine_still_runs_it_as_written():
    defn = load()
    assert defn.key == "onboard"
    assert defn.elements["Look"].action == "item.look"
    assert [f.id for f in defn.out_flows("Gw")] == ["F_yes", "F_no"]


def test_every_tag_is_on_the_step_it_was_written_on():
    tags = load().elements["Look"].tags
    assert tags.instructions == "Open the supplier file and read it."
    assert tags.commands == "grep -i licence supplier.txt"
    assert tags.prompts == "Is the licence current?"
    assert tags.decision_criteria is None
    assert tags.owner == "Procurement lead"
    assert tags.roles == ("Buyer", "Reviewer")
    assert tags.people == ("Dana Reyes",)
    assert tags.software == ("ERP", "Supplier portal")
    assert tags.documents == ("Supplier policy",)
    assert tags.controls == (
        {"reference": "A.9.4", "framework": "ISO 27001"}, {"reference": "CC6.1", "framework": None},
    )


def test_a_decisions_criteria_and_an_untagged_note_are_read_as_process_wiki_reads_them():
    defn = load()
    gate = defn.elements["Gw"].tags
    assert gate.decision_criteria == "Good when the licence is current and the insurance covers us."
    assert gate.instructions == "Decide from the verdict."
    assert defn.elements["Ask"].tags.instructions == "An untagged note, which is read as the instructions."


def test_a_step_without_tags_has_empty_tags():
    tags = load().elements["Start"].tags
    assert tags.empty
    assert tags.as_dict()["roles"] == []


def test_the_view_carries_the_tags_and_only_where_there_are_some():
    described = {s["id"]: s for s in load().describe()}
    assert described["Look"]["tags"]["owner"] == "Procurement lead"
    assert described["Look"]["tags"]["controls"][0] == {"reference": "A.9.4", "framework": "ISO 27001"}
    assert "tags" not in described["Start"]


def test_the_whole_file_is_kept_with_every_element_in_it():
    defn = load()
    for text in ("pa:owner", "pa:control", 'reference="CC6.1"', "pa:future-thing", "camunda:other", "Look_prompts"):
        assert text in defn.xml


def test_tags_are_words_so_changing_them_keeps_what_was_proved_about_the_file():
    plain = load()
    changed = load(EXPORT.replace("Procurement lead", "Head of buying").replace("ERP", "SAP")
                   .replace("Is the licence current?", "Is the licence still valid?"))
    assert plain.file_hash != changed.file_hash
    assert plain.behaviour_hash == changed.behaviour_hash
    assert changed.elements["Look"].tags.owner == "Head of buying"


def test_adding_every_tag_to_a_file_leaves_its_behaviour_hash_alone():
    """The shipped files are refreshed to carry the tags: no proof made against them may be lost by that."""
    from support import fixture_xml

    plain_xml = fixture_xml("check_one.bpmn")
    bare = '<bpmn:serviceTask id="Look" name="Look at it" porter:action="item.look" porter:output="look"/>'
    assert bare in plain_xml
    tagged_xml = plain_xml.replace(bare, """<bpmn:serviceTask id="Look" name="Look at it" porter:action="item.look"
        porter:output="look">
      <bpmn:documentation id="Look_instructions">Open the file.</bpmn:documentation>
      <bpmn:documentation id="Look_criteria">Bad when the licence has lapsed.</bpmn:documentation>
      <bpmn:documentation id="Look_commands">grep licence file.txt</bpmn:documentation>
      <bpmn:documentation id="Look_prompts">Is it current?</bpmn:documentation>
      <bpmn:extensionElements>
        <pa:owner xmlns:pa="http://processatlas.local/bpmn/extensions" name="Lead"/>
        <pa:role xmlns:pa="http://processatlas.local/bpmn/extensions" name="Buyer"/>
        <pa:person xmlns:pa="http://processatlas.local/bpmn/extensions" name="Dana"/>
        <pa:software xmlns:pa="http://processatlas.local/bpmn/extensions" name="ERP"/>
        <pa:document xmlns:pa="http://processatlas.local/bpmn/extensions" name="Policy"/>
        <pa:control xmlns:pa="http://processatlas.local/bpmn/extensions" reference="A.9.4" framework="ISO"/>
      </bpmn:extensionElements>
    </bpmn:serviceTask>""")
    registry = sample_registry()
    plain = load_process(plain_xml, catalog=CATALOG, registry=registry)
    tagged = load_process(tagged_xml, catalog=CATALOG, registry=registry)
    assert tagged.elements["Look"].tags.owner == "Lead"
    assert plain.file_hash != tagged.file_hash
    assert plain.behaviour_hash == tagged.behaviour_hash


def _docs(*docs: tuple[str | None, str]):
    """A task element with these `<documentation>` children, as (id or None, text)."""
    from xml.etree.ElementTree import Element, SubElement

    bpmn = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"
    task = Element(f"{bpmn}task", {"id": "T"})
    for doc_id, text in docs:
        doc = SubElement(task, f"{bpmn}documentation", {"id": doc_id} if doc_id else {})
        doc.text = text
    return task


def test_an_untagged_specification_is_never_read_as_the_instructions():
    from process_engine.bpmn import documentation_of

    spec = "Actor: Dana.\nTrigger: A call is due.\nSource: calls.py:ring"
    assert documentation_of(_docs((None, spec)))["instructions"] is None  # the words were cleared: none, not the spec
    assert documentation_of(_docs((None, spec), (None, "Call the supplier.")))["instructions"] == "Call the supplier."
    assert documentation_of(_docs((None, "Call the supplier."), (None, spec)))["instructions"] == "Call the supplier."
    tagged = _docs((None, spec), ("T_instructions", "Ring them."))
    assert documentation_of(tagged)["instructions"] == "Ring them."
    # Free text that only mentions a heading mid-line is still the instructions.
    assert documentation_of(_docs((None, "Ask who the Actor: is.")))["instructions"] == "Ask who the Actor: is."
