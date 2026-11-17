"""The facts the round's gateway reads about a plan's site (docs/PRD.md 29.3), read with no model.

The deciding is the process file's: one gateway over the
published facts `start.unreadable` and `start.needs_browser` (`playbooks/sites/project.round.bpmn`, PR-7N). This module
only reads the facts the gateway needs and the words that say why:

- a site plan only reads public pages, so it starts without asking: the owner is never asked about a plan;
- a plan that says its site cannot be read starts nothing (the owner decided not to spend cycles on something that will
  fail). The scout says so in an item's data. `asks_not_to_be_read`: the site refuses to be read by anyone, so it is
  switched off and the owner told (`unreadable_clause`, the round's step PR-7U). `needs_browser`: the server cannot
  read it but the browser has not been tried (a page that only loads in one, a sign-in), so the site is set to be read
  in the owner's browser and that path decides (`needs_browser`, step PR-7B); only if the browser fails is it switched
  off (`where.turn_off_unreadable`);
- a project that has used `ROUND_CAP` rounds is given up the same way as a site that asks not to be read: the only
  going-in-circles rule is the app's, never a question to the owner.

There are no other safeguards (kinds within a daily limit, a destructive kind, standing rules,
wheel-spinning): a site loop has one kind of work, reading, and no standing rules.

Also here (a second, separate meaning of "autostart", docs/PRD.md 29.1): `ensure_projects` gives a site the owner
switched on a project of its own, so the round that learns to read it starts without anyone asking. The night's Scout
mission calls it (`jobsearch/missions/core.py`, at most three new projects a night, `by_switch`): a switch never starts
one.
Nor does a skill that breaks (29.4): the night's read retires it (`where.retire_broken`) and the next mission starts
the project.
"""

from urllib.parse import urlparse

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites.models import (
    BY_APP,
    DEFAULT_ROUND_BUDGET,  # noqa: F401  (read from here by `resolution._out_of_rounds` and the tests)
    SEARCH,
    Plan,
    PlanItem,
    PlanItemAction,
    PlanStatus,
    Project,
    Site,
    SiteSkill,
    read_is,
)

#: A plan version that ran (or is running): the rounds a project has used.
RAN = (PlanStatus.approved, PlanStatus.superseded)
#: What a project started for a switched-on site is for (the owner's words are the site's own, never a person's).
OUTCOME = "This site's job listings can be read every night"
#: The most rounds the app lets one site project run before it gives the site up. Within it the app carries on by
#: itself (the owner is never asked "keep going?"); past it the site is switched off with a plain reason on Where to
#: look (`TOO_MANY_CLAUSE`), and tried again about monthly like any site that cannot be read.
ROUND_CAP = 15
TOO_MANY_CLAUSE = "it took too many tries to learn how to read it"
#: The item data the scout sets when a site needs the owner (Plan item `data` keys).
BROWSER_KEY = "needs_browser"
NOT_READ_KEY = "asks_not_to_be_read"


async def rounds_before(session: AsyncSession, project: Project, plan: Plan) -> int:
    """How many plan versions ran before this one."""
    earlier = (await session.scalars(
        select(Plan.id).where(Plan.project_id == project.id, Plan.version < plan.version, Plan.status.in_(RAN))
    )).all()
    return len(earlier)


async def over_cap(session: AsyncSession, project: Project, plan: Plan) -> bool:
    """Whether this plan would be a round past `ROUND_CAP`."""
    return await rounds_before(session, project, plan) + 1 > ROUND_CAP


async def do_items(session: AsyncSession, plan: Plan) -> list[PlanItem]:
    return list((await session.scalars(
        select(PlanItem).where(PlanItem.plan_id == plan.id, PlanItem.action == PlanItemAction.do)
        .order_by(PlanItem.position)
    )).all())


#: Why the site is handed to the browser (`needs_browser`) and why it is switched off (`unreadable_clause`): one plain
#: clause each, the one the owner reads.
BROWSER_CLAUSE = "its listings only load in a browser"
NOT_READ_CLAUSE = "it asks not to be read"


async def _flagged(session: AsyncSession, plan: Plan, key: str) -> bool:
    return any((i.data or {}).get(key) for i in await do_items(session, plan))


async def unreadable_clause(session: AsyncSession, plan: Plan) -> str | None:
    """Why the plan's site cannot be read by anyone, as one plain clause: a `do` item of the plan says it asks not to
    be read (`asks_not_to_be_read`: its robots rules or its terms refuse), or the project has used `ROUND_CAP` rounds
    on it already (`TOO_MANY_CLAUSE`). None for any other plan. The round's gateway reads it as the fact
    `start.unreadable` (PR-7N)."""
    if await _flagged(session, plan, NOT_READ_KEY):
        return NOT_READ_CLAUSE
    project = await session.get(Project, plan.project_id)
    return TOO_MANY_CLAUSE if project is not None and await over_cap(session, project, plan) else None


async def needs_browser(session: AsyncSession, plan: Plan) -> bool:
    """Whether the plan says the server cannot read the site but the browser might (`needs_browser`: its listings only
    load in a browser, or it asks for a sign-in), and nothing says the site refuses to be read at all. The round's
    gateway reads it as the fact `start.needs_browser` (PR-7N)."""
    return await _flagged(session, plan, BROWSER_KEY) and await unreadable_clause(session, plan) is None


def title_for(site: Site) -> str:
    """The title of a project started for a site: its address without the scheme ("Read careers.example.test")."""
    parts = urlparse(site.url if "//" in site.url else f"//{site.url}")
    return f"Read {(parts.netloc or site.url).strip().rstrip('/')}"[:255]


NEW, SWITCHED, BROKE = "new", "switched", "broke"


async def _why_project(
    session: AsyncSession, site: Site, *, by_switch: bool = False,
) -> tuple[str, SiteSkill | None] | None:
    """Why a switched-on site wants a project started now, or None when it does not: it has none that is not
    finished and no skill that works, and then one of

    - `BROKE`: its skill broke (was retired with nothing to replace it) after its last project finished, or it never had
      a project (docs/PRD.md 29.4: a read that returned nothing where it had roles, or a changed shape, starts the
      next cycle). The broken version comes with it;
    - `NEW`: it never had a project;
    - `SWITCHED`, only with `by_switch` (the night's mission, 29.1): the last project ended with no skill and the owner
      has switched the site on again since (`turned_on_at`).

    A project the owner stopped, or that finished without making a skill, is not started again by itself: that is the
    owner's to start (a switch off and on, tonight)."""
    from app.sites import skills  # skills imports the job-search adapters

    open_project = await session.scalar(
        select(Project.id).where(Project.owner == site.owner, Project.site_id == site.id, Project.finished_at.is_(None))
        .limit(1)
    )
    if open_project is not None or await skills.current_skill(session, site.id) is not None:
        return None
    last = await session.scalar(
        select(Project).where(Project.owner == site.owner, Project.site_id == site.id)
        .order_by(Project.created_at.desc()).limit(1)
    )
    broke = await skills.broken(session, site.id, last.finished_at if last is not None else None)
    if broke is not None:
        return BROKE, broke
    if last is None:
        return NEW, None
    if by_switch and site.turned_on_at is not None and last.finished_at is not None \
            and site.turned_on_at > last.finished_at:
        return SWITCHED, None
    return None


async def _broke_words(session: AsyncSession, site: Site, skill: SiteSkill) -> str:
    """What the new project's first plan is told of the skill that broke: its version and kind, and, for an employer's
    site, what the last read said (kept on the employer)."""
    from app.jobsearch.models import Employer  # the job-search tables

    employer = await session.get(Employer, site.employer_id) if site.employer_id else None
    kind = (skill.recipe or {}).get("kind", "recipe")
    said = f" The last read said: {employer.fetch_note}." if employer is not None and employer.fetch_note else ""
    return f"This site's skill (version {skill.version}, a {kind}) stopped working and was retired.{said}"[:1000]


def start_queue(owner: str) -> Select:
    """The sites the night's Scout mission may start a project for, in the order it takes them: the ones switched on
    longest ago first (`ensure_projects`, and the status that says whose turn it is, `where_view._status`)."""
    return (select(Site).where(Site.owner == owner, Site.on.is_(True), Site.list_name == SEARCH, read_is(BY_APP))
            .order_by(Site.turned_on_at.asc().nulls_last(), Site.created_at))


async def ensure_projects(
    session: AsyncSession, owner: str, *, limit: int | None = None, by_switch: bool = False,
) -> list[Project]:
    """Start a project for each site the owner switched on that wants one (`_why_project`), and return the projects
    made. Each is the owner's own (`created_by="you"`: the switch was theirs): a title from the site, the outcome "This
    site's job listings can be read every night" and the default round budget, with its round open and waiting for the
    scout's plan. A site never gets a second project while one is not finished (`create_project` refuses it), so this is
    safe to call as often as the schedule likes. `limit` caps how many are started in one call (the sites switched on
    longest ago come first, the rest wait for the next night); `by_switch` is `_why_project`'s. Services flush, routes
    commit; the caller wakes the scout (`wake_ops.send`) for what was made, after its commit. A site to avoid, one read
    in the owner's own browser and one not read by the app (`models.effective_read`; an `auto` site is tested when it is
    added, `read_probe.settle`, docs/PRD.md 30.3) never gets a project."""
    from app.sites import projects  # projects imports round_process, which imports this module

    made: list[Project] = []
    for site in (await session.scalars(start_queue(owner))).all():
        if limit is not None and len(made) >= limit:
            break
        why = await _why_project(session, site, by_switch=by_switch)
        if why is not None:
            reason, skill = why
            made.append(await projects.create_project(
                session, owner, site.id, title_for(site), OUTCOME, created_by="you",
                context=await _broke_words(session, site, skill) if reason == BROKE and skill else None,
            ))
    return made
