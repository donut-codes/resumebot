"""What a saved change does at once, in code, as part of the save (docs/PRD.md 39.23), once the store has written the
new version: the engine (asked by the page's save and restore) and the owner door's `profile.restore` all call
this one function, so a version made any of these ways is followed by the same work. The store's own `save` already
runs what belongs to the document (Prioritization's scores worked out again, a deleted tab's release); what is here
reads other parts of the app that read the store, which the store cannot import. Services flush; the caller
commits."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.gating import regate


async def run(session: AsyncSession, owner: str, kind: str, version: int | None, how: str, said: list[str]) -> None:
    """A new version of Hard pass checks the whole log and every role again (`regate.py`, 39.14); the line that says
    what it did is put first in `said`. A new version of What to look for judges nothing again (31.4, 39.16): it changes
    what future searches look for, not how a posting or a role is graded. `how` is how the new version came to be, in
    words a role's history says it ("was saved", "was made by bringing version 3 back"). Nothing for a save that
    changed nothing (`version` is None) or a part that nothing runs again here."""
    if version is None or kind != "gates":
        return
    said[:0] = [(await regate.run(session, owner, cause=f"Hard pass version {version} {how}")).said]
