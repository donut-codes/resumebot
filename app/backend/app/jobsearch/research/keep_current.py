"""What the log and the scores hold is kept current with the settings in force, by the app itself (docs/PRD.md 39.14,
39.21, 39.23).

A save of Hard pass checks the whole log and every role again (`regate.py`), and a save of Prioritization works every
score out again (`prioritize.py`); each records the version of the settings it ran under. But a save that changes
nothing makes no new version and runs nothing, so a release leaves what it finds as it was: stops made before the
version was kept as data, or by older gates, and scores worked out before their version was kept, until the owner next
changes the setting. This runs the same re-gate and the same re-score whenever they are stale, whoever last saved the
settings:

- **at every start of the app** (`startup.run_startup_fixups`), so a release is followed at once; and
- **at the start of the night's read** (the first step of `read_boards.bpmn`, `actions.keep_current`), so the night's
  work, and the postings it filters, begin from a log that is current.

Both are code and take seconds. When every stop and every score is
current they do nothing, so running them often costs a few counts. Nothing here reads a job board, fetches a page or
calls Claude. Services flush; the caller commits, except the re-gate and the scores, which commit in slices where
they can (`regate.py`, `prioritize.rescore`)."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.gating import regate
from app.jobsearch.research import prioritize

log = logging.getLogger(__name__)


async def run(session: AsyncSession, owner: str) -> dict:
    """Re-gate when stops are judged under an older Hard pass than the one in force (or none), then work the scores out
    again when some are under an older Prioritization (or none), in the order of the stages. Returns what each did, in
    the line a save would say: `{"gates": str | None, "scores": str | None}`, None for the one that had nothing to do
    (or failed). One that fails is logged with its traceback and is tried again at the next start or night, and the
    other still runs.

    **No turn for the checks, none held across the work.** Whether any stop or score is stale is a read in a reader's
    session; the work is committed in slices as it goes: the re-gate's (`regate.run`) and the scores' (a page of
    `prioritize.rescore` at a time). A failure rolls back only the slice that was open: the ones before it stay, and the
    next check finds what was not reached. Inside a savepoint or the engine's call (`db.can_release`) each is one
    transaction that goes with its savepoint, as the whole pass always was, and a failure there leaves nothing."""
    gates: str | None = None
    scores: str | None = None
    nested = not db.can_release(session)
    try:
        if nested:
            async with session.begin_nested():
                report = await regate.catch_up(session, owner)
        else:
            report = await regate.catch_up(session, owner)
        if report is not None:
            gates = report.said
            log.info("the gates were checked again, stops were under an older Hard pass: %s", gates)
    except Exception:
        if not nested:
            await session.rollback()
        log.exception("couldn't check the gates again for stops under an older Hard pass")
    try:
        if nested:
            async with session.begin_nested():
                scores = await prioritize.catch_up(session, owner)
        else:
            scores = await prioritize.catch_up(session, owner)
        if scores:
            log.info("the scores were worked out again, some were under an older Prioritization: %s", scores)
    except Exception:
        if not nested:
            await session.rollback()
        log.exception("couldn't work the scores out again for scores under an older Prioritization")
    return {"gates": gates, "scores": scores}
