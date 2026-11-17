"""The agents' instruction and template files, editable and versioned (docs/PRD.md 16.1).

Each process step sends an agent an instruction file and a response template (`app/instructions`, `app/templates`,
named by the process file's `porter:instructions` and `porter:template`). The files shipped with the app are version 1.
The owner edits one on How they work; a save adds the next version to `js_instruction_files` (its number, text, who
saved it and why) and never changes an earlier one. What an agent is sent is read here: the newest stored version, else
the file on disk (`read`). Restoring an earlier version saves it as a new version, so it is in the history too.

- **While an agent works with the file.** A file cannot be saved while an agent session that was sent it is still
  running. That is known only where the app keeps a copy of what was sent (`task_handouts`): a scout's search still
  open, or a site project's task in progress. A session of another kind (a reader, the analyst, the writer) is not
  recorded there, so an edit made during one is used from its next session.
- **How long a file is.** As long as the owner writes it, and so is the why. Only the room an agent has refuses a file
  (`owner_text`): an agent is sent the instruction file and the template of a step in one reply, so a file that with the
  file beside it would pass what one reply leaves for the two is refused (413, in words, with both sizes), never cut.
  The routes know which file is beside it (`agents.process_steps`) and pass its size (`beside`); this module stays free
  of `agents/`.
- **A release that ships a different file.** It always wins: `apply_shipped` (run at start-up, which is when a release
  arrives) saves the shipped text as the next version of each file the owner had changed ("Shipped with the app"), so
  the owner's version stays in the history to read or restore. Nothing is asked.

Services flush, routes commit: nothing here commits.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.harness import instructions as files
from app.jobsearch import owner_text
from app.jobsearch.models import InstructionFile, SearchRun
from app.models.handout import TaskHandout
from app.sites.models import PlanTask, PlanTaskStatus
from app.timeutil import iso_utc

KINDS = ("instructions", "templates")
#: Who saved version 1 (the file as shipped) and who saves every other version.
SHIPPED_BY, OWNER_BY = "shipped", "owner"
SHIPPED_WHY = "Shipped with the app."
#: What became of a file that, with the file an agent is sent beside it, is more than one reply holds.
NOT_SAVED = "it was not saved. Nothing was cut."
#: A copy of a hand-out older than this does not count as an agent still working with the file (a search that was
#: never closed must not hold the file for good).
BUSY_FOR = timedelta(hours=6)
#: What a hand-out's file list calls each kind.
_HANDOUT_KIND = {"instruction": "instructions", "template": "templates"}


class FileError(DomainError):
    """A file can't be saved that way; plain words (answered 409)."""

    status = 409


class FileBusy(FileError):
    """An agent is working with the file right now."""


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def label(kind: str) -> str:
    return "instruction file" if kind == "instructions" else "template"


def file_name(kind: str, ref: str) -> str:
    """The file's name, when it is one of the files shipped. Raises `files.UnknownName` (a ValueError) otherwise, and
    for a kind there isn't."""
    if kind not in KINDS:
        raise files.UnknownName(f"there are no {kind!r} files; the kinds are {', '.join(KINDS)}")
    name = files.short_name(kind, ref)
    if name not in files.names(kind):
        files.read(kind, name)  # raises UnknownName, which lists the names there are
    return name


# --- what is in force ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """A file as it is in force: its text, the version number (1 until the owner saves one), whether they have."""

    kind: str
    name: str
    text: str
    version: int
    edited: bool
    #: A release shipped different text from the one the owner's version was made from.
    shipped_newer: bool
    #: The version in force is one a release saved ("Shipped with the app"), not the owner's own.
    from_app: bool = False

    @property
    def sha(self) -> str:
        return sha_of(self.text)


def _resolve(kind: str, name: str, row: InstructionFile | None) -> Resolved:
    disk = files.read(kind, name)
    if row is None:
        return Resolved(kind, name, disk, 1, False, False)
    newer = row.shipped_sha != sha_of(disk) and row.text != disk
    return Resolved(kind, name, row.text, row.version, True, newer, row.saved_by == SHIPPED_BY)


async def newest_map(session: AsyncSession, owner: str) -> dict[tuple[str, str], InstructionFile]:
    """The newest saved version of every file the owner has changed, by `(kind, name)`."""
    top = (
        select(InstructionFile.kind, InstructionFile.name, func.max(InstructionFile.version).label("v"))
        .where(InstructionFile.owner == owner).group_by(InstructionFile.kind, InstructionFile.name).subquery()
    )
    rows = (await session.scalars(
        select(InstructionFile).join(top, and_(
            InstructionFile.kind == top.c.kind, InstructionFile.name == top.c.name, InstructionFile.version == top.c.v,
        )).where(InstructionFile.owner == owner)
    )).all()
    return {(r.kind, r.name): r for r in rows}


async def _newest(session: AsyncSession, owner: str, kind: str, name: str) -> InstructionFile | None:
    return await session.scalar(
        select(InstructionFile).where(
            InstructionFile.owner == owner, InstructionFile.kind == kind, InstructionFile.name == name,
        ).order_by(InstructionFile.version.desc()).limit(1)
    )


async def resolve(session: AsyncSession, owner: str, kind: str, ref: str) -> Resolved:
    """The file `ref` names as it is in force: the newest saved version, else the file on disk (version 1)."""
    name = file_name(kind, ref)
    return _resolve(kind, name, await _newest(session, owner, kind, name))


async def read(session: AsyncSession, owner: str, kind: str, ref: str) -> str:
    """What an agent is sent: the newest saved version of the file, else the file on disk (header comment dropped,
    placeholders as written). The harness's `read` for everything that has a database session."""
    return (await resolve(session, owner, kind, ref)).text


# --- what a page or a hand-out needs of many files at once -----------------------------------------------------


@dataclass
class Store:
    """The saved versions and the files agents are working with, read once for a page."""

    newest: dict[tuple[str, str], InstructionFile] = field(default_factory=dict)
    busy: dict[tuple[str, str], str] = field(default_factory=dict)

    def resolve(self, kind: str, ref: str) -> Resolved:
        name = files.short_name(kind, ref)
        return _resolve(kind, name, self.newest.get((kind, name)))


async def load_store(session: AsyncSession, owner: str) -> Store:
    return Store(await newest_map(session, owner), await busy_files(session, owner))


async def busy_files(session: AsyncSession, owner: str) -> dict[tuple[str, str], str]:
    """The files an agent session that was sent them is still running with, by `(kind, name)`, each with the agent's
    name. Known only for what the app keeps a copy of (`task_handouts`): a search still open, a site task in
    progress."""
    since = datetime.now(UTC) - BUSY_FOR
    rows = (await session.execute(
        select(TaskHandout.files, TaskHandout.agent_name)
        .outerjoin(SearchRun, SearchRun.id == TaskHandout.task_id)
        .outerjoin(PlanTask, PlanTask.id == TaskHandout.plan_task_id)
        .where(
            TaskHandout.owner == owner, TaskHandout.sent_at >= since,
            or_(SearchRun.status == "open", PlanTask.status == PlanTaskStatus.in_progress),
        )
    )).all()
    out: dict[tuple[str, str], str] = {}
    for sent, agent in rows:
        for entry in sent or []:
            kind = _HANDOUT_KIND.get(entry.get("kind", ""))
            if kind and entry.get("name"):
                out.setdefault((kind, entry["name"]), agent or "An agent")
    return out


def busy_words(who: str) -> str:
    return f"{who} is working with this file right now. Save your change once it has finished."


# --- the history ------------------------------------------------------------------------------------------------


def _by_words(saved_by: str) -> str:
    return "You" if saved_by == OWNER_BY else "Shipped with the app"


def _version_view(row: InstructionFile, current: int) -> dict:
    shipped = row.saved_by == SHIPPED_BY
    return {
        "version": row.version, "at": None if shipped else iso_utc(row.saved_at), "by": _by_words(row.saved_by),
        "why": row.why, "chars": len(row.text), "current": row.version == current, "shipped": shipped,
    }


async def history(session: AsyncSession, owner: str, kind: str, ref: str) -> dict:
    """Every version of a file, newest first (number, when, who, why), and which is in force. A file the owner has
    not changed is version 1 alone, as shipped."""
    name = file_name(kind, ref)
    rows = (await session.scalars(
        select(InstructionFile).where(
            InstructionFile.owner == owner, InstructionFile.kind == kind, InstructionFile.name == name,
        ).order_by(InstructionFile.version.desc())
    )).all()
    if not rows:
        disk = files.read(kind, name)
        return {
            "kind": kind, "name": name, "current": 1, "shipped_newer": False,
            "versions": [{
                "version": 1, "at": None, "by": _by_words(SHIPPED_BY), "why": "As shipped with the app.",
                "chars": len(disk), "current": True, "shipped": True,
            }],
        }
    current = rows[0].version
    return {
        "kind": kind, "name": name, "current": current, "shipped_newer": _resolve(kind, name, rows[0]).shipped_newer,
        "versions": [_version_view(r, current) for r in rows],
    }


async def version_text(session: AsyncSession, owner: str, kind: str, ref: str, version: int) -> dict:
    """One version of a file with its text, to read. Raises `FileError` when there is no such version."""
    name = file_name(kind, ref)
    row = await session.scalar(
        select(InstructionFile).where(
            InstructionFile.owner == owner, InstructionFile.kind == kind, InstructionFile.name == name,
            InstructionFile.version == version,
        )
    )
    if row is None:
        if version == 1 and await _newest(session, owner, kind, name) is None:
            return {"version": 1, "text": files.read(kind, name), "by": _by_words(SHIPPED_BY),
                    "why": "As shipped with the app.", "at": None}
        raise FileError(f"there is no version {version} of {name}")
    return {
        "version": row.version, "text": row.text, "by": _by_words(row.saved_by), "why": row.why,
        "at": None if row.saved_by == SHIPPED_BY else iso_utc(row.saved_at),
    }


# --- saving ----------------------------------------------------------------------------------------------------


def _clean_why(why: str | None) -> str:
    """The why as one line, spaces folded, kept whole: there is no limit on what the owner writes."""
    one_line = " ".join((why or "").split())
    if not one_line:
        raise FileError("say why in a line: it is kept with the version")
    return one_line


def _refuse_over_room(text: str, beside: int | None) -> None:
    """Refuse a file that, with the file an agent is sent beside it (`beside`: that file's size as saved now, or None
    when no step sends it with another), would pass what one reply leaves for the two. In words, with both sizes;
    nothing is saved and nothing is cut. A file is otherwise as long as the owner writes it."""
    owner_text.refuse_over_room(
        text, "This file", beside=None if beside is None else ("the file sent with it", beside),
        result=NOT_SAVED, fix="Shorten it." if beside is None else "Shorten one of them.",
    )


async def _guard_busy(session: AsyncSession, owner: str, kind: str, name: str) -> None:
    who = (await busy_files(session, owner)).get((kind, name))
    if who:
        raise FileBusy(busy_words(who))


async def _add(
    session: AsyncSession, owner: str, kind: str, name: str, text: str, why: str, newest: InstructionFile | None,
    *, saved_by: str = OWNER_BY,
) -> InstructionFile:
    """Add the next version of a file; the first save also writes version 1, the shipped text it was made from."""
    disk = files.read(kind, name)
    disk_sha = sha_of(disk)
    if newest is None:
        session.add(InstructionFile(
            owner=owner, kind=kind, name=name, version=1, text=disk, shipped_sha=disk_sha, saved_by=SHIPPED_BY,
            why="As shipped with the app.",
        ))
    row = InstructionFile(
        owner=owner, kind=kind, name=name, version=(newest.version if newest else 1) + 1, text=text,
        shipped_sha=disk_sha, saved_by=saved_by, why=why,
    )
    session.add(row)
    await session.flush()
    return row


async def save(
    session: AsyncSession, owner: str, kind: str, ref: str, text: str, why: str | None, *, beside: int | None = None,
) -> InstructionFile:
    """Save an edit of a file as its next version. Refused when nothing changed, when it is empty, when it names a
    `{{placeholder}}` the shipped file does not (the app would not know what to fill in), when it is more than an agent
    can be sent with the file beside it (`beside`, see `_refuse_over_room`; a 413), and while an agent is working with
    the file. Its length is otherwise the owner's."""
    name = file_name(kind, ref)
    body = (text or "").replace("\r\n", "\n").strip()
    if not body:
        raise FileError("the file can't be empty")
    _refuse_over_room(body, beside)
    why = _clean_why(why)
    newest = await _newest(session, owner, kind, name)
    in_force = newest.text if newest else files.read(kind, name)
    if body == in_force:
        raise FileError("nothing changed")
    unknown = sorted(files.placeholders(body) - files.placeholders(files.read(kind, name)))
    if unknown:
        raise FileError(
            f"{', '.join('{{' + p + '}}' for p in unknown)} isn't something the app fills in; "
            "keep to the placeholders the shipped file has"
        )
    await _guard_busy(session, owner, kind, name)
    return await _add(session, owner, kind, name, body, why, newest)


async def restore(
    session: AsyncSession, owner: str, kind: str, ref: str, version: int, *, beside: int | None = None,
) -> InstructionFile:
    """Bring an earlier version back by saving its text as the next version. The text must still fit with the file
    beside it (`beside`, as for `save`): the other file may have grown since."""
    name = file_name(kind, ref)
    newest = await _newest(session, owner, kind, name)
    if newest is None:
        raise FileError("this file has not been changed: version 1 is the one in force")
    old = await session.scalar(
        select(InstructionFile).where(
            InstructionFile.owner == owner, InstructionFile.kind == kind, InstructionFile.name == name,
            InstructionFile.version == version,
        )
    )
    if old is None:
        raise FileError(f"there is no version {version} of {name}")
    if old.text == newest.text:
        raise FileError(f"version {version} is already the one in force")
    _refuse_over_room(old.text, beside)
    await _guard_busy(session, owner, kind, name)
    return await _add(session, owner, kind, name, old.text, f"Restored version {version}.", newest)


# --- a release that ships a different file ---------------------------------------------------------------------


async def apply_shipped(session: AsyncSession, owner: str) -> int:
    """For each file the owner has changed whose shipped text is different now from the one their version was made from,
    save the shipped text as the next version ("Shipped with the app"): it is what agents are sent from then on, and the
    owner's version stays in the history to read or restore. A file an agent is working with right now is left for the
    next start. Returns how many files were updated."""
    done = 0
    for (kind, name), row in (await newest_map(session, owner)).items():
        if name not in files.names(kind) or not _resolve(kind, name, row).shipped_newer:
            continue
        try:
            await _guard_busy(session, owner, kind, name)
        except FileBusy:
            continue
        await _add(session, owner, kind, name, files.read(kind, name), SHIPPED_WHY, row, saved_by=SHIPPED_BY)
        done += 1
    return done
