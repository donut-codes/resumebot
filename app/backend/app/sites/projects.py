"""Creating site projects: a project is a site the owner wants searched, learned in rounds (docs/PRD.md path 29).

Copied from another project's code (app/PROVENANCE.md). A project opens one way, and it names a site: the
owner starts one for a site (PRT-2). The project gets its first planning task, "Write plan v1", and the round's run
opens and waits for the scout to write it (`round_process.drive_opened`). Waking the scout is not here: the caller sends
it after its commit (`wake_ops.send`).
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites import round_process
from app.sites.base import PlanEngineError
from app.sites.models import (
    AVOID,
    BY_BROWSER,
    PlanItemAction,
    PlanTask,
    PlanTaskMode,
    PlanTaskStatus,
    Project,
    Site,
    effective_read,
)
from app.sites.records import _record_activity

#: The round's start event: the owner started the project.
OWNER_STARTS = "PRT-2"


async def _next_priority(session: AsyncSession, owner: str) -> int:
    """A new project goes last in the projects list's own order."""
    current_max = await session.scalar(select(func.max(Project.priority)).where(Project.owner == owner))
    # `is None`, not `or`: a first project holds priority 0, which is falsy.
    return (-1 if current_max is None else current_max) + 1


async def create_project(
    session: AsyncSession, owner: str, site_id: uuid.UUID, title: str, outcome: str, *, created_by: str = "you",
    context: str | None = None, requested_by: str | None = None,
) -> Project:
    """A project for a site. `created_by` is "you": the owner started it (a site they switched on, or asked for). The
    project gets its first planning task and the round's run opens, waiting for the plan."""
    if not title or not title.strip():
        raise PlanEngineError("a project needs a title")
    if not outcome or not outcome.strip():
        raise PlanEngineError("a project needs an outcome")
    if created_by != "you":
        raise PlanEngineError("a project is started by the owner (you)")
    site = await session.get(Site, site_id)
    if site is None or site.owner != owner:
        raise PlanEngineError("a project is for one of the owner's sites")
    if site.list_name == AVOID:
        raise PlanEngineError("this site is on the owner's list of sites to avoid, so it is never read",
                              "leave it alone; it has no project")
    read = effective_read(site)
    if read == BY_BROWSER:
        raise PlanEngineError("this site is read in the owner's own browser, not by the app, so it has no project",
                              "leave it alone")
    if read is None:
        raise PlanEngineError("resumebot has not tested how this site is read yet, so it has no project yet",
                              "leave it alone")
    already =await session.scalar(
        select(Project.id).where(
            Project.owner == owner, Project.site_id == site_id, Project.finished_at.is_(None),
        ).limit(1)
    )
    if already is not None:
        raise PlanEngineError("this site already has a project that is not finished")
    project = Project(
        owner=owner, site_id=site_id, title=title.strip(), outcome=outcome.strip(), created_by=created_by,
        priority=await _next_priority(session, owner),
    )
    session.add(project)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:  # another request made this site's project between the check above and here
        raise PlanEngineError("this site already has a project that is not finished") from None
    _record_activity(session, project, "you", "created", f"You started {project.title}.")
    session.add(PlanTask(
        owner=owner, project_id=project.id, plan_id=None, plan_version=0, position=0, item_key="plan_v1",
        title="Write plan v1", group_label="Plan", action=PlanItemAction.plan, mode=PlanTaskMode.plan,
        status=PlanTaskStatus.todo,
        context={"outcome": project.outcome, "context": context, "requested_by": requested_by or created_by},
    ))
    await session.flush()
    # The round's run opens, waiting for the plan.
    await round_process.drive_opened(session, project, OWNER_STARTS)
    return project
