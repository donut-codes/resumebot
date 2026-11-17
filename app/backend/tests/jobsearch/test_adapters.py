"""The job-system readers (docs/PRD.md 28.4): each one's list becomes the same plain postings, pages are followed, and a
failing or changed system raises AdapterError. Every system is a made-up employer behind a mock transport; nothing
touches the network."""

import json
import time

import httpx
import pytest

from app.jobsearch.adapters import ADAPTERS, html_to_text
from app.jobsearch.adapters._common import _from_jsonld
from app.jobsearch.fetch.polite import Fetcher
from app.sites import registry


async def test_html_to_text_makes_lines_and_bullets():
    got = html_to_text("<h2>Role</h2><p>Build things &amp; ship.</p><ul><li>Python</li><li>SQL<br/>daily</li></ul>")
    assert got == "Role\nBuild things & ship.\n- Python\n- SQL\ndaily"


async def test_html_to_text_unescapes_until_stable_and_drops_blank_lines():
    assert html_to_text("&lt;p&gt;Fish &amp;amp; chips&lt;/p&gt;&lt;p&gt;&lt;/p&gt;&lt;p&gt;Done&lt;/p&gt;") \
        == "Fish & chips\nDone"
    assert html_to_text(None) == ""
    assert html_to_text("pay &lt; 90 or more") == "pay < 90 or more"


async def test_html_to_text_drops_scripts_and_styles_and_keeps_what_never_closes():
    assert html_to_text("<p>A</p><script type='x'>if (a < b) {}</SCRIPT ><style>p{}</style><p>B</p>") == "A\nB"
    assert html_to_text("<script>one</script>keep<script>two</script><style>x</style>") == "keep"
    assert html_to_text("Open <script>never closed</p><p>rest") == "Open never closed\nrest"
    assert html_to_text('<a href="x" title="y">link</a> <span class=z>t</span>') == "link t"


# The ids are named, not left to be the markup itself: pytest puts a test's id in an environment variable, and Windows
# refuses one over 32,767 characters, so these tests errored there.
@pytest.mark.parametrize("hostile", [
    "<script>" * 25_000, "<style >" * 25_000 + "<script>" * 25_000, "<a " * 70_000, "<p " * 70_000, "<li " * 70_000,
    "<div x" * 40_000 + ">", "x" * 1_000_000,
], ids=["script", "style-then-script", "a", "p", "li", "div", "text"])
async def test_html_to_text_takes_time_in_step_with_the_length_of_markup_built_to_make_it_slow(hostile):
    """Unclosed tags made every pattern run on to the end of the page, once for each of them."""
    started = time.perf_counter()
    html_to_text(hostile)
    assert time.perf_counter() - started < 3


def _address(locality=None, region=None, country=None, **more) -> dict:
    return {"@type": "PostalAddress", "addressLocality": locality, "addressRegion": region, "addressCountry": country,
            **more}


@pytest.mark.parametrize(("job_location", "expected"), [
    ({"@type": "Place", "address": _address("Oakfield", "OH", "US")}, "Oakfield, OH, US"),
    ({"@type": "Place", "address": _address("Oakfield", "Ohio", {"@type": "Country", "name": "United States"})},
     "Oakfield, Ohio, United States"),
    ([{"address": _address("Oakfield", "OH")}, {"address": _address("Riverton", "NJ")},
      {"address": _address("Oakfield", "OH")}], "Oakfield, OH; Riverton, NJ"),  # several places, each once
    ({"address": "12 Main Street, Oakfield, OH"}, "12 Main Street, Oakfield, OH"),
    ({"@type": "Place", "name": "Oakfield Campus"}, "Oakfield Campus"),
    ({"@type": "Place", "name": "Oakfield Campus", "address": _address()}, "Oakfield Campus"),
    (_address("Oakfield", "OH"), "Oakfield, OH"),  # the address fields on the place itself
    ({"address": _address(None, "OH", "US")}, "OH, US"),
    ("Oakfield, OH", "Oakfield, OH"),
    ({"address": _address("Oakfield", "OH", "")}, "Oakfield, OH"),
    (None, None), ("", None), ([], None), ({}, None), (12, None), ([None, 3, {"address": 5}], None),
])
def test_a_job_postings_job_location_is_read_as_its_places(job_location, expected):
    got = _from_jsonld({"@type": "JobPosting", "jobLocation": job_location})
    assert got.get("location") == expected


def test_the_places_of_a_job_posting_are_given_only_to_a_posting_whose_list_left_its_place_out():
    ld = {"@type": "JobPosting", "jobLocation": {"address": _address("Oakfield", "OH", "US")}}
    assert _from_jsonld(ld, {"location": None})["location"] == "Oakfield, OH, US"
    assert _from_jsonld(ld, {"location": ""})["location"] == "Oakfield, OH, US"
    assert _from_jsonld(ld, {"location": "3 Locations"})["location"] == "Oakfield, OH, US"
    assert "location" not in _from_jsonld(ld, {"location": "Riverton, NJ"}), "a posting that has a place keeps it"
    assert _from_jsonld(ld)["location"] == "Oakfield, OH, US"


# --- the night's time -------------------------------------------------------------------------------------------------
# A board is read to its last page, so the night's read has a time limit instead (docs/PRD.md 28.17): the fetcher
# carries a deadline, and a reader that finds it passed after a page stops there, keeps the pages it has and says so.


async def test_a_fetcher_without_a_deadline_never_runs_out_of_time():
    from app.jobsearch.fetch import polite

    class Stand:
        pass

    assert not polite.timeup(Stand()) and not polite.timeup(Fetcher(httpx.AsyncClient()))
    fetch = Fetcher(httpx.AsyncClient())
    polite.give_time(fetch, 3600)
    assert not polite.timeup(fetch)
    fetch.deadline = time.monotonic() - 1
    assert polite.timeup(fetch)


async def test_a_fetcher_made_in_a_read_takes_the_reads_deadline():
    from app.jobsearch.fetch import polite

    assert Fetcher(httpx.AsyncClient()).deadline is None
    token = polite.READ_BY.set(time.monotonic() + 3600)
    try:
        assert Fetcher(httpx.AsyncClient()).deadline is not None
    finally:
        polite.READ_BY.reset(token)
    assert Fetcher(httpx.AsyncClient()).deadline is None


# --- failures ---------------------------------------------------------------------------------------------------------


# --- a career page's own text -----------------------------------------------------------------------------------------


def test_a_career_page_gives_the_posting_it_embeds_else_the_main_content_of_the_page():
    from app.jobsearch.adapters import page_text

    ld = json.dumps({"@type": "JobPosting", "description": "<p>Lead programs.</p><ul><li>Plan</li></ul>"})
    embedded = f'<html><script type="application/ld+json">{ld}</script><body>Menu and footer</body></html>'
    assert page_text(embedded) == "Lead programs.\n- Plan"
    plain = ("<html><head><title>Careers</title><style>p{}</style></head><body><p>Lead programs.</p>"
             "<script>x()</script></body></html>")
    assert page_text(plain) == "Lead programs.", "the page's title is its chrome, not the posting"
    assert page_text("") is None and page_text(None) is None and page_text("<html><script>x</script></html>") is None


def test_a_page_too_long_to_read_is_not_read_in_part():
    from app.jobsearch.adapters import page_text
    from app.jobsearch.adapters._common import HTML_TEXT_MAX

    assert page_text("<p>" + "word " * (HTML_TEXT_MAX // 5 + 10) + "</p>") is None
    assert page_text("<script>" + "x" * (HTML_TEXT_MAX * 2) + "</script><p>The posting.</p>") == "The posting."


# --- asking an open feed whether a board by a name exists (docs/PRD.md 28.2) -----------------------------------------

def test_the_feeds_that_name_an_employer_are_asked_before_the_ones_that_never_do_and_each_in_its_place():
    asked = {named: [k for k, a in ADAPTERS.items() if a.probe_names is named] for named in (True, False, None)}
    in_order = {key: at for at, key in enumerate(registry.order())}
    for named in (True, False):
        assert asked[named], "both kinds of feed are there"
        assert asked[named] == sorted(asked[named], key=in_order.__getitem__), "each in its runbook's place"


