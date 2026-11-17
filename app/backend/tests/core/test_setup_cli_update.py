"""The install skill's update flow (plugins/resumebot-setup/skills/install-resumebot/SKILL.md): its commands are run as
written. Each shell-neutral Python command in the skill is cut out of the skill and run on made-up folders, so what the
skill tells Claude to run is what is tested: back up the data folder, delete the code folder, set it aside, find the
newest release. No network (a local repository stands in for the release repository), no real install."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / "plugins" / "resumebot-setup" / "skills" / "install-resumebot" / "SKILL.md"
DOC = ROOT / "docs" / "install.md"

pytestmark = pytest.mark.skipif(not SKILL.exists(), reason="the setup plugin is not in this image")

BACKUP = "shutil.copytree"
DELETE = "shutil.rmtree"
ASIDE = "os.rename(c,o)"
NEWEST = "re.findall"
LOOK = "print('code folder:'"


def command(marker: str) -> str:
    """The Python code of the one `& "$PY" -c "..."` line in the skill that holds `marker`."""
    lines = [line for line in SKILL.read_text(encoding="utf-8").splitlines()
             if line.startswith('& "$PY" -c "') and marker in line]
    assert len(lines) == 1, f"{marker!r} is in {len(lines)} commands"
    found = re.fullmatch(r'& "\$PY" -c "(.*?)"((?: "\$\w+")*)', lines[0])
    assert found, lines[0]
    return found.group(1)


def run(marker: str, *args: Path | str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", command(marker), *map(str, args)], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


def old_install(tmp_path: Path) -> tuple[Path, Path]:
    code, data = tmp_path / "Programs" / "resumebot", tmp_path / "resumebot"
    (code / "app" / "frontend" / "node_modules").mkdir(parents=True)
    (code / "app" / "frontend" / "node_modules" / "left.js").write_text("x", encoding="utf-8")
    (code / "app" / "backend" / ".venv").mkdir(parents=True)
    held = code / ".git" / "objects" / "ab" / "cdef"
    held.parent.mkdir(parents=True)
    held.write_text("x", encoding="utf-8")
    held.chmod(0o444)  # git writes its objects read-only; Windows will not delete such a file as it is
    link = code / "app" / "node_modules"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(code / "app" / "frontend" / "node_modules"), str(link))
    else:
        link.symlink_to(code / "app" / "frontend" / "node_modules", target_is_directory=True)
    (data / "secrets").mkdir(parents=True)
    (data / ".env").write_text("JOBSEARCH_OWNER=Sam Example\n", encoding="utf-8")
    (data / "resumebot.db").write_bytes(b"the user's work")
    (data / "secrets" / "claude-oauth-token").write_text("a-token", encoding="utf-8")
    (data / "browser-profile").mkdir()
    (data / "browser-profile" / "Cookies").write_text("held open by Chrome", encoding="utf-8")
    return code, data


def tree(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


# --- the look ---------------------------------------------------------------------------------------------------------


def test_the_look_names_what_is_there_and_changes_nothing(tmp_path):
    code, data = old_install(tmp_path)
    before = tree(data)
    said = run(LOOK, code, data).stdout
    assert "code folder: True" in said and "settings: True" in said and "database: True" in said
    assert "agent token: True" in said
    said = run(LOOK, tmp_path / "none", tmp_path / "nothing").stdout
    assert "code folder: False" in said and "settings: False" in said and "agent token: False" in said
    assert tree(data) == before


# --- the backup -------------------------------------------------------------------------------------------------------


def test_the_backup_is_a_sibling_copy_without_the_browser_profile_and_the_data_is_untouched(tmp_path):
    _, data = old_install(tmp_path)
    before = tree(data)
    done = run(BACKUP, data)
    assert done.returncode == 0, done.stderr
    copies = [p for p in tmp_path.iterdir() if p.name.startswith("resumebot-backup-")]
    assert len(copies) == 1 and re.fullmatch(r"resumebot-backup-\d{8}-\d{4}", copies[0].name)
    assert tree(copies[0]) == {k: v for k, v in before.items() if not k.startswith("browser-profile")}
    assert tree(data) == before
    assert str(copies[0]) in done.stdout


def test_a_backup_run_again_in_the_same_minute_still_succeeds(tmp_path):
    _, data = old_install(tmp_path)
    assert run(BACKUP, data).returncode == 0
    assert run(BACKUP, data).returncode == 0


def test_there_is_nothing_to_back_up_when_the_data_folder_holds_no_settings_and_no_database(tmp_path):
    data = tmp_path / "resumebot"
    (data / "logs").mkdir(parents=True)
    done = run(BACKUP, data)
    assert done.returncode == 0 and "nothing to back up" in done.stdout
    assert [p.name for p in tmp_path.iterdir()] == ["resumebot"]
    assert run(BACKUP, tmp_path / "never-made").returncode == 0


def test_a_backup_that_fails_raises_so_the_command_exits_with_an_error_and_the_update_stops(tmp_path, monkeypatch):
    _, data = old_install(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", fail)
    monkeypatch.setattr(sys, "argv", ["-c", str(data)])
    with pytest.raises(OSError, match="disk full"):
        exec(command(BACKUP), {})
    assert tree(data)["resumebot.db"] == b"the user's work"


# --- the replacement --------------------------------------------------------------------------------------------------


def test_the_code_folder_is_deleted_whole_read_only_files_and_the_packages_link_included_and_the_data_stays(tmp_path):
    code, data = old_install(tmp_path)
    before = tree(data)
    done = run(DELETE, code, data)
    assert done.returncode == 0, done.stderr
    assert not code.exists()
    assert tree(data) == before
    assert code.parent.is_dir()


def test_the_delete_refuses_when_one_folder_is_inside_the_other_and_deletes_nothing(tmp_path):
    code, data = old_install(tmp_path)
    inside = code / "app" / "data"
    shutil.copytree(data, inside)
    before = tree(code)
    for first, second in ((code, inside), (inside, code)):
        done = run(DELETE, first, second)
        assert done.returncode != 0 and "nothing deleted" in done.stderr
    assert tree(code) == before


def test_a_code_folder_that_cannot_be_deleted_is_set_aside_beside_itself_with_the_date_and_the_data_stays(tmp_path):
    code, data = old_install(tmp_path)
    link = Path("app") / "node_modules"  # a junction names its target by path: it dangles once the folder moves
    before_code = {k: v for k, v in tree(code).items() if not k.startswith(str(link))}
    before_data = tree(data)
    done = run(ASIDE, code)
    assert done.returncode == 0, done.stderr
    aside = [p for p in code.parent.iterdir() if p.name.startswith("resumebot-old-")]
    assert len(aside) == 1 and re.fullmatch(r"resumebot-old-\d{8}-\d{4}", aside[0].name)
    assert not code.exists() and "can be deleted later" in done.stdout
    assert {k: v for k, v in tree(aside[0]).items() if not k.startswith(str(link))} == before_code
    assert tree(data) == before_data


# --- the newest release -----------------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not here")
def test_the_newest_release_is_the_highest_version_number_not_the_latest_tag_and_pre_releases_are_ignored(tmp_path):
    repo = tmp_path / "release-repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com"}

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "one")
    for tag in ("app-v2.4.0", "app-v2.10.0", "app-v2.9.9", "app-v3.0.0-rc1", "plugin-v9.9.9"):  # tagged in this order
        git("tag", tag)
    done = run(NEWEST, repo)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "app-v2.10.0"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not here")
def test_a_repository_with_no_release_tag_makes_the_release_command_fail_so_nothing_is_deleted(tmp_path):
    repo = tmp_path / "release-repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    assert run(NEWEST, repo).returncode != 0


# --- the skill itself -------------------------------------------------------------------------------------------------


def test_the_skill_never_stops_at_an_existing_install_and_writes_every_command_for_powershell_5_and_sh():
    text = SKILL.read_text(encoding="utf-8")
    assert text.count("already installed") == 1 and 'Never stop at\n"already installed"' in text
    fenced = [line for block in re.findall(r"```\w*\n(.*?)```", text, re.S) for line in block.splitlines()]
    assert not [line for line in fenced if "&&" in line or "||" in line]
    quoted = [line for line in fenced if line.startswith('"$') and " -c " in line]
    assert not quoted, "a quoted program is called with &"
    assert not [line for line in fenced if "stack.py" in line and "\\" in line], "paths use forward slashes"
    for marker in (LOOK, BACKUP, DELETE, ASIDE, NEWEST):
        code = command(marker)
        assert '"' not in code and "\\" not in code.replace("\\s", ""), marker
    assert "-old-" in text and "--again" in text and "-backup-" in text


def test_the_install_page_describes_the_same_one_flow_and_not_a_separate_update_job():
    page = DOC.read_text(encoding="utf-8")
    assert "stops when it finds an existing install" not in page
    assert "There is no `update` command yet" not in page
