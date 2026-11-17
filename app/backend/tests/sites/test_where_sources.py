"""Where to look, by source (docs/PRD.md 30.3): an employer read through a built-in job system belongs to that system's
row and not to the sites to search (which keep everything read on its own); the roles on a site and on a job system are
the distinct roles that reached Found and were found there, because one of that source's postings became them or the
link they were found at points there (the site's host, subdomains and path; the job system's address), 0 when there are
none; and the lists behind those numbers, paged, newest first. Every employer, host and role is made up (example.test);
no network."""

import itertools
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import scoring
from app.jobsearch.missions import core as missions
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.profile import store as profile_store
from app.models.work import Task
from app.sites import job_systems, projects, registry, skills, where, where_view
from app.sites.models import Site
from app.timeutil import iso_utc
from tests.sites.builders import (
    LABEL_A,
    SYSTEM_A,
    SYSTEM_B,
    SYSTEM_C,
    UNREAD,
    board_link,
    board_of,
    posting_link,
    unread_link,
)
from tests.support import OWNER, load_profile

SITES = "/api/v1/sites"
START = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LEARNED = {"kind": "request", "method": "GET", "url": "https://borealis.example.test/api/jobs", "items": "data",
           "fields": {"title": "name", "url": "path", "ext_id": "id"}, "url_prefix": "https://borealis.example.test"}
_numbers = itertools.count(1)


async def _employer(session, name, *, system=None, board=None, watched=True):
    employer = await employers.find_or_make(session, OWNER, name)
    employer.careers_url = f"https://{name.lower()}.example.test/jobs"
    employer.ats, employer.board = system, board or {}
    employer.check_directly = watched
    await session.flush()
    return employer


async def _site_of(session, employer) -> Site:
    return (await session.scalars(select(Site).where(Site.employer_id == employer.id))).one()


async def _role(session, source_id, system, *, status="second_impression", hours=0, owner=OWNER, postings=1,
    title=None, fit=None,
                url=None, apply_url=None):
    """A role, `hours` after the start of the made-up month, read by `postings` postings under `source_id` (the
    employer, or the site) whose system is `system`, and found at `url` (or applied for at `apply_url`) when it has a
    link. `status=None`: a posting that never became a role."""
    n = next(_numbers)
    title = title or f"Role {n}"
    role = None
    if status is not None:
        role = Opportunity(owner=owner, slug=f"role-{n}", company="Made Up Co", title=title, status=status, fit=fit,
                           match_key=f"made up co|{title}|{n}".lower(), created_at=START + timedelta(hours=hours),
                           url=url, apply_url=apply_url)
        session.add(role)
        await session.flush()
    for i in range(postings):
        session.add(JobPosting(
            owner=owner, employer_id=source_id, ats=system, ext_id=f"{n}-{i}", company="Made Up Co", title=title,
            first_seen=START, last_seen=START, opportunity_id=role.id if role is not None else None))
    await session.flush()
    return role


async def _world(session) -> dict:
    """Watched employers in every standing: read through the first system (Acme), a learned recipe on one of its boards
    (Borealis), read in the owner's browser (Cirrus), a question for the owner (Delta), a system with no reader (Echo),
    a board of the first system the app can't address (Foxtrot) and a plain careers page (Golf); one that is not watched
    (Hotel); and a job board the owner added and a site to avoid."""
    await load_profile(session, OWNER)
    await roster.ensure_roster(session, OWNER)
    await missions.ensure(session, OWNER)
    made = {
        "acme": await _employer(session, "Acme", system=SYSTEM_A, board=board_of("acme")),
        "borealis": await _employer(session, "Borealis", system=SYSTEM_A, board=board_of("borealis")),
        "cirrus": await _employer(session, "Cirrus", system=SYSTEM_B, board=board_of("cirrus")),
        "delta": await _employer(session, "Delta", system=SYSTEM_A, board=board_of("delta")),
        "echo": await _employer(session, "Echo", system=UNREAD, board=board_of("echo")),
        "foxtrot": await _employer(session, "Foxtrot", system=SYSTEM_A),
        "golf": await _employer(session, "Golf"),
        "hotel": await _employer(session, "Hotel", system=SYSTEM_B, board=board_of("hotel"), watched=False),
    }
    await where.seed_employer_sites(session, OWNER)
    sites = {name: await _site_of(session, employer) for name, employer in made.items()}
    # Old: Hotel's site started off, unwatched. New (PRD 29.10): its reader switches it on, so the owner switches it
    # off.
    await where.switch(session, OWNER, sites["hotel"].id, False)
    # Borealis: its system's reader was replaced by a recipe the scout learned
    await skills.retire_skill(session, await skills.current_skill(session, sites["borealis"].id))
    await skills.save_skill(session, OWNER, sites["borealis"].id, LEARNED, None)
    await where.set_read_by(session, OWNER, sites["cirrus"].id, "browser")
    # Delta: the reader works, but its project asks the owner something
    project = await projects.create_project(session, OWNER, sites["delta"].id, "Read it", "Read it", created_by="you")
    session.add(Task(owner=OWNER, key="ask-1", title="Should it read the second page too?", kind="decision",
                     link_kind="project", link_id=project.id))
    sites["board"] = await where.add(session, OWNER, "https://board.example.test/jobs")
    sites["bad"] = await where.add(session, OWNER, "https://bad.example.test", list_name="avoid")
    await session.flush()
    return {"employers": made, "sites": sites}


def _by_label(rows):
    return {r["label"]: r for r in rows}


async def test_the_page_leaves_an_employer_read_through_its_job_system_to_the_system_and_keeps_every_other_site(client):
    async with db.session_factory()() as session:
        await _world(session)
        await session.commit()
    page = (await client.get(SITES)).json()
    sites = _by_label(page["sites"])
    assert set(sites) == {"Borealis", "Cirrus", "Delta", "Echo", "Foxtrot", "Golf", "board.example.test",
                          "bad.example.test"}, "Acme is read through its system, and Hotel is not watched"
    assert {label: row["state"] for label, row in sites.items()} == {
        "Borealis": "working", "Cirrus": "browser", "Delta": "needs_you", "Echo": "waiting", "Foxtrot": "waiting",
        "Golf": "waiting", "board.example.test": "waiting", "bad.example.test": "off"}
    assert sites["Borealis"]["skill"]["kind"] == "request", "a learned recipe is read on its own"
    names = {row["key"]: [n["name"] for n in row["employer_names"]] for row in page["job_systems"]}
    assert names == {SYSTEM_A: ["Acme"], SYSTEM_B: [], UNREAD: ["Echo"]}, (
        "the watched employers read through the first system; the second has only one read in the browser and one that "
        "is off; the third can't be read, so the watched employer stuck on it")
    unread = next(r for r in page["job_systems"] if r["key"] == UNREAD)
    assert unread["can_read"] is False and unread["employer_names"][0]["id"]


async def test_an_employer_comes_back_to_the_sites_to_search_when_its_job_system_is_switched_off(client):
    async with db.session_factory()() as session:
        world = await _world(session)
        await session.commit()
    off = (await client.patch(f"{SITES}/job-systems/{SYSTEM_A}", json={"on": False})).json()
    sites = _by_label(off["sites"])
    assert "Acme" in sites and sites["Acme"]["status"] == f"Off: {LABEL_A} is switched off"
    assert sites["Acme"]["employer_id"] == str(world["employers"]["acme"].id)
    first = next(r for r in off["job_systems"] if r["key"] == SYSTEM_A)
    assert [n["name"] for n in first["employer_names"]] == ["Acme"], "it is still the system's, only switched off"
    on = (await client.patch(f"{SITES}/job-systems/{SYSTEM_A}", json={"on": True})).json()
    assert "Acme" not in _by_label(on["sites"])


async def test_the_listing_still_has_every_site_and_a_job_system_row_has_the_new_shape(client):
    async with db.session_factory()() as session:
        await _world(session)
        listed = _by_label(await where_view.listing(session, OWNER))
        assert {"Acme", "Hotel"} <= set(listed), "the Employers screen needs every employer's site"
        await session.commit()
    rows = (await client.get(SITES)).json()["job_systems"]
    shape = {"key", "name", "can_read", "on", "status", "employer_names", "roles", "domains"}
    assert all(set(r) == shape for r in rows)
    assert [r["key"] for r in rows] == [*sorted([SYSTEM_A, SYSTEM_B]), UNREAD], "by name, the ones read first"
    assert all(set(n) == {"id", "name"} for r in rows for n in r["employer_names"])


async def test_a_sites_roles_are_the_distinct_roles_its_own_postings_became_that_reached_found(js):
    async with db.session_factory()() as session:
        world = await _world(session)
        employer, sites = world["employers"], world["sites"]
        # The first system's: a role two postings became counts once; one that moved on counts; one a gate stopped, and
        # a posting that never became a role, do not
        await _role(session, employer["acme"].id, SYSTEM_A, postings=2)
        await _role(session, employer["acme"].id, SYSTEM_A, status="applied")
        await _role(session, employer["acme"].id, SYSTEM_A, status="gated_out")
        await _role(session, employer["acme"].id, SYSTEM_A, status=None)
        # Borealis, read by its learned recipe: a leftover posting of the first system is the system's, not the site's
        await _role(session, employer["borealis"].id, "own_site")
        await _role(session, employer["borealis"].id, "own_site", status="gated_out")
        await _role(session, employer["borealis"].id, "own_site", status="rejected")
        await _role(session, employer["borealis"].id, SYSTEM_A)
        # read in the owner's browser, under the site's own id; a job board's, the same
        await _role(session, sites["cirrus"].id, "browser")
        await _role(session, sites["board"].id, "job_site")
        await _role(session, sites["board"].id, "job_site", status="ready_to_launch")
        await _role(session, sites["board"].id, "job_site", status="gated_out")
        # someone else's postings under the same ids are not the owner's
        await _role(session, sites["board"].id, "job_site", owner="someone else")
        await session.flush()
        roles = {row["label"]: row["roles"] for row in await where_view.listing(session, OWNER)}
        assert roles["Acme"] == 0, "its postings are the system's: its row has them"
        assert roles["Borealis"] == 2
        assert roles["Cirrus"] == 1 and roles["board.example.test"] == 2
        assert roles["bad.example.test"] == 0 and roles["Golf"] == 0
        systems = {row["key"]: row["roles"] for row in await job_systems.listing(session, OWNER)}
        assert systems == {SYSTEM_A: 3, SYSTEM_B: 0, UNREAD: 0}, (
            "Acme's two roles (one of them read twice, one that moved on) and the leftover at Borealis")


async def test_the_roles_of_a_site_are_paged_newest_first_and_leave_out_the_gated(client):
    async with db.session_factory()() as session:
        world = await _world(session)
        board = world["sites"]["board"]
        for hours in range(12):  # twelve roles, an hour apart; the odd ones have moved on
            await _role(session, board.id, "job_site", hours=hours, title=f"Board role {hours:02d}",
                        status="applied" if hours % 2 else stages.LANDING_ZONE)
        await _role(session, board.id, "job_site", hours=20, title="Stopped by a gate", status="gated_out")
        await _role(session, board.id, "job_site", hours=21, title="Never a role", status=None)
        board_id, bad_id = board.id, world["sites"]["bad"].id
        await session.commit()
    first = (await client.get(f"{SITES}/{board_id}/roles")).json()
    assert first["total"] == 12 and len(first["items"]) == 10, "ten at a time unless told otherwise"
    assert [i["title"] for i in first["items"]][:3] == ["Board role 11", "Board role 10", "Board role 09"]
    assert set(first["items"][0]) == {"id", "title", "company", "status", "fit", "fit_pct", "found_at"}
    assert [first["items"][0]["status"], first["items"][1]["status"]] == ["applied", stages.LANDING_ZONE], (
        "the stage as stored")
    assert first["items"][0]["found_at"] == iso_utc(START + timedelta(hours=11))
    assert first["items"][0]["company"] == "Made Up Co"
    rest = (await client.get(f"{SITES}/{board_id}/roles", params={"offset": 10, "limit": 5})).json()
    assert rest["total"] == 12 and [i["title"] for i in rest["items"]] == ["Board role 01", "Board role 00"]
    middle = (await client.get(f"{SITES}/{board_id}/roles", params={"offset": 2, "limit": 3})).json()
    assert [i["title"] for i in middle["items"]] == ["Board role 09", "Board role 08", "Board role 07"]
    past = (await client.get(f"{SITES}/{board_id}/roles", params={"offset": 40})).json()
    assert past == {"total": 12, "items": []}
    everything = (await client.get(f"{SITES}/{board_id}/roles", params={"limit": 500})).json()
    assert len(everything["items"]) == 12 and "Stopped by a gate" not in {i["title"] for i in everything["items"]}
    assert (await client.get(f"{SITES}/{bad_id}/roles")).json() == {"total": 0, "items": []}, "a site to avoid has none"


async def test_a_role_in_the_list_carries_its_fit_as_the_board_shows_it(client):
    """The list behind a row shows each role's fit chip as the Pipeline does: the stored fit, and that as a share of
    what the owner's asks allow, both empty for a role not scored yet."""
    async with db.session_factory()() as session:
        world = await _world(session)
        board = world["sites"]["board"]
        await _role(session, board.id, "job_site", hours=1, title="Scored", fit=3)
        await _role(session, board.id, "job_site", hours=2, title="Not scored yet")
        acme = world["employers"]["acme"]
        await _role(session, acme.id, SYSTEM_A, hours=3, title="Scored on a system", fit=4)
        span = scoring.bounds(await profile_store.current(session, OWNER))
        board_id = board.id
        await session.commit()
    by_title = {i["title"]: i for i in (await client.get(f"{SITES}/{board_id}/roles")).json()["items"]}
    assert by_title["Not scored yet"]["fit"] is None and by_title["Not scored yet"]["fit_pct"] is None
    assert by_title["Scored"]["fit"] == 3
    assert isinstance(by_title["Scored"]["fit_pct"], int), "the made-up profile has a scale to measure a fit against"
    assert by_title["Scored"]["fit_pct"] == scoring.percent_of(Opportunity(fit=3), span)
    system = (await client.get(f"{SITES}/job-systems/{SYSTEM_A}/roles")).json()["items"]
    assert [(i["fit"], i["fit_pct"]) for i in system] == [(4, scoring.percent_of(Opportunity(fit=4), span))]


async def test_the_roles_of_a_job_system_are_the_ones_it_read_newest_first(client):
    async with db.session_factory()() as session:
        world = await _world(session)
        acme = world["employers"]["acme"]
        await _role(session, acme.id, SYSTEM_A, hours=1, title="Oldest")
        await _role(session, acme.id, SYSTEM_A, hours=3, title="Newest")
        await _role(session, world["employers"]["delta"].id, SYSTEM_A, hours=2, title="Middle")
        await _role(session, acme.id, SYSTEM_A, hours=9, status="gated_out", title="Stopped")
        await _role(session, world["employers"]["cirrus"].id, SYSTEM_B, hours=5, title="Another system's")
        await _role(session, world["sites"]["board"].id, "job_site", hours=6, title="Another source's")
        await session.commit()
    body = (await client.get(f"{SITES}/job-systems/{SYSTEM_A}/roles")).json()
    assert body["total"] == 3 and [i["title"] for i in body["items"]] == ["Newest", "Middle", "Oldest"]
    paged = (await client.get(f"{SITES}/job-systems/{SYSTEM_A}/roles", params={"offset": 1, "limit": 1})).json()
    assert paged["total"] == 3 and [i["title"] for i in paged["items"]] == ["Middle"]
    second = (await client.get(f"{SITES}/job-systems/{SYSTEM_B}/roles")).json()
    assert [i["title"] for i in second["items"]] == ["Another system's"]
    assert (await client.get(f"{SITES}/job-systems/{SYSTEM_C}/roles")).json() == {"total": 0, "items": []}, (
        "a system no employer uses has none")
    counts = {r["key"]: r["roles"] for r in (await client.get(SITES)).json()["job_systems"]}
    assert counts[SYSTEM_A] == 3 and counts[SYSTEM_B] == 1, "the number is the length of the list"


async def test_the_roles_routes_refuse_what_there_is_none_of_and_what_is_out_of_range(client):
    async with db.session_factory()() as session:
        world = await _world(session)
        elsewhere = Site(owner="someone else", url="https://other.example.test/jobs", kind="job_board",
                         created_at=START)
        session.add(elsewhere)
        await session.flush()
        board_id, elsewhere_id = world["sites"]["board"].id, elsewhere.id
        await session.commit()
    assert (await client.get(f"{SITES}/{uuid.uuid4()}/roles")).status_code == 404
    assert (await client.get(f"{SITES}/{elsewhere_id}/roles")).status_code == 404, "another owner's site"
    missing = await client.get(f"{SITES}/job-systems/nonsense/roles")
    assert missing.status_code == 404 and missing.json()["detail"] == "no such job system"
    assert (await client.get(f"{SITES}/not-an-id/roles")).status_code == 422
    for params in ({"limit": 0}, {"limit": 501}, {"offset": -1}, {"limit": "many"}):
        assert (await client.get(f"{SITES}/{board_id}/roles", params=params)).status_code == 422, params
        assert (await client.get(f"{SITES}/job-systems/{SYSTEM_A}/roles", params=params)).status_code == 422, params
    assert (await client.get(f"{SITES}/{board_id}/roles", params={"limit": 1, "offset": 0})).status_code == 200


async def test_adding_a_site_needs_only_its_address_and_still_takes_a_kind(client):
    plain = await client.post(SITES, json={"url": "https://plain.example.test/jobs"})
    assert plain.status_code == 200
    assert (plain.json()["kind"], plain.json()["on"], plain.json()["roles"]) == ("job_board", True, 0)
    kinded = await client.post(SITES, json={"url": "https://kinded.example.test/jobs", "kind": "association"})
    assert kinded.status_code == 200 and kinded.json()["kind"] == "association"
    bad = await client.post(SITES, json={"url": "https://worse.example.test/jobs", "kind": "blog"})
    assert bad.status_code == 422
    avoid = await client.post(SITES, json={"url": "https://avoided.example.test", "list": "avoid"})
    assert avoid.status_code == 200 and avoid.json()["list"] == "avoid"


async def _linked(session, url, title, *, hours=0, **fields):
    """A role found at a link and no posting behind it (so only where it was found says where it belongs)."""
    return await _role(session, None, "browser", postings=0, url=url, title=title, hours=hours, **fields)


async def _titles(client, path, limit=500):
    return [i["title"] for i in (await client.get(path, params={"limit": limit})).json()["items"]]


async def test_a_role_found_at_a_sites_host_counts_for_that_site_and_for_its_subdomains(client):
    async with db.session_factory()() as session:
        await _world(session)
        site = await where.add(session, OWNER, "https://www.Careers.Hosted.Example.test")
        await _linked(session, "https://careers.hosted.example.test/job/1", "On the host", hours=1)
        await _linked(session, "https://WWW.Careers.Hosted.Example.test/Job/2?ref=1", "Spelled differently", hours=2)
        await _linked(session, "https://apply.careers.hosted.example.test/3", "On a subdomain", hours=3)
        await _linked(session, None, "Only an apply link", hours=4, apply_url="https://careers.hosted.example.test/a/4")
        await _linked(session, "https://hosted.example.test/5", "On the parent host", hours=5)
        await _linked(session, "https://xcareers.hosted.example.test/6", "On a host that merely ends alike", hours=6)
        await _linked(session, "https://elsewhere.example.test/7", "Found elsewhere, applied here", hours=7,
                      apply_url="https://careers.hosted.example.test/a/7")
        await _linked(session, "https://careers.hosted.example.test/8", "Stopped by a gate", hours=8,
                      status="gated_out")
        await _linked(session, "https://careers.hosted.example.test/9", "Someone else's", hours=9, owner="someone else")
        site_id = site.id
        listed = {row["id"]: row["roles"] for row in await where_view.listing(session, OWNER)}
        assert listed[str(site_id)] == 4 and type(listed[str(site_id)]) is int
        await session.commit()
    assert await _titles(client, f"{SITES}/{site_id}/roles") == [
        "Only an apply link", "On a subdomain", "Spelled differently", "On the host"], "newest first; the same four"
    body = (await client.get(f"{SITES}/{site_id}/roles", params={"limit": 2})).json()
    assert body["total"] == 4 and len(body["items"]) == 2, "the total is the count on the row"
    assert next(r for r in (await client.get(SITES)).json()["sites"] if r["id"] == str(site_id))["roles"] == 4


async def test_a_role_that_a_posting_and_its_link_both_place_at_a_site_counts_once(client):
    async with db.session_factory()() as session:
        world = await _world(session)
        board = world["sites"]["board"]  # https://board.example.test/jobs
        await _role(session, board.id, "job_site", title="Both ways", url="https://board.example.test/jobs/1", hours=2)
        await _role(session, board.id, "job_site", title="By posting only", hours=1)
        await _linked(session, "https://board.example.test/jobs/2", "By link only", hours=3)
        board_id = board.id
        await session.commit()
    assert await _titles(client, f"{SITES}/{board_id}/roles") == ["By link only", "Both ways", "By posting only"]
    listed = {r["id"]: r["roles"] for r in (await client.get(SITES)).json()["sites"]}
    assert listed[str(board_id)] == 3


async def test_a_site_on_a_shared_job_system_host_counts_only_the_roles_under_its_path(client):
    async with db.session_factory()() as session:
        await _world(session)
        host = board_link(SYSTEM_A, "x").removesuffix("/x")  # the system's shared host
        acme = await where.add(session, OWNER, f"{host}/AcmeCo")
        borealis = await where.add(session, OWNER, f"{host}/Borealis/")
        shared = await where.add(session, OWNER, host)
        await _linked(session, f"{host}/acmeco/jobs/1", "Acme one", hours=1)
        await _linked(session, f"{host}/AcmeCo", "Acme front page", hours=2)
        await _linked(session, f"{host}/ACMECO/", "Acme with a slash", hours=3)
        await _linked(session, f"{host}/AcmeCorp/jobs/2", "Another company's", hours=4)
        await _linked(session, f"{host}/borealis/jobs/3", "Borealis one", hours=5)
        await _linked(session, host.replace("https://", "https://jobs.") + "/AcmeCo/4", "Acme on a subdomain", hours=6)
        await _linked(session, f"{host}/other/5", "Neither", hours=7)
        ids = (acme.id, borealis.id, shared.id)
        await session.commit()
    acme_id, borealis_id, shared_id = ids
    assert await _titles(client, f"{SITES}/{acme_id}/roles") == [
        "Acme on a subdomain", "Acme with a slash", "Acme front page", "Acme one"]
    assert await _titles(client, f"{SITES}/{borealis_id}/roles") == ["Borealis one"]
    assert len(await _titles(client, f"{SITES}/{shared_id}/roles")) == 7, "no path: the whole host and its subdomains"
    listed = {r["id"]: r["roles"] for r in (await client.get(SITES)).json()["sites"]}
    assert (listed[str(acme_id)], listed[str(borealis_id)], listed[str(shared_id)]) == (4, 1, 7)


async def test_a_board_whose_address_has_a_path_counts_every_role_on_its_host(client):
    async with db.session_factory()() as session:
        await _world(session)
        board = await where.add(session, OWNER, "https://www.jobs.example.test/search")
        await _linked(session, "https://www.jobs.example.test/job-detail/1", "On the board", hours=1)
        await _linked(session, "https://jobs.example.test/listing/2", "Also on the board", hours=2)
        await _linked(session, "https://elsewhere.example.test/3", "Elsewhere", hours=3)
        board_id = board.id
        await session.commit()
    assert sorted(await _titles(client, f"{SITES}/{board_id}/roles")) == ["Also on the board", "On the board"]


#: A job board host: the registry's first when the install has a board runbook, a made-up one otherwise.
def _board_host():
    return (registry.board_hosts() or ("board.example.test",))[0]


async def test_a_role_at_a_job_systems_link_counts_for_that_system_without_a_posting_or_a_reader(client):
    async with db.session_factory()() as session:
        world = await _world(session)
        acme = world["employers"]["acme"]
        await _linked(session, posting_link(SYSTEM_A, "madeupco", 101), "First system link", hours=1)
        await _linked(session, None, "First system apply link", hours=2,
                      apply_url=posting_link(SYSTEM_A, "madeupco", 102))
        await _role(session, acme.id, SYSTEM_A, title="First system posting", hours=3)
        await _role(session, acme.id, SYSTEM_A, title="Posting and link", hours=4,
                    url=posting_link(SYSTEM_A, "acme", 103))
        await _linked(session, posting_link(SYSTEM_A, "madeupco", 104), "Stopped", hours=5,
                      status="gated_out")
        await _linked(session, posting_link(SYSTEM_B, "madeupco", 1), "Second system link", hours=6)
        await _linked(session, unread_link("madeupco"), "Unread system link, a system with no reader", hours=7)
        await _linked(session, "https://madeupco.example.test/jobs/1", "On no system", hours=8)
        await _linked(session, f"https://{_board_host()}/jobs/view/1", "On a job board", hours=9)
        await _linked(session, posting_link(SYSTEM_B, "madeupco", 2), "Someone else's", hours=10, owner="someone else")
        await session.commit()
    page = (await client.get(SITES)).json()["job_systems"]
    assert {r["key"]: r["roles"] for r in page} == {SYSTEM_A: 4, SYSTEM_B: 1, UNREAD: 1}
    assert await _titles(client, f"{SITES}/job-systems/{SYSTEM_A}/roles") == [
        "Posting and link", "First system posting", "First system apply link", "First system link"]
    assert await _titles(client, f"{SITES}/job-systems/{SYSTEM_B}/roles") == ["Second system link"]
    assert await _titles(client, f"{SITES}/job-systems/{UNREAD}/roles") == [
        "Unread system link, a system with no reader"]
    assert (await client.get(f"{SITES}/job-systems/{SYSTEM_C}/roles")).json() == {"total": 0, "items": []}


async def test_a_role_can_be_found_at_both_a_site_on_a_job_systems_host_and_that_system(client):
    async with db.session_factory()() as session:
        await _world(session)
        site = await where.add(session, OWNER, board_link(SYSTEM_A, "madeupco"))
        await _linked(session, posting_link(SYSTEM_A, "madeupco", 1), "At the site", hours=1)
        await _linked(session, posting_link(SYSTEM_A, "another", 2), "At the system only", hours=2)
        site_id = site.id
        await session.commit()
    assert await _titles(client, f"{SITES}/{site_id}/roles") == ["At the site"]
    assert await _titles(client, f"{SITES}/job-systems/{SYSTEM_A}/roles") == ["At the system only", "At the site"]


async def test_a_source_with_no_roles_says_zero_as_a_number_and_has_an_empty_list(client):
    async with db.session_factory()() as session:
        world = await _world(session)
        quiet = world["sites"]["board"]
        await _linked(session, "https://elsewhere.example.test/jobs/1", "Found elsewhere")
        quiet_id = quiet.id
        await session.commit()
    page = (await client.get(SITES)).json()
    assert all(type(r["roles"]) is int for r in page["sites"] + page["job_systems"])
    assert {r["label"]: r["roles"] for r in page["sites"]}["board.example.test"] == 0
    assert {r["key"]: r["roles"] for r in page["job_systems"]} == {SYSTEM_A: 0, SYSTEM_B: 0, UNREAD: 0}
    assert (await client.get(f"{SITES}/{quiet_id}/roles")).json() == {"total": 0, "items": []}
    assert (await client.get(f"{SITES}/job-systems/{SYSTEM_B}/roles")).json() == {"total": 0, "items": []}
