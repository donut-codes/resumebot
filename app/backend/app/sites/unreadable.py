"""A site the server cannot read, from the project's side. The owner decided that a site where scraping or browser
traffic doesn't work is turned off by default and the user is notified, and that no cycles go on something that will
fail.

The owner's rule is that a site is turned off only when neither the server nor the browser can read it. So there are
two ways out of a project for a site the server cannot read, and the scout's (or a plan's) word decides which:

- **The browser has not been tried** (`needs_browser`: its listings only load in a browser, it asks for a sign-in, a
  token or a bot check): `site_needs_browser`. The project ends (an ending of its own, `resolution.TO_BROWSER`), the
  site is set to be read in the owner's browser (`where.hand_to_browser`, as "Ask resumebot to find out" does) and the
  browser reader decides from there. Nothing is switched off and the owner is told nothing. If the browser fails too,
  the browser reader switches it off (`browser_reader.hand_in`, `where.turn_off_unreadable`).
- **The site refuses to be read** (`asks_not_to_be_read`, or `task.escalate` with the reason `cannot_be_read`: its
  robots rules or terms say no): `site_cannot_be_read`. The project ends (`resolution.UNREADABLE`), and the site is
  switched off (`where.turn_off_unreadable`: it records why and that the app did it, and tells the owner once in a
  notification).

Either way every open task of the project is skipped, open plans are set aside, the questions the project had open for
the owner are closed ("resolved by" what was done), no decision is asked and no task is opened. A plan says it in an
item's data and the round's gateway PR-7N reads the facts `start.needs_browser` and `start.unreadable` (steps PR-7B
and PR-7U, `round_process`'s `round.hand_to_browser` and `round.turn_site_off`); the scout says it with
`task.escalate` (`tools.task_escalate`). Switching the site on again starts a project again in the night's mission,
and a second failure does it all again.

Services flush; the route commits.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.work import Notification
from app.sites import asks, escalation, resolution, where
from app.sites.models import Project, Site

#: The reasons of `task.escalate` that say the server cannot read the site: the browser has not been tried, or the site
#: refuses to be read by anyone.
NEEDS_BROWSER, CANNOT_BE_READ = "needs_browser", "cannot_be_read"
#: Where a site the server cannot read goes: to the owner's browser, or off.
TO_BROWSER, OFF = "browser", "off"
#: The note the owner's open questions about a project are closed with, for each way out.
RESOLVED = {
    OFF: "Resolved by the switch-off: resumebot turned the site off because it can't be read ({why}).",
    TO_BROWSER: "Resolved by handing the site to your browser: the server can't read it ({why}).",
}


async def _end(
    session: AsyncSession, project: Project, clause: str, ending: str, kind: str, summary: str, way: str,
) -> Site | None:
    """The project ends with the ending's own resolution, its open questions are closed as resolved by what is done
    next, and the project's site is returned (None when it has none)."""
    if project.finished_at is None:
        project.resolution = project.resolution or await resolution.build(session, project, ending, clause)
        await escalation.end_project(
            session, project, summary, activity=("app", kind, "This project ended."), ending=ending,
            closes_asks=RESOLVED[way].format(why=clause))
    await asks.settle_project_asks(session, project, RESOLVED[way].format(why=clause))
    site = await session.get(Site, project.site_id) if project.site_id is not None else None
    return site if site is not None and site.owner == project.owner else None


async def site_cannot_be_read(session: AsyncSession, project: Project, reason: str | None) -> Notification | None:
    """The site of `project` refuses to be read, so nothing can read it: the project ends, the questions it had open for
    the owner are closed, and the site is switched off with the owner told once (`where.turn_off_unreadable`). `reason`
    is why, in one plain clause (`where.clause_of`). Returns the owner's notification, or None when the site was off
    already (nothing was switched, so nothing is said). Called from inside the round's own step too (the project's run
    ends there)."""
    clause = where.clause_of(reason)
    site = await _end(
        session, project, clause, resolution.UNREADABLE, "site_unreadable", f"This site can't be read: {clause}.", OFF)
    return await where.turn_off_unreadable(session, site, clause) if site is not None else None


async def site_needs_browser(session: AsyncSession, project: Project, reason: str | None) -> bool:
    """The server cannot read the site of `project` but the browser has not been tried: the project ends, the
    questions it had open for the owner are closed, and the site is set to be read in the owner's browser
    (`where.hand_to_browser`), where the browser reader decides. Nothing is switched off and no one is told: a browser
    that fails too switches it off (`where.turn_off_unreadable`). `reason` is why, in one plain clause. True when the
    site was handed over (False: it was read in the browser already, or the project has no site). Called from inside the
    round's own step too (the project's run ends there)."""
    clause = where.clause_of(reason)
    site = await _end(
        session, project, clause, resolution.TO_BROWSER, "site_to_browser",
        f"This site is read in the browser: {clause}.", TO_BROWSER)
    return await where.hand_to_browser(session, site, clause) if site is not None else False
