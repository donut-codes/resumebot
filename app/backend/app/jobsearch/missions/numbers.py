"""The numbers a mission runs with, shown and changed on the mission they govern (docs/PRD.md 30.7).

They are fields of the profile's Settings document (`profile.Settings`), which used to have a page of its own. That
page is gone: each field moved to the mission whose work reads it, and Claude's peak hours, which hold every mission
that needs Claude, to a section of their own. Storage did not change: a save here is the next version of the
Settings document, with every other field kept, so the history is the profile's.

**Adding a setting to a mission is one line in `NUMBERS` below.** The registry drives the API (`fields` says what the
screen draws; `check` refuses what doesn't fit its kind and limits) and the screen draws whatever the API says, so
the frontend needs nothing. The field itself must exist on `profile.Settings` (a test checks that).

A mission's own done conditions (roles or employers to research, number of matches, most searches, finish by) are not
here: they belong to the mission row and are changed with `missions.change`.

A setting that is a count limit (the sites to learn a night) is marked `unlimited`: it takes -1, no limit
(`limits.py`), as well as a number in its range. Its help line says so.

Services flush; the route commits.
"""

import math
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch import limits
from app.jobsearch.missions.record import ANALYSE, GO, READ, READ_MAIL, SCOUT
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Settings

#: The section for Claude's peak hours, which applies to every mission that needs Claude. The missions' own keys are
#: `missions/record.py`'s.
PEAK = "peak_hours"

#: What an input is: a whole number, a number from 0 to 1, a yes or no, a time of day (HH:MM), a time zone name or
#: a line of words (which `profile.Settings` checks when it is saved).
KINDS = ("whole", "fraction", "flag", "time", "zone", "words")
_CLOCK = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


class NumbersError(DomainError):
    """A value that doesn't fit its setting. The message says what to fix."""

    status = 422


@dataclass(frozen=True)
class Number:
    #: The field of `profile.Settings`.
    name: str
    #: A short name; the screen also uses it in one line ("Research bar 50%").
    label: str
    #: One plain line saying what it does.
    help: str
    kind: str
    #: Limits, inclusive (whole numbers and fractions).
    low: float | None = None
    high: float | None = None
    #: Shown after the value ("%").
    unit: str = ""
    #: Whether it can be left blank (none set).
    optional: bool = False
    #: Whether it is a count limit, which takes -1 for no limit as well as a number in its range (`limits.py`).
    unlimited: bool = False


#: The registry: for each mission key, the Settings fields it governs, in the order the screen shows them.
NUMBERS: dict[str, tuple[Number, ...]] = {
    READ: (
        Number("classifier_min_examples", "Examples before it learns",
               "Once you've kept this many roles, the app learns your taste and puts the likeliest new postings "
               "first. It only changes the order: no posting is hidden or dropped.", "whole", 1, 10000),
        Number("no_reply_days", "Applied roles go quiet after",
               "How many days an Applied role can go without a reply before it moves to Gone Quiet. The days count "
               "from the day you applied or the last mail from the employer, whichever is later. Blank: never.",
               "whole", 1, 3650, " days", optional=True),
    ),
    READ_MAIL: (
        Number("email_lookback_days", "Days of mail to look at",
               "How many days back each read of your mail looks. The first read looks back 14 days, and a read after "
               "a gap reaches back to the last good one. Blank: 3.", "whole", 1, 60, " days", optional=True),
    ),
    SCOUT: (
        Number("site_projects_per_night", "Sites to learn a night",
               "How many of the sites you switched on that have no way to be read yet the scout starts learning in a "
               "night, the ones switched on longest ago first. -1 for no limit.",
               "whole", 1, 100, unlimited=True),
    ),
    ANALYSE: (),  # the roles to research a night is on the mission itself
    GO: (
        Number("second_impression_size", "Roles in Second Impression",
               "How many roles are on deck: the next best after Ready to Launch, each read line by line by the "
               "analyst. It is also how many roles the analyst reads at a time. Blank: 15.",
               "whole", 1, 500, optional=True),
        Number("ready_to_launch_size", "Roles in Ready to Launch",
               "How many roles are in Ready to Launch: the day's list Launch in Browser builds packets for and opens. "
               "Blank: 15.", "whole", 1, 500, optional=True),
        Number("stale_days", "A posting is stale after",
               "How many days old a posting can be before it is stale, counted from when it was posted, else from when "
               "the app found it. A stale posting loses points in the order (below). Default 30.",
               "whole", 1, 3650, " days"),
        Number("stale_penalty", "A stale posting loses",
               "The points a stale posting loses from its score in the order of First Impression, Second Impression "
               "and Ready to Launch, so a fresher role within that many points sorts above it. Default 21.",
               "whole", 0, 100, " points"),
        Number("golden_hours", "A posting found in the last",
               "How many hours a posting counts as new: it gains points in the order (the next setting) and its card "
               "wears the gold edge. 0 turns the boost off. Default 72.", "whole", 0, 8760, " hours"),
        Number("golden_bonus", "A recent posting gains",
               "The points a new posting gains in the order of First Impression, Second Impression and Ready to "
               "Launch, so getting in early is worth something. 0 turns the boost off. Default 20.",
               "whole", 0, 100, " points"),
        Number("research_start_multiple", "Start new research when First Impression holds",
               "New reads wait until First Impression holds at least this many times the Second Impression size in "
               "ranked roles, so the best of many is read first. 0: new reads never wait. Default 3.",
               "whole", 0, 100, " × the Second Impression size"),
        Number("chase_fresh_days", "Chase a missing posting only while at least",
               "A role in Followup needed has no text of its posting, so the browser goes to the employer's own "
               "posting to get it. It does that only while your Freshness hard pass would still pass the role this "
               "many days from now: with a pass of 15 days and this at 3, a role posted up to 12 days ago. A role "
               "past that waits in Followup needed. It is closed only when your Hard pass is next checked and it is "
               "over its limit (saving the Hard pass checks it), not as it gets older. A starred role and a "
               "role in Second Impression are always chased. With no Freshness hard pass set, every such role "
               "is chased. Default 3.", "whole", 0, 3650, " days of freshness are left"),
        Number("days_between_employer_applications", "Days between applications to the same employer",
               "Launch in Browser and the 6:30 tabs skip a role at an employer you applied to within this many days, "
               "and open the next role instead. The skipped role stays in Ready to Launch. Blank: off.",
               "whole", 1, 3650, " days", optional=True),
        Number("employer_places", "Most roles from one employer",
               "For the employers you name, the most roles that hold a place in Second Impression and Ready to Launch "
               "together, written like \"Globex: 4, Initech: 2\". The best roles there keep the places, the rest wait "
               "in First Impression, and the analyst reads the next best roles at other employers. An employer you "
               "don't name has no limit. Blank: no employer has one.", "words", optional=True),
    ),
    PEAK: (
        Number("avoid_peak", "Avoid Claude's peak hours",
               "Peak hours burn tokens faster. When this is checked, no Claude work happens in the window: anything "
               "running stops, the run buttons are off, and everything waits until it ends. Uncheck it to run "
               "something now.", "flag"),
        Number("peak_start", "Peak hours start", "A time of day, in the time zone below.", "time"),
        Number("peak_end", "Peak hours end", "A time of day, in the time zone below.", "time"),
        Number("peak_zone", "Time zone",
               "The zone the peak hours are read in, by its name (Anthropic gives them in Pacific time).", "zone"),
        Number("off_hours_start", "Off hours start",
               "Your own time of day. Outside your off hours the agents start no work of their own; what you start "
               "(Run now, an ask, Launch in Browser, a link you added) still runs.", "time"),
        Number("off_hours_end", "Off hours end", "Your own time of day.", "time"),
    ),
}

BY_NAME: dict[str, Number] = {}
for _section in NUMBERS.values():
    for _number in _section:
        if _number.kind not in KINDS:
            raise ValueError(f"{_number.name}: no kind called {_number.kind!r}; the kinds are {', '.join(KINDS)}")
        if _number.name in BY_NAME:
            raise ValueError(f"{_number.name} is listed twice in NUMBERS")
        BY_NAME[_number.name] = _number


def fields(key: str, settings: Settings) -> list[dict]:
    """What the screen draws for one mission (or `PEAK`): each setting, with its value now. Empty for a mission that
    has none."""
    out = []
    for n in NUMBERS.get(key, ()):
        out.append({"name": n.name, "label": n.label, "help": n.help, "kind": n.kind, "low": n.low, "high": n.high,
                    "unit": n.unit, "optional": n.optional, "unlimited": n.unlimited,
                    "value": getattr(settings, n.name)})
    return out


def _limits(n: Number) -> str:
    if n.low is not None and n.high is not None:
        words = f"{n.low:g} to {n.high:g}{n.unit}"
    else:
        words = f"at least {n.low:g}{n.unit}" if n.low is not None else f"at most {n.high:g}{n.unit}"
    return f"{words}, or -1 for no limit" if n.unlimited else words


def _one(n: Number, raw):
    """One value as the setting stores it, or a NumbersError."""
    blank = raw is None or (isinstance(raw, str) and not raw.strip())
    if blank and n.optional:
        return None
    if blank:
        raise NumbersError(f"{n.label}: needs a value")
    if n.kind == "flag":
        if not isinstance(raw, bool):
            raise NumbersError(f"{n.label}: yes or no")
        return raw
    if n.kind in ("whole", "fraction"):
        if isinstance(raw, bool):
            raise NumbersError(f"{n.label}: a number")
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise NumbersError(f"{n.label}: a number") from None
        if not math.isfinite(number):
            raise NumbersError(f"{n.label}: a number")
        if n.kind == "whole":
            if number != int(number):
                raise NumbersError(f"{n.label}: a whole number")
            number = int(number)
        if n.unlimited and number == limits.NO_LIMIT:
            return limits.NO_LIMIT
        if (n.low is not None and number < n.low) or (n.high is not None and number > n.high):
            raise NumbersError(f"{n.label}: {_limits(n)}")
        return number
    text = str(raw).strip()
    if n.kind == "words":
        return text
    if n.kind == "time":
        m = _CLOCK.fullmatch(text)
        if not m:
            raise NumbersError(f"{n.label}: a time of day like 05:00")
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise NumbersError(f"{n.label}: no time zone called {text!r}, try a name like America/Los_Angeles") from None
    return text


def check(values: dict) -> dict:
    """The values as Settings stores them. A name no mission lists, or a value that doesn't fit, is a NumbersError."""
    if not isinstance(values, dict) or not values:
        raise NumbersError("nothing to change")
    unknown = sorted(set(values) - set(BY_NAME))
    if unknown:
        raise NumbersError(f"{', '.join(unknown)}: not a setting a mission lists")
    return {name: _one(BY_NAME[name], raw) for name, raw in values.items()}


async def save(session: AsyncSession, owner: str, values: dict, *, by: str, why: str = "") -> int | None:
    """Store the values as the next version of the Settings document, every other field as it was. Returns the new
    version, or None when nothing changed."""
    checked = check(values)
    current = (await profile_store.current(session, owner)).settings
    labels = ", ".join(BY_NAME[name].label.lower() for name in checked if getattr(current, name) != checked[name])
    try:
        return await profile_store.save(
            session, owner, "settings", {**current.model_dump(mode="json"), **checked}, by=by,
            why=" ".join(why.split()) or (f"changed {labels}" if labels else ""))
    except profile_store.ProfileError as exc:
        raise NumbersError(str(exc)) from exc
