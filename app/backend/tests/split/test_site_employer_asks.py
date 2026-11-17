"""The engine's three asks for the site and employer routes: `site.probe` tests a site
from the engine, `site.regate` and `employer.regate` check the log and the roles again, and `employers.edit` can save a
gate change and leave its check to the engine. Every name here is made up."""

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import regate
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.work import engine_asks
from app.sites import read_probe, where
from app.sites.models import Site
from tests.support import OWNER, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

URL = "https://board.example.test/jobs"
SIGN_IN = "It asks for a sign-in."


def role(company: str, status: str, slug: str) -> Opportunity:
    return Opportunity(owner=OWNER, slug=slug, company=company, title="Lead", fit=3, origin="scout",
                       match_key=slug, status=status, found=date(2026, 11, 4))


async def _employer(name: str) -> uuid.UUID:
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, name)
        assert employer is not None  # a plain name always makes or finds one
        await session.flush()
        employer_id = employer.id
        await session.commit()
    return employer_id


async def _site() -> uuid.UUID:
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, URL, read_by="auto")
        site_id = site.id
        await session.commit()
    return site_id


def test_the_three_asks_are_registered_under_the_names_the_routes_use():
    assert {"site.probe", "site.regate", "employer.regate"} <= set(engine_asks._asks)


async def test_a_deferred_gate_is_saved_with_its_time_and_no_re_gate_runs(js, monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("the re-gate ran inside the save")

    monkeypatch.setattr(regate, "run", never)
    employer_id = await _employer("Initech")
    async with db.session_factory()() as session:
        edited = await employers.edit(session, OWNER, employer_id, {"gated": True, "gated_reason": "never again"},
                                      defer_gate_change=True)
        await session.commit()
    assert edited["gated"] is True and edited["gated_reason"] == "never again"
    assert edited["gated_at"] is not None
    assert edited["gate_changed"] is True and "said" not in edited


async def test_a_deferred_save_that_does_not_flip_the_gate_says_nothing_of_one(js):
    employer_id = await _employer("Initech")
    async with db.session_factory()() as session:
        edited = await employers.edit(session, OWNER, employer_id, {"industry": "Biotech"}, defer_gate_change=True)
        await session.commit()
    assert edited["industry"] == "Biotech"
    assert "gate_changed" not in edited and "said" not in edited


async def test_the_employer_ask_re_gates_writes_its_line_and_answers_with_it(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add(role("Initech", stages.SECOND_IMPRESSION, "a"))
        await session.flush()
        await employers.settle(session, OWNER)
        initech = await session.scalar(select(Employer).where(Employer.key == "initech"))
        assert initech is not None
        initech_id = initech.id
        await employers.edit(session, OWNER, initech_id, {"gated": True, "gated_reason": "never again"},
                             defer_gate_change=True)
        await session.commit()
    async with db.session_factory()() as session:
        answer = await engine_asks._asks["employer.regate"](
            session, OWNER, {"employer_id": str(initech_id), "by": "owner"})
        await session.commit()
    assert answer == {"said": "Re-checked 0 postings and 1 role: 1 newly stopped."}
    async with db.read_session_factory()() as reader:
        moved = await reader.scalar(select(Opportunity).where(Opportunity.slug == "a"))
    assert moved is not None
    assert (moved.status, moved.screened_out_reason) == (stages.GATED_OUT, "employer gated: never again")


async def test_the_employer_ask_for_an_employer_that_is_gone_answers_with_no_line(js):
    async with db.session_factory()() as session:
        answer = await engine_asks._asks["employer.regate"](
            session, OWNER, {"employer_id": str(uuid.uuid4()), "by": "owner"})
    assert answer == {"said": None}


async def test_the_site_regate_ask_checks_the_log_with_the_cause_it_is_given(js, monkeypatch):
    seen: list[tuple[str, str]] = []

    async def run(session, owner, *, cause, **kwargs):
        seen.append((owner, cause))
        return regate.Report(postings=2, roles=1, postings_stopped=1)

    monkeypatch.setattr(regate, "run", run)
    cause = "jobs.example.test was added to the sites you avoid"
    async with db.session_factory()() as session:
        answer = await engine_asks._asks["site.regate"](session, OWNER, {"cause": cause})
    assert seen == [(OWNER, cause)]
    assert answer == {"said": "Re-checked 2 postings and 1 role: 1 newly stopped."}


async def test_the_probe_ask_tests_the_site_in_the_engine_and_answers_what_was_found(js, monkeypatch):
    tested: list[str] = []

    async def settle(session, site, fetch=None):
        tested.append(site.url)
        await where.set_found(session, site, "browser", SIGN_IN)
        return read_probe.Found("browser", SIGN_IN)

    monkeypatch.setattr(read_probe, "settle", settle)
    site_id = await _site()
    async with db.session_factory()() as session:
        answer = await engine_asks._asks["site.probe"](session, OWNER, {"site_id": str(site_id)})
        await session.commit()
    assert len(tested) == 1
    assert answer == {"read": "browser", "note": SIGN_IN}
    async with db.read_session_factory()() as reader:
        site = await reader.get(Site, site_id)
    assert site is not None and (site.read_found, site.read_note) == ("browser", SIGN_IN)


async def test_the_probe_ask_for_a_site_that_is_gone_answers_with_nothing_found(js):
    async with db.session_factory()() as session:
        answer = await engine_asks._asks["site.probe"](session, OWNER, {"site_id": str(uuid.uuid4())})
    assert answer == {"read": None, "note": None}
