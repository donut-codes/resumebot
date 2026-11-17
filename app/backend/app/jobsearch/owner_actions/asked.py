"""The owner door's writes as the page sees them (the split of the app into a page and an engine, docs/PRD.md path 50).

The page never runs an action: the actions call the engine's own services (the polite fetcher, the host stops, the
re-gate). So `act.do` on the page is a row, a bell and a wait for the notice:

1. `do` makes the job's row in the state `pending` (its id is made there) and asks the engine for it
   (`engine_calls.ask`, the ask `owner_action`). The ask is a fact and a to-do saved in the same transaction as the row,
   so a bell that is lost, or an engine that is down, loses nothing: the engine's look at every start answers each ask
   left unanswered.
2. The engine takes the row (`runner.begin`). In its writer's turn it checks the call, and either deletes the row and
   answers `{"refused": ...}` (the page raises `Refused`: the same words and status as a one-program run), or makes the
   row `running`, starts the job's task and answers `{"started": true}` at once. It holds no turn while the job runs.
3. The page registered its waiter for the job (`action_run`) before it saved anything. The engine tells the page when
   the job's row changes its end (`runner._tell_ended`); the page then reads the row once, on a reader. When no notice
   comes in `WAIT_SECONDS` it reads the row once anyway and answers the job as it stands.

`job` reads one job, or the last `JOBS_LISTED`, on a reader. A job still `running` is first brought up to date by the
engine (the ask `owner_action.follow`: a mission's run follows what it started).

This module imports nothing of the engine: it is what the page's door loads."""

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, notice
from app.errors import DomainError, NotFound
from app.jobsearch import actors
from app.jobsearch.models import ActionRun
from app.jobsearch.work import engine_calls
from app.timeutil import iso_or_none, utc_now

#: How long the page waits for the engine to take a call (the engine answers at once when it runs).
ASK_SECONDS = 20.0
#: How long the page then waits for the job to end before it answers with the job as it stands (the job goes on).
WAIT_SECONDS = 20.0
#: How many jobs `act.job` lists.
JOBS_LISTED = 50
#: The names of the asks the engine answers (`runner` registers both with `engine_asks`).
ASK_DO = "owner_action"
ASK_FOLLOW = "owner_action.follow"


class Refused(DomainError):
    """A call the engine refused, with its words, its HTTP status and what to do about it, as the engine gave them."""

    def __init__(self, status: int, reason: str, how_to_fix: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.how_to_fix = how_to_fix


def plain(value: Any) -> Any:
    """`value` as the JSON it is stored and answered as."""
    return json.loads(json.dumps(value, default=str))


def view(row: ActionRun) -> dict[str, Any]:
    """A job as it is answered: what was said first, then the job's facts and what the action gave."""
    result = dict(row.result or {})
    said = result.pop("said", None)
    out: dict[str, Any] = {"ok": row.state != "failed"}
    if row.state == "failed":
        out["reason"] = result.pop("error", "the action failed")
        out["how_to_fix"] = result.pop("how_to_fix", "read the reason; the job's row keeps the whole record")
    if said:
        out["said"] = said
    out.update({"job": str(row.id), "action": row.action, "dry_run": row.dry_run, "state": row.state,
                "progress": row.progress or None, "started": iso_or_none(row.started), "ended": iso_or_none(row.ended)})
    if row.state != "failed" or result:
        out["result"] = result
    return out


async def listed(session: AsyncSession, owner: str) -> dict[str, Any]:
    """The last `JOBS_LISTED` jobs of the owner, newest first (`act.job` with no id)."""
    rows = (await session.scalars(select(ActionRun).where(ActionRun.owner == owner).order_by(
        ActionRun.started.desc()).limit(JOBS_LISTED))).all()
    return {"ok": True, "jobs": [
        {"job": str(r.id), "action": r.action, "dry_run": r.dry_run, "state": r.state,
         "progress": r.progress or None, "started": iso_or_none(r.started), "ended": iso_or_none(r.ended)}
        for r in rows]}


async def do(session: AsyncSession, owner: str, name: str, raw: dict[str, Any] | None,
             dry_run: bool = False) -> dict[str, Any]:
    """Run a write action as the page: the job's row `pending`, an ask to the engine, and the wait for the notice.
    `session` is the door's writer session; the ask commits it and ends its transaction before any wait, so no turn is
    held while the engine works. Raises `Refused` when the engine refuses the call. Answers the finished job, or the job
    as it stands when the time is out (`pending`: no engine took it; `running`: it goes on). It waits for the job only
    when the engine answers `{"started": true}`."""
    job_id = uuid.uuid4()
    finished = notice.waiters.wait_for("action_run", [str(job_id)])  # first: a job that ends at once is not missed
    try:
        session.add(ActionRun(id=job_id, owner=owner, action=str(name)[:40], arguments=plain(raw or {}),
                              dry_run=dry_run, by=actors.CLAUDE, state="pending", started=utc_now()))
        answer = await engine_calls.ask(session, owner, ASK_DO, {"job": str(job_id)}, ASK_SECONDS)
        if isinstance(answer, dict):
            refused = answer.get("refused")
            if isinstance(refused, dict):
                raise Refused(int(refused.get("status") or 400), str(refused.get("reason") or "the call was refused"),
                              refused.get("how_to_fix"))
            if answer.get("started") is True:  # {"started": false}: the row is gone or another look took it
                await finished.wait(WAIT_SECONDS)
    finally:
        finished.close()
    async with db.read_session_factory()() as reader:
        row = await reader.get(ActionRun, job_id)
        if row is None:
            raise NotFound("this call was not kept: ask again")
        out = view(row)
        state = row.state
    if state == "pending":
        out["said"] = (f"{name} is not started yet: the app's worker has not taken it. It runs when the worker does. "
                       "Ask act.job with this job's id.")
    elif state == "running":
        out["said"] = f"{name} is not done yet; it goes on. Ask act.job with this job's id."
    return out


async def job(session: AsyncSession, owner: str, job_id: str | None) -> dict[str, Any]:
    """One job, or with no id the last `JOBS_LISTED`, read on a reader. A job that is `running` is first brought up to
    date by the engine (`ASK_FOLLOW`), which can write its row; with no engine the row is shown as it stands."""
    async with db.read_session_factory()() as reader:
        if not job_id:
            return await listed(reader, owner)
        try:
            key = uuid.UUID(job_id)
        except ValueError:
            raise Refused(422, "a job id is the id a write answered with") from None
        row = await reader.get(ActionRun, key)
        if row is None or row.owner != owner:
            raise NotFound("no such job")
        if row.state != "running":
            return view(row)
    await engine_calls.ask(session, owner, ASK_FOLLOW, {"job": str(key)}, ASK_SECONDS)
    async with db.read_session_factory()() as reader:
        row = await reader.get(ActionRun, key)
        if row is None:
            raise NotFound("no such job")
        return view(row)
