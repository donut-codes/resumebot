"""A site project, end to end, the way the scout lives it (docs/PRD.md path 29).

Everything here goes through the two doors the product has: the scout's MCP tools, called as `mcp_server` registers them
with the scout's bearer token (so the token check, the commit and the wake sent after it all run for real), and the
Inbox's answer route over HTTP for what only the owner can say. A project starts as the night's mission starts one, for
a site the owner switched on (`begin`). Nothing else reaches into the loop. The only things replaced are the two edges
the product cannot have in a test: the site (a fixture careers site behind `httpx.MockTransport`, swapped in at
`polite.make`) and the scout's wake (`wake.knock`, which records its knocks). The Inbox is wired as the app wires it at
start-up (`inbox_asks.install`).

Four stories: the happy path from a project's first plan to a finished project; a Check that finds the skill off plan,
so a second round closes it (and the round budget holds); the scout's own question to the owner; and a round that was
held after a failure, resumed with Try again. The owner is never asked about a plan. Synthetic data only: the site is
made up."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.main import app
from app.jobsearch.work import wake as scout_wake
from app.models.handout import TaskHandout
from app.settings import settings
from app.sites import agent_calls, asks, inbox_asks, projects, round_process, site_check, wake_ops
from app.sites.models import (
    Lesson,
    Plan,
    PlanItemAction,
    PlanStatus,
    PlanTask,
    PlanTaskStatus,
    Project,
    ReviewReport,
    Site,
    SiteSkill,
)

OWNER = "owner"  # the setting's default: the tools act for it
HOST = "https://careers.fixture.test"
LIST_URL = f"{HOST}/careers"
JOBS = {1: "Programs Director", 2: "Operations Lead", 3: "Data Analyst"}
KNOCK = "site_project"

VALIDATION = {
    "done_means": "Postings came back and their number matches what the page says.",
    "how_to_check": "Run the saved skill and count the postings.",
    "off_plan_means": "No postings came back, or the number is off.",
    "tools_needed": [],
}
PLAN = {
    "summary": "Learn to read the careers list", "method": "Build a recipe, try it on the live page, keep it",
    "problem": "Nothing is known of how this site lists its roles.", "proposal": ["Build a recipe for the list page"],
    "validation": VALIDATION,
}
ACT = {
    "planned": "Read the careers list.", "done": "Built and tried a reader.", "checks": "Every check was on plan.",
    "learned": "The list is one page of plain links.", "next": "Nothing: the skill is kept.",
}
RESOLUTION = {
    "problem": "The site's open roles could not be read by the app.",
    "done": "Built a reader for its careers list and checked it against the page.",
    "outcome": "Its open postings can be read: the check found all three.",
}


# --- the fixture site ---------------------------------------------------------------------------------------------


def _careers_page() -> str:
    links = "".join(f'<li><a href="/careers/jobs/{n}">{name}</a></li>' for n, name in JOBS.items())
    return f'<html><body><nav><a href="/about">About us</a></nav><ul>{links}</ul></body></html>'


class FixtureSite:
    """The made-up careers site: a list page of three postings, the same three as a JSON endpoint, one page for each
    posting, no robots.txt. Every path asked for is kept, in order."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.fetchers: list[Fetcher] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.asked.append(path)
        if path == "/careers":
            return httpx.Response(200, text=_careers_page())
        if path == "/api/jobs":
            rows = [{"id": n, "name": name, "path": f"/careers/jobs/{n}"} for n, name in JOBS.items()]
            return httpx.Response(200, json={"data": {"results": rows}})
        if path.startswith("/careers/jobs/"):
            return httpx.Response(200, text="a posting")
        return httpx.Response(404)  # robots.txt and anything else

    def make(self) -> Fetcher:
        self.fetchers.append(Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(self.handle)), per_host_seconds=0))
        return self.fetchers[-1]


def list_recipe(**over) -> dict:
    return {"kind": "list_page", "url": LIST_URL, "link_pattern": r"/careers/jobs/\d+", **over}


def json_recipe(**over) -> dict:
    return {"kind": "request", "method": "GET", "url": f"{HOST}/api/jobs", "items": "data.results",
            "fields": {"title": "name", "url": "path"}, "url_prefix": HOST, **over}


# --- the edges and the two doors ----------------------------------------------------------------------------------


@pytest.fixture
def site(monkeypatch):
    fixture = FixtureSite()
    monkeypatch.setattr(polite, "make", fixture.make)
    return fixture


@pytest.fixture
def knocks(monkeypatch):
    """The scout's wake, replaced: every knock is kept as `(project id, reason)`; `took()` hands over those since the
    last call, so a step can say exactly what it woke."""
    seen: list[tuple[str, str]] = []

    async def knock(project_id, reason="search_started"):
        seen.append((str(project_id), reason))
        return True

    def took() -> list[tuple[str, str]]:
        out = list(seen)
        seen.clear()
        return out

    monkeypatch.setattr(scout_wake, "knock", knock)
    return SimpleNamespace(took=took, seen=seen)


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
def everything(js, door, site, knocks, inbox, client):
    """The whole stage for one story."""
    return SimpleNamespace(door=door, site=site, knocks=knocks, client=client)


async def rows(model, *where):
    async with db.session_factory()() as session:
        return list((await session.scalars(select(model).where(*where))).all())


async def open_asks(client, kind: str | None = None) -> list[dict]:
    """What the owner sees in the Inbox, as the route lists it."""
    items = (await client.get("/api/v1/inbox")).json()["items"]
    return [i for i in items if kind is None or i["kind"] == kind]


async def owner_answers(client, item: dict, option: str, note: str | None = None):
    body = {"option": option, **({"note": note} if note else {})}
    return await client.post(f"/api/v1/tasks/{item['id']}/answer", json=body)


# --- what the scout does ------------------------------------------------------------------------------------------


def plan_item(key: str = "list", **data) -> dict:
    return {"key": key, "title": f"Read the {key}", "group": "Do", "action": "do", "kind": "site.read",
            "data": {"url": LIST_URL, **data}}


async def begin() -> str:
    """A project for the fixture site, started as the night's mission starts one (`autostart.ensure_projects`'s
    `create_project`): the owner's site, then the project, then the wake for its first plan after the commit."""
    async with db.session_factory()() as session:
        now = datetime.now(UTC)
        site = Site(owner=OWNER, url=LIST_URL, kind="employer", created_at=now, added_by="app", turned_on_at=now)
        session.add(site)
        await session.flush()
        host = HOST.removeprefix("https://")
        project = await projects.create_project(
            session, OWNER, site.id, f"Read {host}",
            f"Every open posting on {host} can be read by the app on its own, and a check passes", created_by="you")
        await session.commit()
    await wake_ops.send(project.id, agent_calls.Wake("project_created", "plan"))
    return str(project.id)


async def take(door, pid: str, action: str) -> dict:
    """`task.next`, and it hands over the kind of task the story expects."""
    reply = await mcp_server.task_next(door, pid)
    assert reply["ok"] is True, reply
    assert reply["task"]["action"] == action, reply["task"]["action"]
    return reply["task"]


async def propose(door, pid: str, *items: dict, **more) -> dict:
    return await mcp_server.plan_propose(door, pid, items=list(items) or [plan_item()], **{**PLAN, **more})


async def revise(door, pid: str, based_on: int, *items: dict, **more) -> dict:
    return await mcp_server.plan_revise(
        door, pid, based_on, summary="Read the list another way", changes="The first skill read nothing",
        items=list(items) or [plan_item()], method="Read the JSON endpoint the page loads from",
        problem="The list page's links were not where the first recipe looked.",
        proposal=["Read the site's data request"], validation=VALIDATION, **more)


async def do_item(door, pid: str, recipe: dict, evidence: str = "3 postings. expected count 3") -> dict:
    """Take the next item, try the recipe, and finish the item with it."""
    held = await take(door, pid, "do")
    tried = await mcp_server.skill_try(door, held["task_id"], recipe)
    assert tried["ok"] is True, tried
    return await mcp_server.task_complete(
        door, held["task_id"], "done", "Built and tried a reader", evidence, result={"count": tried["count"]},
        recipe=recipe)


async def waiting(door) -> list[list[str]]:
    return [w["waiting"] for w in (await mcp_server.work_next(door))["projects"]]


async def project_of(pid: str) -> Project:
    return (await rows(Project, Project.id == uuid.UUID(pid)))[0]


async def plan_of(pid: str, version: int) -> Plan:
    return (await rows(Plan, Plan.project_id == uuid.UUID(pid), Plan.version == version))[0]


async def step_of(pid: str) -> str | None:
    async with db.session_factory()() as session:
        run = await round_process._live_run(session, await session.get(Project, uuid.UUID(pid)))
        return await round_process._step_of(session, run) if run is not None else None


async def finish_the_round(door, pid: str, recipe: dict) -> dict:
    """From an approved plan to a finished project, as a scout with nothing in its way: the item, then Act."""
    done = await do_item(door, pid, recipe)
    assert done["state"] == "done" and done["next"]["reason"] == "the round was checked and Act is ready", done
    act = await take(door, pid, "act")
    ended = await mcp_server.act_complete(door, pid, act["task_id"], ACT, resolution=RESOLUTION)
    assert ended["ok"] is True and ended["next"] == "finished", ended
    return ended


# --- 1. the happy path --------------------------------------------------------------------------------------------


async def test_a_scout_learns_a_site_from_its_first_plan_to_a_finished_project_with_a_kept_skill(everything):
    door, site, knocks, client = everything.door, everything.site, everything.knocks, everything.client

    # a project for the owner's site, and the scout is woken for its first plan
    pid = await begin()
    assert knocks.took() == [(pid, KNOCK)]
    created = await project_of(pid)
    assert created.site_id is not None and created.created_by == "you"
    assert await waiting(door) == [["plan"]]

    # task.next hands the Write-plan task
    plan_task = await take(door, pid, "plan")
    assert (plan_task["title"], plan_task["report_with"]) == ("Write plan v1", "plan.propose")
    assert plan_task["context"]["site"] == {"url": LIST_URL, "kind": "employer", "skill": []}
    assert knocks.took() == []

    # plan.propose: nothing in it needs the owner, so the plan starts without asking
    sent = await propose(door, pid, plan_item())
    assert sent["ok"] is True and sent["status"] == "approved" and sent["started"] is True
    assert knocks.took() == [(pid, KNOCK)], "the scout is woken to do the item"
    assert await open_asks(client) == []
    assert await step_of(pid) == "PR-9"
    assert await waiting(door) == [["items"]]

    # task.next hands the item; skill.try reads the fixture site once and keeps nothing
    held = await take(door, pid, "do")
    assert (held["kind"], held["key"], held["data"]["url"]) == ("site.read", "list", LIST_URL)
    tried = await mcp_server.skill_try(door, held["task_id"], list_recipe())
    assert tried["ok"] is True and tried["count"] == 3 and tried["truncated"] is False
    assert tried["titles"] == list(JOBS.values())
    assert await rows(SiteSkill) == [] and await rows(ReviewReport) == []
    assert knocks.took() == []

    # task.complete: the recipe is kept as the skill, the Check (code) runs it on the site and finds it on plan, the
    # report is written, and Act is ready
    done = await mcp_server.task_complete(
        door, held["task_id"], "done", "Read the list page with a list_page recipe",
        "3 postings: Programs Director, Operations Lead, Data Analyst. expected count 3", result={"count": 3},
        recipe=list_recipe())
    assert (done["state"], done["skill_version"]) == ("done", 1)
    assert done["next"] == {"task": None, "reason": "the round was checked and Act is ready"}
    assert knocks.took() == [(pid, KNOCK)], "Act is due: the scout is woken for it"
    [skill] = await rows(SiteSkill)
    assert (skill.version, skill.recipe["kind"], skill.retired_at, str(skill.made_by_project_id)) == (
        1, "list_page", None, pid)
    [check] = await rows(PlanTask, PlanTask.action == PlanItemAction.review_check)
    assert check.status == PlanTaskStatus.done and check.data["checked_by"] == "code"
    assert check.findings[0]["verdict"] == "ok"
    assert "/careers/jobs/1" in site.asked and site.asked.count("/careers") >= 2, "tried once, then checked"
    assert all(f.client.is_closed for f in site.fetchers)
    [report] = await rows(ReviewReport)
    assert (report.verdict, report.plan_version) == ("accepted", 1)
    assert await waiting(door) == [["act"]]

    # task.next hands Act, with the report and the outcome to judge by
    act = await take(door, pid, "act")
    assert act["context"]["report"]["verdict"] == "accepted" and act["context"]["closing"]["outcome"]
    assert act["context"]["site"]["skill"] == [{"version": 1, "retired": False}]
    lesson = await mcp_server.lesson_record(
        door, pid, act["task_id"], "This site lists every posting as a plain link on one page.", "project",
        "item list: the report", subject="listing")
    assert lesson["ok"] is True and lesson["created"] is True

    # act.complete with a resolution that meets the outcome: the project is finished
    ended = await mcp_server.act_complete(door, pid, act["task_id"], ACT, resolution=RESOLUTION)
    assert ended["ok"] is True and ended["next"] == "finished" and ended["lessons_recorded"] == 1
    assert knocks.took() == [], "nothing is left to wake anyone for"
    project = await project_of(pid)
    assert project.finished_at is not None and project.resolution["outcome"] == RESOLUTION["outcome"]
    assert await step_of(pid) is None
    assert (await mcp_server.task_next(door, pid))["reason"] == "this project is finished"
    assert await waiting(door) == []

    # what stays: the skill, the lesson, the report, and a copy of everything the scout was handed
    assert [(s.version, s.made_by_project_id == project.id) for s in await rows(SiteSkill)] == [(1, True)]
    listed = await mcp_server.lesson_list(door, pid)
    assert [row["text"] for row in listed["lessons"]] == ["This site lists every posting as a plain link on one page."]
    assert [lesson.applies_to for lesson in await rows(Lesson)] == ["project"]
    handed = await rows(TaskHandout, TaskHandout.plan_task_id.is_not(None))
    assert {h.plan_task_id for h in handed} == {
        uuid.UUID(plan_task["task_id"]), uuid.UUID(held["task_id"]), uuid.UUID(act["task_id"])}
    assert {h.via for h in handed} == {"task_next"} and all(h.chars > 0 for h in handed)
    assert not [h for h in await rows(TaskHandout) if h.plan_task_id is None], "every copy says what it was for"


# --- 2. the Check finds the skill off plan: the second round closes it -------------------------------------------


async def first_round_that_reads_nothing(door, pid: str) -> dict:
    """Plan v1 and its item, with a recipe whose link pattern matches nothing on the page (the scout takes the page's
    three roles for granted). Returns the Act task of the round."""
    await take(door, pid, "plan")
    assert (await propose(door, pid, plan_item()))["started"] is True
    held = await take(door, pid, "do")
    wrong = list_recipe(link_pattern=r"/openings/\d+")
    tried = await mcp_server.skill_try(door, held["task_id"], wrong)
    assert tried["ok"] is True and tried["count"] == 0 and "no postings" in tried["note"]
    done = await mcp_server.task_complete(
        door, held["task_id"], "done", "Built a reader for the page", "The page lists 3 openings. expected count 3",
        result={"count": 3}, recipe=wrong)
    assert done["skill_version"] == 1 and done["next"]["reason"] == "the round was checked and Act is ready"
    return await take(door, pid, "act")


async def test_a_check_that_finds_nothing_sends_the_project_round_two_which_reads_the_site_and_finishes(everything):
    door, site, knocks, client = everything.door, everything.site, everything.knocks, everything.client
    pid = await begin()
    knocks.took()
    act = await first_round_that_reads_nothing(door, pid)

    # the Check ran the skill on the fixture site in code and found it off plan, with its findings
    [check] = await rows(PlanTask, PlanTask.action == PlanItemAction.review_check)
    assert check.data["checked_by"] == "code" and check.findings[0]["verdict"] == "issue"
    assert "Nothing came back" in check.findings[0]["note"]
    assert "/careers" in site.asked and "/careers/jobs/1" not in site.asked, "no posting was found to open"
    [report] = await rows(ReviewReport)
    assert (report.verdict, report.plan_version) == ("iterate", 1)
    assert act["context"]["report"]["verdict"] == "iterate" and "closing" not in act["context"]
    assert (await mcp_server.review_read(door, pid))["report"]["verdict"] == "iterate"

    # Act: a lesson, then the round ends and asks for the next plan
    knocks.took()
    lesson = "The list page links its postings under /careers/jobs/, not /openings/."
    assert (await mcp_server.lesson_record(door, pid, act["task_id"], lesson, "project", "item list: the report"))["ok"]
    ended = await mcp_server.act_complete(door, pid, act["task_id"], ACT)
    assert ended["ok"] is True and ended["next"] == "plan_ready" and "finished" not in str(ended["next"])
    assert knocks.took() == [(pid, KNOCK)], "a fresh session is woken to write plan v2"
    assert (await project_of(pid)).finished_at is None and await waiting(door) == [["plan"]]

    # Write plan v2 carries the item that is still unresolved, and the lesson
    second = await take(door, pid, "plan")
    assert second["title"] == "Write plan v2" and second["report_with"] == "plan.revise"
    assert [r["key"] for r in second["context"]["unresolved_items"]] == ["list"]
    assert "Nothing came back" in second["context"]["unresolved_items"][0]["found"]
    assert [row["text"] for row in second["context"]["lessons"]] == [lesson]
    left_out = await revise(door, pid, 1, plan_item("other"))
    assert left_out["ok"] is False and "doesn't account for 1 unresolved item" in left_out["reason"]
    sent = await revise(door, pid, 1, plan_item())
    assert sent["ok"] is True and sent["started"] is True and sent["plan_version"] == 2
    assert knocks.took() == [(pid, KNOCK)]

    # round 2: the corrected skill (the site's data request) reads all three; the old version is retired
    done = await do_item(door, pid, json_recipe())
    assert done["skill_version"] == 2 and done["next"]["reason"] == "the round was checked and Act is ready"
    assert sorted((s.version, s.retired_at is not None, s.recipe["kind"]) for s in await rows(SiteSkill)) == [
        (1, True, "list_page"), (2, False, "request")]
    final = await take(door, pid, "act")
    assert final["context"]["report"]["verdict"] == "accepted" and final["context"]["closing"]["outcome"]
    assert final["context"]["site"]["skill"] == [{"version": 1, "retired": True}, {"version": 2, "retired": False}]
    ended = await mcp_server.act_complete(door, pid, final["task_id"], ACT, resolution=RESOLUTION)
    assert ended["ok"] is True and ended["next"] == "finished"

    project = await project_of(pid)
    assert project.finished_at is not None and project.resolution["outcome"] == RESOLUTION["outcome"]
    reports = await rows(ReviewReport, ReviewReport.project_id == project.id)
    assert sorted((r.plan_version, r.verdict) for r in reports) == [(1, "iterate"), (2, "accepted")]
    assert len(reports) <= project.round_budget == 5, "two rounds, within the budget"
    assert await open_asks(client) == [], "the owner was never asked: nothing in either plan needed them"


async def test_a_second_round_past_the_cap_asks_the_owner_nothing_and_the_app_gives_the_site_up(
        everything, monkeypatch):
    from app.sites import autostart

    monkeypatch.setattr(autostart, "ROUND_CAP", 1)
    door, knocks, client = everything.door, everything.knocks, everything.client
    pid = await begin()
    knocks.took()
    act = await first_round_that_reads_nothing(door, pid)
    assert (await mcp_server.act_complete(door, pid, act["task_id"], ACT))["next"] == "plan_ready"
    knocks.took()
    await take(door, pid, "plan")

    await revise(door, pid, 1, plan_item())
    assert knocks.took() == [], "a round past the cap does not start"
    assert await open_asks(client) == [], "the owner is never asked about rounds"
    project = await project_of(pid)
    site = (await rows(Site))[0]
    assert project.finished_at is not None
    assert (site.on, site.off_by, site.off_note) == (False, "app", autostart.TOO_MANY_CLAUSE)
    assert await waiting(door) == [] and (await mcp_server.task_next(door, pid))["ok"] is False


# --- 2b. what the Check reads of the item ------------------------------------------------------------------------


async def test_a_site_the_page_says_has_no_postings_is_on_plan_and_an_item_that_went_off_plan_is_checked_as_that(
        everything):
    """The Check was handed only the item's key, title and kind, so an empty site always failed with "Nothing
    came back", and an item that had gone off plan was judged on what the site gave."""
    door, knocks = everything.door, everything.knocks
    pid = await begin()
    knocks.took()
    await take(door, pid, "plan")
    assert (await propose(door, pid, plan_item("empty"), plan_item("blocked")))["started"] is True
    first = await take(door, pid, "do")
    nothing = list_recipe(link_pattern=r"/openings/\d+")
    await mcp_server.task_complete(
        door, first["task_id"], "done", "Built a reader", "The page says it has no openings",
        result={"count": 0, "expected_count": 0}, recipe=nothing)
    second = await take(door, pid, "do")
    await mcp_server.task_complete(
        door, second["task_id"], "off_plan", "Could not build one", "The page did not load",
        off_plan_reason="The listings come from a script the app cannot run", tried=["the page's source"])
    checks = {c.data["key"]: c for c in await rows(PlanTask, PlanTask.action == PlanItemAction.review_check)}
    assert checks["empty"].findings[0]["verdict"] == "ok", "the page's own count of none is what the check expected"
    assert checks["blocked"].findings[0]["verdict"] == "issue"
    assert checks["blocked"].findings[0]["note"] == "The listings come from a script the app cannot run"


# --- 3. the scout's own question ----------------------------------------------------------------------------------


async def test_a_scouts_question_in_the_middle_of_an_item_goes_to_the_owner_and_the_answer_comes_back_with_it(
        everything):
    door, knocks, client = everything.door, everything.knocks, everything.client
    pid = await begin()
    await take(door, pid, "plan")
    await propose(door, pid, plan_item())
    held = await take(door, pid, "do")
    knocks.took()

    options = [
        {"id": "json", "label": "Read the data request", "description": "Use the JSON the page loads from."},
        {"id": "links", "label": "Read the page's links", "description": "Use the plain links on the list page."},
    ]
    asked = await mcp_server.task_escalate(
        door, held["task_id"], "The page offers a data request and plain links. Which should the skill read?",
        ["the page's source", "its data request"], "only_you_decide", options=options, recommended="json")
    assert asked == {"ok": True, "asked": "the owner", "next": "the task waits for their answer. Stop now"}
    assert knocks.took() == [], "the scout stops; nobody is woken for a question the owner has to answer"
    [ask] = await open_asks(client, "site_escalation")
    assert ask["title"].startswith("The page offers a data request") and ask["recommended"] == "json"
    assert [o["id"] for o in ask["options"]] == ["json", "links"] and "If you don't answer" not in ask["detail"]
    assert (await rows(PlanTask, PlanTask.id == uuid.UUID(held["task_id"])))[0].status == PlanTaskStatus.waiting
    assert await waiting(door) == []
    blocked = await mcp_server.task_next(door, pid)
    assert blocked["ok"] is False and blocked["reason"] == "your question to the owner is waiting for their answer"

    res = await owner_answers(client, ask, "json", "Use it, and check it against the page.")
    assert res.status_code == 200 and res.json()["state"] == "done"
    assert knocks.took() == [(pid, KNOCK)], "the scout is woken with its answer"
    task = (await rows(PlanTask, PlanTask.id == uuid.UUID(held["task_id"])))[0]
    assert task.status == PlanTaskStatus.todo and task.checked_out_by is None
    assert task.data["escalation"]["answer"] == {
        "option": "json", "label": "Read the data request", "note": "Use it, and check it against the page."}
    assert await waiting(door) == [["items"]] and await open_asks(client) == []

    # the same task is handed out again, with what the owner said
    again = await take(door, pid, "do")
    assert again["task_id"] == held["task_id"]
    assert again["owner_answer"]["question"].startswith("The page offers a data request")
    assert again["owner_answer"]["answer"] == task.data["escalation"]["answer"]
    assert "escalation" not in (again["data"] or {}), "the machinery's record stays out of the item's data"

    done = await mcp_server.task_complete(
        door, again["task_id"], "done", "Read the data request", "3 postings. expected count 3", result={"count": 3},
        recipe=json_recipe())
    assert done["skill_version"] == 1 and done["next"]["reason"] == "the round was checked and Act is ready"
    act = await take(door, pid, "act")
    assert (await mcp_server.act_complete(door, pid, act["task_id"], ACT, resolution=RESOLUTION))["next"] == "finished"


async def test_a_fact_only_the_owner_knows_is_answered_in_the_note_box_and_a_second_question_reaches_them_too(
        everything):
    """`only_you_know` and `only_you_can_do` used to reach the Inbox with no choice, so the card could only be
    marked Done (the task stayed waiting), and a second question about the same task never came back to the owner."""
    door, knocks, client = everything.door, everything.knocks, everything.client
    pid = await begin()
    await take(door, pid, "plan")
    await propose(door, pid, plan_item())
    held = await take(door, pid, "do")
    knocks.took()

    await mcp_server.task_escalate(door, held["task_id"], "What is the company's careers address?", ["the footer"],
                                   "only_you_know")
    [ask] = await open_asks(client, "site_escalation")
    assert [(o["id"], bool(o.get("note"))) for o in ask["options"]] == [("answer", True)], "the existing note box"
    assert (await client.post(f"/api/v1/tasks/{ask['id']}/done", json={})).status_code == 409
    assert (await client.post(f"/api/v1/tasks/{ask['id']}/skip", json={})).status_code == 409
    assert (await owner_answers(client, ask, "answer")).status_code == 409, "the box is the answer"
    assert (await rows(PlanTask, PlanTask.id == uuid.UUID(held["task_id"])))[0].status == PlanTaskStatus.waiting
    assert [a["id"] for a in await open_asks(client, "site_escalation")] == [ask["id"]], "still open"

    assert (await owner_answers(client, ask, "answer", "It is the page under /careers.")).status_code == 200
    assert knocks.took() == [(pid, KNOCK)]
    again = await take(door, pid, "do")
    assert again["owner_answer"]["answer"]["note"] == "It is the page under /careers."

    # the same task asks again: a new question for the owner, not the finished one
    await mcp_server.task_escalate(door, again["task_id"], "Which of the two pages should it read?", ["both pages"],
                                   "only_you_can_do")
    [second] = await open_asks(client, "site_escalation")
    assert second["id"] != ask["id"] and second["title"].startswith("Which of the two pages")
    assert [o["id"] for o in second["options"]] == ["did_it"]
    assert (await owner_answers(client, second, "did_it")).status_code == 200
    assert (await rows(PlanTask, PlanTask.id == uuid.UUID(held["task_id"])))[0].status == PlanTaskStatus.todo
    assert await open_asks(client) == []


# --- 4. a round that is held after a failure ----------------------------------------------------------------------


async def test_a_plan_that_could_not_be_taken_is_held_in_the_inbox_and_try_again_takes_it(everything, monkeypatch):
    door, knocks, client = everything.door, everything.knocks, everything.client
    real = round_process.start_facts
    fault = {"on": True}

    async def broken(session, project, plan):
        if fault["on"]:
            raise RuntimeError("the facts could not be read")
        return await real(session, project, plan)

    monkeypatch.setattr(round_process, "start_facts", broken)
    pid = await begin()
    await take(door, pid, "plan")
    knocks.took()

    sent = await propose(door, pid, plan_item())
    assert sent["ok"] is True and sent["status"] == "awaiting_approval" and "started" not in sent
    assert knocks.took() == [], "the round is held: no one is woken for work that is not there"
    assert await step_of(pid) == "PR-4" and await waiting(door) == []
    [held] = await open_asks(client, "site_failed")
    assert held["title"] == "resumebot couldn't take a plan: something unexpected got in the way"
    assert [o["id"] for o in held["options"]] == ["try_again", "leave_it"] and held["recommended"] == "try_again"
    assert "the facts could not be read" not in held["title"] + (held["detail"] or "")
    assert (await mcp_server.task_next(door, pid))["ok"] is False

    fault["on"] = False
    res = await owner_answers(client, held, "try_again")
    assert res.status_code == 200 and res.json()["answer"] == "try_again"
    assert knocks.took() == [(pid, KNOCK)], "the scout is woken for the plan that is now going"
    assert (await plan_of(pid, 1)).status == PlanStatus.approved and await step_of(pid) == "PR-9"
    assert await open_asks(client) == []
    await finish_the_round(door, pid, list_recipe())
    assert (await project_of(pid)).finished_at is not None


async def test_a_check_that_broke_is_held_in_the_inbox_and_try_again_makes_it(everything, monkeypatch):
    door, knocks, client = everything.door, everything.knocks, everything.client
    real = site_check.site_check
    fault = {"on": True}

    async def broken(session, instance, data):
        if fault["on"]:
            raise RuntimeError("the check could not run")
        return await real(session, instance, data)

    monkeypatch.setattr(site_check, "site_check", broken)
    pid = await begin()
    await take(door, pid, "plan")
    await propose(door, pid, plan_item())
    knocks.took()

    done = await do_item(door, pid, list_recipe())
    assert done["state"] == "done" and done["skill_version"] == 1
    assert await rows(ReviewReport) == [], "no report: the check did not run"
    assert await waiting(door) == [], "the checks are still due, but a failure holds them: no session is spent on it"
    [held] = await open_asks(client, "site_failed")
    assert held["title"].startswith("resumebot couldn't ") and "the check could not run" not in held["title"]

    fault["on"] = False
    knocks.took()
    assert (await owner_answers(client, held, "try_again")).status_code == 200
    assert knocks.took() == [(pid, KNOCK)]
    [report] = await rows(ReviewReport)
    assert report.verdict == "accepted" and await open_asks(client) == []
    act = await take(door, pid, "act")
    ended = await mcp_server.act_complete(door, pid, act["task_id"], ACT, resolution=RESOLUTION)
    assert ended["next"] == "finished" and (await project_of(pid)).finished_at is not None
