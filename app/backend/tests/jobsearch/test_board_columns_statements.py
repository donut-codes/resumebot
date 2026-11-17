"""The board reads every lane's cards and facts once, not once for each lane.

`columns` read the cards and the facts of a lane's page, then the next lane's, and so on: 83 to 102 awaits for a board.
It now plans first (each lane's count and ids), then reads the cards and the facts of all the pages together. The
answer is the same. Every name here is made up."""



from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import board, stored_order
from app.jobsearch.pipeline import row as role_row
from app.models.work import TrailEvent
from app.timeutil import utc_now
from tests.builders import GATE_ZERO, counted, rank_roles
from tests.jobsearch.test_board_loads import statements
from tests.jobsearch.test_role_row import _role
from tests.support import OWNER, load_profile


async def _world() -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        now = utc_now()
        roles = [_role(f"{status}-{n}", status, **(counted() if status == stages.SECOND_IMPRESSION else {}))
                 for status in [s for _, ss in stages.LANES for s in ss] for n in range(2)]
        session.add_all(roles)
        await session.flush()
        first = [r for r in roles if r.status == stages.FIRST_IMPRESSION]
        await rank_roles(session, {first[0].id: GATE_ZERO, first[1].id: 70})
        # a role the owner moved by hand: a gate zero that stays readable
        ready = next(r for r in roles if r.status == stages.READY_TO_LAUNCH)
        await rank_roles(session, {ready.id: GATE_ZERO})
        session.add(TrailEvent(owner=OWNER, actor="person", who="You", event="role_moved", sentence="You moved it.",
                               link_kind="opportunity", link_id=ready.id, at=now))
        await stored_order.settle(session, OWNER)
        await session.commit()


async def _per_lane(session) -> list[dict]:
    """The old composition, one lane after another, kept as the reference."""
    facts = await role_row._owner_facts(session, OWNER)
    out = []
    for lane, statuses in stages.LANES:
        limit = role_row._shown(lane)
        count, ids = await role_row._lane_ids(session, OWNER, facts, lane, statuses, limit, 0, None, None)
        cards = await role_row._cards(session, ids)
        shown = [cards[i] for i in ids if i in cards]
        page = await role_row._page_facts(session, OWNER, facts, shown)
        out.append({"key": lane, "label": board.STAGE_LABELS[lane], "count": count,
                    "cards": [role_row._shape(r, page) for r in shown]})
    return out


async def test_the_board_reads_every_lane_once_and_says_what_each_lane_said(js):
    await _world()
    async with db.session_factory()() as session:
        reference = await _per_lane(session)
        await session.rollback()
    async with db.session_factory()() as session:
        with statements() as seen:
            columns = await role_row.columns(session, OWNER)
        await session.rollback()
    lanes = [c for c in columns if c["key"] != board.ADDED_LINKS]
    assert sum(1 for c in lanes if c["cards"]) >= 5
    assert lanes == reference
    assert len(seen) <= 35, chr(10).join(q[:110] for q in seen)


async def test_a_paged_lane_still_says_its_page(js):
    await _world()
    async with db.session_factory()() as session:
        got = await role_row.columns(session, OWNER, stages.FIRST_IMPRESSION, page=1, size=1)
        await session.rollback()
    assert len(got) == 1 and len(got[0]["cards"]) == 1 and got[0]["page"] == 1 and got[0]["pages"] == 2


async def _old_asks(session, ids) -> dict:
    """A test-local copy of the ask read as `row._asks` makes it: a statement of its own, the id and the integer."""
    ask = Opportunity.analysis["ask"]["ask"].as_integer()
    found = await session.execute(select(Opportunity.id, ask).where(Opportunity.id.in_(ids)))
    return {i: a for i, a in found if a is not None}


async def test_the_board_reads_the_asks_with_the_cards_and_says_what_it_said(js):
    await _world()
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity).order_by(Opportunity.slug))).all())
        for n, role in enumerate(roles):
            if n % 3 != 2:  # two in three have research, the others none
                role.analysis = {"ask": {"ask": 90000 + n * 1000}}
        await session.commit()
    async with db.session_factory()() as session:
        reference = await _per_lane(session)
        every = [r.id for r in (await session.scalars(select(Opportunity))).all()]
        old = await _old_asks(session, every)
        assert len(old) >= 5 and len(old) < len(every)
        await session.rollback()
    async with db.session_factory()() as session:
        with statements() as seen:
            columns = await role_row.columns(session, OWNER)
        await session.rollback()
    lanes = [c for c in columns if c["key"] != board.ADDED_LINKS]
    assert lanes == reference
    assert sum(1 for c in lanes for card in c["cards"] if card["ask"]) >= 5
    assert len(seen) == 41, chr(10).join(q[:110] for q in seen)
    heads = [q.split(" FROM ")[0] for q in seen if " FROM js_opportunities" in q]
    ask_only = [h for h in heads if h.startswith("SELECT js_opportunities.id, ") and "CAST(JSON_EXTRACT(" in h
                and "js_opportunities.company" not in h]
    assert not ask_only, ask_only
