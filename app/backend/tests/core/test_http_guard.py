"""The Host and Origin checks in front of every call and the strict JSON content type, with the
install's own ways in left open: the page by IP address or localhost (a proxy may pass the host without its port),
the scout by a service name, the browser door as a proxy rewrites it. No database for the guard itself; some tests go
through the real app."""

import logging
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app import http_guard
from app.jobsearch.main import app
from app.settings import settings


async def _inner(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


@pytest.fixture
def lists(monkeypatch):
    """The install's lists, empty unless a test sets them (conftest lists `test` for the other tests)."""
    monkeypatch.setattr(settings, "allowed_hosts", "")
    monkeypatch.setattr(settings, "mcp_allowed_hosts", "")
    monkeypatch.setattr(settings, "public_url", "")


async def _call(method: str, host: str, **headers) -> int:
    guarded = http_guard.HttpGuard(_inner)
    async with AsyncClient(transport=ASGITransport(app=guarded), base_url=f"http://{host}") as c:
        return (await c.request(method, "/x", headers=headers)).status_code


@pytest.mark.parametrize("host", [
    "localhost", "localhost:8430", "localhost:8420", "127.0.0.1:8430", "192.168.1.20", "192.168.1.20:8430",
    "[::1]:8430", "server.lan", "myserver.local:8430", "Server.LAN.", "app.localhost",
])
async def test_the_installs_own_kinds_of_host_are_answered(lists, host):
    assert await _call("GET", host) == 200


@pytest.mark.parametrize("host", [
    "attacker.example", "attacker.example:8430", "192.168.1.20.attacker.example", "server.lan.attacker.example",
    "evillocalhost", "test",
])
async def test_a_name_the_install_does_not_know_gets_421(lists, host):
    assert await _call("GET", host) == 421
    assert await _call("POST", host) == 421


async def test_what_the_install_lists_is_answered(lists, monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", "jobs.example.org, *.corp.example:*")
    monkeypatch.setattr(settings, "mcp_allowed_hosts", "resumebot:*")
    monkeypatch.setattr(settings, "public_url", "https://front.example:8443/")
    for host in ("jobs.example.org", "jobs.example.org:8430", "a.corp.example", "resumebot:8420", "front.example:8443"):
        assert await _call("GET", host) == 200, host
    assert await _call("GET", "corp.example") == 421
    assert await _call("GET", "other.example") == 421


async def test_the_scouts_calls_and_the_browser_door_pass(lists, monkeypatch):
    monkeypatch.setattr(settings, "mcp_allowed_hosts", "resumebot:*")
    # The scout: a service name, a bearer token, no Origin. A proxied browser door: Host localhost:8420.
    assert await _call("POST", "resumebot:8420", authorization="Bearer x") == 200
    assert await _call("POST", "localhost:8420", authorization="Bearer x") == 200
    assert await _call("GET", "localhost:8420") == 200


async def test_a_post_from_the_pages_own_origin_passes_with_or_without_the_port_nginx_dropped(lists):
    assert await _call("POST", "192.168.1.20", origin="http://192.168.1.20:8430") == 200
    assert await _call("POST", "localhost", origin="http://localhost:8430") == 200
    assert await _call("POST", "192.168.1.20:8430", origin="http://192.168.1.20:8430") == 200
    same = {"sec-fetch-site": "same-origin"}
    assert await _call("POST", "localhost:8430", origin="http://localhost:8430", **same) == 200


async def test_a_post_from_another_site_is_refused(lists):
    for origin in ("http://attacker.example", "http://localhost:9999", "null", "http://192.168.1.21:8430"):
        host = "192.168.1.20:8430" if origin.endswith("21:8430") else "localhost:8430"
        assert await _call("POST", host, origin=origin) == 403, origin
        assert await _call("DELETE", host, origin=origin) == 403, origin
        assert await _call("PUT", host, origin=origin) == 403, origin
    assert await _call("POST", "localhost:8430", **{"sec-fetch-site": "cross-site"}) == 403
    assert await _call("POST", "localhost:8430", **{"sec-fetch-site": "same-site"}) == 403


async def test_reading_from_another_site_is_not_this_checks_job(lists):
    # A cross-origin read is stopped by the browser's own rules (and a rebinding read by the Host check above).
    assert await _call("GET", "localhost", origin="http://attacker.example") == 200
    assert await _call("OPTIONS", "localhost", origin="http://attacker.example") == 200


async def test_the_refusal_says_why_in_plain_words(lists):
    guarded = http_guard.HttpGuard(_inner)
    async with AsyncClient(transport=ASGITransport(app=guarded), base_url="http://attacker.example") as c:
        resp = await c.get("/x")
    assert resp.status_code == 421
    assert "ALLOWED_HOSTS" in resp.json()["detail"]


async def test_the_real_app_refuses_before_any_route_runs(lists):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://attacker.example") as c:
        assert (await c.get("/api/v1/me")).status_code == 421
        assert (await c.post("/api/v1/notifications/read-all")).status_code == 421
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8430") as c:
        resp = await c.post("/api/v1/notifications/read-all", headers={"Origin": "http://attacker.example"})
        assert resp.status_code == 403


async def test_a_json_body_needs_a_json_content_type(js):
    # A cross-site form can send text/plain without a preflight; the route must not read it as JSON.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        url = f"/api/v1/roles/{uuid.uuid4()}/move"
        plain = await c.post(url, content=b'{"to": "applied"}', headers={"Content-Type": "text/plain"})
        none = await c.post(url, content=b'{"to": "applied"}')
        assert plain.status_code == 422
        assert none.status_code == 422
        ok = await c.post(url, json={"to": "applied"})
        assert ok.status_code != 422


async def _hang_up_mid_body(target, path: str = "/x", query: bytes = b"") -> list[dict]:
    """Drive `target` by hand the way a server does for a client that hangs up before its body arrived: the first
    thing the app's `receive` returns is the disconnect. What the app sent, in order."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http", "method": "POST",
        "path": path, "raw_path": path.encode(), "root_path": "", "query_string": query, "server": ("127.0.0.1", 8430),
        "client": ("127.0.0.1", 50000),
        "headers": [(b"host", b"localhost:8430"), (b"content-type", b"application/json"), (b"content-length", b"24")],
    }
    await target(scope, receive, send)
    return sent


async def _reads_its_body(scope, receive, send):
    await Request(scope, receive).body()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_a_client_that_hangs_up_while_the_body_is_read_is_the_end_of_the_call_not_an_error(lists, caplog):
    caplog.set_level(logging.DEBUG)
    sent = await _hang_up_mid_body(http_guard.HttpGuard(_reads_its_body))
    assert sent == []  # nobody is listening: no answer is attempted
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_hang_up_inside_a_real_route_leaves_no_traceback_and_no_answer(js, caplog):
    # The resume file's route reads the raw body itself (the others have FastAPI answer a failed read with a 400).
    caplog.set_level(logging.DEBUG)
    sent = await _hang_up_mid_body(app, "/api/v1/profile/parse/resume/file", b"name=resume.txt")
    assert sent == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_any_other_error_under_the_guard_still_goes_up(lists):
    async def broken(scope, receive, send):
        raise RuntimeError("a real bug")

    with pytest.raises(RuntimeError, match="a real bug"):
        await _hang_up_mid_body(http_guard.HttpGuard(broken))


@pytest.fixture
def refusal_clock(monkeypatch):
    """A clock the test moves, and a log that starts empty."""
    now = [1000.0]
    monkeypatch.setattr(http_guard, "clock", lambda: now[0], raising=False)
    monkeypatch.setattr(http_guard, "_logged", {}, raising=False)
    return now


async def test_a_refused_host_leaves_one_warning_with_the_host_only(lists, refusal_clock, caplog):
    caplog.set_level(logging.WARNING, logger="app.http_guard")
    assert await _call("GET", "attacker.example:8430") == 421
    lines = [r for r in caplog.records if r.name == "app.http_guard"]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert "host not allowed" in lines[0].getMessage()
    assert "attacker.example:8430" in lines[0].getMessage()


async def test_a_refused_origin_leaves_one_warning_with_both_authorities_and_no_path(lists, refusal_clock, caplog):
    caplog.set_level(logging.WARNING, logger="app.http_guard")
    status = await _call("POST", "localhost:8430", origin="https://user:pw-made-up@evil.example:9/secret-path?key=q-made-up")
    assert status == 403
    text = caplog.text
    assert "origin not allowed" in text and "evil.example:9" in text and "localhost:8430" in text
    for leaked in ("secret-path", "q-made-up", "pw-made-up", "https://"):
        assert leaked not in text


async def test_a_refusal_is_logged_once_a_minute_per_rule(lists, refusal_clock, caplog):
    caplog.set_level(logging.WARNING, logger="app.http_guard")
    for _ in range(3):
        await _call("GET", "attacker.example")
    assert len(caplog.records) == 1
    await _call("POST", "localhost", origin="https://evil.example")  # another rule has its own minute
    assert len(caplog.records) == 2
    refusal_clock[0] += 61
    await _call("GET", "attacker.example")
    assert len(caplog.records) == 3


async def test_a_token_in_the_query_never_reaches_the_refusal_log(lists, refusal_clock, caplog):
    caplog.set_level(logging.DEBUG)
    guarded = http_guard.HttpGuard(_inner)
    async with AsyncClient(transport=ASGITransport(app=guarded), base_url="http://attacker.example") as c:
        r = await c.get("/x?key=sk-made-up-key-123", headers={"authorization": "Bearer tok-made-up-456"})
    assert r.status_code == 421
    ours = " | ".join(x.getMessage() for x in caplog.records if x.name != "httpx")  # httpx is the test's own client
    assert "sk-made-up-key-123" not in ours and "tok-made-up-456" not in ours
    assert "refused 421" in ours
