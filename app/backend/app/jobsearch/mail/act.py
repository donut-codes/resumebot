"""What each kind of mail does (docs/PRD.md 14.6), carried out once a message is classed and matched. Read-only on the
mailbox: nothing here sends, replies, drafts or touches mail; it moves roles, adds a role, writes history and tells the
owner.

- **Application received**: the role moves to Applied when it is earlier than that, or Gone Quiet; one further along
  stays where it is.
- **Rejection**: the role ends Rejected, with the reason "they said no". A role already ended is left.
- **Interview request**: the role moves to Active Discussion (a Gone Quiet one too), and, when the mail gives a time (a
  calendar invite read in code, or the model's reading of the text), an interview is added to the role
  (`interviews.add_from_mail`). The owner is told, with the time.
- **Offer**: the owner is told at once. No move: an offer is part of Active Discussion.
- **Recruiter outreach**: a posting link in the mail goes through the app's own add-a-job path (`add_link.add`) and
  becomes a role found in the usual way; with no link, one note in the Inbox says a recruiter wrote. A role the owner
  already has is not made again.
- **Other**: only the record is kept.

A message that fits several roles equally is never guessed: each candidate's history gets a note and one Inbox
decision lists them (`TASK_KIND`; the answer is carried out by `apply_answer`, from the Inbox route). So is a message
that names an employer's role but came neither from that employer's own address nor from a known job system (a
newsletter, a job alert, a forward: `match.py`), and one that names no job title when it is a confirmation or a
rejection and no single role could be what it moves or ends (`match.can_take`: a confirmation moves only a role the
owner applied to, Launch opened or in Ready to Launch; a rejection ends only one in Applied or Active Discussion, or
Gone Quiet after the owner applied to it): the
same decision, worded for it, and nothing moves or ends until the owner chooses. A message that fits no role of an
employer changes nothing and is kept as read.

Every move writes a line to the role's history with the message's day, sender and subject (the sender and subject
scrubbed of addresses and numbers) and the line that decided it. Services flush; the caller commits."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.inbox import service as inbox
from app.jobsearch import address, ats, stages
from app.jobsearch.interviews import core as interviews
from app.jobsearch.mail import kinds as mail_kinds
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import MailMessage, Opportunity
from app.jobsearch.pipeline import quiet
from app.mailmine.message import Seen, scrub_quote
from app.models.work import Task
from app.timeutil import owner_zone
from app.trail import service as trail

log = logging.getLogger(__name__)

TASK_KIND = "mail_match"
NONE_OF_THEM = "none"
#: What the decision says of a mail that names an employer but came from someone else (`match.py`).
FROM_ELSEWHERE = ("It names the employer, but it came from neither the employer's own address nor a job site's mail "
                  "about your application")
#: What the decision says of a mail that names no job title and fits no single role it could act on (`match.py`).
NO_TITLE = "It names no job title, and it is not clear which of your roles it is about"
#: What came of a message (`MailMessage.outcome`).
MOVED, KEPT, NOTED, ASKED, ADDED, KNOWN, UNMATCHED, TOLD, NOTHING = (
    "moved", "kept", "noted", "asked", "added", "known", "unmatched", "told", "nothing")
APPLIED_EVENT, REJECTED_EVENT, INTERVIEW_EVENT, OFFER_EVENT, UNSURE_EVENT = (
    "mail_applied", "mail_rejected", "mail_interview", "mail_offer", "mail_unsure")
#: The stages a confirmation moves to Applied from, a rejection and an interview move from, and where an interview
#: request leaves a role alone.
BEFORE_APPLIED = (*stages.PRE_APPLY, stages.GONE_QUIET)
STILL_ON = (*stages.OPEN, stages.GONE_QUIET)


def seen_of(row: MailMessage) -> Seen:
    """The message as the matcher reads it: its header facts, and the opening with what the model named."""
    text = " ".join(p for p in (row.opening, row.employer_said, row.title_said) if p)
    return Seen(sender_key=row.sender_key, sender_name=row.sender, subject=row.subject, day=row.day, text=text,
                bulk=False)


def words_of(row: MailMessage) -> str:
    """The history detail every action carries: the message's day, sender and subject, and the line that decided it."""
    return (f'The mail of {row.day:%d %b %Y} from {scrub_quote(row.sender, 80)}: "{scrub_quote(row.subject, 160)}". '
            f"{row.line or ''}").strip()


def _of(role: Opportunity) -> str:
    return f"{role.title} at {role.company}"


def _when(row: MailMessage) -> tuple[datetime | None, str]:
    """The interview time a row carries (the invite's, or the model's reading), and it in words."""
    invite = row.details or {}
    try:
        when = datetime.fromisoformat(invite["start"]) if invite.get("start") else None
    except (TypeError, ValueError):
        when = None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=owner_zone())
    day = None
    if when is None and invite.get("day"):
        try:
            day = datetime.fromisoformat(invite["day"]).date()
        except (TypeError, ValueError):
            day = None
    return when, mail_kinds.when_words(when, day, owner_zone())


async def _note(session: AsyncSession, owner: str, role: Opportunity, row: MailMessage, event: str, sentence: str, *,
                tier: str = "routine") -> None:
    line = await trail.record(session, owner, event=event, sentence=sentence, tier=tier,
                              link=("opportunity", role.id), detail=words_of(row))
    row.trail_event_id = line.id


async def applied(session: AsyncSession, owner: str, role: Opportunity, row: MailMessage) -> str:
    if role.status not in BEFORE_APPLIED:
        return KEPT
    role.status = stages.APPLIED
    await _note(session, owner, role, row, APPLIED_EVENT,
                f"Applied: your mail says {role.company} received your application for {role.title}.", tier="result")
    return MOVED


async def rejected(session: AsyncSession, owner: str, role: Opportunity, row: MailMessage) -> str:
    if role.status not in STILL_ON:
        return KEPT
    line = await quiet.end_role(
        session, owner, role, reason=stages.THEY_SAID_NO, event=REJECTED_EVENT, to=stages.REJECTED,
        sentence=f"Rejected: {role.company} said no by mail for {role.title}.", detail=words_of(row))
    row.trail_event_id = line.id
    return MOVED


async def interview(session: AsyncSession, owner: str, role: Opportunity, row: MailMessage) -> str:
    if role.status not in STILL_ON:
        return KEPT
    when, said = _when(row)
    moved = role.status != stages.ACTIVE_DISCUSSION
    if moved:
        role.status = stages.ACTIVE_DISCUSSION
    await _note(session, owner, role, row, INTERVIEW_EVENT,
                f"{'Active Discussion: ' if moved else ''}{role.company} wrote about an interview"
                f"{f' ({said})' if said else ''}.", tier="needs_you")
    await inbox.notify(
        session, owner, key=f"mail:{row.id}:interview", kind="mail_interview", level="attention",
        title=f"Interview request from {role.company}" + (f": {said}" if said else ""),
        body=f"{_of(role)}. {words_of(row)}", link=("opportunity", role.id))
    if when is not None:
        invite: dict[str, Any] = row.details or {}
        try:
            await interviews.add_from_mail(
                session, owner, role.id, when=when, round_kind=invite.get("round") or "other",
                people=list(invite.get("people") or []), place=invite.get("place") or "", link=invite.get("link") or "",
                source="mail")
        except DomainError as exc:  # a refusal leaves the move and the note as they are
            log.info("an interview from a mail was not added: %s", exc)
    return MOVED if moved else TOLD


async def offered(session: AsyncSession, owner: str, role: Opportunity, row: MailMessage) -> str:
    await _note(session, owner, role, row, OFFER_EVENT, f"{role.company} made an offer in a mail.", tier="needs_you")
    await inbox.notify(session, owner, key=f"mail:{row.id}:offer", kind="mail_offer", level="attention",
                       title=f"An offer from {role.company}", body=f"{_of(role)}. {words_of(row)}",
                       link=("opportunity", role.id))
    return TOLD


EFFECTS = {mail_kinds.CONFIRMATION: applied, mail_kinds.REJECTION: rejected, mail_kinds.INTERVIEW: interview,
           mail_kinds.OFFER: offered}


async def _ask(session: AsyncSession, owner: str, row: MailMessage, roles: list[Opportunity]) -> None:
    """Several roles fit, or the mail names one but came from neither its employer nor a job system (`match.py`): a note
    on each one's history and one Inbox decision listing them (nothing moves)."""
    unverified = bool((row.details or {}).get("unverified"))
    for role in roles:
        await trail.record(
            session, owner, event=UNSURE_EVENT, link=("opportunity", role.id),
            sentence=f"A mail from {scrub_quote(row.sender, 60)} may be about this role: nothing moved.",
            detail=words_of(row) + (f" {FROM_ELSEWHERE}, so you choose." if unverified else
                                    " It fits more than one of your roles, so you choose." if len(roles) > 1 else
                                    f" {NO_TITLE}, so you choose."))
    roles = sorted(roles, key=lambda r: (r.company.lower(), r.title.lower()))
    options = [{"id": str(r.id), "label": _of(r)[:120]} for r in roles] + [
        {"id": NONE_OF_THEM, "label": "None of these"}]
    await inbox.raise_task(
        session, owner, key=f"mail:{row.id}", kind=TASK_KIND,
        title="Is this mail about your role?" if unverified else "Which role is this mail about?",
        detail=f"{words_of(row)}{f' {FROM_ELSEWHERE}.' if unverified else ''}"
               f"{f' {NO_TITLE}.' if len(roles) == 1 and not unverified else ''} Choose the role and it is carried "
               "out: nothing moves until you do.", options=options,
        link=("opportunity", roles[0].id),
        facts=[{"label": "From", "value": scrub_quote(row.sender, 80)},
               {"label": "Subject", "value": scrub_quote(row.subject, 160)},
               {"label": "Kind", "value": row.kind or ""}])


async def apply_answer(session: AsyncSession, owner: str, task: Task, option: str) -> None:
    """The owner chose a role for a mail (the Inbox decision): carry the mail's kind out for it. "None of these" leaves
    everything as it is. The caller commits."""
    row = await session.scalar(select(MailMessage).where(
        MailMessage.owner == owner, MailMessage.id == _uuid(str(task.key).removeprefix("mail:"))))
    if row is None:
        return
    if option != NONE_OF_THEM:
        role = await session.get(Opportunity, _uuid(option))
        effect = EFFECTS.get(row.kind or "")
        if role is not None and role.owner == owner and effect is not None:
            row.role_id = role.id
            row.outcome = await effect(session, owner, role, row)
            await settle(session, owner, [row.outcome or ""])
    else:
        row.outcome = NOTHING
    row.details = None


def _uuid(raw: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return uuid.UUID(int=0)


async def settle(session: AsyncSession, owner: str, outcomes: list[str]) -> None:
    """What follows a role's move: the employer's limit and its watch follow an application beginning or ending (as the
    board's own move does)."""
    if MOVED not in outcomes:
        return
    from app.jobsearch.pipeline import employer_limit
    from app.sites import where

    await employer_limit.settle(session, owner)
    await where.settle_watch(session, owner)


# --- recruiter outreach -------------------------------------------------------------------------------------------


def posting_link(row: MailMessage) -> str | None:
    """The link the model named, kept only when it is in the message's own words (never one the model made up) and
    names a posting on a job system or a followed board. Nothing else is ever opened."""
    link = str((row.details or {}).get("link") or "").strip().rstrip(".,;")
    if not link or link not in (row.opening or ""):
        return None
    if not ((found := ats.parse(link)) and found.read) and not address.followed_board_posting(link):
        return None
    return link


async def outreach(session: AsyncSession, owner: str, row: MailMessage) -> str:
    """A recruiter wrote about a role: one role from its link (once), else one note for the owner."""
    company = " ".join((row.employer_said or "").split())
    title = " ".join((row.title_said or "").split())
    if company and title:
        known = await session.scalar(select(Opportunity.id).where(
            Opportunity.owner == owner, Opportunity.match_key == match_key(company, title)).limit(1))
        if known is not None:
            row.role_id = known
            return KNOWN
    link = posting_link(row)
    if link is not None:
        from app.jobsearch.browser import add_link

        try:
            answer = await add_link.add(session, owner, link)
        except Exception as exc:  # a link the app cannot use or read: the owner is told instead
            log.info("a recruiter's link was not added: %s", type(exc).__name__)
        else:
            row.role_id = _uuid(answer.get("role")) if answer.get("role") else None
            return KNOWN if answer.get("already") else ADDED
    what = f"{title} at {company}" if company and title else (title or company or "a role")
    await inbox.notify(
        session, owner, key=f"mail:{row.id}:outreach", kind="mail_outreach", level="info",
        title=f"A recruiter wrote about {what}", body=f"{words_of(row)} It had no posting link to add the job from.")
    return ASKED


# --- one message ----------------------------------------------------------------------------------------------------


async def act_on(session: AsyncSession, owner: str, row: MailMessage) -> str:
    """Carry out a classed message, matched (`mail_sync.match_rows` set its role or the roles that fit equally). Returns
    what came of it, and the caller sets it on the row."""
    kind = row.kind or mail_kinds.OTHER
    if kind == mail_kinds.OTHER:
        return NOTHING
    if kind == mail_kinds.OUTREACH:
        return await outreach(session, owner, row)
    fits = [_uuid(i) for i in (row.details or {}).get("candidates") or []]
    roles = list((await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.id.in_([row.role_id] if row.role_id else fits)))).all())
    if kind == mail_kinds.AMBIGUOUS:
        if roles:
            await inbox.notify(
                session, owner, key=f"mail:{row.id}:unclear", kind="mail_unclear", level="info",
                title="A mail about a job may need you",
                body=f"{words_of(row)} It could not be told what it says, so nothing changed.",
                link=("opportunity", roles[0].id))
        return ASKED if roles else NOTHING
    if row.role_id is not None and roles:
        return await EFFECTS[kind](session, owner, roles[0], row)
    if roles:
        await _ask(session, owner, row, roles)
        return ASKED
    return UNMATCHED
