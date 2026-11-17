"""What a night's read asks a board for, and so what it could have returned (docs/PRD.md 39.5 and 39.24).

A job system that lets a reader ask for less than its whole board is asked for the owner's places and ways of working
(Hard pass's work location) and, between full reads, for what is new. The code gates stay the safety net and still
judge everything a board returns, so the question is put a little wider than the gates: a place the gates cannot rule
out is asked for. And a board that was asked for less cannot say a posting it left out has gone, so a filtered read
closes only the postings it could have returned (`Asked.can_close`).

- `Wanted` is the owner's work location gate (the All tab's) as a filter reads it: whether the gates keep a posting that
  names a place, which ways of working they accept, and a fingerprint that changes when the owner's places do.
- `Ask` is what `postings.refresh` hands an adapter that has filters (`Adapter.read_asked`): the places, and how many
  days back to look when only what is new is wanted.
- `Asked` is what the adapter hands back in `Listing.asked`: what it really asked for (it asks for nothing it cannot
  ask for, and a board it cannot filter is read whole), which stored postings that question covers, and whether it
  asked only for what is new.

The role tabs' own work locations add to All's and never widen them (docs/PRD.md 31.9), so a filter made from All's
alone is never narrower than the gates."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.jobsearch.measures.pay_place import Gates, location_ok

#: The ways of working, as the gates and the adapters name them.
WAYS = ("remote", "hybrid", "onsite")
#: The statuses a board answers a question it does not take with: the read then goes on without the question, instead of
#: failing a board that reads whole.
FILTER_REFUSED = (400, 422)


def saves_a_page(total: int, kept: int, page: int) -> bool:
    """Whether asking a board for `kept` of its `total` postings costs fewer page loads than reading all of them: the
    first page, which said what could be asked, is read either way, so a question that leaves the board no shorter by
    a page is not worth putting (and a board that fits on its first page has nothing to save)."""
    return math.ceil(max(min(kept, total), 0) / page) + 1 < math.ceil(total / page)


@dataclass(frozen=True)
class Wanted:
    """The owner's work location gate as a board's filter reads it. It exists only for an owner who has one: with none,
    every place is fine and there is nothing to ask a board for (`of`)."""

    gates: Gates

    @classmethod
    def of(cls, gates: Gates | None) -> "Wanted | None":
        """The work location gate to ask boards for, or None when there is none (or no gates were given)."""
        return cls(gates) if gates is not None and gates.places_gated else None

    def keeps(self, location: str | None, *modes: str | None) -> bool:
        """Whether the gates would not rule out a posting that names this place: they keep it, or cannot tell (a town
        written with no state, a place that is only a country). `modes` are the ways of working the posting might be
        listed as, besides none: a posting is kept if the gates keep it as any of them, so the answer is never narrower
        than the gate's on any way the board may list it."""
        # The gates hold a place with no work mode that they do not list as unknown (kept, judged again once the work
        # mode is known). A board lists no work mode, so keeping such places would leave nothing to ask for: the
        # question reads them as the gates did before, as hybrid or in person, and so is narrower than the gates there.
        return any(location_ok(location, mode, self.gates, unlisted_is_unknown=False) is not False
                   for mode in dict.fromkeys((None, *modes)))

    @property
    def ways(self) -> tuple[str, ...]:
        """The ways of working the gates accept (a subset of `WAYS`, in its order)."""
        g = self.gates
        on = {"remote": g.remote_ok, "hybrid": g.hybrid_ok, "onsite": g.onsite_ok}
        return tuple(way for way in WAYS if on[way])

    @property
    def remote_ok(self) -> bool:
        return self.gates.remote_ok

    @property
    def fingerprint(self) -> str:
        """A short fingerprint of the owner's places and ways of working: it changes when they do, so the next night's
        read of every board is a full one (docs/PRD.md 39.24), and not when anything else on Hard pass does."""
        g = self.gates
        parts = [g.remote_ok, sorted(c.strip().lower() for c in g.remote_countries), g.hybrid_ok,
                 sorted(g.hybrid_places), g.onsite_ok, sorted(g.onsite_places)]
        return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Ask:
    """What tonight's read asks one board for. `wanted`: the owner's places and ways of working (None: the whole board's
    places). `new_days`: ask only for postings listed in the last this-many days (None: all of them); the days start
    from the board's last full read, with a little to spare."""

    wanted: Wanted | None = None
    new_days: int | None = None


@dataclass(frozen=True)
class Asked:
    """What a read really asked a board for, in `Listing.asked` (None there: the whole board).

    `within` says whether a stored posting is one the read's place or way-of-working filter would have returned had the
    board still held it (None: the filter left nothing out). `new_only`: the read asked only for what is new. A posting
    that is not in the read is closed only by a read that could have returned it (docs/PRD.md 39.5)."""

    within: Callable[[Any], bool] | None = None
    new_only: bool = False

    def covers(self, posting: Any) -> bool:
        """Whether the read's filter would have returned this stored posting, if the board still had it."""
        return self.within is None or self.within(posting)

    def can_close(self, posting: Any) -> bool:
        """Whether the read not returning this stored posting says the board took it down: never for a read of only what
        is new, which does not return an old posting, and for a read filtered by place only for a posting in those
        places."""
        return not self.new_only and self.covers(posting)
