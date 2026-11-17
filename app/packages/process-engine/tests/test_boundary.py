"""The package stands alone: nothing in it imports an app, and its source imports only the standard library, SQLAlchemy,
defusedxml and itself. An app's code never belongs here; it plugs in through `process_engine.config`."""

import ast
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
SOURCE = PACKAGE / "src" / "process_engine"

#: What the package's source may import besides itself and the standard library (its declared dependencies).
ALLOWED_THIRD_PARTY = {"sqlalchemy", "defusedxml"}
#: Names that mean an app (or the project this was derived from): a host app's own packages and anything else that isn't this package.
FORBIDDEN = {"app", "porter", "tests", "backend", "alembic", "fastapi", "starlette", "pydantic"}


def imported_roots(path: Path) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            roots.add("<relative>")
    return roots


def test_the_source_is_where_the_test_expects_it():
    assert (SOURCE / "engine.py").is_file()


def test_no_file_of_the_package_imports_an_app():
    hits = {
        f"{path.relative_to(PACKAGE).as_posix()}: {root}"
        for path in sorted(PACKAGE.rglob("*.py")) if "build" not in path.parts and ".egg-info" not in str(path)
        for root in imported_roots(path) if root in FORBIDDEN
    }
    assert hits == set()


def test_the_source_imports_only_the_standard_library_its_dependencies_and_itself():
    allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | {"process_engine"}
    hits = {
        f"{path.relative_to(PACKAGE).as_posix()}: {root}"
        for path in sorted(SOURCE.rglob("*.py"))
        for root in imported_roots(path) if root not in allowed
    }
    assert hits == set()


def test_the_source_never_names_an_app_module_in_a_string_or_comment_that_a_reader_could_mistake_for_one():
    hits = [
        f"{path.relative_to(PACKAGE).as_posix()}:{number}"
        for path in sorted(SOURCE.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "from app." in line or "import app." in line or "app.models" in line
    ]
    assert hits == []
