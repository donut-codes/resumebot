"""The head of the filter takes no writer turn (SQLite port round 3): the rules and the owner's roles are read in a
reader's session on the night's path, and the descriptions and places are written in groups of
`index.DETAILS_WRITE_BATCH`, the turn let go between groups. Synthetic data only."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import sift
from app.jobsearch.models import JobPosting
from app.jobsearch.postings import index, same_role
from app.jobsearch.profile import store as profile_store
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

NOW = datetime(2026, 10, 8, 5, 30, tzinfo=UTC)


async def _postings(count: int = 4) -> list:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"]})
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([JobPosting(
            owner=OWNER, employer_id=fab.id, ats=EXAMPLE_SYSTEM, ext_id=str(n), company=fab.name,
            title=f"Programs Role {n}", first_seen=NOW + timedelta(minutes=n), last_seen=NOW, links=[],
            location="Remote", work_mode="remote") for n in range(count)])
        await session.commit()
        return await profile_store.current(session, OWNER)


def _spy(monkeypatch, seen: list):
    real_rules, real_known = gating.rules, same_role.known_roles

    async def rules(session, *args, **more):
        seen.append(("rules", session))
        return await real_rules(session, *args, **more)

    async def known(session, *args, **more):
        seen.append(("known_roles", session))
        return await real_known(session, *args, **more)

    monkeypatch.setattr(gating, "rules", rules)
    monkeypatch.setattr(same_role, "known_roles", known)


async def test_the_night_reads_the_rules_and_known_roles_in_a_reader_and_holds_no_turn(js, monkeypatch):
    profile = await _postings()
    seen: list = []
    _spy(monkeypatch, seen)
    async with db.session_factory()() as session:
        await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
        assert [name for name, _ in seen] == ["rules", "known_roles"]
        assert all(reader is not session for _, reader in seen), "read apart from the writer's session"


async def test_the_owners_own_add_keeps_one_transaction(js, monkeypatch):
    profile = await _postings()
    seen: list = []
    _spy(monkeypatch, seen)
    async with db.session_factory()() as session:
        only = [p.id for p in (await session.scalars(select(JobPosting))).all()][:1]
        await sift.sift(session, OWNER, profile, now=NOW, only=only)
        assert [name for name, _ in seen] == ["rules", "known_roles"]
        assert all(reader is session for _, reader in seen), "read in the caller's own transaction"
        assert session.in_transaction()
        await session.commit()


async def test_details_over_one_batch_are_written_in_several_turns_with_the_same_rows(js, monkeypatch):
    await _postings(index.DETAILS_WRITE_BATCH * 2 + 3)
    releases: list[int] = []
    real = db.release_if_free

    async def release(session):
        releases.append(len([p for p in session.identity_map.values() if getattr(p, "detail_at", None)]))
        return await real(session)

    monkeypatch.setattr(db, "release_if_free", release)
    async with db.session_factory()() as session:
        rows = list((await session.scalars(select(JobPosting).order_by(JobPosting.first_seen))).all())
        await session.commit()
        pages = [{"text": f"A description of the work {n}. " * 20} for n, _ in enumerate(rows)]
        filled = await index.keep_details(session, rows, pages, NOW)
        await session.commit()
    assert filled == len(rows)
    assert len(releases) == 2, "the turn is let go between three groups"
    assert releases[0] <= index.DETAILS_WRITE_BATCH
    async with db.session_factory()() as session:
        kept = list((await session.scalars(select(JobPosting).order_by(JobPosting.first_seen))).all())
        assert [p.detail_at for p in kept] == [NOW] * len(rows)
        assert [p.text for p in kept] == [page["text"] for page in pages]
