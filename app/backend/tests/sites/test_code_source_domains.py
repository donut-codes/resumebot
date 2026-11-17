"""The boards whose roles the app's own code tries first over its browser are the followed boards and the boards whose
runbook says the app's code reads their postings (`code_reads_postings`): no domain is named in code. Every site here
is made up (`.test`)."""

from app.jobsearch.browser import pace
from app.sites import registry
from app.sites.registry import Registry
from app.sites.schema import Runbook


def _registry(*docs: dict) -> Registry:
    runbooks = [Runbook.model_validate({"schema": 1, "kind": "board", **doc}) for doc in docs]
    return Registry(runbooks, (), restricted_present=True, terms=())


def test_a_board_whose_runbook_says_the_code_reads_its_postings_is_one_the_code_tries_first():
    reg = _registry(
        {"key": "read", "name": "Read", "hosts": ["www.readboard.test"], "code_reads_postings": True},
        {"key": "followed", "name": "Followed", "hosts": ["followed.test"], "follow_to_employer": True},
        {"key": "plain", "name": "Plain", "hosts": ["plainboard.test"]})
    with registry.replaced(reg):
        assert pace.code_source_domains() == ("followed.test", "readboard.test")
        assert pace.follow_domains() == ("followed.test",), "reading its postings is not following its roles"


def test_a_board_that_is_followed_and_read_is_named_once_and_with_no_such_board_none_is():
    both = {"key": "both", "name": "Both", "hosts": ["both.test"], "follow_to_employer": True,
            "code_reads_postings": True}
    with registry.replaced(_registry(both)):
        assert pace.code_source_domains() == ("both.test",)
    with registry.replaced(_registry({"key": "plain", "name": "Plain", "hosts": ["plainboard.test"]})):
        assert pace.code_source_domains() == ()


def test_a_domain_is_read_for_its_postings_only_when_its_runbook_says_the_code_reads_them():
    reg = _registry(
        {"key": "read", "name": "Read", "hosts": ["www.readboard.test"], "code_reads_postings": True},
        {"key": "followed", "name": "Followed", "hosts": ["followed.test"], "follow_to_employer": True})
    with registry.replaced(reg):
        assert pace.reads_postings("readboard.test") is True
        assert pace.reads_postings("followed.test") is False, "a followed board's postings are not read by code"
        assert pace.reads_postings("other.test") is False
        assert pace.reads_postings(None) is False
