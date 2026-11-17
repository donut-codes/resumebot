"""Builders for the site-project tests: a site, a project, a plan of items, the scout taking and ending each, and a
first round run to Act. Made-up sites only."""

from datetime import UTC, datetime

from process_engine import live
from process_engine.models import ProcHistory, ProcInstance
from sqlalchemy import func, select

from app.agents import roster
from app.jobsearch import adapters, ats
from app.models.work import TrailEvent
from app.sites import (
    agent_calls,
    check_flow,
    completion,
    projects,
    registry,
    round_process,
)
from app.sites.models import (
    PlanItemAction,
    Project,
    Site,
)
from tests.support import OWNER


def _open_systems() -> list[str]:
    """The job systems the registry holds whose terms allow the app or are silent, a board of which one name (`slug`)
    makes, in the order the runbooks ask them: what the made-up worlds of these tests stand on, taken from the runbooks
    and never typed here, so that no test names a job system."""
    now = registry.current()
    return [r.key for r in now.runbooks if r.kind == "job_system" and now.verdict(r.hosts[0]) in ("allows", "silent")
            and ats.SYSTEMS[r.key].needs == ("slug",)]


#: Three job systems the app reads in code (the third one no employer of a test uses), and one it has no reader for
#: (its employers' sites wait for the browser).
SYSTEM_A, SYSTEM_B, SYSTEM_C = [key for key in _open_systems() if key in adapters.ADAPTERS][:3]
UNREAD = next(key for key in _open_systems() if key not in adapters.READERS)
#: How a system is named on a screen (`Off: <label> is switched off`).
LABEL_A, LABEL_B, LABEL_UNREAD = ats.label(SYSTEM_A), ats.label(SYSTEM_B), ats.label(UNREAD)


def board_of(name: str) -> dict:
    """The board a made-up employer has on any of the three systems above: one name."""
    return {"slug": name.lower()}


def board_link(system: str, name: str) -> str:
    """The address of a made-up employer's board on one of the two systems the app reads, as the system itself writes
    it."""
    found = ats.board_url(system, board_of(name))
    assert found is not None
    return found


def posting_link(system: str, name: str, number: int) -> str:
    """The address of one posting on that board (the shape every system above gives a posting: the board, `/jobs/`, an
    id)."""
    return f"{board_link(system, name)}/jobs/{number}"


def unread_link(name: str, number: int = 1) -> str:
    """A link on the system the app has no reader for: a page of its own domain, under the employer's name."""
    return f"https://{name.lower()}.{ats.SYSTEMS[UNREAD].domains[0]}/jobs/{number}"


ROUND, DO, CHECK = round_process.KEY, round_process.ITEM_KEY, round_process.CHECK_KEY
VALIDATION = {
    "done_means": "Postings came back and their number matches what the page says.",
    "how_to_check": "Fetch the list page and count the postings.",
    "off_plan_means": "No postings came back, or the number is off.",
    "tools_needed": [],
}
SUMMARY = {
    "planned": "Read the list pages.", "done": "Read two pages.", "checks": "One was off plan.",
    "learned": "The site loads more on scroll.", "next": "Use its data request.",
}


def item(key: str, kind: str | None = "site.read", **data) -> dict:
    if kind == "site.read":  # the kind the app ships for site projects names the page to read
        data = {"url": f"https://careers.example.test/{key}", **data}
    return {"key": key, "title": f"Read {key}", "group": "Do", "action": "do", "kind": kind, "data": data}


async def new_site(session, url: str = "https://careers.example.test/") -> Site:
    site = Site(owner=OWNER, url=url, kind="employer", created_at=datetime.now(UTC))
    session.add(site)
    await session.flush()
    return site


async def new_project(session, **more) -> Project:
    site = await new_site(session)
    return await projects.create_project(
        session, OWNER, site.id, "Learn the site", "Its listings can be read", created_by="you", **more)


async def propose(session, project, *items, **more):
    return await agent_calls.propose_plan(
        session, project, summary="Read the site", method="Fetch the pages and count",
        items=list(items) or [item("list")], validation=VALIDATION,
        problem="Nothing is known of how the site lists its roles.",
        proposal=["Read its list pages"], **more)


async def revise(session, project, based_on: int, *items, **more):
    return await agent_calls.revise_plan(
        session, project, based_on_version=based_on, summary="Read it again", changes="The paging was missed",
        method="Follow the next-page link", items=list(items), problem="Page two was never read.",
        proposal=["Follow the paging"], **more)


async def run_of(session, project) -> ProcInstance:
    run = await round_process._live_run(session, project)
    assert run is not None
    return run


async def step_of(session, project) -> str | None:
    return await round_process._step_of(session, await run_of(session, project))


async def child_of(session, project, plan, key, process_key=DO) -> ProcInstance | None:
    rows = (await session.scalars(select(ProcInstance).where(
        ProcInstance.process_key == process_key, ProcInstance.parent_id.is_not(None),
        ProcInstance.subject == round_process.subject_of(OWNER, project.id),
    ))).all()
    return next((c for c in rows if (c.data or {}).get("item", {}).get("key") == key
                 and str(c.data.get("plan")) == str(plan.id)), None)


async def last_end(session, instance) -> str | None:
    return await live.last_end(session, instance)


async def trail(session, event: str) -> list[TrailEvent]:
    return list((await session.scalars(select(TrailEvent).where(TrailEvent.event == event))).all())


async def take(session, project, conn):
    """What the scout does at the start of a session: ask for its next task."""
    task, why = await completion.next_task_or_reason(session, project, conn.id)
    assert task is not None, why
    return task


async def run_items(session, project, conn, ends: dict[str, str]):
    """The scout takes each item of the approved plan and ends it: `{key: "done" | "<why it went off plan>"}`."""
    for _ in ends:
        task = await take(session, project, conn)
        assert task.action == PlanItemAction.do
        said = ends[task.item_key]
        if said == "done":
            await completion.complete_task(
                session, task, project, "done", evidence=f"read {task.item_key}: 12 postings")
        else:
            await completion.complete_task(session, task, project, "off_plan", reason=said, evidence="no")


async def first_round(session, check_says_map, *, ends=None, differences=None, **project_more):
    """A project with a plan of items `a` and `b` that started on its own, run through its items to the checks, report
    and Act: returns `(project, plan, conn, act)`."""
    project = await new_project(session, **project_more)
    conn = await roster.ensure_roster(session, OWNER)
    sent = await propose(session, project, item("a"), item("b"))
    assert sent.started
    check_says_map.update(differences or {})
    await run_items(session, project, conn, ends or {"a": "done", "b": "done"})
    plan = sent.plan
    made, why = await check_flow.advance_plan_checks(session, project, plan)
    assert why == "act_ready" and made.action == PlanItemAction.act
    act = await take(session, project, conn)
    assert act.id == made.id
    return project, plan, conn, act


async def run_of_any(session, project) -> ProcInstance:
    """The project's newest run of the round, open or ended (runs made in one transaction share a timestamp, so the
    newest is the one with the latest history)."""
    rows = (await session.scalars(select(ProcInstance).where(
        ProcInstance.process_key == ROUND, ProcInstance.subject == round_process.subject_of(OWNER, project.id),
    ))).all()
    last = {r.id: await session.scalar(
        select(func.max(ProcHistory.id)).where(ProcHistory.instance_id == r.id)) for r in rows}
    return max(rows, key=lambda r: last[r.id])
