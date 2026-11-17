"""The owner's action door (docs/PRD.md path 50.1 to 50.3, `jobsearch/owner_actions/mcp.py`): four tools behind a token
of
its own, which no lane's agent token and no browser token opens, answered 401 before any MCP message is read, behind
the same Host and Origin guard as every route, and a dry run through it changing nothing. Everything is made up; the
tools are called the way the server calls them (a context with request headers)."""

import inspect
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db
from app.jobsearch.browser import mcp as browser_mcp
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.main import app
from app.jobsearch.models import ActionRun
from app.jobsearch.owner_actions import mcp as owner_mcp
from app.jobsearch.work.door import token_matches
from app.models.work import TrailEvent
from app.settings import settings
from tests.support import OWNER

OWNER_TOKEN = "js_owner_token_1234567890"
SCOUT_TOKEN = "js_scout_token_0987654321"
BROWSER_TOKEN = "js_browser_token_5678901234"

TOOLS = {"actions.list", "act.read", "act.do", "act.job"}


@pytest.fixture
def owner_door(tmp_path, monkeypatch):
    """The three doors' token files and a context carrying the owner's token."""
    for name, token in (("owner-token", OWNER_TOKEN), ("agent-token", SCOUT_TOKEN), ("browser-token", BROWSER_TOKEN)):
        (tmp_path / name).write_text(token + "\n", encoding="utf-8")
    monkeypatch.setattr(settings, "owner_token_file", str(tmp_path / "owner-token"))
    monkeypatch.setattr(settings, "agent_token_file", str(tmp_path / "agent-token"))
    monkeypatch.setattr(settings, "browser_token_file", str(tmp_path / "browser-token"))
    monkeypatch.setattr(settings, "owner", OWNER)
    return SimpleNamespace(headers={"authorization": f"Bearer {OWNER_TOKEN}"})


def _calls(ctx):
    return [owner_mcp.actions_list(ctx), owner_mcp.act_read(ctx, "queues"),
            owner_mcp.act_do(ctx, "requeue", {"scope": "stale"}, False), owner_mcp.act_job(ctx)]


# --- the contract -----------------------------------------------------------------------------------------------------


async def test_the_door_lists_four_tools_and_none_of_the_other_doors():
    listed = {t.name: t for t in await owner_mcp.mcp.list_tools()}
    assert set(listed) == TOOLS, "four tools, not one for each action: each definition is read again on every turn"
    assert listed["act.do"].input_schema["required"] == ["action"]
    assert set(listed["act.do"].input_schema["properties"]) == {"action", "args", "dry_run"}
    assert listed["act.do"].input_schema["properties"]["dry_run"]["default"] is False
    assert listed["act.read"].input_schema["required"] == ["action"]
    assert set(listed["act.job"].input_schema["properties"]) == {"id"} and not listed["act.job"].input_schema.get(
        "required")
    for tool in listed.values():
        assert tool.description and len(tool.description) > 60
    assert "20 seconds" in listed["act.do"].description and "dry_run" in listed["act.do"].description
    other = {t.name for t in await mcp_server.mcp.list_tools()} | {t.name for t in await browser_mcp.mcp.list_tools()}
    assert not other & TOOLS


def test_a_dry_run_is_not_the_default():
    assert inspect.signature(owner_mcp.act_do).parameters["dry_run"].default is False


# --- the token --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer wrong-token", "Basic abc", f"bearer {OWNER_TOKEN}x",
                                    f"Bearer {SCOUT_TOKEN}", f"Bearer {BROWSER_TOKEN}"])
async def test_no_token_a_wrong_one_the_agent_token_and_the_browser_token_are_each_refused_and_nothing_runs(
        js, owner_door, header):
    ctx = SimpleNamespace(headers={"authorization": header} if header is not None else {})
    for call in _calls(ctx):
        got = await call
        assert got["ok"] is False and "owner token" in got["reason"], got
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(ActionRun)) == 0, "nothing ran, no row"
        assert await session.scalar(select(func.count()).select_from(TrailEvent)) == 0


async def test_the_owner_token_passes_every_tool(js, owner_door):
    listed, read, wrote, jobs = [await call for call in _calls(owner_door)]
    assert listed["ok"] and {a["name"] for a in listed["actions"]} >= {"health", "requeue", "board_feed.read"}
    assert read["ok"] and "pool_waiting" in read["result"]
    assert wrote["ok"] and wrote["state"] == "done" and wrote["dry_run"] is False
    assert jobs["ok"] and [j["job"] for j in jobs["jobs"]] == [wrote["job"]]


async def test_the_owner_token_opens_no_other_door_and_no_other_token_opens_this(owner_door):
    assert token_matches(f"Bearer {OWNER_TOKEN}", settings.token_path()) is False
    assert token_matches(f"Bearer {OWNER_TOKEN}", settings.browser_token_path()) is False
    assert mcp_server.token_ok(f"Bearer {OWNER_TOKEN}") is False
    assert token_matches(f"Bearer {OWNER_TOKEN}", settings.owner_token_path()) is True
    for other in (SCOUT_TOKEN, BROWSER_TOKEN):
        assert token_matches(f"Bearer {other}", settings.owner_token_path()) is False


async def test_no_token_file_means_nothing_gets_in(js, owner_door, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "owner_token_file", str(tmp_path / "missing"))
    got = await owner_mcp.act_do(owner_door, "requeue", {"scope": "stale"}, False)
    assert got["ok"] is False and "owner token" in got["reason"]
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(ActionRun)) == 0


def test_the_owner_token_is_a_file_in_the_secrets_folder_of_the_data_folder(monkeypatch, tmp_path):
    from app import settings as s

    monkeypatch.setenv("RESUMEBOT_DATA_DIR", str(tmp_path))
    fresh = s.JobSearchSettings(_env_file=None)
    assert fresh.owner_token_path() == tmp_path / "secrets" / "owner-token"
    monkeypatch.delenv("RESUMEBOT_DATA_DIR")
    assert s.JobSearchSettings(_env_file=None).owner_token_file == "/secrets/owner-token"


# --- the gate in front, and the guard in front of that ----------------------------------------------------------------


class _Inner:
    """What the gate protects: it records that it was reached."""

    reached = 0

    async def __call__(self, scope, receive, send):
        _Inner.reached += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.parametrize("header", [None, "Bearer nope", f"Bearer {SCOUT_TOKEN}", f"Bearer {BROWSER_TOKEN}"])
async def test_the_gate_answers_401_before_anything_is_read(owner_door, header):
    _Inner.reached = 0
    async with AsyncClient(transport=ASGITransport(app=owner_mcp.BearerGate(_Inner())), base_url="http://test") as c:
        got = await c.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           headers={"Authorization": header} if header else {})
    assert got.status_code == 401 and got.headers["www-authenticate"] == 'Bearer realm="resumebot-owner"'
    assert _Inner.reached == 0


async def test_the_gate_lets_the_owner_token_through(owner_door):
    _Inner.reached = 0
    async with AsyncClient(transport=ASGITransport(app=owner_mcp.BearerGate(_Inner())), base_url="http://test") as c:
        got = await c.post("/", json={}, headers={"Authorization": f"Bearer {OWNER_TOKEN}"})
    assert got.status_code == 200 and _Inner.reached == 1


async def test_the_app_mounts_the_door_behind_its_gate_and_the_other_two_stay_as_they_were(owner_door):
    paths = {getattr(route, "path", None): route.app for route in app.routes if hasattr(route, "app")}
    assert isinstance(paths["/mcp-owner"], owner_mcp.BearerGate)
    assert isinstance(paths["/mcp-browser"], browser_mcp.BearerGate) and not isinstance(
        paths["/mcp-browser"], owner_mcp.BearerGate)
    assert "/mcp" in paths and not isinstance(paths["/mcp"], owner_mcp.BearerGate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for token in (None, SCOUT_TOKEN, BROWSER_TOKEN):
            got = await c.post("/mcp-owner/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                               headers={"Authorization": f"Bearer {token}"} if token else {})
            assert got.status_code == 401, token
        got = await c.post("/mcp-browser/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           headers={"Authorization": f"Bearer {OWNER_TOKEN}"})
        assert got.status_code == 401, "and this token opens neither of the others"


async def test_a_host_or_origin_that_is_not_local_is_refused_before_the_token_is_looked_at(owner_door):
    headers = {"Authorization": f"Bearer {OWNER_TOKEN}", "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://attacker.example.org") as c:
        assert (await c.post("/mcp-owner/", json=body, headers=headers)).status_code == 421
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        got = await c.post("/mcp-owner/", json=body, headers={**headers, "Origin": "https://attacker.example.org"})
        assert got.status_code == 403


# --- a dry run through the door ---------------------------------------------------------------------------------------


async def test_act_do_with_dry_run_true_changes_nothing_and_says_so_first(js, owner_door):
    got = await owner_mcp.act_do(owner_door, "requeue", {"scope": "all"}, True)
    assert got["ok"] is True and got["dry_run"] is True and list(got)[1] == "said"
    assert got["said"].startswith("Nothing was changed")
    async with db.session_factory()() as session:
        [row] = (await session.scalars(select(ActionRun))).all()
        assert row.dry_run is True and row.state == "done"
        assert await session.scalar(select(func.count()).select_from(TrailEvent)) == 0


async def test_a_refusal_is_an_ordinary_result_that_names_the_problem(js, owner_door):
    unknown = await owner_mcp.act_do(owner_door, "drop.table", {}, False)
    assert unknown["ok"] is False and "no action drop.table" in unknown["reason"] and unknown["how_to_fix"]
    wrong = await owner_mcp.act_do(owner_door, "health", {}, False)
    assert wrong["ok"] is False and "use act.read" in wrong["reason"]
    extra = await owner_mcp.act_read(owner_door, "health", {"sql": "select 1"})
    assert extra["ok"] is False and "does not take" in extra["reason"]


# --- the token file as Windows writes it, and why a call was refused -----------------------------------------


async def _post(header):
    async with AsyncClient(transport=ASGITransport(app=owner_mcp.BearerGate(_Inner())), base_url="http://test") as c:
        return await c.post("/", json={}, headers={"Authorization": header} if header else {})


@pytest.mark.parametrize("encoding", ["utf-8-sig"])
async def test_a_token_file_with_a_byte_order_mark_is_accepted(owner_door, tmp_path, encoding):
    (tmp_path / "owner-token").write_text(OWNER_TOKEN + "\r\n", encoding=encoding)
    assert (tmp_path / "owner-token").read_bytes().startswith(b"\xef\xbb\xbf")
    _Inner.reached = 0
    got = await _post(f"Bearer {OWNER_TOKEN}")
    assert got.status_code == 200 and _Inner.reached == 1
    assert token_matches(f"Bearer {OWNER_TOKEN}", settings.owner_token_path()) is True


async def test_a_utf16_token_file_is_refused_with_401_and_no_exception(owner_door, tmp_path):
    (tmp_path / "owner-token").write_bytes(OWNER_TOKEN.encode("utf-16"))
    _Inner.reached = 0
    got = await _post(f"Bearer {OWNER_TOKEN}")
    assert got.status_code == 401 and _Inner.reached == 0
    assert token_matches(f"Bearer {OWNER_TOKEN}", settings.owner_token_path()) is False


@pytest.mark.parametrize("case, expect", [
    ("none", "no Authorization header"),
    ("basic", "not a bearer header"),
    ("missing", "token file unreadable"),
    ("empty", "token file empty"),
    ("wrong", "token does not match"),
])
async def test_each_refusal_logs_its_reason_and_nothing_of_the_token(owner_door, tmp_path, caplog, case, expect):
    header = {"none": None, "basic": "Basic abc", "missing": f"Bearer {OWNER_TOKEN}", "empty": f"Bearer {OWNER_TOKEN}",
              "wrong": "Bearer js_wrong_token_abcdefghij"}[case]
    if case == "missing":
        (tmp_path / "owner-token").unlink()
    if case == "empty":
        (tmp_path / "owner-token").write_text("\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        got = await _post(header)
    assert got.status_code == 401 and got.json() == {"error": owner_mcp.BearerGate.reason}
    lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(lines) == 1 and "resumebot-owner" in lines[0] and expect in lines[0]
    if case == "missing":
        assert "FileNotFoundError" in lines[0] and "owner-token" in lines[0]
    for secret in (OWNER_TOKEN, "js_wrong_token_abcdefghij", "wrong_token", "owner_token", "1234567890"):
        assert secret not in lines[0]
