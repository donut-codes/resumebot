"""The server's test of how a site is read (docs/PRD.md 30.3): code, no Claude, no tokens.

A site left to resumebot to test (`read_by` `auto`) is tried from the server once, by the engine, when the request
that adds it or switches it to `auto` asks it to (`settle`; the ask `site.probe`, `work/engine_asks.py`). If the
server can read it, the app reads it (`read_found` `app`); if it cannot, for any reason, the owner's browser does
(`browser`), and the browser reader tests it further on its next run, as it does
any browser site. What counts as working:

- a page with content, no robots rules against it, no sign-in wall: the app ("Its pages are open to apps.");
- anything else is the browser, with the reason as a plain sentence: robots.txt asks not to read it, the site answers
  401 or 407 or the page is a sign-in wall ("It asks for a sign-in."), the site answers 403, 429 or 999 or the page says
  it turned the visit away ("It turns apps away."), or it did not answer, answered an error, or answered nothing.

It goes through the polite fetcher (`polite.py`: robots rules honoured, the same honest user agent, no way round a
block) and the whole test takes at most `PROBE_SECONDS`. A page is a *sign-in wall* when it is reached by a redirect to
a sign-in address, or is titled as a sign-in page, or has a password field and little else on it (a listing page that
merely offers a sign-in link carries far more than `SHORT_PAGE_CHARS`). Services flush; the engine's ask commits.

The night makes the same finding of an employer's own site when it reads the employer's careers page to find its job
system (`resolve.from_pages`): a page the server is refused (robots.txt, or a 403) sends the site to the owner's
browser, as this test does (`hand_refused`), instead of leaving it on and waiting for a project. So does an employer
that posts on a job system the server has no reader for and knows why (one whose job list is behind a route its
robots.txt disallows, or whose postings are read by code but whose list is not): `hand_no_reader`.
"""

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import adapters, ats, consent
from app.jobsearch.employers import resolve
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, Refused, RobotsRefused
from app.jobsearch.models import Employer
from app.jobsearch.postings import site_reads
from app.sites import skills, watch_record, where
from app.sites.models import AUTO, BY_APP, BY_BROWSER, OFF_BY_APP, SEARCH, Project, Site, read_is
from app.timeutil import utc_now

#: The most seconds one test of one site may take, all of it.
PROBE_SECONDS = 15.0
#: A page with no more text than this, with a password field on it, is a sign-in wall.
SHORT_PAGE_CHARS = 1_500
#: How much of a page's text is kept to look for the words of a turned-away visit.
TEXT_KEPT = 4_000

#: The notes the owner reads on the site's row (after "Found out:").
ROBOTS_NOTE = "Its robots rules turn apps away."
SIGN_IN_NOTE = "It asks for a sign-in."
TURNED_AWAY_NOTE = "It turns apps away."
OPEN_NOTE = "Its pages are open to apps."
NO_ANSWER_NOTE = "It didn't answer the server."
EMPTY_NOTE = "It answered the server with an empty page."

#: Answers that ask for a sign-in, and answers that turn a visit away.
SIGN_IN_STATUSES = (401, 407)
TURNED_AWAY_STATUSES = (403, 429, 999)

#: A redirect that ends on a sign-in address: the path has one of these as a whole segment (or a page of that name).
_SIGN_IN_PATH = re.compile(
    r"(^|/)(log-?in|sign-?in|sign_in|log_in|logon|sign-?on|sso|authwall|authenticate|auth)(/|\.[a-z]+$|$)", re.I)
#: A redirect to another host whose first name says it is the sign-in (`login.example.test`).
_SIGN_IN_HOST = re.compile(r"^(log-?in|sign-?in|sso|auth|accounts|idp|id)\.", re.I)
#: A page titled as a sign-in page.
_SIGN_IN_TITLE = re.compile(r"^\W*(sign[\s-]?in|log[\s-]?in|log[\s-]?on|sign[\s-]?on)\b", re.I)
#: What a short page says when it has turned the visit away (a bot check, an access refusal).
_TURNED_AWAY_TEXT = re.compile(
    r"access denied|verify you are (a )?human|are you a robot|unusual traffic|just a moment|captcha|"
    r"enable javascript and cookies to continue", re.I)


def make_fetcher() -> Fetcher:
    """The fetcher the test goes through: the polite one (tests replace this to answer without the network)."""
    return polite.make()


@dataclass(frozen=True)
class Found:
    """What the test came to: `read` is `app` or `browser`, `note` the plain sentence why."""

    read: str
    note: str


class _Page(HTMLParser):
    """What the test needs of a page: its title, the text a person would see, and whether it has a password field."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.text_chars = 0
        self.text = ""
        self.password = False
        self._in: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "title", "noscript", "template"):
            self._in.append(tag)
        if tag == "input" and str(dict(attrs).get("type") or "").lower() == "password":
            self.password = True

    def handle_endtag(self, tag: str) -> None:
        if self._in and self._in[-1] == tag:
            self._in.pop()

    def handle_data(self, data: str) -> None:
        if "title" in self._in:
            self.title += data
        elif not self._in:
            words = " ".join(data.split())
            if words:
                self.text_chars += len(words)
                if len(self.text) < TEXT_KEPT:
                    self.text = f"{self.text} {words}"[:TEXT_KEPT]


def _page(html: str) -> _Page:
    page = _Page()
    try:
        page.feed(consent.without_banners(html, 400_000))  # a cookie banner's words are no wall
        page.close()
    except Exception:  # html.parser is forgiving; a page it chokes on is read as far as it got
        pass
    return page


def _redirected_to_sign_in(reply: httpx.Response) -> bool:
    """Whether a redirect ended on a sign-in address: a sign-in path, or another host that is the sign-in."""
    final = urlparse(str(reply.url))
    first = urlparse(str(reply.history[0].url))
    return bool(_SIGN_IN_PATH.search(final.path)) or (
        (final.hostname or "") != (first.hostname or "") and bool(_SIGN_IN_HOST.search(final.hostname or "")))


def judge(reply: httpx.Response) -> Found:
    """What one answer from the site comes to (the module's rules, in order)."""
    status = reply.status_code
    if status in SIGN_IN_STATUSES:
        return Found(BY_BROWSER, SIGN_IN_NOTE)
    if status in TURNED_AWAY_STATUSES:
        return Found(BY_BROWSER, TURNED_AWAY_NOTE)
    if status >= 500 or status in (408, 425):
        return Found(BY_BROWSER, NO_ANSWER_NOTE)
    if not 200 <= status < 300:
        return Found(BY_BROWSER, f"It answered the server with {status}.")
    body = reply.text or ""
    if not body.strip():
        return Found(BY_BROWSER, EMPTY_NOTE)
    if reply.history and _redirected_to_sign_in(reply):
        return Found(BY_BROWSER, SIGN_IN_NOTE)
    kind = (reply.headers.get("content-type") or "").lower()
    if "json" in kind or "xml" in kind:  # data (a feed of listings) has no wall to be behind
        return Found(BY_APP, OPEN_NOTE)
    page = _page(body)
    short = page.text_chars < SHORT_PAGE_CHARS
    if _SIGN_IN_TITLE.search(" ".join(page.title.split())) or (page.password and short):
        return Found(BY_BROWSER, SIGN_IN_NOTE)
    if short and _TURNED_AWAY_TEXT.search(f"{page.title} {page.text}"):
        return Found(BY_BROWSER, TURNED_AWAY_NOTE)
    return Found(BY_APP, OPEN_NOTE)


async def probe(url: str, fetch: Fetcher) -> Found:
    """Try one site's address from the server and say what that came to. Never raises; never takes more than
    `PROBE_SECONDS`."""
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            reply = await fetch.get(url, patient=False)
    except RobotsRefused as exc:
        return Found(BY_BROWSER, NO_ANSWER_NOTE if exc.unreachable else ROBOTS_NOTE)
    except Refused:  # the site answered 403, or refused us earlier in this run
        return Found(BY_BROWSER, TURNED_AWAY_NOTE)
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL, ValueError):
        return Found(BY_BROWSER, NO_ANSWER_NOTE)
    return judge(reply)


async def settle(session: AsyncSession, site: Site, fetch: Fetcher | None = None) -> Found | None:
    """Test an `auto` site that has not been tested and write what was found on it (`where.set_found`). A site that is
    not `auto`, or was tested already, is left alone (None)."""
    if site.read_by != AUTO or site.read_found is not None:
        return None
    system = await where.adapter_system(session, site)
    if system is not None:  # the app has a reader for its job system: nothing to test from the server
        await where.set_found(session, site, BY_APP, f"{ats.label(system)} is read by the app.")
        return Found(BY_APP, OPEN_NOTE)
    own = fetch is None
    fetch = fetch or make_fetcher()
    await db.release_if_free(session)  # the site is tried with no transaction open; what it showed is written after
    try:
        found = await probe(site.url, fetch)
    finally:
        if own:
            await fetch.close()
    await where.set_found(session, site, found.read, found.note)
    return found


async def hand_refused(session: AsyncSession, owner: str, refused: dict[uuid.UUID, str]) -> int:
    """The server was refused an employer's careers page (`resolve.from_pages` says which, by employer id, and who
    refused it: robots.txt, or the site itself). That is what the test above finds out of a site the owner adds, and it
    has the same answer (docs/PRD.md 30.3): the site is read in the owner's browser, with the reason on its row
    (`where.hand_to_browser`), and it does not wait for a project that would meet the same refusal. Only a site that is
    on and read by the app is handed over: one the owner reads in their browser already is theirs. Returns how many
    sites were handed over. Services flush; the night's step commits."""
    if not refused:
        return 0
    handed = 0
    for site in (await session.scalars(
        select(Site).where(Site.owner == owner, Site.list_name == SEARCH, Site.on.is_(True), read_is(BY_APP),
                           Site.employer_id.in_(list(refused))).order_by(Site.created_at)
    )).all():
        by = refused.get(site.employer_id) if site.employer_id is not None else None
        note = ROBOTS_NOTE if by == resolve.BY_ROBOTS else TURNED_AWAY_NOTE
        handed += int(await where.hand_to_browser(session, site, note))
    return handed


async def hand_no_reader(session: AsyncSession, owner: str) -> int:
    """A watched employer on a job system the server has no reader for, and knows why (`System.why_not`), has its site
    read in the owner's browser (`where.hand_to_browser`), with the reason on its row, and not by a site project the
    server cannot succeed at: that project would take one of the night's few slots (`missions.SITE_PROJECTS_A_NIGHT`)
    from a site it could. The same answer as for a page the server is refused (`hand_refused`).

    Only a site that is on and read by the app, with no recipe the night runs (`site_reads.LEARNED`) and no project at
    work: a recipe the scout learned that reads it is what a system's lack of a reader never meant to stop, and a
    project that has begun is let end. Safe to run as often as asked (the night's read and the Scout mission both do):
    a site handed over is the owner's browser's, and the owner putting it back to the server is handed over again,
    since the server still cannot read it. Returns how many sites were handed over. Services flush; the caller
    commits."""
    handed = 0
    known = [k for k, s in ats.SYSTEMS.items() if s.why_not]
    for site, employer in (await session.execute(
        select(Site, Employer).join(Employer, Site.employer_id == Employer.id)
        .where(Site.owner == owner, Site.list_name == SEARCH, Site.on.is_(True), read_is(BY_APP),
               Employer.gated.is_(False), Employer.ats.in_(known)).order_by(Site.created_at)
    )).all():
        why = ats.why_on(employer.ats) if employer.ats not in adapters.ADAPTERS else None
        if why is None:
            continue
        skill = await skills.current_skill(session, site.id)
        if skill is not None and (skill.recipe or {}).get("kind") in site_reads.LEARNED:
            continue
        if await session.scalar(select(Project.id).where(Project.site_id == site.id, Project.finished_at.is_(None))
                                .limit(1)) is not None:
            continue
        handed += int(await where.hand_to_browser(session, site, why))
    return handed


#: A site the app switched off because it cannot be read is looked at again about this often, a few at a night.
RECHECK_AFTER = timedelta(days=30)
RECHECK_A_NIGHT = 10


async def recheck_unreadable(session: AsyncSession, owner: str, fetch: Fetcher | None = None) -> int:
    """Look again, from the server, at the sites the app switched off because they cannot be read, each about once a
    month (`RECHECK_AFTER`, at most `RECHECK_A_NIGHT` a night, the longest unchecked first). One that is open to
    apps now is switched on again, its reason cleared, and the night reads it; any other stays off with its reason,
    and is looked at again a month on.
    Nothing here tells the owner: a site that cannot be read is nothing they can act on. Returns
    how many were switched on. Services flush; the night's step commits."""
    due = list((await session.scalars(
        select(Site).where(
            Site.owner == owner, Site.list_name == SEARCH, Site.on.is_(False), Site.off_by == OFF_BY_APP,
            or_(Site.off_at.is_(None), Site.off_at < utc_now() - RECHECK_AFTER))
        .order_by(Site.off_at.asc().nulls_first(), Site.created_at).limit(RECHECK_A_NIGHT)
    )).all())
    if not due:
        return 0
    own = fetch is None
    fetch = fetch or make_fetcher()
    back = 0
    try:
        for site in due:
            await db.release_if_free(session)  # each site is written in its own transaction, none held while it loads
            found = await probe(site.url, fetch)
            site.off_at = utc_now()
            if found.read != BY_APP:
                continue
            try:
                await where.switch(session, owner, site.id, True, by=watch_record.BY_PLAY)
            except where.WhereError:  # its host is on the list to avoid now: it stays off
                continue
            site.off_by = site.off_note = site.off_at = None
            site.learn_note, site.learn_tries = None, 0
            back += 1
    finally:
        if own:
            await fetch.close()
    await session.flush()
    return back
