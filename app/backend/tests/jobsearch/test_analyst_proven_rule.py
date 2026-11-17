"""The analyst grades Proven narrowly (owner-approved sharpening, 2026-10-20): the stretches that audits found graded
P too readily are named as Transferable, in the requirements step (the stage-one section went with the tournament). On
2026-10-21 the owner eased two of them: a soft trait backed by a cited line or claim, and a years ask the cited lines
meet, are P. No model runs; the instruction text is read."""

from pathlib import Path

import pytest

INSTRUCTIONS = Path(__file__).resolve().parents[3] / "instructions"


def _sections() -> dict[str, str]:
    text = (INSTRUCTIONS / "jobsearch.analyst.instructions.md").read_text(encoding="utf-8")
    step = text.split("4. **The requirements.**", 1)[1].split("**A posting with no qualifications.**", 1)[0]
    return {"requirements step": " ".join(step.split()).lower()}


#: One phrase for each stretch the owner named, plus one more pattern (a named activity, scale or decision
#: right that no cited line shows).
STRETCHES = [
    "practice-level or team-level work for an enterprise-wide",
    "a different field or function",
    "leading analysts",
    "leading managers",
    "years of experience the owner's relevant experience meets are p",
    "a tool not named in a cited line",
    "startup work",
    "soft trait",
    "backed by a cited master line or claim is p",
    "transferable is for a different scale, field, tool, credential",
    "named activity",
]


@pytest.mark.parametrize("stretch", STRETCHES)
def test_each_stretch_is_named_as_transferable_not_proven(stretch):
    assert stretch in _sections()["requirements step"]


def test_the_requirements_step_says_proven_is_narrow_and_a_doubt_is_transferable():
    step = _sections()["requirements step"]
    assert "proven is narrow" in step and "similar tool is t" in step and "when unsure between p and t, t" in step


def test_a_generic_competency_list_item_is_n_beside_the_existing_n_rule_and_a_soft_trait_in_the_roles_words_is_graded():
    step = _sections()["requirements step"]
    assert "employer's generic competency, values or behaviours list" in step
    assert "name: one-sentence definition" in step
    assert step.index("generic competency") < step.index("use n only when")
    assert "a soft trait the posting asks for in its own words for this role" in step and "(g) says" in step

