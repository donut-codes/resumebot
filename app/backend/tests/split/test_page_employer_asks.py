"""The page answers a merge of two employer records and a split of roles out of one: the page
saves the merge or the split, then asks the engine to work the gates and scores of the moved roles out again
(`engine_calls.ask`, "employer.refresh") and answers the employers as they are after. The page never works the roles
out. Every name here is made up."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db, notice
from app.jobsearch.employers.routes import ENGINE_WAIT_S
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import engine_calls
from app.settings import settings
from tests.employer_builders import make_employer, make_role
from tests.split.test_page_sites import Engine
from tests.split.test_page_writes import imports_recorded
from tests.split.writes_support import on_programs
from tests.support import OWNER

ROWS = {"POST /api/v1/employers/{employer_id}/merge", "POST /api/v1/employers/{employer_id}/split"}
MERGE_KEYS = {"merged", "employer", "rows", "removed", "counts"}
SPLIT_KEYS = {"created", "roles", "postings", "names", "still_gated_out", "employer", "rows", "counts"}


@pytest.fixture
def rang(monkeypatch):
    """The engine's door is the page's real one (`Remote`); each bell it rings is kept instead of sent."""
    kinds: list[str] = []
    monkeypatch.setattr(notice, "ring_engine", lambda kind, **fields: kinds.append(kind) or True)
    before = engine_calls._backend
    engine_calls.install(engine_calls.Remote())
    yield kinds
    engine_calls.install(before)


@pytest.fixture
async def page_http(js, monkeypatch, rang):
    """The page program's app over ASGI as `http://test`, acting for the owner `OWNER`."""
    from app.jobsearch import page

    monkeypatch.setattr(settings, "owner", OWNER)
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as http:
        yield http


async def write(http, method: str, url: str, **request):
    """One call of the page app with the imports recorded: (the reply, the forbidden modules it imported)."""
    with imports_recorded() as reached:
        reply = await http.request(method, url, **request)
    return reply, sorted(reached)


def the_page_never_works_the_roles_out(monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("the page worked the roles out")

    monkeypatch.setattr(employer_research, "after_a_change", never)


async def engine_marks_it(session, args):
    """What the engine writes on the employer while it works the roles out."""
    employer = await session.get(Employer, uuid.UUID(args["employer_ids"][0]))
    assert employer is not None
    employer.notes = "the engine was here"


async def _two_employers() -> tuple[uuid.UUID, uuid.UUID]:
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech")
        away = await make_employer(session, "Initrode")
        session.add(make_role(away, "Lead"))
        await session.flush()
        ids = keep.id, away.id
        await session.commit()
    return ids


async def _employer_with_two_roles() -> tuple[uuid.UUID, uuid.UUID]:
    async with db.session_factory()() as session:
        source = await make_employer(session, "Initech")
        moving = make_role(source, "Lead")
        session.add_all([moving, make_role(source, "Analyst")])
        await session.flush()
        ids = source.id, moving.id
        await session.commit()
    return ids


def test_the_page_serves_the_merge_and_the_split_and_the_engine_does_not():
    missing, still_there = on_programs(ROWS)
    assert missing == [], "rows the page does not serve"
    assert still_there == [], "rows the engine still serves"


async def test_a_merge_is_saved_then_the_engine_works_the_roles_out_and_the_employer_is_answered_as_it_is_after(
        page_http, monkeypatch):
    the_page_never_works_the_roles_out(monkeypatch)
    keep_id, away_id = await _two_employers()
    engine = Engine(monkeypatch, work=engine_marks_it, answer={"refreshed": True})
    reply, imported = await write(page_http, "POST", f"/api/v1/employers/{away_id}/merge", json={"into": str(keep_id)})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert set(body) == MERGE_KEYS
    assert body["merged"] is True and body["removed"] == [str(away_id)]
    assert body["employer"]["id"] == str(keep_id) and [r["id"] for r in body["rows"]] == [str(keep_id)]
    assert body["employer"]["notes"] == "the engine was here", "the employer is read again once the engine is done"
    assert imported == []
    cause = f"{body['employer']['name']} was merged with another record of it"
    assert engine.asked == [("employer.refresh", {"employer_ids": [str(keep_id)], "cause": cause,
                                                  "keep_gated_out": False}, ENGINE_WAIT_S)]
    async with db.session_factory()() as session:
        assert await session.get(Employer, away_id) is None


async def test_with_no_answer_the_merge_is_saved_and_answered_and_a_merge_asked_again_asks_nothing(
        page_http, monkeypatch):
    the_page_never_works_the_roles_out(monkeypatch)
    keep_id, away_id = await _two_employers()
    engine = Engine(monkeypatch)  # the engine answers nothing
    first = await page_http.post(f"/api/v1/employers/{away_id}/merge", json={"into": str(keep_id)})
    assert first.status_code == 200 and first.json()["merged"] is True
    again = await page_http.post(f"/api/v1/employers/{away_id}/merge", json={"into": str(keep_id)})
    assert again.status_code == 200 and again.json()["merged"] is False
    assert [name for name, _, _ in engine.asked] == ["employer.refresh"]
    async with db.session_factory()() as session:
        roles = list(await session.scalars(select(Opportunity.employer_id)))
    assert roles == [keep_id], "the role moved over although nobody worked the roles out"


async def test_a_merge_that_cannot_be_made_says_why_and_asks_the_engine_nothing(page_http, monkeypatch):
    keep_id, _ = await _two_employers()
    engine = Engine(monkeypatch)
    same = await page_http.post(f"/api/v1/employers/{keep_id}/merge", json={"into": str(keep_id)})
    missing = await page_http.post(f"/api/v1/employers/{keep_id}/merge", json={"into": str(uuid.uuid4())})
    assert same.status_code == 422 and missing.status_code == 404
    assert engine.asked == []


async def test_a_split_is_saved_then_the_engine_works_the_roles_out_and_both_employers_are_answered(
        page_http, monkeypatch):
    the_page_never_works_the_roles_out(monkeypatch)
    source_id, role_id = await _employer_with_two_roles()
    engine = Engine(monkeypatch, work=engine_marks_it, answer={"refreshed": True})
    request = {"roles": [str(role_id)], "name": "Fabrikam"}
    reply, imported = await write(page_http, "POST", f"/api/v1/employers/{source_id}/split", json=request)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert set(body) == SPLIT_KEYS
    assert body["created"] is True and body["roles"] == 1 and body["employer"]["name"] == "Fabrikam"
    target_id = body["employer"]["id"]
    assert {row["id"] for row in body["rows"]} == {str(source_id), target_id}
    assert body["employer"]["notes"] == "the engine was here"
    assert imported == []
    assert engine.asked == [("employer.refresh", {
        "employer_ids": [target_id], "cause": "Roles were split out of Initech into Fabrikam", "keep_gated_out": True,
    }, ENGINE_WAIT_S)]


async def test_with_no_answer_the_split_is_saved_and_answered(page_http, monkeypatch):
    the_page_never_works_the_roles_out(monkeypatch)
    source_id, role_id = await _employer_with_two_roles()
    Engine(monkeypatch)
    reply = await page_http.post(f"/api/v1/employers/{source_id}/split",
                                 json={"roles": [str(role_id)], "name": "Fabrikam"})
    assert reply.status_code == 200, reply.text
    async with db.session_factory()() as session:
        moved = await session.get(Opportunity, role_id)
        target = await session.scalar(select(Employer).where(Employer.name == "Fabrikam"))
    assert moved is not None and target is not None and moved.employer_id == target.id


async def test_a_split_that_cannot_be_made_says_why_and_asks_the_engine_nothing(page_http, monkeypatch):
    source_id, _ = await _employer_with_two_roles()
    engine = Engine(monkeypatch)
    other = await page_http.post(f"/api/v1/employers/{source_id}/split",
                                 json={"roles": [str(uuid.uuid4())], "name": "Fabrikam"})
    assert other.status_code == 422 and other.json()["detail"] == "no such role"
    assert engine.asked == []
