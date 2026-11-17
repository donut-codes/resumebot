"""A search that was waiting at the retired title pass or readers' step when the app was updated: the round's file of
that time names actions the app no longer has, so it no longer loads, and the start-up sweep
(`process.engine().move_stranded`) moves the run onto the current file, at the scout's step. The earlier releases'
round files kept as versions (nothing the owner saved) no longer load either, and the release's own file follows them
at start-up. Synthetic data only."""

import hashlib

import pytest
from process_engine import versions
from process_engine.models import ProcFile, ProcInstance, ProcToken, ProcVersion, ProcWait
from sqlalchemy import select

from app import db
from app.agents import edit_process, shipped_rule
from app.jobsearch import actions, process
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import SearchRun
from tests.support import OWNER, load_profile

#: The round's file as it was before the steps went: it names two actions that are not registered now.
BEFORE = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:porter="http://porter.local/bpmn/ext"
                  id="Definitions_before" targetNamespace="http://porter.local/bpmn">
  <bpmn:process id="Process_before" porter:key="jobsearch_round" isExecutable="true">
    <bpmn:serviceTask id="Plan_titles" porter:action="jobs.plan_titles" porter:output="titling"/>
    <bpmn:serviceTask id="Plan_reads" porter:action="jobs.plan_reads" porter:output="reading"/>
  </bpmn:process>
</bpmn:definitions>
"""


def test_the_actions_of_the_retired_steps_are_gone_so_a_round_of_that_time_does_not_load():
    assert not {"jobs.plan_titles", "jobs.plan_reads"} & set(actions.registry.names())
    assert {"Titles_judge", "Readers_read", "Plan_titles", "Plan_reads"}.isdisjoint(
        process.library().get(process.ROUND).elements)


@pytest.mark.parametrize("step", ["Titles_judge", "Readers_read"])
async def test_a_search_waiting_at_a_retired_step_is_moved_to_the_scouts_step_when_the_app_starts(js, step):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER, started_by="manual")
        instance_id = started.instance_id
        # As it stood before the update: on the old file, its token and its wait at the step that is gone.
        file_hash = hashlib.sha256(BEFORE.encode()).hexdigest()
        session.add(ProcFile(file_hash=file_hash, process_key=process.ROUND, xml=BEFORE))
        instance = await session.get(ProcInstance, instance_id)
        instance.file_hash, instance.behaviour_hash = file_hash, None
        token = (await session.scalars(select(ProcToken).where(
            ProcToken.instance_id == instance_id, ProcToken.state != "done"))).one()
        wait = (await session.scalars(select(ProcWait).where(
            ProcWait.instance_id == instance_id, ProcWait.status == "open"))).one()
        token.element_id = wait.element_id = step
        await session.commit()
    async with db.session_factory()() as session:
        assert await process.engine().move_stranded(session) == 1
        await session.commit()
    async with db.session_factory()() as session:
        search = await session.get(SearchRun, started.id)
        assert await run.waiting_steps(session, search) == [process.SCOUT_STEP]
        instance = await session.get(ProcInstance, instance_id)
        assert instance.file_hash == process.library().get(process.ROUND).file_hash and instance.status == "waiting"
        assert search.status == "open", "the search goes on: it is the scout's turn"
        assert await process.engine().move_stranded(session) == 0, "once"


async def test_the_round_file_of_an_earlier_release_kept_as_version_one_is_followed_by_this_releases_at_start_up(js):
    """Nothing the owner saved: version 1 is the file as an earlier release shipped it (it names the retired actions, so
    it no longer loads). At start-up this release's file is saved as the next version, and a new search starts on it."""
    entity = edit_process.entity()
    async with db.session_factory()() as session:
        session.add(ProcVersion(entity_id=entity, process_key=process.ROUND, number=1, kind=edit_process.SHIPPED,
                                file_hash=versions.file_hash_of(BEFORE), xml=BEFORE,
                                wording="The file as it first shipped."))
        await session.commit()
    async with db.session_factory()() as session:
        await versions.refresh(session)
        assert await shipped_rule.settle(session) == 0, "followed on its own: nothing for the owner to settle"
        await session.commit()
    async with db.session_factory()() as session:
        saved = await versions.versions_of(session, entity, process.ROUND)
        assert [(row.number, row.kind) for row in saved] == [(1, edit_process.SHIPPED), (2, edit_process.SHIPPED)]
        running = await versions.running(session, entity, process.ROUND)
        assert running.number == 2 and running.xml == versions.shipped_xml(process.ROUND) and not running.skipped
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER, started_by="manual")
        instance = await session.get(ProcInstance, started.instance_id)
        assert instance.file_hash == versions.file_hash_of(versions.shipped_xml(process.ROUND))
