"""`resumebot init`, `connect`, `doctor` and `status` (app/setup_cli.py): what the user's own Claude runs to set
the app up.
No database, no network, no real Claude Code: the app is a fake behind an httpx mock transport and the `claude` program
is a recorded fake. Every name, zone and token is made up."""

import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import main_cli, setup_cli
from app.settings import DATA_DIR_VARIABLE, VERSION, JobSearchSettings

OWNER = "Sam Example"
ZONE = "Asia/Tokyo"


@pytest.fixture(autouse=True)
def _no_ambient_folder(monkeypatch):
    monkeypatch.delenv(setup_cli.DATA_VARIABLE, raising=False)
    monkeypatch.delenv("TZ", raising=False)


@pytest.fixture(autouse=True)
def _no_real_documents(monkeypatch, tmp_path):
    """The machine running the tests has a Documents folder of its own; no test reads it."""
    monkeypatch.setattr(setup_cli, "documents_folder", lambda: tmp_path / "Docs")


@pytest.fixture(autouse=True)
def _a_safe_sqlite(monkeypatch):
    """The machine running the tests may carry any SQLite; the doctor's line about it has its own tests."""
    monkeypatch.setattr(setup_cli, "sqlite_version", lambda: "3.53.1")


REAL_DOCUMENTS_FOLDER = setup_cli.documents_folder
REAL_CLAUDE_RUNS = setup_cli._claude_runs
REAL_SHORTCUT_PROBLEM = setup_cli._shortcut_problem
REAL_CLAUDE_LOCATIONS = setup_cli._claude_locations


@pytest.fixture(autouse=True)
def _no_real_file_limits(monkeypatch):
    """`init` limits each secret file to this user (icacls on Windows): no test runs that."""
    monkeypatch.setattr(setup_cli, "_only_me_file", lambda path: None)


@pytest.fixture(autouse=True)
def _no_installed_claude(monkeypatch):
    """The machine running the tests may have Claude Code in its install folder; the lookup has its own tests."""
    monkeypatch.setattr(setup_cli, "_claude_locations", lambda: [])


@pytest.fixture(autouse=True)
def _every_claude_runs(monkeypatch):
    """The lookup asks a candidate to run `--version`; here every one answers (the probe has its own tests)."""
    monkeypatch.setattr(setup_cli, "_claude_runs", lambda program: True)


@pytest.fixture(autouse=True)
def _a_good_shortcut(monkeypatch):
    """The machine running the tests has a desktop of its own; its line in the doctor has its own tests."""
    monkeypatch.setattr(setup_cli, "_shortcut_problem", lambda install: None)


@pytest.fixture(autouse=True)
def _no_real_desktop(monkeypatch, tmp_path):
    """The machine running the tests has a Desktop of its own; no test writes there."""
    monkeypatch.setattr(setup_cli, "mac_desktop", lambda: tmp_path / "Desktop")


@pytest.fixture(autouse=True)
def _no_ambient_claude_config(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_windows_zone(monkeypatch):
    """The machine running the tests has a zone of its own in its registry; no test reads it."""
    monkeypatch.setattr(setup_cli, "windows_zone_name", lambda: None)


REAL_CREATE_DATABASE = setup_cli.create_database

#: Elsewhere the machine tells its own zone first (/etc/localtime), so the name Windows reports is never asked for.
windows_zone_only = pytest.mark.skipif(sys.platform != "win32", reason="the machine tells its own zone first")


@pytest.fixture(scope="module")
def schema_file(tmp_path_factory):
    """A database file made once, for real, that the tests copy instead of each building a schema (about 3 s each)."""
    url = setup_cli.database_url(tmp_path_factory.mktemp("schema"))
    assert REAL_CREATE_DATABASE(url) is True
    return setup_cli.database_file(url)


@pytest.fixture(autouse=True)
def _copy_the_schema(monkeypatch, schema_file):
    """init makes its database by copying the one made once; a test that needs the real build asks for real_database."""
    def copy(url):
        path = setup_cli.database_file(url)
        if path is None or path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(schema_file, path)
        return True

    monkeypatch.setattr(setup_cli, "create_database", copy)


@pytest.fixture
def real_database(monkeypatch):
    monkeypatch.setattr(setup_cli, "create_database", REAL_CREATE_DATABASE)


#: A token of the shape `claude setup-token` prints (one whole `sk-ant-` run).
VALID_TOKEN = "sk-ant-oat01-" + "Ab1_-" * 20


def make_install(tmp_path, **env):
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    (folder / "secrets" / setup_cli.TOKEN_FILE).write_text(VALID_TOKEN, encoding="utf-8")  # `signin`'s file
    if env:
        with (folder / ".env").open("a", encoding="utf-8") as file:
            file.writelines(f"{key}={value}\n" for key, value in env.items())
    return folder


HEALTH = {"app": setup_cli.APP_NAME, "status": "ok", "version": VERSION, "db": "ok"}


def serve_fake_app(monkeypatch, handler):
    monkeypatch.setattr(setup_cli, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def app_with_doors(tokens, api=None):
    """A fake app: /health ok, each door answers its tools to its own token only, and the API says what `api` maps."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json=HEALTH)
        for door in setup_cli.DOORS:
            if path == door.path:
                good = request.headers.get("authorization") == f"Bearer {tokens[door.name]}"
                return httpx.Response(200 if good else 401, text=f"event: message\ndata: {door.tool}" if good else "")
        if api is not None and path in api:
            return httpx.Response(200, json=api[path])
        return httpx.Response(404)

    return handler


# --- init -------------------------------------------------------------------------------------------------------------


def test_init_makes_the_folder_the_settings_file_and_each_secret(tmp_path, capsys):
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", f"  {OWNER} ", "--timezone", ZONE, "--data", str(folder)]) == 0
    assert setup_cli.read_env(folder / ".env") == {
        "JOBSEARCH_OWNER": OWNER, "JOBSEARCH_TIMEZONE": ZONE,
        "JOBSEARCH_DATABASE_URL": setup_cli.database_url(folder),
        "RESUMEBOT_PACKET_DIR": str(tmp_path / "Docs" / "resumebot")}
    for name in setup_cli.SECRET_FILES:
        assert len((folder / "secrets" / name).read_text(encoding="utf-8")) == 64
    assert "secrets: 4 made, 0 kept" in capsys.readouterr().out


def test_init_on_a_new_install_names_the_documents_resumebot_folder_as_the_packet_folder(tmp_path):
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    named = setup_cli.read_env(folder / ".env")["RESUMEBOT_PACKET_DIR"]
    assert Path(named) == tmp_path / "Docs" / "resumebot"
    assert not Path(named).exists()


def test_init_adds_no_packet_folder_to_settings_made_before_it_did(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    old = f"JOBSEARCH_OWNER={OWNER}\nJOBSEARCH_TIMEZONE={ZONE}\n"
    (folder / ".env").write_bytes(old.encode("utf-8"))
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert "RESUMEBOT_PACKET_DIR" not in setup_cli.read_env(folder / ".env")


def test_init_names_no_packet_folder_when_the_documents_path_would_break_the_settings_file(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "documents_folder", lambda: tmp_path / "My #Docs")
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert "RESUMEBOT_PACKET_DIR" not in setup_cli.read_env(folder / ".env")
    assert "packet folder: not set" in capsys.readouterr().out


def test_init_keeps_a_documents_path_whose_name_holds_a_hash_without_a_space(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cli, "documents_folder", lambda: tmp_path / "C#dev")
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert Path(setup_cli.read_env(folder / ".env")["RESUMEBOT_PACKET_DIR"]) == tmp_path / "C#dev" / "resumebot"


def test_init_leaves_a_packet_folder_the_settings_already_name_alone(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    old = (f"JOBSEARCH_OWNER={OWNER}\nJOBSEARCH_TIMEZONE={ZONE}\n"
           f"JOBSEARCH_DATABASE_URL={setup_cli.database_url(folder)}\nRESUMEBOT_PACKET_DIR=D:/mine\n")
    (folder / ".env").write_bytes(old.encode("utf-8"))
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert (folder / ".env").read_bytes() == old.encode("utf-8")


def test_documents_folder_falls_back_to_home_documents_when_the_windows_call_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli.Path, "home", classmethod(lambda cls: tmp_path))

    def refuse(*_a, **_k):
        raise OSError("no such folder")

    monkeypatch.setattr("ctypes.windll", SimpleNamespace(shell32=SimpleNamespace(SHGetKnownFolderPath=refuse),
                                                         ole32=SimpleNamespace(CoTaskMemFree=refuse)), raising=False)
    assert REAL_DOCUMENTS_FOLDER() == tmp_path / "Documents"


@pytest.mark.skipif(sys.platform != "win32", reason="the known-folder call is Windows only")
def test_documents_folder_on_windows_is_an_absolute_path():
    import winreg
    found = REAL_DOCUMENTS_FOLDER()
    assert found.is_absolute()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            personal = os.path.expandvars(winreg.QueryValueEx(key, "Personal")[0])
    except OSError:
        return
    assert os.path.normcase(str(found)) == os.path.normcase(personal)


def test_init_says_the_data_folder_variable_is_optional_and_asks_for_nothing_more(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")  # on Windows `init` sets it (test_setup_cli_verbs.py)
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1].startswith(f"optional: set {setup_cli.DATA_VARIABLE} to {folder} for this user")
    assert "--data" in lines[-1]
    assert not any(line.startswith("next:") for line in lines)


def test_init_run_again_with_the_same_answers_changes_nothing(tmp_path, capsys):
    folder = make_install(tmp_path)
    before = {p.name: p.read_text(encoding="utf-8") for p in (folder / "secrets").iterdir()}
    capsys.readouterr()
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert {p.name: p.read_text(encoding="utf-8") for p in (folder / "secrets").iterdir()} == before
    assert "secrets: 0 made, 4 kept" in capsys.readouterr().out


def test_init_never_rewrites_a_settings_file_that_says_something_else(tmp_path, capsys):
    folder = make_install(tmp_path)
    before = (folder / ".env").read_text(encoding="utf-8")
    assert setup_cli.init(["--owner", "Someone Else", "--timezone", ZONE, "--data", str(folder)]) == 1
    assert (folder / ".env").read_text(encoding="utf-8") == before
    assert "edit it by hand" in capsys.readouterr().err


@pytest.mark.parametrize("owner", ["", "   ", "Sam # Example", "'Sam", "Sam\nExample", "${HOME}"])
def test_init_refuses_an_owner_name_that_is_blank_or_would_break_the_settings_file(tmp_path, capsys, owner):
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", owner, "--timezone", ZONE, "--data", str(folder)]) == 2
    assert not folder.exists()
    assert capsys.readouterr().err.strip()


@pytest.mark.parametrize("zone", ["", "  ", "Mars/Olympus", "Asia/Tokyo ", "eastern", "../etc/passwd"])
def test_init_refuses_a_time_zone_that_is_blank_or_not_an_iana_zone(tmp_path, capsys, zone):
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", OWNER, "--timezone", zone, "--data", str(folder)]) == 2
    assert not folder.exists()
    assert "time zone" in capsys.readouterr().err


def test_init_takes_this_machines_zone_when_none_is_given_or_refuses_when_it_cannot_tell(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "machine_zone", lambda: "Europe/Paris")
    assert setup_cli.init(["--owner", OWNER, "--data", str(tmp_path / "a")]) == 0
    assert setup_cli.read_env(tmp_path / "a" / ".env")["JOBSEARCH_TIMEZONE"] == "Europe/Paris"
    monkeypatch.setattr(setup_cli, "machine_zone", lambda: None)
    assert setup_cli.init(["--owner", OWNER, "--data", str(tmp_path / "b")]) == 2
    assert not (tmp_path / "b").exists()
    assert "could not be told" in capsys.readouterr().err


@windows_zone_only
def test_init_records_the_standard_name_of_the_zone_windows_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cli, "windows_zone_name", lambda: "Tokyo Standard Time")
    assert setup_cli.init(["--owner", OWNER, "--data", str(tmp_path / "data")]) == 0
    assert setup_cli.read_env(tmp_path / "data" / ".env")["JOBSEARCH_TIMEZONE"] == "Asia/Tokyo"


@windows_zone_only
def test_init_refuses_a_windows_zone_the_table_does_not_know_and_says_what_to_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "windows_zone_name", lambda: "Olympus Standard Time")
    assert setup_cli.init(["--owner", OWNER, "--data", str(tmp_path / "data")]) == 2
    assert not (tmp_path / "data").exists()
    said = capsys.readouterr().err
    assert "'Olympus Standard Time'" in said
    assert "--timezone" in said
    assert "Area/City" in said


def test_init_uses_the_time_zone_it_is_given_whatever_windows_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cli, "windows_zone_name", lambda: "Tokyo Standard Time")
    assert setup_cli.init(["--owner", OWNER, "--timezone", "Europe/Paris", "--data", str(tmp_path / "data")]) == 0
    assert setup_cli.read_env(tmp_path / "data" / ".env")["JOBSEARCH_TIMEZONE"] == "Europe/Paris"


def test_every_windows_zone_in_the_table_names_a_zone_this_machine_knows():
    from app.windows_zones import WINDOWS_ZONES

    assert WINDOWS_ZONES
    assert [name for name, zone in WINDOWS_ZONES.items() if not setup_cli.is_zone(zone)] == []


def test_machine_zone_reads_tz_and_ignores_a_value_that_is_no_zone(monkeypatch):
    monkeypatch.setenv("TZ", f":{ZONE}")
    assert setup_cli.machine_zone() == ZONE
    monkeypatch.setenv("TZ", "not-a-zone")
    assert setup_cli.machine_zone() != "not-a-zone"


def test_the_data_folder_is_the_option_then_the_environment_then_the_users_own(tmp_path, monkeypatch):
    monkeypatch.setenv(setup_cli.DATA_VARIABLE, str(tmp_path / "from-env"))
    assert setup_cli.data_folder(str(tmp_path / "named")) == tmp_path / "named"
    assert setup_cli.data_folder() == tmp_path / "from-env"
    monkeypatch.delenv(setup_cli.DATA_VARIABLE)
    assert setup_cli.data_folder() == setup_cli.default_data_folder()
    assert setup_cli.default_data_folder().name == "resumebot"


def test_the_names_init_and_the_doors_use_are_the_ones_the_app_reads(tmp_path, monkeypatch):
    assert setup_cli.DATA_VARIABLE == DATA_DIR_VARIABLE
    monkeypatch.setenv(DATA_DIR_VARIABLE, str(tmp_path))
    fresh = JobSearchSettings(_env_file=None)
    read_by_the_app = {fresh.agent_token_file, fresh.wake_secret_file, fresh.owner_token_file, fresh.browser_token_file}
    assert read_by_the_app == {str(tmp_path / "secrets" / name) for name in setup_cli.SECRET_FILES}
    assert {d.token_file for d in setup_cli.DOORS} <= set(setup_cli.SECRET_FILES)


# --- connect ----------------------------------------------------------------------------------------------------------


RECORDED = Path(__file__).parent / "fixtures" / "claude-mcp.txt"


def recorded(section: str) -> str:
    """The text a real `claude mcp` printed in one recorded case (fixtures/claude-mcp.txt, a throwaway configuration and
    a made-up token): its exit-code line is dropped. `<url>` and `<token>` are what a test fills in."""
    text = RECORDED.read_text(encoding="utf-8")
    body = text.split(f"### {section}" + NL, 1)[1].split(NL + "### ", 1)[0]
    return body.split(NL, 2)[2].rstrip(NL) + NL


NL = chr(10)


def recorded_get(name, url, token):
    return (recorded("get").replace("rb-fix", name).replace("http://127.0.0.1:9/mcp/", url)
            .replace("FAKE-TOKEN-0000", token))


class FakeClaude:
    """A fake `claude mcp` that behaves, and prints, as the recorded real one does: `add` of a name that exists exits 1
    "already exists"; `remove` and `get` of a missing name exit 1; `get` prints the whole bearer header. `doors` is what
    it holds ({name: (url, token)}). `refuse` names a subcommand (`add`) it always exits 1 on ("no such thing");
    `fail_add_after_remove` fails the first add that follows a remove (the replace), `fail_every_add_after_remove`
    all of them (so the old door cannot be put back either)."""

    def __init__(self, refuse=None, doors=None, fail_add_after_remove=False, fail_every_add_after_remove=False):
        self.commands, self.refuse, self.doors = [], refuse, dict(doors or {})
        self.fail_once, self.fail_always, self.removed = fail_add_after_remove, fail_every_add_after_remove, False

    def __call__(self, command):
        self.commands.append(list(command))
        verb = command[2]
        done = lambda code, out="", err="": subprocess.CompletedProcess(command, code, out, err)  # noqa: E731
        if verb == "add":
            name = command[command.index("--scope") + 2]
            url = command[command.index("--scope") + 3]
            token = command[command.index("--header") + 1].removeprefix("Authorization: Bearer ")
            if self.refuse == "add":
                return done(1, "", "no such thing")
            if self.removed and (self.fail_once or self.fail_always):
                self.fail_once = False
                return done(1, "", "disk full")
            if name in self.doors:
                return done(1, "", f"MCP server {name} already exists in user config")
            self.doors[name] = (url, token)
            return done(0, recorded("add-new").replace("rb-fix", name))
        if verb == "remove":
            if command[3] not in self.doors:
                return done(1, "", f'No MCP server named "{command[3]}" in user scope')
            del self.doors[command[3]]
            self.removed = True
            return done(0, recorded("remove").replace("rb-fix", command[3]))
        if verb == "get":
            if command[3] not in self.doors:
                return done(1, "", f'No MCP server named "{command[3]}".')
            return done(0, recorded_get(command[3], *self.doors[command[3]]))
        return done(0)


def tokens_of(folder):
    return {d.name: (folder / "secrets" / d.token_file).read_text(encoding="utf-8") for d in setup_cli.DOORS}


def test_connect_registers_each_door_with_its_token_checks_it_and_never_prints_a_token(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    tokens = tokens_of(folder)
    claude = FakeClaude()
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(setup_cli, "_run", claude)
    serve_fake_app(monkeypatch, app_with_doors(tokens))
    capsys.readouterr()
    assert setup_cli.connect(["--data", str(folder)]) == 0
    out = capsys.readouterr()
    adds = [c for c in claude.commands if c[1:3] == ["mcp", "add"]]
    assert [a[a.index("--transport") + 1] for a in adds] == ["http", "http"]
    assert [a[a.index("--scope") + 1] for a in adds] == ["user", "user"]
    headers = [a[a.index("--header") + 1] for a in adds]
    assert headers == [f"Authorization: Bearer {tokens[d.name]}" for d in setup_cli.DOORS]
    assert [a[7] for a in adds] == [d.name for d in setup_cli.DOORS]
    assert [a[8] for a in adds] == ["http://127.0.0.1:8430/mcp-owner/", "http://127.0.0.1:8530/mcp-browser/"]
    assert [c[2] for c in claude.commands].count("remove") == 0  # nothing was there to replace
    assert claude.doors == {d.name: (f"{setup_cli.Install(folder).door_base(d)}{d.path}", tokens[d.name])
                            for d in setup_cli.DOORS}
    assert "it answers" in out.out and "does NOT" not in out.out
    assert not any(token in out.out + out.err for token in tokens.values())


def test_connect_follows_the_app_address_the_settings_name(tmp_path, monkeypatch):
    folder = make_install(tmp_path, RESUMEBOT_APP_URL="http://localhost:8440/")
    claude = FakeClaude()
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(setup_cli, "_run", claude)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 0
    assert any("http://localhost:8440/mcp-owner/" in c for c in claude.commands)
    assert any("http://127.0.0.1:8540/mcp-browser/" in c for c in claude.commands), (
        "the engine is the page's port plus 100")


def test_connect_says_when_a_door_does_not_answer_with_this_installs_token_and_exits_1(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(setup_cli, "_run", FakeClaude())
    serve_fake_app(monkeypatch, app_with_doors({d.name: "another-apps-token" for d in setup_cli.DOORS}))
    assert setup_cli.connect(["--data", str(folder)]) == 1
    assert capsys.readouterr().out.count("it does NOT answer") == 2


def test_connect_says_when_claude_code_refuses_to_register(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(setup_cli, "_run", FakeClaude(refuse="add"))
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 1
    assert "Claude Code refused to register it: no such thing" in capsys.readouterr().err


def test_connect_stops_before_anything_when_not_set_up_or_claude_is_missing(tmp_path, monkeypatch, capsys):
    claude = FakeClaude()
    monkeypatch.setattr(setup_cli, "_run", claude)
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/bin/claude")
    assert setup_cli.connect(["--data", str(tmp_path / "nothing")]) == 1
    assert "resumebot init" in capsys.readouterr().err
    folder = make_install(tmp_path)
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: None)
    assert setup_cli.connect(["--data", str(folder)]) == 1
    assert "Claude Code was not found on PATH" in capsys.readouterr().err
    assert claude.commands == []


# --- doctor -----------------------------------------------------------------------------------------------------------


#: The addresses a default install registers its doors at, as `claude mcp get` prints them.
DOOR_URLS = {"resumebot-owner": "http://127.0.0.1:8430/mcp-owner/", "resumebot-browser": "http://127.0.0.1:8530/mcp-browser/"}


def fake_programs(monkeypatch, *, present=("claude", "node"), signed_in=True, node="v22.4.0", registered=True,
                  at=None):
    """`registered` False: `claude mcp get` exits 1. `at`: the address it prints (default: this install's own)."""
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: f"/bin/{name}" if name in present else None)

    def run(command):
        said = (command[1], command[2]) if len(command) > 2 else (command[1],)
        if command[0].endswith("node"):
            return subprocess.CompletedProcess(command, 0, node + "\n", "")
        ok = {("auth", "status"): signed_in, ("mcp", "get"): registered}.get(said, True)
        printed = ""
        if said == ("mcp", "get") and ok:
            printed = f"{command[3]}:\n  Scope: User config\n  Type: http\n  URL: {at or DOOR_URLS[command[3]]}\n"
        return subprocess.CompletedProcess(command, 0 if ok else 1, printed, "")

    monkeypatch.setattr(setup_cli, "_run", run)


def test_doctor_has_one_ok_line_for_each_piece_when_everything_is_there(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 12 and all(line.startswith(("ok ", "config ")) for line in lines[:-1])
    assert lines[-1].startswith("logs ")  # the logs line is information, not a check


def test_doctor_names_the_logs_folder_and_the_newest_log_files(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    logs = folder / "logs"
    logs.mkdir()
    (logs / "page.log.1").write_text("old", encoding="utf-8")
    (logs / "page.log").write_text("SECRET-CONTENT", encoding="utf-8")
    os.utime(logs / "page.log.1", (1, 1))
    (logs / "engine.log").write_text("x", encoding="utf-8")
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    setup_cli.doctor(["--data", str(folder)])
    out = capsys.readouterr().out
    line = [row for row in out.splitlines() if row.startswith("logs ")]
    assert line == [f"logs     {logs} (page: page.log; engine: engine.log; supervisor: none yet; "
                    "open: none yet; start: none yet)"]
    assert "SECRET-CONTENT" not in out


def test_doctor_names_the_logs_folder_when_there_are_no_logs(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "nothing"
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, lambda request: httpx.Response(500))
    setup_cli.doctor(["--data", str(folder)])
    line = [row for row in capsys.readouterr().out.splitlines() if row.startswith("logs ")]
    assert line == [f"logs     {folder / 'logs'} (page: none yet; engine: none yet; supervisor: none yet; "
                    "open: none yet; start: none yet)"]


def test_doctor_names_each_missing_piece_on_a_line_of_its_own(tmp_path, monkeypatch, capsys):
    fake_programs(monkeypatch, present=("claude",))
    serve_fake_app(monkeypatch, lambda request: httpx.Response(500))
    assert setup_cli.doctor(["--data", str(tmp_path / "nothing")]) == 1
    lines = capsys.readouterr().out.splitlines()
    missing = [line for line in lines if line.startswith("MISSING")]
    assert [line.split(":")[0] for line in missing] == [
        "MISSING  agents sign in", "MISSING  Node", "MISSING  settings", "MISSING  app"]
    assert sum(line.startswith("skipped") for line in lines) == 2  # the doors, while the app is down
    assert len(lines) == 2 + 4 + 2 + 1 + 1 + 1 + 1  # the config line, the shortcut line and the logs line


def test_doctor_catches_claude_code_missing_signed_out_and_node_too_old(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    fake_programs(monkeypatch, present=("node",))
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    assert "MISSING  Claude Code: not found on PATH" in capsys.readouterr().out
    fake_programs(monkeypatch, signed_in=False, node="v20.1.0")
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "MISSING  Claude Code sign-in: not signed in" in out
    assert "MISSING  Node: is v20.1.0: Node 22 or newer is needed" in out


def test_doctor_says_when_the_app_answers_a_door_but_claude_code_does_not_have_it(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch, registered=False)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    assert capsys.readouterr().out.count("run `resumebot connect`") == 2


def test_doctor_says_when_a_door_is_registered_at_another_installs_address(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch, at="http://127.0.0.1:8431/mcp-owner/")
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert ("MISSING  resumebot-owner: your Claude Code has it registered at http://127.0.0.1:8431/mcp-owner/, "
            "not at this install's http://127.0.0.1:8430/mcp-owner/: run `resumebot connect`") in out
    assert "ok       resumebot-owner" not in out


def test_doctor_accepts_a_door_registered_at_this_installs_address(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "ok       resumebot-owner answers and is registered" in out


def test_doctor_says_missing_for_a_door_claude_code_has_not_registered_at_all(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch, registered=False)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "MISSING  resumebot-owner: the app answers it, but your Claude Code does not have it" in out


def test_doctor_says_the_agents_can_sign_in_when_a_token_is_stored_and_never_prints_it(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    (folder / "secrets" / setup_cli.TOKEN_FILE).write_text("sk-ant-SECRET-" + "x" * 40, encoding="utf-8")
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "ok       a token is stored (valid for about a year" in out
    assert "run `resumebot signin --again`" in out
    assert "SECRET" not in out


@pytest.mark.parametrize("content", [None, "", "a-token-from-an-earlier-release", VALID_TOKEN + VALID_TOKEN],
                         ids=["no file", "empty file", "not a token", "two glued"])
def test_doctor_says_to_run_signin_not_setup_token_when_no_token_is_stored(tmp_path, monkeypatch, capsys, content):
    folder = make_install(tmp_path)
    path = folder / "secrets" / setup_cli.TOKEN_FILE
    path.unlink()
    if content is not None:
        path.write_text(content, encoding="utf-8")
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    said = "no token stored" if content is None else "the stored token is not a usable one"
    assert f"MISSING  agents sign in: {said}" in out and "run `resumebot signin`" in out
    assert "setup-token" not in out


def test_doctor_reads_the_token_file_in_this_process_and_shows_none_of_it(tmp_path, monkeypatch, capsys):
    """H5 (docs/PRD.md path 17): the token's shape is checked from the file itself, no call is made with it, and no
    part of it is printed (this replaces the test that said doctor never opens the file)."""
    folder = make_install(tmp_path)
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "ok       a token is stored" in out and "sk-ant" not in out and VALID_TOKEN[13:30] not in out


def test_doctor_warns_when_the_token_is_nearly_a_year_old_and_still_counts_it_stored(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    monkeypatch.setattr(setup_cli, "_wall", lambda: (folder / "secrets" / setup_cli.TOKEN_FILE).stat().st_mtime
                        + 340 * 86400)
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "ok       a token is stored" in out and "warn     the token is 340 days old" in out


def test_a_token_shape_check_wants_one_whole_sk_ant_run(tmp_path):
    install = setup_cli.Install(tmp_path)
    path = install.agent_token_path
    path.parent.mkdir()
    for content, stored in ((VALID_TOKEN, True), (VALID_TOKEN + "\n", True), ("", False), ("sk-ant-short", False),
                            ("sk-ant-" + "a b" * 20, False), ("x" * 60, False), (VALID_TOKEN + VALID_TOKEN, False)):
        path.write_text(content, encoding="utf-8")
        assert setup_cli.token_is_stored(install) is stored, content[:20]
    path.write_bytes(b"\xff\xfe" + VALID_TOKEN.encode("utf-16-le"))
    assert setup_cli.token_is_stored(install) is False  # not UTF-8: no traceback


# --- status -----------------------------------------------------------------------------------------------------------


def run_status(folder, capsys):
    capsys.readouterr()
    assert setup_cli.status(["--for-claude", "--data", str(folder)]) == 0  # always 0: a session-start hook runs it
    return capsys.readouterr().out.splitlines()


def test_status_before_init_points_at_the_setup_skill(tmp_path, capsys):
    lines = run_status(tmp_path / "nothing", capsys)
    assert lines[0] == f"resumebot {VERSION}"
    assert lines[1].startswith("install: not set up")
    assert lines[-1].startswith("next: run the resumebot-setup:install-resumebot skill")


def test_status_with_the_app_down_says_to_start_it(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    serve_fake_app(monkeypatch, lambda request: httpx.Response(503))
    lines = run_status(folder, capsys)
    assert lines[2:] == ["app: not running", lines[-1]] and lines[-1].startswith("next: start the app")


def test_status_and_doctor_hints_start_the_app_with_stack_py_not_a_serve_command(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    def refuse(request):
        raise httpx.ConnectError("refused")

    serve_fake_app(monkeypatch, refuse)
    install = setup_cli.Install(folder)
    hint = setup_cli.start_hint(install)
    assert hint == f'`resumebot start --data "{folder}"`'  # L3: the command the skill allows, not stack.py
    assert hint in setup_cli.app_health(install)
    assert hint in setup_cli.check_door(install, setup_cli.DOORS[0])
    assert hint in "\n".join(run_status(folder, capsys))
    assert "resumebot serve" not in setup_cli.app_health(install)


def onboarding_api(*, resume=0, targets=False, searched=False, mail=False):
    return {
        "/api/v1/documents": {"versions": {"resume": resume, "about": 0, "writing": 0}},
        "/api/v1/profile": {"is_set": targets},
        "/api/v1/missions": {"missions": [{"key": "sweep_stuck", "last_run": {"id": "x"}},
                                          {"key": "scout_boards", "last_run": {"id": "y"} if searched else None}]},
        "/api/v1/mail-login": {"connected": mail},
    }


def test_status_walks_the_steps_in_order_and_names_the_first_one_not_done_and_the_tool_that_does_it(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    tokens = tokens_of(folder)
    steps = [
        (onboarding_api(), "next: import the master resume (act.do resume.import with the file's whole path; "
                           "the owner confirms the reading on My Resume)"),
        (onboarding_api(resume=2), "next: set the target roles and search settings (act.read profile.read, then "
                                   "act.do profile.save with what the owner says)"),
        (onboarding_api(resume=2, targets=True),
         "next: run the first search (act.do mission.run with mission scout_boards)"),
        (onboarding_api(resume=2, targets=True, searched=True),
         "next: connect mail (optional) (act.read mail.state; the owner types the password on the page, never into "
         "the chat)"),
        (onboarding_api(resume=2, targets=True, searched=True, mail=True), "next: onboarding is done"),
    ]
    for api, expected in steps:
        serve_fake_app(monkeypatch, app_with_doors(tokens, api))
        assert run_status(folder, capsys)[-1] == expected


def test_status_gives_every_step_a_plain_line_and_reads_the_search_by_its_mission(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder), onboarding_api(resume=1, targets=True, mail=True)))
    assert run_status(folder, capsys)[3:] == [
        "resume: imported", "tracker: not imported (optional: only if they have one)",
        "employer list: not imported (optional: only if they have one)", "targets: set",
        "first search: not run yet", "mail: connected",
        f"next: {setup_cli.TOOLS['first search'].said()}"]  # the other mission's run is not the first search


def test_status_says_unknown_for_a_step_the_app_would_not_answer_and_goes_on(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    api = onboarding_api(resume=1, targets=True, searched=True, mail=True)
    del api["/api/v1/profile"]
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder), api))
    lines = run_status(folder, capsys)
    assert "targets: unknown (the app did not say)" in lines
    assert lines[-1] == "next: onboarding is done"


def put_rows(folder, *, tracker_roles=0, listed=0, owner=OWNER):
    """Rows in the install's own database: roles that came from a tracker, rows of an employer list."""
    import sqlalchemy as sa

    from app.jobsearch.models import EmployerListEntry, Opportunity
    from app.jobsearch.stages import STAGES

    engine = sa.create_engine(f"sqlite:///{(folder / setup_cli.DATABASE_FILE).as_posix()}")
    try:
        with engine.begin() as connection:
            for n in range(tracker_roles):
                connection.execute(sa.insert(Opportunity).values(
                    owner=owner, company=f"Made-up {n}", title="Role", match_key=f"made-up-{n}", origin="tracker",
                    slug=f"made-up-{n}", status=STAGES[0]))
            for n in range(listed):
                connection.execute(sa.insert(EmployerListEntry).values(
                    owner=owner, source="list.csv", key=f"made-up-{n}", name=f"Made-up {n}",
                    website=f"https://made-up-{n}.example"))
    finally:
        engine.dispose()


def test_status_says_the_tracker_and_the_employer_list_are_imported_once_their_rows_are_there(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    put_rows(folder, tracker_roles=2, listed=1)
    put_rows(folder, tracker_roles=5, owner="Someone Else")  # another owner's rows are not this owner's
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder), onboarding_api(resume=1, targets=True)))
    lines = run_status(folder, capsys)
    assert "tracker: imported" in lines and "employer list: imported" in lines
    only_list = make_install(tmp_path / "second")
    put_rows(only_list, listed=3)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(only_list), onboarding_api(resume=1, targets=True)))
    lines = run_status(only_list, capsys)
    assert "employer list: imported" in lines and lines[4].startswith("tracker: not imported (optional")


def test_an_optional_import_not_done_never_blocks_the_next_step(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)  # no tracker, no employer list
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder), onboarding_api(resume=1)))
    assert run_status(folder, capsys)[-1] == f"next: {setup_cli.TOOLS['targets'].said()}"
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder), onboarding_api(resume=1, targets=True,
                                                                              searched=True, mail=True)))
    assert run_status(folder, capsys)[-1] == "next: onboarding is done", "neither import holds up the end"


def test_status_says_unknown_for_the_imports_when_the_database_is_not_a_file_it_can_read(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    (folder / setup_cli.DATABASE_FILE).write_bytes(b"not a database at all, just text")
    monkeypatch.setattr(setup_cli, "has_settings_row", lambda file: True)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder), onboarding_api(resume=1, targets=True)))
    lines = run_status(folder, capsys)
    assert "tracker: unknown (the app did not say)" in lines and "employer list: unknown (the app did not say)" in lines


def test_status_reads_the_database_without_changing_it(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    before = (folder / setup_cli.DATABASE_FILE).read_bytes()
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder), onboarding_api()))
    run_status(folder, capsys)
    assert (folder / setup_cli.DATABASE_FILE).read_bytes() == before


def test_the_for_claude_flag_is_described_as_changing_nothing(capsys):
    with pytest.raises(SystemExit):
        setup_cli.status(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--for-claude" in text and "changes nothing" in text and "plain lines Claude reads" in text


# --- doctor: the SQLite -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("version,safe", [
    ("3.44.5", False), ("3.44.6", True), ("3.44.9", True), ("3.45.0", False), ("3.49.1", False),
    ("3.50.6", False), ("3.50.7", True), ("3.50.99", True), ("3.51.0", False), ("3.51.2", False),
    ("3.51.3", True), ("3.53.1", True), ("3.43.2", False), ("4.0.0", True)])
def test_sqlite_is_safe_on_each_side_of_each_boundary(version, safe):
    assert setup_cli.sqlite_is_safe(tuple(int(p) for p in version.split("."))) is safe


def test_doctor_says_ok_for_a_safe_sqlite_and_one_plain_line_with_the_fix_for_an_unsafe_one(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    assert "ok       SQLite 3.53.1 is clear of the WAL-reset bug" in capsys.readouterr().out
    monkeypatch.setattr(setup_cli, "sqlite_version", lambda: "3.49.1")
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    said = [line for line in capsys.readouterr().out.splitlines() if "SQLite" in line]
    assert len(said) == 1 and said[0].startswith("MISSING  SQLite: 3.49.1 has the WAL-reset bug")
    assert "uv python install 3.12 --upgrade" in said[0] and "uv python find --managed-python 3.12" in said[0]


def test_doctor_reads_a_sqlite_version_it_cannot_parse_as_a_problem(monkeypatch):
    monkeypatch.setattr(setup_cli, "sqlite_version", lambda: "weird")
    assert "weird" in setup_cli.sqlite_problem()


# --- the command line -------------------------------------------------------------------------------------------------


def test_the_five_commands_are_in_the_help_and_run_through_the_one_entry_point(tmp_path, capsys):
    assert main_cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in ("init", "connect", "shortcut", "open", "doctor", "status"):
        assert name in main_cli.COMMANDS and any(line.split()[:1] == [name] for line in out.splitlines())
    folder = tmp_path / "data"
    assert main_cli.main(["init", "--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert setup_cli.read_env(folder / ".env")["JOBSEARCH_OWNER"] == OWNER
    with pytest.raises(SystemExit) as stopped:
        main_cli.main(["init", "--timezone", ZONE])  # no --owner
    assert stopped.value.code == 2


# --- the database init makes ------------------------------------------------------------------------------------------


def tables_and_revision(path):
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")}
        revision = connection.execute("select version_num from alembic_version").fetchall()
    finally:
        connection.close()
    return tables, revision


def test_init_makes_the_sqlite_file_with_its_tables_stamped_at_the_alembic_head(tmp_path, capsys, real_database):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app import db

    folder = make_install(tmp_path)
    tables, revision = tables_and_revision(folder / setup_cli.DATABASE_FILE)
    config = Config(str(db.BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(db.BACKEND_DIR / "migrations"))
    assert revision == [(ScriptDirectory.from_config(config).get_current_head(),)]
    assert len(tables) > 10
    assert setup_cli.read_env(folder / ".env")["JOBSEARCH_DATABASE_URL"].endswith("/resumebot.db")


def test_init_says_it_made_the_database_and_a_second_run_says_it_kept_it(tmp_path, capsys):
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert "database: made" in capsys.readouterr().out
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert "database: kept" in capsys.readouterr().out


def test_init_run_again_leaves_the_database_file_and_the_settings_as_they_are(tmp_path):
    folder = make_install(tmp_path)
    file, env = folder / setup_cli.DATABASE_FILE, folder / ".env"
    before = (file.read_bytes(), env.read_bytes())
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert (file.read_bytes(), env.read_bytes()) == before


def test_init_never_touches_a_database_file_that_is_already_there(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / setup_cli.DATABASE_FILE).write_bytes(b"someone's work")
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert (folder / setup_cli.DATABASE_FILE).read_bytes() == b"someone's work"


def test_init_leaves_a_database_address_the_settings_already_name_alone(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / ".env").write_text(
        f"JOBSEARCH_OWNER={OWNER}\nJOBSEARCH_TIMEZONE={ZONE}\nJOBSEARCH_DATABASE_URL=postgresql+asyncpg://x/y\n",
        encoding="utf-8")
    before = (folder / ".env").read_bytes()
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert (folder / ".env").read_bytes() == before
    assert not (folder / setup_cli.DATABASE_FILE).exists()


def test_init_adds_only_the_database_line_to_settings_made_before_it_did(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    old = f"JOBSEARCH_OWNER={OWNER}\nJOBSEARCH_TIMEZONE={ZONE}\n"
    (folder / ".env").write_bytes(old.encode("utf-8"))
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    added = f"JOBSEARCH_DATABASE_URL={setup_cli.database_url(folder)}\n"
    assert (folder / ".env").read_bytes() == (old + added).encode("utf-8")
    assert (folder / setup_cli.DATABASE_FILE).is_file()


def test_the_database_address_reads_back_to_its_file_on_both_path_styles(tmp_path):
    assert setup_cli.database_file(setup_cli.database_url(tmp_path)) == tmp_path / setup_cli.DATABASE_FILE
    assert setup_cli.database_file("sqlite+aiosqlite:///C:/data/x/resumebot.db") == Path("C:/data/x/resumebot.db")
    assert setup_cli.database_file("sqlite+aiosqlite:////var/x/resumebot.db") == Path("/var/x/resumebot.db")
    assert setup_cli.database_file("postgresql+asyncpg://x/y") is None


def test_init_removes_a_database_file_that_was_cut_short_and_says_so(tmp_path, monkeypatch, capsys, real_database):
    from app import db

    def fail(_connection):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "create_schema", fail)
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 1
    assert not (folder / setup_cli.DATABASE_FILE).exists()
    assert "disk full" in capsys.readouterr().err


# --- the desktop shortcut, and the old autostart cleaned up ---------------------------------------------------------


REAL_STARTUP_FOLDER = setup_cli.startup_folder


@pytest.fixture(autouse=True)
def fake_startup_folder(tmp_path, monkeypatch):
    """No test here ever writes into the real Startup folder."""
    monkeypatch.setattr(setup_cli, "startup_folder", lambda: tmp_path / "Startup")


class FakePowerShell:
    """Stands in for `_run`. `task` is what Get-ScheduledTask reports (None: no task); `returncode` and `stderr` are
    the answer to every other call; `refuse` lists words, and a call whose command holds one of them fails."""

    def __init__(self, returncode=0, stderr="", task=None, refuse=(), keep_task=False):
        self.calls, self.returncode, self.stderr, self.task, self.refuse = [], returncode, stderr, task, refuse
        self.keep_task = keep_task  # Windows would not remove the task

    def __call__(self, command):
        self.calls.append(list(command))
        text = " ".join(command)
        if "Get-ScheduledTask" in text:
            return subprocess.CompletedProcess(command, 0, "" if self.task is None else f"TASK {self.task}\n", "")
        if "Unregister-ScheduledTask" in text and not self.keep_task:
            self.task = None  # the task is gone when it is asked about again
        if self.refuse and not any(word in text for word in self.refuse):
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)

    def scripts(self, word):
        return [call[-1] for call in self.calls if word in " ".join(call)]


def on_windows(monkeypatch, shell):
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_run", shell)


def test_startup_folder_is_under_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert REAL_STARTUP_FOLDER() == (
        tmp_path / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")


def fake_python(tmp_path, monkeypatch, *, windowless=True):
    fake = tmp_path / "py" / "python.exe"
    fake.parent.mkdir()
    fake.write_bytes(b"")
    if windowless:
        (fake.parent / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(setup_cli.sys, "executable", str(fake))


def test_shortcut_writes_the_lnk_on_the_known_desktop_folder_and_targets_pythonw_running_open(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_python(tmp_path, monkeypatch)
    shell = FakePowerShell()
    on_windows(monkeypatch, shell)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    script, = shell.scripts("CreateShortcut")
    assert "[Environment]::GetFolderPath('Desktop')" in script and "USERPROFILE" not in script
    assert "'resumebot.lnk'" in script and "$link.Save()" in script
    assert "pythonw.exe'" in script and f'-m app.main_cli open --data "{folder}"' in script
    assert f"$link.WorkingDirectory = '{setup_cli._backend_folder()}'" in script
    assert "shortcut resumebot.lnk is on your desktop" in capsys.readouterr().out


def test_shortcut_wears_the_shipped_robot_icon(tmp_path, monkeypatch):
    folder = make_install(tmp_path)
    fake_python(tmp_path, monkeypatch)
    shell = FakePowerShell()
    on_windows(monkeypatch, shell)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    script, = shell.scripts("CreateShortcut")
    assert f"$link.IconLocation = '{setup_cli.shortcut_icon()},0'" in script
    assert setup_cli.shortcut_icon().is_file() and setup_cli.shortcut_icon().name == "resumebot.ico"


def _icon_16px_rgba(data: bytes) -> list[list[tuple[int, int, int, int]]]:
    count = struct.unpack_from("<H", data, 4)[0]
    for index in range(count):
        width, _h, _c, _r, _p, _b, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        if width == 16:
            break
    else:
        raise AssertionError("no 16 px entry")
    image = data[offset:offset + length]
    assert image[1:4] == b"PNG"
    position, idat = 8, b""
    while position < len(image):
        size, kind = struct.unpack_from(">I4s", image, position)
        body = image[position + 8:position + 8 + size]
        if kind == b"IHDR":
            assert struct.unpack(">IIBB", body[:10]) == (16, 16, 8, 6)
        if kind == b"IDAT":
            idat += body
        position += 12 + size
    raw = zlib.decompress(idat)
    rows = []
    for row in range(16):
        line = raw[row * 65:(row + 1) * 65]
        assert line[0] == 0
        rows.append([tuple(line[1 + 4 * c:5 + 4 * c]) for c in range(16)])
    return rows


def test_the_shipped_icon_holds_the_favicons_pixels_at_16_px_and_the_other_sizes(monkeypatch):
    root = Path(__file__).resolve().parents[4]
    expected = [[(0, 0, 0, 0)] * 16 for _ in range(16)]
    svg = (root / "app" / "frontend" / "public" / "favicon.svg").read_text(encoding="utf-8")
    for tag in re.findall(r"<rect[^>]*>", svg):
        a = dict(re.findall(r"(\w+)='([^']*)'", tag))
        fill = a["fill"].lstrip("#")
        expected[int(a["y"])][int(a["x"])] = (int(fill[0:2], 16), int(fill[2:4], 16), int(fill[4:6], 16), 255)
    data = setup_cli.shortcut_icon().read_bytes()
    assert _icon_16px_rgba(data) == expected
    sizes = [struct.unpack_from("<B", data, 6 + 16 * i)[0] for i in range(struct.unpack_from("<H", data, 4)[0])]
    assert sizes == [16, 32, 48, 0]  # 0 is 256 in an icon directory


def test_shortcut_saves_a_new_file_checks_it_and_only_then_replaces_the_old_one(tmp_path, monkeypatch):
    folder = make_install(tmp_path)
    shell = FakePowerShell()
    on_windows(monkeypatch, shell)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    script, = shell.scripts("CreateShortcut")
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "Remove-Item -LiteralPath $path" not in script  # the old one is never deleted first (M9)
    saved = script.index("$link.Save()")
    assert script.index("CreateShortcut($new)") < saved < script.index("Test-Path -LiteralPath $new")
    assert script.index("Test-Path -LiteralPath $new") < script.index("Move-Item -LiteralPath $new -Destination $path")
    assert "resumebot-new.lnk" in script and script.rindex("Test-Path -LiteralPath $path") > script.index("Move-Item")


def test_shortcut_falls_back_to_python_when_there_is_no_pythonw(tmp_path, monkeypatch):
    folder = make_install(tmp_path)
    fake_python(tmp_path, monkeypatch, windowless=False)
    shell = FakePowerShell()
    on_windows(monkeypatch, shell)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    script, = shell.scripts("CreateShortcut")
    assert "python.exe'" in script and "pythonw" not in script


def test_shortcut_doubles_a_quote_in_a_path_so_the_script_stays_one_string(tmp_path, monkeypatch):
    folder = make_install(tmp_path / "O'Brien")
    shell = FakePowerShell()
    on_windows(monkeypatch, shell)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    assert "O''Brien" in shell.scripts("CreateShortcut")[0]


def test_shortcut_says_when_windows_refuses_and_exits_1(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    on_windows(monkeypatch, FakePowerShell(returncode=1, stderr="Access is denied\n", refuse=("CreateShortcut",)))
    assert setup_cli.shortcut(["--data", str(folder)]) == 1
    assert "Access is denied" in capsys.readouterr().err


def test_shortcut_stops_when_there_are_no_settings_yet(tmp_path, monkeypatch, capsys):
    shell = FakePowerShell()
    on_windows(monkeypatch, shell)
    assert setup_cli.shortcut(["--data", str(tmp_path / "none")]) == 1
    assert shell.calls == [] and "resumebot init" in capsys.readouterr().err


def on_macos(tmp_path, monkeypatch):
    """macOS, with a Desktop folder and this install's Python as real files in the test's own folder."""
    monkeypatch.setattr(setup_cli.sys, "platform", "darwin")
    (tmp_path / "Desktop").mkdir()
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    monkeypatch.setattr(setup_cli.sys, "executable", str(python))
    return tmp_path / "Desktop" / "resumebot.command", python


def test_shortcut_on_macos_writes_a_file_finder_can_run_that_opens_this_install(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    file, python = on_macos(tmp_path, monkeypatch)
    shell = FakePowerShell()
    monkeypatch.setattr(setup_cli, "_run", shell)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    assert shell.calls == []  # no PowerShell on a Mac
    assert os.access(file, os.X_OK)
    lines = file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/bin/sh"
    assert shlex.split(lines[-2]) == ["cd", str(setup_cli._backend_folder()), "||", "exit", "1"]
    assert shlex.split(lines[-1]) == ["exec", str(python), "-m", "app.main_cli", "open", "--data", str(folder)]
    assert "shortcut resumebot.command is on your desktop" in capsys.readouterr().out


def test_shortcut_on_macos_replaces_its_own_earlier_file_and_leaves_no_other(tmp_path, monkeypatch):
    folder = make_install(tmp_path)
    file, _ = on_macos(tmp_path, monkeypatch)
    file.write_text("an earlier one", encoding="utf-8")
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    assert "an earlier one" not in file.read_text(encoding="utf-8")
    assert [entry.name for entry in file.parent.iterdir()] == ["resumebot.command"]


def test_shortcut_on_macos_keeps_a_folder_with_a_space_or_a_quote_as_one_word(tmp_path, monkeypatch):
    file, _ = on_macos(tmp_path, monkeypatch)
    text = setup_cli.mac_shortcut_text(tmp_path / "my data's folder")
    assert shlex.split(text.splitlines()[-1])[-1] == str(tmp_path / "my data's folder")


def test_shortcut_on_macos_says_why_when_the_desktop_cannot_be_written(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    file, _ = on_macos(tmp_path, monkeypatch)
    file.parent.rmdir()
    assert setup_cli.shortcut(["--data", str(folder)]) == 1
    assert "could not make the shortcut" in capsys.readouterr().err


def test_the_doctor_on_macos_reads_the_desktop_file_for_what_it_starts(tmp_path, monkeypatch):
    folder = make_install(tmp_path)
    file, python = on_macos(tmp_path, monkeypatch)
    install = setup_cli.Install(folder)
    assert "there is none on the desktop" in REAL_SHORTCUT_PROBLEM(install)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    assert REAL_SHORTCUT_PROBLEM(install) is None
    assert "another data folder" in REAL_SHORTCUT_PROBLEM(setup_cli.Install(tmp_path / "elsewhere"))
    python.unlink()
    assert "which is not there" in REAL_SHORTCUT_PROBLEM(install)


def test_shortcut_on_other_systems_says_it_is_not_built_runs_nothing_and_exits_0(tmp_path, monkeypatch, capsys):
    shell = FakePowerShell()
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    monkeypatch.setattr(setup_cli, "_run", shell)
    assert main_cli.main(["shortcut", "--data", str(tmp_path / "d")]) == 0
    assert shell.calls == [] and "not built for linux" in capsys.readouterr().out


def test_shortcut_removes_this_installs_old_logon_task_and_startup_file_and_says_so_once(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    mine = tmp_path / "Startup" / "resumebot.cmd"
    mine.parent.mkdir()
    mine.write_text(f'start "" /min "p" "s" --data "{folder}" start\n')
    other = tmp_path / "Startup" / "other.cmd"
    other.write_text("echo hi\n")
    shell = FakePowerShell(task=f'"s" --data "{folder}" start')
    on_windows(monkeypatch, shell)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    assert not mine.exists() and other.exists()
    script, = shell.scripts("Unregister-ScheduledTask")
    assert "'resumebot'" in script
    out = capsys.readouterr().out.splitlines()
    assert sum("removed the old start at sign-in" in line for line in out) == 1


def test_shortcut_leaves_another_installs_old_task_and_startup_file_and_says_nothing_of_them(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    theirs = tmp_path / "Startup" / "resumebot.cmd"
    theirs.parent.mkdir()
    theirs.write_text('start "" /min "p" "s" --data "C:\\elsewhere" start\n')
    shell = FakePowerShell(task='"s" --data "C:\\elsewhere" start')
    on_windows(monkeypatch, shell)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    assert theirs.exists() and shell.scripts("Unregister-ScheduledTask") == []
    assert "removed the old start" not in capsys.readouterr().out


def test_the_autostart_verb_is_gone(tmp_path, capsys):
    assert "autostart" not in main_cli.COMMANDS and not hasattr(setup_cli, "autostart")
    assert main_cli.main(["autostart", "on"]) == 2
    assert "unknown command" in capsys.readouterr().err


# --- finding Claude Code ----------------------------------------------------------------------------------------------


def test_find_claude_takes_the_path_first_then_the_native_installers_folder(tmp_path, monkeypatch):
    installed = tmp_path / "claude"
    installed.write_text("")
    monkeypatch.setattr(setup_cli, "_claude_locations", lambda: [installed])
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/on/path/claude")
    assert setup_cli.find_claude() == "/on/path/claude"
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: None)
    assert setup_cli.find_claude() == str(installed)
    installed.unlink()
    assert setup_cli.find_claude() is None


def test_doctor_and_connect_use_the_installers_folder_when_claude_is_not_on_the_path(tmp_path, monkeypatch, capsys):
    installed = tmp_path / "claude"
    installed.write_text("")
    folder = make_install(tmp_path)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    fake_programs(monkeypatch, present=("node",))
    monkeypatch.setattr(setup_cli, "_claude_locations", lambda: [installed])
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    assert "ok       Claude Code is installed" in capsys.readouterr().out
    assert setup_cli.connect(["--data", str(folder)]) == 0


def test_doctor_does_not_claim_a_door_is_registered_when_claude_code_was_not_found(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    fake_programs(monkeypatch, present=("node",))
    monkeypatch.setattr(setup_cli, "_claude_locations", lambda: [])
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "registered" not in out.replace("registration not checked", "")
    assert "ok       resumebot-owner answers (registration not checked: Claude Code was not found)" in out
    assert "MISSING  Claude Code: not found on PATH, in the native installer's folder (~/.local/bin)" in out


HOME, LOCAL, ROAMING = "home", "home/AppData/Local", "home/AppData/Roaming"
DESKTOP_COPY = "Claude/claude-code/2.1.300/0a1b2c/claude.exe"
#: One line for each place a supported Windows install leaves the program, under a made-up user's folders.
INSTALLED_PLACES = {
    "the native installer": f"{HOME}/.local/bin/claude.exe",
    "a WinGet link": f"{LOCAL}/Microsoft/WinGet/Links/claude.exe",
    "the WinGet package": f"{LOCAL}/Microsoft/WinGet/Packages/Anthropic.ClaudeCode_Example.Source_abc123/claude.exe",
    "npm": f"{ROAMING}/npm/claude.cmd",
    "the desktop app": f"{LOCAL}/Claude-Data/claude_abc123/claude-code/2.1.300/0a1b2c/claude.exe",
    "the desktop app, as a package": f"{LOCAL}/Packages/Claude_abc123/LocalCache/Roaming/{DESKTOP_COPY}",
    "the desktop app, unpackaged": f"{ROAMING}/{DESKTOP_COPY}",
}


def a_windows_pc(tmp_path, monkeypatch, *programs):
    """A made-up user's folders with a fake program at each given place, nothing on the PATH, and the real lookup."""
    monkeypatch.setattr(setup_cli, "_claude_locations", REAL_CLAUDE_LOCATIONS)
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    for variable, folder in (("USERPROFILE", HOME), ("LOCALAPPDATA", LOCAL), ("APPDATA", ROAMING)):
        monkeypatch.setenv(variable, str(tmp_path / folder))
    made = []
    for program in programs:
        made.append(tmp_path / program)
        made[-1].parent.mkdir(parents=True, exist_ok=True)
        made[-1].write_text("fake")
    return made


@pytest.mark.parametrize("place", INSTALLED_PLACES.values(), ids=INSTALLED_PLACES.keys())
def test_doctor_and_connect_find_claude_code_in_each_place_a_windows_install_puts_it(
        place, tmp_path, monkeypatch, capsys):
    (installed,) = a_windows_pc(tmp_path, monkeypatch, place)
    folder = make_install(tmp_path)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    fake_programs(monkeypatch, present=("node",))
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    assert f"ok       Claude Code is installed ({installed})" in capsys.readouterr().out
    claude = FakeClaude()
    monkeypatch.setattr(setup_cli, "_run", claude)
    assert setup_cli.connect(["--data", str(folder)]) == 0
    assert {command[0] for command in claude.commands} == {str(installed)}


def test_doctor_and_connect_name_the_places_looked_in_and_the_installer_when_claude_code_is_nowhere(
        tmp_path, monkeypatch, capsys):
    a_windows_pc(tmp_path, monkeypatch)
    folder = make_install(tmp_path)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    fake_programs(monkeypatch, present=("node",))
    capsys.readouterr()
    where = ("not found on PATH, in the native installer's folder (~/.local/bin), in WinGet's or npm's, or in the "
             "Claude desktop app's: install it from https://code.claude.com/docs/en/setup")
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    assert f"MISSING  Claude Code: {where} (looked in: " in capsys.readouterr().out
    assert setup_cli.connect(["--data", str(folder)]) == 1
    assert capsys.readouterr().err.startswith(f"Claude Code was {where}, then run this again (looked in: ")


def test_claude_code_on_the_path_wins_over_every_installed_place(tmp_path, monkeypatch):
    a_windows_pc(tmp_path, monkeypatch, *INSTALLED_PLACES.values())
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/on/path/claude")
    assert setup_cli.find_claude() == "/on/path/claude"


def test_the_installed_places_are_tried_in_order_the_desktop_apps_copy_last(tmp_path, monkeypatch):
    made = a_windows_pc(tmp_path, monkeypatch, *INSTALLED_PLACES.values())
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: None)
    for expected in made[:4]:
        assert setup_cli.find_claude() == str(expected)
        expected.unlink()
    assert setup_cli.find_claude() in [str(copy) for copy in made[4:]]


def test_the_desktop_apps_newest_version_wins_by_number_not_by_name(tmp_path, monkeypatch):
    root = f"{LOCAL}/Claude-Data/claude_abc123/claude-code"
    _, newest = a_windows_pc(
        tmp_path, monkeypatch, f"{root}/2.1.295/0a1b2c/claude.exe", f"{root}/2.1.1000/0a1b2c/claude.exe")
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: None)
    assert setup_cli.find_claude() == str(newest)


def test_a_desktop_copy_that_is_still_being_written_is_passed_over_for_the_whole_one(tmp_path, monkeypatch):
    root = f"{LOCAL}/Claude-Data/claude_abc123/claude-code"
    whole, partial = a_windows_pc(
        tmp_path, monkeypatch, f"{root}/2.1.295/0a1b2c/claude.exe", f"{root}/2.1.300/0a1b2c/claude.exe")
    (whole.parent / ".payload").write_text('{"size": 4}')
    (partial.parent / ".payload").write_text('{"size": 4000}')
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: None)
    assert setup_cli.find_claude() == str(whole)


def test_the_engine_offset_is_the_one_the_programs_use():
    from app import notice

    assert setup_cli.ENGINE_OFFSET == notice.OFFSET


def test_the_engine_address_is_the_pages_port_plus_the_offset_on_this_machine(tmp_path):
    assert setup_cli.Install(make_install(tmp_path)).engine_url == "http://127.0.0.1:8530"
    other = make_install(tmp_path / "other", RESUMEBOT_APP_URL="http://localhost:9000/")
    assert setup_cli.Install(other).engine_url == "http://127.0.0.1:9100"


def test_the_browser_door_is_checked_at_the_engines_address_and_the_owner_door_at_the_pages(
        tmp_path, monkeypatch):
    folder = make_install(tmp_path)
    inner = app_with_doors(tokens_of(folder))
    ports: list[tuple[str, int | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ports.append((request.url.path, request.url.port))
        return inner(request)

    serve_fake_app(monkeypatch, handler)
    install = setup_cli.Install(folder)
    for door in setup_cli.DOORS:
        assert setup_cli.check_door(install, door) is None
    assert set(ports) == {("/mcp-owner/", 8430), ("/mcp-browser/", 8530), ("/health", 8430),
                                  ("/health", 8530)}


def test_a_door_that_is_down_names_the_address_it_was_asked_at(tmp_path, monkeypatch):
    folder = make_install(tmp_path)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    serve_fake_app(monkeypatch, refuse)
    install = setup_cli.Install(folder)
    assert "http://127.0.0.1:8530" in setup_cli.check_door(install, setup_cli.DOORS[1])
    assert "http://127.0.0.1:8430" in setup_cli.check_door(install, setup_cli.DOORS[0])


# --- an update runs every verb again on an install that is already there ----------------------------------------------


def test_init_on_an_install_an_older_release_made_keeps_every_setting_secret_token_and_database_byte(
        tmp_path, capsys, monkeypatch):
    """The update flow runs `init` only when there are no settings, but a second run (a repeated flow, a half-finished
    update) must never touch what an old install left: settings, door secrets, the agents' token, the database."""
    folder = make_install(tmp_path)
    (folder / "secrets" / "owner-token").write_text("my-own-owner-token-" + "k" * 30, encoding="utf-8")
    (folder / "secrets" / setup_cli.TOKEN_FILE).write_text("a-token-the-first-flow-stored", encoding="utf-8")
    (folder / "resumebot.db").write_bytes(b"rows the user's work lives in")
    monkeypatch.setattr(setup_cli, "has_settings_row", lambda file: True)  # the owner saved settings long ago
    (folder / ".env").write_text((folder / ".env").read_text(encoding="utf-8") + "SCOUT_PORT=9000\n", encoding="utf-8")
    before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    assert {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before
    assert "kept (not touched)" in capsys.readouterr().out


def test_connect_run_twice_changes_nothing_the_second_time_and_says_already_registered(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    claude = FakeClaude()
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(setup_cli, "_run", claude)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 0
    first = len(claude.commands)
    assert setup_cli.connect(["--data", str(folder)]) == 0
    second = [c[2] for c in claude.commands[first:] if c[1] == "mcp"]
    assert second == ["get", "get"], "a door that is already right is only looked at: nothing is removed or added"
    out = capsys.readouterr().out
    assert out.count("already registered, and it answers") == 2 and "does NOT" not in out


def test_a_helper_command_opens_no_console_window_of_its_own(monkeypatch):
    # The desktop shortcut runs `open` under pythonw.exe, which has no console: a PowerShell call without this flag
    # would flash a window of its own.
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(setup_cli.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    setup_cli._run(["powershell", "-Command", "1"])
    assert seen["creationflags"] == 0x08000000


# --- hardening: child output, tokens in messages, proxies, quoting, one wrapper for every child ---------------------

RECORDED_CLAUDE_OUTPUT = RECORDED


def _child_printing(data: bytes):
    """A real child Python that writes exactly these bytes on stdout (what `_run` has to decode)."""
    code = "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))"
    return [sys.executable, "-c", code, data.hex()]


def test_run_decodes_bytes_the_code_page_leaves_undefined_without_raising():
    # 0x81 and 0x8d are undefined in cp1252: a strict decode in that code page raised ValueError mid-`connect`.
    done = setup_cli._run(_child_printing(b"before \x81\x8d\x8f\x90\x9d after"))
    assert done.stdout.startswith("before ") and done.stdout.endswith(" after")


def test_run_reads_the_recorded_status_line_of_claude_mcp_get_as_utf8():
    text = RECORDED_CLAUDE_OUTPUT.read_text(encoding="utf-8")
    assert "Status: \u2718" in text
    done = setup_cli._run(_child_printing(text.encode("utf-8")))
    assert "Status: \u2718 Failed to connect" in done.stdout


def test_a_timeout_message_from_connect_holds_no_token(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    tokens = tokens_of(folder)

    def hang(command):
        raise subprocess.TimeoutExpired(command, 30)  # str() of this holds the whole command line, token included

    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(setup_cli, "_run", hang)
    serve_fake_app(monkeypatch, app_with_doors(tokens))
    capsys.readouterr()
    assert setup_cli.connect(["--data", str(folder)]) == 1
    seen = capsys.readouterr()
    for token in tokens.values():
        assert token not in seen.out + seen.err
    assert "TimeoutExpired" in seen.err


def test_the_setup_http_client_ignores_proxy_variables():
    client = setup_cli._client()
    try:
        assert client.trust_env is False
    finally:
        client.close()


@pytest.mark.parametrize("quote", ["'", "\u2018", "\u2019", "\u201a", "\u201b"])
def test_quoted_doubles_every_character_powershell_reads_as_a_single_quote(quote):
    assert setup_cli._quoted(f"a{quote}b") == f"'a{quote}{quote}b'"


def test_powershell_scripts_start_by_setting_the_output_encoding_to_utf8(monkeypatch):
    seen = []

    def run(command):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(setup_cli, "_run", run)
    setup_cli._powershell("Get-Date")
    assert seen[0][-1].startswith("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)")


def test_main_never_ends_on_a_character_the_output_code_page_lacks(monkeypatch):
    import io

    from app import main_cli

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    main_cli.COMMANDS["__probe"] = lambda rest: print("path \u2718") or 0
    try:
        assert main_cli.main(["__probe"]) == 0
    finally:
        del main_cli.COMMANDS["__probe"]
    stream.flush()
    assert raw.getvalue().startswith(b"path ?")


def test_every_child_program_in_setup_cli_starts_through_the_quiet_wrapper():
    """Only `_quiet_run`, `_quiet_popen` and `_call` (the user's own console) may call into `subprocess`."""
    import ast

    source = Path(setup_cli.__file__).read_text(encoding="utf-8")
    allowed = {"_quiet_run", "_quiet_popen", "_call"}
    starters = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
    offenders = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (isinstance(node, ast.Attribute) and node.attr in starters and isinstance(node.value, ast.Name)
                    and node.value.id == "subprocess" and function.name not in allowed):
                offenders.append(f"{function.name}:{node.lineno}")
    assert offenders == []


# --- connect keeps a working door; the doctor tells the truth about it (setup hardening A2) ---------------------------


def a_claude_on_the_path(monkeypatch, claude):
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(setup_cli, "_run", claude)


def wanted_doors(folder):
    install = setup_cli.Install(folder)
    tokens = tokens_of(folder)
    return {d.name: (install.door_base(d) + d.path, tokens[d.name]) for d in setup_cli.DOORS}


def test_registration_is_read_from_the_recorded_output_of_claude_mcp_get(monkeypatch):
    monkeypatch.setattr(setup_cli, "_run", lambda command: subprocess.CompletedProcess(command, 0, recorded("get"), ""))
    got = setup_cli._registration("claude", setup_cli.DOORS[0])
    assert got == setup_cli.Registration("http://127.0.0.1:9/mcp/", "FAKE-TOKEN-0000",
                                         "User config (available in all your projects)")


def test_the_fake_claude_replays_the_recorded_exit_codes():
    claude = FakeClaude()
    add = ["claude", "mcp", "add", "--transport", "http", "--scope", "user", "n", "http://x/mcp/",
           "--header", "Authorization: Bearer t"]
    assert claude(add).returncode == 0
    assert claude(add).returncode == 1 and "already exists" in claude(add).stderr  # add of an existing name is 1
    assert claude(["claude", "mcp", "remove", "n", "--scope", "user"]).returncode == 0
    assert claude(["claude", "mcp", "remove", "n", "--scope", "user"]).returncode == 1  # remove of a missing name
    assert claude(["claude", "mcp", "get", "n"]).returncode == 1  # get of a missing name


def test_connect_with_a_door_already_right_changes_nothing_and_prints_no_token(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    claude = FakeClaude(doors=wanted_doors(folder))
    a_claude_on_the_path(monkeypatch, claude)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.connect(["--data", str(folder)]) == 0
    out = capsys.readouterr()
    assert [c[2] for c in claude.commands] == ["get", "get"]
    assert out.out.count("already registered, and it answers") == 2
    assert not any(token in out.out + out.err for token in tokens_of(folder).values())  # `mcp get` is never echoed
    assert "Bearer" not in out.out + out.err and "running:" not in out.out


@pytest.mark.parametrize("held", ["another-token", "another-address"])
def test_connect_repairs_a_door_with_a_wrong_token_or_address_by_adding_first_and_removing_only_on_the_conflict(
        held, tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    wanted = wanted_doors(folder)
    stale = {n: (u if held == "another-token" else "http://127.0.0.1:9999/mcp-owner/",
                 "another-token" if held == "another-token" else t) for n, (u, t) in wanted.items()}
    claude = FakeClaude(doors=stale)
    a_claude_on_the_path(monkeypatch, claude)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 0
    owner = [c[2] for c in claude.commands if "resumebot-owner" in c]
    assert owner == ["get", "add", "remove", "add", "get"]  # the add comes first; the name conflict is why it goes
    assert claude.doors == wanted
    assert not any(token in capsys.readouterr().out for token in tokens_of(folder).values())


def test_connect_puts_the_old_door_back_when_the_add_fails_after_the_remove(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    wanted = wanted_doors(folder)
    old = {n: (u, "an-old-token") for n, (u, _t) in wanted.items()}
    claude = FakeClaude(doors=old, fail_add_after_remove=True)
    a_claude_on_the_path(monkeypatch, claude)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 1
    assert claude.doors["resumebot-owner"] == old["resumebot-owner"], "the old door is back, as it was"
    err = capsys.readouterr().err
    assert "disk full" in err and "the old door was put back" in err
    assert "an-old-token" not in err


def test_connect_says_the_door_is_unregistered_when_it_cannot_be_put_back_either(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    old = {n: (u, "an-old-token") for n, (u, _t) in wanted_doors(folder).items()}
    claude = FakeClaude(doors=old, fail_every_add_after_remove=True)
    a_claude_on_the_path(monkeypatch, claude)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 1
    err = capsys.readouterr().err
    assert "the door is now unregistered" in err and "resumebot connect" in err
    assert "resumebot-owner" not in claude.doors


def test_connect_leaves_the_old_door_alone_when_claude_code_would_not_remove_it(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    old = {n: (u, "an-old-token") for n, (u, _t) in wanted_doors(folder).items()}
    claude = FakeClaude(doors=old)
    real = claude.__call__

    def no_remove(command):
        if command[2] == "remove":
            claude.commands.append(list(command))
            return subprocess.CompletedProcess(command, 1, "", "locked")
        return real(command)

    a_claude_on_the_path(monkeypatch, no_remove)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 1
    assert claude.doors == old
    assert "would not remove the old door: locked" in capsys.readouterr().err


def test_connect_reads_the_registration_back_and_fails_when_claude_code_holds_another_address(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    claude = FakeClaude()
    real = claude.__call__

    def local_scope_wins(command):
        done = real(command)
        if command[2] == "get" and done.returncode == 0:
            text = done.stdout.replace("User config (available in all your projects)", "Local config (private)")
            text = text.replace("/mcp-owner/", "/mcp-old/")
            return subprocess.CompletedProcess(command, 0, text, "")
        return done

    a_claude_on_the_path(monkeypatch, local_scope_wins)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 1
    err = capsys.readouterr().err
    assert "registered, but your Claude Code has it registered at" in err and "/mcp-old/" in err
    assert "in Local config (private), which wins over the user config" in err
    assert "claude mcp remove resumebot-owner --scope local" in err


def test_connect_exits_1_and_says_so_when_claude_code_cannot_be_asked_what_it_holds(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)

    def cannot(command):
        raise OSError(2, "No such file or directory")

    a_claude_on_the_path(monkeypatch, cannot)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 1
    assert "could not run Claude Code: FileNotFoundError: No such file or directory" in capsys.readouterr().err


def test_a_timeout_on_the_add_says_the_door_may_be_registered_and_holds_no_token(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    claude = FakeClaude()
    real = claude.__call__

    def hang_on_add(command):
        if command[2] == "add":
            raise subprocess.TimeoutExpired(command, 30)
        return real(command)

    a_claude_on_the_path(monkeypatch, hang_on_add)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.connect(["--data", str(folder)]) == 1
    seen = capsys.readouterr()
    assert "TimeoutExpired" in seen.err and "claude mcp list" in seen.err
    assert not any(token in seen.out + seen.err for token in tokens_of(folder).values())


def test_connect_names_the_claude_and_the_configuration_file_it_uses(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    a_claude_on_the_path(monkeypatch, FakeClaude())
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    assert setup_cli.connect(["--data", str(folder)]) == 0
    assert (f"using Claude Code at /bin/claude (its configuration: {tmp_path / 'claude-config' / '.claude.json'})"
            in capsys.readouterr().out)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert setup_cli.claude_config_file() == Path.home() / ".claude.json"


def test_connect_waits_for_an_engine_that_is_still_starting_then_words_it_as_not_up_yet(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    engine_port = setup_cli.Install(folder).engine_url.rsplit(":", 1)[1]
    inner = app_with_doors(tokens_of(folder))
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == int(engine_port):
            asked.append(request.url.path)
            raise httpx.ConnectError("refused")
        return inner(request)

    now = [0.0]
    monkeypatch.setattr(setup_cli, "_clock", lambda: now[0])
    monkeypatch.setattr(setup_cli, "_sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    a_claude_on_the_path(monkeypatch, FakeClaude())
    serve_fake_app(monkeypatch, handler)
    assert setup_cli.connect(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "resumebot-owner: registered, and it answers" in out
    assert "resumebot-browser: registered; the engine is not up yet" in out and "does NOT" not in out
    assert len(asked) > 5 and now[0] >= setup_cli.DOOR_WAIT_SECONDS  # it asked again for the time it waits


def test_connect_does_not_wait_when_the_engine_answers_with_the_wrong_token(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    now = [0.0]
    monkeypatch.setattr(setup_cli, "_clock", lambda: now[0])
    monkeypatch.setattr(setup_cli, "_sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    a_claude_on_the_path(monkeypatch, FakeClaude())
    serve_fake_app(monkeypatch, app_with_doors({d.name: "another-apps-token" for d in setup_cli.DOORS}))
    assert setup_cli.connect(["--data", str(folder)]) == 1
    assert now[0] == 0.0 and "refused the token (401)" in capsys.readouterr().out


# --- the health check says who answered ----------------------------------------------------------------------------


def health_says(monkeypatch, tmp_path, response):
    folder = make_install(tmp_path)
    serve_fake_app(monkeypatch, lambda request: response)
    return setup_cli.app_health(setup_cli.Install(folder))


def test_health_accepts_this_app_healthy_and_this_version(tmp_path, monkeypatch):
    assert health_says(monkeypatch, tmp_path, httpx.Response(200, json=HEALTH)) is None


@pytest.mark.parametrize("response, said", [
    (httpx.Response(200, text="<html>welcome</html>"), "something else answers"),
    (httpx.Response(200, json={"status": "ok"}), "something else answers"),
    (httpx.Response(200, json={"app": "another-app", "status": "ok", "db": "ok", "version": VERSION}),
     "something else answers"),
    (httpx.Response(200, json=["not", "an", "object"]), "something else answers"),
    (httpx.Response(404, text="not found"), "something else answers"),
    (httpx.Response(200, json={**HEALTH, "version": "0.0.1"}), "an older or different resumebot (0.0.1"),
    (httpx.Response(503, json={**HEALTH, "status": "starting"}), "is not healthy yet (status starting, db ok)"),
    (httpx.Response(503, json={**HEALTH, "status": "degraded", "db": "unreachable"}), "db unreachable"),
    (httpx.Response(200, json={**HEALTH, "db": "broken"}), "db broken"),
])
def test_health_names_what_is_wrong_when_something_else_or_an_older_app_answers(
        response, said, tmp_path, monkeypatch):
    assert said in health_says(monkeypatch, tmp_path, response)


def test_health_says_nothing_answers_when_the_connection_is_refused(tmp_path, monkeypatch):
    folder = make_install(tmp_path)

    def refuse(request):
        raise httpx.ConnectError("refused")

    serve_fake_app(monkeypatch, refuse)
    install = setup_cli.Install(folder)
    assert "nothing answers at http://127.0.0.1:8430" in setup_cli.app_health(install)


def test_the_app_name_here_is_the_one_the_app_says_in_health():
    from app.settings import APP_NAME

    assert setup_cli.APP_NAME == APP_NAME


def test_a_foreign_program_answering_200_is_not_given_the_door_token(tmp_path, monkeypatch):
    folder = make_install(tmp_path)
    seen = []

    def foreign(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="hello from something else")

    serve_fake_app(monkeypatch, foreign)
    problem = setup_cli.check_door(setup_cli.Install(folder), setup_cli.DOORS[0])
    assert "something else answers" in problem
    assert [r.url.path for r in seen] == ["/health"]  # the door was never asked
    assert all("authorization" not in r.headers for r in seen), "no token goes to a program not known to be the app"


def test_a_door_answering_404_is_told_apart_from_one_refusing_the_token(tmp_path, monkeypatch):
    folder = make_install(tmp_path)
    install = setup_cli.Install(folder)

    def answer(status):
        def handler(request):
            return httpx.Response(200, json=HEALTH) if request.url.path == "/health" else httpx.Response(status)

        return handler

    serve_fake_app(monkeypatch, answer(404))
    assert "is not there (404)" in setup_cli.check_door(install, setup_cli.DOORS[0])
    serve_fake_app(monkeypatch, answer(401))
    assert "refused the token (401)" in setup_cli.check_door(install, setup_cli.DOORS[0])


def test_the_engines_door_is_checked_against_the_engines_own_health(tmp_path, monkeypatch):
    folder = make_install(tmp_path)

    def handler(request):
        if request.url.port == 8530 and request.url.path == "/health":
            return httpx.Response(200, text="another program on the engine's port")
        return app_with_doors(tokens_of(folder))(request)

    serve_fake_app(monkeypatch, handler)
    install = setup_cli.Install(folder)
    assert setup_cli.check_door(install, setup_cli.DOORS[0]) is None
    assert "something else answers at http://127.0.0.1:8530" in setup_cli.check_door(install, setup_cli.DOORS[1])


# --- the doctor reads what Claude Code holds, and says which Claude and which configuration ---------------------


def doctor_out(tmp_path, monkeypatch, capsys, **programs):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch, **programs)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    code = setup_cli.doctor(["--data", str(folder)])
    return folder, code, capsys.readouterr().out


def test_doctor_counts_a_registration_it_could_not_read_as_missing(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch)
    real = setup_cli._run

    def unreadable(command):
        done = real(command)
        if command[1:3] == ["mcp", "get"]:
            return subprocess.CompletedProcess(command, 0, "something this version prints differently", "")
        return done

    monkeypatch.setattr(setup_cli, "_run", unreadable)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert out.count("could not read what your Claude Code holds") == 2 and "ok       resumebot-owner" not in out


def test_doctor_says_when_the_token_claude_code_holds_is_not_this_installs(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    claude = FakeClaude(doors={n: (u, "a-stale-token") for n, (u, _t) in wanted_doors(folder).items()})
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: f"/bin/{name}")
    real = fake_run_for_doctor(claude)
    monkeypatch.setattr(setup_cli, "_run", real)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "MISSING  resumebot-owner: your Claude Code holds a token for it that is not this install's: run" in out
    assert "a-stale-token" not in out and not any(token in out for token in tokens_of(folder).values())


def fake_run_for_doctor(claude):
    def run(command):
        if command[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0].endswith("node"):
            return subprocess.CompletedProcess(command, 0, "v22.4.0" + chr(10), "")
        return claude(command)

    return run


def test_doctor_accepts_a_door_whose_registration_matches_url_and_token(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(setup_cli, "_run", fake_run_for_doctor(FakeClaude(doors=wanted_doors(folder))))
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    assert capsys.readouterr().out.count("answers and is registered") == 2


def test_doctor_names_the_claude_it_uses_and_the_configuration_it_reads(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    _folder, code, out = doctor_out(tmp_path, monkeypatch, capsys)
    assert code == 0
    assert "ok       Claude Code is installed (/bin/claude)" in out
    assert f"config   Claude Code's configuration is read from {tmp_path / 'claude-config' / '.claude.json'}" in out


def test_doctor_tells_a_claude_that_would_not_run_from_one_that_is_not_signed_in(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch)
    monkeypatch.setattr(setup_cli, "_claude_runs", lambda program: False)
    monkeypatch.setattr(setup_cli, "_claude_candidates", lambda: ["/dangling/claude"])
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "MISSING  Claude Code: found at /dangling/claude, but it would not run" in out
    assert "not signed in" not in out


def test_doctor_says_could_not_run_when_asking_claude_for_its_sign_in_fails(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch)
    real = setup_cli._run

    def broken(command):
        if command[1:3] == ["auth", "status"]:
            raise subprocess.TimeoutExpired(command, 30)
        return real(command)

    monkeypatch.setattr(setup_cli, "_run", broken)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "MISSING  Claude Code sign-in: could not run `claude auth status`: TimeoutExpired" in out


def test_find_claude_skips_a_program_that_does_not_run_and_takes_the_next_one(tmp_path, monkeypatch):
    working = tmp_path / "bin" / "claude.exe"
    working.parent.mkdir()
    working.write_text("fake")
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/dangling/claude")
    monkeypatch.setattr(setup_cli, "_claude_locations", lambda: [working])
    monkeypatch.setattr(setup_cli, "_claude_runs", lambda program: program == str(working))
    assert setup_cli.find_claude() == str(working)
    monkeypatch.setattr(setup_cli, "_claude_runs", lambda program: False)
    assert setup_cli.find_claude() is None


def test_the_claude_probe_asks_for_the_version_quietly_and_a_failure_means_it_does_not_run(monkeypatch):
    seen = []

    def quiet(command, **kwargs):
        seen.append((list(command), kwargs))
        return subprocess.CompletedProcess(command, 0, "1.0 (Claude Code)", "")

    monkeypatch.setattr(setup_cli, "_quiet_run", quiet)
    assert REAL_CLAUDE_RUNS("claude") is True
    assert seen == [(["claude", "--version"], {"timeout": setup_cli.CLAUDE_PROBE_SECONDS})]

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(setup_cli, "_quiet_run", timeout)
    assert REAL_CLAUDE_RUNS("claude") is False
    monkeypatch.setattr(setup_cli, "_quiet_run", lambda command, **kw: subprocess.CompletedProcess(command, 1, "", ""))
    assert REAL_CLAUDE_RUNS("claude") is False


# --- the shortcut and the old autostart -----------------------------------------------------------------------


def test_the_old_autostart_is_compared_by_real_path_not_by_spelling(tmp_path):
    data = tmp_path / "Data"
    data.mkdir()
    spelled = str(tmp_path / "other" / ".." / "Data")
    assert setup_cli._names_data(f'x --data "{spelled}" start', data)
    assert not setup_cli._names_data(f'x --data "{tmp_path / "Elsewhere"}" start', data)
    assert not setup_cli._names_data("no data option here", data)


def test_shortcut_says_when_the_old_logon_task_could_not_be_removed_and_does_not_claim_it_was(
        tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    shell = FakePowerShell(task=f'"s" --data "{folder}" start', keep_task=True)
    on_windows(monkeypatch, shell)
    assert setup_cli.shortcut(["--data", str(folder)]) == 0
    captured = capsys.readouterr()
    assert "could not remove the old logon task resumebot" in captured.err
    assert "removed the old start" not in captured.out


def shortcut_answer(monkeypatch, stdout, returncode=0):
    class Shell:
        def __call__(self, command):
            return subprocess.CompletedProcess(command, returncode, stdout, "")

    monkeypatch.setattr(setup_cli, "_run", Shell())


def test_doctor_checks_the_desktop_shortcut_exists_its_program_exists_and_names_this_data_folder(
        tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cli, "_shortcut_problem", REAL_SHORTCUT_PROBLEM)
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")  # the .lnk, read through PowerShell
    folder = make_install(tmp_path)
    install = setup_cli.Install(folder)
    program = tmp_path / "pythonw.exe"
    program.write_text("fake")
    tab = chr(9)
    shortcut_answer(monkeypatch, "NOLINK" + chr(10))
    assert "there is none on the desktop" in setup_cli._shortcut_problem(install)
    shortcut_answer(monkeypatch, f"LINK {tmp_path / 'gone.exe'}{tab}-m app.main_cli open --data \"{folder}\"")
    assert "which is not there" in setup_cli._shortcut_problem(install)
    shortcut_answer(monkeypatch, f"LINK {program}{tab}-m app.main_cli open --data \"{tmp_path / 'other'}\"")
    assert "opens another data folder" in setup_cli._shortcut_problem(install)
    shortcut_answer(monkeypatch, f"LINK {program}{tab}-m app.main_cli open --data \"{folder}\"")
    assert setup_cli._shortcut_problem(install) is None


def test_doctor_has_a_line_for_the_shortcut_on_windows_only(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors(tokens_of(folder)))
    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli, "_shortcut_problem", lambda install: "there is none on the desktop: run it")
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    assert "MISSING  desktop shortcut: there is none on the desktop" in capsys.readouterr().out
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    assert setup_cli.doctor(["--data", str(folder)]) == 0
    assert "desktop shortcut" not in capsys.readouterr().out


# --- init finishes what a stopped run left, and trusts only what works (plan lane C) ---------------------------------


def init_args(folder, *more):
    return ["--owner", OWNER, "--timezone", ZONE, "--data", str(folder), *more]


def test_a_complete_install_is_complete_and_names_nothing_missing(tmp_path):
    folder = make_install(tmp_path)
    assert setup_cli.Install(folder).missing_pieces() == []


def test_init_that_stopped_on_a_secret_leaves_no_settings_file_and_the_next_run_finishes(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "data"
    real = setup_cli._store_token
    monkeypatch.setattr(setup_cli, "_store_token", lambda *a, **k: "could not store the secret")
    assert setup_cli.init(init_args(folder)) == 1  # stopped on the first secret; the database is made
    monkeypatch.setattr(setup_cli, "_store_token", real)
    assert not (folder / ".env").exists()  # the settings file goes last: nothing mistakes this for a finished install
    assert setup_cli.Install(folder).is_set_up is False
    assert setup_cli.init(init_args(folder)) == 0
    assert setup_cli.Install(folder).missing_pieces() == []
    assert "database: kept (not touched)" in capsys.readouterr().out


def test_init_that_stopped_after_the_settings_file_finishes_the_blank_and_missing_secrets(tmp_path, capsys):
    folder = make_install(tmp_path)
    (folder / "secrets" / "browser-token").unlink()
    (folder / "secrets" / "wake-secret").write_text("", encoding="utf-8")  # half-written
    (folder / "secrets" / "owner-token").write_text("cut", encoding="utf-8")
    kept = (folder / "secrets" / "agent-token").read_text(encoding="utf-8")
    assert setup_cli.Install(folder).missing_pieces() == [
        "the secret wake-secret", "the secret owner-token", "the secret browser-token"]
    capsys.readouterr()
    assert setup_cli.init(init_args(folder)) == 0
    out = capsys.readouterr().out
    assert "secrets: 3 made, 1 kept" in out and "restart the app" in out
    assert (folder / "secrets" / "agent-token").read_text(encoding="utf-8") == kept
    assert setup_cli.Install(folder).missing_pieces() == []


def test_init_with_a_settings_file_and_no_database_makes_the_database_and_seeds_it(tmp_path, capsys):
    folder = make_install(tmp_path)
    (folder / setup_cli.DATABASE_FILE).unlink()
    assert setup_cli.Install(folder).missing_pieces() == [f"the database file {folder / setup_cli.DATABASE_FILE}"]
    assert setup_cli.init(init_args(folder)) == 0
    assert setup_cli.Install(folder).missing_pieces() == []
    assert "database: made" in capsys.readouterr().out


def test_a_failed_seed_is_said_in_one_line_and_the_next_run_seeds_the_database_that_was_made(
        tmp_path, monkeypatch, capsys):
    folder = tmp_path / "data"
    real = setup_cli.seed_starter_settings

    def fails(url, owner):
        raise RuntimeError("disk said no")

    monkeypatch.setattr(setup_cli, "seed_starter_settings", fails)
    assert setup_cli.init(init_args(folder)) == 1
    err = capsys.readouterr().err
    assert "could not save the starter settings" in err and "disk said no" in err and "Traceback" not in err
    assert (folder / setup_cli.DATABASE_FILE).exists() and not (folder / ".env").exists()
    monkeypatch.setattr(setup_cli, "seed_starter_settings", real)
    assert setup_cli.init(init_args(folder)) == 0
    assert setup_cli.has_settings_row(folder / setup_cli.DATABASE_FILE)


def test_doctor_and_status_name_each_missing_piece_of_a_half_finished_install(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    (folder / "secrets" / "browser-token").unlink()
    (folder / setup_cli.DATABASE_FILE).unlink()
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors({d.name: "x" for d in setup_cli.DOORS}))
    capsys.readouterr()
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "MISSING  install: the database file" in out and "MISSING  install: the secret browser-token" in out
    assert "settings are at" not in out
    assert setup_cli.status(["--for-claude", "--data", str(folder)]) == 0
    said = capsys.readouterr().out
    assert "not finished: missing the database file" in said and "the secret browser-token" in said
    assert "next: run `resumebot init` again" in said


def test_init_writes_each_secret_through_the_limiting_step_before_it_holds_a_value(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(setup_cli, "_only_me_file", lambda path: seen.append((path.name, path.stat().st_size)))
    assert setup_cli.init(init_args(tmp_path / "data")) == 0
    assert seen == [(name + ".tmp", 0) for name in setup_cli.SECRET_FILES]


def test_init_stops_when_a_secret_cannot_be_limited_to_this_user(tmp_path, monkeypatch, capsys):
    def refuse_limit(path):
        raise OSError("no icacls")

    monkeypatch.setattr(setup_cli, "_only_me_file", refuse_limit)
    folder = tmp_path / "data"
    assert setup_cli.init(init_args(folder)) == 1
    assert "could not limit" in capsys.readouterr().err
    assert not list((folder / "secrets").glob("*"))


def test_a_utf16_settings_file_gives_one_sentence_not_a_traceback(tmp_path, monkeypatch, capsys):
    folder = make_install(tmp_path)
    (folder / ".env").write_bytes(b"\xff\xfe" + (folder / ".env").read_text(encoding="utf-8").encode("utf-16-le"))
    assert setup_cli.init(init_args(folder)) == 2
    err = capsys.readouterr().err
    assert "not saved as UTF-8" in err and "Traceback" not in err
    fake_programs(monkeypatch)
    serve_fake_app(monkeypatch, app_with_doors({d.name: "x" for d in setup_cli.DOORS}))
    assert setup_cli.doctor(["--data", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "MISSING  settings: " in out and "not saved as UTF-8" in out
    assert setup_cli.status(["--for-claude", "--data", str(folder)]) == 0
    assert "not saved as UTF-8" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["a #b", "x${y}"])
def test_init_refuses_a_data_folder_the_settings_file_could_not_hold(tmp_path, capsys, name):
    assert setup_cli.init(init_args(tmp_path / name)) == 2
    assert "choose another folder" in capsys.readouterr().err
    assert not (tmp_path / name).exists()


def test_a_relative_data_folder_becomes_an_absolute_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert setup_cli.data_folder("here") == (tmp_path / "here").resolve()
    assert setup_cli.data_folder("here").is_absolute()


def test_init_port_writes_the_page_address_and_merges_it_into_an_install_that_has_none(tmp_path, capsys):
    folder = tmp_path / "data"
    assert setup_cli.init(init_args(folder, "--port", "8460")) == 0
    assert setup_cli.read_env(folder / ".env")["RESUMEBOT_APP_URL"] == "http://127.0.0.1:8460"
    other = make_install(tmp_path / "second")
    before = (other / ".env").read_text(encoding="utf-8")
    assert setup_cli.init(init_args(other, "--port", "8470")) == 0
    after = (other / ".env").read_text(encoding="utf-8")
    assert after.startswith(before) and "RESUMEBOT_APP_URL=http://127.0.0.1:8470" in after


def test_init_port_keeps_an_address_the_settings_already_name_and_says_so(tmp_path, capsys):
    folder = make_install(tmp_path, RESUMEBOT_APP_URL="http://127.0.0.1:8500")
    capsys.readouterr()
    assert setup_cli.init(init_args(folder, "--port", "8460")) == 0
    assert "was not applied" in capsys.readouterr().err
    assert setup_cli.read_env(folder / ".env")["RESUMEBOT_APP_URL"] == "http://127.0.0.1:8500"


@pytest.mark.parametrize("port,env", [
    (80, ""), (65500, ""), (8651, ""), (8551, ""), (8655, ""), (8100, "SCOUT_PORT=8200\n"),
    (8200, "SCOUT_PORT=8200\n")])
def test_init_port_refuses_a_port_the_page_the_engine_or_the_lanes_would_share(tmp_path, capsys, port, env):
    folder = tmp_path / "data"
    if env:
        folder.mkdir()
        (folder / ".env").write_text(
            f"JOBSEARCH_OWNER={OWNER}\nJOBSEARCH_TIMEZONE={ZONE}\n" + env, encoding="utf-8")
    assert setup_cli.init(init_args(folder, "--port", str(port))) == 2
    assert capsys.readouterr().err


def test_the_lane_numbers_init_checks_are_the_ones_stack_py_runs():
    import importlib.util

    path = Path(__file__).parents[3] / "scripts" / "local" / "stack.py"
    spec = importlib.util.spec_from_file_location("stack_for_ports", path)
    stack = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stack)
    assert (setup_cli.LANE_COUNT, setup_cli.DEFAULT_SCOUT_PORT) == (stack.LANE_COUNT, stack.DEFAULT_SCOUT_PORT)


def test_a_database_file_that_is_no_database_is_named_missing_and_never_seeded_or_touched(tmp_path, capsys):
    folder = make_install(tmp_path)
    (folder / setup_cli.DATABASE_FILE).write_bytes(b"not a database at all, just text")
    assert setup_cli.has_settings_row(folder / setup_cli.DATABASE_FILE) is None
    assert any("can be read" in piece for piece in setup_cli.Install(folder).missing_pieces())
    assert setup_cli.init(init_args(folder)) == 0
    assert (folder / setup_cli.DATABASE_FILE).read_bytes() == b"not a database at all, just text"


def test_connect_shows_no_part_of_the_token_in_the_command_it_runs(monkeypatch, capsys):
    token = "sk-ant-oat01-" + "x" * 40 + "TAIL"

    def refuse(command, *args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr(setup_cli, "_run", refuse)
    said = setup_cli._register("claude", setup_cli.DOORS[0], "http://127.0.0.1:8430/mcp-owner/", token, None)
    out = capsys.readouterr().out
    assert said is not None and "Bearer <hidden>" in out
    assert "TAIL" not in out and "xxxx" not in out


def test_claude_mcp_runs_in_a_folder_of_no_project_and_other_children_where_they_are(monkeypatch):
    # A project's .mcp.json (or its local config) holding a door of the same name wins `claude mcp get` run inside it,
    # and connect would register again a door that already works.
    ran = []
    monkeypatch.setattr(setup_cli, "_quiet_run", lambda command, **kwargs: ran.append((list(command), kwargs)))
    setup_cli._run(["claude", "mcp", "get", "resumebot-owner"])
    setup_cli._run(["claude", "--version"])
    assert ran[0][1] == {"cwd": setup_cli.tempfile.gettempdir()}
    assert ran[1][1] == {}


def test_doctor_names_the_start_log_not_another_log_that_starts_with_start(tmp_path):
    (tmp_path / "start.log").write_text("x", encoding="utf-8")
    other = tmp_path / "start-app.log"
    other.write_text("x", encoding="utf-8")
    os.utime(other, (time.time() + 60, time.time() + 60))
    assert "start: start.log" in setup_cli.logs_line(tmp_path)
