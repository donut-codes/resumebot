"""Reading the text of a resume file (Me, Import your resume): the owner chooses a `.docx`, `.pdf` or `.txt` file and
the server reads its text, which then goes to the parser agent (`resume/parse.py`). Only the text is read: no layout,
no pictures, no links.

A Word file is read in the order of the page (paragraphs and tables, with the header's text first, where many resume
templates keep the name and the contact details). A PDF is read a page at a time. A PDF that is a picture of a page (a
scan) has no text and says so. A file that cannot be read says so in a sentence for the owner, who can paste the text
instead. Nothing is kept: the file is read and let go.
"""

import io
import logging
import re
import zipfile

from app.errors import DomainError
from app.jobsearch.resume import parse as resume_parse

log = logging.getLogger(__name__)

#: The largest file read, in bytes.
MAX_BYTES = 5 * 1024 * 1024
#: A Word file is refused when its parts unpack to more than this many bytes, or when it has more parts than this.
MAX_DOCX_UNPACKED = 50 * 1024 * 1024
MAX_DOCX_MEMBERS = 500
KINDS = (".docx", ".pdf", ".txt")

WRONG_KIND = "Only a .docx, .pdf or .txt file can be read. Choose one of those, or paste the text instead."
UNREADABLE = "This file couldn't be read. Choose another, or paste the text instead."
TOO_BIG = "This file is over 5 MB, too big to read. Choose a smaller one, or paste the text instead."
NO_TEXT = "This file has no text to read; paste the text instead."
TOO_SHORT = f"This file has too little text to be a resume (under {resume_parse.MIN_CHARS} characters)."


class ReadError(DomainError):
    """A file that cannot be used; the message is the sentence for the owner."""

    status = 422


def _kind(name: str) -> str:
    name = (name or "").strip().lower()
    for kind in KINDS:
        if name.endswith(kind):
            return kind
    raise ReadError(WRONG_KIND)


def _docx_blocks(container) -> list[str]:
    """The lines of a Word container (the document, a page header or a table cell) in page order: a paragraph is a line,
    and a table is a line for each row, its cells divided by ` | `."""
    from docx.table import Table

    lines: list[str] = []
    for block in container.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                cells: list[str] = []
                seen = set()
                for cell in row.cells:
                    if id(cell._tc) in seen:  # a merged cell shows once for each column it spans
                        continue
                    seen.add(id(cell._tc))
                    text = " ".join(line.strip() for line in _docx_blocks(cell) if line.strip())
                    if text:
                        cells.append(text)
                if cells:
                    lines.append(" | ".join(cells))
        else:
            lines.append(block.text)
    return lines


def _docx_text(data: bytes) -> str:
    from docx import Document

    # A .docx is a zip, and 5 MB of it can unpack to gigabytes: read the zip's own sizes before opening it.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
    if len(members) > MAX_DOCX_MEMBERS or sum(m.file_size for m in members) > MAX_DOCX_UNPACKED:
        raise ReadError(UNREADABLE)
    document = Document(io.BytesIO(data))
    head: list[str] = []
    for section in document.sections:
        for header in (section.header, section.first_page_header):
            if header.is_linked_to_previous:
                continue
            for line in _docx_blocks(header):
                if line.strip() and line not in head:
                    head.append(line)
    return "\n".join([*head, *_docx_blocks(document)])


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ReadError(UNREADABLE)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _plain_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _tidy(text: str) -> str:
    """The text with its lines trimmed, runs of blank lines made one, and the odd spaces of a document made plain."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").replace("\u200b", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract(name: str, data: bytes) -> str:
    """The text of the file `name` holds, for the parser, however much of it there is. Raises `ReadError` with the
    sentence for the owner: not a kind of file that is read, too big (5 MB), damaged, with no text (a scan) or too
    little. Whether an agent can be sent the text is the route's to say (`parse_routes.py`), with its size."""
    kind = _kind(name)
    if len(data) > MAX_BYTES:
        raise ReadError(TOO_BIG)
    if not data:
        raise ReadError(NO_TEXT)
    try:
        raw = {".docx": _docx_text, ".pdf": _pdf_text, ".txt": _plain_text}[kind](data)
    except ReadError:
        raise
    except Exception:  # whatever is wrong with the file, the owner is told it could not be read
        log.info("a %s file could not be read", kind, exc_info=True)
        raise ReadError(UNREADABLE) from None
    if "\x00" in raw:  # a file that is not text at all
        raise ReadError(UNREADABLE)
    text = _tidy(raw)
    if not text:
        raise ReadError(NO_TEXT)
    if len(text) < resume_parse.MIN_CHARS:
        raise ReadError(TOO_SHORT)
    return text
