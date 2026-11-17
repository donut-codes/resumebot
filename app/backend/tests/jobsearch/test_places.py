"""Whether a posting's location line names a place the owner listed, a town and its state together (places.py). Found
when a town the owner listed alone matched the same name in every state, and a state written beside a listed town was
not read. Synthetic towns only (no database)."""

import pytest

from app.jobsearch.measures import places
from app.jobsearch.measures.countries import US_CODES, US_STATES


def test_every_state_name_has_its_own_code():
    assert len(US_STATES) == len(US_CODES) == len(places.STATE_CODE) == 51
    named = ["Ohio", "New Jersey", "Washington", "West Virginia", "Virginia", "District of Columbia"]
    assert [places.STATE_CODE[name.lower()] for name in named] == ["OH", "NJ", "WA", "WV", "VA", "DC"]
    assert set(places.STATE_CODE.values()) == set(US_CODES)


@pytest.mark.parametrize(("listed", "town", "state"), [
    ("oakfield, oh", "oakfield", "OH"),
    ("Oakfield, Ohio", "oakfield", "OH"),
    ("  Maple   Heights , OH ", "maple heights", "OH"),
    ("new york, ny", "new york", "NY"),
    ("washington, dc", "washington", "DC"),
    ("oakfield", "oakfield", None),  # a town listed alone: any state
    ("springdale area", "springdale area", None),  # an area
    ("oakfield, st", "oakfield, st", None),  # "st" is no state: the whole is one name
    ("oakfield, north area", "oakfield, north area", None),
    (", oh", ", oh", None),  # no town in front of the state
])
def test_a_listed_place_is_a_town_and_maybe_its_state(listed, town, state):
    assert places.split_place(listed) == (town, state)


@pytest.mark.parametrize(("location", "expected"), [
    # The town and its state, whichever way the posting writes the state.
    ("Oakfield, OH", True), ("OAKFIELD, OH", True), ("Oakfield, OH 44001", True), ("Oakfield OH 44001", True),
    ("Oakfield, Ohio", True), ("Oakfield, Ohio, United States", True), ("Oakfield - Ohio", True),
    ("OH - Oakfield", True), ("US-OH-Oakfield", True), ("USA - OH - Oakfield - 1 Main St", True),
    ("Ohio - Oakfield", True), ("Remote - Oakfield, OH", True), ("Oakfield (OH)", True),
    # The same town in another state is another place.
    ("Oakfield, NJ", False), ("Oakfield, TN", False), ("Oakfield, New Jersey", False), ("US-NJ-Oakfield", False),
    ("NJ - Oakfield", False), ("Oakfield, NJ 08000", False),
    # No state beside the town: it cannot be told, so it is unknown and not a pass.
    ("Oakfield", None), ("Oakfield Campus", None), ("Oakfield, United States", None), ("Oakfield, UK", None),
    ("Remote - Oakfield", None), ("oakfield, oh", None),  # a code in small letters is a word, not read
    # Several places: one of them is enough; one with no state is unknown.
    ("Oakfield, NJ; Oakfield, OH", True), ("Oakfield, NJ / Oakfield, TN", False), ("Oakfield, NJ; Oakfield", None),
    # Another town, or none.
    ("Riverton, OH", False), ("", False), ("Remote", False),
])
def test_a_listed_town_with_its_state_is_named_by_that_town_in_that_state_only(location, expected):
    assert places.match(location, "oakfield, oh") is expected, location


@pytest.mark.parametrize("location", [
    "Oakfield, NJ", "Oakfield, OH", "Oakfield", "Oakfield Campus", "US-NJ-Oakfield", "Oakfield, ST", "oakfield",
    "Remote, or Oakfield",
])
def test_a_town_listed_alone_is_named_by_its_name_in_any_state(location):
    assert places.match(location, "oakfield") is True
    assert places.match("Riverton, OH", "oakfield") is False and places.match("", "oakfield") is False


def test_an_area_or_a_town_with_odd_marks_is_matched_as_a_name():
    assert places.match("Greater Springdale Area, OH", "springdale area") is True
    assert places.match("St. Oakfield, OH", "st. oakfield, oh") is True
    assert places.match("Oakfield (north), OH", "oakfield (north), oh") is True
