"""Where to look lists the job systems the registry has a runbook for, in the order they are asked (the generic-sites
design, Part 6): a copy that ships fewer runbooks lists fewer systems, and none whose terms deny the app. Every employer
and host is made up (example.test); no network."""

from app import db
from app.jobsearch import ats
from app.jobsearch.employers import core as employers
from app.sites import job_systems, loader, registry
from tests.support import OWNER, load_profile


async def _every_system_has_an_employer(session):
    await load_profile(session, OWNER)
    for key in ats.SYSTEMS:
        employer = await employers.find_or_make(session, OWNER, f"Employer {key}")
        employer.careers_url = f"https://{key}.example.test/jobs"
        employer.ats, employer.board = key, {}
    await session.flush()


async def test_a_copy_without_the_restricted_folder_lists_only_the_systems_whose_terms_allow_it_or_are_silent(client):
    async with db.session_factory()() as session:
        await _every_system_has_an_employer(session)
        await session.commit()
    shipped = loader.load(restricted_dir=loader.RESTRICTED_DIR / "absent")
    with registry.replaced(shipped):
        async with db.session_factory()() as session:
            keys = await job_systems.shown(session, OWNER)
    assert keys == [k for k in shipped.order() if k in ats.SYSTEMS]
    assert {shipped.verdict(h) for k in keys for h in shipped.get(k).hosts} <= {"allows", "silent"}  # pyright: ignore


async def test_the_systems_listed_are_those_the_runbooks_order_names_and_in_that_order(client):
    async with db.session_factory()() as session:
        await _every_system_has_an_employer(session)
        await session.commit()
    async with db.session_factory()() as session:
        assert await job_systems.shown(session, OWNER) == [k for k in registry.order() if k in ats.SYSTEMS]
