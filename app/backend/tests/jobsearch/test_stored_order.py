"""The stored order: a lane is read for the roles it shows (docs/PRD.md 24.19, the owner, 2026-11-06): there is no need
to query thousands of rows only to show the top few.

A lane was read whole to be put in order, every role of a large lane for the fifty cards of a
page. Each role now holds its number and what its age counts from (`queue_points`, `queue_day`, `queue_moment`), the
database puts a lane in order and gives one page, and what is stored is kept current by
`stored_order.settle`, told which roles by the triggers of migration 0151. What a card says and the order of every lane
and group do not change: each of these tests holds the stored order to the functions the cards are made by
(`pipeline.order`, `pipeline.age_key`, `pipeline.points`), wherever the roles are and at any hour. Every name and number
here is made up."""

import asyncio
import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text, update

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.models import JobPosting, Opportunity, QueueDirty, RoleRank
from app.jobsearch.pipeline import board, stored_order
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.sites.models import Site
from app.timeutil import owner_today, utc_now
from tests.builders import GATE_ZERO, counted, rank_roles
from tests.jobsearch.test_board_loads import WORKED, statements
from tests.jobsearch.test_card_age import _posting, ago
from tests.jobsearch.test_role_row import _role
from tests.support import OWNER, load_profile

_FIRST, _SECOND, _READY = stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH
HOUR = timedelta(hours=1)
#: The hours the order is asked at: now, an hour and a half on, a day on (the owner's day has turned), three days on
#: (the
#: first hours of the fresh are over), and forty days on (every posting is stale).
CLOCK = (timedelta(0), timedelta(minutes=90), timedelta(hours=26), timedelta(days=3, hours=1), timedelta(days=40))


def _at(base: datetime, **age) -> dict:
    """A role made `age` ago: the fields that say when."""
    return {"created_at": base - timedelta(**age)}


async def _world() -> dict[str, Opportunity]:
    """Roles in every stage and of every age the order counts: posted on a day (today to forty days back, some alike so
    that ties are broken by the age's tail), found today by the scout (counted from the moment it was made), seen
    first at a moment by a posting dated later than it, dated by nothing (a scout's, a tracker's); ranks that tie, a
    gate zero, none; reads with their own score; and a Closed lane of every reason."""
    now = utc_now()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        roles: list[Opportunity] = []
        for n, days in enumerate((0, 0, 1, 2, 3, 3, 5, 10, 29, 30, 31, 31, 40)):
            roles.append(_role(f"first-day-{n}", _FIRST, posted=ago(days), found=ago(days), **_at(now, minutes=7 * n)))
        for n in range(40):  # more than a page of the lane, in ranks that tie
            roles.append(_role(f"first-more-{n}", _FIRST, posted=ago(n % 12), found=ago(n % 12), **_at(now, minutes=n)))
        roles += [
            _role("first-same-age-a", _FIRST, posted=ago(4), found=ago(4), created_at=now - timedelta(days=4)),
            _role("first-same-age-b", _FIRST, posted=ago(4), found=ago(4), created_at=now - timedelta(days=4)),
            _role("first-found-today", _FIRST, posted=None, found=ago(0), **_at(now, hours=3)),
            _role("first-found-earlier", _FIRST, posted=None, found=ago(6), **_at(now, days=6)),
            _role("first-seen-first", _FIRST, posted=ago(0) + timedelta(days=2), found=None, **_at(now, hours=5)),
            _role("first-undated-scout", _FIRST, posted=None, found=None, **_at(now, hours=30)),
            _role("first-undated-tracker", _FIRST, posted=None, found=None, origin="tracker"),
            _role("first-gate-zero", _FIRST, posted=ago(1), found=ago(1)),
            _role("first-unranked", _FIRST, posted=ago(1), found=ago(1)),
            _role("second-read-a", _SECOND, fit=2, fit_breakdown=WORKED, posted=ago(1), found=ago(1), **counted()),
            _role("second-read-b", _SECOND, fit=2, fit_breakdown=WORKED, posted=ago(8), found=ago(8), **counted()),
            _role("second-ranked", _SECOND, posted=ago(2), found=ago(2)),
            _role("ready-read", _READY, fit=3, fit_breakdown={**WORKED, "possible": 4}, posted=ago(33), found=ago(33),
                  **counted()),
            _role("ready-ranked", _READY, posted=ago(0), found=ago(0)),
            _role("first-read", _FIRST, fit=2, fit_breakdown=WORKED, posted=ago(0), found=ago(0), **counted()),
        ]
        for status in (stages.FOLLOWUP_NEEDED, stages.LANDING_ZONE, stages.CLEANSED_DATA, stages.APPLIED,
                       stages.ACTIVE_DISCUSSION):
            for n, days in enumerate((0, 1, 1, 7, 40)):
                roles.append(_role(f"{status}-{n}", status, posted=ago(days), found=ago(days), **_at(now, hours=n)))
        for status, why in ((stages.GATED_OUT, None), (stages.REJECTED, "they_said_no"), (stages.REJECTED, None),
                            (stages.GONE_QUIET, "posting_gone"), (stages.WITHDREW, "i_passed"),
                            (stages.WITHDREW, "no list holds it"), (stages.REJECTED, "received_offer"),
                            (stages.GATED_OUT, None), (stages.GONE_QUIET, "not_my_field")):
            roles.append(_role(f"{status}-{why}-{len(roles)}", status, ended_reason=why, posted=ago(len(roles) % 9),
                               found=ago(len(roles) % 9), **_at(now, minutes=len(roles))))
        session.add_all(roles)
        await session.flush()
        by_slug = {r.slug: r for r in roles}
        # a posting the app first saw this morning, dated later than that, is counted from that moment
        await _seen_today(session, by_slug["first-seen-first"], now)
        ranks = {r.id: 60 for r in roles if r.slug.startswith(("first-day", "first-same", "second-ranked"))}
        ranks.update({r.id: 30 + n % 5 for n, r in enumerate(r for r in roles if r.slug.startswith("first-more"))})
        ranks.update({by_slug["first-gate-zero"].id: GATE_ZERO, by_slug["first-read"].id: 55,
                      by_slug["first-day-1"].id: 95,
                      by_slug["second-read-a"].id: 40, by_slug["ready-ranked"].id: 70,
                      by_slug["first-found-today"].id: 60, by_slug["first-found-earlier"].id: 60,
                      by_slug["first-undated-scout"].id: 60, by_slug["first-undated-tracker"].id: 60,
                      by_slug["first-seen-first"].id: 60})
        await rank_roles(session, ranks)
        await session.commit()
        return by_slug


async def _seen_today(session, role: Opportunity, now: datetime) -> None:
    await _posting(session, role, "seen", first_seen=now - timedelta(hours=5), posted=owner_today() + timedelta(days=2))


async def _reference(session, statuses, queued: bool, on, now) -> list[uuid.UUID]:
    """The ids of the roles of `statuses` in the order the cards are made by: whole roles, `pipeline.order` where the
    work is ahead and `pipeline.age_key` elsewhere, with the facts read for every role of the lane."""
    roles = list((await session.scalars(select(Opportunity).where(
        Opportunity.owner == OWNER, Opportunity.status.in_(statuses)))).all())
    posted = await freshness.posted_by_role(session, OWNER, roles)
    if not queued:
        return [r.id for r in sorted(roles, key=lambda r: pipeline.age_key(r, on, now, posted.get(r.id)))]
    current = await profile_store.current(session, OWNER)
    reads = await analysis.reads(session, [r.id for r in roles])
    ordered = pipeline.order(roles, await pipeline.ranks(session, OWNER), on, now, posted, scoring.bounds(current),
                             reads, pipeline.queue_rules_of(current.settings))
    return [r.id for r in ordered]


def _lanes() -> list[tuple[str, tuple[str, ...], bool, tuple]]:
    """Every lane and group the board reads, with whether the work is ahead in it, and the Closed lane by each
    reason."""
    lanes = [(key, statuses, set(statuses) <= set(role_row._QUEUED), ()) for key, statuses in stages.LANES]
    lanes += [(key, statuses, set(statuses) <= set(role_row._QUEUED), ()) for key, statuses in stages.GROUPS]
    lanes += [(f"closed:{reason}", stages.CLOSED, False, (board.closed_reason_where(reason),))
              for reason in (*board.CLOSED_LABELS, "a word that is no reason")]
    return lanes


async def test_every_roles_stored_number_and_age_are_the_ones_the_cards_are_made_by(js):
    """The column is what `pipeline.points` gives the role, as of the hour it was last worked out: for every role of a
    mixed world, and when the clock moves on (a role's first hours end, a posting turns stale), `settle` makes it so
    again. What a card prints (`shown`) is the stored number for a role with a score, and nothing for one without."""
    await _world()
    for step in CLOCK:
        async with db.session_factory()() as session:
            now = utc_now() + step
            await stored_order.settle(session, OWNER, now)
            await session.commit()
        async with db.session_factory()() as session:
            on = owner_today(now)
            current = await profile_store.current(session, OWNER)
            span, rules = scoring.bounds(current), pipeline.queue_rules_of(current.settings)
            roles = list((await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all())
            posted = await freshness.posted_by_role(session, OWNER, roles)
            ranks = await pipeline.ranks(session, OWNER)
            reads = await analysis.reads(session, [r.id for r in roles])
            assert len(roles) > 100 and len(reads) >= 4
            for r in roles:
                args = (r, ranks.get(r.id), span, r.id in reads)
                assert r.queue_points == pipeline.points(*args, on, now, posted[r.id], rules), (step, r.slug)
                shown = pipeline.shown(*args, on, now, posted[r.id], rules)
                assert (r.queue_points if pipeline.has_score(*args) else None) == shown, (step, r.slug)
                basis = freshness.age_basis(r, posted[r.id])
                assert (r.queue_day, r.queue_moment) == (basis.day, basis.moment), (step, r.slug)
            assert step or any(r.queue_points > 100 for r in roles), "a new role can pass 100: the number is not capped"
            await session.rollback()


async def test_a_page_of_any_lane_or_group_is_the_page_of_the_whole_order_at_any_hour(js):
    """The database's order is the order of the cards: every lane, the ranked list and the work in progress, the Closed
    lane by each of its reasons, a page from any offset, at the hours that change a number, a day or an age's hour. The
    ties are broken as `age_key` breaks them (the younger posting, the more recently found, then the id)."""
    await _world()
    seen_lanes = 0
    for step in CLOCK:
        async with db.session_factory()() as session:
            now = utc_now() + step
            on = owner_today(now)
            await stored_order.settle(session, OWNER, now)
            await session.commit()
            for key, statuses, queued, extra in _lanes():
                where = (Opportunity.owner == OWNER, Opportunity.status.in_(statuses), *extra)
                whole = [i for i in await _reference(session, statuses, queued, on, now)
                         if i in set(await session.scalars(select(Opportunity.id).where(*where)))]
                for limit, offset in ((1000, 0), (1, 0), (7, 0), (7, 7), (5, 23), (3, 400)):
                    got = await stored_order.page(session, where, queued=queued, on=on, now=now, limit=limit,
                                                  offset=offset)
                    assert got == whole[offset:offset + limit], (step, key, limit, offset)
                seen_lanes += bool(whole)
            await session.rollback()
    assert seen_lanes >= len(CLOCK) * 12, "the lanes and the Closed reasons held roles"


async def test_closed_reasons_are_read_in_sql_as_they_are_read_in_python(js):
    by_slug = await _world()
    async with db.session_factory()() as session:
        closed = [r for r in by_slug.values() if r.status in stages.CLOSED]
        assert len(closed) > 8
        for reason in (*board.CLOSED_LABELS, "a word that is no reason"):
            found = set(await session.scalars(select(Opportunity.id).where(
                Opportunity.owner == OWNER, Opportunity.status.in_(stages.CLOSED), board.closed_reason_where(reason))))
            under = {r.id for r in closed if board.closed_reason_key(r.status, r.ended_reason) == reason}
            assert found == under, reason


async def test_the_board_gives_the_cards_the_stored_number_and_every_lane_its_count(js):
    by_slug = await _world()
    async with db.session_factory()() as session:
        columns = {c["key"]: c for c in await role_row.columns(session, OWNER)}
        await session.rollback()
    assert columns[_FIRST]["count"] == sum(1 for r in by_slug.values() if r.status == _FIRST)
    assert columns[_FIRST]["count"] > role_row.SHOWN == len(columns[_FIRST]["cards"]), "a page of a longer lane"
    async with db.session_factory()() as session:
        span = scoring.bounds(await profile_store.current(session, OWNER))
        roles = list((await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all())
        ranks = await pipeline.ranks(session, OWNER)
        reads = await analysis.reads(session, [r.id for r in roles])
        stored = {str(r.id): (r.queue_points if pipeline.has_score(r, ranks.get(r.id), span, r.id in reads) else None)
                  for r in roles}
        await session.rollback()
    cards = [c for column in columns.values() for c in column["cards"]]
    assert len(cards) > 40 and all(c["score"] == stored[c["id"]] for c in cards)


async def test_a_lane_reads_only_the_roles_it_shows(js):
    """A page of the board reads no role it does not show: the lane's roles come from one statement with a limit, the
    cards by their ids, the ranks, reads and postings of the cards shown by their ids, and the owner's whole list of
    ranks and postings is read by no statement."""
    await _world()
    async with db.session_factory()() as session:
        await stored_order.settle(session, OWNER)
        await session.commit()
    with statements() as seen:
        async with db.session_factory()() as session:
            columns = await role_row.columns(session, OWNER)
            await session.rollback()
    assert sum(len(c["cards"]) for c in columns) > 40
    unbounded = [q for q in seen if q.startswith("SELECT js_opportunities.") and " LIMIT " not in q
                 and "json_each" not in q and "js_opportunities.id IN" not in q
                 and "queue_until <=" not in q]  # the roles a day has turned on, which the index names
    assert not unbounded, f"roles read with no limit and no ids: {[q[:200] for q in unbounded]}"
    lanes = [q for q in seen if q.startswith("SELECT js_opportunities.id FROM") and " LIMIT " in q]
    assert len(lanes) == len(stages.LANES), "one limited statement for each lane"
    for q in seen:
        if ("FROM js_role_ranks" in q or "FROM js_postings" in q) and "count(*)" not in q and " LIMIT " not in q:
            assert "json_each" in q, f"read for every role of the owner: {q[:160]}"
    assert not [q for q in seen if "js_queue_dirty" in q and q.startswith(("UPDATE", "DELETE", "INSERT"))], (
        "nothing was owed: the board wrote nothing")


# --- kept current
# -------------------------------------------------------------------------------------------------------


async def _numbers(session) -> dict[uuid.UUID, tuple]:
    rows = (await session.execute(select(Opportunity.id, *stored_order.STORED).where(Opportunity.owner == OWNER))).all()
    return {r[0]: tuple(r[1:]) for r in rows}


async def _agrees(session, now: datetime | None = None) -> None:
    """After a read of the board every stored order is what it would be worked out from scratch: no input was missed."""
    now = now or utc_now()
    on = owner_today(now)
    await stored_order.settle(session, OWNER, now)
    await session.commit()
    current = await profile_store.current(session, OWNER)
    span, rules = scoring.bounds(current), pipeline.queue_rules_of(current.settings)
    roles = list((await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all())
    posted = await freshness.posted_by_role(session, OWNER, roles)
    ranks = await pipeline.ranks(session, OWNER)
    reads = await analysis.reads(session, [r.id for r in roles])
    wrong = []
    for r in roles:
        args = (r, ranks.get(r.id), span, r.id in reads)
        basis = freshness.age_basis(r, posted[r.id])
        want = (pipeline.points(*args, on, now, posted[r.id], rules), basis.day, basis.moment)
        if (r.queue_points, r.queue_day, r.queue_moment) != want:
            wrong.append((r.slug, (r.queue_points, r.queue_day, r.queue_moment), want))
    assert not wrong, wrong
    await session.rollback()


async def _fresh_session_agrees(now: datetime | None = None) -> None:
    async with db.session_factory()() as session:
        await _agrees(session, now)


async def test_a_new_role_and_every_input_of_the_number_name_the_role_to_work_out_again(js):
    by_slug = await _world()
    await _fresh_session_agrees()
    async with db.session_factory()() as session:
        assert not (await session.scalars(select(QueueDirty))).all(), "settled"
        plain = by_slug["first-day-5"]
        # a rank, a newer rank, a role's dates, a read, a posting's dates and a role made
        await rank_roles(session, {plain.id: 91})
        await session.commit()
        await _fresh_session_agrees()
        async with db.session_factory()() as other:
            await other.execute(update(Opportunity).where(Opportunity.id == plain.id).values(posted=ago(60)))
            await other.commit()
        await _fresh_session_agrees()
        async with db.session_factory()() as other:
            await other.execute(update(Opportunity).where(Opportunity.id == plain.id).values(
                analysis_state=analysis.DONE, analysis=counted()["analysis"], fit=2, fit_breakdown=WORKED))
            await other.commit()
        await _fresh_session_agrees()
        async with db.session_factory()() as other:
            role = await other.get(Opportunity, by_slug["first-day-0"].id)
            await _posting_dated(other, role, ago(80))
            await other.commit()
        await _fresh_session_agrees()
        async with db.session_factory()() as other:
            other.add(_role("brand-new", _FIRST, posted=ago(0), found=ago(0)))
            await other.commit()
        await _fresh_session_agrees()
        await session.rollback()
    async with db.session_factory()() as session:
        (first,) = await role_row.columns(session, OWNER, _FIRST)
        assert "Role brand-new" in {c["title"] for c in first["cards"]}
        await session.rollback()


async def _posting_dated(session, role: Opportunity, day) -> JobPosting:
    return await _posting(session, role, "dated", first_seen=utc_now() - timedelta(days=90), posted=day)


async def test_a_rank_written_after_the_board_is_read_is_in_the_order_of_the_next_read(js):
    by_slug = await _world()
    role = by_slug["first-day-6"]  # five days old, a rank of 60: no bonus, no penalty
    async with db.session_factory()() as session:
        before = [c["id"] for c in (await role_row.columns(session, OWNER, _FIRST))[0]["cards"]]
        await rank_roles(session, {role.id: 100})
        await session.commit()
    async with db.session_factory()() as session:
        after = [c["id"] for c in (await role_row.columns(session, OWNER, _FIRST))[0]["cards"]]
        want = await _reference(session, (_FIRST,), True, owner_today(), utc_now())
        await session.rollback()
    assert after == [str(i) for i in want]
    assert before.index(str(role.id)) > 5 and after.index(str(role.id)) < 3, "from the middle of the lane to its top"


async def test_a_rank_deleted_or_changed_names_its_role(js):
    by_slug = await _world()
    await _fresh_session_agrees()
    role = by_slug["first-day-3"]
    async with db.session_factory()() as session:
        await session.execute(update(RoleRank).where(RoleRank.role_id == role.id).values(fit=7))
        await session.commit()
        assert [(line.role_id) for line in await session.scalars(select(QueueDirty))] == [role.id]
    await _fresh_session_agrees()
    async with db.session_factory()() as session:
        await session.execute(text("DELETE FROM js_role_ranks WHERE role_id = :id"), {"id": role.id.hex})
        await session.commit()
        assert [(line.role_id) for line in await session.scalars(select(QueueDirty))] == [role.id]
    await _fresh_session_agrees()


async def test_a_saved_profile_or_a_changed_list_of_job_sites_names_every_role_of_the_owner(js):
    await _world()
    await _fresh_session_agrees()
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "settings", {"avoid_peak": False, "golden_bonus": 5}, by="test")
        await session.commit()
        lines = list(await session.scalars(select(QueueDirty)))
        assert [(line.owner, line.role_id) for line in lines] == [(OWNER, None)]
        assert all(len(line.owner) for line in lines)
    await _fresh_session_agrees()
    async with db.session_factory()() as session:
        rules = pipeline.queue_rules_of((await profile_store.current(session, OWNER)).settings)
        assert rules.golden_bonus == 5
        session.add(Site(owner=OWNER, url="https://boards.example.test", kind="job_board", list_name="search",
                         created_at=utc_now()))
        await session.commit()
        assert [(line.role_id) for line in await session.scalars(select(QueueDirty))] == [None]
    await _fresh_session_agrees()


async def test_a_write_no_input_of_the_order_reads_names_no_role(js):
    """The lines are for the inputs only: a role's text, its notes and status, a posting's text and last-seen time, and
    a saved resume are not."""
    by_slug = await _world()
    await _fresh_session_agrees()
    role = by_slug["first-day-4"]
    async with db.session_factory()() as session:
        posting = await _posting(session, role, "plain", first_seen=utc_now() - timedelta(days=2), posted=ago(2))
        await session.commit()
        await session.execute(delete(QueueDirty))
        await session.commit()
        await session.execute(update(Opportunity).where(Opportunity.id == role.id).values(
            notes="a note", description="a text", status=stages.SECOND_IMPRESSION, favorited_at=utc_now()))
        await session.execute(update(JobPosting).where(JobPosting.id == posting.id).values(
            text="a text", last_seen=utc_now(), title="Another title", filtered_at=utc_now()))
        await profile_store.save(session, OWNER, "resume", {"summary": "A resume."}, by="test")
        await session.commit()
        assert not (await session.scalars(select(QueueDirty))).all()
        await session.rollback()


async def test_a_settle_that_finds_nothing_changed_writes_nothing(js):
    await _world()
    await _fresh_session_agrees()
    with statements() as seen:
        async with db.session_factory()() as session:
            assert await stored_order.settle(session, OWNER) == 0
            await session.commit()
    assert not [q for q in seen if q.startswith(("UPDATE", "INSERT", "DELETE"))], seen
    async with db.session_factory()() as session:
        everything = list(await session.scalars(select(Opportunity.id).where(Opportunity.owner == OWNER)))
        assert (await stored_order.refresh(session, OWNER, everything)).written == 0
        await session.rollback()


async def test_a_role_is_worked_out_again_when_the_clock_passes_the_instant_its_number_changes_and_not_before(js):
    """Time changes a number only as the owner's day turns (the first hours end, a posting turns stale), and
    `queue_until` is that instant: at each one the roles it names are worked out again, with no line and no write of a
    role that did not change, and the order is what it would be worked out from scratch an instant before and at it."""
    by_slug = await _world()
    async with db.session_factory()() as session:
        await stored_order.settle(session, OWNER)
        await session.commit()
        everyone = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all()
        stored = {r.slug: r for r in everyone}
        assert stored["first-day-2"].queue_points == 60 + pipeline.DEFAULT_RULES.golden_bonus
        instants = sorted({r.queue_until for r in stored.values() if r.queue_until})
        await session.rollback()
    assert len(instants) >= 3 and all(i > utc_now() for i in instants)
    async with db.session_factory()() as session:
        assert await stored_order.settle(session, OWNER, instants[0] - timedelta(seconds=1)) == 0, "nothing is due yet"
        await session.commit()
    for instant in instants[:5]:
        await _fresh_session_agrees(instant - timedelta(seconds=1))
        await _fresh_session_agrees(instant)
    async with db.session_factory()() as session:
        late = await session.get(Opportunity, by_slug["first-day-2"].id)
        assert late.queue_points == 60, "its first hours are over"
        assert not (await session.scalars(select(QueueDirty))).all()
        await session.rollback()


async def test_a_settle_begun_while_the_writer_turn_is_held_waits_for_it_and_then_writes_the_role_once(js):
    """There is no role to hold on SQLite, only the one writer turn: a settle that begins while another transaction
    has it waits, the line still names the role for a reader meanwhile, and then the role is written once and the line
    goes."""
    by_slug = await _world()
    await _fresh_session_agrees()
    held = by_slug["first-day-1"]
    async with db.session_factory()() as session:
        await rank_roles(session, {held.id: 99})
        await session.commit()
    holding, let_go = asyncio.Event(), asyncio.Event()

    async def holder():
        async with db.session_factory()() as other:
            await other.execute(select(Opportunity.id).limit(1))  # a writer session's first statement takes the turn
            holding.set()
            await let_go.wait()
            await other.rollback()

    async def settling():
        async with db.session_factory()() as session:
            written = await stored_order.settle(session, OWNER)
            await session.commit()
            return written

    holding_task = asyncio.get_running_loop().create_task(holder())
    await holding.wait()
    settle = asyncio.get_running_loop().create_task(settling())
    await asyncio.sleep(0.3)
    assert not settle.done(), "the settle waits for the turn"
    async with db.read_session_factory()() as reader:
        lines = list(await reader.scalars(select(QueueDirty)))
        assert [line.role_id for line in lines] == [held.id], "and the role is still named"
    let_go.set()
    assert await asyncio.wait_for(settle, 10) == 1
    await holding_task
    async with db.read_session_factory()() as reader:
        assert not (await reader.scalars(select(QueueDirty))).all()
    await _fresh_session_agrees()


async def test_a_start_works_the_order_out_again_for_every_role_and_mends_what_no_line_named(js):
    """A release that finds a role with nothing stored has the owner named (migration 0151), and a start checks every
    role anyway: one the triggers did not name (a rule of the number changed in a release, a write nothing saw) is
    mended, and a role that is right is not written."""
    by_slug = await _world()
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).values(queue_points=0, queue_day=None, queue_moment=None,
                                                         queue_until=None))
        session.add(QueueDirty(owner=OWNER, role_id=None))
        await session.commit()
    from app.jobsearch.loops import startup

    async with db.session_factory()() as session:
        await startup.run_startup_fixups(session, OWNER)
        await session.commit()
    await _fresh_session_agrees()
    async with db.session_factory()() as session:
        assert (await session.scalar(select(Opportunity.queue_points).where(
            Opportunity.id == by_slug["ready-ranked"].id))) == 70 + pipeline.DEFAULT_RULES.golden_bonus
        await session.execute(update(Opportunity).where(Opportunity.id == by_slug["first-day-3"].id).values(
            queue_points=3))  # a write no trigger names: the column is not an input
        await session.commit()
        assert not (await session.scalars(select(QueueDirty))).all()
    async with db.session_factory()() as session:
        await startup.run_startup_fixups(session, OWNER)
        await session.commit()
    await _fresh_session_agrees()
    with statements() as seen:
        async with db.session_factory()() as session:
            assert await stored_order.settle(session, OWNER, everything=True) == 0
            await session.commit()
    assert not [q for q in seen if q.startswith("UPDATE js_opportunities")], "a role that is right is not written"


async def test_the_sql_of_an_age_is_the_age_of_the_python_for_each_basis(js):
    """`stored_order.hours` is `AgeBasis.hours`: a moment counts hours since it, a day counts whole days as hours, and a
    role no day dates has none. The hours are asked at moments either side of a whole hour."""
    by_slug = await _world()
    await _fresh_session_agrees()
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all())
        posted = await freshness.posted_by_role(session, OWNER, roles)
        bases = {freshness.age_basis(r, posted[r.id]) for r in roles}
        assert any(b.moment for b in bases) and any(b.day for b in bases) and freshness.AgeBasis(None, None) in bases
        for step in (timedelta(0), timedelta(minutes=59, seconds=59), HOUR, timedelta(hours=49, seconds=1)):
            now = utc_now() + step
            on = owner_today(now)
            got = dict((await session.execute(select(Opportunity.id, stored_order.hours(on, now)).where(
                Opportunity.owner == OWNER))).all())
            for r in roles:
                want = freshness.posting_hours(r, on, now, posted[r.id])
                assert (None if got[r.id] is None else int(got[r.id])) == want, (r.slug, step)
        await session.rollback()
    assert by_slug


async def test_the_decks_lists_are_ordered_from_the_ranks_of_their_own_roles_and_not_the_owners_whole_list(js):
    """Go's list of Ready to Launch (`board.ready_line`) and the analyst's line order a few roles: the ranks read for
    them are theirs, by their ids, and the order is the one the cards print. Was: the owner's whole list of ranks
    (21,000 live) read to order fifteen roles."""
    by_slug = await _world()
    async with db.session_factory()() as session:
        await stored_order.settle(session, OWNER)
        await session.commit()
    with statements() as seen:
        async with db.session_factory()() as session:
            line, size = await board.ready_line(session, OWNER)
            second = await analysis.picks(session, OWNER, None)
            slugs = {r.slug for r in line}
            await session.rollback()
    ranks = [q for q in seen if "FROM js_role_ranks" in q]
    assert ranks and all("json_each" in q for q in ranks), "the ranks of the roles ordered, by their ids"
    assert size == pipeline.DAILY_DEFAULT
    assert slugs == {"ready-read"}, "a Ready role that was read (the other has no Second Impression)"
    assert by_slug and isinstance(second, list)


async def test_the_hour_tick_settles_the_roles_a_turn_of_the_owners_day_has_passed(js):
    """The hour tick's own look (`handlers.settle_order`, docs/PRD.md 24.19): a role whose first hours end as the day
    turns is changed by no write, so only the clock asks, and the tick works it out."""
    from app.jobsearch.work.queue import handlers, service, topics

    by_slug = await _world()
    async with db.session_factory()() as session:
        await stored_order.settle(session, OWNER)
        await session.commit()
        fresh = await session.get(Opportunity, by_slug["first-day-2"].id)
        ends = fresh.queue_until
        before = fresh.queue_points
        await session.rollback()
    assert ends is not None and before == 60 + pipeline.DEFAULT_RULES.golden_bonus
    group = service.Group(owner=OWNER, topic=topics.TICK_HOUR, keys=("",), ids=(1,), causes=frozenset({"tick"}),
                          attempts=0, waiting_since=datetime.now(UTC))
    await handlers.settle_order(handlers.Run(OWNER, group, ends - timedelta(seconds=1), handlers.Tally()))
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, by_slug["first-day-2"].id)).queue_points == before, "not yet"
        await session.rollback()
    await handlers.settle_order(handlers.Run(OWNER, group, ends, handlers.Tally()))
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, by_slug["first-day-2"].id)).queue_points == 60, "and now"
        await session.rollback()


# --- a long pass does not hold what it wrote, and a board read does not fail with the settle it asks first
# -----------------------------------------------------------------------------------------------------------------


async def test_a_pass_lets_go_of_the_roles_of_a_chunk_before_it_works_the_next(js, monkeypatch):
    """`refresh` writes a chunk at a time, and a transaction that kept every role it wrote until the end held the first
    chunk's roles for the whole pass (far longer than the lock timeout of a
    request, `db.LOCK_TIMEOUT_MS`, on a large account). SQLite has no row lock; the same promise is the one writer's
    turn: before each later
    chunk is worked, a second writer changes the roles the chunks before it wrote, and its write gets through at once
    (it would queue behind the pass until its commit if the pass kept the turn)."""
    by_slug = await _world()
    monkeypatch.setattr(stored_order, "CHUNK", 25)
    real = stored_order.work_out
    chunks = 0
    written: list[uuid.UUID] = []  # what the chunks before this one wrote
    free: list[bool] = []  # for each later chunk: whether a second writer got through while the roles were written

    async def work_out(session, owner, ids, *args):
        nonlocal chunks
        chunks += 1
        if written:
            async with db.session_factory()() as other:
                try:
                    await asyncio.wait_for(other.execute(
                        update(Opportunity).where(db.among(Opportunity.id, written))
                        .values(queue_points=Opportunity.queue_points)), 1)
                    free.append(True)
                except TimeoutError:  # queued behind the pass: it still holds the writer's turn
                    free.append(False)
                await other.rollback()
        worked = await real(session, owner, ids, *args)
        written.extend(i for i, was, new in worked if new != was)
        return worked

    monkeypatch.setattr(stored_order, "work_out", work_out)
    ids = [r.id for r in by_slug.values()]
    async with db.session_factory()() as session:
        done = await stored_order.refresh(session, OWNER, ids)
        await session.commit()
    assert chunks == math.ceil(len(ids) / 25) >= 4 and done.written == len(written) > 100
    assert free == [True] * (chunks - 1), "a role an earlier chunk wrote was held by the pass still working"


async def test_a_pass_inside_a_savepoint_is_one_transaction_to_its_end(js, monkeypatch):
    """A caller inside a savepoint (a process step, the engine's call) cannot release its transaction
    (`db.can_release`): every chunk is written, none is committed before the transaction's owner commits. (The start's
    steps are no savepoint: each commits on its own, `startup.run_startup_fixups`.)"""
    by_slug = await _world()
    monkeypatch.setattr(stored_order, "CHUNK", 25)
    ids = [r.id for r in by_slug.values()]
    stored = select(func.count()).select_from(Opportunity).where(Opportunity.owner == OWNER,
                                                                 Opportunity.queue_points > 0)
    async with db.session_factory()() as session:
        async with session.begin_nested():
            done = await stored_order.refresh(session, OWNER, ids)
        assert done.written > 100
        async with db.read_session_factory()() as other:  # a reader: it sees only what is committed
            assert await other.scalar(stored) == 0, "nothing is committed while the transaction is the owner's"
            await other.rollback()
        await session.commit()
    async with db.session_factory()() as session:
        assert await session.scalar(stored) > 50
        await session.rollback()


async def test_a_chunk_is_read_without_the_writers_turn_and_the_pass_writes_what_the_one_transaction_pass_writes(
        js, monkeypatch):
    """Working a chunk out only reads, and on a large database the reads were most of the time a chunk held the
    file's one writer turn. Every chunk is read in a reader's session, and a second writer gets through while it is
    read (the first chunk too, which no role was written before). What the pass writes is what the pass inside one
    savepoint writes."""
    by_slug = await _world()
    monkeypatch.setattr(stored_order, "CHUNK", 25)
    ids = [r.id for r in by_slug.values()]
    real = stored_order.work_out
    readers: list[bool] = []
    free: list[bool] = []

    async def work_out(session, *args):
        readers.append(db.is_reader(session))
        if not db.is_reader(session):
            return await real(session, *args)  # a second writer inside this writer's transaction is a bug of its own
        async with db.session_factory()() as other:
            try:
                await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.id == ids[0])
                                                     .values(queue_points=Opportunity.queue_points)), 1)
                free.append(True)
            except TimeoutError:  # queued behind the pass: it holds the writer's turn while it reads
                free.append(False)
            await other.rollback()
        return await real(session, *args)

    async def stored() -> dict:
        async with db.session_factory()() as session:
            rows = (await session.execute(select(Opportunity.id, *stored_order.STORED).order_by(Opportunity.id))).all()
            await session.rollback()
        return {row[0]: tuple(row[1:]) for row in rows}

    monkeypatch.setattr(stored_order, "work_out", work_out)
    async with db.session_factory()() as session:
        paged = await stored_order.refresh(session, OWNER, ids)
        await session.commit()
    after_paged = await stored()
    assert len(readers) == len(free) == math.ceil(len(ids) / 25) >= 4
    assert all(readers) and all(free), "a chunk was read in a writer's transaction, holding the turn"
    assert paged.written > 100
    zero = update(Opportunity).values(queue_points=0, queue_day=None, queue_moment=None, queue_until=None)
    async with db.session_factory()() as session:
        await session.execute(zero)
        await session.commit()
    readers.clear()
    async with db.session_factory()() as session:
        async with session.begin_nested():
            whole = await stored_order.refresh(session, OWNER, ids)
        await session.commit()
    assert not any(readers), "inside a savepoint the pass reads in its own transaction, as it always did"
    assert (whole.written, await stored()) == (paged.written, after_paged)


async def _settle_that_raises(session, owner, now=None, *, everything=False):
    raise RuntimeError("the settle broke")


async def _settle_that_fails_in_the_database(session, owner, now=None, *, everything=False):
    await session.execute(text("SELECT * FROM no_such_table"))  # the statement fails in the database


@pytest.mark.parametrize("settle", [_settle_that_raises, _settle_that_fails_in_the_database],
                         ids=["raises", "fails in the database"])
async def test_a_board_read_whose_settle_fails_reads_its_lanes_from_what_is_stored(js, monkeypatch, caplog, settle):
    """The board asks `settle` before it reads a lane, as the hour's tick does, and a failure of it is the tick's
    to bear alone there (logged, tried again at the next hour): here too the transaction is rolled back, the failure is
    logged, and the lanes are read from what is stored, the same cards as with the settle done."""
    await _world()
    async with db.session_factory()() as session:
        before = await role_row.columns(session, OWNER)  # settles the whole world
        await session.rollback()
    monkeypatch.setattr(stored_order, "settle", settle)
    async with db.session_factory()() as session:
        with caplog.at_level(logging.WARNING):
            after = await role_row.columns(session, OWNER)
        await session.rollback()

    def shown(columns):
        return [(c["key"], c["count"], [card["id"] for card in c["cards"]]) for c in columns]

    assert sum(len(c["cards"]) for c in after) > 40
    assert shown(after) == shown(before)
    assert [r.getMessage() for r in caplog.records if "stored order" in r.getMessage()], "the failure is logged"
