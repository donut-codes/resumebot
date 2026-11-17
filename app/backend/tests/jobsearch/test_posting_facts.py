"""The cheap facts of a posting, read in code (the token efficiency plan spec step 1, `posting_facts.py`): place,
way of working, pay, travel and the employer, from the structured data a page embeds and from the posting's header
lines, each with the line it rests on. Every name, number and sentence is made up."""

import json

from app.jobsearch.gating import posting_facts as pf

HEADER = """Programs Lead
Location: Oakfield, ST
Work type: Hybrid
Pay range: $90,000 - $115,000 per year
Company: Fabrikam Labs

About the job
We need someone to lead the programs team. This role may require up to 25% travel.
"""


def test_header_lines_give_the_place_the_way_of_working_the_pay_the_travel_and_the_employer_each_with_its_line():
    found = pf.extract(HEADER)
    assert (found.place.value, found.place.quote) == ("Oakfield, ST", "Location: Oakfield, ST")
    assert (found.mode.value, found.mode.quote) == ("hybrid", "Work type: Hybrid")
    assert (found.pay.value, found.pay.quote) == ("$90,000-$115,000", "Pay range: $90,000 - $115,000 per year")
    assert found.employer.value == "Fabrikam Labs"
    assert "25% travel" in found.travel.value
    assert all(fact.by == pf.CODE for fact in (found.place, found.mode, found.pay, found.employer, found.travel))


def test_a_schema_org_job_posting_in_the_text_gives_its_facts_before_the_lines_do():
    posting = {"@type": "JobPosting", "title": "Programs Lead", "jobLocationType": "TELECOMMUTE",
               "hiringOrganization": {"name": "Fabrikam Labs"}, "description": "<p>Lead.</p>",
               "jobLocation": {"address": {"addressLocality": "Oakfield", "addressRegion": "ST",
                                           "addressCountry": "US"}},
               "baseSalary": {"currency": "USD", "value": {"minValue": 95000, "maxValue": 120000,
                                                          "unitText": "YEAR"}}}
    page = ('<html><script type="application/ld+json">' + json.dumps(posting)
            + "</script><body>Work type: Hybrid</body></html>")
    found = pf.extract(page)
    assert (found.mode.value, found.mode.by) == ("remote", pf.STRUCTURED)  # the page's data, not its line
    assert found.place.value == "Oakfield, ST, US" and found.place.by == pf.STRUCTURED
    assert found.pay.by == pf.STRUCTURED and found.bands[0][:2] == (95000, 120000)
    assert found.employer.value == "Fabrikam Labs"


def test_a_way_of_working_is_taken_only_where_the_posting_says_it_of_this_job():
    for said, want in [("Workplace type: On-site", "onsite"), ("Remote: Yes", "remote"), ("Hybrid", "hybrid"),
                       ("This is an on-site role in our Oakfield office.", "onsite"),
                       ("This position is fully remote.", "remote")]:
        assert pf.mode_from_text(f"Programs Lead\n{said}\nMore.").value == want
    # Benefits, the company's culture and two readings that disagree say nothing of this job.
    for silent in ["We offer remote work options and a gym.", "We are a hybrid workplace.",
                   "Work type: Remote or hybrid", "Remote: No"]:
        assert pf.mode_from_text(f"Programs Lead\nAbout the job\n{silent}") is None
    assert pf.mode_from_text("Work type: Hybrid\nThis is an on-site role.") is None


def test_pay_is_read_from_a_line_with_a_pay_word_and_a_figure_never_from_revenue_or_a_bonus():
    found, bands = pf.pay_from_text("About us\nWe raised $50,000,000 in funding.\nSigning bonus of $5,000.\n"
                                    "The base salary range is $120,000 - $150,000 a year.")
    assert found.value == "$120,000-$150,000" and bands == [(120000, 150000, found.quote)]
    assert pf.pay_from_text("Intern pay: $26 - $46 per hour")[0].value == "$54,080-$95,680"  # an hourly year
    # The line before may carry the word.
    assert pf.pay_from_text("Pay range:\n$90,000 - $115,000")[0].value == "$90,000-$115,000"
    assert pf.pay_from_text("We grew to $500 million in revenue.")[0] is None


def test_several_ranges_are_the_first_for_the_role_and_the_most_generous_for_what_may_stop_it():
    text = ("The pay range for this role is $100,000 - $120,000 per year.\n"
            "In New York the pay range is $130,000 - $160,000 per year.")
    found, bands = pf.pay_from_text(text)
    assert found.value == "$100,000-$120,000"
    assert pf.generous(bands) == (130000, 160000)
    assert pf.generous([]) is None


def test_a_quote_stands_only_when_its_words_are_in_the_posting():
    text = "Location: Oakfield, ST\nThe job is “on-site” – five days."
    assert pf.quote_in(text, "location: oakfield,  ST")
    assert pf.quote_in(text, 'The job is "on-site" - five days.')
    assert not pf.quote_in(text, "The job is hybrid")
    assert not pf.quote_in(text, "") and not pf.quote_in(text, "on")


def test_a_haiku_read_is_given_the_header_and_the_condition_lines_and_never_the_whole_posting():
    body = "\n".join(f"Paragraph {i} says one thing about the work and nothing about where it is." for i in range(60))
    text = (f"Programs Lead\nLocation: Oakfield, ST\n{body}\nTravel is up to 25% of the year.\n"
            "The pay range is $1 to $2.")
    lines = pf.lines_for(text)
    assert "Location: Oakfield, ST" in lines and "Travel is up to 25% of the year." in lines
    assert "Paragraph 59" not in lines and len(lines) < len(text) // 2
    assert len(pf.lines_for(text, limit=60)) <= 60
