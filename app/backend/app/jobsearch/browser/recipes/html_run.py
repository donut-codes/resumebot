"""HTML recipes, running (docs/PRD.md path 49): a site whose postings are in the page's own HTML is read by selectors
recorded once from the rendered page, run on the page the app's own polite fetcher gets, with no browser at all
(`sites/skills.HtmlRecipe` for a site, `HtmlPostingRecipe` for one employer host's posting pages).

**Reading a list** (`run`, called by `skills.run_recipe`). The start page is fetched (robots.txt asked first by the
fetcher, about a request a second to a host, the honest user agent, no cookie), the recipe's `links`
anchors are read with their rows' `location` and `date`, and the `next` anchor is followed while it gives a public
address on the careers host that is not read yet. A refusal (401, 403, 429), a bot check (a 202 document, or a short
page that says so) and robots.txt disallowing a page are stops (`SkillError` with the status that cools the site, or
`Refused`), never retried or worked around. Nothing is typed or clicked. A link is kept only by the rules every page
recipe keeps (`page_recipe._kept`: a public address on the careers host, no sign-in or application, posting-shaped). The
walk ends on a page that brings nothing new or when the night's time runs out; both are said and the listing is marked
truncated, so it closes nothing.

**Reading a posting** (`read_text`). The posting's page is fetched the same way and the recipe's `text` element
read; the page's HTML is kept as the raw copy. A page the recipe does not cover (off its careers host, or a host
recipe whose `url_pattern` does not match it whole) is not fetched. Whether the text is a whole posting is the
caller's to judge (`whole.why_not`). A host recipe that is `jsonld` reads no element: the text is the
description of the schema.org JobPosting the page embeds (`jsonld_text`), for a page whose HTML holds no text of its
own."""

import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from app.jobsearch.adapters import Listing, _loose_date, _posting, job_posting, page_facts, page_text, stopped_at
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, public_url, timeup
from app.jobsearch.postings import closed_words
from app.sites import html_select as hs
from app.sites import skills
from app.timeutil import owner_today

log = logging.getLogger(__name__)

#: A document the site answered with that is a bot check and not a page (an AWS WAF challenge answers 202 and no body),
#: and the status such a stop is given, so the site rests as it does for any refusal.
CHALLENGE_STATUSES = (202,)
BLOCK_STATUS = 403
#: The most result pages one read walks (a guard against a next link that never ends).
PAGES_AT_MOST = 400
PAGE_REPEATED = "a page repeated one already read"
TIME_RAN_OUT = "the time allowed for the read ran out"


@dataclass
class Walk:
    """What a walk of the list pages found: the rows kept (by address), the page-one count, and why it stopped early."""

    found: dict[str, dict] = field(default_factory=dict)
    first_rows: int = 0
    pages: int = 0
    stopped: str | None = None


async def fetch_page(fetch: Fetcher, url: str) -> tuple[str, bytes]:
    """A page's HTML and bytes, or a stop (`checked`). `polite.Refused` (robots.txt, a private address) is not
    caught."""
    return checked(await fetch.get(url), url)


def checked(reply, url: str) -> tuple[str, bytes]:
    """A reply's HTML and bytes, or a stop: SkillError with the status of a refusal or a bot check (403 for a check), or
    the status of any answer that is not a page."""
    host = urlsplit(url).hostname
    if reply.status_code in CHALLENGE_STATUSES:
        raise skills.SkillError(f"{host} showed a bot check (answered {reply.status_code})", BLOCK_STATUS)
    if reply.status_code != 200:
        raise skills.SkillError(f"{host} answered {reply.status_code}", reply.status_code)
    if recipe_record.looks_walled(reply.text):
        raise skills.SkillError(f"{host} showed a bot check", BLOCK_STATUS)
    return reply.text, reply.content


def _first_line(node: hs.Node) -> str:
    for source in (node.text(), node.get("aria-label") or "", node.get("title") or ""):
        for line in source.split("\n"):
            if line.strip():
                return line.strip()[:300]
    return ""


def _within(row: hs.Node | None, selector: str | None) -> hs.Node | None:
    return hs.select_one(row, selector) if row is not None and selector else None


def _said(node: hs.Node | None) -> str | None:
    """What a date or a place element says: its `datetime` attribute (a `time` element) or its words."""
    if node is None:
        return None
    return " ".join((node.get("datetime") or node.text()).split())[:200] or None


def rows_of(root: hs.Node, page_url: str, recipe: skills.HtmlRecipe) -> list[dict]:
    """The rows `recipe` reads on one page, kept by `page_recipe._kept`: {href, title, location, posted}."""
    out: list[dict] = []
    for a in hs.select(root, recipe.links):
        href = urljoin(page_url, a.get("href") or "") if a.get("href") else ""
        if not href.startswith(("http://", "https://")):
            continue
        row = hs.closest(a, recipe.row) if recipe.row else None
        place = _said(_within(row, recipe.location))
        kept = page_recipe._kept({"href": href, "title": _first_line(a), "location": place}, recipe.url)
        if kept is None:
            continue
        kept["posted"] = _loose_date(_said(_within(row, recipe.date)))
        out.append(kept)
    return out


def next_page(root: hs.Node, page_url: str, recipe: skills.HtmlRecipe, visited: set[str]) -> str | None:
    """The next page's address: the `next` anchor's own address when it is public, on the careers host and not read."""
    if not recipe.next or (anchor := hs.select_one(root, recipe.next)) is None or not anchor.get("href"):
        return None
    href = urljoin(page_url, anchor.get("href") or "").split("#", 1)[0]
    if href in visited or not public_url(href) or not page_recipe.on_careers_host(href, recipe.url):
        return None
    return href


async def walk(fetch: Fetcher, recipe: skills.HtmlRecipe, *, enough: int | None = None, pages: int | None = None,
               ) -> Walk:
    """Read the list pages from the start. `enough` stops it once that many postings are found (a recording's test needs
    no more), `pages` after that many pages."""
    got = Walk()
    visited: set[str] = set()
    url: str | None = recipe.url
    while url:
        visited.add(url)
        html, _raw = await fetch_page(fetch, url)
        got.pages += 1
        root = hs.parse(html)
        before = len(got.found)
        listed = 0
        for row in rows_of(root, url, recipe):
            listed += 1
            got.found.setdefault(row["href"], row)
        if got.pages == 1:
            got.first_rows = listed
        if enough is not None and len(got.found) >= enough:
            break
        if pages is not None and got.pages >= pages:
            break
        url = next_page(root, url, recipe, visited)
        if url is None:
            break
        if listed and len(got.found) == before:
            got.stopped = stopped_at(len(got.found), PAGE_REPEATED)
            break
        if timeup(fetch):
            got.stopped = stopped_at(len(got.found), TIME_RAN_OUT)
            break
        if got.pages >= PAGES_AT_MOST:
            got.stopped = stopped_at(len(got.found), f"{PAGES_AT_MOST} pages were read")
            break
    return got


async def run(fetch: Fetcher, recipe: skills.HtmlRecipe, **limits) -> Listing:
    """Read the site by its HTML recipe. Raises `SkillError` (a refusal, a bot check or a page that is not served) and
    `polite.Refused` (robots.txt)."""
    got = await walk(fetch, recipe, **limits)
    postings: list[dict] = [_posting(raw=row, ext_id=page_recipe.ext_id_of(row["href"]), title=row["title"],
                         location=row["location"], posted=row["posted"], url=row["href"], apply_url=row["href"])
                for row in got.found.values()]
    return Listing(postings, truncated=got.stopped is not None, note=got.stopped)


@dataclass
class HtmlRead:
    """One posting page read: the status the site answered with, the text of the recipe's element (None when the
    element is not on the page), the page's bytes, and `gone`: what the page says when it says the posting is closed
    (`closed_note`), in which case the text is not a live posting's."""

    status: int
    text: str | None
    raw: bytes
    gone: str | None = None


def covers(recipe: skills.HtmlRecipe | skills.HtmlPostingRecipe, link: str) -> bool:
    """Whether the recipe reads this posting address: a public one on the careers host of a site recipe's start page,
    or one a host recipe's host covers and its `url_pattern` matches whole."""
    if not public_url(link):
        return False
    if isinstance(recipe, skills.HtmlRecipe):
        return page_recipe.on_careers_host(link, recipe.url)
    host = urlsplit(link).hostname or ""
    return skills.host_covers(recipe.host, host) and skills.compile_pattern(
        recipe.url_pattern, "url_pattern").fullmatch(link.split("#", 1)[0]) is not None


def closed_note(link: str, html: str, root: hs.Node, inside: hs.Node | None) -> str | None:
    """What a page says when it says its posting is closed, or None: the posting's own `validThrough` that has passed
    (as Go reads it, `go_checks._past`), or a closed phrase (`closed_words.py`, the one list) in the words of the page
    outside the posting's own element `inside`, so the posting's words never end it. `root` is the page parsed, and is
    changed here."""
    from app.jobsearch.browser import go_checks  # it reads the whole job-search side

    if day := go_checks._past((job_posting(html) or {}).get("validThrough"), owner_today()):
        return f"{link}: the posting's own data says it took applications until {day:%d %B %Y}."
    if inside is not None and inside.parent is not None:
        inside.parent.children.remove(inside)
    said = closed_words.found(root.text())
    return f'{link} says "{said}".' if said else None


def jsonld_text(html: str) -> str | None:
    """The posting a page embeds as a schema.org JobPosting in a `ld+json` script, as the app reads one everywhere
    (`adapters.page_text`: its description with the markup gone, behind the page's own header lines), or None when the
    page embeds none or its JobPosting has no description. Whether it is a whole posting is the caller's to judge."""
    return page_text(html) if page_facts(html).get("text") else None


async def read_text(fetch: Fetcher, recipe: skills.HtmlRecipe | skills.HtmlPostingRecipe, link: str,
                    ) -> HtmlRead | None:
    """A posting's text by the recipe's `text` element (or, for a host recipe that is `jsonld`, the JobPosting the page
    embeds), or None when the recipe has none or does not cover the address. A page the site answered 404 or 410 to
    comes back with its status and no text (a posting that is gone); another status that is not a page is a stop
    (`fetch_page`)."""
    jsonld = isinstance(recipe, skills.HtmlPostingRecipe) and recipe.jsonld
    if not (recipe.text or jsonld) or not covers(recipe, link):
        return None
    reply = await fetch.get(link)
    if polite.gone(link, reply.status_code) is not None:
        return HtmlRead(reply.status_code, None, b"")
    html, raw = checked(reply, link)
    root = hs.parse(html)
    if jsonld:
        return HtmlRead(200, jsonld_text(html), raw, closed_note(link, html, root, None))
    assert recipe.text is not None
    node = hs.select_one(root, recipe.text)
    text = node.text() if node is not None else None
    return HtmlRead(200, text, raw, closed_note(link, html, root, node))
