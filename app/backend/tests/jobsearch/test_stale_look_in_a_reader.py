"""The look for roles whose gate facts are stale holds no writer turn: `schedule.read_gate_facts` and
`gate_facts.sweep_in_batches` take the stale ids in a reader session, and the batches that write stay writers.
Everything here is made up."""

import pytest

from app import db
from app.jobsearch.gating import gate_facts
from app.jobsearch.loops import schedule
from tests.jobsearch.test_gate_batch_reads_first import twenty_five
from tests.support import OWNER


@pytest.fixture
def looks(monkeypatch):
    """Whether each session `gate_facts.stale` was given was a reader's, and the ids it returned."""
    seen: list[tuple[bool, list]] = []
    original = gate_facts.stale

    async def watched(session, *args, **kw):
        ids = await original(session, *args, **kw)
        seen.append((db.is_reader(session), list(ids)))
        return ids

    monkeypatch.setattr(gate_facts, "stale", watched)
    return seen


async def test_the_turns_stale_look_runs_in_a_reader_session(js, looks):
    made = await twenty_five()
    assert await schedule.read_gate_facts(OWNER) == len(made)
    first_is_reader, ids = looks[0]
    assert first_is_reader is True
    assert set(ids) == set(made.values())


async def test_the_sweeps_stale_look_runs_in_a_reader_session_and_its_batches_read_every_role(js, looks, monkeypatch):
    made = await twenty_five()
    batches: list[tuple[bool, list]] = []
    original = gate_facts.read_batch

    async def watched(session, owner, ids, *args, **kw):
        batches.append((db.is_reader(session), list(ids)))
        return await original(session, owner, ids, *args, **kw)

    monkeypatch.setattr(gate_facts, "read_batch", watched)
    out = await gate_facts.sweep_in_batches(OWNER)
    assert looks[0][0] is True
    assert out["looked"] == len(made)
    assert all(not reader for reader, _ in batches)
    assert {i for _, ids in batches for i in ids} == set(made.values())
