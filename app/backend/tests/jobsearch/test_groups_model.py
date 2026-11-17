"""Groups in Prioritization, as a model (docs/PRD.md 30.9): an "any of" row and a "first match" row hold several
statements, each an ask the reader answers or the app decides; the model refuses what cannot be scored and says why in
words; a document with none reads and dumps as it did. Synthetic data only: made-up ids and sentences."""

import copy

import pytest
from pydantic import ValidationError

from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.profile import store as profile_store
from tests.support import GRADED_ON


def ask(ident: str, says: str | None = None, points: int = 0, **more) -> dict:
    return {"id": ident, "says": says or f"Statement {ident}", "points": points, **more}


def first_match(ident: str = "list-a", levels=(("lvl-a", 4), ("lvl-b", 3)), **more) -> dict:
    return {"id": ident, "kind": "best", "section": "responsibilities",
            "members": [ask(i, points=pts) for i, pts in levels], **more}


def any_of(ident: str = "box-a", points: int = 2, parts=("part-a", "part-b"), **more) -> dict:
    return {"id": ident, "kind": "any", "section": "company", "points": points,
            "members": [ask(i) for i in parts], **more}


def scoring(*groups: dict, asks=(), **more) -> ps.Scoring:
    return ps.Scoring.model_validate({"asks": list(asks), "groups": list(groups), **more})


def refuses(match: str, *groups: dict, asks=(), **more) -> None:
    with pytest.raises(ValidationError, match=match):
        scoring(*groups, asks=asks, **more)


# --- the two kinds, and nothing else ----------------------------------------------------------------


def test_a_first_match_and_an_any_of_load_with_their_statements_in_the_order_given():
    s = scoring(first_match(levels=(("lvl-a", 4), ("lvl-b", 3), ("lvl-c", 2))), any_of())
    first, anyof = s.groups
    assert (first.kind, [m.id for m in first.members], [m.points for m in first.members]) == (
        "best", ["lvl-a", "lvl-b", "lvl-c"], [4, 3, 2])
    assert (anyof.kind, anyof.points, [m.points for m in anyof.members]) == ("any", 2, [0, 0])


def test_there_is_no_all_of_kind():
    refuses("kind", {**any_of(), "kind": "all"})
    refuses("kind", {**first_match(), "kind": "ranked"})


def test_a_group_has_to_name_its_kind_and_its_statements():
    refuses("kind", {"id": "x", "members": [ask("a"), ask("b")]})
    refuses("members", {"id": "x", "kind": "any"})


def test_the_statements_take_the_part_and_the_tab_of_the_group_they_are_in():
    s = scoring(first_match(section="qualifications", roles=["builders"]),
                any_of(parts=("part-a", "part-b", "part-c")))
    first, anyof = s.groups
    assert {(m.section, tuple(m.roles)) for m in first.members} == {("qualifications", ("builders",))}
    assert {(m.section, tuple(m.roles)) for m in anyof.members} == {("company", ())}
    # Whatever a statement said of its own part or tab, the group's is the one that counts.
    odd = first_match(levels=(("lvl-a", 2), ("lvl-b", 1)))
    odd["members"][0].update(section="posting", roles=["other"])
    assert {(m.section, tuple(m.roles)) for m in scoring(odd).groups[0].members} == {("responsibilities", ())}


def test_a_group_names_the_tab_it_is_on_as_an_ask_does():
    s = scoring(first_match(roles=[" builders ", "builders", ""]))
    assert s.groups[0].roles == ["builders"]
    assert scoring(first_match()).groups[0].roles == [], "empty is All's tab"


# --- what the model refuses, in words ----------------------------------------------------------------


def test_a_first_match_needs_at_least_two_statements():
    refuses("a first-match row needs at least two statements", first_match(levels=(("lvl-a", 4),)))
    refuses("a first-match row needs at least two statements", first_match(levels=()))


def test_an_any_of_needs_at_least_two_statements():
    refuses("an 'any of' row needs at least two statements", any_of(parts=("part-a",)))
    refuses("an 'any of' row needs at least two statements", any_of(parts=()))


def test_a_statement_with_no_words_is_refused():
    blank = any_of()
    blank["members"][1]["says"] = "   "
    refuses("every statement of an 'any of' row needs a sentence", blank)
    level = first_match()
    level["members"][0]["says"] = ""
    refuses("every statement of a first-match row needs a sentence", level)


def test_the_points_of_an_any_of_are_on_the_row_and_not_on_its_statements():
    row = any_of()
    row["members"][0]["points"] = 1
    refuses("the points of an 'any of' row are on the row, not on its statements", row)


def test_the_points_of_a_first_match_are_on_its_statements_and_not_on_the_row():
    refuses("the points of a first-match row are on its statements, not on the row", first_match(points=3))
    assert scoring(first_match(points=0)).groups[0].points == 0


def test_a_statement_counted_for_each_item_cannot_be_in_a_group():
    per_item = any_of()
    per_item["members"][0].update(per_item=True, limit=-3, points=0)
    refuses("an ask counted for each item, or the built-in responsibilities ask, cannot be in a first-match or "
            "'any of' row", per_item)
    level = first_match()
    level["members"][1].update(per_item=True, limit=3)
    refuses("cannot be in a first-match or 'any of' row", level)


def test_the_built_in_responsibilities_ask_cannot_be_in_a_group():
    built_in = first_match()
    built_in["members"][0].update(derived="responsibilities")
    refuses("the built-in responsibilities ask, cannot be in a first-match or 'any of' row", built_in)


def test_a_first_match_is_judged_by_claude_or_by_the_app_and_never_both():
    mixed = first_match()
    mixed["members"][0].update(checked_by="app", fact="employees", op="<", value=50)
    refuses("a first-match row is judged by Claude or by the app, not both: every statement needs a test, or none "
            "does", mixed)
    app = first_match(levels=(("small", 2), ("medium", 1)))
    for level, value in zip(app["members"], (50, 500), strict=True):
        level.update(checked_by="app", fact="employees", op="<", value=value)
    assert scoring(app).groups[0].by_the_app() is True
    assert scoring(first_match()).groups[0].by_the_app() is False


def test_an_any_of_may_have_some_statements_for_claude_and_some_for_the_app():
    row = any_of()
    row["members"][0].update(checked_by="app", fact="ai_native", op="=", value=True)
    group = scoring(row).groups[0]
    assert [m.checked_by for m in group.members] == ["app", "reader"]
    assert group.by_the_app() is False


def test_a_statement_the_app_decides_still_needs_a_way_to_decide_it():
    odd = any_of()
    odd["members"][0].update(checked_by="app")
    refuses("the app can only check an ask it can decide itself", odd)


def test_a_group_can_be_decided_by_the_app_from_a_fact_the_posting_age_a_pay_amount_or_the_classes():
    """A row can be answered by the app, not only by an agent (the owner, 2026-10-14): every way the app decides an
    independent ask is one a statement of a group has too."""
    row = {"id": "fit", "kind": "best", "section": "qualifications", "members": [
        ask("proven", points=3, checked_by="app", from_classes="all_P"),
        ask("some", points=1, checked_by="app", from_classes="T")]}
    s = scoring(row)
    assert s.groups[0].by_the_app() is True
    box = {"id": "box-b", "kind": "any", "section": "compensation", "points": 2, "members": [
        ask("fresh", checked_by="app", days=3, above=False),
        ask("paid", checked_by="app", amount=150000),
        ask("small", checked_by="app", fact="employees", op="<", value=100)]}
    group = scoring(box).groups[0]
    assert [m.mechanism() for m in group.members] == ["age", "pay", "employer"] and group.by_the_app() is True


# --- one id space ----------------------------------------------------------------


def test_an_ask_a_group_and_a_statement_each_have_an_id_of_their_own():
    said = "'{}' is used twice: every ask, row and statement needs an id of its own"
    # a group and an ask
    refuses(said.format("list-a"), first_match("list-a"), asks=[ask("list-a")])
    # a statement and an ask
    refuses(said.format("lvl-b"), first_match(), asks=[ask("lvl-b")])
    # a statement and a group
    refuses(said.format("box-a"), first_match(levels=(("box-a", 4), ("lvl-b", 3))), any_of("box-a"))
    # two groups
    refuses(said.format("list-a"), first_match("list-a"), any_of("list-a"))
    # a statement in two groups
    refuses(said.format("part-a"), any_of("box-a"), any_of("box-b"))
    # and the same statement twice in one group
    refuses(said.format("lvl-a"), first_match(levels=(("lvl-a", 4), ("lvl-a", 3))))


def test_the_refusal_reaches_the_owner_in_the_stores_wording():
    with pytest.raises(profile_store.ProfileError, match="the scoring don't fit: groups.0.members: .*at least two"):
        profile_store._check("scoring", {"groups": [first_match(levels=(("lvl-a", 4),))]})
    with pytest.raises(profile_store.ProfileError, match="the scoring don't fit: groups: .*'lvl-a' is used twice"):
        profile_store._check("scoring", {"asks": [ask("lvl-a")], "groups": [first_match()]})


def test_two_asks_that_share_an_id_in_a_document_with_no_groups_still_read_as_they_did():
    s = ps.Scoring.model_validate({"asks": [ask("same", points=1), ask("same", points=2)]})
    assert [a.points for a in s.asks if a.id == "same"] == [1, 2]


def test_a_built_in_ask_takes_no_id_of_a_group_or_a_statement():
    s = scoring(first_match(levels=((ps.RESPONSIBILITIES_ID, 4), ("lvl-b", 3))), any_of(ps.RESPONSIBILITIES_ID + "-2"))
    built_in = [a for a in s.asks if a.derived]
    assert [a.id for a in built_in] == [ps.RESPONSIBILITIES_ID + "-3"], "free of the group's and the statement's ids"
    assert ps.built_in_asks(s.ids_in_use())[0].id == ps.RESPONSIBILITIES_ID + "-4"


# --- a document with none reads as it did; one with them round trips ------------------------------------


OLD = {"sections_off": [], "asks": [ask("ask-a", points=2, section="posting")], "by_role": {}, "old_scale": None,
       "notes": [], "built_ins_given": ["responsibilities"]}


def test_a_document_with_no_groups_reads_as_before_and_dumps_with_the_new_fields_empty():
    s = ps.Scoring.model_validate({"asks": [ask("ask-a", points=2, section="posting")]})
    dumped = s.model_dump(mode="json")
    new = {"groups", "suggestions", "grade_requirements", "match_weight"}
    assert set(dumped) - new == set(OLD), "nothing else about how a document dumps has changed"
    assert dumped["match_weight"] == 0.5, "the queue's split reads as half until the owner sets it"
    assert dumped["groups"] == [] and dumped["suggestions"] == []
    assert dumped["grade_requirements"] is None, "null is off: the model holds no weights until the owner sets them"
    assert [a["id"] for a in dumped["asks"]][0] == "ask-a" and ps.Scoring.model_validate(dumped) == s


def test_the_old_document_with_none_of_the_new_fields_reads_the_same_as_one_that_names_them_empty():
    bare = ps.Scoring.model_validate(copy.deepcopy(OLD))
    named = ps.Scoring.model_validate({**copy.deepcopy(OLD), "groups": [], "suggestions": [],
                                      "grade_requirements": None})
    assert bare == named


def test_a_document_with_groups_and_suggestions_round_trips_field_for_field():
    doc = {"asks": [ask("ask-x", points=1), ask("ask-y", points=2)],
           "groups": [first_match(roles=["builders"]), any_of(points=-2)],
           "suggestions": [{"id": "sug-a", "kind": "best", "asks": ["ask-x", "ask-y"], "points": 0,
                            "why": "These cannot both be true of one posting."}]}
    s = ps.Scoring.model_validate(doc)
    dumped = s.model_dump(mode="json")
    assert ps.Scoring.model_validate(dumped) == s
    assert ps.Scoring.model_validate(dumped).model_dump(mode="json") == dumped
    assert [g["id"] for g in dumped["groups"]] == ["list-a", "box-a"]
    assert dumped["groups"][0]["members"][0]["roles"] == ["builders"], "a statement is stored as an ask is"
    assert dumped["suggestions"][0]["asks"] == ["ask-x", "ask-y"]


def test_the_history_names_a_group_and_a_suggestion_by_their_ids():
    before = ps.Scoring.model_validate(copy.deepcopy(OLD)).model_dump(mode="json")
    after = scoring(first_match(), asks=[ask("ask-x", points=1), ask("ask-y", points=1)], suggestions=[
        {"id": "sug-a", "kind": "any", "asks": ["ask-x", "ask-y"], "points": 1, "why": "Either one."}]
    ).model_dump(mode="json")
    said = profile_store.changes(before, after)
    assert "added groups: list-a" in said and "added suggestions: sug-a" in said
    gone = scoring(asks=[ask("ask-x", points=1), ask("ask-y", points=1)]).model_dump(mode="json")
    assert "removed suggestions: sug-a" in profile_store.changes(after, gone)
    assert "removed groups: list-a" in profile_store.changes(after, gone)
    edited = copy.deepcopy(after)
    edited["groups"][0]["members"][0]["says"] = "In other words"
    assert profile_store.changes(after, edited) == ["changed groups: list-a"]


# --- which role a group counts for ----------------------------------------------------------------


def profile_of(*groups: dict, **scoring_more) -> p.Profile:
    return p.Profile.model_validate({
        "criteria": {"target_roles": [{"id": "builders", "name": "Builders"}, {"id": "analysts", "name": "Analysts"}]},
        "scoring": {"asks": [ask("ask-a", points=1, section="posting")], "groups": list(groups), **scoring_more}})


def ids(items) -> list[str]:
    return [i.id for i in items if not (isinstance(i, ps.Ask) and i.derived)]


def test_a_group_on_all_counts_for_every_role_and_for_a_role_with_no_target_role():
    profile = profile_of(first_match(), any_of())
    for role in ("builders", "analysts", None):
        assert ids(profile.items_for(role)) == ["ask-a", "list-a", "box-a"]


def test_a_group_on_a_roles_tab_counts_for_that_role_alone():
    profile = profile_of(first_match(roles=["builders"]), any_of())
    assert ids(profile.items_for("builders")) == ["ask-a", "list-a", "box-a"]
    assert ids(profile.items_for("analysts")) == ["ask-a", "box-a"]
    assert ids(profile.items_for(None)) == ["ask-a", "box-a"], "a role with no target role counts All's items only"


def test_a_group_follows_the_switch_of_its_part_for_its_tab():
    profile = profile_of(first_match(), any_of(), sections_off=["company"])
    assert ids(profile.items_for("builders")) == ["ask-a", "list-a"], "Company is off in All's switches"
    own = profile_of(first_match(roles=["builders"]), any_of(roles=["builders"]),
                     by_role={"builders": {"sections_off": ["responsibilities"]}})
    assert ids(own.items_for("builders")) == ["ask-a", "box-a"], "the role's own switch governs the role's own group"
    assert ids(own.items_for("analysts")) == ["ask-a"], "and the group is not the other role's"


def test_the_statements_of_a_group_are_not_items_of_their_own():
    profile = profile_of(first_match(), any_of())
    assert "lvl-a" not in ids(profile.items_for("builders")) and "part-a" not in ids(profile.items_for("builders"))
    assert [a.id for a in profile.asks_for("builders")] == ["ask-a"]


def test_a_group_is_counted_where_an_ask_is_on_anywhere():
    s = scoring(first_match(roles=["builders"]), any_of(), by_role={"builders": {"sections_off": ["responsibilities"]}})
    first, anyof = s.groups
    assert s.item_on_anywhere(first) is False and s.item_on_anywhere(anyof) is True
    assert s.item_on(first, "builders") is False and s.item_on(anyof, None) is True


# --- suggestions: notes, never groups ----------------------------------------------------------------


def test_a_suggestion_names_asks_the_owner_has_and_changes_nothing_by_being_stored():
    with_note = profile_of(suggestions=[
        {"id": "sug-a", "kind": "best", "asks": ["ask-a", "ask-b", "ask-a"], "why": "x" * 5000}])
    without = profile_of()
    assert with_note.scoring.suggestions[0].asks == ["ask-a", "ask-b"], "each ask once, in the order proposed"
    assert len(with_note.scoring.suggestions[0].why) == 5000, "no limit on the sentence"
    assert with_note.items_for("builders") == without.items_for("builders") and not with_note.scoring.groups


def test_a_suggestion_may_name_a_single_ask_but_not_none():
    for kind in ("best", "any", "remove"):
        assert ps.Suggestion.model_validate({"id": "sug-a", "kind": kind, "asks": ["ask-a"]}).asks == ["ask-a"], kind
    for none in ([], [" ", ""]):
        with pytest.raises(ValidationError, match="a suggestion names at least one ask"):
            ps.Suggestion.model_validate({"id": "sug-a", "kind": "remove", "asks": none})


def test_a_first_match_suggestion_has_no_points_of_its_own_and_an_any_of_one_has_the_rows():
    with pytest.raises(ValidationError, match="a first-match suggestion has no points of its own"):
        ps.Suggestion.model_validate({"id": "sug-a", "kind": "best", "asks": ["ask-a", "ask-b"], "points": 2})
    kept = ps.Suggestion.model_validate({"id": "sug-a", "kind": "any", "asks": ["ask-a", "ask-b"], "points": 2})
    assert kept.points == 2


def test_a_remove_suggestion_names_asks_to_offer_for_removal_and_has_no_points():
    asks = ["core-gap", "preferred-proven", "core-gap"]
    note = ps.Suggestion.model_validate({"id": "sug-r", "kind": "remove", "asks": asks,
                                        "why": "Grading the requirements counts this already."})
    assert (note.kind, note.asks, note.points) == ("remove", ["core-gap", "preferred-proven"], 0)
    with pytest.raises(ValidationError, match="a remove suggestion has no points"):
        ps.Suggestion.model_validate({"id": "sug-r", "kind": "remove", "asks": ["core-gap"], "points": 1})
    with pytest.raises(ValidationError, match="kind"):
        ps.Suggestion.model_validate({"id": "sug-r", "kind": "delete", "asks": ["core-gap"]})


def test_a_remove_suggestion_removes_nothing_by_being_stored_and_round_trips():
    doc = {"asks": [ask("core-gap", points=-1), ask("keep", points=2)], "suggestions": [
        {"id": "sug-r", "kind": "remove", "asks": ["core-gap"], "why": "Counted twice."}]}
    s = ps.Scoring.model_validate(doc)
    assert [a.id for a in s.asks if not a.derived] == ["core-gap", "keep"], "the app removes no ask"
    assert [g.id for g in s.groups] == [] and s.suggestions[0].kind == "remove"
    again = ps.Scoring.model_validate(s.model_dump(mode="json"))
    assert again == s and again.model_dump(mode="json")["suggestions"][0]["kind"] == "remove"


def test_a_suggestion_is_not_part_of_the_id_space_of_the_asks():
    s = ps.Scoring.model_validate({"asks": [ask("ask-a")], "suggestions": [
        {"id": "ask-a", "kind": "any", "asks": ["ask-a", "ask-b"]}]})
    assert s.suggestions[0].id == "ask-a"


# --- "grade the requirements for me" ----------------------------------------------------------------


def test_grading_the_requirements_is_null_which_is_off_and_the_model_holds_no_default_weight():
    s = ps.Scoring()
    assert s.grade_requirements is None
    assert (s.grades_required(), s.grades_preferred()) == (False, False)
    assert ps.GRADE_ID == "grade:requirements"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ps.Scoring.model_validate({"grade_requirements": {**GRADED_ON, "required_excellent": 5}})


def test_the_object_needs_all_five_numbers_each_a_whole_number():
    for gone in ("required_proven", "required_transferable", "required_unproven",
                 "preferred_proven_or_transferable", "preferred_unproven"):
        short = {k: v for k, v in GRADED_ON.items() if k != gone}
        with pytest.raises(ValidationError, match=gone):
            ps.Scoring.model_validate({"grade_requirements": short})
    with pytest.raises(ValidationError, match="required_proven"):
        ps.Scoring.model_validate({"grade_requirements": {**GRADED_ON, "required_proven": 1.5}})
    with pytest.raises(ValidationError, match="required_unproven"):
        ps.Scoring.model_validate({"grade_requirements": {**GRADED_ON, "required_unproven": "many"}})


def test_a_document_that_ticks_grading_without_the_five_numbers_cannot_be_saved():
    """The page fills in the spec's starting numbers when the box is ticked (GRADE_START in profileSpec.ts, tested
    there); the store refuses a document that has the box on and any number missing, so none is ever kept."""
    with pytest.raises(profile_store.ProfileError, match="the scoring don't fit: grade_requirements.*required_proven"):
        profile_store._check("scoring", {"grade_requirements": {"on": True}})
    profile_store._check("scoring", {"grade_requirements": GRADED_ON})  # all five: kept


def test_each_weight_is_the_owners_to_set_and_takes_either_sign():
    g = ps.Scoring.model_validate({"grade_requirements": {**GRADED_ON, "required_unproven": -5}}).grade_requirements
    assert g is not None
    assert (g.on, g.required_unproven, g.required_proven, g.preferred_unproven) == (True, -5, 2, 0)


def test_each_half_of_the_grade_follows_the_switch_of_the_part_it_belongs_to():
    on = {"grade_requirements": GRADED_ON}
    assert (ps.Scoring.model_validate(on).grades_required(), ps.Scoring.model_validate(on).grades_preferred()) == (
        True, True)
    no_quals = ps.Scoring.model_validate({**on, "sections_off": ["qualifications"]})
    assert (no_quals.grades_required(), no_quals.grades_preferred()) == (False, True)
    no_pref = ps.Scoring.model_validate({**on, "sections_off": ["preferred"]})
    assert (no_pref.grades_required(), no_pref.grades_preferred()) == (True, False)
    off = ps.Scoring.model_validate({"sections_off": []})
    assert (off.grades_required(), off.grades_preferred()) == (False, False), "nothing is graded until it is turned on"
