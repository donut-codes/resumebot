"""A role's fit over the owner's rows (docs/PRD.md 30.9): `scoring.compute` judges each ask, "any of" row and "first
match" row, and the graded requirements, from the answers kept on the role and from what the app knows, and the pure
arithmetic (`score_groups`) adds them up. There is no partial credit (the owner, 2026-10-14): a met ask earns its points
and every other answer earns nothing. Made-up asks, ids and round numbers only."""

from types import SimpleNamespace

import pytest

from app.jobsearch.fit import comp, scoring
from app.jobsearch.profile import schema as p
from app.jobsearch.research import prioritize
from tests.support import GRADED_ON


def ask(ident: str, points: int = 0, says: str | None = None, **more) -> dict:
    return {"id": ident, "says": says or f"Statement {ident}", "points": points, **more}


def first_match(ident: str, levels, **more) -> dict:
    """A first match: `(id, points)` for each level, best first, or `(id, points, {more})` for one the app decides."""
    members = [ask(i, pts, **(rest[0] if rest else {})) for i, pts, *rest in levels]
    return {"id": ident, "kind": "best", "members": members, **more}


def any_of(ident: str, points: int, parts, **more) -> dict:
    return {"id": ident, "kind": "any", "points": points,
            "members": [ask(i, **(rest[0] if rest else {})) for i, *rest in parts], **more}


def profile_of(asks=(), groups=(), roles=(), **more) -> p.Profile:
    return p.Profile.model_validate({
        "criteria": {"target_roles": [{"id": r, "name": r.title()} for r in roles]},
        "scoring": {"asks": list(asks), "groups": list(groups), **more}})


def said(**answers) -> dict:
    """A role's stored answers: `ask_a="met"` answers that ask, a tuple `("lvl-b",)` answers a first match."""
    out = []
    for ident, given in answers.items():
        ident = ident.replace("_", "-")
        if isinstance(given, tuple):  # a first match: the id of its first true statement, none, or unknown
            out.append({"id": ident, "answer": {"none": "not", "unknown": "unknown"}.get(given[0], "met"),
                        "level": given[0], "evidence": "made-up evidence"})
        else:
            out.append({"id": ident, "answer": given, "evidence": "made-up evidence"})
    return {"answers": out}


def work(profile: p.Profile, answers: dict, **more) -> dict:
    scored = scoring.compute(answers, profile, **more)
    assert scored is not None
    return scored


def row(scored: dict, ident: str) -> dict:
    return next(r for r in scored["asks"] if r["id"] == ident)


# --- a worked example, with made-up asks ------------------------------------------------


LADDER_A = [("a-core", 4), ("a-real", 3), ("a-thin", 2)]
LADDER_B = [("b-small", 2), ("b-medium", 1)]
LADDER_C = [("c-lead", 2), ("c-other", 1)]
OTHERS = [("i1", 3), ("i2", 3), ("i3", 2), ("i4", 2)]


def ungrouped_profile() -> p.Profile:
    return profile_of(asks=[ask(i, pts) for i, pts in [*LADDER_A, *LADDER_B, *LADDER_C, *OTHERS]])


def grouped_profile() -> p.Profile:
    return profile_of(asks=[ask(i, pts) for i, pts in OTHERS],
                      groups=[first_match("list-a", LADDER_A), first_match("list-b", LADDER_B),
                              first_match("list-c", LADDER_C)])


ONE_BY_ONE = said(a_core="not", a_real="met", a_thin="met", b_small="unknown", b_medium="met", c_lead="unknown",
                  c_other="unknown", i1="met", i2="unknown", i3="partly", i4="unknown")
AS_PICKS = said(list_a=("a-real",), list_b=("b-medium",), list_c=("unknown",), i1="met", i2="unknown", i3="partly",
                i4="unknown")


def test_asks_that_are_a_ladder_and_answered_one_by_one_earn_only_what_is_met():
    scored = work(ungrouped_profile(), ONE_BY_ONE)
    assert (scored["earned"], scored["possible"], scored["percent"]) == (9.0, 25, 36)
    assert scored["total"] == 9, "a-real 3, a-thin 2, b-medium 1 and i1 3: the unknown and the partly met earn nothing"


def test_the_same_asks_as_first_matches_earn_only_what_is_met_and_keep_what_nobody_could_tell_in_the_most():
    scored = work(grouped_profile(), AS_PICKS)
    assert (scored["earned"], scored["possible"], scored["percent"]) == (7.0, 18, 39)
    assert (scored["decided"], scored["coverage"]) == (11, 61)


def test_a_perfect_role_reaches_a_hundred_percent_with_the_ladders_grouped_and_76_with_them_apart():
    others = [("j1", 5), ("j2", 4), ("j3", 4)]
    apart = profile_of(asks=[ask(i, pts) for i, pts in [*LADDER_A, *LADDER_B, *others]])
    together = profile_of(asks=[ask(i, pts) for i, pts in others],
                          groups=[first_match("list-a", LADDER_A), first_match("list-b", LADDER_B)])
    truthful = said(a_core="met", a_real="not", a_thin="not", b_small="met", b_medium="not", j1="met", j2="met",
                    j3="met")
    perfect = said(list_a=("a-core",), list_b=("b-small",), j1="met", j2="met", j3="met")
    one, two = work(apart, truthful), work(together, perfect)
    assert (one["possible"], one["earned"], one["percent"]) == (25, 19.0, 76)
    assert (two["possible"], two["earned"], two["percent"]) == (19, 19.0, 100)


def test_the_same_inputs_always_give_the_same_breakdown():
    assert work(grouped_profile(), AS_PICKS) == work(grouped_profile(), AS_PICKS)


# --- what a score keeps ---------------------------------------------------------------------------


def test_a_score_keeps_what_it_was_worked_out_from():
    scored = work(grouped_profile(), AS_PICKS, version=13)
    assert list(scored) == ["asks", "earned", "decided", "possible", "coverage", "percent", "total", "version", "math"]
    assert (scored["version"], scored["math"]) == (13, 4)
    assert scoring.MATH == 4, "the card reads the preferred classes: a score kept under 3 is worked out again"
    assert "version" not in work(grouped_profile(), AS_PICKS)


def test_every_row_says_whether_it_was_decided_and_what_it_earned():
    scored = work(grouped_profile(), AS_PICKS)
    assert [(r["id"], r["decided"], r["earned"]) for r in scored["asks"]] == [
        ("i1", True, 3), ("i2", False, 0), ("i3", True, 0), ("i4", False, 0),
        ("list-a", True, 3), ("list-b", True, 1), ("list-c", False, 0)]


# --- a first match --------------------------------------------------------------------------------


def test_a_first_match_row_shows_the_level_that_held_and_every_level():
    scored = work(grouped_profile(), AS_PICKS)
    got = row(scored, "list-a")
    assert (got["kind"], got["answer"], got["level"], got["points"], got["says"]) == (
        "best", "met", "a-real", 3, "Statement a-real")
    assert [(lv["id"], lv["points"]) for lv in got["levels"]] == [("a-core", 4), ("a-real", 3), ("a-thin", 2)]
    assert (got["by"], got["evidence"]) == ("reader", "made-up evidence")


def test_a_first_match_with_no_level_true_is_decided_and_earns_nothing():
    got = row(work(grouped_profile(), said(a_core="not", a_real="unknown", a_thin="not")), "list-a")
    assert (got["answer"], got["level"], got["decided"], got["earned"]) == ("not", "none", True, 0)
    assert (got["says"], got["points"]) == ("Statement a-core", 4), "the top level stands for a list that held none"


def test_a_first_match_nobody_has_answered_is_undecided_and_says_so():
    got = row(work(grouped_profile(), said(i1="met")), "list-a")
    assert (got["answer"], got["level"], got["decided"]) == ("unknown", "unknown", False)
    assert got["evidence"] == scoring.NOT_ANSWERED


def test_a_first_match_answered_as_unknown_or_with_a_level_it_does_not_have_is_undecided():
    for pick in ("unknown", "lvl-nobody-has"):
        got = row(work(grouped_profile(), said(list_a=(pick,))), "list-a")
        assert (got["decided"], got["level"]) == (False, "unknown"), pick


def test_a_first_match_answered_in_other_words_counts_as_not_answered():
    profile = grouped_profile()
    stale = {"answers": [{"id": "list-a", "answer": "met", "level": "a-core", "says": "an earlier list of levels"}]}
    assert row(work(profile, stale), "list-a")["decided"] is False
    answered_now = {"answers": [{"id": "list-a", "answer": "met", "level": "a-core",
                                 "says": scoring._list_words(profile.scoring.groups[0])}]}
    assert row(work(profile, answered_now), "list-a")["level"] == "a-core"


def test_a_ladder_that_crosses_zero_earns_nothing_for_a_negative_level():
    ladder = [("up", 1), ("same", 0), ("down-one", -1), ("down-two", -2)]
    profile = profile_of(groups=[first_match("title", ladder)])
    # Edited 2026-10-27 (PRD 30.10): the held down-one and down-two levels earned -1 and -2; they earn 0.
    for pick, earned in (("up", 1), ("same", 0), ("down-one", 0), ("down-two", 0)):
        scored = work(profile, said(title=(pick,)))
        assert (scored["earned"], scored["possible"]) == (earned, 1), pick
    assert work(profile, said(title=("down-two",)))["percent"] == 0


# --- an any of ------------------------------------------------------------------------------------


def any_profile() -> p.Profile:
    return profile_of(groups=[any_of("box", 4, [("part-a",), ("part-b",), ("part-c",)])], asks=[ask("solo", 2)])


def test_an_any_of_earns_its_points_once_when_any_statement_is_met():
    scored = work(any_profile(), said(part_a="not", part_b="met", part_c="met", solo="met"))
    assert (row(scored, "box")["earned"], scored["earned"], scored["possible"]) == (4, 6.0, 6)
    got = row(scored, "box")
    assert (got["kind"], got["answer"], got["points"], got["decided"]) == ("any", "met", 4, True)
    assert [(x["id"], x["answer"]) for x in got["parts"]] == [("part-a", "not"), ("part-b", "met"), ("part-c", "met")]


def test_an_any_of_with_a_part_met_is_decided_though_another_is_unknown():
    got = row(work(any_profile(), said(part_a="met", solo="met")), "box")
    assert (got["decided"], got["earned"]) == (True, 4)


def test_an_any_of_with_no_part_met_and_one_unknown_is_undecided_and_earns_nothing():
    got = row(work(any_profile(), said(part_a="not", solo="met")), "box")
    assert (got["decided"], got["answer"], got["earned"]) == (False, "unknown", 0.0)
    assert work(any_profile(), said(part_a="not", solo="met"))["possible"] == 6, "it stays in what can be earned"


def test_an_any_of_whose_parts_are_only_partly_met_earns_nothing_and_is_not_met():
    got = row(work(any_profile(), said(part_a="partly", part_b="not", part_c="partly")), "box")
    assert (got["decided"], got["answer"], got["earned"]) == (True, "not", 0)


def test_an_any_of_with_every_part_not_met_is_decided_and_earns_nothing():
    got = row(work(any_profile(), said(part_a="not", part_b="not", part_c="not")), "box")
    assert (got["decided"], got["answer"], got["earned"]) == (True, "not", 0)


def test_the_parts_of_an_any_of_are_decided_as_asks_are_the_apps_own_answer_over_the_readers():
    box = any_of("box", 3, [("young", {"checked_by": "app", "fact": "years_since_founded", "op": "<", "value": 5}),
                            ("small", {"checked_by": "app", "fact": "employees", "op": "<", "value": 100}),
                            ("mission",)])
    profile = profile_of(groups=[box])
    got = row(work(profile, said(mission="not"), employer_facts={"years_since_founded": 3}), "box")
    assert (got["answer"], got["decided"], got["earned"]) == ("met", True, 3), "the app settled it"
    assert [(x["id"], x["answer"], x["by"]) for x in got["parts"]] == [
        ("young", "met", "app"), ("small", "unknown", "app"), ("mission", "not", "reader")]
    assert got["by"] == "reader", "a reader answered one part"
    waiting = row(work(profile, said(mission="not"), employer_facts={"employees": 900}), "box")
    assert (waiting["decided"], waiting["parts"][1]["answer"]) == (False, "not"), "young is still unknown"
    app_only = profile_of(groups=[any_of("box", 2, [
        ("young", {"checked_by": "app", "fact": "years_since_founded", "op": "<", "value": 5}),
        ("small", {"checked_by": "app", "fact": "employees", "op": "<", "value": 100})])])
    assert row(work(app_only, said(), employer_facts={"employees": 40}), "box")["by"] == "app"


# --- a first match the app judges -----------------------------------------------------------------


def sizes() -> p.Profile:
    levels = [("small", 2, {"checked_by": "app", "fact": "employees", "op": "<", "value": 50}),
              ("medium", 1, {"checked_by": "app", "fact": "employees", "op": "<", "value": 500})]
    return profile_of(groups=[first_match("size", levels)])


@pytest.mark.parametrize(("employees", "level", "earned"), [(40, "small", 2), (300, "medium", 1), (900, "none", 0)])
def test_a_first_match_the_app_judges_takes_the_first_level_from_the_top_that_holds(employees, level, earned):
    got = row(work(sizes(), said(), employer_facts={"employees": employees}), "size")
    assert (got["level"], got["earned"], got["decided"], got["by"]) == (level, earned, True, "app")


def test_a_first_match_the_app_judges_waits_for_a_fact_the_record_does_not_hold_yet():
    got = row(work(sizes(), said(), employer_facts={}), "size")
    assert (got["decided"], got["level"], got["by"]) == (False, "unknown", "app")
    assert got["evidence"] == "the app decides this once it has the facts"
    waiting = work(sizes(), said(), employer_facts={})
    assert (waiting["earned"], waiting["possible"]) == (0, 2), "it earns nothing and stays in what can be earned"


def test_a_first_match_of_statements_with_a_fact_is_the_apps_without_saying_checked_by():
    levels = [("small", 2, {"fact": "employees", "op": "<", "value": 100}),
              ("medium", 1, {"fact": "employees", "op": "<", "value": 2500})]
    profile = profile_of(groups=[first_match("size", levels)])
    assert profile.scoring.groups[0].by_the_app() is True
    for employees, level, earned in [(40, "small", 2), (500, "medium", 1), (9000, "none", 0)]:
        got = row(work(profile, said(), employer_facts={"employees": employees}), "size")
        assert (got["level"], got["earned"], got["by"]) == (level, earned, "app")
    missing = row(work(profile, said(), employer_facts={}), "size")
    assert (missing["decided"], missing["level"], missing["earned"]) == (False, "unknown", 0)


def test_a_first_match_the_app_judges_is_never_answered_by_an_agent():
    got = row(work(sizes(), said(size=("small",)), employer_facts={"employees": 900}), "size")
    assert (got["level"], got["earned"]) == ("none", 0), "an answer to it is set aside: the record decides"


def test_a_first_match_can_be_decided_from_the_analysts_classes():
    levels = [("all-proven", 3, {"checked_by": "app", "from_classes": "all_P"}),
              ("some-transferable", 1, {"checked_by": "app", "from_classes": "T"})]
    profile = profile_of(groups=[first_match("fit", levels)])
    assert row(work(profile, said(), classes=["P", "P"]), "fit")["level"] == "all-proven"
    assert row(work(profile, said(), classes=["P", "T"]), "fit")["level"] == "some-transferable"
    assert row(work(profile, said(), classes=["P", "U"]), "fit")["level"] == "none"
    assert row(work(profile, said(), classes=None), "fit")["decided"] is False, "not researched yet"


# --- no partial credit (the owner, 2026-10-14) ---------------------------------------------------


def credit_profile() -> p.Profile:
    return profile_of(asks=[ask("bonus", 4), ask("penalty", -3), ask("each", 1, per_item=True, limit=3)],
                      groups=[first_match("list", [("x", 2), ("y", 1)]), any_of("box", 5, [("p1",), ("p2",)])])


def test_met_earns_the_full_points_of_each_kind_of_row():
    scored = work(credit_profile(), {"answers": [
        {"id": "bonus", "answer": "met"}, {"id": "each", "answer": "met", "count": 9},
        {"id": "list", "answer": "met", "level": "y"}, {"id": "p2", "answer": "met"}]})
    assert [(r["id"], r["earned"]) for r in scored["asks"]] == [
        ("bonus", 4), ("penalty", 0), ("each", 3), ("list", 1), ("box", 5)]
    assert scored["earned"] == 13.0


def test_a_met_negative_ask_earns_nothing_whatever_it_answers():
    # Edited 2026-10-27 (PRD 30.10): a met -3 ask took 3 off (earned 1.0 of the bonus 4); the earned points stay 4.0.
    profile = profile_of(asks=[ask("bonus", 4), ask("penalty", -3)])
    assert work(profile, said(bonus="met", penalty="met"))["earned"] == 4.0
    for given in ("not", "partly", "unknown"):
        assert work(profile, said(bonus="met", penalty=given))["earned"] == 4.0, given
    assert work(profile, said(bonus="met"))["earned"] == 4.0, "unanswered"


def test_not_met_partly_met_unknown_and_unanswered_earn_nothing_in_every_kind_of_row():
    for given in ("not", "partly", "unknown"):
        scored = work(credit_profile(), {"answers": [
            {"id": "bonus", "answer": given}, {"id": "each", "answer": given, "count": 9},
            {"id": "list", "answer": given, "level": "none" if given == "not" else "unknown"},
            {"id": "p1", "answer": given}, {"id": "p2", "answer": given}]})
        assert scored["earned"] == 0 and scored["total"] == 0, given
    assert work(credit_profile(), said())["earned"] == 0, "nothing answered"


def test_a_stored_partly_is_still_read_and_earns_nothing():
    scored = work(profile_of(asks=[ask("a", 4), ask("pen", -3)]), said(a="partly", pen="partly"))
    assert [(r["answer"], r["earned"], r["decided"]) for r in scored["asks"]] == [("partly", 0, True)] * 2
    assert scored["earned"] == 0 and scored["percent"] == 0


def test_what_can_be_earned_does_not_depend_on_the_answers():
    possible = set()
    for given in ("met", "partly", "not", "unknown"):
        possible.add(work(credit_profile(), said(bonus=given, each=given, p1=given))["possible"])
    possible.add(work(credit_profile(), said())["possible"])
    assert possible == {4 + 3 + 2 + 5}


def test_a_role_with_every_ask_unknown_scores_zero_from_its_asks_and_the_probability_is_not_read():
    scored = work(credit_profile(), said(bonus="unknown", penalty="unknown", each="unknown", list=("unknown",),
                                         p1="unknown", p2="unknown"))
    assert [scored[k] for k in ("earned", "percent", "total", "decided", "coverage")] == [0, 0, 0, 0, 0]
    assert scored["possible"] == 14
    assert "prob" not in scored, "no probability is kept or used"


def test_a_score_keeps_the_number_of_the_arithmetic_it_was_worked_out_by():
    assert work(profile_of(asks=[ask("a", 1)]), said(a="met"))["math"] == scoring.MATH == 4


def test_compute_has_no_policy_and_no_probability_to_score_by():
    import inspect
    assert not hasattr(scoring, "UNKNOWN_POLICY") and not hasattr(scoring, "COVERAGE_FLOOR")
    assert not hasattr(scoring, "math_for") and not hasattr(scoring, "NO_PROB")
    assert {"prob", "policy"}.isdisjoint(inspect.signature(scoring.compute).parameters)


# --- which rows count for a role ------------------------------------------------------------------


def tabs() -> p.Profile:
    return profile_of(asks=[ask("all-ask", 2)],
                      groups=[first_match("builders-list", [("hi", 4), ("lo", 3)], roles=["builders"]),
                              any_of("all-box", 2, [("p1",), ("p2",)])],
                      roles=("builders", "analysts"))


def test_a_group_on_a_roles_tab_counts_for_that_role_and_each_percent_is_of_its_own():
    perfect = said(all_ask="met", builders_list=("hi",), p1="met")
    builders = work(tabs(), perfect, target_role="builders")
    analysts = work(tabs(), perfect, target_role="analysts")
    nobody = work(tabs(), perfect)
    assert (builders["possible"], builders["percent"]) == (2 + 4 + 2, 100)
    assert (analysts["possible"], analysts["percent"]) == (2 + 2, 100), "a tab with nothing of its own reaches 100%"
    assert (nobody["possible"], nobody["percent"]) == (4, 100), "a role with no target role counts All's items only"
    assert [r["id"] for r in builders["asks"]] == ["all-ask", "builders-list", "all-box"]
    assert [r["id"] for r in analysts["asks"]] == ["all-ask", "all-box"]


def test_an_answer_to_a_group_that_is_not_the_roles_is_set_aside():
    got = work(tabs(), said(all_ask="met", builders_list=("hi",), p1="met"), target_role="analysts")
    assert "builders-list" not in [r["id"] for r in got["asks"]] and got["earned"] == 4.0


def test_a_part_switched_off_takes_its_groups_out_of_what_can_be_earned_and_decided_and_earned():
    profile = profile_of(asks=[ask("solo", 2, section="posting")],
                         groups=[first_match("list", [("hi", 4), ("lo", 3)], section="company"),
                                 any_of("box", 2, [("p1",), ("p2",)], section="company")],
                         sections_off=["company"])
    scored = work(profile, said(solo="met", list=("hi",), p1="met"))
    assert (scored["possible"], scored["decided"], scored["earned"], scored["coverage"]) == (2, 2, 2.0, 100)
    assert [r["id"] for r in scored["asks"]] == ["solo"]


def test_a_part_switched_off_for_one_roles_tab_takes_only_that_tabs_group_out():
    profile = profile_of(groups=[first_match("list", [("hi", 4), ("lo", 3)], roles=["builders"])], roles=("builders",),
                         by_role={"builders": {"sections_off": ["description"]}})
    assert work(profile, said(), target_role="builders")["possible"] == 0


# --- the percent a score is read by ---------------------------------------------------------------


def role_of(breakdown: dict | None, fit: int | None = 6, target_role: str | None = None):
    return SimpleNamespace(fit=fit, fit_breakdown=breakdown, target_role=target_role)


def test_a_score_worked_out_by_this_arithmetic_is_read_by_the_percent_it_holds():
    scored = work(grouped_profile(), AS_PICKS)
    held = role_of({"answers": [], **scored}, fit=scored["total"])
    assert scoring.percent_of(held, None) == 39
    assert scoring.percent_of(role_of({**scored, "percent": None}, fit=scored["total"]), None) is None


def test_a_percent_is_the_earned_points_over_what_can_be_earned_rounded_half_up_once():
    scored = work(profile_of(asks=[ask("a", 1), ask("b", 7)]), said(a="met", b="not"))
    assert (scored["earned"], scored["possible"], scored["percent"]) == (1.0, 8, 13), "12.5% is 13"
    awkward = work(profile_of(asks=[ask("a", 5), ask("b", 5), ask("c", 15)]), said(a="met", b="partly", c="not"))
    assert awkward["earned"] == 5.0 and awkward["total"] == 5 and awkward["percent"] == 20


def test_a_score_kept_before_this_arithmetic_is_read_as_it_always_was():
    old = role_of({"asks": [{"id": "x", "answer": "met"}], "earned": 2.0, "possible": 5, "total": 2}, fit=2)
    assert scoring.percent_of(old, None) == 40


def test_the_percent_the_owner_sees_is_the_stored_percent_so_25_points_of_100_shows_25_and_24_shows_24():
    for points, shown in ((25, 25), (24, 24)):  # 25 and 24 points of 100
        exact = work(profile_of(asks=[ask("a", points), ask("b", 100 - points)]), said(a="met"))
        held = role_of({"answers": [], **exact}, fit=exact["total"])
        assert (exact["percent"], scoring.percent_of(held, None)) == (shown, shown)


def test_a_role_whose_asks_are_all_unknown_has_a_percent_of_zero_and_a_partly_met_ask_earns_nothing():
    profile = profile_of(asks=[ask("a", 4), ask("b", 6)])
    scored = work(profile, said(a="unknown", b="unknown"))
    held = role_of({"answers": [], **scored}, fit=scored["total"])
    assert scored["percent"] == 0 and scoring.percent_of(held, None) == 0
    enough = work(profile, said(a="partly", b="met"))  # 6 of the 10 points: partly earns nothing
    assert (enough["percent"], enough["decided"]) == (60, 10)


# --- what can be earned ---------------------------------------------------------------------------


def test_what_can_be_earned_counts_each_group_once_at_its_top_value_and_leaves_penalties_out():
    profile = profile_of(asks=[ask("a", 3), ask("pen", -5)],
                         groups=[first_match("list", [("x", 4), ("y", 3), ("z", 2)]),
                                 any_of("box", 2, [("p1",), ("p2",)]), any_of("badbox", -4, [("q1",), ("q2",)])])
    assert scoring.possible(profile) == 3 + 4 + 2
    assert scoring.reach(profile.scoring.groups[0]) == 4 and scoring.reach(profile.scoring.groups[2]) == 0


def test_a_bonus_counted_for_each_item_with_no_limit_counts_its_points_once():
    profile = profile_of(asks=[ask("each", 2, per_item=True)])
    assert scoring.possible(profile) == 2
    scored = work(profile, {"answers": [{"id": "each", "answer": "met", "count": 7}]})
    assert (row(scored, "each")["earned"], scored["percent"]) == (2, 100), "it earned 14 of a possible 2 before"


def test_a_negative_ask_never_enters_what_can_be_earned_or_what_is_decided():
    scored = work(profile_of(asks=[ask("a", 4), ask("pen", -3)]), said(a="met", pen="met"))
    # Edited 2026-10-27 (PRD 30.10): the met -3 left 1.0 earned; it takes nothing off, so 4.0.
    assert (scored["possible"], scored["decided"], scored["earned"]) == (4, 4, 4.0)


async def test_the_profile_page_is_told_the_most_a_role_can_earn_with_each_group_counted_once(client):
    doc = {"asks": [ask("a", 3)], "groups": [first_match("list", [("x", 4), ("y", 3)]),
                                             any_of("box", 2, [("p1",), ("p2",)])]}
    assert (await client.put("/api/v1/profile/scoring", json={"data": doc})).status_code == 200
    body = (await client.get("/api/v1/profile")).json()
    assert body["fit_possible"] == 3 + 4 + 2


# --- the words for a role not scored yet and the role panel --------------------------------------------------------


def test_the_words_for_a_role_not_scored_yet_count_the_first_match_a_reader_answered_and_the_parts_of_an_any_of():
    profile = profile_of(groups=[first_match("list", [("x", 4), ("y", 3)]), any_of("box", 2, [("p1",), ("p2",)])])
    line = scoring.answers_line(said(x="not", y="not", p1="not", p2="met"), profile)
    assert line == "1 met, 2 not met; biggest gap: Statement x", "the any-of has a part met: it is no gap"
    assert scoring.answers_line(said(), profile) == ""
    both_not = scoring.answers_line(said(x="not", y="met", p1="not", p2="not"), profile)
    assert both_not == "1 met, 2 not met; biggest gap: Statement p1"


def test_the_role_panel_has_one_row_for_each_group_with_its_statements_inside_it():
    profile = profile_of(asks=[ask("solo", 1)], groups=[first_match("list", [("x", 4), ("y", 3)]),
                                                        any_of("box", 2, [("p1",), ("p2",)])])
    scored = work(profile, said(list=("y",), p2="met"))
    rows = scoring.view({"answers": [], **scored}, profile)
    by_id = {r["id"]: r for r in rows}
    assert by_id["list"]["state"] == "checked" and by_id["list"]["level"] == "y" and len(by_id["list"]["levels"]) == 2
    assert by_id["box"]["state"] == "checked" and [x["id"] for x in by_id["box"]["parts"]] == ["p1", "p2"]
    assert by_id["solo"]["state"] == "unknown"
    assert all(r["section_label"] == "Description" for r in rows)


def test_the_role_panel_shows_a_group_before_the_role_is_scored_with_the_answer_a_reader_gave():
    profile = profile_of(groups=[first_match("list", [("x", 4), ("y", 3)]), any_of("box", 2, [("p1",), ("p2",)])])
    rows = {r["id"]: r for r in scoring.view(said(list=("y",), p1="not"), profile)}
    assert (rows["list"]["state"], rows["list"]["answer"], rows["list"]["level"], rows["list"]["earned"]) == (
        "unscored", "met", "y", None)
    assert [(x["id"], x["answer"]) for x in rows["box"]["parts"]] == [("p1", "not"), ("p2", None)]
    bare = {r["id"]: r for r in scoring.view(None, profile)}
    assert bare["list"]["answer"] is None and bare["list"]["points"] == 4 and bare["box"]["points"] == 2


# --- the pay tiers --------------------------------------------------------------------------------


def tier(ident: str, points: int, amount: int, **more) -> dict:
    return ask(ident, points, amount=amount, checked_by="app", **more)


def test_independent_pay_tiers_stack_in_the_fit_and_in_the_pay_line():
    profile = profile_of(asks=[tier("over-100", 1, 100_000), tier("over-150", 2, 150_000)])
    assert [a.id for a in scoring.pay_tiers_met(160_000, profile)] == ["over-100", "over-150"]
    assert comp.pay_points(160_000, profile) == (3, "Statement over-100; Statement over-150")
    scored = work(profile, said(), facts={"ask": 160_000})
    assert scored["earned"] == 3.0 == comp.pay_points(160_000, profile)[0]


def test_pay_tiers_in_a_first_match_give_the_held_level_in_the_fit_and_in_the_pay_line():
    profile = profile_of(groups=[first_match("pay", [("over-150", 3, {"checked_by": "app", "amount": 150_000}),
                                                     ("over-100", 2, {"checked_by": "app", "amount": 100_000})])])
    for have, level, points in ((160_000, "over-150", 3), (120_000, "over-100", 2), (90_000, None, 0)):
        scored = work(profile, said(), facts={"ask": have})
        assert row(scored, "pay")["earned"] == points, have
        assert comp.pay_points(have, profile)[0] == points == scored["earned"], "one rule for the fit and the line"
        assert [a.id for a in scoring.pay_tiers_met(have, profile)] == ([level] if level else [])


def test_pay_tiers_in_an_any_of_count_once():
    box = any_of("pay", 2, [("over-100", {"checked_by": "app", "amount": 100_000}),
                            ("over-150", {"checked_by": "app", "amount": 150_000})])
    profile = profile_of(groups=[box])
    scored = work(profile, said(), facts={"ask": 160_000})
    assert row(scored, "pay")["earned"] == 2 and comp.pay_points(160_000, profile)[0] == 2
    assert comp.pay_points(90_000, profile)[0] == 0 and work(profile, said(), facts={"ask": 90_000})["earned"] == 0


# --- grade the requirements for me ----------------------------------------------------------------


GRADE_ON = {"grade_requirements": GRADED_ON}


def graded_profile(**more) -> p.Profile:
    return profile_of(**GRADE_ON, **more)


def test_with_the_setting_off_nothing_changes():
    classes, preferred = ["P", "U", "T"], ["P", "U"]
    base = {"asks": [ask("a", 3)]}
    off = p.Profile.model_validate({"scoring": {**base, "grade_requirements": {**GRADED_ON, "on": False}}})
    never_set = p.Profile.model_validate({"scoring": base})
    with_classes = work(off, said(a="met"), classes=classes, preferred=preferred)
    assert with_classes == work(never_set, said(a="met"), classes=classes, preferred=preferred)
    assert with_classes == work(never_set, said(a="met")), "the classes change nothing while it is off"
    assert scoring.GRADE_SAYS not in [r["says"] for r in with_classes["asks"]]
    assert [r["id"] for r in with_classes["asks"]] == ["a"] and with_classes["possible"] == 3


def test_a_strong_fit_scores_higher_than_a_weak_one():
    profile = graded_profile()
    strong = work(profile, said(), classes=["P", "P", "P", "T"], preferred=["P", "T"])
    weak = work(profile, said(), classes=["U", "U", "P", "T"], preferred=["U", "U"])
    assert (strong["earned"], strong["possible"], strong["percent"]) == (8.0, 10, 80)
    assert (weak["earned"], weak["possible"], weak["percent"]) == (-2.0, 10, 0)
    assert strong["total"] > weak["total"] and strong["percent"] > weak["percent"]


def test_the_percent_uses_the_roles_own_maximum_since_roles_list_different_numbers_of_items():
    profile = graded_profile()
    short = work(profile, said(), classes=["P", "P"], preferred=["P"])
    long = work(profile, said(), classes=["P"] * 5, preferred=["P"] * 3)
    assert (short["possible"], long["possible"]) == (2 * 2 + 1, 5 * 2 + 3)
    assert short["percent"] == long["percent"] == 100


def test_the_maximum_is_the_required_count_times_the_proven_weight_plus_the_preferred_count_times_theirs():
    profile = profile_of(grade_requirements={**GRADED_ON, "required_proven": 3, "preferred_proven_or_transferable": 2})
    got = work(profile, said(), classes=["P", "U", "T", "U"], preferred=["T", "U", "P"])
    assert got["possible"] == 4 * 3 + 3 * 2


def test_an_unproven_required_item_lowers_the_earned_points_and_not_what_can_be_earned():
    profile = graded_profile()
    clean = work(profile, said(), classes=["P", "P", "P", "P"])
    dragged = work(profile, said(), classes=["P", "P", "P", "U"])
    assert clean["possible"] == dragged["possible"] == 8
    assert (clean["earned"], dragged["earned"]) == (8.0, 4.0)
    assert (clean["percent"], dragged["percent"]) == (100, 50)
    assert row(dragged, scoring.GRADE_ID)["points"] == 8, "the row's share of what can be earned does not take it off"


def test_a_role_half_proven_and_half_unproven_nets_zero_from_its_required_items():
    got = work(graded_profile(), said(), classes=["P", "P", "U", "U"])
    assert (row(got, scoring.GRADE_ID)["earned"], got["earned"], got["percent"]) == (0.0, 0.0, 0)
    assert work(graded_profile(), said(), classes=["P", "P", "T", "T", "U", "U"])["earned"] == 0.0


def test_the_preferred_items_earn_when_proven_or_transferable_and_nothing_when_unproven():
    got = work(graded_profile(), said(), classes=[], preferred=["P", "T", "U", "U"])
    assert (got["earned"], got["possible"]) == (2.0, 4)


def test_the_graded_row_is_the_apps_own_decided_and_never_asked_of_an_agent():
    got = work(graded_profile(), said(**{"grade:requirements": "met"}), classes=["P", "U"])
    graded = row(got, scoring.GRADE_ID)
    assert (graded["kind"], graded["by"], graded["decided"], graded["section"]) == (
        "grade", "app", True, "qualifications")
    assert graded["required"] == {"proven": 1, "transferable": 0, "unproven": 1} and graded["earned"] == 0.0
    assert graded["evidence"].startswith("from the research: required 1 Proven, 0 Transferable, 1 Unproven")
    assert got["decided"] == got["possible"] == 4, "always decided: there is no score before research"
    assert work(graded_profile(), said(), classes=["P", "U"])["earned"] == got["earned"], "an agent's word is set aside"


def test_each_half_of_the_grade_follows_the_part_it_belongs_to():
    quals_off = work(graded_profile(sections_off=["qualifications"]), said(), classes=["P", "P"], preferred=["P"])
    assert (quals_off["possible"], quals_off["earned"]) == (1, 1.0)
    assert row(quals_off, scoring.GRADE_ID)["section"] == "preferred"
    preferred_off = work(graded_profile(sections_off=["preferred"]), said(), classes=["P", "P"], preferred=["P"])
    assert (preferred_off["possible"], preferred_off["earned"]) == (4, 4.0)
    both_off = work(graded_profile(sections_off=["qualifications", "preferred"]), said(), classes=["P"],
                    preferred=["P"])
    assert both_off["asks"] == [] and both_off["possible"] == 0 and both_off["percent"] is None


def test_a_role_not_researched_has_nothing_to_grade_and_can_reach_nothing_from_it():
    got = work(graded_profile(asks=[ask("a", 3)]), said(a="met"), classes=None, preferred=None)
    assert (got["possible"], got["earned"], got["percent"]) == (3, 3.0, 100)
    assert row(got, scoring.GRADE_ID)["points"] == 0


def test_the_graded_row_adds_to_the_asks_and_an_ask_nobody_answered_earns_nothing():
    profile = graded_profile(asks=[ask("a", 6), ask("b", 4)])
    got = work(profile, said(a="met"), classes=["P", "P"], preferred=["P", "T"])
    assert got["possible"] == 6 + 4 + (2 * 2 + 2 * 1)
    assert got["earned"] == 6 + 0 + (4 + 2), "a met; b not answered; the grade 4 + 2, by its classes as before"


def test_the_requirements_are_graded_in_the_stored_breakdown_and_read_back_by_the_role_panel():
    profile = graded_profile()
    got = work(profile, said(), classes=["P", "U"], preferred=["T"])
    panel = scoring.view({"answers": [], **got}, profile)
    graded = next(r for r in panel if r["id"] == scoring.GRADE_ID)
    assert (graded["state"], graded["section_label"]) == ("checked", "Qualifications")
    assert scoring.view(None, profile) == [], "no row until the role is scored"


def test_the_preferred_classes_are_read_from_the_list_the_research_keeps_beside_the_required_items():
    researched = SimpleNamespace(analysis={"required": [{"item": "a", "class": "P"}],
                                           "preferred": [{"item": "b", "class": "T"}, {"item": "c", "class": "U"}]})
    assert prioritize.preferred_classes_of(researched) == ["T", "U"]
    assert prioritize.classes_of(researched) == ["P"]
    assert prioritize.preferred_classes_of(SimpleNamespace(analysis={"required": [{"class": "P"}]})) is None
    assert prioritize.preferred_classes_of(SimpleNamespace(analysis=None)) is None
