"""The index of the owner's roles (`same_role.known_roles`) is kept between calls and checked against the database on
every one. The key is the rows themselves (a digest of every column the index reads and of the employers' names),
so nothing a test or a restart does can return an index made from other rows. Synthetic data only."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert, select

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import same_role
from tests.support import OWNER

LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


def _role(n: int, company: str = "Fabrikam", title: str | None = None, **more) -> Opportunity:
    title = title or f"Programs Director {n}"
    return Opportunity(**{"owner": OWNER, "slug": f"{company}-{n}".lower(), "company": company, "title": title,
                          "match_key": f"{company} | {title}".lower(), "fit": 3, "origin": "scout",
                          "url": f"https://jobs.example.test/{company.lower()}/{n}", **more})


class Spy:
    """How each `known_roles` call that made the index made it: `("rebuilt")` from every role, or `("grew")` from the
    roles made since."""

    def __init__(self, monkeypatch) -> None:
        self.made: list[str] = []
        real = same_role._make

        async def make(session, owner, entry, grew):
            self.made.append("grew" if entry is not None and grew else "rebuilt")
            return await real(session, owner, entry, grew)

        monkeypatch.setattr(same_role, "_make", make)

    def take(self) -> list[str]:
        out, self.made = self.made, []
        return out


async def _like_a_fresh_one(session) -> same_role.Known[uuid.UUID]:
    """The index as `known_roles` gives it now, checked against one made from nothing: the same keys for the same
    roles (the cache is then the fresh one)."""
    got = await same_role.known_roles(session, OWNER)
    same_role.clear_cache()
    fresh = await same_role.known_roles(session, OWNER)
    assert (got._at, got._then) == (fresh._at, fresh._then), "the kept index is the one made from every role"
    return got


@pytest.fixture(autouse=True)
def _no_index_kept():
    same_role.clear_cache()
    yield
    same_role.clear_cache()


async def test_nothing_changed_returns_the_index_kept_and_reads_no_role(js, monkeypatch):
    spy = Spy(monkeypatch)
    async with db.session_factory()() as session:
        session.add_all([_role(n) for n in range(5)])
        await session.commit()
        first = await same_role.known_roles(session, OWNER)
        statements: list[str] = []

        def noted(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        engine = db.engine().sync_engine
        event.listen(engine, "before_cursor_execute", noted)
        try:
            again = await same_role.known_roles(session, OWNER)
        finally:
            event.remove(engine, "before_cursor_execute", noted)
    assert again is first and spy.take() == ["rebuilt"], "made once, then handed out again"
    assert statements and all("json_array(" in s for s in statements), (
        f"a call that finds nothing changed asks for each row's text for the digest, builds no index: {statements}")


async def test_a_role_made_since_is_added_to_the_index_kept_and_the_index_equals_one_made_again(js, monkeypatch):
    spy = Spy(monkeypatch)
    async with db.session_factory()() as session:
        session.add_all([_role(n) for n in range(3)])
        await session.commit()
        first = await _like_a_fresh_one(session)
        spy.take()
        session.add(_role(9, "Contoso", "Head of Delivery"))
        await session.commit()
        grown = await _like_a_fresh_one(session)
    assert grown is not first, "an index handed out earlier is never added to: the new one is a copy"
    assert grown.find("Contoso", "Head of Delivery") is not None and first.find("Contoso", "Head of Delivery") is None
    assert spy.take() == ["grew", "rebuilt"], "grown from the one kept, then (the check's own) made again from nothing"


@pytest.mark.parametrize("change", ["retitled", "relinked", "deleted", "alias", "employer renamed", "behind the mark"])
async def test_any_other_change_makes_the_index_again_from_every_role(js, monkeypatch, change):
    spy = Spy(monkeypatch)
    async with db.session_factory()() as session:
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        roles = [_role(n, employer_id=fab.id, created_at=LONG_AGO + timedelta(days=n)) for n in range(4)]
        session.add_all(roles)
        await session.commit()
        await _like_a_fresh_one(session)
        spy.take()
        if change == "retitled":
            roles[1].title, roles[1].match_key = "Senior Programs Director", "fabrikam | senior programs director"
        elif change == "relinked":
            roles[1].board_url = "https://boards.example.test/fabrikam/1"
        elif change == "deleted":
            await session.delete(roles[1])
        elif change == "alias":
            fab.aliases = ["Fabrikam Industries"]
        elif change == "employer renamed":
            fab.name = "Fabrikam Holdings"
        else:  # made by a transaction that began before the newest role: it lands behind the mark
            session.add(_role(8, "Contoso", "Head of Delivery", created_at=LONG_AGO + timedelta(days=1, hours=1)))
        await session.commit()
        got = await same_role.known_roles(session, OWNER)
        assert spy.take() == ["rebuilt"], f"{change}: every role is read again"
        same_role.clear_cache()
        fresh = await same_role.known_roles(session, OWNER)
        assert (got._at, got._then) == (fresh._at, fresh._then)


async def _bulk_roles(session, count: int, first: int = 0, at: datetime = LONG_AGO) -> None:
    """`count` roles made at once (one statement: a test of a few thousand roles stays fast)."""
    await session.execute(insert(Opportunity), [
        {"id": uuid.uuid4(), "owner": OWNER, "slug": f"bulk-{n}", "company": "Fabrikam",
         "title": f"Programs Director {n}", "match_key": f"fabrikam | programs director {n}", "origin": "scout",
         "created_at": at + timedelta(minutes=n)}
        for n in range(first, first + count)])
    await session.commit()


async def _statements_of(call) -> list[str]:
    statements: list[str] = []

    def noted(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        await call()
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    return statements


async def test_the_roles_are_read_in_one_statement_when_the_index_is_made_and_when_it_grows(js):
    """No index serves the order the roles are read in (oldest first, then id), so every statement that reads them is a
    scan of all the owner's roles and a sort: read a page at a time, a large account is many scans where
    one read would do. The roles, with the digest they are checked by, come in one statement; the loop is given back
    while they are put in (`_put_in`), where the CPU is."""
    async with db.session_factory()() as session:
        await _bulk_roles(session, 2100)
        cold = await _statements_of(lambda: same_role.known_roles(session, OWNER))
        await _bulk_roles(session, 2100, first=2100, at=LONG_AGO + timedelta(days=30))
        grown = await _statements_of(lambda: same_role.known_roles(session, OWNER))
    for which, statements in (("made", cold), ("grown", grown)):
        reads = [s for s in statements if "FROM js_opportunities" in s and "json_array(" in s and "ORDER BY" in s]
        assert len(reads) == 1, f"the roles are read in one statement when the index is {which}: {len(reads)}"


async def test_an_employers_other_name_finds_the_role_and_a_new_one_is_found_at_once(js):
    async with db.session_factory()() as session:
        fab = await employers.find_or_make(session, OWNER, "Fabrikam Mutual")
        session.add(_role(1, "Fabrikam Mutual", "Claims Director", employer_id=fab.id))
        await session.commit()
        known = await same_role.known_roles(session, OWNER)
        assert known.find("Fabrikam Mutual Insurance", "Claims Director") is None
        fab.aliases = ["Fabrikam Mutual Insurance"]
        await session.commit()
        known = await same_role.known_roles(session, OWNER)
        assert known.find("Fabrikam Mutual Insurance", "Claims Director") is not None


async def test_a_database_emptied_and_filled_again_never_returns_the_index_made_from_the_one_before(js):
    """Another test, a restart, a second process: the key is the rows, so the same owner with other roles (or the same
    roles under other ids) is a different key."""
    async with db.session_factory()() as session:
        session.add(_role(1, "Fabrikam", "Claims Director"))
        await session.commit()
        before = await same_role.known_roles(session, OWNER)
        assert before.find("Fabrikam", "Claims Director") is not None
        for role in (await session.scalars(select(Opportunity))).all():
            await session.delete(role)
        session.add(_role(2, "Contoso", "Head of Delivery"))
        await session.commit()
        after = await same_role.known_roles(session, OWNER)
    assert after is not before
    assert after.find("Fabrikam", "Claims Director") is None and after.find("Contoso", "Head of Delivery") is not None
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            await session.delete(role)
        await session.commit()
        assert (await same_role.known_roles(session, OWNER))._at == {}


async def test_the_owners_roles_and_no_other_owners_are_in_the_index(js):
    async with db.session_factory()() as session:
        session.add(_role(1, "Fabrikam", "Claims Director"))
        session.add(_role(2, "Contoso", "Head of Delivery", owner="someone else"))
        await session.commit()
        mine = await same_role.known_roles(session, OWNER)
        theirs = await same_role.known_roles(session, "someone else")
    assert mine.find("Contoso", "Head of Delivery") is None and mine.find("Fabrikam", "Claims Director") is not None
    assert theirs.find("Fabrikam", "Claims Director") is None and theirs.find("Contoso", "Head of Delivery") is not None


async def test_the_index_handed_out_is_shared_so_it_raises_when_added_to_and_a_copy_does_not(js):
    async with db.session_factory()() as session:
        session.add(_role(1))
        await session.commit()
        known = await same_role.known_roles(session, OWNER)
    with pytest.raises(RuntimeError, match="shared"):
        known.add("x", "Contoso", "Head of Delivery")
    with pytest.raises(RuntimeError, match="shared"):
        known.add_by_other_name("x", "Contoso", "Head of Delivery")
    mine = known.copy()
    mine.add("x", "Contoso", "Head of Delivery")
    assert mine.find("Contoso", "Head of Delivery") == "x" and known.find("Contoso", "Head of Delivery") is None


async def test_two_calls_at_once_make_the_index_once(js, monkeypatch):
    spy = Spy(monkeypatch)
    async with db.session_factory()() as session:
        session.add_all([_role(n) for n in range(5)])
        await session.commit()
    real = same_role._read_roles

    async def slow(session, owner, after):  # the first call is still reading when the second asks
        await asyncio.sleep(0.05)
        return await real(session, owner, after)

    monkeypatch.setattr(same_role, "_read_roles", slow)

    async def ask():
        async with db.session_factory()() as session:
            return await same_role.known_roles(session, OWNER)

    first, second = await asyncio.gather(ask(), ask())
    assert first is second and spy.take() == ["rebuilt"], "the second waited, looked again and found it made"


async def test_an_index_made_inside_a_transaction_that_is_rolled_back_is_not_returned_to_the_next_caller(js):
    """A caller sees its own unsaved roles, so it makes an index that holds them; the next caller's digest is of the
    rows it sees, which are not those."""
    async with db.session_factory()() as session:
        session.add(_role(1, "Fabrikam", "Claims Director"))
        await session.commit()
    async with db.session_factory()() as session:
        session.add(_role(2, "Contoso", "Head of Delivery"))
        await session.flush()
        held = await same_role.known_roles(session, OWNER)
        assert held.find("Contoso", "Head of Delivery") is not None, "its own unsaved role"
        await session.rollback()
    async with db.session_factory()() as session:
        known = await same_role.known_roles(session, OWNER)
    assert known.find("Contoso", "Head of Delivery") is None and known.find("Fabrikam", "Claims Director") is not None


async def test_making_the_index_gives_the_loop_back_between_roles(js, monkeypatch):
    """A large account is seconds without an `await`: the index is made a role at a time with the loop given back."""
    from app.jobsearch.postings import coop

    monkeypatch.setattr(same_role, "Cooperate", lambda: coop.Cooperate(every=0.0))
    async with db.session_factory()() as session:
        session.add_all([_role(n) for n in range(30)])
        await session.commit()
        ran = 0

        async def other():
            nonlocal ran
            while True:
                await asyncio.sleep(0)
                ran += 1

        task = asyncio.get_running_loop().create_task(other())
        try:
            await same_role.known_roles(session, OWNER)
        finally:
            task.cancel()
    assert ran >= 30, "another task ran between the roles"


def test_nothing_in_the_app_adds_to_what_known_roles_returns():
    """The index is shared by every caller. Each caller of `known_roles` only asks `find`: no `.add(` on its result."""
    import ast
    from pathlib import Path

    root = Path(same_role.__file__).resolve().parents[2]
    callers = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "known_roles(" not in source or path.name == "same_role.py":
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Await | ast.IfExp | ast.Call):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "known_roles(" in ast.unparse(node.value):
                    callers.append((path.name, names))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add":
                owner_name = ast.unparse(node.func.value)
                assert owner_name not in {n for file, names in callers if file == path.name for n in names}, (
                    f"{path.name} adds to the index `known_roles` returned: {ast.unparse(node)}")
    assert callers, "the callers of known_roles were found"
