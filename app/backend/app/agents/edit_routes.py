"""Changing how it works: the routes behind How they work's Edit (docs/PRD.md path 16). Routes for a process's file,
its check, its edits and its versions; there is one owner, so no account or member id is in the address. There is no
approval step: a save is the change. Routes commit; the services flush.

    GET  /harness/processes/{key}/bpmn                      the file that runs, with its version
    POST /harness/processes/{key}/validate                  {xml}: can the engine run it? (nothing is kept)
    POST /harness/processes/{key}/edits                     {xml, why, based_on}: the next version
    GET  /harness/processes/{key}/versions                  the history, newest first
    GET  /harness/processes/{key}/versions/{n}              one earlier version's file
    POST /harness/processes/{key}/rollback                  {to, why?}: a new version with an earlier one's text

An `EditError` answers with its own status (`edit_process`: 404 for a process there is none of, 422 for a file that
cannot run, 409 for one made from a version that has moved on).
"""

from __future__ import annotations

from fastapi import APIRouter
from process_engine import versions
from pydantic import BaseModel, Field

from app.agents import edit_process
from app.db import ReadSession, Session

router = APIRouter(prefix="/api/v1", tags=["agents"])


class XmlIn(BaseModel):
    xml: str = Field(min_length=1)


class EditIn(XmlIn):
    why: str = ""
    #: The version the editor was showing; when the version in use has moved on the save is refused with a 409.
    based_on: int | None = Field(default=None, ge=0)


class RollbackIn(BaseModel):
    to: int = Field(ge=1)
    why: str = ""


# --- a process ---------------------------------------------------------------------------------------------


@router.get("/harness/processes/{key}/bpmn")
async def process_bpmn(key: str, session: ReadSession):
    """The BPMN file of a process as it runs (its newest version that loads): what the editor starts from. `number` is
    the version; an edit sent back says it was `based_on` that number. `note` is plain words when the newest version no
    longer works with this release and an earlier one runs instead, else null."""
    return await edit_process.current(session, key)


@router.post("/harness/processes/{key}/validate")
async def process_validate(key: str, body: XmlIn, session: ReadSession):
    """Asks the engine whether an edited file is one it can run, while it is being written. Nothing is kept. `problems`
    are the engine's own words, one to a line; `changes` says what differs from the version in use."""
    try:
        edit_process.known(key)
        _new, changes = await edit_process.check(session, key, body.xml)
    except versions.VersionError as exc:
        return {"ok": False, "problems": [p for p in str(exc).split("; ") if p], "changes": []}
    except edit_process.NotFound:
        raise
    except edit_process.Unchanged as exc:
        return {"ok": True, "problems": [], "changes": [], "note": str(exc)}
    except edit_process.EditError as exc:
        return {"ok": False, "problems": [str(exc)], "changes": []}
    return {"ok": True, "problems": [], "changes": changes}


@router.post("/harness/processes/{key}/edits")
async def process_edit(key: str, body: EditIn, session: Session, reader: ReadSession):
    """Saves the owner's edit of a process's steps as its next version. The engine's loader checks it first: a file it
    cannot run is refused with every problem. The next run uses it; a run going now finishes as it started."""
    await versions.current_for(session, edit_process.entity())  # the save is checked against the other processes in use
    await edit_process.save(session, key, body.xml, body.why, body.based_on)
    await session.commit()
    return await edit_process.history(reader, key)  # read on a reader, after the commit


@router.get("/harness/processes/{key}/versions")
async def process_versions(key: str, session: ReadSession):
    return await edit_process.history(session, key)


@router.get("/harness/processes/{key}/versions/{number}")
async def process_version(key: str, number: int, session: ReadSession):
    return await edit_process.version_xml(session, key, number)


@router.post("/harness/processes/{key}/rollback")
async def process_rollback(key: str, body: RollbackIn, session: Session, reader: ReadSession):
    """Go back to an earlier version: a new version with that one's text. New work uses it; running work finishes as it
    started."""
    await versions.current_for(session, edit_process.entity())  # as for an edit
    await edit_process.restore(session, key, body.to, body.why)
    await session.commit()
    return await edit_process.history(reader, key)  # read on a reader, after the commit
