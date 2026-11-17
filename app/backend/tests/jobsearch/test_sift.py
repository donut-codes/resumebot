"""The cheap filter (docs/PRD.md 28.10, 30.1): the gates that need no judgement, in code, over postings nobody has
filtered; each stopped posting keeps its rule, each candidate says what let it through and where it goes. The
classifier's side is tested in test_classifier.py. Synthetic data only."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import actions, adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.gating import sift
from app.jobsearch.models import Employer, JobPosting
from app.jobsearch.postings import index as postings
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.gates import compensation_says, location_says
from app.sites.models import Site
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

NOW = datetime(2026, 10, 8, 5, 30, tzinfo=UTC)


async def _profile(session, **criteria):
    await load_profile(session, OWNER, criteria={**PROFILE["criteria"], **criteria})
    return await profile_store.current(session, OWNER)


def _posting(employer, ext_id, title, **more):
    return JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=ext_id, company=employer.name,
                      title=title, first_seen=NOW, last_seen=NOW, links=[], **more)


async def test_with_no_history_each_posting_goes_on_to_become_a_role_or_stops_with_its_rule(js):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        acme = await employers.find_or_make(session, OWNER, "Acme")
        initech = await employers.find_or_make(session, OWNER, "Initech")
        initech.gated = True
        rows = [
            _posting(fab, "1", "Director, Data Governance", location="Remote", work_mode="remote"),
            _posting(fab, "2", "Senior Manager, Operations", location="Remote", work_mode="remote",
                     text="Leads the programs office and the delivery lead group."),
            _posting(fab, "3", "Head of Delivery Lead Program", location="Greater Gotham", work_mode="onsite"),
            _posting(fab, "4", "Delivery Lead", location="Remote", work_mode="remote", comp_min=60000, comp_max=70000),
            _posting(acme, "5", "Data Governance Lead", location="Remote", work_mode="remote"),
            _posting(initech, "6", "Data Governance Lead", location="Remote", work_mode="remote"),
            _posting(fab, "7", "Payroll Specialist", location="Remote", work_mode="remote"),
            _posting(fab, "8", "Data governance director", location="Remote", work_mode="remote", closed_at=NOW),
            _posting(fab, "9", "Summer Intern, Programs", location="Remote", work_mode="remote"),
            _posting(fab, "10", "Internal Audit Lead", location="Remote", work_mode="remote"),
        ]
        session.add_all(rows)
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
        assert result["looked_at"] == 9, "a closed posting isn't looked at"
        assert {c["posting"].ext_id: c["route"] for c in result["candidates"]} == {
            "1": "roles", "2": "roles", "7": "roles", "9": "roles", "10": "roles"}, (
            "no search words: whatever the gates leave goes on to become a role")
        place, pay = location_says(profile.gates.location), compensation_says(profile.gates.compensation)
        assert result["stopped"] == {place: 1, pay: 1, "an employer you never work for": 2}, (
            "a gate's reason is its own sentence")
        assert result["routes"] == {"roles": 5} and result["model"] is None
        by_id = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
        assert by_id["1"].route == "roles" and by_id["1"].route_why == "no history to learn from yet"
        assert by_id["3"].route is None and by_id["1"].fit_prob is None
        assert by_id["3"].filtered_by == place and by_id["1"].filtered_by is None and by_id["1"].filtered_at
        assert by_id["9"].filtered_by is None and by_id["9"].route == "roles" and by_id["10"].filtered_by is None
        again = await sift.sift(session, OWNER, profile, now=NOW)
        assert again["looked_at"] == 0, "a posting is filtered once"


def test_there_is_one_today_and_it_is_the_day_in_the_owners_zone(monkeypatch):
    """The filter read the day in UTC, the employers and the layoff notices in the machine's zone, the rest in
    the owner's: a posting could be stale to one and fresh to another for hours each night."""
    from datetime import date

    from app.settings import settings
    from app.timeutil import owner_today

    monkeypatch.setattr(settings, "timezone", "Pacific/Auckland")
    late = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)  # still the 8th in UTC, already the 9th in Auckland
    assert owner_today(late) == date(2026, 10, 9)
    monkeypatch.setattr(settings, "timezone", "America/Los_Angeles")
    assert owner_today(late) == date(2026, 10, 8)


async def test_the_roles_among_the_postings_are_read_whole_not_asked_for_by_each_postings_key(js):
    """One parameter per match key in one statement, and the driver takes 32,767: a big board or a bulk reset
    went over. The owner's roles are read whole, by the owner alone, so
    a posting can be asked about in any writing of its company and title (same_role.py)."""
    from sqlalchemy import event

    from app.jobsearch.models import Opportunity

    async with db.session_factory()() as session:
        profile = await _profile(session)
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([_posting(fab, str(n), f"Role {n}", location="Remote", work_mode="remote",
                                  match_key=f"fabrikam|role {n}") for n in range(40)])
        session.add_all([Opportunity(owner=OWNER, slug=f"fabrikam-role-{n}", company="Fabrikam", title=f"Role {n}",
                                     match_key=f"fabrikam|role {n}", fit=3, origin="scout") for n in (1, 6)])
        await session.flush()
        sizes: list[int] = []

        def noted(_conn, _cursor, statement, parameters, _context, _many):
            if "js_opportunities" in statement:
                sizes.append(len(parameters))

        engine = db.engine().sync_engine
        event.listen(engine, "before_cursor_execute", noted)
        try:
            result = await sift.sift(session, OWNER, profile, now=NOW)
        finally:
            event.remove(engine, "before_cursor_execute", noted)
    assert result["stopped"] == {"already a role": 2} and len(result["candidates"]) == 38
    assert sizes and max(sizes) <= 10, f"no statement has a parameter for each of the 40 postings: {sizes}"


async def test_a_posting_that_is_a_role_the_owner_has_in_other_words_is_linked_to_it_and_judged_no_further(js):
    """The owner's role is "Director, Programs"; the board's posting of it says "Director - Programs". Found when a role
    the owner had applied to came back as Found: the match key is exact, so the posting went on to be read."""
    from app.jobsearch.models import Opportunity

    async with db.session_factory()() as session:
        profile = await _profile(session)
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        contoso = await employers.find_or_make(session, OWNER, "Contoso")
        role = Opportunity(owner=OWNER, slug="fabrikam-director-programs", company="Fabrikam", origin="tracker",
                           title="Director, Programs", match_key="fabrikam|director, programs", status="applied",
                           url="https://www.listings.example.test/jobs/view/3912345678/")
        session.add(role)
        session.add_all([
            _posting(fab, "1", "Director - Programs", location="Remote", work_mode="remote"),
            _posting(fab, "2", "Senior Director, Programs", location="Remote", work_mode="remote"),
            _posting(fab, "3", "Head of Program Delivery", location="Remote", work_mode="remote",
                     url="https://listings.example.test/comm/jobs/view/3912345678?trackingId=x"),
            _posting(fab, "4", "Operations Analyst", location="Remote", work_mode="remote",
                     url="https://listings.example.test/comm/jobs/view/3912345679"),
            # the role's link, but another employer's posting: the tracker row may carry the wrong link
            _posting(contoso, "5", "Head of Program Delivery", location="Remote", work_mode="remote",
                     url="https://listings.example.test/comm/jobs/view/3912345678"),
        ])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
        by_id = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
        assert result["stopped"] == {"already a role": 2}, "the same words, and the same link at the same employer"
        assert by_id["1"].opportunity_id == role.id and by_id["3"].opportunity_id == role.id
        assert by_id["1"].filtered_at == NOW and by_id["1"].filtered_by is None and by_id["1"].route is None
        assert by_id["2"].opportunity_id is None and by_id["4"].opportunity_id is None, "another job, another id"
        assert by_id["5"].opportunity_id is None, "the same link at another employer is a new posting"
        assert sorted(c["posting"].ext_id for c in result["candidates"]) == ["2", "4", "5"]


async def test_a_posting_of_a_job_the_owner_added_by_its_link_is_that_role_until_research_names_it(js):
    """The owner pasted the job's link; the analyst has not read it, so the role has no employer ("Added link") and no
    title but the link. The night's board read lists the same job under its employer's name, with its own address for
    it: it is the owner's role by its id, and no second role is made while the first waits (docs/PRD.md 24.10). A
    posting of another job is another posting, and the same id on another host is another job."""
    from app.jobsearch.models import ADDED_LINK, ORIGIN_OWNER, Opportunity

    async with db.session_factory()() as session:
        profile = await _profile(session)
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        role = Opportunity(owner=OWNER, slug="added-link-1", company=ADDED_LINK, origin=ORIGIN_OWNER,
            status="second_impression",
                           title="listings.example.test/jobs/view/3912345678",
                           match_key="added link|listings.example.test/jobs/view/3912345678",
                           url="https://listings.example.test/jobs/view/3912345678")
        session.add(role)
        session.add_all([
            _posting(fab, "1", "Director, Programs", location="Remote", work_mode="remote",
                     url="https://www.listings.example.test/comm/jobs/view/director-programs-3912345678?refId=x"),
            _posting(fab, "2", "Operations Analyst", location="Remote", work_mode="remote",
                     url="https://listings.example.test/jobs/view/3912345679"),
            _posting(fab, "3", "Operations Analyst II", location="Remote", work_mode="remote",
                     url="https://elsewhere.example.test/jobs/view/3912345678"),
        ])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
        by_id = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
        assert result["stopped"] == {"already a role": 1}
        assert by_id["1"].opportunity_id == role.id and by_id["1"].filtered_by is None
        assert by_id["2"].opportunity_id is None and by_id["3"].opportunity_id is None
        assert sorted(c["posting"].ext_id for c in result["candidates"]) == ["2", "3"]


async def test_a_posting_filed_under_the_name_a_merge_gave_the_employer_is_a_role_the_owner_already_has(js):
    """Two employer records were merged and the one kept took the fuller name; the role the owner applied to still
    carries the company it was found under, and the board is read under the record's name. Found when a night's
    posting of that job was taken for a new role: the roles are known by every name their employer goes by."""
    from app.jobsearch.employers import records as employer_records
    from app.jobsearch.models import Opportunity

    async with db.session_factory()() as session:
        profile = await _profile(session)
        keep = await employers.find_or_make(session, OWNER, "Harbor Mutual")
        away = await employers.find_or_make(session, OWNER, "Harbor Mutual Insurance")
        role = Opportunity(owner=OWNER, slug="harbor-director-programs", company="Harbor Mutual", origin="tracker",
                           title="Director, Programs", match_key="harbor mutual|director, programs", status="applied",
                           employer_id=keep.id)
        session.add(role)
        await session.flush()
        done = await employer_records.merge(session, OWNER, keep.id, away.id, why="a test")
        assert done.employer.name == "Harbor Mutual Insurance"
        session.add_all([
            _posting(done.employer, "1", "Director - Programs", location="Remote", work_mode="remote"),
            _posting(done.employer, "2", "Senior Director, Programs", location="Remote", work_mode="remote"),
        ])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
        by_id = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
        assert result["stopped"] == {"already a role": 1}
        assert by_id["1"].company == "Harbor Mutual Insurance" and by_id["1"].opportunity_id == role.id
        assert by_id["2"].opportunity_id is None, "another job at the same employer is still a new role"
        assert [c["posting"].ext_id for c in result["candidates"]] == ["2"]


async def test_no_title_phrase_stops_a_posting(js):
    """What to look for says what the owner wants, not a list of counterexamples (docs/PRD.md 30.5): the filter reads
    a title like any other."""
    async with db.session_factory()() as session:
        profile = await _profile(session)
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([
            _posting(fab, "1", "Summer INTERN, Programs", location="Remote", work_mode="remote"),
            _posting(fab, "2", "Sales Rep (Inside)", location="Remote", work_mode="remote"),
        ])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
        assert result["stopped"] == {}, "no title phrase stops a posting"
        assert {c["posting"].ext_id: c["route"] for c in result["candidates"]} == {"1": "roles", "2": "roles"}
        stops = (await session.scalars(select(JobPosting.filtered_by).where(JobPosting.filtered_by.is_not(None)))).all()
        assert stops == []


async def test_a_gated_employer_seen_on_a_job_site_is_stopped_by_its_name(js):
    """The browser reader files a posting under the site's id, not an employer's (it would put thousands of names on the
    Employers screen), so the employer gate has to read the company's name: by the employer's match key (case, "Inc."
    and punctuation folded), and by any name it is also called."""
    async with db.session_factory()() as session:
        profile = await _profile(session)
        initech = await employers.find_or_make(session, OWNER, "Initech")
        initech.gated, initech.aliases = True, ["Initrode"]
        site = Site(owner=OWNER, url="https://jobs.example.test/", kind="job_board", read_by="browser", created_at=NOW)
        session.add(site)
        await session.flush()
        assert await session.get(Employer, site.id) is None, "the posting's employer_id is not an employer"
        seen = [("1", "INITECH, Inc."), ("2", "Initrode"), ("3", "Hooli"), ("4", "Initech Staffing")]
        session.add_all([
            JobPosting(owner=OWNER, employer_id=site.id, ats="browser", ext_id=ext, company=company, title="Director",
                       first_seen=NOW, last_seen=NOW, links=[], location="Remote", work_mode="remote")
            for ext, company in seen])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
        by_id = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert by_id["1"].filtered_by == by_id["2"].filtered_by == "an employer you never work for"
    assert by_id["3"].filtered_by is None, "an employer nobody gated is read as before"
    assert by_id["4"].filtered_by is None, "a different company whose name only starts the same is not the employer"
    assert result["stopped"]["an employer you never work for"] == 2


async def test_a_gated_employer_is_stopped_under_the_tail_of_its_legal_name_and_a_note_of_who_posted_it(js):
    """Found when the gated record "Wingtip (via Fabrikam)" did not stop one posting of "Wingtip and Company": the match
    key kept the "and" the legal name's tail left behind, so the two were different employers."""
    async with db.session_factory()() as session:
        profile = await _profile(session)
        wingtip = await employers.find_or_make(session, OWNER, "Wingtip (via Fabrikam)")
        wingtip.gated = True
        site = Site(owner=OWNER, url="https://jobs.example.test/", kind="job_board", read_by="browser", created_at=NOW)
        session.add(site)
        await session.flush()
        seen = [("1", "Wingtip and Company"), ("2", "WINGTIP & Co."), ("3", "Wingtip Staffing"), ("4", "Wing")]
        session.add_all([
            JobPosting(owner=OWNER, employer_id=site.id, ats="browser", ext_id=ext, company=company, title="Director",
                       first_seen=NOW, last_seen=NOW, links=[], location="Remote", work_mode="remote")
            for ext, company in seen])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
        by_id = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert by_id["1"].filtered_by == by_id["2"].filtered_by == "an employer you never work for"
    assert by_id["3"].filtered_by is None and by_id["4"].filtered_by is None, "another company is not the employer"
    assert result["stopped"]["an employer you never work for"] == 2


async def test_the_search_phrases_do_not_filter(js):
    async with db.session_factory()() as session:
        profile = await _profile(session, search_plan=["data governance"])
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(_posting(fab, "1", "Payroll Specialist", location="Remote", work_mode="remote"))
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
    assert [c["posting"].ext_id for c in result["candidates"]] == ["1"] and result["stopped"] == {}


def test_a_remote_role_abroad_is_named_and_one_that_says_the_united_states_is_not():
    from app.jobsearch.measures.countries import abroad

    us = ["United States"]
    assert abroad("Philippines", us) == "Philippines"
    assert abroad("Gurugram, Haryana", us) == "India"
    assert abroad("London, England, United Kingdom", us) == "United Kingdom"
    assert abroad("Remote - United States", us) is None
    assert abroad("Remote Kentucky", us) is None
    assert abroad("Ohio, United States", us) is None
    assert abroad("San Francisco, CA, or Remote within Canada or United States", us) is None
    assert abroad("Delta - Remote", us) is None, "a place in no list is left for the scout"
    assert abroad("Toronto, Ontario", []) is None, "no countries set: any country"
    assert abroad("Toronto, Ontario", ["Canada"]) is None


def test_a_place_in_another_country_is_abroad_even_when_its_country_code_is_also_a_states():
    """The US check ran first and read DE, IN, CA and IL as Delaware, Indiana, California and Illinois."""
    from app.jobsearch.measures.countries import abroad

    us = ["United States"]
    assert abroad("Berlin, DE", us) == "Germany"
    assert abroad("Bangalore, IN", us) == "India"
    assert abroad("Toronto, CA", us) == "Canada"
    assert abroad("Tel Aviv, IL", us) == "Israel"
    assert abroad("Mexico City, Latin America", us) == "Mexico", "a bare America is not the United States"
    assert abroad("REMOTE WORK IN CANADA", us) == "Canada", "an all-capitals IN is not Indiana"
    # What is still the United States, or left to the scout.
    assert abroad("Manchester, NH", us) is None and abroad("Dublin, OH", us) is None
    assert abroad("Remote - US or Canada", us) is None
    assert abroad("Remote, North America or Canada", us) is None
    assert abroad("Austin, TX", us) is None


async def test_the_filter_stops_a_remote_role_in_another_country(js):
    async with db.session_factory()() as session:
        plan = ["data governance"]
        location = {**PROFILE["gates"]["location"], "remote": {"on": True, "locations": ["United States"]}}
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"], "search_plan": plan},
                           gates={**PROFILE["gates"], "location": location})
        profile = await profile_store.current(session, OWNER)
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([_posting(fab, "1", "Data Governance Lead", location="Philippines", work_mode="remote"),
                         _posting(fab, "2", "Data Governance Lead II", location="Remote - United States",
                                  work_mode="remote")])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
    assert [c["posting"].ext_id for c in result["candidates"]] == ["2"]
    assert result["stopped"] == {location_says(profile.gates.location): 1}


# --- a place that is only a count ("26 Locations") --------------------------------------------------------------------


def _fetcher() -> Fetcher:
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


@dataclass
class PlacesPage:
    """A job system whose posting page names the places (`Adapter.places`), answering each posting as it is told to."""

    pages: dict = field(default_factory=dict)
    asked: list = field(default_factory=list)
    places = True

    async def detail(self, fetch, board, posting):
        self.asked.append((posting["ext_id"], posting["location"]))
        answer = self.pages[posting["ext_id"]]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _wd(employer, ext_id, location, **more):
    """A posting on a job system whose page names the places (ats `tenantone`, as the adapters name it)."""
    return JobPosting(owner=OWNER, employer_id=employer.id, ats="tenantone", ext_id=ext_id, company=employer.name,
                      title=f"Director of Programs {ext_id}", first_seen=NOW, last_seen=NOW, links=[],
                      location=location, **more)


async def test_a_count_of_places_stops_nothing_at_the_filter(js):
    """A posting whose list gives only a count of places says nothing about where the work is. It was stopped as a place
    the owner doesn't take (13 in one night, one of them with an allowed place among its 26)."""
    async with db.session_factory()() as session:
        profile = await _profile(session)
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([
            _posting(fab, "1", "Director, Data Governance", location="26 Locations"),
            _posting(fab, "2", "Director, Data Governance II", location="2 Locations", work_mode="onsite"),
            _posting(fab, "3", "Director, Data Governance III", location="Multiple Locations", work_mode="hybrid"),
            _posting(fab, "4", "Director, Data Governance IV", location=None, work_mode="onsite"),
            _posting(fab, "5", "Director, Data Governance V", location="Gotham, NY", work_mode="onsite"),
        ])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
    assert sorted(c["posting"].ext_id for c in result["candidates"]) == ["1", "2", "3", "4"], "unknown goes on"
    assert result["stopped"] == {location_says(profile.gates.location): 1}, "a place that is named and not taken stops"
    assert result["read"] == 0, "with no fetcher nothing is read"


async def test_the_filter_names_a_listed_town_by_its_state_and_holds_a_town_that_has_none(js):
    """A town listed alone matched the same name in every state (ten stored postings in a trial of the tracker
    import)."""
    where = {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield, OH"]},
             "in_person": {"on": True, "locations": ["Oakfield, OH"]}}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={**PROFILE["gates"], "location": where})
        profile = await profile_store.current(session, OWNER)
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([
            _posting(fab, "ohio", "Director, Data Governance", location="Oakfield, OH", work_mode="onsite"),
            _posting(fab, "jersey", "Director, Data Governance II", location="Oakfield, NJ", work_mode="onsite"),
            _posting(fab, "tennessee", "Director, Data Governance III", location="US-TN-Oakfield", work_mode="hybrid"),
            _posting(fab, "none", "Director, Data Governance IV", location="Oakfield", work_mode="onsite"),
        ])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
    assert sorted(c["posting"].ext_id for c in result["candidates"]) == ["none", "ohio"], (
        "the town with its state goes on; so does one that names no state (a reader finds where it is)")
    assert result["stopped"] == {location_says(profile.gates.location): 2}


async def test_the_filter_reads_the_page_that_names_a_postings_places_and_judges_the_places(js, monkeypatch):
    page = PlacesPage({
        "away": {"location": "Gotham, NY; Denver, CO", "work_mode": "onsite", "text": "Away text.",
                 "posted": "2026-10-01"},
        "near": {"location": "Gotham, NY; Oakfield, ST", "work_mode": "onsite", "text": "Near text."},
        "short": {"text": "Short text."},  # the page did not account for every place the list counted
        "down": adapters.AdapterError("tenantone answered 500", 500),
    })
    monkeypatch.setattr(adapters, "ADAPTERS", {"tenantone": page})
    async with db.session_factory()() as session:
        profile = await _profile(session)
        hooli = await employers.find_or_make(session, OWNER, "Hooli")
        session.add_all([_wd(hooli, "away", "2 Locations"), _wd(hooli, "near", "2 Locations"),
                         _wd(hooli, "short", "9 Locations"), _wd(hooli, "down", "3 Locations"),
                         _wd(hooli, "named", "Oakfield, ST", work_mode="onsite")])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW, fetch=_fetcher(), reads=10)
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert sorted(page.asked) == [("away", "2 Locations"), ("down", "3 Locations"), ("near", "2 Locations"),
                                  ("short", "9 Locations")], "the postings whose place is a count; not the named one"
    assert result["read"] == 4
    away, near = rows["away"], rows["near"]
    assert away.filtered_by == location_says(profile.gates.location), "none of its places is one the owner works from"
    assert (away.location, away.work_mode, away.text, str(away.posted)) == (
        "Gotham, NY; Denver, CO", "onsite", "Away text.", "2026-10-01")
    assert away.judged_by == "gate" and away.detail_at == NOW, "stopped by the gate, and the page is marked as read"
    assert near.filtered_by is None and near.route == "roles" and near.location == "Gotham, NY; Oakfield, ST", (
        "one of its places is the owner's: it goes on, and the title pass and the reader see the places")
    assert near.text == "Near text.", "the description came with the page: the night's descriptions need not read it"
    short, down = rows["short"], rows["down"]
    assert short.filtered_by is None and short.location == "9 Locations" and short.text == "Short text.", (
        "a page that does not account for the count leaves the place unknown, and the posting goes on")
    assert down.filtered_by is None and down.location == "3 Locations" and down.text is None, (
        "a page that cannot be read leaves the place unknown, and the posting goes on")
    assert down.detail_at == NOW, "tried: the night's descriptions leave it alone for a few days"
    assert rows["named"].filtered_by is None and rows["named"].detail_at is None


async def test_the_filter_reads_no_more_pages_than_it_is_given_and_judges_the_rest_unknown(js, monkeypatch):
    page = PlacesPage({str(n): {"location": "Gotham, NY", "work_mode": "onsite"} for n in range(5)})
    monkeypatch.setattr(adapters, "ADAPTERS", {"tenantone": page})
    async with db.session_factory()() as session:
        profile = await _profile(session)
        hooli = await employers.find_or_make(session, OWNER, "Hooli")
        session.add_all([_wd(hooli, str(n), f"{n + 2} Locations") for n in range(5)])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW, fetch=_fetcher(), reads=2)
    assert len(page.asked) == 2 and result["read"] == 2
    assert len(result["candidates"]) == 3 and sum(result["stopped"].values()) == 2, (
        "the two read were stopped on their places; the three left unread are unknown and go on")


async def test_the_filter_begins_no_page_once_the_nights_time_has_run_out_and_leaves_those_postings_unmarked(
        js, monkeypatch):
    """docs/PRD.md 28.17: with no limit on the pages (reads -1) the night's time is what bounds the filter's reads. Past
    it no page is begun; a posting not read keeps its place unknown, goes on, and is not marked as tried."""
    page = PlacesPage({str(n): {"location": "Gotham, NY", "work_mode": "onsite"} for n in range(3)})
    monkeypatch.setattr(adapters, "ADAPTERS", {"tenantone": page})
    async with db.session_factory()() as session:
        profile = await _profile(session)
        hooli = await employers.find_or_make(session, OWNER, "Hooli")
        session.add_all([_wd(hooli, str(n), f"{n + 2} Locations") for n in range(3)])
        await session.flush()
        late = polite.give_time(_fetcher(), -1)  # the time ran out before the filter began
        result = await sift.sift(session, OWNER, profile, now=NOW, fetch=late, reads=-1)
        rows = (await session.scalars(select(JobPosting))).all()
    assert page.asked == [], "no page was begun"
    assert len(result["candidates"]) == 3 and result["stopped"] == {}, (
        "a place not read is unknown, and the posting goes on")
    assert all(p.location.endswith("Locations") and p.detail_at is None for p in rows), (
        "left as it was, not marked as tried: the next read takes it")


@pytest.mark.parametrize(("gate_off", "fetch", "reads"), [
    (False, False, 10), (False, True, 0), (True, True, 10)], ids=["no fetcher", "no pages allowed", "no location gate"])
async def test_the_filter_reads_no_page_without_a_fetcher_a_number_to_read_or_a_work_location_gate(
        js, monkeypatch, gate_off, fetch, reads):
    page = PlacesPage({"1": {"location": "Gotham, NY", "work_mode": "onsite"}})
    monkeypatch.setattr(adapters, "ADAPTERS", {"tenantone": page})
    async with db.session_factory()() as session:
        profile = await _profile(session)
        profile.gates.sections_off = ["posting"] if gate_off else []
        hooli = await employers.find_or_make(session, OWNER, "Hooli")
        session.add(_wd(hooli, "1", "2 Locations"))
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW, fetch=_fetcher() if fetch else None, reads=reads)
    assert page.asked == [] and result["read"] == 0
    assert [c["posting"].ext_id for c in result["candidates"]] == ["1"], "unknown, so it goes on"


async def test_the_pages_the_filter_read_come_out_of_the_nights_descriptions(js, monkeypatch):
    page = PlacesPage({"a": {"location": "Oakfield, ST", "work_mode": "onsite"},
                       "b": {"location": "Riverton, ST", "work_mode": "onsite"}})

    @dataclass
    class Descriptions:
        places = False
        asked: list = field(default_factory=list)

        async def detail(self, fetch, board, posting):
            self.asked.append(posting["ext_id"])
            return {"text": f"About {posting['title']}."}

    feedone = Descriptions()
    monkeypatch.setattr(adapters, "ADAPTERS", {"tenantone": page, EXAMPLE_SYSTEM: feedone})
    monkeypatch.setattr(polite, "make", _fetcher)
    async with db.session_factory()() as session:
        await _profile(session)
        hooli = await employers.find_or_make(session, OWNER, "Hooli")
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([_wd(hooli, "a", "2 Locations"), _wd(hooli, "b", "2 Locations")])
        session.add_all([_posting(fab, str(n), f"Director of Delivery {n}", location="Remote", work_mode="remote")
                         for n in range(3)])
        await session.flush()
        sifted = await actions.sift_postings(session, None, {"owner": OWNER, "details_per_night": 3})
        assert sifted["read"] == 2 and sifted["candidates"] == 5
        done = await actions.read_descriptions(
            session, None, {"owner": OWNER, "details_per_night": 3, "sifted": sifted})
        assert done == {"filled": 1, "left": 0} and len(feedone.asked) == 1, (
            "3 descriptions a night, 2 of them the filter's")
        left = await actions.read_descriptions(
            session, None, {"owner": OWNER, "details_per_night": 2, "sifted": sifted})
        assert left == {"filled": 0, "left": 0}, "the filter used the whole night's number"


def test_the_nights_pages_left_are_what_the_number_allows_and_a_negative_number_is_no_limit():
    assert postings.pages_left(150) == 150 and postings.pages_left(150, 13) == 137
    assert postings.pages_left(5, 9) == 0 and postings.pages_left(0) == 0 and postings.pages_left(0, 4) == 0
    assert postings.pages_left(-1) is None and postings.pages_left(-1, 40) is None


async def test_a_negative_number_lets_the_filter_and_the_descriptions_read_every_page_they_want(js, monkeypatch):
    page = PlacesPage({str(n): {"location": "Oakfield, ST", "work_mode": "onsite"} for n in range(6)})

    @dataclass
    class Descriptions:
        places = False
        asked: int = 0

        async def detail(self, fetch, board, posting):
            self.asked += 1
            return {"text": f"About {posting['title']}."}

    feedone = Descriptions()
    monkeypatch.setattr(adapters, "ADAPTERS", {"tenantone": page, EXAMPLE_SYSTEM: feedone})
    monkeypatch.setattr(polite, "make", _fetcher)
    async with db.session_factory()() as session:
        await _profile(session)
        hooli = await employers.find_or_make(session, OWNER, "Hooli")
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([_wd(hooli, str(n), f"{n + 2} Locations") for n in range(6)])
        session.add_all([_posting(fab, str(n), f"Director of Delivery {n}", location="Remote", work_mode="remote")
                         for n in range(4)])
        await session.flush()
        sifted = await actions.sift_postings(session, None, {"owner": OWNER, "details_per_night": -1})
        assert sifted["read"] == 6 and len(page.asked) == 6, "every posting whose place is a count"
        done = await actions.read_descriptions(
            session, None, {"owner": OWNER, "details_per_night": -1, "sifted": sifted})
        assert done == {"filled": 4, "left": 0} and feedone.asked == 4, "and every description that waits"


async def test_the_filter_reads_up_to_read_at_once_pages_at_once(js, monkeypatch):
    @dataclass
    class Slow:
        places = True
        active: int = 0
        peak: int = 0
        asked: int = 0

        async def detail(self, fetch, board, posting):
            self.active, self.asked = self.active + 1, self.asked + 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return {"location": "Oakfield, ST", "work_mode": "onsite"}

    slow = Slow()
    monkeypatch.setattr(adapters, "ADAPTERS", {"tenantone": slow})
    async with db.session_factory()() as session:
        profile = await _profile(session)
        hooli = await employers.find_or_make(session, OWNER, "Hooli")
        count = postings.READ_AT_ONCE + 6
        session.add_all([_wd(hooli, str(n), f"{n + 2} Locations") for n in range(count)])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW, fetch=_fetcher(), reads=-1)
        rows = (await session.scalars(select(JobPosting))).all()
    assert (slow.asked, slow.peak, result["read"]) == (count, postings.READ_AT_ONCE, count)
    assert all(p.location == "Oakfield, ST" and p.detail_at == NOW for p in rows)


# --- a big filter is written in slices ----------------------------------------------


async def _a_hundred_postings(owner: str = OWNER, *, profile: bool = True):
    """A hundred open postings nobody has filtered, a minute apart: the even ones pass the gates, the odd ones are
    stopped by the place gate."""
    async with db.session_factory()() as session:
        await load_profile(session, owner, criteria={**PROFILE["criteria"]})
        fab = await employers.find_or_make(session, owner, "Fabrikam")
        session.add_all([JobPosting(
            owner=owner, employer_id=fab.id, ats=EXAMPLE_SYSTEM, ext_id=str(n), company=fab.name,
            title=f"Programs Role {n}", first_seen=NOW + timedelta(minutes=n), last_seen=NOW, links=[],
            location="Remote" if n % 2 == 0 else "Greater Gotham", work_mode="remote" if n % 2 == 0 else "onsite")
            for n in range(100)])
        await session.commit()
        return await profile_store.current(session, owner)


async def test_a_big_filter_commits_in_slices(js, monkeypatch):
    """The night's filter judged every new posting in one transaction (2 to 10 seconds, once a night). Each slice of
    `sift.CHUNK` is whole and committed before the next is written, so no posting is held for more than a slice."""
    from sqlalchemy import func

    from app.jobsearch.work import checkpoint

    monkeypatch.setattr(sift, "CHUNK", 20)
    profile = await _a_hundred_postings()
    seen: list[int] = []
    real = checkpoint.reached

    async def reached():
        async with db.read_session_factory()() as other:  # a reader: what is committed, as the slice ends
            seen.append(await other.scalar(select(func.count()).select_from(JobPosting).where(
                JobPosting.filtered_at.is_not(None))) or 0)
        await real()

    monkeypatch.setattr(checkpoint, "reached", reached)
    async with db.session_factory()() as session:
        result = await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
    assert seen == [0, 20, 40, 60, 80], "every slice but the one being written was committed"
    assert result["looked_at"] == 100 and len(result["candidates"]) == 50 and sum(result["stopped"].values()) == 50


async def test_a_posting_another_run_filtered_meanwhile_is_left_as_that_run_judged_it(js, monkeypatch):
    """A slice is locked and looked at again before it is written: a posting filtered since it was read is not
    judged twice, and one another transaction holds is left for the next turn's filter, which takes what is
    unfiltered."""
    monkeypatch.setattr(sift, "CHUNK", 20)
    profile = await _a_hundred_postings()
    real_cooperate, judged = sift.Cooperate, []

    class Probing(real_cooperate):
        """The judge's pace-keeper. At the first posting judged of the fifth slice, which holds no writer turn, another
        run filters a posting of that slice, read already."""

        async def __call__(self) -> None:
            judged.append(1)
            if len(judged) == 81:
                async with db.session_factory()() as other:
                    row = (await other.scalars(select(JobPosting).where(JobPosting.ext_id == "80"))).one()
                    row.filtered_at, row.filtered_by = NOW, "another run's verdict"
                    await other.commit()
            await super().__call__()

    monkeypatch.setattr(sift, "Cooperate", Probing)
    async with db.session_factory()() as session:
        result = await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
    assert "80" not in {c["posting"].ext_id for c in result["candidates"]}
    async with db.session_factory()() as session:
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert rows["80"].filtered_by == "another run's verdict" and rows["80"].route is None
    assert all(p.filtered_at is not None for ext, p in rows.items()), "and every other posting was judged"


async def test_a_sliced_filter_judges_every_posting_as_the_one_transaction_filter_does(js, monkeypatch):
    """The same hundred postings under two owners: one filtered as the night's driver does it, slice by slice, the other
    inside a savepoint, where nothing can be released and the whole filter is one transaction as it always was."""
    monkeypatch.setattr(sift, "CHUNK", 7)
    other = "another"
    profile = await _a_hundred_postings()
    other_profile = await _a_hundred_postings(other)
    async with db.session_factory()() as session:
        sliced = await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
    async with db.session_factory()() as session:
        async with session.begin_nested():
            whole = await sift.sift(session, other, other_profile, now=NOW)
        await session.commit()
    assert {k: v for k, v in sliced.items() if k != "candidates"} == {k: v for k, v in whole.items()
                                                                       if k != "candidates"}
    assert len(sliced["candidates"]) == len(whole["candidates"]) == 50

    async def verdicts(owner: str) -> list:
        async with db.session_factory()() as session:
            rows = (await session.scalars(select(JobPosting).where(JobPosting.owner == owner))).all()
            return sorted((p.ext_id, p.filtered_by, p.route, p.route_why, p.fit_prob, p.stop_gate, p.stop_gate_id,
                           p.judged_by, p.judged_by_version, p.stop_hard_pass, p.filtered_at is not None)
                          for p in rows)

    assert await verdicts(OWNER) == await verdicts(other)


async def test_a_filter_begun_while_the_writer_turn_is_held_waits_for_it_and_judges_each_posting_once(js, monkeypatch):
    """There is no row to hold on SQLite, only the one writer turn: a filter that begins while another transaction has
    it waits (it does not fail and it does not skip a posting), then judges all hundred; the next turn's filter finds
    none left."""
    monkeypatch.setattr(sift, "CHUNK", 20)
    profile = await _a_hundred_postings()
    holding, let_go = asyncio.Event(), asyncio.Event()

    async def holder():
        async with db.session_factory()() as other:
            await other.execute(select(JobPosting.id).limit(1))  # a writer session's first statement takes the turn
            holding.set()
            await let_go.wait()
            await other.rollback()

    held = asyncio.get_running_loop().create_task(holder())
    await holding.wait()

    async def run():
        async with db.session_factory()() as session:
            result = await sift.sift(session, OWNER, profile, now=NOW)
            await session.commit()
            return result

    filtering = asyncio.get_running_loop().create_task(run())
    await asyncio.sleep(0.3)
    assert not filtering.done(), "the filter waits for the turn"
    let_go.set()
    result = await asyncio.wait_for(filtering, 10)
    await held
    assert result["looked_at"] == 100 and len(result["candidates"]) == 50
    async with db.session_factory()() as session:
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
        assert all(p.filtered_at is not None for p in rows.values()), "every posting was judged"
        again = await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
    assert again["looked_at"] == 0 and again["candidates"] == []
