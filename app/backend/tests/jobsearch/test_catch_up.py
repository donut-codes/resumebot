"""The catch-up after a tracker import (docs/PRD.md 2.9): an import that leaves roles to research starts it; it
researches every role the tracker holds that is in play and was never researched, applications first, one at a time and
without the nightly number; it holds in Claude's peak hours and at its usage limit and picks up
again after; it looks for the employers' job systems; and a normal night is unchanged. Everything is made up."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from process_engine.models import ProcInstance
from sqlalchemy import func, select, update
from sqlalchemy.orm.exc import StaleDataError

from app import db
from app.agents import roster
from app.jobsearch import cli, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import resolve
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.fit import classifier
from app.jobsearch.imports import tracker
from app.jobsearch.missions import catch_up
from app.jobsearch.missions import core as missions
from app.jobsearch.missions.catch_up import last_dated
from app.jobsearch.models import Employer, Mission, MissionRun, Opportunity, ParseJob, RoleTagJob, SearchRun
from app.jobsearch.pool import core as pool
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work import availability, housekeeping, session_kinds, tagging
from app.jobsearch.work import hours as night
from app.settings import settings
from app.timeutil import owner_today, owner_zone
from tests.builders import analysis_setup, findings, save_documents, settle_housekeeping, submit_findings
from tests.jobsearch.example_world import PROBING_SYSTEM, example_world, probe_answer, probe_url  # noqa: F401
from tests.support import (
    FIVE,
    ON_A_SCALE_OF_FIVE,
    OWNER,
    POSTING_WORDS,
    PROFILE,
    answered,
    load_profile,
    tracker_row,
    tracker_workbook,
)

pytestmark = pytest.mark.usefixtures("example_world")

#: When the two applications were made, as the tracker's notes write a date. The tracker's own figure for a fit is kept
#: off the role (`tracker.py`: it is on a scale the app does not use), so roles that tie on everything else would go in
#: no order a test could name: the more recent application goes first, by these dates.
APPLIED_ON, TALKING_SINCE = owner_today() - timedelta(days=2), owner_today() - timedelta(days=9)
#: Four roles in play, one closed, one rejected, one screened out: in the tracker's own words for the statuses.
ROWS = [
    tracker_row("Northwind", "Programs Lead", status="second_impression", fit=4,
    url="https://www.board.test/jobs/view/1"),
    tracker_row("Wingtip", "Programs Lead", status="ready", fit=2, url="https://www.board.test/jobs/view/2"),
    tracker_row("Initech", "Programs Lead", status="applied", fit=5, url="https://www.board.test/jobs/view/3",
                notes=f"[{APPLIED_ON}] applied"),
    tracker_row("Umbrella", "Programs Lead", status="interviewing", fit=1, url="https://www.board.test/jobs/view/4",
                notes=f"[{TALKING_SINCE}] interview asked for"),
    tracker_row("Hooli", "Programs Lead", status="dead", fit=5),
    tracker_row("Soylent", "Programs Lead", status="rejected", fit=5),
    tracker_row("Vandelay", "Programs Lead", status="screened_out", fit=5),
]
#: The roles of ROWS the catch-up researches, in its order: Applied and Active Discussion first (the more recent
#: application first, by the date in its notes), then Ready to Launch, then every other role.
IN_PLAY = ["Initech", "Umbrella", "Wingtip", "Northwind"]
#: The same rows, each role's link on a site of its own (ROWS' links are all Exboard's, and research is never on one
#: site in two sessions): for the tests of more than one analyst.
ELSEWHERE = [{**row, "url": f"https://careers.{row['company'].lower()}.example.test/jobs/1"} for row in ROWS]


class Clock:
    """The catch-up's clock, moved by the test."""

    def __init__(self):
        self.now = datetime.now(UTC)

    def after(self, **delta) -> datetime:
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """The clock of the catch-up and of everything it asks: the usage limit it reads lives in `availability`, and the
    words that say when it lifts are `hours`'."""
    clock = Clock()
    for module in (catch_up, availability, night):
        monkeypatch.setattr(module, "utc_now", lambda: clock.now)
    return clock


def _internet(answers: dict[str, object], seen: list[str]) -> Fetcher:
    """Other sites, made up: a body for each address that starts as a key; every other answers 404."""
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        for prefix, body in answers.items():
            if url.startswith(prefix):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={})
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)


@pytest.fixture(autouse=True)
def the_check_on_entering_ready_is_left_to_its_own_tests(monkeypatch):
    """The app's own turn checks a role that came into Ready to Launch (docs/PRD.md 24.16, `go_checks.check_entered`),
    and the made-up internet below answers 404 to every posting, which ends the imported Ready role as gone. These
    tests are of the order the catch-up researches in and of the questions that do no network work, so that check is
    not part of them: `test_ready_check.py` is its own."""
    from app.jobsearch.browser import go_checks

    async def not_made(session, owner, now=None) -> int:
        return 0

    monkeypatch.setattr(go_checks, "check_entered", not_made)


@pytest.fixture(autouse=True)
def other_sites_say_nothing(monkeypatch):
    """No test reaches the network: the employers' job systems are looked for against a made-up internet that knows
    nothing, unless a test puts its own answers in."""
    seen: list[str] = []
    monkeypatch.setattr(polite, "make", lambda: _internet({}, seen))
    return seen


async def _world(rows=ROWS, *, target_roles: bool = False, settings_over: dict | None = None, **parts):
    """The made-up profile and documents, then an import of `rows` that starts the catch-up. With no target roles the
    research is handed out at once; with them the roles are matched first."""
    criteria = PROFILE["criteria"] if target_roles else {**PROFILE["criteria"], "target_roles": []}
    over = {"settings": {**PROFILE["settings"], **settings_over}} if settings_over else {}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=ON_A_SCALE_OF_FIVE, criteria=criteria, **over, **parts)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        await tracker.import_tracker(session, OWNER, tracker_workbook(rows))
        # An import brings no text of a posting and no source, and a role with neither is never researched (docs/PRD.md
        # 39.30, postings/sourced.py): these tests are of the order the catch-up hands roles out in, so each role is
        # Sourced and holds its text.
        await session.execute(update(Opportunity).values(
            description=POSTING_WORDS, source_url="https://careers.example.test/jobs/1", source_kind="employer",
            sourced_at=datetime.now(UTC)))
        await employers.settle(session, OWNER)
        started = await catch_up.start(session, OWNER)
        await session.commit()
    return started


async def _run() -> MissionRun:
    async with db.session_factory()() as session:
        return (await session.scalars(select(MissionRun).where(MissionRun.started_by == "import"))).one()


async def _runs() -> list[MissionRun]:
    """Every catch-up's run, the first started first."""
    async with db.session_factory()() as session:
        return list((await session.scalars(select(MissionRun).where(MissionRun.started_by == "import")
                                           .order_by(MissionRun.started_at))).all())


async def _state() -> dict:
    return catch_up.state_of(await _run())


async def _states() -> dict[str, str | None]:
    async with db.session_factory()() as session:
        return {r.company: r.analysis_state for r in (await session.scalars(select(Opportunity))).all()}


async def _with_the_analyst() -> list[str]:
    return sorted(c for c, s in (await _states()).items() if s == analysis.WAITING)


async def _id(company: str) -> str:
    async with db.session_factory()() as session:
        return str((await session.scalars(select(Opportunity.id).where(Opportunity.company == company))).one())


async def _status(company: str) -> str:
    async with db.session_factory()() as session:
        return (await session.scalars(select(Opportunity.status).where(Opportunity.company == company))).one()


async def _advance(heavy: bool = True, now: datetime | None = None) -> list[tuple[str, str]]:
    async with db.session_factory()() as session:
        knocks = await catch_up.advance(session, OWNER, heavy=heavy, now=now)
        await session.commit()
    return knocks


async def _researches(**over) -> str:
    """The analyst finishes the role waiting for it: its findings are handed in. Returns the company."""
    async with db.session_factory()() as session:
        role = await analysis.waiting(session, OWNER)
        assert role is not None, "no role is with the analyst"
    await submit_findings(findings(role.id, fit_breakdown=answered(), **over))
    return role.company


async def _the_sweep_fails_it() -> None:
    """A session took the role and never sent it: after the stuck time the sweep fails it."""
    async with db.session_factory()() as session:
        assert await analysis.claim_next(session, OWNER) is not None, "a role is with the analyst"
        assert await analysis.settle_stuck(session, OWNER, now=datetime.now(UTC) + timedelta(hours=3)) == 1
        await session.commit()


async def _the_runner_asks() -> str | None:
    """What the runner is told is waiting when it asks for work (`GET /agent/work`), after the app's own pass has run
    (`housekeeping.settle`: it hands the catch-up's next role on, which an ask no longer does)."""
    async with db.session_factory()() as session:
        await housekeeping.settle(session, OWNER)
        asked = await session_kinds.next_kind(session, OWNER)
        await session.commit()
    return asked


async def _summary() -> str:
    return (await _run()).summary


# --- an import starts it ------------------------------------------------------------------------------------------


async def test_an_import_that_stores_roles_starts_the_catch_up_and_says_so(js, tmp_path, monkeypatch, knocks, capsys):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE)
        await save_documents(session)
        await session.commit()
    path = tmp_path / "tracker.xlsx"
    path.write_bytes(tracker_workbook(ROWS).getvalue())

    assert await cli._import_tracker(SimpleNamespace(xlsx=str(path))) == 0
    said = capsys.readouterr().out
    assert "tracker import: rows 7, added 7" in said
    assert ("catch-up: 4 of the 7 roles the tracker holds to research, one after another, once they are matched to "
            "your target roles") in said

    run = await _run()
    async with db.session_factory()() as session:
        mission = await session.get(Mission, run.mission_id)
    assert mission.key == "analyse_finds" and run.outcome == "running" and run.started_by == "import"
    assert run.started["target"] == 4 and run.started["done"] == 0 and len(run.started["instances"]) == 1
    # The roles are matched to a target role first, and the scout is woken for that: the title session is the runner's.
    async with db.session_factory()() as session:
        job = await tagging.open_job(session, OWNER)
    assert job is not None and (str(job.id), "tag_roles") in knocks
    assert await _with_the_analyst() == [], "nothing goes to the analyst before the roles are matched"

    # Run again while it runs: the same tracker brings in nothing, and the roles and employers it holds are that
    # catch-up's, so a second starts nothing.
    assert await cli._import_tracker(SimpleNamespace(xlsx=str(path))) == 0
    again = capsys.readouterr().out
    assert "added 0" in again and "catch-up" not in again
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(MissionRun).where(MissionRun.started_by == "import"))).all()) == 1


async def test_an_import_that_cannot_start_the_catch_up_saves_nothing(js, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "owner", OWNER)

    async def cannot(*_a, **_k):
        raise catch_up.CatchUpError("the made-up reason")
    monkeypatch.setattr(catch_up, "start", cannot)
    path = tmp_path / "tracker.xlsx"
    path.write_bytes(tracker_workbook(ROWS).getvalue())
    assert await cli._import_tracker(SimpleNamespace(xlsx=str(path))) == 1
    assert "error: the made-up reason" in capsys.readouterr().err
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Opportunity))).all() == [], "the import was not saved"


# --- which roles ---------------------------------------------------------------------------------------------------


async def test_the_roles_in_play_that_were_never_researched_are_picked_best_first(js):
    started = await _world()
    state = await _state()
    async with db.session_factory()() as session:
        picked = [await session.get(Opportunity, UUID(i)) for i in state["roles"]]
    assert [r.company for r in picked] == IN_PLAY
    assert {r.status for r in picked} <= set(stages.OPEN)
    assert {await _status("Soylent"), await _status("Hooli"), await _status("Vandelay")} == {
        stages.REJECTED, stages.GONE_QUIET, stages.GATED_OUT}, "an ended or screened-out role is left out"
    assert state["left_out"] == 3 and state["tracker_count"] == 7
    assert started.entry.started["target"] == 4


async def test_an_imported_role_before_the_deck_is_not_researched_at_once_but_goes_through_the_rank_and_the_deck(js):
    started = await _world([tracker_row("Newcomer", "Programs Lead", status="new"),
                            tracker_row("Applied", "Programs Lead", status="applied")])
    assert await _status("Newcomer") == stages.LANDING_ZONE
    assert (await _state())["roles"] == [await _id("Applied")], "Sonnet reads the top N, not every imported role"
    assert await _with_the_analyst() == ["Applied"] and started.entry.started["target"] == 1


def _tracker_role(company: str, status: str = stages.SECOND_IMPRESSION, title: str = "Programs Lead",
                  **fields) -> Opportunity:
    return Opportunity(
        description=POSTING_WORDS,
        owner=OWNER, slug=company.lower(), company=company, title=title, status=status,
                       origin=fields.pop("origin", "tracker"), match_key=f"{company.lower()}|{title.lower()}", **fields)


async def _start_with(roles: list[Opportunity], settings_over: dict | None = None):
    """The roles already stored (as an import leaves them), then the catch-up for all the tracker's of them."""
    over = {"settings": {**PROFILE["settings"], **settings_over}} if settings_over else {}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=ON_A_SCALE_OF_FIVE,
                           criteria={**PROFILE["criteria"], "target_roles": []}, **over)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        session.add_all(roles)
        await session.flush()
        await employers.settle(session, OWNER)
        started = await catch_up.start(session, OWNER)
        await session.commit()
    return started


async def _order() -> list[str]:
    async with db.session_factory()() as session:
        names = {str(r.id): r.company for r in (await session.scalars(select(Opportunity))).all()}
    return [names[i] for i in (await _state())["roles"]]


async def test_applications_go_first_the_most_recent_first_then_ready_to_launch_then_the_rest(js):
    today = owner_today()

    def day(ago: int) -> str:
        return (today - timedelta(days=ago)).isoformat()
    await _start_with([
        # Applied and Active Discussion are one group, the most recent application first: the latest date in the notes
        # (a date still ahead is a deadline, not an application), else the day the role was found, else no date.
        _tracker_role("Undated", "applied", fit=5),
        _tracker_role("Older", "applied", fit=5, notes=f"[{day(6)}] applied. Closes {day(-10)}; applied {day(5)}"),
        _tracker_role("TodayLowFit", "applied", fit=1, notes=f"applied {day(0)}"),
        _tracker_role("FoundTwoDaysAgo", "applied", fit=5, found=today - timedelta(days=2)),
        _tracker_role("Yesterday", "active_discussion", fit=5, notes=f"[email-sync {day(1)}] interview request"),
        _tracker_role("Today", "applied", fit=3, notes=f"[{day(0)}] applied through the employer's site"),
        # Then Ready to Launch, whatever its notes say; then every other role in play.
        _tracker_role("Ready", stages.READY_TO_LAUNCH, fit=5, notes=f"[{day(0)}] packet done"),
        _tracker_role("Drafted", stages.READY_TO_LAUNCH, fit=4),
        _tracker_role("Fresh", stages.SECOND_IMPRESSION, fit=5),
        # Not in play: never researched.
        _tracker_role("Rejected", "rejected", fit=5, notes=f"[{day(0)}] no"),
    ])
    assert await _order() == ["Today", "TodayLowFit", "Yesterday", "FoundTwoDaysAgo", "Older", "Undated", "Ready",
                              "Drafted", "Fresh"], "the same day: the higher fit; Ready to Launch before the rest"
    assert await _with_the_analyst() == ["Today"], "the most recent application is the first with the analyst"


async def test_the_rest_go_likeliest_first_by_the_classifier_where_the_owner_has_history_enough_for_one(js):
    classifier.clear_cache()
    history = [_tracker_role(f"Past{n}", stages.READY_TO_LAUNCH, "Director of Programs", origin="scout")
               for n in range(3)]
    history.append(_tracker_role("Passed", "withdrew", "Warehouse Associate", origin="scout",
                                 ended_reason=stages.I_PASSED))
    roles = [_tracker_role("Warehouse", title="Warehouse Supervisor", fit=5),
             _tracker_role("Programs", title="Programs Director", fit=1)]
    await _start_with([*history, *roles], settings_over={"classifier_min_examples": 2})
    assert await _order() == ["Programs", "Warehouse"], "the likelier kind of job first, though its fit is lower"


async def test_the_rest_go_by_the_queue_rule_when_the_owner_has_too_little_history_for_a_classifier(js):
    classifier.clear_cache()
    await _start_with([_tracker_role("Warehouse", title="Warehouse Supervisor", fit=5),
                       _tracker_role("Programs", title="Programs Director", fit=1)])
    assert await _order() == ["Warehouse", "Programs"], "no model yet: the higher fit first"


def test_how_recent_an_application_is_is_read_from_the_dates_in_its_notes_and_never_from_one_still_ahead():
    today = date(2026, 10, 13)

    def role(notes=None, found=None):
        return Opportunity(notes=notes, found=found)
    many = "[2026-10-05] applied; [email-sync 2026-10-09] application received"
    assert last_dated(role(many), today) == date(2026, 10, 9)
    assert last_dated(role("applied 2026-10-13"), today) == today, "today counts"
    assert last_dated(role("closes 2026-11-30; applied 2026-10-02"), today) == date(2026, 10, 2), "ahead is no date"
    assert last_dated(role("req R2026-10-14, REQ-2026-10-12-B, job 2026-10-1234"), today) is None, "not on its own"
    assert last_dated(role("applied 2026-02-31"), today) is None, "a day that is not one"
    assert last_dated(role("applied 2026-10-02", found=date(2026, 9, 1)), today) == date(2026, 10, 2)
    assert last_dated(role(None, found=date(2026, 9, 1)), today) == date(2026, 9, 1), "else the day it was found"
    assert last_dated(role(None, found=date(2026, 12, 1)), today) is None
    assert last_dated(role(), today) is None


async def test_a_role_already_researched_or_with_the_analyst_is_not_picked_again_but_one_that_failed_is(js):
    started = await _start_with([
        _tracker_role("Researched", analysis_state=analysis.DONE),
        _tracker_role("Waiting", analysis_state=analysis.WAITING),
        _tracker_role("Fresh"),
        _tracker_role("Failed", analysis_state=analysis.FAILED),
        _tracker_role("Scouted", origin="scout"),  # a search found it: the nightly mission's, not the tracker's
    ])
    state = catch_up.state_of(started.entry)
    assert sorted(await _order()) == ["Failed", "Fresh"], "not yet researched: never asked for, or asked and failed"
    assert state["tracker_count"] == 4 and state["left_out"] == 2


async def test_an_import_that_adds_nothing_still_picks_up_what_an_earlier_import_left_unresearched(
        js, tmp_path, monkeypatch, capsys, knocks):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, criteria={**PROFILE["criteria"], "target_roles": []})
        await save_documents(session)
        # An earlier import stored the roles and no catch-up researched them (it ran before the catch-up existed).
        await tracker.import_tracker(session, OWNER, tracker_workbook(ROWS))
        await session.execute(update(Opportunity).values(  # Sourced, with their text: only then are they researched
            description=POSTING_WORDS, source_url="https://careers.example.test/jobs/1", source_kind="employer",
            sourced_at=datetime.now(UTC)))
        await employers.settle(session, OWNER)
        await session.commit()
    path = tmp_path / "tracker.xlsx"
    path.write_bytes(tracker_workbook(ROWS).getvalue())

    assert await cli._import_tracker(SimpleNamespace(xlsx=str(path))) == 0
    said = capsys.readouterr().out
    assert "added 0" in said
    assert "catch-up: 4 of the 7 roles the tracker holds to research, one after another, straight away" in said
    assert await _with_the_analyst() == ["Initech"], "an application goes first"
    assert (await _id("Initech"), "analysis_asked") in knocks


async def test_a_second_catch_up_takes_only_the_roles_and_employers_a_running_one_has_not_got(js):
    await _world()
    first = await _run()
    async with db.session_factory()() as session:
        session.add(_tracker_role("Newcomer", stages.SECOND_IMPRESSION, fit=3))
        await session.flush()
        await employers.settle(session, OWNER)
        started = await catch_up.start(session, OWNER)
        await session.commit()
        newcomer = (await session.scalars(select(Employer).where(Employer.name == "Newcomer"))).one()
    second = catch_up.state_of(started.entry)
    assert second["roles"] == [await _id("Newcomer")]
    assert second["employers"] == [str(newcomer.id)] and second["tracker_count"] == 8
    assert len(catch_up.state_of(first)["roles"]) == 4, "the first still has its own"
    assert set(second["roles"]).isdisjoint(catch_up.state_of(first)["roles"])


async def test_an_import_that_leaves_no_role_to_research_and_no_employer_to_look_at_starts_nothing(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, criteria={**PROFILE["criteria"], "target_roles": []})
        await save_documents(session)
        session.add_all([_tracker_role("Done", analysis_state=analysis.DONE), _tracker_role("Closed", "rejected")])
        await session.flush()
        await employers.settle(session, OWNER)
        for employer in (await session.scalars(select(Employer))).all():
            employer.gated = True  # never asked about
        assert await catch_up.start(session, OWNER) is None
        await session.commit()
        assert (await session.scalars(select(MissionRun))).all() == []


async def test_an_import_that_brings_in_only_closed_roles_has_nothing_to_research_and_ends_after_the_job_systems(js):
    started = await _world([tracker_row("Hooli", "Programs Lead", status="dead"),
                            tracker_row("Soylent", "Programs Lead", status="rejected")])
    assert "none of the 2 roles the tracker holds is left to research" in started.words
    assert (await _summary()).startswith("None of the 2 roles the tracker holds is left to research")
    assert await _advance(heavy=False) == [] and (await _run()).outcome == "running", "the boards wait for a pass"
    await _advance()
    run = await _run()
    assert run.outcome == "done" and run.started["target"] == 0


# --- no number; one at a time -------------------------------------------------------------------------------------


async def test_every_role_is_researched_one_at_a_time_with_no_nightly_number(js):
    # The nightly mission researches 2 a night, the best of the finds; the catch-up ignores that number.
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, criteria={**PROFILE["criteria"], "target_roles": []})
        await missions.ensure(session, OWNER)
        analyse = (await session.scalars(select(Mission).where(Mission.key == missions.ANALYSE))).one()
        await missions.change(session, analyse, by="Tester", values={"target": 2})
        await session.commit()
    await _world()
    seen = []
    while await _with_the_analyst():
        assert len(await _with_the_analyst()) == 1, "never all at once: a role waiting over the stuck time is failed"
        seen.append(await _researches())
        await _the_runner_asks()  # the runner asks what waits: the next role goes to the analyst at once
    assert seen == IN_PLAY, "all four, best first, though the night's number is 2"
    states = await _states()
    assert [states[c] for c in IN_PLAY] == [analysis.DONE] * 4
    assert states["Hooli"] is None and states["Soylent"] is None and states["Vandelay"] is None
    assert (await _summary()).startswith("Researched 4 of 4 roles.")
    await _advance()  # the schedule pass looks for the job systems and closes the run
    run = await _run()
    assert run.outcome == "done" and run.ended_at is not None
    assert run.summary.startswith("Researched 4 of 4 roles. Found the job system of")


async def test_a_role_researched_at_once_keeps_its_stage_and_one_on_deck_goes_up_never_closed_on_a_score(js):
    await _world()
    for _ in range(4):
        await _researches()
        await _the_runner_asks()
    async with db.session_factory()() as session:
        rows = {r.company: r for r in (await session.scalars(select(Opportunity))).all()}
    assert rows["Northwind"].status == stages.READY_TO_LAUNCH, "on deck, researched, room: it goes up"
    assert rows["Initech"].status == stages.APPLIED
    assert rows["Wingtip"].status == stages.READY_TO_LAUNCH
    assert rows["Umbrella"].status == stages.ACTIVE_DISCUSSION
    for role in rows.values():
        if role.origin == "tracker" and role.status in stages.OPEN:
            assert role.analysis_state == analysis.DONE and role.analysis["pay"] and role.fit_breakdown["asks"]


# --- matching the roles to a target role --------------------------------------------------------------------------


async def test_the_roles_are_matched_to_a_target_role_before_any_is_handed_out(js):
    started = await _world(target_roles=True)
    assert await _with_the_analyst() == []
    assert any(reason == "tag_roles" for _id, reason in started.knocks)
    assert "Matching 4 roles to your target roles" in await _summary()
    assert await _advance() == [] and await _with_the_analyst() == [], "still waiting for the title session"
    # The title session answers: three roles match, one matches none.
    async with db.session_factory()() as session:
        job = await tagging.open_job(session, OWNER)
        roles = {r.company: r for r in await tagging.waiting(session, job)}
        assert set(roles) == {"Northwind", "Wingtip", "Initech", "Umbrella", "Hooli", "Soylent"}, (
            "the job gives a target role to every role that has none, those that ended too")
        for company in ("Northwind", "Wingtip", "Initech"):
            tagging.tag(job, roles[company], "builders")
        tagging.tag(job, roles["Umbrella"], None)
        await session.commit()
    # The catch-up goes on as soon as its own roles are matched, though the job is still open for the closed ones.
    knocked = await _advance(heavy=False)
    assert await _with_the_analyst() == ["Initech"]
    assert knocked and knocked[0][1] == "analysis_asked"
    assert "Researched 0 of 4 roles." in await _summary()
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Opportunity.target_role).where(Opportunity.company == "Initech"))).one() \
            == "builders"
        assert [j.status for j in (await session.scalars(select(RoleTagJob))).all()] == ["open"]


async def test_with_no_target_role_set_the_research_starts_at_once(js):
    started = await _world(target_roles=False)
    assert await _with_the_analyst() == ["Initech"]
    assert "analysis_asked" in [reason for _id, reason in started.knocks]
    async with db.session_factory()() as session:
        assert await tagging.open_job(session, OWNER) is None


# --- Claude's peak hours --------------------------------------------------------------------------------------------


async def test_peak_hours_hold_the_catch_up_and_it_goes_on_when_they_end(js):
    # The owner avoids a window that covers every weekday, so the conftest's off-peak instant (a Friday) is inside it.
    started = await _world(settings_over={"avoid_peak": True, "peak_start": "00:00", "peak_end": "23:59",
                                          "peak_zone": "UTC"})
    assert await _with_the_analyst() == [], "nothing is handed to the analyst in the peak hours"
    assert started.entry.started["catch_up"]["hold"]["why"] == "peak"
    assert "Paused: Claude's peak hours" in await _summary()
    assert await _the_runner_asks() is None
    assert await _advance() == [] and await _with_the_analyst() == []
    async with db.session_factory()() as session:  # the owner stops avoiding them
        await profile_store.save(session, OWNER, "settings", {**PROFILE["settings"], "avoid_peak": False}, by="test")
        await session.commit()
    knocked = await _advance()
    assert await _with_the_analyst() == ["Initech"] and knocked[0][1] == "analysis_asked"
    assert "Paused" not in await _summary()


async def test_a_paused_scout_holds_the_catch_up_too(js):
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).paused = True
        await session.commit()
    await _world()
    assert await _with_the_analyst() == [] and "Paused: the scout is paused" in await _summary()
    async with db.session_factory()() as session:
        (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).paused = False
        await session.commit()
    await _advance()
    assert await _with_the_analyst() == ["Initech"]


# --- Claude's usage limit -------------------------------------------------------------------------------------------


def _ended_at_the_limit(lifts: datetime | None) -> dict:
    return {"session_id": "analyst-1a2b3c", "state": "ended",
            "limit": {"resets_at": lifts.isoformat() if lifts else None, "text": "You've hit your session limit"}}


async def test_the_usage_limit_pauses_the_catch_up_and_it_picks_up_in_the_next_window(
        js, client, scout_token, clock):
    await _world()
    first = (await _state())["inflight"][0]
    assert await _with_the_analyst() == ["Initech"]
    async with db.session_factory()() as session:
        old_instance = (await session.get(Opportunity, UUID(first))).analysis_instance_id

    lifts = clock.now + timedelta(hours=3)
    sent = await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(lifts), headers=scout_token)
    assert sent.status_code == 200

    # The research is taken back, so the sweep cannot fail it while the limit holds; the run says why it waits.
    assert await _with_the_analyst() == []
    async with db.session_factory()() as session:
        assert (await session.get(ProcInstance, old_instance)).status == "cancelled"
        assert await analysis.settle_stuck(session, OWNER, now=datetime.now(UTC) + timedelta(hours=5)) == 0
        assert (await session.get(Opportunity, UUID(first))).analysis_state is None
    assert "Paused: Claude's usage limit, until" in await _summary()
    state = await _state()
    assert state["queue"][0] == first and state["tries"][first] == 0, "first in line again; the try does not count"
    # Nothing of any kind starts before it lifts, so no session is begun only to fail.
    assert await _the_runner_asks() is None
    assert await _advance() == [] and await _with_the_analyst() == []
    clock.after(hours=2, minutes=59)
    assert await _advance() == [] and await _with_the_analyst() == [], "a minute early"

    # The next window: the same role goes to the analyst again, and the scout is woken for it.
    clock.after(minutes=2)
    woke = await _advance()
    assert await _with_the_analyst() == ["Initech"] and (first, "analysis_asked") in woke
    assert await _the_runner_asks() == "analysis"
    assert "Paused" not in await _summary()
    assert (await _state())["tries"][first] == 1


async def test_a_usage_limit_with_no_time_is_tried_again_after_half_an_hour_and_a_far_one_is_not_believed(
        js, client, scout_token, clock):
    await _world()
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(None), headers=scout_token)
    assert await _with_the_analyst() == [] and await _the_runner_asks() is None
    clock.after(minutes=29)
    await _advance()
    assert await _with_the_analyst() == []
    clock.after(minutes=2)
    await _advance()
    assert await _with_the_analyst() == ["Initech"], "half an hour on, the next session finds out if it has lifted"
    # It had not: the session meets the limit again, and now the words name a time a month away (misread).
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(clock.now + timedelta(days=30)),
                      headers=scout_token)
    assert await _with_the_analyst() == []
    clock.after(minutes=31)
    await _advance()
    assert await _with_the_analyst() == ["Initech"], "taken as no time at all: asked again in half an hour"


async def test_while_the_limit_holds_the_owner_is_told_so_and_no_job_is_made_only_to_fail(
        js, client, scout_token, clock):
    """Nothing is handed out at the limit (`next_work`), so a request made then would wait five minutes and fail."""
    await _world()
    idle = {"session_id": "runner-idle", "state": "idle"}
    await client.post("/api/v1/agent/heartbeat", json=idle, headers=scout_token)
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": True, "why": None}
    lifts = clock.now + timedelta(hours=3)
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(lifts), headers=scout_token)
    why = f"Claude's usage limit is reached; it resets at {night.when_words(lifts)}. Try again after that."
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": False, "why": why}
    posting = {"text": "Operations Manager. " + "We need someone to run the programs. " * 3}
    async with db.session_factory()() as session:
        parses = await session.scalar(select(func.count()).select_from(ParseJob))  # the first impression's own
    refused = await client.post("/api/v1/profile/parse", json=posting)
    assert refused.status_code == 409 and refused.json() == {"detail": why, "why": why}
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(ParseJob)) == parses, "no job is made"
    clock.after(hours=3, minutes=1)  # it has lifted
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": True, "why": None}
    assert (await client.post("/api/v1/profile/parse", json=posting)).status_code == 200


async def test_a_limit_met_by_a_lane_holds_the_whole_pool_until_it_lifts_and_the_health_line_says_so(
        js, client, scout_token, clock):
    """A session of any lane that ends at the limit holds every kind of work, not only a catch-up's: no lane starts a
    session only to meet the same limit and fail."""
    lifts = clock.now + timedelta(hours=2)
    sent = await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(lifts), headers=scout_token)
    assert sent.status_code == 200
    async with db.session_factory()() as session:
        assert await availability.limit_until(session, OWNER) is not None
        assert (await availability.barred(session, OWNER, availability.Question.POOL)).words == "Claude's usage limit"
        health = {r["kind"]: r["health"] for r in await pool.listing(session, OWNER)}
    assert all(line.startswith(f"Held: Claude's usage limit, until {night.when_words(lifts)}.")
               for line in health.values()), health
    assert await _the_runner_asks() is None, "nothing is handed out while it holds"
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(clock.now + timedelta(minutes=5)),
                      headers=scout_token)
    async with db.session_factory()() as session:
        assert await availability.limit_until(session, OWNER) == lifts, "a shorter time never cuts a hold short"
    clock.after(hours=2, minutes=1)
    async with db.session_factory()() as session:
        assert await availability.limit_until(session, OWNER) is None
        assert await availability.barred(session, OWNER, availability.Question.POOL) is None


async def test_a_limit_with_no_time_holds_the_pool_half_an_hour(js, client, scout_token, clock):
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(None), headers=scout_token)
    async with db.session_factory()() as session:
        assert await availability.limit_until(session, OWNER) == clock.now + availability.RETRY_AFTER


async def test_a_catch_up_that_starts_while_a_lane_holds_the_limit_hands_the_analyst_nothing(
        js, client, scout_token, clock):
    """The limit a lane met holds the whole pool, and docs/PRD.md 2.9 says the catch-up starts no Claude work of any
    kind until it lifts: a catch-up that starts inside the hold hands no role over either (it used to read only the
    limit its own run had learned, so the analyst was given a role while the pool began no session)."""
    lifts = clock.now + timedelta(hours=2)
    sent = await client.post("/api/v1/agent/heartbeat", json={**_ended_at_the_limit(lifts), "lane": "analysis"},
                             headers=scout_token)
    assert sent.status_code == 200
    await _world()
    assert await _with_the_analyst() == [], "no role is handed to the analyst while the lane's limit holds"
    assert "Paused: Claude's usage limit, until" in await _summary()
    assert await _the_runner_asks() is None
    clock.after(hours=2, minutes=1)
    await _advance()
    assert await _with_the_analyst() == ["Initech"], "the role goes out once the limit has lifted"


async def test_the_runner_is_told_nothing_waits_at_the_limit_and_the_next_role_the_moment_one_is_done(
        js, client, scout_token, clock):
    await _world()
    assert (await client.get("/api/v1/agent/work", headers=scout_token)).json()["next"] == "analysis"
    await _researches()
    assert await _with_the_analyst() == []
    await settle_housekeeping(OWNER)  # the next role goes to the analyst in the app's own pass, not in the ask
    got = await client.get("/api/v1/agent/work", headers=scout_token)  # asked as its session ends
    assert got.json()["next"] == "analysis" and await _with_the_analyst() == [IN_PLAY[1]], "no gap between roles"
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(clock.now + timedelta(hours=1)),
                      headers=scout_token)
    assert (await client.get("/api/v1/agent/work", headers=scout_token)).json() == {"next": None}


# --- a search is not started into the limit, and the sweep does not count the time spent in it ----------------------


async def _scout_due(night_ends: datetime) -> None:
    """The search mission on and due since an hour ago on an hourly clock, finishing at `night_ends`."""
    zone = owner_zone()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        scout = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.SCOUT))
        await missions.resume(session, scout, by="Tester")
        scout.schedule = {"kind": "every", "hours": 1}
        scout.checked_at = datetime.now(UTC) - timedelta(hours=2)
        scout.settings = {**scout.settings, "finish_by": night_ends.astimezone(zone).strftime("%H:%M")}
        await session.commit()


async def _searches() -> list[SearchRun]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(SearchRun))).all())


async def test_a_scheduled_search_waits_for_the_usage_limit_and_starts_when_it_lifts(js, client, scout_token, clock):
    await _world()
    await _scout_due(datetime.now(UTC) + timedelta(hours=2))
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(clock.now + timedelta(minutes=30)),
                      headers=scout_token)
    async with db.session_factory()() as session:
        assert await availability.limit_until(session, OWNER) is not None
        await missions.schedule_pass(session, OWNER, now=clock.now)
        await session.commit()
    assert await _searches() == [], "no search is started into the limit"
    async with db.session_factory()() as session:
        scout = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.SCOUT))
        assert await missions.due(session, scout, clock.now), "the night is still due"
        assert (await session.scalars(select(MissionRun).where(MissionRun.mission_id == scout.id))).all() == []
    clock.after(minutes=31)  # the limit has lifted, well before the night's finish time
    async with db.session_factory()() as session:
        knocks = await missions.schedule_pass(session, OWNER, now=clock.now)
        await session.commit()
    [search] = await _searches()
    assert search.status == "open" and (str(search.id), "search_started") in knocks


async def test_a_limit_that_outlasts_the_nights_finish_time_skips_the_night_and_says_why(
        js, client, scout_token, clock):
    await _world()
    await _scout_due(datetime.now(UTC) + timedelta(hours=2))
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(clock.now + timedelta(hours=5)),
                      headers=scout_token)
    async with db.session_factory()() as session:
        await missions.schedule_pass(session, OWNER, now=clock.now)
        await session.commit()
    assert await _searches() == []
    async with db.session_factory()() as session:
        scout = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.SCOUT))
        [skipped] = (await session.scalars(select(MissionRun).where(MissionRun.mission_id == scout.id))).all()
        assert skipped.outcome == "skipped" and "Claude's usage limit holds until" in skipped.summary
        assert not await missions.due(session, scout, clock.now), "the night is over: it is not started late"


async def _a_search_open_since(hours: float) -> SearchRun:
    async with db.session_factory()() as session:
        search = SearchRun(owner=OWNER, status="open", started_by="schedule",
                           started_at=datetime.now(UTC) - timedelta(hours=hours))
        session.add(search)
        await session.commit()
        return search


async def _the_sweep_runs() -> str:
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        sweep = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.SWEEP))
        await missions.start(session, OWNER, sweep, "schedule")
        await session.commit()
    [search] = await _searches()
    return search.status


async def test_the_sweep_does_not_count_the_time_a_search_sat_in_the_usage_limit(js, client, scout_token, clock):
    await _world()
    await _a_search_open_since(3)  # past the 120 minutes, but nothing could start for part of them
    clock.now = datetime.now(UTC) - timedelta(minutes=90)  # the limit was met 90 minutes ago, and lifted 30 ago
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(clock.now + timedelta(hours=1)),
                      headers=scout_token)
    assert await _the_sweep_runs() == "open", "it was waiting for the limit, not stuck"


async def test_the_sweep_waits_out_a_limit_that_still_holds(js, client, scout_token, clock):
    await _world()
    await _a_search_open_since(3)
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(clock.now + timedelta(hours=1)),
                      headers=scout_token)
    assert await _the_sweep_runs() == "open"


async def test_a_search_stuck_long_after_the_limit_lifted_still_fails(js, client, scout_token, clock):
    await _world()
    await _a_search_open_since(3)
    clock.now = datetime.now(UTC) - timedelta(hours=7)  # the limit lifted five hours ago; the search began after
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(clock.now + timedelta(hours=2)),
                      headers=scout_token)
    assert await _the_sweep_runs() == "failed", "a real stuck search is still closed"


# --- a role that fails is tried again, not lost ---------------------------------------------------------------------


async def test_a_role_the_sweep_failed_is_tried_again_after_the_rest_and_one_that_cannot_be_researched_is_said_so(js):
    await _world()
    initech = await _id("Initech")
    await _the_sweep_fails_it()
    await _advance()
    assert await _with_the_analyst() == [IN_PLAY[1]], "the others go first: the failed role is at the back of the line"
    assert (await _state())["queue"] == [await _id(IN_PLAY[2]), await _id(IN_PLAY[3]), initech]
    for _ in range(3):  # the other three are researched
        await _researches()
        await _advance()
    assert await _with_the_analyst() == ["Initech"] and (await _state())["tries"][initech] == 2, "tried again"
    await _the_sweep_fails_it()
    await _advance()
    assert await _with_the_analyst() == ["Initech"] and (await _state())["tries"][initech] == 3
    await _the_sweep_fails_it()
    await _advance(heavy=False)
    state = await _state()
    assert state["gave_up"] == [initech] and state["queue"] == [] and await _with_the_analyst() == []
    assert "Researched 3 of 4 roles. 1 could not be researched." in await _summary()
    await _advance()
    run = await _run()
    assert run.outcome == "done" and "1 could not be researched" in run.summary


async def test_a_run_where_no_role_could_be_researched_ends_failed(js):
    await _world([tracker_row("Northwind", "Programs Lead", status="second_impression")])
    for _ in range(catch_up.ROLE_TRIES):
        await _the_sweep_fails_it()
        await _advance()
    run = await _run()
    assert run.outcome == "failed" and run.summary.startswith("No role could be researched.")


# --- the employers' job systems -------------------------------------------------------------------------------------


async def test_the_employers_job_systems_are_looked_for_by_the_apps_own_detection_when_the_research_is_done(
        js, monkeypatch, other_sites_say_nothing):
    seen = other_sites_say_nothing
    monkeypatch.setattr(polite, "make", lambda: _internet({
        probe_url("northwind"): probe_answer("Northwind")}, seen))
    rows = [
        tracker_row("Northwind", "Programs Lead", status="second_impression",
                    url="https://www.board.test/jobs/view/1"),
        # its own posting is on a job system: the links alone place it, with no request
        tracker_row("Wingtip", "Programs Lead", status="second_impression",
                    url="https://globex.wd5.tenantsys.test/Careers/job/x"),
        tracker_row("Soylent", "Programs Lead", status="rejected", url="https://www.board.test/jobs/view/3"),
        tracker_row("Hooli", "Programs Lead", status="dead", url="https://www.board.test/jobs/view/4"),
    ]
    await _world(rows)
    async with db.session_factory()() as session:  # an employer the import did not bring in: never asked about
        await employers.find_or_make(session, OWNER, "Outsider")
        await session.commit()
    await _researches()
    await _the_runner_asks()
    await _researches()
    assert await _advance(heavy=False) == [] and not seen, "a runner's question does no network work"
    state = await _state()
    assert state["queue"] == [] and state["inflight"] == [] and (await _run()).outcome == "running"
    await _advance()  # the schedule pass
    run = await _run()
    assert run.outcome == "done"
    async with db.session_factory()() as session:
        by_name = {e.name: e for e in (await session.scalars(select(Employer))).all()}
    assert by_name["Wingtip"].ats == "tenantsys" and by_name["Wingtip"].ats_found_by == "link"
    assert by_name["Northwind"].ats == PROBING_SYSTEM and by_name["Northwind"].ats_found_by == "probe"
    assert by_name["Soylent"].ats is None and by_name["Hooli"].ats is None, "no feed names them: left to the scout"
    assert by_name["Outsider"].ats is None and not any("outsider" in u for u in seen)
    assert "Found the job system of 2 employers that had none." in run.summary


async def test_the_job_systems_are_looked_for_a_few_employers_a_pass_and_each_only_once(
        js, monkeypatch, other_sites_say_nothing):
    seen = other_sites_say_nothing
    monkeypatch.setattr(catch_up, "BOARDS_A_PASS", 2)
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    await _world([tracker_row(n, "Programs Lead", status="dead") for n in names])
    await _advance()
    assert len((await _state())["boards"]["looked"]) == 2 and (await _run()).outcome == "running"
    assert "(2 of 5 employers looked at)" in await _summary()
    await _advance()
    assert len((await _state())["boards"]["looked"]) == 4
    await _advance()
    assert (await _run()).outcome == "done" and len((await _state())["boards"]["looked"]) == 5
    asked = [u for u in seen if probe_url("") in u]
    assert len(asked) == 5 and len(set(asked)) == 5, "an employer is asked about once, not once a pass"


async def test_an_employer_the_owner_set_or_gated_is_not_asked_about(js, other_sites_say_nothing):
    seen = other_sites_say_nothing
    await _world([tracker_row("Alpha", "Programs Lead", status="dead"),
                  tracker_row("Bravo", "Programs Lead", status="dead")])
    async with db.session_factory()() as session:
        alpha = await employers.find(session, OWNER, "Alpha")
        alpha.ats, alpha.ats_found_by, alpha.board = "hostsys", "owner", {"host": "careers-alpha.hostsys.test"}
        (await employers.find(session, OWNER, "Bravo")).gated = True
        await session.commit()
    await _advance()
    assert (await _run()).outcome == "done" and not seen
    async with db.session_factory()() as session:
        assert (await employers.find(session, OWNER, "Alpha")).ats == "hostsys"


async def test_a_catch_up_that_fails_in_a_pass_leaves_the_other_and_the_pass_alone_and_goes_on_at_the_next(
        js, monkeypatch, caplog):
    await _world([tracker_row("Alpha", "Programs Lead", status="dead")])
    async with db.session_factory()() as session:  # a second import: a second catch-up beside the first
        session.add(_tracker_role("Bravo", "rejected"))
        await session.flush()
        await employers.settle(session, OWNER)
        assert await catch_up.start(session, OWNER) is not None
        research = (await session.scalars(select(Mission).where(Mission.key == missions.RESEARCH))).one()
        research.state, research.paused_until = "paused", datetime.now(UTC) - timedelta(hours=1)  # its pause ran out
        await session.commit()
    first_run, second_run = await _runs()
    real, calls = resolve.from_links, []

    async def from_links(session, owner):
        calls.append(owner)
        if len(calls) == 1:  # a record gone under the first catch-up's pass
            raise StaleDataError("UPDATE statement on table 'js_employers' expected to update 1 row(s); 0 matched.")
        return await real(session, owner)

    monkeypatch.setattr(resolve, "from_links", from_links)
    with caplog.at_level("WARNING"):
        async with db.session_factory()() as session:
            await missions.schedule_pass(session, OWNER)  # the pass does not fail
            await session.commit()
    assert [r.levelname for r in caplog.records if "catch-up" in r.getMessage()] == ["WARNING"]
    assert "goes on at the next pass" in caplog.text
    states = {r.id: (r.outcome, catch_up.state_of(r)["boards"]["looked"]) for r in await _runs()}
    assert states[first_run.id] == ("running", []), "the first is as it was: its boards are looked for next time"
    assert states[second_run.id][0] == "done" and states[second_run.id][1], "the second went on in the same pass"
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Mission.state).where(Mission.key == missions.RESEARCH))).one() == "on", (
            "what the pass did before it is kept")
    async with db.session_factory()() as session:
        await missions.schedule_pass(session, OWNER)
        await session.commit()
    assert {r.id: r.outcome for r in await _runs()} == {first_run.id: "done", second_run.id: "done"}


# --- a normal night is unchanged ------------------------------------------------------------------------------------


async def test_the_nightly_mission_still_researches_found_roles_up_to_its_number(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        analyse = (await session.scalars(select(Mission).where(Mission.key == missions.ANALYSE))).one()
        await missions.change(session, analyse, by="Tester", values={"target": 1})
        await session.commit()
    # The two come in unscored (the tracker's figure is kept off the role), so the fresher posting goes first.
    today = owner_today()
    deck = "second_impression"
    await _world([tracker_row("Northwind", "Programs Lead", status=deck, fit=2, found=today - timedelta(days=1)),
                  tracker_row("Wingtip", "Programs Lead", status=deck, fit=1, found=today - timedelta(days=9))])
    assert await _with_the_analyst() == ["Northwind"]
    async with db.session_factory()() as session:
        analyse = (await session.scalars(select(Mission).where(Mission.key == missions.ANALYSE))).one()
        # The catch-up is running in this mission's history, and it neither blocks the night nor counts as its start.
        await missions.refuse_a_second(session, analyse)
        assert await missions._last_started(session, analyse) is None, "the catch-up is no scheduled run"
        entry = await missions.start(session, OWNER, analyse, by="you")
        await session.commit()
        assert entry.started["roles"] == [str(ids["Fabrikam"])], "one role (the number), the youngest and the best"
        night_run = await session.get(MissionRun, entry.id)
        await missions.refresh(session, night_run)
        assert night_run.outcome == "running" and night_run.summary.startswith("Sent 1 role to the analyst")
        runs = (await session.scalars(select(MissionRun).where(MissionRun.mission_id == analyse.id))).all()
    assert sorted(r.started_by for r in runs) == ["import", "you"]
    assert await _with_the_analyst() == ["Fabrikam", "Northwind"], "the night's role is beside the catch-up's"


async def test_the_schedule_pass_carries_the_catch_up_on_and_starts_no_night_of_its_mission(js):
    await _world()
    async with db.session_factory()() as session:
        analyse = (await session.scalars(select(Mission).where(Mission.key == missions.ANALYSE))).one()
        assert await missions.due(session, analyse) is False, "Analyse the finds is paused at first"
        await missions.schedule_pass(session, OWNER)
        await session.commit()
        runs = (await session.scalars(select(MissionRun).where(MissionRun.mission_id == analyse.id))).all()
    assert [r.started_by for r in runs] == ["import"] and runs[0].outcome == "running"
    assert await _with_the_analyst() == ["Initech"]


# --- what the Missions screen reads ---------------------------------------------------------------------------------


async def test_the_missions_screen_reads_the_catch_up_as_a_run_with_its_progress(js, client):
    await _world()
    for _ in range(2):
        await _researches()
        await _the_runner_asks()
    listing = (await client.get("/api/v1/missions")).json()["missions"]
    analyse = next(m for m in listing if m["key"] == "analyse_finds")
    last = analyse["last_run"]
    assert last["started_by"] == "import" and last["outcome"] == "running"
    assert last["progress"] == {"done": 2, "target": 4}
    assert last["summary"].startswith("Researched 2 of 4 roles.")
    assert len(last["instances"]) == 1, "the live run view opens from it"
    detail = (await client.get(f"/api/v1/missions/{analyse['id']}")).json()
    assert [r["started_by"] for r in detail["runs"]] == ["import"]
    assert detail["state"] == "paused", "a catch-up runs whether Analyse the finds is on or paused, as Run now does"


# --- more than one analyst ------------------------------------------------------------------------------------------


async def _companies(ids: list[str]) -> list[str]:
    async with db.session_factory()() as session:
        names = {str(r.id): r.company for r in (await session.scalars(select(Opportunity))).all()}
    return [names[i] for i in ids]


def test_the_catch_up_has_one_analyst_unless_the_install_says_more():
    from pydantic import ValidationError

    from app.settings import JobSearchSettings

    assert JobSearchSettings().analysts == 1, "a new install researches one role at a time, as it always did"
    assert JobSearchSettings(analysts=3).analysts == 3
    for bad in (0, -1, 9):
        with pytest.raises(ValidationError):
            JobSearchSettings(analysts=bad)


async def test_with_three_analysts_three_roles_are_with_the_analyst_at_once_and_the_next_follows_each_done(
        js, monkeypatch):
    monkeypatch.setattr(settings, "analysts", 3)
    handed = []
    real = analysis.start

    async def start(session, owner, role):
        handed.append(role.company)
        return await real(session, owner, role)

    monkeypatch.setattr(analysis, "start", start)
    await _world(ELSEWHERE)
    state = await _state()
    assert await _with_the_analyst() == sorted(IN_PLAY[:3]), "three at once, best first"
    assert await _companies(state["inflight"]) == IN_PLAY[:3] and await _companies(state["queue"]) == IN_PLAY[3:]
    assert {state["tries"][r] for r in state["inflight"]} == {1}
    assert await _advance(heavy=False) == [] and len(await _with_the_analyst()) == 3, "never more than three"
    assert await _researches() == IN_PLAY[0]
    await _advance(heavy=False)
    assert await _with_the_analyst() == sorted(IN_PLAY[1:]), "the fourth follows the first at once"
    assert sorted(await _companies((await _state())["inflight"])) == sorted(IN_PLAY[1:])
    # A finished research runs the pipeline, which takes a role waiting that is not on deck out of the line
    # (`pipeline.stop_waiting`); the catch-up's next look hands it to the analyst again, so each finish is followed by
    # one, and the three are still researched one after another with never more than three with the analyst.
    # The roles taken out and handed again may come back in another order, so the three are told by what they are.
    researched = [await _researches()]
    await _advance(heavy=False)
    researched.append(await _researches())
    await _advance(heavy=False)
    assert await _the_runner_asks() == "analysis"
    assert len(await _with_the_analyst()) == 1
    researched.append(await _researches())
    assert sorted(researched) == sorted(IN_PLAY[1:]), "each of the other three, once"
    await _advance(heavy=False)
    assert await _with_the_analyst() == [] and (await _state())["inflight"] == []
    assert set(handed) == set(IN_PLAY) and handed[:3] == IN_PLAY[:3], "every role was handed, the best first"
    assert (await _summary()).startswith("Researched 4 of 4 roles.")


async def test_the_sweep_fails_only_the_role_a_session_took_and_the_roles_in_line_wait(js, monkeypatch):
    monkeypatch.setattr(settings, "analysts", 3)
    await _world(ELSEWHERE)
    async with db.session_factory()() as session:
        assert await analysis.settle_stuck(session, OWNER, now=datetime.now(UTC) + timedelta(hours=5)) == 0, (
            "handed to the analyst and waiting for a session is not stuck")
        await session.commit()
    assert len(await _with_the_analyst()) == 3
    await _the_sweep_fails_it()  # a session took Initech, the oldest, and never sent it
    assert await _with_the_analyst() == sorted(IN_PLAY[1:3]), "the two still in line are untouched"
    await _advance(heavy=False)
    state = await _state()
    assert await _companies(state["inflight"]) == [IN_PLAY[1], IN_PLAY[2], IN_PLAY[3]], "the fourth takes its place"
    assert await _companies(state["queue"]) == [IN_PLAY[0]], "the failed role goes to the back of the line"
    assert state["tries"][await _id(IN_PLAY[0])] == 1


async def test_a_usage_limit_takes_back_every_role_the_catch_up_has_and_all_go_again_when_it_lifts(
        js, client, scout_token, clock, monkeypatch):
    monkeypatch.setattr(settings, "analysts", 3)
    await _world(ELSEWHERE)
    inflight = (await _state())["inflight"]
    assert len(inflight) == 3 and len(await _with_the_analyst()) == 3
    async with db.session_factory()() as session:
        instances = [(await session.get(Opportunity, UUID(i))).analysis_instance_id for i in inflight]
    lifts = clock.now + timedelta(hours=3)
    await client.post("/api/v1/agent/heartbeat", json=_ended_at_the_limit(lifts), headers=scout_token)
    # One session met the limit; every role the catch-up had is taken back, none is left for the sweep to fail
    assert await _with_the_analyst() == []
    async with db.session_factory()() as session:
        assert [(await session.get(ProcInstance, i)).status for i in instances] == ["cancelled"] * 3
        assert await analysis.settle_stuck(session, OWNER, now=datetime.now(UTC) + timedelta(hours=5)) == 0
        assert {(await session.get(Opportunity, UUID(i))).analysis_state for i in inflight} == {None}
    state = await _state()
    assert state["inflight"] == [] and state["queue"] == [*inflight, *state["queue"][3:]], "first in line, in order"
    assert {state["tries"][i] for i in inflight} == {0}, "the tries do not count"
    assert "Paused: Claude's usage limit, until" in await _summary()
    # Nothing starts before it lifts, whichever session asks
    assert await _the_runner_asks() is None
    clock.after(hours=3, minutes=1)
    await _advance()
    assert await _companies((await _state())["inflight"]) == IN_PLAY[:3], "the same three, again, in their order"
    assert len(await _with_the_analyst()) == 3 and "Paused" not in await _summary()


async def test_a_lane_session_that_meets_the_usage_limit_holds_the_catch_up_like_the_scouts_own(
        js, client, scout_token, clock, monkeypatch):
    monkeypatch.setattr(settings, "analysts", 3)
    await _world(ELSEWHERE)
    assert len(await _with_the_analyst()) == 3
    body = {**_ended_at_the_limit(clock.now + timedelta(hours=1)), "lane": "analysis"}
    sent = await client.post("/api/v1/agent/heartbeat", json=body, headers=scout_token)
    assert sent.status_code == 200
    assert await _with_the_analyst() == [], "every role the catch-up had is taken back"
    assert "Paused: Claude's usage limit, until" in await _summary()
    assert await _the_runner_asks() is None


async def test_the_catch_up_never_has_two_roles_on_one_site_with_the_analyst_and_hands_out_the_next_elsewhere(
        js, monkeypatch):
    """The owner's rule (2026-10-13): research does not fetch one site in parallel, and most imported roles' links are
    Exboard's. The roles the analyst has are on different sites: a role on the site of one it has waits its turn (in
    its place in the line), the next role on another site is handed out instead, and when every role left is on such
    a site none is: it waits, it does not double up."""
    monkeypatch.setattr(settings, "analysts", 3)
    elsewhere = {"Wingtip": "https://careers.wingtip.example.test/jobs/1"}
    rows = [{**row, "url": elsewhere.get(row["company"], f"https://www.board.test/jobs/view/{n}")}
            for n, row in enumerate(ROWS)]
    await _world(rows)
    # in order: Initech (Exboard), Umbrella (Exboard: waits), Wingtip (elsewhere), Northwind (Exboard: waits)
    assert await _with_the_analyst() == ["Initech", "Wingtip"], "two roles, two sites, though three may be had"
    state = await _state()
    assert await _companies(state["inflight"]) == ["Initech", "Wingtip"]
    assert await _companies(state["queue"]) == ["Umbrella", "Northwind"], "those that waited keep their place"
    assert await _advance(heavy=False) == [] and await _with_the_analyst() == ["Initech", "Wingtip"], "no doubling up"
    assert await _researches() == "Initech"
    await _advance(heavy=False)
    assert await _with_the_analyst() == ["Umbrella", "Wingtip"], "Exboard is free: the next Exboard role, in its turn"
    assert await _companies((await _state())["queue"]) == ["Northwind"]
    assert await _researches() == "Wingtip"
    await _advance(heavy=False)
    assert await _with_the_analyst() == ["Umbrella"], "Northwind is on Exboard too: it waits for Umbrella"
    assert await _researches() == "Umbrella"
    await _advance(heavy=False)
    assert await _with_the_analyst() == ["Northwind"]
    assert await _researches() == "Northwind"
    await _advance(heavy=False)
    assert await _with_the_analyst() == [] and (await _summary()).startswith("Researched 4 of 4 roles.")


async def test_with_every_link_on_one_site_the_catch_up_is_one_role_at_a_time_however_many_analysts_there_are(
        js, monkeypatch):
    monkeypatch.setattr(settings, "analysts", 3)
    await _world()  # ROWS: every link is Exboard's
    for company in IN_PLAY:
        assert await _with_the_analyst() == [company], "one at a time"
        assert await _researches() == company
        await _the_runner_asks()


async def test_the_roles_the_catch_up_has_on_different_sites_are_taken_by_sessions_that_ask_together(js, monkeypatch):
    monkeypatch.setattr(settings, "analysts", 3)
    await _world(ELSEWHERE)

    async def one_session():
        async with db.session_factory()() as session:
            role = await analysis.claim_next(session, OWNER)
            await session.commit()
            return role.company if role is not None else None

    got = await asyncio.gather(*(one_session() for _ in range(4)))
    assert sorted(g for g in got if g) == sorted(IN_PLAY[:3]), "three sessions, three roles, each on its own site"
    assert got.count(None) == 1


async def test_a_catch_up_saved_with_one_current_role_goes_on_with_it_as_the_one_it_has(js):
    """A run that was going when this release was installed holds the role with the analyst as `current`."""
    await _world()
    run = await _run()
    state = catch_up.state_of(run)
    (rid,) = state["inflight"]
    async with db.session_factory()() as session:
        entry = await session.get(MissionRun, run.id)
        old = dict(entry.started["catch_up"])
        old.pop("inflight")
        old["current"] = rid
        entry.started = {**entry.started, "catch_up": old}
        await session.commit()
    assert (await _state())["inflight"] == [rid] and "current" not in await _state()
    await _researches()
    await _advance(heavy=False)
    assert await _with_the_analyst() == [IN_PLAY[1]] and (await _state())["inflight"] == [await _id(IN_PLAY[1])]


async def test_the_catch_up_hands_the_next_role_on_in_the_app_s_own_pass_whatever_the_open_search_is_doing(
        js, client, scout_token):
    """The catch-up's next role goes to the analyst as the last is done, with a search open too (the pass hands it on,
    not an ask)."""
    from app.jobsearch.models import SearchRun

    await _world()
    async with db.session_factory()() as session:
        session.add(SearchRun(owner=OWNER, status="open", started_by="manual"))  # a search that is open
        await session.commit()
    await _researches()
    assert await _with_the_analyst() == [], "researched; the next is not handed out yet"
    await settle_housekeeping(OWNER)  # the next role goes to the analyst in the app's own pass, not in the ask
    got = await client.get("/api/v1/agent/work", headers=scout_token)
    assert got.json()["next"] == "analysis" and await _with_the_analyst() == [IN_PLAY[1]], "no gap between roles"


# --- the pieces -----------------------------------------------------------------------------------------------------


def test_the_hold_is_said_with_a_time_in_the_owners_own_day(monkeypatch):
    monkeypatch.setattr("app.timeutil.settings.timezone", "UTC")
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    assert night.when_words(now + timedelta(hours=3), now) == "3:00 PM"
    assert night.when_words(now + timedelta(days=2), now) == "Oct 15, 12:00 PM"
