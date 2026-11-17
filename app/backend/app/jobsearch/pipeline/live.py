"""Live updates (the owner, 2026-10-23: the page is told what changed instead of asking again and redrawing): one stream
of server-sent events for each owner, `GET /api/v1/live`.

What is announced, each as it is committed, whichever code made it (a move by hand, the pipeline's own turn, the
analyst, Go, the writer), since every change is seen where the database is written (`_collect`, an `after_flush` hook,
as `models._record_stage_moves` sees every status change):

- `role`: a role was made, changed status, or was starred or unstarred. It carries what a page needs to move a card
  at once, and no more: `id`, `title`, `company`, `from` and `to` (statuses; `from` is None for a new role),
  `lane_from` and `lane_to` (the board's lanes: a closed status is the Closed lane), `label`, `closed_reason` (the
  Closed reason a closed role is read under, `board.closed_reason_key`), `reason` (a gate's), `favorite` and `changed`
  (which of `status`, `favorite` and `new`). What the row says (its note: there only for what is wrong or waits on the
  owner, docs/PRD.md 24.18) is the server's, so a page that holds the role reads its row again
  (`GET /roles/{id}/row`, `role_row.row`) when `changed` names something.
- `counts`: how many roles each lane holds, each Closed reason, and the favorites (`counts`), sent once after a commit
  that moved or starred a role (several commits close together are one message).
- `packet`: a packet was asked, changed status or moved on a step of its way: `id`, `role_id`, `version`, `status`,
  `from` (its status before) and `progress` (`packets.progress_of`: `step`, `label`, `done`, `of` and `fraction`),
  for a building bar.
- `summary`: a role's summary (`role_summary`) was written or changed: `id`, so a page that has the role open reads it
  again.
- `task`: a task was raised or changed (answered, done, set aside, put off): the task as `inbox.service.task_view`
  shows it, so Notifications and the shell's needs-you count change without a fetch; or removed: `id` and `deleted`.
- `notification`: a notification was raised or read: the notification as `inbox.service.notification_view` shows it,
  with `was_read` (whether it had been read before this change; None for a new one), so a page keeps its unread count
  without a fetch; or removed: `id` and `deleted`.
- `employer`: an employer was made, changed (a fact, its hard pass, its board read) or removed: the few facts its row on
  Employers shows (`employers.row`, with `fetched_at`), so the row changes in place and an open employer is read
  again; or removed (merged away): `id` and `deleted`. The researcher changes facts at night, with no page asking.
- `profile_version`: a version of a part of the owner's profile (What to look for, Hard pass, Prioritization or the
  settings) was saved, by the owner on another page, an agent or a restore, or its reason was added: `kind`, `version`,
  `saved_by`, `why` and `saved_at`, so Search Settings reads the profile again only when a version it has not seen
  lands (and says when a re-rank it started has finished).
- `site`: a site on Where to look was added, changed (switched, how it is read, what the app found of it) or removed:
  `id` and the plain columns the session holds (`url`, `label`, `on`, `list`, `read_by`), or `id` and `deleted`. What
  the site's row shows besides (its status, its roles) is worked out from other tables, so the page asks for the sites
  again once the messages stop.
- `changed`: a row behind one of the screens that used to ask again and again was made, changed or removed. It says
  which screen's data with `area` (`jobs`, `gaps`, `agents`, `missions`, `harness`, `prep`, `go` or `ready`) and `ids`
  (the rows, or what names them to the screen), and carries no data: the screen reads its own endpoint again.
  `AREA_WATCH` says which tables are behind which area; `ready` also comes from the engine's minute tick when the
  answer of `GET /agent/ready` changed with the clock (`handlers.tell_ready`).
- `hello`: the first message on a stream, with the counts as they are (when the stream asked for `role` or `counts`).
- `resync`: the page fell so far behind that messages were dropped: it should read the board again.

A page asks for the topics it shows (`GET /api/v1/live?topics=task,notification`; the topics are the message names
above, `TOPICS`); a stream that names none gets every message. `hello` and `resync` go to every stream.

A comment line (`: ping`) goes out when nothing has happened for `HEARTBEAT_SECONDS`, so a proxy keeps the stream open.
Messages are numbered (`id:`) in the order they were sent. A commit does not build its messages: `_collect`
keeps what changed as (owner, kind, id, tags) and `_send` hands the owner's items to the sink (`live_sink`). The local
sink, if a stream of the owner is open, reads the rows on a reader (`live_read.build`) and publishes; with no page open
nothing is read and nothing is sent. A message shows the row as it is when it is delivered.

In the page and engine split the stream is the page program's: the engine's commits reach it as a datagram
(app/notice.py, live_sink.WireSink), the hub (pipeline/live_hub.py) groups them and the page's local sink builds and
publishes.

A stream ends when its page goes away, and when the app is asked to stop (`Broker.close`, called from `app.stopping`):
the server waits for every open response before it shuts down, so a stream that outlived the request held every stop
until the launcher ended the app."""

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from process_engine.models import ProcHistory, ProcInstance, ProcToken, ProcVersion, ProcWait
from sqlalchemy import event, func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm.base import PASSIVE_NO_INITIALIZE

from app.jobsearch import stages
from app.jobsearch.models import (
    BrowserItem,
    CheckJob,
    Employer,
    EngineFact,
    GapAnswerRow,
    GapCard,
    GapCoachJob,
    GapDraft,
    GapNotTrue,
    GapRun,
    GoRun,
    Interview,
    InterviewPrep,
    JobPosting,
    Mission,
    MissionChange,
    MissionRun,
    Opportunity,
    Packet,
    ParseJob,
    ProfileVersion,
    Proposal,
    SearchRun,
)
from app.jobsearch.pipeline import live_sink
from app.jobsearch.pipeline.live_read import CHANGED, lane_of
from app.jobsearch.profile.base import KINDS as PROFILE_KINDS
from app.models.agent import AgentConnection, AgentKindPause, AgentRunner
from app.models.work import Notification, Task
from app.settings import settings
from app.sites.models import Site

log = logging.getLogger(__name__)

#: How long a stream waits with nothing to send before it sends a comment line.
HEARTBEAT_SECONDS = 15.0
#: How many messages a stream holds for a page that is not reading them; past this it is told to `resync`.
MOST_HELD = 1000
#: How long the counts wait after a commit, so the commits of one turn are one message.
COUNTS_AFTER_SECONDS = 0.25
#: Where a session keeps the messages of its open transaction until it commits.
PENDING = "live_pending"
#: The messages a stream can ask for by name (`?topics=`); `hello` and `resync` always go.
TOPICS = frozenset({"changed", "role", "counts", "packet", "summary", "task", "notification", "employer",
                    "profile_version",
                    "site"})
#: The messages whose stream wants the counts in its `hello`.
COUNTED = frozenset({"role", "counts"})


@dataclass(eq=False)
class Subscriber:
    owner: str
    #: The messages it asked for; None for every one.
    topics: frozenset[str] | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MOST_HELD))

    def wants(self, kind: str) -> bool:
        return self.topics is None or kind in self.topics or kind == "resync"


class Broker:
    """Every open stream, by owner, and the numbering of the messages."""

    def __init__(self) -> None:
        self._subs: dict[str, set[Subscriber]] = defaultdict(set)
        self._seq = 0
        self._counts_due: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        #: True once the app was asked to stop (`close`).
        self.closed = False

    def close(self) -> None:
        """The app is stopping: every open stream ends now, and one opened after this ends after its `hello`. The
        server waits for every open response before it shuts down, so a stream that went on until its page went away
        kept the app from ever finishing a stop (`app.stopping`). The page connects again when the app is back."""
        self.closed = True
        for subs in self._subs.values():
            for sub in subs:
                if sub.queue.full():
                    sub.queue.get_nowait()
                sub.queue.put_nowait(None)  # wakes a stream that is waiting for a message

    def subscribe(self, owner: str, topics: frozenset[str] | None = None) -> Subscriber:
        sub = Subscriber(owner, topics)
        self._subs[owner].add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs[sub.owner].discard(sub)

    def listening(self, owner: str) -> bool:
        return bool(self._subs.get(owner))

    def open_owners(self) -> list[str]:
        """The owners with at least one open stream."""
        return [owner for owner, subs in self._subs.items() if subs]

    def wanted(self, owner: str, kind: str) -> bool:
        """Whether any open stream of the owner asked for this message (`resync` goes to every stream)."""
        return any(sub.wants(kind) for sub in self._subs.get(owner, ()))

    def publish(self, owner: str, kind: str, data: dict) -> None:
        """Send one message to every stream of the owner. A stream whose page has fallen `MOST_HELD` behind is emptied
        and told to `resync`."""
        for sub in list(self._subs.get(owner, ())):
            if not sub.wants(kind):
                continue
            self._seq += 1
            if sub.queue.full():
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.queue.put_nowait((self._seq, "resync", {}))
                continue
            sub.queue.put_nowait((self._seq, kind, data))

    def counts_soon(self, owner: str) -> None:
        """Send the owner's counts shortly (`COUNTS_AFTER_SECONDS`), once however many commits ask in the meantime."""
        if owner in self._counts_due or not any(sub.wants("counts") for sub in self._subs.get(owner, ())):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # a commit outside the app's loop (a script): nobody is listening there
            return
        self._counts_due.add(owner)
        task = loop.create_task(self._send_counts(owner))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_counts(self, owner: str) -> None:
        from app import db

        try:
            await asyncio.sleep(COUNTS_AFTER_SECONDS)
            self._counts_due.discard(owner)
            if not self.listening(owner):
                return
            async with db.read_session_factory()() as session:
                self.publish(owner, "counts", await counts(session, owner))
        except Exception:  # a page's counts must never take the app down
            log.warning("the live counts were not sent", exc_info=True)
        finally:
            self._counts_due.discard(owner)


broker = Broker()


async def counts(session: AsyncSession, owner: str) -> dict[str, Any]:
    """How many roles each board lane holds (`lanes`, every lane of `stages.LANES` and Added links, zero included), how
    many roles each of the two groups the page draws apart holds (`groups`: `work` and `ranked`, the sums of their
    lanes, docs/PRD.md 24.17), how many closed roles each Closed reason holds (`closed`, `board.CLOSED_LABELS`'s keys),
    and how many favorites there are (`favorites`)."""
    from app.jobsearch.pipeline import board  # the board reads much of the job search
    from app.jobsearch.pipeline import from_posting as role_from_posting  # the board reads much of the job search

    lanes = {key: 0 for key, _ in stages.LANES}
    closed = dict.fromkeys(board.CLOSED_LABELS, 0)
    for status, n in (await session.execute(
            select(Opportunity.status, func.count()).where(Opportunity.owner == owner)
            .group_by(Opportunity.status))).all():
        lane = lane_of(status)
        if lane in lanes:
            lanes[lane] += n
    for status, reason, n in (await session.execute(  # only the closed roles are read by a reason
            select(Opportunity.status, Opportunity.ended_reason, func.count())
            .where(Opportunity.owner == owner, Opportunity.status.in_(stages.CLOSED))
            .group_by(Opportunity.status, Opportunity.ended_reason))).all():
        closed[board.closed_reason_key(status, reason)] += n
    starred = await session.scalar(select(func.count()).select_from(Opportunity).where(
        Opportunity.owner == owner, Opportunity.favorited_at.is_not(None))) or 0
    groups = {key: sum(lanes[s] for s in statuses) for key, statuses in stages.GROUPS}
    lanes = {board.ADDED_LINKS: await session.scalar(select(func.count()).select_from(JobPosting).where(
        *role_from_posting.waiting_added_where(owner))) or 0, **lanes}
    return {"lanes": lanes, "groups": groups, "closed": closed, "closed_total": sum(closed.values()),
            "favorites": starred}


def _changed(obj: object, name: str) -> tuple[bool, Any]:
    """Whether the attribute changed in this flush, and what it was before (None when it was not loaded). Read without
    loading anything: the hook runs inside a flush."""
    state = sa_inspect(obj)
    assert state is not None
    history = state.get_history(name, PASSIVE_NO_INITIALIZE)
    if not history.has_changes():
        return False, None
    return True, (history.deleted[0] if history.deleted else None)


def _summary_changed(obj: Opportunity, state: dict) -> bool:
    """Whether this flush wrote or changed the role's summary (`role_summary.SUMMARY_KEY` on its analysis)."""
    from app.jobsearch.research import role_summary  # it reads the job search

    changed, old = _changed(obj, "analysis")
    if not changed:
        return False
    now = (state.get("analysis") or {}).get(role_summary.SUMMARY_KEY)
    return bool(now) and now != (old or {}).get(role_summary.SUMMARY_KEY)


#: The columns of a task and a notification whose change is told; a flush that touched none of them says nothing.
TASK_FIELDS = ("title", "kind", "state", "detail", "due", "assignee", "options", "recommended", "answer", "note",
               "facts", "link_kind", "link_id", "snoozed_until", "done_at")
NOTIFICATION_FIELDS = ("kind", "title", "body", "level", "link_kind", "link_id", "read_at")


def _any_changed(obj: object, names: tuple[str, ...]) -> bool:
    return any(_changed(obj, name)[0] for name in names)


def _packet_tags(obj: Packet, new: bool) -> tuple[str, ...] | None:
    """The tags of a `packet` item when the packet is new, changed status, or moved on a step of its way
    (`packets.progress_of`: a change within the same step says nothing); None otherwise. The tag is `new`, or
    `was:<status>`, its status before: the one thing its row cannot say once it is committed."""
    from app.jobsearch.writing import packets  # the packets read much of the job search

    state = sa_inspect(obj).dict
    now_parts = {k: state.get(k) for k in ("status", "brief")}
    before = dict(now_parts)
    for name in now_parts:
        changed, old = (False, None) if new else _changed(obj, name)
        if changed:
            before[name] = old

    def step(parts: dict) -> dict:
        found = (parts["brief"] or {}).get(packets.STEP_KEY) if isinstance(parts["brief"], dict) else None
        return packets.progress_of(parts["status"] or "", found if isinstance(found, str) else None)

    if not new and before["status"] == now_parts["status"] and step(before)["step"] == step(now_parts)["step"]:
        return None
    if new:
        return ("new",)
    return (f"was:{before['status']}",) if before["status"] is not None else ()


def _was_read(obj: Notification, new: bool) -> bool | None:
    """Whether a notification had been read before this flush (None for a new one)."""
    if new:
        return None
    read_changed, before = _changed(obj, "read_at")
    return (before is not None) if read_changed else sa_inspect(obj).dict.get("read_at") is not None


def _read_tags(obj: Notification, new: bool) -> tuple[str, ...]:
    """The tag of a `notification` item: `new`, or whether it had been read before this flush."""
    was = _was_read(obj, new)
    return ("new",) if was is None else ("was_read:yes",) if was else ("was_read:no",)


def _any_column_changed(obj: object) -> bool:
    state = sa_inspect(obj)
    assert state is not None
    return any(state.get_history(key, PASSIVE_NO_INITIALIZE).has_changes() for key in state.manager)


@dataclass(frozen=True)
class AreaWatch:
    """A table behind a screen that re-reads its own endpoint, and what a row of it says (`changed`): the areas, the
    attribute that names the row to the screen, whether the table has an `owner` column, and, when set, which rows
    count (given the attributes the session holds)."""

    areas: tuple[str, ...]
    #: The attribute whose value is the item's id; a text that starts with `=` is the id itself.
    id_attr: str
    #: False for a table with no `owner` column: its owner is then `settings.owner`.
    owned: bool = True
    only: Callable[[dict], bool] | None = None


#: The facts (`facts.py`) the Agents page shows: the window's two lines, the browser-down note and the "Run now" asks.
SHOWN_FACTS = ("night_left", "night_spend", "browser_down")
SHOWN_FACT_PREFIXES = ("asked:", "owner_asked:")


def _shown_fact(state: dict) -> bool:
    key = str(state.get("key") or "")
    return key in SHOWN_FACTS or key.startswith(SHOWN_FACT_PREFIXES)


#: The tables behind each area (`live_read.AREAS`) and how a row of each is named to the screen. A flush that makes,
#: removes or changes a column of one of these rows sends a `changed` item for each of its areas.
AREA_WATCH: dict[type, AreaWatch] = {
    ParseJob: AreaWatch(("jobs",), "id"),
    CheckJob: AreaWatch(("jobs",), "id"),
    GapDraft: AreaWatch(("jobs",), "id"),
    GapRun: AreaWatch(("gaps",), "id"),
    GapCard: AreaWatch(("gaps",), "run_id"),
    GapCoachJob: AreaWatch(("gaps",), "run_id"),
    Proposal: AreaWatch(("gaps",), "run_id"),
    GapAnswerRow: AreaWatch(("gaps",), "id"),
    GapNotTrue: AreaWatch(("gaps",), "id"),
    AgentConnection: AreaWatch(("agents", "ready"), "id"),
    AgentRunner: AreaWatch(("agents",), "id"),
    AgentKindPause: AreaWatch(("agents",), "id"),
    BrowserItem: AreaWatch(("agents",), "=browser"),
    SearchRun: AreaWatch(("agents", "missions"), "id"),
    EngineFact: AreaWatch(("agents",), "key", only=_shown_fact),
    Mission: AreaWatch(("missions",), "id"),
    MissionRun: AreaWatch(("missions",), "mission_id"),
    MissionChange: AreaWatch(("missions",), "mission_id"),
    ProcInstance: AreaWatch(("harness",), "id", owned=False),
    ProcToken: AreaWatch(("harness",), "instance_id", owned=False),
    ProcWait: AreaWatch(("harness",), "instance_id", owned=False),
    ProcHistory: AreaWatch(("harness",), "instance_id", owned=False),
    ProcVersion: AreaWatch(("harness",), "id", owned=False),
    Interview: AreaWatch(("prep",), "id"),
    InterviewPrep: AreaWatch(("prep",), "interview_id"),
    GoRun: AreaWatch(("go",), "owner"),
}


def _keep_areas(pending: list, obj: object, *, new: bool, gone: bool) -> None:
    """Keep a `changed` item (owner, `changed`, the row's id, (area,)) for each area of the table the row is in, when
    the row was made or removed in this flush, or a column of it changed (`AREA_WATCH`)."""
    watch = AREA_WATCH.get(type(obj))
    if watch is None:
        return
    if not (new or gone or _any_column_changed(obj)):
        return
    inspected = sa_inspect(obj)
    assert inspected is not None
    state = inspected.dict
    if watch.only is not None and not watch.only(state):
        return
    owner = state.get("owner") if watch.owned else settings.owner
    if watch.id_attr.startswith("="):
        ident = watch.id_attr[1:]
    else:
        value = state.get(watch.id_attr)
        ident = "" if value is None else str(value)
    for area in watch.areas:
        pending.append((owner, CHANGED, ident, (area,)))


@event.listens_for(OrmSession, "after_flush")
def _collect(session: OrmSession, _flush_context) -> None:
    """Keep what changed, as (owner, kind, id, tags), for every role made, moved or starred, every packet asked or
    moved on, every task and notification raised or changed, every employer and site made, changed or removed, and
    every profile version saved, until the transaction commits (`_send`) or is rolled back (`_drop`). Only ids and a
    few short tags (`new`, `deleted`, `was:<status>`, `changed:status`, `changed:favorite`, `was_read:yes`,
    `was_read:no`; see the top of live_read.py): the sink builds the message from the row."""
    pending: list = session.info.setdefault(PENDING, [])

    def keep(obj: object, kind: str, tags: tuple[str, ...] = ()) -> None:
        inspected = sa_inspect(obj)
        assert inspected is not None
        state = inspected.dict
        pending.append((state.get("owner"), kind, str(state.get("id")), tags))

    for obj in session.deleted:  # still the flush's own list in this hook
        _keep_areas(pending, obj, new=False, gone=True)
        if isinstance(obj, (Task, Notification, Employer, Site)):
            kind = ("task" if isinstance(obj, Task) else "notification" if isinstance(obj, Notification)
                    else "site" if isinstance(obj, Site) else "employer")
            keep(obj, kind, ("deleted",))
    for obj in (*session.new, *session.dirty):
        new = obj in session.new
        _keep_areas(pending, obj, new=new, gone=False)
        if isinstance(obj, Task):
            if new or _any_changed(obj, TASK_FIELDS):
                keep(obj, "task")
        elif isinstance(obj, Notification):
            if new or _any_changed(obj, NOTIFICATION_FIELDS):
                keep(obj, "notification", _read_tags(obj, new))
        elif isinstance(obj, Employer):
            if new or _any_column_changed(obj):
                keep(obj, "employer")
        elif isinstance(obj, ProfileVersion):
            if sa_inspect(obj).dict.get("kind") in PROFILE_KINDS and (new or _changed(obj, "why")[0]):
                keep(obj, "profile_version")
        elif isinstance(obj, Site):
            if new or _any_column_changed(obj):
                keep(obj, "site")
        elif isinstance(obj, Opportunity):
            state = dict(sa_inspect(obj).dict)
            if new:
                keep(obj, "role", ("new",))
                continue
            if _summary_changed(obj, state):
                keep(obj, "summary")
            moved, was = _changed(obj, "status")
            starred, _ = _changed(obj, "favorited_at")
            if moved or starred:
                role_tags = tuple(f"changed:{x}" for x, on in (("status", moved), ("favorite", starred)) if on)
                if moved and was is not None:
                    role_tags = (*role_tags, f"was:{was}")
                keep(obj, "role", role_tags)
        elif isinstance(obj, Packet):
            tags = _packet_tags(obj, new)
            if tags is not None:
                keep(obj, "packet", tags)


@event.listens_for(OrmSession, "after_commit")
def _send(session: OrmSession) -> None:
    pending = session.info.pop(PENDING, None)
    if not pending:
        return
    by_owner: dict[str, list[live_sink.Item]] = {}
    told: set[tuple[str, str, tuple[str, ...]]] = set()  # a `changed` item said twice in one commit is one
    for owner, kind, row_id, tags in pending:
        if owner is None:
            continue
        if kind == CHANGED:
            if (owner, row_id, tags) in told:
                continue
            told.add((owner, row_id, tags))
        by_owner.setdefault(owner, []).append((kind, row_id, tags))
    for owner, items in by_owner.items():
        live_sink.current().announce(owner, items)


@event.listens_for(OrmSession, "after_rollback")
def _drop(session: OrmSession) -> None:
    session.info.pop(PENDING, None)


def frame(seq: int, kind: str, data: dict) -> str:
    """One server-sent event."""
    return f"id: {seq}\nevent: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def parse_topics(raw: str | None) -> frozenset[str] | None:
    """The topics a stream asked for (`role,counts`), or None for every one. A name that is no topic is refused."""
    if raw is None or not raw.strip():
        return None
    names = frozenset(t.strip() for t in raw.split(",") if t.strip())
    unknown = names - TOPICS
    if unknown:
        raise ValueError(f"no such topic: {', '.join(sorted(unknown))}")
    return names


async def stream(owner: str, disconnected: Callable[[], Awaitable[bool]],
                 heartbeat: float = HEARTBEAT_SECONDS, topics: frozenset[str] | None = None) -> AsyncIterator[str]:
    """The owner's stream: `hello` (with the counts when it asked for roles or counts), then every message it asked
    for (`topics`, None for all) as it is sent, a comment line when nothing has happened for `heartbeat` seconds, until
    the page goes away or the app is asked to stop (`Broker.close`)."""
    from app import db

    sub = broker.subscribe(owner, topics)
    try:
        hello: dict[str, Any] = {}
        if topics is None or topics & COUNTED:
            async with db.read_session_factory()() as session:
                hello = {"counts": await counts(session, owner)}
        yield frame(0, "hello", hello)
        while not broker.closed and not await disconnected():
            try:
                message = await asyncio.wait_for(sub.queue.get(), heartbeat)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if message is None:  # woken by `Broker.close`
                break
            yield frame(*message)
    finally:
        broker.unsubscribe(sub)
