"""The page and engine programs (`app.jobsearch.page`, `app.jobsearch.engine`): each app answers `/health`, the page's
start says which program it is and takes no instance lock, and importing the page loads none of the engine's route
modules, loops or doors."""

import pytest
from httpx import ASGITransport, AsyncClient

from app import db, instance_lock, log_setup, program
from app.jobsearch import engine, page
from app.settings import APP_NAME, VERSION
from tests.split import fresh

#: Modules that importing the page program must not load: the whole app, the engine's loops, the doors and one of the
#: engine's route modules. The page loads its own route modules (`routes/agent_ready.py` was the first), never these.
#: Launch in Browser's runner and the add-a-link reader too: the role routes import each where it is used.
NOT_LOADED = (
    "app.jobsearch.main", "app.jobsearch.engine", "app.jobsearch.loops.schedule", "app.jobsearch.loops.startup",
    "app.jobsearch.work.queue.loop", "app.jobsearch.doors.scout", "app.jobsearch.browser.code",
    "app.jobsearch.routes.runner", "app.jobsearch.browser.go", "app.jobsearch.browser.add_link")


@pytest.mark.parametrize("module", [page, engine], ids=["page", "engine"])
async def test_each_programs_app_answers_health(js, module):
    async with AsyncClient(transport=ASGITransport(app=module.app), base_url="http://localhost:8430") as client:
        reply = await client.get("/health")
    assert reply.status_code == 200
    assert reply.json() == {"app": APP_NAME, "status": "ok", "version": VERSION, "db": "ok"}


async def _schema_is_current(seconds: float = 3.0) -> bool:
    return True


async def test_the_pages_start_says_it_is_the_page_and_takes_no_instance_lock(monkeypatch):
    ran: list[str] = []

    def no_lock(url):
        # The page's start never calls this today; the test fails the day a change makes it take the engine's lock.
        raise AssertionError("the page takes no instance lock")

    monkeypatch.setattr(program, "PROGRAM", "all")
    monkeypatch.setattr(instance_lock, "hold", no_lock)
    monkeypatch.setattr(log_setup, "configure_logging", lambda: ran.append("logging"))
    monkeypatch.setattr(db, "configure", lambda url=None: ran.append("configure"))
    monkeypatch.setattr(page, "session_managers", lambda role: [])
    monkeypatch.setattr(db, "schema_is_current", _schema_is_current)  # no database is set up in this test
    async with page.lifespan(page.app):
        assert program.PROGRAM == "page"
        assert ran == ["logging", "configure"]


def test_importing_the_page_program_loads_no_engine_route_module_and_no_loop():
    (loaded,) = fresh.loaded_after_importing("app.jobsearch.page")
    assert [name for name in NOT_LOADED if name in loaded] == []
