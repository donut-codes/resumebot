"""The registry of site runbooks (the generic-sites design, Parts 1 to 3): what loads from the shipped folder and the
restricted folder, what only the restricted folder may carry, how a host's stop and pace are answered, and that a
runbook that does not load never leaves a host at the generic pace or a stop for good. Every site here is made up
(`example.test`); the real runbooks' own facts are the restricted tier's (`restricted_runbooks/tests`)."""

import json
from pathlib import Path

import pytest

from app.jobsearch.fetch import polite
from app.sites import loader, registry
from app.sites.registry import Registry, RunbookUnavailable
from app.sites.schema import DEFAULT_PACE_SECONDS, DEFAULT_SLOW_STATUSES, RESTRICTED_ONLY

TERMS = (
    {"name": "Open Example", "hosts": ["open.example.test"], "verdict": "allows"},
    {"name": "Quiet Example", "hosts": ["quiet.example.test"], "verdict": "silent"},
    {"name": "Closed Example", "hosts": ["closed.example.test", "x.closed.example.test"], "verdict": "forbids"},
)


def runbook(key: str, hosts: list[str] | None = None, **over) -> dict:
    doc = {"schema": 1, "key": key, "name": key.title(), "kind": "host", "hosts": hosts or [f"{key}.example.test"]}
    doc.update(over)
    return doc


class World:
    """A shipped folder and a restricted folder in a temporary place, as the checkout has them."""

    def __init__(self, root: Path) -> None:
        self.shipped = root / "shipped"
        self.restricted = root / "restricted"
        restricted = self.restricted
        for folder in (self.shipped, restricted / "runbooks", restricted / "pending", restricted / "code"):
            folder.mkdir(parents=True)

    def put(self, doc: dict, where: str = "shipped") -> Path:
        folder = self.shipped if where == "shipped" else self.restricted / where
        path = folder / f"{doc['key']}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def load(self, *, restricted: bool = True) -> Registry:
        return loader.load(self.shipped, self.restricted if restricted else self.restricted / "absent", TERMS)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# --- loading ---------------------------------------------------------------------------------------------------------


def test_a_shipped_runbook_loads_with_the_defaults_of_a_field_it_leaves_out(world):
    world.put(runbook("alpha"))
    reg = world.load()
    found = reg.get("alpha")
    assert found is not None and reg.problems == ()
    assert (found.order, found.shared_host, found.starts_off, found.stop) == (None, False, False, "for_good")
    assert (found.pace_seconds, found.slow_statuses, found.probe, found.read) == (None, None, None, None)
    assert (found.mail.kind, found.mail.senders, found.mail.confirms) == ("none", (), False)
    assert (found.flags.needs, found.flags.drawn, found.follow_to_employer) == (("slug",), False, False)
    assert found.code_reads_postings is False


def test_the_restricted_folders_runbooks_and_pending_both_load_beside_the_shipped_ones(world):
    world.put(runbook("alpha"))
    world.put(runbook("beta"), "runbooks")
    world.put(runbook("gamma"), "pending")
    reg = world.load()
    assert sorted(r.key for r in reg.runbooks) == ["alpha", "beta", "gamma"]
    assert reg.restricted_present is True and reg.problems == ()


def test_a_folder_that_is_absent_holds_nothing_and_the_registry_says_the_restricted_one_is_missing(world):
    world.put(runbook("alpha"))
    reg = world.load(restricted=False)
    assert [r.key for r in reg.runbooks] == ["alpha"]
    assert reg.restricted_present is False and reg.problems == ()


def test_job_systems_are_asked_in_their_order_and_a_job_system_without_one_does_not_load(world):
    world.put(runbook("b", kind="job_system", order=1))
    world.put(runbook("a", kind="job_system", order=0))
    world.put(runbook("c", kind="job_system"))
    reg = world.load()
    assert reg.order() == ("a", "b")
    assert [p.path for p in reg.problems] == ["c.json"]


@pytest.mark.parametrize("field,value", [
    ("robots", {"host": "alpha.example.test", "path_prefix": "/feed/"}),
    ("reader", "hook"),
    ("read_hosts", ["feeds.example.test"]),
    ("signed_in", True),
    ("extras", {"anything": 1}),
])
def test_the_five_restricted_fields_are_refused_in_the_shipped_folder(world, field, value):
    assert set(RESTRICTED_ONLY) == {"robots", "reader", "read_hosts", "signed_in", "extras"}
    doc = runbook("alpha", pace_seconds=30, stop="until_next_off_hours", **{field: value})
    world.put(doc)
    reg = world.load()
    assert reg.get("alpha") is None
    [problem] = reg.problems
    assert "restricted folder" in problem.reason and problem.hosts == ("alpha.example.test",)


def test_a_restricted_file_is_known_by_its_real_path_so_a_copy_of_it_in_the_shipped_folder_is_refused(world):
    doc = runbook("alpha", extras={"k": 1})
    world.put(doc, "runbooks")
    assert world.load().get("alpha") is not None
    world.put(doc)  # the same document, in the shipped folder
    reg = world.load(restricted=False)
    assert reg.get("alpha") is None and len(reg.problems) == 1


def test_a_reader_names_a_module_of_the_restricted_code_folder_and_nothing_else(world):
    (world.restricted / "code" / "hook.py").write_text("VALUE = 1\n", encoding="utf-8")
    world.put(runbook("alpha", reader="hook"), "runbooks")
    world.put(runbook("beta", reader="missing"), "runbooks")
    reg = world.load()
    assert reg.get("alpha") is not None and reg.get("beta") is None
    assert reg.hook_for("hook").VALUE == 1  # pyright: ignore[reportOptionalMemberAccess]
    assert reg.hook_for("missing") is None and reg.hook_for("nobody") is None
    world.put(runbook("gamma", reader="../escape"), "runbooks")
    assert world.load().get("gamma") is None


@pytest.mark.parametrize("over", [
    {"pace_seconds": 29.9, "stop": "until_next_off_hours"},
    {"stop": "until_next_off_hours"},
    {"pace_seconds": 30},
    {"pace_seconds": 30, "stop": "until_next_off_hours",
     "robots": {"host": "elsewhere.example.test", "path_prefix": "/x"}},
])
def test_a_robots_runbook_without_its_pace_and_its_stop_or_for_another_host_does_not_load(world, over):
    doc = runbook("alpha", **{"robots": {"host": "alpha.example.test", "path_prefix": "/feed/"}, **over})
    world.put(doc, "runbooks")
    assert world.load().get("alpha") is None


def test_a_robots_runbook_in_the_restricted_folder_loads_with_its_guards(world):
    world.put(runbook("alpha", ["alpha.test"], pace_seconds=30, stop="until_next_off_hours",
                      robots={"host": "www.alpha.test", "path_prefix": "/feed/"}), "runbooks")
    reg = world.load()
    [found] = reg.robots_runbooks()
    assert found.key == "alpha" and reg.pace_for_domain("alpha.test") == 30.0


def test_a_field_the_schema_does_not_know_does_not_load(world):
    world.put(runbook("alpha", surprise=True))
    assert world.load().get("alpha") is None


# --- failing closed (2.4) --------------------------------------------------------------------------------------------


def test_a_runbook_that_does_not_load_leaves_the_app_starting_and_its_hosts_refused_not_read_at_the_generic_pace(world):
    world.put(runbook("alpha"))
    world.put(runbook("broken", ["slow.example.test"], pace_seconds=-1, stop="until_next_off_hours"))
    (world.shipped / "garbled.json").write_text("{not json", encoding="utf-8")
    reg = world.load()
    assert reg.get("alpha") is not None and len(reg.problems) == 2
    for ask in (reg.stop_key, reg.ends_with_the_night, reg.pace_for_domain, reg.pace_for_host, reg.slow_statuses_for):
        with pytest.raises(RunbookUnavailable, match="did not load"):
            ask("api.slow.example.test" if ask is not reg.pace_for_domain else "slow.example.test")
    assert "did not load" in (reg.refusal("slow.example.test") or "")
    assert reg.refusal("alpha.example.test") is None
    assert reg.stop_key("alpha.example.test") == "alpha.example.test"  # a host that loaded is answered as ever


def test_two_runbooks_with_one_key_is_a_problem_and_the_first_stands(world):
    world.put(runbook("alpha"))
    world.put(runbook("alpha", ["other.example.test"]), "runbooks")
    reg = world.load()
    assert [r.hosts for r in reg.runbooks] == [("alpha.example.test",)] and len(reg.problems) == 1


def test_stored_hosts_no_runbook_covers_are_named_while_the_registry_is_not_whole(world):
    world.put(runbook("alpha"))
    whole = world.load()
    assert whole.uncovered(["alpha.example.test", "other.example.test"]) == []
    without = world.load(restricted=False)
    assert without.uncovered(["alpha.example.test", "other.example.test"]) == ["other.example.test"]
    world.put(runbook("broken", ["slow.example.test"], pace_seconds=-1))
    flawed = world.load()
    assert flawed.uncovered(["alpha.example.test", "other.example.test"]) == ["other.example.test"]


# --- stops and pace (1.6) --------------------------------------------------------------------------------------------


def _night(world) -> Registry:
    world.put(runbook("night", ["night.example.test"], kind="board", stop="until_next_off_hours", pace_seconds=30,
                      robots={"host": "www.night.example.test", "path_prefix": "/guest/"}), "runbooks")
    world.put(runbook("plain", ["plain.example.test"], kind="board"))
    return world.load()


def test_every_host_of_a_domain_whose_stop_ends_with_the_night_is_stopped_under_the_domain(world):
    reg = _night(world)
    for host in ("night.example.test", "www.night.example.test", "uk.night.example.test", "UK.NIGHT.example.test."):
        assert reg.stop_key(host) == "night.example.test"
        assert reg.ends_with_the_night(host) is True
    assert reg.stop_key("not-night.example.test") == "not-night.example.test"


def test_a_host_whose_stop_lasts_for_good_is_kept_as_the_host_without_www(world):
    reg = _night(world)
    assert reg.stop_key("www.plain.example.test") == "plain.example.test"
    assert reg.stop_key("jobs.plain.example.test") == "jobs.plain.example.test"
    assert reg.ends_with_the_night("jobs.plain.example.test") is False
    assert reg.stop_key(None) == "" and reg.stop_key("") == ""


def test_the_pace_of_a_domain_is_the_slowest_of_its_runbooks_and_nothing_when_none_is_slowed(world):
    world.put(runbook("one", ["one.test"], pace_seconds=5))
    world.put(runbook("two", ["one.test", "two.test"], pace_seconds=12))
    reg = world.load()
    assert reg.pace_for_domain("one.test") == 12.0
    assert reg.pace_for_domain("two.test") == 12.0
    assert reg.pace_for_domain("free.test") == 0.0 and reg.pace_for_domain("") == 0.0


def test_a_host_is_paced_by_its_runbook_else_by_the_generic_pace_the_caller_names(world):
    world.put(runbook("one", ["one.example.test"], pace_seconds=5, slow_statuses=[429, 503]))
    reg = world.load()
    assert reg.pace_for_host("api.one.example.test") == 5.0
    assert reg.pace_for_host("free.example.test") == DEFAULT_PACE_SECONDS
    assert reg.pace_for_host("free.example.test", 2.5) == 2.5
    assert reg.slow_statuses_for("one.example.test") == (429, 503)
    assert reg.slow_statuses_for("free.example.test") == DEFAULT_SLOW_STATUSES


def test_the_generic_defaults_are_the_fetchers_own():
    assert DEFAULT_SLOW_STATUSES == polite.NOT_NOW
    assert polite.Fetcher.__init__.__defaults__ == (None, DEFAULT_PACE_SECONDS)


# --- what the runbooks say -------------------------------------------------------------------------------------------


def test_the_lists_the_code_once_held_are_read_from_the_runbooks(world):
    world.put(runbook("board_a", ["a.example.test"], kind="board",
                      address={"own_careers_paths": ["about", "jobs"]},
                      mail={"kind": "board", "senders": ["mail.a.example.test"], "confirms": True}))
    world.put(runbook("board_b", ["b.example.test"], kind="board",
                      mail={"kind": "board", "senders": ["b.example.test"]}))
    world.put(runbook("sys_a", ["s.example.test"], kind="job_system", order=0, starts_off=True,
                      mail={"kind": "system", "senders": ["s.example.test", "s-mail.example.test"], "confirms": True}))
    world.put(runbook("sys_b", ["t.example.test"], kind="job_system", order=1,
                      mail={"kind": "system", "senders": ["t.example.test"]}))
    world.put(runbook("quiet", ["q.example.test"], shared_host=True,
                      mail={"kind": "none", "senders": ["q.example.test"]}))
    reg = world.load()
    assert reg.board_hosts() == ("a.example.test", "b.example.test")
    assert reg.board_named("jobs.a.example.test") and reg.board_named("A.example.test.")
    assert not reg.board_named("example.test") and not reg.board_named("sub.s.example.test")
    assert reg.starts_off() == ("sys_a",)
    assert reg.shared_hosts() == ("q.example.test",)
    assert reg.own_careers_paths() == {"a.example.test": ("about", "jobs")}
    boards, systems, confirming = reg.mail_sets()
    assert set(boards) == {"mail.a.example.test", "b.example.test"}
    assert set(systems) == {"mail.a.example.test", "b.example.test", "s.example.test", "s-mail.example.test",
                            "t.example.test"}
    assert set(confirming) == {"mail.a.example.test", "s.example.test", "s-mail.example.test"}


def test_a_hosts_runbook_is_the_most_specific_one_and_a_link_finds_it_by_its_host(world):
    world.put(runbook("wide", ["example.test"]))
    world.put(runbook("narrow", ["deep.example.test"]))
    reg = world.load()
    assert reg.for_host("a.deep.example.test").key == "narrow"  # pyright: ignore[reportOptionalMemberAccess]
    assert reg.for_host("other.example.test").key == "wide"  # pyright: ignore[reportOptionalMemberAccess]
    assert reg.by_address("https://x.deep.example.test/jobs/1?a=b").key == "narrow"  # pyright: ignore
    assert reg.by_address("deep.example.test/x").key == "narrow"  # pyright: ignore[reportOptionalMemberAccess]
    assert reg.for_host("elsewhere.test") is None and reg.for_host(None) is None and reg.by_address("") is None


def test_the_verdict_is_what_the_terms_say_of_the_runbooks_host_and_never_a_field(world):
    reg = world.load()
    assert reg.verdict("jobs.open.example.test") == "allows"
    assert reg.verdict("quiet.example.test") == "silent"
    assert reg.verdict("x.closed.example.test") == "forbids"
    assert reg.verdict("unlisted.example.test") is None
    assert "verdict" not in __import__("app.sites.schema", fromlist=["Runbook"]).Runbook.model_fields


def test_the_owners_switch_for_a_followed_board_needs_the_setting_and_a_runbook_that_has_the_field(world):
    class Settings:
        read_robots_exempt = True

    world.put(runbook("night", ["night.example.test"], kind="board", stop="until_next_off_hours", pace_seconds=30,
                      follow_to_employer=True, robots={"host": "night.example.test", "path_prefix": "/g/"}), "runbooks")
    assert world.load().follow_board_on(Settings()) is True
    assert world.load().follow_board_on(object()) is False
    assert world.load(restricted=False).follow_board_on(Settings()) is False


# --- the one test hook -----------------------------------------------------------------------------------------------


def test_replaced_is_the_one_way_a_test_puts_its_own_registry_in_place_and_it_puts_the_old_back(world):
    world.put(runbook("alpha", kind="board"))
    mine = world.load()
    before = registry.current()
    with registry.replaced(mine) as inside:
        assert inside is mine and registry.current() is mine
        assert registry.board_hosts() == ("alpha.example.test",) and registry.get("alpha") is not None
        assert registry.stop_key("www.alpha.example.test") == "alpha.example.test"
    assert registry.current() is before
    assert registry.get("alpha") is None


def test_the_real_registry_loads_with_no_problem_and_ships_only_what_the_terms_open():
    reg = loader.load(restricted_dir=Path("absent-restricted-folder"))
    assert reg.problems == ()
    assert reg.runbooks, "the shipped folder holds runbooks"
    for r in reg.runbooks:
        assert not r.has_restricted_fields
        assert {reg.verdict(h) for h in r.hosts} <= {"allows", "silent"}, r.key
    assert [r.key for r in reg.runbooks if r.kind == "job_system"] == list(reg.order())


def test_the_whole_registry_of_the_checkout_loads_with_no_problem():
    assert loader.load().problems == ()


def test_the_hosts_whose_postings_the_apps_own_code_reads_are_the_runbooks_that_say_so(world):
    world.put(runbook("alpha", code_reads_postings=True))
    world.put(runbook("beta", ["beta.example.test", "beta.example.org"], code_reads_postings=True))
    world.put(runbook("gamma"))
    reg = world.load()
    assert sorted(reg.code_read_hosts()) == ["alpha.example.test", "beta.example.org", "beta.example.test"]
    with registry.replaced(reg):
        assert sorted(registry.code_read_hosts()) == sorted(reg.code_read_hosts())
