"""Checks on a plan proposal: dropped items and coverage.

Copied from another project's code (app/PROVENANCE.md) (the accounting rule is the one piece kept, since the process
file's PR-4 states it): a plan accounts for everything the
last round left unresolved. Not here: the goal's playbook (`project_playbook`, `_playbook_decision`) and the refusal of
a look-only plan that asks a decision.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites.base import DROPPED_MAX, DROPPED_REASON_MAX_CHARS, OPEN_STATUSES, PlanEngineError
from app.sites.models import Plan, PlanItem, PlanItemAction, PlanTask, Project
from app.sites.records import _plain_item_title
from app.textutil import squash

COVERAGE_KEYS_NAMED_MAX = 8


async def write_task_unresolved(session: AsyncSession, project: Project) -> dict[str, str]:
    """`{key: title}` for every key the open Write-plan task lists as unresolved: its `unresolved_items` and the keys it
    only names (`unresolved_unlisted_keys`). Empty when no open task carries any."""
    tasks = (await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.action == PlanItemAction.plan,
            PlanTask.status.in_(OPEN_STATUSES), PlanTask.superseded.is_(False), PlanTask.context.is_not(None),
        ).order_by(PlanTask.created_at.desc())
    )).all()
    for task in tasks:
        ctx = task.context or {}
        rows = ctx.get("unresolved_items") or []
        unlisted = ctx.get("unresolved_unlisted_keys") or []
        if rows or unlisted:
            out = {
                str(r["key"]): str(r.get("title") or r["key"]) for r in rows if isinstance(r, dict) and r.get("key")
            }
            for key in unlisted:
                out.setdefault(str(key), str(key))
            return out
    return {}


async def _item_titles(session: AsyncSession, project: Project, keys: set[str]) -> dict[str, str]:
    """Title per key for the keys this project knows (newest plan item, else task); unknown keys are left out."""
    if not keys:
        return {}
    titles: dict[str, str] = {}
    for key, title in (await session.execute(
        select(PlanTask.item_key, PlanTask.title).where(
            PlanTask.project_id == project.id, PlanTask.item_key.in_(keys),
        ).order_by(PlanTask.created_at.asc())
    )).all():
        titles[key] = title
    for key, title in (await session.execute(
        select(PlanItem.key, PlanItem.title).join(Plan, Plan.id == PlanItem.plan_id)
        .where(Plan.project_id == project.id, PlanItem.key.in_(keys)).order_by(Plan.version.asc())
    )).all():
        titles[key] = title
    return titles


def _name_keys(keys: list[str], shown: int = COVERAGE_KEYS_NAMED_MAX) -> str:
    named = ", ".join(repr(k) for k in keys[:shown])
    return named + (f" and {len(keys) - shown} more" if len(keys) > shown else "")


async def _validate_dropped(
    session: AsyncSession, project: Project, dropped: Any, items: list[dict], unresolved: dict[str, str],
) -> list[dict] | None:
    """`dropped`: `[{key, reason}]`, the unresolved items this plan leaves out on purpose. Returns the clean list with
    each item's title filled in (None when empty). A key must be a real item key of the project and not also an item of
    this plan; the reason is a plain sentence."""
    if not dropped:
        return None
    if not isinstance(dropped, list) or len(dropped) > DROPPED_MAX:
        raise PlanEngineError(f"`dropped` is a list of {{key, reason}}, at most {DROPPED_MAX}")
    in_plan = {str(i.get("key")) for i in items if isinstance(i, dict)}
    clean: list[dict] = []
    seen: set[str] = set()
    for n, row in enumerate(dropped, start=1):
        what = f"dropped entry {n}"
        if not isinstance(row, dict):
            raise PlanEngineError(f"{what} must be {{key, reason}}")
        key = row.get("key")
        if not isinstance(key, str) or not key.strip():
            raise PlanEngineError(f"{what} needs a `key`: the item's key from `unresolved_items`")
        key = key.strip()
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise PlanEngineError(
                f"dropped item {key!r} needs a `reason`: one plain sentence on why this plan leaves it out"
            )
        if len(reason.strip()) > DROPPED_REASON_MAX_CHARS:
            raise PlanEngineError(
                f"dropped item {key!r}'s `reason` is longer than {DROPPED_REASON_MAX_CHARS} characters"
            )
        if key in seen:
            raise PlanEngineError(f"item {key!r} is in `dropped` twice")
        if key in in_plan:
            raise PlanEngineError(f"item {key!r} is both an item of this plan and in `dropped`; keep it in one place")
        seen.add(key)
        clean.append({"key": key, "reason": squash(reason)})
    unknown = seen - set(unresolved)
    titles = await _item_titles(session, project, unknown)
    unknown -= set(titles)
    if unknown:
        raise PlanEngineError(
            f"`dropped` must name this project's real item keys; unknown: {_name_keys(sorted(unknown))}. "
            "Use the `key`s from `unresolved_items` in your task context."
        )
    for row in clean:
        row["title"] = _plain_item_title(unresolved.get(row["key"]) or titles.get(row["key"]) or row["key"])
    return clean


def check_coverage(
    unresolved: dict[str, str], items: list[dict], dropped: list[dict] | None,
) -> None:
    """Refuse a plan that leaves an unresolved item unaccounted for: every key must be an item (any action) or in
    `dropped`. The refusal names the missing keys and the fix."""
    if not unresolved:
        return
    covered = {str(i.get("key")) for i in items if isinstance(i, dict)}
    covered |= {d["key"] for d in dropped or []}
    missing = [k for k in unresolved if k not in covered]
    if not missing:
        return
    raise PlanEngineError(
        f"this plan doesn't account for {len(missing)} unresolved item{'s' if len(missing) != 1 else ''}: "
        f"{_name_keys(missing)}. Every key in `unresolved_items` must appear in one of two places: as an "
        "item (any action), or in `dropped: [{key, reason}]` with the plain reason this plan leaves it out. "
        "Add each missing key to one of them and send the plan again."
    )


async def _account_for_everything(
    session: AsyncSession, project: Project, items: list[dict], dropped: Any,
) -> list[dict] | None:
    """The gate shared by propose and revise: validates `dropped` and refuses a plan that leaves an unresolved item out
    of items and `dropped`. Returns the clean `dropped` list."""
    unresolved = await write_task_unresolved(session, project)
    clean = await _validate_dropped(session, project, dropped, items, unresolved)
    check_coverage(unresolved, items, clean)
    return clean
