"""The Main work dial (docs/PRD.md 46.4; the owner, 2026-11-12): a Must on Hard pass over
a list of the owner's own words that screens out a role whose main work, as the posting states it with a quote, is one
of them. A short Haiku read answers `{value, quote}` (one of the owner's words, or `none`); code matches whole words and
decides. Unread, unquoted, `none`, a word outside the list or a code-written answer passes. The list is the owner's
data, so the code holds none: every word, title and sentence here is made up. Nothing calls a model: `take_answers`
stands in for the reader."""

from types import SimpleNamespace

import pytest

from app import db
from app.harness import instructions as harness_instructions
from app.jobsearch import stages
from app.jobsearch.doors import scout
from app.jobsearch.fit import scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts, posting_facts, regate
from app.jobsearch.loops import schedule
from app.jobsearch.measures import dials
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import gates as pg
from app.trail import service as trail
from tests.jobsearch.test_dials import profile_with, rules_of, subject
from tests.jobsearch.test_gate_facts import GATES, hand, read, trail_of, world
from tests.jobsearch.test_leads_bd_dial import FILLER, posting, stored_role
from tests.support import FIVE, OWNER, load_profile


@pytest.fixture
def haiku_on(monkeypatch):
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)


WORDS = "wheelwrighting, harbor ledgering"
MUST = {"id": "dial-main-work-must", "kind": "job_fact", "section": "posting", "fact": "main_work", "op": "excludes",
        "value": WORDS}
ONLY = {"location": {}, "other": [MUST]}
NO_LIST = {"location": {}, "other": [{**MUST, "value": ""}]}

DOES = "Most of the day is wheelwrighting for the county carts."
TOUCHES = "You will sit beside the wheelwrighting crew and sometimes order their timber."
PLAIN = posting("Plan and run the programs.", "Report to the board each quarter.")
HOLDS = posting(DOES, "Plan and run the programs.")


def said(value: str, quote: str = DOES, by: str = posting_facts.HAIKU) -> dict:
    return {"main_work": {"value": value, "quote": quote, "by": by}}


def answer(role_id, value: str, quote: str = "") -> dict:
    fact = {"value": value, **({"quote": quote} if quote else {})}
    return {"id": str(role_id)[:8], "kind": "facts", "facts": {"main_work": fact}}


async def take(*answers: dict) -> dict:
    async with db.session_factory()() as session:
        await hand(session)
        taken = await gate_facts.take_answers(session, OWNER, list(answers))
        await session.commit()
    return taken


def line_of(facts: dict, value: str = WORDS) -> gating.Line:
    gate = {**MUST, "value": value}
    return next(iter(gating.results(subject(stored=facts), rules_of(profile_with(gates=[gate])))))


# --- the dial ------------------------------------------------------------------------------------------------------


def test_the_dial_is_a_words_dial_for_a_must_only_that_passes_a_posting_that_does_not_say():
    dial = dials.DIALS["main-work"]
    assert (dial.side, dial.fact, dial.kind, dial.choices) == ("job", "main_work", "words", ("must",))
    assert dial.passes_unstated is True


def test_only_the_excludes_comparison_is_the_dials_so_a_prefer_is_no_dial_line():
    assert dials.is_dial("main_work", "excludes")
    assert not dials.is_dial("main_work", "in") and not dials.is_dial("main_work", ">=")


def test_the_dial_says_what_it_does_in_a_sentence():
    said_it = dials.says("main_work", "excludes", WORDS)
    assert said_it == "The role's main work has none of: wheelwrighting or harbor ledgering."
    assert dials.says("main_work", "excludes", "wheelwrighting") == "The role's main work has none of: wheelwrighting."


def test_the_title_dial_is_unchanged():
    assert dials.DIALS["title"].passes_unstated is False
    assert dials.says("title", "excludes", "intern") == "The title has none of: intern."


# --- the gate: a word and a quote that holds it ---------------------------------------------------------------------


@pytest.mark.parametrize("word, quote", [
    ("wheelwrighting", DOES),
    ("wheelwrighting", DOES.upper()),
    ("harbor ledgering", "The role is mostly Harbor-Ledgering for the port office."),
    ("harbor ledgering", "Most days are harbor   ledgering."),
])
def test_a_word_with_a_quote_that_holds_it_stops_the_role_and_the_reason_names_both(word, quote):
    line = line_of(said(word, quote))
    assert line.result == gating.FAIL
    assert f'"{word}"' in line.fact and quote in line.fact


def test_the_stop_says_the_word_and_the_quote():
    rules = rules_of(profile_with(gates=[MUST]))
    stop = gating.check(subject(stored=said("wheelwrighting")), rules)
    assert stop is not None and stop.gate_id == MUST["id"]
    assert stop.says == "The role's main work has none of: wheelwrighting or harbor ledgering."
    assert 'the posting says the role\'s main work is "wheelwrighting"' in stop.fact and DOES in stop.fact


def test_a_word_whose_quote_does_not_hold_it_passes():
    assert line_of(said("wheelwrighting", "Most of the day is spent on the carts.")).result == gating.PASS


def test_a_partial_word_is_not_the_word():
    assert line_of(said("wheelwright", "Most of the day is wheelwrighting."), "wheelwright").result == gating.PASS
    assert line_of(said("wheelwrights", "Most of the day is wheelwrighting."), "wheelwrights").result == gating.PASS


@pytest.mark.parametrize("facts", [
    {},                                                    # unread
    said("wheelwrighting", ""),                            # no quote
    said("none", DOES),                                    # none
    said("unknown", DOES),
    said("basketry", "Most of the day is basketry."),      # a word outside the list
    said("wheelwrighting", DOES, by=posting_facts.CODE),   # code wrote it
    said("none", "", by=posting_facts.CODE),
])
def test_unread_unquoted_none_outside_the_list_and_code_written_answers_pass(facts):
    line = line_of(facts)
    assert line.result == gating.PASS
    assert line.fact == "the posting does not show the role's main work"


def test_job_facts_carry_only_a_models_word_and_its_quote():
    profile = profile_with(gates=[MUST])
    got = gating.job_facts(subject(stored=said("wheelwrighting")), profile, rules_of(profile).today)
    assert got["main_work"] == "wheelwrighting" and got["main_work_why"] == DOES
    code = posting_facts.CODE
    for facts in ({}, said("none"), said("wheelwrighting", by=code), said("none", "", by=code)):
        got = gating.job_facts(subject(stored=facts), profile, rules_of(profile).today)
        assert "main_work" not in got and "main_work_why" not in got


def test_the_dial_never_waits_for_the_fact():
    rules = rules_of(profile_with(gates=[MUST]))
    assert gating.waiting_dials(subject(), rules) == []


@pytest.mark.parametrize("over", [{"analysis_state": "done"}, {"status": stages.READY_TO_LAUNCH}])
def test_a_role_in_play_keeps_the_fact_and_the_gate_never_sees_it(over):
    """Researched (analysis_state done) and Ready to Launch roles are read and stored but never moved by this dial."""
    rules = rules_of(profile_with(gates=[MUST]))
    facts = said("wheelwrighting")
    assert next(iter(gating.results(subject(stored=facts), rules))).result == gating.FAIL
    base = {"company": "Fabrikam", "location": None, "move_type": None, "comp": None, "posted": None, "found": None,
            "target_role": None, "gate_facts": {"facts": facts}, "gate_answers": None, "analysis": {},
            "title": "Programs Lead", "status": stages.SECOND_IMPRESSION, "analysis_state": "open"}
    open_role = SimpleNamespace(**base)
    assert gating.role_subject(open_role, None).in_play is False
    assert next(iter(gating.results(gating.role_subject(open_role, None), rules))).result == gating.FAIL
    in_play = gating.role_subject(SimpleNamespace(**{**base, **over}), None)
    assert in_play.in_play is True
    assert next(iter(gating.results(in_play, rules))).result == gating.PASS
    assert in_play.stored == facts, "the fact stays stored"


def test_the_reason_names_the_word_and_gives_the_quote_when_there_is_one():
    assert dials.main_work_words("wheelwrighting") == "the posting says the role's main work is \"wheelwrighting\""
    assert dials.main_work_words("wheelwrighting", DOES).endswith(f': "{DOES}"')


def test_the_gate_passes_a_role_with_no_known_main_work():
    result = scoring.dial_gate(pg.Gate.model_validate(MUST), {})
    assert result == ("pass", "the posting does not show the role's main work")


# --- what code writes and what is handed -----------------------------------------------------------------------------


def test_code_writes_none_for_a_whole_posting_that_holds_no_word_of_the_list():
    got = posting_facts.main_work_from_text(PLAIN, "Programs Lead", WORDS)
    assert got is not None and got.value == "none" and got.by == posting_facts.CODE and got.quote == ""
    assert got.as_dict() == {"value": "none", "quote": "", "by": "code"}


def test_code_writes_nothing_when_a_word_is_in_the_posting_or_the_title_or_the_text_is_thin_or_the_list_empty():
    assert posting_facts.main_work_from_text(HOLDS, "Programs Lead", WORDS) is None
    assert posting_facts.main_work_from_text(PLAIN, "Harbor-Ledgering Clerk", WORDS) is None
    assert posting_facts.main_work_from_text("Programs Lead\nWe plan.", "Programs Lead", WORDS) is None
    assert posting_facts.main_work_from_text("", "Programs Lead", WORDS) is None
    assert posting_facts.main_work_from_text(PLAIN, "Programs Lead", "") is None


def test_a_partial_word_in_the_posting_is_no_word_so_code_writes_none():
    text = posting("Plan the wheelwrightings of the quarter.")
    assert posting_facts.main_work_from_text(text, "Programs Lead", "wheelwrighting").value == "none"


def test_extract_leaves_the_fact_to_the_step_that_holds_the_words():
    assert posting_facts.extract(HOLDS, "Fabrikam", "Programs Lead").main_work is None
    assert "main_work" not in posting_facts.extract(PLAIN, "Fabrikam", "Programs Lead").facts()


def test_the_lines_are_the_title_the_opening_and_the_sentences_that_hold_a_word():
    filler = "Plan and run the programs with the county. " * 30
    text = "\n".join(["Programs Lead", filler, DOES, "A plain line.", "We also do harbor ledgering on Fridays."])
    lines = posting_facts.main_work_lines(text, "Programs Lead", WORDS).splitlines()
    assert lines[0] == "Programs Lead"
    assert lines[1].startswith("Programs Lead Plan and run") and len(lines[1]) == posting_facts.MW_OPENING_CHARS
    assert lines[2:] == [DOES, "We also do harbor ledgering on Fridays."]


def test_the_lines_hold_at_most_five_sentences_and_are_capped():
    many = posting(*(f"Part {n} of the day is wheelwrighting in yard {n}." for n in range(40)))
    held = posting_facts.main_work_lines(many, "Programs Lead", WORDS)
    assert len(held) <= posting_facts.MW_CHARS
    assert sum("wheelwrighting" in line for line in held.splitlines()[2:]) == posting_facts.MW_SENTENCES
    long_one = "Wheelwrighting " + "and more " * 200
    held = posting_facts.main_work_lines(posting(long_one), "Programs Lead", WORDS)
    assert all(len(line) <= posting_facts.MW_OPENING_CHARS for line in held.splitlines())


# --- through the step: who asks, and what is stored ------------------------------------------------------------------


async def test_with_an_empty_list_nothing_is_asked_and_no_mark_is_kept(js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {}), gates=NO_LIST)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.gate_facts["haiku"]["asks"] == []
    assert "mw" not in role.gate_facts and "main_work" not in role.gate_facts["facts"]


async def test_with_no_dial_at_all_nothing_is_asked_and_no_mark_is_kept(js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {}), gates={"location": {}})
    _, role = await read(ids["Fabrikam"])
    assert role.gate_facts["haiku"]["asks"] == [] and "mw" not in role.gate_facts


async def test_with_a_list_a_role_that_holds_a_word_is_asked_and_the_item_holds_the_title_lines_and_words_only(
        js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {}), gates=ONLY)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.WAITS and role.gate_facts["state"] == gate_facts.PENDING
    assert role.gate_facts["haiku"]["asks"] == [gate_facts.ASK_MW]
    assert role.gate_facts["haiku"]["mw"] == ["wheelwrighting", "harbor ledgering"]
    async with db.session_factory()() as session:
        (item,) = await hand(session)
    assert item["asks"] == ["main_work"] and item["kind"] == "facts" and item["title"] == "Programs Lead"
    assert item["mw_words"] == ["wheelwrighting", "harbor ledgering"]
    assert item["mw_lines"].splitlines()[0] == "Programs Lead" and DOES in item["mw_lines"]
    assert item["lines"] == "", "the header lines are not sent when only this is asked"
    assert "bd_lines" not in item


async def test_the_first_read_marks_the_words_it_was_asked_sorted(js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {}), gates=ONLY)
    _, role = await read(ids["Fabrikam"])
    assert role.gate_facts["mw"] == ["harbor ledgering", "wheelwrighting"]


async def test_a_posting_that_holds_no_word_gets_none_from_code_and_costs_nothing(js, haiku_on):
    ids = await world(("Fabrikam", PLAIN, {}), gates=ONLY)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.gate_facts["state"] == gate_facts.SETTLED
    assert role.gate_facts["haiku"]["asks"] == []
    assert role.gate_facts["facts"]["main_work"] == {"value": "none", "quote": "", "by": "code"}
    assert role.gate_facts["mw"] == ["harbor ledgering", "wheelwrighting"]
    async with db.session_factory()() as session:
        assert await gate_facts.waiting(session, OWNER) == []


async def test_a_snippet_is_no_posting_so_code_writes_nothing_and_nothing_is_asked(js, haiku_on):
    ids = await world(("Fabrikam", "Programs Lead\nMostly wheelwrighting.", {}), gates=ONLY)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.gate_facts["haiku"]["asks"] == []
    assert "main_work" not in role.gate_facts["facts"]


async def test_with_the_reads_switched_off_a_role_is_not_asked_and_passes(js, monkeypatch):
    monkeypatch.setattr(gate_facts, "HAIKU_READS", False)
    ids = await world(("Fabrikam", HOLDS, {}), gates=ONLY)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    assert role.gate_facts["haiku"]["asks"] == []


# --- the answer ------------------------------------------------------------------------------------------------------


async def test_a_word_on_a_quote_of_the_posting_gates_the_role_out_with_the_quote_on_its_history(js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {}), gates=ONLY)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], "Wheelwrighting", DOES))
    assert taken["taken"] == 1 and taken["stopped"] == 1 and taken["problems"] == []
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.GATED_OUT and role.screened_out_gate_id == MUST["id"]
    assert '"wheelwrighting"' in role.screened_out_reason and DOES in role.screened_out_reason
    assert role.gate_facts["facts"]["main_work"] == {"value": "wheelwrighting", "quote": DOES, "by": "haiku"}
    assert role.gate_facts["state"] == gate_facts.GATED
    assert role.gate_facts["mw"] == ["harbor ledgering", "wheelwrighting"]
    (line,) = await trail_of(ids["Fabrikam"], "gate_facts_stopped")
    assert "main work: wheelwrighting" in line.detail and DOES in line.detail
    assert "title, its opening and the sentences that hold the owner's words" in line.detail
    assert "header and condition lines" not in line.detail


async def test_a_hyphened_answer_for_a_spaced_word_is_the_owners_word(js, haiku_on):
    quote = "The role is mostly harbor ledgering for the port office."
    ids = await world(("Fabrikam", posting(quote), {}), gates=ONLY)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], "Harbor-Ledgering", quote))
    assert taken["stopped"] == 1
    assert (await stored_role(ids["Fabrikam"])).gate_facts["facts"]["main_work"]["value"] == "harbor ledgering"


async def test_a_word_whose_quote_lacks_the_word_is_kept_but_passes(js, haiku_on):
    other = "Plan and run the programs."
    ids = await world(("Fabrikam", HOLDS, {}), gates=ONLY)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], "wheelwrighting", other))
    assert taken["stopped"] == 0
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.SECOND_IMPRESSION and role.gate_facts["state"] == gate_facts.SETTLED


@pytest.mark.parametrize("value, quote, problem", [
    ("wheelwrighting", "", "quote is not in the posting"),
    ("wheelwrighting", "A sentence the posting never held.", "quote is not in the posting"),
    ("basketry", DOES, "not one of the words asked"),
])
async def test_an_unquoted_unfound_or_outside_word_is_dropped_and_gates_nothing(js, haiku_on, value, quote, problem):
    ids = await world(("Fabrikam", HOLDS, {}), gates=ONLY)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], value, quote))
    assert taken["stopped"] == 0 and any(problem in p for p in taken["problems"])
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.SECOND_IMPRESSION and "main_work" not in role.gate_facts["facts"]


@pytest.mark.parametrize("value", ["none", "unknown", "None."])
async def test_none_and_unknown_gate_nothing(js, haiku_on, value):
    ids = await world(("Fabrikam", posting(TOUCHES), {}), gates=ONLY)
    await read(ids["Fabrikam"])
    taken = await take(answer(ids["Fabrikam"], value, TOUCHES))
    assert taken["taken"] == 1 and taken["stopped"] == 0
    role = await stored_role(ids["Fabrikam"])
    assert role.status == stages.SECOND_IMPRESSION and role.gate_facts["state"] == gate_facts.SETTLED


async def test_a_stop_on_another_gate_of_a_role_also_asked_this_keeps_the_header_lines_wording(js, haiku_on):
    quote = "Team members are expected on site every day."
    text = "\n".join(["Programs Lead", "Location: Riverton, OH", "About the job", "Lead the programs team.", quote,
                      FILLER, DOES])
    ids = await world(("Initech", text, {"location": "Riverton, OH"}), gates={**GATES, "other": [MUST]})
    await read(ids["Initech"])
    async with db.session_factory()() as session:
        (item,) = await hand(session)
        taken = await gate_facts.take_answers(session, OWNER, [{
            "id": item["id"], "kind": "facts",
            "facts": {"work_mode": {"value": "on-site", "quote": quote}, "main_work": {"value": "none"}}}])
        await session.commit()
    assert sorted(item["asks"]) == ["main_work", "work_mode"] and taken["stopped"] == 1
    role = await stored_role(ids["Initech"])
    assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "location"
    (line,) = await trail_of(ids["Initech"], "gate_facts_stopped")
    assert "Read by a short Haiku read of the posting's header and condition lines" in line.detail
    assert "owner's words" not in line.detail


async def test_a_researched_role_is_not_read_so_it_is_never_asked(js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {"analysis_state": "done"}), gates=ONLY)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.LEFT and not role.gate_facts


# --- what the session is told ----------------------------------------------------------------------------------------


def test_the_reply_says_what_the_main_work_ask_is():
    how = " ".join(gate_facts.HOW.split())
    for part in ("main_work: {value, quote}", "mw_lines", "mw_words",
                 "none when the word names who the role works with", "a topic it touches or one duty among several"):
        assert part in how, part


def test_the_titles_session_prompt_and_the_answer_template_carry_the_main_work_ask():
    text = " ".join(harness_instructions.read("instructions", "jobsearch.titles").split()).lower()
    step = text.split("4a.", 1)[1].split("5. once every posting", 1)[0]
    for part in ("`main_work`", "`mw_lines`", "`mw_words`", "who the job works with", "a topic it touches",
                 "one duty among several", "thrown away"):
        assert part in step, part
    assert "main_work" in harness_instructions.read("templates", "jobsearch.titles.report")


def test_the_report_tool_describes_the_main_work_answer():
    registered = {t.name: t for t in getattr(scout.door, "tools", [])} if hasattr(scout.door, "tools") else {}
    text = " ".join(str(registered["jobs.titles_report"].description).split()) if registered else \
        " ".join(open(scout.__file__, encoding="utf-8").read().split())
    assert "main_work" in text and "mw_words" in text


# --- roles already read: the catch-up ---------------------------------------------------------------------------------

NO_DIAL = {"location": {}}
MORE = {**MUST, "value": WORDS + ", basketry"}
LESS = {**MUST, "value": "wheelwrighting"}
BASKETS = posting("Most of the day is basketry for the county fair.", "Plan and run the programs.")
PLACE = {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield"]},
         "in_person": {"on": True, "locations": ["Oakfield"]}}
HEADER_TEXT = ("Programs Lead\nLocation: Riverton, OH\nAbout the job\nLead the programs team.\n"
               f"The job needs travel of up to 40% of the year.\n{FILLER}\n{DOES}")


async def stale_ids() -> list:
    async with db.session_factory()() as session:
        return await gate_facts.stale(session, OWNER, limit=100)


async def save_gates(gates: dict) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=gates)
        await session.commit()


async def role_now(role_id) -> Opportunity:
    async with db.read_session_factory()() as session:
        return await session.get(Opportunity, role_id)


async def test_a_role_read_before_the_dial_is_asked_it_once(js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {}), gates=NO_DIAL)
    await read(ids["Fabrikam"])
    assert await stale_ids() == [], "no list: the role is not read again"
    await save_gates(ONLY)
    assert await stale_ids() == [ids["Fabrikam"]]
    assert await schedule.read_gate_facts(OWNER) == 1
    role = await role_now(ids["Fabrikam"])
    assert role.gate_facts["state"] == gate_facts.PENDING and role.gate_facts["haiku"]["asks"] == [gate_facts.ASK_MW]
    assert role.gate_facts["mw"] == ["harbor ledgering", "wheelwrighting"]
    assert await stale_ids() == [] and await schedule.read_gate_facts(OWNER) == 0, "asked once, then left alone"


async def test_with_an_empty_list_the_roles_in_play_are_not_loaded_or_read(js, haiku_on, monkeypatch):
    await world(("Fabrikam", HOLDS, {"analysis_state": "done"}), gates=NO_LIST)
    judged = []
    real = gate_facts._judged_texts

    async def spy(session, owner, roles):
        judged.extend(roles)
        return await real(session, owner, roles)

    monkeypatch.setattr(gate_facts, "_judged_texts", spy)
    assert await stale_ids() == [] and judged == [], "no word listed: no role in play is built a text"


async def test_a_role_waiting_for_another_read_is_asked_the_dial_with_it(js, haiku_on):
    ids = await world(("Fabrikam", HEADER_TEXT, {"location": "Not stated"}), gates={"location": PLACE})
    before = (await read(ids["Fabrikam"]))[1].gate_facts
    assert before["state"] == gate_facts.PENDING and gate_facts.ASK_MW not in before["haiku"]["asks"]
    await save_gates({"location": PLACE, "other": [MUST]})
    assert await schedule.read_gate_facts(OWNER) == 1
    after = (await role_now(ids["Fabrikam"])).gate_facts
    assert after["state"] == gate_facts.PENDING
    assert after["haiku"]["asks"] == [*before["haiku"]["asks"], gate_facts.ASK_MW] and after["haiku"]["handed"] == 0
    assert after["haiku"]["mw"] == ["wheelwrighting", "harbor ledgering"]


async def test_a_settled_role_that_had_asks_is_asked_only_the_dial(js, haiku_on):
    ids = await world(("Fabrikam", HEADER_TEXT, {"location": "Not stated"}), gates={"location": PLACE})
    before = (await read(ids["Fabrikam"]))[1].gate_facts
    await take({"id": str(ids["Fabrikam"])[:8], "kind": "facts", "facts": {}})
    answered = (await role_now(ids["Fabrikam"])).gate_facts
    assert answered["state"] == gate_facts.SETTLED and answered["haiku"]["asks"] == before["haiku"]["asks"]
    await save_gates({"location": PLACE, "other": [MUST]})
    assert await schedule.read_gate_facts(OWNER) == 1
    after = (await role_now(ids["Fabrikam"])).gate_facts
    assert after["state"] == gate_facts.PENDING
    assert after["haiku"] == {"asks": [gate_facts.ASK_MW], "handed": 0, "mw": ["wheelwrighting", "harbor ledgering"]}
    assert after["facts"] == answered["facts"] and after["fp"] == answered["fp"], "nothing else is read or changed"
    async with db.session_factory()() as session:
        (item,) = await hand(session)
    assert item["asks"] == ["main_work"] and item["lines"] == "" and DOES in item["mw_lines"]


async def test_a_posting_that_holds_no_word_of_the_list_gets_none_from_code_at_the_catch_up(js, haiku_on):
    ids = await world(("Fabrikam", PLAIN, {}), gates=NO_DIAL)
    await read(ids["Fabrikam"])
    await save_gates(ONLY)
    assert await schedule.read_gate_facts(OWNER) == 1
    role = await role_now(ids["Fabrikam"])
    assert role.gate_facts["state"] == gate_facts.SETTLED
    assert role.gate_facts["facts"]["main_work"] == {"value": "none", "quote": "", "by": "code"}


async def test_the_same_words_a_removed_word_and_a_save_with_no_change_ask_nothing_again(js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {}), gates=ONLY)
    await read(ids["Fabrikam"])
    assert await stale_ids() == []
    await save_gates({"location": {}, "other": [{**MUST, "value": "harbor ledgering, wheelwrighting"}]})
    assert await stale_ids() == [], "the same words in another order"
    await save_gates({"location": {}, "other": [LESS]})
    assert await stale_ids() == [], "a removed word"
    await save_gates(ONLY)
    assert await stale_ids() == [], "the word is back: it was marked once"


async def test_an_added_word_asks_the_roles_read_for_other_words_again_and_only_once(js, haiku_on):
    ids = await world(("Fabrikam", BASKETS, {}), gates={"location": {}, "other": [LESS]})
    first = (await read(ids["Fabrikam"]))[1].gate_facts
    assert first["facts"]["main_work"]["by"] == "code", "no word of the list: code wrote none"
    await save_gates({"location": {}, "other": [MORE]})
    assert await stale_ids() == [ids["Fabrikam"]]
    assert await schedule.read_gate_facts(OWNER) == 1
    after = (await role_now(ids["Fabrikam"])).gate_facts
    assert after["state"] == gate_facts.PENDING and after["haiku"]["asks"] == [gate_facts.ASK_MW]
    assert after["mw"] == ["basketry", "harbor ledgering", "wheelwrighting"]
    assert await stale_ids() == []


def test_a_role_stopped_by_another_gate_gets_the_mark_and_no_ask(haiku_on):
    rules = rules_of(profile_with(gates=[MUST]))
    role = SimpleNamespace(target_role=None, title="Programs Lead", company="Fabrikam")
    held = {"state": gate_facts.GATED, "stop": "pay", "mw": ["harbor ledgering"]}
    got = gate_facts._mw_catch_up(role, rules, held, posting_facts.Extracted(), HOLDS, {})  # type: ignore[arg-type]
    assert got == {"mw": ["harbor ledgering", "wheelwrighting"]}


async def test_a_role_the_owner_placed_by_hand_is_not_read_and_not_moved(js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {}), gates=NO_DIAL)
    await read(ids["Fabrikam"])
    async with db.session_factory()() as session:
        await trail.record(session, OWNER, event="role_moved", actor="person", who="Owner", sentence="You moved it.",
                           link=("opportunity", ids["Fabrikam"]))
        await session.commit()
    await save_gates(ONLY)
    assert await stale_ids() == [] and await schedule.read_gate_facts(OWNER) == 0
    stored_facts = {"main_work": {"value": "wheelwrighting", "quote": DOES, "by": "haiku"}}
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.gate_facts = {**role.gate_facts, "facts": {**role.gate_facts["facts"], **stored_facts}}
        await session.commit()
    await hard_pass_saved()
    assert (await role_now(ids["Fabrikam"])).status == stages.SECOND_IMPRESSION


# --- roles in play: read and stored, never moved --------------------------------------------------


async def store_hit(role_id) -> None:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.gate_facts = {**(role.gate_facts or {}), "mw": ["harbor ledgering", "wheelwrighting"],
                           "facts": {"main_work": {"value": "wheelwrighting", "quote": DOES, "by": "haiku"}}}
        await session.commit()


async def hard_pass_saved() -> None:
    async with db.session_factory()() as session:
        await regate.run(session, OWNER, cause="Hard pass was saved")
        await session.commit()


@pytest.mark.parametrize("over", [{"analysis_state": "done"}, {"status": stages.READY_TO_LAUNCH}])
async def test_a_role_in_play_with_a_stored_hit_stays_in_its_stage_after_a_hard_pass_save(js, over):
    ids = await world(("Fabrikam", HOLDS, over), gates=ONLY)
    await store_hit(ids["Fabrikam"])
    before = (await role_now(ids["Fabrikam"])).status
    await hard_pass_saved()
    assert (await role_now(ids["Fabrikam"])).status == before


async def test_saving_the_list_gates_the_open_role_that_holds_a_stored_hit_and_not_the_one_in_play(js):
    ids = await world(("Open", HOLDS, {}), ("Played", HOLDS, {"analysis_state": "done"}), gates=NO_DIAL)
    for role_id in ids.values():
        await store_hit(role_id)
    await save_gates(ONLY)
    await hard_pass_saved()
    assert (await role_now(ids["Open"])).status == stages.GATED_OUT
    assert (await role_now(ids["Played"])).status == stages.SECOND_IMPRESSION


@pytest.mark.parametrize("over", [{"analysis_state": "done"}, {"status": stages.READY_TO_LAUNCH}])
async def test_the_read_of_a_role_in_play_stores_the_fact_and_moves_nothing(js, haiku_on, over):
    ids = await world(("Fabrikam", HOLDS, over), ("Initech", PLAIN, over), gates=ONLY)
    status = (await role_now(ids["Fabrikam"])).status
    assert sorted(await stale_ids()) == sorted(ids.values())
    assert await schedule.read_gate_facts(OWNER) == 2
    plain = await role_now(ids["Initech"])
    assert plain.gate_facts["facts"]["main_work"] == {"value": "none", "quote": "", "by": "code"}
    asked = await role_now(ids["Fabrikam"])
    assert asked.gate_facts["haiku"]["asks"] == [gate_facts.ASK_MW] and asked.gate_facts["mwq"] == gate_facts.MWQ
    assert "state" not in asked.gate_facts, "the role's own read is left as it is"
    assert await stale_ids() == [] and await schedule.read_gate_facts(OWNER) == 0
    taken = await take(answer(ids["Fabrikam"], "wheelwrighting", DOES))
    assert taken["taken"] == 1 and taken["stopped"] == 0 and taken["problems"] == []
    done = await role_now(ids["Fabrikam"])
    assert done.status == status and done.screened_out_gate_id is None
    assert done.gate_facts["facts"]["main_work"] == {"value": "wheelwrighting", "quote": DOES, "by": "haiku"}
    assert "mwq" not in done.gate_facts
    assert len(await trail_of(ids["Fabrikam"], "gate_facts_read")) == 1
    await hard_pass_saved()
    assert (await role_now(ids["Fabrikam"])).status == status, "a stored hit never moves it"


async def test_main_work_hits_unmoved_lists_the_hits_of_roles_in_play_with_their_quotes(js, haiku_on):
    ids = await world(("Fabrikam", HOLDS, {"analysis_state": "done"}), ("Initech", HOLDS, {"analysis_state": "done"}),
                      ("Hooli", HOLDS, {}), gates=ONLY)
    await store_hit(ids["Fabrikam"])
    await store_hit(ids["Hooli"])
    async with db.session_factory()() as session:
        initech = await session.get(Opportunity, ids["Initech"])
        initech.gate_facts = {"facts": {"main_work": {"value": "none", "quote": DOES, "by": "haiku"}}}
        await session.commit()
    async with db.session_factory()() as session:
        listed = await gate_facts.main_work_hits_unmoved(session, OWNER)
    assert [(r["id"], r["word"], r["quote"]) for r in listed] == [(ids["Fabrikam"], "wheelwrighting", DOES)]
