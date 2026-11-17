"""The "How they work" tab on the Agents page: the processes the app runs, what each step sends an agent, and a real
example. One owner, so no account or member id is in the address; made in
`app.agents.how_they_work` and `app.agents.live_view`. Read-only, but for the instruction and template files, which the
owner edits here: each save is a new version (`app.jobsearch.work.instruction_files`)."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import how_they_work as how
from app.agents import live_view as live
from app.db import ReadSession, Session
from app.errors import NotFound, ServerFault
from app.harness import HarnessError
from app.jobsearch import off_turn
from app.jobsearch.work import instruction_files
from app.settings import settings

router = APIRouter(prefix="/api/v1", tags=["agents"])


@router.get("/harness")
async def how_they_work(session: ReadSession):
    """The processes, step by step, with the files an agent is sent at each step."""
    try:
        return await how.overview(session, settings.owner)
    except HarnessError as exc:
        raise ServerFault(f"the instruction files can't be read: {exc}") from exc


@router.get("/harness/steps/{step_id}/example")
async def step_example(step_id: str, session: ReadSession):
    """The most recent real hand-out at this step, as it was sent (`example` is null when there is none)."""
    if step_id not in await how.agent_step_ids(session):
        raise NotFound("no agent is sent anything at this step")
    return {"step_id": step_id, "example": await how.example_for_step(session, settings.owner, step_id)}


@router.get("/harness/tasks/{task_id}")
async def task_sent(task_id: uuid.UUID, session: ReadSession):
    """What was sent for one search: its step, with the files, and that search's own payload."""
    out = await how.sent_for_task(session, settings.owner, task_id)
    if out is None:
        raise NotFound("no agent was sent this task")
    return out


@router.get("/harness/runs")
async def process_runs(session: ReadSession):
    """The runs of the search process, newest first: where each is in its process."""
    return {"runs": await live.runs_for(session, settings.owner)}


@router.get("/harness/runs/{instance_id}")
async def process_run(instance_id: uuid.UUID, session: ReadSession):
    """One run: its current step, the steps it passed, and the copy of what was sent at each agent step."""
    out = await live.run_detail(session, settings.owner, instance_id)
    if out is None:
        raise NotFound("no such run")
    return out


# --- the instruction and template files: edit, history, restore (docs/PRD.md 16.1) -------------------------------
# A name there isn't is a 404 (`UnknownName`); anything else the file can't be changed by (nothing changed, an agent is
# working with it, a placeholder the app doesn't fill in) is a 409 with the reason in plain words (`FileError`). No text
# or why has a limit; the one refusal is the room an agent has (413): an agent is sent a step's instruction file and
# template in one reply, so a file that with the file sent beside it would pass what one reply leaves for the two is
# refused, with both sizes, before anything is saved (`owner_text`).


class SaveFile(BaseModel):
    text: str
    #: The one-line why that is kept with the version.
    why: str = ""


class RestoreFile(BaseModel):
    version: int = Field(ge=1)


async def _paired_size(session: AsyncSession, kind: str, name: str) -> int | None:
    """The size, as saved now, of the file an agent is sent with this one: the template of the step that names an
    instruction file, the instruction file of the step that names a template (`how.paired_names_stored`). None when no
    step sends the file with another, so it is checked alone. Should a file be named with more than one, the largest
    counts. The process files are read as the database holds them, with no process library: the page loads none of the
    engine's code."""
    other = "templates" if kind == "instructions" else "instructions"
    sizes = [
        len(await instruction_files.read(session, settings.owner, other, partner))
        for partner in await how.paired_names_stored(session, kind, name)
    ]
    return max(sizes) if sizes else None


async def _file_reply(session: AsyncSession, kind: str, name: str) -> dict:
    """The file as in force (its text and version) and its history, for the page to show after a change."""
    store = await instruction_files.load_store(session, settings.owner)
    file = how.file_view(kind, name, "", store)
    file.pop("title")
    return {"file": file, "history": await instruction_files.history(session, settings.owner, kind, name)}


@router.get("/harness/files/{kind}/{name}")
async def file_history(kind: str, name: str, session: ReadSession):
    """A file as in force and every version of it (number, when, who, why), newest first."""
    return await _file_reply(session, kind, instruction_files.file_name(kind, name))


@router.get("/harness/files/{kind}/{name}/versions/{version}")
async def file_version(kind: str, name: str, version: int, session: ReadSession):
    """One earlier version of a file, with its text."""
    return await instruction_files.version_text(session, settings.owner, kind, name, version)


@router.put("/harness/files/{kind}/{name}")
async def save_file(kind: str, name: str, body: SaveFile, session: Session):
    """Save an edit of a file as its next version, with the owner's one-line why. A 413 when the file, with the file an
    agent is sent beside it, is more than one reply holds; nothing is saved."""
    name = instruction_files.file_name(kind, name)
    async with off_turn.reading(session) as reader:  # the room beside is read apart from the writer's turn
        beside = await _paired_size(reader, kind, name)
    await instruction_files.save(session, settings.owner, kind, name, body.text, body.why, beside=beside)
    await session.commit()
    async with off_turn.reading(session) as reader:  # the reply is read once the write is committed
        return await _file_reply(reader, kind, name)


@router.post("/harness/files/{kind}/{name}/restore")
async def restore_file(kind: str, name: str, body: RestoreFile, session: Session):
    """Bring an earlier version back: its text is saved as the next version, unless it no longer fits beside the file
    an agent is sent with it (a 413, as for a save)."""
    name = instruction_files.file_name(kind, name)
    async with off_turn.reading(session) as reader:  # the room beside is read apart from the writer's turn
        beside = await _paired_size(reader, kind, name)
    await instruction_files.restore(session, settings.owner, kind, name, body.version, beside=beside)
    await session.commit()
    async with off_turn.reading(session) as reader:  # the reply is read once the write is committed
        return await _file_reply(reader, kind, name)
