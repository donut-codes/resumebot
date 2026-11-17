"""The app fetches a posting's text itself, in code, before the analyst is sent the role (docs/PRD.md 39.29, the step
"Fetch the posting's text" of analyse_role.bpmn), from the role's source and no other link: the role's posting on its
job system, its source when it is on a job system whose reader reads one posting, or the employer's own career page,
read politely (robots.txt, an honest user agent). It never reads a job board, Boardone above all. What it
fetches is sent to the analyst whole; the analyst's own text is never kept, so where the app cannot fetch, the role
has none. The role's page shows the text a posting holds when the role has none. Every site, employer and sentence is
made up, and no test reaches the network."""


import asyncio
import html
import json
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import defer

from app import db
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import USER_AGENT, Fetcher
from app.jobsearch.models import ADDED_LINK, Employer, JobPosting, Opportunity
from app.jobsearch.pipeline import quiet
from app.jobsearch.postings import index as postings
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis
from app.timeutil import iso_utc
from tests.builders import analysis_setup, findings, put_in_line, submit_findings
from tests.support import EXAMPLE_SYSTEM, OWNER

SEEN = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)


RESEARCHED = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)


SUMMARY = "A short summary of the posting, the way a model that was handed a web page writes it."


UUID_ID = "7a1f2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b"


#: A posting of eighty paragraphs: far past the 4,000 characters the analyst used to be sent, with line breaks to keep.
PARAGRAPHS = [f"Paragraph {i} of the posting says one thing about the work." for i in range(1, 81)]


WHOLE = "\n".join(PARAGRAPHS)


MARKUP = "".join(f"<p>{p}</p>" for p in PARAGRAPHS)


FEEDONE_PAGE = "https://boards.feedone.test/acme/jobs/4012"


class _NoWaitAsyncio:
    """The fetcher's `asyncio` with a sleep that does not wait. A 429 is waited out for the seconds the site says (five
    with no `Retry-After`) before the fetcher asks once more, which took five real seconds in each test that answers
    one (`tests/jobsearch/test_polite.py` holds the wait itself to a clock). What these tests hold is where the role
    stands after the answer. Only the fetcher's own name is replaced: the event loop keeps the real sleep."""

    @staticmethod
    async def sleep(seconds, *args):
        await asyncio.sleep(0)

    def __getattr__(self, name):
        return getattr(asyncio, name)


def web(monkeypatch, handle, robots: str = "") -> SimpleNamespace:
    """Answer every address of the made-up web with `handle`. `pages` is what was asked (robots.txt apart), `robots`
    the times robots.txt was asked for."""
    seen = SimpleNamespace(pages=[], robots=[])
    monkeypatch.setattr(polite, "asyncio", _NoWaitAsyncio())

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            seen.robots.append(request)
            return httpx.Response(200 if robots else 404, text=robots)
        seen.pages.append(request)
        return handle(request)

    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0))
    return seen


def gone(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"nothing should have been read, and {request.url} was")


def feedone(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v1/boards/acme/jobs/4012"
    return httpx.Response(200, json={"id": 4012, "title": "Programs Lead", "content": html.escape(MARKUP),
                                     "first_published": "2026-09-30T14:02:11-04:00"})


async def _employer_and_posting(role_id, *, text=None) -> tuple[uuid.UUID, uuid.UUID]:
    """Acme on Feedone, and the role's posting on its board (no text unless told)."""
    async with db.session_factory()() as session:
        employer = Employer(owner=OWNER, key="acme", name="Acme", ats=EXAMPLE_SYSTEM, board={"slug": "acme"})
        session.add(employer)
        await session.flush()
        role = await session.get(Opportunity, role_id)  # the posting on the employer's job system is the role's source
        assert role is not None
        # The posting names the role's own employer, as the gates read the cleansed copy's (`gating.role_subject`).
        posting = JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id="4012",
                             company=role.company, title="Programs Lead", match_key="acme|programs lead",
                             url=FEEDONE_PAGE, first_seen=SEEN, last_seen=SEEN, links=[], opportunity_id=role_id,
                             text=text, detail_at=SEEN if text else None)
        session.add(posting)
        role.url = role.source_url = FEEDONE_PAGE
        await session.commit()
        return employer.id, posting.id


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _posting(posting_id) -> JobPosting:
    async with db.session_factory()() as session:
        return await session.get(JobPosting, posting_id, populate_existing=True)


async def test_the_app_reads_a_postings_text_from_its_job_system_and_the_analyst_is_sent_it_whole(js, monkeypatch):
    ids = await analysis_setup(text=None)
    _, posting_id = await _employer_and_posting(ids["Fabrikam"])
    seen = web(monkeypatch, feedone)
    await put_in_line(ids["Fabrikam"])
    assert [r.url.path for r in seen.pages] == ["/v1/boards/acme/jobs/4012"]
    role, posting = await _role(ids["Fabrikam"]), await _posting(posting_id)
    assert role.description == WHOLE and posting.text == WHOLE and posting.detail_at is not None
    assert posting.posted == date(2026, 9, 30)
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER)
    assert task["role"]["posting_text_kept"] == WHOLE
    assert (await submit_findings(findings(ids["Fabrikam"], posting_text=SUMMARY)))["taken"] is True
    assert (await _role(ids["Fabrikam"])).description == WHOLE, "the analyst's summary replaced nothing"


async def test_the_app_gives_a_role_the_text_a_posting_holds_without_reading_anything(js, monkeypatch):
    ids = await analysis_setup(text=None)
    _, posting_id = await _employer_and_posting(ids["Fabrikam"], text=WHOLE)
    seen = web(monkeypatch, gone)
    await put_in_line(ids["Fabrikam"])
    assert seen.pages == [] and seen.robots == []
    role = await _role(ids["Fabrikam"])
    assert role.description == WHOLE and role.description_at == SEEN, "the text and the day it was read"
    assert (await _posting(posting_id)).text == WHOLE


async def test_a_role_listed_without_its_text_is_given_the_text_a_posting_holds(js, monkeypatch):
    ids = await analysis_setup(text=None)
    await _employer_and_posting(ids["Fabrikam"], text=WHOLE)
    web(monkeypatch, gone)
    async with db.session_factory()() as session:  # the Pipeline lists roles without their text; research starts in it
        role = await session.scalar(select(Opportunity).where(Opportunity.id == ids["Fabrikam"])
                                    .options(defer(Opportunity.description)))
        await analysis.start(session, OWNER, role)
        await session.commit()
    assert (await _role(ids["Fabrikam"])).description == WHOLE


async def test_a_text_the_role_holds_is_kept_whoever_wrote_it_and_nothing_is_read(js, monkeypatch):
    ids = await analysis_setup(text=None)
    await _employer_and_posting(ids["Fabrikam"])
    seen = web(monkeypatch, gone)
    await put_in_line(ids["Fabrikam"], description="The owner's own copy.", description_at=SEEN,
                      analysis={"at": RESEARCHED.isoformat()})
    assert seen.pages == [] and (await _role(ids["Fabrikam"])).description == "The owner's own copy."


async def test_a_snippet_the_role_holds_is_not_its_text_and_its_source_is_read_for_the_whole_posting(js, monkeypatch):
    """The CLA case: a role made from a list entry holds a title and a place count (35 characters). That is under
    `cleansed.MIN_TEXT`, so it is no text: the fetch step reads the source instead of calling the role held."""
    ids = await analysis_setup(text=None)
    snippet = "Programs Lead"+chr(10)+"6 Locations"
    _, posting_id = await _employer_and_posting(ids["Fabrikam"], text=snippet)
    seen = web(monkeypatch, feedone)
    await put_in_line(ids["Fabrikam"], description="Programs Lead\n6 Locations", description_at=SEEN)
    assert [r.url.path for r in seen.pages] == ["/v1/boards/acme/jobs/4012"]
    assert (await _role(ids["Fabrikam"])).description == WHOLE and (await _posting(posting_id)).text == WHOLE


BOARD_ANSWERS = {
    EXAMPLE_SYSTEM: (FEEDONE_PAGE, "/v1/boards/acme/jobs/4012", {"content": html.escape(MARKUP)}),
    "feedfour": (f"https://jobs.feedfour.test/acme/{UUID_ID}", f"/v0/postings/acme/{UUID_ID}",
              {"id": UUID_ID, "text": "Programs Lead", "descriptionPlain": WHOLE}),
    "feedtwo": (f"https://jobs.feedtwo.test/acme/{UUID_ID}", "/posting-api/job-board/acme",
              {"jobs": [{"id": "other", "title": "Elsewhere", "descriptionPlain": "Another job."},
                        {"id": UUID_ID, "title": "Programs Lead", "descriptionPlain": WHOLE}]}),
    "tenantone": ("https://acme.wd5.tenantone.test/en-US/Careers/job/Oakfield-ST/Lead_R101?src=x",
                "/wday/cxs/acme/Careers/job/Oakfield-ST/Lead_R101",
                {"jobPostingInfo": {"jobDescription": html.escape(MARKUP), "startDate": "2026-09-30"}}),
}


CAREER_PAGE = "https://careers.example.test/jobs/programs-lead"


def _json_ld_page(request: httpx.Request) -> httpx.Response:
    ld = json.dumps({"@context": "https://schema.org", "@type": "JobPosting", "title": "Programs Lead",
                     "description": MARKUP})
    page = f'<html><head><script type="application/ld+json">{ld}</script></head><body>Menu</body></html>'
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=page)


async def test_an_employers_own_career_page_is_read_for_its_text_with_robots_txt_and_an_honest_agent(js, monkeypatch):
    ids = await analysis_setup(text=None)
    seen = web(monkeypatch, _json_ld_page)
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE)
    assert len(seen.robots) == 1 and [r.url.path for r in seen.pages] == ["/jobs/programs-lead"]
    assert seen.pages[0].headers["user-agent"] == USER_AGENT
    assert (await _role(ids["Fabrikam"])).description == WHOLE


async def test_a_career_page_without_the_posting_embedded_gives_the_text_of_the_page(js, monkeypatch):
    ids = await analysis_setup(text=None)
    page = f"<html><head><title>Programs Lead - Acme Careers</title></head><body><main>{MARKUP}</main></body></html>"
    web(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=page))
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE)
    assert WHOLE in (await _role(ids["Fabrikam"])).description


async def test_a_career_page_that_robots_txt_closes_is_not_read(js, monkeypatch):
    ids = await analysis_setup(text=None)
    seen = web(monkeypatch, gone, robots="User-agent: *\nDisallow: /jobs/")
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE)
    assert seen.pages == [] and (await _role(ids["Fabrikam"])).description is None


async def test_a_page_that_shows_too_little_is_not_kept_as_the_posting(js, monkeypatch):
    ids = await analysis_setup(text=None)
    web(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"},
                                                    text="<html><body><p>We use cookies.</p></body></html>"))
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE)
    assert (await _role(ids["Fabrikam"])).description is None


HTML = {"content-type": "text/html; charset=utf-8"}


#: What an employer's generic careers page says: long enough to be a text, and nothing of any one job.
VALUES = "".join(f"<p>Value {i}: we look after each other and the places we work in, and we say so.</p>"
                 for i in range(1, 13))


GENERIC_PAGE = (f"<html><head><title>Careers at Acme</title></head><body><main><h1>Our values</h1>{VALUES}</main>"
                "</body></html>")


def _page_of(page: str):
    return lambda request: httpx.Response(200, headers=HTML, text=page)


def _job_posting_page(title: str, location: str = "Austin, TX") -> str:
    ld = json.dumps({"@context": "https://schema.org", "@type": "JobPosting", "title": title, "description": MARKUP,
                     "jobLocation": {"@type": "Place", "address": {"addressLocality": location}}})
    return f'<html><head><script type="application/ld+json">{ld}</script></head><body>Menu</body></html>'


async def _read_with(monkeypatch, page: str, title: str | None) -> postings.Reading:
    """What `read_page` makes of the made-up page at the career link, for a role with this title (None: not checked)."""
    web(monkeypatch, _page_of(page))
    fetch = polite.make()
    try:
        return await postings.read_page(fetch, CAREER_PAGE, title)
    finally:
        await fetch.close()


async def test_a_careers_page_that_is_not_the_roles_posting_is_not_kept_as_its_text(js, monkeypatch):
    ids = await analysis_setup(text=None)
    seen = web(monkeypatch, _page_of(GENERIC_PAGE))
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE)
    assert [r.url.path for r in seen.pages] == ["/jobs/programs-lead"], "the page was read"
    role = await _role(ids["Fabrikam"])
    assert role.description is None and role.analysis_state == analysis.NO_TEXT, "the role waits for its text"


async def test_the_text_a_session_captured_is_kept_after_a_page_that_was_not_the_posting(js, monkeypatch):
    ids = await analysis_setup(text=None)
    web(monkeypatch, _page_of(GENERIC_PAGE))
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert await posting_text.take(session, role, posting_text.Capture(WHOLE)) == posting_text.FETCHED
        await session.commit()
    assert (await _role(ids["Fabrikam"])).description == WHOLE


async def test_a_capture_is_kept_whatever_its_text_says_of_the_roles_title(js):
    ids = await analysis_setup(text=None)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert "programs" not in WHOLE.lower() and role.title == "Programs Lead"
        assert await posting_text.take(session, role, posting_text.Capture(WHOLE)) == posting_text.FETCHED
        await session.commit()
    assert (await _role(ids["Fabrikam"])).description == WHOLE


async def test_a_job_systems_answer_is_kept_whatever_its_text_says_of_the_roles_title(js, monkeypatch):
    ids = await analysis_setup(text=None)
    await _employer_and_posting(ids["Fabrikam"])
    web(monkeypatch, feedone)
    await put_in_line(ids["Fabrikam"], title="Platform Engineer")
    assert (await _role(ids["Fabrikam"])).description == WHOLE


async def test_a_role_added_by_its_link_is_not_checked_against_its_title_which_is_the_link(js, monkeypatch):
    ids = await analysis_setup(text=None)
    web(monkeypatch, _page_of(GENERIC_PAGE))
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.company, role.title, role.url, role.source_url = ADDED_LINK, CAREER_PAGE, CAREER_PAGE, CAREER_PAGE
        assert await posting_text.take(session, role) == posting_text.FETCHED
        await session.commit()
    assert "Our values" in (await _role(ids["Fabrikam"])).description


async def test_a_page_that_is_another_jobs_posting_gives_no_text_and_none_of_its_facts(monkeypatch):
    got = await _read_with(monkeypatch, _job_posting_page("Warehouse Associate"), "Programs Lead")
    assert got.text is None and got.facts == {}


async def test_a_page_with_the_roles_posting_gives_its_text_and_facts(monkeypatch):
    got = await _read_with(monkeypatch, _job_posting_page("Programs Lead"), "Programs Lead")
    assert got.text is not None and WHOLE in got.text and got.facts["location"] == "Austin, TX"


async def test_a_page_read_with_no_title_to_check_is_kept_as_it_always_was(monkeypatch):
    got = await _read_with(monkeypatch, _job_posting_page("Warehouse Associate"), None)
    assert got.text is not None and got.facts["title"] == "Warehouse Associate"


@pytest.mark.parametrize("link", [
    "https://www.boardone.test/jobs/view/1001", "https://uk.boardone.test/jobs/view/1001",
    "https://www.boardtwo.test/viewjob?jk=abc", "https://www.boardthree.test/job-detail/abc"])
async def test_a_job_board_is_never_read_for_a_postings_text(js, monkeypatch, link):
    ids = await analysis_setup(text=None)
    seen = web(monkeypatch, gone)
    await put_in_line(ids["Fabrikam"], url=link, board_url=link, apply_url=link, source_url=None, source_kind=None,
                      sourced_at=None)  # a job board's link is no source, so nothing is read for the role
    assert seen.pages == [] and seen.robots == [], "nothing is asked of a job board, not even its robots.txt"
    assert (await _role(ids["Fabrikam"])).description is None


async def test_a_read_that_fails_leaves_the_role_without_text_and_the_analysts_summary_is_not_kept(js, monkeypatch):
    ids = await analysis_setup(text=None)
    await _employer_and_posting(ids["Fabrikam"])
    web(monkeypatch, lambda request: httpx.Response(500))
    await put_in_line(ids["Fabrikam"])
    role = await _role(ids["Fabrikam"])
    assert role.description is None and role.analysis_state == analysis.NO_TEXT, "no text is the truth"
    with pytest.raises(analysis.AnalysisError, match="isn't waiting for research"):
        await submit_findings(findings(ids["Fabrikam"], posting_text=SUMMARY))  # the analyst is never sent the role
    assert (await _role(ids["Fabrikam"])).description is None, "a summary is not text"


async def test_a_step_that_breaks_never_stops_the_research(js, monkeypatch):
    ids = await analysis_setup(text=None)

    async def breaks(*args, **kwargs):
        raise RuntimeError("the step broke")

    monkeypatch.setattr(posting_text, "take", breaks)
    await put_in_line(ids["Fabrikam"])
    assert (await _role(ids["Fabrikam"])).analysis_state == analysis.NO_TEXT, "the run goes on: it waits for the text"


async def test_the_analysts_text_is_kept_on_no_posting_at_all(js):
    ids = await analysis_setup()
    _, posting_id = await _employer_and_posting(ids["Fabrikam"])
    async with db.session_factory()() as session:
        session.add(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="scout", ext_id="s1", company="Fabrikam",
                               title="Reported", match_key="fabrikam|reported", first_seen=SEEN, last_seen=SEEN,
                               links=[], opportunity_id=ids["Fabrikam"]))
        await session.commit()
    await put_in_line(ids["Fabrikam"])
    assert (await submit_findings(findings(ids["Fabrikam"], posting_text=SUMMARY)))["taken"] is True
    assert (await _posting(posting_id)).text is None, "the job system's own read fills it, never an agent's summary"
    async with db.session_factory()() as session:
        reported = await session.scalar(select(JobPosting).where(JobPosting.ext_id == "s1"))
    assert reported.text is None, "nor does a posting nothing reads keep it"


async def test_the_role_page_shows_the_text_a_posting_holds_when_the_role_has_none(js, client):
    ids = await analysis_setup(text=None)
    _, posting_id = await _employer_and_posting(ids["Fabrikam"], text=WHOLE)
    read_at = (await _posting(posting_id)).detail_at
    body = (await client.get(f"/api/v1/roles/{ids['Fabrikam']}")).json()
    assert body["role"]["description"] == WHOLE and body["role"]["description_at"] == iso_utc(read_at)
    assert (await _role(ids["Fabrikam"])).description is None, "the page reads the text; it does not copy it"


async def test_the_role_page_shows_the_roles_own_text_and_none_when_nothing_is_held(js, client):
    ids = await analysis_setup(text=None)
    """A whole text the role holds is shown over its posting's; a snippet it holds gives way to the posting's whole
    text (`posting_text.held`)."""
    await _employer_and_posting(ids["Fabrikam"], text=WHOLE)
    mine = "The role's own text, every duty and ask of it. " * 10
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["Fabrikam"])).description = mine
        await session.commit()
    own = (await client.get(f"/api/v1/roles/{ids['Fabrikam']}")).json()["role"]
    assert own["description"] == mine.strip()
    assert (await client.get(f"/api/v1/roles/{ids['Initech']}")).json()["role"]["description"] is None


TENANTONE_LINK = BOARD_ANSWERS["tenantone"][0]


def tenantone(remote_type: str | None, location: str = "Oakfield, ST"):
    info = {"jobDescription": html.escape(MARKUP), "startDate": "2026-09-30", "location": location}
    if remote_type:
        info["remoteType"] = remote_type
    return lambda request: httpx.Response(200, json={"jobPostingInfo": info})


async def test_a_role_keeps_the_work_mode_and_place_it_has_over_what_the_posting_says(js, monkeypatch):
    ids = await analysis_setup(text=None)
    web(monkeypatch, tenantone("Remote"))
    await put_in_line(ids["Fabrikam"], url=TENANTONE_LINK, source_url=TENANTONE_LINK, move_type="hybrid",
                      location="Riverton, ST")
    role = await _role(ids["Fabrikam"])
    assert (role.move_type, role.location) == ("hybrid", "Riverton, ST")


async def test_a_posting_that_says_nothing_of_the_work_mode_leaves_the_role_without_one(js, monkeypatch):
    ids = await analysis_setup(text=None)
    web(monkeypatch, tenantone(None))
    await put_in_line(ids["Fabrikam"], url=TENANTONE_LINK, source_url=TENANTONE_LINK, move_type=None, location=None)
    assert (await _role(ids["Fabrikam"])).move_type is None


async def _removed_lines(role_id) -> list:
    from app.models.work import TrailEvent

    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == role_id, TrailEvent.event == quiet.REMOVED_EVENT))).all())


@pytest.mark.parametrize("status", [404, 410])
async def test_a_career_page_that_answers_404_or_410_ends_the_role_with_the_evidence(js, monkeypatch, status):
    ids = await analysis_setup(text=None)
    web(monkeypatch, lambda request: httpx.Response(status, text="Not here"))
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE)
    role = await _role(ids["Fabrikam"])
    assert role.status == "gone_quiet" and role.ended_reason == "posting_gone"
    assert role.analysis_state is None
    (line,) = await _removed_lines(ids["Fabrikam"])
    assert CAREER_PAGE in line.detail and str(status) in line.detail


async def test_a_role_researched_before_its_posting_went_stays_researched_when_it_is_ended(js):
    """Found 2026-10-28, when an employer researched done showed no research state after its posting went."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.analysis_state, role.analysed_at = analysis.DONE, RESEARCHED
        assert await quiet.end_gone(session, role, CAREER_PAGE, "No longer accepting applications")
        await session.commit()
    role = await _role(ids["Fabrikam"])
    assert role.status == "gone_quiet" and role.analysis_state == analysis.DONE and role.analysed_at == RESEARCHED


async def test_a_job_systems_404_for_the_posting_ends_the_role(js, monkeypatch):
    ids = await analysis_setup(text=None)
    await _employer_and_posting(ids["Fabrikam"])
    web(monkeypatch, lambda request: httpx.Response(404, json={"message": "no such job"}))
    await put_in_line(ids["Fabrikam"])
    role = await _role(ids["Fabrikam"])
    assert role.status == "gone_quiet" and role.ended_reason == "posting_gone"
    (line,) = await _removed_lines(ids["Fabrikam"])
    assert FEEDONE_PAGE in line.detail and "404" in line.detail


@pytest.mark.parametrize("status", [403, 429, 500, 503])
async def test_a_block_a_rate_limit_or_a_server_error_leaves_the_role_waiting(js, monkeypatch, status):
    ids = await analysis_setup(text=None)
    web(monkeypatch, lambda request: httpx.Response(status, text="Try later"))
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE)
    role = await _role(ids["Fabrikam"])
    assert role.status != "gone_quiet" and role.ended_reason is None
    assert role.analysis_state == analysis.NO_TEXT
    assert await _removed_lines(ids["Fabrikam"]) == []


async def test_a_job_systems_server_error_leaves_the_role_waiting(js, monkeypatch):
    ids = await analysis_setup(text=None)
    await _employer_and_posting(ids["Fabrikam"])
    web(monkeypatch, lambda request: httpx.Response(503, json={}))
    await put_in_line(ids["Fabrikam"])
    role = await _role(ids["Fabrikam"])
    assert role.ended_reason is None and role.analysis_state == analysis.NO_TEXT
