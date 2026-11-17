"""The `resumebot copy-to-sqlite` command's own words and exit codes (`app.migrate.cli`); no database here."""

import datetime
import uuid

import sqlalchemy as sa

from app import main_cli
from app.migrate import cli, copier, verify
from app.migrate.plan import CopyError


def test_the_command_is_listed_with_what_it_does():
    assert "copy-to-sqlite" in main_cli.COMMANDS
    line = next(row for row in main_cli.usage().splitlines() if "copy-to-sqlite" in row)
    assert "Postgres" in line and "SQLite" in line


def test_a_missing_address_is_a_wrong_command_line(capsys):
    assert cli.main(["--to", "x.db"]) == 2
    assert "--from" in capsys.readouterr().err


def test_an_address_that_is_not_postgres_is_refused_and_no_file_is_made(tmp_path, capsys):
    target = tmp_path / "x.db"
    assert cli.main(["--from", "mysql://user:secret@host/db", "--to", str(target)]) == 1
    said = capsys.readouterr().err
    assert "postgresql://" in said and "secret" not in said
    assert not target.exists()


def test_a_stop_the_copy_can_name_is_said_without_a_trace(monkeypatch, tmp_path, capsys):
    async def stops(source, target, say=None):
        raise CopyError("the old database has no table js_x")

    monkeypatch.setattr(copier, "copy", stops)
    assert cli.main(["--from", "postgresql://u@h/d", "--to", str(tmp_path / "x.db")]) == 1
    assert capsys.readouterr().err == "resumebot copy-to-sqlite: the old database has no table js_x\n"


def test_a_copy_that_did_not_check_out_says_so_and_exits_nonzero(monkeypatch, tmp_path, capsys):
    async def loaded(source, target, say=None):
        found = verify.Verified(problems=["js_postings: 2 rows in the old database, 1 in the copy"])
        return copier.Report(found, 0, 0, 1.0)

    monkeypatch.setattr(copier, "copy", loaded)
    assert cli.main(["--from", "postgresql://u@h/d", "--to", str(tmp_path / "x.db")]) == 1
    said = capsys.readouterr()
    assert "NOT VERIFIED: js_postings: 2 rows in the old database, 1 in the copy" in said.err
    assert "do not use it" in said.err and "copied and checked" not in said.out


async def test_settle_runs_as_background_work_so_the_foreground_limit_does_not_end_it(monkeypatch, tmp_path):
    from app import db
    from app.jobsearch.models import JobPosting, Opportunity
    from app.jobsearch.pipeline import stored_order
    from app.jobsearch.postings import raw_copies

    seen = []

    async def repoint(session, owner):
        seen.append(("repoint", db.in_background()))
        return 0

    async def settle(session, owner, everything=False):
        seen.append(("order", db.in_background()))
        return 0

    target = tmp_path / "x.db"
    engine = copier.build(target)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        for table in (JobPosting.__table__, Opportunity.__table__):
            row = {c.name: _filler(c) for c in table.columns if not c.nullable and c.default is None
                   and c.server_default is None}
            connection.execute(table.insert().values(row))
    engine.dispose()
    monkeypatch.setattr(raw_copies, "repoint", repoint)
    monkeypatch.setattr(stored_order, "settle", settle)
    await copier.settle(target)
    assert seen == [("repoint", True), ("order", True)]


def _filler(column):
    """A value of the column's type, for a row the test needs only to exist."""
    kind = column.type.impl if hasattr(column.type, "impl") else column.type
    if getattr(column.type, "python_type", None) is uuid.UUID or isinstance(kind, sa.Uuid):
        return uuid.uuid4()
    if isinstance(kind, sa.Boolean):
        return False
    if isinstance(kind, sa.Integer):
        return 1
    if isinstance(kind, sa.DateTime):
        return datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    if isinstance(kind, sa.Date):
        return datetime.date(2026, 1, 1)
    if isinstance(kind, sa.JSON):
        return []
    return "x"


def test_a_finished_copy_has_the_planners_statistics(tmp_path):
    from app.jobsearch.models import Opportunity

    target = tmp_path / "x.db"
    engine = copier.build(target)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        table = Opportunity.__table__
        row = {c.name: _filler(c) for c in table.columns if not c.nullable and c.default is None
               and c.server_default is None}
        connection.execute(table.insert().values(row))
    copier.finish(engine)
    engine.dispose()
    with sa.create_engine(f"sqlite:///{target.as_posix()}").connect() as connection:
        counted = connection.exec_driver_sql("SELECT count(*) FROM sqlite_stat1").scalar()
    assert counted and counted > 0
