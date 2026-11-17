"""Whether a place a posting names is in another country (docs/PRD.md 28.10): a remote role is only open to the
owner when it is remote in a country they can work from (Remote.countries in the profile). Names only, no lookup: a
place in no list is not called abroad, so the scout still judges it."""

import functools
import re

US_STATES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware", "Florida",
    "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma",
    "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
)
US_CODES = ("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
            "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
            "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC")
#: Ways a posting says the United States ("North America" takes the United States in; a bare "America" would take in
#: "Latin America" too).
US_WORDS = ("United States", "USA", "U.S.A.", "U.S.", "US", "North America", "Nationwide")
#: A state's code counts only as the tail of a "City, ST" place: "DE" in "Berlin, DE" is Germany, and an all-capitals
#: "WORK IN CANADA" holds no state.
_TAIL_CODE = re.compile(r",\s*(" + "|".join(US_CODES) + r")\b(?![A-Za-z])")
#: The codes that are a state's and a listed country's too: after a place that names that country they mean the country.
COUNTRY_CODE = {"Canada": "CA", "Germany": "DE", "India": "IN", "Israel": "IL", "Argentina": "AR", "Colombia": "CO"}

#: Countries, with the regions and cities postings most often name instead of the country.
ABROAD: dict[str, tuple[str, ...]] = {
    "Canada": ("Canada", "Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba", "Nova Scotia", "Toronto",
               "Vancouver", "Montreal", "Calgary", "Ottawa"),
    "Mexico": ("Mexico", "Ciudad de México", "Guadalajara", "Monterrey"),
    "United Kingdom": ("United Kingdom", "UK", "England", "Scotland", "Wales", "Northern Ireland", "London",
                       "Manchester", "Edinburgh", "Birmingham, UK"),
    "Ireland": ("Ireland", "Dublin"),
    "India": ("India", "Bengaluru", "Bangalore", "Hyderabad", "Pune", "Chennai", "Mumbai", "Gurugram", "Gurgaon",
              "Noida", "New Delhi", "Haryana", "Karnataka", "Maharashtra", "Telangana", "Tamil Nadu", "Kolkata"),
    "Philippines": ("Philippines", "Manila", "Cebu", "Taguig", "Makati"),
    "Australia": ("Australia", "Sydney", "Melbourne", "Brisbane", "Perth", "New South Wales", "Victoria, AU"),
    "New Zealand": ("New Zealand", "Auckland", "Wellington"),
    "Germany": ("Germany", "Deutschland", "Berlin", "Munich", "München", "Frankfurt", "Hamburg"),
    "France": ("France", "Paris"),
    "Spain": ("Spain", "Madrid", "Barcelona"),
    "Portugal": ("Portugal", "Lisbon"),
    "Netherlands": ("Netherlands", "Amsterdam"),
    "Belgium": ("Belgium", "Brussels"),
    "Switzerland": ("Switzerland", "Zurich", "Zürich", "Geneva"),
    "Italy": ("Italy", "Milan", "Rome"),
    "Poland": ("Poland", "Warsaw", "Krakow", "Kraków"),
    "Romania": ("Romania", "Bucharest"),
    "Sweden": ("Sweden", "Stockholm"),
    "Denmark": ("Denmark", "Copenhagen"),
    "Norway": ("Norway", "Oslo"),
    "Finland": ("Finland", "Helsinki"),
    "Israel": ("Israel", "Tel Aviv"),
    "United Arab Emirates": ("United Arab Emirates", "UAE", "Dubai", "Abu Dhabi"),
    "Singapore": ("Singapore",),
    "Japan": ("Japan", "Tokyo"),
    "China": ("China", "Shanghai", "Beijing", "Shenzhen"),
    "Hong Kong": ("Hong Kong",),
    "South Korea": ("South Korea", "Seoul"),
    "Brazil": ("Brazil", "São Paulo", "Sao Paulo"),
    "Argentina": ("Argentina", "Buenos Aires"),
    "Colombia": ("Colombia", "Bogotá", "Bogota", "Medellín"),
    "Costa Rica": ("Costa Rica",),
    "South Africa": ("South Africa", "Cape Town", "Johannesburg"),
    "EMEA": ("EMEA", "APAC", "LATAM"),
}


@functools.lru_cache(maxsize=2048)
def _compiled(words: tuple[str, ...]) -> tuple[re.Pattern | None, re.Pattern | None]:
    """The two patterns for a group of words: one in any case for the words over 3 characters, one in the exact case
    for the shorter (codes), each a whole-word alternation. None for a half with no words."""
    def build(group: list[str], flags: int) -> re.Pattern | None:
        if not group:
            return None
        return re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(w) for w in group) + r")(?![A-Za-z])", flags)
    return (build([w for w in words if len(w) > 3], re.IGNORECASE), build([w for w in words if len(w) <= 3], 0))


def _has(text: str, words) -> bool:
    words = tuple(words)
    if not words:
        return False
    return any(rx.search(text) for rx in _compiled(words) if rx is not None)


def says_us(location: str) -> bool:
    """Whether a place says the United States in words or by a state's name (no code alone)."""
    return _has(location, US_WORDS) or _has(location, US_STATES)


def abroad(location: str | None, allowed: list[str]) -> str | None:
    """The other country a place names, or None: when it names none, names an allowed one, or (with the United States
    allowed) names a US state or says the United States anywhere, as in "remote in Canada or the United States"."""
    text = (location or "").strip()
    if not text or not allowed:
        return None
    allowed_l = {a.strip().lower() for a in allowed}
    us_allowed = bool(allowed_l & {"united states", "usa", "us", "u.s."})
    # A place that names another country is abroad: "Berlin, DE" is Germany, not Delaware. It is not when it also says
    # the United States in words ("Canada or the United States"), or ends in a state's code that is not that country's
    # own ("Manchester, NH").
    for country, words in ABROAD.items():
        if country.lower() in allowed_l:
            continue
        if _has(text, words):
            if any(_has(text, ABROAD.get(a, (a,))) for a in allowed if a.lower() != country.lower()):
                return None
            if us_allowed and (says_us(text) or _other_state_code(text, country)):
                return None
            return country
    return None


def _other_state_code(text: str, country: str) -> bool:
    """Whether the place ends in a state's code that is not `country`'s own code."""
    return any(code != COUNTRY_CODE.get(country) for code in _TAIL_CODE.findall(text))
