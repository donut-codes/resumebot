"""What an address says of a job system (docs/PRD.md 28.1), read once, and the base every job system's address rules sit
on. A system the app reads (`Adapter`, one module each) or has no reader for (`unread.py`) is a `System`: its domains,
the parts of a link that address one employer's board, how a link to one of its postings looks. `ats.parse` asks each in
turn. Pure and standard library only: nothing here fetches anything, and nothing here imports `ats`."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

#: Hosts under a job system's own domain that are the vendor's (its marketing site, help centre and docs), not an
#: employer's board: "help.example.test" is not a board named "help".
VENDOR_HOSTS = frozenset({
    "www", "help", "support", "docs", "documentation", "developer", "developers", "api", "app", "blog", "status",
    "community", "marketplace", "partners", "learn", "resources", "go", "info", "careers", "jobs", "hello"})

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: A path part that is a language (`en-US`), which a page opened in one carries before the rest of its path.
LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def id_like(segment: str) -> bool:
    """Whether a path segment carries a posting's id: all digits, a number of three or more digits in it, a GUID, or a
    requisition code (`R0012`, `JR-101`)."""
    return bool(segment.isdigit() or re.search(r"\d{3,}", segment) or GUID.match(segment)
                or re.fullmatch(r"[A-Za-z]{1,4}[-_]?\d{2,}", segment))


def is_customer_host(host: str) -> bool:
    """Whether the first name of a host under a job system's own domain is a customer's (`careers-acme.example.test`)
    and not the vendor's (`www.example.test`, `help.example.test`: `VENDOR_HOSTS`)."""
    return host.split(".")[0] not in VENDOR_HOSTS


@dataclass(frozen=True)
class Address:
    """One link, parsed once: what every system's rules look at. `Address.of(link)` is None for no address (a link that
    is not text, has no host or does not parse)."""

    #: The link as given, less the spaces around it (a posting's address is kept so).
    given: str
    #: The host as `urlparse` reads it, lower case. A system's own board rules read this one.
    host: str
    #: The same, less any dots at its end: the host `System.domains` are held against.
    domain_host: str
    #: The parts of the path, with the case they were written in, and again in lower case.
    parts: list[str]
    lower: list[str]
    #: The query: its keys in lower case with the first value of each (a later key of another case wins), and as
    #: `parse_qs` gives it, for a parameter that is read by its exact key (a board's `for`, a portal's `clientkey`).
    query: dict[str, str]
    qs: dict[str, list[str]]

    @classmethod
    def of(cls, url: object) -> "Address | None":
        if not url or not isinstance(url, str):
            return None
        given = url.strip()
        try:
            parsed = urlparse(given if "://" in given else "https://" + given)
            host = (parsed.hostname or "").lower()
            qs = parse_qs(parsed.query)
        except ValueError:
            return None
        if not host:
            return None
        parts = [p for p in parsed.path.split("/") if p]
        return cls(given, host, host.rstrip("."), parts, [p.lower() for p in parts],
                   {k.lower(): v[0] for k, v in qs.items() if v}, qs)

    def after(self, *words: str) -> str | None:
        """The path part after the first one that is one of `words` (compared in lower case; not the last part, which
        has none after it)."""
        return next((self.parts[i + 1] for i, p in enumerate(self.lower[:-1]) if p in words), None)

    def under(self, domains: tuple[str, ...]) -> bool:
        """Whether the host is one of `domains` or a subdomain of one."""
        return any(self.domain_host == d or self.domain_host.endswith("." + d) for d in domains)


class System:
    """What the app knows of one job system from an address. The defaults say nothing, so a system names only what it
    knows. A reader of the system is an `Adapter` (which adds how the board is read, and which one posting an address
    opens when it reads one alone); a system with no reader is a plain `System` (`unread.py`). `ats.SYSTEMS` holds them
    all, in the order they are tried."""

    #: The id a board stores (`Employer.ats`) and the name people know.
    key: str = ""
    name: str = ""
    #: How the app can read it: `feed` (a public JSON feed answers with every open role), `pages` (plain pages the app
    #: can read without a browser) or `hard` (no reader: the fallback ladder of docs/PRD.md 28.6).
    how: str = "hard"
    #: Where the system serves its employers' pages: a domain, or a host, and every subdomain of it. An employer's
    #: careers page may hand its openings off to the employer's own listing on one of these, and the owner's browser
    #: reads it there (docs/PRD.md 37.12). Only whose host it is: unlike `board` it names no customer, and it does not
    #: take a look-alike ("portal.example.test.evil.test") for the real one.
    domains: tuple[str, ...] = ()
    #: The parts of a board's address that make it whole enough to read; one missing can't be read yet.
    needs: tuple[str, ...] = ("slug",)
    #: What names one customer on a system whose host every customer shares, when `needs` does not say (a system with no
    #: reader): any one of them makes the address one customer's own. None: `needs` says.
    own_parts: tuple[str, ...] | None = None
    #: True for a system that gives each customer a host of its own (`careers-acme.example.test`): the host alone is a
    #: customer's board and names it. Every other system's host is shared by all its customers.
    customer_host: bool = False
    #: True for a system whose pages draw their postings in a browser: a page of its own says nothing, only its reader
    #: can (the owner's browser reads the rest).
    drawn: bool = False
    #: Why a system the app has no reader for can't be read, for the three that something is known for sure about
    #: (docs/PRD.md 28.6), as the owner reads it: "<System> can't be read: <why>".
    why_not: str | None = None

    def board(self, a: Address) -> dict | None:
        """The parts of the board a link on this system names: `{}` when it is on the system and names no customer (its
        assets, its sign-in), None when it is not on this system's hosts or names no board (its root, its help pages).
        The rules are the system's own and read `Address.host`, the host as `urlparse` gives it."""
        return None

    def shaped(self, a: Address, read: bool) -> bool:
        """Whether a link on this system has the shape of one posting's page (the answer `ats.parse` gives as
        `Link.one_posting`, which decides what may be an employer's or a recruiter's source). `read`: the reader of the
        system reads one posting from it alone (`Adapter.posting`). By default a page is that and nothing else; a system
        overrides this only when the page it calls one posting's own is another address than the one its reader reads
        (a shorter id, an apply page, a page the app has no reader for) and says so."""
        return read

    def apply_page(self, a: Address) -> str | None:
        """The posting's page that follows from this system's apply-form address by a fixed rule (the same host, the
        same tenant and the same id; nothing guessed), or None for an address that is no such form. The default has no
        rule. `address.posting_page` asks each system and keeps a page only when it is one posting."""
        return None

    def board_url(self, board: dict) -> str | None:
        """A link a person can open to see the employer's board; None when the address is not whole enough, or when it
        has only the host every customer shares (that is the vendor's root, never an employer's board)."""
        host = board.get("host")
        return f"https://{host}/" if host and self.customer_host else None
