"""One rule for every timestamp the API sends: timezone-aware ISO, with an
offset. A naive `isoformat()` reads as *local* time to `new Date(iso)` in the
browser, which silently double-applies the zone (the project page's Activity
showed "4:56 pm" for 12:56 pm Eastern). Rendering in the viewer's local zone
is the frontend's job (frontend/src/lib/dates.ts)."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.settings import settings


def utc_now() -> datetime:
    """Now, in UTC, with its zone."""
    return datetime.now(UTC)


_last_unique = datetime.min.replace(tzinfo=UTC)
_unique_lock = threading.Lock()


def utc_now_unique() -> datetime:
    """Now, in UTC, and never the same instant twice in this process: a call the clock has not moved on from since the
    last one (a coarse clock, such as Windows', ticks about once a millisecond) gets a microsecond past the last. For
    rows that are put in order by their time, such as the lines of Activity, which would otherwise tie and come out in
    the order of their random ids. Unique in this process only: the page and the engine each have their own mark, so
    a reader that orders lines by it adds the id as the tie-break (`TrailEvent.at.desc(), TrailEvent.id.desc()`)."""
    global _last_unique
    with _unique_lock:
        now = utc_now()
        _last_unique = now if now > _last_unique else _last_unique + timedelta(microseconds=1)
        return _last_unique


def aware(when: datetime | None) -> datetime | None:
    """A time with its zone: a naive one (a test database hands them back without) is taken as UTC; None stays None."""
    return when.replace(tzinfo=UTC) if when is not None and when.tzinfo is None else when


def iso_utc(dt: datetime) -> str:
    """`dt` as ISO 8601 with an offset; a naive value is taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def iso_or_none(dt: datetime | None) -> str | None:
    """`iso_utc(dt)`, or None when there is no time."""
    return iso_utc(dt) if dt else None


def owner_zone() -> ZoneInfo:
    """The owner's time zone (`JOBSEARCH_TIMEZONE`)."""
    return ZoneInfo(settings.timezone)


def owner_today(now: datetime | None = None) -> date:
    """The owner's local day: the day it is in the owner's time zone (`JOBSEARCH_TIMEZONE`), at `now` or now. The one
    meaning of "today" the app has. Every "today" the owner sees - what is due, the date on a letter, how old
    a posting is - reads this, never the server's UTC date, which is already tomorrow for an evening in the Americas."""
    return (now or datetime.now(UTC)).astimezone(owner_zone()).date()
