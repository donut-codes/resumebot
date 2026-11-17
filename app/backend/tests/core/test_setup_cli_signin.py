"""`resumebot signin` (app/setup_cli.py): it runs `claude setup-token` on a pseudo-terminal out of sight, takes the
token off its screen and stores it for the agents' file only. The pseudo-terminal is a fake child here."""

import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import main_cli, setup_cli

REAL_ONLY_ME_FILE = setup_cli._only_me_file
TOKEN = "sk-ant-oat01-" + "Ab1_-" * 20
CODE = "abc123XYZ" + "q" * 20 + "#state987"
ADDRESS = "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz"
FORWARD = "\x1b[1C"  # Ink writes a space between words as a cursor move
URL_SCREEN = f"Browser{FORWARD}didn't{FORWARD}open?{FORWARD}Use{FORWARD}the{FORWARD}url{FORWARD}below\r\n{ADDRESS}\r\n"
PROMPT = f"Paste{FORWARD}code{FORWARD}here{FORWARD}if{FORWARD}prompted{FORWARD}>"
TOKEN_SCREEN = ("\x1b[32m✓\x1b[39m Long-lived authentication token created successfully!\r\n"
                f"Your{FORWARD}OAuth{FORWARD}token{FORWARD}(valid{FORWARD}for{FORWARD}1{FORWARD}year):\r\n{TOKEN}"
                f"{FORWARD}Store{FORWARD}this{FORWARD}token{FORWARD}somewhere{FORWARD}safe\r\n")


class Clock:
    """An injected clock: sleeping moves it, nothing waits."""

    def __init__(self):
        self.now = 0.0
        self.hooks = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for hook in list(self.hooks):
            hook(self.now)


class FakeChild:
    """What `_spawn_pty` gives back. `script` is what the child prints, one chunk per read; `after_code` is what it
    prints once a code has been typed and Enter pressed; `ends` says whether it exits when it has printed everything."""

    def __init__(self, script=(), after_code=(), ends=False, ends_after_code=False):
        self.script, self.after_code = list(script), list(after_code)
        self.ends, self.ends_after_code = ends, ends_after_code
        self.written, self.closed = [], False

    def read(self):
        return self.script.pop(0) if self.script else ""

    def write(self, text):
        self.written.append(text)
        if text == "\r":
            self.script.extend(self.after_code)
            self.ends = self.ends or self.ends_after_code

    def alive(self):
        return not (self.ends and not self.script) and not self.closed

    def close(self):
        self.closed = True


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(setup_cli, "_clock", fake)
    monkeypatch.setattr(setup_cli, "_sleep", fake.sleep)
    return fake


@pytest.fixture
def data(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / ".env").write_text("JOBSEARCH_OWNER=Test\n", encoding="utf-8")
    return folder


@pytest.fixture
def target(data):
    return data / "secrets" / setup_cli.TOKEN_FILE


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """No terminal, `claude` found, no real pseudo-terminal, and the file-limiting step recorded."""
    monkeypatch.setattr(setup_cli, "_has_terminal", lambda: False)
    monkeypatch.setattr(setup_cli, "find_claude", lambda: "claude")
    monkeypatch.setattr(setup_cli, "_pid_alive", lambda pid: pid == LIVE_PID)  # only the made-up live one runs
    monkeypatch.setattr(setup_cli, "_ask_for_code", lambda: pytest.fail("a terminal that was not there"))
    monkeypatch.setattr(setup_cli, "_spawn_pty", lambda argv, env: pytest.fail("a child nobody set up"))
    box = SimpleNamespace(limited=[], spawned=[])
    monkeypatch.setattr(setup_cli, "_only_me_file", lambda path: box.limited.append((path.name, path.stat().st_size)))
    return box


def child_is(monkeypatch, env, child):
    def spawn(argv, environment):
        env.spawned.append((list(argv), dict(environment)))
        return child

    monkeypatch.setattr(setup_cli, "_spawn_pty", spawn)
    return child


def run(data, *argv):
    return setup_cli.signin(["--data", str(data), *argv])


def secrets_of(data):
    return data / "secrets"


LIVE_PID = 4242


def waiting_signin(data, pid=LIVE_PID):
    """What a running `signin` leaves: `waiting` and its process id. Returns the status file."""
    secrets_of(data).mkdir(exist_ok=True)
    (secrets_of(data) / setup_cli.SIGNIN_PID_FILE).write_text(str(pid), encoding="utf-8")
    status = secrets_of(data) / setup_cli.SIGNIN_STATUS_FILE
    status.write_text(setup_cli.SIGNIN_WAITING, encoding="utf-8")
    return status


def everything_written(data):
    return "".join(p.read_text(encoding="utf-8", errors="replace") for p in secrets_of(data).rglob("*") if p.is_file())


# --- the screen -------------------------------------------------------------------------------------------------------


def test_cursor_moves_become_the_spaces_they_stand_for():
    assert setup_cli.screen_text(f"one{FORWARD}two\x1b[3Gthree\x1b[1Bfour\x1b[0mfive") == "one two three\nfourfive"


def test_the_token_is_cut_at_the_first_character_that_is_not_part_of_it_even_when_words_follow_it():
    text = setup_cli.screen_text(TOKEN_SCREEN)
    assert setup_cli.find_token(text) == TOKEN


def test_a_token_still_being_printed_is_not_taken_until_something_follows_it_or_the_output_ends():
    assert setup_cli.find_token(f"token:\n{TOKEN}") is None
    assert setup_cli.find_token(f"token:\n{TOKEN}", ended=True) == TOKEN


def test_a_short_run_after_the_prefix_is_not_a_token():
    assert setup_cli.find_token("use sk-ant-xxx for the key\n") is None


def test_the_authorize_address_is_found_whole_and_not_before_it_is_followed_by_something():
    assert setup_cli.find_address(setup_cli.screen_text(URL_SCREEN)) == ADDRESS
    assert setup_cli.find_address(f"open {ADDRESS}") is None


# --- the flow -----------------------------------------------------------------------------------------------------


def test_the_stored_token_is_exactly_the_token_and_it_appears_nowhere_else(
        data, target, env, monkeypatch, clock, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    child_is(monkeypatch, env, FakeChild([URL_SCREEN, PROMPT, "\x1b[2K", TOKEN_SCREEN]))
    assert run(data) == 0
    assert target.read_text(encoding="utf-8") == TOKEN
    shown = capsys.readouterr()
    assert shown.out.splitlines() == [
        f"approve the sign-in in the browser tab that opened; if none opened, open: {ADDRESS}", "signed in"]
    assert TOKEN not in shown.out + shown.err + caplog.text
    assert TOKEN not in (secrets_of(data) / setup_cli.SIGNIN_STATUS_FILE).read_text(encoding="utf-8")
    assert (secrets_of(data) / setup_cli.SIGNIN_STATUS_FILE).read_text(encoding="utf-8") == "signed in"
    assert env.limited == [(setup_cli.TOKEN_FILE + ".tmp", 0)]
    assert [p.name for p in secrets_of(data).iterdir() if p.name != setup_cli.SIGNIN_STATUS_FILE] == [
        setup_cli.TOKEN_FILE]


def test_the_child_is_setup_token_with_this_installs_environment_and_is_stopped_at_the_end(
        data, env, monkeypatch, clock):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "C:\\somewhere\\own-profile")
    child = child_is(monkeypatch, env, FakeChild([URL_SCREEN, TOKEN_SCREEN]))
    assert run(data) == 0
    (argv, environment), = env.spawned
    assert argv == ["claude", "setup-token"]
    assert environment["CLAUDE_CONFIG_DIR"] == "C:\\somewhere\\own-profile"
    assert child.closed


def test_a_token_split_over_two_reads_is_stored_whole(data, target, env, monkeypatch, clock):
    head, tail = TOKEN_SCREEN.split(TOKEN[40:], 1)
    child_is(monkeypatch, env, FakeChild([URL_SCREEN, head + TOKEN[40:60], TOKEN[60:] + tail]))
    assert run(data) == 0
    assert target.read_text(encoding="utf-8") == TOKEN


def test_signin_again_replaces_the_token_and_leaves_no_temp_file(data, target, env, monkeypatch, clock):
    child_is(monkeypatch, env, FakeChild([TOKEN_SCREEN]))
    assert run(data) == 0
    other = "sk-ant-oat01-" + "Zz9" * 30
    child_is(monkeypatch, env, FakeChild([TOKEN_SCREEN.replace(TOKEN, other)]))
    assert run(data, "--again") == 0
    assert target.read_text(encoding="utf-8") == other
    assert not list(secrets_of(data).glob("*.tmp"))


def test_signin_with_a_token_already_stored_says_so_and_changes_nothing(data, target, env, capsys):
    """An update runs signin on an install that is signed in already (a token the first flow stored is the same
    file): it must not open a browser tab, start a child or touch the file (docs/PRD.md path 17, the update)."""
    target.parent.mkdir()
    target.write_text(TOKEN, encoding="utf-8")
    before = target.stat().st_mtime_ns
    assert run(data) == 0  # the env fixture fails the test if a child is spawned
    shown = capsys.readouterr()
    assert "already signed in" in shown.out and "--again" in shown.out
    assert target.read_text(encoding="utf-8") == TOKEN
    assert target.stat().st_mtime_ns == before
    assert env.limited == []


def test_signin_with_an_empty_token_file_signs_in(data, target, env, monkeypatch, clock):
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")
    child_is(monkeypatch, env, FakeChild([TOKEN_SCREEN]))
    assert run(data) == 0
    assert target.read_text(encoding="utf-8") == TOKEN


def test_signin_again_signs_in_when_no_token_is_stored_too(data, target, env, monkeypatch, clock):
    child_is(monkeypatch, env, FakeChild([TOKEN_SCREEN]))
    assert run(data, "--again") == 0
    assert target.read_text(encoding="utf-8") == TOKEN


def test_a_code_left_by_signin_code_is_typed_at_the_prompt_and_its_file_is_removed(
        data, target, env, monkeypatch, clock):
    secrets_of(data).mkdir()
    child = child_is(monkeypatch, env, FakeChild([URL_SCREEN, PROMPT], after_code=[TOKEN_SCREEN]))

    def the_second_call_leaves_its_code(now):
        code_file = secrets_of(data) / setup_cli.SIGNIN_CODE_FILE
        if not code_file.exists() and not child.written:
            code_file.write_text(CODE, encoding="utf-8")

    clock.hooks.append(the_second_call_leaves_its_code)
    assert run(data) == 0
    assert child.written == [CODE, "\r"]
    assert target.read_text(encoding="utf-8") == TOKEN
    assert not (secrets_of(data) / setup_cli.SIGNIN_CODE_FILE).exists()
    assert CODE not in everything_written(data)


def test_a_code_is_not_typed_before_the_child_asks_for_one(data, env, monkeypatch, clock):
    secrets_of(data).mkdir()
    child = child_is(monkeypatch, env, FakeChild([URL_SCREEN], after_code=[TOKEN_SCREEN]))
    clock.hooks.append(lambda now: (secrets_of(data) / setup_cli.SIGNIN_CODE_FILE).write_text(CODE, encoding="utf-8"))
    assert run(data) == 1  # the prompt never came: it waits out the ten minutes
    assert child.written == []


def test_a_code_left_from_an_earlier_run_is_removed_and_never_typed(data, env, monkeypatch, clock):
    secrets_of(data).mkdir()
    (secrets_of(data) / setup_cli.SIGNIN_CODE_FILE).write_text(CODE, encoding="utf-8")
    child = child_is(monkeypatch, env, FakeChild([URL_SCREEN, PROMPT]))
    assert run(data) == 1
    assert child.written == [] and not (secrets_of(data) / setup_cli.SIGNIN_CODE_FILE).exists()


def test_a_code_pasted_in_a_real_terminal_is_typed_at_the_prompt(data, target, env, monkeypatch, clock):
    child = child_is(monkeypatch, env, FakeChild([URL_SCREEN, PROMPT], after_code=[TOKEN_SCREEN]))
    monkeypatch.setattr(setup_cli, "_has_terminal", lambda: True)
    answers = [CODE]

    def ask():
        if not answers:
            raise EOFError
        return answers.pop()

    monkeypatch.setattr(setup_cli, "_ask_for_code", ask)

    class Inline:  # the listening thread, run right here so the test does not depend on thread timing
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(setup_cli.threading, "Thread", Inline)
    assert run(data) == 0
    assert child.written == [CODE, "\r"]
    assert target.read_text(encoding="utf-8") == TOKEN


def test_a_wrong_code_stores_nothing_and_says_so(data, target, env, monkeypatch, clock, capsys):
    secrets_of(data).mkdir()
    target.write_text("kept", encoding="utf-8")
    child = child_is(monkeypatch, env, FakeChild([URL_SCREEN, PROMPT], after_code=["Invalid\x1b[1Ccode\r\n"],
                                                 ends_after_code=True))
    clock.hooks.append(lambda now: (secrets_of(data) / setup_cli.SIGNIN_CODE_FILE).write_text(CODE, encoding="utf-8")
                       if not child.written else None)
    assert run(data, "--again") == 1
    assert target.read_text(encoding="utf-8") == "kept"
    shown = capsys.readouterr()
    assert "that code was not accepted: nothing was stored" in shown.err
    assert CODE not in shown.out + shown.err + everything_written(data)


def test_a_child_that_ends_without_a_token_stores_nothing(data, target, env, monkeypatch, clock, capsys):
    child_is(monkeypatch, env, FakeChild([URL_SCREEN, "Error:\x1b[1Cno\x1b[1Cbrowser\r\n"], ends=True))
    assert run(data) == 1
    assert "ended without a token: nothing was stored" in capsys.readouterr().err
    assert not target.exists()


def test_nobody_approving_times_out_after_ten_minutes_and_stores_nothing(data, target, env, monkeypatch, clock, capsys):
    child = child_is(monkeypatch, env, FakeChild([URL_SCREEN, PROMPT]))
    assert run(data) == 1
    assert "timed out after 10 minutes: nothing was stored" in capsys.readouterr().err
    assert 600 <= clock.now < 601 and not target.exists() and child.closed


def test_a_child_that_cannot_start_is_one_plain_sentence_with_no_detail_from_it(data, env, monkeypatch, capsys):
    def spawn(argv, environment):
        raise OSError(TOKEN)

    monkeypatch.setattr(setup_cli, "_spawn_pty", spawn)
    assert run(data) == 1
    shown = capsys.readouterr()
    assert shown.err.strip() == "could not start `claude setup-token`: OSError" and TOKEN not in shown.err


def test_signin_says_so_when_claude_is_not_found(data, monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "find_claude", lambda: None)
    assert run(data) == 1
    assert setup_cli.CLAUDE_NOT_FOUND in capsys.readouterr().err


def test_signin_before_init_refuses_and_makes_nothing(tmp_path, capsys):
    folder = tmp_path / "nothing"
    assert run(folder) == 1
    assert "run `resumebot init` first" in capsys.readouterr().err
    assert not folder.exists()


def test_signin_removes_the_temp_file_and_keeps_the_old_token_when_limiting_it_fails(
        data, target, env, monkeypatch, clock, capsys):
    target.parent.mkdir()
    target.write_text("kept", encoding="utf-8")

    def fail(path):
        raise OSError(TOKEN)

    monkeypatch.setattr(setup_cli, "_only_me_file", fail)
    child_is(monkeypatch, env, FakeChild([TOKEN_SCREEN]))
    assert run(data, "--again") == 1
    assert target.read_text(encoding="utf-8") == "kept"
    assert not list(secrets_of(data).glob("*.tmp"))
    shown = capsys.readouterr()
    assert f"could not limit {target} to this user" in shown.err
    assert TOKEN not in shown.out + shown.err + everything_written(data).replace("kept", "")


def test_signin_keeps_the_old_token_when_the_replace_fails_and_prints_no_token(
        data, target, env, monkeypatch, clock, capsys):
    target.parent.mkdir()
    target.write_text("kept", encoding="utf-8")

    def fail(source, destination):
        raise OSError("x")

    monkeypatch.setattr(setup_cli.os, "replace", fail)
    child_is(monkeypatch, env, FakeChild([TOKEN_SCREEN]))
    assert run(data, "--again") == 1
    assert target.read_text(encoding="utf-8") == "kept"
    assert not list(secrets_of(data).glob("*.tmp"))
    shown = capsys.readouterr()
    assert TOKEN not in shown.out + shown.err


@pytest.mark.parametrize("argv", [["--token", "x"], ["x"]])
def test_signin_takes_no_token_on_the_command_line(data, argv):
    with pytest.raises(SystemExit) as stopped:
        setup_cli.signin(["--data", str(data), *argv])
    assert stopped.value.code == 2


def test_signin_is_in_the_command_table():
    assert main_cli.COMMANDS["signin"] is setup_cli.signin


# --- signin --code ------------------------------------------------------------------------------------------------


def test_signin_code_hands_the_code_over_in_a_private_file_and_prints_signed_in_when_the_waiting_signin_is_done(
        data, env, clock, capsys):
    status = waiting_signin(data)
    seen = []

    def the_waiting_signin_answers(now):
        code_file = secrets_of(data) / setup_cli.SIGNIN_CODE_FILE
        seen.append(code_file.read_text(encoding="utf-8"))
        code_file.unlink()
        status.write_text("signed in", encoding="utf-8")

    clock.hooks.append(the_waiting_signin_answers)
    assert run(data, "--code", f"  {CODE}\n") == 0
    assert seen == [CODE]
    assert capsys.readouterr().out == "signed in\n"
    assert env.limited == [(setup_cli.SIGNIN_CODE_FILE + ".tmp", 0)]


def test_signin_code_says_why_when_the_waiting_signin_failed(data, clock, capsys):
    status = waiting_signin(data)
    reason = "that code was not accepted: nothing was stored; run `resumebot signin` again"
    clock.hooks.append(lambda now: status.write_text(reason, encoding="utf-8"))
    assert run(data, "--code", CODE) == 1
    shown = capsys.readouterr()
    assert shown.err.strip() == reason and CODE not in shown.out + shown.err


def test_signin_code_gives_up_after_a_minute_and_removes_the_code(data, clock, capsys):
    waiting_signin(data)
    assert run(data, "--code", CODE) == 1
    assert "no answer to that code yet" in capsys.readouterr().err
    assert 60 <= clock.now < 61 and not (secrets_of(data) / setup_cli.SIGNIN_CODE_FILE).exists()


@pytest.mark.parametrize("before", [None, "signed in", "timed out after 10 minutes: nothing was stored"],
                         ids=["never run", "finished", "failed"])
def test_signin_code_with_no_signin_waiting_says_to_run_signin_first_and_writes_no_code(data, clock, capsys, before):
    if before is not None:
        secrets_of(data).mkdir()
        (secrets_of(data) / setup_cli.SIGNIN_STATUS_FILE).write_text(before, encoding="utf-8")
    assert run(data, "--code", CODE) == 1
    assert "run `resumebot signin` first" in capsys.readouterr().err
    assert not (secrets_of(data) / setup_cli.SIGNIN_CODE_FILE).exists() and clock.now == 0


def test_signin_code_when_the_waiting_signin_was_killed_says_so_at_once_and_writes_no_code(data, clock, capsys):
    waiting_signin(data, pid=999_999)  # `waiting` is still in the file, but nothing runs under that pid
    assert run(data, "--code", CODE) == 1
    shown = capsys.readouterr()
    assert "no longer running: run `resumebot signin` again" in shown.err and CODE not in shown.out + shown.err
    assert clock.now == 0 and not (secrets_of(data) / setup_cli.SIGNIN_CODE_FILE).exists()


def test_signin_code_when_the_waiting_file_has_no_pid_says_to_run_signin_again(data, clock, capsys):
    waiting_signin(data).parent.joinpath(setup_cli.SIGNIN_PID_FILE).unlink()
    assert run(data, "--code", CODE) == 1
    assert "run `resumebot signin` again" in capsys.readouterr().err and clock.now == 0


def test_a_new_signin_clears_the_status_the_code_and_the_pid_a_killed_one_left(data, env, monkeypatch, clock):
    waiting_signin(data, pid=999_999)
    (secrets_of(data) / setup_cli.SIGNIN_CODE_FILE).write_text(CODE, encoding="utf-8")
    seen = []

    class Watching(FakeChild):
        def read(self):
            seen.append({p.name: p.read_text(encoding="utf-8") for p in secrets_of(data).iterdir()})
            return super().read()

    child_is(monkeypatch, env, Watching(script=[TOKEN_SCREEN], ends=True))
    assert run(data) == 0
    assert seen[0] == {setup_cli.SIGNIN_PID_FILE: str(os.getpid()), setup_cli.SIGNIN_STATUS_FILE: "waiting"}
    assert not (secrets_of(data) / setup_cli.SIGNIN_PID_FILE).exists()


def test_signin_code_with_an_empty_code_writes_nothing(data, capsys):
    assert run(data, "--code", "  ") == 1
    assert "no code given" in capsys.readouterr().err


# --- the file-limiting step ---------------------------------------------------------------------------------------


def test_only_me_file_on_windows_drops_inheritance_and_grants_this_users_sid_full_access(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        out = '"host\\user","S-1-5-21-111"\n' if command[0] == "whoami" else ""
        return subprocess.CompletedProcess(command, 0, out, "")

    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli.subprocess, "run", fake_run)
    path = tmp_path / "f"
    REAL_ONLY_ME_FILE(path)
    assert calls[1] == ["icacls", str(path), "/inheritance:r", "/grant:r", "*S-1-5-21-111:F"]


def test_only_me_file_on_windows_raises_when_icacls_fails(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        if command[0] == "icacls":
            raise subprocess.CalledProcessError(5, command)
        return subprocess.CompletedProcess(command, 0, '"u","S-1"\n', "")

    monkeypatch.setattr(setup_cli.sys, "platform", "win32")
    monkeypatch.setattr(setup_cli.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        REAL_ONLY_ME_FILE(tmp_path / "f")


def test_only_me_file_elsewhere_sets_mode_0600(tmp_path, monkeypatch):
    modes = []
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    monkeypatch.setattr(Path, "chmod", lambda self, mode: modes.append((self.name, mode)))
    REAL_ONLY_ME_FILE(tmp_path / "f")
    assert modes == [("f", 0o600)]


# --- hardening: a second sign-in, a bad stored token, a failure with a reason ---------------------------------------


def test_a_second_signin_refuses_while_the_first_is_waiting_and_leaves_its_files_alone(data, env, capsys):
    status = waiting_signin(data)
    pid_file = secrets_of(data) / setup_cli.SIGNIN_PID_FILE
    assert run(data) == 1  # the env fixture fails the test if a child is spawned
    assert "a sign-in is already waiting" in capsys.readouterr().err
    assert pid_file.read_text(encoding="utf-8") == str(LIVE_PID)
    assert status.read_text(encoding="utf-8") == setup_cli.SIGNIN_WAITING


def test_a_signin_that_loses_the_race_for_the_pid_file_refuses(data, env, monkeypatch, capsys):
    secrets_of(data).mkdir()
    pid_file = secrets_of(data) / setup_cli.SIGNIN_PID_FILE
    monkeypatch.setattr(setup_cli, "_clear_signin_files",
                        lambda install: pid_file.write_text("4242", encoding="utf-8"))
    assert run(data) == 1
    assert "already waiting" in capsys.readouterr().err
    assert pid_file.read_text(encoding="utf-8") == "4242"


def test_the_first_signin_does_not_delete_a_second_ones_pid_file_or_status_when_it_ends(
        data, env, monkeypatch, clock, capsys):
    pid_file = secrets_of(data) / setup_cli.SIGNIN_PID_FILE
    status = secrets_of(data) / setup_cli.SIGNIN_STATUS_FILE

    def takeover(now):  # another run took the files over while this one waited
        pid_file.write_text("999999", encoding="utf-8")
        status.write_text(setup_cli.SIGNIN_WAITING, encoding="utf-8")

    class Taken(FakeChild):
        def alive(self):
            takeover(0)
            return super().alive()

    child_is(monkeypatch, env, Taken([URL_SCREEN], ends=True))
    assert run(data) == 1
    assert pid_file.read_text(encoding="utf-8") == "999999"
    assert status.read_text(encoding="utf-8") == setup_cli.SIGNIN_WAITING


@pytest.mark.parametrize("content", ["a-token-from-an-earlier-release", "sk-ant-short",
                                     "sk-ant-" + "a" * 50 + "sk-ant-" + "b" * 50])
def test_signin_signs_in_anew_when_the_stored_token_is_not_shaped_like_one(
        data, target, env, monkeypatch, clock, capsys, content):
    target.parent.mkdir()
    target.write_text(content, encoding="utf-8")
    child_is(monkeypatch, env, FakeChild([TOKEN_SCREEN]))
    assert run(data) == 0
    assert target.read_text(encoding="utf-8") == TOKEN
    assert "not a usable one" in capsys.readouterr().out


def test_a_child_that_cannot_be_found_says_why_without_a_secret(data, env, monkeypatch, capsys):
    def missing(argv, environment):
        raise FileNotFoundError("[WinError 2] The system cannot find the file specified")

    monkeypatch.setattr(setup_cli, "_spawn_pty", missing)
    assert run(data) == 1
    assert "FileNotFoundError: [WinError 2] The system cannot find the file specified" in capsys.readouterr().err
    assert not (secrets_of(data) / setup_cli.SIGNIN_PID_FILE).exists()


def test_a_child_that_ends_without_a_token_shows_the_redacted_tail_of_its_screen_and_the_likely_causes(
        data, env, monkeypatch, clock, capsys):
    secret = "sk-ant-oat01-Z9Z9"  # too short to be taken as a token, still never shown
    screen = f"Opening {ADDRESS}\r\nSomething went wrong near {secret}\r\nlogin failed\r\n"
    child_is(monkeypatch, env, FakeChild([screen], ends=True))
    assert run(data) == 1
    err = capsys.readouterr().err
    assert "ended without a token" in err and "login failed" in err and "usual causes" in err
    assert secret not in err and "Z9Z9" not in err and "client_id=abc" not in err
