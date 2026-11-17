"""The Title words dial (docs/PRD.md 46.4, 46.5; the owner, 2026-10-28): a Must on Hard pass that screens a role out
when its title holds any word or phrase the owner listed. The list is the owner's data, so the code holds none; these
tests use a made-up list and made-up titles. A title is always known, so the dial never waits."""

import copy
from datetime import date

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import check as gating
from app.jobsearch.measures import dials
from app.jobsearch.profile import gates as profile_gates
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from tests.jobsearch.test_regate import GATES, by_slug, role
from tests.support import OWNER, PROFILE, load_profile

TODAY = date(2026, 10, 28)
#: A made-up list shaped like one owner's: words, phrases and a bracketed campus tag.
LIST = "intern, internship, new grad, new graduate, university grad, entry level, early career, (2027 start)"


def rules_with(value: str | None = LIST, except_words: str | None = None) -> gating.Rules:
    data = copy.deepcopy(PROFILE)
    gate = {"id": "title-words", "kind": "job_fact", "fact": "title", "op": "excludes", "value": value}
    if except_words is not None:
        gate["except_words"] = except_words
    data["gates"] = {"location": {}, "other": [gate]}
    data["scoring"] = {"asks": []}
    return gating.Rules.of(p.Profile.model_validate(data), today=TODAY, hard_pass=1, criteria=1)


def line_for(title: str | None, value: str | None = LIST, except_words: str | None = None) -> gating.Line:
    s = gating.Subject(company="Fabrikam", location=None, work_mode=None, comp=None, comp_min=None, comp_max=None,
                       posted=None, found=None, title=title)
    return next(line for line in gating.results(s, rules_with(value, except_words)) if line.id == "title-words")


def test_the_dial_is_a_job_dial_with_a_must_and_no_prefer():
    dial = dials.DIALS["title"]
    assert (dial.side, dial.fact, dial.kind, dial.choices) == ("job", "title", "words", ("must",))


@pytest.mark.parametrize("title", [
    "Software Intern", "Data Engineering Internship, Summer", "INTERN - Analytics", "New Grad Software Engineer",
    "Software Engineer, New Graduate", "University Grad Program Associate", "Entry Level Analyst",
    "Entry-Level Analyst", "Analyst (entry   level)", "Early Career Program Manager",
    "Software Engineer (2027 Start)", "Product Manager - Early-Career",
])
def test_a_title_with_a_listed_word_fails(title):
    assert line_for(title).result == gating.FAIL


@pytest.mark.parametrize("title", [
    "Individual Contributor, Platform", "Staff Software Engineer, Individual Contributor", "Intermediate Developer",
    "Internal Tools Engineer", "International Payroll Lead", "Senior Product Manager", "Director of Engineering",
    "Interning Coordinator", "Software Engineer (2028 Start)", "New Grading Systems Lead",
])
def test_a_title_without_a_listed_word_passes(title):
    assert line_for(title).result == gating.PASS


def test_the_failure_names_the_word_that_matched_and_is_a_rule_stop():
    line = line_for("Senior Analyst, Entry-Level Pipeline")
    assert "entry level" in line.fact
    stop = gating.first_stop([line])
    assert stop is not None and stop.gate.value == "rule"


def test_with_no_title_it_passes_and_never_waits():
    assert line_for(None).result == gating.PASS
    assert line_for("").result == gating.PASS


def test_an_empty_list_stops_nothing():
    assert line_for("Software Intern", value="").result == gating.PASS


def test_matching_ignores_case_and_stray_commas_and_spaces():
    assert line_for("SOFTWARE INTERN", value="  Intern ,, ").result == gating.FAIL


def test_a_phrase_is_matched_literally():
    assert line_for("Analyst (2027 Start)", value="(2027 start)").result == gating.FAIL
    assert line_for("Analyst 2027 Start", value="(2027 start)").result == gating.PASS
    assert line_for("Analyst (2027 Start)", value="c++").result == gating.PASS


VP = "vice president, director"


def test_a_listed_word_inside_an_excepted_phrase_is_no_hit():
    ok = "assistant vice president, associate vice president"
    assert dials.title_hits("Assistant Vice President, Data", VP, ok) == []
    assert dials.title_hits("Associate Vice-President, Data", VP, ok) == []
    assert dials.title_hits("Vice President, Data", VP, ok) == ["vice president"]
    assert dials.title_hits("Vice President, Data", VP) == ["vice president"]


def test_a_hit_outside_the_excepted_span_still_counts():
    ok = "assistant vice president"
    assert dials.title_hits("Assistant Vice President and Director", VP, ok) == ["director"]
    assert dials.title_hits("Vice President, Assistant Vice President", VP, ok) == ["vice president"]


def test_exceptions_are_whole_words_and_ignore_blanks():
    assert dials.title_hits("Vice President", VP, " , ") == ["vice president"]
    assert dials.title_hits("Vice President", VP, "") == ["vice president"]
    assert dials.title_hits("Vice President", VP, None) == ["vice president"]


def test_the_gate_lets_an_excepted_title_pass_and_names_the_word_that_hits_otherwise():
    assert line_for("Assistant Vice President, Data", VP, "assistant vice president").result == gating.PASS
    hit = line_for("Vice President, Data", VP, "assistant vice president")
    assert hit.result == gating.FAIL and "vice president" in hit.fact
    assert line_for("Assistant Vice President, Data", VP).result == gating.FAIL


def _stop(title: str, except_words: str | None = None) -> gating.Stop:
    stop = gating.first_stop([line_for(title, VP_AND_MORE, except_words)])
    assert stop is not None
    return stop


VP_AND_MORE = "vice president, director, sales"


def test_the_stop_reason_names_the_word_that_hit_not_the_whole_list():
    stop = _stop("Sales Analyst")
    assert stop.role_words == 'The title has "sales", which the Title words exclude.'
    assert stop.posting_words == stop.role_words
    assert "none of" not in stop.role_words and "director" not in stop.role_words


def test_the_stop_reason_names_two_hits():
    stop = _stop("Director of Sales")
    assert stop.role_words == 'The title has "director" and "sales", which the Title words exclude.'


def test_the_stop_reason_leaves_out_a_word_inside_an_excepted_phrase():
    stop = _stop("Assistant Vice President, Sales", "assistant vice president")
    assert stop.role_words == 'The title has "sales", which the Title words exclude.'


def test_the_sentence_names_the_exceptions_only_when_set():
    assert dials.says("title", "excludes", "vice president") == "The title has none of: vice president."
    assert dials.says("title", "excludes", "vice president", "assistant vice president, associate vice president") == (
        "The title has none of: vice president, except in: assistant vice president or associate vice president.")


def test_an_exception_list_is_stored_on_the_gate_and_comes_back():
    data = copy.deepcopy(PROFILE)
    gate = {"id": "title-words", "kind": "job_fact", "fact": "title", "op": "excludes", "value": "vice president",
            "except_words": "assistant vice president"}
    data["gates"] = {"location": {}, "other": [gate]}
    back = p.Profile.model_validate(p.Profile.model_validate(data).model_dump(mode="json"))
    assert back.gates.other[0].except_words == "assistant vice president"
    assert "except in: assistant vice president" in profile_gates.gate_says(back.gates.other[0])
    del gate["except_words"]
    assert p.Profile.model_validate(data).gates.other[0].except_words is None


def test_the_line_is_said_in_plain_words():
    assert dials.says("title", "excludes", "intern, new grad") == "The title has none of: intern or new grad."
    assert dials.is_dial("title", "excludes") and not dials.is_dial("title", "in")


def test_a_stored_role_a_posting_and_a_report_are_read_by_their_titles():
    class Row:  # the columns the subjects read
        company, location, move_type, comp, posted, found, target_role = "Fabrikam", None, None, None, None, None, None
        gate_answers = analysis = gate_facts = employer_id = None
        title = "Summer Intern, Platform"
        work_mode = comp_min = comp_max = url = apply_url = first_seen = None

    assert gating.role_subject(Row(), None).title == "Summer Intern, Platform"
    assert gating.posting_subject(Row(), None).title == "Summer Intern, Platform"
    assert gating.report_subject({"title": "Intern"}, None, None).title == "Intern"


async def test_saving_the_list_stops_the_postings_and_roles_whose_titles_hold_a_word_and_leaves_the_rest(js, client):
    """One save of Hard pass with the dial on: the re-gate screens out the titled-for-juniors ones, with the word as
    the reason; individual contributor and internal titles stay where they are (docs/PRD.md 46.4)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"location": {}})
        session.add_all([role("a", stages.SECOND_IMPRESSION, title="Platform Intern (2027 Start)"),
                         role("b", stages.SECOND_IMPRESSION, title="Staff Engineer, Individual Contributor"),
                         role("c", stages.SECOND_IMPRESSION, title="Internal Tools Engineer")])
        await session.commit()

    gate = {"id": "dial-title-must", "kind": "job_fact", "section": "posting", "fact": "title", "op": "excludes",
            "value": "intern, new grad, entry level, (2027 start)"}
    saved = await client.put(GATES, json={"data": {"location": {}, "other": [gate]}, "why": "made-up test list"})
    assert saved.status_code == 200
    async with db.session_factory()() as session:
        roles = await by_slug(session)
        assert roles["a"].status == stages.GATED_OUT
        assert roles["a"].screened_out_gate_id == "dial-title-must"
        assert "intern" in roles["a"].screened_out_reason
        assert roles["b"].status == roles["c"].status == stages.SECOND_IMPRESSION


async def test_saving_with_an_exception_keeps_the_excepted_roles_and_stops_the_rest(js, client):
    """The exception list rides on the saved gate line: the excepted title stays, the bare one goes."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"location": {}})
        session.add_all([role("a", stages.SECOND_IMPRESSION, title="Assistant Vice President, Data"),
                         role("b", stages.SECOND_IMPRESSION, title="Vice President, Data")])
        await session.commit()

    gate = {"id": "dial-title-must", "kind": "job_fact", "section": "posting", "fact": "title", "op": "excludes",
            "value": "vice president", "except_words": "assistant vice president"}
    saved = await client.put(GATES, json={"data": {"location": {}, "other": [gate]}, "why": "made-up test list"})
    assert saved.status_code == 200
    async with db.session_factory()() as session:
        stored = (await profile_store.current(session, OWNER)).gates.other[0]
        assert stored.except_words == "assistant vice president"
        roles = await by_slug(session)
        assert roles["a"].status == stages.SECOND_IMPRESSION
        assert roles["b"].status == stages.GATED_OUT
        assert "vice president" in roles["b"].screened_out_reason
