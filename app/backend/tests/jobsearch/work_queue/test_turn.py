"""The pipeline's turn moves to the work queue (the work queue spec phase 3): `role.settle`, `deck.dirty`,
`employer.limit` and `employer.research` (the topics of the turn's steps) are taken by one handler that runs the whole
turn, Launch's wait for a turn is a wait for the to-dos that ask for one, the hour tick posts what only the clock
changes. Everything is made up."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import gate_facts
from app.jobsearch.loops import schedule
from app.jobsearch.models import Opportunity, StageMove, Todo
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.work import dispatcher, housekeeping, queue
from app.jobsearch.work.queue import consume, coverage, handlers, loop, service, topics
from app.models.work import TrailEvent
from app.timeutil import owner_today
from tests.builders import rank_roles, settle_housekeeping
from tests.jobsearch.work_queue.support import posted_now
from tests.support import OWNER, PROFILE, empty_the_tables, load_profile

TODAY = owner_today()
FIRST, SECOND, THIRD = "test.first", "test.second", "test.third"


# --- the data every scenario is made of -----------------------------------------------------------------------------


def _role(slug: str, status: str, **more) -> Opportunity:
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": f"Firm {slug}", "title": f"Programs Lead {slug}", "origin": "scout",
        "match_key": f"firm {slug}|programs lead {slug}", "status": status, "found": TODAY, "posted": TODAY, **more})


SCENARIOS = {
    "roles waiting for their first read": lambda: (
        2, [_role("a", stages.FOLLOWUP_NEEDED), _role("b", stages.FOLLOWUP_NEEDED),
            _role("c", stages.FOLLOWUP_NEEDED)], {"a": 90, "b": 80, "c": 70}),
    "a deck filled by estimate": lambda: (
        2, [_role("a", stages.FIRST_IMPRESSION), _role("b", stages.FIRST_IMPRESSION),
            _role("c", stages.FIRST_IMPRESSION), _role("d", stages.FIRST_IMPRESSION),
            _role("unranked", stages.FIRST_IMPRESSION)], {"a": 90, "b": 80, "c": 70, "d": 3}),
    "a starred role": lambda: (
        2, [_role("star", stages.FIRST_IMPRESSION, favorited_at=datetime.now(UTC)),
            _role("other", stages.FIRST_IMPRESSION)], {"star": 40, "other": 90}),
    "a role closed by hand and a role that moves up": lambda: (
        1, [_role("closed", stages.WITHDREW, ended_reason=stages.I_PASSED), _role("next", stages.FIRST_IMPRESSION),
            _role("later", stages.FIRST_IMPRESSION)], {"next": 80, "later": 60}),
}


async def _arrange(scenario: str) -> None:
    daily, roles, ranks = SCENARIOS[scenario]()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": daily})
        session.add_all(roles)
        await session.flush()
        by_slug = {r.slug: r.id for r in roles}
        await rank_roles(session, {by_slug[slug]: fit for slug, fit in ranks.items()})
        await session.commit()


async def _snapshot() -> dict:
    """What the roles, their places in the line and the trail say, by name (ids differ from run to run)."""
    async with db.session_factory()() as session:
        roles = {r.id: r for r in (await session.scalars(select(Opportunity))).all()}
        marked = await pipeline.marked_ids(session, OWNER)
        moves = (await session.scalars(select(StageMove))).all()
        trail = (await session.scalars(select(TrailEvent))).all()
        await session.commit()
        return {
            "roles": sorted((r.slug, r.status, r.analysis_state, r.id in marked, r.favorited_at is not None)
                            for r in roles.values()),
            "moves": sorted((roles[m.role_id].slug, m.from_status or "", m.to_status) for m in moves),
            "trail": sorted((t.event, t.sentence) for t in trail),
        }


def turn_dispatcher(**more) -> loop.Dispatcher:
    """The app's own dispatcher with the turn's handler alone (no pauses to wait for)."""
    table = {t: h for t, h in handlers.HANDLERS.items() if t in consume.TURN_TOPICS}
    return loop.Dispatcher(OWNER, table=table, gaps=False, **more)


async def _turn_to_dos() -> list[tuple[str, str]]:
    return [t for t in await posted_now() if t[0] in consume.TURN_TOPICS]


async def _put(topic: str, key: str = "", cause: str = "test") -> None:
    async with db.session_factory()() as session:
        queue.post(session, OWNER, topic, [key], cause=cause)
        await session.commit()


async def _all_todos() -> list[Todo]:
    async with db.read_session_factory()() as session:
        return list((await session.scalars(select(Todo).order_by(Todo.id))).all())


def _report(**moved: int) -> schedule.TurnReport:
    return schedule.TurnReport(moved=moved)


# --- the name and its topics ------------------------------------------------------------------------------------------


def test_the_turn_takes_the_topics_of_its_steps_and_the_janitor_leaves_them_to_it():
    assert set(consume.TURN_TOPICS) == {queue.ROLE_SETTLE, queue.DECK_DIRTY, queue.EMPLOYER_LIMIT,
                                        queue.EMPLOYER_RESEARCH}
    assert set(consume.TURN_TOPICS) <= consume.consumed_topics()


def test_one_handler_takes_all_four_topics_and_waits_the_pause_of_the_loop_it_replaces():
    taken = {t: h for t, h in handlers.HANDLERS.items() if t in consume.TURN_TOPICS}
    assert set(taken) == set(consume.TURN_TOPICS) and len({id(h) for h in taken.values()}) == 1
    (turn,) = {id(h): h for h in taken.values()}.values()
    assert set(turn.takes) == set(consume.TURN_TOPICS)
    assert turn.name == consume.TURN and turn.lane == "db" and turn.gap == timedelta(seconds=30)


# --- a handler of several topics --------------------------------------------------------------------------------------


class Spy:
    def __init__(self, work=None, outcome: handlers.Outcome | None = None) -> None:
        self.calls: list[service.Group] = []
        self.work = work
        self.outcome = outcome or handlers.Outcome()

    async def __call__(self, run: handlers.Run) -> handlers.Outcome:
        self.calls.append(run.group)
        if self.work is not None:
            await self.work(run)
        return self.outcome


def _several(spy: Spy, *, gap: timedelta = timedelta(0)) -> dict[str, handlers.Handler]:
    one = handlers.Handler(FIRST, None, "db", 0, gap, spy, also=(SECOND, THIRD))
    return dict.fromkeys(one.takes, one)


def _dispatcher(table: dict, *, gaps: bool = False) -> loop.Dispatcher:
    return loop.Dispatcher(OWNER, table=table, gaps=gaps)


async def test_a_handler_of_several_topics_runs_once_for_the_to_dos_of_all_and_removes_them_all(js):
    spy = Spy()
    d = _dispatcher(_several(spy))
    for topic, key in ((FIRST, "a"), (SECOND, "b"), (THIRD, "*"), (SECOND, "b")):
        await _put(topic, key)
    assert await d.pass_once() == 1
    (group,) = spy.calls
    assert group.topic == FIRST and set(group.keys) == {"a", "b", "*"} and len(group.ids) == 4
    assert await _all_todos() == [], "every one of the four is removed"
    assert d.tally.handled == {FIRST: 1, SECOND: 2, THIRD: 1}, "each topic is counted for the hour's line"


async def test_a_to_do_posted_for_one_of_its_topics_while_it_runs_survives_and_runs_it_again(js):
    go = asyncio.Event()

    async def another_writer():
        """A request's own task: not the handler's, so what the handler suppresses does not apply to it."""
        await go.wait()
        await _put(THIRD, "late")

    writer = asyncio.create_task(another_writer())

    async def meanwhile(run: handlers.Run) -> None:
        if len(spy.calls) == 1:
            go.set()
            await writer

    spy = Spy(meanwhile, handlers.Outcome(cheap=True))
    d = _dispatcher(_several(spy))
    await _put(SECOND, "early")
    assert await d.pass_once() == 1
    assert [(t.topic, t.key) for t in await _all_todos()] == [(THIRD, "late")], "only the ids it read were deleted"
    assert await d.drain() == 1
    assert [g.keys for g in spy.calls] == [("early",), ("late",)]


async def test_a_handlers_own_writes_do_not_post_any_topic_it_takes_but_another_topic_still_posts(js):
    async def work(run: handlers.Run) -> None:
        async with db.session_factory()() as session:
            queue.post(session, OWNER, SECOND, [""], cause="own")
            queue.post(session, OWNER, THIRD, ["x"], cause="own")
            queue.post(session, OWNER, "test.other", ["y"], cause="own")
            await session.commit()

    d = _dispatcher(_several(Spy(work)))
    await _put(FIRST)
    await d.pass_once()
    assert [(t.topic, t.key) for t in await _all_todos()] == [("test.other", "y")]


async def test_a_group_a_person_asked_for_does_not_wait_out_the_pause_but_one_a_writer_posted_does(js):
    spy = Spy()
    d = _dispatcher(_several(spy, gap=timedelta(hours=1)), gaps=True)
    await _put(FIRST, cause="hook:Opportunity.status")
    assert await d.pass_once() == 1
    await _put(FIRST, cause="hook:Opportunity.status")
    assert await d.pass_once() == 0, "inside its pause"
    await _put(SECOND, cause=topics.ASK)
    assert await d.pass_once() == 1, "someone is waiting for a turn: it is not made to wait for the pause"
    assert len(spy.calls) == 2 and await _all_todos() == []


def test_a_group_only_the_sweep_and_the_clock_asked_for_is_swept_and_no_other_is():
    def group(*causes: str) -> service.Group:
        return service.Group(owner=OWNER, topic=FIRST, keys=("*",), ids=(1,), causes=frozenset(causes), attempts=0,
                             waiting_since=datetime.now(UTC))

    assert group("sweep").swept and group("sweep", topics.CLOCK_CAUSE).swept
    assert not group("start").swept and not group("sweep", "hook:x").swept and not group("sweep", topics.ASK).swept
    assert not group(topics.CLOCK_CAUSE).swept, "the clock alone is not the net"


# --- the same data through the old turn and through the queue ---------------------------------------------------------


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_the_queue_leaves_roles_where_the_turn_leaves_them(js, monkeypatch, scenario):
    """The phase's gate: the old loop's turn and the queue's turn, on the same data, leave the same statuses, places in
    the line, moves and trail lines, and the old turn run again has nothing left to do."""
    await _arrange(scenario)
    await schedule.pipeline_pass(OWNER)
    await schedule.pipeline_pass(OWNER)
    old = await _snapshot()
    assert old["moves"], "the scenario moves roles"

    async with db.engine().begin() as conn:
        await empty_the_tables(conn)
    coverage.forget()
    await _arrange(scenario)
    assert await _turn_to_dos(), "the roles saved left to-dos the turn takes"
    await turn_dispatcher().drain()
    assert await _snapshot() == old
    assert await _turn_to_dos() == [], "the to-dos are removed"
    await schedule.pipeline_pass(OWNER)
    assert await _snapshot() == old, "nothing was left for the old loop"


async def test_a_turns_own_writes_ask_for_no_second_turn(js, monkeypatch):
    """The turn's own writes (a role moved up, a place in the line taken) post to-dos of the turn's topics; while it
    runs they are not another look at what it is looking at, so after the drain the table holds none of them."""
    await _arrange("roles waiting for their first read")
    runs: list[int] = []
    real = schedule.queued_turn

    async def counting(owner):
        runs.append(1)
        return await real(owner)

    monkeypatch.setattr(schedule, "queued_turn", counting)
    await turn_dispatcher().drain()
    assert len(runs) == 1, "one run settled the roles; its own writes asked for no second"
    assert await _turn_to_dos() == []


# --- the turn that stopped short --------------------------------------------------------------------------------------


async def test_a_turn_that_ran_out_of_rounds_with_the_last_still_moving_something_says_it_stopped_short(js,
                                                                                                         monkeypatch):
    await _arrange("roles waiting for their first read")
    monkeypatch.setattr(pipeline, "ROUNDS", 1)
    said: list[bool] = []
    for _ in range(8):
        with pipeline.accounting() as account:
            await pipeline.run_in_steps(OWNER)
        said.append(account["more"])
        if not account["more"]:
            break
    assert said[0] is True, "one round was all it had and that round moved roles"
    assert said[-1] is False, "a round that moved nothing is the end of the turn"


async def test_a_turn_given_all_the_rounds_it_needs_does_not_say_it_stopped_short(js):
    await _arrange("roles waiting for their first read")
    with pipeline.accounting() as account:
        await pipeline.run_in_steps(OWNER)
    assert account["more"] is False


async def test_the_queues_turn_posts_itself_again_until_a_round_moves_nothing(js, monkeypatch):
    await _arrange("a deck filled by estimate")
    monkeypatch.setattr(pipeline, "ROUNDS", 1)
    runs: list[int] = []
    real = schedule.queued_turn

    async def counting(owner):
        report = await real(owner)
        runs.append(report.more)
        return report

    monkeypatch.setattr(schedule, "queued_turn", counting)
    await turn_dispatcher().drain()
    assert len(runs) >= 2 and runs[0] is True and runs[-1] is False, runs
    assert await _turn_to_dos() == []


async def test_a_turn_that_read_the_most_gate_facts_it_may_posts_itself_again(js, monkeypatch):
    """`read_gate_facts` takes at most `GATE_FACTS_BATCH * GATE_FACTS_BATCHES` roles a turn; the old loop read the rest
    thirty seconds later, so a turn that was cut by the cap must be asked for again."""
    monkeypatch.setattr(schedule, "GATE_FACTS_BATCH", 1)
    monkeypatch.setattr(schedule, "GATE_FACTS_BATCHES", 2)
    named: list[list] = []

    async def stale(session, owner, *, limit, rules=None):
        named.append([f"id{n}" for n in range(limit)])  # exactly as many as it may take: more may be left
        return named[-1][:limit]

    async def read_batch(session, owner, ids, rules=None):
        return {"looked": len(ids), "stopped": 0, "waits": 0}

    monkeypatch.setattr(gate_facts, "stale", stale)
    monkeypatch.setattr(gate_facts, "read_batch", read_batch)
    with pipeline.accounting() as account:
        assert await schedule.read_gate_facts(OWNER) == 2
    assert account["more"] is True

    async def fewer(session, owner, *, limit, rules=None):
        return ["id0"]

    monkeypatch.setattr(gate_facts, "stale", fewer)
    with pipeline.accounting() as account:
        assert await schedule.read_gate_facts(OWNER) == 1
    assert account["more"] is False, "fewer than it may take: nothing is left"


async def test_a_failing_turn_keeps_its_to_dos_and_backs_off_and_a_waiting_launch_is_not_kept_waiting(js,
                                                                                                    monkeypatch,
                                                                                                    caplog):
    caplog.set_level(logging.WARNING)

    async def broken(owner):
        raise RuntimeError("the deck broke")

    monkeypatch.setattr(schedule, "queued_turn", broken)
    d = turn_dispatcher()
    ids = await schedule.ask_for_turn(OWNER)
    assert ids and await d.pass_once() == 1
    kept = [t for t in await _all_todos() if t.topic in consume.TURN_TOPICS]
    assert kept and all(t.attempts == 1 and "the deck broke" in (t.last_error or "") for t in kept)
    assert [m for m in caplog.messages if "handler of role.settle failed" in m]
    assert await schedule.turn_waiting(ids) is False, "a turn that failed has covered the ask, as the old one did"


# --- what counts as a catch -------------------------------------------------------------------------------------------


async def test_a_clock_driven_change_is_not_counted_as_a_catch(js, monkeypatch, caplog):
    """The hour tick asks for the deck and the employers because time alone moves them (the first 72 hours, the stale
    days, research that has grown old): what the steps that time drives change at that look is the clock's, and what a
    write drives is still a catch. The clock's share is not thrown away: it is logged (`sweep_clock`, never a warning)
    because a writer that did not post for one of those four steps hides inside it (found in review, 2026-11-02)."""
    caplog.set_level(logging.INFO)

    async def timed(owner):
        return _report(ready=2, deck=1, marked=1, employers=1)

    monkeypatch.setattr(schedule, "queued_turn", timed)
    d = turn_dispatcher()
    await _put(topics.ROLE_SETTLE, "*", "sweep")
    await _put(topics.DECK_DIRTY, "*", "sweep")
    await _put(topics.DECK_DIRTY, "", topics.CLOCK_CAUSE)
    await _put(topics.EMPLOYER_RESEARCH, "*", topics.CLOCK_CAUSE)
    await d.drain()
    assert d.tally.caught == {} and not [m for m in caplog.messages if "sweep_caught" in m]
    assert d.tally.clocked == {topics.ROLE_SETTLE: 5}, "the five the clock's look moved are counted apart"
    (line,) = [r for r in caplog.records if "sweep_clock" in r.getMessage()]
    assert line.levelno == logging.INFO and "n=5" in line.getMessage()
    assert all(f"{step}=" in line.getMessage() for step in ("marked", "ready", "deck", "employers")), "by step"

    async def written(owner):
        return _report(ready=2, advanced=3)

    monkeypatch.setattr(schedule, "queued_turn", written)
    await _put(topics.ROLE_SETTLE, "*", "sweep")
    await _put(topics.DECK_DIRTY, "", topics.CLOCK_CAUSE)
    await d.drain()
    assert d.tally.caught == {topics.ROLE_SETTLE: 3}, "the roles a writer should have told the turn about are counted"
    assert [m for m in caplog.messages if "sweep_caught topic=role.settle n=3" in m]
    assert d.tally.clocked == {topics.ROLE_SETTLE: 7}, "and the two the clock moved are the clock's"


async def test_the_check_of_a_role_the_clock_moved_into_ready_is_the_clocks_and_not_a_catch(js, monkeypatch, caplog):
    """A role leaving its first 72 hours comes into Ready to Launch with no writer to post, and the entry check
    (`ready_check`, docs/PRD.md 24.16) follows it in the same turn: it is time-driven like the move it follows, so a
    clock look does not count it as a writer that missed a post. Old: only `marked`, `ready`, `deck` and `employers`
    were."""
    caplog.set_level(logging.INFO)

    async def timed(owner):
        return _report(ready=1, ready_check=1)

    monkeypatch.setattr(schedule, "queued_turn", timed)
    d = turn_dispatcher()
    await _put(topics.ROLE_SETTLE, "*", "sweep")
    await _put(topics.DECK_DIRTY, "", topics.CLOCK_CAUSE)
    await d.drain()
    assert d.tally.caught == {} and not [m for m in caplog.messages if "sweep_caught" in m]
    assert d.tally.clocked == {topics.ROLE_SETTLE: 2}, "the move and its check are the clock's"


async def test_what_the_clock_moved_is_logged_only_where_it_could_have_hidden_a_catch(js, monkeypatch, caplog):
    """A look a writer asked for as well as the sweep and the clock has no catch to hide: nothing is logged for it."""
    caplog.set_level(logging.INFO)

    async def timed(owner):
        return _report(ready=2, deck=1)

    monkeypatch.setattr(schedule, "queued_turn", timed)
    d = turn_dispatcher()
    await _put(topics.ROLE_SETTLE, "*", "sweep")
    await _put(topics.ROLE_SETTLE, "some-role", "a writer")
    await _put(topics.DECK_DIRTY, "", topics.CLOCK_CAUSE)
    await d.drain()
    assert d.tally.caught == {} and d.tally.clocked == {} and not [m for m in caplog.messages if "sweep_c" in m]


async def test_the_sweep_alone_counts_every_step_it_finds_work_in(js, monkeypatch):
    async def timed(owner):
        return _report(ready=2, deck=1)

    monkeypatch.setattr(schedule, "queued_turn", timed)
    d = turn_dispatcher()
    await _put(topics.DECK_DIRTY, "*", "sweep")
    await d.drain()
    assert d.tally.caught == {topics.ROLE_SETTLE: 3}


async def test_a_role_no_writer_told_the_turn_about_is_a_catch_at_the_hours_sweep_and_is_worked_all_the_same(
        js, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    await _arrange("roles waiting for their first read")
    async with db.session_factory()() as session:
        await session.execute(Todo.__table__.delete())  # the writers' to-dos are lost: only the sweep can tell
        await session.commit()
    d = turn_dispatcher()
    await d.start_up()
    await d.drain()
    assert d.tally.caught == {}, "a start is the catch-up for what was missed while down, not a catch"

    async with db.session_factory()() as session:
        await session.execute(Opportunity.__table__.update().values(status=stages.FOLLOWUP_NEEDED))
        await session.commit()
    await handlers.sweep(OWNER, d.swept())
    await d.drain()
    assert d.tally.caught.get(topics.ROLE_SETTLE, 0) >= 1
    assert [m for m in caplog.messages if "sweep_caught topic=role.settle" in m]
    assert {r[1] for r in (await _snapshot())["roles"]} != {stages.FOLLOWUP_NEEDED}, "and the catch was worked"


# --- the hour tick ----------------------------------------------------------------------------------------------------


def _tick_run(swept: tuple[str, ...]) -> handlers.Run:
    now = datetime.now(UTC)
    group = service.Group(owner=OWNER, topic=topics.TICK_HOUR, keys=("",), ids=(1,), causes=frozenset({"tick"}),
                          attempts=0, waiting_since=now)
    return handlers.Run(OWNER, group, now, handlers.Tally(), swept)


async def test_the_hour_tick_asks_the_deck_and_the_employers_for_the_clock_beside_the_sweep(js, monkeypatch):
    async def no_parts(owner, parts):
        return None

    monkeypatch.setattr(handlers, "_housekeeping", no_parts)
    await handlers.tick_hour(_tick_run(consume.TURN_TOPICS))
    rows = sorted((t.topic, t.key, t.cause) for t in await _all_todos())
    assert ("deck.dirty", "", topics.CLOCK_CAUSE) in rows, "the clock's look at the deck"
    assert {("role.settle", "*", "sweep"), ("deck.dirty", "*", "sweep"), ("employer.limit", "*", "sweep"),
            ("employer.research", "*", "sweep")} <= set(rows), "and the sweep's catch-all for each of the four"
    # Changed on purpose (the hour's posts are one transaction now, found in review 2026-11-02): the clock's look at the
    # employers is the same ask as the sweep's (topic `employer.research`, key `*`), and a transaction posts one to-do
    # for each topic and key, so the sweep's row stands for both. It was two rows, handled together, when they were two
    # commits; the clock's cause rides on the deck's to-do, which is what the dispatcher reads (`Group.causes`).
    assert [r for r in rows if r[:2] == ("employer.research", "*")] == [("employer.research", "*", "sweep")]


async def test_the_hour_tick_posts_the_sweep_and_the_clocks_look_in_one_commit(js, monkeypatch):
    """Two commits let the dispatcher, woken by the first, read the sweep's to-dos alone and start a turn whose steps
    the clock then moved: counted as a catch, and a second turn half a minute later (found in review, 2026-11-02). One
    transaction posts both, so one wake and every to-do of the hour is there for the look that follows it."""
    async def no_parts(owner, parts):
        return None

    monkeypatch.setattr(handlers, "_housekeeping", no_parts)
    wakes: list[int] = []
    monkeypatch.setattr(consume, "wake", lambda: wakes.append(1))
    await handlers.tick_hour(_tick_run(consume.TURN_TOPICS))
    assert wakes == [1], "one commit added to-dos"
    assert {t.cause for t in await _all_todos()} >= {"sweep", topics.CLOCK_CAUSE}


async def test_the_turn_the_hour_tick_asks_for_is_one_and_what_time_moved_in_it_is_no_catch(js, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    async def no_parts(owner, parts):
        return None

    async def timed(owner):
        turns.append(1)
        return _report(ready=2, deck=1, marked=1, employers=1)

    turns: list[int] = []
    monkeypatch.setattr(handlers, "_housekeeping", no_parts)
    monkeypatch.setattr(schedule, "queued_turn", timed)
    await handlers.tick_hour(_tick_run(consume.TURN_TOPICS))
    d = turn_dispatcher()
    await d.drain()
    assert turns == [1], "one turn for the sweep and the clock together"
    assert d.tally.caught == {} and not [m for m in caplog.messages if "sweep_caught" in m]
    assert d.tally.clocked == {topics.ROLE_SETTLE: 5}


async def test_the_hour_tick_asks_nothing_for_the_clock_while_the_turn_is_the_old_loops(js, monkeypatch):
    async def no_parts(owner, parts):
        return None

    monkeypatch.setattr(handlers, "_housekeeping", no_parts)
    await handlers.tick_hour(_tick_run(()))
    assert not [t for t in await _all_todos() if t.cause == topics.CLOCK_CAUSE]


# --- the signals that ask for a turn ----------------------------------------------------------------------------------


async def test_a_commit_asks_for_a_turn_by_to_dos_saved_with_it_and_a_rollback_asks_for_none(js, monkeypatch):
    monkeypatch.setattr(schedule, "settings", type("S", (), {"owner": OWNER})())
    async with db.session_factory()() as session:
        await session.scalar(select(Opportunity.id))
        schedule.signal_after_commit(session)
        await session.rollback()
    assert await _all_todos() == []
    async with db.session_factory()() as session:
        await session.scalar(select(Opportunity.id))
        schedule.signal_after_commit(session)
        assert await _all_todos() == [], "not visible before the commit: the same transaction"
        await session.commit()
    rows = sorted((t.topic, t.key, t.cause) for t in await _all_todos())
    assert rows == [("deck.dirty", "", topics.ASK), ("role.settle", "*", topics.ASK)]


async def test_the_posting_steps_ask_for_a_turn_by_two_to_dos(js):
    await handlers._ask_for_a_turn(OWNER)
    assert sorted((t.topic, t.cause) for t in await _all_todos()) == [("deck.dirty", topics.ASK),
                                                                      ("role.settle", topics.ASK)]


# --- Launch waits for a turn ------------------------------------------------------------------------------------------


async def test_launch_waits_for_a_turn_that_began_after_the_ask(js, monkeypatch):
    """`schedule.pipeline_turn`, which Launch awaits, posts the to-dos that ask for a turn and waits until a run that
    read them has finished: the dispatcher's, never the one who asked. A second ask inside the pause of the first is
    answered without waiting the pause out."""
    await _arrange("roles waiting for their first read")
    ran_in: list[str] = []
    real = pipeline.run_in_steps

    async def run(owner, excluded=frozenset()):
        ran_in.append(asyncio.current_task().get_name())
        return await real(owner, excluded)

    monkeypatch.setattr(pipeline, "run_in_steps", run)
    dispatching = asyncio.create_task(loop.Dispatcher(OWNER).run())
    try:
        assert await schedule.pipeline_turn(OWNER, wait=30) is True
        assert ran_in and set(ran_in) == {"queue:role.settle"}, "the dispatcher ran the turn, never the one who asked"
        before = len(ran_in)
        assert await schedule.pipeline_turn(OWNER, wait=30) is True
        assert len(ran_in) > before, "each ask is covered by a turn that began after it"
    finally:
        dispatching.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatching
    statuses = {r[0]: r[1] for r in (await _snapshot())["roles"]}
    assert set(statuses.values()) == {stages.FIRST_IMPRESSION}


async def test_in_the_tests_launches_turn_is_the_whole_turn_run_where_it_is_asked(js, monkeypatch):
    """`tests/conftest.py` gives Launch's turn (`browser/go.py` `_turn`) a stand-in that runs `schedule.pipeline_pass`
    at once, because no test starts a dispatcher for it to wait on."""
    from app.jobsearch.browser import go as apply_go

    turns: list[str | None] = []

    async def counted(owner=None):
        turns.append(owner)
        return True

    monkeypatch.setattr(schedule, "pipeline_pass", counted)
    await asyncio.wait_for(apply_go._turn(OWNER), 5)
    assert turns == [OWNER]


async def test_launch_gives_up_on_an_ask_nothing_answers_after_the_wait_it_was_given(js, caplog):
    caplog.set_level(logging.WARNING)
    assert await schedule.pipeline_turn(OWNER, wait=0.4) is False
    assert [m for m in caplog.messages if "did not finish within" in m]


# --- the health door --------------------------------------------------------------------------------------------------


async def test_the_health_door_still_reports_the_last_pass_of_housekeeping_and_the_dispatcher_with_the_queue_on(
        js, monkeypatch):
    """The two fields are the times the minute tick wrote to the engine's facts (`handlers.stamp_passes`) from the
    housekeeping pass and the runner dispatcher's pass: none before a pass is written, both after."""
    from app.jobsearch.owner_actions import runner

    monkeypatch.setattr(housekeeping, "last_pass", None)
    monkeypatch.setattr(dispatcher, "last_pass", None)
    async with db.session_factory()() as session:
        got = await runner.read(session, OWNER, "health", {})
    assert got["result"]["housekeeping_last_pass"] is None and got["result"]["dispatcher_last_pass"] is None
    await settle_housekeeping(OWNER)
    monkeypatch.setattr(dispatcher, "last_pass", datetime.now(UTC))
    await handlers.stamp_passes(OWNER)
    async with db.session_factory()() as session:
        got = await runner.read(session, OWNER, "health", {})
    assert got["result"]["housekeeping_last_pass"] and got["result"]["dispatcher_last_pass"]
