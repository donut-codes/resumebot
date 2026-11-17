"""The small part of a BPMN 2.0 reader the loader needs: element names, event-definition kinds, lanes.

Vendored from another project's BPMN service and moved here from the host app's own module, which now imports these
names from here. See `PROVENANCE.md` in the package root. Only what
the engine's loader reads is kept: the rest of that reader (the graph import and export for the process page) stays
with the app that draws processes.
"""

import re

#: A line that starts one of the specification's headings (process-view's `spec.ts` HEADINGS): an untagged
#: documentation carrying one is the step's specification, not its instructions.
SPEC_HEADINGS = re.compile(r"^(Actor|Trigger|Rules|Outputs|What the household sees|Source):", re.M)

#: Event definition child elements -> the flat trigger name.
TRIGGER_ELEMENTS = {
    "timerEventDefinition": "timer",
    "errorEventDefinition": "error",
    "escalationEventDefinition": "escalation",
    "messageEventDefinition": "message",
    "signalEventDefinition": "signal",
    "conditionalEventDefinition": "conditional",
    "compensateEventDefinition": "compensation",
    "terminateEventDefinition": "terminate",
    "cancelEventDefinition": "cancel",
    "linkEventDefinition": "link",
}

#: Elements that live inside <process> beside the flow. They are not modelled, which is not a reason to lose them:
#: an annotation an author wrote is evidence, and a lane says who does the work.
PROCESS_LEVEL_PASSTHROUGH = {
    "laneSet", "dataObject", "dataObjectReference", "dataStoreReference",
    "textAnnotation", "association", "group",
}

#: A timer holds exactly one of these, and they are three different facts: an elapsed period, a fixed instant, a
#: recurrence. The element name is the only thing that says which, so it is kept rather than discarded.
TIMER_SUBTYPES = {
    "timeDuration": "duration",
    "timeDate": "date",
    "timeCycle": "cycle",
}


def local_name(tag) -> str:
    """An element or attribute name without its namespace. A comment or instruction (whose tag is not text) has none."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def lanes_of(lane_set) -> dict[str, str]:
    """flow node id -> lane name, for every lane in a laneSet.

    Nested laneSets (a lane can contain its own) are walked, because a sub-lane still names a performer. A node listed
    by an inner lane and an outer one takes whichever is read last, as the file is read in order. The walk keeps its own
    stack, so a file that nests lanes a thousand deep is read like any other.
    """
    mapping: dict[str, str] = {}
    # Each frame is (what is left to read, the lane name its items belong to). A lane set's items are lanes (name None);
    # a lane's items are its flowNodeRefs and child lane sets.
    stack: list[tuple] = [(iter(lane_set), None)]
    while stack:
        items, lane_name = stack[-1]
        item = next(items, None)
        if item is None:
            stack.pop()
            continue
        tag = local_name(item.tag)
        if lane_name is None:
            if tag == "lane":
                stack.append((iter(item), (item.get("name") or "").strip()))
        elif tag == "flowNodeRef":
            node_id = (item.text or "").strip()
            if node_id and lane_name:
                mapping[node_id] = lane_name
        elif tag == "childLaneSet":
            stack.append((iter(item), None))
    return mapping


#: The names this module used before they were public; `app/process/bpmn.py` still imports them.
_local = local_name
_lanes_of = lanes_of


#: Where the upstream project writes what BPMN has no standard element for: who owns a step, which software and documents it
#: uses, which controls it satisfies. `extensionElements` is BPMN's own extension point, so any other tool reads the
#: file and ignores these. The engine never acts on them: it carries them for the view and for agents.
PA_NS = "http://processatlas.local/bpmn/extensions"


def documentation_of(element) -> dict[str, str | None]:
    """The words a step carries in its `<documentation>` elements, as the upstream project writes and reads them.

    Four are written on export, tagged by id so they can be told apart coming back in: `<step>_instructions`,
    `<step>_criteria` (how a decision is made), `<step>_commands` and `<step>_prompts`. An untagged one, which is
    what any other BPMN tool writes, is read as the instructions, since that is what a task's documentation means
    everywhere else. An untagged one that carries the step's specification (a line starting "Actor:", "Trigger:",
    "Rules:", "Outputs:", "What the household sees:" or "Source:", the headings process-view writes) is the
    specification, never the instructions: process-view reads it the same way (`hasSpecHeadings`).
    """
    found: dict[str, str | None] = {"instructions": None, "criteria": None, "commands": None, "prompts": None}
    for doc in element:
        if local_name(doc.tag) != "documentation" or not (doc.text or "").strip():
            continue
        doc_id = doc.get("id") or ""
        text = doc.text.strip()
        if doc_id.endswith("_criteria"):
            found["criteria"] = text
        elif doc_id.endswith("_commands"):
            found["commands"] = text
        elif doc_id.endswith("_prompts"):
            found["prompts"] = text
        elif doc_id.endswith("_instructions"):
            found["instructions"] = text
        elif found["instructions"] is None and not SPEC_HEADINGS.search(text):
            found["instructions"] = text
    return found


def extensions_of(element) -> dict:
    """What an export from the upstream project wrote into this step's `extensionElements` (namespace `PA_NS`): its owner, roles,
    people, software, documents and controls. Names, not ids: a file travels between installs and a parser has no
    database. Anything in another namespace is left alone (and kept in the file, which the engine stores whole)."""
    found: dict = {"owner": None, "roles": [], "people": [], "software": [], "documents": [], "controls": []}
    for ext in element:
        if local_name(ext.tag) != "extensionElements":
            continue
        for child in ext:
            if not child.tag.startswith(f"{{{PA_NS}}}"):
                continue
            kind = local_name(child.tag)
            name = (child.get("name") or "").strip()
            if kind == "owner" and name:
                found["owner"] = name
            elif kind == "role" and name:
                found["roles"].append(name)
            elif kind == "person" and name:
                found["people"].append(name)
            elif kind == "software" and name:
                found["software"].append(name)
            elif kind == "document" and name:
                found["documents"].append(name)
            elif kind == "control":
                reference = (child.get("reference") or "").strip()
                if reference:
                    found["controls"].append({
                        "reference": reference,
                        "framework": (child.get("framework") or "").strip() or None,
                    })
    return found
