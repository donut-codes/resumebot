"""The sample a Find Gaps run reads (docs/PRD.md 38.1): up to 10 stored postings for one target role, at most one from
each employer, so that one organisation's wording does not weigh more than another's and the themes show what the market
asks, not what one employer repeats. At least 5 are needed.

A stored posting for a role is a role in the Pipeline matched to it that kept its text (`Opportunity.description`), or
a posting the app read from an employer's board that the title pass matched to it and that has its text
(`JobPosting.text`); a posting that became a role is counted once. The draw is random across the employers that have
one, and a New sample prefers employers, and postings, that the sample before it did not read. This is code: choosing
is not the agent's judgement. Nothing here writes.
"""

import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.employers import core as employers
from app.jobsearch.models import JobPosting, Opportunity

#: The most postings a run reads, and the fewest it will run on.
MAX_POSTINGS, MIN_POSTINGS = 10, 5
#: The most of a posting's text a run keeps and an agent is handed (the qualifications are often at the end).
TEXT_CHARS = 8_000

#: Where a stored posting came from.
FROM_ROLE, FROM_POSTING = "role", "posting"


@dataclass(frozen=True)
class Stored:
    """One stored posting for a role, as the sample sees it."""

    source: str
    source_id: str
    employer: str
    title: str
    url: str | None
    text: str

    @property
    def key(self) -> str:
        """Who the employer is: its name folded the way the Employers page matches names."""
        return employers.key_of(self.employer) or self.employer.strip().lower() or self.source_id


def _kept(text: str | None) -> str:
    return (text or "").strip()[:TEXT_CHARS]


async def stored(session: AsyncSession, owner: str, role_id: str) -> list[Stored]:
    """Every stored posting for the target role with this id that has its text."""
    roles = (await session.execute(
        select(Opportunity.id, Opportunity.company, Opportunity.title, Opportunity.url, Opportunity.description)
        .where(Opportunity.owner == owner, Opportunity.target_role == role_id, Opportunity.description.is_not(None))
        .order_by(Opportunity.id))).all()
    out = [Stored(FROM_ROLE, str(r.id), r.company, r.title, r.url, _kept(r.description))
           for r in roles if _kept(r.description)]
    as_roles = {r.id for r in roles}
    posts = (await session.execute(
        select(JobPosting.id, JobPosting.company, JobPosting.title, JobPosting.url, JobPosting.text,
               JobPosting.opportunity_id)
        .where(JobPosting.owner == owner, JobPosting.target_role == role_id, JobPosting.text.is_not(None))
        .order_by(JobPosting.id))).all()
    out += [Stored(FROM_POSTING, str(p.id), p.company, p.title, p.url, _kept(p.text))
            for p in posts if _kept(p.text) and p.opportunity_id not in as_roles]
    return out


def draw(found: list[Stored], avoid: set[str] | None = None, rng: random.Random | None = None
         ) -> tuple[list[Stored], int]:
    """(the sample, how many employers had a stored posting): one posting from each of up to `MAX_POSTINGS` employers,
    drawn at random. `avoid` holds the ids of the postings the sample before read: an employer with another posting is
    read through that one, and an employer none of whose postings was read last time comes first."""
    rng = rng or random.Random()
    avoid = avoid or set()
    by_employer: dict[str, list[Stored]] = {}
    for posting in found:
        by_employer.setdefault(posting.key, []).append(posting)
    fresh: list[Stored] = []
    again: list[Stored] = []
    for members in by_employer.values():
        rng.shuffle(members)
        unread = [m for m in members if m.source_id not in avoid]
        (fresh if unread else again).append((unread or members)[0])
    rng.shuffle(fresh)
    rng.shuffle(again)
    return [*fresh, *again][:MAX_POSTINGS], len(by_employer)
