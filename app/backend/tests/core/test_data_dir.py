"""The data folder (RESUMEBOT_DATA_DIR): the one place a PC install keeps its settings file and its secret files,
named by the environment and by nothing else. Unset (the other tests) nothing changes, and a test run
unsets it for itself: the shell of a PC with resumebot installed names the install's own folder. Everything is made
up."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import settings as s

BACKEND = Path(__file__).resolve().parents[2]
DEV = BACKEND.parent / "scripts" / "dev.py"
#: A value that only the made-up data folder's settings file holds.
MARKER = "marker-only-the-data-folder-holds"
#: A pytest start of a person's own, as far as the tests being collected (tests/conftest.py is loaded by then, and the
#: app's settings read), and then what that run holds. Its arguments: what to collect, and the marker.
A_RUN_AND_WHAT_IT_HOLDS = """
import os, sys
import pytest
collected = pytest.main(["-q", "-p", "no:cacheprovider", "--collect-only", sys.argv[1]])
from app import settings as s
print("collected:", int(collected))
print("variable:", os.environ.get(s.DATA_DIR_VARIABLE))
print("data folder:", s.data_dir())
print("settings files:", s.settings.model_config["env_file"])
print("marker in the settings:", sys.argv[2] in str(s.settings.model_dump()))
"""


def test_a_test_run_has_no_data_folder_and_no_settings_file_from_one():
    assert s.DATA_DIR_VARIABLE not in os.environ
    assert s.data_dir() is None
    assert s.settings.model_config["env_file"] == (".env",)


def test_a_run_started_with_a_data_folder_named_reads_nothing_from_it(tmp_path):
    """The owner's shell names the install's data folder for every program it starts. A pytest started from it must
    not take its settings (the install's database, owner and secret files) from that folder's `.env`: tests/conftest.py
    forgets the variable before the app's settings are read. Proved on a run of its own, since the settings are read
    once, as the app is imported."""
    lines = (f"JOBSEARCH_OWNER={MARKER}", f"JOBSEARCH_DEFAULT_FOCUS={MARKER}",
             f"JOBSEARCH_DATABASE_URL=sqlite+aiosqlite:///{(tmp_path / MARKER).as_posix()}.db")
    (tmp_path / ".env").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    # Nothing of this run's own pytest goes along (the worker's name, the options): the run is one a person starts.
    environ = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}
    environ[s.DATA_DIR_VARIABLE] = str(tmp_path)

    this_file = Path(__file__).relative_to(BACKEND).as_posix()
    done = subprocess.run(
        [sys.executable, "-c", A_RUN_AND_WHAT_IT_HOLDS, this_file, MARKER], cwd=BACKEND, env=environ,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)

    said = done.stdout + done.stderr
    assert done.returncode == 0, said
    for line in ("collected: 0", "variable: None", "data folder: None", "settings files: ('.env',)",
                 "marker in the settings: False"):
        assert line in done.stdout.splitlines(), said
    assert sorted(path.name for path in tmp_path.iterdir()) == [".env"], "and nothing was written in the folder"


@pytest.mark.skipif(not DEV.exists(), reason="the development flow is not in this image")
def test_a_check_gives_nothing_it_starts_the_data_folder(tmp_path, monkeypatch):
    """`dev.py check` starts pytest, the engine's tests, the scout's node tests, npm and its own folder's tests. None
    of them is told where the data folder is, though the shell the check was started from names one."""
    spec = importlib.util.spec_from_file_location("resumebot_dev_under_test", DEV)
    assert spec is not None and spec.loader is not None
    dev = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, dev)
    spec.loader.exec_module(dev)
    assert dev.DATA_DIR_VARIABLE == s.DATA_DIR_VARIABLE
    started: list[tuple[str, str | None]] = []

    def run(cmd, *, cwd=None, env=None, quiet=False):
        # What the process would be started with: the environment it is handed, else this process's own.
        given = os.environ if env is None else env
        started.append((" ".join(str(part) for part in cmd), given.get(s.DATA_DIR_VARIABLE)))
        return 0

    monkeypatch.setattr(dev, "run", run)
    monkeypatch.setattr(dev, "app_python", lambda tree: Path("py"))
    monkeypatch.setattr(dev, "tool", lambda name, hint: name)
    monkeypatch.setattr(dev, "ensure_frontend", lambda tree: "npm")
    monkeypatch.setattr(dev, "ensure_node_modules", lambda folder: None)
    monkeypatch.setattr(dev, "drop_build_info", lambda tree: None)
    monkeypatch.setenv(s.DATA_DIR_VARIABLE, str(tmp_path))

    assert dev.main(["check", "--backend", "--runner", "--frontend", "--scripts"]) == 0

    commands = [command for command, _ in started]
    for part in ("-m pytest", "node --test", "npm test", "-m unittest"):
        assert any(part in command for command in commands), (part, commands)
    assert [command for command, folder in started if folder is not None] == []
    assert s.DATA_DIR_VARIABLE not in os.environ, "nor anything the check starts without the stand-in (ShellCheck)"


def test_with_no_data_folder_named_the_settings_are_the_containers(monkeypatch):
    monkeypatch.delenv("RESUMEBOT_DATA_DIR", raising=False)
    assert s.data_dir() is None
    assert s._env_files() == (".env",)
    fresh = s.JobSearchSettings(_env_file=None)
    assert (fresh.agent_token_file, fresh.browser_token_file, fresh.wake_secret_file) == (
        "/secrets/agent-token", "/secrets/browser-token", "/secrets/wake-secret")


def test_a_blank_data_folder_names_nothing(monkeypatch):
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", "   ")
    assert s.data_dir() is None


def test_a_data_folder_holds_the_settings_file_and_the_secret_files(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", str(tmp_path))
    assert s.data_dir() == tmp_path
    # The working folder's .env first, the data folder's after it (a later file wins).
    assert s._env_files() == (".env", str(tmp_path / ".env"))
    fresh = s.JobSearchSettings(_env_file=None)
    assert Path(fresh.agent_token_file) == tmp_path / "secrets" / "agent-token"
    assert Path(fresh.browser_token_file) == tmp_path / "secrets" / "browser-token"
    assert Path(fresh.wake_secret_file) == tmp_path / "secrets" / "wake-secret"


def test_a_secret_file_the_settings_name_is_where_it_is_whatever_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JOBSEARCH_BROWSER_TOKEN_FILE", str(tmp_path / "elsewhere" / "token"))
    fresh = s.JobSearchSettings(_env_file=None)
    assert Path(fresh.browser_token_file) == tmp_path / "elsewhere" / "token"
    assert Path(fresh.agent_token_file) == tmp_path / "secrets" / "agent-token"


def test_the_settings_file_is_read_as_utf8_with_or_without_a_byte_order_mark(tmp_path):
    """Windows tools write one (Notepad, PowerShell's UTF-8), and a Windows program reads a file as the machine's code
    page unless told otherwise: a name with an accent in it, or the first setting of the file, would be wrong."""
    body = "JOBSEARCH_OWNER=José\r\nJOBSEARCH_TIMEZONE=America/New_York\r\n".encode()
    for name, raw in (("plain.env", body), ("marked.env", b"\xef\xbb\xbf" + body)):
        path = tmp_path / name
        path.write_bytes(raw)
        read = s.JobSearchSettings(_env_file=path)
        assert (read.owner, read.timezone) == ("José", "America/New_York"), name


def test_a_windows_path_in_the_settings_file_is_read_as_it_is_written(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("JOBSEARCH_BROWSER_TOKEN_FILE=C:\\data\\someone\\.resumebot\\browser-token\n", encoding="utf-8")
    assert s.JobSearchSettings(_env_file=path).browser_token_file == r"C:\data\someone\.resumebot\browser-token"
