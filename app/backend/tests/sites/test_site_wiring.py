"""The site round wired to the rest of the app (docs/PRD.md path 29).

What is checked: the planner that could not finish asks in the Inbox and is tried again, paused or stopped; a held
failure of a site round becomes an Inbox task whose Try again resumes it (a job-search process's failure stays a log
line); a wake the loop returns knocks `jobsearch/work/wake.py` with `site_project`, and one that could not be delivered
is
tried again; what a site task hands the scout is kept with its `plan_task_id`; and a switched-on site gets one project
and never two. Synthetic data only."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from process_engine import live
from sqlalchemy import select

from app import db
from app.agents import roster
from app.inbox import service
from app.jobsearch import process as jobsearch_process
from app.jobsearch.main import app
from app.jobsearch.work import wake as scout_wake
from app.models.handout import TaskHandout
from app.models.work import Task
from app.settings import settings
from app.sites import (
    agent_calls,
    asks,
    autostart,
    escalation,
    failures,
    inbox_asks,
    projects,
    round_process,
    skills,
    task_context,
    wake_ops,
)
from app.sites.models import PlanStatus, PlanTaskStatus, Project, Site, SiteSkill
from tests.sites.builders import (
    item,
    new_project,
    new_site,
    propose,
    step_of,
    take,
)
from tests.support import OWNER


@pytest.fixture
def inbox():
    """The loop's asks wired to the Inbox, as the app does at start-up; the default put back afterwards."""
    inbox_asks.install()
    yield
    asks.set_ask_owner(None)


@pytest.fixture
async def client(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def knocks(monkeypatch):
    """The scout's door, replaced: every knock is kept as `(project id, reason)` and answered from `answers`."""
    seen: list[tuple[str, str]] = []
    answers: list[bool] = []

    async def knock(run_id, reason="search_started"):
        seen.append((run_id, reason))
        return answers.pop(0) if answers else True

    monkeypatch.setattr(scout_wake, "knock", knock)
    knock.seen, knock.answers = seen, answers
    return knock


async def another_project(session, url: str):
    """A project for a site of its own (a site has one open project)."""
    site = await new_site(session, url)
    return await projects.create_project(session, OWNER, site.id, "Learn the site", "Its listings can be read",
                                         created_by="you")


async def answer(session, task, option, note=None):
    """What the route does: the Inbox marks the task done, then the site's answer is carried out."""
    await service.answer(session, OWNER, task.id, option, note)
    return await inbox_asks.apply_answer(session, task, option, note)


# --- the planner that could not finish --------------------------------------------------


async def stuck_planner(session, conn, planning):
    """The scout holds the planning task and goes quiet three times: the third asks the owner."""
    for attempt in (1, 2, 3):
        if attempt > 1:
            planning.status, planning.checked_out_by = PlanTaskStatus.in_progress, conn.id
        planning.checked_out_at = datetime.now(UTC) - timedelta(hours=2)
        await session.flush()
        await escalation.retry_planner_runs(session)
        if attempt == 2:
            await escalation.retry_planner_runs(session, datetime.now(UTC) + timedelta(minutes=16))


async def test_a_scout_that_cannot_finish_a_plan_asks_in_the_inbox_and_try_again_hands_it_back(js, inbox, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        planning = await take(session, project, conn)
        await stuck_planner(session, conn, planning)
        task = await service.get_task_by_key(session, OWNER, f"stuck-{planning.id}")
        assert (task.kind, task.state, task.recommended) == ("site_stuck", "open", "retry")
        assert [o["id"] for o in task.options] == ["retry", "pause", "stop"] and "after 3 tries" in task.title
        applied = await answer(session, task, "retry")
        assert applied.carried and planning.status == PlanTaskStatus.todo
        assert "escalation" not in planning.data and "planner_runs" not in planning.data
        assert [(pid, w.reason, w.mode) for pid, w in applied.wakes] == [(project.id, "planner_retry", "plan")]
        # it fails three more times: the same question comes back, open
        planning.status, planning.checked_out_by = PlanTaskStatus.in_progress, conn.id
        await stuck_planner(session, conn, planning)
        assert task.state == "open" and task.answer is None


async def test_pausing_a_stuck_planner_holds_the_project_and_stopping_it_ends_it(js, inbox, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        planning = await take(session, project, conn)
        await stuck_planner(session, conn, planning)
        task = await service.get_task_by_key(session, OWNER, f"stuck-{planning.id}")
        applied = await answer(session, task, "pause")
        assert applied.wakes == [] and project.stopped_at is not None and planning.status == PlanTaskStatus.todo

        other = await another_project(session, "https://other.example.test/")
        planning2 = await take(session, other, conn)
        await stuck_planner(session, conn, planning2)
        task2 = await service.get_task_by_key(session, OWNER, f"stuck-{planning2.id}")
        await answer(session, task2, "stop")
        assert other.finished_at is not None and other.resolution["outcome"].startswith("Stopped.")


async def test_a_plan_that_comes_in_after_all_closes_the_stuck_question(js, inbox, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        planning = await take(session, project, conn)
        await stuck_planner(session, conn, planning)
        task = await service.get_task_by_key(session, OWNER, f"stuck-{planning.id}")
        await propose(session, project, item("a"))
        assert task.state == "done" and task.note == "The plan came in after all."


# --- a held failure --------------------------------------------------


@pytest.fixture
def breaks_once(monkeypatch):
    """`start_facts` fails on its first call: the run that takes a plan is held."""
    real = round_process.start_facts
    calls = {"n": 0}

    async def broken(session, project, plan):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the facts could not be read")
        return await real(session, project, plan)

    monkeypatch.setattr(round_process, "start_facts", broken)


async def test_a_held_failure_is_an_inbox_task_and_try_again_takes_the_plan(js, inbox, check_says, breaks_once):
    async with db.session_factory()() as session:
        project = await new_project(session)
        plan = (await propose(session, project, item("a"))).plan
        assert plan.status == PlanStatus.awaiting_approval and await step_of(session, project) == "PR-4"  # held
        task = await service.get_task_by_key(session, OWNER, f"site-failed:{project.id}")
        assert (task.kind, task.state, task.recommended) == ("site_failed", "open", "try_again")
        assert task.title == "resumebot couldn't take a plan: something unexpected got in the way"
        assert [o["id"] for o in task.options] == ["try_again", "leave_it"]
        assert (task.link_kind, task.link_id) == ("project", project.id)
        assert "the facts could not be read" not in task.title + task.detail  # an error's own words are never quoted
        applied = await answer(session, task, "try_again")
        await session.refresh(plan)
        assert plan.status == PlanStatus.approved and await step_of(session, project) == "PR-9"
        assert applied.carried and task.state == "done" and task.answer == "try_again"
        assert [(pid, w.reason) for pid, w in applied.wakes] == [(project.id, "retried")]


async def test_a_held_failure_closes_by_itself_when_a_later_attempt_works(js, inbox, check_says, breaks_once):
    async with db.session_factory()() as session:
        project = await new_project(session)
        await propose(session, project, item("a"))
        task = await service.get_task_by_key(session, OWNER, f"site-failed:{project.id}")
        assert task.state == "open"
        await round_process.retry(session, OWNER, round_process.subject_of(OWNER, project.id))
        assert (task.state, task.answer, task.note) == ("done", "worked", "It worked on a later try.")


async def test_leave_it_is_remembered_for_the_same_failure_but_a_different_one_is_a_new_question(js, inbox):
    async with db.session_factory()() as session:
        project = await new_project(session)
        subject = round_process.subject_of(OWNER, project.id)
        key = f"site-failed:{project.id}"

        async def fail(exc):
            await failures.held(session, OWNER, round_process.KEY, "take a plan", exc, subject)
            return await service.get_task_by_key(session, OWNER, key)

        task = await fail(RuntimeError("one"))
        again = await fail(RuntimeError("two"))  # the same failure in the owner's words: the one task
        assert again.id == task.id and task.state == "open"
        await answer(session, task, "leave_it")
        assert task.state == "done" and task.answer == "leave_it"
        await fail(RuntimeError("three"))
        assert task.state == "done"  # nothing has worked since: not asked again
        await fail(live.DriveFailed("the plan was not where the round expected it"))
        assert task.state == "open" and "not where the round expected" in task.title  # a different failure: asked
        await failures.worked(session, OWNER, round_process.KEY, "take a plan", subject)
        assert task.state == "done" and task.answer == "worked"
        assert len((await session.scalars(select(Task).where(Task.kind == "site_failed"))).all()) == 1


async def test_a_job_search_process_that_fails_is_still_only_logged(js, inbox):
    async with db.session_factory()() as session:
        await jobsearch_process._drive_failed(
            session, OWNER, jobsearch_process.ROUND, "start a search", RuntimeError("boom"), "search:1")
        await jobsearch_process._drive_worked(
            session, OWNER, jobsearch_process.ROUND, "start a search", "search:1")
        assert (await session.scalars(select(Task))).all() == []


# --- the wake --------------------------------------------------


async def test_a_wake_the_loop_returns_knocks_the_scouts_door_with_site_project(js, check_says, knocks):
    async with db.session_factory()() as session:
        project = await new_project(session)
        sent = await propose(session, project, item("a"))
        await session.commit()
    assert (sent.wake.reason, sent.wake.mode) == ("plan_approved", "do")
    assert await wake_ops.send(project.id, sent.wake) is True
    assert knocks.seen == [(str(project.id), "site_project")]
    assert scout_wake.SITE_PROJECT == "site_project"
    assert await wake_ops.send(project.id, None) is False and len(knocks.seen) == 1  # nothing due: no knock
    # one knock for a project woken twice in the same batch
    taken = await wake_ops.send_all([(project.id, sent.wake), (project.id, sent.wake)])
    assert taken == [str(project.id)] and len(knocks.seen) == 2


async def test_a_wake_that_cannot_be_delivered_is_tried_again_and_then_given_up(js, check_says, knocks, monkeypatch):
    monkeypatch.setattr(settings, "wake_url", "http://scout.test/wake")
    monkeypatch.setattr(wake_ops, "RETRY_DELAYS", (0, 0, 0))
    async with db.session_factory()() as session:
        project = await new_project(session)
        sent = await propose(session, project, item("a"))
        await session.commit()
    knocks.answers[:] = [False, False, True]
    assert await wake_ops.send(project.id, sent.wake) is False
    await wake_ops.settle()
    assert len(knocks.seen) == 3 and all(reason == "site_project" for _, reason in knocks.seen)  # sent, then 2 retries
    knocks.seen.clear()
    knocks.answers[:] = [False] * 9
    await wake_ops.send(project.id, sent.wake)
    await wake_ops.settle()
    assert len(knocks.seen) == 4  # the first knock and three retries; then resumebot gives up


async def test_a_retry_ends_when_the_scout_called_in_since_or_the_work_is_gone(js, check_says, knocks, monkeypatch):
    monkeypatch.setattr(settings, "wake_url", "http://scout.test/wake")
    monkeypatch.setattr(wake_ops, "RETRY_DELAYS", (0, 0, 0))
    async with db.session_factory()() as session:
        project = await new_project(session)
        sent = await propose(session, project, item("a"))
        await session.commit()
        since = datetime.now(UTC) - timedelta(minutes=1)
        assert await wake_ops.work_waiting(str(project.id), since) is True
        await roster.touch(session, OWNER)  # the scout asked for its work itself
        await session.commit()
        assert await wake_ops.work_waiting(str(project.id), since) is False
        assert await wake_ops.work_waiting(str(project.id), datetime.now(UTC) + timedelta(minutes=1)) is True
        project.stopped_at = datetime.now(UTC)
        await session.commit()
        assert await wake_ops.work_waiting(str(project.id), datetime.now(UTC) + timedelta(minutes=1)) is False
    knocks.answers[:] = [False] * 5
    await wake_ops.send(project.id, sent.wake)
    await wake_ops.settle()
    assert len(knocks.seen) == 1  # the work is gone, so there was nothing to try again


# --- the hand-outs --------------------------------------------------


async def test_what_a_site_task_hands_the_scout_is_kept_with_the_task_it_was_for(js, check_says):
    async with db.session_factory()() as session:
        project = await new_project(session)
        conn = await roster.ensure_roster(session, OWNER)
        planning = await take(session, project, conn)
        view = await task_context.task_view(session, project, planning)
        reply = {"ok": True, "task": view}
        assert await agent_calls.keep_copy(session, project, planning, reply, conn.id) is reply
        [row] = (await session.scalars(select(TaskHandout))).all()
        assert (row.via, row.task_id, row.plan_task_id, row.agent_name) == ("task_next", None, planning.id, "Scout")
        assert (row.process, row.step, row.title) == ("project_round", "PR-4", "Write plan v1")
        assert {(f["kind"], f["name"]) for f in row.files} == {
            ("instruction", "projects.plan.write"), ("template", "projects.plan.response")}
        assert row.payload["task"]["task_id"] == str(planning.id) and row.chars > 0
        await agent_calls.keep_copy(session, project, planning, reply, conn.id)  # the same words again: one row
        assert len((await session.scalars(select(TaskHandout))).all()) == 1

        sent = await propose(session, project, item("a"))
        a = await take(session, project, conn)
        await agent_calls.keep_copy(session, project, a, {"ok": True, "task": await task_context.task_view(
            session, project, a)}, conn.id)
        [done] = (await session.scalars(select(TaskHandout).where(TaskHandout.plan_task_id == a.id))).all()
        assert (done.process, done.step, done.via) == ("do_one_item", "Specialist_does", "task_next")
        assert sent.started


async def test_a_hand_out_needs_a_search_or_a_task_to_be_for(js):
    from app.planengine import handouts

    async with db.session_factory()() as session:
        assert "task_next" in handouts.VIA
        assert await handouts.record(session, OWNER, None, {"a": 1}, title="x", process="p", step="s") is None


# --- a switched-on site gets a project --------------------------------------------------


async def test_a_switched_on_site_gets_one_project_and_never_two(js, check_says):
    async with db.session_factory()() as session:
        alpha = await new_site(session, "https://alpha.example.test/careers")
        beta = await new_site(session, "https://beta.example.test/")
        off = await new_site(session, "https://gamma.example.test/")
        off.on = False
        await session.flush()
        made = await autostart.ensure_projects(session, OWNER)
        assert {p.site_id for p in made} == {alpha.id, beta.id}
        assert {p.title for p in made} == {"Read alpha.example.test", "Read beta.example.test"}
        for p in made:
            assert (p.outcome, p.round_budget, p.created_by) == (
                "This site's job listings can be read every night", autostart.DEFAULT_ROUND_BUDGET, "you")
            assert await step_of(session, p) == "PR-4"  # its round started, waiting for the scout's plan
        assert await autostart.ensure_projects(session, OWNER) == []  # never two
        assert (await session.scalars(select(Project).where(Project.site_id == off.id))).all() == []
        off.on = True
        assert [p.site_id for p in await autostart.ensure_projects(session, OWNER)] == [off.id]
        assert len((await session.scalars(select(Project))).all()) == 3


async def test_a_site_with_a_working_skill_or_a_stopped_project_waits_until_its_skill_breaks(js, check_says):
    recipe = {"kind": "list_page", "url": "https://jobs.example.test/careers", "link_pattern": r"/careers/jobs/\d+",
              "next_pattern": "^next$"}
    async with db.session_factory()() as session:
        working = await new_site(session, "https://working.example.test/")
        await skills.save_skill(session, OWNER, working.id, recipe, None)
        assert await autostart.ensure_projects(session, OWNER) == []  # it has a skill that works

        stopped = await new_site(session, "https://stopped.example.test/")
        project = await projects.create_project(session, OWNER, stopped.id, "Read it", "Read it", created_by="you")
        await escalation.stop_for_good(session, project)
        assert await autostart.ensure_projects(session, OWNER) == []  # the owner stopped it: theirs to start again

        # the working site's skill breaks: nothing replaces it, so its site wants a project
        for row in (await session.scalars(select(SiteSkill).where(SiteSkill.site_id == working.id))).all():
            await skills.retire_skill(session, row)
        [made] = await autostart.ensure_projects(session, OWNER)
        assert made.site_id == working.id
        assert await autostart.ensure_projects(session, OWNER) == []  # and while it is open, no second one
        # it ends, and nothing has broken since: left alone. A skill that breaks after that starts it again.
        await escalation.stop_for_good(session, made)
        assert await autostart.ensure_projects(session, OWNER) == []
        fresh = await skills.save_skill(session, OWNER, working.id, recipe, None)
        await skills.retire_skill(session, fresh)
        assert [p.site_id for p in await autostart.ensure_projects(session, OWNER)] == [working.id]


def test_the_scout_may_do_two_kinds_of_work():
    from app.sites import connections

    assert connections.SCOUT_KINDS == ("site.read", "porter.explore")


async def test_the_scouts_connection_is_the_one_in_the_roster(js):
    async with db.session_factory()() as session:
        from app.sites import connections

        assert await connections.scout_connection(session, OWNER) is None
        conn = await roster.ensure_roster(session, OWNER)
        assert (await connections.scout_connection(session, OWNER)).id == conn.id
        site = Site(owner=OWNER, url="https://x.example.test/", kind="employer", created_at=datetime.now(UTC))
        session.add(site)
        await session.flush()
        project = Project(owner=OWNER, site_id=site.id, title="t", outcome="o")
        assert await connections.doer_kinds_for(session, project) == ["site.read", "porter.explore"]
