"""Working a listing page the way a visitor would, while a recording is made (`recipe_record.capture_page`).

A site that renders its first results on the server and calls its search API only when a visitor interacts shows a
passive load nothing. So the recording's tab, in the app's own Chrome, does a handful of things a visitor does on a
public page, once each and in this order, and then stops: run the site's own job search (its search box, first left as
it is, an empty query; only when that brings no results does one keyword from the owner's own target roles go in, given
by the caller, never a word in code), go to the next page of results, apply one filter, and open one job.

Bounds. At most `ACTIONS` actions and `INTERACT_SECONDS` seconds in all, `ACTION_GAP` seconds between two actions
(about a request a second), and a step that is not there is skipped. Each step first *finds* its element in the page
(a small script that clicks nothing and marks the element), and the code vets what it found before anything is
clicked: a link goes to a public address on the page's own host, robots.txt allows it, and it is not a sign-in, a
registration, an account or an application. Nothing is typed but that one keyword, in the search box. No form is
submitted but the site's search box, none that asks for a password, and no cookie or consent control, newsletter or
alert is touched, except one: a cookie banner is declined, once, before the steps (`decline_consent`; the owner,
2026-11-04: deny the tracking cookies and move on), by clicking only its control that refuses non-essential cookies
("Reject all", "Decline", "Necessary only"), never one that accepts, and never counted among the actions. A banner with
no such control is left, and the steps go on under it. After each action the
traffic is looked at: a refusal (401, 403 or 429 on a page, 429 on any request to the host) is a stop for the whole
recording.

What the page's data requests were answered is read by the caller from the same Network events as before; what this
module adds is each step's name and what the page's HTML held afterwards (`Snapshot`), for the fallback that reads
data embedded in the page. Nothing here is a model's: the steps are code, and the scripts below read the page, never
run what the page says."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.jobsearch import consent
from app.jobsearch.browser import code as bc
from app.jobsearch.fetch.polite import public_url
from app.sites import registry

#: The most actions, the most seconds, the pause between two actions, and how long the page is given to answer one.
ACTIONS = 4
INTERACT_SECONDS = 90.0
ACTION_GAP = 1.0
ACTION_SETTLE = 4.0
#: A page that builds its results in the browser is given this long (looked at once a second) to show the element a step
#: looks for; the looking is the browser's own page, never a request to the site.
FIND_WAIT = 10.0
#: The most of a page's HTML kept for reading embedded data.
HTML_MAX = 8_000_000
STEPS = ("search", "next", "filter", "open")
MARK = "data-rb-pick"
BLOCKED_STATUSES = (401, 403, 429)
#: What a link or control may never be: an account, a sign-in, an application, a consent or a subscription.
NEVER = (r"sign[\s-]*in|log[\s-]*in|login|sign[\s-]*up|register|account|password|apply|application|cookie|consent|"
         r"privacy|newsletter|subscribe|alert|terms|remember|captcha")

_PRELUDE = f"""
const NEVER = new RegExp({json.dumps(NEVER)}, 'i');
const seen = (el) => {{ const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
  if (s.visibility === 'hidden' || s.display === 'none' || el.closest('[hidden], [aria-hidden=true]')) return false;
  return (r.width > 0 && r.height > 0) || (el.innerText || '').trim().length > 0; }};
const said = (el) => [el.innerText, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('name'),
  el.getAttribute('placeholder'), el.id, typeof el.className === 'string' ? el.className : '', el.getAttribute('href')]
  .filter(Boolean).join(' ').slice(0, 300);
const unsafeForm = (el) => {{ const f = el.closest('form'); if (!f) return false;
  return !!f.querySelector('input[type=password]') ||
    NEVER.test((f.getAttribute('action') || '') + ' ' + (f.id || '')); }};
const sameHost = (href) => {{ try {{ const u = new URL(href, location.href); return u.hostname === location.hostname; }}
  catch (e) {{ return false; }} }};
document.querySelectorAll('[{MARK}]').forEach((e) => e.removeAttribute('{MARK}'));
const pick = (el, what) => {{ el.setAttribute('{MARK}', '1');
  const href = el.tagName === 'A' ? new URL(el.getAttribute('href'), location.href).href : null;
  return JSON.stringify({{found: true, what: what, href: href}}); }};
const none = JSON.stringify({{found: false}});
"""

_FIND = {
    "search": """
const KEYWORDY = /keyword|search|job|title|role|position|what|query|find/i;
const PLACE = /location|city|zip|postal|where|distance|radius|email|phone/i;
const labelOf = (el) => ((el.labels && el.labels[0] ? el.labels[0].innerText : '') + ' ' +
  (el.getAttribute('aria-labelledby') || '').split(' ').map((i) => (document.getElementById(i) || {}).innerText || '')
  .join(' '));
const inRegion = (el) => !!el.closest('[role=search], form[action*=search i], [class*=search i], [id*=search i]');
const boxes = [...document.querySelectorAll('input')].filter((el) => {
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  if (!['search', 'text'].includes(type) || !seen(el) || el.disabled || el.readOnly || unsafeForm(el)) return false;
  const said_ = said(el) + ' ' + labelOf(el);
  return !PLACE.test(said_) && (KEYWORDY.test(said_) || inRegion(el));
});
const el = boxes.find((b) => KEYWORDY.test(said(b) + ' ' + labelOf(b))) || boxes[0];
if (el) return pick(el, 'search box');
const WORDS = new RegExp('^(search( (all )?(jobs?|openings|roles|positions))?|find (jobs?|roles?|openings)|' +
  '(view|see|browse) (all )?(jobs?|openings|roles|positions))$', 'i');
const btn = [...document.querySelectorAll('a[href], button, [role=button], input[type=submit]')].filter((b) => {
  if (!seen(b) || b.disabled || NEVER.test(said(b)) || unsafeForm(b)) return false;
  if (b.tagName === 'A' && !sameHost(b.getAttribute('href'))) return false;
  const text = ((b.innerText || b.value || '') + '').trim();
  return WORDS.test(text) || WORDS.test((b.getAttribute('aria-label') || '').trim());
})[0];
if (!btn) return none;
return pick(btn, 'search box');
""",
    "next": """
const cands = [...document.querySelectorAll('a[href], button, [role=button], [role=link]')].filter((el) => {
  if (!seen(el) || el.disabled || el.getAttribute('aria-disabled') === 'true' || NEVER.test(said(el))) return false;
  if (el.tagName === 'A' && !sameHost(el.getAttribute('href'))) return false;
  const text = (el.innerText || '').trim();
  const label = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
  return el.matches('a[rel~=next]') || /^(next|next page|[\\u203a\\u00bb>\\u2192])$/i.test(text) ||
    /^(view )?next( page)?$/i.test(label);
});
const el = cands[0];
if (!el) return none;
return pick(el, 'next page');
""",
    "filter": """
const boxes = [...document.querySelectorAll('input[type=checkbox]')].filter((el) => {
  if (!seen(el) || el.disabled || el.checked || unsafeForm(el)) return false;
  if (el.closest('[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i], [id*=onetrust i], ' +
    '[id*=didomi i], [id*=usercentrics i]')) return false;  // a banner's switches are cookies, never a filter
  const label = (el.labels && el.labels[0] ? el.labels[0].innerText : '') + ' ' + said(el);
  return !NEVER.test(label);
});
const inFacet = boxes.filter((el) => !!el.closest(
  '[class*=filter i], [class*=facet i], [class*=refine i], [id*=filter i], [id*=facet i], aside'));
const el = (inFacet.length ? inFacet : boxes)[0];
if (!el) return none;
return pick(el, 'filter');
""",
    "open": """
const jobs = new RegExp('/(jobs?|job-?details?|positions?|openings?|requisitions?|vacanc[a-z]*|careers?|' +
  'postings?)/[^/?#]{3,}', 'i');
const cands = [...document.querySelectorAll('a[href]')].filter((el) => {
  const href = el.getAttribute('href') || '';
  const text = (el.innerText || '').trim();
  return seen(el) && sameHost(href) && jobs.test(href) && !NEVER.test(href) && !NEVER.test(text) &&
    text.length >= 6 && text.length <= 160;
});
const el = cands[0];
if (!el) return none;
return pick(el, 'a job');
""",
}

# A cookie banner's controls, found by what the banner calls itself (its id, class or label) or, for a dialog, by what
# it says; nothing is clicked here (`consent_find.js`). Each control that is in a banner is marked with its number and
# answered with its label, so the code (`consent.is_decline`) can tell which one refuses cookies. A banner is a small
# element that talks about cookies or tracking: the page's own wrapper (a class like "has-cookie-banner" on the whole
# app) is too big to be one. `consent_click.js` clicks the control marked with `%NUMBER%` and takes every mark off.
# Both are files beside this module, read once, so the scout's node tests (`app/scout/consent-find.test.mjs`) run the
# very text the page gets.
_SCRIPTS = Path(__file__).parent
_CONSENT_FIND = (_SCRIPTS / "consent_find.js").read_text(encoding="utf-8")
_CONSENT_CLICK = (_SCRIPTS / "consent_click.js").read_text(encoding="utf-8")


_CLICK = f"""
(() => {{ const el = document.querySelector('[{MARK}]'); if (!el) return JSON.stringify({{clicked: false}});
  el.removeAttribute('{MARK}');
  if (el.tagName === 'A') el.target = '_self';
  if (el.tagName === 'INPUT' && el.type !== 'checkbox') {{
    el.focus();
    const form = el.form;
    const near = (form || el.closest('div, section, header')) &&
      [...(form || el.closest('div, section, header')).querySelectorAll('button, input[type=submit], [role=button]')]
      .find((b) => /search|find|go|submit/i.test(
        (b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '') + ' ' + (b.value || '')));
    if (near) near.click();
    else if (form && form.requestSubmit) form.requestSubmit();
    else {{ for (const t of ['keydown', 'keypress', 'keyup'])
      el.dispatchEvent(new KeyboardEvent(t, {{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}})); }}
  }} else el.click();
  return JSON.stringify({{clicked: true}}); }})()
"""

def _type_script(word: str) -> str:
    """Types one word in the marked search box, the way a visitor does (the page hears an input and a change)."""
    return (f"(() => {{ const el = document.querySelector('[{MARK}]'); "
            "if (!el || el.tagName !== 'INPUT') return JSON.stringify({typed: false}); el.focus(); "
            "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, "
            f"{json.dumps(word)}); "
            "for (const t of ['input', 'change']) el.dispatchEvent(new Event(t, {bubbles: true})); "
            "return JSON.stringify({typed: true}); })()")


_HTML = ("JSON.stringify({url: location.href, "
         f"html: document.documentElement.outerHTML.slice(0, {HTML_MAX})}})")


@dataclass
class Snapshot:
    """The page as one step left it: its address and its HTML."""

    url: str
    html: str


@dataclass
class Outcome:
    """What the interaction did: the steps taken (`done`), the steps skipped with why (`skipped`), the page as it stood
    after the load and after each step (`snapshots`), the status of a refusal that stopped it (`stop`), and whether a
    cookie banner was declined first (`declined`, no step of its own)."""

    done: list[str]
    skipped: dict[str, str]
    snapshots: list[Snapshot]
    stop: int | None = None
    declined: bool = False


#: Hosts that are no job site at all: where a page links to its social profiles, its maps and its stores. The job
#: boards are the registry's (`not_boards`).
NOT_A_JOB_SITE = ("facebook.", "twitter.", "x.com", "youtube.", "instagram.", "apple.com", "microsoft.com", "w3.org",
                  "schema.org", "github.", "tiktok.", "pinterest.")


def not_boards() -> tuple[str, ...]:
    """What a host is never taken for a job board's page by: the sites that are none, and the job boards (a board
    named by its first label and a dot: `example.` for `example.test`), as the registry lists them."""
    return (*NOT_A_JOB_SITE, *(h.removeprefix("www.").split(".")[0] + "." for h in registry.board_hosts()))


_URL = re.compile(r"https://[A-Za-z0-9.-]+(?:/[^\s\"'<>\\)]*)?")
_BOARD_PATH = re.compile(r"/(job-?boards?|jobs?|openings?|positions?)(/[^/]+)?/?", re.I)
_CAREER_PATH = re.compile(r"/careers?(/[^/]+)?/?", re.I)


def board_link(html: str, page_url: str) -> str | None:
    """Where the jobs are, when the employer's own page only points to them: the first public address on another host
    that the page itself names (a link, a button's target, a frame) and whose path is a job board's (`/job-board/x`,
    `/jobs`, `/openings`; a `/careers` path only when there is no better). One hop, never a social site or a store.
    The caller still asks robots.txt before it goes there."""
    here = urlsplit(page_url).hostname or ""
    text = html.replace("\\/", "/").replace("&amp;", "&")
    never = not_boards()
    best: tuple[int, str] | None = None
    for m in _URL.finditer(text):
        link = m.group(0).rstrip(".,;")
        host = urlsplit(link).hostname or ""
        if not host or host == here or any(n in host for n in never) or not public_url(link):
            continue
        path = urlsplit(link).path
        rank = 0 if _BOARD_PATH.fullmatch(path) else 1 if _CAREER_PATH.fullmatch(path) else None
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, link)
    return best[1] if best else None


def refused(requests: list[dict], host: str) -> int | None:
    """The status of a refusal in what the page asked for: a page answered 401, 403 or 429, or any request to the
    page's own host answered 429."""
    for r in requests:
        status = r.get("status")
        if r.get("kind") == "Document" and status in BLOCKED_STATUSES:
            return status
        if status == 429 and (urlsplit(r.get("url") or "").hostname or "") == host:
            return 429
    return None


async def _value(tools: Any, sid: str, expression: str) -> Any:
    got = await tools.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, sid)
    value = (got.get("result") or {}).get("value")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def snapshot(tools: Any, sid: str) -> Snapshot | None:
    """The page's address and HTML now, or None when it cannot be read."""
    got = await _value(tools, sid, _HTML)
    if isinstance(got, dict) and isinstance(got.get("html"), str):
        return Snapshot(str(got.get("url") or ""), got["html"])
    return None


async def find(tools: Any, sid: str, step: str) -> dict | None:
    """The element a step would use, marked in the page (nothing clicked): {found, what, href}, or None."""
    got = await _value(tools, sid, f"(() => {{ {_PRELUDE} {_FIND[step]} }})()")
    return got if isinstance(got, dict) and got.get("found") else None


async def click(tools: Any, sid: str) -> bool:
    got = await _value(tools, sid, _CLICK)
    return bool(isinstance(got, dict) and got.get("clicked"))


async def decline_consent(tools: Any, sid: str) -> bool:
    """Decline a cookie banner the page shows: its control that refuses non-essential cookies is clicked (`is_decline`:
    "Reject all", "Decline", "Necessary only"), once, and True is the answer. A page with no banner, or a banner that
    offers only accept, is left as it is (False): nothing is ever accepted, and what the banner hides is read past, not
    waited for. The page is looked at once, with no wait, and a failed look is no banner."""
    try:
        labels = await _value(tools, sid, _CONSENT_FIND)
        if not isinstance(labels, list):
            return False
        for number, label in enumerate(labels):
            if consent.is_decline(label if isinstance(label, str) else None):
                got = await _value(tools, sid, _CONSENT_CLICK.replace("%NUMBER%", str(number)))
                return bool(isinstance(got, dict) and got.get("clicked"))
    except bc.BrowserUnavailable:  # the tab went: the page's own reading reports it
        return False
    return False


def clean_keyword(word: str | None) -> str | None:
    """The word typed in a search box: the owner's own, one line, at most 80 characters, no control characters."""
    word = " ".join("".join(c for c in (word or "") if c.isprintable()).split())[:80]
    return word or None


async def type_word(tools: Any, sid: str, word: str) -> bool:
    got = await _value(tools, sid, _type_script(word))
    return bool(isinstance(got, dict) and got.get("typed"))


def vet(step: str, found: dict, page_url: str) -> str | None:
    """Why a found link may not be followed (the reason), or None. Only a link has an address to vet; a button or a
    box does not take the visitor anywhere the page did not already say."""
    href = found.get("href")
    if not href:
        return None
    if not public_url(href):
        return "its address is not a public one"
    if (urlsplit(href).hostname or "") != (urlsplit(page_url).hostname or ""):
        return "it goes to another site"
    return None


async def _search_for(tools: Any, sid: str, keyword: str, new_traffic: Callable[[], list[dict]],
                      settle: Callable[..., Awaitable[None]], host: str) -> int | None:
    """The empty search did nothing: type the one keyword in the same box, submit it, and say a refusal's status."""
    await bc.sleep(ACTION_GAP)
    if await find(tools, sid, "search") is None or not await type_word(tools, sid, keyword):
        return None
    new_traffic()
    if not await click(tools, sid):
        return None
    await settle(ACTION_SETTLE)
    return refused(new_traffic(), host)


async def work(tools: Any, sid: str, page_url: str, *, new_traffic: Callable[[], list[dict]],
               allowed: Callable[[str], Awaitable[bool]] | None = None, settle: Callable[..., Awaitable[None]],
               steps: tuple[str, ...] = STEPS, keyword: str | None = None) -> Outcome:
    """Do the steps on the page the tab is on. `new_traffic()` gives the requests the page made since the last call (to
    look for a refusal), `allowed(url)` says whether robots.txt allows a link, and `settle(seconds)` lets the page's
    data requests finish. `keyword` is one word from the owner's own target roles, typed in the search box only when the
    search with an empty query brought no data request and no new page."""
    keyword = clean_keyword(keyword)
    host = urlsplit(page_url).hostname or ""
    out = Outcome([], {}, [])
    if await decline_consent(tools, sid):  # a cookie banner is no stop and no step: refuse its cookies and go on
        out.declined = True
        new_traffic()  # what the banner's own answer asked for is not a step's
        await settle(ACTION_SETTLE)
    first = await snapshot(tools, sid)
    if first:
        out.snapshots.append(first)
    loop = asyncio.get_running_loop()
    until = loop.time() + INTERACT_SECONDS
    for step in steps:
        if len(out.done) >= ACTIONS:
            out.skipped[step] = "the most actions were done"
            continue
        if loop.time() >= until:
            out.skipped[step] = "the time was up"
            continue
        if out.done:
            await bc.sleep(ACTION_GAP)
        found = None
        for _ in range(int(FIND_WAIT) + 1):
            if (found := await find(tools, sid, step)) is not None or loop.time() >= until:
                break
            await bc.sleep(1.0)
        if found is None:
            out.skipped[step] = "the page has none"
            continue
        current = (out.snapshots[-1].url if out.snapshots else page_url) or page_url
        why = vet(step, found, current)
        if why is None and found.get("href") and allowed is not None and not await allowed(found["href"]):
            why = "robots.txt asks not to read it"
        if why is not None:
            out.skipped[step] = why
            continue
        new_traffic()  # what came before is not this step's
        if not await click(tools, sid):
            out.skipped[step] = "it could not be clicked"
            continue
        out.done.append(step)
        await settle(ACTION_SETTLE)
        traffic = new_traffic()
        if (stop := refused(traffic, host)) is not None:
            out.stop = stop
            break
        if step == "search" and keyword and found.get("what") == "search box" and not any(
                r.get("kind") in ("XHR", "Fetch", "Document") for r in traffic):
            if (stop := await _search_for(tools, sid, keyword, new_traffic, settle, host)) is not None:
                out.stop = stop
                break
        snap = await snapshot(tools, sid)
        if snap:
            out.snapshots.append(snap)
    return out
