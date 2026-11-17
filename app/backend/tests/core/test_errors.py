"""One error model. A refusal is a `DomainError` that says its HTTP status; the one handler registered
by `errors.install` answers it, and the MCP doors answer it in the message's own words."""

import importlib
import logging
import pkgutil

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app as app_package
from app import errors
from app.errors import Conflict, DomainError, NotFound, NotReady, Unprocessable
from app.harness import instructions as files
from app.inbox import service as inbox
from app.jobsearch.employers import core as employers
from app.jobsearch.jobs import check as role_check
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.missions import core as missions
from app.jobsearch.pipeline import board
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work import instruction_files, tagging
from app.jobsearch.writing import packets
from app.sites import where


def _all_domain_errors() -> set[type[DomainError]]:
    for module in pkgutil.walk_packages(app_package.__path__, "app."):
        importlib.import_module(module.name)
    found: set[type[DomainError]] = set()
    todo = [DomainError]
    while todo:
        for sub in todo.pop().__subclasses__():
            if sub not in found:
                found.add(sub)
                todo.append(sub)
    return found


def test_no_domain_error_is_also_a_value_error():
    """A bug's ValueError must never be taken for one of the app's own refusals (and the reverse)."""
    both = [e for e in _all_domain_errors() if issubclass(e, ValueError)]
    assert both == []


# What each error answers with, written once here the way a route used to write it by hand.
STATUSES = [
    (board.BoardError, 409), (board.NoSuchColumn, 404), (board.NoSuchRole, 404),
    (packets.PacketError, 409), (packets.NoSuchRole, 404),
    (missions.MissionError, 422), (missions.NoSuchMission, 404), (missions.MissionBusy, 409),
    (analysis.AnalysisError, 422), (analysis.NoSuchRole, 404), (analysis.NoSuchItem, 404),
    (employers.EmployerError, 422), (employers.NoSuchEmployer, 404),
    (tagging.TagError, 422), (role_check.CheckError, 422), (role_check.NotReady, 409), (role_parse.NotReady, 409),
    (profile_store.ProfileError, 422), (profile_store.NoSuchVersion, 404),
    (inbox.TaskError, 409), (inbox.NoSuchTask, 404),
    (where.WhereError, 422), (where.NoSuchSite, 404), (where.OnTheOtherList, 409),
    (instruction_files.FileError, 409), (instruction_files.FileBusy, 409),
    (files.UnknownName, 404), (files.MissingPlaceholder, 409),
    (errors.NotFound, 404), (errors.Conflict, 409), (errors.Unprocessable, 422), (errors.TooLarge, 413),
    (errors.Unauthorized, 401), (errors.ServerFault, 500), (DomainError, 400),
]


@pytest.mark.parametrize(("error", "status"), STATUSES, ids=lambda x: getattr(x, "__qualname__", x))
def test_each_error_says_its_status(error, status):
    assert error.status == status


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t")


@pytest.fixture
def small_app():
    app = FastAPI()
    errors.install(app)

    @app.get("/missing")
    async def missing():
        raise NotFound("no such thing")

    @app.get("/busy")
    async def busy():
        raise Conflict("not now")

    @app.get("/bad")
    async def bad():
        raise Unprocessable("fix this")

    @app.get("/plain")
    async def plain():
        raise DomainError("a refusal with no status of its own")

    @app.get("/not-ready")
    async def not_ready():
        raise NotReady("No agent is free to take it now.")

    @app.get("/bug")
    async def bug():
        raise ValueError("a bug is not a refusal")

    return app


async def test_the_one_handler_answers_a_refusal_with_its_status_and_its_words(small_app):
    async with _client(small_app) as c:
        assert (await c.get("/missing")).status_code == 404
        assert (await c.get("/missing")).json() == {"detail": "no such thing"}
        assert (await c.get("/busy")).status_code == 409
        assert (await c.get("/bad")).status_code == 422
        plain = await c.get("/plain")
        assert plain.status_code == 400 and plain.json() == {"detail": "a refusal with no status of its own"}


async def test_a_refusal_that_says_why_answers_with_detail_and_why(small_app):
    async with _client(small_app) as c:
        res = await c.get("/not-ready")
    assert res.status_code == 409
    assert res.json() == {"detail": "No agent is free to take it now.", "why": "No agent is free to take it now."}


async def test_a_bug_is_still_a_server_error(small_app):
    async with _client(small_app) as c:
        assert (await c.get("/bug")).status_code == 500


async def test_an_unhandled_exception_leaves_one_error_line_without_message_or_query(caplog):
    app = FastAPI()
    errors.install(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret-made-up-resume-text")

    caplog.set_level(logging.DEBUG)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        r = await c.get("/boom?key=sk-made-up-key-789")
    assert r.status_code == 500
    mine = [x for x in caplog.records if x.name == "app.errors"]
    assert len(mine) == 1 and mine[0].levelno == logging.ERROR
    assert mine[0].getMessage() == "500 on GET /boom: RuntimeError"
    assert mine[0].exc_info is None
    ours = " | ".join(x.getMessage() for x in caplog.records if x.name != "httpx")  # httpx is the test's own client
    assert "secret-made-up-resume-text" not in ours and "sk-made-up-key-789" not in ours
