"""The agents this app works with: roles (data, so Writer or Reviewer can be added later) and connections.

`RoleDef` and `AgentConnection` are this app's own and small: one
owner, no tiers, no boundary. A connection says what it is (name, what it is for), which roles it holds (keys into
`agent_roles`), when it last checked in, and whether the owner paused it. The scout's token stays a file
(`app.jobsearch.doors.scout`), so no token is stored here.
"""

import enum
import uuid
from datetime import datetime

from process_engine.types import UtcDateTime
from sqlalchemy import JSON, Boolean, Integer, String, Text, UniqueConstraint, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, one_of
from app.timeutil import utc_now


class RoleDef(Base):
    """One kind of agent job, as data: its key (what a process file's `porter:role` names), its plain title and what it
    does. The scout is shipped; a Writer or a Reviewer is a new row."""

    __tablename__ = "agent_roles"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    about: Mapped[str] = mapped_column(Text, default="", server_default="")
    sort: Mapped[int] = mapped_column(Integer, default=100, server_default="100")


class AgentConnection(Base):
    """One agent on the roster. `slug` is how this app finds it (`scout`)."""

    __tablename__ = "agent_connections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80), index=True)
    slug: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    #: Keys of `agent_roles` this connection holds.
    roles: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    #: The last time it called this app at all (an MCP call or a heartbeat), and the last time its session said it
    #: was alive.
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    checked_in_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: Which agent the runner's session is right now (scout, analyst, writer), from its heartbeat; None between
    #: sessions. One runner takes the three in turn (docs/PRD.md 25.9).
    session_kind: Mapped[str | None] = mapped_column(String(20))
    #: What it said about itself (the runner declares it on start): model_name, hosting, runtime, engine, concurrency,
    #: usage_limit, host, context_window_tokens. Plain facts, none a secret.
    declared: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    declared_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When Claude's usage limit lifts, as a session last met it: kept so a restart of the app does not forget a hold
    #: (`availability.limit_until`). Only ever moved later; a time in the past holds nothing.
    usage_limit_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class RunnerState(enum.StrEnum):
    idle = "idle"  # up, between sessions
    working = "working"  # a session is running


class AgentRunner(Base):
    """One runner of the scout (a Node process of `app/scout/lanes-local.mjs`) as its own heartbeats show it. The pool
    runs
    several at once (`session-1` to `session-5`), and the connection's
    one row cannot say what each is doing, so each heartbeat is also noted here, by the runner's `name`. The Agents page
    reads them: which runner, working or idle, on what kind of session, since when, and when it was last heard.
    `seen_at` is the last heartbeat of any kind; a runner not heard for a while is shown as not seen, and one not heard
    for a day is dropped (`app.agents.roster.note_runner`). An install with one runner has at most one row."""

    __tablename__ = "agent_runners"
    __table_args__ = (
        UniqueConstraint("owner", "name", name="uq_agent_runners_owner_name"),
        one_of("agent_runners", "state", RunnerState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: How the runner names itself in its heartbeats (`runner`): `search`, `analysis-1`, `owner`, `writer`; `scout` for
    #: a runner with no lane that says nothing.
    name: Mapped[str] = mapped_column(String(60))
    #: idle | working
    state: Mapped[str] = mapped_column(String(10), default="idle", server_default="idle")
    #: The kind of session it has while it works (the id's prefix: scout, analyst, writer, reader, titles, ...).
    kind: Mapped[str | None] = mapped_column(String(20))
    session_id: Mapped[str | None] = mapped_column(String(80))
    #: When it entered this state: when the session started, or when it went idle.
    since: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    #: The last heartbeat of any kind.
    seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class AgentKindPause(Base):
    """An agent kind the owner paused (`app.jobsearch.pool`: learner, researcher, writer, reader, title_checker,
    search): a paused kind is never handed out to a session and never queued by the pool. A row is the pause; resuming
    deletes it. The global pause stays the scout connection's (`AgentConnection.paused`)."""

    __tablename__ = "agent_kind_pauses"
    __table_args__ = (UniqueConstraint("owner", "kind", name="uq_agent_kind_pauses_owner_kind"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    paused_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
