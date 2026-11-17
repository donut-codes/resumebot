"""A research run nothing holds any more is cancelled at the start (`stuck.cancel_orphans`)."""

from process_engine.models import CANCELLED, WAITING, ProcInstance

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.research import analysis
from app.jobsearch.work import stuck
from tests.support import OWNER


def _role(slug: str) -> Opportunity:
    return Opportunity(owner=OWNER, slug=slug, company=f"Firm {slug}", title=f"Lead {slug}", origin="scout",
                       match_key=f"firm {slug}|lead {slug}", status=stages.FIRST_IMPRESSION,
                       description="The whole posting, as its employer wrote it. " * 40)


async def test_a_waiting_analysis_no_role_holds_is_cancelled_and_a_held_one_is_left(js):
    """Analyses left waiting that no role named (migrations and older
    releases cleared the role's state without ending its run), and others whose role was in no line."""
    async with db.session_factory()() as session:
        held, dropped, cleared = _role("held"), _role("dropped"), _role("cleared")
        session.add_all([held, dropped, cleared])
        await session.flush()
        for role in (held, dropped, cleared):
            await analysis.start(session, OWNER, role)
        runs = {r.slug: r.analysis_instance_id for r in (held, dropped, cleared)}
        dropped.analysis_state = dropped.analysis_instance_id = None  # what an old migration did
        cleared.analysis_state = None  # still named, but in no line
        await session.commit()
    async with db.session_factory()() as session:
        assert await stuck.cancel_orphans(session, OWNER) == 2
        await session.commit()
    async with db.session_factory()() as session:
        status = {slug: (await session.get(ProcInstance, run)).status for slug, run in runs.items()}
        assert status == {"held": WAITING, "dropped": CANCELLED, "cleared": CANCELLED}
        assert await stuck.cancel_orphans(session, OWNER) == 0, "safe to run again"
