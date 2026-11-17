"""The Act wrap-up has a fixed shape, so it reads the same whichever model writes it.

`act.complete` takes the summary as named parts, each a short plain sentence or two:

- `planned`: what the round set out to do.
- `done`: what was actually done.
- `checks`: what the checks found.
- `learned`: what was learned (or that nothing new was).
- `next`: what happens next, and what to look at first if something went off plan.

The code checks the shape and nothing else: a missing, empty, unknown or over-long part is refused,
with every problem in one answer (the way `plan.propose` does). The writing is the model's. The parts are kept on the
Act task (`data.summary`) and as plain words in its `evidence` (`render`), so every place that shows the summary (the
project page, Activity, the report) shows the same lines. A summary written before this (free text) has no parts and
reads as it always did: `parts_of` returns None and `evidence` is shown as written.

The five parts together stay inside the 2,000-character summary cap, far under `AGENT_PAYLOAD_BUDGET_CHARS`.
"""

from __future__ import annotations

from app.sites.base import PlanEngineError
from app.sites.models import PlanTask
from app.textutil import squash

#: `(part, label, what it holds)`, in the order they are read.
PARTS: tuple[tuple[str, str, str], ...] = (
    ("planned", "Planned", "what the round set out to do"),
    ("done", "Done", "what was actually done"),
    ("checks", "Checks", "what the checks found"),
    ("learned", "Learned", "what was learned, or that nothing new was"),
    ("next", "Next", "what happens next, and what to look at first if something went off plan"),
)
NAMES = tuple(name for name, _, _ in PARTS)
PART_MAX = 300
DATA_KEY = "summary"
SHAPE = "summary is `{planned, done, checks, learned, next}`: " + "; ".join(f"`{n}` {what}" for n, _, what in PARTS)


def parse(raw: object) -> dict[str, str]:
    """The summary's parts as stored, or a refusal naming every problem at once."""
    if not isinstance(raw, dict):
        raise PlanEngineError(f"act.complete needs the {SHAPE}; each a short plain sentence or two")
    problems: list[str] = []
    out: dict[str, str] = {}
    for name, _, what in PARTS:
        value = raw.get(name)
        if value is None:
            problems.append(f"summary is missing `{name}`: {what}")
        elif not isinstance(value, str):
            problems.append(f"summary.{name} must be plain text")
        elif not squash(value):
            problems.append(f"summary.{name} is empty: say {what}")
        elif len(squash(value)) > PART_MAX:
            problems.append(f"summary.{name} is longer than {PART_MAX} characters: keep it to a sentence or two")
        else:
            out[name] = squash(value)
    names = ", ".join(NAMES)
    for extra in sorted(set(raw) - set(NAMES)):
        problems.append(f"summary has no part called `{extra}`; its parts are {names}")
    if problems:
        raise PlanEngineError("\n".join(problems))
    return out


def render(parts: dict[str, str]) -> str:
    """The parts as the plain lines every surface shows: "Planned: ...", "Done: ...", and so on."""
    return "\n".join(f"{label}: {parts[name]}" for name, label, _ in PARTS if parts.get(name))


def parts_of(task: PlanTask | None) -> dict[str, str] | None:
    """The parts an Act task was completed with, or None for a summary written as free text."""
    raw = (task.data or {}).get(DATA_KEY) if task is not None else None
    return {k: str(v) for k, v in raw.items()} if isinstance(raw, dict) and set(raw) == set(NAMES) else None


def done_text(task: PlanTask | None) -> str | None:
    """What was done, for the line a finished project says: the `done` part when the summary has parts, else the
    free-text summary as it was written."""
    if task is None:
        return None
    parts = parts_of(task)
    return parts["done"] if parts else task.evidence


def stored(summary: object) -> tuple[str, dict[str, str]]:
    """`(evidence text, parts)` for a summary given to `complete_act`: checked, then rendered as plain lines."""
    parts = parse(summary)
    return render(parts), parts
