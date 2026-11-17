"""Roles graded on nothing, sent back for their text. Before the app refused findings that grade
nothing of a text with no requirement lines (`analysis.submit`), such a research was taken: the analyst said in its
notes that the text was not the posting (a careers page, a page's shell, a template), the role was settled Done with no
item to grade, and its score came from the owner's other asks alone. One stood in Ready to Launch at 82%.

`send_back_ungraded` finds those roles and sends each back as `submit` does now (`analysis.send_back`): the text is let
go, the role waits for the posting's text, a score over no grade is cleared, and the owner's browser reads the posting
at its source, once (a role whose text comes back as the same page is stopped, not sent round again). It is a repair
done once, so it is a command, run by the owner after the release, never a step of every start (`loops/startup.py`):
`cli.py send-back-ungraded`, whose `--dry-run` counts the roles and changes nothing. It leaves alone what `submit`
leaves alone: a job the owner added and a role nothing can read again (`analysis.may_send_back`). It is a caller of
the role's text and of the analysis, not part of either. Services flush; the caller commits."""

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis, requirement_lines

log = logging.getLogger(__name__)


@dataclass
class Ungraded:
    """What `send_back_ungraded` found (or did): how many roles, and in which stage each was."""

    roles: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)


async def send_back_ungraded(session: AsyncSession, owner: str, *, dry_run: bool = False) -> Ungraded:
    """Send back the roles that are Done, in a stage a reader asks the text of (`analysis.READ_STAGES`: before the
    application, Applied and Active Discussion), whose research the analyst sent and held no item
    (`analysis.graded_nothing`), that `analysis.may_send_back`, and whose held text has no requirement lines (the rule
    `submit` applies to findings: a text with lines and no items would have been refused). The database picks the
    candidates by what it can say of a role (its state, its stage, its origin, its source and the empty lists of its
    research: `analysis.graded_nothing_where`, `analysis.may_send_back_where`); only they are loaded, and the text is
    looked at for those alone. Each is sent back in a savepoint of its own, so one that fails is logged and the rest
    still go. With `dry_run` the roles are only counted."""
    out = Ungraded()
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.analysis_state == analysis.DONE,
        *analysis.graded_nothing_where(), *analysis.may_send_back_where(),
    ).order_by(Opportunity.id))).all()
    for role in roles:
        role_id = role.id  # a rolled-back savepoint expires the row, and its id is read for the log after that
        held = (await posting_text.held(session, role)).text
        if requirement_lines.split(held):
            continue
        label = stages.LABELS.get(role.status, role.status)
        if not dry_run:
            try:
                async with session.begin_nested():
                    await analysis.send_back(session, role, held, notes=str((role.analysis or {}).get("notes") or ""))
            except Exception:
                log.exception("couldn't send role %s back for its text", role_id)
                continue
        out.roles += 1
        out.by_stage[label] = out.by_stage.get(label, 0) + 1
    if out.roles and not dry_run:
        from app.jobsearch.loops import schedule  # the loop reads the analysis; imported here, never at the top

        schedule.signal_after_commit(session)  # a role that left Ready to Launch is placed in the pipeline's turn
        log.info("%d roles graded on nothing were sent back for their text: %s", out.roles, out.by_stage)
    return out
