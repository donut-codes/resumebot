"""The analyst grades each duty bullet of a posting (the owner, 2026-10-19), under the heading the posting lists it
under (`group`), with no cap, skipping only boilerplate; and `reanalysis.mark_stage` / `reanalysis.compare` test that on
the Ready to apply batch alone. Every name, number and sentence is made up."""

import copy
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from app import db
from app.harness import instructions as files
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.gating import role_gates
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import page as roles
from app.jobsearch.pipeline import reanalysis
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.research import analysis, prioritize
from app.jobsearch.writing import packet_writer
from tests.builders import POSTING_WORDS, TODAY, analysis_setup, findings, submit_findings
from tests.support import GRADED_ON, OWNER, PROFILE, load_profile

GRADED = p.Profile.model_validate({"scoring": {"asks": [], "grade_requirements": GRADED_ON}})

#: A made-up posting's duties: two headings, three bullets each, and one boilerplate bullet the analyst leaves out.
POSTING_BULLETS = [
    ("Planning", "Draft the quarterly plan with each team"),
    ("Planning", "Keep the plan's budget current"),
    ("Planning", "Report plan status to the steering group"),
    ("People", "Coach the analysts on the team"),
    ("People", "Run the weekly team meeting"),
    ("People", "Hire for open seats"),
]
BOILERPLATE = "Other duties as assigned"


def _duty(group, item, cls="U", **more):
    return {"group": group, "item": item, "class": cls, **more}


async def _waiting(text: str = POSTING_WORDS) -> uuid.UUID:
    ids = await analysis_setup(text)
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    return ids["Fabrikam"]


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


# --- one graded item per bullet, with its group -----------------------------------------------------------------------


async def test_every_bullet_is_one_stored_item_in_order_with_its_group_and_the_boilerplate_is_not(js):
    role_id = await _waiting()
    sent = [_duty(g, i, "T" if n % 2 else "U", why="close work" if n % 2 else "",
                  **({"from": ["b-risk"]} if n % 2 else {})) for n, (g, i) in enumerate(POSTING_BULLETS)]
    assert (await submit_findings(findings(role_id, responsibilities=sent)))["taken"] is True
    kept = (await _role(role_id)).analysis["responsibilities"]
    assert [(k["group"], k["item"]) for k in kept] == POSTING_BULLETS
    assert BOILERPLATE not in [k["item"] for k in kept]
    assert all(k["by"] == "analyst" for k in kept)


async def test_there_is_no_cap_on_the_number_of_duties(js):
    role_id = await _waiting()
    sent = [_duty(f"Heading {n // 10}", f"Duty number {n}") for n in range(120)]
    assert (await submit_findings(findings(role_id, responsibilities=sent)))["taken"] is True
    assert len((await _role(role_id)).analysis["responsibilities"]) == 120


async def test_a_duty_without_a_group_is_kept_with_an_empty_one(js):
    role_id = await _waiting()
    body = findings(role_id, responsibilities=[{"item": "Lead programs", "class": "U"}])
    assert (await submit_findings(body))["taken"]
    assert (await _role(role_id)).analysis["responsibilities"][0]["group"] == ""


async def test_a_proven_duty_still_needs_its_cited_lines(js):
    role_id = await _waiting()
    reply = await submit_findings(findings(role_id, responsibilities=[_duty("Planning", "Draft the plan", "P")]))
    assert reply["taken"] is False and "is P but cites no master line" in reply["reason"]


async def test_the_role_view_gives_each_duty_with_its_group_and_an_old_analysis_without_one_still_loads(js):
    role_id = await _waiting()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.analysis = {**role.analysis, "responsibilities": [
            {"item": "Old duty", "class": "U", "why": "", "by": "analyst"},
            {"item": "New duty", "class": "U", "why": "", "by": "analyst", "group": " Planning "}]}
        shown = await roles.requirements(session, OWNER, role, "responsibilities")
    assert [(r["item"], r["group"]) for r in shown] == [("Old duty", ""), ("New duty", "Planning")]


# --- a posting's duty bullets are graded: an analysis that grades none is refused -------------------------------------

#: A made-up posting that lists two duty bullets under a heading and states two requirements.
WITH_DUTIES = """Programs Lead

About the job
We need someone to lead the programs team and keep the books in order.

Responsibilities
- Draft the quarterly plan with each team
- Keep the plan's budget current

Requirements
- Five years of programs experience
- A bachelor's degree

What we offer
A fair salary, health cover and four weeks off. Apply on our own site.

About us
""" + "We are a small team that builds programs for the public good. " * 6
REQUIRED = [{"line": 1, "class": "P", "from": ["b-risk"], "why": "the risk working group"}, {"line": 2, "class": "U"}]
PROSE_DUTIES = WITH_DUTIES.replace(
    "- Draft the quarterly plan with each team\n- Keep the plan's budget current",
    "You will draft the quarterly plan and keep its budget current.")
NO_REQUIREMENTS = WITH_DUTIES.replace(
    "Requirements\n- Five years of programs experience\n- A bachelor's degree\n\n", "")


async def test_an_analysis_that_grades_none_of_the_duty_bullets_is_refused_with_the_reason(js):
    role_id = await _waiting(WITH_DUTIES)
    reply = await submit_findings(findings(role_id, required=REQUIRED))
    assert reply["taken"] is False and reply["tries_left"] == 2
    assert ("responsibilities: the posting lists 2 duty bullets under 'Responsibilities' (the first is 'Draft the "
            "quarterly plan with each team') and none is graded") in reply["reason"]
    assert (await _role(role_id)).analysis_state == analysis.WAITING


async def test_grading_one_duty_bullet_is_enough_to_take_the_analysis(js):
    role_id = await _waiting(WITH_DUTIES)
    sent = [_duty("Responsibilities", "Draft the quarterly plan with each team")]
    assert (await submit_findings(findings(role_id, required=REQUIRED, responsibilities=sent)))["taken"] is True


async def test_duties_written_as_prose_need_no_graded_item(js):
    role_id = await _waiting(PROSE_DUTIES)
    reply = await submit_findings(findings(role_id, required=REQUIRED))
    assert reply["taken"] is True, reply
    assert (await _role(role_id)).analysis["responsibilities"] == []


async def test_duties_that_stand_in_as_the_required_items_are_not_asked_for_again(js):
    role_id = await _waiting(NO_REQUIREMENTS)
    stood_in = [{"item": "Draft the quarterly plan with each team", "class": "U", "from_duties": True}]
    assert (await submit_findings(findings(role_id, required=stood_in)))["taken"] is True


# --- the fit counts every item; the gate and the pay read stated required items ---------------------------------------


def test_the_fit_counts_all_the_duties_as_items_by_the_same_points():
    required = [{"item": "Five years", "class": "P"}]
    few = {"required": required, "responsibilities": [_duty("A", "Duty 1", "P")]}
    many = {"required": required,
            "responsibilities": [_duty("A", f"Duty {n}", "P" if n < 4 else "U") for n in range(10)]}

    def row(kept):
        role = SimpleNamespace(analysis=kept)
        scored = scoring.compute({"answers": []}, GRADED, classes=prioritize.classes_of(role),
                                 preferred=prioritize.preferred_classes_of(role),
                                 responsibilities=prioritize.responsibility_classes_of(role))
        return next(r for r in scored["asks"] if r["id"] == ps.GRADE_ID)

    assert row(few)["responsibilities"] == {"proven": 1, "transferable": 0, "unproven": 0}
    assert row(many)["responsibilities"] == {"proven": 4, "transferable": 0, "unproven": 6}
    assert row(many)["earned"] == 2.0 + 4 * 2 - 6 * 2, "Proven +2 and Unproven -2 each, like required items"


def test_grouped_duties_leave_the_required_only_gate_where_it_was():
    data = copy.deepcopy(PROFILE)
    gaps = {"id": "gaps", "kind": "rule", "section": "qualifications", "from_classes": "U", "share": 50,
            "text": "Most required items are unproven. Verify: the analyst's classes of the required items."}
    data["gates"] = {**data["gates"], "other": [*data["gates"]["other"], gaps]}
    row = SimpleNamespace(
        company="Northwind", title="Lead", fit=6, location="Oakfield, ST", move_type="onsite",
        comp="$100,000 - $112,500", posted=None, found=None, fit_breakdown=None, target_role=None,
        analysis={"required": [{"item": "Item 1", "class": "P"}, {"item": "Item 2", "class": "P"}],
                  "responsibilities": [_duty("Planning", f"Duty {n}") for n in range(30)]})
    result = {x["id"]: x for x in role_gates.gate_results(row, p.Profile.model_validate(data), TODAY)}["gaps"]
    assert result["result"] == "pass" and "0 of 2 required items Unproven" in result["fact"]


# --- the instructions and the writer ----------------------------------------------------------------------------------


def test_the_analyst_is_told_one_item_per_bullet_with_its_heading_and_to_skip_only_boilerplate():
    said = files.read("instructions", "jobsearch.analyst")
    assert "one item for every duty bullet" in said and "there is no cap" in said
    assert "as `group`" in said
    for boilerplate in ("other duties as assigned", "complying with policies", "physical requirements"):
        assert boilerplate in said
    template = files.read("templates", "jobsearch.analysis")
    assert any(x.startswith("responsibilities:") for x in template.splitlines())
    assert 'group: "' in template


async def test_the_composer_receives_each_duty_the_analyst_kept(js):
    role_id = await _waiting()
    assert (await submit_findings(findings(role_id, responsibilities=[_duty("Planning", "Draft the plan")])))["taken"]
    found = {k: v for k, v in (await _role(role_id)).analysis.items() if k in ("required", "responsibilities")}
    assert found["responsibilities"][0]["group"] == "Planning"
    assert "responsibilities [Planning]: (U) Draft the plan" in packet_writer.requirement_match(await _role(role_id))


# --- re-analysing only the Ready to apply batch -----------------------------------------------------------------------


async def _make(slug, status, fit, items=2, **more) -> uuid.UUID:
    """A researched role: a Second Impression (`DECK_READ`) that graded `items` requirements, so a role of Ready to
    Launch is in the batch (`board.ready_line` holds only roles read that way: docs/PRD.md 24.13)."""
    required = [{"item": f"Item {n}", "class": "P"} for n in range(items)]
    async with db.session_factory()() as session:
        made = Opportunity(
            owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", fit=fit, origin="scout",
            match_key=f"co {slug}|role {slug}", found=date.today() - timedelta(days=30), status=status,
            analysis_state=analysis.DONE,
            analysis={analysis.DECK_READ: True, "required": required, "at": "2026-10-01T00:00:00+00:00"},
            sourced_at=datetime.now(UTC), description=POSTING_WORDS, **more)
        session.add(made)
        await session.commit()
        return made.id


async def _ready(dry_run=False) -> list[Opportunity]:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        got = await reanalysis.mark_stage(session, OWNER, stages.READY_TO_LAUNCH, dry_run=dry_run)
        await session.commit()
        return got


async def test_reanalyse_ready_marks_only_the_batch_and_stores_a_snapshot_first(js):
    first = await _make("first", stages.READY_TO_LAUNCH, 9)
    other = await _make("other", stages.SECOND_IMPRESSION, 8)  # on deck, not in the Ready to Launch batch
    found = await _make("found", stages.FIRST_IMPRESSION, 3)
    marked = await _ready()
    assert [r.id for r in marked] == [first]
    kept = (await _role(first)).analysis
    assert kept[analysis.REANALYSE] is True
    before = kept[analysis.BEFORE_KEY]
    assert before["items"] == {"required": 2, "preferred": 0, "responsibilities": 0}
    assert before["classes"]["required"] == {"P": 2, "T": 0, "U": 0}
    assert set(before) >= {"fit_pct", "analysis_at"}
    for role_id in (other, found):
        assert analysis.REANALYSE not in (await _role(role_id)).analysis
    assert await _ready() == [], "safe to run again: a role already marked keeps its first snapshot"


async def test_a_dry_run_marks_and_stores_nothing(js):
    role_id = await _make("only", stages.READY_TO_LAUNCH, 9)
    assert [r.id for r in await _ready(dry_run=True)] == [role_id]
    assert analysis.REANALYSE not in (await _role(role_id)).analysis


async def test_marking_second_impression_marks_every_role_there_with_a_snapshot_and_no_other(js):
    one = await _make("one", stages.SECOND_IMPRESSION, 7)
    two = await _make("two", stages.SECOND_IMPRESSION, 4)
    ready = await _make("ready", stages.READY_TO_LAUNCH, 9)
    async with db.session_factory()() as session:
        assert len(await reanalysis.mark_stage(session, OWNER, stages.SECOND_IMPRESSION, dry_run=True)) == 2
    assert analysis.REANALYSE not in (await _role(one)).analysis
    async with db.session_factory()() as session:
        assert len(await reanalysis.mark_stage(session, OWNER, stages.SECOND_IMPRESSION)) == 2
        await session.commit()
    for role_id in (one, two):
        kept = (await _role(role_id)).analysis
        assert kept[analysis.REANALYSE] is True and "fit_pct" in kept[analysis.BEFORE_KEY]
    assert analysis.REANALYSE not in (await _role(ready)).analysis


async def test_the_snapshot_outlives_the_new_research(js):
    role_id = await _waiting()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.analysis = {**role.analysis, analysis.BEFORE_KEY: {"fit_pct": 40}}
        await session.commit()
    assert (await submit_findings(findings(role_id)))["taken"] is True
    assert (await _role(role_id)).analysis[analysis.BEFORE_KEY] == {"fit_pct": 40}


async def _compared() -> list[dict]:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        return await reanalysis.compare(session, OWNER)


async def test_compare_waits_for_the_new_research_then_shows_old_and_new(js):
    kept = await _make("kept", stages.READY_TO_LAUNCH, 9, items=3)
    await _ready()
    said = reanalysis.table(await _compared())
    assert "Role kept at Co kept" in said and "waiting" in said and "1 of 1 roles are not re-analysed yet" in said
    # The new research arrives: more items, graded duties, a poorer fit.
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, kept)
        before = role.analysis[analysis.BEFORE_KEY]
        role.analysis = {"required": [{"item": "Item 0", "class": "U"}],
                         "responsibilities": [_duty("Planning", f"Duty {n}") for n in range(5)],
                         "at": "2026-10-19T00:00:00+00:00", analysis.BEFORE_KEY: before}
        role.fit = 0
        await session.commit()
    rows = await _compared()
    assert len(rows) == 1 and rows[0]["after"]["items"] == {"required": 1, "preferred": 0, "responsibilities": 5}
    assert rows[0]["before"]["items"]["required"] == 3
    said = reanalysis.table(rows)
    assert "old fit" in said and "new fit" in said and "3/0/0" in said and "1/0/5" in said
    assert "not re-analysed yet" not in said and "drafting bar" not in said


def test_the_table_shows_old_and_new_fit_and_items_and_counts_the_roles_still_waiting():
    def m(pct, req, duties):
        return {"fit_pct": pct, "items": {"required": req, "preferred": 1, "responsibilities": duties}}

    rows = [{"role": "Lead at Acme", "before": m(70, 5, 7), "after": m(82, 5, 31)},
            {"role": "Analyst at Zed", "before": m(90, 4, 6), "after": None}]
    said = reanalysis.table(rows)
    assert "70%" in said and "82%" in said and "5/1/7" in said and "5/1/31" in said
    assert "1 of 2 roles are not re-analysed yet" in said

