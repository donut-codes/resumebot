"""Find Gaps over HTTP (docs/PRD.md path 38), under My Resume:

- `GET /find-gaps` is the dashboard: a row for each target role (how many researched roles it rests on, the shares
  of the analyst's grades, its latest run) and the run going now.
- `GET /find-gaps/jobs` is the picker's list of roles (read when it is first opened).
- `POST /find-gaps/runs` starts a run for a target role (`role_id`) or a title that is not one (`title`); a run is
  read with `GET /find-gaps/runs/{id}` (its progress, then its themes), asked again with `check-again` (the same
  postings against the master as it is now) or `new-sample` (a fresh draw). A small Claude session reads the
  postings, woken by the request (`wake.GAPS_ASKED`); whether an agent can take it now is `GET /agent/ready`, and
  Claude's peak hours hold it off like Import your resume.
- A theme is answered with `PUT .../themes/{id}/answer` ("I haven't" or "Not relevant") and the answer taken back with
  `DELETE /find-gaps/answers/{id}`; `POST .../themes/{id}/draft` asks for a line drafted from the owner's own words
  (`GET /find-gaps/drafts/{id}` says how that is going).
- A run started from a job (`opportunity_id`, 38.11) is worked through with the Coach: `POST .../questions` asks it for
  questions about the run's gaps, `PUT /find-gaps/cards/{id}` is the owner's answer, skip or "not true" to one,
  `POST .../proposals` asks it to propose master lines from the answers, and `POST /find-gaps/proposals/{id}/accept`
  (with the owner's own edit of the words, if any) or `.../reject` decides one (38.13 to 38.16).

Nothing here writes to the master resume, a packet or the Pipeline but the owner's Add to resume (the resume's own save,
`PUT /profile/resume`, with the version the page loaded) and their Accept of a proposal (`proposals.accept`, the same
save). Services flush; these routes commit.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.db import ReadSession, Session
from app.errors import NotFound
from app.jobsearch.gaps import coach as gap_coach
from app.jobsearch.gaps import drafts as gap_drafts
from app.jobsearch.gaps import proposals
from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.gaps.runs import GapError
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import engine_calls
from app.settings import settings

router = APIRouter(prefix="/api/v1/find-gaps")


class StartBody(BaseModel):
    #: A target role's id, or...
    role_id: str | None = Field(default=None, max_length=80)
    #: ...a role title that is not one, as long as it was typed, or...
    title: str | None = None
    #: ...a role in the app to start from (38.11).
    opportunity_id: str | None = Field(default=None, max_length=80)


class AnswerBody(BaseModel):
    #: `not_done` (I haven't) or `not_relevant`.
    answer: str = Field(max_length=20)


class CardBody(BaseModel):
    #: `answer` (with `text`, the owner's own words), `skip` or `not_true`.
    action: str = Field(max_length=20)
    text: str = ""


class AcceptBody(BaseModel):
    #: The owner's own rewrite of the words, saved in place of the Coach's.
    text: str | None = None
    #: The master version the page loaded: a newer one in force refuses the accept (StaleSave), in words.
    base_version: int | None = None


class DraftBody(BaseModel):
    #: What the owner did, in their own words, as long as they were written (`gap_drafts.start` refuses what an agent
    #: cannot be sent).
    words: str


async def _started(session, run) -> dict:
    """Commit the run and wake the runner for it."""
    run_id = str(run.id)
    await session.commit()
    await engine_calls.knock(run_id, reason=engine_calls.GAPS_ASKED)
    return {"id": run_id}


@router.get("")
async def dashboard(session: ReadSession) -> dict:
    """A row for each target role, and the run going now (or the last one that failed). It only reads: a run that took
    too long is told as failed, and the engine's pass saves that."""
    return await find_gaps.dashboard(session, settings.owner)


@router.get("/jobs")
async def jobs(session: ReadSession) -> list[dict]:
    """The roles the picker lists (38.11): every role that holds the text of its posting, by company and title.
    Read when the picker is first opened, not with the dashboard."""
    return await find_gaps.picker(session, settings.owner)


@router.post("/runs")
async def start(body: StartBody, session: Session) -> dict:
    """Start a run for a target role, or for a title that is not one. The runner is woken for it. 409 with `why` when no
    agent can take it now, and 409 while another run is going; 422 in words when it cannot be run (fewer than 5
    employers have a stored posting for the role)."""
    try:
        if body.opportunity_id:
            run = await find_gaps.start_job(session, settings.owner, body.opportunity_id)
        elif body.role_id:
            run = await find_gaps.start_role(session, settings.owner, body.role_id)
        elif body.title:
            run = await find_gaps.start_title(session, settings.owner, body.title)
        else:
            raise GapError("Name a target role, pick a job or type a role title.")
    except GapError:
        await session.commit()  # the runs that took too long were failed on the way
        raise
    return await _started(session, run)


@router.get("/runs/{run_id}")
async def read(run_id: str, session: ReadSession) -> dict:
    """Where a run is: waiting for an agent, working (how many postings it has read), done (its themes) or failed
    (why). It only reads: a run that took too long is told as failed, and the engine's pass saves that."""
    out = await find_gaps.told(session, settings.owner, run_id)
    if out is None:
        raise NotFound(find_gaps.NO_SUCH_RUN)
    return out


@router.post("/runs/{run_id}/check-again")
async def check_again(run_id: str, session: Session) -> dict:
    """Grade the run's postings again against the master resume as it is now, with no new search."""
    try:
        run = await find_gaps.check_again(session, settings.owner, run_id)
    except GapError:
        await session.commit()
        raise
    return await _started(session, run)


@router.post("/runs/{run_id}/new-sample")
async def new_sample(run_id: str, session: Session) -> dict:
    """Draw a fresh sample of stored postings for the run's role."""
    try:
        run = await find_gaps.new_sample(session, settings.owner, run_id)
    except GapError:
        await session.commit()
        raise
    return await _started(session, run)


@router.put("/runs/{run_id}/themes/{theme_id}/answer")
async def answer(run_id: str, theme_id: str, body: AnswerBody, session: Session) -> dict:
    """"I haven't" (`not_done`: a real gap) or "Not relevant" (`not_relevant`: hidden for the role, on later runs
    too). It touches nothing outside Find Gaps."""
    row = await find_gaps.answer(session, settings.owner, run_id, theme_id, body.answer)
    await session.commit()
    return {"id": str(row.id), "answer": row.answer}


@router.delete("/answers/{answer_id}")
async def take_back(answer_id: str, session: Session) -> dict:
    """Take an answer back: a theme marked a real gap is unmarked, a hidden theme is brought back."""
    await find_gaps.clear_answer(session, settings.owner, answer_id)
    await session.commit()
    return {"cleared": True}


@router.post("/runs/{run_id}/themes/{theme_id}/draft")
async def draft(run_id: str, theme_id: str, body: DraftBody, session: Session) -> dict:
    """Ask for a line drafted from the owner's own words about a theme. The runner is woken for it. 409 with `why`
    when no agent can take it now."""
    try:
        row = await gap_drafts.start(session, settings.owner, run_id, theme_id, body.words)
    except GapError:
        await session.commit()
        raise
    row_id = str(row.id)
    await session.commit()
    await engine_calls.knock(row_id, reason=engine_calls.GAPS_ASKED)
    return {"id": row_id}


@router.get("/drafts/{draft_id}")
async def read_draft(draft_id: str, session: ReadSession) -> dict:
    """Where a drafted line is: waiting for an agent, drafting, done (the line) or failed (why). It only reads."""
    out = await gap_drafts.told(session, settings.owner, draft_id)
    if out is None:
        raise NotFound("That draft is gone. Ask for it again.")
    return out


async def _coach_started(session, job) -> dict:
    """Commit the Coach's job and wake the runner for it."""
    job_id = str(job.id)
    await session.commit()
    await engine_calls.knock(job_id, reason=engine_calls.COACH_ASKED)
    return {"id": job_id}


@router.post("/runs/{run_id}/questions")
async def ask_questions(run_id: str, session: Session) -> dict:
    """Ask the Coach for questions about the gaps of a finished run started from a job. 409 with `why` when no agent can
    take it now, and while a job is going for the run; 422 in words when there is nothing left to ask."""
    try:
        job = await gap_coach.start_ask(session, settings.owner, run_id)
    except GapError:
        await session.commit()  # the runs and jobs that took too long were failed on the way
        raise
    return await _coach_started(session, job)


@router.post("/runs/{run_id}/proposals")
async def suggest_lines(run_id: str, session: Session) -> dict:
    """Ask the Coach to propose master lines from the owner's answers no suggestion has been made from yet."""
    try:
        job = await gap_coach.start_propose(session, settings.owner, run_id)
    except GapError:
        await session.commit()
        raise
    return await _coach_started(session, job)


@router.put("/cards/{card_id}")
async def answer_card(card_id: str, body: CardBody, session: Session) -> dict:
    """The owner's answer to one of the Coach's questions (`answer` with their words), `skip` or `not_true` (the theme
    is never asked again for the role). It touches nothing outside Find Gaps."""
    card = await gap_coach.answer_card(session, settings.owner, card_id, body.action, body.text)
    await session.commit()
    return {"id": str(card.id), "state": card.state}


@router.get("/proposals/open")
async def open_proposals(reader: ReadSession) -> dict:
    """What the owner's own Claude suggested for the master resume that waits for the owner (docs/PRD.md 50.15), and the
    master version in force, which an accept names. Not a Find Gaps run's proposals: those are under their run."""
    resume = (await profile_store.owner_documents(reader, settings.owner)).resume
    version = await profile_store.newest_version(reader, settings.owner, "resume")
    rows = await proposals.open_door(reader, settings.owner)
    return {"version": version, "proposals": [proposals.view(p, resume) for p in rows]}


@router.post("/proposals/{proposal_id}/accept")
async def accept_proposal(proposal_id: str, body: AcceptBody, session: Session, reader: ReadSession) -> dict:
    """Accept a proposal: its line is saved into its job as the next version of the master (with the owner's own edit of
    the words, when they sent one). Answers with that version and the master as it now is."""
    version = await proposals.accept(session, settings.owner, proposal_id, body.text, body.base_version)
    await session.commit()
    # The master is read on a reader once the new version is committed: the writer's turn is for writing.
    resume = (await profile_store.owner_documents(reader, settings.owner)).resume
    return {"version": version, "resume": resume.model_dump(mode="json")}


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, session: Session) -> dict:
    """Reject a proposal: it is kept as rejected, the master is left as it was and the same words are not proposed
    again."""
    row = await proposals.reject(session, settings.owner, proposal_id)
    await session.commit()
    return {"id": str(row.id), "status": row.status}
