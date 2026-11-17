"""The playbooks say where a step's work is done, for example `sites/loop.py:advance_after_report` or
`harness/loader.py:Kind.feeds_next_plan`. Each pointer is a file under backend/app and, after the colon, a function,
class or field defined in it. A move or a rename in the code must fail here, not leave a playbook pointing nowhere."""

import re

from app import db
from app.config import settings

BACKEND_APP = db.BACKEND_DIR / "app"
#: `dir/file.py`, optionally `:name` (or `:Class.field`). Bare file names with no folder are not pointers.
POINTER = re.compile(r"(?<![\w/.-])((?:[A-Za-z_][\w-]*/)+[A-Za-z_][\w-]*\.py)(?::([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*))?")


def _defines(source: str, name: str) -> bool:
    return re.search(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b|^\s*{re.escape(name)}\s*[:=]",
                     source, re.MULTILINE) is not None


def _pointers():
    for path in sorted(p for p in settings.playbooks_path.rglob("*") if p.is_file()):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in POINTER.finditer(line):
                yield path, n, m.group(1), m.group(2)


def test_every_code_pointer_in_a_playbook_resolves():
    found, broken = 0, []
    for path, n, rel, name in _pointers():
        found += 1
        rel = rel.removeprefix("backend/app/")
        target = BACKEND_APP / rel
        if not target.is_file():
            broken.append(f"{path.name}:{n}: no file {rel}")
        elif name:
            source = target.read_text(encoding="utf-8")
            for part in name.split("."):
                if not _defines(source, part):
                    broken.append(f"{path.name}:{n}: {rel} defines no {part}")
    assert found > 20, "the playbooks should point at code in many places"
    assert broken == []
