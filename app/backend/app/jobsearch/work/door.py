"""What the app's two MCP doors share: the scout's (`doors/scout.py`) and the owner's browser's (`browser/mcp.py`).

A door is an MCP server (the SDK's `MCPServer`) behind a bearer token, over a stateless streamable-HTTP mount. Its tools
are written as plain functions of a database session and the owner, and `Door.tool` registers one under its name and
description:

    @door.tool("jobs.report", "Report roles you found, ...")
    async def jobs_report(session, owner, roles: list[dict[str, Any]], done: bool = False):
        return await tools.report_reply(session, owner, roles, done)

What the SDK shows an agent (the name, the description, the arguments and their defaults, the result's shape) is read
from the function as written, with `session` and `owner` left out and the SDK's `ctx` put first, so a tool is exactly
the one an agent sees. `Door.call` does what is the door's for every tool: the token, the session and its commit, and
the refusal. A refusal is an ordinary result (`{"ok": false, "reason": ..., "how_to_fix": ...}`), so a client never
counts it as the server failing: a `DomainError` is the app's own refusal in plain words, anything else is a bug, which
is logged with its traceback and answered as the app's fault, never as a problem with what the agent sent.
"""

import hmac
import inspect
import json
import logging
import os
import re
import time
import typing
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ListToolsResult

from app import db, notice, program
from app.agents.security import hash_token
from app.config import public_url_hosts
from app.errors import Busy, DomainError, TooLarge
from app.planengine.budget import REPLY_CEILING_CHARS, RESULT_SIZE_META, payload_chars
from app.settings import VERSION, data_dir, settings

log = logging.getLogger(__name__)

#: What a tool call says when the app itself failed (its cause is in the log).
UNEXPECTED = "the app failed on its side and logged why; this is not a problem with what you sent"


class ReplyTooLarge(TooLarge):
    """A reply that passes `REPLY_CEILING_CHARS`. It is refused, with the size, and never cut: a call that changed
    something is rolled back with it."""

    how_to_fix = "tell the owner: the app built a reply too large to send whole; nothing was cut or saved"

    def __init__(self, name: str, size: int) -> None:
        super().__init__(
            f"{name} would send {size:,} characters, over the {REPLY_CEILING_CHARS:,} an agent is sent at most; "
            "it was not sent and nothing was cut")


def refusal(reason: str, how_to_fix: str | None = None) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "how_to_fix": how_to_fix or "check the arguments against this tool's schema"}


def refusal_reason(header: str | None, path: Path) -> str | None:
    """Why the Authorization header does not carry the token the file at `path` holds, in words fit for a log (never the
    token or any part of it), or None when it does. The file is read as the clients read it (`utf-8-sig`: a byte order
    mark that Windows PowerShell 5 or Notepad writes is dropped); one that cannot be read or decoded passes no one."""
    if not header:
        return "no Authorization header"
    if not header.lower().startswith("bearer "):
        return "not a bearer header"
    given = header.split(" ", 1)[1].strip()
    try:
        expected = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError) as exc:  # ValueError covers UnicodeDecodeError (a UTF-16 file)
        return f"token file unreadable ({path}: {type(exc).__name__})"
    if not expected:
        return "token file empty"
    if not given or not hmac.compare_digest(hash_token(given), hash_token(expected)):
        return "token does not match"
    return None


def token_matches(header: str | None, path: Path) -> bool:
    """True when the Authorization header carries the token the file at `path` holds. Unreadable file: none passes."""
    return refusal_reason(header, path) is None


#: A refusal reason is logged once a minute for each door and reason, on this clock (a test replaces it).
clock: Callable[[], float] = time.monotonic
_REFUSAL_EVERY = 60.0
_told: dict[tuple[str, str], float] = {}


def log_refusal(logger: logging.Logger, label: str, tool: str, reason: str) -> None:
    """One WARNING for a call refused for its token: the door, the tool and the reason (never the token), at most once
    a minute for each door and reason."""
    now, key = clock(), (label, reason)
    last = _told.get(key)
    if last is not None and now - last < _REFUSAL_EVERY:
        return
    _told[key] = now
    logger.warning("%s: %s refused, %s", label, tool, reason)


#: The token file each door mount is behind: (mount path, whose token, the setting method that names its file).
_DOOR_TOKENS = (("/mcp", "agent", "token_path"), ("/mcp-browser", "browser", "browser_token_path"),
                ("/mcp-owner", "owner", "owner_token_path"))


def _file_facts(path: Path) -> str:
    """Whether the file exists, its size and its modified time, never what it holds."""
    try:
        stat = path.stat()
    except OSError:
        return "exists=no"
    when = datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"exists=yes size={stat.st_size} modified={when}"


def start_line(role: str) -> str:
    """The one line a program logs as it starts: the version, the program, its process, its port when the command line
    names it, the data folder and each door token file it mounts (path and facts, never the token)."""
    from app.jobsearch.factory import serves_mount

    port = notice.own_port()
    folder = data_dir()
    parts = [f"version={VERSION}", f"program={program.PROGRAM}", f"pid={os.getpid()}"]
    if port is not None:
        parts.append(f"port={port}")
    parts.append(f"data={folder if folder else 'unset'}")
    for path, whose, method in _DOOR_TOKENS:
        if serves_mount(path, role):
            file = getattr(settings, method)()
            parts.append(f"{whose}_token={file} {_file_facts(file)}")
    return "started: " + "; ".join(parts)


def transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection stays on; the Host headers allowed are localhost plus what this install names."""
    extra = [h.strip() for h in settings.mcp_allowed_hosts.split(",") if h.strip()]
    for host in public_url_hosts(settings.public_url):
        if host not in extra:
            extra.append(host)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=["localhost:*", "127.0.0.1:*", *extra],
        allowed_origins=[],
    )


def header_of(ctx: Context) -> str | None:
    headers = ctx.headers
    return (headers.get("authorization") or headers.get("Authorization")) if headers else None


@dataclass(frozen=True)
class Caller:
    """Who is calling: the runner's session (`X-Session-Id`, the id it sends in its heartbeat) and its runner
    (`X-Runner`), as a session's MCP configuration names them. Both are None for a caller that names none."""

    session_id: str | None = None
    runner: str | None = None
    #: The digest of the owner context the session holds in its system prompt (`X-Context`, `session_context.py`).
    context: str | None = None
    #: The hand-out the runner started the session for (`X-Claim`, `hand_outs.py`): the take-next call takes it.
    claim: str | None = None


NOBODY = Caller()
#: The caller of the tool call being answered, for the tools that need to know which session asks (a claim on a role,
#: `analysis.claim`). Set by `Door.call` for the length of the call.
caller: ContextVar[Caller] = ContextVar("door_caller", default=NOBODY)


def caller_of(ctx: Context) -> Caller:
    headers = ctx.headers or {}

    def named(name: str) -> str | None:
        value = headers.get(name) or headers.get(name.title())
        return " ".join(str(value).split())[:80] or None if value else None

    return Caller(named("x-session-id"), named("x-runner"), named("x-context"), named("x-claim"))


def tool_key(name: str) -> str:
    """A tool's name as Claude Code writes it in a tool list: every character outside letters, digits, `_` and `-`
    is `_` (`jobs.analysis_submit` is `jobs_analysis_submit`)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def wanted_tools(headers: Any) -> set[str] | None:
    """The tools a session asked to be shown (`X-Tools`: its kind's own, comma-separated, each as `tool_key` writes it),
    or None when it named none: it is shown them all."""
    try:
        said = (headers.get("x-tools") or headers.get("X-Tools")) if headers else None
    except AttributeError:
        return None
    names = {tool_key(n.strip()) for n in str(said or "").split(",") if n.strip()}
    return names or None


class BearerGate:
    """The front of a door that has a token of its own: an ASGI wrapper that answers 401 to any HTTP request without
    that token, before the MCP layer reads it. Everything else (the lifespan) passes through. A door says which file
    holds its token (`token_file`) and names itself (`realm`, `reason`): the browser door's and the owner's are
    subclasses."""

    realm = "resumebot"
    reason = "no valid token on this call"

    def __init__(self, app) -> None:
        self.app = app

    def token_file(self) -> Path:
        raise NotImplementedError

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            why = refusal_reason(headers.get("authorization"), self.token_file())
            if why is not None:
                log.warning("%s: 401, %s", self.realm, why)
                body = json.dumps({"error": self.reason}).encode()
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", f'Bearer realm="{self.realm}"'.encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


class _KindServer(MCPServer):
    """An MCP server that lists a session only the tools its `X-Tools` header names. Every session of the scout used
    to be sent all 38 tool definitions (about 9k tokens, read again on every turn) and used two to six. A caller that
    names none is listed them all, and the filter changes only what is listed: a call is checked as it always was."""

    async def _handle_list_tools(self, ctx: Any, params: Any) -> ListToolsResult:
        listed = await self.list_tools()
        wanted = wanted_tools(getattr(getattr(ctx, "request", None), "headers", None))
        if wanted is None:
            return ListToolsResult(tools=listed)
        return ListToolsResult(tools=[t for t in listed if tool_key(t.name) in wanted])


class Door:
    """One MCP door: the server, how a call's token is checked, and what a refusal says.

    `token_ok(header)` is looked up when a call arrives, so a test can stand in for it; `token_file()` names the file
    the door's token is kept in, so a refusal can say why it was refused. `no_token` is the reason and the
    fix a call without the token is refused with; `label` begins the log lines; `if_it_fails` is the fix an agent is
    told when the app itself failed. `before(session, owner)` runs before the tool, in a transaction of its own that
    is committed first, and `after(result)` once the commit has landed (a wake is sent only then)."""

    def __init__(
        self, name: str, instructions: str, *, log: logging.Logger, label: str,
        token_ok: Callable[[str | None], bool], token_file: Callable[[], Path], no_token: tuple[str, str],
        if_it_fails: str, before: Callable[[Any, str], Awaitable[None]] | None = None,
        after: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.mcp = _KindServer(name=name, instructions=instructions)
        self._log, self._label, self._token_ok, self._token_file = log, label, token_ok, token_file
        self._no_token, self._if_it_fails = no_token, if_it_fails
        self._before, self._after = before, after
        #: The tools registered `reads=True`, by name: the ones that only read.
        self.reads: set[str] = set()

    async def call(
        self, ctx: Context, name: str, body: Callable[[Any, str], Awaitable[dict[str, Any]]], *,
        lifeline: bool = False, reads: bool = False,
    ) -> dict[str, Any]:
        """One tool call: the token, `before` in its own transaction, the tool in one of its own committed after it,
        then `after`. A `lifeline` tool (a heartbeat) runs on the lifeline pool (`db.lifeline_session_factory`), so it
        answers while the shared pool is busy. A `reads` tool only reads: its session is a reader's
        (`db.read_session_factory`), which takes no turn and never waits for a writer (`before` is a write of its own,
        and still runs first); a write in it raises `db.WriteInReader`."""
        sessions = db.lifeline_session_factory if lifeline else db.session_factory
        if not self._token_ok(header_of(ctx)):
            why = refusal_reason(header_of(ctx), self._token_file())
            log_refusal(self._log, self._label, name, why or "token refused")
            return refusal(*self._no_token)
        token = caller.set(caller_of(ctx))
        try:
            if self._before is not None:
                # Its own short transaction, committed before the tool begins: `before` writes a row every call writes
                # (the scout's connection, `mcp_server._touch`), and held for a tool's whole call that row lock queued
                # every other call and heartbeat behind one slow tool, each holding a pooled connection, until the pool
                # ran out and the app hung. It is bookkeeping: when the row is busy past the lock
                # wait, the call goes on without it.
                try:
                    async with sessions()() as session:
                        await self._before(session, settings.owner)
                        await session.commit()
                except Exception as exc:
                    if not db.is_busy(exc):
                        raise
                    self._log.info("%s %s: the check-in row was busy, so this call did not note it", self._label, name)
            async with (db.read_session_factory if reads else sessions)()() as session:
                result = await body(session, settings.owner)
                if (size := payload_chars(result)) > REPLY_CEILING_CHARS:
                    raise ReplyTooLarge(name, size)  # before the commit: the call is rolled back
                await session.commit()
        except DomainError as exc:  # a refusal the agent can act on: the app's own error, in plain words
            self._log.info("%s %s refused: %s", self._label, name, exc)
            return refusal(str(exc), getattr(exc, "how_to_fix", None))
        except Exception as exc:  # a bug is not a refusal: it is logged with its traceback, and the agent is told so
            if db.is_busy(exc):  # a busy database (a lock wait or a statement past its limit): nothing was changed
                self._log.info("%s %s: the database was busy, so the call was refused to be made again", self._label,
                               name)
                busy = Busy()
                return refusal(str(busy), busy.how_to_fix)
            self._log.exception("%s %s failed", self._label, name)
            return refusal(UNEXPECTED, self._if_it_fails)
        finally:
            caller.reset(token)
        if self._after is not None:
            await self._after(result)
        return result

    def tool(
        self, name: str, description: str, *, lifeline: bool = False, reads: bool = False,
    ) -> Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Any]]:
        """Register `fn(session, owner, **arguments)` as the tool `name`. Returns the registered function, which is
        called as `fn(ctx, **arguments)` and answers as a call from an agent does. `lifeline`, `reads`: see `call`."""

        def register(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Any]:
            hints = typing.get_type_hints(fn)
            arguments = [p.replace(annotation=hints.get(p.name, p.annotation))
                         for p in list(inspect.signature(fn).parameters.values())[2:]]
            ctx = inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
            shown = inspect.Signature([ctx, *arguments], return_annotation=dict[str, Any])

            async def handler(*args: Any, **kwargs: Any) -> dict[str, Any]:
                bound = shown.bind(*args, **kwargs)
                bound.apply_defaults()
                where = bound.arguments.pop("ctx")

                async def body(session: Any, owner: str) -> dict[str, Any]:
                    return await fn(session, owner, **bound.arguments)

                return await self.call(where, name, body, lifeline=lifeline, reads=reads)

            handler.__name__ = handler.__qualname__ = fn.__name__
            handler.__module__ = fn.__module__
            handler.__signature__ = shown  # type: ignore[attr-defined]
            handler.__annotations__ = {"ctx": Context, **{p.name: p.annotation for p in arguments},
                                       "return": dict[str, Any]}
            if reads:
                self.reads.add(name)
            # `meta` lets Claude Code show a reply up to the ceiling whole (`RESULT_SIZE_META`).
            self.mcp.tool(name=name, description=description, meta=dict(RESULT_SIZE_META))(handler)
            return handler

        return register
