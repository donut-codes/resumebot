"""The session listeners of the work queue.

- **The hook** (`before_flush`): every ordinary save of a watched model posts what the watch list says
  (`watch.WATCH`), in the same flush, so the change and its to-do are saved together or not at all. It reads the
  attribute history of the watched columns only (as `models._record_stage_moves` does), so a write that changes nothing
  posts nothing.
- **The tripwire** (`do_orm_execute`, `before_commit`): a direct statement (`update`, `insert`, `delete`, a Postgres
  insert, raw SQL) on a watched table is noted; a `post` or an `exempt` answers it; a note left at commit is a missed
  post. In the tests it raises `MissedPost` naming the call site, so every test that exercises a writer is also a
  coverage test. In production it posts the catch-all `*` for the table's topics in the same transaction and logs once
  for each call site: an unwired writer degrades to today's behaviour instead of stalling roles.

Registered when this module is imported, which the database's configuration does (`app.db.configure`)."""

import logging
import re
import uuid

from sqlalchemy import Uuid, event
from sqlalchemy.orm import ORMExecuteState
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.sql.elements import TextClause

from app.jobsearch.work import engine_calls
from app.jobsearch.work.queue import service, watch
from app.jobsearch.work.queue.topics import ANY
from app.settings import settings

log = logging.getLogger(__name__)

#: A raw statement that writes, wherever in the text its verb is (a `WITH ... UPDATE` writes too): `UPDATE t SET` (the
#: `SET` is what tells it from `SELECT ... FOR UPDATE OF t`), `INSERT INTO t`, `DELETE FROM t`, `TRUNCATE t`,
#: `MERGE INTO t`. The first group is the verb, the second the table.
_RAW_UPDATE = re.compile(r"\b(UPDATE)\s+(?:ONLY\s+)?([\w.\"]+)(?:\s+(?:AS\s+)?\w+)?\s+SET\b", re.IGNORECASE)
_RAW_OTHER = re.compile(r"\b(INSERT\s+INTO|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|MERGE\s+INTO)\s+(?:ONLY\s+)?([\w.\"]+)",
                        re.IGNORECASE)
#: The call sites already logged as missed (once each).
_logged: set[str] = set()


def raw_write(sql: str) -> tuple[str, str] | None:
    """(the verb, the table) of the first write in a raw SQL statement, lower case and without its schema, or None for a
    statement that only reads. The tripwire and the scan test (`tests/jobsearch/work_queue/scan.py`) both ask this."""
    found = [m for m in (_RAW_UPDATE.search(sql), _RAW_OTHER.search(sql)) if m]
    if not found:
        return None
    first = min(found, key=lambda m: m.start())
    return first.group(1).split()[0].lower(), first.group(2).replace('"', "").rsplit(".", 1)[-1].lower()


def _give_ids(session: OrmSession, obj: object) -> None:
    """A new object's id is made at flush; a to-do names it, so it is made now (as `models._record_stage_moves` does for
    a role), and the flush keeps it."""
    column = type(obj).__table__.c.get("id")  # type: ignore[attr-defined]
    if column is not None and isinstance(column.type, Uuid) and getattr(obj, "id", None) is None:
        obj.id = uuid.uuid4()  # type: ignore[attr-defined]


@event.listens_for(OrmSession, "before_flush")
def post_for_changes(session: OrmSession, _flush_context, _instances) -> None:
    """Post the to-dos of the objects this flush creates, changes and deletes."""
    try:
        _post_for_changes(session)
    except Exception:
        if service.strict:
            raise
        log.warning("the work queue's hook failed; the save goes on without its to-dos", exc_info=True)


def _post_for_changes(session: OrmSession) -> None:
    for obj in list(session.new):
        entry = watch.WATCH.get(type(obj))
        if entry is None or not entry.created:
            continue
        _give_ids(session, obj)
        watch.posts_for(session, entry, obj, watch.Created(obj), entry.created,
                        cause=f"hook:{type(obj).__name__}.created")
    for obj in list(session.dirty):
        entry = watch.WATCH.get(type(obj))
        if entry is None or not entry.rules:
            continue
        change = watch.Change(obj)
        touched = set(change.state.committed_state)
        for rule in entry.rules:
            hit = [c for c in rule.columns if c in touched and change.changed(c)]
            if not hit or (rule.when is not None and not rule.when(obj, change)):
                continue
            watch.posts_for(session, entry, obj, change, rule.posts, cause=f"hook:{type(obj).__name__}.{hit[0]}")
    for obj in list(session.deleted):
        entry = watch.WATCH.get(type(obj))
        if entry is None or not entry.deleted:
            continue
        watch.posts_for(session, entry, obj, watch.Change(obj), entry.deleted,
                        cause=f"hook:{type(obj).__name__}.deleted")


@event.listens_for(OrmSession, "after_flush_postexec")
def flush_done(session: OrmSession, _flush_context) -> None:
    service.forget_pending(session)


# --- the tripwire -----------------------------------------------------------------------------------------------------


@event.listens_for(OrmSession, "do_orm_execute")
def note_direct_write(state: ORMExecuteState) -> None:
    """Note a statement that writes to a watched table without the session's change tracking seeing it."""
    if state.is_select:
        return
    table: str | None = None
    verb = ""
    if state.is_insert or state.is_update or state.is_delete:
        table = getattr(getattr(state.statement, "table", None), "name", None)
        verb = "insert" if state.is_insert else "update" if state.is_update else "delete"
    elif isinstance(state.statement, TextClause):
        found = raw_write(state.statement.text)
        if found:
            verb, table = found
    if table in watch.WATCHED_TABLES:
        service.note(state.session, table, verb)


@event.listens_for(OrmSession, "before_commit")
def answer_notes(session: OrmSession) -> None:
    """A write no one posted for: refused in the tests, answered with the catch-all in production."""
    if session.in_nested_transaction():
        return  # a savepoint's release: the transaction's own commit is where the notes are answered
    notes = service.take_notes(session)
    if not notes:
        return
    said = "; ".join(f"{n.verb} on {n.table} at {n.where}" for n in notes)
    if service.strict:
        raise service.MissedPost(
            f"no to-do was posted (queue.post) and the write is not exempt (queue.exempt): {said}")
    for n in notes:
        if n.where not in _logged:
            _logged.add(n.where)
            log.warning("no to-do was posted for a %s on %s at %s: posted the catch-all", n.verb, n.table, n.where)
        for topic in watch.topics_of(n.table):
            service.post(session, settings.owner, topic, [ANY], cause="tripwire")


@event.listens_for(OrmSession, "after_soft_rollback")
def rolled_back(session: OrmSession, previous_transaction) -> None:
    service.drop_rolled_back(session, previous_transaction)


@event.listens_for(OrmSession, "after_commit")
def committed(session: OrmSession) -> None:
    """A transaction that added a to-do has committed: wake the dispatcher (`engine_calls.nudge`: `consume.wake` in the
    engine, a `look` bell from the page), once. A hint, not the record: the row is in the table, and the dispatcher
    looks at least every 30 seconds. SQLAlchemy runs this for a savepoint's release too, when no other connection can
    see the row yet: the wake is kept for the commit that makes it visible (a wake spent at the
    release would leave the real commit waking nothing)."""
    if session.in_nested_transaction():
        return
    if session.info.pop(service.WAKE, None):
        engine_calls.nudge()


@event.listens_for(OrmSession, "after_transaction_end")
def transaction_over(session: OrmSession, transaction) -> None:
    if transaction.parent is None:
        service.take_notes(session)
        service.forget_pending(session)
        session.info.pop(service.WAKE, None)  # a rollback wakes nothing
