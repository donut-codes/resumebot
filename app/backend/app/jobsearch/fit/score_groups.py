"""The arithmetic of a fit (docs/PRD.md 30.9, on 30.6 and 39.20): pure functions over rows whose answers are already
decided. No database, no session, no clock and no randomness: the same rows always give the same score. This module
imports nothing but the standard library, so it can run anywhere (`tests/core/test_score_groups_pure.py` holds it to
that). Who answered a row (an agent, or the app from a fact or the analyst's classes) is `scoring.py`'s business; here
a row is only the answer it ended up with.

There is no partial credit (the owner, 2026-10-14): a row that is met earns its full points, and one that is not met,
partly met, unknown or unanswered earns 0.
Prioritization only adds points (the owner, 2026-10-27, docs/PRD.md 30.10): a row's points below 0, left in a profile
saved before that rule, earn nothing, met or not. What can be earned is unchanged by an answer, so a role whose asks
have not been checked scores low, never high.

The rows (every row adds up with the others, an AND):

- `Single`, an ask on its own: its points when its answer is met, else nothing; a per-item ask counts each item up to
  its limit. A stored answer of "partly" is still read, and earns nothing.
- `FirstMatch`, a ranked set of levels: the first level that is true from the top decides, and the row earns that
  level's points, or nothing for a level whose points are below 0; no level true earns nothing. A level nobody can
  tell stops the scan, and leaves the row undecided and earning nothing.
- `AnyOf`, one row with several statements and one set of points: it earns them once, when a statement is met. It is
  decided when a statement is met, or when none is unknown.
- `Graded`, the owner's "grade the requirements for me": the sum of each required and preferred item's points by the
  analyst's class. The app decides it, so it is always decided.

What a row can add to a role's earned points is its `reach`: the most it can earn, counted once. A group counts at its
top value; a row whose points are below 0 counts for nothing, so a role can reach 100%. Every row is in what
can be earned, decided or not.

The percent is the earned points over what can be earned, worked out once from the exact earned points (two decimals),
half up, in whole numbers.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

MET, PARTLY, NOT, UNKNOWN = "met", "partly", "not", "unknown"
#: What each answer earns of a row's points: all of them when met, none otherwise. A stored "partly" is still an answer
#: (it is read, and counts as decided), and it earns nothing; so does one nobody could give (unknown, or no answer).
WEIGHT = {MET: 1.0, PARTLY: 0.0, NOT: 0.0}


# --- the rows ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Single:
    """An ask on its own, with the answer it has: met, partly, not or unknown (anything else is unknown). `count` is the
    items found, for an ask counted for each item (`per_item`), which `limit` stops."""

    id: str
    points: int
    answer: str = UNKNOWN
    per_item: bool = False
    limit: int | None = None
    count: int = 1


@dataclass(frozen=True)
class Level:
    """One level of a first match: its points, and whether it is true of the posting (`holds`: True, False, or None when
    nobody can tell yet)."""

    id: str
    points: int
    holds: bool | None = None


@dataclass(frozen=True)
class FirstMatch:
    """Levels, best first: the first one that holds, from the top, decides."""

    id: str
    levels: tuple[Level, ...]


@dataclass(frozen=True)
class AnyOf:
    """One row with several statements and one set of points: `parts` are their answers."""

    id: str
    points: int
    parts: tuple[str, ...]


@dataclass(frozen=True)
class GradeWeights:
    """What each item of a role earns by the analyst's class: the owner's settings
    (`profile_scoring.RequirementsGrade`), and these are their defaults."""

    required_proven: int = 2
    required_transferable: int = 0
    required_unproven: int = -2
    preferred_met: int = 1
    preferred_unproven: int = 0


@dataclass(frozen=True)
class Graded:
    """The required and the preferred items of a role, each by its class (P, T or U; an item with none is not
    counted), and the weights they earn by. Leave a side empty to leave it out (its part is off)."""

    id: str
    required: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()
    weights: GradeWeights = GradeWeights()


Item = Single | FirstMatch | AnyOf | Graded


@dataclass(frozen=True)
class ItemScore:
    """What a row did: what it could add (`reach`), whether it was decided, and what it added to the earned points
    (`earned`: nothing for a row that was not met, decided or not). `held` is the level of a first match that held, as
    its index (None: none did, or it is undecided: see `decided`); `weight` is what an any-of's points were taken at (1
    when a statement was met, else 0), once it is decided."""

    id: str
    reach: int
    decided: bool
    earned: float
    held: int | None = None
    weight: float | None = None


@dataclass(frozen=True)
class Scored:
    """A role's score. `possible` is the sum of the rows' reach, `decided` the reach of the decided rows, `coverage` the
    whole percent of the first that is the second, `earned` the points to two decimals, `total` those rounded half up to
    whole points, and `percent` the share of `possible` they are (None: nothing can be earned). `items` are the rows in
    the order given."""

    items: tuple[ItemScore, ...]
    possible: int
    decided: int
    earned: float
    coverage: int
    percent: int | None
    total: int


# --- what a row can add ---------------------------------------------------------------------------------


def reach_single(points: int, per_item: bool = False, limit: int | None = None) -> int:
    """The most an ask can add to the earned points: its points when they are positive (a penalty adds nothing), or for
    an ask counted for each item its limit. A per-item bonus with no limit counts its points once."""
    if points <= 0:
        return 0
    return limit if per_item and limit is not None and limit > 0 else points


def reach_group(kind: str, points: int, level_points: Sequence[int]) -> int:
    """The most a group can add, counted once at its top value: a first match (`best`) its largest positive level, an
    any-of (`any`) the row's points when they are positive; nothing for a penalty."""
    if kind == "best":
        return max(0, max(level_points, default=0))
    return max(0, points)


def first_true(holds: Sequence[bool | None]) -> tuple[bool, int | None]:
    """Which level holds, scanning from the top: `(decided, index)`. A level that holds is the held one and decides; one
    that fails goes on to the next; one nobody can tell stops the scan and leaves it undecided (a level below could not
    be first); after the last, no level holds and it is decided with none (index None)."""
    for i, hold in enumerate(holds):
        if hold is None:
            return False, None
        if hold:
            return True, i
    return True, None


def _items_earned(points: int, per_item: bool, limit: int | None, count: int) -> int:
    """What an ask is worth in full: its points, or for a per-item ask the points for each item found, to its limit, and
    never more than it can add. Points below 0 are worth nothing (docs/PRD.md 30.10)."""
    if not per_item:
        return max(0, points)
    if points <= 0:
        return 0
    total = points * count
    if limit is not None:
        total = min(total, limit)
    return min(total, reach_single(points, per_item, limit))


def _grade_reach(weights: GradeWeights, required: int, preferred: int) -> int:
    best_required = max(0, weights.required_proven, weights.required_transferable, weights.required_unproven)
    best_preferred = max(0, weights.preferred_met, weights.preferred_unproven)
    return required * best_required + preferred * best_preferred


def grade_points(weights: GradeWeights, required: Sequence[str], preferred: Sequence[str]) -> tuple[int, int, int]:
    """`(earned, required items counted, preferred items counted)` for these classes: the sum of each item's points by
    its class. An item whose class is not P, T or U is not counted."""
    by_required = {"P": weights.required_proven, "T": weights.required_transferable, "U": weights.required_unproven}
    by_preferred = {"P": weights.preferred_met, "T": weights.preferred_met, "U": weights.preferred_unproven}
    counted_required = [c for c in required if c in by_required]
    counted_preferred = [c for c in preferred if c in by_preferred]
    earned = sum(by_required[c] for c in counted_required) + sum(by_preferred[c] for c in counted_preferred)
    return earned, len(counted_required), len(counted_preferred)


# --- scoring the rows -----------------------------------------------------------------------------------


def _score_single(item: Single) -> ItemScore:
    reach = reach_single(item.points, item.per_item, item.limit)
    if item.answer in WEIGHT:
        full = _items_earned(item.points, item.per_item, item.limit, item.count)
        return ItemScore(item.id, reach, True, full * WEIGHT[item.answer])
    return ItemScore(item.id, reach, False, 0.0)


def _score_first(item: FirstMatch) -> ItemScore:
    reach = reach_group("best", 0, [lv.points for lv in item.levels])
    decided, held = first_true([lv.holds for lv in item.levels])
    if not decided:
        return ItemScore(item.id, reach, False, 0.0)
    return ItemScore(item.id, reach, True, float(max(0, item.levels[held].points)) if held is not None else 0.0, held)


def _score_any(item: AnyOf) -> ItemScore:
    reach = reach_group("any", item.points, ())
    known = [WEIGHT[a] for a in item.parts if a in WEIGHT]
    unknown = len(item.parts) - len(known)
    if MET in item.parts or not unknown:
        weight = max(known, default=0.0)
        return ItemScore(item.id, reach, True, max(0, item.points) * weight, weight=weight)
    return ItemScore(item.id, reach, False, 0.0)


def _score_graded(item: Graded) -> ItemScore:
    earned, required, preferred = grade_points(item.weights, item.required, item.preferred)
    return ItemScore(item.id, _grade_reach(item.weights, required, preferred), True, float(earned))


def _score_item(item: Item) -> ItemScore:
    if isinstance(item, Single):
        return _score_single(item)
    if isinstance(item, FirstMatch):
        return _score_first(item)
    if isinstance(item, AnyOf):
        return _score_any(item)
    return _score_graded(item)


def coverage(decided: int, possible: int) -> int:
    """The whole percent of the points that can be earned that are decided, half up; 0 when none can be."""
    if possible <= 0:
        return 0
    return (200 * decided + possible) // (2 * possible)


def percent(earned: float, against: int) -> int | None:
    """The share of `against` points that `earned` is, as a whole percent held between 0 and 100, rounded half up once,
    from the exact points (two decimals, so the arithmetic is in whole hundredths): 12.5 is 13, and 12.4 of 25 is 50.
    None when there are no points to take a share of. Negative points are 0."""
    if against <= 0:
        return None
    hundredths = round(earned * 100)
    if hundredths <= 0:
        return 0
    return min(100, (2 * hundredths + against) // (2 * against))


def half_up(x: float) -> int:
    """Whole points, half up (12.5 is 13)."""
    return math.floor(x + 0.5)


def score(items: Sequence[Item]) -> Scored:
    """A role's score over the rows that count for it. Only a met row earns its points (a met penalty applies); every
    other row earns nothing, and every row stays in what can be earned.

    percent = the share of what can be earned that the earned points are; None when nothing can be earned."""
    rows = tuple(_score_item(item) for item in items)
    possible = sum(r.reach for r in rows)
    decided = sum(r.reach for r in rows if r.decided)
    earned = round(sum(r.earned for r in rows), 2)
    return Scored(rows, possible, decided, earned, coverage(decided, possible), percent(earned, possible),
                  half_up(earned))
