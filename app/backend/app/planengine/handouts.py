"""A copy of what the scout was actually sent ("See the harness", exactly as sent).

Every time the scout asks for its task and a search is open, `jobs.profile` calls `sent` with the reply it is about to
return. `record` keeps that payload with anything secret-looking removed (`security.scrub`), the instruction and
template files the step names (by hash, so a later edit to a file is visible), which agent got it and when.
"Agents > How they work" reads the row back: "as sent to Scout at 9:40".

Bounded: the newest 200 rows per owner and 30 per step are kept. The same run with the same payload handed out again
only moves the time on its row. Recording never stops a hand-out: it runs inside a savepoint, and a failure is logged
and dropped. Services flush, routes commit: nothing here commits.

Adapted from another project's code (app/PROVENANCE.md): the task is the search run, the one hand-out path is
`jobs.profile`, and a step's files come from the process file's own `porter:instructions` and `porter:template`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.process_steps import step_spec
from app.agents.security import scrub
from app.harness import instructions as files
from app.jobsearch.work import instruction_files, queue
from app.models.agent import AgentConnection
from app.models.handout import TaskHandout
from app.planengine.budget import payload_chars

log = logging.getLogger(__name__)

#: The hand-out paths, in the owner's words: what `TaskHandout.via` holds, and what the page says. `task_next` is the
#: site projects' door: the scout asked for its next task and was handed a plan to write, an item or an act to run.
VIA = {
    "jobs_profile": "asked for its task (jobs.profile)",
    "task_next": "asked for its next task (task.next)",
}
#: What is kept: the newest this many per owner and per step.
KEEP_PER_OWNER = 200
KEEP_PER_STEP = 30
INSTRUCTION, TEMPLATE = "instruction", "template"


def payload_sha(payload: Any) -> str:
    """A fingerprint of a payload that doesn't depend on key order."""
    text = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_payload(payload: dict) -> dict:
    """The payload as it is kept: anything secret-looking removed, and made plain JSON."""
    return json.loads(json.dumps(scrub(payload), default=str, ensure_ascii=False))


async def _file_entry(session: AsyncSession, owner: str, kind: str, folder: str, name: str) -> dict | None:
    """One file as it is in force now (the owner's newest saved version, else the file on disk) and the version it is:
    `{kind, name, sha, version}`. None when there is no such file."""
    try:
        now = await instruction_files.resolve(session, owner, folder, name)
    except (files.UnknownName, instruction_files.FileError, OSError):
        return None
    return {"kind": kind, "name": now.name, "sha": now.sha, "version": now.version}


async def files_for(
    session: AsyncSession, owner: str, process: str | None, step: str | None, spec: dict | None = None,
) -> list[dict]:
    """`[{kind: instruction | template, name, sha, version}]`: the files the step's agent was sent with, as the process
    file names them (`porter:instructions`, `porter:template`), at the version in force (the owner's newest saved one,
    else the shipped file, version 1). Empty for a step the process doesn't name. `spec` is `{instructions, template}`
    when the caller already holds the names, so
    the job-search files are not searched for a step they don't hold."""
    if spec is None:
        if not process or not step:
            return []
        try:
            spec = step_spec(process, step)
        except Exception:  # a process file that can't be read leaves the copy without its file list, nothing more
            log.warning("couldn't read the files a hand-out used (%s, %s)", process, step, exc_info=True)
            return []
    entries = [
        await _file_entry(session, owner, INSTRUCTION, "instructions", spec["instructions"])
        if spec.get("instructions") else None,
        await _file_entry(session, owner, TEMPLATE, "templates", spec["template"]) if spec.get("template") else None,
    ]
    return [e for e in entries if e is not None]


async def changed_since(session: AsyncSession, owner: str, kept: list[dict]) -> list[dict]:
    """Which of a hand-out's files are different now: `[{kind, name, now: "changed" | "gone", version}]`, `version`
    being the one in force now."""
    out = []
    for entry in kept or []:
        folder = "templates" if entry.get("kind") == TEMPLATE else "instructions"
        now = await _file_entry(session, owner, entry.get("kind", ""), folder, entry.get("name", ""))
        if now is None:
            out.append({"kind": entry.get("kind"), "name": entry.get("name"), "now": "gone"})
        elif now["sha"] != entry.get("sha"):
            out.append({"kind": entry.get("kind"), "name": entry.get("name"), "now": "changed",
                        "version": now["version"]})
    return out


async def _prune(session: AsyncSession, owner: str, step: str | None) -> None:
    scopes = [(TaskHandout.owner == owner, KEEP_PER_OWNER)]
    if step:
        scopes.append(((TaskHandout.owner == owner) & (TaskHandout.step == step), KEEP_PER_STEP))
    for where, keep in scopes:
        older = (
            select(TaskHandout.id).where(where).order_by(TaskHandout.sent_at.desc(), TaskHandout.id.desc())
            .offset(keep)
        )
        with queue.exempt("the record of what was handed out is kept for the screen only: no loop reads it"):
            await session.execute(delete(TaskHandout).where(TaskHandout.id.in_(older)))


async def _record(
    session: AsyncSession, owner: str, run_id: uuid.UUID | None, title: str, payload: dict, process: str,
    step: str | None, via: str, connection: AgentConnection | None, now: datetime,
    plan_task_id: uuid.UUID | None = None, spec: dict | None = None,
) -> TaskHandout:
    clean = clean_payload(payload)
    sha = payload_sha(clean)
    whose = TaskHandout.task_id == run_id if run_id is not None else TaskHandout.plan_task_id == plan_task_id
    again = await session.scalar(
        select(TaskHandout).where(whose, TaskHandout.payload_sha == sha)
        .order_by(TaskHandout.sent_at.desc()).limit(1)
    )
    if again is not None:
        again.sent_at = now  # the same task, the same words: only the time moves
        await session.flush()
        return again
    row = TaskHandout(
        owner=owner, task_id=run_id, plan_task_id=plan_task_id,
        connection_id=connection.id if connection is not None else None,
        agent_name=connection.name if connection is not None else None, via=via, process=process, step=step,
        title=str(scrub(title or ""))[:300], payload=clean, payload_sha=sha, chars=payload_chars(clean),
        files=await files_for(session, owner, process, step, spec), sent_at=now,
    )
    session.add(row)
    await session.flush()
    await _prune(session, owner, step)
    return row


async def record(
    session: AsyncSession, owner: str, run_id: uuid.UUID | None, payload: dict, *, title: str, process: str,
    step: str | None, via: str = "jobs_profile", connection: AgentConnection | None = None,
    now: datetime | None = None, plan_task_id: uuid.UUID | None = None, spec: dict | None = None,
) -> TaskHandout | None:
    """Keep a copy of `payload`, which is about to be handed to an agent: for this search (`run_id`), or, for a site
    project's door (`via="task_next"`), for this task (`plan_task_id`, with `run_id` None) and the step spec stamped
    on it. Returns the row, or None when it couldn't be kept (logged): a failure here never stops the hand-out."""
    await session.flush()  # whatever is pending is the caller's own; the savepoint holds only this
    try:
        if via not in VIA:
            raise ValueError(f"unknown hand-out path {via!r}")
        if run_id is None and plan_task_id is None:
            raise ValueError("a hand-out is for a search run or a site project's task")
        async with session.begin_nested():
            return await _record(
                session, owner, run_id, title, payload, process, step, via, connection, now or datetime.now(UTC),
                plan_task_id, spec,
            )
    except Exception:  # a copy that can't be kept must not stop the hand-out
        log.warning("couldn't keep a copy of the hand-out for %s", run_id or plan_task_id, exc_info=True)
        return None


async def sent(
    session: AsyncSession, owner: str, run_id: uuid.UUID | None, payload: dict, *, title: str, process: str,
    step: str | None, via: str = "jobs_profile", connection: AgentConnection | None = None,
    plan_task_id: uuid.UUID | None = None, spec: dict | None = None,
) -> dict:
    """`payload` itself, after keeping a copy of it: wrap the payload the hand-out path is about to return."""
    await record(
        session, owner, run_id, payload, title=title, process=process, step=step, via=via, connection=connection,
        plan_task_id=plan_task_id, spec=spec,
    )
    return payload
