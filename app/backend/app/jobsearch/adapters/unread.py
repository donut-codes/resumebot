"""The job systems the app has no reader for (docs/PRD.md 28.6): a company on one is found and its board's address
kept, but its postings are read in the owner's browser. Each is a `System` that knows its own addresses and no more; a
reader of its own makes it an `Adapter` in a module of its own (and takes it off `UNREAD`).

What such a system knows is its runbook's (`app/sites/runbooks`, and the restricted folder): its hosts, what it needs
of a board, why it cannot be read when that is known for sure, and its address rules (`app/sites/address_rules.py`).
Nothing here names one: a job system whose runbook has `address.board` and no plain feed to read is made here, in the
order its runbook says.
Some are hard on purpose, for a reason known for sure (the system's robots.txt disallows the route that lists a board,
or its career centre has no public route found); the rest have no reader because none has been built yet."""

from app.jobsearch.adapters._address import Address, System
from app.sites import address_rules, feed_read, registry
from app.sites.schema import Runbook


class DataSystem(System):
    """A job system whose addresses are read by the rules of its runbook."""

    def __init__(self, runbook: Runbook) -> None:
        self._runbook = runbook
        self.key = runbook.key
        self.name = runbook.name
        self.domains = runbook.hosts
        flags = runbook.flags
        self.needs = flags.needs
        self.own_parts = flags.own_parts
        self.customer_host = flags.customer_host
        self.drawn = flags.drawn
        self.why_not = flags.why_not

    @staticmethod
    def _subject(a: Address) -> address_rules.Subject:
        return address_rules.Subject(a.host, tuple(a.parts), a.query)

    def board(self, a: Address) -> dict | None:
        found = address_rules.rules(self._runbook, "board")
        hit = address_rules.first(found, self._subject(a)) if found else None
        return address_rules.captured(hit[0], hit[1], self._subject(a)) if hit else None

    def shaped(self, a: Address, read: bool) -> bool:
        found = address_rules.rules(self._runbook, "one_posting")
        return bool(found) and address_rules.shaped(found, self._subject(a))

    def board_url(self, board: dict) -> str | None:
        found = address_rules.rules(self._runbook, "board_url")
        return (address_rules.board_url(found, board) if found else None) or super().board_url(board)


def _made() -> dict[str, System]:
    return {r.key: DataSystem(r) for r in registry.current().runbooks
            if r.kind == "job_system" and "board" in r.address and feed_read.of(r) is None}


#: Each, by its key, in the order they are tried after the readers.
UNREAD: dict[str, System] = _made()
