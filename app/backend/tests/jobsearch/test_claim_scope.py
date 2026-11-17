"""Scope inflation and the repeated descriptor, as words only (app/jobsearch/writing/claim_scope.py), and as packet
checks.
Everything is made up."""

import copy

import pytest

from app.jobsearch.profile import documents
from app.jobsearch.writing import claim_scope as cs
from app.jobsearch.writing import packets
from tests.builders import RESUME

SRC = "Rolled out three tools company-wide; aimed to cut intake time, supported the review board"


@pytest.mark.parametrize("text, source", [
    ("Reduced intake time by rolling out the three tools", "Cut intake time with three tools"),
    ("Directed the review board", "Led the review board"),
    ("Headed a team of 5", "Led a five-person team"),
    ("Lowered costs", "Reduced costs"),
    ("Shortened the review cycle", "Cut the review cycle"),
    ("Built the knowledge base", "Built the practice knowledge base"),
    ("Three-month engagement", "An engagement of 3 months"),
])
def test_plain_rewording_is_not_inflation(text, source):
    assert cs.inflation(text, source) == []


@pytest.mark.parametrize("text, fragment", [
    ("The whole firm uses the three tools", "'whole'"),
    ("Rolled out five tools", "the number 5"),
    ("Cut intake time by 30%", "the number 30"),
    ("Cut intake time across the three tools", "a result (reduce)"),
    ("Led the review board", "leads the work"),
    ("Rolled out tools to every team", "'every'"),
    ("Rolled out three tools company-wide, doubling adoption", "a result (multiply)"),
])
def test_a_line_that_claims_more_than_its_source_is_flagged(text, fragment):
    got = cs.inflation(text, SRC)
    assert any(fragment in g for g in got), got


def test_company_wide_in_both_is_fine_and_no_source_means_no_flag():
    assert cs.inflation("Rolled out the tools company-wide", SRC) == []
    assert cs.inflation("Cut everything by 90%", "") == []


@pytest.mark.parametrize("line", [
    "Three-month consulting engagement.", "Consulting engagement", "A consulting engagement lasting months",
])
def test_a_line_that_only_restates_the_descriptor_repeats_it(line):
    assert cs.repeats("Consulting engagement", line)


@pytest.mark.parametrize("line", [
    "Led the consulting engagement for a regional bank, cutting review time",
    "Built a knowledge base",
])
def test_a_line_that_says_more_does_not_repeat_it(line):
    assert not cs.repeats("Consulting engagement", line)


# --- as packet checks ----------------------------------------------------------------------------------------------


def _docs(descriptor="") -> documents.Documents:
    master = copy.deepcopy(RESUME)
    master["roles"][0]["descriptor"] = descriptor
    master["roles"][0]["bullets"][1]["note"] = "Planned, not yet delivered"
    return documents.Documents.model_validate({"resume": master, "writing": {}})


def _run(intro="", bullet="Led the risk working group across client engagements", cites=("b-risk",),
         descriptor="") -> dict[str, dict]:
    cut = packets.ResumeCut.model_validate({
        "headline": "Programs Leader", "summary": "Builds programs that people use, across client engagements.",
        "summary_from": ["b-risk"], "competencies": [{"area": "Governance", "items": ["Risk Frameworks"]}],
        "roles": [{"role": "northwind-lead", "intro": intro, "bullets": [{"text": bullet, "from": list(cites)}]}]})
    checks = packets.packet_checks(cut, ["Dear hiring team,", "I would like to help."], ["b-risk"], _docs(descriptor))
    return {c["check"]: c for c in checks}


def test_a_clean_packet_passes_both_and_inflation_is_a_warning_not_a_block():
    got = _run()
    assert got[cs.INFLATION_CHECK]["ok"] and got[cs.REPEAT_CHECK]["ok"]
    got = _run(bullet="Led the risk working group across every client engagement")
    row = got[cs.INFLATION_CHECK]
    assert row["ok"] is False and row["blocking"] is False
    assert "bullet 1 under Senior Manager at Northwind Labs" in row["detail"] and "'every'" in row["detail"]
    assert "b-risk" not in row["detail"], "a check names no master id"


def test_a_scope_line_that_repeats_the_printed_descriptor_blocks():
    got = _run(intro="Three-month consulting engagement.", descriptor="Consulting engagement")
    row = got[cs.REPEAT_CHECK]
    assert row["ok"] is False and row["blocking"] is True
    assert "scope line under Senior Manager at Northwind Labs" in row["detail"]
    assert _run(intro="Three-month consulting engagement.")[cs.REPEAT_CHECK]["ok"], "no descriptor, nothing to repeat"
