"""The style rules that ship with the app: five rules the owner can
switch off, what each flags and what it leaves alone, the exemption for text copied whole from a master line, and the
"Never write" matcher's `built_in` keyword that other checks of owner text use. Everything is made up."""

import pytest

from app.jobsearch.profile import never_write, style_rules
from app.jobsearch.profile.documents import Writing

EM_DASH, NO_MARKUP, NO_YEAR, NO_CONCEDING, NO_WEAK = (
    "no-em-dash", "no-inline-markup", "no-year-by-degree", "no-pre-conceding", "no-weak-words")
NAMES = {
    EM_DASH: "No em dash", NO_MARKUP: "No markup", NO_YEAR: "No year beside a degree",
    NO_CONCEDING: "No phrase that concedes a gap first", NO_WEAK: "No weak or inflated words",
}


def snippets(rule_id: str, text: str, writing: Writing | None = None) -> list[str]:
    """What the rule `rule_id` flags in `text`, as the words that matched."""
    found = style_rules.find_matches(writing or Writing(), [("the text", text)])
    return [m.snippet for m in found if m.rule == NAMES[rule_id]]


# --- the five rules, in the owner's words --------------------------------------------------------------------------


def test_there_are_five_rules_with_their_ids_names_and_a_sentence_each():
    assert [(r.id, r.name) for r in style_rules.STYLE_RULES] == list(NAMES.items())
    assert all(r.rule.endswith(".") for r in style_rules.STYLE_RULES)
    assert style_rules.shipped() == [{"id": r.id, "name": r.name, "rule": r.rule} for r in style_rules.STYLE_RULES]
    by_id = {r.id: r.rule for r in style_rules.STYLE_RULES}
    assert by_id[EM_DASH] == "Use a comma, a period or a new sentence. A dash between two dates is fine."
    assert by_id[NO_MARKUP] == ("Bold, italics and other markup print as typed in the Word file. Write plain text; "
                                "the app sets its own labels.")
    assert by_id[NO_YEAR] == "A year next to a degree tells a reader your age. Name the degree and the school."
    assert by_id[NO_CONCEDING] == ("Say nothing about a gap, or say one confident sentence. Never open with what you "
                                   "lack.")
    assert by_id[NO_WEAK] == ("Words like 'responsible for', 'various' and 'leveraged' say little and sound padded.")


# --- no em dash ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, found", [
    ("Led the program — and grew it", "—"),  # an em dash, spaced
    ("Led the program—and grew it", "—"),  # an em dash anywhere, unspaced too
    ("Ran the years 2019—2022", "—"),  # an em dash is flagged even between dates
    ("Led the program - and grew it", " - "),  # a spaced hyphen between words
    ("Led the program – and grew it", " – "),  # a spaced en dash between words
    ("Led the program -- and grew it", " -- "),  # two hyphens between words
    ("Led the program--and grew it", "--"),
    ("Cut cost (by half) - and time", " - "),  # between a bracket and a word is still a dash between words
])
def test_a_dash_used_as_punctuation_is_flagged(text, found):
    assert snippets(EM_DASH, text) == [found]


@pytest.mark.parametrize("text", [
    "Ran it 2019 - 2022",  # a dash between two dates
    "Ran it from Jan 2019 - Mar 2021",
    "Ran it 2019–2022",  # an en dash between two years, unspaced
    "Ran it 2019 – 2022",
    "Ran it 2019 - present",
    "Ran it Sept - Dec",
    "A long-term plan",  # an unspaced hyphen inside a word
    "Led a cross-functional team",
    "Built a go-to-market plan",
    "A client–server design",  # an unspaced en dash is no spaced dash between words
    "Cut cost by half, then time.",
])
def test_a_dash_between_dates_and_a_hyphen_inside_a_word_are_left_alone(text):
    assert snippets(EM_DASH, text) == []


# --- no markup ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, found", [
    ("Led a **bold** claim mid sentence", "**bold**"),
    ("**Label:** led the program", "**Label:**"),  # a label that opens a line is flagged too
    ("Led a __bold__ claim", "__bold__"),
    ("Led an *italic* claim", "*italic*"),
    ("*Italic* opens the text", "*Italic*"),
    ("Led an _italic_ claim", "_italic_"),
    ("_Italic_ opens the text", "_Italic_"),
    ("Ran `the code` daily", "`the code`"),
    ("Ran the <b>bold</b> thing", "<b>"),
    ("A </strong> closer", "</strong>"),
    ("A <strong>strong</strong> thing", "<strong>"),
    ("An <em>emphasised</em> thing", "<em>"),
    ("An <i>italic</i> thing", "<i>"),
    ("An <u>underlined</u> thing", "<u>"),
    ("Called (*italic*) with a bracket", "*italic*"),  # after punctuation is an opener
])
def test_markup_is_flagged_wherever_it_is(text, found):
    assert snippets(NO_MARKUP, text)[0] == found


@pytest.mark.parametrize("text", [
    "Uses snake_case_name everywhere",
    "Worked 5*3 shifts",
    "Costs a * b in total",
    "A lone * asterisk",
    "A lone _ underscore",
    "Where x < y and y > z",
    "A <span>span</span> and a <br> break",
    "An <img> and an <ul> list",
    "Revenue* grew",  # the opener must follow a space or punctuation
    "* a list marker that is not italic",
])
def test_text_that_only_looks_like_markup_is_left_alone(text):
    assert snippets(NO_MARKUP, text) == []


# --- no year beside a degree --------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "MBA, 2010",
    "MS in Leadership (2014)",
    "Bachelor of Arts, class of 2012",
    "PhD 2009",
    "Ph.D. 2009",
    "B.S. in Biology, 2011",
    "M.A. in Teaching 1998",
    "Master's degree earned in 2016",
    "Associate degree (2008)",
    "Associate's degree 2008",
    "Doctorate granted 2001",
    "JD, 2005",
    "BA in History 2003",
    "Graduated, class of 2012, with an MBA",
    "MBA class of",  # "class of" counts as a year marker
])
def test_a_year_or_class_of_beside_a_degree_is_flagged(text):
    assert snippets(NO_YEAR, text), text


@pytest.mark.parametrize("text", [
    "led the program in 2014",
    "MA 2019",  # a state, not a degree
    "MS 2019 results",
    "The MBA program grew 20%",
    "Mastered Excel in 2019",
    "Scrum master in 2018",
    "A class of events in 2014",
    "Earned an MBA. Joined in 2010.",  # not the same sentence
    "Earned an MBA and many other things that took a very long while to finish in 2010",  # too far apart
    "BS in",  # a degree with no year
])
def test_a_year_away_from_a_degree_or_a_short_acronym_alone_is_left_alone(text):
    assert snippets(NO_YEAR, text) == [], text


def test_a_year_within_six_words_of_the_degree_is_flagged_and_one_further_is_not():
    assert snippets(NO_YEAR, "MBA one two three four five 2010") == ["MBA one two three four five 2010"]
    assert snippets(NO_YEAR, "MBA one two three four five six 2010") == []
    assert snippets(NO_YEAR, "2010 one two three four five MBA") == ["2010 one two three four five MBA"]


def test_the_periods_of_a_degree_do_not_end_the_sentence_but_a_full_stop_does():
    assert snippets(NO_YEAR, "Earned a B.S. in Biology in 2011.") != []
    assert snippets(NO_YEAR, "B.S. Computer Science 2011") == ["B.S. Computer Science 2011"]
    assert snippets(NO_YEAR, "Earned an MBA. Studied in 2011.") == []
    assert snippets(NO_YEAR, "Earned an MBA! Studied in 2011.") == []


# --- no phrase that concedes a gap first --------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Although I lack a degree, I led programs",
    "Despite lacking the title, I ran it",
    "Although I don't have a licence",
    "Although I don’t have a licence",
    "Although I do not have the title",
    "Despite not having the title",
    "While I don't have the tool, I know it",
    "While I do not have the tool",
    "I may not have the full set of skills",
    "Even though I have not led a team",
    "Even though I don't have the title",
    "although i lack depth",
])
def test_a_phrase_that_concedes_a_gap_first_is_flagged(text):
    assert snippets(NO_CONCEDING, text), text


@pytest.mark.parametrize("text", [
    "Although the team lacked budget, it shipped",
    "Despite the headwinds, we grew",
    "I have led programs and I lack nothing here",
    "Whilst growing the team",
])
def test_a_phrase_about_something_else_is_not_a_concession(text):
    assert snippets(NO_CONCEDING, text) == [], text


# --- no weak or inflated words ------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, found", [
    ("Responsible for the budget", "Responsible for"),
    ("Helped with onboarding", "Helped with"),
    ("Worked on the rollout", "Worked on"),
    ("Various teams used it", "Various"),
    ("Multiple teams used it", "Multiple"),
    ("Successfully launched the tool", "Successfully"),
    ("Spearheaded a program", "Spearheaded"),
    ("Leveraging data for the board", "Leveraging"),
    ("Leveraged data for the board", "Leveraged"),
    ("Built synergies across teams", "synergies"),
    ("Created synergy", "synergy"),
])
def test_a_weak_or_inflated_word_is_flagged_in_its_simple_variants(text, found):
    assert snippets(NO_WEAK, text) == [found]


@pytest.mark.parametrize("text", [
    "Responsibility for the budget",
    "Variously described",
    "Multiplied revenue",
    "Reworked the plan",
    "A work in progress",
    "Success came early",
])
def test_words_that_only_look_like_a_weak_word_are_left_alone(text):
    assert snippets(NO_WEAK, text) == []


# --- the switches --------------------------------------------------------------------------------------------------

EVERYTHING_WRONG = "Spearheaded — a **bold** MBA, 2010. Although I lack a title."


def test_with_nothing_switched_off_every_rule_reports():
    found = {m.rule for m in style_rules.find_matches(Writing(), [("the text", EVERYTHING_WRONG)])}
    assert found == set(NAMES.values())


def test_a_rule_switched_off_passes_what_it_flags_and_the_others_still_fail():
    writing = Writing(style_off=[EM_DASH])
    found = {m.rule for m in style_rules.find_matches(writing, [("the text", EVERYTHING_WRONG)])}
    assert found == set(NAMES.values()) - {NAMES[EM_DASH]}
    assert [r.id for r in style_rules.on(writing)] == [i for i in NAMES if i != EM_DASH]


def test_every_rule_switched_off_reports_nothing():
    writing = Writing(style_off=list(NAMES))
    assert style_rules.on(writing) == []
    assert style_rules.find_matches(writing, [("the text", EVERYTHING_WRONG)]) == []


def test_a_writing_document_saved_before_the_switches_loads_with_every_rule_on():
    older = Writing.model_validate({"voice_rules": [{"id": "plain", "says": "Plain."}]})
    assert older.style_off == [] and older.credentials == []
    assert [r.id for r in style_rules.on(older)] == list(NAMES)


def test_an_id_the_app_does_not_know_is_kept_and_ignored():
    writing = Writing.model_validate({"style_off": ["no-such-rule", EM_DASH]})
    assert writing.style_off == ["no-such-rule", EM_DASH]
    assert [r.id for r in style_rules.on(writing)] == [i for i in NAMES if i != EM_DASH]
    assert style_rules.find_matches(writing, [("the text", "Led it — well")]) == []


# --- the where, the words and the exemption ------------------------------------------------------------------------


def test_a_match_names_where_the_words_that_matched_and_the_rule():
    [m] = style_rules.find_matches(Writing(), [("bullet 2 under Senior Manager at Northwind Labs", "Led it - well")])
    assert m == never_write.Match("bullet 2 under Senior Manager at Northwind Labs", " - ", "No em dash")


def test_a_text_the_caller_exempts_is_not_read_by_a_style_rule():
    texts = [("bullet 1", "Led it — well"), ("bullet 2", "Led it — well")]
    found = style_rules.find_matches(Writing(), texts, exempt={"bullet 1"})
    assert [m.where for m in found] == ["bullet 2"]


def test_the_owners_never_write_and_the_built_in_kinds_still_read_an_exempt_text():
    writing = Writing.model_validate({"banned": [{"id": "lev", "phrase": "leveraged", "why": "jargon"}]})
    texts = [("bullet 1", "Leveraged data for 12 years — well")]
    assert style_rules.find_matches(writing, texts, exempt={"bullet 1"}) == []
    hits = never_write.find_hits(writing, texts)
    assert any("'Leveraged' (jargon)" in h for h in hits)
    assert any("A count of years of experience" in h for h in hits)


def test_a_phrase_the_owner_already_bans_is_reported_by_their_rule_alone():
    writing = Writing.model_validate({"banned": [{"id": "spear", "phrase": "spearheaded", "why": "puffery"}]})
    text = "Spearheaded a program and led various teams"
    mine = [m.snippet for m in style_rules.find_matches(writing, [("t", text)]) if m.rule == NAMES[NO_WEAK]]
    assert mine == ["various"], "the starter rule leaves 'spearheaded' to the owner's rule"
    owners = [h for h in never_write.find_hits(writing, [("t", text)]) if "spearheaded" in h.lower()]
    assert owners == ["t: 'Spearheaded' (puffery)"], "reported once, with the owner's reason"


def test_a_variant_of_the_owners_phrase_is_theirs_too_and_a_legacy_pattern_holds_a_phrase_as_well():
    writing = Writing.model_validate({"banned": [{"id": "lev", "phrase": "leverage", "why": ""}],
                                      "legacy_patterns": [{"id": "plain", "pattern": "synergy", "regex": False}]})
    found = [m.snippet for m in style_rules.find_matches(writing, [("t", "Leveraged synergy across various teams")])]
    assert found == ["various"]


# --- the "Never write" matcher's switch for text that is not a document a screener reads ---------------------------


def test_find_hits_without_the_built_in_kinds_reports_the_owners_phrases_and_the_legacy_patterns_only():
    writing = Writing.model_validate({
        "banned": [{"id": "lev", "phrase": "leveraged", "why": "jargon"}],
        "legacy_patterns": [{"id": "since", "pattern": r"\bsince\s+\d{4}\b", "regex": True, "why": "dates"}]})
    texts = [("t", "Leveraged it for 12 years, working since 1999, with zero incidents")]
    lean = never_write.find_hits(writing, texts, built_in=False)
    assert lean == ["t: 'Leveraged' (jargon)", "t: 'since 1999' (dates)"]
    full = never_write.find_hits(writing, texts)
    assert full[:2] == lean and len(full) == 4
    assert never_write.find_hits(writing, texts, built_in=True) == full


def test_find_matches_returns_a_match_for_each_hit_and_find_hits_prints_them_as_it_always_did():
    writing = Writing.model_validate({"banned": [{"id": "lev", "phrase": "leveraged", "why": "jargon"}]})
    texts = [("bullet 1", "Leveraged it for 12 years")]
    years = "checked automatically: A count of years of experience"
    matches = never_write.find_matches(writing, texts)
    assert matches == [never_write.Match("bullet 1", "Leveraged", "jargon"),
                       never_write.Match("bullet 1", "12 years", years)]
    assert never_write.find_hits(writing, texts) == [
        "bullet 1: 'Leveraged' (jargon)", f"bullet 1: '12 years' ({years})"]
    assert never_write.find_matches(writing, texts, built_in=False) == matches[:1]


def test_a_legacy_pattern_that_cannot_be_compiled_is_still_a_failed_check_in_both_forms():
    writing = Writing.model_validate({"legacy_patterns": [{"id": "bad", "pattern": "(unclosed", "regex": True}]})
    [line] = never_write.find_hits(writing, [("t", "anything")], built_in=False)
    assert line.startswith("the rule '(unclosed' isn't a valid pattern")
    assert line.endswith("nothing was checked against it")
    [match] = never_write.find_matches(writing, [("t", "anything")], built_in=False)
    assert match.where == "" and match.snippet == "" and match.rule == line
