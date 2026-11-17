"""What a title session is handed and answers with (docs/PRD.md 30.5): a posting or role shown by its title, employer,
place and the start of its text (`item`), and an answer that names the one target role it matches (`role_for`,
`AnswerError`, `YES`, `NO`, `WHY_CHARS`). The retired title pass is gone: a posting that passes the gates becomes a role
at once (`pipeline/from_posting.py`), so no session judges a title before a reader reads the posting. What is left is
the
one-off tagging's answers (`answer`: each role handed to the tagging job gets the target role it matches, or none,
`tagging.py`) and the words the Check my roles dry run reads (`jobs/check.py`).

This module is data only: it reads roles and the tagging job, and never touches the process. Services flush; the tool
that owns the request commits.
"""

import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import ColumnElement, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import roster
from app.errors import DomainError
from app.jobsearch import ats
from app.jobsearch.models import ADDED_LINK, JobPosting, RoleTagJob
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import hand_outs, tagging

#: What the answers can be.
YES, NO = "yes", "no"
#: The most items one session is handed: it answers them and ends, and the next session takes the next batch (a session
#: handed
#: titles batch after batch runs out of turns and loses what it held). A failed session loses only its batch, which
#: returns when its lease lapses
#: (`hand_outs.ITEM_LEASE_SECONDS`). One batch is a call of jobs.titles_next and one or two of jobs.titles_report, far
#: inside a session's turns.
PER_SESSION = 100
#: What one jobs.titles_next reply is held to: a batch of items with the start of their text (about 330 characters
#: each) and the owner's whole What to look for. Titles cost about two characters a token, so this is about 40,000
#: tokens, inside what the runner lets a session see of one reply (`MAX_MCP_OUTPUT_TOKENS`, 100,000, in
#: app/scout/scout.mjs) and shown whole because the door declares the size (`budget.RESULT_SIZE_META`).
REPLY_CHARS = 80_000
#: How much of a posting's text goes with its title: the start, enough to tell what the job is.
TEXT_CHARS = 200
#: How long the id a session answers with is: the start of the item's id, enough to tell a batch's apart.
ID_CHARS = tagging.ID_CHARS
#: The longest line of why kept.
WHY_CHARS = 240


async def free(session: AsyncSession, owner: str, now: datetime | None = None, *, mine: Collection[uuid.UUID] = (),
               scope: str | None = None) -> list[uuid.UUID]:
    """The roles a title session could be handed now, as their ids, in the order they would be handed: the one-off
    tagging's roles (`scope` "tagging": at most `PER_SESSION`, none while a search is open, and none that another
    session holds on a lease or that are claimed for another, `hand_outs.py`), then the roles whose header lines wait
    for a short read (`scope` "facts": at most `gate_facts.BATCH`); no scope is both. What makes `titles` the answer to
    "which work waits" (`session_kinds.next_kind`) and what a claim holds; the caller's own claim (`mine`) goes first
    in each."""
    from app.jobsearch.gating import gate_facts  # it reads the roles, whose readers read this module

    tagging_ids: list[uuid.UUID] = []
    facts_ids: list[uuid.UUID] = []
    if scope in (None, "tagging") and await roster.open_run(session, owner) is None:
        tagging_ids = [r.id for r in (await tagging.to_tag(session, owner, skip_leased=True, limit=PER_SESSION))]
    if scope in (None, "facts"):
        # Only the ids are used, so the roles are read light (no research, breakdown or posting text).
        waiting = await gate_facts.waiting(session, owner, skip_leased=True, now=now, light=True)
        facts_ids = [r.id for r in waiting[:gate_facts.BATCH]]
    return [i for part in (tagging_ids, facts_ids) for i in hand_outs.claimed_first(part, mine)]


def short_id(posting: JobPosting) -> str:
    return str(posting.id)[:ID_CHARS]


def item(posting: JobPosting) -> dict:
    """One posting as a title session is handed it: what a title is judged by, and nothing more."""
    start = " ".join((posting.text or "").split())[:TEXT_CHARS]
    place = " · ".join(x for x in (posting.location, posting.work_mode) if x)
    out = {"id": short_id(posting), "title": posting.title, "employer": posting.company, "place": place, "text": start}
    return {k: v for k, v in out.items() if v}


def unnamed() -> ColumnElement[bool]:
    """A job the owner added by its link that nothing has named yet (`add_link.py`): its title is the address and its
    employer says "Added link". It waits in Added links, but it has no title to make a role of until the page is read
    and the posting is named (a browser `read` item, `browser_handback.hand_back`, or the app's own read when it was
    added)."""
    return and_(JobPosting.ats == ats.OWNER, JobPosting.company == ADDED_LINK)


class AnswerError(DomainError):
    """An answer that can't be taken, in plain words."""


def _said_none(value) -> bool:
    return str(value or "").strip().lower() in ("", "none", "null", "no", "n/a")


def role_for(key: str, value, role_ids: list[str]) -> str | None:
    """The target role an answer names (docs/PRD.md 30.5): one of the owner's role ids, or None when it names none.
    Raises `AnswerError` for an id that is not one of them."""
    if _said_none(value):
        return None
    said = str(value).strip().lower()
    if said not in role_ids:
        raise AnswerError(f"{key}: role {said!r} is not one of the owner's target roles ({', '.join(role_ids)})")
    return said


async def answer(session: AsyncSession, job: RoleTagJob, raw_answers: list) -> dict:
    """Take a session's answers for the one-off tagging (`tagging.py`): each is `{id, role}` for a role handed to the
    job and still untagged, naming the target role it matches, or none (`answer` no, or a role of `none`). The ones that
    can't be taken are named and the rest are kept (an answer is about one role, and a batch is large). Returns how
    many were taken (`taken`), how many of them got a target role (`tagged`) and the problems."""
    profile = await profile_store.current(session, job.owner)
    role_ids = [r.id for r in profile.criteria.target_roles]
    version = await profile_store.newest_version(session, job.owner, "criteria")
    ledger = tagging.handed(job)
    by_short = {full[:ID_CHARS]: full for full in ledger}
    keys = [str(raw.get("id") or "").strip().lower() for raw in raw_answers if isinstance(raw, dict)]
    pending = await tagging.answerable(session, job, [by_short.get(k) or k for k in keys if k])
    taken = tagged = 0
    problems: list[str] = []
    seen: set[str] = set()
    for raw in raw_answers:
        if not isinstance(raw, dict):
            problems.append("each answer is {id, role}")
            continue
        key = str(raw.get("id") or "").strip().lower()
        if not key:
            problems.append("an answer needs the id it was handed")
            continue
        full = by_short.get(key) or (key if key in ledger else None)
        role = pending.get(full) if full else None
        if role is None or full in seen:
            problems.append(f"{key}: not a posting or role you were handed, or already answered")
            continue
        try:
            matched = None if str(raw.get("answer") or "").strip().lower() == NO else role_for(
                key, raw.get("role"), role_ids)
        except AnswerError as exc:
            problems.append(str(exc))
            continue
        seen.add(str(full))
        tagging.tag(job, role, matched, version)
        taken += 1
        tagged += matched is not None
    await session.flush()
    return {"taken": taken, "tagged": tagged, "problems": problems}
