"""Which program this process is (`app.program`): `all` until a program starts, set by the program's start, and
`require` lets the test seam (`all`) through but refuses the other program."""

import pytest

from app import program
from app.jobsearch import main
from app.jobsearch.loops import startup


def test_a_process_is_the_whole_app_until_a_program_starts():
    assert program.PROGRAM == "all"


def test_start_records_the_program(monkeypatch):
    monkeypatch.setattr(program, "PROGRAM", "all")
    program.start("page")
    assert program.PROGRAM == "page"


def test_start_refuses_a_name_that_is_no_program(monkeypatch):
    monkeypatch.setattr(program, "PROGRAM", "all")
    with pytest.raises(ValueError, match="unknown program"):
        program.start("worker")
    assert program.PROGRAM == "all"


@pytest.mark.parametrize(("current", "wanted", "allowed"), [
    ("all", "engine", True), ("all", "page", True), ("engine", "engine", True), ("page", "page", True),
    ("page", "engine", False), ("engine", "page", False)])
def test_require_lets_the_program_and_the_whole_app_through_but_not_the_other(current, wanted, allowed, monkeypatch):
    monkeypatch.setattr(program, "PROGRAM", current)
    if allowed:
        program.require(wanted)
    else:
        with pytest.raises(program.WrongProgram, match=f"belongs to the {wanted} program"):
            program.require(wanted)


async def test_the_engines_start_says_it_is_the_engine_before_it_opens_anything(monkeypatch):
    class Reached(Exception):
        pass

    def stop():
        raise Reached  # the step right after the program says which it is

    monkeypatch.setattr(program, "PROGRAM", "all")
    monkeypatch.setattr(startup, "configure_logging", stop)
    with pytest.raises(Reached):
        async with main._running("engine"):
            pass
    assert program.PROGRAM == "engine"


async def test_the_start_that_runs_the_loops_is_not_the_pages():
    with pytest.raises(ValueError, match="page"):
        async with main._running("page"):
            pass
