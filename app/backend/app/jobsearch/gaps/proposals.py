"""Proposals: a suggestion to change a document that the owner accepts, edits or rejects (docs/PRD.md 8.3's minimal
shape, first built for Find Gaps, 38.15 and 38.16).

A proposal is one row of `js_proposals`: who suggested it (`coach.gaps`), the document (the master resume), where it
goes (a job, and whether it is a new line or evidence on one of that job's existing lines), the words, why, and the
owner's answer it came from. Its `key` (the document, the part and the folded words) is unique for the owner, so a
suggestion already stored, whatever its status, is not made again: a rejected one is kept for that.

Accepting saves a new version of the master through the resume's own save (`profile_store.save`, 2.2), with the line
added to its job as a Bullet (a new line) or an Evidence entry titled for the line it backs, and a history line that
says the Coach suggested it from the owner's answer and the owner accepted it (and that they edited it, when they did).
The words are the owner's: the app never rewords the master, so what is saved is the proposal's words, which the checks
held to be the owner's answer cut at its ends (`is_cut`), or the owner's own edit. Rejecting saves nothing.
Services flush; the route commits.
"""

import hashlib
import re
import unicodedata
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import actors
from app.jobsearch.gaps.runs import GapError, NoSuchRun
from app.jobsearch.models import Proposal
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.documents import Resume
from app.jobsearch.states import ProposalStatus

#: Who saved a version this makes, and what a proposal's source is. A suggestion from the owner's own Claude (the
#: owner door's `resume.propose`, docs/PRD.md 50.15) is `SOURCE_DOOR`; the owner's Accept saves it, so `by` is the
#: owner.
BY = "Coach (Find Gaps)"
SOURCE = "coach.gaps"
SOURCE_DOOR = "owner.claude"
NEW_LINE, EVIDENCE = "new_line", "evidence"
KINDS = (NEW_LINE, EVIDENCE)
NO_SUCH = "That suggestion is gone."
DECIDED = "That suggestion was already decided."
JOB_GONE = "The job this line goes under is no longer in your master resume, so it cannot be added there."
BLANK = "The line is empty: write the words to add, or reject it."

_PUNCT = " \t\r\n.,;:!?-–—\"'()[]"
_QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"'}


def fold(text: str) -> str:
    """Words as compared for a cut and for a key: NFKC, case folded, curly quotes straight, spaces collapsed and the
    punctuation at both ends cut off."""
    out = unicodedata.normalize("NFKC", text or "")
    for curly, straight in _QUOTES.items():
        out = out.replace(curly, straight)
    return " ".join(out.casefold().split()).strip(_PUNCT)


def is_cut(words: str, answer: str) -> bool:
    """Whether `words` are the owner's `answer` cut at its ends and nothing else: found whole inside it, compared with
    the case, the spaces and the punctuation at the ends set aside (docs/PRD.md 38.15). For a suggestion from the
    owner's Claude (`SOURCE_DOOR`) the answer is what Claude says the owner said, which the app cannot check: there
    this is a tidy check only, and the owner's Accept, Edit or Reject is the guard."""
    cut = fold(words)
    return bool(cut) and cut in fold(answer)


def key_of(job_id: str, kind: str, line_id: str | None, text: str) -> str:
    """What makes two suggestions the same one: the document, the job, new line or evidence, the line, the folded
    words."""
    raw = f"resume|{job_id}|{kind}|{line_id or ''}|{fold(text)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _ids(data) -> set[str]:
    """Every id in the resume, at any depth."""
    found: set[str] = set()
    if isinstance(data, dict):
        if isinstance(data.get("id"), str):
            found.add(data["id"])
        for value in data.values():
            found |= _ids(value)
    elif isinstance(data, list):
        for value in data:
            found |= _ids(value)
    return found


def new_id(text: str, taken: set[str]) -> str:
    """An id for a new line: its first words as a slug, made unique among the ids the resume has."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "line"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def job_name(role) -> str:
    """The job as the master names it: "Senior Manager, Northwind Labs"."""
    return ", ".join(x for x in (role.title.strip(), role.employer.strip()) if x) or "A job with no name"


def view(p: Proposal, resume: Resume) -> dict:
    """A proposal for the page: its words (the owner's, once decided), where it goes in the master's words, the answer
    it came from and where it stands."""
    role = next((r for r in resume.roles if r.id == p.part.get("job_id")), None)
    line = next((b for r in resume.roles for b in r.bullets if b.id == p.part.get("line_id")), None)
    return {"id": str(p.id), "theme": p.theme, "text": p.final_text or p.after, "why": p.why, "status": p.status,
            "source": p.source,
            "place": {"kind": p.part.get("kind"), "job": job_name(role) if role is not None else p.part.get("job_id"),
                      "line": line.text if line is not None else None},
            "question": (p.cites or {}).get("question", ""), "quote": (p.cites or {}).get("answer", "")}


async def get(session: AsyncSession, owner: str, proposal_id: str) -> Proposal:
    try:
        key = uuid.UUID(str(proposal_id))
    except ValueError:
        key = None
    row = await session.scalar(select(Proposal).where(Proposal.owner == owner, Proposal.id == key)) if key else None
    if row is None:
        raise NoSuchRun(NO_SUCH)
    return row


async def open_door(session: AsyncSession, owner: str) -> list[Proposal]:
    """The owner's Claude's suggestions still waiting for the owner, oldest first. By source, not by run: a Coach
    proposal whose run was deleted has no run either, and is not this."""
    return list((await session.scalars(select(Proposal).where(
        Proposal.owner == owner, Proposal.source == SOURCE_DOOR,
        Proposal.status == ProposalStatus.PROPOSED.value).order_by(Proposal.created_at, Proposal.id))).all())


async def accept(session: AsyncSession, owner: str, proposal_id: str, text: str | None = None,
                 base_version: int | None = None) -> int | None:
    """Accept a suggestion: save the line into its job as the next version of the master, whose history says the Coach
    suggested it from the owner's answer and the owner accepted it. `text` is the owner's own rewrite of the words,
    saved instead of the Coach's. Refused in words when it was decided already or its job is no longer in the
    master. Returns the new version number; the route shows the master as it now is, read on a reader once this is
    committed. A suggestion from the owner's Claude is saved by the owner (`actors.BY_OWNER`) and says so;
    `base_version` is the master version the page loaded, and a newer one is refused in words
    (`profile_store.StaleSave`)."""
    p = await get(session, owner, proposal_id)
    if p.status != ProposalStatus.PROPOSED:
        raise GapError(DECIDED)
    words = (text if text is not None else p.after).strip()
    if not words:
        raise GapError(BLANK)
    edited = text is not None and words != p.after.strip()
    docs = await profile_store.owner_documents(session, owner)
    data = docs.resume.model_dump(mode="json")
    role = next((r for r in data["roles"] if r["id"] == p.part.get("job_id")), None)
    if role is None:
        raise GapError(JOB_GONE)
    taken = _ids(data)
    if p.part.get("kind") == EVIDENCE:
        line = next((b for b in role["bullets"] if b["id"] == p.part.get("line_id")), None)
        title = f"Evidence for: {line['text'] if line is not None else p.part.get('line_text') or p.theme}"
        role["evidence"].append({"id": new_id(words, taken), "title": title, "text": words})
    else:
        role["bullets"].append({"id": new_id(words, taken), "text": words, "note": "", "group": ""})
    after_edit = " after you edited it" if edited else ""
    if p.source == SOURCE_DOOR:
        why, by = f"Suggested by your Claude from what you told it; accepted by you{after_edit}.", actors.BY_OWNER
    else:
        why = (f'Suggested by the Coach from your answer to a Find Gaps question about "{p.theme}"; accepted by you'
               f"{after_edit}.")
        by = BY
    version = await profile_store.save(session, owner, "resume", data, by=by, why=why, base_version=base_version)
    p.status, p.decided_at, p.version = ProposalStatus.ACCEPTED.value, datetime.now(UTC), version
    p.final_text = words if edited else None
    await session.flush()
    return version


async def reject(session: AsyncSession, owner: str, proposal_id: str) -> Proposal:
    """Reject a suggestion: it is kept as rejected, the master is left as it was, and the same words are not suggested
    again."""
    p = await get(session, owner, proposal_id)
    if p.status != ProposalStatus.PROPOSED:
        raise GapError(DECIDED)
    p.status, p.decided_at = ProposalStatus.REJECTED.value, datetime.now(UTC)
    await session.flush()
    return p
