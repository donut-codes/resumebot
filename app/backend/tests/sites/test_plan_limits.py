"""The Limits block of the plan response template (what the scout reads, `doc.read('projects.plan.response')`) says what
the code refuses: change a cap in `app/sites/base.py` and the block together."""

from pathlib import Path

from app.config import settings
from app.sites import base

START, END = "<!-- limits:start -->", "<!-- limits:end -->"


def n(name: str) -> str:
    return f"{getattr(base, name):,}"


def the_block_the_caps_give() -> list[str]:
    return [
        f"- `problem`: at most {n('PLAN_PROBLEM_MAX_CHARS')} characters.",
        f"- `proposal`: 1 to {n('PLAN_PROPOSAL_MAX_ITEMS')} bullets, "
        f"each at most {n('PLAN_PROPOSAL_ITEM_MAX_CHARS')} characters.",
        f"- `method`: at most {n('PLAN_METHOD_MAX_CHARS')} characters.",
        "- `validation`: each of `done_means`, `how_to_check`, `off_plan_means` at most "
        f"{n('VALIDATION_TEXT_MAX_CHARS')} characters; `tools_needed` at most {n('TOOLS_NEEDED_MAX')}, "
        f"each field at most {n('TOOL_FIELD_MAX_CHARS')}.",
        f"- `dropped`: at most {n('DROPPED_MAX')}, each `reason` at most {n('DROPPED_REASON_MAX_CHARS')} characters.",
        f"- `act_guidance`: at most {n('ACT_GUIDANCE_MAX_CHARS')} characters.",
        f"- An item's `data`: at most {n('PLAN_ITEM_DATA_MAX_BYTES')} bytes.",
    ]


def test_the_template_limits_block_says_what_the_code_refuses():
    text = (Path(settings.templates_path) / "projects.plan.response.template.md").read_text(encoding="utf-8")
    block = text.split(START, 1)[1].split(END, 1)[0].strip().splitlines()
    assert block == the_block_the_caps_give()
