"""The Coach (docs/PRD.md 15.2): interview prep, done by a session of the `coach` kind.

Adding an interview to a role in Active Discussion puts a prep (`InterviewPrep`) in front of the Coach, one process
each (`playbooks/jobsearch/prepare_interview.bpmn`). A session takes it with `jobs.coach` (the task, then its parts:
the posting, the master and the story library, the earlier reports), researches the company and the interviewers on
public pages, and sends the report with `jobs.coach_submit`. Code checks the report when it is sent (`coach_checks.py`)
and sends it back with every problem named; three refusals fail the prep. A report that passes waits for a second
session, which reads it as the interviewer would and sends findings. Code then files it: the findings the owner can act
on join the Open gaps, one line goes to the role's History and one notification tells the owner.

A session that takes a prep and goes quiet holds it for `CLAIM_LEASE`; then another may take it. A prep taken
`hand_outs.MAX_CLAIMS` times and never answered fails, and one nobody took in a day does. The sweep (`settle_stuck`)
closes the rest. An interview whose time passes while its prep waits lapses. The Coach is told nothing of the owner's
contact details or their lowest pay. Services flush; the tool or the route commits.
"""

import copy
import uuid
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from process_engine.registry import actions as registry
from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.inbox import service as inbox
from app.jobsearch import process
from app.jobsearch.gaps import coach as gap_coach
from app.jobsearch.interviews import coach_checks, coach_view
from app.jobsearch.models import Employer, GapCoachJob, Interview, InterviewPrep, Opportunity, Packet
from app.jobsearch.postings import text as posting_text
from app.jobsearch.profile import documents
from app.jobsearch.profile import store as profile_store
from app.jobsearch.states import PacketState, PrepState
from app.jobsearch.work import hand_outs, instruction_files, stuck
from app.jobsearch.writing import library as story_library
from app.timeutil import aware, owner_today, owner_zone, utc_now
from app.trail import service as trail

BY = "Coach"
#: How long a session keeps the prep it took before another may take it (how many times it may be taken and never
#: answered is `hand_outs.MAX_CLAIMS`), how many times the checks may refuse a report, and how long a prep no session
#: took may wait.
CLAIM_LEASE = timedelta(minutes=20)
MAX_REFUSALS = 3
UNCLAIMED_FUSE = timedelta(hours=24)
PARTS = ("task", "posting", "resume", "earlier")
NOTHING = "No interview prep is waiting. Stop here."
WRITE_FILES = ("jobsearch.coach", "jobsearch.coach.report")
REVIEW_FILES = ("jobsearch.coach.review", "jobsearch.coach.review")
#: The employer facts the Coach reads.
EMPLOYER_FACTS = ("website", "industry", "sectors", "employees", "ownership", "headquarters", "offices", "founded",
                  "ai_native", "events", "notes")
#: What of the analyst's research the Coach may read. The starting expectation is handed as four keys only
#: (`_expectation`): never the floor, the range, the points or what they were worked from.
RESEARCH_KEYS = ("notes", "summary", "employer", "sources")
ROUND_PHRASE = {"recruiter_screen": "Recruiter screen", "hiring_manager": "Hiring manager round",
                "panel": "Panel round", "executive": "Executive round", "other": "interview"}

HOW_WRITE = (
    "Read the posting (part posting), the master resume and the story library (part resume) and the earlier reports "
    "for this employer (part earlier). Research the company and each named interviewer on public pages. Then send the "
    "report with jobs.coach_submit: {id, report}, in the response shape. Code checks it and names every problem: fix "
    "them and send it all again."
)
HOW_REVIEW = (
    "Read the report in this reply as the interviewer would, with the posting (part posting) and the master (part "
    "resume) beside it. Use no web tools. Send jobs.coach_submit: {id, review: {summary, findings}}."
)


class CoachError(DomainError):
    status = 422


class PrepConflict(CoachError):
    status = 409


# --- starting, lapsing, ending -----------------------------------------------------------------------------------


async def start(session: AsyncSession, owner: str, interview: Interview) -> InterviewPrep:
    """The next version of the prep for an interview. One version is open at a time, and an interview whose time has
    passed is refused. With no master resume the version fails at once and says so."""
    now = utc_now()
    if (aware(interview.when_at) or interview.when_at) <= now:
        raise PrepConflict("The time has passed, so no report is made.")
    if await session.scalar(select(InterviewPrep.id).where(
            InterviewPrep.interview_id == interview.id, InterviewPrep.state.in_(coach_view.OPEN))):
        raise PrepConflict("A report for this interview is being made already.")
    latest = await session.scalar(select(InterviewPrep.version).where(InterviewPrep.interview_id == interview.id)
                                  .order_by(InterviewPrep.version.desc()).limit(1))
    prep = InterviewPrep(owner=owner, interview_id=interview.id, version=(latest or 0) + 1,
                         snapshot=coach_view.snapshot_of(interview), asked_at=now)
    docs = await profile_store.owner_documents(session, owner)
    if not documents.master_texts(docs.resume):
        prep.state, prep.finished_at = PrepState.FAILED.value, now
        prep.error = "There is no master resume to prepare from yet: add it under My Resume."
        session.add(prep)
        await session.flush()
        return prep
    session.add(prep)
    await session.flush()
    instance = await process.engine().start(
        session, process.PREPARE_INTERVIEW, {"owner": owner, "prep": str(prep.id)},
        subject=f"jobsearch:interview:{prep.id}")
    prep.instance_id = instance.id
    await session.flush()
    return prep


async def lapse(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """A prep still waiting for a Coach when its interview's time passes is never made: it lapses."""
    moment = now or utc_now()
    rows = (await session.scalars(select(InterviewPrep).join(Interview, Interview.id == InterviewPrep.interview_id)
                                  .where(InterviewPrep.owner == owner, InterviewPrep.state == PrepState.WAITING,
                                         Interview.when_at <= moment))).all()
    for prep in rows:
        await stuck.cancel_instance(session, prep.instance_id, "the interview's time passed",
                                    f"the prep {prep.id}")
        prep.state, prep.finished_at = PrepState.LAPSED.value, moment
    await session.flush()
    return len(rows)


async def _pair(session: AsyncSession, prep: InterviewPrep) -> tuple[Interview, Opportunity]:
    """The interview a prep is for, and its role."""
    interview = await session.get(Interview, prep.interview_id)
    role = await session.get(Opportunity, interview.role_id) if interview is not None else None
    if interview is None or role is None:
        raise CoachError("the interview this prep is for isn't there")
    return interview, role


async def _context(session: AsyncSession, prep: InterviewPrep, interview: Interview) -> coach_checks.Context:
    docs = await profile_store.owner_documents(session, prep.owner)
    return coach_checks.Context.make(
        resume=docs.resume, writing=docs.writing, stories=await _stories(session, prep.owner),
        people=list(interview.people or []), round_kind=interview.round_kind, today=owner_today(),
        has_interview_rules=bool(docs.writing.interview_rules))


async def _stories(session: AsyncSession, owner: str) -> dict[str, str]:
    row = await story_library.newest(session, owner)
    return {s["id"]: s.get("title", "") for s in (row.data or {}).get("stories") or []} if row is not None else {}


async def texts_for(session: AsyncSession, owner: str) -> dict[str, str]:
    """The words of everything a report may cite: master lines by id, and library stories by id (their title)."""
    docs = await profile_store.owner_documents(session, owner)
    return {**documents.master_texts(docs.resume), **await _stories(session, owner)}


async def _fail(session: AsyncSession, prep: InterviewPrep, why: str) -> None:
    """The version ends as failed with one plain sentence; the owner is told, and its process is closed."""
    prep.state, prep.error, prep.finished_at = PrepState.FAILED.value, why, utc_now()
    await stuck.cancel_instance(session, prep.instance_id, why, f"the prep {prep.id}")
    interview = await session.get(Interview, prep.interview_id)
    role = await session.get(Opportunity, interview.role_id) if interview is not None else None
    if role is not None and interview is not None:
        phrase = ROUND_PHRASE.get(interview.round_kind, "interview")
        await inbox.notify(session, prep.owner, key=f"interview_prep_failed:{prep.id}", kind="interview_prep_failed",
                           title=f"The prep for the {phrase} at {role.company} wasn't made.", body=why,
                           level="attention", link=("opportunity", role.id))
    await session.flush()


# --- the line a session takes from -------------------------------------------------------------------------------


def _free_write(moment: datetime):
    return or_(InterviewPrep.state == PrepState.WAITING,
               and_(InterviewPrep.state == PrepState.WRITING, InterviewPrep.taken_at < moment - CLAIM_LEASE))


def _free_review(moment: datetime):
    return or_(InterviewPrep.state == PrepState.REVIEW_WAITING,
               and_(InterviewPrep.state == PrepState.REVIEWING, InterviewPrep.review_taken_at < moment - CLAIM_LEASE))


def _free(moment: datetime):
    return or_(_free_write(moment), _free_review(moment))


async def _preps(session: AsyncSession, owner: str, moment: datetime, skip: Collection[uuid.UUID],
                 mine: Collection[uuid.UUID]) -> list[uuid.UUID]:
    """The preps a session could take: the one nearest done first (a read before a write), then the oldest, with the
    caller's own claim (`mine`) before them, leaving out the ones claimed for another session (`skip`)."""
    query = select(InterviewPrep.id).where(InterviewPrep.owner == owner, _free(moment))
    if skip:
        query = query.where(InterviewPrep.id.not_in(skip))
    reading = case((InterviewPrep.state.in_((PrepState.REVIEW_WAITING, PrepState.REVIEWING)), 0), else_=1)
    return hand_outs.claimed_first((await session.scalars(query.order_by(reading, InterviewPrep.asked_at))).all(), mine)


async def _prep_ids(session: AsyncSession, owner: str, now: datetime | None = None, *,
                    mine: Collection[uuid.UUID] = (), scope: str | None = None) -> list[uuid.UUID]:
    skip = await hand_outs.held_uuids(session, owner, hand_outs.COACH)
    return await _preps(session, owner, now or utc_now(), skip, mine)


async def free(session: AsyncSession, owner: str, now: datetime | None = None, *, mine: Collection[uuid.UUID] = (),
               scope: str | None = None) -> list[uuid.UUID]:
    """What a session could take now, in the order it would be handed: the preps (`_preps`: an interview has a time, so
    they go first), else the Find Gaps jobs the owner is waiting at the page for (`gaps/coach.py`), leaving out what is
    held and what is claimed for another session (`hand_outs.py`). What makes `coach` the answer to "which work waits"
    (`session_kinds.next_kind`), what a claim holds, and what `_claim_next` takes from."""
    return await _prep_ids(session, owner, now, mine=mine) or await gap_coach.free(session, owner, now, mine=mine)


async def waiting(session: AsyncSession, owner: str) -> InterviewPrep | None:
    """The oldest open prep (what `session_kinds.doing` names)."""
    return await session.scalar(select(InterviewPrep).where(
        InterviewPrep.owner == owner, InterviewPrep.state.in_(coach_view.OPEN))
        .order_by(InterviewPrep.asked_at).limit(1))


async def activity(session: AsyncSession, owner: str) -> str:
    if await gap_coach.working(session, owner) is not None:
        return "Working through your gaps"
    prep = await waiting(session, owner)
    interview = await session.get(Interview, prep.interview_id) if prep is not None else None
    role = await session.get(Opportunity, interview.role_id) if interview is not None else None
    return f"Preparing for an interview at {role.company}" if role is not None else "Preparing for an interview"


def _reviewing(prep: InterviewPrep) -> bool:
    return prep.state in (PrepState.REVIEW_WAITING, PrepState.REVIEWING)


def _times_taken(prep: InterviewPrep) -> int:
    return prep.review_claims if _reviewing(prep) else prep.claims


async def _drop(session: AsyncSession, prep: InterviewPrep) -> None:
    if _reviewing(prep):
        await _file(session, prep)  # a read nobody finished: the report stands without it
    else:
        await _fail(session, prep, "No session managed to send the report.")


async def _take(session: AsyncSession, prep: InterviewPrep, moment: datetime) -> bool:
    if _reviewing(prep):
        prep.state, prep.review_taken_at, prep.review_claims = PrepState.REVIEWING.value, moment, prep.review_claims + 1
    else:
        prep.state, prep.taken_at, prep.claims = PrepState.WRITING.value, moment, prep.claims + 1
    return True


#: What the ledger needs to take a prep for a session (`hand_outs.take_next`), and, after it, a Find Gaps job.
_PREPS = hand_outs.Line(free=_prep_ids, model=InterviewPrep, take=_take, where=lambda moment: (_free(moment),),
                        taken=_times_taken, give_up=_drop)
_LINES: list[hand_outs.Line[Any]] = [_PREPS, gap_coach.LINE]


async def _claim_next(session: AsyncSession, owner: str, moment: datetime, session_id: str | None,
                      claim: str | None) -> tuple[InterviewPrep | GapCoachJob | None, str]:
    """The prep (or, when none waits, the Find Gaps job) this session takes (`hand_outs.take_next`), and its mode: the
    one claimed for it while it is free, else the first free."""
    taken = await hand_outs.take_next(session, owner, hand_outs.COACH, _LINES, now=moment,
                                      session_id=session_id, claim=claim)
    if not taken:
        return None, "write"
    row = taken[0]
    if isinstance(row, GapCoachJob):
        return row, gap_coach.TASK_MODES[gap_coach.CoachMode(row.mode)]
    return row, "review" if row.state == PrepState.REVIEWING else "write"


# --- what the Coach is handed ------------------------------------------------------------------------------------


def _when(interview: Interview) -> dict:
    local = (aware(interview.when_at) or interview.when_at).astimezone(owner_zone())
    hour = f"{local:%I:%M %p}".lstrip("0")
    return {"when_at": (aware(interview.when_at) or interview.when_at).isoformat(),
            "when": f"{local:%A, %B} {local.day}, {hour} {local.tzname()}"}


def _interview_part(interview: Interview) -> dict:
    return {**_when(interview), "round": coach_view.round_label(interview.round_kind),
            "round_kind": interview.round_kind, "employer_label": interview.label or None,
            "notes": interview.notes or "",
            "people": [{"id": p["id"], "name": p["name"], "title": p.get("title", "")}
                       for p in interview.people or []]}


def _rules(docs: documents.Documents) -> dict:
    """The owner's rules the Coach follows: their voice, their rules for interviews and the phrases never to write. No
    other section is handed over."""
    rules = documents.rules_for(docs.writing, "interview")
    rules["Never write"] = [b.phrase for b in docs.writing.banned] + [p.pattern for p in docs.writing.legacy_patterns]
    return rules


def _expectation(role: Opportunity) -> dict | None:
    ask = (role.analysis or {}).get("ask")
    if not isinstance(ask, dict):
        return None
    return {"expectation": ask.get("expectation", ask.get("ask")), "basis": ask.get("basis"), "band": ask.get("band"),
            "confidence": ask.get("confidence")}


def _employer_facts(employer: Employer | None) -> dict:
    if employer is None:
        return {}
    facts = {k: getattr(employer, k, None) for k in EMPLOYER_FACTS}
    return {"name": employer.name, **{k: v for k, v in facts.items() if v not in (None, "", [])}}


async def _task(session: AsyncSession, owner: str, prep: InterviewPrep, mode: str) -> dict:
    interview, role = await _pair(session, prep)
    employer = await session.get(Employer, role.employer_id) if role.employer_id else None
    docs = await profile_store.owner_documents(session, owner)
    files = WRITE_FILES if mode == "write" else REVIEW_FILES
    text = (await posting_text.held(session, role)).text
    reply: dict[str, Any] = {
        "ok": True, "id": str(prep.id), "mode": mode, "version": prep.version,
        "interview": _interview_part(interview),
        "role": {"id": str(role.id), "company": role.company, "title": role.title, "location": role.location,
                 "has_posting_text": bool(text.strip())},
        "employer": _employer_facts(employer), "rules": _rules(docs), "budget": coach_checks.BUDGET,
        "has_interview_rules": bool(docs.writing.interview_rules),
        "how": HOW_WRITE if mode == "write" else HOW_REVIEW,
        "read_next": ["posting", "resume", "earlier"] if mode == "write" else ["posting", "resume"],
        "instructions": await instruction_files.read(session, owner, "instructions", files[0]),
        "response_template": await instruction_files.read(session, owner, "templates", files[1]),
    }
    if mode == "write":
        reply["expectation"] = _expectation(role)
    else:
        reply["report"] = prep.report
    return reply


async def _packet_sent(session: AsyncSession, role: Opportunity) -> dict | None:
    """The packet that was sent: the newest that passed its checks, else the newest that was replaced."""
    for state in (PacketState.READY, PacketState.REPLACED):
        packet = await session.scalar(select(Packet).where(
            Packet.role_id == role.id, Packet.status == state.value)
            .order_by(Packet.version.desc()).limit(1))
        if packet is not None:
            return {"version": packet.version, "resume": packet.resume, "letter": packet.letter}
    return None


async def _posting_part(session: AsyncSession, owner: str, prep: InterviewPrep) -> dict:
    interview, role = await _pair(session, prep)
    docs = await profile_store.owner_documents(session, owner)
    texts = documents.master_texts(docs.resume)
    analysis = role.analysis or {}

    def lines(section: str) -> list[dict]:
        return [{"item": it.get("item", ""), "class": it.get("class"), "why": it.get("why", ""),
                 "from": [{"id": i, "text": texts.get(i, "")} for i in it.get("from") or []]}
                for it in analysis.get(section) or [] if isinstance(it, dict)]

    text = (await posting_text.held(session, role)).text
    return {"ok": True, "id": str(prep.id),
            "posting": {"text": text, "note": None if text.strip() else
                        "The app holds no text of the posting: say so, work from the title and add a gap."},
            "requirements": {s: lines(s) for s in ("required", "preferred", "responsibilities")},
            "research": {k: analysis[k] for k in RESEARCH_KEYS if k in analysis},
            "packet_sent": await _packet_sent(session, role)}


async def _resume_part(session: AsyncSession, owner: str, prep: InterviewPrep) -> dict:
    docs = await profile_store.owner_documents(session, owner)
    row = await story_library.newest(session, owner)
    stories = [{"id": s["id"], "title": s.get("title", ""), "index": s.get("index", ""),
                "blocks": [{"id": b["id"], "for": b.get("for", ""), "text": story_library.block_text(b),
                            "from": story_library.block_ids(b)} for b in s.get("blocks") or [] if not b.get("held")]}
               for s in (row.data or {}).get("stories") or []] if row is not None else []
    return {"ok": True, "id": str(prep.id), "master": story_library.master_view(docs), "stories": stories}


async def _earlier_part(session: AsyncSession, owner: str, prep: InterviewPrep) -> dict:
    """The ready reports before this one: this interview's own earlier versions, and the reports of other interviews at
    the same employer, newest first, at most three. The next report never contradicts them silently."""
    interview, role = await _pair(session, prep)
    same_employer = [role.id]
    if role.employer_id:
        same_employer = list((await session.scalars(select(Opportunity.id).where(
            Opportunity.owner == owner, Opportunity.employer_id == role.employer_id))).all())
    rows = (await session.execute(
        select(InterviewPrep, Interview).join(Interview, Interview.id == InterviewPrep.interview_id).where(
            InterviewPrep.owner == owner, InterviewPrep.state == PrepState.READY, InterviewPrep.id != prep.id,
            Interview.role_id.in_(same_employer)).order_by(InterviewPrep.finished_at.desc()).limit(3))).all()
    return {"ok": True, "id": str(prep.id), "earlier": [
        {"about": {**_when(i), "round": coach_view.round_label(i.round_kind), "version": p.version,
                   "same_interview": p.interview_id == interview.id},
         "report": p.report, "review": p.review} for p, i in rows]}


async def task_reply(session: AsyncSession, owner: str, part: str = "task", prep_id: str | uuid.UUID | None = None, *,
                     session_id: str | None = None, claim: str | None = None, now: datetime | None = None) -> dict:
    """`jobs.coach`: `task` takes the next prep waiting (a report to write, or a report to read as the interviewer) and
    gives its task; the other parts (`posting`, `resume`, `earlier`) give what that task says to read next, for the prep
    the session holds."""
    if part not in PARTS:
        raise CoachError(f"the parts are {', '.join(PARTS)}")
    if part == "task":
        prep, mode = await _claim_next(session, owner, now or utc_now(), session_id, claim)
        if prep is None:
            return {"ok": True, "done": True, "task": None, "note": NOTHING}
        if isinstance(prep, GapCoachJob):
            return await gap_coach.task(session, owner, prep)
        return await _task(session, owner, prep, mode)
    if (job := await gap_coach.holding(session, owner, prep_id)) is not None:
        return await gap_coach.part(session, owner, job, part)
    try:
        prep = await session.get(InterviewPrep, uuid.UUID(str(prep_id)))
    except ValueError:
        prep = None
    if prep is None or prep.owner != owner or prep.state not in (PrepState.WRITING, PrepState.REVIEWING):
        return {"ok": True, "note": "You hold no prep with that id. Call jobs.coach (part task) first; stop if it says "
                                    "none is waiting."}
    return await {"posting": _posting_part, "resume": _resume_part, "earlier": _earlier_part}[part](
        session, owner, prep)


# --- what the Coach sends ----------------------------------------------------------------------------------------


def _clean_review(review: dict) -> dict:
    keys = ("where", "problem", "fix", "action")
    return {"summary": " ".join(str(review.get("summary") or "").split()),
            "findings": [{k: " ".join(str(f.get(k) or "").split()) for k in keys if f.get(k)}
                         for f in review.get("findings") or []]}


async def submit(session: AsyncSession, owner: str, body: dict) -> dict:
    """`jobs.coach_submit`: take the report (write mode) or the red-team read (review mode). A report with a problem is
    sent back with every one named and nothing is stored; the third refusal fails the prep."""
    if not isinstance(body, dict):
        raise CoachError("send {id, report} or {id, review}")
    if (job := await gap_coach.get(session, owner, body.get("id"))) is not None:
        return await gap_coach.submit(session, owner, job, body)
    try:
        prep = await session.get(InterviewPrep, uuid.UUID(str(body.get("id"))), populate_existing=True)
    except ValueError:
        prep = None
    if prep is None or prep.owner != owner:
        raise CoachError("no such prep: use the id jobs.coach gave")
    moving = process.engine()
    if prep.state == PrepState.WRITING:
        interview, _ = await _pair(session, prep)
        problems = coach_checks.check_report(body.get("report"), await _context(session, prep, interview))
        if problems:
            prep.attempts += 1
            if prep.attempts >= MAX_REFUSALS:
                await _fail(session, prep, "The report didn't pass the checks after three tries: "
                            + " ".join(problems)[:600])
                return {"ok": False, "failed": True, "reason": "The prep failed: the report was refused three times. "
                        "Stop here.", "problems": problems}
            await session.flush()
            return {"ok": False, "reason": f"The report was sent back: {len(problems)} problem"
                                           f"{'' if len(problems) == 1 else 's'}.", "problems": problems,
                    "how_to_fix": "Fix each and send the whole report again with jobs.coach_submit."}
        prep.report = {k: copy.deepcopy(v) for k, v in body["report"].items() if k in coach_checks.SECTIONS}
        prep.state, prep.written_at = PrepState.REVIEW_WAITING.value, utc_now()
        await session.flush()
        await moving.complete(session, prep.instance_id, process.COACH_STEP, {"submitted": True})
        return {"ok": True, "taken": True,
                "note": "Taken. A second session reads it as the interviewer would. Stop here."}
    if prep.state == PrepState.REVIEWING:
        problems = coach_checks.check_review(body.get("review"))
        if problems:
            return {"ok": False, "reason": "The review was sent back.", "problems": problems,
                    "how_to_fix": "Fix each and send it again with jobs.coach_submit."}
        prep.review = _clean_review(body["review"])
        await session.flush()
        await moving.complete(session, prep.instance_id, process.REVIEW_STEP, {"submitted": True})
        return {"ok": True, "taken": True, "note": "Taken. Stop here."}
    raise CoachError("that prep isn't waiting for an answer from you")


async def _file(session: AsyncSession, prep: InterviewPrep) -> None:
    """File a prep whose report passed: ready, the red-team read beside it, the owner's open gaps made whole, one line
    of History and one notification. Called by the process's last step, and for a read that never ran."""
    interview, role = await _pair(session, prep)
    report = copy.deepcopy(prep.report or {})
    gaps = [g for g in report.get("open_gaps") or [] if isinstance(g, dict)]
    seen = {" ".join(str(g.get("action") or "").lower().split()) for g in gaps}

    def add(text: str, action: str) -> None:
        key = " ".join(action.lower().split())
        if key and key not in seen:
            seen.add(key)
            gaps.append({"text": text, "action": action})

    for q in report.get("questions") or []:
        if not q.get("answer") and q.get("gap"):
            add(f"No story yet for: {q.get('question', '')}", q["gap"])
    leaving = (report.get("staples") or {}).get("why_leaving") or {}
    if leaving.get("ask_owner"):
        add("The master doesn't say why you are leaving.", leaving["ask_owner"])
    review = prep.review or {"summary": "The red-team read didn't run.", "findings": []}
    for finding in review.get("findings") or []:
        if finding.get("action"):
            add(finding.get("problem", ""), finding["action"])
    report["open_gaps"] = gaps
    prep.report, prep.review = report, review
    prep.state, prep.finished_at, prep.error = PrepState.READY.value, utc_now(), None
    phrase = ROUND_PHRASE.get(interview.round_kind, "interview")
    await trail.record(session, prep.owner, event="interview_prep_ready", actor="agent", who=BY,
                       sentence=f"Your prep for the {phrase} at {role.company} is ready.",
                       link=("opportunity", role.id))
    await inbox.notify(session, prep.owner, key=f"interview_prep:{prep.id}", kind="interview_prep",
                       title=f"Your prep for the {phrase} at {role.company} is ready.",
                       link=("opportunity", role.id))
    await session.flush()


# --- stuck work ---------------------------------------------------------------------------------------------------


async def settle_stuck(session: AsyncSession, owner: str, now: datetime | None = None,
                       since: datetime | None = None) -> int:
    """Close what no session finished: a write a session took and `stuck_minutes` have passed since fails; a read the
    same way is filed without its red-team read (and says so); a prep nobody took in a day fails; a prep whose
    interview's time passed lapses. Returns how many."""
    moment = now or utc_now()
    closed = await lapse(session, owner, moment)

    async def writing(cutoff: datetime) -> list[InterviewPrep]:
        return list((await session.scalars(select(InterviewPrep).where(
            InterviewPrep.owner == owner, InterviewPrep.state == PrepState.WRITING,
            InterviewPrep.taken_at < cutoff))).all())

    async def reading(cutoff: datetime) -> list[InterviewPrep]:
        return list((await session.scalars(select(InterviewPrep).where(
            InterviewPrep.owner == owner, InterviewPrep.state == PrepState.REVIEWING,
            InterviewPrep.review_taken_at < cutoff))).all())

    async def give_up(prep: InterviewPrep) -> None:
        await _fail(session, prep, "The Coach stopped before it sent the report.")

    async def file_without(prep: InterviewPrep) -> None:
        prep.review = {"summary": "The red-team read didn't run.", "findings": []}
        await process.engine().complete(session, prep.instance_id, process.REVIEW_STEP, {"submitted": False})

    closed += await stuck.settle_stuck(session, now=moment, since=since, what="interview prep", find=writing,
                                       close=give_up)
    closed += await stuck.settle_stuck(session, now=moment, since=since, what="interview red-team read",
                                       find=reading, close=file_without)
    for prep in (await session.scalars(select(InterviewPrep).where(
            InterviewPrep.owner == owner, InterviewPrep.state == PrepState.WAITING,
            InterviewPrep.asked_at < moment - UNCLAIMED_FUSE))).all():
        await _fail(session, prep, "No agent took it.")
        closed += 1
    return closed


# --- the process's steps (playbooks/jobsearch/prepare_interview.bpmn) -----------------------------------------------


async def _prep(session: AsyncSession, data: dict) -> InterviewPrep:
    prep = await session.get(InterviewPrep, uuid.UUID(data["prep"]))
    if prep is None:
        raise CoachError("the prep this process belongs to isn't there")
    return prep


async def action_gather(session, instance, data):
    prep = await _prep(session, data)
    return {"ready": prep.state in coach_view.OPEN}


async def action_check(session, instance, data):
    return {"passed": True}


async def action_file(session, instance, data):
    prep = await _prep(session, data)
    await _file(session, prep)
    return {"state": prep.state}


def _register() -> None:
    source = "backend/app/jobsearch/interviews/coach.py"
    registry.register("jobs.interview_gather", action_gather, inputs=["owner", "prep"], outputs=["ready"],
                      description="Checks the prep for the interview is still open.", source=source)
    registry.register(
        "jobs.interview_check", action_check, inputs=["owner", "prep"], outputs=["passed"],
        description="Marks the report as having passed the code checks that ran when it was taken: every claim about "
                    "the owner cites the master or a story, no banned phrase, every time-sensitive fact dated.",
        source=source)
    registry.register(
        "jobs.interview_file", action_file, inputs=["owner", "prep"], outputs=["state"],
        description="Files the report with its red-team read, adds what the owner can still do to the open gaps, "
                    "writes a line to the role's History and tells the owner the prep is ready.", source=source)


_register()
