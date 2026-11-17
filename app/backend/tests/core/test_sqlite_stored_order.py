"""The stored order and the days on SQLite: the triggers that
tell `stored_order.settle` which roles to look at again are SQLite triggers made with the tables, a posting's seen day
and a role's made day are stored when the row is written, and the age in whole hours is counted in SQL the way Python
counts it. These make their own file in a temporary folder and use none of the shared database fixtures. Everything here
is made up.
"""

import random
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import insert, select, text, update

from app import db
from app.jobsearch import stages
from app.jobsearch.browser import reader
from app.jobsearch.fit import freshness
from app.jobsearch.models import JobPosting, Opportunity, ProfileVersion, RoleRank
from app.jobsearch.pipeline import from_posting, queue_triggers, row, stored_order
from app.jobsearch.profile import base as profile_base
from app.jobsearch.work import queue
from app.sites.models import Site
from app.timeutil import owner_today
from tests.core.test_sqlite_posting_copies import Hand
from tests.support import EXAMPLE_SYSTEM, OWNER, pin_clock

OTHER = "someone-else"
NOW = datetime(2026, 11, 4, 12, 0, 0, 500_000, tzinfo=UTC)  # not on a whole second: a boundary needs microseconds


# --- B4: the triggers -----------------------------------------------------------------------------------------------


async def lines(session) -> set[tuple[str, uuid.UUID | None]]:
    """The lines of `js_queue_dirty` as (owner, role) pairs, and the table emptied."""
    found = (await session.execute(text("SELECT owner, role_id FROM js_queue_dirty"))).all()
    await session.execute(text("DELETE FROM js_queue_dirty"))
    return {(owner, uuid.UUID(hex=role) if role else None) for owner, role in found}


async def lonely_role(session, slug: str, **more) -> Opportunity:
    role = Opportunity(owner=OWNER, slug=slug, company=f"Firm {slug}", title=f"Lead {slug}", origin="scout",
                       match_key=f"firm {slug}|lead {slug}", status=stages.LANDING_ZONE,
                       **{"found": owner_today(), **more})
    session.add(role)
    await session.flush()
    return role


async def test_the_triggers_are_made_with_the_tables_and_made_again_without_a_change(lite):
    async def made() -> list[tuple[str, str]]:
        async with lite.connect() as conn:
            return [tuple(r) for r in (await conn.execute(text(
                "SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"))).all()]

    before = await made()
    assert [name for name, _table in before] == sorted(name for name, _sql in queue_triggers.TRIGGERS)
    assert len(before) == 12, "the migration's ten, two of which fired on insert or delete"
    async with lite.begin() as conn:
        await conn.run_sync(queue_triggers.make)
    assert await made() == before


def test_the_profile_documents_the_trigger_names_are_the_ones_the_number_reads():
    assert queue_triggers.PROFILE_KINDS == profile_base.KINDS


async def test_a_role_made_or_changed_in_an_input_of_the_number_names_itself_and_no_other_change_does(lite):
    async with db.session_factory()() as session:
        role = await lonely_role(session, "plain")
        await session.commit()
        assert await lines(session) == {(OWNER, role.id)}, "a role made"
        for change in ({"fit": 3}, {"posted": date(2026, 11, 1)}, {"found": date(2026, 10, 30)},
                       {"board_url": "https://board.example.test/1"}, {"target_role": "lead"},
                       {"analysis_state": "waiting"}, {"origin": "tracker"},
                       {"created_at": NOW - timedelta(days=2)}, {"fit_breakdown": {"base": 40}}):
            for name, value in change.items():
                setattr(role, name, value)
            await session.commit()
            assert await lines(session) == {(OWNER, role.id)}, change
        for change in ({"notes": "a note"}, {"description": "a text"}, {"title": "Another title"},
                       {"favorited_at": NOW}):
            for name, value in change.items():
                setattr(role, name, value)
            await session.commit()
            assert await lines(session) == set(), change
        # a statement that sets two inputs to the values they have
        with queue.exempt("a test writes the values a role already has"):
            await session.execute(update(Opportunity).where(Opportunity.id == role.id).values(
                fit=3, posted=date(2026, 11, 1)))
        await session.commit()
        assert await lines(session) == set()


async def test_only_the_deck_read_mark_of_the_analysis_names_the_role(lite):
    async with db.session_factory()() as session:
        role = await lonely_role(session, "analysed", analysis={"deck_read": False, "notes": "a"})
        await session.commit()
        await lines(session)
        role.analysis = {"deck_read": False, "notes": "b", "more": 1}
        await session.commit()
        assert await lines(session) == set(), "the rest of the analysis is no input"
        role.analysis = {"deck_read": True, "notes": "b", "more": 1}
        await session.commit()
        assert await lines(session) == {(OWNER, role.id)}
        role.analysis = {"notes": "b"}  # the mark taken away
        await session.commit()
        assert await lines(session) == {(OWNER, role.id)}


async def _rank(session, role: Opportunity, copy_id: uuid.UUID, **more) -> RoleRank:
    rank = RoleRank(owner=OWNER, role_id=role.id, cleansed_id=copy_id, profile_version=more.pop("profile_version", 1),
                    fit=more.pop("fit", 50), **more)
    session.add(rank)
    await session.flush()
    return rank


async def test_a_rank_written_changed_moved_or_deleted_names_its_roles(lite):
    async with db.session_factory()() as session:
        w = Hand(session)
        a, b = await w.role("a"), await w.role("b")
        await w.read(a, "ca", "a" * 400)
        await w.read(b, "cb", "b" * 400)
        await session.commit()
        await lines(session)
        rank = await _rank(session, a, w.copies["ca"])
        await session.commit()
        assert await lines(session) == {(OWNER, a.id)}, "a rank written"
        rank.fit = 77
        await session.commit()
        assert await lines(session) == {(OWNER, a.id)}, "a fit changed"
        rank.why = "a better reason"
        await session.commit()
        assert await lines(session) == set(), "the why is no input"
        rank.role_id, rank.cleansed_id = b.id, w.copies["cb"]
        await session.commit()
        assert await lines(session) == {(OWNER, a.id), (OWNER, b.id)}, "moved: both roles"
        await session.delete(rank)
        await session.commit()
        assert await lines(session) == {(OWNER, b.id)}, "deleted"


async def test_a_posting_of_a_role_written_changed_moved_or_deleted_names_its_roles_and_one_of_none_names_none(lite):
    async with db.session_factory()() as session:
        w = Hand(session)
        a, b = await w.role("a"), await w.role("b")
        await session.commit()
        await lines(session)
        loose = await w.posting(None, uuid.uuid4(), "loose")
        await session.commit()
        assert await lines(session) == set(), "a posting no role holds"
        loose.url = "https://jobs.example.test/loose"
        await session.commit()
        assert await lines(session) == set()
        held = await w.posting(a, uuid.uuid4(), "held")
        await session.commit()
        assert await lines(session) == {(OWNER, a.id)}, "a posting of a role written"
        held.url, held.apply_url = "https://jobs.example.test/held", "https://jobs.example.test/held/apply"
        await session.commit()
        assert await lines(session) == {(OWNER, a.id)}
        held.posted, held.first_seen, held.ats = date(2026, 11, 2), NOW - timedelta(days=9), EXAMPLE_SYSTEM
        await session.commit()
        assert await lines(session) == {(OWNER, a.id)}
        held.text, held.title, held.last_seen, held.filtered_at = "a text", "Another title", NOW, NOW
        await session.commit()
        assert await lines(session) == set(), "its text, title and last-seen time are no input"
        held.opportunity_id = b.id
        await session.commit()
        assert await lines(session) == {(OWNER, a.id), (OWNER, b.id)}, "moved from one role to another"
        held.opportunity_id = None
        await session.commit()
        assert await lines(session) == {(OWNER, b.id)}, "let go of its role"
        held.opportunity_id = b.id
        await session.commit()
        await lines(session)
        await session.delete(held)
        await session.commit()
        assert await lines(session) == {(OWNER, b.id)}, "deleted"
        await session.delete(loose)
        await session.commit()
        assert await lines(session) == set()


async def test_a_saved_version_of_the_numbers_documents_or_a_changed_list_of_job_sites_names_every_role_of_the_owner(
        lite):
    async with db.session_factory()() as session:
        for kind, named in (("settings", True), ("scoring", True), ("criteria", True), ("gates", True),
                            ("resume", False)):
            session.add(ProfileVersion(owner=OWNER, kind=kind, version=1, data={}, saved_by="test"))
            await session.commit()
            assert await lines(session) == ({(OWNER, None)} if named else set()), kind
        site = Site(owner=OWNER, url="https://boards.example.test", kind="job_board", list_name="search",
                    created_at=NOW)
        session.add(site)
        await session.commit()
        assert await lines(session) == {(OWNER, None)}, "a site added"
        site.label, site.on = "A board", False
        await session.commit()
        assert await lines(session) == set(), "a label and a switch are no input"
        site.url = "https://boards.example.test/jobs"
        await session.commit()
        assert await lines(session) == {(OWNER, None)}
        site.list_name = "avoid"
        await session.commit()
        assert await lines(session) == {(OWNER, None)}
        await session.delete(site)
        await session.commit()
        assert await lines(session) == {(OWNER, None)}, "a site deleted"


async def test_a_seeded_run_of_writes_leaves_exactly_the_lines_the_inputs_it_wrote_call_for(lite):
    """Two hundred writes in a random order, each checked against what the rules say it should add: the lines, as a set,
    are those of the roles whose input the write changed, and none when it changed no input."""
    rng = random.Random(20261107)
    async with db.session_factory()() as session:
        w = Hand(session)
        roles = [await w.role(f"r{n}") for n in range(4)]
        for n, role in enumerate(roles):
            await w.read(role, f"c{n}", "t" * 400)
        loose = await w.posting(None, uuid.uuid4(), "loose")
        await session.commit()
        await lines(session)
        ranks: list[RoleRank] = []
        sites: list[Site] = []
        for step in range(200):
            linked = [p for p in w.postings.values() if p.opportunity_id]
            kind = rng.choice(("role_input", "role_other", "rank_new", "rank_fit", "rank_why", "rank_move",
                               "rank_delete", "posting_input", "posting_other", "posting_move", "posting_unlink",
                               "posting_loose_input", "site_new", "site_url", "site_label", "site_delete"))
            role = rng.choice(roles)
            other = rng.choice([r for r in roles if r is not role])
            expected: set[tuple[str, uuid.UUID | None]] = set()
            if kind == "role_input":
                role.fit = (role.fit or 0) + 1
                expected = {(OWNER, role.id)}
            elif kind == "role_other":
                role.notes = f"note {step}"
            elif kind == "rank_new":
                copy = w.copies[f"c{roles.index(role)}"]
                ranks.append(await _rank(session, role, copy, profile_version=step + 1))
                expected = {(OWNER, role.id)}
            elif ranks and kind == "rank_fit":
                rank = rng.choice(ranks)
                rank.fit = (rank.fit + 7) % 100
                expected = {(OWNER, rank.role_id)}
            elif ranks and kind == "rank_why":
                rng.choice(ranks).why = f"why {step}"
            elif ranks and kind == "rank_move":
                rank = rng.choice(ranks)
                was = rank.role_id
                rank.role_id = other.id
                rank.cleansed_id = w.copies[f"c{roles.index(other)}"]
                rank.profile_version = step + 1000
                expected = {(OWNER, was), (OWNER, other.id)}
            elif ranks and kind == "rank_delete":
                rank = ranks.pop(rng.randrange(len(ranks)))
                expected = {(OWNER, rank.role_id)}
                await session.delete(rank)
            elif linked and kind == "posting_input":
                posting = rng.choice(linked)
                posting.url = f"https://jobs.example.test/{step}"
                expected = {(OWNER, posting.opportunity_id)}
            elif kind == "posting_other":
                rng.choice(list(w.postings.values())).text = f"text {step}"
            elif linked and kind == "posting_move":
                posting = rng.choice(linked)
                was = posting.opportunity_id
                target = other.id if other.id != was else role.id
                posting.opportunity_id = target
                expected = {(OWNER, was), (OWNER, target)} if target != was else set()
            elif linked and kind == "posting_unlink":
                posting = rng.choice(linked)
                expected = {(OWNER, posting.opportunity_id)}
                posting.opportunity_id = None
                if rng.random() < 0.7:  # most are put back, so the run keeps its postings
                    await session.flush()
                    await lines(session)
                    posting.opportunity_id = role.id
                    expected = {(OWNER, role.id)}
            elif kind == "posting_loose_input":
                loose.url = f"https://jobs.example.test/loose/{step}"
            elif kind == "site_new":
                site = Site(owner=OWNER, url=f"https://s{step}.example.test", kind="job_board", list_name="search",
                            created_at=NOW)
                sites.append(site)
                session.add(site)
                expected = {(OWNER, None)}
            elif sites and kind == "site_url":
                rng.choice(sites).url = f"https://moved{step}.example.test"
                expected = {(OWNER, None)}
            elif sites and kind == "site_label":
                rng.choice(sites).label = f"label {step}"
            elif sites and kind == "site_delete":
                await session.delete(sites.pop(rng.randrange(len(sites))))
                expected = {(OWNER, None)}
            else:
                continue
            await session.commit()
            assert await lines(session) == expected, (step, kind)


# --- C5: the days a row is written with -----------------------------------------------------------------------------


async def test_a_posting_stores_the_owners_day_it_was_first_seen_when_it_is_written(lite, monkeypatch):
    pin_clock(monkeypatch, NOW, "America/New_York")
    late_in_new_york = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)  # 10:30 pm on the 4th there
    dst_ends = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 1:30 am EDT, the hour that happens twice
    async with db.session_factory()() as session:
        w = Hand(session)
        a = await w.posting(None, uuid.uuid4(), "late")
        a.first_seen = late_in_new_york
        b = await w.posting(None, uuid.uuid4(), "dst")
        b.first_seen = dst_ends
        await session.commit()
        c = JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="scout", ext_id="given", company="Firm",
                       first_seen=datetime(2026, 11, 2, 2, 0, tzinfo=UTC), last_seen=NOW)
        session.add(c)
        await session.commit()
    async with db.session_factory()() as session:
        seen = dict((await session.execute(select(JobPosting.ext_id, JobPosting.seen_day))).all())
    assert seen == {"late": date(2026, 11, 4), "dst": date(2026, 11, 1), "given": date(2026, 11, 1)}


async def test_a_posting_inserted_without_the_orm_has_its_day_too_and_one_changed_in_other_ways_keeps_it(lite,
                                                                                                           monkeypatch):
    pin_clock(monkeypatch, NOW, "Pacific/Auckland")  # a day ahead of UTC
    moments = [datetime(2026, 11, 4, 12, 30, tzinfo=UTC), datetime(2026, 11, 4, 10, 30, tzinfo=UTC)]
    async with lite.begin() as conn:
        await conn.execute(insert(JobPosting.__table__), [
            {"id": uuid.uuid4(), "owner": OWNER, "employer_id": uuid.uuid4(), "ats": "scout", "ext_id": f"e{n}",
             "company": "Firm", "first_seen": moment, "last_seen": moment, "links": []}
            for n, moment in enumerate(moments)])
    async with db.session_factory()() as session:
        seen = dict((await session.execute(select(JobPosting.ext_id, JobPosting.seen_day))).all())
        assert seen == {"e0": date(2026, 11, 5), "e1": date(2026, 11, 4)}, "12:30 UTC is 1:30 am the next day there"
        posting = await session.scalar(select(JobPosting).where(JobPosting.ext_id == "e0"))
        assert posting is not None
        posting.title = "Another title"
        posting.last_seen = NOW
        await session.commit()
        assert posting.seen_day == date(2026, 11, 5)
        posting.first_seen = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)  # a merge keeps the earlier time
        await session.commit()
        assert await session.scalar(select(JobPosting.seen_day).where(JobPosting.id == posting.id)) == date(2026, 11, 1)


async def test_a_role_stores_the_owners_day_it_was_made_by_the_orm_or_by_a_multi_row_insert(lite, monkeypatch):
    pin_clock(monkeypatch, NOW, "America/New_York")
    late_in_new_york = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
    async with db.session_factory()() as session:
        by_default = await lonely_role(session, "made-now")
        explicit = await lonely_role(session, "made-late", created_at=late_in_new_york)
        await session.commit()
        assert by_default.created_day == date(2026, 11, 4) and explicit.created_day == date(2026, 11, 4)
        assert late_in_new_york.date() == date(2026, 11, 5), "the UTC day is not the owner's"
        explicit.created_at = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
        await session.commit()
        assert explicit.created_day == date(2026, 11, 1), "the day follows the time"

        def values(slug: str, **more) -> dict:
            return {"id": uuid.uuid4(), "owner": OWNER, "slug": slug, "company": "Firm", "title": "Lead",
                    "match_key": slug, "status": stages.LANDING_ZONE, "origin": "tracker", **more}

        # the way the tracker import writes roles, in one multi-row statement: stamped with the time it is now
        with queue.exempt("a test writes roles the way the tracker import does: in one multi-row statement"):
            await session.execute(insert(Opportunity).values([
                values("core-a", created_at=NOW), values("core-b", created_at=NOW), values("core-c")]))
            # the scout's round writes one role at a time, with its own time
            earlier = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
            await session.execute(insert(Opportunity).values(values("core-d", created_at=earlier))
                                  .returning(Opportunity.id))
        await session.commit()
        days = dict((await session.execute(select(Opportunity.slug, Opportunity.created_day).where(
            Opportunity.slug.like("core-%")))).all())
    assert days == {"core-a": date(2026, 11, 4), "core-b": date(2026, 11, 4), "core-c": date(2026, 11, 4),
                    "core-d": date(2026, 11, 1)}


async def test_the_day_a_role_dates_from_for_the_browser_is_its_posted_day_then_found_then_made(lite, monkeypatch):
    pin_clock(monkeypatch, NOW, "America/New_York")
    async with db.session_factory()() as session:
        await lonely_role(session, "posted", posted=date(2026, 10, 1), found=date(2026, 10, 20),
                          created_at=NOW - timedelta(days=1))
        await lonely_role(session, "found", found=date(2026, 10, 10), created_at=NOW - timedelta(days=1))
        await lonely_role(session, "made-old", found=None, created_at=NOW - timedelta(days=30))
        await lonely_role(session, "made-new", found=None, created_at=NOW - timedelta(hours=1))
        await lonely_role(session, "made-late-evening", found=None, created_at=datetime(2026, 10, 31, 3, 0, tzinfo=UTC))
        await session.commit()
        got = (await session.execute(select(Opportunity.slug, reader._youngest()))).all()
        days = {slug: str(day) for slug, day in got}
        ordered = [slug for slug, in (await session.execute(
            select(Opportunity.slug).order_by(reader._youngest().desc(), Opportunity.created_at.desc()))).all()]
    assert days == {"posted": "2026-10-01", "found": "2026-10-10", "made-old": "2026-10-05",
                    "made-new": "2026-11-04", "made-late-evening": "2026-10-30"}
    assert ordered == ["made-new", "made-late-evening", "found", "made-old", "posted"]


async def test_the_day_a_posting_of_the_added_links_column_is_dated_by_is_the_earlier_of_its_employers_and_the_day_seen(
        lite, monkeypatch):
    pin_clock(monkeypatch, NOW, "America/New_York")
    async with db.session_factory()() as session:
        seen_dates = {"employer-older": (date(2026, 10, 1), datetime(2026, 11, 3, 15, 0, tzinfo=UTC)),
                      "seen-older": (date(2026, 11, 3), datetime(2026, 10, 20, 15, 0, tzinfo=UTC)),
                      "no-date": (None, datetime(2026, 11, 2, 15, 0, tzinfo=UTC)),
                      "same-day": (date(2026, 11, 4), datetime(2026, 11, 4, 15, 0, tzinfo=UTC))}
        for ext, (posted, first_seen) in seen_dates.items():
            session.add(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="scout", ext_id=ext, company="Firm",
                                   posted=posted, first_seen=first_seen, last_seen=NOW))
        await session.commit()
        day = {ext: str(d) for ext, d in (await session.execute(select(JobPosting.ext_id, row._posting_day()))).all()}
        order = [ext for ext, in (await session.execute(
            select(JobPosting.ext_id).order_by(row._posting_day().desc().nulls_last(),
                                               JobPosting.first_seen.desc(), JobPosting.id))).all()]
    assert day == {"employer-older": "2026-10-01", "seen-older": "2026-10-20", "no-date": "2026-11-02",
                   "same-day": "2026-11-04"}
    assert order == ["same-day", "no-date", "seen-older", "employer-older"]


async def test_postings_are_made_into_roles_newest_day_first_then_the_one_seen_later(lite, monkeypatch):
    pin_clock(monkeypatch, NOW, "America/New_York")
    async with db.session_factory()() as session:
        for ext, posted, first_seen in (
                ("old-posted", date(2026, 10, 1), datetime(2026, 11, 4, 9, 0, tzinfo=UTC)),
                ("new-posted", date(2026, 11, 3), datetime(2026, 10, 1, 9, 0, tzinfo=UTC)),
                ("seen-late", None, datetime(2026, 11, 3, 23, 0, tzinfo=UTC)),
                ("seen-early", None, datetime(2026, 11, 3, 13, 0, tzinfo=UTC)),
                ("seen-old", None, datetime(2026, 10, 2, 13, 0, tzinfo=UTC))):
            session.add(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="scout", ext_id=ext, company="Firm",
                                   posted=posted, first_seen=first_seen, last_seen=NOW))
        await session.commit()
        order = [ext for ext, in (await session.execute(
            select(JobPosting.ext_id).order_by(*from_posting.newest_first()))).all()]
    # the 3rd is the day of "new-posted" and of both "seen" postings: the later first-seen time goes first
    assert order == ["seen-late", "seen-early", "new-posted", "seen-old", "old-posted"]


# --- C5: the age in whole hours -------------------------------------------------------------------------------------


def _basis_cases() -> list[tuple[str, date | None, datetime | None]]:
    """Every kind of basis the age is counted from, with the clock `NOW` (microseconds in it) and the day `ON`: exact
    hours, a microsecond either side of them, half seconds, a moment ahead of now, days on and across the clock's
    changes,
    a day ahead, and none."""
    out: list[tuple[str, date | None, datetime | None]] = []
    for hours in (0, 1, 2, 3, 23, 24, 25, 26, 48, 72, 24 * 40):
        exact = NOW - timedelta(hours=hours)
        for name, shift in (("exact", timedelta(0)), ("1us older", timedelta(microseconds=1)),
                            ("1us younger", -timedelta(microseconds=1)), ("half a second older", timedelta(seconds=.5)),
                            ("a second younger", -timedelta(seconds=1))):
            out.append((f"{hours} h {name}", None, exact - shift))
    out += [("an hour ahead", None, NOW + timedelta(hours=1)), ("1us ahead", None, NOW + timedelta(microseconds=1)),
            ("a moment with no microseconds", None, datetime(2026, 11, 4, 9, 0, 0, tzinfo=UTC)),
            ("a moment ahead of the owner's day", None, datetime(2026, 11, 5, 3, 0, tzinfo=UTC))]
    on = date(2026, 11, 4)
    for days in (0, 1, 2, 3, 7, 30, 31, 100, 365):
        out.append((f"{days} days back", on - timedelta(days=days), None))
    out += [("the clocks go forward", date(2026, 3, 8), None), ("the clocks go back", date(2026, 11, 1), None),
            ("a leap day", date(2024, 2, 29), None), ("a day ahead", on + timedelta(days=1), None),
            ("a month ahead", on + timedelta(days=30), None), ("no basis", None, None)]
    return out


async def test_the_age_in_whole_hours_is_the_same_in_sql_as_in_python_for_every_basis(lite):
    on = date(2026, 11, 4)
    cases = _basis_cases()
    async with db.session_factory()() as session:
        for n, (_name, day, moment) in enumerate(cases):
            await lonely_role(session, f"case-{n}", queue_day=day, queue_moment=moment)
        await session.commit()
        got = dict((await session.execute(select(Opportunity.slug, stored_order.hours(on, NOW)))).all())
    wrong = [(name, got[f"case-{n}"], freshness.AgeBasis(day, moment).hours(on, NOW))
             for n, (name, day, moment) in enumerate(cases)
             if got[f"case-{n}"] != freshness.AgeBasis(day, moment).hours(on, NOW)]
    assert wrong == []
    assert len(cases) > 60


async def test_a_lane_is_put_in_order_by_the_database_as_python_puts_it(lite):
    on = date(2026, 11, 4)
    rng = random.Random(7)
    async with db.session_factory()() as session:
        made = []
        for n in range(60):
            day = on - timedelta(days=rng.choice((0, 1, 1, 2, 9, 40))) if rng.random() < .5 else None
            moment = None if day else (NOW - timedelta(hours=rng.choice((0, 1, 1, 5, 26, 90)),
                                                      minutes=rng.choice((0, 0, 30)))
                                       if rng.random() < .8 else None)
            made.append(await lonely_role(
                session, f"r{n}", queue_points=rng.choice((0, 10, 10, 55, 90)), queue_day=day, queue_moment=moment,
                created_at=NOW - timedelta(hours=rng.choice((1, 1, 2, 50)))))
        await session.commit()

        def python_order(queued: bool) -> list[uuid.UUID]:
            def key(role: Opportunity):
                hours = freshness.AgeBasis(role.queue_day, role.queue_moment).hours(on, NOW)
                age = (hours is None, hours or 0, -role.created_at.timestamp(), role.id.hex)
                return (-role.queue_points, *age) if queued else age

            return [r.id for r in sorted(made, key=key)]

        where = (Opportunity.owner == OWNER,)
        for queued in (True, False):
            whole = await stored_order.page(session, where, queued=queued, on=on, now=NOW, limit=100)
            assert whole == python_order(queued), queued
            page = await stored_order.page(session, where, queued=queued, on=on, now=NOW, limit=7, offset=14)
            assert page == python_order(queued)[14:21]


async def test_an_update_of_the_stored_columns_moves_no_line_into_the_queue_but_the_inputs_do(lite):
    """`settle` writes the four stored columns of a role: that write names no role again (it would loop)."""
    async with db.session_factory()() as session:
        role = await lonely_role(session, "settled")
        await session.commit()
        await lines(session)
        with queue.exempt("the stored order is read only by the lanes (stored_order.py): no loop looks at it"):
            await session.execute(update(Opportunity), [{
                "id": role.id, "queue_points": 9, "queue_day": date(2026, 11, 1), "queue_moment": None,
                "queue_until": NOW}])
        await session.commit()
        assert await lines(session) == set()
