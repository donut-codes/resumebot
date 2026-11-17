"""Job systems on Where to look (docs/PRD.md 30.3): the services employers post on, each with a switch the owner can
turn off. The listing (the systems employers use, readable ones first, the ones with no reader saying why), the switch
saving the next version of the Settings document, a watched employer on a switched-off system saying so, and the
night's read skipping it. Every employer and host is made up (example.test); no network."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import actions, adapters, ats, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import record
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Settings
from app.sites import job_systems, where, where_view
from app.sites.models import Site
from tests.sites.builders import (
    LABEL_A,
    LABEL_UNREAD,
    SYSTEM_A,
    SYSTEM_B,
    SYSTEM_C,
    UNREAD,
    board_of,
)
from tests.support import OWNER, load_profile


async def _employer(session, name, *, system=None, board=None, watched=False):
    employer = await employers.find_or_make(session, OWNER, name)
    employer.careers_url = f"https://{name.lower()}.example.test/jobs"
    employer.ats, employer.board = system, board or {}
    employer.check_directly = watched
    await session.flush()
    return employer


async def _posting(session, employer, ext_id: str, *, became_a_role: bool, system: str = SYSTEM_A,
                   status: str = stages.LANDING_ZONE):
    """A posting read at the employer, and the role it became (at `status`) when it did."""
    now = datetime.now(UTC)
    role = None
    if became_a_role:
        role = Opportunity(owner=OWNER, slug=f"{employer.name}-{ext_id}".lower(), company=employer.name,
                           title=f"Role {ext_id}", status=status, match_key=f"{employer.name}|role {ext_id}".lower(),
                           employer_id=employer.id, created_at=now)
        session.add(role)
        await session.flush()
    session.add(JobPosting(
        owner=OWNER, employer_id=employer.id, ats=system, ext_id=ext_id, company=employer.name, title=f"Role {ext_id}",
        first_seen=now, last_seen=now, opportunity_id=role.id if role is not None else None))
    await session.flush()


async def _world(session):
    """Employers on systems the app reads or not, one on none, and the missions (the read at 1:00 AM)."""
    await load_profile(session, OWNER)
    await roster.ensure_roster(session, OWNER)
    await missions.ensure(session, OWNER)
    acme = await _employer(session, "Acme", system=SYSTEM_A, board=board_of("acme"), watched=True)
    borealis = await _employer(session, "Borealis", system=SYSTEM_A, board=board_of("borealis"))
    cirrus = await _employer(session, "Cirrus", system=SYSTEM_B, board=board_of("cirrus"), watched=True)
    await _employer(session, "Vale", system=UNREAD)
    await _employer(session, "Plain")
    await where.seed_employer_sites(session, OWNER)
    # Old: Borealis's site started off, unwatched. New (PRD 29.10): its reader switches it on, so the owner switches it
    # off here, which is what leaves it unwatched for the tests below.
    borealis_site = (await session.scalars(select(Site).where(Site.employer_id == borealis.id))).one()
    await where.switch(session, OWNER, borealis_site.id, False)
    return {"acme": acme, "borealis": borealis, "cirrus": cirrus}


def _by_key(rows):
    return {r["key"]: r for r in rows}


def test_the_settings_document_keeps_only_known_job_systems_once_each_in_order():
    assert Settings().job_systems_off == []
    assert Settings(job_systems_off=[SYSTEM_B, SYSTEM_A, SYSTEM_B]).job_systems_off == sorted([SYSTEM_A, SYSTEM_B])
    with pytest.raises(ValidationError, match="not a job system: nonsense"):
        Settings(job_systems_off=["nonsense"])


async def test_the_listing_has_the_systems_employers_use_readable_ones_first(js):
    async with db.session_factory()() as session:
        world = await _world(session)
        await _posting(session, world["acme"], "1", became_a_role=True)
        await _posting(session, world["borealis"], "2", became_a_role=True)
        await _posting(session, world["borealis"], "3", became_a_role=False)
        await _posting(session, world["cirrus"], "4", became_a_role=True, system=SYSTEM_B)
        await _posting(session, world["acme"], "5", became_a_role=True, status="gated_out")
        rows = await job_systems.listing(session, OWNER)
        first, second = sorted([SYSTEM_A, SYSTEM_B])
        assert [r["key"] for r in rows] == [first, second, UNREAD], (
            "only the systems an employer posts on; the ones with no reader after the ones with")
        row_a = next(r for r in rows if r["key"] == SYSTEM_A)
        assert row_a == {"key": SYSTEM_A, "name": LABEL_A, "domains": list(ats.SYSTEMS[SYSTEM_A].domains),
                         "can_read": True, "on": True,
                           "status": "Read nightly at 1:00 AM",
                           "employer_names": [{"id": str(world["acme"].id), "name": "Acme"}], "roles": 2}, (
            "Acme is watched, Borealis is not; two postings became roles that reached Found, one did not, and one "
            "became a role a gate stopped")
        row_b = next(r for r in rows if r["key"] == SYSTEM_B)
        assert ([n["name"] for n in row_b["employer_names"]], row_b["roles"]) == (["Cirrus"], 1)
        assert all("employers" not in r for r in rows), "the count is gone: the page lists the names"
        unread = rows[2]
        assert (unread["can_read"], unread["on"], unread["status"]) == (
            False, False, "Can't be read: no reader has been built for it yet")
        assert unread["roles"] == 0


async def test_a_readable_system_says_the_reading_mission_is_paused_while_it_is(js):
    async with db.session_factory()() as session:
        await _world(session)
        await missions.pause(session, await missions.get(session, OWNER, record.READ), by="Tester")
        rows = _by_key(await job_systems.listing(session, OWNER))
        assert rows[SYSTEM_A]["status"] == "Waits: the reading mission is paused"
        assert rows[UNREAD]["status"].startswith("Can't be read"), "no one reads it, so the mission is not its business"


async def test_the_switch_saves_the_next_settings_version_keeps_the_rest_and_a_repeat_saves_nothing(client):
    async with db.session_factory()() as session:
        await _world(session)
        before = (await profile_store.current(session, OWNER)).settings
        versions = len(await profile_store.history(session, OWNER, "settings"))
        await session.commit()
    page = (await client.get("/api/v1/sites")).json()
    assert [r["key"] for r in page["job_systems"]] == [*sorted([SYSTEM_A, SYSTEM_B]), UNREAD]
    assert all(r["on"] for r in page["job_systems"] if r["can_read"])

    off = await client.patch(f"/api/v1/sites/job-systems/{SYSTEM_A}", json={"on": False})
    assert off.status_code == 200
    first = _by_key(off.json()["job_systems"])[SYSTEM_A]
    assert (first["on"], first["status"], [n["name"] for n in first["employer_names"]]) == (
        False, "Off", ["Acme"]), "off is the owner's word; the employer is still watched"
    async with db.session_factory()() as session:
        after = (await profile_store.current(session, OWNER)).settings
        history = await profile_store.history(session, OWNER, "settings")
    assert after.job_systems_off == [SYSTEM_A]
    assert after.model_copy(update={"job_systems_off": []}) == before, "every other field is as it was"
    assert len(history) == versions + 1
    assert (history[0]["saved_by"], history[0]["why"]) == ("owner", f"switched {LABEL_A} off")

    again = await client.patch(f"/api/v1/sites/job-systems/{SYSTEM_A}", json={"on": False})
    assert again.status_code == 200
    async with db.session_factory()() as session:
        assert len(await profile_store.history(session, OWNER, "settings")) == versions + 1, "nothing changed"

    on = await client.patch(f"/api/v1/sites/job-systems/{SYSTEM_A}", json={"on": True})
    assert _by_key(on.json()["job_systems"])[SYSTEM_A]["on"] is True
    async with db.session_factory()() as session:
        assert (await profile_store.current(session, OWNER)).settings.job_systems_off == []
        assert len(await profile_store.history(session, OWNER, "settings")) == versions + 2


async def test_a_system_with_no_reader_has_no_switch_and_one_that_does_not_exist_is_not_found(client):
    async with db.session_factory()() as session:
        await _world(session)
        await session.commit()
    refused = await client.patch(f"/api/v1/sites/job-systems/{UNREAD}", json={"on": False})
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        f"{LABEL_UNREAD} can't be read (no reader has been built for it yet), so it has no switch")
    missing = await client.patch("/api/v1/sites/job-systems/nonsense", json={"on": False})
    assert missing.status_code == 404 and missing.json()["detail"] == "no such job system"
    async with db.session_factory()() as session:
        assert (await profile_store.current(session, OWNER)).settings.job_systems_off == []


async def test_a_switched_off_system_stays_listed_and_can_be_switched_back_on_when_no_employer_uses_it(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        assert await job_systems.listing(session, OWNER) == []
        row = await job_systems.switch(session, OWNER, SYSTEM_C, False)
        assert (row["key"], row["on"], row["status"], row["employer_names"]) == (SYSTEM_C, False, "Off", [])
        assert [r["key"] for r in await job_systems.listing(session, OWNER)] == [SYSTEM_C]
        await job_systems.switch(session, OWNER, SYSTEM_C, True)
        assert await job_systems.listing(session, OWNER) == []


async def test_a_watched_employer_on_a_switched_off_system_comes_back_to_the_sites_to_search_saying_so(client):
    async with db.session_factory()() as session:
        await _world(session)
        await session.commit()
    page = (await client.get("/api/v1/sites")).json()
    assert page["sites"] == [], "the watched employers are read through their systems: the system rows speak for them"
    assert {r["key"]: [n["name"] for n in r["employer_names"]] for r in page["job_systems"]}[SYSTEM_A] == ["Acme"]
    off = await client.patch(f"/api/v1/sites/job-systems/{SYSTEM_A}", json={"on": False})
    sites = {r["label"]: r for r in off.json()["sites"]}
    assert set(sites) == {"Acme"}, "Cirrus's system is still on"
    assert sites["Acme"]["status"] == f"Off: {LABEL_A} is switched off"
    assert (sites["Acme"]["on"], sites["Acme"]["state"]) == (True, "working"), (
        "the owner's switch on the site is untouched")
    on = await client.patch(f"/api/v1/sites/job-systems/{SYSTEM_A}", json={"on": True})
    assert on.json()["sites"] == [], "and back on, the system has it again"


async def test_a_watched_employers_own_site_says_its_job_system_is_switched_off(js):
    async with db.session_factory()() as session:
        acme = (await _world(session))["acme"].id
        await session.commit()
    async with db.session_factory()() as session:
        assert "switched off" not in (await employers.detail(session, OWNER, acme))["site"]["status"]
    async with db.session_factory()() as session:
        await job_systems.switch(session, OWNER, SYSTEM_A, False)
        await session.commit()
    async with db.session_factory()() as session:
        site = (await employers.detail(session, OWNER, acme))["site"]
        assert (site["on"], site["state"], site["status"]) == (True, "working", f"Off: {LABEL_A} is switched off"), (
            "the watch line says the system is off, not that the site is read nightly")


# --- the job systems whose terms deny the app: off and hidden on a new install (docs/PRD.md path 55) -----------------

def test_the_systems_a_new_install_starts_with_off_are_the_ones_the_runbooks_name_and_each_has_a_reader():
    assert set(ats.NOT_OFFERED) <= set(adapters.READERS), "each is a system with a reader, so it has a switch"


#: A mission this install has not made: the status words do not look at it when a system is off.
NOT_MADE = missions.Standing(made=False, on=False, schedule={}, when="", waits="", settings={}, last_run_at=None)


def test_a_switched_off_job_system_is_said_in_every_state_of_the_site_but_off():
    for state in (where.WAITING, where.LEARNING, where.WORKING, where.NEEDS_YOU, where.STOPPED, where.BROWSER):
        said = where_view._status(state, NOT_MADE, NOT_MADE, None, LABEL_A)
        assert said == f"Off: {LABEL_A} is switched off", state
    off = where_view._status(where.OFF, NOT_MADE, NOT_MADE, None, LABEL_A)
    assert off == "", "a site that is off says nothing"


# --- the night's read -----------------------------------------------------------------------------------------------


@dataclass
class Listing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class Board:
    asked: list = field(default_factory=list)

    async def read(self, fetch, board):
        self.asked.append(board["slug"])
        return Listing([{"ext_id": "1", "title": f"Role at {board['slug']}", "location": "Remote",
                         "work_mode": "remote", "comp": None, "comp_min": None, "comp_max": None, "posted": None,
                         "url": f"https://jobs.example.test/{board['slug']}/1", "apply_url": None, "text": "About it.",
                         "department": None}])

    async def detail(self, fetch, board, posting):
        return {}


def _mock_fetch():
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<p>Jobs</p>")

    return lambda: Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)


async def test_the_read_skips_the_employers_on_a_switched_off_system_and_reads_them_again_when_on(js, monkeypatch):
    board = Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {SYSTEM_A: board, SYSTEM_B: board})
    monkeypatch.setattr(polite, "make", _mock_fetch())
    async with db.session_factory()() as session:
        await _world(session)
        read = await actions.read_boards(session, None, {"owner": OWNER})
        assert sorted(board.asked) == ["acme", "cirrus"] and read["employers"] == 2
        board.asked.clear()
        await job_systems.switch(session, OWNER, SYSTEM_A, False)
        read = await actions.read_boards(session, None, {"owner": OWNER})
        assert board.asked == ["cirrus"] and read["employers"] == 1, "Acme posts on the first system, which is off"
        board.asked.clear()
        await job_systems.switch(session, OWNER, SYSTEM_A, True)
        await actions.read_boards(session, None, {"owner": OWNER})
        assert sorted(board.asked) == ["acme", "cirrus"]
