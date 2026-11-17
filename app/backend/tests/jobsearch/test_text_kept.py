"""Only the posting's own words are kept on a posting or a role (docs/PRD.md 39.7 and 39.29). A reader that opened a
posting's page, and the analyst that sends the posting's text with its findings, give the app their reading of a page:
a summary, so neither is kept, and a posting that had no text still has none afterwards. Everything is made up."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app import db
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.research import analysis
from tests.builders import analysis_setup, filtered_postings, findings, report_to_run, submit_findings
from tests.support import EXAMPLE_SYSTEM, OWNER, POSTING_WORDS, load_profile

SEEN = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
PAGE = "The posting's own words, as the reader read them on its page.\n\nResponsibilities\n- Lead the team."


def _read(title, **more):
    """A role as it is reported: its link and its answers to the owner's asks, no total."""
    return {"company": "Fabrikam", "title": title, "url": f"https://jobs.example.test/{title.replace(' ', '-')}",
            "fit_evidence": "Biggest gap: none.",
            "fit_breakdown": {"asks": [
                {"id": "promotion", "answer": "met", "evidence": "A step up."},
                {"id": "matches-responsibilities", "answer": "not", "evidence": "It is other work."}]}, **more}


async def _postings() -> dict[str, JobPosting]:
    async with db.session_factory()() as session:
        return {p.title: p for p in (await session.scalars(select(JobPosting))).all()}


async def test_what_a_reader_read_is_never_kept_as_the_postings_text_or_the_roles_description(js):
    """A reader's own reading of a page is a summary of it, and a description is only the posting's own words
    (docs/PRD.md 39.29): it fills the reader's answers and nothing it read is kept as text."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await filtered_postings(session, 2, 0)
        for posting in (await session.scalars(select(JobPosting).where(JobPosting.title == "Reader Role 1"))).all():
            posting.text = None  # the board's list gave none and its page has not been fetched
        for posting in (await session.scalars(select(JobPosting).where(JobPosting.title == "Reader Role 2"))).all():
            posting.text = POSTING_WORDS  # its job system gave the whole posting (a snippet is none)
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id

    await report_to_run(run_id, [_read("Reader Role 1", description=PAGE),
                                 _read("Reader Role 2", description="A copy the reader did not need to send.")])
    posts = await _postings()
    assert posts["Reader Role 1"].text is None and posts["Reader Role 1"].detail_at is None
    assert posts["Reader Role 2"].text == POSTING_WORDS, "a posting that has its text keeps it"

    await report_to_run(run_id, [], done=True)
    async with db.session_factory()() as session:
        roles = {r.title: r for r in (await session.scalars(select(Opportunity))).all()}
    assert roles["Reader Role 1"].description is None and roles["Reader Role 1"].description_at is None
    assert roles["Reader Role 2"].description == POSTING_WORDS


async def test_the_analysts_posting_text_is_kept_on_no_posting_of_the_role(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        session.add_all([
            JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="scout", ext_id="1", company="Fabrikam",
                       title="Without", match_key="fabrikam|without", first_seen=SEEN, last_seen=SEEN, links=[],
                       opportunity_id=role.id)])
        await analysis.start(session, OWNER, role)
        await session.commit()
    assert (await submit_findings(findings(ids["Fabrikam"])))["taken"] is True
    posts = await _postings()
    assert posts["Without"].text is None and posts["Without"].detail_at is None, "a summary is not the posting's text"


async def test_an_analyst_that_sends_no_posting_text_leaves_the_postings_as_they_were(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        session.add(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id="1",
                               company="Fabrikam",
                               title="Without", match_key="fabrikam|without", first_seen=SEEN, last_seen=SEEN, links=[],
                               opportunity_id=role.id))
        await analysis.start(session, OWNER, role)
        await session.commit()
    assert (await submit_findings(findings(ids["Fabrikam"], posting_text="")))["taken"] is True
    assert (await _postings())["Without"].text is None


# --- a Boardone page's text is the board's copy, and the employer's takes its place (docs/PRD.md 48.14, 54) ----------






