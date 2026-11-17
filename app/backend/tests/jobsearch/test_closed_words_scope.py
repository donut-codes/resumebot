"""A closed-job phrase closes a role only when the posting's own part of the page says it (`browser_code.page_state`):
another listing's notice in a sidebar, or on a page whose posting part cannot be found, closes nothing. Fake pages."""

from app.jobsearch.browser import code as browser_code

URL = "https://careers.example.test/job/1"
OTHER = "<li>Analyst: No longer accepting applications</li>"


def test_a_closed_phrase_in_a_sidebar_outside_the_main_section_does_not_close_the_role():
    page = (f"<html><body><div class='rail'><ul>{OTHER}</ul></div>"
            "<main><h1>Product Lead</h1><p>Lead the product team and ship things.</p></main></body></html>")
    assert browser_code.page_state(page, URL) == browser_code.STATE_OPEN
    assert browser_code.apply_link(page, URL)["gone_words"] is None


def test_a_closed_phrase_in_a_similar_jobs_block_inside_the_main_section_does_not_close_the_role():
    page = ("<html><body><main><h1>Product Lead</h1><p>Lead the product team.</p>"
            f"<div class='similar-jobs'><ul>{OTHER}</ul></div></main></body></html>")
    assert browser_code.page_state(page, URL) == browser_code.STATE_OPEN


def test_a_closed_phrase_in_the_postings_own_main_section_closes_the_role():
    page = ("<html><body><main><h1>Product Lead</h1><p>No longer accepting applications</p></main>"
            "</body></html>")
    assert browser_code.page_state(page, URL) == browser_code.STATE_REMOVED
    assert browser_code.apply_link(page, URL)["gone_words"]


def test_a_closed_phrase_on_a_page_whose_own_part_cannot_be_found_closes_nothing():
    page = f"<html><body><nav>{OTHER}</nav></body></html>"
    assert browser_code.page_state(page, URL) == browser_code.STATE_OPEN
