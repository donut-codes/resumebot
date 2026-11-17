"""Postings of a board the app no longer reads are Not checked (docs/PRD.md 39.8).

A board stops being read when the owner switches the employer, the site or its job system off under Where to look, when
its job system is one a new install starts with off (`ats.NOT_OFFERED`, docs/PRD.md path 55: the screens then say
resumebot doesn't read it unless it is switched on by hand, `where_view._status`), when the app turns a site off
because it cannot be read (29.3), or when the employer is gated. Its postings then stay in the
log with nothing checking whether the employer still lists them, so the log must not call them open: they are marked
Not checked (`JobPosting.not_checked_at`), with the day they were last seen (`last_seen`, which is kept). They are not
closed either, since nothing says the employer took them down. No stage works a Not checked posting and the log counts
none of them as open (`JobPosting.is_open`).

When the board is read again each of its postings is open or closed as that read finds it (`postings.store`; the owner's
browser seeing one under its own link opens it too, `browser_reader`), and a read that could not say, because it was cut
short, leaves it as it was.

**A board the app reads** (`reading`) is one the owner has a site on for (a site to search that is switched on) and
that nothing above has taken out: an employer's board is read while its site is on, the employer is not gated and its
job system is on; a job board's or an association's, while its site is on; a job board's signed-out job search,
while the owner has it on (`registry.follow_board_on`). A board with no site is read by nothing. A
board whose skill broke is still switched on and is being learned again, so it stays as it is (29.4).

The postings of a role (`ats.SCOUT` and `ats.IMPORT`, `role_postings.py`) are no board's and are left alone.

`settle` marks, and runs wherever a board stops being read: the owner's switch on a site (`where.switch`) or on a job
system (`job_systems.switch`), the app turning a site off (`where._switch_off`), gating an employer
(`employers.edit`) and the night's read (`actions.read_boards`). Migration 0063 brought the log to the rule once, for
the postings that were left open before it. Services flush; the caller commits.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import ats, off_turn
from app.jobsearch.models import Employer, JobPosting
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import queue
from app.sites import registry
from app.sites.models import SEARCH, Site

#: How many boards one statement names (a parameter for each board; the driver takes 32,767 at most).
AT_A_TIME = 500


async def reading(session: AsyncSession, owner: str) -> set[uuid.UUID]:
    """The sources the app reads (a posting's `employer_id` holds its source, an employer's id or a site's): see the
    module's note."""
    settings = (await profile_store.current(session, owner)).settings
    switched_off = set(settings.job_systems_off)
    sources: set[uuid.UUID] = set()
    if registry.follow_board_on(settings):  # a board's signed-out job search, while the owner has it on (PRD path 48)
        board = registry.board_reader("guest")  # which reads postings, so not at the top
        if board is not None:
            sources.add(board.source_id(owner))
    for site_id, employer_id, gated, system in (await session.execute(
        select(Site.id, Site.employer_id, Employer.gated, Employer.ats)
        .outerjoin(Employer, Employer.id == Site.employer_id)
        .where(Site.owner == owner, Site.list_name == SEARCH, Site.on.is_(True))
    )).all():
        if employer_id is None:  # a job board's or an association's: filed under the site
            sources.add(site_id)
        elif not gated:
            sources.add(site_id)  # what the owner's browser read of the employer's site is filed under
            if system not in switched_off:
                sources.add(employer_id)
    return sources


async def settle(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """Mark the open postings of every board the app does not read as Not checked. Safe to run again: a posting already
    marked, a closed one and a role's are left as they are. Returns how many were marked.

    It is one conditional statement for each slice of boards (`UPDATE ... WHERE id IN (SELECT ...) RETURNING`, the shape
    of `work/claim.py` without its row load: a night holds tens of thousands of postings), so a posting is marked once
    however many calls ask at once. The owner switching a job system off while the night's read runs waits for the
    writer's turn like any write, and the read calls this again as its last step and sees what the owner switched by
    then."""
    now = now or datetime.now(UTC)
    open_ones = (JobPosting.owner == owner, JobPosting.is_open, JobPosting.ats.not_in(ats.OF_ROLES))
    # The scan of the open postings is the long read (a long read): where the session is free it
    # is made in a reader's session, with no writer's turn. The switch read (`reading`) and the marks stay in `session`,
    # so a switch made meanwhile is seen at write time, and the UPDATE asks again in SQL whether each posting is still
    # open and its board still unread.
    async with off_turn.reading(session) as reader:
        held = set((await reader.scalars(select(JobPosting.employer_id).where(*open_ones).distinct())).all())
    unread = sorted(held - await reading(session, owner), key=str)
    marked = 0
    for start in range(0, len(unread), AT_A_TIME):
        free = select(JobPosting.id).where(*open_ones, JobPosting.employer_id.in_(unread[start:start + AT_A_TIME]))
        done = await session.execute(
            update(JobPosting).where(JobPosting.id.in_(free)).values(not_checked_at=now).returning(JobPosting.id))
        marked += (here := len(done.all()))
        # Postings no longer checked leave the browser's queue and the deck's counts: posted with the slice's own write,
        # which is a turn of its own (one write over every unread board held the writer too long).
        if here:
            queue.post_all(session, owner, [(queue.BROWSER_RECONCILE, [queue.ANY]), (queue.DECK_DIRTY, [""])],
                           cause="core:not_checked.settle")
        await db.release_if_free(session)
    if not marked:  # nothing marked: this only answers
        queue.post_all(session, owner, [(queue.BROWSER_RECONCILE, []), (queue.DECK_DIRTY, [])],
                       cause="core:not_checked.settle")
    return marked
