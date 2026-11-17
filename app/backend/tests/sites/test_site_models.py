"""The site-project tables (docs/PRD.md path 29): a site, its project, plans with items and tasks, a check report, a
lesson, skill versions and a hand-out for a plan task. That a new file is made with these tables and stamped at head
is `test_sqlite_bootstrap.py`. Synthetic data only."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.models.handout import TaskHandout
from app.sites.models import (
    Lesson,
    Plan,
    PlanItem,
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
from tests.support import OWNER


def now():
    return datetime.now(UTC)


def site(url="https://careers.example.test/"):
    return Site(owner=OWNER, url=url, kind="employer", created_at=now())


async def test_a_site_is_on_by_default_and_one_url_is_one_site(js):
    async with db.session_factory()() as session:
        session.add(site())
        await session.commit()
        row = (await session.execute(select(Site))).scalar_one()
        assert row.on is True and row.kind == "employer"
        session.add(site())  # the same address again
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_site_project_runs_through_a_plan_its_items_tasks_and_a_check_report(js):
    async with db.session_factory()() as session:
        s = site()
        session.add(s)
        await session.flush()
        project = Project(owner=OWNER, site_id=s.id, title="Learn the site", outcome="Its listings can be read")
        session.add(project)
        await session.flush()
        first = PlanTask(
            owner=OWNER, project_id=project.id, plan_version=1, position=0, item_key="plan_v1",
            title="Write plan v1", group_label="Plan", action=PlanItemAction.plan, mode=PlanTaskMode.plan,
        )
        plan = Plan(
            owner=OWNER, project_id=project.id, version=1, summary="Read the list pages", method="Fetch and count",
            items=[
                PlanItem(position=0, key="find", title="Find the careers section", group_label="Find",
                         action=PlanItemAction.do),
                PlanItem(position=1, key="list", title="Read the list pages", group_label="Read",
                         action=PlanItemAction.do, data={"pages": 3}),
            ],
        )
        session.add_all([first, plan])
        await session.flush()
        for item in plan.items:
            session.add(PlanTask(
                owner=OWNER, project_id=project.id, plan_id=plan.id, plan_version=1, position=item.position + 1,
                item_key=item.key, title=item.title, group_label=item.group_label, action=item.action,
            ))
        session.add(ReviewReport(
            owner=OWNER, project_id=project.id, plan_version=1, verdict="iterate", summary="One page missing",
            findings_summary={"paging": "stopped after page 2"},
        ))
        await session.commit()

    async with db.session_factory()() as session:
        project = (await session.execute(
            select(Project).options(selectinload(Project.plans).selectinload(Plan.items), selectinload(Project.tasks))
        )).scalar_one()
        assert (project.created_by, project.round_budget, project.priority) == ("agent", 5, 0)
        assert project.site_id is not None
        (plan,) = project.plans
        assert plan.status is PlanStatus.awaiting_approval
        assert [i.key for i in sorted(plan.items, key=lambda i: i.position)] == ["find", "list"]
        by_key = {i.key: i for i in plan.items}
        assert by_key["find"].data == {} and by_key["list"].data == {"pages": 3}
        tasks = sorted(project.tasks, key=lambda t: t.position)
        assert [t.item_key for t in tasks] == ["plan_v1", "find", "list"]
        assert tasks[0].plan_id is None and tasks[0].mode is PlanTaskMode.plan
        assert all(t.status is PlanTaskStatus.todo and not t.superseded for t in tasks)
        assert all(t.mode is PlanTaskMode.do for t in tasks[1:])
        report = (await session.execute(select(ReviewReport))).scalar_one()
        assert report.verdict == "iterate" and report.findings_summary == {"paging": "stopped after page 2"}
        # status is stored as text, not a database enum type
        stored = (await session.execute(text("SELECT status, mode FROM plan_tasks WHERE item_key = 'find'"))).one()
        assert tuple(stored) == ("todo", "do")

        session.add(Plan(owner=OWNER, project_id=project.id, version=1, summary="again", method="again"))
        with pytest.raises(IntegrityError):  # one plan per version
            await session.commit()


async def test_a_lesson_is_retired_not_deleted_and_survives_its_project(js):
    async with db.session_factory()() as session:
        project = Project(owner=OWNER, title="p", outcome="o")
        session.add(project)
        await session.flush()
        session.add(Lesson(
            owner=OWNER, text="This site loads ten more on scroll: use its data request.", applies_to="employer",
            source="plan v1: the paging check", source_project_id=project.id, plan_version=1, subject="paging",
        ))
        await session.commit()
        lesson = (await session.execute(select(Lesson))).scalar_one()
        assert lesson.retired_at is None
        lesson.retired_at, lesson.retired_by = now(), "plan v2: paging was by a next link"
        await session.commit()
        await session.delete(project)
        await session.commit()
        kept = (await session.execute(select(Lesson).execution_options(populate_existing=True))).scalar_one()
        assert kept.source_project_id is None and kept.retired_by.startswith("plan v2")


async def test_a_site_keeps_each_version_of_its_skill_once(js):
    async with db.session_factory()() as session:
        s = site()
        other = site("https://boards.example.test/")
        session.add_all([s, other])
        await session.flush()
        project = Project(owner=OWNER, site_id=s.id, title="Learn the site", outcome="Its listings can be read")
        session.add(project)
        await session.flush()
        session.add_all([
            SiteSkill(owner=OWNER, site_id=s.id, version=1, recipe={"kind": "list_pages", "url": s.url},
                      created_at=now()),
            SiteSkill(owner=OWNER, site_id=s.id, version=2, recipe={"kind": "data_request"},
                      made_by_project_id=project.id, created_at=now()),
            SiteSkill(owner=OWNER, site_id=other.id, version=1, created_at=now()),  # another site's own version 1
        ])
        await session.commit()
        versions = (await session.execute(
            select(SiteSkill).where(SiteSkill.site_id == s.id).order_by(SiteSkill.version)
        )).scalars().all()
        assert [v.version for v in versions] == [1, 2]
        assert versions[0].recipe == {"kind": "list_pages", "url": s.url} and versions[0].retired_at is None
        assert (await session.get(SiteSkill, versions[0].id)).recipe["kind"] == "list_pages"
        versions[0].retired_at = now()
        await session.commit()
        session.add(SiteSkill(owner=OWNER, site_id=s.id, version=2, created_at=now()))
        with pytest.raises(IntegrityError):  # a version is made once, retired or not
            await session.commit()


async def test_a_hand_out_can_be_for_a_plan_task_instead_of_a_search_run(js):
    async with db.session_factory()() as session:
        project = Project(owner=OWNER, title="p", outcome="o")
        session.add(project)
        await session.flush()
        task = PlanTask(
            owner=OWNER, project_id=project.id, plan_version=1, position=0, item_key="plan_v1", title="Write plan v1",
            group_label="Plan", action=PlanItemAction.plan, mode=PlanTaskMode.plan,
        )
        session.add(task)
        await session.flush()
        session.add(TaskHandout(
            owner=OWNER, plan_task_id=task.id, via="test", payload={"title": "Write plan v1"}, payload_sha="0" * 64,
            chars=24, sent_at=now(),
        ))
        await session.commit()
        sent = (await session.execute(select(TaskHandout))).scalar_one()
        assert sent.plan_task_id == task.id and sent.task_id is None
        await session.delete(project)  # the project's tasks go, and the hand-outs sent for them
        await session.commit()
        assert (await session.execute(select(TaskHandout))).first() is None
