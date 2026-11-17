"""Reads the runbook files into a registry (the generic-sites design, Parts 2 and 3). Two places hold them:

  - the shipped folder, `app/sites/runbooks/*.json`: a site whose terms allow the app, or are silent. It ships.
  - the restricted folder, `app/backend/restricted_runbooks/`, with `runbooks/` and `pending/` (JSON), `code/` (hook
    modules) and `tests/`. It never ships. It is found in the checkout, which an editable install keeps; an install
    without it simply has no restricted runbooks (and `Registry.uncovered` says which stored hosts that leaves bare).

`robots`, `reader`, `signed_in` and `extras` are accepted from the restricted folder only, judged by the file's real
path: a copy or a link of a restricted file into the shipped folder fails to load. That is a guard against accidents,
not a security boundary. A hook is loaded by the bare name in `reader` from the restricted folder's `code/` and from
nowhere else.

A file that does not load is not skipped quietly: it becomes a `Problem` of the registry (with the hosts it names, as
far as they can be read), so every question about those hosts is refused (`registry.RunbookUnavailable`).

Reading only. Nothing is written, in a folder or a database."""

import json
from pathlib import Path

from pydantic import ValidationError

from app.sites.registry import Problem, Registry
from app.sites.schema import RESTRICTED_ONLY, Runbook

SHIPPED_DIR = Path(__file__).resolve().parent / "runbooks"
#: `app/backend/restricted_runbooks`, beside the `app` package (found in the checkout).
RESTRICTED_DIR = Path(__file__).resolve().parents[2] / "restricted_runbooks"
#: The subfolders of the restricted folder that hold runbooks, and the one that holds hooks.
RESTRICTED_RUNBOOKS = ("runbooks", "pending")
HOOKS = "code"


def _hosts_of(raw: object) -> tuple[str, ...]:
    """The hosts a document names, as far as it can be read; none if it is not a document with a list of them."""
    hosts = raw.get("hosts") if isinstance(raw, dict) else None
    if isinstance(hosts, list):
        return tuple(h.strip().lower().rstrip(".") for h in hosts if isinstance(h, str) and h.strip())
    return ()


def _reasons(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'runbook'}: {e['msg']}" for e in exc.errors())


def _read_file(file: Path, base: Path, restricted: Path | None, hooks: Path | None) -> Runbook | Problem:
    rel = file.relative_to(base).as_posix()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Problem(rel, f"not readable as JSON ({type(exc).__name__})")
    hosts = _hosts_of(raw)
    if not isinstance(raw, dict):
        return Problem(rel, "a runbook is a JSON object", hosts)
    of_restricted = restricted is not None and file.resolve().is_relative_to(restricted)
    if not of_restricted and (said := [f for f in RESTRICTED_ONLY if f in raw]):
        return Problem(rel, f"{', '.join(said)} may stand only in the restricted folder", hosts)
    try:
        runbook = Runbook.model_validate(raw)
    except ValidationError as exc:
        return Problem(rel, _reasons(exc), hosts)
    if runbook.reader and (hooks is None or not (
            (hooks / f"{runbook.reader}.py").is_file() or (hooks / runbook.reader / "__init__.py").is_file())):
        return Problem(rel, "its reader is not a module or a package of the restricted folder's code/", hosts)
    return runbook


def load(shipped_dir: Path | None = None, restricted_dir: Path | None = None,
         terms: tuple[dict, ...] | None = None) -> Registry:
    """The registry of the shipped folder and the restricted folder (each defaults to the checkout's own). A folder
    that is absent holds nothing. `terms`: the terms entries the verdicts come from (default `site_terms.json`)."""
    shipped = Path(shipped_dir) if shipped_dir is not None else SHIPPED_DIR
    restricted_root = Path(restricted_dir) if restricted_dir is not None else RESTRICTED_DIR
    present = restricted_root.is_dir()
    restricted = restricted_root.resolve() if present else None
    hooks = restricted_root / HOOKS if present else None
    if terms is None:
        from app.sites import site_terms

        terms = site_terms._load()  # pyright: ignore[reportPrivateUsage]

    found: list[Runbook] = []
    problems: list[Problem] = []
    folders = [(shipped, shipped)]
    if present:
        folders += [(restricted_root / sub, restricted_root) for sub in RESTRICTED_RUNBOOKS]
    for folder, base in folders:
        if not folder.is_dir():
            continue
        for file in sorted(folder.glob("*.json")):
            result = _read_file(file, base, restricted, hooks if hooks and hooks.is_dir() else None)
            (found if isinstance(result, Runbook) else problems).append(result)  # type: ignore[arg-type]
    return Registry(found, problems, restricted_present=present, terms=terms,
                    hook_folder=hooks if hooks and hooks.is_dir() else None)
