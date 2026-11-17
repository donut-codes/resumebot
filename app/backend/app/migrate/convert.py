"""How a value the source database holds is written the way the app writes it to SQLite.

The old engine's driver hands a value back as a Python object (`uuid.UUID`, an aware `datetime`, a `date`, `bytes`),
and a JSON column as the text Postgres holds. The copier writes each through the bind processor of the target
column's own type, so a value is stored as the app stores one it made itself: a uuid as 32 hex digits, a time as
UTC text to the microsecond, a date as its ISO text, a boolean as 0 or 1. JSON is the one type it does not give to
the type: the text is written as it is, so a JSON `null` stays the text `null` and a missing value stays SQL NULL
(`sa.JSON` would write both as one of them, and read both back as `None`).
"""

import json
import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from app.timeutil import aware, owner_today, utc_now

_SQLITE = sqlite.dialect()

UUID, DATETIME, DATE, BOOL, BLOB, JSON, PLAIN = "uuid", "datetime", "date", "bool", "blob", "json", "plain"


def kind(column: sa.Column[Any]) -> str:
    """What sort of value the column holds, as far as the copy cares (`PLAIN`: a number or text, as it is)."""
    kinds = ((sa.JSON, JSON), (sa.Uuid, UUID), (sa.DateTime, DATETIME), (sa.Date, DATE), (sa.Boolean, BOOL),
             (sa.LargeBinary, BLOB))
    # a TypeDecorator (`UtcDateTime`) is not a subclass of the type it is made on: its `impl` is asked as well
    types = (column.type, getattr(column.type, "impl_instance", None))
    return next((name for sort, name in kinds if any(isinstance(t, sort) for t in types)), PLAIN)


def _json_text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bytes | memoryview):
        return bytes(value).decode()
    return json.dumps(value)  # a driver that decoded the column (a list or a dict): the text it came from


def _uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def stored(column: sa.Column[Any]) -> Callable[[Any], Any]:
    """The function that turns a value of the source database's column into the value to bind for the SQLite column."""
    sort = kind(column)
    if sort == JSON:
        return _json_text
    if sort == BLOB:
        return lambda value: None if value is None else bytes(value)
    process = column.type.dialect_impl(_SQLITE).bind_processor(_SQLITE)
    if sort == PLAIN or process is None:
        return lambda value: value
    if sort == UUID:
        return lambda value: None if value is None else process(_uuid(value))
    return lambda value: None if value is None else process(value)


def day_of(when: datetime | None) -> date:
    """The owner's local day of a time (`owner_today`): what the copy stores for a role's `created_day` and a posting's
    `seen_day` when the row is written. A row with no time has the day it is copied."""
    return owner_today(aware(when) if when is not None else utc_now())
