"""The stragglers of the clock rule: an owner-facing day or time is the owner's, an instant kept in the analysis
or in an employer's sources carries its offset, and the coach is told whose "today" it is. The owner's zone is a made-up
one far from UTC (UTC+13 in November) so a UTC reading cannot pass by accident. Every moment is injected; no network."""

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app import db
from app.jobsearch.browser import items as bd
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import signals
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.interviews import coach_checks
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.research import role_summary
from app.jobsearch.resume import structured_profile
from app.sites import lessons as site_lessons
from app.sites.models import Lesson
from tests.builders import ONE, PATHFINDER, browser_card, browser_world, in_session, lease
from tests.jobsearch.coach_builders import context
from tests.support import OWNER, browser_recipe, clock_at, pin_clock

ZONE = "Pacific/Auckland"
#: The owner the browser world is made for.
BROWSER_OWNER = "owner"
#: 11:30 pm on 6 November in UTC is 12:30 pm on the 7th in Auckland (UTC+13): the owner's day is a day ahead.
LATE_UTC = datetime(2026, 11, 6, 23, 30, tzinfo=UTC)


def owner_zone(monkeypatch) -> None:
    monkeypatch.setattr("app.timeutil.settings.timezone", ZONE)


def test_a_lesson_retired_is_dated_by_the_owners_day_in_words(monkeypatch):
    """The Activity sentence carried the UTC date as raw ISO."""
    owner_zone(monkeypatch)
    monkeypatch.setattr(site_lessons, "datetime", clock_at(LATE_UTC))
    said = []
    monkeypatch.setattr(site_lessons, "_record_activity", lambda *a, **k: said.append(a[-1]))
    lesson = SimpleNamespace(text="The box wants a city.", retired_at=None, retired_by=None)
    site_lessons._retire(None, None, lesson, "the report")
    assert said and said[0].endswith("on November 7, 2026.")


async def test_a_runbook_learned_in_the_browser_is_dated_by_the_owners_day(js, monkeypatch):
    """The lesson's source said the UTC date."""
    pin_clock(monkeypatch, LATE_UTC, ZONE)
    await browser_world(learned=False)
    await lease("map", ONE, PATHFINDER, LATE_UTC)
    cards = [browser_card(1), browser_card(2)]
    await in_session(PATHFINDER, lambda s: bd.postings(s, BROWSER_OWNER, cards, now=LATE_UTC))
    reply = await in_session(PATHFINDER, lambda s: bd.skill(
        s, BROWSER_OWNER, ONE, browser_recipe(ONE), page_says=2, lessons=["Results show after Search is pressed."],
        now=LATE_UTC))
    assert reply["ok"]
    async with db.session_factory()() as session:
        (kept,) = (await session.scalars(select(Lesson))).all()
    assert kept.source == "learned in the browser 2026-11-07"




async def _noop():
    return None


async def test_the_years_of_experience_run_to_the_owners_month_not_the_utc_month(monkeypatch):
    """At a month's end the UTC date and the owner's date can fall in different months."""
    owner_zone(monkeypatch)
    seen = {}

    async def version_resume(*a, **k):
        return SimpleNamespace()

    async def keep(*a, **k):
        return None

    def build(_resume, _clean, today):
        seen["today"] = today
        return SimpleNamespace(fields={"name": 1})

    monkeypatch.setattr(structured_profile, "version_of", lambda role_id: 1)
    monkeypatch.setattr(structured_profile, "version_resume", version_resume)
    monkeypatch.setattr(structured_profile, "check", lambda fields, r: ({"name": "x"}, []))
    monkeypatch.setattr(structured_profile, "is_empty", lambda clean: False)
    monkeypatch.setattr(structured_profile, "build", build)
    monkeypatch.setattr(structured_profile, "keep", keep)
    month_end = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)  # 12:00 on 1 November in Auckland
    job = SimpleNamespace(role_id="x", finished_at=None, text="t", state=None, error=None, fields=None)
    await role_parse._report_profile(SimpleNamespace(flush=_noop), OWNER, job, {}, month_end)
    assert seen["today"] == date(2026, 11, 1)


def _warn(body: str) -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text=body)
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)


async def test_an_employers_events_source_is_an_instant_like_every_other_source(js, monkeypatch):
    """`sources["events"]["at"]` was the bare owner's day; every other source's `at` is an instant."""
    pin_clock(monkeypatch, LATE_UTC, ZONE)
    csv = ("id,state,company,company_canonical,company_dba,location,employees_affected,notice_date,effective_date,"
           f"notice_type,first_seen\na1,ST,Fabrikam,Fabrikam,,Oakfield,120,{LATE_UTC.year}-03-02,,Layoff,x\n")
    async with db.session_factory()() as session:
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        assert (await signals.load_warn(session, OWNER, _warn(csv)))["added"] == 1
        at = datetime.fromisoformat(fab.sources["events"]["at"])
    assert at.utcoffset() is not None and at == LATE_UTC


def test_the_analysis_time_marks_carry_their_offset_and_still_sort_as_text():
    """`summary_wanted` and `summary_claimed` were UTC with no offset."""
    earlier = role_summary._now_text(datetime(2026, 11, 6, 23, 30, tzinfo=UTC))
    later = role_summary._now_text(datetime(2026, 11, 7, 0, 0, 0, 5, tzinfo=UTC))
    assert earlier == "2026-11-06T23:30:00.000000+00:00"
    assert earlier < later and len(earlier) == len(later)
    assert role_summary._now_text(datetime(2026, 11, 7, 12, 30, tzinfo=ZoneInfo(ZONE))) == earlier


def test_the_coach_is_told_whose_today_a_source_is_dated_by():
    """A session on a machine ahead of the owner's zone dated a source with its own day and was refused
    with a sentence that did not say whose day counts."""
    ctx = context(today=date(2026, 11, 6))
    source = {"url": "https://example.test/news", "title": "News", "checked_on": "2026-11-07"}
    said = coach_checks._source_problem(source, ctx)
    assert said and "2026-11-07" in said and "the owner's today is 2026-11-06" in said
    assert "machine" in said


def test_the_coach_instructions_say_the_day_looked_is_the_owners_calendar_day():
    folder = Path(coach_checks.__file__).resolve().parents[4] / "instructions"
    assert "by the owner's calendar day" in (folder / "jobsearch.coach.instructions.md").read_text(encoding="utf-8")
