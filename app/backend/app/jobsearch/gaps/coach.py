"""The Coach in Find Gaps (docs/PRD.md 38.13 to 38.19): the questions it asks about a run's gaps, the owner's answers,
and the master lines it proposes from them.

A run started from a job (`find_gaps.start_job`) finishes with themes graded Proven, Transferable or Unproven. On it
the owner asks the Coach (a session of the `coach` kind, in its own mode `clarify`) for questions: one or two for each
Transferable or Unproven theme, as cards, except for a theme the owner hid as not relevant, said is not true (kept, by
theme, for the target role or the job: `GapNotTrue`) or that already has a card. The owner answers each in their own
words, skips it, or says it is not true. Then the Coach is asked to propose lines from the answers: master lines in the
owner's own words, each a cut of the answer at its ends and nothing else (`is_cut`), each citing its answer and saying
where it goes (a new line under a job, or evidence on one of that job's existing lines). A proposal already stored,
whatever its status, is not made again (`proposals.py`); the owner accepts, edits or rejects each one there.

Both are a job (`GapCoachJob`) and a run of the process `playbooks/jobsearch/clarify_gaps.bpmn`: waiting (the owner's
request) -> working (a session took it with `jobs.coach`) -> done (`jobs.coach_submit` gave questions or proposals that
passed the checks, and the process filed them) or failed with a plain sentence. A job nobody took in 5 minutes (counted
as `job_queue.untaken` says) or that went 10 minutes with no sign of life fails; so does one whose session ended. Only
offered when an agent is there to take the work (`job_queue.ready`, peak hours included); an agent busy with other work
queues it and says so. One job for a run at a time. Nothing here writes to the master resume, a packet or the Pipeline:
only the owner's Accept does (`proposals.accept`). Services flush; the route or the tool commits.
"""

import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from process_engine.models import COMPLETED
from process_engine.registry import actions as registry
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import process
from app.jobsearch.gaps import proposals
from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.gaps.runs import Busy, GapError, NoSuchRun, NotReady
from app.jobsearch.models import GapCard, GapCoachJob, GapNotTrue, GapRun, Opportunity, Proposal
from app.jobsearch.owner_text import refuse_over_room
from app.jobsearch.postings import text as posting_text
from app.jobsearch.profile import documents
from app.jobsearch.profile import store as profile_store
from app.jobsearch.states import CardState, CoachMode, GapAnswer, GapState, ProposalStatus
from app.jobsearch.work import hand_outs, instruction_files, job_queue, queue, stuck
from app.jobsearch.writing import library as story_library
from app.timeutil import aware

WAITING, WORKING, DONE, FAILED = GapState.WAITING, GapState.WORKING, GapState.DONE, GapState.FAILED
ASK, PROPOSE = CoachMode.ASK, CoachMode.PROPOSE
#: The fewest characters an answer may have (as a drafted line's words, `gap_drafts.MIN_WORDS`): with less there is
#: nothing to propose a line from. There is no most: the words are kept and handed on whole.
MIN_ANSWER = 10
#: The most questions for a theme, how often the Coach's answer may be sent back before the job fails, how long a taken
#: job may go with no sign of life, and the longest a proposal's one line of why may be.
MAX_QUESTIONS, MAX_REFUSALS = 2, 3
FINISH_WITHIN = timedelta(minutes=10)
MAX_WHY = 600
#: The instruction file and the response template the process step names (`playbooks/jobsearch/clarify_gaps.bpmn`).
INSTRUCTIONS, TEMPLATE = "jobsearch.coach.clarify", "jobsearch.coach.clarify"
#: The modes a Coach session is told (`jobs.coach`).
TASK_MODES = {ASK: "clarify_ask", PROPOSE: "clarify_propose"}
STEPS: dict[tuple[str, str], str] = {
    (WAITING, ASK): "Waiting for an agent", (WAITING, PROPOSE): "Waiting for an agent",
    (WORKING, ASK): "The Coach is writing questions", (WORKING, PROPOSE): "The Coach is writing lines",
    (DONE, ASK): "Done", (DONE, PROPOSE): "Done", (FAILED, ASK): "Stopped", (FAILED, PROPOSE): "Stopped"}

NOT_TAKEN = "No agent picked this up within 5 minutes. Try again in a moment."
NOT_FINISHED = "The agent stopped answering for 10 minutes before it finished. Try again."
SESSION_ENDED = "The agent's session ended before the Coach answered. Try again."
GAVE_UP = "The Coach could not give an answer the app could take. Try again."
ALREADY = "The Coach is already working on this run. It can take another job when it has finished."
NOT_A_JOB_RUN = "Only a finished run started from a job can be worked through."
NOTHING_TO_ASK = ("There is nothing left to ask: every Transferable or Unproven theme has its questions, was set aside "
                  "or was marked not true.")
NO_ANSWERS = "Answer at least one question first: there is no answer yet to suggest a line from."
NO_SUCH_CARD = "That question is gone."
NO_SUCH_JOB = "That job is gone."
NOT_HELD = "that job isn't waiting for an answer from you: call jobs.coach (part task) first"
SUGGESTED = "Lines were already suggested from this answer, so it can no longer be changed."

HOW_ASK = (
    "These are the themes of a Find Gaps run that the owner's master resume does not fully show: Transferable and "
    "Unproven ones. Read the role's own posting (part posting) and the master resume (part resume). Write one or two "
    "specific questions for each theme, each asking for what the owner actually did, in their own words, and send them "
    "with jobs.coach_submit: {id, questions: [{theme, question}]}. Use no web tools. Code checks them and names every "
    "problem: fix them and send it all again."
)
HOW_PROPOSE = (
    "These are the owner's answers to the questions you asked. Read the master resume (part resume). From each answer "
    "that holds something true and specific, propose master lines in the owner's own words, minimally trimmed: a line "
    "is the answer cut at its ends and nothing else, never reworded and never joined. Say where each goes: a new line "
    "under a job, or evidence on one of that job's existing lines. Send them with jobs.coach_submit: {id, proposals: "
    "[{card, text, place: {kind, job, line}, why}]}. Use no web tools. Code checks them and names every problem: fix "
    "them and send it all again. Sending none is fine when nothing in the answers is worth adding."
)


# --- the owner's side: jobs ---------------------------------------------------------------------------------------


def _brief(job: GapCoachJob) -> dict:
    out = {"id": str(job.id), "mode": job.mode, "state": job.state, "step": STEPS[(job.state, job.mode)]}
    if job.state == FAILED:
        out["error"] = job.error or ""
    return out


async def fail(session: AsyncSession, job: GapCoachJob, why: str, now: datetime) -> None:
    """The job ends as failed with one plain sentence; its process is cancelled (in its own savepoint)."""
    job.state, job.error, job.finished_at = FAILED.value, why, now
    await stuck.cancel_instance(session, job.instance_id, why, f"the Coach job {job.id}")


async def expire(session: AsyncSession, owner: str, now: datetime | None = None, *, fail_late: bool = True,
                 drop_old: bool = True) -> None:
    """Fail the jobs that took too long and drop the ones that ended long ago. Run before any read of the jobs. The
    halves can be asked for apart (`job_queue.expire`)."""
    now = now or datetime.now(UTC)
    if drop_old:
        with queue.exempt("jobs that ended long ago are only tidied: no loop reads them, the screen does"):
            await session.execute(delete(GapCoachJob).where(
                GapCoachJob.owner == owner, GapCoachJob.state.in_((DONE, FAILED)),
                GapCoachJob.created_at < now - job_queue.KEEP_FOR))
    if not fail_late:
        return
    open_jobs = (await session.scalars(
        select(GapCoachJob).where(GapCoachJob.owner == owner, GapCoachJob.state.in_((WAITING, WORKING))))).all()
    for job in open_jobs:
        asked = aware(job.created_at) or now
        last_seen = aware(job.touched_at or job.taken_at or job.created_at) or now
        if job.state == WAITING and await job_queue.untaken(session, owner, asked, now):
            await fail(session, job, NOT_TAKEN, now)
        elif job.state == WORKING and last_seen < now - FINISH_WITHIN:
            await fail(session, job, NOT_FINISHED, now)
    await session.flush()


async def _run_for(session: AsyncSession, owner: str, run_id: str, now: datetime) -> GapRun:
    """The finished run started from a job that the request is about, after the jobs and runs that took too long
    fail."""
    await find_gaps.expire(session, owner, now)
    await expire(session, owner, now)
    run = await find_gaps.get(session, owner, run_id, now)
    if run is None:
        raise NoSuchRun(find_gaps.NO_SUCH_RUN)
    if run.state != DONE or run.opportunity_id is None:
        raise GapError(NOT_A_JOB_RUN)
    return run


async def _begin(session: AsyncSession, owner: str, run: GapRun, now: datetime) -> None:
    """What a request needs before its own checks: no job going for the run, and an agent that is there to take one
    (`job_queue.ready`; a busy one is no reason to refuse)."""
    going = await session.scalar(select(GapCoachJob.id).where(
        GapCoachJob.run_id == run.id, GapCoachJob.state.in_((WAITING, WORKING))).limit(1))
    if going is not None:
        raise Busy(ALREADY)
    ok, why = await job_queue.ready(session, owner, now)
    if not ok:
        raise NotReady(why or "No agent can take this now.")


async def _launch(session: AsyncSession, owner: str, job: GapCoachJob) -> GapCoachJob:
    session.add(job)
    await session.flush()
    instance = await process.engine().start(
        session, process.CLARIFY_GAPS, {"owner": owner, "job": str(job.id)}, subject=f"jobsearch:clarify:{job.id}")
    job.instance_id = instance.id
    await session.flush()
    return job


async def themes_to_ask(session: AsyncSession, owner: str, run: GapRun) -> list[dict]:
    """The run's Transferable and Unproven themes that need questions: not one the owner hid as not relevant, not one
    they said is not true (for the run's scope), and none that already has a card on the run (docs/PRD.md 38.13)."""
    scope = find_gaps.scope_of(run)
    hidden = {a.key for a in await find_gaps.answers_in(session, owner, scope) if a.answer == GapAnswer.NOT_RELEVANT}
    not_true = set((await session.scalars(select(GapNotTrue.key).where(
        GapNotTrue.owner == owner, GapNotTrue.scope == scope))).all())
    carded = set((await session.scalars(select(GapCard.theme_key).where(GapCard.run_id == run.id))).all())
    return [t for t in run.themes or [] if t["grade"] in ("T", "U") and t["id"] not in hidden | not_true | carded]


async def start_ask(session: AsyncSession, owner: str, run_id: str, now: datetime | None = None) -> GapCoachJob:
    """A waiting job for the Coach to write questions for the run's themes (the route wakes the runner after the
    commit). Raises `GapError` when nothing is left to ask, `Busy` while a job is going for the run and `NotReady` with
    the reason when no agent can take it now."""
    now = now or datetime.now(UTC)
    run = await _run_for(session, owner, run_id, now)
    await _begin(session, owner, run, now)
    themes = await themes_to_ask(session, owner, run)
    if not themes:
        raise GapError(NOTHING_TO_ASK)
    return await _launch(session, owner, GapCoachJob(
        owner=owner, run_id=run.id, mode=ASK.value, state=WAITING.value, items=[t["id"] for t in themes],
        created_at=now))


async def _answered(session: AsyncSession, run: GapRun) -> list[GapCard]:
    """The cards the owner answered that no suggestion has been made from yet, in the run's order."""
    return _in_order(run, list((await session.scalars(select(GapCard).where(
        GapCard.run_id == run.id, GapCard.state == CardState.ANSWERED, GapCard.proposed_at.is_(None)))).all()))


def _in_order(run: GapRun, cards: list[GapCard]) -> list[GapCard]:
    order = {t["id"]: i for i, t in enumerate(run.themes or [])}
    return sorted(cards, key=lambda c: (order.get(c.theme_key, len(order)), c.position))


async def start_propose(session: AsyncSession, owner: str, run_id: str, now: datetime | None = None) -> GapCoachJob:
    """A waiting job for the Coach to propose master lines from the answers no suggestion has been made from yet."""
    now = now or datetime.now(UTC)
    run = await _run_for(session, owner, run_id, now)
    await _begin(session, owner, run, now)
    cards = await _answered(session, run)
    if not cards:
        raise GapError(NO_ANSWERS)
    return await _launch(session, owner, GapCoachJob(
        owner=owner, run_id=run.id, mode=PROPOSE.value, state=WAITING.value, items=[str(c.id) for c in cards],
        created_at=now))


async def answer_card(session: AsyncSession, owner: str, card_id: str, action: str, text: str = "") -> GapCard:
    """What the owner does with a question: `answer` (their own words, at least `MIN_ANSWER` characters, kept whole),
    `skip` (the card is closed; a later run may ask the theme again) or `not_true` (they have not done it: the theme is
    remembered as not true and every card of it on the run closes). An answer can be changed until a line has been
    suggested from it."""
    try:
        key = uuid.UUID(str(card_id))
    except ValueError:
        key = None
    card = await session.scalar(select(GapCard).where(GapCard.owner == owner, GapCard.id == key)) if key else None
    if card is None:
        raise NoSuchRun(NO_SUCH_CARD)
    if card.proposed_at is not None:
        raise GapError(SUGGESTED)
    now = datetime.now(UTC)
    if action == "answer":
        words = (text or "").strip()
        if len(words) < MIN_ANSWER:
            raise GapError(f"Say it in your own words: at least {MIN_ANSWER} characters.")
        refuse_over_room(words, "What you wrote")
        card.state, card.answer, card.answered_at = CardState.ANSWERED.value, words, now
    elif action == "skip":
        card.state, card.answer, card.answered_at = CardState.SKIPPED.value, "", now
    elif action == "not_true":
        card.state, card.answer, card.answered_at = CardState.NOT_TRUE.value, "", now
        if await session.scalar(select(GapNotTrue.id).where(
                GapNotTrue.owner == owner, GapNotTrue.scope == card.scope, GapNotTrue.key == card.theme_key)) is None:
            session.add(GapNotTrue(owner=owner, scope=card.scope, key=card.theme_key, name=card.theme, at=now))
        with queue.exempt("an answered card is read by the screen: no loop reads the cards"):
            await session.execute(update(GapCard).where(
                GapCard.run_id == card.run_id, GapCard.theme_key == card.theme_key, GapCard.proposed_at.is_(None))
                .values(state=CardState.NOT_TRUE.value, answer="", answered_at=now))
    else:
        raise GapError("an answer is one of answer, skip or not_true")
    await session.flush()
    return card


async def clarify_view(session: AsyncSession, owner: str, run: GapRun) -> dict:
    """What the page shows under a job's finished run: the Coach's cards in the run's order, the proposals made from
    them, the Coach's latest job unless it is done (waiting, working or failed, with why), and whether Work through the
    gaps or Suggest lines can be asked now."""
    cards = _in_order(run, list((await session.scalars(select(GapCard).where(
        GapCard.run_id == run.id))).all()))
    job = await session.scalar(select(GapCoachJob).where(GapCoachJob.run_id == run.id)
                               .order_by(GapCoachJob.created_at.desc()).limit(1))
    coach = await job_queue.say_queued(session, owner, _brief(job)) if job is not None and job.state != DONE else None
    going = job is not None and job.state in (WAITING, WORKING)
    resume = (await profile_store.owner_documents(session, owner)).resume
    made = (await session.scalars(select(Proposal).where(Proposal.owner == owner, Proposal.run_id == run.id)
                                  .order_by(Proposal.created_at, Proposal.id))).all()
    return {
        "cards": [{"id": str(c.id), "theme_id": c.theme_key, "theme": c.theme, "grade": c.grade,
                   "question": c.question, "state": c.state, "answer": c.answer, "can_change": c.proposed_at is None}
                  for c in cards],
        "proposals": [proposals.view(p, resume) for p in made],
        "coach": coach,
        "can_ask": not going and bool(await themes_to_ask(session, owner, run)),
        "can_propose": not going and bool(await _answered(session, run)),
    }


# --- the session's side ---------------------------------------------------------------------------------------------


async def get(session: AsyncSession, owner: str, job_id: str | uuid.UUID | None) -> GapCoachJob | None:
    """The owner's Coach job with this id, or None."""
    try:
        key = uuid.UUID(str(job_id))
    except ValueError:
        return None
    return await session.scalar(select(GapCoachJob).where(GapCoachJob.owner == owner, GapCoachJob.id == key))


async def holding(session: AsyncSession, owner: str, job_id: str | uuid.UUID | None) -> GapCoachJob | None:
    """The job a session holds (working), by the id `jobs.coach` gave."""
    job = await get(session, owner, job_id)
    return job if job is not None and job.state == WORKING else None


async def free(session: AsyncSession, owner: str, now: datetime | None = None, *, mine: Collection[uuid.UUID] = (),
               scope: str | None = None) -> list[uuid.UUID]:
    """The jobs no session has taken and none is claimed for (`hand_outs.py`), the oldest first and the ones claimed for
    the caller (`mine`) before them: the owner is waiting at the page, so they go before an interview's prep."""
    query = select(GapCoachJob.id).where(GapCoachJob.owner == owner, GapCoachJob.state == WAITING)
    if skip := await hand_outs.held_uuids(session, owner, hand_outs.COACH):
        query = query.where(GapCoachJob.id.not_in(skip))
    return hand_outs.claimed_first((await session.scalars(query.order_by(GapCoachJob.created_at))).all(), mine)


async def _take(session: AsyncSession, job: GapCoachJob, now: datetime) -> bool:
    """The job a session takes is working from now, and its time to finish counts from here."""
    job.state, job.taken_at, job.touched_at = WORKING.value, now, now
    return True


#: What the ledger needs to take a job for a session (`hand_outs.take_next`); no count: a job fails by its own time.
LINE = hand_outs.Line(free=free, model=GapCoachJob, take=_take, where=lambda moment: (GapCoachJob.state == WAITING,))


def _theme_view(t: dict, texts: dict[str, str], of: int) -> dict:
    return {"id": t["id"], "name": t["name"], "kind": t["kind"], "grade": t["grade"],
            "words": find_gaps.GRADE_WORDS[t["grade"]], "why": t.get("why", ""), "asked": t["asked"], "of": of,
            "lines": [{"id": i, "text": texts.get(i, "(no longer in the master)")} for i in t.get("from") or []]}


async def task(session: AsyncSession, owner: str, job: GapCoachJob) -> dict:
    """What `jobs.coach` hands a session for the job it took: the themes to ask about (mode `clarify_ask`) or the
    owner's answers to propose lines from (`clarify_propose`), with the instructions and the shape of its answer; the
    posting and the master are the parts after it."""
    run = await session.get(GapRun, job.run_id)
    if run is None:
        raise GapError(NO_SUCH_JOB)
    docs = await profile_store.owner_documents(session, owner)
    texts = documents.master_texts(docs.resume)
    reply = {"ok": True, "id": str(job.id), "mode": TASK_MODES[CoachMode(job.mode)], "role": run.title,
             "instructions": await instruction_files.read(session, owner, "instructions", INSTRUCTIONS),
             "response_template": await instruction_files.read(session, owner, "templates", TEMPLATE)}
    if job.mode == ASK:
        wanted = set(job.items)
        reply["themes"] = [_theme_view(t, texts, len(run.postings or [])) for t in run.themes or []
                           if t["id"] in wanted]
        reply.update(how=HOW_ASK, read_next=["posting", "resume"])
    else:
        kinds = {t["id"]: t["kind"] for t in run.themes or []}
        ids = [uuid.UUID(i) for i in job.items]
        cards = _in_order(run, list((await session.scalars(select(GapCard).where(GapCard.id.in_(ids)))).all()))
        reply["answers"] = [{"card": str(c.id), "theme": c.theme, "kind": kinds.get(c.theme_key, "required"),
                             "question": c.question, "answer": c.answer} for c in cards]
        reply.update(how=HOW_PROPOSE, read_next=["resume"])
    return reply


async def part(session: AsyncSession, owner: str, job: GapCoachJob, which: str) -> dict:
    """The parts a task says to read next, for the job the session holds: the posting the run started from (its own
    text, whole) and the master resume with the id of every line."""
    job.touched_at = datetime.now(UTC)
    if which == "resume":
        docs = await profile_store.owner_documents(session, owner)
        return {"ok": True, "id": str(job.id), "master": story_library.master_view(docs)}
    run = await session.get(GapRun, job.run_id)
    role = await session.get(Opportunity, run.opportunity_id) if run is not None and run.opportunity_id else None
    if which == "posting" and role is not None:
        return {"ok": True, "id": str(job.id), "posting": {
            "employer": role.company, "title": role.title, "text": (await posting_text.held(session, role)).text}}
    return {"ok": True, "id": str(job.id), "note": "There is nothing more to read for this job."}


def check_questions(raw, themes: dict[str, str]) -> tuple[dict[str, list[str]], list[str]]:
    """The questions a session sent, by theme, and every problem with them named at once so it can fix them and send
    them all again: each is about one of the themes it was given, is not empty, there are one or two for a theme, and
    every theme has some."""
    if not isinstance(raw, list) or not raw:
        return {}, ["questions must be a list of {theme, question}: one or two for each theme you were given"]
    problems: list[str] = []
    out: dict[str, list[str]] = {}
    for i, q in enumerate(raw):
        where = f"questions[{i}]"
        if not isinstance(q, dict):
            problems.append(f"{where} must be an object {{theme, question}}")
            continue
        theme = q.get("theme")
        words = " ".join(str(q.get("question") or "").split())
        if theme not in themes:
            problems.append(f"{where}.theme {theme!r} is not one of the themes you were given ({', '.join(themes)})")
        elif not words:
            problems.append(f"{where} ({theme}) has no question")
        else:
            out.setdefault(theme, []).append(words)
    for theme, asked in out.items():
        if len(asked) > MAX_QUESTIONS:
            problems.append(f"{theme} has {len(asked)} questions; at most {MAX_QUESTIONS}: keep the sharpest")
    problems += [f"{theme} has no question: write one or two for it" for theme in themes if theme not in out]
    return out, problems


is_cut = proposals.is_cut  # one home, `proposals.py`: the door's suggestions use it too


def check_proposals(raw, cards: dict[str, GapCard], resume: documents.Resume) -> tuple[list[dict], list[str]]:
    """The proposals a session sent, tidied, and every problem with them named at once: each is about an answer it was
    handed, its words are that answer cut at its ends (`is_cut`), and it goes to a job the master has (a new line) or to
    one of that job's existing lines (evidence)."""
    if not isinstance(raw, list):
        return [], ["proposals must be a list of {card, text, place, why}; send an empty list for none"]
    jobs = {r.id: {b.id for b in r.bullets} for r in resume.roles}
    problems: list[str] = []
    out: list[dict] = []
    for i, p in enumerate(raw):
        where = f"proposals[{i}]"
        if not isinstance(p, dict):
            problems.append(f"{where} must be an object {{card, text, place, why}}")
            continue
        card = cards.get(str(p.get("card")))
        if card is None:
            problems.append(f"{where}.card {p.get('card')!r} is not one of the answers you were given")
        words = " ".join(str(p.get("text") or "").split())
        if not words:
            problems.append(f"{where}.text is empty: send the owner's words")
        elif card is not None and not is_cut(words, card.answer):
            problems.append(f"{where}: the words are not the owner's words from answer {card.id}: a proposal is the "
                            "answer cut at its ends and nothing else (no rewording, nothing dropped from the middle)")
        place: dict = p["place"] if isinstance(p.get("place"), dict) else {}
        kind, job, line = place.get("kind"), place.get("job"), place.get("line")
        if kind not in proposals.KINDS:
            problems.append(f"{where}.place.kind {kind!r} must be {' or '.join(proposals.KINDS)}")
        if job not in jobs:
            problems.append(f"{where}.place.job {job!r} is not a job in the master ({', '.join(jobs) or 'none'})")
        elif kind == proposals.EVIDENCE and line not in jobs[job]:
            problems.append(f"{where}.place.line {line!r} is not a line of the job {job}: evidence goes on one of its "
                            f"lines ({', '.join(sorted(jobs[job])) or 'it has none'})")
        why = str(p.get("why") or "").strip()
        if len(why) > MAX_WHY:
            problems.append(f"{where}.why is {len(why)} characters; at most {MAX_WHY}")
        if card is not None and words:
            out.append({"card": card, "text": words, "kind": kind, "job": job,
                        "line": line if kind == proposals.EVIDENCE else None, "why": why[:MAX_WHY]})
    return out, problems


async def _refuse(session: AsyncSession, job: GapCoachJob, problems: list[str], now: datetime) -> dict:
    job.attempts += 1
    job.touched_at = now
    if job.attempts >= MAX_REFUSALS:
        await fail(session, job, GAVE_UP, now)
        await session.flush()
        return {"ok": False, "failed": True, "problems": problems,
                "reason": "The job failed: what you sent was refused three times. Stop here."}
    await session.flush()
    return {"ok": False, "problems": problems,
            "reason": f"Sent back: {len(problems)} problem{'' if len(problems) == 1 else 's'}.",
            "how_to_fix": "Fix each and send it all again with jobs.coach_submit."}


async def submit(session: AsyncSession, owner: str, job: GapCoachJob, body: dict) -> dict:
    """`jobs.coach_submit` for a Find Gaps job: take the questions (`questions`) or the proposals (`proposals`) it
    holds, or send them back with every problem named (the third refusal fails the job). What is taken is kept, and the
    job's process files it."""
    if job.state != WORKING:
        raise GapError(NOT_HELD)
    now = datetime.now(UTC)
    run = await session.get(GapRun, job.run_id)
    if run is None:
        raise GapError(NO_SUCH_JOB)
    if job.mode == ASK:
        by_id = {t["id"]: t for t in run.themes or []}
        asked, problems = check_questions(body.get("questions"), {i: by_id[i]["name"] for i in job.items if i in by_id})
        if problems:
            return await _refuse(session, job, problems, now)
        scope = find_gaps.scope_of(run)
        for theme_id, questions in asked.items():
            for n, question in enumerate(questions):
                session.add(GapCard(owner=owner, run_id=run.id, scope=scope, theme_key=theme_id,
                                    theme=by_id[theme_id]["name"], grade=by_id[theme_id]["grade"], question=question,
                                    position=n, created_at=now))
        result = {"ok": True, "taken": True, "questions": sum(len(q) for q in asked.values()),
                  "note": "Taken. Stop here."}
    else:
        cards = {str(c.id): c for c in (await session.scalars(select(GapCard).where(
            GapCard.id.in_([uuid.UUID(i) for i in job.items])))).all()}
        resume = (await profile_store.owner_documents(session, owner)).resume
        chosen, problems = check_proposals(body.get("proposals"), cards, resume)
        if problems:
            return await _refuse(session, job, problems, now)
        known = set((await session.scalars(select(Proposal.key).where(Proposal.owner == owner))).all())
        made = dropped = 0
        for p in chosen:
            key = proposals.key_of(p["job"], p["kind"], p["line"], p["text"])
            if key in known:
                dropped += 1
                continue
            known.add(key)
            card = p["card"]
            session.add(Proposal(
                owner=owner, run_id=run.id, source=proposals.SOURCE, document="resume", theme=card.theme,
                part={"job_id": p["job"], "kind": p["kind"], "line_id": p["line"]}, after=p["text"], why=p["why"],
                cites={"card": str(card.id), "question": card.question, "answer": card.answer}, key=key,
                status=ProposalStatus.PROPOSED.value, created_at=now))
            made += 1
        for card in cards.values():
            card.proposed_at = now
        job.dropped = dropped
        result = {"ok": True, "taken": True, "proposals": made, "dropped": dropped, "note": "Taken. Stop here."}
    job.touched_at = now
    await session.flush()
    instance = await process.engine().complete(session, job.instance_id, process.CLARIFY_STEP, {"submitted": True})
    if instance.status != COMPLETED:
        raise GapError(instance.error or f"the job's process {instance.status}")
    return result


async def session_ended(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """The Coach's session ended with a Find Gaps job it took still open: it fails, with a sentence for the owner, so
    they can ask again at once. Returns how many."""
    now = now or datetime.now(UTC)
    jobs = (await session.scalars(select(GapCoachJob).where(
        GapCoachJob.owner == owner, GapCoachJob.state == WORKING))).all()
    for job in jobs:
        await fail(session, job, SESSION_ENDED, now)
    await session.flush()
    return len(jobs)


async def working(session: AsyncSession, owner: str) -> GapCoachJob | None:
    """The job a session is working now (what the Agents page says the Coach is at)."""
    return await session.scalar(select(GapCoachJob).where(GapCoachJob.owner == owner, GapCoachJob.state == WORKING)
                                .order_by(GapCoachJob.taken_at).limit(1))


# --- the process's steps (playbooks/jobsearch/clarify_gaps.bpmn) ----------------------------------------------------


async def _job(session: AsyncSession, data: dict) -> GapCoachJob:
    job = await session.get(GapCoachJob, uuid.UUID(data["job"]))
    if job is None:
        raise GapError(NO_SUCH_JOB)
    return job


async def action_gather(session, instance, data):
    job = await _job(session, data)
    return {"ready": job.state in (WAITING, WORKING), "items": len(job.items or [])}


async def action_file(session, instance, data):
    job = await _job(session, data)
    job.state, job.finished_at = DONE.value, datetime.now(UTC)
    await session.flush()
    return {"state": job.state}


registry.register(
    "jobs.clarify_gather", action_gather, inputs=["owner", "job"], outputs=["ready", "items"],
    description="Checks the Coach's job is still open and how many themes or answers it holds.",
    source="backend/app/jobsearch/gaps/coach.py")
registry.register(
    "jobs.clarify_file", action_file, inputs=["owner", "job"], outputs=["state"],
    description="Files the Coach's questions or proposals the checks took and closes the job.",
    source="backend/app/jobsearch/gaps/coach.py")
