"""A finished packet is saved to the owner's packet folder (RESUMEBOT_PACKET_DIR): PDF and Word, one folder per role, a
newer version replacing the files of the one before; nothing when no folder is set; a failure never stops the packet.
Made-up data only."""

import io
import uuid
from types import SimpleNamespace

import pytest
from docx import Document
from sqlalchemy import select

from app import db
from app.jobsearch import cli
from app.jobsearch.models import Packet
from app.jobsearch.writing import library as story_library
from app.jobsearch.writing import packets
from app.models.work import TrailEvent
from app.settings import settings
from tests.builders import FACTS_CLEAN, compose_reply, packet_setup, run_seats
from tests.support import OWNER

FOLDER = "Globex_Programs_Lead"
BOTH = {"Pat_Example_Resume.pdf", "Pat_Example_Resume.docx", "Pat_Example_CoverLetter.pdf",
        "Pat_Example_CoverLetter.docx"}


async def _packet(role_id, *, letter: bool, bullet: str = "") -> dict:
    """Ask for a packet, answer its seats and return its status and id. The letter is the made-up one; `bullet` is
    a new line the library offers in place of the first."""
    async with db.session_factory()() as session:
        packet = await packets.start(session, OWNER, role_id, "")
        await session.commit()
        packet_id = str(packet.id)
    reply = compose_reply()
    if letter:
        reply["letter"] = {"paragraphs": [{"text": "I would like to help.", "from": ["L001"]}]}
    if bullet:
        async with db.session_factory()() as session:
            lib = await story_library.usable(session, OWNER)
            lib.data = {**lib.data, "lines": [{**x, "text": bullet} if x["id"] == "L001" else x
                                              for x in lib.data["lines"]]}
            await session.commit()
    await run_seats({"Compose": reply, "Facts_1": FACTS_CLEAN})
    async with db.session_factory()() as session:
        stored = await session.get(Packet, uuid.UUID(packet_id))
    return {"taken": True, "status": stored.status, "id": packet_id}


def _files(root) -> set[str]:
    return {p.name for p in (root / FOLDER).iterdir()}


def _docx_text(path) -> str:
    return "\n".join(p.text for p in Document(io.BytesIO(path.read_bytes())).paragraphs)


async def test_a_ready_packet_is_saved_as_pdf_and_word_in_a_folder_for_the_role(js, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "packet_dir", str(tmp_path))
    result = await _packet(await packet_setup(), letter=True)
    assert result["status"] == "ready"
    assert _files(tmp_path) == BOTH
    folder = tmp_path / FOLDER
    assert (folder / "Pat_Example_Resume.pdf").read_bytes().startswith(b"%PDF-")
    assert (folder / "Pat_Example_CoverLetter.pdf").read_bytes().startswith(b"%PDF-")
    assert "Programs Leader" in _docx_text(folder / "Pat_Example_Resume.docx")
    assert "I would like to help." in _docx_text(folder / "Pat_Example_CoverLetter.docx")


async def test_a_newer_version_replaces_the_files(js, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "packet_dir", str(tmp_path))
    role_id = await packet_setup()
    await _packet(role_id, letter=True)
    assert _files(tmp_path) == BOTH
    await _packet(role_id, letter=True, bullet="Led the second version of the risk working group")
    assert _files(tmp_path) == BOTH
    assert "second version" in _docx_text(tmp_path / FOLDER / "Pat_Example_Resume.docx")


async def test_with_no_packet_folder_set_nothing_is_written(js, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "packet_dir", "")
    monkeypatch.chdir(tmp_path)
    result = await _packet(await packet_setup(), letter=True)
    assert result["status"] == "ready"
    assert list(tmp_path.iterdir()) == []


async def test_the_packet_folder_itself_is_made_when_the_first_packet_is_saved(js, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "packet_dir", str(tmp_path / "Resumebot"))
    result = await _packet(await packet_setup(), letter=True)
    assert result["status"] == "ready"
    assert {p.name for p in (tmp_path / "Resumebot" / FOLDER).iterdir()} == BOTH


async def test_a_folder_that_cannot_be_written_is_a_note_on_the_role_and_the_packet_goes_on(js, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "packet_dir", str(tmp_path / "not-there" / "deeper"))
    role_id = await packet_setup()
    result = await _packet(role_id, letter=True)
    assert result["taken"] is True and result["status"] == "ready"
    async with db.session_factory()() as session:
        packet = await session.get(Packet, uuid.UUID(result["id"]))
        notes = (await session.scalars(select(TrailEvent).where(
            TrailEvent.event == "packet_files_not_saved", TrailEvent.link_id == role_id))).all()
    assert packet.status == "ready"
    assert len(notes) == 1 and "could not be saved" in notes[0].sentence
    assert not (tmp_path / "not-there").exists()


# --- a failed save shows on the packet, and can be made again (docs/PRD.md path 10.5) ------------------------


async def _lines(role_id, event) -> list[TrailEvent]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(
            TrailEvent.event == event, TrailEvent.link_id == role_id))).all())


async def _problem(packet_id: str) -> str | None:
    async with db.session_factory()() as session:
        packet = await session.get(Packet, uuid.UUID(packet_id))
        return await packets.files_problem(session, packet)


async def _save_again(packet_id: str) -> dict:
    async with db.session_factory()() as session:
        shown = await packets.save_files_again(session, OWNER, uuid.UUID(packet_id))
        await session.commit()
    return shown


async def test_a_first_save_that_works_writes_no_line_and_no_folder_writes_neither(js, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "packet_dir", str(tmp_path))
    role_id = await packet_setup()
    result = await _packet(role_id, letter=True)
    assert await _problem(result["id"]) is None
    assert await _lines(role_id, "packet_files_saved") == []
    assert await _lines(role_id, "packet_files_not_saved") == []


async def test_a_failed_save_is_the_packets_problem_until_a_save_works(js, monkeypatch, tmp_path):
    root = tmp_path / "later" / "deeper"
    monkeypatch.setattr(settings, "packet_dir", str(root))
    role_id = await packet_setup()
    result = await _packet(role_id, letter=True)
    assert await _problem(result["id"])
    async with db.session_factory()() as session:
        shown = (await packets.for_role(session, OWNER, role_id))[0]
    assert shown["status"] == "ready" and shown["files_problem"]
    again = await _save_again(result["id"])  # the folder is still missing
    assert again["files_problem"] and len(await _lines(role_id, "packet_files_not_saved")) == 2
    root.parent.mkdir()
    again = await _save_again(result["id"])
    assert again["files_problem"] is None
    assert _files(root) == BOTH
    assert len(await _lines(role_id, "packet_files_saved")) == 1
    async with db.session_factory()() as session:
        assert (await packets.for_role(session, OWNER, role_id))[0]["files_problem"] is None


async def test_a_save_again_is_refused_with_no_folder_and_for_a_packet_that_is_not_ready(js, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "packet_dir", str(tmp_path))
    role_id = await packet_setup()
    result = await _packet(role_id, letter=False)
    async with db.session_factory()() as session:
        packet = await session.get(Packet, uuid.UUID(result["id"]))
        packet.status = "replaced"
        await session.commit()
    with pytest.raises(packets.PacketError):
        await _save_again(result["id"])
    async with db.session_factory()() as session:
        packet = await session.get(Packet, uuid.UUID(result["id"]))
        packet.status = "ready"
        await session.commit()
    monkeypatch.setattr(settings, "packet_dir", "")
    with pytest.raises(packets.PacketError):
        await _save_again(result["id"])
    with pytest.raises(packets.NoSuchRole):
        async with db.session_factory()() as session:
            await packets.save_files_again(session, "someone-else", uuid.UUID(result["id"]))


async def test_the_command_saves_again_only_the_ready_packets_whose_last_save_failed(js, monkeypatch, tmp_path, capsys):
    root = tmp_path / "later" / "deeper"
    monkeypatch.setattr(settings, "packet_dir", str(root))
    monkeypatch.setattr(settings, "owner", OWNER)
    role_id = await packet_setup()
    await _packet(role_id, letter=True)  # version 1 fails, then version 2 replaces it: only version 2 is counted
    failed = await _packet(role_id, letter=True)
    await cli._save_packet_files(SimpleNamespace(dry_run=True))
    assert "would save 1 packets" in capsys.readouterr().out
    assert not root.exists() and await _problem(failed["id"])
    await cli._save_packet_files(SimpleNamespace(dry_run=False))
    assert "saved 1 packets (1 failed again)" in capsys.readouterr().out
    root.parent.mkdir()
    await cli._save_packet_files(SimpleNamespace(dry_run=False))
    assert "saved 1 packets (0 failed again)" in capsys.readouterr().out
    assert await _problem(failed["id"]) is None
    await cli._save_packet_files(SimpleNamespace(dry_run=False))
    assert "saved 0 packets (0 failed again)" in capsys.readouterr().out


async def test_the_command_says_so_when_no_packet_folder_is_set(js, monkeypatch, capsys):
    monkeypatch.setattr(settings, "packet_dir", "")
    assert await cli._save_packet_files(SimpleNamespace(dry_run=False)) == 0
    assert capsys.readouterr().out.strip() == "no packet folder set"
