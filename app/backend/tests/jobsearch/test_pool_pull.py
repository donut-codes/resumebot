"""`pool.pull.start_next`, the pool's one choice of which job starts next: the order of its steps and what a pause
skips. Every name here is made up, and no test reaches the network."""

from sqlalchemy import func, select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.pool import pull
from app.jobsearch.research import analysis
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work.session_kinds import RESEARCHER
from tests.builders import analysis_setup
from tests.employer_builders import make_employer, make_role
from tests.jobsearch.test_rank import _jobs, _profile, _role
from tests.support import OWNER


async def _nothing_paused(reader):
    return set(), False


async def _the_researcher_paused(reader):
    return {RESEARCHER}, False


async def _start(look) -> bool:
    async with db.session_factory()() as session:
        started = await pull.start_next(session, OWNER, look)
        await session.commit()
        return started


async def _world(employer_id) -> tuple[int, bool, int]:
    """Roles with the analyst, whether the employer is with the researcher, and rank jobs waiting."""
    async with db.session_factory()() as session:
        roles = await session.scalar(select(func.count(Opportunity.id)).where(
            Opportunity.analysis_state == analysis.WAITING))
        employer = await session.get(Employer, employer_id)
        with_researcher = employer.research_state == employer_research.WAITING
    return int(roles or 0), with_researcher, len(await _jobs())


async def _research_employer_and_rank_work_waits():
    await _profile()
    await analysis_setup()
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Initech")
        session.add(make_role(employer, "Lead Programs", status=stages.READY_TO_LAUNCH))
        await session.commit()
        employer_id = employer.id
    await _role("unranked", status=stages.FIRST_IMPRESSION)
    return employer_id


async def test_calls_start_research_then_the_employer_then_the_rank_batch_then_nothing(js):
    employer_id = await _research_employer_and_rank_work_waits()
    assert await _start(_nothing_paused) is True
    assert await _world(employer_id) == (1, False, 0), "research first, one role ahead"
    assert await _start(_nothing_paused) is True
    assert await _world(employer_id) == (1, True, 0), "then the employer, while the role waits for its session"
    assert await _start(_nothing_paused) is True
    assert await _world(employer_id) == (1, True, 1), "then the rank batch"
    assert await _start(_nothing_paused) is False, "a role and an employer wait untaken, and every role is in a batch"
    assert await _world(employer_id) == (1, True, 1)


async def test_a_pause_on_the_researcher_skips_research_and_the_employer_but_not_the_rank_batch(js):
    employer_id = await _research_employer_and_rank_work_waits()
    assert await _start(_the_researcher_paused) is True
    assert await _world(employer_id) == (0, False, 1), "the rank batch started"
    assert await _start(_the_researcher_paused) is False
    assert await _world(employer_id) == (0, False, 1)
