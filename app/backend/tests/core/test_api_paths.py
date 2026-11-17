"""The app's API addresses are written down in `tests/api_paths.json` (every path and its methods, from the app's own
OpenAPI document), and the frontend keeps a copy (`frontend/src/api/api-paths.json`) that its own test checks every call
in `client.ts` against. A route added, renamed or removed fails here until the file is changed with it, so a screen's
call can no longer go on pointing at a route that is gone."""

import json

import pytest

from app import db
from app.jobsearch.main import app

SNAPSHOT = db.BACKEND_DIR / "tests" / "api_paths.json"
#: The frontend's copy; absent in the backend's own test image, which holds only the backend.
FRONTEND_COPY = db.BACKEND_DIR.parent / "frontend" / "src" / "api" / "api-paths.json"
HOW_TO_FIX = (
    "The API's paths changed. Write the JSON below to app/backend/tests/api_paths.json and to "
    "app/frontend/src/api/api-paths.json (the frontend's test checks client.ts against it):\n")


def _live() -> dict[str, list[str]]:
    methods = ("get", "post", "put", "patch", "delete")
    return {path: sorted(m.upper() for m in ops if m in methods)
            for path, ops in sorted(app.openapi()["paths"].items())}


def _written(paths: dict[str, list[str]]) -> str:
    """The file's own layout: one path to a line."""
    return "{\n" + ",\n".join(f"  {json.dumps(p)}: {json.dumps(m)}" for p, m in paths.items()) + "\n}\n"


def test_the_api_paths_on_file_are_the_ones_the_app_serves():
    live, on_file = _live(), json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    added = sorted(set(live) - set(on_file))
    removed = sorted(set(on_file) - set(live))
    changed = sorted(p for p in set(live) & set(on_file) if live[p] != on_file[p])
    assert (added, removed, changed) == ([], [], []), f"added {added}, removed {removed}, methods changed {changed}\n" \
        + HOW_TO_FIX + _written(live)
    assert SNAPSHOT.read_text(encoding="utf-8") == _written(live), "same paths, but the file is not in its layout"


def test_the_frontends_copy_of_the_paths_is_the_same_file_where_both_are_in_the_checkout():
    if not FRONTEND_COPY.exists():
        pytest.skip("the backend's test image holds only the backend; a checkout holds both")
    assert FRONTEND_COPY.read_text(encoding="utf-8") == SNAPSHOT.read_text(encoding="utf-8"), HOW_TO_FIX
