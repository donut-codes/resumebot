"""The coverage comparison (the work queue spec E5): after a pass of the turn, did it change anything
while no to-do for its topic was posted since its previous pass? Only logged; the log is read at the phase's gate.
Everything is made up."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.jobsearch.models import Todo
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.work import queue
from app.jobsearch.work.queue import coverage
from tests.jobsearch.work_queue.support import OWNER, make_posting
from tests.support import EXAMPLE_SYSTEM, load_profile

LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def nothing_is_remembered():
    coverage.forget()
    yield
    coverage.forget()


async def _todo(topic: str, at: datetime, key: str = "") -> None:
    async with db.session_factory()() as session:
        session.add(Todo(owner=OWNER, topic=topic, key=key, posted_at=at, not_before=at))
        await session.commit()


def _window(previous: datetime | None, start: datetime) -> coverage.Window:
    return coverage.Window(previous, start)


def _minute_before(start: datetime) -> coverage.Window:
    return coverage.Window(start - timedelta(minutes=1), start)


def test_a_pass_that_changed_something_is_counted_from_what_it_returned():
    assert coverage.counted(3) == 3 and coverage.counted(True) == 1 and coverage.counted(0) == 0
    assert coverage.counted((2, 3)) == 5 and coverage.counted([uuid.uuid4(), uuid.uuid4()]) == 2
    assert coverage.counted({"looked_at": 4, "stopped": {}}) == 4
    assert coverage.counted(None) == 0 and coverage.counted({}) == 0


async def test_a_pass_that_changed_something_with_no_to_do_for_its_topic_is_a_miss(js, caplog):
    caplog.set_level(logging.INFO)
    start = datetime.now(UTC)
    assert await coverage.judge(OWNER, "advance", queue.ROLE_SETTLE, _window(start - timedelta(minutes=1), start), 2)
    assert coverage.misses == {"advance": 2}
    said = [r.getMessage() for r in caplog.records if "work-queue coverage" in r.getMessage()]
    assert said == ["work-queue coverage: advance changed 2 and no role.settle to-do was posted since its "
                    "previous pass"]


async def test_a_to_do_of_the_topic_posted_since_the_previous_pass_tells_the_pass(js):
    start = datetime.now(UTC)
    await _todo("role.settle", start - timedelta(seconds=30), key="a-role")
    assert not await coverage.judge(OWNER, "advance", queue.ROLE_SETTLE, _minute_before(start), 2)
    assert coverage.misses == {}


async def test_a_to_do_of_another_topic_or_posted_before_the_previous_pass_or_during_this_one_does_not_tell_it(js):
    start = datetime.now(UTC)
    await _todo("deck.dirty", start - timedelta(seconds=30))
    await _todo("role.settle", start - timedelta(minutes=5))  # before the previous pass began
    await _todo("role.settle", start + timedelta(seconds=5))  # this pass's own writes: the next window's
    assert await coverage.judge(OWNER, "advance", queue.ROLE_SETTLE, _window(start - timedelta(minutes=1), start), 1)


async def test_a_pass_that_changed_nothing_and_the_first_pass_after_a_start_are_not_compared(js):
    start = datetime.now(UTC)
    assert not await coverage.judge(OWNER, "advance", queue.ROLE_SETTLE, _minute_before(start), 0)
    assert not await coverage.judge(OWNER, "advance", queue.ROLE_SETTLE, _window(None, start), 5)
    assert coverage.misses == {}


async def test_a_pass_begun_again_has_a_window_from_the_start_of_the_one_before():
    first = coverage.begin(OWNER, "advance")
    second = coverage.begin(OWNER, "advance")
    other = coverage.begin(OWNER, "gate")
    assert first.previous is None and second.previous == first.start and other.previous is None


async def test_a_comparison_that_cannot_read_the_to_dos_never_stops_the_turn(js, monkeypatch, caplog):
    def broken(*_args):
        raise RuntimeError("the table is gone")

    monkeypatch.setattr(coverage, "told_statement", broken)
    start = datetime.now(UTC)
    assert not await coverage.judge(OWNER, "advance", queue.ROLE_SETTLE, _minute_before(start), 1)
    assert [r for r in caplog.records if "the comparison for advance failed" in r.getMessage()]


async def test_a_turn_logs_the_step_that_changed_something_nobody_told_it_about(js, caplog):
    """The real turn (`run_in_steps`): the first turn only marks where each step begins; a posting added by a writer
    that posts nothing (a statement of the test's own, which the tripwire does not watch) is sifted by the second, and
    the sift's topic has no to-do in its window."""
    caplog.set_level(logging.INFO)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    await pipeline.run_in_steps(OWNER)
    posting = make_posting()
    async with db.session_factory()() as session:
        from sqlalchemy import insert

        await session.execute(insert(type(posting)).values(
            id=uuid.uuid4(), owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id="e1", match_key="k",
            company="Fabrikam", title="Director of Programs", first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC)))
        await session.commit()
    await pipeline.run_in_steps(OWNER)
    assert coverage.misses.get("sift", 0) >= 1
    assert [r for r in caplog.records if "work-queue coverage: sift changed" in r.getMessage()]
