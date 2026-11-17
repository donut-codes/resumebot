"""The gate check (docs/PRD.md 39.11 and 39.12): one check over what is stored for a posting or a role, used wherever a
gate runs, whose stop is data (the step, the gate, the line it is and the versions it was judged under), not only a
sentence. Everything here is made up."""

import copy
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import sift
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from tests.builders import analysis_setup, findings, report_to_run, stored_by_title, submit_findings
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, reported

TODAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 5, 30, tzinfo=UTC)

#: The made-up owner's Hard pass for these tests: remote work anywhere, or on site in Oakfield; at least 100,000 on the
#: posted maximum; a posting no more than 30 days old; never an employer owned by private equity; a sentence about
#: travel that a reader answers; and a sentence the analyst's classes decide.
GATES = {
    "location": {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield"]},
                 "in_person": {"on": True, "locations": ["Oakfield"]}},
    "compensation": {"amount": 100_000, "against": "maximum"},
    "employer_names": ["Globex"],
    "other": [
        {"id": "stale", "kind": "posting_age", "days": 30, "section": "posting"},
        {"id": "no-pe", "kind": "employer_fact", "fact": "ownership", "op": "!=", "value": "pe", "section": "company"},
        {"id": "travel", "kind": "rule", "section": "responsibilities", "text": "Travel of 20% or more."},
        {"id": "gaps", "kind": "rule", "section": "qualifications", "text": "Most items are unproven.",
         "from_classes": "U", "share": 80},
    ],
}


def rules_for(gates: dict | None = None, *, rows=(), avoided=()) -> gating.Rules:
    data = copy.deepcopy(PROFILE)
    data["gates"] = copy.deepcopy(GATES if gates is None else gates)
    return gating.Rules.of(p.Profile.model_validate(data), today=TODAY, avoided=avoided, rows=rows, hard_pass=4,
                           criteria=2)


def who(**over) -> gating.Subject:
    """Somebody at Fabrikam whose posting passes every gate of GATES, with `over` changed."""
    base = {"company": "Fabrikam", "location": "Remote", "work_mode": "remote", "comp": None, "comp_min": 120_000,
            "comp_max": 150_000, "posted": TODAY - timedelta(days=2), "found": None}
    return gating.Subject(**{**base, **over})


def stop_of(subject: gating.Subject, rules: gating.Rules) -> tuple | None:
    stop = gating.check(subject, rules)
    return None if stop is None else (stop.gate.value, stop.gate_id, stop.step.value)


# --- the order, and the stop as data -------------------------------------------------------------------------------


def test_gates_are_tried_cheapest_first_and_the_stop_says_which_gate_and_which_line_and_whose_step():
    initech = Employer(id=uuid.uuid4(), owner=OWNER, key="initech", name="Initech", gated=True)
    hooli = Employer(id=uuid.uuid4(), owner=OWNER, key="hooli", name="Hooli", ownership="pe")
    rules = rules_for(rows=[initech, hooli], avoided={"jobs.example.test"})
    subject = who(company="Initech", location="Gotham, NY", work_mode="onsite", comp_min=40_000, comp_max=50_000,
                  posted=TODAY - timedelta(days=100), links=("https://www.jobs.example.test/a/1", None),
                  employer=initech)
    # Each change takes away the gate that stopped the subject, and the next in the order stops it.
    for change, want in [
        ({}, ("site", "jobs.example.test", "gate")),
        ({"links": ()}, ("employer", "employer", "gate")),
        ({"company": "Hooli", "employer": hooli}, ("location", "location", "gate")),
        ({"location": "Remote", "work_mode": "remote"}, ("compensation", "compensation", "gate")),
        ({"comp_min": 120_000, "comp_max": 150_000}, ("age", "stale", "gate")),
        ({"posted": TODAY - timedelta(days=2)}, ("employer", "no-pe", "gate")),
        ({"employer": None, "company": "Fabrikam",
          "answers": [{"id": "travel", "answer": "fail", "evidence": "40% travel"}]}, ("rule", "travel", "reader")),
        ({"answers": None, "analysis": {"required": [{"class": "U"}] * 4 + [{"class": "P"}]}},
         ("rule", "gaps", "gate")),
        ({"analysis": {"required": [{"class": "U"}] * 3 + [{"class": "P"}] * 2}}, None),
    ]:
        subject = replace(subject, **change)
        assert stop_of(subject, rules) == want, change


def test_the_role_pages_gate_lines_are_the_lines_of_the_check():
    """What the page shows and what stops a role are the same lines, so they cannot differ."""
    from app.jobsearch.gating import role_gates

    role = Opportunity(owner=OWNER, slug="x", company="Fabrikam", title="Lead", location="Gotham, NY",
                       move_type="onsite", comp="$40,000-$50,000", match_key="fabrikam|lead",
                       gate_answers=[{"id": "travel", "answer": "fail", "evidence": "Heavy."}], analysis={})
    rules = rules_for()
    shown = role_gates.gate_results(role, rules.profile, TODAY, None)
    assert shown == [line.as_dict() for line in gating.results(gating.role_subject(role, None), rules)]
    failed = [g["id"] for g in shown if g["result"] == "fail"]
    assert failed == ["location", "compensation", "travel"]
    assert gating.check(gating.role_subject(role, None), rules).gate_id == "location"


def test_a_gate_that_cannot_tell_stops_nothing():
    """A place the posting does not make clear, pay it does not post, a date it does not give, an employer fact nobody
    found, a sentence nobody answered and classes nobody gave are unknown: a gate passes until it is proven failed."""
    unknown = who(location="26 Locations", work_mode=None, comp_min=None, comp_max=None, posted=None, found=None,
                  employer=Employer(id=uuid.uuid4(), owner=OWNER, key="hooli", name="Hooli"), answers=[], analysis={})
    assert gating.check(unknown, rules_for()) is None
    assert {line.result for line in gating.results(unknown, rules_for())} <= {"pass", "unknown"}


def test_a_site_to_avoid_stops_a_posting_and_not_a_role():
    """A posting whose link goes to a site the owner avoids is stopped (docs/PRD.md 30.3); a role is not a posting."""
    rules = rules_for(avoided={"jobs.example.test"})
    posting = JobPosting(owner=OWNER, employer_id=None, ats=EXAMPLE_SYSTEM, ext_id="1", company="Fabrikam",
                         title="Lead", location="Remote", work_mode="remote", first_seen=NOW, last_seen=NOW, links=[],
                         url="https://careers.jobs.example.test/1", apply_url="https://elsewhere.example.test/1")
    assert stop_of(gating.posting_subject(posting, None), rules) == ("site", "jobs.example.test", "gate")
    role = Opportunity(owner=OWNER, slug="x", company="Fabrikam", title="Lead", match_key="fabrikam|lead",
                       url="https://careers.jobs.example.test/1", location="Remote", move_type="remote")
    assert gating.check(gating.role_subject(role, None), rules) is None


def test_a_role_tabs_own_gates_stop_a_subject_matched_to_that_tab_and_name_their_line():
    gates = copy.deepcopy(GATES)
    gates["by_role"] = {"builders": {"compensation": {"amount": 130_000, "against": "maximum"}}}
    rules = rules_for(gates)
    assert stop_of(who(), rules) is None, "All's gates alone"
    assert stop_of(who(target_role="builders", comp_min=120_000, comp_max=125_000), rules) == (
        "compensation", "compensation:builders", "gate"), "the tab's own lowest pay, named by its line"
    assert stop_of(who(target_role="analysts", comp_min=120_000, comp_max=125_000), rules) is None


def test_a_stop_is_recorded_as_data_with_the_versions_it_was_judged_under():
    rules = rules_for()
    stop = gating.check(who(location="Gotham, NY", work_mode="onsite"), rules)
    assert stop is not None
    assert gating.posting_stop_values(stop, rules) == {
        "filtered_by": stop.says, "judged_by": "gate", "judged_by_version": 2, "stop_gate": "location",
        "stop_gate_id": "location", "stop_hard_pass": 4, "route": None, "route_why": None}
    values = gating.role_stop_values(stop, rules)
    assert (values["screened_out_by"], values["screened_out_step"], values["screened_out_gate_id"]) == (
        "location", "gate", "location")
    assert (values["screened_out_hard_pass"], values["screened_out_criteria"]) == (4, 2)
    assert values["screened_out_reason"].startswith(stop.says.rstrip(".")), "and the sentence the owner reads stays"


# --- the same stop at each of the four points ----------------------------------------------------------------------

LOW = {"location": {"remote": {"on": True}}, "compensation": {"amount": 50_000, "against": "maximum"}}
HIGH = {"location": {"remote": {"on": True}}, "compensation": {"amount": 95_000, "against": "maximum"}}


def _posting(employer: Employer, ext: str, **more) -> JobPosting:
    return JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=ext, company=employer.name,
                      title=f"Programs Director {ext}", match_key=match_key(employer.name, f"Programs Director {ext}"),
                      first_seen=NOW, last_seen=NOW, links=[], location="Remote", work_mode="remote",
                      comp_min=70_000, comp_max=80_000, url=f"https://jobs.example.test/{ext}", **more)


async def test_one_gate_changed_gives_the_same_stop_at_each_of_the_four_points(js, client):
    """docs/PRD.md 39.11: the lowest pay is 50,000, a posting and a role at 70,000 to 80,000 pass it, and the owner
    raises it to 95,000. The structured stop (the gate and its line, the step, the version of Hard pass) is the same
    when a board is read, when a scout's report is stored, after research and when Hard pass is saved."""
    ids = await analysis_setup()  # three Found roles with no pay, a resume and the profile
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", LOW, by="test")
        employer = await _employer(session)
        session.add(Opportunity(
            owner=OWNER, slug="saved", company="Fabrikam", title="Saved Lead", fit=3, origin="scout",
            match_key="fabrikam|saved lead", status="second_impression", found=TODAY, location="Remote",
                move_type="remote",
            comp="$70,000-$80,000", employer_id=employer.id))
        session.add(_posting(employer, "saved", filtered_at=NOW, route="titles"))
        await session.commit()
    # 4. Hard pass is saved with the lowest pay raised: every posting and role is checked again.
    saved = await client.put("/api/v1/profile/gates", json={"data": HIGH, "why": "raise the floor"})
    assert saved.status_code == 200 and saved.json()["version"] == 3
    assert saved.json()["said"] == "Re-checked 1 posting and 4 roles: 2 newly stopped."
    version = 3
    async with db.session_factory()() as session:
        employer = await _employer(session)
        session.add(_posting(employer, "board"))  # 1. a board is read: a posting nobody has filtered
        await session.flush()
        await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)
        await session.commit()
    # 2. A scout's report is stored.
    async with db.session_factory()() as session:
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id
    await report_to_run(run_id, [reported("Fabrikam", "Reported Lead", comp_min=70_000, comp_max=80_000)], done=True)
    # 3. After research: the analyst found a band of 70,000 to 80,000 for a role the posting gave no pay for.
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Initech"]))
        await session.commit()
    await submit_findings(findings(ids["Initech"], pay={"band": [70_000, 80_000], "band_source": "the posting"}))

    async with db.session_factory()() as session:
        posting_stops = {p_.ext_id: (p_.stop_gate, p_.stop_gate_id, p_.judged_by, p_.stop_hard_pass)
                         for p_ in (await session.scalars(select(JobPosting))).all()}
        role_stops = {r.slug: (r.status, r.screened_out_by, r.screened_out_gate_id, r.screened_out_step,
                               r.screened_out_hard_pass) for r in (await session.scalars(select(Opportunity))).all()}
    reported_role = (await stored_by_title(run_id))["Reported Lead"]
    want_posting = ("compensation", "compensation", "gate", version)
    want_role = ("gated_out", "compensation", "compensation", "gate", version)
    assert posting_stops["saved"] == want_posting, "4. the save"
    assert posting_stops["board"] == want_posting, "1. the board read"
    assert role_stops["saved"] == want_role, "4. the save"
    assert (reported_role.status, reported_role.screened_out_by, reported_role.screened_out_gate_id,
            reported_role.screened_out_step, reported_role.screened_out_hard_pass) == want_role, "2. the report"
    assert role_stops["initech"] == want_role, "3. after research"


async def _employer(session) -> Employer:
    found = await employers.find_or_make(session, OWNER, "Fabrikam")
    assert found is not None
    return found
