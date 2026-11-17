"""The processes as their BPMN files draw them: every file of `playbooks/jobsearch/` in the version in force (the
owner's newest saved one, else the file as shipped), read with the process engine's importer into steps, and a step's
`{role, instructions, template}` as its file names them.

The page "How they work" (`how_they_work.py`) builds its view from these, and the record of what an agent was sent
(`app.planengine.handouts`) finds a step's files with `step_spec`, so this module imports neither of them.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import defusedxml.ElementTree as DET
from process_engine import versions as proc_versions
from process_engine.model import ProcessDef

from app import owner_keys
from app.config import settings as base_settings
from app.harness import instructions as files
from app.jobsearch import process as job_process
from app.process.bpmn import parse_bpmn
from app.process.models import StepType

#: The namespace of the `porter:` attributes the process files carry (the engine's file format).
EXT_NS = "http://porter.local/bpmn/ext"

# --- the processes ------------------------------------------------------------------------------

#: The headings a step's `<documentation>` is written under, and the key each one fills.
SECTION_KEYS = {
    "Actor": "actor", "Trigger": "trigger", "Rules": "rules", "Outputs": "outputs",
    "What the owner sees": "owner_sees", "Source": "source",
}
_SECTION = re.compile(r"^(" + "|".join(SECTION_KEYS) + r"):[ \t]*", re.M)
_STEP_TYPE = {
    StepType.event_start: "start", StepType.event_end: "end",
    StepType.gateway_exclusive: "decision", StepType.gateway_parallel: "decision",
    StepType.subprocess: "subprocess",
}

#: The labelled parts inside a step's `Rules:` block, each starting a line, and the key each one fills. Anything not
#: under one of these (the numbered rules) stays in `rules`, in the file's order.
_RULE_PARTS = re.compile(r"^(?:(Preconditions|Inputs|Exceptions)\.|(Fired by|Enters at):)[ \t]*(.*)$")
_PLAIN_RULES_LINE = re.compile(r"^Processing rules(?:, in order| and effects)?\.[ \t]*(.*)$")
_RULE_PART_KEYS = {
    "Preconditions": "preconditions", "Inputs": "inputs", "Exceptions": "exceptions",
    "Fired by": "fired_by", "Enters at": "enters_at",
}
_RULE_KEYS = ("preconditions", "inputs", "exceptions", "fired_by", "enters_at")


def split_rules(text: str) -> dict[str, str]:
    """A step's `Rules:` block split into preconditions, inputs, the rules themselves (numbered, in order), exceptions,
    and for a start event what fires it and where it enters. A part that isn't there is an empty string."""
    parts: dict[str, list[str]] = {"rules": [], **{k: [] for k in _RULE_KEYS}}
    current = "rules"
    for line in text.splitlines():
        m = _RULE_PARTS.match(line)
        if m:
            current = _RULE_PART_KEYS[m.group(1) or m.group(2)]
            line = m.group(3)
        elif line.startswith("Processing rules"):
            current = "rules"
            plain = _PLAIN_RULES_LINE.match(line)
            if plain:
                line = plain.group(1)
        if line:
            parts[current].append(line)
    return {k: "\n".join(v).strip() for k, v in parts.items()}


def doc_sections(text: str | None) -> dict[str, str]:
    """A step's `<documentation>` split under its headings (Actor, Trigger, Rules, Outputs, What the owner sees,
    Source), with the Rules block split further by `split_rules`. A heading that isn't there is an empty string."""
    out = dict.fromkeys(SECTION_KEYS.values(), "")
    text = text or ""
    found = list(_SECTION.finditer(text))
    for i, m in enumerate(found):
        key = SECTION_KEYS[m.group(1)]
        if out[key]:
            continue
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        out[key] = text[m.end():end].strip()
    return {**out, **split_rules(out["rules"])}


_TAG_SUFFIXES = ("_instructions", "_criteria", "_commands", "_prompts")


def spec_documentation(raw: bytes) -> dict[str, str]:
    """Each step's specification, by step id: its first untagged `<documentation>`, the one the Actor / Trigger /
    Rules headings are written in. The importer reads a step's tagged documentation (`<step>_instructions` and the
    like, the upstream project's step tags) as the step's instructions, which would hide the specification, so it is
    read from the file here (as `app.agents.how_they_work` does)."""
    found: dict[str, str] = {}
    for process in DET.fromstring(raw):
        if process.tag.rsplit("}", 1)[-1] != "process":
            continue
        for el in process:
            for doc in el:
                if doc.tag.rsplit("}", 1)[-1] != "documentation" or (doc.get("id") or "").endswith(_TAG_SUFFIXES):
                    continue
                if (doc.text or "").strip():
                    found.setdefault(el.get("id") or "", doc.text)
    return found


def step_calls(bpmn_attrs: dict | None, ext_attrs: dict[str, str]) -> str | None:
    """The sub-process a step calls, by its key: a call activity's `calledElement`, or an explicit `porter:calls`."""
    called = (bpmn_attrs or {}).get("calledElement") or ext_attrs.get("calls")
    return (called.strip() or None) if isinstance(called, str) else None


def process_files() -> list[str]:
    """Every process file, by name: the search round, screening one role, and any file added later."""
    return sorted(p.name for p in job_process.process_dir().glob("*.bpmn"))


def playbook_bytes(file: str) -> bytes:
    """A process file as it runs: the owner's newest saved version of it that loads, else the file as shipped
    (`process_engine.versions.active_xml`, the library's copy of the same choice the database gives). The library is
    filled from the database by the caller before this (`how_they_work.read_versions`): this function has no session."""
    raw = (job_process.process_dir() / file).read_bytes()
    for el in DET.fromstring(raw):
        if el.tag.rsplit("}", 1)[-1] == "process":
            key = el.get("{" + EXT_NS + "}key") or el.get("id") or Path(file).stem
            active = proc_versions.active_xml(key, owner_keys.entity_id())
            return active.encode("utf-8") if active is not None else raw
    return raw


def process_head(file: str) -> dict:
    """A process file's own process element: its key (`porter:key`, else its id), its name and its text."""
    root = DET.fromstring(playbook_bytes(file))
    for el in root:
        if el.tag.rsplit("}", 1)[-1] != "process":
            continue
        doc = next((d.text or "" for d in el if d.tag.rsplit("}", 1)[-1] == "documentation"), "")
        key = el.get("{" + EXT_NS + "}key") or el.get("id") or Path(file).stem
        return {"key": key, "name": (el.get("name") or key).strip(), "about": " ".join(doc.split())}
    key = Path(file).stem
    return {"key": key, "name": key, "about": ""}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def loaded_process(key: str) -> ProcessDef | None:
    """The process as the engine loaded it (its conditions, data model, firm parts), or None when it isn't loaded (the
    page then shows the steps without them)."""
    try:
        return job_process.library().get(key, entity_id=owner_keys.entity_id())
    except Exception:  # a file that doesn't load still draws; the loader's own tests say why
        logging.getLogger(__name__).warning("how they work: %s doesn't load as a process", key, exc_info=True)
        return None


def ways_of(defn: ProcessDef, step_id: str) -> list[dict]:
    """A decision's ways out for the page: where each goes, its condition and its plain-words meaning, and whether
    the owner may change the rule and what no rule can change."""
    out = []
    for flow in defn.out_flows(step_id):
        target = defn.elements[flow.target]
        out.append({
            "flow": flow.id, "to": flow.target, "to_title": target.label, "name": flow.name or "",
            "condition": flow.condition, "meaning": flow.documentation or "", "default": flow.is_default,
            "changeable": flow.changeable, "firm": flow.firm, "firm_meaning": flow.firm_meaning,
            "wording": flow.wording,
        })
    return out


def bpmn_steps(file: str) -> list[dict]:
    """One process as its BPMN file draws it, read with the process engine's importer:
    `[{id, title, type (start | task | decision | subprocess | end), who (the lane), role, instructions, template,
    actor, trigger, preconditions, inputs, rules, exceptions, fired_by, enters_at, outputs, owner_sees, source,
    criteria, trigger_kind, trigger_detail, trigger_subtype, calls, next: [{to, label}]}]`, in the file's order."""
    raw = playbook_bytes(file)
    graph = parse_bpmn(raw)
    specs = spec_documentation(raw)
    nxt: dict[str, list[dict]] = defaultdict(list)
    for e in graph.edges:
        nxt[e.source_temp_id].append({"to": e.target_temp_id, "label": e.label or ""})
    steps = []
    for s in graph.steps:
        attrs = {
            k.rsplit("}", 1)[-1]: v for k, v in (s.bpmn_attrs or {}).items()
            if k.startswith("{" + EXT_NS + "}") and isinstance(v, str)
        }
        steps.append({
            "id": s.temp_id, "title": s.description, "type": _STEP_TYPE.get(s.step_type, "task"),
            "who": ", ".join(s.performer_names),
            "role": attrs.get("role"), "instructions": attrs.get("instructions"), "template": attrs.get("template"),
            **doc_sections(specs.get(s.temp_id) or s.instructions), "criteria": s.decision_criteria,
            "trigger_kind": s.event_trigger, "trigger_detail": s.event_trigger_detail,
            "trigger_subtype": s.event_trigger_subtype, "calls": step_calls(s.bpmn_attrs, attrs),
            "next": nxt[s.temp_id],
        })
    return steps


def step_spec(process_id: str, step_id: str) -> dict:
    """A step's `{role, instructions, template}` as its process file names them. Raises KeyError when not there."""
    for file in process_files():
        if process_head(file)["key"] != process_id:
            continue
        for s in bpmn_steps(file):
            if s["id"] == step_id:
                return {"role": s["role"], "instructions": s["instructions"], "template": s["template"]}
    raise KeyError(step_id)


# --- the files a step is sent together --------------------------------------------------------------------------


def _site_texts() -> list[bytes]:
    """The site-project processes (`playbooks/sites/`), as shipped."""
    sites = Path(base_settings.playbooks_path) / "sites"
    return [p.read_bytes() for p in sorted(sites.glob("*.bpmn"))]


def _playbook_texts() -> list[bytes]:
    """Every process file the app runs, as it runs: the job-search processes in the version in force (`playbook_bytes`),
    and the site-project processes (`playbooks/sites/`) as shipped."""
    return [playbook_bytes(file) for file in process_files()] + _site_texts()


def playbook_key(file: str) -> str:
    """A process file's key: its process element's `porter:key`, else the element's id, else the file's name (the key
    its saved versions are kept under, as `playbook_bytes` looks them up)."""
    for el in DET.fromstring((job_process.process_dir() / file).read_bytes()):
        if el.tag.rsplit("}", 1)[-1] == "process":
            return el.get("{" + EXT_NS + "}key") or el.get("id") or Path(file).stem
    return Path(file).stem


def stored_playbook_texts(newest: dict[str, str]) -> list[bytes]:
    """Every process file the app runs, as the database holds it: for each job-search process the text of the owner's
    newest saved version (`newest`, a process key to its text: `how_they_work.newest_stored_texts`), else the file as
    shipped; the site-project processes as shipped. It asks no process library, so the actions the engine registers are
    never imported (the page reads this)."""
    out: list[bytes] = []
    for file in process_files():
        saved = newest.get(playbook_key(file))
        out.append(saved.encode("utf-8") if saved is not None else (job_process.process_dir() / file).read_bytes())
    return out + _site_texts()


def pairs_in(texts: Iterable[bytes]) -> set[tuple[str, str]]:
    """The `(instruction file, template)` each step of these process files names together (`porter:instructions` and
    `porter:template`), by name. An agent is sent the two in one reply at that step."""
    pairs: set[tuple[str, str]] = set()
    for raw in texts:
        for el in DET.fromstring(raw).iter():
            instructions, template = el.get(f"{{{EXT_NS}}}instructions"), el.get(f"{{{EXT_NS}}}template")
            if instructions and template:
                pairs.add((files.short_name("instructions", instructions), files.short_name("templates", template)))
    return pairs


def step_file_pairs() -> set[tuple[str, str]]:
    """The pairs of every process file as it runs now (`pairs_in` the library's versions)."""
    return pairs_in(_playbook_texts())


def names_paired(pairs: set[tuple[str, str]], kind: str, name: str) -> list[str]:
    """The files an agent is sent with this one, by name, sorted, out of `pairs`: for an instruction file (`kind`
    "instructions") the templates of the steps that name it, for a template the instruction files. Empty when no step
    names the file (the scoring instructions, which only ride in another file's session), and a name no file has is
    left out."""
    ref = files.short_name(kind, name)
    here, there, other = (0, 1, "templates") if kind == "instructions" else (1, 0, "instructions")
    have = set(files.names(other))
    return sorted({pair[there] for pair in pairs if pair[here] == ref and pair[there] in have})


def paired_names(kind: str, name: str) -> list[str]:
    """`names_paired` of the pairs of the process files as they run now (the library's versions)."""
    return names_paired(step_file_pairs(), kind, name)
