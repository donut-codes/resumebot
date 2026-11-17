"""Launch in Browser's order is a playbook (`playbooks/jobsearch/go.bpmn`, docs/PRD.md path 18): the file's flow, its
two conditions and its data model; its steps register without loading the runner; a step that fails still hands over
what is open, and a failure before anything is open raises its own error and does no hand-over. Everything here is made
up; the fake window and the made-up roles are those of `test_apply_go_tabs.py`."""

import asyncio
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from process_engine.model import GATEWAY, SERVICE
from process_engine.models import CANCELLED, COMPLETED, ProcHistory, ProcInstance
from sqlalchemy import select

from app import db
from app.config import settings
from app.jobsearch import process, stages
from app.jobsearch.browser import go as apply_go
from app.jobsearch.browser import go_checks, go_status
from app.jobsearch.browser.code import Page
from app.jobsearch.models import GoRun
from tests.builders import counted
from tests.jobsearch.test_apply_go_tabs import (
    CLOSED_PAGE,
    _go,
    _link,
    _one_place,
    _packet,
    _roles,
    _world,
    role,
)
from tests.support import OWNER, fake_go_window

BACKEND = Path(__file__).resolve().parents[2]
FILE = Path(settings.playbooks_path) / "jobsearch" / "go.bpmn"

ORDER = ["Begin", "Turn_first", "Check_roles", "Open_built", "Build_rest", "Refill", "Hand_over", "Finish"]


def test_the_file_loads_and_its_two_conditions_read_the_files_numbers():
    go = process.build_library().get(process.GO)
    steps = [e for e in go.elements.values() if e.kind == SERVICE]
    assert [(e.id, e.action) for e in steps] == [
        ("Begin", "jobs.go_begin"), ("Turn_first", "jobs.go_turn"), ("Check_roles", "jobs.go_check"),
        ("Open_built", "jobs.go_open"), ("Build_rest", "jobs.go_build"), ("Refill", "jobs.go_refill"),
        ("Hand_over", "jobs.go_hand_over"), ("Finish", "jobs.go_finish")]
    assert [e.id for e in go.elements.values() if e.kind == GATEWAY] == ["Gw_dropped", "Gw_more"]
    assert {f.id: (f.source, f.target, f.condition) for f in go.flows.values() if f.condition} == {
        "F_dropped": ("Gw_dropped", "Refill", "built.fill = true"),
        "F_more": ("Gw_more", "Open_built", "refill.again = true and refill.round < 20")}
    ways = {(f.source, f.target) for f in go.flows.values()}
    assert {("Gw_dropped", "Hand_over"), ("Gw_more", "Hand_over"), ("Build_rest", "Gw_dropped"),
            ("Refill", "Gw_more"), ("Hand_over", "Finish")} <= ways
    assert [f.id for f in go.flows.values() if f.is_default] == ["F_not_dropped", "F_done"]
    rounds = int(re.search(r"refill\.round &lt; (\d+)", FILE.read_text(encoding="utf-8")).group(1))
    assert apply_go.MAX_ROUNDS == rounds == 20, "the code's number is the shipped default of the file's"


def test_the_data_paths_are_declared():
    paths = re.findall(r'porter:field path="([^"]+)"', FILE.read_text(encoding="utf-8"))
    assert paths == ["owner", "poll", "wait", "built", "built.fill", "refill", "refill.again", "refill.round"]
    assert "checked.roles" not in paths, "no gateway reads it"


def test_the_actions_register_without_loading_the_runner():
    code = "\n".join([
        "import sys",
        "from app.jobsearch import process",
        "process.load_actions()",
        "from process_engine.registry import actions",
        "assert 'jobs.go_begin' in actions.names() and 'jobs.go_finish' in actions.names()",
        "loaded = [m for m in ('app.jobsearch.browser.go', 'app.jobsearch.browser.code') if m in sys.modules]",
        "assert not loaded, loaded",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]


def _spy_on_hand_over(monkeypatch) -> dict:
    """How many turns of the pipeline and how many hand-overs a run made."""
    seen = {"turns": 0, "finished": 0}
    finish = apply_go._Opener.finish

    async def turn(owner: str) -> None:
        seen["turns"] += 1

    async def counted_finish(self) -> None:
        seen["finished"] += 1
        await finish(self)

    monkeypatch.setattr(apply_go, "_turn", turn)
    monkeypatch.setattr(apply_go._Opener, "finish", counted_finish)
    return seen


async def test_a_failing_step_still_hands_over_and_the_run_says_it_stopped(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    seen = _spy_on_hand_over(monkeypatch)

    async def boom(owner, data):
        raise RuntimeError("the writer's door fell off")

    monkeypatch.setattr(apply_go, "step_build", boom)
    await apply_go.start(OWNER)
    await asyncio.wait_for(apply_go._tasks[OWNER], 10)
    said = apply_go.progress(OWNER)
    assert said["done"] is True and "Launch in Browser stopped: RuntimeError." in said["notes"]
    assert seen == {"turns": 2, "finished": 1}, "the first turn and the hand-over's: it ran once"
    assert opened == [_link("a")], "the tab opened before the step failed is handed over, not lost"
    assert (await _roles())["a"].go_opened_at is not None


async def test_a_failure_before_the_opener_ends_with_the_real_error_and_no_turn(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    await _world(role("a", fit=9, **counted()))
    seen = _spy_on_hand_over(monkeypatch)

    async def prepare(session, owner, progress=None):
        raise ValueError("the checks could not read")

    monkeypatch.setattr(go_checks, "prepare", prepare)
    with pytest.raises(ValueError, match="the checks could not read"):
        await _go()
    assert seen == {"turns": 1, "finished": 0}, "only the first turn: no hand-over, no closing turn"


async def test_a_run_that_goes_well_hands_over_once(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    seen = _spy_on_hand_over(monkeypatch)
    done = await _go()
    assert opened == [_link("a")] and done["done"] is True
    assert seen == {"turns": 2, "finished": 1}



# --- the run is the engine's: the order is the file's -----------------------------------------------------------------


async def _instances() -> list[ProcInstance]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(ProcInstance).where(ProcInstance.process_key == process.GO)
                                           .order_by(ProcInstance.created_at))).all())


async def _steps_of(instance: ProcInstance) -> list[str]:
    """The steps the run completed, in order (the engine's history: a step's `complete` row)."""
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(ProcHistory).where(ProcHistory.instance_id == instance.id)
                                      .order_by(ProcHistory.seq))).all()
    return [r.from_element for r in rows if r.kind == "complete"]


async def test_go_runs_through_the_engine_in_the_files_order(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    done = await _go()
    assert opened == [_link("a")] and done["done"] is True and done["phase"] == "done"
    (instance,) = await _instances()
    assert instance.status == COMPLETED and instance.subject == f"jobsearch-go:{OWNER}"
    assert await _steps_of(instance) == ["Begin", "Turn_first", "Check_roles", "Open_built", "Build_rest", "Hand_over",
                                         "Finish"], "a quiet run: nothing dropped out, so no refill lap"


async def _closed_and_pulled_forward(monkeypatch) -> list[str]:
    """Role a's posting is closed, so c moves up from Second Impression into its one place."""
    opened: list[str] = []
    pages = {_link("a"): Page(url=_link("a"), status=200, html=CLOSED_PAGE)}
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened, pages))
    await _world(role("a", fit=9, **counted()), role("c", fit=2, status=stages.SECOND_IMPRESSION, **counted()))
    await _one_place()
    await _packet("a", "ready")
    await _packet("c", "ready")
    return opened


LAP = ["Open_built", "Build_rest"]


async def test_a_dropped_role_takes_the_refill_lap_and_a_quiet_round_does_not(js, monkeypatch):
    opened = await _closed_and_pulled_forward(monkeypatch)
    done = await _go()
    assert opened == [_link("a"), _link("c")] and [o["title"] for o in done["opened"]] == ["Product Lead c"]
    (first,) = await _instances()
    assert await _steps_of(first) == ["Begin", "Turn_first", "Check_roles", *LAP, "Refill", *LAP, "Hand_over",
                                      "Finish"], "a dropped out while the writers worked: refilled once, then quiet"
    await _go()  # nothing drops out this time (a stays closed, c is open): no refill
    _, second = await _instances()
    assert "Refill" not in await _steps_of(second)


async def test_a_role_the_checks_skipped_alone_takes_the_first_refill_lap(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9, apply_url="not a web address", **counted()),
                 role("c", fit=2, status=stages.SECOND_IMPRESSION, **counted()))
    await _one_place()
    await _packet("a", "ready")
    await _packet("c", "ready")
    done = await _go()
    assert opened == [_link("c")], "nothing dropped out while building: only the checks' own skip made room"
    assert "Pulled forward: Product Lead c at Firm c, in place of Product Lead a at Firm a." in done["notes"]
    (instance,) = await _instances()
    assert (await _steps_of(instance))[3:8] == ["Open_built", "Build_rest", "Refill", "Open_built", "Build_rest"]


async def test_the_twentieth_lap_refills_and_builds_no_twenty_first(js, monkeypatch):
    """Every lap finds a role dropped out and a role to pull forward (made-up: the checks and the sort are replaced
    after the first look, and the building step answers that a role dropped out)."""
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    await _world(role("a", fit=9, **counted()))
    await _packet("a", "ready")
    laps = {"open": 0, "build": 0, "refill": 0}
    pulled = SimpleNamespace(skipped=[], notes=[], ready=[])
    real_prepare, real_sort = go_checks.prepare, apply_go._sort
    real_open, real_refill = apply_go.step_open, apply_go.step_refill
    looks = {"n": 0}

    async def prepare(session, owner, progress=None):
        looks["n"] += 1
        return await real_prepare(session, owner, progress=progress) if looks["n"] == 1 else pulled

    async def sort(state, checked, handled):
        return ([], [uuid.uuid4()]) if checked is pulled else await real_sort(state, checked, handled)

    async def build(owner, data):
        laps["build"] += 1
        return {"fill": True}

    async def lap_open(owner, data):
        laps["open"] += 1
        return await real_open(owner, data)

    async def lap_refill(owner, data):
        laps["refill"] += 1
        return await real_refill(owner, data)

    monkeypatch.setattr(go_checks, "prepare", prepare)
    monkeypatch.setattr(apply_go, "_sort", sort)
    monkeypatch.setattr(apply_go, "step_build", build)
    monkeypatch.setattr(apply_go, "step_open", lap_open)
    monkeypatch.setattr(apply_go, "step_refill", lap_refill)
    done = await _go()
    assert laps == {"open": 20, "build": 20, "refill": 20}, "the 20th lap still refills; no 21st follows"
    assert done["total"] == 1 + 20, "the 20th refill still added to the total"
    (instance,) = await _instances()
    steps = await _steps_of(instance)
    assert steps[-3:] == ["Refill", "Hand_over", "Finish"] and steps.count("Refill") == 20


async def test_the_order_comes_from_the_file_not_the_code(js, monkeypatch):
    """A copy of go.bpmn whose first gateway never takes its condition: a role that would have been pulled forward is
    not, though the code's own steps are unchanged."""
    library = process.ProcessLibrary()
    for path in sorted(process.process_dir().glob("*.bpmn")):
        text = path.read_text(encoding="utf-8")
        if path.name == "go.bpmn":
            text = text.replace("built.fill = true<", "built.fill = true and built.fill = false<")
            assert "built.fill = false" in text
        library.load(text, catalog=process.catalog(), registry=process.registry)
    library.validate()
    engine = process.AppEngine(library, process.registry, catalog=process.catalog(),
                               deferred=frozenset(process.registry.names()))
    monkeypatch.setattr(process, "stepped_engine", lambda: engine)
    opened = await _closed_and_pulled_forward(monkeypatch)
    done = await _go()
    assert opened == [_link("a")] and done["opened"] == [], "c was not pulled forward: the file said no refill"
    (instance,) = await _instances()
    assert "Refill" not in await _steps_of(instance)


# --- a stop leaves a run's process open: the next start cancels it --------------------------------------------------


async def _leave_open() -> uuid.UUID:
    """A run's process begun and left at its first step, as a stop of the app leaves one."""
    async with db.session_factory()() as session:
        instance = await process.stepped_engine().start(session, process.GO, {"owner": OWNER, "poll": 0, "wait": 0},
                                                        subject=f"jobsearch-go:{OWNER}")
        await session.commit()
        return instance.id


async def _settle() -> None:
    async with db.session_factory()() as session:
        await apply_go.settle_runs(session, OWNER)
        await session.commit()


async def _status(instance_id: uuid.UUID) -> str:
    async with db.session_factory()() as session:
        found = await session.get(ProcInstance, instance_id)
        assert found is not None
        return found.status


async def _keep_row(**state) -> None:
    async with db.session_factory()() as session:
        session.add(GoRun(owner=OWNER, state={**go_status.fresh(), **state}, updated_at=datetime.now(UTC)))
        await session.commit()


async def test_settle_runs_cancels_an_instance_the_stop_left_open(js):
    instance_id = await _leave_open()
    await _keep_row(phase="building", done=False)
    await _settle()
    assert await _status(instance_id) == CANCELLED
    kept = await go_status.stored(OWNER)
    assert kept["phase"] == "stopped" and kept["done"] is True


async def test_settle_runs_cancels_an_open_instance_behind_a_done_row(js):
    instance_id = await _leave_open()
    await _keep_row(phase="done", done=True)  # Finish saved `done`, and the stop came before its step was complete
    await _settle()
    assert await _status(instance_id) == CANCELLED
    assert (await go_status.stored(OWNER))["phase"] == "done", "the finished run's row is left as it was"


async def test_settle_runs_touches_nothing_while_this_programs_run_is_going(js, monkeypatch):
    instance_id = await _leave_open()
    await _keep_row(phase="building", done=False)
    going = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setitem(apply_go._tasks, OWNER, going)
    try:
        await _settle()
    finally:
        going.cancel()
        await asyncio.gather(going, return_exceptions=True)
    assert await _status(instance_id) != CANCELLED
    assert (await go_status.stored(OWNER))["phase"] == "building"
