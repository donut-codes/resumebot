"""Reading the employers' boards before the search (docs/PRD.md 28.10, 28.11 and 30.1): a code-only mission that peak
hours never hold reads the boards and filters the new postings; the search's title pass checks every posting's title
against the role tabs and a reader reads what a tab said yes to; each read candidate becomes a role linked to its
posting, with the posting's text. Adapters are stand-ins; no network. Synthetic data only."""

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from process_engine.errors import ActionError
from process_engine.models import CANCELLED, COMPLETED, FAILED, WAITING, ProcInstance
from sqlalchemy import func, select, update

from app import db
from app.jobsearch import actions, adapters, process
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import signals
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.missions import boards_night
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Employer, JobPosting, Mission, MissionRun, Opportunity, StageMove
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.postings import index as postings
from app.settings import settings
from app.sites import where
from tests.employer_builders import make_employer, make_role
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, gate_says, load_profile


@dataclass
class Listing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class Board:
    postings: list[dict] = field(default_factory=list)

    async def read(self, fetch, board):
        return Listing(self.postings)

    async def detail(self, fetch, board, posting):
        return {"text": _whole(posting["title"])}


def _posting(ext_id, title, **more):
    return {"ext_id": ext_id, "title": title, "location": "Remote", "work_mode": "remote", "comp": None,
            "comp_min": None, "comp_max": None, "posted": None, "url": f"https://jobs.example.test/{ext_id}",
            "apply_url": None, "text": None, "department": None, **more}


@pytest.fixture
def board(monkeypatch):
    monkeypatch.setattr(polite, "make", _fetch)  # every step reads through a mock: no network
    fake = Board([_posting("1", "Director, Data Governance"), _posting("2", "Payroll Specialist"),
                  _posting("3", "Data Governance Lead", location="Greater Gotham", work_mode="onsite")])
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: fake})
    return fake


def _fetch():
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


async def _setup():
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"], "search_plan": ["data governance"]})
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        fab.ats, fab.board = EXAMPLE_SYSTEM, {"slug": "fabrikam"}
        fab.board_url, fab.check_directly = "https://boards.example.test/fabrikam", True  # watched: its site is on
        await where.seed_employer_sites(session, OWNER)
        await session.commit()



FILLER = " The role leads the work, sets priorities, manages the team and reports to the executive team." * 4


def _whole(title: str) -> str:
    """A whole posting's text: long enough not to be taken for a list's snippet (`cleansed.MIN_TEXT`)."""
    return f"The whole posting for {title}." + FILLER


async def test_the_read_fills_the_index_filters_it_and_reads_the_candidates_text(js, board):
    await _setup()
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "done" and len(counts["instances"]) == 1, "the read runs as its playbook"
    assert (counts["seen"], counts["new"], counts["candidates"], counts["details"]) == (3, 3, 2, 2)
    assert counts["filtered_out"] == {gate_says()["location"]: 1}
    assert counts["routes"] == {"roles": 2}, "every posting that passes the gates goes on to become a role"
    entry = MissionRun(started={"reading": False, **counts}, outcome="done")
    stopped = missions.run_view(entry, missions.READ)["stopped"]
    assert stopped is None, "the filter's counts are not the run's stop reason"
    assert summary == (
        "Read 1 employer board: 3 new postings.\n2 passed the filter.\n"
        f"Stopped:\n1 {gate_says()['location']}")
    async with db.session_factory()() as session:
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert rows["1"].text == _whole("Director, Data Governance") and rows["1"].filtered_by is None
    assert rows["2"].route == "roles" and rows["3"].filtered_by == gate_says()["location"]




async def test_every_posting_the_read_lets_through_becomes_a_linked_role_with_the_postings_text(js, board):
    await _setup()
    await boards_night.read_boards(OWNER, details_per_night=10)
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER) == 2
        await session.commit()
    async with db.session_factory()() as session:
        roles = {r.title: r for r in (await session.scalars(select(Opportunity))).all()}
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert set(roles) == {"Director, Data Governance", "Payroll Specialist"}, "the stopped posting is no role"
    role = roles["Director, Data Governance"]
    assert rows["1"].opportunity_id == role.id, "the posting knows the role it became"
    assert role.description == _whole("Director, Data Governance"), "the role has the posting's text"
    assert rows["3"].opportunity_id is None and rows["3"].filtered_by == gate_says()["location"]


async def test_peak_hours_never_hold_the_read_and_a_read_that_never_ends_is_closed(js, monkeypatch):
    started: list = []
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: started.append(a))
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        read = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.READ))
        monkeypatch.setattr(missions.night, "in_peak", lambda *a, **k: True)
        monkeypatch.setattr(missions, "due", _due_only(read.id))
        await missions.schedule_pass(session, OWNER)
        await session.commit()  # the read starts once its run is saved
        entry = await session.scalar(select(MissionRun).where(MissionRun.mission_id == read.id))
        assert entry is not None and entry.outcome == "running" and len(started) == 1, "peak hours don't hold it"
        entry.started_at = datetime.now(UTC) - timedelta(minutes=boards_night.LONGEST_MINUTES + 1)
        await missions.refresh(session, entry)
        assert entry.outcome == "running", "past its time limit the read is given a while to end itself, cleanly"
        entry.started_at = datetime.now(UTC) - timedelta(minutes=boards_night.LOST_AFTER_MINUTES + 1)
        await missions.refresh(session, entry)
        assert entry.outcome == "failed" and "never finished" in entry.summary


def _due_only(mission_id):
    async def due(session, mission, now=None):
        return mission.id == mission_id
    return due


async def test_run_now_reads_the_boards_in_peak_hours(js, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.jobsearch.main import app
    from app.settings import settings

    monkeypatch.setattr(missions.night, "blackout", lambda *a, **k: "Claude's peak hours: no Claude work until 2 PM.")
    async with db.session_factory()() as session:
        await load_profile(session, settings.owner)
        await missions.ensure(session, settings.owner)
        mine = {m.key: m for m in (await session.scalars(select(Mission).where(Mission.owner == settings.owner))).all()}
        read, scout = mine[missions.READ], mine[missions.SCOUT]
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(f"/api/v1/missions/{read.id}/run")).status_code == 200, "no Claude: never held"
        assert (await client.post(f"/api/v1/missions/{scout.id}/run")).status_code == 409, "the scout is held"


# --- the read's time limit (docs/PRD.md 28.17) ------------------------------------------------------------------------


async def test_the_read_gives_every_fetcher_its_steps_make_one_deadline_and_takes_it_back(js, board, monkeypatch):
    made: list[Fetcher] = []
    monkeypatch.setattr(polite, "make", lambda: made.append(_fetch()) or made[-1])
    await _setup()
    before = time.monotonic()
    await boards_night.read_boards(OWNER, details_per_night=10)
    after = time.monotonic()
    assert len(made) >= 3, "the steps that fetch each made one"
    limit = boards_night.LONGEST_MINUTES * 60
    assert all(before + limit <= f.deadline <= after + limit for f in made)
    assert polite.READ_BY.get() is None, "it is the read's alone: nothing made after it takes it"


async def test_a_read_whose_time_runs_out_ends_done_with_what_it_read_and_says_what_is_left(js, monkeypatch):
    class Clocked(Fetcher):
        """A fetcher whose time is the test's to run out, the same for every one the read makes."""

        out = False

        @property
        def deadline(self):
            return 0.0 if Clocked.out else None

        @deadline.setter
        def deadline(self, value):
            pass

    class RunsOut:
        """The first board is where the time runs out: it is cut short. The second is never begun."""

        async def read(self, fetch, board):
            if board["slug"] != "contoso":
                raise AssertionError("a board the read never began was asked")
            Clocked.out = True
            return Listing([_posting("c1", "Director, Data Governance"), _posting("c2", "Data Governance Lead")], True,
                           "stopped at 2 postings (the time allowed for the read ran out)")

        async def detail(self, fetch, board, posting):
            raise AssertionError("a description was read after the time ran out")

    monkeypatch.setattr(polite, "make", lambda: Clocked(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0))
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: RunsOut()})
    await _setup()
    async with db.session_factory()() as session:
        contoso = await employers.find_or_make(session, OWNER, "Contoso")
        contoso.ats, contoso.board = EXAMPLE_SYSTEM, {"slug": "contoso"}
        contoso.board_url, contoso.check_directly = "https://boards.example.test/contoso", True
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "done", "running out of time is not a failure"
    assert (counts["employers"], counts["unread"], counts["cut"], counts["seen"], counts["new"]) == (2, 1, 1, 2, 2)
    assert (counts["candidates"], counts["details"], counts["details_left"]) == (2, 0, 2)
    assert summary == (
        "Read 1 of 2 employer boards: 2 new postings.\n2 passed the filter.\n"
        "The read ran out of time (3 hours): 1 employer board not read, 1 read in part, 2 descriptions not read. "
        "They wait for the next read.")
    async with db.session_factory()() as session:
        fab = await employers.find(session, OWNER, "Fabrikam")
        con = await employers.find(session, OWNER, "Contoso")
        assert fab.fetched_at is None, "the board never begun is as it was"
        assert con.fetch_note == "stopped at 2 postings (the time allowed for the read ran out)"
        rows = (await session.scalars(select(JobPosting))).all()
        assert sorted(p.ext_id for p in rows) == ["c1", "c2"]
        assert all(p.text is None for p in rows), "their descriptions are left for the next read"


def test_the_summary_says_what_a_read_that_ran_out_of_time_left_and_nothing_when_it_left_nothing():
    read = {"employers": 63, "new": 5210, "unread": 22, "cut": 3}
    sifted = {"candidates": 1204, "routes": {"roles": 1100}, "stopped": {}}
    assert boards_night.summary_words(read, sifted, {"filled": 40, "left": 1204}) == (
        "Read 41 of 63 employer boards: 5,210 new postings.\n"
        "1,204 passed the filter.\n"
        "The read ran out of time (3 hours): 22 employer boards not read, 3 read in part, 1,204 descriptions not read. "
        "They wait for the next read.")
    only_one = boards_night.summary_words({"employers": 2, "new": 1, "unread": 1}, {"candidates": 0}, {"left": 1})
    assert only_one.splitlines()[0] == "Read 1 of 2 employer boards: 1 new posting."
    assert only_one.splitlines()[-1] == (
        "The read ran out of time (3 hours): 1 employer board not read, 1 description not read. "
        "They wait for the next read.")
    whole = boards_night.summary_words(
        {"employers": 63, "new": 5, "unread": 0, "cut": 0}, {"candidates": 3}, {"left": 0})
    assert "ran out of time" not in whole and whole.startswith("Read 63 employer boards: 5 new postings.")


# --- each step is a transaction of its own ----------------------------------------------------------------------------
#
# The read ran its whole playbook in one session and committed once, after a long time. Its merge of
# duplicate employers had deleted records and nothing else could touch them until the commit.


async def _instances() -> list[ProcInstance]:
    async with db.session_factory()() as session:
        return list((await session.scalars(
            select(ProcInstance).where(ProcInstance.process_key == process.READ_BOARDS)
            .order_by(ProcInstance.created_at))).all())


async def test_another_session_can_change_an_employer_while_the_read_is_between_its_steps(js, monkeypatch):
    monkeypatch.setattr(polite, "make", _fetch)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        one = await make_employer(session, "Initech", website="https://www.example-corp.test")
        two = await make_employer(session, "Initech Labs", website="https://www.example-corp.test")
        session.add_all([make_role(one, "Lead A"), make_role(one, "Lead B"), make_role(two, "Lead C")])
        await session.commit()
    seen: dict = {}

    async def while_the_next_step_runs(session, owner, fetch, full=False):
        """The step after the merge: the merge is committed, and nothing of the read holds the records it changed (the
        update waits for the one writer turn for no more than the guard's few seconds, so a record the read held
        would fail it)."""
        await db.release_if_free(session)
        async with db.session_factory()() as other:
            seen["names"] = sorted((await other.scalars(select(Employer.name))).all())
            await other.execute(update(Employer).values(notes="the owner's own note"))
            await other.commit()
        return {}

    monkeypatch.setattr(signals, "load_warn", while_the_next_step_runs)
    outcome, _summary, counts = await boards_night.read_boards(OWNER, details_per_night=5)
    assert outcome == "done" and len(seen["names"]) == 1, "the merge was saved when its step ended"
    async with db.session_factory()() as session:
        (kept,) = (await session.scalars(select(Employer))).all()
        assert kept.notes == "the owner's own note", "the owner's change was not undone by the rest of the read"
        assert len((await session.scalars(select(Opportunity))).all()) == 3, "the merge lost no role"
    (instance,) = await _instances()
    assert instance.status == COMPLETED and counts["instances"] == [str(instance.id)]


async def test_a_read_that_fails_part_way_keeps_the_steps_before_it_and_the_next_read_does_the_rest(
        js, board, monkeypatch):
    await _setup()
    real = signals.load_warn

    async def breaks(session, owner, fetch, full=False):
        raise RuntimeError("the notices went away")

    monkeypatch.setattr(signals, "load_warn", breaks)
    with pytest.raises(RuntimeError, match="the notices went away"):
        await boards_night.read_boards(OWNER, details_per_night=10)
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(JobPosting))).all()
        assert sorted(p.ext_id for p in rows) == ["1", "2", "3"], "the boards the read had read are kept"
        assert all(p.filtered_at is None and p.text is None for p in rows), "and nothing after the step that failed"
    (failed,) = await _instances()
    assert failed.status == FAILED and "the notices went away" in (failed.error or ""), "the process says where"
    monkeypatch.setattr(signals, "load_warn", real)
    outcome, _summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "done" and (counts["seen"], counts["new"]) == (3, 0), "the boards are read again, adding nothing"
    assert (counts["candidates"], counts["details"]) == (2, 2), "and what the first read left is filtered and read"
    async with db.session_factory()() as session:
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert sorted(rows) == ["1", "2", "3"], "no posting twice"
    assert rows["1"].text == _whole("Director, Data Governance") and rows["2"].route == "roles"
    assert rows["3"].filtered_by == gate_says()["location"], "a posting a gate stopped keeps its reason"
    assert [i.status for i in await _instances()] == [FAILED, COMPLETED]


async def test_a_step_that_names_an_error_ends_the_read_failed_and_keeps_the_steps_before_it(js, board, monkeypatch):
    await _setup()

    async def refuses(session, instance, data):
        raise ActionError("notices_down", "the notices cannot be read")

    spec = process.registry.get("jobs.layoff_notices")
    monkeypatch.setitem(process.registry._actions, "jobs.layoff_notices", dataclasses.replace(spec, fn=refuses))
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "failed" and "the notices cannot be read" in summary
    assert counts["seen"] == 3, "what the steps before it read is in the answer"
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(JobPosting))).all()) == 3
    assert [i.status for i in await _instances()] == [FAILED]


async def test_a_read_cut_short_leaves_its_process_open_and_the_next_read_and_the_start_up_close_it(
        js, board, monkeypatch):
    await _setup()
    real = signals.load_warn

    async def cut(session, owner, fetch, full=False):
        raise asyncio.CancelledError  # the task is cancelled (the app is shutting down): no step marks anything

    monkeypatch.setattr(signals, "load_warn", cut)
    with pytest.raises(asyncio.CancelledError):
        await boards_night.read_boards(OWNER, details_per_night=10)
    assert [i.status for i in await _instances()] == [WAITING], "left at the step it was at"
    async with db.session_factory()() as session:
        await missions.settle_after_restart(session, OWNER)  # what every start does
        await session.commit()
    assert [i.status for i in await _instances()] == [CANCELLED]
    with pytest.raises(asyncio.CancelledError):
        await boards_night.read_boards(OWNER, details_per_night=10)
    monkeypatch.setattr(signals, "load_warn", real)
    outcome, _summary, _counts = await boards_night.read_boards(OWNER, details_per_night=10)  # the next read
    assert outcome == "done"
    assert [i.status for i in await _instances()] == [CANCELLED, CANCELLED, COMPLETED], "none is left open"


async def test_the_boards_are_saved_one_at_a_time_as_they_are_read(js, monkeypatch):
    class EachItsOwn:
        async def read(self, fetch, board):
            return Listing([_posting(f"{board['slug']}-1", "Director, Data Governance")])

    monkeypatch.setattr(polite, "make", _fetch)
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: EachItsOwn()})
    await _setup()
    async with db.session_factory()() as session:
        contoso = await employers.find_or_make(session, OWNER, "Contoso")
        contoso.ats, contoso.board = EXAMPLE_SYSTEM, {"slug": "contoso"}
        contoso.board_url, contoso.check_directly = "https://boards.example.test/contoso", True
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
    saved: list[int] = []
    real = postings.store

    async def store(session, owner, listing, **more):
        async with db.read_session_factory()() as other:  # as the owner's own request would see it
            saved.append(await other.scalar(select(func.count()).select_from(Employer).where(
                Employer.fetched_at.is_not(None))))
        return await real(session, owner, listing, **more)

    monkeypatch.setattr(postings, "store", store)
    outcome, _summary, counts = await boards_night.read_boards(OWNER, details_per_night=0)
    assert outcome == "done" and counts["employers"] == 2
    # Each board's own first write (its fetched_at) is let go before its slices (`refresh`), so it is counted too.
    assert saved == [1, 2], "when the second board is stored, the first is already saved: not held until the step ends"


async def test_the_descriptions_are_saved_slice_by_slice_not_when_the_whole_read_ends(js, board, monkeypatch):
    board.postings = [_posting(str(n), f"Data Governance Lead {n}") for n in range(1, 5)]
    monkeypatch.setattr(actions, "DETAILS_AT_A_TIME", 2)
    await _setup()
    texts: list[int] = []
    real = board.detail

    async def detail(fetch, board_, posting):
        async with db.session_factory()() as other:
            texts.append(await other.scalar(select(func.count()).select_from(JobPosting).where(
                JobPosting.text.is_not(None))))
        return await real(fetch, board_, posting)

    monkeypatch.setattr(board, "detail", detail)
    await boards_night.read_boards(OWNER, details_per_night=10)
    assert texts == [0, 0, 2, 2], "the second slice is read once the first slice's two texts are saved"
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(JobPosting).where(
            JobPosting.text.is_not(None))) == 4


# --- the roles that went quiet, in the run's account ------------------------------------------------------------------
#
# The step "Move roles that went quiet" uses no Claude and fetches nothing, so it
# may have moved roles on a night whose read failed after it, or that found no site to read. The report names the
# Applied roles that heard nothing back, on a line of its own; the roles whose posting went are in their own history.

SILENT_TWO = "2 Applied roles moved to Gone Quiet: no word in 30 days."


def test_the_summary_says_how_many_applied_roles_heard_nothing_back_after_the_filter_lines_and_before_the_stops():
    read = {"employers": 63, "new": 5210, "unread": 22, "cut": 3}
    sifted = {"candidates": 1204, "routes": {"roles": 1100}, "stopped": {"the place": 7}}
    words = boards_night.summary_words(read, sifted, {"filled": 40, "left": 1204}, {"went": 3, "silent": 2, "days": 30})
    assert words == (
        "Read 41 of 63 employer boards: 5,210 new postings.\n"
        "1,204 passed the filter.\n"
        "The read ran out of time (3 hours): 22 employer boards not read, 3 read in part, 1,204 descriptions not read. "
        "They wait for the next read.\n"
        f"{SILENT_TWO}\nStopped:\n7 the place")
    one = boards_night.summary_words(
        {"employers": 1, "new": 0}, {"candidates": 0}, None, quiet={"silent": 1, "days": 1})
    assert one.splitlines()[-1] == "1 Applied role moved to Gone Quiet: no word in 1 day."


def test_with_nothing_to_say_of_it_the_summary_is_the_text_it_always_was():
    read = {"employers": 63, "new": 5210, "unread": 22, "cut": 3}
    sifted = {"candidates": 1204, "routes": {"roles": 1100}, "stopped": {"the place": 7}}
    details = {"filled": 40, "left": 1204}
    today = boards_night.summary_words(read, sifted, details)
    assert "Gone Quiet" not in today
    assert boards_night.summary_words(read, sifted, details, None) == today
    assert boards_night.summary_words(read, sifted, details, quiet=None) == today
    for said in ({"went": 4, "silent": 0, "days": 30}, {"went": 0, "silent": 2, "days": None}, {"silent": 2}, {}):
        assert boards_night.summary_words(read, sifted, details, said) == today, said


async def _quiet_roles(applied: int = 2, *, days: int | None = 30) -> None:
    """The owner's number set (or blank), and Applied roles that became Applied two months ago."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"], "search_plan": ["data governance"]},
                           settings={**PROFILE["settings"], "no_reply_days": days})
        for n in range(applied):
            role = Opportunity(
                owner=OWNER, slug=f"quiet-{n}", company=f"Quiet Co {n}", title=f"Analyst {n}", status="applied",
                match_key=f"quiet co {n}|analyst {n}", origin="scout", fit=3)
            session.add(role)
            await session.flush()
            await session.execute(update(StageMove).where(StageMove.role_id == role.id).values(
                at=datetime.now(UTC) - timedelta(days=60)))
        await session.commit()


async def _applied_roles() -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(Opportunity).where(
            Opportunity.owner == OWNER, Opportunity.status == "applied")) or 0


def _fails_with(monkeypatch, action: str, message: str) -> None:
    async def refuses(session, instance, data):
        raise ActionError("step_down", message)

    spec = process.registry.get(action)
    monkeypatch.setitem(process.registry._actions, action, dataclasses.replace(spec, fn=refuses))


async def test_a_read_that_fails_after_the_step_still_says_how_many_applied_roles_went_quiet(js, board, monkeypatch):
    await _setup()
    await _quiet_roles()
    _fails_with(monkeypatch, "jobs.read_descriptions", "the descriptions cannot be read")
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "failed"
    lines = summary.split("\n")
    assert lines[0].startswith("The read failed:") and "the descriptions cannot be read" in lines[0]
    assert lines[1:] == [SILENT_TWO], "the failure first, then the one line"
    assert (counts["no_reply"], counts["no_reply_days"], counts["posting_went"]) == (2, 30, 0)
    assert await _applied_roles() == 0, "the roles moved, though the read did not finish"


async def test_the_failure_sentence_is_cut_at_500_characters_and_the_line_after_it_never_is(js, board, monkeypatch):
    await _setup()
    await _quiet_roles()
    _fails_with(monkeypatch, "jobs.read_descriptions", "the descriptions cannot be read " * 100)
    outcome, summary, _counts = await boards_night.read_boards(OWNER, details_per_night=10)
    first, second = summary.split("\n")
    assert outcome == "failed" and len(first) == 500 and first.startswith("The read failed:")
    assert second == SILENT_TWO


async def test_a_night_that_finds_no_site_to_read_still_says_how_many_applied_roles_went_quiet(js):
    await _quiet_roles()
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "done"
    assert summary == (
        f"No switched-on site can be read yet: switch one on, or wait while its job system is found.\n{SILENT_TWO}")
    assert (counts["no_reply"], counts["no_reply_days"]) == (2, 30)
    assert await _applied_roles() == 0


async def test_a_read_that_fails_before_the_step_has_no_line_and_moved_nothing(js, board, monkeypatch):
    await _setup()
    await _quiet_roles()
    _fails_with(monkeypatch, "jobs.layoff_notices", "the notices cannot be read")
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "failed" and "\n" not in summary and "Gone Quiet" not in summary
    assert (counts["posting_went"], counts["no_reply"], counts["no_reply_days"]) == (0, 0, None)
    assert await _applied_roles() == 2
    (instance,) = await _instances()
    assert "quiet" not in (instance.data or {}), "the step never ran"


async def test_the_run_keeps_what_went_quiet_in_its_started_counts_as_data_and_one_line_in_its_summary(js, board):
    await _setup()
    await _quiet_roles()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        read = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.READ))
        entry = MissionRun(owner=OWNER, mission_id=read.id, started_by="you", outcome="running",
                           started={"reading": True})
        session.add(entry)
        await session.commit()
        entry_id = entry.id
    await boards_night._finish(entry_id, OWNER, 10)
    async with db.session_factory()() as session:
        done = await session.get(MissionRun, entry_id)
    assert done is not None and done.outcome == "done" and done.started["reading"] is False
    assert (done.started["posting_went"], done.started["no_reply"], done.started["no_reply_days"]) == (0, 2, 30)
    assert SILENT_TWO in done.summary.split("\n")
    assert missions.run_view(done, missions.READ)["stopped"] is None, "these counts are not the run's stop reason"


# --- a read the app's restart cut short carries on (docs/PRD.md 28) ---------------------------------------------------


async def _running_read(session, ago: timedelta, **started) -> MissionRun:
    await missions.ensure(session, OWNER)
    read = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.READ))
    entry = MissionRun(owner=OWNER, mission_id=read.id, started_by="schedule", outcome="running",
                       started_at=datetime(2026, 10, 20, 1, 0, tzinfo=UTC), summary="Reading the employers' boards.",
                       started={"reading": True, "details_per_night": 10, **started})
    session.add(entry)
    await session.flush()
    return entry


async def test_a_read_cut_short_by_a_restart_carries_on_instead_of_failing(js, monkeypatch):
    launched: list = []
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: launched.append(a))
    async with db.session_factory()() as session:
        entry = await _running_read(session, timedelta(hours=1))
        now = entry.started_at + timedelta(hours=1)
        await missions.settle_after_restart(session, OWNER, now=now)
        await session.commit()
        assert entry.outcome == "running" and entry.ended_at is None
        assert launched == [(entry.id, OWNER, 10, True)], "taken up again, as a resume"


async def test_a_held_start_does_not_carry_a_cut_read_on_and_leaves_it_for_the_next_start(js, monkeypatch):
    launched: list = []
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: launched.append(a))
    monkeypatch.setattr(settings, "run_schedule", False)
    closed: list = []

    async def end_unfinished(*a):
        closed.append(a)
        return 0

    monkeypatch.setattr(boards_night, "end_unfinished", end_unfinished)
    async with db.session_factory()() as session:
        entry = await _running_read(session, timedelta(hours=1))
        now = entry.started_at + timedelta(hours=1)
        await missions.settle_after_restart(session, OWNER, now=now)
        await session.commit()
        assert launched == [] and closed == [], "a held start reads nothing and closes no open process"
        assert entry.outcome == "running" and "resumed" not in entry.started
        monkeypatch.setattr(settings, "run_schedule", True)
        await missions.settle_after_restart(session, OWNER, now=now)
        await session.commit()
        assert launched == [(entry.id, OWNER, 10, True)], "the next start that runs the schedule takes it up"


async def test_a_read_older_than_the_next_nights_is_failed_by_the_restart_not_carried_on(js, monkeypatch):
    launched: list = []
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: launched.append(a))
    async with db.session_factory()() as session:
        entry = await _running_read(session, timedelta(0))
        now = entry.started_at + timedelta(hours=boards_night.RESUME_HOURS, minutes=1)
        await missions.settle_after_restart(session, OWNER, now=now)
        await session.commit()
        assert entry.outcome == "failed" and "cut short" in entry.summary and launched == []


async def test_a_resumed_read_takes_up_its_process_at_the_step_it_was_at_and_reads_no_board_again(
        js, board, monkeypatch):
    await _setup()
    reads: list = []
    real_read = board.read

    async def counted(fetch, config):
        reads.append(config)
        return await real_read(fetch, config)

    monkeypatch.setattr(board, "read", counted)
    real = signals.load_warn

    async def cut(session, owner, fetch, full=False):
        raise asyncio.CancelledError

    monkeypatch.setattr(signals, "load_warn", cut)
    async with db.session_factory()() as session:
        entry = await _running_read(session, timedelta(0))
        entry_id = entry.id
        await session.commit()
    boards_night.read_progress.READ_RUN.set(entry_id)
    with pytest.raises(asyncio.CancelledError):
        await boards_night.read_boards(OWNER, details_per_night=10)
    monkeypatch.setattr(signals, "load_warn", real)
    outcome, _summary, _counts = await boards_night.read_boards(OWNER, details_per_night=10, resume=True)
    assert outcome == "done" and len(reads) == 1, "the board the cut read had read is not read again"
    assert [i.status for i in await _instances()] == [COMPLETED], "one process, taken up where it stopped"


async def test_a_read_skips_the_sites_its_run_says_are_done(js, monkeypatch):
    asked: list = []

    class Each:
        async def read(self, fetch, board):
            asked.append(board["slug"])
            return Listing([_posting(f"{board['slug']}-1", "Director, Data Governance")])

    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: Each()})
    await _setup()
    async with db.session_factory()() as session:
        contoso = await employers.find_or_make(session, OWNER, "Contoso")
        contoso.ats, contoso.board = EXAMPLE_SYSTEM, {"slug": "contoso"}
        contoso.board_url, contoso.check_directly = "https://boards.example.test/contoso", True
        await where.seed_employer_sites(session, OWNER)
        fabrikam = await employers.find(session, OWNER, "Fabrikam")
        entry = await _running_read(session, timedelta(0), sites_done=[str(fabrikam.id)])
        await session.commit()
        entry_id = entry.id
    boards_night.read_progress.READ_RUN.set(entry_id)
    async with db.session_factory()() as session:
        read = await postings.refresh(session, OWNER, _fetch())
        await session.commit()
    assert asked == ["contoso"], "the site its run has done is not read again"
    assert read["employers"] == 2, "it still counts among the sites of the read"
    async with db.session_factory()() as session:
        done = (await session.get(MissionRun, entry_id)).started["sites_done"]
    assert len(done) == 2, "and the site it read is noted as done"


async def test_the_running_run_shows_how_many_sites_and_job_texts_it_has_done(js, board):
    await _setup()
    async with db.session_factory()() as session:
        entry = await _running_read(session, timedelta(0))
        await session.commit()
        entry_id = entry.id
    boards_night.read_progress.READ_RUN.set(entry_id)
    await boards_night.read_boards(OWNER, details_per_night=10)
    async with db.session_factory()() as session:
        view = missions.run_view(await session.get(MissionRun, entry_id), missions.READ)
    assert view["progress"] == {"done": 1, "target": 1, "text": "Sites 1 of 1 · job texts 2 of 2"}


# --- one part failing never stops the rest (a failure in the boards never stops the job board's read) ---






def test_the_board_feed_is_a_step_of_its_own_between_the_boards_and_following_the_postings():
    library = process.build_library().get(process.READ_BOARDS)
    step = library.elements["Read_board_feed"]
    assert (step.action, step.output) == ("jobs.read_board_feed", "board_feed")
    flows = {(f.source, f.target) for f in library.flows.values()}
    assert ("Read_boards", "Read_board_feed") in flows and ("Read_board_feed", "Follow_postings") in flows
    assert ("Read_boards", "Follow_postings") not in flows


async def test_the_read_says_whether_it_is_going_and_what_the_board_feed_did_in_it(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        read = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.READ))
        assert await boards_night.being_read(session, OWNER) is False
        entry = MissionRun(owner=OWNER, mission_id=read.id, started_by="schedule", outcome="running",
                           started={"reading": True, "board_feed": {"requests": 4}}, summary="Reading.")
        session.add(entry)
        await session.flush()
        assert await boards_night.being_read(session, OWNER) is True
        assert boards_night.board_feed_of(entry) == {"requests": 4}
        entry.outcome = "done"
        await session.flush()
        assert await boards_night.being_read(session, OWNER) is False
        assert boards_night.board_feed_of(MissionRun(started={})) is None
