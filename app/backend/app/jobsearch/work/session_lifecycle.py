"""A runner's heartbeat word, settled: what the app does with `idle`, `boot`, `started`, `alive` and `ended`.

One call, `settle`, takes the word (`POST /api/v1/agent/heartbeat`) and answers whether the runner should end the
session it has. The order of the parts below is the contract (lock order, and what must come before what); each part
that can fail runs in its own savepoint and is logged, never raised, so one part's failure does not undo the others.
The per-kind part is `session_kinds.heard`; this module owns everything around it.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import roster
from app.agents.roster import open_run
from app.errors import Busy
from app.jobsearch.missions import catch_up
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work import availability, session_kinds
from app.jobsearch.work import hours as night

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Word:
    """The runner's word, read once."""

    state: str
    sid: str  # the session id, cut to what a row keeps
    kind: str | None  # the session id starts with its kind (scout-, analyst-, writer-, parser-): PRD 25.9
    lane: bool  # a runner's analysis lane is not the scout's own session
    alive: bool

    @classmethod
    def read(cls, body: dict) -> "_Word":
        raw = str(body.get("session_id") or "")
        return cls(state=str(body.get("state") or ""), sid=raw[:80],
                   kind=str(body.get("kind") or raw.split("-", 1)[0]) or None, lane=bool(body.get("lane")),
                   alive=body.get("state") in ("started", "alive"))


async def _guarded[T](session: AsyncSession, why: str, call: Callable[[], Awaitable[T]], *,
                      busy: bool = False) -> T | None:
    """One part in its own savepoint: a failure is logged and swallowed. `busy` lets `Busy` through: a part told
    `Busy` (the writer's turn did not come in time) refuses the report with nothing kept, and the runner, which
    sends its start again on any answer but 200 (scout.mjs `announceStart`), tries again; caught, the report was
    answered 200 and never sent again."""
    try:
        async with session.begin_nested():
            return await call()
    except Busy:
        if busy:
            raise
        log.exception("couldn't %s", why)
    except Exception:
        log.exception("couldn't %s", why)
    return None


async def _seats_heard(session: AsyncSession, owner: str) -> None:
    """The runner's word is in: a seat run the last stop of the app cut short is closed when its runner shows it
    (`seats.runner_heard`, docs/PRD.md 43.6)."""
    from app.jobsearch.writing import seats  # the seats read the process, which reads this module's neighbours

    await _guarded(session, "close the seat runs the last stop cut short", lambda: seats.runner_heard(session, owner),
                   busy=True)


async def _scout_stamp(session: AsyncSession, owner: str, word: _Word) -> None:
    """A search's last sign of life, kept on its run while the scout's session is alive (a bare write)."""
    current = await open_run(session, owner)
    if current is not None and word.alive and word.kind == roster.SCOUT:
        current.session_id = word.sid or None
        current.session_seen_at = datetime.now(UTC)


async def _scout_close_unfinished(session: AsyncSession, owner: str, word: _Word) -> None:
    """A scout session that ends without saying it was done (out of turns or time) keeps what it reported."""
    from app.jobsearch.missions import search_run  # it imports the session kinds, which import it

    current = await open_run(session, owner)
    if (current is not None and word.state == "ended" and word.kind == roster.SCOUT and current.reported
            and current.session_id == (word.sid or None)):
        await search_run.close_unfinished(session, current, "The scout's session ended before it said it was done")


async def _boot(session: AsyncSession, owner: str, body: dict) -> None:
    """A runner has just started: the lanes are up again."""
    # A drain a restart asked for is over (`availability.drain`).
    availability.drain(owner, False)
    # Every session it had was killed with it (a restart, a deploy), so the roles they held are free at once, not when
    # their silence would have been noticed (`analysis.release_runner`).
    runner, boot = (str(body.get(k) or "").strip() for k in ("runner", "boot"))
    if runner and boot:
        await _guarded(session, f"release the roles runner {runner} had before it started again",
                       lambda: analysis.release_runner(session, owner, runner, boot), busy=True)
    # Which Chrome the browser jobs use: on a change to the owner's own, every browser site is learned again.
    if isinstance(body.get("browser_mode"), str):
        from app.jobsearch.browser import reader as browser_reader  # imported here, as the pool is above

        await _guarded(session, "note which Chrome the browser jobs use",
                       lambda: browser_reader.browser_mode_seen(session, owner, body["browser_mode"]), busy=True)


async def settle(session: AsyncSession, owner: str, body: dict) -> bool:
    """A runner's heartbeat word (idle, boot, started, alive, ended) in; whether the runner should end the session
    it has out. It does inside the blackout (docs/PRD.md 25.8), and its role is picked up again after. The caller
    commits. True only when the kind's verdict is "stop", or the session is alive, the blackout holds and the kind's
    verdict is not "finish". `idle` and `boot` always answer False.

    Raises only `Busy` (release_runner, browser_mode_seen, the seat sweep, `note_browser`, the scout's run stamp):
    each of them runs in its own savepoint.

    Every heartbeat is also noted against the runner that sent it (`roster.note_runner`: its `runner` name, else its
    `lane`), which is what the Agents page shows when several runners are up. A runner with lanes that does not serve
    the owner's work says `lane` (scout.mjs): what it says is the runner's, not the connection's, so the connection's
    check-in and session are left as they are; its idle word is the runner's alone."""
    word = _Word.read(body)
    # Whether the owner's own Chrome can take browser jobs, as the runner last found it ("" when it can): what Wake up
    # and Run say when it cannot (`pool.note_browser`).
    if word.state in ("idle", "ended") and isinstance(body.get("browser"), str):
        from app.jobsearch.pool import core as pool  # the pool reads the runs

        await _guarded(session, "note whether the browser jobs can run",
                       lambda: pool.note_browser(session, owner, body["browser"]), busy=True)
    if word.state == "idle":
        if not body.get("lane") or body.get("lane") == "any":  # a pool session is the scout's presence too
            await roster.present(session, owner)
        await roster.note_runner(session, owner, body)
        await _seats_heard(session, owner)
        return False
    if word.state == "boot":
        await _boot(session, owner, body)
        return False
    if word.state == "ended" and body.get("spend") is not None:
        from app.jobsearch.pool import budget  # it reads the pool, which reads the runs

        await _guarded(session, f"keep what session {body.get('session_id')} cost",
                       lambda: budget.record(session, owner, str(body.get("session_id") or ""), word.kind,
                                             body["spend"]))
    # The connection's row first, as every tool call takes it (`mcp_server._call`): two requests that take the same
    # two rows in opposite orders deadlock (a heartbeat against the scout's last
    # report), and the one the database ends is a 500.
    # A session of an analysis lane (another container that researches and nothing else, scout.mjs) is not the scout's
    # own: the page and the check for whether an agent can take a job now show the scout's session, which a lane's
    # session, starting and ending beside it, would otherwise overwrite. The runner was heard from all the same.
    await roster.touch(session, owner, alive=word.alive and not word.lane,
                       kind=word.kind if session_kinds.of_runner(word.kind or "") is not None else None,
                       ended=word.state == "ended" and not word.lane)
    await roster.note_runner(session, owner, body, word.kind)
    # The tabs a session opened in the app's Chrome close with it (docs/PRD.md 52.3): off the register now, closed once
    # this commits.
    if word.state == "ended" and word.sid:
        from app.jobsearch.browser import (
            tabs as tab_register,  # it reads the models, which read this module's neighbours
        )

        await _guarded(session, f"close the tabs of session {word.sid}",
                       lambda: tab_register.session_ended(session, owner, word.sid))
    # What the kind does with the session's word (`session_kinds.heard`); each in its own savepoint.
    verdict = await session_kinds.heard(session, owner, word.kind or "", word.sid, word.state,
                                        claim=body.get("claim"))
    # After the kind's own part: a session that says it ended without answering is a failed try (`seats.release`), and
    # only a run whose session went with no word is cut short by the stop.
    await _seats_heard(session, owner)
    if verdict == "stop":
        return True
    # A session that ended at Claude's usage limit (the runner reads it from Claude's own words, scout.mjs): a catch-up
    # after a tracker import holds until the limit lifts, and takes back the role it had with the analyst.
    if word.state == "ended" and isinstance(body.get("limit"), dict):
        await _guarded(session, "note Claude's usage limit",
                       lambda: catch_up.note_usage_limit(session, owner, body["limit"]))
    await _guarded(session, "stamp the search with the scout's sign of life",
                   lambda: _scout_stamp(session, owner, word), busy=True)
    # A session that ended because Claude's subscription is out of usage (the runner reads it in the session's result):
    # nothing that needs Claude can go on, so the night ends cleanly with what it found (docs/PRD.md 25.12). First, so
    # the search it held is wound down as done, not closed as unfinished below.
    said = body.get("usage_limit")
    if word.state == "ended" and isinstance(said, str) and said.strip():
        from app.jobsearch.missions import (
            core as missions,  # missions drives the runs; imported here so the two never loop
        )

        await _guarded(session, "end the night on Claude's usage limit",
                       lambda: missions.usage_limit_hit(session, owner, said))
    await _scout_close_unfinished(session, owner, word)
    if not word.alive or night.blackout((await profile_store.current(session, owner)).settings) is None:
        return False
    # A browser worker holding an item finishes it first: the door then tells it to stop (`browser_door.next_item`).
    return verdict != "finish"
