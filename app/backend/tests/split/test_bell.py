"""The page's side of the bell: `engine_calls.Remote` turns each call of the door into one datagram to the engine and
loads none of the engine's modules. `notice.ring_engine` is replaced by a recorder; the real send is `test_notice.py`'s.
Everything is made up."""

import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from app import notice
from app.jobsearch.work import engine_calls

BACKEND = Path(__file__).resolve().parents[2]

ENGINE_MODULES = (
    "app.jobsearch.work.wake",
    "app.jobsearch.work.dispatcher",
    "app.jobsearch.loops.schedule",
    "app.sites.wake_ops",
    "app.jobsearch.work.bell",
)


@pytest.fixture
def rung(monkeypatch):
    """What the page rang, as (kind, fields)."""
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(notice, "ring_engine", lambda kind, **fields: seen.append((kind, fields)))
    return seen


async def test_a_knock_is_one_bell_with_the_id_and_the_reason(rung):
    assert await engine_calls.Remote().knock("r1", "gaps_asked") is True
    assert rung == [("knock", {"id": "r1", "reason": "gaps_asked"})]


async def test_a_knock_for_a_uuid_sends_it_as_text(rung):
    run_id = uuid.UUID(int=5)
    await engine_calls.Remote().knock(run_id, "search_started")
    assert rung == [("knock", {"id": str(run_id), "reason": "search_started"})]


async def test_a_pass_of_the_dispatcher_is_one_bell_and_answers_zero(rung):
    assert await engine_calls.Remote().dispatch_now() == 0
    assert rung == [("dispatch", {})]


async def test_knocks_go_in_bells_of_at_most_eight_pairs(rung):
    pending = [(f"k{n}", "search_started") for n in range(10)]
    await engine_calls.Remote().knocks(pending)
    assert [kind for kind, _ in rung] == ["knocks", "knocks"]
    assert [len(fields["pairs"]) for _, fields in rung] == [8, 2]
    assert rung[0][1]["pairs"][0] == ["k0", "search_started"]


async def test_no_knocks_ring_nothing(rung):
    await engine_calls.Remote().knocks([])
    assert rung == []


async def test_wakes_send_each_project_once_and_skip_the_ones_with_no_wake(rung):
    wake = object()
    taken = await engine_calls.Remote().send_wakes([("p1", wake), ("p1", wake), ("p2", None), (uuid.UUID(int=3), wake)])
    assert taken == []
    assert rung == [("wakes", {"ids": ["p1", str(uuid.UUID(int=3))]})]


def test_a_nudge_is_a_look(rung):
    engine_calls.Remote().nudge()
    assert rung == [("look", {})]


def test_go_is_rung_with_no_key_and_boards_night_with_the_runs_id(rung):
    remote = engine_calls.Remote()
    run_id = uuid.UUID(int=9)
    remote.start("go", "tester")
    remote.start("boards_night", (run_id, "tester", 150, False))
    assert rung == [("start:go", {}), ("start:boards_night", {"id": str(run_id)})]


def test_a_start_with_no_row_to_find_it_by_is_refused_on_the_page(rung):
    with pytest.raises(LookupError):
        engine_calls.Remote().start("owner_action", ("tester", "x", {}, False, "j", "h"))
    assert rung == []


async def test_with_no_engine_listening_each_call_returns_in_under_fifty_milliseconds():
    remote = engine_calls.Remote()
    slowest = 0.0
    for call in (lambda: remote.knock("r1", "gaps_asked"), remote.dispatch_now,
                 lambda: remote.knocks([("k", "search_started")]), lambda: remote.send_wakes([("p", object())])):
        began = time.perf_counter()
        await call()
        slowest = max(slowest, time.perf_counter() - began)
    for call in (remote.nudge, lambda: remote.start("go", "tester")):
        began = time.perf_counter()
        call()
        slowest = max(slowest, time.perf_counter() - began)
    assert slowest < 0.05


def test_the_remote_door_loads_no_engine_module():
    code = "\n".join([
        "import asyncio, sys",
        "from app import notice",
        "from app.jobsearch.work import engine_calls",
        "notice.ring_engine = lambda kind, **fields: None",
        "remote = engine_calls.Remote()",
        "async def calls():",
        "    await remote.knock('r1', 'gaps_asked')",
        "    await remote.dispatch_now()",
        "    await remote.knocks([('k', 'search_started')])",
        "    await remote.send_wakes([('p1', object())])",
        "asyncio.run(calls())",
        "remote.nudge()",
        "remote.start('go', 'tester')",
        f"loaded = [m for m in {list(ENGINE_MODULES)!r} if m in sys.modules]",
        "assert not loaded, loaded",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]
