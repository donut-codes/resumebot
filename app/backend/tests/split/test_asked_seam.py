"""`engine_calls.ask` in the three programs. The page and the engine programs save the ask and wait.
The whole app in one process (`program.PROGRAM == "all"`: the test seam the tests' `client` is built from, and
`resumebot serve`) has no other program to ask, so a function registered for the ask runs there and then, in the
caller's session, and no row is written. Everything here is made up."""

from app import db, program
from app.jobsearch import facts
from app.jobsearch.work import engine_asks, engine_calls
from tests.support import OWNER


async def _echo(session, owner, args):
    return {"n": args["n"] + 1, "owner": owner}


async def _kept() -> dict[str, str]:
    async with db.read_session_factory()() as reader:
        return await facts.get_many(reader, OWNER, "ask:")


def test_an_ask_is_registered_once_a_function_answers_to_its_name(monkeypatch):
    monkeypatch.setattr(engine_asks, "_asks", {"echo": _echo})
    assert engine_asks.is_registered("echo") is True
    assert engine_asks.is_registered("nobody") is False


async def test_in_the_whole_app_a_registered_ask_runs_here_and_writes_no_row(js, monkeypatch):
    monkeypatch.setattr(engine_asks, "_asks", {"echo": _echo})
    monkeypatch.setattr(program, "PROGRAM", "all")
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)  # before the steps the ask is a row and a wait
    async with db.session_factory()() as session:
        got = await engine_calls.ask(session, OWNER, "echo", {"n": 1}, 0.01)
    assert got == {"n": 2, "owner": OWNER}
    assert await _kept() == {}


async def test_in_the_whole_app_an_ask_nobody_registered_is_still_a_row_and_a_wait(js, monkeypatch):
    monkeypatch.setattr(engine_asks, "_asks", {})
    monkeypatch.setattr(program, "PROGRAM", "all")
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    async with db.session_factory()() as session:
        got = await engine_calls.ask(session, OWNER, "nobody", {}, 0.01)
    assert got is None
    assert len(await _kept()) == 1


async def test_in_the_page_program_a_registered_ask_is_not_run_here(js, monkeypatch):
    ran: list[dict] = []

    async def echo(session, owner, args):
        ran.append(args)
        return {}

    monkeypatch.setattr(engine_asks, "_asks", {"echo": echo})
    monkeypatch.setattr(program, "PROGRAM", "page")
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    async with db.session_factory()() as session:
        got = await engine_calls.ask(session, OWNER, "echo", {"n": 1}, 0.01)
    assert got is None and ran == []
    assert len(await _kept()) == 1, "the ask is kept for the engine"


def test_the_words_for_a_stopped_engine_say_the_request_is_saved():
    assert "saved" in engine_calls.WORKER_NOT_RUNNING
