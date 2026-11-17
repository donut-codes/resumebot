"""The readers that are only data: a job system whose runbook says how its list is read (`read`, checked by
`app/sites/feed_schema.py`) is read by `feed_read` and is made here into the same `Adapter` the hand-written ones are.
Its addresses are its runbook's rules (`app/sites/address_rules.py`), its probe is the runbook's, and its facts (what a
board needs, whether its pages draw their postings, whether a posting's list gives its places) are the runbook's flags.

A reader does only what its runbook says: it reads one posting from an address only when a rule names the posting's id
in it, asks a posting's own answer only when the runbook gives one, has a shape of its own for a page only when a rule
says so, and probes only when the runbook has a feed to ask. `adapter_for` puts together the class that does just that,
so the app's questions of a reader (`type(reader).detail is not Adapter.detail`) are answered from data.

Nothing here names a system: a new plain feed is a runbook and no code."""

from app.jobsearch.adapters._address import Address
from app.jobsearch.adapters._common import Adapter, Listing, Posting, Probed
from app.jobsearch.fetch.polite import Fetcher
from app.sites import address_rules, feed_read, feed_schema, registry
from app.sites.schema import Runbook


class _Feed(Adapter):
    """What every reader made of a runbook holds."""

    runbook: Runbook
    feed: feed_schema.Read

    @staticmethod
    def _subject(a: Address) -> address_rules.Subject:
        return address_rules.Subject(a.host, tuple(a.parts), a.query)


class FeedAdapter(_Feed):
    """A job system read from its runbook (made by `adapter_for`)."""

    def __init__(self, runbook: Runbook) -> None:
        read = feed_read.of(runbook)
        assert read is not None
        self.runbook = runbook
        self.feed = read
        self.key = runbook.key
        self.name = runbook.name
        self.domains = runbook.hosts
        flags = runbook.flags
        self.needs = flags.needs
        self.own_parts = flags.own_parts
        self.customer_host = flags.customer_host
        self.drawn = flags.drawn
        self.why_not = flags.why_not
        self.places = flags.places
        self.probe_names = flags.probe_names
        #: A list that is a page holds its postings in the page, and a list that needs a page read first is asked as the
        #: site's own page asks it; any other is a feed that answers with them.
        self.how = "pages" if read.kind == "embedded" or read.steps else "feed"
        #: A system with no answer for one posting is asked for the whole board, and the posting is picked from it.
        self.detail_is_item = bool(read.detail and read.detail.from_list)

    # --- its addresses

    def board(self, a: Address) -> dict | None:
        found = address_rules.rules(self.runbook, "board")
        hit = address_rules.first(found, self._subject(a)) if found else None
        return address_rules.captured(hit[0], hit[1], self._subject(a)) if hit else None

    def board_url(self, board: dict) -> str | None:
        found = address_rules.rules(self.runbook, "board_url")
        return (address_rules.board_url(found, board) if found else None) or super().board_url(board)

    # --- reading

    def parse_item(self, r: object, board: dict) -> Posting | None:
        return feed_read.parse_item(self.feed, r, board, self.domains)

    async def read(self, fetch: Fetcher, board: dict) -> Listing:
        return await feed_read.read_board(fetch, self.feed, board, who=self.name, hosts=self.domains)


class _Posts(_Feed):
    """A rule names the posting's id in an address: the reader reads one posting from it alone."""

    def posting(self, a: Address, board: dict) -> tuple[str, str] | None:
        found = address_rules.rules(self.runbook, "one_posting")
        return address_rules.posting_of(found, self._subject(a), a.given) if found else None


class _Shapes(_Feed):
    """A rule says what a posting's own page looks like, apart from what the reader reads."""

    def shaped(self, a: Address, read: bool) -> bool:
        found = address_rules.rules(self.runbook, "one_posting")
        return address_rules.shaped(found, self._subject(a)) if found else read


class _Applies(_Feed):
    """A rule says which address is a posting's application form and which page it follows from."""

    def apply_page(self, a: Address) -> str | None:
        found = address_rules.rules(self.runbook, "apply_page")
        subject = self._subject(a)
        hit = address_rules.first(found, subject) if found else None
        if hit is None or not hit[0].template:
            return None
        return hit[0].template.format(host=a.host, **address_rules.captured(hit[0], hit[1], subject))


class _Details(_Feed):
    """The runbook says what one posting's own answer adds to the list's."""

    async def detail(self, fetch: Fetcher, board: dict, posting: dict) -> dict:
        return await feed_read.read_detail(fetch, self.feed, board, posting, who=self.name, hosts=self.domains)


class _Probes(_Feed):
    """The runbook has a feed to ask whether a board by a name exists."""

    async def probe(self, fetch: Fetcher, slug: str) -> Probed | None:
        spec = self.runbook.probe
        return await feed_read.probe(fetch, spec, slug) if spec is not None else None


def adapter_for(runbook: Runbook) -> FeedAdapter:
    """The reader of a runbook that has a plain feed to read: a `FeedAdapter` that has what the runbook gives it and
    nothing it does not (see the module text)."""
    read = feed_read.of(runbook)
    assert read is not None
    one_posting = address_rules.rules(runbook, "one_posting") or ()
    named = [rule for rule in one_posting if any(name == "ext_id" for name, _ in rule.capture)]
    has = (
        (_Posts, bool(named)),
        # A page's own shape is the address the reader reads one posting from, unless some rule says otherwise.
        (_Shapes, any(rule.form or rule not in named for rule in one_posting)),
        (_Applies, bool(address_rules.rules(runbook, "apply_page"))),
        (_Details, read.detail is not None),
        (_Probes, runbook.probe is not None and runbook.probe.url is not None))
    mixins = tuple(cls for cls, present in has if present)
    cls = type("FeedAdapter", (*mixins, FeedAdapter), {}) if mixins else FeedAdapter
    return cls(runbook)


def made() -> list[FeedAdapter]:
    """Every plain feed the runbooks describe, in the order their job systems are asked (`order`)."""
    return [adapter_for(r) for r in registry.current().runbooks
            if r.kind == "job_system" and feed_read.of(r) is not None]
