"""Two things are scheduled, everything downstream runs on need (the owner, 2026-10-22). Scheduled, so kept to the
owner's off hours: building site recipes, and the lookups that bring new postings in (searches, board and recipe reads).
On need, at any hour: cleansing, detail-text reads, Haiku ranking, deck employer research, Sonnet deck reads. Peak hours
(`Settings.avoid_peak`, "busy hours"), when on, hold every model session and so the downstream ones; code-only work goes
on. Everything here is made up."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.models import Opportunity, SearchRun
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.research import analysis
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from tests.builders import analysis_setup
from tests.jobsearch.test_pool_queue import _asks, _count
from tests.support import OWNER


@pytest.fixture(autouse=True)
def deck_research_on(monkeypatch):
    monkeypatch.setattr(employer_research, "DECK_RESEARCH", True)


def _by_day(monkeypatch, *, peak: bool = False) -> None:
    """Outside the owner's off hours, and inside Claude's peak hours when `peak`."""
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    monkeypatch.setattr(night, "in_peak", lambda s, now=None: peak)


async def test_by_day_the_deck_is_read_on_need_and_the_search_waits_for_the_off_hours(js, monkeypatch):
    await analysis_setup()
    _by_day(monkeypatch)
    assert await _asks() == [session_kinds.ANALYSIS], "downstream: a deck read starts at any hour"
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            role.analysis_state = analysis.DONE  # nothing is left to read
        await session.commit()
    assert await _asks(2) == [None, None], "the scheduled lookup (a search) is not started by day"
    assert await _count(SearchRun) == 0
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: True)
    assert await _asks() == [session_kinds.SEARCH], "in the off hours the scheduled lookup starts"


async def test_by_day_the_decks_employer_is_researched_on_need_before_its_read(js, monkeypatch):
    _by_day(monkeypatch)
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        for role in (await session.scalars(select(Opportunity))).all():
            role.employer_id = employer.id if role.id == ids["Fabrikam"] else role.employer_id
        await session.commit()
        assert await pipeline.employers_for_deck(session, OWNER) >= 1
        await session.commit()
    assert (await _asks())[0] == session_kinds.RESEARCHER, "the employer first, at any hour"


async def test_in_peak_hours_no_model_session_starts_and_off_peak_it_does(js, monkeypatch):
    await analysis_setup()
    _by_day(monkeypatch, peak=True)
    assert await _asks(2) == [None, None], "peak hours hold the downstream model work too"
    assert await _count(Opportunity, Opportunity.analysis_state == "waiting") == 0, "and queue none"
    _by_day(monkeypatch, peak=False)
    assert await _asks() == [session_kinds.ANALYSIS]


async def test_peak_hours_hold_nothing_when_the_owner_turned_them_off_and_the_setting_is_avoid_peak(js, monkeypatch):
    from app.jobsearch.profile.schema import Settings

    assert Settings().avoid_peak is True, "the existing setting: on by default"
    assert night.in_peak(Settings(avoid_peak=False)) is False, "off: never peak, whatever the hour"
    when = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)  # a Wednesday morning in the Pacific window
    assert night.in_peak(Settings(avoid_peak=True), when) is True
    assert night.in_peak(Settings(avoid_peak=True), when + timedelta(hours=8)) is False
    await analysis_setup()
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    real = night.in_peak
    monkeypatch.setattr(night, "in_peak", lambda s, now=None: real(Settings(avoid_peak=False), when))
    assert await _asks() == [session_kinds.ANALYSIS]
