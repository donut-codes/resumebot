"""The profile as structure: each part validates, reads as sentences, turns into the gates the process compares, and is
stored as versions with a history that can be read and brought back. Synthetic data only (tests/support.py)."""

import copy

import pytest

from app import db
from app.jobsearch.profile import base as pb
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile import views as pv
from tests.support import LOCATIONS, OWNER, PROFILE, bare, load_profile

#: What a new Hard pass says with nothing in the log and no role to check again (the re-gate, docs/PRD.md 39.14).
NOTHING_TO_CHECK = "Re-checked 0 postings and 0 roles: nothing changed."


def full() -> p.Profile:
    return p.Profile.model_validate(copy.deepcopy(PROFILE))


# --- the documents -------------------------------------------------------------------------------------------------


def test_a_new_install_has_an_empty_profile_and_every_role_gets_through():
    empty = p.Profile()
    assert not empty.is_set()
    gates = pv.engine_gates(empty)
    assert gates.for_process() == {"stale_days": pv.NO_AGE_LIMIT, "comp_floor": 0, "says": {}}
    assert gates.places_gated is False and gates.excluded_employers == []


def test_a_document_that_does_not_fit_its_kind_is_refused_and_says_where():
    with pytest.raises(profile_store.ProfileError, match="other.0.kind"):
        profile_store._check("gates", {"other": [{"id": "x", "kind": "vibes"}]})
    with pytest.raises(profile_store.ProfileError, match="compensation.against"):
        profile_store._check("gates", {"compensation": {"against": "vibes"}})
    with pytest.raises(profile_store.ProfileError, match="unknown_field"):
        profile_store._check("criteria", {"unknown_field": 1})
    with pytest.raises(ValueError, match="the kinds are"):
        p.parse("wishes", {})


def test_there_is_no_fit_bar_and_a_gates_document_that_names_one_is_refused():
    """The owner took it away (docs/PRD.md 30.4): Prioritization ranks and never stops a role."""
    assert "fit" not in pg.GateSet().model_dump() and "fit" not in pg.RoleGates().model_dump()
    assert not hasattr(p, "Fit") and not hasattr(pg.GateSet(), "checks_fit")
    with pytest.raises(profile_store.ProfileError, match="fit: Extra inputs"):
        profile_store._check("gates", {"fit": {"percent": 5}})
    with pytest.raises(profile_store.ProfileError, match="by_role.builders.fit: Extra inputs"):
        profile_store._check("gates", {"by_role": {"builders": {"fit": {"percent": 5}}}})
    with pytest.raises(profile_store.ProfileError, match="sections_off"):
        profile_store._check("gates", {"sections_off": ["overall"]})


def test_ids_are_plain_slugs():
    with pytest.raises(profile_store.ProfileError):
        profile_store._check("scoring", {"flags": [{"id": "Not A Slug", "says": "x"}]})


# --- the sentences the page shows -----------------------------------------------------------------------------------


def test_each_gate_reads_as_one_plain_sentence_built_from_its_values():
    gates = full().gates
    where = ", ".join(LOCATIONS)
    assert pg.location_says(gates.location) == f"Work must be remote, hybrid (in {where}) or in person (in {where})."
    assert pg.compensation_says(gates.compensation) == "Pay must be at least $95,000 a year, on the estimated ask."
    assert pg.gate_says(gates.other[0]) == "Travel of 20% or more."
    assert [line["id"] for line in pg.gate_lines(gates)] == ["location", "compensation", "travel"]


# --- the outline: a gate belongs to a part of a posting ---------------------------------------------------------


def test_the_outline_is_the_seven_parts_of_a_posting_in_order_and_benefits_and_compensation_is_one_name():
    assert pb.SECTIONS == (
        ("company", "Company"), ("posting", "Job posting"), ("description", "Description"),
        ("responsibilities", "Responsibilities"), ("qualifications", "Qualifications"), ("preferred", "Preferred"),
        ("compensation", "Benefits / Compensation"))
    assert pb.SECTION_LABEL["compensation"] == "Benefits / Compensation", "the stored key is the same as ever"
    assert pb.SECTION_IDS == tuple(s for s, _ in pb.SECTIONS)
    bonus = {"id": "pay", "section": "compensation", "says": "A bonus", "points": 1}
    prof = p.Profile.model_validate({"scoring": {"asks": [bonus]}})
    labels = {section: label for section, label, _ in ps.asks_by_section(prof.scoring)}
    assert labels["compensation"] == "Benefits / Compensation"
    agent = {s["id"]: s["section"] for s in pv.scoring_for_agent(prof.scoring)}
    assert agent["compensation"] == "Benefits / Compensation", "what the agents read says it the same way"


def test_a_gate_stored_without_a_part_loads_into_its_kinds_home_never_by_its_words():
    """A profile saved before gates had parts: a sentence loads into Description whatever it says (a travel sentence is
    not guessed to be about the responsibilities, a clearance sentence not about the qualifications); an employer rule
    is about the company and a posting's age about the posting, so those two go there. A gate that names a part keeps
    it."""
    gates = pg.GateSet.model_validate({"other": [
        {"id": "travel", "kind": "rule", "text": "Travel of 20% or more."},
        {"id": "clearance", "kind": "rule", "text": "The role requires a security clearance."},
        {"id": "no-pe", "kind": "employer_fact", "fact": "ownership", "op": "!=", "value": "pe"},
        {"id": "age", "kind": "posting_age", "days": 45},
        {"id": "placed", "kind": "rule", "text": "Relocation is required.", "section": "posting"},
        {"id": "blank", "kind": "rule", "text": "Never placed.", "section": ""},
    ]})
    assert [(g.id, g.section) for g in gates.other] == [
        ("travel", "description"), ("clearance", "description"), ("no-pe", "company"), ("age", "posting"),
        ("placed", "posting"), ("blank", "description")]
    assert pg.GATE_HOME == {"rule": "description", "employer_fact": "company", "posting_age": "posting",
                              "job_fact": "posting"}
    stored = gates.model_dump(mode="json")
    assert all(g["section"] for g in stored["other"]), "saved again, every gate says where it is"
    assert pg.GateSet.model_validate(stored) == gates, "and reading that gives the same gates"
    assert pg.Gate(id="plain", text="A sentence").section == "description"


def test_a_gate_in_a_part_that_is_not_one_of_the_seven_is_refused_and_says_where():
    with pytest.raises(profile_store.ProfileError, match="other.0.section"):
        profile_store._check("gates", {"other": [{"id": "x", "kind": "rule", "text": "x", "section": "elsewhere"}]})
    for section in pb.SECTION_IDS:
        doc = profile_store._check("gates", {"other": [{"id": "x", "text": "x", "section": section}]})
        assert doc.other[0].section == section


def test_the_part_a_gate_is_in_changes_nothing_about_how_it_is_checked():
    placed = p.Profile.model_validate({"gates": {"other": [
        {"id": "age", "kind": "posting_age", "days": 45, "section": "description"},
        {"id": "travel", "kind": "rule", "text": "Travel of 20% or more.", "section": "responsibilities"}]}})
    plain = p.Profile.model_validate({"gates": {"other": [
        {"id": "age", "kind": "posting_age", "days": 45},
        {"id": "travel", "kind": "rule", "text": "Travel of 20% or more."}]}})
    assert pv.engine_gates(placed).for_process() == pv.engine_gates(plain).for_process()
    assert pg.gate_lines(placed.gates) == pg.gate_lines(plain.gates)
    assert pv.for_scout(placed)["hard_gates"] == pv.for_scout(plain)["hard_gates"]


def test_the_work_location_sentence_names_the_ways_of_working_that_are_on_and_where_they_are_open():
    loc = pg.Location(remote=pg.Way(on=True, locations=["Canada", "United States"]), hybrid=pg.Way(on=True),
                     in_person=pg.Way(on=False))
    assert pg.location_says(loc) == "Work must be remote (in Canada, United States) or hybrid (anywhere)."
    assert pg.location_says(pg.Location()) == "No way of working is allowed yet."


def test_the_pay_sentence_says_what_the_amount_is_compared_with():
    for against, words in (("minimum", "the posted minimum"), ("maximum", "the posted maximum"),
                           ("ask", "the estimated ask")):
        assert pg.compensation_says(pg.Compensation(amount=120_000, against=against)) == (
            f"Pay must be at least $120,000 a year, on {words}.")
    assert pg.compensation_says(pg.Compensation()) == "Pay: no amount set yet."


def test_an_ask_reads_with_its_points():
    ask = ps.Ask(id="gaps", section="qualifications", says="Each unproven required item", points=-1, per_item=True,
                limit=-3)
    assert ps.ask_says(ask) == "Each unproven required item: -1 each, down to -3"
    assert ps.ask_says(ps.Ask(id="up", section="posting", says="A promotion", points=1)) == "A promotion: +1"
    assert ps.ask_says(ps.Ask(id="pe", says="Owned by private equity")) == "Owned by private equity (noted)"


def test_the_app_can_only_check_an_ask_it_can_decide_itself():
    with pytest.raises(ValueError, match="can only check an ask it can decide itself"):
        ps.Ask(id="x", says="Feels right", points=1, checked_by="app")
    assert ps.Ask(id="x", says="Old", points=-1, days=30, checked_by="app").mechanism() == "age"
    assert ps.Ask(id="y", says="Small", points=1, fact="employees", op="<", value=1000).mechanism() == "employer"
    assert ps.Ask(id="z", says="Plain", points=1).mechanism() is None


# --- the gates the process compares --------------------------------------------------------------------------------


def test_the_owners_gates_become_what_the_process_compares():
    gates = pv.engine_gates(full())
    assert gates.comp_floor == 95_000
    assert gates.stale_days == pv.NO_AGE_LIMIT, "an ask about age is scored where fit is added up, not by the process"
    assert gates.excluded_employers == ["acme holdings", "acme", "globex"]
    assert gates.places_gated and gates.remote_ok
    places = {"oakfield", "riverton", "maple heights", "springdale", "lakeside, il"}
    assert set(gates.hybrid_places) == places == set(gates.onsite_places)
    assert gates.remote_countries == []
    assert gates.comp_against == "ask"
    assert gates.says["location"] == pg.location_says(full().gates.location), "the reason a role is given"
    assert gates.says["compensation"].startswith("Pay must be at least $95,000")


def test_a_gate_in_a_section_that_is_off_lets_its_roles_through():
    prof = full()
    prof.gates.sections_off = ["posting", "compensation"]
    gates = pv.engine_gates(prof)
    assert gates.comp_floor == 0 and not gates.places_gated
    assert gates.says == {}
    assert pv.pay_floor(prof) == 0 and pv.pay_floor(full()) == 95_000


def test_a_posting_age_gate_screens_out_instead_of_taking_a_point():
    prof = full()
    prof.gates.other.append(pg.Gate(id="age", kind="posting_age", days=45))
    assert pv.engine_gates(prof).stale_days == 45
    assert pv.engine_gates(prof).says["age"] == "Postings older than 45 days are gated out."


def test_each_way_of_working_is_its_own_switch_in_what_the_process_compares():
    prof = full()
    prof.gates.location.remote.locations = ["United States"]
    prof.gates.location.hybrid.on = False
    gates = pv.engine_gates(prof)
    assert gates.remote_countries == ["United States"] and gates.hybrid_ok is False and gates.onsite_ok is True


# --- the scout's view -----------------------------------------------------------------------------------------------


def test_the_scout_reads_only_what_is_set_and_who_checks_each_gate():
    view = pv.for_scout(full())
    assert "places" not in view, "where the owner works is the work location gate now"
    assert [g["id"] for g in view["hard_gates"]] == ["location", "compensation", "travel"]
    who = {g["id"]: g["who_checks"] for g in view["hard_gates"]}
    assert who["location"].startswith("the app:") and who["travel"].startswith("you: answer pass, fail or unknown")
    sections = {s["id"]: s for s in view["scoring"]["sections"]}
    assert list(sections) == ["posting", "responsibilities"], (
        "the sections with asks, in the order a posting reads; Responsibilities holds the built-in ask, since a role "
        "has them written")
    assert sections["posting"]["section"] == "Job posting"
    asks = {a["id"]: a for a in sections["posting"]["asks"]}
    assert asks["promotion"] == {"id": "promotion", "says": "A promotion", "effect": "adds points"}, (
        "no points: the agent never adds up, and no group: each ask counts on its own")
    assert asks["aging"]["effect"] == "noted only" and "leave it out" in asks["aging"]["who_checks"]
    off = full()
    off.gates.sections_off = ["compensation"]
    assert "compensation" not in [g["id"] for g in pv.for_scout(off)["hard_gates"]], "a gate that is off is not sent"
    off.gates.sections_off = ["compensation", "description"]  # the sentence is in Description
    assert [g["id"] for g in pv.for_scout(off)["hard_gates"]] == ["location"]


ROLE_PARTS_IN_ORDER = ["id", "name", "titles", "description", "responsibilities", "qualifications", "preferred",
                       "compensation"]


def two_roles() -> p.Profile:
    """The made-up profile with a second target role."""
    prof = full()
    prof.criteria.target_roles.append(p.TargetRole(
        id="analysts", name="Analysts", titles=["Data Analyst", "Reporting Lead"],
        description="Turns numbers into decisions.", responsibilities="Builds the monthly report."))
    return prof


def test_a_reader_is_sent_the_company_and_every_part_of_each_target_role():
    view = pv.for_scout(two_roles())
    looks = view["what_to_look_for"]
    assert list(looks) == ["company", "target_roles"]
    assert looks["company"].startswith("A mid-sized public agency")
    builders, analysts = looks["target_roles"]
    assert list(builders) == ROLE_PARTS_IN_ORDER, "every part, in the order a posting reads"
    assert (builders["id"], builders["name"], builders["titles"]) == ("builders", "Builders", ["Director of Programs"])
    assert builders["description"].startswith("Builds and runs programs")
    assert builders["responsibilities"].startswith("Plans the year")
    assert builders["qualifications"].startswith("A degree or the same")
    assert builders["preferred"] == "Grant writing; a second language."
    assert builders["compensation"].startswith("A salary over the floor")
    assert list(analysts) == ["id", "name", "titles", "description", "responsibilities"], "an empty part is left out"
    assert analysts["description"] == "Turns numbers into decisions."
    assert "Builds the monthly report" not in str(builders), "each role has its own parts: no blend"
    assert not {"the_work", "title_groups", "job_titles", "search", "positioning", "keywords_for_scoring",
                "employers_to_check"} & set(view), "the parts are under one key, and where to look is not the reader's"
    assert "tier" not in str(looks)


def test_a_part_the_owner_has_not_written_is_left_out_of_what_a_reader_is_sent():
    prof = full()
    prof.criteria.company, prof.criteria.target_roles[0].preferred = "  ", ""
    prof.criteria.target_roles.append(p.TargetRole(id="empty-tab"))
    looks = pv.for_scout(prof)["what_to_look_for"]
    assert list(looks) == ["target_roles"], "no company"
    [role] = looks["target_roles"]
    assert list(role) == ["id", "name", "titles", "description", "responsibilities", "qualifications", "compensation"]
    assert "what_to_look_for" not in pv.for_scout(p.Profile()), "nothing set, nothing sent"


def test_a_title_is_judged_on_the_company_and_each_roles_name_titles_and_description_alone():
    prof = two_roles()
    titles = pv.for_titles(prof)
    assert list(titles) == ["company", "target_roles"]
    assert titles["company"].startswith("A mid-sized public agency")
    builders, analysts = titles["target_roles"]
    assert list(builders) == ["id", "name", "titles", "description"]
    assert builders["id"] == "builders" and builders["titles"] == ["Director of Programs"]
    assert builders["description"].startswith("Builds and runs programs")
    assert analysts == {"id": "analysts", "name": "Analysts", "titles": ["Data Analyst", "Reporting Lead"],
                        "description": "Turns numbers into decisions."}
    flat = str(titles)
    for later in ("Plans the year", "A degree or the same", "Grant writing", "A salary over the floor",
                  "Builds the monthly report"):
        assert later not in flat, "what a title cannot show is for the readers"
    assert pv.for_titles(p.Profile()) == {}, "nothing set, nothing sent"
    only_company = p.Profile(criteria=p.Criteria(company="A small agency."))
    assert pv.for_titles(only_company) == {"company": "A small agency."}
    texts_only = p.Criteria(target_roles=[p.TargetRole(id="x", responsibilities="Runs the books.")])
    assert pv.for_titles(p.Profile(criteria=texts_only)) == {}, (
        "a role with no name, titles or description says nothing")
    assert pv.look_for(texts_only)["target_roles"][0]["responsibilities"] == "Runs the books."


def test_a_target_role_defaults_to_empty_trims_its_words_and_holds_any_amount_of_text():
    role = p.TargetRole(id="x")
    assert (role.name, role.titles) == ("", []) and all(getattr(role, f) == "" for f in p.ROLE_TEXT)
    role = p.TargetRole(id="x", name="  Program   lead ", titles=[" Director ", "", "Head  of Programs"],
                        compensation="\nHealth cover.\n")
    assert role.name == "Program lead" and role.titles == ["Director", "Head of Programs"]
    assert role.compensation == "Health cover."
    for part in p.ROLE_TEXT:  # what the owner writes has no limit on its length (the owner, 2026-10-14)
        assert getattr(p.TargetRole(id="x", **{part: "x" * 20_000}), part) == "x" * 20_000
    assert p.TargetRole(id="x", name="N" * 500).name == "N" * 500
    assert p.Criteria(company="x" * 20_000).company == "x" * 20_000
    assert p.Criteria(company="  A small agency.  ").company == "A small agency."
    with pytest.raises(ValueError, match="at most 10 example titles; it has 11"):
        p.TargetRole(id="x", titles=[f"Title {i}" for i in range(11)])
    with pytest.raises(ValueError, match="id"):
        p.TargetRole(id="Not A Slug")
    assert profile_store._check("criteria", {"target_roles": [{"id": "x", "preferred": "x" * 20_000}]})
    with pytest.raises(ValueError):
        p.TargetRole(id="x", tier="primary")  # a field that is gone is refused, not carried


def test_target_role_ids_are_unique_and_there_are_at_most_twenty_roles():
    with pytest.raises(ValueError, match="target role ids must be unique; repeated: a"):
        p.Criteria(target_roles=[p.TargetRole(id="a"), p.TargetRole(id="b"), p.TargetRole(id="a")])
    with pytest.raises(profile_store.ProfileError, match="unique"):
        profile_store._check("criteria", {"target_roles": [{"id": "a"}, {"id": "a"}]})
    ids = [{"id": f"r{i}"} for i in range(p.MAX_TARGET_ROLES)]
    assert len(p.Criteria(target_roles=ids).target_roles) == 20
    with pytest.raises(ValueError, match="at most 20 roles"):
        p.Criteria(target_roles=[*ids, {"id": "extra"}])
    assert p.Criteria(target_roles=ids).role("r3").id == "r3" and p.Criteria().role("r3") is None


def test_a_search_starts_from_a_company_or_a_target_role_that_says_something():
    assert not p.Profile().is_set()
    assert p.Profile(criteria=p.Criteria(company="A small agency.")).is_set()
    for said in ({"name": "Lead"}, {"titles": ["Director"]}, {"description": "Runs programs."}):
        assert p.Profile(criteria=p.Criteria(target_roles=[p.TargetRole(id="x", **said)])).is_set(), said
    texts_only = p.Criteria(target_roles=[p.TargetRole(id="x", preferred="Grants.")])
    assert not p.Profile(criteria=texts_only).is_set(), "the parts of a posting alone are not a role to look for"
    assert not p.Profile(criteria=p.Criteria(search_plan=["One"])).is_set(), "phrases are not what a search starts from"
    assert full().is_set()


def test_the_search_plan_holds_at_most_eight_and_is_kept_as_saved():
    assert p.Criteria(search_plan=["  One ", ""]).search_plan == ["One"], "blanks go, words are trimmed"
    with pytest.raises(ValueError, match="at most 8"):
        p.Criteria(search_plan=[f"phrase {i}" for i in range(9)])


def test_a_typed_search_uses_each_target_roles_example_titles_one_role_at_a_time():
    c = p.Criteria(search_plan=["no longer read"], target_roles=[
        p.TargetRole(id="a", name="Alpha", titles=["A1", "A2", "A3"]),
        p.TargetRole(id="b", name="Beta"),
        p.TargetRole(id="c", titles=["B1", "a2"]),
        p.TargetRole(id="d"),
    ])
    assert c.search_phrases() == ["A1", "Beta", "B1", "A2", "A3"], (
        "every role is searched before any twice; a role with no titles by its name; each phrase once")
    big = p.Criteria(target_roles=[p.TargetRole(id=f"r{i}", titles=[f"R{i} T{j}" for j in range(10)])
                                   for i in range(5)])
    assert len(big.search_phrases()) == 50, "every example title is searched, however many there are"
    assert [x.split()[1] for x in big.search_phrases()[:5]] == ["T0"] * 5
    assert p.Criteria().search_phrases() == []


# --- the store -----------------------------------------------------------------------------------------------------


async def test_a_save_adds_a_version_and_an_unchanged_save_adds_none(js):
    async with db.session_factory()() as session:
        assert await profile_store.save(session, OWNER, "gates", PROFILE["gates"], by="owner", why="first") == 1
        assert await profile_store.save(session, OWNER, "gates", PROFILE["gates"], by="owner") is None
        changed = copy.deepcopy(PROFILE["gates"])
        changed["compensation"]["amount"] = 99_000
        changed["other"].append({"id": "age", "kind": "posting_age", "days": 60})
        assert await profile_store.save(session, OWNER, "gates", changed, by="owner", why="raised the amount") == 2
        await session.commit()
        history = await profile_store.history(session, OWNER, "gates")
        current = await profile_store.current(session, OWNER)
    assert [h["version"] for h in history] == [2, 1]
    assert history[0]["why"] == "raised the amount" and history[0]["saved_by"] == "owner"
    assert history[0]["changed"] == ["changed compensation", "added other: age"]
    assert history[1]["changed"] == ["set compensation", "set employer limit", "set employer names", "set location",
                                     "added other: travel"]
    assert current.gates.compensation.amount == 99_000


async def test_an_earlier_version_can_be_read_and_brought_back(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await profile_store.save(session, OWNER, "criteria", {"company": "Other"}, by="owner")
        assert (await profile_store.version(session, OWNER, "criteria", 1))["data"]["company"].startswith("A mid-sized")
        assert await profile_store.restore(session, OWNER, "criteria", 1, by="owner") == 3
        current = await profile_store.current(session, OWNER)
        history = await profile_store.history(session, OWNER, "criteria")
        with pytest.raises(profile_store.ProfileError, match="no version 9"):
            await profile_store.version(session, OWNER, "criteria", 9)
    assert current.criteria.company.startswith("A mid-sized")
    assert history[0]["why"] == "brought back version 1"


# --- over HTTP -----------------------------------------------------------------------------------------------------


async def test_the_page_reads_the_profile_saves_a_part_and_reads_its_history(client):
    empty = (await client.get("/api/v1/profile")).json()
    assert empty["is_set"] is False and empty["versions"] == {"criteria": 0, "gates": 0, "scoring": 0, "settings": 0}
    saved = await client.put("/api/v1/profile/gates", json={"data": PROFILE["gates"], "why": "my first gates"})
    assert saved.status_code == 200 and bare(saved.json()) == {"saved": True, "version": 1, "said": NOTHING_TO_CHECK}
    await client.put("/api/v1/profile/criteria", json={"data": PROFILE["criteria"]})
    body = (await client.get("/api/v1/profile")).json()
    assert body["is_set"] is True and body["versions"]["gates"] == 1
    assert body["says"]["gates"]["compensation"] == "Pay must be at least $95,000 a year, on the estimated ask."
    assert body["says"]["gates"]["other"] == {"travel": "Travel of 20% or more."}
    assert body["says"]["gates"]["location"].startswith("Work must be remote, hybrid (in Oakfield, Riverton")
    history = (await client.get("/api/v1/profile/gates/history")).json()["versions"]
    assert history[0]["why"] == "my first gates"
    bad = await client.put("/api/v1/profile/gates", json={"data": {"other": [{"id": "x", "kind": "vibes"}]}})
    assert bad.status_code == 422 and "don't fit" in bad.json()["detail"]
    assert (await client.get("/api/v1/profile/wishes/history")).status_code == 404
    restored = await client.post("/api/v1/profile/gates/versions/1/restore")
    assert bare(restored.json()) == {"restored": 1, "version": None}


async def test_the_page_is_given_no_fit_bar_and_a_save_that_sends_one_is_stored_as_nothing_and_refused(client):
    body = (await client.get("/api/v1/profile")).json()
    assert "fit" not in body["gates"] and "fit" not in body["says"]["gates"]
    refused = await client.put("/api/v1/profile/gates", json={"data": {"fit": {"percent": 5}}, "why": "old page"})
    assert refused.status_code == 422 and "fit" in refused.json()["detail"]
    assert (await client.get("/api/v1/profile")).json()["versions"]["gates"] == 0, "nothing was stored"
    tab = {"builders": {"compensation": {"amount": 100_000}}}
    assert (await client.put("/api/v1/profile/gates", json={"data": {"by_role": tab}})).status_code == 200
    says = (await client.get("/api/v1/profile")).json()["says"]["gates"]
    assert set(says["by_role"]["builders"]) == {"location", "compensation"}


async def test_gates_saved_without_a_part_are_read_with_one_and_saved_with_it(client):
    """The page is sent every gate in a part of a posting: an older save (no part on any gate) reads into its kind's
    home, and a save that names a part keeps it, in the stored version and in the history."""
    travel = {"id": "travel", "kind": "rule", "text": "Travel of 20% or more."}
    no_pe = {"id": "no-pe", "kind": "employer_fact", "fact": "ownership", "op": "!=", "value": "pe"}
    await client.put("/api/v1/profile/gates", json={"data": {"other": [travel, no_pe]}})
    body = (await client.get("/api/v1/profile")).json()
    parts = [(g["id"], g["section"]) for g in body["gates"]["other"]]
    assert parts == [("travel", "description"), ("no-pe", "company")]
    saved = await client.put("/api/v1/profile/gates", json={"data": {"other": [
        {**travel, "section": "responsibilities"}, {**no_pe, "section": "company"}]}})
    assert saved.json()["version"] == 2
    body = (await client.get("/api/v1/profile")).json()
    parts = [(g["id"], g["section"]) for g in body["gates"]["other"]]
    assert parts == [("travel", "responsibilities"), ("no-pe", "company")]
    stored = (await client.get("/api/v1/profile/gates/versions/2")).json()["data"]["other"]
    assert [g["section"] for g in stored] == ["responsibilities", "company"]
    assert (await client.put("/api/v1/profile/gates", json={"data": {"other": [
        {"id": "x", "kind": "rule", "text": "x", "section": "elsewhere"}]}})).status_code == 422
    scoring = await client.put("/api/v1/profile/scoring", json={"data": {"asks": [
        {"id": "pay", "section": "compensation", "says": "A bonus", "points": 1}]}})
    assert scoring.status_code == 200, "the stored key of Benefits / Compensation is still `compensation`"


async def test_a_stored_version_with_no_part_on_its_gates_reads_with_one_and_reading_writes_nothing(js):
    from app.jobsearch.models import ProfileVersion

    raw = {"other": [
        {"id": "travel", "kind": "rule", "text": "Travel of 20% or more."},
        {"id": "no-pe", "kind": "employer_fact", "fact": "ownership", "op": "!=", "value": "pe"}]}
    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner=OWNER, kind="gates", version=1, data=copy.deepcopy(raw), saved_by="import",
                                   why="before gates had parts"))
        await session.flush()
        prof = await profile_store.current(session, OWNER)
        shown = await profile_store.version(session, OWNER, "gates", 1)
        counts = await profile_store.counts(session, OWNER)
    assert [(g.id, g.section) for g in prof.gates.other] == [("travel", "description"), ("no-pe", "company")]
    assert shown["data"] == raw, "a version is never changed: it is read as it was stored"
    assert counts["gates"] == 1, "reading never writes"


def _banned(pattern: str) -> dict:
    return {"banned": [{"id": f"no-{pattern}", "phrase": pattern, "why": "made up"}]}


async def test_a_save_made_from_an_out_of_date_copy_is_refused_and_stores_nothing(client):
    """Found when a My Resume page left open on an old version saved over newer versions (2026-10-13): a save that names
    the version the page loaded is refused when a newer one is in force, for every kind, whole."""
    for kind in ("writing", "resume", "about", "criteria", "gates", "scoring", "settings"):
        first = await client.put(f"/api/v1/profile/{kind}", json={"data": {}, "why": "one"})
        # A new Hard pass says in a line what it changed (the re-gate, docs/PRD.md 39.14); no other part says anything
        # (a new What to look for judges nothing again, 39.16).
        expected = {"saved": True, "version": 1, **({"said": NOTHING_TO_CHECK} if kind == "gates" else {})}
        assert first.status_code == 200 and bare(first.json()) == expected, kind
    await client.put("/api/v1/profile/writing", json={"data": _banned("synergy")})
    await client.put("/api/v1/profile/writing", json={"data": _banned("leverage")})
    stale = await client.put("/api/v1/profile/writing", json={"data": {}, "why": "old page", "base_version": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"] == profile_store.STALE_SAVE
    assert stale.json()["detail"].startswith("This page is out of date")
    for kind in ("resume", "about", "criteria", "gates", "scoring", "settings"):
        old = await client.put(f"/api/v1/profile/{kind}", json={"data": {}, "base_version": 0})
        assert old.status_code == 409, kind
    history = (await client.get("/api/v1/profile/writing/history")).json()["versions"]
    assert [h["version"] for h in history] == [3, 2, 1]
    assert (await client.get("/api/v1/documents")).json()["writing"]["banned"][0]["phrase"] == "leverage"


async def test_a_save_made_from_the_current_copy_is_stored_and_the_next_names_the_new_version(client):
    first = await client.put("/api/v1/profile/gates", json={"data": PROFILE["gates"], "base_version": 0})
    assert first.status_code == 200 and bare(first.json()) == {"saved": True, "version": 1, "said": NOTHING_TO_CHECK}
    changed = copy.deepcopy(PROFILE["gates"])
    changed["compensation"]["amount"] = 99_000
    second = await client.put("/api/v1/profile/gates", json={"data": changed, "base_version": 1})
    assert second.status_code == 200 and bare(second.json()) == {"saved": True, "version": 2, "said": NOTHING_TO_CHECK}
    again = await client.put("/api/v1/profile/gates", json={"data": changed, "base_version": 2})
    assert again.status_code == 200 and bare(again.json()) == {"saved": False, "version": None}


async def test_a_save_that_names_no_version_is_stored_as_it_always_was(client):
    for pattern in ("a", "b", "c"):
        await client.put("/api/v1/profile/writing", json={"data": _banned(pattern)})
    unnamed = await client.put("/api/v1/profile/writing", json={"data": _banned("d"), "base_version": None})
    assert unnamed.status_code == 200 and bare(unnamed.json()) == {"saved": True, "version": 4}
    assert (await client.put("/api/v1/profile/writing", json={"data": {}, "base_version": -1})).status_code == 422


# --- groups and suggestions over HTTP (docs/PRD.md 30.9) ----------------------------------------------

GROUPED = {
    "asks": [{"id": "ask-a", "section": "posting", "says": "A promotion", "points": 1},
             {"id": "ask-b", "section": "posting", "says": "A lateral move", "points": 0}],
    "groups": [
        {"id": "list-a", "kind": "best", "section": "responsibilities", "roles": [], "points": 0,
         "members": [{"id": "lvl-a", "says": "The role is mostly building", "points": 4},
                     {"id": "lvl-b", "says": "The role builds in part", "points": 3}]},
        {"id": "box-a", "kind": "any", "section": "company", "roles": [], "points": 2,
         "members": [{"id": "part-a", "says": "The company is small"},
                     {"id": "part-b", "says": "The company is young"}]}],
    "suggestions": [{"id": "sug-a", "kind": "best", "asks": ["ask-a", "ask-b"], "points": 0,
                     "why": "These cannot both be true of one posting."}],
}


async def test_a_document_with_groups_survives_a_save_a_read_a_restore_and_a_second_save(client):
    first = await client.put("/api/v1/profile/scoring", json={"data": GROUPED, "why": "my first rows"})
    assert first.status_code == 200 and first.json()["saved"] is True and first.json()["version"] == 1
    read = (await client.get("/api/v1/profile")).json()["scoring"]
    assert [g["id"] for g in read["groups"]] == ["list-a", "box-a"]
    assert [s["id"] for s in read["suggestions"]] == ["sug-a"]
    assert [m["id"] for m in read["groups"][0]["members"]] == ["lvl-a", "lvl-b"], "the order of the levels is kept"
    again = await client.put("/api/v1/profile/scoring", json={"data": read, "base_version": 1})
    assert again.status_code == 200 and bare(again.json()) == {"saved": False, "version": None}, (
        "what is read is stored")
    restored = await client.post("/api/v1/profile/scoring/versions/1/restore")
    assert restored.status_code == 200 and bare(restored.json()) == {"restored": 1, "version": None}
    assert (await client.put("/api/v1/profile/scoring", json={"data": read})).json()["saved"] is False
    assert (await client.get("/api/v1/profile")).json()["scoring"]["groups"] == read["groups"]


async def test_the_history_names_a_row_and_a_suggestion_the_owner_added_and_removed(client):
    await client.put("/api/v1/profile/scoring", json={"data": {"asks": GROUPED["asks"]}})
    await client.put("/api/v1/profile/scoring", json={"data": GROUPED, "why": "added rows"})
    await client.put("/api/v1/profile/scoring", json={"data": {**GROUPED, "suggestions": []}, "why": "used the idea"})
    history = (await client.get("/api/v1/profile/scoring/history")).json()["versions"]
    assert history[1]["changed"] == ["added groups: list-a", "added groups: box-a", "added suggestions: sug-a"]
    assert history[0]["changed"] == ["removed suggestions: sug-a"]


async def test_a_row_the_model_cannot_score_is_refused_in_words_and_stores_nothing(client):
    lonely = {"groups": [{**GROUPED["groups"][0], "members": GROUPED["groups"][0]["members"][:1]}]}
    refused = await client.put("/api/v1/profile/scoring", json={"data": lonely})
    assert refused.status_code == 422
    assert "the scoring don't fit: groups.0.members" in refused.json()["detail"]
    assert "a first-match row needs at least two statements" in refused.json()["detail"]
    assert (await client.get("/api/v1/profile")).json()["versions"]["scoring"] == 0


# --- what agents are sent that the page shows under Also sent to agents (docs/PRD.md 30.11) ---------------------

LONG = "word " * 4000  # 20,000 characters


async def _settings_with(session, **fields) -> None:
    current = (await profile_store.current(session, OWNER)).settings.model_dump(mode="json")
    await profile_store.save(session, OWNER, "settings", {**current, **fields}, by="owner")


async def test_a_settings_save_keeps_a_rule_and_a_note_of_any_length(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await _settings_with(session, rules=[{"id": "r1", "area": "search", "says": LONG}],
                             notes=[{"id": "n1", "text": LONG, "source": ""}])
        await session.commit()
        got = (await profile_store.current(session, OWNER)).settings
    assert got.rules[0].says == LONG and got.notes[0].text == LONG and len(LONG) == 20_000


async def test_an_edited_rule_reaches_the_scout_and_a_removed_one_does_not(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await _settings_with(session, rules=[{"id": "r1", "area": "search", "says": "Made-up rule, first words."}])
        assert pv.for_scout(await profile_store.current(session, OWNER))["rules"] == ["Made-up rule, first words."]
        await _settings_with(session, rules=[{"id": "r1", "area": "search", "says": "Made-up rule, new words."}])
        assert pv.for_scout(await profile_store.current(session, OWNER))["rules"] == ["Made-up rule, new words."]
        await _settings_with(session, rules=[])
        assert "rules" not in pv.for_scout(await profile_store.current(session, OWNER))


async def test_a_note_in_each_document_is_sent_as_not_placed_yet(js):
    notes = {kind: [{"id": f"{kind}-note", "text": f"A made-up {kind} note.", "source": ""}]
             for kind in ("criteria", "gates", "scoring", "settings")}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        for kind, held in notes.items():
            current = getattr(await profile_store.current(session, OWNER), kind).model_dump(mode="json")
            await profile_store.save(session, OWNER, kind, {**current, "notes": held}, by="owner")
        sent = pv.for_scout(await profile_store.current(session, OWNER))["not_placed_yet"]
    assert sorted(sent) == sorted(f"A made-up {kind} note." for kind in notes)


async def test_an_edited_positioning_reaches_the_ranker(js):
    from app.jobsearch.research import rank
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        current = (await profile_store.current(session, OWNER)).criteria.model_dump(mode="json")
        await profile_store.save(session, OWNER, "criteria", {**current, "positioning": "A made-up new positioning."},
                                 by="owner")
        text = rank.render_profile(await profile_store.current(session, OWNER))
    assert "Positioning: A made-up new positioning." in text


async def test_only_search_screening_and_packets_rules_are_handed_to_agents(js):
    """The page says of the apply and email rules only that they are kept: nothing reads them (PRD 30.11). A new reader
    of a rule's area fails this, the day the page's label and the PRD line are to be revisited."""
    import re
    from pathlib import Path
    root = Path(p.__file__).parents[1]
    readers = sorted(
        (str(path.relative_to(root)).replace("\\", "/"), sorted(set(re.findall(r'"(\w+)"', line)) - {"rules"}))
        for path in root.rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"\b(?:r|rule)\.area\b", line))
    assert readers == [("profile/views.py", ["screening", "search"]), ("writing/packet_writer.py", ["packets"]),
                       ("writing/packet_writer.py", ["packets"])]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await _settings_with(session, rules=[{"id": f"r-{a}", "area": a, "says": f"Made-up {a} rule."}
                                             for a in ("search", "screening", "packets", "apply", "email")])
        sent = pv.for_scout(await profile_store.current(session, OWNER))["rules"]
    assert sent == ["Made-up search rule.", "Made-up screening rule."]


async def test_a_page_save_of_rules_keeps_every_missions_number_and_a_missions_save_keeps_every_rule(js, client):
    from app.jobsearch.missions import numbers
    rules = [{"id": "r1", "area": "search", "says": "Made-up rule."}]
    notes = [{"id": "n1", "text": "A made-up note.", "source": "old"}]
    first = await client.put("/api/v1/profile/settings", json={
        "data": {**PROFILE["settings"], "rules": rules, "notes": notes}})
    assert first.status_code == 200
    page = (await client.get("/api/v1/profile")).json()
    mine = page["settings"]
    edited = {**mine, "rules": [{**rules[0], "says": "Made-up rule, edited."}]}
    base = page["versions"]["settings"]
    saved = await client.put("/api/v1/profile/settings", json={"data": edited, "base_version": base})
    assert saved.status_code == 200
    after = (await client.get("/api/v1/profile")).json()["settings"]
    assert {k: v for k, v in after.items() if k != "rules"} == {k: v for k, v in mine.items() if k != "rules"}
    assert after["rules"][0]["says"] == "Made-up rule, edited."
    # A Missions save keeps every rule and note.
    async with db.session_factory()() as session:
        assert await numbers.save(session, OWNER, {"avoid_peak": True}, by="owner") is not None
        await session.commit()
    kept = (await client.get("/api/v1/profile")).json()["settings"]
    assert kept["rules"] == after["rules"] and kept["notes"] == notes and kept["avoid_peak"] is True
    # A page still holding the copy from before is refused, and nothing it held is stored.
    stale = await client.put("/api/v1/profile/settings", json={"data": mine, "base_version": base})
    assert stale.status_code == 409
    assert (await client.get("/api/v1/profile")).json()["settings"]["rules"] == after["rules"]


async def test_a_notes_only_save_of_hard_pass_and_prioritization_moves_no_role(js, client):
    """PRD 30.11: the follow-up of a Hard pass or Prioritization save runs whatever changed; with the rest unchanged it
    moves no role (the owner knows the cost: it re-checks the log like any save of those parts)."""
    import time
    from datetime import date

    from sqlalchemy import select

    from app.jobsearch import stages
    from app.jobsearch.gating import regate
    from app.jobsearch.models import Opportunity
    from tests.builders import counted
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all([Opportunity(owner=OWNER, slug=s, company=f"Fabrikam {s}", title=f"Title {s}", origin="scout",
                                     status=status, match_key=f"fabrikam {s}|{s}", found=date.today(), comp=comp,
                                     **counted())
                         for s, status, comp in (("a", stages.FIRST_IMPRESSION, None),
                                                 ("b", stages.FIRST_IMPRESSION, 40_000),
                                                 ("c", stages.APPLIED, None))])
        await session.commit()
    # The log as the gates in force leave it: a role the re-gate would move is moved here, before the saves under test.
    async with db.session_factory()() as session:
        await regate.run(session, OWNER, cause="a made-up first check")
        await session.commit()

    async def snapshot() -> list[dict]:
        async with db.session_factory()() as session:
            rows = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER)
                                          .order_by(Opportunity.slug))).all()
            return [{c.key: getattr(r, c.key) for c in Opportunity.__table__.columns
                     if c.key not in ("updated_at", "modified_at")} for r in rows]

    before = await snapshot()
    page = (await client.get("/api/v1/profile")).json()
    note = [{"id": "n1", "text": "A made-up note.", "source": ""}]
    for kind in ("gates", "scoring"):
        started = time.perf_counter()
        done = await client.put(f"/api/v1/profile/{kind}", json={
            "data": {**page[kind], "notes": note}, "base_version": page["versions"][kind]})
        print(f"{kind} notes-only save: {time.perf_counter() - started:.2f}s")
        assert done.status_code == 200 and done.json()["saved"] is True, done.text
    assert await snapshot() == before
