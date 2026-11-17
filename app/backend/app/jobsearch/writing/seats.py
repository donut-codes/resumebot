"""Agent seats: the single-turn agent steps of the packet and library processes.

A seat is an agent step of a process file (`playbooks/jobsearch/build_packet.bpmn`, `build_library.bpmn`) that one
model turn answers: the compose seat, the editor's once-over, the facts gate, a fix, the arbitrator, and the library's
writers and checkers. The process engine waits at the step (an open `ProcWait` of kind `agent`); that open wait is the
seat's place in line, so the order of work stays in the process file. Code here registers what each seat needs
(`Seat`): which model answers it, its instruction file and reply shape (the step's `porter:instructions` and
`porter:template`, the owner's newest saved version of each), what it is handed (`build`), and what code does with
the answer (`answer`), whose result completes the step.

The runner takes a seat over HTTP (`runner_routes`: take, then answer): it runs one Claude turn with the seat's text as
the whole system prompt, no tools and prompt caching off (a one-turn call never reads its cache back), and sends the
reply text back with what the turn cost. Each try is a `SeatRun`: its claim while it runs (`LEASE`), and its cost and
how it ended. A reply that can't be used is a failed try; after `MAX_TRIES` failed tries the process is closed as
failed by its own handler (`on_fail`).

A stop of the app ends the lanes and their sessions with no word of the end, so a run can be left open under a session
that is gone. The app's start notes the open runs and the runner each one's session was on (`note_restart`); such a run
stops holding its seat once that runner is heard idle or in another session (`_stopped`), and is closed as cut short by
the stop, which is not a failed try. It is closed when that word of the runner comes in (`runner_heard`), so a second
start of the app, which no longer knows what the first one noted, finds nothing of it open. A session that lived
through a restart of the app alone keeps its seat, and its lease like any other's.

Services flush; the route that owns the request commits.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from process_engine.models import LIVE, OPEN, WAITING, ProcInstance, ProcWait
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch import process
from app.jobsearch.models import SeatRun
from app.jobsearch.work import hand_outs, instruction_files
from app.models.agent import AgentRunner, RunnerState

log = logging.getLogger(__name__)

OPUS, SONNET = "opus", "sonnet"
#: How long a run holds its seat without answering: past it the seat is free for another session (a library build's
#: largest seat takes about ten minutes).
LEASE = timedelta(minutes=30)
#: Failed tries before the process is closed as failed.
MAX_TRIES = 3
#: How a run ends that the last stop of the app ended (`ok` stays empty: it neither answered nor failed).
STOPPED = "the app was stopped while this ran and its session ended with it; not a failed try"
#: What the runner tells the model with the seat's text as its system prompt.
PROMPT = "Do the task in your instructions with the inputs given there. Reply with JSON only."


class SeatError(DomainError):
    status = 409


class ReplyError(Exception):
    """The seat's reply can't be used: a failed try, with why."""


Build = Callable[[AsyncSession, str, ProcInstance], Awaitable[str]]
Answer = Callable[[AsyncSession, str, ProcInstance, dict], Awaitable[dict]]
Check = Callable[[AsyncSession, str, ProcInstance], Awaitable[bool]]
Fail = Callable[[AsyncSession, str, ProcInstance, str], Awaitable[None]]


@dataclass(frozen=True)
class Seat:
    """One agent step answered in one model turn."""

    process: str
    element: str
    model: str
    label: str
    build: Build
    answer: Answer
    #: The `--effort` the turn runs at; None leaves it to the CLI.
    effort: str | None = None
    #: Whether the seat can be taken now (a packet's compose waits for its story library).
    ready: Check | None = None
    #: Whether it is the owner's own work (asked for, or Launch's), which the pool hands out at any hour.
    owners: Check | None = None
    #: Whether Launch in Browser asked for it: those go first.
    launched: Check | None = None


SEATS: dict[tuple[str, str], Seat] = {}
FAILS: dict[str, Fail] = {}


def register(seat: Seat) -> Seat:
    SEATS[(seat.process, seat.element)] = seat
    return seat


def on_fail(process_key: str, handler: Fail) -> None:
    FAILS[process_key] = handler


# --- the line ---------------------------------------------------------------------------------------------------


@dataclass
class Open:
    wait: ProcWait
    instance: ProcInstance
    seat: Seat


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


async def _open(session: AsyncSession, owner: str) -> list[Open]:
    """Every seat waiting for an answer, oldest first."""
    if not SEATS:
        process.load_actions()
    keys = {k for k, _ in SEATS}
    rows = (await session.execute(
        select(ProcWait, ProcInstance).join(ProcInstance, ProcInstance.id == ProcWait.instance_id).where(
            ProcWait.status == OPEN, ProcWait.kind == "agent", ProcInstance.status == WAITING,
            ProcInstance.mode == LIVE, ProcInstance.process_key.in_(keys))
        .order_by(ProcWait.created_at))).all()
    out = []
    for wait, instance in rows:
        seat = SEATS.get((instance.process_key, wait.element_id))
        if seat is not None and (instance.data or {}).get("owner") == owner:
            out.append(Open(wait, instance, seat))
    return out


#: What this start of the app found (`note_restart`): each run that was open, and the runner its session was on. Kept
#: by this process alone: a run is closed as soon as its runner's word shows it cut (`runner_heard`), and one whose
#: runner has said nothing since is noted again by the next start, its runner's row being as the stop left it.
_CUT: dict[uuid.UUID, str] = {}


async def note_restart(session: AsyncSession, owner: str) -> int:
    """At the app's start: the runs still open are noted with the runner each one's session was on (the runner whose
    last word named that session), so a run whose session the stop ended gives its seat back as soon as that runner is
    heard again (`_stopped`), not when its lease is out. Nothing is closed here: the lanes are programs of their own,
    and after a restart of the app alone (a crash, an app that stopped answering) a session is still writing and
    answers its run. A run no runner names is left to its lease. Returns how many were noted."""
    rows = (await session.scalars(select(SeatRun).where(SeatRun.owner == owner, SeatRun.ended_at.is_(None)))).all()
    on = {sid: name for name, sid in (await session.execute(
        select(AgentRunner.name, AgentRunner.session_id).where(
            AgentRunner.owner == owner, AgentRunner.state == RunnerState.working.value))).all() if sid}
    noted = {r.id: on[r.session_id] for r in rows if r.session_id in on}
    _CUT.update(noted)
    return len(noted)


async def _stopped(session: AsyncSession, runs: Collection[SeatRun]) -> set[uuid.UUID]:
    """Of these runs, the open ones the last stop ended: open at this start of the app (`note_restart`) on a runner that
    has since said it is idle or in another session. A runner has one session at a time and says nothing of being idle
    while one runs, so that session is over; that holds past the run's lease too. A noted run whose runner still names
    its session is not one, however old: the session lived through the restart, and when it gives no answer its lease
    ends it as a failed try (`_close_lapsed`)."""
    cut = [r for r in runs if r.ended_at is None and r.id in _CUT]
    if not cut:
        return set()
    rows = (await session.execute(select(AgentRunner.owner, AgentRunner.name, AgentRunner.session_id).where(
        AgentRunner.name.in_({_CUT[r.id] for r in cut})))).all()
    at = {(owner, name): sid for owner, name, sid in rows}
    return {r.id for r in cut if at.get((r.owner, _CUT[r.id])) != r.session_id}


async def _runs(session: AsyncSession, wait_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[SeatRun]]:
    """The runs of these seats, oldest first, without the ones the last stop ended (`_stopped`): they hold no seat and
    are no tries."""
    if not wait_ids:
        return {}
    rows = (await session.scalars(select(SeatRun).where(SeatRun.wait_id.in_(wait_ids))
                                  .order_by(SeatRun.started_at))).all()
    stopped = await _stopped(session, rows)
    out: dict[uuid.UUID, list[SeatRun]] = {}
    for r in rows:
        if r.id not in stopped:
            out.setdefault(r.wait_id, []).append(r)
    return out


def _held(runs: list[SeatRun], moment: datetime) -> bool:
    return any(r.ended_at is None and r.started_at >= moment - LEASE for r in runs)


def _lapsed(runs: list[SeatRun], moment: datetime) -> list[SeatRun]:
    return [r for r in runs if r.ended_at is None and r.started_at < moment - LEASE]


def _failed(runs: list[SeatRun]) -> int:
    return sum(1 for r in runs if r.ended_at is not None and r.ok is False)


async def _free(session: AsyncSession, owner: str, now: datetime | None = None) -> list[Open]:
    """The seats a session could take now: open, held by no live run, claimed for no other session (`hand_outs.py`),
    and ready."""
    moment = _now(now)
    claimed = await hand_outs.held(session, owner, hand_outs.SEAT, moment)
    found = [o for o in await _open(session, owner) if str(o.wait.id) not in claimed]
    runs = await _runs(session, [o.wait.id for o in found])
    out = []
    for o in found:
        mine = runs.get(o.wait.id, [])
        if _held(mine, moment) or _failed(mine) + len(_lapsed(mine, moment)) >= MAX_TRIES:
            continue
        if o.seat.ready is not None and not await o.seat.ready(session, owner, o.instance):
            continue
        out.append(o)
    return out


async def _ordered(session: AsyncSession, owner: str, now: datetime | None = None, *, mine: Collection = (),
                   scope: str | None = None) -> list[Open]:
    """The seats a session could take now, in the order they would be handed: the one claimed for the caller (`mine`)
    first, Launch's next, then the oldest. `scope` "launch": Launch's alone; "owners": the owner's own work alone (asked
    for by the owner, Launch's, or the library)."""
    free = await _free(session, owner, now)
    launched = [o for o in free if o.seat.launched is not None and await o.seat.launched(session, owner, o.instance)]
    if scope == "launch":
        ordered = launched
    else:
        ordered = launched + [o for o in free if all(o is not x for x in launched)]
        if scope == "owners":
            ordered = [o for o in ordered if o.seat.owners is None or await o.seat.owners(session, owner, o.instance)]
    return hand_outs.claimed_first(ordered, mine, key=lambda o: o.wait.id)


async def free(session: AsyncSession, owner: str, now: datetime | None = None, *, mine: Collection = (),
               scope: str | None = None) -> list[uuid.UUID]:
    """The seats a session could take now, as the ids of their waits, in the order they would be handed (`_ordered`):
    what a claim holds, what makes `seat` the answer to "which work waits" (`session_kinds.next_kind`), and what `take`
    takes from."""
    return [o.wait.id for o in await _ordered(session, owner, now, mine=mine, scope=scope)]


# --- taking and answering -----------------------------------------------------------------------------------------


_COMMENT = re.compile(r"\A\s*<!--.*?-->\s*", re.S)


async def _file(session: AsyncSession, owner: str, kind: str, name: str | None) -> str:
    if not name:
        return ""
    return _COMMENT.sub("", await instruction_files.read(session, owner, kind, name)).strip()


async def system_text(session: AsyncSession, owner: str, o: Open) -> str:
    """The seat's whole system prompt: its instruction file, its reply shape and what it is handed."""
    payload = o.wait.payload or {}
    instructions = await _file(session, owner, "instructions", payload.get("instructions"))
    template = await _file(session, owner, "templates", payload.get("template"))
    inputs = await o.seat.build(session, owner, o.instance)
    shape = (f"\n\n## Your reply\n\nReply with JSON only: the object this shape describes (written here as YAML, with "
             f"notes after #):\n\n{template}") if template else ""
    return f"{instructions}{shape}\n\n# The inputs\n\n{inputs}".strip()


async def take(session: AsyncSession, owner: str, session_id: str = "", now: datetime | None = None, *,
               claim: str | None = None) -> dict:
    """Hand the next seat to a session: its run id, model, effort, system prompt and prompt. The runs the last stop
    ended are closed first (no tries), then the lapsed ones as failed tries. The seat claimed for the session (`claim`,
    `hand_outs.py`) when it is still free, else the next that none is claimed for. `{"seat": None}` when none waits."""
    moment = _now(now)
    await _close_stopped(session, owner, moment)
    await _close_lapsed(session, owner, moment)
    claimed = await hand_outs.take(session, owner, hand_outs.SEAT, claim, session_id, moment) if claim else None
    ordered = await _ordered(session, owner, moment, mine=claimed or ())
    if not ordered:
        return {"seat": None}
    o = ordered[0]
    run = SeatRun(owner=owner, wait_id=o.wait.id, instance_id=o.instance.id, process_key=o.instance.process_key,
                  element=o.wait.element_id, model=o.seat.model, session_id=(session_id or "")[:80],
                  started_at=moment)
    session.add(run)
    await session.flush()
    text = await system_text(session, owner, o)
    return {"seat": {"id": str(run.id), "model": o.seat.model, "effort": o.seat.effort, "label": o.seat.label,
                     "system": text, "prompt": PROMPT}}


async def runner_heard(session: AsyncSession, owner: str, now: datetime | None = None) -> None:
    """A runner's word was noted (a heartbeat, after `roster.note_runner`): the runs the last stop ended, which that
    word can show, are closed now and not at the next take. What this start noted is gone at the next start, and a run
    still open then on a runner already heard idle would hold its seat again and end as a failed try."""
    if _CUT:
        await _close_stopped(session, owner, _now(now))


async def _close_stopped(session: AsyncSession, owner: str, moment: datetime) -> None:
    """The runs the last stop ended (`_stopped`) are closed as cut short by it: no failed try, so nothing is counted
    and no handler runs. The step still waits, with what the seats before it made, and is handed out again."""
    rows = (await session.scalars(select(SeatRun).where(SeatRun.owner == owner, SeatRun.ended_at.is_(None)))).all()
    stopped = await _stopped(session, rows)
    for r in rows:
        if r.id in stopped:
            r.ended_at, r.ok, r.error = moment, None, STOPPED
    await session.flush()


async def _close_lapsed(session: AsyncSession, owner: str, moment: datetime) -> None:
    rows = (await session.scalars(select(SeatRun).where(
        SeatRun.owner == owner, SeatRun.ended_at.is_(None), SeatRun.started_at < moment - LEASE))).all()
    for r in rows:
        r.ended_at, r.ok, r.error = moment, False, "no answer within the lease"
        await _after_failure(session, owner, r)
    await session.flush()


def parse_reply(text: str) -> dict:
    """The JSON object in a model's reply: in a code fence or bare, from its first brace. When the reply holds several
    fenced objects, the last one that reads is the answer: a model that corrects itself mid-reply ("corrected full
    reply below") means its second version. Raises `ReplyError`."""
    t = (text or "").strip()
    for block in reversed(re.findall(r"```(?:json)?\s*(.*?)```", t, re.S)):
        try:
            found = json.loads(block.strip()[block.strip().find("{"):]) if "{" in block else None
        except json.JSONDecodeError:
            continue
        if isinstance(found, dict):
            return found
    fenced = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fenced:
        t = fenced.group(1).strip()
    start = t.find("{")
    if start < 0:
        raise ReplyError("the reply holds no JSON object")
    t = t[start:]
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        try:
            data = json.loads(t[: t.rfind("}") + 1])
        except json.JSONDecodeError as exc:
            raise ReplyError(f"the reply is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReplyError("the reply is not a JSON object")
    return data


def _spend(run: SeatRun, body: dict) -> None:
    spend: dict = body["spend"] if isinstance(body.get("spend"), dict) else {}
    cost = spend.get("cost_usd")
    run.cost_usd = float(cost) if isinstance(cost, int | float) else None
    run.input_tokens = int(spend.get("input_tokens") or 0)
    run.output_tokens = int(spend.get("output_tokens") or 0)


async def answer(session: AsyncSession, owner: str, run_id: str, body: dict, now: datetime | None = None) -> dict:
    """The runner's answer for a run it took: the reply text (`text`), or that the turn failed (`error`), with what it
    cost (`spend`). A usable reply completes the step; one that can't be used is a failed try."""
    moment = _now(now)
    try:
        run = await session.get(SeatRun, uuid.UUID(str(run_id)))
    except ValueError:
        run = None
    if run is None or run.owner != owner:
        raise SeatError("no such seat run")
    if run.ended_at is not None:
        return {"ok": False, "taken": False, "reason": "this run was closed already"}
    run.ended_at = moment
    _spend(run, body)
    wait = await session.get(ProcWait, run.wait_id)
    instance = await session.get(ProcInstance, run.instance_id)
    seat = SEATS.get((run.process_key, run.element))
    if wait is None or instance is None or seat is None or wait.status != OPEN:
        run.ok, run.error = False, "the step is no longer waiting"
        await session.flush()
        return {"ok": False, "taken": False, "reason": run.error}
    try:
        if body.get("error"):
            raise ReplyError(str(body["error"])[:500])
        reply = parse_reply(str(body.get("text") or ""))
        async with session.begin_nested():
            result = await seat.answer(session, owner, instance, reply)
            await process.engine().complete(session, instance.id, run.element, result, wait_id=wait.id)
    except ReplyError as exc:
        run.ok, run.error = False, str(exc)[:2000]
        await session.flush()
        left = await _after_failure(session, owner, run)
        return {"ok": False, "taken": False, "reason": run.error, "tries_left": left}
    run.ok = True
    await session.flush()
    return {"ok": True, "taken": True}


async def _after_failure(session: AsyncSession, owner: str, run: SeatRun) -> int:
    """After a failed try: how many tries are left; with none, the process's handler closes it."""
    failed = await session.scalar(select(func.count()).select_from(SeatRun).where(
        SeatRun.wait_id == run.wait_id, SeatRun.ended_at.is_not(None), SeatRun.ok.is_(False))) or 0
    left = max(MAX_TRIES - failed, 0)
    if left == 0:
        instance = await session.get(ProcInstance, run.instance_id)
        handler = FAILS.get(run.process_key)
        if instance is not None and handler is not None:
            seat = SEATS.get((run.process_key, run.element))
            why = f"the {seat.label if seat else run.element} failed {MAX_TRIES} times: {run.error}"
            await handler(session, owner, instance, why[:500])
    return left


# --- sessions ---------------------------------------------------------------------------------------------------


async def release(session: AsyncSession, owner: str, session_id: str, now: datetime | None = None) -> int:
    """A session ended without answering its run: the try failed, so the seat is free again at once."""
    sid = (session_id or "")[:80]
    if not sid:
        return 0
    rows = (await session.scalars(select(SeatRun).where(
        SeatRun.owner == owner, SeatRun.session_id == sid, SeatRun.ended_at.is_(None)))).all()
    for r in rows:
        r.ended_at, r.ok, r.error = _now(now), False, "the session ended without an answer"
        await _after_failure(session, owner, r)
    await session.flush()
    return len(rows)


async def running(session: AsyncSession, owner: str, now: datetime | None = None) -> list[SeatRun]:
    """The runs going now."""
    moment = _now(now)
    rows = (await session.scalars(select(SeatRun).where(
        SeatRun.owner == owner, SeatRun.ended_at.is_(None), SeatRun.started_at >= moment - LEASE)
        .order_by(SeatRun.started_at))).all()
    stopped = await _stopped(session, rows)
    return [r for r in rows if r.id not in stopped]


async def activity(session: AsyncSession, owner: str) -> str | None:
    """What the newest running seat is doing, in words, or None."""
    runs = await running(session, owner)
    if not runs:
        return None
    seat = SEATS.get((runs[-1].process_key, runs[-1].element))
    return seat.label if seat else None


async def claim_state(session: AsyncSession, instance_id: uuid.UUID | None, now: datetime | None = None) -> str:
    """What a process run's seats are doing: `working` (a session holds its open seat, or code is between seats) or
    `unclaimed` (a seat waits that no session holds)."""
    moment = _now(now)
    if instance_id is None:
        return "unclaimed"
    waits = (await session.scalars(select(ProcWait).where(
        ProcWait.instance_id == instance_id, ProcWait.status == OPEN, ProcWait.kind == "agent"))).all()
    if not waits:
        return "working"
    runs = await _runs(session, [w.id for w in waits])
    return "working" if any(_held(runs.get(w.id, []), moment) for w in waits) else "unclaimed"


async def writer_busy(session: AsyncSession, owner: str, now: datetime | None = None) -> bool:
    """Whether a session holds any of the owner's open seats: the writer lane is working, so a seat that waits is
    waiting its turn."""
    moment = _now(now)
    opens = await _open(session, owner)
    runs = await _runs(session, [o.wait.id for o in opens])
    return any(_held(rs, moment) for rs in runs.values())


async def cost_of(session: AsyncSession, instance_ids: list[uuid.UUID]) -> dict[str, Any]:
    """What the seats of these process runs cost: dollars, tokens, tries and the time from the first try to the last
    answer."""
    rows = (await session.scalars(select(SeatRun).where(SeatRun.instance_id.in_(instance_ids))
                                  .order_by(SeatRun.started_at))).all() if instance_ids else []
    ended = [r.ended_at for r in rows if r.ended_at]
    return {"cost_usd": round(sum(r.cost_usd or 0 for r in rows), 4),
            "input_tokens": sum(r.input_tokens for r in rows), "output_tokens": sum(r.output_tokens for r in rows),
            "runs": [{"element": r.element, "model": r.model, "ok": r.ok, "cost_usd": r.cost_usd,
                      "seconds": round((r.ended_at - r.started_at).total_seconds(), 1) if r.ended_at else None}
                     for r in rows],
            "seconds": round((max(ended) - rows[0].started_at).total_seconds(), 1) if rows and ended else None}


def section(name: str, body: object) -> str:
    """One named part of what a seat is handed."""
    text = body if isinstance(body, str) else json.dumps(body, indent=1, ensure_ascii=False, default=str)
    return f"## {name}\n\n{text}\n\n"
