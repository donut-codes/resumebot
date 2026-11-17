"""The "Never write" matcher (plain phrases and their variants), the checks that ship with the app, the packet check
that uses both, and the older entries that were patterns. Everything is made up."""

import re

import pytest

from app.jobsearch.profile import documents, never_write
from app.jobsearch.profile.documents import Writing
from app.jobsearch.writing import packets
from tests.builders import RESUME


def hits(phrase: str, text: str) -> bool:
    pattern = never_write.phrase_pattern(phrase)
    return bool(pattern and pattern.search(text))


# --- the owner's phrases ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("phrase, text", [
    ("spearheaded", "She Spearheaded the move."),  # case
    ("spearheaded", "SPEARHEADED"),
    ("leveraged", "Leveraged the data."),  # the phrase itself
    ("leverage", "Leveraging our strengths."),  # -ing, dropping the e
    ("spearheaded", "to spearhead it"),  # the base of a phrase written in -ed
    ("leverage", "He leveraged it, and then leverages more."),  # -ed and -s
    ("leveraging", "to leverage the tools"),  # a phrase written in -ing finds the base
    ("synergy", "cross-team synergies"),  # y -> ies
    ("synergies", "A synergy of skills"),
    ("stop", "stopped and stopping"),  # a doubled last letter
    ("manage team", "managed teams and managing a team"),  # every word varies
    ("worked on", "I work on it, working on it"),
    ("go to market", "A go-to-market plan"),  # a hyphen is a space
    ("go-to-market", "our go to market plan"),
    ("cross-functional", "cross  functional\nwork"),  # any run of space
    ("don't", "We don’t stop"),  # a curly apostrophe is a straight one
    ("don’t", "We don't stop"),
    ("the \"best\"", "the “best”"),
    ("c++", "Wrote C++ daily"),  # a symbol is matched as written
    ("24/7", "on call 24/7."),
])
def test_a_phrase_catches_its_case_variants_hyphens_apostrophes_and_simple_forms(phrase, text):
    assert hits(phrase, text)


@pytest.mark.parametrize("phrase, text", [
    ("lead", "leader of the pack"),  # whole words only
    ("lead", "mislead"),
    ("manage", "management"),
    ("man", "many men"),
    ("use", "reuse, user, usefully"),
    ("c++", "Wrote C daily"),
    ("it's", "its"),
    ("team", "steam"),
    ("being", "a bee"),  # a short base is not invented
    ("need", "nee"),
    ("tool", "a stool"),
])
def test_a_phrase_does_not_match_inside_another_word(phrase, text):
    assert not hits(phrase, text)


def test_an_empty_phrase_or_only_separators_matches_nothing():
    for phrase in ("", "   ", "---", " - "):
        assert never_write.phrase_pattern(phrase) is None


@pytest.mark.parametrize("phrase, text", [
    (".*", "anything at all"),
    ("a|b", "a"),
    ("(unclosed", "no parenthesis here"),
    ("[a-z]+", "letters"),
    (r"\d+", "123"),
    ("x{2,}", "xxx"),
    ("^start$", "start"),
    ("(?i)abc", "abc"),
    ("a\\", "a"),
])
def test_a_phrase_is_never_read_as_a_pattern(phrase, text):
    assert never_write.phrase_pattern(phrase) is not None  # never fails to compile
    assert not hits(phrase, text)


def test_a_phrase_with_regex_characters_matches_itself_only():
    assert hits("(unclosed", "an (unclosed bracket") and hits("a|b", "an a|b mark")
    assert hits(".*", "wild .* card") and not hits(".*", "wild card")


def test_a_very_long_phrase_and_a_long_text_are_matched_in_linear_time():
    phrase = " ".join(["word"] * 200)
    assert not hits(phrase, ("word " * 199 + "other ") * 50)
    assert hits(phrase, " ".join(["words"] * 200))


# --- the checks that ship with the app -----------------------------------------------------------------------------


def built_in(check_id: str):
    return next(c for c in never_write.BUILT_IN if c.id == check_id)


def caught(check_id: str, text: str) -> bool:
    return built_in(check_id).pattern.search(text) is not None


YEARS = "years-of-experience"
HEADCOUNT = "headcount-flex"
ABSOLUTES = "outcome-absolutes"
BUILT = "counts-of-things-built"


def test_the_built_in_checks_have_plain_words_names_and_the_qa_check_they_back():
    assert [c.name for c in never_write.BUILT_IN] == [
        "A count of years of experience", "A headcount used as a flex", "An absolute about outcomes",
        "Counts of things built"]
    assert len({c.id for c in never_write.BUILT_IN}) == 4
    for c in never_write.BUILT_IN:
        assert c.rule.endswith(".") and c.backs == "Nothing you ban appears"
    assert [c["name"] for c in never_write.built_in_checks()] == [c.name for c in never_write.BUILT_IN]


@pytest.mark.parametrize("text", [
    "10+ years of experience in operations", "Over 15+ years of experience", "12 years leading programs",
    "twelve years of experience", "Twenty years in logistics", "fifteen+ years", "2 yrs of work", "7-year track record",
    "3 years", "with 25 Years of", "ten years", "nineteen years",
])
def test_a_count_of_years_is_caught(text):
    assert caught(YEARS, text)


@pytest.mark.parametrize("text", [
    "From 2019 to 2022", "Year over year growth", "Led the program for years", "The year 2024", "Two years of study",
    "Years of service award", "Fiscal year 24", "yearly review", "5th year",
])
def test_years_without_a_count_or_with_a_small_spelled_number_are_not_caught(text):
    assert not caught(YEARS, text)


@pytest.mark.parametrize("text", [
    "Led a 12-person team", "a 12 person team", "Led a 15+ person group", "15+ person org", "built a 3-person pod",
    "Managed 50+ people", "30+ practitioners", "200+ employees", "10+ staff", "6+ consultants", "1,000+ employees",
    "5-persons",
])
def test_a_headcount_flex_is_caught(text):
    assert caught(HEADCOUNT, text)


@pytest.mark.parametrize("text", [
    "Led a team of up to 16 direct reports", "Worked with people across teams", "in-person events", "A person of note",
    "Led 12 people", "5 stars", "one-on-one coaching", "Coached staff weekly", "about 30 employees",
])
def test_a_headcount_without_the_flex_is_not_caught(text):
    assert not caught(HEADCOUNT, text)


@pytest.mark.parametrize("text", [
    "Delivered with zero incidents", "Zero security incidents", "zero critical security incidents",
    "No defects shipped", "no incident reported", "Closed every finding", "Resolved all findings",
    "fixed all critical defects", "every audit finding", "zero attrition", "no escalations",
    "All defects were closed", "no customer escalation",
    "Retained every client", "retained all of the staff", "Retained 100% of the team", "retained 100 % of accounts",
    "Closed all incidents",
])
def test_an_absolute_about_outcomes_is_caught(text):
    assert caught(ABSOLUTES, text)


@pytest.mark.parametrize("text", [
    "Reduced incidents by 40%", "Handled escalations from clients", "Found defects early", "know findings",
    "Several findings were closed", "Retained most clients", "All-hands meetings", "Cut attrition", "now incident-free",
    "no one reported any problems with the findings", "Tracked every release",
])
def test_outcomes_without_an_absolute_are_not_caught(text):
    assert not caught(ABSOLUTES, text)


@pytest.mark.parametrize("text", [
    "Built seven production AI skills", "a 13-competency framework", "Shipped 5 AI agents", "12 production tools",
    "Created 3 assistants", "built 10+ AI tools", "twelve internal tools", "9 new skills", "a 7-competency model",
    "Delivered 4 competencies", "two AI agents",
])
def test_a_count_of_things_built_is_caught(text):
    assert caught(BUILT, text)


@pytest.mark.parametrize("text", [
    "Led a team of up to 16 direct reports", "two-phase cloud data platform", "Built an AI agent", "one of the tools",
    "Used 3 of the tools daily", "Skills in Python", "Tools and agents for the team", "Led two-phase delivery",
    "the 5 largest accounts", "a tool for the team",
])
def test_things_built_without_a_count_are_not_caught(text):
    assert not caught(BUILT, text)


# --- the packet check ----------------------------------------------------------------------------------------------


def _cut(text: str) -> packets.ResumeCut:
    return packets.ResumeCut.model_validate({
        "headline": "Programs Leader", "summary": "Builds programs that people use, across client engagements.",
        "summary_from": ["b-risk"], "competencies": [{"area": "Governance", "items": ["Risk Frameworks"]}],
        "roles": [{"role": "northwind-lead", "bullets": [{"text": text, "from": ["b-risk"]}]}]})


def _check(writing: dict, text: str) -> dict:
    docs = documents.Documents.model_validate({"resume": RESUME, "writing": writing})
    checks = packets.packet_checks(_cut(text), ["Dear hiring team,", "I would like to help."], ["b-risk"], docs)
    return next(c for c in checks if c["check"] == "Nothing you ban appears")


def test_the_packet_check_refuses_an_owners_phrase_in_a_variant_and_says_which_it_was():
    writing = {"banned": [{"id": "lev", "phrase": "leverage", "why": "jargon"}]}
    result = _check(writing, "Leveraging the risk working group")
    assert result["ok"] is False and result["blocking"] is True
    assert "'Leveraging' (jargon)" in result["detail"] and "bullet 1" in result["detail"]
    assert _check(writing, "Ran the risk working group")["ok"] is True


def test_the_packet_check_refuses_a_built_in_check_and_names_it():
    result = _check({}, "Led a 12-person team")
    assert result["ok"] is False and result["blocking"] is True
    assert "'12-person' (checked automatically: A headcount used as a flex)" in result["detail"]
    result = _check({}, "Delivered with zero incidents and twelve years of experience")
    assert "checked automatically: An absolute about outcomes" in result["detail"]
    assert "checked automatically: A count of years of experience" in result["detail"]
    assert "(checked automatically: Counts of things built)" in _check({}, "Shipped 5 AI agents")["detail"]


def test_the_packet_check_names_the_owners_phrase_and_the_built_in_check_when_both_hit():
    writing = {"banned": [{"id": "grew", "phrase": "grew", "why": "vague"}]}
    detail = _check(writing, "Grew the team to 40+ people")["detail"]
    assert "'Grew' (vague)" in detail and "checked automatically: A headcount used as a flex" in detail


def test_a_draft_that_passes_says_what_it_was_checked_against():
    result = _check({"banned": [{"id": "a", "phrase": "zzqx"}], "legacy_patterns": [{"id": "b", "pattern": "qqzx"}]},
                    "Led the risk working group")
    assert result["ok"] is True
    assert result["detail"] == "checked against 2 of your phrases and patterns and 4 built-in checks"


def test_a_legacy_pattern_is_still_enforced_exactly_as_before():
    writing = {"legacy_patterns": [
        {"id": "dates", "pattern": r"\bsince\s+\d{4}\b", "regex": True, "why": "dates the owner"},
        {"id": "lit", "pattern": "a.b", "regex": False, "why": "literal"}]}
    assert "'since 1999' (dates the owner)" in _check(writing, "Working since 1999 on it")["detail"]
    assert "'A.B' (literal)" in _check(writing, "The A.B test")["detail"]
    assert _check(writing, "The aXb test")["ok"] is True


def test_a_legacy_pattern_that_cannot_be_compiled_fails_the_check():
    result = _check({"legacy_patterns": [{"id": "bad", "pattern": "(unclosed", "regex": True}]}, "Ran a program")
    assert result["ok"] is False and "isn't a valid pattern" in result["detail"]


def test_the_counts_check_example_is_neutral():
    check = built_in(BUILT)
    assert "AI" not in check.rule and "competenc" not in check.rule.lower()
    quoted = re.findall(r'"([^"]+)"', check.rule)
    assert len(quoted) == 2
    for example in quoted:
        assert check.pattern.search(example), example


def test_the_writer_is_told_the_built_in_checks_beside_never_write():
    writing = Writing.model_validate({"banned": [{"id": "a", "phrase": "x", "why": "y"}],
                                      "legacy_patterns": [{"id": "b", "pattern": "z", "regex": True}]})
    qa = documents.rules_for(writing, "review")["Settings for QA of documentation"]
    assert qa["Never write"] == [{"phrase": "x", "why": "y"}, {"pattern": "z", "regex": True, "why": ""}]
    assert [c["name"] for c in qa["Checked automatically"]] == [c.name for c in never_write.BUILT_IN]
    assert list(qa)[-3:] == ["Checked automatically", "Always checked", "Style rules on"], (
        "the two lists of the packet review come after the four text kinds, which are unchanged")


# --- the shape of an entry -------------------------------------------------------------------------------------------


def test_the_new_shape_loads_as_it_is_and_a_phrase_cannot_carry_a_pattern_field():
    new = {"banned": [{"id": "a", "phrase": "x", "why": "y"}], "legacy_patterns": []}
    assert Writing.model_validate(new).model_dump(mode="json")["banned"] == new["banned"]
    with pytest.raises(ValueError):
        Writing.model_validate({"banned": [{"id": "a", "phrase": "x", "unknown": 1}]})
