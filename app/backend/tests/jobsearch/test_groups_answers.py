"""The answers to the rows of statements (docs/PRD.md 30.9): the statements of a row, "first match" or "any of", are
asks, each answered by its own id (met, not or unknown) under the exact words it was asked in; the app works the row
out. A first-match row earns the points of the first statement, from the top, that is met: not met and unknown
statements are skipped, and a row with none met earns nothing. A report is refused, in words, when it answers a row as
a whole. An old single answer to a first-match row (the id of its first true statement, kept as `level`) still loads.
Every name, number and sentence is made up."""

import pytest
import yaml

from app.harness import instructions as files
from app.jobsearch.fit import scoring
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps

#: A first-match row of three statements, best first, and an "any of" row of two (the numbers are made up).
FIRST = {"id": "row-a", "kind": "best", "section": "responsibilities", "members": [
    {"id": "stmt-a", "says": "The work is mostly building", "points": 4},
    {"id": "stmt-b", "says": "The work is partly building", "points": 3},
    {"id": "stmt-c", "says": "The work is a little building", "points": 2}]}
ANY = {"id": "box-a", "kind": "any", "section": "company", "points": 2, "members": [
    {"id": "stmt-d", "says": "The employer is a public agency"},
    {"id": "stmt-e", "says": "The employer is a nonprofit"}]}
PLAIN = {"id": "ask-a", "section": "description", "says": "The work is building programs", "points": 2}
SMALL = {"fact": "employees", "op": "<", "value": 1000, "checked_by": "app"}


def profile(*groups: dict, asks=(PLAIN,), **more) -> p.Profile:
    return p.Profile.model_validate({"scoring": {"asks": list(asks), "groups": list(groups), **more}})


def row(profile_: p.Profile, ident: str) -> ps.Group:
    return next(g for g in profile_.scoring.groups if g.id == ident)


def given(ident: str, answer: str, evidence: str = "x", **more) -> dict:
    return {"id": ident, "answer": answer, "evidence": evidence, **more}


# --- a first-match answer names a statement, or none, or can't tell -----------------------------------------------


@pytest.mark.parametrize("key", ["statement", "level"])
@pytest.mark.parametrize("picked, answer", [("stmt-b", "met"), ("none", "not"), ("unknown", "unknown")])
def test_a_first_match_is_answered_with_its_first_true_statement_and_the_answer_follows_from_it(key, picked, answer):
    [kept] = scoring.answers_in({"asks": [{"id": "row-a", key: f" {picked} ", "evidence": "the posting says so"}]})
    assert (kept["id"], kept["level"], kept["answer"], kept["evidence"]) == (
        "row-a", picked, answer, "the posting says so"), "the record keeps it as `level`, the spaces around it gone"
    assert "statement" not in kept


def test_an_ask_keeps_the_answer_it_was_given_and_an_item_that_answers_nothing_is_not_an_answer():
    kept = scoring.answers_in({"asks": [
        {"id": "ask-a", "answer": "partly", "statement": "stmt-a"},  # a stray statement on an ask changes nothing
        {"id": "ask-b"}, {"id": "ask-c", "statement": ""}, {"id": "ask-d", "statement": 3}, {"answer": "met"}]})
    assert [(a["id"], a["answer"]) for a in kept] == [("ask-a", "partly")]


def test_a_stored_first_match_answer_reads_back_as_it_was_kept():
    stored = {"answers": [{"id": "row-a", "answer": "met", "level": "stmt-a", "evidence": "x", "says": "w",
                           "by": "reader"}]}
    assert scoring.answers_in(stored) == stored["answers"]


# --- the words of a first-match row ------------------------------------------------------------------------------


def test_the_words_of_an_ask_are_its_sentence_with_its_spaces_folded():
    assert scoring.words_of(ps.Ask(**PLAIN)) == scoring.wording("  The work is   building programs ")


def test_the_words_of_a_first_match_are_its_statements_sentences_in_order_each_under_its_id():
    assert scoring.words_of(row(profile(FIRST), "row-a")) == (
        "stmt-a: The work is mostly building | stmt-b: The work is partly building | "
        "stmt-c: The work is a little building")


def reworded(group: dict, index: int, says: str) -> dict:
    return {**group, "members": [{**m, "says": says} if i == index else m for i, m in enumerate(group["members"])]}


@pytest.mark.parametrize("name, changed", [
    ("a reworded statement", reworded(FIRST, 1, "The work is somewhat building")),
    ("an added statement", {**FIRST, "members": [*FIRST["members"],
                                                 {"id": "stmt-z", "says": "Hardly building", "points": 1}]}),
    ("a removed statement", {**FIRST, "members": FIRST["members"][:2]}),
    ("a first match in another order", {**FIRST, "members": [FIRST["members"][1], FIRST["members"][0],
                                                              FIRST["members"][2]]}),
    ("a statement under another id", {**FIRST, "members": [{**FIRST["members"][0], "id": "stmt-y"},
                                                            *FIRST["members"][1:]]}),
])
def test_a_first_match_that_is_changed_in_what_it_says_is_a_new_question(name, changed):
    assert scoring.words_of(row(profile(changed), "row-a")) != scoring.words_of(row(profile(FIRST), "row-a")), name


@pytest.mark.parametrize("name, changed", [
    ("a changed number", {**FIRST, "members": [{**m, "points": m["points"] + 1} for m in FIRST["members"]]}),
    ("another part of the posting", {**FIRST, "section": "qualifications"}),
    ("another tab", {**FIRST, "roles": ["builders"]}),
    ("spaces in a sentence", reworded(FIRST, 0, "  The work   is mostly building ")),
])
def test_a_first_match_changed_only_in_points_part_tab_or_spacing_is_the_same_question(name, changed):
    assert scoring.words_of(row(profile(changed), "row-a")) == scoring.words_of(row(profile(FIRST), "row-a")), name


def test_the_statements_of_an_any_of_are_asked_by_their_own_sentences_and_the_row_is_no_question():
    prof = profile(ANY)
    assert scoring.answer_for([given("box-a", "met")], row(prof, "box-a")) is None, "no answer to the row stands"
    words = scoring._words_by_id(prof)
    assert "box-a" not in words
    assert words["stmt-d"] == "The employer is a public agency" and words["stmt-e"] == "The employer is a nonprofit"
    flipped = profile({**ANY, "members": list(reversed(ANY["members"]))})
    assert scoring._words_by_id(flipped) == words, "the order of an 'any of' means nothing"
    renamed = profile(reworded(ANY, 0, "The employer is a government agency"))
    assert {k for k, v in scoring._words_by_id(renamed).items() if v != words[k]} == {"stmt-d"}, "only that one"


# --- recording, finding and stamping an answer ---------------------------------------------------------------------


def test_recording_a_first_match_answer_keeps_its_level_under_the_rows_id_and_words():
    prof = profile(FIRST)
    [kept] = scoring.record([], [given("row-a", "met", level="stmt-b")], prof, "reader")
    assert (kept["id"], kept["answer"], kept["level"], kept["by"]) == ("row-a", "met", "stmt-b", "reader")
    assert kept["says"] == scoring.words_of(row(prof, "row-a"))


def test_recording_a_report_item_that_names_only_a_statement_gives_it_the_answer_that_follows():
    report = [{"id": "row-a", "statement": "none", "evidence": "none of them"}]
    [kept] = scoring.record([], report, profile(FIRST), "analyst")
    assert (kept["answer"], kept["level"]) == ("not", "none") and "statement" not in kept


def test_the_same_row_in_the_same_words_answered_again_takes_the_place_of_the_first_answer():
    prof = profile(FIRST)
    first = scoring.record([], [given("row-a", "met", level="stmt-a")], prof, "reader")
    again = scoring.record(first, [given("row-a", "met", level="stmt-c")], prof, "analyst")
    assert [(a["level"], a["by"]) for a in again] == [("stmt-c", "analyst")]


def test_an_answer_to_the_old_words_of_a_row_stays_on_record_beside_the_new_one_and_five_wordings_are_kept():
    answers: list[dict] = []
    for n in range(7):
        prof = profile(reworded(FIRST, 0, f"The work is building, version {n}"))
        answers = scoring.record(answers, [given("row-a", "met", level="stmt-a")], prof, "reader")
    assert len(answers) == scoring.MOST_WORDINGS == 5, "the newest five wordings"
    assert "version 6" in answers[-1]["says"] and "version 2" in answers[0]["says"]


def test_a_statement_is_answered_in_its_present_words_and_an_answer_to_other_words_is_not_its_answer():
    prof = profile(FIRST)
    answers = scoring.record([], [given("stmt-b", "met")], prof, "reader")
    assert scoring.answer_for(answers, row(prof, "row-a").members[1])["answer"] == "met"
    other = profile(reworded(FIRST, 1, "The work is hardly building"))
    assert scoring.answer_for(answers, row(other, "row-a").members[1]) is None, "reworded: asked again"
    assert answers[0]["answer"] == "met", "and the old answer stays"


def test_a_row_has_no_answer_of_its_own_and_an_answer_with_no_words_kept_is_for_the_words_it_has_now():
    prof = profile(FIRST)
    legacy = [given("stmt-a", "met")]
    assert scoring.answer_for(legacy, row(prof, "row-a")) is None
    assert scoring.answer_for(legacy, row(prof, "row-a").members[0])["answer"] == "met"
    stamped = scoring.stamp(legacy, prof)
    assert stamped[0]["says"] == "The work is mostly building"
    assert scoring.answer_for(stamped, ps.Ask(id="stmt-a", says="Another thing")) is None


def test_stamping_puts_the_words_on_an_answer_to_a_statement_the_owner_grouped_and_on_an_ask():
    prof = profile(FIRST, ANY)
    stamped = scoring.stamp([given("stmt-b", "met"), given("stmt-d", "met"), given("ask-a", "not"),
                             given("gone", "met")], prof)
    assert [a.get("says") for a in stamped] == [
        "The work is partly building", "The employer is a public agency", "The work is building programs", None]


# --- an old single answer to a first-match row still loads -----------------------------------------------------------


def old_single(prof: p.Profile, level: str, answer: str = "met", **more) -> list[dict]:
    """A role's record as it was kept when a first-match row was answered once: the id of its first true statement."""
    return scoring.record([], [given("row-a", answer, level=level, **more)], prof, "reader")


def test_an_old_single_answer_that_names_a_statement_is_that_statement_met_and_the_rest_are_asked_again():
    prof = profile(FIRST)
    kept = scoring.expand(old_single(prof, "stmt-b", evidence="half the time"), prof)
    named = scoring.answer_for(kept, row(prof, "row-a").members[1])
    assert (named["answer"], named["evidence"]) == ("met", "half the time")
    assert scoring.answer_for(kept, row(prof, "row-a").members[0]) is None, "unanswered: asked again"
    assert scoring.answer_for(kept, row(prof, "row-a").members[2]) is None
    assert any(a["id"] == "row-a" for a in kept), "the old record stays on file"


@pytest.mark.parametrize("level, answer", [("none", "not"), ("unknown", "unknown")])
def test_an_old_single_answer_of_none_or_cant_tell_leaves_every_statement_unanswered(level, answer):
    prof = profile(FIRST)
    kept = scoring.expand(old_single(prof, level, answer), prof)
    assert [scoring.answer_for(kept, m) for m in row(prof, "row-a").members] == [None, None, None]


def test_an_old_single_answer_to_other_words_is_not_carried_over_and_a_statement_answered_keeps_its_own():
    prof = profile(FIRST)
    other = profile(reworded(FIRST, 2, "The work is hardly building"))
    assert scoring.answer_for(scoring.expand(old_single(prof, "stmt-a"), other), row(other, "row-a").members[0]) is None
    kept = scoring.record(old_single(prof, "stmt-a"), [given("stmt-a", "not", "no")], prof, "reader")
    assert scoring.answer_for(scoring.expand(kept, prof), row(prof, "row-a").members[0])["answer"] == "not"


# --- what a left-out question is recorded as -----------------------------------------------------------------------


def test_a_statement_a_reader_leaves_out_is_recorded_as_cant_tell_like_an_ask():
    prof = profile(FIRST)
    member = row(prof, "row-a").members[0]
    kept = scoring.record([], [{"id": "stmt-a", "answer": "unknown", "evidence": ""}], prof, "reader")
    assert scoring.answer_for(kept, member)["answer"] == "unknown", "it counts as an answer: not asked again and again"


# --- what a report is refused for ------------------------------------------------------------------------------------


def problems(prof: p.Profile, *items: dict) -> list[str]:
    return scoring.problems({"asks": list(items)}, prof)


def test_the_statements_of_a_first_match_are_answered_like_asks_each_by_its_own_id():
    prof = profile(FIRST)
    assert problems(prof, {"id": "stmt-a", "answer": "unknown"}, {"id": "stmt-b", "answer": "not"},
                    {"id": "stmt-c", "answer": "met", "evidence": "x"}) == []
    assert problems(prof, {"id": "stmt-a", "answer": "yes"}) == [
        "fit_breakdown: the answer to 'stmt-a' is one of met, not, unknown"]


def test_a_first_match_answered_as_a_whole_is_refused_and_told_to_answer_each_statement():
    assert problems(profile(FIRST), {"id": "row-a", "statement": "stmt-a", "evidence": "x"}) == [
        "fit_breakdown answers 'row-a' as a whole, but it is a first-match row: answer each of its statements by "
        "its own id"]


def test_the_statements_of_an_any_of_are_answered_like_asks_and_the_row_is_never_answered_as_a_whole():
    prof = profile(ANY)
    assert problems(prof, {"id": "stmt-d", "answer": "not", "evidence": "x"},
                    {"id": "stmt-e", "answer": "partly", "evidence": "x"}) == []
    assert problems(prof, {"id": "stmt-d", "answer": "yes", "evidence": "x"}) == [
        "fit_breakdown: the answer to 'stmt-d' is one of met, not, unknown"]
    assert problems(prof, {"id": "box-a", "answer": "met", "evidence": "x"}) == [
        "fit_breakdown answers 'box-a' as a whole, but it is an 'any of' row: answer each of its statements by its "
        "own id"]


def test_a_statement_answered_twice_is_refused_as_an_ask_is():
    assert problems(profile(FIRST), {"id": "stmt-a", "answer": "met"}, {"id": "stmt-a", "answer": "not"}) == [
        "fit_breakdown answers 'stmt-a' twice"]
    assert problems(profile(ANY), {"id": "stmt-d", "answer": "met"}, {"id": "stmt-d", "answer": "not"}) == [
        "fit_breakdown answers 'stmt-d' twice"]


def test_a_statement_the_owner_turned_off_or_does_not_have_is_refused_like_an_ask():
    off = profile(FIRST, ANY, sections_off=["responsibilities", "company"])
    assert problems(off, {"id": "stmt-a", "answer": "met"}) == [
        "fit_breakdown names 'stmt-a', which the owner turned off"], "a statement is in its row's part"
    assert problems(off, {"id": "stmt-d", "answer": "met"}) == [
        "fit_breakdown names 'stmt-d', which the owner turned off"]
    assert problems(profile(FIRST), {"id": "row-z", "answer": "met"}) == [
        "fit_breakdown names an ask 'row-z' the owner doesn't have"]


def test_a_report_that_names_no_row_keeps_every_message_an_ask_has_always_had():
    prof = profile(FIRST)
    assert problems(prof, {"id": "ask-a", "answer": "maybe"}, {"id": "made-up", "answer": "met"}) == [
        "fit_breakdown: the answer to 'ask-a' is one of met, not, unknown",
        "fit_breakdown names an ask 'made-up' the owner doesn't have"]


# --- what the score makes of them -------------------------------------------------------------------------------------


def worked(prof: p.Profile, answers: list[dict], **more) -> dict:
    scored = scoring.compute({"answers": answers}, prof, **more)
    assert scored is not None
    return scored


def line(scored: dict, ident: str) -> dict:
    return next(r for r in scored["asks"] if r["id"] == ident)


def test_a_first_match_with_the_top_statement_unknown_and_a_lower_one_met_earns_the_lower_ones_points():
    """The case that was found: the mandated-seat statement could not be told, the client-delivery one plainly was."""
    prof = profile({"id": "row-a", "kind": "best", "section": "company", "members": [
        {"id": "mandated", "says": "Reports into risk or compliance", "points": 2},
        {"id": "internal", "says": "Internal-facing vocabulary", "points": 1},
        {"id": "client", "says": "Client-delivery vocabulary", "points": -1}]}, asks=())
    answers = scoring.record([], [given("mandated", "unknown", ""), given("internal", "not", "no"),
                                  given("client", "met", "customer environments")], prof, "reader")
    got = line(worked(prof, answers), "row-a")
    assert (got["level"], got["decided"], got["earned"], got["answer"], got["evidence"]) == (
        "client", True, 0, "met", "customer environments")  # edited 2026-10-27 (PRD 30.10): the -1 level took 1 off


def test_a_first_match_earns_the_first_statement_from_the_top_that_is_met_and_unknown_does_not_stop_it():
    prof = profile(FIRST, asks=())
    answers = scoring.record([], [given("stmt-a", "unknown"), given("stmt-b", "met", "half"),
                                  given("stmt-c", "met", "also")], prof, "reader")
    got = line(worked(prof, answers), "row-a")
    assert (got["level"], got["earned"], got["evidence"]) == ("stmt-b", 3, "half")
    top = scoring.record([], [given("stmt-a", "met", "all"), given("stmt-b", "met"), given("stmt-c", "met")],
                         prof, "reader")
    assert (line(worked(prof, top), "row-a")["level"], line(worked(prof, top), "row-a")["earned"]) == ("stmt-a", 4)


def test_a_first_match_with_every_statement_unknown_or_not_met_earns_nothing_and_is_decided():
    prof = profile(FIRST, asks=())
    answers = scoring.record([], [given("stmt-a", "unknown"), given("stmt-b", "not"), given("stmt-c", "partly")],
                             prof, "reader")
    got = line(worked(prof, answers), "row-a")
    assert (got["answer"], got["decided"], got["earned"], got["level"]) == ("not", True, 0, "none")
    unknown = scoring.record([], [given(i, "unknown") for i in ("stmt-a", "stmt-b", "stmt-c")], prof, "reader")
    got = line(worked(prof, unknown), "row-a")
    assert (got["decided"], got["earned"]) == (True, 0)


def test_a_first_match_with_a_statement_no_one_has_answered_and_none_met_is_undecided_so_it_is_asked():
    prof = profile(FIRST, asks=())
    answers = scoring.record([], [given("stmt-a", "not"), given("stmt-b", "not")], prof, "reader")
    got = line(worked(prof, answers), "row-a")
    assert (got["decided"], got["level"], got["earned"]) == (False, "unknown", 0)
    assert "not answered" in got["evidence"]


def test_an_old_single_answer_scores_the_statement_it_named_when_that_was_met():
    prof = profile(FIRST, asks=())
    answers = old_single(prof, "stmt-c", evidence="a little")
    got = line(worked(prof, answers), "row-a")
    assert (got["level"], got["earned"], got["by"], got["evidence"]) == ("stmt-c", 2, "reader", "a little")
    unsure = line(worked(prof, old_single(prof, "unknown", "unknown")), "row-a")
    assert unsure["decided"] is False, "any other old answer leaves the row unanswered, to be asked again"


def test_a_first_match_the_app_decides_is_scored_as_before():
    app = {"id": "size", "kind": "best", "section": "company", "members": [
        {"id": "small", "says": "Fewer than 200 people", "points": 2, **{**SMALL, "value": 200}},
        {"id": "medium", "says": "Fewer than 1,000 people", "points": 1, **SMALL}]}
    prof = profile(app, asks=())
    got = line(worked(prof, [], employer_facts={"employees": 500}), "size")
    assert (got["level"], got["earned"], got["by"], got["decided"]) == ("medium", 1, "app", True)


def test_pay_statements_in_a_first_match_are_decided_from_the_likely_ask_and_the_first_met_wins():
    pay = {"id": "pay", "kind": "best", "section": "compensation", "members": [
        {"id": "high", "says": "Likely ask above $200K", "points": 2, "amount": 200_000},
        {"id": "mid", "says": "Likely ask above $160K", "points": 1, "amount": 160_000},
        {"id": "low", "says": "Likely ask below $140K", "points": -1, "amount": 140_000, "above": False}]}
    prof = profile(pay, asks=())
    got = line(worked(prof, [], facts={"ask": 125_000}), "pay")
    # Edited 2026-10-27 (PRD 30.10): the -1 level took 1 off.
    assert (got["level"], got["earned"], got["by"], got["decided"]) == ("low", 0, "app", True)
    assert (line(worked(prof, [], facts={"ask": 210_000}), "pay")["level"]) == "high"
    waits = line(worked(prof, [], facts={}), "pay")
    assert waits["decided"] is False, "no likely ask yet: the app waits"


def test_the_role_panel_before_the_role_is_scored_shows_a_first_match_as_the_reader_answered_it():
    prof = profile(FIRST, asks=())
    answers = scoring.record([], [given("stmt-a", "not"), given("stmt-b", "met"), given("stmt-c", "not")], prof,
                             "reader")
    rows = {r["id"]: r for r in scoring.view({"answers": answers}, prof)}
    assert (rows["row-a"]["answer"], rows["row-a"]["level"]) == ("met", "stmt-b")


@pytest.mark.parametrize("name", ["jobsearch.analysis"])
def test_what_a_template_shows_an_agent_is_accepted_by_the_app_as_a_report_and_read_as_answers(name):
    body = yaml.safe_load(files.read("templates", name))
    breakdown = body["roles"][0]["fit_breakdown"] if "roles" in body else body["fit_breakdown"]
    statements = [{"id": f"stmt-{x}", "says": f"The work is level {x}", "points": 4 - n}
                  for n, x in enumerate("abc", 1)]
    prof = p.Profile.model_validate({"scoring": {
        "asks": [{"id": f"ask-{x}", "section": "description", "says": f"Ask {x}", "points": 1} for x in "abcde"],
        "groups": [
            {"id": "row-c", "kind": "best", "section": "responsibilities", "members": statements},
            {"id": "box-a", "kind": "any", "section": "company", "points": 2, "members": [
                {"id": "stmt-d", "says": "The employer is a public agency"},
                {"id": "stmt-e", "says": "The employer is a nonprofit"}]}]}})
    assert scoring.problems(breakdown, prof) == []
    kept = {a["id"]: a for a in scoring.answers_in(breakdown)}
    assert (kept["stmt-c"]["answer"], kept["stmt-d"]["answer"]) == ("unknown", "met")
