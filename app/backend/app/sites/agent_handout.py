"""The engine's agent-step hand-out: what the scout is sent when a process reaches its step.

A process reaches an agent step (the scout writes a plan, does an item, learns from a round) and waits for it. The
engine does not talk to the agent: the work is a task the project already has (`PlanTask`), and it goes out through the
doors that always handed it out (`task.next`), each of which keeps a copy with `planengine.handouts`. What the engine
adds is that **the step decides what is sent**:

- the step's `porter:role` says who may take it (and, at the end, who may finish it);
- its `porter:instructions` and `porter:template` are stamped on the task (`task.data["handout"]`), and the views the
  doors build read them from there (`named_instruction`, `named_template`);
- the payload is built the way the door builds it and checked against `REPLY_CEILING_CHARS`, the most the door sends,
  before the engine waits, so a step whose payload can't go out fails (the run is held at that step and the Inbox says
  so, with its size) instead of being refused at the door each time the scout asks. It is not checked against
  `AGENT_PAYLOAD_BUDGET_CHARS`: that is the size of a batch of items, and what makes a step's payload large is the
  owner's own words and files, which go whole. A large note is accepted whole, and the plan is still asked for.

A task with no stamp (one made before the process ran it) is sent exactly as it always was. The stamp is the only change
to the record. Copied from another project's code (app/PROVENANCE.md): the roles are the scout's
(the older planner, doer and specialist words in a process file mean the scout too) and there is no Teacher task.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from process_engine.errors import EngineError
from process_engine.models import ProcWait
from sqlalchemy.ext.asyncio import AsyncSession

from app.harness import instructions as files
from app.planengine.budget import REPLY_CEILING_CHARS, payload_chars
from app.sites.models import PlanItemAction, PlanTask

#: Where on the task the step's spec is kept.
STAMP = "handout"
SCOUT = "scout"
#: The roles a step may name, in the role the connection must hold: the scout is the only one.
ROLE_OF = {SCOUT: SCOUT}
#: The kind of task the scout takes at the step the engine hands it, so a step can't be stamped on the wrong kind of
#: record (an item is a `do` task, a plan or Act their own).
TASK_ACTIONS = {SCOUT: (PlanItemAction.do, PlanItemAction.plan, PlanItemAction.act)}


class HandOutError(EngineError):
    """The step can't go out: it names a role or file that isn't there, or its payload doesn't fit the budget."""


class MissingFile(HandOutError):
    """The step names an instruction or template file that isn't there (any more). A hand-out that can do without the
    stamp (a task still goes out without one, as it always did) says so by catching this and no other."""


@dataclass(frozen=True)
class StepSpec:
    """What a step says it hands out: who, the instruction file and the response template (names, or None)."""

    element: str
    role: str
    role_word: str
    instructions: str | None
    template: str | None

    def as_stamp(self) -> dict:
        return {"step": self.element, "role": self.role_word, "instructions": self.instructions,
                "template": self.template}


def spec_of(wait: ProcWait) -> StepSpec:
    """The step a waiting agent wait is, read from what the engine recorded (`wait.payload`): refused in plain words
    when it names a role or a file that isn't there."""
    payload = wait.payload or {}
    word = str(payload.get("role") or "")
    role = ROLE_OF.get(word)
    if role is None:
        raise HandOutError(f"the step '{payload.get('name') or wait.element_id}' names no role an agent can have")
    names: dict[str, str | None] = {}
    for kind, field in (("instructions", "instructions"), ("templates", "template")):
        ref = payload.get(field)
        if not ref:
            names[field] = None
            continue
        short = files.short_name(kind, str(ref))
        if short not in files.names(kind):
            raise MissingFile(
                f"the step '{payload.get('name') or wait.element_id}' names {short!r}, which isn't a file")
        names[field] = short
    return StepSpec(wait.element_id, role, word, names["instructions"], names["template"])


def stamp_of(task: PlanTask) -> dict | None:
    stamp = (task.data or {}).get(STAMP)
    return stamp if isinstance(stamp, dict) and stamp.get("step") else None


def stamp(task: PlanTask, spec: StepSpec) -> None:
    """Put the step's spec on the task the agent will take. Idempotent."""
    task.data = {**(task.data or {}), STAMP: spec.as_stamp()}


# --- the instruction and template the views read ------------------------------------------------------------------


def named_template(task: PlanTask, default: str | None) -> str | None:
    """The response template a view names: the step's, when the step stamped one."""
    mark = stamp_of(task)
    return files.short_name("templates", mark["template"]) if mark and mark.get("template") else default


def named_instruction(task: PlanTask, default: str | None) -> str | None:
    """The instruction file a view reads: the step's, when the step stamped one."""
    mark = stamp_of(task)
    return files.short_name("instructions", mark["instructions"]) if mark and mark.get("instructions") else default


# --- handing a step out -------------------------------------------------------------------------------------------


async def hand_out(
    session: AsyncSession, wait: ProcWait, task: PlanTask,
    build_view: Callable[[AsyncSession, PlanTask], Awaitable[dict]],
) -> StepSpec:
    """The engine reached an agent step and `task` is the record the agent takes. Stamp the step's spec on it, build
    the payload the door will send, and check the door will send it: no more than `REPLY_CEILING_CHARS`, whatever the
    owner's words and files in it come to (nothing is cut to fit). Raises `HandOutError` (the run is then held at
    the step and the Inbox says why, with the size) when the step can't go out as it is. The door keeps the copy
    (`handouts`) when the scout takes the task."""
    spec = spec_of(wait)
    if task.action not in TASK_ACTIONS[spec.role]:
        raise HandOutError(
            f"the step '{wait.payload.get('name') or wait.element_id}' is for the {spec.role_word}, "
            f"but its task is a {task.action.value} task"
        )
    stamp(task, spec)
    await session.flush()
    view = await build_view(session, task)
    size = payload_chars({"ok": True, "task": view})
    if size > REPLY_CEILING_CHARS:
        raise HandOutError(
            f"what the {spec.role_word} would be sent for '{wait.payload.get('name') or wait.element_id}' is {size:,} "
            f"characters, over the {REPLY_CEILING_CHARS:,} an agent is sent at most; nothing was cut"
        )
    return spec
