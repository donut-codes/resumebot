"""The page, and the headers and cache rules it travels with, all served by the app itself.

In the one-process app the page, the API (`/api/v1`), `/health`, the Excel export and both MCP doors answer on one
port, so nothing sits in front of them. Four things come here (tests/core/test_web.py holds each of them):

- **The page.** The built files of the frontend answer every request that no route took. They are written into `page/`
  beside this file by `npm run build:package` (in app/frontend), so they travel inside the package: Node builds them
  and is never needed to run them. A path that names a file of the page is that file. A path under `/assets/` that
  does not is a 404, never the shell: a script that moved must fail where it is loaded, not arrive as a page a
  browser then tries to run. Any other path is the shell, `index.html`, so a reload or a pasted address of a screen
  opens that screen. Only GET and HEAD are answered.
- **Cache rules.** The API's answers are `no-store` (they are the owner's own data). A file under `/assets/` is named
  by its content hash, so it is `immutable`. Everything else of the page, the shell included, is `no-cache`: a
  browser asks again on each load and never goes on running the previous release. The last two go on successful
  answers only, so a 404 is not kept for a year.
- **Security headers.** The content security policy, `nosniff` and `no-referrer`, on every answer: errors and the
  Host and Origin guard's refusals included, which is why `install` puts this layer outside the guard.
- **What is not the page's.** `/api` and everything under it that no route took is the API's own 404, never the
  shell. A wrong method on a route is still its 405. `/health`, `/export.xlsx` and both doors are routes of the app.

Not done: compression (the page is on this machine), a body-size limit in front of the app (it
refuses an oversized resume in words) and read timeouts (the doors stream, and there is no proxy to cut them).

The files are found by name, not by path. A request names a file by being compared with what the folder lists, so what
a client sends is never turned into a path: `..`, a drive, a backslash, a device name (`NUL`), a data stream
(`::$DATA`), a short name, a trailing dot or space and a different case all fail to equal a name the build wrote. A
link or a folder in the way is not followed. Types come from the table below and not from the operating system, whose
table (Windows reads its registry) may call a script `text/plain`, which a browser refuses as a module.
"""

import logging
import os
import re
from email.utils import parsedate
from pathlib import Path

import anyio.to_thread
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

#: Where the built page is: `npm run build:package` writes it here (a git-ignored folder in the package). Read when a
#: request comes, so a rebuilt page is served without a restart.
PAGE_DIR = Path(__file__).resolve().parent / "page"

#: Sent with every answer. The policy matches what the page loads: its own script and stylesheet (no inline
#: script), inline style attributes (the process view and the React style props), data: images (the select chevron and
#: tick masks in the CSS; the favicon is its own file), its own self-hosted font, and fetches to /api on its own origin.
#: Nothing is loaded from another site and the page may not be framed.
SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
     "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)
_SECURITY_RAW = tuple((name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in SECURITY_HEADERS)

NO_STORE = "no-store"
NO_CACHE = "no-cache"
IMMUTABLE = "public, max-age=31536000, immutable"

#: The type of each kind of file a build of the page holds. Anything else is plain bytes (`application/octet-stream`),
#: which with `nosniff` a browser will not guess at.
CONTENT_TYPES = {
    ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
    ".map": "application/json", ".txt": "text/plain", ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".avif": "image/avif", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".wasm": "application/wasm",
}


class WebHeaders:
    """ASGI middleware: the security headers on every answer, and `no-store` on the API's. A header the route set
    itself is replaced, so each is sent once."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        no_store = scope["path"].startswith("/api/")
        replaced = {name for name, _ in _SECURITY_RAW} | ({b"cache-control"} if no_store else set())

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(n, v) for n, v in message.get("headers", []) if n.lower() not in replaced]
                headers.extend(_SECURITY_RAW)
                if no_store:
                    headers.append((b"cache-control", NO_STORE.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def find(root: Path, parts: list[str]) -> tuple[str, os.stat_result] | None:
    """The regular file below `root` that `parts` name, as (its path, its stat), or None. Each part must be exactly
    the name of an entry the folder lists (the page's files are found by name, see the module's note); a link is not
    followed, and a folder is not a file."""
    here = str(root)
    try:
        for index, part in enumerate(parts):
            with os.scandir(here) as entries:
                entry = next((e for e in entries if e.name == part), None)
            if entry is None:
                return None
            if index == len(parts) - 1:
                if not entry.is_file(follow_symlinks=False):
                    return None
                return entry.path, entry.stat(follow_symlinks=False)
            if not entry.is_dir(follow_symlinks=False):
                return None
            here = entry.path
    except OSError:  # no such folder, not a folder, no right to list it, a file gone since it was listed
        return None
    return None


def not_modified(response: Headers, request: Headers) -> bool:
    """Whether the browser already holds this version of the file: its If-None-Match names the file's ETag (or is `*`),
    or, when it sends none, its If-Modified-Since is not older than the file."""
    if tags := request.get("if-none-match"):
        return tags.strip() == "*" or response["etag"] in [tag.strip().removeprefix("W/") for tag in tags.split(",")]
    since, changed = parsedate(request.get("if-modified-since", "")), parsedate(response["last-modified"])
    return since is not None and changed is not None and since >= changed


def answer(root: Path, path: str, request: Headers) -> Response | None:
    """The page's answer to a GET or HEAD for `path` (a path with its slashes merged): the file
    it names, else the shell unless the path is under `/assets/`, else None (a 404). A file under `/assets/` is
    `immutable`, any other `no-cache`; the type comes from `CONTENT_TYPES`."""
    in_assets = path.startswith("/assets/")
    found = None
    if path == "/":
        found = find(root, ["index.html"])
    elif not path.endswith("/"):
        found = find(root, path[1:].split("/"))
    if found is None and not in_assets:
        found = find(root, ["index.html"])
    if found is None:
        return None
    full, stat_result = found
    response = FileResponse(
        full, stat_result=stat_result,
        media_type=CONTENT_TYPES.get(Path(full).suffix.lower(), "application/octet-stream"),
        headers={"cache-control": IMMUTABLE if in_assets else NO_CACHE})
    return NotModifiedResponse(response.headers) if not_modified(response.headers, request) else response


class Page:
    """ASGI app for what no route took (the router's default): a file of the page, the shell, or `otherwise`, the
    router's own answer (a 404). It never answers for `/api`: what is under it that no route took is the API's own
    404."""

    def __init__(self, otherwise: ASGIApp) -> None:
        self.otherwise = otherwise

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.otherwise(scope, receive, send)
            return
        path = "/" + re.sub(r"/{2,}", "/", scope["path"]).lstrip("/")
        if path == "/api" or path.startswith("/api/"):
            await self.otherwise(scope, receive, send)
            return
        if scope["method"] not in ("GET", "HEAD"):
            refusal = JSONResponse({"detail": "Method Not Allowed"}, status_code=405, headers={"Allow": "GET, HEAD"})
            await refusal(scope, receive, send)
            return
        response = await anyio.to_thread.run_sync(answer, PAGE_DIR, path, Headers(scope=scope))
        if response is None:
            await self.otherwise(scope, receive, send)
        else:
            await response(scope, receive, send)


def install(app: FastAPI) -> None:
    """Put the page's layer on the app: the headers outside everything (call it after the guard is added: the last
    middleware added is the outermost, so the guard's refusals carry the headers too), and the page as the router's
    answer to a path no route took. Routes are tried first, so the API keeps its paths, its 405s and its redirects of
    a trailing slash.

    It takes the place of a mount of the page at `/`: a mount that matches every path would take a wrong method on a
    route away from the 405 the route would give."""
    app.add_middleware(WebHeaders)
    app.router.default = Page(app.router.default)


def log_page() -> None:
    """One line at the start: where the page is served from, or that this install has none and answers its API only."""
    if find(PAGE_DIR, ["index.html"]):
        log.info("serving the page from %s", PAGE_DIR)
    else:
        log.info("no page is built into %s, so this app answers its API and its doors only "
                 "(npm run build:package, in app/frontend, builds it)", PAGE_DIR)
