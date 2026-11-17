"""The job-search tables: `opportunities` (every role, kept or screened out), `search_runs`, `profile_versions` (the
owner's criteria, gates, scoring and settings, every saved version) and `profile` (the old prose profile, kept as it
was last imported and no longer read).

`opportunities` holds the tracker's 25 columns (snake_case here, the tracker's own names on export: `tracker.py`)
and four more: `screened_out_reason`, `source_run`, `origin` (`tracker` for a row imported from the owner's
spreadsheet, `scout` for one a search found, `owner` for a job the owner added by its link) and `posted` (the
posting's own date when the scout could read it, else the age is counted from `found`). `match_key` (lowercased
`company|title`) is unique per owner: the same role
on two boards is one role, whichever board it came from. Whether a role is one the owner already has is asked wider than
that key, of the company and title in any writing and of the posting's link (`same_role.py`).
"""

import re
import uuid
from datetime import UTC, date, datetime
from typing import Any

from process_engine.types import UtcDateTime
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ColumnElement,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    and_,
    event,
    exists,
    false,
    func,
    inspect,
    or_,
    text,
    true,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, aliased, mapped_column
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm.base import PASSIVE_NO_INITIALIZE

from app import jsonsql
from app.database import Base, one_of
from app.jobsearch.stages import (
    ACTIVE_DISCUSSION,
    ENDED,
    FIRST_IMPRESSION,
    FOLLOWUP_NEEDED,
    LANDING_ZONE,
    PRE_APPLY,
    READY_TO_LAUNCH,
    SECOND_IMPRESSION,
    STAGES,
    default_end_reason,
)
from app.jobsearch.states import (
    BrowserKind,
    BrowserState,
    CardState,
    CheckScope,
    CheckState,
    CoachMode,
    FetchKind,
    FitsNoneKind,
    GapAnswer,
    GapKind,
    GapState,
    MissionState,
    PacketState,
    ParseKind,
    ParseState,
    PrepState,
    ProposalStatus,
    RoundKind,
    RunOutcome,
    SearchStatus,
    StartedBy,
    StopGate,
    StopStep,
    TabKind,
    TagJobStatus,
)
from app.sites import registry
from app.timeutil import aware, owner_today, utc_now


def _owner_day_of(column: str):
    """A column default: the owner's local day of the time in another column of the row being written (`created_at`,
    `first_seen`), worked out when the row is written and kept (a day is stored when it is recorded,
    not worked out again by the database from a time, whose zone it does not know). It reads the time that is given for
    the row, or the one its own default made (the column that holds it comes first), else now. A write that changes
    that time later moves the day with it (`_keep_days`). One statement that inserts several rows in one `VALUES` (the
    tracker import's) names its parameters in a way a default cannot read: its rows have the day it is now, which is the
    day of the time the import stamps them with."""

    def day(context) -> date:
        try:
            when = context.get_current_parameters().get(column)
        except KeyError:
            when = None
        return owner_today(aware(when) if when is not None else utc_now())

    return day


#: What `status` can hold: the stages of docs/PRD.md path 23 (stages.py), each saying whose move it is. A role a gate
#: stopped is kept in Gated out with its reason, so a gate that misfires shows up on the page instead of hiding.
STATUSES = STAGES

ORIGIN_TRACKER, ORIGIN_SCOUT, ORIGIN_OWNER = "tracker", "scout", "owner"
#: The origins of a role that is the owner's own data (imported from their tracker, or added by their link): the app
#: never closes such a role as noise.
OWN_ORIGINS = (ORIGIN_TRACKER, ORIGIN_OWNER)
#: The employer of a role the owner added by pasting its link (`add_link.py`) until research reads the posting and names
#: it (`analysis.submit`): the role's title is the link meanwhile. It is not an employer, and no record is made for it.
ADDED_LINK = "Added link"


class Opportunity(Base):
    __tablename__ = "js_opportunities"
    __table_args__ = (
        UniqueConstraint("owner", "match_key", name="uq_js_opportunities_owner_match_key"),
        Index("ix_js_opportunities_owner_status", "owner", "status"),
        Index("ix_js_opportunities_owner_favorite", "owner", sqlite_where=text("favorited_at IS NOT NULL")),
        # The reads every turn, ask and dispatcher pass repeats, each for the few roles of one state (migration 0139).
        Index("ix_js_opportunities_owner_waiting", "owner", "analysis_claimed_at",
              sqlite_where=text("analysis_state = 'waiting'")),
        Index("ix_js_opportunities_owner_done", "owner", sqlite_where=text("analysis_state = 'done'")),
        Index("ix_js_opportunities_owner_facts_pending", "owner",
              sqlite_where=text("json_extract(gate_facts, '$.state') = 'pending'")),
        # The stored order (`pipeline/stored_order.py`, migration 0151): a lane is read for the roles it shows, by the
        # number for the stages where the work is ahead (one list, or each stage of it) and by the age elsewhere.
        Index("ix_js_opportunities_owner_status_number", "owner", "status", text("queue_points DESC")),
        # Covers the columns the order reads, so a lane's page is sorted from the index and not from the rows (0156).
        Index("ix_js_opportunities_owner_status_order", "owner", "status", text("queue_points DESC"), "queue_moment",
              "queue_day", "created_at", "id"),
        # Covers the employer merge pass's counts and "via" reads, which need only these columns (0157).
        Index("ix_js_opportunities_owner_employer_company", "owner", "employer_id", "company"),
        Index("ix_js_opportunities_owner_ranked_number", "owner", text("queue_points DESC"),
              sqlite_where=text("status IN ('first_impression', 'second_impression', 'ready_to_launch')")),
        Index("ix_js_opportunities_owner_queue_until", "owner", "queue_until",
              sqlite_where=text("queue_until IS NOT NULL")),
        # Covers what a search matches and counts and the order of its page (migration 0164): the title, employer,
        # place, gate reason, status and ended reason, newest first, so the three reads of one search walk this and
        # not the wide rows (`pipeline/search.py`).
        Index("ix_js_opportunities_owner_search", "owner", text("created_at DESC"), "id", "title", "company",
              "location", "screened_out_reason", "status", "ended_reason",
              sqlite_where=text("owner <> ''"), postgresql_where=text("owner <> ''")),
        one_of("js_opportunities", "status", STATUSES),
        one_of("js_opportunities", "screened_out_by", StopGate),
        one_of("js_opportunities", "screened_out_step", StopStep),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    # --- the tracker's 25 columns, in the tracker's order ---
    slug: Mapped[str] = mapped_column(Text)  # the tracker's `id`: kebab-case company-short-title
    company: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=LANDING_ZONE, server_default=LANDING_ZONE)
    fit: Mapped[int | None] = mapped_column(Integer)
    level: Mapped[str | None] = mapped_column(Text)
    effort: Mapped[str | None] = mapped_column(Text)
    variant: Mapped[str | None] = mapped_column(Text)
    comp: Mapped[str | None] = mapped_column(Text)
    comp_source: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    move_type: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(Text)
    found: Mapped[date | None] = mapped_column(Date)
    board: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    ats: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    packet_complete: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    queue_rank: Mapped[int | None] = mapped_column(Integer)
    match_key: Mapped[str] = mapped_column(Text)
    fit_evidence: Mapped[str | None] = mapped_column(Text)
    col_adjusted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    col_adjusted_comp: Mapped[str | None] = mapped_column(Text)
    # --- what job search adds ---
    screened_out_reason: Mapped[str | None] = mapped_column(Text)
    #: Which gate gave that reason (`StopGate`), so the classifier need not read the sentence to know what it was about.
    screened_out_by: Mapped[str | None] = mapped_column(String(16))
    #: The rest of the stop as data (docs/PRD.md 39.12): the step that made it (`StopStep`), the id of the line the gate
    #: is (the page's id for it: `location`, `compensation:<role>`, or the id of the sentence or employer rule the owner
    #: wrote), and the versions of Hard pass and What to look for it was judged under. None on a stop made before they
    #: were kept, until the first re-gate works it out again from what is stored (`regate.py`).
    screened_out_step: Mapped[str | None] = mapped_column(String(10))
    screened_out_gate_id: Mapped[str | None] = mapped_column(String(80))
    screened_out_hard_pass: Mapped[int | None] = mapped_column(Integer)
    screened_out_criteria: Mapped[int | None] = mapped_column(Integer)
    source_run: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_search_runs.id", ondelete="SET NULL", name="fk_js_opportunities_source_run"),
        index=True)
    origin: Mapped[str] = mapped_column(String(10), default=ORIGIN_SCOUT, server_default=ORIGIN_SCOUT)
    posted: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    #: The owner's local day `created_at` falls on, stored when the role is made: the day a role no day dates is counted
    #: from where an order reads a day (`browser/reader.py`, the youngest roles first).
    created_day: Mapped[date] = mapped_column(Date, default=_owner_day_of("created_at"))
    # --- paths 20 and 21 ---
    #: The employer record this role is at (`Employer`), matched by name or alias.
    employer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_employers.id", ondelete="SET NULL", name="fk_js_opportunities_employer_id"), index=True)
    #: The fit, part by part: {base, base_why, applied: [{id, points, evidence, by}], not_applied: [{id, why}],
    #: total} (scoring.py). Empty for a role scored before structured scoring, or from the tracker.
    fit_breakdown: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict,
                                                server_default="{}")
    #: The reader's answer to each of the owner's sentence gates (Other, kind `rule`): [{id, answer: pass | fail |
    #: unknown, evidence}] (sentence_gates.py). A fail screened the role out with that gate as the reason. Empty for a
    #: role no reader judged. (No Python-side default: a row made before this column existed still inserts.)
    gate_answers: Mapped[list] = mapped_column(JSON().with_variant(JSON, "postgresql"), server_default="[]")
    #: The posting's text as it was read, kept after the posting comes down, and when it was read.
    description: Mapped[str | None] = mapped_column(Text)
    description_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: Whose copy the description is, when it is not the posting's own source's text (the owner, 2026-11-05, docs/PRD.md
    #: 48.14 f): `board` when the employer's own posting could not be read and a job board's copy of it stands in
    #: (the board's copy hook). None for every other description. The employer's text, when it comes, replaces a
    #: copy and takes the label with it (`postings/text.py`).
    description_from: Mapped[str | None] = mapped_column(String(10))
    #: The analyst's research (docs/PRD.md 23.3): none yet | waiting | done | failed, what it found (pay, required
    #: items classed Proven/Transferable/Unproven, notes), its process run, and when it was asked for and finished.
    analysis_state: Mapped[str | None] = mapped_column(String(10))
    analysis: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict,
                                           server_default="{}")
    analysis_instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    analysis_asked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When a session first took the role from the analyst's line (jobs.analysis): the stuck sweep's clock for a role
    #: that is with the analyst. None while it waits in line, however long (`analysis.settle_stuck`).
    analysis_claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    analysed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The versions of the inputs the research was made from, kept when it was taken (`staleness.stamp_analysis`):
    #: {resume, criteria, gates, scoring, instructions}, each the version in force then. None for research kept before
    #: this was recorded: the versions are unknown, and unknown is never stale by itself (`staleness.py`).
    analysis_made_under: Mapped[dict | None] = mapped_column(JSON().with_variant(JSON, "postgresql"))
    # --- path 30 ---
    #: The id of the owner's target role (`profile.TargetRole`) this role was found for, set by the title pass or the
    #: reader; none for a role found before target roles, until the one-off tagging gives it one (`tagging.py`).
    target_role: Mapped[str | None] = mapped_column(String(80))
    #: The version of What to look for in force when the role was matched to that target role (docs/PRD.md 31.4), so a
    #: later change to the tab can be seen. None for a role matched before versions were recorded, or not matched.
    matched_by_version: Mapped[int | None] = mapped_column(Integer)
    #: Which pass gave the role its target role (`work/role_match.py`): `code` (its title holds one target role's
    #: example title, when the role is made), `haiku` (the small title session; a "none" answer is kept too) or
    #: `analyst` (the Second Impression read, final). None: never decided. A pass writes over a lower one, never a
    #: higher one (`role_match.may_write`); written beside `target_role`.
    matched_by: Mapped[str | None] = mapped_column(String(10))

    # --- path 31 ---
    #: Why the role ended, once it is in an Ended stage (stages.END_REASONS): they_said_no, i_passed, posting_gone,
    #: no_reply or unknown; none for a role still in play. Set when the role moves to an Ended stage (`ended.py`, the
    #: board's move; `quiet.py`, a rule's move) and by the stage's own default (`stages.default_end_reason`) for a move
    #: made by anything else.
    ended_reason: Mapped[str | None] = mapped_column(String(20))

    # --- path 32 ---
    #: The job board link the role was found at (any job board, or any site in Where to look), kept for good, even
    #: once the employer's own posting is known (docs/PRD.md 32). `url` is the link the role is read and opened from:
    #: the employer's own posting once the app has found it, else this one, so with no employer link the two are the
    #: same address. None for a role found on the employer's own site, and for one found before this was recorded
    #: whose link is on no job board the app knows. Not `Employer.board_url`, which is the employer's own job system.
    board_url: Mapped[str | None] = mapped_column(Text)

    # --- the owner, 2026-10-15 ---
    #: True once the role has been in Active Discussion, the stage that means an interview. Set by the stage-move hook
    #: below whichever code moves the role there, and never cleared by the app, even when the role later ends. Shown on
    #: no screen: it is there to be found later.
    landed_interview: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), index=True)

    # --- the owner, 2026-10-15: Sourced ---
    #: The one posting of this role on its original source, the employer's careers site or job system (`employer`), a
    #: recruiter's posting its Apply button lands on (`recruiter`), or, for an Easy Apply with no link to follow, the
    #: board's own posting (`board`). A role is Sourced when `sourced_at` is set (`postings/sourced.py`); the job
    #: description is read only from this link, word for word.
    source_url: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str | None] = mapped_column(String(10))
    sourced_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: Why the browser gave up on this role after its tries (`browser_pace.TRIES`: "no posting on the employer's site"),
    #: shown on its card while it stays where it is; None while the browser is still trying, or never had to.
    browser_note: Mapped[str | None] = mapped_column(Text)
    #: What the gate-facts step read of the role's posting before anyone was paid to analyse it (`gate_facts.py`): the
    #: fingerprint of what it read, the facts it took (place, way of working, posted pay, travel, the employer the
    #: posting names) each with the line of the posting it rests on, and whether a short Haiku read is still to come.
    #: Empty for a role the step has not looked at.
    gate_facts: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict,
                                             server_default="{}")

    # --- the owner, 2026-10-16: Go's checks ---
    #: When Go's checks last looked at this role before its tab opened (`go_checks.py`: the owner's mail for an
    #: application already sent, and the posting for whether it is still open), and what the posting check found:
    #: `open` (open, or nothing said it closed) or `browser` (code could not read it, so a browser `read` item checks
    #: it). None for a role never checked.
    go_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When Go opened the role's tab: it stays in Ready to Launch, opened, until the mail confirms it or the owner marks
    #: it Applied.
    go_opened_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    go_check: Mapped[str | None] = mapped_column(String(10))
    #: Why Go skipped this role in Ready to apply (the employer gap, no apply link), in words; None when it was not
    #: skipped. The board leaves such a role out of the batch and its card says why; Go clears it the next time it
    #: walks the role and the role passes (the gap has ended, a link is there).
    go_skip: Mapped[str | None] = mapped_column(String(300))
    #: When the owner starred the role as a favorite; None when it is not one (`favorites.py`, migration 0112). A
    #: favorite is fast-tracked: read now, its packet asked once it is read, and it holds a Ready to Launch place until
    #: it is applied, closed or unstarred.
    favorited_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When the owner moved the role to Ready to Launch by hand while it had no Second Impression that graded its
    #: posting (`board.move`): it is not placed yet, it runs its impressions first as a star does (`favorites.advance`),
    #: and the owner's move is made once it is read (`favorites.place_asked`). None when there is no such ask: cleared
    #: when the role takes its place, and by the owner's next move of it (docs/PRD.md 24.13).
    ready_asked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # --- the stored order (`pipeline/stored_order.py`, migration 0151, docs/PRD.md 24.19) ---
    # What the lanes of the board are read by, kept for every role so that a lane is read for the roles it shows and not
    # for every role it holds. Worked out from the same facts as the card's number and age, by the same functions
    # (`deck.points`, `freshness.posting_hours`), and written by `stored_order.settle` alone; the triggers of migration
    # 0151 say which roles to look at again (`QueueDirty`). No loop reads them but the lanes.
    #: The role's number: what `deck.points` gives (the score or the rank, the first hours' bonus, less the stale
    #: penalty, never below 0), the one the list is sorted by.
    queue_points: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: What the age is counted from (`freshness.age_basis`): the day a role was posted, whole days as hours, or a moment
    #: (the time the app first saw the posting, or made the role), counted in hours. At most one is set; neither means
    #: no age, a role that is last.
    queue_day: Mapped[date | None] = mapped_column(Date)
    queue_moment: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When the number next changes of itself (the first hours end, the posting turns stale), or None when time changes
    #: nothing: the hour's tick and the engine's look settle the roles it has passed (a read of the board settles none).
    queue_until: Mapped[datetime | None] = mapped_column(UtcDateTime)


class QueueDirty(Base):
    """A role whose stored order may be out of date, or, with no role, every role of the owner (a saved profile or a
    changed list of job sites): written by the triggers of migration 0151 in the transaction of the change itself, read
    and deleted by `stored_order.settle`. Insert-only, with no foreign key and no unique index: a writer never waits on
    another writer, and a role listed twice is looked at once."""

    __tablename__ = "js_queue_dirty"
    __table_args__ = (Index("ix_js_queue_dirty_owner", "owner", "id"),)

    #: Arrival order only: ids come from a sequence and are not in commit order.
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(80))
    role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class SearchRun(Base):
    __tablename__ = "js_search_runs"
    #: One open search each owner: two starts at once cannot both be saved.
    __table_args__ = (
        Index("uq_js_search_runs_open", "owner", unique=True, sqlite_where=text("status = 'open'")),
        one_of("js_search_runs", "status", SearchStatus),
        one_of("js_search_runs", "started_by", StartedBy),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: `SearchStatus`: open (the scout has the task), done or failed
    status: Mapped[str] = mapped_column(
        String(10), default=SearchStatus.OPEN.value, server_default=SearchStatus.OPEN.value)
    #: `StartedBy`: schedule or manual
    started_by: Mapped[str] = mapped_column(
        String(10), default=StartedBy.MANUAL.value, server_default=StartedBy.MANUAL.value)
    focus: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The process instance that runs it (no foreign key: the engine's table is its own).
    instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: The scout's session, from its heartbeat, so the page can say it is alive.
    session_id: Mapped[str | None] = mapped_column(String(80))
    session_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: Roles the scout has reported, staged until it says it is done: a list of the scout's role dicts.
    reported: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    #: Counts when the run ends: {reported, duplicates, kept, screened_out}.
    counts: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    summary: Mapped[str | None] = mapped_column(Text)


def read_first() -> ColumnElement[bool]:
    """The roles that run their impressions first (SQL): a star (`favorited_at`), or a move to Ready to Launch the owner
    made before the role was read (`ready_asked_at`). The one rule of what the browser's queue sources first and what
    `favorites.advance` asks for."""
    return or_(Opportunity.favorited_at.is_not(None), Opportunity.ready_asked_at.is_not(None))


def is_read_first(role: Opportunity) -> bool:
    """Whether one role is read first (`read_first`, for a role in hand rather than a query)."""
    return role.favorited_at is not None or role.ready_asked_at is not None


def is_unnamed(role: Opportunity) -> bool:
    """Whether the role is one the owner added by its link that research has not named yet: its title is the link and
    its company says so (`ADDED_LINK`). Code that would make an employer record from a role's company leaves it out.
    Whatever the role's origin says: a role whose employer is still "Added link" is unnamed (some are marked
    as the scout's, which research and the writer must not pass over)."""
    return role.company == ADDED_LINK


#: The key of `Opportunity.analysis` that says the app gave up on a role's posting (`analysis.send_back`): the analyst's
#: findings graded nothing of the text it held, the role was sent back for its text once, and the text that came back
#: was graded as nothing again. The role stays waiting for its text (`analysis.NO_TEXT`) and nothing reads its source
#: for it any more, whatever its stage. It lives in the role's research, where `analysis.wait_for_text` leaves it, and
#: goes with the next research that is taken.
POSTING_NOT_FOUND = "posting_not_found"


def posting_not_found(role: Opportunity) -> bool:
    """Whether the app gave up on this role's posting (`POSTING_NOT_FOUND`)."""
    return bool((role.analysis or {}).get(POSTING_NOT_FOUND))


def _posting_given_up(role: Any) -> ColumnElement[bool]:
    return jsonsql.has(role.analysis, POSTING_NOT_FOUND)


def posting_still_sought() -> ColumnElement[bool]:
    """The roles whose posting the app has not given up on (SQL: the one rule of `posting_not_found`). Every reader of
    a role's text, and the sweep that puts a role that waited for its text back in line, asks it."""
    return ~_posting_given_up(Opportunity)


#: The key of `Opportunity.analysis` that says the deck has picked a followed-board role for its employer's posting to
#: be looked up (the owner, 2026-11-05, docs/PRD.md 32.3): a role in First Impression that holds the board's text and no
#: source, which the walk of the best estimates passed (`pipeline/deck._decide`). It is a place in line for the
#: browser's source item, never a place in line for a read: a role is marked `line.READ_NEXT` only once it is
#: Sourced, and the deck's turn takes this mark off when it is. Like `READ_NEXT` it lives in the role's research and
#: goes with the next research that is taken.
SOURCE_NEXT = "source_next"

#: What `Opportunity.description_from` says of a description that is a job board's copy of the posting.
DESCRIPTION_BOARD = "board"
#: What `Opportunity.source_kind` says of a role Sourced to a job board's own posting of it because the employer's could
#: not be read (the board's copy hook, `registry.board_reader("copy")`): a source the role will leave when the
#: employer's posting comes. Not `board` itself, which says the board's own listing is the only posting (an Easy Apply).
SOURCE_KIND_BOARD_COPY = "board_copy"


def is_board_copy(role: Opportunity) -> bool:
    """Whether the role's description is a job board's copy of its posting, put there because the employer's own could
    not be read (`Opportunity.description_from`, the board's copy hook), and so a text the employer's takes the
    place of."""
    return role.description_from == DESCRIPTION_BOARD


def _followed_board_host() -> re.Pattern[str] | None:
    """The host of a link to a board whose roles are followed to the employer's own site (a runbook's
    `follow_to_employer`), or one of its subdomains: `http` or `https`, then the host, then the end of the link or one
    of `/`, `:`, `?` and `#`. A test of the host, never of the text: an employer's own address that carries the board's
    name in a parameter is not the board's. None when no runbook is such a board. The database's expression
    (`registry.board_reader("sql")`, held to this one by a test) reads the same."""
    hosts = sorted({h for r in registry.current().runbooks if r.kind == "board" and r.follow_to_employer
                    for h in r.hosts})
    if not hosts:
        return None
    return re.compile(r"^https?://([a-z0-9-]+\.)*(" + "|".join(re.escape(h) for h in hosts) + r")([/:?#]|$)",
                      re.IGNORECASE)


def is_on_followed_board(role: Opportunity) -> bool:
    """Whether the role was found on a board whose roles are followed to the employer's own site: the host of its board
    link (`board_url`, kept for good), else of its own link (`url`), is the board's. The role's own link is the
    employer's once it is Sourced; its board link stays the board's. This is where the role was found, not whether it
    has a board page to read (`board_listed`)."""
    link = (role.board_url or "").strip() or (role.url or "").strip()
    found = _followed_board_host()
    return found is not None and found.match(link) is not None


def on_followed_board() -> ColumnElement[bool]:
    """`is_on_followed_board`, as a condition of a query (SQL)."""
    link = func.coalesce(func.nullif(func.trim(Opportunity.board_url), ""), func.trim(Opportunity.url), "")
    sql = registry.board_reader("sql")  # the board's own reader holds the query; none here, no role is the board's
    return sql.on_host(link) if sql is not None else false()


def source_next_marked() -> ColumnElement[bool]:
    """The roles the deck has picked for their employer's posting to be looked up (SQL): First Impression roles carrying
    `SOURCE_NEXT`. A mark on a role that has left First Impression means nothing, as `line.marked_next` does."""
    return and_(Opportunity.status == FIRST_IMPRESSION, jsonsql.has(Opportunity.analysis, SOURCE_NEXT))


def page_read(page_reader: str) -> ColumnElement[bool]:
    """A posting whose own board page was read (SQL, on `JobPosting`): it has a raw copy by the page reader
    (`page_reader`, `raw_copies.board_page()`), whatever that read showed, filed under the posting's own source and id
    as every raw copy is."""
    return exists().where(RawPosting.owner == JobPosting.owner, RawPosting.source_id == JobPosting.employer_id,
                          RawPosting.ext_id == JobPosting.ext_id, RawPosting.reader == page_reader)


def board_unread_posting(source: uuid.UUID, page_reader: str) -> ColumnElement[bool]:
    """A posting the app's code could read the page of (SQL, on `JobPosting`): one the nightly board search made,
    filed under the owner's board source id (`source`), that belongs to a role and is still open, and whose own page
    nobody has read: no raw copy by the page reader (`page_reader`, `raw_copies.board_page()`), whatever that read
    showed. The one rule of which posting of a role is read (the board's capture plan is the same rule as a walk)."""
    return and_(JobPosting.employer_id == source, JobPosting.opportunity_id.is_not(None), JobPosting.is_open,
                ~page_read(page_reader))


def board_link_unread_posting(page_reader: str) -> ColumnElement[bool]:
    """A posting of a role that holds a board job's own page as its link and that nobody has read the page of (SQL,
    on `JobPosting`): the posting the tracker's import made of the link the owner saved (`ats` import), the scout's
    report of it, or a link the owner pasted (`ats.OF_ROLES`), for a role whose employer's text cannot be read and so
    needs the board's copy (the board's copy hook, the owner, 2026-11-05). The nightly search's own postings are
    not these (`board_unread_posting`). Its raw copy by the page reader (`page_reader`) is filed under the posting's
    own source and id, as every raw copy is, whatever the page it was read at."""
    from app.jobsearch import ats  # it names the postings of roles; imported here, it reads no model

    sql = registry.board_reader("sql")
    on_page = sql.on_job_page(JobPosting.url) if sql is not None else false()
    return and_(JobPosting.ats.in_(ats.OF_ROLES), JobPosting.opportunity_id.is_not(None), JobPosting.is_open,
                on_page, ~page_read(page_reader))


def board_listed(source: uuid.UUID, page_reader: str) -> ColumnElement[bool]:
    """The roles whose board page the app's code reads for their text (SQL, docs/PRD.md 48.14): found on the board
    (`on_followed_board`) and holding a posting of the nightly search that nobody has read the page of
    (`board_unread_posting`). A role linked to the board with no such posting (the scout's, one added by its link, an
    import) is not listed, and neither is one whose postings are all read: they keep the sourcing the queue made for
    them before pages were read, so no role is covered by neither."""
    return and_(on_followed_board(), exists().where(JobPosting.opportunity_id == Opportunity.id,
                                                  board_unread_posting(source, page_reader)))


def wants_sourcing() -> ColumnElement[bool]:
    """The roles whose original posting the app may go and get (SQL): the list the browser's queue
    (`browser/reader.board_roles`) and the code's own sourcing step (`postings/sourced.source_held`) both start from,
    through `postings/chase.wanted`, so a role one of them takes the other takes too. A role on deck (Second
    Impression); a role read first (`read_first`: a star or a move to Ready to Launch before it was read) at any stage
    before the application (the owner, 2026-10-28: a star runs the impressions first, and it needs its posting before
    either); a role in Followup needed (2026-11-03): it has no usable text of its posting, so it is never ranked or
    put on deck, and its original posting is where its text is; and a followed-board role in First Impression that the
    deck has picked for its employer's posting to be looked up (`SOURCE_NEXT`, 2026-11-05): it holds the board's text,
    which ranks it, and Sonnet reads the employer's, so the posting is looked for at the read-next mark and not before.
    Every role in Followup needed is here: `chase.wanted` leaves out the ones too old to be worth the chase (the owner,
    2026-11-04), which SQL cannot, as a role's age is worked out from its postings. Whether it is Sourced already, and
    has a link to open, are the caller's to ask."""
    first = and_(read_first(), Opportunity.status.in_(PRE_APPLY))
    looked_up = and_(source_next_marked(), on_followed_board())
    return or_(Opportunity.status.in_((SECOND_IMPRESSION, FOLLOWUP_NEEDED)), first, looked_up)


def posting_of_a_role_given_up() -> ColumnElement[bool]:
    """The postings of a role the app gave up on (SQL, for a query on `JobPosting` that joins no role: the same rule as
    `posting_still_sought`, asked of the posting's own role). A posting of no role is not one."""
    gone = aliased(Opportunity)
    return exists().where(gone.id == JobPosting.opportunity_id, _posting_given_up(gone))


__all__ = ["Opportunity", "SearchRun"]


class ProfileVersion(Base):
    """One saved version of one of the owner's profile documents (criteria, gates, scoring, settings:
    `profile/schema.py`).
    A save never changes a row: it adds the next version, with who saved it and why, so every earlier one can be read
    and brought back. The newest version is the one in force.

    Portable types only (`Uuid`, `JSON`, stored as JSON on Postgres), so the table also runs on a small embedded
    database (docs/PRD.md 2.5)."""

    __tablename__ = "profile_versions"
    __table_args__ = (
        UniqueConstraint("owner", "kind", "version", name="uq_profile_versions_owner_kind_version"),
        Index("ix_profile_versions_owner_kind", "owner", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"))
    #: Who saved it: the owner, an agent by its name, "import" or "restore".
    saved_by: Mapped[str] = mapped_column(String(80))
    why: Mapped[str] = mapped_column(Text, default="", server_default="")
    saved_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class InstructionFile(Base):
    """One saved version of one instruction or template file an agent is sent (`app/instructions`, `app/templates`).
    A save never changes a row: it adds the next version, with who saved it and why, so every earlier one can be read
    and brought back. The newest version is the one in force; with no row at all, the file shipped on disk is (version
    1). Version 1 is written the first time the owner saves an edit, holding the shipped text that edit was made from.

    `text` is what an agent reads (the file's header comment already dropped). `shipped_sha` is the fingerprint of the
    shipped file when this version was saved, so a release that ships a different one is noticed
    (`app.jobsearch.work.instruction_files`). Portable types only."""

    __tablename__ = "js_instruction_files"
    __table_args__ = (
        UniqueConstraint("owner", "kind", "name", "version", name="uq_js_instruction_files_version"),
        Index("ix_js_instruction_files_file", "owner", "kind", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: instructions | templates
    kind: Mapped[str] = mapped_column(String(12))
    #: The file's name without its suffix (`jobsearch.scout`).
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    shipped_sha: Mapped[str] = mapped_column(String(64))
    #: Who saved it: the owner's name, or "shipped" for version 1.
    saved_by: Mapped[str] = mapped_column(String(80))
    why: Mapped[str] = mapped_column(Text, default="", server_default="")
    saved_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


#: A packet's states (`PacketState`): the writer is on it, it passed every check (the role is Ready to Apply), the
#: checks held it (the role stays Needs Drafted, flagged), the writer never sent it, or a newer version replaced it.
PACKET_STATES = tuple(PacketState)


class Packet(Base):
    """One version of a role's application packet (docs/PRD.md path 10): the tailored resume, the cover letter when
    one is warranted, the comp ask, why each choice was made, and what the checks found. A request for changes makes
    the next version; no version is ever changed after it is written. Portable types only."""

    __tablename__ = "js_packets"
    __table_args__ = (
        UniqueConstraint("role_id", "version", name="uq_js_packets_role_version"),
        Index("ix_js_packets_owner_status", "owner", "status"),
        # One packet being written for a role: two asks at once cannot both be saved.
        Index("uq_js_packets_writing", "role_id", unique=True, sqlite_where=text("status = 'writing'")),
        one_of("js_packets", "status", PACKET_STATES),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_opportunities.id", ondelete="CASCADE", name="fk_js_packets_role_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(12), default=PacketState.WRITING.value, server_default=PacketState.WRITING.value)
    #: What the owner asked to change, for a version made on request.
    feedback: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: What the writer was handed, kept as sent.
    brief: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    resume: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    letter: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    comp: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    reasoning: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    checks: Mapped[list] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=list)
    #: How many times the writer's submission was refused by the checks before this one was taken.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    written_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


#: How an employer works, as its postings and policy say.
WORK_STYLES = ("remote", "hybrid", "onsite", "mixed", "unknown")
OWNERSHIPS = ("public", "private", "pe", "vc", "nonprofit", "government", "unknown")


class Employer(Base):
    """An employer the app knows (docs/PRD.md path 21): the facts that filter and score its roles, each with where it
    came from. Facts the owner edited are theirs: research never overwrites them. Portable types only."""

    __tablename__ = "js_employers"
    __table_args__ = (
        UniqueConstraint("owner", "key", name="uq_js_employers_owner_key"),
        one_of("js_employers", "fetch_kind", FetchKind),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: The matching key: the name lowercased, without "Inc", "LLC", "The" and punctuation (employers.key_of).
    key: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=list)
    website: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)
    #: Its page on an employer-review site, a link only (docs/PRD.md 28.13, 36.2): an https address that a public web
    #: search showed (`employers.reviews_link`). Nothing of that page is read or kept.
    reviews_url: Mapped[str | None] = mapped_column(Text)
    headquarters: Mapped[str | None] = mapped_column(Text)
    #: Offices or places it hires near, as written ("Oakfield, ST").
    offices: Mapped[list] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=list)
    #: Whether it has a place in the owner's home area (from its offices and its roles' places).
    local: Mapped[bool | None] = mapped_column(Boolean)
    work_style: Mapped[str] = mapped_column(String(10), default="unknown", server_default="unknown")
    employees: Mapped[int | None] = mapped_column(Integer)
    founded: Mapped[int | None] = mapped_column(Integer)
    industry: Mapped[str | None] = mapped_column(Text)
    sectors: Mapped[list] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=list)
    ownership: Mapped[str] = mapped_column(String(12), default="unknown", server_default="unknown")
    ai_native: Mapped[bool | None] = mapped_column(Boolean)
    #: Dated events: [{kind: "layoff" | ..., date: "YYYY-MM", note, source}].
    events: Mapped[list] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=list)
    #: On the owner's watchlist: its own careers page is checked directly.
    #: Roles from this employer are gated out (the owner's switch on its page), with the owner's reason and when.
    gated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    gated_reason: Mapped[str | None] = mapped_column(Text)
    gated_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    check_directly: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    group: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    #: The job system it posts on (ats.SYSTEMS, docs/PRD.md 28.1), the parts that address its board, a link to the
    #: board, and how the system was found (ats_found_by, set in resolve.py).
    ats: Mapped[str | None] = mapped_column(String(20))
    board: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    board_url: Mapped[str | None] = mapped_column(Text)
    ats_found_by: Mapped[str | None] = mapped_column(String(10))
    #: The last time its board was read, whether that worked (with why not), and how many roles were open then.
    fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    fetch_ok: Mapped[bool | None] = mapped_column(Boolean)
    fetch_note: Mapped[str | None] = mapped_column(Text)
    #: What that attempt concluded, for the code to decide on (`FetchKind`); the note says it in words.
    fetch_kind: Mapped[str | None] = mapped_column(String(16))
    open_roles: Mapped[int | None] = mapped_column(Integer)
    #: When its board was last read in full for the owner's places, whatever else the read asked for or left out, and a
    #: fingerprint of what that read asked (the job system and address of the board, and the owner's places:
    #: `postings.read_key`). A job system with filters is read in full a week after that, and on the first night the
    #: fingerprint no longer matches; between those nights it is asked only for what is new (docs/PRD.md 39.5, 39.24).
    read_full_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    read_asked: Mapped[str | None] = mapped_column(String(40))
    #: Where each fact came from: {field: {source, at, by}}. Beside the facts, the app's marks of its dealings with the
    #: scout (`doors/tools.py`): `board_asked` {at}, when the scout answered for its job board (the mark stands until
    #: the app's reading of the employer's site fails for good: `employers.core.ask_scout_again`), and `board_offered`
    #: {night, search}, the scout night and search that last handed it to the scout.
    sources: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    #: The fields the owner set by hand, which research leaves alone.
    owner_fields: Mapped[list] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=list)
    #: The employer researcher's work on this record (docs/PRD.md path 36): None | waiting (in its line, or with a
    #: session) | done | failed (a session never sent it, or it was refused too often). When it was put in line, when a
    #: session first took it (the stuck time runs from then), the process instance that waits for the report, when the
    #: last report was taken (`researched_at`: the nightly mission skips an employer researched lately), and what that
    #: report came to (the facts set and the facts refused with why, the merges and splits it proposed, the
    #: roles whose gates ran again).
    research_state: Mapped[str | None] = mapped_column(String(10))
    research_asked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    research_claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    research_instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    researched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    research: Mapped[dict | None] = mapped_column(JSON().with_variant(JSON, "postgresql"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, onupdate=utc_now)


class EmployerListEntry(Base):
    """One row of the employer list the owner loaded (docs/PRD.md 28.3; `employers/employer_list.py`): an employer's
    name and website as the owner's file gave them, under the file's name (`source`). A lookup, nothing more: no row is
    an employer, and the website is the owner's word, not a fact the app has checked. Loading the file again replaces
    that file's rows. No foreign keys: a row names an employer by `key` (`employers.key_of`), which may be the key of
    an employer the app does not have yet. Portable types only."""

    __tablename__ = "js_employer_list"
    __table_args__ = (Index("ix_js_employer_list_owner_key", "owner", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: The file's name (no folder), which says which rows a new load of it replaces.
    source: Mapped[str] = mapped_column(Text)
    #: The matching key of `name` (`employers.key_of`), the one an employer record is stored under.
    key: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(Text)
    #: A public web address (`web_address.http_url`), as the file gave it.
    website: Mapped[str] = mapped_column(Text)
    #: What the file's verdict column said, when it has one; rows it says to drop are never kept.
    verdict: Mapped[str | None] = mapped_column(String(40))
    imported_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


MISSION_STATES = tuple(MissionState)


class Mission(Base):
    """A piece of standing work (docs/PRD.md path 18): what it is for, the playbook it follows, when it comes up and
    what it runs with. Missions are data: a new kind of standing work is a new playbook and a new row. Portable
    types."""

    __tablename__ = "js_missions"
    __table_args__ = (
        UniqueConstraint("owner", "key", name="uq_js_missions_owner_key"),
        one_of("js_missions", "state", MISSION_STATES),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: What the app knows the mission by and runs: scout_boards | forge_packets | sweep_stuck (missions.KINDS).
    key: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text, default="")
    #: The process it follows (a process key How they work draws), or None for app-only work.
    playbook: Mapped[str | None] = mapped_column(String(80))
    #: {"kind": "every", "hours": 24} | {"kind": "daily", "at": "02:00"} | {"kind": "on_demand"}
    schedule: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    settings: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    state: Mapped[str] = mapped_column(String(10), default=MissionState.ON.value)
    paused_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When the schedule last looked at it (a sweep that closes nothing leaves no run, only this).
    checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class MissionRun(Base):
    """One time a mission ran: who started it, what it started (a search, packets) and how it ended."""

    __tablename__ = "js_mission_runs"
    __table_args__ = (one_of("js_mission_runs", "outcome", RunOutcome),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    mission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_missions.id", ondelete="CASCADE", name="fk_js_mission_runs_mission_id"), index=True)
    #: schedule | you
    started_by: Mapped[str] = mapped_column(String(20), default="schedule")
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: `RunOutcome`: running, done, failed or skipped. A run that started a search or packets is running until they end.
    outcome: Mapped[str] = mapped_column(String(10), default=RunOutcome.RUNNING.value)
    summary: Mapped[str] = mapped_column(Text, default="")
    #: What it started: {"search_run": id} or {"packets": [ids]}; the process instances it started, to open live.
    started: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)


class HandOut(Base):
    """An item the app claimed for the runner that asked for work (`hand_outs.py`, docs/PRD.md 47): the kind of session
    and the exact items it is for (ids, as text), on a lease. The session started for it names the claim (`X-Claim`) and
    its own take-next call returns those items for it, so a session never starts empty and no two sessions are
    handed the same item. `session_id` is set when the session takes it; until then the items are held back from
    every other ask."""

    __tablename__ = "js_hand_outs"
    __table_args__ = (Index("ix_js_hand_outs_owner_kind", "owner", "kind"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    items: Mapped[list] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=list)
    runner: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    lease_until: Mapped[datetime] = mapped_column(UtcDateTime)
    session_id: Mapped[str | None] = mapped_column(String(80))
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class OpenTab(Base):
    """A tab the app's Chrome has open for the app (docs/PRD.md path 52, `browser/tabs.py`): its DevTools target id,
    whose it is (a browser session's id, a code job's name, or `go`), the kind, the role of a Launch tab, and when it
    was opened. The bridge and the app's own code write a row when a tab opens and delete it when the tab closes;
    cleanup follows the rows."""

    __tablename__ = "js_open_tabs"
    __table_args__ = (
        Index("ix_js_open_tabs_owner_id", "owner_id"),
        Index("ix_js_open_tabs_role_id", "role_id"),
        one_of("js_open_tabs", "kind", TabKind),
    )

    target_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner: Mapped[str] = mapped_column(String(80))
    owner_id: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(10))
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_opportunities.id", ondelete="SET NULL", name="fk_js_open_tabs_role_id"))
    opened_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class MissionChange(Base):
    """Every change to a mission (paused, resumed, schedule or settings changed), with who and why."""

    __tablename__ = "js_mission_changes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    mission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_missions.id", ondelete="CASCADE", name="fk_js_mission_changes_mission_id"), index=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    by: Mapped[str] = mapped_column(String(80))
    what: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text, default="")


class StageMove(Base):
    """One change of a role's status (its stage), written for every change wherever it happens, so the pipeline can
    say how each stage moved in the last day. `from_status` is empty when the role was just found."""

    __tablename__ = "js_stage_moves"
    __table_args__ = (
        Index("ix_js_stage_moves_owner_at", "owner", "at"),
        # The moves of one role (migration 0145): where a gate sent it from, when it became Applied, when the owner's
        # line was last overtaken by a move. Without it each of those read every move of the owner's.
        Index("ix_js_stage_moves_role_at", "role_id", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_opportunities.id", ondelete="CASCADE", name="fk_js_stage_moves_role_id"))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


#: A key of a session's `info`: while it holds a time, the stage moves the hook below records are stamped with it
#: instead of the clock at each row (the tracker import's moves share its one time; the next import knows them by it).
STAGE_MOVES_AT = "stage_moves_at"


@event.listens_for(OrmSession, "before_flush")
def _record_stage_moves(session: OrmSession, _flush_context, _instances) -> None:
    """Every new role and every status change becomes a StageMove, whichever code made it: the board, a packet, an
    agent. Nothing has to remember to call this."""
    stamped = {"at": session.info[STAGE_MOVES_AT]} if STAGE_MOVES_AT in session.info else {}
    for obj in list(session.new):
        if isinstance(obj, Opportunity):
            if obj.id is None:
                obj.id = uuid.uuid4()
            to = obj.status or Opportunity.__table__.c.status.default.arg
            session.add(StageMove(owner=obj.owner, role_id=obj.id, from_status=None, to_status=to, **stamped))
            if to == ACTIVE_DISCUSSION:
                obj.landed_interview = True
            if to in ENDED and not obj.ended_reason:
                obj.ended_reason = default_end_reason(to)
    for obj in list(session.dirty):
        if isinstance(obj, Opportunity):
            history = inspect(obj).get_history('status', PASSIVE_NO_INITIALIZE)
            if history.added and history.deleted and history.added[0] != history.deleted[0]:
                session.add(StageMove(
                    owner=obj.owner, role_id=obj.id, from_status=history.deleted[0], to_status=history.added[0],
                    **stamped,
                ))
                if history.added[0] in (SECOND_IMPRESSION, READY_TO_LAUNCH):
                    from app.jobsearch.research import role_summary  # it reads this module

                    role_summary.want(obj)  # a researched role with no current summary is asked for one, once
                if history.added[0] == ACTIVE_DISCUSSION:
                    obj.landed_interview = True  # only ever set here: a later move out of the stage leaves it true
                # An Ended role says why it ended (docs/PRD.md 31.7); one that is back in play no longer does.
                if history.added[0] in ENDED:
                    obj.ended_reason = obj.ended_reason or default_end_reason(history.added[0])
                else:
                    obj.ended_reason = None
                if history.deleted[0] == READY_TO_LAUNCH:
                    obj.go_skip = None  # Launch's skip reason is about Ready to Launch: it is not true elsewhere


class ParseJob(Base):
    """A text the owner gave so a small session can split it into fields: a job posting (into a target role's fields:
    "Fill from a posting" on What to look for) or their resume (into the master resume's: "Import your resume" on Me);
    `jobs/parse.py`. waiting (no session has it) -> reading (a session took it) -> done | failed. The text is dropped
    once the job ends, and the row after a day."""

    __tablename__ = "js_parse_jobs"
    __table_args__ = (
        Index("ix_js_parse_jobs_owner_state", "owner", "state"),
        one_of("js_parse_jobs", "kind", ParseKind),
        one_of("js_parse_jobs", "state", ParseState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: What the text is: `posting` or `resume`.
    kind: Mapped[str] = mapped_column(
        String(10), default=ParseKind.POSTING.value, server_default=ParseKind.POSTING.value)
    #: The target role's tab it was asked from, so the page puts the answer back where it came from.
    role_id: Mapped[str | None] = mapped_column(String(80))
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    state: Mapped[str] = mapped_column(
        String(10), default=ParseState.WAITING.value, server_default=ParseState.WAITING.value)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The fields as the session reported them: for a posting, name, titles, description, responsibilities,
    #: qualifications, preferred and compensation; for a resume, `resume_parse.FIELDS`.
    fields: Mapped[dict | None] = mapped_column(JSON)
    #: Why it failed, in a plain sentence for the owner.
    error: Mapped[str | None] = mapped_column(Text)


class JobPosting(Base):
    """A posting in the log (docs/PRD.md 28.8 and path 39): one read from an employer's own board, a site or the owner's
    browser, or the posting of a role the scout reported (`ats` `scout`, 39.4) or the owner's tracker held (`ats`
    `import`, 39.9), so that every role has one. Kept with when it was first and last seen and when it closed, every
    link it appeared under, and the system's own id. A posting becomes a role only when it passes the owner's filter;
    one the filter stops keeps the rule that stopped it. A posting is open while its board still lists it and the app
    still reads that board (`is_open`): it is closed when a read finds it gone, and Not checked (`not_checked_at`) when
    the app no longer reads its board."""

    __tablename__ = "js_postings"
    __table_args__ = (
        UniqueConstraint("owner", "employer_id", "ext_id", name="uq_js_postings_owner_employer_ext"),
        Index("ix_js_postings_owner_match_key", "owner", "match_key"),
        Index("ix_js_postings_owner_first_seen", "owner", "first_seen"),
        # The queue's probes of owed work (migration 0141): the postings that wait for the filter, and the ones that
        # passed it and wait for a role. Each holds only those, a handful at rest, so asking "is anything owed?" reads
        # none of the table's rows.
        Index("ix_js_postings_owner_unfiltered", "owner",
              sqlite_where=text("filtered_at IS NULL AND opportunity_id IS NULL AND closed_at IS NULL "
                                "AND not_checked_at IS NULL")),
        Index("ix_js_postings_owner_roles_waiting", "owner",
              sqlite_where=text("route = 'roles' AND filtered_at IS NOT NULL AND filtered_by IS NULL "
                                "AND opportunity_id IS NULL AND closed_at IS NULL AND not_checked_at IS NULL")),
        # The postings a rule stopped before a role was made of them, by the rule (migration 0162): what Search Settings
        # counts per line (`GET /profile/gates/stopped`) without reading the owner's whole table.
        Index("ix_js_postings_owner_stopped", "owner", "stop_gate_id",
              sqlite_where=text("opportunity_id IS NULL AND stop_gate_id IS NOT NULL"),
              postgresql_where=text("opportunity_id IS NULL AND stop_gate_id IS NOT NULL")),
        # The postings of roles that have a current copy, with the copy: what `postings/cleansed.py` reads for every
        # role without opening the table (a posting with no role, the many a search finds, is not in it).
        Index("ix_js_postings_owner_role_copy", "owner", "opportunity_id", "current_cleansed_id", "id",
              sqlite_where=text("opportunity_id IS NOT NULL AND current_cleansed_id IS NOT NULL")),
        # The postings of the roles a lane shows (`freshness.held_postings`: `owner = ? AND opportunity_id IN (...)`).
        # With no statistics SQLite chose the owner's match-key index and read every posting of the owner, once per lane
        # (a long read for each of a board's eight lanes). This one holds both columns, and
        # only the postings that have a role (migration 0155).
        Index("ix_js_postings_owner_role", "owner", "opportunity_id", sqlite_where=text("opportunity_id IS NOT NULL")),
        # The postings of roles that no target role claims, by owner and first seen (migration 0163): what "Fits none of
        # my roles" counts for this week (`jobs/fits_none.py`), reading the week's unclaimed postings only.
        Index("ix_js_postings_owner_week_unclaimed", "owner", "first_seen", "opportunity_id",
              sqlite_where=text("target_role IS NULL AND opportunity_id IS NOT NULL"),
              postgresql_where=text("target_role IS NULL AND opportunity_id IS NOT NULL")),
        # The open postings no role was made of, with what a search matches, counts and shows (migration 0164), newest
        # first: a search reads this index and not the table's wide rows (`pipeline/search.py`).
        Index("ix_js_postings_owner_search", "owner", text("first_seen DESC"), "id", "title", "company", "location",
              "filtered_by", "route", "filtered_at", "ats", "judged_by", "work_mode", "url", "apply_url",
              "opportunity_id", "closed_at", "not_checked_at",
              sqlite_where=text("opportunity_id IS NULL AND closed_at IS NULL AND not_checked_at IS NULL "
                                "AND owner <> ''"),
              postgresql_where=text("opportunity_id IS NULL AND closed_at IS NULL AND not_checked_at IS NULL "
                                    "AND owner <> ''")),
        one_of("js_postings", "stop_gate", StopGate),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: The employer whose board the posting was read from, or, for a posting a browser read from a site, that site's id:
    #: the browser reader files its postings under the site so thousands of names do not land on the Employers screen.
    #: It names one of two tables, so it has no foreign key.
    employer_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    ats: Mapped[str] = mapped_column(String(20))
    ext_id: Mapped[str] = mapped_column(Text)
    match_key: Mapped[str] = mapped_column(Text, default="", server_default="")
    company: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="", server_default="")
    location: Mapped[str | None] = mapped_column(Text)
    work_mode: Mapped[str | None] = mapped_column(String(20))
    comp: Mapped[str | None] = mapped_column(Text)
    comp_min: Mapped[int | None] = mapped_column(Integer)
    comp_max: Mapped[int | None] = mapped_column(Integer)
    #: The employer's own date for it, when its system gives one; first_seen is the date the app trusts.
    posted: Mapped[date | None] = mapped_column(Date)
    url: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    #: The description as plain text; empty until its detail is read, for systems whose list leaves it out.
    text: Mapped[str | None] = mapped_column(Text)
    detail_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    first_seen: Mapped[datetime] = mapped_column(UtcDateTime)
    #: The owner's local day `first_seen` falls on (the day the app first saw the posting), stored when the posting is
    #: written and moved with `first_seen`: the day a card's order counts a posting from when its employer gave no date
    #: (`pipeline/row.py`, `pipeline/from_posting.py`), which the database cannot work out from the time.
    seen_day: Mapped[date] = mapped_column(Date, default=_owner_day_of("first_seen"))
    last_seen: Mapped[datetime] = mapped_column(UtcDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When the posting was marked Not checked (docs/PRD.md 39.8): the board it came from is no longer read (the owner
    #: switched the employer, the site or its job system off, the app turned the site off, or the employer is gated), so
    #: nothing is checking whether the employer still lists it. It is not closed either: nothing says the employer took
    #: it down, and `last_seen` still says the day it was last seen. A read of the board that sees it again leaves it
    #: open, and a whole read that does not see it closes it; either way this goes (`postings.store`). No stage works a
    #: posting that has it set (`is_open`).
    not_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    links: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    #: The role it became once it passed the owner's filter, or the rule that stopped it and when. Indexed: the browser
    #: read's lists ask of each role whether a posting of it holds a text (migration 0081).
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("js_opportunities.id", ondelete="SET NULL", name="fk_js_postings_opportunity_id"), index=True)
    filtered_by: Mapped[str | None] = mapped_column(Text)
    filtered_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The classifier's probability that it fits the owner (docs/PRD.md 29.6), when the owner has a model, kept to rank
    #: the posting and for fit's "unknown" ask weight, and where the filter sent it: titles (every posting that passes
    #: the gates waits for a tab's title check, 31.1) | reader (a tab said yes, or the owner asked: a Haiku agent reads
    #: it whole) | none for a posting the filter stopped or has not looked at. route_why says why, in words.
    fit_prob: Mapped[float | None] = mapped_column(Float)
    route: Mapped[str | None] = mapped_column(String(10))
    route_why: Mapped[str | None] = mapped_column(Text)
    #: The owner said "read it anyway" to a stop (docs/PRD.md 30.2): the classifier counts the posting as a positive
    #: example the next time it trains.
    owner_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    #: The id of the owner's target role (`profile.TargetRole`) the title pass said it matches (docs/PRD.md 30.5); a
    #: role made from it takes the id, unless the reader that read it names another.
    target_role: Mapped[str | None] = mapped_column(String(80))
    #: Which step judged it last, and the version of What to look for (the criteria document) in force when it did
    #: (docs/PRD.md 31.1 and 31.4): `gate` | `titles` for a stop (the step that stopped it), `titles` for a posting a
    #: tab's title check said yes to, `you` for one the owner asked to read anyway. None for a posting nobody has
    #: judged yet. The version is what a re-gate compares against: a stop made under an older version may come
    #: out differently now. A stop made before versions were recorded has a step and no version. (`classifier` was a
    #: step until 2026-10-13, when the classifier stopped stopping: a closed posting it stopped still says so.)
    judged_by: Mapped[str | None] = mapped_column(String(10))
    judged_by_version: Mapped[int | None] = mapped_column(Integer)
    #: When the title check last answered for this posting (set from migration 0078 on; None before, and for a posting
    #: the title check never judged). The Pipeline's "stopped today" counts the title check's stops by it.
    judged_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The rest of a stop as data (docs/PRD.md 39.12), beside the sentence in `filtered_by`, the step in `judged_by`
    #: and the version of What to look for in `judged_by_version`: which gate it was (`StopGate`), the id of the line
    #: the gate is (`location`, `compensation:<role>`, an id the owner wrote, or the avoided site's host) and the
    #: version of Hard pass it was judged under. All three are set and cleared with `filtered_by`. None on a stop made
    #: before they were kept, until the first re-gate works it out again from what is stored (`regate.py`).
    stop_gate: Mapped[str | None] = mapped_column(String(16))
    stop_gate_id: Mapped[str | None] = mapped_column(String(80))
    stop_hard_pass: Mapped[int | None] = mapped_column(Integer)
    #: The tab a title-pass "no" found closest (docs/PRD.md 31.8), so the Gated stage can be narrowed to one tab and
    #: the tab's numbers can say what it stopped. Kept beside `target_role` and not in it: that id says the posting
    #: *matches* a tab (a role made from it takes it, and a reader reads it against that tab), and a posting that was
    #: stopped matches none. None for a stop that is not a title-pass "no", for one made before this was recorded, and
    #: for a "no" the session named no tab for.
    nearest_role: Mapped[str | None] = mapped_column(String(80))
    #: The posting's current cleansed copy: the copy of its newest raw row at the highest cleaner version (the one
    #: `postings/cleansed.py` reads). Kept by the code that writes copies (`postings/raw_copies.py`, which points the
    #: posting at each copy it makes) so that finding it is a read of this column and not a search of every raw row
    #: (`cleansed.repoint` works it out from the rows themselves). No foreign key: the copy's raw row names this
    #: posting, and SQLite cannot add a key that closes the circle. A pointer to a copy that is gone is no copy.
    current_cleansed_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    @hybrid_property
    def is_open(self) -> bool:
        """Whether the board still lists the posting and the app still reads that board: neither closed nor Not
        checked (docs/PRD.md 39.8). Every queue and every count of open postings asks this, so a posting nothing is
        checking is worked by no stage and counted as open nowhere."""
        return self.closed_at is None and self.not_checked_at is None

    @is_open.inplace.expression
    @classmethod
    def _is_open_expression(cls) -> ColumnElement[bool]:
        return and_(cls.closed_at.is_(None), cls.not_checked_at.is_(None))


def _keep_days(target: "JobPosting | Opportunity", column: str, day_column: str) -> None:
    """A stored day follows its time when the time is changed on a row that is saved (a merge keeps the earlier
    `first_seen`: `employers/records.py`)."""
    if inspect(target).get_history(column, PASSIVE_NO_INITIALIZE).has_changes():
        when = getattr(target, column)
        setattr(target, day_column, owner_today(aware(when)) if when is not None else None)


@event.listens_for(JobPosting, "before_update")
def _posting_seen_day_follows_first_seen(_mapper, _connection, target: JobPosting) -> None:
    _keep_days(target, "first_seen", "seen_day")


@event.listens_for(Opportunity, "before_update")
def _role_created_day_follows_created_at(_mapper, _connection, target: Opportunity) -> None:
    _keep_days(target, "created_at", "created_day")


class RoleTagJob(Base):
    """The one-off job that gives the roles found before target roles the target role each matches (docs/PRD.md 30.5).
    One is open at a time: a small title session is handed those roles (`jobs.titles_next`) and answers with the role
    each matches, or none. It closes when none is left. `counts` is {total, tagged, none, handed: {role id: times}}."""

    __tablename__ = "js_role_tag_jobs"
    __table_args__ = (one_of("js_role_tag_jobs", "status", TagJobStatus),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: open | done
    status: Mapped[str] = mapped_column(
        String(10), default=TagJobStatus.OPEN.value, server_default=TagJobStatus.OPEN.value)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    counts: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class CheckJob(Base):
    """A dry run of the title check, asked for on What to look for ("Check my roles"; `jobs/check.py`): the owner's
    pursued roles, a sample of recent postings the title check or classifier stopped, or one pasted posting, judged by a
    small Haiku session against the target roles as they stood when it was asked. waiting (no session has it) -> working
    (a session took it) -> done | failed. It stores nothing on postings or roles: the items are a snapshot held here,
    and they and the pasted text are dropped when the job ends. The row, with its `result`, goes after a day."""

    __tablename__ = "js_check_jobs"
    __table_args__ = (
        Index("ix_js_check_jobs_owner_state", "owner", "state"),
        one_of("js_check_jobs", "scope", CheckScope),
        one_of("js_check_jobs", "state", CheckState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: pursued | gated | posting
    scope: Mapped[str] = mapped_column(String(10))
    #: The pasted posting (scope posting); dropped when the job ends.
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    state: Mapped[str] = mapped_column(
        String(10), default=CheckState.WAITING.value, server_default=CheckState.WAITING.value)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The last time a session took the job or answered a batch: the time it has to finish counts from here.
    touched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: What the check judges against: the company and each target role's id, name, titles and description, as the title
    #: pass is handed them (`profile_views.for_titles`), taken when the job was asked for.
    looks: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    #: The postings or roles to judge, in order (title, company, place, the start of the text, what they are in the
    #: app); an item's id for the session is its position. Dropped when the job ends.
    items: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    #: The session's answers so far, by item id: {answer, role, why}. Dropped when the job ends.
    answers: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    #: What the owner's request meant: {total: how many such roles or postings there are in all (the items may be a
    #: sample or the newest of them)}.
    meta: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    #: What the check found (done), in the shape `role_check.build_result` makes.
    result: Mapped[dict | None] = mapped_column(JSON)
    #: Why it failed, in a plain sentence for the owner.
    error: Mapped[str | None] = mapped_column(Text)


class GapRun(Base):
    """A run of Find Gaps (docs/PRD.md path 38; `gaps/runs.py`): the postings read for one role, the themes the asks
    group into and each theme's grade against the whole master resume. waiting (no session has it) -> working (a session
    took it with `jobs.gaps`) -> done (`jobs.gaps_report` gave its themes) or failed, with a plain sentence. Each target
    role keeps its latest done run (a newer one that finishes replaces it); a failed run, and a run for a title that is
    not a target role, go after a day."""

    __tablename__ = "js_gap_runs"
    __table_args__ = (
        Index("ix_js_gap_runs_owner_state", "owner", "state"),
        Index("ix_js_gap_runs_owner_role", "owner", "role_id"),
        one_of("js_gap_runs", "kind", GapKind),
        one_of("js_gap_runs", "state", GapState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: The target role's id; none for a run of a title that is not one.
    role_id: Mapped[str | None] = mapped_column(String(80))
    #: The role in the Pipeline a run started from a job is about (docs/PRD.md 38.11); none for the other runs.
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_opportunities.id", ondelete="CASCADE", name="fk_js_gap_runs_opportunity_id"))
    #: The role's name, or the title the owner typed.
    title: Mapped[str] = mapped_column(Text)
    #: `role` (stored postings) or `new` (an agent searches the public web).
    kind: Mapped[str] = mapped_column(String(10), default=GapKind.ROLE.value, server_default=GapKind.ROLE.value)
    #: Whether this run grades the postings of the run before it again (Check again), instead of drawing a sample.
    again: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    state: Mapped[str] = mapped_column(
        String(10), default=GapState.WAITING.value, server_default=GapState.WAITING.value)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The last time a session took the run or said how far it was: the time it has to finish counts from here.
    touched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: How many postings the session has read, of `total`.
    read: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: The postings read: [{ref, employer, title, url, text}], the text as handed to the session (cut to a size). A web
    #: run's are the ones the session found.
    postings: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    #: How many employers had a stored posting for the role when the sample was drawn (none for a web run).
    available: Mapped[int | None] = mapped_column(Integer)
    #: The themes the session reported: [{id, name, kind, asked_by, grade, from, why}], counted and sorted when done.
    themes: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    #: The version of the master resume in force when a session was handed it, and of the instruction file it was sent.
    resume_version: Mapped[int | None] = mapped_column(Integer)
    instructions_version: Mapped[int | None] = mapped_column(Integer)
    #: The process run behind it (no foreign key: the engine's table is its own).
    instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: Why it failed, in a plain sentence for the owner.
    error: Mapped[str | None] = mapped_column(Text)


class GapDraft(Base):
    """A line drafted from the owner's own words about a theme ("I've done this" in Find Gaps; `gaps/drafts.py`): a
    small
    request a session answers while the owner waits, in the shape of the parser's. waiting -> working -> done | failed.
    The words are dropped when it ends, and the row after a day."""

    __tablename__ = "js_gap_drafts"
    __table_args__ = (
        Index("ix_js_gap_drafts_owner_state", "owner", "state"),
        one_of("js_gap_drafts", "state", GapState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_gap_runs.id", ondelete="CASCADE", name="fk_js_gap_drafts_run_id"))
    #: The theme, as the run named it: its name and kind.
    theme: Mapped[str] = mapped_column(Text)
    theme_kind: Mapped[str] = mapped_column(String(16))
    words: Mapped[str] = mapped_column(Text, default="", server_default="")
    state: Mapped[str] = mapped_column(
        String(10), default=GapState.WAITING.value, server_default=GapState.WAITING.value)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    draft: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class GapAnswerRow(Base):
    """What the owner said of a theme in Find Gaps (`gaps/runs.py`): "I haven't" (a real gap) or "Not relevant" (hidden
    for that role, on later runs too). Kept by the theme's key (its name, folded) within a scope: the target role's id,
    or the title that was typed for a run of a title that is not a target role. It reaches nothing outside Find Gaps."""

    __tablename__ = "js_gap_answers"
    __table_args__ = (
        UniqueConstraint("owner", "scope", "key", name="uq_js_gap_answers_owner_scope_key"),
        one_of("js_gap_answers", "answer", GapAnswer),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    scope: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))
    answer: Mapped[str] = mapped_column(String(12))
    at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class GapCard(Base):
    """One question the Coach asked about a theme of a Find Gaps run for a job (docs/PRD.md 38.13, 38.14;
    `gaps/coach.py`). The owner answers it in their own words, skips it, or says "not true". `answer` is the owner's
    words, kept whole. It goes with its run; the run that replaces it for the same job takes over the cards of the
    themes that are still there."""

    __tablename__ = "js_gap_cards"
    __table_args__ = (
        Index("ix_js_gap_cards_owner_run", "owner", "run_id"),
        one_of("js_gap_cards", "state", CardState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_gap_runs.id", ondelete="CASCADE", name="fk_js_gap_cards_run_id"))
    #: Where the theme's answers hold (`find_gaps.scope_of`), the theme's key and name, and its grade (T or U).
    scope: Mapped[str] = mapped_column(String(120))
    theme_key: Mapped[str] = mapped_column(String(120))
    theme: Mapped[str] = mapped_column(Text)
    grade: Mapped[str] = mapped_column(String(1))
    question: Mapped[str] = mapped_column(Text)
    #: Its place among the theme's questions (0 first).
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    state: Mapped[str] = mapped_column(
        String(10), default=CardState.OPEN.value, server_default=CardState.OPEN.value)
    #: The owner's own words, whole (empty until answered).
    answer: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    answered_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When the Coach was handed this answer to propose lines from.
    proposed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class GapNotTrue(Base):
    """A theme the owner said is not true of them (docs/PRD.md 38.14): never asked about again within its scope (the
    target role's id, or the job's, `find_gaps.scope_of`). It reaches nothing outside the Coach's questions."""

    __tablename__ = "js_gap_not_true"
    __table_args__ = (UniqueConstraint("owner", "scope", "key", name="uq_js_gap_not_true_owner_scope_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    scope: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class GapCoachJob(Base):
    """A job for the Coach in Find Gaps (docs/PRD.md 38.13, 38.15; `gaps/coach.py`): questions for a run's themes
    (`ask`) or master lines proposed from the owner's answers (`propose`). waiting (no session has it) -> working (a
    session took it with `jobs.coach`) -> done or failed with a plain sentence; the process `jobsearch_clarify_gaps`
    runs it."""

    __tablename__ = "js_gap_coach_jobs"
    __table_args__ = (
        Index("ix_js_gap_coach_jobs_owner_state", "owner", "state"),
        one_of("js_gap_coach_jobs", "mode", CoachMode),
        one_of("js_gap_coach_jobs", "state", GapState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_gap_runs.id", ondelete="CASCADE", name="fk_js_gap_coach_jobs_run_id"))
    mode: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(
        String(10), default=GapState.WAITING.value, server_default=GapState.WAITING.value)
    #: What it is about: the theme keys to ask (`ask`) or the ids of the answered cards to propose from (`propose`).
    items: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    #: How many times the Coach's answer was sent back.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    touched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: How many proposals were dropped as made before (`propose`), and why it failed.
    dropped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)


class Proposal(Base):
    """A suggestion to change a document, accepted or rejected by the owner (docs/PRD.md 8.3's minimal shape, first
    built for Find Gaps 38.15; `proposals.py`). A rejected one is kept, and one whose `key` is stored is not made again.
    It outlives the run it came from."""

    __tablename__ = "js_proposals"
    __table_args__ = (
        UniqueConstraint("owner", "key", name="uq_js_proposals_owner_key"),
        Index("ix_js_proposals_owner_run", "owner", "run_id"),
        one_of("js_proposals", "status", ProposalStatus),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_gap_runs.id", ondelete="SET NULL", name="fk_js_proposals_run_id"))
    #: Who suggested it, and which document it changes.
    source: Mapped[str] = mapped_column(String(30), default="coach.gaps", server_default="coach.gaps")
    document: Mapped[str] = mapped_column(String(20), default="resume", server_default="resume")
    #: The theme it answers (its name), where it goes: {job_id, kind: new_line | evidence, line_id}, and the words.
    theme: Mapped[str] = mapped_column(Text, default="", server_default="")
    part: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    after: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: The owner's answer it came from: {card, question, answer}.
    cites: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(10), default=ProposalStatus.PROPOSED.value, server_default=ProposalStatus.PROPOSED.value)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The words as saved when the owner edited them, and the master version an accept made.
    final_text: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int | None] = mapped_column(Integer)

class FitsNoneOk(Base):
    """A role (or a posting found this week) the owner called fine as it is: it fits none of their target roles and they
    don't mind (`fits_none.py`). It is left out of what "fits none of your roles" counts."""

    __tablename__ = "js_fits_none_ok"
    __table_args__ = (
        UniqueConstraint("owner", "subject_id", name="uq_js_fits_none_ok_owner_subject"),
        one_of("js_fits_none_ok", "kind", FitsNoneKind),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: The id of the role (`js_opportunities`) or the posting (`js_postings`) the owner said was fine.
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    #: role | posting
    kind: Mapped[str] = mapped_column(String(10))
    at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class GoRun(Base):
    """Launch in Browser's run for an owner (`browser/go.py`), kept as the page reads it: the phase, the counts, the
    roles
    opened and skipped with why, the notes and the role being built. One row; a run writes it as it goes, so a restart
    never wipes it."""

    __tablename__ = "js_go_runs"

    owner: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class BrowserItem(Base):
    """One small piece of browser work (`browser/items.py`): find a board role's original
    posting (`source`), read a posting's whole text at its address (`read`), read one site's new listings (`scan`), map
    a site for its runbook (`map`), or, by the app's own code alone, read a board role's posting page for its text
    (`listing`). The app owns the queue: a worker leases one item at a time, hands back what it found, and asks again.
    One row per piece of work (`kind`, `target`); it is opened again when the work comes back."""

    __tablename__ = "js_browser_items"
    __table_args__ = (
        UniqueConstraint("owner", "kind", "target", name="uq_js_browser_items_owner_kind_target"),
        Index("ix_js_browser_items_owner_state", "owner", "state"),
        Index("ix_js_browser_items_role_id", "role_id"),
        one_of("js_browser_items", "kind", BrowserKind),
        one_of("js_browser_items", "state", BrowserState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: `BrowserKind`: source | read | scan | map | record | listing.
    kind: Mapped[str] = mapped_column(String(10))
    #: What it is about, as a key: the role's id, the posting's id (a job the owner added by its link) or the site's
    #: host.
    target: Mapped[str] = mapped_column(String(300))
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_opportunities.id", ondelete="CASCADE", name="fk_js_browser_items_role_id"))
    posting_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_postings.id", ondelete="CASCADE", name="fk_js_browser_items_posting_id"))
    #: The site a scan or map is for (its host as Where to look keys it).
    site: Mapped[str | None] = mapped_column(String(300))
    #: The address the worker opens first.
    link: Mapped[str | None] = mapped_column(Text)
    #: The registrable domain the work is done on: what the pace and the one-worker-per-site rule are kept by.
    domain: Mapped[str] = mapped_column(String(300))
    #: `BrowserState`: waiting | leased | done | stuck (its tries are used: the reason is on the role) | dropped (the
    #: work is no longer needed).
    state: Mapped[str] = mapped_column(String(10), default=BrowserState.WAITING.value)
    #: Tries used: a `not_found`, `unreadable` or failed map is one; a block is none.
    tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_result: Mapped[str | None] = mapped_column(String(20))
    last_note: Mapped[str | None] = mapped_column(Text)
    #: Not handed out before this (a retry's wait); when it was opened, else.
    next_at: Mapped[datetime] = mapped_column(UtcDateTime)
    #: 0 for what the owner is waiting on (a job added by its link), 1 for the rest; then the youngest role first.
    priority: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    order_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The session that holds it, and until when (renewed by each of its calls); None while nobody does.
    leased_by: Mapped[str | None] = mapped_column(String(80))
    lease_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When a session's hold runs past the item's time box (`browser_pace.TIMEBOX`); None while nobody holds it, for a
    #: hold of the app's own code, and always for a map.
    due_by: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: When the app's own code last tried it before any session (`browser/code.py`); None: not since it was opened or
    #: last retried.
    code_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: Only the app's code takes it, never a session (the owner, 2026-11-13): the `source` item of a role in Followup
    #: needed. What the code cannot get, it closes the role as noise for (`postings/noise.py`).
    code_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    #: The run (the session's row on the Missions screen) that last held it.
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    done_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class BrowserAttempt(Base):
    """Every time an item was worked: who (the session), when, what came of it (a result the worker handed back, or
    `no_result` when its lease ran out or its session ended) and a note."""

    __tablename__ = "js_browser_attempts"
    __table_args__ = (Index("ix_js_browser_attempts_owner_at", "owner", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("js_browser_items.id", ondelete="CASCADE", name="fk_js_browser_attempts_item_id"),
        index=True)
    kind: Mapped[str] = mapped_column(String(10))
    session_id: Mapped[str | None] = mapped_column(String(80))
    at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    outcome: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)


class BrowserDomain(Base):
    """The pace the app keeps on one registrable domain, shared by every worker: no page before `next_page_at`, and
    none at all before `cool_until` after a block (`blocks` in a row: an hour, then eight)."""

    __tablename__ = "js_browser_domains"
    __table_args__ = (UniqueConstraint("owner", "domain", name="uq_js_browser_domains_owner_domain"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    domain: Mapped[str] = mapped_column(String(300))
    next_page_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    cool_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    blocks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: How the app's own fetch of a posting page here last went (`browser/code.py`): `text` (the page's HTML holds the
    #: posting), `shell` (a page with no usable text: it draws itself with script, so the next read goes straight to
    #: the browser) or `refused` (the site said no). None: not tried yet. `page_read_at`: when that was seen.
    page_read: Mapped[str | None] = mapped_column(String(10))
    page_read_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: A refusal or bot check stopped this domain for good (`host_stops.py`; "a refusal is a stop"): when, and what
    #: answered. A 429 or a rate-limit page is no stop, only a rest (`cool_until`).
    stopped_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    stopped_for: Mapped[str | None] = mapped_column(String(400))
    #: When the stop ends, for the one domain whose stop does (a board's: the owner's next off hours start, PRD
    #: 48.14 d); None is for good, as every other domain's is. A stop whose end has come is no stop.
    stopped_until: Mapped[datetime | None] = mapped_column(UtcDateTime)


class MailLogin(Base):
    """The owner's mail login for Go's checks (`mail/login.py`): where the inbox is, the address, and the app password
    sealed (`mail/seal.py`), never in the clear. One row each owner. Nothing returns `password_sealed`."""

    __tablename__ = "js_mail_logins"
    __table_args__ = (UniqueConstraint("owner", name="uq_js_mail_logins_owner"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    host: Mapped[str] = mapped_column(String(300))
    port: Mapped[int] = mapped_column(Integer, default=993, server_default="993")
    username: Mapped[str] = mapped_column(String(320))
    password_sealed: Mapped[str] = mapped_column(Text)
    saved_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class MailMessage(Base):
    """One message the mail sync has read (`mail/sync.py`, docs/PRD.md 14.9): who sent it, its subject and day, what
    kind of mail it is, the one line that decided it (at most 200 characters), the role it was matched to and what
    came of it. The key is the message's own id, so a message is read once. Never the body or an attachment: the
    `opening` (the start of the text, for the small model and the matcher) is kept only until the mail is acted on, and
    cleared then. `details` is what the mail said that the act needs (a calendar invite's date, place and people, a
    posting link, the roles that fit equally), cleared when it has been carried out. `state`: `read` (just
    read), `waiting` (the rules could not settle it: it waits for the mail reader, a small session), `classed` (its
    kind is known), `acted` (matched and carried out)."""

    __tablename__ = "js_mail_messages"
    __table_args__ = (
        UniqueConstraint("owner", "message_key", name="uq_js_mail_messages_owner_key"),
        Index("ix_js_mail_messages_owner_state", "owner", "state"),
        Index("ix_js_mail_messages_role", "role_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    message_key: Mapped[str] = mapped_column(String(300))
    sender: Mapped[str] = mapped_column(String(200))
    sender_key: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300))
    day: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(12), default="read", server_default="read")
    kind: Mapped[str | None] = mapped_column(String(20))
    decided_by: Mapped[str | None] = mapped_column(String(8))
    line: Mapped[str | None] = mapped_column(String(200))
    outcome: Mapped[str | None] = mapped_column(String(20))
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_opportunities.id", ondelete="SET NULL", name="fk_js_mail_messages_role_id"))
    trail_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    employer_said: Mapped[str | None] = mapped_column(String(200))
    title_said: Mapped[str | None] = mapped_column(String(200))
    opening: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON().with_variant(JSON, "postgresql"))
    claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    claims: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    read_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class CostNight(Base):
    """One night's Claude cost at API prices (`cost_report.py`): the total and the whole report as stored, kept because
    the lanes prune their session transcripts. One row for each owner and night; a night worked out again replaces
    it."""

    __tablename__ = "js_cost_nights"
    __table_args__ = (UniqueConstraint("owner", "night", name="uq_js_cost_nights_owner_night"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    night: Mapped[date] = mapped_column(Date)
    total_usd: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    sessions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    data: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class AgentSpend(Base):
    """What one finished Claude session cost, at API prices, as the runner reported it (`budget.py`): the night's dollar
    budget is counted from these rows. `background` is true for work the app started itself (the pool's own pull), false
    for what the owner started (it is never counted against the budget)."""

    __tablename__ = "js_agent_spend"
    __table_args__ = (Index("ix_js_agent_spend_owner_at", "owner", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    session_id: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str | None] = mapped_column(String(40))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    background: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class StoryLibrary(Base):
    """One version of the owner's story library (`writing/library.py`): short resume
    lines
    for each master entry, a scope line for each role and polished cover-letter blocks for each master story, each
    citing the master lines it rests on, checked when they are built. A packet is put together from the newest ready
    version. A build writes its draft here (`building`) and marks it `ready` when it is saved, or `failed`."""

    __tablename__ = "js_story_library"
    __table_args__ = (
        UniqueConstraint("owner", "version", name="uq_js_story_library_owner_version"),
        Index("ix_js_story_library_owner_status", "owner", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="building", server_default="building")
    #: The master resume and writing rules versions it was built from.
    resume_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    writing_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    data: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    done_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class SeatRun(Base):
    """One run of an agent seat (`seats.py`): a single-turn session the runner started for an open agent step of the
    packet or library process. Its claim while it runs, and what it cost and how it ended once it answered."""

    __tablename__ = "js_seat_runs"
    __table_args__ = (Index("ix_js_seat_runs_owner_wait", "owner", "wait_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    wait_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    process_key: Mapped[str] = mapped_column(String(120))
    element: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(20))
    session_id: Mapped[str] = mapped_column(String(80), default="", server_default="")
    started_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str] = mapped_column(Text, default="", server_default="")
    cost_usd: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class StructuredRecord(Base):
    """A posting or master structured record: the fields code or a model
    read, each with its source mark and the quote it rests on (`postings/record.py`). A posting record is keyed by its
    role and the fingerprint of the text it was read from, so clearing an analysis never loses it and a changed text
    makes a new one; the master record (`role_id` empty) by the master version. Shadow only: nothing reads it yet."""

    __tablename__ = "js_structured_records"
    __table_args__ = (
        UniqueConstraint("owner", "kind", "role_id", "fingerprint", name="uq_js_structured_records_role_fp"),
        Index("uq_js_structured_records_profile", "owner", "kind", "fingerprint", unique=True,
              sqlite_where=text("role_id IS NULL")),
        CheckConstraint("kind IN ('posting', 'profile', 'claims')", name="ck_js_structured_records_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    #: `posting` or `profile`.
    kind: Mapped[str] = mapped_column(String(10))
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_opportunities.id", ondelete="CASCADE", name="fk_js_structured_records_role_id"),
        index=True)
    #: The hash of the text read (a posting, as `gate_facts.fingerprint`) or the master version.
    fingerprint: Mapped[str] = mapped_column(String(64))
    #: The version of the rules that filled it, so an older one reads as out of date.
    rules_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    #: A record made from a short snippet (a board's card) holds what the snippet states and never fails a gate.
    partial: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    #: {field name: {value, source, quote, also?}}.
    fields: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class RawPosting(Base):
    """What a read fetched for one posting, exactly as it came: the
    posting's item from a list feed, a detail answer's bytes, or, for a posting read before raw copies were kept, its
    stored text (`no_raw`). Append only: nothing edits a row but a move of its posting to another employer record,
    which gives the row the posting's new address and writes nothing else of it (`raw_copies.follow`), and nothing
    deletes one but the hour tick's clearing, seven days after a newer row of its kind from its reader replaced it
    (`housekeeping.clear_old_copies`, docs/PRD.md 47.9; the newest rows of a posting always stay). A read that gets
    the same bytes again writes nothing (unique on the source, the posting's id, the kind and the hash), and neither
    does a list read whose item changed nothing the cleaner reads (`raw_copies.unchanged`), so an item row's
    `fetched_at` is when the content last changed. `parsed` is what the reader made of it, the plain posting fields
    the cleaner starts from (`cleanse.py`), so a cleaner can be run again without a fetch."""

    __tablename__ = "js_raw_postings"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("owner", "source_id", "ext_id", "kind", "body_sha256", name="uq_js_raw_postings_body"),
        Index("ix_js_raw_postings_owner_source_ext", "owner", "source_id", "ext_id"),
        #: A posting's newest raw row is the first of its address in this order, and the id comes with it: the current
        #: copy (`cleansed._CURRENT`) reads them with no sort and no table access (0133).
        Index("ix_js_raw_postings_newest", "owner", "source_id", "ext_id", text("seq DESC")),
        CheckConstraint("kind IN ('item', 'detail', 'legacy')", name="ck_js_raw_postings_kind"),
        {"sqlite_autoincrement": True},
    )

    #: The order rows were written in, whatever the clock said (`fetched_at` is the night's, shared by its rows). The
    #: table's key, so SQLite numbers each row itself (its rowid: one more than the highest, and `AUTOINCREMENT` keeps a
    #: number from being used twice after a row is deleted), with no gap and no repeat for the one writer there is; a
    #: number a rolled-back write took goes back with it. `id` is the row's name everywhere else (`unique`: the copies'
    #: foreign key names it).
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: `insert_sentinel`: the ORM matches the database-made `seq` of a batch of new rows back to them by this
    #: client-made value, so a slice of rows is one INSERT, not one with `RETURNING seq` for each (no schema change).
    id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, default=uuid.uuid4, insert_sentinel=True)
    owner: Mapped[str] = mapped_column(String(80))
    #: Where it was read, as `JobPosting.employer_id` has it: an employer, or the site of a job board. With `ext_id`
    #: it is the posting's address, by which the rows of a posting are read: it changes when the posting's does.
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    posting_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_postings.id", ondelete="SET NULL", name="fk_js_raw_postings_posting_id"))
    #: Who read it: `adapter:<job system>`, `recipe:<kind>` (with `reader_version`, the skill's), or `legacy`.
    reader: Mapped[str] = mapped_column(String(80))
    reader_version: Mapped[int | None] = mapped_column(Integer)
    #: `item` (a list feed's), `detail` (a posting's own answer) or `legacy` (stored text, `no_raw`).
    kind: Mapped[str] = mapped_column(String(10))
    ext_id: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(120))
    #: The bytes, deflated (zlib); `body_sha256` is the hash of the bytes as they were.
    body: Mapped[bytes] = mapped_column(LargeBinary)
    body_sha256: Mapped[str] = mapped_column(String(64))
    body_bytes: Mapped[int] = mapped_column(Integer)
    parsed: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    no_raw: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class CleansedPosting(Base):
    """A posting's raw copies as plain fields, boilerplate removed, one
    row for each raw row it was last built from and each cleaner version (`cleanse.py`). The newest raw row of a
    posting with the highest cleaner version is its current copy. `removed` names every rule that took text out and how
    much; `marks` is the structured record's fields (value, who read it, the quote). Rebuilt by `recleanse`, never
    edited. A copy goes with its raw row, and one that a newer copy replaced over seven days ago, that no rank names
    and that is not its posting's current copy is cleared by the hour tick (`housekeeping.clear_old_copies`)."""

    __tablename__ = "js_cleansed_postings"
    __table_args__ = (UniqueConstraint("raw_id", "cleaner_version", name="uq_js_cleansed_postings_raw_version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80), index=True)
    raw_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_raw_postings.id", ondelete="CASCADE", name="fk_js_cleansed_postings_raw_id"))
    cleaner_version: Mapped[int] = mapped_column(Integer)
    #: Every raw row it was built from (the newest item and the newest detail answer), `raw_id` the newest of them.
    from_raw: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    title: Mapped[str | None] = mapped_column(Text)
    employer: Mapped[str | None] = mapped_column(Text)
    locations: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    work_mode: Mapped[str | None] = mapped_column(String(20))
    pay_min: Mapped[int | None] = mapped_column(Integer)
    pay_max: Mapped[int | None] = mapped_column(Integer)
    posted_date: Mapped[date | None] = mapped_column(Date)
    text_clean: Mapped[str] = mapped_column(Text, default="", server_default="")
    removed: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    marks: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    #: The text is not the whole posting (`whole.why_not`); a gate never fails on a partial copy's empty field.
    partial: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    no_raw: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    cleaned_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class RoleRank(Base):
    """The Haiku ranker's fit score for a role, 0 to 100, with two lines of why (in
    shadow: kept beside today's fit percent, which still orders the board; nothing reads it but the shadow report). It
    is for one cleansed copy of the role's posting and one version of the short profile (`rank.py`); a changed copy or
    profile is ranked again and the earlier rows stay."""

    __tablename__ = "js_role_ranks"
    __table_args__ = (
        UniqueConstraint("role_id", "cleansed_id", "profile_version", name="uq_js_role_ranks_role_copy_profile"),
        Index("ix_js_role_ranks_owner_role", "owner", "role_id"),
        # The ranks that name a cleansed copy: what the database looks for when a copy is deleted (the foreign key
        # below) and what the clearing of old copies asks first (`housekeeping.clear_old_copies`; migration 0159).
        Index("ix_js_role_ranks_cleansed_id", "cleansed_id"),
        CheckConstraint("fit >= 0 AND fit <= 100", name="ck_js_role_ranks_fit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_opportunities.id", ondelete="CASCADE", name="fk_js_role_ranks_role_id"))
    cleansed_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("js_cleansed_postings.id", ondelete="CASCADE", name="fk_js_role_ranks_cleansed_id"))
    #: The version of the short profile it was ranked against (`ProfileVersion`, kind `rank_profile`).
    profile_version: Mapped[int] = mapped_column(Integer)
    fit: Mapped[int] = mapped_column(Integer)
    #: Two short lines, one under the other.
    why: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: True for a rank made when the posting's text was cut to 4,000 characters before it was sent (the ranker until app
    #: 1.179; migration 0144 marks the ranks of the copies over that length): `rank.unranked` counts it as no rank, and
    #: the rank of the same copy made on the whole text replaces it in this row (`rank.keep`). False for every other.
    text_cut: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class Interview(Base):
    """One interview of a role in Active Discussion (docs/PRD.md 15.2.1; `interviews/core.py`): when, what round, who is
    in
    it (`people`: [{id, name, title}]) and the owner's notes. Its preps (`InterviewPrep`) go with it."""

    __tablename__ = "js_interviews"
    __table_args__ = (
        Index("ix_js_interviews_owner_role", "owner", "role_id"),
        one_of("js_interviews", "round_kind", RoundKind),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_opportunities.id", ondelete="CASCADE", name="fk_js_interviews_role_id"))
    when_at: Mapped[datetime] = mapped_column(UtcDateTime)
    round_kind: Mapped[str] = mapped_column(
        String(20), default=RoundKind.OTHER.value, server_default=RoundKind.OTHER.value)
    #: What the employer calls the round, when it names one.
    label: Mapped[str] = mapped_column(Text, default="", server_default="")
    people: Mapped[list] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=list)
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))


class InterviewPrep(Base):
    """One version of the prep for an interview (docs/PRD.md 15.2.2; `coach.py`): the Coach's report, the red-team read
    of it, and where it is on the way (`PrepState`). `snapshot` is the interview as it was when the version was asked
    for, so the page can say what changed since. One version is open at a time."""

    __tablename__ = "js_interview_preps"
    __table_args__ = (
        UniqueConstraint("interview_id", "version", name="uq_js_interview_preps_version"),
        Index("ix_js_interview_preps_owner_state", "owner", "state"),
        Index("uq_js_interview_preps_open", "interview_id", unique=True,
              sqlite_where=text("state IN ('waiting', 'writing', 'review_waiting', 'reviewing')")),
        one_of("js_interview_preps", "state", PrepState),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    interview_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_interviews.id", ondelete="CASCADE", name="fk_js_interview_preps_interview_id"))
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(
        String(16), default=PrepState.WAITING.value, server_default=PrepState.WAITING.value)
    snapshot: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    report: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    review: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    #: How many times a session took the write, and the review, and was never heard from again (`hand_outs.MAX_CLAIMS`).
    claims: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    review_claims: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: How many times the checks refused the report before this one.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    asked_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    written_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    review_taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class ActionRun(Base):
    """One thing the owner's Claude did, or looked at before doing, through the action door (`owner_actions`,
    docs/PRD.md path 50.6): every write and every dry run of one is a row, which is also the whole record of it
    (Activity's line is 140 characters). `state` is `pending` (the page wrote the call and the engine has not taken it
    yet), `running`, `done` or `failed`; `progress` is one short line a long action keeps current; `result` is what the
    caller was answered once it ended. Reads leave no row. A restart ends a running row as failed
    (`fail_interrupted`)."""

    __tablename__ = "js_action_runs"
    __table_args__ = (Index("ix_js_action_runs_owner_started", "owner", "started"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(40))
    arguments: Mapped[dict] = mapped_column(JSON().with_variant(JSON, "postgresql"), default=dict)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    by: Mapped[str] = mapped_column(String(20))
    started: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC))
    ended: Mapped[datetime | None] = mapped_column(UtcDateTime)
    state: Mapped[str] = mapped_column(String(10), default="running", server_default="running")
    progress: Mapped[str] = mapped_column(String(200), default="", server_default="")
    result: Mapped[dict | None] = mapped_column(JSON().with_variant(JSON, "postgresql"))


class Todo(Base):
    """A to-do of the change-driven work queue: a small row that says "look at this",
    never "do this". The code that makes a change writes one in the same transaction (`work/queue`): a `topic` is the
    kind of look (`role.settle`, `deck.dirty`), a `key` says where (a role's id as text, `''` for the whole owner, `*`
    for anything of the topic). The dispatcher reads them; the hour's janitor deletes the ones nothing consumes,
    older than an hour and logs the counts. Never shown. Portable types only: no foreign keys (a to-do may outlive its
    role), no JSON, no unique or partial index."""

    __tablename__ = "js_todos"
    __table_args__ = (
        Index("ix_js_todos_due", "not_before", "id"),
        Index("ix_js_todos_group", "owner", "topic", "key"),
    )

    #: Arrival order only: ids come from a sequence and are not in commit order.
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(80))
    topic: Mapped[str] = mapped_column(String(40))
    key: Mapped[str] = mapped_column(String(80), default="", server_default="")
    #: The earliest time to handle it (a clock to-do, a backoff); the posting time for the rest.
    not_before: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    posted_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    #: Who posted it: `hook:Opportunity.status`, `core:rank.keep`, `sweep`, `start`, `tripwire`.
    cause: Mapped[str] = mapped_column(String(60), default="", server_default="")
    #: For the consumer only (a later phase).
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)


class EngineFact(Base):
    """One thing the engine knows that the page must show, kept as a row so that the page reads it and the engine's
    memory holds nothing the owner sees (the split of the app into a page and an engine, the owner's point 3). A fact is
    a short text under a key, for one owner: `night_left`, `browser_down`, `asked:<kind>` and the rest (`facts.py` lists
    the keys and who writes them). One row for an owner and a key; a write replaces the text and the time. Never a copy
    of another table's rows. Portable types only: no foreign keys, no JSON, no partial index."""

    __tablename__ = "js_engine_facts"
    __table_args__ = (UniqueConstraint("owner", "key", name="uq_js_engine_facts_owner_key"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: When the text was last written (`facts.put` sets it); the age-out reads it.
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
