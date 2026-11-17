"""The cleaner's word tests (`postings/stems.py`): before a reader asks a pattern of a sentence, a plain test for a word
the pattern cannot match without says whether it can match at all. They save the cleaner most of its time and must
change nothing it says, so each reader is held to the answer its patterns alone give: over every alternative of every
pattern a stem list guards, in capitals, lower case and title case, with the letters ignore-case matching folds
(dotless i, long s, Kelvin sign, dotted capital i), and with odd spaces. A pattern that gains an alternative needs the
stem that goes with it and an example here, or a reader silently stops seeing it. Every sentence is made up."""

import re
import string
import sys
from unittest import mock

import pytest

from app.jobsearch.gating import posting_facts as pf
from app.jobsearch.gating import structured_fill as sf
from app.jobsearch.postings import cleanse, stems, whole

LONG_S, DOTLESS_I, DOTTED_I, KELVIN, NBSP = chr(0x17F), chr(0x131), chr(0x130), chr(0x212A), chr(0xA0)
PAD = "\n".join(f"Paragraph {i} of the posting says one thing about the work and nothing else." for i in range(1, 21))


def _plain(read, text):
    """What `read` gives with no word test ever saying no: its patterns alone."""
    with mock.patch.object(stems, "folded", lambda _: None):
        return read(text)


def _degrees(text):
    got = pf.degree_from_text(text)
    return (got.required, got.preferred) if got.required or got.preferred else None


def _boilerplate(line):
    return cleanse.strip_boilerplate("Do the work well.\n" + line)[1] or None


def _shortened(sentence):
    return whole.why_not(PAD + "\n" + sentence + "\n" + PAD)


YEARS = ["Minimum 5 years of experience.", "Min. of 4 years in a similar role.", "At least 3 years of experience.",
         "Requires 7+ years of experience in programs.", "We want 3-5 years experience.",
         "10 or more years of relevant experience.", "2 to 4 years in a related role.", "Needs 6 years of experience.",
         "At least 1 year of experience."]
YEARS_FILLED = ["Requires 7 years of experience.", "Needs 3-5 years of experience in programs.",
                "8 years in a related field is required.", "We want 10+ years of experience leading a team."]
DEGREES = [
    "A PhD is required.", "Ph.D required.", "Doctorate required.", "A doctoral degree is required.",
    "A Juris Doctor is required.", "Master's degree required.", "Masters degree is required.",
    "Master of Science required.", "Master degree required.", "An MBA is required.", "Bachelor's degree required.",
    "Bachelors required.", "Bachelor degree required.", "Associate's degree required.", "Associate degree is required.",
    "Associate's required.",
    "A BS degree is required.", "B.S. degree required.", "A BA degree is required.", "B.A. degree required.",
    "An MS degree is required.", "M.S. degree required.", "A BSc degree is required.", "An MSc degree is required.",
    "A college degree is required.", "A university degree is required.", "A four-year degree is required.",
    "A 4-year degree is required.", "A degree in business is required.", "Degree in any field required.",
    "PhD preferred.", "A Master's degree is a plus.", "Master's degree required; PhD preferred.",
    "BS or equivalent required.", "B.S or equivalent is required.", "BA or equivalent required.",
    "B.A or equivalent required.", "BSc or equivalent required.", "MS or equivalent required.",
    "M.S or equivalent required.", "MSc or equivalent required."]
DEGREES_FILLED = [
    "A PhD.", "Ph.D in physics.", "Doctorate.", "Doctoral candidates apply.", "Master's degree.", "An MBA.",
    "A graduate degree.", "An advanced degree.", "Bachelor's.", "BS in biology.", "B.A. degree.", "BA in English.",
    "Undergraduate degree.", "Four-year degree.", "Four year degree.", "College degree.", "University degree.",
    "Associate's.", "Any degree will do.", "B.A in English.", "B.S in chemistry.", "Master's or higher."]
CLEARANCE = [
    "Active TS/SCI clearance.", "TS / SCI required.", "Top secret level.", "Secret clearance needed.",
    "Security clearance.", "Interim clearance.", "Active clearance.", "Public trust position.", "Polygraph required.",
    "Clearance is required.", "Clearance needed.", "Secret clearance preferred.", "No clearance required.",
    "This role does not require a clearance.", "Clearance is not required.", "A clearance isn't required."]
SELLING = [
    "Sales experience is required.", "Selling to enterprise accounts.", "Own business development for the region.",
    "Business-development lead.", "Biz dev manager role.", "Biz-dev partner.", "New business growth.",
    "Carry a quota every quarter.", "Quotas are set yearly.", "Track bookings weekly.", "A booking target.",
    "Own the sales pipeline.", "Manage the revenue pipeline.", "Own deal pipelines.", "A client pipeline.",
    "A customer pipeline.", "The prospect pipeline.", "An opportunity pipeline.", "The account pipeline.",
    "Hit the revenue target.", "Revenue goals matter.", "Find new clients.", "Win new customers.",
    "Open new accounts.", "Land new logos.", "Close deals every month.", "Closing the sale.",
    "Closed two big contracts.", "Close 3 new business."]
EQUAL_OPPORTUNITY = [
    "We are an equal opportunity employer and a great place to work for all of us.",
    "Equal employment opportunity statement for every applicant here.",
    "We do not discriminate in hiring decisions at any level of the company.",
    "The company does not discriminate against any person who applies here.",
    "We hire without regard to race, color or religion at all of our sites.",
    "An affirmative action employer committed to diversity in every team.",
    "Applicants may be protected veterans or members of a protected class today."]
BENEFITS = [
    "We offer health insurance, dental, vision and a 401k plan to every employee.",
    "Medical, dental and vision for all of our employees and their families.",
    "Health insurance, 401(k), and paid time off for everybody in the company.",
    "401(k) retirement plan with PTO and vacation days for everyone here today.",
    "Parental leave, tuition and wellness programs for everyone on the team.",
    "Life insurance and disability insurance and paid holidays and sick leave for all.",
    "Our 401k, 401(k) and 401(k plans are the only benefit named in this line.",
    "Two benefits only: medical and dental for every employee of the company."]
SHORTENED = [
    "[truncated]", "[shortened]", "[abridged]", "[omitted]", "[summary]", "[summarized]", "[summarised]",
    "[placeholder]", "[rest of the description]", "[continued on the site]", "[full text]",
    "[full description at the link]", "[...]", "[" + chr(0x2026) + "]", "(truncated)", "(shortened)", "(abridged)",
    "(summarized)", "(summarised)", "(omitted)", "(text cut)", "A placeholder for the text.", "Lorem ipsum dolor.",
    "The rest of the description was omitted.", "The remainder of the posting is not shown.",
    "The description was truncated.", "The posting has been shortened.", "The text was abridged.",
    "The description was summarized."]
#: Sentences none of the readers finds anything in.
QUIET = ["We make widgets and love our team.", "Lead the weekly review with the finance group.",
         "Our team sold nothing. Business is business.", "The new office has a pipeline of coffee orders.",
         "Years went by; not one year was wasted.", "A secret recipe, a trusted friend and a master chef.",
         "Equality matters; so does the vision of the founders."]

#: (the reader, the sentences it finds something in): each is asked in every `_forms` and must answer as its
#: patterns alone do.
READERS = [
    (pf.experience_from_text, YEARS), (sf.years_from_text, YEARS_FILLED), (_degrees, DEGREES),
    (sf.degree_from_text, DEGREES_FILLED), (pf.clearance_from_text, CLEARANCE), (pf.bd_candidates, SELLING),
    (_boilerplate, EQUAL_OPPORTUNITY), (_boilerplate, BENEFITS[:-1]), (_shortened, SHORTENED)]


def _forms(sentence: str) -> list[str]:
    return [sentence, sentence.upper(), sentence.lower(), sentence.title(), sentence.replace("s", LONG_S),
            sentence.replace("i", DOTLESS_I), sentence.replace("k", KELVIN), sentence.replace("i", DOTTED_I),
            NBSP + sentence, "x " + sentence + " y"]


# --- the one test every word test rests on --------------------------------------------------------------------------


def test_a_text_with_a_letter_that_ignore_case_matching_folds_and_lowering_does_not_is_asked_the_pattern():
    for text in ("Book" + DOTTED_I + "ngs", "book" + DOTLESS_I + "ngs", LONG_S + "ales"):
        assert stems.folded(text) is None
    assert stems.folded("Sales QUOTA") == "sales quota" and stems.folded("Kelvin " + KELVIN) == "kelvin k"
    assert stems.folded("Business " + NBSP + "Development") == "business " + NBSP + "development"


def test_a_word_test_never_says_no_where_an_ignore_case_search_for_the_word_would_say_yes():
    """For every letter of the alphabet and every character a case maps (a character no case maps cannot be folded
    onto a letter): whichever the engine matches to the letter either lowers to it or is one `folded` hands to the
    pattern."""
    letters = {x: re.compile(x, re.I) for x in string.ascii_lowercase}
    for code in range(128, sys.maxunicode + 1):
        char = chr(code)
        if char.lower() == char and char.upper() == char and char.casefold() == char:
            continue
        folded = stems.folded(char)
        for letter, pattern in letters.items():
            assert not pattern.search(char) or folded is None or letter in folded, (hex(code), letter)


def test_a_stem_is_lower_case_so_it_can_be_found_in_the_lowered_sentence():
    lists = (cleanse._EEO_STEMS, cleanse._BENEFIT_STEMS, pf._DEGREE_STEMS, pf._CLEARANCE_STEMS, pf._BD_STEMS,
             sf._DEGREE_STEMS, whole._SHORT_STEMS)
    assert all(stem and stem == stem.lower() for stem_list in lists for stem in stem_list)


# --- each reader says what its patterns alone say -------------------------------------------------------------------


@pytest.mark.parametrize("read, sentences", READERS,
                         ids=[f"{read.__name__}-{i}" for i, (read, _) in enumerate(READERS)])
def test_a_reader_with_word_tests_answers_what_its_patterns_alone_answer_for_every_alternative(read, sentences):
    for sentence in sentences:
        assert _plain(read, sentence), f"no pattern finds anything in the example {sentence!r}"
        for form in _forms(sentence):
            assert read(form) == _plain(read, form), form


def test_a_reader_with_word_tests_answers_what_its_patterns_alone_answer_for_a_sentence_with_nothing_in_it():
    for sentence in QUIET:
        for read, _ in READERS:
            assert read(sentence) == _plain(read, sentence) and not read(sentence), (read.__name__, sentence)


def test_a_line_with_a_distinct_match_short_of_three_is_not_a_benefits_line_and_with_three_is():
    two, three = BENEFITS[-1], BENEFITS[-2]
    assert _boilerplate(two) is None and [r["rule"] for r in _boilerplate(three)] == ["benefits"]


#: What `_BENEFIT_WORDS` names, one each, in its order.
BENEFIT_WORDS = ["health insurance", "medical", "dental", "vision", "401(k)", "retirement plan", "paid time off", "PTO",
                 "vacation", "parental leave", "tuition", "wellness", "life insurance", "disability insurance",
                 "paid holidays", "sick leave"]


def test_every_benefit_the_pattern_names_counts_toward_the_three_a_benefits_line_needs():
    assert len(BENEFIT_WORDS) == len(cleanse._BENEFIT_WORDS.split("|")), (
        "a benefit was added to the pattern: add it here, and its word to `_BENEFIT_STEMS`")
    for at, word in enumerate(BENEFIT_WORDS):
        two_more = [BENEFIT_WORDS[(at + step) % len(BENEFIT_WORDS)] for step in (1, 2)]
        line = f"Perks: {word}, {two_more[0]} and {two_more[1]} for every employee."
        assert _boilerplate(line) == _plain(_boilerplate, line) and _boilerplate(line), line


# --- what is read once ------------------------------------------------------------------------------------------------


def test_the_cleaner_reads_the_facts_of_a_text_once():
    text = "Programs Lead\nWork type: Hybrid\nAt least 7 years of experience leading a programs team.\n" + PAD
    reads = []
    real = pf.extract
    with mock.patch.object(pf, "extract", lambda *args, **kwargs: reads.append(args) or real(*args, **kwargs)):
        cleanse.cleanse({"title": "Programs Lead", "text": text})
    assert len(reads) == 1


def test_a_record_filled_from_the_facts_already_read_is_the_record_filled_from_the_text():
    text = "Programs Lead\nWork type: Hybrid\nBachelor's degree required.\nAt least 7 years of experience.\n" + PAD
    assert sf.fill(text, "Programs Lead", False, pf.extract(text)).fields == sf.fill(text, "Programs Lead").fields


def test_a_text_is_split_into_lines_and_sentences_once_however_many_readers_ask_for_them():
    text = "First line of the posting.\n" + "A long line. " * 40 + "\nLast line."
    again = "".join([text[:10], text[10:]])
    assert again is not text and again == text
    for split in (pf._lines, pf._sentences, sf._sentences):
        assert isinstance(split(text), tuple) and split(again) is split(text)


def test_the_boilerplate_rules_give_the_same_text_whether_or_not_the_caller_says_it_is_clean_already():
    benefits = "We offer health insurance, dental and vision to every employee here."
    for text in ("Lead the work.\n" + benefits + "\n\n" + PAD, "Lead the work.\n\nOwn the budget.\n" + PAD):
        clean = cleanse.normalize(text)
        assert cleanse.strip_boilerplate(clean, normalized=True) == cleanse.strip_boilerplate(clean)
