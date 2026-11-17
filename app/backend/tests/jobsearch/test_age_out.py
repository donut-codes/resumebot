"""A role keeps getting older after the gates have passed it, so a day can put it past the owner's freshness pass
(docs/PRD.md 40.6, the owner, 2026-11-04). The pass
was once applied only when something else ran the gates over a role, so a
role in Ready to Launch that passes the Must days would have stayed, and a role in Followup needed that ages out
sat for ever. `gating.age_out` is the look for one day and the hour tick runs it once for each day of the owner's.
Every date is counted from a fixed day with an injected clock; everything here is made up."""

import asyncio
import copy
import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, event, select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import age_out, regate
from app.jobsearch.gating import check as gating
from app.jobsearch.models import JobPosting, Opportunity, Packet, Todo
from app.jobsearch.profile import schema as p
from app.jobsearch.profile.gates import Gate
from app.jobsearch.work import housekeeping
from app.jobsearch.work.queue import handlers, loop
from app.jobsearch.work.queue import topics as queue_topics
from app.models.work import TrailEvent
from app.settings import settings
from app.timeutil import owner_zone
from app.trail import service as trail
from tests.jobsearch.work_queue.support import posted_now
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

TODAY = date(2026, 11, 3)
TOMORROW = TODAY + timedelta(days=1)
#: The owner's Must on freshness, as the app saves it: the days since it was posted are within 15.
DIAL = {"id": "dial-freshness-must", "kind": "job_fact", "fact": "posted_days", "op": "<=", "value": 15}
#: The older gate, "postings older than 15 days are gated out", which counts the same days.
OLDER = {"id": "older-than", "kind": "posting_age", "days": 15}
GATES = {"location": {}, "other": [DIAL]}
#: What the dial says of a role posted 16 days ago, counted from the employer's date.
SIXTEEN = ("The days since it was posted is within 15 days "
           "(the days since it was posted 16, counted from the employer's date).")
PRE_APPLY = (stages.FOLLOWUP_NEEDED, stages.LANDING_ZONE, stages.CLEANSED_DATA, stages.FIRST_IMPRESSION,
             stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)


def role(slug: str, status: str, days: int, *, owner: str = OWNER, **more) -> Opportunity:
    """A role the employer dated `days` before the fixed day, with nothing else the gates could stop it on."""
    when = TODAY - timedelta(days=days)
    return Opportunity(**{
        "owner": owner, "slug": slug, "company": f"Fabrikam {slug}", "title": f"Lead {slug}", "fit": 3,
        "origin": "scout", "status": status, "match_key": f"fabrikam {slug}|lead {slug}", "found": when, "posted": when,
        "location": "Remote", "move_type": "remote", **more})


async def by_slug(session, owner: str = OWNER) -> dict[str, Opportunity]:
    return {r.slug: r for r in (await session.scalars(select(Opportunity).where(Opportunity.owner == owner))).all()}


async def history(session, row: Opportunity) -> list[TrailEvent]:
    return list((await session.scalars(select(TrailEvent).where(
        TrailEvent.link_kind == "opportunity", TrailEvent.link_id == row.id).order_by(TrailEvent.at))).all())


async def look(day: date, owner: str = OWNER) -> age_out.Aged:
    """The age-out for `day`, in a transaction of its own, as the hour tick runs it."""
    async with db.session_factory()() as session:
        aged = await age_out.run(session, owner, today=day)
        await session.commit()
    return aged


# --- the look -------------------------------------------------------------------------------------------------------


async def test_a_role_a_day_ages_past_the_pass_is_stopped_at_every_stage_before_the_application(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add_all(role(stage, stage, 15) for stage in PRE_APPLY)
        await session.commit()

    assert await look(TODAY) == age_out.Aged(checked=6, closed=0, left=0), "15 days is within 15 days"
    async with db.session_factory()() as session:
        assert {r.status for r in (await by_slug(session)).values()} == set(PRE_APPLY)

    assert await look(TOMORROW) == age_out.Aged(checked=6, closed=6, left=0)
    async with db.session_factory()() as session:
        for slug, row in (await by_slug(session)).items():
            assert row.status == stages.GATED_OUT, f"{slug} stayed"
            assert row.screened_out_reason == SIXTEEN
            assert (row.screened_out_gate_id, row.screened_out_step) == ("dial-freshness-must", "gate")
            line = [e for e in await history(session, row) if e.event == "regate_stopped"][0]
            assert line.sentence == f"Gated out: {SIXTEEN}"
            assert line.detail.startswith("A new day began, so the gates were checked again.")


async def test_a_role_at_applied_or_later_or_already_closed_is_not_read(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add_all([
            role("applied", stages.APPLIED, 40), role("talks", stages.ACTIVE_DISCUSSION, 40, company="Contoso"),
            role("rejected", stages.REJECTED, 40), role("quiet", stages.GONE_QUIET, 40),
            role("closed", stages.GATED_OUT, 40, screened_out_reason="a gate", screened_out_by="location"),
            role("open", stages.FIRST_IMPRESSION, 40)])
        await session.commit()

    assert await look(TODAY) == age_out.Aged(checked=1, closed=1, left=0)
    async with db.session_factory()() as session:
        rows = await by_slug(session)
        assert {s: r.status for s, r in rows.items()} == {
            "applied": stages.APPLIED, "talks": stages.ACTIVE_DISCUSSION, "rejected": stages.REJECTED,
            "quiet": stages.GONE_QUIET, "closed": stages.GATED_OUT, "open": stages.GATED_OUT}
        assert rows["closed"].screened_out_reason == "a gate", "a closed role's stop is left as it is"


async def test_a_role_the_owner_moved_by_hand_stays_where_it_is(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add_all([role("hand", stages.READY_TO_LAUNCH, 40), role("code", stages.READY_TO_LAUNCH, 40)])
        await session.flush()
        rows = await by_slug(session)
        await trail.record(session, OWNER, event="role_moved", sentence="You moved it.", actor="person",
                           link=("opportunity", rows["hand"].id))
        await session.commit()

    assert (await look(TODAY)).closed == 1
    async with db.session_factory()() as session:
        rows = await by_slug(session)
        assert (rows["hand"].status, rows["code"].status) == (stages.READY_TO_LAUNCH, stages.GATED_OUT)


async def test_a_ready_role_is_stopped_and_keeps_its_packet(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        ready = role("ready", stages.READY_TO_LAUNCH, 15)
        session.add(ready)
        await session.flush()
        session.add(Packet(owner=OWNER, role_id=ready.id, version=1, status="ready", brief={}, resume={}, letter={},
                           comp={}, reasoning={}, checks=[]))
        await session.commit()

    assert (await look(TOMORROW)).closed == 1
    async with db.session_factory()() as session:
        row = (await by_slug(session))["ready"]
        assert row.status == stages.GATED_OUT and row.screened_out_reason == SIXTEEN
        stop = [e for e in await history(session, row) if e.event == "regate_stopped"][0]
        assert "Its packet is kept." in stop.detail
        packet = (await session.scalars(select(Packet).where(Packet.role_id == row.id))).one()
        assert (packet.status, packet.version) == ("ready", 1)


async def test_with_no_freshness_pass_set_nothing_is_read_of_the_roles(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"location": {}, "other": [{
            "id": "travel", "kind": "rule", "text": "Travel of 20% or more."}]})
        session.add(role("ancient", stages.FIRST_IMPRESSION, 400))
        await session.commit()

    statements: list[str] = []
    noted = lambda _conn, _cursor, statement, *_rest: statements.append(statement)  # noqa: E731
    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        assert await look(TOMORROW) == age_out.Aged()
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    assert not [s for s in statements if "js_opportunities" in s or "js_postings" in s], statements
    async with db.session_factory()() as session:
        assert (await by_slug(session))["ancient"].status == stages.FIRST_IMPRESSION


async def test_the_older_than_gate_counts_the_same_days_and_stops_the_same_way(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"location": {}, "other": [OLDER]})
        session.add_all([role("edge", stages.FIRST_IMPRESSION, 15), role("young", stages.FIRST_IMPRESSION, 3)])
        await session.commit()

    assert await look(TODAY) == age_out.Aged(checked=2, closed=0, left=0)
    assert await look(TOMORROW) == age_out.Aged(checked=2, closed=1, left=0)
    async with db.session_factory()() as session:
        rows = await by_slug(session)
        assert (rows["edge"].status, rows["young"].status) == (stages.GATED_OUT, stages.FIRST_IMPRESSION)
        assert rows["edge"].screened_out_gate_id == "older-than"
        assert rows["edge"].screened_out_reason.endswith(
            "(16 days old, counted from the employer's date).")


async def test_a_pass_for_one_target_role_is_judged_for_that_roles_roles_alone(js):
    tab = {**DIAL, "id": "dial-for-builders", "roles": ["builders"]}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"location": {}, "other": [tab]})
        session.add_all([role("mine", stages.FIRST_IMPRESSION, 40, target_role="builders"),
                         role("other", stages.FIRST_IMPRESSION, 40, target_role="writers"),
                         role("none", stages.FIRST_IMPRESSION, 40)])
        await session.commit()

    assert (await look(TODAY)).closed == 1
    async with db.session_factory()() as session:
        rows = await by_slug(session)
        assert {s: r.status for s, r in rows.items()} == {
            "mine": stages.GATED_OUT, "other": stages.FIRST_IMPRESSION, "none": stages.FIRST_IMPRESSION}


async def test_a_role_the_pass_cannot_date_is_left_as_it_is(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add(role("undated", stages.FIRST_IMPRESSION, 0, found=None, posted=None))
        await session.commit()

    assert await look(TOMORROW) == age_out.Aged(checked=1, closed=0, left=0)


async def _employer_posting(session, row: Opportunity, ext: str, *, dated: int | None, seen: int) -> None:
    first = datetime.combine(TODAY - timedelta(days=seen), datetime.min.time().replace(hour=12), tzinfo=owner_zone())
    employer = await employers.find_or_make(session, row.owner, row.company)
    session.add(JobPosting(
        owner=row.owner, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=f"{row.slug}-{ext}", company=row.company,
        title=row.title, match_key=row.match_key, first_seen=first, last_seen=first, links=[], location="Remote",
        url=f"https://jobs.example.test/{row.slug}", opportunity_id=row.id,
        posted=TODAY - timedelta(days=dated) if dated is not None else None))


async def _world(session, owner: str) -> None:
    """The same roles for any owner: each a way the posted day is worked out, at stages the look reads, one of them
    held by the owner's hand."""
    rows = {
        "own-date": role("own-date", stages.FIRST_IMPRESSION, 12, owner=owner),
        "redated": role("redated", stages.SECOND_IMPRESSION, 1, owner=owner),
        "mirror": role("mirror", stages.LANDING_ZONE, 5, owner=owner, posted=None),
        "newer-req": role("newer-req", stages.CLEANSED_DATA, 25, owner=owner),
        "tracked": role("tracked", stages.FOLLOWUP_NEEDED, 15, owner=owner, posted=None),
        "young": role("young", stages.READY_TO_LAUNCH, 2, owner=owner),
        "hand": role("hand", stages.FIRST_IMPRESSION, 60, owner=owner),
    }
    session.add_all(rows.values())
    await session.flush()
    for row, ext, dated, seen in (
            (rows["redated"], "a", 1, 20),  # a board dated it again on a later read: held to the day first seen
            (rows["mirror"], "dated", 40, 5), (rows["mirror"], "site", None, 5),  # the pair: one dated, one not
            (rows["newer-req"], "old", 25, 25), (rows["newer-req"], "new", 11, 11)):  # a newer req keeps it young
        await _employer_posting(session, row, ext, dated=dated, seen=seen)
    await trail.record(session, owner, event="role_moved", sentence="You moved it.", actor="person",
                       link=("opportunity", rows["hand"].id))


async def test_it_stops_the_roles_the_re_gate_would_stop_and_says_what_the_re_gate_says(js):
    """The same world for two owners: one is looked at by the day's look, the other by the whole re-gate for the same
    day. Nothing the one stops differs from what the other stops, in stage, gate, sentence or history."""
    async with db.session_factory()() as session:
        for owner in (OWNER, "other"):
            await load_profile(session, owner, gates=GATES)
            await _world(session, owner)
        await session.commit()

    assert (await look(TOMORROW)).closed == 3
    async with db.session_factory()() as session:
        await regate.run(session, "other", cause="A new day began", today=TOMORROW)
        await session.commit()

    async with db.session_factory()() as session:
        async def shown(owner: str) -> dict:
            rows = await by_slug(session, owner)
            return {s: (r.status, r.screened_out_by, r.screened_out_step, r.screened_out_gate_id,
                        r.screened_out_reason, [(e.event, e.sentence, e.detail) for e in await history(session, r)
                                                if e.event.startswith("regate")]) for s, r in rows.items()}

        aged, regated = await shown(OWNER), await shown("other")
        assert aged == regated
        assert {s for s, v in aged.items() if v[0] == stages.GATED_OUT} == {"redated", "mirror", "tracked"}
        assert aged["hand"][0] == stages.FIRST_IMPRESSION, "the owner's hand is honoured"
        assert aged["newer-req"][0] == stages.CLEANSED_DATA, "a newer req of the role keeps it young"
        assert aged["own-date"][0] == stages.FIRST_IMPRESSION and aged["young"][0] == stages.READY_TO_LAUNCH


async def test_a_look_begun_while_the_writer_turn_is_held_waits_for_it_and_then_handles_each_role_once(js):
    """There is no role to hold on SQLite, only the one writer turn: a look that begins while another transaction has it
    waits (a reader sees nothing changed meanwhile), then stops both roles; the next look finds nothing to stop."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add_all([role("free", stages.FIRST_IMPRESSION, 40), role("busy", stages.FIRST_IMPRESSION, 40)])
        await session.commit()
    holding, let_go = asyncio.Event(), asyncio.Event()

    async def holder():
        async with db.session_factory()() as other:
            await other.execute(select(Opportunity.id).limit(1))  # a writer session's first statement takes the turn
            holding.set()
            await let_go.wait()
            await other.rollback()

    holding_task = asyncio.get_running_loop().create_task(holder())
    await holding.wait()
    looking = asyncio.get_running_loop().create_task(look(TODAY))
    await asyncio.sleep(0.3)
    assert not looking.done(), "the look waits for the turn"
    async with db.read_session_factory()() as reader:
        assert {s: r.status for s, r in (await by_slug(reader)).items()} == {
            "free": stages.FIRST_IMPRESSION, "busy": stages.FIRST_IMPRESSION}, "and nothing is changed meanwhile"
    let_go.set()
    assert await asyncio.wait_for(looking, 10) == age_out.Aged(checked=2, closed=2, left=0)
    await holding_task
    async with db.session_factory()() as session:
        assert {r.status for r in (await by_slug(session)).values()} == {stages.GATED_OUT}
    assert await look(TODAY) == age_out.Aged(checked=0, closed=0, left=0), "each role was handled once"


async def test_a_look_stops_roles_in_slices_that_each_commit(js, monkeypatch):
    monkeypatch.setattr(age_out, "CHUNK", 2)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add_all(role(f"r{n}", stages.FIRST_IMPRESSION, 40) for n in range(5))
        await session.commit()

    assert await look(TODAY) == age_out.Aged(checked=5, closed=5, left=0)


async def test_the_look_is_read_without_the_writers_turn_and_stops_what_the_same_look_in_a_transaction_stops(
        js, monkeypatch):
    """On a large database the look held the file's one writer turn for seconds at an hour tick: its
    read of every role before the application, their postings' dates and the rules. Where the session holds nothing it
    reads in a reader's session (a second writer gets through meanwhile) and takes the turn for each slice's write; in a
    session that has a write in it already it reads in that transaction, as it always did. Both stop the same roles.

    The second writer is not timed: it asks for the turn in the look's own task, in the middle of the look's read. A
    free turn is given without a wait, however slow the machine. A turn the look held would never come (the look is
    waiting for this writer), and the guard every test runs under raises `db.NestedWriter` at once instead."""
    from app.jobsearch.fit import freshness

    async def world() -> None:
        async with db.session_factory()() as session:
            session.add_all([role("old-a", stages.FIRST_IMPRESSION, 40), role("old-b", stages.CLEANSED_DATA, 40),
                             role("young", stages.FIRST_IMPRESSION, 2)])
            await session.commit()

    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        await session.commit()
    await world()
    real = freshness.posted_by_role
    seen: list[tuple[bool, bool]] = []  # (read in a reader's session, a second writer got through)

    async def posted_by_role(session, *args, **kwargs):
        got = False
        if seen:  # the re-gate of a role asks for its own date later, in the slice's transaction
            return await real(session, *args, **kwargs)
        if db.is_reader(session):
            async with db.session_factory()() as other:  # a writer: it needs the turn, and raises if the look holds it
                await other.execute(update(Opportunity).where(Opportunity.slug == "young").values(slug="young"))
                got = True
                await other.rollback()
        seen.append((db.is_reader(session), got))
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(freshness, "posted_by_role", posted_by_role)
    assert await look(TODAY) == age_out.Aged(checked=3, closed=2, left=0)
    assert seen == [(True, True)]
    async with db.session_factory()() as session:
        apart = {s: r.status for s, r in (await by_slug(session)).items()}
        await session.execute(delete(Opportunity))  # the same world again, looked at in a transaction already open
        await session.commit()
    await world()
    seen.clear()
    async with db.session_factory()() as session:
        (await session.scalars(select(Opportunity).limit(1))).all()  # a transaction is open: it holds the turn
        assert await age_out.run(session, OWNER, today=TODAY) == age_out.Aged(checked=3, closed=2, left=0)
        await session.commit()
    assert seen == [(False, False)], "a session with a transaction open is read in itself"
    async with db.session_factory()() as session:
        assert {s: r.status for s, r in (await by_slug(session)).items()} == apart == {
            "old-a": stages.GATED_OUT, "old-b": stages.GATED_OUT, "young": stages.FIRST_IMPRESSION}


async def test_the_check_reads_only_the_roles_posting_dates_and_never_a_role_text(js):
    """Cheap: the roles are read by the columns the day is counted from, and their postings by two. A role's description
    and its research are read only for a role that fails the pass."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add(role("fresh", stages.FIRST_IMPRESSION, 2, description="x" * 500))
        await session.commit()

    statements: list[str] = []
    noted = lambda _conn, _cursor, statement, *_rest: statements.append(statement)  # noqa: E731
    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        assert await look(TODAY) == age_out.Aged(checked=1)
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    reads = [s for s in statements if "FROM js_opportunities" in s]
    assert len(reads) == 1 and "js_opportunities.description" not in reads[0] and "analysis" not in reads[0], reads
    assert not [s for s in statements if "FOR UPDATE" in s], "a role that passes is never locked"


# --- the hour tick --------------------------------------------------------------------------------------------------


@pytest.fixture
def new_york(monkeypatch):
    """The owner's day is in the owner's zone (New York in this test), so a boundary of the owner's day is not UTC's."""
    monkeypatch.setattr(settings, "timezone", "America/New_York")


def at(day: date, hour: int) -> datetime:
    """`hour` o'clock on the owner's local `day`, as a moment."""
    return datetime.combine(day, datetime.min.time().replace(hour=hour), tzinfo=owner_zone()).astimezone(UTC)


def hour_only(clock) -> loop.Dispatcher:
    table = {t: h for t, h in handlers.HANDLERS.items() if t == queue_topics.TICK_HOUR}
    return loop.Dispatcher(OWNER, clock=clock, table=table, gaps=False)


@pytest.fixture
def quiet_housekeeping(monkeypatch):
    async def nothing(*args, **kwargs):
        return {}

    monkeypatch.setattr(housekeeping, "settle", nothing)


async def an_hour_later(d: loop.Dispatcher, clock: list[datetime], moment: datetime) -> int:
    """The clock at `moment`, and the hour tick that has fallen due run once. Returns how many handlers ran."""
    clock[0] = moment
    return await d.pass_once()


async def test_the_hour_tick_stops_the_roles_each_new_day_ages_past_the_pass_and_looks_once_for_a_day(
        js, new_york, quiet_housekeeping, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add_all([role("a", stages.FIRST_IMPRESSION, 15), role("b", stages.READY_TO_LAUNCH, 14),
                         role("c", stages.FOLLOWUP_NEEDED, 13)])
        await session.commit()
    looked: list[date] = []
    real = age_out.run

    async def noted(session, owner, *, today=None, cause=age_out.CAUSE):
        looked.append(today)
        return await real(session, owner, today=today, cause=cause)

    monkeypatch.setattr(age_out, "run", noted)

    async def statuses() -> dict[str, str]:
        async with db.session_factory()() as session:
            return {s: r.status for s, r in (await by_slug(session)).items()}

    clock = [at(TODAY, 9)]
    d = hour_only(lambda: clock[0])
    await d.start_up()
    assert await d.pass_once() == 1
    assert looked == [TODAY] and set((await statuses()).values()) == {
        stages.FIRST_IMPRESSION, stages.READY_TO_LAUNCH, stages.FOLLOWUP_NEEDED}, "15 days is within 15 days"

    for hour in (10, 11, 22):  # the rest of the owner's day: no second look
        assert await an_hour_later(d, clock, at(TODAY, hour)) == 1
    assert looked == [TODAY]

    # 11 pm in New York is already the next day in UTC; the owner's day has not turned over
    assert await an_hour_later(d, clock, at(TODAY, 23) + timedelta(minutes=30)) == 1
    assert looked == [TODAY]

    assert await an_hour_later(d, clock, at(TOMORROW, 0) + timedelta(minutes=30)) == 1
    assert looked == [TODAY, TOMORROW]
    assert await statuses() == {"a": stages.GATED_OUT, "b": stages.READY_TO_LAUNCH, "c": stages.FOLLOWUP_NEEDED}

    assert await an_hour_later(d, clock, at(TOMORROW + timedelta(days=1), 1)) == 1
    assert await statuses() == {"a": stages.GATED_OUT, "b": stages.GATED_OUT, "c": stages.FOLLOWUP_NEEDED}
    assert looked == [TODAY, TOMORROW, TOMORROW + timedelta(days=1)]


async def test_a_stop_the_tick_makes_asks_for_the_turn_that_refills_the_deck(js, new_york, quiet_housekeeping):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        session.add(role("ready", stages.READY_TO_LAUNCH, 40))
        await session.commit()
        await session.execute(delete(Todo))
        await session.commit()

    d = hour_only(lambda: at(TODAY, 9))
    await d.start_up()
    assert await d.pass_once() == 1
    topics = {topic for topic, _key in await posted_now()}
    assert {queue_topics.ROLE_SETTLE, queue_topics.DECK_DIRTY} <= topics, topics


async def test_a_new_dispatcher_after_a_start_looks_again_for_the_same_day(js, new_york, quiet_housekeeping,
                                                                           monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        await session.commit()
    looked: list[date] = []

    async def noted(session, owner, *, today=None, cause=age_out.CAUSE):
        looked.append(today)
        return age_out.Aged()

    monkeypatch.setattr(age_out, "run", noted)
    for moment in (at(TODAY, 9), at(TODAY, 10) + timedelta(minutes=30)):  # the app restarted; the tick table is kept
        d = hour_only(lambda moment=moment: moment)
        await d.start_up()
        assert await d.pass_once() == 1
    assert looked == [TODAY, TODAY]


async def test_a_look_that_fails_is_logged_and_tried_again_at_the_next_tick_and_never_fails_the_tick(
        js, new_york, quiet_housekeeping, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    outcomes = [RuntimeError("the database blinked"), age_out.Aged(checked=3, closed=0, left=0)]
    looked: list[date] = []

    async def noted(session, owner, *, today=None, cause=age_out.CAUSE):
        looked.append(today)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(age_out, "run", noted)
    clock = [at(TODAY, 9)]
    d = hour_only(lambda: clock[0])
    await d.start_up()
    assert await d.pass_once() == 1
    assert ("tick.hour", "") in await posted_now(), "the tick posted itself again: it did not fail"
    assert [m for m in caplog.messages if "the next hour tries again" in m]
    assert await an_hour_later(d, clock, at(TODAY, 11)) == 1
    assert await an_hour_later(d, clock, at(TODAY, 12)) == 1
    assert looked == [TODAY, TODAY], "tried again once, then done for the day"


async def test_a_look_that_left_a_held_role_is_tried_again_at_the_next_tick(
        js, new_york, quiet_housekeeping, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    outcomes = [age_out.Aged(checked=9, closed=2, left=1), age_out.Aged(checked=7, closed=1, left=0)]
    looked: list[date] = []

    async def noted(session, owner, *, today=None, cause=age_out.CAUSE):
        looked.append(today)
        return outcomes.pop(0)

    monkeypatch.setattr(age_out, "run", noted)
    clock = [at(TODAY, 9)]
    d = hour_only(lambda: clock[0])
    await d.start_up()
    assert await d.pass_once() == 1
    assert [m for m in caplog.messages if "age-out: 2 of 9 roles stopped for their age, 1 held" in m]
    assert await an_hour_later(d, clock, at(TODAY, 11)) == 1
    assert await an_hour_later(d, clock, at(TODAY, 12)) == 1
    assert looked == [TODAY, TODAY]


def test_the_look_judges_the_pass_as_the_gate_check_does():
    """`fails` is a second statement of the comparison `gating.results` makes, kept short so the look can read many
    roles; the roles it picks are given to the full check. For every age and every way the pass may be written, they
    agree."""
    for line in (Gate.model_validate(x) for x in (DIAL, OLDER, {**DIAL, "op": "<", "value": 15},
                                                    {**DIAL, "op": ">=", "value": 15})):
        profile = copy.deepcopy(PROFILE)
        profile["gates"] = {"location": {}, "other": [line.model_dump(mode="json")]}
        rules = gating.Rules.of(p.Profile.model_validate(profile), today=TODAY)
        for days in range(0, 40):
            subject = gating.role_subject(
                Opportunity(owner=OWNER, company="Fabrikam", title="Lead", location="Remote", move_type="remote",
                            found=TODAY, posted=TODAY - timedelta(days=days)), None)
            stopped = gating.check(subject, rules) is not None
            assert age_out.fails(line, days) is stopped, f"{line.kind} {line.op} {line.value or line.days}: {days}"
