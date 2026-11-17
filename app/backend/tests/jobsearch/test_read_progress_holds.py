"""The board read's progress note holds no long turn: a resumed read already has its job-text total, so noting it again
writes nothing and takes no turn for a write."""

from datetime import UTC, datetime

from sqlalchemy import select, text

from app import db
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Mission, MissionRun
from app.jobsearch.postings import read_progress
from tests.support import OWNER


async def _a_running_read(started: dict):
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        read = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.READ))
        entry = MissionRun(owner=OWNER, mission_id=read.id, started_by="schedule", outcome="running",
                           started_at=datetime(2026, 10, 20, 1, 0, tzinfo=UTC), summary="Reading.", started=started)
        session.add(entry)
        await session.commit()
        return entry.id


async def _started(run_id) -> dict:
    async with db.session_factory()() as session:
        return dict((await session.get(MissionRun, run_id)).started)


async def test_a_total_that_is_already_noted_is_not_written_again_and_the_steps_turn_is_still_let_go(js, monkeypatch):
    """Old behaviour: every call ended the step's transaction, then opened a writer's session to read the run and write
    it back. New: the total a resumed read already has is read without the turn and nothing is written; the step's
    transaction is still ended (what it did is saved before the pages are read). The first call still writes."""
    run_id = await _a_running_read({"reading": True})
    read_progress.READ_RUN.set(run_id)
    async with db.session_factory()() as step:
        await step.execute(text("select 1"))
        await read_progress.set_details_total(step, 7)
        assert not step.in_transaction()
    assert (await _started(run_id))["details_total"] == 7, "the first time it is known, it is written"

    async def not_written(*_args):
        raise AssertionError("the run was written for a total it already has")

    monkeypatch.setattr(read_progress, "_change", not_written)
    async with db.session_factory()() as step:
        await step.execute(text("select 1"))
        await read_progress.set_details_total(step, 3)
        assert not step.in_transaction(), "the step's turn is let go all the same"
    assert (await _started(run_id))["details_total"] == 7, "fixed the first time it is known"


async def test_with_no_run_set_no_total_is_noted_and_nothing_is_written(js):
    read_progress.READ_RUN.set(None)
    async with db.session_factory()() as step:
        await read_progress.set_details_total(step, 7)
