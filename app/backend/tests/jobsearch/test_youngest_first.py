"""Youngest first in the research queues (docs/PRD.md path 40): the younger a role's posting, the sooner it is
researched, whatever a better-scored older one scores, in the three places the research is picked: a search's own finds,
the Analyse the finds mission's own run and the last group of the catch-up after an import. A posting's age is when the
app first saw it, else the employer's date, else the day the role was found; one that can't be dated goes last, and the
owner's own applications still go ahead of all of it in the catch-up. Everything is made up."""

import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import classifier
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import catch_up
from app.jobsearch.missions import core as missions
from app.jobsearch.models import JobPosting, Mission, MissionRun, Opportunity
from app.jobsearch.research import analysis
from app.timeutil import owner_today, utc_now
from tests.builders import finds_are_sourced, rank_roles, save_documents
from tests.support import EXAMPLE_SYSTEM, FIVE, ON_A_SCALE_OF_FIVE, OWNER, POSTING_WORDS, PROFILE, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")


@pytest.fixture(autouse=True)
def the_finds_are_sourced(monkeypatch):
    """What these tests are about is what happens to a search's finds once the analyst's line is planned, so each find
    is Sourced and holds its text by then (`finds_are_sourced`)."""
    finds_are_sourced(monkeypatch)



def _posting(company: str, first_seen: datetime, role: uuid.UUID, title: str = "Programs Lead") -> JobPosting:
    """The posting a role was made from, first seen at `first_seen`."""
    return JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=str(uuid.uuid4()),
                      company=company, title=title, match_key=match_key(company, title), first_seen=first_seen,
                      last_seen=first_seen, links=[], opportunity_id=role)


def _role(company: str, fit: int, *, status: str = stages.SECOND_IMPRESSION, title: str = "Programs Lead",
          origin: str = "scout", found: date | None = None, posted: date | None = None, **more) -> Opportunity:
    return Opportunity(owner=OWNER, slug=company.lower(), company=company, title=title, fit=fit, status=status,
                       origin=origin, match_key=match_key(company, title), found=found, posted=posted,
                       url=f"https://jobs.example.test/{company.lower()}", **{"description": POSTING_WORDS, **more})


async def _add(session, role: Opportunity, seen_hours_ago: float | None = None) -> Opportunity:
    """Store the role, with a posting the app first saw that many hours ago when it is given."""
    session.add(role)
    await session.flush()
    if seen_hours_ago is not None:
        session.add(_posting(role.company, utc_now() - timedelta(hours=seen_hours_ago), role.id, role.title))
        await session.flush()
    return role


# --- the picks: a search's finds and the mission's own run --------------------------------------------------------


async def test_the_picks_take_the_highest_rank_first_the_younger_posting_on_a_tie_and_an_unranked_role_last(js):
    """Replaces "youngest first": the owner (2026-10-21) put the Haiku rank first. A posting's first hours add the
    bonus (+20) and its age breaks a tie."""
    today = owner_today()
    ranks: dict = {}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE)
        for company, fit, seen, more in (
            # By the employer's date alone: today is 0 hours, yesterday 24, two days 48 (a date can't say the hour).
            ("Today", 3, None, {"posted": today, "found": today}),
            ("Yesterday", 2, None, {"posted": today - timedelta(days=1), "found": today}),
            ("TwoDaysFound", 5, None, {"found": today - timedelta(days=2)}),
            # By the hour the app first saw it: two hours, and 30 hours though it was found today.
            ("TwoHours", 1, 2.0, {"found": today}),
            ("SeenYesterday", 5, 30.0, {"found": today}),
            # The employer dated it last week though the app only just saw it: a week old, not an hour (a board read for
            # the first time shows every posting as first seen now).
            ("DatedLastWeek", 4, 1.0, {"posted": today - timedelta(days=7), "found": today}),
            ("Undated", 5, None, {}),
        ):
            added = await _add(session, _role(company, fit, **more), seen)
            if company != "Undated":
                ranks[added.id] = {"Today": 60, "Yesterday": 60, "TwoHours": 60}.get(company, 80)
        await rank_roles(session, ranks)
        await session.commit()
    async with db.session_factory()() as session:
        picked = [r.company for r in await analysis.picks(session, OWNER, None)]
        assert picked == ["SeenYesterday", "TwoDaysFound", "Today", "TwoHours", "Yesterday", "DatedLastWeek",
                          "Undated"], (
            "rank 80 plus the first hours' bonus first (the role seen 30 hours ago before the one found two days ago, "
            "though the app made it just now: its day is the day it was found, PRD 40.1), "
            "then 60 plus it by age, then the one a week old; no rank is last")
        assert [r.company for r in await analysis.picks(session, OWNER, 2)] == ["SeenYesterday", "TwoDaysFound"], (
            "the number to research a night takes the best ranked")


async def test_roles_go_by_rank_the_younger_first_on_a_tie_and_a_role_without_a_rank_after_them(js):
    """Replaces "the same age go best first": the rank orders every read (the owner, 2026-10-21)."""
    today = owner_today()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=ON_A_SCALE_OF_FIVE)
        ranks = {}
        for company, rank, days in (("Low", 40, 3), ("High", 100, 3), ("Mid", 60, 3), ("YoungLow", 20, 1)):
            ranks[(await _add(session, _role(company, 1, posted=today - timedelta(days=days), found=today))).id] = rank
        await rank_roles(session, ranks)
        await _add(session, _role("Unscored", 1, posted=today - timedelta(days=3), found=today))
        await session.commit()
    async with db.session_factory()() as session:
        assert [r.company for r in await analysis.picks(session, OWNER, None)] == [
            "High", "Mid", "YoungLow", "Low", "Unscored"], "YoungLow's 20 and the bonus tie Low's 40: the younger"


async def test_the_missions_own_run_sends_the_youngest_find_before_a_better_scored_older_one(js):
    today = owner_today()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        await missions.ensure(session, OWNER)
        analyse = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.ANALYSE))
        await missions.change(session, analyse, by="Tester", values={"target": 1})
        old = await _add(session, _role("Fabrikam", 4, posted=today - timedelta(days=10), found=today))
        yesterday = await _add(session, _role("Hooli", 5, posted=today - timedelta(days=1), found=today))
        young = await _add(session, _role("Initech", 2, found=today), seen_hours_ago=3.0)
        entry = await missions.start(session, OWNER, analyse, by="you")
        assert entry.started["roles"] == [str(young.id)] and "Initech" in entry.summary
        assert old.analysis_state is None and yesterday.analysis_state is None
        assert young.analysis_state == analysis.WAITING


# --- the catch-up after an import ---------------------------------------------------------------------------------


def _tracker_role(company: str, status: str = stages.SECOND_IMPRESSION, title: str = "Programs Lead",
                  **fields) -> Opportunity:
    return Opportunity(owner=OWNER, slug=company.lower(), company=company, title=title, status=status,
                       origin=fields.pop("origin", "tracker"), match_key=match_key(company, title), **fields)


async def _catch_up(roles: list[Opportunity], seen: dict[str, float] | None = None,
                    settings_over: dict | None = None) -> list[str]:
    """The roles already stored (as an import leaves them), then the catch-up for all the tracker's of them; returns
    their companies in the order the catch-up hands them to the analyst. `seen` is the hours since the app first saw the
    posting of the roles it names."""
    over = {"settings": {**PROFILE["settings"], **settings_over}} if settings_over else {}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, criteria={**PROFILE["criteria"], "target_roles": []}, **over)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        session.add_all(roles)
        await session.flush()
        for role in roles:
            if role.company in (seen or {}):
                session.add(_posting(role.company, utc_now() - timedelta(hours=seen[role.company]), role.id,
                                     role.title))
        await session.flush()
        await employers.settle(session, OWNER)
        await catch_up.start(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        names = {str(r.id): r.company for r in (await session.scalars(select(Opportunity))).all()}
        entry = (await session.scalars(select(MissionRun).where(MissionRun.started_by == "import"))).one()
    return [names[i] for i in catch_up.state_of(entry)["roles"]]


async def test_the_catch_up_takes_applications_first_then_ready_to_launch_then_the_youngest_of_the_rest(js):
    classifier.clear_cache()
    today = owner_today()
    old = today - timedelta(days=20)
    order = await _catch_up(
        # The owner's own applications go first, the more recent one first, and Ready to Launch after them, whatever the
        # age of the rest: these are all older than anything below, and score lower.
        [_tracker_role("Applied", "applied", fit=1, found=old),
         _tracker_role("Discussing", "active_discussion", fit=1, found=today - timedelta(days=25)),
         _tracker_role("Ready", stages.READY_TO_LAUNCH, fit=1, found=old),
         # Every other role, youngest first: by the day found (today, yesterday) or by the hour its posting was first
         # seen (3 hours; 30 hours though it was found today).
         _tracker_role("FoundToday", fit=1, found=today),
         _tracker_role("FoundYesterday", fit=3, found=today - timedelta(days=1)),
         _tracker_role("SeenThreeHours", fit=2, found=today),
         _tracker_role("SeenThirtyHours", fit=5, found=today),
         # Found ten days ago though its posting was only just read: ten days old, not an hour.
         _tracker_role("TenDaysOld", fit=4, found=today - timedelta(days=10)),
         _tracker_role("MonthOld", fit=5, found=today - timedelta(days=30)),
         # Nothing says how old it is.
         _tracker_role("Undated", fit=5)],
        seen={"SeenThreeHours": 3.0, "TenDaysOld": 1.0, "SeenThirtyHours": 30.0})
    assert order == ["Applied", "Discussing", "Ready", "FoundToday", "SeenThreeHours", "FoundYesterday",
                     "SeenThirtyHours", "TenDaysOld", "MonthOld", "Undated"]


async def test_in_the_catch_ups_last_group_age_comes_first_and_the_likeliest_first_breaks_a_tie(js):
    classifier.clear_cache()
    today = owner_today()
    history = [_tracker_role(f"Past{n}", stages.READY_TO_LAUNCH, "Director of Programs", origin="scout")
               for n in range(3)]
    history.append(_tracker_role("Passed", "withdrew", "Warehouse Associate", origin="scout",
                                 ended_reason=stages.I_PASSED))
    order = await _catch_up(
        [*history,
         _tracker_role("WarehouseToday", title="Warehouse Supervisor", fit=5, found=today),
         _tracker_role("ProgramsToday", title="Programs Director", fit=1, found=today),
         _tracker_role("WarehouseOld", title="Warehouse Supervisor", fit=5, found=today - timedelta(days=30)),
         _tracker_role("ProgramsOld", title="Programs Director", fit=1, found=today - timedelta(days=30)),
         # The likeliest of all, but older than the pair found today.
         _tracker_role("ProgramsWeek", title="Programs Director", fit=1, found=today - timedelta(days=7))],
        settings_over={"classifier_min_examples": 2})
    assert order == ["ProgramsToday", "WarehouseToday", "ProgramsWeek", "ProgramsOld", "WarehouseOld"]
