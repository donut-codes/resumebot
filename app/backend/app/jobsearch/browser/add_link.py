"""The owner adds a job by pasting its link (docs/PRD.md 24.10): a posting is made in the log, as the scout's finds
are, and it waits in Added links. It is no role until the steps that make a role of any posting have run.

The app checks that the text is a public web address, cleans it the way every posting's address is cleaned
(`browser_reader.canonical`: https, no `www.`, no fragment, no tracking parameters), and asks whether the owner already
has that job. It has it when a role, or a posting in the log (28.8), carries the same cleaned address or the same job
id on the same host (`same_role.marks`: a followed board's job page and the same job open in a list are one job); a
job board's host is folded to the board's name for the id (`same_role.board_marks`). The company is not asked,
because nothing is known of it yet. A job that is there is answered as it is, with where it stands, and nothing is made.

A link to a followed board's page of results or recommendations with one job open is stored as that job's
own page (`link_text._single_job`): a list names no title or employer to search by.

A new job is a posting whose system is `owner` (`ats.OWNER`), with the link as its address, not yet filtered. Adding
is database work only and answers at once (the owner, 2026-11-16): the app reads no page here, and no agent is waited
on. The posting is named by its link and the employer "Added link" (`models.ADDED_LINK`), and waits in Added links for
the owner's browser reader, whose read plan lists it (`browser_reader.to_read_added`, priority 0) and which names it
with `browser.texts` (`browser_reader._name_added`, which uses `name_posting` below). Until it is named no title check
is handed it (`titles.unnamed`): a link is no title to judge. One log line says it was added (the posting and the
link's host only).

The posting is then put through the code gates at once (`sift.sift` with `only`), the first step every posting meets,
so it shows where it stands: Added links when the gates pass, Gated out with the gate's reason when one stops it. From
there it goes the way a scouted posting goes: the title check, a reader, a role, sourcing (an employer's own link
sources at once in code, `sourcing.source_in_code`), the text, then the analysis. Nothing is decided here about whether
the job is a fit, and no target role is chosen by hand: the title check names it. Services flush; the route commits.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import ats, stages
from app.jobsearch.browser import link_held
from app.jobsearch.browser.link_held import already_on, posting_answer, role_answer
from app.jobsearch.browser.link_text import read_link
from app.jobsearch.browser.reader import canonical
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.gating import sift
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import ORIGIN_OWNER, JobPosting, Opportunity
from app.jobsearch.pipeline import board
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.postings import dates as posted_dates
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.postings import whole
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis


async def _adopt(session: AsyncSession, owner: str, role: Opportunity, address: str) -> dict:
    """The owner pasted the link of a job they have as a role. A role in Gated out is brought back first, as the owner
    moving it out of Gated out by hand does (`board.move`: its stage, its by-hand mark, its Activity line; the gate's
    history stays), and the answer names the gate that had stopped it (PRD 24.10). Then, when the role is in play and
    not Sourced, and the link is the employer's own posting (`sourcing.source_given`), the link becomes its source,
    the role is the owner's now (origin and research ask), and its research is started ahead of the line. Otherwise
    the answer says where it is."""
    brought = ""
    if role.status == stages.GATED_OUT:
        gate = role.screened_out_reason
        await board.move(session, owner, role.id, stages.FIRST_IMPRESSION)
        brought = (f"{role.title} at {role.company} was brought back from Gated out and is in "
                   f"{stages.LABELS.get(role.status, role.status)}.")
        if gate and gate.strip():  # the gate's sentence stays whole, as the Activity line gives it
            brought += f" It had been Gated out: {gate.strip().rstrip('.')}."
    if role.sourced_at is not None or role.status not in sourcing.IN_PLAY:
        return role_answer(role, said=brought) if brought else already_on(role)
    # A link the role already holds is, when it is on a job system the app knows, that system's own posting for it,
    # whatever shape the system gives it: the owner pasting it again says it is the source.
    key = canonical(address)[0]
    held = any(link_held.is_it([link], key, frozenset(), frozenset()) for link in (role.url, role.apply_url))
    if not await sourcing.source_given(session, role, address, trust_system=held):
        return role_answer(role, said=brought) if brought else already_on(role)
    role.origin = ORIGIN_OWNER
    role.analysis = {**(role.analysis or {}), analysis.ASKED_BY: analysis.ASKED_BY_OWNER}
    if role.analysis_state != analysis.WAITING:
        await analysis.start(session, owner, role, by_owner=True)
    await session.flush()
    where = brought or (f"{role.title} at {role.company} is already on your pipeline, in "
                        f"{stages.LABELS.get(role.status, role.status)}.")
    return role_answer(role, said=f"{where} Your link is its source now, and its research goes ahead of the others.")


def _typed(column: str, value: Any) -> Any:
    """A page fact as its column holds it: `posted` a date, the pay figures whole numbers, the rest as given. A value
    that does not read as its type is None, so it is left off rather than failing the save."""
    if column == "posted":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip()[:10])
        except ValueError:
            return None
    if column in ("comp_min", "comp_max"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return value


def name_posting(posting: JobPosting, facts: dict, now: datetime) -> bool:
    """Put what a page says of the job on its posting, when it names it: both the title and the employer, or nothing
    (a half name would send a placeholder to the title check). The text is kept only when it is a whole posting
    (`whole.why_not`, `too_long`). Returns whether the posting is now named."""
    title, company = " ".join(str(facts.get("title") or "").split()), " ".join(str(facts.get("company") or "").split())
    if not (title and company):
        return False
    posting.title, posting.company, posting.match_key = title[:500], company[:200], match_key(company, title)
    for column in ("location", "work_mode", "comp", "comp_min", "comp_max", "posted"):
        if facts.get(column) and not getattr(posting, column):
            value = _typed(column, facts[column])
            if value is not None:
                setattr(posting, column, value)
    text = (facts.get("text") or "").strip()
    if (text and not (posting.text or "").strip() and whole.why_not(text) is None
            and whole.too_long(text, "text") is None):
        posting.text, posting.detail_at = text, now
    if posting.posted is None:
        posting.posted = posted_dates.day_of(text or posting.text, now)
    return True


async def _put_through(session: AsyncSession, owner: str, posting: JobPosting, now: datetime, *, already: bool,
                       make_role: bool = False) -> dict:
    """The first step every posting meets, now: the code gates (the night's run does the rest, or the owner's Wake up).
    With `make_role` (a posting the scout held that the owner has now asked for by its link) and the gates passed, the
    role is made of it at once from what is stored, the owner's own and asked for research ahead of the line. Its text,
    if the posting has none, is read later by the browser queue (`to_read_in_full`, `to_read_at_source`), never here."""
    await sift.sift(session, owner, await profile_store.current(session, owner), now, only=[posting.id])
    if make_role:
        await role_from_posting.make_roles(session, owner, only=[posting.id], owners_own=True)
        role = await session.get(Opportunity, posting.opportunity_id) if posting.opportunity_id else None
        if role is not None:
            return role_answer(role, said=(
                f"{role.title} at {role.company} is in {stages.LABELS.get(role.status, role.status)}: you asked for "
                "it, so it goes through the gates and the research ahead of the others."))
    return posting_answer(posting, already=already)


async def add(session: AsyncSession, owner: str, text: object, posting_id: str | None = None) -> dict:
    """Add the job at the pasted link, or answer that the owner has it. Returns `{role, posting, already, said, stage,
    stage_label, title, company, sourced}`: `already` says nothing was made, and `role` (or, for a posting that is no
    role yet, `posting`) holds the job; a new job is a posting in Added links (or Gated out, with the gate's reason).
    Raises `LinkError` for what is not a link the app can use. With `posting_id`, the page has already made the posting
    of this link (`link_held.make_posting`) and told the owner; it is only put through the gates here."""
    if posting_id is not None:
        made = await session.get(JobPosting, uuid.UUID(posting_id))
        if made is not None and made.owner == owner:
            return await _put_through(session, owner, made, datetime.now(UTC), already=False)
    key, address = read_link(text)
    boards = await employer_posting.job_board_hosts(session, owner)
    in_role, loose = await link_held.held(session, owner, key, address, boards)
    there = await session.get(Opportunity, in_role) if in_role is not None else None
    if there is not None:
        return await _adopt(session, owner, there, address)
    if loose is not None:
        # The scout's posting with no role yet goes the owner's way.
        if loose.ats != ats.OWNER and loose.is_open and not loose.filtered_by:
            return await _put_through(session, owner, loose, datetime.now(UTC), already=True, make_role=True)
        return posting_answer(loose, already=True)
    posting, made = await link_held.make_posting(session, owner, key, address)
    if not made:
        return posting_answer(posting, already=True)
    return await _put_through(session, owner, posting, datetime.now(UTC), already=False)
