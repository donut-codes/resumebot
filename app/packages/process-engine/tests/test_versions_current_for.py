"""`versions.current_for`: a program reads an owner's saved versions of the process files from the database before it
uses them, so a version another program saved is in force with no message between the two.

- A version whose row was committed elsewhere (this program's library was never told) is in the library afterwards.
- When nothing was saved since the last look, one small read is all it costs: no file is parsed again, not even when
  the newest version no longer loads.
- A library that was emptied since (a restart) is filled again.
- The caller's transaction is left open, with its own unsaved work in it.
"""

import uuid

import pytest
from tiny_files import process

from process_engine import loader, versions
from process_engine.config import configure
from process_engine.models import ProcVersion


@pytest.fixture
def folder(tmp_path):
    """Two shipped processes, `alpha` and `beta`, and the card kinds they may name."""
    configure(card_kinds=["ask.owner", "ask.other"], shipped_dir=lambda: tmp_path)
    (tmp_path / "alpha.bpmn").write_text(process("alpha"), encoding="utf-8")
    (tmp_path / "beta.bpmn").write_text(process("beta"), encoding="utf-8")
    return tmp_path


async def saved_elsewhere(session, owner, number, xml, key="alpha") -> str:
    """A version another program saved: its row is in the database and this program's library was never told. Returns
    the file's hash."""
    file_hash = versions.file_hash_of(xml)
    session.add(ProcVersion(entity_id=owner, process_key=key, number=number, file_hash=file_hash, xml=xml))
    await session.commit()
    return file_hash


def parses(monkeypatch) -> list[str]:
    """Every file the versions module parses from here on, by process key."""
    seen: list[str] = []
    real = versions._try_load  # noqa: SLF001

    def watched(xml, key):
        seen.append(key)
        return real(xml, key)

    monkeypatch.setattr(versions, "_try_load", watched)
    return seen


async def test_a_version_saved_by_another_program_is_in_force_after_current_for(session, folder):
    owner = uuid.uuid4()
    await versions.current_for(session, owner)  # this program has looked once: the owner has saved nothing
    assert loader.owner_active("alpha", owner) is None

    saved = await saved_elsewhere(session, owner, 1, process("alpha", "second"))
    assert loader.owner_active("alpha", owner) is None, "no one told this program's library"

    assert await versions.current_for(session, owner) is True
    assert loader.owner_active("alpha", owner).file_hash == saved


async def test_nothing_is_parsed_when_nothing_was_saved_since(session, folder, monkeypatch):
    owner = uuid.uuid4()
    await saved_elsewhere(session, owner, 1, process("alpha", "second"))
    assert await versions.current_for(session, owner) is True
    seen = parses(monkeypatch)
    assert await versions.current_for(session, owner) is False
    assert seen == [], "one small read, and no file parsed"


async def test_a_version_that_no_longer_loads_is_not_parsed_again_on_every_call(session, folder, monkeypatch):
    owner = uuid.uuid4()
    good = await saved_elsewhere(session, owner, 1, process("alpha", "second"))
    await saved_elsewhere(session, owner, 2, process("alpha", "third", card="ask.other"))
    configure(card_kinds=["ask.owner"])  # a release drops the card kind version 2 names
    assert await versions.current_for(session, owner) is True
    assert loader.owner_active("alpha", owner).file_hash == good, "the newest that loads"
    seen = parses(monkeypatch)
    assert await versions.current_for(session, owner) is False
    assert seen == []


async def test_a_library_emptied_since_is_filled_again(session, folder):
    owner = uuid.uuid4()
    saved = await saved_elsewhere(session, owner, 1, process("alpha", "second"))
    assert await versions.current_for(session, owner) is True
    loader.set_owner_versions({}, {})  # a restart, or a test's reset
    assert loader.owner_active("alpha", owner) is None
    assert await versions.current_for(session, owner) is True
    assert loader.owner_active("alpha", owner).file_hash == saved


async def test_current_for_leaves_the_callers_transaction_and_its_unsaved_work(committed, folder):
    owner = uuid.uuid4()
    async with committed() as writer:
        v1 = await versions.add_version(writer, owner, "alpha", process("alpha", "second"))
        assert writer.in_transaction()
        assert await versions.current_for(writer, owner) is True
        assert writer.in_transaction(), "its transaction goes on"
        assert loader.owner_active("alpha", owner).file_hash == v1.file_hash
        await writer.rollback()
