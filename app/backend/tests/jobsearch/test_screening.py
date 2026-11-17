"""The hard gates, through the process that holds them: `playbooks/jobsearch/screen_one_role.bpmn` run by the
engine, with the facts `gates.role_facts` works out. Every gate has its edge cases: the exact threshold, one past it,
a missing value. Synthetic data only."""

from datetime import timedelta

import pytest

from app.jobsearch.measures import pay_place as g
from tests.builders import TODAY, screen


def gates_with(**over) -> g.Gates:
    base = {
        "excluded_employers": ["globex"], "hybrid_places": ["oakfield"], "onsite_places": ["oakfield"],
        "comp_floor": 100_000, "comp_against": "ask", "stale_days": 30, "places_gated": True,
    }
    return g.Gates(**{**base, **over})


def posted(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


# --- a role that passes everything -------------------------------------------------------------------------------


async def test_a_role_that_passes_every_gate_is_kept_with_its_fit(js):
    verdict, ways = await screen(gates_with(), fit=5, comp="$120,000 - $140,000", posted=posted(3))
    assert verdict["keep"] is True and verdict["fit"] == 5 and verdict["reason"] == "" and verdict["note"] == ""
    assert ways == ["F_not_excluded", "F_place_ok", "F_comp_ok", "F_fresh"]


# --- excluded employer -------------------------------------------------------------------------------------------


async def test_an_excluded_employer_is_dropped_first_whatever_else_is_true(js):
    verdict, ways = await screen(
        gates_with(), company="Globex Corporation", fit=9, location="Gotham", work_mode="onsite")
    assert (verdict["keep"], verdict["reason"]) == (False, "excluded employer")
    assert ways == ["F_excluded"], "the cheapest gate decides: nothing after it was asked"


async def test_a_company_that_only_contains_the_excluded_letters_is_kept(js):
    verdict, _ = await screen(gates_with(), company="Globexa Tech")
    assert verdict["keep"] is True


# --- location ----------------------------------------------------------------------------------------------------


async def test_location_drops_a_far_on_site_role_and_a_place_that_is_not_listed(js):
    far, ways = await screen(gates_with(), location="Denver, CO", work_mode="onsite")
    assert far["keep"] is False and far["reason"].startswith("location")
    assert ways[:2] == ["F_not_excluded", "F_place_bad"]
    unlisted, _ = await screen(gates_with(), location="Gotham, NY", work_mode="hybrid")
    assert unlisted["keep"] is False and unlisted["reason"].startswith("location")


async def test_location_keeps_remote_and_local_even_in_an_unlisted_city_when_remote(js):
    assert (await screen(gates_with(), location="Gotham, NY", work_mode="remote"))[0]["keep"] is True
    assert (await screen(gates_with(), location="Oakfield, ST", work_mode="onsite"))[0]["keep"] is True


async def test_a_posting_that_names_no_place_is_held_not_dropped(js):
    verdict, ways = await screen(gates_with(), location="", work_mode="unknown")
    assert verdict["keep"] is True
    assert "place not stated" in verdict["note"]
    assert "F_place_ok" in ways


# --- comp floor --------------------------------------------------------------------------------------------------


async def test_comp_floor_edge_the_ask_exactly_at_the_floor_is_kept_a_middle_a_thousand_under_is_dropped(js):
    at_floor, _ = await screen(gates_with(), comp_min=87_500, comp_max=112_500)  # ask 100,000
    assert at_floor["keep"] is True
    near, _ = await screen(gates_with(), comp_min=87_498, comp_max=112_500)  # middle 99,999: no ask is certain
    assert near["keep"] is True, "within $1,000 under the floor the ask may still reach it: left for the analysis"
    under, ways = await screen(gates_with(), comp_min=87_000, comp_max=111_000)  # middle 99,000: no ask is above it
    assert under["keep"] is False and under["reason"].startswith("comp floor")
    assert "99,000" in under["reason"] and "estimated ask" in under["reason"]
    assert "F_comp_low" in ways


async def test_comp_floor_a_posted_top_under_the_floor_fails_outright_even_when_the_band_is_wide(js):
    verdict, _ = await screen(gates_with(), comp_min=40_000, comp_max=99_000)  # wide band, top under the floor
    assert verdict["keep"] is False and "posted maximum" in verdict["reason"]


async def test_comp_floor_a_band_too_wide_to_judge_and_pay_not_posted_are_kept(js):
    wide, _ = await screen(gates_with(), comp_min=60_000, comp_max=200_000)  # over 1.5x: not judged at scan
    assert wide["keep"] is True
    unposted, _ = await screen(gates_with())
    assert unposted["keep"] is True


async def test_comp_floor_reads_a_pay_line_when_the_scout_gave_no_figures(js):
    verdict, _ = await screen(gates_with(), comp="$70,000 - $90,000")
    assert verdict["keep"] is False and verdict["reason"].startswith("comp floor")


async def test_the_pay_is_judged_on_the_figure_the_owner_chose(js):
    band = {"comp_min": 80_000, "comp_max": 120_000}  # the middle is 100,000
    assert (await screen(gates_with(comp_against="minimum"), **band))[0]["keep"] is False
    assert (await screen(gates_with(comp_against="maximum"), **band))[0]["keep"] is True
    assert (await screen(gates_with(comp_against="maximum", comp_floor=130_000), **band))[0]["keep"] is False


async def test_a_role_a_gate_stops_is_given_the_gates_own_sentence_and_what_it_was_judged_on(js):
    says = {"location": "Work must be remote, or in person in Oakfield.",
            "compensation": "Pay must be at least $100,000.",
            "age": "Postings older than 30 days are gated out."}
    gates = gates_with(says=says)
    place, _ = await screen(gates, location="Denver, CO", work_mode="onsite")
    assert place["reason"] == "Work must be remote, or in person in Oakfield (posted: Denver, CO)."
    pay, _ = await screen(gates, comp_min=60_000, comp_max=80_000)
    assert pay["reason"] == "Pay must be at least $100,000 (the estimated ask is $70,000)."
    old, _ = await screen(gates, posted=posted(40))
    assert old["reason"] == "Postings older than 30 days are gated out (this one is 40 days old)."


# --- staleness ---------------------------------------------------------------------------------------------------


async def test_staleness_edge_thirty_days_is_kept_thirty_one_is_gated_out(js):
    on_the_line, ways = await screen(gates_with(), fit=5, posted=posted(30))
    assert on_the_line["keep"] is True and on_the_line["fit"] == 5 and on_the_line["note"] == "" and "F_fresh" in ways
    verdict, ways = await screen(gates_with(), fit=5, posted=posted(31))
    assert verdict["keep"] is False and verdict["reason"] == "stale: posted 31 days ago, over 30"
    assert "F_stale_reject" in ways


async def test_with_no_posting_age_gate_no_posting_is_stale(js):
    verdict, ways = await screen(gates_with(stale_days=g.Gates().stale_days), fit=5, posted=posted(400))
    assert verdict["keep"] is True and verdict["fit"] == 5 and "F_fresh" in ways


async def test_an_unknown_posting_date_is_never_stale(js):
    verdict, ways = await screen(gates_with(), fit=5)
    assert verdict["fit"] == 5 and "F_fresh" in ways


async def test_the_age_comes_from_the_day_found_when_the_posting_has_no_date(js):
    gates = gates_with()
    raw = {"company": "N", "title": "D", "fit": 5, "work_mode": "remote",
           "found": (TODAY - timedelta(days=40)).isoformat()}
    assert g.role_facts(raw, gates, TODAY)["age_days"] == 40


# --- the fit is not a gate ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fit", [15, 5, 1, 0, -1, -5])
async def test_no_role_is_dropped_for_a_low_score_and_the_score_goes_on_to_the_stored_role(js, fit):
    verdict, ways = await screen(gates_with(), fit=fit, comp="$120,000 - $140,000", posted=posted(3))
    assert verdict["keep"] is True and verdict["reason"] == "" and verdict["gate"] is None
    assert verdict["fit"] == fit, "kept as it was scored: the score ranks the role and stops nothing"
    assert ways == ["F_not_excluded", "F_place_ok", "F_comp_ok", "F_fresh"]


# --- order -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("role", "reason_starts"), [
    ({"company": "Globex", "location": "Denver", "work_mode": "onsite", "comp_max": 10_000, "fit": 0}, "excluded"),
    ({"location": "Denver", "work_mode": "onsite", "comp_max": 10_000, "fit": 0}, "location"),
    ({"comp_max": 10_000, "fit": 0}, "comp floor"),
    ({"posted": posted(31), "fit": 0}, "stale"),
])
async def test_gates_are_asked_cheapest_first_and_the_first_failure_is_the_reason(js, role, reason_starts):
    verdict, _ = await screen(gates_with(), **role)
    assert verdict["reason"].startswith(reason_starts)
