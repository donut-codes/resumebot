"""Where to look (docs/PRD.md path 29, step 8): the sites the app reads, the owner's switch on each, and what is true of
each now. Written for this app.

- **A site for every employer with an address** (`seed_employer_sites`): its careers page, else its job board. One that
  posts on a job system the app can already read (`Employer.ats` with a complete board) is given that system's adapter
  as version 1 of its skill (`{"kind": "adapter", "ats", "board"}`, made by no project): working with nothing to learn.
  Safe to run as often as asked: it never adds a second skill or a second site and sets no switch of its own (the
  reader it saves switches a site nobody has decided about on, `runbook_saved`); it ends by settling the setting
  "Watch the job board of every employer with a role in play" (`auto_watch.settle`, which does nothing while that is
  off), so an employer that gains a board is watched as soon as its site is made.
  A site the app made for an employer follows it off a board the open feeds guessed (`reaim_sites`): moved to the new
  board's page and given its reader, the guess's reader retired, its switch as it was (bar `runbook_saved`'s rule).
- **Only a site that is switched on is read.** A company's site starts OFF, and is switched on the moment it holds a
  runbook (`runbook_saved`: a reader for its job system, a skill a project learned or a recipe kept for its careers
  page; the owner, 2026-11-05: it costs almost nothing); one for an employer the owner already watched
  (`Employer.check_directly`) starts ON; a site the owner adds starts ON (adding is switching on). The switch is
  the site's `on` (`switch`). The night's read reads the employers whose site is on (`watched_employers`). The owner
  switches a site on and off; the app does too for an employer with a role in play, when the owner asked it to
  (`auto_watch.py`), and keeps who set the watch last (`watch_record.py`) so it never undoes what the owner set.
- **A project is never started here.** Switching on only records `turned_on_at`; the project that learns a site is
  started in the night's Scout mission (`autostart.ensure_projects`, `kinds/scout.py`).
- **A skill that breaks** (`retire_broken`, docs/PRD.md 29.4): the night's read retires the version in force when the
  site refuses it, changed shape or answers nothing where it had roles; the site then waits for tonight's mission to
  start its next project, and the read leaves it alone meanwhile (`unreadable_employer_ids`).
- **What the owner sees** (`listing`): each site with a state, in plain words: `off`, `waiting` (on, no skill, no
  project yet, or its skill broke after its last project ended: it starts tonight), `learning` (its project is open),
  `needs_you` (an open Inbox task belongs to its project), `working` (it has a skill that works), `stopped` (its
  project is paused, or ended without a skill; `needs` says what to do), `browser` (on, and read in the owner's own
  browser). Services flush; the route commits.
- **What is true now, in words** (`status`): the state said as the owner reads it, with what the missions behind it are
  doing. A site with a working skill is "Read nightly at 1:00 AM" (the time of the mission `read_boards`), or
  "Waits: the reading mission is paused" while that mission is paused; one waiting for its project is "Starts tonight
  at 1:00 AM" (the mission's time) while it is one of the sites to learn a night (`Settings.site_projects_per_night`,
  3 at first; -1: all) the mission `scout_boards` starts tonight, in its order (`autostart.start_queue`), else "Waits
  its turn: starts in about 2 nights"; either is "Waits:
  the scouting mission is paused" while the mission `scout_boards` (which starts site projects) is paused; one
  read in the owner's browser is "Read in your browser: not set up yet" until the mission `read_browser` has run, then
  "Read in your browser: waiting to be learned" until a session has learned a recipe for it (docs/PRD.md 37), "Learning
  in your browser: <what the last attempt found>" after an attempt that failed or a recipe that stopped fitting, and
  "Read in your browser: last run Oct 7" once it has one. An employer's site on a job system the owner switched off
  (`job_systems.py`) is "Off: Acme Jobs is switched off", because the night's read skips it (for a system no screen
  offers while it is off, `ats.NOT_OFFERED`: "Off: resumebot doesn't read Acme Jobs unless it is switched on by hand").
  One whose employer
  posts on a system the app can't read, and knows why (`System.why_not`), is handed to the
  owner's browser (`read_probe.hand_no_reader`: a project could not succeed and would take one of the night's
  slots); until it is, its row says "Acme Jobs can't be read: its job list is behind a route its robots.txt disallows.
  Goes to your browser tonight" (the Job systems section says the same of the system). The logic is here, so the
  page only shows the words.
- **Two lists** (docs/PRD.md 30.3). *Sites to search* are the sites the owner added and every employer site that is
  switched on (`page` leaves the employer sites that are off to the Employers screen, which switches them on);
  *sites to avoid* are the owner's (`avoid.py` matches a link to one by host). An avoided site is never on, never read,
  has no skill and no project. A site is never on both lists: adding one to a list when its host is the host of one on
  the other (or a subdomain of it, either way round) is refused in words, as is switching on an employer site whose
  host is avoided; an employer whose address is on an avoided host gets no site.
- **A site read on its own, or through its job system** (`through_systems`). An employer's site that works through the
  built-in reader of a job system the app can read (the employer's board is complete and the
  site's skill in force is that system's reader, not a learned recipe) belongs to that system, which is switched in
  *Job systems*; `page` leaves it out while the system is on. Everything else stays in *Sites to search*: the owner's
  own sites, an employer's with a learned recipe, one read in the owner's browser, one that needs the owner or is
  learning, waiting or stopped, one on a system with no reader, and any whose system the owner switched off.
- **Roles found at a source** (`roles_by_source`, the one rule): a site's `roles`, a job system's `roles` and the lists
  behind them (`roles_of_site`, `job_systems.roles_of`) are the same thing: the distinct roles that reached Found (not
  gated out) and were found there, shown as 0 when there are none. A role is found at a source when EITHER one of the
  source's postings became it OR the link it was found at points there. A job system's postings are those it read
  (`JobPosting.ats` is its key); a site's are those it read itself: an employer's own-site postings (the employer's,
  and not from a job system: `ats` is `own_site`) and any posted under the site's own id (a job site's, or one read in
  the owner's browser). The link is the role's `url`, else its `apply_url`: it points at a site when its host is the
  site's host (`www.` ignored) or a subdomain of it and, when the site's address has a path, it starts with that path
  (a shared job-system host such as `recruiting.example-ats.test/AcmeCo`); it points at a job system when
  `ats.parse` says it is on that system. A role can be found at both its site and its job system.
- **How a site is read** (`read_by`, docs/PRD.md 30.3): `auto` (resumebot tests the site from the server once, when
  it is added or switched to `auto`, `read_probe.py`: it works, so the app reads it; it does not, for any reason, so the
  browser does), `app` (the night's server read) or `browser`. `models.effective_read` is the one rule for who reads a
  site (an `auto` site is read by what was found, `read_found`); every place that asks uses it. A site read in the
  browser is not read by the night (`watched_employers`), gets no project (`create_project`; one already open is paused
  until the site is set back) and shows `browser` instead of the learning and working states. `browser_sites` is what
  the browser reader asks for. The reason a site is read there is its `read_note` ("Found out: ..."): what the test
  found, or why it was handed over (`hand_to_browser`); a site handed over before the row kept it says what its project
  ended with (`where_view._read_note`). The night's read of an employer's careers page makes the same test: a page the
  server is refused (robots.txt, a 403) sends that employer's site to the browser (`read_probe.hand_refused`).
- **A site that no means can read is switched off by the app** (`turn_off_unreadable`; the owner decided that a site
  where scraping or browser traffic doesn't work is turned off by default and the user notified, and that no cycles go
  on something that will fail), and only then: neither the server
  nor the browser can read it. It happens when the browser reader reports a site it could not read (`hand_in`'s
  `blocked`, after the server could not either), when the scout says a site refuses to be read (`task.escalate` with
  the reason `cannot_be_read`) and when a plan says it asks not to be read (the round's step PR-7U,
  `sites/unreadable.py`). A site the server cannot read but the browser has not been tried on (`needs_browser`: its
  listings only load in a browser, it asks for a sign-in) is not switched off: `hand_to_browser` sets it to be read in
  the browser, as "Ask resumebot to find out" does, and the browser reader decides. The site's `on` goes off,
  the site keeps `off_by` `app`, `off_note` (why, in one plain clause) and `off_at`, and no task is opened. Its employer
  is handed to the scout again, if the scout had answered for it (`employers.core.ask_scout_again`, docs/PRD.md 28.3:
  that is the one thing that does, besides its job system's board being gone). Its row says
  "Off: can't be read (<why>)" and stays on the page even for an employer's site: that is where the owner finds it and
  its reason. The owner is told (a notification linked to the site, never a decision) only when there is something they
  could do, a site that asks for a sign-in they could add in the app's Chrome (`needs_sign_in`), and only once for a
  site. A robots.txt refusal, a bot check, a 403, a maintenance page or no listings of its own is nothing they can act
  on: no notification. The app looks again at such a site about monthly (`read_probe.recheck_unreadable`) and switches
  it on if it is readable. Every mission skips it, as it does any switched-off site. The owner switching it on again
  clears the marks and the next run tries again. So does a board the scout names for the employer, when the app can
  read its job system (`scout_found_board`, docs/PRD.md 28.3: the owner, 2026-11-05): the
  site is switched on with the board's reader and the trail says so.
- **When the browser could not read a site:** the browser's work is a queue of small items
  (`jobsearch/browser/items.py`); a block rests the site's domain and the item is tried again, and a stall is raised by
  the queue itself.
- **A site the browser sessions could not learn** (`turn_off_unlearnable`, docs/PRD.md 37.5): each failed learn run adds
  one to the site's `learn_tries` and keeps what it found as `learn_note`; at five the app switches the site off as it
  does an unreadable one, with `off_note` "couldn't learn how to read it in your browser (<why>)" and one notification.
  The owner switching it on clears both. A learn plan takes the sites never tried first (`learn_tried_at`, dated when a
  plan names the site), so sites that keep failing do not keep the rest from a turn (docs/PRD.md 37.13).
"""

import asyncio
import logging
import re
import uuid
from collections.abc import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app import db
from app.errors import DomainError
from app.inbox import service as inbox
from app.jobsearch import adapters, ats, off_turn
from app.jobsearch.models import Employer
from app.models.work import Notification
from app.sites import addresses, avoid, skills, watch_record
from app.sites.models import (
    AUTO,
    AVOID,
    BY_APP,
    BY_BROWSER,
    FOUND_READS,
    OFF_BY_APP,
    READ_BYS,
    SEARCH,
    SITE_KINDS,
    SITE_LISTS,
    Project,
    Site,
    SiteSkill,
    effective_read,
    read_is,
)
from app.textutil import cut_at_word, lower_first, one_line, squash, upper_first
from app.timeutil import utc_now
from app.trail import service as trail

log = logging.getLogger(__name__)

OFF, WAITING, LEARNING, NEEDS_YOU, WORKING, STOPPED = "off", "waiting", "learning", "needs_you", "working", "stopped"
BROWSER = "browser"
STATES = (OFF, WAITING, LEARNING, NEEDS_YOU, WORKING, STOPPED, BROWSER)
OWNER_ADDED, APP_ADDED = "owner", "app"
#: `switch`'s `by` when the app switches a site on because it now holds a runbook (`runbook_saved`): neither the owner's
#: word nor a role in play, so no watch is recorded for it (`watch_record.py`).
BY_RUNBOOK = "runbook"
NEEDS_MAX = 160
#: What `needs` says of a site in each state that has something for the owner to do.
PAUSED_WORDS = "Its project is paused. Switch the site off and on to carry on."
ENDED_WORDS = "Its project ended without a way to read this site. Turn it off and on again to try once more tonight."
#: Why a project is paused when its site is set to be read in the owner's browser (and the mark that resumes it).
BROWSER_PAUSE = "This site is read in your browser."
#: Why a project is paused when the owner switches its site off. Switching the site on resumes it (and any other paused
#: project of the site, unless the site is read in the browser).
SWITCHED_OFF_PAUSE = "Its site was switched off."
#: The kind of Inbox item the browser queue raises when its work stalls (`browser_queue`), and what a site's
#: notification links to (the site).
NOTICE_KIND, SITE_LINK = "read_in_browser", "site"
#: The notification the owner gets when the app switches a site off because it cannot be read (its kind; it links to the
#: site, `SITE_LINK`), and the longest a site's name and the reason ("one plain clause") run in it.
OFF_NOTE_KIND = "site_turned_off"
OFF_NAME_MAX, OFF_REASON_MAX = 80, 120
#: A reason the owner could do something about: the site asks for a sign-in they could add in the app's Chrome. Any
#: other reason (robots.txt, a bot check, a 403, a maintenance page, no listings of its own) is nothing they can act on,
#: so the site is listed on Where to look with its reason and the owner is not notified.
_SIGN_IN = re.compile(r"sign[\s-]?in|log[\s-]?in|logon|password|members only|account (is )?required", re.I)
#: How a site's `off_note` begins when the app switched it off because it could not learn how to read it in the owner's
#: browser (docs/PRD.md 37.5): the note is "couldn't learn how to read it in your browser (<why>)".
LEARN_OFF = "couldn't learn how to read it in your browser"
#: The words of the states that have only one (the rest depend on a mission: `_status`).
STATE_WORDS = {
    NEEDS_YOU: "Needs you",
    LEARNING: "Learning to read it",
    STOPPED: "Stopped",
}
STARTS_TONIGHT = "Starts tonight"


class WhereError(DomainError):
    """The owner asked for something that cannot be done; the message is plain words for them."""

    status = 422


class NoSuchSite(WhereError):
    status = 404


class OnTheOtherList(WhereError):
    """The site is already on the owner's other list (a site is never on both)."""

    status = 409


def employer_address(employer: Employer) -> str | None:
    """Where the app looks for an employer's roles: its careers page, else its job board (a public address)."""
    for url in (employer.careers_url, employer.board_url):
        if url and skills.public_url(url.strip()):
            return url.strip()
    return None


def _adapter_recipe(employer: Employer) -> dict | None:
    """The employer's known job system as a skill recipe, when its board is whole."""
    if not employer.ats or not ats.complete(employer.ats, employer.board):
        return None
    board = {k: v for k, v in (employer.board or {}).items() if k != "gone"}  # what the board keeps for itself
    return {"kind": "adapter", "ats": employer.ats, "board": board}


#: The columns of an employer that `employer_address`, `_adapter_recipe` and the seed's decisions read.
_SEED_COLUMNS = (Employer.id, Employer.name, Employer.gated, Employer.careers_url, Employer.board_url, Employer.ats,
                 Employer.board, Employer.check_directly)
_PLANNED = object()  # in the plan: a site the write will make


async def _plan_seed(reader: AsyncSession, owner: str) -> list[uuid.UUID]:
    """The employers (in name order) whose site or skill the seed would make or adopt, decided from what the reader
    sees and nothing written. Exact for the common case, which is none: an employer whose recipe cannot be parsed is
    left out, since saving it fails and writes nothing."""
    stmt = select(Employer).where(Employer.owner == owner, Employer.gated.is_(False)).order_by(Employer.name)
    if db.is_reader(reader):  # a reader's rows are detached and read by their loaded columns alone
        stmt = stmt.options(load_only(*_SEED_COLUMNS))
    employers = await off_turn.scalars_in_slices(reader, stmt)
    sites = await off_turn.rows_in_slices(reader, select(Site.id, Site.url, Site.employer_id).where(
        Site.owner == owner, Site.list_name == SEARCH))
    avoided = await avoid.hosts(reader, owner)
    by_employer: dict[uuid.UUID, object] = {s.employer_id: s.id for s in sites if s.employer_id is not None}
    by_url: dict[str, tuple[object, uuid.UUID | None]] = {s.url: (s.id, s.employer_id) for s in sites}
    skilled = set((await reader.scalars(select(SiteSkill.site_id).where(SiteSkill.owner == owner))).all())
    todo: list[uuid.UUID] = []
    for n, employer in enumerate(employers):
        if n and n % off_turn.SLICE == 0:
            await asyncio.sleep(0)
        acts = False
        site_id = by_employer.get(employer.id)
        if site_id is None:
            url = employer_address(employer)
            if url is None or avoid.on_list([url], avoided):
                continue
            taken = by_url.get(url)
            if taken is not None:
                if taken[1] is not None:
                    continue
                site_id = taken[0]
                by_url[url] = (site_id, employer.id)
            else:
                site_id = _PLANNED
                by_url[url] = (site_id, employer.id)
            by_employer[employer.id] = site_id
            acts = True
        recipe = _adapter_recipe(employer)
        if recipe is not None and (site_id is _PLANNED or site_id not in skilled):
            try:
                skills.parse_recipe(recipe)
            except ValueError:
                recipe = None
            if recipe is not None:
                acts = True
        if acts:
            todo.append(employer.id)
    return todo


async def seed_employer_sites(session: AsyncSession, owner: str) -> dict:
    """A site for each employer with an address, and the known-system ones their adapter skill. Returns how many sites
    and skills were made. Idempotent. An employer that is gated gets no site (its roles are never read). It ends by
    settling "Watch the job board of every employer with a role in play" (`settle_watch`).

    The plan is made in a reader (`off_turn.reading`) and the writer's turn is taken only when it is not empty: then the
    planned employers are loaded again and decided again from what is true now, so an employer that changed or was
    gated meanwhile gets what it is owed now."""
    async with off_turn.reading(session) as reader:
        planned = await _plan_seed(reader, owner)
    made = learned = 0
    if planned:
        made, learned = await _write_seed(session, owner, planned)
    await settle_watch(session, owner)
    return {"sites": made, "skills": learned}


async def _write_seed(session: AsyncSession, owner: str, planned: list[uuid.UUID]) -> tuple[int, int]:
    """Make what the plan named, from the planned employers as they are now (in name order)."""
    employers = list((await session.scalars(
        select(Employer).where(Employer.owner == owner, Employer.gated.is_(False), Employer.id.in_(planned))
        .order_by(Employer.name)
    )).all())
    addresses = {a for a in (employer_address(e) for e in employers) if a}
    sites = list((await session.scalars(select(Site).where(
        Site.owner == owner, Site.list_name == SEARCH,
        or_(Site.employer_id.in_([e.id for e in employers]), Site.url.in_(addresses)),
    ))).all())
    avoided = await avoid.hosts(session, owner)
    by_employer = {s.employer_id: s for s in sites if s.employer_id is not None}
    by_url = {s.url: s for s in sites}
    skilled = set((await session.scalars(
        select(SiteSkill.site_id).where(SiteSkill.owner == owner, SiteSkill.site_id.in_([s.id for s in sites]))
    )).all())
    made = learned = 0
    for employer in employers:
        site = by_employer.get(employer.id)
        if site is None:
            url = employer_address(employer)
            if url is None or avoid.on_list([url], avoided):  # no address, or on a site the owner avoids: no site
                continue
            taken = by_url.get(url)
            if taken is not None:
                # The address is already a site: the owner's own, which now belongs to this employer too, or another
                # employer's (then there is nothing new to watch).
                if taken.employer_id is not None:
                    continue
                taken.employer_id = employer.id
                site = taken
            else:
                on = bool(employer.check_directly)
                site = Site(
                    owner=owner, url=url, kind="employer", on=on, employer_id=employer.id, label=employer.name,
                    added_by=APP_ADDED, created_at=utc_now(), turned_on_at=utc_now() if on else None,
                )
                session.add(site)
                await session.flush()
                by_url[url] = site
                made += 1
            by_employer[employer.id] = site
        recipe = _adapter_recipe(employer)
        if recipe is not None and site.id not in skilled:
            try:
                await skills.save_skill(session, owner, site.id, recipe, None)
            except ValueError:  # a system the app cannot read yet: this site is learned by a project
                continue
            skilled.add(site.id)
            learned += 1
    await session.flush()
    return made, learned


async def is_plain(session: AsyncSession, site: Site) -> bool:
    """Whether a site holds nothing but what the app makes again: an employer's own site the app made, whose skills are
    all a job system's reader and that no project was ever made for."""
    if site.kind != "employer" or site.added_by != APP_ADDED or site.list_name != SEARCH:
        return False
    recipes = (await session.scalars(select(SiteSkill.recipe).where(SiteSkill.site_id == site.id))).all()
    if any((recipe or {}).get("kind") != "adapter" for recipe in recipes):
        return False
    projects = await session.scalar(select(func.count()).select_from(Project).where(Project.site_id == site.id)) or 0
    return not projects


async def reaim_sites(session: AsyncSession, owner: str, moved: Iterable[tuple[Employer, set[str]]]) -> None:
    """Employers that let go of a board the open feeds guessed (`resolve.from_links`), each with the addresses of the
    guess (its board's page, and the careers page the app took from it): the site the app made for each, when it holds
    nothing of its own (`is_plain`), is made what `seed_employer_sites` would make for the employer now, which it never
    does for a site that already has a skill. A site on the guess's page is moved to the employer's address, unless that
    is another site's (an address is held once) or there is none. The guess's reader is retired and the reader of the
    new board saved; a board that is not whole yet has none, and the site waits to be learned if it is on. The switch
    is left as it was, except that saving the new reader switches on a site nobody has decided about
    (`runbook_saved`). Services flush; the caller commits."""
    moved = list(moved)
    if not moved:
        return
    for employer, guess in moved:
        for site in (await session.scalars(select(Site).where(
                Site.owner == owner, Site.employer_id == employer.id, Site.list_name == SEARCH, Site.kind == "employer"
        ).order_by(Site.created_at))).all():
            if not await is_plain(session, site):
                continue
            address = employer_address(employer)
            if site.url in guess and address != site.url:
                if address is None or await session.scalar(
                        select(Site.id).where(Site.owner == owner, Site.url == address)) is not None:
                    continue
                site.url = address
            recipe = _adapter_recipe(employer)
            kept = None
            if recipe is not None:
                try:
                    kept = await skills.save_skill(session, owner, site.id, recipe, None)
                except ValueError:  # a system the app cannot read yet: this site is learned by a project
                    pass
            for old in await skills.skills_of_site(session, site.id):
                if old.retired_at is None and (kept is None or old.id != kept.id):
                    await skills.retire_skill(session, old)
    await session.flush()


async def settle_watch(session: AsyncSession, owner: str) -> None:
    """Watch what the owner asked to watch for a role in play, and unwatch what the app no longer should
    (`auto_watch.settle`). Run once every employer with an address has its site (so an employer that gains a board is
    seen here, whoever found the board) and where a role comes into play or leaves it (the scout's finds stored, the
    owner moving a role). A nicety to whatever calls it, never a reason to lose that work: if it fails, what it did is
    rolled back, the failure is logged, and the caller goes on."""
    from app.sites import auto_watch  # it switches sites, so it imports this module

    try:
        if off_turn.is_free(session):  # nothing to switch is the usual case: read it apart, take no turn for it
            async with off_turn.reading(session) as reader:
                todo = await auto_watch.plan(reader, owner)
            if not todo:
                return
        async with session.begin_nested():
            await auto_watch.settle(session, owner)
    except Exception:  # the sites are kept whatever the rule does
        log.exception("watching the employers with a role in play failed")


async def watched_employers(session: AsyncSession, owner: str) -> list[Employer]:
    """The employers whose site the owner has switched on: the only ones the night's read looks at. A site the owner
    reads in their own browser is not the server's to fetch (docs/PRD.md 30.3), so it is left out."""
    return list((await session.scalars(
        select(Employer).join(Site, Site.employer_id == Employer.id)
        .where(Employer.owner == owner, Site.owner == owner, Site.on.is_(True), Site.list_name == SEARCH,
               read_is(BY_APP)).order_by(Employer.name)
    )).all())


# --- a skill that breaks (docs/PRD.md 29.4) ---------------------------------------------------------------------------


async def retire_broken(session: AsyncSession, owner: str, faults: list[dict]) -> list[str]:
    """The night's read said these sites' skills no longer fit (a fault of kind `refused`, `shape` or `empty`,
    `postings.BREAKS`): retire each one's version in force. The site is then marked by that alone: it has no skill and
    its last project, if any, ended before the version was retired, so the next night's Scout mission starts a new
    project for it (`autostart.ensure_projects`, at most the owner's sites to learn a night; never now). A fault of any
    other kind (the site is unreachable tonight, or its board was gone and its job system is looked for again,
    `resolve.forget`) leaves the skill alone. A fault names its site (`site_id`: a read by the site's own skill,
    `jobsearch/postings/site_reads.py`, which may be a job board's, with no employer) or its employer (`employer_id`).
    Returns
    the names of the employers (or sites) that lost their skill."""
    from app.jobsearch.postings.index import BREAKS  # imported here so the two never import in a loop

    broke = [f for f in faults if f.get("kind") in BREAKS]
    employer_ids = [uuid.UUID(f["employer_id"]) for f in broke if f.get("employer_id")]
    site_ids = [uuid.UUID(f["site_id"]) for f in broke if f.get("site_id")]
    retired: list[str] = []
    if not employer_ids and not site_ids:
        return retired
    for site, employer in (await session.execute(
        select(Site, Employer).outerjoin(Employer, Site.employer_id == Employer.id)
        .where(Site.owner == owner, Site.on.is_(True), or_(Employer.id.in_(employer_ids), Site.id.in_(site_ids)))
        .order_by(Employer.name, Site.label, Site.url)
    )).all():
        skill = await skills.current_skill(session, site.id)
        if skill is not None:
            await skills.retire_skill(session, skill)
            retired.append(employer.name if employer is not None else site.label or _host(site.url))
    return retired


async def unreadable_employer_ids(session: AsyncSession, owner: str) -> set[uuid.UUID]:
    """The employers whose site has no skill that works because one broke: the night's read leaves them alone, so a
    board that stopped fitting does not fail every night while its project learns the new shape."""
    employer_of = {site_id: employer_id for site_id, employer_id in (await session.execute(
        select(Site.id, Site.employer_id).where(Site.owner == owner, Site.on.is_(True), Site.employer_id.is_not(None))
    )).all() if employer_id is not None}
    if not employer_of:
        return set()
    in_force = set((await session.scalars(
        select(SiteSkill.site_id).where(db.among(SiteSkill.site_id, employer_of), SiteSkill.retired_at.is_(None))
        .distinct())).all())
    broken = await skills.broken_sites(session, {site_id: None for site_id in employer_of if site_id not in in_force})
    return {employer_of[site_id] for site_id in broken}


# --- the owner's controls ---------------------------------------------------------------------------------------------


async def _site(session: AsyncSession, owner: str, site_id: uuid.UUID) -> Site:
    site = await session.get(Site, site_id)
    if site is None or site.owner != owner:
        raise NoSuchSite("no such site")
    return site


async def _ended_without_skill(session: AsyncSession, site: Site) -> bool:
    """Whether the site's last project ended and left it with no skill that works (the state `stopped`, apart from a
    paused project) since it was last switched on."""
    if await skills.current_skill(session, site.id) is not None:
        return False
    open_project = await session.scalar(
        select(Project.id).where(Project.site_id == site.id, Project.finished_at.is_(None)).limit(1))
    if open_project is not None:
        return False
    last_end = await session.scalar(select(func.max(Project.finished_at)).where(Project.site_id == site.id))
    return last_end is not None and (site.turned_on_at is None or site.turned_on_at <= last_end)


def _address(url: str) -> str:
    """The address as the owner typed it, trimmed, with `https://` put in front when it has no scheme."""
    address = (url or "").strip()
    return f"https://{address}" if address and "://" not in address else address


async def _clash(session: AsyncSession, owner: str, host: str, list_name: str) -> Site | None:
    """The site on the owner's other list that `host` is, is under or has under it (docs/PRD.md 30.3: a site is never
    on both). On the list to search only a site that is switched on counts: one that is off is not searched, and an
    owner-added site cannot be removed from the list, only switched off."""
    other = AVOID if list_name == SEARCH else SEARCH
    query = select(Site).where(Site.owner == owner, Site.list_name == other)
    if other == SEARCH:
        query = query.where(Site.on.is_(True))
    for site in (await session.scalars(query.order_by(Site.created_at))).all():
        theirs = avoid.host_of(site.url)
        if theirs and avoid.related(theirs, host):
            return site
    return None


def _clash_words(site: Site) -> str:
    host = avoid.host_of(site.url) or site.url
    if site.list_name == AVOID:
        return f"{host} is on your list of sites to avoid. Take it off that list first."
    return f"{host} is on your list of sites to search. Switch it off there first."


async def add(
    session: AsyncSession, owner: str, url: str, kind: str = "job_board", *, list_name: str = SEARCH,
    read_by: str = BY_APP,
) -> Site:
    """Add a site to one of the owner's lists (docs/PRD.md 30.3). To search: a job board, an association's board or an
    employer's careers page; adding is switching on, and a site already there is switched on. To avoid: kept as its
    host, never read; the host already there is returned. Public http(s) addresses only (an address with no scheme is
    taken as https). `read_by`: tested by resumebot (`auto`; the route then tests the site once,
    `read_probe.settle`), the app's night, or the owner's own browser. Refused (`OnTheOtherList`) when the site is
    already on the other list."""
    if list_name not in SITE_LISTS:
        raise WhereError(f"a list is one of: {', '.join(SITE_LISTS)}")
    if read_by not in READ_BYS:
        raise WhereError(f"a site is read by one of: {', '.join(READ_BYS)}")
    if kind not in SITE_KINDS:
        raise WhereError(f"a site is one of: {', '.join(SITE_KINDS)}")
    address = _address(url)
    if addresses.address_secret(address):
        raise WhereError("that address has a password or key in it; use the plain address of the page")
    host = avoid.host_of(address) if skills.public_url(address) else None
    if host is None:
        raise WhereError(
            "a site is the public http(s) address of a careers section or job board, such as "
            "https://careers.example.com/jobs; no sign-in pages and no private addresses")
    clash = await _clash(session, owner, host, list_name)
    if clash is not None:
        raise OnTheOtherList(_clash_words(clash))
    if list_name == AVOID:
        for site in (await session.scalars(select(Site).where(Site.owner == owner, Site.list_name == AVOID))).all():
            if avoid.host_of(site.url) == host:
                return site
        site = Site(owner=owner, url=host, kind="job_board", on=False, list_name=AVOID, read_by=BY_APP,
                    added_by=OWNER_ADDED, created_at=utc_now())
        session.add(site)
        await session.flush()
        return site
    site = await session.scalar(select(Site).where(Site.owner == owner, Site.url == address))
    if site is not None:
        return await switch(session, owner, site.id, True)
    site = Site(
        owner=owner, url=address, kind=kind, on=True, added_by=OWNER_ADDED, created_at=utc_now(),
        turned_on_at=utc_now(), read_by=read_by,
    )
    session.add(site)
    await session.flush()
    return site


async def switch(
    session: AsyncSession, owner: str, site_id: uuid.UUID, on: bool, *, by: str = watch_record.BY_OWNER,
) -> Site:
    """The owner's switch. Turning on records when (the first time, or again when its project has ended with no skill,
    so the night's mission tries once more); it starts nothing now, but a project of the site that is paused carries on
    (`_follow_switch`). Turning off pauses the site's open project, so nothing more is handed out or fetched for it. A
    site to avoid has no switch, and one whose host is on the list to avoid cannot be switched on (`OnTheOtherList`).
    A company's site is its watch, and `by` says who set it, which is kept (`watch_record.py`): the owner, or the app
    for a role in play (`auto_watch.py`); the app's default for a site that holds a runbook (`BY_RUNBOOK`) is nobody's
    word on the watch, so none is kept. The app's switching leaves the owner's marks on the site alone and a project
    the owner paused by hand paused, and is not the owner's "try again" for a project that ended with no skill: it
    stamps the first time only, so a role going out of play and coming back never starts another project."""
    from app.jobsearch.employers import not_checked  # imported here so the two never import in a loop

    site = await _site(session, owner, site_id)
    if site.list_name == AVOID:
        raise WhereError("a site you avoid is never read, so it has no switch; take it off the list to read it")
    if on:
        clash = await _clash(session, owner, avoid.host_of(site.url) or "", SEARCH)
        if clash is not None:
            raise OnTheOtherList(_clash_words(clash))
    changed = site.on != on
    if on and not site.on:
        if site.turned_on_at is None or (by == watch_record.BY_OWNER and await _ended_without_skill(session, site)):
            site.turned_on_at = utc_now()
    elif on and site.turned_on_at is None:
        site.turned_on_at = utc_now()
    site.on = on
    if on and by == watch_record.BY_OWNER:  # the owner's word: the app's reason for switching it off no longer stands
        site.off_by = site.off_note = site.learn_note = None  # (the next run tries again)
        site.learn_tries = 0
    if site.employer_id is not None:  # a company's own site is its watch switch (set on the employer's page)
        employer = await session.get(Employer, site.employer_id)
        if employer is not None:
            employer.check_directly = on
            if by != BY_RUNBOOK:  # nobody's word: the app must not read it later as the owner's or a role's
                watch_record.note(employer, by)
    if changed:
        await _follow_switch(session, site, on, by)
        await not_checked.settle(session, owner)  # a site switched off is not read: its postings are Not checked
    await session.flush()
    return site


async def runbook_saved(session: AsyncSession, site_id: uuid.UUID | None) -> bool:
    """A site has just been given a runbook (the one rule every place that saves one calls: `skills.save_skill`, which
    holds a job system's reader, a skill a project learned and the one the browser learned, and the three places a
    recipe is kept for a careers page, `browser/recipes`): the app can read it now and reading costs almost nothing, so
    a site that is off is switched on (`switch`, by `BY_RUNBOOK`: the watch of its employer follows, no word of the
    owner's or of a role in play is recorded; the owner, 2026-11-05).

    Only a site nobody has decided about. It stays off when it is on the list to avoid, its host is on that list, its
    employer is blocked (gated), the app switched it off for a reason it recorded (`off_by`: the mark stays until the
    owner, the monthly look at the site, `read_probe.recheck_unreadable`, or a board the scout names for its employer,
    `scout_found_board`, clears it, so the reason still stands), or
    someone switched it on and off since the site was made: the owner (their word on the watch is kept,
    `watch_record.py`; or, for a switch made before the record began, a site that has been on, `turned_on_at`, and is
    off with no reason of the app's) or the app for a role that left play, which must not come back on with every
    runbook. A site with no runbook is never touched here. Returns whether the site was switched on. Services flush;
    the caller commits."""
    site = await session.get(Site, site_id) if site_id is not None else None
    if site is None or site.on or site.list_name != SEARCH or site.off_by is not None or site.turned_on_at is not None:
        return False
    employer = await session.get(Employer, site.employer_id) if site.employer_id is not None else None
    if employer is not None and (employer.gated or watch_record.set_by(employer) is not None):
        return False
    try:
        await switch(session, site.owner, site.id, True, by=BY_RUNBOOK)
    except OnTheOtherList:  # its host is on the owner's list of sites to avoid: it stays off, and the runbook is kept
        return False
    return True


async def scout_found_board(session: AsyncSession, employer: Employer) -> bool:
    """The scout named the employer's job system (`jobs.board_found`: `resolve.take` has just kept the board on the
    employer) and the app has a reader for it: the site the app switched off because it could not read the employer's
    careers page is switched on with that reader, and the app's reason (`off_by`, `off_note`, `off_at`, and the learn
    count and note that go with a site it could not learn) is cleared. The off mark is cleared (the owner, 2026-11-05;
    docs/PRD.md 28.3). Nothing else lifts it for the scout: not a board the employer's links name, not the monthly look
    (`read_probe.recheck_unreadable` reads the careers page that failed, not this board).

    The reader is saved and the site switched on here, not left to `runbook_saved`: that rule refuses a site that was on
    once (`turned_on_at`, set on every site the app could switch off) or whose employer's watch was set by someone
    (`watch_record`), and the app's own switch-off is the last word on such a site, so lifting its reason is what
    `runbook_saved` cannot know. The switch is the runbook's (`BY_RUNBOOK`): it records no one's word on the watch, so
    whoever set the watch before stays on record. The reader, when the site holds none for this board, becomes its
    newest skill, so the built-in read is the site's reader (`site_reads.plan`).

    Only the employer's own site that holds the app's reason. Never a site the owner switched off (it holds no reason
    of the app's), a blocked (gated) employer's, or a site on a host the owner avoids; and not for a board that is not
    whole yet or a job system the app has no reader for: the reason stands, since nothing can read the site. Nor a
    site the owner's browser reads (`effective_read`): the night reads only what the app reads (`watched_employers`), so
    the board would not be read, and the browser would map the careers page that failed until the site was given up on
    again and the scout asked again for the same board. Whether such a site should be read by the app instead is the
    owner's to say (docs/PRD.md 28.3). Records one line on the trail for each site switched on and returns whether
    there was one. Services flush; the caller commits."""
    recipe = None if employer.gated else _adapter_recipe(employer)
    if recipe is None:
        return False
    sites = (await session.scalars(select(Site).where(
        Site.owner == employer.owner, Site.employer_id == employer.id, Site.kind == "employer",
        Site.list_name == SEARCH, Site.on.is_(False), Site.off_by == OFF_BY_APP).order_by(Site.created_at))).all()
    switched = False
    for site in sites:
        if effective_read(site) == BY_BROWSER:  # the night never reads it: nothing here would make the board read
            continue
        if await _clash(session, site.owner, avoid.host_of(site.url) or "", SEARCH) is not None:
            continue
        current = await skills.current_skill(session, site.id)
        held = (current.recipe if current is not None else None) or {}
        if (held.get("kind"), held.get("ats"), held.get("board")) != ("adapter", recipe["ats"], recipe["board"]):
            try:
                await skills.save_skill(session, site.owner, site.id, recipe, None)
            except ValueError:  # a system the app cannot read yet: its reason stands
                continue
        site.off_by = site.off_note = site.off_at = site.learn_note = None
        site.learn_tries = 0
        await switch(session, site.owner, site.id, True, by=BY_RUNBOOK)
        await trail.record(
            session, site.owner, event="site_switched_on",
            sentence=(f"Switched {one_line(await _name(session, site), OFF_NAME_MAX)} back on: the scout found its job "
                      f"system, {ats.label(recipe['ats'])}."),
            link=(SITE_LINK, site.id))
        switched = True
    return switched


#: What a project's Activity says when the app, not the owner, switches its site off or on for a role in play.
PLAY_OFF_WORDS = (
    "Its employer is no longer watched for a role in play, so its site was switched off and this project is paused.")
PLAY_ON_WORDS = "Its employer has a role in play, so its site is watched again and this project carries on."


async def _follow_switch(session: AsyncSession, site: Site, on: bool, by: str = watch_record.BY_OWNER) -> None:
    """The switch reaches the site's open project: switching off pauses it (`SWITCHED_OFF_PAUSE`; its work is kept),
    switching on resumes one that is paused, whoever paused it. A site read in the browser keeps its project paused
    (`_sync_projects`). The app's own switch (`by`, for a role in play) says so in the project's Activity, and resumes
    only a project that the switch-off paused: one the owner paused by hand stays paused."""
    from app.sites import completion  # the project loop, which imports much of this package

    owner_did = by == watch_record.BY_OWNER
    for project in (await session.scalars(
        select(Project).where(Project.site_id == site.id, Project.finished_at.is_(None))
    )).all():
        if not on and project.stopped_at is None:
            await completion._stop(
                session, project, SWITCHED_OFF_PAUSE,
                ("stopped", "You switched its site off, so this project is paused." if owner_did else PLAY_OFF_WORDS),
                who="you" if owner_did else "app")
        elif on and project.stopped_at is not None and effective_read(site) != BY_BROWSER and (
            owner_did or project.stop_reason == SWITCHED_OFF_PAUSE
        ):
            await completion.resume_project(
                session, project, who="you" if owner_did else "app", said=None if owner_did else PLAY_ON_WORDS)


async def _sync_projects(session: AsyncSession, site: Site) -> None:
    """Make a site's open projects agree with who reads it (`effective_read`): a site read in the browser has its
    project paused (with the mark `BROWSER_PAUSE`), and one read by the app has that pause lifted and loses its browser
    recipe (retired, kept as history: the app cannot run it). A project the owner paused themselves stays paused."""
    read = effective_read(site)
    if read is None:
        return
    if read == BY_APP:  # a recipe for the owner's browser (docs/PRD.md 37) is no skill for the app: kept, retired
        skill = await skills.current_skill(session, site.id)
        if skill is not None and (skill.recipe or {}).get("kind") == "browser":
            await skills.retire_skill(session, skill)
    from app.sites import completion  # the project loop, which imports much of this package

    for project in (await session.scalars(
        select(Project).where(Project.site_id == site.id, Project.finished_at.is_(None))
    )).all():
        if read == BY_BROWSER and project.stopped_at is None:
            await completion.stop_project(session, project, BROWSER_PAUSE)
        elif read == BY_APP and project.stopped_at is not None and project.stop_reason == BROWSER_PAUSE:
            await completion.resume_project(session, project)


async def set_read_by(session: AsyncSession, owner: str, site_id: uuid.UUID, read_by: str) -> Site:
    """How a site is read: tested by resumebot (`auto`), the app's night, or the owner's own browser (docs/PRD.md 30.3,
    for a site a server cannot read: a sign-in, robots rules, a bot block). A site read in the browser is not fetched by
    the night's read and gets no project; one already being learned is paused, and resumed when the site is set back to
    the app. A change starts over: what an `auto` site was found to need is forgotten (the route tests the site again,
    `read_probe.settle`)."""
    if read_by not in READ_BYS:
        raise WhereError(f"a site is read by one of: {', '.join(READ_BYS)}")
    site = await _site(session, owner, site_id)
    if site.list_name == AVOID:
        raise WhereError("a site you avoid is never read, so it is not read in your browser either")
    if site.read_by == read_by:
        return site
    site.read_by, site.read_found, site.read_note = read_by, None, None
    await _sync_projects(session, site)
    await session.flush()
    return site


async def set_found(session: AsyncSession, site: Site, found: str, note: str | None) -> Site:
    """Write what the server's test (`read_probe`) found about an `auto` site: `app` or `browser`, and the sentence
    why. A site that is not `auto` is left alone. The site's open projects follow (`_sync_projects`): one found to need
    the browser is paused. Services flush; the caller commits."""
    if found not in FOUND_READS:
        raise WhereError(f"a site is found to be read by one of: {', '.join(FOUND_READS)}")
    if site.read_by != AUTO:
        return site
    site.read_found, site.read_note = found, (note or "").strip() or None
    await _sync_projects(session, site)
    await session.flush()
    return site


# --- a site the browser reader could not read -------------------------------------------------------------------------


async def _name(session: AsyncSession, site: Site) -> str:
    employer = await session.get(Employer, site.employer_id) if site.employer_id else None
    return (employer.name if employer else None) or (site.label or "").strip() or _host(site.url)


def clause_of(reason: str | None) -> str:
    """Why a site cannot be read, as the one plain clause the notice and the row say ("its listings only load in a
    browser"): on one line, no closing full stop, a lowercase start (an acronym keeps its capitals), held to
    `OFF_REASON_MAX` characters at a whole word. Never empty."""
    text = cut_at_word(lower_first(squash(reason).rstrip(". "), keep_acronym=True), OFF_REASON_MAX).rstrip(". ")
    return text or "no way to read it was found"


def needs_sign_in(reason: str | None) -> bool:
    """Whether the reason a site can't be read is one the owner could do something about: it asks for a sign-in."""
    return bool(_SIGN_IN.search(reason or ""))


def sign_in_words(name: str) -> str:
    """The notification the owner gets when the app switches a site off because it needs a sign-in."""
    return (f"{one_line(name, OFF_NAME_MAX)} needs a sign-in to be read, so resumebot turned it off. Sign in to it in "
            "the app's Chrome, then turn it back on under Where to look.")


async def _switch_off(session: AsyncSession, site: Site, off_note: str) -> Notification | None:
    """The app switches a site's `on` off itself (the one way it is done): records why and that it was the app
    (`off_by`, `off_note`), switches the company's watch off with it, hands its employer to the scout again
    (`ask_scout_again`: the app gave up on its page, so the scout may find another way to its jobs), and tells the owner
    once, in a notification that links to the site, titled `words(name)`. None when there was nothing to switch off (a
    site that is off already, or one to avoid)."""
    from app.jobsearch.employers import not_checked  # imported here so the two never import in a loop
    from app.jobsearch.employers.core import ask_scout_again  # (as above)

    if site.list_name != SEARCH or not site.on:
        return None
    site.on = False
    site.off_by, site.off_note, site.off_at = OFF_BY_APP, off_note, utc_now()
    if site.employer_id is not None:
        employer = await session.get(Employer, site.employer_id)
        if employer is not None:
            employer.check_directly = False
            ask_scout_again(employer)  # no means can read its page: the scout looks for another way (docs/PRD.md 28.3)
    note = None
    if needs_sign_in(off_note) and await session.scalar(
        select(Notification.id).where(Notification.owner == site.owner, Notification.kind == OFF_NOTE_KIND,
                                      Notification.link_kind == SITE_LINK, Notification.link_id == site.id).limit(1)
    ) is None:  # told once for a site, and only of what the owner could do
        note = await inbox.notify(
            session, site.owner, key=f"site-off-{site.id}", kind=OFF_NOTE_KIND,
            title=sign_in_words(await _name(session, site)), level="attention", link=(SITE_LINK, site.id))
    await session.flush()
    await not_checked.settle(session, site.owner)  # nothing reads the site now: its postings are Not checked
    return note


async def turn_off_unreadable(session: AsyncSession, site: Site, reason: str | None) -> Notification | None:
    """The site cannot be read by any means the app has (the browser reader reported it blocked after the server could
    not read it either, or the scout, or a plan, said so: `sites/unreadable.py`). The app switches its `on` off itself,
    records why and that it was the app (`off_by`, `off_note`), and tells the owner once, in a notification that links
    to the site: "Turned off <site>: it can't be read (<why>). Turn it back on under Where to look when that changes."
    No decision is asked and no task is opened. A company's watch switch goes off with it, so every mission skips it as
    it does any switched-off site. Returns the notification, or None when there was nothing to switch off (a site that
    is off already, or one to avoid): one notification for each switch-off. Services flush; the route commits."""
    note = clause_of(reason)
    return await _switch_off(session, site, note)


async def turn_off_unlearnable(session: AsyncSession, site: Site, reason: str | None) -> Notification | None:
    """Learning how to read the site in the owner's browser failed as many times as the app tries
    (`browser_reader.LEARN_TRIES`, docs/PRD.md 37.5). The app switches the site off as `turn_off_unreadable` does, with
    `off_note` "couldn't learn how to read it in your browser (<why>)" and one notification, no task. The owner
    switching it on again clears the count and the next learn run tries again."""
    why = clause_of(reason)
    return await _switch_off(session, site, f"{LEARN_OFF} ({why})")


async def adapter_system(session: AsyncSession, site: Site) -> str | None:
    """The job system of a site that the app already reads in code (`adapters.ADAPTERS`), or None: the site's own
    address is on one (`ats.parse` names its board), or its employer posts on one. Such a site is read by the app,
    never by the owner's browser: the browser would only hand in again what the reader already holds."""
    found = ats.parse(site.url)
    key = found.board.system if found and found.board else None
    if key is None and site.employer_id is not None:
        employer = await session.get(Employer, site.employer_id)
        key = employer.ats if employer is not None else None
    return key if key in adapters.ADAPTERS else None


async def hand_to_browser(session: AsyncSession, site: Site, reason: str | None) -> bool:
    """The server cannot read the site but the browser has not been tried (the scout or a plan said its listings only
    load in a browser, or that it asks for a sign-in: `sites/unreadable.py`): the site is set to be read in the owner's
    browser, as "Ask resumebot to find out" does for a site the server's test could not read, and the browser reader
    decides from there. A site left to resumebot (`auto`) is found to need the browser; one the app was told to read is
    set to the browser; either way the reason is kept as the site's note (`read_note`), which its open row says as
    "Found out: ...". Nothing is switched off and the owner is told nothing: if the browser fails too, the site is
    switched off then (`turn_off_unreadable`). Returns False for a site that was read in the browser already, or is to
    be avoided. Services flush; the route commits."""
    if site.list_name != SEARCH or effective_read(site) == BY_BROWSER:
        return False
    if await adapter_system(session, site) is not None:  # code reads its job system: the browser never needs to
        return False
    note = f"{upper_first(clause_of(reason))}."
    if site.read_by == AUTO:
        await set_found(session, site, BY_BROWSER, note)
    else:
        await set_read_by(session, site.owner, site.id, BY_BROWSER)
        site.read_note = note  # `set_read_by` starts over: the owner's own choice has no note, this one has its reason
        await session.flush()
    return True


async def remove(session: AsyncSession, owner: str, site_id: uuid.UUID) -> None:
    """Take a site off the list to avoid. A site to search is only switched off: its history stays."""
    site = await _site(session, owner, site_id)
    if site.list_name != AVOID:
        raise WhereError("a site you search is switched off, not removed; its history stays")
    await session.delete(site)
    await session.flush()


async def browser_sites(session: AsyncSession, owner: str) -> list[Site]:
    """The sites to search that are switched on and read in the owner's browser (the owner chose it, or resumebot found
    it needs it: `effective_read`): what the browser reader works through. Never a site to avoid, and never one the app
    reads itself."""
    return list((await session.scalars(
        select(Site).where(Site.owner == owner, Site.list_name == SEARCH, Site.on.is_(True), read_is(BY_BROWSER))
        .order_by(Site.created_at, Site.url)
    )).all())


def _host(url: str) -> str:
    return avoid.host_of(url) or url
