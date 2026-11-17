"""A run finishes on the file it started on, however the loaded files change under it.

The library holds only the files loaded now. A run is found its file in this order: the exact file; a loaded file with
the same behaviour hash (a release that only changed words); the text kept when it started (`proc_files`)."""

import pytest
from sqlalchemy import func, select
from support import CATALOG, FakeClock

from process_engine.engine import Engine, keep_files
from process_engine.errors import EngineError
from process_engine.loader import ProcessLibrary
from process_engine.models import COMPLETED, WAITING, ProcFile, ProcHistory
from process_engine.registry import ActionRegistry, Catalog

WORDS = "Big numbers go one way, small ones the other."

CHOOSE = (
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:porter="http://porter.local/bpmn/ext">'
    '<bpmn:process id="P" name="Choose" porter:key="choose"><bpmn:documentation>{words}</bpmn:documentation>'
    '<bpmn:extensionElements><porter:dataModel><porter:field path="answer" type="object"/>'
    '<porter:field path="answer.n" type="number"/></porter:dataModel></bpmn:extensionElements>'
    '<bpmn:startEvent id="S"/><bpmn:userTask id="Ask" name="Ask for a number" porter:card="pick_goal" '
    'porter:output="answer"/>'
    '<bpmn:exclusiveGateway id="G" name="Which way?" default="F_small"/>'
    '<bpmn:endEvent id="E_big"/><bpmn:endEvent id="E_small"/>'
    '<bpmn:sequenceFlow id="a" sourceRef="S" targetRef="Ask"/><bpmn:sequenceFlow id="b" sourceRef="Ask" targetRef="G"/>'
    '<bpmn:sequenceFlow id="F_big" name="Big" sourceRef="G" targetRef="E_big"><bpmn:documentation>Big.'
    '</bpmn:documentation><bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">answer.n &gt; {limit}'
    '</bpmn:conditionExpression></bpmn:sequenceFlow>'
    '<bpmn:sequenceFlow id="F_small" name="Small" sourceRef="G" targetRef="E_small"><bpmn:documentation>Small.'
    '</bpmn:documentation></bpmn:sequenceFlow></bpmn:process></bpmn:definitions>'
)


def choose(words=WORDS, limit=5) -> str:
    return CHOOSE.format(words=words, limit=limit)


def library_of(xml: str) -> ProcessLibrary:
    library = ProcessLibrary()
    library.load(xml, catalog=CATALOG, registry=ActionRegistry())
    return library


def engine_on(library: ProcessLibrary, **kwargs) -> Engine:
    return Engine(library, ActionRegistry(), clock=FakeClock(), catalog=CATALOG, **kwargs)


async def ends_at(session, instance) -> str:
    end = await session.scalar(
        select(ProcHistory.from_element).where(ProcHistory.instance_id == instance.id, ProcHistory.kind == "end")
    )
    return end


async def test_an_instance_keeps_the_text_of_the_file_it_started_on_once_per_file(session):
    library = library_of(choose())
    engine = engine_on(library)
    first = await engine.start(session, "choose")
    await engine.start(session, "choose")
    rows = (await session.scalars(select(ProcFile))).all()
    assert [(r.file_hash, r.process_key, r.xml) for r in rows] == [(first.file_hash, "choose", choose())]
    assert await session.scalar(select(func.count()).select_from(ProcFile)) == 1


async def test_a_run_finishes_when_a_release_only_changed_the_files_words(session):
    started = await engine_on(library_of(choose())).start(session, "choose")
    await session.execute(ProcFile.__table__.delete())  # a run from before the files were kept
    assert started.status == WAITING

    reworded = library_of(choose(words="The same rule, said another way."))
    assert reworded.get("choose").file_hash != started.file_hash
    assert reworded.get("choose").behaviour_hash == started.behaviour_hash
    engine = engine_on(reworded)
    assert (await engine.where_is(session, started.id))["step"] == "Ask"
    await engine.complete(session, started.id, "Ask", {"n": 9})
    assert started.status == COMPLETED and await ends_at(session, started) == "E_big"


async def test_a_run_finishes_on_its_own_file_when_a_release_changed_how_it_decides(session):
    started = await engine_on(library_of(choose(limit=5))).start(session, "choose")

    released = library_of(choose(limit=50))  # the same words, a different rule: not the same behaviour
    current = released.get("choose")
    assert current.behaviour_hash != started.behaviour_hash
    engine = engine_on(released)
    await engine.complete(session, started.id, "Ask", {"n": 10})
    # 10 is big under the rule the run started on (more than 5), small under today's (more than 50)
    assert started.status == COMPLETED and await ends_at(session, started) == "E_big"
    assert started.file_hash != current.file_hash

    # the old file only finished this run: new work still starts on the file loaded now
    assert released.get("choose") is current
    assert released.get("choose", started.file_hash) is None
    fresh = await engine.start(session, "choose")
    assert fresh.file_hash == current.file_hash
    await engine.complete(session, fresh.id, "Ask", {"n": 10})
    assert await ends_at(session, fresh) == "E_small"


async def test_a_file_read_back_is_loaded_once_per_library(session):
    started = await engine_on(library_of(choose(limit=5))).start(session, "choose")
    engine = engine_on(library_of(choose(limit=50)))
    first = await engine._def(session, started)  # noqa: SLF001
    await session.execute(ProcFile.__table__.delete())
    assert await engine._def(session, started) is first  # noqa: SLF001 - kept beside the library, not read again


async def test_a_run_whose_file_was_never_kept_is_refused_in_plain_words_not_a_crash(session):
    started = await engine_on(library_of(choose(limit=5))).start(session, "choose")
    await session.execute(ProcFile.__table__.delete())
    engine = engine_on(library_of(choose(limit=50)))
    with pytest.raises(EngineError, match="isn't loaded any more, and its text wasn't kept"):
        await engine.where_is(session, started.id)
    await engine.complete(session, started.id, "Ask", {"n": 1})  # an answer moves it onto the current file instead
    assert started.status == COMPLETED


async def test_a_kept_file_that_can_no_longer_load_is_refused_in_plain_words(session):
    started = await engine_on(library_of(choose(limit=5))).start(session, "choose")
    engine = Engine(library_of(choose(limit=50)), ActionRegistry(), clock=FakeClock(),
                    catalog=Catalog(roles=CATALOG.roles))  # the card it names has since been taken away
    with pytest.raises(EngineError, match="no longer loads.*pick_goal"):
        await engine.where_is(session, started.id)


async def test_the_files_kept_at_start_up_let_a_run_from_before_the_engine_kept_files_finish(session):
    """An app keeps its shipped files when it starts (`keep_files`), so a run that started before its file was kept
    (it has no row of its own) still finishes after a release changes how the file decides."""
    old = library_of(choose(limit=5))
    started = await engine_on(old).start(session, "choose")
    await session.execute(ProcFile.__table__.delete())  # as if it started before files were kept
    assert await keep_files(session, old) == 1
    assert await keep_files(session, old) == 1  # once per file: the second start-up finds it there
    assert await session.scalar(select(func.count()).select_from(ProcFile)) == 1
    await engine_on(library_of(choose(limit=50))).complete(session, started.id, "Ask", {"n": 10})
    assert started.status == COMPLETED and await ends_at(session, started) == "E_big"


def stranded_engine(xml: str) -> Engine:
    """An engine on a newer file, whose catalog has since lost the card the old file names (`pick_goal`): the old file
    can't be loaded from its kept text any more, the way a release that removes an instruction file leaves it."""
    return Engine(library_of(xml), ActionRegistry(), clock=FakeClock(), catalog=Catalog(roles=CATALOG.roles))


async def test_a_run_whose_file_can_no_longer_load_is_moved_to_the_current_file_and_finishes_not_failed(session):
    started = await engine_on(library_of(choose(limit=5))).start(session, "choose")
    engine = stranded_engine(choose(limit=50))
    await engine.complete(session, started.id, "Ask", {"n": 60})  # the same step is still there: it carries on
    assert started.status == COMPLETED and await ends_at(session, started) == "E_big"
    assert started.file_hash == engine.library.get("choose").file_hash
    assert await session.scalar(select(func.count()).select_from(ProcHistory).where(
        ProcHistory.instance_id == started.id, ProcHistory.kind == "migrate")) == 1


async def test_a_run_whose_step_is_gone_goes_back_to_the_nearest_step_that_is_there_and_waits_again(session):
    started = await engine_on(library_of(choose(limit=5))).start(session, "choose")
    renamed = choose(limit=50).replace('id="Ask"', 'id="Ask2"').replace('"Ask"', '"Ask2"')
    engine = stranded_engine(renamed)
    assert await engine.move_stranded(session) == 1  # the start-up sweep: nothing is left that can't be answered
    assert started.status == WAITING and started.file_hash == engine.library.get("choose").file_hash
    await engine.complete(session, started.id, "Ask2", {"n": 60})
    assert started.status == COMPLETED and await ends_at(session, started) == "E_big"
    assert await engine.move_stranded(session) == 0  # nothing left to move, and an ended run is never touched


async def test_one_stranded_run_that_raises_does_not_stop_the_sweep_for_the_others(session, caplog):
    """Found on an update: a step raised something that is not an `EngineError` while the first
    stranded run was moved, and the sweep stopped there, so no run behind it was moved either."""
    first = await engine_on(library_of(choose(limit=5))).start(session, "choose")
    second = await engine_on(library_of(choose(limit=5))).start(session, "choose")
    old_hash, ids = first.file_hash, {first.id: (first, second), second.id: (second, first)}
    broke: list = []

    async def hand_out(session, instance, wait):  # the first run to be handed a step breaks it
        if not broke:
            broke.append(instance.id)
            raise RuntimeError("the hand-out broke")

    renamed = choose(limit=50).replace('id="Ask"', 'id="Ask2"').replace('"Ask"', '"Ask2"')
    engine = Engine(library_of(renamed), ActionRegistry(), clock=FakeClock(), catalog=Catalog(roles=CATALOG.roles),
                    on_wait=hand_out)
    with caplog.at_level("ERROR"):
        assert await engine.move_stranded(session) == 1, "only the run that didn't raise was moved"
    failed, moved = ids[broke[0]]  # a run the savepoint rolled back is expired: its id was kept before the sweep
    await session.refresh(failed)
    await session.refresh(moved)
    assert failed.file_hash == old_hash and moved.file_hash == engine.library.get("choose").file_hash
    assert moved.status == WAITING
    assert any(str(broke[0]) in record.getMessage() and record.exc_info for record in caplog.records), (
        "the log names the run that raised, with what it raised")


async def test_the_sweep_leaves_a_run_of_a_process_this_library_does_not_hold_to_the_library_that_does(session, caplog):
    """The job-search library's start-up sweep took the site projects' runs for
    stranded, failed to move each onto a file it has none of, and logged an error at every start."""
    other = choose().replace('porter:key="choose"', 'porter:key="elsewhere"')
    started = await engine_on(library_of(other)).start(session, "elsewhere")
    file_hash = started.file_hash
    with caplog.at_level("ERROR"):
        assert await stranded_engine(choose()).move_stranded(session) == 0
    assert caplog.records == [] and started.file_hash == file_hash and started.status == WAITING
