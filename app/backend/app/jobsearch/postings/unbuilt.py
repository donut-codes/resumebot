"""The roles the owner applied to, or is in talks about, that hold no text of their posting (the owner, 2026-11-05,
docs/PRD.md 32.6).

Every role the owner is in discussion about has its posting and its full read. The roles the tracker's import brought
in already Applied or in talks never had either: the readers took only the stages before the application, so nothing
looked for such a role's posting, nothing read it, and the research marked done on them in the days before research
needed a text (`analysis.NO_TEXT`) was a research of nothing, which nothing asked for again.

A role here is **unbuilt** when it is in one of `STAGES`, holds no usable text of its posting (`holds_text`: its own
description, or a posting of a job system or of the employer's own site, `text.held`'s rule; a listing's snippet, the
tracker's import and a job board's copy are no text) and the app has not given up on its posting
(`posting_not_found`). The board's copy that became the role's own description, because the employer's posting could
not be read (the board's copy hook, 48.14 i), is a description: the role holds a text and is built out on it, and the
employer's text replaces it if it comes. `models.wants_sourcing` does not list it: the queue's sourcing
(`browser/reader.board_roles`) and the code's own step (`sourced.source_held`) add it to that list, and the reads
(`reader.to_read_at_source`, `to_read_in_full`) take its stages (`reader.READ_IN_FULL_STAGES`), so what a role here
needs is looked for the way it is for any other. How soon:

- **Active Discussion** is the owner's own: its sourcing and its reading are priority 0, handed out at any hour
  (`takeable.owners_item`), and its research is the owner's ask (`pipeline/built_out.py`: first in the analyst's
  line when the text comes), with no click.
- **Applied** is the backlog (`browser_pace.BACKLOG`), taken in the owner's off hours, and its research follows the
  text.

A posting that is gone ends the item with its reason, and the role stays where the owner put it (`quiet.end_gone`).
A role nothing can read (`analysis.can_read_again`: no link to open, or an Easy Apply) is unbuilt too but is given no
item; `pipeline/built_out.py` says so in its history.

Reads the database and nothing else. Services flush; nothing here commits."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.jobsearch import ats, stages
from app.jobsearch.models import JobPosting, Opportunity, posting_still_sought
from app.jobsearch.postings import whole

#: The stages whose roles are built out: the owner applied, or is in talks.
STAGES = (stages.APPLIED, stages.ACTIVE_DISCUSSION)


def in_talks(role: Opportunity) -> bool:
    """Whether the role is one the owner is in talks about (its sourcing, reading and research are the owner's own)."""
    return role.status == stages.ACTIVE_DISCUSSION


def holds_text() -> ColumnElement[bool]:
    """Whether a role holds a usable text of its posting (SQL): the rule of `postings/text.held`. Its own description,
    when it is not a snippet (`whole.thin_sql`), else a posting of the role from a job system the app knows or the
    employer's own site (`ats.from_source`) whose text is not one. A snippet a listing showed, a text the tracker's
    import or a job board gave, and a posting read in the owner's browser that was never opened are no text."""
    from_source = [*ats.SYSTEMS, ats.OWN_SITE]
    posting = exists().where(JobPosting.opportunity_id == Opportunity.id, JobPosting.ats.in_(from_source),
                             not_(whole.thin_sql(JobPosting.text)))
    return or_(not_(whole.thin_sql(Opportunity.description)), posting)


def unbuilt() -> ColumnElement[bool]:
    """The roles to build out (SQL): in `STAGES`, with no usable text, and whose posting the app has not given up on."""
    return and_(Opportunity.status.in_(STAGES), not_(holds_text()), posting_still_sought())


async def roles(session: AsyncSession, owner: str, *where: Any, order_by: Sequence[Any] = (),
                columns: Sequence[InstrumentedAttribute[Any]] | None = None) -> list[Any]:
    """The owner's unbuilt roles, narrowed by `where` and put in `order_by`. The list the browser's queue and the code's
    sourcing step add to `chase.wanted`'s. `Opportunity` instances, or, with `columns`, plain rows of those columns
    (the browser's queue, which writes none)."""
    if columns is None:
        return list((await session.scalars(select(Opportunity).where(
            Opportunity.owner == owner, unbuilt(), *where).order_by(*order_by))).all())
    return list((await session.execute(select(*columns).where(
        Opportunity.owner == owner, unbuilt(), *where).order_by(*order_by))).all())
