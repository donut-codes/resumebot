"""Tests of local/tab_check.py's own parts (the live check itself is run once by the owner's say-so, never here).
Standard library only. Nothing here starts Chrome or reaches the app: the bridge and the app are small servers of this
file's own."""

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent / "local"))

import tab_check


class _Recorder(BaseHTTPRequestHandler):
    seen: list = []

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        type(self).seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = do_DELETE = _answer

    def log_message(self, *args) -> None:
        return None


def _serve() -> tuple[HTTPServer, type]:
    handler = type("Handler", (_Recorder,), {"seen": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler


class TabCheckTest(unittest.TestCase):
    def test_it_does_nothing_without_the_owners_ok(self) -> None:
        args = SimpleNamespace(i_have_the_owners_ok=False)
        self.assertEqual(tab_check.outer(args), 2)

    def test_the_cleanup_names_what_it_opened_to_the_bridge_and_takes_the_rows_off_and_forgets_the_list(self) -> None:
        bridge, bridge_seen = _serve()
        app, app_seen = _serve()
        try:
            with tempfile.TemporaryDirectory() as folder:
                run, secrets = Path(folder, "run"), Path(folder, "secrets")
                run.mkdir()
                secrets.mkdir()
                (secrets / "agent-token").write_text("tok\n", encoding="utf-8")
                (run / tab_check.OPENED_FILE).write_text(json.dumps(["T1", "T2"]), encoding="utf-8")
                config = SimpleNamespace(run=run, secrets=secrets, app_url=f"http://127.0.0.1:{app.server_address[1]}")
                self.assertEqual(tab_check._clean_up(config, bridge.server_address[1]), 2)
                self.assertFalse((run / tab_check.OPENED_FILE).exists())
                self.assertEqual(tab_check._clean_up(config, bridge.server_address[1]), 0, "nothing left to name")
            self.assertEqual([(m, p) for m, p, _a, _b in bridge_seen.seen], [("POST", "/tabs/close")])
            self.assertEqual(json.loads(bridge_seen.seen[0][3]), {"targets": ["T1", "T2"]})
            self.assertEqual([(m, p, a) for m, p, a, _b in app_seen.seen], [
                ("DELETE", "/api/v1/agent/browser/tabs/T1", "Bearer tok"),
                ("DELETE", "/api/v1/agent/browser/tabs/T2", "Bearer tok")])
        finally:
            bridge.shutdown()
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
