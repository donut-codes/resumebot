"""Made-up job boards for the tests whose subject is what the app does with a board, not which board it is: a board
whose pace is slow, whose stop lasts until the owner's next off hours and whose roles are followed to the employer's own
site (`boardone`), two plain boards that carry one posting each at their own address shape (`boardtwo`, `boardthree`)
and a search page whose employer pages sit under its own host (`boardfour`). They are added to the registry the app
loaded, for the length of a test, through the registry's one test hook; no runbook that ships is changed.
No site is named: the keys and hosts are made up (`.test`)."""

import pytest

from app.sites import registry
from app.sites.registry import Registry
from app.sites.schema import Runbook

BOARDS = [
    {"schema": 1, "key": "boardone", "name": "Boardone", "kind": "board", "hosts": ["boardone.test"],
     "mail": {"kind": "board", "senders": ["boardone.test"], "confirms": True},
     "address": {"one_posting": [{"path": "jobs/view/.+"}, {"path": "comm/jobs/view/.+"}]},
     "pace_seconds": 30.0, "stop": "until_next_off_hours", "follow_to_employer": True},
    {"schema": 1, "key": "boardtwo", "name": "Boardtwo", "kind": "board", "hosts": ["boardtwo.test"],
     "mail": {"kind": "board", "senders": ["boardtwo.test"]},
     "address": {"one_posting": [{"path": "(?:.+/)?(?:viewjob|clk)", "query": {"jk": "any"}}]}},
    {"schema": 1, "key": "boardthree", "name": "Boardthree", "kind": "board", "code_reads_postings": True,
     "hosts": ["boardthree.test"], "mail": {"kind": "board", "senders": ["boardthree.test"]},
     "address": {"one_posting": [{"path": "(?:.+/)?job-detail/.+"}]}},
    {"schema": 1, "key": "boardfour", "name": "Boardfour search", "kind": "board", "hosts": ["boardfour.test"],
     "address": {"own_careers_paths": ["about", "careers", "applications", "jobs", "results"], "one_posting": []}},
]


#: Two made-up job systems whose mail comes from a sender of their own (`tenantonemail.test`, `feedonemail.test`).
SYSTEMS = [
    {"schema": 1, "key": "tenantone", "name": "Tenantone", "kind": "job_system", "hosts": ["tenantone.test"],
     "order": 90, "mail": {"kind": "system", "senders": ["tenantonemail.test"], "confirms": True}},
    {"schema": 1, "key": "feedone", "name": "Feedone", "kind": "job_system", "hosts": ["feedone.test"],
     "order": 91, "mail": {"kind": "system", "senders": ["feedonemail.test"], "confirms": True}},
]


def with_made_up_boards() -> Registry:
    """The registry the app loaded, and the made-up boards beside its runbooks."""
    real = registry.current()
    made = [Runbook.model_validate(doc) for doc in (*BOARDS, *SYSTEMS)]
    return Registry([*real.runbooks, *made], real.problems, restricted_present=real.restricted_present,
                    terms=real._terms, hook_folder=real._hook_folder) 


def _forget_what_was_asked() -> None:
    """The one answer kept by the boards a host folds into, which is the registry's as it was when it was asked."""
    from app.jobsearch.postings import same_role

    same_role._board_host.cache_clear()


@pytest.fixture
def made_up_boards():
    _forget_what_was_asked()
    with registry.replaced(with_made_up_boards()) as reg:
        yield reg
    _forget_what_was_asked()


@pytest.fixture
def made_up_mail(made_up_boards, monkeypatch):
    """The sender sets the mail reader froze when it loaded, as the registry with the made-up boards and systems gives
    them: a made-up board's mail is a board's and a made-up system's is a system's."""
    from app.jobsearch.mail import kinds, reader

    boards, systems, confirming = made_up_boards.mail_sets()
    monkeypatch.setattr(kinds, "JOB_BOARDS", boards)
    monkeypatch.setattr(kinds, "JOB_SYSTEMS", systems)
    monkeypatch.setattr(reader, "SYSTEM_SENDERS", confirming)


def example_system_address(slug: str, job: str) -> str:
    """The address of one posting on an employer's board at the job system the shared builders use
    (`tests.support.EXAMPLE_SYSTEM`), built from that system's own runbook: the board's address with the slug put in,
    then the posting's. The reader the app has for the system takes it as one posting. `{n}` may stay in `slug` or `job`
    for a caller to fill."""
    from tests.support import EXAMPLE_SYSTEM

    template = registry.current().get(EXAMPLE_SYSTEM).address["board_url"][0]["template"]
    return f"{template.format(slug=slug)}/jobs/{job}"
