"""Helpers for the work queue's tests: what was posted, and a role or posting to change. Everything is made up."""

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Enum, delete, event, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app import db
from app.jobsearch.models import Employer, JobPosting, Opportunity, Todo
from tests.support import EXAMPLE_SYSTEM

OWNER = "tester"


async def posted(session, owner: str | None = None) -> list[tuple[str, str]]:
    """Every to-do the transaction can see, as (topic, key), oldest first."""
    query = select(Todo.topic, Todo.key).order_by(Todo.id)
    if owner is not None:
        query = query.where(Todo.owner == owner)
    return [(topic, key) for topic, key in (await session.execute(query)).all()]


async def posted_now() -> list[tuple[str, str]]:
    """The committed to-dos, read in a session of their own."""
    async with db.session_factory()() as session:
        return await posted(session)


def make_role(**fields) -> Opportunity:
    base = {"id": uuid.uuid4(), "owner": OWNER, "slug": f"role-{uuid.uuid4().hex[:6]}", "company": "Fabrikam",
            "title": "Lead",
            "match_key": f"fabrikam|lead-{uuid.uuid4().hex[:6]}", "status": "landing_zone"}
    return Opportunity(**{**base, **fields})


def make_posting(**fields) -> JobPosting:
    now = datetime.now(UTC)
    base = {"id": uuid.uuid4(), "owner": OWNER, "employer_id": uuid.uuid4(), "ats": EXAMPLE_SYSTEM,
            "ext_id": uuid.uuid4().hex[:8],
            "match_key": "fabrikam|lead", "company": "Fabrikam", "title": "Lead", "first_seen": now, "last_seen": now}
    return JobPosting(**{**base, **fields})


def make_employer(**fields) -> Employer:
    name = fields.pop("name", "Fabrikam")
    return Employer(**{"id": uuid.uuid4(), "owner": OWNER, "key": name.lower() + uuid.uuid4().hex[:4], "name": name,
                       **fields})


async def saved(session, *objects):
    """Add the objects, flush, and clear the to-dos their creation posted, so a test sees only what it changes next."""
    session.add_all(objects)
    await session.flush()
    await session.execute(delete(Todo))
    await session.flush()
    return objects


# --- a row of any watched model, and a different value for any of its columns -----------------------------------------

_CHECK = re.compile(r"^\s*(\w+)\s+IN\s*\((.*)\)\s*$", re.DOTALL)


def allowed(model: type, column: str) -> list[str] | None:
    """The words a closed-list column may hold (its CHECK constraint, `one_of`), or None for a free column."""
    for constraint in model.__table__.constraints:  # type: ignore[attr-defined]
        if isinstance(constraint, CheckConstraint):
            found = _CHECK.match(str(constraint.sqltext))
            if found and found.group(1) == column:
                return [w.strip().strip("'") for w in found.group(2).split(",")]
    return None


def a_value(model: type, column: str, current: Any = None, *, different: bool = False) -> Any:
    """A value for `column` of `model` that the database takes, and (with `different`) that is not `current`."""
    col = model.__table__.c[column]  # type: ignore[attr-defined]
    words = allowed(model, column)
    if words:
        return next((w for w in words if not different or w != current), words[0])
    if isinstance(col.type, Enum):  # a column of a closed list made of an enum: a member other than the current one
        members = list(col.type.enum_class) if col.type.enum_class else list(col.type.enums)
        return next((m for m in members if not different or m != current), members[0])
    if isinstance(col.type, JSON):  # `JSON().python_type` is not a type: the shape the column holds says which
        if isinstance(current, list) or (current is None and col.default is not None and col.default.arg is list):
            return ["flip"] if current != ["flip"] else ["flip", 2]
        return {"flip": 1} if current != {"flip": 1} else {"flip": 2}
    kind = getattr(col.type, "impl_instance", col.type).python_type  # a TypeDecorator (`UtcDateTime`) is its impl
    if kind is bool:
        return (not current) if different else False
    if kind is int:
        return (current or 0) + 1 if different else 1
    if kind is float:
        return (current or 0.0) + 1.0 if different else 1.0
    if kind is str:
        return "b" if current != "b" else "c"
    if kind is datetime:
        return (current or datetime.now(UTC)) + timedelta(days=1) if different else datetime.now(UTC)
    if kind is date:
        return (current or date.today()) + timedelta(days=1) if different else date.today()
    if kind is uuid.UUID:
        return uuid.uuid4()
    if kind is bytes:
        return b"b" if current != b"b" else b"c"
    if kind is list:
        return ["flip"] if current != ["flip"] else ["flip", 2]
    if kind is dict:
        return {"flip": 1} if current != {"flip": 1} else {"flip": 2}
    raise AssertionError(f"no value for {model.__name__}.{column} of type {col.type!r}")


def blank(model: type, **fields: Any) -> Any:
    """A row of `model` with every column the database insists on filled in (a made-up value, or the first word of its
    closed list), and `fields` over them."""
    values: dict[str, Any] = {}
    for col in model.__table__.columns:  # type: ignore[attr-defined]
        if col.name in fields or col.primary_key and (col.autoincrement is True or col.default is not None):
            continue
        if col.nullable or col.default is not None or col.server_default is not None:
            continue
        values[col.name] = a_value(model, col.name)
    return model(**{**values, **fields})


def _links_off(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=OFF")
    finally:
        cursor.close()


async def without_links(session) -> None:
    """The foreign keys are not checked for this session: a row of any model can be made on its own, with ids that
    point nowhere. SQLite cannot change the setting inside a transaction, so the session is bound to an engine of its
    own on the same file (the same PRAGMAs and the same writer's turn as the app's, but the keys off, and no pool: each
    connection is closed when its transaction ends). Call it before the session's first statement."""
    assert not session.in_transaction(), "the session has begun: its connection has the keys on"
    made = create_async_engine(db.engine().url, poolclass=NullPool)
    event.listen(made.sync_engine, "connect", db._pragmas(db.LOCK_TIMEOUT_MS))
    event.listen(made.sync_engine, "connect", _links_off)  # after the app's PRAGMAs: the last word on the keys
    db._guard(made.sync_engine, str(made.url.database), db._limits)
    session.sync_session.bind = made.sync_engine
