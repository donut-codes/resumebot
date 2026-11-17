"""Check my roles (docs/PRD.md 31.2): a dry run of the title check that answers "do my role tabs catch what I want?".

The owner asks for one of three checks, and a small Claude session (Haiku, the `checker` kind) judges the items the way
the title pass does, from a title, its employer and the start of its text, against the company and each target role's
name, example titles and description (`profile_views.for_titles`):

- `pursued`: the roles the owner moved forward (`fits_none.pursued_where`), so the answer shows which of them the tabs
  would pass, which they would stop, and which fit no tab;
- `gated`: a sample of the recent postings the title check stopped (never the code gates), so the answer shows which
  of them the tabs would pass now;
- `posting`: one pasted posting, so the answer is the tab it matches, or none, in one line.

It is a dry run: no reader, no scoring, and nothing is stored on a posting or a role. The items it judges are a snapshot
held on the job, dropped when the job ends. The owner's own roles are not handed as examples (the title pass hands them
at night), because the pursued roles are the very ones being asked about.

A check job is a row of `js_check_jobs`, in the shape of the parser's (`jobs/parse.py`; `job_queue.py` holds what they
share): waiting (the owner's request) -> working (a session took it with `jobs.match_next`) -> done (`jobs.match_report`
answered every item) or failed, with a plain sentence. A job no session took within 5 minutes (counted as
`job_queue.untaken` says) fails, as does one a session has not answered a batch of for 5 minutes (checked whenever
anything reads the jobs, so no clock is needed); the result is kept for a day. Only offered when an agent is there to
take the work (`ready`: `job_queue.ready`, which also holds off in Claude's peak hours; an agent busy with other work
queues it). The runner's session is woken by the route that makes the job (`wake.CHECK_ASKED`), and
`session_kinds.next_kind` answers `checker` right after the parser: the owner is at the screen.

The session answers each item `{id, answer, role, why}`: `answer` is what the title pass would say (yes: it could be a
job they want, and when unsure, yes; no: clearly another kind of work or employer), and `role` is the id of the one tab
it fits best, or `none` when it fits no tab at all (for a no, the nearest tab, or none). So an item is **passed** (yes,
with a tab), **stopped** (no) or **fits none** (yes, but no tab claims it: the title check would only let it through by
doubt). Services flush; the route commits.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import errors
from app.errors import DomainError
from app.jobsearch import judged
from app.jobsearch.gating import stopped
from app.jobsearch.jobs import fits_none
from app.jobsearch.models import CheckJob, JobPosting, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.views import for_titles
from app.jobsearch.states import CheckScope, CheckState
from app.jobsearch.work import hand_outs, job_queue, titles
from app.jobsearch.work.claim import claim_one
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, payload_chars

#: What a check judges (`CheckScope`) and the states a check job is in (`CheckState`).
PURSUED, GATED, POSTING = CheckScope.PURSUED, CheckScope.GATED, CheckScope.POSTING
SCOPES = tuple(CheckScope)
WAITING, WORKING, DONE, FAILED = CheckState.WAITING, CheckState.WORKING, CheckState.DONE, CheckState.FAILED
PASSED, STOPPED, FITS_NONE = "passed", "stopped", "fits_none"
#: How long a session may go without answering a batch once it has the job; the rest of the limits are every job's
#: (`job_queue`).
FINISH_WITHIN = timedelta(minutes=5)
#: Items a session is handed at a time, and the most pursued roles one check judges (the newest).
BATCH = 100
MAX_PURSUED = 500
#: The sample of stopped postings a check takes unless the owner asks for another size, its bounds, and how many of the
#: newest stopped postings the sample is spread over (a multiple of its size).
DEFAULT_SAMPLE, MIN_SAMPLE, MAX_SAMPLE = 200, 10, 500
SAMPLE_POOL = 5
#: The fewest characters a pasted posting may have (it has no most, and a session is handed all of it).
MIN_CHARS = job_queue.MIN_CHARS
#: The most items one list of the result holds (the counts are always whole).
MAX_LISTED = 200
#: The progress line for each state, in plain words (a working job says how far along it is, `view`).
STEPS = {WAITING: "Waiting for an agent", WORKING: "Checking", DONE: "Done", FAILED: "Stopped"}

NOT_TAKEN = "No agent picked this up within 5 minutes. Try again in a moment."
NOT_FINISHED = "The agent stopped answering for 5 minutes before it finished. Try again."
NO_ROLES = "Add a role first: the check judges against your target roles."
NOTHING_PURSUED = "None of your roles has moved forward yet, so there is nothing to check."
NOTHING_STOPPED = "No posting has been stopped by the title check yet, so there is nothing to check."
UNKNOWN_ID = "no check job has that id; send the id jobs.match_next gave you"
ENDED = job_queue.ENDED
NOT_HANDED = "that job has not been handed to you; call jobs.match_next first"

CHECK_HOW = (
    "This is a dry run of the title check: nothing you answer changes a posting or a role. Judge each item as the "
    "title pass would, on its title and employer (and the start of its text), against what the owner looks for "
    "(`what_to_look_for`: the kind of employer they want, and each target role with its id, name, example titles and "
    "description). Take the target roles one at a time: an item fits one of them, or none. Answer each item "
    "{id, answer, role, why}: answer yes when it could be a job they want, even if you are not sure, and no only "
    "when it is clearly another kind of work or employer; role the id of the one target role it fits best, or none "
    "when it fits none of them (for a no, the nearest one, or none); why one short line. Send the answers with "
    "jobs.match_report: {id: the job's id, answers}. Then call jobs.match_next again until it says none are left."
)
POSTING_HOW = (
    "This is a dry run of the title check: nothing you answer changes anything. `items` holds one job posting the "
    "owner pasted (its whole text). Judge it as the title pass would, against what the owner looks for "
    "(`what_to_look_for`: the kind of employer they want, and each target role with its id, name, example titles and "
    "description). Take the target roles one at a time: it fits one of them, or none. Answer {id, answer, role, why}: "
    "answer yes when it could be a job they want and no only when it is clearly another kind of work or employer; "
    "role the id of the one target role it fits best, or none when it fits none (for a no, the nearest one, or "
    "none); why one short line. Send it with jobs.match_report: {id: the job's id, answers}. Then call "
    "jobs.match_next again until it says none are left."
)


class CheckError(DomainError):
    """A check that cannot be made, found or answered; the message is for the owner or for the session."""

    how_to_fix = "check the arguments against this tool's schema"
    status = 422


class NotReady(errors.NotReady, CheckError):
    """No agent can take a check now; `why` is the one sentence for the owner (answered 409 with `detail` and `why`)."""


#: Whether an agent can take a check now, and, when not, one plain sentence for why: the same answer the parser gets
#: (`GET /api/v1/agent/ready`).
ready = job_queue.ready


def _fail(job: CheckJob, error: str, now: datetime) -> None:
    job_queue.fail(job, FAILED, error, now, text="", items=[], answers={})


def _last_touched(job: CheckJob) -> datetime | None:
    """When a session last had a hand in the job: its last batch of answers, or when it took the job."""
    return job.touched_at or job.taken_at


async def expire(session: AsyncSession, owner: str, now: datetime | None = None, *, fail_late: bool = True,
                 drop_old: bool = True) -> None:
    """Fail the jobs that took too long and drop the ones kept long enough. Run before any read of the jobs. The halves
    can be asked for apart (`job_queue.expire`)."""
    await job_queue.expire(session, CheckJob, owner, working=WORKING, finish_within=FINISH_WITHIN, not_taken=NOT_TAKEN,
                           not_finished=NOT_FINISHED, failed=_fail, last_touched=_last_touched,
                           now=now, fail_late=fail_late, drop_old=drop_old)


# --- what is checked ----------


def _place(*bits: str | None) -> str:
    return " · ".join(b for b in bits if b)


def role_item(role: Opportunity, text: str | None = None) -> dict:
    """One pursued role as the check keeps it: what it is judged by, and what it is in the app (`stage`, and `was`, the
    target role it was found for, if any)."""
    start = " ".join((text if text is not None else role.description or "").split())[:titles.TEXT_CHARS]
    out = {"ref": str(role.id), "title": role.title, "company": role.company,
           "place": _place(role.location, role.move_type), "text": start, "stage": role.status,
           "was": role.target_role}
    return {k: v for k, v in out.items() if v}


def posting_item(posting: JobPosting) -> dict:
    """One stopped posting as the check keeps it: the title pass's own view of it, and the reason it was stopped."""
    seen = titles.item(posting)
    out = {"ref": str(posting.id), "title": seen.get("title"), "company": seen.get("employer"),
           "place": seen.get("place"), "text": seen.get("text"), "stopped_by": posting.filtered_by}
    return {k: v for k, v in out.items() if v}


def _handed(index: int, item: dict) -> dict:
    """An item as a session is handed it: an id (its position), the title, the employer, the place and the start of the
    text, and nothing else (not what the app already says about it)."""
    out = {"id": str(index), "title": item.get("title"), "employer": item.get("company"), "place": item.get("place"),
           "text": item.get("text")}
    return {k: v for k, v in out.items() if v}


async def pursued_items(session: AsyncSession, owner: str) -> tuple[list[dict], int]:
    """The newest `MAX_PURSUED` roles the owner pursued, and how many there are in all."""
    where = fits_none.pursued_where(owner)
    total = await session.scalar(select(func.count()).select_from(Opportunity).where(*where)) or 0
    rows = (await session.scalars(
        select(Opportunity).where(*where).order_by(Opportunity.created_at.desc(), Opportunity.id)
        .limit(MAX_PURSUED))).all()
    return [role_item(r) for r in rows], total


def _title_stops():
    """The stops the `gated` check samples: the title check's (`JobPosting.judged_by`). Every other stop is a code
    gate, which says nothing about the tabs."""
    return JobPosting.judged_by == judged.TITLE_CHECK


async def gated_items(session: AsyncSession, owner: str, size: int) -> tuple[list[dict], int]:
    """A sample of `size` of the recent postings the title check stopped: spread evenly over the newest `SAMPLE_POOL`
    times as many, so it is not one employer's latest batch. Also how many such stops there are."""
    where = (*stopped._stopped(owner), _title_stops())
    total = await session.scalar(select(func.count()).select_from(JobPosting).where(*where)) or 0
    rows = list((await session.scalars(
        select(JobPosting).where(*where).order_by(JobPosting.first_seen.desc(), JobPosting.id)
        .limit(size * SAMPLE_POOL))).all())
    if len(rows) > size:
        rows = [rows[i * len(rows) // size] for i in range(size)]
    return [posting_item(p) for p in rows], total


# --- the request ----------


async def start(session: AsyncSession, owner: str, scope: str, text: str | None = None, sample: int | None = None,
                now: datetime | None = None) -> tuple[CheckJob, bool]:
    """Make a waiting job for a check (the route wakes the runner after the commit), or find the one already open for
    the same check: `(job, new)`. Raises `CheckError` (in words) when the check cannot be asked for, and `NotReady`
    with the reason when no agent can take it now. The items are taken from the database now and held on the job."""
    now = now or datetime.now(UTC)
    if scope not in SCOPES:
        raise CheckError(f"scope is one of {', '.join(SCOPES)}")
    text = (text or "").strip()
    if scope == POSTING and len(text) < MIN_CHARS:
        raise CheckError(f"paste the whole posting: at least {MIN_CHARS} characters")
    await expire(session, owner, now)
    same = await session.scalar(
        select(CheckJob).where(CheckJob.owner == owner, CheckJob.state.in_((WAITING, WORKING)),
                               CheckJob.scope == scope, CheckJob.text == (text if scope == POSTING else ""))
        .order_by(CheckJob.created_at.desc()).limit(1))
    if same is not None:
        return same, False
    looks = for_titles(await profile_store.current(session, owner))
    if not looks.get("target_roles"):
        raise CheckError(NO_ROLES)
    ok, why = await ready(session, owner, now)
    if not ok:
        raise NotReady(why or "No agent can take this now.")
    size = min(max(int(sample or DEFAULT_SAMPLE), MIN_SAMPLE), MAX_SAMPLE)
    if scope == PURSUED:
        items, total = await pursued_items(session, owner)
        if not items:
            raise CheckError(NOTHING_PURSUED)
    elif scope == GATED:
        items, total = await gated_items(session, owner, size)
        if not items:
            raise CheckError(NOTHING_STOPPED)
    else:
        items, total = [{"ref": "", "text": text}], 1
    job = CheckJob(owner=owner, scope=scope, text=text if scope == POSTING else "", state=WAITING, created_at=now,
                   looks=looks, items=items, meta={"total": total})
    session.add(job)
    await session.flush()
    return job, True


async def get(session: AsyncSession, owner: str, job_id: str, now: datetime | None = None) -> CheckJob | None:
    """The owner's job with this id (None when there is none, or it is gone), after the ones that took too long fail."""
    await expire(session, owner, now)
    try:
        key = uuid.UUID(str(job_id))
    except ValueError:
        return None
    return await session.scalar(select(CheckJob).where(CheckJob.owner == owner, CheckJob.id == key))


def _noun(job: CheckJob) -> str:
    return "postings" if job.scope == GATED else "roles"


def progress(job: CheckJob) -> tuple[int, int]:
    """(answered, how many items) of a job still open."""
    return len(job.answers or {}), len(job.items or [])


def view(job: CheckJob) -> dict:
    """What the page reads while it waits: the state, a progress line in plain words, and the result (done) or the error
    (failed)."""
    out = {"id": str(job.id), "scope": job.scope, "state": job.state, "step": STEPS[job.state]}
    if job.state == WORKING:
        done, total = progress(job)
        out["progress"] = {"done": done, "total": total}
        if job.scope == POSTING:
            out["step"] = "Reading the posting"
        else:
            out["step"] = f"Checked {done} of {total} {_noun(job)}" if done else f"Checking {total} {_noun(job)}"
    elif job.state == DONE:
        out["result"] = job.result
    elif job.state == FAILED:
        out["error"] = job.error
    return out


async def told(session: AsyncSession, owner: str, job_id: str, now: datetime | None = None) -> dict | None:
    """What the page is told of the owner's check with this id (`GET /profile/check/{id}`), or None when there is none
    or it is gone. It only reads, so the page asks on a reader: a job kept past its day is gone, and one that took too
    long is told failed in the words `expire` fails it with, whether or not the engine's pass has got to it yet
    (`housekeeping.MINUTE` fails the late jobs, `HOUR` drops the old rows). A waiting job says when it is queued."""
    now = now or datetime.now(UTC)
    try:
        key = uuid.UUID(str(job_id))
    except ValueError:
        return None
    job = await session.scalar(select(CheckJob).where(CheckJob.owner == owner, CheckJob.id == key))
    if job is None or job_queue.kept_too_long(job, now):
        return None
    why = await job_queue.late(session, owner, job, working=WORKING, finish_within=FINISH_WITHIN,
                               not_taken=NOT_TAKEN, not_finished=NOT_FINISHED, now=now, last_touched=_last_touched)
    if why is not None:
        return {"id": str(job.id), "scope": job.scope, "state": FAILED.value, "step": STEPS[FAILED], "error": why}
    return await job_queue.say_queued(session, owner, view(job), now)


async def waiting(session: AsyncSession, owner: str, now: datetime | None = None) -> CheckJob | None:
    """The oldest job no session has taken and none is claimed for (`hand_outs.py`), after the ones that took too long
    fail (the app's housekeeping does that, never this read: `housekeeping.settle`): what makes `checker` the answer to
    "which work waits"."""
    return await job_queue.oldest_waiting(session, CheckJob, owner,
                                          skip=await hand_outs.held_uuids(session, owner, hand_outs.CHECKER))


# --- the session's side ----------


def _unanswered(job: CheckJob) -> list[tuple[int, dict]]:
    """The items still to answer, each with its id (its position, from 1), in order."""
    given = job.answers or {}
    return [(i, item) for i, item in enumerate(job.items or [], start=1) if str(i) not in given]


def _fit_batch(job: CheckJob, left: list[tuple[int, dict]]) -> list[dict]:
    """Up to `BATCH` of the items left, as handed to a session, as many as the payload budget holds with what the owner
    looks for."""
    base = {"ok": True, "what_to_look_for": job.looks, "items": [], "more": 0, "how": CHECK_HOW}
    batch: list[dict] = []
    for i, item in left[:BATCH]:
        handed = _handed(i, item)
        if batch and payload_chars({**base, "items": [*batch, handed]}) > AGENT_PAYLOAD_BUDGET_CHARS:
            break
        batch.append(handed)
    return batch


async def take_next(session: AsyncSession, owner: str, now: datetime | None = None, *,
                    session_id: str | None = None, claim: str | None = None) -> dict:
    """The next items to judge: those of the job this session already holds, else of the job claimed for it (`claim`:
    `hand_outs.py`), else of the oldest waiting job none is claimed for, which is taken (it is `working` from now, and
    has 5 minutes for each batch). With none left it says so."""
    now = now or datetime.now(UTC)
    await expire(session, owner, now)
    job = await session.scalar(
        select(CheckJob).where(CheckJob.owner == owner, CheckJob.state == WORKING)
        .order_by(CheckJob.taken_at).limit(1))
    # A job the session held is `working` already. One taken now is found and marked by a single statement, so two
    # sessions asking at once are never handed the same job.
    if job is None and claim:  # exactly the job the app claimed for this session, while it still waits
        taken = await hand_outs.take(session, owner, hand_outs.CHECKER, claim, session_id, now) or []
        claimed = [uuid.UUID(i) for i in taken]
        job = await claim_one(session, select(CheckJob).where(
            CheckJob.owner == owner, CheckJob.id.in_(claimed), CheckJob.state == WAITING),
            state=WORKING, taken_at=now, touched_at=now) if claimed else None
    if job is None:
        skip = await hand_outs.held_uuids(session, owner, hand_outs.CHECKER)
        job = await claim_one(session, job_queue.oldest_waiting_query(CheckJob, owner, skip=skip),
                              state=WORKING, taken_at=now, touched_at=now)
    if job is None:
        return {"ok": True, "done": True, "items": [], "more": 0,
                "how": "No check is waiting. Stop: you are finished."}
    left = _unanswered(job)
    batch = _fit_batch(job, left)
    return {"ok": True, "id": str(job.id), "scope": job.scope, "what_to_look_for": job.looks, "items": batch,
            "more": len(left) - len(batch), "how": POSTING_HOW if job.scope == POSTING else CHECK_HOW}


def parse_answer(raw, ids: set[str], role_ids: list[str]) -> tuple[str, dict]:
    """(item id, {answer, role, why}) from one answer a session sent, or raise `titles.AnswerError` saying what is wrong
    with it. A yes must say its `role` (a target role's id, or `none`); a no may name the nearest one."""
    if not isinstance(raw, dict):
        raise titles.AnswerError("each answer is {id, answer, role, why}")
    key = str(raw.get("id") or "").strip()
    if not key:
        raise titles.AnswerError("an answer needs the id it was handed")
    if key not in ids:
        raise titles.AnswerError(f"{key}: not an item of this check, or already answered")
    answer = str(raw.get("answer") or "").strip().lower()
    if answer not in (titles.YES, titles.NO):
        raise titles.AnswerError(f"{key}: answer is yes or no")
    why = " ".join(str(raw.get("why") or "").split())[: titles.WHY_CHARS]
    if not why:
        raise titles.AnswerError(f"{key}: say why in one line")
    if answer == titles.YES and "role" not in raw:
        raise titles.AnswerError(
            f"{key}: a yes names the target role it fits (role: one of {', '.join(role_ids)}) or none")
    role = titles.role_for(key, raw.get("role"), role_ids)
    return key, {"answer": answer, "role": role, "why": why}


async def report(session: AsyncSession, owner: str, job_id: str, answers, now: datetime | None = None) -> dict:
    """Take a session's answers for the job it holds. The ones that can't be taken are named and the rest are kept (a
    batch is large); with every item answered the job is done and its result made. A job that has ended (it took too
    long) is refused, so the session stops. Returns how many were taken, the problems, and whether it is done."""
    now = now or datetime.now(UTC)
    job = await get(session, owner, job_id, now)
    if job is None:
        raise CheckError(UNKNOWN_ID)
    if job.state in (DONE, FAILED):
        raise CheckError(ENDED)
    if job.state != WORKING:
        raise CheckError(NOT_HANDED)
    if not isinstance(answers, list) or not answers:
        raise CheckError("send answers as a list of {id, answer, role, why}, one for each item you were handed")
    role_ids = [r["id"] for r in (job.looks or {}).get("target_roles") or []]
    ids = {str(i) for i, _item in _unanswered(job)}
    kept = dict(job.answers or {})
    problems: list[str] = []
    for raw in answers:
        try:
            key, said = parse_answer(raw, ids - set(kept), role_ids)
        except titles.AnswerError as exc:
            problems.append(str(exc))
            continue
        kept[key] = said
    taken = len(kept) - len(job.answers or {})
    if not taken:
        error = CheckError("nothing was taken: " + "; ".join(problems[:10]))
        error.how_to_fix = "fix those and send jobs.match_report again with the same id"
        raise error
    job.answers, job.touched_at = kept, now
    left = len(job.items or []) - len(kept)
    if left == 0:
        job.result = build_result(job)
        job.state, job.finished_at = DONE, now
        job.text, job.items, job.answers = "", [], {}
    await session.flush()
    return {"ok": True, "taken": taken, "problems": problems[:10], "done": left == 0, "more": left,
            "next": "Call jobs.match_next for the next items; when it says none are left, stop."}


# --- the result ----------


def _row(item: dict, said: dict, names: dict[str, str]) -> dict:
    """One item of a list: what it is, what the app says about it, and the check's tab and line."""
    out = {"id": item.get("ref") or None, "title": item.get("title"), "company": item.get("company"),
           "stage": item.get("stage"), "was": item.get("was"), "stopped_by": item.get("stopped_by"),
           "tab": said["role"], "tab_name": names.get(said["role"] or ""), "why": said["why"]}
    return {k: v for k, v in out.items() if v is not None}


def build_result(job: CheckJob) -> dict:
    """What a check found, from its items and the session's answers (every item answered).

    - `posting`: `{scope, matched, outcome, tab: {id, name} or None, why}`.
    - `pursued` and `gated`: `{scope, checked, of, tabs, passed, stopped, fits_none}`. `checked` items were judged, of
      `of` in all (the newest roles, or a sample of the stops); `tabs` is each tab, in order, with how many items it
      would pass and how many would be stopped (a stop is counted under the nearest tab); each of `passed`, `stopped`
      and `fits_none` is `{count, items}`, the items at most `MAX_LISTED`, in the order they were asked."""
    tabs = [{"id": r["id"], "name": r.get("name") or r["id"]} for r in (job.looks or {}).get("target_roles") or []]
    names = {t["id"]: t["name"] for t in tabs}
    answers = job.answers or {}

    def outcome(said: dict) -> str:
        if said["answer"] == titles.NO:
            return STOPPED
        return PASSED if said["role"] else FITS_NONE

    if job.scope == POSTING:
        said = answers.get("1") or {"answer": titles.NO, "role": None, "why": ""}
        tab = said["role"] if said["answer"] == titles.YES else None
        return {"scope": POSTING, "matched": tab is not None, "outcome": outcome(said),
                "tab": {"id": tab, "name": names.get(tab, tab)} if tab else None, "why": said["why"]}
    rows: dict[str, list[tuple[int, dict]]] = {PASSED: [], STOPPED: [], FITS_NONE: []}
    per_tab = {t["id"]: {"id": t["id"], "name": t["name"], "passed": 0, "stopped": 0} for t in tabs}
    for i, item in enumerate(job.items or [], start=1):
        said = answers[str(i)]
        kind = outcome(said)
        rows[kind].append((i, _row(item, said, names)))
        if said["role"] in per_tab and kind != FITS_NONE:
            per_tab[said["role"]][kind] += 1
    order = {t["id"]: n for n, t in enumerate(tabs)}
    # The ones passed are listed tab by tab, in the tabs' order.
    rows[PASSED].sort(key=lambda r: (order.get(r[1].get("tab"), len(order)), r[0]))
    out = {"scope": job.scope, "checked": len(job.items or []), "of": int((job.meta or {}).get("total") or 0),
           "tabs": list(per_tab.values())}
    for kind in (PASSED, STOPPED, FITS_NONE):
        out[kind] = {"count": len(rows[kind]), "items": [row for _i, row in rows[kind][:MAX_LISTED]]}
    return out
