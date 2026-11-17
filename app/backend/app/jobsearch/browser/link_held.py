"""What the page may know of an added link without the engine (docs/PRD.md 24.10): whether the owner already has the
job (a role, or a posting in the log), the answers that say where a job stands, and the one new posting a link makes in
Added links. Database work only, no gate, no page read: the page answers with it at once (`routes/role.py`) and the
engine does the rest (`add_link.add`, which uses the same functions). Imports nothing of the engine."""

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import ats, stages
from app.jobsearch.browser.reader import BrowserError, canonical
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import ADDED_LINK, JobPosting, Opportunity, is_unnamed
from app.jobsearch.postings import role_postings, same_role
from app.sites import avoid

log = logging.getLogger(__name__)


def is_it(links: Iterable[object], key: str, marks: frozenset[str], boards: frozenset[str]) -> bool:
    """Whether any of these addresses is the job: the same cleaned address, or the same job id on the same host (a job
    board's host folded to its name: `board_marks`)."""
    for link in links:
        if not isinstance(link, str) or not link.strip():
            continue
        if marks & same_role.board_marks([link], boards):
            return True
        try:
            if canonical(link)[0] == key:
                return True
        except BrowserError:
            continue  # an address that is not a web address is no job's
    return False


def _needles(address: str, marks: frozenset[str]) -> list[str]:
    """What an address that is this job must contain, whatever else it says: the host and path, or the job id. They let
    the database narrow what is read to a few rows; `is_it` decides."""
    host = avoid.host_of(address) or ""
    return [f"{host}{urlsplit(address).path.rstrip('/')}", *sorted({m.split("#", 1)[1] for m in marks})]


async def held(session: AsyncSession, owner: str, key: str, address: str,
                boards: frozenset[str]) -> tuple[uuid.UUID | None, JobPosting | None]:
    """The role the owner already has for this job, or else the posting in the log that is it and that no role was made
    from. Roles are asked first, the oldest first; then the postings, the first seen first."""
    marks = same_role.board_marks([address], boards)
    needles = _needles(address, marks)

    def narrow(*columns: Any) -> Any:
        return or_(*(column.icontains(needle, autoescape=True) for column in columns for needle in needles))

    roles = (await session.execute(
        select(Opportunity.id, Opportunity.url, Opportunity.apply_url, Opportunity.board_url)
        .where(Opportunity.owner == owner, narrow(Opportunity.url, Opportunity.apply_url, Opportunity.board_url))
        .order_by(Opportunity.created_at, Opportunity.id))).all()
    for row in roles:
        if is_it((row.url, row.apply_url, row.board_url), key, marks, boards):
            return row.id, None
    postings = (await session.scalars(
        select(JobPosting).where(
            JobPosting.owner == owner,
            narrow(JobPosting.url, JobPosting.apply_url, cast(JobPosting.links, Text)))
        .order_by(JobPosting.first_seen, JobPosting.id))).all()
    loose = None
    for posting in postings:
        if not is_it(same_role.posting_links(posting), key, marks, boards):
            continue
        if posting.opportunity_id is not None:
            return posting.opportunity_id, None
        loose = loose or posting
    return None, loose


ADDED_LINKS = "added_links"
CLOSED = "closed"


def role_answer(role: Opportunity, *, said: str) -> dict:
    return {"role": str(role.id), "posting": None, "already": True, "said": said, "stage": role.status,
            "stage_label": stages.LABELS.get(role.status, role.status), "title": role.title, "company": role.company,
            "sourced": role.sourced_at is not None}


def already_on(role: Opportunity) -> dict:
    called = "That link" if is_unnamed(role) else f"{role.title} at {role.company}"
    return role_answer(role, said=f"{called} is already on your pipeline, in "
                        f"{stages.LABELS.get(role.status, role.status)}.")


def named(posting: JobPosting) -> bool:
    return not (posting.ats == ats.OWNER and posting.company == ADDED_LINK)


def _where(posting: JobPosting) -> tuple[str, str]:
    """(the stage key, its label) a posting that is no role stands in, under the current model (`lifecycle.stage_of`):
    Gated out when the filter stopped it; Landing Zone when it is named (a raw posting, a role in the making); the
    Added links list when nothing has named it yet and it waits for the browser reader."""
    if posting.filtered_by:
        return stages.GATED_OUT, stages.LABELS[stages.GATED_OUT]
    if not named(posting):
        return ADDED_LINKS, "Added links"
    return stages.LANDING_ZONE, stages.LABELS[stages.LANDING_ZONE]


def posting_answer(posting: JobPosting, *, already: bool) -> dict:
    stage, label = _where(posting)
    if posting.closed_at is not None:  # a browser read found the page gone (HTTP 410): Added links does not show it
        called = f"{posting.title} at {posting.company}" if named(posting) else "That link"
        return {"role": None, "posting": str(posting.id), "already": already, "stage": CLOSED, "stage_label": "Closed",
                "said": (f"{called} {'is already on your pipeline' if already else 'was added'}, but its page says the "
                         "posting is gone, so it is closed."),
                "title": posting.title, "company": posting.company, "sourced": False}
    called = f"{posting.title} at {posting.company}" if named(posting) else "That link"
    if stage == stages.GATED_OUT:
        said = (f"{called} {'is already on your pipeline' if already else 'was added'}, in Gated out: "
                f"{posting.filtered_by}")
    elif already:
        said = f"{called} is already on your pipeline, in {label}."
    elif stage == ADDED_LINKS:
        said = ("Added, and waiting in the Added links list for its page to be read. It goes through the same steps as "
                "any posting the scout finds: the gates, the title check, then reading and research.")
    else:
        said = (f"Added to {label}. It goes through the same steps as any posting the scout finds: the gates, the "
                "title check, then reading and research.")
    return {"role": None, "posting": str(posting.id), "already": already, "said": said, "stage": stage,
            "stage_label": label, "title": posting.title, "company": posting.company, "sourced": False}

async def make_posting(session: AsyncSession, owner: str, key: str, address: str) -> tuple[JobPosting, bool]:
    """The posting a new link makes: named by its link, the employer "Added link", waiting in Added links and not yet
    filtered. Returns (the posting, whether it was just made); a second click or tab that made it a moment sooner gets
    that one, not made."""
    now = datetime.now(UTC)
    source = role_postings.source_id(owner, ats.OWNER)
    posting = JobPosting(
        owner=owner, employer_id=source, ats=ats.OWNER, ext_id=key,
        match_key=match_key(ADDED_LINK, address.removeprefix("https://")), company=ADDED_LINK,
        title=address.removeprefix("https://")[:500], url=address, links=[address], first_seen=now, last_seen=now)
    try:
        async with session.begin_nested():
            session.add(posting)
            await session.flush()
    except IntegrityError:
        sooner = await session.scalar(select(JobPosting).where(
            JobPosting.owner == owner, JobPosting.employer_id == source, JobPosting.ext_id == key))
        if sooner is None:
            raise
        return sooner, False
    log.info("added link: posting %s on %s", posting.id, avoid.host_of(address))
    return posting, True


async def known(session: AsyncSession, owner: str, key: str, address: str) -> dict | None:
    """Where the owner already has this job, as an answer with `already` true; None when it is new. A role is answered
    as it stands (nothing is changed: the engine's `add_link.add` does what pasting a link to a role does)."""
    from app.jobsearch.employers import follow as employer_posting

    in_role, loose = await held(session, owner, key, address, await employer_posting.job_board_hosts(session, owner))
    there = await session.get(Opportunity, in_role) if in_role is not None else None
    if there is not None:
        return already_on(there)
    return posting_answer(loose, already=True) if loose is not None else None
