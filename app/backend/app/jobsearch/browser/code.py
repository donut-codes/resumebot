"""The app's own code tries browser items first (the owner, 2026-10-16).

The owner's priorities for finding job descriptions, in order: the best data; coverage, without compromising it;
efficiency, without compromising coverage; when in doubt, capture. A text is whole and verbatim from its source, or
nothing; nothing here clicks Apply, fills in a form or gets around a block.

Before a `read` item, or a `source` item on a followed board or a board whose posting page code reads
(`takeable.code_first`), reaches a Claude session, a code-first worker of the app (`code_loop`, beside the schedule)
leases it like any worker, under the same pace per domain, and tries it:

- `read`: the job system's own answer for that posting (`index.read_link`: every system whose reader reads one),
  else the page read politely (`polite.Fetcher`: robots.txt, the app's user agent, its pace), else, when the page
  draws itself in a browser, the app's own Chrome over DevTools (`open_page`). What counts is the schema.org JobPosting
  the page embeds (`posting_facts`: its description, title and company) or, when the page embeds none, the page's main
  readable text (`adapters.page_text`), or the system's answer, and only when it is the whole posting
  (`whole.why_not`) of this role (the titles agree). A 404 or 410 is `gone`. A refusal (403, robots.txt) is
  left to a session, as before. How the page fetch went is kept for each domain (`browser_pace.note_page_read`): a
  domain whose pages hold no text in their HTML (`shell`) goes straight to the app's Chrome for a week, with no fetch
  first. The page's HTML is kept as the raw copy.
- `source` at an address no job board owns (the employer's own posting page): read the same way; when it is the whole
  posting of the role, the role is sourced at that address with the text. Any other board's listing is left to a
  session.
- `source` on a followed board or a board whose posting page code reads: the listing opened in the app's Chrome, and the
  destination of its offsite Apply read from the page (`apply_link`: the Apply anchor's address, the board's
  offsite-apply data or its redirect parameter), never clicked. A clear employer or job-system address is `found`; a
  removed listing ("no longer accepting applications") is `gone`; an Easy Apply with no offsite link and the listing's
  whole text is `easy_apply`.

Anything unclear (several addresses, a sign-in wall, no whole text, a refusal) goes to a session as before, with nothing
used (`browser_door.code_pass_on`), except the `source` item of a role in Followup needed (`code_only`, the owner,
2026-11-13): no session takes it, so a try that found nothing is a counted not found (three daily tries, or one when
the answer is certain: `final`), a try that could not look rests with no try used, and after the last the role is
closed as noise (`postings/noise.py`). On a board whose page carries it the address comes from the page data
(`apply_data`). The worker has no run and no Claude.

The sessions' trimmed page tool is here too: `page_digest` (the door's `browser.page_digest`) loads an address in the
app's browser, under the pace, and returns only what a job needs (`digest`): title, company, location, the posting's
text, the Apply link's address and the page's state (open, removed, a sign-in wall, a check)."""

import asyncio
import base64
import html as htmllib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import adapters, ats, consent
from app.jobsearch.address import is_board_link
from app.jobsearch.browser import handback as browser_handback
from app.jobsearch.browser import items as browser_door
from app.jobsearch.browser import leases as browser_leases
from app.jobsearch.browser import pace as browser_pace
from app.jobsearch.browser import reader as br
from app.jobsearch.browser import tabs as tab_register
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.fetch import polite
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.postings import closed_words, this_posting, whole
from app.jobsearch.postings import index as postings
from app.settings import settings
from app.sites import registry, skills
from app.textutil import squash

log = logging.getLogger(__name__)

#: How many code-first workers run side by side (each on a domain of its own), and how long one waits when the queue
#: has nothing for it.
CODE_WORKERS, CODE_IDLE_SECONDS = 2, 20.0
#: The most one item's code-first try may take in all; past it the item goes to a session.
CODE_SECONDS = 120.0
#: How long a page in the app's browser may take to load, and the pause after it loads for what the page draws itself.
LOAD_SECONDS, SETTLE_SECONDS = 30.0, 2.0
#: A page that is "complete" but has drawn almost no text yet (a page that fills itself in with script) is given up to
#: `FILL_SECONDS` more, asked every `FILL_EVERY`, to show `FILL_CHARS` characters before it is read. A page that never
#: shows that many waits `SETTLE_SECONDS + FILL_SECONDS` in all, so a caller's own time limit must be longer than that:
#: Go's check states the sum as `go_checks.BROWSER_READ_SECONDS`, which a test holds equal to it.
FILL_SECONDS, FILL_EVERY, FILL_CHARS = 8.0, 1.0, 400
#: The most of a page the browser hands back (its markup).
PAGE_MAX_CHARS = 8_000_000

STATE_OPEN, STATE_REMOVED, STATE_SIGN_IN, STATE_CHECK = "open", "removed", "sign_in_wall", "check"

# --- reading a page ---------------------------------------------------------------------------------------------------

#: A page that asks the visitor to sign in before it shows the job, by its address or its words.
_SIGN_IN_URL = re.compile(r"/(?:authwall|login|signin|sign-in|uas/login|checkpoint/(?:lg|rp))\b", re.I)
_SIGN_IN_WORDS = re.compile(r"\b(?:sign in|log in|join) to (?:view|see|apply)\b|\bjoin now to see\b", re.I)
#: A bot check or block page.
_CHECK = re.compile(r"verify (?:that )?you are (?:a )?human|security verification|unusual activity|are you a robot|"
                    r"checkpoint/challenge|captcha", re.I)
#: The keys a job page's own data names its offsite Apply address by (a board's `companyApplyUrl`, and the like).
_APPLY_KEYS = re.compile(
    r'"(?:companyApplyUrl|externalApplyUrl|offsiteApplyUrl|applyUrl|applyLink|apply_url)"\s*:\s*"((?:[^"\\]|\\.)*)"')
#: A board's page data (a Next.js flight payload, escaped JSON inside script text) names where THIS job's Apply leads in
#: `applyButtonData.jobApplyData.applicationDetail`: a small object of scalars, so it ends at its first `}`.
_APPLICATION_DETAIL = re.compile(r'applicationDetail(\\?)"\s*:\s*(\{[^{}]*\})')
#: The query parameters a board's redirect carries the destination in (a safety page's `?url=`).
_REDIRECT_PARAMS = ("url", "redirect", "redirectUrl", "dest", "destination", "target", "u")
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.I | re.S)
_BUTTON = re.compile(r"<(?:button|a)\b([^>]*)>(.*?)</(?:button|a)\s*>", re.I | re.S)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
_APPLY = re.compile(r"\bapply\b", re.I)
_EASY = re.compile(r"^\s*easy apply\b", re.I)
#: The elements that hold a job board's posting body (a followed board's, and job systems' usual ones).
_BODY_IDS = {"job-details", "jobdescriptiontext", "job-description", "jobdescription"}
_BODY_CLASSES = ("jobs-description__content", "show-more-less-html__markup", "description__text", "job-description")
_BODY_TESTIDS = {"jobdescriptionhtml", "job-description"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def _code_reads(found: "ats.Link") -> bool:
    """Whether code, never a session, reads this link: its job system has a reader. A system that reads one posting and
    cannot list a board (`adapters.POSTING_ONLY`) is read by code only for a link that names a posting; its
    board pages are for a session."""
    return bool(found.reader and (found.read or found.system not in adapters.POSTING_ONLY))


def _attrs(text: str) -> dict[str, str]:
    return {k.lower(): htmllib.unescape(v.strip("\"'")) for k, v in _ATTR.findall(text or "")}


def _visible(page: str) -> str:
    return adapters.html_to_text(page or "")


def posting_facts(page: str | None) -> dict:
    """What the schema.org JobPosting a page embeds says: `title`, `company`, `text` (its description as text) and the
    other fields `adapters.page_facts` gives. {} when the page embeds none; nothing is guessed from the markup."""
    return adapters.page_facts(page) if page else {}


def whole_text(text: str | None) -> str | None:
    """The text when it is a whole posting, else None (`whole.why_not`)."""
    kept = (text or "").strip()
    return kept if kept and whole.why_not(kept) is None else None


class _Section(HTMLParser):
    """The inner markup of the first element `want(tag, attrs)` picks, by counting its tags."""

    def __init__(self, want) -> None:
        super().__init__(convert_charrefs=True)
        self.want, self.depth, self.parts, self.done = want, 0, [], False

    def handle_starttag(self, tag, attrs) -> None:
        if self.done:
            return
        if self.depth:
            self.parts.append(self.get_starttag_text() or "")
            if tag not in _VOID:
                self.depth += 1
        elif tag not in _VOID and self.want(tag, {k.lower(): (v or "") for k, v in attrs}):
            self.depth = 1

    def handle_endtag(self, tag) -> None:
        if self.depth and not self.done:
            self.depth -= 1
            if self.depth == 0:
                self.done = True
            else:
                self.parts.append(f"</{tag}>")

    def handle_data(self, data) -> None:
        if self.depth and not self.done:
            self.parts.append(htmllib.escape(data, quote=False))


def _is_body(_tag: str, attrs: dict[str, str]) -> bool:
    classes = attrs.get("class", "").lower()
    return (attrs.get("id", "").lower() in _BODY_IDS or attrs.get("data-testid", "").lower() in _BODY_TESTIDS
            or any(c in classes.split() for c in _BODY_CLASSES))


def _section_text(page: str, want) -> str:
    parser = _Section(want)
    try:
        parser.feed(page or "")
        parser.close()
    except Exception:  # markup the parser cannot read gives no section
        return ""
    return adapters.html_to_text("".join(parser.parts)) if parser.parts else ""


def listing_text(page: str | None) -> str | None:
    """A listing's whole text: its embedded JobPosting's description, else the board's own description element, copied
    as the page holds it. None unless it is whole."""
    if not page:
        return None
    return whole_text(posting_facts(page).get("text")) or whole_text(_section_text(page, _is_body))


def _unwrap(url: str, hosts: frozenset[str]) -> str:
    """A board's redirect (`<board>/safety/go/?url=...`) as the address it leads to; any other as it is."""
    for _ in range(3):
        parts = urlsplit(url)
        if not is_board_link(url, hosts):
            return url
        query = parse_qs(parts.query)
        inner = next((v[0] for k in _REDIRECT_PARAMS for v in [query.get(k) or []] if v and
                      v[0].lower().startswith(("http://", "https://"))), None)
        if inner is None:
            return url
        url = inner
    return url


def _offsite(url: str, page_url: str, hosts: frozenset[str]) -> str | None:
    """The address as an offsite destination (an employer's or a job system's), or None: a board's own address, a
    relative one, or one that is not a web address."""
    url = htmllib.unescape((url or "").strip())
    if not url or url.startswith(("#", "javascript:", "mailto:")):
        return None
    url = _unwrap(urljoin(page_url or "", url), hosts)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        return None
    if is_board_link(url, hosts):
        return None
    return url


def _same(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}?{parts.query}".rstrip("?")


def _json_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw


def _own_words(page: str | None) -> str:
    """The posting's own part of the page, for the closed-words check: its main content from the title down, with the
    page's chrome and its similar-jobs blocks left out (`adapters.main_text`), never the whole page's words (another
    listing's "No longer accepting applications" in a sidebar is not this posting's). Empty when none can be found."""
    return _page_body(consent.without_banners(page or ""))


def page_state(page: str | None, page_url: str = "") -> str:
    """What the page is: removed (the job is gone), a sign-in wall, a bot check, or open. A cookie or consent banner
    is none of them: its words are left out of the reading (`consent.without_banners`; the owner, 2026-11-04)."""
    words = _visible(consent.without_banners(page))
    if _CHECK.search(page_url or "") or (len(words) < 3000 and _CHECK.search(words)):
        return STATE_CHECK
    if _SIGN_IN_URL.search(urlsplit(page_url or "").path or ""):
        return STATE_SIGN_IN
    if closed_words.found(_own_words(page)):
        return STATE_REMOVED
    if len(words) < 3000 and _SIGN_IN_WORDS.search(words):
        return STATE_SIGN_IN
    return STATE_OPEN


def apply_data(page: str | None) -> dict | None:
    """Where a job's Apply leads, as the page's own data says: the `applicationDetail` object of its
    `applyButtonData` (`type`, and for `APPLY_TO_URL` the employer's `url`), read from the escaped payload in the
    page's script text or from plain JSON. Only that object: other keys of the page (an `easyApply` flag) belong to the
    jobs the page lists as similar. None when the page has no such object. Pure."""
    if not page or "applyButtonData" not in page:
        return None
    for found in _APPLICATION_DETAIL.finditer(page):
        raw = found.group(2)
        try:
            detail = json.loads(json.loads(f'"{raw}"') if found.group(1) else raw)
        except ValueError:
            continue
        if isinstance(detail, dict):
            return detail
    return None


def apply_link(page: str | None, page_url: str, hosts: frozenset[str] = frozenset()) -> dict:
    """Where a listing's Apply leads, read from the page and never clicked. Returns `state` (`page_state`), `url` (the
    one offsite address, or None), `candidates` (every distinct offsite address seen), `easy_apply` (the Apply button
    itself says Easy Apply and there is no offsite address) and `gone_words` (what the page said, when removed)."""
    page = page or ""
    found: dict[str, str] = {}
    unescaped = htmllib.unescape(page)
    for raw in _APPLY_KEYS.findall(unescaped):
        if (url := _offsite(_json_string(raw), page_url, hosts)) is not None:
            found.setdefault(_same(url), url)
    for attrs_text, inner in _ANCHOR.findall(page):
        attrs = _attrs(attrs_text)
        label = f"{attrs.get('aria-label', '')} {adapters.html_to_text(inner)}"
        if not _APPLY.search(label) or _EASY.search(label.strip()) or "easy apply" in label.lower():
            continue
        if (url := _offsite(attrs.get("href", ""), page_url, hosts)) is not None:
            found.setdefault(_same(url), url)
    detail = apply_data(page)
    if (detail is not None and str(detail.get("type") or "").upper() == "APPLY_TO_URL"
            and isinstance(detail.get("url"), str)):
        if (url := _offsite(detail["url"], page_url, hosts)) is not None:
            found.setdefault(_same(url), url)
    easy = detail is not None
    for attrs_text, inner in _BUTTON.findall(page):
        attrs = _attrs(attrs_text)
        if _EASY.search(attrs.get("aria-label", "")) or _EASY.search(adapters.html_to_text(inner)):
            easy = True
            break
    state = page_state(page, page_url)
    gone = None
    if state == STATE_REMOVED:
        gone = closed_words.found(_own_words(page))
    candidates = list(found.values())
    return {"state": state, "url": candidates[0] if len(candidates) == 1 else None, "candidates": candidates,
            "easy_apply": easy and not candidates, "gone_words": gone,
            "apply_type": str(detail.get("type")) if detail is not None and detail.get("type") else None}


def _title_of(page: str) -> str | None:
    for pattern in (r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)', r"<h1\b[^>]*>(.*?)</h1\s*>",
                    r"<title\b[^>]*>(.*?)</title\s*>"):
        if m := re.search(pattern, page, re.I | re.S):
            text = squash(adapters.html_to_text(m.group(1)))
            if text:
                return text
    return None


def _page_body(page: str) -> str:
    """The page's own main content, its chrome and markup left out (`adapters.main_text`): never the whole page."""
    return adapters.main_text(page) or ""


def digest(page: str | None, page_url: str, hosts: frozenset[str] = frozenset()) -> dict:
    """Only what a job needs from a page, in place of its whole accessibility snapshot: `title`, `company` and
    `location` (as the page's JobPosting states them; the title from the page's heading when it states none), `text`
    (the posting's body: the JobPosting's description, else the board's description element, else the page's main
    text; `text_from` says which, and `text_whole` whether it reads as the whole posting), `apply_url` (the one offsite
    Apply address, or the several in `apply_candidates`), `easy_apply`, and `state` (open, removed, sign_in_wall,
    check)."""
    page = page or ""
    facts = posting_facts(page)
    apply = apply_link(page, page_url, hosts)
    text, source = facts.get("text"), "job_posting"
    if not text:
        text, source = _section_text(page, _is_body), "description"
    if not text:
        text, source = _page_body(page), "page"
    out = {"url": page_url, "title": facts.get("title") or _title_of(page), "company": facts.get("company"),
           "location": facts.get("location"), "text": text or "", "text_from": source,
           "text_whole": whole_text(text) is not None, "apply_url": apply["url"], "easy_apply": apply["easy_apply"],
           "state": apply["state"]}
    if len(apply["candidates"]) > 1:
        out["apply_candidates"] = apply["candidates"]
    if apply["gone_words"]:
        out["gone_words"] = apply["gone_words"]
    return out


# --- the app's browser, over DevTools --------------------------------------------------------------------------------


@dataclass
class Page:
    """A page as the app's browser shows it: where it ended up, the status it was answered with (0 when unknown) and
    its markup."""

    url: str
    status: int
    html: str


def own_tab(url: str = "about:blank", *, window: bool = False) -> dict:
    """The parameters of `Target.createTarget` for a tab the app works in: the one place that says the app's own
    window (the owner, 2026-10-22). Through the browser bridge (`app/scout/own-chrome.mjs`) a tab is opened from the
    bridge's own tab, in the app's window; `newWindow` is what a Chrome reached without the bridge needs, so a tab never
    lands in a window of the owner's; `window` asks the bridge for a visible window of its own (Go's first tab). The app
    closes only the tabs it made (`Target.closeTarget` of its own targetId) and never ends or restarts Chrome."""
    return {"url": url, "newWindow": True, **({"resumebotWindow": True} if window else {})}


class BrowserUnavailable(Exception):
    """The app's browser could not be reached or would not load the page."""


#: How long the bridge may take to accept a connection, and to answer one command. A bridge stuck on an open answers
#: neither, and a wait with no end held a code job for its whole `CODE_SECONDS` and a digest for 300 s. The bridge's own
#: longest answer is a tab that never shows (15 s) or a first tab opened afresh and tried again, well inside this.
OPEN_SECONDS, ANSWER_SECONDS = 45.0, 60.0

#: What the bridge answers when a command names a tab that is not this connection's.
NOT_MINE = "not a tab this session opened"


async def open_own_tab(tools, url: str = "about:blank") -> tuple[str, str]:
    """A new tab of the app's own window, attached: (targetId, sessionId). The bridge may refuse the attach to a tab it
    had just opened as "not a tab this session opened"; that answer is met by opening a fresh tab (the refused one is
    closed if the bridge allows) before it is given up. Raises BrowserUnavailable."""
    for again in (False, True):
        target = (await tools.call("Target.createTarget", own_tab(url)))["targetId"]
        try:
            attached = await tools.call("Target.attachToTarget", {"targetId": target, "flatten": True})
            return target, attached["sessionId"]
        except BrowserUnavailable as exc:
            if again or NOT_MINE not in str(exc):
                raise
            try:
                await tools.call("Target.closeTarget", {"targetId": target})
            except BrowserUnavailable:
                pass
    raise BrowserUnavailable(NOT_MINE)  # unreachable: the second pass returns or raises


READY = "location.href !== 'about:blank' && document.readyState"
_TEXT_LENGTH = "((document.body && document.body.innerText) || '').length"
_READ_PAGE = (
    "(() => { const n = performance.getEntriesByType('navigation')[0]; "
    "return JSON.stringify({url: location.href, status: (n && n.responseStatus) || 0, "
    f"html: document.documentElement.outerHTML.slice(0, {PAGE_MAX_CHARS})}}); }})()")


#: The header the bridge reads its key from on its HTTP routes (app/scout/own-chrome.mjs `BRIDGE_KEY_HEADER`).
BRIDGE_KEY_HEADER = "X-Bridge-Key"


class _DevTools:
    """A small DevTools client over one WebSocket to the bridge on 127.0.0.1 (app/scout/own-chrome.mjs), which gives
    it only the tabs it opens and closes them when it goes."""

    def __init__(self, port: int, owner_id: str = "code:read", kind: str = "code") -> None:
        self.port, self.seq = port, 0
        #: Whose the tabs this connection opens are on the tab register (docs/PRD.md path 52): a code job's name
        #: (`code:<job>`), or `go` for Launch's tabs, which are the owner's. Also the name the bridge is told in the
        #: address, so it knows these are the app's own and leaves their registering to this code.
        self.owner_id, self.kind = owner_id, kind
        #: The role the next tab is for (Launch's), registered with the tab, then cleared.
        self.role_id: uuid.UUID | None = None
        #: When a list, the events the browser sends between a call and its answer are kept in it (the Network domain).
        self.events: list[dict] | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        ws_key = base64.b64encode(os.urandom(16)).decode()
        label = self.owner_id.replace(":", ".")
        # The bridge serves only clients that carry the app's key (docs/PRD.md path 52; the owner, 2026-11-14).
        bridge_key = settings.bridge_key()
        query = f"?key={quote(bridge_key, safe='')}" if bridge_key else ""
        self.writer.write((f"GET /devtools/browser/{label}{query} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                           f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {ws_key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        await self.writer.drain()
        try:
            async with asyncio.timeout(OPEN_SECONDS):
                head = await self.reader.readuntil(b"\r\n\r\n")
        except TimeoutError as exc:
            raise BrowserUnavailable("the browser bridge did not answer the connection") from exc
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise BrowserUnavailable(head.split(b"\r\n", 1)[0].decode("latin-1", "replace"))

    async def close(self) -> None:
        if self.writer is not None:
            # Send a WebSocket close frame first (RFC 6455: masked, empty close): a socket closed without one leaves
            # the bridge counting this session as connected.
            try:
                await self._send(b"", 0x8)
            except Exception:  # the bridge is gone already
                pass
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:  # the bridge closed it first
                pass

    async def _send(self, payload: bytes, opcode: int = 0x1) -> None:
        assert self.writer is not None
        n, mask = len(payload), os.urandom(4)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        self.writer.write(head + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))
        await self.writer.drain()

    async def _message(self) -> dict:
        assert self.reader is not None
        parts: list[bytes] = []
        while True:
            first, second = await self.reader.readexactly(2)
            n = second & 0x7F
            if n == 126:
                n = int.from_bytes(await self.reader.readexactly(2), "big")
            elif n == 127:
                n = int.from_bytes(await self.reader.readexactly(8), "big")
            mask = await self.reader.readexactly(4) if second & 0x80 else b""
            data = await self.reader.readexactly(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            opcode = first & 0x0F
            if opcode == 0x8:
                raise BrowserUnavailable("the browser closed the connection")
            if opcode == 0x9:
                await self._send(data, 0xA)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if first & 0x80:
                return json.loads(b"".join(parts))

    async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        """One DevTools command. A tab this connection opens is put on the tab register as it exists, and taken off it
        where this code closes it (docs/PRD.md 52.1, 52.2)."""
        db.outside_transaction("a DevTools call")  # the browser answers when it answers: no transaction is held for it
        result = await self._call(method, params, session_id)
        if method == "Target.createTarget" and result.get("targetId"):
            role, self.role_id = self.role_id, None
            await tab_register.opened(str(result["targetId"]), self.kind, self.owner_id, role)
        elif method == "Target.closeTarget" and not session_id and (params or {}).get("targetId"):
            await tab_register.closed(str((params or {})["targetId"]))
        return result

    async def _call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        self.seq += 1
        mine = self.seq
        msg: dict = {"id": mine, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await self._send(json.dumps(msg).encode())
        try:
            async with asyncio.timeout(ANSWER_SECONDS):
                while True:
                    got = await self._message()
                    if "method" in got and self.events is not None:
                        self.events.append(got)
                    if got.get("id") == mine:
                        if "error" in got:
                            raise BrowserUnavailable(f"{method}: {got['error'].get('message', 'refused')}")
                        return got.get("result") or {}
        except TimeoutError as exc:
            raise BrowserUnavailable(f"{method}: the browser did not answer") from exc


async def _read_tab(tools: _DevTools, sid: str, url: str) -> Page:
    """Wait for the tab of session `sid` to load (up to `LOAD_SECONDS`) and draw (`SETTLE_SECONDS`), then read it."""
    loop = asyncio.get_running_loop()
    until = loop.time() + LOAD_SECONDS
    while loop.time() < until:
        state = await tools.call("Runtime.evaluate", {"expression": READY, "returnByValue": True}, sid)
        if (state.get("result") or {}).get("value") == "complete":
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(SETTLE_SECONDS)
    filled = loop.time() + FILL_SECONDS
    while loop.time() < filled:  # complete is not drawn: wait for the page's text to appear (digest 94% empty, 10-20)
        shown = await tools.call("Runtime.evaluate", {"expression": _TEXT_LENGTH, "returnByValue": True}, sid)
        if int((shown.get("result") or {}).get("value") or 0) >= FILL_CHARS:
            break
        await asyncio.sleep(FILL_EVERY)
    read = await tools.call("Runtime.evaluate", {"expression": _READ_PAGE, "returnByValue": True}, sid)
    value = json.loads((read.get("result") or {}).get("value") or "{}")
    return Page(url=str(value.get("url") or url), status=int(value.get("status") or 0),
                html=str(value.get("html") or ""))


class GoWindow:
    """Go's window in the app's Chrome (the owner, 2026-10-16), through the bridge: a normal, visible window of its own
    (never minimized, never raised), the batch's pages as its tabs, each opened straight at its address. The app reads
    each tab and closes a closed posting's; `keep` hands every tab left to the owner, after which the app never
    navigates or closes them. Raises BrowserUnavailable."""

    def __init__(self) -> None:
        self.tools = _DevTools(settings.bridge_port(), "go", "go")
        self.tabs: dict[str, str] = {}  # targetId -> its session, in the order they opened

    async def open(self) -> None:
        try:
            await self.tools.open()
        except (OSError, asyncio.IncompleteReadError, BrowserUnavailable) as exc:
            await self.tools.close()  # a connection left open keeps the app's Chrome from closing when the work is done
            if isinstance(exc, BrowserUnavailable):
                raise
            raise BrowserUnavailable(str(exc) or type(exc).__name__) from exc

    async def tab(self, url: str, role_id: uuid.UUID | None = None) -> str:
        """A new tab at `url`: the window's first opens the window; the rest are opened from a tab of it, so they are
        its tabs. `role_id`: the role it is for, kept with the tab on the register (it closes when the role leaves Ready
        to Launch, docs/PRD.md 52.4)."""
        self.tools.role_id = role_id
        try:
            if not self.tabs:
                params: dict = own_tab(url, window=True)
            else:
                first, sid = next(iter(self.tabs.items()))
                params = {"url": url, "resumebotFrom": {"targetId": first, "sessionId": sid}}
            target = (await self.tools.call("Target.createTarget", params))["targetId"]
            self.tabs[target] = (await self.tools.call("Target.attachToTarget",
                                                       {"targetId": target, "flatten": True}))["sessionId"]
            return target
        except (OSError, asyncio.IncompleteReadError, KeyError) as exc:
            raise BrowserUnavailable(str(exc) or type(exc).__name__) from exc

    async def read(self, target: str, url: str) -> Page:
        try:
            return await _read_tab(self.tools, self.tabs[target], url)
        except (OSError, asyncio.IncompleteReadError, KeyError, ValueError) as exc:
            raise BrowserUnavailable(str(exc) or type(exc).__name__) from exc

    async def close_tab(self, target: str) -> None:
        self.tabs.pop(target, None)
        try:
            await self.tools.call("Target.closeTarget", {"targetId": target})
        except (OSError, asyncio.IncompleteReadError) as exc:
            raise BrowserUnavailable(str(exc) or type(exc).__name__) from exc

    async def keep(self) -> None:
        """Hand the window's tabs to the owner and let go."""
        try:
            if self.tabs:
                await self.tools.call("Target.resumebotKeep")
        finally:
            await self.tools.close()


async def _load(url: str) -> Page:
    """Open `url` in a tab of the app's browser, wait for it to load and draw, read it, and close the tab."""
    tools = _DevTools(settings.bridge_port(), "code:read")
    try:
        await tools.open()
        target, sid = await open_own_tab(tools, url)  # straight to its address
        try:
            return await _read_tab(tools, sid, url)
        finally:
            try:
                await asyncio.shield(tools.call("Target.closeTarget", {"targetId": target}))
            except Exception:  # the bridge closes the tab when the connection goes anyway
                pass
    except (OSError, asyncio.IncompleteReadError, KeyError, ValueError) as exc:
        raise BrowserUnavailable(str(exc) or type(exc).__name__) from exc
    finally:
        await tools.close()


#: How long the bridge may take to start the app's Chrome when it is closed (its own wait is 30 s; Go waits less).
START_SECONDS = 20.0


async def start_browser() -> str:
    """Make sure the app's Chrome is running: ask the bridge for its DevTools address, which starts the profile's Chrome
    (its window on screen, opened in the background, no focus taken) when it is closed, as a browser job does. "" when
    it is up, else one line of why."""
    try:
        async with httpx.AsyncClient(timeout=START_SECONDS) as client:
            reply = await client.get(f"http://127.0.0.1:{settings.bridge_port()}/json/version",
                                     headers={BRIDGE_KEY_HEADER: settings.bridge_key()})
    except httpx.HTTPError as exc:
        return f"the browser bridge did not answer ({type(exc).__name__})"
    if reply.status_code == 200:
        return ""
    try:
        why = str(reply.json().get("why") or "")
    except ValueError:
        why = ""
    return why or f"the browser bridge answered HTTP {reply.status_code}"


async def open_page(url: str) -> Page:
    """The page at `url` as the app's browser shows it (tests replace this). Raises BrowserUnavailable."""
    return await _load(url)


#: How the code waits out a page gap (tests replace it).
sleep = asyncio.sleep


# --- what code makes of an item ---------------------------------------------------------------------------------------


@dataclass
class Job:
    """What the code-first worker needs of an item, read while it is leased."""

    item_id: str
    kind: str
    link: str
    domain: str
    title: str | None
    named: bool  # a job added by its link: its title and company must come with the text
    site: str = ""  # a record item: the site's key
    recipe: dict | None = None  # a live posting recipe of the link's host (`recipe_live.posting_recipe_for`)
    recipe_id: object = None
    page_recipe: dict | None = None  # a live page recipe of the link's site (`recipe_live.page_recipe_for`)
    shell: bool = False  # the domain's pages hold no text in their HTML (`browser_pace.shell_domain`): no fetch first
    page_read: str | None = None  # how the page fetch went (`text`, `shell`, `refused`), to be kept for the domain
    ext_id: str = ""  # a listing item: the board posting's id, whose page is read
    code_only: bool = False  # a role in Followup needed's source item: only the code takes it, no session does


def _is_this_job(page: str, job: Job) -> bool:
    """Whether a page with no embedded JobPosting is this job's: a heading of the page (its `og:title`, its `h1`, its
    title) is the role's title (`this_posting.is_title`: the title with a tail that says the site or the job's number,
    not a heading that lists many jobs). The title merely being in the text is not enough: a page that lists many jobs
    holds it too. A role with no title cannot be told, so it is not."""
    return this_posting.has_title_heading(page, job.title)


def _from_page(page: str, job: Job) -> dict | None:
    """A `text` result from the JobPosting a page embeds, or, when it embeds no description, from its main readable text
    (`adapters.page_text`), when it is the whole posting of this job, else None. A page's own text names no company, so
    a job added by its link (which needs the title and the company with the text) is read only from an embedded
    JobPosting."""
    facts = posting_facts(page)
    if not facts.get("text"):
        found = None if job.named else adapters.page_text(page)
        text = whole_text(found)
        return {"result": browser_pace.TEXT, "text": text} if text and _is_this_job(page, job) else None
    text = whole_text(facts.get("text"))
    if text is None:
        return None
    if job.title and facts.get("title") and not employer_posting.titles_match(job.title, facts["title"]):
        return None  # another job's posting
    if job.named and not (facts.get("title") and facts.get("company")):
        return None
    return {"result": browser_pace.TEXT, "text": text, "title": facts.get("title"), "company": facts.get("company")}


def _readable(page: str) -> bool:
    """Whether the page's HTML holds posting text of its own (an embedded JobPosting, or a main text that is whole)."""
    return bool(posting_facts(page)) or whole_text(adapters.page_text(page)) is not None


async def read_by_code(job: Job, hosts: frozenset[str], turn) -> dict | None:
    """A `read` item tried by code: the job system's own answer, the page read politely, then the app's browser when the
    page draws itself. Returns a hand-back's fields, or None (a session reads it). `turn()` waits out the domain's page
    gap before the browser loads a second page there."""
    link = job.link
    found = ats.parse(link)
    # A job system's posting is read by its adapter, never by a session or the app's browser (the owner, 2026-10-22).
    browser_needed = not (found and _code_reads(found))
    if job.recipe is not None and not is_board_link(link, hosts) and not job.named:
        # The host's posting recipe is THE reader of its pages (the owner, 2026-10-22): the posting's own data request,
        # through the polite fetcher. A page it cannot read goes the way it always went.
        fetch = polite.make()
        try:
            recipe = skills.parse_recipe(job.recipe)
            if isinstance(recipe, skills.HtmlPostingRecipe):  # the posting's own HTML, read by selector (PRD 49)
                from app.jobsearch.browser.recipes import html_run as html_recipe

                seen = await html_recipe.read_text(fetch, recipe, link)
                if seen is not None and seen.text is None and (said := polite.gone(link, seen.status)):
                    return {"result": browser_pace.GONE, "url": link, "note": said, "via_recipe": True}
                if seen is not None and seen.gone:  # the page says the posting is closed: not live text
                    return {"result": browser_pace.GONE, "url": link, "note": seen.gone, "via_recipe": True}
                if seen is not None and (text := whole_text(seen.text)):
                    return {"result": browser_pace.TEXT, "text": text, "raw": seen.raw, "via_recipe": True}
                read = None
            else:
                read = await skills.read_posting(fetch, recipe, link) if isinstance(
                    recipe, skills.PostingRecipe) else None
            if read is not None:
                if read.text is None and (said := polite.gone(link, read.reply.status_code)):
                    return {"result": browser_pace.GONE, "url": link, "note": said, "via_recipe": True}
                if text := whole_text(read.text):
                    return {"result": browser_pace.TEXT, "text": text, "via_recipe": True}
        except (polite.Refused, httpx.HTTPError, ValueError, skills.SkillError):
            pass  # a stop or a changed site: the read goes on as it did before the recipe
        finally:
            await fetch.close()
    if job.page_recipe is not None and not is_board_link(link, hosts) and not job.named:
        # The site's page recipe has an element that holds a posting's text: its page is opened in the app's Chrome and
        # that element read, by code (the owner, 2026-10-22). A page it cannot read goes the way it always went.
        from app.jobsearch.browser.recipes import html_run as html_recipe
        from app.jobsearch.browser.recipes import page_run as page_recipe

        try:
            if job.page_recipe.get("kind") == "html":  # selectors recorded for the page a plain fetch gets (PRD 49)
                html_fetch = polite.make()
                try:
                    seen = await html_recipe.read_text(html_fetch, skills.HtmlRecipe.model_validate(job.page_recipe),
                                                       link)
                finally:
                    await html_fetch.close()
                if seen is not None and seen.gone:  # the page says the posting is closed: not live text
                    return {"result": browser_pace.GONE, "url": link, "note": seen.gone}
                read = {"text": seen.text or "", "raw": seen.raw} if seen is not None else None
            else:
                read = await page_recipe.read_posting(skills.parse_page(job.page_recipe), link, turn)
        except (polite.Refused, ValueError, skills.SkillError):
            read = None
            job.page_read = "refused"
        if read is not None and (text := whole_text(read["text"])):
            return {"result": browser_pace.TEXT, "text": text, "raw": read["raw"]}
    if not is_board_link(link, hosts):  # a job board's page is never read by the app's fetch
        fetch = polite.make()
        try:
            if found and found.read and not job.named:
                got = await postings.read_link(fetch, link)
                if text := whole_text(got.text):
                    return {"result": browser_pace.TEXT, "text": text}
            if not job.shell or not browser_needed:  # a domain whose pages draw themselves goes straight to the
                # browser, unless it is a job system's host (the page is fetched, the adapter is the way)
                try:
                    reply = await fetch.get(link)
                except polite.Refused:
                    job.page_read = "refused"
                    return None  # a refusal is a stop for the app's code; a session reads it as before
                except (httpx.HTTPError, ValueError):
                    return None
                if said := polite.gone(link, reply.status_code):
                    return {"result": browser_pace.GONE, "url": link, "note": said}
                if reply.status_code != 200 or "html" not in reply.headers.get("content-type", "html").lower():
                    return None
                if got := _from_page(reply.text, job):
                    job.page_read = "text"
                    return {**got, "raw": reply.content}
                # A page that holds a posting (or its text) in its HTML shows no more in a browser; nor does a job
                # system's. Only a page with no text of its own draws itself.
                browser_needed = browser_needed and not _readable(reply.text)
                job.page_read = "shell" if browser_needed else "text"
        finally:
            await fetch.close()
        if browser_needed:
            await turn()
    if not browser_needed:
        return None
    page = await open_page(link)
    if said := polite.gone(link, page.status):
        return {"result": browser_pace.GONE, "url": link, "note": said}
    got = _from_page(page.html, job)
    return {**got, "raw": page.html.encode("utf-8")} if got else None


#: What a board answers when it limits the app (a rest: 429, and a board's 999) or refuses it (a stop: 403).
RATE_LIMITED, REFUSED = (429, 999), (403,)


def _refusal(page: Page, state: str | None = None) -> dict | None:
    """A `blocked` hand-back's fields when a board did not show the listing but limited or refused the app: its status
    (429 or 999 a rest, 403 a stop) or a bot check it showed (`state`, a stop). The hand-back's own rule tells a rest
    (`host_stops.is_rate_limit`: an hour, then eight, no try used) from a stop (the site is stopped, the item stuck with
    the reason on the role's card; at a followed board the stop ends at the owner's next off hours start and the item
    waits for it, PRD 48.14 d): the note says which in its words. None when the page is no refusal."""
    if page.status in RATE_LIMITED:
        return {"result": browser_pace.BLOCKED,
                "note": f"HTTP {page.status}: the site is rate limiting the app, so it rests"}
    if page.status in REFUSED:
        return {"result": browser_pace.BLOCKED, "note": f"HTTP {page.status}: the site refused the page"}
    if state == STATE_CHECK:
        return {"result": browser_pace.BLOCKED,
                "note": "a security check page: the site asked the visitor to verify they are human"}
    return None


async def source_by_code(job: Job, hosts: frozenset[str], turn=None) -> dict | None:
    """A `source` item tried by code. At an address no job board owns (the employer's own posting page) the page is read
    as a `read` is, and when it is the whole posting of the role, the role is sourced at that address with the text.
    On a followed board or a board whose posting page code reads, the listing is opened in the app's browser and its
    Apply link's destination read, never clicked; a board that limits or refuses the app, or shows a bot check, is
    handed back as `blocked` (`_refusal`:
    the rest or the stop a session's hand-back would have made: otherwise the item would go on to a session with
    none). Returns a hand-back's fields, or None (a session sources it)."""
    if job.domain not in browser_pace.code_source_domains():
        if is_board_link(job.link, hosts) or turn is None:
            if job.code_only and turn is not None:  # no session will ever take it: code has no route to this board
                return {"result": browser_pace.NOT_FOUND, "final": True,
                        "note": "the role's link is a job board's listing the app's code has no way to read"}
            return None  # another board's listing: a session decides
        got = await read_by_code(job, hosts, turn)
        if got is None:
            return None
        if got["result"] == browser_pace.TEXT:
            return {**got, "result": browser_pace.FOUND, "url": job.link, "source_kind": "employer"}
        return got if got["result"] == browser_pace.GONE else None
    page = await open_page(job.link)
    if refused := _refusal(page):
        return refused
    if said := polite.gone(job.link, page.status):
        return {"result": browser_pace.GONE, "url": job.link, "note": said}
    read = apply_link(page.html, page.url or job.link, hosts)
    if refused := _refusal(page, read["state"]):
        return refused
    if read["state"] == STATE_REMOVED and read["gone_words"]:
        return {"result": browser_pace.GONE, "url": job.link, "note": f"The listing says: {read['gone_words']}"}
    if read["state"] != STATE_OPEN:
        return None  # a sign-in wall or a check: a session decides
    html = page.html
    if browser_pace.reads_postings(job.domain) and not read["url"] and not _has_flight(html) and turn is not None:
        # the page the browser shows holds none of the board's page data (its script text was taken out): the same page,
        # fetched once at the domain's own pace, does (robots.txt allows /job-detail)
        await turn()
        if fetched := await _fetched_html(job.link):
            html = fetched
            read = apply_link(html, page.url or job.link, hosts)
    if read["url"]:
        return {"result": browser_pace.FOUND, "url": read["url"], "source_kind": "employer"}
    if read["easy_apply"] and (text := listing_text(html)):
        got = {"result": browser_pace.EASY_APPLY, "url": job.link, "text": text}
        if read.get("apply_type"):
            got["note"] = f"the page data gives the apply type {read['apply_type']}, with no employer address"
        return got
    if job.code_only and browser_pace.reads_postings(job.domain) and read.get("apply_type") is None:
        # the page is open and was read, and its page data names no apply address: nothing more will come of it
        return {"result": browser_pace.NOT_FOUND, "final": True,
                "note": "the listing's page data names no address where Apply leads"}
    return None


def _has_flight(html: str | None) -> bool:
    """Whether a page holds the board's page data (the apply button's data, or the flight scripts that carry it)."""
    return bool(html) and ("applyButtonData" in html or "__next_f" in html)


async def _fetched_html(link: str) -> str | None:
    """One page fetched politely (robots.txt, the app's agent), or None when it was refused or did not answer."""
    fetch = polite.make()
    try:
        reply = await fetch.get(link)
    except (polite.Refused, httpx.HTTPError, ValueError):
        return None
    finally:
        await fetch.close()
    return reply.text if reply.status_code == 200 else None


def listing_result(view: dict, job: Job) -> dict:
    """What a posting page's read-out (`signed_in.Run.view`) makes of a `listing` item, as a hand-back's fields: `gone`
    for a page that says the posting was removed; `text`, with the read-out as JSON bytes in `raw`
    (`capture.READ_KEYS`), for a page that gives the whole text (`whole.refusal` clean); a sign-in wall or a check shown
    in place of the posting, a rest or a stop; else `unreadable`, which keeps nothing: a page with no whole text is no
    page read."""
    capture = registry.board_reader("capture")  # it reads this module
    if capture is None:  # no reader of a board's pages here: such an item cannot have been made
        return {"result": browser_pace.UNREADABLE, "note": "the page showed no whole description of the posting"}
    name = browser_pace.site_name(job.domain)
    state = view.get("state")
    if state == STATE_REMOVED:
        return {"result": browser_pace.GONE, "url": job.link,
                "note": f"{name}'s page says the job is no longer accepting applications or has been removed"}
    if state == STATE_SIGN_IN:
        return {"result": browser_pace.BLOCKED, "note": signed_out_note(name)}
    if state == STATE_CHECK:
        return {"result": browser_pace.BLOCKED,
                "note": f"a security check page: {name} asked the visitor to verify they are human"}
    text = (view.get("text") or "").strip()
    if text and whole.refusal(text, f"the {name} posting's text") is None:
        read = json.dumps({key: view.get(key) for key in capture.READ_KEYS}, ensure_ascii=False)
        return {"result": browser_pace.TEXT, "raw": read.encode("utf-8")}
    return {"result": browser_pace.UNREADABLE, "note": "the page showed no whole description of the posting"}


def signed_out_note(name: str) -> str:
    """What a `listing` item says when the board (`name`) showed a sign-in page: the app's Chrome is not signed in. It
    has the words of a rest ("try again later"), so the hand-back rests the board an hour and uses no try, as for a
    slow-down."""
    return f"{name} showed a sign-in page, so the app's Chrome is not signed in to {name}: try again later"


async def listing_by_code(job: Job, owner: str | None = None) -> dict:
    """A `listing` item tried by code: the role's board posting page opened in the app's Chrome, one page in a tab of
    its own, and its read-out made a result (`listing_result`; the owner, 2026-11-05, docs/PRD.md 48.14). The page's
    turn on the app's one clock for the board was taken when the item was leased (`code_take`), so no second turn is
    taken here, and a slow-down is not waited out inside the run (`waits_out=False`: past `CODE_SECONDS` the item would
    time out with no rest recorded, and the next page would load thirty seconds later against a site that is limiting
    the app): the run ends and the hand-back rests the board an hour, then eight. Nothing is typed or clicked.

    Raises `BrowserUnavailable` when Chrome cannot be had or its tab would not open: no try is used. A check or a
    refusal is `blocked` with no rate-limit words (a stop: at the board until the owner's next off hours start, PRD
    48.14 d); a slow-down, or a sign-in page, with them (a rest)."""
    capture, chrome = registry.board_reader("capture"), registry.board_reader("signed_in")
    if capture is None or chrome is None:  # nothing here reads a board's pages
        return {"result": browser_pace.UNREADABLE, "note": "the app has no way to read this board's page"}
    name = browser_pace.site_name(job.domain)
    if not job.ext_id:  # the item names no posting that is still in the index: there is no page to open
        return {"result": browser_pace.UNREADABLE,
                "note": "the posting this item was made for is no longer in the index"}
    if why := await start_browser():
        raise BrowserUnavailable(why)
    views: list[dict] = []

    async def kept(_ext_id: str, view: dict) -> None:
        views.append(view)

    pages = await capture.read_pages(chrome.make_tools(), [job.ext_id], kept, waits_out=False)
    if views:
        return listing_result(views[0], job)
    if pages.unreachable:
        raise BrowserUnavailable(pages.unreachable)
    if pages.signed_out:
        return {"result": browser_pace.BLOCKED, "note": signed_out_note(name)}
    if pages.halted == capture.SLOWED:
        return {"result": browser_pace.BLOCKED, "note": f"{name} asked the app to slow down ({pages.stopped})"}
    if pages.stopped:  # a check or a refusal: a stop (`capture.RESTING` cannot come: no turn is taken here)
        return {"result": browser_pace.BLOCKED, "note": pages.stopped}
    return {"result": browser_pace.UNREADABLE, "note": f"{name} could not show the page (it answered with an error)"}


# --- the code-first worker --------------------------------------------------------------------------------------------


async def _turn(owner: str, domain: str, now: datetime | None = None) -> None:
    async with db.session_factory()() as session:
        wait = await browser_door.page_turn(session, owner, domain, now or datetime.now(UTC))
        await session.commit()
    if wait:
        await sleep(wait)


class _ReadingFailed(Exception):
    """The reads that come before a try could not be made."""


async def code_once(owner: str, sid: str, now: datetime | None = None) -> bool:
    """Lease the next item code tries first, try it, and hand back what came of it or pass it on to a session. Returns
    whether there was an item. The network work is done outside any transaction."""
    async with db.session_factory()() as session:
        row = await browser_door.code_take(session, owner, sid, now)
        if row is None:
            await session.commit()
            return False
        # the lease is committed first: the reads that follow are made in a reader's session, with no turn held
        leased = (str(row.id), row.kind, row.link or "", row.domain, row.site or "", row.role_id, row.posting_id,
                  bool(row.code_only))
        await session.commit()
    item_id, kind, link, domain, site, role_id, posting_id, code_only = leased
    job = Job(item_id=item_id, kind=kind, link=link, domain=domain, title=None, named=posting_id is not None,
              site=site, code_only=code_only)
    hosts: frozenset[str] = frozenset()
    why = "nothing whole and certain came of it"
    got: dict | None = None
    try:
        async with db.read_session_factory()() as reader:
            role = await reader.get(Opportunity, role_id) if role_id is not None else None
            page_of = (await reader.get(JobPosting, posting_id)
                       if kind == browser_pace.LISTING and posting_id is not None else None)
            # the board job whose page is opened: a posting of the nightly search holds its id, a role's own link
            # (the tracker's import) holds it in the link (`guest.page_id`)
            job.title = role.title if role is not None else None
            guest = registry.board_reader("guest")  # it reads postings, which read this module
            job.ext_id = (guest.page_id(page_of) or "") if page_of is not None and guest is not None else ""
            hosts = await employer_posting.job_board_hosts(reader, owner)
            if kind in (browser_pace.READ, browser_pace.SOURCE) and domain not in browser_pace.code_source_domains():
                job.shell = await browser_pace.shell_domain(reader, owner, domain, now or datetime.now(UTC))
            if kind == browser_pace.READ and not job.named:
                from app.jobsearch.browser.recipes import live as recipe_live  # it reads this module's neighbours

                page = await recipe_live.page_recipe_for(reader, owner, link)
                job.page_recipe = page.recipe if page is not None else None

                posting_recipe = await recipe_live.posting_recipe_for(reader, owner, link)
                if posting_recipe is not None:
                    job.recipe, job.recipe_id = posting_recipe.recipe, posting_recipe.id
        reading = True
    except Exception:  # the item must not stay leased to this worker: it goes the way a failed try goes
        log.warning("reading for the code-first try of browser item %s failed", item_id, exc_info=True)
        why = "reading for the try failed"
        reading = False
        job = Job(item_id=item_id, kind=kind, link=link, domain=domain, title=None, named=posting_id is not None,
                  site=site, code_only=code_only)
    transient = not reading  # the try did not get to look at the page: it used no try
    try:
        if not reading:
            raise _ReadingFailed
        async with asyncio.timeout(CODE_SECONDS):
            if job.kind == browser_pace.SOURCE:
                got = await source_by_code(job, hosts, lambda: _turn(owner, job.domain, now))
            elif job.kind == browser_pace.RECORD:
                from app.jobsearch.browser.recipes import (
                    record as recipe_record,  # it uses this module's browser client
                )

                got = await recipe_record.record_by_code(owner, job.site)
            elif job.kind == browser_pace.LISTING:
                got = await listing_by_code(job, owner)
            else:
                got = await read_by_code(job, hosts, lambda: _turn(owner, job.domain, now))
    except _ReadingFailed:
        pass  # `why` says so
    except TimeoutError:
        why, transient = "it took too long", True
    except BrowserUnavailable as exc:
        why, transient = f"the app's browser was not available ({str(exc)[:80]})", True
        if job.kind == browser_pace.LISTING:
            browser_handback.note_tab_failed(job.item_id)  # the page was never seen: no try is used
    except Exception:  # whatever the try does, the item goes to a session
        log.warning("the app's code-first try of browser item %s failed", job.item_id, exc_info=True)
        why, transient = "the try failed", True
    if job.page_read:  # how the page fetch went on this domain is kept: a page that draws itself skips the fetch next
        async with db.session_factory()() as session:
            await browser_pace.note_page_read(session, owner, job.domain, job.page_read, now or datetime.now(UTC))
            await session.commit()
    if job.recipe_id is not None:  # the host's recipe was asked: its health is how its reads go
        from app.jobsearch.browser.recipes import live as recipe_live

        async with db.session_factory()() as session:
            await recipe_live.note_posting_read(session, job.recipe_id, bool(got and got.pop("via_recipe", False)),
                                                now)
            await session.commit()
    raw = got.pop("raw", None) if got is not None else None
    if got is not None:
        got.pop("via_recipe", None)
    if got is None and job.kind == browser_pace.READ and (system := ats.parse(job.link)) and _code_reads(system):
        got = {"result": browser_pace.UNREADABLE, "note": f"the job system's own answer gave no whole text ({why}); "
                                                "its postings are read by code, so it is tried again later"}
    # no session takes a record item: a try that failed is a failed result
    if got is None and job.kind == browser_pace.RECORD:
        got = {"result": browser_pace.FAILED, "note": why}
    # nor a listing item: a try that found nothing is an unreadable page, never passed on (`code_pass_on` would leave it
    # waiting for nobody)
    if got is None and job.kind == browser_pace.LISTING:
        got = {"result": browser_pace.UNREADABLE, "note": why}
    # nor a role in Followup needed's source item (the owner, 2026-11-13): code is its only taker, so a try that found
    # nothing is a not found (three daily tries, then the role is closed as noise: `handback._source`); a try that could
    # not look (Chrome, a time out, a failure of the try) is none: it rests, as a listing's does
    if got is None and job.kind == browser_pace.SOURCE and job.code_only:
        got = {"result": browser_pace.NOT_FOUND, "note": why[:browser_pace.NOTE_MAX]}
        if transient:
            browser_handback.note_tab_failed(job.item_id)
    async with db.session_factory()() as session:
        if got is not None:
            try:
                await browser_handback.hand_back(session, owner, job.item_id, got["result"], url=got.get("url"),
                                   text=got.get("text"), note=got.get("note"), title=got.get("title"),
                                   company=got.get("company"), source_kind=got.get("source_kind"), now=now,
                                   sid=sid, raw=raw, final=bool(got.get("final")))
                await session.commit()
                log.info("browser item %s settled by code: %s", job.item_id, got["result"])
                return True
            except br.BrowserError as exc:
                await session.rollback()
                why = f"the app did not take it: {str(exc)[:120]}"
                if job.kind == browser_pace.LISTING:  # nobody else could take it: a try that found nothing
                    await browser_handback.hand_back(session, owner, job.item_id, browser_pace.UNREADABLE,
                                                     note=why[:browser_pace.NOTE_MAX], now=now, sid=sid)
                    await session.commit()
                    return True
                if job.kind == browser_pace.SOURCE and job.code_only:  # likewise: a counted try
                    await browser_handback.hand_back(session, owner, job.item_id, browser_pace.NOT_FOUND,
                                                     note=why[:browser_pace.NOTE_MAX], now=now, sid=sid)
                    await session.commit()
                    return True
        await browser_door.code_pass_on(session, owner, sid, job.item_id, why, now)
        await session.commit()
    return True


async def code_loop(owner: str, sid: str | None = None) -> None:
    """One code-first worker, for as long as the app runs (`main.lifespan`)."""
    sid = sid or f"{browser_pace.CODE}-{uuid.uuid4().hex[:8]}"
    while True:
        try:
            did = await code_once(owner, sid)
        except asyncio.CancelledError:
            raise
        except Exception:  # a database blip must not end the worker
            log.warning("the code-first worker %s failed a turn", sid, exc_info=True)
            did = False
        if not did:
            await asyncio.sleep(CODE_IDLE_SECONDS)


async def start(owner: str) -> list[asyncio.Task]:
    """Start the code-first workers: what an earlier start's held goes back, and sessions then wait for code to try
    each item first (`browser_pace.code_running`)."""
    async with db.session_factory()() as session:
        await browser_door.code_release_all(session, owner)
        await session.commit()
    browser_pace.code_running(owner, True)
    return [asyncio.create_task(code_loop(owner)) for _ in range(CODE_WORKERS)]


def stop(owner: str) -> None:
    browser_pace.code_running(owner, False)


# --- the sessions' digest ---------------------------------------------------------------------------------------------


#: The whole text of the last digest of each item's page, kept for `hand_back(digest_text=true)`: item id -> (the
#: addresses it answers to, text). Only a text that reads as the whole posting is kept; one entry per item at most.
_digested: dict[str, tuple[tuple[str, ...], str]] = {}
_DIGESTED_MAX = 200


async def digest_text(session: AsyncSession, owner: str, item_id, url=None) -> str:
    """The whole text the session's digest read for the item it holds, for a hand-back that sends it by reference. The
    digest must have been of this item's page (the `link`, or the `url` handed back) and read as whole; the hand-back
    still passes the app's whole-posting check on it."""
    row = await browser_leases._held_item(session, owner, browser_pace._asker())
    if row is None or str(row.id) != squash(item_id):
        raise browser_pace.BrowserWorkError("that is not the item you hold: call browser.next_item")
    kept = _digested.get(str(row.id))
    want = _same(br.web_address(squash(url))) if squash(url) else _same(row.link or "")
    if kept is None or want not in kept[0]:
        raise browser_pace.BrowserWorkError(
            "no whole text from browser.page_digest for that page: send text, copied exactly "
            "(or call browser.page_digest on it first)")
    return kept[1]


async def page_digest(session: AsyncSession, owner: str, url, now: datetime | None = None) -> dict:
    """The door's `browser.page_digest`: the page at `url` loaded in the app's browser, under the domain's pace (the
    session need not call the heartbeat for it), and only its job-relevant content returned (`digest`). Only for a
    session that holds an item, and only a public web address."""
    now = now or datetime.now(UTC)
    row, _ = await browser_leases.touch(session, owner, now)
    if row is None:
        raise browser_pace.BrowserWorkError("browser.page_digest goes with an item you hold: call browser.next_item")
    address = br.web_address(squash(url))
    if not polite.public_url(address):
        raise browser_pace.BrowserWorkError("that is not a public web address")
    wait = await browser_door.page_turn(session, owner, br.domain(address) or row.domain, now)
    # The pace wait and the page load are network time: the call commits first, so its connection goes back to the
    # pool (sessions digesting at once held more than the pool has, and the night's read starved).
    await session.commit()
    if wait:
        await sleep(wait)
    try:
        page = await open_page(address)
        browser_handback.note_tab_opened(str(row.id))
    except BrowserUnavailable as exc:
        if re.search(r"createTarget|could not be opened|not connected", str(exc)):
            # no tab: a not_found or unreadable that follows uses no try (`hand_back`)
            browser_handback.note_tab_failed(str(row.id))
        raise browser_pace.BrowserWorkError(
            f"the app's browser could not load it ({str(exc)[:80]}): open the page in your tab "
            "and read it with take_snapshot") from exc
    hosts = await employer_posting.job_board_hosts(session, owner)
    out = digest(page.html, page.url or address, hosts)
    if out["text_whole"]:
        if len(_digested) >= _DIGESTED_MAX:
            _digested.pop(next(iter(_digested)))
        _digested[str(row.id)] = ((_same(address), _same(page.url or address)), out["text"])
    else:
        _digested.pop(str(row.id), None)
    return {"ok": True, **out, "status": page.status,
            "next": "Use what this gives; open the page in your tab and take a snapshot only when it lacks what the "
                    "item needs (text_whole false, no apply_url where one should be, or a state you must look at)."}
