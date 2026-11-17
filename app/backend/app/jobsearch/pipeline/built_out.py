"""The pipeline's turn for the roles the owner applied to or is in talks about (the owner, 2026-11-05,
docs/PRD.md 32.6, 39.30): a role with no text of its posting (`postings/unbuilt.py`) waits for it, and is researched
when it comes. The step is the pipeline's, the way a star's is (`favorites.advance`): it runs in every turn, so a role
that comes into the stage is looked at at once (a move posts the turn), and a role the app finds so (the tracker's
import, a move by hand from a stage with no text) is not left out.

What it does to each role it finds, with no network and no model:

1. **It waits for the posting's text** (`analysis.NO_TEXT`): the research state a role
   with no text is in, which `analysis.resume_waiting` puts in the analyst's line when the text comes, whoever brought
   it (the browser's read, the app's own, the nightly sweep). A research marked done on no text (before 2026-10-15, when
   research began to need one) **counts as not done**: the state changes, and what was stored stays, for the new
   research to replace, as any research it runs again does. A role in the analyst's line is left to it.
2. **A role in Active Discussion carries the owner's ask** (`analysis.ASKED_BY`): its posting is found and read first,
   at any hour (the queue's `asked` set: `takeable.owners_item`), and it goes first in the analyst's line when the
   text comes, as if the owner had pressed "research it" (`role.research`, `analysis.ask`). The history line says the
   app asked, not the owner. A role in Applied is the backlog: no ask, the off hours.

A role the app gave up on (`posting_not_found`: its posting is gone, or no page of it grades) is left alone, and so is a
role that holds its text: nothing here researches a role that has text and no research (that is the deck's, the star's
and the owner's own ask). A role nothing can read (`analysis.can_read_again`: no link to open, or an Easy Apply, whose
listing was its source and is never read again) waits for its text all the same, since a research marked done on no text
counts as not done, but its history does not promise a read that no item will ever make (`UNREAD`). Every change is a
line in the role's history. Services flush; the turn commits."""

from sqlalchemy import and_, false, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import unbuilt
from app.jobsearch.research import analysis, line
from app.trail import service as trail

#: What the history says the app does, for a role in talks and for one applied to, and of a research marked done on no
#: text.
TALKS = ("The app finds its posting and reads it first, at any hour, as your own ask, and researches the role first "
         "when the text comes.")
BACKLOG = "The app finds its posting and reads it in the night's backlog, and researches the role when the text comes."
UNREAD = ("The role has no link the app can open to read its posting (an Easy Apply's listing, or no link at all), so "
          "nothing looks for its text: it waits until a text of it comes, and is researched then.")
KEPT = (" Its research was marked done before the app needed a posting's text for it: it counts as not done now, and "
        "what was stored stays until the new research replaces it.")


def _to_do():
    """The unbuilt roles this step has still to mark (SQL): not marked waiting for their text yet (none, done or failed
    on no text), or in Active Discussion, waiting, and without the owner's ask. One in the analyst's line is left to
    it."""
    unasked = and_(Opportunity.status == stages.ACTIVE_DISCUSSION, Opportunity.analysis_state == analysis.NO_TEXT,
                   not_(func.coalesce(line.owners_asks(), false())))
    return or_(Opportunity.analysis_state.is_(None), Opportunity.analysis_state.in_((analysis.DONE, analysis.FAILED)),
               unasked)


async def advance(session: AsyncSession, owner: str) -> int:
    """Mark each unbuilt role (`unbuilt.unbuilt`) waiting for its posting's text, and a role in Active Discussion with
    the owner's ask. Returns how many roles it changed. A second look at the same roles changes none."""
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, unbuilt.unbuilt(), _to_do())
        .order_by(Opportunity.id).execution_options(populate_existing=True))).all()
    for role in roles:
        name = f"{role.title} at {role.company}"
        talks = unbuilt.in_talks(role)
        label = stages.LABELS.get(role.status, role.status)
        read = analysis.can_read_again(role)  # an item will be made for it; else nothing ever reads it
        if talks:
            role.analysis = {**(role.analysis or {}), analysis.ASKED_BY: analysis.ASKED_BY_OWNER}
        if role.analysis_state == analysis.NO_TEXT:  # already waiting: only the owner's ask is new
            first = ("its posting is found and read first, and it is researched first when the text comes."
                     if read else "it is researched first when the text comes.")
            await trail.record(
                session, owner, event="research_asked", actor="app", who=trail.APP_WHO, link=("opportunity", role.id),
                sentence=f"{name} is in {label} and waits for its posting's text: {first}",
                detail="The app asked for this as you would have: you are in talks about this role.")
            await session.flush()
            continue
        kept = KEPT if role.analysis_state == analysis.DONE else ""
        promise = (TALKS if talks else BACKLOG) if read else UNREAD
        await analysis.wait_for_text(
            session, role, sentence=f"{name} is in {label} and holds no text of its posting, so it waits for it.",
            detail=f"{promise}{kept}")
    return len(roles)
