"""A Must on a dial that waits for its fact holds the role back (docs/PRD.md 46.7): it is kept, never Gated
out, and gets no place in Second Impression, Ready to Launch or the launch queue until the fact arrives; a role
already there drops back to First Impression with the reason. Everything here is made up."""

import asyncio
import copy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import regate
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import dial_hold
from app.models.work import TrailEvent
from app.timeutil import owner_today
from tests.builders import counted
from tests.support import OWNER, PROFILE, load_profile

FIRST, SECOND, LAUNCH = stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH
MUST_YEARS = {"id": "must-years", "kind": "job_fact", "section": "posting", "fact": "experience_years", "op": "<=",
              "value": 5}


def _role(slug: str, status: str, years: str | None = None, **more) -> Opportunity:
    facts = {"facts": {"experience_years": {"value": years, "quote": "a made-up line", "by": "code"}}} if years else {}
    today = owner_today()
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": f"Firm {slug}", "title": f"Lead {slug}", "origin": "scout",
        "match_key": f"firm {slug}|lead {slug}", "status": status, "found": today, "posted": today - timedelta(days=1),
        "gate_facts": facts, "fit": 80, "fit_breakdown": {"math": 1, "percent": 80, "asks": []}, **counted(), **more})


async def _world(*roles: Opportunity, must: bool = True) -> None:
    gates = copy.deepcopy(PROFILE["gates"])
    gates["other"] = [*gates.get("other", []), MUST_YEARS] if must else gates.get("other", [])
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates, settings={**PROFILE["settings"], "apply_per_day": 5})
        session.add_all(roles)
        await session.commit()


async def _status(*, reading: bool = False) -> dict[str, str]:
    """Each role's status; `reading`: in a reader's session, which takes no turn (a writer may hold it meanwhile)."""
    async with (db.read_session_factory() if reading else db.session_factory())() as session:
        rows = await session.scalars(select(Opportunity).execution_options(populate_existing=True))
        return {r.slug: r.status for r in rows}


async def test_a_role_whose_must_waits_gets_no_place_but_is_kept_and_a_role_with_its_fact_does(js):
    await _world(_role("waits", FIRST), _role("fine", FIRST, years="3"), _role("over", FIRST, years="9"))
    async with db.session_factory()() as session:
        await pipeline.rebalance(session, OWNER)
        await session.commit()
    got = await _status()
    assert got["waits"] == FIRST, "waiting for its fact: no place on deck, and not Gated out"
    assert got["fine"] in (SECOND, LAUNCH)
    assert got["over"] != stages.GATED_OUT, "a failed Must is the re-gate's to stop, not the deck's"


async def test_a_role_in_the_deck_when_its_must_begins_to_wait_drops_back_with_the_reason(js):
    await _world(_role("deck", SECOND), _role("ready", LAUNCH), _role("fine", LAUNCH, years="2"),
                 _role("hand", SECOND, years=None))
    async with db.session_factory()() as session:
        await dial_hold.settle(session, OWNER)
        await session.commit()
    got = await _status()
    assert (got["deck"], got["ready"], got["fine"]) == (FIRST, FIRST, LAUNCH)
    async with db.session_factory()() as session:
        ident = await session.scalar(select(Opportunity.id).where(Opportunity.slug == "deck"))
        lines = list(await session.scalars(select(TrailEvent.sentence).where(
            TrailEvent.link_id == ident, TrailEvent.event == dial_hold.HELD_EVENT)))
    assert lines and "waiting for the years of experience asked" in lines[0]


async def test_the_deck_is_read_without_the_writers_turn_and_only_the_roles_to_drop_are_written_under_it(
        js, monkeypatch):
    """The read of the deck's places and the plan are made in a reader's session (a second writer gets through while
    they are read); a settle with nothing to drop takes no turn at all, and one with roles to drop takes it to lock and
    write those."""
    from sqlalchemy import update

    from app.jobsearch.pipeline import placement

    await _world(_role("deck", SECOND), _role("fine", LAUNCH, years="2"))
    real = placement.load
    seen: list[tuple[bool, bool]] = []  # (read in a reader's session, a second writer got through)

    async def load(session, *args, **kwargs):
        got = False
        if db.is_reader(session):
            async with db.session_factory()() as other:
                try:
                    await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.slug == "fine")
                                                         .values(slug="fine")), 1)
                    got = True
                except TimeoutError:  # queued behind the settle: it still holds the writer's turn
                    pass
                await other.rollback()
        seen.append((db.is_reader(session), got))
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(placement, "load", load)
    async with db.session_factory()() as session:
        assert await dial_hold.settle(session, OWNER) == 1
        await session.commit()
    assert seen[0] == (True, True), "the deck was read while a writer held the turn"
    assert await _status() == {"deck": FIRST, "fine": LAUNCH}
    seen.clear()
    async with db.session_factory()() as session:
        assert await dial_hold.settle(session, OWNER) == 0
        assert not session.in_transaction(), "nothing to drop: the settle never took the turn"
    assert seen == [(True, True)]


async def test_saving_hard_pass_drops_the_deck_roles_that_now_wait(js):
    await _world(_role("deck", SECOND), must=False)
    assert (await _status())["deck"] == SECOND
    gates = copy.deepcopy(PROFILE["gates"])
    gates["other"] = [*gates.get("other", []), MUST_YEARS]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates)
        await regate.run(session, OWNER, cause="Hard pass was saved")
        await session.commit()
    assert (await _status())["deck"] == FIRST


async def test_the_role_moves_on_when_its_fact_arrives(js):
    await _world(_role("late", FIRST))
    async with db.session_factory()() as session:
        await pipeline.rebalance(session, OWNER)
        await session.commit()
    assert (await _status())["late"] == FIRST
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.slug == "late"))
        role.gate_facts = {"facts": {"experience_years": {"value": "4", "quote": "a made-up line", "by": "code"}}}
        await session.commit()
    async with db.session_factory()() as session:
        await pipeline.rebalance(session, OWNER)
        await session.commit()
    assert (await _status())["late"] in (SECOND, LAUNCH)


async def test_settle_waits_for_the_turn_of_a_writer_and_then_drops_the_roles_that_wait(js):
    """Found 2026-11-02 (a deadlock at `settle`'s `FOR UPDATE`), when the pass waited for any role
    another transaction held. The database is SQLite with one writer: there is no row to hold, so a writer that has the
    turn is waited for as a whole, in the app, and `settle` then drops every role that waits, once."""
    await _world(_role("busy", SECOND), _role("free", SECOND))
    holds, let_go = asyncio.Event(), asyncio.Event()

    async def the_other_writer() -> None:  # a task of its own: a second writer in one task is `db.NestedWriter`
        async with db.session_factory()() as other:
            await other.execute(select(Opportunity.id).limit(1))  # a writer's transaction: it has the turn
            holds.set()
            await let_go.wait()
            await other.rollback()

    holder = asyncio.ensure_future(the_other_writer())
    await holds.wait()

    async def the_settle() -> int:
        async with db.session_factory()() as session:
            dropped = await dial_hold.settle(session, OWNER)
            await session.commit()
            return dropped

    settling = asyncio.ensure_future(the_settle())
    await asyncio.sleep(0.1)
    assert not settling.done(), "it waits for the turn"
    assert await _status(reading=True) == {"busy": SECOND, "free": SECOND}, "and has dropped nothing meanwhile"
    let_go.set()
    assert await asyncio.wait_for(settling, 5) == 2
    await holder
    assert await _status() == {"busy": FIRST, "free": FIRST}
    async with db.session_factory()() as session:
        assert await dial_hold.settle(session, OWNER) == 0, "nothing is left for a next turn"


async def test_a_settle_that_is_not_given_its_turn_in_time_is_told_busy_and_drops_nothing(js, monkeypatch):
    from app.errors import Busy

    await _world(_role("busy", SECOND))
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 100)
    holds, let_go = asyncio.Event(), asyncio.Event()

    async def the_other_writer() -> None:
        async with db.session_factory()() as other:
            await other.execute(select(Opportunity.id).limit(1))
            holds.set()
            await let_go.wait()
            await other.rollback()

    async def the_settle() -> int:
        async with db.session_factory()() as session:
            return await dial_hold.settle(session, OWNER)

    holder = asyncio.ensure_future(the_other_writer())
    await holds.wait()
    try:
        with pytest.raises(Busy):
            await asyncio.wait_for(asyncio.ensure_future(the_settle()), 3)
    finally:
        let_go.set()
        await holder
    assert await _status() == {"busy": SECOND}


async def test_settle_and_a_pass_that_reads_the_deck_in_the_other_order_take_turns_and_both_finish(js):
    """What was two transactions locking the deck's roles in opposite orders, a deadlock the database ended with an
    error: there is no row lock now, and the pass that has the turn finishes before `settle` begins, so neither is left
    waiting for the other."""
    await _world(_role("first", SECOND), _role("second", SECOND))
    async with db.session_factory()() as session:
        ids = {r.slug: r.id for r in (await session.scalars(select(Opportunity))).all()}
    holds_the_turn, order = asyncio.Event(), []

    async def the_settle() -> int:
        await holds_the_turn.wait()
        async with db.session_factory()() as session:
            dropped = await dial_hold.settle(session, OWNER)
            order.append("settle done")
            await session.commit()
        return dropped

    async def the_pass() -> None:
        async with db.session_factory()() as session:
            await session.execute(select(Opportunity.id).where(Opportunity.id == ids["second"]))
            holds_the_turn.set()
            await asyncio.sleep(0.1)  # settle is asked for meanwhile, and waits
            await session.execute(select(Opportunity.id).where(Opportunity.id == ids["first"]))
            order.append("pass done")
            await session.commit()

    got = await asyncio.wait_for(asyncio.gather(the_settle(), the_pass(), return_exceptions=True), 30)
    assert not [g for g in got if isinstance(g, BaseException)], got
    assert order == ["pass done", "settle done"] and got[0] == 2
