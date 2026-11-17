"""The browser door (docs/PRD.md 30.8, 37, the browser work spec): the six tools the app's browser workers work the
queue through, behind a token of their own. A request without the token is refused before any MCP message is read, the
scout's token opens nothing here and this one opens nothing of the scout's, and every refusal is an ordinary result.
Everything is made up; the tools are called the way the server calls them (a context with request headers)."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db
from app.jobsearch.browser import mcp as browser_mcp
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.main import app
from app.jobsearch.models import JobPosting, MissionRun
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, payload_chars
from tests.builders import ONE, PATHFINDER, WORKER, browser_card, browser_world
from tests.jobsearch.example_world import PORTAL, example_world, portal_card  # noqa: F401
from tests.support import browser_recipe

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out", "example_world")

OWNER = "owner"  # the setting's default: the tools act for it

BROWSER_TOKEN = "js_browser_token_1234567890"
SCOUT_TOKEN = "js_scout_token_0987654321"


@pytest.fixture
def door(tmp_path, monkeypatch):
    """Both doors' token files, the owner the tools act for, and a context carrying the browser token and a worker's
    session."""
    browser, scout = tmp_path / "browser-token", tmp_path / "agent-token"
    browser.write_text(BROWSER_TOKEN + "\n", encoding="utf-8")
    scout.write_text(SCOUT_TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr(browser_mcp.settings, "browser_token_file", str(browser))
    monkeypatch.setattr(browser_mcp.settings, "agent_token_file", str(scout))
    monkeypatch.setattr(browser_mcp.settings, "owner", OWNER)
    return SimpleNamespace(headers={"authorization": f"Bearer {BROWSER_TOKEN}", "x-session-id": WORKER})


def _pathfinder() -> SimpleNamespace:
    return SimpleNamespace(headers={"authorization": f"Bearer {BROWSER_TOKEN}", "x-session-id": PATHFINDER})


# --- the contract -------------------------------------------------------------------------------------------------


async def test_the_door_lists_six_tools_and_none_of_the_scouts():
    listed = {t.name: t for t in await browser_mcp.mcp.list_tools()}
    assert set(listed) == {
        "browser.next_item", "browser.hand_back", "browser.heartbeat", "browser.page_digest", "browser.postings",
        "browser.skill"}, "no plan, no role lists, no posting cap: one item at a time"
    assert listed["browser.page_digest"].input_schema["required"] == ["url"]
    assert not listed["browser.next_item"].input_schema.get("properties")
    assert set(listed["browser.hand_back"].input_schema["properties"]) == {
        "item_id", "result", "url", "text", "note", "title", "company", "source_kind", "digest_text"}
    assert listed["browser.hand_back"].input_schema["required"] == ["item_id", "result"]
    assert set(listed["browser.postings"].input_schema["properties"]) == {"postings", "portals"}
    assert listed["browser.postings"].input_schema["required"] == ["postings"]
    assert set(listed["browser.skill"].input_schema["properties"]) == {"site", "recipe", "page_says", "lessons"}
    assert listed["browser.skill"].input_schema["required"] == ["site", "recipe"]
    for tool in listed.values():
        assert tool.description and len(tool.description) > 60
    for words in ("never a job board's address", "easy_apply", "three tries"):
        assert words in listed["browser.hand_back"].description, words
    # and the scout's door has none of these: a token for one opens the other's tools nowhere
    scouts = {t.name for t in await mcp_server.mcp.list_tools()}
    assert not scouts & set(listed)


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer wrong-token", "Basic abc", f"bearer {BROWSER_TOKEN}x"])
async def test_a_call_without_the_browser_token_is_refused_and_reads_and_stores_nothing(js, door, header):
    await browser_world()
    ctx = SimpleNamespace(headers={**({"authorization": header} if header is not None else {}),
                                   "x-session-id": WORKER})
    for call in (browser_mcp.browser_next_item(ctx), browser_mcp.browser_postings(ctx, [browser_card()]),
                 browser_mcp.browser_hand_back(ctx, "x", "done"), browser_mcp.browser_heartbeat(ctx),
                 browser_mcp.browser_page_digest(ctx, "https://www.example.test/jobs/1"),
                 browser_mcp.browser_skill(ctx, ONE, browser_recipe(ONE))):
        result = await call
        assert result["ok"] is False and "token" in result["reason"]
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(MissionRun)) == 0, "no run opened"
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0


async def test_the_scouts_token_does_not_open_the_browser_door(js, door):
    await browser_world()
    ctx = SimpleNamespace(headers={"authorization": f"Bearer {SCOUT_TOKEN}", "x-session-id": WORKER})
    assert (await browser_mcp.browser_next_item(ctx))["ok"] is False
    # nor the browser token the scout's: it has its own check, on its own file
    assert mcp_server.token_ok(f"Bearer {BROWSER_TOKEN}") is False
    assert mcp_server.token_ok(f"Bearer {SCOUT_TOKEN}") is True


async def test_no_token_file_means_nothing_gets_in(js, door, monkeypatch, tmp_path):
    await browser_world()
    monkeypatch.setattr(browser_mcp.settings, "browser_token_file", str(tmp_path / "missing"))
    assert (await browser_mcp.browser_next_item(door))["ok"] is False


# --- the tools through the door -----------------------------------------------------------------------------------


async def test_a_scan_through_the_door_leaves_postings_in_the_index_and_a_run_for_the_owner(js, door):
    await browser_world(extra_sites=False)
    got = await browser_mcp.browser_next_item(door)
    assert got["ok"] and got["item"]["kind"] == "scan" and payload_chars(got) < AGENT_PAYLOAD_BUDGET_CHARS
    assert got["item"]["runbook"]["start_url"] == f"https://www.{ONE}/jobs", "the site's runbook comes with it"
    assert (await browser_mcp.browser_heartbeat(door))["holding"] is True
    reply = await browser_mcp.browser_postings(door, [browser_card(1), browser_card(2)])
    assert (reply["ok"], reply["new"], reply["known"]) == (True, 2, 0)
    done = await browser_mcp.browser_hand_back(door, got["item"]["item_id"], "done")
    assert done["ok"] and "browser.next_item" in done["next"]
    stop = await browser_mcp.browser_next_item(door)
    assert stop["stop"] is True
    async with db.session_factory()() as session:
        [run] = (await session.scalars(select(MissionRun))).all()
        assert (await session.scalar(select(func.count()).select_from(JobPosting))) == 2
    assert run.outcome == "done" and run.summary == "Scanned Listings. Handed in 2 postings, 2 new."


async def test_a_refusal_is_an_ordinary_result_that_names_the_problem_and_stores_nothing(js, door):
    await browser_world(extra_sites=False)
    await browser_mcp.browser_next_item(door)
    refused = await browser_mcp.browser_postings(
        door, [browser_card(1, work_mode="sometimes"), browser_card(2, title="")])
    assert refused["ok"] is False and "work_mode" in refused["reason"] and "title is required" in refused["reason"]
    assert refused["how_to_fix"]
    wrong = await browser_mcp.browser_hand_back(door, "not-an-item", "done")
    assert wrong["ok"] is False and "not the item you hold" in wrong["reason"]
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0


async def test_postings_with_no_item_held_are_refused_with_what_to_do(js, door):
    await browser_world()
    refused = await browser_mcp.browser_postings(door, [browser_card(1)])
    assert refused["ok"] is False and "call browser.next_item" in refused["reason"]


async def test_a_map_through_the_door_hands_in_postings_then_a_runbook_then_the_item(js, door):
    await browser_world(learned=False, extra_sites=False)
    assert (await browser_mcp.browser_next_item(door))["stop"] is True, "the worker has nothing: no runbook yet"
    ctx = _pathfinder()
    got = await browser_mcp.browser_next_item(ctx)
    assert got["item"]["kind"] == "map" and got["item"]["site"] == ONE and "browser.skill" in got["instructions"]
    early = await browser_mcp.browser_skill(ctx, ONE, browser_recipe(ONE))
    assert early["ok"] is False and "no posting from" in early["reason"] and early["how_to_fix"]
    await browser_mcp.browser_postings(ctx, [browser_card(1), browser_card(2)])
    kept = await browser_mcp.browser_skill(ctx, ONE, browser_recipe(ONE), page_says=2, lessons=["Press Search."])
    assert kept["ok"] is True and kept["kept"] is True and kept["version"] == 1 and "hand_back" in kept["next"]
    await browser_mcp.browser_hand_back(ctx, got["item"]["item_id"], "done")
    async with db.session_factory()() as session:
        runs = {r.started["session_id"]: r for r in (await session.scalars(select(MissionRun))).all()}
    assert runs[PATHFINDER].summary == "Mapped Listings. Handed in 2 postings, 2 new."
    scan = await browser_mcp.browser_next_item(door)
    assert scan["wait_seconds"] == 8, "the site is scanned next, after its page gap"


async def test_a_portal_is_declared_through_the_door_and_the_runbook_for_it_is_kept(js, door):
    listed = {t.name: t for t in await browser_mcp.mcp.list_tools()}
    assert "portals" in listed["browser.postings"].description
    await browser_world(learned=False, extra_sites=False)
    ctx = _pathfinder()
    await browser_mcp.browser_next_item(ctx)
    bare = await browser_mcp.browser_postings(ctx, [portal_card(1)])
    assert bare["ok"] is False and f"url is not on {ONE}" in bare["reason"] and bare["how_to_fix"]
    declared = await browser_mcp.browser_postings(ctx, [], portals=[{"site": ONE, "url": PORTAL}])
    assert declared["ok"] is True and declared["portals"] == {ONE: PORTAL} and "Portal noted" in declared["next"]
    reply = await browser_mcp.browser_postings(ctx, [portal_card(1), portal_card(2)])
    assert (reply["ok"], reply["new"]) == (True, 2)
    wrong = await browser_mcp.browser_postings(ctx, [], portals=[{"site": ONE, "url": "https://www.example.test/"}])
    assert wrong["ok"] is False and "portals 1" in wrong["reason"] and "not a job system's address" in wrong["reason"]
    kept = await browser_mcp.browser_skill(ctx, ONE, browser_recipe(ONE), page_says=2)
    assert kept["ok"] is True and kept["portal"] == PORTAL


async def test_a_paused_mission_tells_the_session_to_stop(js, door):
    await browser_world(resumed=False)
    stop = await browser_mcp.browser_next_item(door)
    assert stop["ok"] and stop["stop"] is True and "paused" in stop["why"]


# --- the door in front of the whole MCP server ----------------------------------------------------------------


class _Inner:
    """What the gate protects: it records that it was reached."""

    reached = 0

    async def __call__(self, scope, receive, send):
        _Inner.reached += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.parametrize("header", [None, "Bearer nope", f"Bearer {SCOUT_TOKEN}", f"Basic {BROWSER_TOKEN}"])
async def test_the_gate_answers_401_before_anything_is_read_and_says_how_to_authenticate(door, header):
    _Inner.reached = 0
    async with AsyncClient(transport=ASGITransport(app=browser_mcp.BearerGate(_Inner())), base_url="http://test") as c:
        got = await c.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           headers={"Authorization": header} if header else {})
    assert got.status_code == 401 and got.headers["www-authenticate"].startswith("Bearer")
    assert _Inner.reached == 0


async def test_the_gate_lets_the_browser_token_through(door):
    _Inner.reached = 0
    async with AsyncClient(transport=ASGITransport(app=browser_mcp.BearerGate(_Inner())), base_url="http://test") as c:
        got = await c.post("/", json={}, headers={"Authorization": f"Bearer {BROWSER_TOKEN}"})
    assert got.status_code == 200 and _Inner.reached == 1


async def test_the_app_publishes_the_browser_door_behind_the_gate_and_the_scouts_door_apart(door):
    paths = {getattr(route, "path", None): route.app for route in app.routes if hasattr(route, "app")}
    assert isinstance(paths["/mcp-browser"], browser_mcp.BearerGate)
    assert "/mcp" in paths and not isinstance(paths["/mcp"], browser_mcp.BearerGate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for token in (None, SCOUT_TOKEN):
            got = await c.post("/mcp-browser/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                               headers={"Authorization": f"Bearer {token}"} if token else {})
            assert got.status_code == 401


async def test_the_missions_screen_cannot_run_the_browser_mission_or_give_it_a_time(js, door):
    await browser_world()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        listed = (await c.get("/api/v1/missions")).json()["missions"]
        browser = next(m for m in listed if m["key"] == "read_browser")
        assert browser["schedule"] == {"kind": "outside"} and browser["next_run_at"] is None
        assert browser["schedule_words"] == "when your computer's scheduled session calls in"
        ran = await c.post(f"/api/v1/missions/{browser['id']}/run")
        assert ran.status_code == 422 and "starts on your computer" in ran.json()["detail"]
        timed = await c.patch(f"/api/v1/missions/{browser['id']}", json={"schedule": {"kind": "daily", "at": "02:00"}})
        assert timed.status_code == 422 and "set on your computer" in timed.json()["detail"]


# --- fewer turns per item (2026-10-20) ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """The door's waits, recorded and not taken."""
    seconds: list[float] = []

    async def fake(n):
        seconds.append(n)

    monkeypatch.setattr(browser_mcp, "sleep", fake)
    return seconds


async def test_next_item_holds_the_wait_itself_before_handing_a_wait_back(js, door, slept):
    await browser_world(learned=False, extra_sites=False)
    ctx = _pathfinder()
    got = await browser_mcp.browser_next_item(ctx)
    await browser_mcp.browser_postings(ctx, [browser_card(1), browser_card(2)])
    await browser_mcp.browser_skill(ctx, ONE, browser_recipe(ONE), page_says=2)
    await browser_mcp.browser_hand_back(ctx, got["item"]["item_id"], "done")
    scan = await browser_mcp.browser_next_item(door)
    assert len(slept) == browser_mcp.HOLDS and slept[0] >= 1, "it waited server-side for the site's page gap, twice"
    assert scan["wait_seconds"] >= 1, "the clock did not move (the wait is stubbed), so the wait is handed back after"


async def test_the_heartbeat_holds_the_pace_itself_and_answers_load_now(js, door, slept):
    await browser_world(extra_sites=False)
    await browser_mcp.browser_next_item(door)
    first = await browser_mcp.browser_heartbeat(door)
    assert first["wait_ms"] == 0 and first["waited_ms"] > 0 and slept[0] > 0, "first page after its gap"
    assert first["next"] == "Load the page now."
    again = await browser_mcp.browser_heartbeat(door)
    assert again["wait_ms"] == 0 and len(slept) == 2, "every page load after is paced the same"
