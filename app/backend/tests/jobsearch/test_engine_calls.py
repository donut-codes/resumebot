"""engine_calls: the one door route code uses to tell the engine (`work/engine_calls.py`). `Local`, the default, calls
the engine's own functions in this process and looks each one up when it is called, so a test that replaces
`wake.knock` (the `knocks` fixture) still sees every knock; `install` swaps the backend. Everything here is made up."""

import ast
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobsearch.work import engine_calls, wake

BACKEND = Path(__file__).resolve().parents[2]

#: The modules that do the engine's work. A route module reaches them only through `engine_calls`.
ENGINE_MODULES = {
    "app.jobsearch.work.wake",
    "app.jobsearch.work.dispatcher",
    "app.jobsearch.loops.schedule",
    "app.sites.wake_ops",
    "app.jobsearch.work.engine_asks",
}

REASONS = (
    "SEARCH_STARTED", "PACKET_ASKED", "ANALYSIS_ASKED", "SITE_PROJECT", "TAG_ROLES", "PARSE_ASKED", "CHECK_ASKED",
    "GAPS_ASKED", "EMPLOYERS_ASKED", "MAIL_ASKED", "COACH_ASKED", "RUN_NOW",
)


def engine_imports(source: str) -> list[str]:
    """The engine modules a file's top-level statements import (an import inside a function is not top level)."""
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            found += [
                f"{node.module}.{alias.name}" for alias in node.names
                if f"{node.module}.{alias.name}" in ENGINE_MODULES or node.module in ENGINE_MODULES
            ]
        elif isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name in ENGINE_MODULES]
    return found


class Fake:
    """A backend that only writes down what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def knock(self, run_id, reason):
        self.calls.append(("knock", run_id, reason))
        return True

    async def dispatch_now(self):
        self.calls.append(("dispatch_now",))
        return 0

    async def knocks(self, pending):
        self.calls.append(("knocks", list(pending)))

    async def send_wakes(self, wakes):
        self.calls.append(("send_wakes", list(wakes)))
        return ["p1"]

    def nudge(self):
        self.calls.append(("nudge",))


@pytest.fixture
def fake(monkeypatch):
    """Every call to the door goes to a backend that writes it down; the local backend comes back afterwards. The wake
    address is empty for the test, so an engine function that a route still called directly would return quietly (a
    knock answers False, a pass of the dispatcher 0) and the fake would see nothing."""
    from app.settings import settings

    monkeypatch.setattr(settings, "wake_url", "")
    backend = Fake()
    engine_calls.install(backend)
    yield backend
    engine_calls.install(None)


def test_the_reasons_are_the_wake_modules_own():
    for name in REASONS:
        assert getattr(engine_calls, name) == getattr(wake, name), name


async def test_the_local_knock_calls_wake_knock_once_with_the_same_arguments(monkeypatch):
    seen = []

    async def knock(run_id, reason=wake.SEARCH_STARTED):
        seen.append((run_id, reason))
        return True

    monkeypatch.setattr(wake, "knock", knock)
    assert await engine_calls.knock("r1", engine_calls.GAPS_ASKED) is True
    assert await engine_calls.knock("r2") is True
    assert seen == [("r1", "gaps_asked"), ("r2", "search_started")]


async def test_the_local_dispatch_now_calls_the_dispatcher_once(monkeypatch):
    from app.jobsearch.work import dispatcher

    seen = []

    async def dispatch_now(owner=None):
        seen.append(owner)
        return 3

    monkeypatch.setattr(dispatcher, "dispatch_now", dispatch_now)
    assert await engine_calls.dispatch_now() == 3
    assert seen == [None]


async def test_the_local_knocks_call_the_schedules_send_knocks_with_the_list(monkeypatch):
    from app.jobsearch.loops import schedule

    seen = []

    async def send_knocks(knocks):
        seen.append(knocks)

    monkeypatch.setattr(schedule, "send_knocks", send_knocks)
    await engine_calls.knocks([("k", "search_started")])
    assert seen == [[("k", "search_started")]]


async def test_the_local_send_wakes_call_wake_ops_send_all(monkeypatch):
    from app.sites import wake_ops

    seen = []

    async def send_all(wakes):
        seen.append(list(wakes))
        return ["p1"]

    monkeypatch.setattr(wake_ops, "send_all", send_all)
    assert await engine_calls.send_wakes([("p1", None)]) == ["p1"]
    assert seen == [[("p1", None)]]


def test_the_local_nudge_calls_consume_wake(monkeypatch):
    from app.jobsearch.work.queue import consume

    seen = []
    monkeypatch.setattr(consume, "wake", lambda: seen.append(1))
    engine_calls.nudge()
    assert seen == [1]


async def test_a_fake_backend_takes_every_call_and_the_wake_is_never_reached(fake, monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("the local wake was called")

    monkeypatch.setattr(wake, "knock", boom)
    assert await engine_calls.knock("r1", "check_asked") is True
    await engine_calls.dispatch_now()
    await engine_calls.knocks([("k", "search_started")])
    assert await engine_calls.send_wakes([("p1", None)]) == ["p1"]
    engine_calls.nudge()
    assert fake.calls == [
        ("knock", "r1", "check_asked"), ("dispatch_now",), ("knocks", [("k", "search_started")]),
        ("send_wakes", [("p1", None)]), ("nudge",),
    ]


async def test_install_with_nothing_puts_the_local_backend_back(monkeypatch):
    seen = []

    async def knock(run_id, reason=wake.SEARCH_STARTED):
        seen.append(run_id)
        return True

    monkeypatch.setattr(wake, "knock", knock)
    engine_calls.install(Fake())
    engine_calls.install(None)
    await engine_calls.knock("r1")
    assert seen == ["r1"]


def test_the_door_loads_none_of_the_engines_modules():
    code = "\n".join([
        "import sys",
        "import app.jobsearch.work.engine_calls",
        f"loaded = [m for m in {sorted(ENGINE_MODULES)!r} if m in sys.modules]",
        "assert not loaded, loaded",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]


def test_the_scan_finds_a_top_level_import_of_an_engine_module():
    assert engine_imports("from app.jobsearch.work import job_queue, wake\n") == ["app.jobsearch.work.wake"]
    assert engine_imports("from app.sites import inbox_asks, wake_ops\n") == ["app.sites.wake_ops"]
    assert engine_imports("from app.jobsearch.loops import schedule\n") == ["app.jobsearch.loops.schedule"]
    assert engine_imports("import app.jobsearch.work.dispatcher\n") == ["app.jobsearch.work.dispatcher"]


def test_the_scan_ignores_the_door_and_an_import_inside_a_function():
    source = "from app.jobsearch.work import engine_calls\n\n\ndef f():\n    from app.jobsearch.work import wake\n"
    assert engine_imports(source) == []


class _Session:
    """A session that only commits: the route code under test commits, and then tells the engine."""

    async def commit(self) -> None:
        return None


APP = BACKEND / "app"

#: Modules whose top level must import no engine module. Paths are under app/backend/app. Each card of the lane
#: `engine-calls-wakes` adds the files it moved to the door.
DOOR_ONLY = [
    "inbox/routes.py",
    "jobsearch/gaps/routes.py",
    "jobsearch/interviews/routes.py",
    "jobsearch/jobs/check_routes.py",
    "jobsearch/jobs/parse_routes.py",
    "jobsearch/owner_actions/asked.py",
    "jobsearch/owner_actions/onboarding.py",
    "jobsearch/owner_actions/writes.py",
    "jobsearch/pool/routes.py",
    "jobsearch/routes/profile.py",
    "jobsearch/routes/role.py",
    "jobsearch/writing/library.py",
    "jobsearch/writing/routes.py",
    "jobsearch/missions/routes.py",
    "jobsearch/work/session_kinds.py",
    "jobsearch/missions/record.py",
    "jobsearch/missions/catch_up.py",
    "jobsearch/missions/kinds/research.py",
]


@pytest.mark.parametrize("name", DOOR_ONLY)
def test_a_door_only_module_imports_no_engine_module_at_the_top(name):
    assert engine_imports((APP / name).read_text(encoding="utf-8")) == []


async def test_the_gap_routes_wake_through_the_door(fake):
    from app.jobsearch.gaps import routes as gap_routes

    assert await gap_routes._started(_Session(), SimpleNamespace(id="run-1")) == {"id": "run-1"}
    assert await gap_routes._coach_started(_Session(), SimpleNamespace(id="job-1")) == {"id": "job-1"}
    assert fake.calls == [("knock", "run-1", "gaps_asked"), ("knock", "job-1", "coach_asked")]


async def test_run_all_wakes_the_lanes_through_the_door(fake, monkeypatch):
    from app.jobsearch.pool import asked as pool_asked

    async def run_all(session, owner):
        return {"queued": {"researcher": 2}, "held": None}

    monkeypatch.setattr(pool_asked.pool, "run_all", run_all)
    assert await pool_asked.run_all(_Session(), "owner", {}) == {"queued": {"researcher": 2}, "held": None}
    assert fake.calls == [("knock", "run-all", "run_now")]


async def test_tagging_roles_wakes_the_titles_session_through_the_door(fake, monkeypatch):
    from app.jobsearch.routes import profile as profile_routes

    async def start(session, owner):
        return SimpleNamespace(id="job-1"), 4

    monkeypatch.setattr(profile_routes.tagging, "start", start)
    assert await profile_routes.tag_roles(_Session()) == {"open": True, "count": 4}
    assert fake.calls == [("knock", "job-1", "tag_roles")]


async def test_moving_a_role_to_ready_wakes_the_analyst_and_asks_for_a_pass_through_the_door(fake):
    from app.jobsearch.work import engine_asks

    role_id = uuid.UUID(int=1)
    await engine_asks.wake_for(role_id, {"research": {"already": False}, "source": False, "rank": False})
    assert fake.calls == [("knock", str(role_id), "analysis_asked"), ("dispatch_now",)]


async def test_a_role_researched_already_wakes_nobody_and_asks_for_no_pass(fake):
    from app.jobsearch.work import engine_asks

    role_id = uuid.UUID(int=1)
    await engine_asks.wake_for(role_id, {"research": {"already": True}, "source": False, "rank": False})
    assert fake.calls == []


async def test_a_started_library_build_wakes_a_session_through_the_door(fake):
    from app.jobsearch.writing import library

    await library.started(SimpleNamespace(id="lib-1"))
    await library.started(None)
    assert fake.calls == [("knock", "lib-1", "packet_asked")]


async def test_asking_for_a_packet_wakes_the_writer_through_the_door(fake, monkeypatch):
    from app.jobsearch.writing import routes as writing_routes

    async def ask(session, owner, role_id, feedback):
        return SimpleNamespace(id="pk-1")

    monkeypatch.setattr(writing_routes.packets, "ask", ask)
    monkeypatch.setattr(writing_routes.packets, "view", lambda packet: {"id": packet.id})
    out = await writing_routes.ask_packet(uuid.UUID(int=2), writing_routes.AskPacket(feedback=""), _Session())
    assert out == {"id": "pk-1"}
    assert fake.calls == [("knock", "pk-1", "packet_asked")]


async def test_running_a_mission_sends_its_knocks_through_the_door(fake, monkeypatch):
    from app.jobsearch.missions import asked as mission_asked

    async def get(session, owner, mission_id):
        return object()

    async def run_now(session, owner, mission, by):
        return SimpleNamespace(run={"outcome": "running"}, wake=[("k1", "search_started")])

    monkeypatch.setattr(mission_asked.missions, "get", get)
    monkeypatch.setattr(mission_asked.missions, "run_now", run_now)
    answer = await mission_asked.run(_Session(), "owner", {"mission_id": str(uuid.UUID(int=3))})
    assert answer == {"run": {"outcome": "running"}}
    assert fake.calls == [("knocks", [("k1", "search_started")])]
