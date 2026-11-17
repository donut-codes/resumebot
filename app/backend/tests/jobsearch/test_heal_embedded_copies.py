"""The stored copies the cleaner's fix left behind are cleaned again (`raw_copies.heal_embedded`, the
`heal-embedded-copies` command). The cleaner drops embedded data (`cleanse.drop_embedded`) under
the same
cleaner number, and a read of the same bytes makes no copy, so a copy made before that day kept its picture's bytes
and its role could never be ranked. The heal looks only at the current copies whose text holds `data:`, and makes a new
copy only where cleaning again changes the text. Every name, sentence and byte here is made up."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import delete, select

from app import db
from app.jobsearch import cli
from app.jobsearch.models import CleansedPosting, JobPosting, RawPosting, RoleRank, Todo
from app.jobsearch.postings import cleanse, raw_copies
from app.settings import settings
from tests.jobsearch.work_queue.support import OWNER, make_posting, make_role, without_links

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
WORDS = ["Programs Lead", "", "About the job",
         "Lead the programs team and run the quarterly review cycle for the whole office.",
         "Own the yearly budget and report to the head of operations.",
         "Work with every team on its plan for the year and coach the managers.",
         "You have ten years in operations or programs."]
PICTURE = "data:image/png;base64,iVBORw0KGgo" + "AAAAB3NzaC1yc2E/+" * 40 + "=="
PICTURED = "\n".join([*WORDS[:4], "", f"[{PICTURE}]", "", *WORDS[4:]])
PROSE = "\n".join([*WORDS, "You keep the reporting data: accurate, current and complete."])


async def _held_before_the_fix(monkeypatch, *texts: str) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """A role and its posting for each text, each with the copy the cleaner made before it dropped embedded data.
    Returns (role id, posting id) in the order of the texts."""
    made = []
    async with db.session_factory()() as session:
        await without_links(session)
        for n, text in enumerate(texts):
            role = make_role()
            posting = make_posting(opportunity_id=role.id, ats="employer", ext_id=str(n), text=text)
            session.add_all([role, posting])
            made.append((role.id, posting.id))
        await session.flush()
        with monkeypatch.context() as before:
            before.setattr(cleanse, "drop_embedded", lambda text: (text, []))
            assert await raw_copies.keep_legacy(session, OWNER, NOW) == len(texts)
        await session.execute(delete(Todo))
        await session.commit()
    return made


async def _heal() -> dict:
    async with db.session_factory()() as session:
        counts = await raw_copies.heal_embedded(session, OWNER, NOW)
        await session.commit()
    return counts


async def _copy_of(posting_id: uuid.UUID) -> CleansedPosting:
    async with db.session_factory()() as session:
        posting = await session.get(JobPosting, posting_id)
        return await session.get(CleansedPosting, posting.current_cleansed_id)


async def _told() -> list[tuple[str, str, str]]:
    async with db.session_factory()() as session:
        return [(t, k, c) for t, k, c in (await session.execute(
            select(Todo.topic, Todo.key, Todo.cause).order_by(Todo.id))).all()]


async def _count(model) -> int:
    async with db.session_factory()() as session:
        return len((await session.scalars(select(model.id))).all())


async def test_a_current_copy_that_holds_an_embedded_picture_is_made_again_and_its_role_is_told(js, monkeypatch):
    ((role_id, posting_id),) = await _held_before_the_fix(monkeypatch, PICTURED)
    old = await _copy_of(posting_id)
    assert PICTURE in old.text_clean, "the copy as the cleaner made it before the fix"

    assert await _heal() == {"looked_at": 1, "rebuilt": 1}

    new = await _copy_of(posting_id)
    assert new.id != old.id and new.raw_id == old.raw_id and new.cleaner_version == cleanse.CLEANER_VERSION
    assert new.text_clean == "\n".join(WORDS[:4] + [""] + WORDS[4:])
    assert new.removed == [{"rule": "embedded_data", "lines": 1, "chars": len(PICTURE) + 2}]
    assert await _count(CleansedPosting) == 1, "a raw row holds one copy for a cleaner number: the old one is gone"
    assert await _count(RawPosting) == 1, "the raw copies are not touched"
    assert await _told() == [("role.settle", str(role_id), "copies.recleanse"), ("pool.owed", "", "copies.recleanse")]


async def test_a_copy_whose_text_only_says_data_in_its_words_is_left_alone_and_its_role_is_told_nothing(
        js, monkeypatch):
    ((_role_id, posting_id),) = await _held_before_the_fix(monkeypatch, PROSE)
    old = await _copy_of(posting_id)
    assert "data:" in old.text_clean

    assert await _heal() == {"looked_at": 1, "rebuilt": 0}

    assert (await _copy_of(posting_id)).id == old.id and await _count(CleansedPosting) == 1
    assert await _told() == []


async def test_only_the_copies_that_change_are_made_again_and_a_copy_with_no_data_is_not_looked_at(js, monkeypatch):
    held = await _held_before_the_fix(monkeypatch, PICTURED, PROSE, "\n".join(WORDS),
                                      PICTURED.replace("data:image/png", "DATA:IMAGE/PNG"))
    before = [(await _copy_of(posting_id)).id for _role_id, posting_id in held]

    assert await _heal() == {"looked_at": 3, "rebuilt": 2}

    after = [(await _copy_of(posting_id)).id for _role_id, posting_id in held]
    assert [a == b for a, b in zip(after, before, strict=True)] == [False, True, True, False]
    assert sorted(key for topic, key, _cause in await _told() if topic == "role.settle") == sorted(
        [str(held[0][0]), str(held[3][0])])


async def test_the_copies_are_saved_a_batch_at_a_time(js, monkeypatch):
    monkeypatch.setattr(raw_copies, "HEAL_BATCH", 2)
    held = await _held_before_the_fix(monkeypatch, *[PICTURED + f"\nTeam {n}." for n in range(5)])
    saved = []
    release = db.release_if_free

    async def counted(session):
        done = await release(session)
        async with db.read_session_factory()() as reader:  # what is committed when the batch ends
            saved.append(len((await reader.scalars(select(CleansedPosting.id).where(
                ~CleansedPosting.text_clean.contains("base64")))).all()))
        return done

    monkeypatch.setattr(db, "release_if_free", counted)
    assert await _heal() == {"looked_at": 5, "rebuilt": 5}

    assert saved == [0, 2, 4, 5], "nothing after the search, then each batch of two saved on its own"
    for _role_id, posting_id in held:
        assert "base64" not in (await _copy_of(posting_id)).text_clean


async def test_a_second_run_makes_nothing_again(js, monkeypatch):
    ((_role_id, posting_id),) = await _held_before_the_fix(monkeypatch, PICTURED)
    assert (await _heal())["rebuilt"] == 1
    healed = await _copy_of(posting_id)
    async with db.session_factory()() as session:
        await session.execute(delete(Todo))
        await session.commit()

    assert await _heal() == {"looked_at": 0, "rebuilt": 0}

    assert (await _copy_of(posting_id)).id == healed.id and await _told() == []


async def test_a_rank_of_the_copy_that_held_the_picture_goes_with_it(js, monkeypatch):
    """A rank is for one copy, and a raw row holds one copy for a cleaner number: the rank of the text with the
    picture's bytes in it cannot stay, and the role is ranked again on the new copy."""
    ((role_id, posting_id),) = await _held_before_the_fix(monkeypatch, PICTURED)
    old = await _copy_of(posting_id)
    async with db.session_factory()() as session:
        await without_links(session)
        session.add(RoleRank(owner=OWNER, role_id=role_id, cleansed_id=old.id, profile_version=1, fit=40))
        await session.commit()

    assert await _heal() == {"looked_at": 1, "rebuilt": 1}

    assert await _count(RoleRank) == 0


async def test_the_command_prints_counts_only(js, monkeypatch, capsys):
    monkeypatch.setattr(settings, "owner", OWNER)
    await _held_before_the_fix(monkeypatch, PICTURED, PROSE)
    capsys.readouterr()

    assert await cli._heal_embedded_copies(SimpleNamespace()) == 0
    assert capsys.readouterr().out == "2 current copies hold 'data:': 1 made again without its embedded data\n"

    assert await cli._heal_embedded_copies(SimpleNamespace()) == 0
    assert capsys.readouterr().out == "1 current copies hold 'data:': 0 made again without its embedded data\n"
