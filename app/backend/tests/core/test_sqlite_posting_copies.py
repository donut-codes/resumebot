"""A posting's copies on SQLite: the raw rows number
themselves, and a posting names its current cleansed copy, kept by the code that writes copies, where Postgres searched
every raw row for it with `DISTINCT ON`. Like `test_sqlite_schema.py`, these make their own file in a temporary
folder and use none of the shared database fixtures. Everything here is made up.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text

from app import db
from app.jobsearch import stages
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting, RoleRank
from app.jobsearch.pipeline import deck
from app.jobsearch.postings import cleansed, raw_copies
from app.jobsearch.research import rank as ranker
from app.timeutil import owner_today
from tests.support import OWNER

OTHER = "someone-else"


# --- B2: the raw rows number themselves ----------------------------------------------------------------------------


def _raw(n: int = 0, *, source: uuid.UUID | None = None, ext: str = "e", owner: str = OWNER) -> RawPosting:
    return RawPosting(owner=owner, source_id=source or uuid.uuid4(), reader="scout", kind="item", ext_id=ext,
                      fetched_at=datetime.now(UTC), body=b"x", body_sha256=f"{n:064x}", body_bytes=1)


async def test_raw_rows_are_numbered_in_the_order_they_are_written(lite):
    async with db.session_factory()() as session:
        rows = [_raw(n) for n in range(3)]
        session.add_all(rows)
        await session.commit()
    async with db.session_factory()() as session:
        late = _raw(3)
        session.add(late)
        await session.commit()
    assert [r.seq for r in rows] == [1, 2, 3] and late.seq == 4
    assert len({r.id for r in (*rows, late)}) == 4, "each still has its own id"


async def test_writers_at_once_get_numbers_with_no_gap_and_no_repeat(lite):
    async def write(n: int) -> int:
        async with db.session_factory()() as session:
            row = _raw(n)
            session.add(row)
            await session.commit()
            return row.seq

    numbers = await asyncio.gather(*(write(n) for n in range(24)))
    assert sorted(numbers) == list(range(1, 25))


async def test_a_number_a_rolled_back_write_took_goes_back_and_a_deleted_one_is_not_used_again(lite):
    async with db.session_factory()() as session:
        first = _raw(1)
        session.add(first)
        await session.commit()
    async with db.session_factory()() as session:
        session.add(_raw(2))
        await session.flush()
        await session.rollback()
    async with db.session_factory()() as session:
        second = _raw(3)
        session.add(second)
        await session.commit()
    assert (first.seq, second.seq) == (1, 2), "no gap where a write was given up"
    async with db.session_factory()() as session:
        await session.delete(await session.get(RawPosting, 2))  # what retention does: the newest goes
        await session.commit()
    async with db.session_factory()() as session:
        third = _raw(4)
        session.add(third)
        await session.commit()
    assert third.seq == 3, "a number is never used twice"


async def test_a_copy_still_names_its_raw_row_by_id_and_goes_with_it(lite):
    async with db.session_factory()() as session:
        raw = _raw(1)
        session.add(raw)
        await session.flush()
        session.add(CleansedPosting(owner=OWNER, raw_id=raw.id, cleaner_version=1, text_clean="words"))
        await session.commit()
    async with db.session_factory()() as session:
        assert (await session.scalar(select(CleansedPosting.raw_id))) == raw.id
        await session.execute(delete(RawPosting).where(RawPosting.id == raw.id))
        await session.commit()
        assert (await session.scalar(select(CleansedPosting.id))) is None, "the copy is deleted with its raw row"


# --- C1: a posting names its current copy ---------------------------------------------------------------------------


class Hand:
    """Roles, postings, raw rows and cleansed copies written by hand, in the order the app writes them, so that a shape
    the writers never leave (a newest raw row with no copy yet, another owner's row of the same address) can be made.
    `copies` names each copy's id, so a test can say which one it expects."""

    def __init__(self, session) -> None:
        self.session = session
        self.roles: dict[str, Opportunity] = {}
        self.copies: dict[str, uuid.UUID] = {}
        self.postings: dict[str, JobPosting] = {}
        self._n = 0

    async def role(self, slug: str, owner: str = OWNER) -> Opportunity:
        role = Opportunity(owner=owner, slug=slug, company=f"Firm {slug}", title=f"Lead {slug}", origin="scout",
                           match_key=f"firm {slug}|lead {slug}", status=stages.LANDING_ZONE, found=owner_today())
        self.session.add(role)
        await self.session.flush()
        self.roles[slug] = role
        return role

    async def posting(self, role: Opportunity | None, source: uuid.UUID, ext: str, owner: str = OWNER) -> JobPosting:
        now = datetime.now(UTC)
        posting = JobPosting(owner=owner, employer_id=source, ats="scout", ext_id=ext, company="Firm", title="Lead",
                             match_key=ext, first_seen=now, last_seen=now, links=[],
                             opportunity_id=role.id if role else None)
        self.session.add(posting)
        await self.session.flush()
        self.postings[ext] = posting
        return posting

    async def raw(self, source: uuid.UUID, ext: str, owner: str = OWNER) -> RawPosting:
        self._n += 1
        row = _raw(self._n, source=source, ext=ext, owner=owner)
        self.session.add(row)
        await self.session.flush()
        return row

    async def copy(self, name: str, raw: RawPosting, words: str, *, version: int = 1, owner: str = OWNER,
                   copy_id: uuid.UUID | None = None) -> None:
        row = CleansedPosting(id=copy_id or uuid.uuid4(), owner=owner, raw_id=raw.id, cleaner_version=version,
                              text_clean=words)
        self.session.add(row)
        await self.session.flush()
        self.copies[name] = row.id

    async def read(self, role: Opportunity | None, name: str, words: str, *, source: uuid.UUID | None = None,
                   **more) -> RawPosting:
        """A posting of the role, read once, with its copy: the shape of nearly every posting."""
        source = source or uuid.uuid4()
        ext = f"{name}-ext"
        await self.posting(role, source, ext, more.get("owner", OWNER))
        raw = await self.raw(source, ext, more.get("owner", OWNER))
        await self.copy(name, raw, words, **more)
        return raw


async def every_shape(session) -> tuple[Hand, dict[str, str]]:
    """Every shape the current copy must get right, and the copy each role is expected to have (by copy name)."""
    w = Hand(session)
    a, b = uuid.uuid4(), uuid.uuid4()

    await w.read(await w.role("alone"), "alone", "a" * 400)
    longer = await w.role("longer")  # two postings: the one with the most text
    await w.read(longer, "longer-short", "b" * 300)
    await w.read(longer, "longer-long", "b" * 900)
    tie = await w.role("tie")  # equal text: the lower copy id, whichever was written first
    await w.read(tie, "tie-high", "c" * 500, copy_id=uuid.UUID(int=2))
    await w.read(tie, "tie-low", "c" * 500, copy_id=uuid.UUID(int=1))
    chars = await w.role("chars")  # most text is most characters, not most bytes
    await w.read(chars, "chars-wide", "é" * 400)
    await w.read(chars, "chars-plain", "e" * 500)

    newest = await w.role("newest")  # the newest raw row of a posting speaks, though an older copy had more text
    await w.posting(newest, a, "newest-ext")
    await w.copy("newest-old", await w.raw(a, "newest-ext"), "d" * 800)
    await w.copy("newest-new", await w.raw(a, "newest-ext"), "d" * 350)
    versions = await w.role("versions")  # the highest cleaner version of the newest raw row, though it is shorter
    await w.posting(versions, b, "versions-ext")
    raw = await w.raw(b, "versions-ext")
    await w.copy("versions-1", raw, "e" * 900, version=1)
    await w.copy("versions-2", raw, "e" * 350, version=2)

    unclean = await w.role("unclean")  # the newest raw row has no copy yet: no current copy, never an older row's
    c = uuid.uuid4()
    await w.posting(unclean, c, "unclean-ext")
    await w.copy("unclean-old", await w.raw(c, "unclean-ext"), "f" * 800)
    await w.raw(c, "unclean-ext")
    half = await w.role("half")  # one posting waits for its copy, the other has one
    e = uuid.uuid4()
    await w.posting(half, e, "half-waiting")
    await w.raw(e, "half-waiting")
    await w.read(half, "half-ready", "g" * 320)

    shared = await w.role("shared")  # another owner's newer raw row of the same address is not this owner's
    d = uuid.uuid4()
    await w.posting(shared, d, "shared-ext")
    await w.copy("shared-mine", await w.raw(d, "shared-ext"), "h" * 330)
    await w.copy("shared-theirs", await w.raw(d, "shared-ext", OTHER), "h" * 900, owner=OTHER)
    await w.read(await w.role("theirs", OTHER), "theirs", "i" * 500, owner=OTHER)
    await w.read(None, "no-role", "j" * 500)  # a posting that is not a role

    expected = {"alone": "alone", "longer": "longer-long", "tie": "tie-low", "chars": "chars-plain",
                "newest": "newest-new", "versions": "versions-2", "half": "half-ready", "shared": "shared-mine"}
    return w, expected


#: The definition the pointer must give the same answer as, written with no pointer: a posting's newest raw row (by its
#: address and `seq`), that row's copy of the highest cleaner version, and of a role's postings that have one the copy
#: with the most characters, the lower copy id on a tie. It is the statement Postgres ran with `DISTINCT ON`.
_COPY_OF_EACH_POSTING = text("""
SELECT p.opportunity_id, (
    SELECT k.id FROM js_cleansed_postings k
    WHERE k.raw_id = (SELECT r.id FROM js_raw_postings r
                      WHERE r.owner = p.owner AND r.source_id = p.employer_id AND r.ext_id = p.ext_id
                      ORDER BY r.seq DESC LIMIT 1)
    ORDER BY k.cleaner_version DESC LIMIT 1) AS cleansed_id
FROM js_postings p WHERE p.owner = :owner AND p.opportunity_id IS NOT NULL""")


async def defined_current(session, owner: str = OWNER) -> dict[uuid.UUID, uuid.UUID]:
    """The current copy of each role by the definition."""
    sizes = {uuid.UUID(hex=copy_id): size for copy_id, size in (await session.execute(
        text("SELECT id, length(text_clean) FROM js_cleansed_postings"))).all()}
    by_role: dict[uuid.UUID, list[uuid.UUID]] = {}
    for role_id, copy_id in (await session.execute(_COPY_OF_EACH_POSTING, {"owner": owner})).all():
        if copy_id is not None:
            by_role.setdefault(uuid.UUID(hex=role_id), []).append(uuid.UUID(hex=copy_id))
    return {role_id: min(copies, key=lambda c: (-sizes[c], c.hex)) for role_id, copies in by_role.items()}


async def test_every_role_has_the_current_copy_the_definition_gives(lite):
    async with db.session_factory()() as session:
        w, expected = await every_shape(session)
        assert await raw_copies.repoint(session, OWNER) > 0
        await session.commit()
    async with db.session_factory()() as session:
        found = await cleansed.current_for_roles(session, OWNER)
        defined = await defined_current(session)
    wanted = {w.roles[slug].id: w.copies[name] for slug, name in expected.items()}
    assert found == wanted
    assert defined == wanted, "the test's own definition says the same"


async def test_a_role_has_its_copy_from_one_statement_or_from_each_read_of_it(lite):
    async with db.session_factory()() as session:
        w, expected = await every_shape(session)
        await raw_copies.repoint(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        for slug, role in w.roles.items():
            copy = await cleansed.copy_of(session, OWNER, role.id)
            assert (copy.id if copy else None) == (w.copies[expected[slug]] if slug in expected else None), slug
        held = await cleansed.copies_by_role(session, OWNER)
    assert {role_id: copy.id for role_id, copy in held.items()} == {
        w.roles[slug].id: w.copies[name] for slug, name in expected.items()}


async def test_the_gates_read_the_same_copy_by_each_of_their_three_reads(lite):
    """`gate_current_for_roles` (every role), `gate_copy_ids_for` (several) and `gate_copy_of` (one) choose alike, and
    for a role with one posting it is that posting's copy. None of these postings is the employer's own and all were
    first seen on one day, so the rule falls to the most text, as the definition does."""
    async with db.session_factory()() as session:
        w, expected = await every_shape(session)
        await raw_copies.repoint(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        everyone = await cleansed.gate_current_for_roles(session, OWNER)
        some = await cleansed.gate_copy_ids_for(session, OWNER, [r.id for r in w.roles.values()])
        one = {role.id: (await cleansed.gate_copy_of(session, OWNER, role.id)) for role in w.roles.values()}
        copies = await cleansed.gate_copies_by_role(session, OWNER)
        several = await cleansed.gate_copies_for(session, OWNER, [r.id for r in w.roles.values()])
    assert everyone == some
    assert {role_id: copy.id for role_id, copy in one.items() if copy} == everyone
    assert {role_id: copy.id for role_id, copy in copies.items()} == everyone
    assert {role_id: copy.id for role_id, copy in several.items()} == everyone
    wanted = {w.roles[slug].id: w.copies[name] for slug, name in expected.items() if slug != "tie"}
    assert {r: c for r, c in everyone.items() if r != w.roles["tie"].id} == wanted
    tie = sorted(("tie-low", "tie-high"), key=lambda name: w.postings[f"{name}-ext"].id)[0]
    assert everyone[w.roles["tie"].id] == w.copies[tie], "where the text ties it is the lower posting id"


async def test_a_pointer_to_a_copy_that_is_gone_is_no_copy(lite):
    async with db.session_factory()() as session:
        w = Hand(session)
        await w.read(await w.role("here"), "here", "a" * 400)
        await w.read(await w.role("gone"), "gone", "b" * 400)
        await raw_copies.repoint(session, OWNER)
        await session.commit()
    async with lite.begin() as conn:
        await conn.execute(text("DELETE FROM js_cleansed_postings WHERE id = :id"), {"id": w.copies["gone"].hex})
    async with db.session_factory()() as session:
        assert await cleansed.current_for_roles(session, OWNER) == {w.roles["here"].id: w.copies["here"]}
        assert await cleansed.copy_of(session, OWNER, w.roles["gone"].id) is None
        assert await cleansed.gate_current_for_roles(session, OWNER) == {w.roles["here"].id: w.copies["here"]}


async def test_the_owner_has_a_current_copy_to_read_when_any_role_has_one(lite):
    async with db.session_factory()() as session:
        assert not await cleansed.any_current(session, OWNER)
        w = Hand(session)
        await w.read(await w.role("theirs", OTHER), "theirs", "a" * 400, owner=OTHER)
        raw = await w.raw(uuid.uuid4(), "newest-has-none")
        await w.posting(await w.role("unread"), raw.source_id, "newest-has-none")
        await raw_copies.repoint(session, OWNER)
        assert not await cleansed.any_current(session, OWNER), "another owner's copy, and a posting with no copy"
        await w.read(await w.role("ready"), "ready", "b" * 400)
        await raw_copies.repoint(session, OWNER)
        assert await cleansed.any_current(session, OWNER)
        assert bool(await cleansed.current_for_roles(session, OWNER)) is True


async def test_repoint_changes_only_the_postings_whose_pointer_is_not_what_the_rows_say(lite):
    async with db.session_factory()() as session:
        w, _expected = await every_shape(session)
        first = await raw_copies.repoint(session, OWNER)
        again = await raw_copies.repoint(session, OWNER)
        await session.commit()
    assert first > 0 and again == 0
    async with db.session_factory()() as session:
        mine = (await session.scalars(select(JobPosting.current_cleansed_id).where(
            JobPosting.owner == OWNER, JobPosting.ext_id == "unclean-ext"))).all()
        assert mine == [None], "the newest raw row has no copy, so the posting has none"
    async with lite.begin() as conn:
        await conn.execute(text("UPDATE js_postings SET current_cleansed_id = NULL"))
    async with db.session_factory()() as session:
        assert await raw_copies.repoint(session, OWNER) == first


# --- the writers keep the pointer ------------------------------------------------------------------------------------


async def _pointer(session, posting: JobPosting) -> uuid.UUID | None:
    return await session.scalar(select(JobPosting.current_cleansed_id).where(JobPosting.id == posting.id))


async def _kept(session, posting: JobPosting, words: str, **more) -> int:
    return await raw_copies._keep_report(session, posting, {"text": words}, reader="scout", text=words, **more)


async def test_a_copy_made_points_its_posting_at_itself_and_a_read_that_makes_none_leaves_it(lite):
    async with db.session_factory()() as session:
        w = Hand(session)
        posting = await w.posting(await w.role("kept"), uuid.uuid4(), "kept-ext")
        assert await _pointer(session, posting) is None
        assert await _kept(session, posting, "a" * 400) == 1
        first = await _pointer(session, posting)
        assert first is not None
        assert await _kept(session, posting, "a" * 400) == 0, "the same bytes again write nothing"
        assert await _pointer(session, posting) == first
        assert await _kept(session, posting, "b" * 500) == 1
        second = await _pointer(session, posting)
        assert second not in (None, first), "a changed posting has a new copy, and the posting names it"
        await session.commit()
    async with db.session_factory()() as session:
        assert await defined_current(session) == {w.roles["kept"].id: second}
        assert await cleansed.current_for_roles(session, OWNER) == {w.roles["kept"].id: second}


async def test_each_writer_of_copies_points_every_posting_it_made_one_for(lite, monkeypatch):
    """The detail answer (`keep_detail`'s path), a stored text (`keep_legacy`) and the cleaner run again (`recleanse`),
    with the postings of two sources."""
    async with db.session_factory()() as session:
        w = Hand(session)
        roles = [await w.role(f"r{n}") for n in range(3)]
        postings = [await w.posting(role, uuid.uuid4(), f"p{n}") for n, role in enumerate(roles)]
        for posting in postings:
            posting.text = "stored text " * 40
        await session.flush()
        assert await raw_copies.keep_legacy(session, OWNER) == 3
        legacy = [await _pointer(session, p) for p in postings]
        assert None not in legacy and len(set(legacy)) == 3
        await _kept(session, postings[0], "c" * 400)
        await session.commit()
        moved = await _pointer(session, postings[0])
        assert moved not in (None, legacy[0])
        # the cleaner run again at a higher version makes every posting's copy new
        monkeypatch.setattr(raw_copies.cleanse, "CLEANER_VERSION", raw_copies.cleanse.CLEANER_VERSION + 1)
        done = await raw_copies.recleanse(session, OWNER)
        assert done["rebuilt"] == 3
        again = [await _pointer(session, p) for p in postings]
        assert None not in again and set(again).isdisjoint({*legacy, moved})
        await session.commit()
    async with db.session_factory()() as session:
        assert await defined_current(session) == {r.id: c for r, c in zip(roles, again, strict=True)}


async def test_a_copy_made_for_the_newest_row_wins_over_an_older_posting_copy_of_more_text(lite):
    async with db.session_factory()() as session:
        w = Hand(session)
        role = await w.role("two")
        one = await w.posting(role, uuid.uuid4(), "one")
        other = await w.posting(role, uuid.uuid4(), "other")
        await _kept(session, one, "x" * 900)
        await _kept(session, other, "y" * 500)
        await _kept(session, one, "x" * 350)  # the posting's newest copy is the shorter now
        await session.commit()
    async with db.session_factory()() as session:
        found = await cleansed.current_for_roles(session, OWNER)
        assert found == {role.id: await _pointer(session, other)}, "500 characters beat 350"
        assert found == await defined_current(session)


# --- the plan --------------------------------------------------------------------------------------------------------


async def _plan(session, statement) -> str:
    compiled = statement.compile(dialect=db.engine().dialect, compile_kwargs={"literal_binds": True})
    rows = (await session.execute(text("EXPLAIN QUERY PLAN " + str(compiled)))).all()
    return "\n".join(row[3] for row in rows)


@pytest.mark.parametrize("name, statement", [
    ("every role", lambda: cleansed._current(OWNER)),
    ("any role", lambda: cleansed._any_current(OWNER)),
    ("gates, every role", lambda: cleansed._gate_choices(OWNER, thin=True)),
])
async def test_the_reads_of_every_role_use_the_posting_index_and_never_the_raw_rows(lite, name, statement):
    async with db.session_factory()() as session:
        plan = await _plan(session, statement())
    assert "COVERING INDEX ix_js_postings_owner_role_copy" in plan, "the rows of the table are opened: " + plan
    assert "js_raw_postings" not in plan, plan


async def test_the_read_of_one_role_uses_the_roles_own_postings(lite):
    async with db.session_factory()() as session:
        plan = await _plan(session, cleansed._current_of(OWNER, uuid.uuid4()))
    assert "ix_js_postings_opportunity_id" in plan or "ix_js_postings_owner_role_copy" in plan, plan
    assert "js_raw_postings" not in plan, plan


@pytest.mark.parametrize("statement", [cleansed._current(OWNER), cleansed._gate_choices(OWNER, thin=True)])
async def test_the_text_of_a_copy_is_measured_only_where_a_role_has_a_choice_of_postings(lite, statement):
    """Measuring unpacks the stored text of every copy it is asked of: it sits inside the `CASE` that asks whether the
    role has several postings that have a copy."""
    sql = str(statement.compile(dialect=db.engine().dialect, compile_kwargs={"literal_binds": True}))
    measures = [at for at in range(len(sql)) if sql.startswith("length(", at)]
    assert measures
    assert all("CASE WHEN (found.postings > 1) THEN (SELECT " in sql[max(0, at - 60):at] for at in measures), sql


# --- the newest rank of a role (deck.py: a max(profile_version) join in place of DISTINCT ON) ------------------------


async def test_a_roles_newest_rank_is_the_latest_one_for_the_newest_profile(lite):
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        w = Hand(session)
        mixed, steady, zero = await w.role("mixed"), await w.role("steady"), await w.role("zero")
        await w.read(mixed, "m1", "a" * 400)
        await w.read(mixed, "m2", "b" * 400)
        await w.read(steady, "s1", "c" * 400)
        await w.read(zero, "z1", "d" * 400)
        session.add_all([
            # a newer profile outranks a later rank of an older one; of two ranks under it the later wins
            RoleRank(owner=OWNER, role_id=mixed.id, cleansed_id=w.copies["m1"], profile_version=1, fit=90,
                     created_at=now),
            RoleRank(owner=OWNER, role_id=mixed.id, cleansed_id=w.copies["m1"], profile_version=2, fit=20,
                     created_at=now - timedelta(hours=3)),
            RoleRank(owner=OWNER, role_id=mixed.id, cleansed_id=w.copies["m2"], profile_version=2, fit=35,
                     created_at=now - timedelta(hours=1)),
            RoleRank(owner=OWNER, role_id=steady.id, cleansed_id=w.copies["s1"], profile_version=3, fit=61,
                     created_at=now),
            RoleRank(owner=OWNER, role_id=zero.id, cleansed_id=w.copies["z1"], profile_version=1, fit=5,
                     created_at=now - timedelta(days=1)),
            RoleRank(owner=OWNER, role_id=zero.id, cleansed_id=w.copies["z1"], profile_version=2, fit=0,
                     why=f"{ranker.GATE_MARK} the place.\nA second line.", created_at=now - timedelta(days=2)),
        ])
        await session.commit()
    async with db.session_factory()() as session:
        assert await deck.ranks(session, OWNER) == {mixed.id: 35, steady.id: 61, zero.id: 0}
        ranked = await deck.ranked(session, OWNER, [mixed.id, zero.id])
        assert ranked.estimates == {mixed.id: 35, zero.id: 0}
        assert ranked.gate_zeros == {zero.id}, "a gate zero of an ordinary role"
        reasons = await deck.gate_reasons(session, OWNER, [mixed.id, steady.id, zero.id])
        assert list(reasons) == [zero.id] and reasons[zero.id].startswith(ranker.GATE_MARK)
