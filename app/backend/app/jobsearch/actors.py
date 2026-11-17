"""Who did it, in the words the app uses for itself and for the owner.

Two kinds of word. The machine values stored in a column that something reads back (`BY_OWNER` on a saved version,
`YOU` on the run the owner started) are constants, so a spelling is never typed twice. The words a person reads (the
Activity line, a mission's history, who saved a version of a process file) name the owner by `actor_name`: the name in
their About page, else "You". Services flush; nothing here writes.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.models import ProfileVersion

#: `ProfileVersion.saved_by` for a version the owner saved.
BY_OWNER = "owner"
#: `ProfileVersion.saved_by` and `MissionChange.by` for what the app itself did.
BY_APP = "resumebot"
#: Who asks for a favorite's packet (`favorites.py`): the app, on the owner's star. It is the app's ask, so the last
#: gate check runs before the writer drafts (`packets._final_gates`), and it is no hand move.
BY_FAVORITE = "favorite"
#: `MissionRun.started_by` for a run the owner started with Run now.
YOU = "you"
#: `MissionRun.started_by` for a run the owner's Claude started through the action door (`owner_actions`, PRD path 50).
CLAUDE = "claude"
#: The Activity line's actor (its column is 10 characters) and who, for what the owner's Claude did on demand.
CLAUDE_ACTOR, CLAUDE_WHO = "claude", "Claude (on demand)"
#: Who the app's own code-first browser worker is called: `CODE_CHECKER` in a line of history and on the Agents page and
#: the Pipeline, `CODE_CHECKER_BY` the `by` its hand-backs pass (`sourcing._who`).
CODE_CHECKER = "Code checker"
CODE_CHECKER_BY = "code checker"
#: What the owner is called until their About page says a name.
UNNAMED = "You"
#: The shortest column that holds a name (`MissionChange.by` is 80).
MOST_CHARS = 80


async def actor_name(session: AsyncSession, owner: str) -> str:
    """The owner as the page names them: the name in their About page, else "You"."""
    data = await session.scalar(
        select(ProfileVersion.data).where(ProfileVersion.owner == owner, ProfileVersion.kind == "about")
        .order_by(ProfileVersion.version.desc()).limit(1))
    name = " ".join(str((data or {}).get("name") or "").split())
    return name[:MOST_CHARS] or UNNAMED
