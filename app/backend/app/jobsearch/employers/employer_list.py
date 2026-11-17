"""The owner's employer list as a lookup (docs/PRD.md 28.3).

The owner may have a file of employers and their websites. `import-employer-list` (`jobsearch/cli.py`) keeps its rows in
`js_employer_list`, under the file's name, and the scout's hand-over (`doors/tools.boards_reply`) carries the list's
websites for an employer whose job board the app does not know, as a first clue the scout must check: the list is the
owner's word, and some of its addresses are dead, parked or another company's.

That is all it is. A row is never an employer (no record is made from one, and none is changed), nothing is researched
from the list, and no model is asked about it. The file is the owner's: this module knows no path, no name and no
column but the headings it reads. Services flush; the command commits.
"""

import csv
import io
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch.employers.core import PLACEHOLDER_KEY, key_of
from app.jobsearch.fetch.polite import public_url
from app.jobsearch.models import Employer, EmployerListEntry
from app.web_address import http_url

#: The headings each column of the file may carry, in the order they are looked for (case and spacing do not matter).
NAME_HEADINGS = ("name", "employer", "company")
WEBSITE_HEADINGS = ("website", "domain", "url", "site")
VERDICT_HEADING = "verdict"
#: The room `EmployerListEntry.verdict` has.
VERDICT_ROOM = 40
#: What the scout's hand-over carries for one employer: at most this many websites, none of them longer than this many
#: characters. The list is the owner's and keeps every row; this bounds only what is sent (`websites_for`), as
#: `doors/tools.BOARDS_ASKED` bounds the employers.
WEBSITES_PER_EMPLOYER = 5
LONGEST_WEBSITE = 300


class ListError(DomainError):
    """The file can't be read as an employer list; plain words."""

    status = 422


@dataclass(frozen=True)
class Entry:
    """A row worth keeping: the employer's name, its matching key, a public website and the file's verdict, if any."""

    key: str
    name: str
    website: str
    verdict: str | None


@dataclass
class Read:
    """What a file came to: the rows to keep and how many of the others were left out, and why."""

    rows: int = 0
    entries: list[Entry] = field(default_factory=list)
    #: The verdict column says to drop it (a verdict that begins "drop" or "probably drop").
    dropped: int = 0
    #: A name that makes no key, or no website that is a public web address.
    unusable: int = 0
    #: The same employer key and website as a row kept before it.
    repeats: int = 0


def _column(headings: dict[str, str], wanted: Iterable[str]) -> str | None:
    return next((headings[h] for h in wanted if h in headings), None)


def _says_drop(verdict: str) -> bool:
    return verdict.lower().startswith(("drop", "probably drop"))


def read(text: str) -> Read:
    """The rows of a file's text (comma separated, a heading row first) that the lookup keeps. A file with no name
    column or no website column, or that the CSV reader cannot read, is refused in words, so a wrong file never loads
    half-understood."""
    try:
        return _read(text)
    except csv.Error as exc:
        raise ListError(f"the file could not be read as a CSV ({exc})") from exc


def _read(text: str) -> Read:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise ListError("the file is empty: it needs a heading row and a row for each employer")
    headings = {h.strip().lower(): h for h in reader.fieldnames if h}
    name_at, site_at = _column(headings, NAME_HEADINGS), _column(headings, WEBSITE_HEADINGS)
    verdict_at = headings.get(VERDICT_HEADING)
    if name_at is None or site_at is None:
        raise ListError("the file needs a name column (headed " + ", ".join(NAME_HEADINGS[:-1]) + " or "
                        + NAME_HEADINGS[-1] + ") and a website column (headed " + ", ".join(WEBSITE_HEADINGS[:-1])
                        + " or " + WEBSITE_HEADINGS[-1] + ")")
    out, seen = Read(), set()
    for row in reader:
        out.rows += 1
        name = " ".join((row.get(name_at) or "").split())
        verdict = (row.get(verdict_at) or "").strip() if verdict_at else ""
        if _says_drop(verdict):
            out.dropped += 1
            continue
        website = http_url(row.get(site_at))
        key = key_of(name)
        if not key or key == PLACEHOLDER_KEY or website is None or not public_url(website):
            out.unusable += 1
            continue
        if (key, website.lower()) in seen:
            out.repeats += 1
            continue
        seen.add((key, website.lower()))
        out.entries.append(Entry(key, name, website, verdict[:VERDICT_ROOM] or None))
    return out


async def replace(session: AsyncSession, owner: str, source: str, entries: Sequence[Entry]) -> int:
    """Keep these rows as what the file called `source` says, in place of the rows an earlier load of a file of that
    name gave (another file's rows stay). Returns how many rows are kept. Written through the session, a row at a time:
    the old rows go first and are flushed, so a row that is in both loads is not kept twice."""
    for old in (await session.scalars(select(EmployerListEntry).where(
            EmployerListEntry.owner == owner, EmployerListEntry.source == source))).all():
        await session.delete(old)
    await session.flush()
    session.add_all(EmployerListEntry(owner=owner, source=source, key=e.key, name=e.name, website=e.website,
                                      verdict=e.verdict) for e in entries)
    await session.flush()
    return len(entries)


def lookup_keys(employer: Employer) -> list[str]:
    """The keys the list is searched by for an employer: its own, then the key of each alias."""
    keys = [employer.key, *(key_of(alias) for alias in employer.aliases or [] if isinstance(alias, str))]
    return list(dict.fromkeys(k for k in keys if k and k != PLACEHOLDER_KEY))


def _host(address: str | None) -> str | None:
    link = http_url(address)
    host = urlsplit(link).hostname if link else None
    return host[4:] if host and host.startswith("www.") else host


async def websites_for(session: AsyncSession, owner: str, employers: Sequence[Employer]) -> dict[uuid.UUID, list[str]]:
    """The list's websites for each of these employers that has any (an exact key, or an alias's key), in a steady
    order, the first `WEBSITES_PER_EMPLOYER` of them. A website on the host of the one the app holds for the employer is
    left out: it is no clue; so is one over `LONGEST_WEBSITE` characters, which leaves its place to a shorter one."""
    keys = {e.id: lookup_keys(e) for e in employers}
    wanted = {k for ks in keys.values() for k in ks}
    if not wanted:
        return {}
    by_key: dict[str, list[str]] = {}
    for key, website in (await session.execute(
            select(EmployerListEntry.key, EmployerListEntry.website).where(
                EmployerListEntry.owner == owner, EmployerListEntry.key.in_(wanted),
                func.length(EmployerListEntry.website) <= LONGEST_WEBSITE)
            .distinct().order_by(EmployerListEntry.website))).all():
        by_key.setdefault(key, []).append(website)
    found: dict[uuid.UUID, list[str]] = {}
    for employer in employers:
        own = _host(employer.website)
        sites = [w for w in dict.fromkeys(w for k in keys[employer.id] for w in by_key.get(k, []))
                 if own is None or _host(w) != own]
        if sites:
            found[employer.id] = sites[:WEBSITES_PER_EMPLOYER]
    return found


@dataclass(frozen=True)
class Matches:
    """How a read file meets the app's employers (what a dry run says)."""

    #: The app's employers the file names, by key or by an alias's key; and all of the app's employers.
    employers: int
    of: int
    #: The rows kept that name one of the app's employers.
    rows: int
    #: Of the employers the file names, how many are waiting for the scout to find their job board.
    waiting: int


def matches(entries: Sequence[Entry], employers: Sequence[Employer], waiting: set[str]) -> Matches:
    """Count, with nothing read from the database but the employers given: `waiting` is the ids (as text) of the
    employers the scout has yet to be handed (`doors/tools.boards_waiting`)."""
    named = {e.key for e in entries}
    meeting = [e for e in employers if named & set(lookup_keys(e))]
    keys = {k for e in meeting for k in lookup_keys(e)}
    return Matches(employers=len(meeting), of=len(employers), rows=sum(1 for e in entries if e.key in keys),
                   waiting=sum(1 for e in meeting if str(e.id) in waiting))
