"""Changing how a process works, and its versions (docs/PRD.md path 16.1 and 16.2).

How they work shows each process from its BPMN file. The owner edits the steps as text (the process view package's
editor); the engine's own loader refuses a file it cannot run, with every problem in plain words; and a save is the next
version of that process, kept in `proc_versions` (the process engine's table for an owner's versions, read through
`process_engine.versions`). There is no approval step: the owner's save is the change.

- **Version 1 is the file as it ships**, kept the first time the app starts (`reconcile`). The owner's edits are 2, 3
  and so on; a restore is a new version whose text is an earlier one's, so the history is only ever added to.
- **The engine runs the newest version that loads** (`process_engine.versions.refresh` at start, and the library's
  own swap after each save commits): `Engine.start` asks the library for the owner's version, so the next run uses it
  and a run already going finishes on the file it started with. The run's own file is the instance's `file_hash`, so
  which version it ran on is read back from that (`run_versions`).
- **A release that ships a changed file** is a new version of its own, whether or not the owner has edited the
  process: while the owner's edit is in force `reconcile` returns the file as newer and `app.agents.shipped_rule` saves
  it as the next version (the owner's edit does not stay in force; it stays in the history; nothing is asked).

Everything here flushes; the route commits. Adapted from another project's code (app/PROVENANCE.md) (`check_edit`,
`summarize_edit`), without its sign-off: see app/PROVENANCE.md.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import defusedxml.ElementTree as DET
from process_engine import versions
from process_engine.loader import PORTER_NS
from process_engine.model import ProcessDef
from process_engine.models import ProcVersion
from sqlalchemy.ext.asyncio import AsyncSession

from app import owner_keys
from app.errors import DomainError
from app.settings import VERSION
from app.textutil import squash

log = logging.getLogger(__name__)

EDIT, SHIPPED, RESTORE = "edit", "shipped", versions.ROLLBACK
SUMMARY_LINES = 12
EDIT_MOVED = "The process changed while you were editing it. Reload it to see the new version."
NEEDS_WHY = "Say why in a line, so the history can tell you later."


class EditError(DomainError):
    """The change can't be made. Plain words; the route answers it with `status`."""

    status = 422


class NotFound(EditError):
    status = 404


class Unchanged(EditError):
    """The file is the one in use, word for word: it can run and there is nothing to save."""


class EditConflict(EditError):
    """The change was made on a version that is no longer the one in use."""

    status = 409


def entity() -> uuid.UUID:
    return owner_keys.entity_id()


def known(key: str) -> str:
    if key not in versions.process_keys():
        raise NotFound("there is no such process")
    return key


def why_of(text: str | None) -> str:
    """The owner's why as one line, spaces folded, kept whole: there is no limit on what the owner writes."""
    said = squash(text)
    if not said:
        raise EditError(NEEDS_WHY)
    return said


# --- checking an edit -----------------------------------------------------------------------------------------


def summarize(old: ProcessDef, new: ProcessDef) -> list[str]:
    """What differs between the file in use and an edited one, in the owner's words: steps added, taken out or renamed,
    steps whose words changed, ways out added, taken out, rewired or re-ruled. A safeguard (`porter:firm`) that goes
    is said so plainly. Empty only when the two files are the same."""
    lines: list[str] = []
    for eid, el in new.elements.items():
        if eid not in old.elements:
            lines.append(f"Added the step “{el.label}”.")
    for eid, el in old.elements.items():
        if eid not in new.elements:
            lines.append(f"Took out the step “{el.label}”.")
    for eid, el in new.elements.items():
        was = old.elements.get(eid)
        if was is None:
            continue
        if was.label != el.label:
            lines.append(f"Renamed “{was.label}” to “{el.label}”.")
        elif (was.documentation, was.lane, was.tags) != (el.documentation, el.lane, el.tags):
            lines.append(f"Changed the words of “{el.label}”.")
        if (was.kind, was.action, was.role, was.instructions, was.template, was.card, was.called) != (
                el.kind, el.action, el.role, el.instructions, el.template, el.card, el.called):
            lines.append(f"Changed what “{el.label}” does.")

    def way(flow, defn: ProcessDef) -> str:
        source = defn.elements.get(flow.source)
        return f"“{flow.name or flow.id}”" + (f" of “{source.label}”" if source else "")

    for fid, flow in new.flows.items():
        if fid not in old.flows:
            lines.append(f"Added the way out {way(flow, new)}.")
    for fid, flow in old.flows.items():
        if fid not in new.flows:
            lines.append(f"Took out the way out {way(flow, old)}." + (" It held a safeguard." if flow.firm else ""))
    for fid, flow in new.flows.items():
        was = old.flows.get(fid)
        if was is None:
            continue
        if (was.source, was.target) != (flow.source, flow.target):
            lines.append(f"Rewired the way out {way(flow, new)}.")
        if (was.condition, was.documentation, was.name) != (flow.condition, flow.documentation, flow.name):
            lines.append(f"Changed the way out {way(flow, new)}.")
        if was.firm != flow.firm:
            lines.append(f"Changed the safeguard on the way out {way(flow, new)}.")
    if len(lines) > SUMMARY_LINES:
        lines = [*lines[:SUMMARY_LINES], f"And {len(lines) - SUMMARY_LINES} more."]
    return lines


def own_key(xml: str) -> str | None:
    """The process a file says it is (`porter:key`, else the process's id). `versions.definition(xml, key)` takes `key`
    as the process's key whatever the file says, so a file for another process would pass as this one; this reads what
    the file itself says. None when the text cannot be read (the loader then says why)."""
    try:
        root = DET.fromstring(xml.encode("utf-8"))
    except Exception:  # a text that is not XML is the loader's to refuse, in its words
        return None
    for el in root:
        if el.tag.rsplit("}", 1)[-1] == "process":
            return el.get(f"{{{PORTER_NS}}}key") or el.get("id")
    return None


async def check(
    session: AsyncSession, key: str, xml: str, *, in_use: versions.Running | None = None,
) -> tuple[ProcessDef, list[str]]:
    """An edited file against the file in use: the engine's loader refuses what it cannot run (`versions.VersionError`,
    every problem it found, in its words), and the lines say what differs. Raises `EditError` for a file with nothing
    changed."""
    in_use = in_use or await versions.running(session, entity(), key)
    if (own := own_key(xml)) is not None and own != key:
        raise EditError("The file is for a different process, not this one.")
    old = versions.definition(in_use.xml, key)
    new = versions.definition(xml, key)
    if new.file_hash == old.file_hash:
        raise Unchanged("nothing is different from the version in use")
    return new, summarize(old, new) or ["Changed words in the file."]


# --- reading ---------------------------------------------------------------------------------------------------


async def current(session: AsyncSession, key: str) -> dict:
    """The file that runs now: its newest version that loads. What the editor starts from."""
    in_use = await versions.running(session, entity(), known(key))
    return {"process": key, "number": in_use.number, "xml": in_use.xml, "note": in_use.note}


async def in_use_numbers(session: AsyncSession) -> dict[str, int]:
    """Every process's version in use (for the overview)."""
    return {key: (await versions.running(session, entity(), key)).number for key in versions.process_keys()}


def _lines(text: str | None) -> list[str]:
    return [line for line in (text or "").splitlines() if line.strip()]


def _view(row: dict) -> dict:
    kind = row["kind"]
    number = row["number"]
    why = row["wording"] or ""
    out = {
        "number": number, "source": kind, "at": row["at"], "by": row["by"], "current": row["current"],
        "why": why, "summary": _lines(row["readback"]) if kind == EDIT else [],
        "can_restore": row["can_restore"] and number >= 1, "loads": row["loads"], "problem": row["problem"],
        "restored": row["restored"] if kind == RESTORE else None,
    }
    if kind == RESTORE and not why:
        out["why"] = f"Went back to version {row['restored']}."
    return out


async def history(session: AsyncSession, key: str) -> dict:
    """The versions of a process, newest first, and the one in use: number, when, who, why and what changed."""
    known(key)
    in_use = await versions.running(session, entity(), key)
    rows = await versions.list_versions(session, entity(), key, run=in_use)
    return {
        "process": key,
        "running": {"number": in_use.number, "note": in_use.note},
        "versions": [_view(r) for r in rows if r["number"] >= 1],
    }


async def version_xml(session: AsyncSession, key: str, number: int) -> dict:
    """One earlier version's file, to look at."""
    known(key)
    if number < 1:
        raise NotFound("there is no such version")
    xml = await versions.xml_of(session, entity(), key, number)
    if xml is None:
        raise NotFound("there is no such version")
    return {"process": key, "number": number, "xml": xml}


# --- changing --------------------------------------------------------------------------------------------------


def _refused(exc: versions.VersionError) -> EditError:
    return EditConflict(EDIT_MOVED) if str(exc) == versions.MOVED else EditError(str(exc))


async def save(session: AsyncSession, key: str, xml: str, why: str | None, based_on: int | None = None) -> ProcVersion:
    """The owner's edit of a process, whole, as the next version. The engine's loader must be able to run it. `based_on`
    is the version the editor was showing: when the version in use has moved on, the save is refused (409) instead of
    going over it. Flushes."""
    known(key)
    said = why_of(why)
    in_use = await versions.running(session, entity(), key)
    if based_on is not None and based_on != in_use.number:
        raise EditConflict(EDIT_MOVED)
    try:
        new, lines = await check(session, key, xml, in_use=in_use)
        return await versions.add_version(
            session, entity(), key, xml, kind=EDIT, changed_by=owner_keys.member_id(), expected_base=in_use.number,
            decision_name=new.name[:255], wording=said, readback="\n".join(lines),
        )
    except versions.VersionError as exc:
        raise _refused(exc) from exc


async def restore(session: AsyncSession, key: str, number: int, why: str | None = None) -> ProcVersion:
    """Go back to an earlier version: a new version whose text is that one's. Running work finishes on the file it
    started with. Flushes."""
    known(key)
    if number < 1:
        raise NotFound("there is no such version")
    try:
        row = await versions.rollback(session, entity(), key, number, owner_keys.member_id())
    except versions.VersionError as exc:
        raise _refused(exc) from exc
    row.wording = squash(why) or f"Went back to version {number}."
    await session.flush()
    return row


async def use_shipped(session: AsyncSession, key: str) -> ProcVersion:
    """Save the file as it ships with this release as the next version (the owner's answer to the Inbox card).
    Flushes."""
    known(key)
    shipped = versions.shipped_xml(key)
    in_use = await versions.running(session, entity(), key)
    if versions.file_hash_of(shipped) == versions.file_hash_of(in_use.xml):
        raise EditError("the version in use is the one that ships already")
    try:
        return await versions.add_version(
            session, entity(), key, shipped, kind=SHIPPED, changed_by=owner_keys.member_id(),
            expected_base=in_use.number, wording=f"The file as shipped with resumebot {VERSION}.",
        )
    except versions.VersionError as exc:
        raise _refused(exc) from exc


# --- which version a run ran on -----------------------------------------------------------------------------


async def run_versions(session: AsyncSession, instances: list) -> dict[uuid.UUID, int | None]:
    """The version of its process each run (a `ProcInstance`) ran on, read from the file it started on. Two versions can
    hold the same text (a restore), so of those the newest that existed when the run started is its version."""
    found: dict[uuid.UUID, int | None] = {}
    rows_by_key: dict[str, list[ProcVersion]] = {}
    for inst in instances:
        if inst.process_key not in rows_by_key:
            rows_by_key[inst.process_key] = await versions.versions_of(session, entity(), inst.process_key)
        same = [r for r in rows_by_key[inst.process_key] if r.file_hash == inst.file_hash]
        then = [r for r in same if r.created_at <= inst.created_at]
        pick = then[-1] if then else (same[0] if same else None)
        found[inst.id] = pick.number if pick else None
    return found


# --- the first start, and a release that ships a changed file -----------------------------------------------


@dataclass(frozen=True)
class Newer:
    """A shipped file that has changed since the owner's version was made: the owner's version stays in force."""

    kind: str  # what the file is: process
    name: str  # the process key
    label: str  # in words
    shipped_hash: str
    in_use: int  # the version in force


async def reconcile(session: AsyncSession) -> list[Newer]:
    """Keep the shipped process files as versions. The first time: each file is version 1. After a release that
    changed a file: while the newest saved version is still the shipped file untouched, the new shipped file becomes
    the next version; while the owner's own version is in force the file is returned as newer, and `shipped_rule`
    saves it as the next version over the owner's edit. Flushes."""
    out: list[Newer] = []
    for key in versions.process_keys():
        try:
            shipped = versions.shipped_xml(key)
            h = versions.file_hash_of(shipped)
            rows = await versions.versions_of(session, entity(), key)
            if not rows:
                await versions.add_version(
                    session, entity(), key, shipped, kind=SHIPPED, wording="The file as it first shipped.")
                continue
            last_shipped = next((r for r in reversed(rows) if r.kind == SHIPPED), None)
            if last_shipped is None or last_shipped.file_hash == h:
                continue
            if rows[-1].file_hash == last_shipped.file_hash:
                await versions.add_version(
                    session, entity(), key, shipped, kind=SHIPPED,
                    wording=f"The file as shipped with resumebot {VERSION}.")
            else:
                out.append(Newer("process", key, await _label(session, key), h, rows[-1].number))
        except versions.VersionError as exc:
            log.warning("the shipped file of %s can't be kept as a version: %s", key, exc)
    return out


async def _label(session: AsyncSession, key: str) -> str:
    try:
        return versions.definition(versions.shipped_xml(key), key).name or key
    except versions.VersionError:
        return key
