"""An owner's versions of a process file, as a release can break them and as saves can race
.

- A version that stops loading (a release renamed a card, an action, a file it names) never locks the owner out:
  rollback compares stored hashes, refresh and the page use the newest version that still loads, and they say so.
- A save reaches the process library only once it is committed, for that owner only, and not at all if it is rolled
  back; a save built on an older version, or one that loses a race, is refused in plain words, never as a raw
  database error; a saved version is checked against the whole library, so a call circle is refused.
"""

import asyncio
import types
import uuid

import pytest
from sqlalchemy import delete, literal_column
from sqlalchemy.ext.asyncio import async_sessionmaker
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


async def a_release_breaks_the_newest(session):
    """Version 1 (words) and version 2 (a card kind) are saved; then a release drops that card kind, so version 2 no
    longer loads."""
    owner = uuid.uuid4()
    v1 = await versions.add_version(session, owner, "alpha", process("alpha", "second"))
    v2 = await versions.add_version(session, owner, "alpha", process("alpha", "third", card="ask.other"))
    await session.commit()
    configure(card_kinds=["ask.owner"])
    return owner, v1, v2


# --- 1.3: a version that stops loading ------------------------------------------------------------------------------


async def test_going_back_works_when_the_newest_version_no_longer_loads(session, folder):
    owner, v1, _v2 = await a_release_breaks_the_newest(session)
    member = uuid.uuid4()
    back = await versions.rollback(session, owner, "alpha", 1, member)
    assert (back.number, back.kind, back.restored_number, back.xml) == (3, versions.ROLLBACK, 1, v1.xml)
    await session.commit()
    assert loader.owner_active("alpha", owner).file_hash == v1.file_hash
    # the shipped file is always there to go back to
    shipped = await versions.rollback(session, owner, "alpha", versions.SHIPPED, member)
    assert (shipped.number, shipped.xml) == (4, versions.shipped_xml("alpha"))


async def test_going_back_to_the_shipped_file_works_when_every_version_is_broken(session, folder):
    owner = uuid.uuid4()
    await versions.add_version(session, owner, "alpha", process("alpha", card="ask.other"))
    await versions.add_version(session, owner, "alpha", process("alpha", "again", card="ask.other"))
    await session.commit()
    configure(card_kinds=["ask.owner"])  # a release drops the card kind both versions name
    back = await versions.rollback(session, owner, "alpha", versions.SHIPPED, uuid.uuid4())
    assert back.number == 3 and back.xml == versions.shipped_xml("alpha")
    await session.commit()
    assert loader.owner_active("alpha", owner).file_hash == back.file_hash


async def test_going_back_to_a_version_that_no_longer_loads_says_so(session, folder):
    owner, _v1, _v2 = await a_release_breaks_the_newest(session)
    await versions.rollback(session, owner, "alpha", 1, uuid.uuid4())  # now version 3 is the newest, and loads
    with pytest.raises(versions.VersionError, match=r"Can't go back to version 2.*ask.other"):
        await versions.rollback(session, owner, "alpha", 2, uuid.uuid4())


async def test_refresh_runs_the_newest_version_that_still_loads(session, folder):
    owner, v1, _v2 = await a_release_breaks_the_newest(session)
    loader.set_owner_versions({}, {})  # a restart
    await versions.refresh(session)
    assert loader.owner_active("alpha", owner).file_hash == v1.file_hash  # not the shipped file


async def test_the_page_and_the_current_file_say_which_version_runs(session, folder):
    owner, v1, v2 = await a_release_breaks_the_newest(session)
    run = await versions.running(session, owner, "alpha")
    assert (run.number, run.xml) == (1, v1.xml)
    assert run.note == "Version 2 no longer works with this release, so version 1 is running."
    assert await versions.current_number(session, owner, "alpha") == 1
    assert await versions.current_xml(session, owner, "alpha") == v1.xml
    listing = {v["number"]: v for v in await versions.list_versions(session, owner, "alpha")}
    assert listing[1]["current"] is True and listing[2]["current"] is False and listing[0]["current"] is False
    assert listing[2]["loads"] is False and "ask.other" in listing[2]["problem"] and listing[2]["can_restore"] is False
    assert listing[1]["loads"] is True and listing[1]["problem"] is None
    # the library and the database name the same file
    loader.set_owner_versions({}, {})
    await versions.refresh(session)
    assert versions.active_xml("alpha", owner) == await versions.current_xml(session, owner, "alpha") == v1.xml
    assert v2.number == 2


async def test_when_no_version_loads_the_shipped_file_runs_and_the_page_says_so(session, folder):
    owner = uuid.uuid4()
    await versions.add_version(session, owner, "alpha", process("alpha", card="ask.other"))
    await versions.add_version(session, owner, "alpha", process("alpha", "again", card="ask.other"))
    await session.commit()
    configure(card_kinds=["ask.owner"])
    run = await versions.running(session, owner, "alpha")
    assert (run.number, run.xml) == (versions.SHIPPED, versions.shipped_xml("alpha"))
    assert run.note == "Versions 2 and 1 no longer work with this release, so the file as shipped is running."
    listing = await versions.list_versions(session, owner, "alpha")
    assert [v["number"] for v in listing if v["current"]] == [0]


async def test_with_everything_loading_the_newest_runs_and_nothing_is_said(session, folder):
    owner = uuid.uuid4()
    await versions.add_version(session, owner, "alpha", process("alpha", "second"))
    run = await versions.running(session, owner, "alpha")
    assert run.number == 1 and run.note is None


# --- 2.7: the library changes after the commit, for one owner ------------------------------------------------------


async def test_a_save_reaches_the_library_only_once_it_is_committed(session, folder):
    owner = uuid.uuid4()
    row = await versions.add_version(session, owner, "alpha", process("alpha", "second"))
    assert loader.owner_active("alpha", owner) is None  # saved in the transaction, not yet the library's
    await session.commit()
    assert loader.owner_active("alpha", owner).file_hash == row.file_hash


async def test_a_save_that_is_rolled_back_leaves_the_library_alone(session, folder):
    owner = uuid.uuid4()
    await versions.add_version(session, owner, "alpha", process("alpha", "second"))
    await session.rollback()
    assert loader.owner_active("alpha", owner) is None
    await session.commit()  # a later commit must not bring the rolled-back save back
    assert loader.owner_active("alpha", owner) is None
    assert await versions.latest(session, owner, "alpha") is None


async def test_a_save_inside_a_rolled_back_savepoint_leaves_the_library_alone(session, folder):
    owner = uuid.uuid4()
    try:
        async with session.begin_nested():
            await versions.add_version(session, owner, "alpha", process("alpha", "second"))
            raise RuntimeError("the work around it failed")
    except RuntimeError:
        pass
    await session.commit()
    assert loader.owner_active("alpha", owner) is None


async def test_a_save_changes_only_that_owners_versions(session, folder):
    first, second = uuid.uuid4(), uuid.uuid4()
    await versions.add_version(session, first, "alpha", process("alpha", "first's"))
    await versions.add_version(session, first, "beta", process("beta", "first's beta"))
    await session.commit()
    others_alpha, others_beta = loader.owner_active("alpha", first), loader.owner_active("beta", first)
    await versions.add_version(session, second, "alpha", process("alpha", "second's"))
    await versions.add_version(session, first, "alpha", process("alpha", "first's again"))
    await session.commit()
    assert "second's" in loader.owner_active("alpha", second).xml
    assert loader.owner_active("beta", first) is others_beta  # untouched, not rebuilt
    assert loader.owner_active("alpha", first) is not others_alpha and "first's again" in loader.owner_active(
        "alpha", first).xml


async def test_a_save_built_on_an_older_version_is_refused(session, folder):
    owner = uuid.uuid4()
    await versions.add_version(session, owner, "alpha", process("alpha", "second"), expected_base=0)
    with pytest.raises(versions.VersionError) as caught:
        await versions.add_version(session, owner, "alpha", process("alpha", "third"), expected_base=0)
    assert caught.value.status == 409 and str(caught.value) == versions.MOVED
    await versions.add_version(session, owner, "alpha", process("alpha", "third"), expected_base=1)


async def test_losing_a_race_for_the_next_number_is_a_plain_refusal_and_the_session_stays_usable(
    session, folder, monkeypatch,
):
    owner = uuid.uuid4()
    await versions.add_version(session, owner, "alpha", process("alpha", "second"))
    # the other save got number 1 while this one was reading the highest number as 0
    monkeypatch.setattr(versions, "func", types.SimpleNamespace(max=lambda _column: literal_column("0")))
    with pytest.raises(versions.VersionError) as caught:
        await versions.add_version(session, owner, "alpha", process("alpha", "third"))
    assert str(caught.value) == versions.MOVED
    assert (await versions.latest(session, owner, "alpha")).number == 1  # the transaction is still good


async def test_two_saves_at_once_one_wins_and_the_other_is_told_plainly(new_engine, folder):
    """Real sessions on their own connections: the second save waits for the first's turn to write, then finds the
    number taken."""
    engine = new_engine()
    make = async_sessionmaker(engine, expire_on_commit=False)
    owner = uuid.uuid4()
    try:
        async with make() as one, make() as two:
            await versions.add_version(one, owner, "alpha", process("alpha", "one's"), expected_base=0)
            racer = asyncio.create_task(
                versions.add_version(two, owner, "alpha", process("alpha", "two's"), expected_base=0))
            await asyncio.sleep(0.5)
            assert not racer.done()  # waiting on the first save
            await one.commit()
            with pytest.raises(versions.VersionError) as caught:
                await racer
            assert caught.value.status == 409
            await two.rollback()
        async with make() as check:
            assert [r.number for r in await versions.versions_of(check, owner, "alpha")] == [1]
        assert "one's" in loader.owner_active("alpha", owner).xml  # and the library has the winner
    finally:
        async with make() as clean:
            await clean.execute(delete(ProcVersion).where(ProcVersion.entity_id == owner))
            await clean.commit()
        await engine.dispose()


# --- 2.7: a version is checked against the whole library -------------------------------------------------------------


async def test_a_version_that_would_make_processes_call_each_other_in_a_circle_is_refused(session, folder):
    (folder / "beta.bpmn").write_text(process("beta", calls="alpha"), encoding="utf-8")
    owner = uuid.uuid4()
    with pytest.raises(versions.VersionError, match="in a circle"):
        await versions.add_version(session, owner, "alpha", process("alpha", calls="beta"))
    assert await versions.latest(session, owner, "alpha") is None  # nothing was kept
    await versions.add_version(session, owner, "alpha", process("alpha", "no call"))  # a fine one still saves


async def test_a_version_that_calls_a_process_that_is_not_there_is_refused(session, folder):
    with pytest.raises(versions.VersionError, match="'nowhere', which isn't a loaded process"):
        await versions.add_version(session, uuid.uuid4(), "alpha", process("alpha", calls="nowhere"))


async def test_a_problem_the_files_already_had_does_not_stop_every_save(session, folder):
    (folder / "beta.bpmn").write_text(process("beta", calls="nowhere"), encoding="utf-8")
    owner = uuid.uuid4()
    await versions.add_version(session, owner, "beta", process("beta", "reworded", calls="nowhere"))
    await versions.add_version(session, owner, "alpha", process("alpha", "reworded"))


# --- lending an owner's versions to a throwaway household ----------------------------------------------------------


async def test_a_borrower_runs_on_the_owners_versions_only_while_the_block_runs(session, folder):
    owner, borrower = uuid.uuid4(), uuid.uuid4()
    row = await versions.add_version(session, owner, "alpha", process("alpha", "mine"))
    await session.commit()
    with versions.lent(borrower, owner):
        assert loader.owner_active("alpha", borrower).file_hash == row.file_hash
        assert loader.owner_of({"owner": str(borrower)}, None) == borrower
    assert loader.owner_active("alpha", borrower) is None
    assert loader.owner_active("alpha", owner).file_hash == row.file_hash  # the owner's own are untouched
    with versions.lent(borrower, uuid.uuid4()):  # an owner with nothing of its own lends nothing
        assert loader.owner_active("alpha", borrower) is None


async def test_refresh_parses_the_files_after_it_has_ended_the_transaction_it_began(committed, folder, monkeypatch):
    """On one writer at a time the files were parsed and checked inside the transaction that read them, which held
    everyone else's turn to write for as long (most of the time, on a large database). A session that held nothing
    when `refresh` was called has no transaction by the time the first file is parsed, and the library is the same."""
    owner = uuid.uuid4()
    async with committed() as writer:
        v1 = await versions.add_version(writer, owner, "alpha", process("alpha", "second"))
        await writer.commit()
    loader.set_owner_versions({}, {})  # a restart
    seen: list[bool] = []
    real = versions._try_load  # noqa: SLF001

    def watched(xml, key):
        seen.append(reader.in_transaction())
        return real(xml, key)

    monkeypatch.setattr(versions, "_try_load", watched)
    async with committed() as reader:
        await versions.refresh(reader)
        assert not reader.in_transaction()
    assert seen == [False], "parsed with no transaction open"
    assert loader.owner_active("alpha", owner).file_hash == v1.file_hash


async def test_refresh_leaves_a_transaction_that_holds_work_as_it_was(committed, folder):
    """A session that has work of its own is not ended by `refresh`: its transaction goes on, and its unsaved version is
    seen by it as before."""
    owner = uuid.uuid4()
    async with committed() as writer:
        v1 = await versions.add_version(writer, owner, "alpha", process("alpha", "second"))
        assert writer.in_transaction()
        await versions.refresh(writer)
        assert writer.in_transaction(), "its transaction goes on"
        assert loader.owner_active("alpha", owner).file_hash == v1.file_hash
        await writer.rollback()
