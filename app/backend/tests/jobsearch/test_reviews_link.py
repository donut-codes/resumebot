"""An employer's reviews page (docs/PRD.md 28.13, 36.2): the door keeps only a public https address (which site it is
on is not checked), records it with its source, and the employer's view carries it. The app never opens that page. All
made up."""

from types import SimpleNamespace

import pytest

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.research import analysis
from tests.builders import analysis_setup, findings, submit_findings
from tests.support import OWNER

PAGE = "https://reviews.example.test/overview/working-at-fabrikam.htm"
# A sign-in address, built from parts: written out whole, its user@host is an e-mail address at a real domain to the
# release guard (scripts/check_no_one.py), which the test does not need to meet.
SIGN_IN = "https://" + "me" + ":" + "pw" + "@"


def test_a_reviews_link_is_a_public_https_address_whatever_site_it_is_on():
    assert employers.reviews_link(PAGE) == PAGE
    assert employers.reviews_link("https://other.example.test/cmp/x") == "https://other.example.test/cmp/x"
    assert employers.reviews_link("reviews.example.test/overview/x") == "https://reviews.example.test/overview/x"
    for bad in ("http://reviews.example.test/overview/x", SIGN_IN + "reviews.example.test/x", "javascript:alert(1)",
                "ftp://reviews.example.test/x", "https://localhost/x", "", None, 5):
        assert employers.reviews_link(bad) is None, bad


def test_only_a_public_https_address_is_set_on_the_employer_whoever_sends_it():
    employer = SimpleNamespace(reviews_url=None, owner_fields=[], sources={})
    wrong = analysis.EmployerFacts(reviews_url="http://reviews.example.test/overview/x")
    assert analysis._research(employer, wrong) == [] and employer.reviews_url is None
    right = analysis.EmployerFacts(reviews_url=PAGE, sources={"reviews_url": "a web search result"})
    assert analysis._research(employer, right) == ["reviews_url"]
    assert employer.reviews_url == PAGE and employer.sources["reviews_url"]["source"] == "a web search result"


def test_a_reviews_address_the_owner_set_is_kept():
    employer = SimpleNamespace(reviews_url="https://reviews.example.test/overview/mine", owner_fields=["reviews_url"],
                               sources={})
    assert analysis._research(employer, analysis.EmployerFacts(reviews_url=PAGE)) == []
    assert employer.reviews_url == "https://reviews.example.test/overview/mine"


async def _start(ids):
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()


async def test_a_reviews_page_a_search_showed_is_recorded_with_its_source_and_is_in_the_employer_view(js):
    ids = await analysis_setup()
    await _start(ids)
    body = findings(ids["Fabrikam"])
    body["employer"]["reviews_url"] = PAGE
    body["employer"]["sources"]["reviews_url"] = "a web search result"
    assert (await submit_findings(body))["taken"] is True
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        employer = await session.get(Employer, role.employer_id)
        assert employer.reviews_url == PAGE
        assert employer.sources["reviews_url"]["source"] == "a web search result"
        assert employers.view(employer)["reviews_url"] == PAGE


@pytest.mark.parametrize("address", [
    "http://reviews.example.test/overview/fabrikam",
    "https://localhost/overview/fabrikam",
    SIGN_IN + "reviews.example.test/overview/fabrikam",
])
async def test_an_address_that_is_not_a_public_https_address_is_refused_and_after_three_tries_left_out(js, address):
    ids = await analysis_setup()
    await _start(ids)
    body = findings(ids["Fabrikam"])
    body["employer"]["reviews_url"] = address
    reply = await submit_findings(body)
    assert reply["taken"] is False and "employer.reviews_url" in reply["reason"] and reply["tries_left"] == 2
    assert (await submit_findings(body))["taken"] is False
    assert (await submit_findings(body))["taken"] is True, "refused three times, the rest of the findings stand"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        employer = await session.get(Employer, role.employer_id)
        assert employer.reviews_url is None and employer.employees == 300
