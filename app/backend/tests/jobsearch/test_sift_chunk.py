"""The filter writes its verdicts a slice at a time, and the default slice is short (60 postings), so no writer turn
is held for long (SQLite port, round 4). Run at the default `sift.CHUNK`; synthetic data only."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import sift
from app.jobsearch.models import JobPosting
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import checkpoint
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

NOW = datetime(2026, 10, 8, 5, 30, tzinfo=UTC)
WAITING = 130  # more than the default slice, fewer than the old one (250)


async def _waiting_postings():
    """130 open postings nobody has filtered: the even ones pass the gates, the odd ones the place gate stops."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"]})
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([JobPosting(
            owner=OWNER, employer_id=fab.id, ats=EXAMPLE_SYSTEM, ext_id=str(n), company=fab.name,
            title=f"Programs Role {n}", first_seen=NOW + timedelta(minutes=n), last_seen=NOW, links=[],
            location="Remote" if n % 2 == 0 else "Greater Gotham", work_mode="remote" if n % 2 == 0 else "onsite")
            for n in range(WAITING)])
        await session.commit()
        return await profile_store.current(session, OWNER)


async def _run(profile, saves: list[int]) -> dict[str, tuple]:
    real = checkpoint.reached

    async def reached():
        saves.append(1)
        await real()

    checkpoint.reached = reached
    try:
        async with db.session_factory()() as session:
            await sift.sift(session, OWNER, profile, now=NOW)
            await session.commit()
    finally:
        checkpoint.reached = real
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(JobPosting))).all()
    return {p.ext_id: (p.route, p.filtered_by) for p in rows}


async def test_the_default_slice_commits_130_waiting_postings_in_three_turns_with_the_same_verdicts(js, monkeypatch):
    profile = await _waiting_postings()
    default_saves: list[int] = []
    by_default = await _run(profile, default_saves)
    assert len(default_saves) >= 3, "130 postings are written in at least three slices (one slice of 250 before)"
    assert len(by_default) == WAITING and all(v[0] or v[1] for v in by_default.values())

    async with db.session_factory()() as session:  # the same postings, unfiltered again
        for p in (await session.scalars(select(JobPosting))).all():
            p.filtered_at = p.filtered_by = p.route = None
        await session.commit()
    monkeypatch.setattr(sift, "CHUNK", 250)
    old_saves: list[int] = []
    assert await _run(profile, old_saves) == by_default
    assert len(old_saves) == 1
