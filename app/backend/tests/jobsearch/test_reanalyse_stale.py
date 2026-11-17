"""A role researched before its posting's text was saved is researched again (`analysis.requeue_if_stale`, called where
a text is saved), and `requeue.reanalyse_stale` does the same for the roles already in that state. Every role is made
up."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis, requeue
from app.models.work import TrailEvent
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

ANALYSED = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
LATER = ANALYSED + timedelta(days=3)
EARLIER = ANALYSED - timedelta(days=3)
OLD = {"required": [{"item": "Five years of programs", "class": "P", "by": "analyst"}], "preferred": [],
       "notes": "kept earlier", "at": "2026-09-01T00:00:00+00:00"}
LINK = "https://boards.feedone.test/acme/jobs/4012"


async def _make(slug, *, text_at, state=analysis.DONE, text="The posting's words.") -> str:
    async with db.session_factory()() as session:
        role = Opportunity(
            owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", fit=3, origin="scout",
            match_key=f"co {slug}|role {slug}", found=date.today() - timedelta(days=30),
                status=stages.SECOND_IMPRESSION,
            analysis_state=state, analysis=dict(OLD), analysed_at=ANALYSED if state == analysis.DONE else None,
            description=text if text_at else None, description_at=text_at)
        session.add(role)
        await session.commit()
        return role.id


async def _row(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _lines(role_id) -> list[TrailEvent]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == role_id, TrailEvent.event == "research_requeued"))).all())


FILLER = " The role leads the work, sets priorities, manages the team and reports to the executive team." * 4
WHOLE_TEXT = "Whole text of the posting." + FILLER


async def test_a_text_saved_after_the_research_queues_the_role_again(js):
    role_id = await _make("a", text_at=None)
    async with db.session_factory()() as session:
        employer = Employer(owner=OWNER, key="acme", name="Acme", ats=EXAMPLE_SYSTEM, board={"slug": "acme"})
        session.add(employer)
        await session.flush()
        session.add(JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id="4012", company="Acme",
                               title="Role a", match_key="co a|role a", url=LINK, first_seen=LATER, last_seen=LATER,
                               links=[], opportunity_id=role_id, text=WHOLE_TEXT, detail_at=LATER))
        await session.commit()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        assert await posting_text.take(session, role, posting_text.FromPostings()) == posting_text.HELD
        await session.commit()
    role = await _row(role_id)
    assert role.description == WHOLE_TEXT
    assert role.analysis_state == analysis.WAITING and role.analysis_instance_id is not None
    assert role.analysis == OLD, "the old research stays until the new one replaces it"
    assert len(await _lines(role_id)) == 1


async def test_a_text_that_is_not_newer_than_the_research_changes_nothing(js):
    role_id = await _make("a", text_at=EARLIER)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        assert await analysis.requeue_if_stale(session, role) is False
        await session.commit()
    assert (await _row(role_id)).analysis_state == analysis.DONE
    assert await _lines(role_id) == []


async def test_reanalyse_stale_queues_only_the_stale_roles_and_a_dry_run_changes_nothing(js):
    stale = await _make("stale", text_at=LATER)
    fresh = await _make("fresh", text_at=EARLIER)
    untexted = await _make("none", text_at=None)
    waiting = await _make("waiting", text_at=LATER, state=analysis.WAITING)

    async with db.session_factory()() as session:
        assert await requeue.reanalyse_stale(session, OWNER, dry_run=True) == 1
    assert (await _row(stale)).analysis_state == analysis.DONE

    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        assert await requeue.reanalyse_stale(session, OWNER) == 1
        await session.commit()
    assert (await _row(stale)).analysis_state == analysis.WAITING
    for role_id, state in ((fresh, analysis.DONE), (untexted, analysis.DONE), (waiting, analysis.WAITING)):
        assert (await _row(role_id)).analysis_state == state

    async with db.session_factory()() as session:
        assert await requeue.reanalyse_stale(session, OWNER) == 0, "safe to run twice"
