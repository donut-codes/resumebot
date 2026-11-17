"""Sites to avoid (docs/PRD.md 30.3, 30.4): the owner's second list on Where to look, matched by host.

A site to avoid is never read, and a posting whose link goes to one is stopped by the filter ("a site you avoid"). An
entry stands for its host, lowercase and without `www.`; a subdomain of an avoided host counts. A site is never on both
lists, so the same matching decides whether an address may be added to one list while it is on the other (`related`).

Pure functions and one query, with nothing from the job-search side, so the filter (`jobsearch/gating/sift.py`) and
Where to
look (`sites/where.py`) can both use it.
"""

from collections.abc import Iterable
from functools import lru_cache
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.sites.models import AVOID, Site

#: The reason the filter gives (and the owner reads) for a posting on an avoided site.
REASON = "a site you avoid"


def host_of(url: object) -> str | None:
    """The address's host: lowercase, `www.` and any trailing dot dropped. None when it has none."""
    if not isinstance(url, str) or not url.strip():
        return None
    return _host_of_text(url.strip())


@lru_cache(maxsize=32768)
def _host_of_text(text: str) -> str | None:
    """`host_of` of a stripped address. It depends on the text alone, so it is remembered: a board reads the same
    thousands of addresses on every request."""
    try:
        host = urlsplit(text if "//" in text else f"//{text}").hostname
    except ValueError:
        return None
    host = (host or "").rstrip(".").removeprefix("www.")
    return host or None


def covers(avoided: str, host: str) -> bool:
    """Whether `host` is `avoided` or one of its subdomains."""
    return host == avoided or host.endswith("." + avoided)


def related(a: str, b: str) -> bool:
    """Whether one host is the other or a subdomain of it (either way round)."""
    return covers(a, b) or covers(b, a)


def matching(urls: Iterable[object], avoided: Iterable[str]) -> str | None:
    """The avoided host any of the addresses goes to (or is a subdomain of), else None: which site stopped it."""
    hosts = [h for h in (host_of(u) for u in urls) if h]
    return next((a for a in sorted(avoided) for h in hosts if covers(a, h)), None)


def on_list(urls: Iterable[object], avoided: Iterable[str]) -> bool:
    """Whether any of the addresses goes to an avoided host (or a subdomain of one)."""
    return matching(urls, avoided) is not None


async def hosts(session: AsyncSession, owner: str) -> frozenset[str]:
    """The hosts the owner avoids."""
    rows = (await session.scalars(select(Site.url).where(Site.owner == owner, Site.list_name == AVOID))).all()
    return frozenset(h for h in (host_of(u) for u in rows) if h)
