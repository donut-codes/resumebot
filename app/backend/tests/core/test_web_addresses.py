"""Addresses that are shown as links: a job system, the scout or the analyst may hand the app any text as an
address, and the app keeps it only when it is an http(s) address, so no link on screen can run a script."""

from types import SimpleNamespace

import pytest

from app.jobsearch import adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.missions import search_run as run
from app.jobsearch.research import analysis
from app.web_address import http_url


@pytest.mark.parametrize("value", [
    "https://jobs.example.test/1", "http://jobs.example.test/a b?x=1#y", "  HTTPS://Jobs.Example.test/1  ",
    "https://jobs.example.test:8443/x",
])
def test_a_web_address_is_kept(value):
    assert http_url(value) == value.strip()


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "JavaScript:alert(1)", " javascript:alert(1)", "java\nscript:alert(1)",
    "data:text/html,x", "file:///etc/passwd", "vbscript:x", "mailto:a@example.test", "//jobs.example.test/1",
    "/careers/1", "https://", "http:///x", "https://jobs.example.test:port/x",
    "https://jobs.example.test/\x00x", "", "   ", None, 5, [],
])
def test_anything_else_is_not_an_address(value):
    assert http_url(value) is None


@pytest.mark.parametrize("value, kept", [
    ("example.com", "https://example.com"), ("  Example.com  ", "https://Example.com"),
    ("www.example.com/careers", "https://www.example.com/careers"),
    ("jobs.example.test", "https://jobs.example.test"),
    ("example.com:8443/x?y=1#z", "https://example.com:8443/x?y=1#z"),
    ("careers.example.co.uk/jobs/1", "https://careers.example.co.uk/jobs/1"),
])
def test_a_bare_domain_becomes_an_https_address(value, kept):
    assert http_url(value) == kept


def test_a_job_systems_posting_keeps_only_web_addresses():
    posting = adapters._posting(title="Lead", url="javascript:alert(1)", apply_url="https://jobs.example.test/apply")
    assert posting["url"] is None and posting["apply_url"] == "https://jobs.example.test/apply"
    assert adapters._posting(url=" https://jobs.example.test/1 ")["url"] == "https://jobs.example.test/1"


def test_a_role_the_scout_reports_keeps_an_apply_link_only_when_it_is_a_web_address():
    base = {"company": "Globex", "title": "Lead", "url": "https://jobs.example.test/1", "fit": 3, "fit_evidence": "x"}
    assert "apply_url" not in run.normalize_role({**base, "apply_url": "javascript:alert(1)"})
    assert run.normalize_role({**base, "apply_url": "https://jobs.example.test/apply"})["apply_url"] \
        == "https://jobs.example.test/apply"
    assert run.normalize_role({**base, "apply_url": "apply.example.test/1"})["apply_url"]         == "https://apply.example.test/1"
    with pytest.raises(run.RunError, match="needs the link to its posting"):
        run.normalize_role({**base, "url": "javascript:alert(1)"})


def test_the_employer_links_the_analyst_sends_are_left_out_unless_they_are_web_addresses():
    facts = analysis.EmployerFacts(website="javascript:alert(1)", careers_url=" https://careers.example.test/ ")
    assert facts.website is None and facts.careers_url == "https://careers.example.test/"
    assert analysis.EmployerFacts(website="example.com").website == "https://example.com"
    assert analysis.EmployerFacts(website="see our site").website is None


def test_a_careers_page_the_app_learns_must_be_a_web_address():
    employer = SimpleNamespace(careers_url=None, owner_fields=[], sources={})
    assert employers.learn_careers_url(employer, "javascript:alert(1)", "the analyst") is False
    assert employer.careers_url is None and employer.sources == {}
    assert employers.learn_careers_url(employer, "https://careers.example.test/", "the analyst") is True
    assert employer.careers_url == "https://careers.example.test/"
