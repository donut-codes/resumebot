"""Whether a role in Followup needed is still worth chasing the posting of (docs/PRD.md 32.6, the owner, 2026-11-04):
the days since it was posted plus a margin must still pass the owner's freshness hard pass. The pure rule, with made-up
gate lines; the queue that uses it is in test_browser_work.py. Every name here is made up."""

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import reader as br
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import chase
from app.jobsearch.profile.gates import Gate, GateSet
from app.jobsearch.research import analysis
from tests.builders import browser_world
from tests.jobsearch.test_browser_work import FRESH_PASS, _days_ago, _hard_pass, _role
from tests.support import DEFAULT_OWNER as OWNER


def must(op: str = "<=", value=15) -> Gate:
    """The Freshness Must dial: the days since the role was posted, `op` `value`."""
    return Gate(id="must-freshness", kind="job_fact", section="posting", fact="posted_days", op=op, value=value)


def age_line(days: int | None = 15) -> Gate:
    return Gate(id="aging", kind="posting_age", section="posting", days=days)


@pytest.mark.parametrize("age, chased", [(0, True), (12, True), (13, False), (15, False), (40, False)])
def test_with_a_pass_of_15_days_and_the_default_margin_of_3_a_role_up_to_12_days_old_is_chased(age, chased):
    assert chase.fresh_after([must()], age, 3) is chased


@pytest.mark.parametrize("age, chased", [(15, True), (16, False)])
def test_a_margin_of_zero_chases_a_role_until_the_pass_would_close_it(age, chased):
    assert chase.fresh_after([must()], age, 0) is chased


def test_a_pass_stored_as_text_is_read_as_a_number():
    """The page stores a dial's value as typed, so a live pass of 15 days holds the text "15"."""
    assert chase.fresh_after([must(value="15")], 12, 3) is True
    assert chase.fresh_after([must(value="15")], 13, 3) is False


def test_the_pass_is_read_as_its_own_comparison_so_under_15_days_leaves_one_day_less():
    assert chase.fresh_after([must("<", 15)], 11, 3) is True, "14 days is under 15"
    assert chase.fresh_after([must("<", 15)], 12, 3) is False, "15 days is not under 15"


def test_a_posting_age_line_is_read_the_same_way():
    assert chase.fresh_after([age_line(15)], 12, 3) is True
    assert chase.fresh_after([age_line(15)], 13, 3) is False


def test_with_two_freshness_lines_the_stricter_decides():
    assert chase.fresh_after([must(value=30), must(value=10)], 8, 3) is False
    assert chase.fresh_after([must(value=30), age_line(10)], 8, 3) is False
    assert chase.fresh_after([must(value=30), must(value=10)], 7, 3) is True


def test_with_no_freshness_line_or_no_known_age_nothing_says_the_role_is_too_old():
    assert chase.fresh_after([], 400, 3) is True, "no freshness hard pass: every role is chased"
    assert chase.fresh_after([must()], None, 3) is True, "an age nobody knows stops nothing"
    assert chase.fresh_after([age_line()], None, 3) is True


def test_a_line_that_is_not_a_number_to_compare_waits_and_stops_nothing():
    assert chase.fresh_after([must(value="soon")], 40, 3) is True


def gates(*lines: Gate, off: list[str] | None = None) -> GateSet:
    return GateSet(other=list(lines), sections_off=off or [])


def test_the_freshness_lines_of_a_role_are_the_pass_and_posting_age_lines_it_is_checked_against():
    other = Gate(id="travel", kind="rule", text="Travel of 20% or more.")
    both = gates(must(), age_line(30), other)
    assert [g.id for g in chase.freshness_lines(both, None)] == ["must-freshness", "aging"]
    assert chase.freshness_lines(gates(other), None) == [], "no freshness hard pass"
    assert chase.freshness_lines(gates(age_line(None)), None) == [], "a posting age with no days says nothing"


def test_a_part_that_is_switched_off_has_no_freshness_line_and_a_roles_own_line_is_for_that_role_only():
    assert chase.freshness_lines(gates(must(), off=["posting"]), None) == [], "an off part stops nothing"
    own = gates(Gate(id="own-freshness", kind="job_fact", section="posting", fact="posted_days", op="<=", value=5,
                     roles=["builders"]))
    assert [g.id for g in chase.freshness_lines(own, "builders")] == ["own-freshness"]
    assert chase.freshness_lines(own, "ops") == [] and chase.freshness_lines(own, None) == []


# --- a role the owner asked research for is chased at any age (PRD 32.6, 48.14; the owner, 2026-11-13) ---------------

OWNER_ASK = {analysis.ASKED_BY: analysis.ASKED_BY_OWNER}


async def _wanted(**kwargs) -> set[str]:
    async with db.session_factory()() as session:
        found = {r.id for r in await chase.wanted(session, OWNER, **kwargs)}
        return set((await session.scalars(select(Opportunity.slug).where(Opportunity.id.in_(found)))).all())


async def _old_roles() -> None:
    await browser_world()
    await _hard_pass(FRESH_PASS)
    await _role(1, status="followup_needed", posted=_days_ago(100), analysis=OWNER_ASK)
    await _role(2, status="followup_needed", posted=_days_ago(100))
    await _role(3, status="followup_needed", posted=_days_ago(100), analysis={"asked_by": "scout"})


async def test_a_role_the_owner_asked_research_for_is_chased_at_any_age(js):
    await _old_roles()
    assert await _wanted() == {"role-1"}, "the ask is chased; the same age with no ask, or another's ask, is not"


async def test_a_role_the_owner_asked_research_for_is_chased_at_any_age_in_the_browsers_plain_rows(js):
    await _old_roles()
    assert await _wanted(columns=br.PLAN_ROLE_COLUMNS) == {"role-1"}


async def test_a_role_the_owner_asked_research_for_stays_sourced_not_listed_while_the_job_board_is_read(js):
    await _old_roles()
    await _hard_pass(FRESH_PASS, read_robots_exempt=True)
    assert await _wanted(listing_exempt=True, columns=br.PLAN_ROLE_COLUMNS) == {"role-1"}


async def test_a_role_in_followup_needed_that_is_too_old_and_not_asked_for_is_still_left_out(js):
    await browser_world()
    await _hard_pass(FRESH_PASS)
    await _role(1, status="followup_needed", posted=_days_ago(100))
    await _role(2, status="followup_needed", posted=_days_ago(5))
    assert await _wanted() == {"role-2"}
