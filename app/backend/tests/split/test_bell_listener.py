"""The engine's listener (`bell.start`): the bells the page rings reach `bell.handle`, one task each, and the engine's
start-up starts it once and stops it. Made-up ids only."""

import asyncio
import socket

import pytest

from app import db, notice, program, turn_log
from app.jobsearch import main
from app.jobsearch.work import bell, wake
from app.settings import JobSearchSettings, settings


@pytest.fixture(autouse=True)
def _ports_back_to_the_command_line():
    yield
    notice.use_ports(None)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def test_a_bell_the_page_rings_reaches_handle_through_a_real_socket(monkeypatch):
    # The one real socket of this lane: the wire's round trip with the engine's own mapping of a datagram to a call.
    notice.use_ports(_free_port(), _free_port())
    heard = asyncio.Event()
    calls: list[tuple[str, str]] = []

    async def knock(run_id, reason=wake.SEARCH_STARTED):
        calls.append((run_id, reason))
        heard.set()
        return True

    monkeypatch.setattr(wake, "knock", knock)
    listening = await bell.start()
    assert listening is not None
    try:
        notice.ring_engine("knock", id="r1", reason="gaps_asked")
        await asyncio.wait_for(heard.wait(), 2)
        assert calls == [("r1", "gaps_asked")]
    finally:
        await listening.close()


async def test_start_is_none_when_the_signal_is_off(monkeypatch):
    monkeypatch.setattr(notice.sys, "argv", ["pytest"])
    notice.use_ports(None)
    assert await bell.start() is None


async def test_a_datagram_that_is_not_a_bell_runs_nothing(monkeypatch):
    notice.use_ports(_free_port(), _free_port())
    kept: dict = {}

    class Recorder:
        def __init__(self, port, on_message):
            kept["on_message"] = on_message

        async def start(self):
            pass

        def stop(self):
            pass

    handled: list = []

    async def handle(name, fields=None):
        handled.append(name)

    monkeypatch.setattr(notice, "Receiver", Recorder)
    monkeypatch.setattr(bell, "handle", handle)
    listening = await bell.start()
    assert listening is not None
    try:
        page_bound = notice.decode(notice.encode("owner-1", [("role", "1")], boot="b", n=1))
        assert page_bound is not None and page_bound.bell is None
        kept["on_message"](page_bound)
        kept["on_message"]("garbage")
        kept["on_message"](None)
        await asyncio.sleep(0)
        assert handled == [] and not bell._tasks
    finally:
        await listening.close()


async def test_closing_the_listener_cancels_a_bell_still_running(monkeypatch):
    notice.use_ports(_free_port(), _free_port())
    started = asyncio.Event()
    forever = asyncio.Event()
    outcome: list[str] = []

    async def handle(name, fields=None):
        started.set()
        try:
            await forever.wait()
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    monkeypatch.setattr(bell, "handle", handle)
    listening = await bell.start()
    assert listening is not None
    bell.on_bell("look", {})
    await asyncio.wait_for(started.wait(), 2)
    await listening.close()
    assert bell._tasks == set()
    assert outcome == ["cancelled"]


@pytest.fixture
def _heavy_parts_replaced(monkeypatch, js):
    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(main.startup, "tune_gc_threshold", lambda: None)
    monkeypatch.setattr(db, "configure", lambda *a, **k: None)
    monkeypatch.setattr(db, "init_schema", nothing)
    monkeypatch.setattr(turn_log, "install_from_environment", lambda: None)
    monkeypatch.setattr(main.startup, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(main.startup, "run_startup_fixups", nothing)
    monkeypatch.setattr(main.inbox_asks, "install", lambda: None)
    monkeypatch.setattr(main, "session_managers", lambda role: [])
    monkeypatch.setattr(settings, "run_schedule", False)
    # a held engine now starts the ask-only queue; this test starts none
    monkeypatch.setattr(main.queue_loop, "run", nothing)
    monkeypatch.setattr(JobSearchSettings, "check_timezone", lambda self: None)
    monkeypatch.setattr(program, "PROGRAM", "all")


async def test_the_engine_starts_the_listener_once_and_stops_it_and_the_seam_does_not(
        monkeypatch, _heavy_parts_replaced):
    events: list[str] = []

    class Standin:
        async def close(self):
            events.append("close")

    async def start():
        events.append("start")
        return Standin()

    monkeypatch.setattr(bell, "start", start)
    async with main._running("engine"):
        assert events == ["start"]
    assert events == ["start", "close"]

    events.clear()
    monkeypatch.setattr(program, "PROGRAM", "all")
    async with main._running("all"):
        pass
    assert events == []
