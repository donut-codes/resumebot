"""The wire sink and the page's live hub: the engine tells the page once per commit, the page's own
commits take the local sink, and an absent page costs the engine next to nothing. Everything is made up."""

import asyncio
import contextlib
import socket
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app import db, notice, program, stopping
from app.jobsearch import engine, page, stages
from app.jobsearch.factory import build_app
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import live, live_hub, live_sink
from tests.split.test_page_reads_take_no_write_lock import stream_status
from tests.split.test_route_programs import _keys
from tests.support import NOON, OWNER


def _role(slug: str) -> Opportunity:
    return Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}",
                       status=stages.FIRST_IMPRESSION, origin="scout", match_key=f"co {slug}|role {slug}",
                       found=NOON.date())


async def _commit(*rows) -> list:
    async with db.session_factory()() as session:
        session.add_all(rows)
        await session.commit()
        return [row.id for row in rows]


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def signal_ports():
    """The signal on, with the page at a free port (nobody listens there until a test makes a receiver) and the engine
    100 above it. Put back to the command line at the end."""
    port = _free_udp_port()
    notice.use_ports(port, port + 100)
    yield port
    notice.use_ports(None)


class Told:
    """Stands in for `notice.tell_page`: it keeps each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list]] = []

    def __call__(self, owner: str, items) -> bool:
        self.calls.append((owner, [tuple(item) for item in items]))
        return True


def test_the_broker_says_which_owners_have_a_stream():
    broker = live.Broker()
    assert broker.open_owners() == []
    sub = broker.subscribe(OWNER, frozenset({"counts"}))
    assert broker.open_owners() == [OWNER]
    broker.unsubscribe(sub)
    assert broker.open_owners() == []


async def test_a_commit_in_the_engine_tells_the_page_once_and_publishes_nothing_itself(js, monkeypatch):
    told = Told()
    monkeypatch.setattr(notice, "tell_page", told)
    monkeypatch.setattr(live_sink, "_sink", live_sink.WireSink())
    sub = live.broker.subscribe(OWNER)
    try:
        ids = await _commit(_role("a"), _role("b"))
        silent = sub.queue.empty()
    finally:
        live.broker.unsubscribe(sub)
    assert len(told.calls) == 1, "one datagram for one commit"
    owner, items = told.calls[0]
    assert owner == OWNER
    assert sorted((item[0], item[1]) for item in items) == sorted(("role", str(i)) for i in ids)
    assert all(isinstance(tag, str) for item in items for tag in item[2]), "tags go as text"
    assert silent, "the engine does not publish to a stream of its own"


async def test_a_commit_in_the_page_reaches_its_own_stream_with_no_datagram(js, monkeypatch):
    told = Told()
    monkeypatch.setattr(notice, "tell_page", told)
    monkeypatch.setattr(live_sink, "_sink", None)  # the local sink, as in the page
    sub = live.broker.subscribe(OWNER, frozenset({"role"}))
    try:
        (role_id,) = await _commit(_role("c"))
        _, kind, data = await asyncio.wait_for(sub.queue.get(), 5)
    finally:
        live.broker.unsubscribe(sub)
    assert (kind, data["id"]) == ("role", str(role_id))
    assert told.calls == [], "the page's own commit sends nothing over the wire"


def test_a_page_that_is_not_running_costs_the_sink_under_five_milliseconds(signal_ports):
    items = [("role", "1", ("new",))]
    took = []
    for _ in range(20):
        start = time.perf_counter()
        live_sink.WireSink().announce(OWNER, items)
        took.append(time.perf_counter() - start)
    assert min(took) < 0.005


def test_a_resync_is_one_item_for_the_owner(monkeypatch):
    told = Told()
    monkeypatch.setattr(notice, "tell_page", told)
    live_sink.tell_resync(OWNER)
    assert told.calls == [(OWNER, [("resync", "")])]


def test_the_text_tags_go_over_the_wire_and_come_back():
    tags = ("was:applied", "changed:status")
    assert live_sink.wire_tags(tags) == ["was:applied", "changed:status"]
    assert live_sink.item_tags(["was:applied", "changed:status"]) == tags
    assert live_sink.item_tags(live_sink.wire_tags(())) == ()


async def test_the_engines_start_puts_the_wire_sink_in_use_and_its_end_puts_the_local_one_back(monkeypatch):
    seen = []

    @contextlib.asynccontextmanager
    async def fake_start(_application):
        seen.append(live_sink.current())
        yield

    monkeypatch.setattr(engine, "_start", fake_start)
    monkeypatch.setattr(live_sink, "_sink", None)
    async with engine.lifespan(engine.app):
        pass
    assert isinstance(seen[0], live_sink.WireSink)
    assert isinstance(live_sink.current(), live_sink.LocalSink)


async def _first(sub: live.Subscriber, seconds: float = 5) -> tuple[int, str, dict]:
    """The next message of a stream, waited for by the queue and not by a sleep."""
    return await asyncio.wait_for(sub.queue.get(), seconds)


async def _until(sub: live.Subscriber, kind: str) -> tuple[list[str], dict]:
    """Messages are taken until one of `kind` comes: the kinds taken on the way, and its data."""
    seen: list[str] = []
    while True:
        _, got, data = await _first(sub)
        if got == kind:
            return seen, data
        seen.append(got)


def _datagram(port: int, n: int, boot: str = "boot-1") -> None:
    """A notice with no items, as a sender with that boot id and counter would send it."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(notice.encode(OWNER, [], boot=boot, n=n), ("127.0.0.1", port))


async def test_a_commit_in_the_engine_reaches_a_stream_of_the_page_through_the_datagram(js, monkeypatch, signal_ports):
    # Real sockets: the round trip over loopback is the thing proved; the OS hands out the port.
    monkeypatch.setattr(live, "COUNTS_AFTER_SECONDS", 0)
    listening = await live_hub.listen()  # the page's receiver and hub, with a local sink of their own
    sub = live.broker.subscribe(OWNER, frozenset({"role"}))
    monkeypatch.setattr(live_sink, "_sink", live_sink.WireSink())  # this process plays the engine for the commit
    try:
        (role_id,) = await _commit(_role("d"))
        before, data = await _until(sub, "role")
    finally:
        live.broker.unsubscribe(sub)
        await listening.stop()
    assert before in ([], ["resync"]), "at most the resync of the first message of a sender"
    assert data["id"] == str(role_id)
    assert data["title"] == "Role d", "the page built the message from the database"


async def test_five_hundred_commits_in_a_second_are_a_few_messages_not_five_hundred(js, signal_ports):
    listening = await live_hub.listen()
    sub = live.broker.subscribe(OWNER, frozenset({"role"}))
    try:
        for _ in range(500):
            notice.tell_page(OWNER, [("role", str(uuid.uuid4()), [])])
        first = await _first(sub)
        await listening.hub.settle()
        rest = []
        while not sub.queue.empty():
            rest.append(sub.queue.get_nowait())
    finally:
        live.broker.unsubscribe(sub)
        await listening.stop()
    assert first[1] == "resync"
    assert len(rest) <= 3


async def test_a_lost_counter_gives_one_resync_to_every_open_stream(js, monkeypatch, signal_ports):
    monkeypatch.setattr(live, "COUNTS_AFTER_SECONDS", 0)
    listening = await live_hub.listen()
    mine = live.broker.subscribe(OWNER, frozenset({"task"}))  # `resync` goes to a stream whatever it asked for
    try:
        _datagram(signal_ports, 1)
        assert (await _first(mine))[1] == "resync", "the first message of a sender is a new boot"
        _datagram(signal_ports, 2)
        _datagram(signal_ports, 4)  # 3 was lost
        assert (await _first(mine))[1] == "resync"
        await listening.hub.settle()
    finally:
        live.broker.unsubscribe(mine)
        await listening.stop()
    assert mine.queue.empty(), "one resync for the gap, no more"


async def test_a_page_that_cannot_bind_says_so_on_health_and_in_the_log(js, monkeypatch, caplog):
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind(("127.0.0.1", 0))
    port = held.getsockname()[1]
    monkeypatch.setattr(notice, "BIND_PROBLEM", None)
    notice.use_ports(port, port + 100)
    try:
        listening = await live_hub.listen()
        problem = notice.BIND_PROBLEM
        transport = ASGITransport(app=build_app("page"))
        async with AsyncClient(transport=transport, base_url="http://localhost:8430") as http:
            health = await http.get("/health")
        await listening.stop()
    finally:
        notice.use_ports(None)
        held.close()
    assert problem is not None and str(port) in problem
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert health.json()["notice"] == problem
    assert problem in caplog.text
    assert notice.BIND_PROBLEM is None, "a clean stop forgets it"


async def test_the_pages_start_listens_and_a_stop_request_closes_its_live_streams(js, monkeypatch):
    heard = []

    @contextlib.contextmanager
    def fake_heard(then):
        heard.append(then)
        yield

    fresh = live.Broker()
    monkeypatch.setattr(stopping, "heard", fake_heard)
    monkeypatch.setattr(live, "broker", fresh)
    monkeypatch.setattr(program, "PROGRAM", "all")
    monkeypatch.setattr("app.log_setup.configure_logging", lambda: None)
    monkeypatch.setattr(db, "configure", lambda url=None: None)
    monkeypatch.setattr(page, "session_managers", lambda role: [])
    async with page.lifespan(page.app):
        assert len(heard) == 1
        heard[0]()
        assert fresh.closed


def test_the_page_serves_the_live_stream_and_the_engine_does_not():
    page_keys, engine_keys = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert "GET /api/v1/live" in page_keys
    assert "GET /api/v1/live" not in engine_keys


async def test_the_live_stream_opens_on_the_page_app_with_its_hello(js):
    assert await stream_status(page.app, "/api/v1/live") == 200
