"""The small CSS selector engine an HTML recipe runs on a page fetched without a browser (docs/PRD.md path 49.2): a
closed grammar of what a recorder writes, never script. Every page here is made up."""

import pytest

from app.sites import html_select as hs

PAGE = """<html><head><title>Open roles</title><style>.x { color: red }</style></head><body>
<nav><a href="/about">About</a></nav>
<ul class="results">
  <li class="row first"><a class="job" href="/job/1">First role</a><span class="loc">Springdale</span>
  <li class="row"><a class="job" href="/job/2">Second role</a><span class="loc">Riverton</span>
  <li class="row"><a class="job" href="/job/3" data-kind="Open Role">Third<br>role</a>
</ul>
<div id="desc"><p>Line one<p>Line two <b>bold</b></p><script>var hidden = 1;</script></div>
<a rel="next" href="/page/2">Next</a>
</body></html>"""


@pytest.fixture
def root():
    return hs.parse(PAGE)


def test_unclosed_list_items_and_paragraphs_are_siblings_not_nested(root):
    rows = hs.select(root, "li.row")
    assert [r.parent.tag for r in rows] == ["ul", "ul", "ul"]
    assert [p.text() for p in hs.select(root, "#desc p")] == ["Line one", "Line two bold"]


@pytest.mark.parametrize("selector, want", [
    ("a.job", ["First role", "Second role", "Third\nrole"]),
    ("ul.results > li > a", ["First role", "Second role", "Third\nrole"]),
    ("ul.results a.job", ["First role", "Second role", "Third\nrole"]),
    ("li.first span.loc", ["Springdale"]),
    ('a[href="/job/2"]', ["Second role"]),
    ('a[href$="/3"]', ["Third\nrole"]),
    ('a[href^="/job/"][class=job]', ["First role", "Second role", "Third\nrole"]),
    ('a[href*="b/1"]', ["First role"]),
    ('a[data-kind*="open role" i]', ["Third\nrole"]),
    ("li:nth-of-type(2) a", ["Second role"]),
    ("span.loc, nav a", ["About", "Springdale", "Riverton"]),
    ("[rel=next]", ["Next"]),
    ("#desc", ["Line one\nLine two bold"]),
])
def test_a_selector_finds_what_a_browsers_would(root, selector, want):
    assert [n.text() for n in hs.select(root, selector)] == want


def test_text_leaves_out_script_and_style_and_breaks_at_blocks(root):
    assert "hidden" not in hs.select_one(root, "#desc").text()
    assert hs.select_one(root, "title").text() == "Open roles"
    assert "color" not in root.text()


def test_the_closest_ancestor_is_found_from_the_element_up_and_including_it(root):
    link = hs.select_one(root, "li.first a")
    assert hs.closest(link, "li.row").get("class") == "row first"
    assert hs.closest(link, "a.job") is link
    assert hs.closest(link, "table") is None


def test_an_attribute_is_read_as_written(root):
    assert hs.select_one(root, "li.first a").get("href") == "/job/1"
    assert hs.select_one(root, "li.first a").get("nothing") is None


@pytest.mark.parametrize("selector", [
    "", "a{color:red}", "a:not(.x)", "a + b", "a ~ b", "div</script>", "a[href=javascript:alert(1)", "a:hover",
    "*", "a::before", "a[onclick=x" + "y" * 300 + "]",
])
def test_a_selector_outside_the_closed_grammar_is_refused(selector):
    with pytest.raises(ValueError):
        hs.check(selector)


def test_a_page_that_is_not_html_at_all_parses_to_an_empty_tree():
    assert hs.select(hs.parse("\x00\x01 not a page"), "a") == []
    assert hs.select(hs.parse(""), "a") == []


def test_a_very_deep_page_does_not_overflow_the_stack():
    deep = "<div>" * 5000 + "<a href='/x'>x</a>" + "</div>" * 5000
    assert len(hs.select(hs.parse(deep), "a")) == 1
