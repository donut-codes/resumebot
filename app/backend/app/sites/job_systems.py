"""Job systems on Where to look (docs/PRD.md 30.3): the services employers post their jobs on
(`ats.SYSTEMS`), each read by the night's read through its public API (`adapters.ADAPTERS`). The owner can
switch a whole system off: the night's read then skips every employer that posts on it (`switched_off_ids`).

- **What is listed** (`listing`): each system at least one employer in the index uses, readable ones first. A system
  with an adapter has a switch; one without is "Can't be read: <why>" and has none. A system the owner switched off
  stays listed, so it can be switched back on, even when no employer uses it any more. The exception is a system whose
  terms deny the app (`ats.NOT_OFFERED`, docs/PRD.md path 55): while it is off it has no row and no switch, whoever
  switched it off; switched on by hand (`switch`, the route), it is listed like any other.
- **Status**, in plain words: "Read nightly at 1:00 AM" (the time of the mission `read_boards`), "Waits: the reading
  mission is paused" while that mission is paused, "Off" when the owner switched it off.
- **Watched employers** (`employer_names`) are the employers the night reads through it: their site is switched on and
  works through the system's built-in reader (`where_view.through_systems`), so Sites to search leaves them to this row,
  until the owner switches the system off. For a system the app can't read they are the watched employers stuck on it:
  not one the night reads anyway through a recipe the scout learned for its site, which Sites to search shows as read.
  **Roles found** are the distinct roles found at it that reached Found: ones its postings became, and ones whose link
  is on it (`where_view.roles_by_source`), 0 when there are none; the list behind the number (`roles_of`) is the same
  roles.
- **The switch** is the Settings field `job_systems_off` (`profile.Settings`), so a change is the next version of the
  Settings document, with who and why, and every other field kept. Services flush; the route commits.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import adapters, ats
from app.jobsearch.employers import not_checked
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import record
from app.jobsearch.models import Employer
from app.jobsearch.postings import site_reads
from app.jobsearch.profile import store as profile_store
from app.sites import registry, where, where_view
from app.sites.models import BY_APP, SEARCH, Site, SiteSkill, read_is

OFF = "Off"
#: What a system with no reader says when nothing is known for sure of why it has none (`System.why_not` says it where
#: it is): none has been built yet, which says nothing against building one.
NO_READER = "no reader has been built for it yet"


def can_read(key: str) -> bool:
    return key in adapters.ADAPTERS


def why_not(key: str) -> str:
    """Why a system the app has no reader for can't be read (`System.why_not`; docs/PRD.md 28.6)."""
    system = ats.SYSTEMS.get(key)
    return (system.why_not if system else None) or NO_READER


async def off_systems(session: AsyncSession, owner: str) -> set[str]:
    """The systems the owner switched off."""
    return set((await profile_store.current(session, owner)).settings.job_systems_off)


async def switched_off_ids(session: AsyncSession, owner: str) -> set[uuid.UUID]:
    """The employers that post on a system the owner switched off: the night's read leaves them alone."""
    off = await off_systems(session, owner)
    if not off:
        return set()
    return set((await session.scalars(
        select(Employer.id).where(Employer.owner == owner, Employer.ats.in_(off)))).all())


async def _employer_names(
    session: AsyncSession, owner: str, keys: list[str], rows: list[dict] | None = None,
    through: dict[str, str] | None = None,
) -> dict[str, list[dict]]:
    """The watched employers of each system in `keys`, as `[{id, name}]` by name. A system the app can read: the
    employers whose site works through its built-in reader (`where_view.through_systems`, whether or not the system is
    on). One it can't: the watched employers whose board is on it, which no reader fetches. `rows` and `through` are
    `where_view.listing` and `where_view.through_systems` when the caller has them already."""
    names: dict[str, list[dict]] = {key: [] for key in keys}
    if any(can_read(key) for key in keys):
        if rows is None:
            rows = await where_view.listing(session, owner, with_roles=False)
        if through is None:
            through = await where_view.through_systems(session, owner, rows)
        for row in rows:
            if through.get(row["id"]) in names:  # the label of an employer's site is the employer's name
                names[through[row["id"]]].append({"id": row["employer_id"], "name": row["label"]})
    stuck = [key for key in keys if not can_read(key)]
    if stuck:
        watched = (await session.execute(
            select(Employer, Site.id).join(Site, Site.employer_id == Employer.id)
            .where(Employer.owner == owner, Site.owner == owner, Site.list_name == SEARCH, Site.on.is_(True),
                   read_is(BY_APP), Employer.ats.in_(stuck)).distinct())).all()
        # A system with no built-in reader can still be read for one employer, through a recipe the scout learned for
        # its site: that employer is read every night, so it is not stuck on the system.
        read = await _read_by_recipe(session, [site_id for _, site_id in watched])
        for employer, site_id in watched:
            if employer.ats and site_id not in read:
                names[employer.ats].append({"id": str(employer.id), "name": employer.name})
    return {key: sorted(found, key=lambda e: (e["name"].lower(), e["id"])) for key, found in names.items()}


async def _read_by_recipe(session: AsyncSession, site_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """The sites among `site_ids` whose skill in force is a recipe the night runs for an employer's site
    (`site_reads.LEARNED`): the ones read nightly though their employer's system has no reader of its own."""
    kinds: dict[uuid.UUID, str | None] = {}
    if site_ids:
        for site_id, recipe in (await session.execute(
            select(SiteSkill.site_id, SiteSkill.recipe)
            .where(SiteSkill.site_id.in_(site_ids), SiteSkill.retired_at.is_(None)).order_by(SiteSkill.version)
        )).all():
            kinds[site_id] = (recipe or {}).get("kind")  # the newest version last wins
    return {site_id for site_id, kind in kinds.items() if kind in site_reads.LEARNED}


async def shown(session: AsyncSession, owner: str, only: str | None = None) -> list[str]:
    """The keys of the systems `listing` shows: each one an employer uses or the owner switched off, or just `only`.
    A system whose terms deny the app (`ats.NOT_OFFERED`) is left out while it is off. Only the systems the registry has
    a runbook for are shown, in the order they are asked (`registry.order`): a copy that ships fewer runbooks lists
    fewer systems."""
    if only is not None:
        return [only]
    used = set((await session.scalars(
        select(Employer.ats).where(Employer.owner == owner, Employer.ats.is_not(None)).distinct())).all())
    off = await off_systems(session, owner)
    return [k for k in registry.order()
            if k in ats.SYSTEMS and (k in used or k in off) and not (k in off and k in ats.NOT_OFFERED)]


async def listing(
    session: AsyncSession, owner: str, only: str | None = None, *, rows: list[dict] | None = None,
    through: dict[str, str] | None = None, roles: dict[str, list[uuid.UUID]] | None = None,
) -> list[dict]:
    """The job systems Where to look shows, each as `{key, name, domains, can_read, on, status, employer_names, roles}`:
    `domains` is the hosts it serves employers' pages on (the page matches them against the known site terms); readable
    systems first, by name. `on` is the owner's switch (False for a system with no reader); `status` is the words above;
    `employer_names` is the watched employers the night reads through it, `[{id, name}]` (for one with no reader, the
    watched employers stuck on it), and `roles` how many roles were found at it and reached Found (0 when none).
    `only`: just that system, whether or not an employer uses it. A caller that has read the sites already passes
    `rows` (`where_view.listing`), `through` (`where_view.through_systems`) and `roles` (`where_view.roles_by_source`,
    with at least these systems), so none of it is read twice."""
    off = await off_systems(session, owner)
    read = where_view.read_words(await missions.standing(session, owner, record.READ))
    keys = await shown(session, owner, only)
    names = await _employer_names(session, owner, keys, rows, through)
    if roles is None:
        roles = await where_view.roles_by_source(session, owner, systems=keys)
    out = []
    for key in keys:
        readable = can_read(key)
        switched_on = readable and key not in off
        out.append({
            "key": key, "name": ats.label(key), "domains": list(ats.SYSTEMS[key].domains),
            "can_read": readable, "on": switched_on,
            "status": read if switched_on else OFF if readable else f"Can't be read: {why_not(key)}",
            "employer_names": names[key], "roles": len(roles[key]),
        })
    return sorted(out, key=lambda r: (not r["can_read"], r["name"].lower()))


async def whole_page(session: AsyncSession, owner: str) -> dict:
    """What Where to look shows (`site_routes`): `{sites, job_systems}`, the sites (`where_view.page`) and the job
    systems (`listing`), everything they both need read once."""
    sites = list((await session.scalars(select(Site).where(Site.owner == owner))).all())
    keys = await shown(session, owner)
    roles = await where_view.roles_by_source(
        session, owner, sites=[s for s in sites if s.list_name != where.AVOID], systems=keys)
    rows = await where_view.listing(session, owner, sites=sites, roles=roles)
    through = await where_view.through_systems(session, owner, rows)
    return {"sites": await where_view.page(session, owner, rows=rows, through=through),
            "job_systems": await listing(session, owner, rows=rows, through=through, roles=roles)}


async def roles_of(session: AsyncSession, owner: str, key: str, offset: int = 0, limit: int = 10) -> dict:
    """The roles found at a job system that reached Found (`where_view.roles_by_source`), newest first, a page at a
    time. `where.NoSuchSite` for a system there is none of."""
    if key not in ats.SYSTEMS:
        raise where.NoSuchSite("no such job system")
    ids = (await where_view.roles_by_source(session, owner, systems=[key]))[key]
    return await where_view.role_page(session, owner, ids, offset, limit)


async def switch(session: AsyncSession, owner: str, key: str, on: bool) -> dict:
    """The owner's switch on a job system, saved as the next version of the Settings document. Refused, in words, for
    a system that does not exist (`where.NoSuchSite`) and for one with no reader, which has no switch."""
    if key not in ats.SYSTEMS:
        raise where.NoSuchSite("no such job system")
    name = ats.label(key)
    if not can_read(key):
        raise where.WhereError(f"{name} can't be read ({why_not(key)}), so it has no switch")
    current = (await profile_store.current(session, owner)).settings
    off = set(current.job_systems_off)
    if on:
        off.discard(key)
    else:
        off.add(key)
    if off != set(current.job_systems_off):
        try:
            await profile_store.save(
                session, owner, "settings", {**current.model_dump(mode="json"), "job_systems_off": sorted(off)},
                by="owner", why=f"switched {name} {'on' if on else 'off'}")
        except profile_store.ProfileError as exc:
            raise where.WhereError(str(exc)) from exc
        await not_checked.settle(session, owner)  # a system switched off is not read: its postings are Not checked
    await session.flush()
    [row] = await listing(session, owner, only=key)
    return row
