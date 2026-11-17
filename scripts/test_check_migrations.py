"""Tests for check_migrations.py: stdlib unittest, run by CI (python -m unittest discover -s scripts -p "test_*.py").

Each test builds a throwaway git repo with a base commit and a change on top of it, so nothing reads the real tree.
"""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_migrations as g  # noqa: E402

V = "app/backend/migrations/versions/"
FIRST = '"""Add a column."""\n\nrevision = "0001"\n\n\ndef upgrade():\n    """Does it."""\n    run("add")\n'


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.test", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


class Repo:
    def __init__(self, tmp: str):
        self.path = Path(tmp)
        git(self.path, "init", "-q", "-b", "main")
        self.write(V + "0001_first.py", FIRST)
        self.commit("base")
        git(self.path, "branch", "base")

    def write(self, rel: str, text: str) -> None:
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", message)

    def problems(self) -> list[str]:
        return g.problems("base", self.path)


class ForwardOnly(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repo(self._tmp.name)

    def test_a_new_migration_is_fine(self):
        self.repo.write(V + "0002_second.py", FIRST.replace("0001", "0002"))
        self.repo.commit("add")
        self.assertEqual(self.repo.problems(), [])

    def test_a_changed_docstring_or_comment_is_fine(self):
        text = FIRST.replace('"""Add a column."""', '"""Add a column to the table, for the reason."""')
        text = text.replace('    run("add")', '    # a comment\n    run("add")')
        self.repo.write(V + "0001_first.py", text)
        self.repo.commit("docs")
        self.assertEqual(self.repo.problems(), [])

    def test_changed_code_is_refused(self):
        self.repo.write(V + "0001_first.py", FIRST.replace('run("add")', 'run("add2")'))
        self.repo.commit("edit")
        found = self.repo.problems()
        self.assertEqual(len(found), 1)
        self.assertIn("0001_first.py", found[0])

    def test_a_deleted_or_renamed_migration_is_refused(self):
        git(self.repo.path, "mv", V + "0001_first.py", V + "0001_renamed.py")
        self.repo.commit("rename")
        found = self.repo.problems()
        self.assertEqual(len(found), 1)
        self.assertIn("deleted or renamed", found[0])

    def test_running_a_downgrade_is_refused(self):
        self.repo.write("app/deploy/x.sh", "alembic downgrade -1\n")
        self.repo.commit("script")
        found = self.repo.problems()
        self.assertEqual(len(found), 1)
        self.assertIn("app/deploy/x.sh:1", found[0])

    def test_a_migration_defining_downgrade_is_not_running_one(self):
        self.repo.write(V + "0002_second.py", FIRST.replace("0001", "0002") + "\n\ndef downgrade():\n    pass\n")
        self.repo.commit("add")
        self.assertEqual(self.repo.problems(), [])


class SqliteSafe(unittest.TestCase):
    """A migration newer than 0154 (the last written for Postgres) may hold no Postgres-only SQL."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repo(self._tmp.name)

    def add(self, name: str, body: str) -> list[str]:
        self.repo.write(V + name, '"""Fake."""\n\nrevision = "x"\n\n\ndef upgrade():\n' + body + "\n")
        self.repo.commit("add")
        return self.repo.problems()

    def test_each_postgres_only_form_is_refused_in_a_new_migration(self):
        forms = {
            "plpgsql": '    op.execute("DO $$ BEGIN END $$ LANGUAGE plpgsql")',
            "create function": '    op.execute("CREATE OR REPLACE FUNCTION f() RETURNS int AS $$ SELECT 1 $$")',
            "create trigger": '    op.execute("create trigger t before insert on x for each row execute function f()")',
            "cast": '    op.execute("UPDATE t SET a = b::text")',
            "jsonb": '    op.execute("ALTER TABLE t ADD c JSONB")',
            "array": '    op.execute("SELECT ARRAY[1, 2]")',
            "any": '    op.execute("SELECT 1 FROM t WHERE a = ANY(b)")',
            "distinct on": '    op.execute("SELECT DISTINCT ON (a) a FROM t")',
            "ilike": '    op.execute("SELECT 1 FROM t WHERE a ILIKE 1")',
            "time zone": '    op.execute("SELECT a AT TIME ZONE 1 FROM t")',
            "now": '    op.execute("UPDATE t SET a = now()")',
            "interval": '    op.execute("SELECT a - INTERVAL 1 FROM t")',
            "information_schema": '    op.execute("SELECT 1 FROM information_schema.tables")',
            "pg_": '    op.execute("SELECT pg_get_serial_sequence(1, 2)")',
            "dialect import": "    from sqlalchemy.dialects import postgresql\n    pass",
            "dialect from": "    from sqlalchemy.dialects.postgresql import JSONB\n    pass",
            "dialect import module": "    import sqlalchemy.dialects.postgresql\n    pass",
        }
        for label, code in forms.items():
            with self.subTest(label):
                found = self.add("0155_new.py", code)
                self.assertEqual(len(found), 1, found)
                self.assertIn("0155_new.py", found[0])
                git(self.repo.path, "rm", "-q", "-f", V + "0155_new.py")
                self.repo.commit("remove")

    def test_the_boundary_and_earlier_migrations_are_exempt(self):
        for name in ("0154_last_postgres.py", "0153_before.py", "0133_old.py"):
            self.assertEqual(self.add(name, '    op.execute("UPDATE t SET a = b::text")'), [])

    def test_the_revision_after_the_boundary_is_checked_even_when_the_boundary_file_is_absent(self):
        self.assertEqual(len(self.add("0155_new.py", '    op.execute("SELECT now()")')), 1)

    def test_portable_sql_docstrings_and_comments_are_fine(self):
        text = (
            '"""Why: the old one used ILIKE and now()."""\n\nrevision = "x"\n\n\ndef upgrade():\n'
            "    # a comment may name jsonb or ::text\n"
            '    op.execute("UPDATE t SET a = CURRENT_TIMESTAMP WHERE b IN (1, 2)")\n'
        )
        self.repo.write(V + "0156_ok.py", text)
        self.repo.commit("ok")
        self.assertEqual(self.repo.problems(), [])


class NoBase(unittest.TestCase):
    def test_a_missing_base_ref_passes_with_a_one_line_note(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = g.main(["check_migrations.py", "origin/no-such-ref-anywhere"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)
        self.assertIn("no base ref", out.getvalue())

    def test_ref_exists_tells_a_real_ref_from_a_missing_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            self.assertTrue(g.ref_exists("base", repo.path))
            self.assertFalse(g.ref_exists("origin/main", repo.path))


class PublicChain(unittest.TestCase):
    """`--public DIR`: an exported tree's migrations are one chain from one root, and make the dev tree's schema."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = Path(self._tmp.name)

    def migration(self, name: str, revision: str, down: str | None) -> None:
        file = self.tree / V / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f'revision = "{revision}"\ndown_revision = {down!r}\n', encoding="utf-8")

    def chain(self, count: int = 3) -> None:
        for n in range(1, count + 1):
            self.migration(f"000{n}_step.py", f"000{n}", f"000{n - 1}" if n > 1 else None)

    def test_one_root_and_a_walking_chain_with_a_file_for_each_revision_is_clean(self):
        self.chain()
        self.assertEqual(g.public_chain_problems(self.tree), ([], "0003"))

    def test_a_single_no_op_root_is_a_chain(self):
        self.chain(1)
        self.assertEqual(g.public_chain_problems(self.tree), ([], "0001"))

    def test_two_roots_fail(self):
        self.chain()
        self.migration("0009_second_root.py", "0009", None)
        found, head = g.public_chain_problems(self.tree)
        self.assertEqual((found, head), ([f"{V}:1: 2 roots, not one"], None))

    def test_no_root_fails(self):
        self.migration("0002_step.py", "0002", "0001")
        self.assertIn("0 roots", g.public_chain_problems(self.tree)[0][0])

    def test_a_branch_fails_by_file_and_line(self):
        self.chain()
        self.migration("0003_other.py", "0003b", "0002")
        found, _ = g.public_chain_problems(self.tree)
        self.assertEqual(found, [f"{V}0002_step.py:1: the chain branches here"])

    def test_a_file_that_is_not_on_the_chain_fails(self):
        self.chain()
        self.migration("0007_lost.py", "0007", "0006")
        found, _ = g.public_chain_problems(self.tree)
        self.assertEqual(found, [f"{V}0007_lost.py:1: not on the chain from the root (its down_revision is 0006)"])

    def test_a_revision_used_twice_fails(self):
        self.chain()
        self.migration("0004_again.py", "0002", "0001")
        self.assertIn("the revision 0002 is used twice", g.public_chain_problems(self.tree)[0][0])

    def test_the_schema_of_the_two_trees_is_compared_and_the_stamp_is_the_head(self):
        self.chain()
        same = {"master": [["table", "a", "a", "CREATE TABLE a (id)"]],
                "rows": {"a": [], "alembic_version": ['["0003"]']}}
        with mock.patch.object(g, "schema_snapshot", return_value=same):
            self.assertEqual(g.public_problems(self.tree, self.tree, "python"), [])
        other = {"master": same["master"] + [["index", "i", "a", "CREATE INDEX i ON a (id)"]], "rows": same["rows"]}
        with mock.patch.object(g, "schema_snapshot", side_effect=[same, other]):
            self.assertEqual(g.public_problems(self.tree, self.tree, "python"),
                             ["the exported tree makes a different sqlite_master from the dev tree's"])
        rows = {"master": same["master"], "rows": {**same["rows"], "a": ['[1]']}}
        with mock.patch.object(g, "schema_snapshot", side_effect=[same, rows]):
            self.assertEqual(g.public_problems(self.tree, self.tree, "python"),
                             ["the rows of a differ between the exported tree and the dev tree"])
        stale = {"master": same["master"], "rows": {"a": [], "alembic_version": ['["0002"]']}}
        with mock.patch.object(g, "schema_snapshot", return_value=stale):
            self.assertIn("stamps", g.public_problems(self.tree, self.tree, "python")[0])

    def test_main_takes_public_and_prints_nothing_but_file_and_line(self):
        self.chain()
        self.migration("0007_lost.py", "0007", "0006")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(g.main(["check_migrations.py", "--public", str(self.tree)]), 1)
        self.assertIn("0007_lost.py:1:", err.getvalue())
        self.assertEqual(g.main(["check_migrations.py", "--public", str(self.tree / "absent")]), 2)


if __name__ == "__main__":
    unittest.main()
