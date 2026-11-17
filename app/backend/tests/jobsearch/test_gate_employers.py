"""Gating an employer on its record (path 21): the switch, what it does to roles already on the board, the names
screening reads, and the one-time move of the profile gate's names onto employer records. Everything is made up."""

from datetime import date

from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import check as gating
from app.jobsearch.models import Employer, Opportunity
from tests.support import OWNER, load_profile


def role(company, status, slug):
    return Opportunity(owner=OWNER, slug=slug, company=company, title="Lead", fit=3, origin="scout",
                       match_key=slug, status=status, found=date.today())


def stopped_by_the_employer_gate(rules, company) -> bool:
    """Whether the one gate check stops somebody at this company, and for the employer's gate."""
    none = gating.Subject(company=company, location=None, work_mode=None, comp=None, comp_min=None, comp_max=None,
                          posted=None, found=None)
    stop = gating.check(none, rules)
    return stop is not None and (stop.gate, stop.gate_id) == ("employer", "employer")


async def test_the_roles_a_merge_sends_to_gated_out_for_a_hard_pass_hold_their_stop_as_data(js):
    """The merge of two employer records sends the roles of a hard-passed one to Gated out (`gate_roles`): a stop like
    any other, with its step, its gate, its line and the versions in force (docs/PRD.md 39.12)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all([role("Initech", stages.SECOND_IMPRESSION, "a"), role("Initech", stages.APPLIED, "b")])
        await session.flush()
        await employers.settle(session, OWNER)
        initech = await session.scalar(select(Employer).where(Employer.key == "initech"))
        initech.gated, initech.gated_reason = True, "never again"
        assert await employers.gate_roles(session, OWNER, initech) == 1
        rows = {r.slug: r for r in (await session.scalars(select(Opportunity))).all()}
        assert (rows["a"].status, rows["a"].screened_out_reason) == (stages.GATED_OUT, "employer gated: never again")
        assert (rows["a"].screened_out_by, rows["a"].screened_out_step, rows["a"].screened_out_gate_id) == (
            "employer", "gate", "employer")
        assert (rows["a"].screened_out_hard_pass, rows["a"].screened_out_criteria) == (1, 1)
        assert rows["b"].status == stages.APPLIED


async def test_gating_an_employer_moves_its_roles_not_yet_applied_to_and_screening_reads_it(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all([role("Initech", stages.SECOND_IMPRESSION, "a"), role("Initech", stages.READY_TO_LAUNCH, "b"),
                         role("Initech Corp", stages.APPLIED, "c"), role("Hooli", stages.SECOND_IMPRESSION, "d")])
        await session.flush()
        await employers.settle(session, OWNER)
        initech = await session.scalar(select(Employer).where(Employer.key == "initech"))
        gated = await employers.edit(session, OWNER, initech.id, {"gated": True, "gated_reason": "never again",
                                                                  "aliases": ["Initrode"]})
        rows = {r.slug: r for r in (await session.scalars(select(Opportunity))).all()}
        assert rows["a"].status == rows["b"].status == stages.GATED_OUT
        assert rows["a"].screened_out_reason == "employer gated: never again"
        assert (rows["a"].screened_out_by, rows["a"].screened_out_step, rows["a"].screened_out_gate_id) == (
            "employer", "gate", "employer"), "the stop is data: the gate, the step and the line"
        assert rows["c"].status == stages.APPLIED, "applied roles are left alone"
        assert rows["d"].status == stages.SECOND_IMPRESSION
        assert gated["said"] == "Re-checked 0 postings and 3 roles: 2 newly stopped.", "the save says what it did"
        rules = await gating.rules(session, OWNER)
        for company in ("Initrode Holdings", "initech", "INITECH, Inc."):
            assert stopped_by_the_employer_gate(rules, company), company
        assert not stopped_by_the_employer_gate(rules, "Hooli")
        assert not stopped_by_the_employer_gate(rules, "Initech Staffing"), "a name that only starts alike is another"
        view = employers.view(initech)
        assert view["gated"] is True and view["gated_reason"] == "never again" and view["gated_at"]
        ungated = await employers.edit(session, OWNER, initech.id, {"gated": False})
        after = {r.slug: r for r in (await session.scalars(select(Opportunity))).all()}
        assert (after["a"].status, after["b"].status) == (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH), (
            "un-gating returns what only that gate stopped, to where it would have been")
        assert after["a"].screened_out_reason is None and after["a"].screened_out_step is None
        assert ungated["said"] == "Re-checked 0 postings and 3 roles: 2 now pass."
        assert not stopped_by_the_employer_gate(await gating.rules(session, OWNER), "Initech")
