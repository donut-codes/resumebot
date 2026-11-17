"""Column types the engine's tables use, and the clock that fills their time columns.

The database is SQLite, which has no time zone type: `UtcDateTime` makes one by storing every time in UTC and giving
every time back with its zone, so an aware `datetime` goes in and an aware `datetime` comes out. The rows' own times
are filled by the app (`utc_now`), not by the database, so a row gets the same time whichever engine writes it.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    """Now, in UTC, with its zone."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A moment in time: stored as UTC, returned aware (in UTC). A naive `datetime` going in is taken as UTC, as
    `app.timeutil.aware` takes one; None stays None. Times sort the same as text and as moments, as every stored time
    is UTC to the microsecond."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
