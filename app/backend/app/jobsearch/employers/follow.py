"""A role found on a job board, followed to the employer's own posting (docs/PRD.md path 32). The owner decided the
employer's link is preferred over the job board's, since its information is taken to be more accurate.

A role keeps two links. `Opportunity.board_url` is the job board link it was found at (a job board's, a site in
Where to look), kept for good. `Opportunity.url` is the link the role is read and opened from everywhere (the reader's
board, the analyst, the Job description tab, the export): the employer's own posting once the app has found it, else the
job board's, so with no employer link the two are the same address. A role found on the employer's own site has an
`url` and no `board_url`.

Code finds the employer's posting first. A job board's Apply button usually leads to the employer's own site, so the
Apply link the role's posting already carries is taken when it passes the check the analyst's link passes
(`apply_link`; it is never opened). Failing that, only from what the app already reads: the listings of the
employer's own job system (or its own site's saved skill) in the posting index (`postings/index.py`, `site_reads.py`).
Nothing here fetches a page: it never opens any job board, and it reads no new site. A listing matches a
role when it is the same
employer's, its title is the same after the usual abbreviations and tags are folded (`titles_match`), and its place
agrees (`places_agree`): one listing, and only one, that is confident. A listing that is unsure (the title is there but
the place differs, is not stated, or two listings fit) stores nothing. The analyst is the backup (`analysis.py`): when
code found no employer link it looks for the employer's own posting on public pages, and what it reports is checked
here (`check_employer_link`) before it is stored.

Services flush; the route, tool or process action commits.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import as_read, ats, off_turn, stages
from app.jobsearch.address import is_board_link, is_single_posting, posting_page
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import resolve
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.sites import avoid
from app.sites.models import SEARCH, Site
from app.sites.skills import public_url
from app.trail import service as trail

#: The stages a role is still looked at in: before the owner applies, when the link they will open still matters.
PRE_APPLY = stages.PRE_APPLY
#: What a posting from a job site (rather than the employer) holds as its system: its listings are never the
#: employer's own.
NOT_THE_EMPLOYERS = (ats.BROWSER, ats.JOB_SITE)

CONFIDENT, UNSURE, NONE = "confident", "unsure", "none"
#: How an employer link was taken, in the role's history: from the posting's own Apply link.
APPLY = "apply"


# --- which links are a job board's -----------------------------------------------------------------------------


async def job_board_hosts(session: AsyncSession, owner: str) -> frozenset[str]:
    """The hosts of the job sites the owner searches (a job board or an association's board in Where to look). The
    boards the app knows by name (`ats.BOARDS`) are always counted besides these. A host the owner also lists as an
    employer's own site is that employer's, never a board."""
    # One row, not one per site: the pairs come back as a JSON array (a board with many sites is read faster that way).
    raw = await session.scalar(select(func.json_group_array(func.json_array(Site.url, Site.kind))).where(
        Site.owner == owner, Site.list_name == SEARCH))
    rows = json.loads(raw or "[]")
    own = {h for h in (avoid.host_of(url) for url, kind in rows if kind == "employer") if h}
    return frozenset(h for h in (avoid.host_of(url) for url, kind in rows if kind != "employer") if h and h not in own)


def is_board_only(role: Opportunity, hosts: frozenset[str] | set[str] = frozenset()) -> bool:
    """Whether the role has only a job board link: found on a board, and no employer posting known yet."""
    if not (role.url or "").strip():
        return False
    if role.board_url:
        return role.url == role.board_url and is_board_link(role.url, hosts)
    return is_board_link(role.url, hosts)


def links_of(role: Opportunity, hosts: frozenset[str] | set[str] = frozenset()) -> dict:
    """The role's two links as its page shows them: the employer's own posting (none when the role has only a job board
    link) and the job board link it was found at (none when it was found on the employer's own site)."""
    url = (role.url or "").strip() or None
    board = role.board_url or (url if url and is_board_link(url, hosts) else None)
    if board and board == url and not is_board_link(url, hosts):
        board = None  # stored while its host counted as a job board: it is the employer's own address
    employer = url if url and url != board and not is_board_link(url, hosts) else None
    return {"employer_url": employer, "board_url": board}


# --- the same role: title ------------------------------------------------------------------------------------------

#: Words a title shortens, said in full so "Sr. Program Mgr" and "Senior Program Manager" are the same title. Only
#: abbreviations with one meaning in a job title.
_ABBREVIATIONS = {"sr": "senior", "jr": "junior", "mgr": "manager", "dir": "director", "asst": "assistant",
                  "assoc": "associate", "vp": "vice president", "svp": "senior vice president",
                  "evp": "executive vice president"}
_NUMERALS = {"ii": "2", "iii": "3", "iv": "4"}
#: Words that join or decorate a title and say nothing about the job.
_STOP = frozenset({"of", "the", "a", "an", "for", "and"})
#: What a tag after a title may say about where, and nothing else ("Program Manager - Remote", "Lead (US)").
_PLACE_WORDS = frozenset({"remote", "hybrid", "onsite", "on", "site", "in", "person", "us", "usa", "u", "s", "united",
                          "states", "america", "anywhere", "nationwide", "or", "and", "flexible", "wfh", "north"})
_TAG_SPLIT = re.compile(r"\s[-|]\s")
_BRACKETED = re.compile(r"[(\[]([^)\]]*)[)\]]")
#: The employment type some job systems add to the end of every title ("Analyst Full-time"). Only full time: a part-time
#: or contract listing is another job.
_FULL_TIME_TAIL = re.compile(r"(?:\s[-|]\s|\s)full[- ]time\s*$")


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).split()


@lru_cache(maxsize=8192)
def title_tokens(title: str | None, loose: bool = False) -> tuple[str, ...]:
    """A title as words folded for comparing: lowercase, punctuation gone, abbreviations in full, a roman numeral level
    as a number, "of", "the", "and" dropped. `loose` also drops a tag that only says where the job is (a bracket or a
    ` - Remote` after the title) and a closing "Full-time"; the strict form keeps every word of it, so a bracket that
    names a kind of job ("Contract") is never folded away."""
    text = (title or "").lower().replace("&", " and ").replace("–", " - ").replace("—", " - ")
    if loose:
        text = _BRACKETED.sub(lambda m: "" if set(_words(m[1])) <= _PLACE_WORDS else f" {m[1]} ", text)
        head, *tags = _TAG_SPLIT.split(text)
        text = " - ".join([head, *[t for t in tags if not set(_words(t)) <= _PLACE_WORDS]])
        text = _FULL_TIME_TAIL.sub("", text)
    words = [_ABBREVIATIONS.get(w, w) for w in _words(text)]
    words = " ".join(words).split()
    words = [_NUMERALS.get(w, w) for w in words if w not in _STOP]
    if len(words) > 1 and words[-1] == "i":
        words[-1] = "1"
    return tuple(words)


def titles_match(a: str | None, b: str | None) -> bool:
    """The same job title once folded (`title_tokens`): in full, or with the where-tags dropped from both."""
    strict_a, strict_b = title_tokens(a), title_tokens(b)
    if strict_a and strict_a == strict_b:
        return True
    loose_a = title_tokens(a, loose=True)
    return bool(loose_a) and loose_a == title_tokens(b, loose=True)


# --- the same role: place --------------------------------------------------------------------------------------------

_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co",
    "connecticut": "ct", "delaware": "de", "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
    "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh",
    "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va",
    "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
}
_REGION_CODES = frozenset(_STATES.values())
_REMOTE = re.compile(r"\b(remote|anywhere|work from home|wfh|telecommute)\b")
_MODE_WORDS = re.compile(r"\b(hybrid|on[- ]?site|in[- ]person)\b")
_COUNTRY = re.compile(r"\b(united states of america|united states|usa|u\.s\.a\.|u\.s\.|us)\b")
_PIECES = re.compile(r"[;|\n]|\s/\s|\sor\s")


@dataclass(frozen=True)
class Place:
    remote: bool
    city: str
    region: str


def _places(location: str | None) -> list[Place]:
    """A posting's place as the pieces it names: "Austin, TX; Remote" is a city and region, and a remote one."""
    out = []
    for piece in _PIECES.split((location or "").lower()):
        remote = bool(_REMOTE.search(piece))
        text = _MODE_WORDS.sub(" ", _REMOTE.sub(" ", piece))
        text = re.sub(r"\([^)]*\)", " ", _COUNTRY.sub(" ", text))
        parts = [p for p in (" ".join(re.sub(r"[^a-z0-9 ]+", " ", x).split()) for x in text.split(",")) if p]
        city, region = (parts + ["", ""])[:2] if len(parts) > 1 else (parts[0] if parts else "", "")
        region = _STATES.get(region, region)
        if city or region or remote:
            out.append(Place(remote, city, region if region in _REGION_CODES else ""))
    return out


def places_agree(loc_a: str | None, mode_a: str | None, loc_b: str | None, mode_b: str | None) -> bool | None:
    """Whether two postings of one job are in the same place: both remote, or the same city (and state when both give
    one). False when one is remote and the other is a place; None when either does not say."""
    pa, pb = _places(loc_a), _places(loc_b)
    remote_a = "remote" in (mode_a or "") or any(p.remote for p in pa)
    remote_b = "remote" in (mode_b or "") or any(p.remote for p in pb)
    if remote_a and remote_b:
        return True
    if remote_a != remote_b:
        elsewhere = pb if remote_a else pa
        stated = any(p.city or p.region for p in elsewhere) or (mode_b if remote_a else mode_a) in ("hybrid", "onsite")
        return False if stated else None
    cities_a, cities_b = [p for p in pa if p.city], [p for p in pb if p.city]
    if not cities_a or not cities_b:
        return None
    return any(a.city == b.city and (not a.region or not b.region or a.region == b.region)
               for a in cities_a for b in cities_b)


# --- finding it in what the app already reads ------------------------------------------------------------------------


def pick(title: str | None, location: str | None, work_mode: str | None,
         listings: list[JobPosting]) -> tuple[str, JobPosting | None]:
    """The listing of the employer's own that is this role, or why none is: `confident` (one listing, the same title, a
    place that agrees: stored), `unsure` (the title is there but the place differs or is not stated, or two listings
    fit: nothing is stored) or `none` (no listing has this title)."""
    same = [p for p in listings if titles_match(title, p.title)]
    if not same:
        return NONE, None
    agreeing = [p for p in same if places_agree(location, work_mode, p.location, p.work_mode) is True
                and (p.url or p.apply_url)]
    if agreeing and len({p.url or p.apply_url for p in agreeing}) == 1:
        return CONFIDENT, agreeing[0]
    return UNSURE, None


async def own_listings(session: AsyncSession, owner: str, employer: Employer,
                       hosts: frozenset[str] | set[str] = frozenset()) -> list[JobPosting]:
    """The employer's open listings the app already holds from its own job system or its own site (never a job site's
    listing of it), each with a link that is not a job board's."""
    rows = (await session.scalars(select(JobPosting).where(
        JobPosting.owner == owner, JobPosting.employer_id == employer.id, JobPosting.is_open,
        JobPosting.ats.not_in(NOT_THE_EMPLOYERS)))).all()
    return [p for p in rows if (p.url or p.apply_url) and not is_board_link(p.url or p.apply_url, hosts)]


def adopt(role: Opportunity, link: str, hosts: frozenset[str] | set[str] = frozenset()) -> bool:
    """Make `link` the role's own posting, the one it is read and opened from. The job board link it was found at is
    kept as `board_url` (never dropped). False when the role already has this link."""
    link = (link or "").strip()
    if not link or role.url == link:
        return False
    if not role.board_url and role.url and is_board_link(role.url, hosts):
        role.board_url = role.url
    role.url = link
    return True


async def record_found(session: AsyncSession, role: Opportunity, link: str, *, by: str = "resumebot",
                       via: str | None = None) -> None:
    """Say in the role's history that the employer's own posting was found (the job board link is kept beside it), and
    when it was taken from the posting's own Apply link (`via` "apply")."""
    sentence = ("Took the employer's own link from the posting's Apply link; the job board link is kept."
                if via == APPLY else "Found the employer's own posting of this role; the job board link is kept.")
    detail = f"{link}" + (f" (found on {role.board})" if role.board else "")
    if by == "analyst":
        await trail.record(session, role.owner, event="employer_posting_found", actor="agent", agent_slug="scout",
                           who="Analyst", sentence=sentence, link=("opportunity", role.id), detail=detail)
    else:
        await trail.record(session, role.owner, event="employer_posting_found", sentence=sentence,
                           link=("opportunity", role.id), detail=detail)


async def follow(session: AsyncSession, role: Opportunity, employer: Employer | None, link: str, *,
                 hosts: frozenset[str] | set[str] = frozenset(), by: str = "resumebot",
                 via: str | None = None) -> bool:
    """Take `link` as the role's employer posting, and learn the employer's careers site from the job system the
    posting is on, or, for an employer with no job system, from the own site the posting is on (kept only where the
    record has none and the owner set none: `employers.learn_careers_url`). A job system's apply form is taken as the
    posting's own page (`address.posting_page`, docs/PRD.md 32.6), as on every other path."""
    link = posting_page(link)
    if not adopt(role, link, hosts):
        return False
    if employer is not None:
        from app.jobsearch.postings import (
            sourced as sourcing,  # sourcing reads this module; imported here so the two never loop
        )

        employers.learn_careers_url(employer, employer.board_url, "its job system's board")
        sourcing.learn_careers_page(employer, link, hosts, role.company)
    await record_found(session, role, link, by=by, via=via)
    return True


async def apply_link(session: AsyncSession, role: Opportunity, employer: Employer, links: list[str | None],
                     hosts: frozenset[str] | set[str] = frozenset()) -> str | None:
    """The employer's own link among the Apply links the role's posting already carries (its own `apply_url`, and the
    `apply_url` of each posting in the index that became it), or None. A job board's apply link says nothing, and one
    is taken only when it passes the check the analyst's link passes (`check_employer_link`): a public address on the
    employer's own domain or on its job system, never another employer's board."""
    for link in dict.fromkeys(x.strip() for x in [role.apply_url, *links] if x and x.strip()):
        if check_employer_link(link, employer, hosts=hosts) is None:
            return link
    return None


#: The columns of a role the read takes (the rest of the row is read only for the roles that are followed), and the ones
#: its decision stands on: a role changed in any of them since the read is left for the next night.
_READ = (Opportunity.id, Opportunity.owner, Opportunity.status, Opportunity.url, Opportunity.board_url,
         Opportunity.apply_url, Opportunity.employer_id, Opportunity.company, Opportunity.title,
         Opportunity.location, Opportunity.move_type, Opportunity.created_at)
_STANDS = ("status", "url", "board_url", "employer_id", "apply_url")
#: How many roles are written between two lookups of their rows, and how many follows are made between two releases of
#: the writer's turn.
_WRITE_ROWS = 20


async def follow_roles(session: AsyncSession, owner: str, hosts: frozenset[str] | None = None) -> dict:
    """The code finder: every role of the owner's still before applying that has only a job board link is followed to
    its employer's own posting. First by the Apply link its posting already carries, when that is on the employer's own
    domain or its job system (`apply_link`: what a job board's Apply button usually leads to); then by looking for it
    among the employer's own listings in the posting index, where one listing that is confident is taken. Reads the
    database and nothing else. Returns how many roles were looked at and how many were followed.

    The read (a few columns of each role, the employers they name, their open listings) is a reader's where `session`
    is free (`off_turn.reading`) and takes no writer's turn; each role and employer that is followed is read again,
    whole, as it is written (`as_read.lock`), so a later role sees what an earlier follow learned of its employer. A
    role changed since the read is left for the next night."""
    async with off_turn.reading(session) as reader:
        if hosts is None:
            hosts = await job_board_hosts(reader, owner)
        rows = await off_turn.rows_in_slices(reader, select(*_READ).where(
            Opportunity.owner == owner, Opportunity.status.in_(PRE_APPLY)).order_by(Opportunity.created_at))
        waiting = [r for r in rows if is_board_only(r, hosts)]  # type: ignore[arg-type]  # a row has the columns read
        # Each waiting role's employer: the one it names, else the one its company's name means.
        named = {r.employer_id for r in waiting if r.employer_id}
        present: set = set()
        asked = sorted(named)
        for at in range(0, len(asked), as_read.STATEMENT_ROWS):
            ids = asked[at:at + as_read.STATEMENT_ROWS]
            present.update((await reader.scalars(select(Employer.id).where(Employer.id.in_(ids)))).all())
        by_name = await employers.find_many(reader, owner, [r.company for r in waiting if not r.employer_id])
        by_employer: dict = {}
        for role in waiting:
            if role.employer_id:
                employer_id = role.employer_id if role.employer_id in present else None
            else:
                found = by_name.get(role.company)
                employer_id = found.id if found is not None else None
            if employer_id is not None:
                by_employer.setdefault(employer_id, []).append(role)
        carried: dict = {}  # the apply links of the postings that became each role
        for at in range(0, len(waiting), as_read.STATEMENT_ROWS):
            for role_id, link in (await reader.execute(select(JobPosting.opportunity_id, JobPosting.apply_url).where(
                    JobPosting.owner == owner,
                    JobPosting.opportunity_id.in_([r.id for r in waiting[at:at + as_read.STATEMENT_ROWS]]),
                    JobPosting.apply_url.is_not(None)))).all():
                carried.setdefault(role_id, []).append(link)
        listings = await _own_listings_of(reader, owner, list(by_employer), hosts)
    work = [(employer_id, role) for employer_id, roles in by_employer.items() for role in roles]
    followed = pending = 0
    for at in range(0, len(work), _WRITE_ROWS):
        part = work[at:at + _WRITE_ROWS]
        held_employers = await as_read.lock(session, Employer, {employer_id for employer_id, _ in part})
        held = await as_read.lock(session, Opportunity, [role.id for _, role in part])
        for employer_id, read in part:
            role, employer = held.rows.get(read.id), held_employers.rows.get(employer_id)
            if role is None or employer is None or any(getattr(role, k) != getattr(read, k) for k in _STANDS):
                continue  # gone, or changed since the read: left for the next night
            link = await apply_link(session, role, employer, carried.get(role.id, []), hosts)
            if link and await follow(session, role, employer, link, hosts=hosts, via=APPLY):
                followed += 1
                pending += 1
                continue
            verdict, posting = pick(role.title, role.location, role.move_type,
                                    listings[employer_id])  # type: ignore[arg-type]  # rows with the columns read
            if verdict == CONFIDENT and await follow(
                    session, role, employer, posting.url or posting.apply_url, hosts=hosts):
                followed += 1
                pending += 1
        await session.flush()
        if pending >= _WRITE_ROWS:
            pending = 0
            await db.release_if_free(session)
    await session.flush()
    return {"looked": len(waiting), "followed": followed}


async def _own_listings_of(session: AsyncSession, owner: str, employer_ids: list,
                           hosts: frozenset[str] | set[str]) -> dict:
    """`own_listings` for each of these employers (by id): the open listings from their own job system or site, each
    with a link that is not a job board's, as rows of the columns the matcher reads (`pick`), in a few statements."""
    out: dict = {employer_id: [] for employer_id in employer_ids}
    for at in range(0, len(employer_ids), as_read.STATEMENT_ROWS):
        for p in await off_turn.rows_in_slices(session, select(
                JobPosting.employer_id, JobPosting.url, JobPosting.apply_url, JobPosting.title, JobPosting.location,
                JobPosting.work_mode).where(
                JobPosting.owner == owner, JobPosting.employer_id.in_(employer_ids[at:at + as_read.STATEMENT_ROWS]),
                JobPosting.is_open, JobPosting.ats.not_in(NOT_THE_EMPLOYERS))):
            if (p.url or p.apply_url) and not is_board_link(p.url or p.apply_url, hosts):
                out[p.employer_id].append(p)
    return out


# --- what the analyst reports ----------------------------------------------------------------------------------

#: Second-level labels under a country's domain: `example.co.uk` is held by whoever holds `example`.
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "edu", "ac"})


def _base_domain(host: str) -> str:
    """The part of a host an owner of a domain holds: `careers.example.test` and `www.example.test` are `example.test`,
    and `jobs.example.co.uk` is `example.co.uk`."""
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def base_domain(host: str) -> str:
    """The registrable domain of a host (`_base_domain`): what the browser's runs are split by (`browser/reader.py`)."""
    return _base_domain(host)


def _lower(board: dict | None) -> dict:
    return {k: str(v).lower() for k, v in resolve.address(board).items()}


def _on_a_board(link: str) -> bool:
    """Whether a link names an employer's board on a job system (`ats.parse`)."""
    found = ats.parse(link)
    return bool(found and found.board)


def on_another_employers_board(found: ats.Board, employer: Employer | None) -> bool:
    """Whether a board a link names (`ats.parse`) is a board of the employer's own job system that is not the
    employer's (a board two employers hold is read twice, so the check can only tell boards apart when both addresses
    are whole)."""
    return bool(employer is not None and employer.ats == found.system and ats.complete(employer.ats, employer.board)
                and ats.complete(*found) and _lower(employer.board) != _lower(found.parts))


def check_employer_link(url: str | None, employer: Employer | None, *, field: str = "employer_posting_url",
                        own_domain: bool = True, website: str | None = None, careers_url: str | None = None,
                        hosts: frozenset[str] | set[str] = frozenset(), one_posting: bool = True) -> str | None:
    """Why a link the analyst reported for the employer cannot be kept, in plain words, or None when it can. It must be
    a public web address (no sign-in in it) and never a job board. With `own_domain` (the employer's posting of a
    role) it must also be on the employer's own domain (its website's or its careers page's, as the record holds them
    or as the analyst reported them with it) or on a job system's own host, not on another employer's board of that
    system, and (`one_posting`) it must be the address of ONE posting (`address.is_single_posting`), never a careers
    home page, a job system's board, a search or a listing page or a sign-in page; a careers page is checked with
    `one_posting` off."""
    text = (url or "").strip()
    if not text:
        return None
    if not public_url(text):
        return f"{field} is not a public web address (it starts with https:// and has no sign-in in it)"
    host = avoid.host_of(text) or ""
    if is_board_link(text, hosts):
        return (f"{field} is on {host}, a job board: send the employer's own page, on its own site or its job "
                "system, or leave it out")
    if not own_domain:
        return None
    if one_posting and not is_single_posting(text):
        return (f"{field} is not one posting's address (a careers home page, a job system's board, a search or a "
                "listing page and a sign-in page are never kept): send the page of this one job")
    named = ats.parse(text)
    if named and named.board:
        if employer is not None and on_another_employers_board(named.board, employer):
            return f"{field} is on another employer's {ats.label(named.board.system)} board, not {employer.name}'s"
        return None
    # The employer's own domains: its website's and its careers page's, but never a job system's host (a careers page
    # kept from its board is on the system's, which every employer of that system shares).
    known = {d for d in (avoid.host_of(u) for u in (website, careers_url, employer.website if employer else None,
                                                    employer.careers_url if employer else None)
                         if u and not _on_a_board(u)) if d}
    if any(_base_domain(host) == _base_domain(k) for k in known):
        return None
    if not known:
        return f"{field} can't be checked: the employer's website is not known, so send it in employer.website too"
    return f"{field} is on {host}, which is not the employer's own domain ({', '.join(sorted(known))}) or a job system"
