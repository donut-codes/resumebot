"""Load a BPMN 2.0 file into an executable model (requirements 15.1, 15.2, 15.3).

Everything the engine will do is checked here, once, so a file either runs as written or is refused with a
plain message naming the element: an element outside the supported subset, a gateway that isn't exclusive, a
step naming an action, role, instruction file, response template or card that doesn't exist, a condition that
reads a field the data model doesn't declare, a way out with no condition and no default, a part of the process
that can never come to an end. `load_process` raises `ProcessLoadError` and nothing else, whatever it is given.

The file is read with the vendored reader's safe XML parser and helpers (`process_engine.bpmn`), not its graph
importer: the page's graph keeps what a picture needs; execution keeps `porter:` attributes, conditions,
defaults, multi-instance markers and boundary links.

The app's extension attributes (namespace `http://porter.local/bpmn/ext`, prefix `porter`):
- service task: `porter:action`, optional `porter:output` (a variable that also receives the result);
- agent task (a plain `bpmn:task`): `porter:role`, `porter:instructions`, `porter:template`, `porter:output`;
- user task: `porter:card`, `porter:output`;
- call activity: `calledElement`, `porter:in` (`childVar=parent.path, ...`), `porter:out` (`parentVar=child.path`),
  and a `multiInstanceLoopCharacteristics` with `porter:collection` and `porter:elementVariable`;
- a decision's way out: `porter:changeable="true"` (the household may rewrite its condition in plain words, 15.4),
  `porter:firm` (a FEEL condition no rule can change: where it holds the work takes this way out whatever the rule
  says, so a safeguard never moves; the engine evaluates `(firm) or (condition)`), `porter:firmMeaning` (its plain
  words) and `porter:wording` (the household's own words for the rule, when it wrote it);
- the process: `porter:key` (else its id) and a `porter:dataModel` of `porter:field path= type= enum=` in its
  `extensionElements`.

A true/false attribute (`porter:changeable`, `isSequential`, `cancelActivity`) is read as the XML schema reads it:
`true`, `false`, `1` or `0`. Anything else is refused, not taken for "no". A `porter:` name the engine doesn't read is
refused when it looks like a misspelling of one it does (`porter:acton`), so a typo can't quietly drop an action or a
safeguard. `porter:engine`, `porter:harness` and `porter:calls` (see `DOCUMENTATION_ATTRIBUTES`) are the app's own words
about a step: always accepted, ignored by the engine, and not part of `behaviour_hash`. `config.known_attributes` lists
the names an app reads itself, and with it set every other name is refused.
"""

import difflib
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime
from xml.etree.ElementTree import ParseError  # noqa: S405 - only the exception type is used

import defusedxml.ElementTree as DET
from defusedxml import DefusedXmlException, DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden

from process_engine import feel
from process_engine.bpmn import (
    PROCESS_LEVEL_PASSTHROUGH,
    TIMER_SUBTYPES,
    TRIGGER_ELEMENTS,
    documentation_of,
    extensions_of,
    lanes_of,
    local_name,
)
from process_engine.config import config
from process_engine.datamodel import DataModel, Field
from process_engine.errors import ConditionError, ProcessLoadError
from process_engine.model import (
    AGENT,
    BOUNDARY,
    CALL,
    CATCH,
    END,
    GATEWAY,
    SERVICE,
    START,
    USER,
    WAITING_KINDS,
    Element,
    Flow,
    MultiInstance,
    ProcessDef,
    StepTags,
    Trigger,
    parse_duration,
)
from process_engine.registry import ActionRegistry, Catalog, actions, default_catalog

log = logging.getLogger(__name__)

PORTER_NS = "http://porter.local/bpmn/ext"

#: The biggest file `load_process` reads (the biggest shipped one is about 120 KB). A bigger one is refused before it
#: is parsed.
MAX_FILE_BYTES = 2 * 1024 * 1024

#: The `porter:` attributes the engine reads. Anything else in the namespace is the app's own, or a typo.
OWN_ATTRIBUTES = frozenset({
    "action", "output", "role", "instructions", "template", "card", "in", "out", "collection", "elementVariable",
    "changeable", "firm", "firmMeaning", "wording", "key",
})

#: `porter:` attributes that the app's shipped files carry for the app's own reading, never the engine's: the code a
#: step stands for (`engine`), its place in the task path (`harness`), the sub-process a step describes in place
#: (`calls`). They are always accepted, the engine ignores them, and they are left out of `behaviour_hash` (they are
#: words about the file, not behaviour). An app with names of its own lists them in `config.known_attributes`.
DOCUMENTATION_ATTRIBUTES = frozenset({"engine", "harness", "calls"})

_NODE_KINDS = {
    "startEvent": START, "endEvent": END, "serviceTask": SERVICE, "task": AGENT, "userTask": USER,
    "callActivity": CALL, "exclusiveGateway": GATEWAY, "boundaryEvent": BOUNDARY,
    "intermediateCatchEvent": CATCH,
}
_UNSUPPORTED_WHY = {
    "parallelGateway": "only exclusive gateways are supported",
    "inclusiveGateway": "only exclusive gateways are supported",
    "eventBasedGateway": "only exclusive gateways are supported",
    "complexGateway": "only exclusive gateways are supported",
    "subProcess": "put the shared work in its own process and use a call activity",
    "transaction": "put the shared work in its own process and use a call activity",
    "adHocSubProcess": "put the shared work in its own process and use a call activity",
    "scriptTask": "a step names a registered action instead of carrying a script",
    "businessRuleTask": "a decision is a gateway with conditions",
    "manualTask": "use a user task (a household card) or an agent task",
    "sendTask": "use a service task naming an action",
    "receiveTask": "use an intermediate message catch event",
    "intermediateThrowEvent": "throwing events aren't supported",
}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


# --- small helpers ------------------------------------------------------------------------------------------

def _a(word: str) -> str:
    """`word` with the right article: "a start", "an end", "an error"."""
    return f"{'an' if word[:1].lower() in tuple('aeiou') else 'a'} {word}"


def _lower_first(text: str) -> str:
    """"The step 'Look' (L)" -> "the step 'Look' (L)": for a sentence that carries a name mid-way."""
    return text[:1].lower() + text[1:]


def _kind_word(trigger_type: str) -> str:
    return "plain" if trigger_type == "none" else trigger_type


def _p(el, name: str) -> str | None:
    value = el.get(f"{{{PORTER_NS}}}{name}")
    return value.strip() if value and value.strip() else None


def _xsd_bool(value: str | None, where: str, what: str, problems: list[str], default: bool = False) -> bool:
    """An attribute of the XML schema's boolean type: `true`, `false`, `1` or `0` (spaces around it don't count). A
    missing attribute is `default`. Anything else ("yes", "True") is a problem, and counts as `default`, so one slip
    is reported once instead of quietly changing what the file does."""
    if value is None:
        return default
    text = value.strip()
    if text in ("true", "1"):
        return True
    if text in ("false", "0"):
        return False
    problems.append(f"{where}: {what} should be true or false, not '{value}'")
    return default


def _doc(el) -> str | None:
    for child in el:
        if local_name(child.tag) == "documentation" and (child.text or "").strip():
            return child.text.strip()
    return None


def _tags(el) -> StepTags:
    """The upstream project's tagged documentation and extension elements on a step (see `StepTags`)."""
    docs, ext = documentation_of(el), extensions_of(el)
    return StepTags(
        instructions=docs["instructions"], decision_criteria=docs["criteria"], commands=docs["commands"],
        prompts=docs["prompts"], owner=ext["owner"], roles=tuple(ext["roles"]), people=tuple(ext["people"]),
        software=tuple(ext["software"]), documents=tuple(ext["documents"]),
        controls=tuple(ext["controls"]),
    )


def _parse_map(text: str | None, where: str, problems: list[str]) -> tuple[tuple[str, str], ...]:
    pairs = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        target, _, source = part.partition("=")
        target, source = target.strip(), source.strip()
        if not _NAME.match(target) or not _PATH.match(source):
            problems.append(f"{where}: '{part}' should read name=path")
            continue
        pairs.append((target, source))
    return tuple(pairs)


def _data_model_of(process, problems: list[str]) -> DataModel:
    fields: list[Field] = []
    for ext in process:
        if local_name(ext.tag) != "extensionElements":
            continue
        for model in ext:
            if model.tag != f"{{{PORTER_NS}}}dataModel":
                continue
            for item in model:
                path = item.get("path") or ""
                enum = tuple(v.strip() for v in (item.get("enum") or "").split(",") if v.strip())
                fields.append(Field(path, item.get("type") or "any", enum,
                                    item.get("description") or (item.text or "").strip()))
    try:
        return DataModel(fields)
    except ProcessLoadError as exc:
        problems.extend(exc.problems)
        return DataModel()


def _trigger(el, who: str, messages: dict, errors: dict, problems: list[str]) -> Trigger:
    defs = [c for c in el if local_name(c.tag) in TRIGGER_ELEMENTS]
    if not defs:
        return Trigger("none")
    if len(defs) > 1:
        problems.append(f"{who} has more than one event definition")
        return Trigger("none")
    child = defs[0]
    kind = TRIGGER_ELEMENTS[local_name(child.tag)]
    if kind == "message":
        ref = child.get("messageRef")
        return Trigger("message", message=messages.get(ref, ref))
    if kind == "error":
        ref = child.get("errorRef")
        return Trigger("error", error_code=errors.get(ref, ref) if ref else None)
    if kind == "timer":
        for grand in child:
            sub = TIMER_SUBTYPES.get(local_name(grand.tag))
            if sub and (grand.text or "").strip():
                return Trigger("timer", timer_kind=sub, timer_text=grand.text.strip())
        problems.append(f"{who} is a timer with no time on it")
        return Trigger("timer")
    return Trigger(kind)  # an event kind the checks below refuse by name


def _multi(el, who: str, problems: list[str]) -> MultiInstance | None:
    for child in el:
        name = local_name(child.tag)
        if name == "standardLoopCharacteristics":
            problems.append(f"{who} loops; only 'once per item' multi-instance is supported")
        elif name == "multiInstanceLoopCharacteristics":
            collection, variable = _p(child, "collection"), _p(child, "elementVariable")
            if not collection or not _PATH.match(collection):
                problems.append(f"{who} is multi-instance but names no list (porter:collection)")
            if not variable or not _NAME.match(variable):
                problems.append(f"{who} is multi-instance but names no item variable (porter:elementVariable)")
            sequential = _xsd_bool(child.get("isSequential"), who, "isSequential", problems)
            if not collection or not variable:
                return None
            return MultiInstance(sequential, collection, variable)
    return None


def _check_timer(el: Element, who: str, problems: list[str]) -> None:
    """A timer's time must read: a period (`timeDuration`) or a date and time (`timeDate`). A repeating `timeCycle` can
    start a process, but a step or a wait can't wait on one (the engine would have no moment to wake at)."""
    trigger = el.trigger
    if trigger.type != "timer" or trigger.timer_kind is None:  # a timer with no time was reported already
        return
    text = (trigger.timer_text or "").strip()
    if trigger.timer_kind == "duration":
        try:
            parse_duration(text)
        except ValueError as exc:
            problems.append(f"{who}: {exc}")
    elif trigger.timer_kind == "date":
        try:
            datetime.fromisoformat(text)
        except ValueError:
            problems.append(f"{who}: '{text}' is not a date and time like 2026-12-31T09:00:00")
    elif el.kind in (CATCH, BOUNDARY):
        problems.append(f"{who}: a timer here takes a period (timeDuration) or a date (timeDate), "
                        "not a repeating timeCycle")


def _near_miss(short: str) -> str | None:
    """The engine's attribute that `short` is probably a misspelling of (same letters in other capitals, or close)."""
    lowered = {name.lower(): name for name in OWN_ATTRIBUTES}
    if short.lower() in lowered:
        return lowered[short.lower()]
    close = difflib.get_close_matches(short.lower(), list(lowered), n=1, cutoff=0.8)
    return lowered[close[0]] if close else None


def _check_attributes(root, problems: list[str]) -> None:
    """Refuse a `porter:` attribute nobody reads: a misspelling of one the engine reads, or (when the app lists the
    names it reads itself in `config.known_attributes`) any other."""
    app_names = config.known_attributes
    allowed = OWN_ATTRIBUTES | DOCUMENTATION_ATTRIBUTES | frozenset(app_names or ())
    prefix = f"{{{PORTER_NS}}}"
    for node in root.iter():
        for attribute in node.attrib:
            if not attribute.startswith(prefix):
                continue
            short = attribute[len(prefix):]
            if short in allowed:
                continue
            near = _near_miss(short)
            if near is None and app_names is None:
                continue  # the app's own, and it hasn't said which: not ours to refuse
            ident = node.get("name") or node.get("id")
            where = f"'{ident}'" if ident else f"A <{local_name(node.tag)}>"
            hint = f"; did you mean porter:{near}?" if near else ""
            problems.append(f"{where} has porter:{short}, which the app doesn't read{hint}")


def _bytes_of(xml) -> bytes:
    """The file as bytes, or a plain refusal for something that isn't a file or is too big."""
    if isinstance(xml, str):
        try:
            raw = xml.encode("utf-8")
        except UnicodeEncodeError:
            raise ProcessLoadError("The file holds a character that can't be stored (half of a pair)") from None
    elif isinstance(xml, bytes | bytearray | memoryview):
        raw = bytes(xml)
    else:
        raise ProcessLoadError("A process file is text (or bytes); this is neither")
    if len(raw) > MAX_FILE_BYTES:
        raise ProcessLoadError(f"The file is {len(raw) / 1_048_576:.1f} MB; "
                               f"a process file can be at most {MAX_FILE_BYTES // 1_048_576} MB")
    return raw


def _read_xml(raw: bytes):
    """The file's root element. The reader refuses a DOCTYPE outright (so no entity can be declared, expanded or
    fetched), and every refusal says in words what the file did."""
    try:
        return DET.fromstring(raw, forbid_dtd=True)
    except DTDForbidden:
        raise ProcessLoadError(
            "The file has a DOCTYPE declaration; a process file may not have one (it can be used to make the "
            "reader expand or fetch things)") from None
    except EntitiesForbidden:
        raise ProcessLoadError("The file declares XML entities, which a process file may not do") from None
    except ExternalReferenceForbidden:
        raise ProcessLoadError("The file points at something outside itself, which it may not do") from None
    except DefusedXmlException:
        raise ProcessLoadError("The file uses an XML feature a process file may not use") from None
    except ParseError as exc:
        line, column = getattr(exc, "position", (None, None))
        reason = re.sub(r": line \d+, column \d+$", "", str(exc)) or "it can't be read"
        where = f" (line {line}, column {column})" if line else ""
        raise ProcessLoadError(f"Not valid XML: {reason}{where}") from None
    except (ValueError, LookupError, OverflowError):
        raise ProcessLoadError("Not valid XML: the reader could not make sense of it") from None


def load_process(
    xml: str | bytes, *, key: str | None = None, catalog: Catalog | None = None,
    registry: ActionRegistry | None = None, data_model: DataModel | None = None,
) -> ProcessDef:
    """BPMN XML -> `ProcessDef`. Raises `ProcessLoadError` listing every problem found, and nothing else, whatever
    the input: anything unforeseen is turned into a refusal too, with the cause kept and logged. `data_model` adds
    fields declared beside the file; the file's own `porter:dataModel` is read too."""
    try:
        return _load(xml, key=key, catalog=catalog, registry=registry, data_model=data_model)
    except ProcessLoadError:
        raise
    except RecursionError as exc:
        raise ProcessLoadError("The file is nested too deeply to check") from exc
    except Exception as exc:  # noqa: BLE001 - the one backstop: a file that can't be loaded is refused, never a crash
        log.warning("a process file crashed the loader", exc_info=True)
        raise ProcessLoadError(
            f"The file could not be checked ({type(exc).__name__}), so it can't be run; "
            "it is probably not a process file") from exc


def _load(
    xml, *, key: str | None, catalog: Catalog | None, registry: ActionRegistry | None, data_model: DataModel | None,
) -> ProcessDef:
    catalog = catalog or default_catalog()
    registry = registry or actions
    raw = _bytes_of(xml)
    root = _read_xml(raw)
    if local_name(root.tag) != "definitions":
        raise ProcessLoadError(f"Expected a <bpmn:definitions> root, found <{local_name(root.tag)}>")

    problems: list[str] = []
    _check_attributes(root, problems)
    messages = {c.get("id"): (c.get("name") or c.get("id")) for c in root if local_name(c.tag) == "message"}
    errors = {c.get("id"): (c.get("errorCode") or c.get("name") or c.get("id"))
              for c in root if local_name(c.tag) == "error"}
    processes = [c for c in root if local_name(c.tag) == "process"]
    if len(processes) != 1:
        raise ProcessLoadError(f"A file holds one process; this one has {len(processes)}")
    process = processes[0]

    model = _data_model_of(process, problems)
    if data_model is not None:
        try:
            model = model.merged(data_model)
        except ProcessLoadError as exc:
            problems.extend(exc.problems)

    lanes: dict[str, str] = {}
    elements: dict[str, Element] = {}
    flows: dict[str, Flow] = {}
    default_attr: dict[str, str] = {}

    for node in process:
        tag = local_name(node.tag)
        if tag in ("documentation", "extensionElements", "incoming", "outgoing") or tag in PROCESS_LEVEL_PASSTHROUGH:
            if tag == "laneSet":
                lanes.update(lanes_of(node))
            continue
        node_id = node.get("id")
        if not node_id:
            problems.append(f"An element <{tag}> has no id")
            continue
        label = node.get("name") or node_id
        if tag == "sequenceFlow":
            if node_id in flows:
                problems.append(f"The id '{node_id}' is used by two flows")
                continue
            condition = None
            for child in node:
                if local_name(child.tag) == "conditionExpression":
                    condition = (child.text or "").strip() or None
            changeable = _xsd_bool(node.get(f"{{{PORTER_NS}}}changeable"), f"The flow '{label}'",
                                   "porter:changeable", problems)
            flows[node_id] = Flow(node_id, node.get("sourceRef") or "", node.get("targetRef") or "",
                                  node.get("name") or None, condition, None, _doc(node),
                                  changeable=changeable, firm=_p(node, "firm"),
                                  firm_meaning=_p(node, "firmMeaning"), wording=_p(node, "wording"))
            continue
        if tag not in _NODE_KINDS:
            why = _UNSUPPORTED_WHY.get(tag, "it isn't part of the supported subset")
            problems.append(f"'{label}' ({node_id}) is {_a(tag)}, which the app can't run: {why}")
            continue
        if node_id in elements:
            problems.append(f"The id '{node_id}' is used twice")
            continue
        kind = _NODE_KINDS[tag]
        who = {
            START: f"The start '{label}'", END: f"The end '{label}'", SERVICE: f"The step '{label}'",
            AGENT: f"The step '{label}'", USER: f"The step '{label}'", CALL: f"The step '{label}'",
            GATEWAY: f"The decision '{label}'", BOUNDARY: f"The boundary event '{label}'",
            CATCH: f"The wait '{label}'",
        }[kind] + f" ({node_id})"
        el = Element(node_id, kind, f"bpmn:{tag}", node.get("name") or "", lanes.get(node_id), _doc(node))
        el.trigger = _trigger(node, who, messages, errors, problems)
        el.tags = _tags(node)
        if node.get("default"):
            default_attr[node_id] = node.get("default")
        if kind in (SERVICE, AGENT, USER, CALL):
            el.output = _p(node, "output")
        if kind == SERVICE:
            el.action = _p(node, "action")
        elif kind == AGENT:
            el.role, el.instructions, el.template = _p(node, "role"), _p(node, "instructions"), _p(node, "template")
        elif kind == USER:
            el.card = _p(node, "card")
        elif kind == CALL:
            el.called = node.get("calledElement")
            el.in_map = _parse_map(_p(node, "in"), f"{who} porter:in", problems)
            el.out_map = _parse_map(_p(node, "out"), f"{who} porter:out", problems)
        if kind in (SERVICE, AGENT, USER, CALL):
            el.multi = _multi(node, who, problems)
        if kind == BOUNDARY:
            el.attached_to = node.get("attachedToRef")
            if not _xsd_bool(node.get("cancelActivity"), who, "cancelActivity", problems, default=True):
                problems.append(f"{who} doesn't interrupt its step; only interrupting boundary events are supported")
        elements[node_id] = el

    def who_of(el: Element) -> str:
        names = {
            START: "The start", END: "The end", SERVICE: "The step", AGENT: "The step", USER: "The step",
            CALL: "The step", GATEWAY: "The decision", BOUNDARY: "The boundary event", CATCH: "The wait",
        }
        return f"{names[el.kind]} '{el.label}' ({el.id})"

    # --- the flows join the elements ---
    for flow in list(flows.values()):
        if flow.id in elements:
            problems.append(f"The id '{flow.id}' is used by a flow and by {_lower_first(who_of(elements[flow.id]))}")
        if not flow.source or not flow.target:
            missing = "start (sourceRef)" if not flow.source else "end (targetRef)"
            problems.append(f"The flow '{flow.id}' has no {missing}")
            del flows[flow.id]
            continue
        if flow.source not in elements or flow.target not in elements:
            problems.append(
                f"The flow '{flow.id}' joins '{flow.source}' to '{flow.target}', and one of them isn't in the process")
            del flows[flow.id]
            continue
        into = elements[flow.target]
        if into.kind == START:
            problems.append(f"The flow '{flow.name or flow.id}' goes into {_lower_first(who_of(into))}, "
                            "but nothing can come before a start")
        elif into.kind == BOUNDARY:
            problems.append(f"The flow '{flow.name or flow.id}' goes into {_lower_first(who_of(into))}, but a "
                            "boundary event is reached only when the step it is attached to times out or fails")
        elements[flow.source].outgoing.append(flow.id)
        elements[flow.target].incoming.append(flow.id)
    for el in elements.values():
        if el.kind == BOUNDARY and el.attached_to in elements:
            elements[el.attached_to].boundaries.append(el.id)
    for gateway_id, flow_id in default_attr.items():
        host = elements[gateway_id]
        if host.kind != GATEWAY:
            problems.append(f"{who_of(host)} has a default way out but isn't a decision")
        elif flow_id in flows and flows[flow_id].source == gateway_id:
            flows[flow_id].is_default = True
        else:
            problems.append(f"{who_of(host)} names '{flow_id}' as its default, which doesn't leave it")
    for el in elements.values():
        el.lane = lanes.get(el.id)

    # --- element by element ---
    for el in elements.values():
        who = who_of(el)
        n_out = len(el.outgoing)
        if el.kind == START:
            if el.trigger.type not in ("none", "message", "timer"):
                problems.append(
                    f"{who} starts on {_a(el.trigger.type)} event; only a message or a timer start is supported")
            if n_out != 1:
                problems.append(f"{who} must have exactly one way out, not {n_out}")
        elif el.kind == END:
            if el.trigger.type != "none":
                problems.append(f"{who} is {_a(el.trigger.type)} end; only a plain end is supported")
            if n_out:
                problems.append(f"{who} is an end but has a way out")
        elif el.kind in (SERVICE, AGENT, USER, CALL, CATCH):
            if n_out != 1:
                problems.append(f"{who} must have exactly one way out, not {n_out} (use a decision to split)")
        if el.kind in (START, CATCH) and el.trigger.type == "message" and not el.trigger.message:
            problems.append(f"{who} waits on a message but names none (messageRef), so nothing could ever send it")
        if el.kind in (START, CATCH, BOUNDARY):
            _check_timer(el, who, problems)
        if el.kind == SERVICE:
            if not el.action:
                problems.append(f"{who} is a service task but names no action (porter:action)")
            elif el.action not in registry:
                problems.append(f"{who} names the action '{el.action}', which isn't registered")
            else:
                for needed in registry.get(el.action).inputs:
                    if model.resolve(needed) is None:
                        problems.append(
                            f"{who}: the action '{el.action}' reads '{needed}', which the data model doesn't declare")
            if el.multi:
                problems.append(f"{who}: only a call activity can run once per item")
        elif el.kind == AGENT:
            if not el.role:
                problems.append(f"{who} is a plain task with no role; an agent step names its role (porter:role)")
            elif el.role not in catalog.roles:
                problems.append(f"{who} names the role '{el.role}', which doesn't exist")
            if el.instructions and not catalog.has_instructions(el.instructions):
                problems.append(f"{who} names the instruction file '{el.instructions}', which doesn't exist")
            if el.template and not catalog.has_template(el.template):
                problems.append(f"{who} names the response template '{el.template}', which doesn't exist")
            if el.instructions:
                el.instructions = catalog.short("instructions", el.instructions)
            if el.template:
                el.template = catalog.short("templates", el.template)
            if el.multi:
                problems.append(f"{who}: only a call activity can run once per item")
        elif el.kind == USER:
            if not el.card:
                problems.append(f"{who} is a user task but names no card (porter:card)")
            elif el.card not in catalog.cards:
                problems.append(f"{who} names the card '{el.card}', which doesn't exist")
            if el.multi:
                problems.append(f"{who}: only a call activity can run once per item")
        elif el.kind == CALL:
            if not el.called:
                problems.append(f"{who} is a call activity but names no process (calledElement)")
            for _child_var, source in el.in_map:
                if model.resolve(source) is None:
                    problems.append(f"{who} hands '{source}' to the sub-process, which the data model doesn't declare")
            for target, _source in el.out_map:
                if model.resolve(target) is None:
                    problems.append(f"{who} brings back into '{target}', which the data model doesn't declare")
            if el.multi:
                field = model.resolve(el.multi.collection)
                if field is None:
                    problems.append(
                        f"{who} runs once per item of '{el.multi.collection}', which the data model doesn't declare")
                elif field.type not in ("list", "any"):
                    problems.append(
                        f"{who} runs once per item of '{el.multi.collection}', which is {_a(field.type)}, not a list")
        elif el.kind == CATCH:
            if el.trigger.type not in ("message", "timer"):
                problems.append(
                    f"{who} waits on {_a(_kind_word(el.trigger.type))} event; only a message or a timer is supported")
        elif el.kind == BOUNDARY:
            host = elements.get(el.attached_to or "")
            if host is None:
                if el.attached_to:
                    problems.append(f"{who} is attached to '{el.attached_to}', which isn't in the process")
                else:
                    problems.append(f"{who} isn't attached to any step (attachedToRef)")
            if el.trigger.type not in ("timer", "error"):
                problems.append(f"{who} is {_a(_kind_word(el.trigger.type))} boundary event; "
                                "only timer and error boundary events are supported")
            if n_out != 1:
                problems.append(f"{who} must have exactly one way out, not {n_out}")
            if host is not None and el.trigger.type == "timer" and host.kind not in WAITING_KINDS:
                problems.append(f"{who} is a timer on a step that doesn't wait; put it on an agent, user or call step")
            if host is not None and el.trigger.type == "error" and host.kind != SERVICE:
                problems.append(f"{who} catches an error, which only an action's step can raise")
        if el.kind in (SERVICE, AGENT, USER, CALL) and el.output:
            if not _NAME.match(el.output) or model.resolve(el.output) is None:
                problems.append(f"{who} puts its result in '{el.output}', which the data model doesn't declare")
        if el.kind == GATEWAY:
            _check_gateway(el, who, flows, model, problems)

    for flow in flows.values():
        src = elements[flow.source]
        if src.kind != GATEWAY and flow.condition:
            problems.append(f"The flow '{flow.name or flow.id}' out of {who_of(src)} has a condition, "
                            "but only a decision's ways out can")
        if src.kind != GATEWAY and (flow.changeable or flow.firm):
            problems.append(f"The flow '{flow.name or flow.id}' out of {who_of(src)} can't be a rule the household "
                            "changes: only a decision's way out can")

    # --- a start and an end, and every element reachable, and every reachable element able to end ---
    starts = [e.id for e in elements.values() if e.kind == START]
    if not starts:
        problems.append("The process has no start event")
    if not any(e.kind == END for e in elements.values()):
        problems.append("The process has no end event")
    seen: set[str] = set()
    todo = list(starts)
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        seen.add(current)
        el = elements[current]
        todo.extend(flows[f].target for f in el.outgoing)
        todo.extend(el.boundaries)
    for el in elements.values():
        if el.id not in seen and el.kind != START:
            problems.append(f"{who_of(el)} can't be reached from a start")
    problems.extend(_cannot_end(elements, flows, seen, who_of))

    pkey = key or _p(process, "key") or process.get("id") or ""
    if not pkey:
        problems.append("The process has no id")
    if problems:
        raise ProcessLoadError(problems)

    warnings = [
        f"The way out '{f.name or f.id}' of '{elements[f.source].label}' has a condition but no plain-words meaning"
        for f in flows.values() if f.condition and not f.documentation
    ]
    text = raw.decode("utf-8", errors="replace")
    return ProcessDef(
        key=pkey, name=process.get("name") or pkey, file_hash=hashlib.sha256(raw).hexdigest(), xml=text,
        elements=elements, flows=flows, data_model=model, start_events=starts, documentation=_doc(process),
        warnings=warnings, behaviour_hash=behaviour_hash(pkey, elements, flows),
    )


def _cannot_end(elements: dict[str, Element], flows: dict[str, Flow], reached: set[str], who_of) -> list[str]:
    """Every element reached from a start must have some path to an end: walk back from the ends, over the flows (and
    from a boundary event to the step it is attached to). A part that is left over is a loop with no way out, or a
    branch that leads nowhere: work that gets in would wait or spin for ever."""
    can_end = {e.id for e in elements.values() if e.kind == END}
    before: dict[str, list[str]] = {}
    for flow in flows.values():
        before.setdefault(flow.target, []).append(flow.source)
    for el in elements.values():
        if el.kind == BOUNDARY and el.attached_to in elements:
            before.setdefault(el.id, []).append(el.attached_to)
    todo = list(can_end)
    while todo:
        for previous in before.get(todo.pop(), ()):
            if previous not in can_end:
                can_end.add(previous)
                todo.append(previous)
    if not can_end:  # no end at all is reported on its own
        return []
    stuck = [e for e in elements.values() if e.id in reached and e.id not in can_end]
    if not stuck:
        return []
    shown = " or ".join(_lower_first(who_of(e)) for e in stuck[:3])
    more = f" (and {len(stuck) - 3} more like them)" if len(stuck) > 3 else ""
    return [f"No path from {shown}{more} leads to an end, so work that gets there could never finish"]


def behaviour_hash(key: str, elements: dict[str, Element], flows: dict[str, Flow]) -> str:
    """A hash of what decides what a process does: its elements and their types, its ways out and their conditions, and
    the `porter:` attributes that bind code (action, role, instructions, template, card, output, what a call calls and
    passes, how many times a step runs, timers and errors). Documentation, Source lines, display names and lanes are
    left out, so editing the words of a file never throws away what was proved about it, and editing a condition
    does.

    The order of a decision's ways out is in the hash: the engine takes the first condition that holds, in the order the
    file lists them, and conditions may overlap, so two ways out swapped can send the same data somewhere else. (The
    default way out is taken only when no condition holds, wherever it is listed, so it goes last. The order of what
    comes into an element, and of the boundary events on a step, changes nothing, so those are sorted.)
    `porter:changeable` is not in it: it says who may rewrite a rule, not what the rule does. Neither are
    `porter:engine`, `porter:harness` or `porter:calls`, which are the app's words about a step."""
    shape = {
        "key": key,
        "elements": [
            {
                "id": e.id, "kind": e.kind, "type": e.bpmn_type, "action": e.action, "role": e.role,
                "instructions": e.instructions, "template": e.template, "card": e.card, "output": e.output,
                "called": e.called, "in": [list(p) for p in e.in_map], "out": [list(p) for p in e.out_map],
                "multi": ([e.multi.sequential, e.multi.collection, e.multi.element_variable] if e.multi else None),
                "attached_to": e.attached_to,
                "trigger": [e.trigger.type, e.trigger.message, e.trigger.timer_kind, e.trigger.timer_text,
                            e.trigger.error_code],
                "outgoing": [*(f for f in e.outgoing if not flows[f].is_default),
                             *(f for f in e.outgoing if flows[f].is_default)],
                "incoming": sorted(e.incoming), "boundaries": sorted(e.boundaries),
            }
            for e in sorted(elements.values(), key=lambda e: e.id)
        ],
        "flows": [
            {"id": f.id, "source": f.source, "target": f.target, "condition": f.condition, "default": f.is_default,
             **({"firm": f.firm} if f.firm else {})}  # the part of a rule no household change can touch decides too
            for f in sorted(flows.values(), key=lambda f: f.id)
        ],
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _check_gateway(el: Element, who: str, flows: dict[str, Flow], model: DataModel, problems: list[str]) -> None:
    ways = [flows[f] for f in el.outgoing]
    if not ways:
        problems.append(f"{who} has no way out")
        return
    if len(ways) == 1:
        if ways[0].condition:
            problems.append(f"{who} has one way out, which has a condition; a single way out needs none")
        if ways[0].changeable or ways[0].firm:
            problems.append(f"{who} has one way out, so there is no rule for the household to change; "
                            "a rule needs another way to go")
        return
    trees = []
    for flow in ways:
        label = flow.name or flow.id
        if flow.is_default:
            if flow.condition:
                problems.append(f"{who}: the default way out '{label}' must not have a condition")
            if flow.changeable or flow.firm:
                problems.append(f"{who}: the default way out '{label}' has no rule of its own to change")
            continue
        if not flow.condition:
            problems.append(f"{who}: the way out '{label}' has no condition and isn't the default")
            continue
        if flow.firm and not flow.changeable:
            problems.append(f"{who}: the way out '{label}' keeps a firm part but isn't a rule the household changes")
        try:
            tree = feel.parse(flow.condition)
        except ConditionError as exc:
            problems.append(f"{who}: the condition of '{label}' ({flow.condition}) can't be read: {exc}")
            continue
        found = feel.analyze(tree, model)
        problems.extend(f"{who}: the condition of '{label}': {p}" for p in found)
        if flow.firm:
            try:
                flow.firm_tree = feel.parse(flow.firm)
            except ConditionError as exc:
                problems.append(f"{who}: the firm part of '{label}' ({flow.firm}) can't be read: {exc}")
                continue
            firm_found = feel.analyze(flow.firm_tree, model)
            problems.extend(f"{who}: the firm part of '{label}': {p}" for p in firm_found)
            found = [*found, *firm_found]
            # What the engine runs is "(firm) or (condition)", built from the two trees just read: never from the
            # joined text, which can be longer than a condition may be and would be read a second time.
            flow.tree = feel.Bin("or", flow.firm_tree, tree)
        else:
            flow.tree = tree
        if not found:
            trees.append(flow.tree)
    defaults = [f for f in ways if f.is_default]
    if not defaults and len(trees) == len(ways) and not feel.covers_every_case(trees, model):
        problems.append(f"{who} has no default way out and its conditions don't visibly cover every case "
                        "(including a field with no value yet); mark one way out as the default")


# --- a set of processes, calling each other ----------------------------------------------------------------

#: Each owner's own versions of process files (requirement 15.4): owner id -> process key -> file hash -> the
#: process, and which of them new work for that owner starts on. One install can hold several owners, so one owner's
#: rule never reaches another's work: a library serves an owner its own versions only when asked for that owner
#: (`ProcessLibrary.get(..., entity_id=...)`). `process_engine.versions.refresh` fills it from the database at startup
#: and after a change or a restore, and `process_engine.versions.current_for` fills it again when another program
#: saved a version.
_owner_versions: dict[uuid.UUID, dict[str, dict[str, ProcessDef]]] = {}
_owner_active: dict[uuid.UUID, dict[str, str]] = {}


def owner_active(key: str, entity_id: uuid.UUID | None) -> ProcessDef | None:
    """The household's newest version of a process, or None when it has changed nothing there (or none is named)."""
    if entity_id is None:
        return None
    return _owner_versions.get(entity_id, {}).get(key, {}).get(_owner_active.get(entity_id, {}).get(key, ""))


def set_owner_versions(
    versions: dict[uuid.UUID, dict[str, list[ProcessDef]]], active: dict[uuid.UUID, dict[str, str]],
) -> None:
    """Replace every household's versions: `versions[household][key]` is each version loaded, `active[household][key]`
    the hash of the one that household's new work starts on. The new tables are built whole first and swapped in with
    one assignment each, so a bad argument leaves the old tables as they were and a reader never sees half a change."""
    global _owner_versions, _owner_active
    new_versions = {
        entity_id: {key: {d.file_hash: d for d in defs} for key, defs in by_key.items()}
        for entity_id, by_key in versions.items()
    }
    new_active = {entity_id: dict(by_key) for entity_id, by_key in active.items()}
    _owner_versions, _owner_active = new_versions, new_active


def owner_of(data: dict | None, subject: str | None) -> uuid.UUID | None:
    """The owner an instance belongs to, when that owner has versions of its own. The app says which strings may be
    the owner's id (`config.owner_candidates`: by default `data[config.owner_key]`); the first that is an owner with
    versions wins. None means the instance runs the shipped files."""
    for text in config.candidates(data, subject):
        try:
            entity_id = uuid.UUID(text)
        except (ValueError, AttributeError, TypeError):
            continue
        if entity_id in _owner_versions:
            return entity_id
    return None


class ProcessLibrary:
    """The processes the engine can run, each in every version it has been loaded in (a running instance
    finishes under the version it started with). Call `validate()` once they are all added."""

    def __init__(self) -> None:
        self._versions: dict[str, dict[str, ProcessDef]] = {}
        self._latest: dict[str, str] = {}

    def add(self, defn: ProcessDef) -> ProcessDef:
        self._versions.setdefault(defn.key, {})[defn.file_hash] = defn
        self._latest[defn.key] = defn.file_hash
        return defn

    def load(self, xml: str | bytes, **kwargs) -> ProcessDef:
        return self.add(load_process(xml, **kwargs))

    def get(self, key: str, file_hash: str | None = None, entity_id: uuid.UUID | None = None) -> ProcessDef | None:
        """A process by key. With `file_hash`, that exact file (a running instance's own; a file is found by what it
        says, so no household's version is ever handed to work that didn't start on it). Without, the file new work
        starts on: the household's newest version when `entity_id` names a household that changed it, else the one
        shipped."""
        versions = self._versions.get(key)
        if not versions:
            return None
        if file_hash:
            found = versions.get(file_hash)
            for by_key in _owner_versions.values() if found is None else ():
                found = by_key.get(key, {}).get(file_hash) or found
            return found
        return owner_active(key, entity_id) or versions.get(self._latest[key])

    def shipped(self, key: str) -> ProcessDef | None:
        """The shipped file's own process, whatever the household has changed."""
        versions = self._versions.get(key)
        return versions.get(self._latest[key]) if versions else None

    def keys(self) -> list[str]:
        return sorted(self._versions)

    def validate(self) -> None:
        """Every call activity names a process in the library, hands over and takes back only fields both sides
        declare, and no process calls itself, directly or through others."""
        problems: list[str] = []
        calls: dict[str, set[str]] = {}
        for key in self.keys():
            defn = self.get(key)
            calls[key] = set()
            for el in defn.elements.values():
                if el.kind != CALL or not el.called:
                    continue
                child = self.get(el.called)
                where = f"{defn.key}: the step '{el.label}' ({el.id})"
                if child is None:
                    problems.append(f"{where} calls '{el.called}', which isn't a loaded process")
                    continue
                calls[key].add(el.called)
                for child_var, _source in el.in_map:
                    if child.data_model.resolve(child_var) is None:
                        problems.append(f"{where} hands over '{child_var}', which '{el.called}' doesn't declare")
                for _target, source in el.out_map:
                    if child.data_model.resolve(source) is None:
                        problems.append(f"{where} brings back '{source}', which '{el.called}' doesn't declare")
                if el.multi and child.data_model.resolve(el.multi.element_variable) is None:
                    problems.append(f"{where} gives each child '{el.multi.element_variable}', "
                                    f"which '{el.called}' doesn't declare")
        state: dict[str, int] = {}

        def visit(key: str, trail: list[str]) -> None:
            if state.get(key) == 2:
                return
            if state.get(key) == 1:
                problems.append("These processes call each other in a circle: " + " -> ".join([*trail, key]))
                return
            state[key] = 1
            for nxt in sorted(calls.get(key, ())):
                visit(nxt, [*trail, key])
            state[key] = 2

        for key in self.keys():
            visit(key, [])
        if problems:
            raise ProcessLoadError(problems)


__all__ = ["PORTER_NS", "ProcessLibrary", "owner_active", "owner_of", "load_process", "set_owner_versions"]
