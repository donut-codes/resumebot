"""Rule 1 of the quiet rules: a role whose posting the employer took
down moves to Gone Quiet, "The posting went", with the evidence in its history. Only the employer's own board speaks;
only a whole read that could have returned the posting closes it; a role the owner moved by hand since the closing is
left alone. Synthetic data only: every employer, title and address here is made up (reserved example domains)."""


import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.adapters._ask import Asked
from app.jobsearch.employers import core as employers
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, Opportunity, StageMove
from app.jobsearch.pipeline import board, placement, quiet
from app.jobsearch.postings import index as postings_module
from app.models.work import TrailEvent
from app.settings import settings
from app.sites import where
from tests.support import EXAMPLE_SYSTEM, OWNER

#: The first read saw the postings; the read that found them gone came three days later (owner days are UTC here).
SEEN = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)


READ = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


TITLE = "Operations Analyst"


@pytest.fixture(autouse=True)
def owner_days_are_utc(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")


@pytest.fixture
def settled(monkeypatch):
    """`where.settle_watch` stood in by a counter, so a test can say how often a run asks for the watch to follow."""
    calls: list[str] = []

    async def settle_watch(session, owner):
        calls.append(owner)

    monkeypatch.setattr(where, "settle_watch", settle_watch)
    return calls


@dataclass
class Listing:
    postings: list[dict]
    truncated: bool = False
    asked: Asked | None = None
    note: str | None = None
    board: dict | None = None


def _raw(ext_id: str, title: str = TITLE) -> dict:
    return {"ext_id": ext_id, "title": title, "url": f"https://boards.example.test/acme/jobs/{ext_id}"}


def _counts() -> dict:
    return {"seen": 0, "new": 0, "reopened": 0, "closed": 0}


async def _employer(session, name: str = "Acme", system: str = EXAMPLE_SYSTEM):
    employer = await employers.find_or_make(session, OWNER, name)
    employer.ats, employer.board = system, {"slug": name.lower()}
    await session.flush()
    return employer


def _role(slug: str, status: str = stages.APPLIED, **more) -> Opportunity:
    return Opportunity(
        owner=OWNER, slug=slug, company=more.pop("company", "Acme"), title=more.pop("title", TITLE), status=status,
        match_key=f"acme|{slug}", origin="scout", fit=3, **more)


def _posting(employer_id, ext_id: str, *, system: str = EXAMPLE_SYSTEM, role: Opportunity | None = None,
             closed: datetime | None = None, not_checked: datetime | None = None, **more) -> JobPosting:
    company = more.pop("company", "Acme")
    title = more.pop("title", TITLE)
    more.setdefault("url", f"https://boards.example.test/acme/jobs/{ext_id}")
    return JobPosting(
        owner=OWNER, employer_id=employer_id, ats=system, ext_id=ext_id, company=company, title=title,
        match_key=match_key(company, title), first_seen=SEEN, last_seen=SEEN, links=more.pop("links", []),
        closed_at=closed, not_checked_at=not_checked, opportunity_id=role.id if role else None, **more)


async def _read(session, employer, ext_ids, at: datetime, **more) -> dict:
    """One read of the employer's board, through the real store: what it does not list it closes, when it could have."""
    counts = _counts()
    await postings_module.store(
        session, OWNER, Listing([_raw(e) for e in ext_ids], **more), source_id=employer.id, ats_name=EXAMPLE_SYSTEM,
        company="Acme", now=at, counts=counts)
    return counts


async def _hand(session, role_id: uuid.UUID, at: datetime, event: str = "role_moved") -> None:
    """A line the owner's hand left (`board.move`, `packets.start`), at the time given."""
    session.add(TrailEvent(owner=OWNER, at=at, actor="person", who="Tester", event=event, sentence="You moved a role.",
                           link_kind="opportunity", link_id=role_id))
    await session.flush()


async def _lines(session, role_id: uuid.UUID, event: str = quiet.POSTING_WENT_EVENT) -> list[TrailEvent]:
    return list((await session.scalars(select(TrailEvent).where(
        TrailEvent.owner == OWNER, TrailEvent.link_id == role_id, TrailEvent.event == event)
        .order_by(TrailEvent.at))).all())


async def _state(session, role: Opportunity) -> tuple[str, str | None]:
    await session.refresh(role)
    return role.status, role.ended_reason


async def test_the_evidence_names_the_employers_own_site_when_that_is_where_it_was_read(js):
    async with db.session_factory()() as session:
        site = uuid.uuid4()
        role = _role("a")
        session.add(role)
        await session.flush()
        session.add(_posting(site, "own-1", system="own_site", role=role, closed=READ))
        await session.flush()
        await quiet.run(session, OWNER, today=READ.date())
        (line,) = await _lines(session, role.id)
    assert line.detail.startswith(f"Acme's own site no longer lists \"{TITLE}\". It was last seen on Oct 9 and was ")


async def test_a_read_of_only_what_is_new_a_read_cut_short_and_a_filtered_read_close_nothing_so_no_role_moves(js):
    async with db.session_factory()() as session:
        acme = await _employer(session)
        role = _role("a")
        session.add(role)
        await session.flush()
        await _read(session, acme, ["keep", "gone"], SEEN)
        (gone,) = (await session.scalars(select(JobPosting).where(JobPosting.ext_id == "gone"))).all()
        gone.opportunity_id = role.id
        await session.flush()
        for kind, more in {
                "new only": {"asked": Asked(new_only=True)}, "cut short": {"truncated": True},
                "other places": {"asked": Asked(within=lambda posting: False)}}.items():
            counts = await _read(session, acme, ["keep"], READ, **more)
            assert counts["closed"] == 0, kind
            assert (await quiet.run(session, OWNER, today=READ.date())).went == 0, kind
            assert await _state(session, role) == ("applied", None), kind
        assert (await _read(session, acme, ["keep"], READ))["closed"] == 1, "a whole read does close it"
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 1


async def test_a_repost_leaves_the_role_where_it_is(js):
    async with db.session_factory()() as session:
        acme = await _employer(session)
        role = _role("a")
        session.add(role)
        await session.flush()
        session.add_all([_posting(acme.id, "old", role=role, closed=READ), _posting(acme.id, "new", role=role)])
        await session.flush()
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 0
        assert await _state(session, role) == ("applied", None)
        assert await _lines(session, role.id) == []


async def test_only_the_employers_own_board_speaks_for_a_role(js):
    async with db.session_factory()() as session:
        acme = await _employer(session)
        roles = {slug: _role(slug) for slug in (
            "not-checked", "browser-open", "job-site-only", "scout-only", "import-only", "closed-and-open")}
        session.add_all(roles.values())
        await session.flush()
        session.add_all([
            # A closed employer posting beside one nothing is checking: nothing says the whole employer took it down.
            _posting(acme.id, "nc-1", role=roles["not-checked"], closed=READ),
            _posting(acme.id, "nc-2", role=roles["not-checked"], not_checked=READ),
            # A closed employer posting beside an open one read in the owner's browser: the browser does not speak.
            _posting(acme.id, "br-1", role=roles["browser-open"], closed=READ),
            _posting(uuid.uuid4(), "br-2", system="browser", role=roles["browser-open"]),
            # A job site's listing says nothing about the employer.
            _posting(uuid.uuid4(), "js-1", system="job_site", role=roles["job-site-only"], closed=READ),
            # What the log keeps for a role is no board's: no read ever closes it, and it is no evidence either way.
            _posting(acme.id, "sc-1", system="scout", role=roles["scout-only"], closed=READ),
            _posting(acme.id, "im-1", system="import", role=roles["import-only"], closed=READ),
            # A closed employer posting beside an open one on the same board.
            _posting(acme.id, "co-1", role=roles["closed-and-open"], closed=READ),
            _posting(acme.id, "co-2", role=roles["closed-and-open"]),
        ])
        await session.flush()
        await quiet.run(session, OWNER, today=READ.date())
        states = {slug: (await _state(session, role))[0] for slug, role in roles.items()}
    assert states == {
        "not-checked": "applied", "browser-open": "gone_quiet", "job-site-only": "applied", "scout-only": "applied",
        "import-only": "applied", "closed-and-open": "applied"}


#: The address a role's posting carries an id in (a number of five digits or more, as the job systems write it).
POSTING_URL = "https://jobs.example.test/acme/jobs/4412345"


async def _followed(session, *, role_links: dict, posting_links: dict, other_employer: bool = False):
    """A role at Acme that the app followed to the employer's own posting (so it carries a link, but the posting was
    never linked to it), and the employer's closed, unlinked posting. Returns the role."""
    acme = await _employer(session)
    elsewhere = await _employer(session, "Globex") if other_employer else acme
    role = _role("followed", employer_id=acme.id, **role_links)
    session.add(role)
    await session.flush()
    session.add(_posting(elsewhere.id, "unlinked", closed=READ, **posting_links))
    await session.flush()
    return role


@pytest.mark.parametrize("way, role_links, posting_links", [
    # (a) the role's url is the apply link one of its job-site postings carried, and the employer's posting has that
    #     address as its own.
    ("a", {"url": "https://apply.example.test/acme/apply/77"}, {"url": "https://apply.example.test/acme/apply/77"}),
    # (b) the adopted link is the posting's apply link, while its own address is another.
    ("b", {"url": "https://apply.example.test/acme/go/9"},
     {"url": "https://boards.example.test/acme/jobs/100", "apply_url": "https://apply.example.test/acme/go/9"}),
    # (c) the role's url is the posting's address with a tracking suffix, the address carrying an id.
    ("c", {"url": f"{POSTING_URL}?utm_source=newsletter"}, {"url": POSTING_URL}),
    # (d) the role's url has www. and the posting's does not, the same id.
    ("d", {"url": "https://www.jobs.example.test/acme/jobs/4412345"}, {"url": POSTING_URL}),
    # a link the posting appeared under counts as well, and so does the role's board link.
    ("links", {"board_url": POSTING_URL}, {"url": "https://boards.example.test/acme/jobs/1", "links": [POSTING_URL]}),
])
async def test_a_role_followed_to_its_employers_posting_moves_when_that_unlinked_posting_closes(
        js, way, role_links, posting_links):
    async with db.session_factory()() as session:
        role = await _followed(session, role_links=role_links, posting_links=posting_links)
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 1, way
        assert await _state(session, role) == ("gone_quiet", "posting_gone")


async def test_an_unlinked_posting_of_another_employer_or_a_link_with_no_id_is_not_the_roles(js):
    async with db.session_factory()() as session:
        other = await _followed(
            session, role_links={"url": POSTING_URL}, posting_links={"url": f"{POSTING_URL}?src=feed"},
            other_employer=True)
        # The same host and the same id, but the posting is another employer's: a careers host shared by two employers.
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 0
        assert await _state(session, other) == ("applied", None)
    async with db.session_factory()() as session:
        board_page = "https://jobs.example.test/acme/careers"
        role = _role("page", url=f"{board_page}?utm_source=x")
        session.add(role)
        await session.flush()
        acme = await _employer(session)
        session.add(_posting(acme.id, "page-1", closed=READ, url=board_page))
        await session.flush()
        # Two links with no id that differ by a suffix name no posting: a careers page can be shared by two roles.
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 0
        assert await _state(session, role) == ("applied", None)


async def test_a_linked_posting_and_an_unlinked_one_that_both_close_give_one_move_and_one_line(js):
    async with db.session_factory()() as session:
        acme = await _employer(session)
        role = _role("a", employer_id=acme.id, url=POSTING_URL)
        session.add(role)
        await session.flush()
        session.add_all([
            _posting(acme.id, "linked", role=role, closed=READ - timedelta(days=1)),
            _posting(acme.id, "unlinked", closed=READ, url=POSTING_URL)])
        await session.flush()
        result = await quiet.run(session, OWNER, today=READ.date())
        assert result.went == 1
        (line,) = await _lines(session, role.id)
    assert "was not in the read of Oct 12" in (line.detail or ""), "the latest closed posting is the one named"


async def test_an_unlinked_posting_with_an_open_sibling_that_carries_the_same_link_keeps_the_role(js):
    async with db.session_factory()() as session:
        acme = await _employer(session)
        role = _role("a", employer_id=acme.id, url=POSTING_URL)
        session.add(role)
        await session.flush()
        session.add_all([
            _posting(acme.id, "closed", closed=READ, url=POSTING_URL),
            _posting(acme.id, "open", url=f"{POSTING_URL}?src=feed")])
        await session.flush()
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 0


async def _applied_with_closed_posting(session, **more):
    acme = await _employer(session)
    role = _role("a", **more)
    session.add(role)
    await session.flush()
    posting = _posting(acme.id, "p", role=role, closed=READ)
    session.add(posting)
    await session.flush()
    return role, posting


async def test_a_role_marked_applied_by_hand_before_the_closing_moves(js):
    async with db.session_factory()() as session:
        role, _posting_row = await _applied_with_closed_posting(session)
        await _hand(session, role.id, READ - timedelta(days=2))
        await _hand(session, role.id, READ - timedelta(hours=1), event="packet_asked")
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 1
        assert await _state(session, role) == ("gone_quiet", "posting_gone")


async def test_a_role_the_owner_moves_back_after_the_closing_stays_back_every_night(js):
    async with db.session_factory()() as session:
        role, posting = await _applied_with_closed_posting(session)
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 1
        await board.move(session, OWNER, role.id, stages.APPLIED)  # the owner knows the posting is live
        assert await _state(session, role) == ("applied", None), "moved back, and its reason cleared"
        for night in range(3):
            assert (await quiet.run(session, OWNER, today=READ.date() + timedelta(days=night))).went == 0, night
            assert await _state(session, role) == ("applied", None)
        assert len(await _lines(session, role.id)) == 1, "no second line"
        # The posting opens again and closes again: a new closing, later than the owner's hand.
        posting.closed_at = None
        await session.flush()
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 0, "open: nothing to move"
        posting.closed_at = datetime.now(UTC) + timedelta(minutes=5)
        await session.flush()
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 1
        assert await _state(session, role) == ("gone_quiet", "posting_gone")
        assert len(await _lines(session, role.id)) == 2


async def test_a_second_run_moves_nothing_and_writes_no_second_line(js):
    async with db.session_factory()() as session:
        role, _posting_row = await _applied_with_closed_posting(session)
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 1
        again = await quiet.run(session, OWNER, today=READ.date())
        assert (again.went, again.silent) == (0, 0)
        assert len(await _lines(session, role.id)) == 1


async def test_a_move_writes_a_stage_move_frees_the_employer_and_settles_the_watch_once_for_the_run(js, settled):
    async with db.session_factory()() as session:
        acme = await _employer(session)
        roles = [_role(slug, employer_id=acme.id) for slug in ("a", "b", "c")]
        session.add_all(roles)
        await session.flush()
        session.add_all([_posting(acme.id, f"p-{role.slug}", role=role, closed=READ) for role in roles])
        await session.flush()

        async def in_play() -> int:
            return len(list(await session.scalars(select(Opportunity.id).where(
                Opportunity.employer_id == acme.id, Opportunity.status.in_(stages.OPEN)))))

        assert await in_play() == 3
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 3
        assert await in_play() == 0, "out of the roles in play, which is all the same-employer limit counts"
        moves = list(await session.scalars(select(StageMove).where(StageMove.role_id.in_([r.id for r in roles]))
                                           .where(StageMove.from_status == "applied")))
        assert {(m.to_status, m.role_id) for m in moves} == {("gone_quiet", r.id) for r in roles}
    assert settled == [OWNER], "once for the run, not once for each role"


async def test_a_role_a_rule_moved_is_not_a_hand_move(js):
    async with db.session_factory()() as session:
        role, _posting_row = await _applied_with_closed_posting(session)
        assert (await quiet.run(session, OWNER, today=READ.date())).went == 1
        assert not (await placement.one(session, OWNER, role, placement.Purpose.GATES)).held
        assert await quiet.hand_since(session, OWNER, [role.id]) == {}


async def test_end_role_sets_the_reason_before_the_flush_writes_the_line_and_returns_it(js):
    async with db.session_factory()() as session:
        role = _role("a")
        session.add(role)
        await session.flush()
        line = await quiet.end_role(
            session, OWNER, role, reason="no_reply", event="no_reply", sentence="x" * 300, detail="Because.")
        assert isinstance(line, TrailEvent) and line.id is not None
        assert (line.actor, line.tier, line.event, line.link_kind, line.link_id) == (
            "app", "result", "no_reply", "opportunity", role.id)
        assert len(line.sentence) <= 140, "cut, never refused"
        assert (role.status, role.ended_reason) == ("gone_quiet", "no_reply")
        move = (await session.scalars(select(StageMove).where(StageMove.role_id == role.id)
                                      .where(StageMove.to_status == "gone_quiet"))).one()
        assert move.from_status == "applied"


async def test_end_role_can_end_a_role_in_rejected_for_the_mail_reader(js):
    async with db.session_factory()() as session:
        role = _role("a")
        session.add(role)
        await session.flush()
        line = await quiet.end_role(
            session, OWNER, role, reason="they_said_no", event="mail_rejected", sentence="Rejected by mail.",
            detail=None, to=stages.REJECTED)
        assert (role.status, role.ended_reason) == ("rejected", "they_said_no")
        assert line.event == "mail_rejected" and line.detail is None
        with pytest.raises(ValueError):
            await quiet.end_role(session, OWNER, role, reason="no_reply", event="x", sentence="x", detail=None,
                                 to=stages.APPLIED)
        with pytest.raises(ValueError):
            await quiet.end_role(session, OWNER, role, reason="because", event="x", sentence="x", detail=None)


async def test_hand_since_is_the_latest_owner_hand_line_of_each_role_and_nothing_else(js):
    async with db.session_factory()() as session:
        mine, theirs, none = _role("a"), _role("b"), _role("c")
        session.add_all([mine, theirs, none])
        await session.flush()
        early, late = READ - timedelta(days=5), READ - timedelta(days=1)
        await _hand(session, mine.id, early)
        await _hand(session, mine.id, late, event="packet_asked")
        await _hand(session, theirs.id, early)
        # Not the owner's hand: an app line, another event of the owner's, another owner's line.
        session.add_all([
            TrailEvent(owner=OWNER, at=READ, actor="app", who="resumebot", event="role_moved", sentence="x",
                       link_kind="opportunity", link_id=theirs.id),
            TrailEvent(owner=OWNER, at=READ, actor="person", who="Tester", event="still_worth_it_kept", sentence="x",
                       link_kind="opportunity", link_id=theirs.id),
            TrailEvent(owner="someone-else", at=READ, actor="person", who="Other", event="role_moved", sentence="x",
                       link_kind="opportunity", link_id=theirs.id)])
        await session.flush()
        found = await quiet.hand_since(session, OWNER, [mine.id, theirs.id, none.id])
    assert found == {mine.id: late, theirs.id: early}
