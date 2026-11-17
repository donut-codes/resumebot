"""`resumebot start`, `smoke`, the data folder variable `init` sets (app/setup_cli.py).
Nothing here runs Task Scheduler, a real process, a window, `claude` or the registry: each is injected."""

import subprocess
from pathlib import Path

import pytest

from app import main_cli, setup_cli

OWNER = "Sam Example"
ZONE = "Asia/Tokyo"


@pytest.fixture(autouse=True)
def _no_real_file_limits(monkeypatch):
    monkeypatch.setattr(setup_cli, "_only_me_file", lambda path: None)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.delenv(setup_cli.DATA_VARIABLE, raising=False)
    monkeypatch.setattr(setup_cli, "documents_folder", lambda: tmp_path / "Docs")
    monkeypatch.setattr(setup_cli, "startup_folder", lambda: tmp_path / "Startup")
    monkeypatch.setattr(setup_cli, "_set_user_variable", lambda name, value: pytest.fail("the real registry"))


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    (path / ".env").write_text("JOBSEARCH_OWNER=Test\n", encoding="utf-8")
    return path


class Clock:
    """An injected clock: sleeping moves it, nothing waits."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(setup_cli, "_clock", fake)
    monkeypatch.setattr(setup_cli, "_sleep", fake.sleep)
    return fake


class Shell:
    """Stands in for `_run` (PowerShell and cmd). `task` is what Get-ScheduledTask reports; `refuse` lists words, and a
    call holding one of them fails."""

    def __init__(self, task=None, refuse=()):
        self.calls, self.task, self.refuse = [], task, refuse

    def __call__(self, command):
        self.calls.append(list(command))
        text = " ".join(command)
        if "Get-ScheduledTask" in text:
            return subprocess.CompletedProcess(command, 0, "" if self.task is None else f"TASK {self.task}\n", "")
        if any(word in text for word in self.refuse):
            return subprocess.CompletedProcess(command, 1, "", "Access is denied")
        return subprocess.CompletedProcess(command, 0, "", "")

    def scripts(self, word):
        return [call[-1] for call in self.calls if word in " ".join(call)]


class Launches:
    """Stands in for `_launch`: what would have been started outside this process."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, command, **kwargs):
        if self.fail:
            raise self.fail
        self.calls.append((list(command), kwargs))


def answers_after(monkeypatch, polls):
    """`app_health` says "not running" for the first `polls` asks, then healthy."""
    asked = []

    def health(install):
        asked.append(1)
        return None if len(asked) > polls else "the app does not answer"

    monkeypatch.setattr(setup_cli, "app_health", health)
    return asked


# --- start ----------------------------------------------------------------------------------------------------------


def test_start_when_the_app_already_answers_says_so_and_starts_nothing(folder, monkeypatch, capsys, clock):
    shell, launches = Shell(), Launches()
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    monkeypatch.setattr(setup_cli, "_run", shell)
    monkeypatch.setattr(setup_cli, "_launch", launches)
    answers_after(monkeypatch, 0)
    assert setup_cli.start(["--data", str(folder)]) == 0
    assert capsys.readouterr().out == f"already running at {setup_cli.DEFAULT_APP_URL}\n"
    assert shell.calls == [] and launches.calls == [] and clock.slept == []


def test_start_before_init_refuses_and_starts_nothing(tmp_path, monkeypatch, capsys):
    launches = Launches()
    monkeypatch.setattr(setup_cli, "_launch", launches)
    assert setup_cli.start(["--data", str(tmp_path / "nothing")]) == 1
    assert "run `resumebot init` first" in capsys.readouterr().err
    assert launches.calls == []


def test_start_on_windows_runs_a_one_off_task_with_no_logon_trigger_and_waits_for_the_app(
        folder, monkeypatch, capsys, clock):
    shell, launches = Shell(), Launches()
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_run", shell)
    monkeypatch.setattr(setup_cli, "_launch", launches)
    asked = answers_after(monkeypatch, 3)
    assert setup_cli.start(["--data", str(folder)]) == 0
    script, = shell.scripts("Register-ScheduledTask")
    assert "-AtLogOn" not in script and "-Trigger" not in script
    assert "-RunLevel" not in script and "Highest" not in script  # no administrator
    assert f"-TaskName '{setup_cli.ONE_OFF_TASK}'" in script
    assert "stack.py" in script and str(folder) in script and script.count("start'") >= 1
    assert shell.scripts("Start-ScheduledTask")
    assert launches.calls == []  # Task Scheduler started it, not this process
    assert len(asked) == 4 and clock.slept  # the first ask, then each wait, until it answered
    assert capsys.readouterr().out.splitlines()[-1] == f"running at {setup_cli.DEFAULT_APP_URL}"


def test_start_on_windows_no_longer_cleans_up_the_old_logon_task_that_is_the_shortcuts_job(
        folder, monkeypatch, capsys, clock):
    # C3.4: `start` and `open` run on every use; the old autostart is removed once, by `shortcut`.
    shell, launches = Shell(task=f'"stack.py" --data "{folder}" start'), Launches()
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_run", shell)
    monkeypatch.setattr(setup_cli, "_launch", launches)
    answers_after(monkeypatch, 1)
    assert setup_cli.start(["--data", str(folder)]) == 0
    assert shell.scripts("Unregister-ScheduledTask") == [] and shell.scripts("Get-ScheduledTask") == []
    script, = shell.scripts("Register-ScheduledTask")  # always its own one-off
    assert f"-TaskName '{setup_cli.ONE_OFF_TASK}'" in script
    assert "removed the old start at sign-in" not in capsys.readouterr().out
    assert launches.calls == []


def test_start_on_windows_leaves_another_installs_autostart_task_alone(folder, monkeypatch, clock):
    shell = Shell(task='"stack.py" --data "C:\\elsewhere" start')
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_run", shell)
    monkeypatch.setattr(setup_cli, "_launch", Launches())
    answers_after(monkeypatch, 1)
    assert setup_cli.start(["--data", str(folder)]) == 0
    script, = shell.scripts("Register-ScheduledTask")
    assert f"-TaskName '{setup_cli.ONE_OFF_TASK}'" in script
    changes = shell.scripts("Register-ScheduledTask") + shell.scripts("Start-ScheduledTask")
    assert all(f"-TaskName '{setup_cli.TASK_NAME}'" not in call for call in changes)  # the lookup alone names it
    assert shell.scripts("Unregister-ScheduledTask") == []


def test_start_on_windows_falls_back_to_a_detached_process_when_task_scheduler_refuses(
        folder, tmp_path, monkeypatch, capsys, clock):
    shell, launches = Shell(refuse=("Register-ScheduledTask",)), Launches()
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_run", shell)
    monkeypatch.setattr(setup_cli, "_launch", launches)
    answers_after(monkeypatch, 1)
    assert setup_cli.start(["--data", str(folder)]) == 0
    (command, kwargs), = launches.calls
    assert command[0].lower().endswith(("python.exe", "pythonw.exe", Path(setup_cli.sys.executable).name.lower()))
    assert command[1].endswith("stack.py") and command[-3:] == ["--data", str(folder), "start"]
    assert kwargs["creationflags"] & getattr(subprocess, "DETACHED_PROCESS", 0x8)
    assert not (tmp_path / "Startup").exists()  # the fallback leaves no Startup file
    assert capsys.readouterr().out.splitlines()[-1] == f"running at {setup_cli.DEFAULT_APP_URL}"


def test_start_elsewhere_starts_a_detached_process_in_a_new_session(folder, monkeypatch, capsys, clock):
    launches, shell = Launches(), Shell()
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    monkeypatch.setattr(setup_cli, "_run", shell)
    monkeypatch.setattr(setup_cli, "_launch", launches)
    answers_after(monkeypatch, 1)
    assert setup_cli.start(["--data", str(folder)]) == 0
    (command, kwargs), = launches.calls
    assert command[0] == setup_cli.sys.executable
    assert command[1].endswith("stack.py") and command[-3:] == ["--data", str(folder), "start"]
    assert kwargs["start_new_session"] is True
    assert shell.calls == []


def test_start_says_what_failed_when_nothing_answers_in_time(folder, monkeypatch, capsys, clock):
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    monkeypatch.setattr(setup_cli, "_launch", Launches())
    answers_after(monkeypatch, 10**9)
    assert setup_cli.start(["--data", str(folder)]) == 1
    error = capsys.readouterr().err
    assert "did not answer" in error and str(folder / "logs" / "supervisor.log") in error
    assert clock.now >= setup_cli.START_WAIT_SECONDS


def test_start_says_so_when_the_process_could_not_be_launched(folder, monkeypatch, capsys, clock):
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    monkeypatch.setattr(setup_cli, "_launch", Launches(fail=OSError("no such file")))
    answers_after(monkeypatch, 10**9)
    assert setup_cli.start(["--data", str(folder)]) == 1
    assert "could not start the app" in capsys.readouterr().err and clock.slept == []


# --- smoke ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv, extra", [([], []), (["--lanes"], ["--lanes"])])
def test_smoke_runs_stack_smoke_for_this_install_and_passes_its_exit_code_through(folder, monkeypatch, argv, extra):
    seen = []
    monkeypatch.setattr(setup_cli, "_call", lambda command: seen.append(list(command)) or 7)
    assert setup_cli.smoke(["--data", str(folder), *argv]) == 7
    (command,) = seen
    assert command[0] == setup_cli.sys.executable and command[1].endswith("stack.py")
    assert command[2:] == ["--data", str(folder), "smoke", *extra]


def test_smoke_and_start_are_in_the_command_table():
    assert main_cli.COMMANDS["start"] is setup_cli.start
    assert main_cli.COMMANDS["smoke"] is setup_cli.smoke


# --- init sets the data folder variable -----------------------------------------------------------------------------


def run_init(folder):
    return setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)])


def test_init_on_windows_sets_the_data_folder_variable_for_this_user_and_says_so_in_one_line(
        tmp_path, monkeypatch, capsys):
    folder, set_to = tmp_path / "fresh", []
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_set_user_variable", lambda name, value: set_to.append((name, value)))
    assert run_init(folder) == 0
    assert set_to == [(setup_cli.DATA_VARIABLE, str(folder))]
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1].startswith(f"set {setup_cli.DATA_VARIABLE} to {folder} for this user")
    assert not any(line.startswith("optional:") for line in lines)


def test_init_on_windows_leaves_a_variable_that_names_another_folder_and_says_both(tmp_path, monkeypatch, capsys):
    """M14, on purpose: the old behaviour repointed the variable at any `--data`, which moved another install's
    commands onto this folder; now only an unset variable is set."""
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setenv(setup_cli.DATA_VARIABLE, str(tmp_path / "other"))
    monkeypatch.setattr(setup_cli, "_set_user_variable", lambda name, value: pytest.fail("repointed"))
    assert run_init(tmp_path / "fresh") == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "other") in out and str(tmp_path / "fresh") in out and "left alone" in out


def test_init_on_windows_leaves_a_variable_that_already_names_this_folder(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "fresh"
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setenv(setup_cli.DATA_VARIABLE, str(folder))
    assert run_init(folder) == 0  # the autouse stub fails the test when it is called
    assert "for this user" not in capsys.readouterr().out


def test_init_on_windows_prints_the_line_to_add_when_the_variable_could_not_be_set(tmp_path, monkeypatch, capsys):
    def refuse(name, value):
        raise OSError("registry locked")

    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_set_user_variable", refuse)
    assert run_init(tmp_path / "fresh") == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith(f"optional: set {setup_cli.DATA_VARIABLE} to ")


def test_init_elsewhere_still_prints_the_line_to_add(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    assert run_init(tmp_path / "fresh") == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith(f"optional: set {setup_cli.DATA_VARIABLE} to ")


# --- open -----------------------------------------------------------------------------------------------------------


class Browser:
    """Stands in for `webbrowser.open`: what would have been opened (a real browser never starts)."""

    def __init__(self, opens=True):
        self.urls, self.opens = [], opens

    def __call__(self, url):
        self.urls.append(url)
        return self.opens


def test_open_starts_the_app_when_nothing_answers_then_opens_the_page(folder, monkeypatch, capsys, clock):
    shell, launches, browser = Shell(), Launches(), Browser()
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_run", shell)
    monkeypatch.setattr(setup_cli, "_launch", launches)
    monkeypatch.setattr(setup_cli.webbrowser, "open", browser)
    answers_after(monkeypatch, 2)
    assert setup_cli.open_page(["--data", str(folder)]) == 0
    script, = shell.scripts("Register-ScheduledTask")  # started exactly as `start` does
    assert f"-TaskName '{setup_cli.ONE_OFF_TASK}'" in script and shell.scripts("Start-ScheduledTask")
    assert browser.urls == [setup_cli.DEFAULT_APP_URL]
    assert not (folder / "logs" / setup_cli.OPEN_LOG).exists()


def test_open_only_opens_the_page_when_the_app_already_answers(folder, monkeypatch, clock):
    shell, launches, browser = Shell(), Launches(), Browser()
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_run", shell)
    monkeypatch.setattr(setup_cli, "_launch", launches)
    monkeypatch.setattr(setup_cli.webbrowser, "open", browser)
    answers_after(monkeypatch, 0)
    assert setup_cli.open_page(["--data", str(folder)]) == 0
    assert browser.urls == [setup_cli.DEFAULT_APP_URL]
    assert shell.calls == [] and launches.calls == [] and clock.slept == []


def test_open_writes_the_reason_to_the_log_and_opens_nothing_when_the_app_does_not_start(
        folder, monkeypatch, capsys, clock):
    browser = Browser()
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    monkeypatch.setattr(setup_cli, "_launch", Launches())
    monkeypatch.setattr(setup_cli.webbrowser, "open", browser)
    answers_after(monkeypatch, 10**9)
    assert setup_cli.open_page(["--data", str(folder)]) == 1
    assert browser.urls == []
    assert "did not answer" in (folder / "logs" / setup_cli.OPEN_LOG).read_text(encoding="utf-8")
    assert "did not answer" in capsys.readouterr().err


def test_open_writes_a_crash_to_the_log_instead_of_hanging_silent(folder, monkeypatch, clock):
    def boom(install):
        raise RuntimeError("no network stack")

    monkeypatch.setattr(setup_cli, "app_health", boom)
    monkeypatch.setattr(setup_cli.webbrowser, "open", Browser())
    assert setup_cli.open_page(["--data", str(folder)]) == 1
    assert "no network stack" in (folder / "logs" / setup_cli.OPEN_LOG).read_text(encoding="utf-8")


def test_open_before_init_refuses_and_says_so_in_the_log(tmp_path, monkeypatch, capsys):
    browser = Browser()
    monkeypatch.setattr(setup_cli.webbrowser, "open", browser)
    nothing = tmp_path / "nothing"
    assert setup_cli.open_page(["--data", str(nothing)]) == 1
    assert browser.urls == [] and "run `resumebot init` first" in capsys.readouterr().err
    assert "run `resumebot init` first" in (nothing / "logs" / setup_cli.OPEN_LOG).read_text(encoding="utf-8")


# --- no console window when the shortcut (pythonw.exe, no console) starts the app ---------------------------


def test_open_on_windows_starts_nothing_with_a_console_window_of_its_own(folder, monkeypatch, clock):
    no_window = 0x08000000
    calls = []

    def fake_run(command, **kwargs):
        calls.append(("run", list(command), kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    def fake_popen(command, **kwargs):
        calls.append(("popen", list(command), kwargs))

    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(setup_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(setup_cli.subprocess, "CREATE_NO_WINDOW", no_window, raising=False)
    monkeypatch.setattr(setup_cli.webbrowser, "open", Browser())
    answers_after(monkeypatch, 2)
    assert setup_cli.open_page(["--data", str(folder)]) == 0
    assert any("Register-ScheduledTask" in " ".join(call[1]) for call in calls)
    assert all(call[2]["creationflags"] & no_window for call in calls), calls


def test_the_detached_fallback_and_the_shortcut_open_no_window_either(folder, monkeypatch, clock):
    no_window = 0x08000000
    calls = []

    def fake_run(command, **kwargs):
        calls.append(("run", list(command), kwargs))
        refused = "Register-ScheduledTask" in " ".join(command)
        return subprocess.CompletedProcess(command, 1 if refused else 0, "", "refused" if refused else "")

    def fake_popen(command, **kwargs):
        calls.append(("popen", list(command), kwargs))

    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(setup_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(setup_cli.subprocess, "CREATE_NO_WINDOW", no_window, raising=False)
    answers_after(monkeypatch, 1)
    assert setup_cli.start(["--data", str(folder)]) == 0
    assert any(call[0] == "popen" for call in calls)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    assert all(call[2]["creationflags"] & no_window for call in calls), calls
