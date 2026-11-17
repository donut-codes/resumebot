"""A Sourced role with no text of its posting is never researched (the owner, 2026-10-14). The order of work is in
analyse_role.bpmn: the app
sources the role, then fetches the posting's text in code from its source and from no other link; a role that holds
none waits ("Waiting for the posting's text", unresearched, its asks unanswered) until the owner's browser or the app
brings the text, and is researched then. The analyst is never sent such a role, and never looks for a link. A page
that gives only a fragment, a cut-off text or a sign-in wall is no text at all. Every name, link and sentence is made
up, and no test reaches the network."""


import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import process, stages
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.missions import core as missions
from app.jobsearch.models import DESCRIPTION_BOARD, Employer, JobPosting, Opportunity
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis, prioritize
from app.models.work import TrailEvent
from tests.builders import analysis_setup, findings, put_in_line, submit_findings
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.support import EXAMPLE_SYSTEM, OWNER, POSTING_WORDS, reads_on

pytestmark = pytest.mark.usefixtures("made_up_boards")


OWN = "https://careers.fabrikam.example.test/jobs/4012"


#: A career page that shows the whole posting: past the least a page may give, a line to each paragraph.
PARAGRAPHS = [f"Paragraph {i} of the posting says one thing about the work and nothing else." for i in range(1, 21)]


# The page names the role in its title, which is never part of the text.
PAGE = ("<html><head><title>Programs Lead - Acme Careers</title></head><body>"
        + "".join(f"<p>{p}</p>" for p in PARAGRAPHS) + "</body></html>")


WHOLE = "\n".join(PARAGRAPHS)


def serve(monkeypatch, handle) -> SimpleNamespace:
    """Answer every address of the made-up web with `handle(request)`. `pages` is every address asked (robots.txt
    apart), query and all."""
    seen = SimpleNamespace(pages=[])

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.pages.append(str(request.url))
        return handle(request)

    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0))
    return seen


def web(monkeypatch, body: str) -> SimpleNamespace:
    """Answer every address of the made-up web with this page. `pages` is what was asked (robots.txt apart)."""
    return serve(monkeypatch, lambda request: httpx.Response(200, text=body, headers={"content-type": "text/html"}))


async def _no_text_role(source: str = OWN, **more) -> str:
    """Fabrikam's Found role, Sourced at `source` and holding no text, put in the analyst's line (the app could not
    read the source's page unless a test made the web answer first). `more` sets other fields of the role. Returns its
    id."""
    ids = await analysis_setup()
    await put_in_line(ids["Fabrikam"], description=None, url=source, source_url=source, **more)
    return str(ids["Fabrikam"])


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, uuid.UUID(role_id), populate_existing=True)


async def _step(role_id) -> str | None:
    async with db.session_factory()() as session:
        return await analysis.open_step(session, await session.get(Opportunity, uuid.UUID(role_id)))


async def _task() -> dict:
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER)
        await session.commit()
    return task


async def test_a_sourced_role_with_no_text_waits_for_it_and_is_never_handed_to_the_analyst(js):
    role_id = await _no_text_role()
    role = await _role(role_id)
    assert role.analysis_state == analysis.NO_TEXT and await _step(role_id) is None, "the process, not the prompt"
    task = await _task()
    assert task["task"] is None and "No role is waiting" in task["note"], "nothing to research, nothing to look for"


async def test_a_role_without_text_is_never_analysed_whatever_the_analyst_sends(js):
    role_id = await _no_text_role()
    before = await _role(role_id)
    with pytest.raises(analysis.AnalysisError, match="isn't waiting for research"):
        await submit_findings(findings(role_id))  # the whole research: employer facts, pay, requirements, notes
    role = await _role(role_id)
    assert role.analysis_state == analysis.NO_TEXT and role.analysed_at is None
    assert not role.analysis.get("required") and not role.analysis.get("pay") and not role.analysis.get("notes")
    assert (role.fit, role.fit_breakdown, role.comp, role.status) == (
        before.fit, before.fit_breakdown, before.comp, before.status), "nothing was scored or moved"
    assert role.description is None and role.description_at is None, "and the analyst's reading is no text"
    async with db.session_factory()() as session:
        employer = await session.scalar(select(Employer).where(Employer.owner == OWNER))
    assert employer is None or employer.employees is None, "the employer was not researched either"


async def test_a_role_that_waits_says_so_in_its_history_and_its_row_says_nothing_of_it(js):
    role_id = await _no_text_role()
    async with db.session_factory()() as session:
        lines = list((await session.scalars(select(TrailEvent).where(TrailEvent.event == "waiting_for_text"))).all())
        assert len(lines) == 1 and "waiting for the posting's text" in lines[0].sentence
        row = await role_row.row(session, OWNER, uuid.UUID(role_id))
        # Old: the row said "Waiting for the posting's text · Its first hours add 20" (the made-up role was posted
        # today). New (docs/PRD.md 24.18): waiting is the normal run, so the row says nothing; the history says it, and
        # the hours are the row's key.
        assert row["next"] is None and row["first_hours_bonus"] == 20


async def test_the_sources_page_is_fetched_in_code_and_the_role_is_researched_from_it(js, monkeypatch):
    seen = web(monkeypatch, PAGE)
    role_id = await _no_text_role()
    assert seen.pages == [OWN], "the app read the role's source, and no job board"
    role = await _role(role_id)
    assert role.url == OWN and role.description == WHOLE, "the posting's own words, verbatim"
    assert role.analysis_state == analysis.WAITING and await _step(role_id) == process.ANALYST_STEP
    task = await _task()
    assert task["role"]["id"] == role_id and "task_now" not in task and task["role"]["posting_text_kept"] == WHOLE
    done = await submit_findings(findings(role_id))
    assert done["taken"] is True
    assert (await _role(role_id)).analysis_state == analysis.DONE


async def test_only_the_source_is_read_never_another_link_of_the_role(js, monkeypatch):
    """The role has other links (its job board's and its Apply link); the text comes from its source and no other."""
    seen = web(monkeypatch, PAGE)
    ids = await analysis_setup()
    await put_in_line(ids["Fabrikam"], description=None, url="https://www.boardone.test/jobs/view/3912345678",
                      board_url="https://www.boardone.test/jobs/view/3912345678",
                      apply_url="https://apply.other.example.test/jobs/55", source_url=OWN)
    assert seen.pages == [OWN]
    assert (await _role(str(ids["Fabrikam"]))).description == WHOLE


async def test_a_role_that_is_not_sourced_has_nothing_fetched_whatever_links_it_has(js, monkeypatch):
    seen = web(monkeypatch, PAGE)
    ids = await analysis_setup()
    await put_in_line(ids["Fabrikam"], description=None, url=OWN, apply_url=OWN, source_url=None, source_kind=None,
                      sourced_at=None)
    assert seen.pages == [], "its links are no source until the app says so (postings/sourced.py)"
    role = await _role(str(ids["Fabrikam"]))
    assert role.description is None and role.analysis_state is None, "left in Found, not researched"


FRAGMENT = "<html><body><p>Programs Lead. We are hiring. Sign in to see more.</p></body></html>"


CUT = ("<html><body>" + "".join(f"<p>{p}</p>" for p in PARAGRAPHS[:12])
       + "<p>Paragraph 13 of the posting says...</p></body></html>")


WALL = ("<html><body>" + "".join(f"<p>{p}</p>" for p in PARAGRAPHS[:10])
        + "<p>Sign in to view the whole job and apply.</p></body></html>")


@pytest.mark.parametrize("page", [FRAGMENT, CUT, WALL], ids=["a fragment", "cut off at see more", "a sign-in wall"])
async def test_an_employer_page_that_gives_only_a_fragment_is_no_text_and_the_role_waits(js, monkeypatch, page):
    web(monkeypatch, page)
    role_id = await _no_text_role()
    role = await _role(role_id)
    assert role.description is None and role.analysis_state == analysis.NO_TEXT
    assert role.source_url == OWN, "the source is kept: only its text is missing"
    async with db.session_factory()() as session:
        assert (await session.scalar(select(JobPosting.id))) is None, "no posting was kept either"


async def test_a_role_that_waits_is_researched_when_the_owners_browser_brings_the_text(js):
    role_id = await _no_text_role()
    assert (await _role(role_id)).analysis_state == analysis.NO_TEXT
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER, [uuid.UUID(role_id)]) == 0, "no text yet"
        role = await session.get(Opportunity, uuid.UUID(role_id))
        assert await posting_text.take(
            session, role, posting_text.Capture(POSTING_WORDS)) == posting_text.FETCHED
        assert await analysis.resume_waiting(session, OWNER, [role.id]) == 1
        await session.commit()
    role = await _role(role_id)
    assert role.analysis_state == analysis.WAITING and await _step(role_id) == process.ANALYST_STEP
    task = await _task()
    assert task["role"]["id"] == role_id and task["role"]["posting_text_kept"] == POSTING_WORDS
    assert "read_next" in task and "task_now" not in task, "researched now: the whole task"
    assert (await submit_findings(findings(role_id)))["taken"] is True
    assert (await _role(role_id)).analysis_state == analysis.DONE


async def test_the_schedules_sweep_puts_a_role_back_in_line_when_the_apps_own_read_brought_the_text(js):
    role_id = await _no_text_role()
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == 0
        role = await session.get(Opportunity, uuid.UUID(role_id))
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id="1", company="Fabrikam",
            title="Programs Lead", match_key="fabrikam|programs lead", first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC), links=[], opportunity_id=role.id, text=POSTING_WORDS, url=OWN,
            detail_at=datetime.now(UTC)))
        await session.flush()
        assert await analysis.resume_waiting(session, OWNER) == 1
        await session.commit()
    assert (await _role(role_id)).analysis_state == analysis.WAITING


async def _posting_holding(role_id: str, *, url: str, text: str = POSTING_WORDS) -> None:
    """A posting of the role, read whole by its job system, at `url`."""
    async with db.session_factory()() as session:
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=url, company="Fabrikam",
            title="Programs Lead", match_key="fabrikam|programs lead", first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC), links=[], opportunity_id=uuid.UUID(role_id), text=text, url=url,
            detail_at=datetime.now(UTC)))
        await session.commit()


async def _waiting_lines() -> int:
    async with db.session_factory()() as session:
        return len((await session.scalars(select(TrailEvent).where(TrailEvent.event == "waiting_for_text"))).all())


async def test_a_role_whose_whole_text_is_a_posting_at_another_address_than_its_source_is_not_restarted(
        js, monkeypatch):
    """The text a posting of the role holds, at an address that is not the role's source, is no text the take (the
    step "Fetch the posting's text") would give it: the take reads the source, which says no. The schedule's sweep asks
    the take's own question, so it starts nothing, reads nothing and leaves no history line, however often it runs
    (PRD 39.30; One role was started every 5 minutes, 283 times a day)."""
    seen = serve(monkeypatch, lambda request: httpx.Response(503))
    role_id = await _no_text_role()
    await _posting_holding(role_id, url="https://careers.fabrikam.example.test/jobs/4012-other")
    asked, lines = list(seen.pages), await _waiting_lines()
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == 0
        assert await analysis.resume_waiting(session, OWNER) == 0
        await session.commit()
    assert (await _role(role_id)).analysis_state == analysis.NO_TEXT
    assert await _waiting_lines() == lines, "no new line in its history"
    assert seen.pages == asked, "and the sweep read nothing"


async def test_a_role_that_holds_a_snippet_and_whose_posting_holds_the_whole_text_is_put_back_in_line(js):
    """The take gives a snippet way to the whole text of a posting of the role, whatever its address."""
    role_id = await _no_text_role()
    await _posting_holding(role_id, url="https://careers.fabrikam.example.test/jobs/4012-other")
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, uuid.UUID(role_id))
        role.description, role.description_at = "Lead the programs team.", datetime.now(UTC)
        await session.commit()
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == 1
        await session.commit()
    assert (await _role(role_id)).analysis_state == analysis.WAITING


async def test_a_role_that_holds_its_own_whole_text_is_put_back_in_line(js):
    role_id = await _no_text_role()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, uuid.UUID(role_id))
        role.description, role.description_at = POSTING_WORDS, datetime.now(UTC)
        await session.commit()
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == 1
        await session.commit()
    assert (await _role(role_id)).analysis_state == analysis.WAITING


async def _waiting_roles_that_hold_their_text(monkeypatch) -> list[uuid.UUID]:
    """Fabrikam, Initech and Hooli, each Sourced and waiting for a text its source refused, and each given its text
    since (the app's own read brought it), in the order the sweep takes them (id order)."""
    serve(monkeypatch, lambda request: httpx.Response(503))
    ids = await analysis_setup(text=None)
    for company in ("Fabrikam", "Initech", "Hooli"):
        await put_in_line(ids[company], description=None)
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all():
            assert role.analysis_state == analysis.NO_TEXT
            role.description, role.description_at = POSTING_WORDS, datetime.now(UTC)
        await session.commit()
    return sorted(ids.values())


async def test_the_sweep_puts_each_role_back_in_line_in_a_transaction_of_its_own(js, monkeypatch):
    """No role row is held for the rest of the sweep."""
    await _waiting_roles_that_hold_their_text(monkeypatch)
    committed = []
    real = analysis.start

    async def start(session, owner, role, **more):
        async with db.read_session_factory()() as other:  # a reader of its own sees only what was committed
            committed.append(len((await other.scalars(
                select(Opportunity.id).where(Opportunity.analysis_state == analysis.WAITING))).all()))
        return await real(session, owner, role, **more)

    monkeypatch.setattr(analysis, "start", start)
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == 3
        await session.commit()
    assert committed == [0, 1, 2], "each role was saved before the next was started"


async def test_the_sweep_locks_a_role_only_to_put_it_back_in_line_and_one_at_a_time(js, monkeypatch):
    ids = await _waiting_roles_that_hold_their_text(monkeypatch)
    with reads_on("js_opportunities") as reads:
        async with db.session_factory()() as session:
            assert await analysis.resume_waiting(session, OWNER) == 3
            await session.commit()
    taken = [found for statement, found in reads if found and "ORDER BY js_opportunities.id" in statement]
    assert taken == [[i] for i in ids], "one statement, one role, in id order"


async def test_the_sweep_locks_no_role_while_it_looks_at_the_waiting_ones(js, monkeypatch):
    serve(monkeypatch, lambda request: httpx.Response(503))
    await _no_text_role()
    with reads_on("js_opportunities") as reads:
        async with db.session_factory()() as session:
            assert await analysis.resume_waiting(session, OWNER) == 0
            await session.rollback()
    assert reads and [found for _statement, found in reads if found] == [], (
        "the roles were looked at, and none was asked for by its id (the read the lock makes)")


async def test_a_role_the_analyst_took_meanwhile_does_not_stop_the_sweep(js, monkeypatch):
    ids = await _waiting_roles_that_hold_their_text(monkeypatch)
    real = analysis.start

    async def start(session, owner, role, **more):
        if role.id == ids[0]:
            raise analysis.AnalysisError("this role is with the analyst already")
        return await real(session, owner, role, **more)

    monkeypatch.setattr(analysis, "start", start)
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == 2, "the other two were put back in line"
        await session.commit()
    states = {i: (await _role(str(i))).analysis_state for i in ids}
    assert states == {ids[0]: analysis.NO_TEXT, ids[1]: analysis.WAITING, ids[2]: analysis.WAITING}


@pytest.mark.parametrize(("blanked", "started"), [(False, 1), (True, 0)])
async def test_a_role_is_judged_again_on_its_postings_as_they_are_once_it_is_locked(
        js, monkeypatch, blanked, started):
    """The sweep judges every waiting role with no lock, and each one that qualifies is judged again once it is locked,
    on the role and on its postings as they are then: a posting another transaction blanked in between is no text. (The
    sweep's own reads keep no posting between the two, so none is stale.)"""
    serve(monkeypatch, lambda request: httpx.Response(503))
    role_id = await _no_text_role()
    await _posting_holding(role_id, url=OWN)  # at the role's source: the source's own text
    real = db.release_if_free

    async def changed_meanwhile(session):
        saved = await real(session)
        if blanked:
            async with db.session_factory()() as other:
                for posting in (await other.scalars(select(JobPosting))).all():
                    posting.text, posting.detail_at = None, None
                await other.commit()
        return saved

    monkeypatch.setattr(db, "release_if_free", changed_meanwhile)
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == started
        await session.commit()
    assert (await _role(role_id)).analysis_state == (analysis.WAITING if started else analysis.NO_TEXT)


async def _unsourced_waiting_role() -> str:
    """Fabrikam's Applied role that no step has Sourced and that holds no text, marked as waiting for it as
    `built_out.advance` marks the roles the owner applied to. Returns its id."""
    ids = await analysis_setup(text=None)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.status, role.description, role.description_at = stages.APPLIED, None, None
        role.source_url, role.source_kind, role.sourced_at = None, None, None
        await analysis.wait_for_text(session, role)
        await session.commit()
    return str(ids["Fabrikam"])


async def test_a_role_not_sourced_whose_own_job_systems_posting_holds_the_text_is_put_back_in_line(js, monkeypatch):
    """The step before the fetch Sources the role to that posting's address (the source the app already holds, no
    network) and the take then gives it the posting's text, so the sweep asks the take's question for the role as it
    will be then. Found by review 2026-11-06: the sweep asked it of the role as it is, and a role marked waiting before
    it was Sourced (an Applied one: `built_out.advance`) was never started, though the start makes progress for it.
    PRD 39.30."""
    seen = serve(monkeypatch, lambda request: httpx.Response(503))
    role_id = await _unsourced_waiting_role()
    posting = "https://careers.fabrikam.example.test/jobs/4012-other"
    await _posting_holding(role_id, url=posting)
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == 1
        await session.commit()
    role = await _role(role_id)
    assert role.analysis_state == analysis.WAITING
    assert (role.source_url, role.source_kind) == (posting, "employer"), "Sourced to the posting that holds the text"
    assert role.description == POSTING_WORDS, "and given its text"
    assert seen.pages == [], "nothing was read"


async def test_a_role_not_sourced_that_no_step_can_source_to_the_posting_holding_the_text_is_not_started(
        js, monkeypatch):
    """The take gives a role that is not Sourced no text, and the posting's address is a careers page, not one posting,
    so no step keeps it as the role's source: a start would leave the role where it was, for the next sweep to start
    again. The text the role's postings hold (`held`) is not the question."""
    serve(monkeypatch, lambda request: httpx.Response(503))
    role_id = await _unsourced_waiting_role()
    await _posting_holding(role_id, url="https://careers.fabrikam.example.test/careers")
    lines = await _waiting_lines()
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == 0
        await session.commit()
    assert (await _role(role_id)).analysis_state == analysis.NO_TEXT
    assert await _waiting_lines() == lines, "no new line in its history"


async def test_the_schedule_pass_puts_a_role_back_in_line_with_its_source_read_outside_any_transaction(
        js, monkeypatch):
    """A role that holds Boardone's copy is put back in line though its employer's text is not there: its source is
    read once more, which the pass can only do with no transaction open if the sweep is not inside a savepoint
    (`db.strict_io` fails the test on a read inside one). PRD 39.30."""
    seen = serve(monkeypatch, lambda request: httpx.Response(503))
    role_id = await _no_text_role()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, uuid.UUID(role_id))
        role.description, role.description_at = POSTING_WORDS, datetime.now(UTC)
        role.description_from = DESCRIPTION_BOARD
        await session.commit()
    seen.pages.clear()
    async with db.session_factory()() as session:
        await missions.schedule_pass(session, OWNER)
        await session.commit()
    assert (await _role(role_id)).analysis_state == analysis.WAITING, "it holds the copy: researched on it"
    assert seen.pages == [OWN], "the source was read, once"


async def test_a_listings_snippet_is_never_the_text_a_role_holds(js):
    """A posting the browser read from a results page and never opened holds only its snippet: a partial text."""
    role_id = await _no_text_role()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, uuid.UUID(role_id))
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="browser", ext_id="snippet", company="Fabrikam",
            title="Programs Lead", match_key="fabrikam|programs lead", first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC), links=[], opportunity_id=role.id, text="Lead the programs team.",
            detail_at=None))
        await session.commit()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, uuid.UUID(role_id))
        assert (await posting_text.held(session, role)).text == "", "a snippet is not the posting"
        assert await posting_text.take(session, role, posting_text.FromPostings()) is None
        assert await posting_text.take(session, role, posting_text.Capture(POSTING_WORDS)) == posting_text.FETCHED
        assert (await session.scalar(select(JobPosting.text))) == POSTING_WORDS, "the capture replaces the snippet"


async def test_a_run_begun_before_the_order_of_work_changed_is_begun_again_when_a_session_takes_the_role(js):
    """A role in line at the research step with no text (a deploy found it there) is cancelled and started again on the
    process as it is: the app fetches the text first, and a role that still has none waits for it."""
    ids = await analysis_setup()
    await put_in_line(ids["Fabrikam"])  # it holds its text: the research step
    assert await _step(str(ids["Fabrikam"])) == process.ANALYST_STEP
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["Fabrikam"])).description = None  # and now it holds none
        await session.commit()
    task = await _task()
    assert task["task"] is None, "the analyst is not handed a role without text"
    role = await _role(str(ids["Fabrikam"]))
    assert role.analysis_state == analysis.NO_TEXT and await _step(str(ids["Fabrikam"])) is None


def test_an_empty_list_of_required_items_is_no_classes_not_a_role_classed_with_nothing_unproven():
    classes = prioritize.classes_of
    assert classes(SimpleNamespace(analysis={"required": []})) is None
    assert classes(SimpleNamespace(analysis={})) is None and classes(SimpleNamespace(analysis=None)) is None
    assert classes(SimpleNamespace(analysis={"required": [{"class": "P"}, {"class": "U"}]})) == ["P", "U"]


def test_the_analyst_is_told_what_each_step_is_for():
    from app.harness import instructions as harness_instructions

    research = harness_instructions.read("instructions", "jobsearch.analyst")
    assert "still send `market_value`" in research and "`spread`" in research
    assert "you never research a role from a summary" in research and "never fetch a page for the text" in research
