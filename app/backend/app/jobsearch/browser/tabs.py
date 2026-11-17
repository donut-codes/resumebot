"""The tab register (docs/PRD.md path 52): every tab the app's Chrome has open for the app is a row in
`js_open_tabs`, and cleanup follows the rows.

The bridge (`app/scout/own-chrome.mjs`) is the only holder of the DevTools connection, so closing a tab means naming it
to the bridge (`close_targets`: `POST /tabs/close`, loopback only). Rows are written when a tab opens (by the bridge for
a browser session's tab, by `browser_code._DevTools` for the app's own code, with the role for a Launch tab) and
deleted when it closes. A tab is closed by the event that ends what it was for:

- a session ends (`session_ended`: the runner's `ended` heartbeat),
- a Launch tab's role leaves Ready to Launch (`_note_departures` and `close_for_roles`, whichever code moved the role),
- the bridge or the app starts (`reconcile_at_app_start`: every page tab of the app's own profile that the register
  does not hold is closed, and the rows whose tab is gone are dropped).

`expired` is the housekeeping pass's part: what the register itself says is over (a Launch tab 24 hours old, a Launch
tab whose role is not in Ready to Launch, a session's or code job's tab past `TAB_MAX_HOURS`). There is no sweeper of
anything the register does not hold.

Every write is a short transaction of its own, a failed write is logged and never stops the work, and the bridge is
called only outside a transaction (`db.outside_transaction`).
"""

import asyncio
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, event, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm.base import PASSIVE_NO_INITIALIZE

from app import db
from app.jobsearch import stages
from app.jobsearch.models import OpenTab, Opportunity
from app.jobsearch.states import TabKind
from app.jobsearch.work import engine_calls, queue
from app.settings import settings

log = logging.getLogger(__name__)

#: How long a Launch tab stays open when its role is still in Ready to Launch (the owner, 2026-10-29).
GO_HOURS = 24
#: A session's or a code job's tab older than this is closed by the housekeeping pass: a backstop for a session that
#: never said it ended (its own end and its connection's end close its tabs at once).
TAB_MAX_HOURS = 3
#: What a call to the bridge may take.
BRIDGE_SECONDS = 5.0


# --- the bridge --------------------------------------------------------------------------------------------------


async def bridge_post(path: str, body: dict) -> dict | None:
    """Ask the bridge for something (`/tabs/close`, `/tabs/reconcile`); its answer, or None when it is not there or
    refuses. Never raises: a tab the bridge could not close is found again at its next start."""
    db.outside_transaction("a call to the browser bridge")
    try:
        async with httpx.AsyncClient(timeout=BRIDGE_SECONDS) as client:
            reply = await client.post(f"http://127.0.0.1:{settings.bridge_port()}{path}", json=body,
                                           headers={"X-Bridge-Key": settings.bridge_key()})
        return reply.json() if reply.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def close_targets(ids: Iterable[str]) -> int:
    """Close these tabs in the app's Chrome, by the bridge; how many it closed."""
    ids = [i for i in ids if i]
    if not ids:
        return 0
    reply = await bridge_post("/tabs/close", {"targets": ids})
    return int((reply or {}).get("closed") or 0)


_tasks: set[asyncio.Task] = set()


def close_soon(ids: Iterable[str]) -> None:
    """Close these tabs in the background, once the caller's transaction has committed. Nothing when there is no event
    loop (a script)."""
    ids = [i for i in ids if i]
    if not ids:
        return
    engine_calls.start("tabs_close", ids)


def _start_close(ids: list[str]) -> None:
    """What `engine_calls.start("tabs_close", ids)` runs where the local door is installed: the closing task, kept in
    `_tasks`. Nothing when there is no event loop (a script)."""
    try:
        task = asyncio.get_running_loop().create_task(close_targets(ids))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


engine_calls.register("tabs_close", _start_close)


async def settle() -> None:
    """Wait for the closes that were started in the background (the tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# --- the rows ----------------------------------------------------------------------------------------------------

#: Whether any Launch tab has been registered (or was at start): a role leaving Ready to Launch looks for its tab
#: only then.
_go_rows = False


async def add(session: AsyncSession, owner: str, target_id: str, kind: str, owner_id: str,
              role_id: uuid.UUID | None = None, now: datetime | None = None) -> None:
    """Register a tab (again: the same target id keeps one row, the newest word on it). The caller commits."""
    global _go_rows
    kind = TabKind(kind).value
    row = await session.get(OpenTab, target_id)
    when = now or datetime.now(UTC)
    if row is None:
        session.add(OpenTab(target_id=target_id[:80], owner=owner, owner_id=owner_id[:120], kind=kind, role_id=role_id,
                            opened_at=when))
    else:
        row.owner_id, row.kind, row.role_id = owner_id[:120], kind, role_id or row.role_id
    if kind == TabKind.GO:
        _go_rows = True
    await session.flush()


async def remove(session: AsyncSession, target_id: str) -> bool:
    """Take a tab off the register (it closed). The caller commits."""
    with queue.exempt("no loop reads the tab register: a row only says what is open"):
        gone = await session.execute(delete(OpenTab).where(OpenTab.target_id == target_id))
    return bool(gone.rowcount)  # pyright: ignore[reportAttributeAccessIssue]


async def release_owner(session: AsyncSession, owner: str, owner_id: str) -> list[str]:
    """Take every tab of this owner (a session's id, a code job) off the register; the target ids it held. The caller
    commits, then closes them (`close_targets`)."""
    rows = (await session.scalars(select(OpenTab.target_id).where(
        OpenTab.owner == owner, OpenTab.owner_id == owner_id))).all()
    if rows:
        with queue.exempt("no loop reads the tab register: a row only says what is open"):
            await session.execute(delete(OpenTab).where(OpenTab.target_id.in_(rows)))
    return list(rows)


async def tabs(session: AsyncSession, owner: str) -> list[OpenTab]:
    return list((await session.scalars(select(OpenTab).where(OpenTab.owner == owner)
                                       .order_by(OpenTab.opened_at))).all())


def shown(row: OpenTab) -> dict:
    return {"target_id": row.target_id, "owner_id": row.owner_id, "kind": row.kind,
            "role_id": str(row.role_id) if row.role_id else None, "opened_at": row.opened_at.isoformat()}


# --- what the app's own code does as it opens and closes a tab ---------------------------------------------------


async def opened(target_id: str, kind: str, owner_id: str, role_id: uuid.UUID | None = None) -> None:
    """The app's own code opened a tab: register it, in a short transaction of its own. Never raises."""
    try:
        async with db.session_factory()() as session:
            await add(session, settings.owner, target_id, kind, owner_id, role_id)
            await session.commit()
    except Exception:
        log.warning("tab register: could not record tab %s", target_id, exc_info=True)


async def closed(target_id: str) -> None:
    """The app's own code closed a tab: take it off the register. Never raises."""
    try:
        async with db.session_factory()() as session:
            await remove(session, target_id)
            await session.commit()
    except Exception:
        log.warning("tab register: could not remove tab %s", target_id, exc_info=True)


# --- events ------------------------------------------------------------------------------------------------------


async def session_ended(session: AsyncSession, owner: str, session_id: str) -> None:
    """A browser session ended: its tabs are off the register now, and closed in the background after the commit."""
    ids = await release_owner(session, owner, session_id)
    if ids:
        db.after_commit(session, lambda: close_soon(ids))


async def _go_tabs_to_drop(session: AsyncSession, owner: str, now: datetime) -> list[str]:
    """The Launch tabs that are over: 24 hours old, or whose role is not in Ready to Launch (any more), or has gone."""
    rows = await session.execute(
        select(OpenTab.target_id).join(Opportunity, Opportunity.id == OpenTab.role_id, isouter=True)
        .where(OpenTab.owner == owner, OpenTab.kind == TabKind.GO,
               (OpenTab.opened_at < now - timedelta(hours=GO_HOURS)) | (OpenTab.role_id.is_(None))
               | (Opportunity.status != stages.READY_TO_LAUNCH)))
    return [r[0] for r in rows]


async def expired(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """The housekeeping pass's part: what the register itself says is over comes off it and is closed after the commit
    (a Launch tab 24 hours old or whose role is not in Ready to Launch; a session's or code job's tab past
    `TAB_MAX_HOURS`). Returns how many."""
    moment = now or datetime.now(UTC)
    ids = await _go_tabs_to_drop(session, owner, moment)
    ids += list((await session.scalars(select(OpenTab.target_id).where(
        OpenTab.owner == owner, OpenTab.kind != TabKind.GO,
        OpenTab.opened_at < moment - timedelta(hours=TAB_MAX_HOURS)))).all())
    if ids:
        with queue.exempt("no loop reads the tab register: a row only says what is open"):
            await session.execute(delete(OpenTab).where(OpenTab.target_id.in_(ids)))
        db.after_commit(session, lambda: close_soon(ids))
    return len(ids)


async def close_for_roles(owner: str, role_ids: Iterable[uuid.UUID]) -> int:
    """These roles left Ready to Launch: their Launch tabs come off the register and are closed. A role that is back
    in Ready to Launch keeps its tab."""
    role_ids = list(role_ids)
    ids: list[str] = []
    try:
        async with db.session_factory()() as session:
            rows = await session.execute(
                select(OpenTab.target_id).join(Opportunity, Opportunity.id == OpenTab.role_id)
                .where(OpenTab.owner == owner, OpenTab.kind == TabKind.GO, OpenTab.role_id.in_(role_ids),
                       Opportunity.status != stages.READY_TO_LAUNCH))
            ids = [r[0] for r in rows]
            if ids:
                with queue.exempt("no loop reads the tab register: a row only says what is open"):
                    await session.execute(delete(OpenTab).where(OpenTab.target_id.in_(ids)))
            await session.commit()
    except Exception:
        log.warning("tab register: could not close the tabs of roles that left Ready to Launch", exc_info=True)
        return 0
    return await close_targets(ids)


def _start_roles(key: tuple[str, Iterable[uuid.UUID]]) -> None:
    """What `engine_calls.start("tabs_roles", (owner, roles))` runs where the local door is installed: the task that
    closes those roles' Launch tabs, kept in `_tasks`. Nothing when there is no event loop (a script)."""
    owner, roles = key
    try:
        task = asyncio.get_running_loop().create_task(close_for_roles(owner, roles))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


engine_calls.register("tabs_roles", _start_roles)


_LEFT = "tab_register_left_ready"


@event.listens_for(OrmSession, "before_flush")
def _note_departures(session: OrmSession, _flush_context, _instances) -> None:
    """A role whose status leaves Ready to Launch, whichever code moved it: remembered until the transaction commits."""
    if not _go_rows:
        return
    for obj in list(session.dirty):
        if isinstance(obj, Opportunity):
            history = inspect(obj).get_history('status', PASSIVE_NO_INITIALIZE)
            if history.deleted and history.deleted[0] == stages.READY_TO_LAUNCH and history.added \
                    and history.added[0] != stages.READY_TO_LAUNCH:
                session.info.setdefault(_LEFT, {}).setdefault(obj.owner, set()).add(obj.id)


@event.listens_for(OrmSession, "after_commit")
def _after_commit(session: OrmSession) -> None:
    left = session.info.pop(_LEFT, None)
    if not left:
        return
    for owner, roles in left.items():
        engine_calls.start("tabs_roles", (owner, roles))


@event.listens_for(OrmSession, "after_rollback")
def _forget(session: OrmSession) -> None:
    session.info.pop(_LEFT, None)


# --- start: the register is what stays ---------------------------------------------------------------------------


async def keep_list(session: AsyncSession, owner: str, *, reason: str, now: datetime | None = None) -> list[str]:
    """Settle the register at a start and say which tabs stay open. At the bridge's start (`bridge`) no session or code
    job is connected any more, so their rows go; at the app's (`app`) the code jobs are gone, and the sessions stay.
    Launch tabs that are over go either way. The caller commits. Returns the target ids to keep."""
    moment = now or datetime.now(UTC)
    drop = await _go_tabs_to_drop(session, owner, moment)
    kinds = [TabKind.CODE, TabKind.SESSION] if reason == "bridge" else [TabKind.CODE]
    drop += list((await session.scalars(select(OpenTab.target_id).where(
        OpenTab.owner == owner, OpenTab.kind.in_(kinds)))).all())
    if drop:
        with queue.exempt("no loop reads the tab register: a row only says what is open"):
            await session.execute(delete(OpenTab).where(OpenTab.target_id.in_(drop)))
    return [t.target_id for t in await tabs(session, owner)]


async def drop_missing(session: AsyncSession, owner: str, gone: Iterable[str]) -> None:
    """Rows whose tab the bridge found gone."""
    gone = list(gone)
    if gone:
        with queue.exempt("no loop reads the tab register: a row only says what is open"):
            await session.execute(delete(OpenTab).where(OpenTab.owner == owner, OpenTab.target_id.in_(gone)))


async def reconcile_at_app_start(owner: str) -> dict | None:
    """The app started: the code jobs it had are gone, so their tabs are, and every page tab of the app's own profile
    that the register does not hold is closed (`/tabs/reconcile`); the rows whose tab is gone are dropped. Nothing is
    closed when the bridge is not up. Never raises."""
    global _go_rows
    try:
        async with db.session_factory()() as session:
            keep = await keep_list(session, owner, reason="app")
            _go_rows = bool(await session.scalar(select(OpenTab.target_id).where(OpenTab.kind == TabKind.GO).limit(1)))
            await session.commit()
        reply = await bridge_post("/tabs/reconcile", {"keep": keep})
        if reply and reply.get("gone"):
            async with db.session_factory()() as session:
                await drop_missing(session, owner, reply["gone"])
                await session.commit()
        return reply
    except Exception:
        log.warning("tab register: the start-up reconcile failed", exc_info=True)
        return None
