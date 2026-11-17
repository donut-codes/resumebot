"""What the cleaner (`postings/cleanse.py`) and the readers it feeds give for two made-up postings, pinned. They were
written before the cleaner's word tests (`postings/stems.py`) and read once instead of twice, and they pass on the
plain cleaner as well: a faster cleaner is held to the output the slower one gave. One posting is plain; the other has
odd spaces, zero-width characters, CRLF line ends, capitals, letters that look like others (dotless i, long s) and an
ending that says "see more". Every name, figure and sentence is made up."""

from app.jobsearch.gating import posting_facts as pf
from app.jobsearch.postings import cleanse

DUTIES = [f"Duty {i}: the programs lead keeps one part of the weekly programs review moving and reports on it."
          for i in range(1, 9)]
EEO = "We are an equal opportunity employer and do not discriminate on any basis at all."
BENEFITS = "We offer health insurance, dental, vision and a 401k plan to every employee here."
APPLY = "To apply, send your resume to the careers page."
SELLING = "Own the sales pipeline for new business and hit the revenue target set each quarter."

HEAD = ["Programs Lead", "Location: Oakfield, ST", "Work type: Hybrid", "Pay range: $90,000 - $115,000 per year", "",
        "About the job", *DUTIES[:4], SELLING]
REQUIREMENTS = ["Requirements", "A Bachelor's degree in a related field is required.",
                "A Master's degree is preferred.", "At least 7 years of experience leading a programs team.",
                "Active secret clearance required, and a polygraph is a plus.",
                "This role may require up to 25% travel.", *DUTIES[4:]]
PLAIN = [*HEAD, "", "", *REQUIREMENTS, BENEFITS, EEO, APPLY]
#: What it keeps: the three boilerplate lines gone and the run of two blank lines one blank line.
PLAIN_KEPT = [*HEAD, "", *REQUIREMENTS]

BENEFITS_CAPS = "MEDICAL, DENTAL, VISION AND 401(K) FOR EVERY EMPLOYEE OF THE COMPANY HERE."
EEO_CAPS = "WE DO NOT DISCRIMINATE ON THE BASIS OF ANYTHING IN OUR HIRING DECISIONS."
LOOK_ALIKES = "Own the \u017fales pipeline for new bu\u0131siness and hit the revenue target set each quarter."
DOTLESS = "At least 5 YEARS of experience with book\u0131ngs and quotas."
DEGREES_CAPS = "A MASTER\u2019S DEGREE IS REQUIRED. A PhD IS PREFERRED."
CLEARANCE_CAPS = "ACTIVE TS/SCI CLEARANCE REQUIRED. NO POLYGRAPH."
ODD = ["Programs\u00a0Lead\u200b", "Work\u2009type:\u202fOn-site", "", *DUTIES[:3],
       LOOK_ALIKES + "   ", DEGREES_CAPS, DOTLESS, CLEARANCE_CAPS, *DUTIES[3:], BENEFITS_CAPS, EEO_CAPS, "", "", "",
       "Read more"]
ODD_KEPT = ["Programs Lead", "Work type: On-site", "", *DUTIES[:3], LOOK_ALIKES, DEGREES_CAPS, DOTLESS,
            CLEARANCE_CAPS, *DUTIES[3:], "", "Read more"]


def _cleansed(lines: list[str], line_end: str = "\n") -> cleanse.Cleansed:
    return cleanse.cleanse({"title": "Programs Lead", "company": "Contoso", "location": "Oakfield, ST",
                            "text": line_end.join(lines)})


def test_a_plain_posting_is_cleansed_to_its_duties_its_fields_and_the_lines_that_were_taken_out():
    got = _cleansed(PLAIN)
    assert got.text_clean == "\n".join(PLAIN_KEPT)
    assert got.removed == [{"rule": "equal_opportunity", "lines": 1, "chars": len(EEO)},
                           {"rule": "benefits", "lines": 1, "chars": len(BENEFITS)},
                           {"rule": "how_to_apply", "lines": 1, "chars": len(APPLY)}]
    assert (got.work_mode, got.pay_min, got.pay_max, got.partial) == ("hybrid", 90000, 115000, False)
    assert got.marks == {
        "title": {"value": "Programs Lead", "source": "listing", "quote": "Programs Lead"},
        "location": {"value": ["Oakfield, ST"], "source": "listing", "quote": "Oakfield, ST"},
        "work_mode": {"value": "hybrid", "source": "code", "quote": "Work type: Hybrid"},
        "pay": {"value": "$90,000-$115,000", "source": "code", "quote": "Pay range: $90,000 - $115,000 per year"},
        "travel": {"value": "This role may require up to 25% travel.", "source": "code",
                   "quote": "This role may require up to 25% travel."},
        "years": {"value": 7, "source": "code", "quote": "At least 7 years of experience leading a programs team."},
        "degree": {"value": "bachelor", "source": "code",
                   "quote": "A Bachelor's degree in a related field is required."},
    }


def test_the_facts_a_plain_posting_states_are_read_with_their_lines():
    clean = _cleansed(PLAIN).text_clean
    facts = pf.extract(clean, "Contoso", "Programs Lead")
    assert facts.experience == pf.Found("7", "At least 7 years of experience leading a programs team.")
    assert facts.degree_required == pf.Found("bachelor", "A Bachelor's degree in a related field is required.")
    assert facts.degree_preferred == pf.Found("master", "A Master's degree is preferred.")
    assert facts.clearance == pf.Found("preferred", "Active secret clearance required, and a polygraph is a plus.")
    assert facts.travel_percent == pf.Found("25", "This role may require up to 25% travel.")
    assert pf.bd_candidates(clean, "Programs Lead") == [SELLING]


def test_a_posting_with_odd_spaces_capitals_look_alike_letters_and_a_see_more_ending_is_cleansed_as_it_always_was():
    got = _cleansed(ODD, "\r\n")
    assert got.text_clean == "\n".join(ODD_KEPT)
    assert got.removed == [{"rule": "equal_opportunity", "lines": 1, "chars": len(EEO_CAPS)},
                           {"rule": "benefits", "lines": 1, "chars": len(BENEFITS_CAPS)}]
    assert (got.work_mode, got.pay_min, got.pay_max, got.partial) == ("onsite", None, None, True)
    assert got.marks == {
        "title": {"value": "Programs Lead", "source": "listing", "quote": "Programs Lead"},
        "location": {"value": ["Oakfield, ST"], "source": "listing", "quote": "Oakfield, ST"},
        "work_mode": {"value": "onsite", "source": "code", "quote": "Work type: On-site"},
        "years": {"value": 5, "source": "code", "quote": DOTLESS},
        "degree": {"value": "degree", "source": "code", "quote": "A MASTER\u2019S DEGREE IS REQUIRED."},
    }


def test_the_facts_a_posting_with_look_alike_letters_states_are_read_with_their_lines():
    clean = _cleansed(ODD, "\r\n").text_clean
    facts = pf.extract(clean, "Contoso", "Programs Lead")
    assert facts.experience == pf.Found("5", DOTLESS)
    assert facts.degree_required is None
    assert facts.degree_preferred == pf.Found("master", DEGREES_CAPS)
    assert facts.clearance == pf.Found("required", CLEARANCE_CAPS)
    assert pf.bd_candidates(clean, "Programs Lead") == [LOOK_ALIKES, DOTLESS]


def _normalized_through_a_table(text: str | None) -> str:
    """What `normalize` gave in its first version, which mapped every character of the text through a table."""
    table = str.maketrans({**{chr(c): " " for c in (0xA0, 0x2009, 0x202F)},
                           **{chr(c): "" for c in (0x200B, 0x200C, 0x200D, 0xFEFF)}})
    lines = [line.rstrip() for line in (text or "").translate(table).replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


NBSP, THIN, NARROW, ZERO, JOINER, BOM, EM = (chr(c) for c in (0xA0, 0x2009, 0x202F, 0x200B, 0x200D, 0xFEFF, 0x2003))
NORMALIZE_CASES = [
    None, "", "  ", "plain\ntext", "a\r\nb\r\n\r\n\r\nc", "a\r\r\nb", "a\rb", f"x{NBSP}y{THIN}z{NARROW}w",
    f"a{ZERO} {JOINER}\n{BOM}b", "tail   \n\n\n  \nnext", " lead\n", f"{NBSP}\n{ZERO}\n", "\r\n\r\n",
    f"caf{chr(0xE9)} {NBSP}\nna{chr(0xEF)}ve{ZERO}", f"a{EM}b  \t\nc", f"a\r{ZERO}\nb",
    f"{BOM}start\r\n{NBSP}\r\nend{NBSP}"]


def test_the_text_is_tidied_as_it_was_when_every_character_went_through_a_table():
    for text in NORMALIZE_CASES:
        assert cleanse.normalize(text) == _normalized_through_a_table(text), repr(text)
        assert cleanse.normalize(cleanse.normalize(text)) == cleanse.normalize(text), repr(text)
