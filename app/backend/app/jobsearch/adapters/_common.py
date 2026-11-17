"""What the readers share: the plain shape every system is read into, the error a system raises, the
helpers that turn its answers into that shape, and the base class of a reader. One module for each job system
sits beside this one; `adapters/__init__.py` lists them."""

import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import httpx

from app.jobsearch.adapters._address import Address, System
from app.jobsearch.adapters._ask import Ask, Asked
from app.jobsearch.fetch.polite import Fetcher, Refused, timeup
from app.jobsearch.fetch.polite import (
    RobotsRefused as RobotsRefused,  # the hooks catch it; they import nothing of polite
)
from app.jobsearch.measures.pay_place import is_part_time, parse_comp_text, says_no_place
from app.jobsearch.postings import dates as posted_dates
from app.web_address import http_url

POSTING_KEYS = ("ext_id", "title", "location", "work_mode", "comp", "comp_min", "comp_max", "posted", "url",
                "apply_url", "text", "department")


class AdapterError(Exception):
    """A job system answered with an error, or with something that is no longer the shape we read. `status`: the HTTP
    status it answered with, when it answered one (`postings.fault_kind` classes a failed read by it)."""

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


#: What a read says when it stopped before a board's last page because the time it was given ran out (`polite.timeup`:
#: the night's read, or a scout's quick try).
TIME_RAN_OUT = "the time allowed for the read ran out"
#: And when it stopped because the pages were going round in circles (`Pages`).
PAGE_REPEATED = "a page repeated one already read"
ENOUGH_READ = "enough for a test of the recipe"


def stopped_at(read: int, why: str, total: int | None = None) -> str:
    """What to keep on the employer when a read stopped before the board's last page: "stopped at 100 of 3305 postings
    (a page repeated one already read)"."""
    if total:
        return f"stopped at {read} of {total} postings ({why})"
    return f"stopped at {read} {'posting' if read == 1 else 'postings'} ({why})"


@dataclass
class Listing:
    """Every open posting found on one board. `truncated` is True when the read stopped before the board's last page
    (`Pages`: a page that brought nothing new, or the night's time ran out), so it closes nothing."""

    postings: list[dict]
    truncated: bool = False
    note: str | None = None
    #: What the read learned about the board's address, to keep on the employer (the tenant name its own page gives).
    board: dict | None = None
    #: What the read asked the board for, when it asked for less than the whole board (docs/PRD.md 39.5): the postings
    #: it could have returned, and whether it asked only for what is new. None: the whole board, so every posting it
    #: did not return is gone.
    asked: Asked | None = None


class Pages:
    """A board read page by page to its last page: there is no page cap (docs/PRD.md 25.12, a board of thousands is read
    whole, a page at a time at the polite pace). A read stops early only when it must. Either it is going round in
    circles (`runaway`): a page brings no posting it has not already read, as a board does that ignores its offset or
    answers the same page again and again. Or the time it was given has run out (`timeup`, docs/PRD.md 28.17:
    `polite.timeup` of the fetcher it reads with). It then says so and marks its listing truncated (`cut`), so nothing
    missing from it is taken as closed. A page with no postings at all is the board's end, which each reader checks
    itself."""

    def __init__(self, fetch: Fetcher | None = None) -> None:
        self.seen: set[str] = set()
        self.runaway = False
        self.timeup = False
        self._fetch = fetch

    def fresh(self, page: list[dict]) -> list[dict]:
        """The postings of this page not read before, in order. A page that has postings and brings none of them is a
        runaway: `runaway` is set, and the reader stops after this page."""
        new = []
        for posting in page:
            ext_id = posting.get("ext_id")
            if ext_id and ext_id not in self.seen:
                self.seen.add(ext_id)
                new.append(posting)
        if page and not new:
            self.runaway = True
        return new

    def stop(self) -> bool:
        """Whether to stop here, before the board's last page: the pages are going round in circles, or the night's read
        has run out of time. A reader asks it only after its own end tests (no more rows, every posting counted), so a
        board that ended on its own is never marked cut short."""
        if not self.runaway and self._fetch is not None and timeup(self._fetch):
            self.timeup = True
        return self.runaway or self.timeup

    @property
    def cut(self) -> bool:
        """Whether the listing is not the whole board (`Listing.truncated`). A repeated page counts even when the board
        said it was at its end: the postings the repeat stood in for were never read."""
        return self.runaway or self.timeup

    def note(self, read: int, total: int | None = None) -> str | None:
        """What to keep on the employer when the read stopped early (None when it read the whole board)."""
        if not self.cut:
            return None
        return stopped_at(read, PAGE_REPEATED if self.runaway else TIME_RAN_OUT, total)


# --- small helpers ---------------------------------------------------------------------------------------------------

#: The most markup `html_to_text` reads; a description is far shorter, and the rest is dropped.
HTML_TEXT_MAX = 200_000

# A tag's attributes end at the first `>` or at the next `<`: a `[^>]*` that ran on to a `>` far away made every
# unclosed `<` on a page cost the whole page, and the time grew with the square of its length.
_BREAK = re.compile(
    r"<br\s*/?>|</(?:p|div|li|ul|ol|h[1-6]|tr|table|section)\s*>|<(?:p|div|ul|ol|h[1-6])(?:\s[^<>]*)?>", re.IGNORECASE)

_ITEM = re.compile(r"<li(?:\s[^<>]*)?>", re.IGNORECASE)

_TAG = re.compile(r"</?[A-Za-z][^<>]*>")

_CUT_OPEN = re.compile(r"<(script|style)\b", re.IGNORECASE)

_CUT_CLOSE = {"script": re.compile(r"</script\s*>", re.IGNORECASE), "style": re.compile(r"</style\s*>", re.IGNORECASE)}


def _cut_scripts(s: str) -> str:
    """`s` without its `<script>` and `<style>` elements. A name with no closing tag left in `s` is looked for once, not
    again for each opening tag after it, so the time is the length of `s`."""
    out: list[str] = []
    at = 0
    unclosed: set[str] = set()
    for opened in _CUT_OPEN.finditer(s):
        name = opened.group(1).lower()
        if opened.start() < at or name in unclosed:  # inside an element already cut, or one that never closes
            continue
        closed = _CUT_CLOSE[name].search(s, opened.end())
        if closed is None:
            unclosed.add(name)
            continue
        out.append(s[at:opened.start()])
        at = closed.end()
    out.append(s[at:])
    return "".join(out)


def html_to_text(s: str | None) -> str:
    """A description as plain text: entities unescaped until they stop changing (a feed that escapes its HTML twice),
    a line for each block of the markup, "- " for list items, every other tag dropped, blank lines removed."""
    if not s:
        return ""
    s = s[:HTML_TEXT_MAX]  # the patterns below can take time squared on markup built to make them
    for _ in range(3):
        again = html.unescape(s)
        if again == s:
            break
        s = again
    s = _cut_scripts(s)
    s = _ITEM.sub("\n- ", s)
    s = _BREAK.sub("\n", s)
    s = _TAG.sub("", s).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in s.splitlines()]
    return "\n".join(line for line in lines if line)


def _join(*parts: str | None, sep: str = "\n\n") -> str | None:
    text = sep.join(p for p in parts if p)
    return text or None


def _str(v: Any) -> str | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    if isinstance(v, str):
        return v.strip() or None
    return None


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


_STAMP_START = re.compile(r"\s*\d{4}-\d{2}-\d{2}")


def _date(v: Any) -> str | None:
    """YYYY-MM-DD from an ISO date or timestamp, or from milliseconds or seconds since 1970; None for anything else,
    such as the display text "Posted Today". An instant (an epoch, or a time with `Z` or an offset) is the owner's day
    of it; a date-only value stays as written."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) or (isinstance(v, str) and _STAMP_START.match(v)):
        day = posted_dates.instant_day(v)
        return day.isoformat() if day is not None else None
    return None


def _loose_date(v: Any) -> str | None:
    """YYYY-MM-DD from `_date`'s forms and from the ones career pages print: "10/7/2026" and "Oct 7, 2026"."""
    found = _date(v)
    if found or not isinstance(v, str):
        return found
    if re.fullmatch(r"\s*\d{9,}(\.\d+)?\s*", v):  # an epoch printed as text (the recipe's date cell)
        return _date(float(v))
    try:
        m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\b", v)
        if m:
            year = int(m.group(3))
            return date(year + 2000 if year < 100 else year, int(m.group(1)), int(m.group(2))).isoformat()
        m = re.match(r"\s*([A-Za-z]{3})[A-Za-z]*\.? (\d{1,2}),? (\d{4})\b", v)
        if m:
            return datetime.strptime(f"{m.group(1).title()} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
    except ValueError:
        return None
    return None


def _mode(raw: Any, location: str | None) -> str | None:
    """remote | hybrid | onsite from a system's own workplace field, else remote when the location text says so."""
    text = str(raw or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    if "hybrid" in text or "flex" in text:
        return "hybrid"
    if "remote" in text:
        return "remote"
    if "onsite" in text or "office" in text or "inperson" in text:
        return "onsite"
    if location and re.search(r"\bremote\b", location, re.IGNORECASE):
        return "remote"
    return None


def _yearly(interval: Any) -> bool:
    text = str(interval or "").lower()
    return "year" in text or "annual" in text


def _money(n: int, currency: str | None) -> str:
    return f"${n:,}" if currency in (None, "", "USD") else f"{n:,} {currency}"


def _comp(
    lo: Any, hi: Any, currency: str | None, interval: Any, *, part_time: bool = False,
) -> tuple[str | None, int | None, int | None]:
    """(as written, min, max) for a pay range. The numbers are only filled for a yearly or hourly range in dollars (an
    hourly one as the year `parse_comp_text` reads it, unless `part_time`: a fractional or part-time role's hour is no
    full-time year, so its hourly range has no yearly numbers, docs/PRD.md 30.4)."""
    lo, hi = _int(lo), _int(hi)
    if lo is None and hi is None:
        return None, None, None
    cur = (currency or "").upper() or None
    per = "a year" if _yearly(interval) else (str(interval or "").replace("per-", "").replace("-", " ").strip() or None)
    low, high = lo or hi, hi or lo
    text = _money(low, cur) if low == high else f"{_money(low, cur)} - {_money(high, cur)}"
    if per:
        text += f" {per}" if per.startswith("a ") else f" per {per}"
    if _yearly(interval) and cur in (None, "USD"):
        return text, low, high
    if "hour" in str(interval or "").lower() and cur in (None, "USD"):
        year_low, year_high = parse_comp_text(text, part_time=part_time)  # hourly: a 2,080-hour year
        return text, year_low, year_high
    return text, None, None


def parse_comp(text: str | None) -> tuple[int | None, int | None]:
    """A yearly dollar range from pay written out in free text ("$80,000 - $100,000 per year", "$90k to $110k"), else
    (None, None). An hourly figure is a 2,080-hour year; the reading itself is `gates.parse_comp_text`, the one comp
    parser."""
    return parse_comp_text(text) if text else (None, None)


class Posting(dict):
    """A posting as a reader hands it on: a plain dict with every key of `POSTING_KEYS`, and, in `raw`, the item the
    system answered it from, exactly as it came (None when the reader has none to give). It is equal to the dict
    alone, so nothing that compares or stores postings sees the difference; `raw_copies.py` keeps the item."""

    raw: Any = None


class Detailed(dict):
    """What a posting's own page gave (`Adapter.detail`), as the fields to update, with the replies it was made from
    (`replies`, put there by `postings.read_detail`) or, when the answer is a part of a larger one, the item itself
    (`raw`). `hold.hold` keeps them."""

    replies: tuple = ()
    raw: Any = None


def _posting(*, raw: Any = None, **given: Any) -> Posting:
    """A posting with every key present: None for what the system did not say. `raw` is the item it was read from."""
    unknown = set(given) - set(POSTING_KEYS)
    if unknown:
        raise ValueError(f"not a posting field: {sorted(unknown)}")
    out = Posting({key: given.get(key) for key in POSTING_KEYS})
    for key in ("url", "apply_url"):  # a job system's address is shown as a link: only a web address is kept
        out[key] = http_url(out[key])
    out.raw = raw
    return out


def _answer(reply: Any, who: str) -> Any:
    if reply.status_code != 200:
        raise AdapterError(f"{who} answered {reply.status_code}", reply.status_code)
    try:
        return reply.json()
    except ValueError:
        raise AdapterError(f"{who} changed shape") from None


def _changed(who: str) -> AdapterError:
    return AdapterError(f"{who} changed shape")


def _rows(found: Any, who: str, key: str | None = None) -> list[dict]:
    """The list of postings out of an answer. A list that holds nothing usable is a changed shape."""
    rows = found.get(key) if key and isinstance(found, dict) else found
    if not isinstance(rows, list):
        raise _changed(who)
    good = [r for r in rows if isinstance(r, dict)]
    if rows and not good:
        raise _changed(who)
    return good


def _kept(postings: list[dict], seen: int, who: str) -> list[dict]:
    """Drops postings with no id or title; if rows were read and none was usable, the shape has changed."""
    keep = [p for p in postings if p["ext_id"] and p["title"]]
    if seen and not keep:
        raise _changed(who)
    return keep


def _slug(board: dict) -> str:
    slug = board.get("slug")
    if not slug or not isinstance(slug, str):
        raise AdapterError("this employer's board has no address")
    return quote(slug, safe="")


def _host(board: dict, suffix: str) -> str:
    """The host a board is read from; only a plain host name under the system's own domain is ever fetched."""
    host = board.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) \
            or not host.lower().endswith(suffix):
        raise AdapterError("this employer's board has no address")
    return host.lower()


def _part(board: dict, key: str, pattern: str = r"[A-Za-z0-9_.-]+") -> str:
    """One addressing part of a board (a site number, a section), checked to be a plain word."""
    value = board.get(key)
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise AdapterError("this employer's board has no address")
    return value


def _page(reply: Any, who: str) -> str:
    if reply.status_code != 200:
        raise AdapterError(f"{who} answered {reply.status_code}", reply.status_code)
    return reply.text


def _text(fragment: str | None) -> str | None:
    """A fragment of markup as one line of plain text."""
    return " ".join(html_to_text(fragment).split()) or None


def _jsonld(page: str) -> dict | None:
    """The schema.org JobPosting a page carries in a `ld+json` script, or None."""
    for m in re.finditer(r"<script[^>]*ld\+json[^>]*>(.*?)</script>", page, re.IGNORECASE | re.DOTALL):
        try:
            data = json.loads(m.group(1), strict=False)
        except ValueError:
            continue
        items = data.get("@graph") if isinstance(data, dict) and isinstance(data.get("@graph"), list) else data
        for item in items if isinstance(items, list) else [items]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def job_posting(page: str | None) -> dict | None:
    """The schema.org JobPosting a page carries, as its data (`validThrough` and the rest), or None."""
    return _jsonld(page) if page else None


def _place_of(item: Any) -> str | None:
    """One schema.org Place as "City, Region, Country": its address's locality, region and country (the country a code
    or a name). A place that is only text is that text; one with no address fields is its name."""
    if isinstance(item, str):
        return _str(item)
    if not isinstance(item, dict):
        return None
    address = item.get("address", item)  # a page may put the address fields on the place itself
    if isinstance(address, str):
        return _str(address)
    if not isinstance(address, dict):
        return _str(item.get("name"))
    country = address.get("addressCountry")
    country = _str(country.get("name")) if isinstance(country, dict) else _str(country)
    parts = (_str(address.get("addressLocality")), _str(address.get("addressRegion")), country)
    return ", ".join(p for p in parts if p) or _str(item.get("name"))


def _places_of(ld: dict) -> str | None:
    """The places of a JobPosting's `jobLocation` (one place, or a list of them), each once, joined with "; "."""
    found = ld.get("jobLocation")
    named = (_place_of(item) for item in (found if isinstance(found, list) else [found]))
    return "; ".join(dict.fromkeys(p for p in named if p)) or None


def page_text(page: str | None) -> str | None:
    """The posting a career page carries, whole: the description of the schema.org JobPosting the page embeds when it
    has one, else the page's main content (`main_text`: the posting's body, never the page's menus, header, footer,
    language list or markup). "Whole and verbatim" is the whole posting, from its title down with its header lines,
    never the page's chrome (the owner, 2026-10-17).
    None when the page has no text, or is longer than `html_to_text` reads (`HTML_TEXT_MAX`, once its scripts and
    styles are out): a page that long is not read, and never read in part, since a posting cut off is worse than none
    (docs/PRD.md 39.29)."""
    if not page:
        return None
    ld = _jsonld(page)
    described = _from_jsonld(ld).get("text") if ld else None
    bare = _cut_scripts(page)
    if described:
        # The JobPosting gives only the description: the page's own header block (the title down to where the
        # description starts: place, full or part time, "Posted 3 days ago", requisition id, pay) goes in front of it,
        # so the metadata is kept (the owner, 2026-10-17).
        header = posting_header(bare, described) if len(bare) <= HTML_TEXT_MAX else None
        return f"{header}\n\n{described}" if header else described
    if len(bare) > HTML_TEXT_MAX:
        return None
    return main_text(bare)


# --- a page's main content -------------------------------------------------------------------------------------------
#
# A career page with no embedded description is read for its main content only. The region is the page's `main` (or
# `role="main"`), else its one `article`, else its body, read from the posting's title (its first `h1`) down, so the
# header lines under the title (place, full or part time, "Posted 3 days ago", requisition id, pay) are kept with the
# description. Inside it every element that is the page's chrome is dropped: navigation, menus, the site's header and
# footer, asides, forms, buttons, lists to pick from, dialogs, log-in links, and the parts a site names for sharing,
# similar jobs, cookies or breadcrumbs.

#: Elements that are never a posting's words: their whole content is dropped.
_CHROME_TAGS = frozenset({"nav", "aside", "form", "button", "select", "option", "dialog", "noscript", "template",
                          "svg", "iframe", "script", "style", "head", "title", "menu", "textarea", "label"})
#: Header and footer are the site's own only outside the posting: inside an `article` or `main` they hold its title.
_SITE_TAGS = frozenset({"header", "footer"})
#: ARIA roles that mark chrome.
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo", "search", "menu", "menubar", "menuitem", "dialog",
                           "alertdialog", "toolbar", "tablist", "complementary", "listbox"})
#: Words of an element's class or id that mark chrome, matched as whole words of it ("article--share" is "share").
#: Only words no container of a posting is named by: a layout's "sidebar" or "menu" class can sit on the element that
#: holds the posting, so those are left to the tags and roles above.
_CHROME_WORDS = frozenset({"share", "sharing", "social", "similar", "related", "recommended", "breadcrumb",
                           "breadcrumbs", "cookie", "cookies", "consent", "skip", "newsletter"})
#: Tags with no end tag: they never open a level.
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track",
                   "wbr", "param"})
#: Tags that break a line (a list item starts its line with "- ").
_BLOCKS = frozenset({"p", "div", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section",
                     "article", "main", "header", "footer", "dl", "dt", "dd", "blockquote", "pre"})
#: A line of the page's own furniture, never part of a posting.
_FURNITURE = re.compile(r"^(?:-\s*)?(?:skip to (?:main )?content|skip navigation|back to top|go back|log ?in|sign ?in|"
                        r"log ?out|sign ?out|-|\.\.\.)$", re.I)


def _chrome(tag: str, attrs: dict[str, str], in_posting: bool) -> bool:
    """Whether an element is the page's chrome, never the posting's words."""
    if tag in _CHROME_TAGS or (tag in _SITE_TAGS and not in_posting):
        return True
    if (attrs.get("role") or "").lower() in _CHROME_ROLES or attrs.get("aria-hidden") == "true" or "hidden" in attrs:
        return True
    named = f"{attrs.get('class') or ''} {attrs.get('id') or ''}"
    named = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", named)  # "widgetSimilarJobs" is "widget similar jobs"
    words = set(re.split(r"[^a-z0-9]+", named.lower()))
    return bool(words & _CHROME_WORDS)


class _Content(HTMLParser):
    """Collects the text of the regions a page may hold its posting in, chrome left out. No markup is ever kept: a
    declaration (`<!DOCTYPE html>`), a comment and an attribute are not text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, str | None]] = []  # (tag, dropped here, region it opens)
        self.dropped = 0  # open elements that are chrome
        self.regions: dict[str, list[str]] = {"main": [], "article": [], "body": []}
        self.open_regions: list[str] = []
        self.articles = 0
        #: Where the posting's title (the first `h1` outside chrome) starts in each region's parts, and its words.
        self.title_at: dict[str, int] = {}
        self.title: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs) -> None:
        tag = tag.lower()
        if tag in _VOID:
            if tag in ("br", "hr"):
                self._out("\n")
            return
        given = {k.lower(): (v or "") for k, v in attrs}
        drop = not self.dropped and _chrome(tag, given, bool(self.open_regions))
        region = None
        if not self.dropped and not drop:
            if tag == "main" or given.get("role", "").lower() == "main":
                region = "main"
            elif tag == "article":
                region = "article"
                self.articles += 1
        self.stack.append((tag, drop, region))
        if drop:
            self.dropped += 1
            return
        if region:
            self.open_regions.append(region)
        if tag == "h1" and "body" not in self.title_at:
            for name in {"body", *self.open_regions}:
                self.title_at.setdefault(name, len(self.regions[name]))
            self._in_title = True
        if tag in _BLOCKS:
            self._out("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag) -> None:
        tag = tag.lower()
        if tag in _VOID or all(name != tag for name, _, _ in self.stack):
            return
        while self.stack:
            name, drop, region = self.stack.pop()
            if name == "h1":
                self._in_title = False
            if drop:
                self.dropped -= 1
            elif region and self.open_regions:
                self.open_regions.pop()
            if not drop and name in _BLOCKS:
                self._out("\n")
            if name == tag:
                return

    def handle_data(self, data) -> None:
        self._out(data)

    def _out(self, text: str) -> None:
        if self.dropped:
            return
        if self._in_title:
            self.title.append(text)
        self.regions["body"].append(text)
        for region in set(self.open_regions):
            self.regions[region].append(text)


def _lines(parts: list[str]) -> str:
    text = "".join(parts).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line and not _FURNITURE.match(line))


def main_text(page: str | None) -> str | None:
    """The text of a page's main content, as lines, with its chrome and every bit of markup left out: the page's
    `main` (or `role="main"`), else its one `article`, else its body. None when it has no text."""
    if not page:
        return None
    parser = _Content()
    try:
        parser.feed(_cut_scripts(page)[:HTML_TEXT_MAX])
        parser.close()
    except (AssertionError, ValueError):  # markup the parser gives up on: no text rather than a part of the page
        return None
    for region in ("main", "article", "body"):
        if region == "article" and parser.articles != 1:
            continue
        if text := _lines(_from_title(parser, region)):
            return text
    return None


def _from_title(parser: "_Content", region: str) -> list[str]:
    """A region's parts from the posting's title down. A region that does not hold the title (the page's `h1` sits
    outside its `main`) is given the title in front, so the posting always starts at its title."""
    parts = parser.regions[region]
    if region in parser.title_at:
        return parts[parser.title_at[region]:]
    if parser.title and parts:
        return [*parser.title, "\n", *parts]
    return parts


#: A header line is short; the description's paragraphs are not.
_HEADER_LINE_MAX = 140
_HEADER_LINES_MAX = 25


def posting_header(page: str | None, description: str | None) -> str | None:
    """The header block of a page that also embeds a schema.org JobPosting, which gives only the description: the page's
    own words from the posting's title down to where that description starts (place, full or part time, contract or
    permanent, "Posted 3 days ago", requisition id, pay), chrome left out. None when the page shows no title, or the
    description starts straight under it."""
    if not page:
        return None
    parser = _Content()
    try:
        parser.feed(_cut_scripts(page)[:HTML_TEXT_MAX])
        parser.close()
    except (AssertionError, ValueError):
        return None
    if "body" not in parser.title_at:
        return None
    lines = _lines(parser.regions["body"][parser.title_at["body"]:]).splitlines()
    first = next((" ".join(x.lower().split()) for x in (description or "").splitlines() if x.strip()), "")
    head: list[str] = []
    for line in lines[:_HEADER_LINES_MAX + 1]:
        norm = " ".join(line.lower().split())
        if len(line) > _HEADER_LINE_MAX or (first and head and norm[:60] == first[:60]):
            break
        head.append(line)
    return "\n".join(head[:_HEADER_LINES_MAX]) if len(head) > 1 else None


def _ext_id(posting: dict, who: str) -> str:
    """The system's own id of a stored posting, as a plain word that is safe to put in an address."""
    value = posting.get("ext_id")
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", str(value)):
        raise AdapterError(f"{who} posting has no id")
    return str(value)


def _from_jsonld(ld: dict, posting: dict | None = None) -> dict:
    """The posting fields a JobPosting carries: the description, the date, the pay, remote work, and where the job is
    (`jobLocation`). The places are given only for a posting whose list left its place out (or gave only a count of
    places): one that has a place keeps it. Without `posting`, they are always given."""
    pay = ld.get("baseSalary") if isinstance(ld.get("baseSalary"), dict) else {}
    value = pay.get("value") if isinstance(pay.get("value"), dict) else {}
    part_time = is_part_time(str(ld.get("title") or ""), str(ld.get("employmentType") or ""))
    comp = _comp(value.get("minValue"), value.get("maxValue"), pay.get("currency"), value.get("unitText"),
                 part_time=part_time)
    update = {"text": html_to_text(ld.get("description") if isinstance(ld.get("description"), str) else None) or None,
              "posted": _date(ld.get("datePosted")), "comp": comp[0], "comp_min": comp[1], "comp_max": comp[2],
              "work_mode": "remote" if ld.get("jobLocationType") == "TELECOMMUTE" else None}
    if posting is None or says_no_place(posting.get("location")):
        update["location"] = _places_of(ld)
    return {k: v for k, v in update.items() if v}


def page_facts(page: str | None) -> dict:
    """What the schema.org JobPosting a page embeds says of the job: `title`, `company` (its `hiringOrganization`), and
    the fields `_from_jsonld` gives (`text`, `posted`, pay, `work_mode`, `location`). Only what the page states: a page
    with no JobPosting gives {} and nothing is guessed from its markup."""
    ld = _jsonld(page) if page else None
    if not ld:
        return {}
    org = ld.get("hiringOrganization")
    company = _str(org.get("name")) if isinstance(org, dict) else _str(org)
    named = {"title": _str(ld.get("title")), "company": company}
    return {k: v for k, v in {**named, **_from_jsonld(ld)}.items() if v}


# --- the readers -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Probed:
    """What an open feed said when asked whether a board by a name exists (`Adapter.probe`): that it does, and the name
    of the employer it gave when its feed gives one."""

    name: str | None = None


async def _json_or_none(fetch: Fetcher, url: str) -> Any:
    """What a feed answers at `url` as JSON, or None for a refusal, a failure, any status but 200 or a reply that is not
    JSON: a probe asks, and an answer that is no board is no news."""
    try:
        reply = await fetch.get(url)
    except (Refused, httpx.HTTPError):
        return None
    if reply.status_code != 200:
        return None
    try:
        return reply.json()
    except ValueError:
        return None


class Adapter(System):
    """Reads one job system (a `System`: it also says which addresses are the system's, and which are its boards and its
    postings). `read` lists a board; `detail` fills in a posting the list left incomplete.

    `places`: True when `detail` gives the places of a posting whose list left them out or gave only their count
    ("26 Locations"). The filter then reads that page before it judges the place (`sift.py`); for a system whose list
    names the places, an unknown place goes on to a reader, who reports where the work is.

    `filters`: True when the system lets a reader ask for less than the whole board (docs/PRD.md 39.5). The night's read
    then calls `read_asked`, and `read` stays the whole board (the CLI, a site's adapter recipe)."""

    how = "feed"
    #: What asking the system's open feed whether a board by a name exists tells (`probe`): True when it names the
    #: employer, so the answer is taken only when the name is the employer's; False when it never does, so the answer
    #: stands unconfirmed; None when the system has no feed to ask.
    probe_names: bool | None = None
    places = False
    filters = False
    #: True when the raw copy of a detail read is the list item itself (the system has no answer for one posting), so
    #: it is made again with `parse_item`, not by running `detail` on a reply.
    detail_is_item = False

    async def read(self, fetch: Fetcher, board: dict) -> Listing:
        raise NotImplementedError

    def parse_item(self, r: Any, board: dict) -> Posting | None:
        """One list item (the raw copy `Posting.raw`) as the plain posting this system makes of it, or None when the
        system leaves it out. `board` is the employer's board address. Pure: it fetches nothing, so a posting can be
        made again from its raw copy (`reparse.py`)."""
        raise NotImplementedError

    async def read_asked(self, fetch: Fetcher, board: dict, ask: Ask) -> Listing:
        """The board, asked for what `ask` wants where this system can be asked, and `Listing.asked` saying what it
        asked. A system with no filters reads the whole board."""
        return await self.read(fetch, board)

    def posting(self, a: Address, board: dict) -> tuple[str, str] | None:
        """The one posting a link on `board` opens, when this reader can read one alone (so it overrides `detail`): its
        id and the address to read it from. None for any other link. A reader that answers here is the only thing that
        names a posting: a system with no reader has none."""
        return None

    async def detail(self, fetch: Fetcher, board: dict, posting: dict) -> dict:
        return {}

    async def probe(self, fetch: Fetcher, slug: str) -> Probed | None:
        """Ask the system's open feed whether a board named `slug` has any postings: `Probed` (with the employer's name
        when the feed gives one) or None when there is no such board, or the feed refused, failed or answered with
        anything else. Asked only of a system with `probe_names` set."""
        return None
