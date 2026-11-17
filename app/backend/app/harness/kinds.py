"""What a plan item's `kind` must satisfy, and the compact views of kinds agents are given.

The refusals are plain sentences that name the problem and the fix, so a small planner can correct
a plan in one try. Items that fail for the same reason are named together.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.harness.loader import Harness, Kind

#: At most this many item keys are named in one refusal line; the rest are counted.
NAMED_KEYS_MAX = 5


def porter_made(kind: Kind) -> bool:
    """A kind no agent does and no plan lists: it runs itself in code (`do.runner: porter`)."""
    return kind.do.runner == "porter"


_TYPE_WORDS = {
    "int": "a whole number", "string": "text", "bool": "true or false", "list": "a list", "object": "an object",
}


def _is_type(value: Any, type_name: str) -> bool:
    if type_name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str) and bool(value.strip())
    if type_name == "bool":
        return isinstance(value, bool)
    if type_name == "list":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return False


def result_problems(kind: Kind, result: Any, *, done: bool) -> list[str]:
    """Plain refusal lines for a `task.complete` `result` against the kind's `result` schema; empty
    when it is fine (a kind that declares no `result` takes none and ignores one sent). A done task must carry every
    required field; an off-plan one may carry none, but what it does send is still checked."""
    shape = ", ".join(
        f"{name} ({_TYPE_WORDS[spec.type]}{'' if spec.required else ', optional'})"
        for name, spec in kind.result.items()
    )
    if not kind.result:
        return []
    if result is None or result == {}:
        needed = [n for n, s in kind.result.items() if s.required]
        if done and needed:
            return [f"{kind.kind} needs a `result` with {', '.join(needed)}; its fields are: {shape}"]
        return []
    if not isinstance(result, dict):
        return [f"`result` is an object with these fields: {shape}"]
    problems = []
    unknown = sorted(set(result) - set(kind.result))
    if unknown:
        problems.append(f"`result` has {', '.join(unknown)}, which {kind.kind} doesn't report; its fields are: {shape}")
    for name, spec in kind.result.items():
        value = result.get(name)
        if value is None:
            if spec.required and done:
                means = f" ({spec.means})" if spec.means else ""
                problems.append(f"`result` is missing {name}{means}")
        elif not _is_type(value, spec.type):
            problems.append(f"`result.{name}` isn't {_TYPE_WORDS[spec.type]}")
    return problems


def usable_kind_ids(h: Harness, doer_kinds: list[str] | None) -> list[str]:
    """The kinds a planner may use now: switched on, and (when a doer is known) declared by it."""
    declared = None if doer_kinds is None else set(doer_kinds)
    return sorted(
        k.kind for k in h.kinds.values()
        if k.enabled and not porter_made(k) and (declared is None or k.kind in declared)
    )


def _usable_phrase(ids: list[str]) -> str:
    return ", ".join(ids) if ids else "none yet"


def _name_items(keys: list[str]) -> str:
    shown = ", ".join(repr(k) for k in keys[:NAMED_KEYS_MAX])
    if len(keys) > NAMED_KEYS_MAX:
        shown += f" and {len(keys) - NAMED_KEYS_MAX} more"
    return (f"items {shown}" if len(keys) > 1 else f"item {shown}")


def kind_problems(
    items: list[dict], h: Harness, doer_kinds: list[str] | None, *, required: bool,
) -> list[str]:
    """Plain refusal lines for `items`, empty when every item is fine.

    `required`: a `do` item must carry a kind (the agent doors); without it only a kind that is
    given is checked. `doer_kinds`: the kinds the project's doer declared, or None when no doer is
    known yet (the capability check is then skipped)."""
    usable = _usable_phrase(usable_kind_ids(h, doer_kinds))
    # (verb for one item, verb for several, rest of the sentence) -> item keys
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for row in items:
        key = str(row.get("key"))
        kind_id = row.get("kind")
        needs_kind = required and row.get("action") == "do"
        if kind_id is None or kind_id == "":
            if needs_kind:
                groups[("has", "have", f"no `kind`; kinds you can use: {usable}")].append(key)
            continue
        if not isinstance(kind_id, str):
            groups[("has", "have", f"a `kind` that isn't text; kinds you can use: {usable}")].append(key)
            continue
        kind = h.kinds.get(kind_id)
        if kind is None:
            groups[("uses", "use", f"kind {kind_id!r}, which doesn't exist; kinds you can use: {usable}")].append(key)
            continue
        if porter_made(kind):
            groups[(
                "uses", "use",
                f"kind {kind_id!r}, which the app makes itself (or runs itself), never a plan; "
                f"kinds you can use: {usable}",
            )].append(key)
            continue
        if not kind.enabled:
            why = kind.needs or "it isn't switched on"
            groups[("uses", "use", f"kind {kind_id!r}: the doer can't run {kind_id} yet: {why}")].append(key)
            continue
        if doer_kinds is not None and kind_id not in doer_kinds:
            groups[(
                "uses", "use",
                f"kind {kind_id!r}: the doer can't run {kind_id} yet, it hasn't declared it; "
                f"kinds you can use: {usable}",
            )].append(key)
            continue
        data = row.get("data") or {}
        for name, spec in kind.data.items():
            if name not in data or data[name] is None:
                if spec.required:
                    means = f" ({spec.means})" if spec.means else ""
                    groups[("is", "are", f"missing {name}{means}, which {kind_id} needs")].append(key)
            elif not _is_type(data[name], spec.type):
                wrong = f"{name} that isn't {_TYPE_WORDS[spec.type]}, which {kind_id} needs"
                groups[("has", "have", wrong)].append(key)
    return [
        f"{_name_items(keys)} {one if len(keys) == 1 else many} {rest}"
        for (one, many, rest), keys in groups.items()
    ]


# --- compact views for agents -----------------------------------------------------------------


def kind_view(kind: Kind) -> dict:
    """One kind, compact, for a planner: what it is, what data it needs."""
    view: dict[str, Any] = {
        "id": kind.kind, "title": kind.title,
        "data": {
            name: f"{spec.type}{'' if spec.required else ', optional'}: {spec.means}"[:110]
            for name, spec in kind.data.items()
        },
        "done_means": kind.done_means,
    }
    return view


def kind_spec(kind: Kind) -> dict:
    """A kind's whole file for the doer or a check: data, how it is done, what done means, the check."""
    spec_view: dict[str, Any] = {
        "id": kind.kind, "title": kind.title,
        "data": {
            name: f"{spec.type}{'' if spec.required else ', optional'}: {spec.means}"
            for name, spec in kind.data.items()
        },
        "do": {"runner": kind.do.runner},
        "risk": kind.risk,  # `read_only`: a step that only looks, which carries `tried` when it goes off plan (13.1)
        "done_means": kind.done_means,
        "check": {"test": kind.check.test, "causes": list(kind.check.causes)},
    }
    if kind.result:
        # What `task.complete` carries as `result` (required fields for a done task).
        spec_view["result"] = {
            name: f"{spec.type}{'' if spec.required else ', optional'}: {spec.means}"
            for name, spec in kind.result.items()
        }
    return spec_view


def planner_kinds(h: Harness, doer_kinds: list[str] | None) -> list[dict]:
    return [kind_view(h.kinds[k]) for k in usable_kind_ids(h, doer_kinds)]
