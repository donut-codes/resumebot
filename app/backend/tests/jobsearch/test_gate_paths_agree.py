"""One gate check decides a role everywhere (docs/PRD.md 39.11): the gate-facts step before analysis (`gate_facts`), the
re-gate (`regate.run`) and the settle that follows a change (`settle.roles`) must stop and release the same roles.

Roles the gate-facts sweep had gated out
(on pay, on place, on a freshness dial) were returned to First Impression by the re-gate that runs at every
start (`regate.catch_up`), because the two paths did not read a role alike:

- a wide posted band whose middle is a thousand or more under the pay floor is a certain fail for the fact path
  (`posted_pay_verdict`: the ask is never above the middle, PRD 26.7), and "too wide to judge" for the one gate check;
- the re-gate read a cleansed copy whose place is only a count ("3 Locations") in place of the role's own places.

Everything here is made up."""

from datetime import UTC, datetime

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts, regate, stated_place
from app.jobsearch.measures.pay_place import Gates, pay_check
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting
from app.jobsearch.pipeline import deck
from app.jobsearch.postings import cleansed, raw_copies
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import settle
from tests.support import EXAMPLE_SYSTEM, FIVE, OWNER, POSTING_WORDS, load_profile

NOW = datetime.now(UTC)
#: Remote anywhere, or hybrid or in person in Oakfield; pay of at least 105,000 on the estimated ask.
GATES = {
    "location": {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield"]},
                 "in_person": {"on": True, "locations": ["Oakfield"]}},
    "compensation": {"amount": 105_000, "against": "ask"},
}
#: How the two paths that release a role are asked: the re-gate's stale pass, and the settle a change of research runs.
RELEASERS = ("regate", "settle")


def posting_paying(line: str) -> str:
    return POSTING_WORDS.replace("Programs Lead\n", f"Programs Lead\n{line}\n", 1)


async def make_role(session, name: str, text: str, **fields) -> Opportunity:
    role = Opportunity(**{
        "owner": OWNER, "slug": name, "company": name, "title": "Programs Lead", "origin": "scout", "fit": 3,
        "match_key": f"{name}|programs lead", "status": stages.FIRST_IMPRESSION, "found": NOW.date(),
        "url": f"https://jobs.example.test/{name}", "description": text, "source_url": f"https://jobs.example.test/{name}",
        "source_kind": "employer", "sourced_at": NOW, "location": "Remote, US", "move_type": "remote", **fields})
    session.add(role)
    await session.flush()
    return role


async def give_copy(session, role: Opportunity, *, locations: list[str], work_mode: str | None = None,
                    marks: dict | None = None) -> None:
    """The role's cleansed copy (`cleansed.copy_of`): one posting of it, its raw row and the copy of that."""
    employer = await employers.find_or_make(session, OWNER, role.company)
    session.add(JobPosting(
        owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=role.slug, company=role.company,
        title=role.title, match_key=role.match_key, first_seen=NOW, last_seen=NOW, links=[], location="x",
        url=role.url, opportunity_id=role.id))
    raw = RawPosting(owner=OWNER, source_id=employer.id, reader="legacy", kind="legacy", ext_id=role.slug,
                     fetched_at=NOW, body=b"x", body_sha256=role.slug.ljust(64, "0"), body_bytes=1, no_raw=True)
    session.add(raw)
    await session.flush()
    session.add(CleansedPosting(owner=OWNER, raw_id=raw.id, cleaner_version=1, locations=locations,
                                work_mode=work_mode, text_clean=role.description or "", marks=marks or {}))
    await session.flush()
    await raw_copies.repoint(session, OWNER)  # the pointer the code that writes copies keeps (`raw_copies._point`)


async def release_by(session, how: str, role: Opportunity) -> None:
    """Ask the path `how` to judge the role again, as it does on its own: the re-gate's stale pass, or the settle."""
    if how == "regate":
        await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass than the one in force")
    else:
        await settle.roles(session, [role], cause="The analyst researched it")
    await session.refresh(role)


@pytest.mark.parametrize("how", RELEASERS)
async def test_a_wide_band_the_fact_path_gates_on_its_middle_is_not_released_by_the_other_paths(js, how):
    """A band of $60,000 to $120,000 has its middle at $90,000, under the $105,000 floor by more than $1,000: no ask is
    above the middle, so the pay gate fails. The band is wider than the setting for "too wide to judge" (1.5 times its
    bottom), which only decides whether the middle stands in for the ask where nothing else says."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", posting_paying("Pay range: $60,000 - $120,000 per year"))
        outcome = await gate_facts.process(session, role)
        assert outcome.state == gate_facts.STOPPED and role.status == stages.GATED_OUT
        assert role.screened_out_gate_id == "compensation"
        # The fact path's own verdict is the one gate check's:
        rules = await gating.rules(session, OWNER)
        stop = gating.check(gating.role_subject(role, None), rules)
        assert stop is not None and stop.gate_id == "compensation"

        await release_by(session, how, role)

        assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "compensation", (
            f"the {how} path released a role the fact path gated on its posted pay")


@pytest.mark.parametrize("how", RELEASERS)
async def test_a_copy_that_names_only_a_count_of_places_does_not_take_the_place_of_the_roles_own(js, how):
    """The role's own places are all outside the places the owner works in; its cleansed copy (the posting with the most
    text) holds the board's count, "3 Locations", which names no place. A fact a copy does not state is the role's."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", POSTING_WORDS, location="US, OH, Riverton; US, OH, Dayton",
                               move_type="hybrid")
        await give_copy(session, role, locations=["3 Locations"])
        outcome = await gate_facts.process(session, role)
        assert outcome.state == gate_facts.STOPPED and role.screened_out_gate_id == "location"

        await release_by(session, how, role)

        assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "location", (
            f"the {how} path released a role the fact path gated on its place")


async def test_a_copy_that_names_a_place_still_takes_the_place_of_the_roles_own(js):
    """The change is only for a place that says nothing: a copy that states where the work is still wins."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", POSTING_WORDS, location="US, OH, Riverton", move_type="hybrid")
        await give_copy(session, role, locations=["Oakfield"])
        rules = await gating.rules(session, OWNER)
        copy = await cleansed.copy_of(session, OWNER, role.id)
        assert gating.check(gating.role_subject(role, None, copy), rules) is None
        own = gating.check(gating.role_subject(role, None), rules)
        assert own is not None and own.gate_id == "location"


@pytest.mark.parametrize(("low", "high", "result"), [
    (60_000, 120_000, "fail"),   # wide, middle $90,000: a certain fail (the ask is never above the middle)
    (90_000, 108_000, "fail"),   # middle $99,000, $6,000 under the floor
    (95_000, 114_000, "unknown"),  # middle $104,500: within $1,000 of the floor, left for the analysis (2026-10-19)
    (104_500, 104_500, "unknown"),  # one figure just under the floor: the round-up edge
    (104_000, 104_000, "fail"),
    (85_000, 130_000, "unknown"),  # middle $107,500, bottom under: not decided by the posting
    (105_000, 140_000, "pass"),    # bottom at the floor: no ask is below it
    (None, None, "unknown"),       # no pay posted
])
def test_the_pay_gate_on_the_estimated_ask_before_any_ask_is_worked_out_fails_only_what_the_posted_range_settles(
        low, high, result):
    gates = Gates(comp_floor=105_000, comp_against="ask")
    assert pay_check(low, high, gates)[0] == result


def test_a_role_the_ask_is_worked_out_for_is_judged_on_the_ask_whatever_the_posted_range_says():
    gates = Gates(comp_floor=105_000, comp_against="ask")
    assert pay_check(50_000, 140_000, gates, worked=108_000)[0] == "pass"
    assert pay_check(112_000, 136_000, gates, worked=101_000)[0] == "fail"


@pytest.mark.parametrize("comp", ["$60,000-$120,000", "$95,000-$114,000", "$105,000-$140,000", "$104,500", None])
async def test_the_fact_paths_check_is_the_one_gate_check_on_the_role_as_it_holds_it(js, comp):
    """`gate_facts.decide` has no pay gate of its own to disagree with the re-gate's."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        rules = await gating.rules(session, OWNER)
        role = await make_role(session, "Fabrikam", POSTING_WORDS, comp=comp)
        decided = gate_facts.decide(role, rules)
        judged = gating.check(gating.role_subject(role, None), rules)
        assert (decided.gate_id if decided else None) == (judged.gate_id if judged else None)


@pytest.mark.parametrize(("own", "stated", "gated"), [
    (("US, OH, Riverton", "hybrid"), (["Oakfield"], "hybrid"), False),  # the copy states a place the owner works in
    (("Oakfield", "hybrid"), (["US, OH, Riverton"], "hybrid"), True),  # and one the owner does not
    (("US, OH, Riverton", "hybrid"), (["Oakfield"], "remote"), False),  # the way of working the copy states
    (("US, OH, Riverton", "hybrid"), (["3 Locations"], None), True),  # a count states nothing: the role's own stands
])
async def test_the_fact_path_judges_the_cleansed_copy_the_re_gate_judges_and_agrees_with_it(js, own, stated, gated):
    """The fact path read the role's own facts only, so a role whose copy stated another place or way of working was
    gated by one path and released by the other (the five of the 164 that were left). Both read the same subject."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", POSTING_WORDS, location=own[0], move_type=own[1])
        await give_copy(session, role, locations=stated[0], work_mode=stated[1])
        outcome = await gate_facts.process(session, role)
        assert (outcome.state == gate_facts.STOPPED) is gated

        report = await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
        await session.refresh(role)

        assert (role.status == stages.GATED_OUT) is gated, "the re-gate changed what the fact path decided"
        assert report.roles_returned == 0 and report.roles_stopped == 0


# --- one place: every reader of the work location gate reads the subject the re-gate reads ----------------------------
# Found when reviewing roles: `stated_place` read the role's own columns while the
# re-gate and the settle read the cleansed copy's stated facts, and `decide` ran only when a source read changed the
# role's own place.

#: The posting's own words for a way of working, as the gate-facts step stores them (code, a line it rests on).
HYBRID_IN_WORDS = {"value": "hybrid", "quote": "This role is hybrid, three days a week in the office.", "by": "code"}


def tag_mark(source: str) -> dict:
    """The copy's way of working as the cleaner marks it: the job system's tag, or the posting's own words."""
    return {"work_mode": {"value": "remote", "source": source, "quote": "remote"}}


@pytest.mark.parametrize(("fact", "mark", "gated"), [
    (HYBRID_IN_WORDS, tag_mark("listing"), True),   # the posting says hybrid in its own words: a tag does not lift it
    ({**HYBRID_IN_WORDS, "by": "haiku"}, tag_mark("listing"), True),  # a model's reading, with its line, is as good
    ({"value": "hybrid", "quote": "", "by": "listing"}, tag_mark("listing"), False),  # a tag against a tag: the copy's
    ({"value": "hybrid", "quote": "hybrid", "by": "structured"}, tag_mark("listing"), False),  # a tag, not words
    (HYBRID_IN_WORDS, tag_mark("code"), False),     # the copy's own words state it: the copy's, as before
    (None, tag_mark("listing"), False),             # no stored reading of the posting: the copy's tag, as before
    ({**HYBRID_IN_WORDS, "value": "onsite"}, tag_mark("listing"), False),  # not the way the role holds now
])
@pytest.mark.parametrize("how", RELEASERS)
async def test_a_listings_tag_does_not_lift_a_way_of_working_the_postings_own_words_state(js, how, fact, mark, gated):
    """The role is hybrid in Riverton, where the owner takes only remote work; its copy says remote."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        more = {"gate_facts": {"facts": {"work_mode": fact}}} if fact else {}
        role = await make_role(session, "Fabrikam", POSTING_WORDS, location="US, OH, Riverton", move_type="hybrid",
                               **more)
        await give_copy(session, role, locations=["US, OH, Riverton"], work_mode="remote", marks=mark)
        rules = await gating.rules(session, OWNER)
        copy = await cleansed.copy_of(session, OWNER, role.id)
        assert (gating.check(gating.role_subject(role, None, copy), rules) is not None) is gated

        if how == "settle":
            await settle.roles(session, [role], cause="The analyst researched it")
        else:
            await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
            await session.refresh(role)
        assert (role.status == stages.GATED_OUT) is gated, f"the {how} path read another way of working"


async def test_decide_reads_the_place_the_copy_states_and_a_role_in_area_by_its_own_columns_is_gated_out(js):
    """The role's own columns say Oakfield, where the owner works; its copy says the work is in Riverton."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", POSTING_WORDS, location="Oakfield", move_type="hybrid",
                               status=stages.READY_TO_LAUNCH)
        await give_copy(session, role, locations=["US, OH, Riverton"], work_mode="hybrid")
        rules = await gating.rules(session, OWNER)
        copy = await cleansed.copy_of(session, OWNER, role.id)
        assert stated_place.fails_known_place(role, rules) is False, "the role alone is in area"
        assert stated_place.fails_known_place(role, rules, copy) is True, "the gate reads the copy's place"

        assert await stated_place.decide(session, OWNER, role, cause="The role's place changed") is True

        assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "location"


async def test_decide_leaves_a_role_whose_copy_places_it_in_area_though_its_own_columns_are_out_of_it(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", POSTING_WORDS, location="US, OH, Riverton", move_type="hybrid",
                               status=stages.READY_TO_LAUNCH)
        await give_copy(session, role, locations=["Oakfield"], work_mode="hybrid")
        rules = await gating.rules(session, OWNER)
        copy = await cleansed.copy_of(session, OWNER, role.id)
        assert stated_place.fails_known_place(role, rules) is True, "the role alone is out of area"
        assert stated_place.fails_known_place(role, rules, copy) is False

        assert await stated_place.decide(session, OWNER, role, cause="The role's place changed") is False
        assert role.status == stages.READY_TO_LAUNCH


async def test_the_analyst_is_not_asked_for_a_place_its_copy_already_states(js):
    """The role holds only a country and the way of working; the copy says where. The gate is decided on that."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", POSTING_WORDS, location="United States", move_type="onsite")
        await give_copy(session, role, locations=["Oakfield"], work_mode="onsite")
        rules = await gating.rules(session, OWNER)
        copy = await cleansed.copy_of(session, OWNER, role.id)
        assert stated_place.problem(role, rules, None, None, POSTING_WORDS, False) is not None
        assert stated_place.problem(role, rules, None, None, POSTING_WORDS, False, copy) is None
        assert stated_place.ask_first(role, rules, POSTING_WORDS, copy) is None


async def test_every_source_read_judges_a_role_in_ready_to_launch_on_its_place_not_only_one_that_changed_it(js):
    """The read gave the role nothing of its own; it gave its posting (so its copy) another place."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        role = await make_role(session, "Fabrikam", POSTING_WORDS, location="Oakfield", move_type="hybrid",
                               status=stages.READY_TO_LAUNCH)
        await give_copy(session, role, locations=["US, OH, Riverton"], work_mode="hybrid")

        await posting_text._place(session, role, {})

        assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "location"


async def test_the_decks_turn_judges_a_ready_role_whose_copy_states_a_place_that_fails_and_leaves_the_rest(js):
    """A changed copy of a posting is a place input: a role in Ready to Launch is looked at when it is made."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        out = await make_role(session, "Fabrikam", POSTING_WORDS, location="Oakfield", move_type="hybrid",
                              status=stages.READY_TO_LAUNCH)
        await give_copy(session, out, locations=["US, OH, Riverton"], work_mode="hybrid")
        same = await make_role(session, "Initech", POSTING_WORDS, location="Oakfield", move_type="hybrid",
                               status=stages.READY_TO_LAUNCH)
        await give_copy(session, same, locations=["Oakfield"], work_mode="hybrid")
        bare = await make_role(session, "Hooli", POSTING_WORDS, location="Oakfield", move_type="hybrid",
                               status=stages.READY_TO_LAUNCH)

        assert await deck.gate(session, OWNER) == 1

        assert out.status == stages.GATED_OUT and out.screened_out_gate_id == "location"
        assert same.status == bare.status == stages.READY_TO_LAUNCH


# --- the copy reaches every caller that asks whether the work location gate is decided --------------------------------
# Found by review of the place fix (2026-11-04): the pure functions took the copy, but nothing pinned that the callers
# hand it over, so a caller that dropped it kept every test green.


async def test_the_gate_facts_step_does_not_ask_haiku_for_a_place_the_roles_copy_states(js, monkeypatch):
    """The role holds only a country and a way of working; the copy says where. The step decides on that: nothing is
    left for a Haiku read."""
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)  # the tests leave the short read off (`conftest.py`)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        bare = await make_role(session, "Initech", POSTING_WORDS, location="United States", move_type="onsite")
        stated = await make_role(session, "Fabrikam", POSTING_WORDS, location="United States", move_type="onsite")
        await give_copy(session, stated, locations=["Oakfield"], work_mode="onsite")

        assert (await gate_facts.process(session, bare)).state == gate_facts.WAITS, "no copy: the place is asked for"
        assert (await gate_facts.process(session, stated)).state == gate_facts.CLEAR
        assert stated.gate_facts["haiku"]["asks"] == []


async def test_the_recheck_does_not_send_a_role_back_to_the_analyst_for_a_place_its_copy_states(js):
    """Researched without a place answer, holding only a country: sent back once, unless its copy says where."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        done = {"status": stages.READY_TO_LAUNCH, "analysis_state": "done", "location": "United States",
                "move_type": "onsite", "analysis": {"required": [{"item": "Five years", "class": "P"}]}}
        bare = await make_role(session, "Initech", POSTING_WORDS, **done)
        stated = await make_role(session, "Fabrikam", POSTING_WORDS, **done)
        await give_copy(session, stated, locations=["Oakfield"], work_mode="onsite")

        assert await stated_place.recheck(session, OWNER, bare, cause="Before the writer") == stated_place.SENT_BACK
        assert await stated_place.recheck(session, OWNER, stated, cause="Before the writer") == stated_place.PASSED
