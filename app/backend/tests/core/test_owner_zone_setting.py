"""JOBSEARCH_TIMEZONE: blank is unset, a bad name stops the start with a plain message, unset warns once."""

import logging

import pytest

from app.settings import JobSearchSettings


def make(monkeypatch, value):
    monkeypatch.delenv("JOBSEARCH_TIMEZONE", raising=False)
    if value is not None:
        monkeypatch.setenv("JOBSEARCH_TIMEZONE", value)
    return JobSearchSettings(_env_file=None)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_zone_is_unset_not_a_crash(monkeypatch, caplog, blank):
    s = make(monkeypatch, blank)
    assert s.timezone == "UTC" and not s.timezone_is_set
    with caplog.at_level(logging.WARNING):
        s.check_timezone()
    assert "JOBSEARCH_TIMEZONE is not set" in caplog.text


def test_an_unset_zone_is_utc_with_one_warning(monkeypatch, caplog):
    s = make(monkeypatch, None)
    with caplog.at_level(logging.WARNING):
        s.check_timezone()
    assert s.timezone == "UTC"
    assert [r.getMessage() for r in caplog.records].count(caplog.records[0].getMessage()) == 1
    assert "in UTC until it is set" in caplog.text


def test_an_unknown_zone_fails_at_the_check_naming_the_setting(monkeypatch):
    s = make(monkeypatch, "Mars/Olympus")
    with pytest.raises(RuntimeError, match="JOBSEARCH_TIMEZONE is 'Mars/Olympus'"):
        s.check_timezone()


def test_a_good_zone_passes_without_a_warning(monkeypatch, caplog):
    s = make(monkeypatch, " Europe/Paris ")
    with caplog.at_level(logging.WARNING):
        s.check_timezone()
    assert s.timezone == "Europe/Paris" and s.timezone_is_set
    assert caplog.text == ""
