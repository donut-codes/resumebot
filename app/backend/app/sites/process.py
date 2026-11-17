"""The site-project processes, loaded and ready to run: `playbooks/sites/*.bpmn` in a library of their own, bound to the
site actions (`app.sites.round_process.registry`).

The engine is the `process_engine` package, configured once by `app.jobsearch.process` (the roles an agent step may
name, `scout` among them; the instruction and template files; the card kinds, `plan_approval` among them; the key of an
instance's data that holds its owner; the hook that holds a failed run). This module reuses that one configuration so
the two libraries never disagree about roles or cards, and builds the library the same way. It is separate from the
job-search library on purpose: the two files' keys (`jobsearch_round`; `project_round`, `do_one_item`, `check_one_item`)
never meet, and a change to one does not change the other's running processes.
"""

from functools import lru_cache
from pathlib import Path

from process_engine.engine import Engine
from process_engine.loader import ProcessLibrary
from process_engine.registry import Catalog

from app.config import settings as base_settings
from app.jobsearch import process as jobsearch_process
from app.sites import round_process

ROUND, DO, CHECK = round_process.KEY, round_process.ITEM_KEY, round_process.CHECK_KEY


def process_dir() -> Path:
    return Path(base_settings.playbooks_path) / "sites"


def catalog() -> Catalog:
    return jobsearch_process.catalog()


def build_library() -> ProcessLibrary:
    library = ProcessLibrary()
    cat = catalog()
    for path in sorted(process_dir().glob("*.bpmn")):
        library.load(path.read_text(encoding="utf-8"), catalog=cat, registry=round_process.registry)
    library.validate()
    return library


@lru_cache(maxsize=1)
def library() -> ProcessLibrary:
    return build_library()


def engine() -> Engine:
    """A live engine with no hand-out hook (a test starts a run and feeds it; the entries in `round_process` use
    `live.live_engine`, which hands steps out)."""
    return jobsearch_process.AppEngine(library(), round_process.registry, catalog=catalog())
