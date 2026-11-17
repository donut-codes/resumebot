"""The registry of the owner's actions (docs/PRD.md path 50.4): one `Action` for each thing the owner's Claude may ask
the app to do now, written once with its name, its arguments (a model), whether it writes, which conflicts refuse it and
the one function that calls the service the page or a mission calls. The door (`owner_actions/mcp.py`) is an adapter
over this
registry and `runner.py` does what is common to every action (the job row, the dry run, the Activity line, the wait).

An action's function takes the `Ctx` (the session, the owner, whether it is a dry run) and its arguments and answers an
`Outcome`. It writes nothing when `ctx.dry_run` and says what it would have done. Services flush; the runner commits."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.errors import DomainError


class BadArguments(DomainError):
    """What an action was sent is not what it takes (an unknown action, an argument it refuses, a missing name)."""

    status = 422


class Args(BaseModel):
    """The base of every action's arguments: a name that is not one of them is refused, not ignored."""

    model_config = ConfigDict(extra="forbid")


class NoArgs(Args):
    pass


@dataclass
class Ctx:
    """What an action's function is given: the session (committed by the runner for a write, rolled back for a read or a
    dry run), the owner, whether it is a dry run, and the job's id (None for a read)."""

    session: AsyncSession
    owner: str
    dry_run: bool = False
    job_id: uuid.UUID | None = None
    #: Keeps the job's one progress line current, in a short transaction of its own (a read has none: it does nothing).
    progress: Callable[[str], Awaitable[None]] | None = None

    async def say(self, line: str) -> None:
        """Say how far the job is, where `act.job` can see it. The line is written in a session of its own, and the
        writer's turn is one for the file, so the action's own transaction is ended first (what it wrote so far is
        saved, as it reports it). A dry run keeps its transaction to the end, to roll it back, and says nothing on the
        way."""
        if self.progress is None or self.dry_run:
            return
        await db.release_if_free(self.session)
        await self.progress(line)


@dataclass
class Outcome:
    """What an action did. `result` is what the caller is answered; `did` is the past-tense phrase after "Claude (on
    demand)" in Activity ("requeued 12 roles for research"); `would` the phrase after "would" of a dry run ("requeue 12
    roles for research"). `after` are the wake-ups sent once the commit has landed, `follow` keeps the job running while
    what it started goes on (a mission's run), and `link` ties the Activity line to a role."""

    result: dict[str, Any]
    did: str = ""
    would: str = ""
    after: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    follow: bool = False
    link: tuple[str, uuid.UUID] | None = None


Run = Callable[[Ctx, Any], Awaitable[Outcome]]
#: Who holds what a second run of the action would collide with, in words ("Scout the boards is running already"), or
#: None. Evaluated before anything is written: a real call is refused (409), a dry run says it would be.
Conflict = Callable[[AsyncSession, str, Any], Awaitable[str | None]]
#: Brings the job of an action that follows what it started up to date: (session, row) -> None. See `Outcome.follow`.
Follow = Callable[[AsyncSession, Any], Awaitable[None]]


@dataclass(frozen=True)
class Action:
    name: str
    does: str
    inputs: type[Args]
    run: Run
    writes: bool = False
    #: Whether a call may outlast the 20 seconds `act.do` waits (it then answers with a job).
    long: bool = False
    #: The conflicts that refuse it, in words, for `actions.list`.
    refuses: str = ""
    #: Whether a second of the same action is refused while one runs (the check is made in the writer's turn that
    #: makes the row).
    one_at_a_time: bool = False
    conflict: Conflict | None = None
    follow: Follow | None = None

    def listed(self) -> dict[str, Any]:
        """The action as `actions.list` shows it: name, what it does, its inputs, whether it writes and is long."""
        schema = self.inputs.model_json_schema()
        inputs = {name: {k: v for k, v in prop.items() if k in ("type", "enum", "description", "default", "anyOf")}
                  for name, prop in (schema.get("properties") or {}).items()}
        return {"name": self.name, "does": self.does, "inputs": inputs, "required": schema.get("required", []),
                "writes": self.writes, "long": self.long, "dry_run": self.writes,
                **({"refuses": self.refuses} if self.refuses else {})}


REGISTRY: dict[str, Action] = {}


def register(name: str, does: str, inputs: type[Args] = NoArgs, *, writes: bool = False, long: bool = False,
             refuses: str = "", one_at_a_time: bool = False, conflict: Conflict | None = None,
             follow: Follow | None = None) -> Callable[[Run], Run]:
    """Register `fn(ctx, args) -> Outcome` as the action `name`."""

    def add(fn: Run) -> Run:
        if name in REGISTRY:
            raise ValueError(f"two actions are named {name}")
        REGISTRY[name] = Action(name, does, inputs, fn, writes=writes, long=long, refuses=refuses,
                                one_at_a_time=one_at_a_time, conflict=conflict, follow=follow)
        return fn

    return add
