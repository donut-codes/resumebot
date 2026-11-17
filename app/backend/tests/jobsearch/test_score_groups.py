"""The arithmetic of a fit, as pure functions (docs/PRD.md 30.9): what each kind of row can add (its reach), what it
earns, which rows are decided, and the percent worked out once from the exact points. There is no partial credit (the
owner, 2026-10-14): a met ask earns its points and anything else earns nothing. No database, no clock, no randomness:
the same rows always give the same score. Made-up ids and round numbers only."""

import inspect

from app.jobsearch.fit import score_groups as g
from app.jobsearch.fit.score_groups import AnyOf, FirstMatch, Graded, GradeWeights, Level, Single


def single(ident="ask-a", points=2, answer="met", **more) -> Single:
    return Single(ident, points, answer, **more)


def first(holds, points=(4, 3, 2), ident="list-a") -> FirstMatch:
    """A first match with these levels worth `points`, each level known to hold (True), not hold (False) or unknown."""
    shown = points[:len(holds)]
    return FirstMatch(ident, tuple(Level(f"lvl-{i}", pts, hold)
                                   for i, (pts, hold) in enumerate(zip(shown, holds, strict=True))))


def anyof(parts, points=2, ident="box-a") -> AnyOf:
    return AnyOf(ident, points, tuple(parts))


def score(*items) -> g.Scored:
    return g.score(items)


def one(item, **more) -> g.ItemScore:
    return score(item, **more).items[0]


# --- a first match --------------------------------------------------------------------------------


def test_the_reach_of_a_first_match_is_its_largest_positive_level():
    assert g.reach_group("best", 0, [4, 3, 2]) == 4
    assert g.reach_group("best", 0, [2, 3, 4]) == 4, "a level worth more than the one above: the largest counts"
    assert g.reach_group("best", 0, [1, 0, -1, -2]) == 1
    assert g.reach_group("best", 0, [0, -1, -2]) == 0 and g.reach_group("best", 0, [-1, -3]) == 0


def test_a_first_match_earns_the_points_of_the_first_level_that_is_true_from_the_top():
    assert one(first([True, True, True])).earned == 4, "a lower level that is true as well changes nothing"
    assert one(first([False, True, True])).earned == 3
    assert one(first([False, False, True])).earned == 2
    assert one(first([False, False, True])).held == 2 and one(first([True, False, False])).held == 0


def test_a_first_match_with_no_level_true_is_decided_and_earns_nothing():
    got = one(first([False, False, False]))
    assert (got.decided, got.earned, got.held) == (True, 0, None)


def test_a_level_is_taken_at_its_points_and_a_negative_level_earns_nothing():
    # Edited 2026-10-27 (PRD 30.10): a level with negative points used to take them off (-1, -2); it earns 0 now.
    ladder = (1, 0, -1, -2)
    assert one(first([False, False, True, False], points=ladder)).earned == 0
    assert one(first([False, False, False, True], points=ladder)).earned == 0
    assert one(first([False, True, False, False], points=ladder)).earned == 0
    assert one(first([True, False, False, False], points=ladder)).earned == 1
    assert one(first([False] * 4, points=ladder)).reach == 1, "the ladder reaches its one bonus"


def test_a_first_match_stops_at_the_first_level_nobody_can_tell():
    assert g.first_true([None, True, True]) == (False, None), "the top level is unknown: a lower one cannot decide"
    assert g.first_true([False, None, True]) == (False, None)
    assert g.first_true([False, True, None]) == (True, 1), "what lies below the held level is not looked at"
    assert g.first_true([False, False, False]) == (True, None)
    assert g.first_true([None, None, None]) == (False, None)
    assert one(first([None, None, None])).decided is False


# --- an any of ------------------------------------------------------------------------------------


def test_an_any_of_earns_its_points_once_when_any_part_is_met():
    assert one(anyof(["met", "not"])).earned == 2
    assert one(anyof(["met", "met"])).earned == 2, "two met earn the row once, not twice"
    assert one(anyof(["not", "met", "not"])).earned == 2


def test_an_any_of_with_no_part_met_earns_nothing_whatever_else_it_holds():
    assert one(anyof(["partly", "not"])).earned == 0, "partly met is not met"
    assert one(anyof(["partly", "partly"])).earned == 0
    assert one(anyof(["not", "not"])).earned == 0
    assert one(anyof(["met", "partly"])).earned == 2, "one part met earns the row, at its full points"


def test_an_any_of_is_decided_when_a_part_is_met_or_no_part_is_unknown():
    assert one(anyof(["met", "unknown"])).decided is True
    assert one(anyof(["met", "unknown"])).earned == 2
    assert one(anyof(["unknown", "not"])).decided is False
    assert one(anyof(["unknown", "unknown"])).decided is False
    assert one(anyof(["partly", "unknown"])).decided is False, "the unknown one might be met"
    assert one(anyof(["not", "not"])).decided is True


def test_an_any_of_with_negative_points_earns_nothing_whatever_its_parts_say():
    # Edited 2026-10-27 (PRD 30.10): a met any-of with -2 used to take 2 off; it earns 0 now.
    assert one(anyof(["unknown", "not"], points=-2)).earned == 0
    assert one(anyof(["unknown", "unknown"], points=-2)).earned == 0
    assert one(anyof(["met", "unknown"], points=-2)).earned == 0
    assert one(anyof(["met", "unknown"], points=-2)).decided is True, "still decided: one part holds"
    assert one(anyof(["not", "not"], points=-2)).earned == 0


def test_the_reach_of_an_any_of_is_its_points_when_they_are_positive():
    assert g.reach_group("any", 2, [0, 0]) == 2
    assert g.reach_group("any", -2, [0, 0]) == 0 and g.reach_group("any", 0, [0, 0]) == 0


# --- an ask on its own ----------------------------------------------------------------------------


def test_an_ask_earns_its_points_when_met_and_nothing_otherwise():
    assert one(single(answer="met")).earned == 2
    assert one(single(answer="partly")).earned == 0
    assert one(single(answer="not")).earned == 0
    assert one(single(answer="unknown")).earned == 0
    # Edited 2026-10-27 (PRD 30.10): a met ask with -3 used to earn -3; it earns 0 now.
    assert one(single(points=-3, answer="met")).earned == 0
    assert one(single(points=-3, answer="partly")).earned == 0


def test_a_negative_ask_never_counts_toward_what_can_be_earned_or_what_is_decided_and_takes_nothing_off():
    # Edited 2026-10-27 (PRD 30.10): the met -3 used to bring the earned points from 4 to 1; they stay 4.
    got = score(single("bonus", 4, "met"), single("penalty", -3, "met"), single("note", 0, "met"))
    assert (got.possible, got.decided, got.earned) == (4, 4, 4)
    assert [i.reach for i in got.items] == [4, 0, 0]
    assert g.reach_single(-3) == 0 and g.reach_single(0) == 0


def test_an_ask_counted_for_each_item_earns_each_item_to_its_limit():
    # Edited 2026-10-27 (PRD 30.10): the negative per-item ask (-3, -2 off) earns 0 now.
    each = single("gaps", -1, "met", per_item=True, limit=-3, count=5)
    assert one(each).earned == 0, "a negative per-item ask earns nothing"
    assert one(single("gaps", -1, "met", per_item=True, limit=-3, count=2)).earned == 0
    assert one(single("bonus", 1, "met", per_item=True, limit=3, count=9)).earned == 3
    assert one(single("bonus", 1, "partly", per_item=True, limit=4, count=9)).earned == 0
    assert one(single("bonus", 1, "met", per_item=True, limit=4, count=9)).reach == 4, "up to its limit"


def test_a_per_item_bonus_with_no_limit_counts_its_points_once_and_earns_no_more_than_it_counts():
    trap = single("bonus", 2, "met", per_item=True, limit=None, count=7)
    got = one(trap)
    assert (got.reach, got.earned) == (2, 2), "it earned 14 of a possible 2 before"
    assert score(trap).percent == 100


# --- the points a role can earn, decided and earned -----------------------------------------------


def test_what_a_role_can_earn_counts_each_row_once_at_its_top_value():
    got = score(first([False, True, False]), anyof(["met", "not"], points=2), single("a", 3, "met"),
                single("pen", -5, "met"))
    assert got.possible == 4 + 2 + 3, "the top level, the any-of's points, the ask; the negative one is outside"


def test_the_percent_is_the_earned_points_over_the_points_that_can_be_earned_worked_out_once():
    # 7 points can be earned and 2 are: 29%. The 3 nobody could tell stay in the 7.
    got = score(single("a", 2, "met"), single("b", 2, "not"), single("c", 3, "unknown"))
    assert (got.possible, got.decided, got.earned, got.percent) == (7, 4, 2, 29)


def test_the_percent_rounds_half_up_once_from_the_exact_points():
    assert score(single("a", 8, "not"), single("b", 8, "unknown")).percent == 0
    assert score(single("a", 8, "met"), single("b", 8, "unknown")).percent == 50
    assert score(single("a", 8, "partly")).percent == 0
    # 1 of 8 is 12.5%, which is 13.
    assert g.percent(1.0, 8) == 13
    assert g.percent(12.4, 25) == 50, "from the exact points: the whole-point fit (12) would have said 48"
    assert g.percent(12.6, 25) == 50 and g.percent(12.5, 25) == 50
    assert g.percent(12.375, 25) == 50 and g.percent(0.37, 100) == 0 and g.percent(0.5, 100) == 1
    assert g.percent(7.0, 7) == 100


def test_a_negative_total_is_no_percent_below_zero():
    assert g.percent(-3.0, 8) == 0
    assert score(single("a", 4, "unknown"), single("pen", -6, "met")).percent == 0, "nothing earned, and no minus 6"
    assert score(single("a", 4, "unknown"), single("pen", -6, "met")).earned == 0


def test_a_percent_never_passes_a_hundred():
    assert g.percent(9.0, 4) == 100


def test_no_percent_only_when_nothing_can_be_earned():
    assert score(single("pen", -2, "met")).percent is None, "no points can be earned: there is no share of nothing"
    assert score().percent is None and score().total == 0
    assert g.percent(3.0, 0) is None


def test_coverage_is_the_whole_percent_of_the_points_decided():
    got = score(single("a", 4, "met"), single("b", 3, "unknown"), single("c", 4, "not"))
    assert (got.decided, got.possible, got.coverage) == (8, 11, 73)
    assert g.coverage(11, 18) == 61 and g.coverage(0, 0) == 0 and g.coverage(18, 18) == 100
    assert g.coverage(1, 8) == 13, "half up, as the percent is"


def test_the_whole_point_fit_is_the_earned_points_rounded_half_up():
    assert g.half_up(2.5) == 3 and g.half_up(2.4) == 2 and g.half_up(-1.5) == -1, "-1.5 rounds up, as it always has"
    assert score(single("a", 5, "met"), single("pen", -3, "met")).total == 5, "edited 2026-10-27 (PRD 30.10): was 2"
    assert score(single("a", 5, "partly")).total == 0, "no half points"


# --- no partial credit (the owner, 2026-10-14) ---------------------------------------------------


def test_met_earns_the_full_points_of_every_kind_of_row():
    assert one(single("a", 4, "met")).earned == 4
    assert one(single("a", 4, "met", per_item=True, limit=3, count=9)).earned == 3
    assert one(first([False, True, False])).earned == 3, "the held level's points, whole"
    assert one(anyof(["not", "met"], points=4)).earned == 4


def test_not_met_partly_unknown_and_unanswered_earn_nothing_in_every_kind_of_row():
    for answer in ("not", "partly", "unknown", "anything else", ""):
        assert one(single("a", 4, answer)).earned == 0, answer
        assert one(anyof([answer], points=4)).earned == 0, answer
    assert one(first([None, None, None])).earned == 0, "a first match nobody can tell earns nothing"
    assert one(first([False, False, False])).earned == 0
    assert one(Single("a", 4)).earned == 0, "an ask with no answer at all is unknown"


def test_a_negative_line_earns_nothing_met_or_not():
    # Edited 2026-10-27 (PRD 30.10): a met penalty used to apply (-4, -4, -2); every one earns 0 now.
    assert one(single("pen", -4, "met")).earned == 0
    for answer in ("not", "partly", "unknown"):
        assert one(single("pen", -4, answer)).earned == 0, answer
        assert one(anyof([answer], points=-4)).earned == 0, answer
    assert one(anyof(["met", "not"], points=-4)).earned == 0
    ladder = (1, -1, -2)
    assert one(first([False, False, None], points=ladder)).earned == 0, "a level nobody can tell does not apply"
    assert one(first([False, False, True], points=ladder)).earned == 0, "a negative level that holds earns nothing"
    assert one(first([True, False, False], points=ladder)).earned == 1, "a positive level still earns its points"


def test_the_earned_points_of_asks_never_go_below_zero():
    got = score(single("a", 4, "not"), single("pen", -6, "met"), anyof(["met"], points=-3),
                first([True, False], points=(-2, 3)))
    assert got.earned == 0 and got.total == 0 and got.percent == 0


def test_what_can_be_earned_is_the_same_whatever_the_answers_are():
    possible = set()
    for a in ("met", "partly", "not", "unknown"):
        for b in ("met", "unknown"):
            rows = (single("a", 5, a), single("b", 3, b), first([None, None]), anyof([a], points=2))
            possible.add(score(*rows).possible)
    assert possible == {5 + 3 + 4 + 2}, "unknown answers stay in what can be earned"


def test_a_role_with_every_ask_unknown_scores_zero_from_its_asks():
    got = score(single("a", 5, "unknown"), Single("b", 3), first([None, None]), anyof(["unknown", "unknown"], points=2),
                single("pen", -4, "unknown"))
    assert (got.earned, got.percent, got.total) == (0, 0, 0)
    assert got.possible == 5 + 3 + 4 + 2 and got.decided == 0 and got.coverage == 0


def test_a_role_whose_asks_have_not_been_checked_scores_below_one_whose_asks_have():
    unchecked = score(single("a", 5, "unknown"), single("b", 5, "unknown"))
    half_checked = score(single("a", 5, "met"), single("b", 5, "unknown"))
    checked = score(single("a", 5, "met"), single("b", 5, "met"))
    assert (unchecked.percent, half_checked.percent, checked.percent) == (0, 50, 100)


def test_there_is_no_credit_mode_no_decided_only_mode_and_no_probability_to_score_by():
    assert not hasattr(g, "CREDIT") and not hasattr(g, "DECIDED") and not hasattr(g, "POLICIES")
    assert list(inspect.signature(g.score).parameters) == ["items"], "the rows alone decide the score"
    assert g.WEIGHT == {"met": 1.0, "partly": 0.0, "not": 0.0}, "a stored partly is read, and is worth nothing"


# --- the percent and the coverage -----------------------------------------------------------------


def test_the_percent_is_over_what_can_be_earned_whether_or_not_it_is_decided():
    got = score(single("a", 5, "met"), single("b", 5, "unknown"), first([None, None]))
    assert (got.possible, got.decided, got.earned, got.percent, got.coverage) == (14, 5, 5, 36, 36)


def test_coverage_says_how_much_of_what_can_be_earned_has_an_answer_and_changes_no_score():
    half = score(single("a", 5, "met"), single("b", 5, "unknown"))
    assert (half.percent, half.coverage) == (50, 50)
    assert score(single("a", 5, "not"), single("b", 5, "unknown")).coverage == 50


# --- the same rows always give the same score -----------------------------------------------------


def test_the_same_rows_always_give_the_same_score():
    rows = (single("a", 4, "partly"), first([False, None, True]), anyof(["partly", "unknown"], points=3))
    assert score(*rows) == score(*rows)


def test_the_rows_in_a_score_are_in_the_order_given_with_each_ones_reach_and_decided_state():
    got = score(single("a", 2, "met"), first([False, True, False], ident="list-a"), anyof(["unknown"], ident="box-a"))
    assert [(i.id, i.reach, i.decided) for i in got.items] == [("a", 2, True), ("list-a", 4, True), ("box-a", 2, False)]


# --- the requirements the app grades from the analyst's classes -----------------------------------


WEIGHTS = GradeWeights()


def graded(required="", preferred="", weights=WEIGHTS) -> Graded:
    return Graded("grade:requirements", tuple(required), tuple(preferred), weights)


def test_the_owners_weights_are_the_defaults():
    assert (WEIGHTS.required_proven, WEIGHTS.required_transferable, WEIGHTS.required_unproven) == (2, 0, -2)
    assert (WEIGHTS.preferred_met, WEIGHTS.preferred_unproven) == (1, 0)


def test_a_graded_row_earns_the_sum_of_its_items_by_class():
    got = one(graded("PPTU", "PTU"))
    assert got.earned == 2 + 2 + 0 - 2 + 1 + 1 + 0, "required P P T U; preferred P T U"
    assert one(graded("PPPP")).earned == 8 and one(graded("UUUU")).earned == -8
    assert one(graded("", "PPT")).earned == 3 and one(graded("", "UU")).earned == 0


def test_a_graded_row_can_add_what_the_role_itself_can_reach_and_no_more():
    """The row's share of what can be earned is the role's own maximum: required count x the proven weight, plus
    preferred count x the preferred weight. Unproven required points are a penalty and stay out of it."""
    assert one(graded("PPTU", "PTU")).reach == 4 * 2 + 3 * 1
    assert one(graded("PPPPPP", "")).reach == 12 and one(graded("PP", "PPPP")).reach == 2 * 2 + 4 * 1
    assert one(graded("UUUU")).reach == 8, "every item counts toward the most it could earn, not only the good ones"
    assert one(graded("", "")).reach == 0 and one(graded()).earned == 0


def test_the_unproven_penalty_lowers_the_earned_points_and_never_the_points_that_can_be_earned():
    clean, dragged = score(graded("PPPP")), score(graded("PPPU"))
    assert clean.possible == dragged.possible == 8
    assert (clean.earned, dragged.earned) == (8, 4)
    assert (clean.percent, dragged.percent) == (100, 50)
    weight = GradeWeights(required_unproven=-5)
    assert score(graded("PPPU", weights=weight)).earned == 1 and score(graded("PPPU", weights=weight)).possible == 8


def test_a_role_that_is_half_proven_and_half_unproven_nets_zero_from_its_required_items():
    assert one(graded("PPUU")).earned == 0
    assert one(graded("PPPUUU")).earned == 0
    assert one(graded("PPTTUU")).earned == 0, "transferable counts nothing: two proven pay for two unproven"
    assert score(graded("PPUU")).percent == 0


def test_a_strong_fit_scores_higher_than_a_weak_one():
    strong, weak = score(graded("PPPPT", "PPT")), score(graded("PUUTU", "UUT"))
    assert strong.earned == 11 and weak.earned == -3, "8 + 3, and -4 + 1"
    assert strong.percent == 85 and weak.percent == 0


def test_each_role_is_a_share_of_its_own_maximum_so_roles_with_different_lists_compare():
    short, long = score(graded("PP")), score(graded("PPPPPPPP"))
    assert (short.possible, long.possible) == (4, 16)
    assert short.percent == long.percent == 100


def test_a_class_nobody_gave_is_not_counted_either_way():
    got = one(graded("PX?U", "T-"))
    assert got.earned == 2 - 2 + 1 and got.reach == 2 * 2 + 1, "two required items and one preferred item had a class"


def test_the_graded_row_is_always_decided_and_scores_by_its_classes_alone():
    got = score(graded("PU"))
    assert got.items[0].decided is True and got.coverage == 100
    assert got.earned == 0 and got.possible == 4, "its classes grade it, per class (rule 13), as they did before"


def test_the_owners_own_weights_are_used():
    weights = GradeWeights(required_proven=3, required_transferable=1, required_unproven=-1, preferred_met=2,
                           preferred_unproven=-1)
    got = one(graded("PTU", "TU", weights))
    assert got.earned == 3 + 1 - 1 + 2 - 1 and got.reach == 3 * 3 + 2 * 2
    assert one(graded("PTU", "", GradeWeights(required_transferable=5))).reach == 3 * 5, (
        "a class worth more than proven is the most an item can earn")
