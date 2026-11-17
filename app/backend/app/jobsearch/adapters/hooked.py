"""The readers that are code a runbook points to (the generic-sites design, Part 3.4): a job system whose list or
posting is read by logic no runbook can say (a facet it asks for, a session it takes from a page) names a hook,
`reader` in its runbook, which is a module of the restricted folder's `code/` and nothing else. The hook is the
system's reader as the app has always asked one: a class `Reader` (an `Adapter`) whose methods are given a `Ctx`
where they were given a fetcher. This module is the one thing that makes such a reader the app's: it binds the
runbook's facts to it, and puts a `Ctx` between it and the fetcher.

A `Ctx` is all a hook reads through. `get` and `post` go only to the runbook's own hosts (`hosts`, and
`read_hosts` for a feed that is not on the site's page host), through the polite fetcher, so the host's pace and
stop are held for it and a refusal is the fetcher's own; a hook has no fetcher, no robots method and no way to
another host. It also says when the night's time has run out, and nothing else.

Nothing here names a site: which hooks there are is what the runbooks say."""

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from app.jobsearch.adapters._common import Adapter, AdapterError
from app.jobsearch.fetch.polite import Fetcher, timeup
from app.sites import registry
from app.sites.schema import Runbook

#: The methods of a reader that read from the site, so are given a `Ctx`.
READS = ("read", "read_asked", "detail", "probe", "listed")


class Ctx:
    """What a hook reads through (see the module text)."""

    def __init__(self, fetch: Fetcher, hosts: tuple[str, ...], name: str) -> None:
        self._fetch = fetch
        self._hosts = hosts
        self._name = name

    @classmethod
    def of(cls, fetch: "Fetcher | Ctx", hosts: tuple[str, ...], name: str) -> "Ctx":
        """`fetch` as a `Ctx`: one that is one already stays so (a hook's own methods call each other)."""
        return fetch if isinstance(fetch, Ctx) else cls(fetch, hosts, name)

    @property
    def deadline(self) -> float | None:
        """When the night's read must be done (`time_up`), if it has a limit."""
        return getattr(self._fetch, "deadline", None)

    def time_up(self) -> bool:
        """Whether the read has run out of its time: a loop over pages stops there and says so."""
        return timeup(self._fetch)

    def _own(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if not any(host == h or host.endswith("." + h) for h in self._hosts):
            raise AdapterError(f"{self._name} is read only from its own hosts")

    async def get(self, url: str, **kwargs: Any) -> Any:
        self._own(url)
        return await self._fetch.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        self._own(url)
        return await self._fetch.post(url, **kwargs)


def _with_ctx(original: Callable[..., Awaitable[Any]], hosts: tuple[str, ...], name: str) -> Callable[..., Any]:
    async def read(self: Adapter, fetch: "Fetcher | Ctx", *args: Any, **kwargs: Any) -> Any:
        return await original(self, Ctx.of(fetch, hosts, name), *args, **kwargs)

    read.__name__ = original.__name__
    read.__doc__ = original.__doc__
    return read


def adapter_for(runbook: Runbook, module: Any) -> Adapter:
    """The reader a hook module makes of its runbook: its `Reader`, with the runbook's facts (the key, the name, the
    hosts and the flags) in the place of what the reader left out, and its reads given a `Ctx`."""
    reader = module.Reader
    hosts = (*runbook.hosts, *runbook.read_hosts)
    # Only what the reader does itself: a method it takes from `Adapter` calls the reader's own, which have a `Ctx`.
    wrapped = {name: _with_ctx(getattr(reader, name), hosts, runbook.name)
               for name in READS if callable(getattr(reader, name, None))
               and getattr(reader, name) is not getattr(Adapter, name, None)}
    made = type(reader.__name__, (reader,), wrapped)()
    flags = runbook.flags
    made.key = runbook.key
    made.name = runbook.name
    made.domains = runbook.hosts
    made.needs = flags.needs
    made.own_parts = flags.own_parts
    made.customer_host = flags.customer_host
    made.drawn = flags.drawn
    made.why_not = flags.why_not
    made.places = flags.places
    made.probe_names = flags.probe_names
    return made


def made() -> tuple[list[Adapter], list[Adapter]]:
    """(the readers of the job systems the night reads, the readers of those whose one posting only is read), made of
    every runbook that names a hook, in the order their job systems are asked (`order`)."""
    reads: list[Adapter] = []
    posting_only: list[Adapter] = []
    for runbook in registry.current().runbooks:
        if runbook.kind != "job_system" or not runbook.reader:
            continue
        module = registry.hook_for(runbook.reader)
        if module is None:
            continue
        (posting_only if runbook.flags.posting_only else reads).append(adapter_for(runbook, module))
    return reads, posting_only
