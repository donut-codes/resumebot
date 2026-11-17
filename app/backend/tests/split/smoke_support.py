"""Stand-ins for the two programs, for the smoke test (`stack.py smoke`): a fake page and a fake engine, each a small
HTTP server on 127.0.0.1 (the page also holds its UDP notice port), answering what the smoke asks and recording what it
was asked. Everything is made up."""

import json
import queue
import random
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECURITY = {"Content-Security-Policy": "default-src 'self'", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer"}
TOKENS = {"/mcp/": "agent-secret", "/mcp-browser/": "browser-secret", "/mcp-owner/": "owner-secret"}
TOOLS = {"/mcp/": b"jobs.profile", "/mcp-browser/": b"browser.next_item", "/mcp-owner/": b"act.do"}
SHELL = b'<html><body><div id="root"></div><script src="/assets/app.js"></script></body></html>'

FALLBACK_PORTS = range(20000, 49000)  # below Windows' dynamic range (49152 up), clear of its excluded UDP blocks


def _udp_at(port: int) -> socket.socket:
    """A UDP socket bound to `port` on 127.0.0.1; `OSError` when it cannot be."""
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.bind(("127.0.0.1", port))
    except OSError:
        udp.close()
        raise
    return udp


class Fake:
    """One program. `page=True`: the page (the shell, the screens' calls, the live stream, the UDP port equal to its TCP
    port). `doors`: the MCP doors it serves ("/mcp/", "/mcp-browser/", "/mcp-owner/"); any other door is a 404.
    `asked` lists "GET /path" and "POST /path" in the order they came. `datagrams` lists the JSON bodies that reached
    the UDP port. `hello`: whether a stream opens with `hello`. `resyncs`: whether a datagram makes the open streams
    hear `resync`. `owner_door_answers`: whether /mcp-owner/ answers a call that has its token."""

    def __init__(self, *, page: bool, version: str, doors: tuple[str, ...] = (),
                 owner_door_answers: bool = True) -> None:
        self.page, self.version, self.doors = page, version, doors
        self.owner_door_answers = owner_door_answers
        self.hello = True
        self.resyncs = True
        self.asked: list[str] = []
        self.datagrams: list[dict] = []
        self._streams: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._started = False
        self._stopped = False
        self._udp: socket.socket | None = None
        self.server: ThreadingHTTPServer | None = None
        # The page needs one number free for TCP and UDP. Choosing the UDP port first fails too: Windows hands UDP
        # ports out one after another into excluded ranges. So the OS picks the TCP port and the page takes the same
        # UDP port; but Windows also hands TCP ports out one after another, so a run of them can all fall in one
        # excluded UDP block (100 wide) and every try fails. Then the numbers below the dynamic range are tried, at
        # random, TCP first and the same UDP number second, skipping any that is refused.
        last_error: OSError | None = None
        candidates = [0] * 5 + random.sample(range(FALLBACK_PORTS.start, FALLBACK_PORTS.stop), 300)
        for number in candidates if page else [0]:
            try:
                server = ThreadingHTTPServer(("127.0.0.1", number), self._handler())
            except OSError as error:
                last_error = error
                continue
            if not page:
                self.server = server
                break
            try:
                udp = _udp_at(server.server_address[1])
            except OSError as error:
                last_error = error
                server.server_close()
                continue
            self.server, self._udp = server, udp
            break
        if self.server is None:
            raise RuntimeError(f"no free port for the fake program: {last_error!r}") from last_error
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        assert self.server is not None
        self._started = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        if self._udp is not None:
            threading.Thread(target=self._listen, daemon=True).start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._done.set()
        assert self.server is not None
        if self._started:
            self.server.shutdown()
        self.server.server_close()
        if self._udp is not None:
            self._udp.close()

    def _listen(self) -> None:
        assert self._udp is not None
        self._udp.settimeout(0.2)
        while not self._done.is_set():
            try:
                data, _ = self._udp.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                body = json.loads(data.decode("ascii"))
            except ValueError:
                continue
            self.datagrams.append(body)
            if self.resyncs and body.get("v") == 1 and "b" in body:
                with self._lock:
                    for waiting in self._streams:
                        waiting.put("resync")

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                for name, value in {"Content-Length": str(len(body)), **(headers or {})}.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                fake.asked.append("GET " + path)
                if path == "/health":
                    body = {"app": "resumebot", "status": "ok", "version": fake.version, "db": "ok"}
                    return self._send(200, json.dumps(body).encode(), {"Content-Type": "application/json"})
                if not fake.page:
                    return self._send(404)
                if path == "/api/v1/live":
                    return self._stream()
                if path.startswith("/api/"):
                    return self._send(200, b"{}", {**SECURITY, "Content-Type": "application/json",
                                                   "Cache-Control": "no-store"})
                if path == "/assets/not-a-file.js":
                    return self._send(404, b"", SECURITY)
                if path == "/assets/app.js":
                    return self._send(200, b"//", {**SECURITY, "Cache-Control": "public, max-age=31536000, immutable"})
                if path == "/export.xlsx":
                    return self._send(200, b"PK\x03\x04", {"Content-Type": "application/octet-stream"})
                return self._send(200, SHELL, {**SECURITY, "Cache-Control": "no-cache", "Content-Type": "text/html"})

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                fake.asked.append("POST " + path)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if path not in fake.doors:
                    return self._send(404)
                good = (self.headers.get("Authorization") or "") == "Bearer " + TOKENS[path]
                if not good and path == "/mcp/":
                    return self._send(200, b'{"result":{"content":[{"text":"no valid agent token"}]}}',
                                      {"Content-Type": "application/json"})
                if not good:
                    return self._send(401)
                if path == "/mcp-owner/" and not fake.owner_door_answers:
                    return self._send(503)
                return self._send(200, b'{"result":{"tools":[{"name":"' + TOOLS[path] + b'"}]}}',
                                  {"Content-Type": "application/json"})

            def _stream(self) -> None:
                waiting: queue.Queue = queue.Queue()
                with fake._lock:
                    fake._streams.append(waiting)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    if fake.hello:
                        self.wfile.write(b"id: 0\nevent: hello\ndata: {}\n\n")
                        self.wfile.flush()
                    while not fake._done.is_set():
                        try:
                            kind = waiting.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        self.wfile.write(f"id: 1\nevent: {kind}\ndata: {{}}\n\n".encode())
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    with fake._lock:
                        fake._streams.remove(waiting)

        return Handler
