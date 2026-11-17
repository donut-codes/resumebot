"""The current-copy reads and the size test (`cleansed.py`).

The current copy of a role used to be found by a statement that searched every raw row the owner has for each
posting's newest (the sqlite evaluation spec 2.5). A
posting now names its current copy (`JobPosting.current_cleansed_id`, kept by the writers of copies), and the tests of
those reads, their answers against the definition and the index each one uses, are in
`tests/core/test_sqlite_posting_copies.py`: they ran on Postgres, with `DISTINCT ON` and its plan, and moved with the
statements. What stays here is what a read of copies does with ids and sizes. Everything here is made up."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import String, literal, select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting
from app.jobsearch.postings import cleansed, raw_copies
from app.timeutil import owner_today
from tests.support import FEW_ARGUMENTS, OWNER, STATEMENT_ARGUMENT_LIMIT, bound_arguments

OTHER = "someone-else"

class World:
    """Roles, postings, raw rows and cleansed copies, written in the order the app writes them (a raw row's `seq` is
    the order it was added in). `copies` names each copy's id, so a test can say which one it expects."""

    def __init__(self, session) -> None:
        self.session = session
        self.roles: dict[str, Opportunity] = {}
        self.copies: dict[str, uuid.UUID] = {}
        self._n = 0

    async def role(self, slug: str, owner: str = OWNER) -> Opportunity:
        role = Opportunity(owner=owner, slug=slug, company=f"Firm {slug}", title=f"Lead {slug}", origin="scout",
                           match_key=f"firm {slug}|lead {slug}", status=stages.LANDING_ZONE, found=owner_today())
        self.session.add(role)
        await self.session.flush()
        self.roles[slug] = role
        return role

    async def posting(self, role: Opportunity | None, source: uuid.UUID, ext: str, owner: str = OWNER) -> None:
        now = datetime.now(UTC)
        self.session.add(JobPosting(
            owner=owner, employer_id=source, ats="scout", ext_id=ext, company="Firm", title="Lead", match_key=ext,
            first_seen=now, last_seen=now, links=[], opportunity_id=role.id if role else None))
        await self.session.flush()

    async def raw(self, source: uuid.UUID, ext: str, owner: str = OWNER) -> RawPosting:
        self._n += 1
        row = RawPosting(owner=owner, source_id=source, reader="scout", kind="item", ext_id=ext,
                         fetched_at=datetime.now(UTC), body=b"x", body_sha256=f"{self._n:064x}", body_bytes=1)
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
        await raw_copies.repoint(self.session, owner)  # a posting names its current copy (`raw_copies._point`)

    async def read(self, role: Opportunity | None, name: str, words: str, *, source: uuid.UUID | None = None,
                   **more) -> RawPosting:
        """A posting of the role, read once, with its copy: the shape of nearly every posting."""
        source = source or uuid.uuid4()
        ext = f"{name}-ext"
        await self.posting(role, source, ext, more.get("owner", OWNER))
        raw = await self.raw(source, ext, more.get("owner", OWNER))
        await self.copy(name, raw, words, **more)
        return raw


async def test_the_owner_has_a_current_copy_to_read_when_any_role_has_one(js):
    async with db.session_factory()() as session:
        assert not await cleansed.any_current(session, OWNER)
        w = World(session)
        await w.read(await w.role("theirs", OTHER), "theirs", "a" * 400, owner=OTHER)
        raw = await w.raw(uuid.uuid4(), "newest-has-none")
        await w.posting(await w.role("unread"), raw.source_id, "newest-has-none")
        assert not await cleansed.any_current(session, OWNER), "another owner's copy, and a posting with no copy"
        await w.read(await w.role("ready"), "ready", "b" * 400)
        assert await cleansed.any_current(session, OWNER)
        assert bool(await cleansed.current_for_roles(session, OWNER)) is True


async def test_copies_and_the_size_test_bind_a_few_arguments_for_any_number_of_ids(js):
    """Found 2026-11-05: `short_ids` and `copies` bound each id they were asked about, and asked about more
    than the 32,767 arguments a statement may carry, so the ranker queued nothing (asyncpg refused the statement). They
    answer the same for a crowd of ids as for a few: the ids with no copy are not found, and no statement binds them."""
    async with db.session_factory()() as session:
        w = World(session)
        await w.read(await w.role("thin"), "thin", "a" * 20)
        await w.read(await w.role("whole"), "whole", "a" * 2000)
        await w.read(await w.role("theirs", OTHER), "theirs", "a" * 2000, owner=OTHER)
        await session.commit()
    asked = [uuid.uuid4() for _ in range(STATEMENT_ARGUMENT_LIMIT + 1)] + list(w.copies.values())
    async with db.session_factory()() as session:
        with bound_arguments() as bound:
            short = await cleansed.short_ids(session, asked)
            held = await cleansed.copies(session, OWNER, iter(asked))
    assert short == {w.copies["thin"]}
    assert set(held) == {w.copies["thin"], w.copies["whole"]}, "another owner's copy is not this owner's"
    assert len(bound) == 2 and max(bound) <= FEW_ARGUMENTS


# --- the size test: is a copy under `MIN_TEXT` characters? ------------------------------------------------------------

#: Every shape the size test must get right, by name: ascii and four-byte characters either side of 300 characters, the
#: bytes either side of the 1,200 at which the test stops measuring (a character takes at most four bytes), empty and
#: only white space. What a copy holds is its text as it is: nothing here is trimmed.
_SIZES = {
    "empty": "", "spaces only": " " * 500, "one": "a", "299 ascii": "a" * 299, "300 ascii": "a" * 300,
    "301 ascii": "a" * 301, "299 two-byte": "é" * 299, "300 two-byte": "é" * 300,
    "299 four-byte, 1,196 bytes": "𝔘" * 299, "300 four-byte, 1,200 bytes": "𝔘" * 300,
    "301 four-byte, 1,204 bytes": "𝔘" * 301, "299 mixed, 1,190 bytes": "𝔘" * 296 + "é" * 3,
    "302 characters, 1,199 bytes": "𝔘" * 299 + "abc", "301 characters, 1,201 bytes": "𝔘" * 300 + "a",
    "1,199 ascii": "a" * 1199, "1,200 ascii": "a" * 1200, "1,201 ascii": "a" * 1201,
    "white space inside": ("a" * 100 + " \n ") * 3, "long": "word " * 4000}


async def test_the_size_test_gives_the_same_answer_as_length_for_every_shape(js):
    """`cleansed.short_ids` and the expression under it (`short_sql`) say what `length(text) < MIN_TEXT` says, stored
    and as a literal: it stops measuring only where the stored size alone settles it, and never where a text could be
    under the minimum."""
    async with db.session_factory()() as session:
        w = World(session)
        for name, words in _SIZES.items():
            await w.read(None, name, words)
        await session.commit()
    async with db.session_factory()() as session:
        short = await cleansed.short_ids(session, w.copies.values())
        by_literal = {name: await session.scalar(select(cleansed.short_sql(literal(words, type_=String))))
                      for name, words in _SIZES.items()}
    assert {name for name, copy in w.copies.items() if copy in short} == {
        name for name, words in _SIZES.items() if len(words) < cleansed.MIN_TEXT}
    assert by_literal == {name: len(words) < cleansed.MIN_TEXT for name, words in _SIZES.items()}


async def test_the_size_of_a_copy_is_asked_of_its_stored_size_before_its_text_is_unpacked(js, transactions):
    async with db.session_factory()() as session:
        w = World(session)
        await w.read(None, "whole", "a" * 2000)
        await w.read(None, "thin", "a" * 20)
        await session.commit()
    transactions.clear()
    async with db.session_factory()() as session:
        assert await cleansed.short_ids(session, w.copies.values()) == {w.copies["thin"]}
    asked = [s for s in transactions.timeline if "js_cleansed_postings" in s]
    assert asked and all("octet_length(" in s for s in asked), "length() alone unpacks every stored text"
