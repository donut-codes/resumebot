"""How a project ended, in three plain parts.

Every project that ends carries one `resolution`: `{problem, done, outcome}`.

- `problem`: what the project was for (its outcome, the owner's words).
- `done`: what was done, from the rounds.
- `outcome`: how it ended: resolved and how it was confirmed, or "stopped: here is what is left and who has it".

Who writes it. The scout writes it as part of the closing round's Act (`act.complete`'s `resolution`), against the
project's own outcome (`closing_hint`), and only for a project the review accepted or that only looked. Every other
ending (a stop) has no agent closing it, so the app writes it here from what it knows. A project whose Act gave none
gets the same deterministic one, so a finished project is never without. No model is called in the deterministic path.

Copied from another project's code (app/PROVENANCE.md): the endings that belong to goals, incidents and setups
(withdrawn, left, goal off, setup, not needed, unanswered) and the backfill of projects that ended before the
resolution existed are not here. Each part is held to `PART_MAX` characters, so a resolution stays inside the agent
payload budget wherever it is handed back (the Act task's `closing` line, a project page).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites import act_summary, project_summary
from app.sites.base import PlanEngineError
from app.sites.models import (
    Plan,
    PlanItemAction,
    PlanStatus,
    PlanTask,
    PlanTaskMode,
    PlanTaskStatus,
    Project,
    ReviewReport,
)
from app.textutil import cut_at_word, squash, upper_first

PARTS = ("problem", "done", "outcome")
PART_MAX = 280

#: How a project can end. The word is what `end_project` and the finishers are told; the owner never sees it.
ACCEPTED = "accepted"  # the review accepted the work and Act is done
STOPPED = "stopped"  # the owner stopped it (a stuck round, out of rounds)
UNREADABLE = "unreadable"  # the site refuses to be read, so resumebot switched it off (`unreadable.py`)
TO_BROWSER = "to_browser"  # the server cannot read it, so it is handed to the owner's browser (`unreadable.py`)
ENDINGS = (ACCEPTED, STOPPED, UNREADABLE, TO_BROWSER)

_WHO_HAS_IT = "It is with you now."
#: What the outcome of a project handed to the owner's browser says around the one clause that gives the reason.
HANDED_TO_BROWSER, CANT_READ_FROM_SERVER = "Handed to your browser: ", ", so the app can't read it from the server"


def browser_reason(outcome: object) -> str | None:
    """Why a project handed its site to the owner's browser, as one plain sentence ("Its listings only load in a
    browser."), read from the outcome the project ended with (`build`); None for any other outcome. The sentence a
    site's row says it was found out by, for a site handed over before the row kept it (`Site.read_note`)."""
    text = squash(outcome).rstrip(". ") if isinstance(outcome, str) else ""
    if not (text.startswith(HANDED_TO_BROWSER) and text.endswith(CANT_READ_FROM_SERVER)):
        return None
    clause = text[len(HANDED_TO_BROWSER):len(text) - len(CANT_READ_FROM_SERVER)].strip()
    return f"{upper_first(clause)}." if clause else None


def _clean(text: Any, limit: int = PART_MAX) -> str:
    """One part as the owner reads it: on one line, whole words, cut with an ellipsis."""
    return cut_at_word(squash(text), limit)


def _sentence(text: Any, limit: int = PART_MAX) -> str:
    """`text` as one sentence with a capital start and an end mark ("" when nothing is left)."""
    out = _clean(text, limit).rstrip(" .")
    if not out:
        return ""
    out = upper_first(out)
    return out if out.endswith(("!", "?", "…")) else f"{out}."


def parse(raw: object) -> dict[str, str]:
    """The scout's resolution as stored, or a refusal that says how to fix it. All three parts are needed, each plain
    words of its own; nothing is half-kept."""
    if not isinstance(raw, dict):
        raise PlanEngineError(
            "resolution is `{problem, done, outcome}`: what was wrong, what was done and how it ended, "
            "each a plain sentence or two"
        )
    out: dict[str, str] = {}
    for part in PARTS:
        value = raw.get(part)
        text = _sentence(value) if isinstance(value, str) else ""
        if not text:
            raise PlanEngineError(f"resolution needs a `{part}`: a plain sentence for the owner")
        out[part] = text
    extra = sorted(set(raw) - set(PARTS))
    if extra:
        raise PlanEngineError("resolution has three parts only: problem, done and outcome")
    return out


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


# --- what the app knows --------------------------------------------------------------------------------------


async def problem_line(session: AsyncSession, project: Project) -> str:
    """What the project was for: its outcome in the owner's words, else its title."""
    purpose = project_summary.purpose_of(project.outcome)
    return _sentence(purpose) or _sentence(project.title) or "A site to learn."


async def _rounds(session: AsyncSession, project: Project) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(ReviewReport).where(ReviewReport.project_id == project.id)
    ) or 0)


async def _step_counts(session: AsyncSession, project: Project) -> tuple[int, int]:
    """`(done, off plan)` do-steps over every round, never counting one that was replaced."""
    rows = (await session.execute(
        select(PlanTask.status, func.count()).where(
            PlanTask.project_id == project.id, PlanTask.mode == PlanTaskMode.do,
            PlanTask.action == PlanItemAction.do, PlanTask.superseded.is_(False),
        ).group_by(PlanTask.status)
    )).all()
    by = dict(rows)
    return by.get(PlanTaskStatus.done, 0), by.get(PlanTaskStatus.off_plan, 0)


async def _closing_act(session: AsyncSession, project: Project) -> PlanTask | None:
    return await session.scalar(
        select(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.action == PlanItemAction.act,
            PlanTask.status == PlanTaskStatus.done, PlanTask.superseded.is_(False),
        ).order_by(PlanTask.updated_at.desc()).limit(1)
    )


async def _what_was_done(session: AsyncSession, project: Project) -> str:
    """What was done, from the rounds: the closing Act's own first sentence when there is one, else the plain count of
    steps and rounds."""
    act = await _closing_act(session, project)
    said = project_summary.finished_words(act_summary.done_text(act))
    if act is not None and said != project_summary.FINISHED:
        return _sentence(said)
    done, off = await _step_counts(session, project)
    rounds = await _rounds(session, project)
    if not done and not off:
        return "Nothing was changed."
    line = f"{_count(done, 'step')} done" + (f", {off} not as planned" if off else "")
    return _sentence(f"{line}, over {_count(max(rounds, 1), 'round')}")


async def _what_is_left(session: AsyncSession, project: Project) -> list[str]:
    """The newest plan's open items, in a few words each, for a project that ended with work left."""
    from app.sites import loop  # the loop imports this module

    plan = await session.scalar(
        select(Plan).where(
            Plan.project_id == project.id, Plan.status.in_((PlanStatus.approved, PlanStatus.superseded)),
        ).order_by(Plan.version.desc()).limit(1)
    )
    if plan is None:
        return []
    rows = (await loop.unresolved_items(session, project, plan)).get("unresolved_items") or []
    return [t for r in rows if (t := _clean(r.get("title"), 70).rstrip(". "))][:3]


def _out_of_rounds(project: Project, rounds: int) -> bool:
    from app.sites.autostart import DEFAULT_ROUND_BUDGET

    return rounds >= (project.round_budget or DEFAULT_ROUND_BUDGET)


async def _how_it_ended(session: AsyncSession, project: Project, ending: str, reason: str | None) -> str:
    if ending == ACCEPTED:
        return "Resolved: the last check confirmed it."
    if ending == TO_BROWSER:  # `reason` is the one clause that says why (`where.clause_of`)
        return _sentence(f"{HANDED_TO_BROWSER}{reason or 'the server cannot read it'}{CANT_READ_FROM_SERVER}")
    if ending == UNREADABLE:  # `reason` is the one clause that says why (`where.clause_of`)
        return _sentence(f"Stopped: it can't be read ({reason or 'no way to read it was found'}), so resumebot "
                         "switched the site off")
    rounds = await _rounds(session, project)
    left = await _what_is_left(session, project)
    head = f"Stopped after using its {_count(rounds, 'round')}." if _out_of_rounds(project, rounds) else "Stopped."
    rest = f" Still open: {'; '.join(left)}." if left else " Nothing more is planned."
    return _sentence(f"{head}{rest} {_WHO_HAS_IT}", PART_MAX + 40)


async def build(session: AsyncSession, project: Project, ending: str, reason: str | None = None) -> dict[str, str]:
    """The resolution the app writes itself, from the records. Deterministic: the same records give the same words,
    and every part is filled."""
    if ending not in ENDINGS:
        raise ValueError(f"unknown ending {ending!r}")
    return {
        "problem": await problem_line(session, project),
        "done": await _what_was_done(session, project),
        "outcome": await _how_it_ended(session, project, ending, reason),
    }


async def close(
    session: AsyncSession, project: Project, ending: str, reason: str | None = None,
    *, agent: dict[str, str] | None = None,
) -> dict[str, str]:
    """Write the resolution as the project ends, once: a resolution already there is kept. `agent` is the closing Act's
    own (already parsed); a project nobody closed gets the app's."""
    if project.resolution:
        return dict(project.resolution)
    resolution = dict(agent) if agent else await build(session, project, ending, reason)
    project.resolution = resolution
    return resolution


async def closing_hint(session: AsyncSession, project: Project) -> dict[str, str]:
    """What the closing Act is handed: the problem as the app would say it, so the scout starts from it, and the
    outcome the project was asked to reach, which the scout judges this round's evidence against before it ends the
    project."""
    hint = {"problem": await problem_line(session, project)}
    if (project.outcome or "").strip():
        hint["outcome"] = project.outcome.strip()
    return hint


def of_act(task: PlanTask | None) -> dict[str, str] | None:
    """The resolution the closing Act task was completed with, if any."""
    raw = (task.data or {}).get("resolution") if task is not None else None
    try:
        return parse(raw) if raw else None
    except PlanEngineError:
        return None
