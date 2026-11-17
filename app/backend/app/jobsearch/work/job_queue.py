"""What the two small request-and-answer jobs share: "Check my roles" (`jobs/check.py`, `js_check_jobs`) and
"Fill from a posting" / "Import your resume" (`jobs/parse.py`, `js_parse_jobs`).

Each is a row the owner's request makes and a small Claude session answers: waiting (no session has it) -> a session
took it -> done, or failed with one plain sentence. A job no session took in 5 minutes, or that was not finished in time
after it was taken, fails (checked whenever anything reads the jobs, so no clock is needed); the text and the items are
dropped when it ends, and the row after a day. It is offered whenever an agent is there to take the work (`ready`). An
agent busy with other work does not refuse it: what the owner asks for waits its turn, ahead of the background work
(`session_kinds.next_kind`), and says so while it waits (`say_queued`); the 5 minutes to be taken count from when the
agent was last at work (`untaken`), so a wait behind a session is not a job nobody took.

This module holds what is the same: the limits, the sentences a session is told, whether an agent can take a job now,
failing the jobs that took too long, and which waiting job is oldest. A job's own words, fields and steps stay in its
module. Services flush; the route commits."""

import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import roster
from app.jobsearch.work import availability, queue
from app.timeutil import aware

#: How long a job waits for a session to take it, and how long a row is kept after it ends.
TAKE_WITHIN = timedelta(minutes=5)
KEEP_FOR = timedelta(days=1)
#: The shortest pasted text a job takes. There is no longest: the owner's own paste has no limit on its length.
MIN_CHARS = 50

#: What a session is told when it answers a job that has already ended.
ENDED = "that job has ended (it took too long, or it was already answered); stop: you are finished"


async def ready(session: AsyncSession, owner: str, now: datetime | None = None) -> tuple[bool, str | None]:
    """Whether an agent is there to take a job the owner asks for, and, when not, one plain sentence for why (`GET
    /api/v1/agent/ready`): Claude's peak hours when the owner avoids them, no agent that has checked in lately, one
    the owner paused, or Claude's usage limit, which nothing is handed out through. An agent busy with a session is not
    a reason to refuse: the job waits its turn, ahead of the background work (`session_kinds.next_kind`), and says it is
    queued (`say_queued`). `now` only moves the runner's clock; Claude's peak hours read the wall clock unless a time is
    given. The reasons, their order and their words are `availability`'s (`Question.JOB`); Claude's usage limit is one
    that `session_kinds.next_kind` hands nothing out through, so a job made then would fail unclaimed."""
    stop = await availability.barred(session, owner, availability.Question.JOB, now=now)
    return (False, stop.words) if stop is not None else (True, None)


async def queued(session: AsyncSession, owner: str, now: datetime | None = None) -> str | None:
    """The one plain sentence for a job that waits because a session of the agent is at work, or None when none is: the
    agent says it is alive (`roster.LIVE_FOR`) in a session of some kind. A kind whose session stopped saying so (the
    runner died) is no session at all."""
    clock = now or datetime.now(UTC)
    conn = await roster.connection_by_slug(session, owner, roster.SCOUT)
    alive = aware(conn.checked_in_at) if conn is not None else None
    if conn is None or not conn.session_kind or alive is None or clock - alive > roster.LIVE_FOR:
        return None
    # What the session is busy with, in the words of "The agent is busy ...", and how long it takes when the app can
    # tell (the analyst researches one role a session, in about three minutes): `session_kinds`.
    from app.jobsearch.work.session_kinds import of_runner  # it loads the engine's wake

    working = of_runner(conn.session_kind)
    busy = working.busy if working is not None else "on another job"
    takes = working.takes if working is not None else None
    return f"Queued: the agent is busy {busy}{', ' + takes if takes else ''}. This starts as soon as it has finished."


async def say_queued(session: AsyncSession, owner: str, view: dict, now: datetime | None = None) -> dict:
    """A job still waiting says it is queued, and behind what, in the line the page shows for its step, while a session
    of the agent is at work; an idle agent leaves it "Waiting for an agent". A job in another state is left as it is."""
    if view.get("state") == "waiting":
        line = await queued(session, owner, now)
        if line is not None:
            view["step"] = line
    return view


async def untaken(session: AsyncSession, owner: str, asked: datetime, now: datetime) -> bool:
    """Whether a job asked for at `asked` has waited its `TAKE_WITHIN` with no session taking it. The five minutes count
    from the later of the request and the agent's last word or deed in a session (`last_seen_at`: a heartbeat, a call,
    the end of a session; the runner's idle heartbeat is not one), so a job queued behind a session is not failed for
    the session's length, and the end of that session is where its five minutes begin."""
    cutoff = now - TAKE_WITHIN
    if (aware(asked) or now) >= cutoff:
        return False
    conn = await roster.connection_by_slug(session, owner, roster.SCOUT)
    active = aware(conn.last_seen_at) if conn is not None else None
    return active is None or active < cutoff


def fail(job, state: str, error: str, now: datetime, **dropped) -> None:
    """The job ends as failed, with one plain sentence; what it held for the session (`text`, `items`, `answers`) is
    dropped, each as the empty value given."""
    job.state, job.error, job.finished_at = state, error, now
    for field, empty in dropped.items():
        setattr(job, field, empty)


def kept_too_long(job, now: datetime) -> bool:
    """Whether a job's row is past its day (`KEEP_FOR`): one that `expire` drops. A read treats it as gone already."""
    return (aware(job.created_at) or now) < now - KEEP_FOR


async def late(session: AsyncSession, owner: str, job, *, working: str, finish_within: timedelta, not_taken: str,
               not_finished: str, now: datetime, waiting: str = "waiting", last_touched=lambda job: job.taken_at,
               patient=lambda job: False) -> str | None:
    """The sentence a job that took too long is failed with, or None while it is in time or has ended. It only reads:
    `expire` fails the job with it (the engine's minute tick), and a read of the job tells it failed with it before
    that pass has run (`told` in `jobs/check.py` and `jobs/parse.py`). The arguments are `expire`'s."""
    if job.state == waiting and not patient(job) and await untaken(session, owner, job.created_at, now):
        return not_taken
    if job.state == working and (last_touched(job) or job.created_at) < now - finish_within:
        return not_finished
    return None


async def expire(session: AsyncSession, model, owner: str, *, working: str, finish_within: timedelta, not_taken: str,
                 not_finished: str, failed, waiting: str = "waiting", last_touched=lambda job: job.taken_at,
                 patient=lambda job: False, now: datetime | None = None, fail_late: bool = True,
                 drop_old: bool = True) -> None:
    """Fail the jobs that took too long and drop the ones kept long enough. Run before any read of the jobs. `working`
    is the state a job is in while a session has it; `failed(job, error, now)` ends one; `last_touched(job)` is when a
    session last had a hand in it, counted from creation when it never did. A job nobody took counts its `TAKE_WITHIN`
    as `untaken` says, unless `patient(job)`: a job that waits for a free session however long it takes.

    The two halves can be asked for apart (the queue's minute tick fails the late jobs, its hour tick drops the old
    rows): `fail_late` and `drop_old`, both on for every other caller."""
    now = now or datetime.now(UTC)
    if drop_old:
        with queue.exempt("the rows of jobs kept past their day are only tidied: a job that ended is read by no loop"):
            await session.execute(delete(model).where(model.owner == owner, model.created_at < now - KEEP_FOR))
    if not fail_late:
        return
    open_jobs = (await session.scalars(
        select(model).where(model.owner == owner, model.state.in_((waiting, working))))).all()
    for job in open_jobs:
        why = await late(session, owner, job, working=working, finish_within=finish_within, not_taken=not_taken,
                         not_finished=not_finished, now=now, waiting=waiting, last_touched=last_touched,
                         patient=patient)
        if why is not None:
            failed(job, why, now)
    await session.flush()


def oldest_waiting_query(model, owner: str, waiting: str = "waiting", *, skip: Collection[uuid.UUID] = ()):
    """The select of the oldest job no session has taken (after `expire`). A job in `skip` (claimed for another
    session, `hand_outs.py`) is passed over. `claim_one` (`work/claim.py`) takes it in one statement."""
    query = select(model).where(model.owner == owner, model.state == waiting)
    if skip:
        query = query.where(model.id.not_in(skip))
    return query.order_by(model.created_at).limit(1)


async def oldest_waiting(session: AsyncSession, model, owner: str, waiting: str = "waiting", *,
                         skip: Collection[uuid.UUID] = ()):
    """The oldest job no session has taken (after `expire`): what makes the session kind the answer to "which work
    waits". A job in `skip` (claimed for another session, `hand_outs.py`) is passed over."""
    return await session.scalar(oldest_waiting_query(model, owner, waiting, skip=skip))
