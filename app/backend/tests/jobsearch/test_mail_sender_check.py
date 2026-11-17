"""Who sent a mail counts before it names a role (`mail/match.py`, docs/PRD.md 14.5): a mail names an employer's role
only when it came from that employer's own domain or from a known job system; from anyone else it only asks the owner.
Pure code, no database: made-up employers, roles and senders."""


import uuid
from datetime import date

import pytest

from app.jobsearch import stages
from app.jobsearch.mail import kinds as mail_kinds
from app.jobsearch.mail import match as mail_match
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.models import Employer, Opportunity
from app.mailmine.message import Seen, sender_key
from tests.jobsearch.made_up_sites_b import made_up_boards, made_up_mail  # noqa: F401 (used through pytestmark)

pytestmark = pytest.mark.usefixtures("made_up_mail")


GLOBEX_SITE = "https://www.globexcorp.example"


#: Job systems that send mail from their own domains, and a mailbox provider where a person has an address.
JOB_SYSTEMS, MAILBOX_PROVIDER = ("tenantonemail.test", "us.feedonemail.test", "boardone.test"), "gmail.com"


def seen(address: str, subject: str = "Thank you for applying", text: str = "", name: str = "Careers") -> Seen:
    return Seen(sender_key=sender_key(address), sender_name=name, subject=subject, day=date(2026, 11, 3), text=text,
                bulk=False, address=address)


def candidate(company: str, title: str, *, sites: tuple[str | None, ...] = (GLOBEX_SITE,),
              status: str = stages.APPLIED) -> mail_match.Candidate:
    """A role in play at `company`, whose employer's own sites are `sites`."""
    role = Opportunity(id=uuid.uuid4(), owner="o", slug=f"{company}-{title}", company=company, title=title, fit=3,
                       origin="scout", match_key=f"{company}|{title}".lower(), status=status)
    employer = Employer(owner="o", key=company.lower(), name=company, website=sites[0] if sites else None)
    return mail_match.Candidate(role, go_mail.employer_names([company]), mail_match.own_domains(employer, sites[1:]))


GLOBEX = "Globex Corporation"


HANK = "Hankel Works"


def test_an_employers_own_domains_are_those_of_its_website_its_careers_page_and_its_own_sites():
    employer = Employer(owner="o", key="globex", name=GLOBEX, website="https://www.globexcorp.example",
                        careers_url="https://careers.globex-jobs.example/open")
    assert mail_match.own_domains(employer, ["https://hiring.globex.example/roles"]) == {
        "globexcorp.example", "globex-jobs.example", "globex.example"}


def test_a_role_with_no_employer_record_has_no_own_domain():
    assert mail_match.own_domains(None) == frozenset()


def test_mail_from_the_employers_own_domain_or_a_subdomain_of_it_is_from_the_employer():
    one = candidate(GLOBEX, "Product Lead")
    assert mail_match.from_the_employer(seen("jobs@globexcorp.example"), one)
    assert mail_match.from_the_employer(seen("no-reply@mail.talent.globexcorp.example"), one)


def test_a_lookalike_domain_is_not_the_employers():
    one = candidate(GLOBEX, "Product Lead")
    assert not mail_match.from_the_employer(seen("jobs@notglobexcorp.example"), one)
    assert not mail_match.from_the_employer(seen("jobs@globexcorp.example.mailer.test"), one)


def test_a_person_at_a_mailbox_provider_is_not_the_employer_whatever_the_employers_site_is():
    one = candidate(GLOBEX, "Product Lead", sites=(f"https://{MAILBOX_PROVIDER}",))
    assert not mail_match.from_the_employer(seen(f"globexcorp@{MAILBOX_PROVIDER}"), one)


def test_an_employer_with_no_site_is_never_the_sender():
    one = candidate(GLOBEX, "Product Lead", sites=())
    assert not mail_match.from_the_employer(seen("jobs@globexcorp.example"), one)


def test_mail_from_the_employers_own_domain_finds_its_role():
    one = candidate(GLOBEX, "Product Lead")
    found = mail_match.match(seen("jobs@globexcorp.example", text="Your application to Globex Corporation."), [one])
    assert found.role is one.role and not found.unverified and found.candidates == []


def test_mail_from_a_known_job_system_finds_its_role_though_its_domain_is_the_job_systems():
    one = candidate(GLOBEX, "Product Lead")
    for domain in JOB_SYSTEMS:
        found = mail_match.match(seen(f"jobs@{domain}", text="Your application to Globex Corporation."), [one])
        assert found.role is one.role and not found.unverified, domain


def test_a_job_systems_mail_names_the_role_whatever_its_subject_says():
    one = candidate(GLOBEX, "Product Lead", sites=())
    for domain in set(mail_kinds.JOB_SYSTEMS) - set(mail_kinds.JOB_BOARDS):
        found = mail_match.match(
            seen(f"jobs@{domain}", "Interview invitation", "Globex Corporation would like to speak with you."), [one])
        assert found.role is one.role and not found.unverified, domain


def test_a_job_boards_mail_that_speaks_of_an_application_finds_its_role():
    one = candidate(GLOBEX, "Product Lead", sites=())
    for domain in mail_kinds.JOB_BOARDS:
        for subject in ("Your application to Product Lead at Globex Corporation", "Thank you for applying",
                        "Boardtwo Application: Product Lead", "You applied to Globex Corporation"):
            found = mail_match.match(seen(f"jobs@{domain}", subject, "Globex Corporation."), [one])
            assert found.role is one.role and not found.unverified, (domain, subject)


def test_a_job_boards_alert_or_digest_that_names_the_employer_asks_the_owner():
    one = candidate(GLOBEX, "Product Lead", sites=())
    text = "Globex Corporation: Product Lead. The position has been filled, we regret to inform you."
    for domain in mail_kinds.JOB_BOARDS:
        for subject in ("Product Lead at Globex Corporation and 9 more new jobs", "Jobs for you: Globex Corporation",
                        "Apply now: Product Lead"):
            found = mail_match.match(seen(f"jobalerts-noreply@mail.{domain}", subject, text), [one])
            assert found.role is None and found.unverified and found.candidates == [one.role], (domain, subject)


def test_a_job_boards_alert_from_a_subdomain_is_still_the_boards():
    board = mail_kinds.JOB_BOARDS[0]
    assert mail_kinds.from_a_system(seen(f"jobalerts-noreply@mail.{board}"))
    assert not mail_kinds.may_name_a_role(seen(f"jobalerts-noreply@mail.{board}", "Jobs for you"))


def test_a_newsletter_that_names_the_employer_finds_no_role_and_asks_the_owner():
    one = candidate(GLOBEX, "Product Lead")
    found = mail_match.match(
        seen("digest@jobnews.example", "Roles this week", "Globex Corporation said the position has been filled."),
        [one])
    assert found.role is None and found.unverified and found.candidates == [one.role]


def test_a_forward_from_a_mailbox_provider_that_names_the_employer_asks_the_owner():
    one = candidate(GLOBEX, "Product Lead")
    forward = seen(f"friend@{MAILBOX_PROVIDER}", "Fwd: Globex Corporation", "Globex Corporation wrote.")
    found = mail_match.match(forward, [one])
    assert found.role is None and found.unverified and found.candidates == [one.role]


def test_mail_naming_an_employer_with_no_site_asks_the_owner_unless_a_job_system_sent_it():
    one = candidate(GLOBEX, "Product Lead", sites=())
    from_them = seen("jobs@globexcorp.example", text="Your application to Globex Corporation.")
    assert mail_match.match(from_them, [one]).unverified
    assert mail_match.match(seen(f"x@{JOB_SYSTEMS[0]}", text="Globex Corporation."), [one]).role is one.role


def test_mail_from_one_employers_domain_that_names_another_is_about_the_first():
    globex, hank = candidate(GLOBEX, "Product Lead"), candidate(HANK, "Planner", sites=("https://hankel.example",))
    text = "Your application to Globex Corporation. Hankel Works is a partner."
    found = mail_match.match(seen("jobs@globexcorp.example", text=text), [globex, hank])
    assert found.role is globex.role and not found.unverified


def test_a_mail_naming_two_employers_from_neither_asks_about_both():
    globex, hank = candidate(GLOBEX, "Product Lead"), candidate(HANK, "Planner", sites=("https://hankel.example",))
    found = mail_match.match(seen("digest@jobnews.example", text="Globex Corporation and Hankel Works."),
                             [globex, hank])
    assert found.role is None and found.unverified and set(found.candidates) == {globex.role, hank.role}


def test_a_mail_naming_no_employer_the_owner_has_finds_nothing_whoever_sent_it():
    found = mail_match.match(seen("digest@jobnews.example", text="Initech Systems has openings."),
                             [candidate(GLOBEX, "Product Lead")])
    assert found.role is None and found.candidates == [] and not found.unverified
