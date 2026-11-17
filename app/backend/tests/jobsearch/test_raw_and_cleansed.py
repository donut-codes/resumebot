"""The raw and the cleansed copy of a read, in shadow: every
read of a posting keeps exactly what was fetched, with its source, time and hash, and a cleansed copy stamped with the
cleaner's version; a cleaner version bump rebuilds the copies from raw with no fetch. Nothing reads them. Real
adapters over a mock transport; synthetic data only."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app import db
from app.jobsearch import adapters
from app.jobsearch.adapters import _posting
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import CleansedPosting, Employer, JobPosting, Opportunity, RawPosting
from app.jobsearch.postings import cleanse, cleansed, raw_copies, reparse, role_postings
from app.jobsearch.postings import index as postings
from app.sites import feed_read
from tests.builders import browser_card, browser_world, scan_hand_in
from tests.support import EXAMPLE_SYSTEM, OWNER, POSTING_WORDS

NIGHT = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
ITEM = {"id": 11, "title": "Director of Programs", "location": {"name": "Remote"},
        "absolute_url": "https://boards.example.test/acme/jobs/11", "first_published": "2026-10-01T00:00:00Z",
        "content": "<p>Lead the programs team.</p><p>Pay range: $120,000 - $150,000 per year</p>"
                   "<p>Acme is an equal opportunity employer and does not discriminate against anyone.</p>"}


def _fetcher(board: dict, detail: dict | None = None) -> Fetcher:
    """Feedone's list answers `board`; the posting's own address answers `detail`; nothing else is there."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/boards/acme/jobs":
            return httpx.Response(200, json=board)
        if detail is not None and request.url.path == "/v1/boards/acme/jobs/11":
            return httpx.Response(200, json=detail)
        return httpx.Response(404)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)


async def _acme(session):
    acme = await employers.find_or_make(session, OWNER, "Acme")
    acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": "acme"}
    await session.flush()
    return acme


async def _raw(session, kind: str | None = None) -> list[RawPosting]:
    q = select(RawPosting).order_by(RawPosting.seq)
    return list((await session.scalars(q.where(RawPosting.kind == kind) if kind else q)).all())


async def _cleansed(session) -> list[CleansedPosting]:
    """The cleansed copies, oldest first: by when each was made, then by its raw copy's place (copies made at one
    instant, in one test's clock, come in the order their raw copies were kept)."""
    return list((await session.scalars(select(CleansedPosting).outerjoin(
        RawPosting, RawPosting.id == CleansedPosting.raw_id).order_by(
        CleansedPosting.cleaned_at, RawPosting.seq.nulls_first()))).all())


# --- the raw copy ---------------------------------------------------------------------------------------------------


async def test_a_read_keeps_each_item_as_it_came_with_its_source_time_and_hash(js):
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await postings.refresh(session, OWNER, _fetcher({"jobs": [ITEM]}), now=NIGHT)
        (raw,) = await _raw(session)
        body = raw_copies.inflate(raw.body)
        assert json.loads(body) == ITEM
        assert raw.body_sha256 == hashlib.sha256(body).hexdigest() and raw.body_bytes == len(body)
        assert (raw.kind, raw.reader, raw.ext_id, raw.source_id) == ("item", f"adapter:{EXAMPLE_SYSTEM}", "11", acme.id)
        assert (raw.fetched_at, raw.http_status, raw.content_type) == (NIGHT, 200, "application/json")
        assert raw.url == ITEM["absolute_url"] and not raw.no_raw


async def test_the_same_bytes_again_write_nothing_and_a_changed_posting_adds_a_second_row(js):
    changed = {**ITEM, "title": "Head of Programs"}
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetcher({"jobs": [ITEM]}), now=NIGHT)
        await postings.refresh(session, OWNER, _fetcher({"jobs": [ITEM]}), now=NIGHT + timedelta(days=1))
        assert len(await _raw(session)) == 1 and len(await _cleansed(session)) == 1
        await postings.refresh(session, OWNER, _fetcher({"jobs": [changed]}), now=NIGHT + timedelta(days=2))
        rows = await _raw(session)
        assert [json.loads(raw_copies.inflate(r.body))["title"] for r in rows] == [
            "Director of Programs", "Head of Programs"], "the history is kept"
        assert (await _cleansed(session))[-1].title == "Head of Programs"


async def test_a_posting_the_reader_gave_no_item_for_keeps_no_raw_copy(js):
    async with db.session_factory()() as session:
        acme = await _acme(session)
        plain = {"ext_id": "1", "title": "A", "location": None, "work_mode": None, "comp": None, "comp_min": None,
                 "comp_max": None, "posted": None, "url": None, "apply_url": None, "text": None, "department": None}
        counts = dict.fromkeys(("seen", "new", "closed", "reopened"), 0)
        listing = type("L", (), {"postings": [plain], "truncated": False, "note": None, "asked": None})()
        await postings.store(session, OWNER, listing, source_id=acme.id, ats_name=EXAMPLE_SYSTEM, company="Acme",
                             now=NIGHT, counts=counts)
        assert await _raw(session) == [] and counts["new"] == 1


async def test_a_recipes_item_says_which_recipe_version_read_it(js):
    async with db.session_factory()() as session:
        acme = await _acme(session)
        item = {"slug": "x-1", "name": "Programs Lead"}
        listed = _posting(raw=item, ext_id="x-1", title="Programs Lead", text="Lead programs.")
        counts = dict.fromkeys(("seen", "new", "closed", "reopened"), 0)
        listing = type("L", (), {"postings": [listed], "truncated": False, "note": None, "asked": None})()
        await postings.store(session, OWNER, listing, source_id=acme.id, ats_name="own_site", company="Acme",
                             now=NIGHT, counts=counts, reader="recipe:request", reader_version=3)
        (raw,) = await _raw(session)
        assert (raw.reader, raw.reader_version, json.loads(raw_copies.inflate(raw.body))) == ("recipe:request", 3, item)


async def test_a_detail_answer_is_kept_as_the_bytes_the_site_sent_and_joins_the_item_in_the_cleansed_copy(js):
    listed = {k: v for k, v in ITEM.items() if k != "content"}
    detail = {**ITEM, "content": "<p>The whole description of the programs job.</p>"}
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetcher({"jobs": [listed]}), now=NIGHT)
        posting = (await session.scalars(select(JobPosting))).one()
        assert await postings.fill_details(session, _fetcher({"jobs": []}, detail), [posting], NIGHT) == 1
        (answer,) = await _raw(session, "detail")
        assert json.loads(raw_copies.inflate(answer.body)) == detail and answer.http_status == 200
        assert answer.url.endswith("/v1/boards/acme/jobs/11") and answer.reader == f"adapter:{EXAMPLE_SYSTEM}"
        current = (await _cleansed(session))[-1]
        assert current.raw_id == answer.id and len(current.from_raw) == 2
        assert current.title == "Director of Programs"
        assert current.text_clean == "The whole description of the programs job."


#: An employer's own career page that shows the whole posting: a line to each paragraph, past what a page must show.
OWN_PAGE = "https://careers.example.test/jobs/programs-lead"
OWN_PARAGRAPHS = [f"Paragraph {i} of the posting says one thing about the work and nothing else." for i in range(1, 21)]
OWN_WHOLE = "\n".join(OWN_PARAGRAPHS)
OWN_HTML = ("<html><head><title>Programs Lead - Acme Careers</title></head><body><main>"
            + "".join(f"<p>{p}</p>" for p in OWN_PARAGRAPHS) + "</main></body></html>")


async def test_a_detail_answer_that_came_with_no_reply_keeps_its_text_as_a_raw_copy_and_the_copy_holds_it_at_once(js):
    """Found when a role's own-site posting read whole stayed a snippet in the copy the stages read: an answer made of
    plain data (an employer's own page, a site recipe's detail) kept its text on the posting and no raw row, so the
    posting's newest raw copy was still the listing's snippet and a repair in the stage turn pushed the text across."""
    snippet = "Programs Lead\n6 Locations"

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=OWN_HTML)

    async with db.session_factory()() as session:
        employer = Employer(owner=OWNER, key="acme", name="Acme", ats="own_site", board={})
        role = Opportunity(owner=OWNER, slug="p", company="Acme", title="Programs Lead", match_key="acme|programs lead")
        session.add_all([employer, role])
        await session.flush()
        posting = JobPosting(owner=OWNER, employer_id=employer.id, ats="own_site", ext_id="own-1", company="Acme",
                             title="Programs Lead", match_key="acme|programs lead", url=OWN_PAGE, text=snippet,
                             first_seen=NIGHT, last_seen=NIGHT, links=[], opportunity_id=role.id)
        session.add(posting)
        await session.flush()
        await raw_copies._keep_report(session, posting, {"title": "Programs Lead", "text": snippet},
                                     reader="recipe:request", text=snippet, kind=raw_copies.ITEM, now=NIGHT)
        assert len((await cleansed.copy_of(session, OWNER, role.id)).text_clean) < cleansed.MIN_TEXT
        fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)
        assert await postings.fill_details(session, fetch, [posting], NIGHT) == 1
        assert posting.text == OWN_WHOLE
        assert (await cleansed.copy_of(session, OWNER, role.id)).text_clean == OWN_WHOLE
        newest = (await _raw(session, "detail"))[-1]
        assert newest.reader == "code:text" and raw_copies.inflate(newest.body) == OWN_WHOLE.encode()


def test_a_raw_copy_of_text_the_app_read_as_plain_data_is_not_parsed_again_from_its_bytes():
    assert "code:text" in reparse.NOT_REPARSED


# --- the cleansed copy ----------------------------------------------------------------------------------------------


async def test_the_cleansed_copy_is_stamped_with_the_cleaner_version_and_names_what_it_took_out(js):
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetcher({"jobs": [ITEM]}), now=NIGHT)
        (c,) = await _cleansed(session)
        assert c.cleaner_version == cleanse.CLEANER_VERSION and c.employer == "Acme" and c.locations == ["Remote"]
        assert "equal opportunity" not in c.text_clean and "Lead the programs team." in c.text_clean
        assert [(r["rule"], r["lines"]) for r in c.removed] == [("equal_opportunity", 1)]
        assert "Pay range: $120,000" in c.text_clean and (c.pay_min, c.pay_max) == (120000, 150000)
        assert c.marks["pay"]["source"] == "code" and c.marks["pay"]["quote"].startswith("Pay range")


def test_a_line_with_a_pay_figure_is_never_taken_out_and_a_text_of_only_boilerplate_stays_whole():
    pay = ("We are an equal opportunity employer. The pay range is $91,000 - $108,000 a year, with health, dental "
           "and 401k.")
    text = "Lead the work.\n" + pay
    clean, removed = cleanse.strip_boilerplate(text)
    assert clean == text and removed == []
    only = "We are an equal opportunity employer and do not discriminate."
    assert cleanse.strip_boilerplate(only) == (only, [])


def test_benefits_and_how_to_apply_lines_are_named_and_the_duties_stay():
    text = ("Run the weekly review.\nWe offer health insurance, dental, vision and a 401k plan to every employee.\n"
            "To apply, send your resume to the careers page.\nOwn the budget.")
    clean, removed = cleanse.strip_boilerplate(text)
    assert clean == "Run the weekly review.\nOwn the budget."
    assert [r["rule"] for r in removed] == ["benefits", "how_to_apply"]


# --- what the owner's browser and the scout report ------------------------------------------------------------------


async def test_a_listing_the_browser_reported_is_kept_as_it_was_reported(js):
    await browser_world()
    card = browser_card(1, description=POSTING_WORDS)
    await scan_hand_in([card], now=NIGHT)
    async with db.session_factory()() as session:
        (raw,) = await _raw(session)
        assert (raw.reader, raw.kind, raw.no_raw) == ("browser", "item", False)
        sent = json.loads(raw_copies.inflate(raw.body))
        assert sent["title"] == card["title"] and sent["description"] == POSTING_WORDS
        (c,) = await _cleansed(session)
        assert c.title == card["title"] and c.text_clean.startswith("Programs Lead")


async def test_a_later_list_read_that_gives_a_posting_its_whole_text_makes_a_copy_that_holds_it_at_once(js):
    """The copy the stages read follows a posting's text with no repair after the fact (the retired `keep_whole_text`
    and the `thin_roles` block of `lifecycle.settle` did that): a read of the list keeps the raw item it wrote the text
    from, so the newest copy holds the text the posting does."""
    whole_item = {**ITEM, "content": "<p>" + POSTING_WORDS.replace("\n", "</p><p>") + "</p>"}
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetcher({"jobs": [ITEM]}), now=NIGHT)
        assert len((await _cleansed(session))[-1].text_clean) < cleansed.MIN_TEXT, "the listing gave a snippet"
        await postings.refresh(session, OWNER, _fetcher({"jobs": [whole_item]}), now=NIGHT + timedelta(days=1))
        (posting,) = (await session.scalars(select(JobPosting))).all()
        assert len(posting.text) >= cleansed.MIN_TEXT
        assert len((await _cleansed(session))[-1].text_clean) >= cleansed.MIN_TEXT


async def test_a_later_browser_report_that_gives_a_posting_its_whole_text_makes_a_copy_that_holds_it_at_once(js):
    """The same for the owner's browser: a listing reported with its snippet, then reported with the page's text."""
    await browser_world()
    await scan_hand_in([browser_card(1)], now=NIGHT)
    async with db.session_factory()() as session:
        assert len((await _cleansed(session))[-1].text_clean) < cleansed.MIN_TEXT, "the listing gave a snippet"
    await scan_hand_in([browser_card(1, description=POSTING_WORDS)], now=NIGHT + timedelta(days=1))
    async with db.session_factory()() as session:
        (posting,) = (await session.scalars(select(JobPosting))).all()
        assert posting.text == POSTING_WORDS and posting.detail_at is not None
        assert len((await _cleansed(session))[-1].text_clean) >= cleansed.MIN_TEXT


REPORT = {"owner": OWNER, "company": "Fabrikam", "title": "Lead", "match_key": "fabrikam|lead",
          "description": "Lead the programs team.", "url": "https://jobs.example.test/1"}


async def test_a_role_the_scout_reported_is_kept_as_it_was_reported(js):
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="s", company="Fabrikam", title="Lead", match_key="fabrikam|lead")
        session.add(role)
        await session.flush()
        await role_postings.log_reported(session, {**REPORT, "id": role.id})
        (raw,) = await _raw(session)
        sent = json.loads(raw_copies.inflate(raw.body))
        assert (raw.reader, sent["description"], sent["id"]) == ("scout", REPORT["description"], str(role.id))
        assert (await _cleansed(session))[0].text_clean == REPORT["description"]


async def test_a_row_of_the_tracker_import_is_kept_as_it_was_read(js):
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="t", company="Fabrikam", title="Lead", match_key="fabrikam|lead")
        session.add(role)
        await session.flush()
        await role_postings.log_imported(session, [{**REPORT, "id": role.id}])
        (raw,) = await _raw(session)
        assert (raw.reader, json.loads(raw_copies.inflate(raw.body))["title"]) == ("import", "Lead")


# --- the rebuild ----------------------------------------------------------------------------------------------------


def _next_version(monkeypatch) -> int:
    monkeypatch.setattr(cleanse, "CLEANER_VERSION", cleanse.CLEANER_VERSION + 1)
    return cleanse.CLEANER_VERSION


async def test_a_new_cleaner_version_rebuilds_the_copies_from_raw_with_no_fetch(js, monkeypatch):
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetcher({"jobs": [ITEM]}), now=NIGHT)
        raws = await _raw(session)

        def no_fetch(*a, **k):
            raise AssertionError("recleanse must not fetch")

        monkeypatch.setattr(Fetcher, "request", no_fetch)
        same = await raw_copies.recleanse(session, OWNER)
        assert (same["postings"], same["rebuilt"]) == (1, 0), "nothing to do at the same version"
        version = _next_version(monkeypatch)
        assert (await raw_copies.recleanse(session, OWNER))["rebuilt"] == 1
        assert (await raw_copies.recleanse(session, OWNER))["rebuilt"] == 0
        old, new = await _cleansed(session)
        assert (old.cleaner_version, new.cleaner_version) == (version - 1, version)
        assert new.raw_id == old.raw_id == raws[0].id and new.text_clean == old.text_clean
        assert await _raw(session) == raws, "the raw copies are not touched"


async def test_the_rebuild_runs_the_readers_parse_again_on_the_raw_bytes(js, monkeypatch):
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetcher({"jobs": [ITEM]}), now=NIGHT)
        reader = adapters.ADAPTERS[EXAMPLE_SYSTEM]
        original = reader.parse_item

        def better(r, board):
            post = original(r, board)
            post["title"] = post["title"].upper()
            return post

        monkeypatch.setattr(reader, "parse_item", better)
        _next_version(monkeypatch)
        counts = await raw_copies.recleanse(session, OWNER)
        assert (counts["reparsed"], counts["stored"]) == (1, {})
        assert (await _cleansed(session))[-1].title == "DIRECTOR OF PROGRAMS"


async def test_a_detail_answer_is_made_again_by_giving_its_stored_reply_back_to_the_adapter(js, monkeypatch):
    listed = {k: v for k, v in ITEM.items() if k != "content"}
    detail = {**ITEM, "content": "<p>The whole description of the programs job.</p>"}
    async with db.session_factory()() as session:
        await _acme(session)
        await postings.refresh(session, OWNER, _fetcher({"jobs": [listed]}), now=NIGHT)
        posting = (await session.scalars(select(JobPosting))).one()
        await postings.fill_details(session, _fetcher({"jobs": []}, detail), [posting], NIGHT)
        text_of = feed_read.html_to_text
        monkeypatch.setattr(feed_read, "html_to_text", lambda s: (text_of(s) or "").upper())
        _next_version(monkeypatch)
        counts = await raw_copies.recleanse(session, OWNER)
        assert (counts["reparsed"], counts["stored"]) == (2, {}), "the list item and the detail answer"
        assert (await _cleansed(session))[-1].text_clean == "THE WHOLE DESCRIPTION OF THE PROGRAMS JOB."


async def test_a_raw_row_no_reader_can_parse_again_starts_from_its_stored_parse_and_is_named(js, monkeypatch):
    async with db.session_factory()() as session:
        acme = await _acme(session)
        session.add(JobPosting(owner=OWNER, employer_id=acme.id, ats=EXAMPLE_SYSTEM, ext_id="old-1", company="Acme",
                               title="Programs Lead", location="Remote", text="Lead programs.\nOwn the budget.",
                               first_seen=NIGHT, last_seen=NIGHT, links=[]))
        await session.flush()
        await raw_copies.keep_legacy(session, OWNER)
        _next_version(monkeypatch)
        counts = await raw_copies.recleanse(session, OWNER)
        assert (counts["rebuilt"], counts["reparsed"], counts["stored"]) == (1, 0, {"legacy": 1})
        old, new = await _cleansed(session)
        assert new.cleaner_version == old.cleaner_version + 1 and new.text_clean == old.text_clean == (
            "Lead programs.\nOwn the budget.")


async def test_a_posting_read_before_raw_copies_were_kept_enters_as_no_raw_and_only_once(js):
    async with db.session_factory()() as session:
        acme = await _acme(session)
        session.add(JobPosting(owner=OWNER, employer_id=acme.id, ats=EXAMPLE_SYSTEM, ext_id="old-1", company="Acme",
                               title="Programs Lead", location="Remote", text="Lead programs.\nOwn the budget.",
                               first_seen=NIGHT, last_seen=NIGHT, links=[]))
        session.add(JobPosting(owner=OWNER, employer_id=acme.id, ats=EXAMPLE_SYSTEM, ext_id="old-2", company="Acme",
                               title="No text here", first_seen=NIGHT, last_seen=NIGHT, links=[]))
        await session.flush()
        assert await raw_copies.keep_legacy(session, OWNER) == 1
        assert await raw_copies.keep_legacy(session, OWNER) == 0
        (raw,) = await _raw(session)
        (c,) = await _cleansed(session)
        assert (raw.kind, raw.no_raw) == ("legacy", True)
        assert raw_copies.inflate(raw.body) == b"Lead programs.\nOwn the budget."
        assert c.no_raw and c.title == "Programs Lead" and c.text_clean == "Lead programs.\nOwn the budget."
        assert await session.scalar(select(func.count()).select_from(CleansedPosting)) == 1
