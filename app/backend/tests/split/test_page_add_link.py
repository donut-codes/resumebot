"""Adding a job by link on the page program: the page checks the text, asks the engine to add the job and answers at
once (PRD 24.10, 2026-11-16: the owner never waits for the engine); it never reads the link's page itself. The engine
is stood in for by a task that answers the ask when the page rings. Every name and address here is made up."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db
from app.jobsearch import facts, page
from app.jobsearch.factory import build_app
from app.jobsearch.models import JobPosting, Todo
from app.jobsearch.work import engine_asks, engine_calls
from app.jobsearch.work.queue import topics
from app.settings import settings
from tests.builders import analysis_setup
from tests.split.test_route_programs import _keys
from tests.support import NOON, OWNER

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

URL = "https://jobs.example.test/jobs/7001"
ANSWER = {"role": None, "posting": "0b6f0f0e-8a0e-4c3e-9d57-2d6f4f3a9c11", "already": False,
          "said": "Added, and waiting in the Added links list for its page to be read.", "stage": "added_links",
          "stage_label": "Added links", "title": "jobs.example.test/jobs/7001", "company": "Added link",
          "sourced": False}


@pytest.fixture
async def on_the_page(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    # In the whole app (what a test is) a registered ask runs on the spot with no row and no wait; the page's real path
    # is pinned here: the ask is a row and the page waits.
    monkeypatch.setattr(engine_asks, "is_registered", lambda name: False)
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://localhost:8430") as client:
        yield client


@pytest.fixture
def the_engine_answers_when_rung(monkeypatch):
    """The engine looks once each time the page rings, and answers every ask it finds (`engine_asks.answer_due`), as the
    dispatcher's handler does. Returns the list of its tasks, for the test to wait for at the end."""
    tasks: list[asyncio.Task] = []

    def ring() -> None:
        # The ask's commit rings too (`queue/hook.py`), and then the ask rings itself: while one look is under way a
        # second ring changes nothing, so the engine's function is never run twice for one ask.
        if all(task.done() for task in tasks):
            tasks.append(asyncio.get_running_loop().create_task(engine_asks.answer_due(OWNER, [topics.ANY], NOON)))

    monkeypatch.setattr(engine_calls, "nudge", ring)
    return tasks


def test_the_page_serves_the_add_a_job_route_and_the_engine_does_not():
    page_keys, engine_keys = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert "POST /api/v1/roles" in page_keys
    assert "POST /api/v1/roles" not in engine_keys


async def test_the_page_answers_at_once_that_the_link_was_added_and_the_engine_is_sent_the_pasted_text(
        on_the_page, the_engine_answers_when_rung, monkeypatch):
    """Old: the page waited up to twenty seconds and answered with what the engine answered. New (PRD 24.10): the page
    waits for nothing (`wait_s` 0), writes the posting itself and answers with it in Added links; the engine still
    gets the pasted text, and the posting to put through the gates."""
    seen: list[tuple] = []
    waited: list[float] = []
    real_ask = engine_calls.ask

    async def add(session, owner, args):
        seen.append((owner, args))
        return ANSWER

    async def ask(session, owner, name, args, wait_s):
        waited.append(wait_s)
        return await real_ask(session, owner, name, args, wait_s)

    monkeypatch.setitem(engine_asks._asks, "role.add_link", add)
    monkeypatch.setattr(engine_calls, "ask", ask)
    reply = await on_the_page.post("/api/v1/roles", json={"url": f"  {URL}  "})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert (body["role"], body["already"], body["stage"], body["stage_label"]) == (
        None, False, "added_links", "Added links")
    assert body["posting"]
    assert (body["title"], body["company"]) == ("jobs.example.test/jobs/7001", "Added link")
    assert body["said"].startswith("Added, and waiting in the Added links list")
    assert waited == [0]
    await asyncio.gather(*the_engine_answers_when_rung)
    assert seen == [(OWNER, {"url": f"  {URL}  ", "posting": body["posting"]})]


async def test_a_text_that_is_no_public_web_address_is_refused_with_422_and_no_ask_is_made(on_the_page, monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("the page asked the engine")

    monkeypatch.setattr(engine_calls, "ask", never)
    for text in ("not a link", "ftp://files.example.test/job", "http://127.0.0.1/job",
                 "https://user:secret@jobs.example.test/job/1"):
        reply = await on_the_page.post("/api/v1/roles", json={"url": text})
        assert reply.status_code == 422, (text, reply.text)
        assert reply.json()["detail"]
    async with db.read_session_factory()() as reader:
        assert await facts.get_many(reader, OWNER, "ask:") == {}


async def test_a_link_the_engine_refuses_in_words_when_it_answers_on_the_spot_is_a_422_with_those_words(
        on_the_page, monkeypatch):
    async def refused(session, owner, name, args, wait_s):
        return {"refused": "That address is not on the public internet, so the analyst could not read it."}

    monkeypatch.setattr(engine_calls, "ask", refused)
    reply = await on_the_page.post("/api/v1/roles", json={"url": URL})
    assert reply.status_code == 422
    assert reply.json()["detail"] == "That address is not on the public internet, so the analyst could not read it."


async def test_the_page_never_waits_and_never_says_the_worker_is_not_running_when_no_answer_comes(
        on_the_page, monkeypatch):
    """Old: it waited twenty seconds at most and answered 503 'not running' with a Retry-After. New: no wait, 200."""
    asked: list[tuple] = []

    async def no_answer(session, owner, name, args, wait_s):
        asked.append((owner, name, args, wait_s))
        return None

    monkeypatch.setattr(engine_calls, "ask", no_answer)
    reply = await on_the_page.post("/api/v1/roles", json={"url": URL})
    assert reply.status_code == 200, reply.text
    assert reply.json()["stage"] == "added_links" and "Retry-After" not in reply.headers
    assert [(o, n, a["url"], w) for o, n, a, w in asked] == [(OWNER, "role.add_link", URL, 0)]


async def test_with_no_engine_the_ask_is_kept_so_the_engine_adds_the_link_when_it_runs(on_the_page, monkeypatch):
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    reply = await on_the_page.post("/api/v1/roles", json={"url": URL})
    assert reply.status_code == 200, reply.text
    async with db.read_session_factory()() as reader:
        asks = await facts.get_many(reader, OWNER, "ask:")
        todos = (await reader.execute(select(Todo.topic, Todo.cause))).all()
    assert len(asks) == 1 and '"role.add_link"' in next(iter(asks.values()))
    assert (topics.ENGINE_ASK, "ask") in [tuple(row) for row in todos]


async def test_with_no_engine_the_posting_is_in_added_links_before_the_engine_runs(on_the_page, monkeypatch):
    """The posting row is written by the page (PRD 24.10), so a stopped engine still leaves the link in Added links."""
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    body = (await on_the_page.post("/api/v1/roles", json={"url": URL})).json()
    async with db.read_session_factory()() as reader:
        rows = (await reader.scalars(select(JobPosting).where(JobPosting.owner == OWNER))).all()
    assert [str(row.id) for row in rows] == [body["posting"]]
    assert (rows[0].company, rows[0].url) == ("Added link", URL)


async def test_with_no_engine_a_link_already_added_answers_already_with_its_posting_and_makes_no_second(
        on_the_page, monkeypatch):
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    first = (await on_the_page.post("/api/v1/roles", json={"url": URL})).json()
    again = (await on_the_page.post("/api/v1/roles", json={"url": URL})).json()
    assert (again["posting"], again["already"], again["stage"]) == (first["posting"], True, "added_links")
    async with db.read_session_factory()() as reader:
        assert len((await reader.scalars(select(JobPosting).where(JobPosting.owner == OWNER))).all()) == 1


async def test_with_no_engine_a_link_of_a_role_the_owner_has_answers_already_with_that_role(on_the_page, monkeypatch):
    monkeypatch.setattr(engine_calls, "nudge", lambda: None)
    ids = await analysis_setup()
    body = (await on_the_page.post("/api/v1/roles", json={"url": "https://jobs.example.test/fabrikam"})).json()
    assert (body["role"], body["already"]) == (str(ids["Fabrikam"]), True)
    assert body["said"].startswith("Programs Lead at Fabrikam is already on your pipeline")
