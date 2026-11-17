"""The session pool queues its own work (the owner, 2026-10-15), and the owner's per-kind control of it.

A session of the pool (`session_kinds.py`) used to be handed only what was already queued: research when the
nightly mission had run, packets when 3 AM came. Now, when one checks in and nothing is queued, the app's own pass
(`session_kinds.pull`) picks the next job on a reader and takes the writer's turn only to start it
(`pull.start_next`), in the pool's order: a packet for a Needs Drafted role
with none, research for a sourced role not yet researched, an employer to look up, a search. Every job is started
by the starter the mission uses (`packets.start`, `analysis.start`, `employer_research.start`,
`scout.start_search`), so it runs exactly as the mission's would. The missions keep their schedules as a safety net.

Every limit stays: peak hours, the global pause and Claude's usage limit are `availability.barred`; the claims are
inside each starter (a job is never queued twice); one session per site is the claims' own rule; a paused kind is
skipped. The per-night counts the owner set still bind: the packets a night (Forge's `count`, over the last 24 hours),
the roles researched a night (Analyse's `target`) and the employers researched a night (Research's `target`), counted
from the start of the owner's off-hours window (`_night_began`), and a search is started only when none has begun
since it. Site learning is never started here (the Scout mission's night
and the owner's switches do): what is queued is handed out first.

A kind is `learner` (site learning), `researcher` (research of roles and employers), `writer` (packets),
`title_checker` (the tagging of roles and the header-line reads), `search`, and the browser's two: `browser_reader`, the
browser worker, which
takes the queue's `source`, `read` and `scan` items, and `pathfinder`, which takes its `map` items
(`browser/items.py`). There is no fixed number of them: a free session takes browser work
while some domain with waiting items is unworked (`browser_queue.free`), each in its own tab of the one browser. Pausing
one stores a row (`AgentKindPause`); pausing all is the global pause (`AgentConnection.paused`), one switch. Services
flush; the route commits."""

import json
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import roster
from app.jobsearch import facts, limits, off_turn
from app.jobsearch.actors import BY_OWNER, CODE_CHECKER
from app.jobsearch.browser import pace as browser_pace
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.fetch import polite
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Employer, Opportunity, Packet, SearchRun
from app.jobsearch.pool import budget
from app.jobsearch.postings import cleansed
from app.jobsearch.postings import index as postings
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, line, rank
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import availability, queue
from app.jobsearch.work import hours as night
from app.jobsearch.work.session_kinds import (
    AGENT_KINDS,
    BROWSER_READER,
    LEARNER,
    PATHFINDER,
    RESEARCHER,
    SEARCH,
    SESSION_KINDS,
    TITLE_CHECKER,
    WRITER,
    of_runner,
)
from app.jobsearch.writing import seats
from app.models.agent import AgentKindPause
from app.sites import tools as site_tools
from app.timeutil import owner_today, owner_zone, utc_now

#: The app's own code-first worker, listed beside the kinds while it works (not one of `KINDS`: no pause, no Run now).
CODE_CHECKER_KIND = "code_checker"
CODE_CHECKER_PURPOSE = ("The app's own code reads postings in the app's browser, with no Claude session. Shown only "
                        "while it works.")
#: The kinds of agent the owner pauses and runs, in the page's order (`session_kinds.AGENT_KINDS`, which also names what
#: each is for and the files its sessions are sent).
KINDS = tuple(a.key for a in AGENT_KINDS)
DAY = timedelta(hours=24)
#: The owner's Run now for a browser kind stands for `ASK_STANDS`. It is a row (the fact `asked:<kind>`, whose text is
#: when it was asked: `facts.py`), so the click the page takes is read by the engine's hand-out, which removes it in
#: the transaction of the claim it answers; a restart of either program does not forget it. A session handed a browser
#: job is handed its item by a claim (`hand_outs.py`, path 47), which holds the item and its domain until the session
#: leases it: sessions asking at once are never handed more jobs than there are free domains.
ASK_STANDS = timedelta(hours=1)
ASKED = "asked:"
#: When the owner last started work of each kind (Run now, Wake up, Go): outside their off hours the pool hands out work
#: of a kind only for `ASK_STANDS` after (`owner_running`). Unlike the ask above, a hand-out does not use it up. A row
#: too (the fact `owner_asked:<kind>`, whose text is when), written in the transaction of the owner's click.
OWNER_ASKED = "owner_asked:"
#: Why the browser jobs cannot run now (the user's everyday Chrome is closed, remote debugging is off, or Chrome's
#: prompt was not allowed), as the runners last said it (`note_browser`), in the one line Wake up and Run show. It is a
#: row (the fact of this key, `facts.py`): the engine is told and the page says it. It stays until a runner says the
#: browser is back, through a restart of either program too.
BROWSER_DOWN = "browser_down"


class NoSuchKind(Exception):
    pass


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise NoSuchKind(f"no such agent kind; the kinds are {', '.join(KINDS)}")
    return kind


# --- pausing --------------------------------------------------------------------------------------------------------


async def paused_kinds(session: AsyncSession, owner: str) -> set[str]:
    return set((await session.scalars(select(AgentKindPause.kind).where(AgentKindPause.owner == owner))).all())


async def pause(session: AsyncSession, owner: str, kind: str) -> None:
    if check_kind(kind) not in await paused_kinds(session, owner):
        session.add(AgentKindPause(owner=owner, kind=kind))
        await session.flush()
    # A paused browser kind's sessions finish the item each holds; the door then tells them to stop.


async def resume(session: AsyncSession, owner: str, kind: str) -> None:
    await session.execute(delete(AgentKindPause).where(AgentKindPause.owner == owner,
                                                       AgentKindPause.kind == check_kind(kind)))
    # Resuming a kind makes the work queued for it ready for a session again.
    queue.post_all(session, owner, [(queue.POOL_OWED, [""]),
                                    *((queue.ready(k.name), [""]) for k in SESSION_KINDS if k.group == kind)],
                   cause="core:pool.resume")
    await session.flush()


async def set_global_pause(session: AsyncSession, owner: str, paused: bool) -> None:
    """Pause or resume every kind at once: the scout connection's pause, the one the Agents page already has."""
    conn = await roster.connection_by_slug(session, owner, roster.SCOUT) or await roster.ensure_roster(session, owner)
    conn.paused = paused
    await session.flush()
    # Browser workers finish the item each holds; the door then tells them to stop.


# --- the limits on starting a session ---------------------------------------------------------------------------------


MAX_SESSIONS = 5  # the sessions the launcher starts (`scout/lanes-local.mjs`)


def over_limit(runner: str | None, limit: int) -> bool:
    """Whether a session named `session-<n>` is past the owner's limit on active sessions (the first `limit` work)."""
    name = runner or ""
    prefix = roster.POOL_PREFIX + "-"
    if not name.startswith(prefix) or not name[len(prefix):].isdigit():
        return False
    return int(name[len(prefix):]) > limit


async def active_sessions(session: AsyncSession, owner: str) -> int:
    return (await profile_store.current(session, owner)).settings.active_sessions


#: `set_active_sessions`: leave the nightly budget as it is. None there is no limit.
UNCHANGED: Any = object()


async def set_active_sessions(session: AsyncSession, owner: str, count: int | None = None,
                              nightly_budget: float | None | Any = UNCHANGED) -> int:
    """The owner's limit on sessions and their nightly budget (`budget.py`), saved as the next version of the Settings
    document (nothing when they already are that)."""
    current = (await profile_store.current(session, owner)).settings
    count = current.active_sessions if count is None else count
    nightly = current.nightly_budget if nightly_budget is UNCHANGED else nightly_budget
    if current.active_sessions != count or current.nightly_budget != nightly:
        saved = {**current.model_dump(mode="json"), "active_sessions": count, "nightly_budget": nightly}
        budget_words = "no limit" if nightly is None else f"${nightly:,.0f}"
        why = f"set how many sessions may work at once: {count}; nightly budget: {budget_words}"
        await profile_store.save(session, owner, "settings", saved, by=BY_OWNER, why=why)
    return count


# --- what is waiting, and the night's counts ------------------------------------------------------------------------


async def _night_began(session: AsyncSession, owner: str) -> datetime:
    """Where the night's quotas count from: the start of the owner's off-hours window they are in, or the last that
    began (so a day's work counts to the night it belongs to), not a rolling 24 hours, which would count last night's
    late work against tonight's start. A window of every hour has no start: the last 24 hours."""
    now = utc_now()
    return night.off_hours_began((await profile_store.current(session, owner)).settings, now) or now - DAY


async def _research_room(session: AsyncSession, owner: str) -> int:
    used = int(await session.scalar(select(func.count(Opportunity.id)).where(
        Opportunity.owner == owner, Opportunity.analysis_asked_at >= await _night_began(session, owner),
        line.not_owners())) or 0)
    return limits.left(int((await missions.standing(session, owner, missions.ANALYSE)).settings["target"]), used)


async def _employers_room(session: AsyncSession, owner: str) -> int:
    used = int(await session.scalar(select(func.count(Employer.id)).where(
        Employer.owner == owner, Employer.research_asked_at >= await _night_began(session, owner))) or 0)
    return limits.left(int((await missions.standing(session, owner, missions.RESEARCH)).settings["target"]), used)


async def _to_research(session: AsyncSession, owner: str) -> int:
    """The roles the pool could put in front of the analyst (`line.startable`, what `analysis.picks` selects)."""
    return int(await session.scalar(select(func.count(Opportunity.id)).where(
        Opportunity.owner == owner, line.startable())) or 0)


async def _employers_to_research(session: AsyncSession, owner: str) -> list[Employer]:
    days = int((await missions.standing(session, owner, missions.RESEARCH)).settings["again_after_days"])
    return await employer_research.picks(session, owner, None, days)


async def _search_in_the_day(session: AsyncSession, owner: str) -> bool:
    return await session.scalar(select(SearchRun.id).where(
        SearchRun.owner == owner, SearchRun.started_at >= await _night_began(session, owner)).limit(1)) is not None


#: How long an idle pool leaves between searches for new roles (a round for titles or reads waiting is never held)
#: when the Scout mission has no "every N hours" schedule.
IDLE_ROUND_GAP = timedelta(hours=4)


async def _idle_gap(session: AsyncSession, owner: str) -> timedelta:
    """The least time between the end of one round and an idle pool starting the next: the Scout mission's "every N
    hours" when it has one, else `IDLE_ROUND_GAP`."""
    schedule = (await missions.standing(session, owner, missions.SCOUT)).schedule
    hours = schedule.get("hours") if schedule.get("kind") == "every" else None
    return timedelta(hours=float(hours)) if hours else IDLE_ROUND_GAP


async def _round_can_start(session: AsyncSession, owner: str, *, idle: bool = True) -> bool:
    """None open and, for an `idle` search, the last one ended at least the gap ago (waiting work and the owner
    do not ask)."""
    if await run.open_run(session, owner) is not None:
        return False
    if not idle:
        return True
    last = await session.scalar(select(func.max(SearchRun.ended_at)).where(SearchRun.owner == owner))
    return last is None or utc_now() - last >= await _idle_gap(session, owner)


# --- starting ---------------------------------------------------------------------------------------------------------


async def _start_round(session: AsyncSession, owner: str) -> bool:
    """Start a search round the way the Scout mission does (`scout.start_search`: its focus, Claude's usage limit, the
    paused scout, one at a time). A refusal is no round."""
    from app.jobsearch.missions.kinds import scout  # the catch-up it reads reads this module

    try:
        await scout.start_search(session, owner, by="pool")
    except run.RunError:
        return False
    return True


async def queue_research(session: AsyncSession, owner: str, n: int | None, *, one_ahead: bool = False) -> int:
    """Research for sourced roles not yet researched (`analysis.picks`), up to `n` and the day's roles left.
    `one_ahead` (the pool's own pull): none while a role is queued and no session has taken it, so the queue never
    piles up behind sessions that wait their turn on a site."""
    roles = await _research_picks(session, owner, n, one_ahead)
    for role in roles:
        await analysis.start(session, owner, role)
    return len(roles)


async def _research_picks(session: AsyncSession, owner: str, n: int | None, one_ahead: bool) -> list[Opportunity]:
    """The roles `queue_research` starts: reads only."""
    if one_ahead and await _role_queued_untaken(session, owner):
        return []
    room = await _research_room(session, owner)
    cap = room if n is None else min(room, n)
    if cap <= 0:
        return []
    return await analysis.picks(session, owner, cap)


async def queue_employers(session: AsyncSession, owner: str, n: int | None, *, one_ahead: bool = False) -> int:
    rows = await _employer_picks(session, owner, n, one_ahead)
    for employer in rows:
        await employer_research.start(session, owner, employer)
    return len(rows)


async def _employer_picks(session: AsyncSession, owner: str, n: int | None, one_ahead: bool) -> list[Employer]:
    """The employers `queue_employers` starts: reads only."""
    if one_ahead and await _employer_queued_untaken(session, owner):
        return []
    room = await _employers_room(session, owner)
    cap = room if n is None else min(room, n)
    return (await _employers_to_research(session, owner))[:max(cap, 0)]


async def _queued_untaken(session: AsyncSession, owner: str, model, *where) -> bool:
    return await session.scalar(select(model.id).where(model.owner == owner, *where).limit(1)) is not None


async def _role_queued_untaken(session: AsyncSession, owner: str) -> bool:
    """Whether a role is in the analyst's line and no session has taken it."""
    return await _queued_untaken(session, owner, Opportunity, line.Standing.where(line.Standing.FREE))


async def _employer_queued_untaken(session: AsyncSession, owner: str) -> bool:
    """Whether an employer is with the researcher and no session has taken it."""
    return await _queued_untaken(session, owner, Employer, Employer.research_state == employer_research.WAITING,
                                 Employer.research_claimed_at.is_(None))


#: How many postings one pull reads the description of, and the seconds it may take (code only, at the polite pace).
DETAILS_PER_PULL, DETAILS_SECONDS = 60, 90
#: The same for the read the pipeline's own turn makes (`schedule.pipeline_pass`): short, so a turn stays short, and
#: every 30 seconds, so a morning's few hundred roles that have no text have it within minutes.
DETAILS_PER_TURN, DETAILS_TURN_SECONDS = 40, 40


async def read_details(owner: str, limit: int | None = None, seconds: float | None = None) -> postings.DetailsRead:
    """Read the descriptions the roles in play lack, by code, the newest first, at any hour: a role with no real posting
    text is not ranked (`rank.unranked`) and waits in Cleansed Data until it has some. Returns how many were filled and
    how many left the owed set (`postings.DetailsRead`). In its own transactions, none of them open while a page is read
    (`postings.read_role_details`)."""
    token = polite.READ_BY.set(time.monotonic() + (seconds or DETAILS_SECONDS))
    fetch = polite.make()
    try:
        return await postings.read_role_details(owner, fetch, limit or DETAILS_PER_PULL)
    finally:
        polite.READ_BY.reset(token)
        await fetch.close()


async def queue_details(owner: str, limit: int | None = None, seconds: float | None = None) -> int:
    """`read_details`, as the number of descriptions it filled."""
    return (await read_details(owner, limit, seconds)).filled


async def queue_search(session: AsyncSession, owner: str, *, idle: bool = True) -> int:
    return int(await _search_due(session, owner, idle=idle) and await _start_round(session, owner))


async def _search_due(session: AsyncSession, owner: str, *, idle: bool = True) -> bool:
    """Whether `queue_search` would try to start a search now: reads only."""
    return await _round_can_start(session, owner, idle=idle) and not await _search_in_the_day(session, owner)


async def queue_sites(session: AsyncSession, owner: str) -> int:
    """Site learning, as the Scout mission starts it (within the owner's sites to learn a night). Only the owner's
    run starts it: the pool never does."""
    from app.jobsearch.missions.kinds import scout  # the catch-up it reads reads this module

    return len(await scout.start_sites(session, owner))


async def queue_kind(session: AsyncSession, owner: str, kind: str, n: int | None = None, *,
                     manual: bool = False) -> int:
    """Queue this kind's waiting work, up to `n` jobs (None: all), within the limits. Returns how many were queued."""
    if kind == WRITER:
        # Packets are built when the owner presses Go or Build now, never ahead of time. What is already asked for waits
        # for a session: a seat of a packet's or the story library's process that no session holds. It is counted, so
        # Run now wakes a lane for it.
        return int(bool(await seats.free(session, owner)))
    if kind == RESEARCHER:
        started = await queue_research(session, owner, n)
        left = None if n is None else n - started
        return started + (await queue_employers(session, owner, left) if left is None or left > 0 else 0)
    if kind == TITLE_CHECKER:
        return 0  # its work (the tagging, the header-line reads) is handed out as it waits, never queued here
    if kind == SEARCH:
        return await queue_search(session, owner, idle=not manual)
    if kind in (BROWSER_READER, PATHFINDER):
        return await _run_browser(session, owner, kind) if manual else 0
    return await queue_sites(session, owner) if manual else 0


async def run_kind(session: AsyncSession, owner: str, kind: str) -> dict:
    """The owner's Run now for one kind: its waiting work is queued, unless a limit holds everything
    (`availability.barred`) or the kind is paused."""
    check_kind(kind)
    _counts.pop(owner, None)
    if (stop := await availability.barred(session, owner, availability.Question.POOL)) is not None:
        return {"kind": kind, "queued": 0, "held": stop.words}
    paused = await paused_kinds(session, owner)
    if kind in paused:
        return {"kind": kind, "queued": 0, "held": "this kind is paused"}
    if kind in (BROWSER_READER, PATHFINDER) and (down := await browser_down(session, owner)) is not None:
        await ask_browser(session, owner, kind)  # tried again at once: the owner may have just opened Chrome
        return {"kind": kind, "queued": 0, "held": down}
    # The owner's own: it runs outside the off hours too (`owner_running`).
    await facts.put(session, owner, OWNER_ASKED + kind, utc_now().isoformat())
    queued = await queue_kind(session, owner, kind, manual=True)
    # What holds the stage back may be the browser's to do: then the browser read is asked for too, and said.
    started = await browser_unblocks(session, owner, kind, paused)
    out = {"kind": kind, "queued": queued, "held": None}
    if queued == 0 and (why := await blocked(session, owner, kind)) is not None:
        down = await browser_down(session, owner) if started else None
        out["blocked"] = f"{why}: {down}" if down else f"{why}: browser read started" if started else why
    return out


async def blocked(session: AsyncSession, owner: str, kind: str) -> str | None:
    """Why a kind that queued nothing still has work: what its stage is stuck on, in a few plain words, or None when
    the stage is truly empty. The same rules as the board's columns: Needs Analysis holds Sourced roles that wait for
    the posting's text (`board.FOUND_STAGES`), Fact checking holds the roles with no green check yet."""
    if kind == RESEARCHER:
        n = int(await session.scalar(select(func.count(Opportunity.id)).where(
            Opportunity.owner == owner, Opportunity.sourced_at.is_not(None),
            Opportunity.analysis_state == analysis.NO_TEXT)) or 0)
        return f"{n} {'is' if n == 1 else 'are'} waiting for the posting's text" if n else None
    if kind in (SEARCH, TITLE_CHECKER):
        n = int(await session.scalar(select(func.count(Opportunity.id)).where(
            Opportunity.owner == owner, Opportunity.status.in_(sourcing.IN_PLAY),
            Opportunity.sourced_at.is_(None))) or 0)
        return f"{n} {'is' if n == 1 else 'are'} waiting for the browser to find the employer's posting" if n else None
    return None


async def run_all(session: AsyncSession, owner: str) -> dict:
    out = [await run_kind(session, owner, kind) for kind in KINDS]
    return {"queued": {r["kind"]: r["queued"] for r in out}, "held": next((r["held"] for r in out if r["held"]), None)}


async def _rank_plan(session: AsyncSession, owner: str) -> rank.Batch | None:
    """The batch the pool's pull would queue for the ranker, decided without a write (`rank.plan`), or None."""
    if not await cleansed.any_current(session, owner):
        return None
    try:
        return await rank.plan(session, owner)
    except rank.RankError:
        return None


# --- the browser ------------------------------------------------------------------------------------------------------


async def note_browser(session: AsyncSession, owner: str, why: str) -> None:
    """A runner said whether the browser jobs can run (its idle word, or a browser session's end): `why` is the one
    line that says what stops them, "" when nothing does. Kept as the fact `BROWSER_DOWN` in the caller's transaction,
    and written only when it differs from what is kept: a runner says it every few minutes."""
    why = " ".join(str(why or "").split())[:200]
    kept = await facts.get(session, owner, BROWSER_DOWN)
    if why and why != kept:
        await facts.put(session, owner, BROWSER_DOWN, why)
    elif not why and kept is not None:
        await facts.drop(session, owner, BROWSER_DOWN)


async def browser_down(session: AsyncSession, owner: str) -> str | None:
    """Why the browser jobs cannot run now, or None when nothing a runner said stops them. It only reads."""
    return await facts.get(session, owner, BROWSER_DOWN)


async def ask_browser(session: AsyncSession, owner: str, kind: str, now: datetime | None = None) -> None:
    """The owner asked for a browser job (Run now, a Wake up): the next free session takes it when there is anything to
    do, without waiting for the read interval or the Pathfinder's pause between tries. The ask is the fact
    `asked:<kind>`, written in the caller's transaction: it is there for every program once the caller commits. The
    owner's hour for the kind (`owner_asked:<kind>`) starts with it."""
    at = now or utc_now()
    await facts.put(session, owner, ASKED + kind, at.isoformat())
    await facts.put(session, owner, OWNER_ASKED + kind, at.isoformat())


async def owner_started_mission(session: AsyncSession, owner: str, key: str, now: datetime | None = None) -> None:
    """The owner pressed Run now on a mission: its work is theirs, handed out at any hour (`owner_running`). The kind of
    work a mission starts is its kind's `group`. Written in the caller's transaction: it is there when the run is, and
    it is rolled back with it."""
    mission_kind = missions.kind_of(key)
    if mission_kind is not None and mission_kind.group is not None:
        await facts.put(session, owner, OWNER_ASKED + mission_kind.group, (now or utc_now()).isoformat())


async def owner_started_at(session: AsyncSession, owner: str, kind: str) -> datetime | None:
    """When the owner last started work of this kind (`owner_running` says whether that is lately). It only reads."""
    return _when(await facts.get(session, owner, OWNER_ASKED + kind))


async def owner_running(session: AsyncSession, owner: str, kind: str, now: datetime | None = None) -> bool:
    """Whether the owner started work of this kind lately (Run now, Wake up, Go: `ASK_STANDS`), so the pool hands it
    out outside the owner's off hours too. It only reads."""
    at = await owner_started_at(session, owner, kind)
    return at is not None and (now or utc_now()) - at < ASK_STANDS


async def owners_running(session: AsyncSession, owner: str, now: datetime | None = None) -> set[str]:
    """Every kind `owner_running` is true of now, in one read: what the hand-out's walk asks once for all its rungs."""
    now = now or utc_now()
    asked = await facts.get_many(session, owner, OWNER_ASKED)
    return {key[len(OWNER_ASKED):] for key, text in asked.items()
            if (at := _when(text)) is not None and now - at < ASK_STANDS}


def _when(text: str | None) -> datetime | None:
    """The moment a fact's text holds (an ask is kept as when it was made, `datetime.isoformat`), or None when there is
    no fact or its text is no moment."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


async def _asked_lately(session: AsyncSession, owner: str, kind: str, now: datetime) -> bool:
    at = _when(await facts.get(session, owner, ASKED + kind))
    return at is not None and now - at < ASK_STANDS


async def browser_asked(session: AsyncSession, owner: str, now: datetime | None = None) -> bool:
    """Whether the owner has asked for a browser job that no session has taken yet. It only reads."""
    now = now or utc_now()
    for kind in (BROWSER_READER, PATHFINDER):
        if await _asked_lately(session, owner, kind, now):
            return True
    return False


async def browser_held(session: AsyncSession, owner: str, kind: str, now: datetime | None = None) -> bool:
    """Whether no session is handed a browser job of this kind (`browser_reader` or `pathfinder`) now: the owner's own
    Chrome cannot be had (`note_browser`), so none is started only to fail at its start, unless the owner asked for the
    job (Run now, Wake up), which tries again. The walk's gate for the browser kinds (`session_kinds._scope`); what is
    waiting for a session is `browser_queue.free`, as for every other kind."""
    if await browser_down(session, owner) is None:
        return False
    return not await _asked_lately(session, owner, kind, now or utc_now())


async def use_up_browser_ask(session: AsyncSession, owner: str, kind: str) -> None:
    """The job the owner asked for (`ask_browser`) was claimed for a session: the ask is answered, and its fact goes in
    the transaction of the claim (`session_kinds._hand_out`). Nothing is written when there was no ask."""
    await facts.drop(session, owner, ASKED + kind)


async def _browser_waiting(session: AsyncSession, owner: str) -> dict[str, int]:
    return {kind: await browser_queue.waiting_count(session, owner, browser_pace.WORK[kind])
            for kind in (BROWSER_READER, PATHFINDER)}


async def _run_browser(session: AsyncSession, owner: str, kind: str) -> int:
    """The owner's Run now for a browser kind: asked, when there is anything for it to do."""
    await browser_queue.refresh(session, owner)
    if not await browser_queue.waiting_count(session, owner, browser_pace.WORK[kind]):
        return 0
    await ask_browser(session, owner, kind)
    return 1


#: The browser items that hold back each kind's stage (the owner, 2026-10-15: Wake up starts the agent that can
#: unblock the stage): Needs Analysis roles waiting for their posting's text, and Fact checking roles waiting for their
#: original posting.
UNBLOCKS = {RESEARCHER: (browser_pace.READ,), SEARCH: (browser_pace.SOURCE,), TITLE_CHECKER: (browser_pace.SOURCE,)}


async def browser_unblocks(session: AsyncSession, owner: str, kind: str, paused: set[str]) -> int:
    """Ask for a browser read when roles of this kind's stage wait for it (`UNBLOCKS`). Returns 1 when asked."""
    names = UNBLOCKS.get(kind)
    if names is None or BROWSER_READER in paused:
        return 0
    if not await browser_queue.waiting_count(session, owner, names):
        return 0
    await ask_browser(session, owner, BROWSER_READER)
    return 1


# --- the page ---------------------------------------------------------------------------------------------------------


async def waiting_counts(session: AsyncSession, owner: str) -> dict[str, int]:
    """What each kind has waiting: queued and not taken, and what the pool would queue. The writer has none the pool
    queues: a packet is built when the owner presses Go or Build now."""
    roles_queued = int(await session.scalar(select(func.count(Opportunity.id)).where(
        Opportunity.owner == owner, line.Standing.where(line.Standing.FREE))) or 0)
    employers_queued = int(await session.scalar(select(func.count(Employer.id)).where(
        Employer.owner == owner, Employer.research_state == employer_research.WAITING,
        Employer.research_claimed_at.is_(None))) or 0)
    return {
        LEARNER: len(await site_tools.waiting_projects(session, owner)),
        # What can be started now: the queued, and of the rest no more than each night's room leaves (`queue_research`,
        # `queue_employers` take the same number), so the count is what Run now would queue.
        RESEARCHER: roles_queued + employers_queued
        + min(await _to_research(session, owner), await _research_room(session, owner))
        + min(len(await _employers_to_research(session, owner)), await _employers_room(session, owner)),
        WRITER: 0,
        TITLE_CHECKER: 0,
        SEARCH: int(await _round_can_start(session, owner, idle=False)
                    and not await _search_in_the_day(session, owner)),
        **await _browser_waiting(session, owner),
    }


#: How long the page's waiting counts are kept (working them out is slow, and the Pipeline asks
#: every minute). Run now and a pause or resume start them afresh. The tests set it to 0.
COUNTS_FOR = timedelta(seconds=30)
_counts: dict[str, tuple[float, dict[str, int]]] = {}  # kept at `time.monotonic()`: a clock change cannot keep them


async def _page_counts(session: AsyncSession, owner: str) -> dict[str, int]:
    now = time.monotonic()
    kept = _counts.get(owner)
    if kept is not None and now - kept[0] < COUNTS_FOR.total_seconds():
        return kept[1]
    counts = await waiting_counts(session, owner)
    _counts[owner] = (now, counts)
    return counts


#: What each kind left undone when the owner's off hours last ended, and why, one line each (the owner, 2026-10-17: the
#: health line says at 7 AM what was left undone and why), and the night's spend line, taken with it
#: (`budget.line`): two facts the window's end writes (`note_window`, `facts.py`), so the page reads them from the
#: database. Each is a JSON text: `{"day": the owner's day, "left": {kind: line}}` and `{"day": the owner's day,
#: "line": the spend line}`.
NIGHT_LEFT = "night_left"
NIGHT_SPEND = "night_spend"
#: Whether the schedule's last look found the owner inside the off hours: the engine's own memory, never shown.
_in_window: dict[str, bool] = {}


async def note_window(session: AsyncSession, owner: str, now: datetime | None = None) -> bool:
    """The schedule's look at the owner's off hours (every pass): when the window has just ended, what each kind left
    undone is taken (`night_left`) with the night's spend line, and both are written as facts (`NIGHT_LEFT`,
    `NIGHT_SPEND`) for the page to read. Returns whether they were taken now: the caller commits when they were.

    The look and the taking read on a reader (`off_turn.reading`), so a look that finds nothing to take asks for no
    writer's turn; `session` takes the turn only for the two facts."""
    now = now or utc_now()
    async with off_turn.reading(session) as reader:
        inside = night.in_off_hours((await profile_store.current(reader, owner)).settings, now)
        was = _in_window.get(owner)
        _in_window[owner] = inside
        if inside or not was:
            return False
        left = await night_left(reader, owner, now)
        spend = budget.line(await budget.state(reader, owner, now), left)
    day = owner_today(now)
    await facts.put(session, owner, NIGHT_LEFT, json.dumps({"day": day.isoformat(), "left": left}), now=now)
    await facts.put(session, owner, NIGHT_SPEND, json.dumps({"day": day.isoformat(), "line": spend}), now=now)
    return True


async def _kept(session: AsyncSession, owner: str, key: str) -> dict[str, Any]:
    """What the window's end wrote under `key` (`note_window`), when it wrote it on the owner's day today; else empty.
    A read only: a reader's session will do."""
    text = await facts.get(session, owner, key)
    kept: dict[str, Any] = json.loads(text) if text else {}
    return kept if kept.get("day") == owner_today().isoformat() else {}


async def spend_line(session: AsyncSession, owner: str) -> str | None:
    """The line the 7 AM look took about the night's spend and what was left undone, for the owner's day; else None."""
    return (await _kept(session, owner, NIGHT_SPEND)).get("line")


async def night_left(session: AsyncSession, owner: str, now: datetime | None = None) -> dict[str, str]:
    """What each kind has still waiting, and why it was not done: a site resting after a block (the browser), the
    night's packets used or Ready to apply full (the writer); "none left" when it is all done."""
    now = now or utc_now()
    counts = await waiting_counts(session, owner)
    at = now.astimezone(owner_zone()).strftime("%I:%M %p").lstrip("0")
    out = {}
    for kind in KINDS:
        n = counts.get(kind, 0)
        why = ""
        if n and kind in (BROWSER_READER, PATHFINDER):
            resting, site = await browser_queue.paced(session, owner, kind, now)
            why = f", {resting} waiting on {site}'s pace" if resting and site else ""
        out[kind] = f"At {at}: {n} left{why}." if n else f"At {at}: none left."
    return out


async def listing(session: AsyncSession, owner: str) -> list[dict]:
    paused = await paused_kinds(session, owner)
    counts = await _page_counts(session, owner)
    doing: dict[str, list[dict]] = {k: [] for k in KINDS}
    rows = await roster.runner_rows(session, owner)
    for row in await roster.runners_view(session, owner, rows, utc_now()):
        found = of_runner(row["kind"] or "")
        if row["state"] == "working" and found is not None and found.group in doing:
            doing[found.group].append({"session": row["label"], "task": row["task"]})
    # The browser kinds' health line: items done in the last hour by kind, the oldest waiting, a stall.
    health = {k: await browser_queue.health(session, owner, k) for k in (BROWSER_READER, PATHFINDER)}
    # By day, what the night left undone and why leads each kind's health line (`night_left`).
    left: dict[str, str] = (await _kept(session, owner, NIGHT_LEFT)).get("left", {})
    # Claude's usage limit holds every kind: no session starts before it lifts, and the line says until when.
    lifts = await availability.limit_until(session, owner)
    limit = f"Held: {availability.USAGE_LIMIT}, until {night.when_words(lifts)}." if lifts is not None else None
    health = {k: " ".join(w for w in (limit, left.get(k), health.get(k)) if w) or None for k in KINDS}
    # Packets being written now, apart from the roles waiting for one (`waiting`), so the page can tell the two apart.
    writing = int(await session.scalar(select(func.count(Packet.id)).where(
        Packet.owner == owner, Packet.status == "writing")) or 0)
    out = [{"kind": a.key, "label": a.label, "purpose": a.purpose, "doing": doing[a.key], "paused": a.key in paused,
            "waiting": counts[a.key], "health": health.get(a.key),
            **({"writing": writing} if a.key == WRITER else {}),
            "instructions": [{"kind_of_file": f, "name": n} for f, n in a.files]} for a in AGENT_KINDS]
    # The app's own code checker (no session, no pause, no queue of its own) is listed only while it holds an item.
    if code := await browser_queue.code_doing(session, owner):
        out.append({"kind": CODE_CHECKER_KIND, "label": CODE_CHECKER, "purpose": CODE_CHECKER_PURPOSE, "doing": code,
                    "paused": False, "waiting": 0, "health": None, "instructions": []})
    return out
