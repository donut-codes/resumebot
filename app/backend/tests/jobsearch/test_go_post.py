"""Pressing Go writes the owner's `GoRun` row in phase `asked` (`go_status.ask`) and the engine begins it
(`apply_go._spawn`, `settle_runs`). Everything is made up."""

import asyncio

import pytest

from app import db
from app.jobsearch.browser import go as apply_go
from app.jobsearch.browser import go_status
from app.jobsearch.models import GoRun
from tests.split.test_page_reads_take_no_write_lock import turns_recorded
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

ASKED = {**go_status.fresh(), "phase": "asked"}


async def _row(**state) -> None:
    async with db.session_factory()() as session:
        session.add(GoRun(owner=OWNER, state={**go_status.fresh(), **state}))
        await session.commit()


async def _kept() -> dict | None:
    async with db.read_session_factory()() as session:
        row = await session.get(GoRun, OWNER)
        return None if row is None else row.state


async def _ask() -> tuple[dict, bool]:
    async with db.session_factory()() as session:
        answer = await go_status.ask(session, OWNER)
        await session.commit()
    return answer


async def test_with_no_row_a_press_writes_an_asked_row_and_asks_for_the_engine(js):
    assert await _ask() == (ASKED, True)
    assert await _kept() == ASKED


async def test_a_press_after_a_finished_run_replaces_its_row_with_an_asked_one(js):
    await _row(phase="done", done=True, total=2, built=2, notes=["Two roles opened."])
    assert await _ask() == (ASKED, True)
    assert await _kept() == ASKED


async def test_a_press_while_a_run_goes_answers_it_writes_nothing_and_takes_no_turn(js):
    await _row(phase="building", total=3, built=1)
    with turns_recorded() as turns:
        answer, ring = await _ask()
    assert answer["phase"] == "building" and answer["total"] == 3 and ring is False
    assert not turns.waits and not turns.ends, "a run that goes is answered from a reader"
    kept = await _kept()
    assert kept is not None and kept["phase"] == "building"


async def test_a_press_on_a_row_still_asked_changes_nothing_and_asks_for_the_engine_again(js):
    await _row(phase="asked")
    assert await _ask() == (ASKED, True)
    assert await _kept() == ASKED


async def test_a_second_spawn_while_go_runs_starts_no_second_task(monkeypatch):
    gate = asyncio.Event()
    calls: list[str] = []

    async def fake_go(owner, **kw):
        calls.append(owner)
        await gate.wait()

    monkeypatch.setattr(apply_go, "go", fake_go)
    apply_go._spawn(OWNER)
    apply_go._spawn(OWNER)
    await asyncio.sleep(0)
    assert calls == [OWNER]
    gate.set()
    await asyncio.wait_for(apply_go._tasks[OWNER], 5)
