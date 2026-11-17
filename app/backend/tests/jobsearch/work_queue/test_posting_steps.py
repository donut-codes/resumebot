"""The posting steps move to the work queue (the work queue spec phase 2): the cheap filter, the making of
roles and the read of descriptions run from `posting.sift`, `posting.roles` and `role.details` while the dispatcher
runs, and the pipeline's turn leaves them to it. The same data through the old turn and through the queue gives the
same roles, statuses and trail lines (the phase's gate). Everything is made up."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, insert, select, text
from sqlalchemy.dialects import sqlite

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.gating import sift
from app.jobsearch.loops import schedule
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import ADDED_LINK, JobPosting, Opportunity, StageMove, Todo
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.pool import core as pool
from app.jobsearch.work import queue
from app.jobsearch.work.queue import consume, handlers, loop
from app.models.work import TrailEvent
from tests.jobsearch.work_queue.support import posted_now
from tests.support import EXAMPLE_SYSTEM, OWNER, empty_the_tables, gate_says, load_profile

LONG_AGO = datetime.now(UTC) - timedelta(days=2)


def real_dispatcher(*topics: str) -> loop.Dispatcher:
    """The app's own dispatcher with its own handlers (the ones named, when some are), no pauses to wait for."""
    table = {t: h for t, h in handlers.HANDLERS.items() if not topics or t in topics}
    return loop.Dispatcher(OWNER, table=table, gaps=False)


STEPS = (queue.POSTING_SIFT, queue.POSTING_ROLES)


# --- the data every scenario is made of -----------------------------------------------------------------------------


def _posting(employer, ext_id: str, title: str, **more) -> JobPosting:
    fields = {"owner": OWNER, "employer_id": employer.id, "ats": EXAMPLE_SYSTEM, "ext_id": ext_id,
              "company": employer.name, "title": title, "match_key": match_key(employer.name, title),
              "first_seen": LONG_AGO, "last_seen": LONG_AGO, "links": [], "location": "Remote", "work_mode": "remote",
              "url": f"https://jobs.example.test/{ext_id}", **more}
    return JobPosting(**fields)


async def _employers(session):
    fab = await employers.find_or_make(session, OWNER, "Fabrikam")
    umbrella = await employers.find_or_make(session, OWNER, "Umbrella")
    initech = await employers.find_or_make(session, OWNER, "Initech")
    initech.gated = True
    return fab, umbrella, initech


SCENARIOS = {
    "postings that pass the gates": lambda fab, umbrella, initech: [
        _posting(fab, "1", "Director, Data Governance"), _posting(umbrella, "2", "Programs Lead"),
        _posting(fab, "3", "Payroll Specialist")],
    "postings a gate stops": lambda fab, umbrella, initech: [
        _posting(fab, "1", "Head of Delivery", location="Greater Gotham", work_mode="onsite"),
        _posting(initech, "2", "Data Governance Lead"), _posting(fab, "3", "Delivery Lead", comp_min=60000,
                                                                  comp_max=70000)],
    "a posting that is a role already": lambda fab, umbrella, initech: [
        _posting(fab, "1", "Programs Director 1"), _posting(fab, "2", "Programs Director 2")],
    "a closed posting and an added link nobody has named": lambda fab, umbrella, initech: [
        _posting(fab, "1", "Data governance director", closed_at=LONG_AGO),
        _posting(umbrella, "2", "jobs.example.test/jobs/9", ats="owner", company=ADDED_LINK)],
    "all of those at once": lambda fab, umbrella, initech: [
        _posting(fab, "1", "Director, Data Governance"), _posting(umbrella, "2", "Programs Lead"),
        _posting(fab, "3", "Head of Delivery", location="Greater Gotham", work_mode="onsite"),
        _posting(initech, "4", "Data Governance Lead"), _posting(fab, "5", "Programs Director 1"),
        _posting(fab, "6", "Old Director", closed_at=LONG_AGO)],
}


async def _arrange(scenario: str) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        fab, umbrella, initech = await _employers(session)
        session.add(Opportunity(owner=OWNER, slug="mine", company="Fabrikam", title="Programs Director 1", fit=3,
                                origin="owner", match_key=match_key("Fabrikam", "Programs Director 1"),
                                status=stages.SECOND_IMPRESSION, found=LONG_AGO.date()))
        session.add_all(SCENARIOS[scenario](fab, umbrella, initech))
        await session.commit()


async def _snapshot() -> dict:
    """What the roles, the postings and the trail say, by name (ids differ from run to run)."""
    async with db.session_factory()() as session:
        roles = {r.id: r for r in (await session.scalars(select(Opportunity))).all()}
        return {
            "postings": sorted(
                (p.ext_id, p.filtered_at is not None, p.filtered_by, p.route,
                 roles[p.opportunity_id].title if p.opportunity_id in roles else None)
                for p in (await session.scalars(select(JobPosting))).all()),
            "roles": sorted((r.company, r.title, r.status, r.origin, r.analysis_state, r.target_role)
                            for r in roles.values()),
            "moves": sorted((roles[m.role_id].title, m.from_status or "", m.to_status)
                            for m in (await session.scalars(select(StageMove))).all()),
            "trail": sorted((t.event, t.sentence) for t in (await session.scalars(select(TrailEvent))).all()),
        }


# --- the same data through the old turn and through the queue -------------------------------------------------------


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_the_queue_leaves_roles_where_the_turn_leaves_them(js, monkeypatch, scenario):
    """The phase's gate: the old turn does the filter and the roles itself; then the same data goes through the
    dispatcher's handlers and the turn that leaves those steps to them."""
    await _arrange(scenario)
    await pipeline.run_in_steps(OWNER)
    await pipeline.run_in_steps(OWNER)
    old = await _snapshot()
    assert old["postings"], "the scenario has postings"

    async with db.engine().begin() as conn:
        await empty_the_tables(conn)
    await _arrange(scenario)
    assert ("posting.sift", "") in await posted_now(), "a posting saved leaves its to-do"
    await real_dispatcher(*STEPS).drain()
    await pipeline.run_in_steps(OWNER, frozenset({"sift", "make_roles"}))
    await pipeline.run_in_steps(OWNER, frozenset({"sift", "make_roles"}))
    assert await _snapshot() == old
    assert not [t for t in await posted_now() if t[0] in ("posting.sift", "posting.roles")], "the to-dos are removed"


async def test_the_scenarios_really_make_roles_stop_postings_and_link_the_owners_own(js):
    await _arrange("all of those at once")
    await pipeline.run_in_steps(OWNER)
    snap = await _snapshot()
    by_ext = {p[0]: p for p in snap["postings"]}
    assert by_ext["1"][4] == "Director, Data Governance" and by_ext["2"][4] == "Programs Lead"
    assert by_ext["3"][2] == gate_says()["location"] and by_ext["3"][4] is None
    assert by_ext["4"][2] == "an employer you never work for"
    assert by_ext["5"][4] == "Programs Director 1" and not by_ext["6"][1], "a closed posting is not looked at"


# --- the handlers ---------------------------------------------------------------------------------------------------


async def test_a_posting_that_waits_for_the_filter_is_sifted_from_its_to_do_and_the_roles_follow_from_the_next(js):
    await _arrange("postings that pass the gates")
    assert await posted_now() and ("posting.sift", "") in await posted_now()
    d = real_dispatcher(*STEPS)
    assert await d.pass_once() == 1, "the filter first"
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(JobPosting))).all()
        assert all(p.filtered_at and p.route == "roles" and p.opportunity_id is None for p in rows)
    assert ("posting.roles", "") in await posted_now(), "the filter's own writes post the follow-on work"
    assert ("posting.sift", "") not in await posted_now(), "and not another look of its own"
    assert await d.pass_once() == 1, "then the roles"
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(Opportunity).where(Opportunity.origin != "owner"))).all()) == 3
    assert not [t for t in await posted_now() if t[0] in ("posting.sift", "posting.roles")]


async def test_the_handler_goes_by_the_data_not_the_to_do(js):
    """One to-do was posted and another posting arrived by a write that posted nothing (a statement of the test's own):
    the handler looks at the data as it is, so both are filtered, and a second to-do for the same thing is harmless."""
    await _arrange("postings that pass the gates")
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        await session.execute(insert(JobPosting).values(
            id=uuid.uuid4(), owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id="quiet", match_key="k",
            company="Fabrikam", title="Quiet Director", first_seen=LONG_AGO, last_seen=LONG_AGO, location="Remote",
            work_mode="remote"))
        queue.post(session, OWNER, queue.POSTING_SIFT, cause="test:twice")
        await session.commit()
    await real_dispatcher(*STEPS).drain()
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(Opportunity).where(Opportunity.origin != "owner"))).all()) == 4


async def test_a_run_with_nothing_owed_reads_nothing_of_the_postings_and_builds_no_rules(js, monkeypatch):
    called: list[str] = []

    async def spy(*args, **kwargs):
        called.append("ran")

    monkeypatch.setattr(sift, "sift", spy)
    monkeypatch.setattr(role_from_posting, "make_roles", spy)
    async with db.session_factory()() as session:
        for topic in (queue.POSTING_SIFT, queue.POSTING_ROLES):
            queue.post(session, OWNER, topic, ["*"], cause="sweep")
        await session.commit()
    assert await real_dispatcher(*STEPS).drain() == 2
    assert called == [] and not [t for t in await posted_now() if t[0].startswith("posting.")]


async def test_a_posting_another_run_has_not_finished_is_left_for_a_second_look(js, monkeypatch):
    """`sift` leaves unfiltered what another transaction holds: the handler says it stopped short, and posts itself."""
    await _arrange("postings that pass the gates")
    real = sift.sift

    async def leaves_one(session, owner, profile, *args, **kwargs):
        out = await real(session, owner, profile, *args, **kwargs)
        await session.execute(text("UPDATE js_postings SET filtered_at = NULL WHERE ext_id = '1'"))
        return out

    monkeypatch.setattr(sift, "sift", leaves_one)
    outcome = await handlers.posting_sift(
        handlers.Run(OWNER, _group(queue.POSTING_SIFT), datetime.now(UTC), handlers.Tally()))
    assert outcome.more and outcome.changed == 3


def _group(topic: str):
    from app.jobsearch.work.queue import service

    now = datetime.now(UTC)
    return service.Group(OWNER, topic, ("",), (1,), frozenset({"test"}), 0, now)


async def test_a_posting_saved_while_the_loop_runs_becomes_a_role_within_seconds_not_at_a_turn(js):
    """The whole path with the app's own handlers: the save posts, the commit wakes the loop, the filter runs, its
    writes post the roles' to-do, the roles are made. The old turn's clock (30 seconds) and the idle look (30 seconds)
    are not waited for."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    dispatcher = loop.Dispatcher(OWNER)
    task = asyncio.create_task(dispatcher.run())
    try:
        await asyncio.sleep(0.5)  # the first look is over and the loop waits for a wake
        await _arrange_postings("postings that pass the gates")
        for _ in range(250):  # at most 25 seconds, in tenths: it takes a second or two
            async with db.session_factory()() as session:
                made = (await session.scalars(select(Opportunity).where(Opportunity.origin != "owner"))).all()
            if len(made) == 3:
                break
            await asyncio.sleep(0.1)
        assert len(made) == 3, "the roles were made by the dispatcher's handlers"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def _arrange_postings(scenario: str) -> None:
    """The scenario's postings only (the made-up profile is loaded by the caller)."""
    async with db.session_factory()() as session:
        fab, umbrella, initech = await _employers(session)
        session.add_all(SCENARIOS[scenario](fab, umbrella, initech))
        await session.commit()


# --- the probes -----------------------------------------------------------------------------------------------------


async def _quiet_posting(filtered: bool = False, route: str | None = None, **more) -> None:
    """A posting made by a statement that posts nothing (the test's own), so nothing but the probe can find it."""
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        await session.execute(insert(JobPosting).values(**{
            "id": uuid.uuid4(), "owner": OWNER, "employer_id": employer.id, "ats": EXAMPLE_SYSTEM,
            "ext_id": uuid.uuid4().hex[:6], "match_key": "k", "company": "Fabrikam", "title": "Quiet Director",
            "first_seen": LONG_AGO, "last_seen": LONG_AGO, "filtered_at": LONG_AGO if filtered else None,
            "route": route, **more}))
        await session.execute(delete(Todo))  # making the employer posted; this is about the posting's own
        await session.commit()


async def test_a_probe_that_finds_owed_work_and_no_to_do_posts_one_and_says_a_writer_did_not(js, caplog):
    caplog.set_level(logging.WARNING)
    await _quiet_posting()
    await _quiet_posting(filtered=True, route="roles")
    tally = handlers.Tally()
    assert await handlers.probe(OWNER, tally) == [queue.POSTING_SIFT, queue.POSTING_ROLES]
    assert sorted(await posted_now()) == [("posting.roles", ""), ("posting.sift", "")]
    assert tally.missed == {queue.POSTING_SIFT: 1, queue.POSTING_ROLES: 1}
    said = [m for m in caplog.messages if "work-queue probe" in m]
    assert len(said) == 2 and "wait for the filter" in said[0] and "no posting.sift to-do was posted" in said[0]
    assert await handlers.probe(OWNER, tally) == [], "a to-do is on its way now: nothing is posted twice"


async def test_a_probe_does_not_post_when_a_to_do_waits_even_one_that_is_not_due_yet(js):
    await _quiet_posting()
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.POSTING_SIFT, after=timedelta(minutes=10), cause="test")
        await session.commit()
    assert await handlers.probe(OWNER) == []


async def test_a_probe_finds_nothing_when_nothing_is_owed_or_the_posting_is_closed_stopped_or_a_roles(js):
    await _quiet_posting(filtered=True)  # filtered, not routed to roles: waits for no one
    await _quiet_posting(closed_at=LONG_AGO)
    await _quiet_posting(filtered=True, route="roles", filtered_by="a gate")
    await _quiet_posting(filtered=True, route="roles", company="Added link", ats="owner")  # an unnamed added link
    assert await handlers.probe(OWNER) == []


async def test_a_probe_that_fails_is_logged_and_the_others_still_ask(js, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    await _quiet_posting(filtered=True, route="roles")

    def broken(owner):
        raise RuntimeError("the predicate broke")

    monkeypatch.setattr(handlers, "PROBES", ((consume.SIFT, queue.POSTING_SIFT, broken, "wait for the filter"),
                                             *handlers.PROBES[1:]))
    assert await handlers.probe(OWNER) == [queue.POSTING_ROLES]
    assert [m for m in caplog.messages if "the look for postings that wait for the filter failed" in m]


async def test_each_probe_reads_through_its_partial_index_and_the_indexes_hold_only_what_waits(js):
    names = {"unfiltered": "ix_js_postings_owner_unfiltered", "roles": "ix_js_postings_owner_roles_waiting"}
    probes = {"unfiltered": handlers._sift_where, "roles": handlers._roles_where}
    await _quiet_posting()
    await _quiet_posting(filtered=True, route="roles")
    await _quiet_posting(filtered=True, route="titles")
    async with db.session_factory()() as session:
        for key, where_of in probes.items():
            sql = str(handlers.owed_statement(where_of(OWNER)).compile(
                dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
            plan = "\n".join(row[3] for row in await session.execute(text(f"EXPLAIN QUERY PLAN {sql}")))
            assert names[key] in plan, f"the probe of {key} does not use {names[key]}:\n{plan}"
        held = dict((await session.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'js_postings'"))).all())
    assert "filtered_at IS NULL" in held[names["unfiltered"]] and "route" in held[names["roles"]], "partial indexes"


async def _a_writer_holding_the_turn():
    """A writer that has the file's one turn until `let_go` is set (a task of its own: a second writer in one task is
    `db.NestedWriter`). Returns (the task, let_go)."""
    holds, let_go = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with db.session_factory()() as other:
            await other.execute(select(Opportunity.id).limit(1))  # a writer's transaction: it has the turn
            holds.set()
            await let_go.wait()
            await other.rollback()

    holder = asyncio.ensure_future(hold())
    await holds.wait()
    return holder, let_go


async def test_the_looks_for_owed_postings_take_no_turn_and_a_probe_takes_it_only_to_post(js):
    """Found: `owed` and `probe` read in a writer's session and held the turn
    for the whole read (while the roles probe walked the wrong index). Both looks are a reader's now; a probe
    that finds nothing owed never waits for a writer, and one that posts waits for the turn for the post alone and
    posts what the one-transaction run did."""
    holder, let_go = await _a_writer_holding_the_turn()
    try:
        assert await asyncio.wait_for(handlers.owed(handlers._sift_where(OWNER)), 5) == 0
        assert await asyncio.wait_for(handlers.probe(OWNER), 5) == [], "nothing owed: no turn is asked for"
    finally:
        let_go.set()
        await holder
    await _quiet_posting()
    await _quiet_posting(filtered=True, route="roles")
    holder, let_go = await _a_writer_holding_the_turn()
    try:
        tally = handlers.Tally()
        assert await asyncio.wait_for(handlers.owed(handlers._sift_where(OWNER)), 5) == 1
        assert await asyncio.wait_for(handlers.owed(handlers._roles_where(OWNER)), 5) == 1
        probing = asyncio.ensure_future(handlers.probe(OWNER, tally))
        done, _ = await asyncio.wait({probing}, timeout=0.2)
        assert not done, "the post waits for the writer that has the turn"
    finally:
        let_go.set()
        await holder
    assert await asyncio.wait_for(probing, 5) == [queue.POSTING_SIFT, queue.POSTING_ROLES]
    assert sorted(await posted_now()) == [("posting.roles", ""), ("posting.sift", "")]
    assert tally.missed == {queue.POSTING_SIFT: 1, queue.POSTING_ROLES: 1}


async def test_a_to_do_posted_by_the_writer_that_had_the_turn_is_not_posted_twice(js, monkeypatch):
    """The probe saw nothing waiting in its reader's session, then waited for the turn; the writer that had it posted
    the to-do. The probe asks again under the turn and posts nothing."""
    await _quiet_posting()
    monkeypatch.setattr(handlers, "PROBES", handlers.PROBES[:1])
    holds, let_go = asyncio.Event(), asyncio.Event()

    async def writer() -> None:
        async with db.session_factory()() as other:
            await other.execute(select(Opportunity.id).limit(1))
            holds.set()
            await let_go.wait()
            queue.post(other, OWNER, queue.POSTING_SIFT, cause="writer")
            await other.commit()

    holder = asyncio.ensure_future(writer())
    await holds.wait()
    probing = asyncio.ensure_future(handlers.probe(OWNER))
    done, _ = await asyncio.wait({probing}, timeout=0.2)
    assert not done
    let_go.set()
    await holder
    assert await asyncio.wait_for(probing, 5) == []
    assert [t for t in await posted_now() if t[0] == "posting.sift"] == [("posting.sift", "")]


async def test_the_roles_probe_names_its_route_and_keeps_to_its_partial_index_whatever_the_statistics_say(js):
    """The statement as the app runs it (the route written in, not bound: a partial index is used only for the very
    value it was made for) and with the statistics a real board has: `ANALYZE` counts a NULL as one of many values, so
    `opportunity_id IS NULL` looks like a handful of rows and the planner walked its index, every posting without a
    role."""
    await _quiet_posting(filtered=True, route="roles")
    async with db.session_factory()() as session:
        await session.execute(text("DELETE FROM sqlite_stat1"))
        await session.execute(text(
            "INSERT INTO sqlite_stat1 VALUES ('js_postings', 'ix_js_postings_opportunity_id', '146328 4'), "
            "('js_postings', 'ix_js_postings_owner_roles_waiting', '45 45'), "
            "('js_postings', 'ix_js_postings_owner_first_seen', '146328 146328 253'), "
            "('js_postings', 'ix_js_postings_owner_match_key', '146328 146328 2'), "
            "('js_postings', 'ix_js_postings_employer_id', '146328 211'), "
            "('js_postings', 'ix_js_postings_owner_role', '56521 56521 2'), "
            "('js_postings', 'ix_js_postings_owner_unfiltered', '0 0'), "
            "('js_postings', 'sqlite_autoindex_js_postings_2', '146328 146328 211 1')"))
        await session.execute(text("ANALYZE sqlite_master"))  # the planner reads the rows again
        compiled = handlers.owed_statement(handlers._roles_where(OWNER)).compile(
            dialect=sqlite.dialect(), compile_kwargs={"render_postcompile": True})
        assert "'roles'" in str(compiled), "the route is in the statement"
        params = tuple(compiled.params[k] for k in compiled.positiontup)
        raw = await session.connection()
        plan = "; ".join(row[3] for row in (await raw.exec_driver_sql(f"EXPLAIN QUERY PLAN {compiled}", params)))
        assert "ix_js_postings_owner_roles_waiting" in plan, plan
        await session.rollback()


# --- the turn leaves the moved steps to the dispatcher --------------------------------------------------------------


async def _turn_with_recorders(monkeypatch, *, excluded: frozenset[str] | None = None,
                               queued: bool = False) -> list[str]:
    """The steps of the turn that ran, in order. By default the whole turn (`schedule.pipeline_pass`); with `excluded`
    the turn given those steps to leave out (`schedule.run_turn`); with `queued` the dispatcher's own turn
    (`schedule.queued_turn`)."""
    ran: list[str] = []
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()

    async def sifted(*args, **kwargs):
        ran.append("sift")
        return {"looked_at": 0}

    async def made(*args, **kwargs):
        ran.append("make_roles")
        return 0

    async def read(*args, **kwargs):
        ran.append("details")
        return 0

    monkeypatch.setattr(sift, "sift", sifted)
    monkeypatch.setattr(role_from_posting, "make_roles", made)
    monkeypatch.setattr(pool, "queue_details", read)
    if queued:
        await schedule.queued_turn(OWNER)
    elif excluded is not None:
        await schedule.run_turn(OWNER, excluded)
    else:
        assert await schedule.pipeline_pass(OWNER) is True
    return ran


async def test_the_whole_turn_does_the_three_steps_as_it_always_did(js, monkeypatch):
    assert await _turn_with_recorders(monkeypatch) == ["sift", "make_roles", "details"]


async def test_the_queues_turn_leaves_the_three_steps_to_the_dispatcher(js, monkeypatch):
    assert await _turn_with_recorders(monkeypatch, queued=True) == []


async def test_a_turn_given_the_three_steps_to_leave_out_does_none_of_them(js, monkeypatch):
    assert await _turn_with_recorders(monkeypatch, excluded=schedule.QUEUE_STEPS) == []


@pytest.mark.parametrize("left, ran", [
    ("sift", ["make_roles", "details"]), ("make_roles", ["sift", "details"]), ("details", ["sift", "make_roles"])])
async def test_a_turn_leaves_out_the_step_it_is_told_to_and_does_the_others(js, monkeypatch, left, ran):
    assert await _turn_with_recorders(monkeypatch, excluded=frozenset({left})) == ran


# --- the description read -------------------------------------------------------------------------------------------


@dataclass
class Board:
    """A job system's reader: it gives a page for each posting, noting which it was asked."""

    asked: list[str] = field(default_factory=list)

    async def detail(self, fetch, board, posting):
        self.asked.append(posting["ext_id"])
        return {"text": f"The whole posting for {posting['title']}."}


@pytest.fixture
def board(monkeypatch):
    import httpx

    from app.jobsearch import adapters
    from app.jobsearch.fetch.polite import Fetcher

    reader = Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: reader})
    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0))
    return reader


async def _thin_role(name: str = "new", days_ago: int = 1, detail_at: datetime | None = None) -> None:
    async with db.session_factory()() as session:
        acme = await employers.find_or_make(session, OWNER, "Acme")
        acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": "acme"}
        role = Opportunity(owner=OWNER, slug=name, company="Acme", title=name, match_key=f"acme|{name}",
                           status=stages.CLEANSED_DATA)
        session.add(role)
        await session.flush()
        session.add(JobPosting(
            owner=OWNER, employer_id=acme.id, ats=EXAMPLE_SYSTEM, ext_id=name, company="Acme", title=name,
            match_key=match_key("Acme", name), first_seen=datetime.now(UTC) - timedelta(days=days_ago),
            last_seen=LONG_AGO, links=[], url=f"https://jobs.example.test/{name}", opportunity_id=role.id,
            text="A snippet.", filtered_at=LONG_AGO, detail_at=detail_at))
        await session.flush()
        await session.execute(delete(Todo))  # what arranging the role posted is not what the test is about
        await session.commit()


async def _texts() -> dict[str, str | None]:
    async with db.session_factory()() as session:
        return {p.ext_id: p.text for p in (await session.scalars(select(JobPosting))).all()}


async def test_a_role_lacking_text_has_it_read_from_its_to_do_and_the_turn_is_asked_for(js, board):
    await _thin_role()
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.ROLE_DETAILS, cause="test")
        await session.commit()
    d = real_dispatcher(queue.ROLE_DETAILS)
    assert await d.pass_once() == 1
    assert board.asked == ["new"] and (await _texts())["new"] == "The whole posting for new."
    assert {("role.settle", "*"), ("deck.dirty", "")} <= set(await posted_now()), (
        "the turn is asked for: it ran again straight after the read")
    assert await d.drain() == 1, "text was filled, so it looks once more"
    assert board.asked == ["new"], "and finds nothing more to read"
    assert [t for t in await posted_now() if t[0] == "role.details"] == [("role.details", "retry")], (
        "its own writes post nothing for its topic; the retry waits for its day")


async def test_no_transaction_is_held_across_the_slow_step_of_the_description_read(js, board, monkeypatch):
    """The page is read with no transaction open, by the handler and by the dispatcher that started it."""
    held: list[bool] = []

    async def reads(self, fetch, board_, posting):
        held.append(db.in_transaction())
        return {"text": f"The whole posting for {posting['title']}."}

    monkeypatch.setattr(Board, "detail", reads)
    await _thin_role()
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.ROLE_DETAILS, cause="test")
        await session.commit()
    assert await real_dispatcher(queue.ROLE_DETAILS).pass_once() == 1
    assert held == [False], "the page was read while a transaction was open"


async def test_the_retry_of_a_read_that_gave_nothing_is_a_to_do_due_when_its_days_are_up(js, board, monkeypatch):
    async def nothing(self, fetch, board_, posting):
        board.asked.append(posting["ext_id"])
        return None

    monkeypatch.setattr(Board, "detail", nothing)
    await _thin_role()
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.ROLE_DETAILS, cause="test")
        await session.commit()
    # Changed on purpose in review (the work queue spec phase 2, role.details): it stopped after a read that
    # gave nothing (1 look); a posting a read was begun for is no longer owed, so it looks once more for the next batch
    # (2 looks: the second finds nothing owed).
    assert await real_dispatcher(queue.ROLE_DETAILS).drain() == 2
    assert board.asked == ["new"] and (await _texts())["new"] == "A snippet."
    async with db.session_factory()() as session:
        retry = (await session.scalars(select(Todo).where(Todo.topic == "role.details"))).one()
    assert retry.key == "retry" and retry.cause == "core:details.retry"
    assert timedelta(days=2, hours=23) < retry.not_before - datetime.now(UTC) <= timedelta(days=3)


async def test_a_posting_tried_three_days_ago_is_read_again_when_its_retry_comes_due_and_that_is_no_catch(
        js, board, caplog):
    """No write happens when the days are up: the to-do posted at the attempt is what brings it back, and a run it asks
    for is not the sweep's, so it is not counted as a miss of a writer."""
    caplog.set_level(logging.WARNING)
    await _thin_role(detail_at=datetime.now(UTC) - timedelta(days=4))
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.ROLE_DETAILS, ["retry"], cause="core:details.retry")
        await session.commit()
    d = real_dispatcher(queue.ROLE_DETAILS)
    await d.drain()
    assert board.asked == ["new"]
    assert d.tally.caught == {} and not [m for m in caplog.messages if "sweep_caught" in m]


async def test_a_sweep_that_reads_what_nothing_posted_for_is_a_catch_and_says_so(js, board, caplog):
    caplog.set_level(logging.WARNING)
    await _thin_role()
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.ROLE_DETAILS, ["*"], cause="sweep")
        await session.commit()
    d = real_dispatcher(queue.ROLE_DETAILS)
    await d.drain()
    assert board.asked == ["new"] and d.tally.caught == {queue.ROLE_DETAILS: 1}
    assert [m for m in caplog.messages if "sweep_caught topic=role.details n=1" in m]


async def _asked_for_the_details() -> None:
    async with db.session_factory()() as session:
        queue.post(session, OWNER, queue.ROLE_DETAILS, cause="test")
        await session.commit()


async def test_a_batch_whose_every_read_failed_is_followed_by_the_next_batch_as_the_turn_took_it(
        js, board, monkeypatch):
    """Found in review: `more` followed only what was filled, so a batch of 40 whose reads all failed left the owed
    postings behind it to the hour's sweep, where the old turn took the next 40 thirty seconds later. A posting a read
    was begun for is marked tried and is no longer owed, so the next look takes the next batch, and the run ends when no
    read is begun."""
    async def nothing(self, fetch, board_, posting):
        board.asked.append(posting["ext_id"])
        return None

    monkeypatch.setattr(Board, "detail", nothing)
    monkeypatch.setattr(pool, "DETAILS_PER_TURN", 2)
    for days_ago, name in enumerate("abcde", start=1):
        await _thin_role(name, days_ago=days_ago)
    await _asked_for_the_details()
    d = real_dispatcher(queue.ROLE_DETAILS)
    assert await d.drain() == 4, "two, two, one, and a look that finds nothing owed"
    assert board.asked == ["a", "b", "c", "d", "e"], "newest first, each once"
    assert d.tally.caught == {}
    assert [t for t in await posted_now() if t[0] == "role.details" and t[1] != "retry"] == []


async def test_a_batch_no_read_was_begun_for_is_not_looked_at_again_and_again(js, board, monkeypatch):
    """A posting whose job system has no reader is not marked tried (nothing was tried), so it stays owed: the look must
    not run again for it, or it would run every pause for ever."""
    from app.jobsearch import adapters

    monkeypatch.setattr(adapters, "ADAPTERS", {})
    await _thin_role()
    await _asked_for_the_details()
    assert await real_dispatcher(queue.ROLE_DETAILS).drain() == 1
    assert board.asked == [] and (await _texts())["new"] == "A snippet."
    assert [t for t in await posted_now() if t[0] == "role.details"] == []
