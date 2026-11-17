"""The analyst classes the posting's preferred items too (the owner, 2026-10-14: preferred qualifications are graded
beside the required ones). Each is Proven, Transferable or Unproven, with the same rules and the same cited master
lines as a required item, and is kept on the analysis beside the required classes. An analysis kept before this holds
none, and its preferred items count as empty. Every name, number and sentence is made up."""

import uuid
from types import SimpleNamespace

import pytest

from app import db
from app.harness import instructions as files
from app.jobsearch.fit import scoring
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.research import analysis, prioritize
from tests.builders import analysis_setup, findings, submit_findings
from tests.support import GRADED_ON, OWNER

#: Two preferred items: one carried over from a master line, one nothing in the master shows.
PREFERRED = [
    {"item": "A second language", "class": "T", "why": "ran the bilingual board briefings", "from": ["b-risk"]},
    {"item": "Grant writing", "class": "U"},
]


async def _waiting() -> uuid.UUID:
    """A Found role the analyst has been handed, as the made-up world has it."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    return ids["Fabrikam"]


async def _role(role_id: uuid.UUID) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id)


# --- what is kept ---------------------------------------------------------------------------------------------------


async def test_preferred_items_the_analyst_classed_are_kept_beside_the_required_ones(js):
    role_id = await _waiting()
    reply = await submit_findings(findings(role_id, preferred=PREFERRED))
    assert reply["taken"] is True
    role = await _role(role_id)
    kept = role.analysis["preferred"]
    assert [(i["item"], i["class"], i["by"]) for i in kept] == [
        ("A second language", "T", "analyst"), ("Grant writing", "U", "analyst")]
    assert (kept[0]["why"], kept[0]["from"]) == ("ran the bilingual board briefings", ["b-risk"])
    assert [i["class"] for i in role.analysis["required"]] == ["P", "U"], "the required classes are as they were"


async def test_findings_that_send_no_preferred_items_keep_an_empty_list(js):
    role_id = await _waiting()
    assert (await submit_findings(findings(role_id)))["taken"] is True
    assert (await _role(role_id)).analysis["preferred"] == []


async def test_researching_a_role_again_replaces_its_preferred_items_with_the_new_findings(js):
    role_id = await _waiting()
    await submit_findings(findings(role_id, preferred=PREFERRED))
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, role_id))
        await session.commit()
    await submit_findings(findings(role_id, preferred=[{"item": "A licence", "class": "U"}]))
    assert [i["item"] for i in (await _role(role_id)).analysis["preferred"]] == ["A licence"]


# --- the same rules as a required item -------------------------------------------------------------------------------


@pytest.mark.parametrize("cls", ["P", "T"])
async def test_a_proven_or_transferable_preferred_item_that_cites_no_master_line_is_refused(js, cls):
    role_id = await _waiting()
    reply = await submit_findings(findings(role_id, preferred=[{"item": "Grant writing", "class": cls}]))
    assert reply["taken"] is False
    assert f"preferred item 'Grant writing' is {cls} but cites no master line: say which (from)" in reply["reason"]
    assert "preferred" not in ((await _role(role_id)).analysis or {}), "nothing was kept"


async def test_an_unproven_preferred_item_needs_no_cited_line(js):
    role_id = await _waiting()
    reply = await submit_findings(findings(role_id, preferred=[{"item": "Grant writing", "class": "U"}]))
    assert reply["taken"] is True


async def test_a_preferred_item_citing_a_master_line_that_is_not_there_is_refused(js):
    role_id = await _waiting()
    bad = [{"item": "A second language", "class": "T", "why": "x", "from": ["no-such-line"]}]
    reply = await submit_findings(findings(role_id, preferred=bad))
    assert reply["taken"] is False
    assert "preferred item 'A second language' cites 'no-such-line', which the master doesn't have" in reply["reason"]


async def test_a_refusal_names_the_required_and_the_preferred_items_that_are_wrong_together(js):
    role_id = await _waiting()
    reply = await submit_findings(findings(
        role_id, required=[{"item": "Five years", "class": "P"}], preferred=[{"item": "A degree", "class": "T"}]))
    assert reply["taken"] is False
    assert "required item 'Five years' is P but cites no master line" in reply["reason"]
    assert "preferred item 'A degree' is T but cites no master line" in reply["reason"]


async def test_a_preferred_item_must_be_one_of_the_three_classes(js):
    role_id = await _waiting()
    with pytest.raises(analysis.AnalysisError, match=r"preferred\.0\.class"):
        await submit_findings(findings(role_id, preferred=[{"item": "A degree", "class": "X"}]))


# --- an analysis kept before preferred items ------------------------------------------------------------------------


def test_an_analysis_kept_before_preferred_items_still_scores_and_counts_the_preferred_ones_as_empty():
    graded = p.Profile.model_validate({"scoring": {"asks": [], "grade_requirements": GRADED_ON}})
    required = [{"item": "Five years", "class": "P", "by": "analyst"}, {"item": "A licence", "class": "U"}]
    scores = []
    for kept in ({"required": required}, {"required": required, "preferred": []}):  # kept before, and kept with none
        role = SimpleNamespace(analysis=kept)
        scores.append(scoring.compute({"answers": []}, graded, classes=prioritize.classes_of(role),
                                      preferred=prioritize.preferred_classes_of(role)))
    assert scores[0] == scores[1], "an old analysis reads as one that sent no preferred items"
    row = next(r for r in scores[0]["asks"] if r["id"] == ps.GRADE_ID)
    assert row["required"] == {"proven": 1, "transferable": 0, "unproven": 1}
    assert row["preferred"] == {"proven": 0, "transferable": 0, "unproven": 0}
    assert scores[0]["earned"] == 0.0, "a proven required item earns 2 and an unproven one costs 2"


async def test_the_role_panels_view_returns_the_preferred_items_and_an_empty_list_for_a_role_researched_before(js):
    role_id = await _waiting()
    await submit_findings(findings(role_id, preferred=PREFERRED))
    role = await _role(role_id)
    assert [(i["item"], i["class"]) for i in analysis.view(role)["preferred"]] == [
        ("A second language", "T"), ("Grant writing", "U")]
    role.analysis = {k: v for k, v in role.analysis.items() if k != "preferred"}  # kept before preferred items
    assert analysis.view(role)["preferred"] == []
    assert [i["class"] for i in analysis.view(role)["required"]] == ["P", "U"]


async def test_a_role_researched_before_is_not_picked_for_research_for_lacking_preferred_classes(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    await submit_findings(findings(ids["Fabrikam"]))  # researched with no preferred items: as one kept before
    async with db.session_factory()() as session:
        picked = await analysis.picks(session, OWNER, None)
    assert sorted(r.company for r in picked) == ["Hooli", "Initech"], "only the roles never researched"


async def test_the_analyst_is_handed_the_shape_that_carries_the_preferred_items(js):
    role_id = await _waiting()
    async with db.session_factory()() as session:
        task = await analysis.brief_part(session, OWNER, await session.get(Opportunity, role_id), "task")
    lines = task["response_template"].splitlines()
    head = next(i for i, x in enumerate(lines) if x.startswith("preferred:"))  # a list beside `required`
    item = lines[head + 1]
    assert item.lstrip().startswith("- {item:") and 'class: "' in item and "from:" in item and "why:" in item, (
        "the same item shape as a required one")
    assert "`preferred`" in task["instructions"]


def test_the_analysts_instructions_say_the_preferred_section_is_classed_and_no_longer_that_it_is_never_counted():
    said = files.read("instructions", "jobsearch.analyst")
    assert "The preferred section is never counted" not in said
    step = next(x for x in said.splitlines() if x.startswith("4. **The requirements.**"))
    assert "preferred" in step and "`preferred`" in step, "sent as `preferred`, beside `required`"
    assert "same" in step, "with the same rules and the same cited lines"
