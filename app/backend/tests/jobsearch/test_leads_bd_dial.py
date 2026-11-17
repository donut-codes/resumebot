"""The Leads business development dial (docs/PRD.md 46.4; the owner, 2026-11-05): a gate on roles that lead business
development is a fact of the posting, yes, no or unknown, that a short Haiku read decides from the sentences
of the posting that speak of selling, stored with the sentence it rests on, and a pick dial over it (Must or Prefer,
never preset). A Must stops a role only on the answer the user does not want, and only on a quote of the posting;
unknown never gates and never holds a role (PRD 46.7). Nothing here calls a model: `take_answers` stands in for the
reader. Every name, place and sentence is made up."""

import copy
from datetime import timedelta

import pytest

from app import db
from app.harness import instructions as harness_instructions
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts, posting_facts, regate
from app.jobsearch.loops import schedule
from app.jobsearch.measures import dials
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import dial_hold
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import schema as p
from app.jobsearch.research import prioritize
from app.timeutil import owner_today
from tests.builders import counted
from tests.jobsearch.test_dials import a_role, profile_with, rules_of, stored, subject
from tests.jobsearch.test_gate_facts import GATES, hand, read, trail_of, world
from tests.support import FIVE, OWNER, PROFILE, load_profile

BD = dials.DIALS["business-development"]
MUST_NO = {"id": "dial-business-development-must", "kind": "job_fact", "section": "posting", "fact": "leads_bd",
           "op": "in", "value": "no"}
MUST_YES = {**MUST_NO, "value": "yes"}
PREFER_YES = {"id": "dial-business-development", "section": "posting", "says": "A made-up line about selling",
              "points": 3, "checked_by": "app", "fact": "leads_bd", "op": "in", "value": "yes"}
#: Gates with no place or pay, and the dial under test; or no dial at all.
NO_DIAL = {"location": {}}
ONLY_NO, ONLY_YES = {"location": {}, "other": [MUST_NO]}, {"location": {}, "other": [MUST_YES]}

SELLS = "You will own the new business pipeline and close deals with new clients."
SUPPORTS = "You will help the sales team with proposals and keep existing client relationships warm."
#: A selling duty the word list does not know (PRD 46.4 lists what it misses): code reads `no` from it.
DUTY = "Drive revenue growth across the region and expand our customer base."


#: A made-up paragraph with no word of selling, so a posting is whole (`whole.thin`): the step reads no `no` from a
#: snippet.
FILLER = ("The programs reach families across the county. The team plans each one, staffs it with volunteers, keeps "
          "the records and reports to the board every quarter. Days are busy and the work is steady, with a small "
          "group that shares the load and meets each Monday to look at what the week holds.")


def posting(*lines: str) -> str:
    return "\n".join(["Programs Lead", "", "About the job", "Lead the programs team.", FILLER, *lines])


LEADS = posting(SELLS)
HELPS = posting(SUPPORTS)
PLAIN = posting("Plan and run the programs.", "Report to the board each quarter.")


@pytest.fixture
def haiku_on(monkeypatch):
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)


def answer(role_id, value: str, quote: str = "") -> dict:
    fact = {"value": value, **({"quote": quote} if quote else {})}
    return {"id": str(role_id)[:8], "kind": "facts", "facts": {"leads_bd": fact}}


def said(value: str, by: str = posting_facts.HAIKU) -> dict:
    """What a role stores of the fact, with who read it: `by` is `code` for the `no` the word list wrote, `haiku` for a
    model's read."""
    return {"leads_bd": {"value": value, "quote": "a made-up line", "by": by}}


async def take(*answers: dict) -> dict:
    async with db.session_factory()() as session:
        await hand(session)
        taken = await gate_facts.take_answers(session, OWNER, list(answers))
        await session.commit()
    return taken


async def stored_role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id)


# --- the lines a read of "does it lead business development" is given ------------------------------------------------


def test_the_lines_the_read_is_given_are_the_title_and_the_sentences_that_speak_of_selling():
    text = posting(SELLS, "Plan and run the programs.", SUPPORTS, "We carry a quota for the whole team.")
    lines = posting_facts.bd_lines(text, "Programs Lead").splitlines()
    assert lines == ["Programs Lead", SELLS, SUPPORTS, "We carry a quota for the whole team."]


@pytest.mark.parametrize("sentence", [
    "Develop new business with accounts in the region.",
    "Build and manage a sales pipeline.",
    "Carry a bookings target each quarter.",
    "Win new clients for the firm.",
    "Own business development for the region.",
    "Responsible for closing deals.",
])
def test_each_wording_of_selling_makes_a_sentence_a_candidate(sentence):
    assert sentence in posting_facts.bd_lines(posting(sentence), "Programs Lead")


def test_a_title_that_speaks_of_selling_is_a_candidate_though_the_text_says_nothing_of_it():
    held = posting_facts.bd_lines(PLAIN, "Business Development Manager")
    assert held.splitlines() == ["Business Development Manager"]


def test_a_posting_that_speaks_of_no_selling_has_no_candidate_lines():
    assert posting_facts.bd_lines(PLAIN, "Programs Lead") == ""
    assert posting_facts.bd_lines("", "Programs Lead") == ""


def test_the_candidate_lines_are_held_to_a_cap_and_a_long_sentence_is_clipped():
    many = posting(*(f"Win new clients in territory {n}." for n in range(500)))
    held = posting_facts.bd_lines(many, "Programs Lead")
    assert 0 < len(held) <= posting_facts.BD_CHARS
    long_one = "Win new clients " + "and more " * 200
    (line,) = posting_facts.bd_lines(posting(long_one), None).splitlines()
    assert len(line) <= posting_facts.BD_LINE_CHARS and long_one.startswith(line)


def test_code_reads_no_where_there_are_no_candidate_lines_and_leaves_the_rest_to_the_reader():
    plain = posting_facts.extract(PLAIN, "Fabrikam", "Programs Lead")
    assert plain.leads_bd is not None
    assert plain.leads_bd.value == "no" and plain.leads_bd.by == posting_facts.CODE
    assert plain.facts()["leads_bd"] == {"value": "no", "quote": "", "by": "code"}
    assert posting_facts.extract(LEADS, "Fabrikam", "Programs Lead").leads_bd is None
    assert posting_facts.extract(PLAIN, "Fabrikam", "Business Development Manager").leads_bd is None
    assert posting_facts.extract("", "Fabrikam", "Programs Lead").leads_bd is None, "no text: nothing to say"


# --- the dial: a Must stops only on the answer the user does not want ----------------------------------------------


@pytest.mark.parametrize("must, facts, result", [
    (MUST_NO, said("yes"), gating.FAIL),
    (MUST_NO, said("no"), gating.PASS),
    (MUST_NO, {}, gating.PASS),  # unknown never gates and never holds the role
    (MUST_YES, said("no"), gating.FAIL),
    (MUST_YES, said("yes"), gating.PASS),
    (MUST_YES, {}, gating.PASS),
])
def test_a_must_on_business_development_fails_only_on_the_answer_the_user_does_not_want(must, facts, result):
    # Edited 2026-11-05 (PRD 46.4, review of feat/bd-lead-dial): the facts are a model's reads (`by: haiku`); the helper
    # that stored `by: code` for every fact stood for a read that code never makes (code writes only `no`, and a code
    # `no` gates nothing: the next test).
    line = gating.results(subject(stored=facts), rules_of(profile_with(gates=[must])))
    assert next(iter(line)).result == result


@pytest.mark.parametrize("must", [MUST_NO, MUST_YES])
def test_a_no_that_code_wrote_is_a_miss_of_the_word_list_not_a_read_and_gates_nothing(must):
    """The review's probe: a Must on Yes stopped a role on code's `no`, which only says no sentence matched the word
    list (a posting about "revenue growth" or a "customer base" matches none)."""
    line = gating.results(subject(stored=said("no", posting_facts.CODE)), rules_of(profile_with(gates=[must])))
    assert next(iter(line)).result == gating.PASS
    assert "leads_bd" not in gating.job_facts(subject(stored=said("no", posting_facts.CODE)), profile_with(),
                                              subject().posted)


@pytest.mark.parametrize("stored_fact", [{}, said("no", posting_facts.CODE)])
def test_a_pass_on_a_posting_that_shows_nothing_says_so_in_words_that_read_as_a_sentence(stored_fact):
    """A role with no fact, or with the `no` code wrote, passes a Must on the dial: the line says the posting does not
    show whether the role leads business development (it said "states no leads business development")."""
    line = next(iter(gating.results(subject(stored=stored_fact), rules_of(profile_with(gates=[MUST_NO])))))
    assert line.result == gating.PASS
    assert line.fact == "the posting does not show whether the role leads business development"


def test_unknown_is_not_a_fact_the_dial_reads_and_a_fact_is_read_from_what_is_stored():
    profile = profile_with()
    quote = {"leads_bd": {"value": "yes", "quote": SELLS, "by": "haiku"}}
    facts = gating.job_facts(subject(stored=quote), profile, subject().posted)
    assert facts["leads_bd"] == "yes" and facts["leads_bd_why"] == SELLS
    # Edited 2026-11-05 (PRD 46.4, review of feat/bd-lead-dial): a `no` is a fact when a model read it; code's `no`
    # (`by: code`) is no fact, so it is left out (the test above).
    no = gating.job_facts(subject(stored={"leads_bd": {"value": "no", "quote": "", "by": "haiku"}}), profile,
                          subject().posted)
    assert no["leads_bd"] == "no" and "leads_bd_why" not in no
    for held in ({"leads_bd": {"value": "unknown", "quote": "", "by": "haiku"}}, {"leads_bd": {"value": "maybe"}}, {}):
        assert "leads_bd" not in gating.job_facts(subject(stored=held), profile, subject().posted)


def test_the_stop_says_what_the_posting_says_and_gives_the_sentence():
    profile = profile_with(gates=[MUST_NO])
    s = subject(stored={"leads_bd": {"value": "yes", "quote": SELLS, "by": "haiku"}})
    stop = gating.check(s, rules_of(profile))
    assert stop is not None and stop.gate_id == MUST_NO["id"]
    assert stop.says == "Whether the role leads business development is no."
    assert SELLS in stop.fact and "leads business development" in stop.fact


def test_a_prefer_earns_its_points_on_yes_and_nothing_on_no_or_unknown():
    profile = profile_with(asks=[PREFER_YES])

    def earned(known: dict) -> int:
        return scoring.compute({"answers": []}, profile, known)["asks"][0]["earned"]

    assert earned({"leads_bd": "yes"}) == 3
    assert earned({"leads_bd": "no"}) == 0
    assert earned({}) == 0, "unknown: no fact, no points"


def test_the_score_reads_the_fact_stored_on_the_role():
    profile = profile_with(asks=[PREFER_YES])
    role = a_role(gate_facts={"facts": stored(leads_bd="yes")})
    assert prioritize.known_of(role, profile, None)["leads_bd"] == "yes"
    assert "leads_bd" not in prioritize.known_of(a_role(), profile, None)


def test_the_dial_is_a_pick_of_yes_and_no_for_a_must_or_a_prefer_that_passes_a_posting_that_says_nothing():
    assert BD.side == "job" and BD.kind == "pick" and BD.choices == ("must", "prefer")
    assert BD.fact == "leads_bd" and BD.passes_unstated is True
    assert dials.is_dial("leads_bd", "in")
    gate = pg.Gate.model_validate(MUST_NO)
    assert pg.gate_says(gate) == "Whether the role leads business development is no."
    assert pg.who_checks(gate) == "app"
    assert pg.gate_says(pg.Gate.model_validate(MUST_YES)) == "Whether the role leads business development is yes."


def test_a_must_on_it_is_a_line_the_hard_pass_accepts_with_no_reader_judging_it():
    from app.jobsearch.profile import store as profile_store

    assert profile_store.reader_lines("gates", {"other": [MUST_NO]}) == []
    assert profile_store.reader_lines("scoring", {"asks": [PREFER_YES]}) == []
    p.Profile.model_validate({**copy.deepcopy(PROFILE), "gates": {"location": {}, "other": [MUST_NO]}})


# --- through the step: who asks, and what it costs -----------------------------------------------------------------


async def test_with_the_dial_off_nothing_is_asked_and_no_role_waits(js, haiku_on):
    ids = await world(("Fabrikam", LEADS, {}), gates=NO_DIAL)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    assert role.gate_facts["state"] == gate_facts.SETTLED and role.gate_facts["haiku"]["asks"] == []
    assert "leads_bd" not in role.gate_facts["facts"]
    async with db.session_factory()() as session:
        assert await gate_facts.waiting(session, OWNER) == []


async def test_with_the_dial_on_a_role_with_candidate_lines_is_asked_and_the_item_holds_only_the_title_and_those_lines(
        js, haiku_on):
    ids = await world(("Fabrikam", posting(SELLS, "Plan and run the programs.", SUPPORTS), {}), gates=ONLY_NO)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.WAITS and role.gate_facts["state"] == gate_facts.PENDING
    assert role.gate_facts["haiku"]["asks"] == [gate_facts.ASK_BD] and role.gate_facts["bd"] is True
    async with db.session_factory()() as session:
        (item,) = await hand(session)
    assert item["asks"] == ["leads_bd"] and item["kind"] == "facts"
    assert item["bd_lines"].splitlines() == ["Programs Lead", SELLS, SUPPORTS]
    assert item["lines"] == "", "the header lines are not sent when only this is asked"
    assert "Plan and run the programs." not in str(item) and "About the job" not in str(item)


def test_the_instructions_say_what_leading_business_development_is_and_what_is_not():
    how = gate_facts.HOW
    assert "leads_bd" in how and "yes|no|unknown" in how
    assert "closing deals" in how and "proposals" in how and "existing client relationships" in how


def test_the_titles_session_prompt_and_the_answer_template_carry_the_business_development_ask():
    """The Haiku session's whole system prompt (step 4a) and the shape of its answer must say what `HOW` in the tool
    reply says, or a session reads a title as no evidence and answers `unknown`, dropping the quote."""
    text = " ".join(harness_instructions.read("instructions", "jobsearch.titles").split()).lower()
    step = text.split("4a.", 1)[1].split("5. once every posting", 1)[0]
    for part in ("`leads_bd`", "`bd_lines`", "yes|no|unknown", "main responsibility", "the title is a line of evidence",
                 "supporting work", "thrown away"):
        assert part in step, part
    assert "leads_bd" in harness_instructions.read("templates", "jobsearch.titles.report")


def test_the_reply_says_the_title_is_evidence_for_this_ask_though_it_is_never_a_guess_for_the_others():
    how = " ".join(gate_facts.HOW.split())
    assert "Never guess from the employer or the title, except for leads_bd" in how
    assert "the title is a line of evidence" in how


async def test_with_the_dial_on_a_role_with_no_candidate_lines_gets_no_from_code_and_costs_nothing(js, haiku_on):
    ids = await world(("Fabrikam", PLAIN, {}), gates=ONLY_NO)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.gate_facts["state"] == gate_facts.SETTLED
    assert role.gate_facts["haiku"]["asks"] == []
    assert role.gate_facts["facts"]["leads_bd"] == {"value": "no", "quote": "", "by": "code"}
    async with db.session_factory()() as session:
        assert await gate_facts.waiting(session, OWNER) == []


async def test_a_snippet_is_no_posting_so_code_reads_no_from_it_and_a_must_that_wants_yes_does_not_stop_on_it(
        js, haiku_on):
    ids = await world(("Fabrikam", "Programs Lead\nPlan and run the programs.", {}), gates=ONLY_YES)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    assert "leads_bd" not in role.gate_facts["facts"] and role.gate_facts["haiku"]["asks"] == []


@pytest.mark.parametrize("duty", [DUTY, "Plan and run the programs."])
async def test_a_must_on_yes_passes_a_whole_posting_that_has_no_selling_sentence_though_code_read_no_from_it(
        js, haiku_on, duty):
    """The review's probe (PRD 46.4): a Must on Yes stopped such a role as "... is yes (the posting does not show ...)"
    on code's `no`, a miss of the word list and no read. The `no` is kept, so nothing is asked again and no spend is
    added, and it gates nothing."""
    ids = await world(("Fabrikam", posting(duty), {}), gates=ONLY_YES)
    outcome, role = await read(ids["Fabrikam"])
    assert role.gate_facts["facts"]["leads_bd"] == {"value": "no", "quote": "", "by": "code"}, (
        "code reads no from this posting: pick a sentence the word list does not know if it was widened")
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    assert role.gate_facts["state"] == gate_facts.SETTLED and role.gate_facts["haiku"]["asks"] == []


async def test_a_role_with_no_text_is_not_read_and_waits_for_none(js, haiku_on):
    ids = await world(("Fabrikam", "", {}), gates=ONLY_NO)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and "leads_bd" not in (role.gate_facts.get("facts") or {})
    assert role.status == stages.SECOND_IMPRESSION


async def test_with_the_reads_switched_off_a_role_is_not_asked_and_passes(js, monkeypatch):
    monkeypatch.setattr(gate_facts, "HAIKU_READS", False)
    ids = await world(("Fabrikam", LEADS, {}), gates=ONLY_NO)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION


# --- the answer ----------------------------------------------------------------------------------------------------


async def test_a_yes_on_a_quote_of_the_posting_gates_the_role_out_with_the_quote_on_its_history(js, haiku_on):
    ids = await world(("Fabrikam", LEADS, {}), gates=ONLY_NO)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], "yes", SELLS))
    assert taken["taken"] == 1 and taken["stopped"] == 1 and taken["problems"] == []
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.GATED_OUT and role.screened_out_gate_id == MUST_NO["id"]
    assert SELLS in role.screened_out_reason
    assert role.gate_facts["facts"]["leads_bd"] == {"value": "yes", "quote": SELLS, "by": "haiku"}
    assert role.gate_facts["state"] == gate_facts.GATED and role.gate_facts["bd"] is True
    (line,) = await trail_of(ids["Fabrikam"], "gate_facts_stopped")
    assert "leads business development" in line.sentence
    # Edited 2026-11-05 (PRD 46.4, review of feat/bd-lead-dial): the history of a stop on this dial says what the read
    # was given (the title and the sentences about selling), not "header and condition lines", which it never saw.
    assert SELLS in line.detail
    assert "Read by a short Haiku read of the posting's title and the sentences that speak of selling" in line.detail
    assert "header and condition lines" not in line.detail


async def test_a_stop_on_another_gate_of_a_role_also_asked_the_dial_keeps_the_header_lines_wording(js, haiku_on):
    quote = "Team members are expected on site every day."
    text = "\n".join(["Programs Lead", "Location: Riverton, OH", "About the job", "Lead the programs team.", quote,
                      FILLER, SELLS])
    gates ={**GATES, "other": [MUST_NO]}
    ids = await world(("Initech", text, {"location": "Riverton, OH"}), gates=gates)
    await read(ids["Initech"])
    async with db.session_factory()() as session:
        (item,) = await hand(session)
        taken = await gate_facts.take_answers(session, OWNER, [{
            "id": item["id"], "kind": "facts",
            "facts": {"work_mode": {"value": "on-site", "quote": quote}, "leads_bd": {"value": "no"}}}])
        await session.commit()
    assert sorted(item["asks"]) == ["leads_bd", "work_mode"] and taken["stopped"] == 1
    role = await stored_role(ids["Initech"])
    assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "location"
    (line,) = await trail_of(ids["Initech"], "gate_facts_stopped")
    assert "Read by a short Haiku read of the posting's header and condition lines" in line.detail
    assert "sentences that speak of selling" not in line.detail


async def test_a_yes_whose_quote_is_not_in_the_posting_is_dropped_and_gates_nothing(js, haiku_on):
    ids = await world(("Fabrikam", LEADS, {}), gates=ONLY_NO)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], "yes", "You will carry a seven figure quota every quarter."))
    assert taken["taken"] == 1 and taken["stopped"] == 0
    assert "the quote is not in the posting" in taken["problems"][0]
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.SECOND_IMPRESSION and "leads_bd" not in role.gate_facts["facts"]
    assert role.gate_facts["state"] == gate_facts.SETTLED


async def test_a_yes_with_no_quote_is_dropped_and_gates_nothing(js, haiku_on):
    ids = await world(("Fabrikam", LEADS, {}), gates=ONLY_NO)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], "yes"))
    assert taken["stopped"] == 0
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.SECOND_IMPRESSION and "leads_bd" not in role.gate_facts["facts"]


@pytest.mark.parametrize("value", ["unknown", "no", "maybe", ""])
async def test_unknown_and_no_never_gate_a_must_that_wants_no(js, haiku_on, value):
    ids = await world(("Fabrikam", HELPS, {}), gates=ONLY_NO)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], value))
    assert taken["taken"] == 1 and taken["stopped"] == 0
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.SECOND_IMPRESSION and role.gate_facts["state"] == gate_facts.SETTLED
    held = role.gate_facts["facts"].get("leads_bd")
    assert held is None or held["value"] == "no"


async def test_a_no_is_kept_as_the_fact_and_a_must_that_wants_yes_stops_on_it(js, haiku_on):
    ids = await world(("Fabrikam", HELPS, {}), gates=ONLY_YES)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], "No"))
    assert taken["stopped"] == 1
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.GATED_OUT and role.gate_facts["facts"]["leads_bd"]["value"] == "no"
    assert role.gate_facts["facts"]["leads_bd"]["by"] == "haiku"


async def test_a_prefer_asks_too_and_a_yes_is_kept_for_the_score(js, haiku_on):
    ids = await world(("Fabrikam", LEADS, {}), gates=NO_DIAL)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring={"asks": [*FIVE["asks"], PREFER_YES]}, gates=NO_DIAL)
        await session.commit()
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.WAITS and role.gate_facts["haiku"]["asks"] == [gate_facts.ASK_BD]
    taken = await take(answer(ids["Fabrikam"], "yes", SELLS))
    assert taken["stopped"] == 0
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.SECOND_IMPRESSION and role.gate_facts["facts"]["leads_bd"]["value"] == "yes"
    known = prioritize.known_of(role, p.Profile.model_validate(
        {**copy.deepcopy(PROFILE), "scoring": {"asks": [PREFER_YES]}, "gates": NO_DIAL}), None)
    assert known["leads_bd"] == "yes"


# --- a role the dial must not drop, and roles already in play ------------------------------------------------------


@pytest.mark.parametrize("status", [stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH])
async def test_a_deck_role_the_step_never_read_is_not_dropped_by_a_must_on_business_development(js, status):
    """The `dial_hold` regression: a Must that waited would drop the roles of the deck the step never reads (the deck is
    read after the cheap gates) back to First Impression, and the fact would never arrive."""
    today = owner_today()
    role = Opportunity(
        owner=OWNER, slug="deck", company="Firm deck", title="Lead deck", origin="scout",
        match_key="firm deck|lead deck", status=status, found=today, posted=today - timedelta(days=1), gate_facts={},
        fit=80, fit_breakdown={"math": 1, "percent": 80, "asks": []}, **counted())
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=ONLY_NO, settings={**PROFILE["settings"], "apply_per_day": 5})
        session.add(role)
        await session.commit()
    async with db.session_factory()() as session:
        assert await dial_hold.settle(session, OWNER) == 0
        await session.commit()
    assert (await stored_role(role.id)).status == status


async def stale_ids() -> list:
    async with db.session_factory()() as session:
        return await gate_facts.stale(session, OWNER, limit=100)


async def turn_dial_on(gates: dict = ONLY_NO) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=gates)
        await session.commit()


async def test_a_role_read_before_the_dial_is_read_once_more_and_gets_no_from_code_or_the_ask(js, haiku_on):
    ids = await world(("Fabrikam", PLAIN, {}), ("Initech", LEADS, {}), gates=NO_DIAL)
    for role_id in ids.values():
        await read(role_id)
    assert await stale_ids() == [], "dial off: the roles are not read again"
    await turn_dial_on()
    assert sorted(await stale_ids()) == sorted(ids.values()), "dial on: each is read once more"
    assert await schedule.read_gate_facts(OWNER) == 2
    plain, leads = await stored_role(ids["Fabrikam"]), await stored_role(ids["Initech"])
    assert plain.gate_facts["facts"]["leads_bd"]["value"] == "no" and plain.gate_facts["state"] == gate_facts.SETTLED
    assert leads.gate_facts["state"] == gate_facts.PENDING and leads.gate_facts["haiku"]["asks"] == [gate_facts.ASK_BD]
    assert plain.gate_facts["bd"] is True and leads.gate_facts["bd"] is True
    assert await stale_ids() == [] and await schedule.read_gate_facts(OWNER) == 0, "read once, then left alone"


async def test_the_catch_up_changes_only_what_the_dial_adds(js, haiku_on):
    ids = await world(("Initech", LEADS, {}), gates=NO_DIAL)
    old = (await read(ids["Initech"]))[1].gate_facts
    await turn_dial_on()
    await schedule.read_gate_facts(OWNER)
    new = (await stored_role(ids["Initech"])).gate_facts
    assert new["fp"] == old["fp"] and new["facts"] == old["facts"] and new["reader"] == old["reader"]
    assert new["state"] == gate_facts.PENDING and new["bd"] is True


async def test_a_role_already_waiting_for_another_read_is_asked_the_dial_with_it(js, haiku_on):
    text = ("Programs Lead\nLocation: Riverton, OH\nAbout the job\nLead the programs team.\n"
            f"The job needs travel of up to 40% of the year.\n{SELLS}")
    place = {"location": {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield"]},
                          "in_person": {"on": True, "locations": ["Oakfield"]}}}
    ids = await world(("Fabrikam", text, {"location": "Not stated"}), gates=place)
    before = (await read(ids["Fabrikam"]))[1].gate_facts
    assert before["state"] == gate_facts.PENDING and gate_facts.ASK_BD not in before["haiku"]["asks"]
    await turn_dial_on({**place, "other": [MUST_NO]})
    await schedule.read_gate_facts(OWNER)
    after = (await stored_role(ids["Fabrikam"])).gate_facts
    assert after["state"] == gate_facts.PENDING
    assert after["haiku"]["asks"] == [*before["haiku"]["asks"], gate_facts.ASK_BD]
    assert after["haiku"]["handed"] == 0


async def test_a_settled_role_that_had_asks_is_asked_only_the_dial_and_not_its_old_asks_or_header_lines_again(
        js, haiku_on):
    """The review's probe: the catch-up kept the old asks of a role whose read was answered and added the dial, so the
    3,500 characters of header lines went out again and the place and the way of working were asked again, against
    PRD 46.4 ("nothing else read again")."""
    text = ("Programs Lead\nLocation: Riverton, OH\nAbout the job\nLead the programs team.\n"
            f"The job needs travel of up to 40% of the year.\n{SELLS}")
    place = {"location": {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield"]},
                          "in_person": {"on": True, "locations": ["Oakfield"]}}}
    ids = await world(("Fabrikam", text, {"location": "Not stated"}), gates=place)
    before = (await read(ids["Fabrikam"]))[1].gate_facts
    assert before["state"] == gate_facts.PENDING and before["haiku"]["asks"], "a read of header facts is asked"
    await take({"id": str(ids["Fabrikam"])[:8], "kind": "facts", "facts": {}})
    answered = (await stored_role(ids["Fabrikam"])).gate_facts
    assert answered["state"] == gate_facts.SETTLED and answered["haiku"]["asks"] == before["haiku"]["asks"]
    await turn_dial_on({**place, "other": [MUST_NO]})
    assert await schedule.read_gate_facts(OWNER) == 1
    after = (await stored_role(ids["Fabrikam"])).gate_facts
    assert after["state"] == gate_facts.PENDING and after["haiku"] == {"asks": [gate_facts.ASK_BD], "handed": 0}
    assert after["facts"] == answered["facts"] and after["fp"] == answered["fp"], "nothing else is read or changed"
    async with db.session_factory()() as session:
        (item,) = await hand(session)
    assert item["asks"] == ["leads_bd"] and item["lines"] == ""
    assert item["bd_lines"].splitlines() == ["Programs Lead", SELLS]


async def test_a_researched_role_is_left_as_it_is(js, haiku_on):
    held = {"fp": "x", "state": gate_facts.SETTLED, "facts": {}, "reader": gate_facts.READER}
    ids = await world(("Fabrikam", LEADS, {"analysis_state": "researched", "gate_facts": held}), gates=ONLY_NO)
    assert await stale_ids() == [] and await schedule.read_gate_facts(OWNER) == 0
    assert (await stored_role(ids["Fabrikam"])).gate_facts == held


async def test_saving_the_dial_gates_at_once_the_roles_that_hold_the_fact_and_leaves_the_rest(js, haiku_on):
    ids = await world(("Fabrikam", LEADS, {}), ("Initech", PLAIN, {}), ("Hooli", LEADS, {}), gates=NO_DIAL)
    for role_id in ids.values():
        await read(role_id)
    async with db.session_factory()() as session:
        for company, value in (("Fabrikam", "yes"), ("Initech", "no")):
            role = await session.get(Opportunity, ids[company])
            role.gate_facts = {**role.gate_facts, "facts": {**role.gate_facts["facts"], "leads_bd": {
                "value": value, "quote": SELLS if value == "yes" else "", "by": "haiku"}}}
        await session.commit()
    await turn_dial_on()
    async with db.session_factory()() as session:
        await regate.run(session, OWNER, cause="Hard pass was saved")
        await session.commit()
    assert (await stored_role(ids["Fabrikam"])).status == stages.GATED_OUT
    assert (await stored_role(ids["Initech"])).status == stages.SECOND_IMPRESSION
    assert (await stored_role(ids["Hooli"])).status == stages.SECOND_IMPRESSION, "no fact: passes, never held"


async def test_saving_a_must_on_yes_stops_no_role_on_a_no_that_code_wrote(js, haiku_on):
    """The same probe through the re-gate: every role read with the dial off holds code's `no` where the posting has no
    sentence the word list knows, and saving a Must on Yes must not gate them all out on it."""
    ids = await world(("Fabrikam", posting(DUTY), {}), ("Initech", PLAIN, {}), gates=NO_DIAL)
    for role_id in ids.values():
        await read(role_id)
        assert (await stored_role(role_id)).gate_facts["facts"]["leads_bd"]["by"] == posting_facts.CODE
    await turn_dial_on(ONLY_YES)
    async with db.session_factory()() as session:
        await regate.run(session, OWNER, cause="Hard pass was saved")
        await session.commit()
    for role_id in ids.values():
        assert (await stored_role(role_id)).status == stages.SECOND_IMPRESSION
