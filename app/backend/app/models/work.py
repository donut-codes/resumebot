"""The owner's things to do and things to know, and the record of what happened: `tasks`, `notifications`,
`trail_events`. This app's own simple model: one owner, no goals.

- A **task** is something for someone to do or decide: a title, a kind, a state, a due date, who it is for, and a
  link to the opportunity or search run it is about. A decision is a task with options.
- A **notification** is a plain sentence about something the owner should know ("The scout couldn't finish a search").
  New roles found never raise one.
- A **trail event** is one line of Activity: what the app or an agent did.
A task or notification can carry a `key`, unique per owner, so the code that raises it can run twice and raise it once.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from process_engine.types import UtcDateTime
from sqlalchemy import (
    JSON,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, one_of
from app.timeutil import utc_now, utc_now_unique


class TaskState(StrEnum):
    """What state an Inbox task is in (`tasks.state`)."""

    OPEN = "open"
    DONE = "done"
    SKIPPED = "skipped"


#: The states of a task, as the plain words the code compares and stores.
TASK_OPEN, TASK_DONE, TASK_SKIPPED = TaskState.OPEN.value, TaskState.DONE.value, TaskState.SKIPPED.value


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("owner", "key", name="uq_tasks_owner_key"),
        Index("ix_tasks_owner_state", "owner", "state"),
        one_of("tasks", "state", TaskState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    key: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))
    #: search_failed | decision | ... (free, short words)
    kind: Mapped[str] = mapped_column(String(40))
    #: open | done | skipped
    state: Mapped[str] = mapped_column(String(10), default=TaskState.OPEN.value, server_default=TaskState.OPEN.value)
    detail: Mapped[str | None] = mapped_column(Text)
    due: Mapped[date | None] = mapped_column(Date)
    #: Who it is for: "owner", or an agent connection's slug ("scout").
    assignee: Mapped[str] = mapped_column(String(80), default="owner", server_default="owner")
    #: A decision: [{id, label, description?}], the id of the one recommended, and the answer given.
    options: Mapped[list | None] = mapped_column(JSON)
    recommended: Mapped[str | None] = mapped_column(String(40))
    answer: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text)
    #: The facts a decision rests on, side by side on Notifications: [{label, value, sub?}], plain words, filled by the
    #: code that raises it (an employer merge: both names with their roles, and the careers site they share).
    facts: Mapped[list | None] = mapped_column(JSON)
    link_kind: Mapped[str | None] = mapped_column(String(20))
    link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    snoozed_until: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    done_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("owner", "key", name="uq_notifications_owner_key"),
        Index("ix_notifications_owner_created", "owner", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    key: Mapped[str | None] = mapped_column(String(120))
    #: search_finished | search_failed | ...
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    #: info | attention (attention counts toward the Inbox badge until read)
    level: Mapped[str] = mapped_column(String(10), default="info", server_default="info")
    link_kind: Mapped[str | None] = mapped_column(String(20))
    link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    read_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


#: The lines a person wrote about a role (`ix_trail_events_person_lines`): what the owner's moves on the board and the
#: rules that leave a role they touched alone are read from, by owner, event and role. A small share of the table's
#: lines.
_PERSON_LINES_ABOUT_A_ROLE = "actor = 'person' AND link_kind = 'opportunity'"


class TrailEvent(Base):
    __tablename__ = "trail_events"
    __table_args__ = (
        Index("ix_trail_events_owner_at", "owner", "at"),
        # The history has no other index on the role a line is about (migration 0145): without this one, asking for the
        # owner's moves of a role (`placement._hand_lines`, `quiet.hand_since`) read every line of the owner's.
        Index("ix_trail_events_person_lines", "owner", "event", "link_id", "at",
              sqlite_where=text(_PERSON_LINES_ABOUT_A_ROLE)),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: When it happened, by the app's clock at the moment it is saved: the database's `now()` is the start of the
    #: transaction, so the lines a run writes together would all share one time and have no order. Each line's time is
    #: its own even where the clock is coarse (`utc_now_unique`): Activity puts the lines in order by it.
    at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now_unique)
    #: person | agent | app
    actor: Mapped[str] = mapped_column(String(10))
    #: The agent's slug when an agent did it, so an agent's own Activity can be listed.
    agent_slug: Mapped[str | None] = mapped_column(String(40), index=True)
    who: Mapped[str] = mapped_column(String(120))
    #: search_started | scout_handed_task | scout_reported | search_finished | search_failed | task_done | ...
    event: Mapped[str] = mapped_column(String(40))
    #: One plain sentence.
    sentence: Mapped[str] = mapped_column(String(200))
    #: needs_you | result | routine
    tier: Mapped[str] = mapped_column(String(10), default="routine", server_default="routine")
    #: on | off | neutral
    mark: Mapped[str] = mapped_column(String(8), default="neutral", server_default="neutral")
    link_kind: Mapped[str | None] = mapped_column(String(20))
    link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: The search run whose hand-out "What was sent" opens.
    sent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_search_runs.id", ondelete="SET NULL", name="fk_trail_events_sent_run_id"))
    detail: Mapped[str | None] = mapped_column(Text)
