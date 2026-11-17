"""The helper agent files (app/project-agents/*.md) and the getting-started agent of the setup plugin: they parse, name
only owner-door actions the app has, hold only the tools they should, and carry the rules that keep them safe. No
database."""

import re
from pathlib import Path

import pytest
import yaml

from app import project_cli
from app.jobsearch import owner_actions  # noqa: F401  (registers every action)
from app.jobsearch.owner_actions.registry import REGISTRY

HELPERS = sorted(project_cli.AGENTS_SOURCE.glob("*.md"))
GETTING_STARTED = (Path(project_cli.__file__).resolve().parents[3]
                   / "plugins" / "resumebot-setup" / "agents" / "getting-started.md")
DOOR = {f"mcp__resumebot-owner__{t}" for t in ("actions_list", "act_read", "act_do", "act_job")}
ALLOWED = {
    "career-coach": DOOR | {"Read"},
    "master-resume-help": DOOR | {"Read"},
    "profile-draft": DOOR | {"Read", "Write", "WebSearch", "WebFetch"},
}
FORBIDDEN = ["Profile/", "Master/", "match-profile", "content-rules", "_example-"]


def split(path: Path) -> tuple[dict, str]:
    head, body = path.read_text(encoding="utf-8").split("\n---", 1)
    return yaml.safe_load(head.removeprefix("---")), body


def test_there_are_three_helper_agents():
    assert [p.stem for p in HELPERS] == sorted(ALLOWED)


@pytest.mark.parametrize("path", [*HELPERS, GETTING_STARTED], ids=lambda p: p.stem)
def test_the_frontmatter_parses_and_the_name_is_the_file_name(path):
    meta, _ = split(path)
    assert meta["name"] == path.stem
    assert str(meta["description"]).strip()


@pytest.mark.parametrize("path", HELPERS, ids=lambda p: p.stem)
def test_every_action_named_after_act_read_or_act_do_is_in_the_registry_and_none_is_bare(path):
    text = split(path)[1]
    for hit in re.finditer(r"\b(" + "|".join(re.escape(n) for n in REGISTRY) + r")\b", text):
        before = text[max(0, hit.start() - 9):hit.start()]
        assert re.search(r"act\.(?:do|read) `?$", before), f"{path.name}: {hit.group(1)} is not spelled act.read/act.do"
    for action in re.findall(r"\bact\.(?:do|read)\s+`?([a-z_]+\.[a-z_]+)", text):
        assert action in REGISTRY, action


@pytest.mark.parametrize("path", HELPERS, ids=lambda p: p.stem)
def test_the_tools_line_exists_and_is_inside_the_explicit_allow_list_without_bash(path):
    meta, _ = split(path)
    tools = {t.strip() for t in str(meta.get("tools", "")).split(",") if t.strip()}
    assert tools, "a missing tools line grants every tool"
    assert tools <= ALLOWED[path.stem]
    assert "Bash" not in tools


@pytest.mark.parametrize("path", HELPERS, ids=lambda p: p.stem)
def test_no_plugin_only_path_or_example_file_name_is_left_in(path):
    text = path.read_text(encoding="utf-8")
    for word in FORBIDDEN:
        assert word not in text, word


def test_the_profile_agent_holds_its_generic_terms_only_rule_and_the_master_agent_its_own_words_rule():
    profile = (project_cli.AGENTS_SOURCE / "profile-draft.md").read_text(encoding="utf-8")
    assert "Every search and every fetch uses generic role and\nindustry terms only." in profile
    master = (project_cli.AGENTS_SOURCE / "master-resume-help.md").read_text(encoding="utf-8")
    assert "## Own words, quoted" in master


def test_the_coach_asks_a_dry_run_before_its_first_profile_save():
    text = split(project_cli.AGENTS_SOURCE / "career-coach.md")[1]
    assert text.index("dry_run") < text.index("profile.save")


def test_getting_started_runs_the_install_skill_then_the_project_command_then_the_restart():
    text = split(GETTING_STARTED)[1]
    assert "sign the agents in" in text
    assert "connect the app to Claude Code" in text
    assert "5b" not in text
    steps = ("2. **Install the app.**", "3. **Write the helper agents", "4. **Restart.**")
    order = [text.index(step) for step in steps]
    assert order == sorted(order)
    assert "resumebot-setup:onboarding" in text
