"""The filter judges a slice before it locks it (docs/PRD.md 28.10, 30.1): the postings
that wait are listed once, each slice is read with nothing locked and judged with no row touched (the loop given back
between postings), and only then locked, looked at again and written, a few statements. The verdicts and the slices
themselves are the ones `test_sift.py` pins. Synthetic data only."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app import db
from app.jobsearch import adapters, as_read
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.gating import sift
from app.jobsearch.models import JobPosting
from app.jobsearch.postings import coop, same_role
from app.jobsearch.profile import store as profile_store
from tests.conftest import Transactions
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

NOW = datetime(2026, 10, 8, 5, 30, tzinfo=UTC)


def _fetcher() -> Fetcher:
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


async def _profile(session):
    await load_profile(session, OWNER, criteria={**PROFILE["criteria"]})
    return await profile_store.current(session, OWNER)


async def _a_hundred_postings():
    """A hundred open postings nobody has filtered, a minute apart: the even ones pass the gates, the odd ones are
    stopped by the place gate."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"]})
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([JobPosting(
            owner=OWNER, employer_id=fab.id, ats=EXAMPLE_SYSTEM, ext_id=str(n), company=fab.name,
            title=f"Programs Role {n}", first_seen=NOW + timedelta(minutes=n), last_seen=NOW, links=[],
            location="Remote" if n % 2 == 0 else "Greater Gotham", work_mode="remote" if n % 2 == 0 else "onsite")
            for n in range(100)])
        await session.commit()
        return await profile_store.current(session, OWNER)


async def test_a_slice_is_judged_before_it_is_locked_and_nothing_is_written_to_a_row_until_then(js, monkeypatch):
    monkeypatch.setattr(sift, "CHUNK", 20)
    profile = await _a_hundred_postings()
    events: list[str] = []
    real_judge, real_lock = sift._judge, as_read.lock

    def judge(*args, **more):
        events.append("judge")
        return real_judge(*args, **more)

    async def lock(session, model, rows, **more):
        assert not session.dirty and not session.new, "no attribute of a row was set before its slice was locked"
        events.append("lock")
        return await real_lock(session, model, rows, **more)

    monkeypatch.setattr(sift, "_judge", judge)
    monkeypatch.setattr(as_read, "lock", lock)
    async with db.session_factory()() as session:
        result = await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
    assert events == (["judge"] * 20 + ["lock"]) * 5
    assert result["looked_at"] == 100 and len(result["candidates"]) == 50


async def test_no_row_of_a_slice_is_held_while_it_is_judged(js, monkeypatch):
    """A slow judge held the slice's rows to the end of its transaction (the night's filter, 2 to 10 seconds of the
    loop); now another transaction can take any posting of the slice while the slice is being judged."""
    monkeypatch.setattr(sift, "CHUNK", 20)
    profile = await _a_hundred_postings()
    async with db.session_factory()() as session:
        wanted = (await session.scalars(select(JobPosting.id).where(JobPosting.ext_id == "5"))).one()
    taken: list[int] = []

    class Probing:
        """The judge's pace-keeper, which at its third posting has another transaction take a posting of the slice."""

        calls = 0

        async def __call__(self) -> None:
            Probing.calls += 1
            if Probing.calls == 3:
                async with db.session_factory()() as other:  # a held writer turn would raise (NestedWriter), not wait
                    await other.execute(
                        select(JobPosting.id).where(JobPosting.id == wanted))
                    await other.rollback()
                taken.append(1)

    monkeypatch.setattr(sift, "Cooperate", Probing)
    async with db.session_factory()() as session:
        await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
    assert taken == [1]


async def test_a_slice_writes_in_a_few_statements_after_its_lock_and_reads_nothing_then(js, monkeypatch, transactions):
    monkeypatch.setattr(sift, "CHUNK", 20)
    profile = await _a_hundred_postings()
    transactions.clear()
    async with db.session_factory()() as session:
        await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
    writers = [each for each in transactions.ended if Transactions.writes(each)]
    assert len(writers) == 5
    for each in writers:
        # the lock is the first plain `SELECT js_postings.id ... ORDER BY js_postings.id` (`as_read.lock`: no row lock)
        locked = next(i for i, s in enumerate(each)
                      if s.lstrip().startswith("SELECT js_postings.id") and "ORDER BY js_postings.id" in s)
        after = each[locked + 1:]
        # a statement for a posting at most (the rows of the two verdicts here alternate, and the ORM sends an
        # `UPDATE` for each change of shape), and the one to-do the writes post
        assert len(after) <= 20 + 2, f"the rows are held for {len(after)} statements: {after}"
        assert not [s for s in after if s.startswith("SELECT")], "nothing is read once the slice is locked"
        assert all(s.startswith(("SELECT", "BEGIN")) for s in each[:locked]), "everything before the lock only reads"


async def test_the_known_roles_and_the_model_are_asked_for_once_a_run_not_once_a_slice(js, monkeypatch):
    monkeypatch.setattr(sift, "CHUNK", 20)
    profile = await _a_hundred_postings()
    asked = {"roles": 0, "model": 0}
    real_roles, real_train = same_role.known_roles, sift.classifier.train

    async def roles(*args, **more):
        asked["roles"] += 1
        return await real_roles(*args, **more)

    async def train(*args, **more):
        asked["model"] += 1
        return await real_train(*args, **more)

    monkeypatch.setattr(same_role, "known_roles", roles)
    monkeypatch.setattr(sift.classifier, "train", train)
    async with db.session_factory()() as session:
        await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
    assert asked == {"roles": 1, "model": 1}


async def test_the_postings_a_run_filters_are_the_ones_waiting_when_it_began(js, monkeypatch):
    """A store that keeps adding postings cannot lengthen a run: its list is read once. What was added meanwhile waits
    for the next run."""
    monkeypatch.setattr(sift, "CHUNK", 20)
    profile = await _a_hundred_postings()
    real, added = sift._read, []

    async def read(*args, **more):
        # at the second slice's read: the first slice is saved and released, so no writer turn is held here
        if len(added) == 1:
            async with db.session_factory()() as other:
                fab = await employers.find_or_make(other, OWNER, "Fabrikam")
                other.add_all([JobPosting(
                    owner=OWNER, employer_id=fab.id, ats=EXAMPLE_SYSTEM, ext_id=f"late-{n}", company="Fabrikam",
                    title=f"Late Role {n}", first_seen=NOW + timedelta(days=1), last_seen=NOW, links=[],
                    location="Remote", work_mode="remote") for n in range(5)])
                await other.commit()
        added.append(1)
        return await real(*args, **more)

    monkeypatch.setattr(sift, "_read", read)
    async with db.session_factory()() as session:
        result = await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
        assert result["looked_at"] == 100
        waiting = (await session.scalars(select(JobPosting.ext_id).where(JobPosting.filtered_at.is_(None)))).all()
        assert sorted(waiting) == [f"late-{n}" for n in range(5)]
        again = await sift.sift(session, OWNER, profile, now=NOW)
    assert again["looked_at"] == 5


@dataclass
class Places:
    """A job system that names the places on a posting's own page; remembers which postings it was asked for."""

    places = True
    asked: list

    async def detail(self, fetch, board, posting):
        self.asked.append(posting["ext_id"])
        return {"location": "Oakfield, ST", "work_mode": "onsite"}


async def test_the_pages_read_for_a_place_are_the_first_ones_wanting_it_across_the_slices(js, monkeypatch):
    """`reads` is the filter's budget for the whole run: the first of the postings that want their places, in the order
    they were first seen, whichever slice they are in."""
    monkeypatch.setattr(sift, "CHUNK", 7)
    stand_in = Places([])
    monkeypatch.setattr(adapters, "ADAPTERS", {"tenantone": stand_in})
    async with db.session_factory()() as session:
        profile = await _profile(session)
        hooli = await employers.find_or_make(session, OWNER, "Hooli")
        session.add_all([JobPosting(
            owner=OWNER, employer_id=hooli.id, ats="tenantone", ext_id=f"{n:02d}", company="Hooli",
            title=f"Programs Role {n}", first_seen=NOW + timedelta(minutes=n), last_seen=NOW, links=[],
            location=f"{n + 2} Locations") for n in range(25)])
        await session.commit()
        result = await sift.sift(session, OWNER, profile, now=NOW, fetch=_fetcher(), reads=10)
        await session.commit()
    assert sorted(stand_in.asked) == [f"{n:02d}" for n in range(10)] and result["read"] == 10
    assert result["looked_at"] == 25


async def test_the_judging_gives_the_loop_back_between_postings(js, monkeypatch):
    """A slice is 0.3 seconds of judging with no `await`: another task runs between its postings."""
    monkeypatch.setattr(sift, "Cooperate", lambda: coop.Cooperate(every=0.0))
    profile = await _a_hundred_postings()
    ran = 0

    async def other():
        nonlocal ran
        while True:
            await asyncio.sleep(0)
            ran += 1

    task = asyncio.get_running_loop().create_task(other())
    try:
        async with db.session_factory()() as session:
            await sift.sift(session, OWNER, profile, now=NOW)
            await session.commit()
    finally:
        task.cancel()
    assert ran >= 100


async def test_a_filter_run_only_for_some_postings_is_one_slice_in_the_callers_transaction(js, transactions):
    profile = await _a_hundred_postings()
    async with db.session_factory()() as session:
        ids = [p.id for p in (await session.scalars(select(JobPosting).where(
            JobPosting.ext_id.in_(["1", "2", "3"])))).all()]
        transactions.clear()
        result = await sift.sift(session, OWNER, profile, now=NOW, only=ids)
        assert transactions.ended == [], "nothing was saved by the call"
        assert result["looked_at"] == 3
        await session.commit()


def test_nothing_is_judged_in_the_write_phase():
    """A slice's verdicts are made by `_judge` from a posting's columns and written by `_write`: the judge sets no
    attribute and the write phase makes no judgement (the one `only` filter and the sliced ones share both)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(sift._judge))
    assigned = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Attribute) for t in n.targets)]
    assert not assigned, "the judge sets no attribute"
