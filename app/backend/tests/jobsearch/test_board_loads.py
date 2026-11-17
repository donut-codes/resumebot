"""The board loads what it shows and what puts the cards in order (docs/PRD.md 24.19).

The pipeline page spent most of the loop's own time receiving and decoding whole role rows (three JSON columns each)
to show a few hundred cards. The cards shown are loaded with the columns a card reads, and a lane
is put in order by the database from what is stored on the roles (`stored_order`, docs/PRD.md 24.19), so no role a lane
holds and does not show is read at all: `test_stored_order.py` holds that to the statements. What a card says does not
change. Every name here is made up."""

import contextlib
import re
import uuid

from sqlalchemy import event, inspect, select

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import freshness
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.pipeline import stored_order
from app.jobsearch.research import analysis
from app.timeutil import owner_today, utc_now
from tests.builders import counted, rank_roles
from tests.jobsearch.test_role_row import _FIRST, _READY, _SAYS, _SECOND, _packet, _role
from tests.support import OWNER, load_profile

#: The roles a lane must hold more of than it shows (`role_row.SHOWN`), so that cards are some of the lane.
EXTRA = role_row.SHOWN + 5
WORKED = {"answers": [], "asks": [], "possible": 5, "decided": 5, "percent": 40, "total": 2, "math": 2}


@contextlib.contextmanager
def statements():
    seen: list[str] = []

    def sent(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        seen.append(" ".join(statement.split()))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", sent)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", sent)


@contextlib.contextmanager
def column_reads():
    """The names of the role columns read from any role while the block runs."""
    names = {c.key for c in inspect(Opportunity).column_attrs}
    seen: set[str] = set()

    def spy(self, name):
        if name in names:
            seen.add(name)
        return object.__getattribute__(self, name)

    Opportunity.__getattribute__ = spy  # type: ignore[method-assign, assignment]
    try:
        yield seen
    finally:
        del Opportunity.__getattribute__


def _loaded(statement: str) -> list[str]:
    """The role columns a select loads, plain (before its first FROM): one named in an expression is not one."""
    listed = statement.split(" FROM ", 1)[0].removeprefix("SELECT ").removeprefix("DISTINCT ")
    return [m.group(1) for part in listed.split(", ") if (m := re.fullmatch(r"js_opportunities\.(\w+)", part))]


def _whole_role_reads(seen: list[str]) -> list[tuple[str, list[str]]]:
    """The statements that load a role, many columns of it (`_loaded`): (the statement, its columns)."""
    return [(q, cols) for q in seen if q.startswith("SELECT js_opportunities.") and len(cols := _loaded(q)) >= 5]


def _by_id(statement: str) -> bool:
    return "js_opportunities.id = ANY" in statement or "js_opportunities.id IN" in statement


async def _world() -> None:
    """A board with every state a card has words for (`_SAYS`), each stage twice, a lane of more roles than it shows,
    starred roles and reads scored by the asks' arithmetic (one with its own possible points, one by the target
    role's)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        made = []
        for n, (_name, status, fields, _said) in enumerate(_SAYS):
            held = {k: v for k, v in fields.items() if k not in ("rank", "packet")}
            made.append((_role(f"s{n}", status, **held), fields.get("rank"), fields.get("packet")))
        extra = [
            *(_role(f"{status}-{n}", status) for status in stages.STAGES for n in range(2)),
            *(_role(f"more-{n}", _FIRST) for n in range(EXTRA)),
            _role("star", _FIRST, favorited_at=utc_now()),
            _role("scored", _SECOND, fit=2, fit_breakdown=WORKED, **counted()),
            _role("by-target", _SECOND, fit=2, fit_breakdown={"answers": [], "asks": [], "total": 2},
                  target_role="a-target", **counted()),
            _role("read-first", _FIRST, fit=2, fit_breakdown=WORKED, **counted()),
            _role("read-ready", _READY, fit=3, fit_breakdown={**WORKED, "possible": 4}, **counted()),
        ]
        session.add_all([role for role, _, _ in made] + extra)
        await session.flush()
        by_slug = {r.slug: r for r in [role for role, _, _ in made] + extra}
        ranks = {role.id: rank for role, rank, _ in made if rank is not None}
        ranks.update({by_slug[f"more-{n}"].id: 30 + n for n in range(EXTRA)})
        await rank_roles(session, ranks)
        for role, _, packet in made:
            if packet:
                session.add(_packet(role, packet[0], **packet[1]))
        await stored_order.settle(session, OWNER)  # a board that has been read: the order is stored
        await session.commit()


async def _asked() -> dict:
    """The board whole, each lane by its key, a lane page and the favorites, as the page asks for them."""
    async with db.session_factory()() as session:
        out = {"board": await role_row.columns(session, OWNER)}
        for lane, _statuses in stages.LANES:
            out[lane] = await role_row.columns(session, OWNER, lane)
        out["page"] = await role_row.columns(session, OWNER, _FIRST, page=2, size=10)
        out["gated"] = await role_row.columns(session, OWNER, "closed", reason="gated_out")
        out["favorites"] = await role_row.favorites(session, OWNER)
        await session.rollback()
    return out


async def test_the_board_loads_no_column_a_card_does_not_read(js):
    await _world()
    with statements() as seen:
        await _asked()
    loads = _whole_role_reads(seen)
    assert len(loads) >= 12, "every lane, its cards, the roles with a read and the favorites were loaded"
    unread = {c.key for c in inspect(Opportunity).column_attrs} - {c.key for c in role_row.CARD_COLUMNS}
    assert {"gate_facts", "gate_answers", "fit_evidence", "notes", "description", "analysis"} <= unread
    for query, columns in loads:
        assert not unread & set(columns), f"{sorted(unread & set(columns))} loaded: {query[:200]}"


async def test_every_lane_is_in_the_order_the_whole_roles_are_given(js):
    """The order of a lane is made from the few columns its roles are read as, and for the roles with a Second
    Impression from the columns their score reads: the lane is where the queue (`pipeline.ordered`) puts the same roles
    loaded whole, wherever a read role stands among the ranked, and where their age puts the roles of the others."""
    await _world()
    async with db.session_factory()() as session:
        expected = {}
        for lane, statuses in stages.LANES:
            whole = list((await session.scalars(select(Opportunity).where(
                Opportunity.owner == OWNER, Opportunity.status.in_(statuses)))).all())
            if lane in (_FIRST, _SECOND, _READY):
                expected[lane] = [str(r.id) for r in await pipeline.ordered(session, OWNER, whole)]
                continue
            posted = await freshness.posted_by_role(session, OWNER, whole)
            expected[lane] = [str(r.id) for r in sorted(whole, key=lambda r: pipeline.age_key(
                r, owner_today(), utc_now(), posted.get(r.id)))]
        reads = await analysis.reads(session, [r.id for r in (await session.scalars(select(Opportunity))).all()])
        await session.rollback()
    assert len(reads) >= 4, "the roles with a Second Impression, some of them in each queue lane"
    async with db.session_factory()() as session:
        for lane, ids in expected.items():
            (column,) = await role_row.columns(session, OWNER, lane)
            assert [c["id"] for c in column["cards"]] == ids, lane
        await session.rollback()
    assert len(expected[_FIRST]) > role_row.SHOWN and all(len(ids) >= 2 for ids in expected.values())


async def test_a_whole_board_reads_the_ranks_of_the_cards_it_shows_and_not_the_owners_whole_list(js):
    """Was: the owner's whole list of ranks read once for a board, for the order of its three queue lanes. Now the order
    is stored (docs/PRD.md 24.19), so only the ranks of the cards shown are read, by their ids."""
    await _world()
    with statements() as seen:
        async with db.session_factory()() as session:
            await role_row.columns(session, OWNER)
            await session.rollback()
    ranks = [q for q in seen if "FROM js_role_ranks" in q and "max(js_role_ranks.profile_version)" in q]
    assert ranks and all("json_each" in q for q in ranks), "the ranks of the cards, by their ids"


async def test_every_card_is_the_row_the_role_loaded_whole_gives(js):
    """What a card says does not change with the narrow reads: each card of the board, of every lane by its key, of a
    page and of the favorites is the row of the same role loaded whole (`role_row.row`)."""
    await _world()
    asked = await _asked()
    columns = [column for lane in asked.values() for column in (lane if isinstance(lane, list) else [lane])]
    cards = {c["id"]: c for column in columns for c in column["cards"]}
    assert len(cards) > 60 and {c["status"] for c in cards.values()} >= set(stages.STAGES)
    alike = [c for c in cards.values() if not c["title"].startswith("Role more-")]  # the lane's many alike: three
    async with db.session_factory()() as session:
        for card in [*alike, *(c for c in cards.values() if c not in alike)][:len(alike) + 3]:
            assert card == await role_row.row(session, OWNER, uuid.UUID(card["id"])), card["title"]
        await session.rollback()


async def test_the_board_reads_no_column_it_does_not_load(js):
    """Whatever a card says reads only the columns it loads (an unloaded column is not read in the loop: it fails).
    Every state a card has words for is on this board, so this holds `_next`, the score and the age to the list."""
    await _world()
    with column_reads() as read:
        await _asked()
    loaded = {c.key for c in role_row.CARD_COLUMNS}
    assert not read - loaded, f"read but not loaded: {sorted(read - loaded)}"
    # Old: "ready_asked_at" was among the less common columns a card read, for "Being read for Ready to Launch" in its
    # note. New (docs/PRD.md 24.18): a row says nothing of what a move to Ready waits for, so no card reads it.
    assert {"title", "status", "fit_breakdown", "target_role", "go_skip", "browser_note"} <= read, (
        "and the board reached the branches that read the less common columns")
    assert "ready_asked_at" not in read | loaded, "a card does not read or load what its note no longer says"


async def test_every_lane_finds_the_postings_of_its_roles_by_the_owner_and_role_index(js):
    """Was: SQLite (no statistics) planned the postings of the roles a lane shows by the owner's match-key index and
    read every posting of the owner, once for each of the board's eight lanes.
    Now the board makes one such read over the roles of all its lanes, and its plan
    searches `ix_js_postings_owner_role` on the owner and the role."""
    await _world()
    sent: list[tuple[str, object]] = []

    def note(_conn, _cursor, statement, parameters, _context, _many) -> None:
        if "FROM js_postings" in statement and "opportunity_id IN" in statement:
            sent.append((statement, parameters))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", note)
    try:
        async with db.session_factory()() as session:
            await role_row.columns(session, OWNER)
            await session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", note)
    assert len(sent) == 1, "the board reads the dates of its roles' postings once, over the roles of every lane"
    async with db.engine().connect() as conn:
        for statement, parameters in sent:
            plan = "\n".join(r[3] for r in (await conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}",
                                                                       parameters)).all())
            assert "USING INDEX ix_js_postings_owner_role (owner=? AND opportunity_id=?)" in plan, plan
