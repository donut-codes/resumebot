"""The site projects' texts (docs/PRD.md path 29): the three step instructions and their
templates, the scout's role text, the kind and the vocab. They load through the harness; each instruction describes
one step; and the closing rule that keeps Act from running extra rounds stays."""

from pathlib import Path

import pytest

from app.config import settings
from app.harness import instructions as hi
from app.harness.loader import harness

STEPS = {
    "projects.plan.write": {"write_version", "plan_call"},
    "projects.do.item": {"item_key", "kind"},
    "projects.act.learn": {"plan_version"},
}
TEMPLATES = ["projects.plan.response", "projects.do.item.response", "projects.act.response"]
ROLE = "agents.scout.role"


def test_every_step_instruction_loads_with_exactly_its_placeholders():
    for name, expected in STEPS.items():
        assert hi.placeholders(hi.read("instructions", name)) == expected, name
    assert hi.placeholders(hi.read("instructions", ROLE)) == set()


def test_each_text_is_a_real_one_not_the_step_three_placeholder():
    for kind, names in (("instructions", [*STEPS, ROLE]), ("templates", TEMPLATES)):
        for name in names:
            text = hi.read(kind, name)
            assert "placeholder" not in text.lower() and hi.word_count(text) > 60, name


def test_act_keeps_the_closing_rule_and_judges_the_round_against_the_projects_own_outcome():
    text = hi.read("instructions", "projects.act.learn")
    assert "`closing.outcome`" in text and "`resolution.closing_hint`" in text
    assert "another project, never a reason to go on" in text
    assert "wiki.write" not in text and "lesson.record" in text


def test_plan_names_the_four_ways_a_site_loads_and_the_scout_never_writes_a_check():
    text = hi.read("instructions", "projects.plan.write")
    for word in ("adapters", "data request", "list pages", "browser", "needs"):
        assert word in text, word
    assert "Check is code" in text and "robots.txt" in text
    do = hi.read("instructions", "projects.do.item")
    for kind in ("adapter", "request", "list_page"):
        assert f"`{kind}`" in do, kind


def test_the_scout_is_forbidden_what_it_must_never_do():
    for name in (ROLE, "projects.plan.write", "projects.do.item"):
        text = hi.read("instructions", name).lower()
        assert "captcha" in text and "robots.txt" in text, name


def test_a_site_that_cannot_be_read_is_escalated_by_every_text_that_speaks_of_it_and_none_leaves_it_to_the_owner():
    """The kind and the skill template told the scout to end the item off plan and "the owner decides", the
    role text said it is never the owner's to decide; the do-instruction sent it to a `kind_spec.do` with no prose."""
    skill = hi.read("templates", "site.skill")
    do = hi.read("instructions", "projects.do.item")
    kind = hi.read_kind("site.read")["notes"]
    for name, text in (("site.skill", skill), ("projects.do.item", do), ("site.read", kind)):
        assert "needs_browser" in text and "cannot_be_read" in text, name
        assert "owner's to decide" not in text and "finish the item off plan" not in text, name
    assert "page_text" not in skill and "page_text" not in do
    assert "kind_spec.do`" not in do and "doc.read(kind_spec.id)" in do


def test_the_texts_promise_nothing_the_app_does_not_do():
    """No `owner_notes`, `suggested_items`, `use_suggestion` or note objects (nothing supplies them),
    and no `default` that is meant to happen when the owner does not answer (nothing applies it)."""
    texts = [hi.read("instructions", n) for n in (*STEPS, ROLE)] + [hi.read("templates", n) for n in TEMPLATES]
    for text in texts:
        for word in ("owner_notes", "suggested_items", "use_suggestion", "note_id"):
            assert word not in text, word
    role = hi.read("instructions", ROLE)
    assert "`default`" not in role and "doesn't answer" not in role


def test_the_loop_and_its_process_file_name_no_field_the_plan_instruction_no_longer_has():
    """The plan instruction lost `suggested_items`; neither the loop's code nor the round's process file may
    still list it."""
    from app.sites import loop

    process_file = Path(settings.playbooks_path) / "sites" / "project.round.bpmn"
    for name, text in (("sites/loop.py", Path(loop.__file__).read_text(encoding="utf-8")),
                       ("project.round.bpmn", process_file.read_text(encoding="utf-8"))):
        for word in ("suggested_items", "expand_suggestions", "owner_notes", "use_suggestion"):
            assert word not in text, f"{name} names {word}"


def test_the_site_read_kind_names_the_field_that_carries_the_pages_own_count():
    spec = hi.read_kind("site.read")
    assert "expected_count" in spec["result"] and "expected_count" in spec["notes"]


def test_the_plan_template_carries_its_limits_block():
    text = hi.read("templates", "projects.plan.response")
    assert "<!-- limits:start -->" in text and "<!-- limits:end -->" in text
    assert "`proposal`: 1 to 3 bullets" in text


def test_the_kind_and_vocab_load_and_agree():
    if not (Path(settings.kinds_path).is_dir() and Path(settings.vocab_path).is_dir()):
        pytest.skip("kinds/ and vocab/ are not in this image yet (they are copied with the content)")
    content = harness()
    explore = content.kinds["porter.explore"]
    assert explore.risk == "read_only" and explore.enabled and explore.check.by == "porter"
    assert {"porter.core", "porter"} <= set(content.vocab)
    assert {"done", "off_plan", "not_attempted", "on_plan", "not_checked", "scout", "owner"} <= content.vocab_ids(
        "porter.core")
    assert content.reference_problems() == []
