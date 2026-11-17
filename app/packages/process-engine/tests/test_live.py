"""A failure nobody expected in a live attempt: the work is undone, the run is held where it was, nothing is cancelled
and no other code acts; the app's `config.on_drive_failure` hook is told, and says so to the person. With no hook the
work is still undone. `config.on_drive_success` is told when a live attempt worked. A returned instance that ended
failed is a failure too, and an answer for a step nobody waits at is not."""

import uuid

import pytest
from process_samples import LOOP
from sqlalchemy import select
from support import CATALOG

from process_engine import config as engine_config
from process_engine import live
from process_engine.errors import NotWaiting
from process_engine.loader import ProcessLibrary
from process_engine.models import CANCELLED, COMPLETED, FAILED, WAITING, ProcInstance
from process_engine.registry import ActionRegistry

ENTITY = uuid.UUID("6f1c2f0e-8b0a-4f43-9c53-0d1d5a0a1b11")
KEY = "again"


def subject_for(entity: uuid.UUID, thing: str = "1") -> str:
    return f"look:{entity}:{thing}"


def register_again(retry=None):
    library = ProcessLibrary()
    library.load(LOOP, catalog=CATALOG, registry=ActionRegistry())

    async def hand_out(session, instance, wait):
        return None

    return live.register_drivable(KEY, "Ask again", lambda: library, handouts={"user": hand_out}, retry=retry)


class Hooks:
    """What the app's two hooks were told, in order."""

    def __init__(self):
        self.failed: list[tuple] = []
        self.worked: list[tuple] = []

    async def on_failure(self, session, entity_id, key, what, exc, subject=None):
        self.failed.append((entity_id, key, what, exc, subject))

    async def on_success(self, session, entity_id, key, what, subject=None):
        self.worked.append((entity_id, key, what, subject))


@pytest.fixture
def hooks():
    seen = Hooks()
    engine_config.configure(on_drive_failure=seen.on_failure, on_drive_success=seen.on_success)
    return seen


async def runs(session, subject: str) -> list[ProcInstance]:
    return list((await session.scalars(select(ProcInstance).where(ProcInstance.subject == subject))).all())


# --- a failure is held, not handed on ---


async def test_a_failure_undoes_the_work_cancels_nothing_and_tells_the_app_once(session, hooks):
    register_again()
    engine = live.live_engine(KEY)
    waiting = await engine.start(session, KEY, subject=subject_for(ENTITY, "waiting"))
    seen_by_hook: list[int] = []

    async def hook(session, entity_id, key, what, exc, subject=None):
        # the work is already undone when the app is told
        seen_by_hook.append(len(await runs(session, subject_for(ENTITY, "new"))))
        await hooks.on_failure(session, entity_id, key, what, exc, subject)

    engine_config.configure(on_drive_failure=hook)

    async def work():
        await engine.start(session, KEY, subject=subject_for(ENTITY, "new"))
        raise RuntimeError("the engine broke")

    done = await live.attempt(session, ENTITY, KEY, "start a look", work, subject="look:7")

    assert done.ok is False
    assert [(e, k, w, str(x), s) for e, k, w, x, s in hooks.failed] == [
        (ENTITY, KEY, "start a look", "the engine broke", "look:7")
    ]
    assert isinstance(hooks.failed[0][3], RuntimeError)
    assert seen_by_hook == [0]
    assert await runs(session, subject_for(ENTITY, "new")) == []  # rolled back
    await session.refresh(waiting)
    assert waiting.status == WAITING  # the run that was waiting is still waiting, not cancelled
    assert hooks.worked == []


async def test_without_the_hook_the_work_is_still_undone_and_nothing_is_cancelled(session):
    register_again()
    engine = live.live_engine(KEY)
    waiting = await engine.start(session, KEY, subject=subject_for(ENTITY, "waiting"))

    async def broken():
        await engine.start(session, KEY, subject=subject_for(ENTITY, "new"))
        raise RuntimeError("the engine broke")

    done = await live.attempt(session, ENTITY, KEY, "start a look", broken)
    assert not done.ok
    assert await runs(session, subject_for(ENTITY, "new")) == []  # rolled back
    await session.refresh(waiting)
    assert waiting.status == WAITING


@pytest.mark.parametrize(("status", "error"), [(FAILED, "it broke"), (CANCELLED, None)])
async def test_work_that_returns_an_instance_that_failed_is_held_too(session, hooks, status, error):
    register_again()

    async def work():
        return ProcInstance(process_key=KEY, file_hash="x", status=status, error=error)

    done = await live.attempt(session, ENTITY, KEY, "start a look", work)
    assert not done.ok
    assert len(hooks.failed) == 1 and isinstance(hooks.failed[0][3], live.DriveFailed)
    assert str(hooks.failed[0][3]) == (error or f"the process {status}")


async def test_work_that_commits_as_it_goes_is_held_too(session, hooks):
    register_again()

    async def broken():
        raise RuntimeError("the runner was unreachable")

    done = await live.attempt(session, ENTITY, KEY, "hand a look to a specialist", broken, savepoint=False)
    assert not done.ok and len(hooks.failed) == 1
    assert hooks.failed[0][2] == "hand a look to a specialist" and hooks.failed[0][4] is None


async def test_work_that_returns_a_finished_instance_is_a_good_attempt(session, hooks):
    register_again()

    async def work():
        return ProcInstance(process_key=KEY, file_hash="x", status=COMPLETED)

    done = await live.attempt(session, ENTITY, KEY, "start a look", work)
    assert done.ok and done.value.status == COMPLETED
    assert hooks.failed == [] and len(hooks.worked) == 1


async def test_an_answer_for_a_step_nobody_waits_at_still_goes_up_and_tells_the_app_nothing(session, hooks):
    register_again()
    engine = live.live_engine(KEY)
    run = await engine.start(session, KEY, subject=subject_for(ENTITY))

    async def answer():
        return await engine.complete(session, run.id, "Ask", {"again": False})

    first = await live.attempt(session, ENTITY, KEY, "take the answer", answer, subject="look:1")
    assert first.ok and run.status == COMPLETED
    with pytest.raises(NotWaiting):
        await live.attempt(session, ENTITY, KEY, "take the answer", answer, subject="look:1")
    assert hooks.failed == []
    assert hooks.worked == [(ENTITY, KEY, "take the answer", "look:1")]  # only the first one worked


# --- a hook that breaks never takes its caller down ---


async def test_a_failure_hook_that_raises_is_logged_not_raised_and_leaves_nothing_half_written(session):
    register_again()

    async def bad_hook(session, entity_id, key, what, exc, subject=None):
        session.add(ProcInstance(process_key=KEY, file_hash="x", status=COMPLETED, subject=subject_for(ENTITY, "half")))
        await session.flush()
        raise RuntimeError("the card could not be raised")

    engine_config.configure(on_drive_failure=bad_hook)

    async def broken():
        raise RuntimeError("the engine broke")

    done = await live.attempt(session, ENTITY, KEY, "start a look", broken)
    assert not done.ok
    assert await runs(session, subject_for(ENTITY, "half")) == []  # the hook's own savepoint was rolled back


# --- a success is told too ---


async def test_a_success_tells_the_app_after_the_work_and_a_failure_does_not(session, hooks):
    register_again()
    engine = live.live_engine(KEY)

    async def good():
        return await engine.start(session, KEY, subject=subject_for(ENTITY, "good"))

    async def broken():
        raise RuntimeError("no")

    assert (await live.attempt(session, ENTITY, KEY, "start a look", good, subject="look:good")).ok
    assert hooks.worked == [(ENTITY, KEY, "start a look", "look:good")]
    assert not (await live.attempt(session, ENTITY, KEY, "start a look", broken, subject="look:bad")).ok
    assert len(hooks.worked) == 1 and len(hooks.failed) == 1


async def test_a_success_hook_that_raises_does_not_undo_the_work(session, hooks):
    register_again()
    engine = live.live_engine(KEY)

    async def bad_success(session, entity_id, key, what, subject=None):
        raise RuntimeError("could not close the card")

    engine_config.configure(on_drive_success=bad_success)

    async def good():
        return await engine.start(session, KEY, subject=subject_for(ENTITY, "kept"))

    done = await live.attempt(session, ENTITY, KEY, "start a look", good)
    assert done.ok and done.value.status == WAITING
    assert len(await runs(session, subject_for(ENTITY, "kept"))) == 1


# --- the retry a process registers ---


async def test_a_process_can_register_how_to_run_its_entry_again(session):
    async def again(session, entity_id, subject):
        return None

    assert register_again(retry=again).retry is again
    assert live.drivable(KEY).retry is again
    assert register_again().retry is None  # the default: no entry to run again
