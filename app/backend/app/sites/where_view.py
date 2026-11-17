"""What the owner sees of the sites they search (docs/PRD.md 29.1, 30.3): each site's row for Where to look (its state,
what it needs from the owner and the words that say how it is being read), the roles each site or job system brought,
and the page of both. Split from `where.py`, which holds the owner's controls and the app's own switching; this reads
that module's words and changes nothing."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from functools import lru_cache
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import adapters, ats, limits
from app.jobsearch.fit import scoring
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import record
from app.jobsearch.missions.core import Standing
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.stages import GATED_OUT
from app.models.work import TASK_OPEN, Task
from app.sites import autostart, avoid, resolution, skills
from app.sites.models import AVOID, BY_BROWSER, OFF_BY_APP, SEARCH, Project, Site, SiteSkill, effective_read
from app.sites.records import PROJECT_LINK
from app.sites.where import (
    BROWSER,
    ENDED_WORDS,
    LEARN_OFF,
    LEARNING,
    NEEDS_MAX,
    NEEDS_YOU,
    OFF,
    PAUSED_WORDS,
    STARTS_TONIGHT,
    STATE_WORDS,
    STOPPED,
    WAITING,
    WORKING,
    _host,
    _site,
)
from app.textutil import one_line
from app.timeutil import aware, iso_utc, owner_zone

# --- what the owner sees ----------------------------------------------------------------------------------------------


def _state(
    site: Site, skill: SiteSkill | None, projects: list[Project], tasks: dict[uuid.UUID, list[Task]],
    broke: bool = False,
) -> tuple[str, str | None]:
    """The state and what the owner can do about it. `projects` are the site's, newest first. `broke`: its skill broke
    after its last project ended (docs/PRD.md 29.4), so it is waiting for tonight's project, not stopped."""
    if not site.on:
        return OFF, None
    open_project = next((p for p in projects if p.finished_at is None), None)
    if open_project is not None and tasks.get(open_project.id):
        return NEEDS_YOU, one_line(tasks[open_project.id][0].title, NEEDS_MAX)
    if skill is not None:
        return WORKING, None
    if open_project is not None:
        return (STOPPED, PAUSED_WORDS) if open_project.stopped_at is not None else (LEARNING, None)
    last = projects[0] if projects else None
    if last is None or broke:
        return WAITING, None
    if site.turned_on_at is not None and last.finished_at is not None and site.turned_on_at > last.finished_at:
        return WAITING, None
    return STOPPED, ENDED_WORDS


def _clock(at: object) -> tuple[str, bool] | None:
    """A daily time such as "00:30" as "12:30 AM", and whether it is in the night (8 PM to 6 AM); None if it is not a
    time."""
    try:
        hour, minute = (int(part) for part in str(at).split(":"))
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}", hour >= 20 or hour < 6


def _short_date(when: datetime) -> str:
    """A day in the install's time zone as "Oct 7", with the year when it is not this one."""
    zone = owner_zone()
    local = aware(when).astimezone(zone)
    text = f"{local:%b} {local.day}"
    return text if local.year == datetime.now(zone).year else f"{text}, {local.year}"


def read_words(read: Standing) -> str:
    """What is true of a site, or a job system, the night's read of the boards reads: it is read at that mission's
    time, unless the mission is paused. `read` is the mission's standing (`missions.standing`); one this install has not
    made yet is read nightly."""
    if not read.made:
        return "Read nightly"
    if not read.on:
        return read.waits
    clock = _clock(read.schedule.get("at")) if read.schedule.get("kind") == "daily" else None
    if clock is not None:
        return f"Read {'nightly' if clock[1] else 'every day'} at {clock[0]}"
    return f"Read {read.when}"


def start_words(scout: Standing, turn: int | None = None, per_night: int | None = None) -> str:
    """What is true of a site waiting for its project: the night's Scout mission starts at most `per_night` projects a
    night (the owner's Sites to learn a night, `Settings.site_projects_per_night`; `missions.SITE_PROJECTS_A_NIGHT`
    when not given; -1: no limit, so every waiting site starts tonight), so `turn` (0 for the first in its order,
    `autostart.start_queue`) says whether it is one of tonight's, "Starts tonight at 1:00 AM" (the mission's time, as
    `read_words` has the read's), or waits, "Waits its turn: starts in about 2 nights". None is not in the queue
    (nothing says when): tonight's words."""
    per_night = missions.SITE_PROJECTS_A_NIGHT if per_night is None else per_night
    if scout.made and not scout.on:
        return scout.waits
    if turn is not None and not limits.unlimited(per_night) and turn >= per_night:
        nights = -(-(turn - per_night + 1) // per_night)
        return f"Waits its turn: starts in about {nights} night{'s' if nights != 1 else ''}"
    clock = _clock(scout.schedule.get("at")) if scout.schedule.get("kind") == "daily" else None
    return f"{STARTS_TONIGHT} at {clock[0]}" if clock is not None else STARTS_TONIGHT


def _status(
    state: str, read: Standing, scout: Standing, browser_ran: datetime | None, system_off: str | None = None,
    unreadable: str | None = None, turn: int | None = None, learned: bool = False, learn_note: str | None = None,
    per_night: int | None = None, no_route: str | None = None, by_hand: bool = False,
) -> str:
    """The Status column, in plain words: the state, and what the mission behind it is doing (`read` and `scout`: the
    missions' standing; `browser_ran`: when the browser mission last ran, None if it never has; `system_off`: the
    name of the job system the owner switched off that this site's employer posts on, if it is one). The night's read
    skips such a site whatever its state, so that is said first; when that system is one no screen offers while it is
    off (`by_hand`: `ats.NOT_OFFERED`), the words send the owner to no switch. Off says nothing, unless the app
    switched the site
    off because it cannot be read (`unreadable`: why, as `off_note` has it): "Off: can't be read (<why>)", or, when
    it could not learn how to read it in the owner's browser, "Off: couldn't learn how to read it in your browser
    (<why>)" (its `off_note` is that whole clause). A site read in the browser (docs/PRD.md 37.8) says "Read in your
    browser: not set up yet" until that mission has run; then "Learning in your browser: <note>" with no recipe and a
    note (`learn_note`: the last attempt failed, or its recipe stopped fitting), "Read in your browser: waiting to be
    learned" with neither, and "Read in your browser: last run Oct 7" once `learned` (it has a browser recipe). A
    site waiting for its project whose employer posts on a system the app can't read, and knows why (`no_route`: "Acme
    Jobs can't be read: its job list is behind a route its robots.txt disallows"), says that and that it goes to the
    owner's browser tonight: no project starts for it (`read_probe.hand_no_reader` hands it over, in the night's read
    and before the Scout mission starts its projects)."""
    if state == OFF:
        if not unreadable:
            return ""
        return f"Off: {unreadable}" if unreadable.startswith(LEARN_OFF) else f"Off: can't be read ({unreadable})"
    if system_off:
        if by_hand:
            return f"Off: resumebot doesn't read {system_off} unless it is switched on by hand"
        return f"Off: {system_off} is switched off"
    if state == WORKING:
        return read_words(read)
    if state == WAITING:
        if no_route:
            return f"{no_route}. Goes to your browser tonight"
        return start_words(scout, turn, per_night)
    if state == BROWSER:
        if browser_ran is None:
            return "Read in your browser: not set up yet"
        if not learned:
            return (f"Learning in your browser: {learn_note}" if learn_note
                    else "Read in your browser: waiting to be learned")
        return f"Read in your browser: last run {_short_date(browser_ran)}"
    return STATE_WORDS.get(state, "")


#: The kind of a skill that is a job system's built-in reader (`skills.AdapterRecipe`), as against a recipe learned for
#: the site (`request`, `list_page`, `browser`).
ADAPTER_KIND = "adapter"


def _no_route(employer: Employer | None) -> str | None:
    """Why the app can't read a site through its employer's job system, when it knows for sure: "Acme Jobs can't be
    read: its job list is behind a route its robots.txt disallows" (`System.why_not`, the reason the Job systems section
    gives). None when the system has a reader, or none is known, or nothing is known for sure of why it has none."""
    system = ats.SYSTEMS.get(employer.ats or "") if employer is not None else None
    if system is None or not system.why_not or system.key in adapters.ADAPTERS:
        return None
    return f"{system.name} can't be read: {system.why_not}"


def _read_note(site: Site, history: list[Project]) -> str | None:
    """Why a site is read the way it is, as one sentence the open row says as "Found out: ...": the note kept on the
    site (what the server's test found, or the reason it was handed to the owner's browser), else, for a site in the
    browser, the reason the project that handed it over ended with (a site handed over before its row kept the note).
    `history` is the site's projects, newest first."""
    if site.read_note or effective_read(site) != BY_BROWSER:
        return site.read_note
    for project in history:
        reason = resolution.browser_reason((project.resolution or {}).get("outcome"))
        if reason:
            return reason
    return None


def _place(url: object) -> tuple[str, str] | None:
    """A link as `(host, path)`: the host as `avoid.host_of` says it (lowercase, no `www.`), the path lowercase without
    its trailing slash (empty for the host's front page). None for a link with no host."""
    if not isinstance(url, str) or not url.strip():
        return None
    return _place_of(url.strip())


@lru_cache(maxsize=32768)
def _place_of(text: str) -> tuple[str, str] | None:
    """`_place` of a stripped address. It depends on the text alone, so it is remembered: the same thousands of role
    links are read on every request."""
    host = avoid.host_of(text)
    if host is None:
        return None
    return host, urlsplit(text if "//" in text else f"//{text}").path.rstrip("/").lower()


@lru_cache(maxsize=32768)
def _board_of(text: str) -> tuple[str | None, bool]:
    """What `ats.parse` says of a link, as `(its job system's key or None, whether it names a board)`. `parse` reads
    the address alone and the job systems are fixed once loaded, so the answer is remembered by the text."""
    named = ats.parse(text)
    return (named.board.system if named and named.board else None), bool(named and named.board)


def _suffixes(hosts: Iterable[str]) -> dict[str, list[str]]:
    """Each host under every name that covers it (`avoid.covers`: the host itself and what follows each of its dots),
    so a site finds the hosts it covers by looking up its own host."""
    index: dict[str, list[str]] = {}
    for host in hosts:
        index.setdefault(host, []).append(host)
        for i, char in enumerate(host):
            if char == ".":
                index.setdefault(host[i + 1:], []).append(host)
    return index


def _under(site: tuple[str, str], link: tuple[str, str]) -> bool:
    """Whether a link is at a site's address: on its host or a subdomain of it, and, when the address has a path, under
    that path (a whole step of it, so `/acme` holds `/acme/jobs` and not `/acmeco`)."""
    (host, path), (link_host, link_path) = site, link
    return avoid.covers(host, link_host) and (not path or link_path == path or link_path.startswith(f"{path}/"))


def _postings_of(owner: str, sites: list[Site], systems: list[str]):
    """The statement for the postings that can say where a role was found: those of a role that is not gated out (the
    rest are dropped by `rank` below, so they are not read), and only the sources asked for: a posting is a site's by
    its `employer_id` (the site's id, or the id of its employer) and a job system's by its `ats`."""
    source_ids = {s.id for s in sites} | {s.employer_id for s in sites if s.employer_id is not None}
    wanted = [JobPosting.employer_id.in_(source_ids)] if source_ids else []
    if systems:
        wanted.append(JobPosting.ats.in_(systems))
    return (select(JobPosting.opportunity_id, JobPosting.employer_id, JobPosting.ats)
            .join(Opportunity, Opportunity.id == JobPosting.opportunity_id)
            .where(JobPosting.owner == owner, Opportunity.owner == owner, Opportunity.status != GATED_OUT,
                   or_(*wanted)))


async def roles_by_source(
    session: AsyncSession, owner: str, sites: Iterable[Site] = (), systems: Iterable[str] = (),
) -> dict[str, list[uuid.UUID]]:
    """The one rule for where a role was found (docs/PRD.md 30.3), which is the count on a row and the list behind it:
    the ids of the owner's roles found at each source, newest first, by the source's key (a site's `str(id)`, a job
    system's key; one with none has `[]`). A role that is gated out is never found anywhere.

    A role was found at a source when EITHER one of the source's postings became it (`JobPosting.opportunity_id`) OR
    the link it was found at points there. A job system's postings are those it read, `ats` its key. A site's are those
    it read itself: for an employer's site, the postings at that employer that no job system read (`ats` is not a
    system's key: the site's own learned skill, `own_site`), and, for any site, the postings under the site's own id (a
    job board's, or one the owner's browser read). The link is the job board link the role was found at (`board_url`,
    kept when the employer's own posting became its `url`), else its `url`, else its `apply_url`: it points at a
    site when it is on the site's host or a subdomain of it (`www.` ignored, `_under`), and, when the site's address is
    on a job system's shared host (`ats.parse` names its board), under the site's path; it points at a job system when
    `ats.parse` says it is on that system.

    Roles are few, so this reads them once and matches here, not in a statement per source."""
    sites, systems = list(sites), list(systems)
    found: dict[str, list[uuid.UUID]] = {**{str(s.id): [] for s in sites}, **{key: [] for key in systems}}
    if not found:
        return found
    roles = (await session.execute(
        select(Opportunity.id, Opportunity.board_url, Opportunity.url, Opportunity.apply_url)
        .where(Opportunity.owner == owner, Opportunity.status != GATED_OUT)
        .order_by(Opportunity.created_at.desc(), Opportunity.id))).all()
    if not roles:
        return found
    rank = {role_id: i for i, (role_id, _, _, _) in enumerate(roles)}  # newest first
    at_host: dict[str, list[tuple[uuid.UUID, str]]] = {}
    on_system: dict[str, set[uuid.UUID]] = {}
    for role_id, board_url, url, apply_url in roles:
        # Where it was found: the job board link it was found at, kept when the employer's own posting became its `url`.
        link = (board_url or "").strip() or (url or "").strip() or (apply_url or "").strip()
        place = _place(link)
        if place is not None:
            at_host.setdefault(place[0], []).append((role_id, place[1]))
        system = _board_of(link)[0] if systems else None
        if system:
            on_system.setdefault(system, set()).add(role_id)
    # what the source's postings became: by job system, by the employer or site they were read under, and (for an
    # employer's own site) by the employer when no job system read them
    by_system: dict[str, set[uuid.UUID]] = {}
    by_source: dict[uuid.UUID, set[uuid.UUID]] = {}
    own_site: dict[uuid.UUID, set[uuid.UUID]] = {}
    for role_id, source_id, system in (await session.execute(_postings_of(owner, sites, systems))).all():
        by_system.setdefault(system, set()).add(role_id)
        by_source.setdefault(source_id, set()).add(role_id)
        if system not in ats.SYSTEMS:
            own_site.setdefault(source_id, set()).add(role_id)
    for key in systems:
        found[key] = sorted((by_system.get(key, set()) | on_system.get(key, set())) & rank.keys(), key=rank.__getitem__)
    covering = _suffixes(at_host)
    for site in sites:
        mine = set(by_source.get(site.id, ()))
        if site.employer_id is not None:
            mine |= own_site.get(site.employer_id, set())
        address = _place(site.url)
        # A path narrows the match only on a job system's shared host, where many employers sit under one host
        # (recruiting.example-ats.test/acme): a board's own address (jobs.example.test/search) is the whole host.
        if address is not None and address[1] and not _board_of(site.url)[1]:
            address = (address[0], "")
        if address is not None:
            for host in covering.get(address[0], ()):
                mine.update(role_id for role_id, path in at_host[host] if _under(address, (host, path)))
        found[str(site.id)] = sorted(mine & rank.keys(), key=rank.__getitem__)
    return found


async def role_page(session: AsyncSession, owner: str, ids: list[uuid.UUID], offset: int, limit: int) -> dict:
    """A page of the roles in `ids` (`roles_by_source`, newest first, kept in that order): `{total, items: [{id, title,
    company, status, fit, fit_pct, found_at}]}`. `status` is the role's stage as stored; `found_at` is when the role was
    made; `fit` is the stored fit and `fit_pct` the same as a share of what the owner's asks allow (as the board shows
    it), each null for a role not scored yet."""
    page = ids[offset:offset + limit]
    found = {o.id: o for o in (await session.scalars(
        select(Opportunity).where(Opportunity.owner == owner, Opportunity.id.in_(page)))).all()} if page else {}
    span = scoring.bounds(await profile_store.current(session, owner)) if found else None
    return {"total": len(ids), "items": [
        {"id": str(o.id), "title": o.title, "company": o.company, "status": o.status, "fit": o.fit,
         "fit_pct": scoring.percent_of(o, span), "found_at": iso_utc(o.created_at)}
        for o in (found[i] for i in page if i in found)]}


async def roles_of_site(
    session: AsyncSession, owner: str, site_id: uuid.UUID, offset: int = 0, limit: int = 10,
) -> dict:
    """The roles found at a site (`roles_by_source`), newest first, a page at a time. `NoSuchSite` for a site that is
    not the owner's. A site to avoid has none."""
    site = await _site(session, owner, site_id)
    if site.list_name == AVOID:
        return {"total": 0, "items": []}
    ids = (await roles_by_source(session, owner, sites=[site]))[str(site.id)]
    return await role_page(session, owner, ids, offset, limit)


async def through_systems(session: AsyncSession, owner: str, rows: list[dict]) -> dict[str, str]:
    """The sites in `rows` (as `listing` made them) that belong to a job system, as `{site id: system key}`: an
    employer's site on the list to search that is on, read by the app, working, with the built-in reader of the system
    its employer posts on as the skill in force, that system being one the app has a reader for (`adapters.ADAPTERS`)
    and the employer's board complete. A learned recipe, the owner's browser, a question for the owner, a project still
    learning or waiting, a board the app cannot address and a system with no reader all leave a site on its own.
    Whether the owner switched the system off is not asked here: the caller decides what that means. (A site is
    `working` only when the app reads it: `listing` gives the browser and the not-known-yet their own states.)"""
    mine = {r["id"]: uuid.UUID(r["employer_id"]) for r in rows if r["employer_id"] and r["list"] == SEARCH and r["on"]
            and r["state"] == WORKING and (r["skill"] or {}).get("kind") == ADAPTER_KIND}
    if not mine:
        return {}
    employers = {e.id: e for e in (await session.scalars(
        select(Employer).where(Employer.owner == owner, Employer.id.in_(set(mine.values()))))).all()}
    return {site_id: employer.ats for site_id, employer_id in mine.items()
            if (employer := employers.get(employer_id)) is not None and employer.ats in adapters.ADAPTERS
            and ats.complete(employer.ats, employer.board)}


#: The recipe kinds the app runs by code alone (an ATS adapter, a replayed data request or feed, embedded data, plain
#: list pages): a site with one in force is read without the browser and is listed in the API section of Where to look.
CODE_KINDS = ("adapter", "request", "embedded", "list_page", "page", "html")


async def listing(
    session: AsyncSession, owner: str, site_id: uuid.UUID | None = None, *, with_roles: bool = True,
    sites: list[Site] | None = None, roles: dict[str, list[uuid.UUID]] | None = None,
) -> list[dict]:
    """The owner's sites on both lists, each as `{id, url, kind, label, on, employer_id, state, off_by, off_note,
    status, skill, needs, roles, added_by, list, read_by, read_found, read_note, created_at}`.

    `status` is what is true of the site now, in plain words, from its state and the missions behind it (the module
    docstring); empty when it is off, except a site the app switched off because it cannot be read: `off_by` is `app`,
    `off_note` says why in one clause and the status is "Off: can't be read (<why>)" (both null for any other site).
    `by_code` is true for a site that is on and read by code alone (a working skill of a kind in `CODE_KINDS`); `off_at`
    is when the app switched it off (with `off_by`). `created_at` is when the site was added to the list.
    A site the app switched off because it could not learn it has
    the status "Off: couldn't learn how to read it in your browser (<why>)" and `off_note` that clause.

    `list` is `search` or `avoid` (a site to avoid is never on and has no state, skill or roles); `read_by` is `auto`,
    `app` or `browser`; an `auto` site's `read_found` is what the test found (`app` or `browser`) and `read_note`
    why; so is the `read_note` of a site handed to the browser (why it was: `_read_note`). A site that is on and read
    in the browser (`effective_read`) has the state `browser`, or `needs_you` while an
    Inbox item says the browser could not read it (its `needs` is that item's sentence). `label` is the employer's name,
    else what the owner called it, else the address's host. `skill` is the working skill `{version, kind}` or null.
    `needs` is one plain line, only in the states `needs_you` and `stopped`. `roles` is how many distinct roles were
    found at the site (`roles_by_source`; 0, never blank, when none; 0 for a site to avoid, and when `with_roles` is
    off, for a caller that only wants the states). Every site is here, an employer's that is off too, and an employer's
    that belongs to its job system (`through_systems`); `page` is what Where to look shows.

    A caller that has already read what the listing would read passes it: `sites` (the owner's sites, all of them) and
    `roles` (`roles_by_source` for the sites that are not to avoid, the lists by `str(site id)`)."""
    if sites is None:
        query = select(Site).where(Site.owner == owner)
        if site_id is not None:
            query = query.where(Site.id == site_id)
        sites = list((await session.scalars(query)).all())
    rows = sites
    if not rows:
        return []
    ids = [s.id for s in rows]
    employer_ids = [s.employer_id for s in rows if s.employer_id is not None]
    employers = {e.id: e for e in (await session.scalars(select(Employer).where(Employer.id.in_(employer_ids)))).all()}
    current: dict[uuid.UUID, SiteSkill] = {}
    for skill in (await session.scalars(
        select(SiteSkill).where(SiteSkill.site_id.in_(ids), SiteSkill.retired_at.is_(None)).order_by(SiteSkill.version)
    )).all():
        current[skill.site_id] = skill  # the newest version last wins
    projects: dict[uuid.UUID, list[Project]] = {}
    for project in (await session.scalars(
        select(Project).where(Project.site_id.in_(ids)).order_by(Project.created_at.desc())
    )).all():
        projects.setdefault(project.site_id, []).append(project)
    open_ids = [p.id for ps in projects.values() for p in ps if p.finished_at is None]
    tasks: dict[uuid.UUID, list[Task]] = {}
    if open_ids:
        for task in (await session.scalars(
            select(Task).where(Task.owner == owner, Task.state == TASK_OPEN, Task.link_kind == PROJECT_LINK,
                               Task.link_id.in_(open_ids)).order_by(Task.created_at.desc())
        )).all():
            tasks.setdefault(task.link_id, []).append(task)
    if roles is None:
        roles = (await roles_by_source(session, owner, sites=[s for s in rows if s.list_name != AVOID])
                 if with_roles else {})
    read_standing = await missions.standing(session, owner, record.READ)
    scout_standing = await missions.standing(session, owner, record.SCOUT)
    systems_off = set((await profile_store.current(session, owner)).settings.job_systems_off)
    browser_ran = None
    if any(s.on and effective_read(s) == BY_BROWSER for s in rows):
        browser_ran = (await missions.standing(session, owner, record.BROWSER)).last_run_at
    # A site with no skill in force, switched on, with no project at work: has its skill broken since the last project?
    unread = {site.id: (projects[site.id][0].finished_at if projects.get(site.id) else None) for site in rows
              if site.list_name != AVOID and site.on and effective_read(site) != BY_BROWSER
              and current.get(site.id) is None and not any(p.finished_at is None for p in projects.get(site.id, []))}
    broke_ids = await skills.broken_sites(session, unread)
    states: dict[uuid.UUID, tuple[str, str | None]] = {}
    for site in rows:
        skill = current.get(site.id)
        history = projects.get(site.id, [])
        read = effective_read(site)
        if site.list_name == AVOID:
            states[site.id] = OFF, None
        elif site.on and read == BY_BROWSER:
            states[site.id] = BROWSER, None
        else:
            states[site.id] = _state(site, skill, history, tasks, site.id in broke_ids)
    # The sites waiting for a project, in the order tonight's Scout mission takes them (`autostart.start_queue`), and
    # each one's turn in it. (One site asked for alone: its turn is found below, from the whole list.) A site whose
    # employer is on a system nothing reads goes to the browser instead, so it has no turn and takes none.
    queue = [s.id for s in (await session.scalars(autostart.start_queue(owner))).all()
             if states.get(s.id, (None,))[0] == WAITING
             and _no_route(employers.get(s.employer_id) if s.employer_id else None) is None] if site_id is None else []
    turns = {sid: turn for turn, sid in enumerate(queue)}
    per_night = (await profile_store.current(session, owner)).settings.site_projects_per_night
    out = []
    for site in rows:
        skill = current.get(site.id)
        state, needs = states[site.id]
        employer = employers.get(site.employer_id) if site.employer_id else None
        # An employer whose job system the owner switched off is skipped by the night's read, whatever its site's state.
        off_system = ats.label(employer.ats) if employer is not None and employer.ats in systems_off else None
        out.append({
            "id": str(site.id), "url": site.url, "kind": site.kind,
            "label": (employer.name if employer else None) or site.label or _host(site.url),
            "on": site.on, "employer_id": str(site.employer_id) if site.employer_id else None,
            "state": state, "off_by": site.off_by, "off_note": site.off_note,
            "off_at": iso_utc(site.off_at) if site.off_at else None,
            "by_code": bool(site.on and site.list_name != AVOID and skill is not None
                            and (skill.recipe or {}).get("kind") in CODE_KINDS),
            "status": _status(
                state, read_standing, scout_standing, browser_ran, off_system,
                site.off_note if site.off_by == OFF_BY_APP else None,
                turns.get(site.id), (skill.recipe or {}).get("kind") == "browser" if skill else False,
                site.learn_note, per_night, _no_route(employer) if state == WAITING else None,
                by_hand=employer is not None and employer.ats in ats.NOT_OFFERED),
            "skill": {"version": skill.version, "kind": (skill.recipe or {}).get("kind")} if skill else None,
            "needs": needs if state in (NEEDS_YOU, STOPPED) else None,
            "roles": len(roles.get(str(site.id), ())),
            "added_by": site.added_by, "list": site.list_name, "read_by": site.read_by,
            "read_found": site.read_found, "read_note": _read_note(site, projects.get(site.id, [])),
            "created_at": iso_utc(site.created_at),
        })
    if site_id is not None and out and out[0]["state"] == WAITING:
        # Whose turn it is depends on the other waiting sites, so a site asked for alone takes its words from the list.
        everyone = {row["id"]: row for row in await listing(session, owner, with_roles=False)}
        out[0]["status"] = everyone[out[0]["id"]]["status"]
    return sorted(out, key=lambda s: (not s["on"], s["label"].lower(), s["url"]))


async def page(
    session: AsyncSession, owner: str, *, rows: list[dict] | None = None, through: dict[str, str] | None = None,
) -> list[dict]:
    """What Where to look shows (docs/PRD.md 30.3), the sites that are read on their own: the sites to avoid, the sites
    the owner added (on or off) and the employers' sites that are switched on. An employer's site that is off is not
    listed: it is switched on from the Employers screen. Nor is one that belongs to its job system (`through_systems`)
    while the owner has that system on: the system's row speaks for it, and it comes back here, saying the system is
    off, when the owner switches the system off. An employer's site the app switched off because it cannot be read
    (`off_by`) stays here, saying so, since the owner is told to turn it back on on this page. A caller that has made
    `listing` and `through_systems` already passes them as `rows` and `through`."""
    if rows is None:
        rows = await listing(session, owner)
    if through is None:
        through = await through_systems(session, owner, rows)
    off = set((await profile_store.current(session, owner)).settings.job_systems_off)
    return [s for s in rows if (s["list"] == AVOID or not s["employer_id"] or s["on"] or s["off_by"] == OFF_BY_APP)
            and not (s["id"] in through and through[s["id"]] not in off)]
