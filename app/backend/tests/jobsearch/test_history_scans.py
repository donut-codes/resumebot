"""A pass reads the history of the roles it decides on, not the whole history (the work queue spec). The
owner's moves on the board (`placement._hand_lines`) were read for the whole owner at every call, one call for each role
the pass settled, from two tables with no index on the role a line is about (`trail_events`, `js_stage_moves`): a busy
night read very many rows for answers about a few roles. What is pinned here:
a read for some roles gives what the whole read gives for them, `placement` decides the same either way, and each read
that asks by role goes through an index made for it. Made-up data only."""

import re
import uuid

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import regate
from app.jobsearch.models import Opportunity, StageMove
from app.jobsearch.pipeline import board, placement
from app.jobsearch.pipeline.placement import Purpose
from app.timeutil import owner_today
from app.trail import service as trail
from tests.builders import counted
from tests.support import OWNER, PROFILE, load_profile

OTHER = "someone-else"
F, S = stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION


def _role(owner: str, slug: str, status: str = F, **more) -> Opportunity:
    return Opportunity(**{
        "owner": owner, "slug": slug, "company": f"Firm {slug}", "title": f"Programs Lead {slug}", "origin": "scout",
        "match_key": f"{owner}|firm {slug}|programs lead {slug}", "status": status, "found": owner_today(),
        "posted": owner_today(), **more})


async def _hand(owner: str, role_id: uuid.UUID, to: str) -> None:
    """The owner moves the role: the board's own writer (a stage move and its line), in a transaction of its own."""
    async with db.session_factory()() as session:
        await board.move(session, owner, role_id, to, allowed=stages.STAGES)
        await session.commit()


async def _code(role_id: uuid.UUID, to: str) -> None:
    """Code moves the role on later: a status change in a transaction of its own."""
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).status = to
        await session.commit()


async def _line(owner: str, role_id: uuid.UUID | None, *, event: str = placement.MOVED_EVENT, actor: str = "person",
                kind: str = "opportunity") -> None:
    async with db.session_factory()() as session:
        await trail.record(session, owner, event=event, actor=actor, who="Someone", sentence="A line.",
                           link=(kind, role_id) if role_id else None)
        await session.commit()


async def _seed(*, read: bool = False) -> dict[str, uuid.UUID]:
    """Roles whose histories differ in each way the read tells apart: where the owner's hand left them, what moved them
    on since, and the lines that are not the owner's move of a role of theirs."""
    names = ("placed", "spent", "twice", "bare", "app-line", "other-event", "packet", "employer-line")
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=PROFILE["gates"], settings=PROFILE["settings"])
        roles = [_role(OWNER, slug, S, **(counted() if read and n % 2 else {})) for n, slug in enumerate(names)]
        roles.append(_role(OTHER, "theirs", S))
        session.add_all(roles)
        await session.flush()
        ids = {r.slug: r.id for r in roles}
        await session.commit()
    await _hand(OWNER, ids["placed"], F)
    await _hand(OWNER, ids["spent"], F)
    await _code(ids["spent"], S)  # code moved it on after the owner's line: no longer where they put it
    await _hand(OWNER, ids["twice"], F)
    await _hand(OWNER, ids["twice"], S)  # the last line is the one that counts
    await _line(OWNER, ids["app-line"], actor="app")
    await _line(OWNER, ids["other-event"], event="favorite_set")
    await _line(OWNER, ids["packet"], event="packet_asked")
    await _line(OWNER, ids["employer-line"], kind="employer")
    await _line(OWNER, None)  # a line about no role
    await _hand(OTHER, ids["theirs"], F)
    return ids


# --- a read for some roles is the whole read for them ----------------------------------------------------------------


async def test_the_moves_asked_for_some_roles_are_the_whole_owners_moves_restricted_to_them(js):
    ids = await _seed()
    every = list(ids.values())
    async with db.session_factory()() as session:
        whole = await placement._hand_lines(session, OWNER)
        assert whole == {ids["placed"]: True, ids["spent"]: False, ids["twice"]: True}, (
            "what the whole read says is pinned, so the comparisons below are never of two empty answers")
        for asked in (every, [], [ids["placed"]], [ids["spent"], ids["bare"]], [ids["theirs"], ids["placed"]],
                      [uuid.uuid4()], [ids["app-line"], ids["other-event"], ids["packet"], ids["employer-line"]],
                      [ids["twice"], ids["twice"]], every[:3], every[3:]):
            wanted = set(asked)
            assert await placement._hand_lines(session, OWNER, asked) == {
                i: placed for i, placed in whole.items() if i in wanted}, asked


async def test_a_role_with_the_owners_line_and_no_move_at_all_is_where_they_put_it(js):
    """The case the other tests never reach, since the board makes a move for every role it writes: a line, and no move
    to be later than it (a role made before the history kept moves, or a history cleared). Nothing overtook the line,
    so the role stands where the owner put it, in the whole read and in the read by role: `_hand_lines` reads the
    missing move as `True`, which the plain comparison of two times would not (it is `NULL`). `spent` shows what the
    move does: it is `False` with its move and `True` without."""
    ids = await _seed()
    async with db.session_factory()() as session:
        assert (await placement._hand_lines(session, OWNER))[ids["spent"]] is False
    before = await _stand(Purpose.STAGE)
    assert before["spent"].held is not placement.Hold.PLACED
    async with db.session_factory()() as session:
        for slug in ("placed", "spent"):
            assert await session.scalar(select(func.count()).select_from(StageMove).where(
                StageMove.role_id == ids[slug])), f"{slug} has moves to take away"
        await session.execute(delete(StageMove).where(StageMove.role_id.in_([ids["placed"], ids["spent"]])))
        await session.commit()
    async with db.session_factory()() as session:
        assert not await session.scalar(select(func.count()).select_from(StageMove).where(
            StageMove.role_id.in_([ids["placed"], ids["spent"]])))
        assert await placement._hand_lines(session, OWNER) == {
            ids["placed"]: True, ids["spent"]: True, ids["twice"]: True}
        assert await placement._hand_lines(session, OWNER, [ids["spent"], ids["bare"], ids["twice"]]) == {
            ids["spent"]: True, ids["twice"]: True}
        assert await placement._hand_lines(session, OWNER, [ids["placed"]]) == {ids["placed"]: True}
    assert (await _stand(Purpose.STAGE))["spent"].held is placement.Hold.PLACED


async def test_the_moves_of_no_roles_are_asked_of_no_one(js):
    await _seed()
    seen: list[str] = []

    def noted(_conn, _cursor, statement, _parameters, _context, _many):
        seen.append(statement)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        async with db.session_factory()() as session:
            assert await placement._hand_lines(session, OWNER, []) == {}
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    assert seen == [], "no statement is sent for no roles"


async def test_the_moves_of_another_owners_roles_are_not_the_owners(js):
    ids = await _seed()
    async with db.session_factory()() as session:
        assert await placement._hand_lines(session, OTHER, [ids["theirs"], ids["placed"]]) == {ids["theirs"]: True}
        assert await placement._hand_lines(session, OTHER) == {ids["theirs"]: True}


# --- placement decides the same whichever way the moves are read ------------------------------------------------------


async def _stand(purpose: Purpose) -> dict[str, placement.Standing]:
    async with db.session_factory()() as session:
        roles = list((await session.scalars(
            select(Opportunity).where(Opportunity.owner == OWNER)
            .options(defer(Opportunity.analysis), defer(Opportunity.description))
            .execution_options(populate_existing=True))).all())
        stood = await placement.load(session, OWNER, roles, purpose)
        return {r.slug: stood[r.id] for r in roles}


@pytest.mark.parametrize("purpose", list(Purpose))
async def test_every_purpose_decides_the_same_whether_the_moves_are_asked_by_role_or_for_the_whole_owner(
        js, monkeypatch, purpose):
    await _seed(read=True)
    monkeypatch.setattr(placement, "OWNER_WIDE_AT", 1_000)
    by_role = await _stand(purpose)
    monkeypatch.setattr(placement, "OWNER_WIDE_AT", 2)
    assert await _stand(purpose) == by_role
    assert {slug for slug, s in by_role.items() if s.held is not None}, "the roles the owner moved are held"


async def test_the_moves_are_asked_by_role_for_a_few_roles_and_for_the_whole_owner_for_many(js, monkeypatch):
    ids = await _seed()
    asked: list[set[uuid.UUID] | None] = []
    real = placement._hand_lines

    async def noted(session, owner, role_ids=None):
        asked.append(None if role_ids is None else set(role_ids))
        return await real(session, owner, role_ids)

    monkeypatch.setattr(placement, "_hand_lines", noted)
    async with db.session_factory()() as session:
        roles = [await session.get(Opportunity, ids[slug], options=[defer(Opportunity.analysis),
                                                                    defer(Opportunity.description)])
                 for slug in ("placed", "spent", "bare")]
        await placement.load(session, OWNER, roles, Purpose.STAGE)
        await placement.one(session, OWNER, roles[0], Purpose.GATES)
        monkeypatch.setattr(placement, "OWNER_WIDE_AT", 2)
        await placement.load(session, OWNER, roles, Purpose.STAGE)
        await placement.one(session, OWNER, roles[0], Purpose.GATES)
    assert asked == [{r.id for r in roles}, {roles[0].id}, None, {roles[0].id}]


# --- the reads that ask by role go through an index made for them -----------------------------------------------------


BY_OWNER_AND_TIME = ("ix_trail_events_owner_at", "ix_js_stage_moves_owner_at")


async def _plans(call, *, drop: tuple[str, ...] = BY_OWNER_AND_TIME) -> list[str]:
    """The query plans (SQLite's `EXPLAIN QUERY PLAN`) of the selects `call(session)` sends, each planned with the
    parameters it was sent with, and the two indexes by owner and time taken away (in a transaction, rolled back after),
    so the plan names the new index or falls back to scanning the table; which index a planner prefers over tables of a
    dozen rows says nothing."""
    sent: list[tuple[str, object]] = []

    def note(_conn, _cursor, statement, parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            sent.append((statement, parameters))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", note)
    try:
        async with db.session_factory()() as session:
            await call(session)
            await session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", note)
    plans = []
    async with db.engine().connect() as conn:
        for name in drop:
            await conn.exec_driver_sql(f"DROP INDEX {name}")
        for statement, parameters in sent:
            rows = (await conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)).all()
            plans.append("\n".join(row[3] for row in rows))
        await conn.rollback()
    return plans


def _scans(plan: str) -> bool:
    """A bare `SCAN` of one of the two history tables: every row visited (`SEARCH ... USING` is an index at work)."""
    return any(re.fullmatch(r"\s*SCAN (trail_events|js_stage_moves)\s*", line) for line in plan.splitlines())


async def test_the_owners_moves_are_read_through_the_indexes_on_the_roles_they_are_about(js):
    ids = await _seed()
    for asked in (None, [ids["placed"], ids["spent"]]):
        plans = await _plans(lambda session, asked=asked: placement._hand_lines(session, OWNER, asked))
        assert len(plans) == 1
        assert "ix_trail_events_person_lines" in plans[0], f"the lines are not read by role ({asked}):\n{plans[0]}"
        assert "ix_js_stage_moves_role_at" in plans[0], f"the moves are not read by role ({asked}):\n{plans[0]}"
        assert not _scans(plans[0]), plans[0]


async def test_the_stage_a_gate_took_a_role_from_is_read_through_the_index_on_the_role(js):
    ids = await _seed()
    plans = await _plans(lambda session: regate._left(session, [ids["placed"], ids["spent"]]))
    assert len(plans) == 1 and "ix_js_stage_moves_role_at" in plans[0], plans


async def test_the_moves_a_hand_since_asks_for_are_read_through_the_same_line_index(js):
    from app.jobsearch.pipeline import quiet

    ids = await _seed()
    plans = await _plans(lambda session: quiet.hand_since(session, OWNER, [ids["placed"], ids["packet"]]))
    assert len(plans) == 1 and "ix_trail_events_person_lines" in plans[0], plans



# --- the classifier's key counts the owner's moves without visiting them --------------------------------------------


async def test_the_classifiers_key_counts_every_move_of_the_owner_and_none_of_anothers_through_the_index(js):
    from app.jobsearch.fit import classifier

    await _seed()
    async with db.session_factory()() as session:
        keys = {}
        for owner in (OWNER, OTHER):
            state = await classifier._state(session, owner, 5)
            by_id = (await session.execute(select(func.count(StageMove.id), func.max(StageMove.at)).where(
                StageMove.owner == owner))).one()
            assert (state[2], state[3]) == tuple(by_id), "its number and newest time are every move's of the owner"
            keys[owner] = state[2]
        assert keys[OWNER] > keys[OTHER] > 0, "the owner's moves and another's are told apart"

    async def ask(session):
        await classifier._state(session, OWNER, 5)

    plans = [p for p in await _plans(ask, drop=()) if "js_stage_moves" in p]
    assert len(plans) == 1 and "USING COVERING INDEX ix_js_stage_moves_owner_at" in plans[0], plans


# --- the count of what each rule stopped reads only the stopped postings --------------------------------------------


async def test_the_count_of_postings_each_rule_stopped_is_read_through_the_index_made_for_it(js):
    from app.jobsearch.models import JobPosting
    from app.jobsearch.routes import profile as profile_routes
    from app.settings import settings
    from app.timeutil import utc_now

    async with db.session_factory()() as session:
        session.add_all([
            JobPosting(owner=settings.owner, employer_id=uuid.uuid4(), ats="t", ext_id=str(n), match_key=f"k{n}",
                       company="Firm", title="Lead", stop_gate_id=gate, first_seen=utc_now(), last_seen=utc_now())
            for n, gate in enumerate(["location", "location", "compensation", None])])
        await session.commit()
        assert (await profile_routes.gates_stopped(session))["stopped"] == {"location": 2, "compensation": 1}

    async def ask(session):
        await profile_routes.gates_stopped(session)

    plans = [p for p in await _plans(ask, drop=("ix_js_postings_opportunity_id",)) if "js_postings" in p]
    assert len(plans) == 1 and "ix_js_postings_owner_stopped" in plans[0], plans
