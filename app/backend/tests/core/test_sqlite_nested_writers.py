"""Nested writers. Postgres gave a task that held a transaction a
second connection for its side writes; SQLite has one writer turn per file, so a task that opens a second writing
session while its first transaction holds the turn waits for a turn only it could give back, and is told `Busy`
seconds later.
`db.NestedWriter` says so at once (in every test, and as a warning in the app), with where it was begun.
"""

import asyncio
import logging
import uuid

import pytest
from sqlalchemy import text

from app import db
from app.errors import Busy
from tests.core.test_sqlite_writer_turn import ADD, COUNT, engine, quick_engine  # noqa: F401  (fixtures, by name)


@pytest.fixture(autouse=True)
def nested_writers_the_test_made_on_purpose_are_not_failures():
    yield
    db.nested_caught.clear()


async def test_a_second_writer_inside_a_writers_transaction_raises_at_once(engine):  # noqa: F811
    async with db.session_factory()() as first:
        await first.execute(ADD, {"body": "first"})
        async with db.session_factory()() as second:
            with pytest.raises(db.NestedWriter, match="second writer inside a writer's transaction"):
                await second.execute(ADD, {"body": "second"})
        await first.commit()
    assert len(db.nested_caught) == 1


async def test_the_second_writer_that_begins_by_a_read_is_nested_too(engine):  # noqa: F811
    """A writer's transaction begins at its first statement, a read included (`BEGIN IMMEDIATE`)."""
    async with db.session_factory()() as first:
        await first.execute(COUNT)
        async with db.session_factory()() as second:
            with pytest.raises(db.NestedWriter):
                await second.execute(COUNT)


async def test_a_reader_inside_a_writers_transaction_is_not_nested(engine):  # noqa: F811
    async with db.session_factory()() as first:
        await first.execute(ADD, {"body": "first"})
        async with db.read_session_factory()() as reading:
            assert (await reading.execute(COUNT)).scalar_one() == 0
        await first.commit()
    assert not db.nested_caught


async def test_a_second_writer_after_the_first_commits_or_releases_is_not_nested(engine):  # noqa: F811
    async with db.session_factory()() as first:
        await first.execute(ADD, {"body": "first"})
        await db.release(first)
        async with db.session_factory()() as second:
            await second.execute(ADD, {"body": "second"})
            await second.commit()
        await first.execute(ADD, {"body": "third"})  # the first goes on, in a transaction of its own
        await first.commit()
    assert not db.nested_caught
    async with db.session_factory()() as session:
        assert (await session.execute(COUNT)).scalar_one() == 3


async def test_a_session_that_ended_its_transaction_with_a_rollback_holds_no_turn(engine):  # noqa: F811
    async with db.session_factory()() as first:
        await first.execute(ADD, {"body": "never kept"})
        await first.rollback()
        async with db.session_factory()() as second:
            await second.execute(ADD, {"body": "kept"})
            await second.commit()
    assert not db.nested_caught


async def test_a_writer_in_another_task_that_waits_its_turn_is_not_nested(engine):  # noqa: F811
    """Two tasks take turns: the second waits for the first, which is not waiting for it, even when the second was
    started from inside the first's transaction and so carries its context."""

    async def other() -> None:
        async with db.session_factory()() as second:
            await second.execute(ADD, {"body": "second"})
            await second.commit()

    async with db.session_factory()() as first:
        await first.execute(ADD, {"body": "first"})
        task = asyncio.create_task(other())
        await asyncio.sleep(0.05)
        assert not task.done()  # it is waiting for the first's turn
        await first.commit()
    await task
    assert not db.nested_caught
    async with db.session_factory()() as session:
        assert (await session.execute(COUNT)).scalar_one() == 2


async def test_outside_the_tests_a_nested_writer_is_logged_once_and_told_busy_when_its_turn_does_not_come(
        quick_engine, monkeypatch, caplog):  # noqa: F811
    monkeypatch.setattr(db, "strict_nested", False)
    db._nested_logged.clear()
    with caplog.at_level(logging.WARNING, logger="app.db"):
        for _ in range(2):
            async with db.session_factory()() as first:
                await first.execute(ADD, {"body": "first"})
                async with db.session_factory()() as second:
                    with pytest.raises(Busy):
                        await second.execute(text("SELECT 1"))
                await first.rollback()
    said = [r for r in caplog.records if "a second writer inside a writer's transaction" in r.message]
    assert len(said) == 1 and "test_sqlite_nested_writers.py" in said[0].message
    assert not db.nested_caught


# --- the sites that held a transaction and opened a second writer: each now ends the first --------------------------


async def _a_running_read():
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.jobsearch.missions import core as missions
    from app.jobsearch.models import Mission, MissionRun
    from tests.support import OWNER

    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        read = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.READ))
        entry = MissionRun(owner=OWNER, mission_id=read.id, started_by="schedule", outcome="running",
                           started_at=datetime(2026, 10, 20, 1, 0, tzinfo=UTC), summary="Reading.",
                           started={"reading": True})
        session.add(entry)
        await session.commit()
        return entry.id


async def test_a_note_of_the_nights_read_saves_the_steps_session_first_and_is_no_nested_writer(js):
    from sqlalchemy import select

    from app.jobsearch.models import Employer, MissionRun
    from app.jobsearch.postings import read_progress
    from tests.support import OWNER

    run_id = await _a_running_read()
    read_progress.READ_RUN.set(run_id)
    site = uuid.uuid4()
    async with db.session_factory()() as step:
        step.add(Employer(owner=OWNER, key="northwind", name="Northwind"))  # what the step wrote before the note
        await step.flush()
        await read_progress.note_sites(step, [site])  # a session of its own writes the note
    async with db.session_factory()() as session:
        assert (await session.get(MissionRun, run_id)).started["sites_done"] == [str(site)]
        assert await session.scalar(select(Employer.name).where(Employer.key == "northwind")) == "Northwind", (
            "what the step had written is saved with the note: what is noted as done is done on disk")


async def test_what_a_run_holds_is_read_without_the_turn_while_a_writer_holds_it(js):
    from app.jobsearch.postings import read_progress

    run_id = await _a_running_read()
    read_progress.READ_RUN.set(run_id)
    async with db.session_factory()() as step:
        await step.execute(text("SELECT 1"))  # the step's transaction holds the turn
        assert await read_progress.sites_done() == set()  # a reader's session: no nested writer, no wait
        assert await read_progress.details_before() == 0
        await step.rollback()




async def test_an_action_that_says_progress_with_work_unsaved_saves_it_first_and_nests_no_writer(js, monkeypatch):
    """The job's progress line is written in a session of its own so `act.job` can show it while the action runs: the
    action's own session ends its transaction first (a real run's writes are saved as it reports), where in a dry run,
    which rolls everything back at its end, nothing is said on the way."""
    from sqlalchemy import select

    from app.jobsearch.models import ActionRun, Employer
    from app.jobsearch.owner_actions import REGISTRY, Action, runner
    from app.jobsearch.owner_actions.registry import Args, Ctx, Outcome
    from app.settings import settings
    from tests.support import OWNER

    class NoArgs(Args):
        pass

    async def run(ctx: Ctx, args: NoArgs) -> Outcome:
        ctx.session.add(Employer(owner=OWNER, key="northwind", name="Northwind"))
        await ctx.session.flush()
        await ctx.say("one employer made")
        return Outcome({"done": 1}, did="made an employer", would="make an employer")

    monkeypatch.setitem(REGISTRY, "test.say", Action("test.say", "says", NoArgs, run, writes=True))
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(runner, "WAIT_SECONDS", 5.0)

    dry = await runner.do(OWNER, "test.say", {}, True)
    assert dry["state"] == "done" and dry["progress"] is None
    async with db.session_factory()() as session:
        assert await session.scalar(select(Employer.id).where(Employer.key == "northwind")) is None

    real = await runner.do(OWNER, "test.say", {}, False)
    assert real["state"] == "done" and real["ok"] is True
    async with db.session_factory()() as session:
        assert await session.scalar(select(Employer.name).where(Employer.key == "northwind")) == "Northwind"
        assert (await session.get(ActionRun, uuid.UUID(real["job"]))).progress == "one employer made"
