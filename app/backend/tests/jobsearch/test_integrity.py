"""The integrity checks: a certification or licence the master does not
hold, work that did not ship written as delivered, and a line the master keeps out of writing. They are code, always on,
and read the words the writer prints. Everything is made up."""

import copy
import re

import pytest

from app.jobsearch.profile import integrity
from app.jobsearch.profile.documents import Resume, Writing

MASTER = {
    "summary": "Builds programs that people use.",
    "summary_variants": [{"id": "sv-1", "text": "Runs programs that last.", "note": ""}],
    "achievements": [{"id": "a-1", "text": "Led a statewide program", "note": ""}],
    "philosophy": "Plain work done well.",
    "competencies": [{"id": "gov", "area": "Governance", "items": ["Risk Frameworks"]}],
    "roles": [{
        "id": "r-1", "employer": "Northwind Labs", "title": "Senior Manager", "tools": ["Visio"],
        "intro": "Runs the practice.", "descriptor": "an example consultancy", "note": "",
        "bullets": [{"id": "b-1", "text": "Led the risk working group across client engagements", "note": ""}],
        "evidence": [{"id": "e-1", "title": "Board deck", "text": "Presented the risk register each quarter"}],
    }],
    "education": [{"id": "ms", "degree": "MS in Leadership", "school": "Example University", "note": ""}],
    "projects": [{"id": "p-1", "text": "Built a tracker", "note": ""}],
    "awards": [{"id": "aw-1", "text": "Won an award", "note": ""}],
    "notes": [],
}
PRINTED = "bullet 1 under Senior Manager at Northwind Labs"


def master(**changes) -> Resume:
    return Resume.model_validate({**copy.deepcopy(MASTER), **changes})


def owner(*names: str) -> Writing:
    return Writing.model_validate({"credentials": [{"id": f"c-{i}", "name": n} for i, n in enumerate(names, 1)]})


def certs(texts, *, resume: Resume | None = None, writing: Writing | None = None) -> list[str]:
    return integrity.certification_hits(writing or Writing(), resume or master(), texts)


# --- certifications and licences -------------------------------------------------------------------------------------


def test_a_certification_in_the_printed_text_and_not_in_the_master_is_a_problem_naming_it_and_where():
    lines = certs([("summary", "A Project Management Professional who runs programs")])
    assert lines == ["summary: 'Project Management Professional' (a certification or licence the master doesn't hold)"]


def test_one_line_for_each_credential_found_in_each_place_and_none_for_one_the_master_holds():
    resume = master(summary="Holds a Certified Public Accountant licence.")
    lines = certs([("summary", "A Certified Public Accountant who is also a Chartered Financial Analyst"),
                   (PRINTED, "Passed the CFA exam and kept the Chartered Financial Analyst charter"),
                   ("achievement 1", "Led a statewide program")], resume=resume)
    assert len(lines) == 2, lines
    assert all("Certified Public Accountant" not in line for line in lines)
    assert lines[0].startswith("summary: ") and "Chartered Financial Analyst" in lines[0]
    assert lines[1].startswith(f"{PRINTED}: ") and "CFA" in lines[1], "one line for the credential in that place"
    assert certs([("summary", "A Certified Public Accountant")], resume=resume) == []


def test_a_credential_named_in_a_bullet_of_the_master_is_held_whichever_form_the_packet_uses():
    resume = master(roles=[{**MASTER["roles"][0], "bullets": [
        {"id": "b-1", "text": "Earned the PMP in 2018 and led programs", "note": ""}]}])
    assert certs([("summary", "A PMP who leads programs")], resume=resume) == []
    assert certs([("summary", "A Project Management Professional who leads programs")], resume=resume) == [], (
        "the acronym and the full name are one credential")
    spelled = master(roles=[{**MASTER["roles"][0], "bullets": [
        {"id": "b-1", "text": "Earned the Project Management Professional licence", "note": ""}]}])
    assert certs([("summary", "A PMP who leads programs")], resume=spelled) == []


FIELDS = {
    "the summary": {"summary": "Holds the PMP."},
    "a summary variant": {"summary_variants": [{"id": "sv-1", "text": "Holds the PMP.", "note": ""}]},
    "an achievement": {"achievements": [{"id": "a-1", "text": "Earned the PMP", "note": ""}]},
    "a competency area": {"competencies": [{"id": "g", "area": "PMP", "items": []}]},
    "a competency item": {"competencies": [{"id": "g", "area": "Governance", "items": ["PMP"]}]},
    "a project": {"projects": [{"id": "p-1", "text": "Studied for the PMP", "note": ""}]},
    "an award": {"awards": [{"id": "aw-1", "text": "PMP of the year", "note": ""}]},
    "the philosophy": {"philosophy": "A PMP's discipline."},
}
ROLE_FIELDS = {
    "a tool": {"tools": ["PMP"]}, "an employer": {"employer": "PMP Partners"}, "a title": {"title": "PMP Lead"},
    "a scope line": {"intro": "Runs the PMP office."}, "a descriptor": {"descriptor": "a PMP shop"},
    "a bullet": {"bullets": [{"id": "b-1", "text": "Taught the PMP", "note": ""}]},
    "an evidence title": {"evidence": [{"id": "e-1", "title": "PMP plan", "text": "x"}]},
    "an evidence text": {"evidence": [{"id": "e-1", "title": "x", "text": "Passed the PMP exam"}]},
}
EDUCATION_FIELDS = {"a degree": {"degree": "PMP course"}, "a school": {"school": "PMP Institute"}}


@pytest.mark.parametrize("field", FIELDS)
def test_a_credential_in_the_masters_own_words_is_held(field):
    assert certs([("summary", "A PMP")], resume=master(**FIELDS[field])) == []


@pytest.mark.parametrize("field", ROLE_FIELDS)
def test_a_credential_in_a_role_of_the_master_is_held(field):
    resume = master(roles=[{**MASTER["roles"][0], **ROLE_FIELDS[field]}])
    assert certs([("summary", "A PMP")], resume=resume) == []


@pytest.mark.parametrize("field", EDUCATION_FIELDS)
def test_a_credential_in_the_masters_education_is_held(field):
    resume = master(education=[{**MASTER["education"][0], **EDUCATION_FIELDS[field]}])
    assert certs([("summary", "A PMP")], resume=resume) == []


NOTED = {
    "a bullet note": lambda m: m["roles"][0]["bullets"][0].update(note="never in writing: a lapsed PMP"),
    "a role note": lambda m: m["roles"][0].update(note="never in writing: a lapsed PMP"),
    "an education note": lambda m: m["education"][0].update(note="never in writing: a lapsed PMP"),
    "an achievement note": lambda m: m["achievements"][0].update(note="the PMP is lapsed"),
    "a project note": lambda m: m["projects"][0].update(note="the PMP is lapsed"),
    "an award note": lambda m: m["awards"][0].update(note="the PMP is lapsed"),
    "a summary variant note": lambda m: m["summary_variants"][0].update(note="the PMP is lapsed"),
    "a note of the master": lambda m: m.update(notes=[{"id": "n-1", "text": "A lapsed PMP", "source": "old file"}]),
}


@pytest.mark.parametrize("where", NOTED)
def test_a_credential_named_only_in_a_note_is_still_a_problem(where):
    data = copy.deepcopy(MASTER)
    NOTED[where](data)
    assert certs([("summary", "A PMP")], resume=Resume.model_validate(data)) == [
        "summary: 'PMP' (a certification or licence the master doesn't hold)"]


def test_an_acronym_matches_whole_words_and_case_and_a_longer_name_whole_words_ignoring_case():
    assert certs([("t", "A pmp study")]) == [], "lower case is not the acronym"
    assert certs([("t", "Built the sapmp tool and the XPMPX one")]) == [], "inside a word is not the acronym"
    assert certs([("t", "The PMPs met")]) == [], "a whole word"
    assert len(certs([("t", "Our PMP-certified lead")])) == 1, "a hyphen is the end of the word"
    assert len(certs([("t", "(PMP)")])) == 1
    assert len(certs([("t", "a certified public accountant")])) == 1, "a longer name ignores case"
    assert len(certs([("t", "Certified  Public\nAccountant")])) == 1, "any run of space"
    assert certs([("t", "Certified Public Accountants met")]) == [], "a whole word, not its plural"
    assert certs([("t", "The CompTIA Security+ exam")]) == ["t: 'CompTIA Security+' (a certification or licence "
                                                            "the master doesn't hold)"]


def test_an_owner_added_name_is_checked_and_a_two_letter_one_too_by_whole_word_and_case():
    writing = owner("Wine Sommelier Level 3", "PE")
    assert len(certs([("t", "A Wine Sommelier Level 3 who pours")], writing=writing)) == 1
    assert len(certs([("t", "A PE who builds")], writing=writing)) == 1
    assert certs([("t", "A pe who builds, a PEER review, the PE2 file")], writing=writing) == []
    held = master(summary="A Wine Sommelier Level 3.")
    assert certs([("t", "A Wine Sommelier Level 3 who pours")], resume=held, writing=writing) == []


def test_a_blank_owner_name_is_ignored_and_one_the_app_knows_is_not_reported_twice():
    writing = owner("", "   ", "PMP", "Certified Public Accountant")
    lines = certs([("t", "A PMP and a Certified Public Accountant")], writing=writing)
    assert len(lines) == 2 and certs([("t", "Led programs")], writing=writing) == []


def test_the_shipped_list_is_about_fifty_whole_names_and_no_short_or_common_abbreviation():
    forms = [form for entry in integrity.CREDENTIALS for form in entry]
    assert 45 <= len(integrity.CREDENTIALS) <= 65
    assert len(set(forms)) == len(forms), "no form in two entries"
    assert not [f for f in forms if re.fullmatch(r"[A-Z]{1,2}", f)], "no acronym of two letters or fewer"
    assert not {"PE", "RN", "CPA", "CISA", "CSM", "PHR", "CMA", "CIA", "CFP", "CFE", "CKA", "FRM", "CCP"} & set(forms)
    acronyms = [f for f in forms if re.fullmatch(r"[A-Z]{2,6}", f)]
    assert acronyms and all(len(f) >= 3 for f in acronyms)
    for entry in integrity.CREDENTIALS:
        assert any(" " in form for form in entry), f"each credential has its full name: {entry}"


def test_no_credential_is_found_in_plain_text_about_the_work():
    plain = [("summary", "Builds programs that people use, across client engagements."),
             (PRINTED, "Led the risk working group and the knowledge base."),
             ("headline", "Programs Leader"), ("t", "Applied six sigma methods and agile practices")]
    assert certs(plain) == []


# --- work that did not ship -------------------------------------------------------------------------------------------


def shipped(text: str) -> list[str]:
    return integrity.unshipped_hits([("bullet 1", text)])


@pytest.mark.parametrize("text, found", [
    ("Designed a proposed framework for the board", "Designed a proposed"),  # a delivery verb, then a proposed thing
    ("Built the planned migration for the team", "Built the planned"),
    ("Launched a draft policy for the firm", "Launched a draft"),
    ("Implemented an planned upgrade", "Implemented an planned"),
    ("Rolled out the proposed process", "Rolled out the proposed"),
    ("Wrote a proposal for a new platform that was delivered to three teams",
     "proposal for a new platform that was delivered"),
    ("Authored a proposal to consolidate vendors, implemented in the first quarter",
     "proposal to consolidate vendors, implemented"),
    ("A proposal for the portal, launched last year", "proposal for the portal, launched"),
    ("The proposed redesign was delivered to every region", "proposed redesign was delivered"),
    ("A planned migration was implemented across teams", "planned migration was implemented"),
    ("The staged rollout was launched in the spring", "staged rollout was launched"),
    ("Planned upgrades were shipped weekly", "Planned upgrades were shipped"),
])
def test_each_of_the_three_shapes_of_work_written_as_delivered_is_flagged(text, found):
    lines = shipped(text)
    assert len(lines) == 1 and lines[0].startswith("bullet 1: "), lines
    assert found in lines[0] and lines[0].endswith("(work that didn't ship, written as delivered)")


@pytest.mark.parametrize("text", [
    "Launched a pilot for the board",
    "Delivered a plan to the board",
    "Proposed a new platform to the board",
    "Planned the migration for the team",
    "Delivered the migration on plan",
    "Wrote a proposal that won funding",
    "Drafted a proposal for the board",
    "Built the platform and kept the draft notes",
    "Shipped the release candidate",
    "A proposal for the board. The platform was delivered.",  # a full stop ends the thought
    "Staged the demo, then launched the tool",
])
def test_shapes_tuned_to_be_quiet_leave_ordinary_work_alone(text):
    assert shipped(text) == []


def test_the_shipped_shapes_are_three_patterns_with_an_id_each():
    assert len(integrity.NOT_SHIPPED) == 3
    assert all(s.id and isinstance(s.pattern, re.Pattern) for s in integrity.NOT_SHIPPED)
    assert len({s.id for s in integrity.NOT_SHIPPED}) == 3


# --- a line the master keeps out of writing ---------------------------------------------------------------------------

MARKED = "Cut the board's review time by running a quiet pilot first"
SECRET = {
    "roles": [{**MASTER["roles"][0], "bullets": [
        {"id": "b-1", "text": "Led the risk working group across client engagements", "note": ""},
        {"id": "b-secret", "text": MARKED, "note": "Never in writing: the client asked"}]},
        {"id": "r-secret", "employer": "Initech", "title": "Consultant", "note": "never-in-writing: a short stint",
         "bullets": [], "evidence": []}],
    "education": [MASTER["education"][0],
                  {"id": "cert", "degree": "A certificate", "school": "Some School", "note": "never  in  writing"}],
    "achievements": [{"id": "a-1", "text": "Led a statewide program", "note": ""}],
}
MARKED_LINE = {"id": "b-x", "text": MARKED, "note": "never in writing"}
LONG_LINES = {
    "a bullet": lambda m: m["roles"][0]["bullets"].append(MARKED_LINE),
    "an achievement": lambda m: m["achievements"].append(MARKED_LINE),
    "a project": lambda m: m["projects"].append(MARKED_LINE),
    "an award": lambda m: m["awards"].append(MARKED_LINE),
    "a summary variant": lambda m: m["summary_variants"].append(MARKED_LINE),
}


def never(cites=(), includes=(), texts=(), **changes) -> list[str]:
    """The finder on the made-up master with marked lines: what is cited, what the packet lists, what it prints."""
    resume = master(**{**SECRET, **changes})
    return integrity.never_in_writing_hits(resume, list(cites), list(includes), list(texts))


def test_a_cited_marked_bullet_is_a_problem_naming_where_and_the_line_quoted():
    [line] = never(cites=[(PRINTED, "b-secret")])
    assert line == f"{PRINTED}: cites a line the master marks never in writing ({MARKED!r})"


def test_a_marked_role_the_packet_includes_and_a_marked_education_entry_are_problems():
    [role] = never(includes=[("the resume's roles", "r-secret")])
    assert role == "the resume's roles: includes 'Consultant at Initech', which the master marks never in writing"
    [edu] = never(includes=[("the resume's education", "cert")])
    assert edu == ("the resume's education: includes 'A certificate, Some School', which the master marks never in "
                   "writing")
    assert never(includes=[("the resume's roles", "r-1"), ("the resume's education", "ms")]) == [], "unmarked stays"


def test_a_marked_role_or_degree_cited_as_a_source_is_a_problem_too():
    [role] = never(cites=[("summary", "r-secret")])
    assert role == "summary: cites a line the master marks never in writing ('Consultant at Initech')"
    assert len(never(cites=[("summary", "cert")])) == 1


def test_a_citation_of_a_line_the_master_does_not_have_or_does_not_mark_is_not_this_checks_business():
    assert never(cites=[("summary", "b-1"), ("summary", "no-such-line")]) == []


def test_five_words_of_a_marked_line_printed_in_one_piece_are_a_problem():
    lines = never(texts=[(PRINTED, "We kept the board's review time down by running a quiet pilot first, and more")])
    assert lines == [f"{PRINTED}: prints words of a line the master marks never in writing ({MARKED!r})"]
    # The words are compared as words: case, punctuation and the apostrophe do not hide a run.
    assert len(never(texts=[("t", "BOARD'S REVIEW time, by running (a quiet pilot) first")])) == 1
    assert len(never(texts=[("t", "the board’s review time by running a")])) == 1


def test_four_words_of_a_longer_marked_line_and_words_scattered_over_pieces_are_not():
    assert never(texts=[("t", "Cut the board's review by a long way")]) == [], "four words in a row"
    assert never(texts=[("t", "Cut the board's"), ("u", "review time by running")]) == [], (
        "a run has to be inside one piece")
    assert never(texts=[("t", "the review time by running")]) == [], "not the words in order, five in a row"


def test_a_marked_line_under_five_words_must_appear_whole():
    short = {"roles": [{**MASTER["roles"][0], "bullets": [
        {"id": "b-short", "text": "Closed the audit early", "note": "never in writing"}]}]}
    assert len(never(texts=[("t", "We closed the audit early.")], **short)) == 1
    assert never(texts=[("t", "We closed the audit")], **short) == []
    assert never(texts=[("t", "closed the audit late")], **short) == []


@pytest.mark.parametrize("kind", LONG_LINES)
def test_a_marked_bullet_achievement_project_award_or_summary_variant_is_read(kind):
    data = copy.deepcopy(MASTER)
    LONG_LINES[kind](data)
    resume = Resume.model_validate(data)
    assert len(integrity.never_in_writing_hits(resume, [("summary", "b-x")], [], [])) == 1
    assert len(integrity.never_in_writing_hits(resume, [], [], [("t", MARKED)])) == 1


NB = chr(0x2011)  # a non-breaking hyphen, which reads as a hyphen


@pytest.mark.parametrize("note", [
    "Never in writing", "never in writing: the client asked", "NEVER IN WRITING", "Never-in-writing",
    "never  in   writing", f"never{NB}in{NB}writing", "Keep it so: never in writing.",
])
def test_a_note_that_says_never_in_writing_marks_the_line_in_any_spelling(note):
    data = copy.deepcopy(MASTER)
    data["roles"][0]["bullets"][0]["note"] = note
    resume = Resume.model_validate(data)
    assert len(integrity.never_in_writing_hits(resume, [("summary", "b-1")], [], [])) == 1


@pytest.mark.parametrize("note", ["approved phrasing", "never write this without asking", "in writing only", ""])
def test_other_notes_do_not_mark_a_line(note):
    data = copy.deepcopy(MASTER)
    data["roles"][0]["bullets"][0]["note"] = note
    resume = Resume.model_validate(data)
    printed = [(PRINTED, MASTER["roles"][0]["bullets"][0]["text"])]
    assert integrity.never_in_writing_hits(resume, [("summary", "b-1")], [], printed) == []


def test_the_masters_free_notes_are_not_machine_read():
    noted = {"notes": [{"id": "n-1", "text": "Never in writing: the Initech stint", "source": "old file"}]}
    assert never(cites=[("summary", "r-1")], texts=[("t", "the Initech stint")], **noted) == []


def test_a_line_used_in_two_ways_in_one_place_is_one_line():
    both = never(cites=[(PRINTED, "b-secret")], texts=[(PRINTED, MARKED)])
    assert len(both) == 1 and "cites a line" in both[0]
    assert len(never(cites=[("summary", "b-secret"), (PRINTED, "b-secret")])) == 2, "two places, two lines"


def test_a_marked_line_with_no_words_reads_nothing():
    blank = {"roles": [{**MASTER["roles"][0], "bullets": [{"id": "b-blank", "text": "", "note": "never in writing"}]}]}
    assert never(texts=[("t", "anything at all")], **blank) == []


# --- the three checks, by name, for the writer and the reviewer to be told -------------------------------------------


def test_the_always_checked_list_names_the_three_checks_with_a_rule_each():
    assert [(c.id, c.name) for c in integrity.ALWAYS_CHECKED] == [
        ("certifications", "Certifications are the master's"),
        ("unshipped-work", "Unshipped work isn't called delivered"),
        ("never-in-writing", "Lines marked never in writing stay out")]
    assert all(c.rule.endswith(".") for c in integrity.ALWAYS_CHECKED)
    assert (integrity.CERTIFICATIONS_CHECK, integrity.UNSHIPPED_CHECK, integrity.NEVER_IN_WRITING_CHECK) == tuple(
        c.name for c in integrity.ALWAYS_CHECKED)


def test_a_role_is_named_by_its_title_and_employer_never_its_id():
    resume = master()
    assert integrity.role_words(resume.roles[0]) == "Senior Manager at Northwind Labs"
    only_title = resume.roles[0].model_copy(update={"employer": ""})
    assert integrity.role_words(only_title) == "Senior Manager"
    only_employer = resume.roles[0].model_copy(update={"title": ""})
    assert integrity.role_words(only_employer) == "Northwind Labs"
    nothing = resume.roles[0].model_copy(update={"title": "", "employer": ""})
    assert integrity.role_words(nothing) == "a role in the master"
