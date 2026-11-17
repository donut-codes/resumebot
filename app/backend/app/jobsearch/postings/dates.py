"""The day a posting says it was posted, read off what the page says (the owner, 2026-10-17: the job pages usually say
"Posted 3 days ago" or "Posted on October 7, 2026", and a role that shows only the day the app found it looks older or
younger than it is).

`freshness.posting_hours`, the freshness ranking and the gold edge count from a role's `posted` first and its
`found` day otherwise, so this is where a posted day is read from a page's text, from the job system's own date or from
the schema.org `datePosted`, and set on the role and its postings where they have none.

Three rules, all here:
- A relative date ("3 days ago", "yesterday", "today") is counted from when the page was read (`read_at`), never from
  today: a text read last week that said "3 days ago" means a day last week. The day of the read is the owner's day
  (`timeutil.owner_today`), not the UTC date: at 8 pm in New York the UTC date is already tomorrow, and "Posted today"
  read then is today's date for the owner, not tomorrow's. Every day here is the owner's day, the day found included.
  Minutes and hours ("Posted 8 hours ago") are counted back from the instant of the read, and the day is the owner's day
  of the moment they reach: read at 2 am, eight hours ago was yesterday evening. Given only a day (a caller that has no
  instant), they are that day.
- A day in the future (after the owner's day of the read) is never kept; neither is a day read off a page's words that
  is more than a day after the day the role was found (a page that says "today" cannot be older than what the app
  saw). A day the job system or the page's schema.org data gives is the employer's own record and is only held to the
  first rule.
- A date a role or posting already has is never replaced by a vaguer one: "30+ days ago" only fills a gap, and a day the
  job system gave stands.

Pure functions, plus `apply`, which sets columns and never commits."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.timeutil import aware, owner_today, owner_zone, utc_now

#: How far past the found day a day read off a page's words may be.
FOUND_SLACK = timedelta(days=1)
#: A page's first lines are the posting's header (title, place, "Posted 3 days ago"): a bare "6 days ago" counts only
#: there.
HEADER_LINES = 40

_UNIT_DAYS = {"minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "wk": 7, "month": 30, "mo": 30}
#: The units shorter than a day: counted back from the instant of the read, not in days.
_UNIT_TIME = {"minute": timedelta(minutes=1), "min": timedelta(minutes=1), "hour": timedelta(hours=1),
              "hr": timedelta(hours=1)}
_UNITS = r"(?:minute|min|hour|hr|day|week|wk|month|mo)s?"
_AGO = re.compile(rf"(\d+)\s*(\+)?\s*({_UNITS})\s+ago\b", re.I)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_US = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_LEAD = re.compile(r"^(?:re)?posted(?:\s+on)?\s+", re.I)
_ORDINAL = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.I)
#: The words a page puts before the date, then the date in any of the forms a page prints.
_WORDS = r"(?:date\s+posted|posting\s+date|posted(?:\s+on|\s+date)?|reposted(?:\s+on)?)"
_WHEN = (rf"(?:\d{{4}}-\d{{2}}-\d{{2}}|[A-Za-z]{{3,9}}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
         rf"|\d{{1,2}}\s+[A-Za-z]{{3,9}}\.?,?\s+\d{{4}}|\d{{1,2}}/\d{{1,2}}/\d{{4}}"
         rf"|\d+\s*\+?\s*{_UNITS}\s+ago|today|yesterday|just now|moments ago)")
_LABELLED = re.compile(rf"\b{_WORDS}\s*[:\-–]?\s*({_WHEN})", re.I)
_JUST_POSTED = re.compile(r"\bjust posted\b", re.I)
_BARE_AGO = re.compile(rf"^\W*(\d+\s*\+?\s*{_UNITS}\s+ago)\b", re.I | re.M)


@dataclass(frozen=True)
class Posted:
    """A day a page or a system gave for a posting: `vague` when it only said "30+ days ago" (a day at least that long
    ago), `structured` when it is the job system's own date or the page's schema.org `datePosted`."""

    day: date
    vague: bool = False
    structured: bool = False


def _squash(text: Any) -> str:
    return " ".join(str(text or "").split())


def ago(amount: int | str, unit: str, when: date | datetime) -> date | None:
    """The day `amount` of `unit` ("minute", "min", "hour", "hr", "day", "week", "wk", "month", "mo", singular or
    plural) before the read. `when` is the instant of the read or only its day. Days, weeks and months count whole days
    back from the owner's day of the read; minutes and hours, given the instant, are taken from it and the day is the
    owner's day of the moment they reach (8 hours before 2 am is yesterday); given only a day, they are that day. None
    when the count reaches back past what the calendar holds: the amount is a page's own words, given as the digits
    it wrote, which may be any number of them (`int` itself refuses a run of thousands)."""
    unit = unit.lower().rstrip("s")
    try:
        amount = int(amount)
        if isinstance(when, datetime):
            moment = aware(when) or utc_now()
            if unit in _UNIT_TIME:
                return owner_today(moment - amount * _UNIT_TIME[unit])
            return owner_today(moment) - timedelta(days=amount * _UNIT_DAYS[unit])
        return when - timedelta(days=amount * _UNIT_DAYS[unit])
    except (OverflowError, ValueError):
        return None


def parse(phrase: str | None, when: date | datetime) -> Posted | None:
    """The day one "posted" phrase means: an ISO date, "Oct 7, 2026", "7/10/2026" (month first), "yesterday",
    "3 days ago", "30+ days ago", "8 hours ago". None when it names none. `when` is when the phrase was read: the
    instant, which counts hours and minutes across midnight (`ago`), or only the day (the owner's) it was read."""
    t = _LEAD.sub("", _squash(phrase).lower())
    if not t:
        return None
    today = owner_today(aware(when)) if isinstance(when, datetime) else when
    if m := _ISO.search(t):
        return _day(int(m[1]), int(m[2]), int(m[3]))
    if m := _US.search(t):
        return _day(int(m[3]), int(m[1]), int(m[2]))
    cleaned = _ORDINAL.sub("", t).replace(".", "").replace(",", ", ").replace(",  ", ", ")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%d %b, %Y", "%d %B, %Y"):
        try:
            return Posted(datetime.strptime(cleaned.title(), fmt).date())
        except ValueError:
            continue
    if "yesterday" in t:
        return Posted(today - timedelta(days=1))
    if "today" in t or "just now" in t or "moments ago" in t or "just posted" in t:
        return Posted(today)
    if m := _AGO.search(t):
        day = ago(m[1], m[3], when)
        return Posted(day, vague=bool(m[2])) if day is not None else None
    return None


def _day(year: int, month: int, day: int) -> Posted | None:
    try:
        return Posted(date(year, month, day))
    except ValueError:
        return None


def find(text: str | None, read_at: datetime | None = None) -> Posted | None:
    """The day a page's text says the posting was posted: "Posted 10 Days Ago", "Posted on October 7, 2026", "Date
    posted: 2026-10-07", "Reposted 2 weeks ago", "30+ days ago", "Posted today", "Just posted", and, in the header (the
    first lines), a line that starts with "6 days ago". Relative days count from `read_at`. The first one the text
    gives wins; None when it gives none."""
    kept = text or ""
    moment = aware(read_at) or utc_now()
    for m in _LABELLED.finditer(kept):
        if got := parse(m.group(1), moment):
            return got
    header = "\n".join(kept.splitlines()[:HEADER_LINES])
    if _JUST_POSTED.search(header):
        return Posted(owner_today(moment))
    if m := _BARE_AGO.search(header):
        return parse(m.group(1), moment)
    return None


_STAMP = re.compile(
    r"\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?)?", re.I)


def instant(value: Any) -> datetime | None:
    """The aware instant a posting time names: milliseconds or seconds since the epoch, or an ISO time that carries a
    zone (`Z` or an offset). None for a date-only value, a time with no zone, or anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10**11 else value
        if seconds < 10**9:
            return None
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str) and (m := _STAMP.match(value)) and m[4] and m[5]:
        zone = "+00:00" if m[5].upper() == "Z" else m[5].replace(":", "")
        if zone != "+00:00":
            zone = f"{zone[:3]}:{zone[3:] or '00'}"
        seconds = m[4] if m[4].count(":") == 2 else m[4] + ":00"
        try:
            return datetime.fromisoformat(f"{m[1]}-{m[2]}-{m[3]}T{seconds}{zone}")
        except ValueError:
            return None
    return None


def instant_day(value: Any) -> date | None:
    """The owner's day of a posting time: an instant (`instant`) is cut to the day it is in the owner's zone, not the
    UTC day; a date-only value, or a time with no zone, keeps the day as written (the employer's day). None for
    anything else."""
    got = instant(value)
    if got is not None:
        return got.astimezone(owner_zone()).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and (m := _STAMP.match(value)):
        made = _day(int(m[1]), int(m[2]), int(m[3]))
        return made.day if made is not None else None
    return None


def structured(value: Any) -> Posted | None:
    """A day a job system or a page's `datePosted` gave, as the ISO date (or the timestamp's date) it is; an instant
    is the owner's day of it (`instant_day`). None for anything else."""
    if isinstance(value, datetime):
        return Posted(day, structured=True) if (day := instant_day(value)) is not None else None
    if isinstance(value, date):
        return Posted(value, structured=True)
    if isinstance(value, str) and (m := _ISO.search(value) or _STAMP.match(value)):
        if _STAMP.match(value) and (stamped := instant_day(value)) is not None:
            return Posted(stamped, structured=True)
        got = _day(int(m[1]), int(m[2]), int(m[3]))
        return Posted(got.day, structured=True) if got is not None else None
    return None


def _owner_day(moment: datetime) -> date:
    """The owner's day at `moment` (`timeutil.owner_today`); a naive time (a test database hands them back without a
    zone) is taken as UTC."""
    return owner_today(aware(moment))


def allowed(got: Posted | None, read_at: datetime, found: date | None) -> bool:
    """Whether a day may be kept: not in the future (against the day it was read), and, for a day read off a page's
    words, not more than a day after the day the role was found."""
    if got is None or got.day > _owner_day(read_at):
        return False
    return got.structured or found is None or got.day <= found + FOUND_SLACK


def day_of(text: str | None, read_at: datetime, found: date | None = None) -> date | None:
    """The day a text says it was posted, when it may be kept (`find`, `allowed`)."""
    got = find(text, read_at)
    return got.day if got is not None and allowed(got, read_at, found) else None


def apply(role: Any, postings: list, *, text: str | None = None, system: Any = None,
          read_at: datetime | None = None) -> bool:
    """Set `posted` on a role and on its postings where they have none, from the job system's own date (`system`: the
    API's day or the page's `datePosted`) when there is one, else from what the page's text says. A day already there
    stays. A role with none takes the day one of its postings holds (the one the job system gave). Returns whether
    anything was set. Never commits."""
    read_at = read_at or datetime.now(UTC)
    found = getattr(role, "found", None)
    if found is None:
        seen = [p.first_seen for p in postings if getattr(p, "first_seen", None)]
        found = _owner_day(min(seen)) if seen else None
    got = structured(system)
    if not allowed(got, read_at, found):
        got = find(text, read_at)
        if not allowed(got, read_at, found):
            got = None
    changed = False
    if got is not None:
        for row in (role, *postings):
            if row.posted is None:
                row.posted = got.day
                changed = True
    if role.posted is None:
        held = sorted(p.posted for p in postings if p.posted is not None and p.posted <= _owner_day(read_at))
        if held:
            role.posted = held[0]
            changed = True
    return changed
