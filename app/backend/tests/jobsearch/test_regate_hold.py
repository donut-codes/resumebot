"""How long the employer re-gate holds the writer's turn (`employer.regate` once held it for whole turns): a slice's
roles are judged before the slice is locked, and the employer ask writes its trail line, settles the boards it no longer
reads and settles the watch in turns of their own. Made-up data only."""

import uuid

import pytest
from sqlalchemy import select, update

from app import db
from app.jobsearch import as_read, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import not_checked
from app.jobsearch.gating import check as gating_check
from app.jobsearch.gating import regate
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import engine_asks
from app.sites import where
from tests.jobsearch.test_regate import TODAY, gates, role
from tests.split.test_site_employer_asks import role as company_role
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")


async def test_a_slice_of_roles_is_judged_before_it_is_locked_not_inside_the_open_write(js, monkeypatch):
    monkeypatch.setattr(regate, "ROLE_CHUNK", 10)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        session.add_all([role(f"r{n:02}", stages.SECOND_IMPRESSION, 60_000) for n in range(25)])
        await session.commit()
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(95_000, location={"remote": {"on": False}}), by="test")
        await session.commit()
    locked = {"now": False}
    judged_while_locked: list[int] = []
    real_lock, real_check, real_release = as_read.lock, gating_check.check, db.release_if_free

    async def lock(session, model, rows, **kwargs):
        if model.__name__ == "Opportunity":
            locked["now"] = True
        return await real_lock(session, model, rows, **kwargs)

    async def release(session):
        locked["now"] = False
        return await real_release(session)

    def check(subject, rules, *args, **kwargs):
        if locked["now"]:
            judged_while_locked.append(1)
        return real_check(subject, rules, *args, **kwargs)

    monkeypatch.setattr(as_read, "lock", lock)
    monkeypatch.setattr(db, "release_if_free", release)
    monkeypatch.setattr(gating_check, "check", check)
    async with db.session_factory()() as session:
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
    assert report.roles_stopped == 25
    assert not judged_while_locked, f"{len(judged_while_locked)} roles were judged with their slice locked"


async def _gated_employer() -> uuid.UUID:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add(company_role("Initech", stages.SECOND_IMPRESSION, "a"))
        await session.flush()
        await employers.settle(session, OWNER)
        initech = await session.scalar(select(Employer).where(Employer.key == "initech"))
        assert initech is not None
        initech_id = initech.id
        await employers.edit(session, OWNER, initech_id, {"gated": True, "gated_reason": "never again"},
                             defer_gate_change=True)
        await session.commit()
    return initech_id


async def test_the_employer_ask_settles_the_boards_and_the_watch_each_in_a_turn_of_its_own(js, monkeypatch):
    employer_id = await _gated_employer()
    open_at: dict[str, bool] = {}
    real_settle, real_watch = not_checked.settle, where.settle_watch

    async def settle(session, owner, *args, **kwargs):
        open_at["not_checked"] = session.in_transaction()
        return await real_settle(session, owner, *args, **kwargs)

    async def watch(session, owner):
        open_at["watch"] = session.in_transaction()
        return await real_watch(session, owner)

    monkeypatch.setattr(not_checked, "settle", settle)
    monkeypatch.setattr(where, "settle_watch", watch)
    async with db.session_factory()() as session:
        answer = await engine_asks._asks["employer.regate"](
            session, OWNER, {"employer_id": str(employer_id), "by": "owner"})
        await session.commit()
    assert answer == {"said": "Re-checked 0 postings and 1 role: 1 newly stopped."}
    assert open_at == {"not_checked": False, "watch": False}, "each starts with no transaction held"


async def test_a_role_whose_reader_answer_lands_between_the_page_read_and_the_lock_is_judged_on_the_new_answer(
        js, monkeypatch):
    travel = {"id": "travel", "kind": "rule", "section": "responsibilities", "text": "Travel of 20% or more."}
    answer = [{"id": "travel", "answer": "fail", "evidence": "Up to 40% travel."}]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(50_000, other=[travel]))
        session.add(role("late", stages.SECOND_IMPRESSION, 100_000))
        await session.commit()
    real_lock = as_read.lock

    async def lock(session, model, rows, **kwargs):
        if model.__name__ == "Opportunity":  # the reader's answer is written after the page read, before the lock
            async with db.session_factory()() as other:
                await other.execute(update(Opportunity).where(Opportunity.owner == OWNER).values(gate_answers=answer))
                await other.commit()
        return await real_lock(session, model, rows, **kwargs)

    monkeypatch.setattr(as_read, "lock", lock)
    async with db.session_factory()() as session:
        report = await regate.run(session, OWNER, cause="a test", today=TODAY)
        await session.commit()
        late = await session.scalar(select(Opportunity).where(Opportunity.slug == "late"))
        assert late is not None
        assert (report.roles_stopped, late.status) == (1, stages.GATED_OUT), "judged on the answer the lock found"


async def test_marking_the_boards_not_checked_is_a_turn_for_each_slice(js, monkeypatch):
    from tests.jobsearch.test_not_checked import _employer, _posting

    monkeypatch.setattr(not_checked, "AT_A_TIME", 1)
    async with db.session_factory()() as session:
        for name in ("Acme", "Globex"):
            employer, _ = await _employer(session, name, EXAMPLE_SYSTEM, on=False)
            session.add(_posting(employer.id, name.lower(), name))
        await session.commit()
    releases: list[int] = []
    real = db.release_if_free

    async def release(session):
        releases.append(1)
        return await real(session)

    monkeypatch.setattr(db, "release_if_free", release)
    async with db.session_factory()() as session:
        assert await not_checked.settle(session, OWNER) == 2
        await session.commit()
    assert len(releases) == 2, "one turn for each slice of boards"
