"""Shared constants, the site loop's error type and the plan diff record.

Every other module of the site project loop builds on this one; it imports nothing from them. Derived from another
project's code with what has no meaning here left out (app/PROVENANCE.md, "Site project loop").
"""

import re
from dataclasses import dataclass

from app.errors import DomainError
from app.harness import instructions as harness_files
from app.sites.models import PLAN_ITEM_DATA_MAX_BYTES, PlanItemAction, PlanTaskStatus  # noqa: F401 (re-exported)

#: A `PlanItem`'s own action is always one of these three; the others are `PlanTask`-only (a plan check, Act, the
#: planning task), never something a `plan.propose`/`plan.revise` caller can author.
_VALID_ITEM_ACTIONS = {PlanItemAction.do.value, PlanItemAction.skip.value, PlanItemAction.investigate.value}

REPORT_FINDINGS_SUMMARY_MAX_CHARS = 4000
#: lesson.record: one plain sentence, and how many the next plan's context carries.
LESSON_TEXT_MAX_CHARS = 2000
LESSON_APPLIES_TO_MAX_CHARS = 120
LESSON_SOURCE_MAX_CHARS = 300
LESSONS_IN_CONTEXT_MAX = 30
#: A lesson that applies to every site the owner has (the other scopes are "project" and the site's kind).
OWNER_LESSON_TAG = "owner"

STEP_SUMMARY_MAX_CHARS = 2000

#: Actions that are never a do-task: the loop's own steps, by PDCA phase. Only `review_check` (a plan check), `act`
#: and `plan` exist here; no other action is in
#: this app's enum.
CHECK_ACTIONS = (PlanItemAction.review_check,)
ACT_ACTIONS = (PlanItemAction.act,)
PLAN_ACTIONS = (PlanItemAction.plan,)
LOOP_ACTIONS = (*CHECK_ACTIONS, *ACT_ACTIONS, *PLAN_ACTIONS)
#: Why a wake: what a hand-out says when resumebot has just made (or released) a planning-mode step, so the door wakes
#: the scout after its commit (the wake itself is `jobsearch/work/wake.py`).
WAKE_REASONS = {"act_ready", "plan_ready", "plan_approved", "planner_retry"}

#: A task still counts as open work while it waits for an answer.
OPEN_STATUSES = (PlanTaskStatus.todo, PlanTaskStatus.in_progress, PlanTaskStatus.waiting)

#: task.complete: a do-task's own outcome vocabulary is exactly "done" | "off_plan".
_VALID_OUTCOMES = {"done", "off_plan"}

#: The plan response template and a plan's Check section, "How to validate". A plan with `validation` runs the
#: plan-driven Check: one check per tried item, made and run by code (`site.check`). Every plan the scout proposes has
#: one.
VALIDATION_TEXT_PARTS = ("done_means", "how_to_check", "off_plan_means")
VALIDATION_TEXT_MAX_CHARS = 6000
TOOLS_NEEDED_MAX = 10
TOOL_FIELD_MAX_CHARS = 200
ACT_GUIDANCE_MAX_CHARS = 6000
CHECK_DIFFERENCE_MAX_CHARS = 2000
PLAN_CHECK_POSITION_BASE = 10**9 + 1_000

PLAN_TEMPLATE_NAME = "projects.plan.response"  # templates/projects.plan.response.template.md


def plan_template(text: str | None = None) -> dict:
    """The plan response template: `{version, text (the whole file), short (all but its worked example)}`. `text` is the
    owner's newest saved version when the caller holds it (`app.jobsearch.work.instruction_files`); left out, it is read
    from
    the file each call, so an edit takes effect without a rebuild. A bare checkout without the file gets version 0 and
    an empty text rather than failing."""
    if text is None:
        try:
            text = harness_files.read("templates", PLAN_TEMPLATE_NAME)
        except (OSError, harness_files.UnknownName):
            return {"version": 0, "text": "", "short": ""}
    found = re.search(r"^template_version:\s*(\d+)", text, re.M)
    version = int(found.group(1)) if found else 0
    head, _sep, _rest = text.partition("## Worked example")
    return {"version": version, "text": text, "short": head.strip()}


def plan_template_for_context(room: int | None = None, text: str | None = None) -> dict:
    """The template as a planning task carries it, within the `room` characters the rest of the context leaves: its
    version and the full text when that fits, else the short form, else just the version; either shorter form says
    `full_with: "doc.read"`. `text` is the template in force when the caller holds it (see `plan_template`)."""
    tpl = plan_template(text)
    if room is None or len(tpl["text"]) + 100 <= room:
        return {"version": tpl["version"], "text": tpl["text"]}
    if len(tpl["short"]) + 140 <= room:
        return {"version": tpl["version"], "text": tpl["short"], "full_with": "doc.read"}
    return {"version": tpl["version"], "full_with": "doc.read"}


class PlanEngineError(DomainError):
    """A plan/task request this module refuses: bad item shape, an unknown project/plan/task, checkout of an unapproved
    project, a decision on a plan version that isn't the one awaiting it, or a review/finish call outside its own turn.
    Callers (the MCP tools, the routes) translate this into their own refusal shape rather than letting it become a raw
    500. `how_to_fix` (optional) is the one plain line an agent door passes on."""

    def __init__(self, message: str = "", how_to_fix: str | None = None) -> None:
        super().__init__(message)
        self.how_to_fix = how_to_fix


@dataclass(frozen=True)
class PlanDiff:
    added: list[str]
    removed: list[str]
    changed: list[str]

    def as_dict(self) -> dict:
        return {"added": self.added, "removed": self.removed, "changed": self.changed}

    @property
    def touched_keys(self) -> set[str]:
        return set(self.added) | set(self.removed) | set(self.changed)


#: A plan's top: the Problem in 2-3 plain sentences, the Proposal as 1-3 short bullets.
PLAN_PROBLEM_MAX_CHARS = 20_000
PLAN_METHOD_MAX_CHARS = 20_000
PLAN_PROPOSAL_MAX_ITEMS = 3
PLAN_PROPOSAL_ITEM_MAX_CHARS = 2_000

#: A plan accounts for everything left: each unresolved item is an item, or left out on
#: purpose with a reason in `dropped`.
DROPPED_MAX = 2_000
DROPPED_REASON_MAX_CHARS = 500
ACT_ITEMS_BUDGET_CHARS = 30_000
ACT_ITEM_TEXT_CHARS = 1_000

#: Keys of a task's `data` that are the machinery's, not the item's: never handed to the scout as the item's data.
_ESCALATION_KEYS = ("escalation", "answered", "planner_runs", "steps")
