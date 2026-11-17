"""A role's age is one number, the days the gates count (the owner, 2026-11-04; docs/PRD.md 40.1).

The Age column and the freshness gate once disagreed: one
card said 12 days where the gate said 35. The gates had been moved to `freshness.posted_basis`, but the card's Age, the
stale penalty and the `new` tag (the first hours) still counted from the role's own date (or the hour the app first saw
the posting), a different day for many of the roles on the page. They all read the same day now: the Age column,
the stale penalty (`pipeline.is_stale`, `points`, `shown`), the `new` tag and the order of the lanes. The hour counts
only where a day cannot decide: a day that is itself the moment the app first saw a posting, and the order of roles of
one day. Every name and number here is made up."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest

from app import db
from app.jobsearch import ats, stages
from app.jobsearch.fit import freshness
from app.jobsearch.models import ADDED_LINK, JobPosting, Opportunity
from app.jobsearch.pipeline import deck, stored_order
from app.jobsearch.pipeline import row as role_row
from app.timeutil import owner_today, owner_zone, utc_now
from tests.builders import rank_roles
from tests.support import EXAMPLE_SYSTEM, OWNER, pin_clock

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")


def ago(days: int) -> date:
    return owner_today() - timedelta(days=days)


def seen(day: date) -> datetime:
    """Noon on the owner's local `day`: a time the app first saw a posting."""
    return datetime.combine(day, time(12), tzinfo=owner_zone())


def hours_ago(hours: float) -> datetime:
    return utc_now() - timedelta(hours=hours)


def _bare(slug: str = "x", **over) -> Opportunity:
    """A role in memory, with only the columns the age reads."""
    return Opportunity(**{"id": uuid.uuid4(), "owner": OWNER, "slug": slug, "company": f"Co {slug}",
                          "title": f"Role {slug}", "origin": "scout", "created_at": utc_now(), **over})


async def _role(session, slug: str, status: str = stages.FIRST_IMPRESSION, **over) -> Opportunity:
    role = Opportunity(**{
        "owner": OWNER, "slug": slug, "company": f"Co {slug}", "title": f"Role {slug}", "origin": "scout",
        "match_key": f"co {slug}|role {slug}", "status": status, "found": ago(0), "fit": 3, "location": "Remote, US",
        "move_type": "remote", **over})
    session.add(role)
    await session.flush()
    return role


async def _posting(session, role: Opportunity, ext: str, *, first_seen: datetime,
                   posted: date | None = None, system: str = EXAMPLE_SYSTEM, url: str | None = None) -> JobPosting:
    """One posting of the role, read from `system`: the employer's job system, or `ats.JOB_SITE` for a board's, at
    `url`."""
    posting = JobPosting(
        owner=OWNER, employer_id=uuid.uuid4(), ats=system, ext_id=f"{role.slug}-{ext}", company=role.company,
        title=role.title, match_key=role.match_key, first_seen=first_seen, last_seen=first_seen, links=[],
        opportunity_id=role.id, posted=posted, url=url)
    session.add(posting)
    await session.flush()
    return posting


# --- the Age column ------------------------------------------------------------------------------------------------


async def test_a_cards_age_is_the_days_the_gates_count_not_the_days_since_the_roles_own_date(js):
    """The case: the role says it was posted 12 days ago, a board dated the posting again on a later read, and the
    app first saw it 35 days ago. The gate said 35; the card said 12."""
    async with db.session_factory()() as session:
        role = await _role(session, "redated", posted=ago(12))
        await _posting(session, role, "a", posted=ago(12), first_seen=seen(ago(35)))
        gate = await freshness.posted_of(session, role)
        row = await role_row.row(session, OWNER, role.id)
    assert gate.days(owner_today()) == 35
    assert row["age"] == {"days": 35, "words": "35 days", "from": "found"}


async def test_a_cards_age_is_counted_from_the_newest_employer_date_among_the_roles_postings(js):
    async with db.session_factory()() as session:
        role = await _role(session, "reposted", posted=ago(40))
        await _posting(session, role, "old", posted=ago(40), first_seen=seen(ago(40)))
        await _posting(session, role, "new", posted=ago(3), first_seen=seen(ago(3)))
        row = await role_row.row(session, OWNER, role.id)
    assert row["age"] == {"days": 3, "words": "3 days", "from": "posted"}


async def test_the_same_card_in_its_column_and_on_its_own_says_the_same_age(client):
    async with db.session_factory()() as session:
        role = await _role(session, "everywhere", posted=ago(12))
        await _posting(session, role, "a", posted=ago(12), first_seen=seen(ago(35)))
        await session.commit()
        role_id = str(role.id)
    column = (await client.get("/api/v1/board", params={"column": "first_impression"})).json()["columns"][0]["cards"]
    page = (await client.get(f"/api/v1/roles/{role_id}")).json()["role"]
    assert [c["age"] for c in column] == [page["age"]] == [{"days": 35, "words": "35 days", "from": "found"}]


def test_an_added_links_age_is_the_day_the_gates_count_for_its_posting():
    """A posting nothing has named yet is counted as its own gate counts it: a date after the day the app first saw it
    is held to that day, and with no date it is the day the app first saw it."""
    for posted, first, words in ((ago(2), seen(ago(20)), "20 days"), (ago(9), seen(ago(9)), "9 days"),
                                 (None, seen(ago(4)), "4 days")):
        posting = JobPosting(owner=OWNER, company="Co link", title="Role link", posted=posted, first_seen=first)
        card = role_row._shape_posting(posting, owner_today())
        assert card["age"]["words"] == words
    held = role_row._shape_posting(
        JobPosting(owner=OWNER, company="Co link", title="Role link", posted=ago(2), first_seen=seen(ago(20))),
        owner_today())
    assert held["age"]["from"] == "found", "the day it was first seen, not the employer's"
    dated = role_row._shape_posting(
        JobPosting(owner=OWNER, company="Co link", title="Role link", posted=ago(9), first_seen=seen(ago(9))),
        owner_today())
    assert dated["age"]["from"] == "posted"


async def test_the_added_links_column_is_in_the_order_of_the_ages_its_cards_show(js):
    """The column is ordered by the day its cards count, the posting's posted day (`freshness.posted_basis`), not by the
    date the employer gave: one posted 2 days ago that the app first saw 20 days ago says 20 days and goes below one
    posted and first seen 5 days ago. Of one day, the one the app saw later goes first; a posting no day dates goes
    last."""
    now = utc_now()
    waiting = {  # title: (posted, first seen)
        "Held to seen": (ago(2), seen(ago(20))), "Dated": (ago(5), seen(ago(5))), "Seen only": (None, seen(ago(12))),
        "Early same day": (None, seen(ago(1)).replace(hour=1)), "Late same day": (None, seen(ago(1)).replace(hour=20)),
        "Fresh": (ago(0), now)}
    async with db.session_factory()() as session:
        for n, (title, (posted, first)) in enumerate(waiting.items()):
            session.add(JobPosting(
                owner=OWNER, employer_id=uuid.uuid4(), ats=ats.OWNER, ext_id=f"w{n}", company=ADDED_LINK, title=title,
                match_key=f"firm {n}|{title.lower()}", url=f"https://jobs.example.test/{n}", posted=posted,
                first_seen=first, last_seen=now, filtered_at=now, links=[], route="roles"))
        await session.flush()
        column = await role_row._added_links(session, OWNER, 10, owner_today(), None)
    cards = [(c["title"], c["age"]["days"]) for c in column["cards"]]
    assert cards == [("Fresh", 0), ("Late same day", 1), ("Early same day", 1), ("Dated", 5), ("Seen only", 12),
                     ("Held to seen", 20)]
    days = [days for _, days in cards]
    assert days == sorted(days), "the ages go up down the column"


# --- the stale penalty ---------------------------------------------------------------------------------------------


def test_the_stale_penalty_counts_from_the_posted_day_the_gates_count():
    today, now = owner_today(), utc_now()
    own = _bare(posted=ago(5))  # its own column says five days
    seen_long_ago = freshness.PostedDay(ago(40), freshness.SEEN)
    assert not deck.is_stale(own, today), "with no day given, the role's own columns"
    assert deck.is_stale(own, today, posted=seen_long_ago)
    assert deck.shown(own, 80, None, False, today, now) == 80
    assert deck.shown(own, 80, None, False, today, now, seen_long_ago) == 80 - deck.STALE_PENALTY
    assert deck.points(own, 80, None, False, today, now, seen_long_ago) == 80 - deck.STALE_PENALTY


def test_a_role_with_a_newer_req_than_its_own_date_is_not_stale():
    today = owner_today()
    old = _bare(posted=ago(40))
    newer = freshness.PostedDay(ago(3), freshness.POSTED)
    assert deck.is_stale(old, today) and not deck.is_stale(old, today, posted=newer)
    assert deck.shown(old, 80, None, False, today, utc_now(), newer) == 80


async def test_a_card_shows_the_score_less_the_penalty_of_the_posted_day(js):
    async with db.session_factory()() as session:
        role = await _role(session, "held-old", posted=ago(5))
        await _posting(session, role, "a", first_seen=seen(ago(40)))
        await rank_roles(session, {role.id: 80})
        row = await role_row.row(session, OWNER, role.id)
    assert row["score"] == 80 - deck.STALE_PENALTY
    assert row["age"]["days"] == 40


# --- the new tag and the order of the lanes -----------------------------------------------------------------------


async def test_a_posting_dated_days_ago_is_not_new_because_the_app_read_its_board_an_hour_ago(client):
    """An employer's board read for the first time shows every posting on it as first seen now: a posting its employer
    dated five days ago is five days old, however fresh the app's first look, and is no longer ahead of one first seen
    two days ago that nothing dates (no date says the day: the day the app first saw it is its age)."""
    async with db.session_factory()() as session:
        dated = await _role(session, "dated-days-ago")
        await _posting(session, dated, "a", posted=ago(5), first_seen=hours_ago(2))
        undated = await _role(session, "seen-two-days-ago")
        await _posting(session, undated, "a", first_seen=seen(ago(2)))
        await rank_roles(session, {dated.id: 60, undated.id: 60})
        await session.commit()
    cards = (await client.get("/api/v1/board", params={"column": "first_impression"})).json()["columns"][0]["cards"]
    assert [(c["title"], c["new"]) for c in cards] == [
        ("Role seen-two-days-ago", True), ("Role dated-days-ago", False)]


async def test_a_role_whose_own_date_is_old_is_new_when_a_newer_req_of_it_is(client):
    async with db.session_factory()() as session:
        role = await _role(session, "newer-req", posted=ago(40))
        await _posting(session, role, "old", posted=ago(40), first_seen=seen(ago(40)))
        await _posting(session, role, "new", posted=ago(0), first_seen=utc_now())
        await session.commit()
    (card,) = (await client.get("/api/v1/board", params={"column": "first_impression"})).json()["columns"][0]["cards"]
    assert (card["new"], card["age"]["days"], card["age"]["from"]) == (True, 0, "posted")


async def test_a_role_no_day_dates_has_an_unknown_age_and_goes_last(client):
    """No date, no day found and no posting in the log: nothing counts it (a role the tracker brought has a posting
    from the day it came in, so this is a role made some other way)."""
    async with db.session_factory()() as session:
        await _role(session, "dated", stages.APPLIED, origin="tracker", found=ago(10))
        await _role(session, "undated", stages.APPLIED, origin="tracker", found=None)
        await session.commit()
    cards = (await client.get("/api/v1/board", params={"column": "applied"})).json()["columns"][0]["cards"]
    assert [(c["title"], c["age"]["words"], c["new"]) for c in cards] == [
        ("Role dated", "10 days", False), ("Role undated", "Unknown", False)]


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    """`hour` o'clock on the owner's local `day`."""
    return datetime.combine(day, time(hour, minute), tzinfo=owner_zone())


async def test_a_card_is_new_by_the_days_its_age_says_not_by_the_hour_the_app_first_saw_the_posting(js, monkeypatch):
    """Half an hour past midnight, a posting the app first saw at 2 am three days ago is 70 hours old by the clock and
    its card says 3 days: the New tag counts the same day (a day, 72 hours, is out of the first 72 hours), so the card
    never says both "3 days" and New. The hour counts only in the order of the lanes."""
    today = date(2026, 11, 4)
    monkeypatch.setattr(role_row, "owner_today", lambda now=None: today)
    monkeypatch.setattr(role_row, "utc_now", lambda: _at(today, 0, 30))
    async with db.session_factory()() as session:
        role = await _role(session, "three-days", found=today - timedelta(days=3))
        await _posting(session, role, "a", first_seen=_at(today - timedelta(days=3), 2))
        late = await _role(session, "two-days", found=today - timedelta(days=2))
        await _posting(session, late, "a", first_seen=_at(today - timedelta(days=2), 23, 50))
        three, two = await role_row.row(session, OWNER, role.id), await role_row.row(session, OWNER, late.id)
    assert (three["age"]["days"], three["new"]) == (3, False)
    assert (two["age"]["days"], two["new"]) == (2, True), "a day still inside the first 72 hours"


def test_the_first_hours_bonus_counts_the_days_its_card_says_and_not_the_hour_of_the_first_sight():
    today, now = date(2026, 11, 4), _at(date(2026, 11, 4), 0, 30)
    role = _bare()
    three_days = freshness.PostedDay(today - timedelta(days=3), freshness.SEEN, _at(today - timedelta(days=3), 2))
    assert freshness.posting_hours(role, today, now, three_days) == 70, "the order still counts the hour"
    assert deck.points(role, 50, None, False, today, now, three_days) == 50, "no bonus: its card says 3 days"
    two_days = freshness.PostedDay(today - timedelta(days=2), freshness.SEEN, _at(today - timedelta(days=2), 23, 50))
    assert deck.points(role, 50, None, False, today, now, two_days) == 50 + deck.GOLDEN_BONUS


def test_a_role_is_as_many_hours_old_for_its_first_hours_as_the_days_its_card_says():
    today, now = date(2026, 11, 4), _at(date(2026, 11, 4), 0, 30)
    role = _bare()
    seen_then = freshness.PostedDay(today - timedelta(days=3), freshness.SEEN, _at(today - timedelta(days=3), 2))
    assert freshness.posting_day_hours(role, today, now, seen_then) == 72
    assert freshness.posting_day_hours(role, today, now, freshness.PostedDay(today, freshness.SEEN, now)) == 0
    assert freshness.posting_day_hours(role, today, now, freshness.PostedDay(today - timedelta(days=1),
                                                                             freshness.POSTED)) == 24
    made = _bare(created_at=now - timedelta(hours=7))
    assert freshness.posting_day_hours(made, today, now, freshness.PostedDay(None, None)) == 7, "no day: when made"
    assert freshness.posting_day_hours(_bare(origin="tracker"), today, now, freshness.PostedDay(None, None)) is None


def test_the_order_counts_the_hour_only_where_a_day_count_cannot_decide():
    """A date says the day and no hour: a role dated three days ago is 72 hours old, whenever the app first saw its
    posting, and it stands between the roles whose day is the moment the app first saw them (50 and 80 hours)."""
    today, now = owner_today(), utc_now()
    roles = {name: _bare(name) for name in ("seen-50", "dated-3", "seen-80")}
    days = {
        roles["seen-50"].id: freshness.PostedDay(owner_today(now - timedelta(hours=50)), freshness.SEEN,
                                                 now - timedelta(hours=50)),
        roles["dated-3"].id: freshness.PostedDay(ago(3), freshness.POSTED, now - timedelta(hours=1)),
        roles["seen-80"].id: freshness.PostedDay(owner_today(now - timedelta(hours=80)), freshness.SEEN,
                                                 now - timedelta(hours=80)),
    }
    ordered = deck.order(list(roles.values()), {}, today, now, days)
    assert [r.slug for r in ordered] == ["seen-50", "dated-3", "seen-80"]


def test_roles_of_one_day_go_by_when_they_were_found_then_by_their_ids():
    today, now = owner_today(), utc_now()
    older = _bare("older", created_at=now - timedelta(hours=9))
    newer = _bare("newer", created_at=now - timedelta(hours=1))
    day = freshness.PostedDay(ago(2), freshness.POSTED)
    ordered = deck.order([older, newer], {}, today, now, {older.id: day, newer.id: day})
    assert [r.slug for r in ordered] == ["newer", "older"]


# --- how many hours a role is -------------------------------------------------------------------------------------


def test_a_role_is_as_many_hours_as_the_days_of_its_posted_day_and_the_moment_only_where_the_day_is_one():
    today, now = owner_today(), utc_now()
    role = _bare()
    moment = now - timedelta(hours=50)
    assert freshness.posting_hours(role, today, now, freshness.PostedDay(ago(3), freshness.POSTED, moment)) == 72
    seen_then = freshness.PostedDay(owner_today(moment), freshness.SEEN, moment)
    assert freshness.posting_hours(role, today, now, seen_then) == 50
    assert freshness.posting_hours(role, today, now, freshness.PostedDay(ago(2), freshness.FOUND)) == 48


def test_a_scout_role_found_today_with_no_posting_is_counted_from_when_the_app_made_it():
    today, now = owner_today(), utc_now()
    made = now - timedelta(hours=2)
    role = _bare(created_at=made)
    day = freshness.PostedDay(owner_today(made), freshness.FOUND)
    assert freshness.posting_hours(role, today, now, day) == 2
    elsewhere = freshness.PostedDay(ago(5), freshness.FOUND)
    assert freshness.posting_hours(role, today, now, elsewhere) == 120, "the found day, when it is not the day made"
    tracker = _bare(origin="tracker", created_at=made)
    assert freshness.posting_hours(tracker, today, now, day) == 0, "a tracker's role has only its day"


@pytest.mark.parametrize(("zone", "tracker_hours"), [("UTC", 24), ("America/New_York", 0)])
def test_a_role_made_the_evening_before_is_counted_by_the_owners_own_day(monkeypatch, zone, tracker_hours):
    """Half past midnight in UTC is half past seven in the evening in New York. A scout's role made two hours ago is two
    hours old in both: the day it was found is the day it was made. A tracker's role has only its found day, and that
    day is one back in UTC and today in New York: the age is counted in the owner's day, never in UTC's."""
    now = datetime(2026, 11, 7, 0, 30, tzinfo=UTC)
    pin_clock(monkeypatch, now, zone)
    made = now - timedelta(hours=2)
    found = freshness.PostedDay(owner_today(made), freshness.FOUND)
    assert owner_today() == (date(2026, 11, 7) if zone == "UTC" else date(2026, 11, 6))
    assert freshness.posting_hours(_bare(created_at=made), owner_today(), now, found) == 2
    assert freshness.posting_hours(_bare(origin="tracker", created_at=made), owner_today(), now, found) == tracker_hours


def test_a_role_no_day_dates_is_counted_from_when_the_app_made_a_scouts_and_is_ageless_otherwise():
    today, now = owner_today(), utc_now()
    nothing = freshness.PostedDay(None, None)
    assert freshness.posting_hours(_bare(created_at=now - timedelta(hours=7)), today, now, nothing) == 7
    assert freshness.posting_hours(_bare(origin="tracker"), today, now, nothing) is None


def test_with_no_day_given_a_role_is_counted_from_its_own_columns():
    today, now = owner_today(), utc_now()
    assert freshness.posting_hours(_bare(posted=ago(4)), today, now) == 96
    assert freshness.posting_hours(_bare(found=ago(2)), today, now) == 48


def test_a_day_the_app_first_saw_a_posting_carries_that_moment_and_no_other_day_does():
    first, second = seen(ago(9)), seen(ago(4))
    got = freshness.posted_basis(None, ago(2), [(None, second), (None, first)])
    assert (got.day, got.basis, got.at) == (ago(9), freshness.SEEN, first), "the earliest"
    held = freshness.posted_basis(None, None, [(ago(2), seen(ago(20)))])
    assert (held.basis, held.at) == (freshness.SEEN, seen(ago(20))), "a date after that day is held to its moment"
    assert freshness.posted_basis(ago(3), None, [(ago(3), seen(ago(3)))]).at is None, "an employer's date has no hour"
    assert freshness.posted_basis(None, ago(7), []).at is None
    assert freshness.PostedDay(ago(9), freshness.SEEN, first) == freshness.PostedDay(ago(9), freshness.SEEN)


# --- what a call reads --------------------------------------------------------------------------------------------


async def test_a_board_reads_the_postings_of_the_roles_it_shows_and_never_the_owners_whole_set(js, monkeypatch):
    """Was: the owner's postings read once for a whole board (`reads == [None]`) and handed to each lane, which put
    every role of it in order by their age; a lane alone read its own roles'. Now (docs/PRD.md 24.19, a lane read for
    the roles it shows; the owner, 2026-11-06): the order is stored on the roles (`stored_order`), so each lane reads
    the postings of the roles on its page, by their ids, and the owner's whole set is read by no lane. A whole board
    of four lanes with one role each is one read of the four ids (the board reads its lanes' cards once), and a
    lane alone is one read of the ids it shows."""
    roles = {}
    async with db.session_factory()() as session:
        for slug, status in (("a", stages.FIRST_IMPRESSION), ("b", stages.SECOND_IMPRESSION), ("c", stages.APPLIED),
                             ("d", stages.REJECTED)):
            role = await _role(session, slug, status)
            roles[slug] = role.id
            await _posting(session, role, "p", first_seen=seen(ago(2)))
        await stored_order.settle(session, OWNER)  # the order is stored: what the board reads is only what it shows
        await session.commit()
    reads = []
    real = freshness.held_postings

    async def counted_read(session, owner, ids=None, boards=None):
        reads.append(ids)
        return await real(session, owner, ids, boards)

    monkeypatch.setattr(freshness, "held_postings", counted_read)
    async with db.session_factory()() as session:
        board = await role_row.columns(session, OWNER)
    assert None not in reads, "by the ids of the roles shown, never for the owner"
    assert len(reads) == 1, "one read for the whole board, over the roles of every lane"
    assert sorted(reads[0]) == sorted(roles.values()), "the roles every lane shows, each once"
    assert {col["key"]: [c["age"]["days"] for c in col["cards"]] for col in board
            if col["key"] in ("first_impression", "second_impression", "applied", "closed")} == {
        "first_impression": [2], "second_impression": [2], "applied": [2], "closed": [2]}
    reads.clear()
    async with db.session_factory()() as session:
        await role_row.columns(session, OWNER, stages.APPLIED)
    assert reads == [[roles["c"]]], "a lane alone reads the roles it shows, by their ids"


def test_the_row_module_keeps_the_age_it_is_given_for_a_role_in_its_hands():
    """`_age` counts from the day it is given and, with none, from the role's own columns."""
    today = owner_today()
    role = _bare(posted=ago(5), found=ago(5))
    assert role_row._age(role, today) == {"days": 5, "words": "5 days", "from": "posted"}
    given = freshness.PostedDay(ago(35), freshness.SEEN)
    assert role_row._age(role, today, given) == {"days": 35, "words": "35 days", "from": "found"}
    unknown = role_row._age(role, today, freshness.PostedDay(None, None))
    assert unknown == {"days": None, "words": "Unknown", "from": None}

