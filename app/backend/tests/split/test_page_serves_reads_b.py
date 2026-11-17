"""The page program serves the owner's reads of the profile, the documents, Where to look, the mail login, the role
checks, the parse jobs, the packet downloads and the export; the engine does not. The writes of the
same route modules are not named here: later lanes move them. Everything is made up."""

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from tests.support import OWNER

#: The reads this lane gave to the page, as the manifest names them ("METHOD /path").
READS = (
    "GET /api/v1/mail-login",
    "GET /api/v1/profile",
    "GET /api/v1/profile/comp-example",
    "GET /api/v1/profile/gates/stopped",
    "GET /api/v1/profile/roles/{role_id}/trend",
    "GET /api/v1/profile/{kind}/history",
    "GET /api/v1/profile/{kind}/versions/{number}",
    "GET /api/v1/sites",
    "GET /api/v1/sites/job-systems/{key}/roles",
    "GET /api/v1/sites/{site_id}/roles",
    "GET /api/v1/documents",
    "GET /api/v1/documents/resume/export.json",
    "GET /api/v1/documents/resume/versions/{number}/export.json",
    "GET /api/v1/documents/built-in-checks",
    "GET /api/v1/documents/story-library",
    "GET /api/v1/profile/check/{job_id}",
    "GET /api/v1/profile/fits-none",
    "POST /api/v1/profile/roles/from-role/{subject_id}",
    "GET /api/v1/profile/parse/{job_id}",
    "GET /api/v1/packets/{packet_id}/resume.pdf",
    "GET /api/v1/packets/{packet_id}/letter.pdf",
    "GET /api/v1/packets/{packet_id}/resume.docx",
    "GET /api/v1/packets/{packet_id}/letter.docx",
    "GET /api/v1/export.xlsx",
    "GET /export.xlsx",
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


@pytest.mark.parametrize("key", READS)
def test_the_page_serves_the_read_and_the_engine_does_not(key):
    from app.jobsearch import engine, page

    assert key in _keys(page.app)
    assert key not in _keys(engine.app)


async def test_the_page_app_answers_the_profile_the_sites_and_the_mail_login_from_the_database(js, monkeypatch):
    from app.jobsearch import page
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
        profile = await client.get("/api/v1/profile")
        documents = await client.get("/api/v1/documents")
        sites = await client.get("/api/v1/sites")
        login = await client.get("/api/v1/mail-login")
    assert profile.status_code == 200 and {"is_set", "versions", "says"} <= set(profile.json())
    assert documents.status_code == 200 and set(documents.json()["versions"]) == {"resume", "about", "writing"}
    assert sites.status_code == 200
    assert {"sites", "job_systems", "watch_in_play", "read_robots_exempt"} <= set(sites.json())
    assert login.status_code == 200
    assert (login.json()["connected"], login.json()["username"]) == (False, None)


async def test_the_page_app_answers_the_job_reads_and_the_downloads_from_the_database(js, monkeypatch):
    from app.jobsearch import page
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
        fits = await client.get("/api/v1/profile/fits-none")
        check = await client.get("/api/v1/profile/check/none-such")
        parse = await client.get("/api/v1/profile/parse/none-such")
        draft = await client.post("/api/v1/profile/roles/from-role/none-such")
        packet = await client.get("/api/v1/packets/00000000-0000-0000-0000-000000000000/resume.pdf")
        export = await client.get("/api/v1/export.xlsx")
        short = await client.get("/export.xlsx")
    assert fits.status_code == 200 and set(fits.json()) == {"pursued", "this_week"}
    assert (check.status_code, parse.status_code, draft.status_code, packet.status_code) == (404, 404, 404, 404)
    assert export.status_code == 200 and export.content[:2] == b"PK"
    assert short.status_code == 200 and short.content[:2] == b"PK"
