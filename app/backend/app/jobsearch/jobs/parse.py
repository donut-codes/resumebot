"""Fill from a posting (What to look for, inside a target role): the owner pastes a job posting and a small Claude
session (Haiku, the `parser` kind) splits it into the role's fields, for the owner to look over before saving.

The same session has a second mode, for a resume (Me, Import your resume): the owner chooses their resume file or pastes
its text, and the session divides it into the master resume's fields (`resume/parse.py` holds those fields and their
check; `resume/text.py` reads a file's text). A job's `kind` says which: `posting` or `resume`. Everything below is the
same for both, but the words, the limits on the text and the fields a session reports.

A parse job is a row of `js_parse_jobs`: waiting (the owner's request) -> reading (a session took it with
`jobs.parse_next`) -> done (`jobs.parse_report` gave its fields) or failed, with a plain sentence. It is its own small
table, not a task, a handout or a mission: it belongs to no search round or site project, nobody but the page reads it,
and it lives for minutes. A job no session took within 5 minutes (of the request, or of the agent's last session ending
if that is later: `job_queue.untaken`), or one not finished 3 minutes after it was taken, fails (checked whenever
anything reads the jobs, so no clock is needed), its text is dropped when it ends, and the row after a day.

A master-resume job (`profile`) is the exception: it waits as long as it takes for a free session (the owner is not at
the screen for it), is made again if it is lost or fails (`structured_profile.ensure`), and is handed out after any job
the owner is waiting on.

Only offered when an agent is there to take the work (`ready`): the runner checked in lately, it is not paused, and
Claude's peak hours are not blacked out. One busy with another session does not refuse it: the job is queued behind that
session and says so (`job_queue.say_queued`). The runner's session is woken by the route that makes the job
(`wake.PARSE_ASKED`), and `session_kinds.next_kind` answers `parser` first while a job waits: the owner is at the
screen.
"""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import errors
from app.errors import DomainError
from app.jobsearch import owner_text
from app.jobsearch.jobs import posting_structure
from app.jobsearch.models import Opportunity, ParseJob
from app.jobsearch.research import rank
from app.jobsearch.resume import cleansed_master, structured_profile
from app.jobsearch.resume import parse as resume_parse
from app.jobsearch.states import ParseKind, ParseState
from app.jobsearch.work import hand_outs, instruction_files, job_queue
from app.jobsearch.work.claim import claim_one
from app.timeutil import owner_today

#: The states of a job (`ParseState`) and the two things its text can be (`ParseKind`).
WAITING, READING, DONE, FAILED = (ParseState.WAITING, ParseState.READING, ParseState.DONE, ParseState.FAILED)
POSTING, RESUME, PROFILE, STRUCTURE = ParseKind.POSTING, ParseKind.RESUME, ParseKind.PROFILE, ParseKind.STRUCTURE
RANK, RECIPE, CLAIMS = ParseKind.RANK, ParseKind.RECIPE, ParseKind.CLAIMS
#: The kinds nobody waits at the screen for: they wait for a free session however long, after the owner's own jobs.
BACKGROUND = (PROFILE, STRUCTURE, RANK, RECIPE, CLAIMS)
#: How long a session may take once it has the job; the rest of the limits are every job's (`job_queue`).
FINISH_WITHIN = timedelta(minutes=3)
TAKE_WITHIN, KEEP_FOR = job_queue.TAKE_WITHIN, job_queue.KEEP_FOR
#: The fewest characters a pasted posting may have (it has no most).
MIN_CHARS = job_queue.MIN_CHARS
#: The progress line for each state, in plain words, for each kind of job.
STEPS = {WAITING: "Waiting for an agent", READING: "Reading the posting", DONE: "Done", FAILED: "Stopped"}
RESUME_STEPS = {**STEPS, READING: "Reading your resume"}
PROFILE_STEPS = {**STEPS, READING: "Reading your master resume"}
STRUCTURE_STEPS = {**STEPS, READING: "Reading a posting for its record"}
RANK_STEPS = {**STEPS, READING: "Ranking postings against your profile"}
CLAIMS_STEPS = {**STEPS, READING: "Condensing your master resume into claims"}
RECIPE_STEPS = {**STEPS, READING: "Writing a recipe for a site's listings"}
#: A recipe takes a session longer than a posting does: it may be handed back and fixed twice.
RECIPE_FINISH_WITHIN = timedelta(minutes=15)

#: The role's fields a session reports, and the most each holds. This is what the agent writes (a summary of the
#: posting in a few sentences, not the owner's own text, which has no limit); a job title is a line. The parser's
#: instructions (`jobsearch.parser.instructions.md`) ask for the posting's title and up to 3 close variants, "never
#: more than 4": that is MAX_TITLES (a target role itself holds more, `profile.MAX_TITLES`).
TEXT_FIELDS = ("description", "responsibilities", "qualifications", "preferred", "compensation")
MAX_TEXT_CHARS = 1000
MAX_NAME_CHARS = 120
MAX_TITLES = 4
MAX_TITLE_CHARS = 120
FIELDS = ("name", "titles", *TEXT_FIELDS)

NOT_TAKEN = "No agent picked this up within 5 minutes. Try again in a moment, or fill the role in yourself."
NOT_FINISHED = "The agent didn't finish reading it within 3 minutes. Try again, or fill the role in yourself."
NOTHING_FOUND = "The agent found nothing it could use in that text. Check that you pasted the job posting itself."
UNKNOWN_ID = "no parse job has that id; send the id jobs.parse_next gave you"
ENDED = job_queue.ENDED


class ParseError(DomainError):
    """A parse job that cannot be made, found or answered; the message is for the owner or for the session."""

    how_to_fix = "check the arguments against this tool's schema"


class NotReady(errors.NotReady, ParseError):
    """No agent can take a parse job now; `why` is the one sentence for the owner (answered 409 with `detail` and
    `why`)."""


ready = job_queue.ready


def _fail(job: ParseJob, error: str, now: datetime) -> None:
    job_queue.fail(job, FAILED, error, now, text="")


def _patient(job: ParseJob) -> bool:
    """A job nobody waits at the screen for: it waits for a free session however long it takes."""
    return job.kind in BACKGROUND


def _last_touched(job: ParseJob) -> datetime | None:
    """Where the time a session has to finish counts from: when it took the job, and later for a recipe (it may be
    handed back and fixed twice: `RECIPE_FINISH_WITHIN`)."""
    if job.kind == RECIPE.value and job.taken_at:
        return job.taken_at + (RECIPE_FINISH_WITHIN - FINISH_WITHIN)
    return job.taken_at


async def expire(session: AsyncSession, owner: str, now: datetime | None = None, *, fail_late: bool = True,
                 drop_old: bool = True) -> None:
    """Fail the jobs that took too long and drop the ones kept long enough. Run before any read of the jobs. The halves
    can be asked for apart (`job_queue.expire`)."""
    await job_queue.expire(
        session, ParseJob, owner, working=READING, finish_within=FINISH_WITHIN, not_taken=NOT_TAKEN,
        not_finished=NOT_FINISHED, failed=_fail, patient=_patient, last_touched=_last_touched, now=now,
        fail_late=fail_late, drop_old=drop_old)


async def start(session: AsyncSession, owner: str, text: str, role_id: str | None = None,
                now: datetime | None = None, kind: str = POSTING) -> ParseJob:
    """Make a waiting job for a posting's or a resume's text (the route checks its length and wakes the runner after the
    commit). Raises `NotReady` with the reason when no agent can take it now."""
    await expire(session, owner, now)
    ok, why = await ready(session, owner, now)
    if not ok:
        raise NotReady(why or "No agent can take this now.")
    job = ParseJob(owner=owner, kind=kind, role_id=(role_id or "").strip()[:80] or None, text=text.strip(),
                   state=WAITING, created_at=now or datetime.now(UTC))
    session.add(job)
    await session.flush()
    return job


#: What a refusal over the room calls the answer shape an agent is sent beside a resume.
SHAPE = "the answer shape sent with it"


async def refuse_resume_over_room(session: AsyncSession, owner: str, text: str, *, what: str, fix: str) -> None:
    """Refuse a resume's text an agent could not be sent whole (413 in words, with both sizes), before a job is made:
    the room it has is what one reply leaves for the owner's texts (`owner_text.ROOM_CHARS`) less the resume's answer
    shape, which rides with it at the size now saved (`take_next`). The text is never cut. The route that takes a pasted
    or chosen resume (`parse_routes.py`) and the owner door's `resume.import` both ask this before `start`."""
    shape = await instruction_files.read(session, owner, "templates", RESUME_TEMPLATE)
    owner_text.refuse_over_room(text.strip(), what, beside=(SHAPE, len(shape)), result=owner_text.NOT_SENT, fix=fix)


async def get(session: AsyncSession, owner: str, job_id: str, now: datetime | None = None) -> ParseJob | None:
    """The owner's job with this id (None when there is none, or it is gone), after the ones that took too long fail."""
    await expire(session, owner, now)
    try:
        key = uuid.UUID(str(job_id))
    except ValueError:
        return None
    return await session.scalar(select(ParseJob).where(ParseJob.owner == owner, ParseJob.id == key))


def view(job: ParseJob) -> dict:
    """What the page reads while it waits: the state, a progress line in plain words, and the fields (done) or the
    error (failed)."""
    steps = {RESUME.value: RESUME_STEPS, PROFILE.value: PROFILE_STEPS, STRUCTURE.value: STRUCTURE_STEPS,
             RANK.value: RANK_STEPS, RECIPE.value: RECIPE_STEPS, CLAIMS.value: CLAIMS_STEPS}.get(job.kind, STEPS)
    out = {"id": str(job.id), "state": job.state, "step": steps[job.state], "role_id": job.role_id}
    if job.state == DONE:
        out["fields"] = job.fields
    elif job.state == FAILED:
        out["error"] = job.error
    return out


async def told(session: AsyncSession, owner: str, job_id: str, now: datetime | None = None) -> dict | None:
    """What the page is told of the owner's job with this id (`GET /profile/parse/{id}`), or None when there is none or
    it is gone. It only reads, so the page asks on a reader: a job kept past its day is gone, and one that took too
    long is told failed in the words `expire` fails it with, whether or not the engine's pass has got to it yet
    (`housekeeping.MINUTE` fails the late jobs, `HOUR` drops the old rows). A waiting job says when it is queued."""
    now = now or datetime.now(UTC)
    try:
        key = uuid.UUID(str(job_id))
    except ValueError:
        return None
    job = await session.scalar(select(ParseJob).where(ParseJob.owner == owner, ParseJob.id == key))
    if job is None or job_queue.kept_too_long(job, now):
        return None
    why = await job_queue.late(session, owner, job, working=READING, finish_within=FINISH_WITHIN,
                               not_taken=NOT_TAKEN, not_finished=NOT_FINISHED, now=now, last_touched=_last_touched,
                               patient=_patient)
    if why is not None:
        return {"id": str(job.id), "state": FAILED.value, "step": STEPS[FAILED], "role_id": job.role_id, "error": why}
    return await job_queue.say_queued(session, owner, view(job), now)


async def _oldest_waiting(session: AsyncSession, owner: str) -> Select[ParseJob]:
    """The select of the oldest job no session has taken and none is claimed for (`hand_outs.py`). The owner's own jobs
    go first, and among the Haiku ranks the one a star asked for (`rank.FIRST`) goes before the ordinary batches."""
    oldest = (select(ParseJob).where(ParseJob.owner == owner, ParseJob.state == WAITING)
              .order_by(ParseJob.kind.in_(BACKGROUND), ParseJob.fields[rank.FIRST].as_boolean().is_distinct_from(True),
                        ParseJob.created_at).limit(1))
    if skip := await hand_outs.held_uuids(session, owner, hand_outs.PARSER):
        oldest = oldest.where(ParseJob.id.not_in(skip))
    return oldest


async def waiting(session: AsyncSession, owner: str, now: datetime | None = None) -> ParseJob | None:
    """The oldest job no session has taken and none is claimed for (`hand_outs.py`), after the ones that took too long
    fail: what makes `parser` the answer to "which work waits". The failing, and the master's own jobs being made, are
    the app's housekeeping (`housekeeping.settle`), never this read's. Taking it is `take_next`'s one conditional
    statement (`claim`), not this read followed by a save."""
    return await session.scalar(await _oldest_waiting(session, owner))


PARSE_HOW = (
    "Split this posting into the role's fields, each from the posting's own words, condensed: name (the role in plain "
    f"words), titles (the posting's title and up to 3 close variants a job board might use; at most {MAX_TITLES}), and "
    "description, responsibilities, qualifications, preferred and compensation (a few sentences each, at most "
    f"{MAX_TEXT_CHARS} characters). Leave a field empty when the posting does not say; never invent. Send them with "
    "jobs.parse_report: {id, fields}. Then call jobs.parse_next again."
)


async def activity(session: AsyncSession, owner: str) -> str:
    """What a parser session is at, for the Agents page: the text it holds (else the oldest waiting) is a resume or a
    posting."""
    kind = await session.scalar(
        select(ParseJob.kind).where(ParseJob.owner == owner, ParseJob.state == READING)
        .order_by(ParseJob.taken_at).limit(1))
    if kind is None:
        kind = await session.scalar(
            select(ParseJob.kind).where(ParseJob.owner == owner, ParseJob.state == WAITING)
            .order_by(ParseJob.created_at).limit(1))
    if kind == PROFILE:
        return "Reading your master resume"
    if kind == STRUCTURE:
        return "Reading a posting for its record"
    if kind == RANK:
        return "Ranking postings against your profile"
    if kind == CLAIMS:
        return "Condensing your master resume into claims"
    if kind == RECIPE:
        return "Writing a recipe for a site's listings"
    return "Reading your resume" if kind == RESUME else "Reading a posting you pasted"


#: The shape a session answers a resume in: `templates/jobsearch.parser.resume.template.md`, handed out with the text.
RESUME_TEMPLATE = "jobsearch.parser.resume"
#: The shape a session answers a master resume in (the profile mode).
PROFILE_TEMPLATE = "jobsearch.parser.profile"
#: The shape a session answers a posting's text in for its structured record (the structure mode).
STRUCTURE_TEMPLATE = "jobsearch.parser.structure"
#: The shape a session answers a batch of postings in for their ranks (the rank mode).
RANK_TEMPLATE = "jobsearch.parser.rank"
#: The shape a session answers a site's recorded traffic in for its recipe (the recipe mode).
RECIPE_TEMPLATE = "jobsearch.parser.recipe"
#: The shape a session answers a master resume in for its claims (the claims mode).
CLAIMS_TEMPLATE = "jobsearch.parser.claims"


async def take_next(session: AsyncSession, owner: str, now: datetime | None = None,
                    session_id: str | None = None, claim: str | None = None) -> dict:
    """The next text to split: the one this session already holds, else the job claimed for it (`claim`, the hand-out
    the runner started the session for: `hand_outs.py`), else the oldest waiting that none is claimed for, which is
    taken (it is `reading` from now, and has 3 minutes; a job a session took is told to that session alone, never to a
    second one asking at the same time). A posting comes as `{id, kind, text, how}`; a resume also with the shape to
    answer in (`response_template`). With none left it says so."""
    now = now or datetime.now(UTC)
    await expire(session, owner, now)
    held = select(ParseJob).where(ParseJob.owner == owner, ParseJob.state == READING)
    if session_id:  # only the job this session took: another session's is its own, never handed out twice
        held = held.where(ParseJob.fields["session_id"].as_string() == session_id[:80])
    job = await session.scalar(held.order_by(ParseJob.taken_at).limit(1))
    took = False  # a job the session held is `reading` already; one taken now is marked below
    if job is None and claim:  # exactly the job the app claimed for this session, while it still waits
        taken = await hand_outs.take(session, owner, hand_outs.PARSER, claim, session_id, now) or []
        claimed = [uuid.UUID(i) for i in taken]
        job = await claim_one(session, select(ParseJob).where(
            ParseJob.owner == owner, ParseJob.id.in_(claimed), ParseJob.state == WAITING),
            state=READING, taken_at=now) if claimed else None
        took = job is not None
    if job is None:  # one statement finds the oldest waiting and takes it: two sessions are never handed the same job
        job = await claim_one(session, await _oldest_waiting(session, owner), state=READING, taken_at=now)
        took = job is not None
    if took and session_id and job is not None:
        job.fields = {**(job.fields or {}), "session_id": session_id[:80]}
        await session.flush()
    if job is None:
        return {"ok": True, "done": True, "how": "No posting is waiting. Stop: you are finished."}
    if job.kind == RESUME:
        return {"ok": True, "id": str(job.id), "kind": RESUME, "text": job.text, "how": resume_parse.HOW,
                "response_template": await instruction_files.read(session, owner, "templates", RESUME_TEMPLATE)}
    if job.kind == PROFILE:
        return {"ok": True, "id": str(job.id), "kind": PROFILE, "text": job.text, "how": structured_profile.HOW,
                "response_template": await instruction_files.read(session, owner, "templates", PROFILE_TEMPLATE)}
    if job.kind == STRUCTURE:
        return {"ok": True, "id": str(job.id), "kind": STRUCTURE, "text": job.text, "how": posting_structure.HOW,
                "response_template": await instruction_files.read(session, owner, "templates", STRUCTURE_TEMPLATE)}
    if job.kind == RANK:
        return {"ok": True, "id": str(job.id), "kind": RANK, "text": job.text, "how": rank.HOW,
                "response_template": await instruction_files.read(session, owner, "templates", RANK_TEMPLATE)}
    if job.kind == CLAIMS:
        return {"ok": True, "id": str(job.id), "kind": CLAIMS, "text": job.text, "how": cleansed_master.HOW,
                "response_template": await instruction_files.read(session, owner, "templates", CLAIMS_TEMPLATE)}
    if job.kind == RECIPE:
        from app.jobsearch.browser.recipes import record as recipe_record  # it loads the engine's browser code

        how = recipe_record.HOW_POSTING if (job.fields or {}).get("posting") else recipe_record.HOW
        return {"ok": True, "id": str(job.id), "kind": RECIPE, "text": job.text, "how": how,
                "response_template": await instruction_files.read(session, owner, "templates", RECIPE_TEMPLATE)}
    return {"ok": True, "id": str(job.id), "kind": POSTING, "text": job.text, "how": PARSE_HOW}


def _text(key: str, value, limit: int, problems: list[str]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        problems.append(f"{key} must be text, not {type(value).__name__}")
        return ""
    value = value.strip()
    if len(value) > limit:
        problems.append(f"{key} is {len(value)} characters; at most {limit}. Condense it to a few sentences")
        return ""
    return value


def check_fields(raw) -> dict:
    """The role's fields as a session reported them, tidied: text trimmed, titles trimmed and without repeats. Anything
    that does not fit is named, every problem at once, so the session can fix it and send it again."""
    if not isinstance(raw, dict):
        raise ParseError("fields must be an object: {name, titles, " + ", ".join(TEXT_FIELDS) + "}")
    problems: list[str] = []
    unknown = [str(k) for k in raw if k not in FIELDS]
    if unknown:
        problems.append(f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not "
                        f"{'a field' if len(unknown) == 1 else 'fields'}; the fields are {', '.join(FIELDS)}")
    out: dict = {"name": _text("name", raw.get("name"), MAX_NAME_CHARS, problems)}
    titles = raw.get("titles")
    kept: list[str] = []
    if titles is None:
        pass
    elif not isinstance(titles, list) or not all(isinstance(t, str) for t in titles):
        problems.append("titles must be a list of strings, one job title each")
    else:
        for title in (t.strip() for t in titles):
            if len(title) > MAX_TITLE_CHARS:
                problems.append(f"a title is {len(title)} characters; at most {MAX_TITLE_CHARS}: send the title only")
            elif title and title.lower() not in {k.lower() for k in kept}:
                kept.append(title)
        if len(kept) > MAX_TITLES:
            problems.append(
                f"titles has {len(kept)}; at most {MAX_TITLES}: send the posting's title and up to 3 close variants")
    out["titles"] = kept
    for key in TEXT_FIELDS:
        out[key] = _text(key, raw.get(key), MAX_TEXT_CHARS, problems)
    if problems:
        error = ParseError("; ".join(problems))
        error.how_to_fix = "fix those and send jobs.parse_report again with the same id"
        raise error
    return out


def check_resume_fields(raw) -> dict:
    """The master resume's fields as a session reported them, tidied (`resume_parse.check`); anything that does not fit
    is named, every problem at once, so the session can fix it and send it again."""
    clean, problems = resume_parse.check(raw)
    if problems:
        error = ParseError("; ".join(problems))
        error.how_to_fix = "fix those and send jobs.parse_report again with the same id"
        raise error
    return clean


async def report(session: AsyncSession, owner: str, job_id: str, fields, now: datetime | None = None) -> dict:
    """Take a session's fields for the job it holds: checked, kept, and the job is done. A job that has already ended
    (it took too long) is refused, so the session stops. Fields with nothing in them fail the job with a sentence for
    the owner: the text was not a posting (or not a resume)."""
    now = now or datetime.now(UTC)
    job = await get(session, owner, job_id, now)
    if job is None:
        raise ParseError(UNKNOWN_ID)
    if job.state in (DONE, FAILED):
        raise ParseError(ENDED)
    if job.kind == PROFILE:
        return await _report_profile(session, owner, job, fields, now)
    if job.kind == STRUCTURE:
        return await _report_structure(session, owner, job, fields, now)
    if job.kind == RANK:
        return await _report_rank(session, owner, job, fields, now)
    if job.kind == CLAIMS:
        return await _report_claims(session, owner, job, fields, now)
    if job.kind == RECIPE:
        return await _report_recipe(session, owner, job, fields, now)
    if job.kind == RESUME:
        clean = check_resume_fields(fields)
        nothing = resume_parse.is_empty(clean)
    else:
        clean = check_fields(fields)
        nothing = not (clean["name"] or clean["titles"] or any(clean[k] for k in TEXT_FIELDS))
    job.finished_at, job.text = now, ""
    if nothing:
        job.state, job.error = FAILED, resume_parse.NOTHING_FOUND if job.kind == RESUME else NOTHING_FOUND
    else:
        job.state, job.fields = DONE, clean
    await session.flush()
    return {"ok": True, "taken": True, "state": job.state,
            "next": "Call jobs.parse_next for the next posting; when it says none is waiting, stop."}


async def _report_profile(session: AsyncSession, owner: str, job: ParseJob, fields, now: datetime) -> dict:
    """A session's names for a master version: checked against that version's lines (every problem named at once, the
    job stays open for another try), then the one record of that version is built by code and kept. The master is only
    read. A report with nothing in it fails the job."""
    version = structured_profile.version_of(job.role_id)
    resume = None if version is None else await structured_profile.version_resume(session, owner, version)
    if version is None or resume is None:
        _fail(job, "The master resume this was made from is gone.", now)
        await session.flush()
        raise ParseError(ENDED)
    clean, problems = structured_profile.check(fields, resume)
    if problems:
        error = ParseError("; ".join(problems))
        error.how_to_fix = "fix those and send jobs.parse_report again with the same id"
        raise error
    job.finished_at, job.text = now, ""
    if structured_profile.is_empty(clean):
        job.state, job.error = FAILED, "The agent found nothing in the master resume it could use."
    else:
        record = structured_profile.build(resume, clean, owner_today(now))
        await structured_profile.keep(session, owner, version, record)
        job.state, job.fields = DONE, {"version": version, "fields": sorted(record.fields)}
    await session.flush()
    return {"ok": True, "taken": True, "state": job.state,
            "next": "Call jobs.parse_next for the next one; when it says none is waiting, stop."}


async def _report_rank(session: AsyncSession, owner: str, job: ParseJob, fields, now: datetime) -> dict:
    """A session's ranks for a batch of postings: every malformed one named at once (the job stays open for another
    try), the rest kept beside each role's fit percent. A posting the session left out is ranked by a later job. A role
    ranked is First Impression, and the best of them go on deck in the pipeline's turn, which this asks for once the
    ranks are committed (`schedule.signal_after_commit`): the turn is the loop's, never run inside the report. A report
    that ranked none fails the job."""
    count = len((job.fields or {}).get("items", []))
    ranks, problems = rank.check(fields, count)
    if problems:
        error = ParseError("; ".join(problems))
        error.how_to_fix = "fix those and send jobs.parse_report again with the same id"
        raise error
    kept = await rank.keep(session, owner, job, ranks) if ranks else 0
    if kept:
        from app.jobsearch.loops import schedule  # the schedule reads the pool, which reads this module

        schedule.signal_after_commit(session)
    job.finished_at, job.text = now, ""
    if not kept:
        job.state, job.error = FAILED, "The agent ranked none of the postings."
    else:
        job.state, job.fields = DONE, {**(job.fields or {}), "ranked": kept, "left_out": count - len(ranks)}
    await session.flush()
    return {"ok": True, "taken": True, "state": job.state,
            "next": "Call jobs.parse_next for the next one; when it says none is waiting, stop."}


async def _report_recipe(session: AsyncSession, owner: str, job: ParseJob, fields, now: datetime) -> dict:
    """A session's recipe for a recorded site: run against the recording (every problem named at once; the job stays
    open for another try, `recipe_record.WRITE_TRIES` in all), then kept as the site's next recipe, live, or, when
    the session said none can be made, the reason kept on the recording."""
    from app.jobsearch.browser.recipes import record as recipe_record  # it loads the engine's browser code

    outcome, problems = await recipe_record.report(session, owner, job, fields, now)
    if outcome == "again":
        error = ParseError("; ".join(problems))
        error.how_to_fix = "fix those and send jobs.parse_report again with the same id"
        raise error
    job.finished_at, job.text = now, ""
    if outcome == "failed":
        job.state, job.error = FAILED, "No recipe reproduced the recording."
    else:
        job.state = DONE
    await session.flush()
    return {"ok": True, "taken": True, "state": job.state,
            "next": "Call jobs.parse_next for the next one; when it says none is waiting, stop."}


def is_sonnet(job: ParseJob) -> bool:
    """Whether this parse job is one Sonnet runs (a recipe to write, the master's claims)."""
    return job.kind in (RECIPE.value, CLAIMS.value)


async def _report_claims(session: AsyncSession, owner: str, job: ParseJob, fields, now: datetime) -> dict:
    """A session's claims for a master version: checked against that version's citable lines (every problem named at
    once, the job stays open for another try), kept as the version's claims. The master is
    only read. A report with no claim in it fails the job."""
    try:
        made = await cleansed_master.report(session, owner, job, fields)
    except cleansed_master.ProblemsError as e:
        error = ParseError(str(e))
        error.how_to_fix = "fix those and send jobs.parse_report again with the same id"
        raise error from e
    if made is None:
        _fail(job, "The master resume this was made from is gone.", now)
        await session.flush()
        raise ParseError(ENDED)
    version, claims = made
    job.finished_at, job.text = now, ""
    if not claims:
        job.state, job.error = FAILED, "The agent found no claim in the master resume it could use."
    else:
        job.state, job.fields = DONE, {"version": version, "claims": len(claims)}
    await session.flush()
    return {"ok": True, "taken": True, "state": job.state,
            "next": "Call jobs.parse_next for the next one; when it says none is waiting, stop."}


async def _report_structure(session: AsyncSession, owner: str, job: ParseJob, fields, now: datetime) -> dict:
    """A session's items for a posting's text: each checked against that text (every problem named at once, the job
    stays open for another try), numbered by code, kept beside the fields code filled as the record of that text
    version. Shadow only: the role is not touched. A report with nothing in it fails the job."""
    try:
        role = await session.get(Opportunity, uuid.UUID(job.role_id or ""))
    except ValueError:
        role = None
    if role is None or role.owner != owner:
        _fail(job, "The role this was read for is gone.", now)
        await session.flush()
        raise ParseError(ENDED)
    text, partial = job.text, bool((job.fields or {}).get("partial"))
    fp = posting_structure.fingerprint(text)
    clean, problems = posting_structure.check(
        fields, text, await posting_structure.previous_fields(session, owner, role.id, fp))
    if problems:
        error = ParseError("; ".join(problems))
        error.how_to_fix = "fix those and send jobs.parse_report again with the same id"
        raise error
    job.finished_at, job.text = now, ""
    if posting_structure.is_empty(clean):
        job.state, job.error = FAILED, "The agent found nothing in the posting it could use."
    else:
        record = posting_structure.build(text, clean, role.title, partial)
        await posting_structure.keep(session, owner, role.id, fp, record)
        job.state, job.fields = DONE, {"partial": partial, "fields": sorted(record.fields)}
    await session.flush()
    return {"ok": True, "taken": True, "state": job.state,
            "next": "Call jobs.parse_next for the next one; when it says none is waiting, stop."}
