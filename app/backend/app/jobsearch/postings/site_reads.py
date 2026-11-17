"""Reading the sites by the skill the scout learned (docs/PRD.md 29.2, 29.5 and 30.1 step 2: every posting from every
site goes into the index), the other half of the night's read of the boards (`playbooks/jobsearch/read_boards.bpmn`).

`postings.refresh` reads an employer's board through its job system's built-in adapter. A site the scout learned has a
skill instead, a saved recipe (`sites/skills.py`): a data request replayed (`request`) or list pages read
(`list_page`). The night runs it with the same runner the Check uses (`skills.run_recipe`, never a second one) through
the same polite fetcher (robots.txt, a pause between requests to a host, a refusal is a stop), and puts what it reads
in the posting index exactly as a board's postings go (`postings.store`: first and last seen, closings, reopenings,
every link). The filter (`sift.py`) then treats them like any other.

**Who reads what, so no site is read twice.** An employer's site whose skill is a learned recipe is read here, and its
employer is left out of the built-in read (otherwise the adapter would read the board again each night, and when it
failed would retire the learned skill that works). An employer's site whose skill is an adapter recipe is the built-in
read's alone: the employer's job system decides what is read, and a board that is gone is let go (`resolve.forget`),
not read on from the recipe. A site that is not an employer's (a job board or an association's board) is read here
whatever the kind of its skill, an adapter recipe included: nothing else would read it. A site whose skill is
`browser` is left alone: it is the owner's browser session's to follow (docs/PRD.md 37.1). A site read in the owner's
browser, a site that is off, one to avoid and an employer the owner gated are not read.

**Where the postings are linked.** An employer's site is that employer's: `employer_id` is the employer and the company
its name, system `own_site`. A site that is not an employer's (a job board, an association's board, an address the owner
added) is treated as the browser reader does (`browser/reader.py`): `employer_id` is the site's id, the company is the
posting's own (a `request` recipe that maps `company`), else the site's name, system `job_site`; the employer record is
made when a posting becomes a role, as always.

**When it fails** (docs/PRD.md 29.4), a read is classified as the adapters' are (`postings.fault_kind`): `refused`,
`shape` and `empty` (it had roles and answered none) retire the version in force (`where.retire_broken`), so the next
Scout mission starts the site's project again; `gone` and `network` leave the skill alone and are tried again
tomorrow. Services flush; the night's step commits."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import adapters, ats, off_turn
from app.jobsearch.fetch.polite import Fetcher, Refused, timeup
from app.jobsearch.models import Employer, JobPosting
from app.jobsearch.postings import index as postings
from app.jobsearch.postings import read_progress
from app.jobsearch.states import FetchKind
from app.sites import avoid, recipes, skills
from app.sites.models import BY_APP, SEARCH, Site, SiteRecipe, SiteSkill, read_is

log = logging.getLogger(__name__)

#: The ways a read is known to fail; anything else is a recipe doing something unforeseen, which is logged.
EXPECTED = (Refused, skills.SkillError, adapters.AdapterError, httpx.HTTPError, ValueError)

#: The recipe kinds the night runs: `browser` is the browser's to run.
RUNNABLE = ("adapter", "request", "list_page", "embedded", "page", "html")
#: The kinds a project learns for a site, as opposed to a known job system's adapter.
LEARNED = ("request", "list_page", "embedded", "page", "html")
#: What a failed read of a live recipe's site is when the site refused it (`recipes.blocked`): the site rests.
BLOCKED = "blocked"
#: What a read counts, as `postings.refresh` does; `unread` is the boards the night's time ran out before it began (or
#: before a posting of it was read), and `cut` the ones it ran out in the middle of (docs/PRD.md 28.17).
COUNTS = ("employers", "failed", "seen", "new", "closed", "reopened", "unread", "cut")


@dataclass
class SiteRead:
    """One site to read tonight: the site, the version of its skill in force and, when the site is an employer's, the
    employer."""

    site: Site
    skill: SiteSkill | SiteRecipe  # a live recipe (`browser/recipes/live.py`) reads its site in place of any skill
    employer: Employer | None

    @property
    def source_id(self) -> uuid.UUID:
        """What the postings' `employer_id` holds: the employer, else the site (as the browser reader does)."""
        return self.employer.id if self.employer is not None else self.site.id

    @property
    def name(self) -> str:
        return self.employer.name if self.employer is not None else (
            (self.site.label or "").strip() or avoid.host_of(self.site.url) or self.site.url)

    @property
    def system(self) -> str:
        """What the postings' `ats` holds (`ats.OWN_SITE`, `ats.JOB_SITE`)."""
        return ats.OWN_SITE if self.employer is not None else ats.JOB_SITE


def kind_of(skill: SiteSkill | SiteRecipe | None) -> str | None:
    return (skill.recipe or {}).get("kind") if skill is not None else None


async def plan(session: AsyncSession, owner: str, watched: list[Employer]) -> tuple[list[Employer], list[SiteRead]]:
    """Split tonight's read. `watched` are the employers whose site is on and readable. Returns the employers the
    built-in read reads (all of them but those whose site is read by its learned skill) and the sites to read by their
    skill (see the module's docstring for who is left out)."""
    # The owner's rule: a live recipe is THE reader of its site.
    live = {r.site_id: r for r in (await session.scalars(select(SiteRecipe).where(
        SiteRecipe.owner == owner, SiteRecipe.site_id.is_not(None), SiteRecipe.retired_at.is_(None),
        SiteRecipe.state == "live").order_by(SiteRecipe.version))).all()}
    sites = list((await session.scalars(
        select(Site).where(Site.owner == owner, Site.list_name == SEARCH, Site.on.is_(True),
                           or_(read_is(BY_APP), Site.id.in_(list(live))))
        .order_by(Site.created_at, Site.url))).all())
    current: dict[uuid.UUID, SiteSkill | SiteRecipe] = {}
    if sites:
        for skill in (await session.scalars(
            select(SiteSkill).where(SiteSkill.site_id.in_([s.id for s in sites]), SiteSkill.retired_at.is_(None))
            .order_by(SiteSkill.version))).all():
            current[skill.site_id] = skill  # the newest version last wins, as `skills.current_skill` has it
    employer_ids = [s.employer_id for s in sites if s.employer_id is not None]
    employers = {e.id: e for e in (await session.scalars(
        select(Employer).where(Employer.owner == owner, Employer.id.in_(employer_ids)))).all()} if employer_ids else {}
    reads: list[SiteRead] = []
    taken: set[uuid.UUID] = set()
    for site in sites:
        skill = live.get(site.id) or current.get(site.id)
        employer = employers.get(site.employer_id) if site.employer_id is not None else None
        # An employer's site whose skill is an adapter is the built-in read's alone: the employer's own job system
        # decides what is read (`resolve.forget` lets a gone board go), never the recipe.
        if kind_of(skill) not in (LEARNED if employer is not None else RUNNABLE):
            continue
        if employer is not None and (employer.gated or employer.id in taken):  # never a gated employer, or one twice
            continue
        if employer is not None:
            taken.add(employer.id)
        reads.append(SiteRead(site, skill, employer))
    learned = {r.employer.id for r in reads if r.employer is not None}
    return [e for e in watched if e.id not in learned], reads


def fault_kind(exc: Exception) -> str:
    """Which kind of failed read this is (`postings.fault_kind`): the runner's own error carries the status the site
    answered with, as the adapters' do."""
    if isinstance(exc, skills.SkillError):
        return postings.fault_kind(adapters.AdapterError(str(exc), exc.status))
    return postings.fault_kind(exc)


async def _page_read(item: SiteRead, terms: list[str]):
    """A page recipe's read: the app's own Chrome, the owner's terms typed in the site's search box. A site whose turn
    at the Chrome did not come before the night's time ran out is `NOT_BEGUN`, as a site the clock stopped at the gate
    is (docs/PRD.md 28.17)."""
    from app.jobsearch.browser.recipes import page_run as page_recipe

    try:
        return await page_recipe.run(skills.parse_page(item.skill.recipe), terms)
    except page_recipe.NotBegun:
        return postings.NOT_BEGUN


async def _read(fetch: Fetcher, item: SiteRead, gate: asyncio.Semaphore, terms: list[str] | None = None):
    async with gate:
        if timeup(fetch):  # the night's read ran out of time before this site was begun: it waits for the next read
            return item, postings.NOT_BEGUN, None, None
        try:
            if kind_of(item.skill) == "page":
                return item, await _page_read(item, terms or []), None, None
            return item, await skills.run_recipe(fetch, item.skill.recipe), None, None
        except Exception as exc:  # a recipe is data the scout wrote: whatever it does, the night's read goes on
            if not isinstance(exc, EXPECTED):
                log.warning("reading %s by its skill failed unexpectedly", item.name, exc_info=True)
            if isinstance(item.skill, SiteRecipe) and recipes.blocked(exc):
                return item, None, str(exc) or exc.__class__.__name__, BLOCKED  # a stop, not a broken recipe
            return item, None, str(exc) or exc.__class__.__name__, fault_kind(exc)


async def _open_postings(session: AsyncSession, owner: str, source_id: uuid.UUID) -> int:
    return int(await session.scalar(select(func.count()).select_from(JobPosting).where(
        JobPosting.owner == owner, JobPosting.employer_id == source_id, JobPosting.closed_at.is_(None))) or 0)


async def refresh(session: AsyncSession, owner: str, fetch: Fetcher, reads: list[SiteRead],
                  now: datetime | None = None) -> dict:
    """Read each site by its skill and bring the index up to date. Returns what `postings.refresh` returns (a site read
    counts as a board read), with each fault carrying the `site_id` too: the site is what loses its skill."""
    now = now or datetime.now(UTC)
    gate = asyncio.Semaphore(postings.READ_AT_ONCE)
    await read_progress.set_part(session, "skilled", len(reads))
    already = await read_progress.sites_done()  # a read taken up after a restart skips the sites it finished
    total = len(reads)
    reads = [r for r in reads if str(r.source_id) not in already]
    finished: list[uuid.UUID] = []
    counts = {"employers": total, "failed": 0, "seen": 0, "new": 0, "closed": 0, "reopened": 0, "unread": 0,
              "cut": 0}
    faults: list[tuple[int, dict]] = []  # with the site's place, so they are told in the order the sites are

    def fault(place: int, item: SiteRead, why: str, kind: str) -> None:
        faults.append((place, {"employer": item.name, "employer_id": str(item.employer.id) if item.employer else None,
                               "site_id": str(item.site.id), "why": why, "kind": kind}))

    terms: list[str] = []
    if any(kind_of(r.skill) == "page" for r in reads):  # the owner's own target terms, typed where a page needs them
        from app.jobsearch.browser.recipes import page_run as page_recipe

        terms = await page_recipe.terms_of(session, owner)
    # Each site is saved once it is written (`finished_first` releases the session before each wait), never held while
    # the others are read.
    async for place, (item, listing, error, kind) in postings.finished_first(
            [_read(fetch, item, gate, terms) for item in reads], session):
        await read_progress.note_sites(session, finished)
        finished.clear()
        if listing is postings.NOT_BEGUN:  # the night's time ran out before it was begun (`_read`); nothing changes
            counts["unread"] += 1
            continue
        # A read the night's time cut short (it is partial and the time is out, not a repeated page) says nothing of the
        # site: what it read is kept, but never judged against the recipe's recent median, and one that ended before it
        # read a posting is no board that answered none. It is a site not read, as one not begun is (docs/PRD.md 28.17).
        cut = error is None and listing is not None and listing.truncated and timeup(fetch)
        if listing is not None and cut and not listing.postings:
            counts["unread"] += 1
            continue
        finished.append(item.source_id)
        employer = item.employer
        if employer is not None:
            employer.fetched_at = now
        if isinstance(item.skill, SiteRecipe) and not cut:
            # A live recipe is the site's reader (the owner, 2026-10-22): its night is judged on its own health, and
            # two bad nights in a row make it stale (the browser covers) and ask a re-record. It never retires a skill.
            from app.jobsearch.browser.recipes import live as recipe_live

            rows = len(listing.postings) if listing is not None else 0
            details = await recipe_live.with_details(session, fetch, item.skill, listing, item.source_id) if (
                error is None and rows) else 0
            night = {"rows": rows, "stats": recipes.quality(listing.postings) if listing is not None else None,
                     "error": error, "blocked": kind == BLOCKED, "details": details, "requests": fetch.requests}
            word = await recipe_live.settle(session, item.skill, item.site, night, now)
            if word != "ok":
                counts["failed"] += word != "blocked"
                await session.flush()
                continue
        elif error is not None:
            counts["failed"] += 1
            if employer is not None:
                employer.fetch_ok, employer.fetch_note, employer.fetch_kind = False, error[:500], kind
            fault(place, item, error[:500], kind)
            continue
        assert listing is not None  # no error: the read answered
        # The board is saved a slice at a time (`store` with `sliced`: each slice is committed), so what the site's
        # recipe and employer wrote above is committed first (`recipe_live.settle` has written what it needs) and the
        # baseline is counted with no turn held. The fault unit is the slice, as in `postings.refresh`: a failed slice
        # is logged, the slices before it stay, and the next read fills the rest.
        await db.release_if_free(session)
        if employer is not None:
            before = employer.open_roles
        else:
            async with off_turn.reading(session) as reader:
                before = await _open_postings(reader, owner, item.source_id)
        name, saved = item.name, (dict(counts), len(faults))
        # Only an employer's record says its last read was empty too; a job site's postings stay open until a read
        # that sees roles replaces them.
        again = employer is not None and postings.was_empty(employer)
        try:
            after = await postings.store(
                session, owner, listing, source_id=item.source_id, ats_name=item.system, company=item.name, now=now,
                counts=counts, company_from_postings=employer is None, close_empty=again,
                reader=f"recipe:{kind_of(item.skill)}:{item.skill.id}", reader_version=item.skill.version,
                sliced=True)
            async with session.begin_nested():
                emptied = bool(before) and after == 0  # it had roles and answered none: a fault, as in postings.refresh
                if emptied:
                    note = postings.EMPTY_NOTE.format(before=before)
                    fault(place, item, note, FetchKind.EMPTY)
                else:
                    note = listing.note or ("stopped before the last page" if listing.truncated else None)
                    if cut:  # cut short by the night's time, not by a repeated page
                        counts["cut"] += 1
                if employer is not None:
                    employer.fetch_ok, employer.fetch_note = not emptied, note
                    employer.fetch_kind = FetchKind.EMPTY if emptied else None
                    if not emptied or again:  # a first empty read closed nothing: the roles it had are the baseline
                        employer.open_roles = after
                await session.flush()
        except Exception as exc:
            log.exception("saving the site %s failed; the slice it failed in is rolled back, the slices before it stay "
                          "saved, and the other sites go on", name)
            counts["failed"], counts["cut"] = saved[0]["failed"], saved[0]["cut"]
            del faults[saved[1]:]
            counts["failed"] += 1
            fault(place, item, f"saving its board failed: {type(exc).__name__}: {exc}"[:500], "failed")
    await read_progress.note_sites(session, finished)
    return {**counts, "faults": [told for _, told in sorted(faults, key=lambda f: f[0])],
            "requests": fetch.requests}


def combine(first: dict, second: dict) -> dict:
    """Two reads' results as one night's: the counts added, the faults joined."""
    return {**{k: first.get(k, 0) + second.get(k, 0) for k in COUNTS},
            "faults": [*first.get("faults", []), *second.get("faults", [])],
            "requests": max(first.get("requests", 0), second.get("requests", 0))}
