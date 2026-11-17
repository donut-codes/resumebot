"""May Claude work now? (`work/availability.py`): the one answer behind the pool (and Launch, which counts it its own
way), the browser worker, the owner's waiting jobs and the catch-up, and the two things it owns, Claude's usage limit
and the restart's drain.

Everything here goes through the module's interface (`barred`, `standing`, `limit_until`, `limit_met`, `counts_from`,
`drain`, `draining`, `forget`) with real rows in the throwaway database and an injected `now`; the world is set by the
rows a caller would see (the connection's pause and check-ins, the profile's peak box, a session's limit, a drain). The
callers' own tests stay with them and prove that no sentence moved. Everything is made up."""

import ast
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.agents import roster
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import availability, hours
from app.jobsearch.work.availability import Barred, Question, Reason
from tests.support import OWNER

#: A Wednesday, 6:00 Pacific: inside the default peak window, which ends at 18:00 UTC (the owner's zone is UTC here).
AT = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
PEAK_ENDS = datetime(2026, 7, 1, 18, 0, tzinfo=UTC)
#: When the limit a session met lifts, well inside the window and before it ends.
LIFTS = AT + timedelta(hours=2)

BLACKOUT = "Claude's peak hours: no Claude work until 6:00 PM."
POOL_VOICE = {
    Reason.RESTARTING: (None, "the app is restarting"),
    Reason.PAUSED: (None, "the agents are paused"),
    Reason.PEAK: (None, "busy hours"),
    Reason.USAGE: (LIFTS, "Claude's usage limit"),
}
#: Every reason each question asks, with what the answer carries: when it lifts and the sentence. Written out here, not
#: read from the module, so a change to a word or a row is a change to this table.
ANSWERS: dict[Question, dict[Reason, tuple[datetime | None, str]]] = {
    Question.POOL: {r: POOL_VOICE[r] for r in (Reason.RESTARTING, Reason.PAUSED, Reason.PEAK, Reason.USAGE)},
    Question.CATCH_UP: {
        Reason.PAUSED: POOL_VOICE[Reason.PAUSED], Reason.USAGE: POOL_VOICE[Reason.USAGE],
        Reason.PEAK: (PEAK_ENDS, "busy hours")},
    Question.WORKER: {
        Reason.PAUSED: (None, "The owner has paused the agents."),
        Reason.PEAK: (None, BLACKOUT + hours.BLACKOUT_HINT)},
    Question.JOB: {
        Reason.PEAK: (None, BLACKOUT),
        Reason.ABSENT: (None, "No agent has checked in yet."),
        Reason.PAUSED: (None, "The agent is paused: it is handed no work until you resume it."),
        Reason.USAGE: (LIFTS, "Claude's usage limit is reached; it resets at 3:00 PM. Try again after that.")},
}
#: The order each question asks its reasons in: the first that stands is the answer.
ORDERS = {
    Question.POOL: (Reason.RESTARTING, Reason.PAUSED, Reason.PEAK, Reason.USAGE),
    Question.WORKER: (Reason.PAUSED, Reason.PEAK),
    Question.JOB: (Reason.PEAK, Reason.ABSENT, Reason.PAUSED, Reason.USAGE),
    Question.CATCH_UP: (Reason.PAUSED, Reason.USAGE, Reason.PEAK),
}


@pytest.fixture(autouse=True)
def the_owners_zone_is_utc(monkeypatch):
    monkeypatch.setattr("app.timeutil.settings.timezone", "UTC")


async def _stand(*reasons: Reason, silent_for: timedelta | None = None) -> None:
    """Make exactly these reasons stand at `AT`, and no other. An agent that has checked in lately is the default;
    `ABSENT` means none ever did, or none for `silent_for`."""
    availability.forget()
    async with db.session_factory()() as session:
        conn = await roster.ensure_roster(session, OWNER)
        conn.paused = Reason.PAUSED in reasons
        conn.usage_limit_until = None
        conn.checked_in_at = conn.declared_at = None
        if Reason.ABSENT not in reasons:
            conn.last_seen_at = AT - timedelta(minutes=1)
        else:
            conn.last_seen_at = None if silent_for is None else AT - silent_for
        await profile_store.save(session, OWNER, "settings", {"avoid_peak": Reason.PEAK in reasons}, by="test")
        if Reason.USAGE in reasons:
            await availability.limit_met(session, OWNER, {"resets_at": LIFTS.isoformat()}, now=AT)
        await session.commit()
    if Reason.RESTARTING in reasons:
        availability.drain(OWNER, True, AT)


async def _ask(question: Question) -> Barred | None:
    async with db.session_factory()() as session:
        return await availability.barred(session, OWNER, question, now=AT)


# --- the matrix: every question against every reason ------------------------------------------------------------------


async def test_each_question_gives_each_of_its_reasons_in_its_own_words_and_with_when_it_lifts(js):
    for question, row in ANSWERS.items():
        for reason, (until, words) in row.items():
            await _stand(reason)
            assert await _ask(question) == Barred(reason, until, words), f"{question}: {reason}"


async def test_a_reason_a_question_does_not_ask_leaves_it_clear(js):
    for question in Question:
        for reason in Reason:
            if reason in ORDERS[question]:
                continue
            await _stand(reason)
            assert await _ask(question) is None, f"{question} must ignore {reason}"


async def test_with_nothing_standing_every_question_is_clear(js):
    await _stand()
    for question in Question:
        assert await _ask(question) is None, question


async def test_an_agent_silent_for_a_while_is_absent_in_the_owners_job_words(js):
    await _stand(Reason.ABSENT, silent_for=timedelta(minutes=11))
    assert await _ask(Question.JOB) == Barred(Reason.ABSENT, None, "No agent has checked in for 11 minutes.")
    await _stand(Reason.ABSENT, silent_for=timedelta(minutes=9))
    assert await _ask(Question.JOB) is None, "heard within the last ten minutes is there"
    await _stand(Reason.ABSENT, silent_for=timedelta(hours=5))
    assert (await _ask(Question.JOB)).words == "No agent has checked in for 5 hours."
    await _stand(Reason.ABSENT, silent_for=timedelta(days=3))
    assert (await _ask(Question.JOB)).words == "No agent has checked in for 3 days."


# --- the order -------------------------------------------------------------------------------------------------------


async def test_each_question_gives_the_first_reason_of_its_own_order_as_the_ones_before_it_lift(js):
    for question, order in ORDERS.items():
        for lifted in range(len(order) + 1):
            await _stand(*(set(Reason) - set(order[:lifted])))  # every reason stands, but the ones already lifted
            stop = await _ask(question)
            assert (stop.reason if stop else None) == (order[lifted] if lifted < len(order) else None), (
                f"{question} after {order[:lifted]}")


async def test_a_reason_after_the_one_that_stands_is_never_read(js, monkeypatch):
    async def never(*_a, **_k):
        raise AssertionError("read after the answer was known")

    await _stand(Reason.PAUSED, Reason.PEAK, Reason.USAGE)
    monkeypatch.setattr(profile_store, "current", never)
    assert (await _ask(Question.POOL)).reason is Reason.PAUSED, "the pause answers before the profile is read"
    monkeypatch.setattr(roster, "connection_by_slug", never)
    availability.drain(OWNER, True, AT)
    assert (await _ask(Question.POOL)).reason is Reason.RESTARTING, "the drain is read from memory alone"


async def test_the_connection_is_read_once_and_the_profile_once_however_many_reasons_ask_for_them(js, monkeypatch):
    await _stand()
    reads = {"connection": 0, "profile": 0}
    real_connection, real_profile = roster.connection_by_slug, profile_store.current

    async def connection(*args, **kwargs):
        reads["connection"] += 1
        return await real_connection(*args, **kwargs)

    async def profile(*args, **kwargs):
        reads["profile"] += 1
        return await real_profile(*args, **kwargs)

    monkeypatch.setattr(roster, "connection_by_slug", connection)
    monkeypatch.setattr(profile_store, "current", profile)
    assert await _ask(Question.JOB) is None  # asks peak, absent, paused and usage: the whole row
    assert reads == {"connection": 1, "profile": 1}


# --- every reason that stands, for a caller whose rule is its own ----------------------------------------------------


async def _all_that_stand(question: Question) -> list[Barred]:
    async with db.session_factory()() as session:
        return await availability.standing(session, OWNER, question, now=AT)


async def test_standing_gives_each_reason_of_a_questions_row_that_stands_in_the_rows_order(js):
    for question, order in ORDERS.items():
        await _stand(*order)
        assert [stop.reason for stop in await _all_that_stand(question)] == list(order), f"{question}: all of them"
        for reason in order:
            await _stand(reason)
            assert [stop.reason for stop in await _all_that_stand(question)] == [reason], f"{question}: {reason} alone"
        await _stand()
        assert await _all_that_stand(question) == [], f"{question}: nothing stands"


async def test_standing_answers_each_reason_as_barred_does(js):
    for question, row in ANSWERS.items():
        await _stand(*row)
        assert {stop.reason: (stop.until, stop.words) for stop in await _all_that_stand(question)} == row, question


async def test_standing_reads_past_the_first_reason_yet_reads_the_connection_and_the_profile_once(js, monkeypatch):
    await _stand(Reason.PAUSED, Reason.PEAK, Reason.USAGE)
    reads = {"connection": 0, "profile": 0}
    real_connection, real_profile = roster.connection_by_slug, profile_store.current

    async def connection(*args, **kwargs):
        reads["connection"] += 1
        return await real_connection(*args, **kwargs)

    async def profile(*args, **kwargs):
        reads["profile"] += 1
        return await real_profile(*args, **kwargs)

    monkeypatch.setattr(roster, "connection_by_slug", connection)
    monkeypatch.setattr(profile_store, "current", profile)
    assert [stop.reason for stop in await _all_that_stand(Question.POOL)] == [Reason.PAUSED, Reason.PEAK, Reason.USAGE]
    assert reads == {"connection": 1, "profile": 1}


# --- Claude's usage limit, and where it is kept ----------------------------------------------------------------------


async def _met(said: Any, *, at: datetime = AT) -> datetime:
    async with db.session_factory()() as session:
        until = await availability.limit_met(session, OWNER, said, now=at)
        await session.commit()
    return until


async def _until(at: datetime = AT) -> datetime | None:
    async with db.session_factory()() as session:
        return await availability.limit_until(session, OWNER, now=at)


async def test_a_time_claude_gave_is_believed_when_it_is_ahead_and_within_eight_days_else_half_an_hour(js):
    await _stand()
    ahead = AT + timedelta(hours=2)
    assert await _met({"resets_at": ahead.isoformat()}) == ahead
    for bad in (None, "", "not a time", (AT - timedelta(minutes=1)).isoformat(), AT.isoformat(),
                (AT + timedelta(days=9)).isoformat()):
        availability.forget()
        assert await _met({"resets_at": bad}) == AT + availability.RETRY_AFTER, repr(bad)
    availability.forget()
    assert await _met({}) == AT + availability.RETRY_AFTER
    assert await _met("said nothing") == AT + availability.RETRY_AFTER, "a heartbeat that is no dict says nothing"


async def test_a_later_shorter_reading_never_cuts_a_hold_short(js):
    await _stand()
    await _met({"resets_at": LIFTS.isoformat()})
    shorter = await _met({"resets_at": (AT + timedelta(minutes=5)).isoformat()})
    assert shorter == AT + timedelta(minutes=5), "what the session read is what it is told"
    assert await _until() == LIFTS, "but the hold is the later of the two"
    availability.forget()
    assert await _until() == LIFTS, "and the connection kept it too"


async def test_after_a_restart_the_connection_alone_still_holds(js):
    await _stand(Reason.USAGE)
    availability.forget()  # a fresh process: the app's memory is empty, the database is the same
    assert await _until() == LIFTS
    assert await _ask(Question.POOL) == Barred(Reason.USAGE, LIFTS, "Claude's usage limit")


async def test_with_no_scout_connection_the_process_memory_alone_holds(js):
    assert await _met({"resets_at": LIFTS.isoformat()}) == LIFTS
    assert await _until() == LIFTS
    availability.forget()
    assert await _until() is None


async def test_a_hold_is_gone_once_the_moment_it_lifts_has_come(js):
    await _stand(Reason.USAGE)
    assert await _until(LIFTS - timedelta(seconds=1)) == LIFTS
    assert await _until(LIFTS) is None
    assert await _until(LIFTS + timedelta(hours=1)) is None


async def test_limit_until_and_the_usage_reason_say_the_same_time(js):
    await _stand(Reason.USAGE)
    for question in (Question.POOL, Question.JOB, Question.CATCH_UP):
        stop = await _ask(question)
        assert stop is not None and stop.until == await _until(), question


async def test_the_sweeps_count_from_the_later_of_the_last_peak_end_and_the_limits_last_end(js):
    after = PEAK_ENDS + timedelta(hours=2)  # 20:00 UTC: the window ended at 18:00

    async def counts() -> datetime | None:
        async with db.session_factory()() as session:
            return await availability.counts_from(session, OWNER, now=after)

    await _stand()  # the box is not ticked, and no limit was met
    assert await counts() is None
    await _stand(Reason.PEAK)
    assert await counts() == PEAK_ENDS
    await _met({"resets_at": (PEAK_ENDS + timedelta(hours=1)).isoformat()}, at=AT)  # lifted at 19:00, after the window
    assert await counts() == PEAK_ENDS + timedelta(hours=1)
    await _stand(Reason.PEAK)
    await _met({"resets_at": (AT + timedelta(hours=1)).isoformat()}, at=AT)  # lifted at 14:00, before it
    assert await counts() == PEAK_ENDS
    await _stand()
    await _met({"resets_at": LIFTS.isoformat()}, at=AT)  # a limit that lifted long ago still counts: not time stuck
    assert await counts() == LIFTS


# --- the drain -------------------------------------------------------------------------------------------------------


async def test_a_drain_bars_the_pool_for_five_minutes_unless_the_lanes_start_again(js):
    await _stand()
    availability.drain(OWNER, True, AT)
    for question in Question:
        stop = await _ask(question)
        assert (stop.reason if stop else None) == (Reason.RESTARTING if question is Question.POOL else None), question
    assert availability.draining(OWNER, AT + timedelta(minutes=5) - timedelta(seconds=1))
    assert not availability.draining(OWNER, AT + availability.DRAIN_FOR)
    async with db.session_factory()() as session:
        later = await availability.barred(session, OWNER, Question.POOL, now=AT + timedelta(minutes=6))
    assert later is None, "five minutes on, the pool is clear again"
    availability.drain(OWNER, False)
    assert not availability.draining(OWNER, AT) and await _ask(Question.POOL) is None


async def test_forget_empties_the_drain_and_the_limit_the_process_remembers(js):
    availability.drain(OWNER, True, AT)
    await _met({"resets_at": LIFTS.isoformat()})
    availability.forget()
    assert not availability.draining(OWNER, AT) and await _until() is None


# --- the layering guard -----------------------------------------------------------------------------------------------


def test_the_module_imports_only_what_keeps_it_at_the_bottom_of_the_import_graph():
    """Callers of every layer import `availability` at the top, which is only safe while it reaches none of them: it
    never imports the browser, the missions, the pool, the job queue or the session kinds (that is how the three lazy
    imports of `catch_up` ended). Peak hours are read through the `hours` module, so a test that patches them binds."""
    allowed = ("app.agents.roster", "app.jobsearch.profile.store", "app.jobsearch.work.hours", "app.timeutil")
    tree = ast.parse(Path(availability.__file__).read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            found += [(alias.name, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "app.jobsearch.work.hours", "peak hours are read as `hours.<function>`, never by name"
            found += [(node.module or "", f"{node.module}.{alias.name}") for alias in node.names]
    assert found, "no imports were read"
    for module, name in found:
        standard = module.split(".")[0] in sys.stdlib_module_names or module.split(".")[0] == "sqlalchemy"
        ours = any(name == ok or name.startswith(ok + ".") or module == ok for ok in allowed)
        assert standard or ours, f"availability must not import {name}"
