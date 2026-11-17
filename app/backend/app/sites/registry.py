"""The registry of site runbooks: what the app knows of each site, read from files and held unchanged for the life of
the process (the generic-sites design, Part 2.1). Every question that was once answered by a constant that named a
site is answered here by a runbook: which hosts are job boards, in what order job systems are asked, which job systems
a new install starts off, how fast and how long a host is read and stopped, whose mail is a job system's.

It writes nothing, ever: no row of the database, no setting (2.2). The facts come from the files, live.

It fails closed (2.4). A runbook that fails to load is a problem the registry holds (`problems`), and every question
about a host that runbook names raises `RunbookUnavailable` instead of answering with the generic pace or a stop for
good. `uncovered` says which stored hosts no runbook covers while the restricted folder is absent or a file failed.

The registry is built by `loader.load`, once, at the first question (`current`). `replaced` is the one way a test puts
another in its place. Pure and standard library, besides the schema: importing this loads no reader and no network."""

import contextlib
import importlib
import importlib.util
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from app.sites.schema import DEFAULT_PACE_SECONDS, DEFAULT_SLOW_STATUSES, Runbook

#: The setting that switches on the read of a runbook that carries `robots` (off by default; `follow_board_on`).
ROBOTS_EXEMPT_SETTING = "read_robots_exempt"


class RunbookUnavailable(Exception):
    """A runbook that names this host did not load, so nothing is known of how to read or stop it: refuse, in plain
    words, rather than read it at the generic pace or stop it for good."""


@dataclass(frozen=True)
class Problem:
    """A runbook file that did not load: its path (relative to its folder), why, and the hosts it named as far as they
    could be read."""

    path: str
    reason: str
    hosts: tuple[str, ...] = ()


def key_of(host: str | None) -> str:
    """The host as a stop is kept: lower case, without `www.` or a trailing dot. Empty when there is none."""
    return (host or "").strip().lower().rstrip(".").removeprefix("www.")


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _host_of(address: str) -> str:
    """The host of a link, or of a bare host, lower case, without a trailing dot; empty when there is none."""
    text = address.strip()
    try:
        host = urlsplit(text if "//" in text else f"//{text}").hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def _domain_of(host: str) -> str:
    # Imported here: the employers package reaches the fetcher, which will ask this registry.
    from app.jobsearch.employers.follow import base_domain

    return base_domain(host.lower())


class Registry:
    """Every runbook that loaded, and the problems of those that did not. Built once; never changed."""

    def __init__(self, runbooks: Iterable[Runbook], problems: Iterable[Problem] = (), *,
                 restricted_present: bool = False, terms: Iterable[dict] = (),
                 hook_folder: Any = None) -> None:
        keyed: dict[str, Runbook] = {}
        problems = list(problems)
        for runbook in runbooks:
            if runbook.key in keyed:
                problems.append(Problem(runbook.key, "two runbooks have this key", runbook.hosts))
                continue
            keyed[runbook.key] = runbook
        self._by_key = keyed
        self._systems = tuple(sorted((r for r in keyed.values() if r.kind == "job_system"),
                                     key=lambda r: (r.order if r.order is not None else 0, r.key)))
        self._others = tuple(sorted((r for r in keyed.values() if r.kind != "job_system"), key=lambda r: r.key))
        self.problems: tuple[Problem, ...] = tuple(problems)
        self.restricted_present = restricted_present
        self._terms = tuple(terms)
        self._hook_folder = hook_folder
        self._hooks: dict[str, ModuleType] = {}
        self._broken = tuple(sorted({h for p in self.problems for h in p.hosts}))
        # What is asked often, worked out once: a registry never changes.
        self._board_hosts = tuple(h for r in self._others if r.kind == "board" for h in r.hosts)
        self._board_set = frozenset(self._board_hosts)
        self._own_careers = {h: tuple(r.address["own_careers_paths"]) for r in self.runbooks
                             if r.address.get("own_careers_paths") for h in r.hosts}
        self._until_night = tuple(h for r in self.runbooks if r.stop == "until_next_off_hours" for h in r.hosts)

    # --- lookups ---------------------------------------------------------------------------------------------------

    @property
    def runbooks(self) -> tuple[Runbook, ...]:
        """Every runbook: the job systems in the order they are asked, then the rest by key."""
        return (*self._systems, *self._others)

    def get(self, key: str | None) -> Runbook | None:
        return self._by_key.get(key) if key else None

    def for_host(self, host: str | None) -> Runbook | None:
        """The runbook whose host is the most specific one that `host` is, or is under; None when none is."""
        found = _host_of(host or "")
        best: tuple[int, Runbook] | None = None
        for runbook in self.runbooks:
            for h in runbook.hosts:
                if found and _under(found, h) and (best is None or len(h) > best[0]):
                    best = (len(h), runbook)
        return best[1] if best else None

    def by_address(self, address: str | None) -> Runbook | None:
        """`for_host` for the host of a link."""
        return self.for_host(_host_of(address or ""))

    def order(self) -> tuple[str, ...]:
        """The keys of the job systems, in the order `ats.parse` asks them."""
        return tuple(r.key for r in self._systems)

    def starts_off(self) -> tuple[str, ...]:
        """The job systems a new install starts switched off and does not list while they are, in `order`."""
        return tuple(r.key for r in self._systems if r.starts_off)

    def board_hosts(self) -> tuple[str, ...]:
        """The hosts of the job boards: sites that list other employers' roles."""
        return self._board_hosts

    def board_named(self, host: str | None) -> bool:
        """Whether a host is a job board's or a subdomain of one: each of its dotted-label endings is looked up."""
        labels = (host or "").lower().rstrip(".").split(".")
        return any(".".join(labels[i:]) in self._board_set for i in range(len(labels)))

    def code_read_hosts(self) -> tuple[str, ...]:
        """The hosts of the sites whose postings the app's own code reads (`code_reads_postings`)."""
        return tuple(h for r in self.runbooks if r.code_reads_postings for h in r.hosts)

    def shared_hosts(self) -> tuple[str, ...]:
        """The hosts many employers' pages sit on (not the boards': `board_hosts`)."""
        return tuple(h for r in self.runbooks if r.shared_host for h in r.hosts)

    def own_careers_paths(self) -> dict[str, tuple[str, ...]]:
        """A host that is also an employer's own careers site, under these path segments (the segments before a
        posting's id)."""
        return dict(self._own_careers)

    def mail_sets(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """(the job boards' sender domains, every job board's and job system's sender domains, the senders of an
        application's confirmation): three sets with three jobs (`may_name_a_role`, `from_a_system`, `confirms`)."""
        def senders(wanted: tuple[str, ...], confirming: bool = False) -> tuple[str, ...]:
            out: list[str] = []
            for r in self.runbooks:
                if r.mail.kind in wanted and (r.mail.confirms or not confirming):
                    out += [s for s in r.mail.senders if s not in out]
            return tuple(out)

        return senders(("board",)), senders(("board", "system")), senders(("board", "system"), confirming=True)

    def verdict(self, host: str | None) -> str | None:
        """What `site_terms.json` says of the host (`allows`, `silent` or `forbids`), by its most specific entry; None
        when no entry covers it. The verdict is never a field of a runbook."""
        found = _host_of(host or "")
        best: tuple[int, str] | None = None
        for entry in self._terms:
            for h in entry.get("hosts", ()):
                h = str(h).strip().lower().rstrip(".")
                if found and _under(found, h) and (best is None or len(h) > best[0]):
                    best = (len(h), entry["verdict"])
        return best[1] if best else None

    def follow_board_on(self, settings: object) -> bool:
        """Whether the owner has switched on the read of a board whose roles must be followed to the employer's own
        site, and a runbook says there is one."""
        return bool(getattr(settings, ROBOTS_EXEMPT_SETTING, False)) and any(
            r.robots and r.follow_to_employer for r in self.runbooks)

    def robots_runbooks(self) -> tuple[Runbook, ...]:
        """The runbooks read though robots.txt asks automated readers not to (restricted folder only)."""
        return tuple(r for r in self.runbooks if r.robots)

    def hook_for(self, name: str | None) -> ModuleType | None:
        """The hook a runbook's `reader` names, loaded once from the restricted folder's `code/` and from nowhere else;
        None when no runbook names it. A hook is one module (`code/<name>.py`) or a package (`code/<name>/`, whose
        modules name each other relatively); a package is kept in `sys.modules` so it is one object for the process,
        whatever registry asks for it."""
        if not name or self._hook_folder is None or not any(r.reader == name for r in self.runbooks):
            return None
        if name not in self._hooks:
            package = self._hook_folder / name / "__init__.py"
            path = package if package.is_file() else self._hook_folder / f"{name}.py"
            module_name = f"restricted_hook_{name}"
            if package.is_file() and module_name in sys.modules:
                self._hooks[name] = sys.modules[module_name]
                return self._hooks[name]
            spec = importlib.util.spec_from_file_location(
                module_name, path, submodule_search_locations=[str(path.parent)] if package.is_file() else None)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            if package.is_file():
                sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(module_name, None)
                raise
            self._hooks[name] = module
        return self._hooks[name]

    def board_reader(self, part: str | None = None) -> ModuleType | None:
        """The hook of the board whose roles are followed to the employer's own site (its package, or one module of it
        by name), or None where no runbook has one: a public install, or the restricted folder absent."""
        for runbook in self.runbooks:
            if runbook.follow_to_employer and runbook.reader:
                module = self.hook_for(runbook.reader)
                if module is None or part is None:
                    return module
                return importlib.import_module(f"{module.__name__}.{part}")
        return None

    # --- stops and pace (1.6) ----------------------------------------------------------------------------------------

    def _check(self, host: str) -> None:
        """Refuse a host a runbook that did not load names (2.4a)."""
        found = key_of(host)
        for broken in self._broken:
            if found and _under(found, broken):
                raise RunbookUnavailable(
                    f"{found} is a site whose runbook did not load, so how to read or stop it is not known; "
                    "it is left alone until that is fixed")

    def refusal(self, host: str | None) -> str | None:
        """The plain reason a read of this host is refused, or None."""
        try:
            self._check(host or "")
        except RunbookUnavailable as exc:
            return str(exc)
        return None

    def uncovered(self, domains: Iterable[str]) -> list[str]:
        """Of the hosts something is stored for (a stop, a pace), those no runbook covers while the registry is not
        whole: the restricted folder is absent, or a file did not load. Such a host must not be read at the generic
        pace or stopped for good (2.4a); `stack.py smoke` fails on a gap."""
        if self.restricted_present and not self.problems:
            return []
        return [d for d in domains if self.for_host(d) is None]

    def stop_key(self, host: str | None) -> str:
        """What a stop is kept and asked under: the host (`key_of`), but one key, the domain's, for every host of a
        domain whose stop ends at the owner's off hours, so a refusal at one of its hosts stops all of them."""
        key = key_of(host)
        self._check(key)
        for domain in self._until_night:
            if _under(key, domain):
                return domain
        return key

    def ends_with_the_night(self, host: str | None) -> bool:
        """Whether a stop of this host ends at the owner's next off hours start, not for good."""
        return self.stop_key(host) in self._until_night

    def pace_for_domain(self, domain: str | None) -> float:
        """The most seconds between two requests that any host of this registrable domain is held to: the largest pace
        of the runbooks whose hosts have this domain. 0.0 when none is slowed (the caller's own gap then stands)."""
        if not domain:
            return 0.0
        self._check(domain)
        return max((r.pace_seconds for r in self.runbooks if r.pace_seconds is not None
                    and any(_domain_of(h) == domain for h in r.hosts)), default=0.0)

    def pace_for_host(self, host: str | None, default: float = DEFAULT_PACE_SECONDS) -> float:
        """The seconds between two requests to this host: its runbook's pace, else `default`. A runbook with `robots`
        always has one (a failed one raises: there is no default for it)."""
        found = key_of(host)
        self._check(found)
        runbook = self.for_host(found)
        return runbook.pace_seconds if runbook and runbook.pace_seconds is not None else default

    def slow_statuses_for(self, host: str | None) -> tuple[int, ...]:
        """The statuses of an answer that says "not now" for this host: its runbook's, else the generic ones."""
        found = key_of(host)
        self._check(found)
        runbook = self.for_host(found)
        return runbook.slow_statuses if runbook and runbook.slow_statuses is not None else DEFAULT_SLOW_STATUSES


# --- the one registry of the process ----------------------------------------------------------------------------------

_current: Registry | None = None


def current() -> Registry:
    """The registry of the process: loaded from the shipped folder and the restricted folder at the first question."""
    global _current
    if _current is None:
        from app.sites import loader

        _current = loader.load()
    return _current


@contextlib.contextmanager
def replaced(registry: Registry) -> Iterator[Registry]:
    """The one test hook: for the length of the block, `registry` is the registry of the process."""
    global _current
    before = _current
    _current = registry
    try:
        yield registry
    finally:
        _current = before


def get(key: str | None) -> Runbook | None:
    return current().get(key)


def for_host(host: str | None) -> Runbook | None:
    return current().for_host(host)


def by_address(address: str | None) -> Runbook | None:
    return current().by_address(address)


def order() -> tuple[str, ...]:
    return current().order()


def starts_off() -> tuple[str, ...]:
    return current().starts_off()


def board_hosts() -> tuple[str, ...]:
    return current().board_hosts()


def board_named(host: str | None) -> bool:
    return current().board_named(host)


def code_read_hosts() -> tuple[str, ...]:
    return current().code_read_hosts()


def shared_hosts() -> tuple[str, ...]:
    return current().shared_hosts()


def own_careers_paths() -> dict[str, tuple[str, ...]]:
    return current().own_careers_paths()


def mail_sets() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return current().mail_sets()


def verdict(host: str | None) -> str | None:
    return current().verdict(host)


def follow_board_on(settings: object) -> bool:
    return current().follow_board_on(settings)


def hook_for(name: str | None) -> ModuleType | None:
    return current().hook_for(name)


def board_reader(part: str | None = None) -> ModuleType | None:
    return current().board_reader(part)


def stop_key(host: str | None) -> str:
    return current().stop_key(host)


def ends_with_the_night(host: str | None) -> bool:
    return current().ends_with_the_night(host)


def pace_for_domain(domain: str | None) -> float:
    return current().pace_for_domain(domain)


def pace_for_host(host: str | None, default: float = DEFAULT_PACE_SECONDS) -> float:
    return current().pace_for_host(host, default)


def slow_statuses_for(host: str | None) -> tuple[int, ...]:
    return current().slow_statuses_for(host)

