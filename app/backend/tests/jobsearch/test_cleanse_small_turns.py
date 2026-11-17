"""The night's cleansing takes small turns of the file's one writer (SQLite port round 4): the copies a pass lacks are
made in batches of `lifecycle.WRITE_BATCH` roles from one read of the raw heads a batch, the stage moves are written
in slices of `MOVE_SLICE`, `keep_whole_text` finds its candidates apart from the turn, and `has_raw` leads with the
owner so an owner-first index serves it. Run at the default constants. Made-up data."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import event, select, text
from sqlalchemy.dialects import sqlite

from app import db
from app.jobsearch import as_read, stages
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting
from app.jobsearch.pipeline import lifecycle
from app.jobsearch.postings import cleansed, raw_copies
from tests.builders import rank_roles
from tests.jobsearch.test_lifecycle import _posting, _role
from tests.support import OWNER, PROFILE, load_profile

WORDS = "A whole posting with real duties, asks and a place. " * 12


async def _roles_lacking_copies(count: int) -> uuid.UUID:
    source = uuid.uuid4()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        roles = [_role(f"c{n:03d}") for n in range(count)]
        session.add_all(roles)
        await session.flush()
        for role in roles:
            session.add(_posting(role, source, None))
            session.add(RawPosting(
                owner=OWNER, source_id=source, ext_id=str(role.id), reader="scout", kind="item",
                fetched_at=datetime.now(UTC), body=b"x", body_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                body_bytes=1, parsed={"title": role.title, "company": role.company, "text": WORDS}))
        await session.commit()
    return source


async def test_copies_a_hundred_roles_lack_are_made_in_batches_of_forty_from_one_heads_read_each(js, monkeypatch):
    assert (lifecycle.WRITE_BATCH, lifecycle.MOVE_SLICE) == (40, 15)
    await _roles_lacking_copies(100)
    heads_sizes, held_calls, releases = [], [], []
    real_heads, real_release = raw_copies.heads, db.release_if_free

    async def heads(session, owner, source_id, ext_ids):
        heads_sizes.append(len(ext_ids))
        return await real_heads(session, owner, source_id, ext_ids)

    async def held(*args, **more):
        held_calls.append(1)
        raise AssertionError("one read of the heads, not a read a posting")

    async def release(session):
        releases.append(1)
        return await real_release(session)

    monkeypatch.setattr(raw_copies, "heads", heads)
    monkeypatch.setattr(raw_copies, "held", held)
    monkeypatch.setattr(db, "release_if_free", release)
    async with db.session_factory()() as session:
        await lifecycle.settle(session, OWNER, set())
        await session.commit()
    assert heads_sizes == [40, 40, 20] and not held_calls
    assert len(releases) >= 3
    async with db.session_factory()() as session:
        copies = list(await session.scalars(select(CleansedPosting)))
        assert len(copies) == 100 and all(c.text_clean.strip() == WORDS.strip() and not c.no_raw for c in copies)
        assert {r.status for r in await session.scalars(select(Opportunity))} == {stages.CLEANSED_DATA}


async def test_forty_stage_moves_are_written_in_slices_of_fifteen(js, monkeypatch):
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        roles = [_role(f"m{n:02d}", stages.READY_TO_LAUNCH, go_opened_at=now) for n in range(40)]
        session.add_all(roles)
        await session.flush()
        await rank_roles(session, {r.id: 50 for r in roles})
        await session.commit()
        ranked = {r.id for r in roles}
    locks, releases = [], []
    real_lock, real_release = as_read.lock, db.release_if_free

    async def lock(session, model, rows, **more):
        if model is Opportunity:
            locks.append(len(rows))
        return await real_lock(session, model, rows, **more)

    async def release(session):
        releases.append(1)
        return await real_release(session)

    monkeypatch.setattr(as_read, "lock", lock)
    monkeypatch.setattr(db, "release_if_free", release)
    async with db.session_factory()() as session:
        assert await lifecycle.settle(session, OWNER, ranked) == 40
        await session.commit()
    assert locks == [15, 15, 10]
    assert len(releases) >= 3


async def test_keep_whole_text_tests_the_characters_and_a_pass_with_no_candidate_takes_no_turn(js):
    ascii_thin = "a" * (cleansed.MIN_TEXT - 1)
    multibyte_thin = "é" * (cleansed.MIN_TEXT - 1)
    multibyte_whole = "é" * cleansed.MIN_TEXT
    source = uuid.uuid4()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        roles = {name: _role(name, stages.FOLLOWUP_NEEDED) for name in ("ascii", "twice", "whole")}
        session.add_all(roles.values())
        await session.flush()
        for name, body in (("ascii", ascii_thin), ("twice", multibyte_thin), ("whole", multibyte_whole)):
            session.add(_posting(roles[name], source, body))
        await session.commit()
        ids = {name: role.id for name, role in roles.items()}
    async with db.session_factory()() as session:
        statements: list = []
        event.listen(session.sync_session, "do_orm_execute", lambda state: statements.append(state))
        assert await raw_copies.keep_whole_text(session, OWNER, [ids["ascii"]]) == 0
        assert not statements and not session.in_transaction(), "no candidate: the writer's session ran nothing"
        assert await raw_copies.keep_whole_text(session, OWNER, [ids["twice"]]) == 0, (
            "its bytes pass the first test, its characters do not")
        assert await raw_copies.keep_whole_text(session, OWNER, list(ids.values())) == 1
        await session.commit()
        copies = list(await session.scalars(select(CleansedPosting)))
        assert len(copies) == 1 and copies[0].text_clean.strip() == multibyte_whole


async def test_has_raw_is_served_by_an_owner_first_index_and_finds_the_same_roles(js):
    source = await _roles_lacking_copies(5)
    async with db.session_factory()() as session:
        extra = _role("noraw")
        session.add(extra)
        await session.flush()
        session.add(_posting(extra, source, None))
        await session.commit()
        stmt = select(JobPosting.id).where(raw_copies.has_raw(JobPosting))
        sql = str(stmt.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        plan = " ".join(str(row[3]) for row in (await session.execute(text("EXPLAIN QUERY PLAN " + sql))).all())
        assert "owner=? AND source_id=? AND ext_id=?" in plan, plan  # an owner-first index (the planner picks one)
        assert "ix_js_raw_postings_owner_source_ext" in plan or "ix_js_raw_postings_newest" in plan, plan
        old = select(JobPosting.id).where(select(RawPosting.id).where(
            RawPosting.source_id == JobPosting.employer_id, RawPosting.ext_id == JobPosting.ext_id).exists())
        now_found, was_found = set(await session.scalars(stmt)), set(await session.scalars(old))
        assert now_found == was_found and len(now_found) == 5
