"""Finds every direct write in the app (the work queue's scan, the work queue spec E3): a Core `update`,
`insert` or `delete` however it is imported or reached (`sqlalchemy`, `sqlalchemy.sql`, a dialect, `Model.__table__`,
`session.query(X).update()`, a bulk mapping call), and any raw SQL that writes. The session's change tracking (the hook)
cannot see these, so each must post a to-do beside it or sit inside `queue.exempt(reason)`;
`direct_writes_baseline.json` lists each function that writes with what it does about it, and the scan test keeps that
list true. It reads the app and the packages it vendors (`app/packages/*/src`).

It reads the source with `ast` and runs nothing; every file is parsed (no cheap look first: a file's imports can be
written any way), and the SQL words are the tripwire's own (`hook.raw_write`), so the two agree."""

import ast
import functools
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.jobsearch.work.queue.hook import raw_write

APP = Path(__file__).resolve().parents[3] / "app"
#: The packages the app vendors, which write to their own tables with statements the app's code never names.
PACKAGES = APP.parents[1] / "packages"
BASELINE = Path(__file__).with_name("direct_writes_baseline.json")

#: What a statement-making function of SQLAlchemy is called, wherever it comes from.
_WRITERS = {"update", "insert", "delete"}
#: The session's bulk writes, which bypass the change tracking.
_BULK = {"bulk_insert_mappings", "bulk_update_mappings", "bulk_save_objects"}
#: What a call that takes SQL as text is named (`text(...)`, `connection.exec_driver_sql(...)`).
_SQL_TAKERS = {"text", "exec_driver_sql"}
_QUEUE_MODULE = ("app.jobsearch.work", "queue")
#: The engine package's upsert (`portable.insert_ignore(session, Model, values, conflict=...)`): an insert on its second
#: argument, which the scan reads like `insert(Model)`.
_INSERT_IGNORE = "insert_ignore"


@dataclass
class Site:
    """One function that writes directly, and what it does about it."""

    path: str
    function: str
    statements: int = 0
    #: The model or table each statement names, where the code says (a variable stands for any model: `""`).
    targets: list[str] = field(default_factory=list)
    #: The names it uses from the queue: the topics and helpers it posts with.
    posts: list[str] = field(default_factory=list)
    #: The calls it makes to the queue's posting functions (`post`, `post_roles`...): naming a topic is not posting it.
    calls: list[str] = field(default_factory=list)
    #: The reasons of its `exempt` blocks.
    exempt: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.path}::{self.function}"


def _root(node: ast.expr) -> str | None:
    """The name an attribute chain starts from (`a` in `a.b.c`), or None when it starts from something else."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _static(node: ast.expr) -> str | None:
    """The text a SQL argument spells out where the code spells it: a string, an f-string (each hole is `x`), pieces
    added or `%`-formatted together, or `"...".format(...)`; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "x" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        left, right = _static(node.left), _static(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "x") + (right if right is not None else "x")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _static(node.func.value)
    return None


def _model(node: ast.expr | None) -> str:
    """The model or table a statement is made on, as the code writes it (a variable the code names: its name; anything
    else: the empty string)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return ast.unparse(node)
    return ""


def _target(call: ast.Call) -> str:
    return _model(call.args[0] if call.args else None)


def _query_of(node: ast.expr) -> ast.Call | None:
    """The `x.query(...)` a chain of `.filter(...)` calls starts from, if it does."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "query":
            return node
        node = node.func.value
    return None


class _Scan(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.sites: dict[str, Site] = {}
        self.stack: list[str] = []
        self.params: list[set[str]] = []  # the parameter names of each function being read, innermost last
        self.writers: set[str] = set()  # local names of update, insert, delete (from any SQLAlchemy module)
        self.modules: set[str] = set()  # local names that stand for SQLAlchemy or a module of it
        self.queue: set[str] = set()  # local names of the queue module
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    local = alias.asname or alias.name
                    if module == "sqlalchemy" or module.startswith("sqlalchemy."):
                        (self.writers if alias.name in _WRITERS else self.modules).add(local)
                    if module == _QUEUE_MODULE[0] and alias.name == _QUEUE_MODULE[1]:
                        self.queue.add(local)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sqlalchemy" or alias.name.startswith("sqlalchemy."):
                        self.modules.add(alias.asname or alias.name.split(".")[0])

    def _site(self) -> Site:
        name = ".".join(self.stack) or "<module>"
        return self.sites.setdefault(name, Site(self.path, name))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        args = node.args
        self.params.append({a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)})
        self.generic_visit(node)
        self.params.pop()
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in self.queue:
            site = self._site()
            if node.attr not in site.posts and (node.attr.startswith("post") or node.attr.isupper()
                                                or node.attr in ("ready", "browser_ready")):
                site.posts.append(node.attr)
        self.generic_visit(node)

    def _statement(self, call: ast.Call) -> tuple[str, str] | None:
        """(verb, the model or table) when this call makes a statement that writes, else None."""
        func = call.func
        if (isinstance(func, ast.Name) and func.id == _INSERT_IGNORE
                or isinstance(func, ast.Attribute) and func.attr == _INSERT_IGNORE):
            return "insert", _model(call.args[1] if len(call.args) > 1 else None)
        if isinstance(func, ast.Name) and func.id in self.writers:
            return func.id, _target(call)
        if not isinstance(func, ast.Attribute):
            return None
        if func.attr in _WRITERS:
            if _root(func.value) in self.modules:  # sa.update(X), postgresql.insert(X), sqlalchemy.sql.delete(X)
                return func.attr, _target(call)
            if isinstance(func.value, ast.Attribute) and func.value.attr == "__table__":  # X.__table__.update()
                return func.attr, _model(func.value.value)
            query = _query_of(func.value) if func.attr != "insert" else None
            if query is not None:  # session.query(X).filter(...).update()
                return func.attr, _target(query)
        if func.attr in _BULK:
            return func.attr, _target(call)
        return None

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        found = self._statement(node)
        taker = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if (found is not None and self.stack and self.stack[-1] == _INSERT_IGNORE and self.params
                and found[1] in self.params[-1]):
            found = None  # the upsert's own definition writes any table it is given; its callers are read as inserts
            # (a general rule for any parameter would hide every helper that takes a model or a session)
        if found is not None and found[1] != "Todo":  # the queue's own table is not a writer to answer for
            site = self._site()
            site.statements += 1
            site.targets.append(found[1])
        elif found is None and taker in _SQL_TAKERS and node.args and (sql := _static(node.args[0])) is not None:
            raw = raw_write(sql)
            if raw:
                site = self._site()
                site.statements += 1
                site.targets.append("raw:" + raw[1])
        elif (isinstance(func, ast.Attribute) and func.attr == "exempt" and isinstance(func.value, ast.Name)
              and func.value.id in self.queue and node.args and isinstance(node.args[0], ast.Constant)):
            site = self._site()
            site.exempt.append(str(node.args[0].value))
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in self.queue
                and func.attr.startswith("post")):
            site = self._site()
            site.calls.append(func.attr)
        self.generic_visit(node)


@functools.cache
def _whole() -> dict[str, Site]:
    return _read([(APP, APP.parent), *((src, PACKAGES.parent) for src in sorted(PACKAGES.glob("*/src")))])


def scan(root: Path | None = None) -> dict[str, Site]:
    """Every function that writes directly, by `path::function`, and what it uses of the queue. With no `root`: the app
    (paths from `app/...`) and each vendored package's `src` (paths from `packages/...`), read once for the whole run;
    with one, that folder alone."""
    return _read([(root, root.parent)]) if root is not None else _whole()


def _read(roots: list[tuple[Path, Path]]) -> dict[str, Site]:
    found: dict[str, Site] = {}
    for top, base in roots:
        for file in sorted(top.rglob("*.py")):
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
            walker = _Scan(file.relative_to(base).as_posix(), tree)
            walker.visit(tree)
            for site in walker.sites.values():
                if site.statements:
                    found[site.key] = site
    return found


def baseline() -> dict[str, dict]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


#: What a person writes in the baseline, which `write_baseline` keeps.
KEPT = ("answered_by", "unwatched")


def write_baseline(found: dict[str, Site]) -> None:
    """Write the baseline as the scan finds it (a builder's step, then read what it says before committing). What a
    person wrote in it (`answered_by`: the function whose post answers a statement made in this one; `unwatched`: why a
    vendored package's statement on a table nobody watches needs no post) is kept."""
    had = baseline() if BASELINE.exists() else {}
    data = {}
    for site in found.values():
        entry = {"statements": site.statements, "targets": sorted(set(site.targets)), "posts": sorted(site.posts),
                 "exempt": site.exempt}
        for kept in KEPT:
            if kept in had.get(site.key, {}):
                entry[kept] = had[site.key][kept]
        data[site.key] = entry
    BASELINE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
