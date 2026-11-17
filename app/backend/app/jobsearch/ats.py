"""Which job system an employer posts on, read from a link (docs/PRD.md 28.1). `parse` says, of one link, which job
system it is on, which employer's board it names and which one posting it opens; the facts of a stored board
(`complete`, `own`, `board_labels`, `board_url`) say what a board's address holds. Each system's own address rules sit
with its reader (`adapters/`, or `adapters/unread.py` for a system the app has no reader for); this module asks each
in turn. Pure functions, no network.

This module is what about forty others import for a label or a name, so importing it loads nothing of the readers (the
HTTP client, the polite fetcher, every system's code): `SYSTEMS` and `parse` load them on their first use.

How a system is read: `feed` (a public JSON feed answers with every open role), `pages` (plain pages the app can read
without a browser), `hard` (no public route found yet: the fallback ladder of 28.6)."""

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, NamedTuple, overload
from urllib.parse import urlparse

from app.sites import registry

if TYPE_CHECKING:
    from app.jobsearch.adapters import Adapter, System


@cache
def _loaded() -> "tuple[Mapping[str, System], Mapping[str, Adapter]]":
    """Every job system, readers first, in the order `parse` asks them, and the readers alone, as `adapters` made them
    (`SYSTEMS` and `READERS`: never `ADAPTERS`, the name a test rebinds to stand in for a reader, which could be what is
    bound when this is first asked and would then be kept for good). Loaded on first use, once."""
    from app.jobsearch.adapters import READERS, SYSTEMS

    asked = {key: at for at, key in enumerate(registry.order())}  # the runbooks say in what order they are asked
    return dict(sorted(SYSTEMS.items(), key=lambda item: asked.get(item[0], len(asked)))), READERS


class _Systems(Mapping[str, "System"]):
    """Every job system by its key, as `_loaded` has them: a mapping that loads them when it is first looked at."""

    def __getitem__(self, key: str) -> "System":
        return _loaded()[0][key]

    def __iter__(self) -> Iterator[str]:
        return iter(_loaded()[0])

    def __len__(self) -> int:
        return len(_loaded()[0])


#: Every job system, readers first, in the order `parse` asks them. Seen from outside it is a table of names, how each
#: is read and where each lives; the rules are each system's own.
SYSTEMS: Mapping[str, "System"] = _Systems()

class _View(Sequence[str]):
    """A table of names that is read from the registry each time it is looked at, so what it holds is whatever the
    runbooks say (and a test's own registry, `registry.replaced`, is what it sees)."""

    def __init__(self, read: Callable[[], tuple[str, ...]]) -> None:
        self._read = read

    @overload
    def __getitem__(self, index: int) -> str: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[str]: ...
    def __getitem__(self, index):
        return self._read()[index]

    def __len__(self) -> int:
        return len(self._read())

    def __contains__(self, item: object) -> bool:
        return item in self._read()

    def __iter__(self) -> Iterator[str]:
        return iter(self._read())

    def __repr__(self) -> str:
        return repr(self._read())


#: The job systems the app has a reader for whose terms deny it, or that a new install does not offer. The owner,
#: 2026-11-10, asked what a new install does with them: "Off and hidden" (docs/PRD.md path 55). The readers stay. A new
#: install starts with these switched off (`setup_cli.seed_starter_settings`), and one that is off is not listed on
#: Where to look (`job_systems.shown`), so no screen offers it; switching one on is done by hand, through the
#: job-system route, and it is then listed like any other. An install that already exists keeps reading whatever it
#: reads. The runbooks say which (`starts_off`); this is a view of them, in the order job systems are asked.
NOT_OFFERED: Sequence[str] = _View(registry.starts_off)

#: Job boards that list other employers' roles: a link to one says nothing about the employer's own system. A view of
#: the runbooks of kind `board`.
BOARDS: Sequence[str] = _View(registry.board_hosts)


class _OwnCareers(Mapping[str, tuple[str, ...]]):
    """A host on `BOARDS` whose pages are not all a board's: an employer's own careers site lives there, under this path
    (the segments before a posting's id). A link under it that names one posting is the employer's own page; the host's
    search and the careers site's own list of postings are still pages of many. Read from the runbooks'
    `address.own_careers_paths`."""

    def __getitem__(self, host: str) -> tuple[str, ...]:
        return registry.own_careers_paths()[host]

    def __iter__(self) -> Iterator[str]:
        return iter(registry.own_careers_paths())

    def __len__(self) -> int:
        return len(registry.own_careers_paths())


OWN_CAREERS: Mapping[str, tuple[str, ...]] = _OwnCareers()


def board_named(host: str) -> bool:
    """Whether a host is a job board the app knows by name (`BOARDS`) or a subdomain of one. The host is lower-cased and
    its trailing dot dropped; each of its dotted-label endings is looked up in the set. The one primitive for it:
    `parse`, `address.is_board_link` and the address shapes all ask it. Pure."""
    return registry.board_named(host)


def own_careers_posting(url: object) -> bool:
    """Whether an address is ONE posting on an employer's own careers site that lives on a board's host
    (`OWN_CAREERS`): the host, the path the site's postings are under and an id (digits first, then a slug) after it.
    The one rule that says such a link is no job board's (`address.is_board_link`, `address.is_single_posting`).
    Pure: it reads the address and nothing else."""
    if not isinstance(url, str) or not url.strip():
        return False
    text = url.strip()
    try:
        parsed = urlparse(text if "//" in text else f"//{text}")
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    parts = [p for p in parsed.path.split("/") if p]
    return any(
        (host == domain or host.endswith("." + domain)) and (
            len(parts) > len(prefix) and tuple(p.lower() for p in parts[:len(prefix)]) == prefix
            and parts[len(prefix)][:1].isdigit())
        for domain, prefix in OWN_CAREERS.items())


class Board(NamedTuple):
    """A board's address as `Employer.ats` and `Employer.board` store it: the system, and the parts that name one
    customer on it (`{}`: a link on the system that names no customer: its assets or its sign-in)."""

    system: str
    parts: dict


@dataclass(frozen=True)
class Read:
    """One posting a system's own reader reads from a link alone (`Link.read`): the reader, the board it reads it on and
    what it takes to read it, as `{"ext_id", "url", "title": None, "location": None}`. The url is the address the reader
    reads (a system's may leave out the language and the apply tail), else the link as given. The three come together or
    not at all: `reader.detail(fetch, board.parts, posting)` is the read."""

    reader: "Adapter"
    board: Board
    posting: dict


@dataclass(frozen=True)
class Link:
    """What one link says of a job system (`parse`)."""

    #: The job system whose own domain the host is under (`System.domains`), or None: an employer's own site, a job
    #: board, a search engine, a look-alike.
    system: str | None
    #: The board the link names, or None for the vendor's root, its help or its sign-in, a job board, an employer's own
    #: site.
    board: Board | None
    #: The one posting the system's reader reads from the link alone (`Adapter.detail`), or None: a link to a board, a
    #: system with no such reader, any other page of its own.
    read: Read | None
    #: Whether the address has the shape of the page of one posting (`System.shaped`: what the reader reads from it, or
    #: the page a system says is a posting's own), whether or not its reader can read one alone. It is not the question
    #: `read` answers: an apply page is read and is not one.
    one_posting: bool
    #: The reader of the system the host belongs to, or None when the app has none: a link on its host is read by it,
    #: never by a session or the app's browser, though it names no posting.
    reader: "Adapter | None"
    #: Whether the system draws its postings in a browser: a page of its own says nothing (it has no text, and a closed
    #: posting looks open), so only its reader can, and its pages are never read as the page they are.
    drawn: bool


def parse(url: str | None) -> Link | None:
    """What a link says of a job system, or None when it names none: no job system's domain, no board, or a job board.
    Never raises, and reads the address and nothing else."""
    from app.jobsearch.adapters import Address

    a = Address.of(url)
    if a is None or board_named(a.host):
        return None
    systems, readers = _loaded()
    board = next((Board(s.key, found) for s in systems.values() if (found := s.board(a)) is not None), None)
    system = next((s for s in systems.values() if a.under(s.domains)), None)
    if board is None and system is None:
        return None
    of_board = readers.get(board.system) if board else None
    hit = of_board.posting(a, board.parts) if of_board and board else None
    read = None
    if of_board and board and hit:
        read = Read(of_board, board, {"ext_id": hit[0], "url": hit[1], "title": None, "location": None})
    one_posting = bool(board and systems[board.system].shaped(a, read is not None))
    reader = of_board or (readers.get(system.key) if system else None)
    return Link(system.key if system else None, board, read, one_posting, reader, bool(system and system.drawn))


#: What a posting read in the owner's own browser (docs/PRD.md 30.8) holds as its system: not a job system, a way of
#: reading. It is on a job site's listing, so it says nothing about the employer's own system.
BROWSER = "browser"
#: What a posting read through a site's own saved skill (a recipe the scout learned, docs/PRD.md 29.2, read by the night
#: in `site_reads.py`) holds as its system: `own_site` on an employer's own site, `job_site` on a job board or an
#: association's listing of other employers' roles. Neither is a job system, so neither has a reader of its own: their
#: descriptions are not filled in from a system (`postings.fill_details`), and a job site's listing is not the
#: employer's board.
OWN_SITE, JOB_SITE = "own_site", "job_site"
#: What the posting of a role holds as its system when the app did not read it from a board (docs/PRD.md 39.4 and 39.9):
#: a role the scout reported (`SCOUT`), a role the owner's tracker held (`IMPORT`) and a job the owner added by pasting
#: its link (`OWNER`, `add_link.py`). None is a job system or a site, so none has a reader and none is read again: the
#: text such a posting has is what a reader or the analyst brought back (39.7), or the role's own.
SCOUT, IMPORT, OWNER = "scout", "import", "owner"
#: Postings whose list is all the app can read: no job system to ask for the description (or, for a browser posting, no
#: way in: it is behind the owner's sign-in; for the posting of a role, nothing to read at all).
NO_DETAIL = (BROWSER, OWN_SITE, JOB_SITE, SCOUT, IMPORT, OWNER)
#: The systems of postings that are no board's: the log holds them for the roles they belong to, and no board read ever
#: opens or closes them (`not_checked` leaves them alone as well).
OF_ROLES = (SCOUT, IMPORT, OWNER)


def from_source(system: str | None) -> bool:
    """Whether a posting of this system was read from a source's own system: a job system the app knows or an
    employer's own site (its saved skill). Never a job board's, the owner's browser's, the scout's, the tracker's or the
    owner's own report: only a source's own words are a posting's text."""
    return system in SYSTEMS or system == OWN_SITE


def _system(ats: str | None) -> "System | None":
    return SYSTEMS.get(ats) if ats else None


def label(ats: str | None) -> str:
    if ats == BROWSER:
        return "Read in your browser"
    if ats == JOB_SITE:
        return "A job site"
    if ats == SCOUT:
        return "Reported by the scout"
    if ats == IMPORT:
        return "Imported from your tracker"
    if ats == OWNER:
        return "Added by you"
    if system := _system(ats):
        return system.name
    return "Its own site" if ats == OWN_SITE else "Not known yet"


def why_on(system: str | None) -> str | None:
    """Why a company's site on this system is read in the owner's browser, as one plain clause ("it posts on Acme Jobs,
    whose job list is behind a route its robots.txt disallows"), for a system the server has no reader for and knows why
    (`System.why_not`); None for any other."""
    found = _system(system)
    why = found.why_not if found else None
    if not why:
        return None
    if why.startswith("its "):
        return f"it posts on {label(system)}, whose {why[len('its '):]}"
    return f"it posts on {label(system)}: {why}"


def how(ats: str | None) -> str:
    """feed | pages | hard | unknown"""
    found = _system(ats)
    return found.how if found else "unknown"


def board_url(ats: str | None, board: dict | None) -> str | None:
    """A link a person can open to see the employer's board."""
    found = _system(ats)
    return found.board_url(board or {}) if found else None


def complete(ats: str | None, board: dict | None) -> bool:
    """Whether a board's address has what its system's adapter needs."""
    if not ats or not board:
        return False
    found = _system(ats)
    return all(board.get(k) for k in (found.needs if found else ("slug",)))


def own(ats: str | None, board: dict | None) -> bool:
    """Whether an address is one customer's own board: whole enough to say whose it is. A system's root host, or an
    address with only that host, is nobody's (`parse` makes none, and a record that holds one is not holding a board).
    Two employers holding the same own address are reading one board twice."""
    if not ats or not board:
        return False
    found = _system(ats)
    named = found.own_parts if found else None
    if named is not None:
        return any(board.get(k) for k in named)
    return complete(ats, board)


def board_labels(ats: str | None, board: dict | None) -> list[str]:
    """The names an address carries of whose board it is, each as the address writes it (`Acme_and_Co`): its slug,
    tenant, site or company, or the first name of its host on a system that gives each customer one
    (`careers-acme.example.test` is `acme`)."""
    board = board or {}
    parts = [str(board[k]) for k in ("slug", "tenant", "site", "company") if board.get(k)]
    found = _system(ats)
    if found and found.customer_host and board.get("host"):
        parts.append(re.sub(r"^(careers|jobs)-", "", str(board["host"]).split(".")[0]))
    return parts


def _words(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_.\s]+", slug) if w)


def employer_from_link(name: str, url: str | None) -> str:
    """`name`, unless it is only the host of the posting's own link (`acme.wd5.example.test`, which names a
    server, not an employer): then the employer the link carries, in words: a job system's tenant or board slug
    (`acme`), else the name in the host (`careers.acme-health.com` is `Acme Health`). A name that is not that host,
    such as one a posting gives of itself, is kept as it is."""
    try:
        host = (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return name
    if not host or name.strip().lower().removeprefix("www.") != host:
        return name
    found = parse(url)
    if found is not None and found.board is not None:
        slug = found.board.parts.get("tenant") or found.board.parts.get("slug") or found.board.parts.get("company")
        if slug and _words(str(slug)):
            return _words(str(slug))
    labels = host.split(".")[:-1]
    if len(labels) > 1 and labels[-1] in ("co", "com", "org", "gov", "ac"):
        labels = labels[:-1]
    return _words(labels[-1]) if labels and _words(labels[-1]) else name
