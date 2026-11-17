"""The analyst answers every line of a posting's requirement sections, cut by code (docs/PRD.md 26.12): each as P, T or
U, or N when a resume can't measure it. N is in no count. Every name, number and sentence is made up."""

import uuid
from types import SimpleNamespace

from app import db
from app.jobsearch.fit import scoring
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import page as roles
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.profile.gates import Gate
from app.jobsearch.research import analysis, prioritize
from tests.builders import analysis_setup, findings, submit_findings
from tests.support import GRADED_ON, OWNER

GRADED = p.Profile.model_validate({"scoring": {"asks": [], "grade_requirements": GRADED_ON}})

POSTING = """Programs Lead

About the job
We need someone to lead the programs team and keep the books in order.

Requirements:Education:
A bachelor's degree in a related field.
Experience:
Five years of programs experience
Willingness to travel one week a quarter

Preferred Qualifications:
- A second language

What we offer
A fair salary and four weeks off. Apply on our own site.

About us
""" + "We are a small team that builds programs for the public good. " * 6

ANSWERS = [
    {"line": 1, "class": "U"},
    {"line": 2, "class": "P", "from": ["b-risk"], "why": "the risk working group"},
    {"line": 3, "class": "N", "why": "a willingness can't be shown by a resume line"},
]
PREFERRED = [{"line": 4, "class": "T", "from": ["b-risk"], "why": "ran the bilingual briefings"}]


async def _waiting(text: str = POSTING) -> uuid.UUID:
    ids = await analysis_setup(text)
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    return ids["Fabrikam"]


async def _role(role_id: uuid.UUID) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id)


async def test_the_task_carries_the_numbered_lines_by_part(js):
    role_id = await _waiting()
    async with db.session_factory()() as session:
        task = await analysis.brief_part(session, OWNER, await session.get(Opportunity, role_id), "task")
    got = task["requirement_lines"]
    assert got["required"] == [{"line": 1, "text": "A bachelor's degree in a related field."},
                               {"line": 2, "text": "Five years of programs experience"},
                               {"line": 3, "text": "Willingness to travel one week a quarter"}]
    assert got["preferred"] == [{"line": 4, "text": "A second language"}]


async def test_every_line_answered_is_kept_with_the_lines_own_words_and_its_number(js):
    role_id = await _waiting()
    reply = await submit_findings(findings(role_id, required=ANSWERS, preferred=PREFERRED))
    assert reply["taken"] is True, reply
    kept = (await _role(role_id)).analysis
    assert [(i["line"], i["item"], i["class"]) for i in kept["required"]] == [
        (1, "A bachelor's degree in a related field.", "U"), (2, "Five years of programs experience", "P"),
        (3, "Willingness to travel one week a quarter", "N")]
    assert [(i["line"], i["item"]) for i in kept["preferred"]] == [(4, "A second language")]
    assert [x["n"] for x in kept["lines"]] == [1, 2, 3, 4]


async def test_the_role_page_gets_each_items_line_number_and_its_n_class(js):
    role_id = await _waiting()
    await submit_findings(findings(role_id, required=ANSWERS, preferred=PREFERRED))
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        rows = await roles.requirements(session, OWNER, role)
    assert [(r["line"], r["class"]) for r in rows] == [(1, "U"), (2, "P"), (3, "N")]
    assert rows[2]["why"] == "a willingness can't be shown by a resume line"


async def test_a_reworded_item_is_kept_as_the_lines_words(js):
    role_id = await _waiting()
    said = [{**ANSWERS[0], "item": "Degree"}, *ANSWERS[1:]]
    assert (await submit_findings(findings(role_id, required=said, preferred=PREFERRED)))["taken"] is True
    assert (await _role(role_id)).analysis["required"][0]["item"] == "A bachelor's degree in a related field."


async def test_a_skipped_line_is_refused_and_named(js):
    role_id = await _waiting()
    reply = await submit_findings(findings(role_id, required=ANSWERS[:2], preferred=PREFERRED))
    assert reply["taken"] is False
    assert "required line 3 ('Willingness to travel one week a quarter') is not answered" in reply["reason"]


async def test_a_line_answered_twice_or_added_is_refused(js):
    role_id = await _waiting()
    twice = [*ANSWERS, {"line": 2, "class": "U"}, {"line": 9, "class": "U"}]
    reply = await submit_findings(findings(role_id, required=twice, preferred=PREFERRED))
    assert reply["taken"] is False
    assert "required line 2 is answered 2 times" in reply["reason"]
    assert "answers line 9, which is not a required line" in reply["reason"]


async def test_an_item_with_no_line_is_refused_when_the_posting_has_lines(js):
    role_id = await _waiting()
    reply = await submit_findings(findings(role_id, required=[*ANSWERS, {"item": "Extra", "class": "U"}],
                                           preferred=PREFERRED))
    assert reply["taken"] is False and "item 'Extra' has no `line`" in reply["reason"]


async def test_a_preferred_line_answered_in_required_is_refused(js):
    role_id = await _waiting()
    reply = await submit_findings(findings(role_id, required=[*ANSWERS, PREFERRED[0]], preferred=[]))
    assert reply["taken"] is False
    assert "answers line 4, which is not a required line" in reply["reason"]
    assert "preferred line 4 ('A second language') is not answered" in reply["reason"]


async def test_a_not_measurable_line_needs_a_reason_and_cites_nothing(js):
    role_id = await _waiting()
    bad = [*ANSWERS[:2], {"line": 3, "class": "N", "from": ["b-risk"]}]
    reply = await submit_findings(findings(role_id, required=bad, preferred=PREFERRED))
    assert reply["taken"] is False
    assert "is N: say in a line why it can't be measured" in reply["reason"]
    assert "is N, not measurable, so it cites no master line" in reply["reason"]


async def test_a_posting_with_no_requirement_section_keeps_the_analysts_own_list(js):
    role_id = await _waiting("Programs Lead\n\nAbout the job\n" + "We build programs for the public good. " * 20)
    reply = await submit_findings(findings(role_id))
    assert reply["taken"] is True
    kept = (await _role(role_id)).analysis
    assert [i["item"] for i in kept["required"]] == ["Five years of programs", "A certification"]
    assert "lines" not in kept
    async with db.session_factory()() as session:
        task = await analysis.brief_part(session, OWNER, await session.get(Opportunity, role_id), "task")
    assert "requirement_lines" not in task


async def test_a_line_that_cannot_be_measured_is_in_no_count(js):
    kept = {"required": [{"item": "a", "class": "P"}, {"item": "b", "class": "N"}, {"item": "c", "class": "U"}],
            "preferred": [{"item": "d", "class": "N"}]}
    role = SimpleNamespace(analysis=kept)
    assert prioritize.classes_of(role) == ["P", "U"]
    assert prioritize.preferred_classes_of(role) is None


def test_the_twenty_percent_proven_gate_counts_only_the_lines_a_resume_can_measure():
    gate = Gate.model_validate({"id": "g", "kind": "rule", "section": "qualifications", "from_classes": "PT",
                                  "share": 50, "text": "Half must be proven or transferable."})
    result, fact = scoring.class_gate(gate, ["P", "N", "N", "N", "U"])
    assert result == "pass" and "1 of 2 required items Proven or Transferable, 50%" in fact


async def test_the_analyst_is_told_to_answer_every_numbered_line_with_n_for_the_unmeasurable(js):
    from app.harness import instructions as files
    said = files.read("instructions", "jobsearch.analyst")
    assert "requirement_lines" in said and "every line" in said and "N" in said
    shape = files.read("templates", "jobsearch.analysis")
    assert "line: 1" in shape and 'class: "N"' in shape


def test_a_line_that_cannot_be_measured_adds_nothing_earned_and_nothing_possible():
    kept = {"required": [{"item": "a", "class": "P"}, {"item": "b", "class": "N"}, {"item": "c", "class": "N"}],
            "preferred": [{"item": "d", "class": "N"}]}
    role = SimpleNamespace(analysis=kept)
    scored = scoring.compute({"answers": []}, GRADED, classes=prioritize.classes_of(role),
                             preferred=prioritize.preferred_classes_of(role))
    row = next(r for r in scored["asks"] if r["id"] == ps.GRADE_ID)
    assert row["required"] == {"proven": 1, "transferable": 0, "unproven": 0}
    assert row["earned"] == 2.0 and row["points"] == 2.0
