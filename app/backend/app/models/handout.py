"""What an agent was actually sent: one row each time the scout is handed its task (`jobs.profile` with a search open).

The payload is the reply as sent, with anything secret-looking removed; the files are the instruction and template the
step names, by hash, so a later edit to a file is visible. Bounded: `app.planengine.handouts` keeps the newest rows.
Read by Agents > How they work; nothing else reads it. Adapted from another project's code (app/PROVENANCE.md): a search
run is the task, so `task_id` is the search run (and a real foreign key now that both tables are here).
"""

import uuid
from datetime import datetime

from process_engine.types import UtcDateTime
from sqlalchemy import JSON, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TaskHandout(Base):
    __tablename__ = "task_handouts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80), index=True)
    #: The search run this was sent for (the task). Null for a hand-out sent for a site
    #: project's task (`plan_task_id`).
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_search_runs.id", ondelete="CASCADE"), index=True
    )
    #: The site project's task this was sent for (docs/PRD.md path 29), when it was not a search run.
    plan_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("plan_tasks.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_connections.id", ondelete="SET NULL")
    )
    #: The agent's name when it was sent, in words, so a removed connection still reads.
    agent_name: Mapped[str | None] = mapped_column(String(255))
    #: Which hand-out path it was (`app.planengine.handouts.VIA`).
    via: Mapped[str] = mapped_column(String(40))
    #: Which process and step it is (the step's id in the process file).
    process: Mapped[str | None] = mapped_column(String(60))
    step: Mapped[str | None] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(300), default="", server_default="")
    payload: Mapped[dict] = mapped_column(JSON)
    payload_sha: Mapped[str] = mapped_column(String(64))
    chars: Mapped[int] = mapped_column(Integer)
    #: [{"kind": "instruction" | "template", "name", "sha"}]: the files it was sent with.
    files: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    sent_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
