"""The owner adds a job by pasting its link (docs/PRD.md 24.10): a new link makes a posting in the log, as the scout's
finds are, that waits in Added links and goes the steps any posting goes (the code gates, then it becomes a role);
the add answers at once with database work only (no page is read, no agent waited on: PRD 24.10, 2026-11-16), and
the owner's browser reader names the posting later; a job the
owner already has (by the cleaned address or by a job id, in a role or in the log's postings) answers with where it
stands and makes nothing; what is not a public web address is refused. Roles an added link made before it became a
posting are still researched and named by the analyst (the last section). Every name, number and link is made up."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from process_engine.models import ProcInstance
from sqlalchemy import func, not_, select

from app import db
from app.jobsearch import ats, judged, stages
from app.jobsearch.browser import add_link
from app.jobsearch.browser import reader as browser_reader
from app.jobsearch.fetch import polite
from app.jobsearch.models import ADDED_LINK, Employer, JobPosting, Opportunity
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.postings import index as postings_index
from app.jobsearch.postings import same_role
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis
from app.jobsearch.work import titles
from app.models.work import TrailEvent
from app.sites.models import Site
from tests.builders import PARAGRAPHS, WHOLE, added_link_role, analysis_setup, findings, serve_page, submit_findings
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import OWNER, POSTING_WORDS, answered, waiting_count

pytestmark = pytest.mark.usefixtures("example_world")

#: A pay under the made-up floor of $95,000: a gate run again on it stops the role.
FAILS_THE_FLOOR = {"band": [60000, 70000]}
ADD = "/api/v1/roles"
#: A job on a job board, as a browser's address bar shows it, with the parts that say how a visitor arrived.
EXBOARD = "https://www.board.test/jobs/view/3912345678/?trackingId=made-up&refId=made-up&utm_source=share#top"
EXBOARD_CLEAN = "https://board.test/jobs/view/3912345678"
#: A job on an employer's own site: one posting, so the owner's link is the job's source (`postings/sourced.py`).
OWN_POSTING = "https://jobs.example.test/jobs/7001"
#: Where the browser reader finds the employer's posting of a job a job board's link was pasted for.
FOUND_POSTING = "https://careers.umbrella.example.test/jobs/4012"


def job_page(title: str = "Director of Programs", company: str = "Umbrella Works", pay: tuple | None = None,
             posted: str | None = None) -> str:
    """An employer's page that embeds the job as schema.org says to (a `JobPosting`): the title, the employer, the whole
    description, and the pay when given."""
    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": title,
               "hiringOrganization": {"@type": "Organization", "name": company},
               "description": "".join(f"<p>{p}</p>" for p in PARAGRAPHS), "jobLocationType": "TELECOMMUTE"}
    if posted:
        posting["datePosted"] = posted
    if pay:
        posting["baseSalary"] = {"@type": "MonetaryAmount", "currency": "USD", "value": {
            "@type": "QuantitativeValue", "minValue": pay[0], "maxValue": pay[1], "unitText": "YEAR"}}
    return f'<html><head><script type="application/ld+json">{json.dumps(posting)}</script></head><body></body></html>'


async def _named_by_the_browser(posting_id, title: str = "Director of Programs",
                                company: str = "Umbrella Works") -> None:
    """What the browser reader does when it has read an added link's page: names the posting and gates it again."""
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, uuid.UUID(posting_id))
        assert await browser_reader._name_added(
            session, OWNER, posting, {"title": title, "company": company}, WHOLE, datetime.now(UTC)) is True
        await session.commit()


async def _posting(posting_id) -> JobPosting:
    async with db.session_factory()() as session:
        return await session.get(JobPosting, uuid.UUID(str(posting_id)), populate_existing=True)


async def _board(client, key: str) -> list[dict]:
    column = next(c for c in (await client.get("/api/v1/board")).json()["columns"] if c["key"] == key)
    return column["cards"]


async def _to_become_roles() -> list[JobPosting]:
    """The postings that would become a role now (a link no page has named is not one: it has no title)."""
    async with db.session_factory()() as session:
        return list((await session.scalars(select(JobPosting).where(
            *role_from_posting.waiting_where(OWNER), not_(titles.unnamed())))).all())


async def _roles() -> list[Opportunity]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all())


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _count(model, *where) -> int:
    async with db.session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _line() -> list:
    """The ids a session would be handed from, in order."""
    async with db.session_factory()() as session:
        return await analysis._eligible(session, OWNER, datetime.now(UTC))


async def _night_starts(*role_ids) -> None:
    """The night's research puts these roles in line, in this order."""
    async with db.session_factory()() as session:
        for role_id in role_ids:
            await analysis.start(session, OWNER, await session.get(Opportunity, role_id))
        await session.commit()


async def _lines(role_id, event: str | None = None) -> list[TrailEvent]:
    async with db.session_factory()() as session:
        query = select(TrailEvent).where(TrailEvent.link_id == role_id).order_by(TrailEvent.at, TrailEvent.id)
        if event:
            query = query.where(TrailEvent.event == event)
        return list((await session.scalars(query)).all())


# --- a new link -------------------------------------------------------------------------------------------------


async def test_a_new_link_is_added_at_once_with_no_page_read_and_waits_in_added_links_for_the_browser_to_name_it(
        client, knocks, monkeypatch, caplog):
    """Old: the add read the page of the link in code and, when it embedded a JobPosting, named the posting from it (a
    wait of up to twelve seconds). New (PRD 24.10): no page is fetched and the posting is in Added links, unnamed, as
    soon as the call returns; the browser queue names it later."""
    await analysis_setup()
    seen = serve_page(monkeypatch, job_page())  # the employer's own page embeds the job, and is never asked for
    roles, instances = len(await _roles()), await _count(ProcInstance)

    with caplog.at_level("INFO", logger="app.jobsearch.browser.link_held"):
        answer = await client.post(ADD, json={"url": f"  {OWN_POSTING}  "})
    assert answer.status_code == 200
    body = answer.json()
    assert (body["role"], body["already"], body["stage"], body["stage_label"]) == (None, False, "added_links",
                                                                                  "Added links")
    assert body["said"].startswith("Added, and waiting in the Added links list")
    assert (len(await _roles()), await _count(ProcInstance)) == (roles, instances), "no role, no research"
    assert knocks == [], "nothing is woken: the posting goes the steps of any posting, and Wake up is the owner's"
    assert seen.pages == [], "no page was read at the add"
    lines = [r.getMessage() for r in caplog.records if r.name == "app.jobsearch.browser.link_held"]
    assert lines == [f"added link: posting {body['posting']} on jobs.example.test"], "one line: posting and host"

    # A posting like the scout's finds: the owner's system, the link as its address and its name, the code gates run.
    posting = await _posting(body["posting"])
    assert (posting.ats, posting.url, posting.links, posting.opportunity_id) == (
        ats.OWNER, OWN_POSTING, [OWN_POSTING], None)
    assert (posting.title, posting.company, posting.text) == ("jobs.example.test/jobs/7001", ADDED_LINK, None)
    assert posting.filtered_at is not None and posting.filtered_by is None
    assert (posting.judged_by, posting.target_role) == (None, None), "no target role is chosen by hand"
    assert [c["title"] for c in await _board(client, "added_links")] == ["jobs.example.test/jobs/7001"], (
        "it is in Added links right after the call")

    # The browser names it; then it waits to become a role, as any posting.
    await _named_by_the_browser(body["posting"])
    posting = await _posting(body["posting"])
    assert (posting.title, posting.company) == ("Director of Programs", "Umbrella Works")
    assert posting.match_key == "umbrella works|director of programs"
    assert posting.text and posting.detail_at is not None and posting.route == judged.ROLES == "roles"
    assert await _board(client, "added_links") == [], "a named posting is not held in Added links: it becomes a role"
    async with db.session_factory()() as session:
        assert await waiting_count(session, OWNER) == 1, "it waits to become a role, as any posting"
        assert await role_from_posting.make_roles(session, OWNER) == 1
        await session.commit()
    assert (await _posting(body["posting"])).opportunity_id is not None


async def test_adding_a_link_fetches_nothing_and_never_reads_a_page_even_one_that_would_be_refused(client, monkeypatch):
    """The add makes no fetcher at all: a fetcher that is asked for fails the test."""
    await analysis_setup()

    def never():
        raise AssertionError("the add made a fetcher")

    monkeypatch.setattr(polite, "make", never)
    body = (await client.post(ADD, json={"url": OWN_POSTING})).json()
    assert body["stage"] == "added_links" and (await _posting(body["posting"])).company == ADDED_LINK


async def test_the_facts_of_a_page_that_states_when_the_job_was_posted_give_the_posting_a_date(client):
    """Found when an employer's page carried a `datePosted` and the add failed: the page's facts give the day as text,
    and the posting's column is a date. (Old: the add read the page; new: the same facts come from the browser reader's
    naming, `add_link.name_posting`, which this calls.)"""
    await analysis_setup()
    body = (await client.post(ADD, json={"url": OWN_POSTING})).json()
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, uuid.UUID(body["posting"]))
        assert add_link.name_posting(posting, {
            "title": "Director of Programs", "company": "Umbrella Works", "posted": "2026-10-09",
            "comp_min": 100000, "comp_max": 120000}, datetime.now(UTC)) is True
        await session.commit()
    posting = await _posting(body["posting"])
    assert str(posting.posted) == "2026-10-09" and (posting.comp_min, posting.comp_max) == (100000, 120000)


async def test_a_link_the_app_cannot_read_waits_in_added_links_named_by_its_link_until_the_browser_reads_it(
        client, monkeypatch):
    """A page that states no job (no JobPosting in it) names nothing: the posting is shown by its link, no title check
    is handed it (a link is no title), and the browser reader's plan lists it. The browser's `title` and `company` name
    it, its gates run on the name, and it is a posting any title check may be handed."""
    await analysis_setup()
    seen = serve_page(monkeypatch)  # a page of text and no JobPosting
    body = (await client.post(ADD, json={"url": OWN_POSTING})).json()
    assert (body["stage"], body["said"][:21]) == ("added_links", "Added, and waiting in")
    assert seen.pages == [], "the page is not read at the add"

    posting = await _posting(body["posting"])
    assert (posting.title, posting.company, posting.text) == ("jobs.example.test/jobs/7001", ADDED_LINK, None)
    assert posting.filtered_at is not None and posting.route == "roles", "the gates ran on what the link gave"
    assert [(c["title"], c["company"]) for c in await _board(client, "added_links")] == [
        ("jobs.example.test/jobs/7001", ADDED_LINK)]
    assert await _to_become_roles() == [], "a link is no title to make a role of"
    async with db.session_factory()() as session:
        planned = await browser_reader.to_read_added(session, OWNER, -1)
    assert [{k: v for k, v in p.items() if k != "order_at"} for p in planned] == [
        {"role_id": body["posting"], "site": "jobs.example.test", "link": OWN_POSTING, "title": "", "company": "",
         "added_by_link": True}]

    async with db.session_factory()() as session:  # the session read the page and sends what it shows
        posting = await session.get(JobPosting, uuid.UUID(body["posting"]))
        assert await browser_reader._name_added(
            session, OWNER, posting, {"title": "Director of Programs", "company": "Umbrella Works"}, WHOLE,
            datetime.now(UTC)) is True
        await session.commit()
    posting = await _posting(body["posting"])
    assert (posting.title, posting.company, posting.text) == ("Director of Programs", "Umbrella Works", WHOLE)
    assert posting.filtered_at is not None and posting.route == "roles"
    assert await _board(client, "added_links") == [], "named: it no longer waits in Added links"
    async with db.session_factory()() as session:
        assert await waiting_count(session, OWNER) == 1, "it waits to become a role"
    async with db.session_factory()() as session:
        assert await browser_reader.to_read_added(session, OWNER, -1) == [], "named: it is no longer listed"


async def test_the_browser_leaves_a_posting_unnamed_when_it_sends_no_name(client, monkeypatch):
    await analysis_setup()
    serve_page(monkeypatch)
    body = (await client.post(ADD, json={"url": OWN_POSTING})).json()
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, uuid.UUID(body["posting"]))
        for half in ({"title": "Director of Programs"}, {"company": "Umbrella Works"}, {}):
            assert await browser_reader._name_added(session, OWNER, posting, half, WHOLE, datetime.now(UTC)) is False
    posting = await _posting(body["posting"])
    assert (posting.company, posting.text) == (ADDED_LINK, None)


async def test_a_job_boards_link_is_never_read_by_the_app_and_waits_for_the_browser(client, knocks, monkeypatch):
    """The app never fetches a job board's page: the posting is the link, cleaned, waiting in Added links."""
    await analysis_setup()
    seen = serve_page(monkeypatch, job_page())
    body = (await client.post(ADD, json={"url": f"  {EXBOARD}  "})).json()
    assert (body["role"] is None, body["already"], body["stage"]) == (True, False, "added_links")
    assert seen.pages == [] and knocks == []

    posting = await _posting(body["posting"])
    assert (posting.url, posting.links, posting.company) == (EXBOARD_CLEAN, [EXBOARD_CLEAN], ADDED_LINK)
    assert posting.title == "board.test/jobs/view/3912345678" and posting.text is None
    assert await _to_become_roles() == []
    async with db.session_factory()() as session:
        assert [r["link"] for r in await browser_reader.to_read_added(session, OWNER, -1)] == [EXBOARD_CLEAN]


async def test_a_gate_that_stops_the_added_posting_says_so_and_it_is_gated_out_with_its_reason(client):
    """Old: a pay the page stated, under the floor, stopped the posting at the add. New: the add knows only the link, so
    the gate that stops it here is one a link can meet (a site the owner avoids); the pay gate runs when the browser has
    named the posting."""
    await analysis_setup()
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://jobs.example.test/", kind="job_board", list_name="avoid",
                         created_at=datetime.now(UTC)))
        await session.commit()
    body = (await client.post(ADD, json={"url": OWN_POSTING})).json()
    assert (body["already"], body["stage"]) == (False, "gated_out")
    assert body["said"].startswith("That link was added, in Gated out: ")

    posting = await _posting(body["posting"])
    assert posting.filtered_by and body["said"].endswith(posting.filtered_by) and posting.route is None
    assert await _board(client, "added_links") == [] and await _to_become_roles() == []


async def test_a_pay_under_the_floor_gates_the_added_posting_out_when_the_browser_names_it(client):
    await analysis_setup()
    body = (await client.post(ADD, json={"url": OWN_POSTING})).json()
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, uuid.UUID(body["posting"]))
        assert add_link.name_posting(posting, {"title": "Director of Programs", "company": "Umbrella Works",
                                               "comp_min": 60000, "comp_max": 70000}, datetime.now(UTC)) is True
        posting.filtered_at = posting.filtered_by = posting.route = None
        await session.flush()
        from app.jobsearch.gating import sift
        from app.jobsearch.profile import store as profile_store
        await sift.sift(session, OWNER, await profile_store.current(session, OWNER), datetime.now(UTC),
                        only=[posting.id])
        await session.commit()
    posting = await _posting(body["posting"])
    assert posting.filtered_by and "$95,000" in posting.filtered_by and posting.route is None


async def test_a_posting_that_is_a_role_the_owner_has_by_its_name_is_linked_to_that_role_by_the_gates(
        client, monkeypatch):
    """The first step is the one every posting meets: a page that names a job the owner already has as a role links the
    posting to the role and sends it nowhere."""
    ids = await analysis_setup()
    body = (await client.post(ADD, json={"url": "https://jobs.example.test/jobs/copy-of-fabrikam"})).json()
    await _named_by_the_browser(body["posting"], "Programs Lead", "Fabrikam")  # the add knows only the link
    posting = await _posting(body["posting"])
    assert posting.opportunity_id == ids["Fabrikam"] and posting.filtered_at is not None and posting.route is None
    assert await _board(client, "added_links") == [] and await _to_become_roles() == []


# --- a job the owner already has --------------------------------------------------------------------------------


async def test_the_same_link_again_answers_with_the_posting_and_makes_nothing(client, knocks, monkeypatch):
    await analysis_setup()
    first = (await client.post(ADD, json={"url": OWN_POSTING})).json()
    await _named_by_the_browser(first["posting"])
    postings, instances = await _count(JobPosting), await _count(ProcInstance)

    # Written another way: no tracking, a bare path with a trailing slash, other case in the host, a page fragment.
    for again in (OWN_POSTING, "https://JOBS.example.test/jobs/7001/", "http://www.jobs.example.test/jobs/7001#x"):
        answer = await client.post(ADD, json={"url": again})
        assert answer.status_code == 200
        body = answer.json()
        assert (body["posting"], body["role"], body["already"], body["stage"]) == (
            first["posting"], None, True, "landing_zone"), again
        assert body["said"] == "Director of Programs at Umbrella Works is already on your pipeline, in Landing Zone."
    assert (await _count(JobPosting), await _count(ProcInstance)) == (postings, instances), "nothing was made"
    assert await _count(JobPosting, JobPosting.ats == ats.OWNER) == 1 and knocks == []


async def test_a_link_that_names_the_same_job_by_its_id_is_the_same_job(client):
    """Exboard's `currentJobId` and its `/jobs/view/<id>` are one job: the id on the same host."""
    await analysis_setup()
    first = (await client.post(ADD, json={"url": EXBOARD})).json()
    other = "https://www.board.test/jobs/collections/recommended/?currentJobId=3912345678&trackingId=made-up"
    answer = (await client.post(ADD, json={"url": other})).json()
    assert (answer["posting"], answer["already"]) == (first["posting"], True)
    assert "That link is already on your pipeline, in Added links." == answer["said"]
    # Another job on the same host is another job.
    new = (await client.post(ADD, json={"url": "https://www.board.test/jobs/view/3912345679/"})).json()
    assert new["already"] is False and new["posting"] != first["posting"]


async def test_the_same_job_on_another_countrys_host_is_the_posting_the_owner_added_before(client):
    first = (await client.post(ADD, json={"url": "https://www.board.test/jobs/view/3912345678"})).json()
    for regional in ("https://ca.board.test/jobs/view/3912345678", "https://uk.board.test/jobs/view/3912345678/"):
        answer = (await client.post(ADD, json={"url": regional})).json()
        assert (answer["posting"], answer["already"]) == (first["posting"], True), regional
    assert await _count(JobPosting, JobPosting.ats == ats.OWNER) == 1


async def test_a_posting_the_log_holds_on_another_countrys_host_is_that_job(client):
    """A posting read from a board's regional address, for a role and for none: the pasted link's host is folded to the
    board's name, so the posting is found. For a role it answers with the role; for none it answers with the posting,
    where it stands (here the filter stopped it), and no second one is made."""
    ids = await analysis_setup()
    stopped_link = "https://uk.board.test/jobs/view/3912345611"
    async with db.session_factory()() as session:
        stopped = JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id="3912345611",
                             match_key="globex|programs lead", company="Globex", title="Programs Lead",
                             url=stopped_link, links=[stopped_link], filtered_by="the place",
                             filtered_at=datetime.now(UTC), first_seen=datetime.now(UTC), last_seen=datetime.now(UTC))
        session.add_all([
            JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id="3912345600",
                       match_key="initech|programs lead", company="Initech", title="Programs Lead",
                       url="https://www.board.test/jobs/view/3912345600?trackingId=made-up", links=[],
                       opportunity_id=ids["Initech"], first_seen=datetime.now(UTC), last_seen=datetime.now(UTC)),
            stopped,
        ])
        await session.commit()
        stopped_id = stopped.id

    held = (await client.post(ADD, json={"url": "https://ca.board.test/jobs/view/3912345600"})).json()
    assert (held["role"], held["already"]) == (str(ids["Initech"]), True)

    again = (await client.post(ADD, json={"url": "https://www.board.test/jobs/view/3912345611"})).json()
    assert (again["posting"], again["role"], again["already"], again["stage"]) == (
        str(stopped_id), None, True, "gated_out")
    assert again["said"] == "Programs Lead at Globex is already on your pipeline, in Gated out: the place"
    assert await _count(JobPosting, JobPosting.ats == ats.OWNER) == 0, "no second posting"


async def test_the_same_id_on_a_host_that_is_no_job_board_is_another_job(client):
    first = (await client.post(ADD, json={"url": "https://www.board.test/jobs/view/3912345678"})).json()
    other = (await client.post(ADD, json={"url": "https://jobs.example.test/jobs/view/3912345678"})).json()
    assert other["already"] is False and other["posting"] != first["posting"]


async def test_a_list_with_a_job_open_and_the_jobs_own_page_are_one_job(client):
    listed = "https://www.board.test/jobs/search/?currentJobId=3912345678"
    first = (await client.post(ADD, json={"url": listed})).json()
    again = (await client.post(ADD, json={"url": "https://www.board.test/jobs/view/3912345678/"})).json()
    assert (again["posting"], again["already"]) == (first["posting"], True)


@pytest.mark.parametrize("stage", [stages.APPLIED, stages.REJECTED])
async def test_a_role_the_scout_found_is_the_job_whatever_stage_it_is_in_and_the_answer_says_where(client, stage):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.status = stage
        await session.commit()
    before, postings = len(await _roles()), await _count(JobPosting)

    # The role's own link, written with a `www.` and a tracking parameter.
    own = "https://www.jobs.example.test/fabrikam/?utm_campaign=made-up"
    answer = (await client.post(ADD, json={"url": own})).json()
    assert (answer["role"], answer["already"], answer["stage"]) == (str(ids["Fabrikam"]), True, stage)
    assert answer["said"] == f"Programs Lead at Fabrikam is already on your pipeline, in {stages.LABELS[stage]}."
    assert (len(await _roles()), await _count(JobPosting)) == (before, postings)
    assert (await _role(ids["Fabrikam"])).analysis_state is None, "nothing was queued for a role the owner had"


async def test_a_link_pasted_for_a_role_that_is_not_sourced_sources_it_and_asks_for_its_research(client):
    """The Crowe case: the role exists (made from a posting the link names) but nothing sourced it. The link is the
    employer's own posting, so it becomes the source, the role is the owner's, and research is queued ahead."""
    ids = await analysis_setup()
    link = "https://careers.example.test/job/R-52052/Transformation-Initiative-Lead"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.status, role.url, role.origin = "first_impression", link, "scout"
        role.sourced_at = role.source_url = role.source_kind = None
        await session.commit()

    answer = (await client.post(ADD, json={"url": link})).json()
    assert (answer["role"], answer["already"], answer["stage"], answer["sourced"]) == (
        str(ids["Fabrikam"]), True, "first_impression", True)
    assert "Your link is its source now" in answer["said"] and "Needs Sorted" not in answer["said"]
    role = await _role(ids["Fabrikam"])
    assert (role.origin, role.source_url, role.source_kind) == ("owner", link, "employer")
    assert role.analysis_state in ("waiting", "no_text") and role.analysis.get("asked_by") == "owner"

    # a link that is a job board's is no source: the role is left as it is
    board = "https://www.board.test/jobs/view/3912345678"
    async with db.session_factory()() as session:
        other = await session.get(Opportunity, ids["Fabrikam"])
        other.sourced_at = other.source_url = other.source_kind = None
        other.analysis_state, other.url, other.origin = None, board, "scout"
        await session.commit()
    again = (await client.post(ADD, json={"url": board})).json()
    assert "Your link is its source now" not in again["said"] and (await _role(ids["Fabrikam"])).sourced_at is None


async def test_a_closed_added_link_added_again_says_it_is_closed_and_why_and_is_not_reopened(client, monkeypatch):
    """The browser read got HTTP 410 and closed the posting; adding the link again answered that it
    was in Added links, a list that does not show it. It says the page showed the posting gone, and stays closed."""
    await analysis_setup()
    serve_page(monkeypatch)
    first = (await client.post(ADD, json={"url": OWN_POSTING})).json()
    async with db.session_factory()() as session:
        (await session.get(JobPosting, uuid.UUID(first["posting"]))).closed_at = datetime.now(UTC)
        await session.commit()
    assert await _board(client, "added_links") == []

    again = (await client.post(ADD, json={"url": OWN_POSTING})).json()
    assert (again["posting"], again["already"], again["stage"]) == (first["posting"], True, "closed")
    assert "Added links" not in again["said"] and "gone" in again["said"] and "closed" in again["said"]
    assert (await _posting(first["posting"])).closed_at is not None


async def test_a_job_whose_posting_the_log_holds_for_a_role_is_that_role(client):
    ids = await analysis_setup()
    async with db.session_factory()() as session:  # a posting read from a board, which became Initech's role
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id="70011", match_key="initech|programs lead",
            company="Initech", title="Programs Lead",
            url="https://boards.example.test/initech/jobs/70011?gh_src=made-up", links=[],
            opportunity_id=ids["Initech"], first_seen=datetime.now(UTC), last_seen=datetime.now(UTC)))
        await session.commit()
    # The same job's id on the same host, written without the parameter the posting's address has.
    answer = (await client.post(ADD, json={"url": "https://www.boards.example.test/initech/jobs/70011"})).json()
    assert (answer["role"], answer["already"]) == (str(ids["Initech"]), True)


async def test_a_posting_the_scout_held_that_is_no_role_yet_becomes_the_owners_role_when_its_link_is_added(client):
    """The CLA case: a posting nobody has looked at (no gates run, no role) and one waiting for a role. Pasting its link
    puts it through the gates and makes the role at once: the owner's own, asked for ahead of the line, in Landing
    Zone, and the answer says so (never a retired status name)."""
    await analysis_setup()
    async with db.session_factory()() as session:
        rows = []
        for n, filtered in enumerate((None, datetime.now(UTC))):
            link = f"https://boards.example.test/initech/jobs/700{n}"
            rows.append(JobPosting(
                owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id=f"700{n}",
                match_key=f"initech|lead {n}",
                company="Initech", title=f"Programs Lead {n}", url=link, links=[link], filtered_at=filtered,
                route="roles" if filtered else None, first_seen=datetime.now(UTC), last_seen=datetime.now(UTC)))
        session.add_all(rows)
        await session.commit()
        links = [r.url for r in rows]
    for n, link in enumerate(links):
        body = (await client.post(ADD, json={"url": link})).json()
        assert body["role"] is not None and body["posting"] is None, body
        assert (body["stage"], body["stage_label"]) == ("landing_zone", "Landing Zone")
        assert "Needs Sorted" not in body["said"] and f"Programs Lead {n} at Initech" in body["said"]
        role = next(r for r in await _roles() if r.title == f"Programs Lead {n}")
        assert role.origin == "owner" and (role.analysis or {}).get("asked_by") == "owner"
        async with db.session_factory()() as session:
            held = await session.scalar(select(JobPosting).where(JobPosting.url == link))
        assert held.opportunity_id == role.id
    assert sorted(c["title"] for c in await _board(client, "landing_zone")) == ["Programs Lead 0", "Programs Lead 1"], (
        "both are counted and shown in the stage the answer named")
    assert await _board(client, "added_links") == []
    async with db.session_factory()() as session:  # and the employer's page lists them, each with its stage
        from app.jobsearch.employers import core as employers
        found = await employers.find(session, OWNER, "Initech")
        shown = await employers.detail(session, OWNER, found.id)
    assert sorted((r["title"], r["status"]) for r in shown["roles"]) == [
        ("Programs Lead 0", "landing_zone"), ("Programs Lead 1", "landing_zone")]
    # asked again, it answers with the role where it stands
    again = (await client.post(ADD, json={"url": links[0]})).json()
    assert again["already"] is True and again["role"] is not None and "Landing Zone" in again["said"]


async def test_a_posting_the_scout_held_becomes_the_owners_role_at_once_and_its_text_is_not_read_inline(
        client, monkeypatch):
    """Old: the add read the held posting's text in code (up to twelve seconds) before making the role. New: the role is
    made from what is stored and the job system's own reader is not run (`fill_details`); the browser queue fills the
    text later. (The research's read-ahead of the source, `analysis.start`, is another step, not the add's.)"""
    await analysis_setup()
    link = "https://boards.example.test/initech/jobs/7100"

    async def never(*args, **kwargs):
        raise AssertionError("the add read the posting's text with the job system's reader")

    async with db.session_factory()() as session:
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id="7100", match_key="initech|lead x",
            company="Initech", title="Programs Lead X", url=link, links=[link], filtered_at=datetime.now(UTC),
            route="roles", first_seen=datetime.now(UTC), last_seen=datetime.now(UTC)))
        await session.commit()
    serve_page(monkeypatch)
    monkeypatch.setattr(postings_index, "fill_details", never)
    body = (await client.post(ADD, json={"url": link})).json()
    assert body["role"] is not None
    role = await _role(uuid.UUID(body["role"]))
    assert role.origin == "owner" and (role.analysis or {}).get("asked_by") == "owner"



# --- what is not a link -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "not a link", "", "   ", "ftp://files.example.test/job", "javascript:alert(1)", "mailto:jobs@example.test",
    "http://localhost:8430/job/1", "http://192.168.1.20/job/1", "http://127.0.0.1/job", "http://printer.local/job",
    "https://user:secret@jobs.example.test/job/1",
], ids=lambda t: t[:24] or "empty")
async def test_what_is_not_a_public_web_address_is_refused_and_nothing_is_made(client, knocks, text):
    await analysis_setup()
    before = (len(await _roles()), await _count(JobPosting), await _count(ProcInstance))
    refused = await client.post(ADD, json={"url": text})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]
    assert (len(await _roles()), await _count(JobPosting), await _count(ProcInstance)) == before
    assert knocks == []


async def test_a_body_with_no_link_is_refused(client):
    assert (await client.post(ADD, json={})).status_code == 422
    assert (await client.post(ADD, json={"url": None})).status_code == 422


async def test_a_link_over_the_longest_address_is_refused_in_words(client):
    refused = await client.post(ADD, json={"url": "https://jobs.example.test/job/" + "a" * 1600})
    assert refused.status_code == 422 and "too long" in refused.json()["detail"]


# --- roles an added link made before it became a posting (still researched, named and gated like any role) ---


async def test_the_analyst_is_handed_added_link_roles_one_at_a_time_and_a_researched_one_is_not_handed_again(
        client, monkeypatch):
    """The owner who pasted links has each role read in turn: the analyst is handed one, and once its findings are in it
    is not handed that role again but the next."""
    await analysis_setup()
    serve_page(monkeypatch)

    first = await added_link_role("https://jobs.example.test/jobs/9")
    second = await added_link_role("https://jobs.example.test/jobs/10")

    async def handed() -> str:
        async with db.session_factory()() as session:
            reply = await analysis.analysis_reply(session, OWNER)
            await session.commit()
        return reply["role"]["id"]

    one = await handed()
    assert one in (first, second)
    reply = await submit_findings(findings(one, title="Programs Lead", company="Umbrella Works",
                                           fit_breakdown=answered("not", "not", "not"), location="Remote, US",
                                           work_mode="remote", target_role="none"))
    assert reply["taken"] is True
    assert await handed() == (second if one == first else first)


# --- researched and gated like any role -------------------------------------------------------------------------


async def _added_and_waiting(client, link: str = EXBOARD) -> str:
    """A role made from an added link (the shape such roles had until 2026-10-15, still in the database) that nobody can
    read the posting of. A job board's link leaves the role not sourced and
    unresearched; the job's own link is its source and, with no text, the role waits for it. Returns the role's id."""
    ids = await analysis_setup()
    await _night_starts(ids["Hooli"])
    role_id = await added_link_role(link)
    role = await _role(uuid.UUID(role_id))
    assert role.analysis_state == (analysis.NO_TEXT if role.sourced_at else None)
    return role_id


async def _added_and_taken(client, link: str = EXBOARD) -> tuple[str, dict]:
    """As `_added_and_waiting`, then the text comes: the browser reader found the employer's posting of a job board's
    link and handed it in whole (`sourcing.accept_source`), or the owner's browser copied the page of the job's own
    link. The role is in the analyst's line and the session is handed it to research; returns the role's id and its
    task."""
    role_id = await _added_and_waiting(client, link)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, uuid.UUID(role_id))
        if role.sourced_at is None:
            assert await sourcing.accept_source(
                session, role, FOUND_POSTING, "employer", POSTING_WORDS, True, "browser reader") == "sourced"
            await analysis.ask(session, OWNER, role.id)
        else:
            assert await posting_text.take(
                session, role, posting_text.Capture(POSTING_WORDS)) == posting_text.FETCHED
            assert await analysis.resume_waiting(session, OWNER, [role.id]) == 1
        await session.commit()
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER)
        await session.commit()
    assert task["role"]["id"] == role_id, "the role is first in the analyst's line again"
    return role_id, task


async def test_the_analyst_is_asked_to_name_the_role_and_the_posting_names_it(client):
    role_id, task = await _added_and_taken(client)
    role = task["role"]
    assert (role["title"], role["company"]) == (None, None), "a link is no title, and 'Added link' is no employer"
    assert "title" in role["name_the_role"] and "company" in role["name_the_role"]
    assert role["board_url"] == EXBOARD_CLEAN and "task_now" not in task, "its text came: it is researched now"
    assert role["url"] == FOUND_POSTING, "the employer's own posting is the link it is read from"

    reply = await submit_findings(findings(
        role_id, title="Director of Programs", company="Umbrella Works", fit_breakdown=answered(),
        location="Remote, US", work_mode="remote", target_role="none"))
    assert reply["taken"] is True and reply["stage"] == stages.READY_TO_LAUNCH, "80% is over the 75% drafting bar"

    role = await _role(uuid.UUID(role_id))
    assert (role.title, role.company, role.origin) == ("Director of Programs", "Umbrella Works", "owner")
    assert role.match_key == "umbrella works|director of programs"
    assert role.slug == "umbrella-works-director-of-programs"
    assert (role.url, role.board_url) == (FOUND_POSTING, EXBOARD_CLEAN), "the job board's link is kept beside it"
    async with db.session_factory()() as session:
        employer = await session.get(Employer, role.employer_id)
        assert employer is not None and employer.name == "Umbrella Works", "the employer is the posting's"
        posting = (await session.scalars(select(JobPosting).where(JobPosting.opportunity_id == role.id))).one()
    assert (posting.company, posting.title, posting.match_key) == (
        "Umbrella Works", "Director of Programs", "umbrella works|director of programs")
    assert await _count(Employer, Employer.owner == OWNER, Employer.key == "added link") == 0
    named = await _lines(role.id, "role_named")
    assert [e.sentence for e in named] == ["The analyst read the link: Director of Programs at Umbrella Works."]
    assert "researched Director of Programs at Umbrella Works" in (await _lines(role.id, "analysed"))[0].sentence


async def test_a_gate_that_fails_after_research_puts_the_added_role_in_gated_out_with_its_reason(client):
    role_id, _ = await _added_and_taken(client, "https://jobs.example.test/jobs/7")
    reply = await submit_findings(findings(
        role_id, title="Programs Lead", company="Umbrella Works", fit_breakdown=answered(), pay=FAILS_THE_FLOOR,
        location="Remote, US", work_mode="remote", target_role="none"))
    assert reply["taken"] is True and reply["stage"] == stages.GATED_OUT
    role = await _role(uuid.UUID(role_id))
    assert role.status == stages.GATED_OUT and role.origin == "owner"
    assert "$95,000" in role.screened_out_reason
    assert (role.title, role.company) == ("Programs Lead", "Umbrella Works")


async def test_a_posting_nobody_could_read_leaves_the_role_named_by_its_link_unresearched_and_makes_no_employer(client):
    role_id = await _added_and_waiting(client)
    role = await _role(uuid.UUID(role_id))
    assert (role.company, role.title) == (ADDED_LINK, "board.test/jobs/view/3912345678")
    assert role.employer_id is None and role.analysis_state is None and not role.analysis.get("required")
    assert role.sourced_at is None, "not sourced: nothing was read, nothing was researched"
    assert await _count(Employer, Employer.owner == OWNER, Employer.key == "added link") == 0


async def test_an_added_job_is_matched_by_its_id_alone_only_until_research_names_it(client):
    """The job is the owner's by its id while it has no employer: another employer's name asking by the same id still
    finds it. Once the analyst has named it, it is an ordinary role again: only its own employer's link is a match."""
    link = "https://listings.example.test/jobs/view/3912345678"
    role_id, _ = await _added_and_taken(client, link)
    other = ("https://www.listings.example.test/comm/jobs/view/director-3912345678?refId=made-up",)

    async def known() -> same_role.Known[uuid.UUID]:
        async with db.session_factory()() as session:
            return await same_role.known_roles(session, OWNER)

    assert (await known()).find("Fabrikam", "Programs Director", other) == uuid.UUID(role_id)

    reply = await submit_findings(findings(
        role_id, title="Director of Programs", company="Umbrella Works", fit_breakdown=answered(),
        location="Remote, US", work_mode="remote", target_role="none"))
    assert reply["taken"] is True
    named = await known()
    assert named.find("Fabrikam", "Programs Director", other) is None, "named: a link across two employers is no match"
    assert named.find("Umbrella Works", "Head of Programs", other) == uuid.UUID(role_id), "its own employer's is"
    assert named.find("Umbrella Works", "Director - Programs", ()) == uuid.UUID(role_id), "and so are its own words"


async def test_a_name_is_taken_only_from_a_role_that_has_none(client):
    """The analyst's title and company are for the job the owner added by its link, and for no other role."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    await submit_findings(findings(ids["Fabrikam"], title="Something Else", company="Another Co"))
    role = await _role(ids["Fabrikam"])
    assert (role.title, role.company, role.match_key) == ("Programs Lead", "Fabrikam", "fabrikam|programs lead")


async def test_a_posting_that_turns_out_to_be_a_role_the_owner_has_keeps_its_link_as_its_name_and_says_so(client):
    """The owner pasted a link the app could not tell was a job it holds (another address for it). Research reads the
    posting and finds the same company and title: the role is not renamed to a name another role has."""
    role_id, _ = await _added_and_taken(client, "https://jobs.example.test/jobs/programs-lead-copy")
    reply = await submit_findings(findings(role_id, title="Programs Lead", company="Fabrikam", fit_breakdown=answered(),
                                           location="Remote, US", work_mode="remote", target_role="none"))
    assert reply["taken"] is True, "the research is kept: nothing about a duplicate refuses it"
    role = await _role(uuid.UUID(role_id))
    assert (role.company, role.title) == (ADDED_LINK, "jobs.example.test/jobs/programs-lead-copy")
    said = await _lines(role.id, "role_named")
    assert len(said) == 1 and "Programs Lead at Fabrikam, which you already have" in said[0].sentence
    assert await _count(Opportunity, Opportunity.owner == OWNER, Opportunity.match_key == "fabrikam|programs lead") == 1
