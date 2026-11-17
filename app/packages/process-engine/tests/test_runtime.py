"""The engine's run-time promises under repeated and late events: an answer for a
step that is no longer waiting changes nothing and is told apart from a real failure; an answer is for one wait, so a
late answer from an earlier lap of a loop can't complete a later lap; `tick` fires each timer on its own, and one that
breaks fails only its instance."""

import pytest
from process_samples import LOOP, engine_for, open_waits, timed_engine
from support import FakeClock, sample_engine

from process_engine.errors import EngineError, NotWaiting
from process_engine.models import COMPLETED, FAILED, WAITING

# --- an answer for a step nobody is waiting at ---


async def test_a_second_answer_to_a_step_that_was_answered_is_not_waiting_and_changes_nothing(session):
    engine = sample_engine()
    instance = await engine.start(session, "intake", message="intake.requested")
    await engine.complete(session, instance.id, "Summary", {"text": "first"})
    with pytest.raises(NotWaiting, match="nothing is waiting to be completed at 'Summary'"):
        await engine.complete(session, instance.id, "Summary", {"text": "second"})
    assert instance.data["summary"] == {"text": "first"}  # the late one wrote nothing
    assert isinstance(NotWaiting("x"), EngineError)  # code that catches EngineError still does


async def test_an_answer_for_an_instance_that_has_ended_is_not_waiting(session):
    engine = engine_for(LOOP)
    instance = await engine.start(session, "again")
    await engine.complete(session, instance.id, "Ask", {"again": False})
    assert instance.status == COMPLETED
    with pytest.raises(NotWaiting, match="already completed"):
        await engine.complete(session, instance.id, "Ask", {"again": False})
    with pytest.raises(NotWaiting):
        await engine.fire_timer(session, instance.id, "Ask")
    with pytest.raises(NotWaiting):
        await engine.deliver_message(session, instance.id, "anything")


async def test_an_error_that_isnt_about_waiting_stays_an_engine_error(session):
    engine = sample_engine()
    instance = await engine.start(session, "intake", message="intake.requested")
    with pytest.raises(EngineError) as raised:
        await engine.complete(session, instance.id, "Summary", {}, error="x")
    assert not isinstance(raised.value, NotWaiting)


# --- an answer is for one wait ---


async def test_an_answer_names_the_wait_it_is_for(session):
    engine = engine_for(LOOP)
    instance = await engine.start(session, "again")
    [wait] = await open_waits(session, instance)
    await engine.complete(session, instance.id, "Ask", {"again": False}, wait_id=wait.id)
    assert instance.status == COMPLETED


async def test_a_late_answer_from_an_earlier_lap_does_not_complete_a_later_lap(session):
    engine = engine_for(LOOP)
    instance = await engine.start(session, "again")
    [lap_one] = await open_waits(session, instance)
    await engine.complete(session, instance.id, "Ask", {"again": True}, wait_id=lap_one.id)
    [lap_two] = await open_waits(session, instance)  # the loop asked again, at the same step
    assert lap_two.id != lap_one.id and lap_two.element_id == lap_one.element_id

    # the same answer arrives a second time, still addressed to lap one's wait
    with pytest.raises(NotWaiting):
        await engine.complete(session, instance.id, "Ask", {"again": False}, wait_id=lap_one.id)
    assert instance.status == WAITING
    assert [w.id for w in await open_waits(session, instance)] == [lap_two.id]  # lap two is still waiting

    await engine.complete(session, instance.id, "Ask", {"again": False}, wait_id=lap_two.id)
    assert instance.status == COMPLETED


async def test_an_answer_with_a_wait_id_that_is_not_one_completes_nothing(session):
    engine = engine_for(LOOP)
    instance = await engine.start(session, "again")
    with pytest.raises(NotWaiting):
        await engine.complete(session, instance.id, "Ask", {"again": False}, wait_id="not-an-id")
    other = await engine.start(session, "again")
    [theirs] = await open_waits(session, other)
    with pytest.raises(NotWaiting):  # a wait of another instance
        await engine.complete(session, instance.id, "Ask", {"again": False}, wait_id=theirs.id)
    assert instance.status == WAITING


async def test_a_message_names_the_wait_it_is_for(session):
    engine = sample_engine()
    instance = await engine.start(session, "intake", message="intake.requested")
    await engine.complete(session, instance.id, "Summary", {"text": "x"})
    [catch] = await open_waits(session, instance)
    assert catch.kind == "catch_message"
    with pytest.raises(NotWaiting):
        await engine.deliver_message(session, instance.id, "intake.confirmed", wait_id=instance.id)  # not this wait
    await engine.deliver_message(session, instance.id, "intake.confirmed", wait_id=catch.id)
    assert (await engine.where_is(session, instance.id))["step"] == "Settle"
    with pytest.raises(NotWaiting):  # the same message again: the wait is already answered
        await engine.deliver_message(session, instance.id, "intake.confirmed", wait_id=catch.id)


# --- tick ---


async def test_one_timer_that_breaks_fails_only_its_instance_and_the_others_still_fire(session):
    clock = FakeClock()
    engine = timed_engine(clock)
    broken = await engine.start(session, "timed", {"boom": True})
    fine = await engine.start(session, "timed", {"boom": False})
    clock.advance(hours=2)

    assert await engine.tick(session) == 1  # only the one that fired is counted

    assert fine.status == COMPLETED
    assert broken.status == FAILED
    assert "the timer at 'B' couldn't be fired" in broken.error and "KeyError" in broken.error
    assert "the late step broke" in broken.error
    assert await open_waits(session, broken) == []  # failed with nothing left open, so it isn't due again
    assert await engine.tick(session) == 0


async def test_tick_works_on_a_limited_number_of_instances_longest_overdue_first(session):
    clock = FakeClock()
    engine = timed_engine(clock)
    first = await engine.start(session, "timed", {"boom": False})
    clock.advance(minutes=30)
    second = await engine.start(session, "timed", {"boom": False})
    clock.advance(minutes=30)
    third = await engine.start(session, "timed", {"boom": False})
    clock.advance(hours=3)

    assert await engine.tick(session, limit=1) == 1
    assert [i.status for i in (first, second, third)] == [COMPLETED, WAITING, WAITING]  # the earliest due went first
    assert await engine.tick(session, limit=5) == 2
    assert [i.status for i in (first, second, third)] == [COMPLETED, COMPLETED, COMPLETED]


async def test_a_timer_that_breaks_is_undone_before_the_instance_is_failed(session):
    clock = FakeClock()
    engine = timed_engine(clock)
    broken = await engine.start(session, "timed", {"boom": True})
    clock.advance(hours=2)
    await engine.tick(session)
    trail = [(h.kind, h.from_element, h.to_element) for h in await engine.history(session, broken.id)]
    assert ("fire", "Work", "Late") not in trail  # the half-done move was rolled back, not left in the history
    assert trail[-1][0] == "fail"
