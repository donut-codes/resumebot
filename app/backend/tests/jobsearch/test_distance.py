"""Miles between two places, from a small offline gazetteer of US places shipped as data (docs/PRD.md 46.8). Nothing is
fetched. Well-known cities only."""

import pytest

from app.jobsearch.measures import distance


def test_the_miles_between_two_known_places_are_the_great_circle_distance():
    miles = distance.between("Madison, WI", "Milwaukee, WI")
    assert miles is not None and 72 <= miles <= 76


@pytest.mark.parametrize("location", ["Hybrid - Milwaukee, WI", "Milwaukee, Wisconsin, United States",
                                      "Onsite: Milwaukee, WI 53202", "US-WI-Milwaukee, Milwaukee, WI"])
def test_a_place_is_found_however_the_posting_writes_it(location):
    assert distance.between("Madison, WI", location) is not None


def test_a_posting_with_several_places_is_measured_to_the_nearest():
    near = distance.between("Milwaukee, WI", "Denver, CO; Madison, WI")
    assert near is not None and near < 100


def test_a_town_in_another_state_is_another_place():
    franklin_wi = distance.between("Madison, WI", "Franklin, WI")
    franklin_ma = distance.between("Madison, WI", "Franklin, MA")
    assert franklin_wi is not None and franklin_ma is not None and franklin_ma > franklin_wi + 500


@pytest.mark.parametrize("location", ["Remote", "United States", "Nowhereville, ZZ", "", None])
def test_a_place_the_gazetteer_does_not_know_has_no_distance(location):
    assert distance.between("Madison, WI", location) is None


def test_no_known_home_has_no_distance():
    assert distance.between("Nowhereville, ZZ", "Madison, WI") is None
    assert distance.between(None, "Madison, WI") is None


def test_the_gazetteer_is_the_census_places_file_trimmed_and_a_few_of_its_rows_are_as_published():
    places = distance.gazetteer()
    assert len(places) > 20_000 and all(-90 < lat < 90 and -180 < lon < 180 for lat, lon in places.values())
    # rows of the Census Bureau's 2024 Gazetteer, to its published precision
    for key, (lat, lon) in {("milwaukee", "WI"): (43.0633, -87.9667), ("madison", "WI"): (43.0878, -89.4299),
                            ("denver", "CO"): (39.7619, -104.8811)}.items():
        assert places[key] == (lat, lon)
