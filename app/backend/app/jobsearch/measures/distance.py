"""Miles between two places, worked out in code from a small offline gazetteer of US places (docs/PRD.md 46.8).

The app holds no coordinates, so `data/us_places.csv` ships with it: town, state, latitude, longitude, for the larger US
cities and the better-known suburbs of the larger metros. Nothing is fetched at run time. A place is read from the text
the way `places.py` reads one, as a town and its state together ("Oakfield, OH", "Oakfield, Ohio"), and found only when
the gazetteer has that town in that state: a town it does not know has no distance (a fact not stated decides nothing,
46.7), and a town in another state is another place. A posting that states several places is measured to its nearest.
The distance is the great-circle distance, in miles, between the two towns' centres: a measure for a band like "within
40 miles", not a drive. This module reads no database."""

import csv
import math
import re
from functools import cache
from pathlib import Path

from app.jobsearch.measures.countries import US_CODES
from app.jobsearch.measures.places import STATE_CODE

DATA = Path(__file__).parent / "data" / "us_places.csv"
#: The earth's mean radius in miles.
EARTH_MILES = 3958.8
_NAMES = "|".join(sorted((re.escape(n) for n in STATE_CODE), key=len, reverse=True))
#: "Town, ST" (the code in capitals: "in" and "or" are words) and "Town, State name".
_PAIR = re.compile(rf"([A-Za-z][A-Za-z.' -]{{1,40}})\s*,\s*(?:([A-Z]{{2}})(?![A-Za-z])|({_NAMES})\b)", re.I)
#: The most words of a town's name ("the woodlands", "salt lake city", "west palm beach").
_MOST_WORDS = 4
_DASHES = re.compile(r"\s+[-–—]\s+|-(?=[A-Z]{2}-)|[-–—]\s*$")


@cache
def gazetteer() -> dict[tuple[str, str], tuple[float, float]]:
    """The places, by (town in lowercase, state code), as (latitude, longitude)."""
    with DATA.open(encoding="utf-8", newline="") as handle:
        return {(row["town"], row["state"]): (float(row["lat"]), float(row["lon"])) for row in csv.DictReader(handle)}


def great_circle(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Miles between two (latitude, longitude) points."""
    (la1, lo1), (la2, lo2) = ((math.radians(x), math.radians(y)) for x, y in (a, b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_MILES * math.asin(math.sqrt(h))


def _town_in(words: str, state: str) -> tuple[float, float] | None:
    """The coordinates of the town that ends these words, in this state: the longest run of trailing words the
    gazetteer knows ("Hybrid - Milwaukee" gives Milwaukee, "Salt Lake City" its own three)."""
    pieces = _DASHES.split(words.strip())[-1].split()
    for n in range(min(_MOST_WORDS, len(pieces)), 0, -1):
        found = gazetteer().get((" ".join(pieces[-n:]).lower().strip(".,"), state))
        if found:
            return found
    return None


def places_in(text: str | None) -> list[tuple[float, float]]:
    """The coordinates of every place the gazetteer knows that this text names as a town and its state."""
    found: list[tuple[float, float]] = []
    for pair in _PAIR.finditer(text or ""):
        code, name = pair.group(2), pair.group(3)
        state = code.upper() if code and code.upper() in US_CODES and code == code.upper() else (
            STATE_CODE.get(name.lower()) if name else None)
        if state and (at := _town_in(pair.group(1), state)) and at not in found:
            found.append(at)
    return found


def between(home: str | None, location: str | None) -> float | None:
    """Miles from the home place to the nearest place the posting's location names, or None when either is not a place
    the gazetteer knows ("Remote", a town it lacks, no home)."""
    homes, there = places_in(home), places_in(location)
    if not homes or not there:
        return None
    return min(great_circle(homes[0], other) for other in there)
