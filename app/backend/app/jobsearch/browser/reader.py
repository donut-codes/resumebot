"""Reading in the app's own browser (docs/PRD.md 30.8): the sites a server cannot read (a
robots rule, a bot block), and the roles found on boards code cannot read (a followed board, a board whose postings
code reads), are the browser's. The work is a queue of small items the app owns (`browser/items.py`): a worker of the
app's pool leases one item at a time, does it in its own tab of one Chrome on the app's own profile, hands back what
it found, and asks again. This module holds what the items are about: the sites, the addresses, the roles each kind of
item is made for, the postings a scan hands in and the runbooks a map hands in.

A site read in the browser is first *mapped* by the Pathfinder: it works out how the site's search and listings are laid
out, proves it by handing in what the first page shows, and hands in a runbook (`hand_skill`, the door's
`browser.skill`) that the app checks and keeps as the site's skill (kind `browser`, `skills.BrowserRecipe`). A worker
then *scans* the site by following that runbook, every 8 hours. A site the Pathfinder could not work out adds one to the
site's `learn_tries`, and at `LEARN_TRIES` the app switches the site off (`where.turn_off_unlearnable`). A scan that
finds the runbook no longer fits retires it (`no_longer_fits`), and the site is mapped again.

A careers page may list no jobs itself and hand off to the employer's own listing on a job system (a payroll or hiring
system and the like: `System.domains`), which a server cannot read and the browser can (docs/PRD.md 37.12). While
mapping, the session declares that portal (`portals` in `hand_in`: [{site, url}]), reads it and hands its postings
in with `site`
still the site's own host, and the portal is recorded on the site (`Site.portal_url`) when the runbook is kept. A
posting is let in from the site's host or from its portal's host and from no other.

Postings go into the posting index (`js_postings`) like a board's, with `ats` "browser". There is no employer record
for a company seen on a job site (that would put thousands of names on the Employers screen), so `employer_id` holds
the id of the site the posting was read on; the employer record is made when a posting becomes a role, as always. A
posting is de-duplicated by its link (the site's own address for it, without tracking) and by company and title, written
any way (`same_role.py`); first seen is kept and last seen moves. A posting that is a role the owner already has, in any
writing or by its link, is known and not stored. Nothing caps how many a scan hands in: the app judges fit, never the
session. `sift.py` treats a browser posting like any other. Services flush; the door commits.

The roles each kind of item is made for (`browser_queue.refresh` reads them):

- `source` (`board_roles`): roles on deck (Second Impression), starred ones, and roles in Followup needed (they hold
  no text, so nothing ranks them and nothing else would ever source them: they go last, `browser_pace.BACKLOG`,
  and only while they would still be fresh a few days on, `postings/chase.py`), found on a
  board, not yet Sourced, with a link to open. Sourced means the app holds the original posting
  (`postings/sourced.py`):
  the employer's own page or job system, a recruiter's page where Apply lands, or, for an Easy Apply, the listing
  itself. A role in First Impression or Cleansed Data has its text and is not sourced before it goes on deck (the
  owner, 2026-10-22), except a board role the deck has picked for its lookup (`models.SOURCE_NEXT`: the
  text it holds is the board's, and the employer's is what is read, so its posting is found at the read-next mark).
- `read` (`to_read_added`, `to_read_in_full`, `to_read_at_source`): the jobs the owner added by link that nothing has
  named yet; roles still in play that hold no text, whose own link is on a site read in the browser (a role before
  Ready the app and the analyst have not yet tried is left to them first, `analysis.py`); and Sourced roles whose
  original posting the app's code could not read. The owner's rule (2026-10-14: a partial result is bad data) is all or
  nothing: a text is kept only whole (`whole.why_not`), never cut (`whole.too_long`).

A role the owner applied to or is in talks about that holds no text of its posting is in both lists (the owner,
2026-11-05, `postings/unbuilt.py`): sourced when it is not Sourced (`board_roles`, `sourcing.source_held`) and read at
its source when it is (`READ_IN_FULL_STAGES`). One in Active Discussion is the owner's own, priority 0, at any hour; one
in Applied is the backlog, in the off hours (`queue._wanted`). A posting that is gone ends the item and leaves the role
where it is (`quiet.end_gone`).
"""


import asyncio
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from sqlalchemy import and_, exists, false, func, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, load_only

from app import db, program
from app.errors import DomainError
from app.jobsearch import as_read, ats, off_turn, stages
from app.jobsearch import limits as count_limits
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import record
from app.jobsearch.models import (
    ADDED_LINK,
    SOURCE_KIND_BOARD_COPY,
    JobPosting,
    Mission,
    MissionRun,
    Opportunity,
    board_link_unread_posting,
    board_unread_posting,
    is_read_first,
    page_read,
    posting_still_sought,
)
from app.jobsearch.postings import chase, hold, raw_copies, same_role, unbuilt, whole
from app.jobsearch.postings import dates as posted_dates
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import session_kinds, titles
from app.models.work import TrailEvent
from app.sites import addresses, avoid, registry, skills, where
from app.sites import lessons as site_lessons
from app.sites.base import LESSON_TEXT_MAX_CHARS
from app.sites.models import BY_BROWSER, SEARCH, Site, SiteRecipe, effective_read
from app.sites.site_check import _tolerance
from app.textutil import squash
from app.timeutil import aware, owner_today, utc_now
from app.trail import service as trail

#: `MissionRun.started_by` of a browser session's run, and `JobPosting.ats` of a posting read in a browser.
BY, ATS = "browser", ats.BROWSER
#: A session's run is a reading one (the browser worker's) or a learning one (the Pathfinder's).
READ, LEARN = "read", "learn"
#: What a session is sent with its first item: the worker's instructions and the shape of a posting (read), and the
#: Pathfinder's and the shape of its runbook (learn).
#: Their names are `session_kinds.py`'s: they are also what the Agents page lists for each kind.
INSTRUCTIONS, TEMPLATE = session_kinds.READER_INSTRUCTIONS, session_kinds.READER_TEMPLATE
LEARNER, SKILL_TEMPLATE = session_kinds.LEARNER_INSTRUCTIONS, session_kinds.SKILL_TEMPLATE
#: The shape of the runbook the Pathfinder writes (sent with each map item as `runbook_template`).
RUNBOOK_TEMPLATE = session_kinds.RUNBOOK_TEMPLATE
#: The most lessons one recipe may bring.
MAX_LESSONS = 3
#: How many map attempts of a site may fail before the app switches it off (docs/PRD.md 37.5).
LEARN_TRIES = 5
#: The most postings one `browser.postings` call takes, and the most portals it may give.
MAX_BATCH, MAX_PORTALS = 25, 10
#: The stages a role is read in full in: the ones still in play before the owner applies, and the ones the owner has
#: applied or is in talks in (`unbuilt.STAGES`, 2026-11-05: a role there that holds no text is built out).
READ_IN_FULL_STAGES = (*stages.PRE_APPLY, *unbuilt.STAGES)
#: Who sourced a role the browser found the original posting of (`sourcing.accept_source`).
SOURCE_BY = "browser"
#: What a text may be made only of and still be blank.
WHITE = " \t\r\n"
#: How far back, in hours, a site's own filter for how recent is asked to reach.
WINDOW_HOURS = 24
#: Links seen within this many days are sent with a scan, newest first, this many for its site.
SEEN_DAYS, SEEN_SHOWN = 14, 120


URL_CHARS = 1_500


#: A posting's fields and the longest each short one may be (longer is a scrape gone wrong, so it is refused).
FIELDS = ("site", "url", "title", "company", "location", "work_mode", "pay", "posted", "snippet", "description",
          "apply_url")


SHORT = {"title": 300, "company": 200, "location": 200, "pay": 200, "posted": 80}


REQUIRED = ("site", "url", "title", "company")


#: The words a site uses for how the work is done, as the index keeps them (remote | hybrid | onsite).
WORK_MODES = {
    "remote": "remote", "work from home": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite",
    "on site": "onsite", "in person": "onsite", "in-person": "onsite", "unknown": None, "": None,
}


class BrowserError(DomainError):
    """The browser door refuses, in plain words (the door turns it into `{"ok": false, "reason": ...}`)."""


# --- addresses and dates ------------------------------------------------------------------------------------------


#: Query parameters that only say how a visitor arrived, dropped from a posting's address.
_TRACKING = re.compile(
    r"^(utm_.*|trk.*|trackingid|refid|ref|referrer|src|source|from|ebp|midtoken|midsig|lipi|lici|eid|fbclid|gclid|"
    r"msclkid|vjs|mc_.*|campaign|cid)$", re.I)


def host_of(url: str | None) -> str:
    """The host of an address (or a bare host), lowercase and without a leading `www.`; empty when there is none."""
    return avoid.host_of(url) or ""  # the one host reader


def web_address(url: str) -> str:
    """`url` as a web address (http or https, with a host), or a BrowserError saying it is not one."""
    text = (url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BrowserError(f"{text[:80]!r} is not a web address (it must start with https://)")
    if len(text) > URL_CHARS:
        raise BrowserError(f"the address is over {URL_CHARS} characters")
    return text


def _system_of(address: str | None) -> str | None:
    """The job system whose own domain an address (or a host) is on, or None (`ats.parse`)."""
    found = ats.parse(address)
    return found.system if found else None


def _arrival(name: str, host: str) -> bool:
    """Whether a query parameter only says how a visitor arrived. A job system's `cid` is not that: it names the
    employer, so on a job system's own address it stays, or the posting's link would lead nowhere (PRD 37.12)."""
    return bool(_TRACKING.match(name)) and not (name.lower() == "cid" and _system_of(host) is not None)


def canonical(url: str) -> tuple[str, str]:
    """(the posting's key, its clean address): https, the host without `www.`, the path without a trailing slash, the
    fragment and the tracking parameters gone, the rest sorted. The key is that address without the scheme,
    lowercase."""
    parsed = urlparse(web_address(url))
    host, path = host_of(url), parsed.path.rstrip("/")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parsed.query) if not _arrival(k, host)))
    tail = f"{host}{path}" + (f"?{query}" if query else "")
    return tail.lower(), f"https://{tail}"


def posted_date(text: str | None, when: date | datetime) -> date | None:
    """The day a site's "posted" text means: an ISO date, "Oct 7, 2026", "yesterday", "3 days ago", "8 hours ago"; None
    when it names none (a posting's age is then counted from when the app first saw it). `when` is the instant of the
    read, which counts hours and minutes across the owner's midnight, or only the day of it. `posted_dates.parse`
    reads it."""
    got = posted_dates.parse(text, when)
    return got.day if got else None


def domain(host: str | None) -> str:
    """The registrable domain of a host or address (`acme.wd5.vendor.example` is `vendor.example`): what the
    browser's pace and its one worker per site are kept by (`browser/items.py`). Empty when there is none."""
    bare = host_of(host)
    return employer_posting.base_domain(bare) if bare else ""


# --- sites --------------------------------------------------------------------------------------------------------


async def browser_sites(session: AsyncSession, owner: str) -> dict[str, list[Site]]:
    """The sites to search that the owner switched on and marked to be read in their browser (Where to look,
    `sites.where.browser_sites`), by host, in the order they were added. A site is never both searched and avoided, but
    one whose host an avoided host covers is left out all the same: the session is never sent to a site to avoid, nor to
    a site whose portal is on one."""
    avoided = await avoid.hosts(session, owner)
    # A site with a live recipe is read by it, never by the browser (the owner, 2026-10-22); a stale one is the
    # browser's again while it is re-recorded.
    live = set((await session.scalars(select(SiteRecipe.site_id).where(
        SiteRecipe.owner == owner, SiteRecipe.site_id.is_not(None), SiteRecipe.retired_at.is_(None),
        SiteRecipe.state == "live"))).all())
    out: dict[str, list[Site]] = {}
    for site in await where.browser_sites(session, owner):
        if site.id in live:
            continue
        if host_of(site.url) and not avoid.on_list([site.url, site.portal_url], avoided):
            out.setdefault(host_of(site.url), []).append(site)
    return out


def site_name(sites: list[Site]) -> str:
    return next((s.label.strip() for s in sites if (s.label or "").strip()), "") or host_of(sites[0].url)


async def pullable_sites(session: AsyncSession, owner: str) -> dict[str, list[Site]] | None:
    """The sites the browser works on, or None when no browser work is handed out (the owner paused the mission, or no
    site is marked to be read in the browser). Peak hours are the pool's own hold."""
    if not (await missions.standing(session, owner, record.BROWSER)).on:  # looked up, never made here
        return None
    return await browser_sites(session, owner) or None


async def browser_recipe(session: AsyncSession, site: Site) -> dict | None:
    """The recipe of the site's current skill when that is a `browser` one (as saved), else None: a site read in the
    browser without one is waiting to be learned (docs/PRD.md 37.2). A skill of another kind is not a recipe the
    browser can follow."""
    skill = await skills.current_skill(session, site.id)
    return skill.recipe if skill is not None and (skill.recipe or {}).get("kind") == "browser" else None


async def to_learn(session: AsyncSession, owner: str, sites: dict[str, list[Site]] | None = None,
                   ) -> dict[str, list[Site]]:
    """The sites the Pathfinder maps: read in the browser, with no runbook or marked to be learned again."""
    sites = sites if sites is not None else await pullable_sites(session, owner)
    if not sites:
        return {}
    return {host: rows for host, rows in sites.items()
            if rows[0].relearn or not await browser_recipe(session, rows[0])}


def _as_sent(recipe: dict) -> dict:
    """A recipe as the reading session is sent it: a runbook recipe without its empty parts (the old shape goes as
    stored)."""
    return {k: v for k, v in recipe.items() if v not in (None, [])} if recipe.get("runbook") else recipe


def _places(profile) -> list[str]:
    """The places to type into a site's location box: the locations the owner would work from (hybrid or in person),
    and Remote when remote work counts, or there is no work location gate, and no place says it."""
    loc = profile.gates.location
    places = list(dict.fromkeys([*loc.hybrid.locations, *loc.in_person.locations]))
    if (loc.remote.on or not profile.gates.checks_location()) and not any("remote" in p.lower() for p in places):
        places.append("Remote")
    return places


def _same_system(name: str | None, system: str | None) -> bool:
    """Whether a runbook's `ats` (a job system's name as the Pathfinder wrote it) is this job system (a key of
    `ats.SYSTEMS`)."""
    if not name or not system:
        return False
    wanted = squash(name).lower()
    return wanted in (system.lower(), squash(ats.label(system)).lower())


def _reuses(rows: list[Site], host: str, recipes: dict[str, dict]) -> dict:
    """A site's entry in a learn plan names a starting point when the job system it sits on (its portal's, else its own
    address's) is the `ats` of another site's runbook in force: `reuses` carries that runbook (docs/PRD.md 37.3)."""
    system = _system_of(recorded_portal(rows)) or next((s for r in rows if (s := _system_of(r.url))), None)
    for other, recipe in recipes.items():
        if other != host and recipe.get("runbook") and _same_system(recipe.get("ats"), system):
            return {"reuses": {"site": other, "ats": recipe["ats"], "runbook": recipe["runbook"]}}
    return {}


# --- a portal the site's careers page hands off to ----------------------------------------------------------------


def recorded_portal(rows: list[Site]) -> str | None:
    """The portal recorded for a site (every row of a host holds the same one), or None."""
    return next((row.portal_url for row in rows if row.portal_url), None)


def portal_address(url, site_host: str, avoided: Iterable[str] = ()) -> str:
    """`url` as the address of a site's portal (docs/PRD.md 37.12): the employer's own listing on a job system that the
    site's careers page hands its openings off to. A BrowserError says what is wrong with it: not a web address, one
    that carries a sign-in or a token, the site itself, a site the owner avoids, or not a job system's address at all (a
    search engine, a job board, another employer's site). Kept as given, less any fragment. Only the query is held
    against `find_secret`, where a token would be, with the employer's public ids taken out of it: the path of a job
    system's page is the employer's id and its words, and looks to `find_secret` like the token it is not."""
    address = web_address(squash(url))
    parsed = urlsplit(address)
    host = host_of(address)
    if parsed.username or parsed.password:
        raise BrowserError("the address carries a user name or password; send the public address")
    if avoid.covers(site_host, host):
        raise BrowserError(f"{host} is the site itself, not a portal it hands off to")
    if avoid.on_list([address], avoided):
        raise BrowserError(f"{host} is a site the owner avoids")
    if _system_of(address) is None:
        names = ", ".join(ats.label(key) for key in ats.SYSTEMS)
        raise BrowserError(f"{host} is not a job system's address ({names}); a portal is the employer's own listing on "
                           "one, reached by the link on the site's own page")
    if reason := addresses.query_secret(parsed.query):
        raise BrowserError(f"the address carries a token or key ({reason}); send the public address")
    return urlunsplit(parsed._replace(fragment=""))


def _declared(
    raw, learning: bool, planned: dict[str, list[Site]], avoided: Iterable[str], problems: list[str],
) -> dict[str, str]:
    """The portals the session declares in a call, {host: address}, each for the site of the item it holds. Only a map
    declares one (a scan that finds the portal gone hands the item back as failed). Every problem is added to
    `problems`, named by its place in the list."""
    if raw in (None, []):
        return {}
    if not learning:
        problems.append("portals is for mapping a site: a scan that finds the portal gone hands its item back failed")
        return {}
    if not isinstance(raw, list) or len(raw) > MAX_PORTALS:
        problems.append(f"portals is a list of at most {MAX_PORTALS} {{site, url}}")
        return {}
    out: dict[str, str] = {}
    for n, item in enumerate(raw, 1):
        host = host_of(item.get("site")) if isinstance(item, dict) else ""
        if host not in planned:
            problems.append(f"portals {n}: site must be one of {', '.join(sorted(planned)) or 'none'}")
            continue
        try:
            out[host] = portal_address(item.get("url"), host, avoided)
        except BrowserError as exc:
            problems.append(f"portals {n}: {exc}")
    return out


# --- the mission and its runs -------------------------------------------------------------------------------------


async def open_runs(session: AsyncSession, owner: str) -> list[MissionRun]:
    """The runs of this mission not yet ended, newest first."""
    return list((await session.scalars(
        select(MissionRun).join(Mission, Mission.id == MissionRun.mission_id)
        .where(Mission.owner == owner, Mission.key == record.BROWSER, MissionRun.outcome == "running")
        .order_by(MissionRun.started_at.desc()))).all())


# --- the roles each kind of item is made for ----------------------------------------------------------------------


def _bases(chosen: dict[str, list[Site]]) -> dict[str, str]:
    """The hosts the session may open a role's link on ({host: the site it belongs to}): each chosen site and its
    portal."""
    bases: dict[str, str] = {}
    for host, rows in chosen.items():
        for base in (host, host_of(recorded_portal(rows))):
            if base:
                bases.setdefault(base, host)
    return bases


def _site_of(link: str | None, bases: dict[str, str]) -> str | None:
    on = host_of(link)
    return next((home for base, home in bases.items() if avoid.covers(base, on)), None)


def _blank(column):  # None, or only white space
    return func.trim(func.coalesce(column, ""), WHITE) == ""


def _order_at(role: Any) -> datetime | None:
    """When a role dates from, for the order its browser work is handed out in (the youngest first): the posting's date,
    else the day it was found, else when the app first saw it. A role, or a row with its `posted`, `found` and
    `created_at`."""
    day = role.posted or role.found
    if day is not None:
        return datetime(day.year, day.month, day.day, tzinfo=UTC)
    return role.created_at


def _youngest():
    """The day a role dates from, for the order its browser work is handed out in: the posting's date, else the day it
    was found, else the owner's day the role was made (`created_day`, stored when it was)."""
    return func.coalesce(Opportunity.posted, Opportunity.found, Opportunity.created_day)


#: The columns of a role the queue's plan reads: what the lists below build their dicts from (`id`, `url`, `board_url`,
#: `source_url`, `title`, `company`, `location`, and `_order_at`'s `posted`, `found` and `created_at`), what they judge
#: it by (`is_read_first`, `unbuilt.in_talks` and `status`; `chase.wanted`'s `target_role`, and
#: `freshness.posted_by_role`'s `posted`, `found` and `board_url`), and the facts the plan keeps on an item beside
#: them (`source_kind`, `sourced_at`).
#: The role's description, its analysis and its gate facts are not read, and not loaded: a plan reads thousands of roles
#: every thirty seconds, as plain rows of these columns (it writes none, and an `Opportunity` instance for each was a
#: third of its cost). A column a list starts to read is added here, or reading it from the role fails.
PLAN_ROLE_COLUMNS = (
    Opportunity.id, Opportunity.url, Opportunity.board_url, Opportunity.source_url, Opportunity.source_kind,
    Opportunity.title, Opportunity.company, Opportunity.location, Opportunity.posted, Opportunity.found,
    Opportunity.created_at, Opportunity.status, Opportunity.favorited_at, Opportunity.ready_asked_at,
    Opportunity.sourced_at, Opportunity.target_role, Opportunity.origin)


async def board_roles(session: AsyncSession, owner: str, chosen: dict[str, list[Site]], *,
                      held: chase.HeldOnce | None = None) -> list[dict]:
    """The roles waiting to be sourced (the owner, 2026-10-15), all of them, youngest first: every role the app goes
    and gets the original posting of (`chase.wanted`: on deck in Second Impression, read first, in Followup needed
    and still fresh enough to be worth the chase, or a board role in First Impression the deck has picked for its
    lookup, `models.SOURCE_NEXT`; not a board role in Followup needed whose own page the app's code reads first,
    `chase.listed`, `listing_roles`) that is not Sourced (the app holds no copy of the original posting)
    and has a link to open, its board posting (`board_url`) else its own link, on any host (a link on no site read in
    the browser, an employer's own page or a board nobody marked, still gets an item). Each is `{role_id, site, link,
    title, company, location}`; `site` the site read in the browser the link is on (its runbook helps), or None. Every
    role the owner applied to or is in talks about that holds no text is listed too (`unbuilt.roles`): `first` for one
    in talks (the owner's own, handed out at any hour), the backlog for one applied to (`queue._wanted`).
    Reads the database and nothing else. The roles are read with `PLAN_ROLE_COLUMNS` only; `held` is the postings'
    dates a plan that reads several lists reads once (`chase.HeldOnce`)."""
    bases = _bases(chosen)
    # Sourcing was the second pass's alone (the owner, 2026-10-22): a role was followed to the employer's
    # own posting when it went on deck, beside the research of its employer, and a role in Followup needed was left out
    # for cost. That left no way
    # out for a role with no text: nothing ranks it, so it never goes on deck, so nothing sources it. A role in Followup
    # needed is sourced again, last in the queue (`backlog`, `browser_pace.BACKLOG`), and code tries it first wherever
    # it can (`takeable.code_first`), but only while it would still be fresh after a few days (the owner, 2026-11-04:
    # `chase.wanted`); an older one waits (only a re-gate closes it, not its age). A job the owner added by link
    # carries its own source (`add_link.py`) and needs no item here. A starred role is first in the queue (the owner,
    # 2026-10-28: a star runs the impressions first, and it needs its posting before either): at any stage before the
    # application (`first`). So is a role the owner moved to Ready to Launch before it was read
    # (`Opportunity.ready_asked_at`): it is read first, as a star is.
    rows = await chase.wanted(
        session, owner, Opportunity.sourced_at.is_(None),
        or_(not_(_blank(Opportunity.board_url)), not_(_blank(Opportunity.url))),
        order_by=(_youngest().desc(), Opportunity.created_at.desc()), listing_exempt=True,
        columns=PLAN_ROLE_COLUMNS, held=held)
    found = []
    # The roles the owner applied to or is in talks about that hold no text are sourced too (`unbuilt`):
    # one in talks is the owner's own, first in the queue, one applied to is the backlog (`queue._wanted`).
    for n, role in enumerate([*rows, *await unbuilt.roles(session, owner, Opportunity.sourced_at.is_(None),
                                                          order_by=(_youngest().desc(), Opportunity.created_at.desc()),
                                                          columns=PLAN_ROLE_COLUMNS)], 1):
        if n % chase.YIELD_EVERY == 0:
            await asyncio.sleep(0)
        link = (role.board_url or "").strip() or (role.url or "").strip()
        if host_of(link):
            first = is_read_first(role) or unbuilt.in_talks(role)
            found.append({"role_id": str(role.id), "site": _site_of(link, bases), "link": link, "title": role.title,
                          "company": role.company, "location": role.location, "order_at": _order_at(role),
                          "first": first, "origin": role.origin,
                          "backlog": role.status == stages.FOLLOWUP_NEEDED and not first})
    return found


async def listing_roles(session: AsyncSession, owner: str, *, held: chase.HeldOnce | None = None) -> list[dict]:
    """The roles whose board page the app's code reads for their text (the owner, 2026-11-05, docs/PRD.md 48.14),
    youngest first: a role in Followup needed, not Sourced, found on a followed board, holding a posting of the nightly
    search
    that nobody has read the page of, not the owner's own (those are sourced), still fresh enough to be worth a page
    (`chase.listing`), and only while the owner reads the board, and, with them, the roles whose employer's text is out
    of reach and that need the board's copy of their posting (`chase.fallback_listing`, the owner, 2026-11-05: the
    roles of the stages a text is asked of, among them those the owner applied to or is in talks about, whatever
    their link: the tracker's import holds its board link as a posting of its own). Each is `{role_id, posting_id,
    link, order_at, first}`: the posting is the role's newest open one that is unread (the rule of
    the board reader's `capture.plan`, here one statement for every role, not a walk over every open board posting: the
    queue is made every 30 seconds; a role's own link is one only for a role that needs the board's copy), and the link
    is its page. `first` is a role in talks: its page is the owner's own, handed out at any hour (`queue._wanted`).
    Reads the database and nothing else. The roles are read with `PLAN_ROLE_COLUMNS` only, and `held` is as in
    `board_roles`."""
    board = registry.board_reader("guest")  # it reads postings, which read this module
    if board is None:  # no reader of a board's pages: there are none to list
        return []
    order = (_youngest().desc(), Opportunity.created_at.desc())
    roles = await chase.listing(session, owner, order_by=order, columns=PLAN_ROLE_COLUMNS, held=held)
    needing = await chase.fallback_listing(session, owner, order_by=order, columns=PLAN_ROLE_COLUMNS, held=held)
    needs = {r.id for r in needing}
    have = {x.id for x in roles}
    roles = [*roles, *[r for r in needing if r.id not in have]]
    if not roles:
        return []
    own_link = (board_link_unread_posting(raw_copies.board_page()) if needs else false())
    # Streamed, `YIELD_EVERY` rows to a fetch: `session.scalars` builds every instance on its first fetch, however the
    # result is cut up after.
    unread = await session.stream_scalars(
        select(JobPosting).options(defer(JobPosting.text)).where(
            JobPosting.opportunity_id.in_([r.id for r in roles]),
            or_(board_unread_posting(board.source_id(owner), raw_copies.board_page()),
                and_(JobPosting.opportunity_id.in_(needs), own_link)))
        .order_by(JobPosting.first_seen.desc(), JobPosting.id).execution_options(yield_per=chase.YIELD_EVERY))
    newest: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
    async for part in unread.partitions(chase.YIELD_EVERY):  # the newest first: a role's first row is the one
        for posting in part:
            if posting.opportunity_id is not None and (page := board.page_id(posting)):
                newest.setdefault(posting.opportunity_id, (posting.id, page))
        await asyncio.sleep(0)
    listed = []
    for n, role in enumerate(roles, 1):
        if role.id in newest:
            listed.append({"role_id": str(role.id), "posting_id": str(newest[role.id][0]),
                           "link": f"{board.VIEW_URL}{newest[role.id][1]}/", "order_at": _order_at(role),
                           "first": role.id in needs and unbuilt.in_talks(role)})
        if n % chase.YIELD_EVERY == 0:
            await asyncio.sleep(0)
    return listed


#: What a role in Fact checking with no link at all says on its card (`Opportunity.browser_note`).
NO_LINK = "it has no link to its posting, so the browser has nothing to open: add the posting's link"


async def note_no_link(session: AsyncSession, owner: str) -> None:
    """A role in Fact checking with no link the browser could open says so on its card, and one that has a link again
    loses the note (only this note: one a worker left after its tries stays)."""
    # Only what it reads and writes is loaded: the link, the note, and the owner and id a change to the role is posted
    # by (`work/queue/hook.py`); a role's description and analysis are never read here, and these are thousands of
    # roles.
    # Streamed, `YIELD_EVERY` rows to a fetch, so the instances are made that many at a time (`session.scalars` builds
    # every one on its first fetch: one long stretch for a large account). Instances, not one `UPDATE`: the hook
    # posts a to-do for each role that changes. The roles are read in a reader's session with no turn (the caller's
    # transaction is ended first, as the queue's refresh does right after this: `db.release_if_free`), and only the
    # roles whose note changes are locked (`as_read.lock`, as they are then) and written: most calls change none.
    await db.release_if_free(session)
    changes: list[tuple[Opportunity, str | None]] = []  # the role as read, and the note it is to hold
    async with off_turn.reading(session) as reader:
        rows = await reader.stream_scalars(select(Opportunity).options(load_only(
            Opportunity.id, Opportunity.owner, Opportunity.board_url, Opportunity.url, Opportunity.browser_note)).where(
            Opportunity.owner == owner, Opportunity.status.in_(stages.PRE_READY), Opportunity.sourced_at.is_(None))
            .execution_options(yield_per=chase.YIELD_EVERY))
        async for part in rows.partitions(chase.YIELD_EVERY):
            for role in part:
                link = (role.board_url or "").strip() or (role.url or "").strip()
                if not host_of(link) and not role.browser_note:
                    changes.append((role, NO_LINK))
                elif host_of(link) and role.browser_note == NO_LINK:
                    changes.append((role, None))
            await asyncio.sleep(0)
    if not changes:
        return
    # The hook reads the owner and id of a role it posts for; a role locked again is loaded with its columns but its
    # text and analysis (never read here), which only the few that change cost, `YIELD_EVERY` at a time with the loop
    # given back between.
    for at in range(0, len(changes), chase.YIELD_EVERY):
        part = changes[at:at + chase.YIELD_EVERY]
        locked = await as_read.lock(session, Opportunity, [role for role, _ in part], stands=(
            Opportunity.board_url, Opportunity.url, Opportunity.browser_note),
            where=(Opportunity.sourced_at.is_(None),),
            defer=(Opportunity.description, Opportunity.analysis, Opportunity.gate_facts))
        for role, note in part:
            if role in locked:
                locked.rows[role.id].browser_note = note
        await asyncio.sleep(0)


def _read_text(owner: str):
    """The roles with a posting that holds a read text (SQL): a text that is not thin and is not a listing's snippet
    (a browser posting never opened). The text of a board posting's own page (the board reader's `capture`) is one for a
    role not yet Sourced, which the app has read the only page of that it will (a browser read of the same board
    link would load it twice), and is not one for a Sourced role: that copy is a job board's, it ranks the role and
    feeds its gates, and the employer's posting is what is read (the owner, 2026-11-05, docs/PRD.md 32.3), so a role
    Sourced to the employer with that copy on its posting is still a role whose text the browser reads. So is the page
    of a role's own board link (the tracker's import, the scout's report, a pasted link: `ats.OF_ROLES`) that the
    capture read (the board reader's `capture`, the board's page copy): its text is on the posting as a job board's
    copy, the same rule as `postings/text._board_copy`."""
    board = registry.board_reader("guest")  # it reads postings, which read this module
    own = JobPosting.employer_id != board.source_id(owner) if board is not None else true()
    board_page = and_(JobPosting.ats.in_(ats.OF_ROLES), page_read(raw_copies.board_page()))  # a role's own link, read
    return exists().where(
        JobPosting.opportunity_id == Opportunity.id, not_(whole.thin_sql(JobPosting.text)),
        or_(JobPosting.ats != ats.BROWSER, JobPosting.detail_at.is_not(None)),
        or_(Opportunity.sourced_at.is_(None),
            and_(own, not_(board_page))))


async def to_read_in_full(session: AsyncSession, owner: str, chosen: dict[str, list[Site]], cap: int,
                          skip: Iterable[str] = ()) -> list[dict]:
    """The postings to read in full this run: roles still in play (`READ_IN_FULL_STAGES`) that hold no text of their
    posting (no description, and no posting of theirs that holds a read text: a listing's snippet is not one), whose own
    link is on one of these sites or on its portal (a job board the session may visit), the youngest first (the
    posting's
    date, else the day it was found, else when the app first saw it), at most `cap` (-1: all). Each is `{role_id, site,
    link, title, company}`. A role before Ready with no research or in the analyst's line is left out: the app and the
    analyst look for the employer's own posting first (`analysis.py`), and only a role still without text is the
    browser's. The role ids in `skip` are left out too: a role waiting to be sourced (`board_roles`) takes its text
    from its original posting and never from the board. So is a role the app gave up on (`analysis.send_back`). Reads
    the database and nothing else: the app's code never fetches a job board."""
    bases = _bases(chosen)
    if not bases:
        return []
    skipped = set(skip)
    read_text = _read_text(owner)
    rows = (await session.execute(select(*PLAN_ROLE_COLUMNS).where(
        Opportunity.owner == owner, Opportunity.status.in_(READ_IN_FULL_STAGES),
        whole.thin_sql(Opportunity.description), not_(read_text), not_(_blank(Opportunity.url)),
        # an Easy Apply is recorded once, and a role Sourced to the board's copy is never read at the board
        or_(Opportunity.source_kind.is_(None), Opportunity.source_kind.not_in(("board", SOURCE_KIND_BOARD_COPY))),
        posting_still_sought(),  # a role the app gave up on (`analysis.send_back`) is not read again
        not_(and_(Opportunity.status.in_(stages.PRE_READY), Opportunity.status != stages.CLEANSED_DATA,
                  or_(Opportunity.analysis_state.is_(None), Opportunity.analysis_state == "waiting")))
    ).order_by(_youngest().desc(), Opportunity.created_at.desc()))).all()
    found = []
    for n, role in enumerate(rows, 1):
        if n % chase.YIELD_EVERY == 0:
            await asyncio.sleep(0)
        site = _site_of(role.url, bases)
        if site is not None and str(role.id) not in skipped:
            found.append({"role_id": str(role.id), "site": site, "link": (role.url or "").strip(), "title": role.title,
                          "company": role.company, "order_at": _order_at(role)})
    return count_limits.upto(found, cap)


async def to_read_added(session: AsyncSession, owner: str, cap: int) -> list[dict]:
    """The jobs the owner added by their link that nothing has named yet (`titles.unnamed`, `add_link.py`), the newest
    first, at most `cap` (-1: all). The app's own read could not name them (a job board's page, which it never reads, a
    page that states no job, a refusal), so the owner's browser does: each is `{role_id, site, link, title, company}`
    with the posting's id as `role_id` and the link as the only address the session opens, and is handed back with
    `browser.texts` with the `title` and `company` the page shows. The link may be on any host. Reads the database and
    nothing else."""
    rows = (await session.scalars(select(JobPosting).where(
        JobPosting.owner == owner, JobPosting.is_open, JobPosting.opportunity_id.is_(None), titles.unnamed(),
        not_(_blank(JobPosting.url))).order_by(JobPosting.first_seen.desc(), JobPosting.id))).all()
    found = [{"role_id": str(p.id), "site": host_of(p.url), "link": (p.url or "").strip(), "title": "", "company": "",
              "added_by_link": True, "order_at": p.first_seen} for p in rows]
    return count_limits.upto(found, cap)


async def to_read_at_source(session: AsyncSession, owner: str, cap: int, skip: Iterable[str] = ()) -> list[dict]:
    """The Sourced roles to read at their own link this run (the owner, 2026-10-15): roles still in play
    (`READ_IN_FULL_STAGES`) that hold a link to their original posting (`source_url`, with its kind) but no text of it
    (no description, and no posting of theirs that holds a read text), the youngest first, at most `cap` (-1: all). The
    link may be on any host: the plan names it, so it is the one address the session opens. Each is `{role_id, site,
    link, title, company}`, `site` the link's host. A role with no `source_url` is never listed, nor one the app gave up
    on (`analysis.send_back`), and the role ids in `skip` (already in `read_in_full`) are left out. Reads the database
    and nothing else."""
    skipped = set(skip)
    read_text = _read_text(owner)
    rows = (await session.execute(select(*PLAN_ROLE_COLUMNS).where(
        Opportunity.owner == owner, Opportunity.status.in_(READ_IN_FULL_STAGES), Opportunity.sourced_at.is_not(None),
        not_(_blank(Opportunity.source_url)), not_(_blank(Opportunity.source_kind)),
        # an Easy Apply's listing was the source, recorded once: never read again (nor the board's copy: its page is
        # read)
        Opportunity.source_kind.not_in(("board", SOURCE_KIND_BOARD_COPY)),
        posting_still_sought(),  # a role the app gave up on (`analysis.send_back`) is not read again
        whole.thin_sql(Opportunity.description), not_(read_text),
    ).order_by(_youngest().desc(), Opportunity.created_at.desc()))).all()
    found = []
    for n, role in enumerate(rows, 1):
        if str(role.id) not in skipped:
            found.append({"role_id": str(role.id), "site": host_of(role.source_url),
                          "link": (role.source_url or "").strip(), "title": role.title, "company": role.company,
                          "order_at": _order_at(role)})
        if n % chase.YIELD_EVERY == 0:
            await asyncio.sleep(0)
    return count_limits.upto(found, cap)


# --- mapping again ------------------------------------------------------------------------------------------------


async def _requeue(session: AsyncSession, owner: str, site: Site, only_the_apps: bool = False) -> tuple[str, str]:
    """One site back in the learn queue: its recipe stays in force for reading (retired only when the new one is
    kept), the tries cleared, a site the app
    switched off as unlearnable switched on (with `only_the_apps`, only a site the app switched off: one the owner
    switched off stays off). A site not read in the browser is left alone."""
    name = site_name([site])
    if effective_read(site) != BY_BROWSER:
        return name, "left alone: it is not read in your browser"
    site.relearn = True  # the recipe in force stays in force for reading until the new runbook is kept
    site.learn_tries, site.learn_tried_at = 0, None
    if not site.on and (site.off_by == "app" or not only_the_apps):
        await where.switch(session, owner, site.id, True)
    return name, "in the learn queue"


async def _search_sites(session: AsyncSession, owner: str) -> list[Site]:
    return list((await session.scalars(select(Site).where(Site.owner == owner, Site.list_name == SEARCH)
                                       .order_by(Site.created_at, Site.url))).all())


async def relearn(session: AsyncSession, owner: str, text: str) -> list[tuple[str, str]]:
    """Put the owner's sites read in the browser whose address or name holds `text` back in the learn queue (the
    owner, 2026-10-15: the board's recipe was retired because its results need a click, and the learner no longer
    treats that as a reason). The recipe in force is retired (kept, not deleted), the learn tries and the time of the
    last try are cleared so the site goes first among the ones never tried, and a site the app switched off for being
    unlearnable is switched on again. Returns (site name, what was done) for each site found, the one the owner's
    words matched, and nothing else is touched."""
    needle = squash(text).lower()
    if not needle:
        raise BrowserError("name the site: part of its address or its name")
    return [await _requeue(session, owner, site) for site in await _search_sites(session, owner)
            if needle in host_of(site.url) or needle in (site.label or "").lower()]


#: The Activity lines that say which Chrome the browser jobs last used; the newest is the one in force.
BROWSER_OWN, BROWSER_PROFILE = "browser_own_chrome", "browser_profile"


async def browser_mode_seen(session: AsyncSession, owner: str, mode: str) -> int:
    """A runner started with SCOUT_BROWSER `mode` (`own` or `profile`). Runbooks mapped on the separate
    profile won't fit the user's everyday Chrome (the owner, 2026-10-16), so on the first start with `own`, or the
    first after a change from `profile`, every browser site goes back to the Pathfinder (`relearn_all`: the runbook in
    force reads until each new one is kept), and an Activity line says so; that line is also how the change is
    remembered.
    A change back to `profile` is noted and relearns nothing. Five runners start at once: one does it. Returns how many
    sites went back to be learned."""
    program.require("engine")
    if mode not in ("own", "profile"):
        return 0
    await db.transaction_lock_or_busy(
        session, f"browser_mode:{owner}", "another runner is checking which browser the jobs use; nothing was changed")
    last = await session.scalar(select(TrailEvent.event).where(
        TrailEvent.owner == owner, TrailEvent.event.in_((BROWSER_OWN, BROWSER_PROFILE)))
        .order_by(TrailEvent.at.desc(), TrailEvent.id.desc()).limit(1))
    if mode == "profile":
        if last == BROWSER_OWN:
            await trail.record(session, owner, event=BROWSER_PROFILE,
                               sentence="The browser jobs now use the separate browser profile.")
        return 0
    if last == BROWSER_OWN:
        return 0
    done = await relearn_all(session, owner)
    n = sum(1 for _, what in done if what == "in the learn queue")
    await trail.record(session, owner, event=BROWSER_OWN, sentence=(
        f"The browser jobs now use your own Chrome: {n} {'site goes' if n == 1 else 'sites go'} back to "
        "the Pathfinder to be learned again."))
    return n


async def relearn_all(session: AsyncSession, owner: str) -> list[tuple[str, str]]:
    """Every site read in the owner's browser back in the learn queue, so the Pathfinder writes each a runbook in the
    new shape; the old recipes are retired and kept. Sites not read in the browser are named and left alone, and a site
    the owner switched off stays off."""
    return [await _requeue(session, owner, site, True) for site in await _search_sites(session, owner)]


# --- handing postings in ------------------------------------------------------------------------------------------


def _text(value) -> str:
    """Long text kept with its line breaks and runs of blank lines folded, and kept whole: a posting is never cut
    (docs/PRD.md 39.29). One no reply could hold is refused where the posting is read (`_clean`)."""
    lines = [" ".join(line.split()) for line in str(value or "").replace("\r", "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _clean(n: int, raw, sites: dict[str, list[Site]], portals: dict[str, str]) -> tuple[dict | None, list[str]]:
    """One posting as the app keeps it, or every problem with it (named by its place in the batch). `portals` holds the
    portal of each site that has one ({site host: address}): a posting is let in from the site's host or its
    portal's."""
    name = f"posting {n}"
    if not isinstance(raw, dict):
        return None, [f"{name}: send each posting as an object with {', '.join(REQUIRED)}"]
    problems = []
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        problems.append(f"{name}: unknown field {', '.join(unknown)} (the fields are {', '.join(FIELDS)})")
    short = {k: squash(raw.get(k)) for k in (*SHORT, "site", "url", "apply_url", "work_mode")}
    for key in REQUIRED:
        if not short[key]:
            problems.append(f"{name}: {key} is required")
    for key, most in SHORT.items():
        if len(short[key]) > most:
            problems.append(f"{name}: {key} is over {most} characters; send what the page shows, not the whole page")
    site = (sites.get(host_of(short["site"])) or [None])[0] if short["site"] else None
    if short["site"] and site is None:
        parent = next((h for h, p in portals.items() if avoid.covers(host_of(p), host_of(short["site"]))), None)
        problems.append(f"{name}: {short['site'][:60]!r} is not one of the sites to read (they are "
                        f"{', '.join(sorted(sites)) or 'none'})"
                        + (f"; it is the portal of {parent}: send site as {parent} and keep the portal's address in url"
                           if parent else ""))
    ext_id = url = apply_url = None
    if short["url"]:
        try:
            ext_id, url = canonical(short["url"])
            if site is not None:
                home, portal = host_of(site.url), host_of(portals.get(host_of(site.url)))
                if not avoid.covers(home, host_of(url)) and not (portal and avoid.covers(portal, host_of(url))):
                    on = f"{home} or on its portal {portal}" if portal else home
                    problems.append(f"{name}: url is not on {on}; send the posting's own address")
        except BrowserError as exc:
            problems.append(f"{name}: url: {exc}")
    if short["apply_url"]:
        try:
            apply_url = web_address(short["apply_url"])
        except BrowserError as exc:
            problems.append(f"{name}: apply_url: {exc}")
    mode = WORK_MODES.get(short["work_mode"].lower(), "?")
    if mode == "?":
        problems.append(f"{name}: work_mode {short['work_mode'][:30]!r} is not remote, hybrid, onsite or unknown")
    snippet, description = _text(raw.get("snippet")), _text(raw.get("description"))
    problems += [too_long for text, what in ((snippet, "snippet"), (description, "description"))
                 if (too_long := whole.too_long(text, f"{name}: {what}"))]
    if problems:
        return None, problems
    return {
        "site": site, "ext_id": ext_id, "url": url, "apply_url": apply_url, "title": short["title"],
        "company": short["company"], "location": short["location"] or None, "work_mode": mode,
        "comp": short["pay"] or None, "posted_text": short["posted"],
        "snippet": snippet, "description": description, "raw": raw,
    }, []


def _fill(posting: JobPosting, p: dict, now: datetime, reopen: bool) -> hold.Put | None:
    """Bring a posting already in the index up to date with a sighting: what it lacked is filled, its link is kept, a
    full description replaces a snippet. First seen is never moved; last seen is. A posting closed on its own board
    is reopened only by a sighting under its own link, not by another site still listing it. The text it gives the
    posting is not put here: it is returned as a `Put`, for the caller's `hold.hold` to put with the raw copy that holds
    it (None when the sighting gave none)."""
    posting.last_seen = now
    if reopen:  # seen under its own link: open, whether it was closed or Not checked (docs/PRD.md 39.8)
        posting.closed_at = posting.not_checked_at = None
    for field, value in (("location", p["location"]), ("work_mode", p["work_mode"]), ("comp", p["comp"]),
                         ("apply_url", p["apply_url"])):
        if value and not getattr(posting, field):
            setattr(posting, field, value)
    posted = posted_date(p["posted_text"], aware(now) or utc_now())
    if posted and posting.posted is None:
        posting.posted = posted
    fuller = p["description"] and (
        not posting.text or posting.detail_at is None or len(p["description"]) > len(posting.text))
    put = None
    if fuller:
        put = hold.Put(p["description"], now)
    elif p["snippet"] and not posting.text:
        put = hold.Put(p["snippet"], hold.KEEP)
    if posting.posted is None:  # the listing's words ("Posted 3 days ago") or the page's, counted from this read
        posting.posted = posted_dates.day_of(put.text if put else posting.text, now,
                                             owner_today(aware(posting.first_seen)))
    if p["url"] not in (posting.links or []):
        posting.links = [*(posting.links or []), p["url"]]
    return put


async def _held(session: AsyncSession, owner: str, clean: list[dict]) -> dict[tuple, JobPosting]:
    """The postings the index already holds under the links of a clean batch, by (site, ext_id): one query for the
    whole batch, so a known link is dropped before the same-role and owner-role lookups that a new one needs."""
    if not clean:
        return {}
    rows = await session.scalars(select(JobPosting).where(
        JobPosting.owner == owner, JobPosting.employer_id.in_({p["site"].id for p in clean}),
        JobPosting.ext_id.in_({p["ext_id"] for p in clean})))
    wanted = {(p["site"].id, p["ext_id"]) for p in clean}
    return {(row.employer_id, row.ext_id): row for row in rows.all() if (row.employer_id, row.ext_id) in wanted}


async def _store(session: AsyncSession, owner: str, p: dict, now: datetime,
                 roles: same_role.Known[uuid.UUID]) -> tuple[str, JobPosting | None]:
    """Put one posting in the index. ("new", the posting) when it is new; ("known", the posting or None) when its
    link, or its company and title in any writing, are already there (a role the owner already has, in any writing or
    by its link, is known, not stored again: `roles` holds them)."""
    site = p["site"]
    key = match_key(p["company"], p["title"])
    posting = await session.scalar(select(JobPosting).where(
        JobPosting.owner == owner, JobPosting.employer_id == site.id, JobPosting.ext_id == p["ext_id"]))
    own = posting is not None
    if posting is None:
        posting = await same_role.same_posting(session, owner, p["company"], p["title"], key)
    if posting is not None:
        put = _fill(posting, p, now, reopen=own)
        await hold.hold(session, posting, hold.item(p["raw"]), put=put, now=now)
        return "known", posting
    if roles.find(p["company"], p["title"], (p["url"], p["apply_url"]), key=key) is not None:
        return "known", None
    today = owner_today(aware(now))  # the owner's day of the read, which "3 days ago" is counted back from
    posting = JobPosting(
        owner=owner, employer_id=site.id, ats=ATS, ext_id=p["ext_id"], match_key=key, company=p["company"],
        title=p["title"], location=p["location"], work_mode=p["work_mode"], comp=p["comp"],
        posted=posted_date(p["posted_text"], aware(now) or utc_now()), url=p["url"], apply_url=p["apply_url"],
        text=p["description"] or p["snippet"] or None, detail_at=now if p["description"] else None,
        first_seen=now, last_seen=now, links=[p["url"]])
    if posting.posted is None:
        posting.posted = posted_dates.day_of(posting.text, now, today)
    session.add(posting)
    await session.flush()
    await hold.hold(session, posting, hold.item(p["raw"]), now=now)  # its text was set with it
    return "new", posting


async def learn_failed(session: AsyncSession, rows: list[Site], why: str) -> None:
    """A map of these site rows ended without a runbook: each adds one to its `learn_tries` and keeps why as its
    `learn_note`; one that reaches `LEARN_TRIES` is switched off by the app (`where.turn_off_unlearnable`)."""
    for site in rows:
        site.learn_tries = (site.learn_tries or 0) + 1
        site.learn_note = why
        site.learn_tried_at = datetime.now(UTC)
        if site.learn_tries >= LEARN_TRIES:
            await where.turn_off_unlearnable(session, site, why)


async def no_longer_fits(session: AsyncSession, rows: list[Site], why: str) -> None:
    """A scan found these site rows' pages do not match their runbook: the version in force is retired, why is kept as
    the site's `learn_note`, and `learn_tries` is left alone (it counts attempts to map, not fits). The site is then the
    Pathfinder's again."""
    for site in rows:
        skill = await skills.current_skill(session, site.id)
        if skill is not None and (skill.recipe or {}).get("kind") == "browser":
            await skills.retire_skill(session, skill)
        site.learn_note = why


def _on_portal(p: dict, portals: dict[str, str]) -> bool:
    """Whether a clean posting was read on its site's portal and not on the site itself."""
    home, read_on = host_of(p["site"].url), host_of(p["url"])
    portal = portals.get(home)
    return bool(portal) and avoid.covers(host_of(portal), read_on) and not avoid.covers(home, read_on)


def _next_words(open_these: list[str]) -> str:
    """What the session is told to do next after a batch of postings."""
    if open_these:
        return ("Open each link in open_these, read the posting, and hand it in again with its description, only "
                "when it is the whole posting. Then go on with the site's next page or search.")
    return "Go on with the site's next page or search; when the runbook is done, hand the item back."


async def hand_in(session: AsyncSession, owner: str, entry: MissionRun, host: str, postings, *, learning: bool = False,
                  portals=None, now: datetime | None = None) -> dict:
    """Take a batch of postings the session read on the site of the item it holds (`host`: a scan, or a map proving its
    runbook), into the session's run (`entry`). Nothing caps how many: the app judges fit, never the session. While
    mapping (`learning`), `portals` ([{site, url}]) says the site's careers page hands its openings off to the
    employer's own listing on a job system (docs/PRD.md 37.12): the postings read there are let in with that site,
    which is kept on the run until a runbook is kept for it. A batch with a bad posting is refused whole and every
    problem is named. Returns what was new and known, and the new postings worth opening for their whole text. A
    whole description that is not whole (`whole.why_not`) is not kept, not even as a snippet's stand-in."""
    now = now or datetime.now(UTC)
    if postings in (None, ""):
        postings = []
    if not isinstance(postings, list):
        raise BrowserError("postings is a list of postings")
    if len(postings) > MAX_BATCH:
        raise BrowserError(f"send at most {MAX_BATCH} postings at a time")
    if not postings and not portals:
        raise BrowserError("nothing to hand in: send postings (or, mapping a site, the portal it hands off to)")
    every = await browser_sites(session, owner)
    if host not in every:
        raise BrowserError(f"{host} is no longer a site read in the browser: hand the item back")
    sites = {host: every[host]}
    started = dict(entry.started or {})
    problems: list[str] = []
    declared = _declared(portals, learning, sites, await avoid.hosts(session, owner) if portals else (), problems)
    kept = {**(started.get("portals") or {}), **declared}
    homes = {h: portal for h, rows in sites.items() if (portal := kept.get(h) or recorded_portal(rows))}
    clean = []
    for n, raw in enumerate(postings, 1):
        item, found = _clean(n, raw, sites, homes)
        problems += found
        if item is not None:
            clean.append(item)
    if problems:
        raise BrowserError("; ".join(problems))
    counts = {"new": 0, "known": 0}
    open_these: list[str] = []
    seen_here: set[tuple] = set()
    held = await _held(session, owner, clean)  # the postings already held under these links, in one query
    fresh = [p for p in clean if (p["site"].id, p["ext_id"]) not in held]
    roles: same_role.Known[uuid.UUID] = await same_role.known_roles(session, owner) if fresh else same_role.Known()
    not_kept: list[dict] = []
    for p in clean:
        why = whole.why_not(p["description"]) if p["description"] else None
        if why:  # all or nothing: a partial text is bad data, so it is not kept, not even in a snippet's place
            not_kept.append({"url": p["url"], "why": why})
            p["description"] = ""
    for p in clean:
        tag = (p["site"].id, p["ext_id"])
        if tag in seen_here:
            counts["known"] += 1
            continue
        seen_here.add(tag)
        if tag in held:  # known by its own link: seen again, and nothing else looked up
            put = _fill(held[tag], p, now, reopen=True)
            if put is not None:  # it gave the posting a text: its raw copy is kept with it, so the copy the stages
                await hold.hold(session, held[tag], hold.item(p["raw"]), put=put, now=now)  # read holds that text
            counts["known"] += 1
            continue
        status, posting = await _store(session, owner, p, now, roles)
        counts[status] += 1
        if not p["description"] and status == "new" and posting is not None:
            open_these.append(posting.url)
    from_here: dict[str, int] = dict(started.get("by_site") or {})  # the postings handed in from each site, this run
    on_portal: dict[str, int] = dict(started.get("by_portal") or {})  # and, of those, read on the site's portal
    for h, address in declared.items():
        if address != (started.get("portals") or {}).get(h):
            on_portal.pop(h, None)  # a portal other than the one before: what was read there is counted afresh
    for p in clean:
        from_here[host] = from_here.get(host, 0) + 1
        if _on_portal(p, homes):
            on_portal[host] = on_portal.get(host, 0) + 1
    started.update({"handed": int(started.get("handed") or 0) + len(clean),
                    "new": int(started.get("new") or 0) + counts["new"], "by_site": from_here, "portals": kept,
                    "by_portal": on_portal, "last_call": now.isoformat()})
    entry.started = started
    await session.flush()
    reply: dict = {"ok": True, "handed": len(clean), **counts, "open_these": open_these}
    if not_kept:
        reply["not_kept"] = not_kept
    if declared:
        reply["portals"] = declared
    reply["next"] = _next_words(open_these)
    if declared and not clean:
        reply["next"] = ("Portal noted. Read the first page of results there and hand those postings in, with site as "
                         "the item gave it and url on the portal.")
    return reply


# --- naming a job the owner added by its link ---------------------------------------------------------------------


async def _added_posting(session: AsyncSession, owner: str, posting_id: uuid.UUID) -> JobPosting | None:
    """The posting a read plan listed for a job the owner added by its link (`to_read_added`): the owner's, no role
    yet, not named. None for any other id (it is a role's)."""
    posting = await session.get(JobPosting, posting_id)
    if posting is None or posting.owner != owner or posting.opportunity_id is not None:
        return None
    return posting if posting.ats == ats.OWNER and posting.company == ADDED_LINK else None


async def _name_added(session: AsyncSession, owner: str, posting: JobPosting, item: dict, text: str,
                      now: datetime) -> bool:
    """Name a job the owner added by its link from what the session read on its page: the `title` and `company` it sent
    (both, or nothing is done), and the description when it is the whole posting (`text` is empty when the session did
    not say it was). The posting then goes through the code gates again under its name (`sift.sift` with `only`), where
    the first run, at the add, knew only the link. Returns whether it was named. Services flush."""
    from app.jobsearch.browser import add_link  # both read this module's neighbours; imported here, never at the top
    from app.jobsearch.gating import sift  # both read this module's neighbours; imported here, never at the top

    facts = {"title": squash(item.get("title")), "company": squash(item.get("company")), "text": text}
    if not add_link.name_posting(posting, facts, now):
        return False
    posting.filtered_at = posting.filtered_by = posting.route = posting.route_why = None
    await session.flush()
    await sift.sift(session, owner, await profile_store.current(session, owner), now, only=[posting.id])
    return True


# --- handing in a runbook -----------------------------------------------------------------------------------------


def _recipe_problems(host: str, handed: int, recipe, page_says, lessons, portal: str | None = None) -> list[str]:
    """Everything wrong with a recipe sent for a site (docs/PRD.md 37.4), each in words: no posting from the site was
    handed in this run, the count is not what the page says, the lessons are not up to three plain sentences, or the
    recipe is not a valid `browser` recipe (every reason `skills.recipe_problems` finds). For a recipe read on the
    site's `portal` (docs/PRD.md 37.12), `handed` is the postings read there, and it is the portal that is named."""
    problems = []
    where = f"the portal {host_of(portal)} of {host}" if portal else host
    if not handed:
        problems.append(f"no posting from {where} has been handed in this run: prove the recipe first by handing in "
                        "what the first results page shows with browser.postings"
                        + (f" (site {host}, url on the portal)" if portal else "") + ", then send the recipe")
    if page_says is not None:
        if isinstance(page_says, bool) or not isinstance(page_says, int) or page_says < 0:
            problems.append("page_says is the number of results the page states, a whole number; leave it out when "
                            "the page states none")
        elif handed and abs(handed - page_says) > _tolerance(page_says):
            problems.append(f"the page says {page_says} results but {handed} were handed in from {where}: hand in what "
                            "the page shows, or leave page_says out when that number is not on this page")
    if lessons is not None and (not isinstance(lessons, list) or len(lessons) > MAX_LESSONS):
        problems.append(f"lessons is a list of at most {MAX_LESSONS} plain sentences")
    else:
        for n, text in enumerate(lessons or [], 1):
            if not isinstance(text, str) or not squash(text) or len(squash(text)) > LESSON_TEXT_MAX_CHARS:
                problems.append(f"lesson {n}: one plain sentence, under {LESSON_TEXT_MAX_CHARS} characters")
    if isinstance(recipe, dict) and recipe.get("kind") != "browser":
        problems.append("recipe: kind must be browser (see the response template)")
    else:
        problems += [f"recipe: {found}" for found in skills.recipe_problems(recipe)]
    return problems


async def hand_skill(session: AsyncSession, owner: str, entry: MissionRun, host: str, site, recipe, page_says=None,
                     lessons=None, now: datetime | None = None) -> dict:
    """The runbook the Pathfinder worked out for the site of the map item it holds (`host`, docs/PRD.md 37.4), kept as
    the site's next skill version when it is sound: this run must already have handed in a posting from the site (the
    evidence is real), the number handed in must be within the site check's tolerance of `page_says` when the page
    states one, and the recipe must be a valid `browser` recipe. Anything wrong is refused with every reason named,
    and the session may fix it and send it again. A runbook for a site whose openings are on its portal (docs/PRD.md
    37.12) is proven on the portal: the postings that count, and the number compared with `page_says`, are the ones
    handed in from the portal's host. On acceptance the version before it is retired, the lessons (up to three) are
    recorded for the site's kind, the site's `learn_tries` and `learn_note` are cleared, its `portal_url` becomes the
    portal the runbook was proven on (or none), and the run notes the site as learned."""
    now = now or datetime.now(UTC)
    named = host_of(site) if isinstance(site, str) else ""
    if named != host:
        raise BrowserError(f"site must be {host}, the site of the item you hold")
    sites = await browser_sites(session, owner)
    if host not in sites:
        raise BrowserError(f"{host} is no longer a site read in the browser: hand the item back")
    started = dict(entry.started or {})
    rows = sites[host]
    declared = started.get("portals") or {}
    portal = declared.get(host) or recorded_portal(rows)
    via = int((started.get("by_portal") or {}).get(host) or 0)
    on_portal = bool(portal) and (host in declared or via > 0)  # the runbook is for the portal's pages, not the site's
    handed = via if on_portal else int((started.get("by_site") or {}).get(host) or 0)
    problems = _recipe_problems(host, handed, recipe, page_says, lessons, portal if on_portal else None)
    if problems:
        raise BrowserError("; ".join(problems))
    versions = []
    for row in rows:  # every row of the host reads the same way
        kept = await skills.save_skill(session, owner, row.id, recipe, None)
        versions.append(kept.version)
        for old in await skills.skills_of_site(session, row.id):
            if old.id != kept.id and old.retired_at is None:
                await skills.retire_skill(session, old)
        row.learn_tries, row.learn_note, row.relearn = 0, None, False
        row.portal_url = portal if on_portal else None
    await site_lessons.record_learned(
        session, owner, rows[0].kind, lessons or [], f"learned in the browser {owner_today(now).isoformat()}")
    started["learned"] = {**(started.get("learned") or {}), host: handed}
    started["last_call"] = now.isoformat()
    entry.started = started
    await session.flush()
    reply = {"ok": True, "kept": True, "version": versions[0],
             "next": "Kept. Hand the item back with browser.hand_back, result done."}
    if on_portal:
        reply["portal"] = portal
    return reply
