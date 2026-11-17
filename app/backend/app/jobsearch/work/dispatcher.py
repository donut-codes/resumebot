"""The dispatcher (docs/PRD.md path 47, step 2): the app checks what work is ready and commands a waiting lane, instead
of every lane asking the app every few minutes.

One pass: the pool's runners that are up and idle (`AgentRunner`, heard from lately, with no claim waiting to be taken)
are taken in order, and for each the app decides the next work for the pool lane exactly as an ask does
(`session_kinds.hand_out`: the same limits, the same order, the exact item claimed on a lease in the same transaction).
The first runner told nothing ends the pass: what waits for one waits for all, so a quiet app costs one check, not one
per lane. The transaction is committed, and only then is each claimed runner sent its command (`wake.command`, the
signed wake the runner already takes, carrying the claim). A command the runner did not accept (busy, held at Claude's
usage limit, down) gives its claim back at once; one that was lost on the way lapses with its lease, and the runner's
own slow check-in is answered with the claim made for it (`hand_outs.pending`). The order of work is the process files'
and the limits are the ones that exist (`session_kinds.py`); this only delivers.

The pass runs from the work queue (`work/queue/handlers.py`: the `ready.<kind>` to-dos, a lane's `dispatch.idle` and the
minute tick). It is not repeated for `QUIET` after a pass that found no work."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.agents import roster
from app.jobsearch.work import hand_outs, session_kinds, wake
from app.models.agent import RunnerState
from app.settings import settings

log = logging.getLogger(__name__)

#: How long the pass waits before it looks again after a pass that found nothing to hand out.
QUIET = timedelta(minutes=1)
_quiet_until: dict[str, datetime] = {}
_passing = asyncio.Lock()  # one pass at a time: two at once would claim for the same idle lane
#: When the last pass finished (it looked, whether or not it found work), for the action door's `health`.
last_pass: datetime | None = None


def forget() -> None:
    """Drop the pace (the tests, between cases)."""
    _quiet_until.clear()


async def idle_runners(session: AsyncSession, owner: str, now: datetime | None = None) -> list[str]:
    """The pool's runners to command now, by number: heard from lately, between sessions, with no claim made for them
    that they have not taken."""
    moment = now or datetime.now(UTC)
    told = await hand_outs.runners_waiting(session, owner, moment)
    names = [r.name for r in await roster.runner_rows(session, owner)
             if r.name.startswith(roster.POOL_PREFIX + "-") and r.state == RunnerState.idle
             and moment - r.seen_at <= roster.LIVE_FOR and r.name not in told]
    return sorted(names, key=lambda n: (len(n), n))


async def decide(session: AsyncSession, owner: str, names: list[str]) -> list[dict]:
    """Claim work for these idle runners, one claim each, in this transaction (the caller commits). Each is
    `{runner, claim, kind, item, model}`. The claims are one transaction and are committed together, then commanded: a
    runner's own ask ends the transaction of its walk before it claims (`session_kinds.hand_out`), and here that would
    commit the first lane's claim when the second lane's ask began, so a pass that failed after it would leave a claim
    held for its lease with no command sent for it. The transaction therefore stays whole (`db.holding`)."""
    out: list[dict] = []
    with db.holding("the dispatcher's claims, which the pass commits together"):
        for name in names:
            reply = await session_kinds.hand_out(session, owner, name)
            if not reply.get("claim"):
                break  # nothing is waiting for the pool (or this runner is past the owner's limit, and so are the rest)
            out.append({"runner": name, "claim": reply["claim"], "kind": reply["next"], "item": reply["item"],
                        "model": reply.get("model")})
    return out


async def settle_commands(owner: str, commands: list[dict]) -> int:
    """Send each command and, in a transaction of its own, give back the claims whose runner did not accept it. Returns
    how many runners took one. Nothing here is inside the transaction that made the claims."""
    refused = [c["claim"] for c in commands
               if not await wake.command(c["runner"], c["kind"], c["claim"], c["item"], c["model"])]
    if refused:
        async with db.session_factory()() as session:
            for claim in refused:
                await hand_outs.release(session, owner, claim)
            await session.commit()
    return len(commands) - len(refused)


@db.background_work
async def dispatch_pass(owner: str | None = None, now: datetime | None = None) -> int:
    """One pass of the dispatcher. Never raises: the loop must outlive a bad pass. Returns how many runners were
    commanded. Does nothing without a wake address (a runner is then found by its check-in alone) or inside `QUIET`
    of a pass that found nothing."""
    global last_pass
    who = owner or settings.owner
    moment = now or datetime.now(UTC)
    if not settings.wake_url or moment < _quiet_until.get(who, moment):
        return 0
    try:
        async with _passing, db.session_factory()() as session:
            names = await idle_runners(session, who, moment)
            commands = await decide(session, who, names) if names else []
            await session.commit()
        last_pass = datetime.now(UTC)
        if names and not commands:
            _quiet_until[who] = moment + QUIET  # looked, and nothing waits: not again for a minute
        if not commands:
            return 0
        return await settle_commands(who, commands)
    except Exception:
        log.warning("dispatch failed", exc_info=True)
        return 0


async def dispatch_now(owner: str | None = None) -> int:
    """A pass at once, for when work was just made ready (a schedule or mission fired, `schedule.send_knocks`): the pace
    of a pass that found nothing is dropped, so the new work does not wait for the next housekeeping pass or the quiet
    minute. Same pass, same limits."""
    _quiet_until.pop(owner or settings.owner, None)
    return await dispatch_pass(owner)
