"""One log line format for the app's lines and uvicorn's: ISO time with its UTC offset, program, pid, level, logger and
message; uvicorn's access line has no query string (the browser bridge's key travels in one). Everything is made up."""

import io
import logging
import logging.config
import os
import re

import pytest

from app import log_setup, program

SECRET = "k3y-made-up-9f2b7c"
LINE = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d (\S+) (\d+) (\w+) (\S+): (.*)$"
)


@pytest.fixture
def program_page(monkeypatch):
    monkeypatch.setattr(program, "PROGRAM", "page")


def _record(msg="hello", args=None, name="app.test", level=logging.INFO):
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


def test_a_line_has_the_offset_the_program_and_the_pid(program_page):
    line = log_setup.LogFormatter().format(_record())
    found = LINE.match(line)
    assert found, line
    assert found.group(1) == "page"
    assert int(found.group(2)) == os.getpid()
    assert found.group(3, 4, 5) == ("INFO", "app.test", "hello")


def test_a_program_that_is_not_set_is_a_dash(monkeypatch):
    monkeypatch.setattr(program, "PROGRAM", "")
    assert " - " in log_setup.LogFormatter().format(_record())


def test_configure_logging_gives_the_root_handler_the_format(monkeypatch):
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    root.handlers = []
    try:
        log_setup.configure_logging()
        assert isinstance(root.handlers[0].formatter, log_setup.LogFormatter)
    finally:
        root.handlers, _ = saved, root.setLevel(level)
        logging.getLogger("uvicorn.access").filters.clear()


def test_an_access_line_holds_no_query(program_page):
    stream = io.StringIO()
    config = log_setup.uvicorn_log_config()
    config["handlers"]["access"]["stream"] = stream
    config["handlers"]["default"]["stream"] = stream
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).propagate) for n in names}
    try:
        logging.config.dictConfig(config)
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", f"/bridge?key={SECRET}&x=1", "1.1", 200
        )
        logging.getLogger("uvicorn.error").info("Application startup complete.")
    finally:
        for n, (handlers, propagate) in saved.items():
            logging.getLogger(n).handlers, logging.getLogger(n).propagate = handlers, propagate
    out = stream.getvalue()
    assert SECRET not in out
    assert '"GET /bridge HTTP/1.1" 200' in out
    lines = out.splitlines()
    assert all(LINE.match(x) and " page " in x for x in lines), lines


def test_the_root_handlers_survive_the_migrations_env(tmp_path):
    from app import db

    root = logging.getLogger()
    mine = logging.NullHandler()
    root.addHandler(mine)
    before = root.handlers[:]
    try:
        db._upgrade(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}", "head")
        assert root.handlers == before
    finally:
        root.removeHandler(mine)


def test_the_start_up_line_has_the_common_format_after_a_library_set_the_root_up_with_a_bare_format(program_page):
    """The MCP library calls `logging.basicConfig(format="%(message)s")` when its server object is built, before the
    program starts; the start-up line then had no time, program or pid."""
    from app.jobsearch.work import door

    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    stream = io.StringIO()
    bare = logging.StreamHandler(stream)
    bare.setFormatter(logging.Formatter("%(message)s"))
    root.handlers = [bare]
    try:
        log_setup.configure_logging()
        ours = [h for h in root.handlers if isinstance(h.formatter, log_setup.LogFormatter)]
        assert len(ours) == 1 and bare not in root.handlers
        ours[0].setStream(stream)
        logging.getLogger("app.jobsearch.main").info(door.start_line("page"))
    finally:
        root.handlers, _ = saved, root.setLevel(level)
        logging.getLogger("uvicorn.access").filters.clear()
    (line,) = stream.getvalue().splitlines()
    found = LINE.match(line)
    assert found, line
    assert found.group(1) == "page" and int(found.group(2)) == os.getpid()
    assert found.group(5).startswith("started: version=")


def test_configure_logging_leaves_handlers_that_are_not_console_ones_alone():
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    other = logging.NullHandler()
    root.handlers = [other]
    try:
        log_setup.configure_logging()
        assert other in root.handlers
    finally:
        root.handlers, _ = saved, root.setLevel(level)
        logging.getLogger("uvicorn.access").filters.clear()
