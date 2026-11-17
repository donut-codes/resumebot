"""Built to share (docs/PRD.md 2.8): the product names no one's preferences, and a new install starts empty.

The check that no one's places, employers or pay figures are in the repository is scripts/check_no_one.py, run by
CI on every pull request: its shape rules sit in the repo, and the words themselves in a file outside it, so this
suite no longer carries a list of them. What stays here: the product files are where that check expects, and an empty
profile lets every role through."""

from pathlib import Path

from app import db
from app.config import settings
from app.jobsearch.measures.pay_place import Gates
from app.jobsearch.profile import schema as profile
from app.jobsearch.profile import views as pv

APP = db.BACKEND_DIR / "app"


def _product_files() -> list[Path]:
    files = [p for p in APP.rglob("*.py")]
    for folder in (settings.playbooks_path, settings.instructions_path, settings.templates_path):
        files += [p for p in folder.rglob("*") if p.is_file()]
    return files


def test_the_product_files_are_found():
    names = {p.name for p in _product_files()}
    assert {"schema.py", "pay_place.py", "jobsearch.scout.instructions.md", "screen_one_role.bpmn"} <= names


def test_a_new_install_starts_empty_and_lets_every_role_through():
    empty = profile.Profile()
    assert not empty.is_set()
    assert pv.engine_gates(empty) == Gates()
    assert Gates().comp_floor == 0 and Gates().excluded_employers == [] and not Gates().places_gated
