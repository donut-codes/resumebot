"""The catch-up after a tracker import (docs/PRD.md 2.9).

Researching a few roles a night is right for what a search finds, and slow for what an import brings: the roles a
tracker holds have no score and no research, and the app is not fully working until they have. So an import that leaves
roles to research starts a catch-up (`start`, from the import command): every role the tracker brought in that is still
in play and was never researched is researched at once, a few at a time (`settings.analysts` with the analyst, one
after another by default), by the analyst the nightly mission uses (`analyse_role.bpmn`), with neither the mission's
number for a night nor the research bar (an imported role has no score yet). Then the employers' job systems are
looked for, by the app's own detection (`resolve.py`), so the boards can be read.

"Brought in by the tracker" is every role stored with `origin` tracker, whichever import stored it. An import that
adds nothing new still finds what an earlier one left unresearched, and the roles whose stage it brought up to date
(a role the tracker now has as Applied is one to research first).

It is the process `catch_up.bpmn`, one run of it for each import, and a line in Analyse the finds' history on Missions
(`MissionRun`, started by "import"): the order of work is the file's, the steps here are the work. The run waits at four
places, and `advance` brings each wait to an end when what it waits for has happened:

- the roles being matched to a target role (the one-off tagging, `tagging.py`, so the analyst has the role's own asks);
- the analyst finishing a role it has (at most `settings.analysts` at a time, the next as each is done: never all at
  once, since the sessions that research are that many and a role is "with the analyst" only when one can take it; and
  never two on one site, so that most links being one board's means one of those at a time, `action_hand_out`);
- Claude being free to use: not in the peak hours the owner avoids, not at its usage limit, the scout not paused (the
  run holds and says why and until when; a research that meets the usage limit is taken back and tried again when it
  lifts, not the next night);
- the next pass of the schedule, for the employers' job systems (network work, a few employers a pass).

A role that fails (the sweep, a session that ended) goes to the back of the line and is tried again, up to
`ROLE_TRIES` times in all; one that cannot be researched is said so, never lost. A role further along is researched
but keeps its stage (`analysis.action_settle`). `advance` runs in the schedule pass (all of it) and whenever the
runner asks for work (the cheap part: handing out the next role at once), so one role follows another with no gap.
Services flush; the import command, the
schedule pass and the heartbeat commit.
"""

import copy
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from process_engine import versions as proc_versions
from process_engine.errors import ActionError, NotWaiting
from process_engine.models import COMPLETED, FAILED, ProcInstance
from process_engine.registry import actions as registry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app import db
from app.errors import DomainError
from app.jobsearch import ats, process, stages
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.employers import resolve
from app.jobsearch.fetch import polite
from app.jobsearch.fit import classifier, freshness, scoring
from app.jobsearch.fit import role_order as queue
from app.jobsearch.missions import record
from app.jobsearch.models import ORIGIN_TRACKER, Employer, MissionRun, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, line
from app.jobsearch.work import availability, engine_calls, tagging
from app.jobsearch.work import hours as night
from app.settings import settings
from app.timeutil import aware, owner_today, utc_now

log = logging.getLogger(__name__)

#: A role is asked for this many times in all (the first and two more) before the catch-up says it could not be
#: researched.
ROLE_TRIES = 3
#: The employers whose job system is looked for in one pass of the schedule (it reads their pages politely, one request
#: a second to each host).
BOARDS_A_PASS = 20

#: Why no research is handed out (`availability`'s reasons, as a run stores them in its hold): the scout is paused,
#: Claude is at its usage limit, or it is inside the peak hours the owner avoids. The Missions text's own words.
HOLD_WORDS: dict[str, str] = {availability.Reason.PAUSED: "the scout is paused",
                              availability.Reason.USAGE: availability.USAGE_LIMIT,
                              availability.Reason.PEAK: "Claude's peak hours"}

#: The messages the process waits on (`catch_up.bpmn`), and which wait each ends.
MATCHED, FREE, RESEARCHED, PASS = "catch_up.matched", "catch_up.free", "catch_up.researched", "catch_up.pass"

#: What `Hand_out` says (the process's gateway reads it).
ASKED, HELD, NONE_LEFT = "asked", "held", "none"


class CatchUpError(DomainError):
    """The catch-up can't start; plain words."""

    status = 422


@dataclass
class Started:
    """A catch-up that has just started: its run, who to wake (the scout, for the matching or the first role), and
    what to tell the person who ran the import."""

    entry: MissionRun
    knocks: list[tuple[str, str]] = field(default_factory=list)
    words: str = ""


# --- the run's state ----------------------------------------------------------------------------------------------


def new_state() -> dict:
    return {
        "instance": None, "phase": "picking",
        #: How many roles the tracker holds, the ones picked to research (in the order they are handed out), and the
        #: ones still to hand out. A role that failed is put back at the end.
        "tracker_count": 0, "roles": [], "queue": [], "left_out": 0,
        #: The roles with the analyst now (up to `settings.analysts`), how many times each was asked for, and the ones
        #: that could not be researched.
        "inflight": [], "tries": {}, "gave_up": [],
        #: The employers of the tracker's roles, how many of them wanted a job system looked for at the start, those
        #: that had one then, and those looked at since.
        "employers": [], "to_look": 0, "boards": {"before": [], "looked": []},
        #: Why nothing is handed out now ({why, until}); when Claude's usage limit was met ({until, seen_at, text});
        #: and who to wake once the work is saved ([id, reason]).
        "hold": None, "limit": None, "knock": [],
    }


def state_of(entry: MissionRun) -> dict:
    """The run's catch-up state, as a copy to change and hand back to `save`."""
    st = copy.deepcopy((entry.started or {}).get("catch_up") or new_state())
    if "inflight" not in st:  # a run saved when the analyst had one role of it at a time ("current")
        st["inflight"] = [st["current"]] if st.get("current") else []
    st.pop("current", None)
    return st


def save(entry: MissionRun, st: dict, **more) -> None:
    """Keep `st` on the run. The run's JSON is replaced, not changed in place: a changed copy of the same dict looks
    like no change to the session."""
    entry.started = {**(entry.started or {}), "catch_up": st, **more}


def is_catch_up(entry: MissionRun) -> bool:
    return entry.started_by == record.IMPORT and bool((entry.started or {}).get("catch_up"))


async def running(session: AsyncSession, owner: str, *, fresh: bool = False) -> list[MissionRun]:
    """The catch-ups that are running, oldest first. `fresh` reads them again in place: the write transaction is the one
    writer, so two passes (or a heartbeat) take turns and none writes a run's state from a copy another has changed."""
    query = select(MissionRun).where(
        MissionRun.owner == owner, MissionRun.started_by == record.IMPORT, MissionRun.outcome == "running"
    ).order_by(MissionRun.started_at)
    if fresh:
        query = query.execution_options(populate_existing=True)
    return [e for e in (await session.scalars(query)).all() if is_catch_up(e)]


async def _entry(session: AsyncSession, data: dict) -> MissionRun:
    entry = await session.get(MissionRun, uuid.UUID(data["run"]))
    if entry is None:
        raise ActionError("no_run", "the catch-up's run isn't there")
    return entry


# --- starting ---------------------------------------------------------------------------------------------------


@dataclass
class Plan:
    """What a catch-up would take on now."""

    brought: list[Opportunity]  # every role the tracker brought in (stored with `origin` tracker), in any stage
    picked: list[Opportunity]  # those to research: in play, not researched, no other catch-up's; in the order they go
    employers: list[str]  # the ids of the employers of the tracker's roles that no other catch-up has
    looking: int  # how many of those employers want their job system looked for


async def plan(session: AsyncSession, owner: str, *, ordered: bool = True) -> Plan:
    brought = list((await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.origin == ORIGIN_TRACKER))).all())
    taken_roles: set[str] = set()
    taken_employers: set[str] = set()
    for other in await running(session, owner):  # a role or an employer is one catch-up's at a time
        theirs = state_of(other)
        taken_roles |= {*theirs["roles"], *theirs["inflight"]}
        taken_employers |= set(theirs["employers"])
    # Not researched: never asked for, or asked for and failed (an earlier run's sweep, or a session that ended). A role
    # the analyst has now (asked for by Run now) is not.
    todo = [r for r in brought if r.status in READ_AT_ONCE and r.analysis_state in (None, analysis.FAILED)
            and str(r.id) not in taken_roles]
    picked = await in_order(session, owner, todo) if ordered else todo
    employer_ids = sorted({str(r.employer_id) for r in brought if r.employer_id} - taken_employers)
    rows = list((await session.scalars(select(Employer).where(
        Employer.id.in_([uuid.UUID(i) for i in employer_ids] or [None])))).all())
    return Plan(brought, picked, employer_ids, sum(1 for e in rows if _looking(e)))


async def start(session: AsyncSession, owner: str) -> Started | None:
    """Start the catch-up after an import, or None when there is nothing for it: no role the tracker brought in is left
    to research and no employer of one wants its job system looked for. Makes the run in Analyse the finds' history and
    the process, and runs the process to its first wait (picking the roles, starting the matching, handing out the
    first role). Raises `CatchUpError` when the process can't start; the import command then saves nothing."""
    wanted = await plan(session, owner, ordered=False)
    if not wanted.picked and not wanted.looking:
        return None
    from app.jobsearch.missions import core as missions  # the missions import this module for their hooks

    entry = await missions.open_run(session, owner, record.ANALYSE, started_by=record.IMPORT, started={},
                                    summary=f"Starting: {len(wanted.picked):,} roles to research.")
    save(entry, new_state(), target=0, done=0, instances=[])
    await session.flush()
    # The import command's own process has not read the owner's saved versions of the process files, as the app does at
    # its start: the first role goes to the analyst on the version in force.
    try:
        await proc_versions.refresh(session)
    except Exception:
        log.exception("couldn't read the saved versions of the process files; the shipped ones are used")
    # The subject is the run's id, as a search's is (`run.start_round`): that is what the live view reads a run by, so
    # "See the run" on Missions opens this process with the step it waits at.
    with db.io_held_knowingly(START_READS):
        instance = await process.engine().start(
            session, process.CATCH_UP, {"owner": owner, "run": str(entry.id)}, subject=f"jobsearch:{entry.id}")
    if instance.status == FAILED:
        raise CatchUpError(f"the catch-up couldn't start: {instance.error}")
    st = state_of(entry)
    st["instance"] = str(instance.id)
    knocks = _take_knocks(st)
    save(entry, st, instances=[str(instance.id)])
    await refresh(session, entry)
    return Started(entry, knocks, _start_words(entry))


def _start_words(entry: MissionRun) -> str:
    """What the import command tells the person who ran it: counts only."""
    st = state_of(entry)
    todo, got = len(st["roles"]), st["tracker_count"]
    if not todo:
        return (f"catch-up: none of the {got:,} roles the tracker holds is left to research (each is closed, "
                "rejected, screened out or already researched); the employers' job systems are looked for.")
    held = st.get("hold")
    if held:
        ends = aware(datetime.fromisoformat(held["until"])) if held.get("until") else None
        how = f"; it waits for {HOLD_WORDS.get(held['why'], held['why'])}"
        how += f", until {night.when_words(ends)}" if ends else ""
    elif st["phase"] == "matching":
        how = ", once they are matched to your target roles"
    else:
        how = ", straight away"
    return (f"catch-up: {todo:,} of the {got:,} roles the tracker holds to research, one after another{how} "
            "(Missions, Analyse the finds, shows how it is going).")


def _take_knocks(st: dict) -> list[tuple[str, str]]:
    """Who the steps asked to have woken, taken off the state (the caller sends them once the work is committed)."""
    out = [(k, why) for k, why in st.get("knock") or []]
    st["knock"] = []
    return out


# --- the process's steps ------------------------------------------------------------------------------------------


async def _roles(session: AsyncSession, ids: list[str], *, lock: bool = False) -> list[Opportunity]:
    if not ids:
        return []
    query = select(Opportunity).where(Opportunity.id.in_([uuid.UUID(i) for i in ids]))
    if lock:
        query = query.order_by(Opportunity.id).execution_options(populate_existing=True)
    return list((await session.scalars(query)).all())


#: A date written in a role's notes ("[2026-10-05] applied", "[2026-10-09] emailed"). The app
#: keeps no date for an application, so how recent one is can only be read from these.
ISO_DAY = re.compile(r"(?<![\w-])(20\d\d)-(\d\d)-(\d\d)(?![\w-])")
#: The stages whose roles go to the analyst first: an application made, or in talks (the owner, 2026-10-13).
IN_MOTION = (stages.ACTIVE_DISCUSSION, stages.APPLIED)
#: The statuses whose roles the catch-up has researched at once. A role before the deck is ranked and read line by
#: line only when it is among the best N (`pipeline/deck.py`): Sonnet reads the top N, never every role the file brought
#: in.
READ_AT_ONCE = (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH, *IN_MOTION)


def last_dated(role: Opportunity, today: date) -> date | None:
    """How recent the role's application is, as far as the role says: the latest date in its notes, else the day it was
    found. A date still ahead (a deadline, an interview to come) is not one. None when the role holds no date."""
    days = [role.found] if role.found is not None else []
    for year, month, day in ISO_DAY.findall(role.notes or ""):
        try:
            days.append(date(int(year), int(month), int(day)))
        except ValueError:  # 2026-02-31
            continue
    days = [d for d in days if d <= today]
    return max(days) if days else None


async def in_order(session: AsyncSession, owner: str, roles: list[Opportunity]) -> list[Opportunity]:
    """The order the roles go to the analyst in (the owner, 2026-10-13): applications first, Applied and Active
    Discussion together with the most recent application first; then Ready to Launch; then every other role, the
    youngest posting first and one it can't date last (`freshness.youngest_first`: getting in in the first 24 hours is
    worth much). Where two are the same age, and in Ready to Launch, the likeliest first: the classifier's probability
    that the role is the owner's kind of job (the order the retired title pass and readers took their postings in),
    where
    the owner has enough history for it to have a model, then the queue rule (fit percent, then the fresher posting)."""
    today, now = owner_today(), utc_now()
    span = scoring.bounds(await profile_store.current(session, owner))
    model = await classifier.train(session, owner) if any(r.status not in IN_MOTION for r in roles) else None
    seen = await freshness.first_seen_by_role(session, owner) if roles else {}

    def key(role: Opportunity) -> tuple:
        by_queue = queue.queue_key(scoring.percent_of(role, span), queue.age_of(role.posted, role.found, today))
        if role.status in IN_MOTION:
            day = last_dated(role, today)
            return 0, day is None, -(day.toordinal() if day else 0), *by_queue, str(role.id)
        likely = model.predict(classifier.text_of(role.title, None, role.description)) if model else 0.0
        if role.status == stages.READY_TO_LAUNCH:
            return 1, False, -likely, *by_queue, str(role.id)
        undated, hours = freshness.youngest_first(role.posted, role.found, seen.get(role.id), today, now)
        return 2, undated, hours, -likely, *by_queue, str(role.id)

    return sorted(roles, key=key)


async def action_pick(session, instance, data):
    """Of the roles the tracker brought in, the ones to research: in play, not researched and no other catch-up's, in
    the order `in_order` gives; and the employers whose job system is to be looked for."""
    entry = await _entry(session, data)
    st = state_of(entry)
    taking = await plan(session, entry.owner)
    ids = [str(r.id) for r in taking.picked]
    with_system = await session.scalars(select(Employer.id).where(
        Employer.id.in_([uuid.UUID(i) for i in taking.employers] or [None]), Employer.ats.is_not(None)))
    st.update({"roles": ids, "queue": list(ids), "employers": taking.employers, "to_look": taking.looking,
               "tracker_count": len(taking.brought), "left_out": len(taking.brought) - len(ids),
               "phase": "matching" if ids else "boards"})
    st["boards"] = {"before": [str(i) for i in with_system.all()], "looked": []}
    save(entry, st, target=len(ids), done=0)
    return {"count": len(ids), "brought": len(taking.brought), "left_out": len(taking.brought) - len(ids)}


async def action_match(session, instance, data):
    """Start the one-off matching of roles to target roles for the picked roles that have none (`tagging.py`): the
    analyst researches a role against its own target role's asks, and the roles an import brings in have none."""
    entry = await _entry(session, data)
    st = state_of(entry)
    untagged = [r for r in await _roles(session, st["roles"]) if r.target_role is None]
    if not untagged:
        return {"open": False, "count": 0}
    try:
        job, _count = await tagging.start(session, entry.owner)
    except tagging.TagError:  # no target role to match them to: they are researched against the asks for every role
        return {"open": False, "count": len(untagged)}
    if job is None:
        return {"open": False, "count": 0}
    st["knock"] = [*st["knock"], [str(job.id), engine_calls.TAG_ROLES]]
    save(entry, st)
    return {"open": True, "count": len(untagged)}


async def _held(session: AsyncSession, owner: str, now: datetime | None) -> dict | None:
    """Why no research is handed out now, and until when (None: Claude can be used): the catch-up's question to
    `availability`, so a limit a lane met holds it as it holds the pool (docs/PRD.md 2.9: it starts no Claude work of
    any kind until the limit lifts), not only the limit its own run learned."""
    stop = await availability.barred(session, owner, availability.Question.CATCH_UP, now=now)
    if stop is None:
        return None
    return {"why": stop.reason.value, "until": stop.until.isoformat() if stop.until else None}


def _limit_until(st: dict, moment: datetime) -> datetime | None:
    """When Claude's usage limit lifts, as the catch-up learned it, while that is still ahead."""
    limit = st.get("limit") or {}
    try:
        until = aware(datetime.fromisoformat(limit["until"]))
    except (KeyError, TypeError, ValueError):
        return None
    return until if until is not None and until > moment else None


async def _settle_inflight(session: AsyncSession, st: dict) -> str:
    """Note how the research of each role that is no longer with the analyst ended, and take it off the roles the
    analyst has: researched (counted from the roles themselves), failed (put back at the end of the line for another
    try, until it has had `ROLE_TRIES`), or taken back (Claude's usage limit: first in line again, in the order it
    was handed out, and the try does not count). Returns the outcome: failed, withdrawn or done (the first of those
    that any role had), waiting when every role is still with the analyst, none when it has none."""
    if not st["inflight"]:
        return "none"
    still, ended, back = [], [], []
    for rid in st["inflight"]:
        found = (await _roles(session, [rid], lock=True))[0:1]
        state = found[0].analysis_state if found else analysis.DONE
        if state == analysis.WAITING:  # still with the analyst (or asked again by someone else meanwhile)
            still.append(rid)
        elif state == analysis.FAILED:
            if int(st["tries"].get(rid, 0)) < ROLE_TRIES:
                st["queue"] = [*st["queue"], rid]
            else:
                st["gave_up"] = [*st["gave_up"], rid]
            ended.append("failed")
        elif state is None:
            back.append(rid)
            st["tries"][rid] = max(int(st["tries"].get(rid, 0)) - 1, 0)
            ended.append("withdrawn")
        else:
            ended.append("done")
    st["queue"] = [*back, *st["queue"]]
    st["inflight"] = still
    return next((o for o in ("failed", "withdrawn", "done") if o in ended), "waiting")


async def _facts(session: AsyncSession, ids: list[str]) -> dict[str, tuple]:
    """(link, stage, analysis state) of the roles, without their texts: a hand-out looks along a long line."""
    if not ids:
        return {}
    rows = (await session.execute(select(
        Opportunity.id, Opportunity.url, Opportunity.status, Opportunity.analysis_state,
    ).where(Opportunity.id.in_([uuid.UUID(i) for i in ids])))).all()
    return {str(rid): (url, status, state) for rid, url, status, state in rows}


async def action_hand_out(session, instance, data, *, now: datetime | None = None):
    """Give the analyst the next roles, or say why not: as many as it can take at once (`settings.analysts`, one by
    default), the next as each is done; best first, but never two on one site (the owner, 2026-10-13: research does not
    fetch one site in parallel, and many of these links are one board's): a role on the same site as one the analyst has
    waits its turn, the next on another site is handed out instead, and when every role left is on such a site none is
    (it does not double up); none while Claude can't be used."""
    entry = await _entry(session, data)
    st = state_of(entry)
    owner = entry.owner
    st["phase"] = "research"
    await _settle_inflight(session, st)  # a role that ended since the last look, whatever the wait said
    held = None
    boards = await employer_posting.job_board_hosts(session, owner)
    facts = await _facts(session, [*st["inflight"], *st["queue"]])
    on = {analysis.site_of(facts[i][0], boards) for i in st["inflight"] if i in facts} - {None}  # sites it is on now
    at = 0
    while at < len(st["queue"]) and len(st["inflight"]) < settings.analysts:
        held = await _held(session, owner, now)
        if held is not None:
            break
        rid = st["queue"][at]
        if rid not in facts or facts[rid][1] not in READ_AT_ONCE or facts[rid][2] == analysis.DONE:
            del st["queue"][at]
            continue  # researched meanwhile, moved out of play, or gone
        site = analysis.site_of(facts[rid][0], boards)
        if site is not None and site in on:
            at += 1
            continue  # the same site as a role the analyst has: not at the same time
        del st["queue"][at]
        found = await _roles(session, [rid], lock=True)
        role = found[0] if found else None
        if role is None or role.status not in READ_AT_ONCE or role.analysis_state == analysis.DONE:
            continue  # researched meanwhile, moved out of play, or gone
        if role.sourced_at is None:
            # not Sourced yet (`postings/sourced.py`): left out of this run, not counted as one that could not be
            # researched; the night tries again
            continue
        if role.analysis_state != analysis.WAITING:
            if role.analysis_state == analysis.FAILED:  # a fresh try: its refusals start again
                role.analysis = {k: v for k, v in (role.analysis or {}).items() if k != "refused"}
            try:
                async with session.begin_nested():
                    await analysis.start(session, owner, role)
            except Exception:  # one role that can't be handed over must not stop the rest
                log.exception("the catch-up couldn't hand role %s to the analyst", rid)
                st["gave_up"] = [*st["gave_up"], rid]
                continue
            st["tries"][rid] = int(st["tries"].get(rid, 0)) + 1
            st["knock"] = [*st["knock"], [rid, engine_calls.ANALYSIS_ASKED]]
        st["inflight"] = [*st["inflight"], rid]
        if site is not None:
            on.add(site)
    st["hold"] = held
    save(entry, st)
    if st["inflight"]:  # the wait is for one of them to be done; a hold shows in the run's line meanwhile
        return {"state": ASKED, "role": st["inflight"][0], "left": len(st["queue"])}
    if held is not None:
        return {"state": HELD, "why": held["why"], "until": held["until"] or "", "left": len(st["queue"])}
    return {"state": NONE_LEFT, "left": 0}


async def action_record(session, instance, data):
    """Note how the research of each role that ended: researched, failed or taken back (`_settle_inflight`)."""
    entry = await _entry(session, data)
    st = state_of(entry)
    outcome = await _settle_inflight(session, st)
    save(entry, st)
    return {"outcome": outcome}


def _looking(employer: Employer) -> bool:
    """Whether an employer of the tracker's roles wants its job system looked for: the app may set it, and it has none
    or only part of a board."""
    mine = employer.ats_found_by != "owner" and "ats" not in (employer.owner_fields or [])  # as resolve.py reads it
    if employer.gated or not mine:
        return False
    return employer.ats is None or not ats.complete(employer.ats, resolve.address(employer.board))


async def _unknown(session: AsyncSession, st: dict) -> list[Employer]:
    ids = [uuid.UUID(i) for i in st["employers"] if i not in set(st["boards"]["looked"])]
    if not ids:
        return []
    rows = (await session.scalars(select(Employer).where(Employer.id.in_(ids)).order_by(Employer.name))).all()
    return [e for e in rows if _looking(e)]


#: The two reads the catch-up still makes inside a transaction (`db.io_held_knowingly`), and why. The import saves
#: nothing unless the catch-up starts, and starting it hands the analyst its first roles, whose first steps read their
#: postings; and the pass's few employers' job systems are looked for inside the catch-up's process step, in the
#: schedule pass's savepoint. Each wants the step run outside the engine's call (`process.run_deferred`), which changes
#: when the import is saved and how a pass recovers.
START_READS = "the catch-up's start hands out its first roles inside the import's transaction"
BOARDS_READS = "the catch-up's job system look-ups run inside its process step, in the schedule pass's savepoint"


async def action_boards(session, instance, data):
    """Look for the job system of some of the tracker's employers that have none, with the app's own detection: from the
    links it holds (the research has added each employer's careers page), the pages those links name, and the open
    feeds. A few employers a pass; nothing here asks Claude."""
    from app.sites import where  # it imports much of the sites package, which reads the job-search tables

    entry = await _entry(session, data)
    st = state_of(entry)
    owner = entry.owner
    st["phase"] = "boards"
    chunk = (await _unknown(session, st))[:BOARDS_A_PASS]
    ids = {e.id for e in chunk}
    await resolve.from_links(session, owner)
    if ids:
        fetch = polite.make()
        try:
            with db.io_held_knowingly(BOARDS_READS):
                await resolve.complete_boards(session, owner, fetch, limit=len(ids), only=ids)
                await resolve.from_pages(session, owner, fetch, limit=len(ids), only=ids)
                await resolve.probe(session, owner, fetch, only=ids)
        finally:
            await fetch.close()
    await where.seed_employer_sites(session, owner)
    st["boards"]["looked"] = [*st["boards"]["looked"], *(str(i) for i in ids)]
    left = len(await _unknown(session, st))
    save(entry, st)
    return {"left": left, "looked": len(st["boards"]["looked"]), "found": await _found(session, st)}


async def _found(session: AsyncSession, st: dict) -> int:
    """How many of the tracker's employers have a job system now that had none when the catch-up began."""
    ids = [uuid.UUID(i) for i in st["employers"] if i not in set(st["boards"]["before"])]
    if not ids:
        return 0
    return int(await session.scalar(select(func.count(Employer.id)).where(
        Employer.id.in_(ids), Employer.ats.is_not(None))) or 0)


async def action_wrap(session, instance, data):
    entry = await _entry(session, data)
    st = state_of(entry)
    st["phase"] = "done"
    st["hold"] = None
    save(entry, st)
    return {"text": await words(session, entry)}


def _register() -> None:
    source = "backend/app/jobsearch/missions/catch_up.py"
    registry.register(
        "jobs.catch_up_pick", action_pick, inputs=["owner", "run"], outputs=["count", "brought", "left_out"],
        description="Picks, of the roles the tracker brought in, the ones still in play that were not researched: "
                    "applications first, then Ready to Launch, then the rest, the youngest posting first and the "
                    "likeliest first among those of an age.", source=source)
    registry.register(
        "jobs.catch_up_match", action_match, inputs=["owner", "run"], outputs=["open", "count"],
        description="Starts the one-off matching of roles to target roles for the picked roles that have none.",
        source=source)
    registry.register(
        "jobs.catch_up_hand_out", action_hand_out, inputs=["owner", "run"],
        outputs=["state", "role", "left", "why", "until"],
        description="Hands the analyst the next role, one at a time, unless Claude can't be used now.", source=source)
    registry.register(
        "jobs.catch_up_record", action_record, inputs=["owner", "run"], outputs=["outcome"],
        description="Notes how the research of the role ended: researched, to be tried again, or taken back.",
        source=source)
    registry.register(
        "jobs.catch_up_boards", action_boards, inputs=["owner", "run"], outputs=["left", "looked", "found"],
        description="Looks for the job system of some of the tracker's employers that have none, from the links and "
                    "pages the app holds and the open feeds.", source=source)
    registry.register(
        "jobs.catch_up_wrap", action_wrap, inputs=["owner", "run"], outputs=["text"],
        description="Writes the run's account.", source=source)


_register()


# --- moving the run on ------------------------------------------------------------------------------------------------


async def _matched(session: AsyncSession, entry: MissionRun) -> bool:
    """Whether none of the picked roles is left for the matching job to hand out."""
    st = state_of(entry)
    job = await tagging.open_job(session, entry.owner)
    if job is None:
        return True
    mine = set(st["roles"])
    return not any(str(r.id) in mine for r in await tagging.waiting(session, job))


async def _researched(session: AsyncSession, entry: MissionRun) -> bool:
    """Whether the analyst has finished with any of the roles the run is waiting on (or has none): the next goes to it
    as each is done."""
    st = state_of(entry)
    if not st["inflight"]:
        return True
    roles = await _roles(session, st["inflight"])
    return len(roles) < len(st["inflight"]) or any(r.analysis_state != analysis.WAITING for r in roles)


async def _ready(session: AsyncSession, entry: MissionRun, message: str, heavy: bool, now: datetime | None) -> bool:
    if message == MATCHED:
        return await _matched(session, entry)
    if message == RESEARCHED:
        return await _researched(session, entry)
    if message == FREE:
        return await _held(session, entry.owner, now) is None
    return message == PASS and heavy


async def _advance_one(session: AsyncSession, entry: MissionRun, heavy: bool, now: datetime | None) -> None:
    st = state_of(entry)
    if not st.get("instance"):
        return
    engine, instance_id = process.engine(), uuid.UUID(st["instance"])
    for _ in range(4):  # one delivery can reach a wait that is already over
        where = await engine.where_is(session, instance_id)
        if where["status"] not in ("running", "waiting"):
            break
        waits = [w for w in where["waiting_for"] if w["kind"] == "catch_message"]
        if not waits or not await _ready(session, entry, waits[0]["message"], heavy, now):
            break
        message = waits[0]["message"]
        try:
            await engine.deliver_message(session, instance_id, message)
        except NotWaiting:
            break
        if message == PASS:  # a pass is a few employers: the next few are the next pass's
            break


async def advance(session: AsyncSession, owner: str, *, heavy: bool = True,
                  now: datetime | None = None) -> list[tuple[str, str]]:
    """Carry every running catch-up on as far as it can go now. `heavy` is the schedule pass: it also lets the network
    work of finding job systems run (a few employers a pass); without it (the runner asking for work) only the cheap
    hand-over goes on, so one role follows another at once, and a run the pass is busy with is left for the pass.
    Each run goes on in a savepoint of its own: one that fails (a record merged away under it) is logged and left as it
    was, still waiting, so the next pass takes it up again; it undoes nothing the other runs or the rest of the pass
    did. Returns who to wake, once the work is committed."""
    knocks: list[tuple[str, str]] = []
    answers = await read_ahead(session, owner, now)
    for entry in await running(session, owner, fresh=True):
        entry_id = entry.id  # a rolled-back savepoint expires the run
        try:
            async with session.begin_nested():
                with polite.read_ahead(answers):  # the hand-outs take the postings read ahead of the step
                    await _advance_one(session, entry, heavy, now)
                await refresh(session, entry)
                st = state_of(entry)
                woken = _take_knocks(st)
                save(entry, st)
        except StaleDataError:
            log.warning("a record the catch-up %s was working on was merged away or deleted under it; it goes on at "
                        "the next pass", entry_id)
            continue
        except Exception:
            log.exception("the catch-up %s couldn't be carried on this pass; it goes on at the next", entry_id)
            continue
        knocks += woken
    await session.flush()
    return knocks


async def hand_on(session: AsyncSession, owner: str) -> None:
    """The cheap part of `advance`, for the runner's question what work waits (`session_kinds.next_kind`): the next role
    goes to the analyst as soon as the last is done. The question must always be answered, so this never raises. It is
    the app's housekeeping (`housekeeping.settle`), which no ask runs."""
    try:
        answers = await read_ahead(session, owner)  # before the savepoint, which cannot be released
        async with session.begin_nested():
            with polite.read_ahead(answers):
                await advance(session, owner, heavy=False)
    except Exception:
        log.exception("the catch-up couldn't be carried on")


async def read_ahead(session: AsyncSession, owner: str, now: datetime | None = None) -> dict:
    """The postings the next hand-outs will read (`action_hand_out` starts each role's analysis, whose first steps
    read its posting, inside the catch-up's own process step, which cannot end its transaction): read now, with no
    transaction open (`analysis.read_ahead`; the session is released first), for `polite.read_ahead`. Only the roles a
    hand-out would give now: as many as the analyst has room for, none while Claude can't be used, never two on one
    site. Empty, with the session left as it is, when there are none or the session cannot be released here."""
    if not db.can_release(session):
        return {}
    boards = await employer_posting.job_board_hosts(session, owner)
    picked: list[str] = []
    for entry in await running(session, owner):
        st = state_of(entry)
        facts = await _facts(session, [*st["inflight"], *st["queue"]])
        still = [i for i in st["inflight"] if i in facts and facts[i][2] == analysis.WAITING]
        room = settings.analysts - len(still)
        if room <= 0 or not st["queue"] or await _held(session, owner, now) is not None:
            continue
        on = {analysis.site_of(facts[i][0], boards) for i in still} - {None}
        for rid in st["queue"]:
            if room <= 0:
                break
            if rid not in facts or facts[rid][1] not in READ_AT_ONCE or facts[rid][2] in (analysis.DONE,
                                                                                         analysis.WAITING):
                continue
            site = analysis.site_of(facts[rid][0], boards)
            if site is not None and site in on:
                continue
            picked.append(rid)
            room -= 1
            if site is not None:
                on.add(site)
    answers: dict = {}
    for role in await _roles(session, picked):
        if role.sourced_at is not None:
            answers.update(await analysis.read_ahead(session, role))
    return answers


# --- Claude's usage limit ---------------------------------------------------------------------------------------------


async def note_usage_limit(session: AsyncSession, owner: str, limit: dict, now: datetime | None = None) -> bool:
    """A session ended at Claude's usage limit (the runner's heartbeat says so, with the time it lifts when Claude gave
    one). Every session that met it, a catch-up's or a lane's, holds the whole pool until it lifts
    (`availability.limit_met`, which `availability.barred` and `limit_until` then read). A running catch-up also
    keeps what it learned in its own run, for the Missions text: the role it has with the analyst is taken back (or
    the sweep would fail it before the limit lifts) and is first in line again. Returns whether a catch-up was
    running."""
    moment = now or utc_now()
    limit = limit if isinstance(limit, dict) else {}
    entries = await running(session, owner, fresh=True)
    until = await availability.limit_met(session, owner, limit, now=moment)
    for entry in entries:
        st = state_of(entry)
        st["limit"] = {"until": until.isoformat(), "seen_at": moment.isoformat(),
                       "text": " ".join(str(limit.get("text") or "").split())[:200]}
        withdrawn = False
        for role in await _roles(session, st["inflight"], lock=True):  # every role it has with the analyst
            # A role the owner asked research of (`analysis.ask`) is not the catch-up's to take back: its claim lapses
            # by itself when the session is gone, and it is handed to another once the limit lifts.
            if await line.leave(session, role, line.SWEEP, why="Claude's usage limit"):
                withdrawn = True
        save(entry, st)
        await session.flush()
        if withdrawn:
            await _advance_one(session, entry, False, now)
        await refresh(session, entry)
    return bool(entries)


# --- what the Missions screen says ------------------------------------------------------------------------------------


async def _researched_count(session: AsyncSession, st: dict) -> int:
    ids = [uuid.UUID(i) for i in st["roles"]]
    if not ids:
        return 0
    return int(await session.scalar(select(func.count(Opportunity.id)).where(
        Opportunity.id.in_(ids), Opportunity.analysis_state == analysis.DONE)) or 0)


def _counted(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


async def words(session: AsyncSession, entry: MissionRun, now: datetime | None = None) -> str:
    """How the catch-up stands, in the Missions screen's words: the roles researched of those picked, what could not be,
    why it is held, and the job systems found."""
    st = state_of(entry)
    todo, done = len(st["roles"]), await _researched_count(session, st)
    lines = []
    if st["phase"] == "matching":
        lines.append(f"Matching {_counted(todo, 'role', 'roles')} to your target roles before the research.")
    elif todo:
        line = f"Researched {done:,} of {_counted(todo, 'role', 'roles')}."
        if st["gave_up"]:
            line += f" {_counted(len(st['gave_up']), 'could not be researched', 'could not be researched')}."
        again = len({*st["queue"]} & {i for i, n in st["tries"].items() if int(n) > 0})
        if again and st["phase"] == "research":
            line += f" {_counted(again, 'is', 'are')} waiting to be tried again."
        lines.append(line)
    else:
        lines.append(f"None of the {st['tracker_count']:,} roles the tracker holds is left to research (each is "
                     "closed, rejected, screened out or already researched).")
    moment = now or utc_now()
    limit_until = _limit_until(st, moment)
    hold = ({"why": availability.Reason.USAGE.value, "until": limit_until.isoformat()}
            if limit_until is not None else st.get("hold"))
    if hold and st["phase"] in ("research", "matching"):
        ends = aware(datetime.fromisoformat(hold["until"])) if hold.get("until") else None
        if ends is None or ends > moment:
            until = f", until {night.when_words(ends, now)}" if ends is not None else ""
            lines.append(f"Paused: {HOLD_WORDS.get(hold['why'], hold['why'])}{until}.")
    if st["phase"] in ("boards", "done") and st["to_look"]:
        found = await _found(session, st)
        line = f"Found the job system of {_counted(found, 'employer', 'employers')} that had none"
        if st["phase"] == "boards":
            looked = len(st["boards"]["looked"])
            line += f" ({looked:,} of {max(st['to_look'], looked):,} employers looked at)"
        lines.append(line + ".")
    return " ".join(lines)


async def refresh(session: AsyncSession, entry: MissionRun) -> None:
    """Bring a running catch-up's run up to date: how far it got, in words, and its end when its process has ended. A
    run whose process ended is closed done (failed when no role could be researched); one whose process failed,
    failed."""
    st = state_of(entry)
    done = await _researched_count(session, st)
    save(entry, st, target=len(st["roles"]), done=done)
    instance = await session.get(ProcInstance, uuid.UUID(st["instance"])) if st.get("instance") else None
    if instance is not None and instance.status == COMPLETED:
        text = await words(session, entry)
        failed = bool(st["roles"]) and done == 0
        _close(entry, "failed" if failed else "done", ("No role could be researched. " if failed else "") + text,
               at=instance.ended_at)
    elif instance is not None and instance.status not in ("running", "waiting"):
        _close(entry, "failed", f"The catch-up stopped: {instance.error or instance.status}.", at=instance.ended_at)
    else:
        entry.summary = await words(session, entry)


def _close(entry: MissionRun, outcome: str, summary: str, at: datetime | None = None) -> None:
    now = utc_now()
    began = aware(entry.started_at)
    when = aware(at) or now
    entry.outcome, entry.summary = outcome, summary
    entry.ended_at = min(max(when, began) if began is not None else when, now)
