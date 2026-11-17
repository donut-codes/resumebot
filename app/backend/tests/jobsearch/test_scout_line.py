"""The scout's line beside a role's Scored event (`pipeline/scout_line.py`): once the posting's text has been read, the
sentences of it that say the text was missing are left out, and nothing else is. Pure functions; every sentence is made
up, in the shapes the scout's lines take."""

import pytest

from app.jobsearch.pipeline import scout_line

NOTE = "place not stated: check the work location before applying"


@pytest.mark.parametrize("line", [
    "Text missing (sign-in job site); scored from title only.",
    "Text missing: the posting sits behind a job-site sign-in and was not fetched; scored from title alone.",
    "Posting text could not be read (the page returned empty), so only the title was judged.",
    "Posting body could not be read: the fetched page showed only navigation and perks.",
    "No posting text: the careers page opened with no body.",
    "Only the title was available; most asks are unknown.",
    "Posting page returned no text when opened; every ask is unknown.",
    "The posting's own page did not return its text when opened; answers rest on the title only.",
    "Scored from title, company and place only.",
])
def test_a_line_that_says_the_text_was_missing_is_left_out_once_it_is_read(line):
    assert scout_line.shown(line, text_read=True) is None


@pytest.mark.parametrize("line", [
    "Text missing (sign-in job site); scored from title only.",
    "Strong overlap on program leadership; the biggest gap is no vendor management.",
])
def test_every_line_is_shown_as_it_was_while_the_text_is_still_missing(line):
    assert scout_line.shown(line, text_read=False) == line


@pytest.mark.parametrize("line", [
    "base 4; internal +1; Manager title -1; comp mid $140K 0 = 4",
    "Strong overlap on program leadership; the biggest gap is no vendor management.",
    "Pay is not stated: the text says wages are available on request.",
    "Posting says travel up to 25%; a Director title with a team of 12 and no description of the budget.",
    "Biggest gap: title steps down one level; scored lower for the title.",
])
def test_a_line_that_says_nothing_of_missing_text_comes_back_as_it_was(line):
    assert scout_line.shown(line, text_read=True) == line


def test_only_the_sentences_about_the_missing_text_go_and_the_scouts_other_words_stay():
    line = "Text missing and not fetched; scored on title only. Non-US place. Staffing firm, real employer unclear."
    assert scout_line.shown(line, text_read=True) == "Non-US place. Staffing firm, real employer unclear."


def test_what_the_app_added_after_the_scouts_line_stays_when_the_scouts_line_goes():
    assert scout_line.shown(f"Text missing (sign-in site); scored from title only. | {NOTE}", text_read=True) == NOTE
    kept = scout_line.shown(f"Text missing; scored from title only. Likely a consulting role. | {NOTE}", text_read=True)
    assert kept == f"Likely a consulting role. | {NOTE}"


def test_a_role_with_no_line_has_none_to_show():
    assert scout_line.shown(None, text_read=True) is None and scout_line.shown("", text_read=True) == ""
