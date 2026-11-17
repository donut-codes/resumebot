"""The queue runs on either engine the app may end on (the work queue spec G): every statement it makes
compiles for the SQLite dialect, and the table holds nothing only Postgres has. A real SQLite run waits for the engine
decision. Everything is made up."""

from datetime import UTC, datetime

from sqlalchemy import insert
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from app.jobsearch.models import JobPosting, Todo
from app.jobsearch.work.queue import coverage, handlers, janitor, service

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def test_every_queue_statement_compiles_on_the_sqlite_dialect():
    window = coverage.Window(NOW, NOW)
    statements = {
        "post": insert(Todo).values(owner="o", topic="role.settle", key="k", not_before=NOW, posted_at=NOW, cause="c"),
        "janitor counts": janitor.counts_statement(NOW),
        "janitor delete": janitor.delete_statement(NOW),
        "janitor counts less a consumer's topics": janitor.counts_statement(NOW, ["posting.sift", "tick.hour"]),
        "janitor delete less a consumer's topics": janitor.delete_statement(NOW, ["posting.sift", "tick.hour"]),
        "due": service.due_statement("o", NOW, ["posting.sift", "tick.hour"]),
        "next due": service.next_due_statement("o", NOW, ["posting.sift", "tick.hour"]),
        "a to-do of the topic waits": service.waiting_statement("o", "posting.sift"),
        "done": service.done_statement([1, 2, 3]),
        "fail": service.fail_statement([1, 2, 3], "boom", NOW),
        "probe: waiting for the filter": handlers.owed_statement(handlers._sift_where("o")),
        "probe: waiting for a role": handlers.owed_statement(handlers._roles_where("o")),
        "coverage": coverage.told_statement("o", "role.settle", window),
        "table": CreateTable(Todo.__table__),
        **{f"index {i.name}": CreateIndex(i) for i in Todo.__table__.indexes},
    }
    for name, statement in statements.items():
        compiled = str(statement.compile(dialect=sqlite.dialect()))
        assert compiled, name


def test_the_table_has_an_integer_key_that_autoincrements_on_sqlite_and_nothing_only_postgres_has():
    ddl = str(CreateTable(Todo.__table__).compile(dialect=sqlite.dialect()))
    assert "id INTEGER NOT NULL" in ddl and "PRIMARY KEY (id)" in ddl and "BIGINT" not in ddl
    table = Todo.__table__
    assert not table.foreign_keys, "a to-do may outlive its role: no foreign keys"
    assert {type(c.type).__name__ for c in table.columns} <= {"BigInteger", "String", "UtcDateTime", "Integer", "Text"}
    for index in table.indexes:
        assert not index.unique and not index.dialect_options["postgresql"]["where"], "no unique or partial index"
    assert {c.name for c in table.columns} == {"id", "owner", "topic", "key", "not_before", "posted_at", "cause",
                                               "attempts", "last_error"}


def test_the_probe_indexes_have_a_where_for_sqlite_too_and_compile():
    probes = [i for i in JobPosting.__table__.indexes if i.name in (
        "ix_js_postings_owner_unfiltered", "ix_js_postings_owner_roles_waiting")]
    assert len(probes) == 2
    for index in probes:
        ddl = str(CreateIndex(index).compile(dialect=sqlite.dialect()))
        assert " WHERE " in ddl and "opportunity_id IS NULL" in ddl, ddl
