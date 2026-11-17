"""The one base for the app's refusals, and the one place that turns them into an HTTP answer.

A service raises its own error (`BoardError`, `NoSuchSite`, ...). The error says which HTTP status it answers with
(`status`, a class attribute, so a subclass states it in one line) and the one handler registered by `install` answers
`{"detail": <message>}` with it. A route handles an error itself only when the same error needs something else on
that screen (a commit before the answer, a different status); everything else just lets it propagate. Services never
raise `HTTPException`, and neither do routes: the generic `NotFound`, `Conflict` and `Unprocessable` below are for a
route's own refusals.

The MCP doors (`mcp_server`, `browser_mcp`) answer a `DomainError` as a polite refusal in the message's own words
and log anything else with its traceback.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

log = logging.getLogger(__name__)


class DomainError(Exception):
    """A refusal with an HTTP status (400 unless a subclass says otherwise)."""

    status: int = 400

    def body(self) -> dict:
        """What the page is answered with."""
        return {"detail": str(self)}


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


class Unprocessable(DomainError):
    status = 422


class Unauthorized(DomainError):
    status = 401


class TooLarge(DomainError):
    status = 413


class ServerFault(DomainError):
    """Something the app cannot do on its own side (a shipped file that cannot be read)."""

    status = 500


class Busy(DomainError):
    """The database is busy for a moment: a row this call needs is held by another (its lock wait ran out), a statement
    ran past its time, or every pooled connection is in use. Nothing was changed; the same call a few seconds later
    goes through. Answered 503 with `Retry-After`, and at the MCP doors as a refusal that says to call again."""

    status = 503
    how_to_fix = "call again in a few seconds: the app was busy and nothing was changed"

    def __init__(self, why: str = "the app is busy for a moment; nothing was changed") -> None:
        super().__init__(why)


class NotReady(Conflict):
    """No agent can take a piece of Claude work now; `why` is the one plain sentence for the owner, answered as both
    `detail` and `why`."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why

    def body(self) -> dict:
        return {"detail": self.why, "why": self.why}


#: How long a caller told `Busy` is asked to wait before it calls again.
RETRY_AFTER_SECONDS = 5


async def _answer(_: Request, exc: DomainError) -> JSONResponse:
    headers = {"Retry-After": str(RETRY_AFTER_SECONDS)} if isinstance(exc, Busy) else None
    return JSONResponse(status_code=exc.status, content=exc.body(), headers=headers)


async def _database(request: Request, exc: Exception) -> JSONResponse:
    """A database error a route let through: a busy database (`db.is_busy`: a lock wait or a statement past its time,
    or no pooled connection free) is answered as `Busy`; anything else is the bug it was, raised on as before."""
    from app import db  # db imports this module

    if db.is_busy(exc):
        return await _answer(request, Busy())
    raise exc


async def _unhandled(request: Request, exc: Exception) -> PlainTextResponse:
    """The answer to an exception nothing handled: one ERROR line that says which call and which kind of exception,
    never the message (it can hold the owner's text) and never the query. Starlette sends this answer and then raises
    the exception on, so the server's own error log still gets the traceback once; this line is only what to search
    for in the page's log."""
    log.error("500 on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return PlainTextResponse("Internal Server Error", status_code=500)


def install(app: FastAPI) -> None:
    """Register the one handler for every `DomainError` a route lets through, and the one that answers a busy database
    as `Busy` instead of a 500."""
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    app.add_exception_handler(DomainError, _answer)
    app.add_exception_handler(DBAPIError, _database)
    app.add_exception_handler(PoolTimeout, _database)
    app.add_exception_handler(Exception, _unhandled)
