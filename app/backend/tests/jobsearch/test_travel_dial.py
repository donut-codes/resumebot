"""The Travel dial (docs/PRD.md 46.4; the owner, 2026-11-03): a Must on Hard pass, "at most N% of the time", that
screens a posting out when the travel its text states is more. It restores the owner's "Required travel must be no more
than 25%" that migration 0124 dropped with every reader-judged line. The percent is read in code and stored with the
line it rests on; a posting that states no percent passes, it never waits. Every name, place and figure is made up."""

import copy
from dataclasses import replace
from datetime import date

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import posting_facts as pf
from app.jobsearch.measures import dials
from app.jobsearch.profile import schema as p
from tests.jobsearch.test_regate import GATES, by_slug, role
from tests.support import OWNER, PROFILE, load_profile

TODAY = date(2026, 11, 3)


# --- reading the percent -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("line, percent", [
    ("This role may require up to 25% travel.", "25"),
    ("Travel: up to 25%", "25"),
    ("Travel required: 10-25%", "25"),
    ("Ability to travel approximately 10% to 15% of the time.", "15"),
    ("Willingness to travel 50% of the time.", "50"),
    ("Occasional travel (<10%) to customer sites.", "10"),
    ("Travel is less than 5 percent.", "5"),
    ("Must be able to travel up to 75% of working time.", "75"),
    ("~25% travel to the regional offices", "25"),
    ("Travel: 0%", "0"),
    ("Traveling 20 % of the time is expected.", "20"),
    ("Travel percentage: 12.5%", "12.5"),
    ("Travel: 10 – 30%", "30"),
    ("Travel: no more than 15% of the time.", "15"),
    ("Ability to work 100% remotely, with occasional travel (up to 10%).", "10"),
    ("Travel required: Up to 80%, of which 10% may be international.", "80"),
])
def test_a_travel_sentence_with_a_percent_gives_the_most_it_states(line, percent):
    found = pf.travel_percent_from_text(f"Programs Lead\nAbout the job\n{line}\nMore words.")
    assert found is not None and found.value == percent
    assert found.quote == " ".join(line.split()) and found.by == pf.CODE


@pytest.mark.parametrize("line", [
    "Travel: occasional.",
    "Some travel is required.",
    "No travel required.",
    "We offer 100% paid travel for team events.",
    "Travel expenses are reimbursed at 100% of cost.",
    "This is a 100% remote role; travel to the office is not required.",
    "The company grew 40% last year; we love to travel.",
    "Our travel insurance pays 80% of the premium.",
    "Employees get a 20% discount on travel with our partners.",
    "Work is performed 100% from home with no requirement to travel.",
    "No overnight travel is required (the role is 100% remote).",
    "Our platform collects data from vehicles that travel 99.9% of U.S. roads.",
    "Travel is not required; we are a hybrid team that is 60% on-site.",
])
def test_a_line_that_states_no_percent_of_the_job_s_travel_gives_none(line):
    assert pf.travel_percent_from_text(f"Programs Lead\nAbout the job\n{line}\nMore words.") is None


def test_of_several_figures_the_largest_stands_with_the_sentence_that_gave_it():
    found = pf.travel_percent_from_text("Travel: 10-25%.\nSome weeks of the year need 50% travel to a client.")
    assert (found.value, found.quote) == ("50", "Some weeks of the year need 50% travel to a client.")


def test_a_percent_on_the_line_under_a_travel_heading_is_the_travel():
    found = pf.travel_percent_from_text("Details\nTravel required\n25%\nMore.")
    assert found is not None and found.value == "25" and "25%" in found.quote
    assert pf.travel_percent_from_text("Details\nBonus target\n25%\nMore.") is None


def test_extract_keeps_the_percent_beside_the_line_the_old_travel_fact_holds():
    found = pf.extract("Programs Lead\nThis role may require up to 25% travel.\n")
    facts = found.facts()
    assert facts["travel_percent"] == {"value": "25", "quote": "This role may require up to 25% travel.", "by": "code"}
    assert "25% travel" in facts["travel"]["value"]  # the whole line, as before: the structured record reads it
    assert "travel_percent" not in pf.extract("Programs Lead\nSome travel is required.").facts()


# --- the dial ------------------------------------------------------------------------------------------------------


def test_the_dial_is_a_job_dial_with_a_must_and_no_prefer_that_passes_a_posting_stating_no_travel():
    dial = dials.DIALS["travel"]
    assert (dial.side, dial.fact, dial.kind, dial.choices, dial.unit) == ("job", "travel_percent", "number", ("must",),
                                                                        "%")
    assert dial.passes_unstated and not dials.DIALS["experience"].passes_unstated


def test_the_line_is_said_in_plain_words():
    assert dials.says("travel_percent", "<=", 25) == "The travel asked is at most 25%."
    assert dials.says("travel_percent", "<=", "12.5") == "The travel asked is at most 12.5%."
    assert dials.is_dial("travel_percent", "<=")


def rules_with(limit: int | None = 25) -> gating.Rules:
    data = copy.deepcopy(PROFILE)
    gate = {"id": "dial-travel-must", "kind": "job_fact", "fact": "travel_percent", "op": "<=", "value": limit}
    data["gates"] = {"location": {}, "other": [gate]}
    data["scoring"] = {"asks": []}
    return gating.Rules.of(p.Profile.model_validate(data), today=TODAY, hard_pass=1, criteria=1)


def line_for(percent: str | None, limit: int | None = 25) -> gating.Line:
    stored = {"travel_percent": {"value": percent, "quote": "a made-up line", "by": "code"}} if percent else None
    s = gating.Subject(company="Fabrikam", location=None, work_mode=None, comp=None, comp_min=None, comp_max=None,
                       posted=None, found=None, stored=stored)
    return next(line for line in gating.results(s, rules_with(limit)) if line.id == "dial-travel-must")


@pytest.mark.parametrize("percent, result", [("0", gating.PASS), ("10", gating.PASS), ("25", gating.PASS),
                                             ("25.5", gating.FAIL), ("26", gating.FAIL), ("100", gating.FAIL)])
def test_a_must_at_most_a_limit_passes_up_to_it_and_fails_above(percent, result):
    assert line_for(percent).result == result


def test_a_failure_says_what_the_posting_asks():
    line = line_for("40")
    assert line.fact == "the travel asked 40%"
    stop = gating.first_stop([line])
    assert stop is not None and stop.gate.value == "rule" and stop.gate_id == "dial-travel-must"


def test_a_posting_that_states_no_travel_passes_and_never_waits():
    line = line_for(None)
    assert line.result == gating.PASS and "no travel" in line.fact
    assert line.result != gating.WAITS


def test_a_limit_of_zero_is_a_limit_and_a_limit_of_none_stops_nothing():
    assert line_for("1", limit=0).result == gating.FAIL
    assert line_for("0", limit=0).result == gating.PASS


def test_the_facts_of_a_posting_hold_its_travel_only_when_stored_and_a_number():
    s = gating.Subject(company="Fabrikam", location=None, work_mode=None, comp=None, comp_min=None, comp_max=None,
                       posted=None, found=None)
    profile = rules_with().profile
    assert "travel_percent" not in gating.job_facts(s, profile, TODAY)
    stored = {"travel_percent": {"value": "15", "quote": "q", "by": "code"}}
    assert gating.job_facts(replace(s, stored=stored), profile, TODAY)["travel_percent"] == 15
    junk = {"travel_percent": {"value": "lots", "quote": "q", "by": "code"}}
    assert "travel_percent" not in gating.job_facts(replace(s, stored=junk), profile, TODAY)


# --- a save of Hard pass --------------------------------------------------------------------------------------------


def stored_travel(percent: str | None) -> dict:
    facts = {"travel_percent": {"value": percent, "quote": f"up to {percent}% travel", "by": "code"}} if percent else {}
    return {"facts": facts}


async def test_saving_the_limit_screens_out_the_roles_that_state_more_and_leaves_the_rest(js, client):
    """One save of Hard pass with the Travel dial on at 25: the roles whose text states more travel are screened out
    with the figure as the reason; one that states less, one that states none, and one with a figure the text never
    stored stay where they are (docs/PRD.md 46.4, 46.7)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"location": {}})
        session.add_all([
            role("a", stages.SECOND_IMPRESSION, gate_facts=stored_travel("40")),
            role("b", stages.SECOND_IMPRESSION, gate_facts=stored_travel("10")),
            role("c", stages.SECOND_IMPRESSION, gate_facts=stored_travel(None)),
            role("d", stages.SECOND_IMPRESSION),
        ])
        await session.commit()

    gate = {"id": "dial-travel-must", "kind": "job_fact", "section": "posting", "fact": "travel_percent", "op": "<=",
            "value": 25}
    saved = await client.put(GATES, json={"data": {"location": {}, "other": [gate]}, "why": "made-up test limit"})
    assert saved.status_code == 200
    async with db.session_factory()() as session:
        roles = await by_slug(session)
        assert roles["a"].status == stages.GATED_OUT
        assert roles["a"].screened_out_gate_id == "dial-travel-must"
        assert "40%" in roles["a"].screened_out_reason
        assert {roles[s].status for s in "bcd"} == {stages.SECOND_IMPRESSION}
