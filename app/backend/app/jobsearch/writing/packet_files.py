"""A packet's files on disk: the names the downloads carry, and the folder a finished packet is saved to.

The owner can name a folder in the settings file (`RESUMEBOT_PACKET_DIR`). When a packet passes its checks, the resume
and the cover letter are written there, as PDF and Word, into one folder per role: `<Company>_<Role title>/`. A newer
version of the packet replaces the files of the one before. Unset, nothing is written. The app is the only writer.
Saving never stops a packet: a failure (the folder is missing, a sync client holds a file) is logged and left as a note
on the role."""

import asyncio
import logging
import os
import re
import unicodedata
from pathlib import Path

from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.profile import store as profile_store
from app.jobsearch.writing import docs_render
from app.settings import settings

log = logging.getLogger(__name__)

#: A folder name part is cut to this many characters, so a long title cannot push a path past what Windows allows.
PART_LIMIT = 60


def owner_file_name(name: str) -> str:
    """A name as a file-name stem: accents dropped, apostrophes dropped, any other run of spaces or punctuation one
    underscore ("Mary Ann O'Neil" -> "Mary_Ann_ONeil"). Empty when nothing of the name is left."""
    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", re.sub(r"['`]", "", ascii_name)).strip("_")


def file_name(packet, role, what: str, extension: str = "docx") -> str:
    """`FirstName_LastName_Resume.docx` or `..._CoverLetter.docx` (`.pdf` the same), from the master resume's name; with
    no name, the older `<name>_<company>_<what>_v<version>.docx`."""
    stem = owner_file_name(packet.resume.get("name") or "")
    if stem:
        return f"{stem}_{what}.{extension}"
    company = "".join(ch for ch in (role.company if role else "") if ch.isalnum())
    return f"resume_{company}_{what}_v{packet.version}.{extension}"


def role_folder_name(role: Opportunity | None) -> str:
    """`<Company>_<Role title>`, written the way the file names are."""
    parts = [owner_file_name(text)[:PART_LIMIT].strip("_") for text in ((role.company, role.title) if role else ())]
    return "_".join(part for part in parts if part) or "role"


def _write(folder: Path, name: str, data: bytes) -> None:
    """The file under its name, whole or not at all: written beside it and moved over the old one."""
    target = folder / name
    partial = folder / f".{name}.part"
    try:
        partial.write_bytes(data)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def _save(root: Path, role: Opportunity | None, files: dict[str, bytes], stale: list[str]) -> Path:
    """Write `files` into the role's folder (made here, and `root` itself when it is missing; a missing parent of
    `root` still fails) and remove what an earlier version left that this one has no counterpart for."""
    root.mkdir(exist_ok=True)
    folder = root / role_folder_name(role)
    folder.mkdir(exist_ok=True)
    for name, data in files.items():
        _write(folder, name, data)
    for name in stale:
        (folder / name).unlink(missing_ok=True)
    return folder


def enabled() -> bool:
    """Whether the owner has named a packet folder."""
    return bool((settings.packet_dir or "").strip())


async def save(session, packet: Packet, role: Opportunity | None) -> str | None:
    """Save a ready packet's resume and letter as PDF and Word. Returns None when it was saved or no folder is set,
    else the reason it was not. Never raises."""
    where = (settings.packet_dir or "").strip()
    if not where:
        return None
    try:
        margin = (await profile_store.owner_documents(session, packet.owner)).writing.page_margin_inches
        company = role.company if role else ""
        files = {
            file_name(packet, role, "Resume", "pdf"): await asyncio.to_thread(
                docs_render.resume_pdf, packet.resume, margin_inches=margin),
            file_name(packet, role, "Resume", "docx"): await asyncio.to_thread(
                docs_render.resume_docx, packet.resume, margin_inches=margin),
        }
        stale = [file_name(packet, role, "CoverLetter", ext) for ext in ("pdf", "docx")]
        if (packet.letter or {}).get("paragraphs"):
            files[stale[0]] = await asyncio.to_thread(
                docs_render.letter_pdf, packet.letter, packet.resume, company, margin_inches=margin,
                role_title=role.title if role else "")
            files[stale[1]] = await asyncio.to_thread(
                docs_render.letter_docx, packet.letter, packet.resume, company, margin_inches=margin,
                role_title=role.title if role else "")
            stale = []
        root = Path(where).expanduser()
        await asyncio.to_thread(_save, root, role, files, stale)
    except Exception as err:  # whatever went wrong, the packet is not stopped by it
        log.warning("could not save packet %s to %s: %s", packet.id, where, err)
        return str(err) or err.__class__.__name__
    return None
