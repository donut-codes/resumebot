"""The analyst classes a posting's responsibilities too (the owner, 2026-10-16): one item per real duty, classed
Proven, Transferable or Unproven like a required item, kept beside `required` and `preferred`. They count in the
graded requirements as more required items, by the same points; the gate from the classes still reads the required
items only. A posting with no qualifications sends its main duties as required items marked `from_duties`, counted
once. `requeue.reanalyse_all` marks every researched active role to be researched again, and the pool takes them
behind the new finds, the highest fit first. Every name, number and sentence is made up."""

import copy
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app import db
from app.harness import instructions as files
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.gating import role_gates
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.research import analysis, line, prioritize, requeue
from app.jobsearch.writing import packet_writer
from app.models.work import TrailEvent
from tests.builders import POSTING_WORDS, TODAY, analysis_setup, findings, submit_findings
from tests.support import GRADED_ON, OWNER, PROFILE, load_profile

DUTIES = [
    {"item": "Run the quarterly planning cycle", "class": "P", "why": "ran the risk working group", "from": ["b-risk"]},
    {"item": "Build the vendor scorecard", "class": "U"},
]
GRADED = p.Profile.model_validate({"scoring": {"asks": [], "grade_requirements": GRADED_ON}})


async def _waiting() -> uuid.UUID:
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    return ids["Fabrikam"]


async def _role(role_id: uuid.UUID) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


def _score(kept: dict) -> dict:
    role = SimpleNamespace(analysis=kept)
    return scoring.compute({"answers": []}, GRADED, classes=prioritize.classes_of(role),
                           preferred=prioritize.preferred_classes_of(role),
                           responsibilities=prioritize.responsibility_classes_of(role))


def _grade_row(scored: dict) -> dict:
    return next(r for r in scored["asks"] if r["id"] == ps.GRADE_ID)


# --- the shape --------------------------------------------------------------------------------------------------------


async def test_responsibilities_the_analyst_classed_are_kept_beside_the_required_ones(js):
    role_id = await _waiting()
    assert (await submit_findings(findings(role_id, responsibilities=DUTIES)))["taken"] is True
    kept = (await _role(role_id)).analysis["responsibilities"]
    assert [(i["item"], i["class"], i["by"]) for i in kept] == [
        ("Run the quarterly planning cycle", "P", "analyst"), ("Build the vendor scorecard", "U", "analyst")]
    assert kept[0]["from"] == ["b-risk"]


async def test_findings_without_responsibilities_are_taken_and_keep_an_empty_list(js):
    role_id = await _waiting()
    assert (await submit_findings(findings(role_id)))["taken"] is True
    assert (await _role(role_id)).analysis["responsibilities"] == []


async def test_a_proven_responsibility_that_cites_no_master_line_is_refused(js):
    role_id = await _waiting()
    reply = await submit_findings(findings(role_id, responsibilities=[{"item": "Run the cycle", "class": "P"}]))
    assert reply["taken"] is False
    assert "responsibilities item 'Run the cycle' is P but cites no master line" in reply["reason"]


def test_an_analysis_kept_before_responsibilities_still_loads_and_scores_as_one_with_none():
    required = [{"item": "Five years", "class": "P"}, {"item": "A licence", "class": "U"}]
    assert _score({"required": required}) == _score({"required": required, "responsibilities": []})
    role = SimpleNamespace(analysis_state=analysis.DONE, analysis={"required": required})
    assert analysis.view(role)["responsibilities"] == []


# --- the instructions -------------------------------------------------------------------------------------------------


def test_the_instructions_skip_boilerplate_duties_and_never_infer_preferred_items():
    said = files.read("instructions", "jobsearch.analyst")
    assert "`responsibilities`" in said
    for boilerplate in ("other duties as assigned", "complying with policies", "physical requirements"):
        assert boilerplate in said, f"says to skip {boilerplate}"
    assert said.count("Preferred items are only what the posting's own words list as preferred; never infer them.") == 1
    template = files.read("templates", "jobsearch.analysis")
    assert any(x.startswith("responsibilities:") for x in template.splitlines())


def test_the_instructions_make_duties_required_when_the_posting_states_no_qualifications():
    said = files.read("instructions", "jobsearch.analyst")
    assert "When it states none, the main responsibilities stand in as required items" in said
    assert "`from_duties: true`" in said and "do not repeat them in `responsibilities`" in said


async def test_a_duty_standing_in_for_qualifications_is_kept_marked_and_counted_once(js):
    role_id = await _waiting()
    stand_in = {"item": "Run the quarterly planning cycle", "class": "P", "why": "x", "from": ["b-risk"],
                "from_duties": True}
    body = findings(role_id, required=[stand_in], responsibilities=[{**stand_in, "from_duties": False}])
    assert (await submit_findings(body))["taken"] is True
    role = await _role(role_id)
    assert role.analysis["required"][0]["from_duties"] is True
    assert prioritize.responsibility_classes_of(role) is None, "the repeat is not counted again"


# --- the fit ----------------------------------------------------------------------------------------------------------


def test_responsibilities_count_as_more_required_items_by_the_same_points():
    required = [{"item": "Five years", "class": "P"}]
    alone = _grade_row(_score({"required": required}))
    both = _grade_row(_score({"required": required, "responsibilities": [
        {"item": "Duty one", "class": "P"}, {"item": "Duty two", "class": "T"}, {"item": "Duty three", "class": "U"}]}))
    assert (alone["earned"], alone["points"]) == (2.0, 2)
    # Proven +2, Transferable 0, Unproven -2 (GRADED_ON's required numbers), on top of the required item's +2.
    assert (both["earned"], both["points"]) == (2.0, 8)
    assert both["required"] == {"proven": 1, "transferable": 0, "unproven": 0}
    assert both["responsibilities"] == {"proven": 1, "transferable": 1, "unproven": 1}
    assert "responsibilities 1 Proven, 1 Transferable, 1 Unproven" in both["evidence"]


def test_the_required_numbers_the_owner_set_are_the_ones_a_responsibility_earns():
    own = p.Profile.model_validate({"scoring": {"asks": [], "grade_requirements": {
        **GRADED_ON, "required_proven": 5, "required_transferable": 3}}})
    role = SimpleNamespace(analysis={"required": [], "responsibilities": [
        {"item": "Duty one", "class": "P"}, {"item": "Duty two", "class": "T"}]})
    scored = scoring.compute({"answers": []}, own, classes=prioritize.classes_of(role),
                             responsibilities=prioritize.responsibility_classes_of(role))
    assert _grade_row(scored)["earned"] == 8.0


def test_the_gate_from_the_classes_reads_the_required_items_only():
    data = copy.deepcopy(PROFILE)
    gaps = {"id": "gaps", "kind": "rule", "section": "qualifications", "from_classes": "U", "share": 50,
            "text": "Most required items are unproven. Verify: the analyst's classes of the required items."}
    data["gates"] = {**data["gates"], "other": [*data["gates"]["other"], gaps]}
    prof = p.Profile.model_validate(data)
    row = SimpleNamespace(
        company="Northwind", title="Lead", fit=6, location="Oakfield, ST", move_type="onsite",
        comp="$100,000 - $112,500", posted=None, found=None, fit_breakdown=None, target_role=None,
        analysis={"required": [{"item": "Item 1", "class": "P"}, {"item": "Item 2", "class": "P"}],
                  "responsibilities": [{"item": f"Duty {n}", "class": "U"} for n in range(5)]})
    result = {x["id"]: x for x in role_gates.gate_results(row, prof, TODAY)}["gaps"]
    assert result["result"] == "pass"
    assert "0 of 2 required items Unproven" in result["fact"]


# --- the writer -------------------------------------------------------------------------------------------------------


def test_the_composer_is_handed_the_responsibilities_in_the_requirement_match():
    role = SimpleNamespace(analysis={"responsibilities": [
        {"group": "Planning", "item": "Draft the plan", "class": "T", "from": ["b-risk"]}]})
    assert packet_writer.requirement_match(role) == ["responsibilities [Planning]: (T) Draft the plan [b-risk]"]


# --- re-analysis ------------------------------------------------------------------------------------------------------


async def _make(slug, status, fit, state=analysis.DONE, **more) -> uuid.UUID:
    """A Sourced role holding its posting's text, researched unless `state` says otherwise."""
    async with db.session_factory()() as session:
        made = Opportunity(
            owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", fit=fit, origin="scout",
            match_key=f"co {slug}|role {slug}", found=date.today() - timedelta(days=30), status=status,
            analysis_state=state, analysis={"required": [{"item": "Five years", "class": "P"}]} if state else None,
            sourced_at=datetime.now(UTC), description=POSTING_WORDS, **more)
        session.add(made)
        await session.commit()
        return made.id


async def _reanalyse(dry_run=False) -> list[Opportunity]:
    async with db.session_factory()() as session:
        # No asks: saving the profile does not re-score the roles, so their fits stay the order the step reads.
        await load_profile(session, OWNER, scoring={"asks": []})
        roles = await requeue.reanalyse_all(session, OWNER, dry_run=dry_run)
        await session.commit()
        return roles


async def test_reanalyse_all_marks_active_researched_roles_in_fit_order_and_leaves_the_rest(js):
    low = await _make("low", stages.SECOND_IMPRESSION, 2)
    high = await _make("high", stages.READY_TO_LAUNCH, 9)
    mid = await _make("mid", stages.READY_TO_LAUNCH, 5)
    left = [await _make(s, s, 7) for s in (stages.APPLIED, stages.REJECTED, stages.GATED_OUT)]
    never = await _make("never", stages.SECOND_IMPRESSION, 4, state=None)
    marked = await _reanalyse()
    assert [r.id for r in marked] == [high, mid, low]
    ready, deck = stages.READY_TO_LAUNCH, stages.SECOND_IMPRESSION
    for role_id, stage in ((high, ready), (mid, ready), (low, deck)):
        role = await _role(role_id)
        assert role.analysis.get(analysis.REANALYSE) is True
        assert (role.analysis_state, role.status) == (analysis.DONE, stage), "research and stage stay as they were"
    for role_id in [*left, never]:
        assert analysis.REANALYSE not in ((await _role(role_id)).analysis or {})
    async with db.session_factory()() as session:
        lines = (await session.scalars(select(TrailEvent).where(TrailEvent.event == requeue.REANALYSE_EVENT))).all()
    assert len(lines) == 3
    assert await _reanalyse() == [], "safe to run again"


async def test_the_pool_picks_marked_roles_behind_the_new_finds_highest_fit_first(js):
    low = await _make("low", stages.SECOND_IMPRESSION, 2)
    high = await _make("high", stages.READY_TO_LAUNCH, 9)
    new = await _make("new", stages.SECOND_IMPRESSION, None, state=None)
    await _reanalyse()
    async with db.session_factory()() as session:
        picked = await analysis.picks(session, OWNER, None)
        assert [r.id for r in picked] == [new, high, low]
        role = await session.get(Opportunity, high)
        await analysis.start(session, OWNER, role)
        await session.commit()
    role = await _role(high)
    assert role.analysis_state == analysis.WAITING and analysis.REANALYSE not in role.analysis
    assert role.status == stages.READY_TO_LAUNCH, "its stage stays as it is"
    assert line.asked_by_owner(role) is False, "normal priority: not the owner's ask"


async def test_a_dry_run_marks_nothing_and_a_real_run_marks_the_role(js):
    role_id = await _make("a", stages.READY_TO_LAUNCH, 5)
    assert [r.id for r in await _reanalyse(dry_run=True)] == [role_id]
    assert analysis.REANALYSE not in (await _role(role_id)).analysis
    assert [r.id for r in await _reanalyse()] == [role_id]
    assert (await _role(role_id)).analysis[analysis.REANALYSE] is True
