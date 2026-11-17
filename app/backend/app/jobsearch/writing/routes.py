"""Packets and the export over HTTP (docs/PRD.md paths 9 and 10): ask for the next version of a role's packet,
download a written packet's resume or cover letter as a PDF or a Word file, and export the tracker as a spreadsheet
(`GET /export.xlsx`)."""

import asyncio
import re
import unicodedata
import uuid
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ReadSession, Session
from app.errors import NotFound
from app.jobsearch.imports import tracker
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import engine_calls
from app.jobsearch.writing import docs_render, packet_files, packets
from app.settings import settings

router = APIRouter()

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class AskPacket(BaseModel):
    #: What to change from the version before; empty for a first packet.
    feedback: str = ""


@router.post("/api/v1/roles/{role_id}/packet")
async def ask_packet(role_id: uuid.UUID, body: AskPacket, session: Session) -> dict:
    """Ask for the next version of a role's packet (with what to change, for a later one). The writer is woken."""
    packet = await packets.ask(session, settings.owner, role_id, body.feedback)
    await session.commit()
    await engine_calls.knock(str(packet.id), reason=engine_calls.PACKET_ASKED)
    return packets.view(packet)


@router.post("/api/v1/packets/{packet_id}/save-files")
async def save_packet_files(packet_id: uuid.UUID, session: Session) -> dict:
    """Write a ready packet's files to the packet folder now; a save that fails again is its `files_problem`."""
    shown = await packets.save_files_again(session, settings.owner, packet_id)
    await session.commit()
    return shown


async def _packet_and_role(session: AsyncSession, packet_id: uuid.UUID):
    packet = await session.get(Packet, packet_id)
    if packet is None or packet.owner != settings.owner or not packet.resume:
        raise NotFound("no written packet with that id")
    return packet, await session.get(Opportunity, packet.role_id)


def attachment(filename: str) -> str:
    """A Content-Disposition for a download (RFC 6266): a plain-ASCII `filename` for old clients and the real name in
    `filename*` (UTF-8, percent-encoded). A name with an accent, a CJK letter or a quote in it is no longer a 500 or a
    broken header."""
    plain = re.sub(r'[^A-Za-z0-9._ -]', "_", unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode())
    plain = plain.strip() or "download"
    header = f'attachment; filename="{plain}"'
    return header if plain == filename else f"{header}; filename*=UTF-8''{quote(filename, safe='')}"


owner_file_name = packet_files.owner_file_name
_filename = packet_files.file_name


async def _page_margin(session) -> float | None:
    """The owner's page margin in inches, from the writing rules in force now (None: the export keeps its own)."""
    return (await profile_store.owner_documents(session, settings.owner)).writing.page_margin_inches


@router.get("/api/v1/packets/{packet_id}/resume.pdf")
async def packet_resume_pdf(packet_id: uuid.UUID, session: ReadSession) -> Response:
    packet, role = await _packet_and_role(session, packet_id)
    data = await asyncio.to_thread(
        docs_render.resume_pdf, packet.resume, margin_inches=await _page_margin(session))
    return Response(data, media_type=PDF,
                    headers={"Content-Disposition": attachment(_filename(packet, role, "Resume", "pdf"))})


@router.get("/api/v1/packets/{packet_id}/letter.pdf")
async def packet_letter_pdf(packet_id: uuid.UUID, session: ReadSession) -> Response:
    packet, role = await _packet_and_role(session, packet_id)
    if not (packet.letter or {}).get("paragraphs"):
        raise NotFound("this packet has no cover letter")
    data = await asyncio.to_thread(
        docs_render.letter_pdf, packet.letter, packet.resume, role.company if role else "",
        margin_inches=await _page_margin(session), role_title=role.title if role else "")
    return Response(data, media_type=PDF,
                    headers={"Content-Disposition": attachment(_filename(packet, role, "CoverLetter", "pdf"))})


@router.get("/api/v1/packets/{packet_id}/resume.docx")
async def packet_resume_docx(packet_id: uuid.UUID, session: ReadSession) -> Response:
    packet, role = await _packet_and_role(session, packet_id)
    data = await asyncio.to_thread(
        docs_render.resume_docx, packet.resume, margin_inches=await _page_margin(session))
    return Response(data, media_type=DOCX,
                    headers={"Content-Disposition": attachment(_filename(packet, role, "Resume"))})


@router.get("/api/v1/packets/{packet_id}/letter.docx")
async def packet_letter_docx(packet_id: uuid.UUID, session: ReadSession) -> Response:
    packet, role = await _packet_and_role(session, packet_id)
    if not (packet.letter or {}).get("paragraphs"):
        raise NotFound("this packet has no cover letter")
    data = await asyncio.to_thread(
        docs_render.letter_docx, packet.letter, packet.resume, role.company if role else "",
        margin_inches=await _page_margin(session), role_title=role.title if role else "")
    return Response(data, media_type=DOCX,
                    headers={"Content-Disposition": attachment(_filename(packet, role, "CoverLetter"))})


@router.get("/api/v1/export.xlsx")
@router.get("/export.xlsx")
async def export_xlsx(session: ReadSession) -> Response:
    tracker_rows, screened_rows = await tracker.export_rows(session, settings.owner)
    # The read is done: let go of the connection and its transaction before the workbook is written.
    await session.rollback()
    data = await tracker.build_workbook(tracker_rows, screened_rows)
    return Response(
        data, media_type=XLSX,
        headers={"Content-Disposition": 'attachment; filename="resumebot-tracker.xlsx"'},
    )
