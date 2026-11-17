"""The owner's versions of the process files are read from the database (the split into a page program and an engine
program: the two share the database and no memory). A version one program saved is used by the other with no message
between them: the engine reads the versions before it begins a run. A run already going keeps the file it started with.
Synthetic data only."""

import pytest
from process_engine import versions
from process_engine.models import ProcInstance, ProcVersion

from app import db, owner_keys
from app.agents import how_routes, live_view, shipped_rule
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import SearchRun
from tests.support import OWNER, load_profile

ROUND = "jobsearch_round"


@pytest.fixture
async def seeded(client):
    """The first start: each shipped process file is kept as version 1."""
    async with db.session_factory()() as session:
        await shipped_rule.settle(session)
        await session.commit()
    return client


def renamed(xml: str, was: str = "Read the gates", now: str = "Read the owner's gates") -> str:
    assert f'name="{was}"' in xml
    return xml.replace(f'name="{was}"', f'name="{now}"')


async def saved_by_another_program(xml: str, number: int = 2, key: str = ROUND) -> None:
    """A version the other program saved: its row is committed and this program's process library is not told."""
    async with db.session_factory()() as session:
        session.add(ProcVersion(
            entity_id=owner_keys.entity_id(), process_key=key, number=number,
            file_hash=versions.file_hash_of(xml), xml=xml, kind="edit", wording="Say it plainly",
        ))
        await session.commit()


async def _start_search():
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER, started_by="manual", focus="banks")
        await session.commit()
        return started.id, started.instance_id


async def _finish_search(run_id) -> None:
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        await run.stage_report(session, row, [], done=True)
        await session.commit()


async def _instance(instance_id):
    async with db.session_factory()() as session:
        return await session.get(ProcInstance, instance_id)


# --- the engine ---------------------------------------------------------------------------------------------------


async def test_the_next_run_starts_on_a_version_another_program_saved(seeded):
    new = renamed(versions.shipped_xml(ROUND))
    await saved_by_another_program(new)
    assert versions.active_xml(ROUND, owner_keys.entity_id()) == versions.shipped_xml(ROUND), "the library was not told"

    _run, instance_id = await _start_search()
    assert (await _instance(instance_id)).file_hash == versions.file_hash_of(new)


async def test_a_run_already_going_keeps_its_file_when_another_program_saves_a_version(seeded):
    old_run, old_instance = await _start_search()
    before = (await _instance(old_instance)).file_hash
    assert before == versions.file_hash_of(versions.shipped_xml(ROUND))

    await saved_by_another_program(renamed(versions.shipped_xml(ROUND)))
    await _finish_search(old_run)
    assert (await _instance(old_instance)).file_hash == before


# --- the page ----------------------------------------------------------------------------------------------------


def lookups(monkeypatch) -> list:
    """Every owner whose versions are read from the database from here on (`versions.current_for`), in order."""
    asked: list = []
    real = versions.current_for

    async def watched(session, entity_id):
        asked.append(entity_id)
        return await real(session, entity_id)

    monkeypatch.setattr(versions, "current_for", watched)
    return asked


async def test_how_they_work_draws_a_version_another_program_saved(seeded):
    await saved_by_another_program(renamed(versions.shipped_xml(ROUND)))
    assert versions.active_xml(ROUND, owner_keys.entity_id()) == versions.shipped_xml(ROUND), "the library was not told"

    overview = (await seeded.get("/api/v1/harness")).json()
    shown = next(p for p in overview["processes"] if p["id"] == ROUND)
    assert shown["version"] == 2
    assert "Read the owner's gates" in [s["title"] for s in shown["steps"]]


async def test_the_live_run_view_draws_a_version_another_program_saved(seeded):
    await saved_by_another_program(renamed(versions.shipped_xml(ROUND)))
    async with db.read_session_factory()() as session:
        pages = await live_view._page_processes(session)
    assert "Read the owner's gates" in [s["title"] for s in pages[ROUND]["steps"].values()]


async def test_the_step_lists_read_the_versions_first(seeded, monkeypatch):
    """The two other readers of the tree (which steps send an agent something, and what one search was sent) go
    through the same door as the overview."""
    asked = lookups(monkeypatch)
    assert (await seeded.get("/api/v1/harness/steps/no-such-step/example")).status_code == 404
    assert asked == [owner_keys.entity_id()]


async def test_a_file_save_sizes_its_pair_from_the_stored_versions_without_the_process_library(seeded, monkeypatch):
    """The page reads the process files for the pair as the database holds them (`how.paired_names_stored`), so a
    version another program saved is seen with no message and the process library is not asked (asking it registers the
    engine's actions, which the page must not load)."""
    asked = lookups(monkeypatch)
    async with db.read_session_factory()() as session:
        await how_routes._paired_size(session, "instructions", "jobsearch.fixer")
    assert asked == []


async def test_an_edit_and_a_rollback_read_the_versions_first(seeded, monkeypatch):
    asked = lookups(monkeypatch)
    base = f"/api/v1/harness/processes/{ROUND}"
    edited = await seeded.post(
        f"{base}/edits", json={"xml": renamed(versions.shipped_xml(ROUND)), "why": "Say it plainly"})
    assert edited.status_code == 200
    assert asked == [owner_keys.entity_id()]
    back = await seeded.post(f"{base}/rollback", json={"to": 1, "why": "Back"})
    assert back.status_code == 200
    assert asked == [owner_keys.entity_id(), owner_keys.entity_id()]
