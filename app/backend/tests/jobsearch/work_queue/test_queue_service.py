"""The to-do queue's service (the work queue spec C2, C3, C9, E2): a to-do is a row written in its writer's
own transaction, and goes with it; a write nobody posted for is caught. Everything is made up."""

import logging
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import db
from app.jobsearch.models import JobPosting, Opportunity, Todo
from app.jobsearch.work import queue
from app.jobsearch.work.queue import service
from tests.jobsearch.work_queue.support import OWNER, make_role, posted, posted_now


@pytest.fixture(autouse=True)
def statements_a_test_makes_are_watched_here(monkeypatch):
    """The tripwire answers for the app's writers; here the test is the writer."""
    monkeypatch.setattr(service, "check_tests", True)


async def test_a_to_do_is_saved_with_its_change_and_dropped_with_a_rollback(js):
    async with db.session_factory()() as session:
        role = make_role()
        session.add(role)
        await session.flush()
        assert ("role.settle", str(role.id)) in await posted(session)
        await session.rollback()
    assert await posted_now() == []
    async with db.session_factory()() as session:
        role = make_role()
        session.add(role)
        await session.commit()
        saved_id = role.id
    assert ("role.settle", str(saved_id)) in await posted_now()


async def test_a_rolled_back_savepoint_takes_its_to_dos_and_does_not_hide_a_later_identical_post(js):
    async with db.session_factory()() as session:
        nested = await session.begin_nested()
        queue.post(session, OWNER, queue.DECK_DIRTY, cause="test:in the savepoint")
        await session.flush()
        assert await posted(session) == [("deck.dirty", "")]
        await nested.rollback()
        assert await posted(session) == []
        queue.post(session, OWNER, queue.DECK_DIRTY, cause="test:after it")
        await session.commit()
    assert await posted_now() == [("deck.dirty", "")]


async def test_a_post_before_a_savepoint_is_not_lost_when_the_savepoint_rolls_back(js):
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.DECK_DIRTY, cause="test:outside")
        nested = await session.begin_nested()
        queue.post(session, OWNER, queue.DECK_DIRTY, cause="test:inside")
        await nested.rollback()
        await session.commit()
    assert await posted_now() == [("deck.dirty", "")]


async def test_a_flush_posts_one_to_do_per_topic_and_key(js):
    async with db.session_factory()() as session:
        for _ in range(3):
            queue.post(session, OWNER, queue.DECK_DIRTY, cause="test")
            queue.post(session, OWNER, queue.ROLE_SETTLE, keys=("a", "b", "a"), cause="test")
        await session.flush()
        assert sorted(await posted(session)) == [("deck.dirty", ""), ("role.settle", "a"), ("role.settle", "b")]
        # Dedupe is within one flush only: the same post after the flush is a row of its own.
        queue.post(session, OWNER, queue.DECK_DIRTY, cause="test")
        await session.flush()
        assert (await posted(session)).count(("deck.dirty", "")) == 2
        await session.rollback()


async def test_a_to_do_can_wait_for_a_time_and_says_who_posted_it(js):
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.ROLE_DETAILS, after=timedelta(hours=1), cause="test:later")
        await session.commit()
    async with db.session_factory()() as session:
        row = (await session.scalars(select(Todo))).one()
        assert row.cause == "test:later"
        assert row.not_before - row.posted_at == timedelta(hours=1)
        assert (row.attempts, row.last_error) == (0, None)


async def test_a_key_may_be_a_uuid_and_a_long_cause_is_cut(js):
    ident = uuid.uuid4()
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.ROLE_SETTLE, keys=(ident,), cause="x" * 200)
        await session.commit()
    async with db.session_factory()() as session:
        row = (await session.scalars(select(Todo))).one()
        assert row.key == str(ident)
        assert len(row.cause) == 60


# --- the tripwire (E2) ------------------------------------------------------------------------------------------


async def test_a_bulk_update_of_a_watched_table_without_a_post_is_refused_in_tests(js):
    async with db.session_factory()() as session:
        await session.execute(update(JobPosting).values(route="roles"))
        with pytest.raises(queue.MissedPost) as why:
            await session.commit()
        assert "js_postings" in str(why.value)
        await session.rollback()


async def test_a_bulk_update_with_a_post_or_an_exemption_is_not_refused(js):
    async with db.session_factory()() as session:
        await session.execute(update(JobPosting).values(route="roles"))
        queue.post(session, OWNER, queue.POSTING_ROLES, cause="test:posts")
        await session.commit()
    async with db.session_factory()() as session:
        with queue.exempt("a test's own statement that nothing reads"):
            await session.execute(update(JobPosting).values(route="roles"))
        await session.commit()
    assert await posted_now() == [("posting.roles", "")]


async def test_a_statement_rolled_back_with_its_savepoint_is_not_a_missed_post(js):
    async with db.session_factory()() as session:
        nested = await session.begin_nested()
        await session.execute(update(JobPosting).values(route="roles"))
        await nested.rollback()
        await session.commit()


async def test_a_statement_a_test_makes_to_arrange_rows_is_not_the_apps_writer(js, monkeypatch):
    monkeypatch.setattr(service, "check_tests", False)
    async with db.session_factory()() as session:
        await session.execute(update(JobPosting).values(route="roles"))
        await session.commit()
    assert await posted_now() == []


async def test_a_read_and_a_write_to_a_table_nobody_watches_are_not_refused(js):
    from app.models.work import Notification

    async with db.session_factory()() as session:
        await session.scalars(select(JobPosting))
        session.add(Notification(owner=OWNER, kind="info", title="made up"))
        await session.execute(delete(Todo))
        await session.commit()


async def test_the_same_bulk_update_in_production_mode_posts_the_catch_all_and_logs(js, monkeypatch, caplog):
    monkeypatch.setattr(service, "strict", False)
    caplog.set_level(logging.WARNING)
    for _ in range(2):  # the same call site twice: logged once
        async with db.session_factory()() as session:
            await session.execute(update(JobPosting).values(route="roles"))
            await session.commit()
    topics = set(await posted_now())
    assert {("posting.sift", "*"), ("posting.roles", "*"), ("role.settle", "*")} <= topics
    assert all(key == "*" for _, key in topics)
    logged = [r for r in caplog.records if "no to-do was posted" in r.getMessage()]
    assert len(logged) == 1 and "js_postings" in logged[0].getMessage()


async def test_a_raw_sql_write_to_a_watched_table_is_refused(js):
    async with db.session_factory()() as session:
        await session.execute(text("UPDATE js_postings SET route = 'roles'"))
        with pytest.raises(queue.MissedPost):
            await session.commit()
        await session.rollback()


@pytest.mark.parametrize("sql", [
    "WITH old AS (SELECT id FROM js_postings) UPDATE js_postings SET route = 'roles' FROM old",
    "UPDATE main.js_postings AS p SET route = 'roles'",
    'DELETE FROM "js_postings" WHERE route IS NULL',
    "WITH gone AS (SELECT id FROM js_postings) DELETE FROM js_postings WHERE id IN (SELECT id FROM gone)",
])
async def test_a_raw_sql_write_is_refused_wherever_its_verb_is_in_the_text(js, sql):
    async with db.session_factory()() as session:
        await session.execute(text(sql))
        with pytest.raises(queue.MissedPost):
            await session.commit()
        await session.rollback()


async def test_a_raw_sql_read_is_not_a_write(js):
    async with db.session_factory()() as session:
        await session.execute(text("SELECT id FROM js_postings"))
        await session.commit()


def test_the_words_of_a_raw_write_say_its_verb_and_table():
    from app.jobsearch.work.queue.hook import raw_write

    assert raw_write("UPDATE js_postings SET a = 1") == ("update", "js_postings")
    assert raw_write("with x as (select 1) insert into public.js_postings (a) select 1 on conflict do update set a = 2"
                     ) == ("insert", "js_postings")
    assert raw_write('DELETE FROM ONLY "js_postings"') == ("delete", "js_postings")
    assert raw_write("TRUNCATE TABLE js_postings") == ("truncate", "js_postings")
    assert raw_write("SELECT * FROM js_postings FOR UPDATE") is None
    assert raw_write("SELECT 1") is None


async def test_the_session_event_sees_every_shape_of_bulk_statement_on_the_locked_version(js):
    """Phase 1's first test (the doc's J): the tripwire matches the statement's table, so each shape a writer uses must
    reach the session's event: a Core insert, a Postgres insert with a conflict clause, an update that returns, a bulk
    update by key, a delete and a statement on the table itself."""
    seen: list[str] = []

    def noted(state) -> None:
        if state.is_insert or state.is_update or state.is_delete:
            kind = "insert" if state.is_insert else "update" if state.is_update else "delete"
            seen.append(f"{kind}:{state.statement.table.name}")

    event.listen(Session, "do_orm_execute", noted)
    try:
        async with db.session_factory()() as session:
            with queue.exempt("a test of the event"):
                role = make_role()
                session.add(role)
                await session.flush()
                seen.clear()
                await session.execute(insert(Opportunity).values(
                    id=uuid.uuid4(), owner=OWNER, slug="s", company="c", title="t", match_key="k1"))
                await session.execute(pg_insert(Opportunity).values(
                    id=uuid.uuid4(), owner=OWNER, slug="s", company="c", title="t", match_key="k1")
                    .on_conflict_do_nothing())
                await session.execute(update(Opportunity).where(Opportunity.id == role.id).values(fit=1)
                                      .returning(Opportunity.id))
                await session.execute(update(Opportunity), [{"id": role.id, "fit": 2}])
                await session.execute(insert(Opportunity.__table__).values(
                    id=uuid.uuid4(), owner=OWNER, slug="t", company="c", title="t", match_key="k2"))
                await session.execute(delete(Opportunity).where(Opportunity.id == role.id))
            await session.rollback()
    finally:
        event.remove(Session, "do_orm_execute", noted)
    assert seen.count("insert:js_opportunities") == 3
    assert seen.count("update:js_opportunities") == 2
    assert seen.count("delete:js_opportunities") == 1
