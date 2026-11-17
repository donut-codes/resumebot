"""The gate-facts step reads the percent of travel a posting states and whether it names its employer, and the Travel
and Employer named dials stop a role on them (docs/PRD.md 46.4; the owner, 2026-11-03). These tests go through the real
step (`gate_facts.process`, `schedule.read_gate_facts`), so the wiring that hands the posting's text and the listing's
company to the reader is pinned: the reader's own tests (`test_travel_dial.py`, `test_employer_named_dial.py`) call it
directly.

A role read before the step read these (no `reader` on its gate facts) has them read once more from the text it holds,
with nothing else read again: a posting's first sentence about travel is the one the old step kept, and the percent may
stand in a later one. Every name, place and figure is made up."""

from datetime import timedelta

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import gate_facts
from app.jobsearch.loops import schedule
from app.jobsearch.models import Opportunity
from tests.jobsearch.test_gate_facts import NOW, read, trail_of, world
from tests.support import FIVE, OWNER, load_profile

TRAVEL = {"id": "dial-travel-must", "kind": "job_fact", "section": "posting", "fact": "travel_percent", "op": "<=",
          "value": 25}
NAMED = {"id": "dial-employer-named-must", "kind": "job_fact", "section": "posting", "fact": "employer_named",
         "op": "=", "value": "yes"}
#: Gates with no place or pay, and the dial(s) under test.
TRAVEL_ONLY, NAMED_ONLY = {"location": {}, "other": [TRAVEL]}, {"location": {}, "other": [NAMED]}
BOTH, NONE = {"location": {}, "other": [TRAVEL, NAMED]}, {"location": {}}


def posting(*lines: str) -> str:
    return "\n".join(["Programs Lead", "", "About the job", "Lead the programs team.", *lines])


MUCH = posting("This role needs up to 50% travel to our regional offices.")
LITTLE = posting("This role needs up to 10% travel to our regional offices.")
SOME = posting("Some travel is required.")
LATER = posting("Travel is required within the assigned territory; may include some overnight stays.", "",
                "Travel Percentage: 40%")
CLIENT = posting("Our client is seeking a Data Manager to lead the programs team.")


# --- the travel dial, through the step ----------------------------------------------------------------------------


async def test_a_role_whose_text_states_more_travel_than_the_limit_is_stopped_at_the_travel_dial(js):
    ids = await world(("Fabrikam", MUCH, {}), gates=TRAVEL_ONLY)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.STOPPED
    assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "dial-travel-must"
    assert "50%" in role.screened_out_reason
    assert role.gate_facts["facts"]["travel_percent"]["value"] == "50"
    (line,) = await trail_of(ids["Fabrikam"], "gate_facts_stopped")
    assert "50%" in line.sentence


async def test_a_role_whose_text_states_less_travel_passes_and_keeps_the_percent(js):
    ids = await world(("Fabrikam", LITTLE, {}), gates=TRAVEL_ONLY)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    assert role.gate_facts["facts"]["travel_percent"]["value"] == "10"


async def test_a_role_whose_text_states_no_percent_passes_without_waiting_for_one(js):
    ids = await world(("Fabrikam", SOME, {}), ("Initech", posting("Nothing about the road."), {}), gates=TRAVEL_ONLY)
    for company in ("Fabrikam", "Initech"):
        outcome, role = await read(ids[company])
        assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
        assert role.gate_facts["state"] == gate_facts.SETTLED
        assert "travel_percent" not in role.gate_facts["facts"]


# --- the employer named dial, through the step --------------------------------------------------------------------


async def test_a_posting_that_speaks_of_an_unnamed_client_is_stopped_at_the_employer_named_dial(js):
    """The listing's company is a real name: only the posting's own words say the employer is hidden, and they are read
    with the company (`posting_facts.extract(text, company)`)."""
    ids = await world(("Staffco", CLIENT, {}), gates=NAMED_ONLY)
    outcome, role = await read(ids["Staffco"])
    assert outcome.state == gate_facts.STOPPED
    assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "dial-employer-named-must"
    assert role.gate_facts["facts"]["employer_named"]["value"] == "no"
    assert "Our client is seeking" in role.gate_facts["facts"]["employer_named"]["quote"]


async def test_a_role_listed_under_a_placeholder_company_is_stopped_at_the_employer_named_dial(js):
    ids = await world(("Confidential", posting("Lead the programs team."), {}), gates=NAMED_ONLY)
    outcome, role = await read(ids["Confidential"])
    assert outcome.state == gate_facts.STOPPED and role.screened_out_gate_id == "dial-employer-named-must"


async def test_a_role_that_names_its_employer_passes_the_employer_named_dial_without_waiting(js):
    ids = await world(("Fabrikam", posting("Our clients value careful work."), {}), gates=NAMED_ONLY)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    assert "employer_named" not in role.gate_facts["facts"]


async def test_both_dials_off_stop_nothing_though_the_facts_are_read(js):
    ids = await world(("Staffco", posting("Our client is seeking a lead. Up to 50% travel."), {}), gates=NONE)
    outcome, role = await read(ids["Staffco"])
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    facts = role.gate_facts["facts"]
    assert facts["travel_percent"]["value"] == "50" and facts["employer_named"]["value"] == "no"


# --- a role read before the step read them ------------------------------------------------------------------------


async def before_this_reader(role_id) -> dict:
    """A role as the step left it before the travel percent and the employer named were read: read for real, then the
    marker and the two facts taken off, so what is left is what the old step stored."""
    await read(role_id)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        old = dict(role.gate_facts)
        old.pop("reader", None)
        old["facts"] = {k: v for k, v in old["facts"].items() if k not in ("travel_percent", "employer_named")}
        role.gate_facts = old
        await session.commit()
        return old


async def stale_ids() -> list:
    async with db.session_factory()() as session:
        return await gate_facts.stale(session, OWNER, limit=100)


async def stored(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id)


async def test_a_role_read_before_reads_the_percent_a_later_sentence_states_and_is_stopped_on_it(js):
    """The old step kept the first sentence that speaks of travel ("Travel is required within the assigned
    territory"), which holds no figure; the text holds one further down."""
    ids = await world(("Fabrikam", LATER, {}), gates=NONE)
    old = await before_this_reader(ids["Fabrikam"])
    assert "travel_percent" not in old["facts"] and "40" not in old["facts"]["travel"]["value"]
    assert ids["Fabrikam"] in await stale_ids(), "a role read under an older reader is read once more"
    # the owner then sets the limit: the catch-up stops the role on the figure it reads
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=TRAVEL_ONLY)
        await session.commit()
    assert await schedule.read_gate_facts(OWNER) == 1
    role = await stored(ids["Fabrikam"])
    assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "dial-travel-must"
    assert role.gate_facts["facts"]["travel_percent"]["value"] == "40"
    assert role.gate_facts["reader"] == gate_facts.READER


async def test_the_catch_up_stores_the_percent_when_the_limit_is_not_set_and_stops_nothing(js):
    ids = await world(("Fabrikam", LATER, {}), gates=NONE)
    await before_this_reader(ids["Fabrikam"])
    assert await schedule.read_gate_facts(OWNER) == 1
    role = await stored(ids["Fabrikam"])
    assert role.status == stages.SECOND_IMPRESSION
    assert role.gate_facts["facts"]["travel_percent"]["value"] == "40"


async def test_the_catch_up_reads_an_unnamed_client_a_role_read_before_never_stored(js):
    ids = await world(("Staffco", CLIENT, {}), gates=NONE)
    old = await before_this_reader(ids["Staffco"])
    assert "employer_named" not in old["facts"]
    assert await schedule.read_gate_facts(OWNER) == 1
    role = await stored(ids["Staffco"])
    assert role.gate_facts["facts"]["employer_named"]["value"] == "no"


async def test_the_catch_up_reads_a_role_once_even_when_the_text_states_neither_fact(js):
    ids = await world(("Fabrikam", SOME, {}), gates=NONE)
    await before_this_reader(ids["Fabrikam"])
    assert await schedule.read_gate_facts(OWNER) == 1
    role = await stored(ids["Fabrikam"])
    assert role.gate_facts["reader"] == gate_facts.READER
    assert "travel_percent" not in role.gate_facts["facts"] and "employer_named" not in role.gate_facts["facts"]
    assert await stale_ids() == [] and await schedule.read_gate_facts(OWNER) == 0, "read once, then left alone"


async def test_the_catch_up_changes_only_what_the_reader_added(js):
    """What the old read stored (its fingerprint, state, time, facts, the Haiku count) is as it was; a fact the role
    holds is never read over."""
    ids = await world(("Fabrikam", MUCH, {}), gates=NONE)
    old = await before_this_reader(ids["Fabrikam"])
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        held = {"value": "5", "quote": "q", "by": "haiku"}
        role.gate_facts = {**old, "facts": {**old["facts"], "travel_percent": held}}
        await session.commit()
    await schedule.read_gate_facts(OWNER)
    new = (await stored(ids["Fabrikam"])).gate_facts
    assert new["facts"]["travel_percent"] == held, "a fact held stays"
    assert {k: v for k, v in new.items() if k != "reader"} == old | {"facts": new["facts"]}
    assert new["fp"] == old["fp"] and new["at"] == old["at"] and new["haiku"] == old["haiku"]


async def test_a_role_read_by_the_step_now_is_not_read_again_by_the_catch_up(js):
    ids = await world(("Fabrikam", MUCH, {}), gates=NONE)
    first = (await read(ids["Fabrikam"]))[1].gate_facts
    assert first["reader"] == gate_facts.READER and await stale_ids() == []
    assert (await read(ids["Fabrikam"]))[1].gate_facts == first


async def test_the_catch_up_reads_the_oldest_roles_first_like_every_other_read(js):
    ids = await world(*((company, MUCH, {"created_at": NOW - timedelta(days=age)})
                        for company, age in (("Fabrikam", 3), ("Initech", 2), ("Hooli", 1))), gates=NONE)
    for role_id in ids.values():
        await before_this_reader(role_id)
    assert await stale_ids() == [ids["Fabrikam"], ids["Initech"], ids["Hooli"]]
