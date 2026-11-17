"""The pool's pull decides in a reader's session and takes the writer's turn only to add what it decided, not to
read. A pull with nothing to start takes no turn at all; one that starts a job takes
the turn for the add and for the few facts that make it safe, read again under it: nothing is queued twice and nothing
against a profile that has changed since. Every name here is made up, and no test reaches the network."""

import contextlib
import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app import db
from app.agents import roster
from app.jobsearch import off_turn, stages
from app.jobsearch.models import Employer, Opportunity, ProfileVersion, RoleRank
from app.jobsearch.postings import cleansed
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, rank
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from tests.builders import analysis_setup
from tests.employer_builders import make_employer, make_role
from tests.jobsearch.test_rank import _jobs, _long, _profile, _role
from tests.support import OWNER, PROFILE

#: The tables `rank.unranked` read whole to pick a batch, before it asked the database to narrow: the roles, the copies
#: of their postings and the ranks already made. It now reads only the rows of the roles in play that hold no rank
#: (`cleansed.current_among`), on a reader; the tests here hold that the writer's turn sees no read of these tables.
WHOLE_TABLES = ("js_opportunities", "js_raw_postings", "js_cleansed_postings", "js_role_ranks")


@pytest.fixture
def writer(js):
    """What the writers did from here on: `turns`, one entry for each writer's turn that ended (`db.turn_ends`), and
    `statements`, every statement sent on a writer's connection (a reader's takes no turn and is left out)."""
    seen = SimpleNamespace(turns=[], statements=[])

    def ended(_waited: float, held: float | None) -> None:
        seen.turns.append(held)

    def sent(conn, _cursor, statement, _parameters, _context, _many) -> None:
        if not conn.get_execution_options().get("db_read_only"):
            seen.statements.append(" ".join(statement.split()))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", sent)
    db.turn_ends.append(ended)
    try:
        yield seen
    finally:
        db.turn_ends.remove(ended)
        event.remove(engine, "before_cursor_execute", sent)


async def _a_pool_with_a_free_session(monkeypatch, *, by_day: bool = True) -> None:
    """The owner's connection, and a free pool session that asked a moment ago and found nothing queued (what
    `session_kinds.hand_out` notes). `by_day`: outside the owner's off hours, so no search is the pull's to start."""
    if by_day:
        monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await session.commit()
    session_kinds._pool_asked[OWNER] = datetime.now(UTC)


async def _pull() -> int:
    """One pull as the work queue runs it (`handlers.pool_owed`): a session of its own, committed."""
    async with db.session_factory()() as session:
        started = await session_kinds.pull(session, OWNER)
        await session.commit()
        return started


def _between_the_plan_and_the_add(monkeypatch, change) -> None:
    """Run `change` once, when the pull's first reader's session has closed: after the pull decided and before it takes
    the writer's turn."""
    real = off_turn.reading
    entered: list[int] = []

    @contextlib.asynccontextmanager
    async def reading(session, **more):
        first = not entered
        entered.append(1)
        async with real(session, **more) as reader:
            yield reader
        if first:
            await change()

    monkeypatch.setattr(off_turn, "reading", reading)


async def test_a_pull_with_nothing_to_start_takes_no_writers_turn(js, monkeypatch, writer):
    await _profile()
    role = await _role("ranked", status=stages.FIRST_IMPRESSION)
    async with db.session_factory()() as session:
        profile = await rank.ensure_profile(session, OWNER)
        copies = await cleansed.current_for_roles(session, OWNER)
        session.add(RoleRank(owner=OWNER, role_id=role, cleansed_id=copies[role], profile_version=profile.version,
                             fit=60, why="A programs role.\nIt fits on duties."))
        await session.commit()
    await _a_pool_with_a_free_session(monkeypatch)
    writer.turns.clear()
    assert await _pull() == 0, "its one role holds a rank at the profile in force: there is nothing to start"
    assert writer.turns == [], "the pull only read, so it never took the writer's turn"
    assert await _jobs() == []


async def test_a_pull_whose_only_waiting_role_is_too_long_for_a_batch_takes_no_turn_and_queues_nothing(
        js, monkeypatch, writer, caplog):
    monkeypatch.setattr(rank, "BATCH_CHARS", 3_000)
    await _profile()
    big = await _role("big", text=_long("big", 5_000), status=stages.FIRST_IMPRESSION)
    await _a_pool_with_a_free_session(monkeypatch)
    writer.turns.clear()
    with caplog.at_level("WARNING", logger=rank.log.name):
        assert await _pull() == 0
    assert writer.turns == [], "nothing was added, so the writer's turn was never taken"
    assert await _jobs() == [], "the posting is not cut to fit and not queued"
    refusals = [r.getMessage() for r in caplog.records if str(big) in r.getMessage()]
    assert len(refusals) == 1 and "not cut" in refusals[0], "it is refused in the log as before"
    async with db.session_factory()() as session:
        saved = (await session.scalars(select(ProfileVersion).where(ProfileVersion.kind == rank.PROFILE_KIND))).all()
    assert saved == [], "nor was a version of the short profile saved for a job that was not made"


async def test_a_pull_with_rankable_roles_queues_one_job_and_reads_no_whole_table_under_its_turn(
        js, monkeypatch, writer):
    await _profile()
    roles = {str(await _role(slug, status=stages.FIRST_IMPRESSION)) for slug in ("a", "b")}
    await _a_pool_with_a_free_session(monkeypatch)
    picked_in: list[bool] = []
    real = rank.unranked

    async def unranked(session, owner, version):
        picked_in.append(db.is_reader(session))
        return await real(session, owner, version)

    monkeypatch.setattr(rank, "unranked", unranked)
    writer.turns.clear()
    writer.statements.clear()
    assert await _pull() == 1
    assert len(writer.turns) == 1, "one writer's turn: the add"
    under_the_turn = list(writer.statements)
    (job,) = await _jobs()
    assert {i["role_id"] for i in job.fields["items"]} == roles and job.fields["profile_version"] == 1
    assert picked_in == [True], "the roles to rank were picked once, in a reader's session"
    whole = [s for s in under_the_turn if s.startswith("SELECT") and any(t in s for t in WHOLE_TABLES)]
    assert whole == [], "the turn was held for a read of the roles, the copies or the ranks"
    assert any(s.startswith("INSERT INTO js_parse_jobs") for s in under_the_turn), "the add is the writer's"


async def test_a_rank_job_that_took_the_same_roles_between_the_plan_and_the_add_is_not_queued_again(js, monkeypatch):
    await _profile()
    for slug in ("a", "b"):
        await _role(slug, status=stages.FIRST_IMPRESSION)
    await _a_pool_with_a_free_session(monkeypatch)

    async def another_queues_them() -> None:
        async with db.session_factory()() as session:
            assert (await rank.queue(session, OWNER, jobs=1))["jobs"] == 1
            await session.commit()

    _between_the_plan_and_the_add(monkeypatch, another_queues_them)
    assert await _pull() == 0, "an open rank job holds the roles the pull planned: it adds nothing"
    assert len(await _jobs()) == 1, "no second job for the same roles"


async def test_a_profile_changed_between_the_plan_and_the_add_queues_nothing_against_the_old_version(
        js, monkeypatch):
    await _profile()
    await _role("a", status=stages.FIRST_IMPRESSION)
    async with db.session_factory()() as session:
        assert (await rank.ensure_profile(session, OWNER)).version == 1
        await session.commit()
    await _a_pool_with_a_free_session(monkeypatch)

    async def the_owner_saves_a_title() -> None:
        async with db.session_factory()() as session:
            saved = copy.deepcopy(PROFILE["criteria"])
            saved["target_roles"][0]["titles"].append("Head of Programs")
            await profile_store.save(session, OWNER, "criteria", saved, by="test")
            await session.commit()

    _between_the_plan_and_the_add(monkeypatch, the_owner_saves_a_title)
    assert await _pull() == 0, "the profile the batch was planned against is no longer the one in force"
    assert await _jobs() == [], "nothing is queued against the old version"
    session_kinds._pool_asked[OWNER] = datetime.now(UTC)
    assert await _pull() == 1, "the next pull plans again"
    (job,) = await _jobs()
    assert job.fields["profile_version"] == 2 and "Head of Programs" in job.text


async def test_a_role_put_in_the_analysts_line_between_the_plan_and_the_add_is_not_started_again(js, monkeypatch):
    ids = await analysis_setup()
    await _a_pool_with_a_free_session(monkeypatch)

    async def the_owner_runs_the_researcher() -> None:
        async with db.session_factory()() as session:
            for role in await analysis.picks(session, OWNER, 1):
                await analysis.start(session, OWNER, role)
            await session.commit()

    _between_the_plan_and_the_add(monkeypatch, the_owner_runs_the_researcher)
    assert await _pull() == 0, "a role waits for a session already: the pull adds none behind it"
    async with db.session_factory()() as session:
        waiting = (await session.scalars(select(Opportunity.id).where(
            Opportunity.analysis_state == analysis.WAITING))).all()
    assert len(waiting) == 1 and waiting[0] in ids.values(), "one role is with the analyst, started once"


async def test_an_employer_put_with_the_researcher_between_the_plan_and_the_add_is_not_started_again(js, monkeypatch):
    await _profile()
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Initech")
        session.add(make_role(employer, "Lead Programs", status=stages.READY_TO_LAUNCH))  # past its own research
        await session.commit()
        employer_id = employer.id
    await _a_pool_with_a_free_session(monkeypatch)
    started: list[object] = []

    async def the_deck_asks_for_its_research() -> None:
        async with db.session_factory()() as session:
            row = await session.get(Employer, employer_id)
            started.append((await employer_research.start(session, OWNER, row, for_deck=True)).research_instance_id)
            await session.commit()

    _between_the_plan_and_the_add(monkeypatch, the_deck_asks_for_its_research)
    assert await _pull() == 0, "the employer the pull planned is with the researcher already: it starts nothing"
    async with db.session_factory()() as session:
        row = await session.get(Employer, employer_id)
        assert row.research_state == employer_research.WAITING and [row.research_instance_id] == started, (
            "its research was started once, by the deck")


async def test_a_pull_inside_an_open_transaction_still_reads_again_what_makes_the_start_safe(js, monkeypatch):
    """Old: a pull in a session that already held something (an open transaction) started whatever it had picked, with
    no second look. New (owner decision 2026-11-14): the same safe-start checks run on every
    path, so a role that is no longer one to start is not started."""
    from app.jobsearch.pool import core as pool_core

    ids = await analysis_setup()
    await _a_pool_with_a_free_session(monkeypatch)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.analysis_state = analysis.DONE
        await session.commit()

    async def picks(*args, **kwargs):
        return [role]  # picked a moment ago, and no longer one to start

    monkeypatch.setattr(pool_core, "_research_picks", picks)
    async with db.session_factory()() as session:
        await session.execute(select(1))  # the session holds a transaction: the pull reads and starts in it
        assert await session_kinds.pull(session, OWNER) == 0
        await session.commit()
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, ids["Fabrikam"])).analysis_state == analysis.DONE
