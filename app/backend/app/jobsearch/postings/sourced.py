"""A role is Sourced when the app knows the company and holds a link to the original posting (the owner, 2026-10-15,
docs/PRD.md "Sourced").

"Found" means a job was found somewhere, often on the board. It is Sourced when we hold one link to the posting where it
was first made:

- **`employer`**: on the employer's own careers site or its own job system (a hosted job system's board, and so on);
- **`recruiter`**: for a recruiter's posting, where the board's Apply button lands, when it is the same posting;
- **`board`**: for an Easy Apply with no link to follow, the board's own posting;
- **`board_copy`**: for a role found on the board whose employer's own posting could not be read (the site refused, the
  posting is gone, the lookup found none) and that the board's copy of the posting stands in for (the owner, 2026-11-05,
  docs/PRD.md 48.14 f, the board's copy hook). It is a stand-in: the role is Sourced to the employer's posting, and its
  description is the employer's, once that comes (`posting_text._resource`).

The job description is pulled only from that source, word for word (`posting_text.take`), and only then is the role
researched. A role that is not Sourced stays where it is, is never researched, and is tried again every night: in code
(`sweep`, run by the step that follows job-board roles to their employers' postings) and by the browser reader, which
opens a board's posting, follows its Apply button or searches the employer's careers page, and hands what it found to
`accept_source`. The analyst never sources and never writes a posting's text.

Every accepted link is ONE actual posting (`is_single_posting`), never a careers home page, a board's front page, a
search or a listing page or a sign-in page, and never another job's: a link found for a role is refused when its page,
its capture or its job system's own list states the title of another job (`_another_job`). A link
the owner gave is kept as it is. Nothing here fills a form, solves a CAPTCHA or gets round a refusal: reads are polite
(`polite.py`), and a refusal is a stop.

Services flush; the route, tool, process step or command commits.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app import db
from app.jobsearch import adapters, as_read, ats, off_turn, stages
from app.jobsearch.actors import CODE_CHECKER, CODE_CHECKER_BY, actor_name
from app.jobsearch.address import is_board_link, is_marker, is_single_posting, posting_page
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Refused
from app.jobsearch.models import SOURCE_KIND_BOARD_COPY, Employer, JobPosting, Opportunity, is_board_copy, is_unnamed
from app.jobsearch.postings import chase, role_postings, this_posting, unbuilt, whole
from app.jobsearch.postings import text as posting_text
from app.jobsearch.work import engine_calls
from app.jobsearch.work.claim import claim_one
from app.sites.skills import public_url
from app.trail import service as trail

log = logging.getLogger(__name__)

EMPLOYER, RECRUITER, BOARD = "employer", "recruiter", "board"
#: Only the app's own fallback makes a role Sourced to the board's posting (`source_to_board_copy`): no session, tool or
#: hand-back names it, so it is not one of `KINDS`.
BOARD_COPY = SOURCE_KIND_BOARD_COPY
KINDS = (EMPLOYER, RECRUITER, BOARD)
#: What `accept_source` and `source_in_code` answer.
SOURCED, NO_TEXT, NOT_SOURCED = "sourced", "no_text", "not_sourced"
#: The most seconds the code reads one board for one role (a board is read to its last page at the polite pace).
BOARD_READ_SECONDS = 60.0
#: The stages a role is still in play in, whose sourcing is tried again every night.
IN_PLAY = stages.PRE_APPLY


# --- the careers page of the site an employer's posting is on ---------------------------------------------------------

#: Path words of the jobs section of an employer's own site: a posting's address is under one, and the section is the
#: address of the careers page. The other markers (`job`, `position`, `apply`) begin one posting's own address, which
#: says nothing of where the section is.
_SECTIONS = frozenset({"career", "careers", "jobs", "positions", "openings", "opportunities", "vacancies",
                       "requisitions", "postings", "roles", "employment"})
#: Words in a host's own labels that say it is a careers site: its root is the careers page.
_CAREERS_HOST = frozenset({"career", "careers", "job", "jobs", "talent", "recruiting", "employment", "hiring", "apply"})
#: The fewest letters a word of an employer's name has to have to be looked for in a domain ("crh", "afl").
_NAME_WORD = 3
#: What an employer's `sources` say of a careers page taken from the site its own posting is on.
POSTING_SITE = "its own posting's site"


def careers_root(url: str | None) -> str | None:
    """The careers page of the site one posting is on, or None when the address does not tell it: the root of a host
    that is a careers site by its own name (`careers.`, `jobs.`: its root is the page, whatever path a posting is
    under), else the site's jobs section when the posting's address is under one (`/careers/...`, `/en/jobs/...`).
    Never the posting itself, and never a guess at the page of a company's front site: a posting under `/job/` on a
    `www.` host does not say which of the site's pages is its careers page. Pure: it reads the address and nothing
    else."""
    if not is_single_posting(url):
        return None
    text = (url or "").strip()
    try:
        parsed = urlparse(text if "://" in text else "https://" + text)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").lower()
    origin = f"{parsed.scheme}://{host}" + (f":{port}" if port else "")
    own = host.removesuffix(employer_posting.base_domain(host))  # the labels the employer chose, not its domain
    if _CAREERS_HOST.intersection(own.split(".")):
        return f"{origin}/"  # a section's path on such a host is a guess: such a guess can be a 404
    parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(parts):
        if is_marker(part.lower()):
            return f"{origin}/{'/'.join(parts[:i + 1])}" if part.lower() in _SECTIONS else None
    return None


def _on_the_employers_site(employer: Employer, link: str) -> bool:
    """Whether the address is on the employer's own site, as far as the record tells: its registered domain is its
    website's, or a word of the employer's name (or of a name it goes by) that tells it from another employer, or the
    initials of the name, is in the domain's own label ("crh" in crh.com, "aflglobal" for "AFL Global"). A hiring
    platform's host that no list names (a tenant of a vendor not in `pairs._shared_hosts`) has neither. False for an
    address that is no employer's own (`employer_pairs.domain_of`)."""
    domain = employer_pairs.domain_of(link)
    if domain is None:
        return False
    if domain == employer_pairs.domain_of(employer.website):
        return True
    label = re.sub(r"[^a-z0-9]", "", domain.partition(".")[0])
    for name in (employer.name, *(employer.aliases or [])):
        words = employer_pairs.name_parts(name)
        if any(len(w) >= _NAME_WORD and employer_pairs.NO_RARITY.distinctive(w) and w in label
               for w in words):
            return True
        if len(words) >= 2 and label == "".join(w[0] for w in words):
            return True
    return False


def learn_careers_page(employer: Employer | None, link: str, hosts: frozenset[str] | set[str] = frozenset(),
                       company: str | None = None) -> bool:
    """An employer's own posting was found at `link`: keep the careers page of the site it is on, where the employer
    has no board and no careers page (`employers.learn_careers_url`: one the owner set or cleared is theirs, and one
    already kept stays), so that the careers-page path takes over (its site, off until switched on; mapping when it is).
    Only a site that is the employer's own (`_on_the_employers_site`): never a job board (one of the owner's job sites
    in `hosts` too), a job system's host, a platform's host that many employers share, nor the site of the staffing firm
    a role is hired through: an employer named "via" one, or a role whose `company` text carries the tag
    (see `employer_pairs._facts`). An employer on a job system is given its careers page from its
    board, not from here. Returns whether it was kept."""
    if (employer is None or employer.ats or employer.board_url or employer_pairs.has_via(employer.name)
            or employer_pairs.via_names(company) or is_board_link(link, hosts)
            or not _on_the_employers_site(employer, link)):
        return False
    root = careers_root(link)
    return root is not None and employers.learn_careers_url(employer, root, POSTING_SITE)


# --- keeping a source -------------------------------------------------------------------------------------------------


async def _who(session: AsyncSession, role: Opportunity, by: str) -> dict:
    """How a line of the role's history names whoever sourced it: the app's code, the owner, or an agent."""
    if by in ("code", "app", "resumebot", ""):
        return {"actor": "app"}
    if by == CODE_CHECKER_BY:
        return {"actor": "agent", "agent_slug": "scout", "who": CODE_CHECKER}
    if by == "owner":
        return {"actor": "person", "who": await actor_name(session, role.owner)}
    return {"actor": "agent", "agent_slug": "scout", "who": by[:80]}


async def _record(session: AsyncSession, role: Opportunity, kind: str, link: str, by: str, how: str | None = None,
                  ) -> None:
    where = {EMPLOYER: "the employer's own posting", RECRUITER: "the recruiter's posting where Apply lands",
             BOARD: "the job board's own posting (Easy Apply, no link to follow)",
             BOARD_COPY: "the board's own posting, in place of the employer's, which could not be read"}[kind]
    await trail.record(
        session, role.owner, event="sourced", link=("opportunity", role.id),
        sentence=f"Sourced {role.title} at {role.company}: {where}.",
        detail=f"{link}" + (f" ({how})" if how else ""), **await _who(session, role, by))


async def _keep_source(session: AsyncSession, role: Opportunity, link: str, kind: str, by: str,
                       hosts: frozenset[str], how: str | None = None) -> None:
    """Make `link` the role's source: the link, its kind and the time. The role is read and opened from the employer's
    or recruiter's posting from now on (`employer_posting.adopt`, which keeps the job board link beside it). A role that
    had another source loses the text it read from it: a description is the text of the role's source."""
    if role.source_url and role.source_url != link:
        role.description, role.description_at, role.description_from = None, None, None
    role.source_url, role.source_kind, role.sourced_at = link, kind, datetime.now(UTC)
    if kind not in (BOARD, BOARD_COPY):
        employer_posting.adopt(role, link, hosts)
    if kind == EMPLOYER and role.employer_id:  # the employer's own site: its careers page, where none is known
        learn_careers_page(await session.get(Employer, role.employer_id), link, hosts, role.company)
    await session.flush()
    await _record(session, role, kind, link, by, how)


#: `_keep_source` for the callers outside this module: the app's fallback to the board's copy
#: (`source_to_board_copy`) and the replacing of that copy (`posting_text._resource`).
keep_source = _keep_source


async def source_to_board_copy(session: AsyncSession, role: Opportunity, link: str, why: str) -> None:
    """Source a role that has no source to the board's own posting of it (`link`, its page), the stand-in kept while the
    employer's posting cannot be read (`why` says in its history line what could not be). Only a role not Sourced is
    given it: one Sourced to the employer keeps that source and only its description is the board's copy
    (`Opportunity.description_from`). The job board link stays the role's own (`employer_posting.adopt` is not run).
    Services flush."""
    if role.sourced_at is not None:
        return
    hosts = await employer_posting.job_board_hosts(session, role.owner)
    await _keep_source(session, role, link, BOARD_COPY, "code", hosts, why)


def _refusal(link: str, employer: Employer | None, kind: str, hosts: frozenset[str], *,
             trust_system: bool = False) -> str | None:
    """Why a link cannot be the source of an employer's or a recruiter's posting, or None when it can. With
    `trust_system` (the browser followed the role's own Apply link there): an address on a
    job system the app knows (`ats.parse` names a board) is always taken, whatever its path or which of the system's
    boards it is on."""
    found = ats.parse(link)
    if trust_system and found and found.board and public_url(link) and not is_board_link(link, hosts):
        return None
    if not is_single_posting(link):
        return ("it is not one posting's address: a careers home page, a board's front page, a search or listing page "
                "and a sign-in page are never a source")
    if is_board_link(link, hosts):
        return "it is on a job board: send the employer's own posting, or the posting where Apply lands"
    if kind == EMPLOYER and found and found.board:
        if employer is not None and employer_posting.on_another_employers_board(found.board, employer):
            return f"it is on another employer's {ats.label(found.board.system)} board, not {employer.name}'s"
    return None


def _captured_title(page: bytes | None) -> str | None:
    """The title of the JobPosting a captured page embeds, or None: a capture that embeds none states no title."""
    return adapters.page_facts(page.decode("utf-8", "replace")).get("title") if page else None


async def _listed_title(session: AsyncSession, role: Opportunity, link: str) -> str | None:
    """The title a job system's or an employer's site's own list gave `link`, when it is not the role's job, else None.
    The app's index holds each posting it read with the title its source gave. A posting at this address that is the
    role's own, or none that a source gave (a tracker's row, a browser's), settles nothing; one posting that says the
    role's job is enough."""
    mine = link.strip().rstrip("/").lower()

    def at(column):
        return func.lower(func.rtrim(func.trim(column), "/")) == mine

    rows = (await session.scalars(select(JobPosting).where(
        JobPosting.owner == role.owner, or_(at(JobPosting.url), at(JobPosting.apply_url))))).all()
    if any(p.opportunity_id == role.id for p in rows):
        return None
    titles = [p.title.strip() for p in rows if ats.from_source(p.ats) and (p.title or "").strip()]
    if not titles or any(this_posting.same_job(role.title, t) for t in titles):
        return None
    return titles[0]


async def _another_job(session: AsyncSession, role: Opportunity, link: str, page: bytes | None, fetch: bool,
                       ) -> tuple[str | None, dict]:
    """Whether `link`, found for `role`, is another job's posting: `(why, reads)`, with `why` None when nothing says it
    is. Three things say it, each a title that is stated (`this_posting.same_job`): the job system's own list for that
    address (no network), the JobPosting the captured `page` embeds, and the JobPosting of the page the app reads at
    that address, as the take of the role's text would (`posting_text.ahead`; with `fetch`). `reads` is what that
    read brought, for the take that follows to use as it is: a page is read once. A role added by its link has the link
    for its title, which no page names: it is not asked."""
    if is_unnamed(role):
        return None, {}
    if (listed := await _listed_title(session, role, link)) is not None:
        return f"its job system lists that address as another job, {listed!r}, not {role.title!r}", {}
    if (shown := _captured_title(page)) and not this_posting.same_job(role.title, shown):
        return f"the page captured there is another job's posting, {shown!r}, not {role.title!r}", {}
    if not fetch:
        return None, {}
    reads = await posting_text.ahead(session, role, link)
    if (other := posting_text.another_job(reads)) is not None:
        return f"the page there is another job's posting, {other!r}, not {role.title!r}", {}
    return None, reads


async def accept_source(session: AsyncSession, role: Opportunity, url: str | None, kind: str, text: str | None,
                        marked_whole: bool, by: str, *, fetch: bool = True, trust_system: bool = False,
                        page: bytes | None = None) -> str:
    """Take `url` as the role's source, of this `kind`, and put the posting's text on the role. Returns `"sourced"`,
    `"rejected:<reason>"` (nothing stored) or `"no_text"` (the source is stored, but no text of it could be had: the
    role waits for the posting's text, `analysis.action_wait_for_text`).

    `employer` and `recruiter`: `url` must be one posting (`is_single_posting`), not a job board's, and (for an
    employer's) not on another employer's board of its job system. Its text is fetched here, in code, word for word,
    from that address (`posting_text.take`); `text`, when `marked_whole` says it is a capture of that same page as a
    browser shows it, is the role's text (`posting_text.Capture`, with its posted day) unless the role already holds a
    usable one: a snippet gives way to it, a usable description stays. `board` (an Easy Apply): `url` is the board's
    own posting and `text` must be that page's whole text (`marked_whole`), which replaces any description. A text that
    is not the whole posting, or is too long to hold (`whole.refusal`), is never kept, and no text is ever cut. A line
    in the role's history says who sourced it and how. `by` is who: `code`, `owner`, or an agent's name. `fetch=False`
    reads nothing from the network: only `text` is kept. `page` is the page the text was captured from, as it came:
    kept as the raw copy of each posting of the role. `trust_system`: a job system's address is always taken
    (`_refusal`), unless it is another job's: a link found for the role (`by` is not `owner`) is refused with
    `"rejected:<why>"` when the title its job system's list, its captured `page` or the page the app reads there states
    is not the role's (`_another_job`)."""
    if kind not in KINDS:
        return f"rejected:the kind is one of {', '.join(KINDS)}"
    link = posting_page(url)  # an apply form with a fixed rule to its posting page is stored as that page
    hosts = await employer_posting.job_board_hosts(session, role.owner)
    employer = await session.get(Employer, role.employer_id) if role.employer_id else None
    if kind == BOARD:
        if not link or not is_single_posting(link) or not is_board_link(link, hosts):
            return "rejected:a board source is the job board's own posting of this one job, not a front page or search"
        kept = (text or "").strip()
        problem = None if marked_whole else "the text was not marked as the whole posting"
        problem = problem or whole.refusal(kept, "the posting's text")
        if problem:
            return f"rejected:{problem}"
        await _keep_source(session, role, link, kind, by, hosts)
        await posting_text.take(session, role, posting_text.Capture(kept, page, replaces=True))
        return SOURCED
    problem = _refusal(link, employer, kind, hosts, trust_system=trust_system)
    if problem:
        return f"rejected:{problem}"
    reads: dict = {}
    if by != "owner":  # a link the owner gave is theirs, kept as it is (`source_given`)
        problem, reads = await _another_job(session, role, link, page, fetch)
        if problem:
            return f"rejected:{problem}"
    with polite.read_ahead(reads):
        await _keep_source(session, role, link, kind, by, hosts)
        if fetch and (not (role.description or "").strip() or is_board_copy(role)):  # the board's copy gives way
            await posting_text.take(session, role)
    kept = (text or "").strip()
    if marked_whole and kept:  # `take` refuses a text that is not the whole posting; a held description stays
        await posting_text.take(session, role, posting_text.Capture(kept, page))  # (its page is kept beside it)
    return SOURCED if (role.description or "").strip() and not is_board_copy(role) else NO_TEXT


async def source_given(session: AsyncSession, role: Opportunity, link: str, by: str = "owner", *,
                       trust_system: bool = False) -> bool:
    """Take a link someone gave for the role (the owner pasted it) as the role's source, when it is one posting that is
    not a job board's. The text is not fetched here: the process fetches it from the source (`posting_text.take`).
    With `trust_system`, an address on a job system the app knows is taken whatever its shape (`_refusal`); a job
    board's is never. Returns whether it was taken."""
    link = posting_page(link)
    hosts = await employer_posting.job_board_hosts(session, role.owner)
    if _refusal(link, None, EMPLOYER, hosts, trust_system=trust_system) is not None:
        return False
    await _keep_source(session, role, link, EMPLOYER, by, hosts, "the link that was given for it")
    return True


# --- sourcing in code -------------------------------------------------------------------------------------------------


def _board_key(found: ats.Board) -> tuple:
    """A board as the boards cache keeps it (`_board_listings`)."""
    return found.system, tuple(sorted((k, str(v)) for k, v in found.parts.items()))


async def _board_listings(fetch, found: ats.Board, boards: dict) -> list:
    """The open postings of a board, read through its adapter (cached for the sweep that asks for it again), as the
    plain listings `employer_posting.pick` compares. Nothing is stored."""
    key = _board_key(found)
    if key not in boards:
        adapter = adapters.ADAPTERS.get(found.system)
        try:
            async with asyncio.timeout(BOARD_READ_SECONDS):
                listing = await adapter.read(fetch, found.parts) if adapter else None
        except (Refused, adapters.AdapterError, httpx.HTTPError, ValueError, TimeoutError):
            listing = None
        except Exception:  # one board that fails unforeseen must not stop the roles behind it
            log.warning("reading %s's board to source a role failed unexpectedly", found.system, exc_info=True)
            listing = None
        boards[key] = None if listing is None else [
            SimpleNamespace(title=p.get("title"), location=p.get("location"), work_mode=p.get("work_mode"),
                            url=p.get("url"), apply_url=p.get("apply_url")) for p in listing.postings]
    return boards[key] or []


def stored_source(role: Opportunity, rows: list, employer: Employer | None,
                  hosts: frozenset[str]) -> tuple[str, str] | None:
    """The source the app already holds for a role, with no network and nothing written: `(link, how)`, or None. A
    posting of the role that came from the employer's own job system or site with an address that is one posting
    (`rows` are the role's postings); else a stored link of the role (its employer link or its Apply link) that is one
    posting on the employer's job system or its own domain."""
    for posting in rows:
        if not ats.from_source(posting.ats):
            continue
        for link in dict.fromkeys(posting_page(x) for x in (posting.url, posting.apply_url) if x and x.strip()):
            if not is_board_link(link, hosts) and is_single_posting(link):
                return link, "its own job system's posting"
    for link in dict.fromkeys(posting_page(x) for x in _links(role, hosts)):
        if is_single_posting(link) and employer_posting.check_employer_link(link, employer, hosts=hosts) is None:
            return link, "a stored link of the role"
    return None


def _links(role: Opportunity, hosts: frozenset[str]) -> list[str]:
    """The role's stored links that are not a job board's: its employer link, and its Apply link."""
    own = employer_posting.links_of(role, hosts)["employer_url"]
    return [x for x in dict.fromkeys(x.strip() for x in (own, role.apply_url) if x and x.strip())
            if not is_board_link(x, hosts)]


def board_roots(role: Opportunity, hosts: frozenset[str]) -> list[tuple[str, ats.Board]]:
    """The role's stored links that are on a known job system's board root and not one posting, with the board each
    names: where the role's own posting is looked for by its title and place (`source_in_code`)."""
    return [(link, found.board) for link in _links(role, hosts)
            if (found := ats.parse(link)) and found.board and found.board.system in adapters.ADAPTERS
            and ats.complete(*found.board) and not is_single_posting(link)]


async def source_in_code(session: AsyncSession, role: Opportunity, *, boards: dict | None = None) -> str:
    """Source a role in code, with no browser and nothing the app does not already read, or leave it as it is. Returns
    `"sourced"` or `"not_sourced"`. A role is sourced when:

    1. a posting of the role came from the employer's own job system or its own site (the posting index) and has an
       address that is one posting;
    2. its stored links, the employer's own link or its Apply link, are one posting on the employer's own job system or
       its own domain (`employer_posting.check_employer_link`);
    3. a stored link is on a known job system's board root (not one posting): the role's title (and place) is matched
       on that board with `employer_posting.pick`, the board read through its adapter for this role only, and exactly
       one listing is the same job.

    The text is not fetched here: the process fetches it from the source next (`posting_text.take`). `boards` is the
    boards a caller already read; without it, the boards read ahead of the process step (`read_boards_ahead`,
    `polite.read_ahead`)."""
    if await source_stored(session, role) == SOURCED:
        return SOURCED
    hosts = await employer_posting.job_board_hosts(session, role.owner)
    roots = board_roots(role, hosts)
    if not roots:
        return NOT_SOURCED
    ready = polite.ahead(BOARDS_AHEAD)
    cache = boards if boards is not None else (ready if isinstance(ready, dict) else {})
    if not all(_board_key(found) in cache for _link, found in roots):
        await db.release_if_free(session)  # what was done so far is saved; the boards are read with none open
    fetch = polite.make()
    try:
        target = await _board_match(fetch, role, roots, cache)
    finally:
        await fetch.close()
    if target is None:
        return NOT_SOURCED
    await _keep_source(session, role, target, EMPLOYER, "code", hosts, ON_ITS_BOARD)
    return SOURCED


#: How a role sourced on its employer's board says it was.
ON_ITS_BOARD = "the one posting on the employer's board with this title and place"
#: The key the boards read ahead of a process step are kept under (`polite.read_ahead`).
BOARDS_AHEAD = ("sourcing", "boards")


async def read_boards_ahead(session: AsyncSession, roles: list[Opportunity]) -> dict:
    """The boards `source_in_code` would read for `roles` (those the app holds no source for, with a stored link on a
    known job system's board root), read now with no transaction open: the database part in `session`, which is then
    released (`db.release`: what it did so far is committed). The boards cache `source_in_code` takes; empty, with the
    session left as it is, when no board is to be read, or when the session cannot be released here (inside a process
    step: the step's own caller reads ahead)."""
    if not roles or not db.can_release(session):
        return {}
    hosts = await employer_posting.job_board_hosts(session, roles[0].owner)
    wanted: list[ats.Board] = []
    for role in roles:
        if role.sourced_at is not None or await _stored(session, role, hosts) is not None:
            continue
        wanted += [found for _link, found in board_roots(role, hosts)]
    if not wanted:
        return {}
    await db.release(session)
    cache: dict = {}
    fetch = polite.make()
    try:
        for found in wanted:
            await _board_listings(fetch, found, cache)
    finally:
        await fetch.close()
    return cache


async def _stored(session: AsyncSession, role: Opportunity, hosts: frozenset[str]) -> tuple[str, str] | None:
    """`stored_source` for a role, its postings and employer read here: what `source_stored` would keep, unwritten."""
    employer = (await session.get(Employer, role.employer_id) if role.employer_id
                else await employers.find(session, role.owner, role.company))
    return stored_source(role, await role_postings.of_role(session, role), employer, hosts)


async def predicted_source(session: AsyncSession, role: Opportunity, boards: dict) -> str | None:
    """The source `source_in_code` would give a role that is not Sourced, worked out from what is stored and from
    `boards` (read ahead), with nothing written and nothing read from the network; None when it would give none."""
    if role.sourced_at is not None:
        return None
    hosts = await employer_posting.job_board_hosts(session, role.owner)
    held = await _stored(session, role, hosts)
    if held is not None:
        return held[0]
    roots = board_roots(role, hosts)
    if not roots or not all(_board_key(found) in boards for _link, found in roots):
        return None
    return await _board_match(None, role, roots, boards)


async def source_stored(session: AsyncSession, role: Opportunity) -> str:
    """Steps 1 and 2 of `source_in_code`: the source the app already holds (`stored_source`), kept, with no network.
    Returns `"sourced"` or `"not_sourced"`."""
    if role.sourced_at is not None:
        return SOURCED
    hosts = await employer_posting.job_board_hosts(session, role.owner)
    held = await _stored(session, role, hosts)
    if held is None:
        return NOT_SOURCED
    await _keep_source(session, role, held[0], EMPLOYER, "code", hosts, held[1])
    return SOURCED


async def _board_match(fetch, role: Opportunity, roots: list[tuple[str, ats.Board]], cache: dict) -> str | None:
    """Step 3 of `source_in_code`, the network part, with nothing written: the one posting on one of the role's boards
    with its title and place, or None."""
    for _link, found in roots:
        listings = await _board_listings(fetch, found, cache)
        verdict, matched = employer_posting.pick(role.title, role.location, role.move_type, listings)
        target = (matched.url or matched.apply_url) if matched is not None else None
        if verdict == employer_posting.CONFIDENT and target and is_single_posting(target):
            return target.strip()
    return None


#: How many roles `source_held` writes in one turn of the writer before it lets the turn go.
SLICE = 20


async def source_held(session: AsyncSession, owner: str) -> int:
    """Source, from what each already holds and with nothing read, the roles the browser's queue would source
    (`chase.wanted`: on deck, read first, or in Followup needed and still fresh enough to be worth the chase; and the
    roles the owner applied to or is in talks about that hold no text, `unbuilt.roles`) that are not Sourced and whose
    stored link, the employer's own or its Apply link, is already one posting on the employer's job system or domain
    a session is never queued to "find" a link the role already carries. Returns how many it sourced."""
    # Read in a reader's session, with no turn, then take the
    # turn for the few roles to source and write them as they are then (`as_read.lock`). The caller's transaction is
    # ended first, as the queue's refresh does right after this (`db.release_if_free`): a reader is a reader's only
    # where nothing is open (`off_turn.reading`), and one that cannot be ended reads in the session, as before.
    await db.release_if_free(session)
    async with off_turn.reading(session) as reader:
        found = {role.id: role for role in [
            *await chase.wanted(reader, owner, Opportunity.sourced_at.is_(None)),
            *await unbuilt.roles(reader, owner, Opportunity.sourced_at.is_(None))]}  # the roles built out (2026-11-05)
        hosts = await employer_posting.job_board_hosts(reader, owner)
        chosen: list[tuple[Opportunity, str, str]] = []  # the role as read, the link it holds, and how
        for role in found.values():
            if not _links(role, hosts):
                continue
            employer = (await reader.get(Employer, role.employer_id) if role.employer_id
                        else await employers.find(reader, owner, role.company))
            held = stored_source(role, [], employer, hosts)
            if held is not None:
                chosen.append((role, held[0], held[1]))
    if not chosen:
        return 0
    sourced = 0
    # A slice at a time, the turn let go between.
    for start in range(0, len(chosen), SLICE):
        part = chosen[start:start + SLICE]
        locked = await as_read.lock(session, Opportunity, [role for role, _, _ in part], stands=_SOURCE_STANDS,
                                    where=(Opportunity.sourced_at.is_(None),))
        for role, link, how in part:
            if role not in locked:  # not as it was read: left for the next turn
                continue
            row = locked.rows[role.id]
            # The stands are the role's own columns; the choice also rested on its employer's (website, careers page,
            # job system and board), which an employer's change since the read does not show there. The employer is
            # read again here, under the turn, and the choice made again on it: a link its record no longer allows is
            # left unwritten.
            employer = await _employer_now(session, row)
            if stored_source(row, [], employer, hosts) != (link, how):
                continue
            await _keep_source(session, row, link, EMPLOYER, "code", hosts, how)
            sourced += 1
        await session.flush()
        await db.release_if_free(session)  # in a savepoint or process step this does nothing: one turn, as before
    return sourced


async def _employer_now(session: AsyncSession, role: Opportunity) -> Employer | None:
    """The role's employer as the database holds it now (read again, not as the session may hold it)."""
    if not role.employer_id:
        return await employers.find(session, role.owner, role.company)
    return await session.scalar(select(Employer).where(Employer.id == role.employer_id)
                                .execution_options(populate_existing=True))


#: What `source_held`'s choice of a role stood on: its links, its employer and whether it is Sourced.
_SOURCE_STANDS = (Opportunity.sourced_at, Opportunity.status, Opportunity.url, Opportunity.board_url,
                  Opportunity.apply_url, Opportunity.employer_id)


async def sweep(session: AsyncSession, owner: str) -> dict:
    """Try again, in code, every role still in play that is not Sourced: the nightly retry of what code can do. In the
    caller's transaction, only what needs no network (`source_stored`); a role that fails here is left as it was and
    the rest go on. The roles whose boards must be read are looked for once that transaction has committed, in the
    background and with no transaction open while a board is read (`sweep_boards`): the sweep runs inside a process
    step, and its board reads held that step's transaction (and the tool call or turn around it) for as long as the
    boards took. Returns `{looked, sourced}` (sourced here, before any board is read)."""
    if db.can_release(session):  # the caller owns its transaction: read apart, write in slices, gates in batches
        return await _sweep_in_slices(session, owner)
    return await _sweep_in_session(session, owner)


async def _sweep_in_session(session: AsyncSession, owner: str) -> dict:
    """`sweep` in the caller's one transaction (inside a savepoint or a process step, where nothing can be released)."""
    # Gates first: the cheap gates are decided on what each role's listing shows
    # before anything is spent on finding its posting, and again once sourcing has brought more (below).
    await _gate_first(session, owner)
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.status.in_(IN_PLAY), Opportunity.sourced_at.is_(None))
        .order_by(Opportunity.created_at))).all()
    hosts = await employer_posting.job_board_hosts(session, owner)
    sourced = 0
    left = False
    for role in roles:
        try:
            async with session.begin_nested():
                if await source_stored(session, role) == SOURCED:
                    sourced += 1
                elif board_roots(role, hosts):
                    left = True
        except Exception:
            log.warning("sourcing role %s in code failed", role.id, exc_info=True)
    if sourced:
        await _gate_first(session, owner)
    if left:
        db.after_commit(session, lambda: start_board_sweep(owner))
    return {"looked": len(roles), "sourced": sourced}


#: How many roles' postings one statement of the sweep's read carries.
_READ_IN = 500


async def _sweep_facts(reader: AsyncSession, owner: str, roles: list[Opportunity],
                       ) -> tuple[dict, dict, dict]:
    """What the sweep's choice rests on besides the roles, for all of them at once: their employers by id (one
    statement per `_READ_IN`), the employers of the roles with none by name (`employers.find_many`), and their postings
    (all of them, open or not) by role, with only the columns `stored_source` reads."""
    ids = list(dict.fromkeys(r.employer_id for r in roles if r.employer_id))
    by_id: dict = {}
    for start in range(0, len(ids), _READ_IN):
        for employer in (await reader.scalars(select(Employer).where(Employer.id.in_(ids[start:start + _READ_IN])))):
            by_id[employer.id] = employer
    by_name = await employers.find_many(reader, owner, [r.company for r in roles if not r.employer_id])
    return by_id, by_name, await _postings_by_role(reader, owner, [r.id for r in roles])


async def _postings_by_role(session: AsyncSession, owner: str, role_ids: list, *, fresh: bool = False) -> dict:
    """The postings of these roles (`role_postings.of_role`'s, with the columns `stored_source` reads), by role id."""
    out: dict = {}
    for start in range(0, len(role_ids), _READ_IN):
        stmt = select(JobPosting).where(
            JobPosting.owner == owner, JobPosting.opportunity_id.in_(role_ids[start:start + _READ_IN])).options(
            load_only(JobPosting.opportunity_id, JobPosting.ats, JobPosting.url, JobPosting.apply_url))
        if fresh:
            stmt = stmt.execution_options(populate_existing=True)
        for posting in await session.scalars(stmt):
            out.setdefault(posting.opportunity_id, []).append(posting)
    return out


async def _sweep_in_slices(session: AsyncSession, owner: str) -> dict:
    """`sweep` for a caller that can end its transaction (`db.can_release`), in the shape of `source_held`: the gates
    run a batch of roles to a transaction, the roles and what their choice rests on are read in a reader's session with
    no turn, the choice is made in memory, and the chosen roles are written `SLICE` at a time, each slice on the rows
    as they are then (`as_read.lock`), the turn let go between (one turn held for the whole sweep was most of the
    follow's hold). Each role is in a savepoint of its own: one that fails costs that role, not its slice. The rows
    written, and their order, are the in-session sweep's."""
    await db.release_if_free(session)
    await _gate_first_in_batches(owner)  # sweep_in_batches opens writer sessions of its own: none is held here
    from app.jobsearch.gating import gate_facts  # imported here, never at the top (see `_gate_first`)

    async with off_turn.reading(session) as reader:
        roles = (await reader.scalars(select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.status.in_(IN_PLAY), Opportunity.sourced_at.is_(None))
            .order_by(Opportunity.created_at))).all()
        hosts = await employer_posting.job_board_hosts(reader, owner)
        by_id, by_name, postings = await _sweep_facts(reader, owner, list(roles))
        chosen: list[tuple[Opportunity, str, str]] = []
        left = False
        pace = gate_facts._Pace()
        for role in roles:
            try:
                employer = by_id.get(role.employer_id) if role.employer_id else by_name.get(role.company)
                held = stored_source(role, postings.get(role.id, []), employer, hosts)
                if held is not None:
                    chosen.append((role, held[0], held[1]))
                elif board_roots(role, hosts):
                    left = True
            except Exception:
                log.warning("sourcing role %s in code failed", role.id, exc_info=True)
            await pace.tick()
    sourced = 0
    for start in range(0, len(chosen), SLICE):
        part = chosen[start:start + SLICE]
        locked = await as_read.lock(session, Opportunity, [role for role, _, _ in part], stands=_SOURCE_STANDS,
                                    where=(Opportunity.sourced_at.is_(None),))
        now = await _postings_by_role(session, owner, [r.id for r, _, _ in part if r in locked], fresh=True)
        for role, link, how in part:
            if role not in locked:  # not as it was read: left for the next turn
                left = left or bool(board_roots(role, hosts))
                continue
            row = locked.rows[role.id]
            kept = False
            try:
                async with session.begin_nested():
                    # The employer and the postings are read again under the turn and the choice made again on them:
                    # a role whose choice differs is left unwritten (an earlier role's learning of the employer's
                    # careers page is seen here, as it is in the in-session sweep).
                    employer = await _employer_now(session, row)
                    if stored_source(row, now.get(row.id, []), employer, hosts) == (link, how):
                        await _keep_source(session, row, link, EMPLOYER, "code", hosts, how)
                        kept = True
            except Exception:
                log.warning("sourcing role %s in code failed", role.id, exc_info=True)
                continue
            if kept:
                sourced += 1
            else:
                left = left or bool(board_roots(row, hosts))
        await session.flush()
        await db.release_if_free(session)
    if left:  # registered after the last release: it fires at the first commit after it, and the board sweep must not
        db.after_commit(session, lambda: start_board_sweep(owner))  # start before the slices and the gates are done
    if sourced:
        await _gate_first_in_batches(owner)
    return {"looked": len(roles), "sourced": sourced}


#: The board sweep running now, if one is (`start_board_sweep`): one at a time.
_board_sweep: asyncio.Task | None = None


def _launch_board_sweep(owner: str) -> None:
    """The task itself: `engine_calls.start("board_sweep", owner)` runs this in the program that does the work. One
    sweep at a time: asked while one is running, it starts nothing."""
    global _board_sweep
    if _board_sweep is not None and not _board_sweep.done():
        return
    _board_sweep = asyncio.get_running_loop().create_task(_sweep_boards_safely(owner))


engine_calls.register("board_sweep", _launch_board_sweep)


def start_board_sweep(owner: str) -> None:
    """Ask the engine, by name, for `sweep_boards` in the background (one at a time: `_launch_board_sweep`)."""
    engine_calls.start("board_sweep", owner)


@db.background_work
async def _sweep_boards_safely(owner: str) -> int:
    try:
        return await sweep_boards(owner)
    except Exception:  # tried again at the next sweep
        log.warning("sourcing the roles on their employers' boards failed", exc_info=True)
        return 0


async def sweep_boards(owner: str) -> int:
    """Step 3 of `source_in_code` for every role still in play that is not Sourced and has a stored link on a known job
    system's board root: the roles are chosen in one transaction, the boards read with none open, and each match kept
    in another, on the role as it is then (one sourced or out of play meanwhile, or locked by another, is left).
    Returns how many were sourced."""

    async def choose(session: AsyncSession) -> list[tuple[Opportunity, list[tuple[str, ats.Board]]]]:
        hosts = await employer_posting.job_board_hosts(session, owner)
        roles = (await session.scalars(select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.status.in_(IN_PLAY), Opportunity.sourced_at.is_(None))
            .order_by(Opportunity.created_at))).all()
        return [(role, roots) for role in roles if (roots := board_roots(role, hosts))]

    async def read(planned: list[tuple[Opportunity, list[tuple[str, ats.Board]]]]) -> list[tuple[object, str]]:
        found: list[tuple[object, str]] = []
        if not planned:
            return found
        fetch, cache = polite.make(), {}
        try:
            for role, roots in planned:
                try:
                    if (target := await _board_match(fetch, role, roots, cache)) is not None:
                        found.append((role.id, target))
                except Exception:
                    log.warning("reading the boards of role %s failed", role.id, exc_info=True)
        finally:
            await fetch.close()
        return found

    async def keep(session: AsyncSession, found: list[tuple[object, str]]) -> int:
        if not found:
            return 0
        hosts = await employer_posting.job_board_hosts(session, owner)
        kept = 0
        for role_id, target in found:
            role = await _claim_unsourced(session, role_id)
            if role is not None:
                await _keep_source(session, role, target, EMPLOYER, "code", hosts, ON_ITS_BOARD)
                kept += 1
        return kept

    kept = await db.read_fetch_write(choose, read, keep)
    if kept:  # once the keep is committed, a batch of roles to a transaction: the writer is not held for all of them
        await _gate_first_in_batches(owner)
    return kept


async def _claim_unsourced(session: AsyncSession, role_id: object) -> Opportunity | None:
    """The role, when it is still in play and not Sourced, taken for sourcing in one conditional statement (it is marked
    Sourced as it is read, so a second sweep asking at once is handed none), else None: a role sourced or out of play
    meanwhile is left. The role is as the database has it now, fresh in the session; `_keep_source` writes where it
    came from."""
    return await claim_one(session, select(Opportunity).where(
        Opportunity.id == role_id, Opportunity.status.in_(IN_PLAY), Opportunity.sourced_at.is_(None)),
        sourced_at=datetime.now(UTC))


async def _gate_first(session: AsyncSession, owner: str) -> None:
    """Read the gate facts of the roles before Ready that have not been read in their present state
    (`gate_facts.sweep`)."""
    from app.jobsearch.gating import gate_facts  # gate_facts reads postings and gates; imported here, never at the top

    await gate_facts.sweep_safely(session, owner)


async def _gate_first_in_batches(owner: str) -> None:
    """`_gate_first` for a caller with no transaction open: a batch of roles to a transaction."""
    from app.jobsearch.gating import gate_facts  # gate_facts reads postings and gates; imported here, never at the top

    await gate_facts.sweep_in_batches(owner)
