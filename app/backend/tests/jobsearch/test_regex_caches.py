"""The country and title-word patterns are built once per word group, and answer as a pattern per word would.
Every place and title here is made up."""

import re

import pytest

from app.jobsearch.measures import countries, dials


def _reference(text, words):
    return any(re.search(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", text, re.IGNORECASE if len(w) > 3 else 0)
               for w in words)


TEXTS = ["Remote in München", "munchen", "ZÜRICH office", "Zurich", "Berlin, DE", "work in canada", "WORK IN CANADA",
         "Manchester, NH", "uk based", "UK", "Uk", "Birmingham, UK", "New York, NY", "new york", "Costa Rica",
         "Remote - US", "us only", "the US", "Austin, TX", "", "Tel Aviv", "Brisbane", "Perthshire", "Tokyo, JP"]
GROUPS = [countries.US_WORDS, countries.US_STATES, countries.US_CODES, *countries.ABROAD.values(), (),
          ("München", "Zürich"), ("UK", "uk"), ("Costa Rica", "Rica"), ["Canada", "US"]]


@pytest.mark.parametrize("words", GROUPS, ids=lambda w: ",".join(w)[:30] or "empty")
def test_has_matches_a_pattern_per_word(words):
    for text in TEXTS:
        assert countries._has(text, words) == _reference(text, words), (text, words)


def test_has_accepts_a_list_and_nothing():
    assert countries._has("Remote in Canada", ["Canada"]) is True
    assert countries._has("Remote in Canada", []) is False


def test_has_builds_patterns_once_per_group():
    countries._compiled.cache_clear()
    countries._has("Toronto", countries.ABROAD["Canada"])
    before = countries._compiled.cache_info()
    countries._has("Ottawa", list(countries.ABROAD["Canada"]))
    after = countries._compiled.cache_info()
    assert after.hits == before.hits + 1 and after.misses == before.misses


TITLES = ["Software Intern", "Internal Tools Engineer", "Entry-Level Analyst", "entry level analyst",
          "Analyst (2027 Start)",
          "International Sales", "C++ Developer", "", None, "Senior  Intern"]
WORDS = "intern, entry level, (2027 start), c++, senior, , -x-, analyst"


def _old_hits(title, value):
    hits = []
    for word in dials.values_of(value):
        pattern = r"[\s\-]+".join(re.escape(part) for part in re.split(r"[\s\-]+", word) if part)
        before = r"(?<!\w)" if word[0].isalnum() or word[0] == "_" else ""
        after = r"(?!\w)" if word[-1].isalnum() or word[-1] == "_" else ""
        if pattern and re.search(f"{before}{pattern}{after}", title or "", re.IGNORECASE):
            hits.append(word)
    return hits


@pytest.mark.parametrize("title", TITLES)
def test_title_hits_unchanged(title):
    assert dials.title_hits(title, WORDS) == _old_hits(title, WORDS)


def test_title_hits_builds_a_word_pattern_once():
    dials._word_regex.cache_clear()
    dials.title_hits("Software Intern", "intern")
    before = dials._word_regex.cache_info()
    dials.title_hits("Another Intern", "intern")
    after = dials._word_regex.cache_info()
    assert after.hits == before.hits + 1 and after.misses == before.misses
