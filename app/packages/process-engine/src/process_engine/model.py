"""A loaded process: the executable model the engine runs (requirement 15.1).

Built by `loader.load_process` from a BPMN file; never edited in place. It keeps what execution needs (ids,
kinds, ways out with their conditions, who does each step, what an agent is sent) and what the page shows (the
file's own words, lanes, documentation), so one object serves both.
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta

from process_engine.datamodel import DataModel

#: Element kinds the engine runs. Everything else in a file is refused when it loads.
START, END, SERVICE, AGENT, USER, CALL, GATEWAY, BOUNDARY, CATCH = (
    "start", "end", "service", "agent", "user", "call", "gateway", "boundary", "catch",
)
#: Steps a boundary event can be attached to, by event type.
WAITING_KINDS = (AGENT, USER, CALL)


class StartNotFound(KeyError):
    """`ProcessDef.start_for` found no start event. Prints as its plain message, not as a quoted key."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


@dataclass(frozen=True)
class Trigger:
    """What starts or interrupts something. `type`: none, message, timer or error."""

    type: str = "none"
    message: str | None = None
    timer_kind: str | None = None  # duration | date | cycle
    timer_text: str | None = None
    error_code: str | None = None  # None on an error boundary: any error


@dataclass(frozen=True)
class MultiInstance:
    sequential: bool
    collection: str  # a path in the data: one child instance per item
    element_variable: str  # the child's own variable holding its item


@dataclass
class Flow:
    id: str
    source: str
    target: str
    name: str | None = None
    condition: str | None = None
    tree: object | None = None
    documentation: str | None = None
    is_default: bool = False
    #: The household may change this way out's rule (`porter:changeable`, requirement 15.4). Only a decision's way
    #: out with a condition can be.
    changeable: bool = False
    #: What no rule can change (`porter:firm`): where it holds, the work takes this way out whatever the rule says.
    #: It is how a safeguard stays in the file: the household's rule is only ever the `condition` beside it.
    firm: str | None = None
    firm_tree: object | None = None
    firm_meaning: str | None = None
    #: The household's own words for the rule, when the household wrote it (`porter:wording`).
    wording: str | None = None

    @property
    def effective(self) -> str | None:
        """The condition the engine evaluates: the firm part or the rule. Equal to `condition` without a firm part."""
        if self.firm and self.condition:
            return f"({self.firm}) or ({self.condition})"
        return self.firm or self.condition


@dataclass(frozen=True)
class StepTags:
    """What an export from the upstream project says about a step beyond how it runs, carried so the view and agents can read it.
    The engine acts on none of it, and none of it is part of `behaviour_hash` (it is words and references).

    From the step's tagged `<documentation>` (`<step>_instructions`, `_criteria`, `_commands`, `_prompts`) and from
    its `extensionElements` in the extension namespace (owner, role, person, software, document,
    control). Names, not ids; a control is `{"reference", "framework"}`. `instructions` is the step's own words (an
    agent task's instruction *file* is `Element.instructions`, a different thing)."""

    instructions: str | None = None
    decision_criteria: str | None = None
    commands: str | None = None
    prompts: str | None = None
    owner: str | None = None
    roles: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    software: tuple[str, ...] = ()
    documents: tuple[str, ...] = ()
    controls: tuple[dict, ...] = ()

    @property
    def empty(self) -> bool:
        return not any(getattr(self, name) for name in self.__dataclass_fields__)

    def as_dict(self) -> dict:
        return {
            "instructions": self.instructions, "decision_criteria": self.decision_criteria, "commands": self.commands,
            "prompts": self.prompts, "owner": self.owner, "roles": list(self.roles), "people": list(self.people),
            "software": list(self.software), "documents": list(self.documents),
            "controls": [dict(c) for c in self.controls],
        }


@dataclass
class Element:
    id: str
    kind: str
    bpmn_type: str
    name: str
    lane: str | None = None
    documentation: str | None = None
    action: str | None = None
    role: str | None = None
    instructions: str | None = None
    template: str | None = None
    card: str | None = None
    output: str | None = None
    called: str | None = None
    in_map: tuple[tuple[str, str], ...] = ()  # (child variable, parent path)
    out_map: tuple[tuple[str, str], ...] = ()  # (parent variable, child path)
    multi: MultiInstance | None = None
    attached_to: str | None = None
    trigger: Trigger = field(default_factory=Trigger)
    outgoing: list[str] = field(default_factory=list)
    incoming: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    #: the upstream project's tagged documentation and extension elements, carried and never acted on.
    tags: StepTags = field(default_factory=StepTags)

    @property
    def label(self) -> str:
        return self.name or self.id


@dataclass
class ProcessDef:
    key: str
    name: str
    file_hash: str
    xml: str
    elements: dict[str, Element]
    flows: dict[str, Flow]
    data_model: DataModel
    start_events: list[str]
    documentation: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: A hash of what decides behaviour only (see `loader.behaviour_hash`): a change to words (documentation, Source
    #: lines, names) leaves it as it was, and a change to a way out changes it. A run finds a loaded file by it.
    behaviour_hash: str = ""

    def element(self, element_id: str) -> Element:
        return self.elements[element_id]

    def out_flows(self, element_id: str) -> list[Flow]:
        return [self.flows[f] for f in self.elements[element_id].outgoing]

    def start_for(self, message: str | None = None, start_event: str | None = None) -> Element:
        """The start event an instance begins at: named, or the one for a message, or the only plain one. Raises
        `StartNotFound` (a `KeyError`) when there is none, with a message that finishes the sentence "has no start
        event for ..."."""
        starts = [self.elements[s] for s in self.start_events]
        if start_event:
            for s in starts:
                if s.id == start_event:
                    return s
            raise StartNotFound(f"the id '{start_event}'")
        if message:
            for s in starts:
                if s.trigger.type == "message" and s.trigger.message == message:
                    return s
            raise StartNotFound(f"the message '{message}'")
        plain = [s for s in starts if s.trigger.type == "none"]
        if len(plain) == 1:
            return plain[0]
        if len(starts) == 1:
            return starts[0]
        raise StartNotFound("an unnamed start: it has more than one, so say which")

    def describe(self) -> list[dict]:
        """For the How they work page: each step with who, what it is bound to and where it goes."""
        out = []
        for el in self.elements.values():
            out.append({
                "id": el.id, "kind": el.kind, "name": el.name, "lane": el.lane, "action": el.action,
                "role": el.role, "instructions": el.instructions, "template": el.template, "card": el.card,
                "called": el.called, "trigger": el.trigger.type,
                "ways_out": [{"flow": f.id, "to": f.target, "name": f.name, "condition": f.condition,
                              "meaning": f.documentation, "default": f.is_default, "changeable": f.changeable,
                              "firm": f.firm, "firm_meaning": f.firm_meaning, "wording": f.wording}
                             for f in self.out_flows(el.id)],
                **({} if el.tags.empty else {"tags": el.tags.as_dict()}),
            })
        return out


_DURATION = re.compile(
    r"^P(?:([0-9]{1,6})W)?(?:([0-9]{1,6})D)?(?:T(?:([0-9]{1,6})H)?(?:([0-9]{1,6})M)?(?:([0-9]{1,6})S)?)?$")
_LONG_NUMBER = re.compile(r"[0-9]{7,}")

#: The longest wait a timer may hold (about ten years). It keeps `now + period` inside the calendar, and nothing a
#: household waits on is longer.
MAX_DURATION = timedelta(days=3660)


def parse_duration(text: str) -> timedelta:
    """An ISO 8601 period (`PT30M`, `P1D`, `P2DT4H`). Months and years aren't fixed lengths, so they're refused:
    say them in days. A number has at most six digits and the whole period at most about ten years. Raises
    ValueError, with a message in plain words."""
    shown = str(text or "").strip()
    if _LONG_NUMBER.search(shown):
        raise ValueError(f"'{shown}' has a number too big for a period")
    match = _DURATION.match(shown)
    if not match or not any(match.groups()):
        raise ValueError(f"'{shown}' is not a period like PT30M, P1D or P2DT4H (months and years aren't supported)")
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    try:
        period = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    except OverflowError:  # not reachable with six-digit numbers; kept so a change to them can't bring a crash back
        raise ValueError(f"'{shown}' is too long a period") from None
    if period > MAX_DURATION:
        raise ValueError(f"'{shown}' is longer than ten years")
    return period
