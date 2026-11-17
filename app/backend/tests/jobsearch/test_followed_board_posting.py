"""A recruiter's link in a mail is opened only when it names one posting on a job system or on a followed board
(`follow_to_employer`), by the board's own `address.one_posting`: no board is named in code. Every site is made up."""

import pytest

from app.jobsearch import address
from app.sites import registry
from app.sites.registry import Registry
from app.sites.schema import Runbook


def _registry() -> Registry:
    docs = [
        {"key": "followed", "name": "Followed", "kind": "board", "hosts": ["followed.test"], "follow_to_employer": True,
         "address": {"one_posting": [{"path": "jobs/view/.+"}, {"path": "comm/jobs/view/.+"}]}},
        {"key": "plain", "name": "Plain", "kind": "board", "hosts": ["plainboard.test"],
         "address": {"one_posting": [{"path": "jobs/view/.+"}]}},
    ]
    return Registry([Runbook.model_validate({"schema": 1, **d}) for d in docs], (), restricted_present=True, terms=())


@pytest.mark.parametrize("link, expected", [
    ("https://www.followed.test/jobs/view/12345", True),
    ("https://followed.test/comm/jobs/view/12345?trk=email", True),
    ("https://www.followed.test/jobs/search/?keywords=x", False),
    ("https://www.followed.test/", False),
    ("http://www.followed.test/jobs/view/12345", False),
    ("https://www.plainboard.test/jobs/view/12345", False),
    ("https://elsewhere.test/jobs/view/12345", False),
    ("not an address", False),
], ids=["a posting", "a mail redirect posting", "a search", "the front page", "no https", "not followed",
        "an unknown host", "not an address"])
def test_only_one_posting_on_a_followed_board_is_taken_from_a_mail(link, expected):
    with registry.replaced(_registry()):
        assert address.followed_board_posting(link) is expected
