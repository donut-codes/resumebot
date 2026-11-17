"""Two repairs the owner kept (2026-10-31): the posted day of roles that have none, and the posting's header on a text
that was read as a page. Live dry runs still find work for both, so their commands stay (`cli.py`:
`backfill-posted-dates`, `reextract-posting-header`).

They are callers of the role's text (`postings/text.py`), not part of it: neither is a way a text reaches a role. The
second replaces a held description by design (the page now gives the posting's header too), so it is the one second
writer of `Opportunity.description`; it goes when the repair does. Services flush; the caller commits."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import ats
from app.jobsearch.fetch import polite
from app.jobsearch.gating import gate_facts, stated_place
from app.jobsearch.models import SOURCE_KIND_BOARD_COPY, JobPosting, Opportunity, is_board_copy, is_unnamed
from app.jobsearch.postings import dates as posted_dates
from app.jobsearch.postings import index as postings
from app.jobsearch.postings import role_postings, same_role, whole
from app.jobsearch.research import analysis, settle

log = logging.getLogger(__name__)

#: The most seconds the repair reads one role's page.
FETCH_SECONDS = 40.0


def _text(posting: JobPosting) -> str:
    return (posting.text or "").strip()


async def _dated(session: AsyncSession, role: Opportunity, text: str | None, read_at: datetime | None,
                 system=None) -> bool:
    """Set the posted day of a role, and of its postings that have none, from what the text it just read says
    (`posted_dates.apply`): the job system's own date (`system`) first, else the text's words counted from `read_at`."""
    return posted_dates.apply(role, await role_postings.of_role(session, role), text=text, system=system,
                              read_at=read_at or datetime.now(UTC))


async def _saved(session: AsyncSession, role: Opportunity) -> None:
    """A role researched before its text changed is queued for research again, and one not yet researched has its gate
    facts read from the new text (what the role's text does after a change: `text.take`)."""
    await analysis.requeue_if_stale(session, role)
    await gate_facts.after_text(session, role)


# --- the posted day, for roles that have none (the owner, 2026-10-17) -------------------------------------------------


@dataclass
class Backfilled:
    """What `backfill_posted` found (or did): the roles with no posted day, how many of them got one (from a posting
    that holds the job system's own date, or from what their stored text says), and how many postings got one."""

    undated: int = 0
    filled: int = 0
    postings: int = 0


async def backfill_posted(session: AsyncSession, owner: str, *, dry_run: bool = False) -> Backfilled:
    """Give a posted day to the roles that have none, from what is already stored: the date a posting of the role holds
    (the job system's own), else what the text of the role, or of one of its postings, says ("Posted 3 days ago",
    counted from when that text was read: `posted_dates`). Reads nothing from the network. With `dry_run` it is only
    counted: the changes are made in the session and never flushed. Services flush; the caller commits."""
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.posted.is_(None)).order_by(Opportunity.id))).all()
    out = Backfilled(undated=len(roles))
    system: dict[str, date] = {}  # the job system's own day for each posting address it gave one for
    for row in (await session.scalars(select(JobPosting).where(
            JobPosting.owner == owner, JobPosting.posted.is_not(None)))).all():
        for mark in same_role.marks(same_role.posting_links(row)):
            if row.posted is not None:
                system[mark] = min(row.posted, system.get(mark, row.posted))
    with session.no_autoflush:
        for role in roles:
            rows = await role_postings.of_role(session, role)
            before = sum(1 for p in rows if p.posted is not None)
            # The same job under another address (a posting without the page's language) holds the day.
            theirs = [system[m] for m in same_role.marks([role.source_url, role.url, role.apply_url]) if m in system]
            candidates = [((role.description or "").strip(), role.description_at or role.created_at)]
            candidates += [(_text(p), p.detail_at or p.first_seen) for p in rows]
            for text, read_at in candidates:
                posted_dates.apply(role, rows, text=text or None, system=min(theirs, default=None),
                                   read_at=read_at or datetime.now(UTC))
                if role.posted is not None:
                    break
            out.filled += role.posted is not None
            out.postings += sum(1 for p in rows if p.posted is not None) - before
    if dry_run:
        session.expunge_all()
        return out
    await session.flush()
    return out


# --- texts kept before the posting's header was (the owner, 2026-10-17) ----------------------------------------------


@dataclass
class Headered:
    """What `reextract_posting_header` found (or did): the roles whose text is a page read from the source's own page
    (the reader that kept the posting's body only), how many got only the posting's header block in front of
    the same description (`header_only`: saved without research again), how many got a changed description
    (`reanalysed`: queued for research again), and how many kept their text (the page gave no more, or could not be
    read)."""

    candidates: int = 0
    header_only: int = 0
    reanalysed: int = 0
    kept: int = 0

    @property
    def replaced(self) -> int:
        return self.header_only + self.reanalysed


def _page_read(role: Opportunity) -> bool:
    """Whether a role's description is the text of its source read as a page (not a job system's own answer)."""
    source = (role.source_url or "").strip()
    at = ats.parse(source)
    return bool((role.description or "").strip() and role.sourced_at is not None and source
                and role.source_kind not in ("board", SOURCE_KIND_BOARD_COPY) and not is_board_copy(role)
                and not (at and at.drawn))  # the board's copy is not a page of its source read


async def reextract_posting_header(session: AsyncSession, owner: str, *, dry_run: bool = False) -> Headered:
    """Read again, from their source's page, the roles whose text was read as a page, so the text starts at the
    posting's title and keeps its header lines (place, full or part time, "Posted 3 days ago", requisition id, pay),
    and set the posted day from them. The new text replaces the old one (on the role and on the postings that held the
    same text) only when it is the whole of a posting (`whole.why_not`), holds more than the old one and is the role's
    page (`this_posting`: it names the role by its title, as every page the app reads for a role must).

    When the new text is the old one with only a header block in front (the description unchanged), it is saved with
    the day the old text was read, so the research is not queued again (`analysis.is_stale` stays false); the parts
    code decides are run again on it: the posted day, the work mode and place the header states, and the gates
    (`settle.roles`). A changed description queues the research again (`_saved`). With `dry_run` the pages are read
    (politely) and the roles counted by what would happen, and nothing is changed. Services flush; the caller
    commits."""
    roles = [r for r in (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.description.is_not(None)).order_by(Opportunity.id))).all()
        if _page_read(r)]
    out = Headered(candidates=len(roles))
    fetch = polite.make()
    try:
        for role in roles:
            await db.release(session)  # each role is written in its own transaction; none is held while a page loads
            try:
                async with asyncio.timeout(FETCH_SECONDS):
                    reading = await postings.read_page(  # the page is the role's text only when it names the role
                        fetch, (role.source_url or "").strip(), None if is_unnamed(role) else role.title)
            except Exception:  # a page that cannot be read leaves the role's text as it was
                log.info("the header of role %s was not read", role.id, exc_info=True)
                reading = postings.NOTHING
            text, facts = reading.text, reading.facts
            old = (role.description or "").strip()
            if not text or len(text.strip()) <= len(old) or whole.too_long(text, "text"):
                out.kept += 1
                continue
            header_only = " ".join(text.split()).endswith(" ".join(old.split()))
            if dry_run:
                out.header_only += header_only
                out.reanalysed += not header_only
                continue
            now = datetime.now(UTC)
            for posting in await role_postings.of_role(session, role):
                if _text(posting) == old:
                    posting.text = text
                    posting.detail_at = posting.detail_at if header_only else now
            if header_only:
                role.description = text  # read at the same time as the description it holds: no research is stale
                stated_place.keep_facts(role, facts)
                stated_place.fill(role, text)
                await _dated(session, role, text, now, facts.get("posted"))  # the header was read now
                await session.flush()
                await settle.roles(session, [role], cause="the posting's header was kept")
                out.header_only += 1
                continue
            role.description, role.description_at = text, now
            await _dated(session, role, text, now, facts.get("posted"))
            await session.flush()
            await _saved(session, role)
            out.reanalysed += 1
    finally:
        await fetch.close()
    return out
