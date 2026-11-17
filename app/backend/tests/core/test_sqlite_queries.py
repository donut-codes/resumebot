"""The queries that were Postgres's own now run on SQLite: a list of ids is one JSON argument, a key of a JSON column
is `json_extract` and an upsert names the columns of its unique key. Each test makes its own file in a temporary
folder (see `test_sqlite_schema.py`) and uses none of the shared database fixtures.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Uuid, event, insert, select, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.dialects.sqlite import insert as lite_insert

from app import db, jsonsql
from app.jobsearch.models import (
    POSTING_NOT_FOUND,
    SOURCE_NEXT,
    BrowserItem,
    Opportunity,
    RoleRank,
    posting_still_sought,
    source_next_marked,
)
from tests.core import test_sqlite_schema as schema

template, lite = schema.template, schema.lite  # the file `init_schema` made once, and a copy of it for each test

OWNER = "the-owner"
SEEN = datetime(2026, 11, 4, 12, tzinfo=UTC)


@pytest.fixture
async def session(lite):
    async with db.session_factory()() as s:
        yield s


def scratch(*columns: Column) -> Table:
    return Table(f"scratch_{uuid.uuid4().hex}", MetaData(), Column("id", Integer, primary_key=True), *columns)


async def make(engine, table: Table) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(table.metadata.create_all)


def role(n: int, **fields) -> Opportunity:
    return Opportunity(owner=OWNER, slug=f"r{n}", company=f"Company {n}", title=f"Title {n}", match_key=f"key-{n}",
                       **fields)


async def keep(session, *rows) -> None:
    session.add_all(rows)
    await session.flush()


def statements(engine):
    """The statements the engine runs and their arguments, while the `with` block runs."""
    seen: list[tuple[str, object]] = []

    def note(conn, cursor, statement, parameters, context, executemany):
        seen.append((statement, parameters))

    class Watch:
        def __enter__(self):
            event.listen(engine.sync_engine, "before_cursor_execute", note)
            return seen

        def __exit__(self, *exc):
            event.remove(engine.sync_engine, "before_cursor_execute", note)

    return Watch()


# --- C2: many ids, one argument -------------------------------------------------------------------------------------


async def test_forty_thousand_ids_are_one_argument_of_one_statement(lite):
    table = scratch(Column("ident", Uuid), Column("number", Integer), Column("name", String))
    await make(lite, table)
    digits = uuid.UUID("12345678-1234-5678-1234-567812345678")  # its hex is all digits: a number to a naive column
    mine = [digits, uuid.UUID(int=7), uuid.uuid4()]
    async with lite.begin() as conn:
        await conn.execute(insert(table), [{"ident": u, "number": i, "name": f"n{i}"} for i, u in enumerate(mine)])
        await conn.execute(insert(table), [{"ident": uuid.uuid4(), "number": 99, "name": "other"}])
    wanted = [*(uuid.uuid4() for _ in range(40_000)), *mine[:2]]
    with statements(lite) as seen:
        async with lite.connect() as conn:
            found = set((await conn.execute(select(table.c.ident).where(db.among(table.c.ident, wanted)))).scalars())
    assert found == set(mine[:2])
    ours = [parameters for statement, parameters in seen if "json_each" in statement]
    assert len(ours) == 1 and len(ours[0]) == 1, "one argument, however many ids"


async def test_among_matches_integers_and_text_and_an_empty_list_matches_nothing(lite):
    table = scratch(Column("number", Integer), Column("name", String))
    await make(lite, table)
    async with lite.begin() as conn:
        await conn.execute(insert(table), [{"number": n, "name": f"n{n}"} for n in range(5)])
        numbers = (await conn.execute(select(table.c.number).where(db.among(table.c.number, [1, 3, 99])))).scalars()
        names = (await conn.execute(select(table.c.name).where(db.among(table.c.name, ("n0", "n4", "zz"))))).scalars()
        none = (await conn.execute(select(table.c.number).where(db.among(table.c.number, [])))).scalars()
        assert sorted(numbers) == [1, 3]
        assert sorted(names) == ["n0", "n4"]
        assert list(none) == []


async def test_two_lists_of_ids_in_one_statement_keep_their_own_arguments(lite):
    table = scratch(Column("ident", Uuid), Column("other", Uuid))
    await make(lite, table)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with lite.begin() as conn:
        await conn.execute(insert(table), [{"ident": a, "other": b}, {"ident": b, "other": c},
                                           {"ident": c, "other": a}])
        rows = (await conn.execute(select(table.c.ident).where(
            db.among(table.c.ident, [a, b]), db.among(table.c.other, [c, a])))).scalars().all()
    assert rows == [b]


async def test_ids_written_as_json_match_the_stored_ids_of_a_statement_of_text(lite):
    table = scratch(Column("ident", Uuid))
    await make(lite, table)
    digits = uuid.UUID("12345678-1234-5678-1234-567812345678")
    plain = uuid.uuid4()
    async with lite.begin() as conn:
        await conn.execute(insert(table), [{"ident": digits}, {"ident": plain}, {"ident": uuid.uuid4()}])
        found = (await conn.execute(
            text(f"SELECT ident FROM {table.name} WHERE ident IN (SELECT value FROM json_each(:roles))"),
            {"roles": db.json_ids([digits, plain])})).scalars().all()
    assert sorted(found) == sorted([digits.hex, plain.hex])


def test_the_gate_choices_for_roles_take_the_ids_as_one_json_argument():
    from app import db
    from app.jobsearch.postings import cleansed

    roles = [uuid.uuid4() for _ in range(3)]
    statement = cleansed._gate_choices(OWNER, db.among(cleansed._P.opportunity_id, roles), thin=False)
    sql = str(statement.compile(dialect=sqlite.dialect()))
    assert "json_each(" in sql and "ANY" not in sql


# --- C3: the keys of a JSON column ----------------------------------------------------------------------------------


PENDING_INDEX = "ix_js_opportunities_owner_facts_pending"


async def test_the_pending_read_is_written_as_the_partial_index_holds_it_so_the_index_can_serve_it(lite):
    from sqlalchemy.exc import OperationalError

    from app.jobsearch.gating import gate_facts

    statement = select(Opportunity.id).where(Opportunity.owner == OWNER, gate_facts.pending())
    written = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    assert "json_extract(js_opportunities.gate_facts, '$.state')" in written
    served = written.replace("FROM js_opportunities", f"FROM js_opportunities INDEXED BY {PENDING_INDEX}")
    async with lite.connect() as conn:
        assert (await conn.execute(text(served))).all() == []  # `INDEXED BY` refuses a statement the index cannot serve
        # the same read with the path as an argument (what SQLAlchemy's own accessor sends) is not served by it
        with pytest.raises(OperationalError, match="no query solution"):
            await conn.execute(text(
                f"SELECT id FROM js_opportunities INDEXED BY {PENDING_INDEX} "
                "WHERE owner = 'o' AND json_extract(gate_facts, :path) = 'pending'"), {"path": "$.state"})


async def test_a_json_key_reads_as_text_or_null(lite):
    table = scratch(Column("data", JSON))
    await make(lite, table)
    rows = {1: {"a": "x", "n": 5, "t": True, "f": False, "z": None, "o": {"k": 1}, "l": [], "odd key": "y"},
            2: {}, 3: {"a": ""}}
    async with lite.begin() as conn:
        for key, data in rows.items():
            await conn.execute(insert(table).values(id=key, data=data))
        read = {name: (await conn.execute(select(jsonsql.field(table.c.data, name)).where(table.c.id == 1))).scalar()
                for name in ("a", "n", "t", "f", "z", "o", "l", "odd key", "missing")}
        has = {name: set((await conn.execute(select(table.c.id).where(jsonsql.has(table.c.data, name)))).scalars())
               for name in ("a", "z", "missing", "odd key")}
    assert read == {"a": "x", "n": 5, "t": 1, "f": 0, "z": None, "o": '{"k":1}', "l": "[]", "odd key": "y",
                    "missing": None}
    assert has == {"a": {1, 3}, "z": set(), "missing": set(), "odd key": {1}}


async def test_the_roles_whose_posting_the_app_gave_up_on_and_the_roles_marked_for_a_source(session):
    await keep(session,
               role(1, analysis={POSTING_NOT_FOUND: True}),
               role(2, analysis={}),
               role(3, analysis={POSTING_NOT_FOUND: None}),
               role(4, analysis={SOURCE_NEXT: True}, status="first_impression"),
               role(5, analysis={SOURCE_NEXT: True}, status="second_impression"),
               role(6, analysis={}, status="first_impression"))
    sought = set(await session.scalars(select(Opportunity.slug).where(posting_still_sought())))
    marked = set(await session.scalars(select(Opportunity.slug).where(source_next_marked())))
    assert sought == {"r2", "r3", "r4", "r5", "r6"}
    assert marked == {"r4"}


async def test_a_summary_of_an_older_voice_is_stale(session):
    from app.jobsearch.research import role_summary

    version = role_summary.SUMMARY_VERSION
    key = role_summary.SUMMARY_KEY
    await keep(session,
               role(1, analysis={key: {"v": version - 1, "why": "w"}}),
               role(2, analysis={key: {"v": version, "why": "w"}}),
               role(3, analysis={key: {"why": "no stamp"}}),
               role(4, analysis={key: "not an object"}),
               role(5, analysis={}))
    stale = set(await session.scalars(select(Opportunity.slug).where(role_summary._stale_sql())))
    # a summary that is no object has no voice either (as when Postgres read it): it is read as older than any
    assert stale == {"r1", "r3", "r4"}


async def test_the_analysts_marks_are_read_from_the_research(session):
    from app.jobsearch.research import analysis, line

    await keep(session,
               role(1, analysis={analysis.ASKED_BY: analysis.ASKED_BY_OWNER}),
               role(2, analysis={analysis.ASKED_BY: "the app"}),
               role(3, analysis={}),
               role(4, analysis={analysis.SESSION_KEY: "s1", analysis.RUNNER_KEY: "r/1"}))
    asked = set(await session.scalars(select(Opportunity.slug).where(line.owners_asks())))
    not_owners = set(await session.scalars(select(Opportunity.slug).where(line.not_owners())))
    session_of = dict((await session.execute(select(Opportunity.slug, analysis._field(analysis.SESSION_KEY)))).all())
    assert asked == {"r1"}
    assert not_owners == {"r2", "r3", "r4"}
    assert session_of["r4"] == "s1" and session_of["r3"] is None


async def test_what_the_analyst_said_of_a_place_is_read_for_few_roles_and_for_many(session):
    from app.jobsearch.pipeline import placement
    from app.jobsearch.research import analysis

    said = {1: "Remote", 2: None, 3: "Springdale, IL"}
    rows = [role(n, analysis={"place": place, analysis.DECK_READ: True} if place else {analysis.DECK_READ: True},
                 analysis_state=analysis.DONE) for n, place in said.items()]
    await keep(session, *rows)
    ids = [r.id for r in rows]
    few = await placement._place_said(session, OWNER, ids)
    assert few == {rows[0].id: "Remote", rows[1].id: None, rows[2].id: "Springdale, IL"}
    # a list longer than `OWNER_WIDE_AT` is read for the owner's whole set of read roles and picked out of it
    crowd = [*ids, *(uuid.uuid4() for _ in range(placement.OWNER_WIDE_AT))]
    many = await placement._place_said(session, OWNER, crowd)
    assert many == few


async def test_the_predicted_ask_of_a_role_is_read_from_the_research(session):
    from app.jobsearch.pipeline import row

    rows = [role(1, analysis={"ask": {"ask": 120000}}), role(2, analysis={"ask": {}}), role(3, analysis={})]
    await keep(session, *rows)
    assert await row._asks(session, [r.id for r in rows]) == {rows[0].id: 120000}


async def test_the_roles_the_haiku_read_holds_back_are_the_pending_ones(session, monkeypatch):
    from app.jobsearch.gating import gate_facts

    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    now = SEEN + timedelta(hours=1)
    pending = {"state": gate_facts.PENDING, "at": SEEN.isoformat()}
    held = role(1, analysis_state="waiting", gate_facts=pending)
    stale = role(2, analysis_state="waiting",
                 gate_facts={**pending, "at": (SEEN - gate_facts.HOLD - timedelta(hours=1)).isoformat()})
    plain = role(3, analysis_state="waiting", gate_facts={})
    done = role(4, analysis_state="done", gate_facts=pending)
    await keep(session, held, stale, plain, done)
    assert await gate_facts.held_back_ids(session, OWNER, now) == {held.id}
    assert {r.id for r in await gate_facts.waiting(session, OWNER, now=now)} == {held.id, stale.id}


async def test_an_employer_handed_out_in_a_search_and_an_employer_answered_since(session):
    from app.jobsearch.doors import tools
    from app.jobsearch.models import Employer

    when = datetime(2026, 11, 4, 12, tzinfo=UTC)
    handed = Employer(owner=OWNER, name="One", key="one", sources={tools.OFFERED: {"search": "s1", "night": "n1"}})
    answered = Employer(owner=OWNER, name="Two", key="two", sources={tools.ASKED: {"at": when.isoformat()}})
    old = Employer(owner=OWNER, name="Three", key="three",
                   sources={tools.ASKED: {"at": (when - timedelta(days=3)).isoformat()}})
    await keep(session, handed, answered, old)
    assert await tools.boards_handed_out(session, OWNER, ["s0", "s1"]) is True
    assert await tools.boards_handed_out(session, OWNER, ["s0"]) is False
    assert await tools.boards_handed_out(session, OWNER, []) is False
    assert await tools.boards_answered_since(session, OWNER, when - timedelta(hours=1)) is True
    assert await tools.boards_answered_since(session, OWNER, when + timedelta(hours=1)) is False


async def test_a_session_is_handed_again_the_posting_it_took_and_no_other_session_is(session):
    from app.jobsearch.jobs import parse
    from app.jobsearch.models import ParseJob

    taken = SEEN + timedelta(seconds=30)
    job = ParseJob(owner=OWNER, kind=parse.POSTING, state=parse.READING, text="the posting", taken_at=taken,
                   fields={"session_id": "abc"})
    await keep(session, job)
    mine = await parse.take_next(session, OWNER, taken + timedelta(seconds=30), session_id="abc")
    assert mine["id"] == str(job.id) and mine["text"] == "the posting"
    other = await parse.take_next(session, OWNER, taken + timedelta(seconds=30), session_id="another")
    assert other["done"] is True, "the posting is that session's alone"


async def test_a_run_nothing_holds_is_cancelled_for_the_owner_in_its_data(session, monkeypatch):
    from process_engine.models import ProcInstance

    from app.jobsearch import process
    from app.jobsearch.work import stuck

    mine = ProcInstance(process_key=process.ANALYSE, file_hash="h", status="waiting", data={"owner": OWNER})
    theirs = ProcInstance(process_key=process.ANALYSE, file_hash="h", status="waiting", data={"owner": "another"})
    held = ProcInstance(process_key=process.ANALYSE, file_hash="h", status="waiting", data={"owner": OWNER})
    await keep(session, mine, theirs, held)
    await keep(session, role(1, analysis_instance_id=held.id, analysis_state="waiting"))
    cancelled = []

    async def cancel(session, instance_id, why, what):  # the engine's own cancel is tested with the engine
        cancelled.append(instance_id)
        return True

    monkeypatch.setattr(stuck, "cancel_instance", cancel)
    assert await stuck.cancel_orphans(session, OWNER) == 1
    assert cancelled == [mine.id]


async def test_a_planning_task_waiting_out_its_retry_goes_back_to_todo_and_one_with_no_planner_run_stays(session):
    from app.sites import escalation
    from app.sites import models as sm

    project = sm.Project(owner=OWNER, title="p", outcome="o")
    await keep(session, project)

    def task(n: int, data: dict) -> sm.PlanTask:
        return sm.PlanTask(owner=OWNER, project_id=project.id, plan_version=1, position=n, item_key=f"k{n}",
                           title=f"t{n}", group_label="g", action=sm.PlanItemAction.plan, mode=sm.PlanTaskMode.plan,
                           status=sm.PlanTaskStatus.waiting, data=data)

    due = task(1, {"planner_runs": {"retry_at": (SEEN - timedelta(minutes=1)).isoformat(), "n": 1}})
    early = task(2, {"planner_runs": {"retry_at": (SEEN + timedelta(minutes=5)).isoformat()}})
    none = task(3, {"escalation": {"options": []}})
    await keep(session, due, early, none)
    wakes = await escalation.retry_planner_runs(session, SEEN)
    assert [(p.id, why) for p, why, _ in wakes] == [(project.id, "planner_retry")]
    assert (due.status, early.status, none.status) == (sm.PlanTaskStatus.todo, sm.PlanTaskStatus.waiting,
                                                       sm.PlanTaskStatus.waiting)
    assert due.data == {"planner_runs": {"n": 1}}


# --- C6: upserts ----------------------------------------------------------------------------------------------------


async def test_a_runner_first_heard_twice_makes_one_row(session):
    from app.agents import roster
    from app.models.agent import AgentRunner

    body = {"name": "runner-1", "state": "idle"}
    await roster.note_runner(session, OWNER, body, "scout")
    await roster.note_runner(session, OWNER, body, "scout")
    rows = (await session.scalars(select(AgentRunner).where(AgentRunner.owner == OWNER))).all()
    assert [r.name for r in rows] == [roster.runner_name(body)]


async def test_the_tracker_import_run_twice_stores_each_role_once(session, monkeypatch):
    from app.jobsearch.imports import tracker
    from tests.support import tracker_row, tracker_workbook

    async def no_log(session, rows):  # the raw copies of the log are numbered by their own port task (B2)
        return None

    monkeypatch.setattr(tracker.role_postings, "log_imported", no_log)
    rows = [tracker_row("Initrode", "Programs Lead"), tracker_row("Initrode", "Programs Director")]
    first = await tracker.import_tracker(session, OWNER, tracker_workbook(rows))
    second = await tracker.import_tracker(session, OWNER, tracker_workbook(rows))
    assert (first["added"], second["added"], second["already_there"]) == (2, 0, 2)
    assert len((await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all()) == 2


async def test_a_second_insert_of_a_role_with_the_same_key_is_ignored_and_returns_no_id(session):
    """The statement of `actions.store` and the tracker import: the unique key named by its columns."""
    values = {"id": uuid.uuid4(), "owner": OWNER, "slug": "s", "company": "C", "title": "T", "match_key": "same"}

    async def insert_role(**more):
        return await session.execute(
            lite_insert(Opportunity).values({**values, **more})
            .on_conflict_do_nothing(index_elements=["owner", "match_key"]).returning(Opportunity.id))

    first, second = await insert_role(), await insert_role(id=uuid.uuid4())
    assert first.first() is not None and second.first() is None
    assert len((await session.scalars(select(Opportunity))).all()) == 1


async def test_the_browser_queue_adds_an_item_once_however_many_refreshes_add_it(session):
    from app.jobsearch.browser import queue

    def add() -> dict:
        return {"id": uuid.uuid4(), "owner": OWNER, "kind": "scan", "target": "https://careers.example.test",
                "state": "waiting", "tries": 0, "next_at": SEEN, "created_at": SEEN,
                **dict.fromkeys(queue.FIELDS), "domain": "careers.example.test", "priority": 0}

    await queue._apply(session, OWNER, queue._Plan(adds=[add(), add()]), SEEN)
    await queue._apply(session, OWNER, queue._Plan(adds=[add()]), SEEN)
    assert len((await session.scalars(select(BrowserItem))).all()) == 1


async def test_a_rank_made_on_a_cut_text_is_replaced_in_its_row_and_any_other_stands(lite):
    """The statement of `rank.keep`. The rows it points at are not the matter, so the keys are not checked."""
    key = {"role_id": uuid.uuid4(), "cleansed_id": uuid.uuid4(), "profile_version": 3}

    async def keep_rank(conn, fit: int, cut: bool) -> None:
        made = lite_insert(RoleRank).values(id=uuid.uuid4(), owner=OWNER, **key, fit=fit, why="w", text_cut=cut,
                                            created_at=SEEN)
        await conn.execute(made.on_conflict_do_update(
            index_elements=["role_id", "cleansed_id", "profile_version"],
            set_={"fit": made.excluded.fit, "text_cut": False}, where=RoleRank.text_cut.is_(True)))

    async with lite.connect() as conn:
        raw = await conn.get_raw_connection()
        # before the transaction: inside one the pragma does nothing
        await raw.driver_connection.execute("PRAGMA foreign_keys=OFF")
        await keep_rank(conn, 40, True)
        await keep_rank(conn, 70, False)  # replaces the cut one
        await keep_rank(conn, 10, False)  # stands as it is
        rows = (await conn.execute(select(RoleRank.fit, RoleRank.text_cut))).all()
    assert [tuple(r) for r in rows] == [(70, False)]
