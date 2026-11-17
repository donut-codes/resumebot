"""The catch-up for roles added by a job system's link before the app kept the posting's work mode (a one-off step of
the command line, `cli.py fill-work-modes`; not something the app runs by itself).

The job system's own answer for a posting says whether the work is remote, hybrid or on site (one system's `remoteType`,
another's `workplaceType`), and a role added by link used to keep the text and drop that. With a place and no work mode
the gate could not tell; it now holds such a role as unknown, and this step fills it in. For each role that holds a
link on a job system whose reader reads one posting (`ats.parse(link).read`) and no work mode, the posting is read
again, politely, the work mode and, when the role has none, the place are kept on the role (`stated_place.keep_facts`),
and the role is settled again (`settle.roles`: its score and its gates, which read the way of working). A dry run only
counts the roles: it fetches nothing and writes nothing.
Each role is written in its own transaction (`db.release` before each read); the command commits the last."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import ats
from app.jobsearch.fetch import polite
from app.jobsearch.gating import stated_place
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import index as postings
from app.jobsearch.postings.role_postings import WAYS_OF_WORKING
from app.jobsearch.research import settle

CAUSE = "The job system's own posting says how the work is done"


@dataclass
class Result:
    """The roles the step takes (or took): `roles` hold a job system link and no work mode; `filled` got one now;
    `regated` were stopped, returned or restated by the gates as a result."""

    roles: int = 0
    filled: int = 0
    regated: int = 0


def link_of(role: Opportunity) -> str | None:
    """The role's link on a job system that has a reader for one posting, if it holds one."""
    return next((x for x in (role.source_url, role.url, role.apply_url, role.board_url)
                 if (found := ats.parse(x)) and found.read), None)


async def candidates(session: AsyncSession, owner: str) -> list[Opportunity]:
    """The roles with a job system link and no work mode (read-only)."""
    rows = (await session.scalars(select(Opportunity).where(Opportunity.owner == owner))).all()
    return [r for r in rows if (r.move_type or "").strip().lower() not in WAYS_OF_WORKING and link_of(r)]


async def run(session: AsyncSession, owner: str, *, dry_run: bool = False) -> Result:
    roles = await candidates(session, owner)
    result = Result(roles=len(roles))
    if dry_run or not roles:
        return result
    fetch = polite.make()
    try:
        for role in roles:
            link = link_of(role)
            await db.release(session)  # each role is written in its own transaction; none is held while a page loads
            facts = (await postings.read_link(fetch, link)).facts if link else {}
            before, where = role.move_type, role.location
            stated_place.keep_facts(role, facts)
            if role.move_type == before and role.location == where:
                continue
            result.filled += 1
            settled = await settle.roles(session, [role], cause=CAUSE)
            result.regated += bool(settled.gated)
    finally:
        await fetch.close()
    await session.flush()
    return result
