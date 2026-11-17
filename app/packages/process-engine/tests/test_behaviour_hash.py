"""`behaviour_hash` says what a process does, so a passed check can be kept across edits to words and lost on edits to
behaviour. Found in the 2026-10-08 review: it sorted a decision's ways out, but the engine takes the first condition
that holds in the order the file lists them, so two ways out swapped kept the proof and changed the decision.

Changing the hash scheme changed every process's hash, so every stored check reads "run it again" once."""

import pytest
from support import CATALOG, fixture_xml, sample_registry

from process_engine import feel
from process_engine.loader import load_process

NS = ('xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:porter="http://porter.local/bpmn/ext" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
MODEL = '<porter:field path="n" type="number"/><porter:field path="flag" type="boolean"/>'

#: Three ways out of one decision whose conditions overlap: n = 9 satisfies both `big` and `some`.
BIG = ('<bpmn:sequenceFlow id="big" name="Big" sourceRef="Gate" targetRef="Big_end"{extra}>'
       '<bpmn:conditionExpression>n &gt; 5</bpmn:conditionExpression></bpmn:sequenceFlow>')
SOME = ('<bpmn:sequenceFlow id="some" name="Some" sourceRef="Gate" targetRef="Some_end">'
        '<bpmn:conditionExpression>n &gt; 3</bpmn:conditionExpression></bpmn:sequenceFlow>')
REST = '<bpmn:sequenceFlow id="rest" name="Rest" sourceRef="Gate" targetRef="Rest_end"/>'
IN = '<bpmn:sequenceFlow id="in" sourceRef="Start" targetRef="Gate"/>'


def process(*ways: str, extra: str = "") -> str:
    """Start -> Gate -> one of three ends; `ways` are listed in the order given, `extra` goes on the `big` flow."""
    ext = f"<bpmn:extensionElements><porter:dataModel>{MODEL}</porter:dataModel></bpmn:extensionElements>"
    return (f'<bpmn:definitions {NS} id="D"><bpmn:process id="P" name="Tiny" porter:key="hash">{ext}'
            '<bpmn:startEvent id="Start"/><bpmn:exclusiveGateway id="Gate" name="Which way?" default="rest"/>'
            '<bpmn:endEvent id="Big_end"/><bpmn:endEvent id="Some_end"/><bpmn:endEvent id="Rest_end"/>'
            f'{IN}{"".join(w.replace("{extra}", extra) for w in ways)}</bpmn:process></bpmn:definitions>')


def defn_of(xml: str):
    return load_process(xml, catalog=CATALOG, registry=sample_registry())


def hash_of(xml: str) -> str:
    return defn_of(xml).behaviour_hash


def test_the_same_file_has_the_same_hash():
    assert hash_of(process(BIG, SOME, REST)) == hash_of(process(BIG, SOME, REST)) != ""


def test_swapping_two_ways_out_changes_the_hash_because_it_changes_the_decision():
    first, swapped = process(BIG, SOME, REST), process(SOME, BIG, REST)
    assert hash_of(first) != hash_of(swapped)
    # and it really is a different decision: the engine takes the first condition that holds, in the file's order
    for xml, expected in [(first, "big"), (swapped, "some")]:
        defn = defn_of(xml)
        taken = [f.id for f in defn.out_flows("Gate")
                 if not f.is_default and feel.evaluate_condition(f.tree, {"n": 9})[0]]
        assert taken[0] == expected


def test_where_the_default_is_listed_decides_nothing_so_it_keeps_the_hash():
    assert hash_of(process(BIG, SOME, REST)) == hash_of(process(REST, BIG, SOME)) == hash_of(process(BIG, REST, SOME))


def test_reordering_things_that_do_not_decide_anything_keeps_the_hash():
    plain = process(BIG, SOME, REST)
    flows_moved = plain.replace(IN, "").replace("</bpmn:process>", IN + "</bpmn:process>")
    assert flows_moved != plain and hash_of(flows_moved) == hash_of(plain)
    ends_moved = plain.replace('<bpmn:endEvent id="Big_end"/>', "").replace(
        "<bpmn:endEvent id=\"Some_end\"/>", '<bpmn:endEvent id="Some_end"/><bpmn:endEvent id="Big_end"/>')
    assert ends_moved != plain and hash_of(ends_moved) == hash_of(plain)


@pytest.mark.parametrize("edit", [
    lambda xml: xml.replace("Which way?", "Which way should it go?"),
    lambda xml: xml.replace('name="Big"', 'name="Large"'),
    lambda xml: xml.replace('<bpmn:startEvent id="Start"/>',
                            '<bpmn:startEvent id="Start"><bpmn:documentation>  Starts\n\n   here.  '
                            '</bpmn:documentation></bpmn:startEvent>'),
    lambda xml: xml.replace("><", ">\n        <"),
    lambda xml: xml.replace("><", ">\r\n\t  <"),
    lambda xml: xml.replace('name="Tiny"', 'name="  Tiny   process "'),
    lambda xml: xml.replace('default="rest"/>', 'default="rest"><bpmn:documentation>Words, words.'
                            '</bpmn:documentation></bpmn:exclusiveGateway>'),
    lambda xml: xml.replace('<bpmn:exclusiveGateway ', '<bpmn:exclusiveGateway porter:engine="app.thing:fn" '
                            'porter:harness="h.x" porter:calls="other" '),
    lambda xml: xml.replace('id="big"', 'porter:wording="more than five" porter:firmMeaning="never" id="big"'),
    lambda xml: xml.replace('id="big" name="Big"', 'id="big" name="Big" porter:changeable="true"'),
], ids=["gateway name", "flow name", "documentation with spaces", "white space between elements",
        "other line endings and tabs", "process name", "gateway documentation", "porter:engine, harness and calls",
        "wording", "changeable"])
def test_words_and_white_space_keep_the_hash(edit):
    base = process(BIG, SOME, REST)
    edited = edit(base)
    assert edited != base
    assert hash_of(edited) == hash_of(base)


def _default_moved(xml: str) -> str:
    """`some` becomes the default and `rest` takes a condition of its own."""
    xml = xml.replace(REST, REST.replace("/>", "><bpmn:conditionExpression>n &lt;= 3</bpmn:conditionExpression>"
                                         "</bpmn:sequenceFlow>"))
    xml = xml.replace("<bpmn:conditionExpression>n &gt; 3</bpmn:conditionExpression>", "")
    return xml.replace('default="rest"', 'default="some"')


def _targets_swapped(xml: str) -> str:
    return xml.replace('id="big" name="Big" sourceRef="Gate" targetRef="Big_end"',
                       'id="big" name="Big" sourceRef="Gate" targetRef="Some_end"').replace(
        'id="some" name="Some" sourceRef="Gate" targetRef="Some_end"',
        'id="some" name="Some" sourceRef="Gate" targetRef="Big_end"')


@pytest.mark.parametrize("edit", [
    lambda xml: xml.replace("n &gt; 5", "n &gt; 6"),
    _default_moved,
    _targets_swapped,
    lambda xml: xml.replace("Rest_end", "Rest_end2"),
    lambda xml: xml.replace('id="big" name="Big"',
                            'id="big" name="Big" porter:changeable="true" porter:firm="flag"'),
    lambda xml: xml.replace('porter:key="hash"', 'porter:key="other"'),
], ids=["condition", "default", "targets", "end renamed", "firm part", "key"])
def test_a_change_to_what_decides_changes_the_hash(edit):
    base = process(BIG, SOME, REST)
    edited = edit(base)
    assert edited != base
    assert hash_of(edited) != hash_of(base)


def test_the_firm_part_is_in_the_hash_and_changeable_alone_is_not():
    def hash_with(extra: str) -> str:
        return hash_of(process(BIG, SOME, REST, extra=extra))

    assert hash_with(' porter:changeable="true" porter:firm="flag"') != hash_with(
        ' porter:changeable="true" porter:firm="n = 1"') != hash_with(' porter:changeable="true"')
    assert hash_with(' porter:changeable="true"') == hash_with(' porter:changeable="false"') == hash_with("")


def test_the_samples_keep_their_hash_through_a_wording_edit_and_lose_it_on_a_condition_edit():
    registry = sample_registry()
    base = load_process(fixture_xml("intake.bpmn"), catalog=CATALOG, registry=registry).behaviour_hash
    reworded = fixture_xml("intake.bpmn").replace("Write the summary", "Write the short summary")
    assert load_process(reworded, catalog=CATALOG, registry=registry).behaviour_hash == base
    changed = fixture_xml("intake.bpmn").replace("count(plan.items) = 0", "count(plan.items) = 1")
    assert load_process(changed, catalog=CATALOG, registry=registry).behaviour_hash != base
