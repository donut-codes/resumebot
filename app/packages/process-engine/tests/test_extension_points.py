"""The extension points an app fills in (`process_engine.config`): each one changes what the engine does, and each has a
default that works with nothing configured. Nothing here touches an app."""

import uuid

import pytest

from process_engine import loader, versions
from process_engine.config import configure
from process_engine.engine import Engine
from process_engine.errors import ProcessLoadError
from process_engine.registry import ActionRegistry

TINY = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
    xmlns:porter="http://porter.local/bpmn/ext" targetNamespace="http://example.test/bpmn">
  <bpmn:process id="Tiny" name="Tiny" porter:key="tiny">
    <bpmn:startEvent id="Start"/>
    <bpmn:userTask id="Ask" name="Ask" porter:card="ask.owner"><bpmn:documentation>WORDS</bpmn:documentation>
    </bpmn:userTask>
    <bpmn:endEvent id="End"/>
    <bpmn:sequenceFlow id="F1" sourceRef="Start" targetRef="Ask"/>
    <bpmn:sequenceFlow id="F2" sourceRef="Ask" targetRef="End"/>
  </bpmn:process>
</bpmn:definitions>
"""


def tiny(words: str = "first") -> str:
    return TINY.replace("WORDS", words)


# --- who owns an instance ---------------------------------------------------------------------------------------


def test_the_owner_is_read_from_the_configured_key_of_the_data(monkeypatch):
    owner = uuid.uuid4()
    monkeypatch.setattr(loader, "_owner_versions", {owner: {}})
    assert loader.owner_of({"owner": str(owner)}, None) == owner  # the default key
    assert loader.owner_of({"tenant": str(owner)}, None) is None
    configure(owner_key="tenant")
    assert loader.owner_of({"tenant": str(owner)}, None) == owner
    assert loader.owner_of({"tenant": str(uuid.uuid4())}, None) is None  # an owner with no versions of its own


def test_an_app_can_find_the_owner_anywhere_in_the_data_or_subject(monkeypatch):
    owner = uuid.uuid4()
    monkeypatch.setattr(loader, "_owner_versions", {owner: {}})
    configure(owner_candidates=lambda data, subject: ["not-an-id", *(subject or "").split(":")])
    assert loader.owner_of({}, f"round:{owner}:project") == owner


# --- the roles, cards and files a process may name ----------------------------------------------------------------


def test_with_nothing_configured_a_step_naming_a_role_or_card_is_refused():
    with pytest.raises(ProcessLoadError, match="ask.owner"):
        loader.load_process(tiny(), registry=ActionRegistry())
    configure(card_kinds=lambda: ["ask.owner"])
    assert loader.load_process(tiny(), registry=ActionRegistry()).key == "tiny"


# --- versions: where the shipped files are, who made a change, what registers the actions ---------------------------


async def test_versions_read_the_shipped_folder_the_app_names_and_the_names_it_gives(session, tmp_path):
    (tmp_path / "tiny.bpmn").write_text(tiny(), encoding="utf-8")
    owner, member = uuid.uuid4(), uuid.uuid4()
    asked = []

    async def member_names(_session, entity_id):
        asked.append(entity_id)
        return {member: "Dana"}

    registered = []
    configure(
        card_kinds=["ask.owner"], shipped_dir=lambda: tmp_path, member_names=member_names,
        register_processes=lambda: registered.append(1), shipped_label="The file as shipped.",
    )
    assert versions.process_keys() == ["tiny"]
    assert versions.shipped_xml("tiny") == tiny()
    assert await versions.current_number(session, owner, "tiny") == versions.SHIPPED

    row = await versions.add_version(session, owner, "tiny", tiny("second"), changed_by=member, wording="w")
    assert (row.number, row.entity_id, row.changed_by) == (1, owner, member)
    assert registered  # the app was asked to register what the files name before one loaded
    assert await versions.current_xml(session, owner, "tiny") == tiny("second")

    history = await versions.list_versions(session, owner, "tiny")
    assert [h["number"] for h in history] == [1, 0]
    assert history[0]["by"] == "Dana" and history[1]["what"] == "The file as shipped."
    assert asked == [owner]

    back = await versions.rollback(session, owner, "tiny", versions.SHIPPED, member)
    assert (back.number, back.kind, back.restored_number) == (2, versions.ROLLBACK, 0)
    assert back.xml == tiny()
    await session.commit()  # the library changes only once the save is committed
    # the library starts this owner's new work on the version just saved
    library = loader.ProcessLibrary()
    library.load(tiny(), catalog=None)
    assert library.get("tiny", entity_id=owner).file_hash == back.file_hash


async def test_a_version_that_does_not_load_is_refused_in_plain_words(session, tmp_path):
    (tmp_path / "tiny.bpmn").write_text(tiny(), encoding="utf-8")
    configure(shipped_dir=lambda: tmp_path)  # no card kinds: the file names a card that doesn't exist
    with pytest.raises(versions.VersionError) as caught:
        await versions.add_version(session, uuid.uuid4(), "tiny", tiny("second"))
    assert caught.value.status == 409
    assert "ask.owner" in str(caught.value)


def test_with_no_shipped_folder_nothing_is_shipped():
    assert versions.process_keys() == []
    with pytest.raises(versions.VersionError, match="no such process"):
        versions.shipped_xml("tiny")


# --- the config itself ----------------------------------------------------------------------------------------------


def test_an_unknown_setting_is_refused_by_name():
    with pytest.raises(TypeError, match="no_such"):
        configure(no_such=1)


def test_the_engine_runs_with_nothing_configured():
    assert Engine(loader.ProcessLibrary()).library.keys() == []
