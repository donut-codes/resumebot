"""How a value of the old engine is written the way the app writes it (`app.migrate.convert`): no database here."""

import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from process_engine.types import UtcDateTime
from sqlalchemy.dialects import sqlite

from app.jobsearch.models import JobPosting, Opportunity, RawPosting
from app.migrate import convert, plan
from app.settings import settings

EASTERN = timezone(timedelta(hours=-5))


def test_a_uuid_of_digits_is_written_as_32_hex_digits_text():
    """A naive type would read `12345678...` back as a number; the stored text is the hex of the uuid."""
    value = uuid.UUID("12345678-1234-5678-1234-567812345678")
    written = convert.stored(Opportunity.__table__.c.id)(value)
    assert written == "12345678123456781234567812345678"
    assert isinstance(written, str)


def test_a_uuid_the_driver_hands_back_as_text_is_written_the_same():
    value = uuid.UUID("0b2f6d7a-3c1e-4f55-9a70-1d2c3b4a5f60")
    column = Opportunity.__table__.c.id
    assert convert.stored(column)(str(value)) == convert.stored(column)(value) == value.hex


def test_an_aware_time_is_written_as_utc_text_to_the_microsecond():
    when = datetime(2026, 3, 8, 22, 30, 15, 120, tzinfo=EASTERN)
    assert convert.stored(Opportunity.__table__.c.created_at)(when) == "2026-03-09 03:30:15.000120"


def test_a_naive_time_is_taken_as_utc_as_the_app_takes_it():
    put = convert.stored(Opportunity.__table__.c.created_at)
    assert put(datetime(2026, 3, 9, 3, 30)) == "2026-03-09 03:30:00.000000"


def test_the_written_time_is_the_text_the_app_reads_back_as_the_same_moment():
    """The copy and the app's own write agree: the column's type reads what the copy stored as the aware moment."""
    column = Opportunity.__table__.c.created_at
    when = datetime(2026, 3, 8, 22, 30, 15, 120, tzinfo=EASTERN)
    read = column.type.dialect_impl(sqlite.dialect()).result_processor(sqlite.dialect(), None)
    assert read is not None
    assert read(convert.stored(column)(when)) == when.astimezone(UTC)


def test_a_json_null_stays_the_text_null_and_a_missing_value_stays_missing():
    put = convert.stored(Opportunity.__table__.c.fit_breakdown)
    assert put("null") == "null"
    assert put(None) is None
    assert put('{"b": 1, "a": [1, 2]}') == '{"b": 1, "a": [1, 2]}'


def test_a_json_value_the_driver_already_decoded_is_written_as_its_json_text():
    assert convert.stored(Opportunity.__table__.c.fit_breakdown)({"a": [1, None]}) == '{"a": [1, null]}'


def test_a_blob_is_written_as_bytes_whatever_view_the_driver_gave():
    put = convert.stored(RawPosting.__table__.c.body)
    assert put(b"\x00\x01zip") == b"\x00\x01zip"
    assert put(memoryview(b"\x00\x01zip")) == b"\x00\x01zip"
    assert put(None) is None


def test_a_date_is_written_as_its_iso_text_and_a_boolean_as_a_number():
    assert convert.stored(Opportunity.__table__.c.posted)(date(2026, 3, 8)) == "2026-03-08"
    assert convert.stored(Opportunity.__table__.c.packet_complete)(True) == 1
    assert convert.stored(Opportunity.__table__.c.packet_complete)(False) == 0


def test_text_and_numbers_go_in_as_they_are():
    assert convert.stored(Opportunity.__table__.c.title)("A title with a é") == "A title with a é"
    assert convert.stored(Opportunity.__table__.c.fit)(80) == 80


@pytest.mark.parametrize("when, day", [
    (datetime(2026, 3, 9, 3, 30, tzinfo=UTC), date(2026, 3, 8)),  # 11:30 pm Eastern, the evening before
    (datetime(2026, 3, 9, 4, 30, tzinfo=UTC), date(2026, 3, 9)),  # 12:30 am Eastern: daylight time began on the 8th
    (datetime(2026, 11, 2, 4, 30, tzinfo=UTC), date(2026, 11, 1)),  # 11:30 pm standard time, after the clocks went back
    (datetime(2026, 11, 2, 5, 30, tzinfo=UTC), date(2026, 11, 2)),
])
def test_a_day_is_the_owners_day_of_the_time(monkeypatch, when, day):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    assert convert.day_of(when) == day


def test_a_day_of_no_time_is_the_day_it_is_copied(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    assert convert.day_of(None) == datetime.now(UTC).date()


def test_the_port_added_columns_the_copy_fills_exist_and_are_where_it_says():
    """The copy's table of what is new names real columns: a day is worked out from a time column of the same table."""
    for table, days in plan.ADDED_DAYS.items():
        model = {"js_opportunities": Opportunity, "js_postings": JobPosting}[table]
        columns = {c.name: c for c in model.__table__.columns}
        for new, old in days.items():
            assert new in columns and old in columns
            assert convert.kind(columns[new]) == convert.DATE and convert.kind(columns[old]) == convert.DATETIME
    assert "current_cleansed_id" in JobPosting.__table__.c


def test_every_column_of_every_table_has_a_kind_and_a_writer_that_takes_a_missing_value():
    kinds = {convert.UUID, convert.DATETIME, convert.DATE, convert.BOOL, convert.BLOB, convert.JSON, convert.PLAIN}
    for table in plan.target_tables():
        for column in table.columns:
            assert convert.kind(column) in kinds
            assert convert.stored(column)(None) is None, f"{table.name}.{column.name}"
            if isinstance(column.type, UtcDateTime):
                assert convert.kind(column) == convert.DATETIME, f"{table.name}.{column.name}"
