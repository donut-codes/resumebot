"""The migration chain and the tables the app owns. The chain is what a new migration (made for SQLite) is added to and
what `db.create_schema` stamps a new file with; the migrations of the Postgres era never run on SQLite (a file is made
from the models and stamped at head), so the tests that ran them are gone. The file made from the models, its PRAGMAs,
its triggers and its foreign keys are in `test_sqlite_bootstrap.py` and `test_sqlite_schema.py`."""

from process_engine.base import Base as EngineBase

from app import db
from app.database import Base
from tests.support import migration_head


def test_every_table_the_app_owns_has_a_model():
    """A name in OWNED_TABLES with no model would be truncated by the tests, yet never made by `create_schema`."""
    assert db.OWNED_TABLES <= set(Base.metadata.tables) | set(EngineBase.metadata.tables)


def test_the_migrations_are_one_unbroken_chain_with_one_head():
    """A branch (two heads) would make `upgrade head` refuse to start the app, and a revision pointing at nothing would
    break the walk from an empty database. (That they only go forward is `scripts/check_migrations.py`'s rule.)"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(db.BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(db.BACKEND_DIR / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [migration_head()]
    walked = list(scripts.walk_revisions())
    assert len(walked) == len(list((db.BACKEND_DIR / "migrations" / "versions").glob("[0-9]*.py")))
    assert [r.revision for r in walked if r.down_revision is None] == [walked[-1].revision]
