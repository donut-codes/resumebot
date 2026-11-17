"""The scout's site tools (docs/PRD.md path 29): `work.next`,
`task.next`, `plan.propose`, `task.complete`, `skill.try`, `task.escalate`, `lesson.record`, `lesson.retire`,
`act.complete` and the read tools, called the way the MCP server calls them (a context with request headers), as
`test_mcp.py` does. Every site is made up and sits behind a mock transport. What is checked: the door's rules (a token,
refusals as results, replies under the budget, a wake sent after the commit), that the scout is handed its plan task
and then its items, that a recipe sent with a done item is kept as the site's
next version before the item's Check reads it, that `skill.try` reads a site once and writes nothing, that an
escalation becomes an owner ask, and that no tool writes a check result."""

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.missions import search_run as run
from app.jobsearch.work import session_kinds
from app.models.agent import AgentConnection
from app.models.work import Notification, Task, TrailEvent
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, payload_chars
from app.sites import asks, projects
from app.sites import tools as site_tools
from app.sites.models import (
    Lesson,
    Plan,
    PlanItemAction,
    PlanTask,
    PlanTaskStatus,
    Project,
    ReviewReport,
    Site,
    SiteSkill,
)
from tests.support import load_profile

OWNER = "owner"  # the setting's default: the tools act for it
HOST = "https://jobs.example.test"
SITE_URL = f"{HOST}/careers"
VALIDATION = {
    "done_means": "Postings came back and their number matches what the page says.",
    "how_to_check": "Fetch the data request and count the postings.",
    "off_plan_means": "No postings came back, or the number is off.",
    "tools_needed": [],
}
SUMMARY = {
    "planned": "Read the site's data request.", "done": "Built and tried a reader.",
    "checks": "Every check was on plan.", "learned": "The listings come from one data request.",
    "next": "Nothing: the skill is kept.",
}


@pytest.fixture
def asked(monkeypatch):
    """The owner asks the loop raises, in place of the Inbox's: the keyword arguments of each."""
    seen: list[dict] = []

    async def fake(session, project, **kw):
        seen.append({"project": project, **kw})
        return None

    asks.set_ask_owner(fake)
    yield seen
    asks.set_ask_owner(None)


# --- the made-up site ---------------------------------------------------------------------------------------------


def _job(n: int) -> dict:
    return {"id": n, "name": f"Role {n}", "path": f"/jobs/{n}"}


def _recipe(**over) -> dict:
    recipe = {
        "kind": "request", "method": "GET", "url": f"{HOST}/api/jobs", "items": "data.results",
        "fields": {"title": "name", "url": "path"}, "url_prefix": HOST,
    }
    return {**recipe, **over}


def _world(count: int = 4, robots: str | None = None):
    rows = [_job(n) for n in range(1, count + 1)]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots) if robots is not None else httpx.Response(404)
        if request.url.path == "/api/jobs":
            if "offset" in request.url.params:  # a site that pages: two rows to a page
                start = int(request.url.params["offset"])
                return httpx.Response(200, json={"data": {"results": rows[start:start + 2]}})
            return httpx.Response(200, json={"data": {"results": rows}})
        if request.url.path.startswith("/jobs/"):
            return httpx.Response(200, text="a posting")
        return httpx.Response(404)

    return handle


def _use(monkeypatch, handle):
    """Every read the app makes (the tool's own, the Check's) goes to the made-up site; returns the fetchers made."""
    made: list[Fetcher] = []

    def make() -> Fetcher:
        made.append(Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0))
        return made[-1]

    monkeypatch.setattr(polite, "make", make)
    return made


def item(key: str) -> dict:
    return {"key": key, "title": f"Read {key}", "group": "Do", "action": "do", "kind": "site.read",
            "data": {"url": SITE_URL}}


async def start() -> str:
    """A project for the made-up site, started the way the night's mission starts one: the owner's site, then the
    project with its first planning task waiting for the scout."""
    async with db.session_factory()() as session:
        now = datetime.now(UTC)
        site = Site(owner=OWNER, url=SITE_URL, kind="employer", created_at=now, added_by="app", turned_on_at=now)
        session.add(site)
        await session.flush()
        host = HOST.removeprefix("https://")
        project = await projects.create_project(
            session, OWNER, site.id, f"Read {host}",
            f"Every open posting on {host} can be read by the app on its own, and a check passes", created_by="you")
        await session.commit()
    return str(project.id)


async def plan(door, project_id: str, *keys: str) -> dict:
    """The scout takes its Write-plan task and proposes a plan of these items."""
    taken = await mcp_server.task_next(door, project_id)
    assert taken["ok"] is True and taken["task"]["action"] == "plan", taken
    sent = await mcp_server.plan_propose(
        door, project_id, summary="Read the site", method="Replay its data request and count",
        items=[item(k) for k in keys or ("list",)], validation=VALIDATION,
        problem="Nothing is known of how the site lists its roles.", proposal=["Replay its data request"],
    )
    assert sent["ok"] is True, sent
    return sent


async def rows(model, *where):
    async with db.session_factory()() as session:
        return list((await session.scalars(select(model).where(*where))).all())


# --- the contract of the door -------------------------------------------------------------------------------------


@pytest.mark.parametrize("header", [None, "Bearer wrong-token", "Basic abc"])
async def test_a_site_tool_without_the_scouts_token_is_refused_and_does_nothing(js, door, knocks, header):
    ctx = SimpleNamespace(headers={"authorization": header} if header is not None else {})
    for call in (mcp_server.work_next(ctx), mcp_server.task_next(ctx, str(uuid.uuid4())),
                 mcp_server.doc_read(ctx, "site.skill"), mcp_server.skill_try(ctx, str(uuid.uuid4()), _recipe())):
        result = await call
        assert result["ok"] is False and "token" in result["reason"]
    assert await rows(Project) == [] and await rows(Site) == [] and knocks == []


async def test_a_refusal_is_an_ordinary_result_with_the_line_that_fixes_it(js, door):
    refused = await mcp_server.task_next(door, "not-an-id")
    assert refused["ok"] is False and "project_id" in refused["reason"] and refused["how_to_fix"]
    unknown = await mcp_server.task_next(door, str(uuid.uuid4()))
    assert unknown == {"ok": False, "reason": "no such project", "how_to_fix": "work.next lists your projects"}


# --- work.next and the runner's answer ----------------------------------------------------------------------------


async def test_work_next_lists_the_projects_with_something_waiting_and_says_what(js, door):
    nothing = await mcp_server.work_next(door)
    assert nothing["projects"] == [] and "Stop here" in nothing["note"]
    pid = await start()
    waiting = (await mcp_server.work_next(door))["projects"]
    assert waiting == [{
        "project_id": pid, "title": f"Read {HOST.removeprefix('https://')}", "waiting": ["plan"], "open_tasks": 1,
        "site": {"url": SITE_URL, "kind": "employer"},
    }]
    await plan(door, pid, "a", "b")
    waiting = (await mcp_server.work_next(door))["projects"]
    assert [(w["waiting"], w["open_tasks"]) for w in waiting] == [(["items"], 2)]


async def test_work_next_is_held_to_the_budget_and_says_how_many_it_left_out(js, door):
    async with db.session_factory()() as session:
        for n in range(site_tools.WORK_PROJECTS_MAX + 2):
            site = Site(owner=OWNER, url=f"https://site{n}.example.test/jobs", kind="employer",
                        created_at=datetime.now(UTC))
            session.add(site)
            await session.flush()
            await projects.create_project(session, OWNER, site.id, f"Learn site {n}", "Its listings can be read")
        await session.commit()
    reply = await mcp_server.work_next(door)
    assert len(reply["projects"]) == site_tools.WORK_PROJECTS_MAX and reply["more"] == 2
    assert payload_chars(reply) <= AGENT_PAYLOAD_BUDGET_CHARS


async def test_the_runner_is_told_site_work_waits_after_a_search_and_before_a_packet(js, door):
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) is None
    await start()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SITE == "site"
        await load_profile(session, OWNER)
        await run.start_round(session, OWNER, focus="banks")
        await session.commit()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SITE, (
            "the pool's order: site work before the search")
    async with db.session_factory()() as session:
        await run.close_unfinished(session, await run.open_run(session, OWNER), "test")
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SITE


async def test_a_paused_stopped_or_finished_project_is_nobodys_work_and_a_waiting_question_is_not_either(
        js, door, asked):
    pid = await start()
    async with db.session_factory()() as session:
        assert await site_tools.free(session, OWNER)
        project = await session.get(Project, uuid.UUID(pid))
        project.stopped_at = datetime.now(UTC)
        await session.commit()
        assert await site_tools.free(session, OWNER) == []
        project.stopped_at = None
        await session.commit()
    task = (await mcp_server.task_next(door, pid))["task"]
    escalated = await mcp_server.task_escalate(
        door, task["task_id"], "Which of the two careers pages is the real one?", ["the site's footer links"],
        "only_you_know")
    assert escalated["ok"] is True
    async with db.session_factory()() as session:
        assert await site_tools.free(session, OWNER) == [], "it waits for the owner, not for the scout"


# --- task.next: the plan task -------------------------------------------------------------------------------------


async def test_task_next_hands_the_scout_its_plan_task_with_the_site_the_lessons_and_the_kinds(js, door):
    pid = await start()
    reply = await mcp_server.task_next(door, pid)
    assert reply["ok"] is True
    task = reply["task"]
    assert (task["action"], task["mode"], task["title"], task["group"]) == ("plan", "plan", "Write plan v1", "Plan")
    assert task["report_with"] == "plan.propose" and task["report_example"]["project_id"] == pid
    assert task["instruction"].startswith("# Write plan v1") and task["template"] == "projects.plan.response"
    context = task["context"]
    assert context["site"] == {"url": SITE_URL, "kind": "employer", "skill": []}
    assert {k["id"] for k in context["kinds"]} >= {"site.read", "porter.explore"}
    assert context["lessons"] == [] and context["limits"]["payload_budget_chars"] == AGENT_PAYLOAD_BUDGET_CHARS
    assert payload_chars(reply) <= AGENT_PAYLOAD_BUDGET_CHARS
    # asked again, the scout gets the task it already holds, not a second one
    assert (await mcp_server.task_next(door, pid))["task"]["task_id"] == task["task_id"]
    assert [t.status for t in await rows(PlanTask)] == [PlanTaskStatus.in_progress]


async def test_task_next_with_nothing_to_hand_out_says_why_and_whether_to_stop(js, door, knocks):
    pid = await start()
    await plan(door, pid)  # the plan started: its item is next
    held = (await mcp_server.task_next(door, pid))["task"]
    assert held["action"] == "do"
    async with db.session_factory()() as session:
        project = await session.get(Project, uuid.UUID(pid))
        project.stopped_at = datetime.now(UTC)
        await session.commit()
    stopped = await mcp_server.task_next(door, pid)
    assert stopped == {"ok": False, "reason": "this project was stopped",
                       "how_to_fix": "stop: nothing is waiting for you in this project"}


# --- plan.propose -------------------------------------------------------------------------------------------------


async def test_plan_propose_needs_the_write_plan_task_held_then_the_plan_starts_by_itself_and_wakes(js, door, knocks):
    pid = await start()
    args = {"summary": "Read the site", "method": "Replay its data request", "items": [item("list")],
            "validation": VALIDATION, "problem": "Nothing is known of how it lists roles.",
            "proposal": ["Replay its data request"]}
    early = await mcp_server.plan_propose(door, pid, **args)
    assert early["ok"] is False and "don't hold" in early["reason"] and early["how_to_fix"].startswith("call task.next")
    await mcp_server.task_next(door, pid)
    knocks.clear()
    sent = await mcp_server.plan_propose(door, pid, **args)
    assert sent["ok"] is True and sent["plan_version"] == 1 and sent["status"] == "approved" and sent["started"] is True
    assert knocks == [(pid, "site_project")]
    assert "_wakes" not in sent
    first = (await rows(PlanTask, PlanTask.action == PlanItemAction.plan))[0]
    assert first.status == PlanTaskStatus.done and first.checked_out_by is None


async def test_plan_propose_refuses_a_plan_the_loop_refuses_and_says_what_to_fix(js, door):
    pid = await start()
    await mcp_server.task_next(door, pid)
    refused = await mcp_server.plan_propose(
        door, pid, summary="Read it", method="Replay", items=[{**item("a"), "kind": "no.such.kind"}],
        validation=VALIDATION, problem="Unknown.", proposal=["Read"])
    assert refused["ok"] is False and "no.such.kind" in refused["reason"]
    assert await rows(Plan) == []  # nothing stored, and the task is still held
    assert (await rows(PlanTask))[0].status == PlanTaskStatus.in_progress


# --- the item, its recipe and the Check ---------------------------------------------------------------------------


async def test_a_done_item_with_a_recipe_keeps_the_skill_before_its_check_reads_it_and_hands_on_to_act(
        js, door, knocks, monkeypatch):
    made = _use(monkeypatch, _world())
    pid = await start()
    await plan(door, pid)
    work_item = (await mcp_server.task_next(door, pid))["task"]
    assert (work_item["action"], work_item["kind"], work_item["key"]) == ("do", "site.read", "list")
    assert work_item["kind_spec"]["risk"] == "write" and "result" in work_item["kind_spec"]
    assert work_item["instruction"].startswith("# Do item list")
    assert work_item["template"] == "projects.do.item.response"
    knocks.clear()
    done = await mcp_server.task_complete(
        door, work_item["task_id"], "done", "Replayed the data request",
        "4 postings: Role 1, Role 2, Role 3, Role 4. expected count 4", result={"count": 4}, recipe=_recipe())
    assert done["skill_version"] == 1 and done["state"] == "done"
    assert done["next"] == {"task": None, "reason": "the round was checked and Act is ready"}
    assert knocks == [(pid, "site_project")], "Act is due: the runner is woken for it"
    skills = await rows(SiteSkill)
    assert [(s.version, s.recipe["kind"], s.retired_at, str(s.made_by_project_id)) for s in skills] == [
        (1, "request", None, pid)]
    task = (await rows(PlanTask, PlanTask.item_key == "list"))[0]
    assert task.status == PlanTaskStatus.done and task.data["skill_version"] == 1 and task.result == {"count": 4}
    # the Check ran inside that call, on the skill just kept, in code
    report = (await rows(ReviewReport))[0]
    assert report.verdict == "accepted" and made and all(f.client.is_closed for f in made)
    checks = await rows(PlanTask, PlanTask.action == PlanItemAction.review_check)
    assert [c.findings[0]["verdict"] for c in checks] == ["ok"]
    waiting = (await mcp_server.work_next(door))["projects"]
    assert [w["waiting"] for w in waiting] == [["act"]]


async def test_the_same_recipe_twice_is_one_version_and_a_changed_one_is_the_next(js, door, monkeypatch):
    _use(monkeypatch, _world())
    pid = await start()
    await plan(door, pid, "a", "b")
    first = (await mcp_server.task_next(door, pid))["task"]
    done = await mcp_server.task_complete(door, first["task_id"], "done", "Read a", "4 postings", recipe=_recipe())
    assert done["skill_version"] == 1
    second = done["next"]["task"]  # the reply carries the next task, as the scout's next call would
    assert second["key"] == "b" and second["action"] == "do"
    same = await mcp_server.task_complete(door, second["task_id"], "done", "Read b", "4 postings", recipe=_recipe())
    assert same["skill_version"] == 1 and len(await rows(SiteSkill)) == 1


async def test_a_changed_recipe_retires_the_version_before_it(js, door, monkeypatch):
    _use(monkeypatch, _world())
    pid = await start()
    await plan(door, pid, "a", "b")
    first = (await mcp_server.task_next(door, pid))["task"]
    await mcp_server.task_complete(door, first["task_id"], "done", "Read a", "4 postings", recipe=_recipe())
    second = (await mcp_server.task_next(door, pid))["task"]
    changed = _recipe(fields={"title": "name", "url": "path", "location": "loc"})
    done = await mcp_server.task_complete(door, second["task_id"], "done", "Read b", "4 postings", recipe=changed)
    assert done["skill_version"] == 2
    versions = await rows(SiteSkill)
    assert sorted((s.version, s.retired_at is not None) for s in versions) == [(1, True), (2, False)]


async def test_a_recipe_the_app_cannot_run_or_sent_the_wrong_way_is_refused_and_the_item_stays_open(
        js, door, monkeypatch):
    _use(monkeypatch, _world())
    pid = await start()
    await plan(door, pid, "a", "b")
    held = (await mcp_server.task_next(door, pid))["task"]
    bad = await mcp_server.task_complete(
        door, held["task_id"], "done", "Read it", "4 postings", recipe=_recipe(fields={"title": "name"}))
    assert bad["ok"] is False and "isn't a recipe the app can run: fields needs url" in bad["reason"]
    assert "site.skill" in bad["how_to_fix"]
    script = await mcp_server.task_complete(
        door, held["task_id"], "done", "Read it", "4 postings", recipe={"kind": "script", "code": "1"})
    assert script["ok"] is False and "unknown recipe kind" in script["reason"]
    off = await mcp_server.task_complete(
        door, held["task_id"], "off_plan", "No", "no", off_plan_reason="It needs a login", recipe=_recipe())
    assert off["ok"] is False and "only with a done item" in off["reason"]
    leaky = _recipe(url=f"{HOST}/api/jobs?key=sk-" + "a" * 30)
    token = await mcp_server.task_complete(door, held["task_id"], "done", "Read it", "4 postings", recipe=leaky)
    assert token["ok"] is False and "recipe" in token["reason"]
    browser = await mcp_server.task_complete(door, held["task_id"], "done", "Read it", "4 postings", recipe={
        "kind": "browser", "start_url": SITE_URL, "search": {"how": "none"},
        "results": {"row": "a card with a title", "fields": {"title": "the bold line"}, "next": "none"},
        "posting": {"description": "the text under About the role"}})
    assert browser["ok"] is False and "not by the app" in browser["reason"]
    assert "never write a browser skill" in browser["how_to_fix"], "the words skill.try uses"
    assert await rows(SiteSkill) == []
    assert (await rows(PlanTask, PlanTask.item_key == "a"))[0].status == PlanTaskStatus.in_progress


async def test_a_recipe_belongs_to_the_item_that_builds_the_skill_only(js, door, monkeypatch):
    _use(monkeypatch, _world())
    pid = await start()
    await mcp_server.task_next(door, pid)
    await mcp_server.plan_propose(
        door, pid, summary="Look first", method="Look at the site", validation=VALIDATION, problem="Unknown.",
        proposal=["Look"], items=[{"key": "look", "title": "Look", "group": "Do", "action": "do",
                                   "kind": "porter.explore",
                                   "data": {"subject": "the careers page", "question": "How does it load?"}}])
    looking = (await mcp_server.task_next(door, pid))["task"]
    assert looking["kind"] == "porter.explore"
    refused = await mcp_server.task_complete(
        door, looking["task_id"], "done", "Looked", "it loads from a data request",
        result={"answer": "A data request", "sources": [SITE_URL]}, recipe=_recipe())
    assert refused["ok"] is False and "only by an item of kind site.read" in refused["reason"]
    assert await rows(SiteSkill) == []


async def test_an_off_plan_item_needs_its_reason_and_a_task_the_scout_does_not_hold_is_refused(js, door):
    pid = await start()
    await plan(door, pid, "a", "b")
    held = (await mcp_server.task_next(door, pid))["task"]
    silent = await mcp_server.task_complete(door, held["task_id"], "off_plan", "No", "no")
    assert silent["ok"] is False and "off_plan_reason" in silent["reason"]
    bad_outcome = await mcp_server.task_complete(door, held["task_id"], "great", "x", "y")
    assert bad_outcome["ok"] is False
    # the item goes to another connection (the app took it back and gave it elsewhere)
    async with db.session_factory()() as session:
        other = AgentConnection(owner=OWNER, slug="other", name="Other", description="x", roles=["scout"])
        session.add(other)
        await session.flush()
        (await session.get(PlanTask, uuid.UUID(held["task_id"]))).checked_out_by = other.id
        await session.commit()
    stolen = await mcp_server.task_complete(door, held["task_id"], "off_plan", "No", "no", off_plan_reason="x")
    assert stolen["ok"] is False and "isn't checked out to you" in stolen["reason"] and stolen["how_to_fix"]
    off = await mcp_server.skill_try(door, held["task_id"], _recipe())
    assert off["ok"] is False and "isn't checked out to you" in off["reason"]


async def test_an_off_plan_item_ends_off_plan_with_its_reason_and_the_next_item_comes_back(js, door):
    pid = await start()
    await plan(door, pid, "a", "b")
    held = (await mcp_server.task_next(door, pid))["task"]
    done = await mcp_server.task_complete(
        door, held["task_id"], "off_plan", "The page is a sign-in wall", "a login form",
        off_plan_reason="The listings are behind a sign-in page")
    assert done["state"] == "off_plan" and done["next"]["task"]["key"] == "b" and "skill_version" not in done
    looked = await mcp_server.item_lookup(door, pid, ["a", "b", "nope"])
    by_key = {i["key"]: i for i in looked["items"]}
    assert set(by_key) == {"a", "b"}, "an unknown key is simply absent"
    assert by_key["a"]["status"] == "off_plan" and by_key["a"]["off_plan_reason"].startswith("The listings are behind")
    assert by_key["a"]["evidence"] == "a login form" and by_key["a"]["kind"] == "site.read"
    assert by_key["b"]["status"] == "in_progress" and "off_plan_reason" not in by_key["b"]


# --- skill.try ----------------------------------------------------------------------------------------------------


async def test_skill_try_reads_the_site_once_and_says_what_came_back_and_keeps_nothing(js, door, monkeypatch):
    made = _use(monkeypatch, _world(count=7))
    pid = await start()
    await plan(door, pid)
    held = (await mcp_server.task_next(door, pid))["task"]
    tried = await mcp_server.skill_try(door, held["task_id"], _recipe())
    assert tried["ok"] is True and tried["count"] == 7 and tried["truncated"] is False
    assert tried["titles"] == ["Role 1", "Role 2", "Role 3", "Role 4", "Role 5"]
    assert tried["links"] == [f"{HOST}/jobs/1", f"{HOST}/jobs/2"]
    assert len(made) == 1 and made[0].client.is_closed
    assert await rows(SiteSkill) == [] and await rows(ReviewReport) == []
    assert (await rows(PlanTask, PlanTask.item_key == "list"))[0].status == PlanTaskStatus.in_progress
    assert payload_chars(tried) < 2_000


async def test_skill_try_takes_a_recipe_whose_address_has_a_public_id_in_its_path_and_still_refuses_a_key_in_a_query(
        js, door, monkeypatch):
    _use(monkeypatch, _world(count=7))
    pid = await start()
    await plan(door, pid)
    held = (await mcp_server.task_next(door, pid))["task"]
    key = "0123456789abcdef" * 2
    kept = await mcp_server.skill_try(door, held["task_id"], _recipe(url_prefix=f"{HOST}/portal/{key}"))
    assert "redact and resend" not in str(kept.get("reason", ""))
    leaky = await mcp_server.skill_try(door, held["task_id"], _recipe(url=f"{HOST}/api/jobs?key=sk-" + "a" * 30))
    assert leaky["ok"] is False and "redact and resend" in leaky["reason"]


async def test_skill_try_is_a_quick_look_so_a_site_with_a_great_many_pages_is_cut_short(js, door, monkeypatch):
    made = _use(monkeypatch, _world(count=7))
    monkeypatch.setattr(site_tools.skills, "TRY_SECONDS", -1.0)  # its time is already out after the first page
    pid = await start()
    await plan(door, pid)
    held = (await mcp_server.task_next(door, pid))["task"]
    tried = await mcp_server.skill_try(door, held["task_id"], _recipe(page={"param": "page", "size": 7}))
    assert tried["ok"] is True and tried["count"] == 7 and tried["truncated"] is True
    assert tried["note"] == "stopped at 7 postings (the time allowed for the read ran out)"
    assert len(made) == 1 and made[0].client.is_closed


async def test_skill_try_stops_once_it_has_enough_postings_to_test_the_recipe(js, door, monkeypatch):
    """Found when a try of a site with a very long list read all of it until its time ran out."""
    made = _use(monkeypatch, _world(count=50))
    pid = await start()
    await plan(door, pid)
    held = (await mcp_server.task_next(door, pid))["task"]
    tried = await mcp_server.skill_try(door, held["task_id"], _recipe(page={"param": "offset", "size": 2}))
    assert tried["ok"] is True and tried["count"] < 50 and tried["truncated"] is True
    assert "enough for a test" in tried["note"] and len(made) == 1


async def test_skill_try_says_when_nothing_came_back(js, door, monkeypatch):
    _use(monkeypatch, _world(count=0))
    pid = await start()
    await plan(door, pid)
    held = (await mcp_server.task_next(door, pid))["task"]
    empty = await mcp_server.skill_try(door, held["task_id"], _recipe())
    assert empty["ok"] is True and empty["count"] == 0 and empty["titles"] == [] and "no postings" in empty["note"]


async def test_skill_try_stops_at_a_site_that_asks_not_to_be_read_and_says_not_to_look_for_a_way_round(
        js, door, monkeypatch):
    _use(monkeypatch, _world(robots="User-agent: *\nDisallow: /api/"))
    pid = await start()
    await plan(door, pid)
    held = (await mcp_server.task_next(door, pid))["task"]
    refused = await mcp_server.skill_try(door, held["task_id"], _recipe())
    assert refused["ok"] is False and refused["reason"].startswith("the site asks not to be read")
    assert "cannot_be_read" in refused["how_to_fix"] and "way round" in refused["how_to_fix"]


async def test_skill_try_refuses_a_bad_recipe_a_browser_one_and_a_task_with_no_skill(js, door, monkeypatch):
    _use(monkeypatch, _world())
    pid = await start()
    plan_task = (await mcp_server.task_next(door, pid))["task"]
    wrong_task = await mcp_server.skill_try(door, plan_task["task_id"], _recipe())
    assert wrong_task["ok"] is False and "item that builds the site's skill" in wrong_task["reason"]
    await mcp_server.plan_propose(
        door, pid, summary="Read", method="Replay", items=[item("list")], validation=VALIDATION,
        problem="Unknown.", proposal=["Read"])
    held = (await mcp_server.task_next(door, pid))["task"]
    bad = await mcp_server.skill_try(door, held["task_id"], {"kind": "request", "url": "http://localhost/x"})
    assert bad["ok"] is False and "not a recipe the app can run" in bad["reason"]
    words = await mcp_server.skill_try(door, held["task_id"], {
        "kind": "browser", "start_url": SITE_URL, "search": {"how": "none"},
        "results": {"row": "a card with a title", "fields": {"title": "the bold line"}, "next": "none"},
        "posting": {"description": "the text under About the role"}})
    assert words["ok"] is False and "browser session" in words["reason"] and "not by the app" in words["reason"]
    assert "needs_browser" in words["how_to_fix"] and "never write a browser skill" in words["how_to_fix"]


# --- task.escalate ------------------------------------------------------------------------------------------------


async def test_escalating_a_decision_makes_an_owner_ask_and_the_task_waits(js, door, asked):
    pid = await start()
    task = (await mcp_server.task_next(door, pid))["task"]
    options = [{"id": "browser", "label": "Use a browser", "description": "Drive the page the way a person would."},
               {"id": "skip", "label": "Skip this site", "description": "Leave the site out."}]
    reply = await mcp_server.task_escalate(
        door, task["task_id"], "The listings only show in a browser. Should the app use one?",
        ["the page's source", "its data requests"], "only_you_decide", options=options, recommended="skip")
    assert reply == {"ok": True, "asked": "the owner", "next": "the task waits for their answer. Stop now"}
    assert len(asked) == 1
    ask = asked[0]
    assert ask["card"] == site_tools.ESCALATION_CARD == "escalation" and ask["key"].startswith("escalate-")
    assert ask["title"].startswith("The listings only show in a browser")
    assert [o["id"] for o in ask["options"]] == ["browser", "skip"] and ask["recommended"] == "skip"
    assert "Looked in: the page's source; its data requests." in ask["detail"]
    assert "If you don't answer" not in ask["detail"], "nothing is promised for a question nobody answers"
    assert str(ask["task"].id) == task["task_id"] and ask["project"].id == uuid.UUID(pid)
    waiting = (await rows(PlanTask))[0]
    assert waiting.status == PlanTaskStatus.waiting and waiting.checked_out_by is None
    kept = waiting.data["escalation"]
    assert (kept["reason"], kept["recommended"], "default" in kept) == ("only_you_decide", "skip", False)
    # not handed out again, and the scout is told why
    again = await mcp_server.task_next(door, pid)
    assert again["ok"] is False and again["reason"] == "your question to the owner is waiting for their answer"


async def test_escalating_a_fact_or_a_step_needs_its_reason_and_where_the_scout_looked(js, door, asked):
    pid = await start()
    task = (await mcp_server.task_next(door, pid))["task"]
    tid = task["task_id"]
    ask = mcp_server.task_escalate
    cases = [
        (await ask(door, tid, "What is the site's name?", [], "only_you_know"), "`checked` is 1 to 10"),
        (await ask(door, tid, "What is the site's name?", ["its title"], "stuck"), "`reason` is one of"),
        (await ask(door, tid, "", ["its title"], "only_you_know"), "`question`"),
        (await ask(door, tid, "Which?", ["x"], "only_you_decide", options=[{"id": "a", "label": "A"}],
                   recommended="a"), "needs 2 to 8 `options`"),
        (await ask(door, tid, "Which?", ["x"], "only_you_decide",
                   options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], recommended="c"),
         "`recommended` is the id of one of the options"),
        (await ask(door, tid, "Which?", ["x"], "only_you_decide",
                   options=[{"id": "a", "label": "A"}, {"id": "a", "label": "B"}], recommended="a"),
         "option ids must be different"),
        (await ask(door, tid, "Is the key sk-" + "a" * 30 + " right?", ["x"], "only_you_know"), "redact"),
    ]
    for reply, why in cases:
        assert reply["ok"] is False and why in reply["reason"], reply
    assert asked == [] and (await rows(PlanTask))[0].status == PlanTaskStatus.in_progress
    step = await ask(door, tid, "Someone has to sign in to see the listings.", ["the login page"], "only_you_can_do")
    assert step["ok"] is True and len(asked) == 1
    assert [o["id"] for o in asked[0]["options"]] == ["did_it"], "a step only a person takes: 'I did it'"


async def test_the_default_ask_leaves_a_line_on_the_activity_page_that_needs_the_owner(js, door):
    pid = await start()
    task = (await mcp_server.task_next(door, pid))["task"]
    await mcp_server.task_escalate(
        door, task["task_id"], "Is this the careers page?", ["the footer"], "only_you_know")
    events = [e.event for e in await rows(TrailEvent) if e.link_id == uuid.UUID(pid)]
    assert "task_escalated" in events and "owner_asked" in events


# --- task.escalate: a site the server cannot read -----------------------------------------------------------------


async def test_escalating_that_the_site_refuses_to_be_read_asks_no_one_and_switches_the_site_off(js, door, asked):
    """The owner decided that a site where scraping or browser traffic doesn't work is turned off by default and the
    owner is told; nothing is asked and nothing is wasted on it. A refusal is something no browser can get round."""
    pid = await start()
    task = (await mcp_server.task_next(door, pid))["task"]
    reply = await mcp_server.task_escalate(
        door, task["task_id"], "Its robots rules say no to every app.",
        ["the page's source", "its robots.txt"], "cannot_be_read")
    assert reply == {"ok": True, "site_turned_off": True,
                     "next": "resumebot switched the site off and ended the project. Stop now"}
    # no one is asked: no ask, no Inbox task
    assert asked == [] and await rows(Task) == []
    # the site is off, by the app, with why
    site = (await rows(Site))[0]
    assert (site.on, site.off_by, site.off_note) == (False, "app", "its robots rules say no to every app")
    # a robots refusal is nothing the owner can act on: no notification, the site's row says why
    assert await rows(Notification) == []
    # the project ended with a resolution that says so, and the held task is set aside
    project = (await rows(Project))[0]
    assert project.finished_at is not None and project.resolution["outcome"] == (
        "Stopped: it can't be read (its robots rules say no to every app), so resumebot switched the site off.")
    assert [t.status for t in await rows(PlanTask)] == [PlanTaskStatus.skipped]
    again = await mcp_server.task_next(door, pid)
    assert again["ok"] is False
    events = [e.event for e in await rows(TrailEvent) if e.link_id == uuid.UUID(pid)]
    assert "site_unreadable" in events and "owner_asked" not in events and "task_escalated" not in events


async def test_escalating_that_a_browser_might_read_the_site_hands_it_to_the_browser_and_switches_nothing_off(
        js, door, asked):
    """The browser has not been tried, so the site is not turned off: it is set to be read in the owner's browser, which
    decides. Nobody is asked and nobody is told."""
    pid = await start()
    task = (await mcp_server.task_next(door, pid))["task"]
    reply = await mcp_server.task_escalate(
        door, task["task_id"], "Its listings only load in a browser session.",
        ["the page's source", "its data requests"], "needs_browser")
    assert reply == {"ok": True, "site_to_browser": True,
                     "next": "resumebot handed the site to the owner's browser and ended the project. Stop now"}
    assert asked == [] and await rows(Task) == [] and await rows(Notification) == []
    site = (await rows(Site))[0]
    assert (site.on, site.read_by, site.off_by, site.off_note) == (True, "browser", None, None)
    project = (await rows(Project))[0]
    assert project.finished_at is not None and project.resolution["outcome"] == (
        "Handed to your browser: its listings only load in a browser session, so the app can't read it from the "
        "server.")
    assert [t.status for t in await rows(PlanTask)] == [PlanTaskStatus.skipped]
    events = [e.event for e in await rows(TrailEvent) if e.link_id == uuid.UUID(pid)]
    assert "site_to_browser" in events and "site_unreadable" not in events and "owner_asked" not in events


@pytest.mark.parametrize("reason", ["needs_browser", "cannot_be_read"])
async def test_escalating_that_the_server_cannot_read_the_site_needs_why_in_one_clause_and_where_it_looked(
        js, door, asked, reason):
    pid = await start()
    tid = (await mcp_server.task_next(door, pid))["task"]["task_id"]
    ask = mcp_server.task_escalate
    cases = [
        (await ask(door, tid, "It is no use. " * 20, ["its page"], reason), "one plain clause"),
        (await ask(door, tid, "It needs a sign-in", [], reason), "`checked` is 1 to 10"),
        (await ask(door, tid, "", ["its page"], reason), "`question`"),
        (await ask(door, tid, "Needs the key sk-" + "a" * 30, ["its page"], reason), "redact"),
    ]
    for reply, why in cases:
        assert reply["ok"] is False and why in reply["reason"], reply
    # a refusal changes nothing: the site is still on and the project still open
    site, project = (await rows(Site))[0], (await rows(Project))[0]
    assert site.on and site.off_by is None and site.read_by == "app" and project.finished_at is None
    assert await rows(Notification) == [] and (await rows(PlanTask))[0].status == PlanTaskStatus.in_progress


async def test_a_site_the_owner_already_switched_off_is_not_announced_when_the_scout_says_it_refuses_to_be_read(
        js, door, asked):
    pid = await start()
    tid = (await mcp_server.task_next(door, pid))["task"]["task_id"]
    async with db.session_factory()() as session:
        (await session.scalar(select(Site))).on = False
        await session.commit()
    reply = await mcp_server.task_escalate(door, tid, "Its terms forbid reading it", ["its page"], "cannot_be_read")
    assert reply["ok"] is True
    assert await rows(Notification) == [], "nothing was switched, so nothing is said"
    assert (await rows(Project))[0].finished_at is not None and (await rows(Site))[0].off_by is None


# --- the Act step: lessons, act.complete, the read tools ----------------------------------------------------------


async def _to_act(door, monkeypatch, *keys: str) -> tuple[str, dict]:
    _use(monkeypatch, _world())
    pid = await start()
    await plan(door, pid, *keys)
    for _ in keys:
        held = (await mcp_server.task_next(door, pid))["task"]
        done = await mcp_server.task_complete(
            door, held["task_id"], "done", f"Read {held['key']}", "4 postings. expected count 4",
            recipe=_recipe(), next=False)
        assert done["state"] == "done"
    # the last item asked for no next task, so the checks are not made yet: that is work for the scout's next session
    assert [w["waiting"] for w in (await mcp_server.work_next(door))["projects"]] == [["checks"]]
    ended = await mcp_server.task_next(door, pid)  # makes the checks; Act is ready for a fresh session
    assert ended["ok"] is False and ended["reason"] == "the round was checked and Act is ready"
    assert "fresh one" in ended["how_to_fix"]
    act = await mcp_server.task_next(door, pid)
    assert act["ok"] is True and act["task"]["action"] == "act"
    return pid, act["task"]


async def test_a_lesson_recorded_without_a_scope_is_about_its_own_project_only(js, door, knocks, monkeypatch):
    """A lesson reaches every site only when the scout says so."""
    pid, act = await _to_act(door, monkeypatch, "list")
    kept = await mcp_server.lesson_record(
        door, pid, act["task_id"], "This site lists every posting from one data request.",
        source="item list: the report")
    assert kept["ok"] is True and kept["created"] is True
    assert [row.applies_to for row in await rows(Lesson)] == ["project"]


async def test_the_whole_round_through_the_door_ends_with_lessons_a_kept_skill_and_a_finished_project(
        js, door, knocks, monkeypatch):
    pid, act = await _to_act(door, monkeypatch, "list")
    assert act["report_with"] == "act.complete" and act["context"]["report"]["verdict"] == "accepted"
    assert act["instruction"].startswith("# Learn from plan v1") and act["context"]["closing"]["outcome"]
    assert act["context"]["site"]["skill"] == [{"version": 1, "retired": False}]
    kept = await mcp_server.lesson_record(
        door, pid, act["task_id"], "This site lists every posting from one data request.", "project",
        "item list: the report", subject="listing")
    owner_wide = await mcp_server.lesson_record(
        door, pid, act["task_id"], "A careers site often lists its roles from one data request.", "owner",
        "item list: the report")
    assert kept["created"] is True and owner_wide["created"] is True
    again = await mcp_server.lesson_record(
        door, pid, act["task_id"], "This site lists every posting from one data request.", "project",
        "item list: the report")
    assert again == {"ok": True, "lesson_id": kept["lesson_id"], "created": False}
    listed = await mcp_server.lesson_list(door, pid)
    assert listed["total"] == 2 and {row["text"] for row in listed["lessons"]} >= {
        "This site lists every posting from one data request."}
    retired = await mcp_server.lesson_retire(
        door, pid, act["task_id"], owner_wide["lesson_id"], "item list: the report")
    assert retired == {"ok": True, "lesson_id": owner_wide["lesson_id"], "retired": True}
    assert (await mcp_server.lesson_list(door, pid))["total"] == 1
    newer = _recipe(fields={"title": "name", "url": "path", "location": "loc"})
    knocks.clear()
    finished = await mcp_server.act_complete(door, pid, act["task_id"], SUMMARY, skill=newer)
    assert finished["ok"] is True and finished["next"] == "finished" and finished["skill_version"] == 2
    assert finished["lessons_recorded"] == 2 and "_wakes" not in finished and knocks == []
    project = (await rows(Project))[0]
    assert project.finished_at is not None and project.resolution["outcome"]
    assert sorted((s.version, s.retired_at is not None) for s in await rows(SiteSkill)) == [(1, True), (2, False)]
    assert sorted((row.applies_to, row.retired_at is not None) for row in await rows(Lesson)) == [
        ("owner", True), ("project", False)]
    assert (await mcp_server.task_next(door, pid))["reason"] == "this project is finished"
    assert (await mcp_server.work_next(door))["projects"] == []
    report = await mcp_server.review_read(door, pid)
    assert report["report"]["verdict"] == "accepted" and report["report"]["plan_version"] == 1
    other = await mcp_server.review_read(door, pid, 7)
    assert other["report"]["plan_version"] == 1 and "plan v7 has no report" in other["note"]


async def test_act_complete_refuses_a_summary_missing_a_part_and_names_every_problem(js, door, monkeypatch):
    pid, act = await _to_act(door, monkeypatch, "list")
    refused = await mcp_server.act_complete(door, pid, act["task_id"], {"planned": "Read", "done": " "})
    assert refused["ok"] is False
    for part in ("`checks`", "`learned`", "`next`", "summary.done is empty"):
        assert part in refused["reason"]
    assert (await rows(PlanTask, PlanTask.action == PlanItemAction.act))[0].status == PlanTaskStatus.in_progress


async def test_a_lesson_needs_the_act_task_a_source_from_this_round_and_a_scope_that_exists(js, door, monkeypatch):
    pid, act = await _to_act(door, monkeypatch, "list")
    text = "The list is one request."
    no_source = await mcp_server.lesson_record(door, pid, act["task_id"], text, "project", "hunch")
    assert no_source["ok"] is False and "needs a `source` from this round" in no_source["reason"]
    scope = await mcp_server.lesson_record(door, pid, act["task_id"], text, "everywhere", "item list")
    assert scope["ok"] is False and "`applies_to` is" in scope["reason"]
    machinery = await mcp_server.lesson_record(
        door, pid, act["task_id"], "Always call task.complete after each item.", "project", "item list")
    assert machinery["ok"] is False and "restates a rule" in machinery["reason"]
    not_act = await mcp_server.lesson_record(
        door, pid, str((await rows(PlanTask, PlanTask.item_key == "list"))[0].id), "x y z", "project", "item list")
    assert not_act["ok"] is False and "from the Act task" in not_act["reason"]
    assert await rows(Lesson) == []
    unknown = await mcp_server.lesson_retire(door, pid, act["task_id"], str(uuid.uuid4()), "item list")
    assert unknown["ok"] is False and "no such lesson" in unknown["reason"]


# --- doc.read and the sizes ---------------------------------------------------------------------------------------


async def test_doc_read_gives_a_site_documents_text_and_refuses_the_rest_listing_what_there_is(js, door):
    plan_response = await mcp_server.doc_read(door, "projects.plan.response")
    assert plan_response["type"] == "template" and plan_response["template_version"] >= 1
    assert "Plan response template" in plan_response["text"]
    recipe_doc = await mcp_server.doc_read(door, "site.skill")
    assert recipe_doc["type"] == "template" and all(k in recipe_doc["text"] for k in (
        "adapter", "request", "list_page", "browser", "skill.try")) and "page_text" not in recipe_doc["text"]
    role = await mcp_server.doc_read(door, "instructions/agents.scout.role.instructions.md")
    assert role["name"] == "agents.scout.role" and role["type"] == "instruction"
    kind = await mcp_server.doc_read(door, "site.read")
    assert kind["type"] == "kind" and kind["kind"]["id"] == "site.read" and "result" in kind["kind"]
    assert "site.skill" in kind["kind"]["notes"]
    for outside in ("jobsearch.scout", "jobsearch.packet", "nonesuch"):
        refused = await mcp_server.doc_read(door, outside)
        assert refused["ok"] is False and "no document named" in refused["reason"]
        assert "projects.plan.write" in refused["how_to_fix"] and "jobsearch" not in refused["how_to_fix"]


# --- no tool writes a check result --------------------------------------------------------------------------------


async def test_there_is_no_tool_that_writes_a_check_result_and_a_check_cannot_be_completed_by_the_scout(
        js, door, monkeypatch):
    names = {t.name for t in await mcp_server.mcp.list_tools()}
    assert not [n for n in names if "check" in n or "finding" in n or "verdict" in n]
    source = inspect.getsource(site_tools)
    assert "check_flow" not in source and "complete_plan_check" not in source and "site_check" not in source
    pid, _act = await _to_act(door, monkeypatch, "list")
    check = (await rows(PlanTask, PlanTask.action == PlanItemAction.review_check))[0]
    for call in (
        mcp_server.task_complete(door, str(check.id), "done", "On plan", "I looked"),
        mcp_server.task_complete(door, str(check.id), "off_plan", "Off plan", "I looked", off_plan_reason="x"),
        mcp_server.task_escalate(door, str(check.id), "Is it on plan?", ["x"], "only_you_know"),
        mcp_server.skill_try(door, str(check.id), _recipe()),
    ):
        refused = await call
        assert refused["ok"] is False, refused
    after = (await rows(PlanTask, PlanTask.id == check.id))[0]
    assert after.findings == check.findings and after.status == check.status
