"""A stop of the app ends the page's live-updates streams, so the server can finish by itself.

The server waits for every open response before it shuts down, and a live-updates stream ends only when its page goes
away: with a page left open, every stop of the local install waited the launcher's whole 60 seconds and was ended
(`stack.py` `Child.finish`). The stop request is heard as it arrives (`app.stopping`), and the streams end at once
(`live.Broker.close`)."""

import asyncio
import contextlib
import signal

import pytest

from app import db, stopping
from app.jobsearch import main
from app.jobsearch.pipeline import live
from app.settings import settings

OWNER = "owner"
#: A stream that asks for tasks only reads no counts for its hello, so these tests need no database.
TASKS = frozenset({"task"})
#: What the launcher's stop arrives as: Ctrl+Break on Windows, SIGTERM elsewhere (`stack.py` `Child.finish`).
STOP = getattr(signal, "SIGBREAK", signal.SIGTERM)
#: Far longer than a stream takes to end once told, far shorter than its 15 seconds between pings.
SOON = 2.0


async def _still_there() -> bool:
    return False


@pytest.fixture
def broker(monkeypatch):
    """A broker of this test's own, so a close here is not the next test's."""
    fresh = live.Broker()
    monkeypatch.setattr(live, "broker", fresh)
    return fresh


@pytest.fixture
def server_heard():
    """Stands in for the server's own listener: the stop requests it was handed, and the listeners put back after."""
    got: list[int] = []
    before = {sig: signal.signal(sig, lambda sig, _frame: got.append(sig)) for sig in stopping.SIGNALS}
    yield got
    for sig, handler in before.items():
        signal.signal(sig, handler)


async def test_an_open_stream_ends_when_the_app_is_stopping_though_its_page_is_still_there(broker):
    stream = live.stream(OWNER, _still_there, topics=TASKS)
    assert (await anext(stream)).startswith("id: 0\nevent: hello\n")
    waiting = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)  # the stream is now waiting for a message
    broker.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, SOON)
    assert not broker.listening(OWNER), "and it is no longer counted as a page that listens"


async def test_a_stream_that_fell_behind_ends_too(broker):
    stream = live.stream(OWNER, _still_there, topics=TASKS)
    await anext(stream)
    for number in range(live.MOST_HELD):
        broker.publish(OWNER, "task", {"id": number})
    broker.close()

    async def rest() -> int:
        return len([frame async for frame in stream])

    assert await asyncio.wait_for(rest(), SOON) < live.MOST_HELD, "it ends without sending the page all it held"


async def test_a_stream_opened_while_the_app_is_stopping_ends_after_its_hello(broker):
    broker.close()
    stream = live.stream(OWNER, _still_there, topics=TASKS)
    assert (await anext(stream)).startswith("id: 0\nevent: hello\n")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), SOON)


async def test_a_stop_request_is_heard_first_and_still_reaches_the_server(server_heard):
    told: list[str] = []
    with stopping.heard(lambda: told.append("stopping")):
        signal.raise_signal(STOP)
        await asyncio.sleep(0)
        assert told == ["stopping"]
        assert server_heard == [STOP], "the server's own listener got the same request"
    signal.raise_signal(STOP)
    await asyncio.sleep(0)
    assert told == ["stopping"], "after it, nothing of this is left listening"
    assert server_heard == [STOP, STOP]


async def test_where_nothing_listens_for_a_stop_nothing_is_put_in_its_way():
    before = signal.signal(STOP, signal.SIG_DFL)
    try:
        with stopping.heard(lambda: None):
            assert signal.getsignal(STOP) is signal.SIG_DFL, "a stop request still ends the process as it did"
    finally:
        signal.signal(STOP, before)


async def test_a_stop_request_to_the_running_app_ends_its_open_live_streams(broker, server_heard, tmp_path,
                                                                           monkeypatch):
    @contextlib.asynccontextmanager
    async def started(_role):  # the start-up and the loops are not what this is about
        yield

    monkeypatch.setattr(main, "_running", started)
    monkeypatch.setattr(settings, "database_url", db.sqlite_url(tmp_path / "app.db"))
    async with main.lifespan(main.app):
        stream = live.stream(OWNER, _still_there, topics=TASKS)
        await anext(stream)
        waiting = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        signal.raise_signal(STOP)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(waiting, SOON)
    assert server_heard == [STOP]
