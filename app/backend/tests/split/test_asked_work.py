"""The task answer, as the page program serves it: `POST /tasks/{id}/answer` writes the answer,
the task and any employer merge or split in the page, starts a search (`run.start_round`) in its transaction, and tells
the engine only through the door, after the commit. The other writes of the lane have a file each: `test_asked_seam.py`,
`test_asked_missions.py`, `test_asked_pool.py`. Everything here is made up."""

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db
from app.inbox import service as inbox
from app.jobsearch import page
from app.jobsearch.factory import build_app
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import SearchRun
from app.models.work import Task
from tests.split import asked_support
from tests.split.test_route_programs import _keys
from tests.support import OWNER, load_profile

ANSWER = "POST /api/v1/tasks/{task_id}/answer"


def test_the_page_serves_the_task_answer_and_the_engine_does_not():
    page_keys, engine_keys = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert ANSWER in page_keys
    assert ANSWER not in engine_keys


async def test_the_page_answers_a_decision_with_no_engine_and_rings_nothing_for_it(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    async with db.session_factory()() as session:
        task = await inbox.raise_task(
            session, OWNER, key="plan:1", title="Keep this plan?", kind="plain_question",
            options=[{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}])
        task_id = task.id
        await session.commit()
    with asked_support.the_engine(answers=False) as told:
        async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as http:
            answered = await http.post(f"/api/v1/tasks/{task_id}/answer", json={"option": "yes"})
    assert answered.status_code == 200
    assert answered.json()["state"] == "done" and answered.json()["answer"] == "yes"
    # The route tells the door there are no wakes to send (the door rings nothing for none); nothing else.
    assert [call[0] for call in told.told()] == ["send_wakes"]


async def test_search_again_on_the_page_starts_the_search_in_the_page_and_knocks_for_it(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER, started_by="manual", focus="banks")
        await run.fail_run(session, started, "The scout didn't finish this search, so it was closed. Search again.")
        await session.commit()
        task_id = (await session.scalars(
            select(Task.id).where(Task.owner == OWNER, Task.kind == "search_failed"))).one()
    asked_support.as_the_page(monkeypatch)  # after the set-up: from here the route runs as the page program
    with asked_support.the_engine(answers=False) as told:
        async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as http:
            answered = await http.post(f"/api/v1/tasks/{task_id}/answer", json={"option": "search_again"})
    assert answered.status_code == 200 and answered.json()["answer"] == "search_again"
    async with db.session_factory()() as session:
        opened = (await session.scalars(
            select(SearchRun.id).where(SearchRun.owner == OWNER, SearchRun.status == "open"))).all()
    assert len(opened) == 1, "the new search is the one open"
    assert ("knock", str(opened[0]), "search_started") in told.told()
