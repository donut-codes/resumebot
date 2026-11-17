"""Posting a to-do, and the tripwire's book-keeping.

A to-do is a row in the writer's own transaction (`post` adds it through the caller's session and never commits), so
the change and its to-do are saved together or not at all, and a rolled-back savepoint takes its to-dos with it.
Nothing here upserts, locks or updates a row: writers only insert, and coalescing is the consumer's job (`due`, `done`
and `fail` below, and `loop.py`).

A write that bypasses the session's change tracking (a Core `update`, `insert` or `delete`) is noted by the tripwire
(`hook.py`) and must be followed by a `post` or sit inside `exempt(reason)`: a note left at commit is a missed post
(`MissedPost` in the tests, the catch-all `*` in production)."""

import contextlib
import logging
import sys
from collections.abc import Collection, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from greenlet import getcurrent
from sqlalchemy import Delete, Select, Update, delete, func, inspect, select, update

from app.jobsearch.models import Todo
from app.jobsearch.work.queue import topics
from app.settings import settings
from app.timeutil import utc_now

log = logging.getLogger(__name__)

#: In the tests a write nobody posted for raises `MissedPost` at commit (`tests/conftest.py` turns it on for every
#: test, so every test that exercises a writer is also a coverage test); in production it posts the catch-all `*` for
#: the
#: table's topics in the same transaction and logs once for each call site.
strict = False

#: Whether a statement a test makes itself (to arrange state: the call site is in `tests/`) is watched too. It is
#: not: the
#: tripwire answers for the app's writers, and the many tests that set up rows with a direct statement are not them.
#: The tests of the tripwire turn it on.
check_tests = False

#: The longest `Todo.cause`.
CAUSE_CHARS = 60


class MissedPost(RuntimeError):
    """A write to a watched table reached commit with no `post` and no `exempt` (the tripwire, in strict mode)."""


# --- posting ----------------------------------------------------------------------------------------------------------

#: The to-dos this session has added and not yet flushed, by (owner, topic, key): a flush posts one of each. Dropped
#: after every flush, and an entry whose row a savepoint's rollback took is no longer pending, so it hides nothing.
_PENDING = "queue_pending"
#: The writes the tripwire has noted in this transaction and nothing has yet answered.
_NOTES = "queue_notes"
#: Set when this transaction has added a to-do: the session's commit then wakes the dispatcher (`hook.committed`). A
#: hint only; the row is the record.
WAKE = "queue_wake"
#: The topics a handler is running for (`suppressing`): its own writes do not post them again while it converges.
_suppressed: ContextVar[frozenset[str]] = ContextVar("queue_suppressed", default=frozenset())


def _sync(session: Any) -> Any:
    return getattr(session, "sync_session", session)


def post(session: Any, owner: str, topic: str, keys: Iterable[object] = ("",), *, after: timedelta | None = None,
         cause: str = "", now: datetime | None = None) -> int:
    """Add a to-do of `topic` for each of `keys` (a key is where to look: a role's id, `''` for the whole owner, `*` for
    anything of the topic) in `session`'s transaction. Never commits; a rollback takes them. One to-do for each topic
    and key in one flush however often it is asked. `after` holds it back until then, counted from `now` (the clock,
    which the dispatcher's tests move). Returns how many rows it added.
    None is added for a topic whose handler is running here (`suppressing`) unless it is held back: a handler's own
    writes are not another look at what it is looking at."""
    sync = _sync(session)
    if not after and topic in _suppressed.get():
        sync.info.pop(_NOTES, None)  # the write is answered: the handler goes on to look at it
        return 0
    pending: dict[tuple[str, str, str], Todo] = sync.info.setdefault(_PENDING, {})
    now = now or utc_now()
    made = 0
    for key in dict.fromkeys(str(k) for k in keys):
        triple = (owner, topic, key)
        have = pending.get(triple)
        if have is not None and inspect(have).pending:
            continue
        row = Todo(owner=owner, topic=topic, key=key, posted_at=now, not_before=now + after if after else now,
                   cause=cause[:CAUSE_CHARS])
        sync.add(row)
        pending[triple] = row
        made += 1
    if made:
        sync.info[WAKE] = True
    sync.info.pop(_NOTES, None)  # an answer to every write noted so far in this transaction
    return made


@contextlib.contextmanager
def suppressing(*topics: str) -> Iterator[None]:
    """While the block runs, in this task, `post` adds nothing for `topics` that is not held back: the handler of a
    topic converges on what it is looking at (`posting.sift`'s own filter marks every posting it reads), and a to-do
    for its own topic made by its own writes would only ask it to look again. What it leaves undone it posts itself
    (`handlers.Outcome.more`). Every other topic is still posted: those are the glue between the steps."""
    token = _suppressed.set(_suppressed.get() | frozenset(topics))
    try:
        yield
    finally:
        _suppressed.reset(token)


def post_all(session: Any, owner: str, posts: Iterable[tuple[str, Iterable[object]]], *, cause: str = "") -> int:
    """`post` for each (topic, keys) of `posts`, under one cause."""
    return sum(post(session, owner, topic, keys, cause=cause) for topic, keys in posts)


def post_roles(session: Any, owner: str, role_ids: Iterable[object], *, cause: str = "") -> int:
    """The to-dos of roles made or changed by a direct statement: each role is settled (`role.settle`) and the deck is
    looked at (`deck.dirty`). With no ids it posts nothing, and still answers the write the tripwire noted (the
    statement changed no row)."""
    ids = [str(i) for i in role_ids]
    return (post(session, owner, topics.ROLE_SETTLE, ids, cause=cause)
            + post(session, owner, topics.DECK_DIRTY, [""] if ids else [], cause=cause))


def post_new_roles(session: Any, owner: str, rows: Iterable[dict], *, cause: str = "") -> int:
    """The to-dos of roles a direct `insert` made, from the rows it inserted (the columns by name: `id`, `status`,
    `employer_id`, `description` ...): the same the hook posts for a role saved through the session
    (`watch.WATCH[Opportunity].created`). With no rows it posts nothing, and still answers the write the tripwire
    noted (the statement stored no row)."""
    from types import SimpleNamespace

    from app.jobsearch.models import Opportunity
    from app.jobsearch.work.queue import watch

    entry = watch.WATCH[Opportunity]
    base = {"description": None, "employer_id": None, "analysis_state": None, "analysis_claimed_at": None,
            "gate_facts": {}, "status": None}
    count = 0
    for row in rows:
        obj = SimpleNamespace(**{**base, **row})
        watch.posts_for(session, entry, obj, watch.Created(obj), entry.created, cause=cause)
        count += 1
    post(session, owner, topics.ROLE_SETTLE, [], cause=cause)
    return count


def post_claimed(session: Any, model: type, rows: Iterable[Any], columns: Iterable[str], *, cause: str = "") -> int:
    """The to-dos of rows a direct `UPDATE ... RETURNING` just changed (`work/claim.py`), from `columns` (the ones it
    set): what the hook posts when a session saves the same change (the rules of `watch.WATCH[model]` that name one of
    `columns`), as a change from nothing known. With no rows, or a model nobody watches, it posts nothing, and still
    answers the write the tripwire noted (the statement took no row)."""
    from app.jobsearch.work.queue import watch

    named = set(columns)
    count = 0
    entry = watch.WATCH.get(model)
    if entry is not None:
        for obj in rows:
            change = watch.Created(obj)
            for rule in entry.rules:
                if named.isdisjoint(rule.columns) or (rule.when is not None and not rule.when(obj, change)):
                    continue
                watch.posts_for(session, entry, obj, change, rule.posts,
                                cause=cause or f"claim:{model.__name__}.{sorted(named & set(rule.columns))[0]}")
                count += 1
    post(session, settings.owner, topics.POOL_OWED, [], cause=cause)
    return count


_exempt: ContextVar[str | None] = ContextVar("queue_exempt", default=None)


@contextlib.contextmanager
def exempt(reason: str) -> Iterator[None]:
    """The statements run inside the block post nothing and are not a missed post: an audited write no loop reads (a
    claim renewal every minute, a note only the consumer reads, a cleanup delete). `reason` is the audit: say why no
    to-do is owed, and the same words are in the baseline of the scan test."""
    if not reason.strip():
        raise ValueError("an exemption says why")
    token = _exempt.set(reason)
    try:
        yield
    finally:
        _exempt.reset(token)


def exempted() -> str | None:
    """Why the statements now running are exempt, or None."""
    return _exempt.get()


# --- the tripwire's notes ------------------------------------------------------------------------------------------


@dataclass
class Note:
    """One write to a watched table that nothing has yet answered."""

    table: str
    verb: str
    where: str
    #: The savepoint it was made in (None: the transaction itself), so a rolled-back savepoint takes its notes.
    within: Any


def _shown(path: Path, line: int, name: str) -> str | None:
    parts = path.parts
    if "app" in parts or "tests" in parts:
        if "sqlalchemy" in parts or "queue" in parts[-3:-1] or "packages" in parts or "asyncio" in parts:
            return None
        start = len(parts) - 1 - parts[::-1].index("app") if "app" in parts else parts.index("tests")
        return f"{'/'.join(parts[start:])}:{line} in {name}"
    return None


def call_site() -> str:
    """The code that made the write: the nearest frame outside SQLAlchemy and the queue, as `path:line in function`. A
    statement runs in a greenlet of its own, whose frames stop short of the code that awaited it, so the frames of the
    greenlets it was started from (the coroutines that awaited it) are read after its own."""
    frame = sys._getframe(1)
    here = getcurrent()
    while True:
        while frame is not None:
            if (found := _shown(Path(frame.f_code.co_filename), frame.f_lineno, frame.f_code.co_name)) is not None:
                return found
            frame = frame.f_back
        here = here.parent
        if here is None:
            return "?"
        frame = here.gr_frame


def note(session: Any, table: str, verb: str) -> None:
    """Record a write to `table` that the session's change tracking did not see."""
    if exempted() is not None:
        return
    where = call_site()
    if not check_tests and "/tests/" in where.split(":")[0]:
        return
    sync = _sync(session)
    sync.info.setdefault(_NOTES, []).append(Note(table, verb, where, sync.get_nested_transaction()))


def notes_of(session: Any) -> list[Note]:
    return list(_sync(session).info.get(_NOTES) or [])


def take_notes(session: Any) -> list[Note]:
    """The notes still open, and none after."""
    return list(_sync(session).info.pop(_NOTES, None) or [])


def drop_rolled_back(session: Any, rolled_back: Any) -> None:
    """A transaction (or savepoint) rolled back: the notes made inside it are gone with its writes."""
    sync = _sync(session)
    notes: list[Note] | None = sync.info.get(_NOTES)
    if not notes:
        return
    if rolled_back.parent is None:
        sync.info.pop(_NOTES, None)
        return

    def inside(note: Note) -> bool:
        tx = note.within
        while tx is not None:
            if tx is rolled_back:
                return True
            tx = tx.parent
        return False

    kept = [n for n in notes if not inside(n)]
    if kept:
        sync.info[_NOTES] = kept
    else:
        sync.info.pop(_NOTES, None)


def forget_pending(session: Any) -> None:
    """The flush is over: every to-do added is a row now."""
    _sync(session).info.pop(_PENDING, None)


# --- the consumer's side: what is due, done, failed --------------------------

#: How long a failed to-do waits before it is handed out again, by how many times it failed: 5 s, 30 s, 2 min, 10 min,
#: then an hour for every later one.
BACKOFF = (timedelta(seconds=5), timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(hours=1))
#: A failing to-do logs at error when its attempts reach one of these (never deleted silently).
LOUD_AT = (3, 10)
#: The most to-do rows one read of what is due takes. The rest are still there for the next.
READ_AT_MOST = 5000
#: The most ids one statement names.
IDS_PER_STATEMENT = 500


@dataclass(frozen=True)
class Group:
    """The due to-dos of one topic for one owner, as the consumer read them: the keys they name and exactly the ids it
    read (it deletes those, never "everything up to a mark": ids come from a sequence and are not in commit order)."""

    owner: str
    topic: str
    keys: tuple[str, ...]
    ids: tuple[int, ...]
    #: Who posted them (`Todo.cause`): a group that only the sweep asked for is a catch when it finds work.
    causes: frozenset[str]
    #: The most times any of them has failed.
    attempts: int
    #: The earliest time any of them was due (the later of when it was posted and its `not_before`).
    waiting_since: datetime
    #: How many of the to-dos were of each topic, for a group that merges the topics one handler takes (`merged`); empty
    #: for a group of one topic.
    parts: tuple[tuple[str, int], ...] = ()
    #: The one key a group of a `PER_KEY` topic is for (an ask's id); empty for a group of the whole topic.
    unit: str = ""

    @property
    def flight(self) -> str:
        """What the dispatcher counts as running: the topic, or, for one key of a `PER_KEY` topic, that key of it."""
        return f"{self.topic}:{self.unit}" if self.unit else self.topic

    @property
    def swept(self) -> bool:
        """Whether nothing but the hour's sweep (and the hour's look at what only time changes) asked for this look."""
        return "sweep" in self.causes and self.causes <= {"sweep", topics.CLOCK_CAUSE}

    @property
    def counts(self) -> tuple[tuple[str, int], ...]:
        """How many to-dos there were of each topic: the hour's line is by topic, whatever handler took them."""
        return self.parts or ((self.topic, len(self.ids)),)


def merged(topic: str, groups: Iterable[Group]) -> Group:
    """One group of the groups of the several topics one handler takes: the keys and ids of all, under the handler's own
    topic. The handler runs once for all of them and deletes exactly these ids."""
    found = list(groups)
    first = found[0]
    return Group(
        owner=first.owner, topic=topic, keys=tuple(dict.fromkeys(k for g in found for k in g.keys)),
        ids=tuple(sorted(i for g in found for i in g.ids)), causes=frozenset(c for g in found for c in g.causes),
        attempts=max(g.attempts for g in found), waiting_since=min(g.waiting_since for g in found),
        parts=tuple((g.topic, len(g.ids)) for g in found))


#: The topics whose to-dos each run apart, one run for each key: a page's ask (key = the ask's id) may take minutes (a
#: re-gate), and a quick ask posted meanwhile must not wait for it. Their other rules are the topic's (lane, pause).
PER_KEY = frozenset({topics.ENGINE_ASK})


def due_statement(owner: str, now: datetime, topics: Collection[str], limit: int = READ_AT_MOST) -> Select:
    """The to-dos of `topics` that are due, oldest first. One read of an index, on a table that is nearly always
    empty."""
    return select(Todo.id, Todo.topic, Todo.key, Todo.cause, Todo.attempts, Todo.posted_at, Todo.not_before).where(
        Todo.owner == owner, Todo.topic.in_(sorted(topics)), Todo.not_before <= now).order_by(Todo.id).limit(limit)


def next_due_statement(owner: str, now: datetime, topics: Collection[str]) -> Select:
    """When the next to-do of `topics` that is not yet due comes due."""
    return select(func.min(Todo.not_before)).where(
        Todo.owner == owner, Todo.topic.in_(sorted(topics)), Todo.not_before > now)


def waiting_statement(owner: str, topic: str) -> Select:
    """Whether any to-do of the topic is in the table, due or not: a look for it is on its way."""
    return select(Todo.id).where(Todo.owner == owner, Todo.topic == topic).limit(1)


#: The causes (`Todo.cause`) of the to-dos the dispatcher makes itself, which no writer posted: the hour's sweep, the
#: hour's look at what only time changes, a handler that stopped short, a tick's next time.
OWN_CAUSES = ("sweep", topics.CLOCK_CAUSE, "more", "tick")


def writer_posted_statement(owner: str, topics: Collection[str], ids: Collection[int]) -> Select:
    """Any to-do of `topics` a writer posted to be looked at at once, other than the ones in `ids`."""
    return select(Todo.id).where(Todo.owner == owner, Todo.topic.in_(sorted(topics)), Todo.cause.not_in(OWN_CAUSES),
                                 Todo.not_before <= Todo.posted_at, Todo.id.not_in(sorted(ids))).limit(1)


async def writer_posted(session: Any, owner: str, topics: Collection[str], ids: Collection[int]) -> bool:
    """Whether the table holds a to-do of `topics` that a writer posted (its cause is not one of `OWN_CAUSES`) and that
    is not one of `ids`, the ones a run read before it looked: a writer told the handler about a change while it
    waited or ran, so what the run found is not a writer's miss. It goes by which rows are there, never by comparing
    ids (they are not in commit order). Read only.
    A to-do that is held back does not count: the only to-do of its own topics a running handler can post is a held
    one (`post`, `suppressing`: the read of descriptions posts its retry three days on), and that is not a writer
    telling it of a change. So a writer's held-back to-do, and one that failed and backs off, do not count either, and
    such a run is called a catch as it was before.
    With more ids than one statement names it answers for the first of them only and may say yes for one of the rest:
    a sweep's group is a handful."""
    if not topics:
        return False
    named = sorted(ids)[:IDS_PER_STATEMENT]
    return await session.scalar(writer_posted_statement(owner, topics, named)) is not None


async def due(session: Any, owner: str, now: datetime, topics: Collection[str]) -> tuple[list[Group], datetime | None]:
    """The groups of to-dos of `topics` that are due, a group for each topic, in the order of the ids of their first
    rows, and when the next one not yet due comes due (None when none waits). Read only. A to-do of any other topic is
    not read: its consumer is another phase's or none."""
    if not topics:
        return [], None
    rows = (await session.execute(due_statement(owner, now, topics))).all()
    by_topic: dict[str, list[Any]] = {}
    for row in rows:
        by_topic.setdefault(row.topic, []).append(row)
    groups: list[Group] = []
    for topic, found in by_topic.items():
        apart = {r.key: [x for x in found if x.key == r.key] for r in found} if topic in PER_KEY else {"": found}
        for unit, rows_of in apart.items():
            groups.append(Group(
                owner=owner, topic=topic, keys=tuple(dict.fromkeys(r.key for r in rows_of)),
                ids=tuple(r.id for r in rows_of), causes=frozenset(r.cause for r in rows_of),
                attempts=max(r.attempts for r in rows_of),
                waiting_since=min(max(r.posted_at, r.not_before) for r in rows_of), unit=unit))
    groups.sort(key=lambda g: g.ids[0])
    return groups, await session.scalar(next_due_statement(owner, now, topics))


def done_statement(ids: Collection[int]) -> Delete:
    return delete(Todo).where(Todo.id.in_(sorted(ids)))


async def done(session: Any, ids: Collection[int]) -> None:
    """Delete exactly these to-dos: the ones a handler read before it looked at the data. One posted after has a higher
    id and stays. Never commits."""
    ordered = sorted(ids)
    for start in range(0, len(ordered), IDS_PER_STATEMENT):
        await session.execute(done_statement(ordered[start:start + IDS_PER_STATEMENT]))


def backoff(attempts: int) -> timedelta:
    """How long a to-do that has failed `attempts` times waits for its next try."""
    return BACKOFF[min(attempts, len(BACKOFF) - 1)]


def fail_statement(ids: Collection[int], error: str, not_before: datetime) -> Update:
    return update(Todo).where(Todo.id.in_(sorted(ids))).values(
        attempts=Todo.attempts + 1, last_error=error[:2000], not_before=not_before)


async def fail(session: Any, ids: Collection[int], error: str, now: datetime, attempts: int = 0) -> None:
    """A handler failed for these to-dos: they stay, with one more attempt, the error, and a due time that backs off by
    how often they have failed (`attempts` is the most any of them had). Logs at error at the third and the tenth
    attempt. Never commits."""
    ordered = sorted(ids)
    for start in range(0, len(ordered), IDS_PER_STATEMENT):
        await session.execute(fail_statement(ordered[start:start + IDS_PER_STATEMENT], error, now + backoff(attempts)))
    if attempts + 1 in LOUD_AT:
        log.error("work-queue: %d to-dos have now failed %d times; the last error: %s", len(ordered), attempts + 1,
                  error)
