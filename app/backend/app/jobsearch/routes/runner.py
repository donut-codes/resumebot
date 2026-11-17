"""What the scout's runner says and asks, over HTTP (`app/scout/scout.mjs`): the work to start next, the instruction
files a session starts with, its heartbeat, a seat to take and its answer (`seats.py`), and what is behind it. The
runner's calls carry the agent token
(`Authorization`); `GET /agent/ready` is not here: it is the page's, in `routes/agent_ready.py`."""

from typing import Annotated

from fastapi import APIRouter, Header, Query

from app.agents import roster
from app.db import LifelineSession, ReadSession, Session
from app.errors import DomainError, Unauthorized, Unprocessable
from app.jobsearch.browser import leases as browser_leases
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.browser import tabs as tab_register
from app.jobsearch.doors.scout import token_ok
from app.jobsearch.research import session_context
from app.jobsearch.work import availability, instruction_files, session_kinds, session_lifecycle
from app.jobsearch.writing import seats
from app.settings import settings

router = APIRouter(prefix="/api/v1/agent")


def _scout_only(authorization: str | None) -> None:
    if not token_ok(authorization):
        raise Unauthorized("no valid agent token")


@router.get("/work")
async def agent_work(
    session: Session, authorization: Annotated[str | None, Header()] = None,
    only: Annotated[str | None, Query(max_length=100)] = None,
    runner: Annotated[str | None, Query(max_length=40)] = None,
) -> dict:
    """What the runner should start next: a pasted posting to split, a search, a role to research, a packet, or nothing
    (docs/PRD.md 23.3), with the exact item claimed for it (`claim`, `item`: `session_kinds.hand_out`). It only reads
    what is waiting; the settling and the expiring are the app's own pass (`housekeeping.settle`). The pool is the one
    lane (`only=any`, or none): a runner named `session-<n>` (`runner`) past the owner's limit on active sessions is
    told nothing; research is told only when a role is waiting that no session has taken, and on a site no other session
    is on."""
    _scout_only(authorization)
    if only not in (None, "any"):
        raise Unprocessable(f"the pool is the only lane: only=any, not {only!r}")
    reply = await session_kinds.hand_out(session, settings.owner, runner)
    await session.commit()  # the claim of the item and the answer are one transaction
    return reply


@router.get("/instructions")
async def agent_instructions(
    session: ReadSession, names: Annotated[str, Query(max_length=2000)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """The instruction files a session starts with, by the names the runner asks for (comma-separated, with or without
    the `.instructions.md` suffix): each as the owner's newest saved version, else the shipped file (docs/PRD.md 16.1).
    A name that is no instruction file is left out; the runner then reads its own copy."""
    _scout_only(authorization)
    found: dict[str, dict] = {}
    for ref in (n.strip() for n in names.split(",")):
        try:
            now = await instruction_files.resolve(session, settings.owner, "instructions", ref)
        except DomainError:
            continue
        found[ref] = {"text": now.text, "version": now.version}
    return {"files": found}


@router.get("/context")
async def agent_context(
    session: ReadSession, kind: Annotated[str, Query(max_length=40)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """The owner's stable material for a kind of session (`analysis` or `reader`: `session_context.py`), as text for the
    end of the session's system prompt, with its digest. The runner names the digest in the session's MCP configuration;
    a tool leaves the material out of its replies only for a session that holds the app's current text. A kind with no
    such material answers an empty text."""
    _scout_only(authorization)
    if kind not in session_context.KINDS:
        return {"text": "", "digest": ""}
    text = await session_context.text(session, settings.owner, kind)
    return {"text": text, "digest": session_context.digest(text)}


@router.post("/heartbeat")
async def heartbeat(
    session: LifelineSession, body: dict, authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """The scout's session says it started, is alive, or ended: the page can say it is working. The runner itself says
    `idle` every few minutes while it is up between sessions, so the app knows an agent is there to take work now
    (`GET /api/v1/agent/ready`). `stop` tells the runner to end the session it has (inside the blackout). On the
    lifeline pool (`db.LifelineSession`), so a check-in answers while the shared pool is busy."""
    _scout_only(authorization)
    stop = await session_lifecycle.settle(session, settings.owner, body)
    await session.commit()
    return {"ok": True, "stop": stop}


@router.post("/browser/drain")
async def browser_drain(session: Session, body: dict, authorization: Annotated[str | None, Header()] = None) -> dict:
    """The lanes are stopping (a stop, a release): `{"on": true}` drains the browser workers (no item is handed out
    and no session starts; each finishes the item it holds), `{"on": false}` ends the drain. Answers how many items
    workers still hold, which the lanes wait on (up to about three minutes)."""
    _scout_only(authorization)
    availability.drain(settings.owner, bool((body or {}).get("on")))
    held = await browser_leases.leased_count(session, settings.owner)
    await session.commit()
    return {"ok": True, "draining": availability.draining(settings.owner), "holding": held}


@router.get("/browser/drain")
async def browser_drain_state(session: ReadSession, authorization: Annotated[str | None, Header()] = None) -> dict:
    """How many items browser workers still hold, and whether a drain is on. Reads only: a lease that ran out is
    counted until the engine's minute tick gives it back (`browser_leases.held_count`)."""
    _scout_only(authorization)
    held = await browser_leases.held_count(session, settings.owner)
    return {"ok": True, "draining": availability.draining(settings.owner), "holding": held}


@router.get("/browser/busy")
async def browser_busy(session: ReadSession, authorization: Annotated[str | None, Header()] = None) -> dict:
    """What keeps the app's Chrome open (docs/PRD.md 52.9): `leased`, the browser items workers hold, and `waiting`, the
    items a session or the app's code could be handed now (`browser_queue.open_work`). The bridge asks it before it
    closes the app's Chrome for want of work. Reads only."""
    _scout_only(authorization)
    work = await browser_queue.open_work(session, settings.owner)
    return {"ok": True, **work}


@router.get("/browser/tabs")
async def browser_tabs(session: ReadSession, authorization: Annotated[str | None, Header()] = None) -> dict:
    """The tab register (docs/PRD.md path 52): every tab the app's Chrome has open for the app."""
    _scout_only(authorization)
    rows = await tab_register.tabs(session, settings.owner)
    await session.commit()
    return {"tabs": [tab_register.shown(r) for r in rows]}


@router.post("/browser/tabs")
async def browser_tab_opened(session: Session, body: dict,
                             authorization: Annotated[str | None, Header()] = None) -> dict:
    """The bridge says a browser session opened a tab: `{target_id, owner_id, kind}`
    (the session's id is `owner_id`)."""
    _scout_only(authorization)
    target, owner_id = str((body or {}).get("target_id") or ""), str((body or {}).get("owner_id") or "")
    if not target or not owner_id:
        raise Unprocessable("a tab needs its target_id and owner_id")
    try:
        await tab_register.add(session, settings.owner, target, str((body or {}).get("kind") or "session"), owner_id)
    except ValueError:
        raise Unprocessable("the kind of a tab is session, code or go") from None
    await session.commit()
    return {"ok": True}


@router.delete("/browser/tabs/{target_id}")
async def browser_tab_closed(target_id: str, session: Session,
                             authorization: Annotated[str | None, Header()] = None) -> dict:
    """The bridge says a tab is gone."""
    _scout_only(authorization)
    gone = await tab_register.remove(session, target_id)
    await session.commit()
    return {"ok": True, "removed": gone}


@router.post("/browser/tabs/release")
async def browser_tabs_released(session: Session, body: dict,
                                authorization: Annotated[str | None, Header()] = None) -> dict:
    """The bridge says a session's connection is gone, and with it its tabs: they come off the register. (The bridge has
    closed them; the app names none to it again.)"""
    _scout_only(authorization)
    owner_id = str((body or {}).get("owner_id") or "")
    ids = await tab_register.release_owner(session, settings.owner, owner_id) if owner_id else []
    await session.commit()
    return {"ok": True, "released": len(ids)}


@router.post("/browser/tabs/reconcile")
async def browser_tabs_reconcile(session: Session, body: dict,
                                 authorization: Annotated[str | None, Header()] = None) -> dict:
    """The bridge started: no session or code job is connected, so their rows go, and the Launch tabs that are over. The
    answer is the tabs to keep; the bridge closes every other page of the app's own profile."""
    _scout_only(authorization)
    keep = await tab_register.keep_list(session, settings.owner, reason="bridge")
    await session.commit()
    return {"keep": keep}


@router.post("/capabilities")
async def capabilities(session: Session, body: dict, authorization: Annotated[str | None, Header()] = None) -> dict:
    """The runner states once on start what is behind the scout (model, where it runs, its limits)."""
    _scout_only(authorization)
    await roster.declare(session, settings.owner, roster.SCOUT, body if isinstance(body, dict) else {})
    await session.commit()
    return {"ok": True}


@router.post("/seat/take")
async def seat_take(session: Session, body: dict, authorization: Annotated[str | None, Header()] = None) -> dict:
    """The next seat for a session of the writer's kind (`seats.take`): its run id, model, effort, system prompt and
    prompt, or `{"seat": null}` when none waits. The session's id (`session`) holds it until it answers."""
    _scout_only(authorization)
    reply = await seats.take(session, settings.owner, str((body or {}).get("session") or ""),
                             claim=str((body or {}).get("claim") or "") or None)
    await session_kinds.saw(session, settings.owner, session_kinds.SEAT, empty=reply["seat"] is None,
                           sid=str((body or {}).get("session") or "") or None)
    await session.commit()
    return reply


@router.post("/seat/{run_id}/answer")
async def seat_answer(run_id: str, session: Session, body: dict,
                      authorization: Annotated[str | None, Header()] = None) -> dict:
    """A seat's answer (`seats.answer`): the turn's reply text (`text`) or why it failed (`error`), with what it cost
    (`spend`). A usable reply completes the process's step; one that can't be used is a failed try."""
    _scout_only(authorization)
    reply = await seats.answer(session, settings.owner, run_id, body if isinstance(body, dict) else {})
    await session.commit()
    return reply
