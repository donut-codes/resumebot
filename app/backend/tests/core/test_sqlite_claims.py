"""Claims on SQLite: a waiting row is taken by one conditional
`UPDATE ... WHERE id IN (SELECT ... LIMIT n) RETURNING ...` (`work/claim.py`), which the one writer runs whole, so
sessions asking at once are each handed a different row and none is lost. These make their own file in a temporary
folder (`lite`). Everything here is made up.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app import db
from app.jobsearch import stages
from app.jobsearch.jobs import check, parse
from app.jobsearch.models import CheckJob, Opportunity, ParseJob, Todo
from app.jobsearch.research import analysis, role_summary
from app.jobsearch.work.claim import claim, claim_one
from app.timeutil import owner_today
from tests.support import OWNER

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


async def waiting_jobs(count: int, *, owner: str = OWNER, kind: str = parse.ParseKind.POSTING.value) -> list[uuid.UUID]:
    """`count` waiting parse jobs, each made a minute after the one before, and their ids oldest first."""
    ids: list[uuid.UUID] = []
    async with db.session_factory()() as session:
        for n in range(count):
            job = ParseJob(owner=owner, kind=kind, text=f"posting {n}", created_at=NOW + timedelta(minutes=n))
            session.add(job)
            await session.flush()
            ids.append(job.id)
        await session.commit()
    return ids


def oldest_waiting(owner: str = OWNER):
    return (select(ParseJob).where(ParseJob.owner == owner, ParseJob.state == parse.WAITING)
            .order_by(ParseJob.created_at))


async def test_fifty_claimers_of_twenty_waiting_rows_take_each_row_once_and_lose_none(lite):
    ids = await waiting_jobs(20)

    async def claimer(n: int) -> uuid.UUID | None:
        async with db.session_factory()() as session:
            job = await claim_one(session, oldest_waiting(), state=parse.READING, taken_at=NOW)
            await session.commit()
            return job.id if job is not None else None

    got = await asyncio.gather(*(claimer(n) for n in range(50)))

    taken = [g for g in got if g is not None]
    assert sorted(taken) == sorted(ids), "every waiting row was claimed, and no row twice"
    assert len(taken) == 20 and got.count(None) == 30, "the other thirty were told nothing waits"
    async with db.session_factory()() as session:
        states = (await session.scalars(select(ParseJob.state))).all()
    assert states == [parse.READING] * 20


async def test_claimers_asking_for_several_rows_at_once_share_them_without_overlap(lite):
    ids = await waiting_jobs(20)

    async def claimer() -> list[uuid.UUID]:
        async with db.session_factory()() as session:
            rows = await claim(session, oldest_waiting().limit(3), state=parse.READING, taken_at=NOW)
            await session.commit()
            return [r.id for r in rows]

    got = await asyncio.gather(*(claimer() for _ in range(10)))

    flat = [i for batch in got for i in batch]
    assert sorted(flat) == sorted(ids) and len(flat) == len(set(flat)), "twenty rows, each in exactly one batch"
    assert sorted(len(b) for b in got) == [0, 0, 0, 2, 3, 3, 3, 3, 3, 3], "six of three, one of two, three of none"


async def test_a_claim_takes_the_oldest_first_and_only_a_waiting_row(lite):
    ids = await waiting_jobs(3)
    async with db.session_factory()() as session:
        first = await claim_one(session, oldest_waiting(), state=parse.READING, taken_at=NOW)
        second = await claim_one(session, oldest_waiting(), state=parse.READING, taken_at=NOW)
        assert (first.id, second.id) == (ids[0], ids[1])
        assert first.state == parse.READING and first.taken_at == NOW, "the row comes back as it now stands"
        again = await claim_one(session, select(ParseJob).where(ParseJob.id == ids[0], ParseJob.state == parse.WAITING),
                                state=parse.READING, taken_at=NOW)
        assert again is None, "a row already taken is not waiting for a second claim"
        await session.commit()


async def test_a_claim_of_nothing_answers_empty_and_changes_nothing(lite):
    await waiting_jobs(2, owner="someone-else")
    async with db.session_factory()() as session:
        assert await claim(session, oldest_waiting(), state=parse.READING, taken_at=NOW) == []
        await session.commit()
        states = (await session.scalars(select(ParseJob.state))).all()
    assert states == [parse.WAITING] * 2


async def test_a_session_that_already_holds_the_row_sees_the_claim_in_it(lite):
    (job_id,) = await waiting_jobs(1)
    async with db.session_factory()() as session:
        held = await session.get(ParseJob, job_id)
        assert held.state == parse.WAITING
        taken = await claim_one(session, oldest_waiting(), state=parse.READING, taken_at=NOW)
        assert taken is held and held.state == parse.READING, "the copy the session had is the one refreshed"
        await session.commit()


async def test_a_job_taken_posts_what_the_same_change_saved_through_the_session_posts(lite):
    (job_id,) = await waiting_jobs(1)
    async with db.session_factory()() as session:
        await session.execute(Todo.__table__.delete())
        await session.commit()
    async with db.session_factory()() as session:
        await claim_one(session, oldest_waiting(), state=parse.READING, taken_at=NOW)
        await session.commit()
        by_claim = sorted((await session.execute(select(Todo.topic, Todo.key))).all())
        await session.execute(Todo.__table__.delete())
        await session.execute(ParseJob.__table__.update().values(state=parse.WAITING))
        await session.commit()
    async with db.session_factory()() as session:
        job = await session.get(ParseJob, job_id)
        job.state = parse.READING
        await session.commit()
        by_save = sorted((await session.execute(select(Todo.topic, Todo.key))).all())
    assert by_claim and by_claim == by_save
    assert await _count_todos() == len(by_save)


async def _count_todos() -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(Todo))


async def test_fifty_parser_sessions_asking_at_once_are_each_handed_a_different_posting(lite):
    ids = await waiting_jobs(20)

    async def ask(n: int) -> dict:
        async with db.session_factory()() as session:
            answer = await parse.take_next(session, OWNER, NOW, session_id=f"session-{n}")
            await session.commit()
            return answer

    answers = await asyncio.gather(*(ask(n) for n in range(50)))

    handed = [a["id"] for a in answers if not a.get("done")]
    assert sorted(handed) == sorted(str(i) for i in ids) and len(set(handed)) == 20
    assert sum(1 for a in answers if a.get("done")) == 30, "the rest were told to stop"
    async with db.session_factory()() as session:
        jobs = (await session.scalars(select(ParseJob))).all()
    assert {j.state for j in jobs} == {parse.READING} and all(j.taken_at == NOW for j in jobs)
    assert sorted(j.fields["session_id"] for j in jobs) == sorted(
        f"session-{n}" for n in range(50) if answers[n].get("id")), "each job names the one session that took it"


async def test_fifty_checker_sessions_asking_at_once_take_one_job_between_them(lite):
    """A checker holds the owner's working job, so a session that asks while one is taken is handed that one: the
    waiting job is taken once, however many ask at the same moment."""
    async with db.session_factory()() as session:
        for n in range(20):
            session.add(CheckJob(owner=OWNER, scope=check.POSTING, text=f"posting {n}", state=check.WAITING,
                                 created_at=NOW + timedelta(minutes=n), looks="work", items=[], answers={}))
        await session.commit()

    async def ask(n: int) -> dict:
        async with db.session_factory()() as session:
            answer = await check.take_next(session, OWNER, NOW, session_id=f"session-{n}")
            await session.commit()
            return answer

    answers = await asyncio.gather(*(ask(n) for n in range(50)))

    assert len({a["id"] for a in answers}) == 1 and not any(a.get("done") for a in answers)
    async with db.session_factory()() as session:
        states = sorted((await session.scalars(select(CheckJob.state))).all())
    assert states == [check.WAITING] * 19 + [check.WORKING]


async def test_fifty_analyst_sessions_asking_for_a_summary_are_each_handed_a_different_role(lite):
    asked = role_summary._now_text(NOW - timedelta(hours=1))
    async with db.session_factory()() as session:
        for n in range(20):
            session.add(Opportunity(
                owner=OWNER, slug=f"role-{n}", company=f"Firm {n}", title=f"Lead {n}", origin="scout",
                match_key=f"firm {n}|lead {n}", status=stages.SECOND_IMPRESSION, found=owner_today(),
                analysis_state=analysis.DONE, analysed_at=NOW - timedelta(days=n),
                analysis={role_summary.WANTED_KEY: asked}))
        await session.commit()

    async def ask(n: int):
        async with db.session_factory()() as session:
            role = await role_summary.claim_next(session, OWNER, NOW, session_id=f"session-{n}")
            await session.commit()
            return role.id if role is not None else None

    got = await asyncio.gather(*(ask(n) for n in range(50)))

    taken = [g for g in got if g is not None]
    assert len(taken) == 20 and len(set(taken)) == 20 and got.count(None) == 30
    async with db.session_factory()() as session:
        roles = (await session.scalars(select(Opportunity))).all()
    assert all((r.analysis or {}).get(role_summary.TRIES_KEY) == 1 for r in roles), "each was handed out once"
    assert all((r.analysis or {}).get(role_summary.CLAIMED_KEY) == role_summary._now_text(NOW) for r in roles)
    assert sorted(r.analysis[role_summary.CLAIMED_BY_KEY] for r in roles) == sorted(
        f"session-{n}" for n in range(50) if got[n] is not None)
    assert all(r.analysis[role_summary.WANTED_KEY] == asked for r in roles), "what else the analysis held is kept"


# --- the sites that read, then wrote: one conditional statement each -------------------------------------------


async def _roles_for_the_order(count: int) -> list[uuid.UUID]:
    day = owner_today()
    async with db.session_factory()() as session:
        roles = [Opportunity(owner=OWNER, slug=f"order-{n}", company=f"Co {n}", title=f"Role {n}",
                             status=stages.READY_TO_LAUNCH, origin="scout", match_key=f"co {n}|role {n}",
                             posted=day, found=day) for n in range(count)]
        session.add_all(roles)
        await session.commit()
        return [r.id for r in roles]


def _numbers(points: int):
    from app.jobsearch.pipeline.stored_order import Stored

    return Stored(points, owner_today(), NOW, None)


async def test_the_stored_order_is_written_in_one_statement_when_every_role_is_there(js):
    from app.jobsearch.pipeline import stored_order

    ids = await _roles_for_the_order(5)
    async with db.session_factory()() as session:
        seen: list[str] = []
        engine = db.engine().sync_engine
        from sqlalchemy import event

        def noted(_conn, _cursor, statement, _parameters, _context, _many):
            seen.append(statement.split()[0].upper())

        event.listen(engine, "before_cursor_execute", noted)
        try:
            done = await stored_order._write(session, [(i, _numbers(10 + n)) for n, i in enumerate(ids)])
        finally:
            event.remove(engine, "before_cursor_execute", noted)
        await session.commit()
    assert (done.written, done.skipped) == (5, set())
    assert [s for s in seen if s != "BEGIN"] == ["UPDATE"], "no read of which roles are there first"
    async with db.session_factory()() as session:
        points = dict((await session.execute(select(Opportunity.id, Opportunity.queue_points))).all())
    assert points == {i: 10 + n for n, i in enumerate(ids)}


async def test_a_role_that_is_gone_is_left_out_of_the_stored_order_and_named(js):
    from app.jobsearch.pipeline import stored_order

    ids = await _roles_for_the_order(3)
    gone = uuid.uuid4()
    async with db.session_factory()() as session:
        done = await stored_order._write(session, [(ids[0], _numbers(1)), (gone, _numbers(2)), (ids[1], _numbers(3))])
        await session.commit()
    assert done.written == 2 and done.skipped == {gone}
    async with db.session_factory()() as session:
        points = dict((await session.execute(select(Opportunity.id, Opportunity.queue_points))).all())
    assert points[ids[0]] == 1 and points[ids[1]] == 3 and points[ids[2]] != 2


async def test_ten_sweeps_asking_to_keep_the_same_role_sourced_are_handed_it_once(js):
    from app.jobsearch.postings import sourced

    day = owner_today()
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="sweep", company="Co", title="Role", status=stages.FIRST_IMPRESSION,
                           origin="scout", match_key="co|role", posted=day, found=day, sourced_at=None)
        session.add(role)
        await session.commit()
        role_id = role.id

    async def sweep() -> bool:
        async with db.session_factory()() as session:
            got = await sourced._claim_unsourced(session, role_id)
            await session.commit()
            return got is not None

    taken = await asyncio.gather(*(sweep() for _ in range(10)))
    assert taken.count(True) == 1
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, role_id)).sourced_at is not None


async def test_a_role_out_of_play_or_already_sourced_is_not_claimed_for_sourcing(js):
    from app.jobsearch.postings import sourced

    day = owner_today()
    async with db.session_factory()() as session:
        done = Opportunity(owner=OWNER, slug="done", company="Co", title="Done", status=stages.FIRST_IMPRESSION,
                           origin="scout", match_key="co|done", posted=day, found=day)  # Sourced: tests make them so
        out = Opportunity(owner=OWNER, slug="out", company="Co", title="Out", status=stages.GATED_OUT, origin="scout",
                          match_key="co|out", posted=day, found=day, sourced_at=None)
        session.add_all([done, out])
        await session.commit()
        assert await sourced._claim_unsourced(session, done.id) is None
        assert await sourced._claim_unsourced(session, out.id) is None
