"""Lessons: what a finished round taught, recorded, retired, listed and put in front of the next plan.

Copied from another project's code (app/PROVENANCE.md). A lesson's scope (`applies_to`) is "project" (only the
project it came from), the kind of site it is about ("employer", "job_board", "association") or "owner" (every site
the owner has). Act keeps the site's skill in a `js_site_skills` row, not through a lesson.
"""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites.base import (
    ACT_ACTIONS,
    LESSON_APPLIES_TO_MAX_CHARS,
    LESSON_SOURCE_MAX_CHARS,
    LESSON_TEXT_MAX_CHARS,
    LESSONS_IN_CONTEXT_MAX,
    OWNER_LESSON_TAG,
    PlanEngineError,
)
from app.sites.models import Lesson, PlanItem, PlanTask, PlanTaskStatus, Project, Site
from app.sites.records import _record_activity
from app.textutil import one_line, squash
from app.timeutil import owner_today

PROJECT_LESSON_TAG = "project"
LESSON_SUBJECT_MAX_CHARS = 120
#: Words an agent may use for "every site" that are stored as `OWNER_LESSON_TAG`.
_OWNER_WORDS = {"all", "owner", "everyone"}


async def _site_kind(session: AsyncSession, project: Project) -> str | None:
    """The kind of the site this project is for ("employer", "job_board", "association"), or None."""
    if project.site_id is None:
        return None
    return await session.scalar(select(Site.kind).where(Site.id == project.site_id))


def _project_tags(site_kind: str | None) -> set[str]:
    """The `applies_to` tags a project matches: the owner's own, and the kind of its site."""
    tags = {OWNER_LESSON_TAG}
    if site_kind:
        tags.add(site_kind)
    return tags


def _in_scope_clause(project: Project, site_kind: str | None):
    """The lessons that apply to `project`: the owner's, its site kind's, and its own."""
    return or_(
        Lesson.applies_to.in_(_project_tags(site_kind)),
        (Lesson.applies_to == PROJECT_LESSON_TAG) & (Lesson.source_project_id == project.id),
    )


async def lessons_for_project(session: AsyncSession, project: Project) -> list[dict]:
    """The lessons the scout planning/acting on `project` sees: this owner's rows that are in scope -- the project's
    site kind, "owner", or "project" lessons of this very project -- and not retired, newest first. Selected by scope,
    deterministically: no model, no search."""
    kind = await _site_kind(session, project)
    rows = (await session.scalars(
        select(Lesson).where(
            Lesson.owner == project.owner, Lesson.retired_at.is_(None), _in_scope_clause(project, kind),
        ).order_by(Lesson.created_at.desc(), Lesson.id).limit(LESSONS_IN_CONTEXT_MAX)
    )).all()
    return [
        {"id": str(row.id), "text": row.text, "applies_to": row.applies_to, "source": row.source} for row in rows
    ]


async def lessons_for_kind(session: AsyncSession, owner: str, kind: str) -> list[str]:
    """The sentences of the lessons that apply to a site of this kind (the owner's, and its kind's), not retired, newest
    first: what the session that learns a site in the owner's browser is told (docs/PRD.md 37.2). The same selection as
    `lessons_for_project`, without a project."""
    return list((await session.scalars(
        select(Lesson.text).where(
            Lesson.owner == owner, Lesson.retired_at.is_(None), Lesson.applies_to.in_(_project_tags(kind)),
        ).order_by(Lesson.created_at.desc(), Lesson.id).limit(LESSONS_IN_CONTEXT_MAX)
    )).all())


async def record_learned(session: AsyncSession, owner: str, kind: str, texts: list[str], source: str) -> int:
    """Record what a session learning a site in the owner's browser wrote down, one lesson for each sentence, scoped to
    the site's kind with no project (docs/PRD.md 37.4). A sentence already recorded for that kind is kept once. Returns
    how many were new."""
    made = 0
    for text in texts:
        clean = squash(text)
        if not clean or await session.scalar(select(Lesson.id).where(
                Lesson.owner == owner, Lesson.applies_to == kind, func.lower(Lesson.text) == clean.lower(),
                Lesson.retired_at.is_(None)).limit(1)):
            continue
        session.add(Lesson(owner=owner, text=clean, applies_to=kind, source=source[:LESSON_SOURCE_MAX_CHARS]))
        made += 1
    await session.flush()
    return made


async def lessons_recorded(session: AsyncSession, project: Project, plan_version: int) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(Lesson).where(
            Lesson.source_project_id == project.id, Lesson.plan_version == plan_version,
        )
    ) or 0)


#: Rules resumebot itself enforces: "lessons are about the site or the way sites work, never about the machinery", so a
#: lesson that restates one is refused. A short deny list of patterns: the kind/key rules, the escalation rules, and
#: any naming of one of the app's own tools.
_MACHINERY_PATTERNS = (
    re.compile(r"\b(every|each|all)\s+(plan\s+)?items?\s+(needs?|must\s+have|requires?|has\s+to\s+have)\b"
               r".*\b(kinds?|keys?)\b", re.I),
    re.compile(r"\b(real|valid|known|listed|existing)\s+(keys?|kinds?)\b", re.I),
    re.compile(r"\bescalat\w*\b.*\b(reasons?|options?|recommended|default|checked)\b", re.I),
    re.compile(r"\bat\s+most\s+(3|three)\s+(things|cards|questions)\b", re.I),
    re.compile(r"\b(porter|resumebot)\s+(refuses|enforces|requires|rejects)\b", re.I),
    re.compile(
        r"\b(task|lesson|plan|act|review|project|item|kind|template|instructions?|work|agent|skill)"
        r"\.(propose|revise|complete|escalate|record|retire|next|report|add_tasks|write|read|lookup|list|"
        r"declare|finish|note|publish)\b", re.I),
)


def restates_app_rule(text: str) -> bool:
    """True when a lesson restates a rule the app enforces or names its own machinery."""
    return any(p.search(text or "") for p in _MACHINERY_PATTERNS)


_SOURCE_NOTE_WORDS = re.compile(r"\b(report|notes?)\b", re.I)
_SOURCE_ITEMS = re.compile(r"\bitems?\b[\s:]*([^;]*)", re.I)
_KEY_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.:/-]*", re.I)


async def source_names_this_round(session: AsyncSession, task: PlanTask, source: str) -> bool:
    """True when a lesson's `source` names something from this round: its review report, an owner's note, or an item of
    this plan version (by key: after the word "item", or a bare key of three or more characters)."""
    text = (source or "").strip()
    if not text:
        return False
    if _SOURCE_NOTE_WORDS.search(text):
        return True
    if task.plan_id is None:
        return False
    plan_keys = (await session.scalars(select(PlanItem.key).where(PlanItem.plan_id == task.plan_id))).all()
    keys = {k.casefold() for k in plan_keys}
    named = {t.casefold().rstrip(".:,") for m in _SOURCE_ITEMS.findall(text) for t in _KEY_TOKEN.findall(m)}
    bare = {t.casefold().rstrip(".:,") for t in _KEY_TOKEN.findall(text) if len(t.rstrip(".:,")) >= 3}
    return bool(keys & (named | bare))


SOURCE_REFUSAL = (
    "a lesson needs a `source` from this round: name the items (their keys), the report, or the owner's "
    "note it came from, e.g. \"item list-pages: the page loads ten more on scroll\" or \"the report\""
)


def _retire(session: AsyncSession, project: Project, lesson: Lesson, source: str) -> None:
    """Mark `lesson` retired and leave the trail entry: "Retired: contradicted by <source> on <date>"."""
    now = datetime.now(UTC)
    day = owner_today(now)  # the owner's day, not the UTC date
    lesson.retired_at = now
    lesson.retired_by = one_line(f"contradicted by {source}", 400)
    _record_activity(
        session, project, "app", "lesson_retired",
        f"Retired: “{one_line(lesson.text, 160)}” was contradicted by {one_line(source, 200)} on "
        f"{day:%B} {day.day}, {day.year}.",
    )


def _tag_of(applies_to: str | None) -> str:
    tag = (applies_to or "").strip()
    return OWNER_LESSON_TAG if tag.lower() in _OWNER_WORDS else tag


async def record_lesson(
    session: AsyncSession, task: PlanTask, project: Project, text: str, applies_to: str, source: str | None = None,
    subject: str | None = None,
) -> tuple[Lesson, bool]:
    """`lesson.record`: Act only. One plain sentence about the site or the way sites work (never the app's own
    machinery: a restated rule is refused), scoped by `applies_to` ("project", the site's kind, or "owner"), with a
    `source` naming the items, report or owner's note of this round it came from (refused without). An identical
    sentence for the same scope is kept once (`created` False). With a `subject`, a lesson already in scope on the same
    subject that says something else is retired by this one."""
    if task.action not in ACT_ACTIONS:
        raise PlanEngineError("lessons are recorded from the Act task")
    if task.project_id != project.id:
        raise PlanEngineError("that task isn't on this project")
    if task.status != PlanTaskStatus.in_progress:
        raise PlanEngineError("that task isn't checked out -- call task.next first")
    clean = squash(text)
    if not clean:
        raise PlanEngineError("a lesson needs `text`: one plain sentence")
    if len(clean) > LESSON_TEXT_MAX_CHARS:
        raise PlanEngineError(f"a lesson is one plain sentence, under {LESSON_TEXT_MAX_CHARS} characters")
    tag = _tag_of(applies_to) or PROJECT_LESSON_TAG  # left out, a lesson is about this project only
    kind = await _site_kind(session, project)
    if not tag or len(tag) > LESSON_APPLIES_TO_MAX_CHARS or tag not in {PROJECT_LESSON_TAG, *_project_tags(kind)}:
        site = f", \"{kind}\" (this site's kind)" if kind else ""
        raise PlanEngineError(f"`applies_to` is \"project\"{site} or \"owner\"")
    if restates_app_rule(clean):
        raise PlanEngineError(
            "that restates a rule the app already enforces, or names its own tools. A lesson is about "
            "the site or the way sites work: what was true of its pages, its listings or its paging"
        )
    cite = squash(source)
    if not await source_names_this_round(session, task, cite):
        raise PlanEngineError(SOURCE_REFUSAL)
    same = select(Lesson).where(
        Lesson.owner == project.owner, Lesson.applies_to == tag, func.lower(Lesson.text) == clean.lower(),
        Lesson.retired_at.is_(None),
    )
    if tag == PROJECT_LESSON_TAG:
        same = same.where(Lesson.source_project_id == project.id)
    existing = await session.scalar(same.limit(1))
    if existing is not None:
        return existing, False
    label = squash(subject).casefold()[:LESSON_SUBJECT_MAX_CHARS] or None
    if label:
        older = (await session.scalars(
            select(Lesson).where(
                Lesson.owner == project.owner, Lesson.retired_at.is_(None),
                func.lower(Lesson.subject) == label, _in_scope_clause(project, kind),
                # this round's own lessons stand together; only an earlier round's is contradicted
                or_(
                    Lesson.source_project_id.is_(None), Lesson.source_project_id != project.id,
                    Lesson.plan_version.is_(None), Lesson.plan_version != task.plan_version,
                ),
            )
        )).all()
        for row in older:
            _retire(session, project, row, cite)
    lesson = Lesson(
        owner=project.owner, text=clean, applies_to=tag, subject=label,
        source=cite[:LESSON_SOURCE_MAX_CHARS], source_project_id=project.id, plan_version=task.plan_version,
    )
    session.add(lesson)
    await session.flush()
    return lesson, True


async def retire_lesson(
    session: AsyncSession, task: PlanTask, project: Project, lesson_id: str, source: str | None,
) -> Lesson:
    """`lesson.retire`: Act only. This round's evidence contradicts a lesson that applies here (the same subject, now
    different): it is retired, not deleted, and the trail says by what. The `source` names the items, report or note of
    this round that contradict it (refused without)."""
    if task.action not in ACT_ACTIONS:
        raise PlanEngineError("lessons are retired from the Act task")
    if task.project_id != project.id:
        raise PlanEngineError("that task isn't on this project")
    if task.status != PlanTaskStatus.in_progress:
        raise PlanEngineError("that task isn't checked out -- call task.next first")
    cite = squash(source)
    if not await source_names_this_round(session, task, cite):
        raise PlanEngineError(SOURCE_REFUSAL)
    try:
        wanted = uuid.UUID(str(lesson_id))
    except ValueError as exc:
        raise PlanEngineError("`lesson_id` is the id of a lesson in your task's `lessons`") from exc
    lesson = await session.scalar(
        select(Lesson).where(
            Lesson.id == wanted, Lesson.owner == project.owner,
            _in_scope_clause(project, await _site_kind(session, project)),
        )
    )
    if lesson is None:
        raise PlanEngineError("no such lesson applies to this project")
    if lesson.retired_at is not None:
        return lesson
    _retire(session, project, lesson, cite)
    await session.flush()
    return lesson
