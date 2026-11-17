"""The rule for a release that ships a changed process file (docs/PRD.md path 16.2).

A release always wins. At every start (`settle`), a shipped process file that differs from the one the owner's newest
version was made from is saved as the next version (`edit_process.use_shipped`): the owner's own version stays in the
history to read or restore. Nothing is asked.

Everything here flushes; the caller commits.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import edit_process


async def settle(session: AsyncSession) -> int:
    """Keep the shipped process files as versions and adopt every newer shipped file over the owner's version. Returns
    how many files were adopted."""
    adopted = 0
    for newer in await edit_process.reconcile(session):
        await edit_process.use_shipped(session, newer.name)
        adopted += 1
    return adopted
