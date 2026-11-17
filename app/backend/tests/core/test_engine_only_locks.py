"""Engine-only locks and per-process uniqueness. The named locks and
`transaction_lock` live in one process, so the code that takes them runs only in the engine program (or the whole-app
test seam); the page's start takes no instance lock; and a line's time (`utc_now_unique`) is unique in one process
only, so lines are put in order by (instant, id). Everything is made up."""

import asyncio
import sqlite3
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from app import db, program
from app.agents import roster
from app.errors import Conflict
from app.jobsearch.actors import CLAUDE
from app.jobsearch.browser import reader
from app.jobsearch.gating import regate
from app.jobsearch.models import ActionRun
from app.jobsearch.owner_actions import Action, runner
from app.jobsearch.owner_actions.registry import Args, Ctx, Outcome
from app.jobsearch.pipeline import quiet
from app.jobsearch.work import hand_outs
from app.models.work import TrailEvent
from tests.support import NOON, OWNER

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

#: Stands in for a session: each call below must refuse before it touches one.
NO_SESSION: Any = None
APP = Path(db.__file__).parent


async def _regate() -> None:
    async with regate._one_at_a_time(NO_SESSION, "the-owner"):
        pass


CALLS = {
    "the re-gate": _regate,
    "the quiet run": lambda: quiet.run(NO_SESSION, "the-owner"),
    "the hand-out lock": lambda: hand_outs.lock(NO_SESSION, "the-owner", "parser"),
    "the hand-out decide": lambda: hand_outs.decide(NO_SESSION, "the-owner", "parser"),
    "the browser mode check": lambda: reader.browser_mode_seen(NO_SESSION, "the-owner", "own"),
}


@pytest.mark.parametrize("name", list(CALLS))
async def test_a_call_that_takes_an_in_process_lock_refuses_to_run_in_the_page(name, monkeypatch):
    monkeypatch.setattr(program, "PROGRAM", "page")
    with pytest.raises(program.WrongProgram, match="belongs to the engine program"):
        await CALLS[name]()


def test_the_page_program_takes_no_instance_lock_and_the_engine_start_does():
    assert "instance_lock" not in (APP / "jobsearch" / "page.py").read_text(encoding="utf-8")
    assert "instance_lock.hold" in (APP / "jobsearch" / "main.py").read_text(encoding="utf-8")

# --- the running-row check of an owner action is the writer's turn, not an in-process lock -------------------------

class _NoArgs(Args):
    pass


async def _nothing(ctx: Ctx, args: _NoArgs) -> Outcome:
    return Outcome({}, did="did it", would="do it")


ONCE = Action("test.once", "once", _NoArgs, _nothing, writes=True, one_at_a_time=True)


async def _running() -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(ActionRun).where(
            ActionRun.action == "test.once", ActionRun.state == "running")) or 0


async def test_opening_an_action_takes_no_in_process_lock(js, monkeypatch):
    async def refuse(*_, **__):
        raise AssertionError("an in-process lock was taken: a second program would not see it")

    monkeypatch.setattr(db, "transaction_lock", refuse)
    monkeypatch.setattr(db, "transaction_lock_or_busy", refuse)
    _, holder = await runner._open(OWNER, ONCE, _NoArgs(), False)
    assert holder is None and await _running() == 1


async def test_two_opens_at_once_leave_one_running_row_and_the_second_is_refused(js):
    got = await asyncio.gather(*(runner._open(OWNER, ONCE, _NoArgs(), False) for _ in range(2)),
                               return_exceptions=True)
    assert sorted(type(one).__name__ for one in got) == ["Conflict", "tuple"]
    assert await _running() == 1


async def test_a_running_row_the_other_program_commits_while_this_one_waits_refuses_the_open(js):
    async with db.session_factory()() as session:
        session.add(ActionRun(owner=OWNER, action="test.once", arguments={}, dry_run=False, by=CLAUDE,
                              state="done", started=NOON, ended=NOON))
        await session.commit()
    other = sqlite3.connect(js.url.database, timeout=5, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")  # the other program's write turn: SQLite's own lock
    other.execute("UPDATE js_action_runs SET state = 'running', ended = NULL WHERE action = 'test.once'")
    opening = asyncio.create_task(runner._open(OWNER, ONCE, _NoArgs(), False))
    commit = threading.Thread(target=other.commit)  # ends the other program's turn, from outside the loop
    commit.start()
    with pytest.raises(Conflict, match="running already"):
        await opening
    commit.join()
    other.close()
    assert await _running() == 1


# --- lines that share an instant are put in order by their id ---------------------------------------------------

def _line(n: int, **fields: Any) -> TrailEvent:
    """A history line with the id `n` and the one instant NOON: two programs can each give a line the same instant."""
    return TrailEvent(id=uuid.UUID(int=n), owner=OWNER, at=NOON, actor="app", who="App",
                      event=fields.pop("event", "x"), sentence=f"line {n}", **fields)


async def _store(*lines: TrailEvent) -> None:
    """Stores the lines one at a time, in the order given, so the order they sit in the table is the order given."""
    async with db.session_factory()() as session:
        for line in lines:
            session.add(line)
            await session.flush()
        await session.commit()


async def test_an_agents_lines_at_one_instant_come_back_the_highest_id_first(js):
    # Stored in the order 2, 3, 1: a read that leaves the tie to the table comes back 2, 3, 1 (first stored first) or
    # 1, 3, 2 (last stored first), and neither is 3, 2, 1.
    await _store(*(_line(n, agent_slug="scout") for n in (2, 3, 1)))
    async with db.session_factory()() as session:
        got = await roster.activity(session, OWNER, SimpleNamespace(slug="scout"))  # type: ignore[arg-type]
    assert [one["text"] for one in got] == ["line 3", "line 2", "line 1"]


async def test_the_browser_mode_check_takes_the_highest_id_when_lines_share_an_instant(js):
    # Stored in the order id 1 (profile), id 3 (own), id 2 (profile). The highest id says "own", so a check with mode
    # "profile" is a change back and notes one more profile line (3 in all). A read that leaves the tie to the table
    # takes the first stored or the last stored, both "profile": it notes nothing (2 in all).
    await _store(_line(1, event=reader.BROWSER_PROFILE), _line(3, event=reader.BROWSER_OWN),
                 _line(2, event=reader.BROWSER_PROFILE))
    async with db.session_factory()() as session:
        await reader.browser_mode_seen(session, OWNER, "profile")
        await session.commit()
        noted = await session.scalar(select(func.count()).select_from(TrailEvent).where(
            TrailEvent.owner == OWNER, TrailEvent.event == reader.BROWSER_PROFILE))
    assert noted == 3


@pytest.mark.parametrize("path, tail", [
    (("agents", "roster.py"), ".limit(limit)"),
    (("jobsearch", "browser", "reader.py"), ".limit(1))"),
    (("jobsearch", "owner_actions", "reads.py"), ".limit(20))).all()"),
])
def test_the_three_history_reads_order_by_instant_then_id(path, tail):
    source = APP.joinpath(*path).read_text(encoding="utf-8")
    assert f".order_by(TrailEvent.at.desc(), TrailEvent.id.desc()){tail}" in source
