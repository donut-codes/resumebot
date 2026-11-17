"""What the doors log: one start-up line for each program, and why a call was refused for its token (docs/PRD.md
path 50.2). Neither ever holds a token."""

import logging
import os
from types import SimpleNamespace

import pytest

from app import program
from app.jobsearch.browser import mcp as browser_mcp
from app.jobsearch.doors import scout as scout_door
from app.jobsearch.owner_actions import mcp as owner_mcp
from app.jobsearch.work import door as door_module
from app.settings import DATA_DIR_VARIABLE, VERSION, settings
from tests.support import OWNER

OWNER_TOKEN = "js_distinct_owner_secret_314159"
AGENT_TOKEN = "js_distinct_agent_secret_271828"
BROWSER_TOKEN = "js_distinct_browser_secret_161803"
WRONG = "js_distinct_wrong_secret_577215"
SECRETS = (OWNER_TOKEN, AGENT_TOKEN, BROWSER_TOKEN, WRONG, "314159", "271828", "161803", "577215")


@pytest.fixture
def tokens(tmp_path, monkeypatch):
    for name, token in (("owner-token", OWNER_TOKEN), ("agent-token", AGENT_TOKEN)):
        (tmp_path / name).write_text(token + "\n", encoding="utf-8")
    # the browser token file is left out on purpose: the line says it does not exist
    monkeypatch.setattr(settings, "owner_token_file", str(tmp_path / "owner-token"))
    monkeypatch.setattr(settings, "agent_token_file", str(tmp_path / "agent-token"))
    monkeypatch.setattr(settings, "browser_token_file", str(tmp_path / "browser-token"))
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setenv(DATA_DIR_VARIABLE, str(tmp_path))
    monkeypatch.setattr(program, "PROGRAM", "page")
    door_module._told.clear()
    yield tmp_path
    door_module._told.clear()


def test_the_start_line_names_version_program_pid_and_each_token_file_and_never_a_token(tokens):
    line = door_module.start_line("engine")
    assert f"version={VERSION}" in line and "program=page" in line and f"pid={os.getpid()}" in line
    assert f"data={tokens}" in line
    agent = tokens / "agent-token"
    assert f"agent_token={agent} exists=yes size={agent.stat().st_size} modified=" in line
    assert f"browser_token={tokens / 'browser-token'} exists=no" in line
    assert "owner_token" not in line, "the engine does not mount the owner's door"
    for secret in SECRETS:
        assert secret not in line


def test_the_page_start_line_lists_the_owner_token_only(tokens):
    line = door_module.start_line("page")
    owner = tokens / "owner-token"
    assert f"owner_token={owner} exists=yes size={owner.stat().st_size} modified=" in line
    assert "agent_token" not in line and "browser_token" not in line


def test_the_whole_app_lists_all_three_doors_and_the_port_when_the_command_line_names_it(tokens, monkeypatch):
    monkeypatch.setattr("sys.argv", ["uvicorn", "app.jobsearch.page:app", "--port", "8430"])
    line = door_module.start_line("all")
    assert "port=8430" in line
    assert all(f"{name}_token=" in line for name in ("owner", "agent", "browser"))


def test_no_port_on_the_command_line_leaves_it_out(tokens, monkeypatch):
    monkeypatch.setattr("sys.argv", ["pytest"])
    assert "port=" not in door_module.start_line("page")


async def test_a_call_refused_for_its_token_logs_its_reason_and_not_the_token(tokens, caplog):
    ctx = SimpleNamespace(headers={"authorization": f"Bearer {WRONG}"})
    with caplog.at_level(logging.INFO):
        got = await owner_mcp.act_read(ctx, "queues")
    assert got["ok"] is False and "owner token" in got["reason"]
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(lines) == 1
    assert "owner door" in lines[0] and "act.read" in lines[0] and "token does not match" in lines[0]
    for secret in SECRETS:
        assert secret not in caplog.text


@pytest.mark.parametrize("header, why", [(None, "no Authorization header"), ("Basic abc", "not a bearer header")])
async def test_the_other_reasons_are_named(tokens, caplog, header, why):
    ctx = SimpleNamespace(headers={"authorization": header} if header else {})
    with caplog.at_level(logging.INFO):
        await scout_door.door.call(ctx, "work.next", None)  # refused before the body is looked at
    assert why in caplog.text and "mcp" in caplog.text


async def test_a_reason_is_logged_once_a_minute_for_each_door_and_reason(tokens, caplog, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(door_module, "clock", lambda: now[0])
    wrong = SimpleNamespace(headers={"authorization": f"Bearer {WRONG}"})
    none = SimpleNamespace(headers={})

    def warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            await owner_mcp.act_read(wrong, "queues")
        assert len(warnings()) == 1, "the same reason on the same door within the minute"
        await owner_mcp.act_read(none, "queues")
        assert len(warnings()) == 2, "another reason is its own line"
        await scout_door.door.call(wrong, "work.next", None)
        assert len(warnings()) == 3, "another door is its own line"
        now[0] += 59
        await owner_mcp.act_read(wrong, "queues")
        assert len(warnings()) == 3
        now[0] += 2
        await owner_mcp.act_read(wrong, "queues")
        assert len(warnings()) == 4, "a minute on, it says it again"


@pytest.mark.parametrize("door, label, setting", [
    (scout_door.door, "mcp", "agent_token_file"),
    (browser_mcp.door, "browser door", "browser_token_file"),
    (owner_mcp.door, "owner door", "owner_token_file"),
])
async def test_each_door_names_its_own_token_file_in_the_reason_it_refuses(tokens, caplog, monkeypatch, door, label,
                                                                          setting):
    """A door's in-call refusal reads its own token file, so an empty one is named as empty, not as 'token refused'."""
    empty = tokens / "empty-token"
    empty.write_text(" ", encoding="utf-8")
    monkeypatch.setattr(settings, setting, str(empty))
    ctx = SimpleNamespace(headers={"authorization": f"Bearer {WRONG}"})
    with caplog.at_level(logging.INFO):
        got = await door.call(ctx, "some.tool", None)
    assert got["ok"] is False
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(lines) == 1 and label in lines[0] and "token file empty" in lines[0]
