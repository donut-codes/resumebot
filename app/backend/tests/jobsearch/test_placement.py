"""Where a role is allowed to sit (`pipeline/placement.py`): which roles code leaves where they are, and why, asked by
purpose. Everything goes through `load`, `one` and `starred`. The owner's moves are made by the real writer
(`board.move`) in a transaction of their own, and code moving a role on afterwards is a later transaction, so the
writer-to-reader contract is pinned: a move and its own line count as placed, and a status code changed since spends
it. Made-up data only."""

import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import check as gating
from app.jobsearch.models import ORIGIN_OWNER, ORIGIN_TRACKER, Opportunity
from app.jobsearch.pipeline import board, placement
from app.jobsearch.pipeline.placement import Hold, Purpose, Standing
from app.jobsearch.research import analysis
from app.timeutil import owner_today
from app.trail import service as trail
from tests.builders import counted
from tests.support import OWNER, PROFILE, load_profile

C, F, S, R = (stages.CLEANSED_DATA, stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)
#: The statuses a role the owner just moved is held at, for each purpose that reads the owner's hand as "still where
#: they put it". Written out here, not read from the table: the test pins the table.
PLACED_AT = {Purpose.STAGE: {C, F, S, R}, Purpose.QUEUE: {F, S, R}, Purpose.REREAD: {S, R}, Purpose.DROP: {S, R},
             Purpose.LIMIT: {F, S, R}}
EVERY_STATUS = (stages.FOLLOWUP_NEEDED, stages.LANDING_ZONE, C, F, S, R)
#: Where each purpose honours each hold: the statuses at which a role that has that hold alone is held. Written out
#: here, not read from the table, so the test pins it. Absent: the purpose does not honour the hold at any status.
EVERYWHERE = set(EVERY_STATUS)
HONOURED_AT = {
    Purpose.STAGE: {Hold.STAR: {R}, Hold.OPENED: {R}, Hold.PLACED: {C, F, S, R}, Hold.OWN: {S, R},
                    Hold.READING: {S, R}},
    Purpose.QUEUE: {Hold.STAR: {F, S, R}, Hold.OPENED: {R}, Hold.PLACED: {F, S, R}, Hold.SKIPPED: {R},
                    Hold.OWN: {S, R}, Hold.READING: {S, R}},
    Purpose.REREAD: {Hold.OPENED: EVERYWHERE, Hold.PLACED: {S, R}},
    Purpose.DROP: {Hold.OPENED: EVERYWHERE, Hold.PLACED: {S, R}},
    Purpose.LIMIT: {Hold.PLACED: {F, S, R}},
    Purpose.GATES: {Hold.MOVED: EVERYWHERE},
}
#: A town in a job system's order that the made-up owner did not list: only remote can pass there, so a role that holds
#: it and no way of working has an undecided work location gate.
FENWICK = "US, WA, Fenwick"
MUST_YEARS = {"id": "must-years", "kind": "job_fact", "section": "posting", "fact": "experience_years", "op": "<=",
              "value": 5}
GAP = "a place at an employer applied to within the gap is not offered"
YEARS = "waiting for the years of experience asked"
PLACE = "where the work is (the work location gate cannot be decided yet)"


def _role(slug: str, status: str = F, **more) -> Opportunity:
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": f"Firm {slug}", "title": f"Programs Lead {slug}", "origin": "scout",
        "match_key": f"firm {slug}|programs lead {slug}", "status": status, "found": owner_today(),
        "posted": owner_today(), **more})


def _with_must() -> dict:
    gates = copy.deepcopy(PROFILE["gates"])
    gates["other"] = [*gates.get("other", []), MUST_YEARS]
    return gates


async def _make(*roles: Opportunity, gates: dict | None = None, **settings) -> dict[str, uuid.UUID]:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates or PROFILE["gates"],
                           settings={**PROFILE["settings"], **settings})
        session.add_all(roles)
        await session.flush()
        ids = {r.slug: r.id for r in roles}
        await session.commit()
        return ids


async def _by_hand(role_id: uuid.UUID, to: str) -> None:
    """The owner moves the role: the board's own writer, in a transaction of its own."""
    async with db.session_factory()() as session:
        await board.move(session, OWNER, role_id, to, allowed=stages.STAGES)
        await session.commit()


async def _by_code(role_id: uuid.UUID, to: str) -> None:
    """Code moves the role on later (a migration, the stages): a status change in a transaction of its own."""
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).status = to
        await session.commit()


async def _stand(purpose: Purpose, **more) -> dict[str, Standing]:
    """Every role the owner has, as `purpose` sees it, loaded the way the callers load them (research deferred)."""
    async with db.session_factory()() as session:
        roles = list((await session.scalars(
            select(Opportunity).where(Opportunity.owner == OWNER)
            .options(defer(Opportunity.analysis), defer(Opportunity.description))
            .execution_options(populate_existing=True))).all())
        stood = await placement.load(session, OWNER, roles, purpose, **more)
        return {r.slug: stood[r.id] for r in roles}


async def _held(purpose: Purpose) -> dict[str, Hold | None]:
    return {slug: standing.held for slug, standing in (await _stand(purpose)).items()}


# --- the owner's hand -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("purpose", list(PLACED_AT))
async def test_a_role_the_owner_moved_is_held_by_hand_at_the_statuses_each_purpose_honours(js, purpose):
    ids = await _make(*(_role(f"to-{status}", F if status != F else S, **counted()) for status in EVERY_STATUS))
    for status in EVERY_STATUS:
        await _by_hand(ids[f"to-{status}"], status)
    held = await _held(purpose)
    assert {slug: hold for slug, hold in held.items()} == {
        f"to-{status}": Hold.PLACED if status in PLACED_AT[purpose] else None for status in EVERY_STATUS}


async def test_the_queue_gives_a_role_the_owner_placed_a_seat_at_second_impression_or_ready_and_none_at_first(js):
    ids = await _make(_role("first", S), _role("second", F), _role("ready", F, **counted()))
    await _by_hand(ids["first"], F)
    await _by_hand(ids["second"], S)
    await _by_hand(ids["ready"], R)
    seats = {slug: standing.seat for slug, standing in (await _stand(Purpose.QUEUE)).items()}
    assert seats == {"first": None, "second": S, "ready": R}
    assert {s.seat for s in (await _stand(Purpose.STAGE)).values()} == {None}, "only the queue gives a seat"


async def test_a_hand_move_code_has_moved_on_from_holds_no_place_but_the_gates_still_leave_the_role_alone(js):
    ids = await _make(_role("spent", S, **counted()))
    await _by_hand(ids["spent"], R)
    for purpose in PLACED_AT:
        assert (await _held(purpose))["spent"] is Hold.PLACED, purpose
    await _by_code(ids["spent"], F)  # code moved it on: a migration, the stages since
    for purpose in PLACED_AT:
        assert (await _held(purpose))["spent"] is None, f"{purpose}: the move is spent, the role is like any other"
    assert (await _held(Purpose.GATES))["spent"] is Hold.MOVED, "no re-run of the gates moves it again (PRD 39.14)"


async def test_a_move_and_its_own_line_in_one_transaction_count_as_placed_and_a_later_move_places_it_again(js):
    """The board's writer changes the status and writes the owner's line in one transaction: the stage move is stamped
    with the transaction's start, the line with the app's clock, and the move still counts as placed."""
    ids = await _make(_role("moved", F, **counted()))
    await _by_hand(ids["moved"], S)
    assert (await _held(Purpose.STAGE)) == {"moved": Hold.PLACED}
    await _by_code(ids["moved"], F)
    assert (await _held(Purpose.STAGE)) == {"moved": None}
    await _by_hand(ids["moved"], R)
    assert (await _held(Purpose.STAGE)) == {"moved": Hold.PLACED}, "the owner's next move places it"
    assert (await _held(Purpose.GATES)) == {"moved": Hold.MOVED}


async def test_no_hand_holds_a_role_at_followup_needed_or_landing_zone(js):
    ids = await _make(_role("followup", F), _role("landing", F))
    await _by_hand(ids["followup"], stages.FOLLOWUP_NEEDED)
    await _by_hand(ids["landing"], stages.LANDING_ZONE)
    for purpose in (*PLACED_AT, Purpose.GATES):
        held = await _held(purpose)
        expected = Hold.MOVED if purpose is Purpose.GATES else None
        assert held == {"followup": expected, "landing": expected}, purpose


async def test_only_the_owners_board_move_is_a_hand_move(js):
    slugs = ("mine", "app", "other-event", "packet", "other-owner", "other-kind")
    ids = await _make(*(_role(slug, S) for slug in slugs))

    async def line(slug: str, *, owner: str = OWNER, event: str = placement.MOVED_EVENT, actor: str = "person",
                   kind: str = "opportunity") -> None:
        async with db.session_factory()() as session:
            await trail.record(session, owner, event=event, actor=actor, who="Someone", sentence="A line.",
                               link=(kind, ids[slug]))
            await session.commit()

    await line("mine")
    await line("app", actor="app")
    await line("other-event", event="favorite_set")
    await line("packet", event="packet_asked")  # an ask for a packet is not a stage move (the owner, 2026-10-20)
    await line("other-owner", owner="someone-else")
    await line("other-kind", kind="employer")
    held = await _held(Purpose.GATES)
    assert held == {"mine": Hold.MOVED, "app": None, "other-event": None, "packet": None, "other-owner": None,
                    "other-kind": None}


# --- Go opened it, the star, Go skipped it, own data and a read under way ---------------------------------------------


async def test_go_opened_holds_a_ready_role_for_the_stage_and_the_queue_and_a_deck_role_for_rereads_and_drops(js):
    now = datetime.now(UTC)
    await _make(*(_role(f"at-{status}", status, go_opened_at=now, **counted()) for status in EVERY_STATUS))
    held = {purpose: await _held(purpose) for purpose in Purpose}
    for purpose in (Purpose.STAGE, Purpose.QUEUE):
        assert held[purpose] == {f"at-{s}": Hold.OPENED if s == R else None for s in EVERY_STATUS}, purpose
    for purpose in (Purpose.REREAD, Purpose.DROP):
        assert held[purpose] == {f"at-{s}": Hold.OPENED for s in EVERY_STATUS}, f"{purpose}: at any status"
    for purpose in (Purpose.LIMIT, Purpose.GATES):
        assert held[purpose] == {f"at-{s}": None for s in EVERY_STATUS}, f"{purpose}: Go's opening is not its concern"
    seats = {slug: standing.seat for slug, standing in (await _stand(Purpose.QUEUE)).items()}
    assert seats[f"at-{R}"] == R and seats[f"at-{S}"] is None


async def test_a_read_favorite_before_the_application_holds_ready_and_counts_and_an_unread_or_ended_one_does_not(js):
    now = datetime.now(UTC)
    await _make(
        _role("read-first", F, favorited_at=now, **counted()), _role("read-second", S, favorited_at=now, **counted()),
        _role("read-ready", R, favorited_at=now, **counted()), _role("unread", R, favorited_at=now),
        _role("followup", stages.FOLLOWUP_NEEDED, favorited_at=now, **counted()),
        _role("applied", stages.APPLIED, favorited_at=now, **counted()),
        _role("rejected", stages.REJECTED, favorited_at=now, **counted()),
        _role("not-starred", R, **counted()))
    stage = await _held(Purpose.STAGE)
    queue = await _stand(Purpose.QUEUE)
    assert stage["read-ready"] is Hold.STAR and stage["read-first"] is None and stage["read-second"] is None
    assert {slug: s.held for slug, s in queue.items() if slug in ("read-first", "read-second", "read-ready")} == {
        "read-first": Hold.STAR, "read-second": Hold.STAR, "read-ready": Hold.STAR}, "a star holds at any of the three"
    assert {queue[slug].seat for slug in ("read-first", "read-second", "read-ready")} == {R}, "and it counts at Ready"
    assert queue["unread"].held is None and queue["not-starred"].held is None, "an unread star holds nothing"
    async with db.session_factory()() as session:
        roles = {r.slug: r for r in await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))}
        assert {slug: placement.starred(role, analysis.counts(role)) for slug, role in roles.items()} == {
            "read-first": True, "read-second": True, "read-ready": True, "unread": False, "followup": True,
            "applied": False, "rejected": False, "not-starred": False}, "before the application, read, starred"


async def test_a_star_beats_go_opened_and_a_hand_move_and_the_role_counts_once_at_ready(js):
    ids = await _make(_role("all-three", S, favorited_at=datetime.now(UTC), go_opened_at=datetime.now(UTC),
                            **counted()))
    await _by_hand(ids["all-three"], R)
    assert (await _held(Purpose.STAGE))["all-three"] is Hold.STAR
    queue = (await _stand(Purpose.QUEUE))["all-three"]
    assert (queue.held, queue.seat, queue.read) == (Hold.STAR, R, True), "one seat, at Ready, whatever else holds it"
    assert (await _held(Purpose.GATES))["all-three"] is Hold.MOVED, "the gates read the owner's hand only"


async def test_a_ready_role_go_skipped_is_held_in_the_queue_takes_no_place_and_is_not_held_by_the_stage_rule(js):
    await _make(_role("skipped", R, go_skip="no apply link", **counted()),
                _role("deck", S, go_skip="no apply link"))
    queue = await _stand(Purpose.QUEUE)
    assert (queue["skipped"].held, queue["skipped"].seat) == (Hold.SKIPPED, None)
    assert queue["deck"].held is None, "Go skips only what sits in Ready to Launch"
    assert (await _held(Purpose.STAGE)) == {"skipped": None, "deck": None}


async def test_own_data_and_a_read_under_way_hold_a_top_place_until_read_in_the_stage_and_the_queue(js):
    await _make(
        _role("tracker", S, origin=ORIGIN_TRACKER), _role("added", R, origin=ORIGIN_OWNER),
        _role("reading", S, analysis_state=analysis.WAITING), _role("asked", R, analysis_state=analysis.WAITING),
        _role("low-own", F, origin=ORIGIN_TRACKER), _role("scout", S),
        _role("own-read", S, origin=ORIGIN_TRACKER, **counted()),
        _role("reading-read", R, analysis_state=analysis.DONE, analysis={analysis.DECK_READ: True}))
    for purpose in (Purpose.STAGE, Purpose.QUEUE):
        held = await _held(purpose)
        assert held == {"tracker": Hold.OWN, "added": Hold.OWN, "reading": Hold.READING, "asked": Hold.READING,
                        "low-own": None, "scout": None, "own-read": None, "reading-read": None}, purpose
    assert Hold.OWN.weak and Hold.READING.weak and not any(h.weak for h in (Hold.STAR, Hold.OPENED, Hold.PLACED,
                                                                          Hold.SKIPPED, Hold.MOVED))
    for purpose in (Purpose.REREAD, Purpose.DROP, Purpose.LIMIT, Purpose.GATES):
        assert set((await _held(purpose)).values()) == {None}, f"{purpose}: only the stage rule and the queue wait"
    queue = await _stand(Purpose.QUEUE)
    assert {slug: s.seat for slug, s in queue.items()} == {slug: None for slug in queue}, "they take no place yet"


async def test_every_hold_at_every_status_is_honoured_where_each_purpose_says_and_nowhere_else(js):
    """The whole table, cell by cell: a role with one hold and no other at each status, asked for every purpose. So
    neither the stage rule nor the queue holds any role at Followup needed or Landing Zone, whatever it has, and no
    purpose holds a role at a status outside its cells."""
    now = datetime.now(UTC)
    single = {  # the columns that make exactly one hold true of a role
        Hold.STAR: {"favorited_at": now, **counted()}, Hold.OPENED: {"go_opened_at": now, **counted()},
        Hold.SKIPPED: {"go_skip": "no apply link", **counted()}, Hold.OWN: {"origin": ORIGIN_TRACKER},
        Hold.READING: {"analysis_state": analysis.WAITING}}
    by_hand = (Hold.PLACED, Hold.MOVED)  # made by the real writer, below

    def elsewhere(status: str) -> str:
        return R if status != R else F

    ids = await _make(*(_role(f"{hold}-{status}", elsewhere(status) if hold is Hold.PLACED else status,
                              **single.get(hold, counted() if hold in by_hand else {}))
                        for hold in (*single, *by_hand) for status in EVERY_STATUS))
    for status in EVERY_STATUS:
        await _by_hand(ids[f"{Hold.PLACED}-{status}"], status)  # where the owner put it: the status it is at
        await _by_hand(ids[f"{Hold.MOVED}-{status}"], elsewhere(status))
        await _by_code(ids[f"{Hold.MOVED}-{status}"], status)  # code moved it back: the move is spent
    for purpose, honours in HONOURED_AT.items():
        held = await _held(purpose)
        assert len(held) == 7 * len(EVERY_STATUS)
        for hold in (*single, *by_hand):
            for status in EVERY_STATUS:
                # A role the owner placed has been moved by hand too, which only the gates read.
                kinds = (Hold.PLACED, Hold.MOVED) if hold is Hold.PLACED else (hold,)
                want = next((kind for kind in kinds if status in honours.get(kind, ())), None)
                assert held[f"{hold}-{status}"] is want, (purpose, hold, status)


# --- the bars: the gap, a dial that waits, an undecided place ---------------------------------------------------------


async def test_a_role_at_an_employer_applied_to_within_the_gap_is_off_the_deck_in_the_queue_until_the_days_pass(js):
    await _make(_role("applied", stages.APPLIED, company="Globex"), _role("deck", S, company="Globex"),
                _role("other", S, company="Initech"),
                _role("opened", R, company="Globex", go_opened_at=datetime.now(UTC), **counted()),
                days_between_employer_applications=7)
    queue = await _stand(Purpose.QUEUE)
    assert queue["deck"].off_deck == GAP and queue["deck"].held is None
    assert queue["other"].off_deck is None, "another employer"
    assert (queue["opened"].held, queue["opened"].off_deck) == (Hold.OPENED, None), "a strong hold is never barred"
    later = await _stand(Purpose.QUEUE, today=owner_today() + timedelta(days=8))
    assert later["deck"].off_deck is None, "the days have passed"
    assert (await _stand(Purpose.DROP))["deck"].off_deck is None, "the gap bars no one from staying on deck"


async def test_the_gap_is_the_only_reason_when_it_applies(js):
    await _make(_role("applied", stages.APPLIED, company="Globex"),
                _role("both", S, company="Globex", gate_facts={}, location=FENWICK, **counted(place="stated")),
                gates=_with_must(), days_between_employer_applications=7)
    assert (await _stand(Purpose.QUEUE))["both"].off_deck == GAP, "it waits for a fact too, and the gap alone is said"
    assert (await _stand(Purpose.DROP))["both"].off_deck == f"{YEARS}, {PLACE}", "the drop has no gap"


async def test_a_blank_gap_bars_nothing(js):
    await _make(_role("applied", stages.APPLIED, company="Globex"), _role("deck", S, company="Globex"))
    assert {s.off_deck for s in (await _stand(Purpose.QUEUE)).values()} == {None}


def _years(value: str) -> dict:
    return {"facts": {"experience_years": {"value": value, "quote": "a made-up line", "by": "code"}}}


async def test_a_must_on_a_dial_bars_a_role_for_its_fact_in_the_queue_and_a_drop_and_none_set_reads_nothing(
        js, monkeypatch):
    await _make(_role("waits", S, gate_facts={}), _role("over", S, gate_facts=_years("9")),
                _role("fine", S, gate_facts=_years("3")), gates=_with_must())
    for purpose in (Purpose.QUEUE, Purpose.DROP):
        got = {slug: s.off_deck for slug, s in (await _stand(purpose)).items()}
        assert got == {"waits": YEARS, "over": None, "fine": None}, f"{purpose}: a failed Must is the re-gate's"
    assert {s.off_deck for s in (await _stand(Purpose.STAGE)).values()} == {None}, "the stage rule has no bars"
    await _make(gates=PROFILE["gates"])  # no Must on a dial: nothing is read for any role

    def refuse(*_):
        raise AssertionError("the gates were read though no Must on a dial is set")

    monkeypatch.setattr(gating, "results", refuse)
    for purpose in (Purpose.QUEUE, Purpose.DROP):
        assert {s.off_deck for s in (await _stand(purpose)).values()} == {None}


async def test_an_undecided_place_bars_only_a_read_role(js):
    await _make(
        _role("read", S, location=FENWICK, **counted(place="stated")), _role("unread", S, location=FENWICK),
        _role("silent", S, **counted(place="not_stated")), _role("unasked", S, **counted()),
        _role("remote", S, location=FENWICK, move_type="remote", **counted(place="stated")))
    for purpose in (Purpose.QUEUE, Purpose.DROP):
        got = {slug: s.off_deck for slug, s in (await _stand(purpose)).items()}
        assert got == {"read": f"waiting for {PLACE}", "unread": None, "silent": None, "unasked": None,
                       "remote": None}, purpose


async def test_a_role_with_a_strong_hold_is_never_barred_and_a_weak_hold_yields_to_a_bar_only_in_the_queue(
        js, monkeypatch):
    now = datetime.now(UTC)
    ids = await _make(
        _role("star", F, favorited_at=now, company="Globex", location=FENWICK, gate_facts={}, **counted()),
        _role("opened", R, go_opened_at=now, company="Globex", location=FENWICK, gate_facts={}, **counted()),
        _role("placed", F, company="Globex", location=FENWICK, gate_facts={}, **counted()),
        _role("skipped", R, go_skip="no apply link", company="Globex", location=FENWICK, gate_facts={}, **counted()),
        _role("own", S, origin=ORIGIN_TRACKER, gate_facts={}),
        _role("reading", S, analysis_state=analysis.WAITING, gate_facts={}),
        _role("applied", stages.APPLIED, company="Globex"),
        gates=_with_must(), days_between_employer_applications=7)
    await _by_hand(ids["placed"], S)
    strong = ("star", "opened", "placed", "skipped")
    drop = await _stand(Purpose.DROP)  # the drop honours a Go opening and a placing, not a star or a skip
    assert [(drop[s].held, drop[s].off_deck) for s in ("opened", "placed")] == [(Hold.OPENED, None),
                                                                                (Hold.PLACED, None)]
    queue = await _stand(Purpose.QUEUE)
    assert {slug: queue[slug].off_deck for slug in strong} == {slug: None for slug in strong}
    assert {slug: queue[slug].held for slug in strong} == {
        "star": Hold.STAR, "opened": Hold.OPENED, "placed": Hold.PLACED, "skipped": Hold.SKIPPED}
    assert [(queue[s].held, queue[s].off_deck) for s in ("own", "reading")] == [(None, YEARS)] * 2, "a bar outranks"
    stage = await _stand(Purpose.STAGE)
    assert (stage["own"].held, stage["reading"].held) == (Hold.OWN, Hold.READING), "the stage rule has no bars"

    def refuse(*_a, **_k):
        raise AssertionError("the rules were loaded for roles no bar can touch")

    monkeypatch.setattr(gating, "rules", refuse)
    async with db.session_factory()() as session:
        held = list(await session.scalars(select(Opportunity).where(Opportunity.slug.in_(("opened", "placed")))
                                          .options(defer(Opportunity.analysis), defer(Opportunity.description))))
        for purpose in (Purpose.QUEUE, Purpose.DROP):
            assert {s.off_deck for s in (await placement.load(session, OWNER, held, purpose)).values()} == {None}


async def test_the_queue_is_told_which_roles_are_free_to_move_which_stay_and_which_compete(js):
    """The verdicts the queue acts on, so it never needs to know which holds are weak: a strong hold leaves the role
    where it is (not free); a weak hold that no bar outranks stays (free, but not moved); a role off the deck is free
    to move and competes for nothing; the rest compete."""
    now = datetime.now(UTC)
    await _make(
        _role("applied", stages.APPLIED, company="Globex"), _role("star", F, favorited_at=now, **counted()),
        _role("opened", R, go_opened_at=now, **counted()), _role("skipped", R, go_skip="no apply link", **counted()),
        _role("own", S, origin=ORIGIN_TRACKER), _role("reading", S, analysis_state=analysis.WAITING),
        _role("own-gapped", S, origin=ORIGIN_TRACKER, company="Globex"), _role("gapped", S, company="Globex"),
        _role("plain", S), _role("unread", F), days_between_employer_applications=7)
    queue = await _stand(Purpose.QUEUE)
    got = {slug: (s.free, s.stays, s.competes) for slug, s in queue.items() if slug != "applied"}
    assert got == {
        "star": (False, False, False), "opened": (False, False, False), "skipped": (False, False, False),
        "own": (True, True, False), "reading": (True, True, False),
        "own-gapped": (True, False, False), "gapped": (True, False, False),
        "plain": (True, False, True), "unread": (True, False, True)}


async def test_a_role_waiting_on_a_dial_and_an_undecided_place_is_told_the_dial_by_the_queue_and_both_by_the_drop(js):
    """The queue stops at the first bar that applies and always has (the place is asked only of a role the dial does
    not already hold back); the drop at a Hard pass save names every reason it finds, and always did."""
    await _make(_role("both", S, location=FENWICK, gate_facts={}, **counted(place="stated")), gates=_with_must())
    assert (await _stand(Purpose.QUEUE))["both"].off_deck == YEARS
    assert (await _stand(Purpose.DROP))["both"].off_deck == f"{YEARS}, {PLACE}"


# --- one role, many roles, nothing written ----------------------------------------------------------------------------


async def test_one_role_asked_alone_gets_the_answer_the_whole_owner_gets(js, monkeypatch):
    now = datetime.now(UTC)
    ids = await _make(
        _role("read", R, **counted()), _role("star", F, favorited_at=now, **counted()),
        _role("open", R, go_opened_at=now), _role("plain", F), _role("hand", F), _role("own", S, origin=ORIGIN_TRACKER))
    await _by_hand(ids["hand"], S)
    for threshold in (1_000, 2):  # by id for a few roles, for the whole owner and intersected for many
        monkeypatch.setattr(placement, "OWNER_WIDE_AT", threshold)
        for purpose in Purpose:
            everyone = await _stand(purpose)
            async with db.session_factory()() as session:
                for slug, role_id in ids.items():
                    role = await session.get(Opportunity, role_id, populate_existing=True,
                                             options=[defer(Opportunity.analysis), defer(Opportunity.description)])
                    assert await placement.one(session, OWNER, role, purpose) == everyone[slug], (threshold, purpose,
                                                                                                  slug)
    monkeypatch.setattr(placement, "OWNER_WIDE_AT", 1_000)
    by_id = await _stand(Purpose.QUEUE)
    monkeypatch.setattr(placement, "OWNER_WIDE_AT", 2)
    assert await _stand(Purpose.QUEUE) == by_id, "both ways to read who has a Second Impression give one answer"
    assert {slug for slug, s in by_id.items() if s.read} == {"read", "star"}


async def test_load_writes_nothing_and_issues_the_same_statements_for_300_roles_as_for_3(js):
    """Edited with PRD 39.31 (the owner's rules are kept for as long as what they are made of is the same, so a call
    builds them once and the next calls send three statements): old, the statements were the same for any number of
    roles, with every call of `gating.rules` building the employers; new, the same once the rules are kept, so the rules
    are asked for once before the two calls that are measured (the first of them would otherwise send the build)."""
    await _make(*(_role(f"r{n}", S if n % 2 else R, **(counted() if n % 3 == 0 else {})) for n in range(300)))
    async with db.session_factory()() as session:
        await gating.rules(session, OWNER)  # built and kept here: what is counted below is `load`'s own asks

    async def statements(count: int) -> list[str]:
        seen: list[str] = []

        def noted(_conn, _cursor, statement, _parameters, _context, _many):
            seen.append(statement)

        async with db.session_factory()() as session:
            roles = list((await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER)
                                                .options(defer(Opportunity.analysis), defer(Opportunity.description))
                                                .order_by(Opportunity.slug).limit(count))).all())
            assert len(roles) == count
            engine = db.engine().sync_engine
            event.listen(engine, "before_cursor_execute", noted)
            try:
                for purpose in Purpose:
                    seen.append(f"-- {purpose}")
                    await placement.load(session, OWNER, roles, purpose)
            finally:
                event.remove(engine, "before_cursor_execute", noted)
            assert not (session.new or session.dirty or session.deleted), "nothing is changed in the session"
        return [s.split()[0].upper() if not s.startswith("--") else s for s in seen]

    few, many = await statements(3), await statements(300)
    assert few == many, "the statements do not depend on how many roles are passed"
    assert set(few) <= {"SELECT", "WITH", *(f"-- {p}" for p in Purpose)}, "nothing is written"
    plain = few[:few.index(f"-- {Purpose.QUEUE}")]  # the asks that have no bars: the owner's hand and who is read
    assert plain == [f"-- {Purpose.STAGE}", "SELECT", "WITH"], "two statements for an ask without bars"
    async with db.session_factory()() as session:
        assert await placement.load(session, OWNER, [], Purpose.STAGE) == {}, "no roles, no answer"


async def test_the_deck_read_flag_is_one_definition(js):
    await _make(_role("second", S, **counted()), _role("old-full-analysis", S, analysis_state=analysis.DONE,
                                                       analysis={"summary": "x"}),
                _role("flag-not-done", S, analysis_state=analysis.WAITING, analysis={analysis.DECK_READ: True}),
                _role("unread", S))
    async with db.session_factory()() as session:
        roles = list(await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER)))
        by_object = {r.slug for r in roles if analysis.counts(r)}
        by_ids = await analysis.reads(session, [r.id for r in roles])
        by_flag = set(await session.scalars(select(Opportunity.id).where(
            Opportunity.analysis_state == analysis.DONE, analysis.deck_read_flag().is_(True))))
        stood = await placement.load(session, OWNER, roles, Purpose.STAGE)
        assert by_object == {"second"}
        assert by_ids == by_flag == {r.id for r in roles if r.slug == "second"}
        assert {r.slug for r in roles if stood[r.id].read} == by_object


# --- a place in Ready to Launch stands on a read that graded the posting (the owner, 2026-11-03, PRD 24.13) -----------


async def _hand_line(role_id: uuid.UUID) -> None:
    """A move of the owner's on the board in the role's history, as an older release left it: the role keeps its
    status and the line stands unspent (the board's writer no longer places a role with no read at Ready)."""
    async with db.session_factory()() as session:
        await trail.record(session, OWNER, event=placement.MOVED_EVENT, actor="person", who="Owner",
                           sentence="You moved it.", link=("opportunity", role_id))
        await session.commit()


READ_STATES = {"unread": {}, "nothing-graded": counted(required=[]), "graded": counted()}


@pytest.mark.parametrize("purpose", [Purpose.STAGE, Purpose.QUEUE])
async def test_a_hold_keeps_a_role_in_ready_only_while_it_has_a_read_that_graded_its_posting(js, purpose):
    now = datetime.now(UTC)
    by = {Hold.OPENED: {"go_opened_at": now}, Hold.SKIPPED: {"go_skip": "no apply link"},
          Hold.STAR: {"favorited_at": now}, Hold.PLACED: {}}
    ids = await _make(*(_role(f"{hold}-{state}", R, **more, **read) for hold, more in by.items()
                        for state, read in READ_STATES.items()))
    for state in READ_STATES:
        await _hand_line(ids[f"{Hold.PLACED}-{state}"])
    honoured = {Hold.OPENED, Hold.STAR, Hold.PLACED} | ({Hold.SKIPPED} if purpose is Purpose.QUEUE else set())
    held = await _held(purpose)
    assert held == {f"{hold}-{state}": hold if hold in honoured and state == "graded" else None
                    for hold in by for state in READ_STATES}


async def test_a_hold_at_another_status_needs_no_graded_read_and_an_old_read_and_a_drop_leave_an_opened_role(js):
    now = datetime.now(UTC)
    ids = await _make(_role("opened-ready", R, go_opened_at=now), _role("opened-deck", S, go_opened_at=now),
                      _role("placed-deck", S), _role("star-deck", S, favorited_at=now))
    await _hand_line(ids["placed-deck"])
    for purpose in (Purpose.STAGE, Purpose.QUEUE):
        held = await _held(purpose)
        assert held["placed-deck"] is Hold.PLACED, f"{purpose}: a place on deck needs no grade"
        assert held["opened-ready"] is None and held["opened-deck"] is None
    for purpose in (Purpose.REREAD, Purpose.DROP):
        held = await _held(purpose)
        assert held["opened-ready"] is Hold.OPENED and held["opened-deck"] is Hold.OPENED, (
            f"{purpose}: asked about a role wherever it is, it reads Go's opening as it did")


async def test_standing_says_whether_a_read_graded_the_posting_whichever_way_the_reads_are_found(js, monkeypatch):
    await _make(_role("graded", S, **counted()), _role("nothing", S, **counted(required=[])), _role("unread", S),
                _role("preferred-only", S, **counted(required=[], preferred=[{"item": "x", "class": "T"}])),
                _role("duty-only", S, **counted(required=[], responsibilities=[{"item": "y", "class": "N"}])),
                _role("not-done", S, analysis_state=analysis.WAITING,
                      analysis={analysis.DECK_READ: True, "required": [{"item": "z", "class": "P"}]}))
    want = {"graded": (True, True), "nothing": (True, False), "unread": (False, False),
            "preferred-only": (True, True), "duty-only": (True, True), "not-done": (False, False)}
    for threshold in (1_000, 2):  # by id for a few roles, for the whole owner and intersected for many
        monkeypatch.setattr(placement, "OWNER_WIDE_AT", threshold)
        assert {slug: (s.read, s.graded) for slug, s in (await _stand(Purpose.STAGE)).items()} == want
    async with db.session_factory()() as session:
        roles = list(await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER)))
        assert {r.slug for r in roles if analysis.graded_read(r)} == {"graded", "preferred-only", "duty-only"}, (
            "the one definition, in Python and in SQL")
        by_ids = await analysis.reads_graded(session, [r.id for r in roles])
        assert {r.slug: graded for r in roles if (graded := by_ids.get(r.id)) is not None} == {
            "graded": True, "nothing": False, "preferred-only": True, "duty-only": True}
