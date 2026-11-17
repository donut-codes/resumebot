"""The engine's side of adding a job by link: the ask `role.add_link` runs `add_link.add` for the link the page sent and
answers what it answers, or says in words why a link is refused. Every name and address here is made up."""

from app import db
from app.jobsearch.browser import add_link, link_text
from app.jobsearch.work import engine_asks
from tests.support import OWNER

URL = "https://jobs.example.test/jobs/7001"
ANSWER = {"role": None, "posting": "0b6f0f0e-8a0e-4c3e-9d57-2d6f4f3a9c11", "already": False,
          "said": "Added, and waiting in the Added links list for its page to be read.", "stage": "added_links",
          "stage_label": "Added links", "title": "jobs.example.test/jobs/7001", "company": "Added link",
          "sourced": False}
WORDS = "That is not a web address. Paste the link to the job's page, starting with https://."


def test_the_engine_answers_the_ask_called_role_add_link():
    assert "role.add_link" in engine_asks._asks


async def test_the_ask_adds_the_link_with_add_link_and_returns_its_answer_unchanged(js, monkeypatch):
    seen: list[tuple] = []

    async def add(session, owner, text, posting_id=None):
        seen.append((owner, text, posting_id))
        return ANSWER

    monkeypatch.setattr(add_link, "add", add)
    async with db.session_factory()() as session:
        got = await engine_asks._asks["role.add_link"](session, OWNER, {"url": URL})
    assert got == ANSWER
    assert seen == [(OWNER, URL, None)]


async def test_the_ask_passes_the_posting_the_page_made_so_it_is_only_put_through_the_gates(js, monkeypatch):
    """New (PRD 24.10): the page writes the posting of a new link before it asks, and names it in the ask."""
    seen: list[tuple] = []

    async def add(session, owner, text, posting_id=None):
        seen.append((text, posting_id))
        return ANSWER

    monkeypatch.setattr(add_link, "add", add)
    async with db.session_factory()() as session:
        await engine_asks._asks["role.add_link"](session, OWNER, {"url": URL, "posting": ANSWER["posting"]})
    assert seen == [(URL, ANSWER["posting"])]


async def test_a_link_add_link_refuses_is_answered_as_refused_in_its_own_words(js, monkeypatch):
    async def add(session, owner, text, posting_id=None):
        raise link_text.LinkError(WORDS)

    monkeypatch.setattr(add_link, "add", add)
    async with db.session_factory()() as session:
        got = await engine_asks._asks["role.add_link"](session, OWNER, {"url": "not a link"})
    assert got == {"refused": WORDS}
