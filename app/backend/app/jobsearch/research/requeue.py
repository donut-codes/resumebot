"""Researched roles put back in the analyst's line, or marked to be researched again, when the research they hold is out
of date: `reanalyse_stale` (the door's `requeue` scope `stale`) for research
older than its posting's text or made from an input that changed, and `reanalyse_all` (the door's `requeue` scope `all`)
for every researched active role at once, highest fit first. Neither is something the app runs by itself.

A role marked to be researched again (`mark_for_reanalysis`) keeps its stage, its research and its score until the new
research replaces them; the pool takes it like any research, so its daily room and its order decide when. Services
flush; the caller commits."""

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.research import analysis, staleness
from app.trail import service as trail

#: The stages the sweeps take roles from. Applied and later, Gated out and the rest are never touched.
FROM_STAGES = stages.PRE_APPLY


async def reanalyse_stale(session: AsyncSession, owner: str, *, dry_run: bool = False) -> int:
    """Put every role whose research is out of date back in the analyst's line (scope `stale`), each with the
    reason in its history: its research is older than its posting's text (`analysis.stale`, as a text saved after
    research does: `analysis.requeue_if_stale`), or an input the research was made from changed
    (`staleness.analysis_reasons`: a master line it cites, its target role, a sentence rule or ask it answered, the
    analyst's instructions; only research that kept its versions, and only active roles in a stage before Applied
    (`FROM_STAGES`) that are not marked to be researched again; a role researched whose state was cleared by the old
    `stop_waiting` counts the same way). Research with no versions kept is never picked by them.
    Stage and packets are untouched; the old research stays until the new replaces it. With `dry_run`, change nothing
    and only count. Returns how many."""
    rows = (await session.scalars(
        select(Opportunity).where(
            Opportunity.owner == owner,
            or_(and_(Opportunity.analysis_state == analysis.DONE,
                     or_(analysis.stale(), Opportunity.status.in_(FROM_STAGES))),
                # researched, then taken out of the line by `pipeline.stop_waiting` before 1.148.1 (no state)
                and_(Opportunity.analysis_state.is_(None), Opportunity.analysed_at.is_not(None),
                     Opportunity.status.in_(FROM_STAGES))))
        .order_by(Opportunity.id).execution_options(populate_existing=True))).all()
    now = await staleness.Now.load(session, owner) if rows else None
    picked: list[tuple[Opportunity, list[str] | None]] = []
    for role in rows:
        if analysis.is_stale(role):
            picked.append((role, None))
        elif role.status in FROM_STAGES and analysis.REANALYSE not in (role.analysis or {}):
            if reasons := await staleness.analysis_reasons(session, role, now):
                picked.append((role, reasons))
    if dry_run:
        return len(picked)
    for role, reasons in picked:
        if reasons is None:
            await analysis.requeue_if_stale(session, role)
        else:
            await analysis.requeue_for_inputs(session, role, reasons)
    await session.flush()
    return len(picked)


#: What a role marked to be researched again (`reanalyse_all`) says in its history.
REANALYSE_EVENT = "requeued_for_reanalysis"
REANALYSE_WHY = ("The analyst now classes a posting's responsibilities too, and the score counts them, so this role is "
                 "researched again. The pool takes it like any research, behind the new finds, the highest fit first; "
                 "its stage and its old research stay until the new research replaces them.")


def reanalysable(role: Opportunity) -> bool:
    """Whether `reanalyse_all` marks the role: researched (`analysis_state` done), Sourced, active (a stage before
    Applied, `FROM_STAGES`: never Applied or later, Ended or Gated out) and not marked already."""
    stored = role.analysis or {}
    return (role.status in FROM_STAGES and role.analysis_state == analysis.DONE and bool(stored)
            and role.sourced_at is not None and analysis.REANALYSE not in stored)


async def reanalyse_all(session: AsyncSession, owner: str, *, dry_run: bool = False) -> list[Opportunity]:
    """Mark every `reanalysable` role to be researched again (`analysis.REANALYSE`), with a line in its history each,
    and return them in fit order, highest first (the order the pool takes them in, `analysis.picks`). The role keeps
    its stage, its research and its score until the new research replaces them; it is not put in the analyst's line
    here, so the pool's daily room and its order decide when, as for any research. With `dry_run`, change nothing.
    Safe to run again: a role marked already is left as it is. Services flush; the caller commits."""
    rows = (await session.scalars(
        select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.status.in_(FROM_STAGES),
            Opportunity.analysis_state == analysis.DONE, Opportunity.sourced_at.is_not(None))
        .order_by(Opportunity.id).execution_options(populate_existing=True))).all()
    roles = sorted((r for r in rows if reanalysable(r)), key=lambda r: (r.fit is None, -(r.fit or 0), str(r.id)))
    if dry_run:
        return roles
    await mark_for_reanalysis(session, owner, roles)
    return roles


async def mark_for_reanalysis(session: AsyncSession, owner: str, roles: list[Opportunity],
                              extra: dict[str, dict] | None = None) -> None:
    """Mark each of `roles` to be researched again, with a line in its history. `extra` is kept on a role's analysis
    beside the mark, by the role's id (`reanalysis.mark_stage` keeps the snapshot it compares with there)."""
    for role in roles:
        role.analysis = {**role.analysis, analysis.REANALYSE: True, **((extra or {}).get(str(role.id)) or {})}
        await trail.record(
            session, owner, event=REANALYSE_EVENT, actor="app", link=("opportunity", role.id),
            sentence=f"Marked {role.title} at {role.company} to be researched again.", detail=REANALYSE_WHY)
    await session.flush()
