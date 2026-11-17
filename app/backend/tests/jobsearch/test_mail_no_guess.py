"""A mail that names no job title is not guessed onto a role (`mail/match.py`, docs/PRD.md 14.5; the owner, 2026-11-04):
it may move a role to Applied only when that role is one the owner applied to, one Launch opened, or one in Ready to
Launch, and it may end only a role in Applied or Active Discussion. With no such role, or more than one, it asks.
Pure code, no database: made-up employers, roles and senders."""

import uuid
from datetime import UTC, date, datetime

import pytest

from app.jobsearch import stages
from app.jobsearch.mail import kinds as mail_kinds
from app.jobsearch.mail import match as mail_match
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.models import Opportunity
from app.mailmine.message import Seen, sender_key
from tests.jobsearch.made_up_sites_b import made_up_boards, made_up_mail  # noqa: F401 (used through pytestmark)

pytestmark = pytest.mark.usefixtures("made_up_mail")

GLOBEX = "Globex Corporation"
#: A known job system, which sends mail from its own domain.
JOB_SYSTEM = "tenantonemail.test"
CONFIRM, REJECT = mail_kinds.CONFIRMATION, mail_kinds.REJECTION


def seen(subject: str = "Globex Corporation - Update on your application", text: str = "",
         address: str = f"no-reply@{JOB_SYSTEM}") -> Seen:
    """A job system's mail (a known system, so it may name an employer's role)."""
    return Seen(sender_key=sender_key(address), sender_name="Globex Corporation", subject=subject,
                day=date(2026, 11, 4), text=text, bulk=False, address=address)


def candidate(title: str, status: str, *, opened: bool = False, applied_before: bool = False) -> mail_match.Candidate:
    """A role in play at Globex."""
    role = Opportunity(id=uuid.uuid4(), owner="o", slug=f"globex-{title}", company=GLOBEX, title=title, fit=3,
                       origin="scout", match_key=f"globex|{title}".lower(), status=status,
                       go_opened_at=datetime(2026, 11, 3, tzinfo=UTC) if opened else None)
    return mail_match.Candidate(role, go_mail.employer_names([GLOBEX]), frozenset(), applied_before=applied_before)


def decide(kind: str | None, *roles: mail_match.Candidate, mail: Seen | None = None) -> mail_match.Match:
    return mail_match.match(mail or seen(), list(roles), kind=kind)


# --- a confirmation that names no title -------------------------------------------------------------------------------


def test_a_confirmation_naming_no_title_does_not_move_a_role_found_by_a_search_and_never_applied_to():
    sales = candidate("Sales Manager", stages.FOLLOWUP_NEEDED)
    found = decide(CONFIRM, sales)
    assert found.role is None and found.candidates == [sales.role] and not found.unverified


@pytest.mark.parametrize("status", [stages.LANDING_ZONE, stages.CLEANSED_DATA, stages.FIRST_IMPRESSION,
                                    stages.SECOND_IMPRESSION, stages.GONE_QUIET])
def test_a_confirmation_naming_no_title_asks_for_a_role_that_is_neither_launched_nor_applied_to(status):
    only = candidate("Sales Manager", status)
    found = decide(CONFIRM, only)
    assert found.role is None and found.candidates == [only.role]


def test_a_confirmation_naming_no_title_finds_the_one_role_in_ready_to_launch():
    ready, other = candidate("Sales Manager", stages.READY_TO_LAUNCH), candidate("Analyst", stages.FOLLOWUP_NEEDED)
    assert decide(CONFIRM, ready, other).role is ready.role


def test_a_confirmation_naming_no_title_finds_the_one_role_launch_opened_though_it_left_ready_to_launch():
    opened, other = candidate("Sales Manager", stages.SECOND_IMPRESSION, opened=True), \
        candidate("Analyst", stages.FOLLOWUP_NEEDED)
    assert decide(CONFIRM, opened, other).role is opened.role


def test_a_confirmation_naming_no_title_finds_the_one_gone_quiet_role_the_owner_applied_to():
    ghosted = candidate("Sales Manager", stages.GONE_QUIET, applied_before=True)
    assert decide(CONFIRM, ghosted, candidate("Analyst", stages.GONE_QUIET)).role is ghosted.role


def test_a_confirmation_naming_no_title_asks_when_two_roles_could_be_the_one_applied_to():
    a, b = candidate("Sales Manager", stages.READY_TO_LAUNCH), candidate("Analyst", stages.READY_TO_LAUNCH)
    found = decide(CONFIRM, a, b)
    assert found.role is None and {r.id for r in found.candidates} == {a.role.id, b.role.id}


def test_a_role_already_applied_to_counts_as_one_the_confirmation_may_be_about():
    applied, ready = candidate("Sales Manager", stages.APPLIED), candidate("Analyst", stages.READY_TO_LAUNCH)
    found = decide(CONFIRM, applied, ready)
    assert found.role is None and {r.id for r in found.candidates} == {applied.role.id, ready.role.id}


def test_a_confirmation_naming_the_title_still_finds_its_role_whatever_its_stage():
    sales, other = candidate("Sales Manager", stages.FOLLOWUP_NEEDED), candidate("Analyst", stages.READY_TO_LAUNCH)
    found = decide(CONFIRM, sales, other, mail=seen(text="We have received your application for Sales Manager."))
    assert found.role is sales.role


# --- a rejection that names no title ----------------------------------------------------------------------------------


def test_a_rejection_naming_no_title_does_not_end_a_role_found_by_a_search():
    sales = candidate("Sales Manager", stages.FOLLOWUP_NEEDED)
    found = decide(REJECT, sales)
    assert found.role is None and found.candidates == [sales.role]


@pytest.mark.parametrize("status", [stages.READY_TO_LAUNCH, stages.SECOND_IMPRESSION])
def test_a_rejection_naming_no_title_asks_for_a_role_that_is_not_applied_or_in_active_discussion(status):
    only = candidate("Sales Manager", status, opened=True, applied_before=True)
    found = decide(REJECT, only)
    assert found.role is None and found.candidates == [only.role]


def test_a_rejection_naming_no_title_asks_for_a_gone_quiet_role_the_owner_never_applied_to():
    closed = candidate("Sales Manager", stages.GONE_QUIET)
    found = decide(REJECT, closed)
    assert found.role is None and found.candidates == [closed.role]


def test_a_rejection_naming_no_title_ends_the_one_gone_quiet_role_the_owner_applied_to():
    """docs/PRD.md 14.6: a rejection takes a role Gone Quiet after no word came back to Rejected."""
    ghosted, searched = candidate("Sales Manager", stages.GONE_QUIET, applied_before=True), \
        candidate("Analyst", stages.FOLLOWUP_NEEDED)
    assert decide(REJECT, ghosted, searched).role is ghosted.role


def test_a_rejection_naming_no_title_asks_when_an_applied_role_sits_beside_one_gone_quiet_after_applying():
    applied, ghosted = candidate("Account Executive", stages.APPLIED), \
        candidate("Sales Manager", stages.GONE_QUIET, applied_before=True)
    found = decide(REJECT, applied, ghosted, candidate("Analyst", stages.GONE_QUIET),
                   candidate("Designer", stages.FIRST_IMPRESSION))
    assert found.role is None and {r.id for r in found.candidates} == {applied.role.id, ghosted.role.id}


def test_a_rejection_naming_no_title_ends_the_applied_role_beside_a_gone_quiet_one_never_applied_to():
    applied, closed = candidate("Account Executive", stages.APPLIED), candidate("Sales Manager", stages.GONE_QUIET)
    assert decide(REJECT, applied, closed).role is applied.role


@pytest.mark.parametrize("status", [stages.APPLIED, stages.ACTIVE_DISCUSSION])
def test_a_rejection_naming_no_title_ends_the_one_role_applied_or_in_active_discussion(status):
    live, searched = candidate("Account Executive", status), candidate("Sales Manager", stages.FOLLOWUP_NEEDED)
    assert decide(REJECT, live, searched).role is live.role


def test_a_rejection_naming_no_title_asks_when_two_roles_are_applied_or_in_active_discussion():
    a, b = candidate("Account Executive", stages.APPLIED), candidate("Analyst", stages.ACTIVE_DISCUSSION)
    found = decide(REJECT, a, b, candidate("Sales Manager", stages.FOLLOWUP_NEEDED))
    assert found.role is None and {r.id for r in found.candidates} == {a.role.id, b.role.id}


def test_a_rejection_naming_the_title_still_ends_that_role_whatever_its_stage():
    sales, live = candidate("Sales Manager", stages.FOLLOWUP_NEEDED), candidate("Analyst", stages.APPLIED)
    found = decide(REJECT, sales, live, mail=seen(text="We will not be moving forward with Sales Manager."))
    assert found.role is sales.role


def test_a_rejection_naming_the_longer_title_is_about_the_longer_one():
    lead, senior = candidate("Product Lead", stages.APPLIED), candidate("Senior Product Lead", stages.FOLLOWUP_NEEDED)
    found = decide(REJECT, lead, senior, mail=seen(text="Your application for Senior Product Lead."))
    assert found.role is senior.role


# --- what the rule leaves alone ---------------------------------------------------------------------------------------


def test_a_mail_of_no_kind_or_of_another_kind_keeps_the_one_role_at_the_employer_rule():
    sales = candidate("Sales Manager", stages.FOLLOWUP_NEEDED)
    for kind in (None, mail_kinds.INTERVIEW, mail_kinds.OFFER, mail_kinds.AMBIGUOUS):
        assert decide(kind, sales).role is sales.role


def test_two_roles_at_the_employer_and_no_title_in_the_mail_still_ask_for_every_kind():
    a, b = candidate("Sales Manager", stages.FOLLOWUP_NEEDED), candidate("Analyst", stages.FOLLOWUP_NEEDED)
    found = decide(mail_kinds.INTERVIEW, a, b)
    assert found.role is None and {r.id for r in found.candidates} == {a.role.id, b.role.id}


def test_a_mail_from_a_stranger_still_asks_whatever_the_kind():
    ready = candidate("Sales Manager", stages.READY_TO_LAUNCH)
    found = decide(CONFIRM, ready, mail=seen(address="digest@jobnews.example", text="Globex Corporation is hiring."))
    assert found.role is None and found.unverified and found.candidates == [ready.role]
