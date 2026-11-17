"""The queue rule: which kept role to look at first.

The queue rule: fit first; at the same fit the fresher posting wins; and a little of the fit
can be made up by freshness, never a lot: a fresh posting (14 days old or newer) can outrank a stale one a few points of
percent higher (early applicants get read), but a gap of seven or more always wins outright, whatever the age.

The fit is the percent the owner sees (`scoring.percent_of`), never the raw points: points are over each role's own
decided points and each tab's own asks, so two roles' points are not comparable and their percents are.
The rule is one number: the percent, plus
`FRESH_BONUS_PERCENT` (6) when the posting is fresh. 6 is the share of the old scale of 25 that the rule's 1.5 points
were. It is over five and under seven, so a fresh 64 (70) beats a stale 69 (69), a stale 70 beats a fresh 63 (69), and
at equal percent the fresh one is ahead. Ties after that go to the younger posting; a posting whose age is unknown
counts as stale and sorts after a known age. A role with no percent (no score yet, or too little decided to give one)
comes after every role that has one, in the classifier's order.

The research queues (`analysis.picks`, and the last group of `catch_up.in_order`) put one thing before this rule: the
youngest posting first (`freshness.youngest_first`; the owner, 2026-10-13: getting in in the first 24 hours is worth
much), one it can't date last. Roles of the same age then go in the order above.
"""

from datetime import date

FRESH_DAYS = 14
#: What a fresh posting adds to the percent it is ordered by (the 1.5 points the rule once added on a scale of 25).
FRESH_BONUS_PERCENT = 6


def effective_fit(percent: int | None, age_days: int | None) -> float:
    fresh = age_days is not None and age_days <= FRESH_DAYS
    return (percent if percent is not None else -99) + (FRESH_BONUS_PERCENT if fresh else 0.0)


def queue_key(percent: int | None, age_days: int | None, prob: float | None = None) -> tuple[float, float, float]:
    """Sort ascending by this: best first, on the fit percent (`scoring.percent_of`). A role with no percent (no score
    yet, since it waits for its research: docs/PRD.md 39.20; or too little decided to give one) comes after every one
    that has one, in the classifier's order: the likeliest first (`prob`, the classifier's probability for the posting
    it was made from), those it never rated last, then the fresher posting. Freshness does not lift a role over the
    classifier's order, and the probability is read for a role with no percent only."""
    age = age_days if age_days is not None else 10**6
    if percent is None:
        return (-effective_fit(None, None), -(prob if prob is not None else -1.0), age)
    return (-effective_fit(percent, age_days), 0.0, age)


def age_of(posted: date | None, found: date | None, today: date) -> int | None:
    when = posted or found
    return max((today - when).days, 0) if when else None
