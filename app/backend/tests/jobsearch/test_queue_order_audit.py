"""The queue-order bugs (docs/PRD.md 23.7, 24.6 to 24.9, 39.21, 39.22), one test
for each, as the order stands now: Go's line is the read roles of Ready to Launch (a role
with no read that graded its posting is not in it: 24.13) in the pipeline's order, the score plus the first hours'
bonus less the stale penalty, ending on the role's id; a role scored the earlier way is rescored on the asks when what
it holds allows it; the gold edge counts from when the app first saw the posting; and the percent a card shows is
worked out once from the exact points. Every name, number and sentence is made up."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import board
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import prioritize
from app.timeutil import owner_today
from tests.builders import counted, rank_roles
from tests.support import EXAMPLE_SYSTEM, NOON, OWNER, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")
NOW = NOON  # the app's clock, so the rows made from it are the age the app counts


def role(slug: str, fit: int | None, *, possible: int | None = None, status: str = stages.READY_TO_LAUNCH,
         n: int | None = None, read: bool = False, **more) -> Opportunity:
    """A role named by its slug (company `Co <slug>`, title `Role <slug>`). With `possible` it is scored on the asks:
    its score keeps the points it was scored against, so its percent is `fit` of `possible`. Without it the role holds a
    plain fit, as one scored the earlier way does (the made-up profile's old scale runs 0 to 5). `n` gives it an id that
    sorts by that number. With `read` it has a Second Impression that graded its posting, as every role in Go's line
    has (`board.ready_line`, docs/PRD.md 24.13), and its score is its `fit` as a percent unless `possible` says other
    points: a read role is sorted by that score, not by its rank."""
    if read:
        possible = possible or 100
        more = {**counted(), **more}
    scored = bool(possible) and fit is not None
    breakdown = {"answers": [], "earned": fit, "possible": possible, "total": fit} if scored else {}
    base = {"id": uuid.UUID(int=n) if n is not None else uuid.uuid4(), "owner": OWNER, "slug": slug,
            "company": f"Co {slug}", "title": f"Role {slug}", "fit": fit, "origin": "scout",
            "match_key": f"co {slug}|role {slug}", "status": status, "found": owner_today(),
            "location": "Remote, US", "move_type": "remote", "fit_breakdown": breakdown}
    return Opportunity(**{**base, **more})


async def _line(*roles: Opportunity, ranks: dict[str, int] | None = None) -> list[str]:
    """Go's line (`board.ready_line`): the slugs of the roles in Ready to Launch in the order Go walks them. The roles
    are stored first, with the made-up profile, each with a rank (its `fit`, unless `ranks` names it by slug)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all(roles)
        await session.flush()
        given = ranks or {}
        await rank_roles(session, {r.id: given.get(r.slug, r.fit) for r in roles
                                   if given.get(r.slug, r.fit) is not None})
        await session.commit()
    async with db.session_factory()() as session:
        line, _target = await board.ready_line(session, OWNER)
    return [r.slug for r in line]


# --- 1. Go's order ----------------------------------------------------------------------------------------------------


async def test_go_takes_read_roles_by_their_score_and_not_by_their_rank_or_their_points(js):
    """A role in Go's line has a read (24.13), so its score orders it: the share of the points it was scored against,
    not the points and not the rank (which only orders a role not read yet: `test_board_order.py`). The role with the
    most points of 20 has the lowest share, so it is last, whatever its rank; a role with no score is after every
    scored one."""
    ahead = await _line(role("many-points", 6, possible=20, read=True), role("most-of-it", 4, possible=5, read=True),
                        role("half", 5, possible=10, read=True), role("unscored", None, read=True),
                        ranks={"many-points": 90, "most-of-it": 10, "half": 50})
    assert ahead == ["most-of-it", "half", "many-points", "unscored"]


async def test_go_takes_roles_by_the_score_plus_the_first_hours_bonus_less_the_stale_penalty(js):
    """The owner, 2026-10-16: score plus `pipeline.GOLDEN_BONUS` points while under 72 hours (posting dates are whole
    days, so a role posted today is inside the window), and less `pipeline.STALE_PENALTY` once the posting is over 30
    days old. A fresh 70 (90) goes first, an old 82 next, an old 90 (69) after it, and a fresh 45 and an old 65 tie
    at 65: the fresher goes first."""
    today = owner_today()
    ahead = await _line(
        role("old-90", 90, read=True, posted=today - timedelta(days=40)),
        role("old-82", 82, read=True, posted=today - timedelta(days=30)),
        role("fresh-70", 70, read=True, posted=today),
        role("fresh-45", 45, read=True, posted=today),
        role("old-65", 65, read=True, posted=today - timedelta(days=30)))
    assert ahead == ["fresh-70", "old-82", "old-90", "fresh-45", "old-65"]


async def test_go_and_the_board_hold_ready_to_launch_in_one_order(js):
    """The place Go walks a role in is the place the board shows it in (one order, `pipeline.ordered`)."""
    today = owner_today()
    roles = [role("old-90", 90, read=True, posted=today - timedelta(days=40), n=1),
             role("gold-50", 50, read=True, posted=today, n=2),
             role("old-70", 70, read=True, posted=today - timedelta(days=20), n=3),
             role("gold-60", 60, read=True, posted=today, n=4)]
    ahead = await _line(*roles)
    async with db.session_factory()() as session:
        shown = await role_row.columns(session, OWNER, stages.READY_TO_LAUNCH)
    on_board = [c["title"].removeprefix("Role ") for c in shown[0]["cards"]]
    assert ahead == on_board == ["gold-60", "gold-50", "old-70", "old-90"], (
        "80, then 70 and 70 (the fresher first), then 69: the old 90 less the stale penalty of 21")
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(Opportunity))).all()
        assert [r.slug for r in await pipeline.ordered(session, OWNER, list(rows))] == ahead


async def test_roles_the_rule_leaves_tied_are_taken_in_the_order_of_their_ids_not_the_databases(js):
    """Three roles alike in score and age, stored with the highest id first: the order is the lowest id first every
    time (when they were found is the same here too)."""
    made = {"created_at": NOW}
    third, second, first = (role(f"same-{n}", 30, n=n, read=True, **made) for n in (3, 2, 1))
    assert await _line(third, second, first) == ["same-1", "same-2", "same-3"]


# --- 2. a role scored the earlier way is scored on the asks when what it holds allows it -----------------------------


APP_ONLY = {"asks": [{"id": "tier", "section": "compensation", "says": "The ask is over 100,000", "points": 2,
                      "amount": 100000, "checked_by": "app"}]}
EARLIER = {"base": 3, "base_why": "x", "applied": [], "not_applied": [], "total": 3}


async def test_a_role_scored_the_earlier_way_is_scored_on_the_asks_when_what_it_holds_allows_it_and_not_else(js):
    async with db.session_factory()() as session:
        # Every ask is the app's own to decide: nothing waits for a reader, so the role is scored on the asks.
        await load_profile(session, OWNER, scoring=APP_ONLY)
        old = role("old", 3, comp="$150,000 - $170,000", fit_breakdown=EARLIER)
        session.add(old)
        await session.flush()
        profile = await profile_store.current(session, OWNER)
        version = await profile_store.newest_version(session, OWNER, "scoring")
        assert await prioritize.stale_scores(session, OWNER, profile, version) == 1
        done = await prioritize.rescore(session, OWNER, profile, version=version)
        assert (done.scored, done.failed) == (1, 0)
        assert scoring.is_asks(old.fit_breakdown) and old.fit == 2 and old.fit_breakdown["possible"] == 2
        assert await prioritize.stale_scores(session, OWNER, profile, version) == 0, "and it is done once"

        # No ask at all: a fit is not replaced by a score that is a share of nothing.
        await profile_store.save(session, OWNER, "scoring", {"asks": []}, by="test")
        kept = role("kept", 3, comp="$150,000 - $170,000", fit_breakdown=EARLIER)
        session.add(kept)
        await session.flush()
        profile = await profile_store.current(session, OWNER)
        version = await profile_store.newest_version(session, OWNER, "scoring")
        assert await prioritize.stale_scores(session, OWNER, profile, version) == 0
        await prioritize.rescore(session, OWNER, profile, version=version)
        assert kept.fit == 3 and kept.fit_breakdown == EARLIER


# --- 3. the gold edge counts from when the app first saw the posting ------------------------------------------------


def _posting(slug: str, role_id: uuid.UUID, hours_ago: float) -> JobPosting:
    """The posting a role was made from, first seen that many hours ago."""
    seen = NOW - timedelta(hours=hours_ago)
    return JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=f"R-{slug}",
                      company=f"Co {slug}",
                      title=f"Role {slug}", match_key=f"co {slug}|role {slug}", first_seen=seen, last_seen=seen,
                      links=[], opportunity_id=role_id)


async def test_the_gold_edge_counts_from_the_employers_date_then_the_first_sight_of_the_posting_then_the_role(client):
    today = owner_today()
    made = {"created_at": NOW - timedelta(hours=10)}  # every row was made 10 hours ago; the postings are older
    roles = [
        role("seen-long-ago", 3, status=stages.SECOND_IMPRESSION, **made),  # first seen 100 hours ago: past its 72
        role("seen-lately", 3, status=stages.SECOND_IMPRESSION, **made),  # first seen 10 hours ago
        role("dated-today", 3, status=stages.SECOND_IMPRESSION, posted=today, **made),  # the employer's date first
        # dated today by a board that dates it again on every read: the app first saw it 500 hours ago
        role("redated-today", 3, status=stages.SECOND_IMPRESSION, posted=today, **made),
        role("dated-long-ago", 3, status=stages.SECOND_IMPRESSION, posted=today - timedelta(days=10), **made),
        role("no-posting", 3, status=stages.SECOND_IMPRESSION, **made),  # no first-seen time: the role's own
        role("drafted-long-ago", 3, status=stages.READY_TO_LAUNCH, **made),
    ]
    ids = {r.slug: r.id for r in roles}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all(roles)
        await session.flush()
        for slug, hours in (("seen-long-ago", 100), ("seen-lately", 10), ("dated-today", 5), ("redated-today", 500),
                            ("dated-long-ago", 1), ("drafted-long-ago", 90)):
            session.add(_posting(slug, ids[slug], hours))
        await session.commit()
    found = (await client.get("/api/v1/board", params={"column": "second_impression"})).json()["columns"][0]["cards"]
    assert {c["title"]: c["new"] for c in found} == {
        "Role seen-long-ago": False, "Role seen-lately": True, "Role dated-today": True,
        "Role redated-today": False, "Role dated-long-ago": False, "Role no-posting": True}
    titles = [c["title"] for c in found]
    assert set(titles[:3]) == {"Role dated-today", "Role seen-lately", "Role no-posting"}, "the first 72 hours first"
    assert set(titles[3:]) == {"Role seen-long-ago", "Role redated-today", "Role dated-long-ago"}
    ready = (await client.get("/api/v1/board", params={"column": "ready_to_launch"})).json()["columns"][0]["cards"]
    assert [(c["title"], c["new"]) for c in ready] == [("Role drafted-long-ago", False)]
    page = (await client.get(f"/api/v1/roles/{ids['seen-long-ago']}")).json()
    assert page["role"]["new"] is False, "the role's own page says the same"


# --- 4. the percent a card shows is worked out once from the exact points ---------------------------------------------

TWO_HUNDRED = {"asks": [
    {"id": "a", "section": "description", "says": "The work is building programs", "points": 48},
    {"id": "b", "section": "responsibilities", "says": "The role leads a team", "points": 1},
    {"id": "c", "section": "qualifications", "says": "Asks for a field the owner knows", "points": 1},
    {"id": "d", "section": "qualifications", "says": "Asks for a second field", "points": 150}]}


async def test_the_percent_a_card_shows_is_worked_out_once_from_the_exact_points(js):
    """A role earning 50 of 200 points is 25%, one earning 49 is 24.5% and one earning 48 is 24%. The percent is worked
    out once from the exact points and rounded half up (docs/PRD.md 30.9), so the first two are shown as 25% and the
    third as 24%: what is on screen decides. (The percent was taken of the fit of 6, shown as 24% for all three.)"""

    def said(**answers):
        return [{"id": k, "answer": v, "evidence": "made up"} for k, v in {"a": "met", "b": "met", "c": "met",
                                                                            "d": "not", **answers}.items()]

    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=TWO_HUNDRED)
        profile = await profile_store.current(session, OWNER)
        rows = {}
        for slug, answers in (("over", said()), ("near", said(c="not")), ("short", said(b="not", c="not"))):
            rows[slug] = role(slug, None, status=stages.SECOND_IMPRESSION, analysis_state=prioritize.RESEARCHED)
            session.add(rows[slug])
            await session.flush()
            rows[slug].fit_breakdown = {"answers": scoring.record([], answers, profile, prioritize.BY_READER)}
            assert prioritize.work_out(rows[slug], profile, employer=None, version=1)
        span = scoring.bounds(profile)
        assert [(r.fit, r.fit_breakdown["earned"], scoring.percent_of(r, span)) for r in rows.values()] == [
            (50, 50.0, 25), (49, 49.0, 25), (48, 48.0, 24)], "each rounded once from the exact points"
