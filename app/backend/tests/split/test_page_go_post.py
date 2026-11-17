"""The page program answers `POST /apply/go` (Launch in Browser): it writes the `GoRun` row in phase `asked`, rings the
engine and answers the row, with no engine running; the engine begins the run on the bell (`start:go`). Everything is
made up."""

import pytest
from httpx import ASGITransport, AsyncClient

from app import db
from app.jobsearch import engine, page
from app.jobsearch.browser import go as apply_go
from app.jobsearch.browser import go_status
from app.jobsearch.models import GoRun
from app.jobsearch.work import bell
from app.settings import settings
from tests.split.test_page_reads_take_no_write_lock import turns_recorded
from tests.split.test_role_reads_on_page import _keys
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

GO = "/api/v1/apply/go"
ASKED = {**go_status.fresh(), "phase": "asked"}


@pytest.fixture(autouse=True)
def the_owner(monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)


@pytest.fixture
async def page_client(js):
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://localhost:8430") as client:
        yield client


async def _row(**state) -> None:
    async with db.session_factory()() as session:
        session.add(GoRun(owner=OWNER, state={**go_status.fresh(), **state}))
        await session.commit()


async def _kept() -> dict | None:
    async with db.read_session_factory()() as session:
        row = await session.get(GoRun, OWNER)
        return None if row is None else row.state


def test_the_page_serves_the_press_and_the_engine_does_not():
    assert f"POST {GO}" in _keys(page.app)
    assert f"POST {GO}" not in _keys(engine.app)


async def test_with_no_engine_the_press_leaves_an_asked_row_and_answers_it(page_client, fake_starts):
    answer = await page_client.post(GO)
    assert answer.status_code == 200 and answer.json() == ASKED
    assert await _kept() == ASKED
    assert fake_starts.starts == [("go", OWNER)], "one bell, after the row; nothing ran here"
    assert (await page_client.get(GO)).json() == ASKED


async def test_a_press_while_a_run_goes_answers_it_rings_nothing_and_takes_no_turn(page_client, fake_starts):
    await _row(phase="building", total=3, built=1)
    with turns_recorded() as turns:
        answer = await page_client.post(GO)
    assert answer.status_code == 200 and answer.json()["phase"] == "building" and answer.json()["total"] == 3
    assert fake_starts.starts == []
    assert not turns.waits and not turns.ends


async def test_a_press_on_a_row_still_asked_rings_again_and_changes_nothing(page_client, fake_starts):
    await _row(phase="asked")
    await page_client.post(GO)
    assert fake_starts.starts == [("go", OWNER)]
    assert await _kept() == ASKED


async def test_a_request_from_another_machine_is_refused_and_writes_nothing(page_client, fake_starts, monkeypatch):
    monkeypatch.setattr(go_status, "THIS_MACHINE", ("10.0.0.9",))
    answer = await page_client.post(GO)
    assert answer.status_code == 403
    assert await _kept() is None and fake_starts.starts == []


async def test_the_engine_begins_the_run_the_press_left_once(page_client, fake_starts, monkeypatch):
    monkeypatch.setattr(apply_go, "_runs", {})  # settle_runs keeps a copy of the run; none stays for a later test
    await page_client.post(GO)
    await bell.handle("start:go", {})
    await bell.handle("start:go", {})  # the row says checking now: a second bell starts nothing
    assert fake_starts.starts == [("go", OWNER), ("go", OWNER)], "the page's bell, then the engine's one start"
    assert (await _kept())["phase"] == "checking"
