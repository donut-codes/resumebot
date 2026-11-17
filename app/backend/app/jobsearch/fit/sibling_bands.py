"""The employer's other posted pay bands, worked out in code (docs/PRD.md 26): the analyst is not asked to research
them, so the
bands come from the app's own data, and the employer pay stance (`comp.compute`, which needs
`comp_stance_min_siblings`) reads them.

A sibling is another role of the same employer whose research is done and whose posting stated a band (`pay.band`,
never a modelled one) and whose market pay the analyst stored (`pay.market_value`): `{name, low, high, market}`. They
are given to the analyst as `sibling_bands`, and put on the analysis at submit (`merge`), so the stance reads them. The
method is unchanged, so `comp.METHOD_VERSION` stays: only roles that have siblings now differ, and they are worked out
as they are researched."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.models import Opportunity

#: The most sibling bands one role is given (the newest researched first).
MOST = 12
DONE = "done"


def band_of(role: Opportunity) -> dict | None:
    """The sibling this role makes: its posted band with its market pay, or None when it has either missing."""
    pay = (role.analysis or {}).get("pay") or {}
    band, market = pay.get("band"), pay.get("market_value")
    if not isinstance(band, list) or len(band) != 2 or not market:
        return None
    try:
        low, high, market = int(band[0]), int(band[1]), int(market)
    except (TypeError, ValueError):
        return None
    if low <= 0 or high < low or market <= 0:
        return None
    return {"name": (role.title or "").strip() or "Other role", "low": low, "high": high, "market": market}


async def for_role(session: AsyncSession, owner: str, role: Opportunity) -> list[dict]:
    """The sibling bands of the employer's other researched roles, newest research first, at most `MOST`."""
    if role.employer_id is None:
        return []
    others = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.employer_id == role.employer_id, Opportunity.id != role.id,
        Opportunity.analysis_state == DONE))).all()
    ordered = sorted(others, key=lambda r: str((r.analysis or {}).get("at") or ""), reverse=True)
    return [b for r in ordered if (b := band_of(r)) is not None][:MOST]


def merge(built: list[dict], sent: list[dict]) -> list[dict]:
    """The siblings the analysis keeps: the ones worked out in code, then any the analyst sent for a name they do not
    cover."""
    names = {b["name"].lower() for b in built}
    return [*built, *(s for s in sent if str(s.get("name", "")).strip().lower() not in names)]
