"""How long the re-gate holds the writer's turn: the classifier is trained on a reader, a
slice of roles writes its history lines in one flush, and a slice of roles is smaller than a slice of postings. Made-up
data only."""

from sqlalchemy import delete, event, select

from app import db
from app.jobsearch import as_read, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import classifier
from app.jobsearch.gating import regate
from app.jobsearch.models import Todo
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work.queue import topics as queue_topics
from app.models.work import TrailEvent
from tests.jobsearch.test_regate import (
    TODAY,
    _a_writer_gets_the_turn,
    gates,
    judge,
    posting,
    role,
)
from tests.jobsearch.work_queue.support import posted_now
from tests.support import OWNER, load_profile


async def test_a_regate_trains_the_classifier_without_holding_the_writer_turn(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(posting(fab, "low", 80_000))
        session.add(role("low", stages.SECOND_IMPRESSION, 80_000))
        await session.flush()
        await judge(session)
        await session.commit()
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(50_000), by="test")
        await session.commit()
    classifier.clear_cache()
    real, calls = classifier.examples, []

    async def examples(session, owner):
        """Another writer takes the turn now: it raises (`db.NestedWriter`) if the run's session still holds it."""
        await _a_writer_gets_the_turn()
        calls.append(owner)
        return await real(session, owner)

    monkeypatch.setattr(classifier, "examples", examples)
    async with db.session_factory()() as session:
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
    assert report.postings_released == 1
    assert calls, "the classifier was trained"


async def test_a_slice_writes_its_history_lines_in_one_flush(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        session.add_all([role(f"r{n:02}", stages.SECOND_IMPRESSION, 60_000) for n in range(40)])
        await session.commit()
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(95_000, location={"remote": {"on": False}}), by="test")
        await session.commit()
    async with db.session_factory()() as session:  # what setting up posted is not what the run posts
        await session.execute(delete(Todo))
        await session.commit()
    inserts: list[str] = []

    def noted(_conn, _cursor, statement, _parameters, _context, _many):
        if "INSERT INTO trail_events" in statement:
            inserts.append(statement)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        async with db.session_factory()() as session:
            report = await regate.run(session, OWNER, cause="a test", today=TODAY)
            await session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    assert report.roles_stopped == 40
    assert len(inserts) <= 2, f"{len(inserts)} statements wrote the lines"
    async with db.session_factory()() as session:
        lines = (await session.scalars(select(TrailEvent).where(TrailEvent.event == regate.STOPPED_EVENT)
                                       .order_by(TrailEvent.at, TrailEvent.id))).all()
    assert len(lines) == 40
    assert all(line.sentence.startswith("Gated out: ") for line in lines)
    settle = [key for topic, key in await posted_now() if topic == queue_topics.ROLE_SETTLE]
    assert len(settle) == 40, "one to-do to settle for each changed role, as when each line was flushed on its own"


async def test_a_regate_locks_the_roles_in_slices_of_the_role_size(js, monkeypatch):
    monkeypatch.setattr(regate, "ROLE_CHUNK", 50)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        session.add_all([role(f"r{n:03}", stages.SECOND_IMPRESSION, 60_000) for n in range(120)])
        await session.commit()
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(95_000, location={"remote": {"on": False}}), by="test")
        await session.commit()
    sizes: list[int] = []
    real = as_read.lock

    async def lock(session, model, rows, **kwargs):
        rows = list(rows)
        sizes.append(len(rows))
        return await real(session, model, rows, **kwargs)

    monkeypatch.setattr(as_read, "lock", lock)
    async with db.session_factory()() as session:
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
    assert report.roles_stopped == 120
    assert sizes == [50, 50, 20]
