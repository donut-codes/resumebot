"""Page recipes (the owner, 2026-10-22): a site that draws its listings in the browser and has no data request a recipe
can repeat is read by steps and selectors recorded once, then run every night by code over DevTools in the app's own
Chrome, with no Claude session (`sites/skills.PageRecipe`).

**Running** (`run`). The recipe's start page is loaded (robots.txt asked first) and, for each of the owner's target
terms (`profile.search_phrases`), the term is typed in the recipe's search box, the search is run, and the result
pages are walked by the recipe's next control, the posting anchors read from each. With no search box the page is
read as
it loads. About a request a second (`ACTION_GAP`), an honest agent (the app's own Chrome profile), a refusal (401, 403,
429) or a bot check is a stop (`SkillError` with the status; `recipes.blocked` cools the site); a cookie banner is
neither: it is declined and the page read on (`Tab.go`). Nothing is clicked but the recipe's own search and next
controls and a cookie banner's control that refuses cookies, nothing typed but the term, and a link is kept only when
it is a public address on the start page's own careers host (`on_careers_host`: the rest of the employer's site is not
it) and no sign-in, account, application or consent. A listing found by searching terms is a partial view of the
board, so it is marked truncated and closes nothing. The night's time limit bounds it (docs/PRD.md 28.17): the run is
given the shorter of its own time and what is left of the night (`polite.READ_BY`), and a site whose turn at the one
Chrome does not come before the night's time is out is not begun (`NotBegun`: left for the next read, like a board not
begun).

**Reading a posting** (`read_posting`). The posting's page is opened in the app's Chrome (robots.txt asked first) and
the
recipe's text element read, the page's HTML kept as the raw copy. Used by the browser's read items on the recipe's site
(`browser_code.read_by_code`): the posting is read by code, never by a session.

**Recording** (`record`, from `recipe_record.record_by_code`). In the app's own Chrome, the same finders a visitor's
steps
use (`recipe_interact`: the search box, the next page control) are marked on the live page and each one's selector is
worked out; the posting links are the group an attribute selector on one path word matches whose addresses look most
like single postings (`link_groups`: an id leaf, a posting word; not a team's, a category's or one place's page), a few
of them opened to see they are postings and not pages that list jobs (`_sample`); the posting's text element is the
opened posting's description. The recipe made is run once for real (`verify`); it is kept, live, only when it reads
postings. A recording re-made after two failed nights is the same code. Nothing here is a model's, and nothing a page
says is run: the scripts below read the page and click the recipe's controls. Services flush; the caller commits."""

import asyncio
import collections
import hashlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.adapters import Listing, _posting, stopped_at
from app.jobsearch.browser import code as bc
from app.jobsearch.browser.recipes import interact as recipe_interact
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch.polite import Fetcher, Refused, public_url
from app.jobsearch.profile import store as profile_store
from app.settings import settings
from app.sites import skills, where
from app.sites.models import Site, SiteRecipe, SiteRecording

log = logging.getLogger(__name__)

#: A request a second: the pause between two actions on a site's page. The time a page is given to answer one. How long
#: a page is looked at for the element a step needs before the step is called changed.
ACTION_GAP = 1.0
SETTLE_SECONDS = 3.0
FIND_SECONDS = 12.0
#: The most seconds one site's run may take in all (it then stops, truncated, and says so), and the most result pages
#: one
#: term may walk (a guard against a next control that never ends; a page that repeats ends it sooner).
RUN_SECONDS = 3600.0
PAGES_AT_MOST = 400
#: A recipe is kept only if the real run it is tested with reads at least this many postings.
VERIFY_ROWS = skills.VERIFY_ROWS
#: The least characters a posting's text must have to be a whole description rather than a stub.
TEXT_MIN = 200
#: What a document answered with that is a bot check and not a page (a WAF challenge answers 202 with no body).
CHALLENGE_STATUSES = (202,)
#: One Chrome, one tab at a time: the runs and the readings take turns.
_one_tab = asyncio.Lock()


class NotBegun(Exception):
    """The night's time ran out before this run's turn at the app's one Chrome came: nothing was read, and the site
    waits for the next read (`run`)."""


@asynccontextmanager
async def _turn_at_the_chrome(fetch: Fetcher) -> AsyncIterator[None]:
    """The app's one Chrome (`_one_tab`), waited for no longer than the night's time: past `fetch.deadline` a run that
    has not had its turn is `NotBegun`. A fetcher with no deadline waits for as long as the runs before it take."""
    left = None if fetch.deadline is None else max(fetch.deadline - time.monotonic(), 0.0)
    try:
        await asyncio.wait_for(_one_tab.acquire(), left)
    except TimeoutError:
        raise NotBegun("the night's time ran out before this site's turn at the browser") from None
    try:
        yield
    finally:
        _one_tab.release()


def make_tools() -> Any:
    """The app's Chrome over DevTools (tests replace this)."""
    return bc._DevTools(settings.bridge_port(), "code:page")


# --- the scripts ----------------------------------------------------------------------------------------------
# Each starts with a marker comment and the call's arguments as one line of JSON, so a script says what it is and
# what it
# was given, and the code that runs it never builds one from a page's words.


def _script(name: str, args: list, body: str) -> str:
    return f"/*rb:{name}*/ //{json.dumps(args)}\n(() => {{ const A = {json.dumps(args)}; {body} }})()"


_SEEN = ("const seen = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); "
         "if (s.visibility === 'hidden' || s.display === 'none') return false; "
         "return (r.width > 0 && r.height > 0) || (el.innerText || '').trim().length > 0; }; ")


def has(selector: str) -> str:
    """Whether an element is on the page, shown and not disabled."""
    return _script("has", [selector], _SEEN + "let el = null; try { el = document.querySelector(A[0]); } catch (e) {} "
                   "return JSON.stringify(!!el && seen(el) && !el.disabled && "
                   "el.getAttribute('aria-disabled') !== 'true');")


def type_in(selector: str, word: str) -> str:
    """Type one word in a box, the way a visitor does (the page hears an input and a change)."""
    return _script("type", [selector, word],
                   "const el = document.querySelector(A[0]); "
                   "if (!el || el.tagName !== 'INPUT') return JSON.stringify({typed: false}); el.focus(); "
                   "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, A[1]); "
                   "for (const t of ['input', 'change']) el.dispatchEvent(new Event(t, {bubbles: true})); "
                   "return JSON.stringify({typed: true});")


def click(selector: str, box: str | None = None) -> str:
    """Click a control. With `box` (the search box) and no control, run the box's own search: the button near it, its
    form's submit, or Enter."""
    return _script("click", [selector, box],
                   "const el = document.querySelector(A[0]); if (!el) return JSON.stringify({clicked: false}); "
                   "if (el.tagName === 'A') el.target = '_self'; "
                   "if (A[1] && el.tagName === 'INPUT') { el.focus(); const form = el.form; "
                   "const scope = form || el.closest('div, section, header'); "
                   "const near = scope && [...scope.querySelectorAll('button, input[type=submit], [role=button]')]"
                   ".find((b) => /search|find|go|submit/i.test((b.innerText || '') + ' ' + "
                   "(b.getAttribute('aria-label') || '') + ' ' + (b.value || ''))); "
                   "if (near) near.click(); else if (form && form.requestSubmit) form.requestSubmit(); "
                   "else { for (const t of ['keydown', 'keypress', 'keyup']) el.dispatchEvent(new KeyboardEvent(t, "
                   "{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true})); } } else el.click(); "
                   "return JSON.stringify({clicked: true});")


def rows_of(links: str, row: str | None, location: str | None) -> str:
    """The posting anchors on the page: address, words and, inside the row, the place."""
    return _script("rows", [links, row, location],
                   "const out = []; for (const a of document.querySelectorAll(A[0])) { "
                   "if (!a.href || !/^https?:/i.test(a.href)) continue; "
                   "let title = ((a.innerText || a.getAttribute('aria-label') || a.title || '') + '').trim(); "
                   "title = title.split('\\n').map((t) => t.trim()).filter(Boolean)[0] || ''; "
                   "let place = null; if (A[1] && A[2]) { const r = a.closest(A[1]); "
                   "const p = r && r.querySelector(A[2]); place = p ? (p.innerText || '').trim() : null; } "
                   "out.push({href: a.href, title: title.slice(0, 300), "
                   "location: place ? place.slice(0, 200) : null}); "
                   "if (out.length >= 2000) break; } return JSON.stringify(out);")


def text_of(selector: str) -> str:
    return _script("text", [selector], "const el = document.querySelector(A[0]); "
                   "return JSON.stringify({text: el ? (el.innerText || '').trim() : ''});")


_HTML = ("/*rb:html*/ //[]\nJSON.stringify({url: location.href, "
         f"html: document.documentElement.outerHTML.slice(0, {bc.PAGE_MAX_CHARS})}})")


# --- driving one tab --------------------------------------------------------------------------------------------------


class Tab:
    """One tab of the app's Chrome, with what the page asked for noted so a refusal is seen."""

    def __init__(self, tools: Any, sid: str) -> None:
        self.tools, self.sid = tools, sid
        self._reported: set[str] = set()

    async def value(self, expression: str) -> Any:
        got = await self.tools.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, self.sid)
        value = (got.get("result") or {}).get("value")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    def stop(self, host: str) -> int | None:
        """The status of a refusal among the requests made since the last look (`recipe_interact.refused`)."""
        fresh = [r for r in recipe_record.assemble(self.tools.events or []) if r["request_id"] not in self._reported]
        self._reported.update(r["request_id"] for r in fresh if r["status"] is not None)
        return recipe_interact.refused(fresh, host)

    async def settle(self, seconds: float | None = None) -> None:
        await recipe_record._settle(self.tools, self.sid, SETTLE_SECONDS if seconds is None else seconds)

    async def go(self, url: str) -> int | None:
        """Load a page and let it draw; the status of a refusal or of a bot check, or None. A cookie banner the page
        shows is no stop: its control that refuses cookies is clicked (`recipe_interact.decline_consent`), and the page
        is read on."""
        await self.tools.call("Page.navigate", {"url": url}, self.sid)
        await recipe_record._wait_loaded(self.tools, self.sid)
        await self.settle()
        if await recipe_interact.decline_consent(self.tools, self.sid):
            await self.settle()
        if (status := self.stop(urlsplit(url).hostname or "")) is not None:
            return status
        return self.challenged()

    def challenged(self) -> int | None:
        """The status of a page the site answered with a bot check instead of its page (HTTP 202 for a document, as an
        AWS WAF challenge does): a stop like a refusal, never worked around (the owner: nothing solves a bot check)."""
        for r in recipe_record.assemble(self.tools.events or []):
            if r.get("kind") == "Document" and r.get("status") in CHALLENGE_STATUSES:
                return int(r["status"])
        return None

    async def wait_for(self, selector: str) -> bool:
        loop = asyncio.get_running_loop()
        until = loop.time() + FIND_SECONDS
        while True:
            if await self.value(has(selector)) is True:
                return True
            if loop.time() >= until:
                return False
            await bc.sleep(1.0)


@asynccontextmanager
async def tab(tools: Any = None) -> AsyncIterator[Tab]:
    """A fresh tab of the app's own Chrome, closed when done. Raises `browser_code.BrowserUnavailable`."""
    tools = tools or make_tools()
    tools.events = []
    target = None
    try:
        await tools.open()
        target, sid = await bc.open_own_tab(tools)
        await tools.call("Network.enable", {}, sid)
        yield Tab(tools, sid)
    except (OSError, asyncio.IncompleteReadError, KeyError, ValueError) as exc:
        raise bc.BrowserUnavailable(str(exc) or type(exc).__name__) from exc
    finally:
        if target is not None:
            try:
                await asyncio.shield(tools.call("Target.closeTarget", {"targetId": target}))
            except Exception:  # the bridge closes the tab when the connection goes anyway
                pass
        await tools.close()


# --- running a recipe -------------------------------------------------------------------------------------------------


#: What a posting link's address may never hold as a whole path part, and what its words may never be on their own: a
#: sign-in, an account, an application, a consent (a job titled "Account Manager" is a posting, not an account page).
NEVER_SEGMENTS = {"login", "signin", "sign-in", "logout", "register", "signup", "sign-up", "account", "accounts",
                  "apply", "application", "applications", "password", "cookie", "cookies", "consent", "privacy",
                  "terms", "subscribe", "newsletter", "alert", "alerts", "captcha"}
NEVER_TITLE = (r"(sign[\s-]*in|log[\s-]*in|sign[\s-]*up|register|apply( now)?|create (an )?account|my account|"
               r"privacy( policy)?|terms( of (use|service))?|cookies?( settings)?|subscribe|job alerts?)")


def careers_host(url: str) -> str:
    """The host a recipe's start page is on, without a leading `www.` (that is the employer's whole site, so its
    subdomains are in it)."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def on_careers_host(href: str, base: str) -> bool:
    """Whether a link stays on the careers host of the start page `base`: that host or one under it (a careers site's
    `apply.` host). A link to the rest of the employer's site leaves it."""
    host, home = (urlsplit(href).hostname or "").lower(), careers_host(base)
    return bool(home) and (host == home or host.endswith("." + home))


def ext_id_of(href: str) -> str:
    """A posting's id from its address: the path, with the query that names it (tracking words left out)."""
    parts = urlsplit(href)
    tracking = re.compile(r"(utm_|ref$|source$|src$|gh_src$|lang$)", re.I)
    keep = [(k, v) for k, v in parse_qsl(parts.query) if not tracking.match(k)]
    out = parts.path + (f"?{urlencode(keep)}" if keep else "")
    return out if 0 < len(out) <= 200 else hashlib.sha1(href.encode()).hexdigest()[:20]


#: The posting shape.
#: A single posting's address names one: a path part that is a posting word (`/job/`, `/job_details/`), or an id
#: (a GUID, a number of four digits or more, a long code with digits) in its path or query while no part names a hub
#: (a team, a category, a place: a page that lists jobs). Migration 0120 holds a frozen copy of this rule.
POSTING_WORDS = frozenset({"job", "job_details", "job-details", "jobdetails", "jobdetail", "job-detail", "position",
                           "requisition", "req", "posting", "opening", "vacancy"})
#: Plurals that name a list of postings unless ids follow them (`/jobs/` is a search, `/jobs/12345` a posting).
POSTING_PLURALS = frozenset({"jobs", "positions", "requisitions", "reqs", "postings", "openings", "vacancies"})
HUB_WORDS = frozenset({"team", "teams", "job-family", "job-families", "jobfamily", "jobfamilies", "family",
                       "families", "category", "categories", "department", "departments", "location", "locations",
                       "area", "areas", "function", "functions", "search", "results", "benefits", "culture", "life",
                       "students", "events", "blog", "news", "about", "stories", "story", "landing_pages",
                       "how-we-hire", "hiring-process"})
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _parts(href: str) -> list[str]:
    return [p for p in urlsplit(href).path.split("/") if p]


def is_id(part: str) -> bool:
    """Whether a path part or query value is an id: a GUID, four digits in a row, or a long code with three digits."""
    return bool(_GUID.search(part) or re.search(r"\d{4,}", part)
                or (len(part) >= 10 and re.fullmatch(r"[0-9A-Za-z]+", part) and sum(c.isdigit() for c in part) >= 3))


#: A query key that names a posting's id (`?jobId=`, `?gh_jid=`, `?id=`, `?req=`); another key's number is a filter's
#: (a category's search, `?listFilterMode=true&11760[0]=27877240`), not a posting's.
_ID_KEY = re.compile(r"(^|_)(id|jid|job|jobid|job_id|req|reqid|requisition|requisitionid|posting|postingid|pid)$", re.I)


def _query_id(href: str) -> bool:
    return any(_ID_KEY.search(k) and is_id(v) for k, v in parse_qsl(urlsplit(href).query))


def has_id(href: str) -> bool:
    return any(is_id(p) for p in _parts(href)) or _query_id(href)


def posting_shaped(href: str) -> bool:
    """Whether an address looks like one posting's page (a `POSTING_WORDS` part, a posting's id in its query, or an id
    in its path with no `HUB_WORDS` part), not a page that lists jobs or describes the employer."""
    words = {p.lower() for p in _parts(href)}
    if words & POSTING_WORDS or _query_id(href):
        return True
    return any(is_id(p) for p in _parts(href)) and not words & HUB_WORDS


def _kept(row: dict, base: str) -> dict | None:
    """A row of the page as a posting row, or None when it is no posting of this site: off its careers host, a sign-in
    or an application, or an address that is not posting-shaped (`posting_shaped`), at recording and at every read."""
    href = str(row.get("href") or "").split("#", 1)[0]
    title = " ".join(str(row.get("title") or "").split())
    if not title or not public_url(href) or not on_careers_host(href, base) or not posting_shaped(href):
        return None
    segments = {p.lower() for p in re.split(r"[/_.]+", urlsplit(href).path) if p}
    if segments & NEVER_SEGMENTS or re.fullmatch(NEVER_TITLE, title, re.I):
        return None
    place = " ".join(str(row.get("location") or "").split()) or None
    return {"href": href, "title": title, "location": place}


async def _terms_run(t: Tab, recipe: skills.PageRecipe, word: str | None, found: dict[str, dict], loop,
                     until: float, enough: int | None = None) -> tuple[str | None, int]:
    """Search one term (or read the page as it loads) and walk its pages. Returns why it stopped early (or None) and how
    many pages were read. With `enough`, it stops as soon as that many postings are found (a test of a recipe needs
    no more). Raises SkillError on a refusal or a changed shape."""
    host = urlsplit(recipe.url).hostname or ""
    if recipe.search:
        if not await t.wait_for(recipe.search):
            raise skills.SkillError("the search box the recipe types in is not on the page any more")
        if word is not None:
            if (await t.value(type_in(recipe.search, word))) != {"typed": True}:
                raise skills.SkillError("the search box could not be typed in")
        await bc.sleep(ACTION_GAP)
        t.stop(host)
        if recipe.submit:
            clicked = await t.value(click(recipe.submit))
        else:
            clicked = await t.value(click(recipe.search, recipe.search))
        if not (isinstance(clicked, dict) and clicked.get("clicked")):
            raise skills.SkillError("the search could not be run")
        await t.settle()
        if (status := t.stop(host)) is not None:
            raise skills.SkillError(f"{host} answered {status}", status)
    pages = 0
    while True:
        pages += 1
        got = await t.value(rows_of(recipe.links, recipe.row, recipe.location))
        before = len(found)
        listed = 0
        for raw in got if isinstance(got, list) else []:
            row = _kept(raw, recipe.url)
            if row is None:
                continue
            listed += 1
            found.setdefault(row["href"], row)
        if enough is not None and len(found) >= enough:
            return None, pages
        if not recipe.next or not await t.value(has(recipe.next)):
            return None, pages
        if listed and len(found) == before:
            return stopped_at(len(found), "a page repeated one already read"), pages
        if loop.time() >= until:
            return stopped_at(len(found), "the time allowed for the read ran out"), pages
        if pages >= PAGES_AT_MOST:
            return stopped_at(len(found), f"{PAGES_AT_MOST} pages were read"), pages
        await bc.sleep(ACTION_GAP)
        t.stop(host)
        clicked = await t.value(click(recipe.next))
        if not (isinstance(clicked, dict) and clicked.get("clicked")):
            return None, pages
        await t.settle()
        if (status := t.stop(host)) is not None:
            raise skills.SkillError(f"{host} answered {status}", status)


async def run(recipe: skills.PageRecipe, terms: list[str], *, seconds: float = RUN_SECONDS,
              enough: int | None = None) -> Listing:
    """Read the site by its page recipe: each term searched and its pages walked (or the page read as it loads when the
    recipe has no search box). Raises `Refused` when robots.txt disallows the start, `SkillError` on a refusal or a
    changed shape, `BrowserUnavailable` when the app's Chrome is not there, `NotBegun` when the night's time (the
    fetcher's deadline: `polite.READ_BY`) runs out before the run's turn at the Chrome comes. The run is given
    `seconds`, or what is left of the night's time when that is less. With `enough`, the read stops once that many
    postings are found (`verify`: a recording's test run must fit the code-first try's time)."""
    words: list[str | None] = [w for w in (recipe_interact.clean_keyword(x) for x in terms) if w] \
        if recipe.search else [None]
    if recipe.search and not words:
        words = [None]  # no terms: the search box is run empty, as a visitor looks at all the jobs
    fetch = Fetcher()
    try:
        if not await fetch.allows(recipe.url):
            raise Refused(f"robots.txt asks not to read {recipe.url}")
        found: dict[str, dict] = {}
        stopped: str | None = None
        async with _turn_at_the_chrome(fetch):
            loop = asyncio.get_running_loop()
            left = seconds if fetch.deadline is None else min(seconds, fetch.deadline - time.monotonic())
            until = loop.time() + left
            async with tab() as t:
                for i, word in enumerate(words):
                    if i:
                        await bc.sleep(ACTION_GAP)
                    if enough is not None and len(found) >= enough:
                        break
                    if loop.time() >= until:
                        stopped = stopped_at(len(found), "the time allowed for the read ran out")
                        break
                    if (status := await t.go(recipe.url)) is not None:
                        raise skills.SkillError(f"{urlsplit(recipe.url).hostname} answered {status}", status)
                    why, _pages = await _terms_run(t, recipe, word, found, loop, until, enough)
                    stopped = stopped or why
    finally:
        await fetch.close()
    postings: list[dict] = [
        _posting(raw=row, ext_id=ext_id_of(row["href"]), title=row["title"], location=row["location"],
                 url=row["href"], apply_url=row["href"]) for row in found.values()]
    searched = [w for w in words if w]
    note = "; ".join(x for x in (stopped, f"read for {len(searched)} search terms: {', '.join(searched)}"[:300]
                                 if searched else None) if x) or None
    return Listing(postings, truncated=bool(searched) or stopped is not None, note=note)


async def read_posting(recipe: skills.PageRecipe, link: str, turn=None) -> dict | None:
    """A posting's text by the recipe: the page opened in the app's Chrome and its text element read. {text, raw} (raw
    is the page's HTML), or None when the recipe has no text element, the element is not there or holds too little.
    Raises `Refused` when robots.txt disallows the page, `SkillError` on a refusal."""
    if not recipe.text or not public_url(link) or not on_careers_host(link, recipe.url):
        return None  # a link that leaves the careers host is no posting of the recipe's site
    fetch = Fetcher()
    try:
        if not await fetch.allows(link):
            raise Refused(f"robots.txt asks not to read {link}")
    finally:
        await fetch.close()
    if turn is not None:
        await turn()
    async with _one_tab, tab() as t:
        if (status := await t.go(link)) is not None:
            raise skills.SkillError(f"{urlsplit(link).hostname} answered {status}", status)
        if not await t.wait_for(recipe.text):
            return None
        got = await t.value(text_of(recipe.text))
        text = (got.get("text") if isinstance(got, dict) else "") or ""
        page = await t.value(_HTML)
    if len(text) < TEXT_MIN:
        return None
    html = str(page.get("html") or "") if isinstance(page, dict) else ""
    return {"text": text, "raw": html.encode("utf-8")}


# --- recording --------------------------------------------------------------------------------------------------------

_SELECTOR_OF = """
const marked = document.querySelector('[%(mark)s]'); if (!marked) return JSON.stringify({selector: null});
const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(v) : v.replace(/[^\\w-]/g, '');
const unique = (s) => { try { return document.querySelectorAll(s).length === 1; } catch (e) { return false; } };
const tag = marked.tagName.toLowerCase();
const tries = [];
if (marked.id) tries.push('#' + esc(marked.id));
for (const a of ['data-testid', 'data-test', 'data-automation-id', 'aria-label', 'name', 'placeholder', 'title',
    'type']) {
  const v = marked.getAttribute(a);
  if (v && v.length < 80 && !/["\\\\]/.test(v)) tries.push(tag + '[' + a + '="' + v + '"]');
}
for (const c of [...marked.classList].filter((c) => /^[A-Za-z][\\w-]{2,40}$/.test(c) && !/\\d{3,}/.test(c))) {
  tries.push(tag + '.' + c);
}
for (const t of tries) { if (unique(t)) return JSON.stringify({selector: t}); }
const path = []; let el = marked;
while (el && el.nodeType === 1 && path.length < 6) {
  let part = el.tagName.toLowerCase();
  if (el.id) { path.unshift('#' + esc(el.id)); break; }
  const same = [...el.parentNode.children].filter((c) => c.tagName === el.tagName);
  if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
  path.unshift(part); el = el.parentNode;
  if (unique(path.join(' > '))) break;
}
const s = path.join(' > '); return JSON.stringify({selector: unique(s) ? s : null});
""".replace("%(mark)s", recipe_interact.MARK)

#: The page's links on its own site, each with its address (`href` absolute, `raw` as written, which a CSS attribute
#: selector matches), its words and the row that holds it. The grouping is done in Python (`link_groups`).
_ANCHORS = """
const NEVER = new RegExp(A[0], 'i');
const home = location.hostname.toLowerCase().replace(/^www\\./, '');
const plain = (c) => /^[A-Za-z][\\w-]{2,40}$/.test(c) && !/\\d{3,}/.test(c);
const out = [];
for (const a of document.querySelectorAll('a[href]')) {
  const t = (a.innerText || '').trim(); let same = false;
  try { const h = new URL(a.href).hostname.toLowerCase(); same = h === home || h.endsWith('.' + home); }
  catch (e) {}
  if (!same || t.length < 6 || t.length > 160 || NEVER.test(a.href) || NEVER.test(t)) continue;
  const row = a.closest('li, tr, article'); let rowSel = null;
  if (row) { const cls = [...row.classList].find(plain);
    rowSel = cls ? row.tagName.toLowerCase() + '.' + cls : row.tagName.toLowerCase(); }
  out.push({href: a.href, raw: a.getAttribute('href') || '', title: t.split('\\n')[0].slice(0, 160), row: rowSel});
  if (out.length >= 3000) break;
}
return JSON.stringify(out);
"""

#: The fewest links a group of posting links has; how many groups are tried, and how many of a group's links are
#: opened to see they are postings; how many posting-shaped links make an opened page a list of jobs.
MIN_GROUP = 3
CANDIDATES = 3
SAMPLES = 2
HUB_LINKS = 5
_TOKEN = re.compile(r"[A-Za-z0-9._~%-]{2,60}")


def _matches(selector: str, raw: str) -> bool:
    """What the browser's `a[href*="v"]` / `a[href$="v"]` matches, on the attribute as written."""
    got = re.fullmatch(r'a\[href([*$])="([^"]+)"\]', selector)
    if not got:
        return False
    return got.group(2) in raw if got.group(1) == "*" else raw.endswith(got.group(2))


def _mask(href: str) -> tuple[bool, ...]:
    return tuple(is_id(p) for p in _parts(href))


def link_groups(anchors: list[dict]) -> list[dict]:
    """The page's groups of links that may be its postings, the likeliest first: {selector, row, count, members}.
    Each group is the links an attribute selector built on one path word matches. A group is likelier the more of its
    links are posting-shaped (an id leaf, a posting word: `/job_details/123`, `/<place>/<slug>/<GUID>/job`), and much
    less likely when its word names a hub (`/teams/`, `/job-family/`) or pins one value of a part that varies among
    links of the same shape (one town of many: the common word is chosen over it)."""
    rows: dict[str, dict] = {}
    for a in anchors if isinstance(anchors, list) else []:
        if isinstance(a, dict) and isinstance(a.get("href"), str) and a["href"].startswith("http"):
            rows.setdefault(a["href"].split("#", 1)[0], {**a, "raw": str(a.get("raw") or a["href"])})
    links = list(rows.values())
    selectors: dict[str, str] = {}
    for r in links:
        for part in _parts(r["href"]):
            if is_id(part) or part.lower() in NEVER_SEGMENTS or not _TOKEN.fullmatch(part):
                continue
            selectors.setdefault(f'a[href*="/{part}/"]', part)
            selectors.setdefault(f'a[href$="/{part}"]', part)
    groups: dict[frozenset, dict] = {}
    for selector, word in selectors.items():
        members = [r for r in links if _matches(selector, r["raw"])]
        if len(members) < MIN_GROUP:
            continue
        low = word.lower()
        ids = sum(has_id(m["href"]) for m in members) / len(members)
        named = low in POSTING_WORDS or (low in POSTING_PLURALS and ids >= 0.5)
        weight = (1 + 3 * ids) * (3 if named else 1) * (0.1 if low in HUB_WORDS else 1)
        if _pins(word, members, links):
            weight *= 0.3
        rows_seen = collections.Counter(m.get("row") for m in members if m.get("row"))
        group = {"selector": selector, "row": rows_seen.most_common(1)[0][0] if rows_seen else None,
                 "count": len(members), "members": members, "score": len(members) * weight, "named": named}
        key = frozenset(m["href"] for m in members)
        held = groups.get(key)
        if held is None or (group["score"], group["named"], -len(selector)) > (
                held["score"], held["named"], -len(held["selector"])):
            groups[key] = group
    return sorted(groups.values(), key=lambda g: (-g["score"], not g["named"], len(g["selector"])))


def _pins(word: str, members: list[dict], links: list[dict]) -> bool:
    """Whether `word` is one value of a path part that varies: a link of the same shape outside the group has another
    word where the group's links have this one (`/springdale-xx/<slug>/<id>/job` beside `/riverton-yy/...`)."""
    inside = {m["href"] for m in members}
    for m in members:
        parts = _parts(m["href"])
        if word not in parts:
            continue
        at, mask = parts.index(word), _mask(m["href"])
        for other in links:
            if other["href"] in inside or _mask(other["href"]) != mask:
                continue
            if _parts(other["href"])[at] != word:
                return True
    return False


def is_hub(link: str, anchors: Any) -> bool:
    """Whether an opened link is a page that lists jobs (a team's, a category's or a place's page) and not a posting:
    its own address names no id (`/job/engineering`, `/teams/design`) and it links to many posting-shaped addresses."""
    if has_id(link):
        return False
    hrefs = {str(a.get("href") or "").split("#", 1)[0] for a in anchors if isinstance(a, dict)} \
        if isinstance(anchors, list) else set()
    return sum(1 for h in hrefs if h != link and posting_shaped(h)) >= HUB_LINKS

_DESCRIPTION = """
const cands = ['[itemprop=description]', '[data-automation-id*=description i]', '[class*=job-description i]',
  '[class*=jobdescription i]', '[class*=description i]', '[id*=description i]', 'article', 'main', '[role=main]'];
for (const s of cands) { const el = document.querySelector(s);
  if (el && (el.innerText || '').trim().length >= A[0]) return JSON.stringify({selector: s}); }
return JSON.stringify({selector: null});
"""


async def _mark(t: Tab, step: str) -> dict | None:
    """The element a visitor's step would use (`recipe_interact`'s finders), marked in the page, nothing clicked."""
    return await recipe_interact.find(t.tools, t.sid, step)


def _marked_tag() -> str:
    return _script("tag", [], f"const el = document.querySelector('[{recipe_interact.MARK}]'); "
                   "return JSON.stringify(el ? el.tagName : '');")


async def _selector(t: Tab) -> str | None:
    got = await t.value(f"(() => {{ {_SELECTOR_OF} }})()")
    sel = got.get("selector") if isinstance(got, dict) else None
    try:
        return skills.css(sel, "selector") if sel else None
    except ValueError:
        return None


async def _sample(t: Tab, group: dict, base: str) -> tuple[bool, str | None, int | None]:
    """Open a few of a group's links (robots.txt asked first, a request a second) and see each is a posting, not a page
    that lists jobs (`is_hub`). (are they postings, the posting text element found, the status of a refusal). A group
    with no posting-shaped link is no group of postings, and one whose links could not be opened is taken on its
    shape."""
    links = [k["href"] for k in (_kept(m, base) for m in group["members"]) if k][:SAMPLES]
    if not links:
        return False, None, None
    text: str | None = None
    for link in links:
        fetch = Fetcher()
        try:
            allowed = await fetch.allows(link)
        finally:
            await fetch.close()
        if not allowed:
            continue
        await bc.sleep(ACTION_GAP)
        if (status := await t.go(link)) is not None:
            return False, None, status
        if is_hub(link, await t.value(_script("anchors", [recipe_interact.NEVER], _ANCHORS))):
            return False, None, None
        if text is None:
            got = await t.value(_script("description", [TEXT_MIN], _DESCRIPTION))
            text = got.get("selector") if isinstance(got, dict) and got.get("selector") else None
    return True, text, None


async def propose(url: str, term: str | None) -> tuple[dict | None, str | None, int | None]:
    """Steps recorded on the live site: the recipe they make, why none could be made, or the status of a refusal. The
    finders are a visitor's (`recipe_interact`), the page is loaded once and the search run once with `term`."""
    fetch = Fetcher()
    try:
        if not await fetch.allows(url):
            return None, "robots.txt asks not to read the page", None
    finally:
        await fetch.close()
    recipe: dict[str, Any] = {"kind": "page", "url": url}
    async with _one_tab, tab() as t:
        if (status := await t.go(url)) is not None:
            return None, None, status
        here = await recipe_interact.snapshot(t.tools, t.sid)
        if here is not None and recipe_record.looks_walled(here.html):
            return None, None, recipe_record.BOT_WALL_STATUS
        host = urlsplit(url).hostname or ""
        box = await _mark(t, "search")
        if box is not None and box.get("what") == "search box" and not box.get("href"):
            # The finder may mark a button ("Search jobs") as the search box; only a box that takes typing is one.
            is_box = await t.value(_marked_tag()) == "INPUT"
            if is_box and (sel := await _selector(t)):
                typed = term is None or await t.value(type_in(sel, term)) == {"typed": True}
                if typed:
                    recipe["search"] = sel
                if term and typed:
                    await bc.sleep(ACTION_GAP)
                    t.stop(host)
                    await t.value(click(sel, sel))
                    await t.settle()
                    if (status := t.stop(host)) is not None:
                        return None, None, status
        groups = link_groups(await t.value(_script("anchors", [recipe_interact.NEVER], _ANCHORS)))
        if not groups:
            await bc.sleep(FIND_SECONDS / 4)
            groups = link_groups(await t.value(_script("anchors", [recipe_interact.NEVER], _ANCHORS)))
        if not groups:
            return None, "no group of posting links could be told apart on the page", None
        if await _mark(t, "next") is not None and (sel := await _selector(t)):
            recipe["next"] = sel
        chosen = None
        for group in groups[:CANDIDATES]:
            postings, text, status = await _sample(t, group, url)
            if status is not None:
                return None, None, status
            if postings:
                chosen = group
                if text:
                    recipe["text"] = text
                break
        if chosen is None:
            return None, ("the links on the page lead to pages that list jobs (a team's, a category's or a place's), "
                          "not to single postings"), None
        recipe["links"] = chosen["selector"]
        if chosen.get("row"):
            recipe["row"] = chosen["row"]
    try:
        skills.parse_recipe(recipe)
    except ValueError as exc:
        return None, f"the steps recorded make no valid recipe ({exc})", None
    return recipe, None, None


async def terms_of(session: AsyncSession, owner: str) -> list[str]:
    """The owner's own target terms, the phrases a site that needs a typed search is searched with."""
    return (await profile_store.current(session, owner)).criteria.search_phrases()


async def verify(recipe: dict, terms: list[str]) -> tuple[int, str | None, int | None]:
    """Run the recipe once for real with the first term, until `VERIFY_ROWS` postings are read (the test needs no
    more, and a whole walk of a big site does not fit the code-first try's time): (postings read, why not, status of a
    refusal)."""
    try:
        listing = await run(skills.parse_page(recipe), terms[:1], enough=VERIFY_ROWS)
    except skills.SkillError as exc:
        return 0, str(exc), exc.status
    except Refused as exc:
        return 0, str(exc), None
    return len(listing.postings), None, None


async def keep(session: AsyncSession, owner: str, rec: SiteRecording, recipe: dict, rows: int, now: datetime,
               ) -> SiteRecipe:
    """The verified recipe as the site's next version, live; the version before it is retired. A site that is off and
    that nobody has decided about is switched on by it (`where.runbook_saved`)."""
    recipe = {**recipe, "provenance": {"recorded_at": now.isoformat(), "recording_sha256": "", "model": None,
                                       "prompt_version": "page-1", "tested_on": rows}}
    parsed = skills.parse_recipe(recipe)
    last = await session.scalar(select(func.max(SiteRecipe.version)).where(SiteRecipe.site_id == rec.site_id))
    for old in (await session.scalars(select(SiteRecipe).where(
            SiteRecipe.site_id == rec.site_id, SiteRecipe.retired_at.is_(None)))).all():
        old.state, old.retired_at = "retired", now
    row = SiteRecipe(owner=owner, site_id=rec.site_id, version=(last or 0) + 1,
                     recipe=parsed.model_dump(mode="json", by_alias=True), recording_id=rec.id, state="live",
                     created_at=now, bad_nights=0, health=[])
    session.add(row)
    rec.state, rec.recorded_at = "written", now
    await session.flush()
    await where.runbook_saved(session, rec.site_id)
    return row


async def is_page_site(session: AsyncSession, site_id) -> bool:
    """Whether the site's recipe in force, or the last it had that was not retired, is a page recipe (a re-record
    makes another). A retired version is history: a site whose page recipe was retired is recorded as an API site."""
    last = await session.scalar(select(SiteRecipe).where(SiteRecipe.site_id == site_id, SiteRecipe.state != "retired")
                                .order_by(SiteRecipe.version.desc()).limit(1))
    return last is not None and (last.recipe or {}).get("kind") == "page"


async def record_by_code(owner: str, rec_id) -> dict:
    """The code-first worker's `record` item for a page recipe: record the steps on the live site, run the recipe
    once for real, keep it when it reads postings. Returns a hand-back's fields (`finished` or `blocked`, a note)."""
    from app.jobsearch.browser import pace as browser_pace

    async with db.session_factory()() as session:
        rec = await session.get(SiteRecording, rec_id)
        site = await session.get(Site, rec.site_id) if rec is not None and rec.site_id else None
        if rec is None or site is None or rec.state != "requested":
            return {"result": browser_pace.FINISHED, "note": "no recording was asked for any more"}
        terms = await terms_of(session, owner)
        url = site.url
    recipe, why, status = await propose(url, terms[0] if terms else None)
    rows = 0
    if recipe is not None:
        rows, why, status = await verify(recipe, terms)
        if status is None and why is None and rows < VERIFY_ROWS:
            why = f"the steps recorded read {rows} postings when run for real (at least {VERIFY_ROWS} are needed)"
    async with db.session_factory()() as session:
        rec = await session.get(SiteRecording, rec_id)
        if rec is None or rec.state != "requested":
            return {"result": browser_pace.FINISHED, "note": "the request was answered already"}
        if status is not None:
            return {"result": browser_pace.BLOCKED, "url": url,
                    "note": f"the page answered {status} or showed a bot check: a block, so the site rests"}
        if recipe is None or why is not None:
            rec.state, rec.note = "failed", (why or "no steps could be recorded")[:300]
            await session.commit()
            return {"result": browser_pace.FINISHED, "note": rec.note}
        await keep(session, owner, rec, recipe, rows, datetime.now(UTC))
        await session.commit()
    return {"result": browser_pace.FINISHED, "note": f"a page recipe was recorded and read {rows} postings"}
