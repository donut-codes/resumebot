"""Count limits (docs/PRD.md 25.10 and 25.12): the most a mission does in a night or in a run.

Every one takes -1, which means no limit, or a number in its own range, and nothing else below 0. A limit is stored as
the owner set it, so a reader of the setting sees -1; `unlimited` and `left` say what it means. The limits are, by
mission:

- Scout the boards: the number of matches, the most searches a night, the sites to learn a night;
- Read the employers' boards: the descriptions read a night;
- Read in your browser: the postings handed in a run, the postings opened for their whole description;
- Analyse the finds: the roles researched; Research employers: the employers researched (docs/PRD.md 36.1).

Not limits, and so never -1: batch sizes ("at a time"), thresholds and bars (a number from 0 to 1, a percent), times of
day, how long before a run counts as stuck or an employer is researched again (days), and the politeness of a fetch
(the pace, the robots rules).
"""

#: What a limit is saved as when there is none.
NO_LIMIT = -1
#: How many are "left" of a limit that has none: more than any list can hold, so a `min` with a count is the count.
PLENTY = 10**9


def unlimited(limit: float | None) -> bool:
    """Whether a limit as stored means no limit (any negative number; only -1 is ever saved)."""
    return limit is not None and limit < 0


def left(limit: int, used: int) -> int:
    """How many more are allowed under `limit` once `used` are gone (`PLENTY` when there is no limit)."""
    return PLENTY if unlimited(limit) else max(limit - used, 0)


def upto(items: list, limit: int) -> list:
    """The first `limit` of `items`, all of them when there is no limit."""
    return items if unlimited(limit) else items[:limit]
