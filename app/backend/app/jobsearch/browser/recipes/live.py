"""A site's recipe is its reader (the owner, 2026-10-22). A site that has a live
`SiteRecipe` (made by `browser/recipes/record.py`) is read each night by that recipe, with no model and no browser, and
its
postings go into the index like any board's (`site_reads.py`). The browser no longer reads the site's listings
(`browser_reader.browser_sites`). This module is what keeps the recipe honest.

**Details.** A recipe with a `detail` request has each posting's description read by it (`with_details`), for the
postings the index holds no text of yet, at most `DETAILS_A_NIGHT` a night: every request goes through the night's
polite fetcher (robots.txt, about one request a second a host, an honest agent, a refusal is a stop).

**Health, each night.** A night that failed, read no postings, read far fewer than the recent median, or no longer
fills in the required fields is a bad night (`recipes.judge_night`); zero rows is a failure and never "nothing new".
A block is a stop and not a bad night: it never asks for a rewrite. After `recipes.STALE_AFTER` (two) bad nights in a
row the recipe is stale, and the site goes back to the browser meanwhile (a stale recipe is not live); a re-record
is asked for (`recipe_record.request`, reason `repair`): at most one rewrite of a site a week, and after
`recipes.REWRITES_MAX` failed rewrites the recipe needs the owner's attention. The rewrite, once verified, is live
at once.

**A host's posting recipe** (`SiteRecipe` with a `host` and no site, kind `posting`) reads the single-posting pages
of an employer host that has no site row, for the browser's read items on that host (`browser_code.read_by_code`): the
posting is read by code, and a page it cannot read goes to the browser as before. Its health is the reads of the day:
a day that failed some and read none is a bad day, and after the same number of bad days in a row it is stale (the
browser covers) and a re-record is asked for (`recipe_record.request_host`).
Services flush; the night's step commits."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.browser import reader as br
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch.polite import Fetcher, timeup
from app.jobsearch.models import JobPosting
from app.sites import recipes, skills
from app.sites.models import Site, SiteRecipe, SiteRecording
from app.timeutil import owner_today

log = logging.getLogger(__name__)

#: How many detail requests one site's night may make (the fetcher's own pace and the night's time apply too).
DETAILS_A_NIGHT = 400


async def with_details(session: AsyncSession, fetch: Fetcher, row: SiteRecipe, listing, source_id) -> int:
    """Put on each posting of the listing that the index holds no text of the description its recipe's detail request
    gives. Returns how many details were read. A posting's page failing is no reason to stop the site's night; a block
    is."""
    recipe = skills.parse_recipe(row.recipe)
    if not isinstance(recipe, skills.RequestRecipe) or recipe.detail is None:
        return 0
    have = set((await session.scalars(select(JobPosting.ext_id).where(
        JobPosting.owner == row.owner, JobPosting.employer_id == source_id,
        func.length(func.coalesce(JobPosting.text, "")) > 0))).all())
    await db.release_if_free(session)  # up to 400 pages at the polite pace: none read inside a transaction
    read, left = 0, DETAILS_A_NIGHT
    for p in listing.postings:
        ext_id = p.get("ext_id")
        if left <= 0 or timeup(fetch):
            break
        if not ext_id or ext_id in have:
            continue
        left -= 1
        try:
            got = await skills.read_detail(fetch, recipe, ext_id)
        except Exception as exc:
            if recipes.blocked(exc):
                break
            continue
        read += 1
        if got.text:
            p["text"] = got.text
    return read


async def _ask_repair(session: AsyncSession, row: SiteRecipe, site: Site, now: datetime) -> str | None:
    """Ask for a re-record of a site whose recipe is stale: not when one is asked for already, not within a week of the
    last, and not after `recipes.REWRITES_MAX` failed rewrites (then the owner is told). Returns what was done."""
    if await recipe_record.attention(session, row.owner, site.id) >= recipes.REWRITES_MAX:
        row.state = "attention"
        return "needs attention"
    last = await session.scalar(select(func.max(SiteRecording.requested_at)).where(
        SiteRecording.site_id == site.id, SiteRecording.reason == "repair"))
    if last is not None and now - last < timedelta(days=recipes.REWRITE_EVERY_DAYS):
        return None
    await recipe_record.request(session, row.owner, site, "repair", now)
    return "re-record asked"


async def settle(session: AsyncSession, row: SiteRecipe, site: Site, night: dict, now: datetime) -> str | None:
    """Put a night in the recipe's health and act on it. `night` is {rows, stats, error, blocked, details, requests}.
    Returns a word for the night: `ok`, `blocked`, `bad`, or what a repair did (the recipe is then no longer live)."""
    history = list(row.health or [])
    entry = {"at": now.isoformat(), "rows": night["rows"], "requests": night.get("requests", 0),
             "details": night.get("details", 0), "fill": night["stats"]}
    if night["blocked"]:
        entry["why"] = "blocked"
        word: str | None = "blocked"
    elif why := recipes.judge_night(history, night["rows"], night["stats"], night["error"]):
        entry["why"], word = why, "bad"
        row.bad_nights = (row.bad_nights or 0) + 1
    else:
        row.bad_nights, word = 0, "ok"
    row.health = [*history, entry][-recipes.HISTORY:]
    if word == "bad" and row.bad_nights >= recipes.STALE_AFTER and row.state == "live":
        row.state = "stale"
        word = await _ask_repair(session, row, site, now) or "stale"
    return word


async def posting_recipe_for(session: AsyncSession, owner: str, link: str | None) -> SiteRecipe | None:
    """The live posting recipe of the host this posting address is on (the narrowest host that covers it), or None."""
    host = br.host_of(link)
    if not host:
        return None
    rows = [r for r in (await session.scalars(select(SiteRecipe).where(
        SiteRecipe.owner == owner, SiteRecipe.site_id.is_(None), SiteRecipe.host.is_not(None),
        SiteRecipe.retired_at.is_(None), SiteRecipe.state == "live"))).all()
        if r.host and skills.host_covers(r.host, host)]
    return min(rows, key=lambda r: (r.host or "").startswith("*."), default=None)


async def page_recipe_for(session: AsyncSession, owner: str, link: str | None) -> SiteRecipe | None:
    """The live page or HTML recipe of the site this posting address is on (the same site: the last two labels of the
    host match), or None. Its text element reads the posting's page by code (`page_recipe.read_posting` in the app's
    Chrome, `html_recipe.read_text` by plain fetch)."""
    host = br.host_of(link)
    if not host:
        return None

    def site_of(h: str) -> str:
        return ".".join(h.split(".")[-2:])

    rows = [r for r in (await session.scalars(select(SiteRecipe).where(
        SiteRecipe.owner == owner, SiteRecipe.site_id.is_not(None), SiteRecipe.retired_at.is_(None),
        SiteRecipe.state == "live").order_by(SiteRecipe.created_at.desc()))).all()
        if (r.recipe or {}).get("kind") in ("page", "html") and (r.recipe or {}).get("text")
        and site_of(br.host_of(r.recipe.get("url")) or "") == site_of(host)]
    return rows[0] if rows else None


async def _ask_repair_host(session: AsyncSession, row: SiteRecipe, now: datetime) -> str | None:
    """`_ask_repair` for a host's posting recipe."""
    assert row.host
    if await recipe_record.attention_host(session, row.owner, row.host) >= recipes.REWRITES_MAX:
        row.state = "attention"
        return "needs attention"
    last = await session.scalar(select(func.max(SiteRecording.requested_at)).where(
        SiteRecording.host == row.host, SiteRecording.reason == "repair"))
    if last is not None and now - last < timedelta(days=recipes.REWRITE_EVERY_DAYS):
        return None
    return "re-record asked" if await recipe_record.request_host(session, row.owner, row.host, "repair", now) else None


def _close_days(row: SiteRecipe, today: str) -> None:
    """Judge the days of a posting recipe's reads that are over: a day that failed some and read none is bad; two bad
    days in a row make the recipe stale. A day is judged once (`judged`)."""
    health = [dict(h) for h in (row.health or [])]
    for entry in health:
        if entry.get("day", today) < today and not entry.get("judged"):
            entry["judged"] = True
            bad = bool(entry.get("failed")) and not entry.get("rows")
            row.bad_nights = (row.bad_nights or 0) + 1 if bad else 0
            if bad:
                entry["why"] = "it read none of the postings it was asked for"
    row.health = health[-recipes.HISTORY:]
    if (row.bad_nights or 0) >= recipes.STALE_AFTER and row.state == "live":
        row.state = "stale"


async def note_posting_read(session: AsyncSession, row_id, ok: bool, now: datetime | None = None) -> None:
    """A read of one posting by a host's posting recipe went well (`ok`) or did not: counted in the day's entry of its
    health. A day over is judged first (`_close_days`); a recipe that went stale asks for a re-record."""
    now = now or datetime.now(UTC)
    row = await session.get(SiteRecipe, row_id)
    if row is None:
        return
    today = owner_today(now).isoformat()
    _close_days(row, today)
    health = [dict(h) for h in (row.health or [])]
    if not health or health[-1].get("day") != today:
        health.append({"day": today, "at": now.isoformat(), "rows": 0, "failed": 0})
    health[-1]["rows" if ok else "failed"] = int(health[-1].get("rows" if ok else "failed", 0)) + 1
    row.health = health[-recipes.HISTORY:]
    if row.state == "stale":
        await _ask_repair_host(session, row, now)
    await session.flush()


async def maintain(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """The stale recipes, each night: a week after the last, a new re-record is asked for (the site is read by the
    browser meanwhile). A host's posting recipe has the days of its reads judged first. Returns how many were asked."""
    now = now or datetime.now(UTC)
    asked = 0
    for row in (await session.scalars(select(SiteRecipe).where(
            SiteRecipe.owner == owner, SiteRecipe.site_id.is_(None), SiteRecipe.host.is_not(None),
            SiteRecipe.retired_at.is_(None), SiteRecipe.state.in_(("live", "stale")))
            .order_by(SiteRecipe.created_at))).all():
        _close_days(row, owner_today(now).isoformat())
        if row.state == "stale" and await _ask_repair_host(session, row, now) == "re-record asked":
            asked += 1
    for row in (await session.scalars(select(SiteRecipe).where(
            SiteRecipe.owner == owner, SiteRecipe.site_id.is_not(None), SiteRecipe.retired_at.is_(None),
            SiteRecipe.state == "stale").order_by(SiteRecipe.created_at))).all():
        site = await session.get(Site, row.site_id)
        if site is not None and site.on and await _ask_repair(session, row, site, now) == "re-record asked":
            asked += 1
    await session.flush()
    return asked
