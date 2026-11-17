"""How the agents work ("See the harness"): the processes this app runs, step by step, and at each
agent step what the agent is sent.

The processes are the BPMN files in `playbooks/jobsearch/` (one file for each piece of work: the search round,
researching a role, a packet and the rest), each as the version in force: the owner's newest saved version when they
have changed it on this page (docs/PRD.md path 16, `process_engine.versions`), else the file as shipped. Each is read
with the process engine's own importer (`app.process.bpmn.parse_bpmn`): lanes are the actors, start events carry what
triggers the process, a task's `<documentation>` holds its rules, outputs and what the owner sees, and a few
`porter:` attributes say who does a step (`porter:role`) and which instruction and template files an agent is sent
(`porter:instructions`, `porter:template`). A decision's conditions come from the process as the engine loaded it
(`app.jobsearch.process.library()`), so the page shows what runs.

What the scout is sent is read from the copy the app keeps of each hand-out (`planengine.handouts`, the table
`task_handouts`): "as sent to Scout at 9:40", with the instruction and template files that have changed since named.
Read-only: nothing here writes, and a payload leaves here with anything secret-looking removed (there is none by
design; `scrub` is the second lock).

Adapted from another project's code (app/PROVENANCE.md): the source project's example processes, its example
tasks built from plan tasks and its role files are gone; roles come from the `agent_roles` table, so a new role needs no
code, and the pieces of a hand-out are the fields `jobs.profile` returns.
"""

from __future__ import annotations

import uuid

from process_engine import versions as proc_versions
from process_engine.model import GATEWAY
from process_engine.models import ProcVersion
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import edit_process
from app.agents.process_steps import (
    bpmn_steps,
    loaded_process,
    names_paired,
    pairs_in,
    process_files,
    process_head,
    slug,
    step_spec,
    stored_playbook_texts,
    ways_of,
)
from app.agents.roster import SCOUT, SCOUT_ROLE
from app.agents.security import scrub
from app.harness import instructions as files
from app.harness import wording
from app.jobsearch.work import instruction_files
from app.models.agent import RoleDef
from app.models.handout import TaskHandout
from app.planengine import handouts
from app.planengine.budget import REPLY_CEILING_CHARS, payload_chars
from app.timeutil import iso_utc

#: Who does a step when it is not an agent: the app by rule, the owner, or a tool.
BUILT_IN_ROLES = {
    "porter": ("Automatic", "Done by rule, with no agent asked."),
    "household": ("You", "Only you can do this one."),
    "tool": ("A tool", "A tool runs the check, with no model."),
}

#: What each part of the scout's hand-out is, in plain words: the fields `jobs.profile` returns. Every key must be here
#: (a test), so a new field can't ship undescribed.
CONTEXT_MEANS = wording.group("how_they_work")  # templates/wording.how_they_work.yaml
#: The parts of a hand-out, in the order they are shown, by role key.
SENT_FIELDS: dict[str, list[str]] = {
    "scout": ["task", "note", "roles_already_known", "profile"],
    "writer": ["packet", "role", "change_asked_for", "previous_version", "owner", "positioning", "rules", "read_next",
               "instructions", "response_template"],
}

#: Instruction files several agents share, by the step's own instruction: the runner appends them to the system prompt
#: (scout.mjs), so the page lists them as files given once. The scoring is explained in one place for every agent that
#: scores a posting (docs/PRD.md 30.6).
SHARED_INSTRUCTIONS: dict[str, list[str]] = {
    "jobsearch.analyst": ["jobsearch.scoring"],
}

# --- the files ----------------------------------------------------------------------------------


def file_view(kind: str, ref: str, title: str, store: instruction_files.Store | None = None) -> dict:
    """One file as the agent gets it (its header comment dropped, placeholders as written): the owner's newest saved
    version, else the file on disk (`store` holds what is saved, read once for the page; without it, the disk's).
    `version` is the one in force (1 until the owner saves an edit), `edited` says they have, `shipped_newer` that a
    release has shipped different text since, and `busy` says an agent is working with the file now (no save then)."""
    name = files.short_name(kind, ref)
    now = store.resolve(kind, name) if store is not None else instruction_files.Resolved(
        kind, name, files.read(kind, name), 1, False, False,
    )
    busy = store.busy.get((kind, name)) if store is not None else None
    return {
        "kind": kind, "name": name, "title": title, "text": now.text, "chars": len(now.text),
        "placeholders": sorted(files.placeholders(now.text)), "version": now.version, "edited": now.edited,
        "from_app": now.from_app,
        "shipped_newer": now.shipped_newer, "busy": instruction_files.busy_words(busy) if busy else None,
    }


# --- who does a step, in words ------------------------------------------------------------------


def role_table(rows: list[RoleDef]) -> dict[str, tuple[str, str]]:
    """`{key: (who, about)}`: the built-ins and every role in the table."""
    shipped = {SCOUT: (SCOUT_ROLE.title, SCOUT_ROLE.about)}
    return {**BUILT_IN_ROLES, **shipped, **{r.key: (r.title, r.about or "") for r in rows}}


def _role_of(step: dict, roles: dict[str, tuple[str, str]]) -> str:
    """The role of a step: the one it names, else the lane's (a lane that starts with a role's title is that role's),
    else the app itself."""
    if step["role"] in roles:
        return step["role"]
    lane = step["who"].lower()
    for key, (title, _about) in roles.items():
        if key not in BUILT_IN_ROLES and lane.startswith(title.lower()):
            return key
    if lane.startswith("owner") or lane.startswith("you"):
        return "household"
    return "porter"


def is_agent_step(step: dict, roles: dict[str, tuple[str, str]]) -> bool:
    return step["type"] == "task" and _role_of(step, roles) not in BUILT_IN_ROLES


def sent_for(
    step: dict, roles: dict[str, tuple[str, str]], store: instruction_files.Store | None = None,
) -> dict | None:
    """What an agent is sent at this step, from the files: the step's instructions, the response template, and the
    parts of the hand-out (field by field). None when no agent is asked."""
    if not is_agent_step(step, roles):
        return None
    role = _role_of(step, roles)
    instr = (
        file_view("instructions", step["instructions"], "Instructions for this step", store)
        if step["instructions"] else None
    )
    templ = file_view("templates", step["template"], "How it answers", store) if step["template"] else None
    fields = [
        {"key": k, "means": CONTEXT_MEANS.get(k, ""), "group": "what the scout is handed"}
        for k in SENT_FIELDS.get(role, [])
    ]
    shared = [
        file_view("instructions", name, "Shared by every agent that scores a posting", store)
        for name in SHARED_INSTRUCTIONS.get(files.short_name("instructions", step["instructions"]), [])
    ] if step["instructions"] else []
    return {
        "role_files": shared, "instruction": instr, "template": templ, "fields": fields,
        "budget_chars": REPLY_CEILING_CHARS, "has_example": True,
    }


def step_view(step: dict, roles: dict[str, tuple[str, str]], store: instruction_files.Store | None = None) -> dict:
    """One step as the page gets it. `who` is the lane (the actor, in the file's words); `harness_id` is the step's own
    id when an agent is sent something there (what the example and "What was sent" are keyed by), else None."""
    role = _role_of(step, roles)
    return {
        "id": step["id"], "harness_id": step["id"] if is_agent_step(step, roles) else None,
        "title": step["title"], "type": step["type"], "who": step["who"] or roles[role][0], "role": step["role"],
        "who_about": roles[role][1],
        "actor": step["actor"], "trigger": step["trigger"], "preconditions": step["preconditions"],
        "inputs": step["inputs"], "rules": step["rules"], "outputs": step["outputs"],
        "exceptions": step["exceptions"], "fired_by": step["fired_by"], "enters_at": step["enters_at"],
        "owner_sees": step["owner_sees"], "source": step["source"], "criteria": step["criteria"],
        "trigger_kind": step["trigger_kind"], "trigger_detail": step["trigger_detail"],
        "trigger_subtype": step["trigger_subtype"], "calls": step["calls"], "next": step["next"],
        "sent": sent_for(step, roles, store),
    }


def process_tree(roles: dict[str, tuple[str, str]], store: instruction_files.Store | None = None) -> list[dict]:
    """Every process the page draws, as a tree read from the files: `[{id (the process key), slug, name, about, file,
    key, top_level, children (process ids, in the order the process first calls them), steps, process_key,
    data_model}]`, the top-level processes first. A process another calls is under each that calls it and is still one
    page. A step's `calls` is the called process's id."""
    found: list[dict] = []
    for file in process_files():
        head = process_head(file)
        found.append({
            "id": head["key"], "slug": slug(head["name"]) or slug(head["key"]), "key": head["key"],
            "name": head["name"], "about": head["about"], "file": file,
        })
    by_key = {p["key"]: p["id"] for p in found}
    called: set[str] = set()
    for p in found:
        p["steps"] = [step_view(s, roles, store) for s in bpmn_steps(p["file"])]
        p["process_key"] = p["key"]
        defn = loaded_process(p["key"])
        p["data_model"] = defn.data_model.listing() if defn is not None else []
        for step in p["steps"]:
            if defn is not None and step["type"] == "decision":
                element = defn.elements.get(step["id"])
                if element is not None and element.kind == GATEWAY:
                    step["ways"] = ways_of(defn, step["id"])
        children: list[str] = []
        for step in p["steps"]:
            child = by_key.get(step["calls"]) if step["calls"] else None
            step["calls"] = child
            if child and child != p["id"]:
                called.add(child)
                if child not in children:
                    children.append(child)
        p["children"] = children
    for p in found:
        p["top_level"] = p["id"] not in called
    return sorted(found, key=lambda p: not p["top_level"])


async def read_versions(session: AsyncSession) -> None:
    """Make the process library hold the owner's versions as the database has them now
    (`process_engine.versions.current_for`): the other program may have saved one since this one last looked, and
    neither tells the other. Whatever reads the process files as they run (`process_tree`,
    `process_steps.paired_names`) is called after this. One small read when nothing was saved."""
    await proc_versions.current_for(session, edit_process.entity())


async def newest_stored_texts(session: AsyncSession) -> dict[str, str]:
    """The text of the owner's newest saved version of each process, by process key, read from the database alone (no
    process library, nothing registered). A process the owner never changed is not in it. The newest saved version is
    taken whether or not it still loads under this release: the page only reads which files its steps name."""
    entity = edit_process.entity()
    newest = (
        select(ProcVersion.process_key, func.max(ProcVersion.number).label("number"))
        .where(ProcVersion.entity_id == entity).group_by(ProcVersion.process_key).subquery()
    )
    rows = await session.execute(
        select(ProcVersion.process_key, ProcVersion.xml)
        .join(newest, and_(ProcVersion.process_key == newest.c.process_key, ProcVersion.number == newest.c.number))
        .where(ProcVersion.entity_id == entity)
    )
    return {key: xml for key, xml in rows.all()}


async def paired_names_stored(session: AsyncSession, kind: str, name: str) -> list[str]:
    """`process_steps.paired_names`, out of the process files as the database holds them now (`newest_stored_texts`),
    not out of this program's process library. It imports none of the engine's code: the page uses it to size a file
    beside the file an agent is sent with it."""
    texts = stored_playbook_texts(await newest_stored_texts(session))
    return names_paired(pairs_in(texts), kind, name)


async def current_tree(
    session: AsyncSession, roles: dict[str, tuple[str, str]], store: instruction_files.Store | None = None,
) -> list[dict]:
    """`process_tree` of the versions the database holds now (`read_versions` first)."""
    await read_versions(session)
    return process_tree(roles, store)


async def overview(session: AsyncSession, owner: str) -> dict:
    """The whole page: every process (the top-level ones, then the sub-processes they call) with every step's files."""
    roles = role_table(list((await session.scalars(select(RoleDef).order_by(RoleDef.sort, RoleDef.key))).all()))
    in_use = await edit_process.in_use_numbers(session)
    store = await instruction_files.load_store(session, owner)
    return {
        "budget_chars": REPLY_CEILING_CHARS,
        "roles": [{"role": r, "who": who, "about": about} for r, (who, about) in roles.items()],
        "processes": [
            {**_said(p), "version": in_use.get(p["key"], 1)} for p in await current_tree(session, roles, store)
        ],
    }


def _said(process: dict) -> dict:
    return {k: v for k, v in process.items() if k not in ("file", "key")}


# --- a real example ------------------------------------------------------------------------------


def sent_view(row: TaskHandout, changed: list[dict] | None = None) -> dict:
    """A task as it was actually sent (the copy `planengine.handouts` kept): `label` says to whom ("as sent to Scout";
    the page adds the time from `sent_at`), and `changed_files` names the instruction and template files that are
    different now from the ones it was sent with."""
    payload = scrub(row.payload)  # kept clean already; the second lock again on the way out
    agent = row.agent_name or "an agent"
    return {
        "task_id": str(row.task_id), "title": row.title, "payload": payload, "chars": payload_chars(payload),
        "budget_chars": REPLY_CEILING_CHARS,
        "parts": [{"key": k, "chars": payload_chars(v)} for k, v in payload.items()],
        "source": "sent", "label": f"as sent to {agent}", "agent": row.agent_name, "sent_at": iso_utc(row.sent_at),
        "via": handouts.VIA.get(row.via, row.via), "changed_files": changed or [],
        "files": [{"kind": f.get("kind"), "name": f.get("name"), "version": f.get("version")} for f in row.files or []],
    }


async def latest_sent(session: AsyncSession, owner: str, run_id: uuid.UUID) -> TaskHandout | None:
    """The newest copy kept of what this search's agent was sent."""
    return await session.scalar(
        select(TaskHandout).where(TaskHandout.owner == owner, TaskHandout.task_id == run_id)
        .order_by(TaskHandout.sent_at.desc()).limit(1)
    )


async def example_for_step(session: AsyncSession, owner: str, step_id: str) -> dict | None:
    """The most recent real hand-out at this step: the copy of what it was sent ("as sent to Scout"); None if there
    is none yet."""
    row = await session.scalar(
        select(TaskHandout).where(TaskHandout.owner == owner, TaskHandout.step == step_id)
        .order_by(TaskHandout.sent_at.desc()).limit(1)
    )
    return sent_view(row, await handouts.changed_since(session, owner, row.files)) if row is not None else None


async def agent_step_ids(session: AsyncSession) -> set[str]:
    roles = role_table(list((await session.scalars(select(RoleDef))).all()))
    return {
        s["harness_id"] for p in await current_tree(session, roles) for s in p["steps"] if s["harness_id"]
    }


async def sent_for_task(session: AsyncSession, owner: str, run_id: uuid.UUID) -> dict | None:
    """What was sent for one search: its step with the files, and the payload as it was sent. None when the scout was
    never sent that search's task."""
    kept = await latest_sent(session, owner, run_id)
    if kept is None or not kept.step:
        return None
    roles = role_table(list((await session.scalars(select(RoleDef))).all()))
    store = await instruction_files.load_store(session, owner)
    changed = await handouts.changed_since(session, owner, kept.files)
    for process in await current_tree(session, roles, store):
        step = next((s for s in process["steps"] if s["harness_id"] == kept.step), None)
        if step is not None:
            return {"process": process["id"], "step": step, "example": sent_view(kept, changed)}
    return {"process": kept.process, "step": None, "example": sent_view(kept, changed)}


__all__ = [
    "BUILT_IN_ROLES", "overview", "process_tree", "current_tree", "read_versions", "example_for_step", "sent_for_task",
    "scrub", "bpmn_steps", "paired_names_stored", "newest_stored_texts",
    "step_spec", "agent_step_ids",
]
