"""The measurable dials (docs/PRD.md 46): each dial is a fact the app measures, set as a Must (a gate) or a Prefer
 (an ask), with no default. A fact not stated decides nothing: a Prefer scores nothing and a Must holds the role,
 never stopping it. Every place, name and number here is made up."""

import copy
from datetime import date, timedelta

import pytest

from app.jobsearch.employers import core as employers
from app.jobsearch.fit import scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.measures import dials
from app.jobsearch.models import Employer
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import schema as p
from app.jobsearch.research import prioritize
from tests.support import OWNER, PROFILE

TODAY = date(2026, 10, 26)
HOME = {"in_person": {"on": True, "locations": ["Madison, WI"]}}


def profile_with(*, gates: list | None = None, asks: list | None = None, location: dict | None = None) -> p.Profile:
    data = copy.deepcopy(PROFILE)
    data["gates"] = {"location": location or {}, "other": gates or []}
    data["scoring"] = {"asks": asks or []}
    return p.Profile.model_validate(data)


def must(dial_id: str, op: str, value, kind: str = "job_fact", fact: str | None = None) -> dict:
    return {"id": f"must-{dial_id}", "kind": kind, "fact": fact or dials.DIALS[dial_id].fact, "op": op, "value": value}


def ask(dial_id: str, op: str, value, points: int) -> dict:
    return {"id": f"ask-{dial_id}", "section": "posting", "says": f"A made-up line about {dial_id}", "points": points,
            "checked_by": "app", "fact": dials.DIALS[dial_id].fact, "op": op, "value": value}


def subject(**over) -> gating.Subject:
    base = {"company": "Fabrikam", "location": "Milwaukee, WI", "work_mode": "onsite", "comp": None,
            "comp_min": 90_000, "comp_max": 120_000, "posted": TODAY - timedelta(days=3), "found": None}
    return gating.Subject(**{**base, **over})


def stored(**facts) -> dict:
    return {k: {"value": v, "quote": "a made-up line", "by": "code"} for k, v in facts.items()}


def rules_of(profile: p.Profile) -> gating.Rules:
    return gating.Rules.of(profile, today=TODAY, hard_pass=1, criteria=1)


def lines_of(profile: p.Profile, s: gating.Subject) -> dict[str, gating.Line]:
    return {line.id: line for line in gating.results(s, rules_of(profile))}


# --- the registry ---------------------------------------------------------------------------------------------------


def test_each_dial_names_its_fact_and_the_choices_it_takes():
    assert set(dials.DIALS) == {"size", "ownership", "industry", "distance", "experience", "degree", "freshness",
                                "travel", "pay", "mode", "clearance", "title", "employer-named",
                                "business-development", "main-work"}
    # Edited 2026-11-12 (PRD 46.4): Main work is the fifteenth dial, a words dial for a Must only (the owner, "build the
    # main work setting").
    # Edited 2026-11-05 (PRD 46.4): Leads business development is the fourteenth dial, a pick of yes and no that takes a
    # Must or a Prefer (the owner's request for a gate of that kind).
    # Edited 2026-11-03 (PRD 46.4): Travel and Employer named are the twelfth and thirteenth dials, each a Must only
    # (the owner's two hard passes that migration 0124 dropped, brought back as checks the app runs).
    # Edited 2026-10-28 (PRD 46.4): Title words is the eleventh dial, a Must only.
    # Edited 2026-10-27 (PRD 30.10, 46.1): Avoid is gone from the dials; it was in every row.
    assert dials.DIALS["pay"].choices == ("prefer",)
    assert dials.DIALS["mode"].choices == ("prefer",)
    assert dials.DIALS["clearance"].choices == ("prefer",)
    assert dials.DIALS["distance"].choices == ("must", "prefer")
    assert all("avoid" not in d.choices for d in dials.DIALS.values())
    assert dials.DIALS["size"].side == "employer" and dials.DIALS["distance"].side == "job"


@pytest.mark.parametrize("employees, band", [(3, "micro"), (9, "micro"), (10, "small"), (99, "small"),
                                              (100, "mid"), (999, "mid"), (1000, "large"), (50_000, "large")])
def test_headcount_is_read_as_a_band(employees, band):
    assert employers.facts(Employer(owner=OWNER, key="k", name="K", employees=employees), TODAY)["size_band"] == band


def test_no_headcount_is_no_band():
    assert "size_band" not in employers.facts(Employer(owner=OWNER, key="k", name="K"), TODAY)


def test_a_pick_list_condition_is_met_by_any_listed_value():
    assert scoring.compare("pe", "in", "pe,vc") is True
    assert scoring.compare("public", "in", "pe,vc") is False
    assert scoring.compare("healthcare software finance", "in", "health") is True
    assert scoring.compare("private", "in", "private equity") is False


# --- the facts of a posting -----------------------------------------------------------------------------------------


def test_job_facts_are_read_from_what_is_stored_and_the_subject():
    profile = profile_with(location=HOME)
    facts = gating.job_facts(subject(stored=stored(experience_years="6", degree_required="bachelor",
                                                   clearance="none")), profile, TODAY)
    assert facts["experience_years"] == 6 and facts["degree_required"] == "bachelor" and facts["clearance"] == "none"
    assert facts["pay_top"] == 120_000 and facts["posted_days"] == 3 and facts["work_mode"] == "onsite"
    assert 72 <= facts["distance_miles"] <= 76


def test_a_remote_posting_has_no_distance_and_no_home_has_none_either():
    home = profile_with(location=HOME)
    assert "distance_miles" not in gating.job_facts(subject(work_mode="remote"), home, TODAY)
    assert "distance_miles" not in gating.job_facts(subject(), profile_with(), TODAY)
    assert "distance_miles" not in gating.job_facts(subject(location="Nowhereville, ZZ"), home, TODAY)


def test_the_home_place_is_the_first_in_person_place_the_gazetteer_knows_else_a_hybrid_one():
    both = {"in_person": {"on": True, "locations": ["Nowhereville, ZZ", "Denver, CO"]},
            "hybrid": {"on": True, "locations": ["Madison, WI"]}}
    assert dials.home_place(profile_with(location=both).gates) == "Denver, CO"
    only_hybrid = {"hybrid": {"on": True, "locations": ["Madison, WI"]}}
    assert dials.home_place(profile_with(location=only_hybrid).gates) == "Madison, WI"
    assert dials.home_place(profile_with().gates) is None


# --- a Must is a gate: it fails, passes, or waits for its fact -------------------------------------------------------


def test_a_must_on_distance_fails_beyond_its_miles_and_passes_within_them():
    profile = profile_with(gates=[must("distance", "<=", 50)], location=HOME)
    assert lines_of(profile, subject(location="Denver, CO"))["must-distance"].result == gating.FAIL
    assert lines_of(profile, subject())["must-distance"].result == gating.FAIL  # Milwaukee is 74 miles from Madison
    assert lines_of(profile_with(gates=[must("distance", "<=", 90)], location=HOME), subject()
                    )["must-distance"].result == gating.PASS


@pytest.mark.parametrize("dial_id, op, value, facts, result", [
    ("experience", "<=", 5, {"experience_years": "8"}, gating.FAIL),
    ("experience", "<=", 5, {"experience_years": "4"}, gating.PASS),
    ("degree", "in", "associate,bachelor", {"degree_required": "master"}, gating.FAIL),
    ("degree", "in", "associate,bachelor", {"degree_required": "bachelor"}, gating.PASS),
])
def test_each_job_dial_as_a_must_fails_or_passes_on_its_fact(dial_id, op, value, facts, result):
    profile = profile_with(gates=[must(dial_id, op, value)], location=HOME)
    assert lines_of(profile, subject(stored=stored(**facts)))[f"must-{dial_id}"].result == result


def test_a_must_on_freshness_reads_the_postings_age():
    assert lines_of(profile_with(gates=[must("freshness", "<=", 2)]), subject())["must-freshness"].result == gating.FAIL
    assert lines_of(profile_with(gates=[must("freshness", "<=", 7)]), subject())["must-freshness"].result == gating.PASS


def test_a_must_whose_fact_is_not_stated_waits_and_never_stops_the_role():
    profile = profile_with(gates=[must("experience", "<=", 5), must("distance", "<=", 50)])
    remote = subject(location="Remote", work_mode="remote")
    lines = lines_of(profile, remote)
    assert [lines[i].result for i in ("must-experience", "must-distance")] == [gating.WAITS, gating.WAITS]
    assert "waiting for" in lines["must-experience"].fact and "years" in lines["must-experience"].fact
    assert gating.check(remote, rules_of(profile)) is None


@pytest.mark.parametrize("dial_id, fact_name, value, record, result", [
    ("ownership", "ownership", "public,nonprofit", {"ownership": "pe"}, gating.FAIL),
    ("ownership", "ownership", "public,nonprofit", {"ownership": "public"}, gating.PASS),
    ("size", "size_band", "micro,small", {"employees": 5000}, gating.FAIL),
    ("size", "size_band", "micro,small", {"employees": 40}, gating.PASS),
    ("industry", "industry", "health", {"industry": "Healthcare staffing"}, gating.PASS),
    ("industry", "industry", "health", {"industry": "Mining"}, gating.FAIL),
])
def test_each_employer_dial_as_a_must_reads_the_employer_record(dial_id, fact_name, value, record, result):
    profile = profile_with(gates=[must(dial_id, "in", value, kind="employer_fact", fact=fact_name)])
    row = Employer(owner=OWNER, key="fabrikam", name="Fabrikam", **record)
    assert lines_of(profile, subject(employer=row))[f"must-{dial_id}"].result == result


def test_an_employer_must_waits_while_the_record_lacks_the_fact():
    profile = profile_with(gates=[must("ownership", "in", "public", kind="employer_fact")])
    row = Employer(owner=OWNER, key="fabrikam", name="Fabrikam")
    assert lines_of(profile, subject(employer=row))["must-ownership"].result == gating.WAITS
    assert lines_of(profile, subject())["must-ownership"].result == gating.WAITS


# --- Prefer and Avoid are asks the app decides: a fact not stated scores nothing -----------------------------------


def score(profile: p.Profile, known: dict) -> dict:
    return scoring.compute({"answers": []}, profile, known)


@pytest.mark.parametrize("dial_id, op, value, points, facts, earned", [
    ("distance", "<=", 50, 3, {"distance_miles": 20.0}, 3),
    ("distance", "<=", 50, 3, {"distance_miles": 80.0}, 0),
    # Edited 2026-10-27 (PRD 30.10): the lines with negative points lost -2, -2, -2, -1 and -3 when met; they earn 0.
    ("distance", ">", 50, -2, {"distance_miles": 80.0}, 0),
    ("experience", ">", 8, -2, {"experience_years": 10}, 0),
    ("degree", "in", "doctorate", -2, {"degree_required": "doctorate"}, 0),
    ("pay", ">=", 150_000, 2, {"pay_top": 160_000}, 2),
    ("pay", "<", 100_000, -1, {"pay_top": 90_000}, 0),
    ("mode", "in", "remote", 1, {"work_mode": "remote"}, 1),
    ("clearance", "in", "required", -3, {"clearance": "required"}, 0),
    ("clearance", "in", "required", -3, {"clearance": "none"}, 0),
    ("freshness", "<=", 3, 1, {"posted_days": 2}, 1),
])
def test_a_prefer_earns_its_points_and_a_negative_line_nothing(dial_id, op, value, points, facts, earned):
    row = score(profile_with(asks=[ask(dial_id, op, value, points)]), facts)["asks"][0]
    assert row["earned"] == earned and row["by"] == "app"


def test_a_prefer_or_avoid_with_no_fact_scores_nothing_and_is_not_sent_to_a_reader():
    profile = profile_with(asks=[ask("experience", "<=", 5, 3), ask("clearance", "in", "required", -3)])
    rows = score(profile, {})["asks"]
    assert [r["earned"] for r in rows] == [0, 0] and [r["answer"] for r in rows] == ["unknown", "unknown"]


def test_the_words_of_a_dial_line_say_the_fact_the_value_and_who_checks():
    gate = pg.Gate.model_validate(must("distance", "<=", 50))
    assert "miles" in pg.gate_says(gate) and "50" in pg.gate_says(gate)
    pick = pg.Gate.model_validate(must("ownership", "in", "pe,vc", kind="employer_fact"))
    assert "private equity" in pg.gate_says(pick).lower()
    assert pg.who_checks(gate) == "app"


# --- the places the facts come from: the posting text read in code, and the score -----------------------


def a_role(**over):
    from types import SimpleNamespace

    base = {"employer_id": None, "company": "Fabrikam", "location": "Milwaukee, WI", "move_type": "onsite",
            "comp": None, "comp_source": None, "posted": TODAY - timedelta(days=3), "found": None, "target_role": None,
            "gate_facts": {}, "gate_answers": None, "analysis": {}}
    return SimpleNamespace(**{**base, **over})


def test_a_must_on_the_years_asked_stops_a_role_the_moment_its_text_is_read():
    from app.jobsearch.gating import gate_facts, posting_facts

    rules = rules_of(profile_with(gates=[must("experience", "<=", 5)]))
    found = posting_facts.extract("Director\nYou bring 9+ years of experience leading programs.")
    stop = gate_facts.decide(a_role(), rules, found)
    assert stop is not None and stop.gate_id == "must-experience"
    assert "years of experience asked" in stop.says
    quiet = posting_facts.extract("Director\nA warm team that likes to build.")
    assert gate_facts.decide(a_role(), rules, quiet) is None, "no years stated: the role is held, not stopped"


def test_the_score_reads_the_facts_stored_on_the_role_and_nothing_when_none_are_stored():
    profile = profile_with(asks=[ask("experience", "<=", 12, 2)], location=HOME)  # edited 2026-10-27: was > 8 for -2
    role = a_role(gate_facts={"facts": stored(experience_years="10")})
    known = prioritize.known_of(role, profile, None)
    assert known["experience_years"] == 10
    assert score(profile, known)["asks"][0]["earned"] == 2
    bare = prioritize.known_of(a_role(), profile, None)
    assert "experience_years" not in bare and score(profile, bare)["asks"][0]["earned"] == 0
