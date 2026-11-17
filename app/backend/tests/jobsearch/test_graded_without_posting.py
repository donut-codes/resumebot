"""A role whose held text is not its posting goes back for the text instead of being graded on nothing (found
2026-11-03: a role in Ready to Launch held the employer's careers page, the analyst said so in its notes and graded no
item, and the score came from the owner's other asks alone). The analyst's findings with no requirement, no preferred
item and no responsibility, for a text that has no requirement lines either, are not taken: the text is let go, the
open step is cancelled and the role waits for the posting's text, which the browser's read of its source brings.
The round is made once: a role sent back whose text comes back as the same page is not sent round again (the analyst's
second findings that grade nothing stop it where it is, and nothing reads it again), and a text that may be real is not
let go (research that holds items, a job the owner added, a role nothing can read again, an ended or gated out role,
which no reader reads). A role the app stopped on is read once more when the owner asks for its research. Everything is
made up."""

import uuid
from datetime import UTC, datetime

import pytest
from process_engine.models import ProcInstance
from sqlalchemy import select

from app import db
from app.jobsearch import actions, stages
from app.jobsearch.actors import BY_APP
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.browser import reader
from app.jobsearch.missions import core as missions
from app.jobsearch.models import BrowserItem, JobPosting, Opportunity, posting_not_found
from app.jobsearch.postings import index as postings_index
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis, requirement_lines
from app.jobsearch.writing import packets
from app.models.work import TrailEvent
from app.sites.models import Site
from tests.builders import (
    analysis_setup,
    findings,
    made_up_words,
    mission_of,
    pipeline_turn,
    put_in_line,
    save_documents,
)
from tests.jobsearch.example_world import EXAMPLE_SYSTEM, OTHER_SYSTEM
from tests.support import OWNER, POSTING_WORDS, load_profile

#: What an employer's values page says: long enough to pass for a posting, with no requirement section in it.
CAREERS = made_up_words("values", 1259)
NOTES = "The text is the employer's careers page, its values and mission, not the job posting."
NOTHING = {"required": [], "preferred": [], "responsibilities": [], "notes": NOTES}


async def _fresh(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _submit(role_id, **over) -> dict:
    async with db.session_factory()() as session:
        reply = await analysis.submit(session, OWNER, findings(role_id, **{**NOTHING, **over}))
        await session.commit()
    return reply


async def _events(role_id, event: str) -> list[TrailEvent]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == role_id, TrailEvent.event == event))).all())


async def _posting(role_id, ext_id: str, text: str, system: str = EXAMPLE_SYSTEM) -> uuid.UUID:
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        posting = JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats=system, ext_id=ext_id, company="Fabrikam",
            title="Programs Lead", match_key="fabrikam|programs lead", url=f"https://jobs.example.test/{ext_id}",
            text=text, detail_at=now, links=[], first_seen=now, last_seen=now, filtered_at=now, opportunity_id=role_id)
        session.add(posting)
        await session.commit()
        return posting.id


def test_the_made_up_values_page_has_no_requirement_lines():
    assert requirement_lines.split(CAREERS) == [] and len(CAREERS) == 1259


async def test_findings_that_grade_nothing_of_a_text_with_no_requirement_lines_send_the_role_back_for_its_text(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id)
    before = await _fresh(role_id)
    instance, fit = before.analysis_instance_id, before.fit

    reply = await _submit(role_id)

    # The analyst's session ends on `taken`, and the reply says why nothing was kept.
    assert reply["ok"] is True and reply["taken"] is True and reply["sent_back"] is True
    assert "posting" in reply["reason"]
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.NO_TEXT and role.analysis_claimed_at is None
    assert role.description is None and role.description_at is None, "the text that is not the posting is let go"
    assert "notes" not in (role.analysis or {}) and role.analysed_at is None, "the findings are not stored"
    assert role.fit == fit, "no score was worked out from the empty findings"
    async with db.session_factory()() as session:
        assert (await session.get(ProcInstance, instance)).status == "cancelled", "the open step is closed"
    [said] = await _events(role_id, "waiting_for_text")
    assert NOTES in (said.detail or ""), "the analyst's words stay in the role's history"
    assert not await _events(role_id, "analysed")
    await pipeline_turn()
    assert (await _fresh(role_id)).status == stages.FIRST_IMPRESSION, "it is no longer on deck"


async def test_a_role_sent_back_is_one_the_browser_reads_at_its_source(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id)
    await _posting(role_id, "same", CAREERS)  # a copy of the same page, kept on a posting by the read
    async with db.session_factory()() as session:
        assert role_id not in {x["role_id"] for x in await reader.to_read_at_source(session, OWNER, -1)}

    await _submit(role_id)

    async with db.session_factory()() as session:
        listed = {x["role_id"] for x in await reader.to_read_at_source(session, OWNER, -1)}
    assert str(role_id) in listed, "a copy of the page on a posting would hide it from the browser's read"


async def test_a_posting_holding_another_text_keeps_it_and_the_role_holds_that_text(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id)
    real = made_up_words("duties", 2400)
    same, other = await _posting(role_id, "same", CAREERS), await _posting(role_id, "other", real, OTHER_SYSTEM)

    await _submit(role_id)

    async with db.session_factory()() as session:
        assert (await session.get(JobPosting, same)).text is None
        assert (await session.get(JobPosting, same)).detail_at is None
        assert (await session.get(JobPosting, other)).text == real
        role = await session.get(Opportunity, role_id)
        assert (await posting_text.held(session, role)).text == real
        assert (await analysis.resume_waiting(session, OWNER, [role_id])) == 0, (
            "the take reads the role's source, never a posting at another address: that text does not put it back in "
            "line (it would be started to wait again, PRD 39.30)")


async def test_an_empty_report_for_a_text_that_has_requirement_lines_is_refused_as_before(js):
    text = CAREERS + "\n\nRequirements\n- Five years of programs\n- A certification"
    ids = await analysis_setup(text)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id)

    reply = await _submit(role_id)

    assert reply["taken"] is False and "not answered" in reply["reason"]
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.WAITING and role.description == text


async def test_one_item_graded_not_measurable_is_taken_as_before(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id)

    reply = await _submit(role_id, required=[{"item": "Willingness to travel", "class": "N",
                                              "why": "no resume line could show it"}])

    assert reply["taken"] is True and "sent_back" not in reply
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.DONE and role.description == CAREERS
    assert role.analysis["notes"] == NOTES


async def test_findings_with_items_for_a_text_with_no_lines_are_taken_as_before(js):
    ids = await analysis_setup(POSTING_WORDS)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id)

    reply = await _submit(role_id, required=findings(role_id)["required"])

    assert reply["taken"] is True and "sent_back" not in reply
    assert (await _fresh(role_id)).analysis_state == analysis.DONE


async def test_the_app_does_not_draft_a_packet_for_a_role_waiting_for_the_posting_text(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        role = await session.get(Opportunity, role_id)
        role.analysis_state = analysis.NO_TEXT
        await session.flush()
        try:
            await packets.check_ask(session, OWNER, role_id, by=BY_APP)
        except packets.SentBack as exc:
            assert "posting's text" in str(exc)
        else:
            raise AssertionError("a role with no text was drafted")


# --- the round is made once ------------------------------------------------------------------------------------------


async def _unread_posting(role_id, ext_id: str) -> uuid.UUID:
    """A posting of the role on a job system the app reads, open and owing its text (none yet, never tried)."""
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        posting = JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=ext_id, company="Fabrikam",
            title="Programs Lead", match_key="fabrikam|programs lead", url=f"https://jobs.example.test/{ext_id}",
            links=[], first_seen=now, last_seen=now, filtered_at=now, opportunity_id=role_id)
        session.add(posting)
        await session.commit()
        return posting.id


async def _owed(role_id) -> set[uuid.UUID]:
    """The postings of the role the app's own read would take the text of now (`index.details_where`)."""
    async with db.session_factory()() as session:
        return set(await session.scalars(
            select(JobPosting.id).join(Opportunity, Opportunity.id == JobPosting.opportunity_id)
            .where(Opportunity.id == role_id, *postings_index.details_where(OWNER))))


async def _browser_asks(role_id) -> dict[str, bool]:
    """Whether each way the browser has of reading a role's text asks for it now: the read of its source, the read in
    full, and an open item in the queue (made as the app's pass makes it)."""
    async with db.session_factory()() as session:
        at_source = {x["role_id"] for x in await reader.to_read_at_source(session, OWNER, -1)}
        in_full = {x["role_id"] for x in await reader.to_read_in_full(
            session, OWNER, await reader.browser_sites(session, OWNER), -1)}
        await browser_queue.refresh(session, OWNER)
        await session.commit()
        open_items = (await session.scalars(select(BrowserItem).where(
            BrowserItem.role_id == role_id, BrowserItem.kind == "read",
            BrowserItem.state.in_(("waiting", "stuck", "leased"))))).all()
    return {"source": str(role_id) in at_source, "full": str(role_id) in in_full, "item": bool(open_items)}


async def _browser_brings_the_page_back(role_id) -> int:
    """The owner's browser reads the role's source and hands the same page back (`browser/handback.py`): it is kept
    as a capture and the role that waited for its text is put in the analyst's line. Returns how many were put there."""
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        assert await posting_text.take(session, role, posting_text.Capture(CAREERS)) == posting_text.FETCHED
        put = await analysis.resume_waiting(session, OWNER, [role_id])
        await session.commit()
    return put


async def _the_app_reads_the_page_back(posting_id) -> int:
    """The app's own read of a posting of the role gives the same page, and the schedule's sweep (every waiting role)
    puts the roles that waited for text in the analyst's line. Returns how many were put there."""
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        posting.text, posting.detail_at = CAREERS, datetime.now(UTC)
        put = await analysis.resume_waiting(session, OWNER)
        await session.commit()
    return put


def _stopped_lines(lines: list[TrailEvent]) -> list[TrailEvent]:
    return [x for x in lines if "could not be found at its source" in x.sentence]


async def test_findings_that_grade_nothing_of_the_same_page_again_stop_the_role_and_the_browser_leaves_it(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    async with db.session_factory()() as session:
        await missions.resume(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        session.add(Site(owner=OWNER, url="https://jobs.example.test/", kind="job_board", read_by="browser",
                         created_at=datetime.now(UTC)))
        await session.commit()
    await put_in_line(role_id)

    await _submit(role_id)  # the first lap: the page is let go and the browser is asked for the posting
    assert (await _fresh(role_id)).analysis[analysis.SENT_BACK] == 1, "counted on the role, past the wait for text"
    assert await _browser_asks(role_id) == {"source": True, "full": True, "item": True}
    assert await _browser_brings_the_page_back(role_id) == 1, "the page comes back and the role is in line again"

    reply = await _submit(role_id)  # the second findings that grade nothing

    assert reply["ok"] is True and reply["taken"] is True and reply["sent_back"] is True, "the session ends"
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.NO_TEXT and role.analysis_claimed_at is None
    assert role.description is None and posting_not_found(role)
    lines = await _events(role_id, "waiting_for_text")
    assert len(lines) == 2 and len(_stopped_lines(lines)) == 1, "the history says the posting could not be found"
    assert NOTES in (_stopped_lines(lines)[0].detail or "")
    assert not await _events(role_id, "analysed")
    assert await _browser_asks(role_id) == {"source": False, "full": False, "item": False}, "no read of any kind"
    await pipeline_turn()
    assert (await _fresh(role_id)).status == stages.FIRST_IMPRESSION, "it did not come back to the deck's top"
    assert await _browser_brings_the_page_back(role_id) == 0, "a hand-back that comes late puts it nowhere"
    assert (await _fresh(role_id)).analysis_state == analysis.NO_TEXT


async def test_findings_that_grade_nothing_of_a_page_the_app_read_again_stop_the_role_and_its_postings_are_left(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    posting_id = await _unread_posting(role_id, "fabrikam")  # at the role's source: the app's read gives its text
    await put_in_line(role_id)

    await _submit(role_id)  # the first lap
    assert await _owed(role_id) == {posting_id}, "the app reads the posting's own page for the text"
    assert await _the_app_reads_the_page_back(posting_id) == 1, "its sweep puts the role in line again"

    reply = await _submit(role_id)  # the second findings that grade nothing

    assert reply["taken"] is True and reply["sent_back"] is True
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.NO_TEXT and role.description is None and posting_not_found(role)
    assert len(_stopped_lines(await _events(role_id, "waiting_for_text"))) == 1
    assert await _owed(role_id) == set(), "the page let go from the posting is not read for again"
    assert await _the_app_reads_the_page_back(posting_id) == 0, "and a sweep never puts the role in line"
    assert (await _fresh(role_id)).analysis_state == analysis.NO_TEXT


# --- a text that may be real is not let go ---------------------------------------------------------------------------


async def test_a_role_that_holds_research_with_items_keeps_the_text_when_the_new_findings_grade_nothing(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id)
    assert (await _submit(role_id, required=findings(role_id)["required"]))["taken"] is True
    await put_in_line(role_id)  # researched again: a blip in the analyst's session sends nothing this time
    assert (await _fresh(role_id)).analysis["required"], "it holds research with classed items"

    reply = await _submit(role_id)

    assert reply["taken"] is True and "sent_back" not in reply
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.DONE and role.description == CAREERS
    assert not posting_not_found(role) and analysis.SENT_BACK not in role.analysis


async def test_a_job_the_owner_added_keeps_its_text_when_the_findings_grade_nothing(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).origin = "owner"
        await session.commit()
    await put_in_line(role_id)

    reply = await _submit(role_id, target_role="none")

    assert reply["taken"] is True and "sent_back" not in reply
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.DONE and role.description == CAREERS


@pytest.mark.parametrize("more", [
    {"source_kind": "board"},  # an Easy Apply: the board's own listing was the source, recorded once
    {"url": None, "source_url": None},  # no link to open
], ids=["easy-apply", "no-link"])
async def test_a_role_nothing_can_read_again_keeps_its_text_when_the_findings_grade_nothing(js, more):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id, **more)

    reply = await _submit(role_id)

    assert reply["taken"] is True and "sent_back" not in reply
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.DONE and role.description == CAREERS


@pytest.mark.parametrize("status", [stages.REJECTED, stages.GATED_OUT])
async def test_an_ended_or_gated_out_role_keeps_its_text_when_the_findings_grade_nothing(js, status):
    """No reader asks for the text of an ended or gated out role (`reader.READ_IN_FULL_STAGES`), so a text let go there
    would never come back: the owner's ask for research is taken as it was. (Until 2026-11-05 this held for a role
    applied to or in talks too, which no reader asked the text of either.)"""
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id, status=status)

    reply = await _submit(role_id)

    assert reply["taken"] is True and "sent_back" not in reply
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.DONE and role.description == CAREERS and role.status == status
    assert not posting_not_found(role) and analysis.SENT_BACK not in role.analysis


@pytest.mark.parametrize("status", [stages.APPLIED, stages.ACTIVE_DISCUSSION])
async def test_a_role_applied_to_or_in_talks_is_sent_back_for_its_text_and_read_again(js, status):
    """Changed on purpose (2026-11-05, docs/PRD.md 39.30): the readers now ask for the text of a role the owner applied
    to or is in talks about (`postings/unbuilt.py`), so the text let go comes back, and findings that grade nothing of a
    page that is not the posting no longer leave such a role researched on nothing. The role keeps its stage. It was
    taken as it was, with the page as its text, while no reader asked for its text."""
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    await put_in_line(role_id, status=status)

    reply = await _submit(role_id)

    assert reply["taken"] is True and reply["sent_back"] is True
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.NO_TEXT and role.description is None and role.status == status
    async with db.session_factory()() as session:
        assert str(role_id) in {x["role_id"] for x in await reader.to_read_at_source(session, OWNER, -1)}


async def test_the_database_rule_for_sending_a_role_back_says_what_the_code_rule_says(js):
    """`may_send_back_where` picks the roles the repair command looks at: it is `may_send_back` in SQL, whatever the
    stage, the origin, the source or the link."""
    ids = await analysis_setup(CAREERS)
    cases = [{}, *({"status": s} for s in stages.STAGES), {"origin": "owner"}, {"source_kind": "board"},
             {"url": None, "source_url": None}, {"url": " ", "source_url": None}, {"url": None, "source_url": " x "}]
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        plain = {name: getattr(role, name) for name in ("status", "origin", "source_kind", "url", "source_url")}
        for case in cases:
            for name, value in {**plain, **case}.items():
                setattr(role, name, value)
            await session.flush()
            in_sql = await session.scalar(select(Opportunity.id).where(
                Opportunity.id == role.id, *analysis.may_send_back_where())) is not None
            assert in_sql is analysis.may_send_back(role), case


# --- the owner's ask puts a stopped role in line again ---------------------------------------------------------------

NO_READ = {"source": False, "full": False, "item": False}
READ_AGAIN = {"source": True, "full": True, "item": True}


async def _browser_world() -> None:
    async with db.session_factory()() as session:
        await missions.resume(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        session.add(Site(owner=OWNER, url="https://jobs.example.test/", kind="job_board", read_by="browser",
                         created_at=datetime.now(UTC)))
        await session.commit()


async def _ask(role_id) -> dict:
    async with db.session_factory()() as session:
        said = await analysis.ask(session, OWNER, role_id)
        await session.commit()
    return said


async def test_the_owners_ask_has_the_browser_read_a_stopped_role_once_more_and_a_second_empty_report_stops_it(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    await _browser_world()
    await put_in_line(role_id)
    await _submit(role_id)
    assert await _browser_brings_the_page_back(role_id) == 1
    await _submit(role_id)  # stopped
    assert posting_not_found(await _fresh(role_id)) and await _browser_asks(role_id) == NO_READ

    said = await _ask(role_id)

    assert "posting's text" in said["said"]
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.NO_TEXT and not posting_not_found(role)
    assert role.analysis[analysis.SENT_BACK] == 1, "the lap it was given stays counted"
    assert await _browser_asks(role_id) == READ_AGAIN, "the owner's ask is what has the browser read it again"
    assert await _browser_brings_the_page_back(role_id) == 1, "and the text that comes puts it in the analyst's line"

    reply = await _submit(role_id)  # the page graded as nothing again

    assert reply["taken"] is True and reply["sent_back"] is True
    role = await _fresh(role_id)
    assert role.analysis_state == analysis.NO_TEXT and posting_not_found(role) and role.description is None
    assert role.analysis[analysis.SENT_BACK] == 1, "no second lap: it is stopped at once"
    assert len(_stopped_lines(await _events(role_id, "waiting_for_text"))) == 2
    assert await _browser_asks(role_id) == NO_READ
    assert await _browser_brings_the_page_back(role_id) == 0


async def test_the_owners_ask_has_the_app_read_a_stopped_roles_posting_again(js):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    posting_id = await _unread_posting(role_id, "fabrikam")
    await put_in_line(role_id)
    await _submit(role_id)
    assert await _the_app_reads_the_page_back(posting_id) == 1
    await _submit(role_id)  # stopped
    assert posting_not_found(await _fresh(role_id)) and await _owed(role_id) == set()

    await _ask(role_id)

    assert not posting_not_found(await _fresh(role_id))
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        assert posting.detail_at is not None, "the ask's own run tried the posting's page (and the read failed)"
        posting.detail_at = None  # as when the days before a posting is tried again have passed
        await session.commit()
    assert await _owed(role_id) == {posting_id}, "the app's own read takes the posting's text again"


# --- the night's read of descriptions leaves a stopped role's postings ----------------------------------------------


async def test_the_nights_read_of_descriptions_leaves_the_postings_of_a_stopped_role(js, monkeypatch):
    ids = await analysis_setup(CAREERS)
    role_id = ids["Fabrikam"]
    posting_id = await _unread_posting(role_id, "fabrikam")
    asked: list[uuid.UUID] = []

    async def fill(_session, _fetch, waiting):
        asked.extend(p.id for p in waiting)
        return 0

    monkeypatch.setattr(actions.postings, "fill_details", fill)

    async def night() -> list[uuid.UUID]:
        asked.clear()
        async with db.session_factory()() as session:
            await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": 5})
        return list(asked)

    await put_in_line(role_id)
    await _submit(role_id)
    assert await night() == [posting_id], "a role sent back once has its posting read, as before"
    assert await _the_app_reads_the_page_back(posting_id) == 1
    await _submit(role_id)  # stopped
    assert posting_not_found(await _fresh(role_id))

    assert await night() == [], "the page let go from the posting is not read for the stopped role"
