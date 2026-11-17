"""The engine's half of a profile save: the ask `profile.after_save` checks the roles
against a new Hard pass again and works the scores out again after a new Prioritization, and saves the sentence that
says what it did as the answer and as the fact `said:<kind>:<version>`. Every name, number and sentence is made up."""

import json
from datetime import UTC, datetime

from app import db
from app.jobsearch import facts
from app.jobsearch.profile import after_save
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import engine_asks
from app.timeutil import iso_utc
from tests.support import FIVE, OWNER, PROFILE, load_profile

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
ID = "p1f2a3"
NOTHING_TO_CHECK = "Re-checked 0 postings and 0 roles: nothing changed."
REWORDED = {**PROFILE["gates"], "other": [{**PROFILE["gates"]["other"][0], "text": "Travel of 40% or more."}]}
BIGGER = {"asks": [{**FIVE["asks"][0], "points": 4}, *FIVE["asks"][1:]]}


async def _loaded(**parts) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, **parts)
        await session.commit()


async def _saved(kind: str, data: dict) -> int | None:
    """The version the page's route stores: the follow-up left to the engine (`defer_works_out`), committed."""
    async with db.session_factory()() as session:
        version = await profile_store.save(session, OWNER, kind, data, by="test", defer_works_out=True)
        await session.commit()
    return version


async def _asked(kind: str, version: int | None, how: str = "was saved") -> None:
    ask = {"name": "profile.after_save", "args": {"kind": kind, "version": version, "how": how},
           "asked_at": iso_utc(NOW)}
    async with db.session_factory()() as session:
        await facts.put(session, OWNER, f"ask:{ID}", json.dumps(ask), now=NOW)
        await session.commit()


async def _get(key: str) -> str | None:
    async with db.read_session_factory()() as reader:
        return await facts.get(reader, OWNER, key)


async def _answer() -> dict:
    raw = await _get(f"answer:{ID}")
    assert raw is not None
    return json.loads(raw)


def test_the_engine_answers_the_ask_called_profile_after_save():
    assert "profile.after_save" in engine_asks._asks


async def test_a_new_hard_pass_is_checked_against_every_role_again_and_the_sentence_is_the_answer_and_a_fact(js):
    await _loaded()
    version = await _saved("gates", REWORDED)
    assert version == 2
    await _asked("gates", version)
    assert await engine_asks.answer_due(OWNER, [ID], NOW) == 1
    assert await _answer() == {"said": NOTHING_TO_CHECK}
    assert await _get("said:gates:2") == NOTHING_TO_CHECK


async def test_a_new_prioritization_has_its_scores_worked_out_again_by_the_engine(js, monkeypatch):
    await _loaded(scoring=FIVE)
    version = await _saved("scoring", BIGGER)
    assert version == 2
    calls: list[tuple] = []

    async def scores(session, owner, kind, version):
        calls.append((owner, kind, version))
        return "Worked out 2 scores again: 1 changed."

    monkeypatch.setattr(profile_store, "works_out_again", scores)
    await _asked("scoring", version)
    await engine_asks.answer_due(OWNER, [ID], NOW)
    assert calls == [(OWNER, "scoring", 2)]
    assert await _answer() == {"said": "Worked out 2 scores again: 1 changed."}
    assert await _get("said:scoring:2") == "Worked out 2 scores again: 1 changed."


async def test_a_part_that_nothing_follows_is_answered_with_an_empty_sentence_and_still_said(js):
    await _loaded()
    version = await _saved("criteria", {**PROFILE["criteria"], "positioning": "A director who builds things."})
    assert version == 2
    await _asked("criteria", version)
    await engine_asks.answer_due(OWNER, [ID], NOW)
    assert await _answer() == {"said": ""}
    assert await _get("said:criteria:2") == ""


async def test_the_line_of_the_scores_comes_before_the_line_of_the_check_and_how_the_version_came_is_passed_on(
        js, monkeypatch):
    await _loaded()
    seen: list[tuple] = []

    async def check(session, owner, kind, version, how, said):
        seen.append((kind, version, how))
        said.insert(0, "Checked made-up roles.")

    async def scores(session, owner, kind, version):
        return "Worked out made-up scores."

    monkeypatch.setattr(after_save, "run", check)
    monkeypatch.setattr(profile_store, "works_out_again", scores)
    how = "was made by bringing version 1 back"
    await _asked("gates", 1, how)
    await engine_asks.answer_due(OWNER, [ID], NOW)
    assert seen == [("gates", 1, how)]
    assert await _answer() == {"said": "Worked out made-up scores. Checked made-up roles."}
