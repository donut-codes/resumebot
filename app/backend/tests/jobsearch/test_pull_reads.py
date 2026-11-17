"""The pool's pull looks without the writer's turn. The look is a
reader's; only a pull that has a job to start takes the turn, and it does not look again under it: it reads again only
what makes the start safe (`pool.pull.start_next`, tests/jobsearch/test_pull_takes_no_turn_to_read.py). Every name here
is made up, and no test reaches the network."""

from app import db
from app.jobsearch.work import session_kinds
from tests.jobsearch.test_session_kinds import OWNER, _a_free_session_asks, analysis_setup


def _spy_looks(monkeypatch) -> list[bool]:
    """Whether each look of the pull was made in a reader's session."""
    looks: list[bool] = []
    real = session_kinds._look

    async def look(session, owner):
        looks.append(db.is_reader(session))
        return await real(session, owner)

    monkeypatch.setattr(session_kinds, "_look", look)
    return looks


async def test_a_pull_that_finds_work_waiting_looks_in_a_reader_and_takes_no_turn(js, monkeypatch):
    """Old: the look ran in the writer's session, which held the turn for it. New: in a reader's session; with work
    waiting for a free session nothing is started and no transaction is open."""
    await analysis_setup()
    await _a_free_session_asks()
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 1
        await session.commit()
    looks = _spy_looks(monkeypatch)
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 0, "the job started is waiting: nothing more is pulled"
        assert not session.in_transaction(), "nothing was written, so no turn was taken"
    assert looks == [True]


async def test_a_pull_that_starts_a_job_looks_once_in_a_reader_and_starts_it_under_the_turn(js, monkeypatch):
    """Old (to 2026-11-10): the reader found nothing waiting, the turn was taken and the whole look made once more in
    the writer's session, which held the turn for it. New (docs/PRD.md path 17, one writer): the look is made once, in
    the reader's session, and the job started in the writer's."""
    await analysis_setup()
    await _a_free_session_asks()
    looks = _spy_looks(monkeypatch)
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 1
        assert session.in_transaction(), "the job was started in the writer's session"
        await session.commit()
    assert looks == [True]
