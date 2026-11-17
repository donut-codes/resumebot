"""Process instances: where each running process is, its data and its history (requirement 15.1).

- `proc_instances`: one running (or finished) process: which file and version, its status, its data, and the
  instance that called it (a call activity, or one item of a multi-instance step).
- `proc_files`: the text of every file an instance started on, by its hash, so a run can finish on its own file after
  a release changed the one that is loaded.
- `proc_tokens`: where an instance is (the element) and whether it is moving or waiting.
- `proc_waits`: what a waiting token waits for and what to hand out: an agent's task, a household card, a
  timer, a message, a sub-process's end.
- `proc_history`: every move the instance made, append-only: from, to, the condition that chose it, the data it
  read, the time. "Where is this?" is read from the instance; Activity is written from this.
- `proc_versions`: an owner's own versions of a process file.

They sit on `process_engine.base.Base`, with no foreign key to a table the app owns (`entity_id`, `changed_by`,
`signed_off_by` and `rule_change_id` are plain UUIDs); the app adds the constraints it wants in its own migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from process_engine.base import Base, Timestamps, UUIDPk
from process_engine.portable import AutoKey
from process_engine.types import UtcDateTime, utc_now

# Instance status
RUNNING, WAITING, COMPLETED, FAILED, CANCELLED = "running", "waiting", "completed", "failed", "cancelled"
LIVE = "live"
# Token state
ACTIVE, WAITS, DONE = "active", "waiting", "done"
# Wait kinds and status
OPEN, CLOSED, VOID = "open", "done", "cancelled"


class ProcInstance(UUIDPk, Timestamps, Base):
    __tablename__ = "proc_instances"
    __table_args__ = (Index("ix_proc_instances_key_status", "process_key", "status"),)

    process_key: Mapped[str] = mapped_column(String(120))
    file_hash: Mapped[str] = mapped_column(String(64))
    #: A hash of what decides behaviour only (`loader.behaviour_hash`): the instance runs on the loaded file of its
    #: process with this hash, however the file's words were edited. None for an instance that started before this was
    #: kept: it finds its file by `file_hash` alone.
    behaviour_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=RUNNING, server_default=RUNNING)
    #: Always `live`: the column stays, and the app's queries filter on it.
    mode: Mapped[str] = mapped_column(String(8), default=LIVE, server_default=LIVE)
    #: What the instance is about, as the caller names it (e.g. "goal:<id>"), so the caller can find it again.
    subject: Mapped[str | None] = mapped_column(String(255), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("proc_instances.id", ondelete="CASCADE"), index=True
    )
    parent_element: Mapped[str | None] = mapped_column(String(120))
    item_index: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class ProcFile(Base):
    """The text of a process file by its hash (`ProcInstance.file_hash`), written when an instance starts on it. A
    release can change the shipped file, or a household a version, while a run is still on the old one; the library
    only holds the files loaded now, so the run's own file is read back from here and it finishes on it."""

    __tablename__ = "proc_files"

    file_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    process_key: Mapped[str] = mapped_column(String(120))
    xml: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class ProcToken(UUIDPk, Base):
    __tablename__ = "proc_tokens"

    instance_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("proc_instances.id", ondelete="CASCADE"), index=True
    )
    element_id: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(10), default=ACTIVE, server_default=ACTIVE)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class ProcWait(UUIDPk, Base):
    __tablename__ = "proc_waits"
    __table_args__ = (Index("ix_proc_waits_due", "status", "due_at"),)

    instance_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("proc_instances.id", ondelete="CASCADE"), index=True
    )
    token_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("proc_tokens.id", ondelete="CASCADE"))
    #: The step (or boundary event) this wait belongs to.
    element_id: Mapped[str] = mapped_column(String(120))
    #: agent | user | deferred | call | catch_message | catch_timer | boundary_timer
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), default=OPEN, server_default=OPEN)
    #: What to hand out (role, instructions, template, card, the step's name) or, for a call, its children.
    payload: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    message: Mapped[str | None] = mapped_column(String(255))
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class ProcHistory(Base):
    """Append-only: rows are added, never changed."""

    __tablename__ = "proc_history"
    __table_args__ = (UniqueConstraint("instance_id", "seq", name="uq_proc_history_seq"),)

    id: Mapped[int] = mapped_column(AutoKey, primary_key=True, autoincrement=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("proc_instances.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    #: start | move | decision | wait | complete | fire | error | end | fail | cancel
    kind: Mapped[str] = mapped_column(String(12))
    from_element: Mapped[str | None] = mapped_column(String(120))
    to_element: Mapped[str | None] = mapped_column(String(120))
    flow_id: Mapped[str | None] = mapped_column(String(120))
    #: The condition that chose this way out (its text), or "default" / "only way out".
    condition: Mapped[str | None] = mapped_column(Text)
    #: The data the condition read: {path: value}.
    reads: Mapped[dict | None] = mapped_column(JSON)
    detail: Mapped[dict | None] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class ProcVersion(UUIDPk, Base):
    """One household version of a process file (requirements 15.4, 15.6): the shipped file stays read-only, and a
    household's change to a decision's rule is saved here as a whole new file. The newest version of a process is the
    one new work starts on; instances already running finish on the file they started with (their `file_hash`).
    A rollback is a new version too (its text is an earlier one's, or the shipped file's), so the history is never
    rewritten. `number` counts from 1 per household and process; the shipped file is "version 0" and is never a row."""

    __tablename__ = "proc_versions"
    __table_args__ = (UniqueConstraint("entity_id", "process_key", "number", name="uq_proc_versions_number"),)

    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    process_key: Mapped[str] = mapped_column(String(120))
    number: Mapped[int] = mapped_column(Integer)
    file_hash: Mapped[str] = mapped_column(String(64))
    xml: Mapped[str] = mapped_column(Text)
    #: rule_change | rollback
    kind: Mapped[str] = mapped_column(String(12), default="rule_change", server_default="rule_change")
    #: For a rollback: the version it went back to (0 is the shipped file).
    restored_number: Mapped[int | None] = mapped_column(Integer)
    rule_change_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    gateway_id: Mapped[str | None] = mapped_column(String(120))
    flow_id: Mapped[str | None] = mapped_column(String(120))
    #: The decision and way out, as the household reads them ("Does the plan need you?", "It needs you").
    decision_name: Mapped[str | None] = mapped_column(String(255))
    old_condition: Mapped[str | None] = mapped_column(Text)
    new_condition: Mapped[str | None] = mapped_column(Text)
    #: The household's own words, and the read-back it signed off.
    wording: Mapped[str | None] = mapped_column(Text)
    readback: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    signed_off_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    signed_off_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)

