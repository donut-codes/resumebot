"""Find Gaps (docs/PRD.md path 38): what the postings for one role ask that the master resume does not yet show.

The analyst grades one posting's required qualifications against the master (`analysis.py`); that says how one
posting fits, not what a kind of job asks for. A run here reads a sample of postings for one role (`gaps/sample.py`: up
to 10 stored postings, one from each employer, or, for a title that is not a target role, about 10 an agent finds on
the public web), groups the asks that repeat into themes, and grades each theme Proven, Transferable or Unproven
against the whole master resume, in the analyst's own words. It writes nothing to the resume, a packet or the
Pipeline: the owner adds a line themselves (`gaps/drafts.py`, then the page's own save of the resume).

A run is a row of `js_gap_runs` and a run of the process `playbooks/jobsearch/find_gaps.bpmn`: waiting (the owner's
request) -> working (a session took it with `jobs.gaps`; `jobs.gaps_progress` says how many postings it has read) ->
done (`jobs.gaps_report` gave its themes, and the process counted, sorted and kept them) or failed, with a plain
sentence. Choosing the sample, the one-from-each-employer rule, counting, sorting, keeping the latest run, hiding what
the owner set aside and the resume's version are code; the grouping, the grades, the drafted line and the web search are
the agent's. Each target role keeps its latest done run; a newer one that finishes replaces it. Nothing here sends a
notification. Only offered when an agent is there to take the work (`job_queue.ready`: the same rule, and the same
sentence, as Import your resume, which includes Claude's peak hours). An agent busy with other work, a role's research
say, does not refuse it: the run is queued, `session_kinds.next_kind` hands it out before the next background session,
and its step says "Queued" while it waits (`job_queue.say_queued`); one run at a time. A run can also start from one
role in the app (`start_job`, docs/PRD.md 38.11: its own posting and similar ones, topped up from the web), and is
worked through with the Coach (`gaps/coach.py`). Services flush; the route or the tool commits.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

from process_engine.models import COMPLETED
from process_engine.registry import actions as registry
from sqlalchemy import and_, case, delete, func, literal_column, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import errors, jsonsql
from app.errors import DomainError
from app.jobsearch import process
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.gaps import sample as gap_sample
from app.jobsearch.models import GapAnswerRow, GapCard, GapRun, Opportunity
from app.jobsearch.postings import text as posting_text
from app.jobsearch.profile import documents
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.states import CardState, GapAnswer, GapKind, GapState
from app.jobsearch.work import job_queue, queue, stuck
from app.timeutil import aware, iso_or_none
from app.web_address import http_url

WAITING, WORKING, DONE, FAILED = GapState.WAITING, GapState.WORKING, GapState.DONE, GapState.FAILED
ROLE, NEW = GapKind.ROLE, GapKind.NEW
NOT_DONE, NOT_RELEVANT = GapAnswer.NOT_DONE, GapAnswer.NOT_RELEVANT
#: What an ask is: a required qualification, a preferred one, or a responsibility.
KINDS = ("required", "preferred", "responsibility")
#: The analyst's classes and their words (docs/PRD.md 26.3): the only scale there is.
GRADES = ("P", "T", "U")
GRADE_WORDS = {"P": "Proven", "T": "Transferable", "U": "Unproven"}
#: How long a run waits for a session to take it (counted as `job_queue.untaken` says), how long it may go with no sign
#: of life once taken, and how long a failed run, or a run for a title that is not a target role, is kept.
TAKE_WITHIN, KEEP_FOR = job_queue.TAKE_WITHIN, job_queue.KEEP_FOR
FINISH_WITHIN = timedelta(minutes=20)
#: What a session is handed at a time of the postings (they are long), and what it may send.
PAGE = 4
MAX_THEMES, MAX_NAME, MAX_WHY, MAX_FROM = 60, 160, 600, 12
MAX_EMPLOYER, MAX_POSTING_TITLE = 200, 300
STEPS: dict[str, str] = {WAITING: "Waiting for an agent", DONE: "Done", FAILED: "Stopped"}

NOT_TAKEN = "No agent picked this up within 5 minutes. Try again in a moment."
NOT_FINISHED = "The agent stopped answering for 20 minutes before it finished. Try again."
ALREADY = "A Find Gaps run is already going. It can take another when it has finished."
NO_SUCH_ROLE = "No target role has that id."
NO_SUCH_JOB = "No role has that id."
NO_TEXT = ("The app holds no text of this role's posting yet, so there is nothing to compare. Open the role to get its "
           "posting first.")
NOT_A_JOB_RUN = "Pick the job again to read a fresh set of postings."
NO_SUCH_RUN = "That run is gone. Run Find gaps again."
NO_SUCH_THEME = "That theme is not in this run."
NOT_READY_RUN = "That run has not finished, so it has no themes to answer yet."
UNKNOWN_ID = "no Find Gaps run has that id; send the id jobs.gaps gave you"
ENDED = job_queue.ENDED
NOT_HELD = "you do not hold that run; call jobs.gaps first"


class GapError(DomainError):
    """A run that cannot be made, found or answered; the message is for the owner or for the session."""

    how_to_fix = "check the arguments against this tool's schema"
    status = 422


class NoSuchRun(GapError):
    status = 404


class Busy(errors.Conflict, GapError):
    """A run is already going."""


class NotReady(errors.NotReady, GapError):
    """No agent can take a run now; `why` is the one sentence for the owner (answered 409 with `detail` and `why`): the
    sentence Import your resume gives."""


def key_of(name: str) -> str:
    """A theme's key: its name folded to letters and digits, so the same words in another case or with other
    punctuation are the same theme."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:100]


def scope_of(run: GapRun) -> str:
    """Where what the owner said of a theme holds: the target role's id, or the job's for a run started from a job whose
    role has no target role, or the title typed for a run of a title that is not one."""
    if run.role_id:
        return run.role_id
    return f"job:{run.opportunity_id}" if run.opportunity_id else f"new:{key_of(run.title)}"


# --- the sweep ---------------------------------------------------------------------------------------------------


def _blank(postings: list[dict]) -> list[dict]:
    """The postings without their text: a run that ended and will not be graded again needs only who and where."""
    return [{k: v for k, v in p.items() if k != "text"} for p in postings or []]


async def fail(session: AsyncSession, run: GapRun, why: str, now: datetime) -> None:
    """The run ends as failed with one plain sentence; its process is cancelled (in its own savepoint)."""
    run.state, run.error, run.finished_at = FAILED, why, now
    run.postings = _blank(run.postings)
    await stuck.cancel_instance(session, run.instance_id, why, f"the Find Gaps run {run.id}")


async def late(session: AsyncSession, owner: str, run: GapRun, now: datetime) -> str | None:
    """The sentence a run that took too long is failed with, or None while it is in time or has ended. It only reads:
    `expire` fails the run with it (the engine's minute tick), and the page is told the run failed with it before that
    pass has run (`told`, `dashboard`)."""
    asked = aware(run.created_at) or now
    last_seen = aware(run.touched_at or run.taken_at or run.created_at) or now
    if run.state == WAITING and await job_queue.untaken(session, owner, asked, now):
        return NOT_TAKEN
    if run.state == WORKING and last_seen < now - FINISH_WITHIN:
        return NOT_FINISHED
    return None


async def expire(session: AsyncSession, owner: str, now: datetime | None = None, *, fail_late: bool = True,
                 drop_old: bool = True) -> None:
    """Fail the runs that took too long and drop the ones kept long enough. Run before any read of the runs. The halves
    can be asked for apart (`job_queue.expire`)."""
    now = now or datetime.now(UTC)
    if drop_old:
        with queue.exempt("runs kept long enough are only tidied: no loop reads them, the screen does"):
            await session.execute(delete(GapRun).where(
                GapRun.owner == owner, GapRun.created_at < now - KEEP_FOR,
                or_(GapRun.state == FAILED, and_(GapRun.kind == NEW, GapRun.opportunity_id.is_(None)))))
    if not fail_late:
        return
    open_runs = (await session.scalars(
        select(GapRun).where(GapRun.owner == owner, GapRun.state.in_((WAITING, WORKING))))).all()
    for run in open_runs:
        why = await late(session, owner, run, now)
        if why is not None:
            await fail(session, run, why, now)
    await session.flush()


# --- the owner's request -----------------------------------------------------------------------------------------


async def _target_role(session: AsyncSession, owner: str, role_id: str):
    profile = await profile_store.current(session, owner)
    for role in profile.criteria.target_roles:
        if role.id == role_id:
            return role
    raise NoSuchRun(NO_SUCH_ROLE)


async def _begin(session: AsyncSession, owner: str, now: datetime) -> None:
    """What every request needs first: no run going (one at a time, queued or working), and an agent that is there to
    take one (`job_queue.ready`; a busy one is no reason to refuse)."""
    await expire(session, owner, now)
    going = await session.scalar(select(GapRun.id).where(
        GapRun.owner == owner, GapRun.state.in_((WAITING, WORKING))).limit(1))
    if going is not None:
        raise Busy(ALREADY)
    ok, why = await job_queue.ready(session, owner, now)
    if not ok:
        raise NotReady(why or "No agent can take this now.")


def _snapshot(chosen: list[gap_sample.Stored]) -> list[dict]:
    return [{"ref": f"p{i}", "employer": s.employer, "title": s.title, "url": s.url, "text": s.text,
             "source": s.source, "source_id": s.source_id} for i, s in enumerate(chosen, start=1)]


def too_few(name: str, employers_with_one: int) -> str:
    who = "employer has" if employers_with_one == 1 else "employers have"
    return (f"Only {employers_with_one} {who} a posting stored for {name}; Find Gaps needs at least "
            f"{gap_sample.MIN_POSTINGS}, each from a different employer.")


async def _launch(session: AsyncSession, owner: str, run: GapRun) -> GapRun:
    session.add(run)
    await session.flush()
    instance = await process.engine().start(
        session, process.FIND_GAPS, {"owner": owner, "run": str(run.id)}, subject=f"jobsearch:gaps:{run.id}")
    run.instance_id = instance.id
    await session.flush()
    return run


async def start_role(session: AsyncSession, owner: str, role_id: str, *, avoid: set[str] | None = None,
                     again: GapRun | None = None, now: datetime | None = None, rng=None) -> GapRun:
    """A run for a target role (the route wakes the runner after the commit): up to 10 stored postings for it, one from
    each employer, drawn now; or, with `again`, the postings of that run read again against the master as it is now.
    Raises `GapError` when the role is not one or has fewer than 5 employers with a stored posting, `Busy` while a
    run is going, and `NotReady` with the reason when no agent can take it now."""
    now = now or datetime.now(UTC)
    role = await _target_role(session, owner, role_id)
    await _begin(session, owner, now)
    if again is not None:
        postings, available = [dict(p) for p in again.postings], again.available
    else:
        found = await gap_sample.stored(session, owner, role_id)
        chosen, available = gap_sample.draw(found, avoid, rng)
        if available < gap_sample.MIN_POSTINGS:
            raise GapError(too_few(role.name or "this role", available))
        postings = _snapshot(chosen)
    run = GapRun(owner=owner, role_id=role_id, title=role.name, kind=ROLE, again=again is not None, state=WAITING,
                 created_at=now, postings=postings, available=available, total=len(postings))
    return await _launch(session, owner, run)


async def start_title(session: AsyncSession, owner: str, title: str, now: datetime | None = None) -> GapRun:
    """A run for a role title that is not a target role: an agent looks for about 10 postings on the public web. A title
    that is one of the owner's target roles is that role's run instead."""
    now = now or datetime.now(UTC)
    title = " ".join((title or "").split())
    if not title:
        raise GapError("Type a role title.")
    profile = await profile_store.current(session, owner)
    for role in profile.criteria.target_roles:
        if role.name.casefold() == title.casefold():
            return await start_role(session, owner, role.id, now=now)
    await _begin(session, owner, now)
    run = GapRun(owner=owner, title=title, kind=NEW, state=WAITING, created_at=now, total=gap_sample.MAX_POSTINGS)
    return await _launch(session, owner, run)


async def start_job(session: AsyncSession, owner: str, opportunity_id: str, now: datetime | None = None,
                    rng=None) -> GapRun:
    """A run started from one role in the app (docs/PRD.md 38.11, 38.12): that role's own posting first and up to 9
    similar ones, one from each employer: the stored postings of its target role, none from its own employer. With fewer
    than 5 postings in all, the agent tops them up from the public web, read-only (a run of kind `new` that already
    holds the ones read). It is kept as the job's latest run and leaves the target role's run on the dashboard as it
    was. Raises `GapError` when the role is not there or holds no posting text, `Busy` while a run is going, and
    `NotReady` with the reason when no agent can take it now."""
    now = now or datetime.now(UTC)
    try:
        key = uuid.UUID(str(opportunity_id))
    except ValueError:
        key = None
    role = await session.scalar(select(Opportunity).where(Opportunity.owner == owner, Opportunity.id == key)
                                ) if key else None
    if role is None:
        raise NoSuchRun(NO_SUCH_JOB)
    text = (await posting_text.held(session, role)).text.strip()
    if not text:
        raise GapError(NO_TEXT)
    await _begin(session, owner, now)
    own = gap_sample.Stored(gap_sample.FROM_ROLE, str(role.id), role.company, role.title, role.url,
                            text[:gap_sample.TEXT_CHARS])
    similar: list[gap_sample.Stored] = []
    if role.target_role:
        similar = [s for s in await gap_sample.stored(session, owner, role.target_role)
                   if s.source_id != own.source_id and s.key != own.key]
    chosen, available = gap_sample.draw(similar, None, rng)
    chosen = chosen[:gap_sample.MAX_POSTINGS - 1]
    postings = _snapshot([own, *chosen])
    if len(postings) >= gap_sample.MIN_POSTINGS:
        run = GapRun(owner=owner, role_id=role.target_role, opportunity_id=role.id, title=role.title, kind=ROLE,
                     state=WAITING, created_at=now, postings=postings, available=available + 1, total=len(postings))
    else:
        for p in postings:
            p["seed"] = True
        run = GapRun(owner=owner, role_id=role.target_role, opportunity_id=role.id, title=role.title, kind=NEW,
                     state=WAITING, created_at=now, postings=postings, total=gap_sample.MAX_POSTINGS)
    return await _launch(session, owner, run)


async def get(session: AsyncSession, owner: str, run_id: str, now: datetime | None = None) -> GapRun | None:
    """The owner's run with this id (None when there is none, or it is gone), after the ones that took too long fail."""
    await expire(session, owner, now)
    try:
        key = uuid.UUID(str(run_id))
    except ValueError:
        return None
    return await session.scalar(select(GapRun).where(GapRun.owner == owner, GapRun.id == key))


async def _done_run(session: AsyncSession, owner: str, run_id: str, now: datetime | None = None) -> GapRun:
    run = await get(session, owner, run_id, now)
    if run is None:
        raise NoSuchRun(NO_SUCH_RUN)
    if run.state != DONE:
        raise GapError(NOT_READY_RUN)
    return run


async def check_again(session: AsyncSession, owner: str, run_id: str, now: datetime | None = None) -> GapRun:
    """The same postings graded again against the master as it is now, with no new search (docs/PRD.md 38.6)."""
    run = await _done_run(session, owner, run_id, now)
    if run.opportunity_id is not None:
        raise GapError(NOT_A_JOB_RUN)
    if run.kind != ROLE or run.role_id is None:
        raise GapError("Only a target role's run can be checked again.")
    return await start_role(session, owner, run.role_id, again=run, now=now)


async def new_sample(session: AsyncSession, owner: str, run_id: str, now: datetime | None = None, rng=None) -> GapRun:
    """A fresh draw of stored postings for the run's role, preferring employers the run did not read (38.6)."""
    run = await _done_run(session, owner, run_id, now)
    if run.opportunity_id is not None:
        raise GapError(NOT_A_JOB_RUN)
    if run.kind != ROLE or run.role_id is None:
        raise GapError("Only a target role's run can take a new sample.")
    return await start_role(session, owner, run.role_id, avoid={p.get("source_id", "") for p in run.postings},
                            now=now, rng=rng)


# --- what the page reads -----------------------------------------------------------------------------------------


def _percents(counts: dict[str, int]) -> dict[str, int] | None:
    """The shares of Proven, Transferable and Unproven as whole percents that add to 100 (the largest remainders take
    the rounding), or None when nothing was graded."""
    total = sum(counts.values())
    if not total:
        return None
    exact = {g: 100 * counts[g] / total for g in GRADES}
    out = {g: int(exact[g]) for g in GRADES}
    by_remainder = sorted(GRADES, key=lambda g: (exact[g] - out[g], -GRADES.index(g)), reverse=True)
    for g in by_remainder[:100 - sum(out.values())]:
        out[g] += 1
    return out


async def researched(session: AsyncSession, owner: str, role_ids: list[str]) -> dict[str, dict]:
    """For each target role: how many researched roles in the Pipeline are matched to it (the analyst has finished) and
    how many of their required items it classed Proven, Transferable and Unproven. Both are counted in the database, so
    the rows that come back are one for each role and one for each role and grade, not one for each researched role."""
    out: dict[str, dict] = {r: {"roles": 0, "counts": {g: 0 for g in GRADES}} for r in role_ids}
    done = (Opportunity.owner == owner, Opportunity.target_role.in_(role_ids or [""]),
            Opportunity.analysis_state == analysis.DONE)
    for role_id, n in (await session.execute(
            select(Opportunity.target_role, func.count()).where(*done).group_by(Opportunity.target_role))).all():
        if role_id in out:
            out[role_id]["roles"] = n
    items = func.json_each(jsonsql.field(Opportunity.analysis, "required")).table_valued("value", "type")
    grade = case((items.c.type == "object", func.json_extract(items.c.value, literal_column("'$.class'"))))
    for role_id, g, n in (await session.execute(
            select(Opportunity.target_role, grade, func.count()).select_from(Opportunity).join(items, true())
            .where(*done, func.json_type(Opportunity.analysis, literal_column("'$.required'")) == "array",
                   grade.in_(GRADES)).group_by(Opportunity.target_role, grade))).all():
        if role_id in out:
            out[role_id]["counts"][g] += n
    return out


def _step(run: GapRun) -> str:
    if run.state == WORKING:
        if run.read:
            return f"{run.read} of {run.total} read"
        return "Looking for postings" if run.kind == NEW else "Starting"
    return STEPS[run.state]


def brief(run: GapRun, why: str | None = None) -> dict:
    """Where a run is, for the dashboard and the page's polling. `why` is the sentence of a run that took too long
    (`late`): the run is told as the failed one the engine's pass makes of it."""
    over = why is not None
    out = {"id": str(run.id), "role_id": None if run.opportunity_id else run.role_id, "title": run.title,
           "kind": run.kind, "state": FAILED.value if over else run.state,
           "step": STEPS[FAILED] if over else _step(run), "read": run.read, "total": run.total}
    if run.opportunity_id is not None:
        out["job_id"] = str(run.opportunity_id)
    if over:
        out["error"] = why
    elif run.state == FAILED:
        out["error"] = run.error
    return out


async def dashboard(session: AsyncSession, owner: str, now: datetime | None = None) -> dict:
    """One row for each target role, in the order of Search Settings' tabs: how many researched roles it rests on, the
    shares of the analyst's grades on their required qualifications, and the role's latest run; and the run going
    now. It only reads, so the page asks on a reader: a run that took too long is told as the failed one, in the words
    `expire` fails it with (`late`), and a failed run kept past its day is left out, whether or not the engine's passes
    have got to them yet."""
    now = now or datetime.now(UTC)
    profile = await profile_store.current(session, owner)
    targets = profile.criteria.target_roles
    counts = await researched(session, owner, [r.id for r in targets])
    latest: dict[str, GapRun] = {}
    for run in (await session.scalars(select(GapRun).where(
            GapRun.owner == owner, GapRun.state == DONE, GapRun.role_id.is_not(None),
            GapRun.opportunity_id.is_(None)).order_by(GapRun.finished_at))).all():
        if run.role_id is not None:
            latest[run.role_id] = run
    going = await session.scalar(select(GapRun).where(
        GapRun.owner == owner, GapRun.state.in_((WAITING, WORKING))).order_by(GapRun.created_at).limit(1))
    failed = await session.scalar(select(GapRun).where(
        GapRun.owner == owner, GapRun.state == FAILED, GapRun.created_at >= now - KEEP_FOR)
        .order_by(GapRun.created_at.desc()).limit(1))
    active, stopped = None, None
    if going is not None:
        why = await late(session, owner, going, now)
        if why is not None:
            stopped = brief(going, why)
        else:
            active = await job_queue.say_queued(session, owner, brief(going), now)
    elif failed is not None:
        stopped = brief(failed)
    return {
        "roles": [{
            "id": r.id, "name": r.name, "researched": counts[r.id]["roles"], "counts": counts[r.id]["counts"],
            "percent": _percents(counts[r.id]["counts"]),
            "last_run": ({"id": str(latest[r.id].id), "at": iso_or_none(latest[r.id].finished_at)}
                         if r.id in latest else None),
        } for r in targets],
        "active": active,
        "failed": stopped,
        "job_runs": await _job_runs(session, owner),
    }


async def picker(session: AsyncSession, owner: str) -> list[dict]:
    """The roles the owner can start a run from (docs/PRD.md 38.11): every role in the app that holds the text of its
    posting, by company and title."""
    rows = (await session.execute(select(Opportunity.id, Opportunity.company, Opportunity.title).where(
        Opportunity.owner == owner, Opportunity.description.is_not(None),
        func.length(func.trim(Opportunity.description)) > 0,
    ).order_by(func.lower(Opportunity.company), func.lower(Opportunity.title), Opportunity.id))).all()
    return [{"id": str(r.id), "company": r.company, "title": r.title} for r in rows]


async def _job_runs(session: AsyncSession, owner: str) -> list[dict]:
    """The finished runs started from a job, newest first, to open again."""
    rows = (await session.execute(
        select(GapRun.id, GapRun.opportunity_id, GapRun.finished_at, Opportunity.company, Opportunity.title)
        .join(Opportunity, Opportunity.id == GapRun.opportunity_id)
        .where(GapRun.owner == owner, GapRun.state == DONE).order_by(GapRun.finished_at.desc()))).all()
    return [{"id": str(r.id), "job_id": str(r.opportunity_id), "company": r.company, "title": r.title,
             "at": iso_or_none(r.finished_at)} for r in rows]


async def answers_in(session: AsyncSession, owner: str, scope: str) -> list[GapAnswerRow]:
    return list((await session.scalars(select(GapAnswerRow).where(
        GapAnswerRow.owner == owner, GapAnswerRow.scope == scope).order_by(GapAnswerRow.name))).all())


def _note(run: GapRun) -> str | None:
    """Said when fewer than 10 employers had a stored posting: how many this run read."""
    if run.kind != ROLE or run.available is None or run.available >= gap_sample.MAX_POSTINGS:
        return None
    return f"Only {run.available} employers have a posting for this role; this reads {len(run.postings)}."


async def _job_of(session: AsyncSession, run: GapRun) -> dict | None:
    """The role a run started from a job is about: its id, company and title."""
    role = await session.get(Opportunity, run.opportunity_id) if run.opportunity_id else None
    return {"id": str(role.id), "company": role.company, "title": role.title} if role is not None else None


async def view(session: AsyncSession, owner: str, run: GapRun, why: str | None = None) -> dict:
    """A run for the page: where it is and, when done, its themes sorted by how many postings ask them (the ones the
    owner set aside as not relevant left out and listed in `hidden`), each with its grade, the master lines behind it
    and what the owner said of it; and whether the master resume has changed since it was graded. A run waiting behind a
    session at work says it is queued, as the other requests do. With `why` (`late`) it is told as failed with it."""
    out = await job_queue.say_queued(session, owner, brief(run, why))
    out.update({"note": _note(run), "checked_at": iso_or_none(run.finished_at) if run.state == DONE else None,
                "instructions_version": run.instructions_version,
                "can_repeat": run.kind == ROLE and run.opportunity_id is None,
                "job": await _job_of(session, run), "clarify": None,
                "postings": [{k: p.get(k) for k in ("ref", "employer", "title", "url")} for p in run.postings or []],
                "themes": [], "hidden": [], "stale": False})
    if run.state != DONE:
        return out
    from app.jobsearch.gaps import coach as gap_coach  # it reads the runs

    if run.opportunity_id is not None:
        out["clarify"] = await gap_coach.clarify_view(session, owner, run)
    current = await profile_store.newest_version(session, owner, "resume")
    out["stale"] = (run.resume_version or 0) < current
    texts = documents.master_texts((await profile_store.owner_documents(session, owner)).resume)
    said = {a.key: a for a in await answers_in(session, owner, scope_of(run))}
    out["hidden"] = [{"id": str(a.id), "name": a.name, "kind": a.kind} for a in said.values()
                     if a.answer == NOT_RELEVANT]
    for t in run.themes or []:
        a = said.get(t["id"])
        if a is not None and a.answer == NOT_RELEVANT:
            continue
        out["themes"].append({
            "id": t["id"], "name": t["name"], "kind": t["kind"], "asked": t["asked"], "of": len(run.postings or []),
            "grade": t["grade"], "words": GRADE_WORDS[t["grade"]], "why": t.get("why", ""),
            "lines": [{"id": i, "text": texts.get(i, "(no longer in the master)")} for i in t.get("from") or []],
            "postings": t["asked_by"], "answer": a.answer if a is not None else None,
            "answer_id": str(a.id) if a is not None else None,
        })
    return out


def _dropped(run: GapRun, now: datetime) -> bool:
    """Whether a run's row is one that `expire` drops: kept past its day, and failed or for a title that is not a
    target role (and not started from a job). A read treats it as gone already."""
    old = (aware(run.created_at) or now) < now - KEEP_FOR
    return old and (run.state == FAILED or (run.kind == NEW and run.opportunity_id is None))


async def told(session: AsyncSession, owner: str, run_id: str, now: datetime | None = None) -> dict | None:
    """What the page is told of the owner's run with this id (`GET /find-gaps/runs/{id}`), or None when there is none
    or it is gone. It only reads, so the page asks on a reader: a run `expire` would drop is gone, and one that took
    too long is told failed in the words `expire` fails it with, whether or not the engine's pass has got to it yet
    (`housekeeping.MINUTE` fails the late runs and cancels their process, `HOUR` drops the old rows)."""
    now = now or datetime.now(UTC)
    try:
        key = uuid.UUID(str(run_id))
    except ValueError:
        return None
    run = await session.scalar(select(GapRun).where(GapRun.owner == owner, GapRun.id == key))
    if run is None or _dropped(run, now):
        return None
    return await view(session, owner, run, await late(session, owner, run, now))


# --- what the owner says of a theme ------------------------------------------------------------------------------


async def answer(session: AsyncSession, owner: str, run_id: str, theme_id: str, said: str) -> GapAnswerRow:
    """"I haven't" (a real gap) or "Not relevant" (hidden for the role on this run and later ones), kept by the theme's
    key within the role. It changes nothing outside Find Gaps: no resume, packet, grade or score."""
    if said not in tuple(GapAnswer):
        raise GapError(f"an answer is one of {', '.join(GapAnswer)}")
    run = await _done_run(session, owner, run_id)
    theme = next((t for t in run.themes or [] if t["id"] == theme_id), None)
    if theme is None:
        raise NoSuchRun(NO_SUCH_THEME)
    scope = scope_of(run)
    row = await session.scalar(select(GapAnswerRow).where(
        GapAnswerRow.owner == owner, GapAnswerRow.scope == scope, GapAnswerRow.key == theme["id"]))
    if row is None:
        row = GapAnswerRow(owner=owner, scope=scope, key=theme["id"], name=theme["name"], kind=theme["kind"],
                           answer=said)
        session.add(row)
    row.answer, row.name, row.kind, row.at = said, theme["name"], theme["kind"], datetime.now(UTC)
    await session.flush()
    return row


async def clear_answer(session: AsyncSession, owner: str, answer_id: str) -> None:
    """Take an answer back: a theme marked a real gap is unmarked, a hidden theme is brought back."""
    try:
        key = uuid.UUID(str(answer_id))
    except ValueError:
        key = None
    row = await session.scalar(select(GapAnswerRow).where(
        GapAnswerRow.owner == owner, GapAnswerRow.id == key)) if key else None
    if row is None:
        raise NoSuchRun("That answer is gone.")
    await session.delete(row)
    await session.flush()


# --- the session's side ------------------------------------------------------------------------------------------


async def held(session: AsyncSession, owner: str, run_id: str) -> GapRun:
    """The run this session was handed (working), by the id `jobs.gaps` gave. A run that has ended is refused, so the
    session stops."""
    run = await get(session, owner, run_id)
    if run is None:
        raise GapError(UNKNOWN_ID)
    if run.state in (DONE, FAILED):
        raise GapError(ENDED)
    if run.state != WORKING:
        raise GapError(NOT_HELD)
    return run


def check_found(raw, seeded: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """The postings an agent found on the public web for a title that is not a target role: each its employer, title and
    public link, one from each employer, 5 to 10 of them, given refs p1... in the order sent. A run started from a job
    already holds some (`seeded`: the role's own and the stored ones, read before the agent starts): the finds come
    after them, with the next refs, none from an employer already there, and the seeded and the found are 5 to 10 in
    all. Returns all of them, the seeded first."""
    seeded = list(seeded or [])
    if not isinstance(raw, list) or not all(isinstance(p, dict) for p in raw):
        return [], ["postings must be a list of {employer, title, url}, one for each employer"]
    problems: list[str] = []
    out: list[dict] = list(seeded)
    seen: set[str] = {employers.key_of(p["employer"]) or p["employer"].lower() for p in seeded}
    for i, p in enumerate(raw[:gap_sample.MAX_POSTINGS * 2]):
        employer = " ".join(str(p.get("employer") or "").split())[:MAX_EMPLOYER]
        title = " ".join(str(p.get("title") or "").split())[:MAX_POSTING_TITLE]
        url = http_url(p.get("url"))
        if not employer or not url or not polite.public_url(url):
            problems.append(f"postings[{i}] needs an employer and the posting's own public address (https://...)")
            continue
        key = employers.key_of(employer) or employer.lower()
        if key in seen:
            problems.append(f"postings[{i}]: {employer} is there twice; send one posting from each employer")
            continue
        seen.add(key)
        out.append({"ref": f"p{len(out) + 1}", "employer": employer, "title": title, "url": url, "text": "",
                    "source": "web", "source_id": ""})
    if len(out) > gap_sample.MAX_POSTINGS:
        problems.append(f"send at most {gap_sample.MAX_POSTINGS} postings")
    if not problems and len(out) < gap_sample.MIN_POSTINGS:
        problems.append(f"send at least {gap_sample.MIN_POSTINGS} postings, each from a different employer; keep "
                        "looking")
    return out, problems


def check_themes(raw, refs: set[str], lines: set[str]) -> tuple[list[dict], list[str]]:
    """The themes a session reported, tidied, and every problem with them named at once so it can fix them and send
    them again. Each: `name`, `kind` (required, preferred or responsibility), `asked_by` (the refs of the postings that
    ask it), `grade` (P, T or U), `from` (the master lines that back a P or T, which must cite some; a U cites none) and
    `why`."""
    if not isinstance(raw, list) or not raw:
        return [], ["themes must be a list of {name, kind, asked_by, grade, from, why}, at least one"]
    problems: list[str] = []
    out: list[dict] = []
    seen: set[str] = set()
    if len(raw) > MAX_THEMES:
        problems.append(f"themes has {len(raw)}; at most {MAX_THEMES}: keep the asks that repeat")
    for i, t in enumerate(raw[:MAX_THEMES]):
        where = f"themes[{i}]"
        if not isinstance(t, dict):
            problems.append(f"{where} must be an object")
            continue
        name = " ".join(str(t.get("name") or "").split())
        key = key_of(name)
        if not key or len(name) > MAX_NAME:
            problems.append(f"{where}.name must be the theme in plain words, at most {MAX_NAME} characters")
            continue
        if key in seen:
            problems.append(f"{where}: two themes are named alike ({name!r}); one theme for each ask")
        seen.add(key)
        if t.get("kind") not in KINDS:
            problems.append(f"{where}.kind must be one of {', '.join(KINDS)}")
        asked = t.get("asked_by")
        if not isinstance(asked, list) or not asked or not all(isinstance(a, str) for a in asked):
            problems.append(f"{where}.asked_by must list the refs of the postings that ask it (p1, p2...)")
            asked = []
        unknown = [a for a in asked if a not in refs]
        if unknown:
            problems.append(f"{where}.asked_by names {', '.join(unknown[:5])}, which no posting has")
        grade = t.get("grade")
        if grade not in GRADES:
            problems.append(f"{where}.grade must be P, T or U")
        cited = t.get("from") or []
        if not isinstance(cited, list) or not all(isinstance(c, str) for c in cited):
            problems.append(f"{where}.from must be a list of master line ids")
            cited = []
        if grade in ("P", "T") and not cited:
            problems.append(f"{where} ({name[:60]!r}) is {grade} but cites no master line: say which (from)")
        if grade == "U" and cited:
            problems.append(f"{where} ({name[:60]!r}) is U but cites master lines: a theme that has a line is P or T")
        missing = [c for c in cited if c not in lines]
        if missing:
            problems.append(f"{where}.from cites {', '.join(missing[:5])}, which the master doesn't have")
        if len(cited) > MAX_FROM:
            problems.append(f"{where}.from cites {len(cited)} lines; at most {MAX_FROM}: the ones that show it best")
        why = str(t.get("why") or "").strip()
        if len(why) > MAX_WHY:
            problems.append(f"{where}.why is {len(why)} characters; at most {MAX_WHY}")
        out.append({"id": key, "name": name, "kind": t.get("kind"), "asked_by": list(dict.fromkeys(asked)),
                    "grade": grade, "from": list(dict.fromkeys(cited)), "why": why[:MAX_WHY]})
    return out, problems


async def progress(session: AsyncSession, owner: str, run_id: str, read: int, now: datetime | None = None) -> dict:
    """The session says how many postings it has read: the page shows "3 of 10 read" while the run works. It only goes
    up, never past the run's total."""
    run = await held(session, owner, run_id)
    if isinstance(read, bool) or not isinstance(read, int):
        raise GapError("read is how many postings you have read, a whole number")
    run.read = max(run.read, min(max(read, 0), run.total))
    run.touched_at = now or datetime.now(UTC)
    await session.flush()
    return {"ok": True, "read": run.read, "of": run.total}


async def report(session: AsyncSession, owner: str, run_id: str, themes, postings=None) -> dict:
    """Take a session's themes for the run it holds, or refuse them with every problem named. A run for a title that is
    not a target role also sends the postings it found. The run's process then counts, sorts and keeps it."""
    run = await held(session, owner, run_id)
    problems: list[str] = []
    if run.kind == NEW:
        found, problems = check_found(postings, [p for p in run.postings or [] if p.get("seed")])
        refs = {p["ref"] for p in found}
    else:
        found, refs = run.postings, {p["ref"] for p in run.postings}
    lines = set(documents.master_texts((await profile_store.owner_documents(session, owner)).resume))
    clean, more = check_themes(themes, refs, lines)
    problems += more
    if problems:
        run.touched_at = datetime.now(UTC)
        await session.flush()
        error = GapError("not taken; fix these and send it all again: " + " | ".join(problems))
        error.how_to_fix = "fix those and send jobs.gaps_report again with the same id"
        raise error
    if run.kind == NEW:
        run.postings, run.total = found, len(found)
    run.themes = clean
    await session.flush()
    instance = await process.engine().complete(session, run.instance_id, process.FINDER_STEP, {"submitted": True})
    if instance.status != COMPLETED:
        raise GapError(instance.error or f"the run's process {instance.status}")
    return {"ok": True, "taken": True, "themes": len(clean)}


# --- the process's service steps ---------------------------------------------------------------------------------


async def _run(session: AsyncSession, data: dict) -> GapRun:
    run = await session.get(GapRun, uuid.UUID(data["run"]))
    if run is None:
        raise GapError("the run is gone")
    return run


async def action_gather(session, instance, data):
    run = await _run(session, data)
    return {"ready": run.state == WAITING, "postings": len(run.postings or [])}


async def action_settle(session, instance, data):
    """Count each theme by the postings that asked it, sort them most first, and keep the run as its role's latest: the
    run before it is dropped."""
    run = await _run(session, data)
    themes = [{**t, "asked": len(t["asked_by"])} for t in run.themes or []]
    run.themes = sorted(themes, key=lambda t: (-t["asked"], t["name"].lower()))
    run.state, run.finished_at, run.read = DONE, datetime.now(UTC), len(run.postings or [])
    run.total = run.read
    if run.kind == NEW:
        run.postings = _blank(run.postings)
    if run.opportunity_id is not None:
        await _replace_job_run(session, run)
    elif run.role_id is not None:
        with queue.exempt("a finished run replaces an earlier one: the old run is removed, none is made ready"):
            await session.execute(delete(GapRun).where(and_(
                GapRun.owner == run.owner, GapRun.role_id == run.role_id, GapRun.state == DONE, GapRun.id != run.id,
                GapRun.opportunity_id.is_(None))))
    await session.flush()
    return {"themes": len(run.themes)}


async def _replace_job_run(session: AsyncSession, run: GapRun) -> None:
    """A run started from a job replaces the job's earlier one (docs/PRD.md 38.11). The cards the owner answered, for
    the themes that are still there, go over to it: their words are their work; the open and skipped ones are asked
    afresh."""
    olds = list((await session.scalars(select(GapRun.id).where(
        GapRun.owner == run.owner, GapRun.opportunity_id == run.opportunity_id, GapRun.state == DONE,
        GapRun.id != run.id))).all())
    if not olds:
        return
    with queue.exempt("a finished run replaces its job's earlier one: its cards move over, the old run is removed"):
        await session.execute(update(GapCard).where(
            GapCard.run_id.in_(olds), GapCard.state == CardState.ANSWERED, GapCard.theme_key.in_(
                [t["id"] for t in run.themes or []])).values(run_id=run.id))
        await session.execute(delete(GapRun).where(GapRun.id.in_(olds)))


registry.register(
    "jobs.gaps_gather", action_gather, inputs=["run"], outputs=["ready", "postings"],
    description="Checks the run is still waiting for the agent and how many stored postings its sample holds.",
    source="backend/app/jobsearch/gaps/runs.py")
registry.register(
    "jobs.gaps_settle", action_settle, inputs=["run"], outputs=["themes"],
    description="Counts each theme by the postings that asked it, sorts them, and keeps the run as the role's latest.",
    source="backend/app/jobsearch/gaps/runs.py")


async def activity(session: AsyncSession, owner: str) -> str:
    """What a Find Gaps session is at, for the Agents page."""
    run = await session.scalar(select(GapRun).where(GapRun.owner == owner, GapRun.state == WORKING)
                               .order_by(GapRun.taken_at).limit(1))
    return f"Finding gaps for {run.title}" if run is not None else "Drafting a line for your resume"
