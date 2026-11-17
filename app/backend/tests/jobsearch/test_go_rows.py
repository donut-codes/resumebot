"""Launch in Browser's progress is its row (`js_go_runs`, `GoRun`): the page reads the row and nothing of the engine's
memory (`go_status.stored`), on a reader, so the page program can answer `GET /apply/go` (docs/PRD.md path 15.1). The
engine writes the row when what the owner sees changes (`apply_go.save`), pressing the button writes it before the run
begins, and a start closes a run the last stop cut short (`apply_go.settle_runs`). Everything is made up."""

import asyncio

from app import db
from app.jobsearch.browser import go as apply_go
from app.jobsearch.browser import go_checks, go_status
from app.jobsearch.loops import schedule
from app.jobsearch.loops.startup import run_startup_fixups
from app.jobsearch.models import GoRun
from tests.jobsearch.test_apply_go_tabs import _link, _set_packets, _until, _world
from tests.jobsearch.test_go_checks import _packet, role
from tests.split.test_page_reads_take_no_write_lock import turns_recorded
from tests.support import OWNER, fake_go_window, load_profile


async def _row(**state) -> None:
    """A run's row as an engine left it: a fresh status with `state` over it."""
    async with db.session_factory()() as session:
        session.add(GoRun(owner=OWNER, state={**go_status.fresh(), **state}))
        await session.commit()


async def _kept() -> dict:
    """The row's status as another program reads it (a reader's session); empty when there is no row."""
    async with db.read_session_factory()() as session:
        row = await session.get(GoRun, OWNER)
        return {} if row is None else row.state


async def _settle() -> None:
    """The start-up step, as the start runs it: in a writer's session, committed after it."""
    async with db.session_factory()() as session:
        await apply_go.settle_runs(session, OWNER)
        await session.commit()


# --- the page's read ------------------------------------------------------------------------------------------------


async def test_the_page_reads_the_row_and_nothing_of_the_engines_memory(js, monkeypatch):
    monkeypatch.setitem(apply_go._runs, OWNER, {**go_status.fresh(), "phase": "building", "total": 2})
    await apply_go.save(OWNER)
    async with db.session_factory()() as session:  # another program's write: the memory here knows nothing of it
        row = await session.get(GoRun, OWNER)
        assert row is not None
        row.state = {**row.state, "total": 41}
        await session.commit()
    read = await go_status.stored(OWNER)
    assert (read["phase"], read["total"], read["done"]) == ("building", 41, False)
    assert set(read) == set(go_status.fresh()), "the same parts as ever, and none of the run's private ones"


async def test_with_no_row_the_page_reads_idle(js):
    assert await go_status.stored(OWNER) == {**go_status.fresh(), "phase": "idle", "done": True}


async def test_reading_the_progress_takes_no_turn_of_the_writer(client):
    await _row(phase="done", done=True, total=1, built=1)
    with turns_recorded() as turns:
        answer = await client.get("/api/v1/apply/go")
    assert answer.status_code == 200 and answer.json()["phase"] == "done"
    assert not turns.waits and not turns.ends, "the page's read of Launch in Browser takes no turn"


# --- the engine's writes --------------------------------------------------------------------------------------------


async def test_a_save_of_nothing_new_takes_no_turn_and_a_change_made_in_place_is_written(js, monkeypatch):
    monkeypatch.setitem(apply_go._runs, OWNER, go_status.fresh())
    await apply_go.save(OWNER)
    with turns_recorded() as turns:
        await apply_go.save(OWNER)
    assert not turns.waits and not turns.ends, "nothing changed: no turn of the writer"
    apply_go._runs[OWNER]["opened"].append({"id": "1", "title": "Product Lead a", "company": "Firm a"})
    with turns_recorded() as turns:
        await apply_go.save(OWNER)
    assert turns.ends, "the list changed in place: it is written"
    assert [o["title"] for o in (await _kept())["opened"]] == ["Product Lead a"]


async def test_pressing_go_writes_the_row_before_the_run_begins(client, fake_starts):
    await _row(phase="done", done=True, total=1, built=1)  # the run before this one
    answer = await client.post("/api/v1/apply/go")
    assert answer.status_code == 200 and answer.json()["phase"] == "asked"
    assert fake_starts.starts == [("go", OWNER)], "the run is asked for by name, and nothing ran here"
    assert await _kept() == {**go_status.fresh(), "phase": "asked"}, (
        "the page's next look finds the new run, not the one before it"
    )
    assert (await client.get("/api/v1/apply/go")).json() == {**go_status.fresh(), "phase": "asked"}


# --- the start-up step ----------------------------------------------------------------------------------------------


async def test_an_unfinished_row_reads_as_it_is_until_a_start_closes_it_as_stopped(js):
    built = {"id": "1", "title": "Product Lead a", "company": "Firm a", "step": apply_go.STEPS["working"]}
    await _row(phase="building", total=3, built=1, current=built, building=[built], stalled="No writer yet.",
               notes=["A note the run left."])
    read = await go_status.stored(OWNER)
    assert read["phase"] == "building" and read["done"] is False and read["current"] == built, (
        "the page reads the row as it stands: only the engine's start closes it")
    await _settle()
    closed = await go_status.stored(OWNER)
    assert closed["phase"] == "stopped" and closed["done"] is True and closed["built"] == 1 and closed["total"] == 3
    assert closed["current"] is None and closed["building"] == [] and closed["stalled"] is None
    assert closed["notes"] == ["A note the run left.", apply_go.STOPPED]
    await _settle()
    assert await go_status.stored(OWNER) == closed, "a second start changes nothing"


async def test_a_start_leaves_a_finished_row_and_no_row_alone(js, fake_starts):
    await _settle()
    assert await _kept() == {}
    await _row(phase="done", done=True, total=1, built=1)
    before = await _kept()
    await _settle()
    assert await _kept() == before and fake_starts.starts == []


async def test_a_row_left_asked_is_started_once_by_a_start(js, fake_starts):
    await _row(phase="asked")
    await _settle()
    assert fake_starts.starts == [("go", OWNER)]
    assert await _kept() == go_status.fresh(), "the row says the run began: it is asked for no longer"
    assert apply_go.progress(OWNER) == go_status.fresh()
    await _settle()  # the engine stopped again before the run did anything
    assert fake_starts.starts == [("go", OWNER)], "started once"
    assert (await go_status.stored(OWNER))["phase"] == "stopped"


async def test_a_start_leaves_a_run_this_program_is_making_alone(js, monkeypatch):
    async def never(owner, **kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(apply_go, "go", never)
    await _row(phase="building")
    await apply_go.start(OWNER)  # the fixture around every test cancels it (`go_that_leaves_nothing_running`)
    assert apply_go.running(OWNER)
    await _settle()
    assert (await _kept())["phase"] == "building"


async def test_the_apps_start_closes_a_run_the_last_stop_cut_short(js, caplog):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    await _row(phase="opening", total=2, built=1)
    async with db.session_factory()() as session:
        await run_startup_fixups(session, OWNER)
        await session.commit()
    assert "start-up step" not in caplog.text, "no step failed"
    closed = await go_status.stored(OWNER)
    assert closed["phase"] == "stopped" and closed["done"] is True and closed["built"] == 1
    assert closed["notes"] == [apply_go.STOPPED]


# --- the row follows the run (the engine saves when what the owner sees changes) -------------------------------------


async def _row_says(**parts) -> bool:
    kept = await _kept()
    return all(kept.get(name) == value for name, value in parts.items())


async def _handed_over(n: int) -> bool:
    return len((await _kept()).get("opened", [])) >= n


async def test_the_row_says_a_tab_is_opening_while_a_built_packets_tab_loads(js, monkeypatch):
    opened: list[str] = []
    loading = asyncio.Event()
    window = fake_go_window(opened)

    class Slow:
        """Go's window, with a tab that does not open until the test says so."""

        def __init__(self) -> None:
            self.inner = window()

        async def open(self) -> None:
            await self.inner.open()

        async def tab(self, url: str, role_id=None) -> str:
            await loading.wait()
            return await self.inner.tab(url, role_id)

        async def read(self, target: str, url: str):
            return await self.inner.read(target, url)

        async def close_tab(self, target: str) -> None:
            await self.inner.close_tab(target)

        async def keep(self) -> None:
            await self.inner.keep()

    monkeypatch.setattr(apply_go, "go_window", Slow)
    await _world(role("a", fit=9))
    await _packet("a", "writing")
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        await _until(lambda: _row_says(phase="building", built=0), 5)
        await _set_packets("a", "ready")  # the writer sent it and the checks passed it
        await _until(lambda: _row_says(phase="opening", built=1), 5)
        assert opened == [], "the row said so before the tab opened"
        loading.set()
        done = await asyncio.wait_for(task, 10)
    finally:
        loading.set()
        if not task.done():
            task.cancel()
    assert done["phase"] == "done" and len(done["opened"]) == 1 and opened == [_link("a")]
    assert await _kept() == done, "the row ends as the run's own answer"


async def test_what_the_hand_over_opened_is_on_the_row_before_the_last_turn_ends(js, monkeypatch):
    sent: list[str] = []
    asked: list[str] = []
    last_turn = asyncio.Event()

    async def no_chrome() -> str:
        return "Chrome was not found"

    async def open_links(links, pause=None):
        sent.extend(links)

    async def turn(owner: str) -> None:
        asked.append(owner)
        if len(asked) == 2:  # the turn after the hand-over (the first is before the checks)
            await last_turn.wait()
        await schedule.pipeline_pass(owner)

    monkeypatch.setattr(go_checks, "start_browser", no_chrome)
    monkeypatch.setattr(apply_go, "open_links", open_links)
    monkeypatch.setattr(apply_go, "_turn", turn)
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    task = asyncio.create_task(apply_go.go(OWNER, poll=0, wait=0))
    try:
        await _until(lambda: _handed_over(1), 5)
        kept = await _kept()
        assert kept["done"] is False and kept["current"] is None and sent == [_link("a")]
        last_turn.set()
        done = await asyncio.wait_for(task, 10)
    finally:
        last_turn.set()
        if not task.done():
            task.cancel()
    assert done["done"] is True and await _kept() == done
