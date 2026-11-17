"""Migration 0049's rules, for tests that need a document an older release saved, written as the app reads it now. The
Gates are then written without the fit bar, as the migration that took it away does (0049 itself still writes `fit`)."""

import copy
import importlib.util
from datetime import datetime

from app import db
from tests.support import clock_at


def _load(pattern: str):
    """A migration's module, read from its file (a name that starts with a number is not importable)."""
    path = next((db.BACKEND_DIR / "migrations" / "versions").glob(pattern))
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load("0049_stored_versions_to_today.py")
#: The migration that took the fit bar away: the Gates without `fit`, and the roles it had stopped.
fit_bar = _load("*_fit_bar_removed.py")


def migrations_read_the_clock_at(monkeypatch, moment: datetime, *numbers: str) -> None:
    """The migrations with these file numbers (`"0059"`) read `moment` when they call `datetime.now(...)`, in the next
    upgrade that loads them. A released migration is never edited, so it cannot be handed its clock, and one that counts
    a day from "now" counts it from the real one: a test that made its rows from the clock at the start of the run found
    them a day older when the upgrade ran past UTC midnight. Only for a
    migration that reads nothing of `datetime` but `now`."""
    from alembic.util import pyfiles

    real = pyfiles.load_module_py

    def load(module_id, path):
        module = real(module_id, path)
        if module_id.split("_", 1)[0] in numbers:
            module.datetime = clock_at(moment)
        return module

    monkeypatch.setattr(pyfiles, "load_module_py", load)


def via(kind: str, doc: dict) -> dict:
    """The document as the migrations write it (a document already in today's shape is left as it is)."""
    is_old, to_today = migration.CONVERTERS[kind]
    out = to_today(copy.deepcopy(doc)) if is_old(doc) else copy.deepcopy(doc)
    return fit_bar.gates_without_fit(out) if kind == "gates" else out
