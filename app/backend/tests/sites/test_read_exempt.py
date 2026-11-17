"""`Fetcher.read_exempt`, the one door to reading a host's feed though its robots.txt asks automated readers not to
(docs/PRD.md path 48; the generic-sites design, Part 7): four guards (pace, the stop that ends with the night, a
signed-out guest, the owner's setting), a loader that takes the field from the restricted folder only, and one caller
of the primitive behind it. Made-up sites and a made-up tree only."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.jobsearch.fetch.polite import ExemptRefused, Fetcher
from app.sites import loader, registry
from app.sites.schema import Runbook

FEED = "https://feed.example.test/open/search?start=0"
ELSEWHERE = "https://feed.example.test/private/page"
ON = SimpleNamespace(read_robots_exempt=True)
OFF = SimpleNamespace(read_robots_exempt=False)
APP = Path(__file__).resolve().parents[2] / "app"
RESTRICTED = Path(__file__).resolve().parents[2] / "restricted_runbooks"


def _doc(**over) -> dict:
    doc = {"schema": 1, "key": "example_feed", "name": "Example Feed", "kind": "board", "hosts": ["feed.example.test"],
           "pace_seconds": 30.0, "stop": "until_next_off_hours",
           "robots": {"host": "feed.example.test", "path_prefix": "/open/"}}
    return {**doc, **over}


def _fetcher(**client) -> Fetcher:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /") if request.url.path == "/robots.txt" \
            else httpx.Response(200, text="ok")

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(answer), **client), per_host_seconds=0)


@pytest.fixture
def example(tmp_path):
    """A registry whose restricted folder (a temporary one) holds one runbook that carries `robots`."""
    (tmp_path / "shipped").mkdir()
    for sub in ("runbooks", "pending", "code"):
        (tmp_path / "restricted" / sub).mkdir(parents=True)
    (tmp_path / "restricted" / "runbooks" / "example_feed.json").write_text(json.dumps(_doc()), encoding="utf-8")
    reg = loader.load(tmp_path / "shipped", tmp_path / "restricted", ())
    assert reg.problems == ()
    with registry.replaced(reg):
        yield reg.get("example_feed")


async def test_a_runbook_with_robots_opens_its_one_prefix_and_nothing_else(example):
    fetch = _fetcher()
    assert await fetch.allows(FEED) is False, "robots.txt disallows everything until the door is used"
    fetch.read_exempt(example, ON)
    assert await fetch.allows(FEED) is True
    assert await fetch.allows(ELSEWHERE) is False
    await fetch.close()


@pytest.mark.parametrize(("what", "change"), [
    ("a pace under 30 seconds", {"pace_seconds": 29.0}),
    ("no pace", {"pace_seconds": None}),
    ("a stop for good", {"stop": "for_good"}),
    ("a signed-in session", {"signed_in": True}),
])
async def test_a_runbook_that_fails_a_guard_the_loader_holds_is_refused_again_at_the_door(example, what, change):
    changed = example.model_copy(update=change)  # a copy is not validated: the door must not rely on the loader
    with registry.replaced(registry.Registry([changed])):
        fetch = _fetcher()
        with pytest.raises(ExemptRefused):
            fetch.read_exempt(changed, ON)
        assert await fetch.allows(FEED) is False, what
        await fetch.close()


async def test_the_owners_setting_must_be_on_and_is_off_unless_it_says_true(example):
    for settings in (OFF, SimpleNamespace(), SimpleNamespace(read_robots_exempt="yes")):
        fetch = _fetcher()
        with pytest.raises(ExemptRefused):
            fetch.read_exempt(example, settings)
        assert await fetch.allows(FEED) is False
        await fetch.close()


@pytest.mark.parametrize("client", [
    {"cookies": {"session": "x"}},
    {"headers": {"Authorization": "Bearer x"}},
    {"headers": {"Cookie": "session=x"}},
    {"auth": ("someone", "secret")},
])
async def test_a_fetcher_that_carries_a_login_is_not_a_guest_and_is_refused(example, client):
    fetch = _fetcher(**client)
    with pytest.raises(ExemptRefused):
        fetch.read_exempt(example, ON)
    await fetch.close()


async def test_a_runbook_the_registry_does_not_hold_or_one_without_robots_is_refused(example):
    stranger = example.model_copy(update={"key": "other_feed"})
    plain = Runbook.model_validate({"schema": 1, "key": "plain", "name": "Plain", "kind": "board",
                                    "hosts": ["a.example.test"]})
    fetch = _fetcher()
    for runbook in (stranger, plain):
        with pytest.raises(ExemptRefused):
            fetch.read_exempt(runbook, ON)
    await fetch.close()


def test_a_shipped_runbook_that_carries_robots_does_not_load(tmp_path):
    (tmp_path / "shipped").mkdir()
    (tmp_path / "shipped" / "example_feed.json").write_text(json.dumps(_doc()), encoding="utf-8")
    reg = loader.load(tmp_path / "shipped", tmp_path / "absent", ())
    assert reg.get("example_feed") is None
    assert [p.path for p in reg.problems] == ["example_feed.json"] and "robots" in reg.problems[0].reason


def _calls(tree: ast.AST, name: str) -> list[int]:
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
            and (getattr(n.func, "attr", None) == name or getattr(n.func, "id", None) == name)]


def _sources(root: Path) -> dict[Path, ast.AST]:
    return {f: ast.parse(f.read_text(encoding="utf-8")) for f in root.rglob("*.py")}


def test_the_primitive_behind_the_door_has_one_caller_and_no_hook_can_reach_it():
    polite = APP / "jobsearch" / "fetch" / "polite.py"
    callers = {f: _calls(tree, "_exempt_robots") for f, tree in _sources(APP).items()}
    assert {f: lines for f, lines in callers.items() if lines}.keys() == {polite}
    assert len(callers[polite]) == 1
    module = ast.parse(polite.read_text(encoding="utf-8"))
    door = next(n for n in ast.walk(module) if isinstance(n, ast.FunctionDef) and n.name == "read_exempt")
    assert len(_calls(door, "_exempt_robots")) == 1, "the one call is inside the door"
    writes = [n.lineno for tree in [module] for n in ast.walk(tree) if isinstance(n, ast.Subscript)
              and isinstance(n.value, ast.Attribute) and n.value.attr == "_exempt" and isinstance(n.ctx, ast.Store)]
    assert len(writes) == 1, "the exempt table is written in one place"
    others = [f for f, tree in _sources(APP).items() if f != polite and any(
        isinstance(n, ast.Attribute) and n.attr in ("_exempt", "_exempt_robots") for n in ast.walk(tree))]
    assert others == []
    if RESTRICTED.is_dir():  # the hooks live outside the shipped tree; where they are present they may not reach in
        hooks = [f for f, tree in _sources(RESTRICTED).items() if any(
            isinstance(n, ast.Attribute) and n.attr in ("_exempt", "_exempt_robots") for n in ast.walk(tree))]
        assert hooks == []


async def test_save_skill_refuses_a_recipe_that_carries_robots():
    from app.sites import skills

    recipe = {"kind": "request", "method": "GET", "url": "https://feed.example.test/api/jobs", "items": "data.results",
              "fields": {"title": "name", "url": "path"}, "url_prefix": "https://feed.example.test"}
    skills.parse_recipe(recipe)  # the recipe is valid without the field
    with pytest.raises(ValueError, match="robots"):
        await skills.save_skill(None, "owner", None,  # type: ignore[arg-type]
                                {**recipe, "robots": {"host": "feed.example.test", "path_prefix": "/"}})
