"""The notice module (`app.notice`): loopback datagrams between the page and the engine."""

import asyncio
import socket

import pytest

from app import notice


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


def test_a_send_to_a_port_nobody_listens_on_returns_true_and_raises_nothing():
    notice.use_ports(_free_port(), _free_port())
    assert notice.tell_page("owner-1", [("role", "1")]) is True
    assert notice.tell_page("owner-1", [("role", "1")]) is True
    assert notice.ring_engine("look") is True
    # The send socket never waits: it is non-blocking (a timeout of 0.0), so a missing listener costs one failed call.
    assert notice._sender is not None and notice._sender.gettimeout() == 0.0


def test_the_engine_listens_on_the_injected_port_or_its_own_port_flag():
    notice.use_ports(4001, 4101)
    try:
        assert notice.listen_port() == 4101
    finally:
        notice.use_ports(None)
    assert notice.listen_port() == notice.own_port()


def test_the_signal_is_off_without_ports(monkeypatch):
    monkeypatch.setattr(notice.sys, "argv", ["pytest"])
    assert notice.tell_page("owner-1", [("role", "1")]) is False
    assert notice.ring_engine("look") is False


def test_the_ports_come_from_the_servers_own_port_argument(monkeypatch):
    monkeypatch.setattr(notice.sys, "argv", ["uvicorn", "x:app", "--port", "8430"])
    assert notice.engine_target() == ("127.0.0.1", 8530)
    monkeypatch.setattr(notice.sys, "argv", ["uvicorn", "x:app", "--port=8530"])
    assert notice.page_target() == ("127.0.0.1", 8430)
    assert notice.own_port(["--host", "127.0.0.1"]) is None


def test_an_oversize_batch_becomes_one_resync_for_that_owner():
    many = [("role", str(i) * 20, ["changed"]) for i in range(80)]
    data = notice.encode("owner-1", many, boot="b1", n=1)
    message = notice.decode(data)
    assert len(data) <= notice.MAX_BYTES
    assert message is not None and message.owner == "owner-1"
    assert message.items == [("resync", "", [])]


def test_a_small_batch_keeps_its_items_and_turns_ids_into_text():
    message = notice.decode(notice.encode("owner-1", [("role", 12, ["new"]), ("ask", "x")], boot="b1", n=7))
    assert message is not None
    assert (message.boot, message.n) == ("b1", 7)
    assert message.items == [("role", "12", ["new"]), ("ask", "x", [])]


@pytest.mark.parametrize("data", [b"\xff\x00garbage", b"{}", b"[]", b'{"v":2,"b":"x","n":1,"o":"o","m":[]}',
                                  b'{"v":1,"b":"x","n":"1","o":"o","m":[]}', b'{"v":1,"b":"x","n":1,"o":"o","m":5}'])
def test_a_datagram_that_is_not_a_notice_decodes_to_nothing(data):
    assert notice.decode(data) is None


def _message(boot: str, n: int) -> notice.Message:
    return notice.Message(boot, n, "owner-1", [], None, {})


def test_a_new_boot_id_and_a_skipped_counter_are_reported_a_repeat_is_not():
    gaps = notice.Gaps()
    seen = [gaps.check(_message("a", 1)), gaps.check(_message("a", 2)), gaps.check(_message("a", 4)),
            gaps.check(_message("a", 4)), gaps.check(_message("b", 1)), gaps.check(_message("b", 2))]
    assert seen == ["new_boot", None, "gap", None, "new_boot", None]


async def test_a_message_goes_round_over_one_real_loopback_socket():
    # The only real socket in the suite's notice tests: the round trip is what the module is for.
    got: asyncio.Queue[notice.Message] = asyncio.Queue()
    receiver = notice.Receiver(0, got.put_nowait)
    await receiver.start()
    try:
        notice.use_ports(receiver.port, receiver.port)
        notice.tell_page("owner-1", [("role", 12, ["new"]), ("ask", "x")])
        message = await asyncio.wait_for(got.get(), 2)
        assert message.owner == "owner-1"
        assert message.items == [("role", "12", ["new"]), ("ask", "x", [])]
        notice.ring_engine("start:go", name="a")
        bell = await asyncio.wait_for(got.get(), 2)
        assert bell.bell == "start:go" and bell.fields == {"name": "a"}
        assert receiver.address()[0] == "127.0.0.1"
    finally:
        receiver.stop()


async def test_the_listener_ignores_garbage_and_a_failing_handler_does_not_end_it():
    seen: list[notice.Message] = []

    def handler(message: notice.Message) -> None:
        seen.append(message)
        raise RuntimeError("a handler that fails")

    listener = notice._Listener(notice.Receiver(0, handler))
    listener.datagram_received(b"\xff\x00garbage", ("127.0.0.1", 9))
    listener.datagram_received(notice.encode("owner-1", [("role", "1")], boot="b", n=1), ("10.0.0.5", 9))
    assert seen == []  # garbage, and a sender that is not the loopback
    listener.datagram_received(notice.encode("owner-1", [("role", "1")], boot="b", n=1), ("127.0.0.1", 9))
    listener.datagram_received(notice.encode("owner-1", [("role", "2")], boot="b", n=2), ("127.0.0.1", 9))
    assert [m.items[0][1] for m in seen] == ["1", "2"]


async def test_a_waiter_registered_first_returns_when_the_notice_comes_even_early():
    waiters = notice.Waiters()
    waiter = waiters.wait_for("ask", ["7"])
    assert waiters.notify("ask", ["7"]) == 1  # before anyone awaits
    assert await waiter.wait(1) is True


async def test_a_waiter_times_out_and_is_removed_and_another_id_does_not_wake_it():
    waiters = notice.Waiters()
    waiter = waiters.wait_for("ask", ["7"])
    assert waiters.notify("ask", ["8"]) == 0
    assert waiters.notify("action_run", ["7"]) == 0
    assert await waiter.wait(0.01) is False
    assert waiters.notify("ask", ["7"]) == 0


async def test_wake_all_wakes_every_waiter():
    waiters = notice.Waiters()
    first, second = waiters.wait_for("ask", ["1"]), waiters.wait_for("go_run", ["2"])
    assert waiters.wake_all() == 2
    assert await first.wait(1) is True and await second.wait(1) is True
