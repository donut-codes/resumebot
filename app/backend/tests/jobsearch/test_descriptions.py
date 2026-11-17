"""The text of every posting that passes the code gates is fetched (docs/PRD.md 39.6): read by code from its job system,
with no Claude, under the polite rules. A posting a code gate stopped keeps its list entry and no text; one the title
check stopped, and one that became a role, passed the code gates and have it. A posting a later change to the owner's
settings lets through is in the set the next time the step runs. With the limit unlimited, a night reads the text of
every posting in that set and of no other. Adapters are stand-ins; no network. Everything is made up."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app import db
from app.jobsearch import actions, adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import boards_night
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.postings import cleansed, read_progress, whole
from app.jobsearch.postings import index as postings
from app.sites import where
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

NOW = datetime.now(UTC)
SEEN = NOW - timedelta(days=2)


@dataclass
class Listing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class Board:
    """A job system's reader: it lists what it is told and gives a page for each posting, noting which it was asked."""

    listed: list[dict] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)

    async def read(self, fetch, board):
        return Listing(self.listed)

    async def detail(self, fetch, board, posting):
        self.asked.append(posting["ext_id"])
        return {"text": f"The whole posting for {posting['title']}."}


def _fetch() -> Fetcher:
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


@pytest.fixture
def board(monkeypatch):
    reader = Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: reader})
    monkeypatch.setattr(polite, "make", _fetch)
    return reader


def _post(source, ext_id: str, **more) -> JobPosting:
    more.setdefault("ats", EXAMPLE_SYSTEM)
    more.setdefault("filtered_at", SEEN)
    more.setdefault("route", "titles")
    return JobPosting(owner=OWNER, employer_id=source, ext_id=ext_id, company="Acme", title=ext_id,
                      match_key=match_key("Acme", ext_id), first_seen=SEEN, last_seen=SEEN, links=[],
                      url=f"https://jobs.example.test/{ext_id}", **more)


async def _acme(session):
    acme = await employers.find_or_make(session, OWNER, "Acme")
    acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": "acme"}
    await session.flush()
    return acme


async def _the_set(session) -> set[str]:
    return {p.ext_id for p in (await session.scalars(select(JobPosting).where(*postings.needing_text(OWNER)))).all()}


async def _texts(session) -> dict[str, str | None]:
    return {p.ext_id: p.text for p in (await session.scalars(select(JobPosting))).all()}


async def _every_kind(session, acme) -> None:
    """One posting of every kind: those the text is fetched for, and those it is not."""
    role = Opportunity(owner=OWNER, slug="a-role", company="Acme", title="a role", match_key="acme|a role")
    session.add(role)
    await session.flush()
    session.add_all([
        # the set: open, through the code gates, no text, a job system to ask
        _post(acme.id, "passed"),
        _post(acme.id, "a-tab-said-yes", route="reader", judged_by="titles"),
        _post(acme.id, "title-check-stopped", filtered_by="not your kind of job: sales", judged_by="titles",
              route=None),
        _post(acme.id, "a-role-now", route=None, opportunity_id=role.id),
        _post(acme.id, "tried-long-ago", detail_at=NOW - timedelta(days=postings.DETAIL_RETRY_DAYS + 1)),
        _post(acme.id, "empty-text", text=""),
        _post(acme.id, "a-snippet", text="The board gave it."),  # under cleansed.MIN_TEXT: not a posting's text
        # not the set
        _post(acme.id, "code-gate-stopped", filtered_by="the place", judged_by="gate", route=None),
        _post(acme.id, "stopped-before-steps-were-recorded", filtered_by="the place", route=None),
        _post(acme.id, "not-filtered-yet", filtered_at=None, route=None),
        _post(acme.id, "has-its-text", text="The board gave it. " * 20),
        _post(acme.id, "closed", closed_at=SEEN),
        _post(acme.id, "not-checked", not_checked_at=SEEN),
        _post(acme.id, "tried-an-hour-ago", detail_at=NOW - timedelta(hours=1)),
        *[_post(acme.id, f"read-by-{system}", ats=system) for system in ("browser", "job_site", "scout",
                                                                         "import")],
    ])
    await session.flush()


async def test_a_snippet_padded_with_white_space_is_a_snippet_to_the_set_and_to_the_read(js):
    """A text is thin by what it says stripped (`whole.thin`), so a snippet with lines of white space after it is still
    a snippet: its posting is in the set and is read, as it is for every other reader of the rule."""
    padded = "A snippet the board gave." + "\n" * 400
    assert len(padded) >= cleansed.MIN_TEXT and whole.thin(padded)
    async with db.session_factory()() as session:
        acme = await _acme(session)
        session.add_all([_post(acme.id, "padded", text=padded), _post(acme.id, "has-its-text", text="It. " * 100)])
        await session.flush()
        assert await _the_set(session) == {"padded"}
        plan = await postings.plan_details(session, list((await session.scalars(select(JobPosting))).all()))
        assert [p.ext_id for p in plan.todo] == ["padded"]


THE_SET = {"passed", "a-tab-said-yes", "title-check-stopped", "a-role-now", "tried-long-ago", "empty-text", "a-snippet"}


async def test_the_set_is_every_open_posting_the_code_gates_let_through_that_has_no_text_and_a_system_to_ask(js):
    async with db.session_factory()() as session:
        await _every_kind(session, await _acme(session))
        assert await _the_set(session) == THE_SET


async def test_unlimited_a_night_reads_the_text_of_every_posting_in_the_set_and_of_no_other(js, board):
    async with db.session_factory()() as session:
        await _every_kind(session, await _acme(session))
        done = await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": -1})
        texts = await _texts(session)
    assert done == {"filled": len(THE_SET), "left": 0}
    assert set(board.asked) == THE_SET
    assert {n for n, t in texts.items() if t == f"The whole posting for {n}."} == THE_SET
    assert texts["has-its-text"] == "The board gave it. " * 20 and texts["code-gate-stopped"] is None
    assert texts["not-checked"] is None and texts["closed"] is None and texts["tried-an-hour-ago"] is None


async def test_a_limit_reads_that_many_a_night_the_likeliest_first_and_the_rest_wait(js, board):
    """The order is the classifier's, the likeliest first, a posting with no likelihood after those that have one
    (docs/PRD.md 39.10). Between postings of equal likelihood and first sight it is not promised (the database sorts
    ties as its plan does, and the plan changes once the table has statistics), so the two this test wants read are the
    only two that have a likelihood, and it asks nothing of the order of the rest."""
    async with db.session_factory()() as session:
        await _every_kind(session, await _acme(session))
        for ext_id, likelihood in (("a-snippet", 0.3), ("empty-text", 0.8)):
            (await session.scalars(select(JobPosting).where(JobPosting.ext_id == ext_id))).one().fit_prob = likelihood
        await session.flush()
        night = await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": 2})
        assert night == {"filled": 2, "left": 0}
        assert board.asked == ["empty-text", "a-snippet"], "the likeliest first, those with none after"
        assert len(await _the_set(session)) == len(THE_SET) - 2, "the rest are in the set the next night"
        # the pages the filter read for places came out of the night's number
        board.asked.clear()
        places = await actions.read_descriptions(
            session, None, {"owner": OWNER, "details_per_night": 3, "sifted": {"read": 2}})
        assert places == {"filled": 1, "left": 0} and len(board.asked) == 1
        # a limit of nothing reads nothing
        board.asked.clear()
        assert await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": 0}) == {
            "filled": 0, "left": 0} and board.asked == []


async def test_a_posting_a_change_to_the_owners_settings_lets_through_is_read_the_next_time(js, board):
    """Loosening a gate is the re-gate's to do (docs/PRD.md 39.14): it takes the stop off the postings the gate no
    longer stops. All this step needs is what it leaves behind, so this test takes the stop off two of three the way it
    does; the one still stopped is not read."""
    async with db.session_factory()() as session:
        acme = await _acme(session)
        session.add_all([_post(acme.id, n, filtered_by="pay under your amount", judged_by="gate", route=None)
                         for n in ("let-through", "also-let-through", "still-stopped")])
        await session.flush()
        assert await _the_set(session) == set(), "a posting a code gate stopped needs no text"
        assert await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": -1}) == {
            "filled": 0, "left": 0}
        for posting in (await session.scalars(select(JobPosting).where(JobPosting.ext_id.like("%let-through")))).all():
            posting.filtered_by, posting.judged_by, posting.route = None, None, "titles"
        await session.flush()
        assert await _the_set(session) == {"let-through", "also-let-through"}
        night = await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": -1})
        assert night["filled"] == 2
        texts = await _texts(session)
    assert texts["let-through"] and texts["also-let-through"] and texts["still-stopped"] is None


# --- the night ----------------------------------------------------------------------------------------------------


async def test_the_night_reads_the_text_even_when_nothing_new_passed_the_filter_that_night(js, board):
    """The step used to run only when tonight's filter let a new posting through, so a posting an earlier night passed
    (or one a change let through) waited for the next new one."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"], "search_plan": ["programs"]})
        acme = await _acme(session)
        acme.board_url, acme.check_directly = "https://boards.example.test/acme", True
        await where.seed_employer_sites(session, OWNER)
        session.add(_post(acme.id, "1"))
        await session.commit()
    board.listed = [{"ext_id": "1", "title": "1", "location": "Remote", "work_mode": "remote", "comp": None,
                     "comp_min": None, "comp_max": None, "posted": None, "url": "https://jobs.example.test/1",
                     "apply_url": None, "text": None, "department": None}]
    outcome, _summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "done" and counts["new"] == 0 and counts["candidates"] == 0, "nothing new passed tonight"
    assert counts["details"] == 1 and board.asked == ["1"], "its text was read all the same"
    async with db.session_factory()() as session:
        assert (await _texts(session))["1"] == "The whole posting for 1."


async def test_the_pool_reads_the_text_roles_lack_by_code_at_any_hour_newest_first_and_only_for_roles_in_play(
        js, board, monkeypatch):
    """A role with no real posting text (none, or a snippet under cleansed.MIN_TEXT) is not ranked; the pool reads its
    description from its job system, without waiting for the night, so it can be."""
    from app.jobsearch import stages
    from app.jobsearch.pool import core as pool

    monkeypatch.setattr(polite, "make", _fetch)
    monkeypatch.setattr(pool, "DETAILS_PER_PULL", 2)
    async with db.session_factory()() as session:
        acme = await _acme(session)
        rows = {}
        for name, days_ago, status in (("old", 9, stages.CLEANSED_DATA), ("mid", 5, stages.CLEANSED_DATA),
                                       ("new", 1, stages.CLEANSED_DATA), ("ended", 0, stages.REJECTED)):
            role = Opportunity(owner=OWNER, slug=name, company="Acme", title=name, match_key=f"acme|{name}",
                               status=status)
            session.add(role)
            await session.flush()
            posting = _post(acme.id, name, opportunity_id=role.id, text="A snippet.", route=None)
            posting.first_seen = NOW - timedelta(days=days_ago)
            session.add(posting)
            rows[name] = role
        session.add(_post(acme.id, "no-role", route=None))
        await session.commit()
    assert await pool.queue_details(OWNER) == 2
    assert board.asked == ["new", "mid"], "the newest first, two a pull, none for an ended role or no role"
    assert await pool.queue_details(OWNER) == 1
    assert board.asked == ["new", "mid", "old"]
    async with db.session_factory()() as session:
        texts = await _texts(session)
    assert texts["old"] == "The whole posting for old." and texts["ended"] == "A snippet."


async def test_the_pages_a_read_gave_are_kept_a_batch_at_a_time_and_a_second_writer_gets_in_between(
        js, board, monkeypatch):
    """`read_role_details` kept all of its pages in one writer's turn. Old behaviour: one turn for the whole write. New:
    `DETAILS_WRITE_BATCH` pages to a turn, the turn let go between batches (a second writer is let in: here it commits
    a row, which raises `db.NestedWriter` in the tests if the batch's session still held the turn), and the result is
    the one the one-transaction run gives (the same count read and filled, the same texts, every posting marked
    tried)."""
    from app.jobsearch import stages
    from app.jobsearch.pool import core as pool

    monkeypatch.setattr(polite, "make", _fetch)
    names = [f"p{i}" for i in range(5)]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        for name in names:
            role = Opportunity(owner=OWNER, slug=name, company="Acme", title=name, match_key=f"acme|{name}",
                               status=stages.CLEANSED_DATA)
            session.add(role)
            await session.flush()
            session.add(_post(acme.id, name, opportunity_id=role.id, text="A snippet.", route=None))
        await session.commit()
    monkeypatch.setattr(postings, "DETAILS_WRITE_BATCH", 2)
    let_in = []
    real_release = db.release

    async def release_and_let_one_in(session):
        await real_release(session)
        async with db.session_factory()() as other:
            other.add(Opportunity(owner=OWNER, slug=f"in-{len(let_in)}", company="Other", title="in",
                                  match_key=f"other|in{len(let_in)}", status=stages.CLEANSED_DATA))
            await other.commit()
        let_in.append(1)

    monkeypatch.setattr(db, "release", release_and_let_one_in)
    read = await pool.read_details(OWNER, 10)
    assert read == postings.DetailsRead(filled=5, settled=5)
    assert len(let_in) == 2, "five pages in batches of two: a turn let go before the second and the third batch"
    async with db.session_factory()() as session:
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert {n: rows[n].text for n in names} == {n: f"The whole posting for {n}." for n in names}
    assert all(rows[n].detail_at is not None for n in names)


async def test_in_peak_hours_no_model_session_starts_but_the_code_reads_the_descriptions_roles_lack(
        js, board, monkeypatch):
    """Peak hours (`Settings.avoid_peak`) hold the model work; reading a description from a job system by code is not
    model work, so it goes on (the owner, 2026-10-22)."""
    from app.jobsearch import stages
    from app.jobsearch.loops import schedule
    from app.jobsearch.work import hours as night
    from app.jobsearch.work import session_kinds

    monkeypatch.setattr(polite, "make", _fetch)
    monkeypatch.setattr(night, "in_peak", lambda s, now=None: True)
    async with db.session_factory()() as session:
        acme = await _acme(session)
        role = Opportunity(owner=OWNER, slug="peak", company="Acme", title="peak", match_key="acme|peak",
                           status=stages.CLEANSED_DATA)
        session.add(role)
        await session.flush()
        session.add(_post(acme.id, "peak", opportunity_id=role.id, text="A snippet.", route=None))
        await session.flush()
        assert await session_kinds.next_kind(session, OWNER) is None, "no model session in peak hours"
        assert board.asked == [], "an ask for work reads nothing: it answers at once"
        await session.commit()
    assert await schedule.pipeline_pass(OWNER) is True
    assert board.asked == ["peak"], "the code read of the description went on, in the pipeline's own turn"


def _job_page(title: str) -> str:
    """A page that embeds one JobPosting, with this title."""
    ld = json.dumps({"@type": "JobPosting", "title": title, "description": f"<p>{'The whole posting. ' * 30}</p>"})
    return f'<html><body><script type="application/ld+json">{ld}</script></body></html>'


def _site(answer):
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(answer)), per_host_seconds=0)


async def test_a_posting_on_an_employers_own_site_is_read_from_its_own_page(js, board, monkeypatch):
    from app.jobsearch import stages
    from app.jobsearch.pool import core as pool

    seen = []

    def answer(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=_job_page("own"), headers={"content-type": "text/html"})

    async with db.session_factory()() as session:
        acme = await _acme(session)
        role = Opportunity(owner=OWNER, slug="own", company="Acme", title="own", match_key="acme|own",
                           status=stages.FOLLOWUP_NEEDED)
        session.add(role)
        await session.flush()
        session.add(_post(acme.id, "own", ats="own_site", opportunity_id=role.id, route=None))
        await session.commit()
    monkeypatch.setattr(polite, "make", lambda: _site(answer))
    assert await pool.queue_details(OWNER) == 1
    async with db.session_factory()() as session:
        assert "The whole posting." in (await _texts(session))["own"]
    assert "/own" in seen


async def test_a_page_that_is_another_jobs_posting_gives_an_own_site_posting_no_text(js, board, monkeypatch):
    """A text kept on a posting goes to its role (`posting_text._plan`), so the page an employer's own
    site gives for a posting must name that posting, as the page read for a role does (docs/PRD.md 39.29)."""
    from app.jobsearch import stages
    from app.jobsearch.pool import core as pool

    def answer(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=_job_page("Warehouse Associate"), headers={"content-type": "text/html"})

    async with db.session_factory()() as session:
        acme = await _acme(session)
        role = Opportunity(owner=OWNER, slug="own", company="Acme", title="own", match_key="acme|own",
                           status=stages.FOLLOWUP_NEEDED)
        session.add(role)
        await session.flush()
        session.add(_post(acme.id, "own", ats="own_site", opportunity_id=role.id, route=None))
        await session.commit()
    monkeypatch.setattr(polite, "make", lambda: _site(answer))
    assert await pool.queue_details(OWNER) == 0
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting))).one()
        assert not posting.text, "another job's page is no text of this posting"
        assert posting.detail_at is not None, "the page was read, so the posting waits its retry"


async def test_a_posting_no_read_was_begun_for_is_not_marked_as_tried(js, board):
    """A posting with no reader for its system, or the time up before its turn, waits for no retry."""
    async with db.session_factory()() as session:
        acme = await _acme(session)
        no_reader = _post(acme.id, "no-reader", ats="unread-system")
        late = _post(acme.id, "late")
        session.add_all([no_reader, late])
        await session.flush()
        fetch = _fetch()
        fetch.deadline = 0.0  # the time ran out before any page was begun
        assert await postings.fill_details(session, fetch, [no_reader, late]) == 0
        assert no_reader.detail_at is None and late.detail_at is None
        assert await postings.fill_details(session, _fetch(), [late]) == 1 and late.detail_at is not None


async def test_the_pipelines_own_turn_reads_the_text_of_roles_that_have_none_and_moves_them_on(js, board, monkeypatch):
    """Roles the pipeline makes from postings read ahead of their text (a night cut short) get it within the turn."""
    from app.jobsearch import stages
    from app.jobsearch.loops import schedule

    monkeypatch.setattr(polite, "make", _fetch)
    async with db.session_factory()() as session:
        acme = await _acme(session)
        role = Opportunity(owner=OWNER, slug="quiet", company="Acme", title="quiet", match_key="acme|quiet",
                           status=stages.FOLLOWUP_NEEDED)
        session.add(role)
        await session.flush()
        session.add(_post(acme.id, "quiet", opportunity_id=role.id, route=None))
        await session.commit()
    assert await schedule.pipeline_pass(OWNER) is True
    assert board.asked == ["quiet"]
    async with db.session_factory()() as session:
        assert (await _texts(session))["quiet"] == "The whole posting for quiet."


async def test_an_own_site_posting_whose_sites_live_recipe_has_a_detail_request_is_read_by_the_recipe(
        js, board, monkeypatch):
    from app.jobsearch import stages
    from app.jobsearch.pool import core as pool
    from app.sites.models import Site, SiteRecipe

    seen = []

    def answer(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": {"body": "<p>%s</p>" % ("The whole posting. " * 30)}})

    recipe = {"kind": "request", "method": "GET", "url": "https://jobs.example.test/api/search", "items": "jobs",
              "fields": {"title": "title", "url": "path", "ext_id": "id"}, "url_prefix": "https://jobs.example.test",
              "detail": {"url": "https://jobs.example.test/api/job/{ext_id}", "description": "data.body"}}
    monkeypatch.setattr(polite, "make", lambda: _site(answer))
    async with db.session_factory()() as session:
        acme = await _acme(session)
        site = Site(owner=OWNER, url="https://jobs.example.test", kind="employer", employer_id=acme.id, created_at=NOW)
        session.add(site)
        await session.flush()
        session.add(SiteRecipe(owner=OWNER, site_id=site.id, version=1, recipe=recipe, state="live", created_at=NOW))
        role = Opportunity(owner=OWNER, slug="own-site", company="Acme", title="own-site", match_key="acme|own-site",
                           status=stages.FOLLOWUP_NEEDED)
        session.add(role)
        await session.flush()
        session.add(_post(acme.id, "77", ats="own_site", opportunity_id=role.id, route=None))
        await session.commit()
    assert await pool.queue_details(OWNER) == 1
    async with db.session_factory()() as session:
        assert "The whole posting." in (await _texts(session))["77"]
    assert seen == ["https://jobs.example.test/api/job/77"], "the recipe's request, not the posting's page"


async def test_an_ask_for_work_never_reads_a_page_or_turns_the_pipeline(js, board, monkeypatch):
    """The pipeline's turn and its description reads (up to 40 + 90 seconds) ran inside
    `GET /agent/work`, whose runners give up after ten seconds ("asking for work failed: timeout" on every lane), and
    held the row locks the other asks queued behind. The ask answers from what is there; the app's own loop turns."""
    from app.jobsearch import stages
    from app.jobsearch.loops import schedule
    from app.jobsearch.work import hours as night
    from app.jobsearch.work import session_kinds

    monkeypatch.setattr(polite, "make", _fetch)
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: True)
    async with db.session_factory()() as session:
        acme = await _acme(session)
        role = Opportunity(owner=OWNER, slug="ask", company="Acme", title="ask", match_key="acme|ask",
                           status=stages.FOLLOWUP_NEEDED)
        session.add(role)
        await session.flush()
        session.add(_post(acme.id, "ask", opportunity_id=role.id, route=None))
        await session.commit()
    async with db.session_factory()() as session:
        await session_kinds.next_kind(session, OWNER)
        await session.commit()
    assert board.asked == [], "no page is read inside the ask"
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, role.id)).status == stages.FOLLOWUP_NEEDED
    assert await schedule.pipeline_pass(OWNER) is True
    assert board.asked == ["ask"]


async def test_one_pipeline_turn_at_a_time(js):
    """A turn still reading descriptions when the next is due: the next does not start beside it."""
    from app.jobsearch.loops import schedule

    async with schedule._pipeline_lock:
        assert await schedule.pipeline_pass(OWNER) is False
    assert await schedule.pipeline_pass(OWNER) is True


async def test_the_owed_set_is_read_before_the_steps_writes_so_a_second_writer_gets_in_and_the_result_is_the_same(
        js, board, monkeypatch):
    """A resumed read's first step made the owed-set read inside its writer's turn (about a second on a large database).
    Old: the step's session held the turn from its first statement to the note of the total. New: the set is read in a
    reader's session, so a second writer gets in at the note (the guard raises `NestedWriter` if the turn is held) and
    the step's result is the one a single transaction gives."""
    async with db.session_factory()() as session:
        acme = await _acme(session)
        session.add_all([_post(acme.id, n) for n in ("one", "two", "three")])
        await session.commit()
    seen: dict = {}
    real = read_progress.set_details_total

    async def a_second_writer_gets_in(session, count):
        async with db.session_factory()() as other:
            await other.execute(update(JobPosting).where(JobPosting.ext_id == "one").values(company="Acme Two"))
            await other.commit()
        seen["total"] = count
        await real(session, count)

    monkeypatch.setattr(read_progress, "set_details_total", a_second_writer_gets_in)
    async with db.session_factory()() as session:
        done = await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": -1})
        await session.commit()
        texts = await _texts(session)
    assert seen["total"] == 3 and done == {"filled": 3, "left": 0}
    assert set(board.asked) == {"one", "two", "three"} and all(texts[n] for n in board.asked)
