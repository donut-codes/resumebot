"""Validating what the scout writes: validation text, items, and the plan's problem and proposal.

Copied from another project's code (app/PROVENANCE.md): an item that names a missing ability instead of a kind
(`needs`, Equip) is not carried, and kinds are checked against the kind files this app ships (and not at all while it
ships none).
"""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.harness import HarnessError, harness
from app.harness.kinds import kind_problems
from app.sites.base import (
    _VALID_ITEM_ACTIONS,
    ACT_GUIDANCE_MAX_CHARS,
    PLAN_ITEM_DATA_MAX_BYTES,
    PLAN_PROBLEM_MAX_CHARS,
    PLAN_PROPOSAL_ITEM_MAX_CHARS,
    PLAN_PROPOSAL_MAX_ITEMS,
    TOOL_FIELD_MAX_CHARS,
    TOOLS_NEEDED_MAX,
    VALIDATION_TEXT_MAX_CHARS,
    VALIDATION_TEXT_PARTS,
    PlanEngineError,
)
from app.sites.connections import doer_kinds_for
from app.sites.models import Project


def _validate_validation(validation: Any, *, required: bool) -> dict | None:
    """A plan's "How to validate": the three text parts and `tools_needed` ({title, purpose} each).
    None is fine for a revision; a new plan must have it. Refusals name the part."""
    if validation is None:
        if required:
            raise PlanEngineError(
                "a new plan needs `validation` (its Check section, How to validate): done_means, "
                "how_to_check, off_plan_means and tools_needed. doc.read('projects.plan.response') shows each."
            )
        return None
    if not isinstance(validation, dict):
        raise PlanEngineError("`validation` is {done_means, how_to_check, off_plan_means, tools_needed}")
    out: dict[str, Any] = {}
    for part in VALIDATION_TEXT_PARTS:
        value = validation.get(part)
        if not isinstance(value, str) or not value.strip():
            raise PlanEngineError(
                f"`validation` is missing {part}: say it in plain words (doc.read('projects.plan.response'))"
            )
        if len(value.strip()) > VALIDATION_TEXT_MAX_CHARS:
            raise PlanEngineError(f"validation.{part} is longer than the {VALIDATION_TEXT_MAX_CHARS}-character cap")
        out[part] = value.strip()
    tools = validation.get("tools_needed")
    if tools is None:
        raise PlanEngineError("`validation` is missing tools_needed: a list of {title, purpose}, [] if none")
    if not isinstance(tools, list):
        raise PlanEngineError("validation.tools_needed is a list of {title, purpose}")
    if len(tools) > TOOLS_NEEDED_MAX:
        raise PlanEngineError(f"validation.tools_needed has more than {TOOLS_NEEDED_MAX} entries")
    clean_tools = []
    for row in tools:
        title = row.get("title") if isinstance(row, dict) else None
        purpose = row.get("purpose") if isinstance(row, dict) else None
        if not isinstance(title, str) or not title.strip() or not isinstance(purpose, str) or not purpose.strip():
            raise PlanEngineError("each validation.tools_needed entry is {title, purpose}, both plain text")
        if len(title.strip()) > TOOL_FIELD_MAX_CHARS or len(purpose.strip()) > TOOL_FIELD_MAX_CHARS:
            raise PlanEngineError(f"a tools_needed title or purpose is longer than {TOOL_FIELD_MAX_CHARS} characters")
        clean_tools.append({"title": title.strip(), "purpose": purpose.strip()})
    out["tools_needed"] = clean_tools
    return out


def _validate_act_guidance(act_guidance: Any) -> str | None:
    if act_guidance is None or (isinstance(act_guidance, str) and not act_guidance.strip()):
        return None
    if not isinstance(act_guidance, str):
        raise PlanEngineError("`act_guidance` is plain text")
    if len(act_guidance.strip()) > ACT_GUIDANCE_MAX_CHARS:
        raise PlanEngineError(f"act_guidance is longer than the {ACT_GUIDANCE_MAX_CHARS}-character cap")
    return act_guidance.strip()


def _validate_items(items: list[dict]) -> None:
    if not items:
        raise PlanEngineError("a plan needs at least one item")
    seen: set[str] = set()
    for row in items:
        key = row.get("key")
        title = row.get("title")
        action = row.get("action")
        if not key or not isinstance(key, str):
            raise PlanEngineError("every item needs a non-empty, stable `key`")
        if key in seen:
            raise PlanEngineError(f"duplicate item key: {key!r}")
        seen.add(key)
        if not title or not isinstance(title, str):
            raise PlanEngineError(f"item {key!r} needs a non-empty `title`")
        if not row.get("group"):
            raise PlanEngineError(f"item {key!r} needs a `group`")
        if action not in _VALID_ITEM_ACTIONS:
            raise PlanEngineError(
                f"item {key!r} has an unknown action {action!r}; must be one of {sorted(_VALID_ITEM_ACTIONS)}"
            )
        data = row.get("data") or {}
        try:
            encoded = json.dumps(data)
        except (TypeError, ValueError) as exc:
            raise PlanEngineError(f"item {key!r}'s data isn't JSON-serializable") from exc
        if len(encoded.encode("utf-8")) > PLAN_ITEM_DATA_MAX_BYTES:
            raise PlanEngineError(f"item {key!r}'s data is larger than the {PLAN_ITEM_DATA_MAX_BYTES}-byte cap")


async def _validate_kinds(session: AsyncSession, project: Project, items: list[dict]) -> None:
    """Every item's `kind` must exist, be switched on and carry its required data, and every `do` item needs one.
    Refusals are plain sentences naming the item, the problem and the fix. While this app ships no kind files (`kinds/`
    is empty until the instructions and kinds step) there is nothing to check a kind against, so items are taken as
    they are."""
    try:
        content = harness()
    except HarnessError as exc:
        raise PlanEngineError(f"the kind files have a problem, so no plan can be checked yet: {exc}") from exc
    if not content.kinds:
        return
    problems = kind_problems(items, content, await doer_kinds_for(session, project), required=True)
    if problems:
        raise PlanEngineError("\n".join(problems))


def _validate_problem_proposal(problem: Any, proposal: Any) -> tuple[str, list[str]]:
    """The plan's Problem and Proposal (templates/projects.plan.response.template.md). Both are required; a refusal
    names what's missing."""
    has_problem = problem is not None and not (isinstance(problem, str) and not problem.strip())
    has_proposal = bool(proposal)
    missing = []
    if not has_problem:
        missing.append("`problem` (what's wrong, in 2-3 plain sentences with real numbers)")
    if not has_proposal:
        missing.append("`proposal` (what the plan will do, as 1-3 short bullets)")
    if missing:
        raise PlanEngineError(
            "a plan needs " + " and ".join(missing) + ". doc.read('projects.plan.response') shows each."
        )
    if not isinstance(problem, str):
        raise PlanEngineError("`problem` is plain text")
    if len(problem.strip()) > PLAN_PROBLEM_MAX_CHARS:
        raise PlanEngineError(f"`problem` is longer than the {PLAN_PROBLEM_MAX_CHARS}-character cap")
    if not isinstance(proposal, list) or len(proposal) > PLAN_PROPOSAL_MAX_ITEMS:
        raise PlanEngineError(f"`proposal` is 1 to {PLAN_PROPOSAL_MAX_ITEMS} short bullets")
    clean_proposal = []
    for bullet in proposal:
        if not isinstance(bullet, str) or not bullet.strip():
            raise PlanEngineError("every `proposal` bullet needs non-empty text")
        if len(bullet.strip()) > PLAN_PROPOSAL_ITEM_MAX_CHARS:
            raise PlanEngineError(
                f"a `proposal` bullet is longer than the {PLAN_PROPOSAL_ITEM_MAX_CHARS}-character cap"
            )
        clean_proposal.append(bullet.strip())
    return problem.strip(), clean_proposal
