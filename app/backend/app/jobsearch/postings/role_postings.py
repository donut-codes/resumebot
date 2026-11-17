"""Every role is in the log as a posting, and the text the app fetched is kept on it (docs/PRD.md 39.4, 39.7 and
39.9).

The log is the posting index (28.8). A role the app finds on a board is made from a posting it read there, and the
posting knows the role it became. Two kinds of role were not: a role the scout reports directly, and a role the owner's
tracker holds. Each gets a posting of its own here, its system `scout` or `import` (`ats.SCOUT`, `ats.IMPORT`), linked
to the role, so no role is outside the log and a re-run over the log reaches it as it reaches any role. A job the owner
adds by pasting its link gets one the same way, its system `owner` (`ats.OWNER`, `add_link.py`).

Such a posting is filed under a source of its own, not under the role's employer (`source_id`). What an employer's board
read lists is what the employer's postings are, and a read that no longer finds a posting closes it (`postings.store`):
a role's posting is not a board's, and no read of a board may close it, count it as the employer's hiring or offer it as
the employer's own listing of a role (`employer_posting.own_listings`).

A posting already in the log that is the role, in any writing of its company and title or by its link (`same_role`), is
the role's: it is linked to the role and no second posting is added (28.8). The tracker import adds one posting for
each role it makes, so importing the file again adds none; the migration that brought this in (0063) gave the roles that
were there before it theirs.

What the app fetched from a posting's page is kept as the posting's text where the posting has none
(`posting_text.take`), so the next reader is handed it and fetches nothing (39.7).

Services flush; the route, tool or process step commits.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import ats
from app.jobsearch.measures.pay_place import WORK_MODES
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.postings import hold, same_role
from app.timeutil import owner_zone

#: The ways of working a posting holds as its `work_mode`: a role's `move_type` is one of them or some other word (an
#: older import kept the older plugin's tiers there), and only these go on the posting.
WAYS_OF_WORKING = tuple(mode for mode in WORK_MODES if mode != "unknown")


def source_id(owner: str, system: str) -> uuid.UUID:
    """What the postings of an owner's roles hold as `employer_id`, by the system they hold (`ats.SCOUT` or
    `ats.IMPORT`): a fixed id of the owner's, never an employer's or a site's. (Migration 0063 makes the same ids.)"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"resumebot:{owner}:{system}")


def posting_of(role: dict, system: str, now: datetime) -> JobPosting:
    """The posting of a role (`role` is its stored fields: id, owner, company, title, match_key, location, move_type,
    comp, posted, found, url, apply_url, and the description and when it was read, when it has them), linked to it.
    Nothing was judged about it as a posting: the role is what was judged, so the posting is in the log as one the
    filter has already looked at (`filtered_at`), as the filter marks a posting that is a role the owner has.

    The dates are the role's own. A tracker role was first seen on the day the tracker says it was found, at the
    moment that day begins for the owner (their zone, not UTC: 00:00 UTC is the evening before in the Americas); a
    role the scout reported was first seen now."""
    found = role.get("found")
    seen = datetime.combine(found, time.min, owner_zone()).astimezone(UTC) if system == ats.IMPORT and found else now
    text = (role.get("description") or "").strip()
    read_at = (role.get("description_at") or now) if text else None
    link, way = role.get("url"), role.get("move_type")
    return JobPosting(
        owner=role["owner"], employer_id=source_id(role["owner"], system), ats=system, ext_id=str(role["id"]),
        match_key=role["match_key"], company=role["company"], title=role["title"][:500], location=role.get("location"),
        work_mode=way if way in WAYS_OF_WORKING else None, comp=role.get("comp"), posted=role.get("posted"), url=link,
        apply_url=role.get("apply_url"), text=text or None, detail_at=read_at, first_seen=seen, last_seen=seen,
        links=[link] if link else [], opportunity_id=role["id"], filtered_at=now)


async def log_imported(session: AsyncSession, roles: Iterable[dict], now: datetime | None = None) -> int:
    """A posting for each role the tracker import just made (docs/PRD.md 39.9): its link, employer, title, place and
    dates, with the import as its source. The caller passes only the roles that were stored now, so importing the file
    again, which makes none, adds no second posting. Returns how many were added."""
    now = now or datetime.now(UTC)
    rows = list(roles)
    made = [posting_of(role, ats.IMPORT, now) for role in rows]
    session.add_all(made)
    await session.flush()
    for role, posting in zip(rows, made, strict=True):
        await hold.hold(session, posting, hold.reported(role, "import"), now=now)
    return len(made)


async def log_reported(session: AsyncSession, role: dict, now: datetime | None = None) -> JobPosting:
    """The log's posting for a role the scout reported directly, which no posting read from a board became (docs/PRD.md
    39.4). A posting the log already holds that is this role (the same company and title in any writing, or the same
    link) is the role's: it is linked to it when no other role has it, and no second posting is added. Otherwise the
    role gets a posting of its own, which says the scout reported it. Returns the role's posting."""
    now = now or datetime.now(UTC)
    owner, link = role["owner"], role.get("url")
    held = await same_role.same_posting(session, owner, role["company"], role["title"], role["match_key"])
    if held is None and link:
        held = await session.scalar(select(JobPosting).where(
            JobPosting.owner == owner, JobPosting.url == link).order_by(JobPosting.first_seen).limit(1))
    if held is not None:
        if held.opportunity_id is None:
            held.opportunity_id = role["id"]
            await session.flush()
        return held
    posting = posting_of(role, ats.SCOUT, now)
    session.add(posting)
    await session.flush()
    await hold.hold(session, posting, hold.reported(role, "scout"), now=now)
    return posting


async def of_role(session: AsyncSession, role: Opportunity) -> list[JobPosting]:
    """The postings that are this role (a job system's, the scout's, the tracker's), with their text."""
    return list((await session.scalars(select(JobPosting).where(
        JobPosting.owner == role.owner, JobPosting.opportunity_id == role.id))).all())
