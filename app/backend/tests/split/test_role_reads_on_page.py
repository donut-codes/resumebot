"""The page program serves the role reads (`routes/role.py`): the board, the favorites, the search, one role whole, one
role's row and where Launch in Browser stands; and the task, notification and packet reads. The engine holds none of
them. Everything here is made up."""

from httpx import ASGITransport, AsyncClient

from app import db
from app.jobsearch import engine, page, stages
from app.jobsearch.models import Opportunity
from tests.support import NOON, OWNER, pin_clock

READS = {
    "GET /api/v1/board", "GET /api/v1/favorites", "GET /api/v1/apply/go", "GET /api/v1/search",
    "GET /api/v1/roles/{role_id}", "GET /api/v1/roles/{role_id}/row",
    "GET /api/v1/tasks", "GET /api/v1/tasks/{task_id}", "GET /api/v1/notifications",
    "GET /api/v1/packets/{packet_id}/resume.pdf", "GET /api/v1/packets/{packet_id}/letter.pdf",
    "GET /api/v1/packets/{packet_id}/resume.docx", "GET /api/v1/packets/{packet_id}/letter.docx"}


def _keys(app) -> set[str]:
    """Every route of an app as the manifest names it, "METHOD /path". FastAPI keeps an included router as one entry
    that points at the router, so the walk goes into it (as `_keys` in tests/split/test_route_programs.py does)."""
    keys: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            router = getattr(route, "original_router", None)
            if router is not None:
                walk(router.routes)
            elif getattr(route, "methods", None):
                keys.update(f"{method} {route.path}" for method in route.methods)

    walk(app.routes)
    return keys


def test_the_page_holds_these_reads_and_the_engine_holds_none_of_them():
    assert READS <= _keys(page.app)
    assert not READS & _keys(engine.app)


async def test_the_page_answers_each_role_read_with_the_engine_app_absent(js, monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)
    pin_clock(monkeypatch, NOON)
    role = Opportunity(owner=OWNER, slug="p1", company="Co p1", title="Role p1", status=stages.READY_TO_LAUNCH,
                       origin="tracker", match_key="co p1|role p1", posted=NOON.date(), found=NOON.date(),
                       favorited_at=NOON)
    async with db.session_factory()() as session:
        session.add(role)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
        board = await client.get("/api/v1/board")
        favorites = await client.get("/api/v1/favorites")
        found = await client.get("/api/v1/search", params={"q": "p1"})
        whole = await client.get(f"/api/v1/roles/{role.id}")
        row = await client.get(f"/api/v1/roles/{role.id}/row")
        go = await client.get("/api/v1/apply/go")
    assert board.status_code == 200 and set(board.json()) == {"columns", "ended_reasons"}
    assert [card["title"] for card in favorites.json()["cards"]] == ["Role p1"]
    assert found.status_code == 200
    assert whole.status_code == 200 and whole.json()["role"]["title"] == "Role p1"
    assert row.status_code == 200 and row.json()["title"] == "Role p1"
    assert go.status_code == 200 and go.json()["phase"] == "idle" and go.json()["done"] is True
