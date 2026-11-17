"""The owner's action door on the page program. The door's modules are loaded by the page, so
they must load none of the engine's loops or starters (`tests/split/forbidden.py`); each read action answers through the
door on a reader and takes no write turn. Its writes go to the engine (tests/split/test_owner_door_split.py).
Everything is made up."""

import contextlib
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app import db, log_setup, program, web
from app.jobsearch import engine, page, stages
from app.jobsearch.models import Opportunity
from app.jobsearch.owner_actions import mcp as owner_mcp
from app.jobsearch.owner_actions.registry import REGISTRY
from app.settings import settings
from tests.split import forbidden, fresh
from tests.split.test_page_reads_take_no_write_lock import turns_recorded
from tests.support import NOON, OWNER, load_profile, pin_clock

TOKEN = "js_owner_token_page_1234567890"
READS = ["health", "mail.state", "night.summary", "onboarding.state", "postings.sample", "profile.read", "queues",
         "reanalysis.compare", "resume.read", "role.state", "stops.list"]


PACKAGE = "app.jobsearch.owner_actions"
DOOR, ASKED = PACKAGE + ".mcp", PACKAGE + ".asked"


def _engine_modules_in(loaded: frozenset[str]) -> list[str]:
    return [name for name in forbidden.FORBIDDEN if name in loaded]


@pytest.fixture
def door(tmp_path, monkeypatch):
    """A context carrying the owner's token, the token file it is compared with, and the clock at noon."""
    (tmp_path / "owner-token").write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr(settings, "owner_token_file", str(tmp_path / "owner-token"))
    monkeypatch.setattr(settings, "owner", OWNER)
    pin_clock(monkeypatch, NOON)
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


def test_the_owner_actions_load_no_engine_module():
    # One fresh interpreter imports the package and then the door module (which loads the package first anyway), with a
    # snapshot after each: the package's is taken before the door module is imported, so it is the package alone.
    package_alone, _ = fresh.loaded_after_importing(PACKAGE, DOOR)
    assert _engine_modules_in(package_alone) == []


def test_the_door_module_loads_no_engine_module():
    _, with_door = fresh.loaded_after_importing(PACKAGE, DOOR)
    assert _engine_modules_in(with_door) == []


def test_the_actions_that_only_read_are_these_eleven():
    assert sorted(name for name, action in REGISTRY.items() if not action.writes) == READS


async def test_each_read_action_answers_through_the_door_and_takes_no_write_turn(js, door):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        role = Opportunity(owner=OWNER, slug="od1", company="Co od1", title="Role od1", status=stages.READY_TO_LAUNCH,
                           origin="tracker", match_key="co od1|role od1", posted=NOON.date(), found=NOON.date())
        session.add(role)
        await session.commit()
    sent = {"role.state": {"role": str(role.id)}, "postings.sample": {"target": "Role od1"}}
    took: dict[str, int] = {}
    for name in READS:
        with turns_recorded() as turns:
            got = await owner_mcp.act_read(door, name, sent.get(name, {}))
        assert got["ok"] is True, (name, got)
        if turns.waits or turns.ends:
            took[name] = len(turns.waits)
    assert not took, f"read actions that asked for the write turn (name and how many times): {took}"


async def test_the_listing_names_every_action_and_takes_no_write_turn(js, door):
    with turns_recorded() as turns:
        got = await owner_mcp.actions_list(door)
    assert got["ok"] is True
    assert {a["name"] for a in got["actions"]} == set(REGISTRY)
    assert not turns.waits and not turns.ends


async def test_the_page_app_mounts_the_door_behind_its_token_and_the_engine_app_does_not():
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Authorization": "Bearer not-the-owner-token"}
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://localhost:8430") as client:
        on_the_page = await client.post("/mcp-owner/", json=body, headers=headers)
    async with AsyncClient(transport=ASGITransport(app=engine.app), base_url="http://localhost:8430") as client:
        on_the_engine = await client.post("/mcp-owner/", json=body, headers=headers)
    assert on_the_page.status_code == 401, "the page's door answers, behind the owner's token"
    assert on_the_engine.status_code == 404, "the engine no longer has this door"


async def _schema_is_current(seconds: float = 3.0) -> bool:
    return True


async def test_the_pages_start_runs_the_session_manager_of_each_door_it_mounts(monkeypatch):
    seen: list[str] = []

    class Manager:
        @contextlib.asynccontextmanager
        async def run(self):
            seen.append("running")
            yield
            seen.append("stopped")

    def managers(role):
        seen.append(role)
        return [Manager()]

    monkeypatch.setattr(program, "PROGRAM", "all")  # `lifespan` sets it; the test's own value comes back afterwards
    monkeypatch.setattr(page, "session_managers", managers)
    monkeypatch.setattr(db, "schema_is_current", _schema_is_current)  # no database is set up in this test
    for owner, name in ((db, "configure"), (web, "log_page"), (log_setup, "configure_logging")):
        monkeypatch.setattr(owner, name, lambda *args, **kwargs: None)
    async with page.lifespan(page.app):
        assert seen == ["page", "running"]
    assert seen == ["page", "running", "stopped"]


def test_the_asked_module_of_the_door_loads_no_engine_module():
    (with_asked,) = fresh.loaded_after_importing(ASKED)  # its own interpreter: it is a sibling of the door module
    assert _engine_modules_in(with_asked) == []
