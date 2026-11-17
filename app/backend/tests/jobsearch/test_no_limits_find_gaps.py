"""No character limit on what the owner writes in Find Gaps (the owner, 2026-10-14: a length limit on what they type is
"a terrible feature"): what they did, in their own words, and the title of a role that is not one of their targets.
Both are
kept whole and handed to the agent whole. What stays is the floor (ten characters of words), and the one refusal for a
text an agent cannot be sent: past the room a reply leaves for it, the words are refused in words, with their size, and
nothing is saved or cut. Synthetic data only."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db
from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.gaps import tools as gap_tools
from app.jobsearch.main import app
from app.jobsearch.models import GapAnswerRow, GapDraft, GapRun, Opportunity
from app.jobsearch.owner_text import ROOM_CHARS, too_large_words
from app.settings import settings
from tests.builders import MINUTE, criteria, parse_runner, save_documents, tab
from tests.support import OWNER, load_profile

LONG = 20_000


def _words(tag: str, n: int = LONG) -> str:
    """About `n` characters of made-up plain sentences, each different, so a cut anywhere shows."""
    out, i = [], 0
    while sum(len(s) + 1 for s in out) < n:
        i += 1
        out.append(f"{tag} sentence {i} says one more thing the owner did.")
    return " ".join(out)[: n - 1] + "!"  # exactly n characters, and no space at either end for a save to trim


@pytest.fixture
async def client(js, monkeypatch, knocks):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=criteria(tab("builders"), tab("planners")))
        await save_documents(session)
        await session.commit()
    await parse_runner(seen=MINUTE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _refs(n: int) -> list[str]:
    return [f"p{i}" for i in range(1, n + 1)]


def _theme(name: str, asked: list[str], grade: str = "P", kind: str = "required") -> dict:
    return {"name": name, "kind": kind, "asked_by": asked, "grade": grade, "from": ["b-risk"] if grade != "U" else [],
            "why": ""}


async def _take() -> dict:
    async with db.session_factory()() as session:
        reply = await gap_tools.task_reply(session, OWNER)
        await session.commit()
        return reply


async def _report(run_id: str, themes: list[dict], postings: list[dict] | None = None) -> dict:
    async with db.session_factory()() as session:
        reply = await gap_tools.report_reply(session, OWNER, run_id, themes, postings)
        await session.commit()
        return reply


async def _finished_run() -> str:
    """A run for a target role that has ten stored postings, asked for, taken and reported: its id. One of its themes is
    "Data catalog tooling"."""
    async with db.session_factory()() as session:
        for i in range(10):
            company = f"Employer {i + 1}"
            session.add(Opportunity(
                owner=OWNER, slug=f"e{i}", company=company, title="Programs Lead", origin="scout",
                match_key=f"{company.lower()}|programs lead", status="second_impression", target_role="builders",
                url=f"https://jobs.example.test/{i}", description=f"Required: five years of programs. Posting {i}."))
        await session.commit()
    async with db.session_factory()() as session:
        run = await find_gaps.start_role(session, OWNER, "builders")
        await session.commit()
        run_id = str(run.id)
    await _take()
    await _report(run_id, [_theme("Programs at scale", _refs(10)),
                           _theme("Data catalog tooling", _refs(6), "U", kind="preferred")])
    return run_id


def _draft_path(run_id: str) -> str:
    return f"/api/v1/find-gaps/runs/{run_id}/themes/data-catalog-tooling/draft"


async def _drafts() -> list[GapDraft]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(GapDraft))).all())


# --- what the owner did, in their own words ---


async def test_words_of_any_length_are_stored_whole_and_handed_to_the_agent_whole(client):  # 28
    run_id = await _finished_run()
    words = _words("did")
    asked = await client.post(_draft_path(run_id), json={"words": words})
    assert asked.status_code == 200, asked.text
    [row] = await _drafts()
    assert row.words == words and len(row.words) == LONG
    handed = await _take()
    assert handed["mode"] == "draft" and handed["id"] == asked.json()["id"]
    assert handed["words"] == words, "jobs.gaps hands the agent their words as they wrote them"


async def test_words_over_the_room_are_refused_in_words_and_make_no_row_while_the_room_itself_passes(client):  # 28
    run_id = await _finished_run()
    refused = await client.post(_draft_path(run_id), json={"words": "x" * (ROOM_CHARS + 1)})
    assert refused.status_code == 413
    assert refused.json() == {"detail": too_large_words("What you wrote", ROOM_CHARS + 1)}
    assert "What you wrote is 160,001 characters." in refused.json()["detail"]
    assert await _drafts() == [], "nothing was saved"
    fits = await client.post(_draft_path(run_id), json={"words": "x" * ROOM_CHARS})
    assert fits.status_code == 200, fits.text
    [row] = await _drafts()
    assert len(row.words) == ROOM_CHARS


async def test_nine_characters_are_still_too_few_and_ten_are_enough(client):  # 28
    run_id = await _finished_run()
    short = await client.post(_draft_path(run_id), json={"words": "x" * 9})
    assert short.status_code == 422
    assert "at least 10 characters" in short.json()["detail"]
    assert await _drafts() == []
    assert (await client.post(_draft_path(run_id), json={"words": "x" * 10})).status_code == 200


# --- the title of a role that is not a target ---


def _found(n: int = 6) -> list[dict]:
    return [{"employer": f"Firm {i}", "title": "Head of Programs", "url": f"https://careers.example.test/{i}"}
            for i in range(1, n + 1)]


async def test_a_title_of_five_thousand_characters_makes_a_run_with_the_title_whole_and_its_answers_save(client):  # 29
    title = _words("title", 5_000)
    started = await client.post("/api/v1/find-gaps/runs", json={"title": title})
    assert started.status_code == 200, started.text
    run_id = started.json()["id"]
    async with db.session_factory()() as session:
        run = await session.get(GapRun, uuid.UUID(run_id))
    assert (run.kind, run.title) == ("new", title)
    task = await _take()
    assert task["mode"] == "search"
    await _report(run_id, [_theme("Programs", _refs(6)), _theme("Budgets", _refs(2), "U")], _found(6))
    view = (await client.get(f"/api/v1/find-gaps/runs/{run_id}")).json()
    assert view["state"] == "done" and view["title"] == title, "the page is given the title whole"
    answered = await client.put(f"/api/v1/find-gaps/runs/{run_id}/themes/{view['themes'][0]['id']}/answer",
                                json={"answer": "not_done"})
    assert answered.status_code == 200, answered.text
    async with db.session_factory()() as session:
        saved = (await session.scalars(select(GapAnswerRow))).one()
    assert saved.scope.startswith("new:") and len(saved.scope) < 120, "the scope is an id: it stays under its column"
