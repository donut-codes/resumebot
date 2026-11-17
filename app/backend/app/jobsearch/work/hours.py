"""The night's clock (docs/PRD.md 25.8 and 25.10): Claude's peak hours (weekdays, 5 am to 11 am Pacific, as Anthropic
published them in March 2026), which the owner can ask the missions to avoid, and a mission's latest finish time.
Times of day are read in a zone: the peak window in its own (stored as Pacific, daylight saving followed), a finish
time in the owner's."""

import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Settings
from app.timeutil import owner_zone, utc_now

CLOCK = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def clock(text: str | None) -> time | None:
    m = CLOCK.fullmatch(str(text or "").strip())
    return time(int(m.group(1)), int(m.group(2))) if m else None


def _window_on(day, s: Settings, *, any_day: bool = False) -> tuple[datetime, datetime] | None:
    """The peak window that starts on `day` (a date in the peak zone), as UTC times. Anthropic publishes peak hours for
    weekdays only (Monday to Friday, 5 am to 11 am Pacific), so a Saturday or Sunday has none; `any_day` gives the
    window as it would be on that day, for saying what it is."""
    if day.weekday() >= 5 and not any_day:
        return None
    start, end = clock(s.peak_start), clock(s.peak_end)
    if start is None or end is None:
        return None
    zone = ZoneInfo(s.peak_zone)
    a = datetime.combine(day, start, zone)
    b = datetime.combine(day, end, zone)
    if b <= a:
        b += timedelta(days=1)
    return a.astimezone(UTC), b.astimezone(UTC)


def _windows_near(s: Settings, now: datetime) -> list[tuple[datetime, datetime]]:
    today = now.astimezone(ZoneInfo(s.peak_zone)).date()
    return [w for d in (today - timedelta(days=1), today) if (w := _window_on(d, s))]


def in_peak(s: Settings, now: datetime | None = None) -> bool:
    """Whether new Claude work should wait: the owner asked to avoid peak hours, and it is inside the window."""
    now = now or datetime.now(UTC)
    return bool(s.avoid_peak) and any(a <= now < b for a, b in _windows_near(s, now))


def peak_ends(s: Settings, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(UTC)
    return next((b for a, b in _windows_near(s, now) if a <= now < b), None)


def last_peak_end(s: Settings, now: datetime | None = None) -> datetime | None:
    """The end of the most recent peak window that has ended (the stuck sweep counts waiting time from it)."""
    now = now or datetime.now(UTC)
    ended = [b for _, b in _windows_near(s, now) if b <= now]
    return max(ended) if s.avoid_peak and ended else None


def in_off_hours(s: Settings, now: datetime | None = None) -> bool:
    """Whether it is the owner's off hours now (their own time zone, any day; 1 to 7 AM unless they chose another
    window): the one time the pool starts work of its own. A window with the same start and end is every hour."""
    start, end = clock(s.off_hours_start), clock(s.off_hours_end)
    if start is None or end is None or start == end:
        return True
    at = (now or datetime.now(UTC)).astimezone(owner_zone()).time().replace(second=0, microsecond=0)
    return start <= at < end if start < end else at >= start or at < end


def wall_moment(day, at: time, zone, fold: int = 0) -> datetime:
    """The UTC instant a clock reads `at` on `day` in `zone`. On fall-back night a time inside the repeated hour is
    two instants: `fold` 0 is the first, 1 the second. A time in the hour spring-forward skips is read as the hour
    after it. Times are compared as instants, never as wall-clock times of one zone, which ignore the repeat."""
    return datetime.combine(day, at, zone).replace(fold=fold).astimezone(UTC)


def off_hours_began(s: Settings, now: datetime | None = None) -> datetime | None:
    """When the off-hours window the instant `now` is in began (UTC), or None when the window is every hour. The
    answer for an instant outside the window is the start of the one before it."""
    start, end = clock(s.off_hours_start), clock(s.off_hours_end)
    if start is None or end is None or start == end:
        return None
    zone = owner_zone()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    day = now.astimezone(zone).date()
    began = wall_moment(day, start, zone)
    if began > now:
        began = wall_moment(day - timedelta(days=1), start, zone)
    return began


def next_off_hours_start(s: Settings, now: datetime | None = None) -> datetime:
    """When the owner's off-hours window next begins after `now` (UTC), counted from inside the window too: the answer
    for an instant in the middle of a night is the start of the next one (the night after), and at the very start of a
    window it is the following night's. What a refusal at the board stops it until (docs/PRD.md 48.14 d). A window with
    no start or end, or the same start and end (every hour), has no beginning: a day on."""
    now = now or datetime.now(UTC)
    start, end = clock(s.off_hours_start), clock(s.off_hours_end)
    if start is None or end is None or start == end:
        return now + timedelta(days=1)
    zone = owner_zone()
    day = now.astimezone(zone).date()
    began = wall_moment(day, start, zone)
    if began <= now:
        began = wall_moment(day + timedelta(days=1), start, zone)
    return began


def _words(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def when_words(when: datetime, now: datetime | None = None) -> str:
    """A time as the owner reads it: "3:00 PM" today, "Oct 14, 3:00 AM" another day."""
    local, today = when.astimezone(owner_zone()), (now or utc_now()).astimezone(owner_zone()).date()
    clock = local.strftime("%I:%M %p").lstrip("0")
    return clock if local.date() == today else f"{local.strftime('%b')} {local.day}, {clock}"


def window_words(s: Settings, now: datetime | None = None) -> str:
    """The window in the owner's own time: "8:00 AM to 2:00 PM"."""
    now = now or datetime.now(UTC)
    w = _window_on(now.astimezone(ZoneInfo(s.peak_zone)).date(), s, any_day=True)
    if w is None:
        return "not set"
    zone = owner_zone()
    return f"{_words(w[0].astimezone(zone))} to {_words(w[1].astimezone(zone))}"


def peak_view(s: Settings, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    ends = peak_ends(s, now)
    return {"avoid": bool(s.avoid_peak), "window": window_words(s, now), "start": s.peak_start, "end": s.peak_end,
            "zone": s.peak_zone, "now": ends is not None and bool(s.avoid_peak),
            "resumes_at": ends.isoformat() if ends and s.avoid_peak else None,
            "resumes_words": _words(ends.astimezone(owner_zone())) if ends and s.avoid_peak else None}


#: What a refusal adds to say how to run the work anyway; a screen that cannot offer the checkbox leaves it off.
BLACKOUT_HINT = " To run it now, uncheck \"Avoid Claude's peak hours\" on the Missions tab of Agent Settings."


def blackout(s: Settings, now: datetime | None = None) -> str | None:
    """Why Claude work is refused right now, or None: inside the peak window with the box ticked, nothing runs
    through Claude, not even by hand (docs/PRD.md 25.8)."""
    now = now or datetime.now(UTC)
    ends = peak_ends(s, now)
    if not s.avoid_peak or ends is None:
        return None
    return f"Claude's peak hours: no Claude work until {_words(ends.astimezone(owner_zone()))}." + BLACKOUT_HINT


class PeakHours(Conflict):
    """Claude work asked for inside the peak window the owner chose to avoid; the message says until when."""


async def refuse_in_blackout(session: AsyncSession, owner: str) -> None:
    """Refuse Claude work inside the peak window when the owner asked to avoid it (docs/PRD.md 25.8)."""
    why = blackout((await profile_store.current(session, owner)).settings)
    if why:
        raise PeakHours(why)


def finish_moment(started: datetime, finish_by: str | None) -> datetime | None:
    """The first time `finish_by` (owner's time of day) comes after a run started."""
    at = clock(finish_by)
    if at is None:
        return None
    zone = owner_zone()
    day = started.astimezone(zone).date()
    # The first instant after the start that the clock reads `at`: on fall-back night that may be the second pass
    # through the repeated hour.
    return next(m for d in (day, day + timedelta(days=1)) for fold in (0, 1)
                if (m := wall_moment(d, at, zone, fold)) > started)
