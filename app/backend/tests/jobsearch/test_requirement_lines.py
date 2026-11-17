"""Code cuts a posting's requirement sections into lines, verbatim and in order (docs/PRD.md 26.12). Every posting here
is made up."""

import pytest

from app.jobsearch.research import requirement_lines as rl

BARE = """Programs Lead

About the job
We run programs for the public good.

Key Responsibilities
Lead the team.
Keep the books.

Requirements:Education:
A bachelor's degree preferred.
Experience:
Seven years of program work.
Experience coordinating vendors, partners and internal teams on one plan.
Skills:
Strong written communication.
Willingness to travel one week a quarter.

Preferred Qualifications:
- A master's degree
- Grant writing

Why Join Us
Free lunch.
"""


def _cut(text):
    return [(x.n, x.part, x.text) for x in rl.split(text)]


def test_lines_come_out_verbatim_in_posting_order_with_headings_left_out():
    assert _cut(BARE) == [
        (1, "preferred", "A bachelor's degree preferred."),
        (2, "required", "Seven years of program work."),
        (3, "required", "Experience coordinating vendors, partners and internal teams on one plan."),
        (4, "required", "Strong written communication."),
        (5, "required", "Willingness to travel one week a quarter."),
        (6, "preferred", "A master's degree"),
        (7, "preferred", "Grant writing"),
    ]


def test_a_line_after_a_heading_on_the_same_line_is_kept_without_its_heading():
    got = _cut("Qualifications: Five years of programs\n- A licence\n\nBenefits:\n- Free lunch")
    assert got == [(1, "required", "Five years of programs"), (2, "required", "A licence")]


def test_education_and_experience_under_a_preferred_heading_stay_preferred():
    got = _cut("Nice to have:\nEducation:\n- A doctorate\nExperience:\n- Ten years abroad")
    assert [(x[1], x[2]) for x in got] == [("preferred", "A doctorate"), ("preferred", "Ten years abroad")]


def test_a_known_heading_of_another_kind_ends_the_part():
    got = _cut("Requirements\n- One\nResponsibilities\n- Two\nWhat we offer\n- Three")
    assert got == [(1, "required", "One")]


def test_an_unknown_heading_that_ends_in_a_colon_changes_nothing():
    got = _cut("Requirements\n- One\nLeadership:\n- Two")
    assert [x[2] for x in got] == ["One", "Two"]


def test_a_short_line_that_only_starts_like_a_heading_is_a_line():
    got = _cut("Requirements\nExperience with SQL\nSkills in Python")
    assert [x[2] for x in got] == ["Experience with SQL", "Skills in Python"]


def test_a_posting_with_no_requirement_section_has_no_lines():
    assert rl.split("About the job\nWe do things.\n\nResponsibilities\n- Do them.") == []


def test_windows_line_ends_and_odd_spacing_do_not_change_the_lines():
    got = _cut("Requirements\r\n  -   Five years of programs  \r\n\r\n")
    assert got == [(1, "required", "Five years of programs")]


def test_each_line_is_its_own_quote_in_the_structured_items_shape():
    items = rl.as_items(rl.split("Requirements\n- Five years of programs"))
    assert items == [{"n": 1, "part": "required", "text": "Five years of programs", "quote": "Five years of programs"}]


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_an_empty_text_has_no_lines(text):
    assert rl.split(text) == []


def test_the_closing_boilerplate_ends_the_part_and_is_no_line():
    got = _cut("Preferred qualifications\n- One\nAcme is an equal opportunity employer.\nJob details\nSome place")
    assert got == [(1, "preferred", "One")]


@pytest.mark.parametrize("heading", ["What we need to see", "What you�ll bring", "Here's what you need", "About You",
                                     "Required Job Qualifications",
                                     "Must-Have Pantry Staples (Minimum Qualifications)"])
def test_the_usual_ways_a_posting_titles_its_requirements_open_the_required_part(heading):
    assert _cut(f"The job\n\n{heading}\nOne thing\nAnother thing") == [
        (1, "required", "One thing"), (2, "required", "Another thing")]


def test_a_short_title_case_line_under_requirements_that_is_not_a_heading_is_a_line():
    assert [x[2] for x in _cut("Requirements\nProject Management\nStrong communication skills")] == [
        "Project Management", "Strong communication skills"]


@pytest.mark.parametrize("heading", [
    "What we need", "What You Need", "What you'll need", "What you’ll need", "What we're looking for",
    "What we’re looking for", "Who you are", "Your background", "Basic qualifications", "Minimum Qualifications",
    "Required qualifications", "What we need:", "What we're looking for:", "Who we're looking for",
    "What you'll bring", "What you bring to the team", "What you have",
    "What you will have", "What you’ll need to be successful",
    "What you’ve done",
])
def test_a_common_requirement_heading_variant_opens_the_required_part_bare_or_with_a_colon(heading):
    text = f"Programs Lead\n\nThe Role\nLead the work.\n\n{heading}\nSeven years of program work.\nA clear writer.\n"
    assert [x.text for x in rl.split(text)] == ["Seven years of program work.", "A clear writer."]
    assert {x.part for x in rl.split(text)} == {"required"}


# Unusual ways a posting titles what it wants: a lead-in sentence, a quoted line, "you are / you have" (PRD 26.12).
@pytest.mark.parametrize("heading", [
    "“I am the person Acme Corp is looking for.”",
    "\"I am the person Acme Corp is looking for.\"",
    "The person we're looking for",
    "The person we’re looking for.",
    "Acme is looking for",
    "What we’re looking for in you",
    "What we're looking for in a candidate:",
    "“Who you are.”",
    "“What you’ll bring.”",
    "You are:",
    "You are someone who",
    "You are a person who:",
    "You have",
    "You’ll have",
    "You will bring",
])
def test_a_lead_in_or_quoted_heading_opens_the_required_part(heading):
    text = f"Lead\n\nThe Role\nLead the work.\n\n{heading}\n· Seven years of program work.\n· A clear writer.\n"
    assert _cut(text) == [(1, "required", "Seven years of program work."), (2, "required", "A clear writer.")]


QUOTED = """About the role
We run programs. Acme is looking for a lead to run them, working across teams.
“I am the person Acme Corp is looking for.”
· You have 10+ years of program management experience.
· You have asset management experience (preferred).
“I can apply in less than 4 minutes.”
You’ve reviewed this job posting and you’re ready to start. If this role isn’t what you’re looking for, see others.
"""


def test_a_quoted_heading_opens_a_part_and_the_next_quoted_line_ends_it():
    assert _cut(QUOTED) == [
        (1, "required", "You have 10+ years of program management experience."),
        (2, "preferred", "You have asset management experience (preferred)."),
    ]


def test_a_sentence_that_only_ends_in_looking_for_is_no_heading_when_it_is_long_or_starts_with_if():
    got = _cut("Requirements\n- One\nIf this is not what you're looking for.\n- Two\n"
               "We spent years working out which kind of person we are looking for.\n- Three")
    assert [x[2] for x in got] == [
        "One", "If this is not what you're looking for.", "Two",
        "We spent years working out which kind of person we are looking for.", "Three"]


def test_a_sentence_that_starts_with_you_are_is_no_heading():
    assert rl.split("About the job\nYou are responsible for the team.\nYou have a desk.\n- Do it.") == []


def test_a_quoted_heading_of_a_part_that_is_not_a_requirement_ends_the_part():
    got = _cut("Requirements\n- One\n“What we offer.”\n- Free lunch")
    assert got == [(1, "required", "One")]


@pytest.mark.parametrize("heading", [
    "We're looking for you to bring:",
    "What We’re Looking For On Your Resume",
    "To qualify for the role, you must have",
    "What You’ll Bring To The Role",
    "Your Expertise:",
    "This might describe you",
    "## What We Look For in a Candidate",
    "### Who you are ###",
])
def test_other_usual_titles_of_the_requirements_open_the_required_part(heading):
    text = f"Lead\n\nThe Role\nLead the work.\n\n{heading}\n- Seven years of program work.\n- A clear writer."
    assert _cut(text) == [(1, "required", "Seven years of program work."), (2, "required", "A clear writer.")]


@pytest.mark.parametrize("heading", ["Ideally, you'll also have", "It Would Be Great If You Had", "## Nice to have"])
def test_a_lead_in_that_says_ideally_opens_the_preferred_part(heading):
    text = f"Requirements\n- One\n{heading}\n- Two"
    assert _cut(text) == [(1, "required", "One"), (2, "preferred", "Two")]


def test_a_markdown_heading_of_another_part_ends_the_part_and_is_no_line():
    assert _cut("Requirements\n- One\n## Responsibilities\n- Two") == [(1, "required", "One")]


@pytest.mark.parametrize("heading", ["The impact you will have:", "The Impact You’ll Have"])
def test_a_lead_in_that_ends_in_you_have_but_titles_another_part_ends_the_part(heading):
    assert _cut(f"Requirements\n- One\n{heading}\n- Two") == [(1, "required", "One")]


# Headings built on capability, competency, proficiency and fluency words (PRD 26.12; found 2026-11-03 on a role whose
# "Expected Capabilities & Tooling Fluency" section held four requirement items that were never graded).
CAPABILITIES = """Claims Automation Lead

About the role
Northwind Mutual runs claims work for small carriers.

Qualifications:
- Seven years of claims operations work.
- A bachelor's degree in a related field.

Responsibilities
- Run the weekly review.

Expected Capabilities & Tooling Fluency
Claims workflow mapping and optimization: Strong understanding of how a claim moves from intake to payment.
Generative AI and agentic systems: Working fluency with prompt design and with checking an assistant's answers.
Reporting and query tools: Hands-on use of dashboards.
Vendor management: Has run a vendor through a pilot.

Benefits
Free lunch.
"""


def test_a_capabilities_section_after_another_part_is_cut_into_required_lines():
    assert _cut(CAPABILITIES) == [
        (1, "required", "Seven years of claims operations work."),
        (2, "required", "A bachelor's degree in a related field."),
        (3, "required", "Claims workflow mapping and optimization: Strong understanding of how a claim moves from "
                        "intake to payment."),
        (4, "required", "Generative AI and agentic systems: Working fluency with prompt design and with checking an "
                        "assistant's answers."),
        (5, "required", "Reporting and query tools: Hands-on use of dashboards."),
        (6, "required", "Vendor management: Has run a vendor through a pilot."),
    ]


@pytest.mark.parametrize("heading", [
    "Expected Capabilities",
    "Expected Capabilities:",
    "Expected Capabilities & Tooling Fluency",
    "Core Competencies",
    "Key Competencies",
    "Required Competencies:",
    "Technical Proficiencies",
    "Technical Competencies:",
    "Competencies:",
    "## Expected Capabilities",
])
def test_a_heading_built_on_a_capability_word_opens_the_required_part(heading):
    text = f"Lead\n\nThe Role\nLead the work.\n\n{heading}\n- Seven years of program work.\n- A clear writer."
    assert _cut(text) == [(1, "required", "Seven years of program work."), (2, "required", "A clear writer.")]


def test_a_capability_heading_inside_a_requirement_part_is_no_line():
    got = _cut("Qualifications\n- One\nExpected Capabilities & Tooling Fluency\n- Two")
    assert got == [(1, "required", "One"), (2, "required", "Two")]


@pytest.mark.parametrize("heading", ["Preferred Competencies", "Desired Competencies:"])
def test_a_preferred_capability_heading_opens_the_preferred_part(heading):
    got = _cut(f"Requirements\n- One\n{heading}\n- Two")
    assert got == [(1, "required", "One"), (2, "preferred", "Two")]


@pytest.mark.parametrize("heading", [
    "Our capabilities",
    "Our Core Competencies:",
    "About our capabilities",
    "Acme capabilities",
    "Platform Capabilities",
    "Our capabilities include:",
])
def test_a_capabilities_heading_of_the_company_or_a_product_opens_no_part(heading):
    assert rl.split(f"About the job\n{heading}\n- We build claims tools.\n- We ship them every week.") == []


def test_a_capabilities_heading_of_the_company_ends_the_requirement_part_before_it():
    got = _cut("Requirements\n- One\nOur Core Competencies:\n- We build claims tools.")
    assert got == [(1, "required", "One")]


def test_a_line_that_only_starts_with_a_capability_word_opens_no_part():
    text = ("About the job\nProficiency in Python: pandas and numpy\nCompetencies are reviewed yearly.\n"
            "Fluency in English")
    assert rl.split(text) == []


def test_an_item_labelled_by_a_capability_word_opens_no_part_outside_one():
    assert rl.split("About the job\nTechnical Proficiency: Comfort with VoIP systems and Microsoft Office.") == []


def test_an_item_labelled_by_a_capability_word_keeps_its_label_inside_a_part():
    got = _cut("Requirements\n- Technical Proficiency: Comfort with VoIP systems.\n"
               "- Core Competencies: Attention to detail")
    assert [x[2] for x in got] == ["Technical Proficiency: Comfort with VoIP systems.",
                                   "Core Competencies: Attention to detail"]


# "Supervisory Responsibility" follows a competencies list in many postings: it ends the part, with its paragraph.
@pytest.mark.parametrize("heading", [
    "Supervisory Responsibility", "Supervisory Responsibility:", "Supervisory Responsibilities",
    "Supervisory Responsibility: None",
])
def test_a_supervisory_responsibility_heading_ends_the_part_and_is_no_line(heading):
    got = _cut(f"Competencies\n- One\n{heading}\nLeads the team and directs its work.\nQualifications\n- Two")
    assert got == [(1, "required", "One"), (2, "required", "Two")]


# A competencies list sits last in many postings, or ahead of parts the splitter has no heading for, so a part opened
# by a capability heading also ends at the notices and headings that close a posting (PRD 26.12; found 2026-11-03 when
# the first version ran on into pay notices, ad tags, recruiter notices and location lists).
@pytest.mark.parametrize("tail", [
    "The compensation range for this position in Ohio is $90,000 - $120,000.",
    "Applicants can expect a base compensation range of $80,000 to $95,000.",
    "Target pay includes on-target earnings with commission.",
    "#LI-AB1",
    "#LI-Hybrid",
    "Apply now »",
    "Time Type: Full time",
    "Job Type: Full Time",
    "Posting Start Date: 10/21/2026",
    "Posting End Date: 11/06/2026",
    "Travel Percentage: 10%",
    "Travel Required",
    "Pay Range",
    "United Kingdom Pay Ranges:",
    "Success Measures",
    "What success looks like",
    "Anticipated Weekly Hours",
    "Measures of Success",
    "Success in this role will be shown by:",
    "Essential Duties and Responsibilities",
    "Essential Functions",
    "Job Role Summary",
    "Other Duties",
    "Other Information:",
    "Additional Locations:",
    "What we bring to you",
    "Our approach to compensation emphasizes teamwork and career growth.",
    "We are aware of recent recruitment scams that use our name.",
    "Northwind does not extend unsolicited employment offers.",
    "Note to third-party recruiters: Northwind does not work with agencies.",
])
def test_a_capability_part_ends_at_the_notices_and_headings_that_close_a_posting(tail):
    text = f"Competencies\n- Clear writer.\n- Calm under pressure.\n{tail}\n- Not a requirement.\nNor is this one."
    assert _cut(text) == [(1, "required", "Clear writer."), (2, "required", "Calm under pressure.")]


def test_a_capability_part_ends_at_a_closing_heading_and_a_later_requirement_heading_opens_a_part_again():
    text = "Core Competencies\n- One\nSuccess Measures\n- Shipped on time\nQualifications\n- Two"
    assert _cut(text) == [(1, "required", "One"), (2, "required", "Two")]


def test_the_milliman_shaped_posting_with_a_pay_notice_and_ad_tag_after_its_capabilities_is_cut_there():
    text = CAPABILITIES.replace(
        "Benefits\nFree lunch.\n",
        "The compensation range for this position in Ohio is $90,000 - $120,000.\n"
        "Northwind pays by the standard of its market.\n#LI-NW1\nApply now »\n")
    got = _cut(text)
    assert [x[0] for x in got] == [1, 2, 3, 4, 5, 6]
    assert got[-1][2] == "Vendor management: Has run a vendor through a pilot."


@pytest.mark.parametrize("item", [
    "Compensation analysis and benchmarking of roles.",
    "Travel to client sites twice a quarter.",
    "Time management across several projects.",
    "Job costing and margin reporting.",
    "Posting journal entries to the ledger.",
    "Pay attention to detail.",
    "Success metrics tracking for a product.",
    "Essential skills for running a queue.",
    "Other teams' priorities are weighed.",
    "Apply knowledge of the claims process.",
    "Additional languages are a plus.",
    "Measures of quality built into daily work.",
])
def test_a_capability_item_that_only_resembles_a_closing_line_stays_a_line(item):
    got = _cut(f"Competencies\n- Clear writer.\n- {item}\n- Calm under pressure.")
    assert [x[2] for x in got] == ["Clear writer.", item, "Calm under pressure."]


def test_a_part_opened_by_another_heading_after_a_capability_part_is_not_cut_at_those_lines():
    # Only a part a capability heading opened is cut at them: a Qualifications part is cut as it was before.
    text = "Competencies\n- One\nQualifications\n- Two\nApply now »\n- Three"
    assert [x[2] for x in _cut(text)] == ["One", "Two", "Apply now »", "Three"]


# Items read as items, not by their headings (PRD 26.12; found 2026-11-03 reviewing fifteen roles: lines that call
# themselves "(preferred)" were graded as required, a benefit bullet opened a requirement part, "What you will need" was
# no heading, the closing equal-opportunity text was cut as requirements, and an item's label was stripped).
ITEMS_THAT_SAY_OPTIONAL = [
    "Asset management experience (preferred)",
    "Relevant degree preferred.",
    "Supervisory experience, strongly preferred",
    "A master's degree is highly preferred.",
    "Cantonese is preferred but not required.",
    "SAFe certification is a plus",
    "Knowledge of Spark (a plus)",
    "Claims experience a big plus.",
    "Tableau is nice to have.",
    "Experience with agentic tools (nice to have)",
]


@pytest.mark.parametrize("item", ITEMS_THAT_SAY_OPTIONAL)
def test_an_item_that_says_it_is_preferred_is_a_preferred_line_inside_a_required_part(item):
    got = _cut(f"Requirements\n- Seven years of program work.\n- {item}\n- A clear writer.")
    assert got == [(1, "required", "Seven years of program work."), (2, "preferred", item),
                   (3, "required", "A clear writer.")]


def test_a_plain_line_that_says_it_is_preferred_is_a_preferred_line_not_a_heading():
    got = _cut("Requirements\nSeven years of program work.\nAsset management experience (preferred)\nA clear writer.")
    assert got == [(1, "required", "Seven years of program work."),
                   (2, "preferred", "Asset management experience (preferred)"), (3, "required", "A clear writer.")]


@pytest.mark.parametrize("item", [
    "Comfort with the client's preferred communication tools.",
    "The strongly preferred candidate lives in the region.",
    "A bachelor's degree is required; a master's degree is preferred.",
    "A bachelor's degree in business; a master's degree preferred.",
    "A bachelor's degree in business (master's preferred)",
    "Pluses and minuses of each vendor weighed in writing.",
    "At least five years of program work (preferred ten).",
])
def test_a_line_that_only_uses_the_word_or_also_asks_for_something_required_stays_required(item):
    assert _cut(f"Requirements\n- {item}") == [(1, "required", item)]


@pytest.mark.parametrize("heading", ["What you will need", "What You Will Need:", "What you will need to succeed",
                                     "What you’ll need to succeed", "What you would need"])
def test_what_you_will_need_opens_the_required_part(heading):
    text = f"Lead\n\nThe Role\nLead the work.\n\n{heading}\n- Seven years of program work.\n- A clear writer."
    assert _cut(text) == [(1, "required", "Seven years of program work."), (2, "required", "A clear writer.")]


def test_a_benefit_bullet_labelled_by_the_word_skills_opens_no_requirement_part():
    text = ("Benefits\n- Expand Your Skills: Unlock your potential with training.\n"
            "- Enjoy Where You Work: Thrive in our modern offices.")
    assert rl.split(text) == []


def test_a_bullet_labelled_by_a_generic_requirement_word_opens_no_part_after_a_part_that_ended():
    text = "About the job\n- Basic life support: a certificate is on file.\n- Minimum age: 18"
    assert rl.split(text) == []


def test_an_item_labelled_by_the_word_skills_keeps_its_label_inside_a_required_part():
    label = "Strong business analysis and process management skills"
    got = _cut(f"Requirements\n- Seven years of program work.\n - {label}: runs discovery workshops.\n"
               "Process modelling skills: models processes in BPMN.")
    assert [x[2] for x in got] == ["Seven years of program work.", f"{label}: runs discovery workshops.",
                                   "Process modelling skills: models processes in BPMN."]


def test_a_heading_made_of_the_word_skills_still_opens_the_part_and_is_no_line_when_its_text_follows():
    assert _cut("Technical Skills: SQL, Python\n- Spark") == [(1, "required", "SQL, Python"), (2, "required", "Spark")]


@pytest.mark.parametrize("closing", [
    "Acme provides equal employment opportunities to all qualified applicants.",
    "Equal Employment Opportunities",
    "EOE/M/F/Vet/Disability",
    "Acme is a tobacco-free workplace.",
    "All of our locations are tobacco free including in company vehicles.",
    "Acme is a drug-free workplace and a smoke-free campus.",
    "All qualified applicants will be considered without regard to race, color or religion.",
])
def test_the_closing_equal_opportunity_and_workplace_lines_end_the_part_and_are_no_line(closing):
    assert _cut(f"Requirements\n- One\n{closing}\n- Two") == [(1, "required", "One")]


def test_a_requirement_that_only_names_tobacco_or_employment_stays_a_line():
    got = _cut("Requirements\n- Domain knowledge in fuel and tobacco tax compliance.\n- Equal pay analysis experience.")
    assert [x[2] for x in got] == ["Domain knowledge in fuel and tobacco tax compliance.",
                                   "Equal pay analysis experience."]


def test_a_career_framework_value_is_no_requirement():
    got = _cut("Requirements\n- One\nCareer Framework:\nProfessional - Lead Associate (P4)\nCareer Framework\nSME - P5")
    assert got == [(1, "required", "One")]


@pytest.mark.parametrize("heading", [
    "What Does Success Look Like in This Role", "What does success look like?", "What success looks like:",
    "## What Does Success Look Like", "Success in this role",
])
def test_a_what_success_looks_like_section_ends_the_part_and_a_later_heading_opens_one_again(heading):
    text = (f"Requirements\n- One\n\n{heading}\nWithin your first year the team should work differently.\n"
            "AI is embedded across the organization.\n\nQualifications\n- Two")
    assert _cut(text) == [(1, "required", "One"), (2, "required", "Two")]


def test_a_line_that_only_starts_like_a_success_heading_is_a_line():
    got = _cut("Requirements\n- Success in this role requires a clear writer.")
    assert [x[2] for x in got] == ["Success in this role requires a clear writer."]


def test_the_benefits_that_follow_a_what_you_will_need_part_are_no_requirements():
    text = ("What you will need\nAcme is looking for curious people.\n- Seven years of program work.\n"
            "What you will gain at Acme\nYou will get the chance to grow.\n- Expand Your Skills: training.\n"
            "Acme provides equal employment opportunities to all.\nPlease note: Acme will not hire by text.")
    assert _cut(text) == [(1, "required", "Acme is looking for curious people."),
                          (2, "required", "Seven years of program work.")]


def test_a_mark_in_capitals_counts_and_a_bracket_that_holds_more_than_the_mark_does_not():
    got = _cut("Requirements\n- Python (Preferred)\n- SQL (C++ is a plus)\n- Spark (nice to have)")
    assert [(x[1], x[2]) for x in got] == [("preferred", "Python (Preferred)"), ("required", "SQL (C++ is a plus)"),
                                           ("preferred", "Spark (nice to have)")]


def test_a_short_plain_line_that_names_a_part_in_brackets_is_still_a_heading():
    got = _cut("Requirements\n- One\nNice to have (preferred qualifications)\n- Two")
    assert got == [(1, "required", "One"), (2, "preferred", "Two")]


def test_a_bullet_mark_the_list_does_not_know_hides_neither_a_heading_label_nor_an_item_label():
    got = _cut("Requirements\n● Required Experience: 6 years of program work\n● Process modelling skills: BPMN")
    assert [x[2] for x in got] == ["6 years of program work", "● Process modelling skills: BPMN"]


@pytest.mark.parametrize("heading", ["Certifications (Preferred)", "Domain Knowledge (Preferred)", "SAP (Preferred)",
                                     "2. Certifications (Preferred)"])
def test_a_title_case_line_with_the_mark_alone_heads_a_preferred_part(heading):
    got = _cut(f"Requirements\n- One\n{heading}\nProject Management Professional (PMP)\nScrum Master")
    assert got == [(1, "required", "One"), (2, "preferred", "Project Management Professional (PMP)"),
                   (3, "preferred", "Scrum Master")]


# --- the bullets a posting lists as its duties (the analyst grades each one: `analysis.submit`) -----------------------

DUTIES = """Programs Lead

About the job
We run programs for the public good.

Key Responsibilities
Leadership
- Lead the team of analysts
- Plan: keep the books in order
Delivery:
- Ship each plan on time
- Other duties as assigned

Requirements
- Seven years of program work

What we offer
- Free lunch
"""


def _duties(text):
    return [(d.heading, d.text) for d in rl.duty_bullets(text)]


def test_the_bullets_under_a_responsibilities_heading_are_its_duties_and_a_group_heading_does_not_end_it():
    assert _duties(DUTIES) == [("Key Responsibilities", "Lead the team of analysts"),
                               ("Key Responsibilities", "Plan: keep the books in order"),
                               ("Key Responsibilities", "Ship each plan on time")]


@pytest.mark.parametrize("heading", [
    "Responsibilities", "Responsibilities:", "## Responsibilities", "Key Responsibilities",
    "Essential Duties and Responsibilities", "Duties", "What you'll do", "What You Will Do:",
    "What you will be doing", "In this role, you will:"])
def test_each_way_a_posting_heads_its_duties_opens_the_part(heading):
    got = _duties(f"About the job\n- Not a duty\n{heading}\n- Run the weekly meeting\nRequirements\n- Seven years")
    assert [text for _, text in got] == ["Run the weekly meeting"]


def test_duties_written_as_prose_are_none():
    text = ("Responsibilities\nLead the team.\nKeep the books.\n\nResponsibilities include planning.\n"
            "Requirements\n- Five years")
    assert _duties(text) == []


def test_bullets_under_a_heading_that_is_not_the_duties_are_not_duties():
    assert _duties("About the job\n- We run programs\nRequirements\n- Seven years\nWhat we offer\n- Free lunch") == []


def test_the_equal_opportunity_text_and_a_requirements_heading_end_the_duties():
    text = ("Responsibilities\n- Lead the team\nAcme provides equal employment opportunities to all.\n"
            "- Keep the books\nResponsibilities\n- Plan the year\nQualifications\n- Seven years")
    assert [t for _, t in _duties(text)] == ["Lead the team", "Plan the year"]


def test_a_posting_whose_only_duty_bullet_is_the_catch_all_has_none():
    assert _duties("Responsibilities\n- Other duties as assigned\n- Perform additional duties as required") == []
