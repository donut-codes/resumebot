"""The page program alone answers the reads the owner opens most: the board, the missions and one role. The engine is
stood in for by a recorder that does nothing, and the test fails if a read asks it for anything but a look, or takes
the writer's turn. It also pins that the mission and Find Gaps reads are the page's routes. Everything is made up."""

import pytest
from httpx import ASGITransport, AsyncClient

from app import db
from app.jobsearch.factory import build_app
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Opportunity
from app.jobsearch.work import engine_calls
from app.timeutil import owner_today
from tests.builders import counted, save_documents
from tests.split.test_page_reads_take_no_write_lock import turns_recorded
from tests.split.test_route_programs import _keys
from tests.support import OWNER, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

#: The reads the page serves.
FLIPPED = {
    "GET /api/v1/missions",
    "GET /api/v1/missions/{mission_id}",
    "GET /api/v1/find-gaps",
    "GET /api/v1/find-gaps/runs/{run_id}",
    "GET /api/v1/find-gaps/drafts/{draft_id}",
}


class Told:
    """Stands in for the engine (`engine_calls.Backend`): it keeps what it was told and does nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def knock(self, run_id: str, reason: str) -> bool:
        self.calls.append(("knock", run_id, reason))
        return True

    async def dispatch_now(self) -> int:
        self.calls.append(("dispatch_now",))
        return 0

    async def knocks(self, pending: list) -> None:
        self.calls.append(("knocks", list(pending)))

    async def send_wakes(self, wakes) -> list[str]:
        self.calls.append(("send_wakes",))
        return []

    def nudge(self) -> None:
        self.calls.append(("nudge",))

    def start(self, name: str, key) -> None:
        self.calls.append(("start", name))


def test_the_page_serves_the_mission_and_find_gaps_reads_and_the_engine_does_not():
    page, engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert FLIPPED <= page, sorted(FLIPPED - page)
    assert not FLIPPED & engine, sorted(FLIPPED & engine)
    assert "GET /api/v1/agent/ready" in page - engine


async def test_the_page_alone_answers_the_board_the_missions_and_a_role(js, monkeypatch):
    from app.jobsearch import page
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        made = await missions.ensure(session, OWNER)
        role = Opportunity(owner=OWNER, slug="globex", company="Globex", title="Programs Lead", fit=3, origin="scout",
                           match_key="globex|programs lead", status="ready_to_launch", found=owner_today(),
                           **counted())
        session.add(role)
        await session.flush()
        role_id = str(role.id)
        await session.commit()
    told = Told()
    before = engine_calls._backend
    engine_calls.install(told)
    try:
        with turns_recorded() as turns:
            async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as http:
                board = await http.get("/api/v1/board")
                listed = await http.get("/api/v1/missions")
                assert listed.status_code == 200, listed.text
                first = listed.json()["missions"][0]
                one = await http.get(f"/api/v1/missions/{first['id']}")
                shown = await http.get(f"/api/v1/roles/{role_id}")
    finally:
        engine_calls.install(before)
    assert [reply.status_code for reply in (board, listed, one, shown)] == [200, 200, 200, 200]
    assert "columns" in board.json()
    assert made > 0 and len(listed.json()["missions"]) == made
    assert one.json()["key"] == first["key"] and one.json()["runs"] == []
    assert shown.json()["role"]["title"] == "Programs Lead"
    assert not turns.waits and not turns.ends, "a read of the page took the writer's turn"
    # A nudge is a hint to look that needs no answer; anything else is work the page asked the engine for.
    assert [call for call in told.calls if call[0] != "nudge"] == []
