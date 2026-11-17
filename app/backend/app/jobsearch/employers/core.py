"""Employers as records (docs/PRD.md path 21): matching a role to its employer, linking every role, the facts scoring
reads, and the owner's edits. Services flush; the route or command commits.
"""

import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.errors import DomainError
from app.jobsearch import ats, off_turn, stages
from app.jobsearch.actors import BY_APP, BY_OWNER, actor_name
from app.jobsearch.employers import not_checked
from app.jobsearch.fetch.polite import public_url
from app.jobsearch.measures import dials
from app.jobsearch.measures.places import match as place_match
from app.jobsearch.models import ADDED_LINK, OWNERSHIPS, WORK_STYLES, Employer, JobPosting, Opportunity, is_unnamed
from app.jobsearch.profile import store as profile_store
from app.jobsearch.states import EMPLOYER_LINE, FetchKind, StopGate, StopStep
from app.models.work import TrailEvent
from app.sites import watch_record
from app.timeutil import iso_utc, owner_today
from app.web_address import http_url

#: Words that don't tell one employer from another ("Globex Inc." is "Globex"). "and" is one, so that "&" and "and" are
#: the same ("Northwind & Fabrikam", "Northwind and Fabrikam") and the tail of a legal name leaves nothing behind
#: ("Globex and Company" and "Globex & Co." are "Globex", not "Globex and"). "companies" is one as "company" is: "Globex
#: Insurance Group, Inc." and "Globex Insurance Companies" are one employer, whose earlier application the employer gap
#: has to see under either name.
_LEGAL_WORDS = r"the|and|inc|incorporated|llc|l\.l\.c|ltd|limited|co|company|companies|corp|corporation|plc|lp|llp"
_NOISE = re.compile(rf"\b({_LEGAL_WORDS}|group|holdings)\b\.?", re.I)
#: The same without "group" and "holdings": the words that are only a legal form, never part of what the name says.
_LEGAL = re.compile(rf"\b({_LEGAL_WORDS})\b\.?", re.I)
#: "US" and "USA" are noise only at the end of a name ("Globex USA" is "Globex"); at the start they are the name ("US
#: Bank" is not "Bank").
_COUNTRY_TAIL = ("us", "usa")
#: The fields an owner can set by hand, and how each is read.
EDITABLE = {
    "name": str, "aliases": list, "website": str, "careers_url": str, "headquarters": str, "offices": list,
    "local": bool, "work_style": str, "employees": int, "founded": int, "industry": str, "sectors": list,
    "ownership": str, "ai_native": bool, "events": list, "check_directly": bool, "group": str, "notes": str,
    "gated": bool, "gated_reason": str,
}


class EmployerError(DomainError):
    status = 422


class NoSuchEmployer(EmployerError):
    status = 404


def key_of(name: str | None) -> str:
    """The matching key: lowercase, parentheses ("Globex (via Fabrikam)", "Globex (Retail)") and noise words out,
    punctuation and spacing folded. Employer records are stored under it (`Employer.key`); a change to this rule needs a
    migration that gives each record the key the new rule makes (0041, 0056)."""
    text = re.sub(r"\(.*?\)", " ", (name or "").lower())
    text = _NOISE.sub(" ", text)
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    while len(words) > 1 and words[-1] in _COUNTRY_TAIL:
        words.pop()
    return " ".join(words)[:200]


def whole_key_of(name: str | None) -> str:
    """The name as a message would write it, for reading mail (`mail/reader.py`): `key_of` with "Group" kept
    ("Northfield Group" is not "Northfield", which "Harbor Northfield" holds too), and "Holdings" dropped only at the
    end. It is no stored key: nothing matches an employer record by it."""
    text = _LEGAL.sub(" ", re.sub(r"\(.*?\)", " ", (name or "").lower()))
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    while len(words) > 1 and words[-1] in ("holdings", *_COUNTRY_TAIL):
        words.pop()
    return " ".join(words)[:200]


#: The key the placeholder name "Added link" (`models.ADDED_LINK`) would be stored under: no employer record has it.
PLACEHOLDER_KEY = "added link"


async def find(session: AsyncSession, owner: str, name: str | None) -> Employer | None:
    """The employer a name means: by key, then by any alias's key."""
    key = key_of(name)
    if not key or key == PLACEHOLDER_KEY:  # "Added link" is no employer: nothing is found, linked or merged under it
        return None
    found = await session.scalar(select(Employer).where(Employer.owner == owner, Employer.key == key))
    if found is not None:
        return found
    rows = (await session.scalars(select(Employer).where(Employer.owner == owner, Employer.aliases != []))).all()
    return next((e for e in rows if key in {key_of(a) for a in e.aliases or []}), None)


async def find_many(session: AsyncSession, owner: str,
                    names: Iterable[str | None]) -> dict[str | None, Employer | None]:
    """`find` for each of these names, in at most two statements: one select for the distinct keys, and the alias scan
    once, only when a key missed. Each name maps to what `find` would return for it (key first, then the first employer
    whose alias has the key, in the same row order)."""
    names = list(dict.fromkeys(names))
    keys = {n: key_of(n) for n in names}
    wanted = {k for k in keys.values() if k and k != PLACEHOLDER_KEY}
    by_key: dict[str, Employer] = {}
    if wanted:
        by_key = {e.key: e for e in (await session.scalars(
            select(Employer).where(Employer.owner == owner, Employer.key.in_(wanted)))).all()}
    aliased: list[Employer] | None = None
    out: dict[str | None, Employer | None] = {}
    for n in names:
        k = keys[n]
        if not k or k == PLACEHOLDER_KEY:
            out[n] = None
        elif k in by_key:
            out[n] = by_key[k]
        else:
            if aliased is None:
                aliased = list((await session.scalars(
                    select(Employer).where(Employer.owner == owner, Employer.aliases != []))).all())
            out[n] = next((e for e in aliased if k in {key_of(a) for a in e.aliases or []}), None)
    return out


async def find_or_make(session: AsyncSession, owner: str, name: str) -> Employer | None:
    found = await find(session, owner, name)
    if found is not None or not key_of(name) or key_of(name) == PLACEHOLDER_KEY:
        return found
    return await make(session, owner, name)


async def make(session: AsyncSession, owner: str, name: str) -> Employer:
    """A new employer for a name `find` did not find (the caller has looked)."""
    employer = Employer(owner=owner, key=key_of(name), name=" ".join(name.split()), aliases=[], offices=[],
                        sectors=[], events=[], sources={}, owner_fields=[])
    session.add(employer)
    await session.flush()
    return employer


def learn_careers_url(employer: Employer, link: str | None, source: str, by: str = BY_APP) -> bool:
    """Keep a careers site the app has learned (from the job system the employer posts through, from the scout finding
    its board, from the analyst), with where it came from. Only where the record has none: a careers page already known
    stays, and one the owner set by hand, or cleared by hand, is theirs and is never touched (`owner_fields`). Returns
    whether it was set. Only a public web address is kept: it is shown as a link, and the night reads it."""
    link = http_url(link)
    if not link or not public_url(link) or employer.careers_url or "careers_url" in (employer.owner_fields or []):
        return False
    employer.careers_url = link[:500]
    employer.sources = {**(employer.sources or {}),
                        "careers_url": {"source": source, "at": datetime.now(UTC).isoformat(), "by": by}}
    return True


#: The mark in an employer's `sources` of the scout's answer for its job board (`doors/tools.py`: `{"at": ...}`, set
#: whatever the answer said, a board, its own careers page or none to find). While it stands the scout is not handed
#: the employer again; only `ask_scout_again` takes it away, and no timer does (docs/PRD.md 28.3).
BOARD_ASKED = "board_asked"


def ask_scout_again(employer: Employer) -> bool:
    """The app's reading of the employer's site has failed for good (its job system's board is gone: `resolve.forget`;
    or no means can read its careers page: `where.turn_off_unreadable` and `turn_off_unlearnable`), so the scout is
    handed the employer again from the next scout night: the mark of its earlier answer is taken away, and nothing else
    of the employer changes. The mark of the night that handed it over stays (an employer is handed over once a night),
    and so does the scout's "no board to find", which is for good. Returns whether there was an answer to take away."""
    sources = employer.sources or {}
    if BOARD_ASKED not in sources or employer.fetch_kind == FetchKind.BOARD_NOT_FOUND:
        return False
    employer.sources = {k: v for k, v in sources.items() if k != BOARD_ASKED}
    return True


REVIEWS_URL_CHARS = 500


def reviews_link(value: object) -> str | None:
    """`value` as the address of an employer's page on an employer-review site, or None: a public `https` address with
    no sign-in in it. It is only ever shown as a link for the owner to open; the app never opens it, so an address is
    kept only as a public web search showed it. Which site it is on is not checked."""
    link = http_url(value)
    if not link or len(link) > REVIEWS_URL_CHARS or not public_url(link):
        return None
    parts = urlsplit(link)
    if parts.scheme.lower() != "https" or parts.username or parts.password:
        return None
    return link


async def link_roles(session: AsyncSession, owner: str) -> int:
    """Give every role without an employer its employer record (made when there is none). Returns how many linked. A job
    the owner added by its link has none until research names it (`models.is_unnamed`): "Added link" is no employer.
    The roles are taken oldest first, so a record two names share ("Globex Inc.", "GLOBEX") is named by the role that
    came first: unordered, Postgres returned them in whatever order the table's free space held them (found as a test
    that failed only in the full run, where a worker's tables are emptied by DELETE and reused).

    Whether any role has none is a read in a reader's session (`off_turn.reading`), so a start with every role linked
    (and the owner's added links, which wait for research, are not counted) takes no writer turn here. The pass never
    ends its transaction: `settle` is also the import's, whose caller may roll it all back (a dry run)."""
    async with off_turn.reading(session) as reader:
        waiting = (await reader.scalars(select(Opportunity.id).where(
            Opportunity.owner == owner, Opportunity.employer_id.is_(None), Opportunity.company != ADDED_LINK)
            .limit(1))).first()
    if waiting is None:
        return 0
    rows = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.employer_id.is_(None))
        .order_by(Opportunity.created_at, Opportunity.id))).all()
    linked = 0
    for role in rows:
        if is_unnamed(role):
            continue
        employer = await find_or_make(session, owner, role.company)
        if employer is not None:
            role.employer_id = employer.id
            linked += 1
    await session.flush()
    return linked


async def mark_local(session: AsyncSession, owner: str) -> None:
    """An employer is local when one of its offices, or a place one of its roles names, is among the places the owner
    works on site, hybrid or in person (the work location gate's places), by the gate's own rule: a town the owner wrote
    with its state is that town in that state only, one written alone is named in any state, and an office that names
    the town and no state is not known to be local (`places.match`).

    Every employer is judged in a reader's session (`off_turn.reading`: the writer's turn is not held for it), and only
    the employers whose answer differs from what is stored are
    written, each read again first: one the owner set by hand, or that changed since the read, is left as it is. A
    start that changes nothing takes no turn."""
    changes: dict[uuid.UUID, tuple[bool | None, bool]] = {}
    async with off_turn.reading(session) as reader:
        location = (await profile_store.current(reader, owner)).gates.location
        towns = sorted({t.lower() for t in [*location.hybrid.locations, *location.in_person.locations] if t})
        if not towns:
            return
        places: dict[uuid.UUID, list[str]] = {}
        for employer_id, place in (await reader.execute(select(Opportunity.employer_id, Opportunity.location).where(
                Opportunity.owner == owner, Opportunity.employer_id.is_not(None),
                Opportunity.location.is_not(None)))).all():
            if employer_id is not None and place is not None:  # the query asks for neither; this tells the checker
                places.setdefault(employer_id, []).append(place)
        for employer_id, owner_fields, offices, was in (await reader.execute(select(
                Employer.id, Employer.owner_fields, Employer.offices, Employer.local)
                .where(Employer.owner == owner))).all():
            if "local" in (owner_fields or []):
                continue
            seen = " | ".join([*(offices or []), *places.get(employer_id, [])])
            if seen:
                now = any(place_match(seen, t) is True for t in towns)
                if now != was:
                    changes[employer_id] = (was, now)
    if changes:
        for employer in (await session.scalars(select(Employer).where(
                Employer.owner == owner, db.among(Employer.id, list(changes))))).all():
            was, now = changes[employer.id]
            if employer.local == was and "local" not in (employer.owner_fields or []):
                employer.local = now
    await session.flush()


async def settle(session: AsyncSession, owner: str) -> dict:
    """Link every role and work out which employers are local. Safe to run again. (The watchlist an older profile held
    was folded in once, by migration 0041; doing it at every start undid the owner's switches.)"""
    linked = await link_roles(session, owner)
    await mark_local(session, owner)
    return {"linked": linked}


# --- facts for scoring (docs/PRD.md 21.4) -------------------------------------------------------------------------


#: What an Activity line about an employer links to (`employer_records.LINK`, the same word).
EMPLOYER_LINK = "employer"
#: The span `layoffs_in_2_years` counts dated layoff events over.
LAYOFF_WINDOW_DAYS = 730


def facts(employer: Employer | None, today: date | None = None) -> dict:
    """What scoring can read about an employer, by fact name; a fact the record doesn't hold is left out."""
    if employer is None:
        return {}
    today = today or owner_today()
    out: dict = {}
    if employer.founded:
        out["years_since_founded"] = today.year - employer.founded
    if employer.employees is not None:
        out["employees"] = employer.employees
        out["size_band"] = dials.size_band(employer.employees)
    if employer.ownership and employer.ownership != "unknown":
        out["ownership"] = employer.ownership
    if employer.ai_native is not None:
        out["ai_native"] = employer.ai_native
    if employer.industry or employer.sectors:
        out["industry"] = " ".join([employer.industry or "", *(employer.sectors or [])]).lower()
    if employer.local is not None:
        out["local"] = employer.local
    if employer.work_style and employer.work_style != "unknown":
        out["work_style"] = employer.work_style
    layoffs = [e for e in employer.events or [] if str(e.get("kind", "")).lower() == "layoff" and e.get("date")]
    if layoffs:
        newest = max(str(e["date"]) for e in layoffs)
        try:
            year, month = (int(x) for x in (newest + "-01").split("-")[:2])
            out["years_since_layoff"] = round((today.year - year) + (today.month - month) / 12, 2)
        except ValueError:
            pass
    elif "events" in (employer.sources or {}):
        out["years_since_layoff"] = None  # researched, and none found
    if layoffs or "events" in (employer.sources or {}):  # researched: none found counts 0; unresearched leaves it out
        recent = 0
        for e in layoffs:
            try:
                year, month = (int(x) for x in (str(e["date"]) + "-01").split("-")[:2])
                recent += (today - date(year, month, 1)).days <= LAYOFF_WINDOW_DAYS
            except ValueError:
                pass
        out["layoffs_in_2_years"] = recent
    return out


# --- reading and editing -----------------------------------------------------------------------------------------


def view(employer: Employer, roles: Sequence[Any] | None = None) -> dict:
    out = {
        **row(employer),
        "website": employer.website, "careers_url": employer.careers_url, "reviews_url": employer.reviews_url,
        "headquarters": employer.headquarters, "offices": employer.offices or [], "work_style": employer.work_style,
        "founded": employer.founded, "sectors": employer.sectors or [], "ownership": employer.ownership,
        "ai_native": employer.ai_native,
        "events": employer.events or [], "check_directly": employer.check_directly, "group": employer.group,
        "notes": employer.notes, "sources": employer.sources or {}, "owner_fields": employer.owner_fields or [],
        "gated_at": employer.gated_at.isoformat() if employer.gated_at else None,
        "ats_found_by": employer.ats_found_by,
        "fetched_at": employer.fetched_at.isoformat() if employer.fetched_at else None,
        "fetch_ok": employer.fetch_ok, "fetch_note": employer.fetch_note, "open_roles": employer.open_roles,
    }
    if roles is not None:
        out["roles"] = [{"id": str(r.id), "title": r.title, "status": r.status, "fit": r.fit,
                         "found": r.found.isoformat() if r.found else None} for r in roles]
    return out


def row(employer: Employer) -> dict:
    """An employer as the list of them shows it: the few facts a row reads (its name and aliases, whether it is local,
    its industry and size, the job system it posts on, whether it is hard-passed), not the whole record (`view`), which
    the employer's own page loads when it is opened."""
    return {
        "id": str(employer.id), "name": employer.name, "aliases": employer.aliases or [], "local": employer.local,
        "industry": employer.industry, "employees": employer.employees, "gated": bool(employer.gated),
        "gated_reason": employer.gated_reason, "ats": employer.ats, "ats_label": ats.label(employer.ats),
        "ats_how": ats.how(employer.ats), "board_url": employer.board_url,
        "ats_confirmed": (employer.board or {}).get("confirmed", True) if employer.ats else None,
    }


#: The list's views (the rail of Employers): every employer, those with open
#: roles, those whose own site is watched, those on the hard pass list, and those with a local office.
VIEWS = ("all", "open", "watched", "hard_pass", "local")
#: The stages from Ready to Launch on: an employer with a role there has its open-role count washed on the list.
AHEAD = (stages.READY_TO_LAUNCH, stages.APPLIED, stages.ACTIVE_DISCUSSION)
#: How many employers a page of the list holds, unless asked for another number (at most `MOST_PER_PAGE`).
PER_PAGE = 50
MOST_PER_PAGE = 500


def _view_clause(view: str, owner: str):
    """The test a view (`VIEWS`) puts to an employer, as SQL over its own columns and its roles and sites: the list
    reads no role and no site to answer it. An employer is watched when any of its sites is on (the
    one its row shows is an on one first)."""
    from app.sites.models import Site  # imported here to avoid an import loop with the sites

    if view == "open":
        return select(Opportunity.id).where(Opportunity.owner == owner, Opportunity.employer_id == Employer.id,
                                            Opportunity.status.in_(stages.OPEN)).exists()
    if view == "watched":
        return select(Site.id).where(Site.owner == owner, Site.employer_id == Employer.id, Site.on.is_(True)).exists()
    if view == "hard_pass":
        return Employer.gated.is_(True)
    if view == "local":
        return Employer.local.is_(True)
    return true()


def _names_hold(text: str, name: str | None, aliases: Sequence[str | None] | None) -> bool:
    """Whether its name, or another name it goes by, holds what was typed (any case; `text` is already lowered)."""
    return not text or any(text in (n or "").lower() for n in (name, *(aliases or [])))


async def rows(session: AsyncSession, owner: str, ids: list[uuid.UUID] | None = None) -> list[dict]:
    """Employers as the list shows them, by name: each one's `row`, how many roles it has (`roles_total`), how many are
    open (`roles_open`: new, ready, applied, interviewing), how many are in Ready to Launch or later (`roles_ahead`),
    when its board was last read (`fetched_at`) and its own site (`site`). `ids` keeps only those employers; their
    counts and sites are read for those alone."""
    by = (Opportunity.owner == owner,)
    if ids is not None:
        by = (*by, Opportunity.employer_id.in_(ids))
    total = (select(Opportunity.employer_id, func.count(),
                    func.count().filter(Opportunity.status.in_(stages.OPEN)),
                    func.count().filter(Opportunity.status.in_(AHEAD)))
             .where(*by).group_by(Opportunity.employer_id))
    counts = {k: (n, o, a) for k, n, o, a in (await session.execute(total)).all()}
    query = select(Employer).where(Employer.owner == owner)
    if ids is not None:
        query = query.where(Employer.id.in_(ids))
    found = (await session.scalars(query.order_by(Employer.name, Employer.id))).all()
    sites = await _sites_by_employer(session, owner, [e.id for e in found] if ids is not None else None)
    return [{**row(e), "roles_total": counts.get(e.id, (0, 0, 0))[0], "roles_open": counts.get(e.id, (0, 0, 0))[1],
             "roles_ahead": counts.get(e.id, (0, 0, 0))[2],
             "fetched_at": e.fetched_at.isoformat() if e.fetched_at else None,
             "site": sites.get(e.id)} for e in found]


def _start(cursor: str | None) -> int:
    """Where a page starts: the `next` the page before it gave (a count of the employers before it), else the first."""
    if not cursor:
        return 0
    if not cursor.isdigit():
        raise EmployerError("cursor is the next of the page before")
    return int(cursor)


async def listing(session: AsyncSession, owner: str, *, view: str = "all", q: str = "", industry: str = "",
                  cursor: str | None = None, limit: int = PER_PAGE) -> dict:
    """A page of the employers in a view (`VIEWS`), by name, narrowed to a name (`q`, any of the names it goes by) and
    an industry: `employers` (`rows`), `next` (the cursor of the page after it, None at the last), `total` (how many
    are in the view, so narrowed), `counts` (how many each view holds, of every employer: the rail's) and `industries`
    (every industry an employer is in, for the choice). The view, the industry, the order, the page and the counts are
    the database's; only the page's employers are read whole, with their role counts and sites."""
    if view not in VIEWS:
        raise EmployerError(f"view is one of {', '.join(VIEWS)}")
    start = _start(cursor)
    narrowed = select(Employer.id).where(Employer.owner == owner, _view_clause(view, owner))
    if industry:
        narrowed = narrowed.where(Employer.industry == industry)
    ordered = narrowed.order_by(Employer.name, Employer.id)
    text = q.strip().lower()
    if text:  # names are matched here, in the same words as ever (case, and the aliases, a JSON list)
        named = (await session.execute(ordered.add_columns(Employer.name, Employer.aliases))).all()
        every = [r[0] for r in named if _names_hold(text, r[1], r[2])]
        total, page_ids = len(every), every[start:start + limit]
    else:
        total = (await session.scalar(select(func.count()).select_from(narrowed.subquery()))) or 0
        page_ids = list((await session.scalars(ordered.offset(start).limit(limit))).all())
    end = start + limit
    shown = await rows(session, owner, page_ids) if page_ids else []
    kinds = (await session.scalars(select(Employer.industry).where(
        Employer.owner == owner, Employer.industry.is_not(None), Employer.industry != "").distinct())).all()
    return {"employers": shown, "next": str(end) if end < total else None, "total": total,
            "counts": await view_counts(session, owner), "industries": sorted(k for k in kinds if k)}


async def view_counts(session: AsyncSession, owner: str) -> dict[str, int]:
    """How many employers each view of the list holds now (the rail's counts), in one statement."""
    one = [select(func.count()).select_from(Employer).where(Employer.owner == owner, _view_clause(v, owner))
           .scalar_subquery() for v in VIEWS]
    return dict(zip(VIEWS, (await session.execute(select(*one))).one(), strict=True))


async def _sites_by_employer(session: AsyncSession, owner: str,
                             employer_ids: list[uuid.UUID] | None = None) -> dict[uuid.UUID, dict]:
    """Each employer's own site (docs/PRD.md 29.1), as its page shows it: the watch switch, what it needs, and its
    status in Where to look's words (which says when the employer's job system is switched off). Given `employer_ids`,
    only those employers' sites are read and built; a site waiting for a project takes its turn in the queue of every
    waiting site, so when one of them waits the whole list is read, as it is with no ids."""
    from app.sites import where_view  # imported here to avoid an import loop
    from app.sites.models import Site

    listed = None
    if employer_ids is not None:
        if not employer_ids:
            return {}
        own = list((await session.scalars(
            select(Site).where(Site.owner == owner, Site.employer_id.in_(employer_ids)))).all())
        if not own:
            return {}
        listed = await where_view.listing(session, owner, with_roles=False, sites=own)
        if any(site["state"] == where_view.WAITING for site in listed):
            listed = None
    if listed is None:
        listed = await where_view.listing(session, owner, with_roles=False)
    out: dict[uuid.UUID, dict] = {}
    for site in listed:
        if site["employer_id"]:
            out.setdefault(uuid.UUID(site["employer_id"]), {
                k: site[k] for k in ("id", "url", "on", "state", "needs", "status")})
    return out


def roles_shown(employer_id: uuid.UUID):
    """The employer pane's roles: the five fields it shows, newest found first, not whole role rows."""
    return (select(Opportunity.id, Opportunity.title, Opportunity.status, Opportunity.fit, Opportunity.found)
            .where(Opportunity.employer_id == employer_id).order_by(Opportunity.found.desc().nulls_last()))


async def detail(session: AsyncSession, owner: str, employer_id: uuid.UUID) -> dict:
    employer = await session.get(Employer, employer_id)
    if employer is None or employer.owner != owner:
        raise NoSuchEmployer("no such employer")
    roles = (await session.execute(roles_shown(employer.id))).all()
    site = (await _sites_by_employer(session, owner)).get(employer.id)
    if site is not None:  # why it is watched, when the app set the watch for a role in play (docs/PRD.md 29.9)
        site = {**site, "watched_for": watch_record.watched_for(employer, site["on"])}
    return {**view(employer, roles), "hiring": await hiring(session, employer), "site": site,
            "history": await history(session, owner, employer.id)}


#: How many lines of an employer's history its page shows, newest first.
HISTORY_SHOWN = 20


async def history(session: AsyncSession, owner: str, employer_id: uuid.UUID) -> list[dict]:
    """What happened to the employer, newest first (`HISTORY_SHOWN` at most): the Activity lines about it (merges,
    splits, hard passes, research), `{at, sentence}`."""
    lines = (await session.scalars(select(TrailEvent).where(
        TrailEvent.owner == owner, TrailEvent.link_kind == EMPLOYER_LINK, TrailEvent.link_id == employer_id)
        .order_by(TrailEvent.at.desc(), TrailEvent.id.desc()).limit(HISTORY_SHOWN))).all()
    return [{"at": iso_utc(line.at), "sentence": line.sentence} for line in lines]


async def hiring(session: AsyncSession, employer: Employer, days: int = 30) -> dict | None:
    """How fast the employer is hiring, counted from its own board in the posting index (docs/PRD.md 28.12): open
    now, opened and closed in the last `days`. The app's own count, not a reported fact; None before any read."""
    if employer.fetched_at is None:
        return None
    since = datetime.now(UTC) - timedelta(days=days)
    base = (JobPosting.owner == employer.owner, JobPosting.employer_id == employer.id)
    count = lambda *where: select(func.count()).select_from(JobPosting).where(*base, *where)  # noqa: E731
    first = await session.scalar(select(func.min(JobPosting.first_seen)).where(*base))
    return {"open": await session.scalar(count(JobPosting.is_open)) or 0,
            "opened": await session.scalar(count(JobPosting.first_seen >= since)) or 0,
            "closed": await session.scalar(count(JobPosting.closed_at >= since)) or 0,
            "days": days, "since": owner_today(first).isoformat() if first else None}


def _coerce(field: str, value):
    kind = EDITABLE[field]
    if value is None or value == "":
        return [] if kind is list else None
    if kind is list:
        if not isinstance(value, list):
            raise EmployerError(f"{field} is a list")
        return value
    if kind is bool:
        if not isinstance(value, bool):
            raise EmployerError(f"{field} is yes or no")
        return value
    if kind is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise EmployerError(f"{field} is a whole number") from None
    text = " ".join(str(value).split())
    if field == "work_style" and text not in WORK_STYLES:
        raise EmployerError(f"work_style is one of {', '.join(WORK_STYLES)}")
    if field == "ownership" and text not in OWNERSHIPS:
        raise EmployerError(f"ownership is one of {', '.join(OWNERSHIPS)}")
    return text


async def edit(session: AsyncSession, owner: str, employer_id: uuid.UUID, changes: dict, by: str = BY_OWNER, *,
               defer_gate_change: bool = False) -> dict:
    """Set facts by hand. An owner's fact is marked theirs (research leaves it alone) with its source.

    Gating or ungating an employer checks the log and the roles again (`gate_changed`) in the same save. With
    `defer_gate_change` the flag and its time are saved and the check is left to the caller (the page's route asks the
    engine for it, the ask `employer.regate`): the answer then carries `gate_changed: True`, and no `said`."""
    employer = await session.get(Employer, employer_id)
    if employer is None or employer.owner != owner:
        raise NoSuchEmployer("no such employer")
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise EmployerError(f"no field called {', '.join(unknown)}")
    if changes.get("name"):  # a name another employer already has would break the one-key-per-employer rule
        taken = await session.scalar(select(Employer).where(
            Employer.owner == owner, Employer.key == key_of(_coerce("name", changes["name"])),
            Employer.id != employer.id))
        if taken is not None:
            raise EmployerError(f"{taken.name} is already an employer; use its page, or give this one another name")
    stamp = datetime.now(UTC).isoformat()
    was_gated = bool(employer.gated)
    for field, value in changes.items():
        setattr(employer, field, _coerce(field, value))
        if field == "name":
            employer.key = key_of(employer.name) or employer.key
        employer.sources = {**(employer.sources or {}), field: {"source": "you", "at": stamp, "by": by}}
        if by == BY_OWNER and field not in (employer.owner_fields or []):
            employer.owner_fields = [*(employer.owner_fields or []), field]
    gate_flipped = bool(employer.gated) != was_gated
    report = None
    if gate_flipped and not defer_gate_change:
        report = await gate_changed(session, owner, employer, by)
    elif gate_flipped:  # the flag is saved now; the engine does the check (`gate_changed` sets this time again)
        employer.gated_at = datetime.now(UTC) if employer.gated else None
    await session.flush()
    out = view(employer)
    if report is not None:
        out["said"] = report.said  # the one line a gate or an ungate says of what it changed (the re-gate, 39.14)
    if gate_flipped and defer_gate_change:
        out["gate_changed"] = True  # the caller asks the engine to check, and takes this key out of its answer
    return out


# --- gating an employer: set on its page, read by screening ------------------------------------

#: The stages a newly gated employer's roles leave for Gated out: anything not yet applied to.
GATE_TAKES = stages.PRE_APPLY


async def gate_roles(session: AsyncSession, owner: str, employer: Employer) -> int:
    """Send the roles of a gated employer that are not yet applied to into Gated out, with the reason, kept as data like
    any stop (the employer's gate, the step `gate`, the versions of Hard pass and What to look for in force; docs/PRD.md
    39.12). Returns how many moved. The merge of two records uses it too: a hard pass on either holds on the one record
    left."""
    reason = f"employer gated: {employer.gated_reason or employer.name}"
    hard_pass = await profile_store.newest_version(session, owner, "gates")
    criteria = await profile_store.newest_version(session, owner, "criteria")
    rows = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.employer_id == employer.id,
        Opportunity.status.in_(GATE_TAKES)))).all()
    for role in rows:
        role.status, role.screened_out_reason, role.screened_out_by = "gated_out", reason, StopGate.EMPLOYER
        role.screened_out_step, role.screened_out_gate_id = StopStep.GATE, EMPLOYER_LINE
        role.screened_out_hard_pass, role.screened_out_criteria = hard_pass, criteria
    return len(rows)


async def gate_changed(session: AsyncSession, owner: str, employer: Employer, by: str):
    """Gating or ungating an employer checks every posting in the log and every role again (docs/PRD.md 39.14, the
    re-gate): the roles of a gated employer that are not yet applied to, and not moved by hand, go to Gated out with
    the reason; its postings are stopped; and ungating returns what only that gate stopped. Both go in the trail.
    Returns what the re-gate did (`regate.Report`). `edit` calls it; so does the engine's ask `employer.regate`
    (`work/engine_asks.py`) when the page saved the flag with `defer_gate_change` and left this to the engine."""
    from app.jobsearch.gating import regate  # re-gating reads the employers; imported here so the two never loop
    from app.trail import service as trail  # the trail reads the job-search tables

    employer.gated_at = datetime.now(UTC) if employer.gated else None
    report = await regate.run(
        session, owner, cause=f"{employer.name} was {'gated' if employer.gated else 'ungated'} on its page")
    if employer.gated:
        moved = report.roles_stopped
        sentence = f"Gated roles from {employer.name}" + (
            f": {moved} not yet applied to moved to Gated out." if moved else ".")
    else:
        back = report.roles_returned
        sentence = f"Stopped gating roles from {employer.name}" + (f": {back} back in play." if back else ".")
    await trail.record(session, owner, event="employer_gated" if employer.gated else "employer_ungated",
                       actor="person" if by == BY_OWNER else "agent",
                       who=await actor_name(session, owner) if by == BY_OWNER else by,
                       sentence=sentence, detail=employer.gated_reason or None, link=(EMPLOYER_LINK, employer.id))
    await session.flush()
    await db.release_if_free(session)  # the line is its own turn: settling the boards below is another (a long write)
    if employer.gated:  # a gated employer's board is no longer read: its postings are Not checked (docs/PRD.md 39.8)
        await not_checked.settle(session, owner)
    return report
