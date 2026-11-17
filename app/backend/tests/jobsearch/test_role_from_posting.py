"""A posting that passed the gates becomes a role by code:
`role_from_posting.make_roles` makes it, newest first, a batch at a time; a posting that now fails a gate is stopped;
one that is the owner's role already is linked to it; an unnamed added link waits. Synthetic data only."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import ats, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import ADDED_LINK, JobPosting, Opportunity
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.postings import raw_copies
from app.jobsearch.research import rank
from tests.support import EXAMPLE_SYSTEM, OWNER, gate_says, load_profile, waiting_count

NOW = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
TEXT = " ".join(f"Lead programs team {i} and report to the board on its yearly plan." for i in range(8))


async def _post(session, n, *, company="Fabrikam", **more) -> JobPosting:
    """A posting that passed the filter, remote, waiting for a role (route roles)."""
    employer = await employers.find_or_make(session, OWNER, company)
    employer_id = employer.id if employer else uuid.uuid4()  # an unnamed added link has no employer
    title = more.pop("title", f"Programs Director {n}")
    row = JobPosting(**{
        "owner": OWNER, "employer_id": employer_id, "ats": EXAMPLE_SYSTEM, "ext_id": f"p{n}", "company": company,
        "title": title, "match_key": match_key(company, title), "location": "Remote", "work_mode": "remote",
        "url": f"https://jobs.example.test/{n}", "first_seen": NOW, "last_seen": NOW, "filtered_at": NOW,
        "route": "roles", "links": [], **more})
    session.add(row)
    await session.flush()
    return row


async def _roles(session) -> list[Opportunity]:
    return list((await session.scalars(select(Opportunity).order_by(Opportunity.created_at))).all())


async def _made(session) -> set[str]:
    """The ext ids of the postings that have a role."""
    return {p.ext_id.removeprefix("p") for p in (await session.scalars(
        select(JobPosting).where(JobPosting.opportunity_id.is_not(None)))).all()}


async def test_postings_become_roles_newest_first_by_posted_date_else_first_seen_a_batch_at_a_time(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await _post(session, "oct1", posted=date(2026, 10, 1))
        await _post(session, "oct10", posted=date(2026, 10, 10))
        await _post(session, "undated", first_seen=datetime(2026, 10, 5, 12, tzinfo=UTC))
        await _post(session, "sep1", posted=date(2026, 9, 1))
        assert await waiting_count(session, OWNER) == 4
        assert await role_from_posting.make_roles(session, OWNER, limit=2) == 2
        assert await _made(session) == {"oct10", "undated"}, "the two newest: the undated one by the day first seen"
        assert await role_from_posting.make_roles(session, OWNER, limit=2) == 2
        assert await _made(session) == {"oct10", "undated", "oct1", "sep1"}, "the rest on the next call"
        assert await role_from_posting.make_roles(session, OWNER) == 0


async def test_a_posting_that_now_fails_a_gate_is_stopped_with_its_reason_and_no_role_is_made(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        gone = await _post(session, "far", location="Greater Gotham", work_mode="onsite")
        fine = await _post(session, "near")
        assert await role_from_posting.make_roles(session, OWNER) == 1
        assert gone.filtered_by == gate_says()["location"] and gone.opportunity_id is None
        assert fine.opportunity_id is not None and fine.filtered_by is None
        assert [r.title for r in await _roles(session)] == [fine.title]
        assert await waiting_count(session, OWNER) == 0, "a stopped posting no longer waits"


async def test_a_posting_that_is_the_owners_role_already_is_linked_to_it_and_none_is_made(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        mine = Opportunity(owner=OWNER, slug="mine", company="Fabrikam", title="Programs Director 1", fit=3,
                           origin="owner", match_key=match_key("Fabrikam", "Programs Director 1"),
                           status=stages.SECOND_IMPRESSION, found=NOW.date())
        session.add(mine)
        await session.flush()
        same = await _post(session, 1)
        other = await _post(session, 2)
        assert await role_from_posting.make_roles(session, OWNER) == 1, "only the other one is new"
        assert same.opportunity_id == mine.id
        assert other.opportunity_id not in (None, mine.id)
        assert len(await _roles(session)) == 2


async def test_an_unnamed_added_link_waits_and_added_links_holds_only_those(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        link = await _post(session, "link", company=ADDED_LINK, title="jobs.example.test/jobs/1", ats=ats.OWNER)
        named = await _post(session, "named")
        assert await role_from_posting.make_roles(session, OWNER) == 1
        assert link.opportunity_id is None and named.opportunity_id is not None
        waiting = (await session.scalars(select(JobPosting).where(
            *role_from_posting.waiting_added_where(OWNER)))).all()
        assert [p.id for p in waiting] == [link.id]
        assert await waiting_count(session, OWNER) == 0, "no title to make a role of"
        await session.commit()
    columns = {c["key"]: c for c in (await client.get("/api/v1/board")).json()["columns"]}
    assert [c["title"] for c in columns["added_links"]["cards"]] == ["jobs.example.test/jobs/1"]
    assert columns["added_links"]["count"] == 1


async def test_the_role_carries_the_postings_facts_and_its_text_only_when_the_source_is_its_own(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        full = await _post(session, "ats", comp_min=120_000, comp_max=150_000, posted=date(2026, 10, 9), text=TEXT,
                           apply_url="https://jobs.example.test/apply/ats")
        own = await _post(session, "own", ats=ats.OWN_SITE, text=TEXT, posted=date(2026, 10, 8))
        board = await _post(session, "board", ats="boardone", text=TEXT, posted=date(2026, 10, 7))
        assert await role_from_posting.make_roles(session, OWNER) == 3
        roles = {r.id: r for r in await _roles(session)}
        role = roles[full.opportunity_id]
        employer = await employers.find(session, OWNER, "Fabrikam")
        assert (role.company, role.title, role.location, role.move_type) == (
            "Fabrikam", "Programs Director ats", "Remote", "remote")
        assert (role.comp, role.url, role.apply_url, role.posted) == (
            "$120,000-$150,000", "https://jobs.example.test/ats", "https://jobs.example.test/apply/ats",
            date(2026, 10, 9))
        assert (role.status, role.origin, role.employer_id) == (stages.LANDING_ZONE, "scout", employer.id)
        assert role.match_key == full.match_key and role.ats == EXAMPLE_SYSTEM
        assert role.description == TEXT.strip() and role.description_at is not None, "an ATS's own text"
        assert roles[own.opportunity_id].description == TEXT.strip(), "an employer's own site"
        assert roles[board.opportunity_id].description is None, "a job board's text is not the description"


#: A page read whole: markup and a skip link in front of the posting, past what a snippet is.
PAGE_DUMP = "<!DOCTYPE html>" + chr(10) + "Skip to content" + chr(10) + TEXT


async def test_a_posting_that_holds_a_page_read_whole_gives_its_role_no_description(js):
    """Found with the role-creation rule written twice: the scout's path took a snippet and a page dump, and this one
    took a page dump as the role's description whenever it was long enough."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        dump = await _post(session, "dump", text=PAGE_DUMP)
        assert await role_from_posting.make_roles(session, OWNER) == 1
        role = await session.get(Opportunity, dump.opportunity_id)
        assert role.description is None and role.description_at is None


async def test_many_waiting_postings_drain_in_order_over_repeated_runs_of_the_pipeline(js, monkeypatch):
    monkeypatch.setattr(role_from_posting, "BATCH", 10)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        for n in range(25):  # the higher the number, the newer the posting
            await _post(session, f"{n:02d}", posted=date(2026, 9, 1) + timedelta(days=n))
        await session.commit()
    seen: list[set[str]] = []
    for _ in range(3):
        async with db.session_factory()() as session:
            await pipeline.run(session, OWNER)
            await session.commit()
            seen.append(await _made(session))
    assert [len(s) for s in seen] == [10, 20, 25]
    assert seen[0] == {f"{n:02d}" for n in range(15, 25)}, "the newest ten first"
    assert seen[1] - seen[0] == {f"{n:02d}" for n in range(5, 15)}
    async with db.session_factory()() as session:
        assert await waiting_count(session, OWNER) == 0
        assert await session.scalar(select(func.count()).select_from(Opportunity)) == 25


def _roles_made_in_each(transactions) -> list[int]:
    """How many roles each transaction that ended and wrote any inserted (a slice's roles are one statement of many
    rows). The reads that find the postings, the rules and the job boards are made in a reader's transaction of their
    own, which writes nothing and is not one of the slices."""
    def rows(statement: str) -> int:
        return statement.count("), (") + 1 if "VALUES (" in statement else 1

    return [sum(rows(statement) for statement in each if "INSERT INTO js_opportunities" in statement)
            for each in transactions.ended if any("INSERT INTO js_opportunities" in statement for statement in each)]


async def _waiting_by_age(n: int) -> None:
    """`n` postings waiting for a role, the higher the number the newer the posting."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        for i in range(n):
            await _post(session, f"{i:02d}", posted=date(2026, 9, 1) + timedelta(days=i))
        await session.commit()


async def test_a_batch_of_more_than_one_slice_is_saved_a_slice_at_a_time(js, monkeypatch, transactions):
    """The turn's batch of 300 was one long transaction: the
    roles' rows were held to its end. It is made in slices, each saved before the next (the write
    bar of PRD 17: no write waits over 250 ms; the project rules: a transaction is held only for its database work)."""
    monkeypatch.setattr(role_from_posting, "SLICE", 4)
    await _waiting_by_age(10)
    transactions.clear()
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER) == 10
        await session.commit()
    assert _roles_made_in_each(transactions) == [4, 4, 2], "the newest four, the next four, the last two"


async def test_the_owners_link_of_one_slice_is_not_saved_before_the_caller_saves_it(js, monkeypatch, transactions):
    """A call for the postings it is given (the owner's added link, `only`) is part of a request: it never ends the
    transaction. A batch that is not narrowed by `only` is saved before the watch list is settled
    (`test_deck_stage_holds`: the watch list is settled after the last slice is saved)."""
    monkeypatch.setattr(role_from_posting, "SLICE", 4)
    await _waiting_by_age(10)
    async with db.session_factory()() as session:
        ids = [p.id for p in (await session.scalars(select(JobPosting).where(JobPosting.ext_id.in_(
            ["p00", "p01", "p02", "p03"])))).all()]
        transactions.clear()
        assert await role_from_posting.make_roles(session, OWNER, only=ids) == 4, "as many as a slice holds"
        assert transactions.ended == [], "nothing was saved by the call"
        await session.commit()


async def test_a_batch_is_saved_in_slices_of_forty(js, transactions):
    """The slice is sized by what making a role costs, a few milliseconds a posting: 40 postings are
    0.18 s, inside the 250 ms write bar of PRD 17; 50 are 0.23 s, at its edge and over it at the run-to-run spread."""
    await _waiting_by_age(41)
    transactions.clear()
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER) == 41
        await session.commit()
    assert _roles_made_in_each(transactions) == [40, 1]


async def test_a_slice_writes_its_roles_in_one_flush(js):
    """Each role was its own flush, a round trip to the database each and a turn for the other tasks between them. The
    slice's reads and its gate checks come first and the roles are written together, so the writer is held for one
    statement batch."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session as OrmSession

    await _waiting_by_age(10)
    flushes: list[int] = []

    def counted(_session, _ctx) -> None:
        flushes.append(1)

    event.listen(OrmSession, "after_flush", counted)
    try:
        async with db.session_factory()() as session:
            assert await role_from_posting.make_roles(session, OWNER) == 10
            await session.commit()
    finally:
        event.remove(OrmSession, "after_flush", counted)
    assert len(flushes) <= 2, f"ten roles took {len(flushes)} flushes"


@pytest.mark.parametrize("inside", ["a savepoint", "a process step"])
async def test_a_batch_of_many_slices_is_not_saved_inside_a_savepoint_or_a_process_step(
        js, monkeypatch, transactions, inside):
    """Neither can end its transaction (`db.release_if_free` does nothing there): every slice is made in the caller's
    own, which the caller saves."""
    monkeypatch.setattr(role_from_posting, "SLICE", 4)
    await _waiting_by_age(10)
    async with db.session_factory()() as session:
        transactions.clear()
        if inside == "a savepoint":
            async with session.begin_nested():
                made = await role_from_posting.make_roles(session, OWNER)
        else:
            with db.holding("a process step"):
                made = await role_from_posting.make_roles(session, OWNER)
        assert made == 10, "every posting was made a role"
        assert transactions.ended == [], "nothing was saved by the call"
        await session.commit()
    assert _roles_made_in_each(transactions) == [10], "all ten in the caller's transaction"


async def test_a_posting_another_transaction_changed_between_two_slices_is_looked_at_again(js, monkeypatch):
    """Postings of a later slice were read before the earlier slices were saved: one stopped meanwhile gets no role,
    and one whose role another transaction made meanwhile is linked to that role, not given a second."""
    monkeypatch.setattr(role_from_posting, "SLICE", 4)
    await _waiting_by_age(10)  # in slices: 09 08 07 06 | 05 04 03 02 | 01 00
    real = db.release_if_free
    done = []

    async def interfering(session):
        saved = await real(session)
        if not done and saved:
            done.append(True)
            async with db.session_factory()() as other:
                stopped = await other.scalar(select(JobPosting).where(JobPosting.ext_id == "p04"))
                stopped.filtered_by = "pay"
                other.add(Opportunity(
                    owner=OWNER, slug="theirs", company="Fabrikam", title="Programs Director 03", fit=3, origin="owner",
                    match_key=match_key("Fabrikam", "Programs Director 03"), status=stages.SECOND_IMPRESSION,
                    found=NOW.date()))
                await other.commit()
        return saved

    monkeypatch.setattr(db, "release_if_free", interfering)
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER) == 8, "ten less the two the other dealt with"
        await session.commit()
    async with db.session_factory()() as session:
        by_ext = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
        theirs = await session.scalar(select(Opportunity.id).where(Opportunity.slug == "theirs"))
        assert by_ext["p04"].opportunity_id is None and by_ext["p04"].filtered_by == "pay", "stopped, and no role"
        assert by_ext["p03"].opportunity_id == theirs, "linked to the role that was made meanwhile"
        assert await session.scalar(select(func.count()).select_from(Opportunity)) == 9, "8 made, and theirs"


async def test_a_failure_in_a_later_slice_leaves_the_earlier_slices_saved_and_its_own_postings_waiting(js, monkeypatch):
    """A slice is written together: one that fails while its roles are judged has written none of them, the slices
    before it are saved, and a second run makes the rest, none twice."""
    from app.jobsearch.gating import check as gating

    monkeypatch.setattr(role_from_posting, "SLICE", 4)
    await _waiting_by_age(10)  # in slices: 09 08 07 06 | 05 04 03 02 | 01 00
    real, looked = gating.check, []

    def failing(subject, rules):
        looked.append(subject)
        if len(looked) == 7:  # the third posting of the second slice
            raise RuntimeError("a gate failed")
        return real(subject, rules)

    monkeypatch.setattr(gating, "check", failing)
    async with db.session_factory()() as session:
        with pytest.raises(RuntimeError):
            await role_from_posting.make_roles(session, OWNER)
        await session.rollback()
    async with db.session_factory()() as session:
        assert await _made(session) == {"09", "08", "07", "06"}, "the first slice was saved, the second wrote nothing"
        assert await waiting_count(session, OWNER) == 6
    monkeypatch.setattr(gating, "check", real)
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER) == 6
        await session.commit()
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Opportunity)) == 10, "ten roles, none twice"
        assert await waiting_count(session, OWNER) == 0


async def test_a_role_another_transaction_made_while_a_slice_saved_its_new_employer_is_linked_not_made_twice(
        js, monkeypatch):
    """A slice that makes an employer saves it at once (`release`), which is a moment in which another transaction may
    make the role of one of its postings: the owner's roles of the slice are read after that, so the role is found and
    linked, and no second is written (the owner holds one role to a writing: `uq_js_opportunities_owner_match_key`)."""
    monkeypatch.setattr(role_from_posting, "SLICE", 4)
    await _waiting_by_age(10)  # in slices: 09 08 07 06 | 05 04 03 02 | 01 00
    async with db.session_factory()() as session:  # 04 is a company that has no employer record yet: its slice makes it
        row = await session.scalar(select(JobPosting).where(JobPosting.ext_id == "p04"))
        row.company, row.match_key = "Initech", match_key("Initech", row.title)
        await session.commit()
    real, calls = db.release_if_free, []

    async def interfering(session):
        saved = await real(session)
        calls.append(saved)
        if len(calls) == 2:  # the first saves slice one when slice two begins; the second saves the new employer
            async with db.session_factory()() as other:
                other.add(Opportunity(
                    owner=OWNER, slug="theirs", company="Initech", title="Programs Director 04", fit=3, origin="owner",
                    match_key=match_key("Initech", "Programs Director 04"), status=stages.SECOND_IMPRESSION,
                    found=NOW.date()))
                await other.commit()
        return saved

    monkeypatch.setattr(db, "release_if_free", interfering)
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER) == 9, "ten less the one the other transaction made"
        await session.commit()
    assert len(calls) >= 2
    async with db.session_factory()() as session:
        theirs = await session.scalar(select(Opportunity.id).where(Opportunity.slug == "theirs"))
        row = await session.scalar(select(JobPosting).where(JobPosting.ext_id == "p04"))
        assert row.opportunity_id == theirs, "linked to the role made meanwhile"
        assert await session.scalar(select(func.count()).select_from(Opportunity)) == 10, "9 made, and theirs"


async def test_a_role_made_from_a_posting_is_ranked_once_it_has_a_cleansed_copy(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        row = await _post(session, "rank", text=TEXT)
        assert await role_from_posting.make_roles(session, OWNER) == 1
        role_id = row.opportunity_id
        assert await rank.unranked(session, OWNER, 1) == [], "no cleansed copy yet: nothing to rank"
        await raw_copies.keep_legacy(session, OWNER)
        assert [r for r, _ in await rank.unranked(session, OWNER, 1)] == [role_id]


async def test_a_job_the_owner_added_makes_the_owners_role_sourced_by_its_link_and_asked_for_ahead_of_the_line(js):
    """The Crowe case: the link the owner pasted is the employer's own posting, so the role it makes is the owner's
    (not a scout's), is Sourced by that link, and carries the owner's ask so research reads it first."""
    from app.jobsearch.research import analysis

    link = "https://careers.example.test/job/R-52052/Transformation-Initiative-Lead"
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        added = await _post(session, "own", ats=ats.OWNER, url=link, links=[link], title="Transformation Lead")
        await _post(session, "scout")
        assert await role_from_posting.make_roles(session, OWNER) == 2
        mine = await session.get(Opportunity, added.opportunity_id)
        assert (mine.origin, mine.source_url, mine.source_kind) == ("owner", link, "employer")
        assert mine.sourced_at is not None
        assert mine.analysis_state in (analysis.WAITING, analysis.NO_TEXT), "queued for research, or for its text"
        assert (mine.analysis or {}).get(analysis.ASKED_BY) == analysis.ASKED_BY_OWNER
        scouts = [r for r in await _roles(session) if r.id != mine.id]
        assert [(r.origin, r.analysis_state, r.analysis) for r in scouts] == [("scout", None, {})], (
            "a scout's role is as before")


async def test_a_pipeline_turn_runs_the_gates_on_a_posting_nobody_has_filtered_and_makes_its_role(js):
    """The CLA case: a read cut short before its filter step left postings no stage had looked at; any pipeline turn
    now puts them through the code gates, and the ones that pass become roles in Landing Zone or beyond."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        fresh = await _post(session, "fresh", filtered_at=None, route=None)
        far = await _post(session, "far2", filtered_at=None, route=None, location="Greater Gotham", work_mode="onsite")
        await pipeline.run(session, OWNER)
        assert fresh.filtered_at is not None and fresh.opportunity_id is not None
        assert far.filtered_at is not None and far.filtered_by == gate_says()["location"] and far.opportunity_id is None
