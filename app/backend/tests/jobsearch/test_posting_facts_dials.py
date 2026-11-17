"""The three job facts a dial reads, taken from a posting's text in code (docs/PRD.md 46.4): years of experience asked,
the degree (required or preferred) and a security clearance. Every sentence is made up."""

import pytest

from app.jobsearch.gating import posting_facts as pf


@pytest.mark.parametrize("text, years", [
    ("You bring 5+ years of experience leading programs.", "5"),
    ("Minimum 7 years of relevant experience.", "7"),
    ("At least 3 years in a similar role.", "3"),
    ("3-5 years of project experience is expected.", "3"),
    ("8 or more years of hands-on experience.", "8"),
    ("Requires 3+ years of Python and 6+ years of experience overall.", "6"),
])
def test_experience_years_asked_are_read_from_a_sentence_that_asks_for_them(text, years):
    found = pf.experience_from_text(text)
    assert found is not None and found.value == years and found.quote == text


@pytest.mark.parametrize("text", [
    "We have been in business for 20 years.",
    "Our team has grown 3 years in a row.",
    "Experience with spreadsheets is a plus.",
    "",
])
def test_a_sentence_that_asks_for_no_years_gives_no_experience(text):
    assert pf.experience_from_text(text) is None


@pytest.mark.parametrize("text, level", [
    ("A bachelor's degree is required.", "bachelor"),
    ("Master's degree required; PhD preferred.", "master"),
    ("BS in a related field or equivalent experience.", "bachelor"),
    ("MBA required.", "master"),
    ("Associate degree minimum.", "associate"),
    ("A degree is required.", "degree"),
])
def test_a_required_degree_is_kept_with_its_level(text, level):
    found = pf.degree_from_text(text)
    assert found.required is not None and found.required.value == level


def test_a_preferred_degree_is_kept_apart_from_the_required_one():
    found = pf.degree_from_text("Bachelor's degree required.\nMaster's degree preferred.")
    assert (found.required.value, found.preferred.value) == ("bachelor", "master")
    only = pf.degree_from_text("A master's degree is a plus.")
    assert only.required is None and only.preferred.value == "master"


@pytest.mark.parametrize("text", [
    "Our office is in Jackson, MS and we need a good attitude.",
    "You will master data management tools and be a scrum master.",
    "Degree programs are listed on our site.",
])
def test_a_state_code_or_another_word_is_no_degree(text):
    found = pf.degree_from_text(text)
    assert found.required is None and found.preferred is None


@pytest.mark.parametrize("text, value", [
    ("Active TS/SCI clearance required.", "required"),
    ("Must hold a secret clearance.", "required"),
    ("Public trust is needed; a polygraph may be given.", "required"),
    ("No security clearance is required.", "none"),
    ("This role does not require a clearance.", "none"),
    ("A security clearance is a plus.", "preferred"),
])
def test_a_clearance_is_required_preferred_or_none(text, value):
    found = pf.clearance_from_text(text)
    assert found is not None and found.value == value


def test_a_posting_that_says_nothing_of_clearance_gives_none():
    assert pf.clearance_from_text("We sell clearance items online.") is None


def test_extract_keeps_the_new_facts_with_their_lines():
    found = pf.extract("Program Lead\nYou bring 5+ years of experience.\nBachelor's degree required.\n"
                       "Active secret clearance required.")
    facts = found.facts()
    assert facts["experience_years"]["value"] == "5"
    assert facts["degree_required"]["value"] == "bachelor"
    assert facts["clearance"]["value"] == "required"
    assert facts["degree_required"]["quote"] == "Bachelor's degree required."
