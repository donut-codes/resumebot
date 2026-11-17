"""An owner's own versions of a process file (requirements 15.4 and 15.6).

The shipped files (`config.shipped_dir`) stay read-only. When an owner changes a decision's rule (the loop an app builds
on `add_version`), the result is saved here as a new, whole version of that process's file, for this owner:

- **New work uses the newest version that loads.** `refresh` hands every version to the process library
  (`loader.set_owner_versions`), which starts new instances on it and finds a running instance's own version by its
  file hash, so an instance finishes under the file it started with. A release can rename something a saved version
  names, so that it no longer loads: new work then runs the newest version that still does (or the shipped file when
  none does), and `running` / `list_versions` say so in plain words. Nothing is lost: the history keeps every version.
- **Any earlier version can be rolled back to**, the shipped file included (it is "version 0" and never a row). A
  rollback is a new version whose text is the earlier one's: the history is only ever added to. It compares stored file
  hashes, so it works even when the newest version no longer loads.
- **The history** (`list_versions`) says who changed which rule, when, and who signed off on what.
- **A save is checked and kept safely.** `add_version` refuses a file that doesn't load, one that would make
  processes call each other in a circle, and one built on a version that is no longer the one in use. The in-memory
  library changes only once the caller's transaction has committed, and only for that owner.
- **Another program's save is read, not told.** `current_for` reads which versions the database holds for an owner
  and refreshes the library only when they are not the ones it was last filled from.

A version is a complete BPMN file that passes the same checks as a shipped one (`load_process`): a rule that reads a
field the data model doesn't have, or leaves a case with no way out, never becomes a version. Each household's versions
are its own: the library serves them only to work that household started.
"""

import hashlib
import html
import logging
import re
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as DET
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from process_engine import loader
from process_engine.bpmn import _local
from process_engine.config import config
from process_engine.errors import ProcessLoadError
from process_engine.loader import ProcessLibrary
from process_engine.model import ProcessDef
from process_engine.models import ProcVersion

log = logging.getLogger(__name__)

SHIPPED = 0
RULE_CHANGE, ROLLBACK = "rule_change", "rollback"
_EXT = loader.PORTER_NS


class VersionError(ValueError):
    """A version can't be made or restored. Plain words. `status` is the HTTP answer an app may give it (409)."""

    status = 409


# --- the shipped files ----------------------------------------------------------------------------------------


def ensure_registered() -> None:
    """Ask the app to import whatever registers the actions the shipped files name (`config.register_processes`), so
    a file loads here as it does there."""
    if config.register_processes is not None:
        config.register_processes()


def _shipped_dir() -> Path | None:
    found = config.shipped_dir() if config.shipped_dir is not None else None
    return Path(found) if found else None


@lru_cache(maxsize=8)
def _shipped_index(directory: str) -> dict[str, str]:
    """Process key -> the file name in the shipped folder."""
    out: dict[str, str] = {}
    for path in sorted(Path(directory).glob("*.bpmn")):
        try:
            root = DET.fromstring(path.read_bytes())
        except Exception:  # noqa: BLE001 - a file that doesn't read is the loader's to report, not this index's
            continue
        for el in root:
            if _local(el.tag) == "process":
                out[el.get(f"{{{_EXT}}}key") or el.get("id") or path.stem] = path.name
    return out


def _shipped_files() -> dict[str, str]:
    """Process key -> the file name in the shipped folder (`config.shipped_dir`); empty when none is configured."""
    directory = _shipped_dir()
    return _shipped_index(str(directory)) if directory is not None and directory.is_dir() else {}


def shipped_xml(key: str) -> str:
    """The shipped file of a process, as shipped."""
    name = _shipped_files().get(key)
    directory = _shipped_dir()
    if name is None or directory is None:
        raise VersionError("there is no such process")
    return (directory / name).read_text(encoding="utf-8")


def process_keys() -> list[str]:
    return sorted(_shipped_files())


def definition(xml: str, key: str) -> ProcessDef:
    """A version's file as the engine runs it. Raises `VersionError` with every problem the checks found."""
    ensure_registered()
    try:
        defn = loader.load_process(xml, key=key)
    except ProcessLoadError as exc:
        raise VersionError("; ".join(exc.problems)) from exc
    if defn.key != key:
        raise VersionError("the file is for a different process")
    return defn


# --- reading and keeping the overlay --------------------------------------------------------------------------

#: What the owner is told when a save or a sign-off was built on a version that is no longer the one in use.
MOVED = "The process changed in the meantime, so this was not saved. Look at the new version and try again."


def file_hash_of(xml: str) -> str:
    """The hash `load_process` gives a file's text (`ProcessDef.file_hash`), without loading it."""
    return hashlib.sha256(xml.encode("utf-8")).hexdigest()


async def versions_of(session: AsyncSession, entity_id: uuid.UUID, key: str) -> list[ProcVersion]:
    """Every household version of a process, oldest first."""
    return list((await session.scalars(
        select(ProcVersion).where(ProcVersion.entity_id == entity_id, ProcVersion.process_key == key)
        .order_by(ProcVersion.number)
    )).all())


async def latest(session: AsyncSession, entity_id: uuid.UUID, key: str) -> ProcVersion | None:
    """The newest version saved, whether or not it still loads (`running` is the one in use)."""
    rows = await versions_of(session, entity_id, key)
    return rows[-1] if rows else None


async def xml_of(session: AsyncSession, entity_id: uuid.UUID, key: str, number: int) -> str | None:
    """The text of one version of a process (0 is the shipped file), or None when there is no such version."""
    if number == SHIPPED:
        return shipped_xml(key)
    return await session.scalar(select(ProcVersion.xml).where(
        ProcVersion.entity_id == entity_id, ProcVersion.process_key == key, ProcVersion.number == number))


def _try_load(xml: str, key: str) -> tuple[ProcessDef | None, str]:
    """A version's file as the engine runs it, or why it doesn't load (in the loader's plain words)."""
    try:
        return definition(xml, key), ""
    except VersionError as exc:
        return None, str(exc)


def _numbers(numbers: list[int]) -> str:
    words = [str(n) for n in numbers]
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


@dataclass(frozen=True)
class Running:
    """The version new work starts on for an owner: the newest that still loads, else the shipped file (number 0).
    `skipped` lists the newer versions that no longer load, newest first, each with the loader's reason."""

    number: int
    xml: str
    skipped: tuple[tuple[int, str], ...] = ()

    @property
    def note(self) -> str | None:
        """Plain words for the page when the newest version is not the one in use, else None."""
        if not self.skipped:
            return None
        gone = [n for n, _why in self.skipped]
        running = "the file as shipped is" if self.number == SHIPPED else f"version {self.number} is"
        if len(gone) == 1:
            return f"Version {gone[0]} no longer works with this release, so {running} running."
        return f"Versions {_numbers(gone)} no longer work with this release, so {running} running."


async def running(session: AsyncSession, entity_id: uuid.UUID, key: str) -> Running:
    """Which version of a process is in use for an owner, read as `refresh` reads it: the newest that loads."""
    skipped: list[tuple[int, str]] = []
    for row in reversed(await versions_of(session, entity_id, key)):
        defn, problem = _try_load(row.xml, key)
        if defn is not None:
            return Running(row.number, row.xml, tuple(skipped))
        skipped.append((row.number, problem))
    return Running(SHIPPED, shipped_xml(key), tuple(skipped))


async def current_number(session: AsyncSession, entity_id: uuid.UUID, key: str) -> int:
    """The version new work starts on: the newest that loads, or 0 (the shipped file) when the household changed
    nothing (or no version of it loads any more)."""
    return (await running(session, entity_id, key)).number


async def current_xml(session: AsyncSession, entity_id: uuid.UUID, key: str) -> str:
    """The file in use: the text of the version `current_number` names. It is the file `refresh` hands the library, so
    what the page draws, what an edit starts from and what the engine runs are one file."""
    return (await running(session, entity_id, key)).xml


def active_xml(key: str, entity_id: uuid.UUID | None) -> str | None:
    """The text of the version the process library starts this owner's new work on, or None when the owner changed
    nothing there (the shipped file runs). The library holds what `refresh` and `add_version` gave it, by the same
    rule `running` reads the database with."""
    active = loader.owner_active(key, entity_id)
    return active.xml if active is not None else None


# --- the process library's copy of the versions ---------------------------------------------------------------


def _snapshot() -> tuple[dict[uuid.UUID, dict[str, list[ProcessDef]]], dict[uuid.UUID, dict[str, str]]]:
    """Every owner's versions in the library now, as `loader.set_owner_versions` takes them."""
    held = {
        owner: {key: list(by_hash.values()) for key, by_hash in by_key.items()}
        for owner, by_key in loader._owner_versions.items()  # noqa: SLF001 - one package; no public read yet
    }
    active = {owner: dict(by_key) for owner, by_key in loader._owner_active.items()}  # noqa: SLF001
    return held, active


def _build(rows: Sequence[Any]) -> tuple[list[ProcessDef], str | None]:
    """Load one process's versions (oldest first): the ones that load, and the hash of the newest of them, which is
    the one new work starts on. One that doesn't load (a release renamed what it names) is left out and logged."""
    defs: list[ProcessDef] = []
    active: str | None = None
    for row in rows:
        defn, problem = _try_load(row.xml, row.process_key)
        if defn is None:
            log.warning("household version %s of %s doesn't load, so it is left out: %s",
                        row.number, row.process_key, problem)
            continue
        defs.append(defn)
        active = defn.file_hash
    return defs, active


def _replace_key(entity_id: uuid.UUID, key: str, defs: list[ProcessDef], active: str | None) -> None:
    """Swap in one owner's versions of one process, leaving everyone else's as they are."""
    held, hashes = _snapshot()
    if defs and active:
        held.setdefault(entity_id, {})[key] = defs
        hashes.setdefault(entity_id, {})[key] = active
    else:
        held.get(entity_id, {}).pop(key, None)
        hashes.get(entity_id, {}).pop(key, None)
    for table in (held, hashes):
        if entity_id in table and not table[entity_id]:
            del table[entity_id]
    loader.set_owner_versions(held, hashes)


async def refresh(session: AsyncSession, entity_id: uuid.UUID | None = None) -> None:
    """Hand the saved versions to the process library: every owner's at startup (no `entity_id`), or one owner's.
    New work starts on the newest version that loads; a version that no longer loads (a shipped file changed under
    it) is left out and logged, so the app starts instead of failing, and the version before it is the one in use.

    A session that holds nothing when this is called (no transaction, nothing added, changed or deleted) is left with
    none: the versions are read, the transaction is ended, and only then are the files parsed and checked, which is
    most of the time this takes (most of the time, with many versions of megabytes, on a large database, 2026-11-09). On a
    database with one writer at a time, parsing inside the transaction held everyone else's turn to write for as long.
    A session that holds work of its own keeps it, and its transaction goes on as it was."""
    ensure_registered()
    query = select(ProcVersion.entity_id, ProcVersion.process_key, ProcVersion.number, ProcVersion.xml).order_by(
        ProcVersion.entity_id, ProcVersion.process_key, ProcVersion.number)
    if entity_id is not None:
        query = query.where(ProcVersion.entity_id == entity_id)
    free = not session.in_transaction() and not (session.new or session.dirty or session.deleted)
    saved = (await session.execute(query)).all()
    if free:
        await session.commit()
    grouped: dict[uuid.UUID, dict[str, list[Any]]] = {}
    for row in saved:
        grouped.setdefault(row.entity_id, {}).setdefault(row.process_key, []).append(row)
    held: dict[uuid.UUID, dict[str, list[ProcessDef]]] = {}
    hashes: dict[uuid.UUID, dict[str, str]] = {}
    for owner, by_key in grouped.items():
        for key, rows in by_key.items():
            defs, active = _build(rows)
            if defs and active:
                held.setdefault(owner, {})[key] = defs
                hashes.setdefault(owner, {})[key] = active
    if entity_id is None:
        loader.set_owner_versions(held, hashes)
        return
    everyone, everyone_active = _snapshot()
    everyone.pop(entity_id, None)
    everyone_active.pop(entity_id, None)
    everyone.update(held)
    everyone_active.update(hashes)
    loader.set_owner_versions(everyone, everyone_active)


#: What `current_for` last read from the database for each owner: every saved version's process, number and file hash,
#: oldest first. `_seen_in` is the library table (`loader._owner_versions`) those reads were true of: anything that
#: swaps the tables (a save's own install, `lent`, a restart, a test's reset) makes a new table, and the notes are then
#: dropped, so the next `current_for` reads the files again.
_seen: dict[uuid.UUID, tuple[tuple[str, int, str], ...]] = {}
_seen_in: object | None = None


async def current_for(session: AsyncSession, entity_id: uuid.UUID) -> bool:
    """Make the process library hold this owner's versions as the database has them now. A program calls it before it
    uses an owner's versions (before it starts a run, before it draws a process), so a version another program saved
    is in force with no message between the two.

    One small read (process, number and file hash of each saved version; no file text). When that is what the library
    was last filled from, nothing else happens. Otherwise `refresh` reads and parses this owner's files once. A version
    that no longer loads is in the note too, so it is not parsed again on every call. Returns True when it refreshed.

    The read is made in `session` and the transaction it began is left open: `refresh` ends a transaction only for a
    session that held none, and after the read this one holds one. A caller's unsaved work is seen and kept."""
    global _seen_in
    saved = tuple(
        (row.process_key, row.number, row.file_hash)
        for row in (await session.execute(
            select(ProcVersion.process_key, ProcVersion.number, ProcVersion.file_hash)
            .where(ProcVersion.entity_id == entity_id)
            .order_by(ProcVersion.process_key, ProcVersion.number)
        )).all()
    )
    if _seen_in is not loader._owner_versions:  # noqa: SLF001 - one package; the tables were swapped since
        _seen.clear()
    if _seen.get(entity_id) == saved:
        return False
    await refresh(session, entity_id)
    _seen[entity_id] = saved
    _seen_in = loader._owner_versions  # noqa: SLF001
    return True


def _after_commit(session: AsyncSession, row: ProcVersion, install: Callable[[], None]) -> None:
    """Run `install` once the session's transaction commits, and never if it is rolled back, or if the savepoint `row`
    was saved in was (the row is then gone from the session). Until then the library is as it was, so work that starts
    meanwhile never runs on a version with no database row."""
    sync = session.sync_session
    state = {"done": False}

    def committed(_session) -> None:
        if not state["done"]:
            state["done"] = True
            if row in sync:
                install()

    def rolled_back(_session) -> None:
        state["done"] = True

    event.listen(sync, "after_commit", committed)
    event.listen(sync, "after_rollback", rolled_back)


@contextmanager
def lent(to_owner: uuid.UUID, from_owner: uuid.UUID) -> Iterator[None]:
    """While the block runs, work for `to_owner` runs on `from_owner`'s versions, as if it were theirs: a throwaway
    household's run with the real household's files in force. Nothing is lent when `from_owner` has no versions of its
    own (it runs the shipped files, and so does the borrower)."""
    held, hashes = _snapshot()
    if from_owner not in held:
        yield
        return
    held[to_owner] = {key: list(defs) for key, defs in held[from_owner].items()}
    hashes[to_owner] = dict(hashes.get(from_owner, {}))
    loader.set_owner_versions(held, hashes)
    try:
        yield
    finally:
        held, hashes = _snapshot()
        held.pop(to_owner, None)
        hashes.pop(to_owner, None)
        loader.set_owner_versions(held, hashes)


# --- changing a rule in the file ------------------------------------------------------------------------------


def _text(value: str) -> str:
    return html.escape(value, quote=False)


def with_rule(xml: str, flow_id: str, condition: str, meaning: str, wording: str | None = None) -> str:
    """`xml` with the way out `flow_id` carrying `condition` (its rule), `meaning` (its plain words) and, when the
    household wrote it, `wording`. Only that flow is touched. Raises `VersionError` when the flow isn't there."""
    pattern = re.compile(
        rf'<(?P<p>(?:\w+:)?)sequenceFlow\b(?P<attrs>[^>]*?\bid="{re.escape(flow_id)}"[^>]*?)'
        r"(?:/>|>(?P<inner>.*?)</(?P=p)sequenceFlow>)",
        re.S,
    )
    found = pattern.search(xml)
    if found is None:
        raise VersionError(f"the way out '{flow_id}' isn't in the file")
    prefix, attrs = found.group("p"), found.group("attrs").rstrip("/").rstrip()
    attrs = re.sub(r'\s+porter:wording="[^"]*"', "", attrs)
    if wording:
        attrs += f' porter:wording="{html.escape(wording, quote=True)}"'
    inner = found.group("inner") or ""
    inner = re.sub(rf"<{prefix}documentation>.*?</{prefix}documentation>\s*", "", inner, flags=re.S)
    inner = re.sub(rf"<{prefix}conditionExpression\b.*?</{prefix}conditionExpression>\s*", "", inner, flags=re.S)
    indent = "      "
    body = (
        f"\n{indent}<{prefix}documentation>{_text(meaning)}</{prefix}documentation>"
        f'\n{indent}<{prefix}conditionExpression xsi:type="{prefix}tFormalExpression">{_text(condition)}'
        f"</{prefix}conditionExpression>"
    )
    rebuilt = f"<{prefix}sequenceFlow{attrs}>{body}{inner.rstrip()}\n    </{prefix}sequenceFlow>"
    return xml[: found.start()] + rebuilt + xml[found.end():]


def _check_calls(defn: ProcessDef, entity_id: uuid.UUID) -> None:
    """A version is checked against the whole library, as a shipped file is (`ProcessLibrary.validate`): what it calls
    must exist and be handed only what it declares, and no process may call itself, straight or through others. The
    other processes are the owner's own versions where it has them, else the shipped files. Only a problem the
    version brings is refused, so one the files already had doesn't stop every save."""
    ensure_registered()
    others: list[ProcessDef] = []
    for other_key in process_keys():
        if other_key == defn.key:
            continue
        other = loader.owner_active(other_key, entity_id)
        if other is None:
            try:
                other = loader.load_process(shipped_xml(other_key), key=other_key)
            except (ProcessLoadError, VersionError):
                continue  # a file that doesn't load is not this version's to answer for
        others.append(other)
    in_use = loader.owner_active(defn.key, entity_id)
    if in_use is None:
        try:
            in_use = loader.load_process(shipped_xml(defn.key), key=defn.key)
        except (ProcessLoadError, VersionError):
            in_use = None

    def problems(candidate: ProcessDef | None) -> list[str]:
        library = ProcessLibrary()
        for other in others:
            library.add(other)
        if candidate is not None:
            library.add(candidate)
        try:
            library.validate()
        except ProcessLoadError as exc:
            return exc.problems
        return []

    before = problems(in_use)
    brought = [p for p in problems(defn) if p not in before]
    if brought:
        raise VersionError("; ".join(brought))


async def add_version(
    session: AsyncSession, entity_id: uuid.UUID, key: str, xml: str, *, kind: str = RULE_CHANGE,
    changed_by: uuid.UUID | None = None, expected_base: int | None = None, **fields,
) -> ProcVersion:
    """Save `xml` as the next version. It must load and fit the rest of the library (no call cycle); when
    `expected_base` is given it must still be the version in use (`current_number`): a save built on an older one, or
    one that loses a race with another save, raises `VersionError` (plain words, 409), never a database error.

    Flushes; the caller commits. The process library changes only after that commit, for this owner and process only:
    until then new work still starts on the version that is saved, and a save that is rolled back leaves no trace."""
    defn = definition(xml, key)
    _check_calls(defn, entity_id)
    if expected_base is not None and (await running(session, entity_id, key)).number != expected_base:
        raise VersionError(MOVED)
    number = (await session.scalar(
        select(func.max(ProcVersion.number)).where(ProcVersion.entity_id == entity_id, ProcVersion.process_key == key)
    ) or 0) + 1
    row = ProcVersion(
        entity_id=entity_id, process_key=key, number=number, file_hash=defn.file_hash, xml=xml, kind=kind,
        changed_by=changed_by, **fields,
    )
    try:
        async with session.begin_nested():  # a lost race leaves the caller's transaction usable
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        raise VersionError(MOVED) from exc
    defs, active = _build(await versions_of(session, entity_id, key))
    _after_commit(session, row, lambda: _replace_key(entity_id, key, defs, active))
    return row


async def rollback(
    session: AsyncSession, entity_id: uuid.UUID, key: str, to_number: int, member_id: uuid.UUID,
) -> ProcVersion:
    """Go back to an earlier version (0 is the shipped file): a new version with that text. Running instances keep
    the file they started on. It compares the stored file hashes, so it works when the newest version no longer loads;
    the version gone back to must load (the shipped file always does)."""
    rows = await versions_of(session, entity_id, key)
    if not rows:
        raise VersionError("nothing has been changed here, so there is nothing to go back to")
    newest = rows[-1]
    if to_number == SHIPPED:
        xml = shipped_xml(key)
        target_hash = file_hash_of(xml)
    else:
        target = next((r for r in rows if r.number == to_number), None)
        if target is None:
            raise VersionError("there is no such version")
        xml, target_hash = target.xml, target.file_hash
    if to_number == newest.number or target_hash == newest.file_hash:
        raise VersionError("that is the version in use already")
    in_use = await running(session, entity_id, key)
    try:
        return await add_version(
            session, entity_id, key, xml, kind=ROLLBACK, changed_by=member_id, restored_number=to_number,
            signed_off_by=member_id, signed_off_at=datetime.now(UTC), expected_base=in_use.number,
        )
    except VersionError as exc:
        if str(exc) == MOVED:
            raise
        what = "the file as shipped" if to_number == SHIPPED else f"version {to_number}"
        raise VersionError(f"Can't go back to {what}: {exc}") from exc


# --- for the page ---------------------------------------------------------------------------------------------


def _words(row: ProcVersion) -> str:
    where = f"“{row.decision_name}”" if row.decision_name else "a decision"
    if row.kind == ROLLBACK:
        back = "the file as shipped" if not row.restored_number else f"version {row.restored_number}"
        return f"Went back to {back}."
    return f"Changed how {where} is decided."


async def list_versions(
    session: AsyncSession, entity_id: uuid.UUID, key: str, *, run: Running | None = None,
) -> list[dict]:
    """A process's history for How they work, newest first: who changed what rule, when, and the sign-off. The shipped
    file is last (version 0). `current` marks the version in use (`running`): usually the newest, but when the newest
    no longer loads it is the one before it, and the ones that don't load say so (`loads` False, with the loader's
    `problem`, and no restore)."""
    rows = await versions_of(session, entity_id, key)
    run = run or await running(session, entity_id, key)
    names = await config.member_names(session, entity_id) if config.member_names is not None else {}
    newest = rows[-1].number if rows else SHIPPED
    broken = dict(run.skipped)
    out = [
        {
            "number": r.number, "kind": r.kind, "at": r.created_at.isoformat(), "current": r.number == run.number,
            "what": _words(r), "by": names.get(r.changed_by), "wording": r.wording, "readback": r.readback,
            "old_rule": r.old_condition, "new_rule": r.new_condition, "decision": r.decision_name,
            "signed_off_by": names.get(r.signed_off_by),
            "signed_off_at": r.signed_off_at.isoformat() if r.signed_off_at else None,
            "restored": r.restored_number, "can_restore": r.number != newest and r.number not in broken,
            "loads": r.number not in broken, "problem": broken.get(r.number),
        }
        for r in reversed(rows)
    ]
    out.append({
        "number": SHIPPED, "kind": "shipped", "at": None, "current": run.number == SHIPPED,
        "what": config.shipped_label, "by": None, "wording": None, "readback": None, "old_rule": None,
        "new_rule": None, "decision": None, "signed_off_by": None, "signed_off_at": None, "restored": None,
        "can_restore": bool(rows), "loads": True, "problem": None,
    })
    return out
