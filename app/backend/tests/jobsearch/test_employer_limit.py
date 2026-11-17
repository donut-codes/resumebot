"""At most two open applications per employer (docs/PRD.md 34.2, the owner, 2026-10-26). Roles at an employer that are
Applied or in Active Discussion count; with two, the other roles there that are in the deck are held out of it, Gated
out with the reason and kept; an application that ends frees its place; a role the owner placed by hand is never held;
turning the switch off returns every held role. Everything is made up."""

import asyncio
from datetime import date

from sqlalchemy import select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import regate
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import employer_limit
from app.models.work import TrailEvent
from tests.builders import counted, rank_roles
from tests.support import OWNER, load_profile

FIRST, SECOND, LAUNCH = stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH
APPLIED, TALKS, GATED = stages.APPLIED, stages.ACTIVE_DISCUSSION, stages.GATED_OUT
GATES = "/api/v1/profile/gates"


async def _world(*specs: tuple[str, str, str]) -> dict[str, object]:
    """The made-up profile and one role per (slug, employer, status); returns their ids by slug."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        rows = {slug: Opportunity(
            owner=OWNER, slug=slug, company=company, title=f"Title {slug}", origin="scout", status=status,
            match_key=f"{company}|{slug}".lower(), found=date.today(), **counted())
            for slug, company, status in specs}
        session.add_all(rows.values())
        await session.flush()
        ids = {slug: row.id for slug, row in rows.items()}
        await session.commit()
    return ids


async def _settle() -> employer_limit.Settled:
    async with db.session_factory()() as session:
        done = await employer_limit.settle(session, OWNER)
        await session.commit()
        return done


async def _row(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _lines(role_id, event) -> list[str]:
    async with db.session_factory()() as session:
        return list(await session.scalars(select(TrailEvent.sentence).where(
            TrailEvent.link_id == role_id, TrailEvent.event == event)))


async def test_a_third_role_at_an_employer_with_two_open_applications_is_held_out_of_the_deck_and_kept(js):
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", TALKS), ("first", "Fabrikam", FIRST),
                       ("deck", "Fabrikam", SECOND), ("ready", "Fabrikam", LAUNCH))
    done = await _settle()
    assert (done.held, done.returned) == (3, 0)
    for slug in ("first", "deck", "ready"):
        row = await _row(ids[slug])
        assert (row.status, row.screened_out_by, row.screened_out_gate_id) == (GATED, "limit", "employer_limit")
        assert row.screened_out_reason == "Two open applications at this employer: Title a and Title b"
        assert await _lines(ids[slug], "limit_held"), "its history says it was held"
    assert (await _row(ids["a"])).status == APPLIED and (await _row(ids["b"])).status == TALKS


async def test_one_open_application_holds_nothing_and_other_employers_are_not_touched(js):
    ids = await _world(("a", "Fabrikam", APPLIED), ("x", "Fabrikam", FIRST),
                       ("c", "Northwind", APPLIED), ("d", "Northwind", TALKS), ("y", "Contoso", FIRST))
    assert (await _settle()).held == 0
    assert (await _row(ids["x"])).status == FIRST and (await _row(ids["y"])).status == FIRST


async def test_an_application_that_ends_frees_the_place_and_the_held_role_returns_to_first_impression(js, client):
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", APPLIED), ("held", "Fabrikam", SECOND))
    await _settle()
    assert (await _row(ids["held"])).status == GATED
    moved = await client.post(f"/api/v1/roles/{ids['a']}/move", json={"to": stages.REJECTED})
    assert moved.status_code == 200, moved.text
    back = await _row(ids["held"])
    assert (back.status, back.screened_out_by, back.screened_out_reason) == (FIRST, None, None)
    assert await _lines(ids["held"], "limit_released") == [
        "Back in First Impression: there is room at this employer again."]


async def test_moving_a_role_to_applied_holds_the_other_roles_at_that_employer_in_the_same_request(js, client):
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", LAUNCH), ("c", "Fabrikam", FIRST))
    moved = await client.post(f"/api/v1/roles/{ids['b']}/move", json={"to": APPLIED})
    assert moved.status_code == 200, moved.text
    assert (await _row(ids["c"])).status == GATED


async def test_a_role_the_owner_placed_by_hand_is_never_held(js, client):
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", APPLIED), ("mine", "Fabrikam", FIRST),
                       ("other", "Fabrikam", FIRST))
    assert (await client.post(f"/api/v1/roles/{ids['mine']}/move", json={"to": SECOND})).status_code == 200
    await _settle()
    assert (await _row(ids["mine"])).status == SECOND
    assert (await _row(ids["other"])).status == GATED


async def test_a_held_role_the_owner_moves_by_hand_stays_where_they_put_it(js, client):
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", APPLIED), ("held", "Fabrikam", SECOND))
    await _settle()
    assert (await _row(ids["held"])).status == GATED
    assert (await client.post(f"/api/v1/roles/{ids['held']}/move", json={"to": LAUNCH})).status_code == 200
    await _settle()
    assert (await _row(ids["held"])).status == LAUNCH


async def test_turning_the_switch_off_returns_every_held_role_and_turning_it_on_holds_them_again(js, client):
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", TALKS), ("h1", "Fabrikam", FIRST),
                       ("h2", "Fabrikam", LAUNCH))
    await _settle()
    off = await client.put(GATES, json={"data": {"employer_limit": False}, "why": "off"})
    assert off.status_code == 200, off.text
    assert (await _row(ids["h1"])).status == FIRST and (await _row(ids["h2"])).status == FIRST
    assert await _lines(ids["h1"], "limit_released") == ["Back in First Impression: the limit was turned off."]
    on = await client.put(GATES, json={"data": {"employer_limit": True}, "why": "on"})
    assert on.status_code == 200, on.text
    assert (await _row(ids["h1"])).status == GATED and (await _row(ids["h2"])).status == GATED


async def test_the_limit_is_on_for_an_owner_who_never_set_it(js, client):
    await _world()
    profile = (await client.get("/api/v1/profile")).json()
    assert profile["gates"]["employer_limit"] is True


async def test_a_re_gate_leaves_a_held_role_held(js):
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", APPLIED), ("held", "Fabrikam", FIRST))
    await _settle()
    async with db.session_factory()() as session:
        report = await regate.run(session, OWNER, cause="a test ran it")
        await session.commit()
    assert (await _row(ids["held"])).status == GATED and report.roles_returned == 0
    async with db.session_factory()() as session:
        assert await regate.stale(session, OWNER) == 0, "a held role is not a stop judged under an older Hard pass"


async def test_the_pipeline_holds_a_role_before_it_fills_the_deck(js):
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", APPLIED), ("x", "Fabrikam", FIRST))
    async with db.session_factory()() as session:
        await rank_roles(session, {ids["x"]: 80})
        await session.commit()
    async with db.session_factory()() as session:
        done = await pipeline.run(session, OWNER)
        await session.commit()
    assert done["held"] == 1
    assert (await _row(ids["x"])).status == GATED


async def _statements_of(body) -> list[str]:
    """The SQL the engine ran while `body()` did (the pattern of `test_regate.py`)."""
    from sqlalchemy import event

    seen: list[str] = []

    def noted(_conn, _cursor, statement, _parameters, _context, _many):
        seen.append(statement)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        await body()
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    return seen


async def test_a_turn_with_no_hold_to_make_or_change_takes_no_row_lock_and_writes_nothing(js):
    """Every pipeline turn read the roles held by the limit,
    planned a "keep" for each (the same reason it already carried), and locked them all `FOR UPDATE` to write the same
    words again, so a turn with nothing to change held a row lock and a transaction id for the length of its reads.
    A hold already in force with its reason unchanged is left alone: nothing is locked and nothing is written."""
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", TALKS), ("held", "Fabrikam", SECOND))
    assert (await _settle()).held == 1
    before = await _row(ids["held"])
    done: list[employer_limit.Settled] = []

    async def again() -> None:
        done.append(await _settle())

    statements = await _statements_of(again)
    assert (done[0].held, done[0].returned) == (0, 0)
    assert not [s for s in statements if "FOR UPDATE" in s], "nothing to change: no row is locked"
    assert not [s for s in statements if s.lstrip().upper().startswith(("UPDATE", "INSERT"))], "and nothing is written"
    assert (await _row(ids["held"])).screened_out_reason == before.screened_out_reason


async def test_the_deck_is_read_without_the_writers_turn_and_the_holds_are_those_of_a_settle_inside_a_transaction(
        js, monkeypatch):
    """On a large database the employer limit's read of the deck held the file's one writer turn for about a
    second at every turn of the pipeline. Where the session holds nothing, the deck and the owner's moves are read in a
    reader's session (a second writer gets through meanwhile), and only the roles that change are locked and written.
    In a session that has a transaction open (`board.move`'s) it reads in that transaction, as it always did, and holds
    and returns exactly the same."""
    from app.jobsearch.pipeline import placement

    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", TALKS), ("deckA", "Fabrikam", SECOND),
                       ("deckB", "Fabrikam", FIRST), ("free", "Contoso", FIRST))
    real = placement.load
    seen: list[tuple[bool, bool]] = []  # (read in a reader's session, a second writer got through)

    async def load(session, *args, **kwargs):
        got = False
        if db.is_reader(session):
            async with db.session_factory()() as other:
                try:
                    await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.id == ids["free"])
                                                         .values(slug="free")), 1)
                    got = True
                except TimeoutError:  # queued behind the settle: it still holds the writer's turn
                    pass
                await other.rollback()
        seen.append((db.is_reader(session), got))
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(placement, "load", load)
    apart = await _settle()
    assert seen == [(True, True)] and (apart.held, apart.returned) == (2, 0)
    held = {slug: (await _row(ids[slug])).screened_out_reason for slug in ("deckA", "deckB")}
    async with db.session_factory()() as session:  # the same world, put back, settled in a transaction already open
        await session.execute(update(Opportunity).where(Opportunity.slug.in_(("deckA", "deckB"))).values(
            status=FIRST, screened_out_reason=None, screened_out_by=None, screened_out_step=None,
            screened_out_gate_id=None))
        seen.clear()
        within = await employer_limit.settle(session, OWNER)
        await session.commit()
    assert seen == [(False, False)], "a transaction with a write in it is read in itself"
    assert (within.held, within.returned) == (2, 0)
    assert {slug: (await _row(ids[slug])).screened_out_reason for slug in ("deckA", "deckB")} == held


async def test_a_hold_whose_applications_changed_has_its_reason_written_again_under_a_lock(js):
    """The other side of the above: a role still held whose open applications changed carries the new reason."""
    ids = await _world(("a", "Fabrikam", APPLIED), ("b", "Fabrikam", TALKS), ("held", "Fabrikam", SECOND),
                       ("c", "Fabrikam", FIRST))
    await _settle()
    async with db.session_factory()() as session:  # b ends and c is applied to: the pair of applications is a and c
        (await session.get(Opportunity, ids["b"])).status = stages.REJECTED
        (await session.get(Opportunity, ids["c"])).status = APPLIED
        await session.commit()
    done = await _settle()
    assert (done.held, done.returned) == (0, 0), "still full: held stays held"
    assert (await _row(ids["held"])).screened_out_reason == (
        "Two open applications at this employer: Title a and Title c")
