"""The board reader is restricted code (`restricted_runbooks/code/`), so an install without it has none: every question
the shipped code asks of it gets the answer of a board nothing reads, and nothing raises. Made-up data only."""

from app.jobsearch import models
from app.jobsearch.browser import go_checks, link_text
from app.sites import loader, registry
from app.sites.schema import Runbook

LIST_PAGE = "https://jobs.example.test/search?currentJobId=3912345678"


def _board_without_reader() -> Runbook:
    return Runbook.model_validate({"schema": 1, "key": "example_board", "name": "Example Board", "kind": "board",
                                   "hosts": ["jobs.example.test"], "follow_to_employer": True})


def test_no_runbook_names_a_reader_so_there_is_no_board_reader():
    with registry.replaced(registry.Registry([_board_without_reader()])):
        assert registry.board_reader() is None
        assert registry.board_reader("guest") is None
        assert registry.hook_for("example_board") is None


def test_a_registry_with_no_restricted_folder_has_no_board_reader(tmp_path):
    with registry.replaced(loader.load(restricted_dir=tmp_path / "none")):
        assert registry.board_reader() is None
        assert registry.board_reader("guest") is None


def test_a_list_page_with_one_job_open_is_left_as_pasted_where_no_reader_knows_the_board_shape():
    with registry.replaced(registry.Registry([_board_without_reader()])):
        assert link_text._single_job(LIST_PAGE) == LIST_PAGE


def test_a_page_sent_on_to_a_search_is_not_taken_for_a_removed_posting_where_no_reader_knows_the_shape():
    from datetime import date
    from types import SimpleNamespace

    page = SimpleNamespace(status=200, url="https://jobs.example.test/search?trk=expired_jd_redirect",
                           html="<html></html>")
    with registry.replaced(registry.Registry([_board_without_reader()])):
        _, said = go_checks.page_verdict(page, "https://jobs.example.test/jobs/view/3912345678", date(2026, 11, 15))
    assert "taken down" not in said


def test_no_role_is_looked_up_on_a_board_and_no_listing_is_asked_where_no_reader_holds_the_query():
    import asyncio

    from sqlalchemy import and_, false, or_

    from app.jobsearch.models import (
        FOLLOWUP_NEEDED,
        PRE_APPLY,
        SECOND_IMPRESSION,
        Opportunity,
        read_first,
        source_next_marked,
    )
    from app.jobsearch.postings import chase

    with registry.replaced(registry.Registry([_board_without_reader()])):
        without_the_board = or_(Opportunity.status.in_((SECOND_IMPRESSION, FOLLOWUP_NEEDED)),
                                and_(read_first(), Opportunity.status.in_(PRE_APPLY)),
                                and_(source_next_marked(), false()))
        assert str(models.wants_sourcing()) == str(without_the_board)
        assert asyncio.run(chase.fallback_listing(None, "owner")) == []  # type: ignore[arg-type]
