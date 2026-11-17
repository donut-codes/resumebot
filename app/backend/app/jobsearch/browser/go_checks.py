"""Go's checks: before the tabs of Ready to apply open, each role is checked, and the batch is refilled (the owner,
2026-10-16).

1. **The mail** (`mail/reader.py`, read-only): a confirmation that the owner already applied moves the role to Applied,
with
   the mail's date and subject on its history, and it is skipped. An unsure match blocks nothing and is noted.
2. **The posting.** The main test (the owner, 2026-10-16) compares the page the role's text was read from with the
   posting held (`compared`, `posting_match.py`): the text there is open, part of it is open and flagged "posting
   changed" on the history (`note_changed`), neither its text nor its title is closed. Code takes a match only as
   evidence it is open; the app's Chrome decides closed. The quick signals beside it: in code first (`open_state`) the
   job system's own answer for the posting (a drawn system's board search, its reader's `listed`: a posting it does not
   find is closed), else the page, read
   politely (`polite.py`: robots.txt, a request a second to a host, an honest user agent). A 404 or 410, a schema.org
   JobPosting whose `validThrough` has passed, or a page that says it no longer takes applications or is missing (the
   one list, `closed_words.py`), or, in the browser, a redirect to a page of the site without the posting's id
   (`moved_away`), is closed: the role
   ends as Gone Quiet, "the posting went", with that evidence (`quiet.end_gone`). Code says "open" only on
   evidence (the job system's own text, or a JobPosting whose `validThrough` has not passed): a plain page that merely
   says nothing proves nothing, because many pages draw "no longer accepting applications" only in a browser.
   What code cannot tell (a job board's page, one that refuses the app, one that says nothing) is looked at in the
   app's own Chrome over DevTools (`browser_verdict`, `BROWSER_SECONDS` a page, pages of different hosts side by side,
   a host's pages one after another) before the tab opens; closed there ends the role the same way and the batch is
   refilled. Still unknown after that counts as open, and a page the app could not read stays a priority browser `read`
   item (`browser_queue._wanted`) that lives `BROWSER_LIFE`.
3. **The line**: `prepare` walks the line (Ready to apply, then Application is ready, in the board's order:
   `board.ready_line`), role by role, until `ready_to_launch_size` roles pass or the line runs out. A role with no
   apply link is skipped. A role at an employer applied to within `days_between_employer_applications` days is
   skipped too (`employer_gap`).

A role checked in the last `CHECKED_WITHIN` is not checked again (the night's pre-check, `precheck`, at `PRECHECK_AT`,
runs the same checks on the next batch and its refill so Go is quick). Nothing here clicks Apply or fills a form, and
nothing is written to the mailbox.

4. **The check on entering Ready to Launch** (the owner, 2026-11-06, docs/PRD.md 24.16): a role that comes into Ready to
Launch, by the deck, a star or the owner's hand, is checked at once and at any hour (`check_entered`, a step of the
pipeline's turn after the queue is balanced), without waiting for Go: the posting check of step 2 in code, with the page
of the role's own source compared with the posting held (`compared`). The result is on the role (`go_check`,
`go_checked_at`, a line of its history and its row's words). A posting that is gone ends the role as in step 2, and it
leaves Ready to Launch with its reason; a page that refuses the app, or a job board's, is looked at by the browser as
the owner's own item (`browser_checks`, priority 0, at any hour), never by a Chrome started from the turn. A page that
said nothing either way (a timeout, a posting drawn in the browser, no answer: `UNKNOWN`, which Go counts as open) is
not: it says so on the role and Launch checks it again before its tab opens, so a short outage never queues a read for
every role that came in. At most `ENTRIES_PER_CHECK` roles are checked in one call, the rest by the next. Go's own run
still checks the role (the owner's mail for an application already sent, the posting again) unless the browser's check
is waiting (`_fresh`). Services flush; the route or the night's task commits."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlsplit

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import adapters, as_read, ats, consent, stages
from app.jobsearch.address import is_board_link
from app.jobsearch.browser import posting_match
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, Refused
from app.jobsearch.mail import login as mail_login
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.mail import sync as mail_sync
from app.jobsearch.models import Employer, Opportunity, StageMove
from app.jobsearch.pipeline import board, quiet
from app.jobsearch.postings import closed_words as phrases
from app.jobsearch.postings import dates as posted_dates
from app.jobsearch.profile import store as profile_store
from app.mailmine import imap
from app.mailmine.message import Seen, scrub_quote
from app.sites import registry
from app.timeutil import aware, owner_today, owner_zone, utc_now
from app.trail import service as trail

log = logging.getLogger(__name__)

#: A role checked this recently is not checked again by Go.
CHECKED_WITHIN = timedelta(hours=12)
#: How long a browser check stays in the browser's queue: a short life, so a stale check never lingers.
BROWSER_LIFE = timedelta(hours=2)
#: How long the posting check of one role may take in code, and how long Chrome may take to start.
FETCH_SECONDS, START_SECONDS = 20.0, 20.0
#: How long a page may take in the app's browser: longer than the longest the read waits on a page that has loaded.
#: `BROWSER_READ_SECONDS` is that wait, `browser_code.SETTLE_SECONDS + FILL_SECONDS` (a page that shows fewer than
#: `FILL_CHARS` characters, as a closed posting's one line does, uses all of it); that module imports this one, so the
#: sum is stated here and a test holds the two equal (`test_go_browser_check.py`). `BROWSER_LOAD_SECONDS` is the
#: allowance on top for opening the tab and loading the page: about what a page that draws itself had of the 10 seconds
#: the whole check had before (2026-11-11: with the budget equal to the read's waits, a short page was never read and
#: every check of it said unknown).
BROWSER_READ_SECONDS, BROWSER_LOAD_SECONDS = 10.0, 10.0
BROWSER_SECONDS = BROWSER_READ_SECONDS + BROWSER_LOAD_SECONDS
#: The night's pre-check: at the end of the off-hours window, in the owner's time, and how late it may still start.
PRECHECK_AT, PRECHECK_UNTIL = time(6, 45), time(7, 45)
#: What `go_check` holds: the posting is open (or nothing said it closed), or a browser checks it. The check made when
#: a role comes into Ready to Launch (`check_entered`) holds `ENTERED` (the posting was found open), `LOOKING` (code
#: could not read the page: the browser looks, `browser_checks`) or `UNCLEAR` (the page said nothing either way, and
#: Launch checks it again). Go's own run takes none of them as its check (`_fresh`), since it reads the owner's mail
#: too, and checks the role itself.
OPEN, BROWSER, ENTERED, LOOKING, UNCLEAR = "open", "browser", "entered", "looking", "unclear"
#: How many roles `check_entered` checks in one call: each page may take `FETCH_SECONDS`, and the calls run in the
#: pipeline's turn, so a long list of entries is taken a deck at a time (the turn goes on to the rest).
ENTRIES_PER_CHECK = 15
#: The two that wait for a browser's look (`browser_checks`).
BROWSER_LOOKS = (BROWSER, LOOKING)
#: What a posting check finds. `CHANGED`: the posting is up, in other words than the text held (it counts as open).
CLOSED, UNKNOWN, UNREADABLE, CHANGED = "closed", "unknown", "unreadable", "changed"
#: The statuses that say a page won't let the app read it (a browser looks instead).
REFUSING = (401, 403, 429)
APPLIED_EVENT, UNSURE_EVENT = "applied_in_mail", "mail_maybe_applied"
BROWSER_EVENT, CHANGED_EVENT = "go_browser_check", "posting_changed"
#: The history line of the check made when a role comes into Ready to Launch (`check_entered`).
READY_CHECK_EVENT = "ready_check"


@dataclass
class Outcome:
    """The roles to open, in order, and what was skipped (one short line each) and noted."""

    ready: list[Opportunity] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: The employer gap (`employer_gap`), read once: the days, and the day each employer last became Applied.
    gap: _Gap | None = None


def _web(text: str | None) -> str | None:
    text = (text or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else None


def _bare(link: str) -> bool:
    """A site's home page: no path and no query, so no particular posting."""
    parts = urlsplit(link)
    return not parts.path.strip("/") and not parts.query


def link_of(role: Opportunity) -> str | None:
    """The address Go opens: the employer's own posting when sourcing found it (`source_url` of kind "employer"), else
    the apply link, else the role's link; the first of those that is a web address and not a bare home page, else the
    first that is a web address; None when none is."""
    own = _web(role.source_url) if role.source_kind == "employer" else None
    found = [x for x in (own, _web(role.apply_url), _web(role.url)) if x]
    return next((x for x in found if not _bare(x)), found[0] if found else None)


@dataclass
class _Gap:
    """`Settings.days_between_employer_applications` and, by employer key, the last day an application began."""

    days: int
    today: date
    last: dict[str, date]


async def _gap(session: AsyncSession, owner: str, out: Outcome, today: date) -> _Gap | None:
    """The employer gap as it stands (read once per Go); None when the owner left it blank."""
    if out.gap is None:
        days = (await profile_store.current(session, owner)).settings.days_between_employer_applications
        last: dict[str, date] = {}
        if days and days > 0:
            rows = (await session.execute(
                select(Opportunity.company, Opportunity.created_at, func.max(StageMove.at))
                .outerjoin(StageMove, (StageMove.role_id == Opportunity.id) & (StageMove.to_status == stages.APPLIED))
                .where(Opportunity.owner == owner, Opportunity.status == stages.APPLIED)
                .group_by(Opportunity.id))).all()
            for company, created, moved in rows:
                when = aware(moved or created)
                if when is None:
                    continue
                day = when.astimezone(owner_zone()).date()
                key = employers.key_of(company)
                last[key] = max(day, last.get(key, day))
        out.gap = _Gap(days or 0, today, last)
    return out.gap if out.gap.days > 0 else None


async def employer_gap(session: AsyncSession, owner: str, role: Opportunity, out: Outcome, today: date) -> str | None:
    """The line that says why a role is skipped: the owner applied to its employer within their number of days; None
    when the number is blank or no application is that recent. Nothing is changed on the role."""
    gap = await _gap(session, owner, out, today)
    day = gap.last.get(employers.key_of(role.company)) if gap else None
    if gap is None or day is None or (gap.today - day).days >= gap.days:
        return None
    ago = (gap.today - day).days
    return f"Applied to {role.company} {ago} day{'' if ago == 1 else 's'} ago."


async def gapped_employers(session: AsyncSession, owner: str, today: date) -> set[str]:
    """The employers (`employers.key_of`) the owner applied to within their number of days (`employer_gap`): no role at
    one takes a deck or Ready to Launch place (`pipeline.rebalance`). Empty when the number is blank."""
    gap = await _gap(session, owner, Outcome(), today)
    return {k for k, day in gap.last.items() if (gap.today - day).days < gap.days} if gap else set()


#: The last zone on Earth to see a date end: a date-only `validThrough` is past only once the day has ended there.
_LAST_ZONE = timezone(timedelta(hours=-12))


def _past(valid_through: object, today: date | None = None, now: datetime | None = None) -> date | None:
    """The day a JobPosting's `validThrough` names, when it has passed at `now` (the clock; `today` is no longer used).
    An instant (a time with `Z` or an offset, or an epoch) has passed when it is before `now`, and names the owner's day
    of it; a date-only value has passed only after that day has ended in the last zone on Earth (UTC-12), so a posting
    is never called closed on its last day anywhere."""
    now = aware(now) or utc_now()
    text = str(valid_through or "")
    if (when := posted_dates.instant(valid_through if isinstance(valid_through, int | float) else text)) is not None:
        return when.astimezone(owner_zone()).date() if when < now else None
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return day if datetime.combine(day + timedelta(days=1), time(), _LAST_ZONE) <= now else None


def closed_words(text: str | None) -> str | None:
    """A closed or missing phrase on the page (`closed_words.py`, the one list), or None."""
    return phrases.found(text)


def source_link(role: Opportunity | None) -> str | None:
    """The page the role's held text was read from (`Opportunity.source_url`), when it is a web address."""
    text = ((role.source_url if role is not None else None) or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else None


def _page_key(url: str) -> tuple[str, str]:
    """An address as a page: its host without "www." and its path in lower case, without a language ("en-US")."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = "/".join(p for p in parts.path.lower().split("/") if p and not re.fullmatch(r"[a-z]{2}-[a-z]{2}", p))
    return host, path


def compared(role: Opportunity | None, link: str, text: str | None) -> posting_match.Match | None:
    """How much of the role's held posting the page at `link` carries (`posting_match.compare`), or None when there is
    nothing to compare: the role holds no text, or `link` is not the page the text was read from (an application form
    or a careers home does not carry the posting, and is not judged by it)."""
    if role is None or not (role.description or "").strip():
        return None
    source = source_link(role)
    if source is None or _page_key(source) != _page_key(link):
        return None
    return posting_match.compare(role.title, role.description, text)


#: A posting's id in an address: a GUID, or a long number with at most a short prefix ("R13956", "JR0027479").
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
                 r"(?<![a-z0-9])[a-z]{0,4}-?\d{4,}(?:-\d+)?(?![a-z0-9])")


def _ids(url: str) -> set[str]:
    parts = urlsplit(url.lower())
    return {m.group(0).lstrip("-") for m in _ID.finditer(f"{parts.path}?{parts.query}")}


def _site(host: str | None) -> str:
    return ".".join((host or "").lower().split(".")[-2:])


_FRAME = re.compile(r"<iframe\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)", re.I)


def _framed(html: str, url: str) -> bool:
    """Whether the page draws part of itself in a frame from its own site (a job system that shows the posting so): what
    the page's own markup lacks may be in the frame, so it is not judged missing."""
    here = _site(urlsplit(url).hostname)
    return any(_site(urlsplit(urljoin(url, src)).hostname) == here for src in _FRAME.findall(html or ""))


def moved_away(link: str, final: str) -> bool:
    """Whether the page at `link` sent the browser on to another page of the same site that names none of the posting's
    ids (a job search, or the careers home): the posting is not there any more. A link that carries no id, a page that
    stayed where it was, and a move to another site (a tracking link on to the employer) say nothing."""
    if not final.lower().startswith(("http://", "https://")):
        return False
    here, there = urlsplit(link), urlsplit(final)
    if _site(here.hostname) != _site(there.hostname) or _page_key(link) == _page_key(final):
        return False
    ids = _ids(link)
    return bool(ids) and not any(i in final.lower() for i in ids)


async def open_state(fetch: Fetcher, link: str, today: date, role: Opportunity | None = None) -> tuple[str, str]:
    """Whether the posting at `link` still takes applications, as code can tell: `OPEN`, `CHANGED` (up, in other words
    than the text held), `CLOSED` (with the evidence), `UNREADABLE` (a job board, or a page that refuses the app: a
    browser looks) or `UNKNOWN` (counts as open). With `role`, the page is compared with the posting it holds
    (`compared`); in code that is only ever evidence the posting is up, never that it is gone (a page drawn in the
    browser has no text in its markup): the browser's look decides that."""
    if is_board_link(link):
        return UNREADABLE, "a job board's page, which the app does not read"
    found = ats.parse(link)
    if found and found.read and found.drawn:
        # A system that draws its postings in a browser: its page says nothing, so its reader's answer for the posting
        # is the one thing code can judge by. Every other system's page is read below, as any page is.
        adapter, board, ref = found.read.reader, found.read.board.parts, found.read.posting
        listed = getattr(adapter, "listed", None)
        if listed is not None:  # a drawn system: the board's own search says whether the posting is still on it
            try:
                on_board = await listed(fetch, board, ref["url"])
            except (Refused, httpx.HTTPError, ValueError):
                on_board = None
            if on_board is False:
                return CLOSED, (f"{link} is no longer on the employer's {ats.label(found.system)} board: "
                                "its search finds no such posting.")
        try:
            got = await adapter.detail(fetch, board, ref)
        except adapters.AdapterError as exc:
            if said := polite.gone(link, exc.status):
                return CLOSED, said
            return UNKNOWN, ""
        except (Refused, httpx.HTTPError, ValueError):
            return UNKNOWN, ""
        text = (got or {}).get("text")
        if said := closed_words(text):
            return CLOSED, f'{link} says "{said}".'
        found = compared(role, link, text) if text else None
        if found is not None and found.verdict in (posting_match.CHANGED, posting_match.MISSING):
            return CHANGED, found.words()  # the job system's own answer for this posting: up, in other words
        return (OPEN if text else UNKNOWN), ""
    try:
        reply = await fetch.get(link, patient=False)
    except polite.NotPublic:
        return UNKNOWN, ""
    except Refused as exc:
        if getattr(exc, "unreachable", False):  # the site did not answer: that is not the site saying no
            return UNKNOWN, ""
        return UNREADABLE, "the site does not let the app read it"
    except (httpx.HTTPError, ValueError):
        return UNKNOWN, ""
    if said := polite.gone(link, reply.status_code):
        return CLOSED, said
    if reply.status_code in REFUSING:
        return UNREADABLE, f"the site answered HTTP {reply.status_code}"
    if reply.status_code != 200 or "html" not in reply.headers.get("content-type", "html").lower():
        return UNKNOWN, ""
    page = reply.text
    data = adapters.job_posting(page) or {}
    if day := _past(data.get("validThrough")):
        return CLOSED, f"{link}: the posting's own data says it took applications until {day:%d %B %Y}."
    text = adapters.html_to_text(page[:400_000])
    if said := closed_words(text):
        return CLOSED, f'{link} says "{said}".'
    found = compared(role, link, text)
    if found is not None and found.verdict == posting_match.MATCH:
        return OPEN, ""
    if found is not None and found.verdict == posting_match.CHANGED:
        return CHANGED, found.words()
    return (OPEN if data else UNKNOWN), ""


async def browser_page(link: str):
    """The page at `link` as the app's own Chrome shows it (tests replace this); raises BrowserUnavailable."""
    from app.jobsearch.browser import (
        code as browser_code,  # imported here: browser_code imports modules that import this one
    )

    return await browser_code.open_page(link)


async def start_browser() -> str:
    """Start the app's Chrome if it is closed (tests replace this): "" when it is up, else why it could not start."""
    from app.jobsearch.browser import code as browser_code

    try:
        return await asyncio.wait_for(browser_code.start_browser(), START_SECONDS)
    except Exception as exc:
        return f"Chrome could not be started ({type(exc).__name__})"


def _site_name(address: str | None) -> str:
    """What the site at an address is called, by its runbook; the site otherwise."""
    runbook = registry.by_address(address)
    return runbook.name if runbook else "the site"

_SIGN_IN_PAGE = re.compile(r"/(?:authwall|login|signin|sign-in|uas/login|checkpoint)\b", re.I)


async def browser_verdict(link: str, today: date, role: Opportunity | None = None) -> tuple[str, str]:
    """Whether the posting at `link` still takes applications, as the app's Chrome shows it (`page_verdict`);
    `UNKNOWN` when the browser could not load it or took too long."""
    try:
        page = await asyncio.wait_for(browser_page(link), BROWSER_SECONDS)
    except Exception:  # unavailable, too slow, or anything else: the tab opens as before
        return UNKNOWN, ""
    return page_verdict(page, link, today, role)


def page_verdict(page, link: str, today: date, role: Opportunity | None = None) -> tuple[str, str]:
    """What a page the app's Chrome shows says about the posting at `link` (`browser_verdict`, and Go's tabs).

    The main test is the posting held (`compared`, on the page its text was read from): its text there is `OPEN`, part
    of it `CHANGED`, neither its text nor its title `UNKNOWN` (lacking the posting is not proof it is gone).
    Beside it, quick signals: a 404 or 410, a board's
    removed-posting redirect, a `validThrough` that has passed, a closed or missing phrase (`closed_words.py`), or a
    move to another page of the site that names none of the posting's ids (`moved_away`) are `CLOSED`. A sign-in wall
    or a bot check is `UNKNOWN`, and so is a page with no posting data and fewer than `posting_match.MIN_PAGE_WORDS`
    words (not confirmed, not open). Anything else is `OPEN`."""
    from app.jobsearch.browser import (
        code as browser_code,  # imported here: browser_code imports modules that import this one
    )

    if said := polite.gone(link, page.status):
        return CLOSED, said
    links = registry.board_reader("links")
    if links is not None and links.removed_redirect(page.url):  # a board sends a removed posting to a search page
        return CLOSED, f"{link} was taken down: {_site_name(page.url)} sent it on to a job search instead."
    if _SIGN_IN_PAGE.search(page.url or ""):
        return UNKNOWN, ""
    data = adapters.job_posting(page.html) or {}
    if day := _past(data.get("validThrough")):
        return CLOSED, f"{link}: the posting's own data says it took applications until {day:%d %B %Y}."
    # What the posting check reads leaves out a cookie banner's words: they say nothing of the posting, and a page that
    # is only a banner then shows too little to judge (`posting_match.MIN_PAGE_WORDS`), not a drawn page without it.
    text = adapters.html_to_text(consent.without_banners(page.html))
    if said := closed_words(text):
        return CLOSED, f'{link} says "{said}" (as the browser shows it).'
    if browser_code.page_state(page.html, page.url or "") == browser_code.STATE_CHECK:
        return UNKNOWN, ""  # a bot check hides the posting: what is missing from it says nothing
    found = compared(role, link, text)
    if found is not None and found.verdict == posting_match.MATCH:
        return OPEN, ""
    if found is not None and found.verdict == posting_match.CHANGED:
        return CHANGED, found.words()
    if moved_away(link, page.url or ""):
        return CLOSED, f"{link} sent the browser on to {page.url}, which is not the posting (as the browser shows it)."
    if (found is not None and found.verdict == posting_match.MISSING and not found.title and not data
            and not _framed(page.html, page.url or link)):
        return UNKNOWN, ""  # a page that lacks the posting may be a shell or an interstitial: not proof it is gone
    if not data and len(posting_match.words(text)) < posting_match.MIN_PAGE_WORDS:
        return UNKNOWN, ""  # too little drawn to judge, and no posting data: not confirmed, which is not open
    return OPEN, ""


async def note_changed(session: AsyncSession, role: Opportunity, link: str, said: str) -> None:
    """A posting still up in other words than the text held: flagged on the role's history (it counts as open)."""
    await trail.record(
        session, role.owner, event=CHANGED_EVENT, link=("opportunity", role.id),
        sentence=f"Posting changed: {role.title} at {role.company} is up, in other words than the text held.",
        detail=f"{link}: {said}" if said else link)


def _fresh(role: Opportunity, now: datetime, within: timedelta) -> bool:
    checked = aware(role.go_checked_at)
    return checked is not None and role.go_check in (OPEN, BROWSER) and timedelta(0) <= now - checked < within


async def _names(session: AsyncSession, role: Opportunity) -> go_mail.Names:
    own: list[str | None] = [role.company]
    aliases: list[str] = []
    if role.employer_id is not None and (employer := await session.get(Employer, role.employer_id)) is not None:
        own.append(employer.name)
        aliases = [str(a) for a in (employer.aliases or [])]
    return go_mail.employer_names(own, aliases)


def _mail_words(seen: Seen) -> str:
    return f'The mail of {seen.day:%d %B %Y} from {seen.sender_name}: "{scrub_quote(seen.subject, 160)}".'


async def _applied(session: AsyncSession, owner: str, role: Opportunity, seen: Seen) -> None:
    role.status, role.ended_reason = stages.APPLIED, None
    await trail.record(
        session, owner, event=APPLIED_EVENT, tier="result", link=("opportunity", role.id),
        sentence=f"Applied: your mail says you applied to {role.title} at {role.company}.", detail=_mail_words(seen))


class _Mail:
    """The owner's confirmations, read once, the first time a role needs them."""

    def __init__(self, session: AsyncSession, owner: str, notes: list[str], today: date) -> None:
        self.session, self.owner = session, owner
        self.notes, self.today = notes, today
        self.read = False
        self.mails: list[Seen] | None = None

    async def get(self) -> list[Seen] | None:
        if self.read:
            return self.mails
        self.read = True
        who = await mail_login.find(self.session, self.owner)
        if who is None:
            self.notes.append(mail_sync.NO_MAILBOX)
            return None
        await db.release(self.session)  # the mailbox may take seconds: no transaction is held while it answers
        try:
            async with asyncio.timeout(go_mail.READ_SECONDS):
                self.mails = await asyncio.to_thread(go_mail.read_confirmations, who, self.today)
        except imap.MailReadError as exc:
            self.notes.append(f"Mail wasn't checked: {exc}")
        except TimeoutError:
            self.notes.append("Mail wasn't checked: the mailbox took too long to answer.")
        except Exception:  # the check never stops Go
            log.warning("reading the mail for Go failed", exc_info=True)
            self.notes.append("Mail wasn't checked: the mailbox couldn't be read.")
        return self.mails


async def applied_by_mail(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """Move the roles Go opened to Applied when the owner's mail holds a sure confirmation of the application (the same
    matching Go's checks use, `go_mail.best`). A role Go opened that is not applied or closed and has no confirmation
    yet stays where it is. Such a role is in Ready to Launch, or before it: one that lost its read leaves Ready to
    Launch (the owner, 2026-11-03, docs/PRD.md 24.13) though its application was sent. Returns how many moved. The mail
    is read once, and only when a role needs it."""
    opened = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.status.in_(stages.PRE_APPLY),
        Opportunity.go_opened_at.is_not(None)))).all()
    if not opened:
        return 0
    notes: list[str] = []
    mails = await _Mail(session, owner, notes, owner_today(now)).get()
    moved = 0
    for role in opened:
        said, seen = go_mail.best(mails or [], await _names(session, role), role.title)
        if said == go_mail.SURE and seen is not None:
            await _applied(session, owner, role, seen)
            moved += 1
    await session.flush()
    return moved


async def _code_check(fetch: Fetcher, role: Opportunity, link: str, today: date) -> tuple[str, str]:
    """The posting check of one role in code: `(state, evidence)`; a failure or a timeout is `UNKNOWN`."""
    try:
        async with asyncio.timeout(FETCH_SECONDS):
            return await open_state(fetch, link, today, role)
    except TimeoutError:
        return UNKNOWN, ""
    except Exception:  # one bad page never stops Go
        log.warning("checking the posting of role %s failed", role.id, exc_info=True)
        return UNKNOWN, ""


async def _settle(session: AsyncSession, role: Opportunity, link: str, state: str, said: str, now: datetime,
                  unreadable: bool) -> str | None:
    """Record a verdict: None when the role may open, else the line that says why it was skipped. `unreadable` is
    whether code could not read the page (so a browser check is still queued when no browser settled it)."""
    if state == CLOSED and await quiet.end_gone(session, role, link, said):
        return "the posting is closed; moved to Gone Quiet."
    if state == CHANGED:
        await note_changed(session, role, link, said)
    role.go_check = BROWSER if (unreadable and state not in (OPEN, CHANGED)) else OPEN
    role.go_checked_at = now
    return None


async def _browse(waiting: list[tuple[Opportunity, str, str]], today: date) -> list[tuple[str, str]]:
    """The browser verdict of each `(role, link, code state)`, in order: hosts side by side, a host's pages one after
    another. When the role's text was read from another page than `link` (`source_link`), that page is looked at too,
    unless `link` already showed the posting closed: it is the page the posting held is compared with."""
    by_host: dict[str, list[int]] = {}
    for n, (_, link, _) in enumerate(waiting):
        by_host.setdefault((urlparse(link).hostname or "").lower(), []).append(n)
    verdicts: list[tuple[str, str]] = [(UNKNOWN, "")] * len(waiting)

    async def one_host(indexes: list[int]) -> None:
        for n in indexes:
            role, link, _ = waiting[n]
            verdicts[n] = await browser_verdict(link, today, role)
            source = source_link(role) if (role.description or "").strip() else None
            if source and verdicts[n][0] != CLOSED and _page_key(source) != _page_key(link):
                at_source = await browser_verdict(source, today, role)
                if at_source[0] in (CLOSED, CHANGED) or verdicts[n][0] == UNKNOWN:
                    verdicts[n] = at_source

    await asyncio.gather(*(one_host(ix) for ix in by_host.values()))
    return verdicts


@dataclass
class _Looked:
    """A role whose posting code has checked: its link, and what code found."""

    role: Opportunity
    link: str
    state: str
    said: str


async def _look(session: AsyncSession, owner: str, role: Opportunity, link: str, mail: _Mail, fetch: Fetcher,
                today: date, out: Outcome) -> _Looked | None:
    """The mail and the posting in code, for one role; None when the mail already settled it (skipped, said)."""
    entry = {"id": str(role.id), "title": role.title, "company": role.company}
    mails = await mail.get()
    if mails:
        said, seen = go_mail.best(mails, await _names(session, role), role.title)
        if said == go_mail.SURE and seen is not None:
            await _applied(session, owner, role, seen)
            out.skipped.append({**entry, "why": f"You already applied ({seen.day:%d %b}); moved to Applied."})
            return None
        if said == go_mail.UNSURE and seen is not None:
            await trail.record(
                session, owner, event=UNSURE_EVENT, link=("opportunity", role.id),
                sentence=f"Maybe applied already: a confirmation from {role.company} may be for this role.",
                detail=_mail_words(seen) + " It names the employer but not the whole title, so nothing moved.")
    await db.release(session)  # what the role's checks wrote so far is kept; the page is read with no transaction open
    state, said_by_code = await _code_check(fetch, role, link, today)
    return _Looked(role, link, state, said_by_code)


async def prepare(session: AsyncSession, owner: str, now: datetime | None = None, *,
                  within: timedelta = CHECKED_WITHIN, progress: dict | None = None) -> Outcome:
    """Walk the line until the batch holds `ready_to_launch_size` roles that pass the checks (or the line runs out). A
    role checked within `within` is taken as it stands. The line is taken a step at a time (as many roles as the batch
    still needs): each is checked in code, those code cannot tell go to the browser together, and a role found closed is
    replaced by the next step. `progress` (the run's state, which the page polls) is told how many roles of the batch
    are to be checked (`check_total`) and how many have been (`checked`)."""
    now = now or datetime.now(UTC)
    today = owner_today(now)
    line, target = await board.ready_line(session, owner)
    out = Outcome()
    if progress is not None:
        progress.update(check_total=min(target, len(line)), checked=0)
    mail = _Mail(session, owner, out.notes, today)
    fetch = polite.make()
    queue, place = iter(line), {str(r.id): n for n, r in enumerate(line)}
    browser_why: str | None = None  # None: Chrome not asked for yet; "": up; else why it could not start
    not_checked = 0
    try:
        while len(out.ready) < target:
            step = [r for _, r in zip(range(target - len(out.ready)), queue, strict=False)]
            if not step:
                break
            slots: list[Opportunity | _Looked | None] = []  # one per role of the step, in order
            for role in step:
                link = link_of(role)
                if (waited := await employer_gap(session, owner, role, out, today)) is not None:
                    role.go_skip = waited
                    out.skipped.append({"id": str(role.id), "title": role.title, "company": role.company,
                                        "why": waited})
                    slots.append(None)
                elif link is None:
                    role.go_skip = "It has no apply link."
                    out.skipped.append({"id": str(role.id), "title": role.title, "company": role.company,
                                        "why": role.go_skip})
                    slots.append(None)
                elif _fresh(role, now, within):
                    role.go_skip = None
                    slots.append(role)
                else:
                    role.go_skip = None  # it passed the gap and the link; the posting check may still skip it
                    slots.append(await _look(session, owner, role, link, mail, fetch, today, out))
                if progress is not None:
                    progress["checked"] = min(progress["checked"] + 1, progress["check_total"])
            undecided = [s for s in slots if isinstance(s, _Looked) and s.state in (UNREADABLE, UNKNOWN)]
            if undecided:
                await db.release(session)  # the browser's looks take seconds a page: no transaction is held meanwhile
            if undecided and browser_why is None:
                browser_why = await start_browser()  # once: Chrome is closed most of the day, and stays up for the run
            if undecided and browser_why:
                verdicts = iter([(UNKNOWN, "")] * len(undecided))
                not_checked += len(undecided)
            else:
                verdicts = iter(await _browse([(s.role, s.link, s.state) for s in undecided], today)
                                if undecided else [])
            for slot in slots:
                if slot is None:
                    continue
                if isinstance(slot, Opportunity):
                    out.ready.append(slot)
                    continue
                state, said, unreadable = slot.state, slot.said, slot.state == UNREADABLE
                if slot in undecided:
                    seen_state, seen_said = next(verdicts)
                    shown = seen_state if seen_state in (OPEN, CLOSED, CHANGED) else UNKNOWN
                    await trail.record(
                        session, owner, event=BROWSER_EVENT, link=("opportunity", slot.role.id),
                        sentence=f"Checked in the browser: {shown}.",
                        detail=(seen_said or (f"The browser could not look: {browser_why}." if browser_why else None)))
                    if seen_state != UNKNOWN:
                        state, said = seen_state, seen_said
                why = await _settle(session, slot.role, slot.link, state, said, now, unreadable)
                if why is None:
                    out.ready.append(slot.role)
                else:
                    out.skipped.append({"id": str(slot.role.id), "title": slot.role.title,
                                        "company": slot.role.company, "why": why[0].upper() + why[1:]})
    finally:
        await fetch.close()
    if not_checked:
        out.notes.append(f"The closed-posting check couldn't run for {not_checked} "
                         f"{'role' if not_checked == 1 else 'roles'}: {browser_why}.")
    await session.flush()
    out.skipped.sort(key=lambda s: place[s["id"]])  # in the line's order, whichever check said it
    return out


async def browser_checks(session: AsyncSession, owner: str, now: datetime | None = None) -> list[tuple]:
    """The roles whose posting a browser checks (`browser_queue._wanted`): `(role id, link)` for each role still in
    Ready to Launch whose check code could not read (Go's, or the one made on entering Ready), within `BROWSER_LIFE` of
    that check."""
    now = now or datetime.now(UTC)
    rows = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.status == stages.READY_TO_LAUNCH,
        Opportunity.go_check.in_(BROWSER_LOOKS), Opportunity.go_checked_at >= now - BROWSER_LIFE))).all()
    return [(r.id, link) for r in rows if (link := link_of(r)) is not None]


# --- the check when a role comes into Ready to Launch ---------------------------------------------------------------


async def entered_unchecked(session: AsyncSession, owner: str, limit: int | None = None) -> list[Opportunity]:
    """The roles in Ready to Launch that came in after their posting was last checked, or were never checked: the move
    into Ready (`StageMove`, whichever way it was made) is later than `go_checked_at`, or the role holds no check. A
    role Go has opened is not one: Go checked it before its tab opened. In id order, the first `limit` of them."""
    ready = (Opportunity.owner == owner, Opportunity.status == stages.READY_TO_LAUNCH,
             Opportunity.go_opened_at.is_(None))
    entered = (select(StageMove.role_id, func.max(StageMove.at).label("at"))
               .where(StageMove.role_id.in_(select(Opportunity.id).where(*ready)),
                      StageMove.to_status == stages.READY_TO_LAUNCH)
               .group_by(StageMove.role_id).subquery())
    return list((await session.scalars(
        select(Opportunity).outerjoin(entered, entered.c.role_id == Opportunity.id)
        .where(*ready, or_(Opportunity.go_checked_at.is_(None), entered.c.at > Opportunity.go_checked_at))
        .order_by(Opportunity.id).limit(limit))).all())


async def _code_looks(fetch: Fetcher, wanted: list[tuple[Opportunity, str]], today: date) -> dict:
    """The posting check in code (`_code_check`) of each `(role, link)`: hosts side by side, a host's pages one after
    another, so no host is asked for pages faster than the polite reader allows. By role id."""
    by_host: dict[str, list[tuple[Opportunity, str]]] = {}
    for role, link in wanted:
        by_host.setdefault((urlparse(link).hostname or "").lower(), []).append((role, link))
    found: dict = {}

    async def one_host(items: list[tuple[Opportunity, str]]) -> None:
        for role, link in items:
            found[role.id] = await _code_check(fetch, role, link, today)

    await asyncio.gather(*(one_host(items) for items in by_host.values()))
    return found


def _ready_check_words(state: str) -> str:
    """The history line of one check made on entering Ready to Launch, from what it found."""
    if state == OPEN:
        return "Checked on entering Ready to Launch: the posting is still open."
    if state == CHANGED:
        return "Checked on entering Ready to Launch: the posting is up, in other words than the text held."
    if state == UNREADABLE:
        return "Checked on entering Ready to Launch: the app could not read the page, so the browser looks at it next."
    return ("Checked on entering Ready to Launch: the app could not tell from the page; Launch checks it again before "
            "its tab opens.")


async def check_entered(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """Check the posting of the roles that came into Ready to Launch since they were last checked
    (`entered_unchecked`, the first `ENTRIES_PER_CHECK` of them): each is read in code as Go's check reads it
    (`open_state`: the role's own source, compared with the posting held), with no transaction open while a page is
    read. A posting found gone ends the role (`quiet.end_gone`: Gone Quiet, "the posting went", which takes it out of
    Ready to Launch with its reason). A page code could not read (a board's, or one that refuses the app) marks the role
    for the browser (`LOOKING`: `browser_checks` makes it the owner's item, priority 0, at any hour); a page that said
    nothing either way is marked `UNCLEAR` and is not the browser's (Launch checks it again); one that shows the
    posting in other words is flagged on its history as Go flags it (`note_changed`). Otherwise the role is marked as
    found open when it came in (`ENTERED`). Each check says so in a line of the role's history. A role with no link to
    check is marked checked and nothing more (Go says "It has no apply link" when it meets it). Only a role still in
    Ready to Launch and not opened when the checks are written is written (`as_read.lock`). Returns how many roles were
    checked or ended; a caller that gets `ENTRIES_PER_CHECK` back may call again."""
    now = now or datetime.now(UTC)
    roles = await entered_unchecked(session, owner, ENTRIES_PER_CHECK)
    if not roles:
        return 0
    links = {r.id: link for r in roles if (link := link_of(r))}
    looks: dict = {}
    if links:
        await db.release_if_free(session)  # the pages are read with no transaction open
        fetch = polite.make()
        try:
            looks = await _code_looks(fetch, [(r, links[r.id]) for r in roles if r.id in links], owner_today(now))
        finally:
            await fetch.close()
    held = await as_read.lock(session, Opportunity, roles,
                              stands=(Opportunity.status, Opportunity.go_opened_at, Opportunity.go_checked_at))
    done = 0
    for role in roles:
        if role not in held:
            continue
        done += 1
        link = links.get(role.id)
        if link is None:
            role.go_checked_at = now
            continue
        state, said = looks.get(role.id, (UNKNOWN, ""))
        if state == CLOSED and await quiet.end_gone(session, role, link, said):
            continue
        if state == CHANGED:
            await note_changed(session, role, link, said)
        role.go_check = ENTERED if state in (OPEN, CHANGED) else LOOKING if state == UNREADABLE else UNCLEAR
        role.go_checked_at = now
        await trail.record(session, owner, event=READY_CHECK_EVENT, actor="app", who=trail.APP_WHO,
                           link=("opportunity", role.id), sentence=_ready_check_words(state),
                           detail=f"{link}: {said}" if said else link)
    await session.flush()
    return done


# --- the night's pre-check ------------------------------------------------------------------------------------------

_prechecked: dict[str, date] = {}
_running: set[asyncio.Task] = set()


def precheck_due(owner: str, now: datetime | None = None) -> bool:
    """Whether the night's pre-check should run now: from `PRECHECK_AT` to `PRECHECK_UNTIL` in the owner's time, once a
    day."""
    local = (now or datetime.now(UTC)).astimezone(owner_zone())
    return PRECHECK_AT <= local.time() < PRECHECK_UNTIL and _prechecked.get(owner) != local.date()


async def precheck(owner: str, now: datetime | None = None) -> Outcome:
    """Check the next batch and its refill now, whatever was checked before, so Go only re-checks what changed."""
    async with db.session_factory()() as session:
        out = await prepare(session, owner, now, within=timedelta(0))
        await session.commit()
    return out


def start_precheck(owner: str, now: datetime | None = None) -> bool:
    """Start the night's pre-check in the background when it is due (the schedule calls this on each pass)."""
    if not precheck_due(owner, now):
        return False
    _prechecked[owner] = (now or datetime.now(UTC)).astimezone(owner_zone()).date()

    async def run() -> None:
        try:
            await precheck(owner)
        except Exception:  # the night's work goes on without it; Go checks in full instead
            log.warning("the night's pre-check for Go failed", exc_info=True)

    task = asyncio.create_task(run())
    _running.add(task)
    task.add_done_callback(_running.discard)
    return True
