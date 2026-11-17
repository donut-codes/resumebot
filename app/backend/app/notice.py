"""The small local channel between the page program and the engine program: one loopback datagram that says "look".

Two UDP endpoints on 127.0.0.1, no connection, no thread, no timer. The page listens on its own port (uvicorn's
`--port`), the engine on that port plus `OFFSET` (UDP and TCP ports are separate spaces, so the numbers never clash).
A message carries ids and short tags, never row data: the receiver reads the rows. It is a hint, not a record: whatever
must not be lost is a database row first, and a lost datagram is found again by the receiver's catch-up (a new boot
id or a gap in the counter, see `Gaps`).

This module imports nothing from the app. It is `notice.py` and not `signal.py`: a file named `signal` in this
folder would shadow the standard library for any script run from here."""

import asyncio
import ctypes
import itertools
import json
import logging
import os
import socket
import sys
from collections.abc import Callable

#: The engine's port is the page's plus this.
OFFSET = 100
#: A datagram is at most this many bytes; a longer batch becomes one `resync`.
MAX_BYTES = 1200
VERSION = 1
LOOPBACK = "127.0.0.1"

log = logging.getLogger(__name__)

#: This process's id as a sender: eight random bytes, new at every start. A receiver that sees another one knows the
#: sender restarted.
BOOT = os.urandom(8).hex()
_counter = itertools.count(1)
_sender: socket.socket | None = None
#: Ports a test sets with `use_ports`: (page port, engine port). None: read them from the command line.
_injected: tuple[int, int] | None = None


def own_port(argv: list[str] | None = None) -> int | None:
    """The port this server was started on, read from its command line the way uvicorn takes it (`--port N` or
    `--port=N`); None when it is not named there (tests, `resumebot serve`)."""
    args = sys.argv[1:] if argv is None else argv
    port = None
    for index, arg in enumerate(args):
        if arg == "--port" and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--port="):
            value = arg.split("=", 1)[1]
        else:
            continue
        port = int(value) if value.isdigit() else None
    return port


def use_ports(page: int | None, engine: int | None = None) -> None:
    """For tests: send to these ports instead of reading the command line. `use_ports(None)` goes back to it."""
    global _injected
    _injected = None if page is None or engine is None else (page, engine)


def page_target() -> tuple[str, int] | None:
    """Where the engine sends to the page: the page's port, which is this server's own `--port` minus `OFFSET`
    (the engine's own `--port` is the page's plus `OFFSET`). None when the signal is off."""
    if _injected is not None:
        return LOOPBACK, _injected[0]
    port = own_port()
    return None if port is None or port <= OFFSET else (LOOPBACK, port - OFFSET)


def engine_target() -> tuple[str, int] | None:
    """Where the page sends to the engine: the page's own `--port` plus `OFFSET`. None when the signal is off."""
    if _injected is not None:
        return LOOPBACK, _injected[1]
    port = own_port()
    return None if port is None or port + OFFSET > 65535 else (LOOPBACK, port + OFFSET)


def listen_port() -> int | None:
    """The port the engine program listens on for the page's bells: the injected one in a test (`use_ports`), else its
    own `--port`. None when the signal is off."""
    if _injected is not None:
        return _injected[1]
    return own_port()


def _plain(items) -> list[list]:
    """Each item as [kind, id, tags]: kind and id as text, tags as a list of text. An item is (kind, id) or
    (kind, id, tags)."""
    out = []
    for item in items:
        kind, ident = item[0], item[1]
        tags = item[2] if len(item) > 2 else ()
        out.append([str(kind), str(ident), [str(tag) for tag in tags]])
    return out


def _dump(body: dict) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def encode(owner: str, items, *, boot: str, n: int) -> bytes:
    """One page-bound datagram: header `v`, `b` (boot), `n` (counter) and body `o` (owner), `m` ([[kind, id, tags]]).
    A batch that does not fit `MAX_BYTES` becomes one `resync` for that owner."""
    head = {"v": VERSION, "b": boot, "n": n, "o": str(owner)}
    data = _dump({**head, "m": _plain(items)})
    if len(data) > MAX_BYTES:
        data = _dump({**head, "m": [["resync", "", []]]})
    return data


def encode_bell(kind: str, fields: dict, *, boot: str, n: int) -> bytes:
    """One engine-bound datagram: the same header and `k` (kind), `f` (fields). Too long: the kind alone."""
    head = {"v": VERSION, "b": boot, "n": n}
    data = _dump({**head, "k": str(kind), "f": fields})
    if len(data) > MAX_BYTES:
        data = _dump({**head, "k": str(kind), "f": {}})
    return data


class Message:
    """A datagram read back: `boot`, `n`, and either `owner` and `items` ([(kind, id, tags)]) or `bell` and `fields`."""

    def __init__(self, boot: str, n: int, owner: str, items: list[tuple[str, str, list[str]]],
                 bell: str | None, fields: dict):
        self.boot, self.n, self.owner, self.items, self.bell, self.fields = boot, n, owner, items, bell, fields


def decode(data: bytes) -> Message | None:
    """The message a datagram holds; None for anything else (garbage, another version, a wrong shape)."""
    try:
        body = json.loads(data.decode("ascii"))
        if body["v"] != VERSION:
            return None
        boot, n = body["b"], body["n"]
        if not isinstance(boot, str) or not isinstance(n, int) or isinstance(n, bool):
            return None
        if "k" in body:
            fields = body["f"]
            if not isinstance(body["k"], str) or not isinstance(fields, dict):
                return None
            return Message(boot, n, "", [], body["k"], fields)
        owner = body["o"]
        items = [(str(m[0]), str(m[1]), [str(tag) for tag in m[2]]) for m in body["m"]]
        if not isinstance(owner, str):
            return None
        return Message(boot, n, owner, items, None, {})
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def _send(data: bytes, target: tuple[str, int] | None) -> bool:
    """One non-blocking `sendto` on an unconnected socket. Any `OSError` is swallowed: a program that is not running
    costs one failed syscall. True when the system took the datagram."""
    global _sender
    if target is None:
        return False
    try:
        if _sender is None:
            _sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _sender.setblocking(False)
        _sender.sendto(data, target)
    except OSError:
        return False
    return True


def tell_page(owner: str, items) -> bool:
    """Engine to page, after a commit: `owner` changed these `items` ((kind, id) or (kind, id, tags))."""
    target = page_target()
    if target is None:
        return False
    return _send(encode(owner, items, boot=BOOT, n=next(_counter)), target)


def ring_engine(kind: str, **fields) -> bool:
    """Page to engine, after a commit: a hint to look (`look`, `knock`, `start:<name>`). Never starts work by itself."""
    target = engine_target()
    if target is None:
        return False
    return _send(encode_bell(kind, fields, boot=BOOT, n=next(_counter)), target)


#: WSAIoctl code SIO_UDP_CONNRESET (the standard library does not name it).
_SIO_UDP_CONNRESET = 0x9800000C


def _no_connreset(sock: socket.socket) -> None:
    """Turn off Windows' habit of failing the next receive after a send to a closed port. Windows only."""
    off = ctypes.c_ulong(0)
    returned = ctypes.c_ulong(0)
    result = ctypes.windll.Ws2_32.WSAIoctl(
        ctypes.c_size_t(sock.fileno()), ctypes.c_ulong(_SIO_UDP_CONNRESET), ctypes.byref(off), ctypes.c_ulong(4),
        None, ctypes.c_ulong(0), ctypes.byref(returned), None, None)
    if result != 0:
        raise OSError("could not turn off SIO_UDP_CONNRESET")


class Gaps:
    """What a receiver watches for: a sender that restarted (a boot id it has not seen) and datagrams that never came
    (a counter that skipped). Either one means "read everything once"; the caller does that, it is not a timer."""

    def __init__(self) -> None:
        self._boot: str | None = None
        self._last = 0

    def check(self, message: Message) -> str | None:
        """'new_boot' on the first message and on a changed boot id, 'gap' when the counter skipped, else None.
        A counter that did not go up (a repeat) is ignored."""
        if message.boot != self._boot:
            self._boot, self._last = message.boot, message.n
            return "new_boot"
        if message.n <= self._last:
            return None
        gap = message.n > self._last + 1
        self._last = message.n
        return "gap" if gap else None


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, receiver: "Receiver") -> None:
        self._receiver = receiver

    def datagram_received(self, data: bytes, addr) -> None:
        if addr[0] != LOOPBACK:
            return
        message = decode(data)
        if message is None:
            return
        try:
            self._receiver.on_message(message)
        except Exception:  # one bad handler must not end the endpoint
            log.exception("a notice handler failed")

    def error_received(self, exc: Exception) -> None:
        pass  # an earlier send to a port nobody listens on; nothing to do


class Receiver:
    """Listens on a UDP port on 127.0.0.1 only and calls `on_message(Message)` (a plain function, run on the loop)
    for each datagram that decodes. Anything else is dropped. `port` 0 lets the system pick (tests)."""

    def __init__(self, port: int, on_message: Callable[[Message], None]) -> None:
        self.port = port
        self.on_message = on_message
        self._transport: asyncio.BaseTransport | None = None

    async def start(self) -> None:
        """Bind and listen. Raises `OSError` when the port is taken; the caller says so (the page's /health)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            # Windows reports an earlier send to a closed port as an error on the next receive and would end the
            # endpoint; this turns that off. Other systems have no such habit and no such call.
            if sys.platform == "win32":
                _no_connreset(sock)
            sock.bind((LOOPBACK, self.port))
            sock.setblocking(False)
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _Listener(self), sock=sock)
        except BaseException:
            sock.close()
            raise
        self._transport = transport
        self.port = sock.getsockname()[1]

    def address(self) -> tuple[str, int]:
        """The (host, port) it listens on; the host is always the loopback."""
        if self._transport is None:
            raise RuntimeError("the receiver is not started")
        host, port = self._transport.get_extra_info("sockname")[:2]
        return host, port

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class Waiter:
    """One caller waiting for a notice. Made by `Waiters.wait_for` BEFORE the caller commits, so a notice that comes
    early is kept: `wait` then returns at once."""

    def __init__(self, owner: "Waiters", keys: list[tuple[str, str]]) -> None:
        self._owner, self._keys = owner, keys
        self._event = asyncio.Event()

    async def wait(self, timeout: float) -> bool:
        """True when a notice came, False at the timeout. Always removes the waiter."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self.close()

    def close(self) -> None:
        self._owner._drop(self)


class Waiters:
    """The callers waiting for a notice, by (kind, id). `wait_for` registers (sync); `notify` wakes."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], list[Waiter]] = {}

    def wait_for(self, kind: str, ids) -> Waiter:
        keys = [(str(kind), str(i)) for i in ids]
        waiter = Waiter(self, keys)
        for key in keys:
            self._by_key.setdefault(key, []).append(waiter)
        return waiter

    def notify(self, kind: str, ids) -> int:
        """Wake every waiter on any of these ids; how many were woken."""
        woken = {w for i in ids for w in self._by_key.get((str(kind), str(i)), ())}
        for waiter in woken:
            waiter._event.set()
        return len(woken)

    def wake_all(self) -> int:
        """Wake every waiter (the sender restarted: each re-reads its row once)."""
        woken = {w for waiters in self._by_key.values() for w in waiters}
        for waiter in woken:
            waiter._event.set()
        return len(woken)

    def _drop(self, waiter: Waiter) -> None:
        for key in waiter._keys:
            left = [w for w in self._by_key.get(key, []) if w is not waiter]
            if left:
                self._by_key[key] = left
            else:
                self._by_key.pop(key, None)


#: The one set of waiters of this process.
waiters = Waiters()


#: Why the page is not listening for the engine's notices, or None. The page's start sets it when its bind failed;
#: `/health` reports it (app/jobsearch/factory.py), because a notice the page cannot hear is the one place the "told"
#: promise fails unseen.
BIND_PROBLEM: str | None = None


def page_port() -> int | None:
    """The UDP port the page listens on: this server's own `--port` when it is the page, or the page port a test set
    with `use_ports`. None when the signal is off."""
    if _injected is not None:
        return _injected[0]
    return own_port()
