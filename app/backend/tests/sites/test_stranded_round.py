"""A project round that started under the file that asked the owner about a plan (the step PR-7, its card
`plan_approval` and PR-7A, all removed in 1.166.0), still waiting at that step when the app was updated. Its file no
longer loads, so the start-up sweep (`site_process.engine().move_stranded`) moves it onto the current file at the
gateway PR-7N, which starts the plan (PR-6S). Before this fix the step raised when the plan was no longer awaiting
approval (the owner had declined it), the run stayed stranded and the error came back at every start. Synthetic data
only."""

import hashlib
import uuid

from process_engine.models import CANCELLED, ProcFile, ProcHistory, ProcInstance, ProcToken, ProcWait
from sqlalchemy import func, select

from app import db
from app.sites import completion, process, round_process
from app.sites.models import Plan, PlanStatus, PlanTask, Project
from tests.sites.builders import ROUND, item, new_project, propose, step_of
from tests.support import OWNER

#: The round's file as it was before the owner's plan question went: it names an action that is not registered now, so
#: it does not load.
BEFORE = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:porter="http://porter.local/bpmn/ext"
                  id="Definitions_before" targetNamespace="http://porter.local/bpmn">
  <bpmn:process id="Process_before" porter:key="project_round" isExecutable="true">
    <bpmn:serviceTask id="PR-7A" porter:action="round.carry_out" porter:output="decided"/>
  </bpmn:process>
</bpmn:definitions>
"""


async def strand_at_the_owners_question(session, project: Project, plan: Plan) -> uuid.UUID:
    """What the update left: the project's round on the old file, its token and its wait at PR-7 (a step the current
    file has no more), having passed the gateway PR-7N, which the current file still has."""
    engine = process.engine()
    for earlier in await round_process._open_runs(session, project):
        await engine.cancel(session, earlier.id, "set aside to stand in for the run the update stranded")
    file_hash = hashlib.sha256(BEFORE.encode()).hexdigest()
    session.add(ProcFile(file_hash=file_hash, process_key=ROUND, xml=BEFORE))
    facts = await round_process.plan_facts(session, project, plan)
    instance = ProcInstance(
        process_key=ROUND, file_hash=file_hash, behaviour_hash=None, status="waiting", mode="live",
        subject=round_process.subject_of(OWNER, project.id),
        data={"owner": OWNER, "project": str(project.id), "plan": facts,
              "start": {"unreadable": False, "needs_browser": False, "why": ""}})
    session.add(instance)
    await session.flush()
    token = ProcToken(instance_id=instance.id, element_id="PR-7", state="waits")
    session.add(token)
    await session.flush()
    session.add(ProcWait(instance_id=instance.id, token_id=token.id, element_id="PR-7", kind="user", status="open"))
    for seq, (before, now) in enumerate([(None, "PRT-6"), ("PRT-6", "PR-7N"), ("PR-7N", "PR-7")], start=1):
        session.add(ProcHistory(instance_id=instance.id, seq=seq, kind="move", from_element=before, to_element=now))
    await session.flush()
    return instance.id


async def test_a_round_stranded_at_the_owners_question_for_a_declined_plan_is_ended_when_it_is_moved(js):
    async with db.session_factory()() as session:
        project = await new_project(session)
        plan = Plan(owner=OWNER, project_id=project.id, version=1, status=PlanStatus.declined,
                    summary="Read the site", method="Fetch the pages", decision_reason="Don't do this.")
        session.add(plan)
        await session.flush()
        await completion.stop_project(session, project, "paused by the owner")
        instance_id = await strand_at_the_owners_question(session, project, plan)
        await session.commit()
    async with db.session_factory()() as session:
        assert await process.engine().move_stranded(session) == 1
        await session.commit()
    async with db.session_factory()() as session:
        instance = await session.get(ProcInstance, instance_id)
        assert instance.status == CANCELLED
        assert instance.file_hash == process.library().get(ROUND).file_hash
        assert "declined" in (instance.error or ""), "the run says why it ended"
        assert await session.scalar(select(ProcToken.id).where(
            ProcToken.instance_id == instance_id, ProcToken.state != "done")) is None, "no token is left going"
        assert await session.scalar(select(ProcWait.id).where(
            ProcWait.instance_id == instance_id, ProcWait.status == "open")) is None, "no wait is left open"
        plan = await session.scalar(select(Plan).where(Plan.project_id == project.id))
        assert plan.status == PlanStatus.declined, "nothing about the plan changed"
        assert await session.scalar(select(func.count()).select_from(PlanTask).where(
            PlanTask.project_id == project.id, PlanTask.plan_id == plan.id)) == 0, "a declined plan makes no tasks"
        project = await session.get(Project, project.id)
        assert project.stopped_at is not None, "the project is still paused"
        assert await process.engine().move_stranded(session) == 0, "once: nothing is left to move"
    async with db.session_factory()() as session:
        project = await session.get(Project, project.id)
        assert await completion.resume_project(session, project) is True, "Resume asks for a fresh plan, as ever"
        assert await step_of(session, project) == round_process.SCOUT_PLANS, "and a new round waits for it"


async def test_a_round_stranded_at_the_owners_question_for_an_approved_plan_goes_on_without_deciding_again(js):
    async with db.session_factory()() as session:
        project = await new_project(session)
        plan = (await propose(session, project, item("list"))).plan
        assert plan.status == PlanStatus.approved
        tasks = await session.scalar(select(func.count()).select_from(PlanTask).where(PlanTask.plan_id == plan.id))
        instance_id = await strand_at_the_owners_question(session, project, plan)
        await session.commit()
    async with db.session_factory()() as session:
        assert await process.engine().move_stranded(session) == 1
        await session.commit()
    async with db.session_factory()() as session:
        instance = await session.get(ProcInstance, instance_id)
        where = await process.engine().where_is(session, instance_id)
        assert instance.status == "waiting" and where["step"] == round_process.DO_ITEMS, "it is at its items"
        assert instance.file_hash == process.library().get(ROUND).file_hash
        assert (await session.get(Plan, plan.id)).status == PlanStatus.approved
        assert await session.scalar(
            select(func.count()).select_from(PlanTask).where(PlanTask.plan_id == plan.id)) == tasks, "no tasks twice"
