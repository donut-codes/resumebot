"""The site project round as a process (docs/PRD.md path 29): `playbooks/sites/project.round.bpmn` is the round and the
engine runs it for every project (`sites/round_process.py`); Do one plan item and Check one item are its calls.

There is an owner, a site and the scout. What is checked, end to end
through the code that owns each event: a project opens and its round starts on the sites library; a plan is proposed and
starts without asking; items are handed out and end done or off plan; the checks are made and run by `site.check` (a
stand-in here), the report is written and Act is handed out; Act with an accepted report closes the project; Act with an
iterate report asks for the next plan, which carries the lessons; a site that cannot be read asks the owner nothing; and
the round budget stops endless rounds. Synthetic data only."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from process_engine import live
from process_engine.models import COMPLETED, ProcHistory, ProcInstance
from sqlalchemy import select

from app import db
from app.agents import roster
from app.models.work import Notification, Task
from app.sites import (
    agent_calls,
    asks,
    autostart,
    check_flow,
    completion,
    escalation,
    failures,
    lessons,
    process,
    projects,
    records,
    round_process,
    site_check,
    task_context,
)
from app.sites.models import (
    Lesson,
    PlanItemAction,
    PlanStatus,
    PlanTask,
    PlanTaskMode,
    PlanTaskStatus,
    Project,
    ReviewReport,
    Site,
    SiteSkill,
)
from tests.builders import mission_of
from tests.sites.builders import (
    CHECK,
    DO,
    ROUND,
    SUMMARY,
    child_of,
    first_round,
    item,
    last_end,
    new_project,
    new_site,
    propose,
    revise,
    run_items,
    run_of,
    run_of_any,
    step_of,
    take,
    trail,
)
from tests.support import OWNER

# --- the files and the library ---------------------------------------------------------------------------------


async def test_the_site_processes_are_a_library_of_their_own_and_every_action_they_name_is_registered(js):
    library = process.library()
    assert library.keys() == [CHECK, DO, ROUND]
    assert set(round_process.registry.names()) == {
        "round.assemble_task", "round.start_plan", "round.make_checks", "round.write_report",
        "round.next_plan", "round.finish", "round.retry_planner", "round.turn_site_off", "round.hand_to_browser",
        "item.hand_out", "item.read", "check.make", "site.check",
    }
    from app.jobsearch import process as jobsearch_process

    assert set(jobsearch_process.library().keys()).isdisjoint(library.keys())
    assert all(live.drivable(key) is not None for key in (ROUND, DO, CHECK))


# --- opening ---------------------------------------------------------------------------------------------------


async def test_a_project_opens_and_its_round_starts_on_the_sites_library_waiting_for_the_scouts_plan(js):
    async with db.session_factory()() as session:
        project = await new_project(session)
        run = await run_of(session, project)
        assert (run.process_key, run.subject) == (ROUND, f"round:{OWNER}:{project.id}")
        assert await step_of(session, project) == "PR-4"
        start = await session.scalar(
            select(ProcHistory.to_element).where(ProcHistory.instance_id == run.id, ProcHistory.kind == "start"))
        assert start == "PRT-2"  # the owner started it
        planning = await session.scalar(select(PlanTask).where(PlanTask.project_id == project.id))
        assert (planning.action, planning.mode, planning.status) == (
            PlanItemAction.plan, PlanTaskMode.plan, PlanTaskStatus.todo)
        # the step decides what the scout is sent: its role, instruction file and response template are stamped
        assert planning.data["handout"] == {
            "step": "PR-4", "role": "scout", "instructions": "projects.plan.write",
            "template": "projects.plan.response",
        }
        conn = await roster.ensure_roster(session, OWNER)
        task = await take(session, project, conn)
        assert task.id == planning.id and task.status == PlanTaskStatus.in_progress
        view = await task_context.task_view(session, project, task)
        assert view["report_with"] == "plan.propose" and view["where"]["phase"] == "plan"
        assert view["context"]["site"]["url"] == "https://careers.example.test/"
        assert "projects" in view["instruction"] or view["instruction"]


async def test_a_second_open_project_for_a_site_is_refused(js):
    async with db.session_factory()() as session:
        site = await new_site(session)
        await projects.create_project(session, OWNER, site.id, "Learn it", "Read its roles", created_by="you")
        with pytest.raises(Exception, match="already has a project"):
            await projects.create_project(session, OWNER, site.id, "Again", "Again", created_by="you")


# --- the plan starts without asking ------------------------------------------------------------------------------


async def test_a_plan_that_needs_nobody_starts_without_asking_and_each_item_gets_its_own_run(js, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        sent = await propose(session, project, item("a"), item("b"))
        plan = sent.plan
        assert sent.started and plan.status == PlanStatus.approved
        assert (sent.wake.reason, sent.wake.mode) == ("plan_approved", "do")
        tasks = (await session.scalars(
            select(PlanTask).where(PlanTask.plan_id == plan.id).order_by(PlanTask.position))).all()
        assert [(t.item_key, t.status) for t in tasks] == [("a", PlanTaskStatus.todo), ("b", PlanTaskStatus.todo)]
        assert await step_of(session, project) == "PR-9"
        for key in ("a", "b"):
            kid = await child_of(session, project, plan, key)
            assert kid is not None and await round_process._step_of(session, kid) == "Hand_out"
        [line] = await trail(session, "plan_auto_started")
        assert "Started without asking" in line.sentence and "public pages" in line.sentence
        assert asks.ASKS_KEY not in session.info  # the owner was not asked
        # the planning task ended with the plan
        planning = await session.scalar(select(PlanTask).where(PlanTask.action == PlanItemAction.plan))
        assert planning.status == PlanTaskStatus.done


# --- items ------------------------------------------------------------------------------------------------------


async def test_items_are_handed_out_in_plan_order_and_end_done_or_off_plan(js, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        plan = (await propose(session, project, item("a"), item("b"))).plan
        a = await take(session, project, conn)
        assert (a.item_key, a.status, a.checked_out_by) == ("a", PlanTaskStatus.in_progress, conn.id)
        kid = await child_of(session, project, plan, "a")
        assert await round_process._step_of(session, kid) == "Specialist_does"
        assert (a.data["handout"]["step"], a.data["handout"]["instructions"]) == (
            "Specialist_does", "projects.do.item")
        await completion.complete_task(session, a, project, "done", evidence="12 postings", summary="read it")
        await session.refresh(kid)
        assert kid.status == COMPLETED and await last_end(session, kid) == "End_done"
        assert kid.data["reading"]["outcome"] == "done"

        b = await take(session, project, conn)
        assert b.item_key == "b"
        with pytest.raises(Exception, match="off_plan_reason"):
            await completion.complete_task(session, b, project, "off_plan")
        await completion.complete_task(session, b, project, "off_plan", reason="the page needs a login", evidence="no")
        kid_b = await child_of(session, project, plan, "b")
        assert kid_b.status == COMPLETED and await last_end(session, kid_b) == "End_off_plan"
        assert kid_b.data["reading"] == {"outcome": "off_plan", "said": "the page needs a login"}
        # every item has ended: the round waits to make its checks
        assert await step_of(session, project) == "PR-11A"
        assert [(t.status, t.outcome_reason) for t in (a, b)] == [
            (PlanTaskStatus.done, None), (PlanTaskStatus.off_plan, "the page needs a login")]
        assert len(await trail(session, "task_done")) == 1 and len(await trail(session, "task_off_plan")) == 1


# --- check, report, act -----------------------------------------------------------------------------------------


async def test_checks_are_made_and_run_by_site_check_and_the_report_is_written(js, check_says):
    async with db.session_factory()() as session:
        project, plan, conn, act = await first_round(
            session, check_says, ends={"a": "done", "b": "done"}, differences={"b": "only 3 of 40 postings came back"})
        checks = (await session.scalars(
            select(PlanTask).where(PlanTask.plan_id == plan.id, PlanTask.action == PlanItemAction.review_check)
            .order_by(PlanTask.position))).all()
        assert [(c.data["key"], c.status, c.findings[0]["verdict"]) for c in checks] == [
            ("a", PlanTaskStatus.done, "ok"), ("b", PlanTaskStatus.done, "issue")]
        assert checks[1].findings[0]["note"] == "only 3 of 40 postings came back"
        assert all(c.data["checked_by"] == "code" for c in checks)
        for key, ending in (("a", "End_on_plan"), ("b", "End_off_plan")):
            kid = await child_of(session, project, plan, key, CHECK)
            assert kid.status == COMPLETED and await last_end(session, kid) == ending
        report = await session.scalar(select(ReviewReport).where(ReviewReport.project_id == project.id))
        assert (report.verdict, report.summary) == ("iterate", "1 of 2 on plan; 1 off plan.")
        assert "only 3 of 40 postings came back" in report.findings_summary
        # What the owner reads in Activity says it in job-search words: no plan version, no item counts on or off plan.
        assert [line.sentence for line in await trail(session, "plan_proposed")] == [
            "The scout drew up a plan for “Learn the site”."]
        assert [line.sentence for line in await trail(session, "check_report")] == [
            "resumebot checked the scout's work on “Learn the site”: some of it needs another try."]
        # Act was made when the round reached it, with the round's items and the site
        assert act.action == PlanItemAction.act and act.context["report"]["verdict"] == "iterate"
        assert [r["key"] for r in act.context["items"]] == ["a", "b"] and act.context["site"]["kind"] == "employer"
        assert "closing" not in act.context  # not accepted: nothing to close yet
        assert await step_of(session, project) == "PR-13"
        assert act.data["handout"]["step"] == "PR-13"


async def test_a_second_caller_that_read_the_round_waiting_to_make_the_checks_finds_them_made_and_nothing_fails(
    js, check_says, monkeypatch,
):
    """Two requests for one project (a scout asking again while the first was still
    making the checks) both read the round waiting at PR-11A, before either had committed. The first made the checks;
    the second's answer to PR-11A reached the engine after the step was answered and raised `NotWaiting` out of
    `task.next`. The step already answered is not an error: the second caller has nothing to do, exactly as when it
    reads the round somewhere else. The savepoint undid its attempt, so its project and plan are read back and still
    work."""
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        sent = await propose(session, project, item("a"), item("b"))
        plan = sent.plan
        await run_items(session, project, conn, {"a": "done", "b": "done"})
        assert await step_of(session, project) == "PR-11A"

        made = await round_process.drive_make_checks(session, project, plan)  # the first caller
        assert [c.data["key"] for c in made] == ["a", "b"] and await step_of(session, project) == "PR-13"

        async def read_before_the_first_committed(session, instance):
            return {round_process.MAKE_CHECKS_STEP}

        with monkeypatch.context() as stale:
            stale.setattr(round_process, "_waiting_at", read_before_the_first_committed)
            assert await round_process.drive_make_checks(session, project, plan) is None  # the second caller

        assert plan.status == PlanStatus.approved and project.stopped_at is None  # read back, not left expired
        assert await step_of(session, project) == "PR-13"  # the round stays where the first caller left it
        checks = (await session.scalars(
            select(PlanTask).where(PlanTask.plan_id == plan.id, PlanTask.action == PlanItemAction.review_check))).all()
        assert sorted(c.data["key"] for c in checks) == ["a", "b"]  # one check for each item, made once
        reports = (await session.scalars(select(ReviewReport).where(ReviewReport.project_id == project.id))).all()
        assert len(reports) == 1
        assert not await trail(session, "process_failed")  # nothing was held, so nobody is asked about it
        assert not (await session.scalars(select(Task).where(Task.kind == failures.KIND))).all()


async def test_the_check_is_handed_how_each_item_ended_and_what_it_found(js, monkeypatch):
    """`site.check` read `expected_count` and the off-plan state of the item, but the round only handed it the
    item's key, title and kind."""
    seen: dict[str, dict] = {}

    async def fake(session, instance, data):
        seen[data["item"]["key"]] = data["item"]
        return {"on_plan": True, "difference": ""}

    monkeypatch.setattr(site_check, "site_check", fake)
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        plan = (await propose(session, project, item("a"), item("b"))).plan
        a = await take(session, project, conn)
        await completion.complete_task(
            session, a, project, "done", evidence="no openings", result={"count": 0, "expected_count": 0})
        b = await take(session, project, conn)
        await completion.complete_task(session, b, project, "off_plan", reason="the page needs a login", evidence="no")
        await check_flow.advance_plan_checks(session, project, plan)
    assert (seen["a"]["status"], seen["a"]["result"], seen["a"]["evidence"]) == (
        "done", {"count": 0, "expected_count": 0}, "no openings")
    assert (seen["b"]["status"], seen["b"]["outcome_reason"]) == ("off_plan", "the page needs a login")
    assert seen["a"]["data"]["url"].endswith("/a") and "handout" not in seen["a"]["data"]


async def test_the_scout_has_no_tool_that_writes_a_check_result(js, check_says):
    async with db.session_factory()() as session:
        project, plan, conn, act = await first_round(session, check_says)
        check = await session.scalar(select(PlanTask).where(PlanTask.action == PlanItemAction.review_check))
        check.status = PlanTaskStatus.in_progress
        with pytest.raises(Exception, match="run by the app"):
            await completion.complete_task(session, check, project, "done", evidence="trust me")


async def test_act_with_an_accepted_report_closes_the_project_and_keeps_the_skill_and_the_lessons(js, check_says):
    async with db.session_factory()() as session:
        project, plan, conn, act = await first_round(session, check_says)
        # every check was on plan and nothing is left to look into: Act is handed the project's own outcome to judge by
        assert act.context["report"]["verdict"] == "accepted"
        assert act.context["closing"]["outcome"] == "Its listings can be read"
        lesson, created = await agent_calls.record_lesson(
            session, project, act, conn.id, text="This site loads ten more postings on scroll.",
            applies_to="employer", source="item a: the report", subject="paging")
        assert created and lesson.applies_to == "employer"
        outcome, wake = await agent_calls.complete_act(
            session, project, act, conn.id, summary=SUMMARY, resolution=None,
            skill={"kind": "list_page", "url": "https://careers.example.test/jobs", "link_pattern": r"/jobs/\d+",
                   "title_from": "link_text"})
        assert (outcome["next"], outcome["lessons_recorded"], outcome["skill_version"]) == ("finished", 1, 1)
        assert wake is None
        assert project.finished_at is not None and project.finish_summary == "Read two pages."
        assert project.resolution["problem"] == "Its listings can be read." and project.resolution["outcome"] == (
            "Resolved: the last check confirmed it.")
        run = await run_of_any(session, project)
        assert run.status == COMPLETED and await last_end(session, run) == "E-finished"
        skill = await session.scalar(select(SiteSkill).where(SiteSkill.site_id == project.site_id))
        assert (skill.version, skill.retired_at, skill.recipe["kind"], skill.made_by_project_id) == (
            1, None, "list_page", project.id)
        assert len(await trail(session, "project_finished")) == 1
        assert [line.sentence for line in await trail(session, "act_done")] == [
            "The scout wrote up what it learned about “Learn the site”."]
        assert [line.sentence for line in await trail(session, "check_report")] == [
            "resumebot checked the scout's work on “Learn the site”: it all held up."]
        # a finished project hands out nothing more
        assert await completion.next_task_or_reason(session, project, conn.id) == (None, "finished")


async def test_act_judges_the_round_against_the_projects_outcome_and_a_bad_resolution_is_refused(js, check_says):
    """The closing rule (act.learn): the Act task carries `closing.outcome`; the resolution is read against it. A bad
    resolution is refused whole so the scout can say it again."""
    async with db.session_factory()() as session:
        project, plan, conn, act = await first_round(session, check_says)
        with pytest.raises(Exception, match="problem"):
            await agent_calls.complete_act(
                session, project, act, conn.id, summary=SUMMARY, resolution={"done": "Read it.", "outcome": "Met."})
        assert act.status == PlanTaskStatus.in_progress and project.finished_at is None
        await agent_calls.complete_act(
            session, project, act, conn.id, summary=SUMMARY, resolution={
                "problem": "The site's roles were not being read.", "done": "Built a reader for its list pages.",
                "outcome": "Its listings can be read: both pages returned their postings."})
        assert project.resolution["outcome"].startswith("Its listings can be read")  # the scout's own, kept


async def test_act_with_an_iterate_report_asks_for_the_next_plan_which_carries_the_lessons(js, check_says):
    async with db.session_factory()() as session:
        project, plan, conn, act = await first_round(
            session, check_says, ends={"a": "done", "b": "the page needs a login"},
            differences={"b": "no postings came back"})
        report = await session.scalar(select(ReviewReport).where(ReviewReport.project_id == project.id))
        assert report.verdict == "iterate"
        await agent_calls.record_lesson(
            session, project, act, conn.id, text="Its list pages need no login, only the data request.",
            applies_to="project", source="item b: the report", subject="login")
        outcome, wake = await agent_calls.complete_act(session, project, act, conn.id, summary=SUMMARY, resolution=None)
        assert outcome["next"] == "plan_ready" and (wake.reason, wake.mode) == ("plan_ready", "plan")
        assert project.finished_at is None
        nxt = await session.get(PlanTask, uuid.UUID(outcome["next_task_id"]))
        assert (nxt.title, nxt.action, nxt.status, nxt.plan_id) == (
            "Write plan v2", PlanItemAction.plan, PlanTaskStatus.todo, plan.id)
        assert nxt.context["based_on_version"] == 1 and nxt.context["write_version"] == 2
        assert [r["key"] for r in nxt.context["unresolved_items"]] == ["b"]
        assert nxt.context["unresolved_items"][0]["found"] == "no postings came back"
        assert nxt.data["handout"]["step"] == "PR-4"
        assert await step_of(session, project) == "PR-4"
        # the scout is handed it with the lessons of the round before
        task = await take(session, project, conn)
        view = await task_context.task_view(session, project, task)
        assert view["report_with"] == "plan.revise" and view["report_example"]["based_on_version"] == 1
        assert [item["text"] for item in view["context"]["lessons"]] == [
            "Its list pages need no login, only the data request."]
        # a plan that leaves the unresolved item out is refused; one that accounts for it starts on its own
        with pytest.raises(Exception, match="doesn't account for 1 unresolved item"):
            await revise(session, project, 1, item("a"))
        sent = await revise(session, project, 1, item("a"), item("b"))
        assert sent.started and sent.plan.version == 2 and sent.plan.status == PlanStatus.approved
        assert await step_of(session, project) == "PR-9"
        assert plan.status == PlanStatus.superseded


async def test_a_second_round_that_is_accepted_finishes_the_project_after_the_first_iterated(js, check_says):
    """The loop chain (the upstream test_loop_chain, by pattern): iterate, then accepted, then finished."""
    async with db.session_factory()() as session:
        project, plan, conn, act = await first_round(
            session, check_says, ends={"a": "done", "b": "off the plan"}, differences={"b": "nothing came back"})
        await agent_calls.complete_act(session, project, act, conn.id, summary=SUMMARY, resolution=None)
        check_says.clear()
        sent = await revise(session, project, 1, item("a"), item("b"))
        assert sent.started
        await run_items(session, project, conn, {"a": "done", "b": "done"})
        made, why = await check_flow.advance_plan_checks(session, project, sent.plan)
        assert why == "act_ready"
        act2 = await take(session, project, conn)
        assert act2.item_key == "act-v2" and act2.context["report"]["verdict"] == "accepted"
        assert act2.context["closing"]["outcome"] == "Its listings can be read"
        outcome, _ = await agent_calls.complete_act(session, project, act2, conn.id, summary=SUMMARY, resolution=None)
        assert outcome["next"] == "finished" and project.finished_at is not None
        reports = (await session.scalars(
            select(ReviewReport).where(ReviewReport.project_id == project.id)
            .order_by(ReviewReport.plan_version))).all()
        assert [r.verdict for r in reports] == ["iterate", "accepted"]
        assert project.resolution["done"] == "Read two pages."  # the closing Act's own words


# --- pausing, resuming and stopping ---------------------------------------------------------------------------------


async def test_resuming_after_a_decline_asks_for_a_fresh_plan_whatever_the_pause_says(js, check_says):
    """The declined plan was found by comparing the pause's stored sentence with a rebuilt one. Nothing declines a
    plan any more (migration 0088 left such projects paused), so the plan is declined here the way it left them."""
    async with db.session_factory()() as session:
        project = await new_project(session)
        plan = (await propose(session, project, item("a"))).plan
        plan.status = PlanStatus.declined
        await completion.stop_project(session, project, "The owner declined plan v1.")
        project.stop_reason = "paused by the owner"
        assert await completion.resume_project(session, project) is True
        assert project.stopped_at is None and await step_of(session, project) == "PR-4"


async def test_stopping_a_project_closes_the_questions_it_still_had_open_in_the_inbox(js, check_says):
    """The owner's Stop left the project's other Inbox questions open, asking about a project that was over."""
    from app.models.work import Task

    async with db.session_factory()() as session:
        project = await new_project(session)
        session.add(Task(owner=OWNER, key="escalate-x", title="Which page?", kind="site_escalation",
                         link_kind="project", link_id=project.id))
        await session.flush()
        await escalation.stop_for_good(session, project)
        [ask] = (await session.scalars(select(Task).where(Task.key == "escalate-x"))).all()
        assert ask.state == "done" and ask.note == "The project ended."


async def test_an_activity_line_is_cut_at_a_word_and_what_was_cut_is_kept_as_its_detail(js):
    """The sentence was cut at 140 characters wherever that fell, and the reason in it was lost."""
    async with db.session_factory()() as session:
        project = await new_project(session)
        long = "The scout " + " ".join(["measured"] * 30) + " and then the last long word wentoverthelimit."
        line = records._record_activity(session, project, "agent", "note", long)
        assert len(line.sentence) <= 140 and line.sentence.endswith("…") and not line.sentence.endswith("t…")
        assert line.sentence.removesuffix("…").split()[-1] == "measured"
        assert line.detail == long, "the whole line is kept"
        kept = records._record_activity(session, project, "agent", "note", long, detail="Its own detail.")
        assert kept.detail == "Its own detail."
        short = records._record_activity(session, project, "agent", "note", "Done.")
        assert (short.sentence, short.detail) == ("Done.", None)


async def test_two_unfinished_projects_for_one_site_are_refused_by_the_database_too(js):
    """The code checked for an open project first, but two requests at once could both pass the check."""
    from sqlalchemy.exc import IntegrityError

    async with db.session_factory()() as session:
        site = await new_site(session)
        await projects.create_project(session, OWNER, site.id, "Learn", "Its listings can be read")
        session.add(Project(owner=OWNER, site_id=site.id, title="Again", outcome="Again"))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
    async with db.session_factory()() as session:
        site = await new_site(session, "https://careers.other.example.test/")
        first = await projects.create_project(session, OWNER, site.id, "Learn", "Its listings can be read")
        first.finished_at = datetime.now(UTC)
        await session.flush()
        again = await projects.create_project(session, OWNER, site.id, "Learn", "Its listings can be read")
        assert again.id != first.id, "a finished project does not count"


# --- a site the server cannot read asks the owner nothing -----------------------------------------------------------


async def test_a_plan_that_says_its_site_asks_not_to_be_read_asks_no_one_and_the_site_is_switched_off(js, check_says):
    """The owner decided not to spend cycles on something that will fail. A site nothing can read is turned off:
    the gateway PR-7N takes the way out to PR-7U: no card, no Inbox task, no round of items; the site is off and the
    owner is told in a notification."""
    why = "it asks not to be read"
    async with db.session_factory()() as session:
        project = await new_project(session)
        site = await session.get(Site, project.site_id)
        sent = await propose(session, project, item("a", asks_not_to_be_read=True))
        plan = sent.plan
        assert not sent.started and sent.wake is None
        # the round went to PR-7U and ended at E-unreadable: nothing waits for the scout or for the owner
        run = await run_of_any(session, project)
        assert await last_end(session, run) == "E-unreadable"
        assert asks.ASKS_KEY not in session.info and not await trail(session, "owner_asked")
        assert (await session.scalars(select(Task))).all() == []
        # the plan is set aside and the project ended with its own words
        assert plan.status == PlanStatus.superseded and project.finished_at is not None
        assert project.resolution["outcome"] == (
            f"Stopped: it can't be read ({why}), so resumebot switched the site off.")
        [line] = await trail(session, "site_unreadable")
        assert line.sentence.startswith("This project ended. Stopped: it can't be read")
        # the site is off by the app, with why, and the owner was told once
        assert (site.on, site.off_by, site.off_note) == (False, "app", why)
        assert (await session.scalars(select(Notification))).all() == [], "a refusal is nothing the owner can act on"
        # nothing is handed out for it
        conn = await roster.ensure_roster(session, OWNER)
        assert await completion.next_task_or_reason(session, project, conn.id) == (None, "finished")


@pytest.mark.parametrize("read_by, found", [("app", None), ("auto", None), ("auto", "app")])
async def test_a_plan_that_needs_a_browser_hands_the_site_to_it_and_switches_nothing_off(
        js, check_says, read_by, found):
    """The browser has not been tried, so the site is not turned off: it is set to be read in the owner's browser, as
    "Ask resumebot to find out" does, and no one is told. The gateway takes PR-7B and the run ends at E-browser."""
    from app.sites.models import BY_BROWSER, effective_read

    async with db.session_factory()() as session:
        project = await new_project(session)
        site = await session.get(Site, project.site_id)
        site.read_by, site.read_found = read_by, found
        sent = await propose(session, project, item("a", needs_browser=True))
        assert not sent.started and sent.wake is None
        run = await run_of_any(session, project)
        assert await last_end(session, run) == "E-browser"
        assert asks.ASKS_KEY not in session.info and not await trail(session, "owner_asked")
        assert (await session.scalars(select(Task))).all() == []
        # the project ended with its own words and the plan is set aside: no round of items, no skill to learn
        assert sent.plan.status == PlanStatus.superseded and project.finished_at is not None
        assert project.resolution["outcome"] == (
            "Handed to your browser: its listings only load in a browser, so the app can't read it from the server.")
        [line] = await trail(session, "site_to_browser")
        assert line.sentence.startswith("This project ended. Handed to your browser")
        # the site is on and read in the browser: nothing was switched off and nobody was told
        assert effective_read(site) == BY_BROWSER and site.on is True
        assert (site.off_by, site.off_note) == (None, None)
        assert (await session.scalars(select(Notification))).all() == []
        if read_by == "auto":
            assert (site.read_by, site.read_found, site.read_note) == (
                "auto", "browser", "Its listings only load in a browser.")
        else:  # a site the app was told to read keeps the reason it was handed over too, for its row to say
            assert (site.read_by, site.read_note) == (BY_BROWSER, "Its listings only load in a browser.")
        conn = await roster.ensure_roster(session, OWNER)
        assert await completion.next_task_or_reason(session, project, conn.id) == (None, "finished")


@pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")
async def test_the_browser_then_reads_a_site_handed_to_it_and_a_block_stops_its_domain_without_switching_it_off(
        js, check_says):
    """A site whose plan says it needs the browser is the browser's (the browser work spec): once mapped it is a
    scan item of the queue, and a block stops its domain for good (`host_stops.py`); the site stays on."""
    from app.jobsearch.browser import handback as bh
    from app.jobsearch.browser import items as bd
    from app.jobsearch.missions import core as missions
    from app.jobsearch.models import BrowserItem
    from app.sites import where
    from tests.builders import WORKER, in_session
    from tests.support import learn_sites, load_profile

    now = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.resume(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        project = await new_project(session)
        site = await session.get(Site, project.site_id)
        await propose(session, project, item("a", needs_browser=True))
        assert [s.id for s in await where.browser_sites(session, OWNER)] == [site.id], "the browser's work has it"
        await learn_sites(session, site)  # the Pathfinder has mapped it
        await session.commit()
        site_id = site.id
    got = await in_session(WORKER, lambda s: bd.next_item(s, OWNER, now))
    assert (got["item"]["kind"], got["item"]["site"]) == ("scan", "careers.example.test")
    await in_session(WORKER, lambda s: bh.hand_back(s, OWNER, got["item"]["item_id"], "blocked",
                                                    note="Asked me to sign in.", now=now))
    async with db.session_factory()() as session:
        site = await session.get(Site, site_id)
        assert site.on is True and site.off_by is None
        assert (await session.scalars(select(Notification))).all() == []
        [scan] = (await session.scalars(select(BrowserItem).where(BrowserItem.kind == "scan"))).all()
    assert scan.state == "stuck" and "sign in" in scan.last_note


async def test_a_plan_that_says_both_is_a_refusal_and_the_site_is_switched_off(js, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        site = await session.get(Site, project.site_id)
        await propose(session, project, item("a", needs_browser=True, asks_not_to_be_read=True))
        run = await run_of_any(session, project)
        assert await last_end(session, run) == "E-unreadable"
        assert (site.on, site.off_by, site.off_note) == (False, "app", "it asks not to be read")


async def test_a_site_switched_on_again_gets_a_project_again_and_is_told_again_when_the_plan_says_the_same(
        js, check_says):
    from app.sites import autostart, where

    async with db.session_factory()() as session:
        project = await new_project(session)
        site = await session.get(Site, project.site_id)
        await propose(session, project, item("a", asks_not_to_be_read=True))
        assert not site.on and project.finished_at is not None
        # off, it is skipped by the mission that starts site projects
        assert await autostart.ensure_projects(session, OWNER, by_switch=True) == []
        # the owner switches it on: the next mission starts a project for it, and the same plan switches it off again
        await where.switch(session, OWNER, site.id, True)
        assert (site.on, site.off_by, site.off_note) == (True, None, None)
        [again] = await autostart.ensure_projects(session, OWNER, by_switch=True)
        assert again.id != project.id
        await propose(session, again, item("a", asks_not_to_be_read=True))
        assert (site.on, site.off_by) == (False, "app") and again.finished_at is not None
        assert (await session.scalars(select(Notification))).all() == []


# --- the round budget ---------------------------------------------------------------------------------------------


async def test_a_project_past_its_rounds_is_given_up_by_the_app_and_asks_the_owner_nothing(js, check_says, monkeypatch):
    monkeypatch.setattr(autostart, "ROUND_CAP", 1)
    async with db.session_factory()() as session:
        project, plan, conn, act = await first_round(session, check_says, differences={"b": "nothing came back"})
        site = await session.get(Site, project.site_id)
        await agent_calls.complete_act(session, project, act, conn.id, summary=SUMMARY, resolution=None)
        # the second plan would be a round past the cap: no question, the app switches the site off
        sent = await revise(session, project, 1, item("a"), item("b"))
        assert not sent.started and asks.ASKS_KEY not in session.info
        assert (await session.scalars(select(Task))).all() == []
        run = await run_of_any(session, project)
        assert await last_end(session, run) == "E-unreadable"
        assert project.finished_at is not None
        assert (site.on, site.off_by, site.off_note) == (False, "app", autostart.TOO_MANY_CLAUSE)
        assert (await session.scalars(select(Notification))).all() == [], "nothing the owner can act on"
        assert await completion.next_task_or_reason(session, project, conn.id) == (None, "finished")


async def test_a_project_within_its_rounds_carries_on_without_asking(js, check_says):
    async with db.session_factory()() as session:
        project, plan, conn, act = await first_round(session, check_says, differences={"b": "nothing came back"})
        await agent_calls.complete_act(session, project, act, conn.id, summary=SUMMARY, resolution=None)
        sent = await revise(session, project, 1, item("a"), item("b"))
        assert sent.started and asks.ASKS_KEY not in session.info


# --- the scout that does not finish --------------------------------------------------------------------------------


async def test_a_planning_task_the_scout_did_not_finish_is_tried_again_then_asked_about(js, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        for attempt in (1, 2, 3):
            planning = await take(session, project, conn) if attempt == 1 else None
            if attempt > 1:
                planning = await session.scalar(select(PlanTask).where(PlanTask.action == PlanItemAction.plan))
                planning.status = PlanTaskStatus.in_progress
                planning.checked_out_by = conn.id
            planning.checked_out_at = datetime.now(UTC) - timedelta(hours=2)
            await session.flush()
            wakes = await escalation.retry_planner_runs(session)
            if attempt == 1:
                assert [(w[1], w[2]) for w in wakes] == [("planner_retry", "plan")]
                assert planning.status == PlanTaskStatus.todo
            elif attempt == 2:
                assert wakes == [] and planning.status == PlanTaskStatus.waiting  # back in 15 minutes
                later = datetime.now(UTC) + timedelta(minutes=16)
                assert [(w[1], w[2]) for w in await escalation.retry_planner_runs(session, later)] == [
                    ("planner_retry", "plan")]
                assert planning.status == PlanTaskStatus.todo
            else:
                assert wakes == [] and planning.status == PlanTaskStatus.waiting
                [ask] = session.info[asks.ASKS_KEY]
                assert ask["card"] == "stuck" and [o["id"] for o in ask["options"]] == ["retry", "pause", "stop"]
                assert "after 3 tries" in ask["title"]
        # the pass is a run of its own, and the round's own run was not replaced
        passes = (await session.scalars(
            select(ProcInstance).where(ProcInstance.subject.endswith(":pass")))).all()
        assert [await last_end(session, p) for p in passes] == ["E-retried"] * 3 and len(passes) == 3
        assert await step_of(session, project) == "PR-4"


async def test_the_sweep_hands_a_stuck_planning_task_back_to_the_scout_and_knocks_for_its_project(js, check_says):
    """`retry_planner_runs` had no caller, so a planning task the scout held for good was never handed back."""
    from app.jobsearch.missions import core as missions
    from tests.support import load_profile

    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        planning = await take(session, project, conn)
        planning.checked_out_at = datetime.now(UTC) - timedelta(hours=2)
        await session.flush()
        sweep = next(m for m in (await session.scalars(select(missions.Mission))).all() if m.key == missions.SWEEP)
        entry = await missions.start(session, OWNER, sweep, by="you")
        assert entry.outcome == "done" and "back to the scout" in entry.summary
        assert entry.started == {"site_projects": [str(project.id)]}
        assert planning.status == PlanTaskStatus.todo and planning.checked_out_by is None
        assert planning.data["planner_runs"]["failed"] == 1


# --- lessons ------------------------------------------------------------------------------------------------------


async def test_lessons_are_scoped_sourced_and_retired_not_deleted(js, check_says):
    async with db.session_factory()() as session:
        project, plan, conn, act = await first_round(session, check_says)
        record = lambda text, applies_to="employer", source="item a: the report", subject=None: (  # noqa: E731
            agent_calls.record_lesson(
                session, project, act, conn.id, text=text, applies_to=applies_to, source=source, subject=subject))
        with pytest.raises(Exception, match="needs a `source`"):
            await record("Page two loads on scroll.", source="somewhere")
        with pytest.raises(Exception, match="restates a rule"):
            await record("Every plan item needs a real kind.")
        with pytest.raises(Exception, match="applies_to"):
            await record("Page two loads on scroll.", applies_to="household")
        first, created = await record("Paging is a next link.", subject="paging")
        again, created_again = await record("Paging is a next link.", subject="paging")
        assert created and not created_again and again.id == first.id
        # "all" is the owner's scope
        everywhere, _ = await record("Sites hide the count until the list loads.", applies_to="all")
        assert everywhere.applies_to == "owner"
        # a later round's lesson on the same subject retires the earlier one
        act.plan_version = 2
        newer, _ = await record("Paging is the data request, not a link.", subject="paging")
        await session.refresh(first)
        assert first.retired_at is not None and "contradicted by item a: the report" in first.retired_by
        shown = [r["text"] for r in await lessons.lessons_for_project(session, project)]
        assert "Paging is a next link." not in shown and "Paging is the data request, not a link." in shown
        assert (await session.scalar(select(Lesson).where(Lesson.id == first.id))) is not None
        assert newer.applies_to == "employer"


# --- seams ----------------------------------------------------------------------------------------------------------


async def test_the_owner_ask_is_a_seam_step_5_wires_to_the_inbox(js, check_says):
    seen = []

    async def inbox(session, project, **ask):
        seen.append((ask["card"], ask["key"], project.id))
        return None

    asks.set_ask_owner(inbox)
    try:
        async with db.session_factory()() as session:
            project = await new_project(session)
            conn = await roster.ensure_roster(session, OWNER)
            planning = await take(session, project, conn)
            for attempt in (1, 2, 3):  # the planner goes quiet three times: the third asks the owner
                if attempt > 1:
                    planning.status, planning.checked_out_by = PlanTaskStatus.in_progress, conn.id
                planning.checked_out_at = datetime.now(UTC) - timedelta(hours=2)
                await session.flush()
                await escalation.retry_planner_runs(session)
                if attempt == 2:
                    await escalation.retry_planner_runs(session, datetime.now(UTC) + timedelta(minutes=16))
            assert seen == [("stuck", f"stuck-{planning.id}", project.id)]
            assert asks.ASKS_KEY not in session.info and not await trail(session, "owner_asked")
    finally:
        asks.set_ask_owner(None)


async def test_a_plan_taken_by_a_run_that_failed_is_held_and_try_again_takes_it(js, check_says, monkeypatch):
    """A run that fails is rolled back and held where it was (no other code decides); `retry` runs the entry again."""
    async with db.session_factory()() as session:
        project = await new_project(session)
        real = round_process.start_facts
        calls = {"n": 0}

        async def broken(session, project, plan):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("the facts could not be read")
            return await real(session, project, plan)

        monkeypatch.setattr(round_process, "start_facts", broken)
        sent = await propose(session, project, item("a"))
        plan = sent.plan
        assert not sent.started and plan.status == PlanStatus.awaiting_approval  # held: nothing started it
        assert await step_of(session, project) == "PR-4"  # and the run is where it was
        await round_process.retry(session, OWNER, round_process.subject_of(OWNER, project.id))
        await session.refresh(plan)
        assert plan.status == PlanStatus.approved and await step_of(session, project) == "PR-9"


async def test_a_plan_with_nothing_to_do_goes_straight_to_its_checks_and_report(js, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        skip = {"key": "later", "kind": "site.read", "title": "Maybe later", "group": "Do", "action": "skip",
                "data": {"url": "https://careers.example.test/later"}}
        sent = await propose(session, project, skip)
        assert sent.started and await step_of(session, project) == "PR-11A"
        made, why = await check_flow.advance_plan_checks(session, project, sent.plan)
        assert why == "act_ready"
        report = await session.scalar(select(ReviewReport).where(ReviewReport.project_id == project.id))
        assert (report.verdict, report.summary) == ("accepted", "0 of 0 on plan; 0 off plan.")
        assert (await take(session, project, conn)).action == PlanItemAction.act


async def test_stopping_a_project_sets_its_run_aside(js, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        await propose(session, project, item("a"))
        await escalation.stop_for_good(session, project)
        run = await run_of_any(session, project)
        assert run.status != COMPLETED and project.finished_at is not None
        assert project.resolution["outcome"].startswith("Stopped.") and "Still open: Read a" in (
            project.resolution["outcome"])
        with pytest.raises(Exception, match="already finished"):
            await escalation.stop_for_good(session, project)


# --- kinds of work (the app ships none yet: these write their own) --------------------------------------------------

KIND = """---
kind: site.read
domain: site
title: Read a site's list pages
risk: read_only
enabled: true
data:
  url: {type: string, required: true, means: the page to read}
do: {runner: model}
done_means: The list pages were read and their postings counted.
check: {test: Fetch the page and count its postings., causes: []}
result:
  found: {type: int, required: true, means: how many postings came back}
---
Read the pages as they are.
"""


@pytest.fixture
def kinds(tmp_path):
    from app.config import settings
    from app.harness import clear_cache

    folder = tmp_path / "kinds" / "site"
    folder.mkdir(parents=True)
    path = folder / "site.read.kind.md"
    path.write_text(KIND, encoding="utf-8")
    saved = settings.kinds_path
    settings.kinds_path = tmp_path / "kinds"
    clear_cache()
    yield path
    settings.kinds_path = saved
    clear_cache()


def read(key: str) -> dict:
    return item(key, "site.read", url=f"https://careers.example.test/{key}")


async def test_kinds_are_checked_once_the_app_ships_some(js, check_says, kinds):
    async with db.session_factory()() as session:
        project = await new_project(session)
        with pytest.raises(Exception, match="doesn't exist"):
            await propose(session, project, item("a", "site.nope"))
        with pytest.raises(Exception, match="missing url"):
            await propose(session, project, {**item("a", None), "kind": "site.read"})
        with pytest.raises(Exception, match="no `kind`"):
            await propose(session, project, item("a", None))
        sent = await propose(session, project, read("a"))
        assert sent.started


async def test_a_kind_switched_off_after_the_plan_ends_its_item_off_plan_with_the_reason(js, check_says, kinds):
    from app.harness import clear_cache

    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        plan = (await propose(session, project, read("a"), read("b"))).plan
        kinds.write_text(KIND.replace("enabled: true", "enabled: false"), encoding="utf-8")
        clear_cache()
        task, why = await completion.next_task_or_reason(session, project, conn.id)
        assert (task, why) == (None, "act_ready")  # both items ended off plan, so the checks and Act came next
        tasks = (await session.scalars(
            select(PlanTask).where(PlanTask.plan_id == plan.id, PlanTask.action == PlanItemAction.do)
            .order_by(PlanTask.position))).all()
        assert [t.status for t in tasks] == [PlanTaskStatus.off_plan] * 2
        assert "switched off" in tasks[0].outcome_reason
        assert len(await trail(session, "permission_denied")) == 2
        kid = await child_of(session, project, plan, "a")
        assert kid.status == COMPLETED and await last_end(session, kid) == "End_off_plan"


async def test_a_done_item_of_a_kind_with_a_result_must_carry_it_and_it_is_kept_as_data(js, check_says, kinds):
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        await propose(session, project, read("a"))
        task = await take(session, project, conn)
        with pytest.raises(Exception, match="needs a `result` with found"):
            await completion.complete_task(session, task, project, "done", evidence="read it")
        with pytest.raises(Exception, match="isn't a whole number"):
            await completion.complete_task(session, task, project, "done", result={"found": "lots"})
        with pytest.raises(Exception, match="say what you tried"):
            await agent_calls.complete_task(session, task, project, conn.id, outcome="off_plan", reason="blocked")
        await completion.complete_task(session, task, project, "done", evidence="read it", result={"found": 12})
        assert task.result == {"found": 12}


async def test_a_round_that_only_looked_may_be_closed_by_act_against_the_projects_outcome(js, check_says, kinds):
    """A round of read-only items with nothing off plan reads "iterate" (its findings feed the next plan); Act is told
    it may close the project, and does when its evidence meets the project's own outcome."""
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        sent = await propose(session, project, read("a"), read("b"))
        for _ in range(2):
            task = await take(session, project, conn)
            await completion.complete_task(session, task, project, "done", evidence="read", result={"found": 12})
        await check_flow.advance_plan_checks(session, project, sent.plan)
        report = await session.scalar(select(ReviewReport).where(ReviewReport.project_id == project.id))
        assert report.verdict == "iterate"
        assert report.summary == "2 of 2 on plan; 0 off plan. 2 with more to do next round."
        act = await take(session, project, conn)
        assert act.context["closing"]["optional"] is True
        assert act.context["closing"]["outcome"] == "Its listings can be read"
        outcome, _ = await agent_calls.complete_act(
            session, project, act, conn.id, summary=SUMMARY, resolution={
                "problem": "The site's roles were not being read.", "done": "Read both list pages.",
                "outcome": "Its listings can be read: both pages returned their postings."})
        assert outcome["next"] == "finished" and project.finished_at is not None


async def test_a_round_that_only_looked_goes_on_when_act_finds_its_outcome_not_met(js, check_says, kinds):
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        sent = await propose(session, project, read("a"))
        task = await take(session, project, conn)
        await completion.complete_task(session, task, project, "done", evidence="read", result={"found": 3})
        await check_flow.advance_plan_checks(session, project, sent.plan)
        act = await take(session, project, conn)
        outcome, _ = await agent_calls.complete_act(session, project, act, conn.id, summary=SUMMARY, resolution=None)
        assert outcome["next"] == "plan_ready" and project.finished_at is None
        nxt = await session.get(PlanTask, uuid.UUID(outcome["next_task_id"]))
        [row] = nxt.context["unresolved_items"]
        assert (row["key"], row["result"]) == ("a", {"found": 3})  # the finding rides to the next plan, whole
