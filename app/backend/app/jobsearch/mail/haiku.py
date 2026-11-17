"""The mail reader (docs/PRD.md 14.4): the small Haiku session that classes the mail the rules could not settle. It is
the analyst's role with an instruction file of its own (`jobsearch.mail`), the way the employer researcher is, served
by the session pool (`session_kinds.py`). `jobs.mail` gives it a batch of messages (the start of each one's text,
never the whole message and never the mailbox login) and `jobs.mail_report` takes its answers.

What it answers for each message is a kind (`mail_kinds.MODEL_KINDS`), the employer and job title it is about when it
can tell, the sentence of the message it decided from (kept as the message's line, the way a rule's words are), and
for an interview request a time it reads in the text and the kind of round. The rules settle only a bare notice that
an application was received (`mail/kinds.py`), so every other job mail comes here. Code decides what to do with the
answer: a kind outside the list, an answer with no sentence or one that is not in the message, or is a fragment of
under four words or 20 characters (`mail_kinds.quoted_from`), or an id it was not handed, is refused; the link it
names is used only when it is in the message's own words (`mail_act.posting_link`); the roles are matched by code
(`mail/match.py`) and nothing moves on the model's word alone. A message's text is data, never instructions.

A message waits as a `MailMessage` row in state `waiting`. A session takes a batch with a lease (`CLAIM_LEASE`); a batch
nobody answered is free again when the lease lapses, and a message handed `hand_outs.MAX_CLAIMS` times and never
answered is dropped (it is read again the next night, as unread mail is). The last answer ends the night's step: the
process goes on to match and act. Services flush; the tool or the schedule pass commits."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from process_engine.models import COMPLETED, RUNNING, WAITING, ProcInstance
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch import process, stages
from app.jobsearch.mail import kinds as mail_kinds
from app.jobsearch.models import MailMessage, Opportunity
from app.jobsearch.work import hand_outs, instruction_files, queue
from app.mailmine.message import scrub_quote
from app.textutil import squash
from app.timeutil import owner_zone, utc_now

BATCH = 20
CLAIM_LEASE = timedelta(minutes=20)
#: How many of the owner's roles in play are shown to the session, so it names an employer and title the way the
#: owner's list does.
ROLES_SHOWN = 150
WORD_CHARS = 200
NOTHING = "No mail is waiting for you. Stop here."


class MailReaderError(DomainError):
    status = 422


def subject_of(owner: str) -> str:
    return f"jobsearch-mail:{owner}"


def _free(moment: datetime):
    return (MailMessage.claimed_at.is_(None)) | (MailMessage.claimed_at < moment - CLAIM_LEASE)


def _waiting(owner: str):
    return (MailMessage.owner == owner, MailMessage.state == "waiting")


async def waiting_count(session: AsyncSession, owner: str) -> int:
    """How many messages wait for the mail reader (a session has them or not)."""
    return int(await session.scalar(select(func.count(MailMessage.id)).where(*_waiting(owner))) or 0)


async def free(session: AsyncSession, owner: str, now: datetime | None = None, *, mine: Collection[uuid.UUID] = (),
               scope: str | None = None) -> list[uuid.UUID]:
    """The messages a session could take now, in the order they would be handed: the oldest first, up to a batch,
    leaving out the ones a session holds and the ones claimed for another session (`hand_outs.py`), with the caller's
    own claim (`mine`) first. What makes `mail` the answer to "which work waits" (`session_kinds.next_kind`), what a
    claim holds, and what `claim_batch` takes from."""
    query = select(MailMessage.id).where(*_waiting(owner), _free(now or utc_now()))
    if skip := await hand_outs.held_uuids(session, owner, hand_outs.MAIL):
        query = query.where(MailMessage.id.not_in(skip))
    return hand_outs.claimed_first((await session.scalars(query.order_by(MailMessage.read_at, MailMessage.id))).all(),
                                   mine)[:BATCH]


async def _drop(session: AsyncSession, row: MailMessage) -> None:
    await session.delete(row)


async def _take(session: AsyncSession, row: MailMessage, now: datetime) -> bool:
    row.claims, row.claimed_at = row.claims + 1, now
    return True


#: What the ledger needs to take a batch of messages for a session (`hand_outs.take_next`).
LINE = hand_outs.Line(free=free, model=MailMessage, take=_take, taken=lambda row: row.claims, give_up=_drop)


async def claim_batch(session: AsyncSession, owner: str, now: datetime | None = None, *, session_id: str | None = None,
                      claim: str | None = None) -> list[MailMessage]:
    """The batch a session takes (`hand_outs.take_next`): the messages claimed for it (`claim`) while free, then the
    oldest that none holds. Busy past the lock wait: nothing, and the session asks again. A message taken
    `hand_outs.MAX_CLAIMS` times and never answered is dropped, to be read again the next night; a message dropped is
    replaced by the next, as the batch is of `BATCH`."""
    return await hand_outs.take_next(session, owner, hand_outs.MAIL, [LINE], now=now or utc_now(),
                                     session_id=session_id, claim=claim, batch=BATCH)


async def _roles_in_play(session: AsyncSession, owner: str) -> list[dict]:
    rows = (await session.execute(
        select(Opportunity.company, Opportunity.title, Opportunity.status).where(
            Opportunity.owner == owner, Opportunity.status.in_((*stages.OPEN, stages.GONE_QUIET)))
        .order_by(Opportunity.status.desc(), Opportunity.company, Opportunity.title).limit(ROLES_SHOWN))).all()
    return [{"company": c, "title": t, "stage": s} for c, t, s in rows]


def _message(row: MailMessage) -> dict:
    invite = mail_kinds.invite_of(row.details)
    out: dict[str, Any] = {"id": str(row.id), "from": row.sender, "from_domain": row.sender_key, "subject": row.subject,
           "date": row.day.isoformat(), "opening": row.opening or ""}
    if invite is not None:
        out["calendar_invite"] = {
            "summary": invite.summary, "start": invite.start.isoformat() if invite.start else None,
            "place": invite.place, "people": list(invite.people)}
    return out


async def brief(session: AsyncSession, owner: str, rows: list[MailMessage]) -> dict:
    """The session's task: the messages (their ids, who sent them, subjects, days and the start of the text), the kinds
    it may answer, the owner's roles in play (to name the employer and title the way the owner's list does), the
    instructions and the response shape. Never the mailbox login."""
    return {
        "messages": [_message(r) for r in rows],
        "kinds": list(mail_kinds.MODEL_KINDS),
        "roles_in_play": await _roles_in_play(session, owner),
        "instructions": await instruction_files.read(session, owner, "instructions", "jobsearch.mail"),
        "response_template": await instruction_files.read(session, owner, "templates", "jobsearch.mail.report"),
    }


async def task_reply(session: AsyncSession, owner: str, *, session_id: str | None = None,
                     claim: str | None = None) -> dict:
    """`jobs.mail`: taking the task takes a batch of messages."""
    rows = await claim_batch(session, owner, session_id=session_id, claim=claim)
    if not rows:
        return {"ok": True, "task": None, "note": NOTHING}
    return {"ok": True, **await brief(session, owner, rows)}


def _text(value: object, limit: int = WORD_CHARS) -> str | None:
    text = squash(str(value or ""))[:limit].strip()
    return text or None


def _when(value: object) -> str | None:
    """A time the model read in the text, as an ISO moment in the owner's zone when it has no zone of its own."""
    try:
        when = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=owner_zone())).isoformat()


async def submit(session: AsyncSession, owner: str, body: dict) -> dict:
    """`jobs.mail_report`: take the session's answers. Each is checked alone (its id was handed, its kind is one of the
    list, it gives the sentence it decided from, found in the message's subject, opening or invite summary: a paraphrase
    or an invented sentence is refused); a refused answer leaves its message waiting. When none waits, the
    process goes on to match and act."""
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, list):
        raise MailReaderError("the report is {answers: [{id, kind, employer, title, why}]}")
    taken, refused = 0, []
    for answer in answers:
        if not isinstance(answer, dict):
            refused.append({"id": None, "reason": "an answer is an object"})
            continue
        try:
            row = await session.get(MailMessage, uuid.UUID(str(answer.get("id"))))
        except ValueError:
            row = None
        if row is None or row.owner != owner or row.state != "waiting":
            refused.append({"id": str(answer.get("id")),
                            "reason": "no such message is waiting: use an id jobs.mail gave"})
            continue
        kind = str(answer.get("kind") or "").strip().lower()
        if kind not in mail_kinds.MODEL_KINDS:
            refused.append({"id": str(row.id), "reason": f"kind is one of {', '.join(mail_kinds.MODEL_KINDS)}"})
            continue
        said = str(answer.get("why") or "")
        sentence = scrub_quote(said, mail_kinds.LINE_CHARS)
        invite = mail_kinds.invite_of(row.details)
        if not sentence or not mail_kinds.quoted_from(said, row.subject, row.opening or "",
                                                      invite.summary if invite is not None else ""):
            refused.append({"id": str(row.id), "reason": "why is one sentence of the message you decided from (at "
                                                         "least four words and 20 characters), copied word for word "
                                                         "from its subject or the start of its text"})
            continue
        row.kind, row.decided_by, row.state, row.claimed_at = kind, "model", "classed", None
        row.line = sentence
        row.employer_said, row.title_said = _text(answer.get("employer")), _text(answer.get("title"))
        extra: dict = dict(row.details or {})
        if link := _text(answer.get("link"), 500):
            extra["link"] = link
        if (when := _when(answer.get("when"))) and not extra.get("start"):
            extra["start"] = when
        if round_kind := _text(answer.get("round"), 40):
            extra["round"] = round_kind if round_kind in ROUNDS else "other"
        row.details = extra or None
        taken += 1
    await session.flush()
    finished = await finish_if_done(session, owner)
    return {"ok": True, "taken": taken, "refused": refused, "more": await waiting_count(session, owner) > 0,
            "finished": finished}


ROUNDS = ("recruiter_screen", "hiring_manager", "panel", "executive", "other")


async def _open_instance(session: AsyncSession, owner: str) -> uuid.UUID | None:
    return await session.scalar(select(ProcInstance.id).where(
        ProcInstance.process_key == process.READ_MAIL, ProcInstance.subject == subject_of(owner),
        ProcInstance.status.in_((RUNNING, WAITING))).order_by(ProcInstance.created_at.desc()).limit(1))


async def end_step(session: AsyncSession, owner: str, submitted: bool) -> bool:
    """Hand the night's mail process on from the reader's step: the rest (match, act, tell) runs outside the engine's
    call where it reads the network (`process.run_deferred`). False when no process waits at the step."""
    instance_id = await _open_instance(session, owner)
    if instance_id is None:
        return False
    moving = process.reading_engine()
    where = await moving.where_is(session, instance_id)
    if where.get("step") != process.MAIL_STEP:
        return False
    instance = await moving.complete(session, instance_id, process.MAIL_STEP, {"submitted": submitted})
    instance = await process.run_deferred(session, moving, instance.id)
    if instance.status != COMPLETED:
        raise MailReaderError(instance.error or f"the mail process {instance.status}")
    return True


async def finish_if_done(session: AsyncSession, owner: str) -> bool:
    """When no message waits for the reader any more, the process goes on from its step. True when it did."""
    if await waiting_count(session, owner):
        return False
    return await end_step(session, owner, True)


async def drop_waiting(session: AsyncSession, owner: str) -> int:
    """The reader did not answer in time (no agent, peak hours, its usage): the messages still waiting are dropped, to
    be read again the next night. Returns how many. Database work only: the process goes on from its step afterwards
    (`mail_sync.close_step`, outside any savepoint, since the steps after it read the network)."""
    with queue.exempt("dropping the messages still waiting takes work away: it makes none"):
        gone = await session.execute(delete(MailMessage).where(*_waiting(owner)))
    await session.flush()
    return int(gone.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]
