"""The local launcher (app/scripts/local/stack.py): what it reads, what it starts the app with and how it
keeps its pid files. Not the processes themselves: those are proved by running it (`stack.py smoke`). The launcher is
not in the backend's image (only app/backend is), so these skip there. Everything is made up."""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

STACK = Path(__file__).resolve().parents[3] / "scripts" / "local" / "stack.py"

pytestmark = pytest.mark.skipif(not STACK.exists(), reason="the launcher is not in this image")


@pytest.fixture(scope="module")
def stack():
    spec = importlib.util.spec_from_file_location("resumebot_stack_under_test", STACK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.real_venv_state = module.venv_state  # the autouse fixture below replaces these
    module.real_holder = module.holder
    return module


@pytest.fixture(autouse=True)
def _nothing_listens(stack, monkeypatch, tmp_path_factory):
    # The check itself is kept beside its stand-in, for the test of the check.
    monkeypatch.setattr(stack, "real_port_is_free", stack.port_is_free, raising=False)
    monkeypatch.setattr(stack, "port_is_free", lambda port, udp: True)
    monkeypatch.setattr(stack, "holder", lambda port, udp: None)
    # What `problems` reads from this machine and its checkout, made up: a Python environment that runs (when its file
    # is there) and a built page. The tests of those checks undo them.
    page = tmp_path_factory.mktemp("built-page") / "index.html"
    page.write_text("<div id='root'></div>", encoding="utf-8")
    monkeypatch.setattr(stack, "PAGE_INDEX", page)
    monkeypatch.setattr(stack, "venv_state", lambda cfg: ("ok", "3.51.3") if cfg.python.exists() else ("missing", ""))


def config(stack, tmp_path, file: str = "", **environ):
    (tmp_path / ".env").write_text(file, encoding="utf-8")
    return stack.Config(tmp_path, environ)


def test_the_settings_file_is_read_the_way_the_lanes_read_it(stack):
    parsed = stack.parse_env(
        "\ufeff# a comment\r\nJOBSEARCH_OWNER = José\r\nexport A=1\nB=\"two words\"\nC='x'\nD=plain # note\n"
        "E=C:\\data\\me\\x\n\nnot a setting\n")
    assert parsed == {"JOBSEARCH_OWNER": "José", "A": "1", "B": "two words", "C": "x", "D": "plain",
                      "E": "C:\\data\\me\\x"}


def test_the_apps_address_is_this_machines_and_names_its_port(stack, tmp_path):
    assert config(stack, tmp_path).app_url == "http://127.0.0.1:8430"
    assert config(stack, tmp_path).port == 8430
    assert config(stack, tmp_path, "RESUMEBOT_APP_URL=http://localhost:9000").port == 9000
    # The environment wins over the file.
    both = config(stack, tmp_path, "RESUMEBOT_APP_URL=http://localhost:9000", RESUMEBOT_APP_URL="http://127.0.0.1:9100")
    assert both.port == 9100
    with pytest.raises(stack.Stop, match="on this machine"):
        config(stack, tmp_path, "RESUMEBOT_APP_URL=http://192.168.1.20:8430").app_url  # noqa: B018


def test_the_search_lanes_port_is_where_the_app_sends_its_wake(stack, tmp_path):
    assert config(stack, tmp_path).scout_port == 8651
    assert config(stack, tmp_path, "SCOUT_PORT=9100").scout_port == 9100
    assert config(stack, tmp_path, "SCOUT_PORT=not-a-port").scout_port == 8651
    env = config(stack, tmp_path, "SCOUT_PORT=9100").app_environment(hold=False)
    assert env["JOBSEARCH_WAKE_URL"] == "http://127.0.0.1:9100/wake"


def test_the_app_is_started_with_the_data_folder_utf8_and_the_lanes_it_has(stack, tmp_path):
    shell = config(stack, tmp_path, PYTHONPATH="x", PYTHONHOME="y", VIRTUAL_ENV="z", KEPT="yes")
    env = shell.app_environment(hold=False)
    assert env["RESUMEBOT_DATA_DIR"] == str(tmp_path)
    assert (env["PYTHONUTF8"], env["PYTHONUNBUFFERED"], env["KEPT"]) == ("1", "1", "yes")
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & set(env), "nothing of the shell's Python leaks in"
    assert env["JOBSEARCH_ANALYSTS"] == "2", "five lanes, two of them researchers"
    assert "JOBSEARCH_RUN_SCHEDULE" not in env


def test_what_the_settings_name_is_not_overridden_by_the_launchers_defaults(stack, tmp_path):
    env = config(stack, tmp_path, "JOBSEARCH_ANALYSTS=1\nJOBSEARCH_WAKE_URL=http://127.0.0.1:1/wake\n").app_environment(False)
    named = "JOBSEARCH_ANALYSTS" in env or "JOBSEARCH_WAKE_URL" in env
    assert not named, "the app reads them from the file itself"
    env = config(stack, tmp_path, "", JOBSEARCH_ANALYSTS="3").app_environment(False)
    assert env["JOBSEARCH_ANALYSTS"] == "3"


def test_held_starts_the_app_with_its_schedule_off(stack, tmp_path):
    assert config(stack, tmp_path).app_environment(hold=True)["JOBSEARCH_RUN_SCHEDULE"] == "false"
    # Even when the file says it should run: the hold is for this start only.
    held = config(stack, tmp_path, "JOBSEARCH_RUN_SCHEDULE=true").app_environment(hold=True)
    assert held["JOBSEARCH_RUN_SCHEDULE"] == "false"


def test_a_start_that_cannot_work_says_why_in_plain_sentences(stack, tmp_path):
    cfg = config(stack, tmp_path, RESUMEBOT_VENV=str(tmp_path / "no-venv"))
    said = " ".join(stack.problems(cfg, hold=True))
    assert "Python environment" in said and "JOBSEARCH_DATABASE_URL" in said and "agent-token" in said
    assert "Node.js" not in said, "a held start has no lanes, so it needs no Node"
    ready = config(stack, tmp_path, "JOBSEARCH_DATABASE_URL=postgresql+asyncpg://a:b@127.0.0.1:1/c",
                   RESUMEBOT_VENV=str(tmp_path / "venv"))
    (ready.secrets).mkdir()
    for name in ("agent-token", "wake-secret"):
        (ready.secrets / name).write_text("x", encoding="utf-8")
    ready.python.parent.mkdir(parents=True)
    ready.python.write_text("", encoding="utf-8")
    assert stack.problems(ready, hold=True) == []


def test_the_data_folder_is_the_one_named_else_the_instances_own_folder(stack, monkeypatch, tmp_path):
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", str(tmp_path / "mine"))
    assert stack.data_folder() == tmp_path / "mine"
    monkeypatch.delenv("RESUMEBOT_DATA_DIR")
    assert stack.data_folder() == stack.default_data_folder()


def test_a_pid_file_names_a_process_only_while_it_is_that_process(stack, tmp_path):
    here = stack.PidFile(tmp_path, "supervisor")
    assert here.live() is None, "no file"
    here.write(os.getpid())
    assert here.live() == os.getpid()
    here.path.write_text(json.dumps({"pid": os.getpid(), "mark": 12345}), encoding="utf-8")
    assert here.live() is None, "the number is a later process's now: after a reboot it is never taken for ours"
    here.path.write_text("not json", encoding="utf-8")
    assert here.live() is None
    here.write(os.getpid())
    here.clear()
    assert not here.path.exists() and here.live() is None


def test_a_log_over_the_limit_is_kept_as_the_last_few(stack, tmp_path):
    log = tmp_path / "app.log"
    for number in range(1, 6):
        log.write_text(f"run {number} " + "x" * 50, encoding="utf-8")
        stack.rotate(log, limit=10, keep=3)
    assert not log.exists(), "each pass moved the log aside"
    kept = sorted(p.name for p in tmp_path.iterdir())
    assert kept == ["app.log.1", "app.log.2", "app.log.3"]
    assert (tmp_path / "app.log.1").read_text(encoding="utf-8").startswith("run 5")
    assert (tmp_path / "app.log.3").read_text(encoding="utf-8").startswith("run 3")
    small = tmp_path / "small.log"
    small.write_text("tiny", encoding="utf-8")
    stack.rotate(small, limit=10, keep=3)
    assert small.exists()


def test_the_version_the_smoke_test_expects_is_this_checkouts(stack):
    from app.settings import VERSION

    assert stack.version_of_checkout() == VERSION


def test_a_stopped_port_is_not_an_app(stack):
    """health() on an address nothing listens on is None, quickly, not an error."""
    started = time.monotonic()
    assert stack.health("http://127.0.0.1:1") is None
    assert time.monotonic() - started < 10


# --- the watch on the app ------------------------------------------------------------------------------------------


def test_the_watch_counts_failed_answers_in_a_row_only_once_the_app_is_up(stack):
    watch = stack.Watch(every=10, fails=3, grace=100)
    watch.restart(0)
    assert not watch.due(5) and watch.due(10)
    assert [watch.saw(False, t) for t in (10, 20, 30, 40)] == [False] * 4, "starting: within its grace"
    assert watch.saw(True, 50) is False
    assert [watch.saw(False, t) for t in (60, 70)] == [False, False]
    assert watch.saw(True, 80) is False, "an answer starts the count again"
    assert [watch.saw(False, t) for t in (90, 100, 110)] == [False, False, True]
    watch.restart(200)
    assert watch.saw(False, 210) is False, "a fresh start has its grace again"
    assert [watch.saw(False, t) for t in (310, 320, 330)] == [False, False, True], "past the grace it counts"


class _App:
    def __init__(self, name: str = "app", log: Path | None = None) -> None:
        self.name, self.log, self.ended = name, log, 0

    def alive(self) -> bool:
        return True

    def finish(self, grace: float) -> bool:
        self.ended += 1
        return True


def _fake_pg(stack, monkeypatch, tmp_path) -> None:
    script = tmp_path / "pg_fake.py"
    script.write_text("import sys\nprint('pid | state | blocked_by')\nprint('41 | idle in transaction | {}')\n"
                      "print('42 | active | {41}')\nprint(sys.argv[1])\n", encoding="utf-8")
    monkeypatch.setattr(stack, "PG_SCRIPT", script)


def test_an_app_that_stops_answering_is_restarted_after_a_snapshot_of_the_database(stack, monkeypatch, tmp_path):
    _fake_pg(stack, monkeypatch, tmp_path)
    cfg = config(stack, tmp_path, "JOBSEARCH_DATABASE_URL=postgresql+asyncpg://a:b@127.0.0.1:1/c")
    cfg.logs.mkdir()
    (cfg.logs / "app.log").write_text("the last thing the app said\n", encoding="utf-8")
    app, watch = _App(log=cfg.logs / "app.log"), stack.Watch(every=1, fails=3, grace=0)
    watch.restart(0)
    answers = iter([{"status": "ok"}, None, {"status": "degraded"}, None])
    ended = [stack.watch_child(cfg, app, watch, cfg.app_url, t, ask=lambda url: next(answers)) for t in (1, 2, 3, 4)]
    assert ended == [False, False, False, True] and app.ended == 1
    (kept,) = cfg.logs.glob("db-snapshot-*.txt")
    said = kept.read_text(encoding="utf-8")
    assert "did not answer /health 3 times in a row" in said
    assert "42 | active | {41}" in said and "snapshot" in said, "pg.py snapshot was asked"
    assert "the last thing the app said" in said


def test_an_app_that_answers_is_never_restarted_and_a_look_waits_its_turn(stack, monkeypatch, tmp_path):
    cfg = config(stack, tmp_path)
    app, watch = _App(), stack.Watch(every=10, fails=1, grace=0)
    watch.restart(0)
    asked: list[str] = []

    def ok(url):
        asked.append(url)
        return {"status": "ok"}

    assert [stack.watch_child(cfg, app, watch, cfg.app_url, t, ask=ok) for t in (5, 10, 15, 20)] == [False] * 4
    assert len(asked) == 2 and app.ended == 0


def test_only_the_newest_snapshots_are_kept(stack, monkeypatch, tmp_path):
    _fake_pg(stack, monkeypatch, tmp_path)
    cfg = config(stack, tmp_path, "JOBSEARCH_DATABASE_URL=postgresql+asyncpg://a:b@127.0.0.1:1/c")
    cfg.logs.mkdir()
    for n in range(stack.SNAPSHOTS_KEPT + 3):
        (cfg.logs / f"db-snapshot-20260101-0000{n:02d}.txt").write_text("old", encoding="utf-8")
    assert stack.db_snapshot(cfg, "a test") is not None
    kept = sorted(p.name for p in cfg.logs.glob("db-snapshot-*.txt"))
    assert len(kept) == stack.SNAPSHOTS_KEPT and kept[-1].startswith("db-snapshot-2026")
    assert "db-snapshot-20260101-000000.txt" not in kept


def test_a_wedged_engine_is_ended_and_its_snapshot_names_it_and_carries_its_own_log(stack, tmp_path):
    cfg = _sqlite_config(stack, tmp_path)
    cfg.logs.mkdir()
    (cfg.logs / "engine.log").write_text("the last thing the engine said\n", encoding="utf-8")
    (cfg.logs / "app.log").write_text("not this one\n", encoding="utf-8")
    engine, watch = _App("engine", cfg.logs / "engine.log"), stack.Watch(every=1, fails=2, grace=0)
    watch.restart(0)
    ended = [stack.watch_child(cfg, engine, watch, "http://127.0.0.1:1", t, ask=lambda url: None) for t in (1, 2)]
    assert ended == [False, True] and engine.ended == 1
    (kept,) = cfg.logs.glob("db-snapshot-*.txt")
    said = kept.read_text(encoding="utf-8")
    assert "the engine did not answer /health 2 times in a row" in said
    assert "== the end of engine.log ==" in said and "the last thing the engine said" in said
    assert "not this one" not in said


def _sqlite_config(stack, tmp_path, **environ):
    file = (tmp_path / "db" / "app.db").as_posix()
    return config(stack, tmp_path, f"JOBSEARCH_DATABASE_URL=sqlite+aiosqlite:///{file}", **environ)


def test_a_sqlite_url_names_its_file_and_a_postgres_url_names_none(stack, tmp_path):
    lite = _sqlite_config(stack, tmp_path)
    assert not lite.uses_postgres and lite.db_file == tmp_path / "db" / "app.db"
    pg = config(stack, tmp_path, "JOBSEARCH_DATABASE_URL=postgresql+asyncpg://a:b@127.0.0.1:1/c")
    assert pg.uses_postgres and pg.db_file is None


def test_no_postgres_is_run_or_asked_with_a_sqlite_url(stack, monkeypatch, tmp_path):
    def never(*args, **kwargs):
        raise AssertionError("pg.py was run")

    monkeypatch.setattr(stack.subprocess, "run", never)
    lite = _sqlite_config(stack, tmp_path)
    assert [stack.postgres(lite, action) for action in ("start", "stop", "status")] == [0, 0, 0]


def test_a_sqlite_start_does_not_demand_the_database_file_to_exist(stack, tmp_path):
    lite = _sqlite_config(stack, tmp_path, RESUMEBOT_VENV=str(tmp_path / "venv"))
    assert not lite.db_file.exists()
    assert "database" not in " ".join(stack.problems(lite, hold=True)).lower().replace("jobsearch_database_url", "")


def test_no_database_address_is_never_taken_for_postgres_and_points_to_init(stack, monkeypatch, tmp_path):
    def never(*args, **kwargs):
        raise AssertionError("pg.py was run")

    monkeypatch.setattr(stack.subprocess, "run", never)
    bare = config(stack, tmp_path, RESUMEBOT_VENV=str(tmp_path / "venv"))
    assert not bare.uses_postgres and bare.db_file is None
    assert [stack.postgres(bare, action) for action in ("start", "stop", "status")] == [0, 0, 0]
    said = " ".join(stack.problems(bare, hold=True))
    assert "resumebot init" in said and "pg.py" not in said


def test_both_spellings_of_a_postgres_address_still_mean_postgres(stack, tmp_path):
    for url in ("postgresql+asyncpg://a:b@127.0.0.1:1/c", "postgresql://a:b@127.0.0.1:1/c", "postgres://a@h/c"):
        assert config(stack, tmp_path, f"JOBSEARCH_DATABASE_URL={url}").uses_postgres


def test_the_default_data_folder_is_the_users_own_the_one_init_makes(stack):
    from app import setup_cli

    assert stack.default_data_folder() == setup_cli.default_data_folder()
    assert "openintellect" not in str(stack.default_data_folder()).lower()


def test_pg_py_without_a_named_folder_uses_the_same_users_own_folder_as_the_launcher(stack, monkeypatch):
    spec = importlib.util.spec_from_file_location("resumebot_pg_under_test", STACK.with_name("pg.py"))
    pg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = pg
    spec.loader.exec_module(pg)
    monkeypatch.delenv("RESUMEBOT_DATA_DIR", raising=False)

    assert pg.data_dir() == stack.default_data_folder()
    assert "openintellect" not in str(pg.data_dir()).lower()
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", "named")
    assert pg.data_dir() == Path("named")


def test_the_secret_files_setup_makes_are_found_by_the_settings_in_a_folder_init_made(stack, monkeypatch, tmp_path):
    from app import setup_cli
    from app.settings import JobSearchSettings

    assert set(stack.SECRET_FILES) <= set(setup_cli.SECRET_FILES)
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", str(tmp_path))
    settings = JobSearchSettings(_env_file=None)
    found = {Path(settings.agent_token_file), Path(settings.owner_token_file), Path(settings.wake_secret_file),
             Path(settings.browser_token_file)}
    assert found == {tmp_path / "secrets" / name for name in stack.SECRET_FILES}


def test_setup_builds_the_page_unless_told_not_to(stack, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(stack, "setup", lambda cfg, dev, page: seen.append(page))
    assert stack.main(["setup"]) == 0 and stack.main(["setup", "--no-page"]) == 0
    assert seen == [True, False]


def test_what_setup_says_next_is_resumebot_init_until_the_folder_has_a_database_address(stack, tmp_path):
    fresh = config(stack, tmp_path)
    said = stack.next_step(fresh)
    assert "resumebot init" in said and "by hand" not in said and "pg.py" not in said
    ready = _sqlite_config(stack, tmp_path)
    assert "resumebot init" not in stack.next_step(ready) and "stack.py start" in stack.next_step(ready)


def test_the_snapshot_of_a_sqlite_database_is_its_file_and_wal_sizes(stack, monkeypatch, tmp_path):
    def never(*args, **kwargs):
        raise AssertionError("pg.py was run")

    monkeypatch.setattr(stack.subprocess, "run", never)
    lite = _sqlite_config(stack, tmp_path)
    lite.logs.mkdir()
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "app.db").write_bytes(b"x" * 5)
    (tmp_path / "db" / "app.db-wal").write_bytes(b"y" * 7)
    kept = stack.db_snapshot(lite, "a test")
    assert kept is not None
    said = kept.read_text(encoding="utf-8")
    assert "app.db: 5 bytes" in said and "app.db-wal: 7 bytes" in said
    (tmp_path / "db" / "app.db-wal").unlink()
    assert "app.db-wal: not there" in stack.sqlite_sizes(lite)


def _ready(stack, tmp_path, **environ):
    cfg = config(stack, tmp_path, "JOBSEARCH_DATABASE_URL=sqlite+aiosqlite:///x.db",
                 RESUMEBOT_VENV=str(tmp_path / "venv"), **environ)
    cfg.secrets.mkdir()
    for name in ("agent-token", "wake-secret"):
        (cfg.secrets / name).write_text("x", encoding="utf-8")
    cfg.python.parent.mkdir(parents=True)
    cfg.python.write_text("", encoding="utf-8")
    return cfg


def test_the_engine_listens_on_the_pages_port_plus_the_notice_offset(stack, tmp_path):
    from app import notice

    assert stack.ENGINE_OFFSET == notice.OFFSET
    page = config(stack, tmp_path)
    assert (page.engine_port, page.engine_url) == (8530, "http://127.0.0.1:8530")
    moved = config(stack, tmp_path, "RESUMEBOT_APP_URL=http://127.0.0.1:9000\n")
    assert moved.engine_port == 9100


def test_the_lanes_are_sent_to_the_engine_and_the_browser_window_to_the_page(stack, tmp_path):
    cfg = config(stack, tmp_path, "", RESUMEBOT_APP_URL="http://127.0.0.1:8430")
    env = cfg.lanes_environment()
    assert env["RESUMEBOT_APP_URL"] == "http://127.0.0.1:8530"
    assert env["RESUMEBOT_PAGE_URL"] == "http://127.0.0.1:8430"
    bare = config(stack, tmp_path).lanes_environment()
    assert (bare["RESUMEBOT_APP_URL"], bare["RESUMEBOT_PAGE_URL"]) == ("http://127.0.0.1:8530", "http://127.0.0.1:8430")


def test_each_program_is_started_with_its_own_module_port_and_log(stack, tmp_path):
    cfg = config(stack, tmp_path)
    engine = stack.program_command(cfg, "engine", "app.jobsearch.engine:app", cfg.engine_port)
    assert engine[2:] == ["serve", str(cfg.logs / "engine.log"), "app.jobsearch.engine:app", "--host", "127.0.0.1",
                          "--port", "8530", "--no-server-header"]


def test_a_start_needs_the_engines_port_free(stack, monkeypatch, tmp_path):
    cfg = _ready(stack, tmp_path)
    monkeypatch.setattr(stack, "port_is_free", lambda port, udp: udp or port != 8530)
    said = " ".join(stack.problems(cfg, hold=True))
    assert "8530" in said and "is taken" in said
    monkeypatch.setattr(stack, "port_is_free", lambda port, udp: True)
    assert stack.problems(cfg, hold=True) == []


def test_a_port_whose_listener_has_just_closed_is_free_and_one_still_listened_on_is_not(stack):
    # A start right after a stop: the page's port still has its last connections winding down. Off Windows a plain
    # test bind of it fails for a while though nothing holds it, and the start was refused.
    import socket

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    caller = socket.create_connection(("127.0.0.1", port))
    served, _ = listener.accept()
    try:
        assert stack.real_port_is_free(port, False) is False  # something listens on it
    finally:
        served.close()  # the server's side closes first: that is the side whose port is left waiting
        listener.close()
        caller.close()
    assert stack.real_port_is_free(port, False) is True


def test_a_running_instance_is_not_told_its_own_engine_port_is_taken(stack, monkeypatch, tmp_path):
    cfg = _ready(stack, tmp_path)
    monkeypatch.setattr(stack, "port_is_free", lambda port, udp: False)
    cfg.run.mkdir(parents=True)
    stack.PidFile(cfg.run, "supervisor").write(os.getpid())
    assert stack.problems(cfg, hold=True) == []


def test_an_engine_port_past_the_last_one_is_said_plainly(stack, tmp_path):
    cfg = _ready(stack, tmp_path, RESUMEBOT_APP_URL="http://127.0.0.1:65500")
    assert any("past 65535" in line for line in stack.problems(cfg, hold=True))


class _FakeChild:
    log: list = []
    made: dict = {}

    def __init__(self, name, command, cwd, env, log, pids):
        self.name, self.command, self.env, self.pids = name, command, env, pids
        self.proc = None  # until it is started
        self.started, self.delay, self.up = 0.0, 5.0, False
        _FakeChild.made[name] = self

    def start(self):
        self.up = True
        self.proc = type("Proc", (), {"pid": 1, "returncode": 0})()
        _FakeChild.log.append(("start", self.name))
        if self.name == "lanes":  # the last child of a start: ask the supervisor to stop
            (self.pids / "stop").write_text("stop", encoding="utf-8")

    def alive(self):
        return self.up

    def finish(self, grace):
        self.up = False
        _FakeChild.log.append(("finish", self.name, grace))
        return True


def _supervised(stack, monkeypatch, tmp_path, *, hold=False, engine_up=True):
    cfg = _sqlite_config(stack, tmp_path)
    _FakeChild.log, _FakeChild.made = [], {}
    seen: dict = {}

    turns = {"n": 0}

    def waited(*args, **kwargs):
        raise AssertionError("supervise waited for a program with wait_for_health")

    def asked(url):  # the supervisor's looks at /health
        return {"status": "ok"} if engine_up else None

    def sleeping(seconds):
        turns["n"] += 1
        if turns["n"] >= 3 and (hold or not engine_up):  # no lanes will ask for the stop
            (cfg.run / "stop").write_text("stop", encoding="utf-8")

    monkeypatch.setattr(stack, "Child", _FakeChild)
    monkeypatch.setattr(stack, "wait_for_health", waited)
    monkeypatch.setattr(stack, "health", asked)
    monkeypatch.setattr(stack.time, "sleep", sleeping)
    monkeypatch.setattr(stack, "say", lambda text: None)
    monkeypatch.setattr(stack.signal, "signal", lambda *args: None)
    monkeypatch.setattr(stack.shutil, "which", lambda name: "node")
    seen["code"] = stack.supervise(cfg, hold)
    return cfg, _FakeChild.log, _FakeChild.made, seen


def test_two_programs_start_together_the_lanes_wait_for_the_engine_and_stop_in_reverse(stack, monkeypatch, tmp_path):
    cfg, log, made, seen = _supervised(stack, monkeypatch, tmp_path)
    assert log == [("start", "page"), ("start", "engine"), ("start", "lanes"),
                   ("finish", "lanes", 210), ("finish", "engine", 60), ("finish", "page", 60)]
    assert seen["code"] == 0
    assert "app.jobsearch.engine:app" in made["engine"].command and "8530" in made["engine"].command
    assert "app.jobsearch.page:app" in made["page"].command and "8430" in made["page"].command
    assert made["lanes"].env["RESUMEBOT_PAGE_URL"] == cfg.app_url


def test_held_two_programs_start_no_lanes_and_only_the_engine_has_its_schedule_off(stack, monkeypatch, tmp_path):
    _, log, made, _ = _supervised(stack, monkeypatch, tmp_path, hold=True)
    assert [entry for entry in log if entry[1] == "lanes"] == []
    assert made["engine"].env["JOBSEARCH_RUN_SCHEDULE"] == "false"
    assert "JOBSEARCH_RUN_SCHEDULE" not in made["page"].env


def test_an_engine_that_never_answers_leaves_the_page_up_starts_no_lanes_and_does_not_end_the_stack(
        stack, monkeypatch, tmp_path):
    _, log, _, seen = _supervised(stack, monkeypatch, tmp_path, engine_up=False)
    assert seen["code"] == 0, "it ends cleanly when the stop is asked"
    assert log == [("start", "page"), ("start", "engine"), ("finish", "engine", 60), ("finish", "page", 60)]


def test_the_supervisor_is_started_with_no_flag_for_the_programs(stack):
    assert stack.supervisor_command(False)[-1] == "supervise"
    assert stack.supervisor_command(True)[-2:] == ["supervise", "--hold"]


def test_the_supervisor_runs_on_the_console_python_when_the_launcher_is_pythonw(stack, monkeypatch, tmp_path):
    # Under the logon task (pythonw.exe) the supervisor had no console, so each child opened a visible one.
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(stack, "WINDOWS", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert stack.supervisor_command(False)[0] == str(tmp_path / "python.exe")


def test_the_supervisor_keeps_the_python_it_was_started_with_when_that_is_not_pythonw(stack, monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(stack, "WINDOWS", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert stack.supervisor_command(False)[0] == str(tmp_path / "python.exe")


def test_the_supervisor_keeps_pythonw_when_no_python_exe_sits_beside_it(stack, monkeypatch, tmp_path):
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(stack, "WINDOWS", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert stack.supervisor_command(False)[0] == str(tmp_path / "pythonw.exe")


def test_start_and_run_take_no_split_flag_and_reach_start_and_supervise(stack, monkeypatch, tmp_path, capsys):
    calls: list = []
    monkeypatch.setattr(stack, "start", lambda cfg, hold, wait: calls.append(("start", hold, wait)) or 0)
    monkeypatch.setattr(stack, "supervise", lambda cfg, hold: calls.append(("supervise", hold)) or 0)
    for words in (["start", "--hold"], ["start"], ["supervise"], ["run"]):
        assert stack.main(["--data", str(tmp_path), *words]) == 0
    assert calls == [("start", True, 300.0), ("start", False, 300.0), ("supervise", False), ("supervise", False)]
    with pytest.raises(SystemExit):
        stack.main(["--data", str(tmp_path), "start", "--split"])
    capsys.readouterr()


def test_the_supervisor_gets_the_plain_environment_not_the_lanes(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path)
    monkeypatch.setattr(stack, "problems", lambda *args: [])
    monkeypatch.setattr(stack, "health", lambda url: None)
    monkeypatch.setattr(stack, "say", lambda text: None)
    monkeypatch.setattr(stack, "wait_for_health", lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(stack, "status", lambda config: 0)
    started: dict = {}

    class Proc:
        pid, returncode = 1, None

        def poll(self):
            return None

    def popen(command, **kwargs):
        started.update(command=command, env=kwargs["env"])
        return Proc()

    monkeypatch.setattr(stack.subprocess, "Popen", popen)
    assert stack.start(cfg, hold=True, wait=0) == 0
    assert started["command"][-2:] == ["supervise", "--hold"]
    assert "RESUMEBOT_PAGE_URL" not in started["env"], "only the lanes get the engine's address and the page's"
    assert started["env"].get("RESUMEBOT_APP_URL") != cfg.engine_url


def test_a_stop_ends_what_an_earlier_supervisor_left_in_order_and_clears_the_files(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path)
    cfg.run.mkdir(parents=True)
    for name in ("app", "engine", "page", "lanes"):
        stack.PidFile(cfg.run, name).write(os.getpid())
    said: list[str] = []
    ended: list[int] = []
    monkeypatch.setattr(stack, "say", said.append)
    monkeypatch.setattr(stack, "kill_tree", ended.append)
    assert stack.stop(cfg, wait=0) == 0
    assert [line.split()[2] for line in said if line.startswith("ending the ")] == ["lanes", "engine", "page", "app"]
    assert ended == [os.getpid()] * 4
    assert list(cfg.run.glob("*.pid")) == []


def test_status_shows_the_page_the_engine_and_its_phase_when_two_programs_run(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path)
    cfg.run.mkdir(parents=True)
    said: list[str] = []
    monkeypatch.setattr(stack, "say", said.append)
    monkeypatch.setattr(stack, "lane_health", lambda port: False)
    answers ={cfg.app_url: {"status": "ok", "db": "ok", "version": "1"}}
    monkeypatch.setattr(stack, "health", lambda url: answers.get(url))
    assert stack.status(cfg) == 0
    assert any(line.startswith("page: ") and " ok" in line for line in said)
    assert any(line.startswith("engine: nothing answers") for line in said)
    stack.PidFile(cfg.run, "engine").write(os.getpid())  # its process runs, nothing answers: it is migrating
    said.clear()
    stack.status(cfg)
    assert any(line.startswith("engine: starting") for line in said)
    answers[cfg.engine_url] = {"status": "degraded", "db": "unreachable", "version": "1"}
    said.clear()
    assert stack.status(cfg) == 0, "the exit status follows the page"
    assert any(line.startswith("engine: ") and "degraded" in line for line in said)
    del answers[cfg.app_url]
    assert stack.status(cfg) == 1


def test_a_start_refuses_when_something_already_answers_at_the_engines_address(
    stack, monkeypatch, tmp_path
):
    cfg = _sqlite_config(stack, tmp_path)
    monkeypatch.setattr(stack, "problems", lambda *args: [])
    monkeypatch.setattr(stack, "health", lambda url: {"status": "ok"} if url == cfg.engine_url else None)
    with pytest.raises(stack.Stop, match="8530"):
        stack.start(cfg, hold=True, wait=0)


# --- each child on its own turn -------------------------------------------------------------------------------------

PAGE, ENGINE = "http://127.0.0.1:1/page", "http://127.0.0.1:1/engine"


class _Kid:
    """A made-up child: up from its start until it is ended or dies; it counts its starts and ends. The clock is the
    test's own (`world["now"]`)."""

    def __init__(self, name: str, world: dict, log: Path | None = None) -> None:
        self.name, self.world, self.log = name, world, log
        self.proc = None
        self.started, self.delay = 0.0, 5.0
        self.up, self.starts, self.finishes = False, 0, 0

    def start(self) -> None:
        self.starts += 1
        self.proc = SimpleNamespace(pid=100 + self.starts, returncode=None)
        self.started, self.up = self.world["now"], True

    def alive(self) -> bool:
        return self.up

    def die(self) -> None:
        self.up = False
        self.proc.returncode = 1

    def finish(self, grace: float) -> bool:
        self.finishes += 1
        self.up = False
        self.proc.returncode = -1
        return True


def _look(stack, cfg, world, keepers, t, ask) -> None:
    world["now"] = t
    stack.tend(cfg, keepers, t, ask)


def _all_well(url):
    return {"status": "ok"}


def test_a_crashed_engine_is_started_again_alone_and_the_page_is_left_alone(stack, tmp_path):
    cfg, world = _sqlite_config(stack, tmp_path), {"now": 0.0}
    page, engine = _Kid("page", world), _Kid("engine", world)
    keepers = [stack.Keeper(page), stack.Keeper(engine)]
    _look(stack, cfg, world, keepers, 0, _all_well)
    assert (page.starts, engine.starts) == (1, 1)
    engine.die()
    _look(stack, cfg, world, keepers, 10, _all_well)
    _look(stack, cfg, world, keepers, 14, _all_well)
    assert engine.starts == 1, "it waits its five seconds"
    _look(stack, cfg, world, keepers, 15, _all_well)
    assert (page.starts, page.finishes, engine.starts, engine.delay) == (1, 0, 2, 10.0)
    _look(stack, cfg, world, keepers, 80, _all_well)
    assert engine.delay == 5.0, "a minute up sets the wait to five seconds again"
    assert (page.starts, page.finishes) == (1, 0)


def test_a_wedged_page_is_ended_and_started_again_and_the_engine_is_left_alone(stack, tmp_path):
    cfg, world = _sqlite_config(stack, tmp_path), {"now": 0.0}
    page, engine = _Kid("page", world, cfg.logs / "page.log"), _Kid("engine", world, cfg.logs / "engine.log")
    keepers = [stack.Keeper(page, PAGE, stack.Watch(every=1, fails=2, grace=0)),
               stack.Keeper(engine, ENGINE, stack.Watch(every=1, fails=2, grace=0))]
    asked: list[str] = []

    def ask(url):
        asked.append(url)
        return None if url == PAGE else {"status": "ok"}

    for t in (0, 1, 2):
        _look(stack, cfg, world, keepers, t, ask)
    assert (page.finishes, engine.finishes) == (1, 0)
    assert len(list(cfg.logs.glob("db-snapshot-*.txt"))) == 1
    for t in (3, 7):
        _look(stack, cfg, world, keepers, t, ask)
    assert page.starts == 1, "it waits its own five seconds"
    _look(stack, cfg, world, keepers, 8, ask)
    assert (page.starts, engine.starts, engine.finishes) == (2, 1, 0)
    assert ENGINE in asked and asked.count(PAGE) == 2


def test_a_child_waiting_to_start_again_does_not_hold_up_another(stack, tmp_path):
    cfg, world = _sqlite_config(stack, tmp_path), {"now": 0.0}
    page, engine = _Kid("page", world), _Kid("engine", world)
    keepers = [stack.Keeper(page), stack.Keeper(engine)]
    _look(stack, cfg, world, keepers, 0, _all_well)
    engine.delay = 100.0
    engine.die()
    _look(stack, cfg, world, keepers, 10, _all_well)
    page.die()
    _look(stack, cfg, world, keepers, 11, _all_well)
    _look(stack, cfg, world, keepers, 16, _all_well)
    assert (page.starts, engine.starts) == (2, 1), "the page is back while the engine still waits"
    _look(stack, cfg, world, keepers, 109, _all_well)
    assert engine.starts == 1
    _look(stack, cfg, world, keepers, 110, _all_well)
    assert engine.starts == 2


def test_an_engine_that_never_answers_is_ended_and_started_again_and_nothing_else_is(stack, tmp_path):
    cfg, world = _sqlite_config(stack, tmp_path), {"now": 0.0}
    page, engine = _Kid("page", world), _Kid("engine", world)
    keepers = [stack.Keeper(page, PAGE, stack.Watch(every=1, fails=2, grace=3)),
               stack.Keeper(engine, ENGINE, stack.Watch(every=1, fails=2, grace=3))]

    def ask(url):
        return None if url == ENGINE else {"status": "ok"}

    for t in (0, 1, 2, 3):
        _look(stack, cfg, world, keepers, t, ask)
    assert engine.finishes == 0, "within its grace, and the first miss is counted at three seconds"
    _look(stack, cfg, world, keepers, 4, ask)
    assert (engine.finishes, page.finishes) == (1, 0)
    for t in (5, 9):
        _look(stack, cfg, world, keepers, t, ask)
    assert engine.starts == 1
    _look(stack, cfg, world, keepers, 10, ask)
    assert (engine.starts, page.starts) == (2, 1)


def test_the_lanes_start_once_the_engine_answers_and_only_once(stack, tmp_path):
    cfg, world = _sqlite_config(stack, tmp_path), {"now": 0.0}
    engine, lanes = _Kid("engine", world), _Kid("lanes", world)
    keepers = [stack.Keeper(engine, ENGINE), stack.Keeper(lanes, wait_url=ENGINE)]
    answering = {"yes": False}

    def ask(url):
        return {"status": "ok"} if answering["yes"] and url == ENGINE else None

    for t in (0, 1):
        _look(stack, cfg, world, keepers, t, ask)
    assert (engine.starts, lanes.starts, lanes.proc) == (1, 0, None)
    answering["yes"] = True
    for t in (2, 3, 4):
        _look(stack, cfg, world, keepers, t, ask)
    assert lanes.starts == 1


# --- the whole supervisor, with made-up children ---------------------------------------------------------------------

_MADE: list = []
_ENDED: list = []


class _SuperKid:
    """A made-up Child for a whole run of `supervise`: nothing is started; it records."""

    def __init__(self, name, *args, **kwargs) -> None:
        self.name, self.log, self.proc, self.up = name, None, None, False
        self.started, self.delay = 0.0, 5.0
        _MADE.append(self)

    def start(self) -> None:
        self.proc = SimpleNamespace(pid=7, returncode=None)
        self.up, self.started = True, time.monotonic()

    def alive(self) -> bool:
        return self.up

    def finish(self, grace: float) -> bool:
        if self.proc is None:
            return True
        self.up = False
        _ENDED.append(self.name)
        return True


def _whole_run(stack, monkeypatch, tmp_path, engine_answers_after: int):
    """Run `supervise` for twelve looks and a stop request. The page always answers; the engine answers /health after
    that many questions to it. Returns the exit code, the made-up children by name and the engine's question count."""
    cfg = _sqlite_config(stack, tmp_path)
    _MADE.clear()
    _ENDED.clear()
    engine_asks = {"n": 0}
    sleeps = {"n": 0}

    def health(url):
        if url == cfg.app_url:
            return {"status": "ok"}
        engine_asks["n"] += 1
        return {"status": "ok"} if engine_asks["n"] > engine_answers_after else None

    def sleep(seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= 12:
            (cfg.run / "stop").write_text("stop", encoding="utf-8")

    def waited(*args, **kwargs):
        raise AssertionError("supervise waited for a program with wait_for_health")

    monkeypatch.setattr(stack, "Child", _SuperKid)
    monkeypatch.setattr(stack, "health", health)
    monkeypatch.setattr(stack, "wait_for_health", waited)
    monkeypatch.setattr(stack.signal, "signal", lambda *a: None)
    monkeypatch.setattr(stack.time, "sleep", sleep)
    code = stack.supervise(cfg, False)
    return code, {kid.name: kid for kid in _MADE}, engine_asks


def test_an_engine_that_never_answers_does_not_end_the_stack_or_start_the_lanes(stack, monkeypatch, tmp_path):
    code, kids, asks = _whole_run(stack, monkeypatch, tmp_path, engine_answers_after=10**6)
    assert code == 0 and asks["n"] >= 10, "it kept looking until it was asked to stop"
    assert kids["page"].proc is not None and kids["engine"].proc is not None
    assert kids["lanes"].proc is None, "the lanes wait for the engine"
    assert _ENDED == ["engine", "page"], "the engine is ended before the page"


def test_the_lanes_start_when_the_engine_first_answers_and_stop_first(stack, monkeypatch, tmp_path):
    code, kids, _ = _whole_run(stack, monkeypatch, tmp_path, engine_answers_after=3)
    assert code == 0 and kids["lanes"].proc is not None
    assert _ENDED == ["lanes", "engine", "page"]


def test_supervise_takes_only_the_config_and_the_hold_and_there_is_no_app_command(stack):
    import inspect

    assert list(inspect.signature(stack.supervise).parameters) == ["config", "hold"]
    assert not hasattr(stack, "app_command")


def test_a_stop_with_nothing_running_is_quiet_and_safe_to_repeat_on_an_empty_data_folder(stack, monkeypatch, tmp_path):
    """The update stops the app before it replaces the code, whatever state the install is in, with the new code and an
    old install's data folder: no run folder at all, then only a stale file."""
    cfg = _sqlite_config(stack, tmp_path)
    ended: list[int] = []
    monkeypatch.setattr(stack, "say", lambda text: None)
    monkeypatch.setattr(stack, "kill_tree", ended.append)
    assert stack.stop(cfg, wait=0) == 0
    cfg.run.mkdir(parents=True, exist_ok=True)
    (cfg.run / "supervisor.pid").write_text("999999999", encoding="utf-8")  # an old install's file for a dead process
    assert stack.stop(cfg, wait=0) == 0
    assert stack.stop(cfg, wait=0) == 0
    assert ended == [] and list(cfg.run.glob("*.pid")) == []


# --- no console window when the shortcut starts the launcher (pythonw.exe has no console) ---------------------

NO_WINDOW = 0x08000000


def _record_windows_calls(stack, monkeypatch):
    """Pretend Windows; every subprocess.run and Popen the launcher makes is recorded and nothing real starts."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(("run", list(command), kwargs))
        out = ""
        if command[0] == "netstat":
            out = "  TCP    127.0.0.1:8430    0.0.0.0:0    LISTENING    4242\n"
        elif command[0] == "tasklist":
            out = '"x.exe","4242","Console","1","1 K"\n'
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    class FakePopen:
        def __init__(self, command, **kwargs):
            calls.append(("popen", list(command), kwargs))
            self.pid, self.returncode = 1, None

        def poll(self):
            return None

    monkeypatch.setattr(stack, "WINDOWS", True)
    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    monkeypatch.setattr(stack.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(stack.subprocess, "CREATE_NO_WINDOW", NO_WINDOW, raising=False)
    return calls


def test_the_checks_before_a_start_open_no_window_on_windows(stack, monkeypatch, tmp_path):
    cfg = _ready(stack, tmp_path)
    monkeypatch.undo()  # the file's autouse fakes: here the port is taken and the real holder looks at it
    calls = _record_windows_calls(stack, monkeypatch)
    monkeypatch.setattr(stack, "port_is_free", lambda port, udp: False)
    assert any("x.exe" in line for line in stack.problems(cfg, hold=True))
    stack.kill_tree(99)
    assert {call[1][0] for call in calls} >= {"netstat", "tasklist"} and len(calls) >= 3
    assert all(call[2]["creationflags"] & NO_WINDOW for call in calls), calls


@pytest.mark.skipif(sys.platform != "win32", reason="the hidden console is Windows' own (STARTUPINFO)")
def test_a_start_hides_the_supervisor_with_its_own_console_and_runs_nothing_else_visibly(
        stack, monkeypatch, tmp_path):
    cfg = _ready(stack, tmp_path)
    calls = _record_windows_calls(stack, monkeypatch)
    monkeypatch.setattr(stack, "problems", lambda config, hold: [])
    monkeypatch.setattr(stack, "health", lambda url: None)
    monkeypatch.setattr(stack, "wait_for_health", lambda url, seconds, alive=None: {"ok": True})
    monkeypatch.setattr(stack, "status", lambda config: 0)
    monkeypatch.setattr(stack, "open_log", lambda path: os.open(os.devnull, os.O_WRONLY))
    assert stack.start(cfg, hold=True, wait=1) == 0
    (kind, _, kwargs), = [call for call in calls if call[0] == "popen"]
    assert kwargs["creationflags"] & stack.NEW_CONSOLE == stack.NEW_CONSOLE or stack.NEW_CONSOLE == 0
    assert not kwargs["creationflags"] & NO_WINDOW, "the supervisor's children share the console it makes"
    assert kwargs["startupinfo"].wShowWindow == stack.subprocess.SW_HIDE
    assert all(call[2]["creationflags"] & NO_WINDOW for call in calls if call[0] == "run")


def test_no_window_is_no_flag_away_from_windows(stack, monkeypatch):
    monkeypatch.setattr(stack, "WINDOWS", False)
    assert stack.no_window() == 0


# --- hardening of start and setup (2.5.6): why a start stopped, half-made pieces, one quiet way to run a program ---


def test_a_command_that_stops_leaves_the_reason_and_the_time_in_start_log(stack, monkeypatch, tmp_path, capsys):
    """Under pythonw nothing is printed anywhere, so start.log is the only place the sentence is."""
    monkeypatch.setattr(stack, "wall_clock", lambda: stack.dt.datetime(2026, 11, 16, 9, 30, tzinfo=stack.dt.UTC))
    fresh = tmp_path / "fresh"  # no logs folder yet: it is made
    assert stack.main(["--data", str(fresh), "start", "--wait", "0"]) == 2
    said = (fresh / "logs" / "start.log").read_text(encoding="utf-8")
    assert said.startswith("2026-11-16T09:30:00+00:00 start: not starting:") and "stack.py setup" in said
    assert "not starting" in capsys.readouterr().err


def test_an_error_that_is_not_a_stop_is_in_start_log_too(stack, monkeypatch, tmp_path):
    def boom(config):
        raise RuntimeError("the made-up failure")

    monkeypatch.setattr(stack, "status", boom)
    assert stack.main(["--data", str(tmp_path), "status"]) == 1
    said = (tmp_path / "logs" / "start.log").read_text(encoding="utf-8")
    assert "status:" in said and "RuntimeError: the made-up failure" in said


def test_a_supervisor_that_stops_by_a_stop_writes_start_log(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path)
    monkeypatch.setattr(stack, "postgres", lambda *a: 1)
    monkeypatch.setattr(stack.Config, "uses_postgres", property(lambda self: True))
    monkeypatch.setattr(stack.signal, "signal", lambda *args: None)
    assert stack.supervise(cfg, hold=True) == 1
    assert "Postgres did not start" in (cfg.logs / "start.log").read_text(encoding="utf-8")


def test_an_env_file_that_is_not_utf8_is_one_sentence_and_a_bom_is_fine(stack, tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("\ufeffA=1\n".encode("utf-8"))
    assert stack.read_env(env) == {"A": "1"}
    env.write_bytes("A=1\n".encode("utf-16"))  # what Windows PowerShell's > writes
    with pytest.raises(stack.Stop, match="not saved as UTF-8"):
        stack.read_env(env)
    env.write_bytes(b"A=\xff\xfe\x81\n")
    with pytest.raises(stack.Stop, match="not saved as UTF-8"):
        stack.Config(tmp_path, {})
    assert stack.main(["--data", str(tmp_path), "status"]) == 2


def test_the_sqlite_rule_here_is_the_one_in_setup_cli(stack):
    from app import setup_cli

    for major in (3,):
        for minor in range(40, 56):
            for patch in range(0, 10):
                version = (major, minor, patch)
                assert stack.sqlite_is_safe(version) == setup_cli.sqlite_is_safe(version), version
    assert stack.sqlite_is_safe((3, 51, 3)) and not stack.sqlite_is_safe((3, 51, 2))


def _probe(monkeypatch, stack, *, out="3.51.3\n", err="", code=0, raises=None):
    def fake(command, **kwargs):
        if raises:
            raise raises
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    monkeypatch.setattr(stack, "quiet_run", fake)


def test_the_environments_python_is_asked_for_pip_and_its_sqlite(stack, monkeypatch, tmp_path):
    cfg = _ready(stack, tmp_path)
    _probe(monkeypatch, stack)
    assert stack.real_venv_state(cfg) == ("ok", "3.51.3")
    _probe(monkeypatch, stack, out="3.50.4\n")
    assert stack.real_venv_state(cfg) == ("unsafe", "3.50.4")
    _probe(monkeypatch, stack, out="", err="Traceback\nModuleNotFoundError: No module named 'pip'\n", code=1)
    assert stack.real_venv_state(cfg) == ("broken", "ModuleNotFoundError: No module named 'pip'")
    _probe(monkeypatch, stack, raises=OSError("no"))
    assert stack.real_venv_state(cfg) == ("broken", "OSError")
    _probe(monkeypatch, stack, out="not a version\n")
    assert stack.real_venv_state(cfg)[0] == "broken"
    cfg.python.unlink()
    assert stack.real_venv_state(cfg) == ("missing", "")


def test_a_start_is_refused_for_a_broken_or_unsafe_environment_a_missing_page_and_a_missing_console_python(
        stack, monkeypatch, tmp_path):
    cfg = _ready(stack, tmp_path)
    monkeypatch.setattr(stack, "venv_state", lambda c: ("broken", "OSError"))
    assert "does not run" in " ".join(stack.problems(cfg, hold=True))
    monkeypatch.setattr(stack, "venv_state", lambda c: ("unsafe", "3.50.4"))
    said = " ".join(stack.problems(cfg, hold=True))
    assert "3.50.4" in said and "WAL-reset" in said and "uv python install 3.12 --upgrade" in said
    monkeypatch.setattr(stack, "venv_state", lambda c: ("ok", "3.51.3"))
    assert stack.problems(cfg, hold=True) == []
    monkeypatch.setattr(stack, "PAGE_INDEX", tmp_path / "no-page" / "index.html")
    assert "the page is not built" in " ".join(stack.problems(cfg, hold=True))
    # Windows, with a pythonw.exe that has no python.exe beside it
    monkeypatch.setattr(stack, "WINDOWS", True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "pythonw.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(stack.sys, "executable", str(tmp_path / "scripts" / "pythonw.exe"))
    monkeypatch.setattr(stack, "venv_state", lambda c: ("ok", "3.51.3"))
    monkeypatch.setattr(stack, "PAGE_INDEX", tmp_path / "scripts" / "pythonw.exe")
    assert "beside pythonw.exe" in " ".join(stack.problems(cfg, hold=True))
    (tmp_path / "scripts" / "python.exe").write_text("", encoding="utf-8")
    assert not any("pythonw" in line for line in stack.problems(cfg, hold=True))


def _setup_recorder(stack, monkeypatch, states):
    """`setup` with every step made up: the venv states it will see, in order, and what it runs, recorded."""
    ran = []
    seen = iter(states)
    monkeypatch.setattr(stack, "venv_state", lambda c: next(seen))
    monkeypatch.setattr(stack, "run_step", lambda command, cwd=None, label="", env=None: ran.append(label or command))
    monkeypatch.setattr(stack, "only_me", lambda path: None)
    monkeypatch.setattr(stack, "site_packages", lambda c: c.data / "site-packages")
    monkeypatch.setattr(stack, "say", lambda text: None)
    monkeypatch.setattr(stack, "taken_ports", lambda *a, **k: pytest.fail("setup ran a port preflight"))
    return ran


def test_setup_makes_a_half_made_environment_again_and_never_asks_about_ports(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path, RESUMEBOT_VENV=str(tmp_path / "venv"))
    cfg.python.parent.mkdir(parents=True)
    cfg.python.write_text("", encoding="utf-8")  # python.exe, and no pip
    (cfg.venv / "pyvenv.cfg").write_text("home = x", encoding="utf-8")
    ran = _setup_recorder(stack, monkeypatch, [("broken", "No module named pip"), ("ok", "3.51.3")])
    stack.setup(cfg, dev=False, page=False)
    assert not cfg.python.exists(), "the half-made environment was removed whole"
    assert ran[0] == "making the app's Python environment" and "installing the app's packages" in ran
    assert not (cfg.run / "setup.pid").exists(), "the lock is let go"


def test_setup_does_not_remove_a_broken_python_folder_it_did_not_make(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path, RESUMEBOT_VENV=str(tmp_path / "venv"))
    cfg.python.parent.mkdir(parents=True)
    cfg.python.write_text("", encoding="utf-8")
    _setup_recorder(stack, monkeypatch, [("broken", "OSError")])
    with pytest.raises(stack.Stop, match="remove it yourself"):
        stack.setup(cfg, dev=False, page=False)
    assert cfg.python.exists()


def test_setup_stops_with_the_remedy_when_the_environments_sqlite_has_the_wal_bug(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path, RESUMEBOT_VENV=str(tmp_path / "venv"))
    ran = _setup_recorder(stack, monkeypatch, [("missing", ""), ("unsafe", "3.45.1")])
    with pytest.raises(stack.Stop, match=r"3\.45\.1.*WAL-reset.*uv python install 3.12 --upgrade"):
        stack.setup(cfg, dev=False, page=False)
    assert ran == ["making the app's Python environment"], "no package was installed into it"


def test_setup_refuses_while_the_app_runs_or_another_setup_does(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path)
    ran = _setup_recorder(stack, monkeypatch, [])
    cfg.run.mkdir(parents=True)
    stack.PidFile(cfg.run, "supervisor").write(os.getpid())
    with pytest.raises(stack.Stop, match="stack.py stop"):
        stack.setup(cfg, dev=False, page=False)
    (cfg.run / "supervisor.pid").unlink()
    stack.PidFile(cfg.run, "setup").write(os.getpid())  # a setup that is running: this very process stands for it
    monkeypatch.setattr(os, "getpid", lambda: os.getppid())  # so the one that asks is another process
    with pytest.raises(stack.Stop, match="another setup is running") as refused:
        stack.setup(cfg, dev=False, page=False)
    assert refused.value.code == 10 and ran == []


def test_a_pid_file_can_be_taken_by_only_one_live_process_and_a_dead_ones_is_taken_over(stack, tmp_path):
    lock = stack.PidFile(tmp_path, "lock")
    assert lock.acquire() is None
    assert lock.live() == os.getpid()
    lock.path.write_text(json.dumps({"pid": 999999999, "mark": 5}), encoding="utf-8")  # a process long gone
    assert lock.acquire() is None and lock.live() == os.getpid()
    parent = os.getppid()
    lock.path.write_text(json.dumps({"pid": parent, "mark": stack.process_mark(parent)}), encoding="utf-8")
    assert lock.acquire() == parent, "a live holder keeps it"


def test_a_second_supervisor_does_not_start_beside_a_live_one(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path)
    cfg.run.mkdir(parents=True)
    parent = os.getppid()
    stack.PidFile(cfg.run, "supervisor").path.write_text(
        json.dumps({"pid": parent, "mark": stack.process_mark(parent)}), encoding="utf-8")
    with pytest.raises(stack.Stop, match=f"already running .pid {parent}"):
        stack.supervise(cfg, hold=True)


def test_the_supervisor_takes_its_pid_file_before_anything_else(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path)
    seen = {}

    def rolled(old, *a):
        seen["pid file first"] = stack.PidFile(cfg.run, "supervisor").live() == os.getpid()

    cfg.logs.mkdir(parents=True)
    (cfg.logs / "x.log").write_text("", encoding="utf-8")
    monkeypatch.setattr(stack, "rotate", rolled)
    monkeypatch.setattr(stack, "Child", _FakeChild)
    _FakeChild.log, _FakeChild.made = [], {}
    monkeypatch.setattr(stack.signal, "signal", lambda *args: None)
    monkeypatch.setattr(stack, "health", lambda url: {"status": "ok"})
    monkeypatch.setattr(stack.time, "sleep", lambda s: (cfg.run / "stop").write_text("stop", encoding="utf-8"))
    stack.supervise(cfg, hold=True)
    assert seen["pid file first"] is True


def test_a_start_whose_supervisor_never_appears_ends_it_and_says_so_within_the_limit(stack, monkeypatch, tmp_path):
    cfg = _ready(stack, tmp_path)
    ended = []
    clock = {"now": 100.0}

    class Alive:
        pid, returncode = 4321, None

        def poll(self):
            return None

    monkeypatch.setattr(stack, "problems", lambda c, h: [])
    monkeypatch.setattr(stack, "health", lambda url: None)
    monkeypatch.setattr(stack.subprocess, "Popen", lambda *a, **k: Alive())
    monkeypatch.setattr(stack, "open_log", lambda path: os.open(os.devnull, os.O_WRONLY))
    monkeypatch.setattr(stack, "kill_tree", ended.append)
    monkeypatch.setattr(stack, "say", lambda text: None)
    monkeypatch.setattr(stack.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(stack.time, "sleep", lambda s: clock.update(now=clock["now"] + 10))
    (cfg.logs).mkdir(parents=True, exist_ok=True)
    (cfg.logs / "start.log").write_text("2026 start: the made-up reason\n", encoding="utf-8")
    with pytest.raises(stack.Stop, match="did not appear within 30s") as stopped:
        stack.start(cfg, hold=True, wait=300)
    assert ended == [4321] and "the made-up reason" in str(stopped.value)
    assert clock["now"] < 100 + 60, "it gave up long before the 300 seconds it was told to wait"


def test_a_supervisor_that_died_is_explained_from_both_logs(stack, monkeypatch, tmp_path):
    cfg = _ready(stack, tmp_path)

    class Dead:
        pid, returncode = 4321, 3

        def poll(self):
            return 3

    monkeypatch.setattr(stack, "problems", lambda c, h: [])
    monkeypatch.setattr(stack, "health", lambda url: None)
    monkeypatch.setattr(stack.subprocess, "Popen", lambda *a, **k: Dead())
    monkeypatch.setattr(stack, "say", lambda text: None)
    cfg.logs.mkdir(parents=True, exist_ok=True)
    (cfg.logs / "start.log").write_text("2026 supervise: Postgres did not start\n", encoding="utf-8")
    with pytest.raises(stack.Stop, match=r"it stopped \(3\)") as stopped:
        stack.start(cfg, hold=True, wait=300)
    assert "start.log" in str(stopped.value) and "Postgres did not start" in str(stopped.value)


def test_a_stop_waits_at_least_as_long_as_the_lanes_grace_and_two_minutes(stack):
    import inspect

    assert stack.STOP_WAIT >= stack.LANES_GRACE + 120
    assert inspect.signature(stack.stop).parameters["wait"].default == stack.STOP_WAIT


def test_a_step_that_fails_names_itself_and_says_it_is_safe_to_repeat(stack, monkeypatch):
    monkeypatch.setattr(stack.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=7))
    with pytest.raises(stack.Stop) as failed:
        stack.run_step(["pip", "install"], label="installing the app's packages")
    text = str(failed.value)
    assert "installing the app's packages failed (7)" in text and "safe to repeat" in text and "antivirus" in text

    def cannot(*a, **k):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(stack.subprocess, "run", cannot)
    with pytest.raises(stack.Stop, match="could not start .*safe to repeat"):
        stack.run_step(["nothing"], label="a step")


def test_a_step_runs_in_a_clean_environment_unless_given_one(stack, monkeypatch):
    seen = []
    monkeypatch.setattr(stack.subprocess, "run",
                        lambda command, **kw: seen.append(kw["env"]) or SimpleNamespace(returncode=0))
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_INDEX_URL", "NODE_ENV"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("KEPT", "yes")
    stack.run_step(["a"])
    stack.run_step(["b"], env={"ONLY": "this"})
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_INDEX_URL", "NODE_ENV"} & set(seen[0])
    assert seen[0]["KEPT"] == "yes" and seen[1] == {"ONLY": "this"}


def test_node_is_found_by_the_registrys_path_and_its_installers_folders_when_the_shells_path_lacks_it(
        stack, monkeypatch, tmp_path):
    folder = tmp_path / "registry-node"
    folder.mkdir()
    (folder / "node.exe").write_text("", encoding="utf-8")
    other = tmp_path / "Program Files" / "nodejs"
    other.mkdir(parents=True)
    (other / "npm.cmd").write_text("", encoding="utf-8")
    monkeypatch.setattr(stack, "WINDOWS", True)
    monkeypatch.setattr(stack.shutil, "which", lambda name, path=None: _which(name, path))
    monkeypatch.setattr(stack, "registry_paths", lambda: [str(folder)])
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    assert Path(stack.find_program("node")).name == "node.exe"
    assert Path(stack.find_program("npm")).name == "npm.cmd", "the folder Node's installer uses"
    assert stack.find_program("nothing-here") is None


def _which(name, path=None):
    if path is None:
        return None  # the process's own PATH has neither
    for folder in path.split(os.pathsep):
        for ext in ("", ".exe", ".cmd"):
            candidate = Path(folder) / (name + ext)
            if candidate.is_file():
                return str(candidate)
    return None


def test_the_lanes_get_the_folder_of_a_node_found_off_the_path_and_the_python_that_wraps_their_sessions(
        stack, monkeypatch, tmp_path):
    monkeypatch.setattr(stack, "find_program", lambda name: str(tmp_path / "nodejs" / "node.exe"))
    cfg = _ready(stack, tmp_path, PATH=os.pathsep.join(["C:/somewhere"]))
    env = cfg.lanes_environment()
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "nodejs")
    assert env["SCOUT_JOBWRAP_PYTHON"] == str(cfg.python)
    (tmp_path / "b").mkdir()
    named = _ready(stack, tmp_path / "b", SCOUT_JOBWRAP_PYTHON="C:/mine/python.exe")
    assert named.lanes_environment()["SCOUT_JOBWRAP_PYTHON"] == "C:/mine/python.exe"
    folder = str(tmp_path / "nodejs")
    again = stack.with_path_first({"Path": folder + os.pathsep + "x"}, [folder])
    assert again == {"Path": folder + os.pathsep + "x"}, "a folder already on the PATH is not added twice"


@pytest.mark.parametrize("version,expected", [
    ((18, 20), False), ((20, 18), False), ((20, 19), True), ((21, 7), False), ((22, 11), False), ((22, 12), True),
    ((24, 0), True)])
def test_the_page_builds_only_on_a_node_its_tools_support(stack, version, expected):
    assert stack.node_is_new_enough(version) is expected


def test_an_old_node_is_a_stop_with_the_winget_line_before_any_install(stack, monkeypatch):
    monkeypatch.setattr(stack, "find_program", lambda name: f"C:/n/{name}")
    monkeypatch.setattr(stack, "quiet_run", lambda command, **kw: SimpleNamespace(returncode=0, stdout="v18.20.4\n"))
    monkeypatch.setattr(stack, "run_step", lambda *a, **k: pytest.fail("a step ran on an old Node"))
    with pytest.raises(stack.Stop, match=r"v18\.20\.4.*winget install OpenJS.NodeJS.LTS --source winget"):
        stack.build_page()


def test_the_page_is_built_to_a_temp_folder_and_swapped_in_only_when_it_worked(stack, tmp_path):
    page = tmp_path / "page"
    page.mkdir()
    (page / "index.html").write_text("old", encoding="utf-8")
    built = tmp_path / "built"
    built.mkdir()
    with pytest.raises(stack.Stop, match="made no page"):
        stack.swap_page(built, page)
    assert (page / "index.html").read_text(encoding="utf-8") == "old", "a failed build leaves the old page"
    (built / "index.html").write_text("new", encoding="utf-8")
    (built / "assets").mkdir()
    stack.swap_page(built, page)
    assert (page / "index.html").read_text(encoding="utf-8") == "new" and (page / "assets").is_dir()
    assert not built.exists() and not (tmp_path / "page.old").exists()


def test_the_frontends_temp_build_writes_where_the_launcher_swaps_from(stack):
    package = Path(__file__).resolve().parents[3] / "frontend" / "package.json"
    if not package.is_file():
        pytest.skip("the frontend's package.json is not in this image")
    script = json.loads(package.read_text(encoding="utf-8"))["scripts"]["build:package-temp"]
    assert "--emptyOutDir" in script
    assert stack.PAGE_BUILD == package.parent / script.split("--outDir ")[1].split()[0]


def test_a_program_that_cannot_start_is_tried_again_after_its_own_wait_and_the_others_go_on(stack, tmp_path):
    cfg, world = _sqlite_config(stack, tmp_path), {"now": 0.0}

    class Locked(_Kid):
        refusing = True

        def start(self):
            if self.refusing:
                self.tries = getattr(self, "tries", 0) + 1
                raise PermissionError(13, "Access is denied")
            super().start()

    page, engine = Locked("page", world), _Kid("engine", world)
    keepers = [stack.Keeper(page, PAGE), stack.Keeper(engine, ENGINE)]
    for t in (0, 1, 2, 3):
        _look(stack, cfg, world, keepers, t, _all_well)
    assert page.tries == 1 and page.proc is None, "one try, then its own wait of five seconds"
    assert engine.starts == 1, "the engine started in spite of it"
    _look(stack, cfg, world, keepers, 6, _all_well)
    assert page.tries == 2, "tried again once the wait was over"
    Locked.refusing = False
    _look(stack, cfg, world, keepers, 17, _all_well)
    assert page.starts == 1 and page.proc is not None


def test_a_restart_that_cannot_start_keeps_the_supervisor_and_tries_again(stack, tmp_path):
    cfg, world = _sqlite_config(stack, tmp_path), {"now": 0.0}

    class Fragile(_Kid):
        broken = False

        def start(self):
            if self.broken and self.starts:
                raise OSError(5, "Access is denied")
            super().start()

    kid = Fragile("engine", world)
    keepers = [stack.Keeper(kid, ENGINE)]
    _look(stack, cfg, world, keepers, 0, _all_well)
    kid.die()
    Fragile.broken = True
    for t in (1, 2, 7, 8, 20):
        _look(stack, cfg, world, keepers, t, _all_well)  # none of these raises
    assert kid.starts == 1
    Fragile.broken = False
    _look(stack, cfg, world, keepers, 60, _all_well)
    assert kid.starts == 2


def test_a_listener_is_found_by_its_empty_foreign_address_in_any_language(stack, monkeypatch):
    monkeypatch.setattr(stack, "WINDOWS", True)
    shown = ("  Proto  Lokale Adresse  Remoteadresse  Status  PID\n"
             "  TCP    127.0.0.1:8430    127.0.0.1:50000    HERGESTELLT    1111\n"
             "  TCP    127.0.0.1:8430    0.0.0.0:0    ABHÖREN    4242\n")

    def fake(command, **kwargs):
        out = shown if command[0] == "netstat" else '"x.exe","4242","Console","1","1 K"\n'
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(stack, "quiet_run", fake)
    assert stack.real_holder(8430, udp=False) == "x.exe (pid 4242)"
    shown = "  TCP    [::]:8430    [::]:0    ESCUCHANDO    77\n"
    assert stack.real_holder(8430, udp=False) == "x.exe (pid 77)"
    shown = "  UDP    127.0.0.1:8430    *:*    99\n"
    assert stack.real_holder(8430, udp=True) == "x.exe (pid 99)"


def test_the_quiet_runner_hides_the_window_limits_the_time_and_reads_utf8_with_replacement(stack, monkeypatch):
    seen = {}

    def fake(command, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stack, "WINDOWS", True)
    monkeypatch.setattr(stack.subprocess, "run", fake)
    monkeypatch.setattr(stack.subprocess, "CREATE_NO_WINDOW", NO_WINDOW, raising=False)
    stack.quiet_run(["x"], timeout=5)
    assert seen["creationflags"] & NO_WINDOW and seen["timeout"] == 5
    assert (seen["encoding"], seen["errors"], seen["stdin"]) == ("utf-8", "replace", stack.subprocess.DEVNULL)


def test_no_other_subprocess_call_is_made_but_the_steps_the_supervisor_and_its_children(stack):
    """Every program the launcher runs goes through `quiet_run`, which hides its window and bounds its time; the ones
    that keep the user's own console, or stay running, are listed here and nowhere else."""
    import ast

    allowed = {"quiet_run", "run_step", "start"}  # start: the supervisor's launch; Child.start: the programs
    tree = ast.parse(STACK.read_text(encoding="utf-8"))
    found = []

    def walk(node, inside):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and isinstance(
                    child.func.value, ast.Name) and child.func.value.id == "subprocess" and child.func.attr in (
                    "run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
                found.append((inside, child.func.attr))
            walk(child, name or inside)

    walk(tree, "")
    assert found, "the scan found nothing: it is broken"
    assert {where for where, _ in found} <= allowed, found


def test_smoke_fails_a_door_whose_token_file_is_missing_instead_of_skipping_it(stack, monkeypatch, tmp_path, capsys):
    cfg = _sqlite_config(stack, tmp_path)
    monkeypatch.setattr(stack, "health", lambda url: {"status": "ok", "db": "ok",
                                                      "version": stack.version_of_checkout()})
    monkeypatch.setattr(stack, "wait_for_health", lambda *a, **k: {"status": "ok", "db": "ok",
                                                                    "version": stack.version_of_checkout()})
    monkeypatch.setattr(stack, "fetch", lambda *a, **k: (200, {}, b"PK"))
    monkeypatch.setattr(stack, "live_events", lambda *a, **k: ["hello", "resync"])
    code = stack.smoke(cfg, lanes=False)
    out = capsys.readouterr().out
    assert code == 1 and "skip" not in out
    assert "FAIL /mcp/ answers a call with its token" in out and "no token file" in out


def test_the_lanes_are_asked_again_for_a_minute_before_one_is_called_down(stack, monkeypatch, tmp_path):
    cfg = _sqlite_config(stack, tmp_path)
    clock = {"now": 0.0, "asks": 0}

    def lane(port):
        clock["asks"] += 1
        return clock["now"] >= 20  # a cold start: they answer after twenty seconds

    monkeypatch.setattr(stack, "lane_health", lane)
    monkeypatch.setattr(stack.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(stack.time, "sleep", lambda s: clock.update(now=clock["now"] + s))
    assert stack.lanes_answer(cfg) == [True] * 5 and clock["now"] == 20
    clock.update(now=0.0, asks=0)
    monkeypatch.setattr(stack, "lane_health", lambda port: False)
    assert stack.lanes_answer(cfg) == [False] * 5 and clock["now"] >= 60, "given up only after the whole minute"
