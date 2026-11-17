#!/usr/bin/env python3
"""The app's database migrations only go forward (the owner, 2026-10).

A migration that has been released has run on a real database; changing it afterwards leaves that database and a new one
with different schemas and nothing to say so. So, against a base commit (origin/main by default):

  - an existing file under app/backend/migrations/versions/ may not be deleted, renamed or have its code changed. A
    change to its docstring or comments is fine (they are not part of what runs); a new file is the only other way;
  - nothing outside the migrations may run a downgrade (`alembic downgrade`, `command.downgrade`): a migration's own
    `downgrade()` is never relied on;
  - a migration newer than the newest Postgres-era one (revision 0154) may hold no Postgres-only SQL: it has to run on
    SQLite too. Refused: a `postgresql` dialect import, and in any string of the file (a docstring is not read) plpgsql,
    CREATE FUNCTION / TRIGGER, `::type` casts, jsonb, ARRAY[, = ANY(, DISTINCT ON, ILIKE, AT TIME ZONE, now(),
    INTERVAL, information_schema and pg_ names. Migrations up to 0154 are exempt: they never run on SQLite.

  - `--public DIR` checks an exported tree's chain instead (no git, no base): exactly one root, a chain that walks from
    it to one head without a gap or a branch, one file for each revision of it, and, given `--against DEV_DIR` and
    `--python PYTHON` (an environment that has the app's packages), that a new install made by each tree's
    `create_schema` is the same: the same `sqlite_master`, the same rows in every table, and the stamped revision
    equal to the exported head.

Stdlib only; reads git, changes nothing. It is run by hand beside a release's whole check, and by the Windows job on
GitHub when that is started by hand.

Usage: python scripts/check_migrations.py [BASE_REF]      (default origin/main)
       python scripts/check_migrations.py --public DIR [--against DEV_DIR --python PYTHON]
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = "app/backend/migrations/versions/"
DOWNGRADE_RUNNERS = re.compile(r"\bcommand\.downgrade\b|\balembic\s+downgrade\b")
#: The newest migration written for Postgres. Later ones must run on SQLite as well.
LAST_POSTGRES_REVISION = 154
POSTGRES_ONLY_SQL = (
    (r"\bplpgsql\b", "plpgsql"),
    (r"\bcreate\s+(or\s+replace\s+)?(function|trigger)\b", "CREATE FUNCTION/TRIGGER"),
    (r"::\s*[a-z_]", "a ::type cast"),
    (r"\bjsonb\b", "jsonb"),
    (r"\barray\s*\[", "ARRAY[...]"),
    (r"=\s*any\s*\(", "= ANY(...)"),
    (r"\bdistinct\s+on\b", "DISTINCT ON"),
    (r"\bilike\b", "ILIKE"),
    (r"\bat\s+time\s+zone\b", "AT TIME ZONE"),
    (r"\bnow\s*\(", "now()"),
    (r"\binterval\b", "INTERVAL"),
    (r"\binformation_schema\b", "information_schema"),
    (r"\bpg_\w+", "a pg_ name"),
)
SCANNED = (".py", ".sh", ".yml", ".yaml", ".ini", ".toml", ".cfg")


def git(*args: str, root: Path = ROOT) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise SystemExit(f"check_migrations: git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def ref_exists(ref: str, root: Path = ROOT) -> bool:
    done = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], capture_output=True
    )
    return done.returncode == 0


def code_of(text: str) -> str:
    """What the file does: its syntax tree without docstrings. Comments and layout are not in a tree at all."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.dump(tree)


def revision_number(path: str) -> int | None:
    """The number a migration's file name starts with (0155_x.py is 155), or None for a file that has none."""
    match = re.match(r"(\d+)_", path.rsplit("/", 1)[-1])
    return int(match.group(1)) if match else None


def postgres_only(text: str) -> list[str]:
    """What in a migration's code only Postgres understands: a postgresql dialect import, or Postgres SQL in any string
    that is not a docstring (comments are not code)."""
    tree = ast.parse(text)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and "postgresql" in (node.module or "").split("."):
            found.append("imports the postgresql dialect")
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy.dialects" and any(a.name == "postgresql" for a in node.names):
            found.append("imports the postgresql dialect")
        elif isinstance(node, ast.Import) and any("postgresql" in a.name.split(".") for a in node.names):
            found.append("imports the postgresql dialect")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            for pattern, name in POSTGRES_ONLY_SQL:
                if re.search(pattern, node.value, re.IGNORECASE):
                    found.append(f"has {name} in a string")
    return sorted(set(found))


def postgres_only_problems(root: Path = ROOT) -> list[str]:
    found = []
    for rel in git("ls-files", VERSIONS, root=root).splitlines():
        number = revision_number(rel)
        if number is None or number <= LAST_POSTGRES_REVISION or not rel.endswith(".py"):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
            reasons = postgres_only(text)
        except (OSError, UnicodeDecodeError):
            continue
        except SyntaxError as e:
            found.append(f"{rel}: does not parse ({e.msg}, line {e.lineno})")
            continue
        for reason in reasons:
            found.append(f"{rel}: {reason}; migrations after {LAST_POSTGRES_REVISION:04d} must run on SQLite too")
    return found


def changed_migrations(base: str, root: Path = ROOT) -> list[tuple[str, str]]:
    """(status, path) for each migration file that is not simply new, from where HEAD left `base` to HEAD."""
    fork = git("merge-base", base, "HEAD", root=root).strip()
    out = git("diff", "--name-status", "--no-renames", fork, "HEAD", "--", VERSIONS, root=root)
    found = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        if status != "A":
            found.append((status, path))
    return found


def problems(base: str, root: Path = ROOT) -> list[str]:
    fork = git("merge-base", base, "HEAD", root=root).strip()
    found = []
    for status, path in changed_migrations(base, root):
        if status != "M":
            found.append(f"{path}: a released migration was deleted or renamed ({status}); add a new migration instead")
            continue
        old = git("show", f"{fork}:{path}", root=root)
        new = git("show", f"HEAD:{path}", root=root)
        try:
            same = code_of(old) == code_of(new)
        except SyntaxError as e:
            found.append(f"{path}: does not parse ({e.msg}, line {e.lineno})")
            continue
        if not same:
            found.append(f"{path}: a released migration's code changed; add a new migration instead (comments and docstrings may change)")
    found.extend(postgres_only_problems(root))
    for rel in git("ls-files", root=root).splitlines():
        if rel.startswith(VERSIONS) or not rel.endswith(SCANNED) or rel == "scripts/check_migrations.py":
            continue
        if rel.endswith("test_check_migrations.py"):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if DOWNGRADE_RUNNERS.search(line):
                found.append(f"{rel}:{number}: runs a downgrade; the migrations only go forward")
    return found


def revisions(text: str) -> tuple[str, str | None] | None:
    """(revision, down_revision) of a migration file; None when it assigns no revision."""
    found: dict[str, object] = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                    found[target.id] = node.value.value
    revision, down = found.get("revision"), found.get("down_revision")
    return (revision, down if isinstance(down, str) else None) if isinstance(revision, str) else None


def public_chain_problems(tree: Path) -> tuple[list[str], str | None]:
    """(problems, the head) of the migrations of an exported tree: one root, a walk from it to one head, a file for each
    revision of the chain and no file that is not in it."""
    folder = tree / VERSIONS
    nodes: dict[str, tuple[str | None, str]] = {}
    found: list[str] = []
    files = sorted(folder.glob("[0-9]*.py")) if folder.is_dir() else []
    for file in files:
        rel = file.relative_to(tree).as_posix()
        try:
            pair = revisions(file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            found.append(f"{rel}:1: does not parse")
            continue
        if pair is None:
            found.append(f"{rel}:1: assigns no revision")
        elif pair[0] in nodes:
            found.append(f"{rel}:1: the revision {pair[0]} is used twice")
        else:
            nodes[pair[0]] = (pair[1], rel)
    if found:
        return found, None
    roots = [r for r, (down, _) in nodes.items() if down is None]
    if len(roots) != 1:
        return [f"{VERSIONS}:1: {len(roots)} roots, not one"], None
    children: dict[str, list[str]] = {}
    for revision, (down, _) in nodes.items():
        if down is not None:
            children.setdefault(down, []).append(revision)
    walked, at = [roots[0]], roots[0]
    while at in children:
        if len(children[at]) != 1:
            return [f"{nodes[at][1]}:1: the chain branches here"], None
        at = children[at][0]
        walked.append(at)
    for revision, (down, rel) in nodes.items():
        if revision not in walked:
            found.append(f"{rel}:1: not on the chain from the root (its down_revision is {down})")
    if not found and len(walked) != len(files):
        found.append(f"{VERSIONS}:1: {len(files)} files for a chain of {len(walked)}")
    return found, walked[-1]


#: Run in a subprocess of the tree's own Python environment: the install a new user gets, as data.
SNAPSHOT = """
import json, os
import sqlalchemy as sa
from app import setup_cli
path = os.path.join(os.environ["SNAPSHOT_DIR"], "new.db").replace(os.sep, "/")
assert setup_cli.create_database("sqlite+aiosqlite:///" + path), "no database was made"
engine = sa.create_engine("sqlite:///" + path)
out = {"master": [], "rows": {}}
with engine.connect() as c:
    out["master"] = sorted(map(list, c.execute(sa.text(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))))
    names = [n for (n,) in c.execute(sa.text("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))]
    for name in names:
        rows = c.execute(sa.text('SELECT * FROM "' + name + '"')).fetchall()
        out["rows"][name] = sorted(json.dumps(list(r), default=str) for r in rows)
engine.dispose()
print(json.dumps(out, sort_keys=True))
"""


def schema_snapshot(tree: Path, python: str) -> dict:
    """What a new install of `tree` is: every object of its sqlite_master and every row of every table, as made by the
    tree's own `create_schema` and stamped at its head, read in a subprocess of `python` (which has the packages)."""
    backend = tree / "app" / "backend"
    with tempfile.TemporaryDirectory(prefix="public-schema-") as scratch:
        paths = [str(backend), str(tree / "app" / "packages" / "process-engine" / "src")]
        env = dict(os.environ, PYTHONUTF8="1", SNAPSHOT_DIR=scratch, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=os.pathsep.join(paths))
        env.pop("DATABASE_URL", None)
        env.pop("RESUMEBOT_DATA_DIR", None)
        done = subprocess.run([python, "-B", "-c", SNAPSHOT], cwd=backend, env=env, capture_output=True, text=True,
                              encoding="utf-8")
    if done.returncode != 0:
        raise SystemExit(f"check_migrations: a new install of {tree} could not be made:\n{done.stderr.strip()[-2000:]}")
    return json.loads(done.stdout.splitlines()[-1])


def public_problems(tree: Path, against: Path | None, python: str) -> list[str]:
    found, head = public_chain_problems(tree)
    if found or against is None:
        return found
    exported, dev = schema_snapshot(tree, python), schema_snapshot(against, python)
    if exported["master"] != dev["master"]:
        found.append("the exported tree makes a different sqlite_master from the dev tree's")
    for name in sorted(set(exported["rows"]) | set(dev["rows"])):
        if name != "alembic_version" and exported["rows"].get(name) != dev["rows"].get(name):
            found.append(f"the rows of {name} differ between the exported tree and the dev tree")
    stamped = exported["rows"].get("alembic_version")
    if stamped != [json.dumps([head])]:
        found.append(f"the exported tree stamps {stamped}, not its head {head}")
    return found


def public_main(argv: list[str]) -> int:
    args = list(argv)
    options: dict[str, str | None] = {"--against": None, "--python": sys.executable}
    tree = Path(args.pop(0)) if args and not args[0].startswith("-") else None
    while args:
        flag = args.pop(0)
        if flag not in options or not args:
            print(__doc__, file=sys.stderr)
            return 2
        options[flag] = args.pop(0)
    if tree is None or not tree.is_dir():
        print(__doc__, file=sys.stderr)
        return 2
    against = Path(options["--against"]) if options["--against"] else None
    found = public_problems(tree, against, str(options["--python"]))
    for line in found:
        print(f"check_migrations: {line}", file=sys.stderr)
    if found:
        return 1
    same = " and makes the dev tree's schema" if against else ""
    print(f"check_migrations: the exported chain is one line from one root{same}")
    return 0


def main(argv: list[str]) -> int:
    if argv[1:2] == ["--public"]:
        return public_main(argv[2:])
    if len(argv) > 2 or (len(argv) == 2 and argv[1].startswith("-")):
        print(__doc__, file=sys.stderr)
        return 2
    base = argv[1] if len(argv) == 2 else "origin/main"
    if not ref_exists(base):
        print(f"check_migrations: no base ref {base} (the first commit of a fresh repository); nothing to compare, passing")
        return 0
    found = problems(base)
    for line in found:
        print(f"check_migrations: {line}", file=sys.stderr)
    if found:
        return 1
    print(f"check_migrations: the migrations only go forward and the new ones are SQLite-safe (against {base})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
