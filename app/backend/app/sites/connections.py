"""The few facts about the scout and the kinds of work that the site loop reads.

There is one scout that holds every site step, and Check is code, so none of
that is needed: the scout takes every kind of step it has a kind for, and nobody else checks. What is here is what the
other modules call:

- `_content_or_none`: the kinds and words (`app.harness`), or None when a kind file is broken (a broken content file
  never stops a task from being handed out);
- `scout_connection`: the scout's `AgentConnection` (resumebot's roster: `app.agents.roster`);
- `SCOUT_KINDS` and `doer_kinds_for`: the kinds the scout may be given. The runner is resumebot's own, so what it can do
  is stated here, not declared by it: reading a site (`site.read`, which builds or fixes the site's skill) and looking
  something up (`porter.explore`). A kind outside the list is refused in a plan even when its file is switched on.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import roster
from app.harness import HarnessError, harness
from app.harness.loader import Harness
from app.models.agent import AgentConnection
from app.sites.models import Project


async def _content_or_none(session: AsyncSession, owner: str) -> Harness | None:
    """The kinds of work and the words; None when a content file is broken, so no hand-out is held for it."""
    try:
        return harness()
    except HarnessError:
        return None


#: The kinds of work the scout does for a site project.
SCOUT_KINDS = ("site.read", "porter.explore")


async def scout_connection(session: AsyncSession, owner: str) -> AgentConnection | None:
    """The scout's connection, or None before it has been made (`roster.ensure_roster`)."""
    return await roster.connection_by_slug(session, owner, roster.SCOUT)


async def doer_kinds_for(session: AsyncSession, project: Project) -> list[str] | None:
    """The kinds the scout may be given for this project: `SCOUT_KINDS` (a kind whose file is switched off is still
    left out by the harness). Never None: the scout is the one agent that does a site project's items."""
    return list(SCOUT_KINDS)
