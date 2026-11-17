"""The page served by the app itself.

`app.web` serves the built page from the package, with the shell for any address of a screen and a 404 for a missing
asset; it keeps the cache rules (`no-store` on `/api/`, `immutable` on hashed assets, `no-cache` on the shell); it sends
the security headers on every answer, the guard's refusals included; and it leaves `/api/...`, `/health`, the export and
the browser door to the API. No database: the page is files, the API's side is a stand-in, and the real app is asked
only what needs none.
Everything here is made up."""

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from app import db, web
from app.jobsearch import main as real_main

#: The folder the app really serves the page from; the `page` fixture points `web.PAGE_DIR` at a made-up one instead.
REAL_PAGE_DIR = web.PAGE_DIR
FRONTEND = db.BACKEND_DIR.parent / "frontend"

SHELL = (
    b'<!doctype html><html lang="en"><head><meta charset="UTF-8" /><title>made up</title>'
    b'<link rel="icon" type="image/svg+xml" href="/favicon.svg" />'
    b'<script type="module" src="/assets/index-abc123.js"></script>'
    b'<link rel="stylesheet" href="/assets/index-def456.css" /></head><body><div id="root"></div></body></html>'
)
#: The made-up build, laid out as Vite lays one out: the shell and a root file, and hashed files under assets.
FILES = {
    "index.html": SHELL,
    "favicon.svg": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><path d="M0 0h1v1z"/></svg>',
    "assets/index-abc123.js": b'document.getElementById("root").textContent = "made up";\n',
    "assets/index-def456.css": b"@font-face { src: url(./archivo-xyz789.woff2); }\nbody { margin: 0; }\n",
    "assets/archivo-xyz789.woff2": bytes(range(256)),
}
TYPES = {
    "/": "text/html", "/index.html": "text/html", "/favicon.svg": "image/svg+xml",
    "/assets/index-abc123.js": "text/javascript", "/assets/index-def456.css": "text/css",
    "/assets/archivo-xyz789.woff2": "font/woff2",
}
#: What is beside the page, not in it: no request may ever be answered with it.
DECOY = b"this file is not the page's"

SECURITY_NAMES = ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy")


@pytest.fixture
def page(tmp_path, monkeypatch) -> Path:
    """A made-up built page, in a folder the app is pointed at, with a decoy file beside it."""
    root = tmp_path / "page"
    for name, content in FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    (tmp_path / "secret.txt").write_bytes(DECOY)
    monkeypatch.setattr(web, "PAGE_DIR", root)
    return root


def stub_app() -> FastAPI:
    """The API's side of the app, reduced to the kinds of answer the page's layer meets: a JSON API route, health, the
    export and a door that wants a token. The page's layer is put on it the way `jobsearch.main` puts it on the real
    app."""
    stub = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @stub.get("/api/v1/ping")
    async def ping() -> dict:
        return {"from": "the api"}

    @stub.get("/health")
    async def health() -> dict:
        return {"from": "health"}

    @stub.get("/export.xlsx")
    async def export() -> dict:
        return {"from": "the export"}

    async def door(scope, receive, send) -> None:
        await JSONResponse({"error": "no valid browser token on this call"}, status_code=401)(scope, receive, send)

    stub.mount("/mcp-browser", door)
    web.install(stub)
    return stub


async def get(app, path: str, method: str = "GET", host: str = "localhost:8430", **kw) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url=f"http://{host}") as c:
        return await c.request(method, path, **kw)


async def call(app, path: str, method: str = "GET") -> tuple[int, httpx.Headers, bytes]:
    """The app's answer to a request whose path is exactly `path`, put to it as an ASGI server would: no client between
    that tidies a path up first."""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode("utf-8"), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"localhost:8430")], "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8430),
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], httpx.Headers(start["headers"]), body


def kind(resp: httpx.Response) -> str:
    return resp.headers.get("content-type", "").split(";")[0]


def assert_secure(resp: httpx.Response, where: str = "") -> None:
    """Each security header once, with the value the app sends (the same on every answer)."""
    for name, value in web.SECURITY_HEADERS:
        assert resp.headers.get_list(name) == [value], f"{where or resp.request.url.path}: {name}"


# --- the page ------------------------------------------------------------------------------------------------------


async def test_the_shell_answers_the_root_and_its_own_name(page):
    for path in ("/", "/index.html"):
        resp = await get(stub_app(), path)
        assert resp.status_code == 200 and resp.content == SHELL, path
        assert kind(resp) == "text/html"


@pytest.mark.parametrize("path", [
    "/pipeline", "/pipeline/", "/employers/9f1c2d/merge", "/agents/how", "/inbox", "/a/b/c/d/e/f", "/pipeline//deep",
    "/robots.txt", "/not-a-file.js",
])
async def test_an_address_of_a_screen_is_the_shell_so_a_reload_or_a_pasted_link_opens_it(page, path):
    resp = await get(stub_app(), path)
    assert resp.status_code == 200 and resp.content == SHELL
    assert kind(resp) == "text/html"


@pytest.mark.parametrize("path", sorted(set(TYPES) - {"/", "/index.html"}))
async def test_a_file_of_the_page_is_that_file_with_its_type(page, path):
    resp = await get(stub_app(), path)
    assert resp.status_code == 200 and resp.content == FILES[path.lstrip("/")]
    assert kind(resp) == TYPES[path]


async def test_the_types_do_not_come_from_the_operating_system(page, monkeypatch):
    # Windows takes a type from its registry, where `.js` is often `text/plain`, and a browser refuses a module script
    # of that type: the page would load as a blank screen. The app names the types itself.
    import mimetypes

    import starlette.responses

    wrong = lambda *a, **k: ("text/plain", None)  # noqa: E731
    monkeypatch.setattr(mimetypes, "guess_type", wrong)
    monkeypatch.setattr(starlette.responses, "guess_type", wrong)
    for path, expected in TYPES.items():
        assert kind(await get(stub_app(), path)) == expected, path


async def test_a_file_of_a_type_the_app_does_not_know_is_sent_as_plain_bytes_that_a_browser_may_not_guess_at(page):
    (page / "assets" / "data.unheard-of").write_bytes(b"\x00\x01")
    resp = await get(stub_app(), "/assets/data.unheard-of")
    assert resp.status_code == 200 and kind(resp) == "application/octet-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_a_missing_asset_is_a_404_and_never_the_shell(page):
    # A script that moved must fail where it is loaded, not arrive as a page that a browser tries to run as code.
    for path in ("/assets/missing.js", "/assets/", "/assets/deeper/missing.css"):
        resp = await get(stub_app(), path)
        assert resp.status_code == 404, path
        assert resp.content != SHELL and kind(resp) == "application/json", path
        assert "cache-control" not in resp.headers, f"{path}: a 404 must not be kept"
        assert_secure(resp)


async def test_assets_are_kept_for_good_the_shell_and_the_root_files_are_asked_about_each_time(page):
    for path in ("/assets/index-abc123.js", "/assets/index-def456.css", "/assets/archivo-xyz789.woff2"):
        assert (await get(stub_app(), path)).headers["cache-control"] == "public, max-age=31536000, immutable", path
    for path in ("/", "/index.html", "/favicon.svg", "/pipeline"):
        assert (await get(stub_app(), path)).headers["cache-control"] == "no-cache", path


async def test_head_gets_the_headers_of_get_without_the_body(page):
    for path in ("/", "/assets/index-abc123.js", "/pipeline"):
        got, head = await get(stub_app(), path), await get(stub_app(), path, "HEAD")
        assert head.status_code == 200 and head.content == b"", path
        assert head.headers["content-length"] == got.headers["content-length"] == str(len(got.content))
        assert head.headers["content-type"] == got.headers["content-type"]
        assert head.headers["cache-control"] == got.headers["cache-control"]
        assert_secure(head)


async def test_a_browser_that_holds_the_file_is_told_it_is_unchanged_and_keeps_its_cache_rule(page):
    for path in ("/", "/pipeline", "/favicon.svg", "/assets/index-abc123.js"):
        first = await get(stub_app(), path)
        for name, value in (("If-None-Match", first.headers["etag"]),
                            ("If-None-Match", f"W/{first.headers['etag']}"), ("If-None-Match", "*"),
                            ("If-Modified-Since", first.headers["last-modified"])):
            again = await get(stub_app(), path, headers={name: value})
            assert again.status_code == 304 and again.content == b"", (path, name)
            assert again.headers["cache-control"] == first.headers["cache-control"], path
            assert again.headers["etag"] == first.headers["etag"]
            assert_secure(again)
        stale = await get(stub_app(), path, headers={"If-None-Match": '"some-other-version"'})
        assert stale.status_code == 200 and stale.content == first.content, path
        older = await get(stub_app(), path, headers={"If-Modified-Since": "Mon, 01 Jan 1990 00:00:00 GMT"})
        assert older.status_code == 200, path


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_the_page_is_read_never_changed_so_only_get_and_head_are_answered(page, method):
    for path in ("/", "/pipeline", "/assets/index-abc123.js", "/assets/missing.js"):
        resp = await get(stub_app(), path, method)
        assert resp.status_code == 405 and resp.headers["allow"] == "GET, HEAD", (method, path)
        assert_secure(resp)


HOSTILE = [
    "/../secret.txt", "/assets/../secret.txt", "/assets/../../secret.txt", "/assets/./../secret.txt",
    "/..\\secret.txt", "/assets/..\\secret.txt", "/assets/..\\..\\secret.txt", "/assets/..%5csecret.txt",
    "//secret.txt", "/assets//..//secret.txt", "/%2e%2e/secret.txt",
    "/C:/Windows/win.ini", "/C:\\Windows\\win.ini", "/assets/C:/Windows/win.ini", "/assets/C:secret.txt",
    "/assets/NUL", "/assets/CON", "/assets/nul.js", "/assets/COM1", "/assets/LPT1.txt", "/assets/aux",
    "/assets/index-abc123.js::$DATA", "/assets/index-abc123.js:stream", "/assets/index-abc123.js.",
    "/assets/index-abc123.js ", "/assets/INDEX-ABC123.JS", "/assets/Index-abc123.js", "/ASSETS/index-abc123.js",
    "/assets/index-abc123.js/", "/assets/index-abc123.js/.", "/assets/index-abc123.js\x00.png", "/assets/\x00",
    "/assets/index-abc123*", "/assets/index-abc12?.js", "/assets/INDEX~1.JS", "/assets/index-abc123.js\\",
    "/\\\\localhost\\c$\\secret.txt", "/assets/\\\\localhost\\c$\\secret.txt",
]


@pytest.mark.parametrize("path", HOSTILE, ids=ascii)
async def test_nothing_outside_the_page_folder_is_ever_served_whatever_the_path_says(page, path):
    # The paths of an attacker, of a Windows quirk (a drive, a device name, a data stream, a short name, a trailing dot
    # or space, a different case, a wildcard) and of a typo. None reaches a file the build did not write by that name.
    for app in (stub_app(), real_main.app):
        status, _, body = await call(app, path)
        assert body != DECOY, "the decoy beside the page was served"
        assert status in (200, 404), f"{path!r} was answered {status}"
        if path.startswith("/assets/"):
            assert status == 404 and body != SHELL, f"{path!r}: a miss under assets is a 404"
        else:
            assert status == 200 and body == SHELL, f"{path!r}: any other miss is the shell"


async def test_a_name_too_long_for_a_file_system_is_a_miss_like_any_other(page):
    for path in ("/assets/" + "a" * 5000, "/" + "a" * 5000, "/" + "a/" * 3000):
        status, _, body = await call(stub_app(), path)
        assert status == (404 if path.startswith("/assets/") else 200), path[:20]


async def test_a_link_or_a_folder_in_the_way_is_not_followed(page, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.js").write_bytes(DECOY)
    try:
        (page / "assets" / "linked.js").symlink_to(outside / "x.js")
        (page / "assets" / "linked-dir").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this system does not let the tests make links")
    for path in ("/assets/linked.js", "/assets/linked-dir/x.js"):
        status, _, body = await call(stub_app(), path)
        assert status == 404 and body != DECOY, path


async def test_a_folder_is_not_a_page_and_is_not_listed(page):
    status, _, body = await call(stub_app(), "/assets/")
    assert status == 404 and b"index-abc123" not in body
    (page / "assets" / "folder.d").mkdir()
    status, _, body = await call(stub_app(), "/assets/folder.d")
    assert status == 404


async def test_without_a_built_page_the_app_answers_its_api_and_says_not_found_to_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "PAGE_DIR", tmp_path / "no-such-folder")
    stub = stub_app()
    assert (await get(stub, "/api/v1/ping")).json() == {"from": "the api"}
    for path in ("/", "/pipeline", "/assets/index-abc123.js", "/favicon.svg"):
        resp = await get(stub, path)
        assert resp.status_code == 404 and kind(resp) == "application/json", path
        assert_secure(resp)


# --- what stays the API's ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api", "/api/", "/api/v1/nothing", "/api/v1/ping/extra", "/api/nothing/at/all"])
async def test_what_no_route_took_under_api_is_the_apis_own_404_and_never_the_shell(page, path):
    resp = await get(stub_app(), path)
    assert resp.status_code == 404 and resp.json() == {"detail": "Not Found"}, path


async def test_a_wrong_method_on_an_api_route_is_still_a_405_the_page_does_not_take_it(page):
    resp = await get(stub_app(), "/api/v1/ping", "POST")
    assert resp.status_code == 405 and "GET" in resp.headers["allow"]
    assert (await get(stub_app(), "/health", "DELETE")).status_code == 405
    assert (await get(stub_app(), "/export.xlsx", "POST")).status_code == 405
    assert (await get(stub_app(), "/api/v1/nothing", "POST")).status_code == 404


async def test_health_the_export_and_the_door_are_the_apis_and_a_name_beside_them_is_the_pages(page):
    for path in ("/health", "/export.xlsx", "/api/v1/ping"):
        resp = await get(stub_app(), path)
        assert resp.status_code == 200 and kind(resp) == "application/json", path
    assert (await get(stub_app(), "/mcp-browser/")).status_code == 401
    for path in ("/health/extra", "/healthy", "/export.xlsx/extra", "/export", "/mcp-browserx", "/apiary"):
        resp = await get(stub_app(), path)
        assert resp.status_code == 200 and resp.content == SHELL, path


async def test_the_apis_own_documents_are_not_published_so_those_addresses_are_the_shell_as_with_nginx(page):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert (await get(stub_app(), path)).content == SHELL, path
        assert (await get(real_main.app, path)).content == SHELL, path


# --- the headers ---------------------------------------------------------------------------------------------------


def csp(value: str) -> dict[str, list[str]]:
    parts = (part.split() for part in value.split(";") if part.strip())
    return {words[0]: words[1:] for words in parts}


def test_the_policy_lets_the_page_load_nothing_from_another_site_and_run_no_script_of_its_own():
    assert [name for name, _ in web.SECURITY_HEADERS] == list(SECURITY_NAMES)
    headers = dict(web.SECURITY_HEADERS)
    policy = csp(headers["Content-Security-Policy"])
    assert policy["default-src"] == ["'self'"]
    assert policy["script-src"] == ["'self'"], "no inline script, no eval, no other site's script"
    assert policy["connect-src"] == ["'self'"]
    assert policy["object-src"] == ["'none'"] and policy["frame-ancestors"] == ["'none'"]
    assert policy["base-uri"] == ["'none'"] and policy["form-action"] == ["'self'"]
    assert set(policy["img-src"]) == {"'self'", "data:"}
    assert not [src for sources in policy.values() for src in sources if src.startswith(("http:", "https:", "*"))]
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Referrer-Policy"] == "no-referrer"


EVERY_KIND_OF_ANSWER = [
    ("GET", "/"), ("GET", "/pipeline"), ("GET", "/favicon.svg"), ("GET", "/assets/index-abc123.js"),
    ("GET", "/assets/missing.js"), ("HEAD", "/"), ("POST", "/pipeline"), ("GET", "/api/v1/ping"),
    ("GET", "/api/v1/nothing"), ("POST", "/api/v1/ping"), ("GET", "/api"), ("GET", "/health"),
    ("GET", "/export.xlsx"), ("GET", "/mcp-browser/"),
]


@pytest.mark.parametrize(("method", "path"), EVERY_KIND_OF_ANSWER)
async def test_every_answer_carries_each_security_header_once(page, method, path):
    assert_secure(await get(stub_app(), path, method), f"{method} {path}")


async def test_the_apis_answers_are_never_kept_by_a_browser_or_a_proxy_whatever_their_status(page):
    for method, path in (("GET", "/api/v1/ping"), ("GET", "/api/v1/nothing"), ("POST", "/api/v1/ping")):
        resp = await get(stub_app(), path, method)
        assert resp.headers.get_list("cache-control") == ["no-store"], f"{method} {path}: {resp.status_code}"
    for path in ("/health", "/export.xlsx", "/mcp-browser/", "/healthy"):
        assert "no-store" not in (await get(stub_app(), path)).headers.get("cache-control", ""), path


async def test_a_cache_rule_the_route_set_for_itself_is_replaced_not_added_to(page):
    stub = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @stub.get("/api/v1/kept")
    async def kept() -> JSONResponse:
        return JSONResponse({}, headers={"Cache-Control": "max-age=600", "X-Content-Type-Options": "sniff"})

    web.install(stub)
    resp = await get(stub, "/api/v1/kept")
    assert resp.headers.get_list("cache-control") == ["no-store"]
    assert resp.headers.get_list("x-content-type-options") == ["nosniff"]


# --- the real app ---------------------------------------------------------------------------------------------------


@pytest.fixture
def real(page, monkeypatch):
    """The real app, its database out of reach (a health check says so, as it does when the database is down)."""
    def unreachable():
        raise RuntimeError("no database in this test")

    monkeypatch.setattr(db, "session_factory", unreachable)
    monkeypatch.setattr(db, "lifeline_session_factory", unreachable)  # /health asks on the lifeline pool
    return real_main.app


async def test_the_real_app_serves_the_page_and_keeps_the_apis_paths(real):
    for path in ("/", "/pipeline", "/agents/how", "/docs"):
        resp = await get(real, path)
        assert resp.status_code == 200 and resp.content == SHELL and resp.headers["cache-control"] == "no-cache", path
        assert_secure(resp)
    asset = await get(real, "/assets/index-abc123.js")
    assert asset.content == FILES["assets/index-abc123.js"] and "immutable" in asset.headers["cache-control"]
    assert (await get(real, "/assets/missing.js")).status_code == 404
    nothing = await get(real, "/api/v1/nothing")
    assert nothing.status_code == 404 and nothing.json() == {"detail": "Not Found"}
    assert nothing.headers.get_list("cache-control") == ["no-store"]
    assert_secure(nothing)
    health = await get(real, "/health")
    assert health.status_code == 503 and health.json()["status"] == "degraded", "the real route answered, not the page"
    assert "cache-control" not in health.headers
    assert_secure(health)
    assert (await get(real, "/api/v1/me", "DELETE")).status_code == 405, "a wrong method is still the API's 405"


async def test_the_guards_refusals_carry_the_security_headers_and_the_page_itself_is_guarded(real):
    # A name that points at this machine from another site must not get the page either (DNS rebinding).
    for path in ("/", "/pipeline", "/assets/index-abc123.js", "/api/v1/me"):
        resp = await get(real, path, host="attacker.example")
        assert resp.status_code == 421 and resp.content != SHELL, path
        assert_secure(resp)
    resp = await get(real, "/api/v1/notifications/read-all", "POST", headers={"Origin": "http://attacker.example"})
    assert resp.status_code == 403
    assert_secure(resp)
    for host in ("localhost:8430", "127.0.0.1:8430", "[::1]:8430"):
        assert (await get(real, "/", host=host)).content == SHELL, host


async def test_the_browser_door_is_on_the_same_port_and_still_wants_its_token(real):
    resp = await get(real, "/mcp-browser/", "POST", json={})
    assert resp.status_code == 401 and resp.headers["www-authenticate"].startswith("Bearer")
    assert_secure(resp)


# --- the page loads from the package, with no Node -----------------------------------------------------------------


def test_the_page_lives_inside_the_backend_package():
    assert REAL_PAGE_DIR == db.BACKEND_DIR / "app" / "page"


def test_the_frontends_package_build_writes_the_folder_the_app_serves():
    package = FRONTEND / "package.json"
    if not package.is_file():
        pytest.skip("the frontend's package.json is not here (the backend image is given it; a checkout has it)")
    scripts = json.loads(package.read_text(encoding="utf-8"))["scripts"]
    target = re.search(r"--outDir\s+(\S+)", scripts["build:package"])
    assert target, "build:package names no --outDir"
    # Where the app looks, inside the backend folder ("app/page"), reached from the frontend folder as the repository
    # lays them out beside each other. (In the test image the two sit elsewhere, so the path is compared as written.)
    assert target.group(1) == f"../backend/{REAL_PAGE_DIR.relative_to(db.BACKEND_DIR).as_posix()}"
    assert "--emptyOutDir" in scripts["build:package"], "an old build's files would stay in the package"
    assert "outDir" not in scripts["build"], "`build` still writes dist/; only `build:package` fills the package"


# --- the browser jobs' window's first tab is a page of the app (the owner, 2026-11-05) -----------------------------
#
# The browser jobs' window used to open a data: URL with one line of unstyled text as its first tab. It opens a page the
# app serves instead (app/scout/own-chrome.mjs `anchorUrl`: the app's address and `/browser-window.html`): a second HTML
# file of the frontend's build, with no script and no call to the API, in the app's own look. The tests here read the
# frontend's source, as `test_the_frontends_package_build_writes_the_folder_the_app_serves` does, and are skipped where
# it is not (the backend image).

WINDOW_PAGE = FRONTEND / "browser-window.html"
needs_frontend_source = pytest.mark.skipif(
    not (FRONTEND / "index.html").is_file(), reason="the frontend's source is not here (a checkout has it)")


@pytest.fixture
def window_page(page) -> bytes:
    """The window's page as the frontend's source holds it, laid beside the made-up shell in the page folder, where a
    build writes it (its stylesheet named by hash, which the source cannot know)."""
    source = WINDOW_PAGE.read_bytes()
    (page / "browser-window.html").write_bytes(source)
    return source


@needs_frontend_source
async def test_the_browser_jobs_window_page_is_served_with_no_sign_in_titled_resumebot_and_is_not_the_shell(
        window_page, real):
    resp = await get(real, "/browser-window.html")  # no cookie, no token: a browser on this machine's own address
    assert resp.status_code == 200
    assert resp.content == window_page and resp.content != SHELL, "the page itself, not the shell a missing name gives"
    assert resp.headers["content-type"].split(";")[0] == "text/html"
    assert resp.headers["cache-control"] == "no-cache", "asked again on each load, never a release behind"
    assert resp.text.count("<title>resumebot</title>") == 1
    assert_secure(resp)
    head = await get(real, "/browser-window.html", "HEAD")
    assert head.status_code == 200 and head.content == b""
    # As guarded as the rest of the page: a name that points here from another site does not get it either.
    assert (await get(real, "/browser-window.html", host="attacker.example")).status_code == 421


@needs_frontend_source
def test_the_browser_jobs_window_page_has_no_script_and_no_call_so_it_loads_at_once_and_outlives_a_restart():
    html = WINDOW_PAGE.read_text(encoding="utf-8")
    assert not re.search(r"<script", html, re.IGNORECASE), "nothing runs in it"
    assert "/api" not in html and "fetch(" not in html and "XMLHttpRequest" not in html, "nothing is asked of the app"
    assert "http://" not in html and "https://" not in html, "no address of anyone's, and nothing from another site"
    assert re.search(r'<a\s[^>]*href="/"', html), "a link back to the app, by no address at all (the app's own)"
    assert re.search(r'<meta\s+name="color-scheme"\s+content="light dark"', html), "light and dark, as the app's page"
    words = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    for said in ("browser jobs", "tabs of this window", "closes its own tabs", "Leave this window open",
                 "Back to resumebot"):
        assert said in words, said


@needs_frontend_source
def test_the_browser_jobs_window_page_is_in_the_apps_own_look_its_tokens_and_its_base():
    html = WINDOW_PAGE.read_text(encoding="utf-8")
    sheets = re.findall(r'<link\s[^>]*rel="stylesheet"[^>]*href="(/src/[^"]+\.css)"', html)
    assert sheets, "it names a stylesheet of the frontend's source, which the build hashes and rewrites"
    css = "\n".join((FRONTEND / name.lstrip("/")).read_text(encoding="utf-8") for name in sheets)
    assert "focus/tokens.css" in css and "focus/base.css" in css, "the Focus Inbox's tokens (day and night) and base"
    assert "archivo" in css.lower(), "its own face"
    rules = FRONTEND / "src" / "styles" / "focus" / "browser-window.css"
    assert "./styles/focus/browser-window.css" in css, "and its few rules"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rules.read_text(encoding="utf-8")), "no colour of its own"


@needs_frontend_source
def test_the_frontends_build_makes_the_window_page_a_second_page_beside_the_shell():
    config = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")
    assert re.search(r"input\s*:\s*\{[^}]*index\.html[^}]*browser-window\.html", config, re.DOTALL), (
        "both pages are named, or the build writes only the shell and the window opens the shell")


BUILT = REAL_PAGE_DIR / "index.html"
needs_build = pytest.mark.skipif(
    not BUILT.is_file(), reason="the page is not built into this checkout (npm run build:package, in app/frontend)")


async def walk_the_page(root: Path, fetch) -> int:
    """Load the page as a browser does, from `fetch(path) -> (status, headers, body)`: the shell, everything it names,
    then what the stylesheets name (the fonts). Every file must come from `root`, with a type a browser accepts for it
    and the cache rule of its place. Returns how many files were loaded."""
    status, headers, body = await fetch("/")
    assert status == 200 and body == (root / "index.html").read_bytes()
    shell = body.decode()
    assert not re.search(r"<script(?![^>]*\bsrc=)", shell), "an inline script: the policy would refuse it"
    assert "http://" not in shell and "https://" not in shell, "something from another site"
    wanted = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml", "woff2": "font/woff2"}
    names = re.findall(r'(?:src|href)="(/[^"]*)"', shell)
    assert any(name.startswith("/assets/") for name in names)
    loaded, sheets = 1, []
    for name in names:
        status, headers, body = await fetch(name)
        assert status == 200 and body == (root / name.lstrip("/")).read_bytes(), name
        expected = wanted.get(name.rsplit(".", 1)[-1])
        assert expected is None or headers["content-type"].split(";")[0] == expected, (name, headers["content-type"])
        assert headers["cache-control"] == ("public, max-age=31536000, immutable" if name.startswith("/assets/")
                                            else "no-cache"), name
        loaded += 1
        if name.endswith(".css"):
            sheets.append((name, body.decode()))
    for sheet, css in sheets:
        for url in re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", css):
            if url.startswith("data:"):
                continue
            target = urljoin(sheet, re.split(r"[?#]", url)[0])
            status, headers, body = await fetch(target)
            assert status == 200 and body == (root / target.lstrip("/")).read_bytes(), (sheet, url)
            suffix = target.rsplit(".", 1)[-1]
            assert suffix not in wanted or headers["content-type"].split(";")[0] == wanted[suffix], target
            loaded += 1
    return loaded


@needs_build
async def test_the_built_page_loads_from_the_package(monkeypatch):
    monkeypatch.setattr(web, "PAGE_DIR", REAL_PAGE_DIR)

    async def fetch(path: str):
        resp = await get(stub_app(), path)
        assert_secure(resp)
        return resp.status_code, resp.headers, resp.content

    assert await walk_the_page(REAL_PAGE_DIR, fetch) >= 3

@needs_build
async def test_the_built_browser_window_page_is_a_page_of_the_package_with_its_stylesheet_and_no_script(monkeypatch):
    monkeypatch.setattr(web, "PAGE_DIR", REAL_PAGE_DIR)
    built = REAL_PAGE_DIR / "browser-window.html"
    assert built.is_file(), "the build wrote no browser-window.html: the bridge's first tab would open the shell"
    resp = await get(stub_app(), "/browser-window.html")
    assert resp.status_code == 200 and resp.content == built.read_bytes()
    assert_secure(resp)
    html = resp.text
    assert resp.headers["cache-control"] == "no-cache" and "<title>resumebot</title>" in html
    assert not re.search(r"<script", html, re.IGNORECASE)
    sheets = re.findall(r'<link\s[^>]*rel="stylesheet"[^>]*href="(/assets/[^"]+\.css)"', html)
    assert sheets, "the build named the page's stylesheet under /assets/"
    for name in sheets:
        sheet = await get(stub_app(), name)
        assert sheet.status_code == 200 and sheet.headers["content-type"].split(";")[0] == "text/css", name
        assert sheet.headers["cache-control"] == "public, max-age=31536000, immutable", name
        assert "--fi-ground" in sheet.text, "the app's own tokens are in it"
        for url in re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", sheet.text):
            if url.startswith("data:"):
                continue
            font = await get(stub_app(), urljoin(name, re.split(r"[?#]", url)[0]))
            assert font.status_code == 200, (name, url)


#: A server as the app runs: uvicorn on h11 and the standard loop (no uvloop; the one-process app is run plain), a free
#: loopback port, the page layer on a small API. It has no Node on its path, and says so by failing if it has; it ends
#: when its parent closes its input, so a test that dies leaves nothing running.
SERVE = '''
import os
import shutil
import socket
import sys
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from app import web

assert shutil.which("node") is None and shutil.which("npm") is None, "Node is on this server's path"
web.PAGE_DIR = Path(sys.argv[1])
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api/v1/ping")
async def ping() -> dict:
    return {"from": "the api"}


web.install(app)
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen()
threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()
Path(sys.argv[2]).write_text(str(sock.getsockname()[1]))
uvicorn.Server(uvicorn.Config(app, http="h11", loop="asyncio", log_level="warning")).run(sockets=[sock])
'''


@contextlib.contextmanager
def serving(root: Path, tmp_path: Path):
    """A real server on a free loopback port serving `root`, in a process whose PATH holds nothing (so no Node)."""
    empty = tmp_path / "empty-path"
    empty.mkdir(exist_ok=True)
    script, port_file, log = tmp_path / "serve.py", tmp_path / "port.txt", tmp_path / "server.log"
    script.write_text(SERVE, encoding="utf-8")
    env = {"PATH": str(empty), "PYTHONPATH": str(db.BACKEND_DIR), "PYTHONDONTWRITEBYTECODE": "1"}
    env |= {name: os.environ[name] for name in ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP") if name in os.environ}
    with log.open("wb") as out:
        proc = subprocess.Popen([sys.executable, str(script), str(root), str(port_file)], cwd=db.BACKEND_DIR, env=env,
                                stdin=subprocess.PIPE, stdout=out, stderr=out)
        try:
            deadline = time.monotonic() + 60
            while not port_file.exists():
                assert proc.poll() is None and time.monotonic() < deadline, log.read_text(errors="replace")
                time.sleep(0.05)
            yield int(port_file.read_text())
        finally:
            assert proc.stdin is not None
            proc.stdin.close()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


async def raw_get(port: int, target: str) -> tuple[int, bytes]:
    """A GET with the request target exactly as written (a client would tidy it), over a real socket."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {target} HTTP/1.1\r\nHost: localhost:{port}\r\nConnection: close\r\n\r\n".encode("latin-1"))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 15)
    finally:
        writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


async def test_a_real_server_with_no_node_on_its_path_serves_the_page_to_a_client(page, tmp_path):
    with serving(page, tmp_path) as port:
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
            async def fetch(path: str):
                resp = await c.get(path)
                assert_secure(resp)
                return resp.status_code, resp.headers, resp.content

            assert await walk_the_page(page, fetch) == 5
            assert (await c.get("/pipeline")).content == SHELL
            missing = await c.get("/assets/missing.js")
            assert missing.status_code == 404 and "cache-control" not in missing.headers
            head = await c.head("/")
            assert head.status_code == 200 and head.content == b"" and int(head.headers["content-length"]) == len(SHELL)
            api = await c.get("/api/v1/ping")
            assert api.json() == {"from": "the api"} and api.headers["cache-control"] == "no-store"
            assert_secure(api)
            first = await c.get("/")
            assert (await c.get("/", headers={"If-None-Match": first.headers["etag"]})).status_code == 304
            ranged = await c.get("/assets/index-abc123.js", headers={"Range": "bytes=0-7"})
            assert ranged.status_code == 206 and ranged.content == FILES["assets/index-abc123.js"][:8]


async def test_a_real_server_is_not_led_outside_the_page_by_a_request_target_a_client_would_tidy(page, tmp_path):
    targets = [
        "/../secret.txt", "/assets/../secret.txt", "/assets/..%5csecret.txt", "/assets/..%5c..%5csecret.txt",
        "/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt", "/assets/..%2fsecret.txt", "/..%2fsecret.txt",
        "/C:/Windows/win.ini", "/assets/C%3a/Windows/win.ini", "/assets/NUL", "/assets/CON", "/assets/nul.js",
        "/assets/index-abc123.js::$DATA", "/assets/index-abc123.js%00.png", "/assets/%00", "/assets/INDEX-ABC123.JS",
        "/assets/index-abc123.js.", "/assets/index-abc123.js%20", "/assets/INDEX~1.JS", "/assets/" + "a" * 3000,
    ]
    with serving(page, tmp_path) as port:
        for target in targets:
            status, body = await raw_get(port, target)
            assert body != DECOY and status in (200, 400, 404), f"{target[:40]} was answered {status}"
            if target.startswith("/assets/") and status != 400:
                assert status == 404 and body != SHELL, target[:40]


@needs_build
async def test_a_real_server_with_no_node_on_its_path_serves_the_built_page_from_the_package(tmp_path):
    with serving(REAL_PAGE_DIR, tmp_path) as port:
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
            async def fetch(path: str):
                resp = await c.get(path)
                assert_secure(resp)
                return resp.status_code, resp.headers, resp.content

            assert await walk_the_page(REAL_PAGE_DIR, fetch) >= 3
            assert (await c.get("/some/screen/that/is/no/file")).content == BUILT.read_bytes()
