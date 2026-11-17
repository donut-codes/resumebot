"""The reads the page program serves now: the Agents page (who is here, the roster, one
agent, Activity), How they work with its files and its process versions, the agent kinds with their sessions and the
nightly cost, the Inbox, Tasks and Notifications, and the employers. Each is on the page's app and not on the
engine's. Everything is made up."""

from fastapi.routing import APIRoute

from app.jobsearch.factory import build_app

#: Each read as the manifest names it, "METHOD /path".
PAGE_READS = (
    "GET /api/v1/me",
    "GET /api/v1/agents",
    "GET /api/v1/agents/{connection_id}",
    "GET /api/v1/agents/{connection_id}/trail",
    "GET /api/v1/trail",
    "GET /api/v1/harness",
    "GET /api/v1/harness/steps/{step_id}/example",
    "GET /api/v1/harness/tasks/{task_id}",
    "GET /api/v1/harness/runs",
    "GET /api/v1/harness/runs/{instance_id}",
    "GET /api/v1/harness/files/{kind}/{name}",
    "GET /api/v1/harness/files/{kind}/{name}/versions/{version}",
    "GET /api/v1/harness/processes/{key}/bpmn",
    "POST /api/v1/harness/processes/{key}/validate",
    "GET /api/v1/harness/processes/{key}/versions",
    "GET /api/v1/harness/processes/{key}/versions/{number}",
    "GET /api/v1/inbox",
    "GET /api/v1/inbox/history",
    "GET /api/v1/tasks",
    "GET /api/v1/tasks/{task_id}",
    "GET /api/v1/notifications",
    "GET /api/v1/employers",
    "GET /api/v1/employers/{employer_id}",
    "GET /api/v1/employers/{employer_id}/merge",
    "GET /api/v1/employers/{employer_id}/split",
    "GET /api/v1/agent-kinds",
    "GET /api/v1/agent-kinds/sessions",
    "GET /api/v1/cost-nights",
)


def _keys(app) -> set[str]:
    """Every route of an app as "METHOD /path". FastAPI keeps an included router as one entry that points at the
    router, so the walk goes into it (as `_keys` in tests/split/test_route_programs.py does)."""
    keys: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            router = getattr(route, "original_router", None)
            if router is not None:
                walk(router.routes)
            elif isinstance(route, APIRoute):
                keys.update(f"{method} {route.path}" for method in route.methods)

    walk(app.routes)
    return keys


def test_the_page_serves_these_reads_and_the_engine_does_not():
    page, engine = _keys(build_app("page")), _keys(build_app("engine"))
    assert sorted(set(PAGE_READS) - page) == [], "reads the page does not serve"
    assert sorted(set(PAGE_READS) & engine) == [], "reads the engine still serves"
