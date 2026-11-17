"""The browser bridge serves only clients that carry the app's key (docs/PRD.md path 52; the owner, 2026-11-14): the
app's own code reads the key from the data folder's `secrets/bridge-key` and sends it on every call to the bridge."""

import asyncio

import httpx

from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import tabs as tab_register
from app.settings import DATA_DIR_VARIABLE, JobSearchSettings, settings

REAL_BRIDGE_POST = tab_register.bridge_post  # conftest replaces the module's name for every test
KEY = "ab" * 32


def _with_key(monkeypatch, tmp_path, text: str = KEY) -> None:
    file = tmp_path / "bridge-key"
    file.write_text(text + "\n", encoding="utf-8")
    monkeypatch.setattr(settings, "bridge_key_file", str(file))


def _bridge_stand_in(monkeypatch, seen: list[httpx.Request], status: int = 200) -> None:
    """The bridge as a transport: every request it gets is kept, and it answers `status`."""
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"why": "no"} if status != 200 else {"closed": 0})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_the_key_is_read_from_the_secrets_folder_of_the_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(DATA_DIR_VARIABLE, str(tmp_path))
    assert JobSearchSettings().bridge_key() == "", "no key file yet: the app sends none, and the bridge refuses"
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "bridge-key").write_text(KEY + "\n", encoding="utf-8")
    assert JobSearchSettings().bridge_key() == KEY


async def test_the_devtools_connection_carries_the_key_in_the_address(monkeypatch, tmp_path):
    _with_key(monkeypatch, tmp_path)
    sent: list[bytes] = []

    class Writer:
        def write(self, data):
            sent.append(data)

        async def drain(self):
            return None

    class Reader:
        async def readuntil(self, _sep):
            return b"HTTP/1.1 101 Switching Protocols\r\n\r\n"

    async def connect(host, port):
        return Reader(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    await browser_code._DevTools(9333, "code:read").open()
    assert sent[0].startswith(f"GET /devtools/browser/code.read?key={KEY} HTTP/1.1\r\n".encode())


async def test_start_browser_sends_the_key_in_the_header(monkeypatch, tmp_path):
    _with_key(monkeypatch, tmp_path)
    seen: list[httpx.Request] = []
    _bridge_stand_in(monkeypatch, seen)
    assert await browser_code.start_browser() == ""
    assert [r.headers.get("x-bridge-key") for r in seen] == [KEY]
    assert seen[0].url.path == "/json/version"


async def test_a_bridge_that_refuses_the_app_is_said_in_one_line(monkeypatch, tmp_path):
    seen: list[httpx.Request] = []
    monkeypatch.setattr(settings, "bridge_key_file", str(tmp_path / "none"))
    _bridge_stand_in(monkeypatch, seen, status=403)
    assert await browser_code.start_browser() == "no"
    assert seen[0].headers.get("x-bridge-key") == "", "no key file: nothing to send"


async def test_the_tab_register_sends_the_key_with_its_calls_to_the_bridge(monkeypatch, tmp_path):
    _with_key(monkeypatch, tmp_path)
    seen: list[httpx.Request] = []
    _bridge_stand_in(monkeypatch, seen)
    assert await REAL_BRIDGE_POST("/tabs/close", {"targets": ["T1"]}) == {"closed": 0}
    assert [r.headers.get("x-bridge-key") for r in seen] == [KEY]
    assert seen[0].url.path == "/tabs/close"
