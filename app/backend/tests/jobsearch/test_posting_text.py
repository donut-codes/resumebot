"""The role's text (`postings/text.py`, docs/PRD.md 39.29): `held` says what text a role holds, and `take` is the one
way a text, or the word that the posting is gone, reaches it, whatever the word: the app's own read of the role's
source (`Source`), only what a posting of the role holds (`FromPostings`), what the owner's browser captured
(`Capture`). The tests cross that interface and nothing behind it: the test Postgres, and the one seam to the network,
`polite.make`, answered by a fetcher on a mock transport. Every name, address and sentence is made up."""

import uuid
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import ats, stages
from app.jobsearch.fetch import polite
from app.jobsearch.models import JobPosting, Opportunity, RawPosting
from app.jobsearch.postings import cleansed, raw_copies, sourced, whole
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis
from app.models.work import TrailEvent
from tests.builders import analysis_setup, made_up_words
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.jobsearch.test_posting_text_fetch import CAREER_PAGE, MARKUP, WHOLE, web
from tests.support import EXAMPLE_SYSTEM, OWNER, POSTING_WORDS

pytestmark = pytest.mark.usefixtures("made_up_boards")

SEEN = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
RESEARCHED = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)
SNIPPET = "Programs Lead\n6 Locations"
#: The address `analysis_setup` Sources its roles at: a generic employer's page, read as the page it is.
SOURCE = "https://jobs.example.test/fabrikam"
#: An address that is one posting of an employer's own site (what a role Sourced there holds).
OWN = "https://careers.fabrikam.example.test/jobs/4012"
PAGE = "<html><head><title>Programs Lead - Acme Careers</title></head><body><main>" + MARKUP + "</main></body></html>"
HTML = {"content-type": "text/html"}


def posting(role_id, text=None, system=EXAMPLE_SYSTEM, ext="1", detail_at=SEEN, url=None) -> JobPosting:
    """A posting of the role: the job system's (or the owner's browser's, the scout's ...) own, holding `text`."""
    return JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=system, ext_id=ext, company="Fabrikam",
                      title=f"Programs Lead {ext}", match_key=f"fabrikam|programs lead {ext}", first_seen=SEEN,
                      last_seen=SEEN, links=[], opportunity_id=role_id, text=text, detail_at=detail_at, url=url)


async def role_with(*postings: JobPosting, who: str = "Fabrikam", **role_fields) -> uuid.UUID:
    """A role of `analysis_setup(text=None)` (Fabrikam, or `who`), Sourced at its own address, holding no text, with
    these postings; the roles are made the first time a test asks."""
    async with db.session_factory()() as session:
        have = {r.company: r.id for r in (await session.scalars(select(Opportunity))).all()}
    ids = have or await analysis_setup(text=None)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids[who])
        for field, value in role_fields.items():
            setattr(role, field, value)
        for p in postings:
            p.opportunity_id = role.id
            session.add(p)
        await session.commit()
    return ids[who]


async def state(role_id) -> tuple[Opportunity, list[JobPosting], list[RawPosting]]:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id, populate_existing=True)
        rows = list((await session.scalars(select(JobPosting).where(JobPosting.opportunity_id == role_id)
                                           .order_by(JobPosting.ext_id))).all())
        raw = list((await session.scalars(select(RawPosting).where(RawPosting.posting_id.in_([p.id for p in rows]))
                                          .order_by(RawPosting.seq))).all())
        return role, rows, raw


async def run(role_id, word=None, now=None, after=None):
    """`take` for the role in a session of its own, committed; returns what it answered."""
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        how = await posting_text.take(session, role, word, now=now)
        if after is not None:
            await after(session, role)
        await session.commit()
        return how


# --- held -----------------------------------------------------------------------------------------------------------


async def held(role_id) -> posting_text.Held:
    async with db.session_factory()() as session:
        return await posting_text.held(session, await session.get(Opportunity, role_id))


async def test_a_usable_description_is_what_the_role_holds_whatever_its_postings_hold(js):
    role_id = await role_with(posting(None, WHOLE), description=POSTING_WORDS, description_at=SEEN)
    assert await held(role_id) == (POSTING_WORDS, SEEN)


async def test_a_snippet_description_gives_way_to_a_whole_text_a_posting_holds(js):
    role_id = await role_with(posting(None, WHOLE), description=SNIPPET, description_at=NOW)
    assert await held(role_id) == (WHOLE, SEEN), "the posting's text and the day its posting was read"


async def test_the_fullest_text_of_the_roles_postings_is_the_fallback(js):
    short = POSTING_WORDS
    role_id = await role_with(posting(None, short, ext="1"), posting(None, WHOLE, ext="2"))
    assert (await held(role_id)).text == WHOLE


@pytest.mark.parametrize("system, text, detail_at", [
    (ats.BROWSER, WHOLE, None), (ats.BROWSER, WHOLE, SEEN), (ats.SCOUT, WHOLE, SEEN), (ats.IMPORT, WHOLE, SEEN),
    (EXAMPLE_SYSTEM, "<!DOCTYPE html>\nSkip to content\n" + WHOLE, SEEN), (EXAMPLE_SYSTEM, SNIPPET, SEEN)],
    ids=["an unopened browser posting", "an opened browser posting", "a scout's", "a tracker's", "a page dump",
         "a list's snippet"])
async def test_a_text_that_is_not_a_posting_of_a_source_is_never_the_text_a_role_holds(js, system, text, detail_at):
    role_id = await role_with(posting(None, text, system=system, detail_at=detail_at))
    assert await held(role_id) == ("", None)


async def test_a_role_holding_only_its_own_snippet_still_shows_it(js):
    role_id = await role_with(description=SNIPPET, description_at=SEEN)
    assert await held(role_id) == (SNIPPET, SEEN), "what the role page shows; it is not a text for anything else"
    assert whole.thin(SNIPPET)


# --- take(FromPostings) ---------------------------------------------------------------------------------------------


async def test_a_role_with_no_text_is_given_the_text_a_posting_holds_dated_as_the_posting_was_read(js):
    role_id = await role_with(posting(None, WHOLE))
    assert await run(role_id, posting_text.FromPostings(), NOW) == posting_text.HELD
    role, (row,), _ = await state(role_id)
    assert role.description == WHOLE and role.description_at == SEEN and row.text == WHOLE


async def test_a_snippet_description_gives_way_and_is_dated_now_so_its_research_is_queued_again_at_once(js):
    """The rule is `take`'s, for every word: a role researched before its text changed is queued again as the text is
    kept (`_after`). The bridge `take_whole`, which still runs in the deck until it goes, dates the text the same way
    and leaves the role done and out of date for the `requeue` action's scope `stale`; it is the one place the old
    rule holds (its test pins it: `test_lifecycle`). Nothing reaches `take(FromPostings)` with a role that is done
    today (`analysis.submit` holds it waiting; `packet_writer` runs only when there is no description)."""
    role_id = await role_with(posting(None, WHOLE), description=SNIPPET, description_at=SEEN,
                              analysis_state=analysis.DONE, analysed_at=RESEARCHED, analysis={"at": "x"})
    assert await run(role_id, posting_text.FromPostings(), NOW) == posting_text.HELD
    role, _, _ = await state(role_id)
    assert role.description == WHOLE and role.description_at == NOW
    assert role.analysis_state == analysis.WAITING and role.analysis_instance_id is not None, "queued again at once"
    assert not analysis.is_stale(role), "so it is no longer waiting to be queued"


async def test_a_usable_description_is_kept_and_nothing_of_it_changes(js):
    role_id = await role_with(posting(None, WHOLE), description=POSTING_WORDS, description_at=SEEN)
    assert await run(role_id, posting_text.FromPostings(), NOW) == posting_text.HELD
    role, _, _ = await state(role_id)
    assert (role.description, role.description_at) == (POSTING_WORDS, SEEN)


async def test_a_role_none_of_whose_postings_holds_a_text_is_given_none(js):
    role_id = await role_with(posting(None, SNIPPET), posting(None, WHOLE, system=ats.SCOUT, ext="2"))
    assert await run(role_id, posting_text.FromPostings(), NOW) is None
    role, _, _ = await state(role_id)
    assert role.description is None


async def test_a_text_given_to_a_role_researched_before_it_queues_the_research_again(js):
    role_id = await role_with(posting(None, WHOLE, detail_at=NOW), analysis_state=analysis.DONE,
                              analysed_at=RESEARCHED, analysis={"at": RESEARCHED.isoformat()})
    assert await run(role_id, posting_text.FromPostings(), NOW) == posting_text.HELD
    role, _, _ = await state(role_id)
    assert role.analysis_state == analysis.WAITING and role.analysis_instance_id is not None


async def test_the_posted_day_the_job_system_gave_is_set_with_the_text(js):
    role_id = await role_with(posting(None, WHOLE), posted=None)
    async with db.session_factory()() as session:
        (row,) = (await session.scalars(select(JobPosting).where(JobPosting.opportunity_id == role_id))).all()
        row.posted = date(2026, 9, 30)
        await session.commit()
    await run(role_id, posting_text.FromPostings(), NOW)
    role, _, _ = await state(role_id)
    assert role.posted == date(2026, 9, 30)


# --- take(Capture) --------------------------------------------------------------------------------------------------

FRAGMENT = "Programs Lead. We are hiring."
CUT = WHOLE[:900] + " ..."
WALL = WHOLE[:600] + "\nSign in to view this job."
TOO_LONG = "Paragraph one says one thing about the work and nothing else. " * (whole.CEILING_CHARS // 60 + 10)


async def capture(role_id, text, *, page=None, replaces=False, now=NOW):
    return await run(role_id, posting_text.Capture(text, page, replaces), now)


async def test_a_capture_is_kept_stripped_on_the_role_and_on_the_postings_with_none_or_a_snippet(js):
    role_id = await role_with(
        posting(None, None, ext="1"), posting(None, "", ext="2"), posting(None, WHOLE, ext="3"),
        posting(None, SNIPPET, system=ats.BROWSER, ext="4", detail_at=None),
        posting(None, "A snippet the system gave.", ext="5"))
    assert await capture(role_id, "  " + POSTING_WORDS + "  ") == posting_text.FETCHED
    role, rows, raw = await state(role_id)
    assert (role.description, role.description_at) == (POSTING_WORDS, NOW)
    texts = {p.ext_id: p.text for p in rows}
    assert texts["1"] == texts["2"] == texts["4"] == POSTING_WORDS, "none, an empty text and a browser snippet take it"
    assert texts["3"] == WHOLE and texts["5"] == "A snippet the system gave.", "any other text is the system's own"
    read = {p.ext_id: p.detail_at for p in rows}  # every posting starts SEEN; the browser snippet's was never read
    assert read["1"] == read["2"] == read["4"] == NOW, "a posting that took the text is marked read, as a read does"
    assert read["3"] == read["5"] == SEEN, "a posting that kept its own text keeps the day it was read"
    assert sorted(r.reader for r in raw) == ["browser:page"] * 3 and all(r.content_type == "text/plain" for r in raw)


@pytest.mark.parametrize("text", [FRAGMENT, CUT, WALL, TOO_LONG, "", "   "],
                         ids=["a fragment", "cut off", "a wall", "too long", "empty", "blank"])
async def test_a_capture_that_is_not_the_whole_posting_is_refused_and_nothing_changes(js, text):
    role_id = await role_with(posting(None, None))
    assert await capture(role_id, text) is None
    role, (row,), raw = await state(role_id)
    assert role.description is None and role.description_at is None and row.text is None and raw == []


async def test_a_capture_for_a_role_with_no_source_is_refused(js):
    role_id = await role_with(posting(None, None), source_url=None, source_kind=None, sourced_at=None)
    assert await capture(role_id, WHOLE) is None
    role, (row,), _ = await state(role_id)
    assert role.description is None and row.text is None, "only a source's own text is a description"


async def test_a_capture_never_overwrites_a_usable_description_unless_it_replaces_it(js):
    role_id = await role_with(posting(None, None), description=POSTING_WORDS, description_at=SEEN)
    assert await capture(role_id, WHOLE) == posting_text.HELD
    role, (row,), raw = await state(role_id)
    assert (role.description, role.description_at, row.text, raw) == (POSTING_WORDS, SEEN, None, [])
    assert await capture(role_id, WHOLE, replaces=True) == posting_text.FETCHED
    role, (row,), _ = await state(role_id)
    assert (role.description, role.description_at, row.text) == (WHOLE, NOW, WHOLE)


async def test_a_capture_takes_the_place_of_a_snippet_description(js):
    role_id = await role_with(description=SNIPPET, description_at=SEEN)
    assert await capture(role_id, WHOLE) == posting_text.FETCHED
    assert (await state(role_id))[0].description == WHOLE


async def test_a_capture_with_its_page_keeps_the_page_as_the_one_raw_copy_of_every_posting_even_when_held(js):
    page = b"<html><body>" + WHOLE.encode() + b"</body></html>"
    role_id = await role_with(posting(None, None, ext="1"), posting(None, WHOLE, ext="2"))
    assert await capture(role_id, WHOLE, page=page) == posting_text.FETCHED
    _, _, raw = await state(role_id)
    assert [(r.reader, r.content_type, raw_copies.inflate(r.body)) for r in raw] == [
        ("code:page", "text/html", page)] * 2, "one row each, not a text row beside it"


async def test_a_role_that_holds_its_text_keeps_the_page_a_capture_came_from_and_nothing_else_changes(js):
    page = b"<html><body>" + WHOLE.encode() + b"</body></html>"
    role_id = await role_with(posting(None, None, ext="1"), posting(None, WHOLE, ext="2"),
                              description=POSTING_WORDS, description_at=SEEN)
    assert await capture(role_id, WHOLE, page=page) == posting_text.HELD
    role, rows, raw = await state(role_id)
    assert (role.description, role.description_at, rows[0].text) == (POSTING_WORDS, SEEN, None)
    assert [r.reader for r in raw] == ["code:page"] * 2


async def snippet_copy(role_id) -> None:
    """The role's browser posting has a raw copy of a listing's snippet: the cleansed copy the stages read is thin."""
    async with db.session_factory()() as session:
        (row,) = (await session.scalars(select(JobPosting))).all()
        await raw_copies._keep_report(session, row, {"title": "Programs Lead", "snippet": SNIPPET}, reader="browser",
                                     text=SNIPPET, now=SEEN)
        assert len((await cleansed.copy_of(session, OWNER, role_id)).text_clean) < cleansed.MIN_TEXT
        await session.commit()


async def test_a_capture_enters_the_text_in_the_cleansed_copy_in_the_same_call(js):
    role_id = await role_with(posting(None, SNIPPET, system=ats.BROWSER, detail_at=None), source_url=OWN)
    await snippet_copy(role_id)
    assert await capture(role_id, WHOLE) == posting_text.FETCHED
    async with db.session_factory()() as session:
        assert (await cleansed.copy_of(session, OWNER, role_id)).text_clean == WHOLE


async def test_an_easy_apply_boards_own_posting_enters_its_text_in_the_cleansed_copy_in_the_same_call_F4(js):
    """Found when the board's own posting was put on the role and on its postings by hand and no raw copy was kept: the
    posting's newest raw copy was still the listing's snippet, so the copy the stages read stayed thin."""
    role_id = await role_with(posting(None, SNIPPET, system=ats.BROWSER, detail_at=None))
    await snippet_copy(role_id)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        said = await sourced.accept_source(session, role, "https://www.boardone.test/jobs/view/1001", "board", WHOLE,
                                           True, "owner")
        await session.commit()
    assert said == sourced.SOURCED
    role, (row,), _ = await state(role_id)
    assert role.description == WHOLE and row.text == WHOLE
    async with db.session_factory()() as session:
        assert (await cleansed.copy_of(session, OWNER, role_id)).text_clean == WHOLE


async def test_one_capture_writes_one_sourced_line_and_leaves_sourced_at_alone_F5(js):
    """Found when keeping a capture asked `accept_source` again whether the stored link was one posting: it wrote a
    second "sourced" line and started the source's clock again."""
    sourced_at = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)
    role_id = await role_with(posting(None, None), source_url=OWN, sourced_at=sourced_at)
    assert await capture(role_id, WHOLE) == posting_text.FETCHED
    role, _, _ = await state(role_id)
    assert role.sourced_at == sourced_at
    async with db.session_factory()() as session:
        lines = (await session.scalars(select(TrailEvent).where(TrailEvent.link_id == role_id,
                                                                TrailEvent.event == "sourced"))).all()
    assert lines == []


# --- take(Source) ---------------------------------------------------------------------------------------------------


def page_reply(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers=HTML, text=PAGE)


async def test_the_source_is_read_and_its_text_is_kept_on_the_role_and_its_postings_F3(js, monkeypatch):
    """Found when a text read from the source was put on a posting that had a raw row (a listing's snippet) and no
    raw row of its own: the cleansed copy the stages read stayed the snippet."""
    seen = web(monkeypatch, page_reply)
    role_id = await role_with(posting(None, None, system=ats.BROWSER, ext="1", detail_at=None))
    async with db.session_factory()() as session:
        (row,) = (await session.scalars(select(JobPosting))).all()
        await raw_copies._keep_report(session, row, {"title": "Programs Lead", "snippet": SNIPPET}, reader="browser",
                                     text=SNIPPET, now=SEEN)
        await session.commit()
    assert await run(role_id, now=NOW) == posting_text.FETCHED
    assert [r.url.path for r in seen.pages] == ["/fabrikam"]
    role, (row,), raw = await state(role_id)
    assert role.description == WHOLE and role.description_at == NOW and row.text == WHOLE, "a browser snippet takes it"
    assert raw[-1].reader == "code:text" and raw_copies.inflate(raw[-1].body) == WHOLE.encode()
    async with db.session_factory()() as session:
        assert (await cleansed.copy_of(session, OWNER, role_id)).text_clean == WHOLE


async def test_a_role_that_holds_its_text_and_one_that_is_not_sourced_are_read_for_nothing(js, monkeypatch):
    seen = web(monkeypatch, lambda request: pytest.fail(f"nothing should be read, and {request.url} was"))
    holding = await role_with(description=POSTING_WORDS, description_at=SEEN)
    assert await run(holding) == posting_text.HELD
    unsourced = await role_with(who="Initech", sourced_at=None, source_url=None, source_kind=None)
    assert await run(unsourced) is None
    assert seen.pages == [] and seen.robots == []


@pytest.mark.parametrize("status", [404, 410])
async def test_a_source_that_says_the_posting_is_gone_ends_the_role_and_take_says_so(js, monkeypatch, status):
    web(monkeypatch, lambda request: httpx.Response(status, text="Not here"))
    role_id = await role_with(url=CAREER_PAGE, source_url=CAREER_PAGE)
    assert await run(role_id, now=NOW) == posting_text.GONE
    role, _, _ = await state(role_id)
    assert role.status == "gone_quiet" and role.ended_reason == "posting_gone"
    async with db.session_factory()() as session:
        (line,) = (await session.scalars(select(TrailEvent).where(TrailEvent.link_id == role_id,
                                                                  TrailEvent.event == "posting_removed"))).all()
    assert CAREER_PAGE in line.detail and str(status) in line.detail


@pytest.mark.parametrize("status, text", [
    (403, "Forbidden"), (429, "Slow down"), (500, "Oops"), (503, "Try later"),
    (200, "<html><body><p>Sign in to view this job. Create an account.</p></body></html>")],
    ids=["a block", "a rate limit", "a 500", "a 503", "a sign-in wall"])
async def test_a_block_a_rate_limit_a_server_error_or_a_wall_is_never_gone(js, monkeypatch, status, text):
    web(monkeypatch, lambda request: httpx.Response(status, headers=HTML, text=text))
    role_id = await role_with(url=CAREER_PAGE, source_url=CAREER_PAGE)
    assert await run(role_id, now=NOW) is None
    role, _, _ = await state(role_id)
    assert role.status != "gone_quiet" and role.ended_reason is None and role.description is None


async def test_a_gone_source_leaves_a_role_that_is_not_in_play_as_it_is(js, monkeypatch):
    web(monkeypatch, lambda request: httpx.Response(404, text="Not here"))
    role_id = await role_with(url=CAREER_PAGE, source_url=CAREER_PAGE, status=stages.GONE_QUIET)
    assert await run(role_id, now=NOW) is None
    role, _, _ = await state(role_id)
    assert role.status == stages.GONE_QUIET and role.ended_reason != stages.POSTING_GONE
    async with db.session_factory()() as session:
        assert (await session.scalar(select(TrailEvent.id).where(TrailEvent.link_id == role_id,
                                                                 TrailEvent.event == "posting_removed"))) is None


async def test_the_reads_made_ahead_of_a_step_are_taken_and_nothing_is_read_inside_its_transaction(js, monkeypatch):
    """`ahead` reads with no transaction open; `take`, inside a savepoint that cannot be released, reads nothing new
    (`db.strict_io` fails the test if it did)."""
    seen = web(monkeypatch, page_reply)
    role_id = await role_with()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        answers = await posting_text.ahead(session, role)
        assert len(answers) == 1 and len(seen.pages) == 1, "read once, ahead"
        with polite.read_ahead(answers):
            async with session.begin_nested():
                assert await posting_text.take(session, role, now=NOW) == posting_text.FETCHED
        await session.commit()
    assert len(seen.pages) == 1, "the step read nothing"
    assert (await state(role_id))[0].description == WHOLE


async def test_nothing_is_read_ahead_for_a_role_that_needs_no_read(js, monkeypatch):
    seen = web(monkeypatch, lambda request: pytest.fail(f"nothing should be read, and {request.url} was"))
    role_id = await role_with(description=POSTING_WORDS, description_at=SEEN)
    async with db.session_factory()() as session:
        assert await posting_text.ahead(session, await session.get(Opportunity, role_id)) == {}
    assert seen.pages == []


async def test_a_text_the_app_reads_is_kept_whole_at_any_length(js, monkeypatch):
    long = made_up_words("read", 60_000)
    page = ("<html><head><title>Programs Lead - Acme Careers</title></head><body><main>"
            + "".join(f"<p>{s}</p>" for s in long.split(". ")) + "</main></body></html>")
    web(monkeypatch, lambda request: httpx.Response(200, headers=HTML, text=page))
    role_id = await role_with(posting(None, None))
    assert await run(role_id, now=NOW) == posting_text.FETCHED
    role, (row,), _ = await state(role_id)
    assert role.description == row.text and len(role.description) > 55_000, "nothing was cut"
