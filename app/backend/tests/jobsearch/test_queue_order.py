"""The queue rule on the fit percent (the prioritization groups spec, "The queue effect"): `queue_key` reads
the percent the owner sees, a fresh posting gains six points of it, and a role with no percent goes after every scored
one in the classifier's order. Made-up numbers only."""

from app.jobsearch.fit import role_order as queue
from app.jobsearch.fit.role_order import effective_fit, queue_key


def test_a_fresh_posting_gains_six_points_of_percent_and_a_stale_or_undated_one_gains_none():
    assert queue.FRESH_BONUS_PERCENT == 6
    assert effective_fit(70, 14) == 76.0 and effective_fit(70, 0) == 76.0
    assert effective_fit(70, 15) == 70.0 and effective_fit(70, None) == 70.0
    assert effective_fit(None, None) == -99.0, "no percent is under every percent a role can have"


def test_a_fresh_role_makes_up_six_points_of_percent_and_a_gap_of_seven_or_more_always_wins():
    assert queue_key(64, 14) < queue_key(69, 15), "fresh 64 (70) beats stale 69"
    assert queue_key(70, 90) < queue_key(63, 0), "stale 70 beats fresh 63 (69)"
    assert queue_key(77, 90) < queue_key(70, 0), "a gap of seven always wins, whatever the age"
    assert queue_key(76, 90) > queue_key(70, 0), "a gap of six is made up by a fresh posting, which wins the tie on age"


def test_at_one_percent_the_younger_posting_goes_first_and_an_unknown_age_goes_last():
    assert queue_key(40, 2) < queue_key(40, 9) < queue_key(40, 20) < queue_key(40, 40)
    assert queue_key(40, 30) < queue_key(40, None)
    assert queue_key(60, 3) < queue_key(50, 1) and queue_key(60, 40) < queue_key(50, 20)


def test_a_role_with_no_percent_goes_after_every_scored_one_in_the_classifiers_order():
    scored_worst_stale = queue_key(0, None)
    assert scored_worst_stale < queue_key(None, 0, 0.99), "freshness and a likely posting do not lift an unscored role"
    assert queue_key(None, 5, 0.9) < queue_key(None, 5, 0.4) < queue_key(None, 5, None), "likeliest first, unrated last"
    assert queue_key(None, 2, 0.5) < queue_key(None, 9, 0.5), "then the fresher posting"
