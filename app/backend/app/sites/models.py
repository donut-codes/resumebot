"""The tables of site projects (docs/PRD.md path 29): `js_sites` (a site the owner wants searched), `js_site_skills`
(one row per version of the saved recipe a site's project built), and the plan-do-check-act tables: `projects`, `plans`,
`plan_items`, `plan_tasks`, `review_reports` and `lessons`.

Adapted from another project's code (app/PROVENANCE.md). A row
belongs to an `owner` (as in every table here), a project to a site by `site_id`, and what happened is written to
`trail_events`. Statuses and modes are text in the database (no enum type to
migrate) and enum members in Python,.
"""

import enum
import uuid
from datetime import datetime

from process_engine.types import UtcDateTime
from sqlalchemy import (
    JSON,
    Boolean,
    ColumnElement,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    and_,
    false,
    or_,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import Timestamps, UUIDPk

#: What kind of site `js_sites.kind` can hold.
SITE_KINDS = ("employer", "job_board", "association")
#: Which of the owner's two lists a `js_sites` row is on (docs/PRD.md 30.3): a site to search, or a site to avoid.
SEARCH, AVOID = "search", "avoid"
SITE_LISTS = (SEARCH, AVOID)
#: Who reads a site (`js_sites.read_by`, 30.3): `auto` (resumebot tests the site from the server once: if that works
#: the app reads it, if not the owner's browser does), the app on the server, or the owner's own browser (sign-in,
#: robots rules, bot blocks). A site read in the browser is never fetched by the night's read and never gets a project.
#: What an `auto` site came to is `read_found` (`app` or `browser`; None only until it has been tested);
#: `effective_read` is the one rule that says who reads a site, and `read_is` the same rule as a query condition.
AUTO, BY_APP, BY_BROWSER = "auto", "app", "browser"
READ_BYS = (AUTO, BY_APP, BY_BROWSER)
#: What an `auto` site can be found to need (`js_sites.read_found`).
FOUND_READS = (BY_APP, BY_BROWSER)
#: Who switched a site off when it was not the owner (`js_sites.off_by`): the app, because the site cannot be read by
#: any means it has (`sites/where.py:turn_off_unreadable`).
OFF_BY_APP = "app"


def _enum(cls: type[enum.Enum]) -> Enum:
    """Stored as text (no database enum type to migrate), read back as the member."""
    return Enum(
        cls, values_callable=lambda e: [m.value for m in e], name=cls.__name__.lower(), native_enum=False, length=20,
    )


class PlanStatus(enum.StrEnum):
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    changes_requested = "changes_requested"
    superseded = "superseded"
    # The owner said "Don't do this": the project is paused, no rewrite is asked for.
    declined = "declined"


class PlanItemAction(enum.StrEnum):
    do = "do"
    skip = "skip"
    investigate = "investigate"
    # A project's own first task, "Write plan v1", made up front with no `Plan` row yet to belong to (see
    # `PlanTask.plan_id`). Never a PlanItem's own action.
    plan = "plan"
    # A plan check, made from the approved plan's "How to validate": read-only, one per item.
    review_check = "review_check"
    # After every check, an ACT task (read the check report, record lessons, keep what worked). Never a PlanItem's own
    # action.
    act = "act"


class PlanTaskMode(enum.StrEnum):
    """Which kind of session a task's checkout wakes: "do" for an ordinary do-task, "plan" for a planning task (Write
    plan, Act or a tag-in), "review" for a plan check (read-only). The process routes every phase change on this
    field; the agent never decides what kind of session to be."""

    do = "do"
    plan = "plan"
    review = "review"


class PlanTaskStatus(enum.StrEnum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"
    # A do-task's only non-"done" outcome: the item didn't match the plan, a check came back wrong, or the machinery
    # broke. `outcome_reason` carries what differed and the run moves on.
    off_plan = "off_plan"
    skipped = "skipped"
    # `task.escalate`: the task lacks a fact only the owner or the doer can supply (its question is in
    # `data.escalation`). Not handed out; the answer puts it back to todo.
    waiting = "waiting"


#: Cap on a plan item's/task's free-JSON `data` blob (serialized), so an agent-authored plan can't grow an unbounded
#: row.
PLAN_ITEM_DATA_MAX_BYTES = 8_000

#: How many rounds a project may start before it stops and asks, until the owner or the app sets another.
DEFAULT_ROUND_BUDGET = 5


class Site(UUIDPk, Base):
    """A site the owner wants searched: an employer's careers site, a job board or an association's board. Its state
    (learning, working, needs the owner) comes from its project and its skill from `js_site_skills`; neither is
    stored here."""

    __tablename__ = "js_sites"
    #: One address once for each owner. The index holds a hash of the address, not the address: an address has no limit
    #: on its length (the owner's own text), and Postgres refuses a btree entry over about 2.7 KB (migration 0072).
    __table_args__ = (Index("uq_js_sites_owner_url", "owner", "url", unique=True),)

    owner: Mapped[str] = mapped_column(String(80), index=True)
    url: Mapped[str] = mapped_column(Text)
    #: employer | job_board | association (SITE_KINDS)
    kind: Mapped[str] = mapped_column(String(20))
    #: The owner's switch for searching it.
    on: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    #: The employer this is the careers site (or board) of, when it is one (`sites/where.py` makes one for each).
    employer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_employers.id", ondelete="SET NULL", name="fk_js_sites_employer_id"),
        index=True)
    #: What the owner calls it; the page shows the employer's name, or the address's host, when empty.
    label: Mapped[str | None] = mapped_column(Text)
    #: "owner" (they added it) or "app" (made for an employer, or found by the scout).
    added_by: Mapped[str] = mapped_column(String(10), default="app", server_default="app")
    #: When it was first switched on (and again when it is switched on after its project ended with no skill: a fresh
    #: try). A project for the site is started only after this, in the night's mission (`sites/autostart.py`).
    turned_on_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: "search" (a site to search) or "avoid" (a site to avoid, never read: a row on this list is never on, has no skill
    #: and no project, and a posting whose link goes to its host is stopped by the filter). `SITE_LISTS`.
    list_name: Mapped[str] = mapped_column(String(10), default=SEARCH, server_default=SEARCH)
    #: "auto" (resumebot finds out), "app" (the night's server read) or "browser" (the owner's own browser reads it).
    #: `READ_BYS`. The owner's sites start at "auto" (the route's default); a site made any other way starts at "app".
    read_by: Mapped[str] = mapped_column(String(10), default=BY_APP, server_default=BY_APP)
    #: What an "auto" site was found to need: "app" or "browser", or None until the server's test has run. Said by
    #: `effective_read`; meaningless for the other two.
    read_found: Mapped[str | None] = mapped_column(String(10))
    #: Why, in a plain sentence ("It asks for a sign-in."), shown on the site's row.
    read_note: Mapped[str | None] = mapped_column(Text)
    #: "app" when resumebot switched the site off itself because it cannot be read by any means it has (the owner did
    #: not); None otherwise. Cleared when the owner switches the site on again, by the monthly look when the site reads
    #: (`read_probe.recheck_unreadable`) and when the scout names its employer's job system (`where.scout_found_board`).
    #: Only ever set while `on` is false.
    off_by: Mapped[str | None] = mapped_column(String(10))
    #: Why the app switched it off, one plain clause ("its listings only load in a browser"); set with `off_by`.
    off_note: Mapped[str | None] = mapped_column(Text)
    #: When the app switched the site off, or last looked again to see whether it can be read now
    #: (`read_probe.recheck_unreadable`: about monthly). Set with `off_by`.
    off_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: How many times learning how to read this site in the owner's browser has failed (docs/PRD.md 37.5). At
    #: `browser_reader.LEARN_TRIES` the app switches the site off. Cleared by a recipe that is kept and when the owner
    #: switches the site on.
    learn_tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: True while the site waits to be learned again though it still has a recipe (`browser_reader.relearn`): the
    #: recipe keeps reading until the new one is kept, which clears this.
    relearn: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    #: What the last attempt to learn the site found, or why its recipe stopped fitting, in a plain clause; shown on the
    #: site's row. Cleared with `learn_tries`.
    learn_note: Mapped[str | None] = mapped_column(Text)
    #: When a learn run was last sent this site (docs/PRD.md 37.13); None for a site never tried. The learn plan takes
    #: the sites never tried first, then the one tried longest ago, so a few sites that keep failing cannot keep the
    #: rest from a turn. Not cleared by anything: it is when, not how many.
    learn_tried_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The employer's own listing on a job system (a page on one the app has no reader for, or reads only in part) that
    #: this site's careers page hands its openings off to (docs/PRD.md 37.12): found while learning the site in the
    #: owner's browser and recorded when its recipe is kept. A browser run opens it instead of the recipe's start
    #: address, and the postings read there are filed under this site. None for a site that lists its openings itself.
    portal_url: Mapped[str | None] = mapped_column(Text)


def effective_read(site: Site) -> str | None:
    """Who reads this site: the owner's choice, or, for an `auto` site, what was found (`read_found`): `app`,
    `browser`, or None until the test has run (read by no one till then). Every place that asks who reads a site asks
    this; a query asks `read_is`."""
    return site.read_found if site.read_by == AUTO else site.read_by


def read_is(read: str) -> ColumnElement[bool]:
    """`effective_read(site) == read` as a query condition, for `read` one of `BY_APP`, `BY_BROWSER` (a test keeps the
    two equal)."""
    return or_(Site.read_by == read, and_(Site.read_by == AUTO, Site.read_found == read))


class SiteSkill(UUIDPk, Base):
    """One version of a site's skill: the saved recipe its project built, which the app runs (path 29.2). Versions
    are kept, never overwritten; a version a later one replaces, or that broke, is retired."""

    __tablename__ = "js_site_skills"
    __table_args__ = (UniqueConstraint("site_id", "version", name="uq_js_site_skills_site_version"),)

    owner: Mapped[str] = mapped_column(String(80), index=True)
    site_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("js_sites.id", ondelete="CASCADE", name="fk_js_site_skills_site_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    #: The saved recipe, kept as it was handed over (its shape is the skill runner's to define).
    recipe: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    #: The project that built it.
    made_by_project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL", name="fk_js_site_skills_made_by_project_id"),
        index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class SiteRecording(UUIDPk, Base):
    """The traffic one load of a site's job listings made, cookies and tokens stripped before it was kept
    (`sites/recipes.py`). A row with no body is the request for a recording
    (`requested`); the app's own Chrome fills it in. `body` is the deflated JSON capture and `sha256` its hash, which a
    recipe's provenance names."""

    __tablename__ = "js_site_recordings"

    owner: Mapped[str] = mapped_column(String(80), index=True)
    #: The site this is a recording of; empty for a host's posting page (`host`).
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_sites.id", ondelete="CASCADE", name="fk_js_site_recordings_site_id"),
        index=True)
    #: A recording of one posting's page on an employer host that has no site row: the host, or `*.base.domain`.
    host: Mapped[str | None] = mapped_column(String(120), index=True)
    reason: Mapped[str] = mapped_column(String(10), default="first", server_default="first")
    state: Mapped[str] = mapped_column(String(10), default="requested", server_default="requested")
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime)
    recorded_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: The address that was loaded.
    url: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    body: Mapped[bytes | None] = mapped_column(LargeBinary)
    #: How many responses the capture holds.
    responses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: What happened, in a plain clause (why it failed, why no recipe was made).
    note: Mapped[str | None] = mapped_column(Text)


class SiteRecipe(UUIDPk, Base):
    """One version of a recipe a model wrote from a recording (`sites/recipes.py`). While it is `live` it is THE reader
    of the site (`jobsearch/postings/site_reads.py`): the browser no longer reads the site's listings (the owner,
    2026-10-22).
    `health` is the last nights' rows and fill, newest last (`jobsearch/browser/recipes/live.py`)."""

    __tablename__ = "js_site_recipes"
    __table_args__ = (UniqueConstraint("site_id", "version", name="uq_js_site_recipes_site_version"),
                      UniqueConstraint("host", "version", name="uq_js_site_recipes_host_version"))

    owner: Mapped[str] = mapped_column(String(80), index=True)
    #: The site it reads; empty for a host's posting recipe (`host`: how its single-posting pages are read).
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_sites.id", ondelete="CASCADE", name="fk_js_site_recipes_site_id"),
        index=True)
    host: Mapped[str | None] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer)
    recipe: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    recording_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_site_recordings.id", ondelete="SET NULL",
                                       name="fk_js_site_recipes_recording_id"))
    state: Mapped[str] = mapped_column(String(10), default="live", server_default="live")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: Nights in a row the read failed or came back empty in a way that looks broken.
    bad_nights: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    health: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")


class Project(UUIDPk, Timestamps, Base):
    __tablename__ = "projects"
    # A site has at most one project that is not finished (`projects.create_project` checks it; two requests at once
    # could both pass the check, so the database holds the line).
    __table_args__ = (
        Index("uq_projects_one_open_per_site", "site_id", unique=True, sqlite_where=text("finished_at IS NULL")),
    )

    owner: Mapped[str] = mapped_column(String(80), index=True)
    #: The site this project is for.
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("js_sites.id", ondelete="SET NULL", name="fk_projects_site_id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(Text)
    #: "agent" or "you": who started it. A new project is always "you"; "agent" is on the ones the scout started
    #: while it had a tool for it.
    created_by: Mapped[str] = mapped_column(String(20), default="agent", server_default="agent")
    # A manual stop: once set, the project is over -- task.next refuses and remaining todo/in_progress tasks are
    # marked skipped.
    stopped_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    # Set when the loop finishes the project: an accepted check report whose Act is done. Distinct from `stopped_at`.
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finish_summary: Mapped[str | None] = mapped_column(Text)
    # How the project ended, in three plain parts: `{problem, done, outcome}`. Null on a project that has not ended.
    resolution: Mapped[dict | None] = mapped_column(JSON)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)
    # How many rounds (plan versions that ran) this project may start before it stops and asks.
    round_budget: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_ROUND_BUDGET, server_default=str(DEFAULT_ROUND_BUDGET))

    plans: Mapped[list["Plan"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    tasks: Mapped[list["PlanTask"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Plan(UUIDPk, Timestamps, Base):
    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_plans_project_version"),)

    owner: Mapped[str] = mapped_column(String(80), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[PlanStatus] = mapped_column(_enum(PlanStatus), default=PlanStatus.awaiting_approval)
    summary: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    # Set only on a plan.revise (the based_on version, and a plain-English note of what changed and why); null on a
    # project's first plan.propose.
    based_on_version: Mapped[int | None] = mapped_column(Integer)
    changes: Mapped[str | None] = mapped_column(Text)
    method_changed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # {"added": [key,...], "removed": [key,...], "changed": [key,...]} against `based_on_version`.
    diff: Mapped[dict | None] = mapped_column(JSON)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    # The plan's Check section, "How to validate": {done_means, how_to_check, off_plan_means, tools_needed}.
    validation: Mapped[dict | None] = mapped_column(JSON)
    # The plan's Act section: what to learn and standardize after the check. Optional.
    act_guidance: Mapped[str | None] = mapped_column(Text)
    # The plan template version this plan was written to.
    template_version: Mapped[int | None] = mapped_column(Integer)
    # What's wrong (plain text) and what the plan will do (1-3 bullets).
    problem: Mapped[str | None] = mapped_column(Text)
    proposal: Mapped[list[str] | None] = mapped_column(JSON)
    # What the plan leaves out on purpose: [{key, title, reason}]. Null when nothing is left out.
    dropped: Mapped[list[dict] | None] = mapped_column(JSON)

    project: Mapped[Project] = relationship(back_populates="plans")
    items: Mapped[list["PlanItem"]] = relationship(back_populates="plan", cascade="all, delete-orphan")


class PlanItem(UUIDPk, Base):
    __tablename__ = "plan_items"
    __table_args__ = (UniqueConstraint("plan_id", "key", name="uq_plan_items_plan_key"),)

    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    group_label: Mapped[str] = mapped_column(String(255))
    action: Mapped[PlanItemAction] = mapped_column(_enum(PlanItemAction))
    # What kind of work this is (kinds/<domain>/<kind>.kind.md); `group_label` stays the display label.
    kind: Mapped[str | None] = mapped_column(String(120))
    data: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")

    plan: Mapped[Plan] = relationship(back_populates="items")


class PlanTask(UUIDPk, Timestamps, Base):
    """One item's execution under one plan version. A new plan version creates its own `PlanTask` rows on approval;
    `plan.revise` marks the prior version's not-yet-done rows for touched items `superseded` rather than deleting
    them, so the project page can still show what happened."""

    __tablename__ = "plan_tasks"

    owner: Mapped[str] = mapped_column(String(80), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # Null only for a project's own first task ("Write plan v1", action `plan`), made before any `Plan` row exists.
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("plans.id", ondelete="CASCADE")
    )
    plan_version: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    item_key: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    group_label: Mapped[str] = mapped_column(String(255))
    action: Mapped[PlanItemAction] = mapped_column(_enum(PlanItemAction))
    # The item's kind (see PlanItem.kind); copied onto its do task and its check.
    kind: Mapped[str | None] = mapped_column(String(120))
    data: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    mode: Mapped[PlanTaskMode] = mapped_column(_enum(PlanTaskMode), default=PlanTaskMode.do, server_default="do")
    status: Mapped[PlanTaskStatus] = mapped_column(
        _enum(PlanTaskStatus), default=PlanTaskStatus.todo, server_default="todo", index=True
    )
    # An off-plan task's reason ("doesn't match the plan", a check came back wrong, the machinery broke). Never set
    # for a "done" outcome.
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    # Set on an Act or Write-plan task: a frozen compact snapshot of what that step needs, so later activity never
    # changes what the session reading it sees.
    context: Mapped[dict | None] = mapped_column(JSON)
    # Set only on a done plan check: its own `findings`, a list of {key, verdict: "ok"|"issue", note}.
    findings: Mapped[list | None] = mapped_column(JSON)
    # The agent's own evidence quote from task.complete, kept apart from `outcome_reason`.
    evidence: Mapped[str | None] = mapped_column(Text)
    # What the task found, as data: the `result` task.complete carried, checked against its kind's `result` fields.
    result: Mapped[dict | None] = mapped_column(JSON)
    # True once a later plan.revise supersedes this row; a superseded row is never checked out again.
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    checked_out_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_connections.id", ondelete="SET NULL")
    )
    checked_out_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    project: Mapped[Project] = relationship(back_populates="tasks")


class ReviewReport(UUIDPk, Timestamps, Base):
    """The versioned check report written at the end of CHECK: one per project per plan version. `verdict` is
    "accepted" (the loop finishes after Act) or "iterate" (Act, then "Write plan v(n+1)"). `findings_summary` is the
    findings summarized by cause (free text, or {cause: sentence})."""

    __tablename__ = "review_reports"
    __table_args__ = (UniqueConstraint("project_id", "plan_version", name="uq_review_reports_project_version"),)

    owner: Mapped[str] = mapped_column(String(80), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    plan_version: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(20))
    summary: Mapped[str] = mapped_column(Text)
    findings_summary: Mapped[dict | list | str | None] = mapped_column(JSON)


class Lesson(UUIDPk, Timestamps, Base):
    """What the scout has learned: one plain sentence recorded during a project's ACT phase (`lesson.record`).
    Selected for a task by `applies_to` tag, never by search, and listed read-only in every planning task's context."""

    __tablename__ = "lessons"

    owner: Mapped[str] = mapped_column(String(80), index=True)
    text: Mapped[str] = mapped_column(Text)
    #: The lesson's scope: a kind of site ("employer", "job_board", "association"), "all" or "project" (only the
    #: project in `source_project_id`).
    applies_to: Mapped[str] = mapped_column(String(120), index=True)
    #: Where it came from, in the agent's words ("plan v1: the path check").
    source: Mapped[str | None] = mapped_column(String(300))
    source_project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    plan_version: Mapped[int | None] = mapped_column(Integer)
    #: The short label two lessons on the same thing share. A lesson later evidence contradicts is retired, never
    #: deleted: `retired_by` says what retired it.
    subject: Mapped[str | None] = mapped_column(String(120))
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    retired_by: Mapped[str | None] = mapped_column(String(400))
