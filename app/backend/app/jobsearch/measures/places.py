"""Whether a posting's location line names a place the owner listed (the work location gate's lists for hybrid and in
person, docs/PRD.md 30.4): a town and its state together.

A listed place written with its state ("Oakfield, OH") is that town in that state. The same town in another state
("Oakfield, NJ") is another place, and a posting that names the town and no state ("Oakfield") cannot be told from them:
it is unknown, not a pass. A listed place written alone ("Oakfield", or an area such as "Greater Springdale") matches by
its name wherever a posting writes it, whatever state follows: the app cannot know which state the owner means, so the
owner says it by writing it ("Oakfield, OH").

A state is read as its two-letter code in capitals ("Oakfield, OH", "Oakfield OH 44001", "OH - Oakfield",
"US-OH-Oakfield") or as its name ("Oakfield, Ohio"), after the town or before it with a dash between. A code in small
letters is not read: "in" and "or" are words. Names only, no lookup, like `countries.py`."""

import re

from app.jobsearch.measures.countries import US_CODES, US_STATES

#: A state's name (lowercased) to its code; `US_STATES` and `US_CODES` are in step.
STATE_CODE = dict(zip((s.lower() for s in US_STATES), US_CODES, strict=False))
#: The dashes that join a state to the town beside it (a hyphen, an en dash, an em dash), and what may stand between a
#: town and the state written after it.
_DASHES = "-\N{EN DASH}\N{EM DASH}"
_AFTER = " ,./(" + _DASHES
#: Where the words after a town stop being a state's name, and where the words before one start.
_STOPS = re.compile(r"[,;|/()]")
_BEFORE_STOPS = re.compile(rf"\s*[{re.escape(_DASHES)},;|/()]\s*")
_CODE = re.compile(r"([A-Z]{2})(?![A-Za-z])")


def split_place(listed: str) -> tuple[str, str | None]:
    """(the town or area, the code of its state) of a listed place: "oakfield, oh" is ("oakfield", "OH"), and so is
    "Oakfield, Ohio". A place with no state after its last comma is (itself, None)."""
    name = " ".join(listed.lower().split())
    town, comma, tail = name.rpartition(",")
    tail = tail.strip()
    state = tail.upper() if tail.upper() in US_CODES else STATE_CODE.get(tail)
    if comma and town.strip() and state:
        return town.strip(), state
    return name, None


def _state_named(words: list[str], at_end: bool = False) -> str | None:
    """The code of the state these words begin with (`at_end`: end with) by its name, up to three words ("District of
    Columbia")."""
    for n in (3, 2, 1):
        state = STATE_CODE.get(" ".join(words[-n:] if at_end else words[:n]).lower())
        if state:
            return state
    return None


def _state_after(rest: str) -> str | None:
    """The state written right after a town, as its code: ", OH", " OH 44001", " - Ohio", ", New Jersey, USA"."""
    rest = rest.lstrip(_AFTER)
    code = _CODE.match(rest)
    if code and code.group(1) in US_CODES:
        return code.group(1)
    return _state_named(_STOPS.split(rest, maxsplit=1)[0].split())


def _state_before(before: str) -> str | None:
    """The state written right before a town with a dash between them: "OH - ", "US-OH-", "USA - OH - ", "Ohio - "."""
    tail = before.rstrip()
    if not tail or tail[-1] not in _DASHES:
        return None
    segment = _BEFORE_STOPS.split(tail.rstrip(_DASHES + " "))[-1]  # the last piece before the dash
    if segment in US_CODES:
        return segment
    return _state_named(segment.split(), at_end=True)


def match(location: str, listed: str) -> bool | None:
    """Whether the posting's `location` names the place `listed` (a lowercased entry of the owner's list).

    True when it does: the town, and for a listed place with a state, that state beside it. False when it does not,
    or names the town in another state. None when it names the town of a listed place that has a state and gives it
    no state, so it cannot be told. A listed place with no state matches by its name, in any state."""
    town, state = split_place(listed)
    unsure = False
    for found in re.finditer(rf"(?<!\w){re.escape(town)}(?!\w)", location, re.IGNORECASE):
        if state is None:
            return True
        there = _state_after(location[found.end():]) or _state_before(location[:found.start()])
        if there == state:
            return True
        unsure = unsure or there is None
    return None if unsure else False
