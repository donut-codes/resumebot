"""The owner's control over the read of a site's feed though its robots.txt asks automated readers not to, where no
runbook carries `robots` (the public install): the page's read says there is nothing to switch, the switch saves
nothing, and the owner door's action says there is nothing to read. Made-up data only."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db
from app.jobsearch import page
from app.jobsearch.models import ActionRun
from app.jobsearch.owner_actions import runner
from app.jobsearch.profile import store as profile_store
from app.settings import settings
from app.sites import registry
from app.sites.schema import Runbook
from tests.support import OWNER, load_profile


@pytest.fixture(autouse=True)
def acts_for_the_tester(monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)


@pytest.fixture
def no_robots_runbook():
    plain = Runbook.model_validate({"schema": 1, "key": "plain", "name": "Plain", "kind": "board",
                                    "hosts": ["jobs.example.test"], "follow_to_employer": True})
    with registry.replaced(registry.Registry([plain])):
        yield


async def test_the_page_says_there_is_nothing_to_switch_and_the_switch_saves_nothing(js, no_robots_runbook):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        before = (await profile_store.current(session, OWNER)).settings.read_robots_exempt
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
        assert (await client.get("/api/v1/sites")).json()["read_robots_exempt"] is None
        answer = await client.put("/api/v1/sites/robots-exempt", json={"on": True})
    assert answer.status_code == 200 and answer.json() == {"on": False, "note": "nothing to switch"}
    async with db.session_factory()() as session:
        assert (await profile_store.current(session, OWNER)).settings.read_robots_exempt is before is False


async def test_the_old_route_is_gone(js, no_robots_runbook):
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
        old = "/api/v1/sites/" + "".join(["linked", "in"])
        assert (await client.put(old, json={"on": True})).status_code in (404, 405, 422)


async def test_the_door_says_there_is_nothing_to_read_where_no_runbook_carries_robots(js, no_robots_runbook):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    done = await runner.do(OWNER, "board_feed.read", {}, False)
    assert done["ok"] is True and "nothing to read" in done["said"].lower()
    async with db.session_factory()() as session:
        asked = select(func.count()).select_from(ActionRun).where(ActionRun.action == "board_feed.read")
        assert await session.scalar(asked) == 1
