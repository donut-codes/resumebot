"""`ats` keeps its tables of hosts as views of the registry's runbooks (the generic-sites design, 2.1): what a test's
registry says is what they hold. Every site is made up."""

import json

from app.jobsearch import ats
from app.sites import loader, registry


def _registry(tmp_path):
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    docs = [
        {"key": "board_x", "name": "Board X", "kind": "board", "hosts": ["board-x.example.test"],
         "address": {"own_careers_paths": ["about", "jobs"]}},
        {"key": "sys_y", "name": "Sys Y", "kind": "job_system", "hosts": ["sys-y.example.test"], "order": 0,
         "starts_off": True},
        {"key": "sys_z", "name": "Sys Z", "kind": "job_system", "hosts": ["sys-z.example.test"], "order": 1},
    ]
    for doc in docs:
        (shipped / f"{doc['key']}.json").write_text(json.dumps({"schema": 1, **doc}), encoding="utf-8")
    return loader.load(shipped, tmp_path / "absent", ())


def test_the_boards_and_the_systems_a_new_install_starts_off_are_what_the_runbooks_say(tmp_path):
    with registry.replaced(_registry(tmp_path)):
        assert list(ats.BOARDS) == ["board-x.example.test"] and "board-x.example.test" in ats.BOARDS
        assert ats.NOT_OFFERED == ats.NOT_OFFERED and list(ats.NOT_OFFERED) == ["sys_y"]
        assert len(ats.BOARDS) == 1 and ats.BOARDS[0] == "board-x.example.test"
        assert "sys_z" not in ats.NOT_OFFERED
        assert dict(ats.OWN_CAREERS) == {"board-x.example.test": ("about", "jobs")}


def test_a_host_is_a_board_when_a_runbook_says_so_and_so_is_every_host_under_it(tmp_path):
    with registry.replaced(_registry(tmp_path)):
        assert ats.board_named("jobs.board-x.example.test") and not ats.board_named("sys-y.example.test")
        assert ats.own_careers_posting("https://board-x.example.test/about/jobs/123-the-role")
        assert not ats.own_careers_posting("https://board-x.example.test/about/jobs/results")
        assert not ats.own_careers_posting("https://sys-y.example.test/about/jobs/123-x")
