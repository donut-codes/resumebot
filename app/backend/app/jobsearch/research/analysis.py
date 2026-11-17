"""The analyst's work (docs/PRD.md 23.3): a role is researched before anyone drafts for it (every role that
passes the gates is, once it is Sourced: there is no research bar, 39.17). The step "Source the posting" comes first
(`postings/sourced.py`, the owner, 2026-10-15): a role the app holds no link to the original posting of stays where it
is, unresearched, and is tried again every night; the analyst never sources a role. The app then brings the posting's
text itself, in code and whole, from the source alone (the step "Fetch the posting's text", `posting_text.take`: 39.29),
and sends it to the analyst as it is. The
analyst fills in the employer record, the pay (the posted band, or market pay to model one, and the employer's other
bands), the work location and way of working where the role has none, and how each required and each preferred item
stands against the master resume (Proven, Transferable or Unproven, with the master lines that show it), answers the
owner's sentence rules the role still has no answer to, and may re-judge the score with the whole posting in front of
it. The app then settles the role (`settle.roles`: the ask worked out from the research, then the score, then every
gate again on the facts), and a role a gate now fails goes to Gated out. A role the owner moved by hand (23.8) is
researched like any other but never moved: its stage is theirs.

The analyst is the last and final pass that matches a role to a target role (`to_match`, `match_target_role`, PRD 24.10
and 30.5; the owner, 2026-11-12): a title check or the small title session may have matched the role already, and the
analyst, reading the whole posting, overrules them. It matches in the same findings that research the role: the task
lists every target role, the asks and the gates with the roles each is for, and the findings send `target_role` (an id,
or `none`) and answer the asks of that role. The role then has it before the score is worked out and the gates run, so
its own pay floor, gates and asks apply. A role the analyst has matched is not asked again. The answer is written only
when the findings really give one (an id of the owner's or `none`): findings taken after the refusals ran out without it
leave the role's target role and the pass that set it as they were. A job that matches none keeps the gates and asks for
every role, and its history says so.

The owner can also ask research of one chosen role at any stage, Ended and Gated out included (`ask`, PRD 39.28): it is
put in the analyst's line at once, ahead of every role that was not asked for (`line_order`), through the same process,
claims and one-site rule as the rest. An Ended, Applied, Active Discussion or Gated out role keeps its stage (and a
Gated out role its reason: `keep_gated_out`), and a role in Ready to Launch is gated again, as the catch-up's is
(`GATED_AGAIN`). A role already researched is researched again, the new research
replacing the old, the owner's own corrections of a required item's class with it. The automatic selection (`picks`) is
unchanged.

The process is playbooks/jobsearch/analyse_role.bpmn; its service steps are the `jobs.analysis_*` actions (actions.py),
which call the functions here. The analyst reads its task with jobs.analysis and sends its findings with
jobs.analysis_submit. Services flush; the route or the MCP call commits.
"""

import contextlib
import json
import logging
import uuid
from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from process_engine.models import COMPLETED, OPEN, ProcWait
from process_engine.registry import actions as registry
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.errors import DomainError
from app.jobsearch import as_read, ats, limits, off_turn, process, stages
from app.jobsearch.actors import actor_name
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.fetch import polite
from app.jobsearch.fit import scoring, sibling_bands
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts, stated_place
from app.jobsearch.imports.tracker import slugify
from app.jobsearch.measures.pay_place import match_key, names_a_town, says_no_place
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import record
from app.jobsearch.models import (
    DESCRIPTION_BOARD,
    ORIGIN_OWNER,
    POSTING_NOT_FOUND,
    Employer,
    JobPosting,
    Opportunity,
    SearchRun,
    is_board_copy,
    is_unnamed,
    posting_still_sought,
)
from app.jobsearch.pipeline import placement
from app.jobsearch.postings import cleansed, same_role, unbuilt, whole
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.postings import text as posting_text
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.gates import gate_lines
from app.jobsearch.profile.views import look_for, look_for_role, scoring_for_agent
from app.jobsearch.research import line, prioritize, requirement_lines, role_summary, session_context, settle, staleness
from app.jobsearch.research.line import (
    ASKED_BY,
    ASKED_BY_OWNER,
    DONE,
    FAILED,
    NO_TEXT,
    REANALYSE,
    RUNNER_KEY,
    SESSION_KEY,
    UNREAD_ASKED,
    WAITING,
    asked_by_owner,
    startable,
)
from app.jobsearch.research.line import field as _field
from app.jobsearch.research.line import not_owners as _not_owners
from app.jobsearch.work import hand_outs, instruction_files, queue, stuck, titles
from app.jobsearch.work import hours as night
from app.jobsearch.work.role_match import ANALYST, may_write
from app.sites import avoid
from app.trail import service as trail
from app.web_address import http_url

log = logging.getLogger(__name__)

PARTS = ("task", "resume", "context")
#: A role's states and the keys of its research that say where it stands in the analyst's line live in `line`
#: (`WAITING`, `DONE`, `FAILED`, `NO_TEXT`, `UNREAD_ASKED`, `ASKED_BY`, `REANALYSE`, `READ_NEXT`, `SESSION_KEY`,
#: `RUNNER_KEY`), imported here for this module's own use.
MAX_REFUSALS = 3
#: The lists of a research that hold what the posting is graded by: its requirements, its preferred items and its
#: duties.
GRADED = ("required", "preferred", "responsibilities")
#: How many times findings that graded nothing sent a role back for its text (`send_back`), kept on the role's research,
#: where `wait_for_text` leaves it, and gone with the next research that is taken. A role goes back once
#: (`SENT_BACK_LAPS`): the second such findings stop it where it is (`POSTING_NOT_FOUND`).
SENT_BACK = "sent_back"
SENT_BACK_LAPS = 1
#: What the analyst is told when its findings grade nothing of a text that has no requirement lines (`submit`).
SENT_BACK_REASON = ("taken, nothing kept: the text the app held for this role has no requirement, preferred item or "
                    "duty to grade, so it is not the posting. The role goes back for the posting's text from its "
                    "source and is researched when it comes.")
#: What it is told when the role went back for its text already, and the text that came was graded as nothing again.
NOT_FOUND_REASON = ("taken, nothing kept: the text the app held for this role has no requirement, preferred item or "
                    "duty to grade, and the role went back for its text once already. The app could not find its "
                    "posting at its source: the role waits, and nothing reads it again.")
#: What the analyst is told when it sends findings for a role that holds no text of the posting.
NO_TEXT_REASON = ("not taken: this role holds no text of the posting, and a role is never researched from a summary "
                  "of a page. The posting's text is the app's to fetch from the role's source; nothing is taken.")
#: Where a role's research stops when it is not Sourced (the owner, 2026-10-15): the role stays where it is and is not
#: researched; its sourcing is tried again every night (`sourcing.sweep`, the browser reader).
NOT_SOURCED = "Not sourced yet"
#: A role nobody has taken waits in the analyst's line however long: a night's finds are all put in line at once and
#: a session researches one in about three minutes, so the last of hundreds waits the better part of a day, and that is
#: not a failure. It keeps its place and its owner ask. Only a role a session took (`claim`) can go stuck:
#: `settings.stuck_minutes` runs from then (`settle_stuck`), and `hand_outs.MAX_CLAIMS` takes without a send fail it.
#: How long a role is a session's when the app does not know which session took it (a call that named none), before
#: another may take it instead. A research is about three minutes (45 at the very most, the runner's run limit); a
#: session that has said nothing for this long is gone. It is the outer bound: a session the app knows (`Caller`) holds
#: its role by its heartbeat instead (`SESSION_SILENCE`), so one that was killed does not keep it for the lease.
CLAIM_LEASE = timedelta(minutes=20)
#: How long a role stays with a session the app knows when it has not been heard from. A session says it is alive
#: every minute (`renew_claims`), so one that has been silent this long was killed or crashed, and its role is free for
#: another at once; it also ends the claim when the session says it ended (`release_claims`) or its runner starts again.
SESSION_SILENCE = timedelta(minutes=3)
#: Stamped on every research the analyst's findings make (`submit`): the role has a Second Impression, a line-by-line
#: read made under the ranked deck. Research kept before this existed (the old full analysis of every role) has no
#: stamp and does not count (`counts`): such a role is read again on deck before it can be Ready to Launch.
DECK_READ = "deck_read"
#: What a role looked like before the `requeue` action's scope `ready` marked it (`reanalysis.snapshot`), kept on its
#: analysis through the new research so `reanalysis.compare` can set the two side by side.
BEFORE_KEY = "reanalysis_before"
#: The versions of the inputs in force when the analyst was handed the role (`staleness.stamp_analysis`), kept on the
#: role's analysis until the findings come back: the research is stamped with these, not with what is in force later.
HANDED_UNDER_KEY = "handed_under"
#: The stages past the deck whose gates run again after their research (what `ask` tells the owner who asks for research
#: there; the gates run in `settle.roles`); every other stage past it keeps its stage and its reason, with the research
#: recorded on it.
GATED_AGAIN = (stages.READY_TO_LAUNCH,)
#: What the task says for a job the owner added by its link (`add_link.py`), which has no title or employer until the
#: analyst reads the posting: the findings name it (`Submission.title`, `Submission.company`, `name_added`).
NAME_THE_ROLE = ("The owner added this role by its link, so it has no title or employer yet. Read the posting and send "
                 "`title` and `company` as the posting writes them: the employer's own name, never the job board's. "
                 "Send both or neither.")
#: What the task says for a role the analyst has not yet matched while the owner has target roles: the analyst does, in
#: the findings (`Submission.target_role`, `match_target_role`), and its answer is final.
MATCH_THE_ROLE = ("No analyst has read this role against the owner's target roles yet. A title check may have given it "
                  "one: you may overrule it. `what_to_look_for` holds every target role in full. Decide which one role "
                  "this job matches "
                  "(a job matches one role or none, never a blend) and send its `id` as `target_role`, or `none`. Then "
                  "answer the asks and the gates for that role: `only_for_roles` names the roles one is for, and one "
                  "with none is for every role.")
#: What the task says in place of `MATCH_THE_ROLE` when a lower pass has already given the role a target role: that role
#: is the starting point, its picture and gates are the ones handed, and the analyst confirms it or overrules it.
CONFIRM_THE_ROLE = ("A title check or the small title session matched this role to the target role in "
                    "`what_to_look_for` (its gates are listed). Read the posting against it: confirm it by sending its "
                    "`id` as `target_role`, or overrule it with the `id` of one of `other_target_roles` (in full), or "
                    "`none`. Your answer is final. A job matches one role or none, never a blend. Answer the asks that "
                    "are for the role you send: `only_for_roles` names the roles one is for, and one with none is for "
                    "every role.")
#: What the task says when the posting's text is the board's copy of it, put in place of the employer's because the
#: employer's own posting could not be read (the board's copy hook, the owner, 2026-11-05): the findings are of
#: that text, and say where it limits them.
POSTING_TEXT_FROM_BOARD = (
    "`posting_text_kept` is the board's copy of the posting, not the employer's own words: the employer's own posting "
    "could not be read. Grade it as it stands, and say in `notes` what the board's copy may leave out or change (the "
    "pay, the place, the requirements).")
#: What the task says about the posting's requirement lines, cut and numbered by code (`requirement_lines.py`, 26.12).
REQUIREMENT_LINES_SAY = ("The app cut the posting's requirement sections into these numbered lines, verbatim. "
                         "Answer every line, none skipped, merged, reworded or added: one item in "
                         "`required` (a required line) or "
                         "`preferred` (a preferred one) with `line` as its number, and the class P, T or U, or N when "
                         "the line can't be measured from a resume (with the reason in `why`). The app puts the line's "
                         "own words in the item.")


class AnalysisError(DomainError):
    status = 422


class NoSuchRole(AnalysisError):
    status = 404


class NoSuchItem(AnalysisError):
    status = 404


# --- what the analyst sends ---------------------------------------------------------------------------------------


class Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


#: The most an employer fact may hold. Research is web text the analyst read, stored on the employer record: a number
#: past a 32-bit column was a 500, and an unbounded text or list is a record that never stops growing.
URL_CHARS, PLACE_CHARS, NAME_CHARS = 2000, 300, 200
MOST_OFFICES, MOST_SECTORS, MOST_EVENTS, MOST_SOURCES = 50, 20, 50, 30
EVENT_CHARS, SOURCE_CHARS = 1000, 500
MOST_EMPLOYEES = 10_000_000
FOUNDED_FROM, FOUNDED_TO = 1000, 2100
#: Every line of a posting's requirement sections is answered (docs/PRD.md 26.12), so a list has no small cap.
MOST_REQUIRED, ITEM_CHARS, WHY_CHARS, MOST_LINES = 500, 2000, 600, 30


class EmployerFacts(Loose):
    website: str | None = Field(default=None, max_length=URL_CHARS)
    careers_url: str | None = Field(default=None, max_length=URL_CHARS)
    #: The employer's reviews page, only as a public web search showed it (checked in `submit`).
    reviews_url: str | None = Field(default=None, max_length=URL_CHARS)
    headquarters: str | None = Field(default=None, max_length=PLACE_CHARS)
    offices: list[Annotated[str, Field(max_length=NAME_CHARS)]] | None = Field(default=None, max_length=MOST_OFFICES)
    work_style: Literal["remote", "hybrid", "onsite", "mixed", "unknown"] | None = None
    employees: int | None = Field(default=None, ge=0, le=MOST_EMPLOYEES)
    founded: int | None = Field(default=None, ge=FOUNDED_FROM, le=FOUNDED_TO)
    industry: str | None = Field(default=None, max_length=NAME_CHARS)
    sectors: list[Annotated[str, Field(max_length=NAME_CHARS)]] | None = Field(default=None, max_length=MOST_SECTORS)
    ownership: Literal["public", "private", "pe", "vc", "nonprofit", "government", "unknown"] | None = None
    ai_native: bool | None = None
    events: list[dict] | None = Field(default=None, max_length=MOST_EVENTS)
    #: Where each fact came from: {field: url or words}.
    sources: dict[Annotated[str, Field(max_length=40)], Annotated[str, Field(max_length=SOURCE_CHARS)]] = Field(
        default={}, max_length=MOST_SOURCES)

    @field_validator("website", "careers_url", "reviews_url")
    @classmethod
    def _links_are_web_addresses(cls, link):
        """These are shown as links: a value that is not an http(s) address is left out, as if it had not been sent."""
        return http_url(link)

    @field_validator("events")
    @classmethod
    def _events_are_small(cls, events):
        if events is not None and any(len(json.dumps(e, default=str)) > EVENT_CHARS for e in events):
            raise ValueError(f"an event is at most {EVENT_CHARS} characters")
        return events


class Sibling(Loose):
    name: str
    low: int
    high: int
    market: int | None = None


class Pay(Loose):
    band: list[int] | None = None
    band_source: str = ""
    market_value: int | None = None
    market_sources: list[str] = []
    spread: float | None = None
    siblings: list[Sibling] = []
    note: str = ""


class Required(Loose):
    """One item of the posting's required section, classed against the master resume. A preferred item is classed the
    same way, so `Submission.preferred` holds these too, and so does `Submission.responsibilities` for the posting's
    duties."""

    #: The item's words. For a graded line (`line`) the app puts the line's own words here, so it is left out.
    item: str = Field(default="", max_length=ITEM_CHARS)
    #: P, T or U; or N, "not measurable from a resume", with a reason in `why` and no master lines (26.12).
    cls: Literal["P", "T", "U", "N"] = Field(alias="class")
    #: The number of the posting line this item answers (`requirement_lines`), when the task gave numbered lines.
    line: int | None = None
    why: str = Field(default="", max_length=WHY_CHARS)
    #: The master resume lines (their ids) that show a Proven item or bridge a Transferable one.
    from_: list[Annotated[str, Field(max_length=80)]] = Field(default=[], alias="from", max_length=MOST_LINES)
    #: A required item that is one of the posting's duties standing in for qualifications the posting does not state
    #: (the owner, 2026-10-16): marked so the page can say so. Never set on a stated qualification.
    from_duties: bool = False


class Duty(Required):
    """One duty of the posting, graded like a required item: one per bullet the posting lists, in its words. `group` is
    the heading the posting lists it under ("" when it has none, and for an analysis kept before duties had one)."""

    group: str = Field(default="", max_length=NAME_CHARS)


class Submission(Loose):
    role: str
    #: The title and the employer as the posting writes them, for a role the owner added by its link, which has only the
    #: link until research reads the posting (`name_added`). Kept for no other role, which already has both.
    title: str | None = Field(default=None, max_length=300)
    company: str | None = Field(default=None, max_length=NAME_CHARS)
    #: The target role the job matches (`to_match`, `match_target_role`): the `id` of one of the owner's target roles,
    #: or `none`. Kept only for a role the analyst is asked to match.
    target_role: str | None = Field(default=None, max_length=80)
    employer: EmployerFacts = EmployerFacts()
    pay: Pay = Pay()
    posting_text: str = ""
    #: The work location and the way of working the posting (or the employer) states, kept only where the role has none
    #: (`submit`), as a band is kept only where the role has no pay.
    location: str | None = Field(default=None, max_length=PLACE_CHARS)
    work_mode: Literal["remote", "hybrid", "onsite"] | None = None
    #: True when neither the posting nor the employer's page says where the work is: the answer to the place when the
    #: work location gate is undecided on what the role holds (`stated_place.problem`).
    place_not_stated: bool = False
    required: list[Required] = Field(default=[], max_length=MOST_REQUIRED)
    #: The items of the posting's preferred section, classed the same way as the required ones, with the same rules and
    #: the same cited master lines (the owner, 2026-10-14: preferred qualifications are graded too). A posting with no
    #: preferred section sends none; an analysis kept before this holds none.
    preferred: list[Required] = Field(default=[], max_length=MOST_REQUIRED)
    #: The posting's real duties, in its own words, classed the same way (the owner, 2026-10-16: an employer's screener
    #: matches the whole posting, not only its qualifications). They count in the graded requirements as more required
    #: items, with the same points; the gates never read them. An analysis kept before this holds none.
    #: One item per duty bullet of the posting, so there is no cap on how many.
    responsibilities: list[Duty] = []
    fit_breakdown: dict | None = None
    notes: str = ""
    for_owner: list[str] = []

    @field_validator("work_mode", mode="before")
    @classmethod
    def _work_mode_as_written(cls, mode):
        """A way of working in any case; none, or one that says nothing ("unknown"), is as if it had not been sent."""
        mode = mode.strip().lower() if isinstance(mode, str) else mode
        return None if mode in ("", "unknown") else mode


# --- asking for an analysis ---------------------------------------------------------------------------------------


async def start(session: AsyncSession, owner: str, role: Opportunity, *, by_owner: bool = False) -> Opportunity:
    """Put a role in front of the analyst. One analysis at a time per role. `by_owner` is the owner's own ask (`ask`):
    the role goes first in the line. A restart (any start without `by_owner`) keeps an owner's ask the role already
    carries and never adds one, so a role the owner asked for stays asked for when the app sends it back."""
    if role.analysis_state == WAITING:
        raise AnalysisError("this role is with the analyst already")
    answers = await read_ahead(session, role)
    if answers:  # the session was released for the reads: the role is locked and read again as it is now
        await session.refresh(role)
        if role.analysis_state == WAITING:
            raise AnalysisError("this role is with the analyst already")
    role.analysis_state, role.analysis_asked_at, role.analysis_claimed_at = WAITING, datetime.now(UTC), None
    # A fresh try: no session has taken it yet. The owner's ask stays if the role has one, and is added when the owner
    # asks now; a restart never removes it.
    kept_ask = by_owner or (role.analysis or {}).get(ASKED_BY) == ASKED_BY_OWNER
    fresh = {k: v for k, v in (role.analysis or {}).items() if k not in ("claims", ASKED_BY, REANALYSE)}
    fresh = {**fresh, ASKED_BY: ASKED_BY_OWNER} if kept_ask else fresh
    if fresh != (role.analysis or {}):
        role.analysis = fresh
    await session.flush()
    with polite.read_ahead(answers):  # the process's sourcing and fetch steps take what was read, and read nothing
        instance = await process.engine().start(
            session, process.ANALYSE, {"owner": owner, "role": str(role.id)}, subject=f"jobsearch:analysis:{role.id}")
    role.analysis_instance_id = instance.id
    await session.flush()
    return role


async def read_ahead(session: AsyncSession, role: Opportunity) -> dict:
    """What the analysis process's first steps read from the network for `role` (its employer's boards when it is not
    Sourced, then its source's text: `sourcing.source_in_code`, `posting_text.take`), read now with no transaction
    open, for `polite.read_ahead`. When there is anything to read, `session` is released first (`db.release`): what the
    caller did so far is committed. Empty, and the session left as it is, when nothing is to be read."""
    answers: dict = {}
    source = None  # the link a role not yet Sourced is about to be given by the step before the read
    if role.sourced_at is None:
        boards = await sourcing.read_boards_ahead(session, [role])
        if boards:
            answers[sourcing.BOARDS_AHEAD] = boards
        source = await sourcing.predicted_source(session, role, boards)
        if source is None:
            return answers
    answers.update(await posting_text.ahead(session, role, source))
    return answers


def is_stale(role: Opportunity) -> bool:
    """Whether a role's research is older than its posting's text: done, and the text was read after the research was
    kept (`description_at` after `analysed_at`)."""
    return bool(role.analysis_state == DONE and role.analysed_at and role.description_at
                and role.description_at > role.analysed_at)


def stale():
    """The roles whose research is older than their posting's text (SQL): the one rule of `is_stale`."""
    return and_(Opportunity.analysis_state == DONE, Opportunity.analysed_at.is_not(None),
                Opportunity.description_at.is_not(None), Opportunity.description_at > Opportunity.analysed_at)


async def requeue_if_stale(session: AsyncSession, role: Opportunity) -> bool:
    """Put a role back in front of the analyst when its text was saved after its research was kept: the research was of
    other words. Called wherever a role's text is saved. A role not done, or whose text is not newer, is left alone;
    the old research stays on the role until the new one replaces it. Returns whether it was queued. Services flush;
    the caller commits."""
    if not is_stale(role):
        return False
    await start(session, role.owner, role)
    await trail.record(
        session, role.owner, event="research_requeued", actor="app", link=("opportunity", role.id),
        sentence=f"Queued {role.title} at {role.company} for research again: its posting's text changed after it "
                 "was researched.", detail="The old research stays until the new research replaces it.")
    return True


async def requeue_for_inputs(session: AsyncSession, role: Opportunity, reasons: list[str]) -> None:
    """Put a role back in front of the analyst because an input its research was made from changed
    (`staleness.analysis_reasons`), and say which in its history. The old research stays until the new replaces it, and
    the new is stamped with the versions in force then. Services flush; the caller commits."""
    await start(session, role.owner, role)
    why = "; ".join(reasons)
    await trail.record(
        session, role.owner, event="research_requeued", actor="app", link=("opportunity", role.id),
        sentence=f"Queued {role.title} at {role.company} for research again: an input it was made from changed.",
        detail=f"{why}. The old research stays until the new research replaces it; research that does not depend on "
               "the change is not queued.")


def deck_read_flag():
    """Whether the role's research is a Second Impression (SQL): the analyst's findings stamped it `DECK_READ`. The one
    spelling of the stamp that `reads` reads."""
    return Opportunity.analysis[DECK_READ].as_boolean()


def counts(role: Opportunity) -> bool:
    """Whether the role has a Second Impression: its research was made by the analyst's findings under the ranked deck
    (`DECK_READ`). The old full analysis, kept through migration 0104, does not count."""
    return role.analysis_state == DONE and (role.analysis or {}).get(DECK_READ) is True


async def reads(session: AsyncSession, ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these roles have a Second Impression (`counts`), read without loading their research."""
    if not ids:
        return set()
    return set(await session.scalars(select(Opportunity.id).where(
        db.among(Opportunity.id, ids), Opportunity.analysis_state == DONE, deck_read_flag().is_(True))))


def graded_something():
    """Whether the role's research holds at least one item in what the posting is graded by (SQL: the twin of
    `graded_nothing`, which says the opposite in Python). `reads_graded` and the placement of a Ready to Launch role
    ask it."""
    return or_(*(func.coalesce(_field(part), "[]") != "[]" for part in GRADED))


def graded_read(role: Opportunity) -> bool:
    """Whether the role has a Second Impression that graded something of the posting (`counts`, and `graded_nothing` is
    not true of its research): the only read a Ready to Launch place stands on (docs/PRD.md 24.13). Reads the research,
    so the role is loaded with it."""
    return counts(role) and not graded_nothing(role.analysis)


async def reads_graded(session: AsyncSession, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, bool]:
    """Each of these roles that has a Second Impression (`reads`), and whether that read graded something of the posting
    (`graded_read`), in one statement and without loading the research. A role with no Second Impression is not in the
    answer."""
    if not ids:
        return {}
    return {i: bool(graded) for i, graded in (await session.execute(select(Opportunity.id, graded_something()).where(
        Opportunity.id.in_(ids), Opportunity.analysis_state == DONE, deck_read_flag().is_(True)))).all()}


async def picks(session: AsyncSession, owner: str, limit: int | None,
                source_run: uuid.UUID | None = None) -> list[Opportunity]:
    """The roles the analyst should research next: every one never analysed, up to `limit` (None: all of them).
    There is no research bar (docs/PRD.md 39.17): a role before Ready has passed the gates, a bar is a score, and the
    score is worked out last, after research (39.20), so a bar would put prioritising before research. When a night
    cannot research them all, the roles the owner moved on by hand (23.8: `placement`, a move on the board) go
    first,
    then the rest by the deck's order (`pipeline.order`, the owner, 2026-10-21): the highest Haiku rank first, a stale
    posting (over 30 days, since posted else since found) sorting below every fresher role within 20 points, the younger
    posting on a tie. That is the order of every read, a role marked to be read again (`REANALYSE`) included: its old
    fit no longer orders it. `source_run` narrows them to the finds of one search. This is the automatic selection
    and stays so: a role the owner asks research of (`ask`) is put in line by that ask, at any stage. Only a Sourced
    role is picked (the owner, 2026-10-15: `postings/sourced.py`): one that is not stays where it is and is tried again
    every
    night."""
    query = select(Opportunity).where(Opportunity.owner == owner, startable())  # one rule with the column and the count
    if source_run is not None:  # a search's own finds: never a role marked to be researched again
        query = query.where(Opportunity.source_run == source_run, Opportunity.analysis_state.is_(None))
    roles = list((await session.scalars(query)).all())
    from app.jobsearch.pipeline import deck as pipeline  # the deck's order; the pipeline imports this module

    places = await placement.load(session, owner, roles, placement.Purpose.GATES)
    best = await pipeline.ordered(session, owner, roles)  # the Haiku rank, the stale rule, the younger posting on a tie
    return sorted(best, key=lambda r: not places[r.id].held)[:limit]  # a stable sort: the owner's hand first


# --- the owner asks for research (docs/PRD.md 39.28) ----------------------------------------------------------------


async def ask(session: AsyncSession, owner: str, role_id: uuid.UUID, *, dry_run: bool = False) -> dict:
    """The owner asks for the research of one role, whatever its stage: an Ended or Gated out role too, since what the
    roles that went somewhere or nowhere had in common is what the owner wants to learn. The role is put in the
    analyst's line now, through the same process as every research (`start`), ahead of every role that was not asked
    for (`line_order`); its claims, the one-site rule and the lanes are the ones the rest use. A role with no posting
    link and no posting text is queued too: the analyst looks for the employer's own posting (`UNREAD_REASON`), and
    `POSTING_NOT_READ` covers one that is gone everywhere.

    A role already researched is researched again, the findings replacing the research it holds, the owner's own
    corrections of a required item's class (`correct_class`) with it: the role's history says so. A role whose last
    research failed starts afresh. A role already in the line answers that it is queued, and goes first if the owner had
    not asked for it yet and no session has taken it; asking twice changes nothing the second time. Each ask that
    changes something is a line in the role's history.

    A role at Rejected, Gone Quiet, Withdrew, Applied, Active Discussion or Gated out keeps its stage and its reason,
    and no gate is run on it; a role the owner moved by hand stays where they put it (23.8); a role in Ready to Launch
    (`GATED_AGAIN`) has its gates run again after the research, as it does when the catch-up researches it, and goes to
    Gated out if one now fails, unless the owner moved it by hand (`settle.roles`). Raises `NoSuchRole` for a role there
    is none of.
    With `dry_run` (the action door's, PRD path 50.7) nothing is written and nothing is started: the answer says what
    the ask would do, from the same facts the ask reads.
    Services flush; the route commits and wakes the scout. Returns `{role, state, already, again, said}`."""
    # The write transaction is the one writer: two asks at once (a double click) queue the role once, the second
    # finding it in the line.
    query = select(Opportunity).where(Opportunity.id == role_id, Opportunity.owner == owner)
    role = await session.scalar(query.execution_options(populate_existing=True))
    if role is None:
        raise NoSuchRole("no such role")
    name = f"{role.title} at {role.company}"
    if dry_run:
        return _asked_dry(role, name)
    who = await actor_name(session, owner)
    link = ("opportunity", role.id)
    if role.analysis_state == WAITING:
        said = f"{name} is queued for research already."
        # In line (no session has taken it) but not as the owner's ask: it goes first now. A role a session has taken
        # stays as it is.
        if role.analysis_claimed_at is None and not asked_by_owner(role):
            role.analysis = {**(role.analysis or {}), ASKED_BY: ASKED_BY_OWNER}
            role.analysis_asked_at = datetime.now(UTC)  # the owner's asks go in the order they were made
            await trail.record(session, owner, event="research_asked", actor="person", who=who, link=link,
                               sentence=f"You asked for research of {name}; it goes first in the analyst's line.")
            await session.flush()
            said = f"{name} is queued for research already; it goes first."
        return _asked(role, already=True, again=False, said=said)
    again = role.analysis_state == DONE
    corrected = _owner_corrections(role)
    # A fresh try: the refusals, and the one ask to look again, that an earlier try used are not this one's. A role the
    # app gave up on (`POSTING_NOT_FOUND`) is sought again: the owner's ask is what puts it in line. `SENT_BACK` stays,
    # so findings that grade nothing of the text that comes stop it again at once, with no second lap.
    role.analysis = {k: v for k, v in (role.analysis or {}).items()
                     if k not in ("refused", UNREAD_ASKED, POSTING_NOT_FOUND)}
    await start(session, owner, role, by_owner=True)
    if role.sourced_at is None and role.analysis_state is None:  # the process left it where it was: it is not Sourced
        return _asked(role, already=False, again=again, said=(
            f"{name} is not sourced yet, so it can't be researched: the app holds no link to the original posting. "
            "Its sourcing is tried again every night."))
    if role.analysis_state == NO_TEXT:  # Sourced, but its source's text could not be read: it waits for the text
        return _asked(role, already=False, again=again, said=(
            f"{name} is sourced, but the app can't read the posting's text yet, so it waits for the posting's text: "
            "it is researched when the text comes."))
    notes = []
    label = stages.LABELS.get(role.status, role.status)
    if role.status in GATED_AGAIN:
        notes.append(f"It is in {label} now: the gates run again after the research, and a gate it now fails sends it "
                     "to Gated out, unless you moved it by hand.")
    elif role.status not in stages.PRE_READY:
        notes.append(f"It stays in {label}: research is recorded on a role there and moves it nowhere.")
    if again or corrected:
        yours = "your correction" if corrected == 1 else f"your {corrected} corrections"
        notes.append("The new research replaces the one it has"
                     + (f", and {yours} of a required item's class go with it." if corrected else "."))
    await trail.record(
        session, owner, event="research_asked", actor="person", who=who, link=link,
        sentence=f"You asked for {name} to be researched again." if again else f"You asked for research of {name}.",
        detail=" ".join(notes) or None)
    await session.flush()
    said = (f"{name} is queued to be researched again; the new research replaces the old." if again
            else f"{name} is queued for research.")
    return _asked(role, already=False, again=again, said=said)


def _owner_corrections(role: Opportunity) -> int:
    """How many required items of the research the role holds carry the owner's own class (`correct_class`): the
    findings that replace the research (`submit`) do not keep them."""
    return sum(1 for item in (role.analysis or {}).get("required") or [] if isinstance(item, dict)
               and item.get("by") == "owner")


def _asked_dry(role: Opportunity, name: str) -> dict:
    """What `ask` would do for the role, in the shape it answers in, and nothing done."""
    if role.analysis_state == WAITING:
        first = role.analysis_claimed_at is None and not asked_by_owner(role)
        return _asked(role, already=True, again=False, said=(
            f"{name} is queued for research already" + ("; it would go first." if first else ".")))
    again = role.analysis_state == DONE
    said = f"{name} would be queued to be researched again." if again else f"{name} would be queued for research."
    if role.sourced_at is None:
        said += " It is not sourced yet, so the process would try to source it first."
    return _asked(role, already=False, again=again, said=said)


def _asked(role: Opportunity, *, already: bool, again: bool, said: str) -> dict:
    return {"role": str(role.id), "state": role.analysis_state, "already": already, "again": again, "said": said}


# --- a search's own research (docs/PRD.md 25.13) ------------------------------------------------------------------
#
# When "Analyse the finds" is switched on, the finds of each search go to the
# analyst as soon as the search ends, before the next search starts: the last steps of the search round
# (playbooks/jobsearch/search_round.bpmn), `jobs.plan_analysis` and the analyst's step after it. Each find is researched
# by "Research a role" (analyse_role.bpmn) like any other, and the mission's own settings decide how many (its roles to
# research, -1 for all of them) and when ("paused" skips it). The mission's own run, which follows the search, takes
# whatever this left: finds past the limit, finds of searches that came while it was paused.


async def round_waiting(session: AsyncSession, run_id: uuid.UUID) -> int:
    """How many finds of this search are with the analyst now. A find the owner asked research of is the owner's, not
    the round's: the round does not wait for it."""
    return int(await session.scalar(select(func.count(Opportunity.id)).where(
        Opportunity.source_run == run_id, Opportunity.analysis_state == WAITING, _not_owners())) or 0)


async def plan_round(session: AsyncSession, owner: str, run_id: uuid.UUID) -> dict:
    """Send a search's finds to the analyst: those on deck, Sourced and never researched, the roles the owner moved by
    hand first and then the deck's order (`picks`), up to the mission's roles to research (-1: all). Nothing goes
    when the mission is paused or missing, inside Claude's peak hours (it would be Claude work started then), or when
    the search was cut short (the finish time, Claude's usage limit): its finds wait for the mission's own run.
    Returns `{on, waiting}`."""
    off = {"on": False, "waiting": 0}
    search = await session.get(SearchRun, run_id)
    if search is None or (search.counts or {}).get("stopped"):
        return off
    mission = await missions.standing(session, owner, record.ANALYSE)
    if not mission.on:
        return off
    if night.in_peak((await profile_store.current(session, owner)).settings):
        return off
    target = int(mission.settings["target"])
    roles = await picks(session, owner, None if limits.unlimited(target) else target, source_run=run_id)
    for role in roles:
        await start(session, owner, role)
    return {"on": True, "waiting": len(roles)}


async def action_plan_round(session, instance, data):
    """The round's own research must never cost the round its finds: if sending them fails, what it did is rolled back,
    the failure is logged and the round ends without it (the mission's own run takes them later)."""
    try:
        async with session.begin_nested():
            return await plan_round(session, data["owner"], uuid.UUID(data["run"]))
    except Exception:
        log.exception("sending a search's finds to the analyst failed")
        return {"on": False, "waiting": 0}


async def release_round(session: AsyncSession, run_id: uuid.UUID, why: str) -> int:
    """Send back the finds of a search that are still waiting for the analyst (the night was stopped): each analysis is
    cancelled and the role is in the research queue again, for the next night. A find the owner asked research of stays
    in line: the night's stop is not theirs (a role past Ready would not be picked again). Returns how many."""
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.source_run == run_id, line.Standing.where(line.Standing.FREE, line.Standing.TAKEN,
                                                              owner_ask=False)))).all()
    return len(await line.leave_many(session, roles, line.SWEEP, why=why))


async def settle_rounds(session: AsyncSession, owner: str) -> int:
    """End the research step of every finished search whose finds are no longer with the analyst (researched, failed
    or sent back), so its round, which waits there, is over. Run by the schedule pass, apart from the analysts' own
    transactions. Returns how many it ended."""
    searches = (await session.scalars(
        select(SearchRun).join(ProcWait, ProcWait.instance_id == SearchRun.instance_id)
        .where(SearchRun.owner == owner, SearchRun.status != "open", ProcWait.status == OPEN,
               ProcWait.element_id == process.ROUND_ANALYST_STEP))).all()
    ended = 0
    for search in searches:
        if await round_waiting(session, search.id):
            continue
        researched = int(await session.scalar(select(func.count(Opportunity.id)).where(
            Opportunity.source_run == search.id, Opportunity.analysis_state == DONE)) or 0)
        try:
            async with session.begin_nested():
                await process.engine().complete(session, search.instance_id, process.ROUND_ANALYST_STEP,
                                                {"count": researched})
        except Exception:
            log.exception("couldn't end the research step of search %s", search.id)
            continue
        ended += 1
    return ended


async def waiting(session: AsyncSession, owner: str) -> Opportunity | None:
    """The role first in the analyst's line (`line_order`): one the owner asked for before any other, else the one that
    has waited longest. What the Agents page names when the analyst's session has no role of its own to name
    (`session_kinds.doing`), and the role whose resume is read (`analysis_reply`)."""
    return await session.scalar(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.analysis_state == WAITING)
        .order_by(*line_order()).limit(1))


def line_order() -> tuple:
    """The analyst's line, in order (SQL): the roles the owner asked for first, the earliest ask first, then the rest,
    the one that has waited longest first. The one order of the three places that read the line: where a session is
    handed a role (`_eligible`) and the role the Agents page names for the analyst's session when it has none of its
    own (`waiting`). A row no longer counts the roles ahead of it (docs/PRD.md 24.18)."""
    return case((_field(ASKED_BY) == ASKED_BY_OWNER, 0), else_=1), Opportunity.analysis_asked_at


def _lapsed_before(moment: datetime):
    """The time (SQL) a claim must have been made or renewed after to still be held at `moment`: the session's silence
    for one the app knows the session of, the lease for the rest."""
    return case((_field(SESSION_KEY).is_not(None), moment - SESSION_SILENCE),
                else_=moment - CLAIM_LEASE)


def _free(moment: datetime):
    """The roles with the analyst that no session has right now: never taken, released, or taken by a session that has
    gone quiet (`SESSION_SILENCE`, or the lease when the session is not known)."""
    return or_(Opportunity.analysis_claimed_at.is_(None), Opportunity.analysis_claimed_at < _lapsed_before(moment))


def site_of(url: object, boards: Iterable[str] = ()) -> str | None:
    """The site a link goes to, for keeping research off one site at a time (the owner, 2026-10-13): its host, lowercase
    and without `www.`, which is how the app tells one site's requests from another's (`polite.py`); a job board the app
    knows by name, or one of the owner's (`boards`), is one site whichever country's address a posting is on
    (`uk.board.example` is the board). None for a role with no link."""
    host = avoid.host_of(url)
    return None if host is None else same_role.board_host(host, boards)


async def _eligible(session: AsyncSession, owner: str, moment: datetime,
                    skip: Iterable[uuid.UUID] = ()) -> list[uuid.UUID]:
    """The roles a session could take now, in the line's order (`line_order`: the roles the owner asked for, then the
    one that has waited longest): with the analyst, not another session's (one heard from lately holds its role,
    `claim`), and on no site that a role another session has taken is on. The owner's rule is that research never
    fetches one site in parallel, whatever the number of sessions: many roles' links are the board's, so one session at
    a time is on the board, and the rest go to the roles on other sites; when every role left is on a site in use, none
    is offered and the sessions wait, they do not double up. A role in `skip` (claimed for another session) is left
    out."""
    boards = await employer_posting.job_board_hosts(session, owner)
    in_use = {site_of(url, boards) for url in await session.scalars(select(Opportunity.url).where(
        Opportunity.owner == owner, Opportunity.analysis_state == WAITING,
        Opportunity.analysis_claimed_at.is_not(None), Opportunity.analysis_claimed_at >= _lapsed_before(moment)))}
    # A role whose short Haiku read of its header lines is still to come is not offered yet (`gate_facts.HOLD`): the
    # cheap gates are decided on the posting's facts before anyone is paid to analyse it.
    from app.jobsearch.research import employer as employer_research  # it reads this module's names

    skipped = set(skip) | await gate_facts.held_back_ids(session, owner, moment)
    skipped |= await employer_research.held_back_ids(session, owner, moment)  # its employer's research, a short while
    rows = (await session.execute(select(Opportunity.id, Opportunity.url).where(
        Opportunity.owner == owner, Opportunity.analysis_state == WAITING, _free(moment))
        .order_by(*line_order()))).all()
    return [rid for rid, url in rows if rid not in skipped and ((site := site_of(url, boards)) is None
                                                                or site not in in_use)]


#: What an offer names when it is for a summary alone (`role_summary`), not a role's research.
SUMMARY = "summary"
async def roles(session: AsyncSession, owner: str, now: datetime | None = None, *,
                mine: Collection = (), held: Collection[str] | None = None) -> list[uuid.UUID]:
    """The roles a session could take now, in the order they would be handed: the roles `_eligible` lists (the owner's
    asks first, then the one that has waited longest, with the analyst, on a site no other session is on), leaving out
    the roles claimed for another session (`hand_outs.py`), and the caller's own claim (`mine`) first. `held` is what
    is claimed for another session, when the caller has read it."""
    moment = now or datetime.now(UTC)
    if held is None:
        held = await hand_outs.held(session, owner, hand_outs.ANALYSIS, moment)
    skip: set[uuid.UUID] = set()
    for i in held:
        with contextlib.suppress(ValueError):
            skip.add(uuid.UUID(i))
    return hand_outs.claimed_first(await _eligible(session, owner, moment, skip), mine)


async def free(session: AsyncSession, owner: str, now: datetime | None = None, *, mine: Collection = (),
               scope: str | None = None) -> list[uuid.UUID | str]:
    """What a session could take now, in the order it would be handed: the roles (`roles`), and a summary alone
    (`role_summary`), which is analysis work too, handed out after the research, as `SUMMARY`, unless it is claimed.
    The runner is told there is analysis work only then: a role another session has taken needs no second one, and one
    on a site another session is on waits for it (`session_kinds.next_kind`). What a claim holds, and what `claim_next`
    takes its roles from."""
    moment = now or datetime.now(UTC)
    held = await hand_outs.held(session, owner, hand_outs.ANALYSIS, moment)
    found: list[uuid.UUID | str] = list(await roles(session, owner, moment, mine=mine, held=held))
    if not found and SUMMARY not in held and await role_summary.offerable(session, owner, moment):
        return [SUMMARY]
    return found


def claim(role: Opportunity, now: datetime | None = None, session_id: str | None = None,
          runner: str | None = None) -> None:
    """A session took the role from the analyst's line (`jobs.analysis` handed it over). It is that session's while it
    keeps saying it is alive (`renew_claims`; `SESSION_SILENCE`), or for `CLAIM_LEASE` when the call named no session,
    and the stuck time (`settle_stuck`) runs from now. The times it has been taken are counted. `session_id` and
    `runner` are the session's and its runner's own names (the call's `X-Session-Id` and `X-Runner`)."""
    kept = {k: v for k, v in (role.analysis or {}).items() if k not in (SESSION_KEY, RUNNER_KEY)}
    who = {k: v[:80] for k, v in ((SESSION_KEY, session_id), (RUNNER_KEY, runner)) if v}
    # A runner is named only with a session: a claim by a runner alone could never be renewed, so it holds by the lease.
    if SESSION_KEY not in who:
        who = {}
    role.analysis_claimed_at = now or datetime.now(UTC)
    role.analysis = {**kept, "claims": int(kept.get("claims", 0)) + 1, **who}


async def _stamp_hand_out(session: AsyncSession, owner: str, role: Opportunity) -> None:
    """Keep the versions in force now on the role: each hand-out takes a new stamp (`HANDED_UNDER_KEY`)."""
    role.analysis = {**(role.analysis or {}), HANDED_UNDER_KEY: await staleness.stamp_analysis(session, owner)}


async def held_by(session: AsyncSession, owner: str, session_ids: Iterable[str]) -> dict[str, Opportunity]:
    """The role each of these sessions holds, by session id: the role with the analyst that carries the session's id
    (`claim`). A session that has taken nothing yet, or whose call named no session, holds none that can be told. Of two
    a session took, the later is the one it is on."""
    ids = [s[:80] for s in session_ids if s]
    if not ids:
        return {}
    rows = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.analysis_state == WAITING, _field(SESSION_KEY).in_(ids))
        .order_by(Opportunity.analysis_claimed_at))).all()
    return {str(r.analysis.get(SESSION_KEY)): r for r in rows}


async def renew_claims(session: AsyncSession, owner: str, session_id: str, now: datetime | None = None) -> None:
    """A session says it is alive (its heartbeat, every minute): the roles it took stay its own for another stretch of
    silence (`SESSION_SILENCE`). A role another session has taken since is not renewed, whatever this one believes.
    The caller commits."""
    if not session_id:
        return
    with queue.exempt("a claim is renewed every minute per live session: a post would wake the dispatcher every "
                      "minute for nothing, and silence is the Sweep mission's to notice"):
        await session.execute(update(Opportunity).where(
            Opportunity.owner == owner, Opportunity.analysis_state == WAITING,
            _field(SESSION_KEY) == session_id[:80])
            .values(analysis_claimed_at=now or datetime.now(UTC)).execution_options(synchronize_session=False))


async def _release(session: AsyncSession, owner: str, *conditions) -> int:
    """Free the roles with the analyst that match `conditions`. The times each was taken stay counted, so a role that
    kills its sessions is still given up on (`hand_outs.MAX_CLAIMS`). Returns how many."""
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.analysis_state == WAITING, *conditions)
        .order_by(Opportunity.id).execution_options(populate_existing=True))).all()
    for role in roles:
        role.analysis_claimed_at = None
        role.analysis = {k: v for k, v in (role.analysis or {}).items() if k not in (SESSION_KEY, RUNNER_KEY)}
    await session.flush()
    return len(roles)


async def release_claims(session: AsyncSession, owner: str, session_id: str) -> int:
    """Let go of the roles a session took, now: it ended without sending them (its heartbeat says so). They are free for
    another session at once, not when the silence would have been noticed. Returns how many. The caller commits."""
    return await _release(session, owner, _field(SESSION_KEY) == session_id[:80]) if session_id else 0


def runner_name(runner: str, boot: str) -> str:
    """A runner as a claim names it: its own name, and which start of it this is, so a session of the runner's start
    now is never taken for one of an earlier start (`X-Runner`, and the runner's boot report)."""
    return f"{runner[:40]}/{boot[:30]}"


async def release_runner(session: AsyncSession, owner: str, runner: str, boot: str) -> int:
    """A runner has started (`boot`): every session of an earlier start of it was killed with it (a restart, a deploy),
    so the roles they took are free at once. The sessions of this start, which may have taken a role before the report
    arrived, are left as they are. Returns how many. The caller commits."""
    if not runner or not boot:
        return 0
    return await _release(session, owner, _field(RUNNER_KEY).startswith(f"{runner[:40]}/", autoescape=True),
                          _field(RUNNER_KEY) != runner_name(runner, boot))


async def _give_up(session: AsyncSession, owner: str, role: Opportunity) -> None:
    """Close a research nobody sent: the process is cancelled, the role is failed and says so in its history."""
    await stuck.cancel_instance(session, role.analysis_instance_id, "the analyst didn't send it",
                                f"the analysis of role {role.id}")
    role.analysis_state = FAILED
    await trail.record(session, owner, event="analysis_failed", actor="agent", agent_slug="scout", who="Analyst",
                       sentence=f"The analyst didn't finish researching {role.title} at {role.company}.",
                       link=("opportunity", role.id))


async def claim_next(session: AsyncSession, owner: str, now: datetime | None = None, *,
                     session_id: str | None = None, runner: str | None = None,
                     claim_id: str | None = None) -> Opportunity | None:
    """The first role in the analyst's line (`line_order`) that no session has, on a site no other session is on
    (`_eligible`), taken by the session that asks (`hand_outs.take_next`), or None. Sessions that ask at the same moment
    get different roles, and never two on one site. A role whose session died is free again as soon as its session is
    known to be gone (`SESSION_SILENCE`, `release_claims`; else when its lease ends); one taken `hand_outs.MAX_CLAIMS`
    times and never sent is failed instead of handed over again (no session manages to finish it). Busy past the lock
    wait: None, and the session asks again.

    A session started for a hand-out (`claim_id`, `hand_outs.py`) takes the role claimed for it, and never one claimed
    for another session: with the claimed role gone it takes the first that no claim holds (`roles`)."""

    async def give_up(session: AsyncSession, role: Opportunity) -> None:
        rid = role.id
        try:
            async with session.begin_nested():
                await _give_up(session, owner, role)
        except Exception:  # a role that can't be closed must not keep the others from their research
            log.exception("couldn't close the analysis of role %s that was taken %s times", rid,
                          hand_outs.MAX_CLAIMS)
            role.analysis_state = FAILED

    async def take(session: AsyncSession, role: Opportunity, moment: datetime) -> bool:
        if await gate_facts.stop_if_failed(session, role, "before an analyst took it"):
            return False  # a gate fails on what the role holds now (an employer's record, say): it is not analysed
        claim(role, moment, session_id, runner)
        await _stamp_hand_out(session, owner, role)
        await session.flush()
        if await _on_the_flow(session, owner, role):  # its run began on the old order of work: begun again
            if role.analysis_state != WAITING:
                return False  # the new run left it: not Sourced, or waiting for its text; no session has it
            claim(role, moment, session_id, runner)  # and still this session's
            await _stamp_hand_out(session, owner, role)
            await session.flush()
        return True

    line = hand_outs.Line(free=roles, model=Opportunity, take=take, give_up=give_up,
                          where=lambda moment: (Opportunity.analysis_state == WAITING, _free(moment)),
                          taken=lambda role: int((role.analysis or {}).get("claims", 0)))
    taken = await hand_outs.take_next(session, owner, hand_outs.ANALYSIS, [line], now=now or datetime.now(UTC),
                                      session_id=session_id, claim=claim_id)
    return taken[0] if taken else None


async def _on_the_flow(session: AsyncSession, owner: str, role: Opportunity) -> bool:
    """A role waiting at the analyst's research step with no text of its posting began its run before research of a role
    without text was stopped (a deploy found it in line): its run is cancelled and begun again on the process as it
    is, so the app sources it and fetches the text first, and a role still without text waits for it. Returns whether
    it did."""
    if await open_step(session, role) != process.ANALYST_STEP or (await posting_text.held(session, role)).text:
        return False
    first = asked_by_owner(role)
    await stuck.cancel_instance(session, role.analysis_instance_id, "the order of work changed",
                                f"the analysis of role {role.id}")
    role.analysis_state = None
    await start(session, owner, role, by_owner=first)
    return True


async def settle_stuck(session: AsyncSession, owner: str, now: datetime | None = None,
                       since: datetime | None = None) -> int:
    """An analysis the analyst never sent fails, so the queue moves on: a session took the role and `stuck_minutes`
    have passed since (counted from `since` when that is later: the end of peak hours). A role no session has taken
    is waiting in line, not stuck, however long the line is (a night's finds are all put in line at once): it stays."""

    async def find(cutoff: datetime) -> list[Opportunity]:
        return list((await session.scalars(select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.analysis_state == WAITING,
            Opportunity.analysis_claimed_at.is_not(None), Opportunity.analysis_claimed_at < cutoff))).all())

    async def close(role: Opportunity) -> None:
        await _give_up(session, owner, role)

    return await stuck.settle_stuck(session, now=now, since=since, what="analysis", find=find, close=close)


# --- the analyst's task ------------------------------------------------------------------------------------------


# --- a job the owner added by its link is matched to a target role (docs/PRD.md 24.10) --------------------------------


def _target_ids(profile) -> list[str]:
    """The ids of the owner's target roles that say something: the ones a job can be matched to (as the title pass is
    handed them, `profile_views.for_titles`)."""
    return [r.id for r in profile.criteria.target_roles if r.says_something()]


def to_match(role: Opportunity, profile) -> bool:
    """Whether the analyst is asked to match this role to one of the owner's target roles: any role, while the owner has
    a target role to match it to and the analyst has not matched it yet. A title check or the small title session may
    have matched it; the analyst's answer is final and replaces theirs. The match is the analyst's judgement, in the
    same session as the research, so the role is researched for the asks and rules of the role it matches and no second
    research is needed."""
    return role.matched_by != ANALYST and bool(_target_ids(profile))


async def match_target_role(
        session: AsyncSession, owner: str, role: Opportunity, matched: str | None, profile
) -> None:
    """Give the role the target role the analyst matched it to, or say that it matched none; either way the analyst is
    the pass that matched it (`matched_by`), final. The role then has that
    role's own gates, pay floor and asks wherever the app reads them: the research that follows works the score out on
    its asks, the gates run again on its pay floor and lines (`action_settle`), and the Pipeline narrowed to the role
    shows it. The version of What to look for it was matched under is kept, as for every match. A job that
    matches none stays on the gates and asks for every role, and its history says so."""
    link = ("opportunity", role.id)
    if not may_write(role, ANALYST):  # pyright: ignore[reportArgumentType]  # always true here
        return
    role.target_role = matched
    role.matched_by = ANALYST
    role.matched_by_version = await profile_store.newest_version(session, owner, "criteria")
    if matched is None:
        await trail.record(
            session, owner, event="role_matched", actor="agent", agent_slug="scout", who="Analyst", link=link,
            sentence="No target role of yours matched this job, so the gates and asks for every role apply.")
        return
    named = profile.criteria.role(matched)
    name = (named.name if named is not None else "") or matched
    await trail.record(
        session, owner, event="role_matched", actor="agent", agent_slug="scout", who="Analyst", link=link,
        sentence=f"The analyst matched this job to your target role {name}: its gates and asks apply.")


async def open_step(session: AsyncSession, role: Opportunity) -> str | None:
    """The analyst's step the role's process is waiting at: `process.ANALYST_STEP` (the role is Sourced, holds the
    posting's text and is researched), or None when it waits at none."""
    if role.analysis_instance_id is None:
        return None
    return await session.scalar(select(ProcWait.element_id).where(
        ProcWait.instance_id == role.analysis_instance_id, ProcWait.status == OPEN,
        ProcWait.element_id == process.ANALYST_STEP).limit(1))


async def brief_part(session: AsyncSession, owner: str, role: Opportunity, part: str,
                     context: str | None = None) -> dict:
    """One part of the analyst's task. `context` is the digest of the owner context the session holds in its system
    prompt (`session_context.py`): when it is the one the app holds now, the task leaves out what that holds (the
    resume, the owner's scoring, the response shape and the instructions, which are in the prompt) and says so."""
    if part not in PARTS:
        raise AnalysisError(f"the parts are {', '.join(PARTS)}")
    if part == "resume":
        return await session_context.resume_part(session, owner)
    if part == "context":
        return await session_context.parts(session, owner, session_context.ANALYSIS)
    profile = await profile_store.current(session, owner)
    employer = await session.get(Employer, role.employer_id) if role.employer_id else None
    hosts = await employer_posting.job_board_hosts(session, owner)
    links = employer_posting.links_of(role, hosts)
    unnamed = is_unnamed(role)
    kept = (await posting_text.held(session, role)).text
    # The analyst matches every role it researches (`to_match`): it picks one of the owner's target roles in these
    # findings, with every target role in front of it, and answers the asks and rules of the role it picks.
    matching = to_match(role, profile)
    # A role a lower pass has already matched is handed that role, and its gates, as the starting point to confirm or
    # overrule; the other target roles come beside it. A role with none is handed every target role and every gate
    # line, each naming the roles it is for (`only_for_roles`, as the asks do): the ones of the role it matches are in
    # them.
    held = look_for_role(profile.criteria, role.target_role) if matching and role.target_role else {}
    confirming = bool(held.get("target_role"))
    every = not confirming and matching
    task = {
        # `url` is the employer's own posting when the app has found it (`employer_url`), else the job board's
        # (`board_url`). A job the owner added by its link has no title or employer yet: `name_the_role` asks for them.
        "role": {"id": str(role.id), "title": None if unnamed else role.title,
                 "company": None if unnamed else role.company, "url": role.url,
                 **({"name_the_role": NAME_THE_ROLE} if unnamed else {}),
                 **links,
                 "apply_url": role.apply_url, "location": role.location, "work_mode": role.move_type,
                 "comp": role.comp, "posted": role.posted.isoformat() if role.posted else None,
                 "fit": role.fit, "fit_evidence": role.fit_evidence, "fit_breakdown": role.fit_breakdown or None,
                 "posting_text_kept": kept or None,
                 # the board's copy stands in where the employer's own posting could not be read: said, so the findings
                 # are not taken for a read of the employer's words (the owner, 2026-11-05, the board's copy hook)
                 **({"posting_text_from": POSTING_TEXT_FROM_BOARD}
                    if kept and is_board_copy(role) and kept == (role.description or "").strip() else {})},
        **({"match_the_role": CONFIRM_THE_ROLE if confirming else MATCH_THE_ROLE} if matching else {}),
        **({"other_target_roles": [r for r in look_for(profile.criteria).get("target_roles", [])
                                   if r["id"] != role.target_role]} if confirming else {}),
        **await _requirement_lines(session, role),
        "employer": employers.view(employer) if employer else None,
        "what_to_look_for": (look_for(profile.criteria) if every
                             else look_for_role(profile.criteria, role.target_role)) or None,
        "gates": [{"id": line["id"], "says": line["says"], "checked_by": line["who"],
                   **({"only_for_roles": line["roles"]} if every and line["roles"] else {})}
                  for line in gate_lines(profile.gates)
                  if line["on"] and (every or not line["roles"] or role.target_role in line["roles"])],
        # The place, when the work location gate is undecided on what the role holds: asked for in the first submission,
        # so it is not refused and sent again whole (`stated_place.ask_first`).
        "send_the_place": stated_place.ask_first(role, await gating.rules(session, owner, profile),
                                                 (await posting_text.held(session, role)).text,
                                                 await cleansed.gate_copy_of(session, owner, role.id)),
        # The employer's other researched roles' posted bands, each with the market pay stored for it
        # (`sibling_bands`): the analyst searches no market for them and sends no siblings of its own.
        "sibling_bands": await sibling_bands.for_role(session, owner, role) or None,
        **await _stable_parts(session, owner, profile, context),
    }
    return task


async def _requirement_lines(session: AsyncSession, role: Opportunity) -> dict:
    """The posting's requirement lines, cut by code and numbered, for the analyst to answer one by one (docs/PRD.md
    26.12); nothing when the posting has none (the analyst lists the items itself, as before)."""
    found = requirement_lines.split((await posting_text.held(session, role)).text)
    if not found:
        return {}
    return {"requirement_lines": {
        "say": REQUIREMENT_LINES_SAY,
        **{part: [{"line": x.n, "text": x.text} for x in found if x.part == part] for part in requirement_lines.PARTS},
    }}


async def _stable_parts(session: AsyncSession, owner: str, profile, context: str | None) -> dict:
    """What every role's task holds the same: the owner's scoring, the response shape, the instructions, and the
    resume to read next. A session whose system prompt holds them is told so instead (`session_context.held`)."""
    if await session_context.held(session, owner, session_context.ANALYSIS, context):
        return {"in_your_system_prompt": "the owner's master resume, their scoring, the response shape and your "
                                          "instructions; jobs.analysis part 'context' returns them again"}
    return {
        "scoring": {"sections": scoring_for_agent(profile.scoring, profile.criteria)},
        "read_next": ["resume"],
        "instructions": await instruction_files.read(session, owner, "instructions", "jobsearch.analyst"),
        "response_template": await instruction_files.read(session, owner, "templates", "jobsearch.analysis"),
    }


async def analysis_reply(session: AsyncSession, owner: str, part: str = "task", *,
                         session_id: str | None = None, runner: str | None = None,
                         context: str | None = None, claim: str | None = None) -> dict:
    """The analyst's task. Taking the task takes the role: the first in the line (`line_order`) that no other session
    has (sessions that ask together get different roles), and it is this session's while the session lives (`claim`).
    The resume is read after, with the role in hand, and takes nothing."""
    # A part that is not one is refused by `brief_part` before anything is taken: the claim is of the transaction.
    role = (await claim_next(session, owner, session_id=session_id, runner=runner, claim_id=claim) if part == "task"
            else await waiting(session, owner))
    if role is None and part == "task":
        # No research waits for this session: a summary alone may (`role_summary`), one short turn on the research kept.
        held = await role_summary.claim_next(session, owner, session_id=session_id)
        if held is not None:
            return {"ok": True, **await role_summary.summary_task(session, owner, held, context)}
    if role is None:
        return {"ok": True, "task": None, "note": "No role is waiting for research. Stop here."}
    if part == "resume" and await gate_facts.stop_if_failed(session, role, "when the analyst asked for the resume"):
        # The analysis stops at the first gate that fails: a gate now fails on the facts held (an employer's record
        # filled in since the role was taken), so the master resume is not sent and no findings are wanted.
        return {"ok": True, "task": None, "stopped": True,
                "note": f"A gate now fails for this role ({role.screened_out_reason}). It is Gated out: stop here and "
                        "send no findings."}
    return {"ok": True, **await brief_part(session, owner, role, part, context)}


# --- taking what it found ----------------------------------------------------------------------------------------


def _research(employer: Employer, found: EmployerFacts, by: str = "analyst") -> list[str]:
    """Set the facts the analyst found on the employer record, never one the owner set. Returns the fields set. A
    careers page is kept only where the record has none (`employers.learn_careers_url`), like every careers page the app
    learns."""
    stamp = datetime.now(UTC).isoformat()
    mine = set(employer.owner_fields or [])
    done = []
    for field, value in found.model_dump(exclude={"sources"}).items():
        if value is None or field in mine:
            continue
        if field == "reviews_url" and not employers.reviews_link(value):
            continue  # whoever sends it, only an address that passes the check is kept
        source = found.sources.get(field) or "the analyst's research"
        if field == "careers_url":
            if employers.learn_careers_url(employer, value, source, by=by):
                done.append(field)
            continue
        if field == "events" and not value and employer.events:
            value = employer.events  # searched and found nothing new: keep the dated events the record already holds
        setattr(employer, field, value)
        employer.sources = {**(employer.sources or {}), field: {"source": source, "at": stamp, "by": by}}
        done.append(field)
    return done


async def name_added(session: AsyncSession, owner: str, role: Opportunity, sub: Submission) -> bool:
    """Name a job the owner added by its link (`add_link.py`, `models.is_unnamed`) from what the analyst read in the
    posting: its title and employer replace the link and "Added link", and the role's key, slug and posting in the log
    follow. Both are needed; a role the analyst could not read stays as it is, named by its link. Nothing is renamed to
    a role the owner already has under another address (`same_role`): the unique key would refuse it, and one job is one
    role, so the role keeps the link as its name and its history says which role it looks like. Returns whether it was
    named. A role that has a name already is never renamed."""
    if not is_unnamed(role):
        return False
    company, title = " ".join((sub.company or "").split()), " ".join((sub.title or "").split())
    if not (company and title):
        return False
    key = match_key(company, title)
    known = await same_role.known_roles(session, owner)
    held = known.find(company, title, (role.url, role.apply_url, role.board_url), key=key)
    link = ("opportunity", role.id)
    if held is not None and held != role.id:
        other = await session.get(Opportunity, held)
        like = f"{other.title} at {other.company}" if other is not None else f"{title} at {company}"
        await trail.record(session, owner, event="role_named", actor="agent", agent_slug="scout", who="Analyst",
                           link=link, sentence=f"The analyst read this link as {like}, which you already have.",
                           detail="This role keeps the link as its name, because one job is one role and a second "
                                  "one by the same name is refused. The other role is the one that was there first.")
        return False
    role.company, role.title, role.match_key, role.slug = company, title, key, slugify(company, title)
    with queue.exempt("the same transaction's ORM write to the role (company, title) posts for it"):
        await session.execute(update(JobPosting).where(JobPosting.opportunity_id == role.id,
                                                       JobPosting.ats == ats.OWNER)
                              .values(company=company, title=title[:500], match_key=key))
    await trail.record(session, owner, event="role_named", actor="agent", agent_slug="scout", who="Analyst", link=link,
                       sentence=f"The analyst read the link: {title} at {company}.")
    return True


async def submit(session: AsyncSession, owner: str, body: dict) -> dict:
    """Take the analyst's findings, or refuse them with every problem named. A role that holds no text of its posting
    (a snippet is none: `whole.thin`) is not researched, and findings sent for it are not taken (`NO_TEXT_REASON`), so
    an analysis is never marked done on no text."""
    try:
        sub = Submission.model_validate(body)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:12])
        raise AnalysisError(f"the findings don't fit the shape; fix these and send them again: {problems}") from exc
    try:
        role = await session.get(Opportunity, uuid.UUID(sub.role))
    except ValueError:
        role = None
    if role is None or role.owner != owner:
        raise AnalysisError("no such role: use the id jobs.analysis gave")
    if role.analysis_state != WAITING and role_summary.held(role):
        return await role_summary.take(session, owner, role, body)  # a summary alone (`role_summary.summary_task`)
    if role.analysis_state != WAITING:
        raise AnalysisError("that role isn't waiting for research")
    text = (await posting_text.held(session, role)).text
    if whole.thin(text):  # none, or the role's own snippet, which `held` gives when it has nothing better
        return {"ok": False, "taken": False, "reason": NO_TEXT_REASON}  # never researched from a summary of a page
    posting_lines = requirement_lines.split(text)
    if (not (sub.required or sub.preferred or sub.responsibilities) and not posting_lines
            and graded_nothing(role.analysis) and may_send_back(role)):
        # Findings that grade nothing of a text with no requirement lines are the analyst saying the text is not the
        # posting (a careers page, a page's shell): a role graded on the owner's other asks alone would stand in Ready
        # to Launch on no grade. Nothing is kept; the role goes back for its text, once, and the
        # second time it is stopped (`send_back`). A text with lines and no items is refused below, as before, and an
        # item graded N is an item. Findings for a role that holds research with items (a second research that sent
        # nothing), a job the owner added and a role nothing can read again are taken as before: that text may be real.
        from app.jobsearch.loops import schedule  # the loop reads this module; imported here, never at the top

        again = await send_back(session, role, text, notes=sub.notes)
        schedule.signal_after_commit(session)  # a role that left Ready to Launch is placed in the pipeline's turn
        return {"ok": True, "taken": True, "sent_back": True,
                "reason": SENT_BACK_REASON if again else NOT_FOUND_REASON}
    profile = await profile_store.current(session, owner)
    # The analyst matches the role to one of the owner's target roles, here (`to_match`): the asks and rules the
    # findings must answer, and the gates that then run, are the matched role's. Nothing is set on the role until the
    # findings are taken, so a refusal leaves it as it was.
    matching = to_match(role, profile)
    matched: str | None = None
    answered = False  # the findings really gave an answer: an id of the owner's, or an explicit none
    match_problems: list[str] = []
    if matching and sub.target_role is not None:
        try:
            matched = titles.role_for("target_role", sub.target_role, _target_ids(profile))
            answered = True
        except titles.AnswerError as exc:
            match_problems.append(str(exc))
    problems = scoring.problems(sub.fit_breakdown, profile) if sub.fit_breakdown else []
    # The employer's careers page the analyst reports is checked here: a job board is refused. The analyst never
    # sources a role: the role's posting is found by code and the browser reader (`postings/sourced.py`).
    employer = await session.get(Employer, role.employer_id) if role.employer_id else None
    hosts = await employer_posting.job_board_hosts(session, owner)
    careers_problem = employer_posting.check_employer_link(
        sub.employer.careers_url, employer, field="employer.careers_url", own_domain=False, hosts=hosts)
    reviews_problem = None
    if sub.employer.reviews_url and not employers.reviews_link(sub.employer.reviews_url):
        reviews_problem = ("employer.reviews_url must be a public https address, as a web search "
                           "showed it: leave it out otherwise")
    problems += [p for p in (careers_problem, reviews_problem) if p]
    lines = _master_ids(await profile_store.owner_documents(session, owner))
    problems += (_class_problems("required", sub.required, lines) + _class_problems("preferred", sub.preferred, lines)
                 + _class_problems("responsibilities", sub.responsibilities, lines)
                 + _line_problems("required", sub.required, posting_lines)
                 + _line_problems("preferred", sub.preferred, posting_lines))
    problems += [f"responsibilities item {r.item[:60]!r} is N: a duty is P, T or U" for r in sub.responsibilities
                 if r.cls == "N"]
    problems += ["responsibilities item has no words: say the duty" for r in sub.responsibilities
                 if not r.item.strip()]
    problems += _duty_problems(sub, text)
    problems += match_problems
    if is_unnamed(role) and not (" ".join((sub.company or "").split()) and " ".join((sub.title or "").split())):
        # Research is what names a role added by its link: findings without the employer and the title would leave it
        # "Added link" and still move on to the writer.
        problems.append("company and title: this role is only a link, so say the employer and the job title exactly as "
                        "the posting writes them")
    if matching and sub.target_role is None:
        # Said either way: a target role's id, or `none`.
        problems.append("target_role: say which target role this job matches (one of "
                        f"{', '.join(_target_ids(profile))}), or none")
    # The work location gate is decided on the place the posting states (the owner, 2026-10-17): left undecided, the
    # analyst is asked for the place, or to say the posting gives none (`stated_place`).
    place_problem = stated_place.problem(role, await gating.rules(session, owner, profile), sub.location,
                                         sub.work_mode, text, sub.place_not_stated,
                                         await cleansed.gate_copy_of(session, owner, role.id))
    if place_problem:
        problems.append(place_problem)
    if problems:
        tries = int((role.analysis or {}).get("refused", 0)) + 1
        if tries < MAX_REFUSALS:
            role.analysis = {**(role.analysis or {}), "refused": tries}
            await session.flush()
            return {"ok": False, "taken": False, "tries_left": MAX_REFUSALS - tries,
                    "reason": "not taken; fix these and send it all again: " + " | ".join(problems)}
        sub.fit_breakdown = None  # taken without its re-judged score; the scout's stands
    if careers_problem:
        sub.employer.careers_url = None
    if reviews_problem:
        sub.employer.reviews_url = None
    await name_added(session, owner, role, sub)  # a job added by its link takes the name the posting gives it
    if matching and answered:  # the matched role's own gates, pay floor and asks apply from here on; an answer the
        # findings did not really give (taken after the refusals ran out) leaves the role's match as it was
        await match_target_role(session, owner, role, matched, profile)
    if employer is None and not is_unnamed(role):  # "Added link" is no employer: none is made for it
        employer = await employers.find_or_make(session, owner, role.company)
        role.employer_id = employer.id if employer else None
    filled = _research(employer, sub.employer) if employer else []
    now = datetime.now(UTC)
    # The text of the posting is the app's (docs/PRD.md 39.29): the role is given the text a posting of its own holds.
    # The analyst's reading of the page (`posting_text`) is a summary and is never kept, even where the role has none.
    await posting_text.take(session, role, posting_text.FromPostings(), now=now)
    if sub.pay.band and len(sub.pay.band) == 2 and not (role.comp or "").strip():
        # A band the search couldn't see, found by research: the pay gate reads it when the gates run again.
        role.comp, role.comp_source = f"${sub.pay.band[0]:,}-${sub.pay.band[1]:,}", "research"
    # The place and the way of working the posting or the employer states, where the role has none: the work location
    # gate reads them when the gates run again. A place that says nothing ("26 Locations", "Multiple Locations", "Not
    # stated") is none, as the board's own is (`says_no_place`), and so, for the gate, is a place that names no town
    # ("United States" on an onsite role: `stated_place.fills`); a town the role holds is never overwritten. Where the
    # analyst sent none, the posting's own location line gives it (`stated_place.fill`).
    if sub.work_mode and (role.move_type or "").strip().lower() in ("", "unknown"):
        role.move_type = sub.work_mode
    place = " ".join((sub.location or "").split())
    if place and ((says_no_place(role.location) and not says_no_place(place))
                  or stated_place.fills(role.location, role.move_type, place)):
        role.location = place
    stated_place.fill(role, text)
    # What is kept follows what the analyst answered and what the posting says: "not stated" holds unless the
    # analyst sent a town or the posting's own labels state one. The town on the role's card is no statement of the
    # posting.
    posting_states = names_a_town(place) or bool(stated_place.stated(text))
    place_answer = (stated_place.NOT_STATED if sub.place_not_stated and not posting_states
                    else stated_place.ASKED if place_problem else stated_place.STATED)
    pay = sub.pay.model_dump(mode="json")
    pay["siblings"] = sibling_bands.merge(await sibling_bands.for_role(session, owner, role), pay["siblings"])
    handed = (role.analysis or {}).get(HANDED_UNDER_KEY)
    role.analysis = {
        "pay": pay,
        "required": _kept("required", sub.required, posting_lines),
        "preferred": _kept("preferred", sub.preferred, posting_lines),
        **({"lines": requirement_lines.as_items(posting_lines)} if posting_lines else {}),
        "responsibilities": [{**r.model_dump(mode="json", by_alias=True), "by": "analyst"}
                             for r in sub.responsibilities],
        DECK_READ: True,
        "notes": sub.notes.strip()[:2000], "for_owner": [x.strip()[:400] for x in sub.for_owner if x.strip()][:12],
        "employer_facts_found": filled, "at": now.isoformat(), stated_place.PLACE_KEY: place_answer,
        # The snapshot a re-analysis is compared with (`reanalysis.compare`) outlives the research it was taken of.
        **{k: v for k, v in (role.analysis or {}).items() if k == BEFORE_KEY},
    }
    # The versions the research was made from, so a later change can say which research it touches (`staleness.py`).
    # They are the versions at the hand-out, when the analyst was given its inputs; with none kept, those now.
    role.analysis_made_under = handed if isinstance(handed, dict) else await staleness.stamp_analysis(session, owner)
    # The analyst's re-judged answers join the ones kept on the role, each under the ask's words (docs/PRD.md 39.20).
    # The ask, the score and the gates are worked out at the process's next step, `Settle` (`settle.roles`), which runs
    # in the call below. Written only when answers were sent, so a role holding a fit on another scale keeps it
    # otherwise.
    if new := scoring.answers_in(sub.fit_breakdown):
        role.fit_breakdown = {"answers": scoring.record(
            scoring.stamp(scoring.answers_in(role.fit_breakdown), profile), new, profile, prioritize.BY_ANALYST)}
    await session.flush()
    instance = await process.engine().complete(session, role.analysis_instance_id, process.ANALYST_STEP,
                                               {"submitted": True})
    if instance.status != COMPLETED:
        raise AnalysisError(instance.error or f"the analysis's process {instance.status}")
    await session.refresh(role)
    return {"ok": True, "taken": True, "stage": role.status, "fit": role.fit,
            "fit_pct": scoring.percent_of(role, scoring.bounds(profile))}


def _duty_problems(sub: Submission, text: str) -> list[str]:
    """Refuse findings that grade none of the duty bullets a posting lists under a responsibilities heading (found
    2026-11-03: `responsibilities` defaults to none, and only the required and preferred lines were checked against the
    posting, so a role whose duties were never graded was taken). One graded duty is enough here: which bullets are the
    job and which are filler is the analyst's call, as the instruction says. Duties written as prose have no bullet and
    ask nothing. A posting that states no qualifications has its main duties stand in as required items
    (`from_duties`), and those are not asked for again."""
    if sub.responsibilities or any(r.from_duties for r in sub.required):
        return []
    duties = requirement_lines.duty_bullets(text)
    if not duties:
        return []
    return [f"responsibilities: the posting lists {len(duties)} duty bullets under {duties[0].heading!r} (the first is "
            f"{duties[0].text[:60]!r}) and none is graded: send one item in `responsibilities` for every duty bullet, "
            "P, T or U (a posting whose duties are written as prose sends none)"]


def _line_problems(part: str, items: Sequence[Required], posting_lines: list[requirement_lines.Line]) -> list[str]:
    """What is wrong with how the items of `required` or `preferred` answer the posting's numbered lines (docs/PRD.md
    26.12): when the posting has lines for the part, each is answered once, in its own item, and no other is added. A
    part the posting has no lines for is the analyst's own list, as before."""
    mine = {x.n: x for x in posting_lines if x.part == part}
    if not mine:
        return ([f"{part} item {r.item[:60]!r} has a `line`, but the posting has no {part} lines" for r in items
                 if r.line is not None]
                + [f"{part} item has no words: say the item" for r in items if not r.item.strip()])
    out = []
    answered: dict[int, int] = {}
    for r in items:
        if r.line is None:
            out.append(f"{part} item {r.item[:60]!r} has no `line`: answer the numbered lines only, one item for each")
        elif r.line not in mine:
            out.append(f"{part} item {r.item[:60]!r} answers line {r.line}, which is not a {part} line: send `line` "
                       "as the task numbered it")
        else:
            answered[r.line] = answered.get(r.line, 0) + 1
    out += [f"{part} line {n} is answered {k} times: answer it once" for n, k in sorted(answered.items()) if k > 1]
    out += [f"{part} line {n} ({x.text[:60]!r}) is not answered: every line needs an item, P, T, U or N"
            for n, x in mine.items() if n not in answered]
    out += [f"{part} item for line {r.line} is from the duties: lines are graded as they stand" for r in items
            if r.from_duties and r.line is not None]
    return out


def _kept(part: str, items: Sequence[Required], posting_lines: list[requirement_lines.Line]) -> list[dict]:
    """The items as they are kept: an item answering a line takes the line's own words (nothing is reworded), and the
    items stand in the posting's order."""
    mine = {x.n: x.text for x in posting_lines if x.part == part}
    out = []
    for r in items:
        kept = {**r.model_dump(mode="json", by_alias=True), "by": "analyst"}
        if r.line in mine:
            kept["item"] = mine[r.line]
        else:
            kept.pop("line", None)
        out.append(kept)
    return sorted(out, key=lambda k: k.get("line") or 0) if mine else out


def _class_problems(part: str, items: Sequence[Required], lines: set[str]) -> list[str]:
    """What is wrong with the classes the analyst gave the items of one section (`required`, `preferred` or
    `responsibilities`), in words:
    a Proven or Transferable item must cite the master lines that back it, and each line cited must be in the master.
    The two sections are held to the same rules."""
    out = []
    for r in items:
        if r.cls == "N":
            if r.from_:
                out.append(f"{part} item {r.item[:60]!r} is N, not measurable, so it cites no master line")
            if not r.why.strip():
                out.append(f"{part} item {r.item[:60]!r} is N: say in a line why it can't be measured from a resume")
        if r.cls in ("P", "T") and not r.from_:
            out.append(f"{part} item {r.item[:60]!r} is {r.cls} but cites no master line: say which (from)")
        out += [f"{part} item {r.item[:60]!r} cites {i!r}, which the master doesn't have"
                for i in r.from_ if i not in lines]
    return out


def _master_ids(docs) -> set[str]:
    r = docs.resume
    ids = {b.id for role in r.roles for b in role.bullets} | {role.id for role in r.roles}
    ids |= {e.id for role in r.roles for e in role.evidence}
    ids |= {x.id for x in [*r.achievements, *r.projects, *r.awards, *r.summary_variants]}
    return ids | {e.id for e in r.education}


#: The clause the role's history gives for the settle that follows a corrected class (`correct_class`).
CORRECTED_CAUSE = "You corrected a required item's class"


async def correct_class(session: AsyncSession, owner: str, role_id: uuid.UUID, index: int, cls: str,
                        why: str, *, defer_settle: bool = False) -> dict:
    """The owner corrects a required item's class (docs/PRD.md 26.2): kept in the role's history, then the role is
    settled again from the classes (`settle.roles`: the expectation, the score, the gates). With `defer_settle` the
    correction and its line are saved and the settle is left to the caller (the page's route asks the engine, the ask
    `role.settle`, with `CORRECTED_CAUSE`): the answer is then the research as it stands, with no `said`."""
    role = await session.get(Opportunity, role_id)
    if role is None or role.owner != owner:
        raise NoSuchRole("no such role")
    required = list((role.analysis or {}).get("required") or [])
    if not 0 <= index < len(required):
        raise NoSuchItem("no such required item")
    if cls not in ("P", "T", "U"):
        raise AnalysisError("a class is P, T or U")
    old = required[index]
    required[index] = {**old, "class": cls, "by": "owner", "owner_why": " ".join(why.split())}
    role.analysis = {**role.analysis, "required": required}
    words = {"P": "Proven", "T": "Transferable", "U": "Unproven"}
    was = {**words, "N": "Not measurable"}.get(old.get("class"), "?")
    await trail.record(session, owner, event="requirement_corrected", actor="person",
                       who=await actor_name(session, owner),
                       sentence=f"You classed \"{old['item'][:80]}\" {words[cls]} (was {was}).",
                       link=("opportunity", role.id), detail=why or None)
    # The classes changed the pay the analyst's work gives, the score and what a rule decided from them says, so the
    # role is settled again (docs/PRD.md 39.14): a role the gates now stop is Gated out, and one only they stopped is
    # back in play. A role the owner moved by hand, or at Applied or later, stays where it is.
    if defer_settle:  # the caller asks the engine to settle the role (`role.settle`) and reads it again
        return view(role) or {}
    settled = await settle.roles(session, [role], cause=CORRECTED_CAUSE)
    out = view(role) or {}
    if said := settled.gated.get(role.id):
        out["said"] = said
    return out


# --- the process's service steps ------------------------------------------------------------------------------


async def _role(session: AsyncSession, data: dict) -> Opportunity:
    role = await session.get(Opportunity, uuid.UUID(data["role"]))
    if role is None:
        raise AnalysisError("the role is gone")
    return role


async def action_gather(session, instance, data):
    role = await _role(session, data)
    return {"ready": role.status in stages.PRE_READY}


async def action_source(session, instance, data):
    """The step "Source the posting" (the owner, 2026-10-15, `postings/sourced.py`): a role that is not Sourced is
    sourced in
    code where the app already holds what it takes (`sourcing.source_in_code`). Only a Sourced role is researched. A
    step that fails is logged and leaves the role as it was."""
    role = await _role(session, data)
    if role.sourced_at is None:
        try:
            async with session.begin_nested():
                with line.in_step():  # a role it ends keeps its instance
                    await sourcing.source_in_code(session, role)
        except Exception:
            log.exception("sourcing role %s in code failed", role.id)
    return {"sourced": role.sourced_at is not None}


async def action_not_sourced(session, instance, data):
    """The step "Leave it where it is": a role that is not Sourced is not researched. It is not in the analyst's line,
    and its sourcing is tried again every night (`sourcing.sweep`, the browser reader); its history says so."""
    role = await _role(session, data)
    await line.leave(session, role, line.NOT_SOURCED, inside=True)
    await trail.record(
        session, role.owner, event="not_sourced", sentence=f"{role.title} at {role.company}: not sourced yet.",
        link=("opportunity", role.id),
        detail="The app holds no link to the original posting (the employer's careers site or its job system), so the "
               "role is not researched. It stays where it is and its sourcing is tried again every night.")
    await session.flush()
    return {"state": NOT_SOURCED}


async def action_fetch_text(session, instance, data):
    """Fetch the posting's text before the analyst reads the role (docs/PRD.md 39.29): the text the app holds is put on
    the role, or else it is read in code from the role's source, and from no other link (`posting_text.take`). A
    role the app cannot get it for waits for the posting's text; a step that fails is logged and leaves the role as it
    was."""
    role = await _role(session, data)
    try:
        async with session.begin_nested():
            with line.in_step():  # a role it ends (the posting is gone) keeps its instance
                how = await posting_text.take(session, role)
    except Exception:
        log.exception("fetching the posting's text for role %s failed", role.id)
        how = None
    return {"held": how is not None, "how": how}


async def action_wait_for_text(session, instance, data):
    """The step "Wait for the posting's text": the role holds no text of its posting, the app fetched what it could
    and the analyst found no employer posting the app could read. It is not researched and not scored (its asks
    stay unanswered, which the scoring counts as none met); its research state is `NO_TEXT`, and its history says
    why."""
    role = await _role(session, data)
    if role.status in stages.ENDED and role.ended_reason == stages.POSTING_GONE:  # the fetch ended it: it waits for
        return {"state": "ended"}  # nothing (`quiet.end_gone`)
    await wait_for_text(session, role)
    return {"state": NO_TEXT}


async def wait_for_text(session: AsyncSession, role: Opportunity, *, detail: str | None = None,
                         sentence: str | None = None) -> None:
    """Put the role in `NO_TEXT`, with the session's claim and the tries of its research cleared, and say so in its
    history (`detail` is the words under the line, the default being why the app could not read the posting;
    `sentence` is the line, by default that the role waits). The one way a role comes to wait for the posting's text,
    whether the process says so or the analyst's findings do (`send_back`). The keys it clears are named; whatever
    else the research holds stays, `SENT_BACK` and `POSTING_NOT_FOUND` among them. Services flush."""
    await line.leave(session, role, line.SEND_BACK_TEXT, inside=True)
    await trail.record(
        session, role.owner, event="waiting_for_text",
        sentence=sentence or f"{role.title} at {role.company}: waiting for the posting's text.",
        link=("opportunity", role.id),
        detail=detail or "The app could not read the posting and the analyst found no employer posting it could read. "
                         "A role is never researched from a partial text, so it waits until the owner's browser reads "
                         "the whole posting.")
    await session.flush()


def graded_nothing(research: dict | None) -> bool:
    """Whether stored research holds no item at all in what the posting is graded by (`GRADED`). An item graded N is an
    item: it was read and judged not measurable, which is not the same as nothing read."""
    return not any((research or {}).get(part) for part in GRADED)


def graded_nothing_where() -> tuple:
    """What the database can say of a research the analyst sent (`at` is stamped by `submit` and by nothing else) that
    `graded_nothing` holds of, for a query that picks the roles to look at (`postings/ungraded.py`): each list of
    `GRADED` is absent or empty. The stored lists are lists, so this is the rule's own answer, not a wider one."""
    return (_field("at").is_not(None), *(func.coalesce(_field(part), "[]") == "[]" for part in GRADED))


def can_read_again(role: Opportunity) -> bool:
    """Whether anything can read this role's posting again. An Easy Apply (`source_kind` board: the board's own listing
    was the source, recorded once) is never read again (`browser/reader.py`), and a role with no link has nothing to
    open."""
    if role.source_kind in (sourcing.BOARD, sourcing.BOARD_COPY):
        return False
    return bool((role.source_url or "").strip() or (role.url or "").strip())


#: The stages whose roles a reader asks the text of (`browser/reader.READ_IN_FULL_STAGES`, a test pins them equal): the
#: stages before the application, and the ones the owner applied or is in talks in (`unbuilt.STAGES`, 2026-11-05).
READ_STAGES = (*stages.PRE_APPLY, *unbuilt.STAGES)


def may_send_back(role: Opportunity) -> bool:
    """Whether a text that findings graded nothing of may be let go for not being the posting (`send_back`). It may not
    for a job the owner added by hand (the text is theirs, or the link they gave is all there is), for a role nothing
    can read again (`can_read_again`), or for an ended or gated out role (`READ_STAGES`: the readers of a text ask for
    no role there, `browser/reader.py`): the text would be thrown away and never replaced. A role the owner applied to
    or is in talks about is read (2026-11-05), so its text may be let go, and is read again. Nor for a role that holds
    the board's copy of its posting (the board's copy hook): the employer's own posting could not be read, so the
    copy is the best text there is and nothing better will be read; letting it go would research it a second time and
    then give the role up as one whose posting was not found. The caller also asks that
    the role holds no research with items, which findings that grade nothing would otherwise replace."""
    return (role.status in READ_STAGES and role.origin != ORIGIN_OWNER and can_read_again(role)
            and not is_board_copy(role))


def may_send_back_where() -> tuple:
    """`may_send_back`, for a query that picks the roles to look at (SQL: the same rule, in the same words)."""
    blank = " \t\r\n"
    return (Opportunity.status.in_(READ_STAGES), Opportunity.origin != ORIGIN_OWNER,
            or_(Opportunity.source_kind.is_(None),
                Opportunity.source_kind.not_in((sourcing.BOARD, sourcing.BOARD_COPY))),
            Opportunity.description_from.is_distinct_from(DESCRIPTION_BOARD),
            or_(func.trim(func.coalesce(Opportunity.source_url, ""), blank) != "",
                func.trim(func.coalesce(Opportunity.url, ""), blank) != ""))


async def send_back(session: AsyncSession, role: Opportunity, text: str, *, notes: str = "") -> bool:
    """A role whose held `text` is not its posting goes back for the posting's text, instead of being graded on nothing:
    the open step is cancelled when the analyst has the role (a role researched already has none open); the text is let
    go (`posting_text.let_go`), so the browser's read of the role's source is asked for it again and the role is not
    taken for one that holds a text; a score that stood on research which held no item, and so on the owner's other asks
    alone, is cleared (the answers stay: the role waits for its research as one with answers and no score does); and the
    role waits for the posting's text (`wait_for_text`), its history saying what the analyst said. Old research stays
    until new research replaces it. The caller commits and asks the loop for a turn (`schedule.signal_after_commit`),
    which places the role: a role in Ready to Launch that is no longer read leaves it.

    A role goes back once (`SENT_BACK_LAPS`, counted on the role's research). One that is sent back again, the page it
    came back with graded as nothing too, is stopped instead: it stays waiting for its text with `POSTING_NOT_FOUND`
    set, and its history says its posting could not be found at its source. No read of any kind asks for it (the
    browser's two, its queue, the app's own, the night's read of descriptions, `resume_waiting`: all leave a role the
    app gave up on out), so it never re-enters Ready to Launch or the analyst's line by itself: only the owner's ask
    for its research (`ask`) puts it in line again, which clears the stop and keeps the lap count, so the next findings
    that grade nothing stop it again at once. Returns whether the role went back for another read (False: it was
    stopped). Services flush."""
    research = role.analysis or {}
    laps = int(research.get(SENT_BACK) or 0)
    again = laps < SENT_BACK_LAPS
    on_nothing = graded_nothing(research) and bool(research.get("at"))
    if role.analysis_state == WAITING:
        await stuck.cancel_instance(session, role.analysis_instance_id, "the text held is not the posting",
                                    f"the analysis of role {role.id}")
    await posting_text.let_go(session, role, text)
    if on_nothing:
        answers = scoring.answers_in(role.fit_breakdown)
        role.fit, role.fit_breakdown = None, {"answers": answers} if answers else {}
    said = " ".join((notes or "").split())[:600]
    said = f" The analyst said: {said}" if said else ""
    if again:
        role.analysis = {**research, SENT_BACK: laps + 1}
        await wait_for_text(session, role, detail=(
            "The text the app held for this role is not its posting: it has no requirement, preferred item or duty to "
            "grade. The app let it go, and the owner's browser reads the posting at its source again; the role is "
            "researched when the text comes." + said))
    else:
        role.analysis = {**research, POSTING_NOT_FOUND: True}
        await wait_for_text(
            session, role, sentence=f"{role.title} at {role.company}: its posting could not be found at its source.",
            detail=("The role went back for its posting's text once, and the text that came was not its posting "
                    "either: it has no requirement, preferred item or duty to grade. The app let it go, and nothing "
                    "reads the role's source for it again, so it waits for its text until you ask for its "
                    "research." + said))
    return again


async def resume_waiting(session: AsyncSession, owner: str, role_ids: Iterable[uuid.UUID] | None = None) -> int:
    """Put the roles that waited for the posting's text and now hold it in the analyst's line (a fresh run: the app
    fetches nothing more, the text is there, and the analyst researches). A role is put there when the fetch step would
    give it its text with no read (`posting_text.holds_without_read`: the fetch's own question, so a role that is put
    back in line is one the step does not send straight back to wait). A role the owner asked research of keeps its
    place first. Returns how many were put in line.

    `role_ids` narrows it to these roles (the ones a browser capture just gave text): the caller's own transaction
    holds them by then, so they are locked, in id order, and started in it. With none given, every waiting role is
    looked at (the schedule's sweep, which catches a text the app's own read brought): the roles are read and judged
    with no lock, and each one that qualifies is locked alone, judged again on the locked row, started and saved in a
    transaction of its own (`db.release_if_free` before and after), so no role row is held for the rest of the sweep
    and the read the start makes ahead (`read_ahead`) can end the transaction. A role another transaction holds is
    left for the next sweep, never waited for, and so is a role the analyst took meanwhile (`AnalysisError`). Services
    flush; the caller commits (the sweep commits its roles as it goes where it may, and the caller commits the last)."""
    # A role the app gave up on (`send_back`) is not put in line by a text that came: only the owner's ask is.
    waiting = (Opportunity.owner == owner, Opportunity.analysis_state == NO_TEXT, posting_still_sought())
    query = select(Opportunity).where(*waiting)
    started = 0
    if role_ids is not None:
        for role in (await session.scalars(query.where(Opportunity.id.in_(list(role_ids))).order_by(Opportunity.id)
                                           .execution_options(populate_existing=True))).all():
            if await posting_text.holds_without_read(session, role):
                await _put_in_line(session, owner, role)
                started += 1
        return started
    # The look is a reader's where the session is free (`off_turn.reading`): on SQLite a read in the writer's session
    # took the file's one turn for the whole look (1 to 1.6 s at startup). Only ids leave it; each role is locked alone.
    async with off_turn.reading(session) as reader:
        wanted = [role.id for role in (await reader.scalars(query.order_by(Opportunity.id).execution_options(
            populate_existing=True))).all() if await posting_text.holds_without_read(reader, role)]
    for role_id in wanted:
        await db.release_if_free(session)  # the role is locked alone: no other role row is held by now
        locked = (await as_read.lock(session, Opportunity, [role_id], where=waiting)).rows.get(role_id)
        if locked is None or not await posting_text.holds_without_read(session, locked):
            continue  # changed since it was read, or held by another transaction (which is changing it anyway: a wait
            # for its row would end the sweep at the lock timeout): left for the next sweep
        try:
            await _put_in_line(session, owner, locked)
        except AnalysisError:  # the analyst has it already (a session took it while the text was read ahead)
            log.info("the role %s is with the analyst already: the sweep leaves it", role_id)
            continue
        started += 1
    await db.release_if_free(session)
    return started


async def _put_in_line(session: AsyncSession, owner: str, role: Opportunity) -> None:
    """One waiting role put in the analyst's line, first if the owner asked for its research (`resume_waiting`)."""
    await start(session, owner, role, by_owner=(role.analysis or {}).get(ASKED_BY) == ASKED_BY_OWNER)


async def action_settle(session, instance, data):
    """The step "Settle" of `analyse_role.bpmn`: the role is marked researched and settled (`settle.roles`: the ask
    worked out from the findings, the score, then every gate again on the facts, docs/PRD.md 39.11), and a role that now
    fails a gate is Gated out with its reason, unless the owner moved it by hand (a gate found at any stage closes a
    role, never its score). A role at Applied, Active Discussion, Rejected, Gone Quiet, Withdrew or Gated out keeps its
    status, and a Gated out role its reason: no gate is run on it here. Every other role stays where it is, researched:
    on deck, it goes up to Ready to Launch when there is room, and a role a gate closed leaves the deck for the next one
    by rank, in the pipeline's turn, which this asks for once the research is committed
    (`schedule.signal_after_commit`): the turn is the loop's, never run inside the analyst's report."""
    from app.jobsearch.loops import schedule  # the loop reads this module; imported here, never at the top

    role = await _role(session, data)
    # A role researched before (`analysed_at` is kept when it is asked for again, `ask`) says so in its history.
    researched = f"The analyst researched {role.title} at {role.company}" + (" again" if role.analysed_at else "")
    was = role.status
    # Researched first: a role researched with no answers and no fit is scored all the same, since it is researched.
    role.analysis_state, role.analysed_at = DONE, datetime.now(UTC)
    from app.jobsearch.writing import packets  # imported here: the writer reads this module

    if packets.is_sent_back_skip(role.go_skip):  # the research Launch waited for is back
        role.go_skip = None
    await settle.roles(session, [role], cause="The analyst researched it", keep_gated_out=True)
    pct = scoring.percent_of(role, scoring.bounds(await profile_store.current(session, role.owner)))
    if role.status == stages.GATED_OUT and was != stages.GATED_OUT:
        sentence = f"{researched}; it failed a gate on the facts: {role.screened_out_reason}"
    elif role.status in stages.PRE_READY:
        sentence = f"{researched}: fit {pct}%." if pct is not None else f"{researched}."
    else:
        sentence = (f"{researched}; it stays in {stages.LABELS.get(role.status, role.status)}"
                    + (f", fit {pct}%." if pct is not None else "."))
    role_summary.want(role)  # a role researched in Second Impression or Ready to Launch is asked a summary
    found = role.analysis or {}
    detail = " ".join([found.get("notes") or "", *(f"For you: {x}" for x in found.get("for_owner") or [])]).strip()
    await trail.record(session, role.owner, event="analysed", actor="agent", agent_slug="scout", who="Analyst",
                       sentence=sentence, link=("opportunity", role.id), detail=detail or None)
    await session.flush()
    schedule.signal_after_commit(session)
    return {"stage": role.status}


registry.register(
    "jobs.analysis_gather", action_gather, inputs=["role"], outputs=["ready"],
    description="Checks the role is still before Ready to Launch and waiting for research.",
    source="backend/app/jobsearch/research/analysis.py")
registry.register(
    "jobs.analysis_fetch_text", action_fetch_text, inputs=["role"], outputs=["held", "how"],
    description="Puts the posting's text on the role before research: the text of its source, else the text it reads "
                "itself from the source's address and no other (never a job board).",
    source="backend/app/jobsearch/research/analysis.py")
registry.register(
    "jobs.analysis_source", action_source, inputs=["role"], outputs=["sourced"],
    description="Sources a role that is not Sourced in code where the app already holds what it takes: a posting on "
                "the employer's own job system or site, a stored link that is one posting, or the one match on a job "
                "system's board. Only a Sourced role is researched.",
    source="backend/app/jobsearch/research/analysis.py")
registry.register(
    "jobs.analysis_not_sourced", action_not_sourced, inputs=["role"], outputs=["state"],
    description="Leaves a role that is not Sourced where it is, not researched: its sourcing is tried again every "
                "night.",
    source="backend/app/jobsearch/research/analysis.py")
registry.register(
    "jobs.analysis_wait_for_text", action_wait_for_text, inputs=["role"], outputs=["state"],
    description="Leaves a role that holds no text of its posting waiting for it: it is not researched, and its asks "
                "stay unanswered, until the owner's browser or the app brings the text.",
    source="backend/app/jobsearch/research/analysis.py")
registry.register(
    "jobs.analysis_gate_facts", gate_facts.action_gate_facts, inputs=["role"], outputs=["stage", "state"],
    description="Reads the posting's place, way of working, pay, travel and employer in code and decides the cheap "
                "gates on them before the analyst is sent the role: a gate that fails sends the role to Gated out with "
                "the reason and the line of the posting it rests on. An unknown passes.",
    source="backend/app/jobsearch/gating/gate_facts.py")
registry.register(
    "jobs.analysis_settle", action_settle, inputs=["role"], outputs=["stage"],
    description="Works the ask out from the findings, then the score, then runs the gates again on the researched "
                "facts: Gated out when one fails; else the role stays where it is, researched, and the best "
                "researched roles go up to Ready to Launch.",
    source="backend/app/jobsearch/research/analysis.py")
registry.register(
    "jobs.plan_analysis", action_plan_round, inputs=["owner", "run"], outputs=["on", "waiting"],
    description="When Analyse the finds is switched on, sends the search's finds to the analyst, the youngest posting "
                "first, up to the mission's roles to research (-1: all).",
    source="backend/app/jobsearch/research/analysis.py")


def view(role: Opportunity) -> dict | None:
    """What the analyst found, for the role panel."""
    if not role.analysis_state:
        return None
    a = role.analysis or {}
    return {"state": role.analysis_state, "at": a.get("at"), "pay": a.get("pay"), "required": a.get("required", []),
            "preferred": a.get("preferred", []), "responsibilities": a.get("responsibilities", []),
            "notes": a.get("notes"), "for_owner": a.get("for_owner", []),
            "employer_facts_found": a.get("employer_facts_found", [])}
