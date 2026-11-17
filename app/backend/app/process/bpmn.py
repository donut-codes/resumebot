"""BPMN 2.0 import.

Vendored from another project's BPMN import code (see app/PROVENANCE.md).
See PROVENANCE.md for what changed. In short: parsed with `defusedxml`
instead of stdlib `xml.etree.ElementTree` (this app parses inbound files from
untrusted sources, so entity expansion must be rejected, not silently
expanded); the upstream extension-namespace parsing for owner/role/
person/software/document/control references is dropped, since this app has no
such catalogs to resolve them against -- performer names (lanes, Camunda
`candidateGroups`/`assignee`) are still read and merged into a single
`performer` string on save.

`step_type` is the small semantic core the rest of the app reasons about,
and every BPMN element is mapped *down* to its nearest core type. What the
element actually was is kept in `bpmn_type`, and anything not modelled is
kept verbatim in `bpmn_attrs`, so nothing the file said is
lost. An element we do not recognise still imports -- as a task carrying its
real BPMN name.
"""

import xml.etree.ElementTree as ET  # only used to write back the pieces of a file already parsed with defusedxml
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree as DET
from process_engine.bpmn import PROCESS_LEVEL_PASSTHROUGH, TIMER_SUBTYPES, TRIGGER_ELEMENTS
from process_engine.bpmn import lanes_of as _lanes_of
from process_engine.bpmn import local_name as _local

from app.errors import DomainError
from app.process.models import StepType
from app.process.schemas import GraphEdgeInput, GraphSaveRequest, GraphStepInput

BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"

# BPMN element local name -> the core step type it maps down to.
ELEMENT_TO_STEP_TYPE: dict[str, StepType] = {
    "task": StepType.task,
    "userTask": StepType.task,
    "manualTask": StepType.task,
    "serviceTask": StepType.task,
    "scriptTask": StepType.task,
    "businessRuleTask": StepType.task,
    "sendTask": StepType.task,
    "receiveTask": StepType.task,
    "exclusiveGateway": StepType.gateway_exclusive,
    "inclusiveGateway": StepType.gateway_exclusive,
    "complexGateway": StepType.gateway_exclusive,
    "eventBasedGateway": StepType.gateway_exclusive,
    "parallelGateway": StepType.gateway_parallel,
    "startEvent": StepType.event_start,
    "endEvent": StepType.event_end,
    "intermediateCatchEvent": StepType.event_intermediate,
    "intermediateThrowEvent": StepType.event_intermediate,
    "boundaryEvent": StepType.event_boundary,
    "subProcess": StepType.subprocess,
    "transaction": StepType.subprocess,
    "adHocSubProcess": StepType.subprocess,
    "callActivity": StepType.subprocess,
}

FLOW_ELEMENTS = {"sequenceFlow"}
# Elements that describe the diagram, not the process -- regenerated on export.
IGNORED_ELEMENTS = {"extensionElements", "incoming", "outgoing", "documentation"}


class BpmnParseError(DomainError):
    """The file is not BPMN we can read. Always names the offending part."""


#: The attribute that identifies what an event definition points at. Taking
#: "the first attribute" instead picked up `id` on any element written with
#: id first, so an errorEventDefinition could report its own id as its code.
TRIGGER_REF_ATTRS = (
    "errorRef", "escalationRef", "messageRef", "signalRef", "activityRef", "name",
)


def _trigger_of(element) -> tuple[str | None, str | None, str | None]:
    """Reads the event definition child, if any, into (trigger, detail, subtype)."""
    for child in element:
        name = _local(child.tag)
        kind = TRIGGER_ELEMENTS.get(name)
        if not kind:
            continue
        detail = None
        subtype = None
        # Prefer a child that names a timer kind over whichever happens to come
        # first: a timer with a stray sibling element was order-dependent.
        for grandchild in child:
            local = _local(grandchild.tag)
            text = (grandchild.text or "").strip()
            if not text:
                continue
            if local in TIMER_SUBTYPES:
                detail, subtype = text, TIMER_SUBTYPES[local]
                break
            if detail is None:
                detail = text
        if detail is None:
            # A reference rather than a literal: look it up by name instead of
            # taking whatever attribute the file happened to write first.
            for attr in TRIGGER_REF_ATTRS:
                if child.get(attr):
                    detail = child.get(attr)
                    break
        return kind, detail, subtype
    return None, None, None


#: Camunda states who does a task on the element itself. `candidateGroups`
#: and `candidateUsers` name a group or user list; `assignee` is meant to be
#: a specific user, but modellers routinely put a group slug there. All three
#: just become performer names -- this app has no role/user catalog to match
#: them against.
CAMUNDA_ROLE_ATTRS = ("candidateGroups", "candidateUsers", "assignee")


def normalise_performer(name: str) -> str:
    """The key two spellings of one performer agree on.

    A lane says "AP Clerk" and the Camunda attribute on the same task says
    "ap-clerk" -- one performer, written twice. Matching on case alone kept
    them apart, so separators are normalised away too.
    """
    return " ".join(name.replace("-", " ").replace("_", " ").lower().split())


def _performers_of(element) -> list[str]:
    """Names this element says are responsible, in the file's own words."""
    found: list[str] = []
    for key, value in element.attrib.items():
        if _local(key) not in CAMUNDA_ROLE_ATTRS:
            continue
        for part in (value or "").split(","):
            part = part.strip()
            # An unresolved expression names nobody.
            if part and not part.startswith("${") and part not in found:
                found.append(part)
    return found


def _unmodelled_attrs(element, consumed: set[str]) -> dict | None:
    """Everything on the element we did not map, so export can put it back."""
    extras = {k: v for k, v in element.attrib.items() if k not in consumed}
    children = [
        ET.tostring(child, encoding="unicode")
        for child in element
        if _local(child.tag) not in IGNORED_ELEMENTS
    ]
    if children:
        extras["__children__"] = children
    return extras or None


def parse_bpmn(xml: str | bytes) -> GraphSaveRequest:
    """BPMN XML -> a graph save payload. Raises BpmnParseError on unusable
    input, including entity-expansion ("billion laughs") payloads, which
    defusedxml rejects rather than expands."""
    try:
        root = DET.fromstring(xml)
    except ParseError as exc:
        raise BpmnParseError(f"Not valid XML: {exc}") from exc
    except Exception as exc:  # defusedxml raises its own exception types
        raise BpmnParseError(f"Rejected while parsing: {exc}") from exc

    if _local(root.tag) != "definitions":
        raise BpmnParseError(
            f"Expected a <bpmn:definitions> root, found <{_local(root.tag)}>"
        )

    processes = [child for child in root if _local(child.tag) == "process"]
    if not processes:
        raise BpmnParseError(
            "No <bpmn:process> found. Collaboration-only files (pools without an "
            "expanded process) are not supported yet."
        )

    # Diagram coordinates, so an imported model keeps the author's layout.
    positions: dict[str, tuple[float, float]] = {}
    for shape in root.iter(f"{{{BPMNDI_NS}}}BPMNShape"):
        ref = shape.get("bpmnElement")
        bounds = shape.find(f"{{{DC_NS}}}Bounds")
        if ref and bounds is not None:
            try:
                positions[ref] = (float(bounds.get("x", 0)), float(bounds.get("y", 0)))
            except ValueError:
                continue

    steps: list[GraphStepInput] = []
    edges: list[GraphEdgeInput] = []
    seen_ids: set[str] = set()
    # Kept verbatim so an export returns the file that came in, and read for
    # the one thing inside them we do model: who does the work.
    process_extras: list[str] = []
    lane_of: dict[str, str] = {}
    # Default flows are declared on the gateway, not the flow itself.
    default_flow_ids: set[str] = set()

    for process in processes:
        for element in process:
            name = _local(element.tag)
            if name in IGNORED_ELEMENTS or name in PROCESS_LEVEL_PASSTHROUGH:
                # Not a flow node, but not ours to throw away either: kept
                # verbatim so an export returns the file that came in.
                process_extras.append(ET.tostring(element, encoding="unicode"))
                if name == "laneSet":
                    lane_of.update(_lanes_of(element))
                continue

            element_id = element.get("id")
            if not element_id:
                continue

            if name in FLOW_ELEMENTS:
                source, target = element.get("sourceRef"), element.get("targetRef")
                if not source or not target:
                    continue
                condition = None
                for child in element:
                    if _local(child.tag) == "conditionExpression":
                        condition = (child.text or "").strip() or None
                edges.append(GraphEdgeInput(
                    source_temp_id=source,
                    target_temp_id=target,
                    label=element.get("name") or None,
                    condition=condition,
                    bpmn_type=f"bpmn:{name}",
                    bpmn_attrs=_unmodelled_attrs(
                        element, {"sourceRef", "targetRef", "name"}
                    ),
                ))
                continue

            if element_id in seen_ids:
                continue
            seen_ids.add(element_id)

            default_ref = element.get("default")
            if default_ref:
                default_flow_ids.add(default_ref)

            step_type = ELEMENT_TO_STEP_TYPE.get(name, StepType.task)
            trigger, trigger_detail, trigger_subtype = _trigger_of(element)
            x, y = positions.get(element_id, (0.0, len(steps) * 120.0))
            label = element.get("name") or None

            # A lane names the performer; Camunda names it on the element.
            # Both are the file saying who is responsible, in its own words.
            performers = _performers_of(element)
            lane = lane_of.get(element_id)
            if lane:
                performers.insert(0, lane)
            # One entry per spelling. The lane name goes first and wins,
            # because it is what a person wrote on the diagram; the slug
            # beside it is the same performer as the modelling tool stores it.
            deduped: list[str] = []
            seen_keys: set[str] = set()
            for candidate in performers:
                key = normalise_performer(candidate)
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    deduped.append(candidate)
            performers = deduped

            # Two <documentation> elements are written on export, tagged by
            # id so they can be told apart coming back in. An untagged one --
            # which is what any other BPMN tool writes -- is read as the
            # instruction, since that is what a task's documentation means
            # everywhere else.
            instructions = None
            criteria = None
            commands = None
            prompts = None
            for doc in element:
                if _local(doc.tag) != "documentation" or not (doc.text or "").strip():
                    continue
                doc_id = doc.get("id") or ""
                if doc_id.endswith("_criteria"):
                    criteria = doc.text.strip()
                elif doc_id.endswith("_commands"):
                    commands = doc.text.strip()
                elif doc_id.endswith("_prompts"):
                    prompts = doc.text.strip()
                elif doc_id.endswith("_instructions") or instructions is None:
                    instructions = doc.text.strip()

            steps.append(GraphStepInput(
                temp_id=element_id,
                instructions=instructions,
                commands=commands,
                prompts=prompts,
                decision_criteria=criteria,
                performer_names=performers,
                step_type=step_type,
                # `description` is required and carries the readable title.
                description=label or name,
                position_x=x,
                position_y=y,
                attached_to_temp_id=element.get("attachedToRef"),
                # BPMN's cancelActivity defaults to true when absent.
                interrupting=element.get("cancelActivity", "true") != "false",
                event_trigger=trigger,
                event_trigger_detail=trigger_detail,
                event_trigger_subtype=trigger_subtype,
                bpmn_type=f"bpmn:{name}",
                bpmn_attrs=_unmodelled_attrs(
                    element,
                    {"name", "attachedToRef", "cancelActivity", "default"},
                ),
            ))

    if not steps:
        raise BpmnParseError("The process contains no flow elements we can import.")

    step_ids = {s.temp_id for s in steps}
    # Edges pointing at something we skipped (a data object, an unexpanded pool)
    # would otherwise dangle.
    edges = [e for e in edges if e.source_temp_id in step_ids and e.target_temp_id in step_ids]

    for edge in edges:
        flow_id = (edge.bpmn_attrs or {}).get("id")
        if flow_id and flow_id in default_flow_ids:
            edge.is_default = True

    return GraphSaveRequest(steps=steps, edges=edges, process_extras=process_extras or None)
