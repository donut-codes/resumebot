"""The page answers the role writes that start work: asking research of a role and correcting a
requirement's class. The page saves what is the owner's (the correction and its line) and asks the engine
(`engine_calls.ask`) for the rest, then answers the role as it is after the engine's work; the page never researches
and never settles a role. Every name here is made up."""

import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app import db, notice
from app.jobsearch import facts, stages
from app.jobsearch.models import Opportunity, StageMove
from app.jobsearch.research import analysis
from app.jobsearch.routes import role as role_routes
from app.jobsearch.work import engine_asks, engine_calls
from app.models.work import TrailEvent
from app.settings import settings
from tests.split.test_page_sites import Engine
from tests.split.test_page_writes import imports_recorded
from tests.split.writes_support import on_programs
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

RESEARCH_AND_CLASS = {"POST /api/v1/roles/{role_id}/research", "POST /api/v1/roles/{role_id}/requirements"}
REQUIRED = {"required": [{"item": "Manage a team of ten people", "class": "T"}]}


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


async def _add(slug: str = "r1", **fields) -> uuid.UUID:
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", fit=3, origin="scout",
                           match_key=f"co {slug}|role {slug}", found=date(2026, 11, 4), source_kind="board",
                           **{"status": stages.SECOND_IMPRESSION, **fields})
        session.add(role)
        await session.flush()
        role_id = role.id
        await session.commit()
    return role_id


def test_the_page_serves_research_and_the_class_correction_and_the_engine_does_not():
    missing, still_there = on_programs(RESEARCH_AND_CLASS)
    assert missing == [], "rows the page does not serve"
    assert still_there == [], "rows the engine still serves"


# --- research -------------------------------------------------------------------------------------------------------


async def test_research_asks_the_engine_and_answers_what_it_said_and_the_page_imports_none_of_its_code(
        page_http, monkeypatch):
    role_id = await _add()
    said = {"role": str(role_id), "state": "waiting", "already": False, "again": False,
            "said": "Role r1 at Co r1 is queued for research."}
    engine = Engine(monkeypatch, answer={"asked": said})
    reply, imported = await write(page_http, "POST", f"/api/v1/roles/{role_id}/research")
    assert reply.status_code == 200, reply.text
    assert reply.json() == said
    assert imported == []
    assert engine.asked == [("role.research", {"role_id": str(role_id)}, role_routes.ROLE_WAIT_S)]


async def test_a_refusal_the_engine_gave_is_said_again_with_its_status_and_words(page_http, monkeypatch):
    Engine(monkeypatch, answer={"refused": {"status": 404, "message": "no such role"}})
    reply = await page_http.post(f"/api/v1/roles/{uuid.uuid4()}/research")
    assert reply.status_code == 404
    assert reply.json()["detail"] == "no such role"


async def test_with_the_engine_down_research_says_the_worker_is_not_running_and_the_ask_is_kept(
        page_http, monkeypatch):
    role_id = await _add()
    monkeypatch.setattr(engine_asks, "is_registered", lambda name: False)  # the real path: a row, and a wait
    monkeypatch.setattr(role_routes, "ROLE_WAIT_S", 0.05)
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    reply = await page_http.post(f"/api/v1/roles/{role_id}/research")
    assert reply.status_code == 503
    assert "isn't running" in reply.json()["detail"]
    assert reply.headers["Retry-After"]
    async with db.read_session_factory()() as reader:
        asks = await facts.get_many(reader, OWNER, "ask:")
    assert len(asks) == 1 and '"role.research"' in next(iter(asks.values()))


# --- the class of a required item -----------------------------------------------------------------------------------


async def engine_marks_it(session, args):
    """What the engine writes on the role while it settles."""
    role = await session.get(Opportunity, uuid.UUID(args["role_id"]))
    assert role is not None
    role.analysis = {**role.analysis, "notes": "the engine was here"}


async def test_a_corrected_class_is_saved_then_the_engine_settles_and_the_role_is_answered_as_it_is_after(
        page_http, monkeypatch):
    role_id = await _add(analysis_state=analysis.DONE, analysis=REQUIRED)
    engine = Engine(monkeypatch, work=engine_marks_it, answer={"said": "Moved to Gated out: pay."})
    body = {"index": 0, "class": "P", "why": "I did it"}
    reply, imported = await write(page_http, "POST", f"/api/v1/roles/{role_id}/requirements", json=body)
    assert reply.status_code == 200, reply.text
    shown = reply.json()
    assert shown["required"][0]["class"] == "P" and shown["required"][0]["by"] == "owner"
    assert shown["notes"] == "the engine was here", "the role is read again once the engine has settled it"
    assert shown["said"] == "Moved to Gated out: pay."
    assert imported == []
    cause = analysis.CORRECTED_CAUSE
    assert engine.asked == [("role.settle", {"role_id": str(role_id), "cause": cause}, role_routes.ROLE_WAIT_S)]
    async with db.session_factory()() as session:
        events = list(await session.scalars(select(TrailEvent.event).where(TrailEvent.link_id == role_id)))
    assert events.count("requirement_corrected") == 1


async def test_with_no_answer_the_correction_is_saved_and_the_role_is_answered_without_a_line(page_http, monkeypatch):
    role_id = await _add(analysis_state=analysis.DONE, analysis=REQUIRED)
    Engine(monkeypatch)
    reply = await page_http.post(f"/api/v1/roles/{role_id}/requirements",
                                 json={"index": 0, "class": "U", "why": ""})
    assert reply.status_code == 200, reply.text
    assert reply.json()["required"][0]["class"] == "U" and "said" not in reply.json()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
    assert role is not None and role.analysis["required"][0]["class"] == "U"


@pytest.mark.parametrize(("body", "status"), [
    ({"index": 5, "class": "P", "why": ""}, 404),
    ({"index": 0, "class": "X", "why": ""}, 422),
])
async def test_a_correction_that_cannot_be_made_asks_the_engine_nothing(page_http, monkeypatch, body, status):
    role_id = await _add(analysis_state=analysis.DONE, analysis=REQUIRED)
    engine = Engine(monkeypatch)
    reply = await page_http.post(f"/api/v1/roles/{role_id}/requirements", json=body)
    assert reply.status_code == status, reply.text
    assert engine.asked == []


# --- a move by hand, a move back and the star ------------------------------------------------------------------------

MOVES_AND_STARS = {
    "POST /api/v1/roles/{role_id}/move",
    "POST /api/v1/roles/{role_id}/move-back",
    "PUT /api/v1/roles/{role_id}/favorite",
}
STAR_KEYS = {"favorite", "changed", "moved", "source", "rank", "research", "packet", "role", "counts"}
NOT_LAUNCHED = {"moved": False, "source": False, "rank": False, "research": None, "packet": None}


def test_the_page_serves_the_moves_and_the_star_and_the_engine_does_not():
    missing, still_there = on_programs(MOVES_AND_STARS)
    assert missing == [], "rows the page does not serve"
    assert still_there == [], "rows the engine still serves"


async def test_a_move_that_holds_nothing_for_a_read_is_saved_and_answered_and_asks_the_engine_nothing(
        page_http, monkeypatch):
    role_id = await _add()
    engine = Engine(monkeypatch)
    body = {"to": "applied", "note": "applied on their site"}
    reply, imported = await write(page_http, "POST", f"/api/v1/roles/{role_id}/move", json=body)
    assert reply.status_code == 200, reply.text
    assert reply.json()["status"] == "applied" and "counts" in reply.json()
    assert imported == []
    assert engine.asked == []
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
    assert role is not None and role.status == "applied" and "applied on their site" in role.notes


async def test_a_move_to_ready_to_launch_of_a_role_not_read_is_held_and_the_engine_is_asked_for_its_reads(
        page_http, monkeypatch):
    role_id = await _add()
    engine = Engine(monkeypatch, answer={"done": None})
    reply, imported = await write(page_http, "POST", f"/api/v1/roles/{role_id}/move", json={"to": "ready_to_launch"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["status"] == stages.SECOND_IMPRESSION, "held: the role stays where it is until it is read"
    assert imported == []
    assert engine.asked == [("role.read_first", {"role_id": str(role_id)}, role_routes.ROLE_WAIT_S)]
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
    assert role is not None and role.status == stages.SECOND_IMPRESSION and role.ready_asked_at is not None


async def test_with_no_answer_the_held_move_is_still_saved_and_answered(page_http, monkeypatch):
    role_id = await _add()
    Engine(monkeypatch)  # the engine answers nothing
    reply = await page_http.post(f"/api/v1/roles/{role_id}/move", json={"to": "ready_to_launch"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["status"] == stages.SECOND_IMPRESSION
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
    assert role is not None and role.ready_asked_at is not None


async def test_a_move_that_cannot_be_made_says_why_and_asks_the_engine_nothing(page_http, monkeypatch):
    role_id = await _add()
    engine = Engine(monkeypatch)
    refused = await page_http.post(f"/api/v1/roles/{role_id}/move", json={"to": "gated_out"})
    missing = await page_http.post(f"/api/v1/roles/{uuid.uuid4()}/move", json={"to": "applied"})
    assert refused.status_code == 409 and missing.status_code == 404
    assert engine.asked == []


async def test_a_closed_role_is_moved_back_and_answered_with_no_ask(page_http, monkeypatch):
    role_id = await _add(status=stages.REJECTED)
    async with db.session_factory()() as session:  # no move records where it came from: it goes to First Impression
        await session.execute(update(StageMove).where(StageMove.role_id == role_id).values(from_status=None))
        await session.commit()
    engine = Engine(monkeypatch)
    reply, imported = await write(page_http, "POST", f"/api/v1/roles/{role_id}/move-back")
    assert reply.status_code == 200, reply.text
    assert reply.json()["status"] == stages.FIRST_IMPRESSION and "counts" in reply.json()
    assert imported == []
    assert engine.asked == []
    refused = await page_http.post(f"/api/v1/roles/{await _add('r2')}/move-back")
    assert refused.status_code == 409


async def test_a_star_is_saved_and_the_engine_is_asked_to_fast_track_it(page_http, monkeypatch):
    role_id = await _add()
    done = {"moved": False, "source": False, "rank": True, "research": None, "packet": None}
    engine = Engine(monkeypatch, answer={"done": done})
    reply, imported = await write(page_http, "PUT", f"/api/v1/roles/{role_id}/favorite", json={"favorite": True})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert set(body) == STAR_KEYS
    assert body["favorite"] is True and body["changed"] is True and body["rank"] is True
    assert body["role"]["favorite"] is True
    assert imported == []
    assert engine.asked == [("role.fast_track", {"role_id": str(role_id)}, role_routes.ROLE_WAIT_S)]


async def test_with_no_answer_the_star_is_saved_and_answered_with_nothing_launched(page_http, monkeypatch):
    role_id = await _add()
    Engine(monkeypatch)
    reply = await page_http.put(f"/api/v1/roles/{role_id}/favorite", json={"favorite": True})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert {k: body[k] for k in NOT_LAUNCHED} == NOT_LAUNCHED
    assert body["favorite"] is True and body["changed"] is True and body["role"]["favorite"] is True
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
    assert role is not None and role.favorited_at is not None


@pytest.mark.parametrize(("fields", "favorite"), [
    ({"favorited_at": datetime(2026, 11, 4, 12, tzinfo=UTC)}, False),  # unstarring
    ({"favorited_at": datetime(2026, 11, 4, 12, tzinfo=UTC)}, True),  # starring a favorite again changes nothing
    ({"status": stages.APPLIED}, True),  # a role past the application is starred and fast-tracked by nobody
])
async def test_a_star_that_has_nothing_to_fast_track_asks_the_engine_nothing(page_http, monkeypatch, fields, favorite):
    role_id = await _add(**fields)
    engine = Engine(monkeypatch)
    reply = await page_http.put(f"/api/v1/roles/{role_id}/favorite", json={"favorite": favorite})
    assert reply.status_code == 200, reply.text
    assert set(reply.json()) == STAR_KEYS
    assert engine.asked == []
