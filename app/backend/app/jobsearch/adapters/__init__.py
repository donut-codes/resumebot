"""One reader per job system (docs/PRD.md 28.4): given an employer's board address (from `ats.parse`), read every open
posting through the polite fetcher and hand back the same plain shape whichever system it came from.

Every job system is a `System` (`_address.py`): the domains it serves, which addresses name an employer's board and
which one posting, how it is read. A system with a reader is an `Adapter` (below); one with none (`unread.py`) knows its
addresses and no more. `ats.parse` asks each of them in turn, so a system's address rules sit beside its reader, and a
new reader brings its own.

`read` lists the board. `detail` fills in what a system's list leaves out (the description, the true date) for one
posting; it is given only the stored posting (`ext_id`, `url`, `title`) and answers with the fields to update, or `{}`
when the list already held everything. A system that answers with an error raises `AdapterError`; `polite.Refused`
is never caught here, the caller decides what a refusal means.

A package: `_common.py` holds the shape, the error, the helpers and the base class; a system is a runbook that says how
its list is read (`feeds.py`) or names a hook that reads it (`hooked.py`), each with its own way of paging (a board is
read to its last page, `Pages`); `ADAPTERS` below lists them by their key (the name `Employer.ats` stores). A system
whose one posting is read but whose list may not be is in `POSTING_ONLY` instead: `ats.parse` finds its postings'
reader there, and the night never reads its board.

A system that lets a reader ask for less than the whole board (`Adapter.filters`) is asked by the night's read for the
owner's places and ways of working and, between full reads, for what is new (`read_asked`, `_ask.py`, docs/PRD.md
39.5); the rest keep their full lists."""

from collections.abc import Mapping
from types import MappingProxyType

from app.jobsearch.adapters._address import Address, System, id_like
from app.jobsearch.adapters._ask import Ask, Asked, Wanted
from app.jobsearch.adapters._common import (
    ENOUGH_READ,
    PAGE_REPEATED,
    TIME_RAN_OUT,
    Adapter,
    AdapterError,
    Detailed,
    Listing,
    Probed,
    _loose_date,
    _mode,
    _posting,
    html_to_text,
    job_posting,
    main_text,
    page_facts,
    page_text,
    parse_comp,
    stopped_at,
)
from app.jobsearch.adapters.feeds import made as _feeds
from app.jobsearch.adapters.hooked import made as _hooked
from app.jobsearch.adapters.unread import UNREAD
from app.sites import registry

__all__ = ["ADAPTERS", "ENOUGH_READ", "PAGE_REPEATED", "READERS", "SYSTEMS", "TIME_RAN_OUT", "UNREAD", "Adapter",
           "AdapterError", "Address", "Ask", "Asked", "Detailed", "Listing", "POSTING_ONLY", "Probed", "System",
           "Wanted",
           "_loose_date", "_mode", "_posting", "html_to_text", "id_like", "job_posting", "main_text", "page_facts",
           "page_text", "parse_comp", "stopped_at"]


def _in_order(readers: list[Adapter]) -> dict[str, Adapter]:
    """The readers by their key, in the order the runbooks say `ats.parse` asks their systems (a reader whose runbook
    is not here comes last, in the order given)."""
    asked = {key: at for at, key in enumerate(registry.order())}
    return {a.key: a for a in sorted(readers, key=lambda a: asked.get(a.key, len(asked)))}


_HOOKED, _HOOKED_POSTING_ONLY = _hooked()
#: Every job system the app has a reader for, by its key, in the order `ats.parse` asks them (`ats.SYSTEMS`). The plain
#: feeds are made from their runbooks (`feeds.py`), the rest from the hooks their runbooks name (`hooked.py`).
ADAPTERS: dict[str, Adapter] = _in_order([*_HOOKED, *_feeds()])
#: A job system whose one posting is read by code but whose list is not (a list route its robots.txt disallows). It is
#: NOT in `ADAPTERS`, which means "the night reads this system's board": it has a reader for `ats.parse` ->
#: `Link.read` -> `postings.read_link` and nothing else. Its runbook says so (`flags.posting_only`).
POSTING_ONLY: dict[str, Adapter] = {a.key: a for a in _HOOKED_POSTING_ONLY}
#: What `ats` asks, made once here when this package is imported and never rebound or changed: the readers alone, and
#: every job system, readers first, in the order `ats.parse` asks them. `ats` loads them lazily, at the first question
#: put to it, and that can come while a test has `ADAPTERS` stood in for, as many tests do for the code that reads a
#: board: what a link says must be the real rules whenever that is, and a table `ats` had kept from a stand-in stayed
#: for the rest of the process.
READERS: Mapping[str, Adapter] = MappingProxyType({**ADAPTERS, **POSTING_ONLY})
SYSTEMS: Mapping[str, System] = MappingProxyType({**ADAPTERS, **POSTING_ONLY, **UNREAD})
# Not read, and why (docs/PRD.md 28.6): `UNREAD`, in `unread.py`. A system whose list route its robots.txt disallows, or
# whose career centre has no public route that was found, says so in its runbook (`why_not`); one whose list is
# disallowed but whose postings are allowed has only its postings read (`POSTING_ONLY`).
