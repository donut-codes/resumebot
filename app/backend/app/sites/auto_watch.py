"""Watch the job board of every employer with a role in play (docs/PRD.md 29.9): the owner's setting at the top of
Employers, and the rule it asks the app to keep.

**The setting** is the Settings field `watch_in_play` (`profile.Settings`), stored and saved as the others are
(`avoid_peak`, `job_systems_off`): a change is the next version of the Settings document, with who and why, every other
field kept (`set_on`). It is off unless the owner switches it on, on an install that has never seen it too. Services
flush; the route commits.

**The rule** (`settle`, safe to run as often as asked) works on the employers' own sites, the switch the employer's page
calls "Watch its job board", and switches them the way the owner's switch does (`where.switch`, so a site's project, if
it has one, is paused and resumed with it). The app keeps that it was the app that set a watch (`watch_record.py`),
which is how it tells its own from the owner's:

- With the setting on, an employer with a role in play (`stages.OPEN`: Found, Needs Drafted, Ready to Apply, Applied,
  Active Discussion) is watched; switching the setting on watches every one that has a role in play right then.
- Never one the owner switched off by hand (their word is kept; so is a site that was on once and is off with no
  reason of the app's, for an employer switched off before the record began), never one whose site the app switched
  off because it cannot be read (`Site.off_by`, which the owner, the monthly look and the scout's board clear, and
  this rule never does), never a gated (blocked) employer, and
  never one whose address is on the owner's list of sites to avoid.
- An employer watched this way is unwatched when none of its roles is in play any more, or when it is gated. One the
  owner watched, or switched, is never unwatched by the app. This runs whether or not the setting is on, so switching
  the setting off only stops new watching: the employers it had watched stay watched until their roles leave play, and
  then are unwatched. The owner can switch any of them off on its page, which keeps it off.
- An employer with no board yet has no site to watch; its site is made when a board is found (the start and the
  night's read run `where.seed_employer_sites`, which settles this at its end), and it is watched then if it still has
  a role in play. `settle` runs as well where a role comes into play or leaves it (the scout's finds stored, the owner
  moving a role) and when the owner blocks an employer or lets it back in, so the page is right without waiting for
  the night.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.actors import BY_OWNER
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.profile import store as profile_store
from app.sites import watch_record, where
from app.sites.models import SEARCH, Site

#: Why the owner's save of the setting is kept, as the Settings document's history says it.
WHY = {True: "watch every employer with a role in play", False: "stopped watching every employer with a role in play"}


async def is_on(session: AsyncSession, owner: str) -> bool:
    """Whether the owner switched the setting on."""
    return (await profile_store.current(session, owner)).settings.watch_in_play


async def in_play(session: AsyncSession, owner: str) -> set[uuid.UUID]:
    """The employers that have a role in play."""
    return {employer_id for employer_id in (await session.scalars(
        select(Opportunity.employer_id).where(
            Opportunity.owner == owner, Opportunity.employer_id.is_not(None), Opportunity.status.in_(stages.OPEN)
        ).distinct()
    )).all() if employer_id is not None}


def may_watch(site: Site, employer: Employer) -> bool:
    """Whether the app may switch this employer's site on, which is off: the employer is not gated, the app did not
    switch the site off because it cannot be read, and the owner has not kept it off. The owner's word on the watch
    (`watch_record.set_by`) is kept for good; where none was kept, a site that has been on (`turned_on_at`) and is off
    was switched off by the owner before the record began, and one that never was on was never theirs to refuse."""
    if employer.gated or site.off_by is not None:
        return False
    by = watch_record.set_by(employer)
    if by is None:
        return site.turned_on_at is None
    return by == watch_record.BY_PLAY


async def plan(session: AsyncSession, owner: str) -> list[tuple[uuid.UUID, str, bool]]:
    """What `settle` would switch, read and decided with nothing written: each (site id, employer name, wanted state)
    in the order `settle` goes through them. An employer's decision depends on its own site and mark alone, and one site
    speaks for an employer (the one that is on, else the first made), so the plan applied after is the loop done in
    one."""
    on = await is_on(session, owner)
    playing = await in_play(session, owner)
    rows = (await session.execute(
        select(Site, Employer).join(Employer, Site.employer_id == Employer.id)
        .where(Site.owner == owner, Site.list_name == SEARCH, Employer.owner == owner)
        .order_by(Site.on.desc(), Site.created_at, Site.url)
    )).all()
    todo: list[tuple[uuid.UUID, str, bool]] = []
    seen: set[uuid.UUID] = set()
    for site, employer in rows:
        if employer.id in seen:
            continue
        seen.add(employer.id)
        if site.on:
            if watch_record.set_by(employer) == watch_record.BY_PLAY and (employer.gated or employer.id not in playing):
                todo.append((site.id, employer.name, False))
        elif on and employer.id in playing and may_watch(site, employer):
            todo.append((site.id, employer.name, True))
    return todo


async def settle(session: AsyncSession, owner: str) -> dict:
    """Watch the employers with a role in play when the setting is on, and unwatch the ones the app watched that have
    none, whether or not it is on (the module's rule). Returns the names of the employers watched and unwatched.
    Idempotent. One site speaks for an employer (the one that is on, else the first made)."""
    watched: list[str] = []
    unwatched: list[str] = []
    for site_id, name, wanted in await plan(session, owner):
        if not wanted:
            await where.switch(session, owner, site_id, False, by=watch_record.BY_PLAY)
            unwatched.append(name)
            continue
        try:
            await where.switch(session, owner, site_id, True, by=watch_record.BY_PLAY)
        except where.OnTheOtherList:  # its host is on the owner's list of sites to avoid
            continue
        watched.append(name)
    return {"watched": sorted(watched), "unwatched": sorted(unwatched)}


async def set_on(session: AsyncSession, owner: str, on: bool) -> dict:
    """The owner's setting, saved as the next version of the Settings document (nothing is saved when it already is
    what they asked), then settled: switching it on watches every employer with a role in play now. Returns `{on,
    watched, unwatched}`: the setting, and the names of the employers the app switched. `where.WhereError` when the
    document will not take it."""
    current = (await profile_store.current(session, owner)).settings
    if current.watch_in_play != on:
        try:
            await profile_store.save(
                session, owner, "settings", {**current.model_dump(mode="json"), "watch_in_play": on},
                by=BY_OWNER, why=WHY[on])
        except profile_store.ProfileError as exc:
            raise where.WhereError(str(exc)) from exc
    return {"on": on, **await settle(session, owner)}
