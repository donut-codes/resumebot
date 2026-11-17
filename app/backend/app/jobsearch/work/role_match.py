"""Which pass matched a role to a target role (docs/PRD.md 30.5): three passes, each more certain than the one before,
and a later pass overwrites an earlier one, never the reverse. (1) `code`: the role's title holds one target role's
example title as a whole phrase (`by_title`). (2) `haiku`: the small title session. (3) `analyst`: the Second Impression
read, final. A role records the pass in `Opportunity.matched_by`; None means never decided. Data and pure functions,
and `code_pass`, the one-time free pass over the roles already stored."""

from collections.abc import Iterable
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import queue

CODE, HAIKU, ANALYST = "code", "haiku", "analyst"
#: The higher the rank, the more certain the pass.
RANK = {CODE: 1, HAIKU: 2, ANALYST: 3}
#: The stages a role is live in.
LIVE = (stages.FIRST_IMPRESSION, stages.FOLLOWUP_NEEDED, stages.APPLIED, stages.ACTIVE_DISCUSSION,
        stages.READY_TO_LAUNCH, stages.SECOND_IMPRESSION)


class _Decided(Protocol):
    matched_by: str | None


class _Target(Protocol):
    id: str
    titles: list[str]

    def says_something(self) -> bool: ...


def may_write(role: _Decided, tier: str) -> bool:
    """Whether the pass `tier` may write the role's target role: over no answer, a lower pass or its own, never over a
    higher one."""
    return RANK[tier] >= RANK.get(role.matched_by or "", 0)


def _words(text: str) -> str:
    """Lower case, every run of characters that are not letters or digits as one space, trimmed."""
    out: list[str] = []
    gap = True
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
            gap = False
        elif not gap:
            out.append(" ")
            gap = True
    return "".join(out).strip()


def by_title(title: str, target_roles: Iterable[_Target]) -> str | None:
    """The id of the one target role whose example title the role's title holds as a whole phrase, case and punctuation
    ignored; None when none does or when two or more do (the next pass decides)."""
    padded = f" {_words(title)} "
    found: list[str] = []
    for role in target_roles:
        if not role.says_something():
            continue
        for example in role.titles:
            words = _words(example)
            if words and f" {words} " in padded:
                found.append(role.id)
                break
    return found[0] if len(found) == 1 else None


#: Roles written to by one statement.
CHUNK = 500


async def code_pass(session: AsyncSession, owner: str) -> tuple[int, int]:
    """The free title pass over the live roles that have no answer (no pass recorded and no target role): each whose
    title holds one target role's example title as a whole phrase gets that role, `matched_by` code and the newest
    criteria version, written as Core `UPDATE`s of `CHUNK` roles; then one to-do post for the whole pass. A role with
    any `matched_by` is never touched, so a second run changes nothing. No model is asked. Returns (roles looked at,
    roles matched). The caller commits."""
    profile = await profile_store.current(session, owner)
    targets = list(profile.criteria.target_roles)
    rows = (await session.execute(select(Opportunity.id, Opportunity.title).where(
        Opportunity.owner == owner, Opportunity.status.in_(LIVE), Opportunity.matched_by.is_(None),
        Opportunity.target_role.is_(None)))).all()
    by_role: dict[str, list[object]] = {}
    for role_id, title in rows:
        found = by_title(title or "", targets)
        if found is not None:
            by_role.setdefault(found, []).append(role_id)
    matched = sum(len(ids) for ids in by_role.values())
    if matched:
        version = await profile_store.newest_version(session, owner, "criteria")
        for target, ids in by_role.items():
            for at in range(0, len(ids), CHUNK):
                await session.execute(update(Opportunity).where(
                    Opportunity.owner == owner, Opportunity.id.in_(ids[at:at + CHUNK]),
                    Opportunity.matched_by.is_(None)
                ).values(target_role=target, matched_by=CODE, matched_by_version=version))
        queue.post_all(session, owner, [(queue.POOL_OWED, [""]), (queue.DECK_DIRTY, [""])],
                       cause="core:role_match.code")
    return len(rows), matched
