"""The ticks and the six housekeeping parts that moved to them (the work queue spec C6, C7, phase 2): the
minute tick runs the parts that notice what only the clock changes and asks the probes, the hour tick runs the rest,
the janitor and the sweep; both post themselves again with a due time and survive a restart. The whole pass (`settle`
with no parts) does all eight, in the order the ask did them. Everything is made up."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert, select

from app import db
from app.jobsearch.loops import schedule
from app.jobsearch.models import JobPosting, ParseJob, Todo
from app.jobsearch.states import ParseKind, ParseState
from app.jobsearch.work import housekeeping, queue
from app.jobsearch.work.queue import handlers, janitor, loop
from app.jobsearch.work.queue import topics as queue_topics
from tests.jobsearch.work_queue.support import posted_now
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

PARTS = {
    "search_steps": ("app.jobsearch.missions.search_run", "open_run"),
    "jobs.parse": ("app.jobsearch.jobs.parse", "expire"),
    "jobs.check": ("app.jobsearch.jobs.check", "expire"),
    "jobs.runs": ("app.jobsearch.gaps.runs", "expire"),
    "jobs.drafts": ("app.jobsearch.gaps.drafts", "expire"),
    "jobs.coach": ("app.jobsearch.gaps.coach", "expire"),
    "jobs.master_claims": ("app.jobsearch.resume.cleansed_master", "ensure"),
    "jobs.master_record": ("app.jobsearch.resume.structured_profile", "ensure"),
    "browser.leases": ("app.jobsearch.browser.leases", "expire"),
    "tagging": ("app.jobsearch.work.tagging", "settle"),
    "catch_up": ("app.jobsearch.missions.catch_up", "hand_on"),
    "queued": ("app.jobsearch.work.session_kinds", "pull"),
    "claims": ("app.jobsearch.work.hand_outs", "expire"),
    "tabs": ("app.jobsearch.browser.tabs", "expired"),
}


@pytest.fixture
def parts(monkeypatch) -> list[tuple[str, dict]]:
    """Every call a housekeeping part makes, by name and by the halves asked for, recorded in place of being made."""
    import importlib

    calls: list[tuple[str, dict]] = []
    for name, (module, attr) in PARTS.items():
        def make(name=name):
            async def record(*args, **kwargs):
                calls.append((name, kwargs))
                return None

            return record

        monkeypatch.setattr(importlib.import_module(module), attr, make())
    return calls


def names(calls: list[tuple[str, dict]]) -> list[str]:
    return [n for n, _ in calls]


async def settle(parts_to_run=None) -> dict[str, int]:
    async with db.session_factory()() as session:
        did = await housekeeping.settle(session, OWNER, parts_to_run)
        await session.commit()
    return did


OLD_ORDER = ["search_steps", "jobs.parse", "jobs.master_claims", "jobs.master_record", "jobs.check", "jobs.runs",
             "jobs.drafts", "jobs.coach", "browser.leases", "tagging", "catch_up", "queued", "claims", "tabs"]


# --- the old pass ---------------------------------------------------------------------------------------------------


async def test_the_default_pass_does_all_eight_parts_in_the_old_order(js, parts):
    did = await settle()
    assert names(parts) == OLD_ORDER
    assert list(did) == ["search_steps", "jobs", "browser", "tagging", "catch_up", "queued", "claims", "tabs"]
    assert all(kwargs == {} for _, kwargs in parts), "no half is left out for a caller that asked for none"


async def test_the_parts_of_the_ticks_the_browser_and_the_pull_are_the_parts_of_the_old_pass():
    moved = {*housekeeping.MINUTE, *housekeeping.HOUR}
    assert moved | {"browser", "queued"} | {"jobs"} == set(housekeeping.ORDER) | {"jobs_late", "jobs_old"}
    assert not set(housekeeping.MINUTE) & set(housekeeping.HOUR)
    assert housekeeping.LEASES == ("leases",) and "leases" not in housekeeping.ORDER


# --- the halves -----------------------------------------------------------------------------------------------------


async def test_the_minute_parts_notice_the_clock_and_leave_the_old_rows_and_the_retries_to_the_hour(js, parts):
    did = await settle(housekeeping.MINUTE)
    assert names(parts) == ["search_steps", "jobs.parse", "jobs.check", "jobs.runs", "jobs.drafts", "jobs.coach",
                            "tagging", "catch_up"]
    assert list(did) == list(housekeeping.MINUTE)
    assert all(kwargs == {"drop_old": False} for n, kwargs in parts if n.startswith("jobs."))


async def test_the_hour_parts_drop_the_old_rows_make_the_masters_jobs_again_and_tidy_claims_and_tabs(js, parts):
    did = await settle(housekeeping.HOUR)
    assert names(parts) == ["jobs.parse", "jobs.master_claims", "jobs.master_record", "jobs.check", "jobs.runs",
                            "jobs.drafts", "jobs.coach", "claims", "tabs"]
    assert list(did) == list(housekeeping.HOUR)
    assert all(kwargs == {"fail_late": False} for n, kwargs in parts if n.startswith("jobs.") and "master" not in n)


async def _parse_job(state: str, **more) -> None:
    async with db.session_factory()() as session:
        session.add(ParseJob(owner=OWNER, kind=ParseKind.POSTING, text="x" * 60, state=state, **more))
        await session.commit()


async def _jobs() -> list[tuple[str, str | None]]:
    async with db.session_factory()() as session:
        return sorted((j.state, j.error) for j in (await session.scalars(select(ParseJob))).all())


async def test_the_minute_fails_a_job_no_session_took_and_keeps_the_old_row_which_the_hour_then_drops(js):
    now = datetime.now(UTC)
    await _parse_job(ParseState.WAITING, created_at=now - timedelta(minutes=10))  # no agent: nobody took it in time
    await _parse_job(ParseState.DONE, created_at=now - timedelta(days=2))  # ended and kept past its day
    await settle(housekeeping.MINUTE)
    states = await _jobs()
    assert [s for s, _ in states] == ["done", "failed"], "the late one failed; the old row is still there"
    await settle(housekeeping.HOUR)
    assert [s for s, _ in await _jobs()] == ["failed"], "the hour drops the old row (and fails nothing more)"


async def test_the_hour_does_not_fail_a_late_job_and_the_minute_does_not_drop_an_old_row(js):
    from app.jobsearch.jobs import parse

    now = datetime.now(UTC)
    await _parse_job(ParseState.WAITING, created_at=now - timedelta(minutes=10))
    async with db.session_factory()() as session:
        await parse.expire(session, OWNER, fail_late=False)
        await session.commit()
    assert [s for s, _ in await _jobs()] == ["waiting"]
    await _parse_job(ParseState.DONE, created_at=now - timedelta(days=2))
    async with db.session_factory()() as session:
        await parse.expire(session, OWNER, drop_old=False)
        await session.commit()
    assert [s for s, _ in await _jobs()] == ["done", "failed"]


# --- the ticks ------------------------------------------------------------------------------------------------------


def ticks_only(clock=None) -> loop.Dispatcher:
    table = {t: h for t, h in handlers.HANDLERS.items() if t in queue_topics.CLOCK}
    return loop.Dispatcher(OWNER, clock=clock or (lambda: datetime.now(UTC)), table=table, gaps=False)



async def test_the_minute_and_hour_ticks_repost_themselves_and_survive_a_restart(js, parts):
    d = ticks_only()
    await d.start_up()
    assert sorted(await posted_now()) == [("tick.hour", ""), ("tick.minute", "")]
    assert await d.pass_once() == 2
    async with db.session_factory()() as session:
        rows = {t.topic: t for t in (await session.scalars(select(Todo))).all()}
    assert set(rows) == {"tick.minute", "tick.hour"}
    assert timedelta(seconds=55) < rows["tick.minute"].not_before - datetime.now(UTC) <= timedelta(minutes=1)
    assert timedelta(minutes=59) < rows["tick.hour"].not_before - datetime.now(UTC) <= timedelta(hours=1)
    assert await d.pass_once() == 0, "neither is due again yet"

    again = ticks_only()  # the app restarted: a new dispatcher, the same table
    await again.start_up()
    assert sorted(await posted_now()) == [("tick.hour", ""), ("tick.minute", "")], "no second tick is made"
    later = datetime.now(UTC) + timedelta(seconds=61)
    assert await ticks_only(lambda: later).pass_once() == 1, "the minute only"
    assert names(parts).count("tagging") == 2 and names(parts).count("claims") == 1


async def test_ticks_that_fell_due_while_the_app_was_down_run_once_at_the_start_and_not_once_for_each_missed(js, parts):
    long_ago = datetime.now(UTC) - timedelta(days=3)
    async with db.session_factory()() as session:
        for topic in queue_topics.CLOCK:
            await session.execute(insert(Todo).values(owner=OWNER, topic=topic, key="", posted_at=long_ago,
                                                      not_before=long_ago, cause="tick"))
        await session.commit()
    d = ticks_only()
    await d.start_up()
    assert await d.drain() == 2
    assert names(parts).count("tagging") == 1 and names(parts).count("claims") == 1


async def test_the_minute_tick_runs_the_minute_parts_and_asks_the_probes(js, parts, monkeypatch):
    asked: list[str] = []

    async def probe(owner, tally=None):
        asked.append(owner)
        return []

    monkeypatch.setattr(handlers, "probe", probe)
    d = ticks_only()
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue_topics.TICK_MINUTE, cause="test")
        await session.commit()
    assert await d.pass_once() == 1
    assert names(parts) == ["search_steps", "jobs.parse", "jobs.check", "jobs.runs", "jobs.drafts", "jobs.coach",
                            "tagging", "catch_up", "browser.leases"] and asked == [OWNER], (
        "the leases that ran out are the minute's too, since phase 4")


async def test_the_hour_tick_runs_the_hour_parts_says_what_was_handled_and_posts_the_sweep(js, parts, caplog):
    caplog.set_level(logging.INFO)
    d = ticks_only()
    d.tally.note("posting.sift", 3, 12.0)
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue_topics.TICK_HOUR, cause="test")
        await session.commit()
    swept = ("posting.sift", "posting.roles", "role.details")
    d.table = {**d.table, **{t: handlers.HANDLERS[t] for t in swept}}
    assert await d.pass_once() == 1
    assert names(parts) == ["jobs.parse", "jobs.master_claims", "jobs.master_record", "jobs.check", "jobs.runs",
                            "jobs.drafts", "jobs.coach", "claims", "tabs"]
    assert ["work-queue handled in the last hour: posting.sift=3 (waited at most 12s)"] == [
        m for m in caplog.messages if "handled in the last hour" in m]
    async with db.session_factory()() as session:
        sweeps = [(t.topic, t.key, t.cause) for t in (await session.scalars(select(Todo))).all()
                  if t.topic in swept]
    assert sorted(sweeps) == [(t, "*", "sweep") for t in sorted(swept)]
    assert "posting.sift" not in d.tally.handled, "and the tally starts again"


async def test_the_hour_tick_clears_the_old_to_dos_nothing_takes_and_leaves_the_ones_the_dispatcher_does(
        js, parts, monkeypatch):
    long_ago = datetime.now(UTC) - timedelta(hours=5)
    async with db.session_factory()() as session:
        for topic in ("rules.changed", "posting.sift"):
            await session.execute(insert(Todo).values(owner=OWNER, topic=topic, key="", posted_at=long_ago,
                                                      not_before=datetime.now(UTC) + timedelta(hours=1), cause="test"))
        await session.commit()
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue_topics.TICK_HOUR, cause="test")
        await session.commit()
    table = {t: h for t, h in handlers.HANDLERS.items() if t in (*queue_topics.CLOCK, "posting.sift")}
    d = loop.Dispatcher(OWNER, table=table, gaps=False)
    assert await d.pass_once() == 1
    assert ("rules.changed", "") not in await posted_now(), (
        "nothing takes rules.changed yet (its catch-ups): the janitor removed it")
    assert ("posting.sift", "") in await posted_now(), "a consumed topic is the dispatcher's to delete"


async def test_the_janitor_does_not_touch_the_topics_the_dispatcher_takes(js):
    long_ago = datetime.now(UTC) - timedelta(hours=5)
    async with db.session_factory()() as session:
        for topic in ("rules.changed", "posting.sift", "tick.hour", "role.details"):
            await session.execute(insert(Todo).values(owner=OWNER, topic=topic, key="", posted_at=long_ago,
                                                      not_before=long_ago, cause="test"))
        await session.commit()
    assert await janitor.prune() == {"rules.changed": 1}, "only the topic nothing takes is cleared"
    assert sorted(await posted_now()) == [("posting.sift", ""), ("role.details", ""), ("tick.hour", "")]


# --- the sweep ------------------------------------------------------------------------------------------------------


async def _quiet_unfiltered_posting() -> None:
    """A posting that arrived by a write that posted nothing: only a probe or the sweep can find it."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.execute(insert(JobPosting).values(
            id=uuid.uuid4(), owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id="quiet", match_key="k",
            company="Fabrikam", title="Director, Data Governance",
            first_seen=datetime.now(UTC) - timedelta(days=2), last_seen=datetime.now(UTC), location="Remote",
            work_mode="remote"))
        await session.commit()


async def test_the_hourly_sweep_posts_the_catch_all_and_counts_what_it_caught(js, caplog):
    caplog.set_level(logging.WARNING)
    await _quiet_unfiltered_posting()
    table = {t: h for t, h in handlers.HANDLERS.items() if t in (queue_topics.TICK_HOUR, "posting.sift")}
    d = loop.Dispatcher(OWNER, table=table, gaps=False)
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue_topics.TICK_HOUR, cause="test")
        await session.commit()
    with pytest.MonkeyPatch.context() as patch:
        async def nothing(self, *args, **kwargs):
            return {}

        patch.setattr(housekeeping, "settle", lambda *a, **k: nothing(None))
        await d.drain()
    assert d.tally.caught == {"posting.sift": 1}
    assert [m for m in caplog.messages if "sweep_caught topic=posting.sift n=1" in m]
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting))).one()
        assert posting.filtered_at is not None, "and the catch was worked, as the old turn would have"


async def test_a_start_posts_the_catch_all_so_what_changed_while_the_app_was_down_is_looked_at_and_not_counted(
        js, caplog):
    caplog.set_level(logging.WARNING)
    await _quiet_unfiltered_posting()
    table = {t: h for t, h in handlers.HANDLERS.items() if t == "posting.sift"}
    d = loop.Dispatcher(OWNER, table=table, gaps=False)
    await d.start_up()
    assert await d.drain() == 1
    assert d.tally.caught == {} and not [m for m in caplog.messages if "sweep_caught" in m]
    async with db.session_factory()() as session:
        assert (await session.scalars(select(JobPosting))).one().filtered_at is not None


# --- an idle hour ---------------------------------------------------------------------------------------------------


async def load_profile_for_idle() -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()


async def test_an_idle_app_runs_no_statement_that_writes_or_locks_a_role_or_a_posting_in_an_hour(js):
    """A clock stepping through the minutes of an hour, one at a time, over an app with nothing to do: the dispatcher's
    looks and the minute tick's work read the to-do table and the probes, and write only the ticks' own rows. It stops a
    little short of the hour: from phase 3 the hour tick's sweep runs the pipeline's whole turn once, which locks the
    roles it reads (`test_the_hours_sweep_runs_the_whole_turn_once_and_nothing_else_locks_a_role`)."""
    await load_profile_for_idle()
    statements: list[str] = []

    def noted(_conn, _cursor, statement, *_rest) -> None:
        statements.append(statement)

    now = [datetime.now(UTC)]
    d = loop.Dispatcher(OWNER, clock=lambda: now[0], gaps=False)
    await d.start_up()
    await d.drain()  # the start's own work: the first ticks and the catch-all
    now[0] += timedelta(seconds=5)  # the hour tick's own posts were stamped by the real clock: due a moment later
    await d.drain()
    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        for _minute in range(58):
            now[0] += timedelta(seconds=61)
            await d.drain()
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    probes = [s for s in statements if "count(*)" in s.lower() and "js_postings.id" in s.lower()]
    assert len(probes) >= 100, f"the minute ticks asked their two probes each minute ({len(probes)} asks in an hour)"
    for sql in statements:
        lowered = sql.lower()
        touches = "js_opportunities" in lowered or "js_postings" in lowered
        writes = lowered.lstrip().startswith(("update", "insert", "delete"))
        assert not (touches and ("for update" in lowered or writes)), (
            f"a write or a lock on a role or a posting while idle: {sql[:200]}")


async def _an_hour_of_minutes(monkeypatch, *, asked: bool) -> dict[str, int]:
    """How often the pull, the browser's refresh and the runner dispatcher's pass are made in an hour of minutes stepped
    one at a time over an app with nothing to do (the start's own work is over first); `asked`: a free pool session has
    asked for work all the hour."""
    from collections import Counter

    from app.jobsearch.browser import queue as browser_queue
    from app.jobsearch.work import dispatcher, session_kinds

    await load_profile_for_idle()
    calls: Counter[str] = Counter()
    real = {"pull": session_kinds.pull, "refresh": browser_queue.refresh, "walk": dispatcher.dispatch_now}

    def counted(name: str):
        async def call(*args, **kwargs):
            calls[name] += 1
            return await real[name](*args, **kwargs)

        return call

    monkeypatch.setattr(session_kinds, "pull", counted("pull"))
    monkeypatch.setattr(browser_queue, "refresh", counted("refresh"))
    monkeypatch.setattr(dispatcher, "dispatch_now", counted("walk"))
    if asked:
        session_kinds._pool_asked[OWNER] = datetime.now(UTC)
    now = [datetime.now(UTC)]
    d = loop.Dispatcher(OWNER, clock=lambda: now[0], gaps=False)
    await d.start_up()
    await d.drain()  # the start's own work: the first ticks and the catch-all
    now[0] += timedelta(seconds=5)
    await d.drain()
    calls.clear()
    for _minute in range(58):
        now[0] += timedelta(seconds=61)
        if asked:
            session_kinds._pool_asked[OWNER] = now[0]  # a lane asks every two minutes: the ask always stands
        await d.drain()
    return dict(calls)


async def test_an_idle_hour_makes_no_pull_and_no_refresh_and_the_walk_once_a_minute_as_the_quiet_pace_did(
        js, monkeypatch):
    """The saving of phase 4: the pull ran 302 times and the refresh 94 times an hour with nothing to do, and with no
    free session asking and nothing changed they run no more than at the start and at the hour's sweep."""
    got = await _an_hour_of_minutes(monkeypatch, asked=False)
    assert got.get("pull", 0) == 0 and got.get("refresh", 0) == 0, got
    assert got["walk"] == 58, "once a minute, the pace of a pass that found nothing"


async def test_with_a_free_session_asking_all_hour_the_pull_runs_once_in_five_minutes_and_the_refresh_not_at_all(
        js, monkeypatch):
    """What the minute tick's nudge costs while a lane is idle and asking (the hours, the day's room and the gap between
    searches change with no write): one pull every five minutes, against one every ten seconds (302 an hour). The
    review of phase 4 found the first build, a nudge every minute (58 pulls, about 55M rows or 21% of today's quiet
    hour), over the gate of "under about a fifth", where the design's model took the nudge as ten more pulls. The
    start made the first nudge, so in fifty-eight minutes of 61 seconds (the clock begins 5 seconds on) the nudge
    comes at minutes 5, 10 ... 55: eleven."""
    got = await _an_hour_of_minutes(monkeypatch, asked=True)
    assert got["pull"] == 11 and got.get("refresh", 0) == 0, got
    assert got["walk"] == 58, "the walk is still once a minute: it is the quiet pace the old loop kept"


async def test_the_hours_sweep_runs_the_whole_turn_once_and_nothing_else_locks_a_role(js, monkeypatch):
    """The hour tick posts the catch-all for the turn's topics and the clock's look at the deck: one turn, whole form,
    and every statement that locks or writes a role or a posting in that minute is the turn's own."""
    await load_profile_for_idle()
    inside: list[bool] = []
    turns: list[int] = []
    real = schedule.queued_turn

    async def counting(owner):
        turns.append(1)
        inside.append(True)
        try:
            return await real(owner)
        finally:
            inside.pop()

    monkeypatch.setattr(schedule, "queued_turn", counting)
    statements: list[tuple[str, bool]] = []

    def noted(_conn, _cursor, statement, *_rest) -> None:
        statements.append((statement, bool(inside)))

    now = [datetime.now(UTC)]
    d = loop.Dispatcher(OWNER, clock=lambda: now[0], gaps=False)
    await d.start_up()
    await d.drain()  # the start's own turn and ticks
    now[0] += timedelta(seconds=5)  # the hour tick's own posts were stamped by the real clock: due a moment later
    await d.drain()
    started = len(turns)
    assert started >= 1, "the start: the catch-up for what changed while the app was down"
    for _minute in range(58):
        now[0] += timedelta(seconds=61)
        await d.drain()
    assert len(turns) == started, "and no turn in the minutes between: nothing changed"
    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        now[0] += timedelta(seconds=61)  # the hour tick falls due
        await d.drain()
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    assert len(turns) == started + 1, "one whole turn at the hour"
    for sql, in_turn in statements:
        lowered = sql.lower()
        touches = "js_opportunities" in lowered or "js_postings" in lowered
        writes = lowered.lstrip().startswith(("update", "insert", "delete"))
        assert in_turn or not (touches and ("for update" in lowered or writes)), (
            f"a write or a lock on a role or a posting outside the turn: {sql[:200]}")


async def test_a_to_do_the_dispatcher_cannot_handle_is_never_deleted_silently(js, monkeypatch):
    async def broken(run):
        raise RuntimeError("the filter broke")

    patched = handlers.Handler("posting.sift", "sift", "db", 2, timedelta(0), broken)
    d = loop.Dispatcher(OWNER, table={"posting.sift": patched}, gaps=False)
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue_topics.POSTING_SIFT, cause="test")
        await session.commit()
    await d.pass_once()
    (kept,) = [t for t in await _all_todos() if t.topic == "posting.sift"]
    assert kept.attempts == 1
    assert await janitor.prune(datetime.now(UTC) + timedelta(hours=5)) == {}, "the janitor leaves it to the dispatcher"


async def _all_todos() -> list[Todo]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Todo))).all())
