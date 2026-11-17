"""A role is Sourced when the app knows the company and holds a link to the original posting (the owner, 2026-10-15):
on the employer's own careers site or job system, or the posting where a recruiter's Apply lands, or, for an Easy Apply
with no link to follow, the board's own posting. The job description is pulled only from that source, word for word;
only a Sourced role is researched; every accepted link is ONE posting, never a front page. Every name, link and
sentence is made up, and no test reaches the network."""


import json
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import ADDED_LINK, Employer, JobPosting, Opportunity
from app.jobsearch.owner_actions import runner
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis
from app.models.work import TrailEvent
from tests.builders import (
    PARAGRAPHS,
    WHOLE,
    analysis_setup,
    findings,
    put_in_line,
    serve_page,
    submit_findings,
)
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.support import OWNER, POSTING_WORDS

pytestmark = pytest.mark.usefixtures("made_up_boards")


POSTING = "https://careers.fabrikam.example.test/jobs/4012"


RECRUITER = "https://apply.talentco.example.test/jobs/88123"


BOARDONE = "https://www.boardone.test/jobs/view/3912345678"


#: An employer's own careers site on a host that is also a job board's (`ats.OWN_CAREERS`).
ON_A_BOARDS_HOST = "https://www.boardfour.test/about/careers/applications/jobs/results/123456789-program-manager"


def _web(monkeypatch, handle) -> SimpleNamespace:
    """Answer every address of the made-up web with `handle`; `pages` is what was asked (robots.txt apart)."""
    seen = SimpleNamespace(pages=[])

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.pages.append(str(request.url))
        return handle(request)

    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0))
    return seen


async def _role(*, sourced: bool = False, title: str = "Programs Lead", company: str = "Fabrikam", **more) -> uuid.UUID:
    """A Found role found on Boardone, not Sourced unless said."""
    values = {"owner": OWNER, "slug": f"{company}-{title}".lower().replace(" ", "-"), "company": company,
              "title": title, "match_key": f"{company}|{title}".lower(), "status": stages.SECOND_IMPRESSION,
              "origin": "scout", "location": "Remote, US", "move_type": "remote", "url": BOARDONE,
              "board_url": BOARDONE, **more}
    if not sourced and "sourced_at" not in values:
        values["sourced_at"] = None
    async with db.session_factory()() as session:
        role = Opportunity(**values)
        session.add(role)
        await session.commit()
        return role.id


async def _get(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _lines(role_id, event: str) -> list[TrailEvent]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(
            TrailEvent.link_kind == "opportunity", TrailEvent.link_id == role_id, TrailEvent.event == event))).all())


async def _accept(role_id, url, kind, text=None, whole=False, by="browser reader") -> str:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        reply = await sourcing.accept_source(session, role, url, kind, text, whole, by)
        await session.commit()
        return reply


async def test_an_employers_own_posting_is_the_source_and_its_text_is_fetched_word_for_word(js, monkeypatch):
    seen = serve_page(monkeypatch)
    role_id = await _role()
    assert await _accept(role_id, POSTING, "employer") == "sourced"
    role = await _get(role_id)
    assert (role.source_url, role.source_kind) == (POSTING, "employer") and role.sourced_at is not None
    assert role.description == WHOLE and role.description_at is not None, "the posting's own words, whole"
    assert (role.url, role.board_url) == (POSTING, BOARDONE), "read from the source; the board's link kept beside it"
    assert seen.pages == [POSTING], "nothing else was read, and no job board"
    [line] = await _lines(role_id, "sourced")
    assert (line.actor, line.who, line.link_id) == ("agent", "browser reader", role_id)
    assert POSTING in line.detail and "employer's own posting" in line.sentence


async def test_an_employers_posting_on_a_boards_host_is_a_source_not_a_job_boards_address(js, monkeypatch):
    """19 source items for roles at an employer whose careers site is on a host that is also a job
    board were refused, "it is on a job board" and "not one posting's address", whatever the page was."""
    seen = serve_page(monkeypatch)
    role_id = await _role()
    assert await _accept(role_id, ON_A_BOARDS_HOST, "employer") == "sourced"
    role = await _get(role_id)
    assert (role.source_url, role.source_kind, role.description) == (ON_A_BOARDS_HOST, "employer", WHOLE)
    assert (role.url, role.board_url) == (ON_A_BOARDS_HOST, BOARDONE)
    assert seen.pages == [ON_A_BOARDS_HOST]
    refused = await _accept(await _role(title="Other Lead"), "https://www.boardfour.test/search?q=lead", "employer")
    assert refused.startswith("rejected:")


async def test_a_recruiters_posting_where_apply_lands_is_a_source_of_its_own_kind(js, monkeypatch):
    serve_page(monkeypatch)
    role_id = await _role()
    assert await _accept(role_id, RECRUITER, "recruiter", by="code") == "sourced"
    role = await _get(role_id)
    assert (role.source_url, role.source_kind, role.description) == (RECRUITER, "recruiter", WHOLE)
    [line] = await _lines(role_id, "sourced")
    assert line.actor == "app" and "recruiter" in line.sentence


async def test_an_easy_apply_takes_the_boards_own_posting_when_its_text_is_whole(js, monkeypatch):
    seen = _web(monkeypatch, lambda request: pytest.fail(f"nothing is read for a board's own posting: {request.url}"))
    role_id = await _role()
    assert await _accept(role_id, BOARDONE, "board", text=WHOLE, whole=True) == "sourced"
    role = await _get(role_id)
    assert (role.source_url, role.source_kind, role.description) == (BOARDONE, "board", WHOLE)
    assert role.url == BOARDONE and role.sourced_at is not None and seen.pages == []


@pytest.mark.parametrize("text, whole, why", [
    (WHOLE, False, "not marked as the whole posting"),
    ("A snippet of the listing.", True, "snippet"),
    (WHOLE + " ... see more", True, "cut off"),
    (None, True, "no text"),
])
async def test_an_easy_apply_needs_the_whole_text_and_nothing_less_is_kept(js, text, whole, why):
    role_id = await _role()
    reply = await _accept(role_id, BOARDONE, "board", text=text, whole=whole)
    assert reply.startswith("rejected:") and why in reply
    role = await _get(role_id)
    assert (role.source_url, role.sourced_at, role.description) == (None, None, None)


SITE = "https://careers.fabrikam.example.test"


#: The employer's own website: on the registered domain of `SITE` (the made-up `example.test`).
WEBSITE = "https://www.fabrikam.example.test"


@pytest.mark.parametrize("link, root", [
    (POSTING, f"{SITE}/"),
    (f"{SITE}/en-us/jobs/r4012/senior-program-manager-remote", f"{SITE}/"),
    (f"{SITE}/jobs/4012?lang=en-us&src=Boardone#apply", f"{SITE}/"),
    (f"{SITE}/us/en/job/R4012/Program-Manager", f"{SITE}/"),
    ("https://jobs.fabrikam.example.test/job/Austin-Program-Manager/4012-en_US", "https://jobs.fabrikam.example.test/"),
    ("https://www.careers.fabrikam.example.test/jobs/4012", "https://www.careers.fabrikam.example.test/"),
    (f"{SITE}:8443/apply/4012", f"{SITE}:8443/"),
], ids=lambda value: str(value)[8:70])
def test_the_careers_root_of_a_posting_on_a_careers_host_is_the_root_of_the_host_never_a_guessed_path(link, root):
    """Found by review 2026-11-04: of the roots the first version would have kept, some were a
    `/jobs` that answers 404 on a host whose root is the careers page, and a page once kept is never replaced."""
    assert sourcing.careers_root(link) == root


@pytest.mark.parametrize("link, root", [
    ("https://www.fabrikam.example.test/careers/positions/program-manager-r1234",
     "https://www.fabrikam.example.test/careers"),
    ("https://www.fabrikam.example.test/careers/senior-program-manager", "https://www.fabrikam.example.test/careers"),
    ("https://www.fabrikam.example.test/careers?gh_jid=4012345", "https://www.fabrikam.example.test/careers"),
    ("https://www.fabrikam.example.test/en-us/jobs/r4012/senior-program-manager",
     "https://www.fabrikam.example.test/en-us/jobs"),
], ids=lambda value: str(value)[8:70])
def test_the_careers_root_of_a_posting_on_a_front_site_is_the_jobs_section_its_address_is_under(link, root):
    assert sourcing.careers_root(link) == root


@pytest.mark.parametrize("link", [
    "https://www.fabrikam.example.test/job/4012",
    "https://www.fabrikam.example.test/us/en/job/R4012/program-manager",
    "https://www.fabrikam.example.test/join-us/program-manager?jobid=4012",
    f"{SITE}/jobs/search?q=program+manager",
    f"{SITE}/",
    "https://www.boardone.test/login?next=https%3A%2F%2Fcareers.fabrikam.example.test%2Fjobs%2F4012",
    "http://localhost/jobs/4012",
    "",
    None,
], ids=lambda value: str(value)[8:70] or "empty")
def test_no_careers_root_where_the_page_is_not_one_posting_or_the_jobs_section_is_not_told(link):
    """A company's front site with a posting under `/job/` does not say which page is its careers page; a search is no
    posting; neither is a sign-in page or an address that is not public."""
    assert sourcing.careers_root(link) is None


def _employer(**over) -> Employer:
    """An employer record as `learn_careers_page` reads it (nothing stored)."""
    return Employer(**{"owner": OWNER, "key": "fabrikam", "name": "Fabrikam", **over})


@pytest.mark.parametrize("link", [
    "https://fabrikam.hirevendor.test/jobs/4012",
    "https://jobs.hirevendor.test/fabrikam/careers/4012",
    "https://www.hirevendor.test/jobs/fabrikam-program-manager-4012",
], ids=["its tenant is in the host", "its tenant is in the path", "the vendor's own page"])
def test_a_posting_on_a_careers_vendors_host_is_not_the_employers_careers_page_though_no_list_names_the_vendor(link):
    """Found by review 2026-11-04: nothing tied the posting's host to the employer, and a hiring platform not in the
    list of shared hosts (`pairs._shared_hosts`) would have been kept as the employer's own site, then fed the mail
    match and the shared-careers-site question as if it were. The host is the employer's when it is its website's
    domain, or its name is in the domain."""
    employer = _employer(website="https://www.fabrikam.test")
    assert sourcing.learn_careers_page(employer, link) is False
    assert employer.careers_url is None


@pytest.mark.parametrize("over, link, root", [
    ({"name": "Contoso Freight", "website": "https://www.cf-logistics.test"},
     "https://jobs.cf-logistics.test/job/4012-program-manager", "https://jobs.cf-logistics.test/"),
    ({"name": "Fabrikam Rail"}, "https://careers.fabrikamrail.test/jobs/4012", "https://careers.fabrikamrail.test/"),
    ({"name": "Fabrikam Rail"}, "https://careers.fabrikam-rail.test/jobs/4012", "https://careers.fabrikam-rail.test/"),
    ({"name": "Northern Dynamic Logistics"}, "https://careers.ndl.test/jobs/4012", "https://careers.ndl.test/"),
    ({"name": "Fabrikam Rail", "aliases": ["Contoso Freight"]}, "https://jobs.contosofreight.test/jobs/4012",
     "https://jobs.contosofreight.test/"),
], ids=["its website's domain", "its name in the domain", "its name with a hyphen", "its initials",
        "another name it goes by"])
def test_a_posting_on_a_site_with_the_employers_domain_or_name_gives_its_careers_page(over, link, root):
    employer = _employer(**over)
    assert sourcing.learn_careers_page(employer, link) is True
    assert employer.careers_url == root


def test_a_word_every_employer_of_a_kind_has_in_its_name_does_not_tie_a_site_to_the_employer():
    """"Systems" is in the domain of a hiring platform and in the names of many employers: it tells none of them
    apart (`pairs.DESCRIPTORS`)."""
    employer = _employer(name="Fabrikam Systems")
    assert sourcing.learn_careers_page(employer, "https://jobs.systemshire.test/jobs/4012") is False
    assert employer.careers_url is None


def test_a_two_letter_word_of_a_name_in_a_domain_ties_no_site_to_the_employer():
    employer = _employer(name="Fabrikam IT")
    assert sourcing.learn_careers_page(employer, "https://jobs.digitalhire.test/jobs/4012") is False
    assert employer.careers_url is None


def test_a_posting_on_one_of_the_owners_job_sites_is_never_a_careers_page_though_the_employers_website_is_there():
    """The analyst sets a website while reading one posting, so a website can be a page on a job site the owner lists
    in Where to look; the posting is then on the employer's domain by the record, and on a job board by the owner's
    word."""
    employer = _employer(website="https://www.nicheboard.test/employers/fabrikam")
    assert sourcing.learn_careers_page(employer, "https://www.nicheboard.test/jobs/4012", {"nicheboard.test"}) is False
    assert employer.careers_url is None


async def _employer_row(**over) -> uuid.UUID:
    async with db.session_factory()() as session:
        employer = Employer(**{"owner": OWNER, "key": "fabrikam", "name": "Fabrikam", "website": WEBSITE, **over})
        session.add(employer)
        await session.commit()
        return employer.id


async def _careers_of(employer_id) -> tuple[str | None, dict]:
    async with db.session_factory()() as session:
        employer = await session.get(Employer, employer_id, populate_existing=True)
        return employer.careers_url, (employer.sources or {}).get("careers_url") or {}


async def test_an_employers_own_posting_on_a_site_no_job_system_owns_gives_the_employer_that_sites_careers_page(
        js, monkeypatch):
    serve_page(monkeypatch)
    employer_id = await _employer_row()
    role_id = await _role(employer_id=employer_id)
    assert await _accept(role_id, POSTING, "employer") == "sourced"
    careers_url, said = await _careers_of(employer_id)
    assert careers_url == f"{SITE}/", "the root of the careers site, not the one posting"
    assert (said["source"], said["by"]) == ("its own posting's site", "resumebot")


async def test_a_role_hired_through_a_staffing_firm_by_its_company_text_gives_no_careers_page(js, monkeypatch):
    """The app keeps the tag on the role's company text ("Fabrikam (via Staffco)" under the employer
    "Fabrikam", `pairs._facts`), not in the employer's name."""
    serve_page(monkeypatch)
    employer_id = await _employer_row()
    role_id = await _role(employer_id=employer_id, company="Fabrikam (via Staffco)")
    assert await _accept(role_id, POSTING, "employer") == "sourced"
    assert await _careers_of(employer_id) == (None, {})


OTHER = "Field Logistics Manager"


def _job_page(title: str) -> str:
    """A page that embeds the JobPosting of a job titled `title`, with the posting's whole text."""
    ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": title,
          "description": "".join(f"<p>{p}</p>" for p in PARAGRAPHS)}
    return (f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body>'
            + "".join(f"<p>{p}</p>" for p in PARAGRAPHS) + "</body></html>")


async def _listed(url: str, title: str, system: str = "own_site", ext: str = "4012", role_id=None) -> None:
    """A posting the app's index holds at `url`, with the title its job system or its site gave."""
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        session.add(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=system, ext_id=ext, company="Fabrikam",
                               title=title, match_key=f"fabrikam|{title}".lower(), url=url, first_seen=now,
                               last_seen=now, links=[], opportunity_id=role_id))
        await session.commit()


async def _accept_capture(role_id, page: bytes | None, by="browser reader", url=POSTING) -> str:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        reply = await sourcing.accept_source(session, role, url, "employer", WHOLE, True, by, fetch=False, page=page)
        await session.commit()
        return reply


async def _nothing_kept(role_id) -> None:
    role = await _get(role_id)
    assert (role.source_url, role.source_kind, role.sourced_at, role.description) == (None, None, None, None)
    assert await _lines(role_id, "sourced") == []


async def test_a_link_whose_page_embeds_another_jobs_posting_is_rejected_and_nothing_is_kept(js, monkeypatch):
    seen = serve_page(monkeypatch, _job_page(OTHER))
    role_id = await _role()
    reply = await _accept(role_id, POSTING, "employer")
    assert reply.startswith("rejected:") and OTHER in reply and "Programs Lead" in reply
    await _nothing_kept(role_id)
    assert seen.pages == [POSTING], "the page was read once"


async def test_a_link_whose_page_embeds_the_roles_own_posting_is_taken_and_its_page_is_read_once(js, monkeypatch):
    seen = serve_page(monkeypatch, _job_page("Programs Lead - Remote - 44263"))
    role_id = await _role()
    assert await _accept(role_id, POSTING, "employer") == "sourced"
    role = await _get(role_id)
    assert role.source_url == POSTING and "Paragraph 7 of the posting" in (role.description or "")
    assert seen.pages == [POSTING], "what the check read is the text kept: no second read"


async def test_a_capture_whose_page_embeds_another_jobs_posting_is_rejected(js):
    role_id = await _role()
    reply = await _accept_capture(role_id, _job_page(OTHER).encode())
    assert reply.startswith("rejected:") and OTHER in reply
    await _nothing_kept(role_id)


@pytest.mark.parametrize("page", [None, b"<html><head><title>Careers</title></head></html>",
                                  _job_page("Programs Lead").encode()], ids=["no page", "no title", "its own title"])
async def test_a_capture_that_states_no_other_title_is_not_judged(js, page):
    """Only a title the page states is evidence: a text with no title in it is any posting's text."""
    role_id = await _role()
    assert await _accept_capture(role_id, page) == "sourced"
    assert (await _get(role_id)).description == WHOLE


@pytest.mark.parametrize("given", [POSTING, POSTING + "/"], ids=["as listed", "with a slash"])
async def test_a_link_its_job_system_lists_as_another_job_is_rejected_before_any_page_is_read(js, monkeypatch, given):
    await _listed(POSTING, OTHER)
    seen = _web(monkeypatch, lambda request: pytest.fail(f"nothing is read for a refused link: {request.url}"))
    role_id = await _role()
    reply = await _accept(role_id, given, "employer")
    assert reply.startswith("rejected:") and OTHER in reply and "lists" in reply
    await _nothing_kept(role_id)
    assert seen.pages == []


async def test_a_link_any_posting_lists_under_the_roles_own_title_is_taken(js, monkeypatch):
    await _listed(POSTING, OTHER, ext="1")
    await _listed(POSTING, "Programs Lead (Remote)", ext="2")
    serve_page(monkeypatch)
    role_id = await _role()
    assert await _accept(role_id, POSTING, "employer") == "sourced"


@pytest.mark.parametrize("system, of_the_role", [("own_site", True), ("import", False), ("browser", False)],
                         ids=["the role's own posting", "a tracker's row", "a browser's row"])
async def test_a_posting_that_is_the_roles_own_or_no_source_of_the_site_is_not_the_job_systems_word(
        js, monkeypatch, system, of_the_role):
    role_id = await _role()
    await _listed(POSTING, OTHER, system=system, role_id=role_id if of_the_role else None)
    serve_page(monkeypatch)
    assert await _accept(role_id, POSTING, "employer") == "sourced"


async def test_a_link_the_owner_gave_is_kept_as_it_is_whatever_the_page_or_the_job_system_says(js, monkeypatch):
    await _listed(POSTING, OTHER)
    serve_page(monkeypatch, _job_page(OTHER))
    role_id = await _role()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        said = await sourcing.accept_source(session, role, POSTING, "employer", None, False, "owner")
        await session.commit()
    assert said == "no_text", "kept as the source; the page of another job gave no text"
    assert (await _get(role_id)).source_url == POSTING
    other = await _role(title="Ops Lead")
    async with db.session_factory()() as session:
        assert await sourcing.source_given(session, await session.get(Opportunity, other), POSTING) is True
        await session.commit()
    assert (await _get(other)).source_url == POSTING


async def test_a_role_added_by_its_link_is_not_judged_by_a_title_it_does_not_have(js, monkeypatch):
    await _listed(POSTING, OTHER)
    serve_page(monkeypatch, _job_page(OTHER))
    role_id = await _role(company=ADDED_LINK, title=BOARDONE)
    assert await _accept(role_id, POSTING, "employer") == "sourced"


async def test_a_source_whose_page_cannot_be_read_is_kept_with_no_text_unless_a_capture_came_with_it(js, monkeypatch):
    _web(monkeypatch, lambda request: httpx.Response(404))
    role_id = await _role()
    assert await _accept(role_id, POSTING, "employer") == "no_text"
    role = await _get(role_id)
    assert role.sourced_at is not None and role.source_url == POSTING and role.description is None

    other = await _role(title="Ops Lead")
    assert await _accept(other, POSTING, "employer", text=WHOLE, whole=True, by="owner") == "sourced"
    assert (await _get(other)).description == WHOLE, "the capture of that same page is the text"
    [line] = await _lines(other, "sourced")
    assert line.actor == "person"


async def test_a_text_is_never_cut_and_one_that_is_not_the_whole_posting_is_not_kept(js, monkeypatch):
    _web(monkeypatch, lambda request: httpx.Response(404))
    long = "\n".join(f"Paragraph {i} of a long posting says one thing about the work." for i in range(400))
    role_id = await _role()
    assert await _accept(role_id, POSTING, "employer", text=long, whole=True) == "sourced"
    assert (await _get(role_id)).description == long, "kept as it is, never cut"
    other = await _role(title="Ops Lead")
    assert await _accept(other, POSTING, "employer", text="A snippet.", whole=True) == "no_text"
    assert (await _get(other)).description is None


async def test_a_capture_given_with_the_source_takes_the_place_of_a_snippet_description_and_never_a_usable_one(js):
    """A description under 300 characters is a listing's snippet, which is no text (docs/PRD.md 39.29): the capture of
    the source's own page, whole, is the role's text. Before the posting-text deepening any description at all kept the
    capture out (`keep_capture` and `accept_source` each asked for none), so a role that held a snippet waited for a
    text it had been handed; a usable description is still never overwritten."""
    read = datetime(2026, 10, 8, tzinfo=UTC)
    snippet = await _role(description="Programs Lead at Fabrikam. Remote.", description_at=read)
    usable = await _role(title="Ops Lead", description=POSTING_WORDS, description_at=read)
    for role_id in (snippet, usable):
        async with db.session_factory()() as session:
            role = await session.get(Opportunity, role_id)
            said = await sourcing.accept_source(session, role, POSTING, "employer", WHOLE, True, "owner", fetch=False)
            await session.commit()
        assert said == "sourced"
    assert (await _get(snippet)).description == WHOLE
    assert (await _get(usable)).description == POSTING_WORDS


@pytest.mark.parametrize("kind, url", [("employer", POSTING), ("board", BOARDONE)])
async def test_a_capture_kept_with_the_source_sets_the_posted_day_its_text_says(js, kind, url):
    """Every text kept goes through `take`, which sets the posted day from it (`posted_dates.apply`); the board's own
    posting never did before, though the employer's capture was dated by `keep_capture`."""
    role_id = await _role()
    assert (await _get(role_id)).posted is None
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        text = WHOLE + chr(10) + "Date posted: 2026-10-07"
        said = await sourcing.accept_source(session, role, url, kind, text, True, "owner", fetch=False)
        await session.commit()
    assert said == "sourced"
    assert (await _get(role_id)).posted == date(2026, 10, 7)


async def test_a_new_source_replaces_the_text_of_the_old_one(js, monkeypatch):
    serve_page(monkeypatch)
    role_id = await _role(sourced=True, source_url=RECRUITER, source_kind="recruiter",
                          description="From the old source.")
    assert await _accept(role_id, POSTING, "employer") == "sourced"
    role = await _get(role_id)
    assert (role.source_url, role.source_kind, role.description) == (POSTING, "employer", WHOLE)


async def test_only_the_source_is_read_for_the_text_and_a_role_that_is_not_sourced_has_none_fetched(js, monkeypatch):
    seen = serve_page(monkeypatch)
    sourced = await _role(sourced=True, source_url=POSTING, source_kind="employer",
                          apply_url="https://apply.other.example.test/jobs/55")
    unsourced = await _role(title="Ops Lead", url=POSTING, apply_url=POSTING)
    async with db.session_factory()() as session:
        assert await posting_text.take(session, await session.get(Opportunity, sourced)) == posting_text.FETCHED
        assert await posting_text.take(session, await session.get(Opportunity, unsourced)) is None
        await session.commit()
    assert seen.pages == [POSTING], "its source, and no other link of any role"
    assert (await _get(sourced)).description == WHOLE and (await _get(unsourced)).description is None


async def _source(role_id, **kwargs) -> str:
    async with db.session_factory()() as session:
        reply = await sourcing.source_in_code(session, await session.get(Opportunity, role_id), **kwargs)
        await session.commit()
        return reply


async def test_an_employers_own_careers_site_the_owner_also_lists_as_a_job_site_is_not_a_job_board(js):
    """A careers site listed as a job site and as the employer's own site made every role found there
    "board only", so its own posting was never taken as the source and a browser session was sent to find it."""
    from app.sites.models import Site

    async with db.session_factory()() as session:
        session.add(Employer(owner=OWNER, key="fabrikam", name="Fabrikam",
                             careers_url="https://careers.fabrikam.example.test/search"))
        for kind, page in (("job_board", "home"), ("employer", "search-results")):
            session.add(Site(owner=OWNER, url=f"https://careers.fabrikam.example.test/{page}", kind=kind, on=True,
                             created_at=datetime.now(UTC)))
        session.add(Site(owner=OWNER, url="https://jobs.sitebox.example.test/search", kind="job_board", on=True,
                         created_at=datetime.now(UTC)))
        await session.commit()
        assert await employer_posting.job_board_hosts(session, OWNER) == {"jobs.sitebox.example.test"}
    role_id = await _role(url=POSTING, board_url=POSTING, board="careers.fabrikam.example.test")
    assert await _source(role_id) == "sourced"
    role = await _get(role_id)
    assert (role.source_url, role.source_kind) == (POSTING, "employer")


async def test_research_picks_and_starts_only_sourced_roles(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["Initech"])).sourced_at = None
        await session.commit()
    async with db.session_factory()() as session:
        assert {r.company for r in await analysis.picks(session, OWNER, None)} == {"Fabrikam", "Hooli"}


async def test_a_role_that_is_not_sourced_is_left_in_found_when_put_in_line_and_says_so(js):
    ids = await analysis_setup()
    await put_in_line(ids["Initech"], sourced_at=None, source_url=None, source_kind=None, url=BOARDONE)
    role = await _get(ids["Initech"])
    assert (role.status, role.analysis_state, role.analysis_instance_id is not None) == (
        stages.SECOND_IMPRESSION, None, True)
    [line] = await _lines(ids["Initech"], "not_sourced")
    assert "not sourced yet" in line.sentence
    async with db.session_factory()() as session:
        assert await analysis.open_step(session, role) is None
        reply = await analysis.analysis_reply(session, OWNER)
    assert reply["task"] is None


async def test_the_owner_asking_for_a_role_that_is_not_sourced_is_told_it_cannot_be_researched_yet(client):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        (await session.get(Opportunity, ids["Fabrikam"])).sourced_at = None
        await session.commit()
    asked = (await client.post(f"/api/v1/roles/{ids['Fabrikam']}/research")).json()
    assert asked["state"] is None and "not sourced yet" in asked["said"]


async def test_the_analyst_never_sources_a_role_and_never_writes_its_text(js):
    ids = await analysis_setup()
    await put_in_line(ids["Fabrikam"])
    before = await _get(ids["Fabrikam"])
    reply = await submit_findings(findings(ids["Fabrikam"], employer_posting_url=POSTING, posting_text="A summary."))
    assert reply["taken"] is True
    after = await _get(ids["Fabrikam"])
    assert (after.source_url, after.source_kind, after.url) == (before.source_url, before.source_kind, before.url)
    assert after.description == before.description == POSTING_WORDS


async def test_what_an_agent_reads_of_a_role_says_whether_it_is_sourced_and_where(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Initech"])
        role.sourced_at = role.source_url = role.source_kind = None
        await session.commit()
    async with db.session_factory()() as session:
        sourced = (await runner.read(session, OWNER, "role.state", {"role": str(ids["Fabrikam"])}))["result"]
        bare = (await runner.read(session, OWNER, "role.state", {"role": str(ids["Initech"])}))["result"]
    assert (sourced["sourced"], sourced["role"]["source_kind"]) == (True, "employer")
    assert sourced["role"]["source_url"] == "https://jobs.example.test/fabrikam"
    assert (bare["sourced"], bare["role"]["source_url"], bare["role"]["source_kind"]) == (False, None, None)
