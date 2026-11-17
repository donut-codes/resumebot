"""Reading other sites the way docs/PRD.md 28.5 says: robots.txt honoured (its wildcards and its Crawl-delay), about one
request a second to each host, an honest user agent, a wait when a site says "too many requests", and a refusal (403)
taken as a stop for that host for the rest of the run, never retried from somewhere else. Never a way around a block.

One exception to robots.txt, the owner's (2026-10-26, docs/PRD.md path 48): a fetcher told `read_exempt` does not ask
a host's robots.txt about one path prefix. Only the reader of a runbook that carries `robots` does (restricted folder
only), for that board's public signed-out job feed, at the pace the runbook gives it (`registry.pace_for_host`, never
under 30 seconds: a permission, not a restriction). `read_exempt` is the one door to it, and it holds four guards.

This is the one place the app's own code fetches a URL, and it is guarded:

- only a public address is ever read: the address is checked as written (`public_url`), its host name is resolved and
  refused unless every address it gives is global, and the connection goes to an address that was checked, so a name
  that answers differently the second time cannot reach a private one;
- a redirect is never followed by the client: each hop (at most `MAX_HOPS`) is checked, paced and read against
  robots.txt like a first request, and a redirect from https to http is refused. A robots.txt's own redirects are
  followed as RFC 9309 says (2.3.1.2): up to `MAX_HOPS`, across hosts and from https to http (a site that sends its
  robots.txt to plain http is read, not taken for unreachable); the rules found are the rules of the host asked
  about, and a chain longer than that leaves the host alone, as a robots.txt that cannot be had does;
- an answer is read to at most `MAX_BYTES` once decoded, and a request takes at most `REQUEST_SECONDS` in all;
- no request is sent from inside an open database transaction (`db.outside_transaction`: logged once for each place
  that does it, raised in every test). Code reads, ends its transaction (`db.release`), fetches, then writes in a new
  one; a process step that cannot end its transaction takes what was read before it began (`read_ahead`).

How many at once and how fast (docs/PRD.md 28.17), whoever asks and however many boards, pages or postings are read
side by side: a host is asked by one request at a time, a pace apart, and a platform (the domain its hosts share:
`platform_of`) by at most `PLATFORM_AT_ONCE` requests begun in any one pace and open at once. A host is the host name of
the address, so the tenants of a job system that gives each its own (`acme.wd5.jobs.example.test`,
`beta.wd5.jobs.example.test`) are hosts of their own, each paced on its own and each with its own robots.txt and its own
stop, and the platform is what keeps the job system as a whole from being asked faster than the read asked it before it
went wide: four boards at once, up to four of its tenants, each at a request a pace, so four a pace and no more. Both
limits are this `Fetcher`'s own: each step and each lane makes its own, and none is shared between them.

One `Fetcher` per run; tests hand it an httpx client with a mock transport.

A read that goes on without a page cap (docs/PRD.md 25.12) is bounded by time instead. A `Fetcher` may carry a
`deadline`, and a loop over boards, pages or postings asks `timeup(fetch)` between its requests and stops cleanly there,
keeping what it has read: the night's read gives every fetcher it makes the same one (`READ_BY`), and a scout's quick
try of a recipe gives its own."""

import asyncio
import collections
import contextlib
import contextvars
import functools
import ipaddress
import math
import re
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from app import db
from app.jobsearch.fetch import host_stops
from app.settings import APP_NAME, VERSION
from app.sites import registry
from app.sites.schema import ROBOTS_MIN_PACE_SECONDS, Runbook

USER_AGENT = f"{APP_NAME}/{VERSION} (a personal job-search assistant; reads public job listings only)"
#: The name robots.txt files know this app by (the user agent's product token).
PRODUCT_TOKEN = APP_NAME.lower()
#: The longest a "too many requests" wait is honoured before the request is given up.
MAX_RETRY_AFTER = 60.0
#: The statuses of an answer that says "not now" (timeout, too early, too many requests), not "no": a try tomorrow.
NOT_NOW = (408, 425, 429)


def retry_after(value: str | None, default: float, now: datetime | None = None) -> float:
    """The seconds a `Retry-After` header asks for: a number of seconds, or an HTTP date (RFC 9110: the moment to try
    again, counted from `now`). A missing or unreadable value is `default`; a date already past is 0."""
    text = (value or "").strip()
    if not text:
        return default
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return default
        seconds = (when.replace(tzinfo=UTC) if when.tzinfo is None else when).timestamp() - (
            now or datetime.now(UTC)).timestamp()
    return max(seconds, 0.0) if math.isfinite(seconds) else default
#: The longest a site's Crawl-delay is honoured; a site asking for more is left alone.
MAX_CRAWL_DELAY = 30.0
#: Redirects followed for one request.
MAX_HOPS = 5
#: The most one answer may hold once decoded, and the most a robots.txt may. A job feed is not small: a list that
#: holds every description in one answer (a `?content=true` parameter) is several MB. 25 MB leaves room.
MAX_BYTES = 25_000_000
MAX_ROBOTS_BYTES = 500_000
#: The most seconds one request may take in all, however slowly the site sends it.
REQUEST_SECONDS = 60.0
#: The most requests the hosts of one platform are asked in one pace (the seconds between two requests to a host,
#: `Fetcher.per_host_seconds` or its slower own), and the most open at once. Four is what the read could send a job
#: system before it read wide: it read four boards at once, so up to four tenants of one system, each at a request a
#: pace. The limit is on how fast as well as how many are open (`Fetcher._begin`): a place freed by a quick answer is
#: not taken again until a pace has passed since the fourth-last request of the platform began. It is kept whatever the
#: read's width (`postings.index.READ_AT_ONCE`), so no job system is asked faster than it was.
PLATFORM_AT_ONCE = 4


#: The seconds a host is held to that a test sets so no wait is made (`HOST_SECONDS[host] = 0.0`). The app sets none: a
#: host's pace is its runbook's, which `pace_of` asks the registry.
HOST_SECONDS: dict[str, float] = {}


def pace_of(host: str, default: float = registry.DEFAULT_PACE_SECONDS) -> float:
    """The seconds between two requests to this host: what a test set (`HOST_SECONDS`), else its runbook's pace, else
    `default`. A host whose runbook did not load raises `registry.RunbookUnavailable`, never answers with a default
    (design 2.4a)."""
    if host in HOST_SECONDS:
        return HOST_SECONDS[host]
    return registry.pace_for_host(host, default)

#: When the night's read must stop reading, in `time.monotonic()` seconds: `boards_night.read_boards` sets it for the
#: task that runs the read (docs/PRD.md 28.17), and each `Fetcher` made there takes it as its `deadline`. None: no
#: time limit.
READ_BY: contextvars.ContextVar[float | None] = contextvars.ContextVar("read_by", default=None)
#: Where the answers a read gets are kept for the raw copy (`raw_copies.py`): set
#: by `tapping` for the task that reads, so reads running side by side never mix their answers.
_TAP: contextvars.ContextVar[list | None] = contextvars.ContextVar("fetch_tap", default=None)


class Refused(Exception):
    """The site said no: robots.txt disallows the path, or the host refused us earlier in this run."""


class ExemptRefused(Exception):
    """`Fetcher.read_exempt` was asked for a runbook, a fetcher or a setting that fails one of its four guards: the
    robots.txt of no host is left unasked."""


class RobotsRefused(Refused):
    """robots.txt asks not to read the path. `unreachable`: the rules could not be read at all (the site was failing or
    did not answer), so the host is left alone for now; that is not the site saying no."""

    def __init__(self, message: str, unreachable: bool = False) -> None:
        super().__init__(message)
        self.unreachable = unreachable


class NotPublic(Refused):
    """The address is not one on the public internet (a private, local or reserved address, or a name that points at
    one), so it is never read."""


class TooLarge(httpx.TransportError):
    """The answer was bigger than the fetcher reads once decoded."""


# --- addresses -------------------------------------------------------------------------------------------------------

_PRIVATE_NAMES = ("localhost", ".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")


def public_address(text: str) -> bool:
    """Whether `text` is an IP address that is global: not private, loopback, link-local, shared (100.64/10) or
    reserved, and not an IPv6 address that carries one of those inside (mapped, 6to4, Teredo, NAT64)."""
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inside = [ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())]
        if ip in _NAT64 or ip in _V4_COMPATIBLE:
            inside.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(v4 is not None and not v4.is_global for v4 in inside):
            return False
    return ip.is_global and not ip.is_multicast


def public_host(host: str) -> bool:
    """Whether a host written in an address is one to read: a global IP address, or a name with a dot, of plain letters,
    digits and hyphens, whose last part begins with a letter (so 127.1, 0x7f.1 and 2130706433, which a resolver reads
    as numbers, are not names), and that does not end in a private suffix (.local, .internal ...)."""
    host = host.lower()
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        return public_address(host)
    labels = host.split(".")
    return (len(labels) > 1 and all(re.fullmatch(r"[\w-]+", label) for label in labels) and labels[-1][0].isalpha()
            and not any(host == n.lstrip(".") or host.endswith(n) for n in _PRIVATE_NAMES))


def public_url(url: object) -> bool:
    """Whether `url` is a plain http(s) address on the public internet: no sign-in in the address, no local, private
    or reserved host. A skill, or a careers page, is only ever allowed to read these. (A name that points at a private
    address is caught when it is connected to: `GuardedBackend`.) An address has no limit on its length: a site the
    owner adds under Where to look is read at whatever address they gave."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return False
    return public_host(host)


#: The second-level names under a country's two-letter domain that are not a name of their own (`example.co.uk`).
_GENERIC_SECOND_LEVELS = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go"})


def platform_of(host: str) -> str:
    """The platform a host belongs to: the domain it shares with the other hosts of its job system or site, which is
    its last two names (three under a country's `co.uk`, `com.au`). `acme.wd5.jobs.example.test` and
    `beta.wd1.jobs.example.test` are two hosts of `jobs.example.test`; an address given as numbers is its own."""
    host = (host or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    keep = 3 if len(labels[-1]) == 2 and labels[-2] in _GENERIC_SECOND_LEVELS else 2
    return ".".join(labels[-keep:])


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve(host: str, port: int) -> list[str]:
    """Every address a host name gives, as the system resolver answers."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class GuardedBackend(httpcore.AnyIOBackend):
    """Opens connections only to public addresses: the host name is resolved here, refused unless every address it
    gives is global, and the connection is made to one of the addresses that was checked (never resolved again), so a
    name cannot answer a public address to the check and a private one to the connection."""

    def __init__(self, resolver: Resolver | None = None) -> None:
        super().__init__()
        self._resolve = resolver or resolve

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await self._resolve(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"{host} could not be looked up") from exc
        if not addresses or not all(public_address(a) for a in addresses):
            raise NotPublic(f"{host} is not on the public internet")
        failure: Exception | None = None
        for address in addresses:
            try:
                return await super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure  # type: ignore[misc]  # addresses is not empty


@functools.cache
def _ssl_context():
    """The TLS context, built once per process: reading the CA bundle costs tens of milliseconds of loop time."""
    return httpx.create_ssl_context()


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport, with its connections opened by `GuardedBackend`."""

    def __init__(self, resolver: Resolver | None = None) -> None:
        super().__init__(verify=_ssl_context())
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=_ssl_context(), max_connections=100, max_keepalive_connections=20,
            keepalive_expiry=5.0, network_backend=GuardedBackend(resolver))


# --- robots.txt ------------------------------------------------------------------------------------------------------


def _matches(pattern: str, text: str) -> bool:
    """Whether a robots.txt path pattern (`*` for any run of characters, a final `$` for the end) matches the start of
    `text`."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    first, *rest = pattern.split("*")
    if not text.startswith(first):
        return False
    pos = len(first)
    if not rest:
        return not anchored or len(text) == pos
    for part in rest[:-1]:
        found = text.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    last = rest[-1]
    if anchored:
        return len(text) - len(last) >= pos and text.endswith(last)
    return text.find(last, pos) >= 0


class Robots:
    """One host's robots.txt, read the way RFC 9309 says: the group naming this app wins over the `*` group, the longest
    matching rule wins (Allow on a tie), `*` and `$` work in a path, and a Crawl-delay is kept. `disallow_all`: the file
    could not be had, so nothing is read."""

    def __init__(self, text: str = "", disallow_all: bool = False) -> None:
        self.disallow_all = disallow_all
        self.rules: list[tuple[bool, str]] = []
        self.delay: float | None = None
        groups: list[dict] = []
        current: dict | None = None
        naming = False
        for raw in text.splitlines():
            field, colon, value = raw.split("#", 1)[0].strip().partition(":")
            if not colon:
                continue
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if current is None or not naming:
                    current = {"agents": [], "rules": [], "delay": None}
                    groups.append(current)
                naming = True
                current["agents"].append(value.split("/")[0].strip().lower())
            elif field in ("allow", "disallow", "crawl-delay") and current is not None:
                naming = False
                if field == "crawl-delay":
                    try:
                        delay = float(value)
                    except ValueError:
                        continue
                    if math.isfinite(delay) and delay >= 0 and current["delay"] is None:
                        current["delay"] = delay
                elif value:  # an empty Disallow allows everything
                    current["rules"].append((field == "allow", value))
        use = [g for g in groups if PRODUCT_TOKEN in g["agents"]] or [g for g in groups if "*" in g["agents"]]
        for group in use:
            self.rules += group["rules"]
            if self.delay is None:
                self.delay = group["delay"]

    def allows(self, url: str) -> bool:
        if self.disallow_all:
            return False
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool] | None = None
        for allow, pattern in self.rules:
            if _matches(pattern, path) and (best is None or (len(pattern), allow) > best):
                best = (len(pattern), allow)
        return best is None or best[1]


# --- the fetcher -----------------------------------------------------------------------------------------------------

_REDIRECTS = (301, 302, 303, 307, 308)
_BODY_KEYS = ("json", "content", "data", "files")


class Fetcher:
    def __init__(self, client: httpx.AsyncClient | None = None, per_host_seconds: float = 1.0) -> None:
        self.client = client or httpx.AsyncClient(transport=GuardedTransport(), timeout=20.0, follow_redirects=False)
        self.per_host_seconds = per_host_seconds
        self._robots: dict[str, Robots | None] = {}
        self._robots_unreachable: set[str] = set()
        self._too_slow: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        #: The request a host has open (one at a time) and the places a platform has (`PLATFORM_AT_ONCE`): `_turn`.
        self._open: dict[str, asyncio.Lock] = {}
        self._places: dict[str, asyncio.Semaphore] = {}
        #: When the last `PLATFORM_AT_ONCE` requests of each platform began, and the lock for its rate (`_begin`).
        self._started: dict[str, collections.deque[float]] = {}
        self._rates: dict[str, asyncio.Lock] = {}
        #: The host's robots.txt being read: one request reads it, the others at the host wait for it (`_allowed`).
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        #: Hosts this fetcher was asked to go slower with, and the seconds between requests it keeps (`slow_host`).
        self._slowed: dict[str, float] = {}
        self.refused_hosts: set[str] = set()
        self.requests = 0
        #: Hosts whose robots.txt is not asked, and the one path prefix of each that may be read (`read_exempt`).
        self._exempt: dict[str, str] = {}
        #: When this fetcher's read must stop (`time.monotonic()` seconds); None for no time limit (see `timeup`).
        self.deadline: float | None = READ_BY.get()

    async def close(self) -> None:
        await self.client.aclose()

    def slow_host(self, host: str, factor: float, most: float) -> float:
        """Space this fetcher's requests to `host` `factor` times further apart for the rest of its run, to at most
        `most` seconds; returns the new gap."""
        now = max(self._own_pace(host), self._slowed.get(host, 0.0))
        self._slowed[host] = min(now * factor, max(most, now))
        return self._slowed[host]

    def _own_pace(self, host: str) -> float:
        """The host's runbook's pace, else this fetcher's. A host whose runbook did not load is refused, never read at
        the generic pace (design 2.4a)."""
        try:
            return pace_of(host, self.per_host_seconds)
        except registry.RunbookUnavailable as exc:
            raise Refused(str(exc)) from exc

    def _every(self, host: str) -> float:
        """The seconds between two requests to `host`: the fetcher's pace, or the host's own when it is read more slowly
        (its runbook's, `slow_host`, a robots.txt Crawl-delay)."""
        rules = self._robots.get(host)
        return max(self._own_pace(host), self._slowed.get(host, 0.0), (rules.delay or 0.0) if rules else 0.0)

    async def _wait_turn(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = self._every(host) - (time.monotonic() - self._last.get(host, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            self._last[host] = time.monotonic()

    @contextlib.asynccontextmanager
    async def _turn(self, host: str) -> AsyncIterator[None]:
        """One request's turn, from its wait for the host to its answer being in. A host has one request open at a time,
        however slowly it answers, a pace apart, and the platform it is on (`platform_of`) has `PLATFORM_AT_ONCE` places
        and is asked at most that many requests in one pace (`_begin`). The host is waited for first, with no place
        held, so the requests that wait for one host hold none of the platform's places. A place is given up when the
        request ends, however it ends."""
        async with self._open.setdefault(host, asyncio.Lock()):
            await self._wait_turn(host)
            platform = platform_of(host)
            async with self._places.setdefault(platform, asyncio.Semaphore(PLATFORM_AT_ONCE)):
                await self._begin(host, platform)
                yield

    async def _begin(self, host: str, platform: str) -> None:
        """The platform's rate: no more than `PLATFORM_AT_ONCE` requests of one platform begin in one pace, so a place
        freed by a quick answer is not taken again at once by a host whose own pace is long met (docs/PRD.md 28.17).
        Waits until the fourth-last request of the platform began a pace ago, then notes this one as begun, for the
        host's own pace too (`_last`): a request is paced from the moment it begins, not from the moment it asked.
        The pace is the fetcher's own (`per_host_seconds`), not the arriving host's: a host that is read more slowly
        (`slow_host`, its runbook's, a Crawl-delay) has kept its pace in `_wait_turn` before it got here, and waiting
        for it again here would hold the platform's rate lock and a place for that long, with every other host of the
        platform queued behind it."""
        starts = self._started.setdefault(platform, collections.deque(maxlen=PLATFORM_AT_ONCE))
        async with self._rates.setdefault(platform, asyncio.Lock()):
            if len(starts) == PLATFORM_AT_ONCE:
                gap = starts[0] + self.per_host_seconds - time.monotonic()
                if gap > 0:
                    await asyncio.sleep(gap)
            self._last[host] = time.monotonic()
            starts.append(self._last[host])

    async def _sent(self, host: str, method: str, url: str, headers: dict, kwargs: dict, cap: int) -> httpx.Response:
        """One request, in its turn (`_turn`: the host's pace and the platform's), counted, and its answer read."""
        async with self._turn(host):
            self.requests += 1
            return await self._exchange(method, url, headers, kwargs, cap)

    async def _exchange(self, method: str, url: str, headers: dict, kwargs: dict, cap: int) -> httpx.Response:
        """One request, no redirect followed: the answer read (decoded) to at most `cap` bytes, in at most
        `REQUEST_SECONDS`. Never inside an open database transaction (`db.outside_transaction`): a page that takes
        a minute would hold the transaction's row locks and its pooled connection that long."""
        db.outside_transaction(f"{method} {url}")
        request = self.client.build_request(method, url, headers=headers, **kwargs)
        too_large = f"{request.url.host} sent more than {cap // 1000} KB"
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                sent = await self.client.send(request, stream=True, follow_redirects=False)
                try:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in sent.aiter_bytes():
                        size += len(chunk)
                        if size > cap:
                            raise TooLarge(too_large, request=request)
                        chunks.append(chunk)
                finally:
                    await sent.aclose()
        except TimeoutError:
            raise httpx.ReadTimeout(f"{request.url.host} took longer than {REQUEST_SECONDS:g} s",
                                    request=request) from None
        kept = httpx.Headers(sent.headers)
        for name in ("content-encoding", "content-length", "transfer-encoding"):
            kept.pop(name, None)  # the body below is already decoded
        keep = ("http_version", "reason_phrase")
        reply = httpx.Response(sent.status_code, headers=kept, content=b"".join(chunks), request=request,
                               extensions={k: v for k, v in sent.extensions.items() if k in keep})
        return reply

    async def _load_robots(self, url: str) -> Robots | None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        try:
            # RFC 9309 (2.3.1.2): a robots.txt may redirect, even across hosts and to plain http; the file reached
            # within five redirects is the one to follow, as the host asked about has it.
            reply = await self._follow("GET", f"{parts.scheme}://{parts.netloc}/robots.txt",
                                       {"User-Agent": USER_AGENT}, {}, patient=False, page=False, cap=MAX_ROBOTS_BYTES,
                                       downgrade=True)
        except NotPublic:
            raise
        except (Refused, httpx.HTTPError):
            self._robots_unreachable.add(host)
            return Robots(disallow_all=True)
        if reply.status_code == 200:
            rules = Robots(reply.text)
            if rules.delay and rules.delay > MAX_CRAWL_DELAY:
                self._too_slow.add(host)
                rules.disallow_all = True
            return rules
        if 400 <= reply.status_code < 500:
            return None  # no robots.txt: everything is allowed
        self._robots_unreachable.add(host)
        return Robots(disallow_all=True)  # the site is failing: leave it alone

    async def allows(self, url: str) -> bool:
        """Whether robots.txt allows a page of this address (read once per host, like a request's own check)."""
        return await self._allowed(url)

    def read_exempt(self, runbook: Runbook, settings: object) -> None:
        """Do not ask the robots.txt of the one host and path prefix a runbook's `robots` names, for this fetcher only:
        the owner's one exception (the project rules, 2026-10-26: one board's public signed-out job search). The one
        door to the exception (`_exempt_robots` has no other caller), and it holds four guards, each refused with
        `ExemptRefused`: the runbook is the registry's own and carries `robots`; its pace is at least
        `ROBOTS_MIN_PACE_SECONDS` seconds; a refusal stops the host until the owner's next off hours (the stop's key
        ends with the night); the read is a guest's, signed out (the runbook has no signed-in session and this fetcher
        carries no cookie, login or authorization); and the owner's setting (`Settings.read_robots_exempt`, off by
        default) is on. Every other path of the host, and every other host, is still held to robots.txt, and the host's
        own pace (its runbook's) still applies."""
        found = registry.current()
        if runbook.robots is None or found.get(runbook.key) is not runbook:
            raise ExemptRefused("only a runbook of the registry that carries robots is read this way")
        host = runbook.robots.host
        if (runbook.pace_seconds is None or runbook.pace_seconds < ROBOTS_MIN_PACE_SECONDS
                or found.pace_for_host(host) < ROBOTS_MIN_PACE_SECONDS):
            raise ExemptRefused(
                f"a read though robots.txt asks not to is paced at {ROBOTS_MIN_PACE_SECONDS:g} seconds or more")
        if runbook.stop != "until_next_off_hours" or not found.ends_with_the_night(host):
            raise ExemptRefused("a read though robots.txt asks not to stops for the night at any refusal")
        client = self.client
        if (runbook.signed_in or len(client.cookies.jar) or client.auth is not None
                or any(name.lower() in ("cookie", "authorization") for name in client.headers)):
            raise ExemptRefused("a read though robots.txt asks not to is a signed-out guest's, with no login")
        if getattr(settings, registry.ROBOTS_EXEMPT_SETTING, False) is not True:
            raise ExemptRefused("the owner has not switched this read on")
        self._exempt_robots(host, runbook.robots.path_prefix)

    def _exempt_robots(self, host: str, prefix: str) -> None:
        """The primitive behind `read_exempt`, which is its only caller (a test holds that): `host`'s robots.txt is
        not asked about paths under `prefix`."""
        self._exempt[host.lower()] = prefix

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host.lower() in self._exempt and parts.path.startswith(self._exempt[host.lower()]):
            return True
        if host not in self._robots:
            # One request reads it; the others at the host wait here, and find it read (a wide read begins many requests
            # to a host not yet asked together: each would read robots.txt for itself, a paced request apiece).
            async with self._robots_locks.setdefault(host, asyncio.Lock()):
                if host not in self._robots:
                    self._robots[host] = await self._load_robots(url)
        rules = self._robots[host]
        return rules is None or rules.allows(url)

    async def _page(self, method: str, url: str, headers: dict, kwargs: dict, patient: bool) -> httpx.Response:
        """One address of a request: not refused earlier, allowed by robots.txt, paced; a 403 is a stop for the host."""
        host = urlsplit(url).hostname or ""
        if host in self.refused_hosts:
            raise Refused(f"{host} refused an earlier request in this run")
        if why := host_stops.stopped_reason(host):  # a refusal or bot check stopped it (host_stops.py)
            raise Refused(f"{host} is stopped: {why}")
        if not await self._allowed(url):
            if host in self._too_slow:
                raise RobotsRefused(f"robots.txt on {host} asks for more time between requests than this app waits")
            raise RobotsRefused(f"robots.txt on {host} asks not to read {urlsplit(url).path}",
                                unreachable=host in self._robots_unreachable)
        for attempt in range(2):
            reply = await self._sent(host, method, url, headers, kwargs, MAX_BYTES)
            if reply.status_code == 403:
                self.refused_hosts.add(host)
                await host_stops.record_fetch_refusal(host, 403)
                raise Refused(f"{host} refused the request (403)")
            if reply.status_code == 429 and attempt == 0 and patient:
                wait = retry_after(reply.headers.get("Retry-After"), 5.0)
                if wait > MAX_RETRY_AFTER:
                    return reply
                await asyncio.sleep(wait)
                continue
            return reply
        return reply

    async def _follow(self, method: str, url: str, headers: dict, kwargs: dict, *, patient: bool, page: bool,
                      cap: int, downgrade: bool = False, redirects: bool = True) -> httpx.Response:
        """The request, and the redirects it meets, one hop at a time: each address is checked as public and (for a
        page) allowed, paced and counted like a first request. `page` False is a robots.txt, which is not itself held
        to robots.txt. A redirect from https to http is refused unless `downgrade` (a robots.txt's own redirects)."""
        history: list[httpx.Response] = []
        for _ in range(MAX_HOPS + 1):
            if not public_url(url):
                raise NotPublic(f"{urlsplit(url).hostname or 'that address'} is not on the public internet")
            if page:
                reply = await self._page(method, url, headers, kwargs, patient)
            else:
                reply = await self._sent(urlsplit(url).hostname or "", method, url, headers, kwargs, cap)
            where = reply.headers.get("location")
            if reply.status_code not in _REDIRECTS or not where or not redirects:
                reply.history = history
                return reply
            target = urljoin(url, where)
            if not public_url(target):
                raise NotPublic(f"{urlsplit(target).hostname or 'that address'} is not on the public internet")
            here, there = urlsplit(url), urlsplit(target)
            if here.scheme == "https" and there.scheme != "https" and not downgrade:
                raise Refused(f"{here.hostname} redirected from https to {there.scheme or 'a bare address'}")
            history.append(reply)
            status = reply.status_code
            if (status == 303 and method != "HEAD") or (status in (301, 302) and method == "POST"):
                method = "GET"
                kwargs = {k: v for k, v in kwargs.items() if k not in _BODY_KEYS}
            kwargs = {k: v for k, v in kwargs.items() if k != "params"}
            if (there.hostname or "") != (here.hostname or ""):  # nothing that signs in goes on to another host
                headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "cookie")}
                kwargs = {k: v for k, v in kwargs.items() if k not in ("auth", "cookies")}
            url = target
        raise httpx.TooManyRedirects(f"more than {MAX_HOPS} redirects", request=reply.request)

    async def request(self, method: str, url: str, *, patient: bool = True, **kwargs) -> httpx.Response:
        """One polite request. Raises Refused when the site says no (or the address is not a public one); returns any
        other answer as it came, redirects followed. `patient`: wait out a "too many requests" once (the night's reads);
        off, that answer comes back at once (a quick test of a site). `redirects` False: a redirect comes back as it
        is, not followed (a site whose redirect is itself an answer)."""
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9", **kwargs.pop("headers", {})}
        redirects = kwargs.pop("redirects", True)
        reply = await self._follow(method, url, headers, kwargs, patient=patient, page=True, cap=MAX_BYTES,
                                   redirects=redirects)
        if (tap := _TAP.get()) is not None:
            tap.append(reply)
        return reply

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


@contextlib.contextmanager
def tapping() -> Iterator[list[httpx.Response]]:
    """Inside the block, every answer `Fetcher.request` returns to this task is also put in the list given, as it came
    (the bytes of the last hop's reply, not changed). The raw copy of a read is made from it."""
    answers: list[httpx.Response] = []
    token = _TAP.set(answers)
    try:
        yield answers
    finally:
        _TAP.reset(token)


#: What was read ahead of the transaction the task is in now, by the reader's own key (`read_ahead`).
_AHEAD: contextvars.ContextVar[dict | None] = contextvars.ContextVar("polite_ahead", default=None)


@contextlib.contextmanager
def read_ahead(answers: dict | None) -> Iterator[None]:
    """Inside the block, `ahead(key)` hands back what was read for `key` before the transaction began. For work that
    must read the network inside a transaction it cannot end: a process step, which runs inside the engine's own
    transaction (a savepoint, often). The code that starts the step reads first, with no transaction open
    (`db.release`), and the step takes what was read instead of reading itself. A key the read did not reach is read
    by the step as before (and `db.outside_transaction` says so)."""
    merged = {**(_AHEAD.get() or {}), **(answers or {})}
    token = _AHEAD.set(merged)
    try:
        yield
    finally:
        _AHEAD.reset(token)


#: What `ahead` answers for a key nothing was read for.
NOT_AHEAD = object()


def ahead(key: object) -> object:
    """What was read for `key` ahead of the transaction (`read_ahead`), else `NOT_AHEAD`."""
    return (_AHEAD.get() or {}).get(key, NOT_AHEAD)


def make() -> Fetcher:
    """The fetcher every read of the app's own code goes through: the polite one, which honours robots.txt, keeps to
    about a request a second to a host, says what it is and reads only public addresses. Tests replace this one
    attribute to answer without the network; a caller always calls it as `polite.make()`, never imports the name."""
    return Fetcher()


#: The HTTP statuses that say a page or a job system's posting no longer exists. Nothing else does.
_GONE_STATUSES = (404, 410)


def gone(link: str, status: int | None) -> str | None:
    """What the evidence of a source that answered 404 or 410 says, for the role's history, or None for any other
    status (a block, a rate limit, a 5xx or no answer is never "gone"). The one place the rule is written."""
    if status not in _GONE_STATUSES:
        return None
    return f"{link} answered HTTP {status}, which means the page or the posting does not exist."


def timeup(fetch: object) -> bool:
    """Whether the read `fetch` belongs to has run out of its time. A loop over boards, pages or postings asks this
    between its requests and stops there, keeping what it has read and marking what it read as not the whole. A
    stand-in without a deadline never runs out."""
    deadline = getattr(fetch, "deadline", None)
    return deadline is not None and time.monotonic() >= deadline


def give_time(fetch: Fetcher, seconds: float) -> Fetcher:
    """Give `fetch` `seconds` from now to finish its read (`timeup` then says so); hands it back."""
    fetch.deadline = time.monotonic() + seconds
    return fetch
