"""Where to look, over HTTP (docs/PRD.md path 29, step 8, and 30.3): the owner's two lists, the sites to search and the
sites to avoid, adding to either, the owner's switch on each site to search, and how it is read (resumebot tests it,
the app, or their own browser); and the job systems the night reads through, each with a switch. Switching a site on
starts nothing now: its project, if it needs one, is started in the night's Scout mission. A site left to resumebot to
test is tried once, by the engine, when the request that adds it (or switches it to that) asks it to (the ask
`site.probe`; the page never fetches): the request waits at most 15 seconds, so the row that comes back says what was
found, or, when the engine does not answer in time, is the row as saved. Adding a site to avoid or taking one off the
list is checked the same way (`site.regate`), and `said` is the line that says so. A site there is none of is 404,
one on the other list 409, anything else the owner asked for that cannot be done 422 (`where.WhereError`)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.db import ReadSession, Session
from app.errors import Unprocessable
from app.jobsearch import off_turn
from app.jobsearch.work import engine_calls
from app.settings import settings
from app.sites import auto_watch, job_systems, registry, site_terms, where, where_view
from app.sites.models import AUTO, AVOID, SEARCH, Site

router = APIRouter(prefix="/api/v1/sites")
#: A page of roles: where to start and how many (`roles` of a site, `roles` of a job system).
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=500)]
#: How long a route waits for the engine's answer to its ask, in seconds: the engine's test of a site takes at most 15
#: (`read_probe.PROBE_SECONDS`). Past it the route answers the row as saved, and the ask stays for the engine.
ENGINE_WAIT_S = 15.0


class AddSite(BaseModel):
    url: str
    #: Not needed: the page no longer asks. An employer's site is made for its employer; the sites the owner adds are
    #: job boards. Still accepted (`employer`, `job_board`, `association`).
    kind: str = "job_board"
    #: "search" (a site to search) or "avoid" (a site to avoid).
    list: str = "search"
    #: "auto" (resumebot finds out), "app" (the night reads it) or "browser" (the owner's own browser reads it).
    read_by: str = AUTO


class ChangeSite(BaseModel):
    """What the owner changes on a site's row: its switch, or how it is read (at least one)."""

    on: bool | None = None
    read_by: str | None = None


class ReadRobotsExempt(BaseModel):
    """The owner's switch on the read of a site's signed-out job search though its robots.txt asks automated readers
    not to (docs/PRD.md path 48)."""

    on: bool


class ChangeSystem(BaseModel):
    """The owner's switch on a job system."""

    on: bool


async def _page(session: AsyncSession) -> dict:
    return await job_systems.whole_page(session, settings.owner)


def _needs_probe(site: Site | None) -> bool:
    """Whether a switched-on site to search that is left to resumebot (`auto`) has not been tested yet. The engine tests
    it (the ask `site.probe`: `read_probe.settle`, code, no tokens, at most 15 seconds), so the row the request answers
    with says what was found."""
    return bool(site is not None and site.list_name == SEARCH and site.on and site.read_by == AUTO
                and site.read_found is None)


async def _row(session: AsyncSession, site_id: uuid.UUID) -> dict:
    """The site's row as it is now, read apart from the writer's turn (what the request changed is committed by now)."""
    async with off_turn.reading(session) as reader:
        return (await where_view.listing(reader, settings.owner, site_id))[0]


@router.get("")
async def sites(session: ReadSession) -> dict:
    """What Where to look shows. `sites`: the sites that are read on their own, on both lists, switched-on ones first:
    `{id, url, kind, label, on, employer_id, state, status, skill, needs, roles, added_by, list, read_by, read_found,
    read_note, created_at}`. `list` is `search` or `avoid`; `read_by` is `auto`, `app` or `browser` and an `auto` site's
    `read_found` and `read_note` say what was found and why; `status` is what is true of the site now, in plain words;
    `created_at` is when it was added; `roles` is how many roles were found there that got past the gates (0 when
    none). An employer's site is here only while it is switched on, and not while it is read through a job system that
    is on (that system's row has it). `job_systems`: the services employers post on that the night reads through,
    `{key, name, can_read, on, status, employer_names, roles}`: `employer_names` is the watched employers read through
    it, `[{id, name}]`. `watch_in_play`: whether the owner switched on watching the job board of every employer with a
    role in play (docs/PRD.md 29.9; set with `PUT /employers/watch-in-play`). `read_robots_exempt`: whether the owner
    switched on the read of a site's signed-out job search though its robots.txt asks automated readers not to
    (docs/PRD.md path 48; set with `PUT /sites/robots-exempt`); null when no runbook carries `robots`, so there is
    nothing to switch and the page shows no control. `terms`: the
    known sites whose terms forbid automated tools, `[{hosts, terms_url}]`, for the one warning line the page shows
    (docs/PRD.md 55.10); it informs and changes nothing. A job system's row carries its `domains` to match."""
    board = registry.board_reader("guest")
    carried = board is not None and bool(registry.current().robots_runbooks())
    return {**await _page(session), "terms": site_terms.forbidding(),
            "watch_in_play": await auto_watch.is_on(session, settings.owner),
            "read_robots_exempt": await board.is_on(session, settings.owner) if carried and board else None}


@router.put("/robots-exempt")
async def read_robots_exempt(body: ReadRobotsExempt, session: Session) -> dict:
    """Switch the night's read of a site's signed-out job search, though its robots.txt asks automated readers not to,
    on or off (a field of the Settings profile, so the change is its next version). Off by default. Where no runbook
    carries `robots` there is nothing to switch: it answers `{"on": false, "note": "nothing to switch"}` and saves
    nothing."""
    board = registry.board_reader("guest")
    if board is None or not registry.current().robots_runbooks():
        return {"on": False, "note": "nothing to switch"}
    saved = await board.set_on(session, settings.owner, body.on)
    await session.commit()
    return {"on": saved}


@router.get("/job-systems/{key}/roles")
async def system_roles(key: str, session: ReadSession, offset: Offset = 0, limit: Limit = 10) -> dict:
    """The roles found at a job system that got past the gates, newest first: `{total, items: [{id, title, company,
    status, fit, fit_pct, found_at}]}` (as a site's). 404 for a system there is none of."""
    return await job_systems.roles_of(session, settings.owner, key, offset, limit)


@router.get("/{site_id}/roles")
async def site_roles(site_id: uuid.UUID, session: ReadSession, offset: Offset = 0, limit: Limit = 10) -> dict:
    """The roles found at a site that got past the gates, newest first: `{total, items: [{id, title, company, status,
    fit, fit_pct, found_at}]}` (`status` is the role's stage as stored, `fit` its stored fit and `fit_pct` that as a
    share of what the owner's asks allow, either null before it is scored, `found_at` when the role was made). 404 for
    a site there is none of."""
    return await where_view.roles_of_site(session, settings.owner, site_id, offset, limit)


@router.patch("/job-systems/{key}")
async def change_system(key: str, body: ChangeSystem, session: Session) -> dict:
    """Switch a job system on or off (a field of the Settings profile, so the change is its next version). While it is
    off the night's read skips the employers that post on it. 404 for a system there is none of; 422, in words, for one
    the app has no reader for. Answers the whole page, since the sites on that system change their status."""
    await job_systems.switch(session, settings.owner, key, body.on)
    await session.commit()
    async with db.read_session_factory()() as reader:
        return await _page(reader)


@router.post("")
async def add(body: AddSite, session: Session) -> dict:
    """Add a site to a list (`list`: a site to search, or to avoid); adding to search switches it on. One left to
    resumebot to test (`read_by` `auto`, the default) is tried by the engine once before the answer (waited for at most
    15 seconds), so the row says what was found. A site to avoid stops the postings read from it: the engine checks the
    log again, and `said` is the line it says (absent when it did not answer in time; the check is kept and runs when it
    can). 409, in words, when it is already on the other list."""
    site = await where.add(session, settings.owner, body.url, body.kind, list_name=body.list, read_by=body.read_by)
    site_id, url = site.id, site.url
    said = None
    if site.list_name == AVOID:  # a site to avoid stops the postings read from it, now (docs/PRD.md 39.14)
        asked = await engine_calls.ask(session, settings.owner, "site.regate",
                                       {"cause": f"{url} was added to the sites you avoid"}, ENGINE_WAIT_S)
        said = (asked or {}).get("said")
    elif _needs_probe(site):  # the site is saved with the ask, which commits before it waits
        await engine_calls.ask(session, settings.owner, "site.probe", {"site_id": str(site_id)}, ENGINE_WAIT_S)
    else:
        await session.commit()
    row = await _row(session, site_id)
    return {**row, "said": said} if said else row


@router.patch("/{site_id}")
async def change(site_id: uuid.UUID, body: ChangeSite, session: Session) -> dict:
    """The owner's switch (`on`) and how the site is read (`read_by`). Starts nothing: a site that needs a project gets
    it in the night's mission. Changing it to `auto` has the engine try the site before the answer (waited for at most
    15 seconds; past that, the row as saved). 409, in words, when it cannot be switched on because it is on the list to
    avoid."""
    if body.on is None and body.read_by is None:
        raise Unprocessable("say what to change: on, or read_by")
    if body.read_by is not None:
        await where.set_read_by(session, settings.owner, site_id, body.read_by)
    if body.on is not None:
        await where.switch(session, settings.owner, site_id, body.on)
    if _needs_probe(await session.get(Site, site_id)):  # the change is saved with the ask, which commits first
        await engine_calls.ask(session, settings.owner, "site.probe", {"site_id": str(site_id)}, ENGINE_WAIT_S)
    else:
        await session.commit()  # a refusal commits nothing: a change made before it goes with the session
    return await _row(session, site_id)


@router.delete("/{site_id}")
async def remove(site_id: uuid.UUID, session: Session) -> dict:
    """Take a site off the list to avoid. A site to search is switched off, not removed. `said` is the line the removal
    says: the postings that were stopped only for being on that site are back in line (docs/PRD.md 39.14). The engine
    checks the log; `said` is absent when it did not answer in time (the check is kept and runs when it can)."""
    site = await session.get(Site, site_id)
    host = site.url if site is not None and site.owner == settings.owner else "A site"
    await where.remove(session, settings.owner, site_id)  # refuses a site that is not on the list to avoid
    asked = await engine_calls.ask(session, settings.owner, "site.regate",
                                   {"cause": f"{host} was taken off the sites you avoid"}, ENGINE_WAIT_S)
    said = (asked or {}).get("said")
    return {"ok": True, "said": said} if said else {"ok": True}
