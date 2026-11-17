"""The page's live hub (`live_hub.Hub`): grouping, dropping, waking waiters, and the coalesced `resync`. The datagrams
are not in play here (test_live_signal.py has them); the sink is a recorder. Everything is made up."""

import asyncio
import threading

import pytest

from app.jobsearch.pipeline import live, live_hub, live_sink
from tests.support import OWNER

OTHER = "someone-else"


@pytest.fixture(autouse=True)
def own_broker_and_no_wait(monkeypatch):
    """A broker of this test's own (a stream left open is not the next test's), and no wait between an arrival and its
    send, so the window is the stretch of one loop turn."""
    monkeypatch.setattr(live, "broker", live.Broker())
    monkeypatch.setattr(live, "COUNTS_AFTER_SECONDS", 0)


class Seen:
    """What the sink was asked to announce, and which waiters were woken."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list]] = []
        self.woken: list[tuple[str, list[str]]] = []

    def announce(self, owner: str, items: list) -> None:
        self.sent.append((owner, list(items)))

    def notify(self, kind: str, ids: list[str]) -> None:
        self.woken.append((kind, list(ids)))


@pytest.fixture
def seen():
    return Seen()


@pytest.fixture
def hub(seen):
    return live_hub.Hub(seen.notify, seen)


def _kinds(sub: live.Subscriber) -> list[str]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait()[1])
    return out


def _item(kind: str, ident: str, *wire: str) -> tuple[str, str, list[str]]:
    """What a datagram carries for one item: kind, id, text tags."""
    return (kind, ident, list(wire))


def _sent(kind: str, ident: str, *wire: str) -> tuple[str, str, object]:
    """The same item as the sink is handed it."""
    return (kind, ident, live_sink.item_tags(list(wire)))


async def test_arrivals_of_one_kind_in_one_window_are_sent_once_and_equal_ones_are_one(hub, seen):
    live.broker.subscribe(OWNER)
    hub.arrive(OWNER, [_item("role", "1"), _item("role", "2", "was:applied"), _item("role", "1")])
    hub.arrive(OWNER, [_item("role", "3")])
    await hub.settle()
    assert seen.sent == [(OWNER, [_sent("role", "1"), _sent("role", "2", "was:applied"), _sent("role", "3")])]


async def test_five_hundred_arrivals_of_one_role_are_one_item(hub, seen):
    live.broker.subscribe(OWNER)
    for _ in range(500):
        hub.arrive(OWNER, [_item("role", "1")])
    await hub.settle()
    assert seen.sent == [(OWNER, [_sent("role", "1")])]


async def test_an_owner_with_no_stream_costs_nothing_but_the_waiters_hear(hub, seen):
    hub.arrive(OWNER, [_item("role", "1")])
    assert not hub.busy()
    await hub.settle()
    assert seen.sent == []
    assert seen.woken == [("role", ["1"])]


async def test_a_stream_that_did_not_ask_for_the_kind_is_not_sent_it(hub, seen):
    live.broker.subscribe(OWNER, frozenset({"task"}))
    hub.arrive(OWNER, [_item("role", "1")])
    await hub.settle()
    assert seen.sent == []


async def test_a_stream_that_asked_for_the_counts_only_is_sent_a_role(hub, seen):
    live.broker.subscribe(OWNER, frozenset({"counts"}))
    hub.arrive(OWNER, [_item("role", "1")])
    await hub.settle()
    assert seen.sent == [(OWNER, [_sent("role", "1")])]


async def test_every_arrival_wakes_the_waiters_and_a_kind_for_waiters_alone_is_not_sent(hub, seen):
    live.broker.subscribe(OWNER)
    hub.arrive(OWNER, [_item("ask", "x"), _item("action_run", "y")])
    assert not hub.busy()
    assert seen.woken == [("ask", ["x"]), ("action_run", ["y"])]
    assert seen.sent == []


async def test_a_window_of_more_than_the_most_is_one_resync_and_no_message_for_each(hub, seen):
    sub = live.broker.subscribe(OWNER)
    hub.arrive(OWNER, [_item("role", str(n)) for n in range(500)])
    await hub.settle()
    assert seen.sent == []
    assert _kinds(sub) == ["resync"]


async def test_a_lost_track_asked_five_hundred_times_is_one_resync_for_each_open_stream(hub, seen):
    mine, theirs = live.broker.subscribe(OWNER), live.broker.subscribe(OTHER)
    for _ in range(500):
        hub.lost_track()
    await hub.settle()
    assert _kinds(mine) == ["resync"] and _kinds(theirs) == ["resync"]


async def test_a_resync_item_from_the_wire_reaches_only_the_owners_streams_and_once(hub, seen):
    mine, theirs = live.broker.subscribe(OWNER), live.broker.subscribe(OTHER)
    for _ in range(100):
        hub.arrive(OWNER, [_item("resync", "")])
    await hub.settle()
    assert _kinds(mine) == ["resync"] and _kinds(theirs) == []
    assert seen.woken == [], "a resync wakes no waiter"


async def test_a_sink_that_fails_is_logged_and_the_next_window_goes(seen, caplog):
    tried: list[list] = []

    class Flaky:
        def announce(self, owner: str, items: list) -> None:
            tried.append(items)
            if len(tried) == 1:
                raise RuntimeError("boom")

    hub = live_hub.Hub(seen.notify, Flaky())  # pyright: ignore[reportArgumentType]
    live.broker.subscribe(OWNER)
    hub.arrive(OWNER, [_item("role", "1")])
    await hub.settle()
    hub.arrive(OWNER, [_item("role", "2")])
    await hub.settle()
    assert [items[0][1] for items in tried] == ["1", "2"]
    assert "were not sent" in caplog.text


async def test_stop_drops_what_waits(hub, seen, monkeypatch):
    monkeypatch.setattr(live, "COUNTS_AFTER_SECONDS", 3600)
    sub = live.broker.subscribe(OWNER)
    hub.arrive(OWNER, [_item("role", "1")])
    hub.lost_track()
    assert hub.busy()
    await hub.stop()
    assert not hub.busy()
    assert seen.sent == [] and _kinds(sub) == []


def test_settle_returns_when_every_task_is_done_before_its_discard_has_run(seen):
    # Found when the 500-commit test hung: a task that is done but still in the set (its done-callback has not run yet)
    # made settle gather without yielding, and loop forever. Run on a loop of its own in a daemon thread, so a spin is
    # a red result and not a hung suite.
    finished = threading.Event()

    async def scenario() -> None:
        hub = live_hub.Hub(seen.notify, seen)

        async def nothing() -> None:
            return None

        task = asyncio.get_running_loop().create_task(nothing())
        await task
        hub._tasks.add(task)  # done, and its discard has not run
        await hub.settle()

    def run() -> None:
        asyncio.run(scenario())
        finished.set()

    threading.Thread(target=run, daemon=True).start()
    assert finished.wait(2), "settle did not return with only finished tasks in the set"
