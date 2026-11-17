"""What the Find Gaps session sees (docs/PRD.md path 38): the tools `jobs.gaps`, `jobs.gaps_progress` and
`jobs.gaps_report`, and which work waits for it. One kind of session (`gaps`, the runner's session of the analyst's role
with its own instruction file) answers both the owner's drafted lines and the runs, a line first: the owner is at a box,
waiting. The runs and the lines themselves are `gaps/runs.py` and `gaps/drafts.py`; this is only the door between them
and the session. Services flush; the tool commits.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.gaps import drafts as gap_drafts
from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.gaps import sample as gap_sample
from app.jobsearch.gaps.runs import GapError
from app.jobsearch.models import GapDraft, GapRun
from app.jobsearch.profile import store as profile_store
from app.jobsearch.states import GapKind, GapState
from app.jobsearch.work import hand_outs, instruction_files, job_queue

WAITING, WORKING, DONE = GapState.WAITING, GapState.WORKING, GapState.DONE
#: The parts of a run's task, one call each (the postings and the master are long).
PARTS = ("task", "postings", "resume")
#: The instruction file and the response template the process step names (`playbooks/jobsearch/find_gaps.bpmn`).
INSTRUCTIONS, TEMPLATE = "jobsearch.gaps", "jobsearch.gaps.report"

NOTHING = "Nothing is waiting for you. Stop here."
HOW = {
    "find": (
        "Read the postings (part postings, a few at a time) and the owner's master resume (part resume). Say how many "
        "postings you have read with jobs.gaps_progress as you go. Then send the themes with jobs.gaps_report: "
        "{id, themes}."
    ),
    "check": (
        "These are the postings the last check read, kept as they were. Read them again (part postings) and grade the "
        "themes against the master resume as it is now (part resume). Where a theme is the same ask as one in "
        "previous_themes, keep its name. Say how many you have read with jobs.gaps_progress. Send jobs.gaps_report: "
        "{id, themes}."
    ),
    "search": (
        "This title is not one of the owner's target roles, so no postings are stored for it. Look for about "
        f"{gap_sample.MAX_POSTINGS} postings for it on the public web, one from each employer, read-only: "
        "WebSearch and WebFetch, never getting round a block. Read the master resume (part resume). "
        "Say how many you have read with jobs.gaps_progress. Send jobs.gaps_report: {id, postings: [{employer, "
        "title, url}], themes}, with each theme's asked_by naming the postings by their place in your list (p1 is "
        "the first)."
    ),
}

HOW_TOP_UP = (
    "This role's own posting and the similar ones the app already holds are {read} (part postings): read them like the "
    "rest. That is fewer than {least} postings, so look for at least {need} more and at most {most} on the public web, "
    "read-only: WebSearch and WebFetch, never getting round a block, one from each employer and none from an employer "
    "already among {read}. Read the master resume (part resume). Say how many you have read with jobs.gaps_progress. "
    "Send jobs.gaps_report: {{id, postings: [{{employer, title, url}}], themes}}, with postings holding only the ones "
    "you found, in the order you read them (the first you found is p{first}), and each theme's asked_by naming "
    "postings by ref, the ones already read included."
)


async def waiting(session: AsyncSession, owner: str, now: datetime | None = None) -> GapDraft | GapRun | None:
    """The oldest thing no session has taken and none is claimed for (`hand_outs.py`; a drafted line before a run),
    after the ones that took too long fail (the app's housekeeping does that, never this read): what makes `gaps` the
    answer to "which work waits"."""
    skip = await hand_outs.held_uuids(session, owner, hand_outs.GAPS)
    draft = await job_queue.oldest_waiting(session, GapDraft, owner, skip=skip)
    return draft if draft is not None else await job_queue.oldest_waiting(session, GapRun, owner, skip=skip)


async def _working_run(session: AsyncSession, owner: str) -> GapRun | None:
    return await session.scalar(select(GapRun).where(GapRun.owner == owner, GapRun.state == WORKING)
                                .order_by(GapRun.taken_at).limit(1))


async def _run_task(session: AsyncSession, owner: str, run: GapRun) -> dict:
    said = await find_gaps.answers_in(session, owner, find_gaps.scope_of(run))
    reply = {
        "ok": True, "id": str(run.id), "role": run.title, "total": run.total,
        "mode": "search" if run.kind == GapKind.NEW else ("check" if run.again else "find"),
        "answered": [{"theme": a.name, "kind": a.kind, "answer": a.answer} for a in said],
        "read_next": ["postings", "resume"] if run.postings else ["resume"],
    }
    seeded = [p for p in run.postings or [] if p.get("seed")]
    if run.kind == GapKind.NEW and seeded:
        n = len(seeded)
        reply["how"] = HOW_TOP_UP.format(
            read="p1" if n == 1 else f"p1 to p{n}", least=gap_sample.MIN_POSTINGS, need=gap_sample.MIN_POSTINGS - n,
            most=gap_sample.MAX_POSTINGS - n, first=n + 1)
    if run.opportunity_id is not None and not run.again:
        before = await session.scalar(select(GapRun).where(
            GapRun.owner == owner, GapRun.opportunity_id == run.opportunity_id, GapRun.state == DONE,
            GapRun.id != run.id).order_by(GapRun.finished_at.desc()).limit(1))
        if before is not None:
            reply["previous_themes"] = [{"name": t["name"], "kind": t["kind"]} for t in before.themes or []]
    if run.again and run.role_id is not None:
        before = await session.scalar(select(GapRun).where(
            GapRun.owner == owner, GapRun.role_id == run.role_id, GapRun.state == DONE)
            .order_by(GapRun.finished_at.desc()).limit(1))
        reply["previous_themes"] = [{"name": t["name"], "kind": t["kind"]} for t in (before.themes if before else [])]
    reply.setdefault("how", HOW[reply["mode"]])
    reply["instructions"] = await instruction_files.read(session, owner, "instructions", INSTRUCTIONS)
    reply["response_template"] = await instruction_files.read(session, owner, "templates", TEMPLATE)
    return reply


async def _take(session: AsyncSession, owner: str, now: datetime, session_id: str | None = None,
                claim: str | None = None) -> dict:
    await find_gaps.expire(session, owner, now)
    await gap_drafts.expire(session, owner, now)
    draft = await gap_drafts.holding(session, owner)
    if draft is not None:
        return gap_drafts.hand_over(draft)
    run = await _working_run(session, owner)
    if run is None:
        # Exactly what the app claimed for this session (`hand_outs.py`), while it still waits, else the oldest that
        # none is claimed for.
        taken = await hand_outs.take(session, owner, hand_outs.GAPS, claim, session_id, now) if claim else None
        claimed = [uuid.UUID(i) for i in taken or []]
        skip = await hand_outs.held_uuids(session, owner, hand_outs.GAPS)
        draft = (await session.scalar(select(GapDraft).where(
            GapDraft.owner == owner, GapDraft.id.in_(claimed), GapDraft.state == WAITING)) if claimed else None
        ) or await job_queue.oldest_waiting(session, GapDraft, owner, skip=skip)
        if draft is not None:
            draft.state, draft.taken_at = WORKING, now
            await session.flush()
            return gap_drafts.hand_over(draft)
        run = (await session.scalar(select(GapRun).where(
            GapRun.owner == owner, GapRun.id.in_(claimed), GapRun.state == WAITING)) if claimed else None
        ) or await job_queue.oldest_waiting(session, GapRun, owner, skip=skip)
        if run is not None:
            run.state, run.taken_at, run.touched_at = WORKING, now, now
            # What the run was graded against and sent, so the page can say when the resume has changed since, and which
            # version of the instructions it used.
            run.resume_version = await profile_store.newest_version(session, owner, "resume")
            run.instructions_version = (await instruction_files.resolve(session, owner, "instructions",
                                                                         INSTRUCTIONS)).version
            await session.flush()
    if run is None:
        return {"ok": True, "done": True, "task": None, "note": NOTHING}
    return await _run_task(session, owner, run)


async def task_reply(session: AsyncSession, owner: str, part: str = "task", page: int = 0,
                     now: datetime | None = None, *, session_id: str | None = None, claim: str | None = None) -> dict:
    """The next piece of work, one part at a time. `task` takes the oldest thing waiting (a drafted line, else a run)
    and gives it; for a run, `postings` gives its stored postings a few at a time (`page` from 0) and `resume` the
    owner's master resume, whole."""
    now = now or datetime.now(UTC)
    if part not in PARTS:
        raise GapError(f"the parts are {', '.join(PARTS)}")
    if part == "task":
        return await _take(session, owner, now, session_id, claim)
    run = await _working_run(session, owner)
    if run is None:
        return {"ok": True,
                "note": "You hold no run. Call jobs.gaps (part task) first; stop if it says none is waiting."}
    if part == "resume":
        docs = await profile_store.owner_documents(session, owner)
        return {"ok": True, "resume": docs.resume.model_dump(mode="json", exclude={"notes"})}
    page = max(int(page or 0), 0)
    here = (run.postings or [])[page * find_gaps.PAGE:(page + 1) * find_gaps.PAGE]
    rest = max(len(run.postings or []) - (page + 1) * find_gaps.PAGE, 0)
    out = {"ok": True, "postings": [{k: p.get(k) for k in ("ref", "employer", "title", "text")} for p in here],
           "page": page, "more": rest}
    if rest:
        out["next_page"] = page + 1
    if not run.postings:
        out["note"] = "This run has no stored postings: look for them on the web, as the task says."
    return out


async def progress_reply(session: AsyncSession, owner: str, run_id: str, read: int) -> dict:
    return await find_gaps.progress(session, owner, run_id, read)


async def report_reply(session: AsyncSession, owner: str, item_id: str, themes=None, postings=None, draft=None) -> dict:
    """The answer to what `jobs.gaps` handed out: a drafted line (`draft`) for a request, or the themes (and, for a
    title searched on the web, the postings) for a run."""
    if await gap_drafts.get(session, owner, item_id) is not None:
        return await gap_drafts.report(session, owner, item_id, draft)
    return await find_gaps.report(session, owner, item_id, themes, postings)


async def session_ended(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """The Find Gaps session ended with work it took still open (it ran out of turns or time): that work fails, with a
    sentence for the owner, so they can ask again at once. Returns how many."""
    now = now or datetime.now(UTC)
    ended = 0
    for run in (await session.scalars(select(GapRun).where(GapRun.owner == owner, GapRun.state == WORKING))).all():
        await find_gaps.fail(session, run, "The agent's session ended before it sent the themes. Try again.", now)
        ended += 1
    drafts = (await session.scalars(
        select(GapDraft).where(GapDraft.owner == owner, GapDraft.state == WORKING))).all()
    for draft in drafts:
        job_queue.fail(draft, GapState.FAILED, "The agent's session ended before it drafted the line. Try again, "
                       "or write the line yourself.", now, words="")
        ended += 1
    await session.flush()
    return ended
