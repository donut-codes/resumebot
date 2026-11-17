"""The two router lists (`page.ROUTERS` and `engine.ROUTERS`) and the doors each program mounts (`page.DOORS`,
`engine.DOORS`): every route of the whole app is in exactly one list, the engine serves the lane doors and its own
`/health` and nothing else, and a program's app holds only what its lists give it. Everything is made up."""

import importlib

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from starlette.routing import Mount

from app.jobsearch import engine, factory, page
from app.jobsearch.factory import build_app
from app.settings import APP_NAME, VERSION

#: The routes the engine serves besides `/health`: the lane doors (hand-out, instructions, context, heartbeat, drain,
#: busy, tabs and reconcile, capabilities, seat). `/mcp` and `/mcp-browser` are the engine's mounts (`engine.DOORS`).
LANE_DOORS = frozenset({
    "GET /api/v1/agent/work", "GET /api/v1/agent/instructions", "GET /api/v1/agent/context",
    "POST /api/v1/agent/heartbeat", "POST /api/v1/agent/browser/drain", "GET /api/v1/agent/browser/drain",
    "GET /api/v1/agent/browser/busy", "GET /api/v1/agent/browser/tabs", "POST /api/v1/agent/browser/tabs",
    "DELETE /api/v1/agent/browser/tabs/{target_id}", "POST /api/v1/agent/browser/tabs/release",
    "POST /api/v1/agent/browser/tabs/reconcile", "POST /api/v1/agent/capabilities", "POST /api/v1/agent/seat/take",
    "POST /api/v1/agent/seat/{run_id}/answer",
})


def _keys(app) -> list[str]:
    """Every route of an app as "METHOD /path". FastAPI keeps an included router as one entry that points at the
    router, so the walk goes into it."""
    keys: list[str] = []

    def walk(routes) -> None:
        for route in routes:
            router = getattr(route, "original_router", None)
            if router is not None:
                assert not route.include_context.prefix, "a router included under a prefix"
                walk(router.routes)
            elif isinstance(route, APIRoute):
                keys.extend(f"{method} {route.path}" for method in sorted(route.methods))

    walk(app.routes)
    return keys


def _mounts(app) -> list[str]:
    return [route.path for route in app.routes if isinstance(route, Mount)]


def test_every_route_of_the_whole_app_is_in_exactly_one_list():
    whole, page_keys, engine_keys = (_keys(build_app(role)) for role in ("all", "page", "engine"))
    assert len(whole) == len(set(whole)), "a route is registered twice"
    assert set(page_keys) | set(engine_keys) == set(whole)
    assert set(page_keys) & set(engine_keys) == {"GET /health"}, "a route is in both lists"


def test_a_router_is_in_one_list_only_and_each_list_names_a_router_that_exists():
    listed = [*page.ROUTERS, *engine.ROUTERS]
    assert len(listed) == len(set(listed)), "a router is listed twice"
    for spec in listed:
        module, attribute = spec.split(":")
        router = getattr(importlib.import_module(module), attribute)
        assert isinstance(router, APIRouter), spec
        assert all(isinstance(route, APIRoute) for route in router.routes), spec


def test_the_engine_serves_the_lane_doors_and_its_own_health_and_nothing_else():
    assert set(_keys(build_app("engine"))) == LANE_DOORS | {"GET /health"}


def test_a_router_moved_between_the_lists_moves_its_routes_and_only_those(monkeypatch):
    spec = "app.agents.routes:router"
    moved = {"GET /api/v1/me", "GET /api/v1/agents", "GET /api/v1/agents/{connection_id}",
             "PATCH /api/v1/agents/{connection_id}", "GET /api/v1/agents/{connection_id}/trail", "GET /api/v1/trail"}
    before_page, before_engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert moved <= before_page - before_engine
    monkeypatch.setattr(page, "ROUTERS", tuple(item for item in page.ROUTERS if item != spec))
    monkeypatch.setattr(engine, "ROUTERS", (*engine.ROUTERS, spec))
    now_page, now_engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert now_page == before_page - moved
    assert now_engine == before_engine | moved


async def test_a_route_the_page_serves_answers_on_the_page_and_the_others_are_not_there():
    app = build_app("page")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8430") as client:
        served = await client.get("/api/v1/me")
        not_here = await client.get("/api/v1/agent/work")  # a lane door: the engine's, for good
    assert served.status_code == 200 and served.json()["version"] == VERSION
    assert not_here.status_code == 404


def test_every_door_is_served_by_exactly_one_program_and_the_whole_app_mounts_them_all():
    paths = [path for path, *_ in factory.MOUNTS]
    assert sorted([*page.DOORS, *engine.DOORS]) == sorted(paths)
    assert _mounts(build_app("all")) == paths


def test_the_owner_door_is_the_pages_and_the_other_two_are_the_engines():
    assert _mounts(build_app("page")) == ["/mcp-owner"]
    assert _mounts(build_app("engine")) == ["/mcp", "/mcp-browser"]


def test_a_door_is_mounted_only_by_the_program_that_lists_it(monkeypatch):
    monkeypatch.setattr(page, "DOORS", ("/mcp-owner", "/mcp-browser"))
    monkeypatch.setattr(engine, "DOORS", ("/mcp",))
    assert _mounts(build_app("page")) == ["/mcp-browser", "/mcp-owner"]
    assert _mounts(build_app("engine")) == ["/mcp"]


def test_a_door_is_built_once_so_every_app_of_the_process_mounts_the_same_one():
    everything = [route.app for route in build_app("all").routes if isinstance(route, Mount)]
    theirs = [route.app for role in ("page", "engine") for route in build_app(role).routes if isinstance(route, Mount)]
    assert everything and len(everything) == len(theirs)  # no door missing, none extra
    assert all(sum(door is other for other in everything) == 1 for door in theirs)  # the same object, mounted once
    assert all(sum(door is other for other in theirs) == 1 for door in everything)


def test_an_unknown_role_is_refused():
    with pytest.raises(ValueError, match="unknown program"):
        build_app("worker")


@pytest.mark.parametrize("role", ["all", "page", "engine"])
async def test_every_role_answers_health(js, role):
    async with AsyncClient(transport=ASGITransport(app=build_app(role)), base_url="http://localhost:8430") as client:
        reply = await client.get("/health")
    assert reply.status_code == 200
    assert reply.json() == {"app": APP_NAME, "status": "ok", "version": VERSION, "db": "ok"}


def test_the_whole_app_is_the_one_the_tests_and_serve_build_from():
    from app.jobsearch import main

    assert _keys(main.app) == _keys(build_app("all"))
    assert _mounts(main.app) == _mounts(build_app("all"))
