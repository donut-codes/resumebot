"""`resumebot project [folder]` (app/project_cli.py): writes the three helper agents into a project folder, only there,
and never touches a file that is already there. No database; every folder is a temp folder."""

import os
from pathlib import Path

import pytest

from app import main_cli, project_cli

NAMES = ["career-coach.md", "master-resume-help.md", "profile-draft.md"]


@pytest.fixture
def places(tmp_path, monkeypatch):
    """A made-up clone, a made-up data folder and a project folder, all under tmp_path."""
    code, data, project = tmp_path / "code", tmp_path / "data", tmp_path / "project"
    for folder in (code, data, project):
        folder.mkdir()
    monkeypatch.setattr(project_cli, "CODE_ROOT", code)
    monkeypatch.setenv("RESUMEBOT_DATA_DIR", str(data))
    return code, data, project


def written(project: Path) -> list[str]:
    return sorted(p.name for p in (project / ".claude" / "agents").glob("*"))


def test_it_writes_the_three_agent_files_under_the_projects_claude_agents(places, capsys):
    _, _, project = places
    assert main_cli.main(["project", str(project)]) == 0
    assert written(project) == NAMES
    assert "career-coach.md: written" in capsys.readouterr().out


def test_a_second_run_changes_no_byte_and_says_each_is_already_there(places, capsys):
    _, _, project = places
    main_cli.main(["project", str(project)])
    before = {n: (project / ".claude" / "agents" / n).read_bytes() for n in NAMES}
    capsys.readouterr()
    assert main_cli.main(["project", str(project)]) == 0
    assert {n: (project / ".claude" / "agents" / n).read_bytes() for n in NAMES} == before
    assert capsys.readouterr().out.count("already there") == 3


def test_an_edited_agent_file_is_left_byte_identical_and_the_run_says_it_kept_it(places, capsys):
    _, _, project = places
    main_cli.main(["project", str(project)])
    mine = project / ".claude" / "agents" / "career-coach.md"
    mine.write_bytes(b"my own words\n")
    capsys.readouterr()
    assert main_cli.main(["project", str(project)]) == 0
    assert mine.read_bytes() == b"my own words\n"
    assert "kept your career-coach.md" in capsys.readouterr().out


def test_an_edited_file_does_not_stop_the_other_two_from_being_written(places):
    _, _, project = places
    agents = project / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "career-coach.md").write_bytes(b"mine\n")
    assert main_cli.main(["project", str(project)]) == 0
    assert written(project) == NAMES
    assert (agents / "career-coach.md").read_bytes() == b"mine\n"


def test_a_copy_with_windows_line_ends_counts_as_already_there_not_as_edited(places, capsys):
    _, _, project = places
    main_cli.main(["project", str(project)])
    mine = project / ".claude" / "agents" / "master-resume-help.md"
    mine.write_bytes(mine.read_bytes().replace(b"\n", b"\r\n"))
    capsys.readouterr()
    assert main_cli.main(["project", str(project)]) == 0
    assert "master-resume-help.md: already there" in capsys.readouterr().out


def test_a_folder_that_does_not_exist_is_refused_and_nothing_is_written(places, tmp_path, capsys):
    gone = tmp_path / "nope"
    assert main_cli.main(["project", str(gone)]) == 1
    assert not gone.exists()
    assert "does not exist" in capsys.readouterr().err


def test_a_file_is_not_a_folder(places, tmp_path, capsys):
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    assert main_cli.main(["project", str(plain)]) == 1
    assert "is not a folder" in capsys.readouterr().err


@pytest.mark.parametrize("which, inner", [("code", False), ("code", True), ("data", False), ("data", True)])
def test_the_clone_the_data_folder_and_anything_inside_them_are_refused(places, which, inner, capsys):
    code, data, _ = places
    folder = code if which == "code" else data
    if inner:
        folder = folder / "inner"
        folder.mkdir()
    assert main_cli.main(["project", str(folder)]) == 1
    assert not (folder / ".claude").exists()
    assert "your own project folder" in capsys.readouterr().err


def test_a_claude_agents_link_that_leaves_the_folder_is_refused_and_nothing_is_written(places, tmp_path, capsys):
    _, _, project = places
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (project / ".claude").mkdir()
    try:
        os.symlink(elsewhere, project / ".claude" / "agents", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot make a link without extra rights")
    assert main_cli.main(["project", str(project)]) == 1
    assert list(elsewhere.iterdir()) == []
    assert "leads outside" in capsys.readouterr().err


def test_nothing_outside_claude_agents_is_created(places):
    _, _, project = places
    (project / "mine.txt").write_text("keep", encoding="utf-8")
    before = {p.relative_to(project) for p in project.rglob("*")}
    main_cli.main(["project", str(project)])
    after = {p.relative_to(project) for p in project.rglob("*")}
    added = after - before
    assert all(p.parts[:2] == (".claude", "agents") or p == Path(".claude") for p in added)


def test_no_folder_argument_means_the_current_directory(places, monkeypatch):
    _, _, project = places
    monkeypatch.chdir(project)
    assert main_cli.main(["project"]) == 0
    assert written(project) == NAMES


def test_an_option_is_a_usage_error(places, capsys):
    assert main_cli.main(["project", "--force"]) == 2
    assert "usage: resumebot project" in capsys.readouterr().err
