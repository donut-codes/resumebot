"""A line drafted from the owner's own words (Find Gaps, "I've done this"; docs/PRD.md 38.5): the owner says in
their own words what they did, and a small request an agent answers while they wait turns it into one entry for the
master resume. The owner reads it in a box they can rewrite freely, chooses the job it belongs to and whether it is
Evidence or a Bullet, and the page saves it through the resume's own save. Nothing here touches the resume.

The agent writes only what the owner said: it adds no claim, number or scope their words do not hold. Code holds the
one part of that it can check, that every number in the draft is in the words (`numbers_added`). A draft is a row of
`js_gap_drafts`, in the shape of the parser's job (`jobs/parse.py`): waiting -> working (a session took it with
`jobs.gaps`) -> done (`jobs.gaps_report` gave the line) or failed, with a plain sentence. A request no session took
within 5 minutes (counted as `job_queue.untaken` says), or not finished 3 minutes after it was taken, fails; the
owner's words are dropped when it ends, and the row after a day. Only offered when an agent is there to take the work
(the same rule as every other request: peak hours too; an agent busy with other work queues it). Services flush; the
route or the tool commits.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.gaps.runs import GapError, NoSuchRun, NotReady
from app.jobsearch.models import GapDraft
from app.jobsearch.owner_text import refuse_over_room
from app.jobsearch.states import GapState
from app.jobsearch.work import job_queue, queue
from app.timeutil import aware

WAITING, WORKING, DONE, FAILED = GapState.WAITING, GapState.WORKING, GapState.DONE, GapState.FAILED
TAKE_WITHIN, KEEP_FOR = job_queue.TAKE_WITHIN, job_queue.KEEP_FOR
FINISH_WITHIN = timedelta(minutes=3)
#: The fewest characters the owner's words may have: with less there is nothing to draft a line from. There is no most:
#: the words go to the agent as written, or are refused past the room an agent can be sent (`owner_text`).
MIN_WORDS = 10
#: The line drafted from them, at most, in characters: what the agent writes.
MAX_DRAFT = 1500
STEPS: dict[str, str] = {
    WAITING: "Waiting for an agent", WORKING: "Drafting the line", DONE: "Done", FAILED: "Stopped"}

NOT_TAKEN = "No agent picked this up within 5 minutes. Try again in a moment, or write the line yourself."
NOT_FINISHED = "The agent didn't finish drafting it within 3 minutes. Try again, or write the line yourself."
UNKNOWN_ID = "no drafted line has that id; send the id jobs.gaps gave you"
NOT_HELD = "you do not hold that request; call jobs.gaps first"
DRAFT_HOW = (
    "The owner says they have done what this theme asks. Write one line for their master resume from their own "
    "words alone: a single plain sentence, in the owner's voice, that begins with a verb. Add no claim, number, "
    "scope or result their words do not hold, and leave out what they said they did not do. Send it with "
    "jobs.gaps_report: {id, draft}."
)

_NUMBER = re.compile(r"\d[\d,.]*")


def numbers_added(words: str, draft: str) -> list[str]:
    """The numbers in the draft that the owner's words do not hold (the one part of "adds no claim" code can check)."""
    def of(text: str) -> set[str]:
        return {n.rstrip(".,").replace(",", "") for n in _NUMBER.findall(text)} - {""}

    return sorted(of(draft) - of(words))


async def late(session: AsyncSession, owner: str, draft: GapDraft, now: datetime) -> str | None:
    """The sentence a request that took too long is failed with, or None while it is in time or has ended. It only
    reads: `expire` fails the request with it (the engine's minute tick), and the page is told it failed with it before
    that pass has run (`told`)."""
    asked = aware(draft.created_at) or now
    last_seen = aware(draft.taken_at or draft.created_at) or now
    if draft.state == WAITING and await job_queue.untaken(session, owner, asked, now):
        return NOT_TAKEN
    if draft.state == WORKING and last_seen < now - FINISH_WITHIN:
        return NOT_FINISHED
    return None


async def expire(session: AsyncSession, owner: str, now: datetime | None = None, *, fail_late: bool = True,
                 drop_old: bool = True) -> None:
    """Fail the requests that took too long and drop the ones kept long enough. Run before any read of the requests. The
    halves can be asked for apart (`job_queue.expire`)."""
    now = now or datetime.now(UTC)
    if drop_old:
        with queue.exempt("requests kept long enough are only tidied: no loop reads them, the screen does"):
            await session.execute(
                delete(GapDraft).where(GapDraft.owner == owner, GapDraft.created_at < now - KEEP_FOR))
    if not fail_late:
        return
    open_drafts = (await session.scalars(
        select(GapDraft).where(GapDraft.owner == owner, GapDraft.state.in_((WAITING, WORKING))))).all()
    for draft in open_drafts:
        why = await late(session, owner, draft, now)
        if why is not None:
            job_queue.fail(draft, FAILED, why, now, words="")
    await session.flush()


async def start(session: AsyncSession, owner: str, run_id: str, theme_id: str, words: str,
                now: datetime | None = None) -> GapDraft:
    """A waiting request to draft a line from the owner's words about one theme of a finished run (the route wakes the
    runner after the commit). The words are kept and handed over whole: they are refused (`TextTooLarge`, 413) only
    when they pass the room an agent can be sent, and never cut. Raises `NotReady` with the reason when no agent can
    take it now."""
    now = now or datetime.now(UTC)
    words = words.strip()
    if len(words) < MIN_WORDS:
        raise GapError(f"Say what you did in your own words: at least {MIN_WORDS} characters.")
    refuse_over_room(words, "What you wrote")
    run = await find_gaps.get(session, owner, run_id, now)
    if run is None:
        raise NoSuchRun(find_gaps.NO_SUCH_RUN)
    theme = next((t for t in run.themes or [] if t["id"] == theme_id), None) if run.state == DONE else None
    if theme is None:
        raise NoSuchRun(find_gaps.NO_SUCH_THEME)
    await expire(session, owner, now)
    ok, why = await job_queue.ready(session, owner, now)
    if not ok:
        raise NotReady(why or "No agent can take this now.")
    draft = GapDraft(owner=owner, run_id=run.id, theme=theme["name"], theme_kind=theme["kind"], words=words,
                     state=WAITING, created_at=now)
    session.add(draft)
    await session.flush()
    return draft


async def get(session: AsyncSession, owner: str, draft_id: str, now: datetime | None = None) -> GapDraft | None:
    """The owner's request with this id (None when there is none, or it is gone), after the ones that took too long
    fail."""
    await expire(session, owner, now)
    try:
        key = uuid.UUID(str(draft_id))
    except ValueError:
        return None
    return await session.scalar(select(GapDraft).where(GapDraft.owner == owner, GapDraft.id == key))


def view(draft: GapDraft) -> dict:
    """What the page reads while it waits: the state, a progress line in plain words, and the line (done) or the error
    (failed)."""
    out: dict = {"id": str(draft.id), "state": draft.state, "step": STEPS[draft.state]}
    if draft.state == DONE:
        out["draft"] = draft.draft
    elif draft.state == FAILED:
        out["error"] = draft.error
    return out


async def told(session: AsyncSession, owner: str, draft_id: str, now: datetime | None = None) -> dict | None:
    """What the page is told of the owner's request with this id (`GET /find-gaps/drafts/{id}`), or None when there is
    none or it is gone. It only reads, so the page asks on a reader: a request kept past its day is gone, and one that
    took too long is told failed in the words `expire` fails it with, whether or not the engine's pass has got to it
    yet. A waiting request says when it is queued."""
    now = now or datetime.now(UTC)
    try:
        key = uuid.UUID(str(draft_id))
    except ValueError:
        return None
    draft = await session.scalar(select(GapDraft).where(GapDraft.owner == owner, GapDraft.id == key))
    if draft is None or job_queue.kept_too_long(draft, now):
        return None
    why = await late(session, owner, draft, now)
    if why is not None:
        return {"id": str(draft.id), "state": FAILED.value, "step": STEPS[FAILED], "error": why}
    return await job_queue.say_queued(session, owner, view(draft), now)


# --- the session's side ------------------------------------------------------------------------------------------


async def holding(session: AsyncSession, owner: str) -> GapDraft | None:
    """The request this session already holds."""
    return await session.scalar(select(GapDraft).where(GapDraft.owner == owner, GapDraft.state == WORKING)
                                .order_by(GapDraft.taken_at).limit(1))


def hand_over(draft: GapDraft) -> dict:
    """What the session is handed for a request."""
    return {"ok": True, "mode": "draft", "id": str(draft.id), "theme": {"name": draft.theme, "kind": draft.theme_kind},
            "words": draft.words, "how": DRAFT_HOW}


async def report(session: AsyncSession, owner: str, draft_id: str, text, now: datetime | None = None) -> dict:
    """Take the line a session drafted: one plain line, with no number the owner's words do not hold, or refuse it
    naming what is wrong."""
    now = now or datetime.now(UTC)
    draft = await get(session, owner, draft_id, now)
    if draft is None:
        raise GapError(UNKNOWN_ID)
    if draft.state in (DONE, FAILED):
        raise GapError(find_gaps.ENDED)
    if draft.state != WORKING:
        raise GapError(NOT_HELD)
    line = " ".join(text.split()) if isinstance(text, str) else ""
    problems = []
    if not line:
        problems.append("draft must be the line, as text")
    elif len(line) > MAX_DRAFT:
        problems.append(f"draft is {len(line)} characters; at most {MAX_DRAFT}: one line")
    extra = numbers_added(draft.words, line)
    if extra:
        problems.append(f"the draft holds {', '.join(extra)}, which the owner's words do not: use only their numbers")
    if problems:
        error = GapError("not taken; fix these and send it again: " + " | ".join(problems))
        error.how_to_fix = "fix those and send jobs.gaps_report again with the same id"
        raise error
    draft.state, draft.draft, draft.words, draft.finished_at = DONE, line, "", now
    await session.flush()
    return {"ok": True, "taken": True,
            "next": "Call jobs.gaps for the next request; when it says none is waiting, stop."}
