"""A page the app reads for a role is kept as the role's posting only when the page names the role (docs/PRD.md 39.29).

A role's kept text was an employer's generic careers page (its values and mission). The browser had
handed back an employer link that draws a posting in the browser, and the app's own read of that link kept the text of
the page around the posting. `whole.why_not` says only that a text is long enough and no wall, so nothing asked whether
the page was this role's. The check is pure: the role's title against the page's text, its embedded JobPosting's title
and its headings. Review of the first version found it let too much through: a heading or a JobPosting title with
most of the title's words in another job's title, a heading that lists many jobs, and a text with the title's words
anywhere, far apart. Every title and sentence is made up."""

import pytest

from app.jobsearch.browser import posting_match
from app.jobsearch.postings import this_posting

TITLE = "Senior Programs Lead"
PLAIN = "<p>We care about our people and the places where they live and work. Our values are listed below.</p>"


def page(head: str = "", body: str = PLAIN) -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def names(text: str, html: str = "", title: str | None = TITLE, posting_title: str | None = None) -> bool:
    return this_posting.names_the_role(title, text, html or page(), posting_title)


# --- the text names the role ---------------------------------------------------------------------------------------


def test_a_text_with_the_roles_title_in_it_is_its_posting():
    assert names("Senior Programs Lead\nRemote\nYou will lead the programs of the team.")


def test_a_text_with_three_quarters_of_the_titles_words_is_its_posting():
    assert names("Programs Lead Manager\nYou will lead the programs of the team.", title="Senior Programs Lead Manager")


def test_a_text_with_fewer_of_the_titles_words_is_not_its_posting():
    assert not names("Lead\nYou will lead the work of the team.")


def test_a_text_that_spells_out_a_short_form_of_the_title_is_its_posting():
    assert names("Sr. Programs Lead, remote", title="Senior Programs Lead")


SCATTERED = ("Our senior people look after each other and the places where they live and work every day. We run "
             "programs for our neighbours in each of those places, and we ask every team to say what it learned. Few "
             "things matter more to us than the chance to lead by example.")


def test_a_text_with_the_titles_words_far_apart_is_not_its_posting():
    assert not names(SCATTERED)


def test_a_text_with_the_titles_words_together_in_another_order_is_its_posting():
    assert names("About the job: Programs Lead, Senior. You will lead the programs of the team.")


# --- the page names the role besides ----------------------------------------------------------------------------------


def test_a_text_with_no_title_is_its_posting_when_the_job_posting_the_page_embeds_has_the_roles_title():
    assert names(PLAIN, posting_title="Senior Programs Lead - United States")


def test_a_text_with_no_title_is_not_its_posting_when_the_job_posting_the_page_embeds_is_another_job():
    assert not names(PLAIN, posting_title="Warehouse Associate")


@pytest.mark.parametrize("head", [
    '<meta property="og:title" content="Senior Programs Lead | Acme Careers">',
    "<title>Senior Programs Lead - Acme</title>",
])
def test_a_text_with_no_title_is_its_posting_when_the_pages_title_or_social_title_is_the_roles(head):
    assert names(PLAIN, page(head))


def test_a_text_with_no_title_is_its_posting_when_the_pages_h1_has_the_title_and_the_sites_tail():
    assert names(PLAIN, page(body="<h1>Senior Programs Lead - United States - 44263</h1>" + PLAIN))


def test_a_heading_that_has_the_titles_words_in_another_order_is_another_jobs_title():
    assert not names(PLAIN, page(body="<h1>Programs Lead, Senior</h1>" + PLAIN))


def test_a_heading_that_lists_many_jobs_is_not_the_roles_title():
    head = "<title>Senior Programs Lead, " + ", ".join(f"Role Number {i}" for i in range(40)) + "</title>"
    assert not names(PLAIN, page(head))


def test_a_job_posting_whose_title_has_the_roles_words_in_another_job_is_not_the_roles_posting():
    assert not names(PLAIN, posting_title="Senior Analyst, Programs Lead")


def test_a_title_of_only_white_space_is_no_headings_title():
    assert not this_posting.has_title_heading(page("<title>Careers</title>", "<h1>Open roles</h1>"), "   ")


def test_a_generic_careers_page_names_nothing_of_the_role():
    head = "<title>Careers at Acme</title>"
    assert not names(PLAIN, page(head, "<h1>Our values</h1>" + PLAIN))


# --- when nothing can be checked -----------------------------------------------------------------------------------


@pytest.mark.parametrize("title", ["プログラム リード", "", None, "- / -"])
def test_a_title_with_no_word_that_can_be_checked_passes(title):
    assert names(PLAIN, title=title)


def test_title_together_is_none_for_a_title_with_no_word_that_can_be_checked():
    assert posting_match.title_together("プログラム リード", "anything") is None


def test_title_together_counts_the_share_of_the_titles_words_in_one_stretch_of_the_text():
    assert posting_match.title_together("Senior Programs Lead", "the programs lead of a team") == pytest.approx(2 / 3)


def test_title_together_counts_a_short_form_as_the_long_one():
    assert posting_match.title_together("Senior Programs Lead", "Sr. Programs Lead, remote") == 1.0


def test_title_together_does_not_count_words_far_apart_in_the_text():
    assert posting_match.title_together("Senior Programs Lead", SCATTERED) == pytest.approx(1 / 3)


# --- a title a page or a job system states is the role's job, or another ----------------------------------------------


@pytest.mark.parametrize("said", [
    "Senior Programs Lead", "Sr. Programs Lead", "Senior Programs Lead - United States - 44263",
    "Senior Programs Lead | Acme Careers",
])
def test_a_title_stated_for_the_job_is_the_roles_when_it_is_the_title_in_one_of_its_usual_writings(said):
    assert this_posting.same_job(TITLE, said)


def test_a_title_stated_for_the_job_is_the_roles_when_the_role_holds_it_with_a_tag_of_its_own():
    assert this_posting.same_job("Senior Programs Lead - (Lancaster area) full time", "Senior Programs Lead")


@pytest.mark.parametrize("said", [
    "Warehouse Associate", "Senior Analyst, Programs Lead", "Programs Lead, Senior", "Programs Manager, Finance",
])
def test_a_title_stated_for_the_job_is_another_jobs_when_it_is_not_the_title(said):
    assert not this_posting.same_job(TITLE, said)


@pytest.mark.parametrize("title, said", [("プログラム リード", "Warehouse Associate"), ("", "Warehouse Associate"),
                                         (None, "Warehouse Associate"), (TITLE, ""), (TITLE, None)])
def test_nothing_is_told_when_the_title_or_the_stated_title_has_no_word_to_check(title, said):
    assert this_posting.same_job(title, said)
