"""Changing how a process works (docs/PRD.md path 16.1 and 16.2): the owner edits a process on How they work, the
engine's own loader decides whether it can run, a save is the next version with no approval step, the next run uses it
and says which version it ran on, an earlier version can be restored, and a release that ships a changed file is
followed or the owner is asked. Synthetic data only."""

import shutil

import pytest
from process_engine import versions
from process_engine.models import ProcInstance
from sqlalchemy import select

from app import db
from app.agents import shipped_rule
from app.config import settings as base_settings
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import SearchRun
from app.models.work import Task
from tests.support import OWNER, load_profile

ROUND = "jobsearch_round"
SCREEN = "jobsearch_screen_role"


@pytest.fixture
async def seeded(client):
    """The first start: each shipped process file is kept as version 1."""
    await _settle()
    return client


async def _settle():
    async with db.session_factory()() as session:
        cards = await shipped_rule.settle(session)
        await session.commit()
    return cards


def process_url(key, tail):
    return f"/api/v1/harness/processes/{key}/{tail}"


def renamed(xml: str, was="Read the gates", now="Read the owner's gates") -> str:
    assert f'name="{was}"' in xml
    return xml.replace(f'name="{was}"', f'name="{now}"')


async def save(client, xml, why="Say it plainly", key=ROUND, **extra):
    return await client.post(
        process_url(key, "edits"), json={"xml": xml, "why": why, **extra})


async def current(client, key=ROUND):
    return (await client.get(process_url(key, "bpmn"))).json()


async def history(client, key=ROUND):
    return (await client.get(process_url(key, "versions"))).json()


async def _start_search():
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER, started_by="manual", focus="banks")
        await session.commit()
        return started.id, started.instance_id


async def _finish_search(run_id):
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        await run.stage_report(session, row, [], done=True)
        await session.commit()


async def _instance(instance_id):
    async with db.session_factory()() as session:
        return await session.get(ProcInstance, instance_id)


# --- the first start --------------------------------------------------------------------------------------------


async def test_the_shipped_files_are_kept_as_version_one_once(seeded):
    for key in versions.process_keys():
        got = await history(seeded, key)
        assert [v["number"] for v in got["versions"]] == [1], key
        assert got["versions"][0]["current"] and got["versions"][0]["source"] == "shipped"
        assert got["running"] == {"number": 1, "note": None}
    await _settle()  # a second start adds nothing
    assert [v["number"] for v in (await history(seeded))["versions"]] == [1]
    assert (await current(seeded))["xml"] == versions.shipped_xml(ROUND)


# --- checking and saving an edit ---------------------------------------------------------------------------------


async def test_a_file_the_engine_cannot_run_is_checked_in_plain_words_and_cannot_be_saved(seeded):
    xml = versions.shipped_xml(ROUND)
    good = await seeded.post(process_url(ROUND, "validate"), json={"xml": renamed(xml)})
    assert good.json()["ok"] is True and good.json()["problems"] == []
    assert good.json()["changes"] == ["Renamed “Read the gates” to “Read the owner's gates”."]

    broken = xml.replace('porter:action="jobs.open_run"', 'porter:action="jobs.not_there"')
    bad = (await seeded.post(process_url(ROUND, "validate"), json={"xml": broken})).json()
    assert bad["ok"] is False
    assert any("jobs.not_there" in p and "isn't registered" in p for p in bad["problems"])

    refused = await save(seeded, broken)
    assert refused.status_code == 422 and "jobs.not_there" in refused.json()["detail"]
    notxml = await save(seeded, "this is not a process")
    assert notxml.status_code == 422
    other = await save(seeded, versions.shipped_xml(SCREEN))  # another process's file under this process's name
    assert other.status_code == 422 and "different process" in other.json()["detail"]
    assert [v["number"] for v in (await history(seeded))["versions"]] == [1], "nothing was saved"


async def test_a_long_file_is_judged_by_the_engine_not_refused_for_its_length(seeded):
    """The owner's file is as long as the owner writes it: nothing caps its characters, and a file the engine can run
    is saved however long it is."""
    long = renamed(versions.shipped_xml(ROUND)) + "\n<!-- " + "a" * 600_000 + " -->\n"
    checked = await seeded.post(process_url(ROUND, "validate"), json={"xml": long})
    assert checked.status_code == 200 and checked.json()["ok"] is True
    saved = await save(seeded, long)
    assert saved.status_code == 200 and saved.json()["running"]["number"] == 2


async def test_a_save_needs_a_why_and_a_change(seeded):
    xml = versions.shipped_xml(ROUND)
    assert (await save(seeded, renamed(xml), why="  ")).status_code == 422
    same = await save(seeded, xml)
    assert same.status_code == 422 and "nothing is different" in same.json()["detail"]


async def test_a_save_is_the_next_version_with_its_why_who_and_time_and_no_approval(seeded):
    new = renamed(versions.shipped_xml(ROUND))
    got = await save(seeded, new, why="The first step reads the owner's gates")
    assert got.status_code == 200
    body = got.json()
    assert body["running"]["number"] == 2
    two, one = body["versions"]
    assert (two["number"], two["current"], two["source"]) == (2, True, "edit")
    assert two["why"] == "The first step reads the owner's gates" and two["by"] == "You" and two["at"]
    assert two["summary"] == ["Renamed “Read the gates” to “Read the owner's gates”."]
    assert (one["number"], one["current"], one["can_restore"]) == (1, False, True)
    # In force at once: the file that runs is the edit, and nothing waits in the Inbox
    now = await current(seeded)
    assert (now["number"], now["xml"]) == (2, new)
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Task))).all() == []
    # the page draws it
    overview = (await seeded.get(
        "/api/v1/harness")).json()
    shown = next(p for p in overview["processes"] if p["id"] == ROUND)
    assert shown["version"] == 2
    assert "Read the owner's gates" in [s["title"] for s in shown["steps"]]


async def test_a_save_made_on_an_older_version_is_refused(seeded):
    xml = versions.shipped_xml(ROUND)
    assert (await save(seeded, renamed(xml), based_on=1)).status_code == 200
    moved = await save(seeded, renamed(xml, now="Read the gates again"), based_on=1)
    assert moved.status_code == 409 and "changed while you were editing" in moved.json()["detail"]


# --- the next run, and the version a run ran on ---------------------------------------------------------------


async def test_the_next_run_uses_the_saved_version_and_records_it_while_an_earlier_run_keeps_its_own(seeded):
    old_run, old_instance = await _start_search()
    before = await _instance(old_instance)
    assert before.file_hash == versions.file_hash_of(versions.shipped_xml(ROUND))

    new = renamed(versions.shipped_xml(ROUND))
    assert (await save(seeded, new)).status_code == 200
    # the run going now finishes on the file it started with
    await _finish_search(old_run)
    assert (await _instance(old_instance)).file_hash == before.file_hash

    _run, instance_id = await _start_search()
    started = await _instance(instance_id)
    assert started.file_hash == versions.file_hash_of(new)

    base = "/api/v1/harness/runs"
    newer = (await seeded.get(f"{base}/{instance_id}")).json()
    assert (newer["version"], newer["version_now"]) == (2, 2)
    older = (await seeded.get(f"{base}/{old_instance}")).json()
    assert (older["version"], older["version_now"]) == (1, 2)
    listed = (await seeded.get(base)).json()["runs"]
    assert {r["id"]: r["version"] for r in listed} == {str(instance_id): 2, str(old_instance): 1}


# --- restoring --------------------------------------------------------------------------------------------------


async def test_restoring_an_earlier_version_saves_it_as_a_new_version_the_next_run_uses(seeded):
    shipped = versions.shipped_xml(ROUND)
    assert (await save(seeded, renamed(shipped))).status_code == 200

    got = await seeded.post(process_url(ROUND, "rollback"), json={"to": 1, "why": "Back"})
    assert got.status_code == 200
    three = got.json()["versions"][0]
    assert (three["number"], three["current"], three["restored"], three["why"]) == (3, True, 1, "Back")
    assert (await current(seeded))["xml"] == shipped
    assert [v["number"] for v in got.json()["versions"]] == [3, 2, 1], "the history is only added to"

    _run, instance_id = await _start_search()
    assert (await _instance(instance_id)).file_hash == versions.file_hash_of(shipped)

    again = await seeded.post(process_url(ROUND, "rollback"), json={"to": 3})
    assert again.status_code == 422 and "in use already" in again.json()["detail"]
    gone = await seeded.post(process_url(ROUND, "rollback"), json={"to": 9})
    assert gone.status_code == 422 and "no such version" in gone.json()["detail"]


async def test_an_earlier_version_can_be_looked_at(seeded):
    shipped = versions.shipped_xml(ROUND)
    assert (await save(seeded, renamed(shipped))).status_code == 200
    one = await seeded.get(process_url(ROUND, "versions/1"))
    assert one.json()["xml"] == shipped
    assert (await seeded.get(process_url(ROUND, "versions/7"))).status_code == 404


# --- a release that ships a changed file ------------------------------------------------------------------------


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A later release: the playbooks folder is a copy in which the search round's first step is renamed."""
    shutil.copytree(base_settings.playbooks_path, tmp_path / "playbooks")
    monkeypatch.setattr(base_settings, "playbooks_path", tmp_path / "playbooks")
    path = tmp_path / "playbooks" / "jobsearch" / "search_round.bpmn"
    path.write_text(renamed(path.read_text(encoding="utf-8"), now="Read the gates, newest release"), encoding="utf-8")
    return path


async def test_a_changed_shipped_file_becomes_the_next_version_while_the_owner_has_changed_nothing(seeded, release):
    assert await _settle() == 0
    got = await history(seeded)
    assert [(v["number"], v["source"]) for v in got["versions"]] == [(2, "shipped"), (1, "shipped")]
    assert (await current(seeded))["xml"] == release.read_text(encoding="utf-8")
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Task))).all() == []
    # the next run follows the release's file
    _run, instance_id = await _start_search()
    assert (await _instance(instance_id)).file_hash == versions.file_hash_of(release.read_text(encoding="utf-8"))


async def test_a_changed_shipped_file_replaces_the_owners_version_on_its_own_and_theirs_stays_in_the_history(
    seeded, release,
):
    # the owner's edit was made against the file as it first shipped
    original = release.read_text(encoding="utf-8").replace("Read the gates, newest release", "Read the gates")
    owners = renamed(original, now="The owner's own step name")
    assert (await save(seeded, owners, why="Mine")).status_code == 200

    assert await _settle() == 1
    assert (await current(seeded))["xml"] == release.read_text(encoding="utf-8"), "the release's file is in force"
    got = await history(seeded)
    assert [(v["number"], v["source"]) for v in got["versions"]] == [(3, "shipped"), (2, "edit"), (1, "shipped")]
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Task).where(Task.kind == "shipped_newer"))).all() == []  # the old kind
    assert await _settle() == 0, "nothing is newer now"

    # the owner can still edit afterwards
    again = renamed(release.read_text(encoding="utf-8"), was="Read the gates, newest release", now="My step again")
    assert (await save(seeded, again, why="Mine again")).status_code == 200
    assert (await current(seeded))["xml"] == again
