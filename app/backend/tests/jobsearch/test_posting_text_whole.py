"""A posting's text is kept whole and as the posting wrote it (docs/PRD.md 39.29, the owner, 2026-10-14). The app holds
the text of a posting from its job system or its own page; the analyst is sent it whole and its own reading of the
page (a summary, when it had only WebFetch) is never kept, not even where the role has no text. No size cap cuts a
posting any more: the analyst's brief, the reader's, the reader sessions' reports, the browser session's postings and
what the app keeps all carry the text as it is, and a text past what one reply can hold (`owner_text.ROOM_CHARS`) is
refused with its size and never cut. Every name, number and sentence is made up."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import reader as br
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.owner_text import ROOM_CHARS
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis
from app.jobsearch.writing import seats
from tests.builders import (
    WORLD_NOW,
    analysis_setup,
    browser_card,
    browser_world,
    filtered_postings,
    findings,
    made_up_words,
    packet_setup,
    put_in_line,
    report_to_run,
    scan_hand_in,
    submit_findings,
    write_packet,
)
from tests.support import EXAMPLE_SYSTEM, OWNER, POSTING_WORDS, load_profile

SEEN = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
SUMMARY = "A short summary of the posting, the way a model that was handed a web page writes it."
#: The address the made-up roles are Sourced at (`analysis_setup`): a posting at it is the role's source.
SOURCE = "https://jobs.example.test/fabrikam"


def _posting(role_id, text, ats=EXAMPLE_SYSTEM, ext="1"):
    """A posting of the role that a job system's own read gave this text."""
    return JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=ats, ext_id=ext, company="Fabrikam",
                      title=f"Posting {ext}", match_key=f"fabrikam|posting {ext}", first_seen=SEEN, last_seen=SEEN,
                      links=[], opportunity_id=role_id, text=text, detail_at=SEEN, url=SOURCE)


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


# --- the analyst is sent the text whole -------------------------------------------------------------------------------


async def test_the_analyst_is_sent_the_text_the_role_holds_whole(js):
    ids = await analysis_setup()
    held = made_up_words("held", 9_000)  # past the 4,000 characters the brief used to be cut to
    await put_in_line(ids["Fabrikam"], description=held, description_at=SEEN)
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER)
    assert task["role"]["posting_text_kept"] == held


async def test_the_analyst_is_sent_the_text_a_posting_holds_when_the_role_has_none(js):
    ids = await analysis_setup()
    feed = made_up_words("feed", 9_000)
    async with db.session_factory()() as session:
        session.add(_posting(ids["Fabrikam"], feed))
        await session.commit()
    await put_in_line(ids["Fabrikam"], description=None)
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER)
    assert task["role"]["posting_text_kept"] == feed


# --- the analyst's text is never kept: it replaces nothing and fills nothing ---------------------------------------


async def test_the_analysts_text_never_replaces_the_text_the_role_holds(js):
    ids = await analysis_setup()
    feed = made_up_words("feed", 9_000)
    await put_in_line(ids["Fabrikam"], description=feed, description_at=SEEN)
    assert (await submit_findings(findings(ids["Fabrikam"], posting_text=SUMMARY)))["taken"] is True
    role = await _role(ids["Fabrikam"])
    assert role.description == feed and role.description_at == SEEN


async def test_the_analysts_text_does_not_take_the_place_of_the_text_a_posting_holds(js):
    ids = await analysis_setup()
    feed = made_up_words("feed", 9_000)
    async with db.session_factory()() as session:
        session.add(_posting(ids["Fabrikam"], feed))
        await session.commit()
    await put_in_line(ids["Fabrikam"], description=None)
    assert (await submit_findings(findings(ids["Fabrikam"], posting_text=SUMMARY)))["taken"] is True
    role = await _role(ids["Fabrikam"])
    assert role.description == feed, "the app's own text fills the role; the analyst's summary is not kept"
    async with db.session_factory()() as session:
        assert (await session.scalar(select(JobPosting.text))) == feed


async def test_a_role_the_app_cannot_read_is_not_researched_from_the_analysts_summary_and_stays_empty(js):
    ids = await analysis_setup()
    await put_in_line(ids["Fabrikam"], url="https://www.boardone.test/jobs/view/1001", description=None)
    with pytest.raises(analysis.AnalysisError, match="isn't waiting for research"):
        await submit_findings(findings(ids["Fabrikam"], posting_text=SUMMARY))  # the analyst is never sent such a role
    role = await _role(ids["Fabrikam"])
    assert role.description is None and role.description_at is None, "the analyst's text is never kept"
    assert role.analysis_state == analysis.NO_TEXT and not role.analysis.get("required"), "nothing was researched"


# --- nothing is cut: the analyst's text is taken whole, and one no reply could hold is refused ------------------------


async def test_the_analysts_text_is_taken_however_long_a_posting_is_and_not_kept(js):
    ids = await analysis_setup()
    long = made_up_words("analyst", 60_000)  # past the 30,000 characters a role used to keep
    await put_in_line(ids["Fabrikam"])
    assert (await submit_findings(findings(ids["Fabrikam"], posting_text=long)))["taken"] is True
    assert (await _role(ids["Fabrikam"])).description == POSTING_WORDS, "the posting's own words stay"


# --- the scout's and the browser session's texts ----------------------------------------------------------------------


def test_a_role_a_scout_reports_keeps_the_description_it_sent_whole_and_a_text_no_reply_can_hold_is_refused():
    raw = {"company": "Fabrikam", "title": "Programs Lead", "url": "https://jobs.example.test/1", "fit": 3,
           "fit_evidence": "One line.", "work_mode": "remote"}
    long = made_up_words("scout", 60_000)  # past the 30,000 characters a report was cut to
    assert run.normalize_role({**raw, "description": long})["description"] == long
    with pytest.raises(run.RunError, match=r"nothing was cut"):
        run.normalize_role({**raw, "description": "x" * (ROOM_CHARS + 1)})


async def test_the_postings_a_browser_session_hands_in_keep_their_snippet_and_description_whole(js):
    await browser_world()
    snippet, description = made_up_words("snippet", 6_000), made_up_words("page", 50_000)  # past 2,500 and 20,000
    await scan_hand_in([browser_card(1, snippet=snippet), browser_card(2, description=description)], now=WORLD_NOW)
    async with db.session_factory()() as session:
        stored = {p.title: p.text for p in (await session.scalars(select(JobPosting))).all()}
    assert stored == {"Programs Director 1": snippet, "Programs Director 2": description}


async def test_a_browser_session_that_hands_in_a_text_no_reply_can_hold_is_refused_and_nothing_is_stored(js):
    await browser_world()
    with pytest.raises(br.BrowserError, match=r"nothing was cut"):
        await scan_hand_in([browser_card(1, description="x" * (ROOM_CHARS + 1))], now=WORLD_NOW)
    async with db.session_factory()() as session:
        assert (await session.scalar(select(JobPosting.id))) is None


# --- what the app keeps is not cut either -----------------------------------------------------------------------------


async def test_the_text_kept_on_a_role_and_its_posting_is_kept_whole(js):
    ids = await analysis_setup(text=None)
    long = made_up_words("kept", 60_000)  # past the 30,000 characters a posting kept
    async with db.session_factory()() as session:
        posting = _posting(ids["Fabrikam"], None)
        session.add(posting)
        await session.flush()
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert await posting_text.take(session, role, posting_text.Capture(long)) == posting_text.FETCHED
        await session.commit()
        assert (await session.get(JobPosting, posting.id)).text == long and role.description == long


async def test_a_role_made_from_a_posting_that_holds_its_text_keeps_that_text_whole_whatever_a_reader_sent(js):
    long = made_up_words("feed", 40_000)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await filtered_postings(session, 1, 0)
        posting = await session.scalar(select(JobPosting))
        posting.text = long
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id
    reading = {"company": "Fabrikam", "title": "Reader Role 1", "url": "https://jobs.example.test/reader-role-1",
               "fit_evidence": "Biggest gap: none.",
               "fit_breakdown": {"asks": [{"id": "promotion", "answer": "met", "evidence": "A step up."},
                                          {"id": "matches-responsibilities", "answer": "not", "evidence": "Other."}]},
               "description": SUMMARY}
    await report_to_run(run_id, [reading])
    await report_to_run(run_id, [], done=True)
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.title == "Reader Role 1"))
    assert role.description == long, "the posting's own text, whole, not the reader's summary of it"


SNIPPET_100 = "Programs Lead, Oakfield. We need someone to lead the programs team and keep the books in order. Apply."
PAGE_DUMP = "<!DOCTYPE html>" + chr(10) + "Skip to content" + chr(10) + made_up_words("page", 800)


@pytest.mark.parametrize("text", [SNIPPET_100, PAGE_DUMP], ids=["a snippet", "a page read whole"])
async def test_a_role_a_scout_reports_takes_neither_a_snippet_nor_a_page_dump_from_its_posting_as_its_description(
        js, text):
    """Found with the rule written in SQL: a posting of a job system held for the role was taken whenever it had any
    text at all, so a 100-character list entry (a title, a place, a line) or a page's markup became the role's
    description, and every reader of the role took it for the posting's text."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await filtered_postings(session, 1, 0)
        posting = await session.scalar(select(JobPosting))
        posting.text = text
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id
    reading = {"company": "Fabrikam", "title": "Reader Role 1", "url": "https://jobs.example.test/reader-role-1",
               "fit_evidence": "Biggest gap: none.",
               "fit_breakdown": {"asks": [{"id": "promotion", "answer": "met", "evidence": "A step up."},
                                          {"id": "matches-responsibilities", "answer": "not", "evidence": "Other."}]},
               "description": SUMMARY}
    await report_to_run(run_id, [reading])
    await report_to_run(run_id, [], done=True)
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.title == "Reader Role 1"))
    assert role.description is None and role.description_at is None, "neither is the posting's text"


# --- the writer's text ------------------------------------------------------------------------------------------


async def _ask_for_a_packet(client, role_id) -> str:
    return (await client.post(f"/api/v1/roles/{role_id}/packet", json={})).json()["id"]


async def test_a_written_packet_leaves_a_roles_description_as_it_was_and_never_fills_an_empty_one(client):
    role_id = await packet_setup()
    held = "Lead programs for the board. " * 2
    await _ask_for_a_packet(client, role_id)
    await write_packet()
    assert (await _role(role_id)).description == held
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).description = None
        await session.commit()
    await _ask_for_a_packet(client, role_id)
    await write_packet()
    assert (await _role(role_id)).description is None, "nothing the writer reads of a page is kept as the posting"


async def test_the_composer_is_handed_the_text_a_posting_holds_whole_when_the_role_has_none(client):
    role_id = await packet_setup()
    feed = made_up_words("feed", 9_000)
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).description = None
        session.add(_posting(role_id, feed))
        await session.commit()
    await _ask_for_a_packet(client, role_id)
    async with db.session_factory()() as session:
        taken = (await seats.take(session, OWNER, "s-1"))["seat"]
        await session.commit()
    assert feed in taken["system"]
