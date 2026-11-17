"""Read the mail (docs/PRD.md path 14): the owner's mailbox read read-only, the job mail in it classed, matched to roles
and acted on, so the board stays honest without the owner logging every reply by hand.

It is the mission "Read the mail" (`record.READ_MAIL`) and runs as the process `playbooks/jobsearch/read_mail.bpmn`:

1. **Read the mailbox** (code, network): the mail since the last read that looks like job mail, asked of the server and
   not downloaded (`go_mail`'s reader, derived from another project's code: log in, list, examine, search and peek-only
   fetch, so no message is changed, moved or marked read), minus what was handled (a message is read once, by its id).
   Only the start of its text is read, and a calendar invite (`.ics`) is the one attachment that is opened.
2. **Settle by rules** (code, `mail_kinds.classify`): only a bare notice that an application was received (every
   sentence of it a fixed received or a fixed harmless one, naming no company or job) is classed with no model, as a
   confirmation. The rules make no
   rejection, offer or interview.
3. **The mail reader reads the rest** (a small Haiku session, `mail/haiku.py`): only the start of the text of what the
   rules did not settle, and each answer gives the sentence it comes from. Nothing else of the mailbox, and never its
   login.
4. **Match to roles** (code, `mail/match.py`): by employer name or alias and title, when the mail came from that
   employer's own address or a known job system; two roles that fit equally, and mail from anyone else that names an
   employer, are never guessed.
5. **Act** (code, `mail/act.py`): move a role, add one or ask, each with a line in the role's history.
6. **Tell the owner**: the run's report, what needs the owner first.

The reading and the moving are code and never held for peak hours; only the model's step waits for the window to end
(the owner's peak-hours setting holds every model session). No sending, no replies, no drafts, and nothing written to
the mailbox. The login is `mail_login.find` (the saved one, else the install's `.env`); it goes to the mail reader and
nowhere else: not to a model, a log, a run or a reply.

The window: the first read looks back `FIRST_DAYS`; later ones look back the owner's days (`email_lookback_days`,
`DEFAULT_DAYS` when blank) or to the last good read, whichever is further, and never past `MOST_DAYS`. A night the
reader did not answer
in time (`MODEL_WAIT`) drops what it had not read, to be read again, and the next window reaches back to it.

Services flush; the step or the schedule pass commits."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
import uuid
from collections import Counter
from datetime import date, datetime, timedelta

from process_engine.engine import Engine
from process_engine.errors import ActionError
from process_engine.models import COMPLETED, RUNNING, WAITING, ProcInstance
from process_engine.registry import actions as registry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import off_turn, process
from app.jobsearch.mail import act as mail_act
from app.jobsearch.mail import haiku as mail_haiku
from app.jobsearch.mail import kinds as mail_kinds
from app.jobsearch.mail import login as mail_login
from app.jobsearch.mail import match as mail_match
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.missions import record
from app.jobsearch.models import MailMessage, MissionRun
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import checkpoint, engine_calls, stuck
from app.mailmine import imap, message
from app.mailmine.message import Seen
from app.timeutil import aware, owner_today, owner_zone

log = logging.getLogger(__name__)

#: How far back the first read goes, the owner's days when they have set none, and the furthest any read reaches.
FIRST_DAYS, DEFAULT_DAYS, MOST_DAYS = 14, 3, 60
#: How long the night's process waits for the mail reader before it goes on without it.
MODEL_WAIT = timedelta(hours=12)
#: The words the server is asked for (a server matches part of a word, so a stem finds its forms).
STEMS = ("applying", "application", "applied", "interview", "candidate", "offer", "recruit", "opportunity", "position")
#: The most messages whose text is read in one run, and the longest the mailbox may take (seconds).
MOST_MESSAGES = 400
READ_SECONDS = 240.0
NO_MAILBOX = "No mailbox is connected: add one under Agent Settings, Your mailbox."

_tasks: set[asyncio.Task] = set()


def message_key(seen: Seen) -> str:
    """The key a message is read once by: its own id, else a digest of who, what and when."""
    if seen.message_id:
        return seen.message_id[:300]
    return "x:" + hashlib.sha256(f"{seen.address}|{seen.subject}|{seen.day}".encode()).hexdigest()[:40]


async def window_start(session: AsyncSession, owner: str, mission_id: uuid.UUID, today: date) -> date:
    """The first day the next read looks at (see the module's docstring)."""
    profile = await profile_store.current(session, owner)
    asked = profile.settings.email_lookback_days
    lookback = min(asked, MOST_DAYS) if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0 \
        else DEFAULT_DAYS
    last = (await session.scalars(select(MissionRun).where(
        MissionRun.mission_id == mission_id, MissionRun.outcome == "done").order_by(MissionRun.started_at.desc())
        .limit(1))).first()
    read = _day((last.started or {}).get("read_day")) if last is not None else None
    if read is None:
        days = FIRST_DAYS
    else:
        days = max(lookback, (today - read).days + 1)
    since = today - timedelta(days=min(days, MOST_DAYS))
    retry = _day((last.started or {}).get("retry_from")) if last is not None else None
    return min(since, retry) if retry is not None and retry >= today - timedelta(days=MOST_DAYS) else since


def _day(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


# --- reading the mailbox (blocking) ---------------------------------------------------------------------------------


def read_new(who: go_mail.Login, since: date, handled: set[str], employer_keys: list[str]) -> list[Seen]:
    """The job mail in the mailbox since `since` that was not handled: the headers are searched and filtered, then the
    start of the text of what is worth reading is fetched. Blocking (run it in a thread); raises `imap.MailReadError` in
    plain words. Read-only: the reader sends only the commands its guard allows."""
    wanted: dict[str, list[int]] = {}
    keys: set[str] = set()
    count = 0

    def header(raw: bytes, ref: imap.Ref | None) -> None:
        nonlocal count
        seen = message.from_bytes(raw)
        if seen is None or ref is None or seen.day < since or count >= MOST_MESSAGES:
            return
        key = message_key(seen)
        if key in handled or key in keys or not mail_kinds.worth_reading(seen, employer_keys):
            return
        keys.add(key)
        wanted.setdefault(ref[0], []).append(ref[1])
        count += 1

    out: dict[str, Seen] = {}

    def body(raw: bytes, _ref: imap.Ref | None) -> None:
        seen = message.from_bytes(raw)
        if seen is not None:
            out.setdefault(message_key(seen), seen)

    with go_mail.open_inbox(who) as inbox:
        inbox.read_headers(since, header, stems=STEMS)
        if wanted:
            inbox.read_bodies(wanted, body)
    return list(out.values())


# --- the process's steps (playbooks/jobsearch/read_mail.bpmn) ---------------------------------------------------------


def _row(owner: str, seen: Seen, who: go_mail.Login) -> MailMessage:
    invite = mail_kinds.parse_invite(seen.calendar, owner_zone(), exclude=frozenset({who.username.lower()})) \
        if seen.calendar else None
    return MailMessage(
        owner=owner, message_key=message_key(seen), sender=seen.sender_name[:200], sender_key=seen.sender_key[:320],
        subject=seen.subject[:mail_kinds.SUBJECT_CHARS], day=seen.day, state="read",
        opening=seen.text[:mail_kinds.OPENING_CHARS],
        details=mail_kinds.invite_view(invite) if invite is not None else None)


async def action_read(session: AsyncSession, instance: ProcInstance, data: dict) -> dict:
    """Read the mailbox, read-only, and keep one row for each new job message (its sender, subject, day and the start of
    its text, which is dropped once the message is acted on)."""
    owner = data["owner"]
    # the login, the handled keys and the roles in play are only read, and used only for the mailbox read: in a reader's
    # session where this one holds nothing, so the writer's turn is not taken for them
    async with off_turn.reading(session) as reader:
        who = await mail_login.find(reader, owner)
        if who is None:
            raise ActionError("no_mailbox", NO_MAILBOX)
        since = _day(data.get("since")) or owner_today() - timedelta(days=DEFAULT_DAYS)
        handled = set((await reader.scalars(select(MailMessage.message_key).where(
            MailMessage.owner == owner))).all())
        keys = mail_match.all_keys(await mail_match.candidates_of(reader, owner))
    await db.release_if_free(session)  # the mailbox may take minutes: no transaction is held while it answers
    try:
        async with asyncio.timeout(READ_SECONDS):
            found = await asyncio.to_thread(read_new, who, since, handled, keys)
    except imap.MailReadError as exc:
        raise ActionError("mail_unreadable", str(exc)) from None
    except TimeoutError:
        raise ActionError("mail_slow", "The mailbox took too long to answer. It is read again next time.") from None
    for seen in found:
        session.add(_row(owner, seen, who))
    await session.flush()
    return {"new": len(found)}


async def settle_rows(session: AsyncSession, owner: str) -> tuple[int, int]:
    """Class the rows just read by rules (`mail_kinds.classify`); the rest wait for the mail reader. Returns (classed by
    rules, waiting for the reader), the waiting count including any a night before left."""
    rows = (await session.scalars(select(MailMessage).where(
        MailMessage.owner == owner, MailMessage.state == "read"))).all()
    classed = 0
    for row in rows:
        invite = mail_kinds.invite_of(row.details)
        seen = mail_act.seen_of(row)
        verdict = mail_kinds.classify(seen, invite)
        if verdict is None:
            row.state = "waiting"
            continue
        row.kind, row.line, row.decided_by, row.state = verdict.kind, verdict.line[:mail_kinds.LINE_CHARS], "rules", \
            "classed"
        classed += 1
    await session.flush()
    return classed, await mail_haiku.waiting_count(session, owner)


async def action_settle(session: AsyncSession, instance: ProcInstance, data: dict) -> dict:
    classed, waiting = await settle_rows(session, data["owner"])
    return {"rules": classed, "waiting": waiting}


async def match_rows(session: AsyncSession, owner: str) -> dict:
    """Match each classed message to a role: the role when exactly one is meant (`row.role_id`), else the roles that fit
    equally (`row.details["candidates"]`), or the roles a message from neither their employer nor a job system names
    (the same, marked `row.details["unverified"]`: the owner decides, nothing moves). A confirmation or a rejection that
    names no job title is matched only to a role it may act on (`mail_match.can_take`). Returns the counts."""
    rows = (await session.scalars(select(MailMessage).where(
        MailMessage.owner == owner, MailMessage.state == "classed", MailMessage.outcome.is_(None)))).all()
    candidates = await mail_match.candidates_of(session, owner)
    counts: Counter[str] = Counter()
    for row in rows:
        if row.kind in (mail_kinds.OTHER, mail_kinds.OUTREACH):
            continue
        found = mail_match.match(mail_act.seen_of(row), candidates, row.kind)
        if found.role is not None:
            row.role_id = found.role.id
            counts["matched"] += 1
        elif found.candidates:
            row.details = {**(row.details or {}), "candidates": [str(r.id) for r in found.candidates],
                           **({"unverified": True} if found.unverified else {})}
            counts["asked"] += 1
        else:
            counts["unmatched"] += 1
    await session.flush()
    return dict(counts)


async def action_match(session: AsyncSession, instance: ProcInstance, data: dict) -> dict:
    counts = await match_rows(session, data["owner"])
    return {"matched": counts.get("matched", 0), "asked": counts.get("asked", 0),
            "unmatched": counts.get("unmatched", 0)}


async def act_rows(session: AsyncSession, owner: str) -> dict:
    """Carry each classed, matched message out and drop what was kept of its text. Returns what the report says: the
    interview requests and offers (with the times), the roles moved or added, and the decisions asked."""
    rows = (await session.scalars(select(MailMessage).where(
        MailMessage.owner == owner, MailMessage.state == "classed", MailMessage.outcome.is_(None))
        .order_by(MailMessage.day, MailMessage.read_at))).all()
    out: dict[str, list[str]] = {"needs_you": [], "moved": [], "added": [], "asked": []}
    outcomes: list[str] = []
    for row in rows:
        role_id = row.role_id
        outcome = await mail_act.act_on(session, owner, row)
        words = await _said(session, row, outcome, role_id)  # before what was kept of the message is dropped
        row.outcome, row.state, row.opening = outcome, "acted", None
        if outcome != mail_act.ASKED:
            row.details = None
        outcomes.append(outcome)
        await session.flush()
        if words is not None:
            out[words[0]].append(words[1])
        await checkpoint.reached()
    await mail_act.settle(session, owner, outcomes)
    return out


async def _said(session: AsyncSession, row: MailMessage, outcome: str,
                role_id: uuid.UUID | None) -> tuple[str, str] | None:
    """Which line of the report a message earns, and its words."""
    from app.jobsearch.models import Opportunity

    role = await session.get(Opportunity, row.role_id or role_id) if (row.role_id or role_id) else None
    named = f"{role.title} at {role.company}" if role is not None else row.subject
    if outcome == mail_act.ASKED:
        return "asked", f"{row.sender}: {row.subject}"
    if outcome == mail_act.ADDED:
        return "added", named
    if row.kind == mail_kinds.INTERVIEW and outcome in (mail_act.MOVED, mail_act.TOLD) and role is not None:
        _, said = mail_act._when(row)  # pyright: ignore[reportPrivateUsage]
        when = f": {said}" if said else ""
        return "needs_you", f"Interview request from {role.company}{when} ({role.title})"
    if row.kind == mail_kinds.OFFER and role is not None:
        return "needs_you", f"An offer from {role.company} ({role.title})"
    if outcome == mail_act.MOVED and role is not None:
        verb = "Rejected" if row.kind == mail_kinds.REJECTION else "Applied"
        return "moved", f"{named}: {verb}"
    return None


async def action_act(session: AsyncSession, instance: ProcInstance, data: dict) -> dict:
    return await act_rows(session, data["owner"])


def report_of(said: dict) -> str:
    """The run's report (docs/PRD.md 14.8): what needs the owner first (interview requests and offers, with dates), then
    the roles moved, the roles added and the mail waiting for a call. No count of mail read; a run with nothing to say
    says so in one line."""
    lines: list[str] = list(said.get("needs_you") or [])
    for kind, words in (("moved", "Moved"), ("added", "Added")):
        lines += [f"{words}: {w}." for w in said.get(kind) or []]
    asked = said.get("asked") or []
    if asked:
        lines.append(f"{len(asked)} mail{' is' if len(asked) == 1 else 's are'} waiting for your call in the Inbox.")
    quiet_after = said.get("went_quiet")
    if quiet_after:
        lines.append(str(quiet_after))
    return "\n".join(lines) if lines else "No mail needed anything."


async def action_tell(session: AsyncSession, instance: ProcInstance, data: dict) -> dict:
    """The report: assembled from what the steps before said."""
    acted = data.get("acted") or {}
    waited = data.get("settled") or {}
    report = report_of(acted)
    if data.get("found") and data["found"].get("submitted") is False:
        report += "\nSome mail was not read by the mail reader in time: it is read again next time."
    return {"report": report, "rules": int(waited.get("rules") or 0)}


def _register() -> None:
    source = "backend/app/jobsearch/mail/sync.py"
    registry.register(
        "mail.read", action_read, inputs=["owner", "since"], outputs=["new"],
        description="Reads the owner's mailbox read-only (the server is asked for job mail since the last read; "
                    "nothing is changed, moved or marked read) and keeps a row for each new job message: who sent it, "
                    "its subject and day, and the start of its text until it is acted on.", source=source)
    registry.register(
        "mail.settle", action_settle, inputs=["owner"], outputs=["rules", "waiting"],
        description="Classes only a bare received notice in code, as a confirmation; every other message waits "
                    "for the mail reader.", source=source)
    registry.register(
        "mail.match", action_match, inputs=["owner"], outputs=["matched", "asked", "unmatched"],
        description="Matches each classed message to a role by employer name or alias and title, only when it came "
                    "from that employer's own address or a known job system; two roles that fit equally, a "
                    "confirmation or rejection that names no job title and fits no single role it may act on, or mail "
                    "from anyone else that names the employer, are never guessed: the owner decides.", source=source)
    registry.register(
        "mail.act", action_act, inputs=["owner"], outputs=["needs_you", "moved", "added", "asked"],
        description="Carries each message out: an application confirmation moves its role to Applied, a rejection to "
                    "Rejected, an interview request to Active Discussion (and adds the interview), an offer tells "
                    "the owner, a recruiter's posting link goes through the add-a-job path. Each move writes a line "
                    "to the role's history.", source=source)
    registry.register(
        "mail.tell", action_tell, inputs=["owner"], outputs=["report", "rules"],
        description="Writes the run's report: what needs the owner first, then the roles moved and added and the mail "
                    "waiting for a call.", source=source)


_register()


# --- running it (the driver; the mission starts it) -------------------------------------------------------------------


def _subject(owner: str) -> str:
    return mail_haiku.subject_of(owner)


async def end_unfinished(session: AsyncSession, owner: str, why: str) -> int:
    """Close the process of a read that never ended (the app restarted in it). Returns how many were closed."""
    open_ones = (await session.scalars(select(ProcInstance.id).where(
        ProcInstance.process_key == process.READ_MAIL, ProcInstance.subject == _subject(owner),
        ProcInstance.status.in_((RUNNING, WAITING))))).all()
    for instance_id in open_ones:
        await stuck.cancel_instance(session, instance_id, why, "the read of the mail")
    await session.flush()
    return len(open_ones)


async def _run_step(engine: Engine, instance_id: uuid.UUID) -> str:
    """Do the step the process waits at, in a session of its own, and commit it with the move to the next. `ran`;
    `waits` when it is at the mail reader's step (nobody here runs it); `ended` when the process is over."""
    step = None
    try:
        async with db.session_factory()() as session:
            where = await engine.where_is(session, instance_id)
            if where["status"] not in (RUNNING, WAITING):
                return "ended"
            waits = [w for w in where["waiting_for"] if w["kind"] == "deferred"]
            if not waits:
                return "waits"
            step = waits[0]["step"]
            action = engine.registry.get(waits[0]["payload"]["action"])
            instance = await session.get(ProcInstance, instance_id)
            if action is None or instance is None:
                raise RuntimeError(f"the mail read's step '{step}' has no action to run")
            try:
                async with checkpoint.saving_as_it_goes(session):
                    result = await action.fn(session, instance, copy.deepcopy(instance.data or {}))
            except ActionError as error:
                await session.rollback()
                await engine.complete(session, instance_id, step, error=error.code, error_message=error.message)
            else:
                await engine.complete(session, instance_id, step, result)
            await session.commit()
            return "ran"
    except Exception as exc:
        if step is not None:
            try:
                async with db.session_factory()() as session:
                    await engine.complete(session, instance_id, step, error="step_failed",
                                          error_message=f"{type(exc).__name__}: {exc}"[:300])
                    await session.commit()
            except Exception:
                log.exception("couldn't mark the mail read failed")
        raise


async def drive(owner: str, since: date, entry_id: uuid.UUID | None = None) -> uuid.UUID:
    """Start the process and run its steps until it waits for the mail reader or ends. Returns the instance's id."""
    engine = process.stepped_engine()
    async with db.session_factory()() as session:
        await end_unfinished(session, owner, "a later read of the mail began")
        instance = await engine.start(session, process.READ_MAIL, {"owner": owner, "since": since.isoformat()},
                                      subject=_subject(owner))
        if entry_id is not None:
            run = await session.get(MissionRun, entry_id)
            if run is not None:
                run.started = {**(run.started or {}), "instance": str(instance.id), "instances": [str(instance.id)]}
        await session.commit()
        instance_id = instance.id
    while await _run_step(engine, instance_id) == "ran":
        pass
    return instance_id


def failure_words(error: str | None) -> str:
    """What a failed process says, in the owner's words: the step's own sentence, never the machinery around it."""
    found = re.search(r"raised \w+: (.*)$", error or "")
    return (found.group(1) if found else (error or "it stopped")).strip()[:400]


async def settle_run(session: AsyncSession, entry: MissionRun) -> bool:
    """End a mail run whose process is over (the mission's refresh calls it). True when ended."""
    started = dict(entry.started or {})
    if not started.get("instance"):
        return False
    instance = await session.get(ProcInstance, uuid.UUID(started["instance"]))
    if instance is None:
        record.end(entry, "failed", "The read of the mail was lost: its process is gone.")
        return True
    data = dict(instance.data or {})
    if instance.status == COMPLETED:
        report = str((data.get("told") or {}).get("report") or "No mail needed anything.")
        entry.started = {**started, "waiting": False,
                         **({"retry_from": started["retry_from"]} if started.get("retry_from") else {})}
        record.end(entry, "done", report)
        return True
    if instance.status not in (RUNNING, WAITING):
        record.end(entry, "failed", f"The mail couldn't be read: {failure_words(instance.error)}")
        return True
    return False


async def give_up_waiting(session: AsyncSession, entry: MissionRun, now: datetime) -> bool:
    """The mail reader had `MODEL_WAIT` and did not finish: drop what it had not read (read again next time, the next
    window reaches back to it) and mark the run, so that `tend` lets the process go on with what is settled. Database
    work only (the schedule pass calls it inside a savepoint). True when it did."""
    began = aware(entry.started_at)
    started = dict(entry.started or {})
    if not started.get("instance") or started.get("gave_up") or began is None or now - began < MODEL_WAIT:
        return False
    oldest = await session.scalar(select(func.min(MailMessage.day)).where(
        MailMessage.owner == entry.owner, MailMessage.state == "waiting"))
    dropped = await mail_haiku.drop_waiting(session, entry.owner)
    entry.started = {**started, "gave_up": True,
                     **({"retry_from": oldest.isoformat()} if dropped and oldest is not None else {})}
    return True


async def tend(session: AsyncSession, entry: MissionRun) -> bool:
    """What a read still going needs from the schedule pass: the reader's step ended without it (`give_up_waiting`) goes
    on in the background; a run whose steps of code were cut short by a restart is taken up again. True when something
    was started."""
    started = entry.started or {}
    if _tasks or not started.get("instance"):
        return False
    if started.get("gave_up"):
        start_close_in_background(entry.id, entry.owner)
        return True
    return await resume_if_cut(session, entry)


@db.background_work
async def _close(entry_id: uuid.UUID, owner: str) -> None:
    """The background task after the reader's time ran out: the process goes on from its step with what is settled, then
    the run is closed if the process is over."""
    try:
        async with db.session_factory()() as session:
            await mail_haiku.end_step(session, owner, False)
            await session.commit()
    except Exception as exc:
        log.exception("going on without the mail reader failed")
        async with db.session_factory()() as session:
            entry = await session.get(MissionRun, entry_id)
            if entry is not None and entry.outcome == "running":
                record.end(entry, "failed", f"The mail couldn't be read: {exc}"[:500])
                await session.commit()
        return
    async with db.session_factory()() as session:
        entry = await session.get(MissionRun, entry_id)
        if entry is not None and entry.outcome == "running":
            await settle_run(session, entry)
            await session.commit()


def _spawn_close(key: tuple[uuid.UUID, str]) -> None:
    """What `engine_calls.start("mail_close", (entry_id, owner))` runs where the local door is installed: the task,
    kept in `_tasks`."""
    entry_id, owner = key
    task = asyncio.get_running_loop().create_task(_close(entry_id, owner))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


engine_calls.register("mail_close", _spawn_close)


def start_close_in_background(entry_id: uuid.UUID, owner: str) -> None:
    engine_calls.start("mail_close", (entry_id, owner))


async def resume_if_cut(session: AsyncSession, entry: MissionRun) -> bool:
    """A read whose steps of code were cut short (the app restarted): the process waits at one of them and nothing
    here is running it, so it is taken up again where it stopped. True when it was."""
    started = entry.started or {}
    if _tasks or not started.get("instance"):
        return False
    engine = process.stepped_engine()
    where = await engine.where_is(session, uuid.UUID(started["instance"]))
    if where["status"] not in (RUNNING, WAITING) or not any(w["kind"] == "deferred" for w in where["waiting_for"]):
        return False
    start_in_background(entry.id, entry.owner, _day(started.get("since")) or owner_today(), resume=True)
    return True


@db.background_work
async def _finish(entry_id: uuid.UUID, owner: str, since: date, resume: bool = False) -> None:
    """The background task: run the process as far as it goes, then close the run if it is over (else the mail reader's
    answer ends it, and the mission's refresh closes the run)."""
    async with db.session_factory()() as session:
        entry = await session.get(MissionRun, entry_id)
    if entry is None or entry.outcome != "running":
        return
    try:
        if resume:
            engine = process.stepped_engine()
            while await _run_step(engine, uuid.UUID((entry.started or {})["instance"])) == "ran":
                pass
        else:
            await drive(owner, since, entry_id)
    except Exception as exc:  # the run must end with a reason, whatever went wrong
        log.exception("reading the mail failed")
        async with db.session_factory()() as session:
            entry = await session.get(MissionRun, entry_id)
            if entry is not None and entry.outcome == "running":
                record.end(entry, "failed", f"The mail couldn't be read: {exc}"[:500])
                await session.commit()
        return
    async with db.session_factory()() as session:
        entry = await session.get(MissionRun, entry_id)
        if entry is not None and entry.outcome == "running":
            await settle_run(session, entry)
            await session.commit()


def _spawn_read(key: tuple[uuid.UUID, str, date, bool]) -> None:
    """What `engine_calls.start("mail_read", (entry_id, owner, since, resume))` runs where the local door is installed:
    the task, kept in `_tasks`."""
    entry_id, owner, since, resume = key
    task = asyncio.get_running_loop().create_task(_finish(entry_id, owner, since, resume))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


engine_calls.register("mail_read", _spawn_read)


def start_in_background(entry_id: uuid.UUID, owner: str, since: date, resume: bool = False) -> None:
    engine_calls.start("mail_read", (entry_id, owner, since, resume))


async def stop_all() -> None:
    """At shutdown: cancel the reads in flight (their runs are closed by the refresh after the restart)."""
    import contextlib

    for task in list(_tasks):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
