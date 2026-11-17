"""A posting recipe is found by code on pages as employers really serve them (docs/PRD.md path 49): the container that
holds the whole posting in a plain fetch of the page, or, where the HTML holds no text, the schema.org JobPosting the
page embeds. The pages are trimmed, anonymised copies of four hosts' (`posting_pages.py`); no test reaches the network,
a model or a real browser."""

from datetime import UTC, datetime

import pytest

from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser.recipes import html_record
from app.jobsearch.browser.recipes import html_run as html_recipe
from app.jobsearch.fetch import polite
from app.jobsearch.postings import closed_words, whole
from app.sites import html_select as hs
from app.sites import skills
from app.sites.models import SiteRecipe, SiteRecording
from tests.jobsearch import posting_pages as pages
from tests.jobsearch.test_html_recipes import HOST, FakeFetcher, _host_asked, _only, chrome, plain, quick  # noqa: F401
from tests.support import OWNER, pin_clock

LINK = f"https://{HOST}/job/1"
LAST_LINE = "A record of teaching others how to do the same work"


def _container(page: str) -> hs.Node:
    root = hs.parse(page)
    selector = html_record.text_selector(root)
    assert selector is not None, "no text element was found"
    node = hs.select_one(root, selector)
    assert node is not None
    return node


def test_the_container_of_a_store_page_holds_every_section_not_the_first_one_or_the_menus():
    text = _container(pages.store_page()).text()
    assert text.startswith("Description") and "Basic Qualifications" in text and LAST_LINE in text
    assert whole.why_not(text) is None
    assert "Share this job" not in text and "Job details" not in text and "Teams" not in text


def test_the_container_of_a_device_page_is_the_one_that_holds_all_its_articles_not_the_first_article():
    text = _container(pages.device_page()).text()
    assert "General Information" in text and "Why Work Here" in text and "Description and Requirements" in text
    assert "A day in the life" in text and "3+ years of professional experience" in text
    assert whole.why_not(text) is None
    assert "Search jobs" not in text and "Deutsch" not in text and "Legal" not in text and "Apply" not in text


def test_a_page_whose_html_holds_almost_no_text_has_no_text_element():
    assert html_record.text_selector(hs.parse(pages.script_drawn_page())) is None


def test_the_jobposting_a_page_embeds_is_its_text_with_the_markup_gone_and_only_when_there_is_one():
    text = html_recipe.jsonld_text(pages.script_drawn_page())
    assert text is not None and "&lt;" not in text and "<p>" not in text
    assert "A day in the life" in text and LAST_LINE in text and whole.why_not(text) is None
    assert html_recipe.jsonld_text(pages.store_page()) is None
    assert html_recipe.jsonld_text("<html><script type='application/ld+json'>{broken</script></html>") is None


def _host_recipe(**over) -> dict:
    return {"kind": "html_posting", "host": HOST, "url_pattern": r"https://[^/]+/job/\d+", **over}


def test_a_host_recipe_reads_either_a_text_element_or_the_embedded_jobposting_and_never_neither_or_both():
    assert isinstance(skills.parse_recipe(_host_recipe(jsonld=True)), skills.HtmlPostingRecipe)
    assert isinstance(skills.parse_recipe(_host_recipe(text="div.content")), skills.HtmlPostingRecipe)
    for over in ({}, {"text": "div.content", "jsonld": True}, {"jsonld": False}):
        with pytest.raises(ValueError):
            skills.parse_recipe(_host_recipe(**over))


def _rendered_script_drawn() -> str:
    """What Chrome shows of the script-driven page: the text is there, in an element the plain fetch lacks."""
    body = "".join(f"<p>{p}</p>" for p in pages.PARAS + pages.QUALIFIED + pages.PREFERRED)
    return ("<html><body><main><h1>Communications Director</h1>"
            f"<div class='job-description'>{body}</div></main></body></html>")


@pytest.mark.parametrize("plain_page, rendered_page, how", [
    (pages.store_page(), pages.store_page(), "text"),
    (pages.device_page(), pages.device_page(), "text"),
    (pages.script_drawn_page(), _rendered_script_drawn(), "jsonld"),
], ids=["sections-in-one-container", "articles-in-one-section", "jobposting-in-ld-json"])
async def test_a_host_posting_page_is_recorded_from_its_plain_fetch_and_the_recipe_reads_the_whole_posting(
        js, monkeypatch, plain_page, rendered_page, how):
    rec_id = await _host_asked()
    chrome(monkeypatch, {LINK: rendered_page})
    fetch = plain(monkeypatch, {LINK: plain_page})
    got = await html_record.record_by_code(OWNER, rec_id)
    assert got["result"] == bp.FINISHED and "an HTML recipe was recorded" in got["note"], got
    (live,) = await _only(SiteRecipe)
    assert live.state == "live" and live.host == HOST and live.recipe["kind"] == "html_posting"
    assert (live.recipe.get("jsonld") is True) == (how == "jsonld") and bool(live.recipe.get("text")) == (how == "text")
    recipe = skills.parse_recipe(live.recipe)
    assert isinstance(recipe, skills.HtmlPostingRecipe)
    read = await html_recipe.read_text(FakeFetcher({f"https://{HOST}/job/77": plain_page}), recipe,
                                       f"https://{HOST}/job/77")
    assert read is not None and read.text is not None and whole.why_not(read.text) is None
    assert "A day in the life" in read.text or "Why Work Here" in read.text
    (recording,) = await _only(SiteRecording)
    assert recording.state == "written" and LINK in fetch.got


async def test_an_embedded_jobposting_that_is_not_a_whole_posting_is_not_a_recipe(js, monkeypatch):
    short = pages.script_drawn_page(paragraphs=["Join us."])
    rec_id = await _host_asked()
    chrome(monkeypatch, {LINK: _rendered_script_drawn()})
    plain(monkeypatch, {LINK: short})
    got = await html_record.record_by_code(OWNER, rec_id)
    assert got["note"].startswith(html_record.NEEDS_JS) and await _only(SiteRecipe) == []
    (recording,) = await _only(SiteRecording)
    assert recording.state == "failed"


async def test_a_posting_read_item_on_an_embedded_jobposting_recipe_gives_its_text_by_code(js, monkeypatch):
    fetch = FakeFetcher({LINK: pages.script_drawn_page()})
    monkeypatch.setattr(polite, "make", lambda: fetch)
    job = browser_code.Job(item_id="i", kind=bp.READ, link=LINK, domain="example.test", title=None, named=False)
    job.recipe, job.recipe_id = _host_recipe(jsonld=True), "r"

    async def turn():
        return None

    got = await browser_code.read_by_code(job, frozenset(), turn)
    assert got is not None and got["result"] == "text" and got["via_recipe"] is True
    assert "A day in the life" in got["text"] and b"ld+json" in got["raw"]


# --- a closed posting is gone, not live text ----------------------------------------------------------------------

#: How two hosts' pages say, in their plain HTML, that a posting is closed.
CLOSED_NOTICES = ["Applications for this position are no longer being accepted. We invite you to explore other "
                  "opportunities that may match your interests.",
                  "We're sorry, the job you are trying to apply for has been filled."]


@pytest.mark.parametrize("notice", CLOSED_NOTICES)
def test_the_one_list_of_closed_words_knows_how_careers_pages_say_a_posting_is_closed(notice):
    assert closed_words.found(notice) is not None


async def _read(page: str, recipe: dict):
    parsed = skills.parse_recipe(recipe)
    assert isinstance(parsed, skills.HtmlPostingRecipe)
    return await html_recipe.read_text(FakeFetcher({LINK: page}), parsed, LINK)


@pytest.mark.parametrize("notice", CLOSED_NOTICES)
async def test_an_embedded_jobposting_on_a_page_that_says_it_is_closed_is_gone_not_text(notice):
    read = await _read(pages.script_drawn_page(closed=notice), _host_recipe(jsonld=True))
    assert read is not None and read.gone is not None and read.text is not None


async def test_an_embedded_jobposting_whose_valid_through_has_passed_is_gone_and_one_to_come_is_not():
    past = await _read(pages.script_drawn_page(valid_through="2020-01-31T00:00"), _host_recipe(jsonld=True))
    assert past is not None and past.gone and "2020" in past.gone
    future = await _read(pages.script_drawn_page(valid_through="2999-01-31"), _host_recipe(jsonld=True))
    assert future is not None and future.gone is None and future.text is not None
    assert (await _read(pages.script_drawn_page(), _host_recipe(jsonld=True))).gone is None


async def test_a_valid_through_on_the_owners_day_of_the_read_is_not_past_though_it_is_the_utc_day_before(monkeypatch):
    """8 pm on 5 November in New York is 1 am on the 6th in UTC: a posting valid through the 5th still takes
    applications that evening, as Go's own check (`go_checks._past` with `owner_today`) says."""
    pin_clock(monkeypatch, datetime(2026, 11, 6, 1, 0, tzinfo=UTC), "America/New_York")
    through = await _read(pages.script_drawn_page(valid_through="2026-11-05"), _host_recipe(jsonld=True))
    assert through is not None and through.gone is None and through.text is not None
    ended = await _read(pages.script_drawn_page(valid_through="2026-11-04"), _host_recipe(jsonld=True))
    assert ended is not None and ended.gone and "04 November 2026" in ended.gone


async def test_a_container_recipe_reads_a_closed_notice_beside_the_posting_as_gone_but_not_the_postings_own_words():
    recipe = _host_recipe(text="#job-detail-body div.content")
    beside = pages.store_page().replace('<p class="meta">', '<p class="notice">This position has been filled.</p>'
                                        '<p class="meta">')
    read = await _read(beside, recipe)
    assert read is not None and read.gone and "has been filled" in read.gone
    inside = pages.store_page().replace("<h2>Description</h2><p>", "<h2>Description</h2><p>The previous position has "
                                        "been filled by a colleague who moved on. ")
    read = await _read(inside, recipe)
    assert read is not None and read.gone is None and "has been filled by a colleague" in (read.text or "")
    assert (await _read(pages.store_page(), recipe)).gone is None


async def test_a_read_item_on_a_closed_posting_gets_the_gone_result_the_other_readers_give(js, monkeypatch):
    fetch = FakeFetcher({LINK: pages.script_drawn_page(closed=CLOSED_NOTICES[1])})
    monkeypatch.setattr(polite, "make", lambda: fetch)
    job = browser_code.Job(item_id="i", kind=bp.READ, link=LINK, domain="example.test", title=None, named=False)
    job.recipe, job.recipe_id = _host_recipe(jsonld=True), "r"

    async def turn():
        return None

    got = await browser_code.read_by_code(job, frozenset(), turn)
    assert got is not None and got["result"] == bp.GONE and got["url"] == LINK and got["via_recipe"] is True
    assert "text" not in got


async def test_a_recording_on_a_page_that_is_closed_still_keeps_the_recipe_the_structure_is_what_is_tested(
        js, monkeypatch):
    rec_id = await _host_asked()
    closed = pages.script_drawn_page(closed=CLOSED_NOTICES[0])
    chrome(monkeypatch, {LINK: closed})
    plain(monkeypatch, {LINK: closed})
    got = await html_record.record_by_code(OWNER, rec_id)
    assert "an HTML recipe was recorded" in got["note"]
    (live,) = await _only(SiteRecipe)
    assert live.recipe["jsonld"] is True
