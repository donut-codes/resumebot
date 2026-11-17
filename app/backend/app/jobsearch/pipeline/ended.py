"""Why a role ended (docs/PRD.md 31.7). A role in an Ended stage (Rejected, Gone Quiet, Withdrew) records its reason, so
the classifier learns the right lesson from it: a role the owner passed on is a lesson about the kind of job, one the
employer turned down is a role the owner pursued, and a posting that came down teaches nothing about the job.

The owner says the reason when they end a role by hand (`board.move`). When they don't, it is worked out from what the
app holds: Rejected is the employer saying no, and a role whose posting has closed is a posting that went; a role the
owner withdrew from with neither is one they passed on. Services flush; the route owns the transaction."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch import stages
from app.jobsearch.models import JobPosting, Opportunity


class ReasonError(DomainError):
    """A reason that is not one of the reasons, in words."""


def check(reason: str | None) -> str | None:
    """The reason as given, or None for none; a word that is not one of the reasons is refused."""
    said = (reason or "").strip().lower()
    if not said:
        return None
    if said not in stages.END_REASONS:
        raise ReasonError(f"a role ended for one of {', '.join(stages.OWNER_END_REASONS)}, not {said}")
    return said


async def posting_closed(session: AsyncSession, role_id: uuid.UUID) -> bool:
    """Whether a posting this role was made from has come down from its board."""
    return bool(await session.scalar(select(func.count()).select_from(JobPosting).where(
        JobPosting.opportunity_id == role_id, JobPosting.closed_at.is_not(None))))


async def reason_for(session: AsyncSession, role: Opportunity, to: str, given: str | None = None) -> str | None:
    """The reason a role ends in the stage `to`: the one the owner gave, else Rejected as the employer saying no, else a
    posting that closed as a posting that went, else Withdrew as the owner passing on it, else unknown. None when `to`
    is not an Ended stage."""
    if to not in stages.ENDED:
        return None
    said = check(given)
    if said is not None:
        return said
    if to == stages.REJECTED:
        return stages.THEY_SAID_NO
    if await posting_closed(session, role.id):
        return stages.POSTING_GONE
    return stages.I_PASSED if to == stages.WITHDREW else stages.UNKNOWN_REASON


def words(role) -> str | None:
    """The reason an Ended role ended, in words for a card; None for a role still in play."""
    if role.status not in stages.ENDED:
        return None
    return stages.END_REASON_LABELS.get(role.ended_reason or stages.UNKNOWN_REASON, stages.END_REASON_LABELS[
        stages.UNKNOWN_REASON])


def offered() -> list[dict]:
    """The reasons the owner is offered when they end a role by hand, for the page."""
    return [{"reason": r, "label": stages.END_REASON_LABELS[r]} for r in stages.OWNER_END_REASONS]
