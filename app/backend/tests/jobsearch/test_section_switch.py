"""One switch on each section of Gates and of Scoring (docs/PRD.md 30.4, 30.6): what an off section stops, and how the
work location keeps one list of locations for each way of working. Everything here is made up (tests/support.py). The
older shapes (a switch on each gate or ask) are tested with migration 0049: tests/test_stored_versions_migration.py."""

import copy
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.actions import _gates
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import role_gates, sift
from app.jobsearch.measures.pay_place import is_excluded, location_ok
from app.jobsearch.models import Employer, JobPosting
from app.jobsearch.profile import base as pb
from app.jobsearch.profile import gate_words
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile import views as pv
from tests.support import EXAMPLE_SYSTEM, LOCATIONS, OWNER, PROFILE, load_profile

TODAY = date(2026, 10, 10)


def gates_from(doc: dict) -> pg.GateSet:
    return pg.GateSet.model_validate(copy.deepcopy(doc))


def profile_with(gates: dict | None = None, scoring_doc: dict | None = None) -> p.Profile:
    data = copy.deepcopy(PROFILE)
    if gates is not None:
        data["gates"] = copy.deepcopy(gates)
    if scoring_doc is not None:
        data["scoring"] = copy.deepcopy(scoring_doc)
    return p.Profile.model_validate(data)


# --- a new install and a current document: every section is on, and nothing is checked either way -------------------


def test_a_new_install_has_every_section_on_and_nothing_set_so_nothing_is_checked():
    gates = pg.GateSet()
    assert gates.sections_off == [] and all(gates.section_on(s) for s in gate_words.SECTION_IDS)
    assert not (gates.checks_location() or gates.checks_pay())
    assert gates.checked_lines() == []
    assert ps.Scoring().sections_off == []


def test_the_sections_are_the_seven_of_the_outline_for_gates_and_for_scoring_alike():
    assert gate_words.SECTION_IDS == pb.SECTION_IDS
    assert pg.GateSet(sections_off=["preferred", "company", "company"]).sections_off == ["company", "preferred"], (
        "once each, in the page's order")
    with pytest.raises(ValueError, match="sections_off"):
        pg.GateSet(sections_off=["elsewhere"])
    with pytest.raises(ValueError, match="sections_off"):
        pg.GateSet(sections_off=["overall"])  # Overall held only the lowest fit, which is gone
    with pytest.raises(ValueError, match="sections_off"):
        pg.RoleGates(sections_off=["overall"])
    with pytest.raises(ValueError, match="sections_off"):
        ps.Scoring(sections_off=["overall"])


def test_a_document_in_the_current_shape_has_no_switch_on_a_line():
    doc = {"sections_off": ["preferred"], "other": [{"id": "x", "kind": "rule", "section": "preferred", "text": "X"}]}
    gates = gates_from(doc)
    assert "on" not in gates.other[0].model_dump() and "on" not in gates.compensation.model_dump()
    assert "on" not in gates.location.model_dump()
    assert pg.GateSet.model_validate(gates.model_dump(mode="json")) == gates
    with pytest.raises(ValueError):
        ps.Ask(id="a", says="A", points=1, on=True)  # an ask has no switch of its own either


# --- Gates: an off section stops nothing ----------------------------------------------------------------------------


def full_gates(**over) -> pg.GateSet:
    """The made-up gates with a sentence in every section, a posting age, an employer rule and the employers' names."""
    doc = copy.deepcopy(PROFILE["gates"])
    doc["other"] = [
        {"id": "no-pe", "kind": "employer_fact", "section": "company", "fact": "ownership", "op": "!=", "value": "pe"},
        {"id": "company-says", "kind": "rule", "section": "company", "text": "A staffing firm"},
        {"id": "age", "kind": "posting_age", "section": "posting", "days": 45},
        {"id": "posting-says", "kind": "rule", "section": "posting", "text": "Asks you to relocate"},
        *({"id": f"{s}-says", "kind": "rule", "section": s, "text": f"A sentence in {s}"}
          for s in ("description", "responsibilities", "qualifications", "preferred", "compensation")),
    ]
    doc.update(over)
    return pg.GateSet.model_validate(doc)


def profile_of(gates: pg.GateSet) -> p.Profile:
    data = copy.deepcopy(PROFILE)
    data["gates"] = gates.model_dump(mode="json")
    return p.Profile.model_validate(data)


def test_with_every_section_on_everything_is_checked():
    gates = full_gates()
    prof = profile_of(gates)
    engine = pv.engine_gates(prof)
    assert engine.places_gated and engine.comp_floor == 95_000 and engine.stale_days == 45
    assert engine.excluded_employers
    assert [x["id"] for x in pg.gate_lines(gates) if x["on"]] == [
        "location", "compensation", "no-pe", "company-says", "age", "posting-says", "description-says",
        "responsibilities-says", "qualifications-says", "preferred-says", "compensation-says"]


@pytest.mark.parametrize(("section", "gone_from_the_engine"), [
    ("posting", lambda e: not e.places_gated and e.stale_days == pv.NO_AGE_LIMIT),
    ("compensation", lambda e: e.comp_floor == 0 and "compensation" not in e.says),
    ("company", lambda e: e.excluded_employers == []),
])
def test_a_code_gate_in_an_off_section_checks_nothing(section, gone_from_the_engine):
    gates = full_gates(sections_off=[section])
    assert gone_from_the_engine(pv.engine_gates(profile_of(gates)))


def test_an_off_section_keeps_what_is_in_it_and_the_other_sections_keep_checking():
    gates = full_gates(sections_off=["posting"])
    assert gates.location.hybrid.locations == LOCATIONS and any(g.id == "age" for g in gates.other)
    engine = pv.engine_gates(profile_of(gates))
    assert engine.comp_floor == 95_000 and engine.excluded_employers
    assert location_ok("Gotham, NY", "onsite", engine) is True, "no work location gate: every place passes"
    assert location_ok("Gotham, NY", "onsite", pv.engine_gates(profile_of(full_gates()))) is False


def test_a_work_location_with_no_way_of_working_ticked_is_no_gate_at_all():
    gates = full_gates(location={"remote": {"on": False}, "hybrid": {"on": False, "locations": ["Oakfield"]}})
    assert not gates.location.is_set() and not gates.checks_location()
    assert pv.engine_gates(profile_of(gates)).places_gated is False


@pytest.mark.parametrize("section", gate_words.SECTION_IDS)
def test_the_sentences_of_an_off_section_are_not_sent_to_a_reader_and_the_rest_are(section):
    gates = full_gates(sections_off=[section])
    sent = [g["id"] for g in pv.for_scout(profile_of(gates))["hard_gates"]]
    mine = {g.id for g in gates.other if g.section == section}
    assert mine and not mine & set(sent), "nothing in an off section is sent"
    others = {g.id for g in gates.other if g.section != section}
    assert others <= set(sent), "and the other sections' lines still are"


def test_the_lines_the_app_checks_in_an_off_section_are_not_checked_either():
    on = full_gates()
    off = full_gates(sections_off=["company", "posting"])
    assert [g.id for g in on.checked_lines() if g.kind != "rule"] == ["no-pe", "age"]
    assert [g.id for g in off.checked_lines() if g.kind != "rule"] == []
    assert [g.id for g in off.checked_lines()] == [
        "description-says", "responsibilities-says", "qualifications-says", "preferred-says", "compensation-says"]
    assert pv.engine_gates(profile_of(off)).stale_days == pv.NO_AGE_LIMIT, "the posting age is Job posting's"


def test_the_pay_floor_follows_its_sections_switch():
    assert pv.pay_floor(profile_of(full_gates())) == 95_000
    assert pv.pay_floor(profile_of(full_gates(sections_off=["compensation"]))) == 0
    assert pv.pay_floor(profile_of(full_gates(sections_off=["posting"]))) == 95_000


async def test_the_employers_gated_on_their_records_stop_nothing_while_company_is_off(js):
    def stops(rules: gating.Rules, company: str) -> bool:
        """Whether the one gate check stops somebody at this company for an employer gate."""
        who = gating.Subject(company=company, location=None, work_mode=None, comp=None, comp_min=None, comp_max=None,
                             posted=None, found=None)
        stop = gating.check(who, rules)
        return stop is not None and stop.gate == "employer"

    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add(Employer(owner=OWNER, key="initech", name="Initech", gated=True))
        await session.flush()
        assert stops(await gating.rules(session, OWNER), "Initech Corp")
        gates = (await profile_store.current(session, OWNER)).gates.model_dump(mode="json")
        gates["sections_off"] = ["company"]
        await profile_store.save(session, OWNER, "gates", gates, by="owner")
        off = await gating.rules(session, OWNER)
        assert not stops(off, "Initech Corp") and not stops(off, "Globex"), (
            "neither a gated employer record nor a name gated here")
        gates["sections_off"] = []
        await profile_store.save(session, OWNER, "gates", gates, by="owner")
        assert stops(await gating.rules(session, OWNER), "Initech Corp")
        assert not is_excluded("Initech Corp", await _gates(session, OWNER)), (
            "the process carries the names on Hard pass's list; an employer's own gate is the check's, by its record")


NOW = datetime(2026, 10, 10, 5, 30, tzinfo=UTC)


async def sifted(session, *off: str) -> dict[str, str | None]:
    """Filter four made-up postings (a gated employer's, a place no one works from, pay under the amount, one that
    passes) with these sections of Gates switched off; each posting's id and the rule that stopped it."""
    await load_profile(session, OWNER)
    profile = await profile_store.current(session, OWNER)
    profile.gates.sections_off = list(off)
    fab = await employers.find_or_make(session, OWNER, "Fabrikam")
    initech = await employers.find_or_make(session, OWNER, "Initech")
    initech.gated = True
    posting = lambda e, ext, **more: JobPosting(  # noqa: E731
        owner=OWNER, employer_id=e.id, ats=EXAMPLE_SYSTEM, ext_id=ext, company=e.name, title="Director", first_seen=NOW,
        last_seen=NOW, links=[], **more)
    session.add_all([
        posting(fab, "ok", location="Remote", work_mode="remote"),
        posting(initech, "employer", location="Remote", work_mode="remote"),
        posting(fab, "place", location="Greater Gotham", work_mode="onsite"),
        posting(fab, "pay", location="Remote", work_mode="remote", comp_min=60000, comp_max=70000)])
    await session.flush()
    await sift.sift(session, OWNER, profile, now=NOW)
    return {p.ext_id: p.filtered_by for p in (await session.scalars(select(JobPosting))).all()}


async def test_the_filter_stops_a_posting_by_each_gate_while_every_section_is_on(js):
    async with db.session_factory()() as session:
        stopped = await sifted(session)
        profile = await profile_store.current(session, OWNER)
    assert stopped == {
        "ok": None, "employer": "an employer you never work for", "place": pg.location_says(profile.gates.location),
        "pay": pg.compensation_says(profile.gates.compensation)}


@pytest.mark.parametrize(
    ("section", "posting"), [("company", "employer"), ("posting", "place"), ("compensation", "pay")])
async def test_the_filter_lets_through_what_only_a_gate_in_an_off_section_would_stop(js, section, posting):
    async with db.session_factory()() as session:
        stopped = await sifted(session, section)
    assert stopped[posting] is None, "nothing in an off section stops a posting"
    assert stopped["ok"] is None
    others = {k: v for k, v in stopped.items() if k not in ("ok", posting)}
    assert others and all(v for v in others.values()), "and the other sections still do"


def test_the_role_page_shows_no_gate_from_an_off_section():
    role = SimpleNamespace(company="Initech", title="Lead", fit=1, location="Oakfield, ST", move_type="onsite",
                           comp="$80,000", posted=None, found=TODAY, analysis=None, target_role=None)
    boss = Employer(owner=OWNER, key="initech", name="Initech", gated=True)

    def shown(*off):
        prof = profile_of(full_gates(sections_off=list(off)))
        return [g["id"] for g in role_gates.gate_results(role, prof, TODAY, None)]

    everything = shown()
    assert {"location", "compensation", "no-pe", "age", "company-says", "posting-says"} <= set(everything)
    assert not any("fit" in gate for gate in everything), "no gate is about a role's fit"
    assert not {"location", "age", "posting-says"} & set(shown("posting"))
    assert "compensation" not in shown("compensation") and "compensation-says" not in shown("compensation")
    assert not {"no-pe", "company-says"} & set(shown("company"))
    # An employer gated on its record is a gate of Company, too.
    prof = profile_of(full_gates())
    assert "employer" in [g["id"] for g in role_gates.gate_results(role, prof, TODAY, boss)]
    off = profile_of(full_gates(sections_off=["company"]))
    assert "employer" not in [g["id"] for g in role_gates.gate_results(role, off, TODAY, boss)]

# --- the work location: one list of locations for each way of working -----------------------------------------------


def test_locations_are_where_a_posting_must_be_and_empty_means_anywhere():
    prof = profile_with(gates={"location": {
        "remote": {"on": True, "locations": ["United States"]}, "hybrid": {"on": True},
        "in_person": {"on": True, "locations": ["Oakfield"]}}})
    gates = pv.engine_gates(prof)
    assert gates.remote_countries == ["United States"]
    assert location_ok("Manila, Philippines", "remote", gates) is False
    assert location_ok("Gotham, NY", "hybrid", gates) is True, "hybrid with no locations: anywhere"
    assert location_ok("Gotham, NY", "onsite", gates) is False and location_ok("Oakfield, ST", "onsite", gates) is True


def test_the_summary_line_is_built_from_the_lists():
    loc = gates_from({"location": {"remote": {"on": True, "locations": ["United States"]},
                                   "hybrid": {"on": True, "locations": ["Oakfield", "Riverton"]},
                                   "in_person": {"on": True}}}).location
    assert pg.location_says(loc) == (
        "Work must be remote (in United States), hybrid (in Oakfield, Riverton) or in person (anywhere).")


def test_locations_are_kept_once_each_in_order_without_regard_to_case():
    assert gates_from({"location": {"hybrid": {"on": True, "locations": ["Oakfield", "oakfield", " Riverton ", ""]}}}
                      ).location.hybrid.locations == ["Oakfield", "Riverton"]


# --- Scoring: an off section counts for nothing ---------------------------------------------------------------------

ASKS = [
    {"id": "builds", "section": "description", "says": "The work is building programs", "points": 3},
    {"id": "degree", "section": "qualifications", "says": "Asks for a degree", "points": 2},
    {"id": "statistics", "section": "qualifications", "says": "Asks for statistics", "points": 1,
     "roles": ["analysts"]},
    {"id": "small", "section": "company", "says": "The employer is small", "points": 1},
    {"id": "age", "section": "posting", "says": "Posted over 30 days ago", "points": -1, "days": 30,
     "checked_by": "app"},
    {"id": "pay-high", "section": "compensation", "says": "Ask over 150,000", "points": 2, "amount": 150000,
     "above": True, "checked_by": "app"},
]
ROLES = [{"id": "builders", "name": "Builders", "titles": ["Director"], "description": "Builds.",
          "responsibilities": "Plans the year."},
         {"id": "analysts", "name": "Analysts", "titles": ["Analyst"], "description": "Counts.",
          "responsibilities": "Builds the report."}]


def scored_profile(**scoring_doc) -> p.Profile:
    data = copy.deepcopy(PROFILE)
    data["criteria"] = {"company": "A public agency.", "target_roles": copy.deepcopy(ROLES)}
    data["scoring"] = {"asks": copy.deepcopy(ASKS), **scoring_doc}
    data["gates"] = {}
    return p.Profile.model_validate(data)


def test_every_section_on_counts_every_ask_and_the_built_in_one_is_in_responsibilities():
    prof = scored_profile()
    assert prof.scoring.sections_off == []
    assert scoring.possible(prof, "builders") == 3 + 2 + 1 + 2 + ps.RESPONSIBILITIES_POINTS
    assert scoring.possible(prof, "analysts") == 3 + 2 + 1 + 1 + 2 + ps.RESPONSIBILITIES_POINTS


def test_an_ask_in_an_off_section_adds_no_points_and_no_possible_points_on_the_tab_it_is_off_on():
    # All's switch governs All's asks; a role's own switch governs the asks on its own tab (docs/PRD.md 31.9).
    prof = scored_profile(sections_off=["qualifications"])
    assert scoring.possible(prof, "builders") == 3 + 1 + 2 + ps.RESPONSIBILITIES_POINTS
    assert scoring.possible(prof, "analysts") == 3 + 1 + 2 + 1 + ps.RESPONSIBILITIES_POINTS, (
        "the analysts' own ask is on their own tab, whose switches are their own")
    assert scoring.possible(prof) == 3 + 1 + 2, "a role with no target role: the asks for every role"
    assert scoring.possible_by_role(prof) == {"builders": 9, "analysts": 10}
    assert [a.id for a in prof.asks_for("analysts")] == [
        "builds", "statistics", "small", "age", "pay-high", ps.RESPONSIBILITIES_ID]
    assert prof.scoring.asks[1].says == "Asks for a degree", "it stays on the page"
    own = scored_profile(by_role={"analysts": {"sections_off": ["qualifications"]}})
    assert scoring.possible(own, "analysts") == 3 + 2 + 1 + 2 + ps.RESPONSIBILITIES_POINTS, (
        "off on the Analysts tab: their own ask is gone, All's degree ask still counts for them")
    assert scoring.possible(own, "builders") == 3 + 2 + 1 + 2 + ps.RESPONSIBILITIES_POINTS, "other tabs are untouched"
    assert [a.id for a in own.asks_for("analysts")] == ["builds", "degree", "small", "age", "pay-high",
                                                       ps.RESPONSIBILITIES_ID]


def test_the_built_in_responsibilities_ask_follows_its_sections_switch():
    prof = scored_profile(sections_off=["responsibilities"])
    assert ps.RESPONSIBILITIES_ID not in [a.id for a in prof.asks_for("builders")]
    assert scoring.possible(prof, "builders") == 3 + 2 + 1 + 2 + 0
    gone = scoring.possible(scored_profile(), "builders") - scoring.possible(prof, "builders")
    assert gone == ps.RESPONSIBILITIES_POINTS


def test_a_roles_fit_is_worked_out_from_the_asks_in_sections_that_are_on():
    answers = {"asks": [{"id": a, "answer": "met", "evidence": "x"} for a in
                        ("builds", "degree", "statistics", "small", "pay-high", ps.RESPONSIBILITIES_ID)]}
    on = scoring.compute(answers, scored_profile(), target_role="analysts", facts={"ask": 160_000})
    off = scoring.compute(
        answers, scored_profile(sections_off=["qualifications", "compensation"],
                                by_role={"analysts": {"sections_off": ["qualifications"]}}),
        target_role="analysts", facts={"ask": 160_000})
    assert {a["id"] for a in on["asks"]} >= {"degree", "statistics", "pay-high"}
    assert {a["id"] for a in off["asks"]} == {"builds", "small", "age", ps.RESPONSIBILITIES_ID}
    assert off["possible"] == 3 + 1 + ps.RESPONSIBILITIES_POINTS and on["possible"] > off["possible"]
    assert off["earned"] == 3 + 1 + ps.RESPONSIBILITIES_POINTS, "the answers to the asks that are off are set aside"


def test_the_fit_bars_follow_the_switches():
    role = SimpleNamespace(fit=6, fit_breakdown={"asks": []}, target_role="builders")
    on, off = scored_profile(), scored_profile(sections_off=["qualifications", "compensation"])
    assert scoring.percent_of(role, scoring.bounds(on)) == round(6 * 100 / 11)
    assert scoring.percent_of(role, scoring.bounds(off)) == round(6 * 100 / 7)
    assert scoring.percent_of(role, scoring.bounds(off)) >= 80 > scoring.percent_of(role, scoring.bounds(on)), (
        "the drafting bar is a percent of what the asks that count can earn")


def test_a_section_with_every_ask_off_leaves_nothing_to_score_and_the_bounds_say_so():
    prof = p.Profile.model_validate({"scoring": {"asks": [
        {"id": "a", "section": "description", "says": "A", "points": 2}], "sections_off": ["description"]}})
    assert scoring.possible(prof) == 0 and scoring.bounds(prof) is None


def test_the_agents_are_sent_only_the_sections_that_are_on():
    prof = scored_profile(sections_off=["qualifications", "responsibilities"],
                          by_role={"analysts": {"sections_off": ["qualifications"]}})
    sent = [s["id"] for s in pv.scoring_for_agent(prof.scoring, prof.criteria)]
    assert sent == ["company", "posting", "description", "compensation"]
    assert [s[0] for s in ps.asks_by_section(prof.scoring)] == ["company", "posting", "description", "compensation"]
    # An ask on a role's own tab is sent while that tab has its part on, whatever All's switch says; it names its role.
    only_all = scored_profile(sections_off=["qualifications", "responsibilities"])
    sections = {s["id"]: s for s in pv.scoring_for_agent(only_all.scoring, only_all.criteria)}
    assert list(sections) == ["company", "posting", "description", "qualifications", "compensation"]
    assert sections["qualifications"]["asks"] == [{
        "id": "statistics", "says": "Asks for statistics", "effect": "adds points", "only_for_roles": ["analysts"]}]


def test_a_pay_tier_in_an_off_section_gives_no_points_to_the_ask():
    from app.jobsearch.fit import comp

    assert comp.pay_points(160_000, scored_profile()) == (2, "Ask over 150,000")
    assert comp.pay_points(160_000, scored_profile(sections_off=["compensation"])) == (0, "")


def test_an_answer_to_an_ask_in_an_off_section_is_refused_with_the_owners_word_for_it():
    prof = scored_profile(sections_off=["qualifications"])
    assert scoring.problems({"asks": [{"id": "degree", "answer": "met"}]}, prof) == [
        "fit_breakdown names 'degree', which the owner turned off"]
    assert scoring.problems({"asks": [{"id": "builds", "answer": "met"}]}, prof) == []


def test_the_role_page_lists_only_the_asks_that_count():
    prof = scored_profile(sections_off=["qualifications"])
    rows = scoring.view({"asks": []}, prof, "analysts")
    assert "degree" not in [r["id"] for r in rows] and "builds" in [r["id"] for r in rows]

# --- over HTTP ------------------------------------------------------------------------------------------------------


async def test_the_switches_are_saved_and_read_back_and_the_possible_points_follow_the_scoring_switch(client):
    criteria = {"company": "A public agency.", "target_roles": ROLES}
    assert (await client.put("/api/v1/profile/criteria", json={"data": criteria})).status_code == 200
    asks = {"asks": ASKS}
    assert (await client.put("/api/v1/profile/scoring", json={"data": asks, "why": "all on"})).json()["saved"] is True
    before = (await client.get("/api/v1/profile")).json()
    assert before["scoring"]["sections_off"] == [] and before["gates"]["sections_off"] == []
    off = (await client.put("/api/v1/profile/scoring", json={"data": {**asks, "sections_off": ["qualifications"]},
                                                             "why": "off"}))
    assert off.status_code == 200 and off.json()["saved"] is True
    after = (await client.get("/api/v1/profile")).json()
    assert after["scoring"]["sections_off"] == ["qualifications"]
    assert {a["id"] for a in after["scoring"]["asks"]} >= {"degree", "statistics"}, "the asks stay on the page"
    assert after["fit_possible"] == before["fit_possible"] - 2
    assert after["fit_possible_by_role"]["analysts"] == before["fit_possible_by_role"]["analysts"] - 2, (
        "All's switch does not turn off the ask on the Analysts tab")
    own = {**asks, "sections_off": ["qualifications"], "by_role": {"analysts": {"sections_off": ["qualifications"]}}}
    assert (await client.put("/api/v1/profile/scoring", json={"data": own, "why": "off for the analysts"})).json()[
        "saved"] is True
    after = (await client.get("/api/v1/profile")).json()
    assert after["scoring"]["by_role"] == {"analysts": {"sections_off": ["qualifications"]}}
    assert after["fit_possible_by_role"]["analysts"] == before["fit_possible_by_role"]["analysts"] - 3
    assert after["fit_possible_by_role"]["builders"] == before["fit_possible_by_role"]["builders"] - 2
    assert (await client.put("/api/v1/profile/gates", json={"data": PROFILE["gates"], "why": "first"})).json()["saved"]
    gates = {**PROFILE["gates"], "sections_off": ["posting", "preferred"]}
    assert (await client.put("/api/v1/profile/gates", json={"data": gates, "why": "off"})).json()["saved"] is True
    assert (await client.get("/api/v1/profile")).json()["gates"]["sections_off"] == ["posting", "preferred"]
    history = (await client.get("/api/v1/profile/gates/history")).json()["versions"]
    assert history[0]["changed"] == ["set sections off"]
    refused = await client.put("/api/v1/profile/gates", json={"data": {"sections_off": ["somewhere"]}})
    assert refused.status_code == 422 and "sections_off" in refused.json()["detail"]

