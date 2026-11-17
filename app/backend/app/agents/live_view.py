"""The live view of a running process (docs/PRD.md): where one run is in its process, the step
it is at, the steps it passed, and for each agent step it passed the copy of what was sent.

Read-only, from the engine's own rows (`process_engine.models`: the instance, its token, its open waits and its
history) and the hand-out copies (`planengine.handouts`, the table `task_handouts`). Nothing here writes, and nothing
here starts or moves a run. "How they work" draws the process from its file; this says which steps of that drawing a run
has been through. The words here are the owner's: no ids, no element names, no file names.

A search run's process instance has the subject `jobsearch:<search run id>`, and a sub-run belongs to its parent's.
Anything whose subject isn't that is never shown. Adapted from another project's code (app/PROVENANCE.md); here there is
one owner, runs are found by their search run,
and the engine runs live.
"""

from __future__ import annotations

import re
import uuid

from process_engine.models import (
    CANCELLED,
    COMPLETED,
    FAILED,
    OPEN,
    RUNNING,
    WAITING,
    ProcHistory,
    ProcInstance,
    ProcToken,
    ProcWait,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import edit_process
from app.agents import how_they_work as how
from app.models.agent import RoleDef
from app.models.handout import TaskHandout
from app.timeutil import iso_utc

#: The subject a search's process instance carries (`app.jobsearch.missions.search_run.start_round`).
SUBJECT_PREFIX = "jobsearch:"
#: How many runs one list shows, newest first.
LIST_LIMIT = 8
#: The most hand-outs shown at one step.
PER_STEP = 3
#: The history rows that say the run left the step they name.
LEAVES = ("move", "decision", "error", "complete")

STATUS_WORDS = {
    RUNNING: "Working through its steps",
    WAITING: "Waiting at a step",
    COMPLETED: "Finished",
    FAILED: "Stopped before the end",
    CANCELLED: "Stopped",
}
#: What a waiting run waits for, in the owner's words, by the kind of wait.
WAIT_WORDS = {
    "agent": "an agent's answer",
    "user": "an answer from you",
    "call": "another process to finish",
    "catch_message": "something to happen",
    "catch_timer": "a set time",
    "boundary_timer": "a set time",
    "shadow_action": "the app to finish this step",
}


def _plain(title: str | None) -> str | None:
    """A step's title without the specification's number in front ("PR-4 Write the plan" is "Write the plan")."""
    return re.sub(r"^[A-Z]{1,4}-\d+[A-Z]?\s+", "", title) if title else title


def run_id_of(subject: str | None) -> uuid.UUID | None:
    """The search run a subject names, or None when it isn't a search's."""
    if not subject or not subject.startswith(SUBJECT_PREFIX):
        return None
    try:
        return uuid.UUID(subject[len(SUBJECT_PREFIX):])
    except ValueError:
        return None


async def _page_processes(session: AsyncSession) -> dict[str, dict]:
    """The engine's process key -> the page's process (its id, slug, name, and its steps by id)."""
    roles = how.role_table(list((await session.scalars(select(RoleDef))).all()))
    return {
        p["key"]: {"id": p["id"], "slug": p["slug"], "name": p["name"], "steps": {s["id"]: s for s in p["steps"]}}
        for p in await how.current_tree(session, roles)
    }


async def _root(session: AsyncSession, instance: ProcInstance) -> ProcInstance:
    """The run at the top of a sub-run's chain (a run with no parent is its own top)."""
    seen = 0
    while instance.parent_id is not None and seen < 10:
        parent = await session.get(ProcInstance, instance.parent_id)
        if parent is None:
            break
        instance, seen = parent, seen + 1
    return instance


async def _current(session: AsyncSession, instance: ProcInstance) -> tuple[str | None, list[ProcWait]]:
    """The step the run's token is at (even after the run ended: its last step), and what it waits for."""
    token = await session.scalar(
        select(ProcToken).where(ProcToken.instance_id == instance.id).order_by(ProcToken.created_at, ProcToken.id)
        .limit(1)
    )
    waits = list((await session.scalars(select(ProcWait).where(
        ProcWait.instance_id == instance.id, ProcWait.status == OPEN))).all())
    waits.sort(key=lambda w: (w.kind == "boundary_timer", w.created_at, w.element_id))
    return (token.element_id if token else None), waits


def _row(instance: ProcInstance, page: dict, step_title: str | None, version: int | None = None) -> dict:
    return {
        "id": str(instance.id), "process": page["id"], "name": page["name"], "slug": page["slug"],
        "version": version,  # the version of the process the run started on (docs/PRD.md 16.2)
        "alongside": False, "status": instance.status,
        "status_words": STATUS_WORDS.get(instance.status, instance.status),
        "step_title": step_title,
        "started_at": iso_utc(instance.created_at),
        "ended_at": iso_utc(instance.ended_at) if instance.ended_at else None,
    }


async def _rows(session: AsyncSession, instances: list[ProcInstance]) -> list[dict]:
    pages = await _page_processes(session)
    ran_on = await edit_process.run_versions(session, instances)
    rows = []
    for inst in instances:
        page = pages.get(inst.process_key)
        if page is None:
            continue
        element, _waits = await _current(session, inst)
        step = page["steps"].get(element or "")
        rows.append(_row(inst, page, _plain(step["title"]) if step else None, ran_on.get(inst.id)))
    return rows


async def runs_for(session: AsyncSession, owner: str, *, limit: int = LIST_LIMIT) -> list[dict]:
    """The search runs' processes, newest first."""
    found = (await session.scalars(
        select(ProcInstance)
        .where(ProcInstance.parent_id.is_(None), ProcInstance.subject.like(SUBJECT_PREFIX + "%"))
        .order_by(ProcInstance.created_at.desc(), ProcInstance.id).limit(limit)
    )).all()
    return await _rows(session, list(found))


async def _handouts(
    session: AsyncSession, owner: str, root: ProcInstance, steps: dict[str, dict], reached: set[str],
) -> dict[str, list[dict]]:
    """For each agent step the run reached: the hand-outs kept of what was sent for this search, newest first.
    `{step id: [{task_id, agent, sent_at, title}]}`. Each opens "What was sent" through the search's own address."""
    run = run_id_of(root.subject)
    agent_steps = {sid for sid, s in steps.items() if s["harness_id"] and sid in reached}
    if run is None or not agent_steps:
        return {}
    rows = (await session.scalars(
        select(TaskHandout).where(
            TaskHandout.owner == owner, TaskHandout.task_id == run, TaskHandout.step.in_(list(agent_steps))
        ).order_by(TaskHandout.sent_at.desc(), TaskHandout.id)
    )).all()
    out: dict[str, list[dict]] = {}
    for row in rows:
        kept = out.setdefault(row.step, [])
        if len(kept) < PER_STEP:
            kept.append({
                "task_id": str(row.task_id), "agent": row.agent_name, "title": row.title,
                "sent_at": iso_utc(row.sent_at),
            })
    return out


async def run_detail(session: AsyncSession, owner: str, instance_id: uuid.UUID) -> dict | None:
    """One run, for the page that draws its process: `{..row, current (the step id or None), passed: [{step, at}],
    waiting_for, sent: {step id: [hand-outs]}, parts (its sub-runs), others (the other search runs)}`; None when there
    is no such run."""
    instance = await session.get(ProcInstance, instance_id)
    if instance is None or run_id_of((await _root(session, instance)).subject) is None:
        return None
    pages = await _page_processes(session)
    page = pages.get(instance.process_key)
    if page is None:
        return None
    root = await _root(session, instance)
    history = list((await session.scalars(
        select(ProcHistory).where(ProcHistory.instance_id == instance.id).order_by(ProcHistory.seq))).all())
    element, waits = await _current(session, instance)
    steps = page["steps"]
    passed: list[dict] = []
    seen: set[str] = set()
    for h in history:
        # A step is passed once the run has left it; the start event is passed as soon as the run begins.
        left = h.to_element if h.kind == "start" else h.from_element if h.kind in LEAVES else None
        if left in steps and left not in seen and left != element:
            seen.add(left)
            passed.append({"step": left, "at": iso_utc(h.at)})
    current = element if element in steps else None
    reached = set(seen) | ({current} if current else set())
    sent = await _handouts(session, owner, root, steps, reached)
    kids = list((await session.scalars(
        select(ProcInstance).where(ProcInstance.parent_id == instance.id)
        .order_by(ProcInstance.created_at, ProcInstance.id))).all())
    step = steps.get(current or "")
    others: list[dict] = []
    if instance.parent_id is None:
        others = [r for r in await runs_for(session, owner) if r["id"] != str(instance.id)]
    ran_on = (await edit_process.run_versions(session, [instance])).get(instance.id)
    detail = _row(instance, page, _plain(step["title"]) if step else None, ran_on)
    detail["version_now"] = (await edit_process.in_use_numbers(session)).get(instance.process_key)
    detail.update({
        "current": current,
        "ended": instance.status in (COMPLETED, FAILED, CANCELLED),
        "passed": passed,
        "waiting_for": [
            {"step": w.element_id, "words": WAIT_WORDS.get(w.kind, "something")}
            for w in waits if w.element_id in steps and w.kind != "boundary_timer"
        ],
        "sent": sent,
        "parts": await _rows(session, kids),
        "others": others,
    })
    return detail
