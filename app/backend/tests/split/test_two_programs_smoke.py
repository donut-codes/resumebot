"""`stack.py smoke` against stand-ins for the two programs (tests/split/smoke_support.py): it checks the page and the
engine, asks the lane doors at the engine and the owner door at the page, and fails on a missing engine, a version that
differs and an owner door that does not answer. Everything is made up."""

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.split.smoke_support import TOKENS, Fake

STACK = Path(__file__).resolve().parents[3] / "scripts" / "local" / "stack.py"

pytestmark = pytest.mark.skipif(not STACK.exists(), reason="the launcher is not in this image")

OWNER_DOOR = "/mcp-owner/ answers a call with its token (tools/list)"
ENGINE_READY = "the engine's /health: ok, database ok"


@pytest.fixture(scope="module")
def stack():
    spec = importlib.util.spec_from_file_location("resumebot_stack_for_smoke", STACK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def make():
    made: list[Fake] = []

    def build(**kwargs) -> Fake:
        fake = Fake(**kwargs)
        fake.start()
        made.append(fake)
        return fake

    yield build
    for fake in made:
        fake.stop()


def _pair(make, *, page_version="9.9.9", engine_version="9.9.9", owner_door_answers=True):
    page = make(page=True, version=page_version, doors=("/mcp-owner/",), owner_door_answers=owner_door_answers)
    engine = make(page=False, version=engine_version, doors=("/mcp/", "/mcp-browser/"))
    return page, engine


def _run(stack, tmp_path, monkeypatch, page, engine, *, lanes=False):
    environ = {"RESUMEBOT_APP_URL": page.url}
    for name, door in (("JOBSEARCH_AGENT_TOKEN_FILE", "/mcp/"), ("JOBSEARCH_BROWSER_TOKEN_FILE", "/mcp-browser/"),
                       ("JOBSEARCH_OWNER_TOKEN_FILE", "/mcp-owner/")):
        file = tmp_path / name
        file.write_text(TOKENS[door] + "\n", encoding="utf-8")
        environ[name] = str(file)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(stack, "ENGINE_OFFSET", engine.port - page.port)
    monkeypatch.setattr(stack, "SMOKE_PAGE_WAIT", 1)
    monkeypatch.setattr(stack, "SMOKE_ENGINE_WAIT", 1)
    monkeypatch.setattr(stack, "SMOKE_LIVE_WAIT", 1)
    monkeypatch.setattr(stack, "version_of_checkout", lambda: "9.9.9")
    return stack.smoke(stack.Config(tmp_path, environ), lanes)


def _fails(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("[smoke] FAIL ")]


def test_the_smoke_passes_when_the_page_and_the_engine_both_answer(stack, make, tmp_path, monkeypatch, capsys):
    page, engine = _pair(make)
    code = _run(stack, tmp_path, monkeypatch, page, engine)
    out = capsys.readouterr().out
    assert code == 0 and _fails(out) == []
    assert "[smoke] ok   " + ENGINE_READY in out
    assert "[smoke] PASS" in out


def test_the_lane_doors_are_asked_at_the_engine_and_the_owner_door_at_the_page(stack, make, tmp_path, monkeypatch):
    page, engine = _pair(make)
    _run(stack, tmp_path, monkeypatch, page, engine)
    assert "POST /mcp/" in engine.asked and "POST /mcp-browser/" in engine.asked
    assert "POST /mcp-owner/" not in engine.asked
    assert "POST /mcp/" not in page.asked and "POST /mcp-browser/" not in page.asked
    assert "POST /mcp-owner/" in page.asked


def test_a_missing_engine_fails_the_smoke(stack, make, tmp_path, monkeypatch, capsys):
    page, engine = _pair(make)
    engine.stop()
    # Windows takes about two seconds to refuse a connection to a port nobody listens on, and the smoke asks the
    # stopped engine seven times (14 seconds). What `fetch` answers for a refused connection is status 0 with nothing,
    # so that is answered at once; the smoke's own logic is unchanged and the page is still reached for real.
    real_fetch = stack.fetch
    monkeypatch.setattr(
        stack, "fetch", lambda url, **kwargs: (0, {}, b"") if url.startswith(engine.url) else real_fetch(url, **kwargs))
    code = _run(stack, tmp_path, monkeypatch, page, engine)
    fails = _fails(capsys.readouterr().out)
    assert code == 1
    assert any(ENGINE_READY in line for line in fails)


def test_an_engine_with_another_version_fails_the_smoke_and_nothing_else(stack, make, tmp_path, monkeypatch, capsys):
    page, engine = _pair(make, engine_version="8.8.8")
    code = _run(stack, tmp_path, monkeypatch, page, engine)
    fails = _fails(capsys.readouterr().out)
    assert code == 1
    assert len(fails) == 1 and "the engine's /health: version 9.9.9" in fails[0] and "8.8.8" in fails[0]


def test_an_owner_door_that_does_not_answer_fails_the_smoke(stack, make, tmp_path, monkeypatch, capsys):
    page, engine = _pair(make, owner_door_answers=False)
    code = _run(stack, tmp_path, monkeypatch, page, engine)
    fails = _fails(capsys.readouterr().out)
    assert code == 1
    assert len(fails) == 1 and OWNER_DOOR in fails[0]


def test_the_lanes_are_checked_only_when_asked(stack, make, tmp_path, monkeypatch, capsys):
    page, engine = _pair(make)
    asked: list[int] = []
    monkeypatch.setattr(stack, "lane_health", lambda port: asked.append(port) or False)
    # One round of asks: the minute of asking again (stack.lanes_answer) is pinned on a fake clock in test_local_stack.
    def one_round(config, seconds=0):
        return [stack.lane_health(config.scout_port + index) for index in range(stack.LANE_COUNT)]

    monkeypatch.setattr(stack, "lanes_answer", one_round)
    assert _run(stack, tmp_path, monkeypatch, page, engine) == 0 and asked == []
    capsys.readouterr()
    assert _run(stack, tmp_path, monkeypatch, page, engine, lanes=True) == 1
    fails = _fails(capsys.readouterr().out)
    assert len(asked) == 5 and len(fails) == 1 and "the 5 lanes answer /health" in fails[0]


def test_a_stream_that_never_says_hello_fails_both_live_checks(stack, make, tmp_path, monkeypatch, capsys):
    page, engine = _pair(make)
    page.hello = False
    code = _run(stack, tmp_path, monkeypatch, page, engine)
    fails = _fails(capsys.readouterr().out)
    assert code == 1
    assert any("/api/v1/live opens with hello" in line for line in fails)
    assert any("a resync datagram" in line for line in fails)


def test_a_datagram_the_page_does_not_pass_on_fails_only_the_resync_check(stack, make, tmp_path, monkeypatch, capsys):
    page, engine = _pair(make)
    page.resyncs = False
    code = _run(stack, tmp_path, monkeypatch, page, engine)
    fails = _fails(capsys.readouterr().out)
    assert code == 1
    assert len(fails) == 1 and "a resync datagram" in fails[0]
    (body,) = page.datagrams
    assert body["v"] == 1 and body["m"] == [["resync", "", []]] and isinstance(body["b"], str) and body["n"] == 1


def test_every_smoke_sends_its_datagram_from_a_sender_the_page_has_not_seen(stack, make, tmp_path, monkeypatch):
    page, engine = _pair(make)
    assert _run(stack, tmp_path, monkeypatch, page, engine) == 0
    assert _run(stack, tmp_path, monkeypatch, page, engine) == 0
    first, second = page.datagrams
    assert first["b"] != second["b"]
