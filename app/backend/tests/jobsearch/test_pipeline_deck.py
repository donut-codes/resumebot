"""The best-of queue and the stages around it (`pipeline/deck.py`): First Impression, Second Impression and Ready to
Launch
compete on one score; Second Impression holds only read roles, Sonnet reads from the top of First Impression, Ready to
Launch is the best N read roles; a score closes no role, a role waiting for the analyst that is not marked to be
read is taken out of the line unless the owner asked for it, a stale posting drops below a fresher one of a similar
score, a hard gate closes a role at Cleansed Data on its cleansed copy. Everything here is made up."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.employers.core import find_or_make
from app.jobsearch.fetch import host_stops
from app.jobsearch.loops import schedule
from app.jobsearch.models import (
    SOURCE_NEXT,
    BrowserDomain,
    CleansedPosting,
    JobPosting,
    Opportunity,
    Packet,
    RawPosting,
)
from app.jobsearch.pipeline import board, placement
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.postings import raw_copies
from app.jobsearch.profile.schema import Settings
from app.jobsearch.research import analysis, line
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from app.models.work import TrailEvent
from app.timeutil import owner_today
from app.trail import service as trail
from tests.builders import GATE_ZERO, counted, rank_roles
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

pytestmark = pytest.mark.usefixtures("made_up_boards")

TODAY = owner_today()


def _role(slug: str, status: str, **more) -> Opportunity:
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": f"Firm {slug}", "title": f"Programs Lead {slug}", "origin": "scout",
        "match_key": f"firm {slug}|programs lead {slug}", "status": status, "found": TODAY, "posted": TODAY, **more})


async def _world(n: int | None, *roles: Opportunity, ranks: dict[str, int] | None = None,
                 **settings) -> dict[str, uuid.UUID]:
    """The made-up profile with N as the owner's daily number (and any other setting), these roles stored and ranked
    (`ranks` by slug)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": n, **settings})
        session.add_all(roles)
        await session.flush()
        by_slug = {r.slug: r.id for r in roles}
        await rank_roles(session, {by_slug[s]: fit for s, fit in (ranks or {}).items()})
        await session.commit()
        return by_slug


async def _balance(session) -> int:
    """One run of the best-of queue (`pipeline.rebalance`): how many roles changed place."""
    return sum(await pipeline.rebalance(session, OWNER))


async def _statuses() -> dict[str, str]:
    async with db.session_factory()() as session:
        rows = await session.scalars(select(Opportunity).execution_options(populate_existing=True))
        return {r.slug: r.status for r in rows}


async def _events(slug: str) -> list[str]:
    async with db.session_factory()() as session:
        role_id = await session.scalar(select(Opportunity.id).where(Opportunity.slug == slug))
        return list(await session.scalars(select(TrailEvent.event).where(
            TrailEvent.link_kind == "opportunity", TrailEvent.link_id == role_id)))


# --- the best-of queue ---------------------------------------------------------------------------------------------
# Replaced behaviour (the owner, 2026-10-21): First Impression, Second Impression and Ready to Launch are one queue on
# one score. Second Impression holds only read roles; Sonnet reads from the top of First Impression, a role
# being read (marked `line.READ_NEXT`) stays there; Ready to Launch is always the best N read roles.


async def _marked() -> set[str]:
    async with db.session_factory()() as session:
        by_id = {r.id: r.slug for r in await session.scalars(select(Opportunity))}
        return {by_id[i] for i in await pipeline.marked_ids(session, OWNER)}


async def test_only_the_top_n_unread_roles_by_estimate_are_marked_to_be_read_and_a_low_rank_closes_nothing(js):
    ids = await _world(
        2, _role("a", stages.FIRST_IMPRESSION), _role("b", stages.FIRST_IMPRESSION),
        _role("c", stages.FIRST_IMPRESSION), _role("d", stages.FIRST_IMPRESSION),
        _role("unranked", stages.FIRST_IMPRESSION), _role("hand", stages.FIRST_IMPRESSION),
        ranks={"a": 90, "b": 80, "c": 70, "d": 3, "hand": 99})
    async with db.session_factory()() as session:
        await trail.record(session, OWNER, event="role_moved", sentence="You moved it.", actor="person",
                           link=("opportunity", ids["hand"]))
        assert await _balance(session) == 2
        await session.commit()
    assert set((await _statuses()).values()) == {stages.FIRST_IMPRESSION}, "an unread role is never on deck"
    assert await _marked() == {"a", "b"}, "the best two estimates are read next; the rank of 3 is a place in line"
    async with db.session_factory()() as session:
        assert await _balance(session) == 0, "settled: the same two on the next run"


async def test_a_role_that_leaves_the_queue_is_replaced_by_the_next_role_by_estimate(js):
    await _world(1, _role("a", stages.FIRST_IMPRESSION), _role("b", stages.FIRST_IMPRESSION),
                 ranks={"a": 90, "b": 60})
    async with db.session_factory()() as session:
        await _balance(session)
        (await session.scalar(select(Opportunity).where(Opportunity.slug == "a"))).status = stages.GATED_OUT
        assert await _balance(session) >= 1
        await session.commit()
    assert await _marked() == {"b"}


async def test_a_new_high_rank_bumps_a_role_not_yet_read_out_of_the_reading_line(js):
    """The line is the current top N, not the first N once (the owner, 2026-10-21: 78, 72 and 70 waited while 66 and
    65 were read)."""
    ids = await _world(1, _role("old", stages.FIRST_IMPRESSION), _role("new", stages.FIRST_IMPRESSION),
                       ranks={"old": 66})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _marked() == {"old"}
    async with db.session_factory()() as session:
        await rank_roles(session, {ids["new"]: 78})
        await _balance(session)
        await session.commit()
    assert await _marked() == {"new"}, "its 78 takes the place; the 66 has not been read and gives way"
    async with db.session_factory()() as session:
        events = {e.event for e in await session.scalars(select(TrailEvent).where(TrailEvent.owner == OWNER))}
        assert "read_next" in events


async def test_a_role_a_session_is_reading_keeps_its_place_in_the_line_and_counts_toward_n(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 2})
        session.add_all([
            _role("taken", stages.FIRST_IMPRESSION, analysis_state=analysis.WAITING,
                  analysis_claimed_at=datetime.now(UTC)),
            _role("better", stages.FIRST_IMPRESSION), _role("best", stages.FIRST_IMPRESSION)])
        await session.flush()
        ids = {r.slug: r.id for r in await session.scalars(select(Opportunity))}
        await rank_roles(session, {ids["taken"]: 10, ids["better"]: 80, ids["best"]: 90})
        await session.commit()
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _marked() == {"best"}, "one place is the taken read's: only the best takes the other"
    assert set((await _statuses()).values()) == {stages.FIRST_IMPRESSION}, "being read, it stays in First Impression"


async def test_a_posting_over_thirty_days_old_by_its_found_date_sorts_below_a_fresher_role_within_twenty_points(js):
    """No posted date: the stale rule counts from the day the role was found."""
    await _world(1, _role("stale", stages.FIRST_IMPRESSION, posted=None, found=TODAY - timedelta(days=55)),
                 _role("fresh", stages.FIRST_IMPRESSION), ranks={"stale": 78, "fresh": 66})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _marked() == {"fresh"}, "78 less the stale penalty is under the fresher 66"


async def _sizes(**settings) -> tuple[int, int]:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], **settings})
        sizes = await pipeline.stage_sizes(session, OWNER)
        return sizes.deck, sizes.ready


async def test_each_stage_size_is_the_owners_else_fifteen(js):
    """Second Impression and Ready to Launch each have a size of their own (the owner, 2026-10-28)."""
    assert await _sizes() == (pipeline.DAILY_DEFAULT, pipeline.DAILY_DEFAULT) == (15, 15)
    assert await _sizes(second_impression_size=4, ready_to_launch_size=9) == (4, 9)
    assert await _sizes(second_impression_size=4) == (4, 15), "one set, the other stays at fifteen"
    assert await _sizes(second_impression_size=0, ready_to_launch_size=-3) == (15, 15), "no size is none"


async def test_the_one_daily_number_an_owner_had_is_both_sizes_until_they_change_one(js):
    """Nothing changes until the owner edits: the old `apply_per_day` sized both stages."""
    assert await _sizes(apply_per_day=4) == (4, 4)
    assert await _sizes(apply_per_day=4, ready_to_launch_size=2) == (4, 2), "the size they set wins for its stage"
    assert await _sizes(apply_per_day=0) == (15, 15)


async def _sized_world(deck: int, ready: int, *roles: Opportunity) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "second_impression_size": deck,
                                                     "ready_to_launch_size": ready})
        session.add_all(roles)
        await session.commit()


def _read_roles(count: int) -> list[Opportunity]:
    return [_role(f"s{n}", stages.FIRST_IMPRESSION, **counted(), **_fit(95 - n)) for n in range(count)]


async def test_ready_to_launch_and_second_impression_hold_the_size_each_was_given(js):
    """Ready takes the best 2, the deck the next 3, whatever the other holds."""
    await _sized_world(3, 2, *_read_roles(8))
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    held = await _statuses()
    assert sorted(s for s, st in held.items() if st == stages.READY_TO_LAUNCH) == ["s0", "s1"]
    assert sorted(s for s, st in held.items() if st == stages.SECOND_IMPRESSION) == ["s2", "s3", "s4"]
    assert sum(st == stages.FIRST_IMPRESSION for st in held.values()) == 3


async def test_a_deck_smaller_than_ready_and_a_larger_one_are_both_kept(js):
    await _sized_world(1, 4, *_read_roles(8))
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    held = list((await _statuses()).values())
    assert (held.count(stages.READY_TO_LAUNCH), held.count(stages.SECOND_IMPRESSION)) == (4, 1)


def _fit(percent: int) -> dict:
    return {"fit": percent, "fit_breakdown": {"math": 1, "percent": percent, "asks": []}}


async def test_the_best_n_read_roles_hold_ready_and_the_next_n_are_on_deck_the_rest_stay_in_first_impression(js):
    await _world(
        2, _role("r1", stages.FIRST_IMPRESSION, **counted(), **_fit(95)),
        _role("r2", stages.SECOND_IMPRESSION, **counted(), **_fit(90)),
        _role("d1", stages.FIRST_IMPRESSION, **counted(), **_fit(80)),
        _role("d2", stages.READY_TO_LAUNCH, **counted(), **_fit(70)),
        _role("low", stages.SECOND_IMPRESSION, **counted(), **_fit(40)), _role("unread", stages.FIRST_IMPRESSION),
        ranks={"unread": 99})
    async with db.session_factory()() as session:
        assert await _balance(session) > 0
        await session.commit()
    assert await _statuses() == {
        "r1": stages.READY_TO_LAUNCH, "r2": stages.READY_TO_LAUNCH, "d1": stages.SECOND_IMPRESSION,
        "d2": stages.SECOND_IMPRESSION, "low": stages.FIRST_IMPRESSION, "unread": stages.FIRST_IMPRESSION}, (
        "Ready: the best 2 by fit; on deck: the next 2; a read role below them goes back with its fit")
    async with db.session_factory()() as session:
        assert await _balance(session) == 0, "settled"
    assert await _marked() == {"unread"}, "its 99 beats the weakest fit on deck, so it is read next"


async def test_a_role_read_low_goes_back_to_first_impression_and_a_weaker_unread_estimate_is_not_read(js):
    await _world(
        1, _role("ready", stages.READY_TO_LAUNCH, **counted(), **_fit(90)),
        _role("deck", stages.SECOND_IMPRESSION, **counted(), **_fit(70)),
        _role("just-read", stages.SECOND_IMPRESSION, **counted(), **_fit(40)),
        _role("under", stages.FIRST_IMPRESSION), _role("over", stages.FIRST_IMPRESSION),
        ranks={"under": 50, "over": 85})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {
        "ready": stages.READY_TO_LAUNCH, "deck": stages.SECOND_IMPRESSION, "just-read": stages.FIRST_IMPRESSION,
        "under": stages.FIRST_IMPRESSION, "over": stages.FIRST_IMPRESSION}
    assert await _marked() == {"over"}, "the 50 does not beat the weakest on deck (70); the 85 does"
    assert "off_deck" in await _events("just-read")


async def test_the_next_read_role_is_promoted_into_ready_when_a_place_opens_and_the_deck_refills(js):
    await _world(
        1, _role("applied", stages.APPLIED), _role("s1", stages.SECOND_IMPRESSION, **counted(), **_fit(70)),
        _role("s2", stages.FIRST_IMPRESSION, **counted(), **_fit(60)), _role("u", stages.FIRST_IMPRESSION),
        ranks={"u": 20})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"applied": stages.APPLIED, "s1": stages.READY_TO_LAUNCH,
                                 "s2": stages.SECOND_IMPRESSION, "u": stages.FIRST_IMPRESSION}


async def test_a_newly_read_role_that_beats_the_weakest_ready_role_takes_its_place_and_that_one_drops_to_deck(js):
    await _world(
        2, _role("weak", stages.READY_TO_LAUNCH, **counted(), **_fit(50)),
        _role("strong", stages.READY_TO_LAUNCH, **counted(), **_fit(90)),
        _role("new", stages.SECOND_IMPRESSION, **counted(), **_fit(75)))
    async with db.session_factory()() as session:
        assert await _balance(session) == 2, "new goes up, weak comes down"
        await session.commit()
    assert await _statuses() == {"weak": stages.SECOND_IMPRESSION, "strong": stages.READY_TO_LAUNCH,
                                 "new": stages.READY_TO_LAUNCH}
    assert "on_deck" in await _events("weak")


async def test_a_read_roles_sonnet_fit_is_its_score_and_outranks_a_higher_rank(js):
    await _world(
        1, _role("high-rank", stages.SECOND_IMPRESSION, **counted()),
        _role("high-fit", stages.SECOND_IMPRESSION, **counted(), **_fit(95)), ranks={"high-rank": 90, "high-fit": 40})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"high-fit": stages.READY_TO_LAUNCH, "high-rank": stages.SECOND_IMPRESSION}, (
        "the read's 95 beats the rank of 90; the other read role is on deck")


async def test_a_role_go_opened_keeps_its_place_in_ready_and_counts_toward_n(js):
    await _world(
        2, _role("opened", stages.READY_TO_LAUNCH, go_opened_at=datetime.now(UTC), **counted()),
        _role("a", stages.SECOND_IMPRESSION, **counted(), **_fit(90)),
        _role("b", stages.SECOND_IMPRESSION, **counted(), **_fit(80)), ranks={"opened": 1})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"opened": stages.READY_TO_LAUNCH, "a": stages.READY_TO_LAUNCH,
                                 "b": stages.SECOND_IMPRESSION}, "the opened role is not pushed out by better ones"


async def test_roles_go_opened_hold_every_ready_seat_until_one_is_applied_then_the_best_on_deck_moves_up(js):
    """PRD 24.20: N opened roles that are not Applied fill the N seats, so no unopened role moves up; marking one
    Applied (the owner's move, the same path the mail sync's confirmation takes) frees one seat for the best on deck."""
    opened = {"go_opened_at": datetime.now(UTC)}
    ids = await _world(
        2, _role("o1", stages.READY_TO_LAUNCH, **opened, **counted()),
        _role("o2", stages.READY_TO_LAUNCH, **opened, **counted()),
        _role("a", stages.SECOND_IMPRESSION, **counted(), **_fit(90)),
        _role("b", stages.SECOND_IMPRESSION, **counted(), **_fit(80)), ranks={"o1": 1, "o2": 1})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"o1": stages.READY_TO_LAUNCH, "o2": stages.READY_TO_LAUNCH,
                                 "a": stages.SECOND_IMPRESSION, "b": stages.SECOND_IMPRESSION}, (
        "opened roles fill both seats, so nothing moves up")
    async with db.session_factory()() as session:
        await board.move(session, OWNER, ids["o1"], stages.APPLIED)
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"o1": stages.APPLIED, "o2": stages.READY_TO_LAUNCH,
                                 "a": stages.READY_TO_LAUNCH, "b": stages.SECOND_IMPRESSION}, (
        "one seat freed: the best role on deck moves up, the next waits")


async def test_a_role_at_an_employer_applied_to_within_the_gap_takes_no_deck_or_ready_place(js):
    gap = {**PROFILE["settings"], "apply_per_day": 3, "days_between_employer_applications": 14}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings=gap)
        session.add_all([
            _role("done", stages.APPLIED, company="Acme"),
            _role("ready", stages.READY_TO_LAUNCH, company="Acme", **counted(), **_fit(95)),
            _role("deck", stages.SECOND_IMPRESSION, company="Acme", **counted(), **_fit(90)),
            _role("unread", stages.FIRST_IMPRESSION, company="Acme"),
            _role("other", stages.SECOND_IMPRESSION, **counted(), **_fit(50))])
        await session.flush()
        ids = {r.slug: r.id for r in await session.scalars(select(Opportunity))}
        await rank_roles(session, {ids["unread"]: 99})
        await session.commit()
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {
        "done": stages.APPLIED, "ready": stages.FIRST_IMPRESSION, "deck": stages.FIRST_IMPRESSION,
        "unread": stages.FIRST_IMPRESSION, "other": stages.READY_TO_LAUNCH}
    assert await _marked() == set(), "an unread role at that employer is not read either"


async def test_reads_waiting_in_second_impression_fill_free_ready_places_and_beat_weaker_ready_roles(js, monkeypatch):
    """A state seen in use: Ready held six read roles with low fits while four read roles with higher fits,
    marked to be read again, sat in Second Impression with places free."""
    from app.jobsearch.research import staleness

    async def reasons(session, role, now=None):
        return ["the master changed"] if role.slug.startswith("d") else []

    monkeypatch.setattr(staleness, "analysis_reasons", reasons)
    ready = {f"r{i}": f for i, f in enumerate((59, 75, 35, 38, 15, 4))}
    deck = {f"d{i}": f for i, f in enumerate((63, 58, 57, 58))}
    await _world(
        15, *(_role(s, stages.READY_TO_LAUNCH, **counted(), **_fit(f)) for s, f in ready.items()),
        *(_role(s, stages.SECOND_IMPRESSION, **counted(**{analysis.REANALYSE: True}), **_fit(f))
          for s, f in deck.items()))
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert set((await _statuses()).values()) == {stages.READY_TO_LAUNCH}, "ten read roles, fifteen places"


async def test_a_role_with_an_older_full_analysis_in_the_reading_line_is_marked_and_takes_no_idle_slot(js):
    await _world(
        2, _role("old1", stages.FIRST_IMPRESSION, **_OLD), _role("old2", stages.FIRST_IMPRESSION, **_OLD),
        _role("fresh", stages.FIRST_IMPRESSION), ranks={"old1": 90, "old2": 80, "fresh": 70})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    async with db.session_factory()() as session:
        picked = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER, line.startable())
                                        )).all()
    assert sorted(r.slug for r in picked) == ["old1", "old2"], "both queue places are ones the analyst can start"


async def test_research_made_under_an_older_master_does_not_count_and_is_read_again(js, monkeypatch):
    from app.jobsearch.research import staleness

    async def reasons(session, role, now=None):
        return ["the master changed"] if role.slug == "old" else []

    monkeypatch.setattr(staleness, "analysis_reasons", reasons)
    await _world(2, _role("old", stages.SECOND_IMPRESSION, sourced_at=datetime.now(UTC), **counted(), **_fit(99)),
                 _role("new", stages.SECOND_IMPRESSION, **counted(), **_fit(50)))
    async with db.session_factory()() as session:
        assert await _balance(session) == 2, "a read made under older inputs still stands on its fit"
        assert await pipeline.mark_for_reads(session, OWNER) == 1
        await session.commit()
    # Replaced behaviour (1.137.1): only the current read used to go up; both go up, the old one is read again.
    assert await _statuses() == {"old": stages.READY_TO_LAUNCH, "new": stages.READY_TO_LAUNCH}
    async with db.session_factory()() as session:
        picked = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER, line.startable())
                                        )).all()
    assert [r.slug for r in picked] == ["old"], "the pool starts Sonnet on it again"


# --- research without the stamp does not count ---------------------------------------------------------------------

#: The old full analysis of a role, kept through migration 0104: done, but never stamped as a Second Impression.
_OLD = {"analysis_state": analysis.DONE, "analysis": {}}


async def test_a_deck_role_with_old_research_is_marked_to_be_read_again_and_never_goes_up(js):
    await _world(2, _role("old", stages.SECOND_IMPRESSION, sourced_at=datetime.now(UTC), **_OLD),
                 ranks={"old": 90})
    async with db.session_factory()() as session:
        assert await pipeline.mark_for_reads(session, OWNER) == 1
        await session.commit()
    assert await _statuses() == {"old": stages.SECOND_IMPRESSION}
    assert "read_again" in await _events("old")
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.slug == "old"))
        assert role.analysis[analysis.REANALYSE] is True
        picked = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER, line.startable())
                                        )).all()
        assert [r.slug for r in picked] == ["old"]
        assert await pipeline.mark_for_reads(session, OWNER) == 0, "marked once"


async def test_a_ready_role_with_old_research_keeps_its_place_is_read_again_and_its_packet_rows_are_untouched(js):
    ids = await _world(2, _role("old", stages.READY_TO_LAUNCH, **_OLD), ranks={"old": 90})
    async with db.session_factory()() as session:
        session.add(Packet(owner=OWNER, role_id=ids["old"], version=1, status="ready", brief={}, resume={}, letter={}))
        await session.commit()
    async with db.session_factory()() as session:
        assert await pipeline.mark_for_reads(session, OWNER) == 1
        await session.commit()
    # Replaced behaviour (1.137.1, the owner): it used to go back on deck until its new read; it keeps its place.
    assert await _statuses() == {"old": stages.READY_TO_LAUNCH}
    async with db.session_factory()() as session:
        packets = (await session.scalars(select(Packet).where(Packet.role_id == ids["old"]))).all()
        assert [(p.version, p.status) for p in packets] == [(1, "ready")]


async def test_old_research_is_left_alone_when_go_opened_the_role_or_the_owner_moved_it_by_hand(js):
    ids = await _world(
        5, _role("opened", stages.READY_TO_LAUNCH, go_opened_at=datetime.now(UTC), **_OLD),
        _role("hand", stages.READY_TO_LAUNCH, **_OLD), _role("hand-deck", stages.SECOND_IMPRESSION, **_OLD),
        ranks={"opened": 90, "hand": 80, "hand-deck": 70})
    async with db.session_factory()() as session:
        for slug in ("hand", "hand-deck"):
            await trail.record(session, OWNER, event="role_moved", sentence="You moved it.", actor="person",
                               link=("opportunity", ids[slug]))
        await session.commit()
    async with db.session_factory()() as session:
        assert await pipeline.mark_for_reads(session, OWNER) == 0
        await session.commit()
    assert await _statuses() == {"opened": stages.READY_TO_LAUNCH, "hand": stages.READY_TO_LAUNCH,
                                 "hand-deck": stages.SECOND_IMPRESSION}
    async with db.session_factory()() as session:
        rows = await session.scalars(select(Opportunity).execution_options(populate_existing=True))
        assert all(analysis.REANALYSE not in r.analysis for r in rows)


# --- the analyst's line --------------------------------------------------------------------------------------------


async def test_a_waiting_analysis_that_is_not_on_deck_and_not_the_owners_ask_is_taken_out_of_the_line(js):
    waiting = {"analysis_state": analysis.WAITING, "analysis_asked_at": datetime.now(UTC)}
    asked = {"analysis": {analysis.ASKED_BY: analysis.ASKED_BY_OWNER}}
    ids = await _world(
        5, _role("first", stages.FIRST_IMPRESSION, **waiting), _role("deck", stages.SECOND_IMPRESSION, **waiting),
        _role("mine", stages.LANDING_ZONE, **waiting, **asked),
        _role("taken", stages.FIRST_IMPRESSION, **waiting, analysis_claimed_at=datetime.now(UTC)),
        _role("closed", stages.GATED_OUT, **waiting))
    async with db.session_factory()() as session:
        assert await pipeline.stop_waiting(session, OWNER) == 1
        await session.commit()
    async with db.session_factory()() as session:
        states = {r.slug: r.analysis_state for r in await session.scalars(
            select(Opportunity).execution_options(populate_existing=True))}
    assert states == {"first": None, "deck": analysis.WAITING, "mine": analysis.WAITING,
                      "taken": analysis.WAITING, "closed": analysis.WAITING}, (
        "only the one off the deck that nobody asked for and no session has is taken out")
    assert ids["first"], "the role stays where it is"


async def test_a_researched_role_sent_back_for_new_research_stays_in_the_analysts_line_off_the_deck(js):
    """85 researched roles were queued again because an input changed; the 71 in First Impression
    were taken out of the line by the next pipeline pass, lost their done state and were never researched again."""
    waiting = {"analysis_state": analysis.WAITING, "analysis_asked_at": datetime.now(UTC)}
    await _world(
        5, _role("again", stages.FIRST_IMPRESSION, **waiting, analysed_at=datetime.now(UTC) - timedelta(days=5)),
        _role("never", stages.FIRST_IMPRESSION, **waiting))
    async with db.session_factory()() as session:
        assert await pipeline.stop_waiting(session, OWNER) == 1
        await session.commit()
    async with db.session_factory()() as session:
        states = {r.slug: r.analysis_state for r in await session.scalars(
            select(Opportunity).execution_options(populate_existing=True))}
    assert states == {"again": analysis.WAITING, "never": None}


# --- the order -----------------------------------------------------------------------------------------------------


def _aged(slug: str, days: int) -> Opportunity:
    return Opportunity(id=uuid.uuid4(), owner=OWNER, slug=slug, status=stages.FIRST_IMPRESSION, origin="scout",
                       posted=TODAY - timedelta(days=days), found=TODAY, created_at=datetime.now(UTC))


def test_a_posting_over_thirty_days_old_drops_below_a_fresher_one_of_a_similar_rank():
    now = datetime.now(UTC)
    old, fresh = _aged("old", pipeline.STALE_DAYS + 1), _aged("fresh", 5)
    rank = {old.id: 80, fresh.id: 70}
    assert [r.slug for r in pipeline.order([old, fresh], rank, TODAY, now)] == ["fresh", "old"], "80 less the penalty"
    assert pipeline.points(old, 80, None, False, TODAY, now) == 80 - pipeline.STALE_PENALTY
    edge = _aged("edge", pipeline.STALE_DAYS)  # 30 days old is not over thirty
    assert pipeline.points(edge, 80, None, False, TODAY, now) == 80
    equal = {old.id: 75, fresh.id: 75}
    assert [r.slug for r in pipeline.order([old, fresh], equal, TODAY, now)] == ["fresh", "old"]
    far_better = {old.id: 99, fresh.id: 70}
    assert [r.slug for r in pipeline.order([fresh, old], far_better, TODAY, now)] == ["old", "fresh"], (
        "the penalty moves a stale role below a similar score only")


def test_the_stale_penalty_is_21_so_a_stale_role_sorts_below_every_fresher_role_within_20_points():
    now = datetime.now(UTC)
    old, within, beyond = _aged("old", pipeline.STALE_DAYS + 1), _aged("within", 5), _aged("beyond", 5)
    assert pipeline.STALE_PENALTY == 21
    rank = {old.id: 90, within.id: 70, beyond.id: 68}  # 20 and 22 points under the stale role
    assert [r.slug for r in pipeline.order([old, beyond, within], rank, TODAY, now)] == ["within", "old", "beyond"], (
        "70 beats 90 less 21 (69); 68 does not")


def test_the_queue_rules_are_the_owners_settings_and_default_to_the_old_numbers():
    assert pipeline.queue_rules_of(Settings()) == pipeline.QueueRules(
        stale_days=30, stale_penalty=21, golden_hours=72, golden_bonus=20, research_start_multiple=3)
    assert pipeline.QueueRules() == pipeline.queue_rules_of(Settings()), "the constants are the defaults"
    own = Settings(stale_days=10, stale_penalty=5, golden_hours=24, golden_bonus=7, research_start_multiple=1)
    assert pipeline.queue_rules_of(own) == pipeline.QueueRules(10, 5, 24, 7, 1)


def test_the_owners_stale_days_and_penalty_decide_what_is_stale_and_what_it_loses():
    now = datetime.now(UTC)
    ten, thirty = _aged("ten", 11), _aged("thirty", 31)
    rules = pipeline.QueueRules(stale_days=10, stale_penalty=5)
    assert pipeline.is_stale(ten, TODAY, rules) and not pipeline.is_stale(_aged("edge", 10), TODAY, rules)
    assert not pipeline.is_stale(ten, TODAY), "eleven days is not stale by the default thirty"
    assert pipeline.points(ten, 80, None, False, TODAY, now, rules=rules) == 75
    assert pipeline.points(thirty, 80, None, False, TODAY, now, rules=rules) == 75, "the penalty is the owner's too"
    assert pipeline.points(ten, 80, None, False, TODAY, now) == 80
    rank = {ten.id: 74, thirty.id: 77}  # 69 and 72 under the owner's rule; 74 and 56 under the default
    assert [r.slug for r in pipeline.order([ten, thirty], rank, TODAY, now, rules=rules)] == ["thirty", "ten"]
    assert [r.slug for r in pipeline.order([thirty, ten], rank, TODAY, now)] == ["ten", "thirty"]


def test_the_owners_golden_hours_and_bonus_decide_the_first_hours_boost():
    now = datetime.now(UTC)
    today, yesterday = _aged("today", 0), _aged("yesterday", 1)
    rules = pipeline.QueueRules(golden_hours=24, golden_bonus=7)
    assert pipeline.points(today, 50, None, False, TODAY, now, rules=rules) == 57
    assert pipeline.points(yesterday, 50, None, False, TODAY, now, rules=rules) == 50, "24 hours old is not under 24"
    assert pipeline.points(yesterday, 50, None, False, TODAY, now) == 70, "the default: 72 hours, 20 points"
    off = pipeline.QueueRules(golden_hours=0)
    assert pipeline.points(today, 50, None, False, TODAY, now, rules=off) == 50, "no hours: no boost"


def test_the_number_a_card_shows_is_the_number_the_list_sorts_by():
    """The owner, 2026-11-06 (docs/PRD.md 24.15): the card shows the order's number with the first hours' bonus in it
    (from 2026-11-03 it showed the score alone), so a new role scored 70 shows 90. Old (the first build): the card kept
    it to 100 while the list sorted by 120. New: the card shows the number the list sorts by, 120."""
    now = datetime.now(UTC)
    fresh, old = _aged("fresh", 0), _aged("old", pipeline.STALE_DAYS + 1)
    assert pipeline.points(fresh, 80, None, False, TODAY, now) == 100
    assert pipeline.shown(fresh, 70, None, False, TODAY, now) == 90, "a new role scored 70 shows 90"
    assert pipeline.shown(old, 80, None, False, TODAY, now) == 80 - pipeline.STALE_PENALTY, "the stale penalty shows"
    assert pipeline.shown(old, 10, None, False, TODAY, now) == 0, "never below 0"
    assert pipeline.shown(fresh, 100, None, False, TODAY, now) == 120 == pipeline.points(
        fresh, 100, None, False, TODAY, now), "over 100, the card says what the order sorts by"
    assert pipeline.shown(fresh, None, None, False, TODAY, now) is None, "no score, no number"
    assert pipeline.shown(fresh, 0, None, False, TODAY, now) == 0, "a gate zero's 0 is not lifted by the bonus"
    off = pipeline.QueueRules(golden_bonus=0)
    assert pipeline.shown(fresh, 80, None, False, TODAY, now, rules=off) == 80, "no bonus: the score"
    own = pipeline.QueueRules(golden_bonus=35, stale_days=10, stale_penalty=5)
    assert pipeline.shown(fresh, 40, None, False, TODAY, now, rules=own) == 75, "the owner's bonus"
    assert pipeline.shown(_aged("ten", 11), 40, None, False, TODAY, now, rules=own) == 35, "and the owner's penalty"


def test_the_list_is_sorted_by_the_number_the_card_shows():
    now = datetime.now(UTC)
    roles = [_aged("plain60", 5), _aged("new70", 0), _aged("stale95", pipeline.STALE_DAYS + 1), _aged("old85", 5),
             _aged("new100", 0)]
    rank = {r.id: n for r, n in zip(roles, (60, 70, 95, 85, 100), strict=True)}
    listed = pipeline.order(roles, rank, TODAY, now)
    assert [r.slug for r in listed] == ["new100", "new70", "old85", "stale95", "plain60"]
    assert [pipeline.shown(r, rank[r.id], None, False, TODAY, now) for r in listed] == [120, 90, 85, 74, 60]


def test_a_read_role_whose_score_is_lower_than_an_unread_roles_rank_sorts_below_it():
    """A newer read replaces the Haiku rank even when it is lower (docs/PRD.md 24.15): the role drops."""
    now = datetime.now(UTC)
    read, unread = _aged("read", 5), _aged("unread", 5)
    read.fit, read.fit_breakdown = 40, {"math": 1, "percent": 40, "asks": []}
    rank = {read.id: 90, unread.id: 60}  # Haiku had put the role at 90 before Sonnet read it
    assert pipeline.points(read, 90, None, True, TODAY, now) == 40
    assert [r.slug for r in pipeline.order([read, unread], rank, TODAY, now, None, None, {read.id})] == [
        "unread", "read"]


def test_a_role_with_no_score_is_0_in_the_order_and_a_read_with_no_score_and_no_rank_has_none():
    now = datetime.now(UTC)
    new = _aged("new", 0)
    assert pipeline.points(new, None, None, False, TODAY, now) == 0, "0 until it is ranked, though it is new"
    assert not pipeline.has_score(new, None, None, False)
    assert pipeline.has_score(new, 0, None, False), "a rank of 0 is a score"
    assert not pipeline.has_score(_aged("read", 5), None, None, True), "read, and no queue score and no rank"
    assert pipeline.has_score(_aged("read2", 5), 30, None, True), "a read with no queue score falls back to its rank"


async def test_a_read_role_with_no_score_and_no_rank_takes_no_seat_while_the_deck_has_room(js):
    await _world(None, _role("nothing", stages.FIRST_IMPRESSION, **counted()), second_impression_size=3,
                 ready_to_launch_size=2)
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"nothing": stages.FIRST_IMPRESSION}


async def test_an_unread_roles_number_is_its_newest_haiku_rank_as_written_not_a_mapped_figure(js):
    """With read roles enough that the old rank map would have been fitted (25 pairs of rank 70 and fit 3), a rank of 70
    stays 70 (docs/PRD.md 24.15)."""
    pairs = [_read_at(f"pair{i}", stages.FIRST_IMPRESSION, 3, days=10) for i in range(25)]
    ids = await _world(None, *pairs, _role("unread", stages.FIRST_IMPRESSION), _role("low", stages.FIRST_IMPRESSION),
                       ranks={**{p.slug: 70 for p in pairs}, "unread": 70, "low": 12})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    async with db.session_factory()() as session:
        got = await pipeline.ranks(session, OWNER)
    assert (got[ids["unread"]], got[ids["low"]]) == (70, 12), "the rank as the ranker wrote it"


async def test_ready_and_second_impression_hold_read_roles_only_though_unread_roles_top_the_list(js):
    """Two unread roles at 95 and 90 are the top of the list; the read roles at 50, 40 and 30 hold the places, and the
    best unread role is picked for its read."""
    await _world(None, _role("u1", stages.FIRST_IMPRESSION), _role("u2", stages.FIRST_IMPRESSION),
                 _read_at("r1", stages.FIRST_IMPRESSION, 50), _read_at("r2", stages.FIRST_IMPRESSION, 40),
                 _read_at("r3", stages.FIRST_IMPRESSION, 30), second_impression_size=1, ready_to_launch_size=2,
                 ranks={"u1": 95, "u2": 90})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"u1": stages.FIRST_IMPRESSION, "u2": stages.FIRST_IMPRESSION,
                                 "r1": stages.READY_TO_LAUNCH, "r2": stages.READY_TO_LAUNCH,
                                 "r3": stages.SECOND_IMPRESSION}
    assert await _marked() == {"u1"}, "the best unread role beats the weakest on deck, and one read starts at a time"


async def _first_impression_with_two_ranked_and_one_unranked(**settings) -> set[str]:
    """First Impression holds 2 ranked roles and a third waits for its rank, the deck holds 1: what is marked to be
    read after one run."""
    await _world(1, _role("a", stages.FIRST_IMPRESSION), _role("b", stages.FIRST_IMPRESSION),
                 _role("unranked", stages.CLEANSED_DATA), ranks={"a": 80, "b": 70}, **settings)
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    return await _marked()


async def test_new_reads_wait_until_first_impression_holds_three_times_the_deck_by_default(js):
    assert await _first_impression_with_two_ranked_and_one_unranked() == set(), "2 is under 3 times 1"


async def test_the_owners_multiple_decides_when_new_reads_stop_waiting(js):
    assert await _first_impression_with_two_ranked_and_one_unranked(research_start_multiple=2) == {"a"}


async def test_a_multiple_of_zero_never_holds_new_reads_back(js):
    assert await _first_impression_with_two_ranked_and_one_unranked(research_start_multiple=0) == {"a"}


# --- Ready to Launch holds the top read roles, whatever they score (the owner, 2026-11-06; docs/PRD.md 24.15) --------
# The owner's lowest score for Ready to Launch (2026-11-03, 24.12) is gone: Ready is the top M read roles by the one
# number, so a weak read role takes a free place and a stored settings version that holds the minimum still loads.

_FIRST, _SECOND, _READY = stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH


def _read_at(slug: str, status: str, score: int, days: int = 5, **more) -> Opportunity:
    """A read role whose card says `score`, posted `days` days ago (a posting under 3 days old is in its first hours,
    where the order and the card add 20 points)."""
    return _role(slug, status, posted=TODAY - timedelta(days=days), **counted(), **_fit(score), **more)


def test_there_is_no_lowest_score_for_ready_to_launch_in_the_queue_rules_and_a_stored_one_is_read_past():
    assert "ready_minimum" not in pipeline.QueueRules._fields
    assert "ready_minimum" not in Settings.model_fields
    stored = Settings.model_validate({"ready_minimum": 65, "stale_days": 12})
    assert pipeline.queue_rules_of(stored) == pipeline.QueueRules(stale_days=12)
    assert "ready_minimum" not in stored.model_dump(), "the next save leaves it out"


async def test_a_weak_read_role_takes_a_free_ready_place_whatever_it_scores(js):
    await _world(None, _read_at("weak", _FIRST, 20), _read_at("weaker", _FIRST, 10),
                 second_impression_size=3, ready_to_launch_size=2)
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"weak": _READY, "weaker": _READY}


async def test_a_stored_settings_version_that_holds_the_ready_minimum_loads_and_the_queue_reads_past_it(js):
    from app.jobsearch.models import ProfileVersion
    from app.jobsearch.profile import store as profile_store

    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner=OWNER, kind="settings", version=1, saved_by="test", why="before 2026-11-06",
                                   data={"ready_minimum": 90, "ready_to_launch_size": 2, "second_impression_size": 3}))
        session.add_all([_read_at("weak", _FIRST, 20), _read_at("weaker", _FIRST, 10)])
        await session.commit()
    async with db.session_factory()() as session:
        settings = (await profile_store.current(session, OWNER)).settings
        assert settings.ready_to_launch_size == 2 and not hasattr(settings, "ready_minimum")
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"weak": _READY, "weaker": _READY}, "a minimum of 90 no longer keeps them out"


def test_a_score_is_the_fit_of_a_read_role_else_its_rank_else_none():
    role = _aged("scored", 5)
    role.fit, role.fit_breakdown = 80, {"math": 1, "percent": 80, "asks": []}
    assert pipeline.score(role, 55, None, True) == (80, "fit"), "a role with a Second Impression shows its fit"
    assert pipeline.score(role, 55, None, False) == (55, "rank"), "one without shows the Haiku rank"
    assert pipeline.score(role, None, None, False) == (None, None)
    unscored = _aged("unscored", 5)
    assert pipeline.score(unscored, 55, None, True) == (55, "rank"), "a read with no fit falls back to the rank"


def test_the_order_sorts_by_the_fit_of_a_read_role_and_the_rank_of_the_others():
    now = datetime.now(UTC)
    read, other = _aged("read", 5), _aged("other", 5)
    read.fit, read.fit_breakdown = 95, {"math": 1, "percent": 95, "asks": []}
    rank = {read.id: 40, other.id: 90}
    assert [r.slug for r in pipeline.order([other, read], rank, TODAY, now, None, None, {read.id})] == [
        "read", "other"]
    assert [r.slug for r in pipeline.order([other, read], rank, TODAY, now)] == ["other", "read"], "unread: the rank"


def test_a_role_with_no_rank_comes_last():
    now = datetime.now(UTC)
    ranked, unranked = _aged("ranked", 5), _aged("unranked", 0)
    assert [r.slug for r in pipeline.order([unranked, ranked], {ranked.id: 1}, TODAY, now)] == ["ranked", "unranked"]


# --- the hard gates at Cleansed Data -------------------------------------------------------------------------------


async def _with_copy(session, role: Opportunity, **copy) -> None:
    """The role's posting, read and cleansed: the raw row, the posting of the role, and its current cleansed copy."""
    employer = await find_or_make(session, OWNER, role.company)
    raw = RawPosting(owner=OWNER, source_id=employer.id, reader="legacy", kind="legacy", ext_id=role.slug,
                     fetched_at=datetime.now(UTC), body=b"x", body_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                     body_bytes=1, no_raw=True)
    session.add(raw)
    await session.flush()
    session.add(JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=role.slug,
                           company=role.company, title=role.title, match_key=role.match_key,
                           first_seen=datetime.now(UTC), last_seen=datetime.now(UTC), links=[],
                           opportunity_id=role.id))
    session.add(CleansedPosting(owner=OWNER, raw_id=raw.id, cleaner_version=1, title=role.title, employer=role.company,
                                locations=[], text_clean="The posting.", **copy))
    await session.flush()
    await raw_copies.repoint(session, OWNER)  # the pointer the code that writes copies keeps (`raw_copies._point`)


async def test_the_gates_at_cleansed_data_load_only_the_copies_of_the_roles_there(js, monkeypatch):
    """Every turn loaded every role's whole cleansed text to gate a handful."""
    from app.jobsearch.postings import cleansed

    asked: list[set] = []
    real = cleansed.copies

    async def copies(session, owner, ids):
        asked.append(set(ids))
        return await real(session, owner, ids)

    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"compensation": {"amount": 95_000, "against": "maximum"}})
        low, ranked = _role("low", stages.CLEANSED_DATA, comp=None), _role("ranked", stages.FIRST_IMPRESSION)
        session.add_all([low, ranked])
        await session.flush()
        await _with_copy(session, low, pay_min=60_000, pay_max=70_000)
        await _with_copy(session, ranked, pay_min=60_000, pay_max=70_000)
        monkeypatch.setattr(cleansed, "copies", copies)
        assert await pipeline.gate(session, OWNER) == 1
        wanted = (await cleansed.current_for_roles(session, OWNER))[low.id]
    assert asked == [{wanted}], "the one role at Cleansed Data, not the First Impression role's copy"


async def test_the_gates_at_cleansed_data_choose_the_copy_of_the_roles_there_and_no_other(js, monkeypatch):
    """The gate chose the copy to read for every role that has one, to gate the few at Cleansed Data."""
    from app.jobsearch.postings import cleansed

    chosen_for: list[set] = []
    real = cleansed.gate_copy_ids_for

    async def chosen(session, owner, ids):
        chosen_for.append(set(ids))
        return await real(session, owner, ids)

    async def everyones(*_args, **_kwargs):
        raise AssertionError("the copy of every role was chosen")

    monkeypatch.setattr(cleansed, "gate_current_for_roles", everyones)
    monkeypatch.setattr(cleansed, "gate_copy_ids_for", chosen)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"compensation": {"amount": 95_000, "against": "maximum"}})
        low, ranked = _role("low", stages.CLEANSED_DATA, comp=None), _role("ranked", stages.FIRST_IMPRESSION)
        session.add_all([low, ranked])
        await session.flush()
        await _with_copy(session, low, pay_min=60_000, pay_max=70_000)
        await _with_copy(session, ranked, pay_min=60_000, pay_max=70_000)
        assert await pipeline.gate(session, OWNER) == 1
        await session.commit()
    assert chosen_for == [{low.id}], "the one role at Cleansed Data"
    assert await _statuses() == {"low": stages.GATED_OUT, "ranked": stages.FIRST_IMPRESSION}


async def test_a_role_at_cleansed_data_that_fails_a_hard_gate_on_its_copy_closes_gated_out_and_unknown_passes(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"compensation": {"amount": 95_000, "against": "maximum"}})
        roles = {s: _role(s, stages.CLEANSED_DATA, comp=None, posted=date.today()) for s in ("low", "high", "unknown")}
        hand = _role("hand", stages.CLEANSED_DATA)
        session.add_all([*roles.values(), hand])
        await session.flush()
        await _with_copy(session, roles["low"], pay_min=60_000, pay_max=70_000)
        await _with_copy(session, roles["high"], pay_min=120_000, pay_max=150_000)
        await _with_copy(session, roles["unknown"])
        await _with_copy(session, hand, pay_min=60_000, pay_max=70_000)
        await trail.record(session, OWNER, event="role_moved", sentence="You moved it.", actor="person",
                           link=("opportunity", hand.id))
        assert await pipeline.gate(session, OWNER) == 1
        await session.commit()
    async with db.session_factory()() as session:
        rows = {r.slug: r for r in await session.scalars(select(Opportunity).execution_options(
            populate_existing=True))}
    assert rows["low"].status == stages.GATED_OUT
    assert rows["low"].screened_out_reason.startswith("Pay must be at least $95,000 a year")
    assert (rows["low"].screened_out_by, rows["low"].screened_out_step) == ("compensation", "gate")
    assert rows["high"].status == stages.CLEANSED_DATA, "a pay over the floor passes"
    assert rows["unknown"].status == stages.CLEANSED_DATA, "a fact the posting does not state passes"
    assert rows["hand"].status == stages.CLEANSED_DATA, "a role the owner moved by hand stays"


# --- who the analyst reads -----------------------------------------------------------------------------------------


async def test_the_analyst_starts_only_roles_on_deck(js):
    await _world(
        5, *(_role(s, st) for s, st in (
            ("landing", stages.LANDING_ZONE), ("cleansed", stages.CLEANSED_DATA), ("first", stages.FIRST_IMPRESSION),
            ("deck", stages.SECOND_IMPRESSION), ("ready", stages.READY_TO_LAUNCH), ("applied", stages.APPLIED))),
        _role("not-sourced", stages.SECOND_IMPRESSION, sourced_at=None))
    async with db.session_factory()() as session:
        picked = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER, line.startable())
                                        )).all()
    assert [r.slug for r in picked] == ["deck"], "second impression only, and only once the posting is sourced"


async def test_a_hand_moved_role_code_has_moved_on_since_competes_for_the_deck_like_any_other(js):
    """The owner, 2026-10-23: once code has moved a hand-moved role on (the hand move spent), it is read next
    and goes on deck by its score like any other role. A role still where the owner put it stays pinned."""
    ids = await _world(5, _role("moved", stages.FOLLOWUP_NEEDED), _role("plain", stages.FOLLOWUP_NEEDED),
                       _role("placed", stages.FIRST_IMPRESSION), ranks={"moved": 90, "plain": 80, "placed": 95})
    async with db.session_factory()() as session:
        for slug in ("moved", "placed"):
            await trail.record(session, OWNER, event="role_moved", actor="person", who="Owner",
                               link=("opportunity", ids[slug]), sentence="You moved it.")
        await session.commit()
    async with db.session_factory()() as session:
        await pipeline.run(session, OWNER)
        await session.commit()
    assert await _statuses() == {"moved": stages.FIRST_IMPRESSION, "plain": stages.FIRST_IMPRESSION,
                                 "placed": stages.FIRST_IMPRESSION}
    assert await _marked() == {"moved", "plain"}, "the spent hand move competes; the placed role is never marked"
    async with db.session_factory()() as session:
        for role in await session.scalars(select(Opportunity).where(Opportunity.slug.in_(("moved", "plain")))):
            role.analysis_state, role.fit = analysis.DONE, 4  # read since, both
            role.analysis = {"deck_read": True}
        await session.commit()
    async with db.session_factory()() as session:
        await pipeline.rebalance(session, OWNER)
        await session.commit()
    statuses = await _statuses()
    assert statuses["moved"] in (stages.READY_TO_LAUNCH, stages.SECOND_IMPRESSION), "read, it takes a place by fit"
    assert statuses["placed"] == stages.FIRST_IMPRESSION, "still where the owner put it"


async def test_a_hand_move_that_code_has_moved_on_from_no_longer_pins_the_role(js):
    """Roles the owner moved out of Gated out were dropped to Followup needed by
    the statuses migration, flowed on to Cleansed Data and stayed there with low ranks: any hand move ever
    made pinned them. A role stays where the owner put it, not where code took it afterwards."""
    ids = await _world(5, _role("spent", stages.FOLLOWUP_NEEDED), _role("placed", stages.FOLLOWUP_NEEDED),
                       ranks={"spent": 90, "placed": 80})

    async def hand(slug: str) -> None:
        async with db.session_factory()() as session:
            await trail.record(session, OWNER, event="role_moved", actor="person", who="Owner",
                               link=("opportunity", ids[slug]), sentence="You moved it.")
            await session.commit()

    await hand("spent")  # the owner's move, before code moved the role on
    async with db.session_factory()() as session:
        for role in await session.scalars(select(Opportunity)):
            role.status = stages.CLEANSED_DATA  # a migration, the stages since
        await session.commit()
    await hand("placed")  # the owner's move, after: they put it in Cleansed Data
    async with db.session_factory()() as session:
        await pipeline.advance(session, OWNER)
        await session.commit()
    assert await _statuses() == {"spent": stages.FIRST_IMPRESSION, "placed": stages.CLEANSED_DATA}


async def test_the_roles_move_on_by_code_at_any_hour_and_only_model_work_waits_for_the_off_hours(js, monkeypatch):
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)  # by day
    await _world(5, _role("one", stages.FOLLOWUP_NEEDED), _role("two", stages.FOLLOWUP_NEEDED),
                 ranks={"one": 90, "two": 80})
    assert await schedule.pipeline_pass(OWNER) is True, "the pipeline's own turn, at any hour"
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) is None, "no model work is started by day"
        await session.commit()
    assert await _statuses() == {"one": stages.FIRST_IMPRESSION, "two": stages.FIRST_IMPRESSION}
    assert await _marked() == {"one", "two"}, "both are marked to be read next, by code, by day"



async def test_opened_ready_roles_and_a_stale_deck_role_explain_why_a_higher_fit_does_not_swap_in(js):
    """Seen live on 1.137.1: Ready held fits 25, 33 and 37, Second Impression held 44 and 38. The low Ready roles were
    opened by Go (they keep their place); the 44 was posted in 2024, so its score is 44 less the stale penalty, and the
    38 does not beat the weakest unopened Ready role (41)."""
    stale = {"posted": date(2024, 10, 11), "found": TODAY}
    await _world(
        3, _role("opened25", stages.READY_TO_LAUNCH, go_opened_at=datetime.now(UTC), **counted(), **_fit(25)),
        _role("ready41", stages.READY_TO_LAUNCH, **counted(), **_fit(41)),
        _role("ready42", stages.READY_TO_LAUNCH, **counted(), **_fit(42)),
        _role("old44", stages.SECOND_IMPRESSION, **counted(), **_fit(44), **stale),
        _role("deck38", stages.SECOND_IMPRESSION, **counted(), **_fit(38)))
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {
        "opened25": stages.READY_TO_LAUNCH, "ready41": stages.READY_TO_LAUNCH, "ready42": stages.READY_TO_LAUNCH,
        "old44": stages.SECOND_IMPRESSION, "deck38": stages.SECOND_IMPRESSION}


# --- the queue holds no transaction while it decides ---------------------------------------------------------------
# `rebalance` once read, then worked the queue out in Python with its transaction open (idle in transaction), then
# wrote. It reads, ends the transaction, decides, and writes only what is still as it read it, under the row's lock.


def _watched(seen: list, name: str, real):
    def watch(*args, **kwargs):
        seen.append((name, db.in_transaction()))
        return real(*args, **kwargs)

    return watch


async def test_the_queue_is_worked_out_with_no_transaction_open(js, monkeypatch):
    await _world(2, _role("a", stages.FIRST_IMPRESSION, **counted(), **_fit(90)),
                 _role("b", stages.FIRST_IMPRESSION, **counted(), **_fit(80)), _role("u", stages.FIRST_IMPRESSION),
                 ranks={"u": 70})
    seen: list = []
    for name in ("order", "points"):
        monkeypatch.setattr(pipeline, name, _watched(seen, name, getattr(pipeline, name)))
    monkeypatch.setattr(placement, "standing", _watched(seen, "standing", placement.standing))  # the bars' Python
    async with db.session_factory()() as session:
        assert await _balance(session) == 3, "a and b go up to Ready; u is marked to be read next"
        await session.commit()
    assert {name for name, _ in seen} == {"order", "points", "standing"}
    assert [name for name, held in seen if held] == [], "the queue is decided from what was read, not inside the read"


async def test_a_role_the_owner_moves_while_the_queue_is_being_worked_out_is_not_moved_back(js, monkeypatch):
    await _world(2, _role("a", stages.FIRST_IMPRESSION, **counted(), **_fit(95)),
                 _role("b", stages.FIRST_IMPRESSION, **counted(), **_fit(80)))
    real = pipeline._apply

    async def the_owner_ends_a(session, owner, plan):
        # the queue is decided and no transaction is open: the owner moves a role before the plan is written
        async with db.session_factory()() as other:
            (await other.scalar(select(Opportunity).where(Opportunity.slug == "a"))).status = stages.REJECTED
            await other.commit()
        return await real(session, owner, plan)

    monkeypatch.setattr(pipeline, "_apply", the_owner_ends_a)
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"a": stages.REJECTED, "b": stages.READY_TO_LAUNCH}, (
        "the owner's move stands: the queue wrote only the role still as it read it")


async def test_a_role_an_analyst_claims_while_the_queue_is_being_worked_out_keeps_its_claim_and_takes_no_mark(
        js, monkeypatch):
    await _world(1, _role("a", stages.FIRST_IMPRESSION), _role("b", stages.FIRST_IMPRESSION),
                 ranks={"a": 90, "b": 60})
    real = pipeline._apply
    claimed_at = datetime.now(UTC)

    async def an_analyst_takes_a(session, owner, plan):
        async with db.session_factory()() as other:  # the queue is decided and no transaction is open
            role = await other.scalar(select(Opportunity).where(Opportunity.slug == "a"))
            role.analysis_state, role.analysis_claimed_at = analysis.WAITING, claimed_at
            await other.commit()
        return await real(session, owner, plan)

    monkeypatch.setattr(pipeline, "_apply", an_analyst_takes_a)
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _marked() == set(), "a role a session has taken is not marked to be read next: it is being read"
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.slug == "a"))
        assert (role.analysis_state, role.analysis_claimed_at) == (analysis.WAITING, claimed_at)


async def test_a_settled_queue_opens_no_transaction_that_writes_and_a_queue_with_moves_writes_in_its_own(
        js, transactions):
    await _world(2, _role("a", stages.FIRST_IMPRESSION, **counted(), **_fit(90)),
                 _role("b", stages.FIRST_IMPRESSION, **counted(), **_fit(80)))
    transactions.clear()
    async with db.session_factory()() as session:
        assert await _balance(session) == 2
        await session.commit()
    assert [transactions.writes(t) for t in transactions.ended] == [False, True, False], (
        "one transaction to read, one, after the queue is decided, to write the two moves, and one of its own for "
        "`stop_waiting`, which now only reads (no role is waiting to be taken out of the analyst's line)")
    transactions.clear()
    async with db.session_factory()() as session:
        assert await _balance(session) == 0
        await session.commit()
    assert transactions.ended and not any(transactions.writes(t) for t in transactions.ended), (
        "nothing to move: the turn only read")


async def test_the_writer_turn_is_free_when_stop_waiting_begins(js, monkeypatch):
    await _world(2, _role("a", stages.FIRST_IMPRESSION, **counted(), **_fit(90)),
                 _role("b", stages.FIRST_IMPRESSION, **counted(), **_fit(80)))
    real = pipeline.stop_waiting
    open_when_it_began: list[bool] = []

    async def probe_then_stop_waiting(session, owner):
        """`stop_waiting` runs in a transaction of its own: the pass's moves are committed and its turn let go before
        it begins, so what it waits for is never the pass's own turn."""
        open_when_it_began.append(session.in_transaction() or db.in_transaction())
        return await real(session, owner)

    monkeypatch.setattr(pipeline, "stop_waiting", probe_then_stop_waiting)
    async with db.session_factory()() as session:
        assert await _balance(session) == 2
        await session.commit()
    assert open_when_it_began == [False], (
        "the moves were written and their transaction ended before `stop_waiting` began: a pass that kept the "
        "writer's turn while it waited would be half of a cycle of waits")
    assert set((await _statuses()).values()) == {stages.READY_TO_LAUNCH}, "and the two moves stand"


@pytest.mark.parametrize("fields", [
    {"analysis_state": analysis.FAILED},
    {"analysis_state": analysis.NO_TEXT},
    {"analysis_state": analysis.DONE, "analysis": {analysis.REANALYSE: True}},
], ids=["its read failed", "it holds no text", "it is already marked to be read again"])
async def test_a_queued_role_the_mark_cannot_go_on_is_not_locked_so_a_settled_queue_writes_nothing(
        js, transactions, fields):
    """Found in review: every such role was locked (`FOR UPDATE`) at every turn only for
    `_apply` to find nothing to change in it, a pattern to avoid."""
    await _world(1, _role("a", stages.FIRST_IMPRESSION, **fields), ranks={"a": 90})
    for turn in (1, 2):
        transactions.clear()
        async with db.session_factory()() as session:
            assert await _balance(session) == 0, "the mark has nowhere to go: nothing is moved"
            await session.commit()
        assert transactions.ended and not any(transactions.writes(t) for t in transactions.ended), (
            f"turn {turn}: the queue only read")
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.slug == "a"))
        assert line.READ_NEXT not in (role.analysis or {})


# --- no unread or ungraded role in Ready to Launch (the owner, 2026-11-03; docs/PRD.md 24.13) -------------------------
# A place in Ready to Launch stands on a Second Impression that graded the posting. The queue gives none to a read that
# graded nothing, a role the owner placed or Launch opened with no read leaves and comes back once read if it still
# ranks, and a move the owner asked for before the read is made once the role is read.


async def test_a_read_that_graded_nothing_takes_no_ready_place_though_it_scores_best(js):
    await _world(None, _role("empty", _FIRST, posted=TODAY - timedelta(days=5), **counted(required=[]), **_fit(95)),
                 _read_at("graded", _FIRST, 60), second_impression_size=3, ready_to_launch_size=1)
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"empty": _SECOND, "graded": _READY}, "it waits on deck for a read that graded"
    async with db.session_factory()() as session:
        assert await _balance(session) == 0, "settled"


async def test_an_unread_ready_role_the_owner_placed_or_launch_opened_leaves_and_comes_back_once_read_if_it_ranks(js):
    ids = await _world(
        None, _role("opened", _READY, go_opened_at=datetime.now(UTC)), _role("hand", _READY),
        _role("kept", _READY, go_opened_at=datetime.now(UTC), **counted(), **_fit(70)),
        ranks={"opened": 60, "hand": 50}, second_impression_size=3, ready_to_launch_size=2)
    async with db.session_factory()() as session:
        await trail.record(session, OWNER, event="role_moved", actor="person", who="Owner",
                           link=("opportunity", ids["hand"]), sentence="You moved it.")
        await session.commit()
    async with db.session_factory()() as session:
        await pipeline.run(session, OWNER)
        await session.commit()
    assert await _statuses() == {"opened": _FIRST, "hand": _FIRST, "kept": _READY}, "unread: they leave Ready"
    assert "left_ready_unread" in await _events("opened") and "left_ready_unread" in await _events("hand")
    async with db.session_factory()() as session:  # the analyst reads them: one scores well, the other does not
        for slug, score in (("opened", 90), ("hand", 20)):
            role = await session.get(Opportunity, ids[slug])
            role.analysis_state, role.analysis = analysis.DONE, counted()["analysis"]
            role.fit, role.fit_breakdown = score, _fit(score)["fit_breakdown"]
        await session.commit()
    async with db.session_factory()() as session:
        await pipeline.run(session, OWNER)
        await session.commit()
    assert await _statuses() == {"opened": _READY, "hand": _SECOND, "kept": _READY}, (
        "back once read, for the one that still ranks")


async def test_a_role_asked_for_in_ready_before_its_read_takes_its_place_once_it_is_read_and_holds_it(js):
    ids = await _world(None, _read_at("asked", _FIRST, 40, ready_asked_at=datetime.now(UTC)),
                       _read_at("best", _READY, 90), second_impression_size=3, ready_to_launch_size=1)
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _statuses() == {"asked": _READY, "best": _SECOND}, "the owner's ask is a place, ahead of the score"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["asked"], populate_existing=True)
        assert role.ready_asked_at is None, "taken"
        line = await session.scalar(select(TrailEvent).where(TrailEvent.link_id == ids["asked"],
                                                             TrailEvent.event == "role_moved"))
        assert line.actor == "person" and line.sentence == (
            "You moved Programs Lead asked at Firm asked from First Impression to Ready to Launch.")
        stood = await placement.one(session, OWNER, role, placement.Purpose.QUEUE)
        assert stood.held is placement.Hold.PLACED, "it holds the place as any role the owner moved by hand"
    async with db.session_factory()() as session:
        assert await _balance(session) == 0, "and stays"
    assert (await _statuses())["asked"] == _READY


async def test_a_role_asked_for_in_ready_whose_read_graded_nothing_is_not_placed_and_keeps_its_ask(js):
    ids = await _world(None, _role("asked", _FIRST, ready_asked_at=datetime.now(UTC), **counted(required=[]),
                                   **_fit(80)), second_impression_size=3, ready_to_launch_size=2)
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert (await _statuses())["asked"] != _READY
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, ids["asked"], populate_existing=True)).ready_asked_at is not None


# --- a Haiku gate zero is zero (docs/PRD.md 24.14) ------------------------------------------------------------------
# The ranker's "Gate:" zero counts as 0, sorts at the bottom and is read by Sonnet only when the owner asks for it
#.


def test_a_new_role_the_ranker_put_at_zero_gets_no_first_hours_bonus_and_sorts_below_an_older_role_with_a_score():
    now = datetime.now(UTC)
    new, old = _aged("new", 0), _aged("old", 10)
    assert pipeline.points(new, 0, None, False, TODAY, now) == 0, "a hard no is not lifted by a new posting's 20"
    assert pipeline.points(_aged("new2", 0), 1, None, False, TODAY, now) == 21, "a score of 1 still takes the bonus"
    assert [r.slug for r in pipeline.order([new, old], {new.id: 0, old.id: 5}, TODAY, now)] == ["old", "new"]


async def test_a_gate_zero_is_haikus_own_zero_and_every_other_rank_stands_as_written(js):
    """Moved here from `test_rank_calibration.py` (docs/PRD.md 24.15). Old: the figures asserted were the rank map's (a
    plain role 55, a gate zero 0 though the map said 55). New: the rank as written, a plain role 20; the gate zero is 0
    because Haiku wrote 0, and the ordinary one is in `Ranked.gate_zeros`, which is the bar and the card's line."""
    ids = await _world(
        3, _role("gate", _FIRST), _role("plain", _FIRST), _role("zero", _FIRST), _role("second", _SECOND),
        ranks={"gate": GATE_ZERO, "plain": 20, "zero": (0, "Not a job posting.\nNothing to score."),
               "second": GATE_ZERO})
    async with db.session_factory()() as session:
        got = await pipeline.ranked(session, OWNER)
    assert got.estimates[ids["plain"]] == 20, "a low rank is the number of a low-ranked role"
    assert got.estimates[ids["zero"]] == 0 and ids["zero"] not in got.gate_zeros, "a zero with no gate named is no bar"
    assert got.estimates[ids["gate"]] == 0 and ids["gate"] in got.gate_zeros, "the ranker said it fails a hard ask"
    assert got.estimates[ids["second"]] == 0 and ids["second"] in got.gate_zeros, "in Second Impression, not ready"


async def test_a_role_that_is_applied_or_closed_with_a_gate_zero_is_not_barred(js):
    """Moved here from `test_rank_calibration.py` (docs/PRD.md 24.15). Old: such a role kept the map's estimate (55).
    New: its number is Haiku's 0 like any gate zero, and the line between it and the ordinary role is that it is not in
    `Ranked.gate_zeros`."""
    ids = await _world(
        3, _role("applied", stages.APPLIED), _role("out", stages.GATED_OUT), _role("quiet", stages.GONE_QUIET),
        ranks={"applied": GATE_ZERO, "out": GATE_ZERO, "quiet": GATE_ZERO})
    async with db.session_factory()() as session:
        got = await pipeline.ranked(session, OWNER)
    assert [got.estimates[ids[s]] for s in ("applied", "out", "quiet")] == [0, 0, 0]
    assert got.gate_zeros == set(), "a role past the queue is not the owner's to be warned off by a score"


async def test_a_gate_zero_the_owner_starred_asked_for_opened_or_moved_is_not_barred(js):
    """Moved here from `test_rank_calibration.py` (docs/PRD.md 24.15). Old: those four kept the map's estimate (55) and
    the untouched one was 0. New: all five are Haiku's 0, and only the untouched one is in `Ranked.gate_zeros`: the
    owner's hand lifts the bar, not the number."""
    now = datetime.now(UTC)
    ids = await _world(
        6, _role("starred", _FIRST, favorited_at=now), _role("asked", _FIRST, ready_asked_at=now),
        _role("opened", _FIRST, go_opened_at=now), _role("moved", _FIRST), _role("untouched", _FIRST),
        ranks={s: GATE_ZERO for s in ("starred", "asked", "opened", "moved", "untouched")})
    async with db.session_factory()() as session:
        # Moved by hand long ago, and code has moved it on since: the owner's choice to pursue it stands.
        session.add(TrailEvent(owner=OWNER, actor="person", who="You", event="role_moved", sentence="You moved it.",
                               link_kind="opportunity", link_id=ids["moved"], at=datetime(2020, 1, 1, tzinfo=UTC)))
        await session.flush()
        got = await pipeline.ranked(session, OWNER)
    assert {s: got.estimates[ids[s]] for s in ids} == dict.fromkeys(ids, 0)
    assert got.gate_zeros == {ids["untouched"]}, "what the owner chose to pursue is theirs: the ranker's gate bars none"


async def test_a_gate_zero_is_not_marked_to_be_read_while_the_queue_has_room_and_a_low_rank_still_is(js):
    await _world(3, _role("good", _FIRST), _role("low", _FIRST), _role("gate", _FIRST),
                 ranks={"good": 60, "low": 5, "gate": GATE_ZERO})
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _marked() == {"good", "low"}, "room for three: the gate zero is left out, the rank of 5 is not"
    assert set((await _statuses()).values()) == {_FIRST}, "and it is closed by nothing: it keeps its place in line"


async def test_a_gate_zero_the_owner_has_touched_is_read_like_any_other(js):
    now = datetime.now(UTC)
    ids = await _world(6, _role("good", _FIRST), _role("starred", _FIRST, favorited_at=now),
                       _role("asked", _FIRST, ready_asked_at=now), _role("moved", _FIRST), _role("gate", _FIRST),
                       ranks={"good": 60, "starred": GATE_ZERO, "asked": GATE_ZERO, "moved": GATE_ZERO,
                              "gate": GATE_ZERO})
    async with db.session_factory()() as session:
        # Moved by hand long ago, and code has moved it on since: the move is spent for the queue (`placement`), but the
        # owner's choice to pursue it stands.
        session.add(TrailEvent(owner=OWNER, actor="person", who="You", event="role_moved", sentence="You moved it.",
                               link_kind="opportunity", link_id=ids["moved"], at=datetime(2020, 1, 1, tzinfo=UTC)))
        await session.commit()
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _marked() == {"good", "starred", "asked", "moved"}, "the owner's hand overrules the ranker's gate"


async def test_a_gate_zero_marked_to_be_read_before_is_taken_off_the_line(js):
    await _world(3, _role("good", _FIRST), _role("gate", _FIRST, analysis={line.READ_NEXT: True}),
                 ranks={"good": 60, "gate": GATE_ZERO})
    assert await _marked() == {"gate"}
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    assert await _marked() == {"good"}


# --- a Boardone role is looked up at the read-next mark, not before (the owner, 2026-11-05; docs/PRD.md 32.3) -------
# A Boardone role in First Impression holds Boardone's text and no employer posting, and Sonnet reads the employer's own
# posting: so a role the walk of the best estimates passes is marked for its employer's posting to be looked up
# (`models.SOURCE_NEXT`), and the read slots go to the roles below it. It is marked to be read (`READ_NEXT`) only
# once it is Sourced. Roles from other boards are marked as before.

BOARDONE = "https://www.boardone.test/jobs/view/{slug}/"
_SOURCED = {"sourced_at": datetime(2026, 11, 5, 8, 0, tzinfo=UTC), "source_url": "https://careers.example.test/jobs/1",
            "source_kind": "employer"}


def _on_boardone(slug: str, **more) -> Opportunity:
    """A First Impression role found on Boardone whose employer's posting is not found. A role a test makes is Sourced
    unless it says not (`conftest`), so this one says so."""
    link = BOARDONE.format(slug=slug)
    return _role(slug, stages.FIRST_IMPRESSION, url=link, board_url=link, sourced_at=None, source_url=None,
                 source_kind=None, **more)


async def _looked_up() -> set[str]:
    """The roles marked for their employer's posting to be looked up (`SOURCE_NEXT`)."""
    async with db.session_factory()() as session:
        rows = await session.scalars(select(Opportunity).execution_options(populate_existing=True))
        return {r.slug for r in rows if SOURCE_NEXT in (r.analysis or {})}


async def _sourced(slug: str) -> None:
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.slug == slug))
        for column, value in _SOURCED.items():
            setattr(role, column, value)
        await session.commit()


async def _one_turn() -> int:
    async with db.session_factory()() as session:
        moved = await _balance(session)
        await session.commit()
    return moved


async def test_an_unsourced_boardone_role_at_the_top_is_looked_up_first_and_the_read_goes_to_the_role_below(js):
    await _world(1, _on_boardone("li"), _role("below", stages.FIRST_IMPRESSION), ranks={"li": 90, "below": 60})
    assert await _one_turn() >= 1
    assert await _looked_up() == {"li"}, "its employer's posting is looked up first"
    assert await _marked() == {"below"}, "no read slot waits on a role with no employer posting to read"
    assert "source_next" in await _events("li") and "read_next" not in await _events("li")
    assert await _one_turn() == 0, "settled: the same marks on the next run"


async def test_a_role_once_sourced_competes_like_any_other_and_its_lookup_mark_goes(js):
    await _world(1, _on_boardone("li"), _role("below", stages.FIRST_IMPRESSION), ranks={"li": 90, "below": 60})
    await _one_turn()
    await _sourced("li")
    await _one_turn()
    assert await _marked() == {"li"}, "found, it is the best estimate and takes the read"
    assert await _looked_up() == set(), "and its lookup mark goes with the find"
    assert "read_next" in await _events("li")


async def test_at_most_the_decks_size_of_roles_are_looked_up_and_a_sourced_one_stops_counting(js):
    await _world(2, *(_on_boardone(f"li{i}") for i in range(1, 5)), _role("plain", stages.FIRST_IMPRESSION),
                 ranks={"li1": 90, "li2": 80, "li3": 70, "li4": 60, "plain": 50})
    await _one_turn()
    assert await _looked_up() == {"li1", "li2"}, "the deck's size, the best two"
    assert await _marked() == {"plain"}
    await _sourced("li1")
    await _one_turn()
    assert await _marked() == {"li1", "plain"}
    assert await _looked_up() == {"li2", "li3"}, "a found role is no carrier: the third lookup starts"


async def test_a_role_that_loses_its_place_loses_its_lookup_mark(js):
    ids = await _world(1, _on_boardone("old"), _on_boardone("new"), _role("plain", stages.FIRST_IMPRESSION),
                       ranks={"old": 70, "new": 20, "plain": 10})
    await _one_turn()
    assert await _looked_up() == {"old"}
    async with db.session_factory()() as session:
        await rank_roles(session, {ids["new"]: 95})
        await session.commit()
    await _one_turn()
    assert await _looked_up() == {"new"}, "the better estimate takes the place, as it does for a read"


async def test_a_role_the_browser_gave_up_on_is_neither_looked_up_nor_counted_and_rejoins_when_the_note_clears(js):
    ids = await _world(1, _on_boardone("gave-up", analysis={SOURCE_NEXT: True}, browser_note="no posting found"),
                       _on_boardone("next"), _role("plain", stages.FIRST_IMPRESSION),
                       ranks={"gave-up": 90, "next": 80, "plain": 60})
    await _one_turn()
    assert await _looked_up() == {"gave-up", "next"}, (
        "the next role is looked up though the deck's one place would be the given-up role's; its mark stays, so its "
        "stuck item and its note do (dropping it would drop the note and the role would be tried again for ever)")
    assert await _marked() == {"plain"}
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["gave-up"])).browser_note = None
        await session.commit()
    await _one_turn()
    assert await _looked_up() == {"gave-up"}, "the note is gone: it is the best estimate again and takes the place"


async def test_no_lookup_starts_while_boardone_is_stopped_and_the_slot_goes_to_the_role_below(js, monkeypatch):
    monkeypatch.setattr(host_stops, "_stopped", {"boardone.test": "www.boardone.test answered 403"})
    await _world(1, _on_boardone("li"), _role("below", stages.FIRST_IMPRESSION), ranks={"li": 90, "below": 60})
    await _one_turn()
    assert await _looked_up() == set()
    assert await _marked() == {"below"}


async def test_a_lookup_starts_again_once_linkedins_stop_has_run_out(js, monkeypatch):
    """Boardone's stop lasts until the owner's next off hours start (docs/PRD.md 48.14 d): the deck reads it as the
    fetcher does, so the lookup it held goes ahead by itself."""
    monkeypatch.setattr(host_stops, "_stopped", {"boardone.test": "a security check"})
    monkeypatch.setattr(host_stops, "_ends", {"boardone.test": datetime.now(UTC) + timedelta(hours=5)})
    await _world(1, _on_boardone("li"), _role("below", stages.FIRST_IMPRESSION), ranks={"li": 90, "below": 60})
    await _one_turn()
    assert await _looked_up() == set() and await _marked() == {"below"}, "while it is in force"
    monkeypatch.setitem(host_stops._ends, "boardone.test", datetime.now(UTC) - timedelta(minutes=1))
    await _one_turn()
    assert await _looked_up() == {"li"}, "once it has run out"


async def test_a_stop_another_process_made_holds_the_lookups_too(js, monkeypatch):
    """Found by review (2026-11-05): the deck read the stops this process loaded at its start, so the hand-run command's
    stop (another process, written to the record only) did not hold it until a restart. It asks the record each turn."""
    monkeypatch.setattr(host_stops, "_stopped", {})  # loaded at the start: nothing was stopped then
    monkeypatch.setattr(host_stops, "_ends", {})
    await _world(1, _on_boardone("li"), _role("below", stages.FIRST_IMPRESSION), ranks={"li": 90, "below": 60})
    async with db.session_factory()() as session:
        session.add(BrowserDomain(owner=OWNER, domain="boardone.test", stopped_at=datetime.now(UTC),
                                  stopped_until=datetime.now(UTC) + timedelta(hours=5),
                                  stopped_for="www.boardone.test answered 403"))
        await session.commit()
    await _one_turn()
    assert await _looked_up() == set() and await _marked() == {"below"}, "held by the record's stop"
    async with db.session_factory()() as session:
        row = await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == "boardone.test"))
        row.stopped_until = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    monkeypatch.setattr(host_stops, "_stopped", {})
    await _one_turn()
    assert await _looked_up() == {"li"}, "and goes ahead when the record's stop has run out"


async def test_a_lookup_already_marked_stays_marked_while_boardone_is_stopped(js, monkeypatch):
    """Only a new lookup is held: the stop ends the waiting items the way it ends any (`host_stops.end_waiting`)."""
    monkeypatch.setattr(host_stops, "_stopped", {"boardone.test": "www.boardone.test answered 403"})
    await _world(1, _on_boardone("li", analysis={SOURCE_NEXT: True}), _role("below", stages.FIRST_IMPRESSION),
                 ranks={"li": 90, "below": 60})
    await _one_turn()
    assert await _looked_up() == {"li"}


async def test_no_lookup_starts_while_most_roles_still_wait_for_their_rank_as_no_read_does(js):
    await _world(1, _on_boardone("li"), _role("below", stages.FIRST_IMPRESSION), _role("raw", stages.CLEANSED_DATA),
                 ranks={"li": 90, "below": 60})
    await _one_turn()
    assert await _looked_up() == set() and await _marked() == set()


@pytest.mark.parametrize("link", [
    "https://www.boardthree.test/job-detail/abc",
    "https://careers.example.test/jobs/1?ref=boardone.test",
    "https://notlinkedin.com/jobs/1",
], ids=["another board", "an employer's own page that names Boardone in its query", "a look-alike host"])
async def test_a_role_not_on_boardone_is_marked_to_be_read_as_before(js, link):
    await _world(1, _role("other", stages.FIRST_IMPRESSION, url=link, sourced_at=None, source_url=None,
                          source_kind=None), ranks={"other": 80})
    await _one_turn()
    assert await _marked() == {"other"} and await _looked_up() == set()


async def test_a_boardone_role_that_holds_the_read_mark_loses_it_and_is_marked_for_its_lookup(js):
    await _world(1, _on_boardone("li", analysis={line.READ_NEXT: True}), _role("below", stages.FIRST_IMPRESSION),
                 ranks={"li": 90, "below": 60})
    assert await _marked() == {"li"}
    await _one_turn()
    assert await _marked() == {"below"}
    assert await _looked_up() == {"li"}
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.slug == "li"))
        assert role.analysis == {SOURCE_NEXT: True}, "the one mark replaced the other"


@pytest.mark.parametrize("asked", [{"favorited_at": datetime(2026, 11, 5, tzinfo=UTC)},
                                   {"ready_asked_at": datetime(2026, 11, 5, tzinfo=UTC)}],
                         ids=["starred", "asked for in Ready to Launch"])
async def test_a_boardone_role_the_owner_asked_for_is_not_in_the_walk_and_takes_no_ready_place_unread(js, asked):
    """Its lookup is the owner's, at any hour (priority 0, `queue._wanted`), and a role with no read takes no place in
    Ready to Launch whoever asked (docs/PRD.md 24.13): it holds no read slot, and it is not marked."""
    await _world(1, _on_boardone("li", **asked), _role("below", stages.FIRST_IMPRESSION),
                 ranks={"li": 95, "below": 60})
    await _one_turn()
    assert await _looked_up() == set()
    assert await _marked() == {"below"}
    assert (await _statuses())["li"] == stages.FIRST_IMPRESSION


async def test_a_role_the_ranker_ruled_out_on_a_gate_is_not_looked_up(js):
    await _world(2, _on_boardone("gate"), _on_boardone("fine"), ranks={"gate": GATE_ZERO, "fine": 40})
    await _one_turn()
    assert await _looked_up() == {"fine"}


async def test_a_lookup_mark_that_is_already_on_writes_nothing_on_the_next_turn(js, transactions):
    await _world(1, _on_boardone("li"), _role("below", stages.FIRST_IMPRESSION), ranks={"li": 90, "below": 60})
    await _one_turn()
    transactions.clear()
    assert await _one_turn() == 0
    assert transactions.ended and not any(transactions.writes(t) for t in transactions.ended), "a settled queue reads"


def test_the_first_hours_bonus_is_what_the_number_has_of_them():
    now = datetime.now(UTC)
    fresh, old = _aged("fresh", 0), _aged("old", 10)
    assert pipeline.first_hours_bonus(fresh, 70, None, False, TODAY, now) == 20
    assert pipeline.first_hours_bonus(old, 70, None, False, TODAY, now) == 0, "the hours are over"
    assert pipeline.first_hours_bonus(fresh, 0, None, False, TODAY, now) == 0, "a rank of 0 is not lifted"
    assert pipeline.first_hours_bonus(fresh, None, None, False, TODAY, now) == 0, "no score, no bonus"
    own = pipeline.QueueRules(golden_bonus=35)
    assert pipeline.first_hours_bonus(fresh, 70, None, False, TODAY, now, rules=own) == 35
    assert pipeline.shown(fresh, 70, None, False, TODAY, now) == 70 + pipeline.first_hours_bonus(
        fresh, 70, None, False, TODAY, now)


async def _probe_writer(slug: str) -> bool:
    """Whether a second writer gets through now (a statement in a session of its own, rolled back)."""
    async with db.session_factory()() as other:
        try:
            await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.slug == slug).values(slug=slug)),
                                   1)
            return True
        except TimeoutError:  # queued behind the holder of the writer's turn
            return False
        finally:
            await other.rollback()


async def test_the_gates_are_read_without_the_writers_turn_and_close_the_roles_a_pass_inside_one_closes(
        js, monkeypatch):
    """On a large database the hard gates at Cleansed Data held the file's one writer turn for too long at
    every round of a turn. Where the session holds nothing, the roles, their copies and the judgement are made in a
    reader's session (a second writer gets through meanwhile) and only a role to close is locked and written. In a
    session that already has a write in it, it reads in that transaction as it always did, and closes the same roles."""
    from app.jobsearch.postings import cleansed

    seen: list[tuple[bool, bool]] = []  # (read in a reader's session, a second writer got through)
    real = cleansed.copies

    async def copies(session, owner, ids):
        seen.append((db.is_reader(session), await _probe_writer("ranked") if db.is_reader(session) else False))
        return await real(session, owner, ids)

    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"compensation": {"amount": 95_000, "against": "maximum"}})
        low, high = _role("low", stages.CLEANSED_DATA, comp=None), _role("high", stages.CLEANSED_DATA, comp=None)
        ranked = _role("ranked", stages.FIRST_IMPRESSION)
        session.add_all([low, high, ranked])
        await session.flush()
        await _with_copy(session, low, pay_min=60_000, pay_max=70_000)
        await _with_copy(session, high, pay_min=120_000, pay_max=150_000)
        await _with_copy(session, ranked, pay_min=60_000, pay_max=70_000)
        await session.commit()
    monkeypatch.setattr(cleansed, "copies", copies)
    async with db.session_factory()() as session:
        assert await pipeline.gate(session, OWNER) == 1
        await session.commit()
    assert seen == [(True, True)], "the copies were read apart, and a second writer got through"
    apart = await _statuses()
    assert apart == {"low": stages.GATED_OUT, "high": stages.CLEANSED_DATA, "ranked": stages.FIRST_IMPRESSION}

    seen.clear()
    async with db.session_factory()() as session:  # put back, and gated in a session that holds a write already
        await session.execute(update(Opportunity).where(Opportunity.slug == "low").values(
            status=stages.CLEANSED_DATA, screened_out_reason=None, screened_out_by=None, screened_out_step=None))
        assert await pipeline.gate(session, OWNER) == 1
        await session.commit()
    assert seen == [(False, False)], "a transaction with a write in it is read in itself"
    assert await _statuses() == apart


async def test_a_role_changed_meanwhile_is_not_closed_by_a_gate_decided_before_the_change(js, monkeypatch):
    """The gates are decided from a read with no transaction open, so a role the owner moved before the closing is
    locked keeps its place: only a role still as it was read is closed."""
    from app.jobsearch import as_read

    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"compensation": {"amount": 95_000, "against": "maximum"}})
        low = _role("low", stages.CLEANSED_DATA, comp=None)
        session.add(low)
        await session.flush()
        await _with_copy(session, low, pay_min=60_000, pay_max=70_000)
        await session.commit()
    real = as_read.lock

    async def the_owner_moves_it(session, model, rows, **more):
        async with db.session_factory()() as other:
            (await other.scalar(select(Opportunity).where(Opportunity.slug == "low"))).status = stages.REJECTED
            await other.commit()
        return await real(session, model, rows, **more)

    monkeypatch.setattr(as_read, "lock", the_owner_moves_it)
    async with db.session_factory()() as session:
        assert await pipeline.gate(session, OWNER) == 0
        await session.commit()
    assert await _statuses() == {"low": stages.REJECTED}


async def test_the_queue_is_read_without_the_writers_turn_and_moves_the_roles_a_pass_inside_one_moves(
        js, monkeypatch):
    """On a large database the reads of `rebalance` held the file's one writer turn for too long at every
    round of a turn. Where the session holds nothing, and no role waits to take the places its postings name, the queue
    is read in a reader's session (a second writer gets through meanwhile) and the moves are written to the rows the
    lock gave. In a session that already has a write in it, it reads in that transaction, and moves the same."""
    ids = await _world(2, _role("a", stages.FIRST_IMPRESSION, **counted(), **_fit(90)),
                       _role("b", stages.FIRST_IMPRESSION, **counted(), **_fit(80)))
    seen: list[tuple[bool, bool]] = []  # (read in a reader's session, a second writer got through)
    real = placement.gather

    async def gather(session, *args, **kwargs):
        seen.append((db.is_reader(session), await _probe_writer("a") if db.is_reader(session) else False))
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(placement, "gather", gather)
    async with db.session_factory()() as session:
        moved = await _balance(session)
        await session.commit()
    assert moved == 2 and seen == [(True, True)]
    apart = await _statuses()
    assert set(apart.values()) == {stages.READY_TO_LAUNCH}

    seen.clear()
    async with db.session_factory()() as session:  # put back, and queued in a session that holds a write already
        await session.execute(update(Opportunity).where(Opportunity.id.in_(ids.values())).values(
            status=stages.FIRST_IMPRESSION))
        assert await _balance(session) == moved
        await session.commit()
    assert seen == [(False, False)], "a transaction with a write in it is read in itself"
    assert await _statuses() == apart


async def test_a_role_with_no_place_is_read_in_the_writers_transaction_only_when_its_postings_name_one(js):
    """The places a role's postings name are written before the queue's reads, so a turn that must write them reads in
    the caller's transaction; one with nothing to write (no posting names a place) reads apart."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 2})
        lone, named = _role("lone", stages.FIRST_IMPRESSION), _role("named", stages.FIRST_IMPRESSION)
        session.add_all([lone, named])
        await session.flush()
        await _with_copy(session, lone)
        await _with_copy(session, named)
        for posting in await session.scalars(select(JobPosting).where(JobPosting.opportunity_id == named.id)):
            posting.location = "Anytown, ST"
        await session.commit()
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity))).all())
        by_slug = {r.slug: r for r in roles}
        assert await pipeline._naming_places(session, OWNER, [by_slug["lone"]]) == []
        assert await pipeline._naming_places(session, OWNER, [by_slug["named"]]) == [by_slug["named"].id]
        assert await pipeline._naming_places(session, OWNER, roles) == [by_slug["named"].id]


async def test_the_places_postings_name_are_written_a_batch_to_a_transaction_before_the_queue_is_read_apart(
        js, monkeypatch, transactions):
    """On a large database the first turn after a restore wrote the places of many roles in the
    transaction of the queue's reads, one long hold. The places are written a batch of roles
    (`stated_place.PLACES_BATCH`) to a transaction, then the queue is read apart, and every role has the place its
    posting names."""
    from app.jobsearch.gating import stated_place

    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        roles = [_role(slug, stages.FIRST_IMPRESSION) for slug in ("a", "b", "c")]
        session.add_all(roles)
        await session.flush()
        for role in roles:
            await _with_copy(session, role)
        for posting in await session.scalars(select(JobPosting)):
            posting.location = "Anytown, ST"
        await session.commit()
    monkeypatch.setattr(stated_place, "PLACES_BATCH", 2)
    transactions.clear()
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    placing = [t for t in transactions.ended if any(
        x.lstrip().upper().startswith("UPDATE JS_OPPORTUNITIES") and "location" in x for x in t)]
    assert len(placing) == 2, "two batches of roles (2 and 1), each its own transaction"
    async with db.session_factory()() as session:
        places = {r.slug: r.location for r in await session.scalars(select(Opportunity))}
    assert places == {"a": "Anytown, ST", "b": "Anytown, ST", "c": "Anytown, ST"}


# --- an employer's own limit (`Settings.employer_places`, the owner, 2026-11-17) ------------------------------------


def _at(firm: str, slug: str, status: str = stages.FIRST_IMPRESSION, **more) -> Opportunity:
    return _role(slug, status, company=firm, **more)


async def _limited_world(places: str | None, *roles: Opportunity, ranks: dict[str, int] | None = None) -> None:
    await _world(None, *roles, ranks=ranks, second_impression_size=3, ready_to_launch_size=2,
                 research_start_multiple=0, employer_places=places)
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()


def _globex_and_others() -> list[Opportunity]:
    return [*[_at("Globex", f"g{n}", **counted(), **_fit(95 - n)) for n in range(5)],
            *[_role(f"o{n}", stages.FIRST_IMPRESSION, **counted(), **_fit(60 - n)) for n in range(4)]]


async def test_an_employer_with_a_limit_holds_that_many_places_and_other_employers_take_the_rest(js):
    await _limited_world("Globex: 2", *_globex_and_others())
    held = await _statuses()
    assert {s for s, st in held.items() if st == stages.READY_TO_LAUNCH} == {"g0", "g1"}, "its best two by number"
    assert {s for s, st in held.items() if st == stages.SECOND_IMPRESSION} == {"o0", "o1", "o2"}
    assert {held[s] for s in ("g2", "g3", "g4")} == {stages.FIRST_IMPRESSION}, "kept, and waiting, not closed"


async def test_with_no_limit_an_employers_roles_take_every_place_their_numbers_win(js):
    await _limited_world(None, *_globex_and_others())
    held = await _statuses()
    assert {s for s, st in held.items() if st in (stages.READY_TO_LAUNCH, stages.SECOND_IMPRESSION)} == {
        "g0", "g1", "g2", "g3", "g4"}


async def test_a_limited_employers_name_is_matched_as_employers_are_and_a_role_sent_back_says_why(js):
    await _limited_world("GLOBEX: 1", _at("Globex", "best", **counted(), **_fit(90)),
                         _at("Globex (Retail)", "next", stages.READY_TO_LAUNCH, **counted(), **_fit(80)))
    assert await _statuses() == {"best": stages.READY_TO_LAUNCH, "next": stages.FIRST_IMPRESSION}
    async with db.session_factory()() as session:
        role_id = await session.scalar(select(Opportunity.id).where(Opportunity.slug == "next"))
        said = list(await session.scalars(select(TrailEvent.sentence).where(TrailEvent.link_id == role_id)))
    assert any("your limit of 1 at Globex" in s for s in said), said


async def test_unread_roles_at_a_limited_employer_are_read_only_while_they_could_take_one_of_its_places(js):
    await _limited_world(
        "Globex: 2", _at("Globex", "g-top", **counted(), **_fit(90)), _at("Globex", "g-low", **counted(), **_fit(70)),
        _at("Globex", "u99"), _at("Globex", "u98"), _at("Globex", "u97"), _at("Globex", "u60"),
        _role("other", stages.FIRST_IMPRESSION), ranks={"u99": 99, "u98": 98, "u97": 97, "u60": 60, "other": 40})
    assert await _marked() == {"u99", "u98", "other"}, (
        "two at a time (the limit), none whose estimate is under the weakest role holding a place, and the analyst "
        "goes on to the other employer")


async def test_a_role_the_owner_placed_counts_toward_its_employers_limit(js):
    roles = [_at("Globex", "mine", **counted(), **_fit(50)), *_globex_and_others()]
    await _world(None, *roles, second_impression_size=3, ready_to_launch_size=2, research_start_multiple=0,
                 employer_places="Globex: 2")
    async with db.session_factory()() as session:
        mine = await session.scalar(select(Opportunity).where(Opportunity.slug == "mine"))
        await board.move(session, OWNER, mine.id, stages.SECOND_IMPRESSION)
        await session.commit()
    async with db.session_factory()() as session:
        await _balance(session)
        await session.commit()
    held = await _statuses()
    assert held["mine"] == stages.SECOND_IMPRESSION and held["g0"] == stages.READY_TO_LAUNCH
    assert {held[s] for s in ("g1", "g2", "g3", "g4")} == {stages.FIRST_IMPRESSION}


def test_an_employers_limit_is_written_as_a_name_and_a_number_and_says_what_is_wrong_otherwise():
    assert Settings(employer_places=" Globex: 4;\nInitech Labs:2 ").employer_places == "Globex: 4, Initech Labs: 2"
    assert Settings(employer_places="  ").employer_places is None
    assert pipeline.queue_rules_of(Settings(employer_places="GLOBEX (Retail): 4")).employer_places == (("globex", 4),)
    for wrong in ("Globex 4", "Globex: four", "Globex: 0", ": 3", "Globex: 2, globex: 3"):
        with pytest.raises(ValueError):
            Settings(employer_places=wrong)
