"""Posting instants are read in the owner's zone: an epoch or a `Z` time is cut to
the owner's day, a date-only value stays as written, and `validThrough` compares instants."""

from datetime import UTC, datetime

import pytest

from app import timeutil
from app.jobsearch.adapters import _common
from app.jobsearch.browser import go_checks
from app.jobsearch.postings import dates

# 2026-10-07 20:00 UTC: already the 8th in Auckland-like zones, still the 7th in Honolulu-like ones.
EPOCH = int(datetime(2026, 10, 7, 20, 0, tzinfo=UTC).timestamp())


@pytest.fixture
def east(monkeypatch):
    monkeypatch.setattr(timeutil.settings, "timezone", "Pacific/Auckland")


@pytest.fixture
def west(monkeypatch):
    monkeypatch.setattr(timeutil.settings, "timezone", "Pacific/Honolulu")


@pytest.mark.parametrize("fn", [_common._date])
def test_epoch_milliseconds_are_the_owners_day_east_of_utc(east, fn):
    assert fn(EPOCH * 1000) == "2026-10-08"


@pytest.mark.parametrize("fn", [_common._date])
def test_epoch_seconds_are_the_owners_day_east_of_utc(east, fn):
    assert fn(EPOCH) == "2026-10-08"


@pytest.mark.parametrize("fn", [_common._date])
def test_z_time_is_the_owners_day_west_of_utc(west, fn):
    assert fn("2026-10-08T03:00:00Z") == "2026-10-07"


@pytest.mark.parametrize("fn", [_common._date])
def test_offset_time_is_converted(east, fn):
    assert fn("2026-10-07T23:30:00-05:00") == "2026-10-08"


@pytest.mark.parametrize("fn", [_common._date])
def test_date_only_stays_as_written(east, fn):
    assert fn("2026-10-07") == "2026-10-07"


def test_time_without_a_zone_stays_as_written(east):
    assert _common._date("2026-10-07T23:30:00") == "2026-10-07"


def test_display_text_is_no_date():
    assert _common._date("Posted Today") is None


def test_recipe_date_cell_reads_an_epoch_printed_as_text(east):
    assert _common._loose_date(str(EPOCH * 1000)) == "2026-10-08"


def test_recipe_date_cell_keeps_slash_dates_month_first(east):
    assert _common._loose_date("10/7/2026") == "2026-10-07"


def test_structured_aware_datetime_is_the_owners_day(east):
    assert dates.structured(datetime(2026, 10, 7, 20, 0, tzinfo=UTC)).day.isoformat() == "2026-10-08"


def test_structured_z_string_is_the_owners_day(east):
    assert dates.structured("2026-10-07T20:00:00Z").day.isoformat() == "2026-10-08"


def test_structured_date_string_stays(east):
    assert dates.structured("2026-10-07").day.isoformat() == "2026-10-07"


def _at(y, m, d, h):
    return datetime(y, m, d, h, 0, tzinfo=UTC)


def test_date_only_valid_through_is_not_past_on_its_last_day_anywhere(east):
    assert go_checks._past("2026-10-07", now=_at(2026, 10, 8, 11)) is None


def test_date_only_valid_through_is_past_once_the_day_ended_in_the_last_zone(east):
    assert go_checks._past("2026-10-07", now=_at(2026, 10, 8, 12)).isoformat() == "2026-10-07"


def test_z_valid_through_compares_the_instant_west_of_utc(west):
    # 03:00Z on the 8th is the evening of the 7th for the owner: past at 04:00Z, named as the owner's day.
    got = go_checks._past("2026-10-08T03:00:00Z", now=_at(2026, 10, 8, 4))
    assert got.isoformat() == "2026-10-07"
    assert go_checks._past("2026-10-08T03:00:00Z", now=_at(2026, 10, 8, 2)) is None


def test_valid_through_epoch_compares_the_instant(east):
    assert go_checks._past(EPOCH * 1000, now=datetime.fromtimestamp(EPOCH + 60, UTC)) is not None
    assert go_checks._past(EPOCH * 1000, now=datetime.fromtimestamp(EPOCH - 60, UTC)) is None
