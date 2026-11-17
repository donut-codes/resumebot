"""What the earlier items of a plan found, handed to the item that builds on them.

A plan can have a tool item that "saved a report" and a next item, "Open the saved report and count the stale
entries", that goes to a specialist with no command that reads a saved file. A file a tool saves
lives in the runner's private folder, which is wiped when the run ends: nothing but the run's own result, evidence
and summary outlives it, and those are in the app. So the app hands them on: every model-run do item sees what the
items before it in the same plan version found, compactly (one line each, `result` trimmed, the rest named by key),
instead of the doer having to find a file. The "Built to work on small models" rule: the app supplies the facts it
already has.

Read-only, no commit. A tool-run item never carries it (its tool takes its `data` only)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.planengine.budget import payload_chars
from app.sites.models import PlanItemAction, PlanTask, PlanTaskMode, PlanTaskStatus
from app.textutil import one_line

#: The most earlier items shown (the nearest ones: a follow-up builds on what came just before it).
MAX_EARLIER_ITEMS = 6
#: What one item may take of the payload, and what all of them may.
ITEM_MAX_CHARS = 900
EARLIER_MAX_CHARS = 4_000
#: What a result's own text, list and map are trimmed to.
_TEXT_MAX = 200
_LIST_KEEP = 3
_MAP_KEEP = 12
_DEPTH = 3
#: Shown for an item whose run left nothing but a bare acknowledgement.
NOTHING_KEPT = "it reported no data of its own"
NOTE = (
    "What the items before this one found, as resumebot holds it. Use it instead of looking for a file or a "
    "page an earlier item saved: a saved file can't be opened from here, and nothing else is kept of it."
)


def trim(value: Any, depth: int = _DEPTH) -> Any:
    """A result made small: long text cut, a long list cut to its first few with its length said, a wide map
    cut to its first keys; numbers, booleans and short text stay as they are."""
    if isinstance(value, str):
        return one_line(value, _TEXT_MAX)
    if isinstance(value, dict):
        if depth <= 0:
            return f"{{{len(value)} fields}}"
        kept = {str(k): trim(v, depth - 1) for k, v in list(value.items())[:_MAP_KEEP]}
        if len(value) > _MAP_KEEP:
            kept["more_fields"] = len(value) - _MAP_KEEP
        return kept
    if isinstance(value, list):
        if depth <= 0:
            return f"[{len(value)} items]"
        kept_list = [trim(v, depth - 1) for v in value[:_LIST_KEEP]]
        return {"count": len(value), "first": kept_list} if len(value) > _LIST_KEEP else kept_list
    return value


def _row(task: PlanTask) -> dict:
    row: dict[str, Any] = {"key": task.item_key, "title": one_line(task.title, 160)}
    data = {k: v for k, v in (task.result or {}).items() if k != "tool_version"}
    if data:
        row["result"] = trim(data)
    found = one_line(task.evidence or "", 300)
    if found:
        row["found"] = found
    if "result" not in row and found.lower() in ("", "ok", "done", "ran"):
        row["found"] = NOTHING_KEPT
    if payload_chars(row) > ITEM_MAX_CHARS:  # still too long: its fields named, its text cut
        row["result"] = sorted(data)[:_MAP_KEEP] if data else None
        row["found"] = one_line(row.get("found", ""), 200)
        row = {k: v for k, v in row.items() if v is not None}
    return row


async def earlier_results(session: AsyncSession, task: PlanTask) -> dict:
    """`{"earlier": {"note", "items"}}` for a do item that follows finished do items of the same plan version, or
    `{}` when none did. The nearest `MAX_EARLIER_ITEMS`, oldest first, within `EARLIER_MAX_CHARS`."""
    if task.plan_id is None or task.mode != PlanTaskMode.do or task.action != PlanItemAction.do:
        return {}
    before = PlanTask.position < task.position
    rows = (await session.scalars(
        select(PlanTask).where(
            PlanTask.project_id == task.project_id, PlanTask.plan_id == task.plan_id,
            PlanTask.mode == PlanTaskMode.do, PlanTask.action == PlanItemAction.do,
            PlanTask.status == PlanTaskStatus.done, PlanTask.superseded.is_(False), before,
        ).order_by(PlanTask.position.desc()).limit(MAX_EARLIER_ITEMS)
    )).all()
    items: list[dict] = []
    used = 0
    for row in rows:  # nearest first, so what is cut for room is the oldest
        item = _row(row)
        size = len(json.dumps(item, default=str, ensure_ascii=False)) + 1
        if items and used + size > EARLIER_MAX_CHARS:
            break
        items.append(item)
        used += size
    if not items:
        return {}
    return {"earlier": {"note": NOTE, "items": list(reversed(items))}}
