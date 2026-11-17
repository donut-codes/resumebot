"""The gate-facts turn builds the rules once a batch in a reader session, hashes each role's fingerprint once, and
reads eight roles to a writer turn. The fingerprint is stored in `gate_facts.fp`, so its bytes must not change.
Everything here is made up."""

import hashlib
import random
from types import SimpleNamespace

from sqlalchemy import select

from app import db
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts
from app.jobsearch.loops import schedule
from app.jobsearch.models import Opportunity
from tests.jobsearch.test_gate_facts import NOTHING, PAY_OK, world
from tests.support import OWNER


def old_fingerprint(role, text) -> str:
    """The implementation before the parts were hashed one after another: the joined string."""
    parts = [text or "", role.location or "", role.move_type or "", role.comp or "", role.company or ""]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def test_the_fingerprint_is_the_one_the_joined_string_gave_for_generated_roles():
    rng = random.Random(7)
    pool = [None, "", "a", "Oakfield, ZZ", "Zürich · Café", "日本語のテキスト", "line one\nline two", "\x1f",
            "emoji \U0001f600"]
    for _ in range(200):
        role = SimpleNamespace(location=rng.choice(pool), move_type=rng.choice(pool), comp=rng.choice(pool),
                               company=rng.choice(pool))
        text = rng.choice(pool + ["x" * rng.randint(0, 3000)])
        assert gate_facts.fingerprint(role, text) == old_fingerprint(role, text)


async def test_a_role_fill_changes_is_stored_with_the_fingerprint_of_what_fill_left_and_is_not_stale_again(js):
    ids = await world(("Fabrikam", PAY_OK, {"location": "Remote, US", "move_type": "remote"}))
    async with db.session_factory()() as session:
        assert (await gate_facts.read_batch(session, OWNER, [ids["Fabrikam"]]))["looked"] == 1
        await session.commit()
    async with db.read_session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert role.comp == "$115,000-$140,000"  # fill put the pay on the role, which the fingerprint hashes
        assert role.gate_facts["fp"] == old_fingerprint(role, PAY_OK)
        assert await gate_facts.stale(session, OWNER, limit=10) == []


async def test_a_role_fill_does_not_change_is_hashed_once_in_read_batch(js, monkeypatch):
    ids = await world(*((f"Company{n}", NOTHING, {"location": "Riverton, OH"}) for n in range(12)))
    calls = []
    real = gate_facts.fingerprint
    monkeypatch.setattr(gate_facts, "fingerprint", lambda role, text: calls.append(role.id) or real(role, text))
    async with db.session_factory()() as session:
        assert (await gate_facts.read_batch(session, OWNER, list(ids.values())))["looked"] == 12
        await session.commit()
    assert sorted(calls) == sorted(ids.values())  # one call each
    # read once more, under an older reader: the stored fingerprint is compared, and the role is still hashed once
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            role.gate_facts = {**role.gate_facts, "reader": 1}
        await session.commit()
    calls.clear()
    async with db.session_factory()() as session:
        assert (await gate_facts.read_batch(session, OWNER, list(ids.values())))["looked"] == 12
        await session.commit()
    assert sorted(calls) == sorted(ids.values())


async def test_a_turn_over_a_hundred_stale_roles_commits_a_writer_turn_for_every_eight(js, monkeypatch):
    await world(*((f"Company{n}", NOTHING, {"location": "Riverton, OH"}) for n in range(100)))
    batches = []
    real = gate_facts.read_batch

    async def watched(session, owner, ids, *args, **kw):
        batches.append((db.is_reader(session), len(ids)))
        return await real(session, owner, ids, *args, **kw)

    monkeypatch.setattr(gate_facts, "read_batch", watched)
    assert await schedule.read_gate_facts(OWNER) == 100
    assert len(batches) >= 12 and all(not reader and size <= 8 for reader, size in batches)


async def test_the_rules_of_a_batch_are_built_in_a_reader_session_never_in_the_writer_turn(js, monkeypatch):
    await world(*((f"Company{n}", NOTHING, {"location": "Riverton, OH"}) for n in range(20)))
    built = []
    real = gating.rules

    async def watched(session, *args, **kw):
        built.append(db.is_reader(session))
        return await real(session, *args, **kw)

    monkeypatch.setattr(gating, "rules", watched)
    assert await schedule.read_gate_facts(OWNER) == 20
    assert len(built) >= 3 and all(built)  # the stale look and each batch: every one a reader's
