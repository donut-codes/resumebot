"""`lifecycle.settle` writes its stage moves in slices of `MOVE_SLICE` roles, each locked, written and committed in a
turn of its own, so no turn holds the file's one writer for all of a pass's moves. The result is the same as one
batch. Made-up data."""

from datetime import UTC, datetime

from sqlalchemy import delete, select, update

from app import db
from app.jobsearch import as_read, stages
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import lifecycle
from app.models.work import TrailEvent
from tests.builders import rank_roles
from tests.jobsearch.test_lifecycle import _role
from tests.support import OWNER, PROFILE, load_profile

SLUGS = [f"r{n}" for n in range(7)]


async def _ready_roles_with_no_read() -> None:
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        roles = [_role(slug, stages.READY_TO_LAUNCH, go_opened_at=now) for slug in SLUGS]
        session.add_all(roles)
        await session.flush()
        await rank_roles(session, {r.id: 50 for r in roles})
        await session.commit()


async def _outcome() -> tuple[dict[str, str], list[tuple[str, str]]]:
    async with db.session_factory()() as session:
        status = {r.slug: r.status for r in await session.scalars(
            select(Opportunity).execution_options(populate_existing=True))}
        lines = sorted((await session.execute(select(TrailEvent.event, TrailEvent.sentence))).all())
    return status, [tuple(line) for line in lines]


async def _put_back() -> None:
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).values(status=stages.READY_TO_LAUNCH))
        await session.execute(delete(TrailEvent).where(TrailEvent.event == "left_ready_unread"))
        await session.commit()


async def _ranked() -> set:
    async with db.session_factory()() as session:
        return set(await session.scalars(select(Opportunity.id)))


async def _settle() -> int:
    ranked = await _ranked()
    async with db.session_factory()() as session:
        moved = await lifecycle.settle(session, OWNER, ranked)
        await session.commit()
    return moved


async def test_moves_beyond_a_slice_are_written_in_several_turns_with_the_result_of_one_batch(js, monkeypatch):
    await _ready_roles_with_no_read()
    real, locks, releases = as_read.lock, [], []
    real_release = db.release_if_free

    async def lock(session, model, rows, **more):
        if model is Opportunity:
            locks.append(len(rows))
        return await real(session, model, rows, **more)

    async def release(session):
        releases.append(1)
        return await real_release(session)

    monkeypatch.setattr(as_read, "lock", lock)
    monkeypatch.setattr(db, "release_if_free", release)

    monkeypatch.setattr(lifecycle, "MOVE_SLICE", 3)
    assert await _settle() == 7
    assert locks == [3, 3, 1], "a lock per slice, in order"
    assert len(releases) >= 3
    sliced = await _outcome()

    await _put_back()
    locks.clear()
    monkeypatch.setattr(lifecycle, "MOVE_SLICE", 1000)
    assert await _settle() == 7
    assert locks == [7]
    assert await _outcome() == sliced
    assert sum(1 for event, _ in sliced[1] if event == "left_ready_unread") == 7
    assert set(sliced[0].values()) == {stages.FIRST_IMPRESSION}


async def test_in_a_savepoint_the_slices_stay_one_transaction(js, monkeypatch):
    await _ready_roles_with_no_read()
    monkeypatch.setattr(lifecycle, "MOVE_SLICE", 3)
    ranked = await _ranked()
    async with db.session_factory()() as session:
        nested = await session.begin_nested()
        assert await lifecycle.settle(session, OWNER, ranked) == 7
        await nested.rollback()  # one savepoint holds every slice: nothing was committed by the slices
        await session.rollback()
    status, lines = await _outcome()
    assert set(status.values()) == {stages.READY_TO_LAUNCH} and not lines
