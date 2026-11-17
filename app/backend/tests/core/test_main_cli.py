"""The `resumebot` command (app/main_cli.py): its usage and version, the command it has and the ones that are planned.
None of it needs a database."""

import subprocess
import sys
import tomllib

import pytest

from app import db, main_cli
from app.settings import APP_NAME, VERSION


def test_it_says_the_app_and_its_version(capsys):
    assert main_cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"{APP_NAME} {VERSION}"


def test_no_command_says_how_to_use_it_and_exits_with_a_usage_status(capsys):
    assert main_cli.main([]) == 2
    error = capsys.readouterr().err
    assert "usage: resumebot COMMAND" in error
    assert "serve" in error


def test_help_lists_each_command_with_what_it_does_and_marks_the_planned_ones(capsys):
    assert main_cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "run the app on this machine (loopback only)" in out
    for name, package in main_cli.PLANNED.items():
        assert name in out
        assert f"not built yet: {package}" in out


@pytest.mark.parametrize("name", sorted(main_cli.PLANNED))
def test_a_planned_command_says_which_package_builds_it_and_changes_nothing(name, capsys):
    assert main_cli.main([name, "--anything"]) == main_cli.EXIT_NOT_BUILT == 3
    error = capsys.readouterr().err
    assert f"resumebot {name} is not built yet" in error
    assert main_cli.PLANNED[name] in error


def test_an_unknown_command_is_a_usage_error(capsys):
    assert main_cli.main(["frobnicate"]) == 2
    assert "unknown command 'frobnicate'" in capsys.readouterr().err


def test_a_command_is_never_both_built_and_planned():
    assert not set(main_cli.COMMANDS) & set(main_cli.PLANNED)


def test_every_built_command_has_a_summary_for_the_help():
    for name, command in main_cli.COMMANDS.items():
        assert (command.__doc__ or "").strip(), name


def test_serve_listens_on_this_machine_only_on_the_apps_port_unless_told_another(monkeypatch):
    import uvicorn

    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append((app, options)))
    assert main_cli.main(["serve"]) == 0
    assert main_cli.main(["serve", "--port", "8431"]) == 0
    from app.log_setup import uvicorn_log_config

    assert started == [
        ("app.jobsearch.main:app", {"host": "127.0.0.1", "port": 8430, "log_config": uvicorn_log_config()}),
        ("app.jobsearch.main:app", {"host": "127.0.0.1", "port": 8431, "log_config": uvicorn_log_config()}),
    ]


def test_serve_refuses_a_port_that_is_not_a_number():
    with pytest.raises(SystemExit) as stopped:
        main_cli.main(["serve", "--port", "http"])
    assert stopped.value.code == 2


def test_it_runs_as_a_module_from_the_backend_folder():
    done = subprocess.run(
        [sys.executable, "-m", "app.main_cli", "--version"], cwd=db.BACKEND_DIR, capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"{APP_NAME} {VERSION}"


def test_the_package_names_it_as_the_resumebot_entry_point():
    project = tomllib.loads((db.BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"] == {"resumebot": "app.main_cli:main"}
    module, _, function = project["scripts"]["resumebot"].partition(":")
    assert module == main_cli.__name__
    assert callable(getattr(main_cli, function))
