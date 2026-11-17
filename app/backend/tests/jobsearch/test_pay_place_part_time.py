"""A fractional, part-time or per-engagement role's hourly pay is not a full-time year (docs/PRD.md 30.4)."""
from datetime import date

import pytest

from app.jobsearch.measures import pay_place as g

HOURLY = "$190-$630 an hour"


@pytest.mark.parametrize("words", [
    "Fractional Chief AI Officer", "Part-time bookkeeper", "Part time analyst", "Paid per engagement",
    "Work 10 hours per week", "15-20 hours a week", "20 hrs/week", "Contract, up to 25 hours per week",
])
def test_the_words_of_a_part_time_role_are_found(words):
    assert g.is_part_time(words) is True


@pytest.mark.parametrize("words", [
    "Chief AI Officer", "Full-time analyst", "40 hours per week", "30 hours a week", "Fractionally owned shares",
    "Partitioned time", "20-40 hours a week", None, "",
])
def test_a_full_time_role_or_a_word_that_only_contains_one_is_not(words):
    assert g.is_part_time(words) is False


def test_hourly_pay_is_what_makes_a_part_time_role_ask_no_yearly_figure():
    assert g.hourly_part_time("Fractional CFO", HOURLY) is True
    assert g.hourly_part_time("Chief AI Officer", HOURLY, "A part-time engagement of 10 hours per week.") is True
    assert g.hourly_part_time("Fractional CFO", "$200,000 a year") is False
    assert g.hourly_part_time("Chief AI Officer", HOURLY) is False


def test_the_hourly_pay_of_a_part_time_role_is_no_yearly_range():
    assert g.parse_comp_text(HOURLY, part_time=True) == (None, None)
    assert g.parse_comp_text(HOURLY) == (395_200, 1_310_400)


def test_a_part_time_role_paid_by_the_year_keeps_its_range():
    assert g.parse_comp_text("$60,000 - $80,000 a year", part_time=True) == (60_000, 80_000)


def test_the_screening_facts_of_a_part_time_role_have_no_yearly_pay_or_ask():
    role = {"company": "Co", "title": "Fractional Chief AI Officer", "comp": "$60-$80 an hour", "fit": 1}
    facts = g.role_facts(role, g.Gates(), date(2026, 11, 12))
    assert facts["ask"] is None and facts["comp_max"] is None
    full = g.role_facts({**role, "title": "Chief AI Officer"}, g.Gates(), date(2026, 11, 12))
    assert full["ask"] and full["comp_max"] == 166_400


# --- the callers: no yearly band or ask anywhere for a part-time hourly role ------------------------------------------

from app.jobsearch.adapters import _common  # noqa: E402
from app.jobsearch.gating import check as gating  # noqa: E402
from app.jobsearch.gating import gate_facts  # noqa: E402
from app.jobsearch.pipeline import row as role_row  # noqa: E402


def _subject(title, comp, comp_min=None, comp_max=None):
    return gating.Subject(company="Co", location=None, work_mode=None, comp=comp, comp_min=comp_min, comp_max=comp_max,
                          posted=None, found=None, title=title)


def test_the_pay_gate_reads_no_yearly_band_for_a_part_time_hourly_role_even_from_stored_figures():
    assert gating.posted_band(_subject("Fractional CFO", HOURLY)) == (None, None)
    assert gating.posted_band(_subject("Fractional CFO", HOURLY, 395_200, 1_310_400)) == (None, None)


def test_the_pay_gate_still_reads_a_full_time_hourly_role_and_a_part_time_yearly_one():
    assert gating.posted_band(_subject("Chief AI Officer", "$60-$80 an hour")) == (124_800, 166_400)
    assert gating.posted_band(_subject("Fractional CFO", "$100,000 a year", 100_000, 100_000)) == (100_000, 100_000)


def test_a_haiku_pay_quote_of_a_part_time_hourly_role_states_no_yearly_band():
    text = "Fractional CFO. Pay: $190-$630 an hour."
    raw = {"facts": {"pay": {"value": "$190-$630 an hour", "quote": "$190-$630 an hour"}}}
    problems: list[str] = []
    found = gate_facts.read_answer(text, raw, [gate_facts.ASK_PAY], problems, "Fractional CFO")
    assert found.pay is None and found.bands == [] and problems
    full: list[str] = []
    assert gate_facts.read_answer(text, raw, [gate_facts.ASK_PAY], full, "CFO").pay is not None


def test_the_card_shows_the_hourly_range_as_written_for_a_part_time_role():
    assert role_row._pay_words(HOURLY, "Fractional CFO") == HOURLY
    assert role_row._pay_words("$60-$80 an hour", "Chief AI Officer") == "$125K–$166K"
    assert role_row._pay_words(HOURLY) == "$395K–$1310K"


def test_an_hourly_interval_is_not_made_a_year_for_a_part_time_posting():
    assert _common._comp(190, 630, "USD", "hour", part_time=True) == ("$190 - $630 per hour", None, None)
    assert _common._comp(60, 80, "USD", "hour") == ("$60 - $80 per hour", 124_800, 166_400)
    assert _common._comp(60_000, 80_000, "USD", "year", part_time=True)[1:] == (60_000, 80_000)


def test_a_json_ld_posting_that_says_part_time_keeps_its_hourly_pay_hourly():
    ld = {"title": "Bookkeeper", "employmentType": "PART_TIME", "baseSalary": {
        "currency": "USD", "value": {"minValue": 30, "maxValue": 40, "unitText": "HOUR"}}}
    got = _common._from_jsonld(ld)
    assert got["comp"].lower() == "$30 - $40 per hour" and "comp_min" not in got
    ld = {"title": "Bookkeeper", "employmentType": "FULL_TIME", "baseSalary": ld["baseSalary"]}
    assert _common._from_jsonld(ld)["comp_min"] == 62_400
