"""Go opens the employer's own posting when it is known (`go_checks.link_of`), and skips a role at an employer the owner
applied to within the owner's number of days (`Settings.days_between_employer_applications`, blank = off): Go's checks,
its refill and the 6:30 tabs, all one path. Synthetic roles and employers only."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import go as apply_go
from app.jobsearch.browser import go_checks
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import numbers as mission_numbers
from app.jobsearch.models import Opportunity, StageMove
from app.jobsearch.pipeline import board
from app.jobsearch.pipeline import row as role_row
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.jobsearch.test_go_checks import _add, _packet, _prepare, _roles, pages, role  # noqa: F401
from tests.support import OWNER, fake_go_window

pytestmark = pytest.mark.usefixtures("example_world")

EMPLOYER_POSTING = "https://jobs.acme.example.test/careers/product-lead-4471"


def _r(**kw) -> Opportunity:
    return Opportunity(owner=OWNER, slug="x", company="X", title="T", **kw)


def test_the_employers_own_posting_is_opened_over_the_board_link():
    r = _r(url="https://www.board.test/jobs/view/123456/", apply_url="https://www.board.test/jobs/view/123456/",
           source_url=EMPLOYER_POSTING, source_kind="employer")
    assert go_checks.link_of(r) == EMPLOYER_POSTING


def test_a_source_that_is_not_the_employers_is_not_preferred():
    r = _r(url="https://www.board.test/jobs/view/123456/", source_url=EMPLOYER_POSTING, source_kind="board")
    assert go_checks.link_of(r) == "https://www.board.test/jobs/view/123456/"
    r = _r(url="https://www.board.test/jobs/view/123456/", source_url="not a link", source_kind="employer")
    assert go_checks.link_of(r) == "https://www.board.test/jobs/view/123456/"


def test_a_bare_careers_home_is_not_preferred_over_a_known_posting_link():
    home = "https://careers.acme.example.test/"
    r = _r(apply_url=home, url=EMPLOYER_POSTING)
    assert go_checks.link_of(r) == EMPLOYER_POSTING
    r = _r(url=EMPLOYER_POSTING, source_url=home, source_kind="employer")
    assert go_checks.link_of(r) == EMPLOYER_POSTING
    assert go_checks.link_of(_r(apply_url=home)) == home, "a home page is still better than nothing"


def _at(slug: str, company: str, fit: int) -> Opportunity:
    made = role(slug, fit=fit)
    made.company, made.match_key = company, f"{company.lower()}|product lead {slug}"
    return made


async def _applied(company: str, days_ago: int) -> None:
    async with db.session_factory()() as session:
        done = Opportunity(owner=OWNER, slug=f"done-{company}-{days_ago}", company=company, title="Other Role",
                           match_key=f"{company.lower()}|other role", status="applied", origin="scout")
        session.add(done)
        await session.flush()
        then = datetime.now(UTC) - timedelta(days=days_ago)
        moves = (await session.scalars(select(StageMove).where(StageMove.role_id == done.id))).all()
        for move in moves:  # the app writes its own move for the new role; this one is the day it became Applied
            move.at = then
        if not moves:
            session.add(StageMove(owner=OWNER, role_id=done.id, from_status=None, to_status="applied", at=then))
        await session.commit()


async def _days(n):
    async with db.session_factory()() as session:
        await mission_numbers.save(session, OWNER, {"days_between_employer_applications": n}, by="owner")
        await session.commit()


async def test_a_role_at_an_employer_applied_to_within_the_days_is_skipped_with_its_reason_and_stays_ready(
        js, pages, monkeypatch):  # noqa: F811
    monkeypatch.setattr(board, "APPLY_BATCH_DEFAULT", 2)
    await _days(14)
    await _applied("Acme", 3)
    await _add(_at("a", "Acme", 9), role("b", fit=8), role("c", fit=7))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["b", "c"], "the batch is refilled from the next role"
    [skipped] = out.skipped
    assert skipped["title"] == "Product Lead a" and skipped["why"] == "Applied to Acme 3 days ago."
    assert (await _roles())["a"].status == "ready_to_launch", "nothing is dropped"


async def test_an_application_under_the_other_spelling_of_the_employers_name_counts_for_the_gap(
        js, pages, monkeypatch):  # noqa: F811
    """Found when a role at "X Insurance Companies" was offered three days after an application to "X Insurance
    Group, Inc.": the two names had two keys, so the gap never saw the earlier application."""
    monkeypatch.setattr(board, "APPLY_BATCH_DEFAULT", 2)
    await _days(14)
    await _applied("Northwind Insurance Group, Inc.", 3)
    await _add(_at("a", "Northwind Insurance Companies", 9), role("b", fit=8))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["b"]
    [skipped] = out.skipped
    assert skipped["why"] == "Applied to Northwind Insurance Companies 3 days ago."
    assert (await _roles())["a"].status == "ready_to_launch", "nothing is dropped"


async def test_the_gap_is_off_by_default_and_an_older_application_does_not_count(js, pages, monkeypatch):  # noqa: F811
    monkeypatch.setattr(board, "APPLY_BATCH_DEFAULT", 3)
    await _applied("Acme", 3)
    await _add(_at("a", "Acme", 9))
    assert [r.slug for r in (await _prepare()).ready] == ["a"], "blank: off"
    await _days(14)
    await _applied("Beta", 20)
    await _add(_at("b", "Beta", 8))
    out = await _prepare()
    assert "b" in [r.slug for r in out.ready] and [s["title"] for s in out.skipped] == ["Product Lead a"]


async def test_go_skips_the_employer_and_opens_the_roles_after_it(js, monkeypatch, pages):  # noqa: F811
    monkeypatch.setattr(board, "APPLY_BATCH_DEFAULT", 1)
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _days(7)
    await _applied("Acme", 2)
    await _add(_at("a", "Acme", 9), role("b", fit=8))
    await _packet("a")
    await _packet("b")
    body = await apply_go.go(OWNER, poll=0, wait=0)
    assert [o["title"] for o in body["opened"]] == ["Product Lead b"] and opened == [
        "https://careers.example.test/open/b"]
    # Replaced behaviour (the best-of queue): a role at an employer applied to within the gap takes no Ready place, so
    # the queue moves it out before Go walks it, and Go no longer skips it with a reason.
    assert (await _roles())["a"].status == "first_impression", "it holds no Ready place until the gap has passed"


async def test_the_setting_is_a_blank_by_default_number_on_the_go_mission():
    assert "days_between_employer_applications" in [n.name for n in mission_numbers.NUMBERS[missions.GO]]
    from app.jobsearch.profile.schema import Settings
    assert Settings().days_between_employer_applications is None


async def test_a_skipped_role_carries_its_reason_on_its_card_and_clears_when_it_passes(js, pages, monkeypatch):  # noqa: F811
    monkeypatch.setattr(board, "APPLY_BATCH_DEFAULT", 2)
    await _days(14)
    await _applied("Acme", 3)
    await _add(_at("a", "Acme", 9), role("b", fit=8), role("c", fit=7), role("d", fit=6))
    await _prepare()
    assert (await _roles())["a"].go_skip == "Applied to Acme 3 days ago."
    async with db.session_factory()() as session:
        cols = {c["key"]: c for c in await role_row.columns(session, OWNER)}
    a = next(c for c in cols["ready_to_launch"]["cards"] if c["title"] == "Product Lead a")
    assert "Applied to Acme 3 days ago." in a["next"], "the card says why Go passed it by"
    await _days(None)  # the gap ends; Go walks the role again and it passes
    await _prepare()
    assert (await _roles())["a"].go_skip is None
