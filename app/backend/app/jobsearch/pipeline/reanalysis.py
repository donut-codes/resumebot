"""The test of re-analysing only the Ready to apply batch (the owner, 2026-10-19): mark just those roles to be
researched again, after storing what each looked like (`snapshot`), then set the new research beside it (`compare`).

The owner's `requeue` action (scopes `ready` and `second_impression`) marks them (`mark_stage`); the
`reanalysis.compare` action sets the old and the new side by side (`compare`, `table`). The snapshot is kept on the
role's analysis (`analysis.BEFORE_KEY`) and carried through the new research, so it is still there to compare with.
Services flush; the caller commits."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import board
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, requeue

SECTIONS = ("required", "preferred", "responsibilities")
CLASSES = ("P", "T", "U")


def _counts(items) -> dict[str, int]:
    kept = [i for i in items or [] if isinstance(i, dict)]
    return {c: sum(1 for i in kept if i.get("class") == c) for c in CLASSES}


def measure(role: Opportunity, span, profile) -> dict:
    """What a role's research looks like now: its fit percent, how many items it holds in each section, those items'
    class counts."""
    found = role.analysis or {}
    return {
        "fit_pct": scoring.percent_of(role, span),
        "items": {s: len(found.get(s) or []) for s in SECTIONS},
        "classes": {s: _counts(found.get(s)) for s in SECTIONS},
        "analysis_at": found.get("at"),
    }


async def ready_batch(session: AsyncSession, owner: str) -> list[Opportunity]:
    """The roles in the Ready to apply batch now: the first roles of the Ready to Apply line, as many as the owner's
    daily number."""
    ordered, size = await board.ready_line(session, owner)
    return ordered[:size]


async def second_impression_roles(session: AsyncSession, owner: str) -> list[Opportunity]:
    """Every role in Second Impression now, in the board's order (the whole stage, with no daily number)."""
    rows = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.status == stages.SECOND_IMPRESSION))).all()
    return await pipeline.ordered(session, owner, list(rows))


async def mark_stage(session: AsyncSession, owner: str, stage: str, *, dry_run: bool = False) -> list[Opportunity]:
    """Mark the roles of `stage` (Ready to Launch's batch, or Second Impression) that `requeue.reanalysable` accepts to
    be researched again, each with its snapshot stored first. A role already marked is left as it is, so this is safe
    to run again. With `dry_run`, change nothing."""
    found = await (ready_batch if stage == stages.READY_TO_LAUNCH else second_impression_roles)(session, owner)
    roles = [r for r in found if requeue.reanalysable(r)]
    if dry_run:
        return roles
    profile = await profile_store.current(session, owner)
    span = scoring.bounds(profile)
    snaps = {str(r.id): {analysis.BEFORE_KEY: measure(r, span, profile)} for r in roles}
    await requeue.mark_for_reanalysis(session, owner, roles, snaps)
    return roles


def _done(role: Opportunity) -> bool:
    """Whether the new research has replaced the one the snapshot was taken of."""
    stored = role.analysis or {}
    before = stored.get(analysis.BEFORE_KEY) or {}
    return (role.analysis_state == analysis.DONE and analysis.REANALYSE not in stored
            and stored.get("at") != before.get("analysis_at"))


async def compare(session: AsyncSession, owner: str) -> list[dict]:
    """One row for each role holding a snapshot, in the order of its old fit, highest first: `before` (the snapshot),
    and `after` (what the role looks like now, or None while the new research is still to come)."""
    profile = await profile_store.current(session, owner)
    span = scoring.bounds(profile)
    rows = []
    for role in (await session.scalars(select(Opportunity).where(Opportunity.owner == owner))).all():
        before = (role.analysis or {}).get(analysis.BEFORE_KEY)
        if not isinstance(before, dict):
            continue
        after = measure(role, span, profile) if _done(role) else None
        rows.append({"role": f"{role.title} at {role.company}", "before": before, "after": after})
    return sorted(rows, key=lambda r: (r["before"].get("fit_pct") is None, -(r["before"].get("fit_pct") or 0),
                                       r["role"]))


def _items(m: dict | None) -> str:
    if m is None:
        return "waiting"
    i = m["items"]
    return f"{i['required']}/{i['preferred']}/{i['responsibilities']}"


def _pct(m: dict | None) -> str:
    return "-" if m is None or m["fit_pct"] is None else f"{m['fit_pct']}%"


def table(rows: list[dict]) -> str:
    """The comparison as text: role, old fit, new fit, items before and after (required/preferred/responsibilities)."""
    head = ("role", "old fit", "new fit", "items before", "items after")
    body = [(r["role"], _pct(r["before"]), _pct(r["after"]), _items(r["before"]), _items(r["after"])) for r in rows]
    widths = [max(len(str(x[i])) for x in [head, *body]) for i in range(len(head))]
    lines = ["  ".join(str(c).ljust(w) for c, w in zip(line, widths, strict=True)).rstrip() for line in [head, *body]]
    lines.append("items are required/preferred/responsibilities")
    waiting = sum(1 for r in rows if r["after"] is None)
    if waiting:
        lines.append(f"{waiting} of {len(rows)} roles are not re-analysed yet")
    return "\n".join(lines)
