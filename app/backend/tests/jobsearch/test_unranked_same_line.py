"""`rank.unranked` asks the database for the roles waiting to be ranked, and its line is the one it always was.

To pick at most 25 roles it read the current copy of every role,
every role in play and every rank at the profile version into Python, then
filtered and sorted there. It now reads the current copies of the roles in play alone and leaves out, in the statement,
the roles already ranked at the version on text that was not cut. Which roles wait, with which copy and in what order
did not change: the body it had is kept here (`_as_it_was`) and the two are held to each other over every case the
definition names. Every name here is made up, and nothing is timed."""

import contextlib
import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import event, select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, ParseJob, RawPosting, RoleRank
from app.jobsearch.postings import cleansed, raw_copies
from app.jobsearch.research import rank
from app.jobsearch.states import ParseKind, ParseState
from tests.support import OWNER

OTHER = "someone-else"
#: The version of the short profile in force here; ranks are made at the one before and the one after too.
V = 3
NOW = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
DAY = NOW.date()
#: What dates a role: its posted day, the day it was found and when it was made, given to the roles in turn. Several
#: roles share each, so the line's last word (the role's id) decides among them, and a day found or made stands in
#: where no day was posted.
DATES: tuple[tuple[date | None, date | None, datetime], ...] = (
    (DAY - timedelta(days=3), DAY, NOW),
    (None, DAY - timedelta(days=3), NOW),
    (None, None, NOW - timedelta(days=3)),
    (None, None, NOW - timedelta(days=3, hours=2)),
    (DAY - timedelta(days=1), DAY - timedelta(days=9), NOW - timedelta(days=9)),
    (None, DAY - timedelta(days=1), NOW - timedelta(hours=5)),
    (DAY, None, NOW - timedelta(days=30)),
)


async def _as_it_was(session, owner: str, version: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """`rank.unranked` as it was before it asked the database to narrow (app 1.183.1), with the read of the open rank
    jobs it called (`rank._open_roles`): every current copy, every role in play and every rank at the version, then
    filtered and sorted in Python. The reference the statement is held to; nothing in the app calls it."""
    copies = await cleansed.current_for_roles(session, owner)
    if not copies:
        return []
    made = {i: (posted or found or created.date(), created) for i, posted, found, created in (await session.execute(
        select(Opportunity.id, Opportunity.posted, Opportunity.found, Opportunity.created_at).where(
            Opportunity.owner == owner, Opportunity.status.in_(stages.PRE_READY)))).all()}
    copies = {r: c for r, c in copies.items() if r in made}
    if not copies:
        return []
    done = {(r, c) for r, c in (await session.execute(select(RoleRank.role_id, RoleRank.cleansed_id).where(
        RoleRank.owner == owner, RoleRank.profile_version == version, RoleRank.text_cut.is_(False)))).all()}
    held: set[str] = set()
    for fields in (await session.scalars(select(ParseJob.fields).where(
            ParseJob.owner == owner, ParseJob.kind == ParseKind.RANK,
            ParseJob.state.in_((ParseState.WAITING, ParseState.READING))))).all():
        held.update(str(i["role_id"]) for i in (fields or {}).get("items", []))
    short = await cleansed.short_ids(session, copies.values())
    waiting = [(r, c) for r, c in copies.items() if (r, c) not in done and str(r) not in held and c not in short]
    return sorted(waiting, key=lambda rc: (made[rc[0]], str(rc[0])), reverse=True)


class Line:
    """Roles, their postings and copies, ranks and rank jobs, written as the app writes them (a posting names its
    current copy: `raw_copies.repoint`). `roles` and `copies` name each id, so a test can say which it expects; a
    posting's copy has the role's name unless the test names it."""

    def __init__(self, session) -> None:
        self.session = session
        self.roles: dict[str, uuid.UUID] = {}
        self.copies: dict[str, uuid.UUID] = {}
        self._owners: dict[str, str] = {}
        self._dates = itertools.cycle(DATES)
        self._n = 0

    async def role(self, name: str, status: str = stages.FIRST_IMPRESSION, *, owner: str = OWNER) -> None:
        posted, found, created = next(self._dates)
        role = Opportunity(owner=owner, slug=name, company=f"Firm {name}", title=f"Lead {name}", origin="scout",
                           match_key=f"firm {name}|lead {name}", status=status, posted=posted, found=found,
                           created_at=created)
        self.session.add(role)
        await self.session.flush()
        self.roles[name], self._owners[name] = role.id, owner

    async def posting(self, role: str, chars: int | None = 400, *, name: str | None = None, reads: int = 1) -> None:
        """A posting of the role, read `reads` times, each read with a copy of `chars` characters (the last is its
        current copy, named `name`; the earlier ones `<name>-read-<n>`). `chars=None`: a posting no one has read."""
        name, owner, source = name or role, self._owners[role], uuid.uuid4()
        self.session.add(JobPosting(
            owner=owner, employer_id=source, ats="scout", ext_id=name, company="Firm", title="Lead", match_key=name,
            first_seen=NOW, last_seen=NOW, links=[], opportunity_id=self.roles[role]))
        for n in range(reads if chars is not None else 0):
            self._n += 1
            raw = RawPosting(owner=owner, source_id=source, reader="scout", kind="item", ext_id=name, fetched_at=NOW,
                             body=b"x", body_sha256=f"{self._n:064x}", body_bytes=1)
            self.session.add(raw)
            await self.session.flush()
            copy = CleansedPosting(owner=owner, raw_id=raw.id, cleaner_version=1, text_clean="a" * chars)
            self.session.add(copy)
            await self.session.flush()
            self.copies[name if n == reads - 1 else f"{name}-read-{n + 1}"] = copy.id

    async def waiting_role(self, name: str, status: str = stages.FIRST_IMPRESSION, *, owner: str = OWNER) -> None:
        """A role with one posting that has a copy of real length: it waits for a rank unless something else says no."""
        await self.role(name, status, owner=owner)
        await self.posting(name)

    def rank(self, role: str, version: int = V, *, copy: str | None = None, cut: bool = False,
             owner: str | None = None) -> None:
        self.session.add(RoleRank(owner=owner or self._owners[role], role_id=self.roles[role],
                                  cleansed_id=self.copies[copy or role], profile_version=version, fit=50,
                                  why="A lead role.\nIt fits on duties.", text_cut=cut))

    def job(self, *roles: str, state: ParseState = ParseState.WAITING, owner: str = OWNER) -> None:
        items = [{"n": n, "role_id": str(self.roles[r]), "cleansed_id": str(self.copies[r])}
                 for n, r in enumerate(roles, 1)]
        self.session.add(ParseJob(owner=owner, kind=ParseKind.RANK, role_id=f"rank:{V}", text="", state=state,
                                  fields={"profile_version": V, "items": items}))

    async def written(self) -> None:
        await self.session.flush()
        for owner in (OWNER, OTHER):
            await raw_copies.repoint(self.session, owner)
        await self.session.commit()


async def _mixed(line: Line) -> dict[str, str]:
    """Every case of the definition, several roles to each date: returns the roles that wait for a rank at `V`, each
    with the name of the copy it is ranked on."""
    waits: dict[str, str] = {}
    for status in stages.PRE_READY:  # in play, never ranked
        await line.waiting_role(f"at-{status}", status)
        waits[f"at-{status}"] = f"at-{status}"
    for status in (s for s in stages.STAGES if s not in stages.PRE_READY):  # out of play, with and without a rank
        await line.waiting_role(f"away-{status}", status)
        await line.waiting_role(f"away-{status}-ranked", status)
        line.rank(f"away-{status}-ranked")

    await line.waiting_role("ranked")
    line.rank("ranked")
    await line.waiting_role("ranked-on-cut-text")
    line.rank("ranked-on-cut-text", cut=True)
    waits["ranked-on-cut-text"] = "ranked-on-cut-text"
    await line.waiting_role("ranked-at-the-version-before")
    line.rank("ranked-at-the-version-before", V - 1)
    waits["ranked-at-the-version-before"] = "ranked-at-the-version-before"
    await line.waiting_role("ranked-at-the-version-after")
    line.rank("ranked-at-the-version-after", V + 1)
    waits["ranked-at-the-version-after"] = "ranked-at-the-version-after"
    await line.waiting_role("ranked-at-every-version")
    for version in (V - 1, V, V + 1):
        line.rank("ranked-at-every-version", version)
    await line.waiting_role("cut-before-whole-now")
    line.rank("cut-before-whole-now", V - 1, cut=True)
    line.rank("cut-before-whole-now", V)
    await line.waiting_role("ranked-under-another-owner")
    line.rank("ranked-under-another-owner", owner=OTHER)
    waits["ranked-under-another-owner"] = "ranked-under-another-owner"
    await line.role("ranked-on-an-earlier-read")
    await line.posting("ranked-on-an-earlier-read", reads=2)
    line.rank("ranked-on-an-earlier-read", copy="ranked-on-an-earlier-read-read-1")
    waits["ranked-on-an-earlier-read"] = "ranked-on-an-earlier-read"

    await line.role("short")
    await line.posting("short", cleansed.MIN_TEXT - 1)
    await line.role("just-long-enough")
    await line.posting("just-long-enough", cleansed.MIN_TEXT)
    waits["just-long-enough"] = "just-long-enough"
    await line.role("too-long-for-a-batch")
    await line.posting("too-long-for-a-batch", rank.BATCH_CHARS + 1)
    waits["too-long-for-a-batch"] = "too-long-for-a-batch"
    await line.role("no-posting")
    await line.role("never-read")
    await line.posting("never-read", None)

    for state in ParseState:
        await line.waiting_role(f"held-{state}")
        line.job(f"held-{state}", state=state)
        if state not in (ParseState.WAITING, ParseState.READING):
            waits[f"held-{state}"] = f"held-{state}"
    await line.waiting_role("held-with-another")
    await line.waiting_role("held-with-another-2")
    line.job("held-with-another", "held-with-another-2")
    await line.waiting_role("held-by-another-owner")
    line.job("held-by-another-owner", owner=OTHER)
    waits["held-by-another-owner"] = "held-by-another-owner"

    # A role on two boards: the copy with the most text is the role's, and a rank counts only on that one.
    for name, ranked_on in (("two", None), ("two-ranked-on-the-lesser", "lesser"), ("two-ranked-on-the-most", "most")):
        await line.role(name)
        await line.posting(name, 400, name=f"{name}-lesser")
        await line.posting(name, 900, name=f"{name}-most")
        if ranked_on:
            line.rank(name, copy=f"{name}-{ranked_on}")
        if ranked_on != "most":
            waits[name] = f"{name}-most"
    await line.role("two-one-short")
    await line.posting("two-one-short", 900, name="two-one-short-whole")
    await line.posting("two-one-short", 100, name="two-one-short-thin")
    waits["two-one-short"] = "two-one-short-whole"
    await line.role("two-both-short")
    await line.posting("two-both-short", 100, name="two-both-short-a")
    await line.posting("two-both-short", 200, name="two-both-short-b")
    await line.role("two-one-never-read")
    await line.posting("two-one-never-read", None, name="two-one-never-read-unread")
    await line.posting("two-one-never-read", name="two-one-never-read-read")
    waits["two-one-never-read"] = "two-one-never-read-read"
    await line.role("two-alike")
    await line.posting("two-alike", 500, name="two-alike-a")
    await line.posting("two-alike", 500, name="two-alike-b")
    waits["two-alike"] = min(("two-alike-a", "two-alike-b"), key=lambda n: str(line.copies[n]))

    # Another owner's roles are theirs: in play, one waiting and one ranked.
    await line.waiting_role("theirs", owner=OTHER)
    await line.waiting_role("theirs-ranked", owner=OTHER)
    line.rank("theirs-ranked")
    await line.written()
    return waits


async def test_the_roles_that_wait_for_a_rank_are_the_same_roles_in_the_same_order_as_before(js):
    async with db.read_session_factory()() as session:
        assert await rank.unranked(session, OWNER, V) == await _as_it_was(session, OWNER, V) == [], "no role yet"
    async with db.session_factory()() as session:
        line = Line(session)
        waits = await _mixed(line)
    for factory in (db.read_session_factory(), db.session_factory()):  # the pool's pull reads apart; `queue` does not
        async with factory() as session:
            now = await rank.unranked(session, OWNER, V)
            assert now == await _as_it_was(session, OWNER, V), "the same roles, each with the same copy, in one order"
            assert set(now) == {(line.roles[r], line.copies[c]) for r, c in waits.items()} and len(now) == len(waits)
            for owner, version in itertools.product((OWNER, OTHER), (V - 1, V, V + 1, V + 2)):
                found = await rank.unranked(session, owner, version)
                assert found == await _as_it_was(session, owner, version), (owner, version)
                assert found, "every owner has a role waiting at every version here"
            theirs = await rank.unranked(session, OTHER, V)
            assert theirs == [(line.roles["theirs"], line.copies["theirs"])]


@contextlib.contextmanager
def rows_fetched(session) -> Iterator[list[tuple]]:
    """Every row each statement `session` runs in the block gave back, counted where the session runs a statement (its
    `do_orm_execute` hook reads the answer to its end and hands the same rows on), never by a clock."""
    seen: list[tuple] = []

    def fetched(state):
        kept = state.invoke_statement().freeze()
        seen.extend(tuple(row) for row in kept().all())
        return kept()

    event.listen(session.sync_session, "do_orm_execute", fetched)
    try:
        yield seen
    finally:
        event.remove(session.sync_session, "do_orm_execute", fetched)


async def test_the_ranker_is_given_rows_only_for_roles_in_play_that_hold_no_rank(js):
    """What was slow: the copy of every role the owner has, every role in play and every rank at the
    version were all fetched to find the few that wait. The rows the statements give back now name
    no role that is out of play and none that holds its rank, however many of those there are."""
    async with db.session_factory()() as session:
        line = Line(session)
        for name in ("new-1", "new-2", "new-3"):
            await line.waiting_role(name)
        await line.role("thin")
        await line.posting("thin", cleansed.MIN_TEXT - 1)
        for n in range(4):
            await line.waiting_role(f"ranked-{n}")
            line.rank(f"ranked-{n}")
        for n, status in enumerate((stages.GATED_OUT, stages.APPLIED, stages.REJECTED) * 4):
            await line.waiting_role(f"away-{n}", status)
            if n % 2:
                line.rank(f"away-{n}")
        await line.waiting_role("theirs", owner=OTHER)
        await line.written()
    waiting = {line.roles[name] for name in ("new-1", "new-2", "new-3")}
    not_ranked_in_play = waiting | {line.roles["thin"]}
    async with db.read_session_factory()() as session:
        with rows_fetched(session) as rows:
            found = await rank.unranked(session, OWNER, V)
    assert {role for role, _ in found} == waiting
    named = {value for row in rows for value in row if isinstance(value, uuid.UUID)} & set(line.roles.values())
    assert waiting <= named <= not_ranked_in_play, "a row was fetched for a role out of play, or one that has its rank"
    assert len(rows) <= 2 * len(not_ranked_in_play), "a row for the role, and at most one more for its copy's size"
