"""The code fillers of the structured record (the structured data plan spec step 2): pay, years, degree, work mode
and travel, each with the line it came from; the title from the listing. Every name and figure is made up. The
disagreements the plan's parse check found are fixtures."""

from app.jobsearch.gating import structured_fill as sf
from app.jobsearch.postings.record import check_quotes

POSTING = """Programs Lead
Location: Oakfield, ST
Work type: Hybrid
Pay range: $90,000 - $115,000 per year

About the job
This role may require up to 25% travel.
Requirements
Bachelor's degree in a related field.
At least 7 years of experience leading a programs team.
Preferred: a Master's degree and 10+ years of experience.
"""


def test_every_filler_gives_its_value_with_the_line_it_came_from():
    record = sf.fill(POSTING, listing_title="Programs Lead")
    f = record.fields
    assert (f["pay"].value, f["pay"].quote) == ("$90,000-$115,000", "Pay range: $90,000 - $115,000 per year")
    assert (f["work_mode"].value, f["work_mode"].quote) == ("hybrid", "Work type: Hybrid")
    assert f["travel"].quote == "This role may require up to 25% travel."
    assert (f["years"].value, f["years"].quote) == (7, "At least 7 years of experience leading a programs team.")
    assert (f["degree"].value, f["degree"].quote) == ("bachelor", "Bachelor's degree in a related field.")
    assert all(field.source == "code" for name, field in f.items() if name != "title")
    assert check_quotes(record, POSTING) == []


def test_the_title_is_the_listings_never_a_line_of_the_text():
    record = sf.fill("Senior Widget Wrangler\nWe need 3 years of experience.", listing_title="Programs Lead")
    assert record.fields["title"].value == "Programs Lead" and record.fields["title"].source == "listing"
    assert "title" not in sf.fill("Senior Widget Wrangler\nMore.", listing_title="").fields


def test_a_silent_posting_stays_empty_and_is_never_guessed():
    record = sf.fill("About us\nWe make widgets and love our team.", listing_title="")
    assert record.fields == {}


def test_several_years_figures_in_one_line_keep_the_largest_and_note_the_rest_for_a_model():
    record = sf.fill("Requires 3-5 years of experience, or 8 years in a related field.", listing_title="")
    years = record.fields["years"]
    assert years.value == 8 and years.also == (3, 5)


def test_a_preferred_line_is_not_a_requirement_for_years_or_degree():
    record = sf.fill("A Master's degree and 10+ years of experience are a plus.", listing_title="")
    assert "years" not in record.fields and "degree" not in record.fields


def test_the_highest_stated_degree_wins_and_a_bare_degree_word_is_just_a_degree():
    assert sf.fill("PhD or Master's required.", listing_title="").fields["degree"].value == "phd"
    assert sf.fill("A degree is required.", listing_title="").fields["degree"].value == "degree"
    assert sf.fill("An MBA is required.", listing_title="").fields["degree"].value == "master"


def test_hourly_pay_is_a_year_and_revenue_is_not_pay():
    assert sf.fill("Intern pay: $26 - $46 per hour", listing_title="").fields["pay"].value == "$54,080-$95,680"
    assert "pay" not in sf.fill("We raised $50,000,000 in funding.", listing_title="").fields


def test_the_other_ranges_a_posting_gives_are_noted_beside_the_first_one():
    text = ("The pay range for this role is $100,000 - $120,000 per year.\n"
            "In New York the pay range is $130,000 - $160,000 per year.")
    pay = sf.fill(text, listing_title="").fields["pay"]
    assert pay.value == "$100,000-$120,000" and pay.also == ("$130,000-$160,000",)
