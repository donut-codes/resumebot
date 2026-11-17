"""What a session can be started for (docs/PRD.md 23.3), as one table, and the pool's one hand-out of it.

A session of the scout's runner has its own instructions and only its own tools, so before one starts the runner asks
which kind of work is waiting. This module is the one place that knows the kinds a session can be started for. Its rows
are pure data (`SESSION_KINDS`: the name an ask is answered with, the runner's word for the session, the agent kind it
is paused and listed with, the wake that knocks for it, the words of "The agent is busy ..."), and so are the agent
kinds the Agents page lists (`AGENT_KINDS`). `kind` and `of_runner` look a row up by its name or by the runner's word:
two lookups because the two vocabularies must stay visible at the call site.

The behaviour is a few calls. `hand_out` gives a runner its next job (the kind, and the exact items claimed for it in
the same transaction, `hand_outs.py`); `next_kind` says which kind would be next, with no lock and no row; `saw` is told
what a session's own take-next call gave (a session handed nothing holds its kind out of the order until what waits for
it changes); `heard` is what a session's heartbeat does for its kind; `pull` starts one job for a free pool session when
nothing is queued; `doing` says what a session is at, in the Agents page's words; `forget` drops the in-memory state
(the tests, between cases). A decision is a claim: the ask walks `ORDER` (one rung for each way a kind is handed out),
and the claim is what that same walk saw (`free`, below), under a try-lock per kind that never waits; the session's
own take-next call decides under the same lock (`hand_outs.take_next`).

Each kind answers one question, `free`: the ids a session could take now, in the order they would be handed, leaving out
whatever is claimed for another session. It only reads and never checks pause, hold, hour, drain or the owner's ask:
those are the walk's gates. The adapters are built once, on the first call (`_wire`), because they reach every kind's
module. This module imports only `db`, `hand_outs` (the kinds' names), `engine_calls` (reason strings) and `roster` (the
runner's words) at the top, so the roster, the pool, the job queue, the budget and the missions can ask it for facts
without a cycle."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.agents import roster
from app.jobsearch import off_turn
from app.jobsearch.work import engine_calls, hand_outs
from app.jobsearch.work.hand_outs import (
    ANALYSIS,
    BROWSER_READER,
    CHECKER,
    COACH,
    GAPS,
    MAIL,
    PARSER,
    PATHFINDER,
    RESEARCHER,
    SEARCH,
    SEAT,
    SITE,
    TITLES,
)
from app.timeutil import utc_now

log = logging.getLogger(__name__)

#: The agent kinds' keys (what the owner pauses and runs, `pool.KINDS`). The researcher and the writer are the keys of
#: the sessions too.
LEARNER, WRITER, TITLE_CHECKER = "learner", "writer", "title_checker"


@dataclass(frozen=True)
class SessionKind:
    """One kind of session a runner can be started for. Pure data: nothing here asks the database."""

    #: What an ask for work is answered with (`reply["next"]`, `HandOut.kind`, the `X-Kind` of the wake).
    name: str
    #: The runner's word for the session (`AgentRunner.kind`, the start of the session's id, the heartbeat's `kind`).
    runner: str
    #: The agent kind it is paused, run and listed with; None only for the owner's asks (a posting to split, a check of
    #: their roles, Find Gaps): the owner is waiting at the screen, so nothing pauses them.
    group: str | None
    #: What the schedule's backstop knocks with when work of this kind waits and nothing else knocked.
    wake: str
    #: The words of "The agent is busy {busy}" (`job_queue.queued`).
    busy: str
    #: How long a session of the kind takes, for the one kind whose length the app can tell.
    takes: str | None = None


SESSION_KINDS: tuple[SessionKind, ...] = (
    SessionKind(PARSER, roster.PARSER, None, engine_calls.PARSE_ASKED, "reading another posting"),
    SessionKind(CHECKER, roster.CHECKER, None, engine_calls.CHECK_ASKED, "checking your roles"),
    SessionKind(GAPS, roster.GAPS, None, engine_calls.GAPS_ASKED, "finding gaps in your resume"),
    SessionKind(SEARCH, roster.SCOUT, SEARCH, engine_calls.SEARCH_STARTED, "with a search"),
    SessionKind(TITLES, roster.TITLES, TITLE_CHECKER, engine_calls.TAG_ROLES, "judging titles"),
    SessionKind(ANALYSIS, roster.ANALYST, RESEARCHER, engine_calls.ANALYSIS_ASKED, "researching a role",
                "which takes about 3 minutes"),
    SessionKind(RESEARCHER, roster.RESEARCHER, RESEARCHER, engine_calls.EMPLOYERS_ASKED, "researching an employer"),
    SessionKind(SITE, roster.SITE, LEARNER, engine_calls.SITE_PROJECT, "mapping a site"),
    SessionKind(SEAT, roster.WRITER, WRITER, engine_calls.PACKET_ASKED, "writing a packet"),
    SessionKind(COACH, roster.COACH, RESEARCHER, engine_calls.COACH_ASKED, "preparing you for an interview"),
    SessionKind(MAIL, roster.MAIL, RESEARCHER, engine_calls.MAIL_ASKED, "reading the mail"),
    SessionKind(BROWSER_READER, roster.BROWSER_READER, BROWSER_READER, engine_calls.RUN_NOW, "working in the browser"),
    SessionKind(PATHFINDER, roster.PATHFINDER, PATHFINDER, engine_calls.RUN_NOW, "mapping a site in the browser"),
)
_BY_NAME = {k.name: k for k in SESSION_KINDS}
_BY_RUNNER = {k.runner: k for k in SESSION_KINDS}


def kind(name: str) -> SessionKind | None:
    """The kind an ask is answered with ("seat"), or None for a name that is no kind."""
    return _BY_NAME.get(name)


def of_runner(word: str) -> SessionKind | None:
    """The kind of the session a runner says it is in ("writer": its word, the start of the session's id), or None."""
    return _BY_RUNNER.get(word)


@dataclass(frozen=True)
class AgentKind:
    """One kind of agent as the Agents page lists it: what the owner pauses and runs, and the files its sessions are
    sent (`kind_of_file`, `name`). Pure data."""

    key: str
    label: str
    purpose: str
    files: tuple[tuple[str, str], ...]


#: The names of the files a browser session is sent: the worker's instructions and the shape of a posting, and the
#: Pathfinder's, the shape of its skill and the shape of its runbook. `browser_reader` reads them from here (it imports
#: the world, so it cannot be where they are written).
READER_INSTRUCTIONS, READER_TEMPLATE = "browser.reader", "browser.reader.postings"
LEARNER_INSTRUCTIONS, SKILL_TEMPLATE, RUNBOOK_TEMPLATE = (
    "browser.learner", "browser.learner.skill", "browser.pathfinder.runbook")

#: The agent kinds, in the page's order.
AGENT_KINDS: tuple[AgentKind, ...] = (
    AgentKind(
        LEARNER, "Site setup",
        "Works out how the app itself reads each site that needs no browser (its plan, skill and checks), and hands a "
        "site that needs one to the browser.",
        (("instructions", "projects.plan.write"), ("instructions", "projects.do.item"),
         ("instructions", "projects.act.learn"), ("templates", "projects.plan.response"),
         ("templates", "projects.do.item.response"), ("templates", "projects.act.response"))),
    AgentKind(
        PATHFINDER, "Pathfinder",
        "Maps how each job site works in the browser (search, filters, recency) so the browser reader can search it "
        "reliably.",
        (("instructions", LEARNER_INSTRUCTIONS), ("templates", SKILL_TEMPLATE),
         ("templates", RUNBOOK_TEMPLATE))),
    AgentKind(
        RESEARCHER, "Researcher",
        "Researches each role and its employer, scores the fit and prepares interview reports.",
        (("instructions", "jobsearch.analyst"), ("instructions", "jobsearch.researcher"),
         ("instructions", "jobsearch.scoring"), ("templates", "jobsearch.analysis"),
         ("templates", "jobsearch.researcher.report"), ("instructions", "jobsearch.coach"),
         ("instructions", "jobsearch.coach.review"), ("templates", "jobsearch.coach.report"),
         ("templates", "jobsearch.coach.review"), ("instructions", "jobsearch.coach.clarify"),
         ("templates", "jobsearch.coach.clarify"), ("instructions", "jobsearch.mail"),
         ("templates", "jobsearch.mail.report"))),
    AgentKind(
        WRITER, "Writer", "Writes the tailored resume and cover letter for a role.",
        (("instructions", "jobsearch.composer"), ("instructions", "jobsearch.facts"),
         ("instructions", "jobsearch.fixer"), ("instructions", "jobsearch.arbitrator"),
         ("instructions", "jobsearch.library.lines"), ("instructions", "jobsearch.library.letters"),
         ("instructions", "jobsearch.library.check"), ("instructions", "jobsearch.library.fix"),
         ("templates", "jobsearch.packet"), ("templates", "jobsearch.facts.report"),
         ("templates", "jobsearch.fix"), ("templates", "jobsearch.arbitrator.report"),
         ("templates", "jobsearch.library.lines"), ("templates", "jobsearch.library.letters"),
         ("templates", "jobsearch.library.check"), ("templates", "jobsearch.library.fix"))),
    AgentKind(
        BROWSER_READER, "Browser reader",
        "Reads the job sites in the browser and finds each role's original posting.",
        (("instructions", READER_INSTRUCTIONS), ("templates", READER_TEMPLATE))),
    AgentKind(
        TITLE_CHECKER, "Title checker",
        "Reads place, pay and way of working from a role's header and tags it with a target role.",
        (("instructions", "jobsearch.titles"), ("templates", "jobsearch.titles.report"))),
    AgentKind(
        SEARCH, "Search", "Searches the job boards for new roles.",
        (("instructions", "jobsearch.scout"), ("templates", "jobsearch.scout.report"))),
)


# --- the order ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rung:
    """One way a kind is handed out: `gate` says which limits stand in front of it, `scope` which part of the kind's
    work it is for (`free`'s `scope`)."""

    kind: str
    gate: str
    scope: str | None = None


#: The gates. LAUNCH ignores peak hours, the writer's pause and the hold (the owner's own action). ASK is the owner at
#: the screen: nothing pauses it, only the hold passes it over. NEED is work that runs on need at any hour. OWNERS is
#: NEED with only the owner's own work unless the owner has started the kind lately. SCHEDULED is scheduled work: it
#: keeps to the owner's off hours and the nightly budget (`spare`) unless the owner started the kind lately, and then
#: only what the owner started is handed out. SPARE is work that runs only when there is room (`spare`).
LAUNCH, ASK, OWNERS, SCHEDULED, NEED, SPARE = "launch", "ask", "owners", "scheduled", "need", "spare"

#: The pool's order, the most rightward job first (the owner, 2026-10-15): Launch in Browser's own
#: seats; the owner's asks (a posting to split, a check of their roles, Find Gaps); the owner's own seats (a packet they
#: asked for, the story library their edit started: they go before the browser's backlog, which can stay non-empty for
#: hours); the owner's mail (a short Haiku session, and the owner's own: it goes before the same backlog, so a
#: message never waits behind a long run of browser items); the browser (map, then source,
#: read and scan); site learning; packets; an interview's prep (it has a date); the short reads of a role's header lines
#: (gates before an analyst's session, so a role a cheap gate stops is never analysed); the research of an employer, the
#: research of a role; the search; and last the one-off tagging. Peak hours hold all of it but Launch
#: (`availability.barred`).
ORDER: tuple[Rung, ...] = (
    Rung(SEAT, LAUNCH, "launch"),
    Rung(PARSER, ASK), Rung(CHECKER, ASK), Rung(GAPS, ASK),
    Rung(SEAT, OWNERS),
    Rung(MAIL, NEED),
    Rung(PATHFINDER, SCHEDULED), Rung(BROWSER_READER, SCHEDULED),
    Rung(SITE, NEED),
    Rung(SEAT, SCHEDULED),
    Rung(COACH, NEED),
    Rung(TITLES, NEED, "facts"),
    Rung(RESEARCHER, NEED), Rung(ANALYSIS, NEED),
    Rung(SEARCH, SCHEDULED),
    Rung(TITLES, SPARE, "tagging"),
)

#: How long a kind that was handed nothing is passed over when what waits for it does not change (`saw`).
HOLD = timedelta(minutes=10)
#: How long a free pool session's ask stands: `pull` starts a job only while one asked within it.
POOL_ASK_STANDS = timedelta(minutes=5)
#: How many fed sessions are remembered before the oldest are forgotten.
FED_KEPT = 1000

#: (owner, kind) -> (when it was handed nothing, what waited for the kind then).
_held: dict[tuple[str, str], tuple[datetime, object]] = {}
#: The sessions that have been handed something, and when last: a session that worked and then finds nothing left has
#: ended its work as it should, which holds nothing.
_fed: dict[str, datetime] = {}
#: When a free pool session last asked for work (`hand_out`), per owner.
_pool_asked: dict[str, datetime] = {}
#: How often the work queue's minute tick asks for a pull while a free pool session asked lately: the hours, the day's
#: room, the gap between searches and Claude's usage limit change with no write, so no writer can post them, and five
#: minutes (as long as an ask stands) is the most that costs. The nudge is about ten pulls an hour: one a minute would
#: cost too many pulls and too much of the quiet hour's reading.
NUDGE_EVERY = POOL_ASK_STANDS
#: When the pull was last asked for by the nudge, per owner.
_nudged: dict[str, datetime] = {}
#: The owners whose free pool session just asked when no ask stood: the ask that follows is the one that asks for a pull
#: (`_post_ask_edge`), outside the walk, which only reads.
_ask_began: set[str] = set()


def forget() -> None:
    """Drop the hold, the fed sessions, the pool's ask clock and the nudge's (the tests, between cases)."""
    _held.clear()
    _fed.clear()
    _pool_asked.clear()
    _nudged.clear()
    _ask_began.clear()


def pool_asked(owner: str, now: datetime | None = None) -> bool:
    """Whether a free pool session asked for work lately (`POOL_ASK_STANDS`): the one condition `pull` starts a job
    under before it reads anything, and so what the work queue's minute tick and its `pool.owed` handler ask first."""
    asked = _pool_asked.get(owner)
    return asked is not None and (now or utc_now()) - asked <= POOL_ASK_STANDS


def nudge_due(owner: str, now: datetime | None = None) -> bool:
    """Whether the minute tick should ask for a pull now (a free pool session asked lately, and no nudge within
    `NUDGE_EVERY`), and if so, note that it did. The first nudge after a start or a quiet spell is at once."""
    now = now or utc_now()
    if not pool_asked(owner, now):
        return False
    last = _nudged.get(owner)
    if last is not None and timedelta(0) <= now - last < NUDGE_EVERY:
        return False
    _nudged[owner] = now
    return True


# --- the adapters: what each kind answers to `free` -------------------------------------------------------------------

#: `free(session, owner, now=None, *, mine=(), scope=None) -> list[id]`: the ids a session could take now, in the order
#: they would be handed, leaving out what is claimed for another session (`mine` are the ids claimed for the caller, and
#: go first). It reads only.
Free = Callable[..., Awaitable[list]]


#: `doing(session, owner, role=None, run=None, guess=True) -> words`: what a session of the kind is at, in the words the
#: Agents page uses.
Doing = Callable[..., Awaitable[str]]

#: `heard(session, owner, sid, state, claim) -> verdict`: what a kind does when its session says it started, is alive or
#: ended (`heard`): "stop" (end the session now), "finish" (let it finish what it holds), or None.
Heard = Callable[..., Awaitable[str | None]]


@dataclass(frozen=True)
class _Adapter:
    """What the module asks of a kind. `batch` is how many of `free`'s ids one claim holds; `signature` says what waits
    when the default (the count and the ids a claim would hold) is not what changes; `heard` is what the kind does when
    its session says it started, is alive or ended."""

    free: Free
    doing: Doing
    batch: int = 1
    signature: Callable[[AsyncSession, str], Awaitable[object]] | None = None
    heard: Heard | None = None


_wired: SimpleNamespace | None = None


def _wire() -> SimpleNamespace:
    """The modules the walk reads and the adapter of every kind, built on the first call: they reach every kind's
    module, which reach this one for its names."""
    global _wired
    if _wired is not None:
        return _wired

    from app.jobsearch.browser import leases as browser_leases
    from app.jobsearch.browser import pace as browser_pace
    from app.jobsearch.browser import queue as browser_queue
    from app.jobsearch.gaps import coach as gap_coach
    from app.jobsearch.gaps import runs as find_gaps
    from app.jobsearch.gaps import tools as gap_tools
    from app.jobsearch.gating import gate_facts
    from app.jobsearch.interviews import coach
    from app.jobsearch.jobs import check as role_check
    from app.jobsearch.jobs import parse as role_parse
    from app.jobsearch.mail import haiku as mail_haiku
    from app.jobsearch.missions import search_run as run
    from app.jobsearch.models import ParseJob
    from app.jobsearch.pool import budget
    from app.jobsearch.pool import core as pool
    from app.jobsearch.pool import pull as pool_pull
    from app.jobsearch.profile import store as profile_store
    from app.jobsearch.research import analysis
    from app.jobsearch.research import employer as employer_research
    from app.jobsearch.work import availability, titles
    from app.jobsearch.work import hours as night
    from app.jobsearch.writing import seats
    from app.sites import tools as site_tools

    def one(module: Any) -> Free:
        """A kind whose work is one row at a time: the module's oldest waiting."""

        async def free(session, owner, now=None, *, mine=(), scope=None) -> list:
            row = await module.waiting(session, owner)
            return [row.id] if row is not None else []

        return free

    def browser(name: str) -> Free:
        worker = browser_pace.PATHFINDER if name == PATHFINDER else browser_pace.WORKER

        async def free(session, owner, now=None, *, mine=(), scope=None) -> list:
            return await browser_queue.free(session, owner, worker, now, mine=mine, scope=scope)

        return free

    async def heard_analysis(session, owner, sid, state, claim) -> str | None:
        """The roles an analyst session took are its own while it says it is alive (a killed one is noticed in minutes,
        not at the end of the lease), and are let go the moment it says it ended without sending them."""
        if sid and state == "ended":
            await analysis.release_claims(session, owner, sid)
        elif sid and state in ("started", "alive"):
            await analysis.renew_claims(session, owner, sid)
        return None

    async def heard_seat(session, owner, sid, state, claim) -> str | None:
        """A seat a session of the writer's kind took is free again the moment the session ends without answering it
        (`seats.release`); while it runs, the seat's lease holds it."""
        if sid and state == "ended":
            await seats.release(session, owner, sid)
        return None

    async def heard_browser(session, owner, sid, state, claim) -> str | None:
        """A browser session: the item it holds goes back the moment it ends, and one that has handed nothing back, nor
        called, for a lease's length is told to stop (`stop`); one holding an item finishes it first (`finish`), which
        the caller applies when the app stops sessions (the blackout: `browser_door.next_item` then tells it to
        stop)."""
        if not sid:
            return None
        if state == "ended":
            await browser_leases.release_session(session, owner, sid, "the session ended")
        elif state == "alive":
            if await browser_queue.silent(session, owner, sid):
                return "stop"
            if await browser_leases.holds_item(session, owner, sid):
                return "finish"
        return None

    async def heard_gaps(session, owner, sid, state, claim) -> str | None:
        """A Find Gaps session that ends with work it took still open fails it at once, so the owner can ask again."""
        if state == "ended":
            await gap_tools.session_ended(session, owner)
        return None

    async def heard_coach(session, owner, sid, state, claim) -> str | None:
        """The Coach's session that ends with a Find Gaps job it took still open does the same."""
        if state == "ended":
            await gap_coach.session_ended(session, owner)
        return None

    async def heard_search(session, owner, sid, state, claim) -> str | None:
        """A search session has no take-next call: its start takes the claim made for it (`hand_outs.py`)."""
        if state == "started" and claim:
            await hand_outs.take(session, owner, SEARCH, str(claim), sid or "")
        return None

    def says(text: str) -> Doing:
        async def doing(session, owner, *, role=None, run=None, guess=True) -> str:
            return text

        return doing

    def says_of(module: Any) -> Doing:
        """A kind whose words its module tells (`activity`)."""

        async def doing(session, owner, *, role=None, run=None, guess=True) -> str:
            return await module.activity(session, owner)

        return doing

    async def doing_analysis(session, owner, *, role=None, run=None, guess=True) -> str:
        """The analyst's session has no role of its own in the data: it is told by `role` when the caller knows which
        (`roster.runner_roles`), else it is the first in the analyst's line; `guess` False says the caller cannot tell
        which, and the words name none."""
        role = role or (await analysis.waiting(session, owner) if guess else None)
        return f"Researching {role.title} at {role.company}" if role else "Researching a role"

    async def doing_seat(session, owner, *, role=None, run=None, guess=True) -> str:
        return await seats.activity(session, owner) or "Writing a packet"

    async def doing_search(session, owner, *, role=None, run=None, guess=True) -> str:
        return "Searching for roles" + (f": {run.focus}" if run is not None and run.focus else "")

    async def waiting_of_search(session, owner) -> object:
        open_run = await run.open_run(session, owner)
        return (str(open_run.id), tuple(await run.waiting_steps(session, open_run))) if open_run else None

    def waiting_of_browser(name: str) -> Callable[[AsyncSession, str], Awaitable[object]]:
        worker = browser_pace.PATHFINDER if name == PATHFINDER else browser_pace.WORKER

        async def signature(session, owner) -> object:
            return await browser_queue.waiting_count(session, owner, browser_pace.WORK[worker])

        return signature

    adapters = {
        PARSER: _Adapter(one(role_parse), says_of(role_parse)),
        CHECKER: _Adapter(one(role_check), says("Checking your roles")),
        GAPS: _Adapter(one(gap_tools), says_of(find_gaps), heard=heard_gaps),
        SEARCH: _Adapter(run.free, doing_search, signature=waiting_of_search, heard=heard_search),
        TITLES: _Adapter(titles.free, says("Judging titles"), batch=titles.PER_SESSION + gate_facts.BATCH),
        ANALYSIS: _Adapter(analysis.free, doing_analysis, heard=heard_analysis),
        RESEARCHER: _Adapter(employer_research.free, says_of(employer_research)),
        SITE: _Adapter(site_tools.free, says("Mapping a site")),
        SEAT: _Adapter(seats.free, doing_seat, heard=heard_seat),
        COACH: _Adapter(coach.free, says_of(coach), heard=heard_coach),
        MAIL: _Adapter(mail_haiku.free, says("Reading the mail"), batch=mail_haiku.BATCH),
        BROWSER_READER: _Adapter(browser(BROWSER_READER), says("Working in the browser"),
                                 signature=waiting_of_browser(BROWSER_READER), heard=heard_browser),
        PATHFINDER: _Adapter(browser(PATHFINDER), says("Mapping a site in the browser"),
                             signature=waiting_of_browser(PATHFINDER), heard=heard_browser),
    }
    _wired = SimpleNamespace(
        adapters=adapters, availability=availability, budget=budget, night=night, pool=pool, pool_pull=pool_pull,
        profile_store=profile_store, role_parse=role_parse, ParseJob=ParseJob)
    return _wired


# --- the walk ---------------------------------------------------------------------------------------------------------


@dataclass
class _Ctx:
    """What one ask reads once: the hour, the room left in the nightly budget, the agent kinds the owner paused, the
    kinds the hold passes over, the browser kinds that cannot run and the kinds the owner started lately."""

    owner: str
    now: datetime | None
    off_hours: bool
    spare: bool
    paused: set[str]
    held: set[str]
    #: The browser kinds nothing is handed out for now: the owner's own Chrome cannot be had, unless they asked for it.
    down: set[str]
    #: The kinds of work the owner started lately (`pool.owners_running`), read once for the ask.
    running: set[str]

    def owner_running(self, group: str | None) -> bool:
        return group is not None and group in self.running


async def _ctx(session: AsyncSession, owner: str, settings, now: datetime | None) -> _Ctx:
    w = _wire()
    off_hours = w.night.in_off_hours(settings, now)
    # The nightly budget (`budget.py`): spent or ahead of the clock, the pool hands out only what the owner started and
    # starts nothing of its own.
    spare = off_hours and await w.budget.hold(session, owner, now) is None
    down = {k for k in hand_outs.BROWSER_KINDS if await w.pool.browser_held(session, owner, k, now)}
    return _Ctx(owner, now, off_hours, spare, await w.pool.paused_kinds(session, owner),
                await _held_kinds(session, owner, now), down, await w.pool.owners_running(session, owner, now))


def _note_pool_ask(owner: str, now: datetime) -> None:
    """A free pool session asked and the walk got past the hold and the limit: `pull` may start work for it. An ask that
    comes when none stood (the peak hours ended, a pause lifted, the first start) is remembered as the one that asks
    for a pull (`_post_ask_edge`)."""
    if not pool_asked(owner, now):
        _ask_began.add(owner)
    _pool_asked[owner] = now


def _post_ask_edge(session: AsyncSession, owner: str, now: datetime | None = None) -> None:
    """The ask that began a standing ask (see `_note_pool_ask`) is the change that makes the pull look, which the old
    loop saw within ten seconds of any ask: it posts `pool.owed`, in the ask's transaction and after its walk (which
    only reads). An ask that stands (a lane asks every two minutes, and an ask stands five) posts nothing: the nudge is
    the clock's."""
    from app.jobsearch.work import queue

    if owner in _ask_began:
        _ask_began.discard(owner)
        queue.post(session, owner, queue.POOL_OWED, [""], cause="ask", now=now)


async def _walk(session: AsyncSession, owner: str, runner: str | None, now: datetime | None, *,
                peek: bool) -> tuple[str, str | None] | None:
    """The kind of the next job and the scope it is for, or None: the rungs of `ORDER`, each under its own gate.
    Launch's rung stands in front of the hold at any hour (a runner past the owner's limit on active sessions never gets
    it); every other rung is passed over while anything holds the pool (`availability.standing`), and by day for a
    runner past the limit, and is otherwise tried under the gates of `_scope`. It only reads (the owner's ask for a
    browser job is used up by the claim, in `_hand_out`): a `peek` is not a free pool session asking (`_pool_asked`),
    which only a walk that gets past the hold and the limit to the rest of the order is."""
    w = _wire()
    settings = (await w.profile_store.current(session, owner)).settings
    over = w.pool.over_limit(runner, settings.active_sessions)
    # One read answers both: the pool's hold, and Launch's, which is the same but for peak hours (the owner's own act).
    hold = await w.availability.standing(session, owner, w.availability.Question.POOL)
    launch = not over and all(stop.reason is w.availability.Reason.PEAK for stop in hold)
    ctx: _Ctx | None = None
    stopped = False
    for rung in ORDER:
        adapter = w.adapters[rung.kind]
        if rung.gate == LAUNCH:
            if launch and await adapter.free(session, owner, now, scope=rung.scope):
                return rung.kind, rung.scope  # Launch in Browser is the owner's own action: at any hour
            continue
        if stopped:
            continue
        if ctx is None:
            # the code-only work goes on in the pipeline's own turn (`schedule.pipeline_pass`), peak or not; a runner
            # past the owner's limit by day finishes what it has, then is idle
            if hold or (over and not w.night.in_off_hours(settings, now)):
                stopped = True
                continue
            if not peek:
                _note_pool_ask(owner, now or utc_now())  # a free pool session is there: `pull` may start work for it
            ctx = await _ctx(session, owner, settings, now)
        go, scope = _scope(rung, ctx)
        if go and await adapter.free(session, owner, now, scope=scope):
            return rung.kind, scope
    return None


def _scope(rung: Rung, ctx: _Ctx) -> tuple[bool, str | None]:
    """Whether this rung may be tried, and the scope it asks `free` for: the hold passes over a kind, a browser kind
    that cannot run is passed over, and a paused agent kind passes over all its kinds but the owner's asks."""
    if rung.kind in ctx.held or rung.kind in ctx.down:
        return False, None
    group = _BY_NAME[rung.kind].group
    if rung.gate == ASK:
        return True, rung.scope
    if group in ctx.paused:
        return False, None
    if rung.gate == OWNERS:
        return True, None if ctx.owner_running(group) else "owners"
    if rung.gate == SCHEDULED:
        return True, None if ctx.spare or ctx.owner_running(group) else "owners"
    if rung.gate == SPARE:
        return ctx.spare, rung.scope
    return True, rung.scope


async def next_kind(session: AsyncSession, owner: str, runner: str | None = None, *,
                    now: datetime | None = None) -> str | None:
    """Which kind of session would be handed out next to this runner, or None: the decision alone. No lock, no row; it
    does not use up the owner's ask for a browser job and does not count as a free pool session asking; it reads only
    (`housekeeping.py`), so a caller that needs the app's own pass first runs `housekeeping.settle`."""
    found = await _walk(session, owner, runner, now, peek=True)
    return found[0] if found is not None else None


async def _reply(session: AsyncSession, row) -> dict:
    """The answer for a claim: the kind, the claim's id and its items (and the parser's own model)."""
    w = _wire()
    reply: dict = {"next": row.kind, "claim": str(row.id), "item": {"id": row.items[0], "ids": list(row.items)}}
    if row.kind == PARSER:  # the claimed job's own model: a recipe, or the master's claims: Sonnet; the rest are Haiku
        import uuid

        job = await session.get(w.ParseJob, uuid.UUID(row.items[0]))
        if job is not None and w.role_parse.is_sonnet(job):
            reply["model"] = "sonnet"
    return reply


async def hand_out(session: AsyncSession, owner: str, runner: str | None = None, *,
                   now: datetime | None = None) -> dict:
    """A runner's ask for work (`GET /api/v1/agent/work`), and the dispatcher's own call for a runner it is about to
    command (`dispatcher.py`): the next kind of session, and the exact items it is for, claimed for this runner in the
    same transaction (`hand_outs.py`). The reply is `{"next": kind}` as it always was, plus `claim` (the id the session
    is started with) and `item` (`id`, `ids`) when something was claimed, and `model` for a parse job that needs Sonnet.
    A claim already made for this runner that no session has taken (its command was lost, or it has not started) is the
    answer first, and nothing new is claimed: the fallback check-in never strands work (path 47.3). The decision is the
    claim: the items are read again under the kind's try-lock with the scope the walk decided on, so what is claimed is
    what the walk saw. The look (the claim made already, the walk) is read in a reader's session where `session` holds
    nothing yet (`off_turn.reading`), so the writer's turn is taken only to claim, and an ask with nothing waiting takes
    none. The ask only reads what is waiting (settling, expiring and starting jobs are the app's own pass:
    `housekeeping.settle`, `pull`) and never waits. The claim is never committed here: the caller commits it, so the
    claim and the answer are one transaction. Before the kind's lock is taken the transaction of the walk is ended where
    that may be (`db.can_release`: not inside a savepoint, nor where the caller holds its transaction whole, as the
    dispatcher's pass does), which keeps what the caller had written so far and holds nothing through the walk; the
    lock, the claim and the answer are then a short transaction of their own. Nothing waiting, or the kind being
    claimed by another ask now: `{"next": None}`. An ask that begins a standing ask of a free pool session asks the
    work queue for a pull (`_post_ask_edge`), after all of the above."""
    reply = await _hand_out(session, owner, runner, now)
    _post_ask_edge(session, owner, now)
    return reply


async def _hand_out(session: AsyncSession, owner: str, runner: str | None, now: datetime | None) -> dict:
    w = _wire()
    # The look (the claim already made for this runner, the walk: rungs, limits, gates, a second of Python between
    # reads) is a reader's where the session holds nothing yet (`off_turn.reading`), so an ask with nothing waiting, or
    # one answered with the claim it was made before, never takes the writer's turn. Where the caller holds its
    # transaction whole (the dispatcher's pass) the look is in `session`, as it was.
    async with off_turn.reading(session) as reader:
        if (waiting := await hand_outs.pending(reader, owner, runner, now)) is not None:
            return await _reply(reader, waiting)
        found = await _walk(reader, owner, runner, now, peek=False)
    if found is None:
        return {"next": None}
    name, scope = found
    # The walk only read. Its transaction (when it was `session`'s) ends here, so the lock and the claim are a short
    # one of their own, and nothing is held through the walk. The items are read again under the lock below, so the
    # claim still decides on what is true then.
    await db.release_if_free(session)
    if not await hand_outs.lock(session, owner, name):
        return {"next": None}  # another ask is claiming this kind now: this one asks again, it does not wait
    adapter = w.adapters[name]
    ids = (await adapter.free(session, owner, now, scope=scope))[: adapter.batch]
    row = await hand_outs.claim(session, owner, name, ids, runner, now)
    if row is None:
        return {"next": None}  # what was waiting went to another ask in the meantime
    if name in hand_outs.BROWSER_KINDS:
        # The owner's Run now for this kind is answered by this job. The ask is a row (`pool.ask_browser`): it goes in
        # the claim's own transaction, here under the writer's turn; the walk above read on a reader and wrote nothing.
        await w.pool.use_up_browser_ask(session, owner, name)
    return await _reply(session, row)


async def doing(session: AsyncSession, owner: str, word: str, *, role: Any = None, run: Any = None,
                guess: bool = True) -> str:
    """What a session of the runner's kind `word` (`of_runner`) is at, in the words the Agents page uses ("Researching
    Lead at Contoso", "Mapping a site", "Reading the mail"). The analyst's session has no role of its own in the data:
    it is told by `role` when the caller knows which, else it is the first in the analyst's line (and the writer's the
    oldest packet being written); `guess` False says the caller cannot tell which, and the words name none. `run` is the
    open search, for the scout's focus. A word that is no kind is "Working"."""
    row = of_runner(word)
    if row is None:
        return "Working"
    return await _wire().adapters[row.name].doing(session, owner, role=role, run=run, guess=guess)


async def heard(session: AsyncSession, owner: str, word: str, sid: str | None, state: str, *,
                claim: str | None = None) -> str | None:
    """What a session of the runner's kind `word` (`of_runner`) says about itself (`started`, `alive` or `ended`, from
    its heartbeat) does for its kind: the roles an analyst took stay its own while it lives and are let go when it ends,
    a seat is freed when its session ends, a browser session's item goes back when it ends, and a Find Gaps job it left
    open fails, a search session's start takes the claim made for it. Each kind's part runs in a savepoint of its own: a
    failure is logged and never raised, so the heartbeat still answers. Returns "stop" (end the session now: a browser
    session that handed nothing back, nor called, for a lease's length), "finish" (let it finish the item it holds when
    the app stops sessions), or None."""
    row = of_runner(word)
    adapter = _wire().adapters[row.name] if row is not None else None
    if adapter is None or adapter.heard is None:
        return None
    try:
        async with session.begin_nested():
            return await adapter.heard(session, owner, sid, state, claim)
    except Exception:  # the heartbeat has other things to say; the claim falls back to its lease or its silence
        log.exception("couldn't settle what the %s session %s said (%s)", word, sid, state)
        return None


async def pull(session: AsyncSession, owner: str) -> int:
    """The housekeeping's part of the pool's pull (`housekeeping.settle`): start the next job when nothing is queued
    for a free session, in the pool's order (`pool.pull.start_next`), under every limit an ask keeps to
    (`availability.barred`: peak hours, the pause, Claude's usage limit, a restart's drain, and a hold on the kind).
    Only while a free pool session asked lately (`POOL_ASK_STANDS`): nothing is pulled for one that is not there. The
    downstream steps (the Haiku ranking, the deck's Sonnet reads: `bounded`) go at any hour; the scheduled one (a
    search) only when there is room in the owner's off hours and the nightly budget. At most one job is started a time.
    Returns 1 when one was."""
    w = _wire()
    if not pool_asked(owner):
        return 0
    # The look (the limits, the hold, the walk of every kind's waiting work: dozens of statements) and the choice of the
    # job are a reader's, so the writer's turn is not held while they read, and a pull with nothing to start never takes
    # it; the pool's `pull.start_next` owns that, and the safe start under the turn. It is called by attribute so a
    # test's patch on it bites.
    return int(await w.pool_pull.start_next(session, owner, lambda reader: _look(reader, owner)))


async def _look(session: AsyncSession, owner: str) -> tuple[set[str], bool] | None:
    """What `pull` asks before it starts a job, reading only: None when nothing may be started (a limit holds the pool,
    or work for a free session waits already), else the kinds not to queue and whether the night has room."""
    w = _wire()
    if await w.availability.barred(session, owner, w.availability.Question.POOL) is not None:
        return None
    settings = (await w.profile_store.current(session, owner)).settings
    ctx = await _ctx(session, owner, settings, None)
    if await _walk(session, owner, None, None, peek=True) is not None:
        return None
    # A held kind is not queued again either: its sessions were just handed nothing.
    return ctx.paused | {g for k in ctx.held if (g := _BY_NAME[k].group) is not None}, bool(ctx.spare)


# --- the backstop: a kind that was handed nothing is not offered again until what waits for it changes
# ------------------


async def _signature(session: AsyncSession, owner: str, name: str) -> object:
    """What waits for a kind of session now, as a value that changes when that does: what a claim would hold (the count
    and the first ids) unless the kind says otherwise."""
    adapter = _wire().adapters[name]
    if adapter.signature is not None:
        return await adapter.signature(session, owner)
    ids = await adapter.free(session, owner, None)
    return len(ids), tuple(str(i) for i in ids[: adapter.batch])


async def saw(session: AsyncSession, owner: str, name: str, *, empty: bool, sid: str | None = None,
              now: datetime | None = None) -> None:
    """A session of this kind (`sid`, when the call names it) asked its own take-next call and got nothing (`empty`), or
    got something (which lifts any hold). A kind that was just handed nothing is not offered again until what waits for
    it changes, or for `HOLD`. Nothing
    is held for a session that was handed something before. Kept in the app's memory, not the database: a pace, lost
    harmlessly on a restart."""
    now = now or utc_now()
    if not empty:
        _held.pop((owner, name), None)
        if sid:
            _fed[sid] = now
            if len(_fed) > FED_KEPT:
                for old in sorted(_fed, key=_fed.__getitem__)[: len(_fed) - FED_KEPT]:
                    _fed.pop(old, None)
        return
    if sid and sid in _fed:
        return
    _held[(owner, name)] = (now, await _signature(session, owner, name))


async def _held_kinds(session: AsyncSession, owner: str, now: datetime | None = None) -> set[str]:
    """The kinds the walk passes over now: handed nothing within `HOLD`, and what waits for them unchanged since. A hold
    that lapsed or whose signature changed is dropped."""
    now = now or utc_now()
    out: set[str] = set()
    for (who, name), (at, was) in list(_held.items()):
        if who != owner:
            continue
        if now - at >= HOLD or await _signature(session, owner, name) != was:
            _held.pop((who, name), None)
            continue
        out.add(name)
    return out
