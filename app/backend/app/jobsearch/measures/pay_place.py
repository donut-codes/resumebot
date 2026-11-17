"""The facts the screening process decides on, and the gates in the shape it compares them.

The owner's gates are part of their profile (`profile/`, set in the app);
`profile_views.engine_gates` turns them into a `Gates`. The *thresholds* are conditions in
`playbooks/jobsearch/screen_one_role.bpmn`; this module only works out the facts those conditions compare
(`role_facts`): a role's age in days, whether its location is acceptable, whether it names an excluded employer, the
figure its pay gives. A gate passes until it is proven failed (docs/PRD.md 30.2): a place the posting doesn't make
clear, pay it doesn't post and an employer fact nobody has found all pass. No gate reads a role's fit: Prioritization
ranks roles and never stops one (docs/PRD.md 30.4).

Order, cheapest first: excluded employer, location, pay, staleness.
"""

import re
from dataclasses import asdict, dataclass, field
from datetime import date

from app.jobsearch.measures.countries import abroad
from app.jobsearch.measures.places import match as place_match

#: What a new pay gate compares its amount with when the owner has not chosen: the posted maximum, so a band that
#: reaches the amount is kept whatever its bottom is. (An install that chose "ask" has it stored: migration 0044.)
PAY_AGAINST_DEFAULT = "maximum"

WORK_MODES = ("remote", "hybrid", "onsite", "restricted_remote", "unknown")

#: The numbers and words the process's conditions read (`gates.<name>`); the lists stay with the code that matches text.
#: `says` is each gate's sentence, the reason a role it stops is given.
PROCESS_KEYS = ("stale_days", "comp_floor", "says")


@dataclass
class Gates:
    """The gates as the process compares them. The defaults let every role through: no age rule, no pay amount, no
    excluded employer, no place rule (`profile_views.engine_gates` fills in what the owner has turned on)."""

    #: A posting older than this many days is gated out (the posting-age gate; the default is older than any posting).
    stale_days: int = 100_000
    #: The amount a year the pay must reach (0: no pay gate), what it is compared with (`minimum`, `maximum` or `ask`)
    #: and how wide a posted band may be before its middle is not taken as the ask.
    comp_floor: int = 0
    comp_against: str = PAY_AGAINST_DEFAULT
    comp_wide_band_ratio: float = 1.5
    excluded_employers: list[str] = field(default_factory=list)
    #: False: the owner has no work location gate, so every place is fine.
    places_gated: bool = False
    remote_ok: bool = True
    #: Where a remote role may be (the countries, as the owner listed them); empty: any.
    remote_countries: list[str] = field(default_factory=list)
    #: Hybrid and in person: allowed, and the locations (lowercased) that count; allowed with none: anywhere.
    hybrid_ok: bool = True
    hybrid_places: list[str] = field(default_factory=list)
    onsite_ok: bool = True
    onsite_places: list[str] = field(default_factory=list)
    #: The sentence of each gate that is on (location, compensation, age): the reason a role it stops is given.
    says: dict[str, str] = field(default_factory=dict)
    #: A target role's own gates, by the role's id (Hard pass's role tabs, docs/PRD.md 31.9): the same numbers and
    #: lists, only the ones the role's tab has on. They ADD to these (All's): a posting the title pass matched to the
    #: role must pass both.
    by_role: dict[str, "Gates"] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    def adds_anything(self) -> bool:
        """Whether any gate here is on: a place rule, a pay amount or an age limit."""
        plain = Gates()
        return self.places_gated or self.comp_floor != plain.comp_floor or self.stale_days != plain.stale_days

    def for_process(self) -> dict:
        """What the process reads as `gates`: only the numbers and words its conditions compare."""
        return {k: getattr(self, k) for k in PROCESS_KEYS}


# --- the facts a role is decided on ----------------------------------------------------------------------------


_HOURLY = re.compile(r"/\s*h(?:ou)?r|per hour|an hour|hourly", re.I)
_MONTHLY = re.compile(r"/\s*mo(?:nth)?(?![a-z])|per month|a month|monthly", re.I)
#: A figure that is not the salary: a bonus, a stipend or equity named right after it ("$5,000 signing bonus") or right
#: before it ("signing bonus of $5,000").
_EXTRA = r"(?:signing|sign-on|relocation|retention|referral|performance|annual|cash|stock)?\s*(?:bonus|stipend|equity)"
_EXTRA_AFTER = re.compile(rf"^\s*(?:{_EXTRA}|sign-?on|signing)", re.I)
_EXTRA_BEFORE = re.compile(rf"(?:{_EXTRA}|sign-?on|signing)\s*(?:of|is|up to|worth|:|-)?\s*$", re.I)
#: A month's pay this size or more is a year's already.
MONTHLY_CEILING = 30_000


_NUMBER = r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?"
#: A figure with a currency mark ("$" or "USD") and, in a range, the second figure after a dash or "to" without one
#: ("USD 40 - 50"). A bare number with no mark anywhere in its range ("5 days", "401k") is never a figure.
_FIGURE = re.compile(
    r"(?:\$|\bUSD)\s?" + _NUMBER
    + r"(?:\s*(?:-|–|—|\bto\b)\s*(?!\$|USD)" + _NUMBER + r"(?![\d%]))?", re.I)


def _figures(text: str):
    """(start, end, digits, k) of each pay figure in a line, in order."""
    for m in _FIGURE.finditer(text):
        yield m.start(), m.end(1) + len(m.group(2) or ""), m.group(1), m.group(2)
        if m.group(3):
            yield m.start(3), m.end(3) + len(m.group(4) or ""), m.group(3), m.group(4)


_PART_TIME = re.compile(r"\bfractional\b|\bpart[-_ ]?time\b|\bper[- ]engagement\b", re.I)
_HOURS_A_WEEK = re.compile(r"\b(\d{1,3})(?:\s*(?:-|–|to)\s*(\d{1,3}))?\s*(?:hours|hrs?)\s*(?:a|per|/)\s*week\b", re.I)
#: Hours a week this many or more is full time.
FULL_TIME_HOURS = 30


def is_part_time(*texts: str | None) -> bool:
    """Whether the title or text says the role is fractional, part time, paid per engagement or under 30 hours a week
    (whole words; docs/PRD.md 30.4). Such a role's hourly pay is no full-time year."""
    text = " ".join(t for t in texts if t)
    if _PART_TIME.search(text):
        return True
    return any(int(m.group(2) or m.group(1)) < FULL_TIME_HOURS for m in _HOURS_A_WEEK.finditer(text))


def hourly_part_time(title: str | None, comp: str | None, description: str | None = None) -> bool:
    """Whether the role is part time (`is_part_time`) and its pay is posted by the hour: the pay that makes no yearly
    range and no yearly ask."""
    return bool(comp and _HOURLY.search(comp)) and is_part_time(title, comp, description)


def parse_comp_text(text: str | None, *, part_time: bool = False) -> tuple[int | None, int | None]:
    """(low, high) a year from a pay line like `$120,000 - $160,000`, `$120K-$160K` or `up to $150K`; hourly
    figures are read as a 2,080-hour year and monthly ones as twelve months. A bonus, stipend or equity figure is not
    the pay ("$120,000 plus a $5,000 signing bonus" is $120,000). (None, None) when the line has no figure, or when
    `part_time` and the pay is hourly: a fractional or part-time role's hour is not a full-time year's."""
    if not text:
        return None, None
    hourly, monthly = bool(_HOURLY.search(text)), bool(_MONTHLY.search(text))
    if part_time and hourly:
        return None, None
    found = []
    for start, end, digits, kilo in _figures(text):
        if _EXTRA_AFTER.match(text[end:end + 30]) or _EXTRA_BEFORE.search(text[max(0, start - 30):start]):
            continue
        value = float(digits.replace(",", ""))
        value = value * 1000 if kilo else value
        if hourly and value < 1000:
            value *= 2080
        elif monthly and not hourly and value < MONTHLY_CEILING:
            value *= 12
        elif not kilo and value < 1000:
            continue  # "$5" in "5 days" is not a salary
        found.append(int(round(value)))
    if not found:
        return None, None
    return min(found), max(found)


def provisional_ask(low: int | None, high: int | None, wide_ratio: float = 1.5) -> int | None:
    """The estimated ask before research works one out: the middle of the posted band, or the one figure given. A band
    whose top is over `wide_ratio` times its bottom is too wide to judge (None: kept, left to later); no pay is None
    too."""
    if low and high:
        if high > wide_ratio * low:
            return None
        return int(round(low + 0.5 * (high - low)))
    return low or high or None


#: What a figure the pay gate compared is called, for the reason a role is given.
PAY_FROM = {"minimum": "posted minimum", "maximum": "posted maximum", "ask": "estimated ask"}


def pay_check(low: int | None, high: int | None, gates: Gates,
              worked: int | None = None) -> tuple[str, int | None, str]:
    """The pay gate on one role (docs/PRD.md 30.4): ("pass" | "fail" | "unknown", the figure compared, what it is).

    The amount is compared with the posted minimum, the posted maximum or the estimated ask, as the owner chose. The
    estimated ask is the one the analyst worked out (`worked`). Until then the posted range settles an ask-based gate
    only where it is certain (the owner, 2026-10-19: `posted_pay_verdict`): a middle `POSTED_MARGIN` or more under the
    amount fails, since no ask is above the middle, whatever the width of the band; a band the ask cannot be under the
    amount for passes; anything nearer is left for the analysis ("unknown"). With no posted pay the estimated ask is
    used whichever was chosen; with neither the role passes ("unknown": nothing to judge). One posted figure is both
    the minimum and the maximum. The one rule for every path that judges a role: the gate-facts step before analysis,
    the re-gate and the settle after research all come here (docs/PRD.md 39.11)."""
    amount = gates.comp_floor
    if not amount:
        return "unknown", None, ""
    low, high = low or high or None, high or low or None
    posted = low is not None
    if posted and gates.comp_against == "minimum":
        figure, source = low, PAY_FROM["minimum"]
    elif posted and gates.comp_against == "maximum":
        figure, source = high, PAY_FROM["maximum"]
    else:
        figure, source = (worked, PAY_FROM["ask"]) if worked is not None else (None, "")
        if figure is None and posted:
            figure = provisional_ask(low, high, gates.comp_wide_band_ratio)
            source = PAY_FROM["ask"]
            certain = posted_pay_verdict(low, high, gates)[0] == "fail"
            if figure is None and high is not None and high < amount:
                figure, source = high, PAY_FROM["maximum"]
            elif figure is None and certain:  # too wide to find the middle, but the middle is under all the same
                figure = int(((low or 0) + (high or 0)) / 2)
            elif figure is not None and figure < amount and not certain:
                figure = None  # the middle is under, but no ask is certain to be: left for the analysis
    if figure is None:
        return "unknown", None, ""
    return ("fail" if figure < amount else "pass"), figure, source


#: The most the estimated ask can sit above the middle of a posted range (comp.py, method v4): the ask is at most the
#: likely offer, which is at most the midpoint, except the round-up to the next $1,000 that lifts a single figure or a
#: range under $2,000 wide by less than $1,000.
POSTED_MARGIN = 1_000


def posted_pay_verdict(low: int | None, high: int | None, gates: Gates) -> tuple[str, str]:
    """What the posted range alone settles of an ask-based pay gate, before any analysis: ("fail" | "pass" | "unknown",
    the evidence). A midpoint at least `POSTED_MARGIN` under the floor is a certain fail (no ask is above it); a bottom
    at or above the floor is a certain pass (no ask is below it); anything else, or no range, or a gate that is not on
    the estimated ask, is unknown (left for the analysis). Hourly pay is annualised by `parse_comp_text`."""
    floor = gates.comp_floor
    if not floor or gates.comp_against != "ask":
        return "unknown", ""
    low, high = low or high or None, high or low or None
    if low is None or high is None:
        return "unknown", ""
    low, high = min(low, high), max(low, high)
    mid = (low + high) / 2
    posted = f"${low:,}" if low == high else f"${low:,}-${high:,}"
    if mid <= floor - POSTED_MARGIN:
        return "fail", (f"the posted {posted} has its midpoint at ${mid:,.0f}, at least ${POSTED_MARGIN:,} under the "
                        f"${floor:,} pay floor, so the estimated ask is under it")
    if low >= floor:
        return "pass", f"the posted {posted} starts at or above the ${floor:,} pay floor"
    return "unknown", ""


def mostly_below_floor(low: int | None, high: int | None, floor: int) -> tuple[int, int] | None:
    """The posted range (low, high) when its middle is under the floor but its top reaches it: a role the pay gate keeps
    (the gate closes a role only when the top is under the floor) that the owner wants told, and the ranker given. None
    for no floor, no range, a range whose middle reaches the floor, or one whose top does not (the gate closes that)."""
    if not floor or not low or not high or low >= high:
        return None
    return (low, high) if (low + high) / 2 < floor <= high else None


def below_floor_words(band: tuple[int, int]) -> str:
    return f"Pay mostly below your floor: ${band[0]:,}–${band[1]:,}"


def age_in_days(posted: date | None, found: date | None, today: date) -> int | None:
    """Days since the posting's own date; failing that since the day it was first seen; None when neither is known
    (an unknown age is never stale)."""
    when = posted or found
    if when is None:
        return None
    return max((today - when).days, 0)


#: A word that is no place: a count of places ("26 Locations", which is all a job system's list says of a posting that
#: has several), "Multiple Locations", "Various", or a placeholder ("N/A", "Not stated").
_VAGUE = (r"(?:(?:\d+|multiple|various|several|many)\s+)?locations?|various|n\s*/\s*a|none|unknown|unspecified|tbd|"
          r"not\s+(?:stated|specified|given|provided|available|listed)")
#: A location that says nothing about where the work is (an empty one too): no place and no country.
_SAYS_NOTHING = re.compile(rf"^\W*(?:{_VAGUE})?\W*$", re.I)
_COUNTED = re.compile(r"^\W*(\d+)\s+locations?\W*$", re.I)


def says_no_place(location: str | None) -> bool:
    """Whether a location says nothing about where the work is: empty, or only a word that is no place ("26 Locations",
    "Multiple Locations", "N/A", "Not stated"). Such a place is unknown: it neither passes nor fails the work location
    gate, and the filter goes on to the next step instead of stopping the posting."""
    return bool(_SAYS_NOTHING.match(location or ""))


def counted_places(location: str | None) -> int | None:
    """How many places a location that is only a count names ("26 Locations": 26), else None."""
    found = _COUNTED.match(location or "")
    return int(found.group(1)) if found else None


# --- a location written as free text ---------------------------------------------------------------------------------
#
# A scout or a reader writes the place as a sentence as often as a town: "Not stated (HOLD)", "US, work mode not
# stated", "Remote or Atlanta, GA (listing conflicts: ...)", "United States Work at Home". The gate reads it in pieces.

#: What ends one piece of a written place: a comma, semicolon, colon, slash, bar or bracket, or a dash with spaces round
#: it ("N/A" is taken out first: its slash is no break).
_PIECES = re.compile(r"[,;:|/()\[\]]|\s[-\N{EN DASH}\N{EM DASH}]\s|[\N{EN DASH}\N{EM DASH}]")
#: A piece that is a note about the posting and no place: "not stated", "work mode not stated", "posting does not say",
#: "no city or work mode in JD", "HOLD".
_NOTE = re.compile(
    r"\b(?:not|no|never|unknown|unspecified|unclear|tbd|hold|stated|specified|mentioned|posting|listing|jd)\b"
    r"|\w+n(?:'|\N{RIGHT SINGLE QUOTATION MARK})t\b", re.I)
#: The words in a piece that name no town: a country (for hybrid and in person that is no place), a way of working, a
#: count, and the small words between them.
_NO_TOWN = re.compile(
    r"(?<![a-z])(?:united states|usa|u\.s\.a?\.?|us|nationwide|north america"
    r"|remote(?:ly)?|hybrid|on[\s-]?site|in[\s-]?(?:office|person)|flexible|telecommut\w*|wfh|home[\s-]?based"
    r"|work(?:ing)?[\s-]+(?:at|from)[\s-]+home"
    r"|\d+|multiple|various|several|many|other|others|locations?|and|or|in|the|a|an|of|at|to|for|from|with|within"
    r"|only|if|based)(?![a-z])", re.I)
#: A posting that says it is remote: the word, and the other ways a board says it ("Work at Home", "Telecommuter"). A
#: remote that is denied ("not remote", "no remote work", "remote work is not available") is not one; "remote not
#: stated" does not deny it either, it only does not say.
_REMOTE = re.compile(
    r"\b(?:remote(?:ly)?|telecommut\w*|wfh|home[\s-]?based|work(?:ing)?[\s-]+(?:at|from)[\s-]+home)\b", re.I)
_REMOTE_DENIED = re.compile(
    r"\b(?:not|non|no|never|without)\b[\s-]+"
    r"(?:(?:fully|completely|entirely|full|100%|a|an|any|eligible|for)[\s-]+){0,2}"
    r"(?:remote|telecommut\w*|work(?:ing)?[\s-]+(?:at|from)[\s-]+home)"
    r"|\bremote\w*(?:\s+work)?\s+(?:is\s+|are\s+)?"
    r"(?:not\s+(?:available|allowed|offered|possible|permitted|supported|an\s+option|eligible)|unavailable)\b", re.I)
#: The words that say a job is hybrid, or remote (a place's or a posting's text), which code reads a way of working by
#: (`posting_facts._MODE_WORDS` takes them from here).
HYBRID_WORDS = re.compile(r"\bhybrid\b", re.I)
REMOTE_WORDS = re.compile(r"\b(?:remote(?:ly)?|work(?:ing)?[\s-]+(?:from|at)[\s-]+home|telecommut\w*)\b", re.I)
#: A place that names hybrid or in person work as well as remote ("Remote or hybrid in Oakfield").
_OTHER_WAY = re.compile(r"hybrid|on[\s-]?site|in[\s-]?(?:office|person)", re.I)
#: A place that calls itself hybrid or on-site, and the days or weeks it names: it is saying which days are remote
#: ("Hybrid - Mon and Fri remote"), and a role that is remote some days is not remote.
_HYBRID = re.compile(r"hybrid|on[\s-]?site", re.I)
_DAYS = re.compile(
    r"\bdays?\b|\bweeks?\b|%|\bpartial|\boccasional"
    r"|\b(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day)?s?\b|\b(?:tuesday|wednesday|thursday|saturday)s?\b",
    re.I)
#: A remote that is open in every country, so open in the owner's.
_WORLDWIDE = re.compile(r"\b(?:global(?:ly)?|worldwide|world-wide|anywhere in the world|any country|all countries)\b",
                        re.I)


def _pieces(text: str) -> list[str]:
    """The pieces of a written place that are not notes about the posting."""
    text = re.sub(r"\bn\s*/\s*a\b", " ", text, flags=re.I)
    return [piece for piece in (x.strip() for x in _PIECES.split(text)) if piece and not _NOTE.search(piece)]


def _says_nothing(text: str) -> bool:
    """Whether a written place says nothing about where the work is once its notes are set aside: empty, a count or a
    placeholder, with or without a note after it ("Not stated (HOLD)", "Location not stated", "Multiple Locations
    (US)"). `says_no_place` is the same for a place that is only that."""
    return all(_SAYS_NOTHING.match(piece) for piece in _pieces(text))


#: The US states and the District, by name, and by two-letter code. A state alone names no town. "New York" and
#: "Washington" are left out: each is a city as much as a state.
_STATE_NAMES = frozenset((
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
    "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "west virginia", "wisconsin", "wyoming"))
_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NC ND OH OK OR PA RI "
    "SC SD TN TX UT VT VA WV WI WY DC".split())


def _is_state(rest: str) -> bool:
    """Whether what a piece has left, once the words that name no town are taken out, is a state's name or its code."""
    rest = " ".join(rest.split())
    return rest in _STATE_CODES or rest.lower() in _STATE_NAMES


def _names_a_town(text: str) -> bool:
    """Whether a written place names anything that could be a town: it does not when it is empty, a placeholder, a note,
    a country, a state or a way of working alone ("US, work mode not stated", "Hybrid", "United States (no city in
    JD)", "Indiana, United States"). A state's name beside a state's code ("Indiana, PA") is a town in that state."""
    rests = [" ".join(_NO_TOWN.sub(" ", piece).split()) for piece in _pieces(text)]
    rests = [r for r in rests if re.search(r"[^\W\d_]", r)]
    has_code = any(r in _STATE_CODES for r in rests)
    return any(not _is_state(r) or (has_code and r.lower() in _STATE_NAMES) for r in rests)


def _says_remote(text: str) -> bool:
    """Whether a written place says the work is remote: it does not when it denies it, and not when it is hybrid or
    on-site and gives the days (remote some days is hybrid)."""
    text = _REMOTE_DENIED.sub(" ", text)
    return bool(_REMOTE.search(text)) and not (_HYBRID.search(text) and _DAYS.search(text))


def names_a_town(location: str | None) -> bool:
    """Whether a written place names anything that could be a town, as the work location gate reads it: "United
    States", "Remote", "Hybrid" or "Not stated" do not, so a hybrid or in person role with only that is undecided."""
    return _names_a_town(location or "")


def says_remote(text: str) -> bool:
    """Whether a written place (lowercase) says the work is remote, as the work location gate reads it."""
    return _says_remote(text)


def _remote_ok(location: str | None, text: str, gates: Gates) -> bool | None:
    """The remote part of a role's place: closed when remote work is off, closed when the place is another country's
    (never when it says it is open worldwide), unknown with countries named and no place said."""
    if not gates.remote_ok:
        return False
    if gates.remote_countries:
        if _WORLDWIDE.search(text):
            return True
        if abroad(location, gates.remote_countries):
            return False
        if _says_nothing(text):  # nothing says which country: it cannot be told
            return None
    return True


def _in_person_ok(location: str | None, text: str, mode: str, gates: Gates) -> bool | None:
    """The hybrid and in person part of a role's place, judged on the towns the text names."""
    if not _names_a_town(text):  # no town: a placeholder, a note, a bare country or a way of working alone
        return None
    if mode == "hybrid":
        ways = [(gates.hybrid_ok, gates.hybrid_places)]
    elif mode == "onsite":
        ways = [(gates.onsite_ok, gates.onsite_places)]
    else:
        ways = [(gates.hybrid_ok, gates.hybrid_places), (gates.onsite_ok, gates.onsite_places)]
    ways = [(ok, names) for ok, names in ways if ok]
    if not ways:
        return False
    found = [place_match(location or "", name) for _, names in ways for name in names]
    if True in found or any(not names for _, names in ways):
        return True
    return None if None in found else False


def location_ok(
    location: str | None, work_mode: str | None, gates: Gates, *, unlisted_is_unknown: bool = True,
) -> bool | None:
    """True when the role may be worked the way and where it says, False when it may not, None when the posting
    doesn't say enough to tell (held, not guessed: the role is kept, with a note to check the place). With no work
    location gate every place passes. A location that says nothing (empty, a count such as "26 Locations", a
    placeholder, a bare country or a way of working, with or without a note: "Not stated (HOLD)", "US, work mode not
    stated") is unknown, never a pass and never a fail (`_names_a_town`), and so is a work mode that is none of
    `WORK_MODES` (a word an imported tracker holds, such as "local" or "lateral").

    Remote passes when remote work is on (and, with countries named, the place is not in another country, unless it
    says it is open worldwide; with countries named and no place stated, it is unknown). A place written as free text
    that says it is remote ("Remote or Atlanta, GA", "Hybrid/remote (US)", "United States Work at Home",
    "Telecommuter") and has no work mode is read that way even when it names hybrid, in person or a town too: the role
    can be worked remote, so the rest of the line is not held against it. When remote is closed to the owner (off, or
    in another country) and the place names hybrid or in person work too, the towns it names decide. Hybrid and in
    person pass when that way of working is on and a listed place is named; on with no places listed, anywhere. A
    place with no work mode, and no word in it for hybrid, in person or no remote, that is not listed is unknown, not a
    fail: nothing says the role is worked in person, and it is judged again when the work mode is known. With a work
    mode, or one of those words, a place that isn't listed fails, and a posting that lists several places passes when
    one of them is allowed. A listed town written with its state ("Oakfield, OH") is named by that town in that state
    only, and a posting that names the town and no state is unknown (`places.match`)."""
    if not gates.places_gated:
        return True
    mode = (work_mode or "unknown").strip().lower()
    if mode not in WORK_MODES:
        mode = "unknown"
    text = (location or "").strip().lower()
    if mode == "remote" or (mode == "unknown" and _says_remote(text)):
        remote = _remote_ok(location, text, gates)
        if remote is not False or mode == "remote" or not _OTHER_WAY.search(text):
            return remote
        # Remote is closed to the owner, but the place also names hybrid or in person work: the towns it names decide.
    in_person = _in_person_ok(location, text, mode, gates)
    says_how = _OTHER_WAY.search(text) or _REMOTE_DENIED.search(text)
    if unlisted_is_unknown and in_person is False and mode == "unknown" and not says_how:
        return None  # a place says nothing of how the work is done: not on site until something says so
    return in_person


#: The ways of working a posting could have when it states none (`fails_every_way`).
STATED_MODES = ("remote", "hybrid", "onsite")


def fails_every_way(location: str | None, work_mode: str | None, gates: Gates) -> bool:
    """Whether a place the work location gate cannot decide for want of the way of working fails it in every way the
    posting could have (the owner, 2026-10-23): with no way of working stated, a place that fails remote (the owner's
    countries), hybrid and in person (the owner's listed places) alike is a fail, since no missing fact can rescue it
    (a town abroad when remote is open only in the owner's country and in person only at their towns). Only a place the
    posting does name: one that says nothing ("26 Locations", "Not stated") stays unknown, and so does one any way of
    working could pass. Everything it compares is the owner's own settings (`gates`)."""
    mode = (work_mode or "unknown").strip().lower()
    if mode in (*STATED_MODES, "restricted_remote"):
        return False
    if not (location or "").strip() or says_no_place(location):
        return False
    return all(location_ok(location, way, gates) is False for way in STATED_MODES)


#: The way of working a posting that states none is judged as (the owner, 2026-11-05, docs/PRD.md 30.2 and 48.14 e), in
#: the words `location_ok` reads, and what a gate says of
#: it. Never stored on a posting or a role: it is what the work location gate assumes while nothing states a way.
ASSUMED_WAY = "onsite"
ASSUMED_WAY_WORDS = "on-site assumed: the posting states no remote or hybrid"


#: The words of a place that state a way of working the gate's own reading (`_REMOTE`, `_OTHER_WAY`) does not know: a
#: virtual office or address, "anywhere", "nationwide", home teleworkers, a flexible place. "Anywhere in Country",
#: "Virtual Office", "Home Teleworkers" and "Flexible" must not be judged as in person. They state a way, not one the
#: gate can judge, so the way stays unknown, as before.
_WAY_WORDS_OF_A_PLACE = re.compile(r"\b(?:virtual|anywhere|nationwide|telework\w*|flexible)\b", re.I)
#: The words of a posting's text that say a job is hybrid or remote: the words code reads a way of working by, and
#: the two more a board writes (teleworking, "WFH").
_WAY_WORDS_OF_A_TEXT = re.compile(
    rf"{HYBRID_WORDS.pattern}|{REMOTE_WORDS.pattern}|\btelework\w*|\bwfh\b", re.I)


def states_a_way(work_mode: str | None, location: str | None) -> bool:
    """Whether a way of working is stated, by the way of working itself (one of `STATED_MODES`, or restricted remote)
    or by the words of the place: it says it is remote, hybrid or on-site, or denies remote ("Remote or Atlanta, GA",
    "Hybrid - Oakfield", "not remote"), or uses a word of a way the gate does not read (`_WAY_WORDS_OF_A_PLACE`:
    "Virtual Office", "Anywhere in Country"). The work location gate reads the first words itself (`location_ok`), so a
    place that says one is never assumed on-site; a way that is empty, "unknown" or a word the gate does not know is
    none."""
    if (work_mode or "").strip().lower() in (*STATED_MODES, "restricted_remote"):
        return True
    text = (location or "").strip().lower()
    return bool(_says_remote(text) or _OTHER_WAY.search(text) or _REMOTE_DENIED.search(text)
                or _WAY_WORDS_OF_A_PLACE.search(text))


def text_states_a_way(text: str | None) -> bool:
    """Whether a posting's text has a word of hybrid or remote work in it anywhere ("#LI-REMOTE", "Work arrangement
    Remote", "Location-based hybrid policy", "follows a hybrid work model"), outside a remote that is denied ("not
    remote", "remote work is not available": `_REMOTE_DENIED`, as the gate reads a place). Code reads only some of these
    as the way of working of this job (`posting_facts.mode_from_text`); the rest still say the posting speaks of one, so
    a posting that has one is never taken to state none (the owner, 2026-11-05). On-site words say nothing of the kind:
    the assumption is already that."""
    return bool(_WAY_WORDS_OF_A_TEXT.search(_REMOTE_DENIED.sub(" ", text or "")))


def is_excluded(company: str | None, gates: Gates) -> bool:
    name = (company or "").strip().lower()
    return bool(name) and any(re.search(rf"\b{re.escape(e)}\b", name) for e in gates.excluded_employers)


def role_facts(role: dict, gates: Gates, today: date) -> dict:
    """What the screening process compares for one reported role. `role` is the scout's dict (company, title,
    location, work_mode, comp, comp_min, comp_max, posted, found, fit)."""
    low, high = role.get("comp_min"), role.get("comp_max")
    if hourly_part_time(role.get("title"), role.get("comp")):
        low = high = None  # a part-time hour is no full-time year, whatever figure an earlier reading stored
    elif low is None and high is None:
        low, high = parse_comp_text(role.get("comp"))
    low = int(low) if low else None
    high = int(high) if high else None
    posted = as_date(role.get("posted"))
    found = as_date(role.get("found"))
    target = role.get("target_role")
    own = gates.by_role.get(target) if target else None
    result, judged, judged_from = pay_check(low, high, gates)
    place_ok = location_ok(role.get("location"), role.get("work_mode"), gates)
    floor, stale_days = gates.comp_floor, gates.stale_days
    # What a gate says when it is the role's own that decides: the sentence the process drops a role with.
    says: dict[str, str] = {}
    if own is not None:
        # A role's own gates ADD to All's (docs/PRD.md 31.9): the role must pass both. Where All's already fails, All's
        # sentence stands; where only the role's fails, the role's does.
        own_place = location_ok(role.get("location"), role.get("work_mode"), own)
        if own_place is False and place_ok is not False:
            place_ok, says["location"] = False, own.says.get("location", "")
        elif place_ok is True and own_place is None:
            place_ok = None
        if own.comp_floor and result != "fail":
            own_result = pay_check(low, high, own)
            if own_result[0] == "fail" or result == "unknown":
                (result, judged, judged_from), floor = own_result, own.comp_floor
                if result == "fail":
                    says["compensation"] = own.says.get("compensation", "")
        if own.stale_days < stale_days:
            stale_days, says["age"] = own.stale_days, own.says.get("age", "")
    return {
        "key": match_key(role.get("company"), role.get("title")),
        # The score the reader's answers add up to. No gate reads it: it passes through to the stored role.
        "fit": int(role["fit"]) if role.get("fit") is not None else 0,
        "age_days": age_in_days(posted, found, today),
        # The posting age that makes a posting stale for this role: All's, or the role's own when that is lower.
        "stale_days": stale_days,
        "excluded": is_excluded(role.get("company"), gates),
        "location_ok": place_ok,
        "location": role.get("location") or "",
        "comp_max": high,
        "ask": provisional_ask(low, high, gates.comp_wide_band_ratio),
        # The figure the pay gate compared (empty when there is nothing to judge) and what it is, and the amount it was
        # compared with (All's, or the role's own when that gate is the one that judged).
        "comp_judged": judged, "comp_from": judged_from, "comp_floor": floor,
        # The sentences of the gates that are the role's own, where they are the ones that decide (empty otherwise: the
        # process uses All's `gates.says`).
        "says": says,
    }


def match_key(company: str | None, title: str | None) -> str:
    """The dedupe key: lowercased `company|title` (tracker_io.make_match_key), with every run of whitespace (a double
    space, a tab, a non-breaking space) folded to one space. The one place a key is made: a posting's, a reported
    role's and a stored role's must agree or a role is read, and dropped, twice. It is exact on purpose; whether
    two keys are one role written two ways is `same_role`'s to say."""
    return f"{' '.join((company or '').split()).lower()}|{' '.join((title or '').split()).lower()}"


def as_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
