"""The write hooks read an attribute's history with `InstanceState.get_history`, not `InstanceState.attrs`: `attrs`
builds one `AttributeState` per mapped attribute, each pointing back at the state, so every row a hook touched left a
reference cycle that only a full (generation 2) collection frees. What is tested is that no `AttributeState` survives
in the garbage a collection finds after the hooks ran on a score of rows. Every name is made up."""

import gc
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm.base import PASSIVE_NO_INITIALIZE

from app import db
from app.jobsearch import as_read, stages
from app.jobsearch.browser import tabs
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import live
from app.jobsearch.work.queue import watch
from tests.support import OWNER

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
ROWS = 20


def _role(n: int) -> Opportunity:
    return Opportunity(id=uuid.UUID(int=n), owner=OWNER, slug=f"role-{n}", company="Firm a", title=f"Lead {n}",
                       match_key=f"firm a|lead {n}", origin="scout", status=stages.FIRST_IMPRESSION,
                       found=date(2026, 11, 3))


def _posting(n: int) -> JobPosting:
    return JobPosting(id=uuid.UUID(int=1000 + n), owner=OWNER, employer_id=uuid.UUID(int=900), ats="scout",
                      ext_id=f"ext-{n}", company="Firm a", title=f"Lead {n}", first_seen=NOW, last_seen=NOW)


@pytest.fixture
def collector():
    """A clean heap first, then no automatic collection; the debug flag, the garbage and the collector are put back."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.set_debug(0)
        gc.garbage.clear()
        gc.enable()


def _attribute_states_left() -> list:
    gc.set_debug(gc.DEBUG_SAVEALL)
    gc.collect()
    return [o for o in gc.garbage if type(o).__name__ == "AttributeState"]


async def test_the_hooks_leave_no_attribute_state_cycle_on_the_rows_they_touch(js, collector, monkeypatch):
    monkeypatch.setattr(tabs, "_go_rows", True)  # the departures hook looks only once a Launch tab was registered
    async with db.session_factory()() as session:
        session.add_all([*(_role(n) for n in range(1, ROWS + 1)), *(_posting(n) for n in range(1, ROWS + 1))])
        await session.commit()
    async with db.session_factory()() as session:
        roles = list(await session.scalars(select(Opportunity)))
        postings = list(await session.scalars(select(JobPosting)))
        for role in roles:
            role.status = stages.READY_TO_LAUNCH  # the stage move, a status read in the live and tabs hooks
        for posting in postings:
            posting.last_seen = NOW + timedelta(days=1)  # the day follows its time
            posting.first_seen = NOW - timedelta(days=1)
        await session.flush()
        for role in roles:
            role.status = stages.FIRST_IMPRESSION  # a departure from Ready to Launch
            role.title = f"{role.title}, again"
        await session.flush()
        held = await as_read.lock(session, Opportunity, roles, stands=(Opportunity.status,))
        assert len(held.ids) == ROWS
        for role in roles:
            role.title = f"{role.title}, once more"
            live._changed(role, "title")
            change = watch.Change(role)
            change.changed("title")
            change.old("title")
        await session.commit()
    del roles, postings, held, role, posting, change  # the rows are garbage once nothing names them
    assert _attribute_states_left() == []


def test_the_history_read_is_the_one_attrs_gave():
    role = _role(1)
    state = inspect(role)
    role.status = stages.APPLIED
    assert state.get_history("status", PASSIVE_NO_INITIALIZE) == state.attrs.status.history
