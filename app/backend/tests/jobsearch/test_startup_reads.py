"""The reads that run at start-up and in the first hour decode only what they must (the owner's ask: on SQLite with real
data no writer turn held over 1 second). Each pass reads plain columns or lets the database drop the rows that cannot
count, and keeps its Python test as the judge. Every name and number is made up."""

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import comp, freshness, scoring
from app.jobsearch.gating import age_out
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import prioritize, settle
from tests.jobsearch.test_age_out import GATES, TODAY, look, role
from tests.support import FIVE, OWNER, PROFILE, load_profile


async def test_the_age_out_reads_plain_columns_and_asks_for_the_dates_a_slice_at_a_time(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add_all([role("old-a", stages.FIRST_IMPRESSION, 20), role("old-b", stages.READY_TO_LAUNCH, 30),
                         role("young", stages.FIRST_IMPRESSION, 2)])
        await session.commit()

    monkeypatch.setattr(age_out, "SLICE", 1)
    asked: list[list] = []
    real = freshness.posted_by_role

    async def noting(session, owner, roles, *args, **kwargs):
        roles = list(roles)
        asked.append(roles)
        return await real(session, owner, roles, *args, **kwargs)

    monkeypatch.setattr(freshness, "posted_by_role", noting)
    assert await look(TODAY) == age_out.Aged(checked=3, closed=2)
    # The re-gate of a stopped role asks for its own date with the role itself; the look's calls are those with rows.
    looked = [part for part in asked if not isinstance(part[0], Opportunity)]
    assert len(looked) == 3 and all(len(part) == 1 for part in looked), "one call for each slice of one role"


async def _researched(slug: str, analysis: dict) -> None:
    async with db.session_factory()() as session:
        session.add(role(slug, stages.FIRST_IMPRESSION, 2, analysis=analysis, analysis_state=prioritize.RESEARCHED))
        await session.commit()


async def test_old_asks_decodes_only_the_research_the_database_cannot_rule_out(js, monkeypatch):
    now = {"required": [{"text": "a"}], "ask": {"method_version": comp.METHOD_VERSION}}
    await _researched("current-a", now)
    await _researched("current-b", now)
    calls: list[object] = []
    real = settle._is_old

    def noting(analysis):
        calls.append(analysis)
        return real(analysis)

    monkeypatch.setattr(settle, "_is_old", noting)
    async with db.session_factory()() as session:
        assert await settle.old_asks(session, OWNER) == 0
    assert calls == [], "research already on this method is never decoded"

    older = {"required": [{"text": "a"}], "ask": {"method_version": comp.METHOD_VERSION - 1}}
    none = {"required": [{"text": "a"}]}
    await _researched("older", older)
    await _researched("none", none)
    await _researched("nothing-required", {"ask": {"method_version": 1}})
    monkeypatch.setattr(settle, "_ask", lambda role, profile: False)  # only the choice of roles is under test
    async with db.session_factory()() as session:
        assert await settle.old_asks(session, OWNER) == 2, "the old ask and the role with none"
    assert calls and all(c in (older, none) for c in calls), "only those two were ever decoded and judged"


async def test_stale_scores_decodes_only_the_breakdowns_the_database_cannot_rule_out(js, monkeypatch):
    def breakdown(version, math):
        return {"version": version, "math": math}

    current = breakdown(1, scoring.MATH)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, settings=PROFILE["settings"])
        profile = await profile_store.current(session, OWNER)
        for slug, kept in (("cur-a", current), ("cur-b", current)):
            session.add(role(slug, stages.SECOND_IMPRESSION, 2, fit_breakdown=kept))
        await session.commit()
    calls: list[object] = []
    real = prioritize._scoreable

    def noting(kept, fit, state):
        calls.append(kept)
        return real(kept, fit, state)

    monkeypatch.setattr(prioritize, "_scoreable", noting)
    async with db.session_factory()() as session:
        assert await prioritize.stale_scores(session, OWNER, profile, 1) == 0
    assert calls == [], "scores already on this version and arithmetic are never decoded"

    async with db.session_factory()() as session:
        session.add(role("older", stages.SECOND_IMPRESSION, 2, fit_breakdown=breakdown(0, scoring.MATH)))
        await session.commit()
    async with db.session_factory()() as session:
        assert await prioritize.stale_scores(session, OWNER, profile, 1) == 1
    assert calls == [breakdown(0, scoring.MATH)], "only the stale one was judged"
