"""The roster: the agents on this install, what they hold, and how each looks right now.

An agent is an `AgentConnection` (one row), its roles are rows of `agent_roles` (data: a Writer or a Reviewer is a new
row, no code), and what it is doing is read from the open search run. The scout is the one shipped agent:
`ensure_roster` makes its role and its connection on start. Adapted from another project's code (app/PROVENANCE.md),
`agents/service.py` and `agents/capacity.py`, which read plan tasks and projects; here the work an agent holds is the
open search run.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as lite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.models import Opportunity, SearchRun
from app.jobsearch.work import queue
from app.models.agent import AgentConnection, AgentRunner, RoleDef, RunnerState
from app.models.work import TrailEvent
from app.settings import settings
from app.timeutil import iso_utc

#: A scout session at work on a site project (docs/PRD.md path 29), as the runner names it.
SITE = "site"
#: A small Haiku session judging the uncertain postings' titles (docs/PRD.md 30.1), as the runner names it.
TITLES = "titles"
#: A small Haiku session splitting a posting the owner pasted into a target role's fields (`jobs/parse.py`), as the
#: runner names it.
PARSER = "parser"
#: A small Haiku session judging the owner's role tabs in a dry run of the title check (`jobs/check.py`), as the runner
#: names it.
CHECKER = "checker"
#: A session of the analyst's role that finds the gaps between the postings for a role and the master resume, or drafts
#: a line from the owner's words (`gaps/tools.py`), as the runner names it.
GAPS = "gaps"
#: A session of the analyst's role that researches one employer (`research/employer.py`, the employer researcher of
#: docs/PRD.md path 36), with an instruction file of its own, as the runner names it.
RESEARCHER = "researcher"
#: A session of the analyst's role that classes the owner's job mail the rules could not settle (`mail/haiku.py`, the
#: mail reader of docs/PRD.md path 14), with an instruction file of its own, as the runner names it.
MAIL = "mail"
#: A session of the coach kind (`coach.py`, docs/PRD.md 15.2): it prepares the owner for an interview, or reads that
#: report as the interviewer would, as the runner names it.
COACH = "coach"
#: A pool session that works the browser's queue in the app's own browser (`browser/items.py`: source, read and scan
#: items), and one that maps a site the browser has no runbook for (the Pathfinder: map items), as the runner names
#: them (the owner, 2026-10-15: the browser runs in the pool).
BROWSER_READER, PATHFINDER = "browser_reader", "pathfinder"
BROWSER_KINDS = (BROWSER_READER, PATHFINDER)
#: What no role of the runner ever does. Each role's words end with it, so the Agents page, which shows the runner's
#: three roles together, can say it once (`may_do`) while How they work still gives each role its whole account.
NEVER = "Never applies, fills a form or sends anything."
SCOUT = "scout"
SCOUT_ROLE = RoleDef(
    key=SCOUT, title="Scout", sort=10,
    about=(
        "Maps how each site works (search, filters, recency) and finds employers' job boards, so the app can read "
        "them itself. "
        + NEVER
    ),
)
WRITER = "writer"
WRITER_ROLE = RoleDef(
    key=WRITER, title="Writer", sort=20,
    about=(
        "Writes a role's application packet from the master resume: a tailored resume, a cover letter when one is "
        "warranted, and the pay ask's inputs, citing the master line behind every claim. " + NEVER
    ),
)
ANALYST = "analyst"
ANALYST_ROLE = RoleDef(
    key=ANALYST, title="Analyst", sort=15,
    about=(
        "Follows behind the scout: researches a found role's employer, pay and full posting, classes each required "
        "and each preferred item against the master resume, and re-judges the score. " + NEVER
    ),
)
SCOUT_DESCRIPTION = (
    "Maps how each site works and finds employers' job boards, so the app reads them itself every night. It judges "
    "no posting: the app de-duplicates, applies your gates and makes a role of what is left."
)
#: A session that has said nothing for this long while it holds a search is "stuck": the page says so.
STUCK_AFTER = timedelta(minutes=30)
#: The scout's own session heartbeat counts as "checked in" for this long.
LIVE_FOR = timedelta(minutes=10)
#: A runner (`AgentRunner`) that has said nothing for `LIVE_FOR` is not seen: the page says since when. One not heard
#: from for this long is dropped, as its lane was turned off.
RUNNER_KEPT = timedelta(days=1)
#: The session pool's runners are named `session-1` to `session-5` (`SCOUT_RUNNER`, set by `app/scout/lanes-local.mjs`).
POOL_PREFIX = "session"


async def ensure_roster(session: AsyncSession, owner: str) -> AgentConnection:
    """Make the scout's, analyst's and writer's roles, and the runner's connection (which holds all three), if they are
    not there. The three roles are shipped, so their words are the code's: an older install's are brought up to date.
    Safe on every start; never changes what the owner set (the pause stays)."""
    for role in (SCOUT_ROLE, ANALYST_ROLE, WRITER_ROLE):
        row = await session.get(RoleDef, role.key)
        if row is None:
            session.add(RoleDef(key=role.key, title=role.title, about=role.about, sort=role.sort))
        elif row.about != role.about:
            row.about = role.about
    conn = await connection_by_slug(session, owner, SCOUT)
    if conn is None:
        conn = AgentConnection(owner=owner, slug=SCOUT, name="Scout", description=SCOUT_DESCRIPTION,
                               roles=[SCOUT, ANALYST, WRITER])
        session.add(conn)
    else:
        # The runner writes packets (docs/PRD.md path 10) and researches (23.3) too: an older install gains the roles.
        missing = [r for r in (ANALYST, WRITER) if r not in (conn.roles or [])]
        if missing:
            conn.roles = [*(conn.roles or []), *missing]
        # What the scout is for is shipped words too, like the roles' above: nothing lets the owner change them, and an
        # older install is brought up to date, so the page never keeps describing a search that is gone.
        conn.description = SCOUT_DESCRIPTION
    await session.flush()
    return conn


async def connection_by_slug(session: AsyncSession, owner: str, slug: str) -> AgentConnection | None:
    return await session.scalar(
        select(AgentConnection).where(AgentConnection.owner == owner, AgentConnection.slug == slug)
    )


async def touch(session: AsyncSession, owner: str, slug: str = SCOUT, *, alive: bool = False,
                kind: str | None = None, ended: bool = False) -> None:
    """Note that this agent called the app (and, with `alive`, that its session says it is running, as `kind`: a runner
    word the caller has told apart, `session_kinds.of_runner`). `ended` clears the kind: the runner is between
    sessions."""
    conn = await connection_by_slug(session, owner, slug)
    if conn is None:
        conn = await ensure_roster(session, owner)
    now = datetime.now(UTC)
    conn.last_seen_at = now
    if alive:
        conn.checked_in_at = now
        if kind is not None:
            conn.session_kind = kind
    if ended:
        conn.session_kind = None
    await session.flush()


async def present(session: AsyncSession, owner: str, slug: str = SCOUT) -> None:
    """The runner says it is up and between sessions (its heartbeat state `idle`, every few minutes). It counts as
    checked in (`checked_in_at`) and as holding no session, but it is not a call about any work: `last_seen_at` stays
    as it was, because a failed wake is retried until the scout has called the app since (`sites/wake_ops.py`)."""
    conn = await connection_by_slug(session, owner, slug)
    if conn is None:
        conn = await ensure_roster(session, owner)
    conn.checked_in_at = datetime.now(UTC)
    conn.session_kind = None
    await session.flush()


def runner_name(body: dict) -> str:
    """The name a heartbeat gives its runner (`runner`), else its lane's (`lane`), else `scout`: a runner with no lane
    says neither, and is the only one. Lower case, letters, digits, `-`, `_` and `+` only."""
    for key in ("runner", "lane"):
        value = body.get(key)
        name = re.sub(r"[^a-z0-9+_-]", "", value.strip().lower())[:60] if isinstance(value, str) else ""
        if name:
            return name
    return SCOUT


async def note_runner(session: AsyncSession, owner: str, body: dict, kind: str | None = None,
                      now: datetime | None = None) -> None:
    """Note a heartbeat against the runner that sent it (`AgentRunner`), so the page can show every runner's session
    and not only the connection's one. `started` and `alive` mean it is in a session of this kind (the session's start
    is when the app first heard of it); `ended` that it is not, if it is that session that ended (a late word of an
    earlier session changes nothing); `idle` that it is up between sessions. Any heartbeat says the runner was heard.
    A runner not heard from for `RUNNER_KEPT` is dropped. Services flush; the route commits."""
    moment = now or datetime.now(UTC)
    name = runner_name(body)
    state = body.get("state")
    session_id = str(body.get("session_id") or "")[:80] or None
    with queue.exempt("a runner not heard for a day is dropped: no loop reads the ones that are gone"):
        await session.execute(delete(AgentRunner).where(AgentRunner.owner == owner,
                                                        AgentRunner.seen_at < moment - RUNNER_KEPT))
    # The row is made if it is not there (two first words at once make one: the other does nothing), then locked for
    # the one that changes it. A runner that is new is idle, so the dispatcher looks at it (`dispatch.idle`).
    made = await session.execute(
        lite_insert(AgentRunner)
        .values(id=uuid.uuid4(), owner=owner, name=name, state=RunnerState.idle.value, since=moment, seen_at=moment)
        .on_conflict_do_nothing(index_elements=["owner", "name"]).returning(AgentRunner.id))
    queue.post(session, owner, queue.DISPATCH_IDLE, [name] if made.first() is not None else [],
               cause="core:roster.note_runner")
    row = (await session.execute(select(AgentRunner).where(AgentRunner.owner == owner, AgentRunner.name == name)
                                 .execution_options(populate_existing=True))).scalar_one()
    row.seen_at = moment
    if state in ("started", "alive"):
        if row.state != RunnerState.working or row.session_id != session_id:
            row.state, row.kind, row.session_id, row.since = RunnerState.working.value, kind, session_id, moment
    elif state == "idle" or (state == "ended" and row.session_id == session_id):
        if row.state != RunnerState.idle:
            row.state, row.kind, row.session_id, row.since = RunnerState.idle.value, None, None, moment
    await session.flush()


async def declare(session: AsyncSession, owner: str, slug: str, facts: dict) -> None:
    """What the runner says about itself (model, where it runs, its limits): kept as plain text facts only."""
    conn = await connection_by_slug(session, owner, slug) or await ensure_roster(session, owner)
    keep = {}
    for key in ("model_name", "hosting", "runtime", "engine", "usage_limit", "host"):
        value = facts.get(key)
        if isinstance(value, str) and value.strip():
            keep[key] = value.strip()[:200]
    for key in ("concurrency", "context_window_tokens"):
        value = facts.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            keep[key] = value
    conn.declared = keep
    conn.declared_at = datetime.now(UTC)
    conn.last_seen_at = conn.declared_at
    await session.flush()


@dataclass
class Work:
    """What an agent holds right now: the open search run, or the analyst's or writer's session (its kind and a title
    for the task it is on)."""

    run: SearchRun | None = None
    kind: str | None = None
    title: str | None = None


async def open_run(session: AsyncSession, owner: str) -> SearchRun | None:
    """The owner's newest search that is still open, or None."""
    return await session.scalar(
        select(SearchRun).where(SearchRun.owner == owner, SearchRun.status == "open")
        .order_by(SearchRun.started_at.desc()).limit(1)
    )


def resume_if_stale(run: SearchRun, now: datetime | None = None) -> bool:
    """A search nothing has called about for `STUCK_AFTER` is orphaned (its session ended or never came back), and the
    scout's next check-in picks it up: the session about to be handed it is its session now, so it stops reading
    "not responding", the sweep's clock (`run.settle_stuck`) starts again from here, and what the run has staged is
    kept for it to finish. Returns whether it was stale. The caller commits."""
    now = now or datetime.now(UTC)
    sign = max(t for t in (run.session_seen_at, run.started_at) if t is not None)
    if now - sign <= STUCK_AFTER:
        return False
    run.session_id, run.session_seen_at = None, now
    return True


async def work_of(session: AsyncSession, owner: str, conn: AgentConnection) -> Work:
    if SCOUT not in (conn.roles or []):
        return Work()
    run = await open_run(session, owner)
    live = conn.checked_in_at is not None and datetime.now(UTC) - conn.checked_in_at <= LIVE_FOR
    if conn.session_kind in (ANALYST, WRITER, PARSER, CHECKER, GAPS, RESEARCHER, COACH, MAIL) and live:
        from app.jobsearch.work import session_kinds  # it reads this module's words

        return Work(None, conn.session_kind, await session_kinds.doing(session, owner, conn.session_kind))
    return Work(run, SCOUT if run is not None else None)


def _seen(conn: AgentConnection) -> datetime | None:
    stamps = [t for t in (conn.last_seen_at, conn.checked_in_at, conn.declared_at) if t is not None]
    return max(stamps) if stamps else None


def last_heard(conn: AgentConnection) -> datetime | None:
    """The last time the runner was heard from in any way (a call, a session's heartbeat, its idle heartbeat, what it
    declared on start): what the Agents page shows as last seen."""
    return _seen(conn)


async def role_titles(session: AsyncSession) -> dict[str, RoleDef]:
    return {r.key: r for r in (await session.scalars(select(RoleDef))).all()}


def roles_words(keys: list[str], roles: dict[str, RoleDef]) -> str:
    """"Scout", or "Scout and writer": the first title as written, the rest in lower case."""
    names = [roles[k].title if k in roles else k.replace("_", " ").capitalize() for k in keys]
    if not names:
        return "No role"
    names = [names[0], *(n.lower() for n in names[1:])]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _status(conn: AgentConnection, work: Work, now: datetime) -> tuple[str, dict | None]:
    seen = _seen(conn)
    window = timedelta(hours=settings.connected_hours)
    if work.run is None and work.kind in (ANALYST, WRITER, PARSER, CHECKER, GAPS, RESEARCHER, COACH, MAIL):
        return "working", {"task_title": work.title, "project_title": "Job search",
                           "run_id": None, "role": work.kind, "started_at": iso_utc(conn.checked_in_at)}
    if work.run is None:
        if seen is None or now - seen > window:
            return "offline", None
        return "idle", None
    run = work.run
    sign = max([t for t in (run.session_seen_at, run.started_at) if t is not None])
    current = {
        "task_title": "Searching for roles" + (f": {run.focus}" if run.focus else ""),
        "project_title": "Job search", "run_id": str(run.id),
        # Which of the agent's roles the work is for: one runner can hold several and takes them in turn.
        "role": SCOUT,
        "started_at": iso_utc(run.started_at),
    }
    if now - sign > STUCK_AFTER:
        current["stuck_since"] = iso_utc(sign)
        return "idle", current
    return "working", current


def _session_state(conn: AgentConnection, work: Work, now: datetime) -> str | None:
    if work.run is not None and work.run.session_seen_at is not None:
        age = now - work.run.session_seen_at
        if age <= LIVE_FOR:
            return "live"
        if age > STUCK_AFTER:
            return "not_responding"
    return None


def runner_label(name: str) -> str:
    """A runner's name as the page says it: `analysis-1` is "Analysis 1"."""
    words = name.replace("-", " ").replace("_", " ").replace("+", " and ")
    return words[:1].upper() + words[1:]


async def runner_rows(session: AsyncSession, owner: str) -> list[AgentRunner]:
    """The runners the app has heard from lately, by name (the pool's `session-1` to `session-5`)."""
    return list((await session.scalars(select(AgentRunner).where(AgentRunner.owner == owner)
                                       .order_by(AgentRunner.name))).all())


def _alive(row: AgentRunner, now: datetime) -> bool:
    return now - row.seen_at <= LIVE_FOR


async def runner_roles(session: AsyncSession, owner: str, working: list[AgentRunner],
                       now: datetime) -> dict[str, Opportunity]:
    """The role each researching session holds, by runner name, exactly. A session that takes a role is recorded on it
    (the call's `X-Session-Id`, `analysis.claim`), and the session a runner is in is the one its last heartbeat named
    (`AgentRunner.session_id`), so the role is the one that carries that id. A session that has taken nothing yet, or
    whose calls named no session, holds none that can be told, and the page says "Researching a role"."""
    from app.jobsearch.research import analysis  # it reads the roster

    sessions = {r.session_id: r.name for r in working if r.kind == ANALYST and r.session_id}
    held = await analysis.held_by(session, owner, sessions)
    return {sessions[sid]: role for sid, role in held.items() if sid in sessions}


async def _browser_items(session: AsyncSession, owner: str) -> dict[str, str]:
    """What each browser session is on, by session id, in a few words: the domain and role of the item it holds
    ("jobs.example.com: finding the original posting of Analyst at Initech", "scanning Example Jobs"), so the browser
    sessions running at once tell apart (`browser_queue.doing`)."""
    from app.jobsearch.browser import (
        queue as browser_queue,  # imported here, as the other job-search modules are, so none loops
    )

    return await browser_queue.doing(session, owner)


async def runners_view(session: AsyncSession, owner: str, rows: list[AgentRunner], now: datetime) -> list[dict]:
    """Every runner as the page shows it: its name and label, `working` (on what, since when), `idle` (since when) or
    `not_seen` (since when it was last heard: it has said nothing for `LIVE_FOR`, so it is not counted as there)."""
    from app.jobsearch.work import session_kinds  # it reads this module's words

    working = [r for r in rows if _alive(r, now) and r.state == RunnerState.working]
    roles = await runner_roles(session, owner, working, now)
    run = await open_run(session, owner) if any(r.kind == SCOUT for r in working) else None
    writers = sum(1 for r in working if r.kind == WRITER)
    browsing = await _browser_items(session, owner) if any(r.kind in BROWSER_KINDS for r in working) else {}
    out = []
    for r in rows:
        kind, task, since, state = None, None, r.seen_at, "not_seen"
        if _alive(r, now):
            since, state = r.since, "working" if r.state == RunnerState.working else "idle"
            if state == "idle":
                task = "Idle"
            else:
                kind = r.kind
                # Which role a researcher holds is `runner_roles`' to say, never a guess; the writer's session is
                # told by the oldest packet being written, its own only when it is the one writer at work.
                task = await session_kinds.doing(session, owner, kind or "", role=roles.get(r.name), run=run,
                                                 guess=kind == WRITER and writers == 1)
                if kind in BROWSER_KINDS and (slice_words := browsing.get(r.session_id or "")):
                    task = f"{task}: {slice_words}"
        out.append({"name": r.name, "label": runner_label(r.name), "state": state, "kind": kind, "task": task,
                    "since": iso_utc(since), "seen_at": iso_utc(r.seen_at)})
    return out


def _with_runners(status: str, current: dict | None, runners: list[dict]) -> tuple[str, dict | None]:
    """The agent's status and headline once the runners are known. Any runner at work makes it work, and with no search
    run to name, the first of them is the headline. Every runner gone quiet makes it offline: they say they are there
    every couple of minutes. A held search that has gone silent is told as it was."""
    if current is not None and current.get("stuck_since"):
        return status, current
    alive = [r for r in runners if r["state"] != "not_seen"]
    busy = [r for r in alive if r["state"] == "working"]
    if busy:
        if current is None:
            first = busy[0]
            kind = first["kind"]
            current = {"task_title": first["task"], "project_title": "Job search", "run_id": None,
                       "role": kind if kind in (ANALYST, WRITER, PARSER, CHECKER, GAPS, COACH) else SCOUT,
                       "started_at": first["since"]}
        return "working", current
    if not alive and status == "idle":
        return "offline", None
    if alive and status == "offline":
        return "idle", current
    return status, current


def _capacity(conn: AgentConnection, status: str, work: Work, runners: list[dict] | None = None) -> dict:
    if runners:
        # Several runners: the slots are the runners that are there, and busy the ones with a session at work.
        alive = [r for r in runners if r["state"] != "not_seen"]
        busy = [r for r in alive if r["state"] == "working"]
        offline = status == "offline" or not alive
        free = not offline and len(busy) < len(alive)
        if offline:
            words = "Offline"
        elif not busy:
            words = "Free"
        elif free:
            words = f"{len(busy)} of {len(alive)} at work"
        else:
            words = f"All {len(alive)} at work"
        return {"free": free, "busy": len(busy), "slots": len(alive), "words": words,
                "since": max((r["since"] for r in busy), key=datetime.fromisoformat, default=None)}
    slots = (conn.declared or {}).get("concurrency") or None
    # Busy is what the status says: the analyst, writer, parser and checker hold a session, not a search run.
    busy = 1 if status == "working" else 0
    offline = status == "offline"
    free = not offline and busy < (slots or 1)
    words = "Offline" if offline else ("Busy: working on a task" if not free else "Free")
    return {
        "free": free, "busy": busy, "slots": slots, "words": words,
        "since": iso_utc(work.run.started_at) if work.run is not None else None,
    }


def _behind(conn: AgentConnection) -> dict:
    d = conn.declared or {}
    model = d.get("model_name")
    if model and d.get("context_window_tokens"):
        model = f"{model} · {round(d['context_window_tokens'] / 1000)}k-token window"
    hosting = {"local": "runs at home", "hosted": "hosted"}.get(d.get("hosting") or "")
    if model and hosting:
        model = f"{model} · {hosting}"
    limits = [d["usage_limit"]] if d.get("usage_limit") else []
    if d.get("concurrency") == 1:
        limits.append("One task at a time")
    return {
        "machine": d.get("host"), "runtime": d.get("runtime"), "model": model, "engine": d.get("engine"),
        "shares": [], "shares_words": None, "limits": limits,
    }


def _model_words(conn: AgentConnection, runners: list[dict] | None = None) -> str | None:
    """What is true of the model behind it. The pool's sessions each run the model their kind of work calls for, so the
    page names no one model for them; "hosted" says nothing about size, so only a runner at home is told as small."""
    if runners:
        return "A model for each kind of work"
    if (conn.declared or {}).get("hosting") == "local":
        return "small local model"
    return None


async def summary(session: AsyncSession, owner: str, conn: AgentConnection, roles: dict[str, RoleDef]) -> dict:
    now = datetime.now(UTC)
    work = await work_of(session, owner, conn)
    status, current = _status(conn, work, now)
    state = _session_state(conn, work, now)
    seen = _seen(conn)
    # An install with lanes runs several runners at once, each with a session of its own; the page shows each of them.
    # One runner (or none heard from yet) is the agent as it always was.
    runners: list[dict] = []
    if conn.slug == SCOUT:
        rows = await runner_rows(session, owner)
        if len(rows) > 1:
            runners = await runners_view(session, owner, rows, now)
            status, current = _with_runners(status, current, runners)
            seen = max(t for t in (seen, *(r.seen_at for r in rows)) if t is not None)
    availability = "Offline" if status == "offline" else ("On a task" if status == "working" else "Free")
    return {
        "connection_id": str(conn.id), "name": conn.name, "description": conn.description, "status": status,
        "paused": conn.paused, "session_state": state,
        "checked_in_at": iso_utc(conn.checked_in_at) if conn.checked_in_at else None,
        "last_seen_at": iso_utc(seen) if seen else None,
        "declared_at": iso_utc(conn.declared_at) if conn.declared_at else None,
        "current": current, "roles": list(conn.roles or []),
        "roles_words": "Any kind of work" if runners else roles_words(conn.roles or [], roles),
        "availability": availability, "model_words": _model_words(conn, runners),
        "behind": _behind(conn), "capacity": _capacity(conn, status, work, runners),
        # Every runner, when there are several (empty for one): which, what it is at, since when.
        "runners": runners,
    }


async def activity(session: AsyncSession, owner: str, conn: AgentConnection, limit: int = 8) -> list[dict]:
    rows = (await session.scalars(
        select(TrailEvent).where(TrailEvent.owner == owner, TrailEvent.agent_slug == conn.slug)
        .order_by(TrailEvent.at.desc(), TrailEvent.id.desc()).limit(limit)
    )).all()
    return [{"at": iso_utc(r.at), "text": r.sentence, "detail": r.detail} for r in rows]


_CLOSING_NEVER = re.compile(r"(?<!\w)(Never\b[^.!?]*[.!?])\s*$")


def _split_never(about: str) -> tuple[str, str]:
    """A role's words as (what it does, the closing "Never ..." sentence, or "" if it has none)."""
    found = _CLOSING_NEVER.search(about)
    return (about[: found.start()].strip(), found.group(1)) if found else (about.strip(), "")


def may_do(conn: AgentConnection, roles: dict[str, RoleDef]) -> dict:
    """What it may do, from its roles' own words. One kind of work is told as its role tells it. When the agent does
    several, each kind is told once under its role's title (`rules`) and what none of them ever does is told once
    (`summary`), not once for each role."""
    held = [roles[k] for k in (conn.roles or []) if k in roles and roles[k].about]
    if not held:
        return {"summary": "Nothing has been set for it yet.", "rules": []}
    if len(held) == 1:
        return {"summary": held[0].about, "rules": []}
    parts = [(r, *_split_never(r.about)) for r in held]
    nevers = list(dict.fromkeys(never for _, _, never in parts if never))
    return {
        "summary": " ".join(nevers) or f"Does {len(held)} kinds of work, each named here.",
        "rules": [{"kind": r.key, "title": r.title, "rule": does} for r, does, _ in parts if does],
    }


async def detail(session: AsyncSession, owner: str, conn: AgentConnection, roles: dict[str, RoleDef]) -> dict:
    out = await summary(session, owner, conn, roles)
    out["activity"] = await activity(session, owner, conn)
    out["may_do"] = may_do(conn, roles)
    return out


async def listing(session: AsyncSession, owner: str) -> list[dict]:
    """The roster as it stands. A read: nothing is made or mended here. The engine's start makes the roster and brings
    its shipped words up to date (`startup.run_startup_fixups`, the step "the roster"), so on a new install the list is
    empty until the engine has started once (the owner, 2026-11-10: the page program runs no work)."""
    roles = await role_titles(session)
    conns = (await session.scalars(
        select(AgentConnection).where(AgentConnection.owner == owner).order_by(AgentConnection.created_at)
    )).all()
    return [await summary(session, owner, c, roles) for c in conns]


async def get(session: AsyncSession, owner: str, connection_id: uuid.UUID) -> AgentConnection | None:
    return await session.scalar(
        select(AgentConnection).where(AgentConnection.owner == owner, AgentConnection.id == connection_id)
    )
