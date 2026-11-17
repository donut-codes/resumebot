"""`GET /api/v1/agent/ready` is the page's own route, in a module of its own (`routes/agent_ready.py`), apart from the
lane doors in `routes/runner.py`, which are the engine's. Everything here is made up."""

from pathlib import Path

from httpx import ASGITransport, AsyncClient

from tests.split import fresh
from tests.support import OWNER

BACKEND = Path(__file__).resolve().parents[2]
READY = "/api/v1/agent/ready"


def _get_paths(app) -> list[str]:
    """Every GET path of an app. FastAPI keeps an included router as one entry that points at the router, so the
    walk goes into it (as `_keys` in tests/split/test_route_programs.py does)."""
    paths: list[str] = []

    def walk(routes) -> None:
        for route in routes:
            router = getattr(route, "original_router", None)
            if router is not None:
                walk(router.routes)
            elif "GET" in (getattr(route, "methods", None) or ()):
                paths.append(route.path)

    walk(app.routes)
    return paths


def test_the_page_serves_agent_ready_once():
    from app.jobsearch import page

    assert _get_paths(page.app).count(READY) == 1


def test_the_engine_does_not_serve_agent_ready():
    from app.jobsearch import engine

    assert READY not in _get_paths(engine.app)


def test_the_whole_app_serves_agent_ready_once():
    from app.jobsearch.main import app

    assert _get_paths(app).count(READY) == 1


async def test_the_page_app_answers_agent_ready_from_the_database(js, monkeypatch):
    from app.jobsearch import page
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
        got = await client.get(READY)
    assert got.status_code == 200
    assert got.json() == {"ready": False, "why": "No agent has checked in yet."}


def test_the_page_app_does_not_import_the_lane_doors():
    (loaded,) = fresh.loaded_after_importing("app.jobsearch.page")
    assert "app.jobsearch.routes.agent_ready" in loaded, "the page did not load agent_ready"
    assert "app.jobsearch.routes.runner" not in loaded, "the page imported the lane doors"
