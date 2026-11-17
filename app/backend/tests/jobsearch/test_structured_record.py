"""The structured record (the structured data plan spec step 1): one shape for a posting and for the owner's master,
each filled field with its source mark and the quote it rests on; an empty field is simply absent. Synthetic data."""

import pytest

from app.jobsearch.postings import record as sr

TEXT = "Programs Lead\nLocation: Oakfield, ST\nWork type: Hybrid\nWe need 5+ years of experience."


def _field(**kw):
    return {"value": "hybrid", "source": "code", "quote": "Work type: Hybrid", **kw}


def test_an_empty_record_is_valid_and_round_trips_as_an_empty_object():
    record = sr.Record.from_dict({})
    assert record.fields == {} and record.partial is False
    assert sr.Record.from_dict(record.as_dict()) == record


def test_a_filled_field_round_trips_with_its_source_mark_and_quote():
    record = sr.Record.from_dict({"partial": True, "fields": {"work_mode": _field(also=["x"])}})
    assert record.partial is True
    got = record.fields["work_mode"]
    assert (got.value, got.source, got.quote, got.also) == ("hybrid", "code", "Work type: Hybrid", ("x",))
    assert sr.Record.from_dict(record.as_dict()) == record


@pytest.mark.parametrize("bad", [
    {"value": "hybrid", "quote": "Work type: Hybrid"},                       # no source
    {"value": "hybrid", "source": "guess", "quote": "Work type: Hybrid"},     # a source that is not a mark
    {"value": "hybrid", "source": "code"},                                    # no quote
    {"value": "hybrid", "source": "code", "quote": "   "},
])
def test_a_field_with_no_source_or_no_quote_is_refused(bad):
    with pytest.raises(sr.RecordError):
        sr.Record.from_dict({"fields": {"work_mode": bad}})


def test_a_field_name_the_record_does_not_have_is_refused():
    with pytest.raises(sr.RecordError):
        sr.Record.from_dict({"fields": {"favourite_colour": _field()}})


def test_an_empty_value_is_not_a_filled_field():
    with pytest.raises(sr.RecordError):
        sr.Record.from_dict({"fields": {"work_mode": _field(value="")}})


def test_a_quote_that_is_not_in_the_text_is_no_fact_but_a_listing_field_needs_no_text_line():
    record = sr.Record.from_dict({"fields": {
        "work_mode": _field(), "title": {"value": "Programs Lead", "source": "listing", "quote": "Programs Lead"}}})
    assert sr.check_quotes(record, TEXT) == []
    lie = sr.Record.from_dict({"fields": {"work_mode": _field(quote="Work type: Remote")}})
    assert sr.check_quotes(lie, TEXT) == ["work_mode"]
    listing = sr.Record.from_dict({"fields": {
        "title": {"value": "Other Title", "source": "listing", "quote": "Other Title"}}})
    assert sr.check_quotes(listing, TEXT) == []
