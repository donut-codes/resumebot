"""Reading the text of a resume file (Me, Import your resume): a Word file, a PDF and a plain text file give their text;
a scan, a damaged file, a kind that is not read and a file too big or too short say so in a sentence for the owner.
Small files made in memory (`resume_files.py`); no one's resume."""

import io
import zipfile

import pytest
from docx import Document
from pypdf import PdfReader, PdfWriter

from app.jobsearch.resume import text as resume_text
from tests.resume_files import RESUME_LINES, docx_resume, pdf_resume, scanned_pdf


def _lines(text: str) -> list[str]:
    return text.split("\n")


def _saved(writer: PdfWriter) -> bytes:
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_a_word_file_gives_its_paragraphs_in_order():
    text = resume_text.extract("My Resume.DOCX", docx_resume())
    assert _lines(text) == RESUME_LINES


def test_a_word_file_gives_a_table_a_line_for_each_row_and_the_page_header_first():
    data = docx_resume(
        RESUME_LINES[2:4], header="Pat Example, pat@example.test",
        table=[["Skills", "Risk frameworks; Policy design"], ["Tools", "Visio"]])
    lines = _lines(resume_text.extract("resume.docx", data))
    assert lines[0] == "Pat Example, pat@example.test", "a template's name and contact details are often in the header"
    assert lines[1:3] == RESUME_LINES[2:4]
    assert lines[3:] == ["Skills | Risk frameworks; Policy design", "Tools | Visio"]


def test_a_merged_table_cell_is_read_once():
    document = Document()
    document.add_paragraph(RESUME_LINES[3])
    grid = document.add_table(rows=1, cols=3)
    grid.cell(0, 0).merge(grid.cell(0, 1)).text = "Wide cell"
    grid.cell(0, 2).text = "Narrow cell"
    out = io.BytesIO()
    document.save(out)
    assert _lines(resume_text.extract("resume.docx", out.getvalue()))[1:] == ["Wide cell | Narrow cell"]


def test_a_pdf_gives_the_text_of_every_page():
    text = resume_text.extract("resume.pdf", pdf_resume(pages=2))
    for line in RESUME_LINES:
        assert text.count(line) == 2, line


def test_a_plain_text_file_is_read_as_utf8_utf16_or_windows_text():
    body = "\n".join(RESUME_LINES)
    assert resume_text.extract("resume.txt", body.encode("utf-8")) == body
    with_mark = b"\xef\xbb\xbf" + body.encode("utf-8")
    assert resume_text.extract("resume.txt", with_mark) == body, "a byte order mark is dropped"
    assert resume_text.extract("resume.txt", body.encode("utf-16")) == body
    accented = body.replace("Pat Example", "Pat Exämple")
    assert resume_text.extract("resume.txt", accented.encode("cp1252")) == accented


def test_the_text_is_tidied_lines_trimmed_and_runs_of_blank_lines_made_one():
    raw = "  Pat Example  \r\n\r\n\r\n\r\nSummary of work\t done  \r\n" + "x" * 60
    assert resume_text.extract("resume.txt", raw.encode()) == "Pat Example\n\nSummary of work done\n" + "x" * 60


def test_a_pdf_that_is_only_a_picture_has_no_text_to_read_and_says_so():
    with pytest.raises(resume_text.ReadError) as error:
        resume_text.extract("scan.pdf", scanned_pdf())
    assert str(error.value) == "This file has no text to read; paste the text instead."
    blank = PdfWriter()
    blank.add_blank_page(width=612, height=792)
    with pytest.raises(resume_text.ReadError, match="no text to read"):
        resume_text.extract("blank.pdf", _saved(blank))


@pytest.mark.parametrize("name, data", [
    ("resume.docx", b"this is not a zip file"),
    ("resume.pdf", b"this is not a pdf"),
    ("resume.pdf", b"%PDF-1.4\nnot really"),
    ("resume.txt", b"\x00\x01\x02 binary " * 10),
])
def test_a_file_that_cannot_be_read_says_so_and_offers_pasting(name, data):
    with pytest.raises(resume_text.ReadError) as error:
        resume_text.extract(name, data)
    assert str(error.value) == "This file couldn't be read. Choose another, or paste the text instead."


def _zip(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return out.getvalue()


def test_a_word_file_that_unpacks_to_far_more_than_it_weighs_is_refused_unopened():
    """The 5 MB cap is on the compressed file; a zip of zeros is a few KB and unpacks to hundreds of MB."""
    bomb = _zip({"word/document.xml": b"\x00" * (resume_text.MAX_DOCX_UNPACKED + 1)})
    assert len(bomb) < resume_text.MAX_BYTES
    with pytest.raises(resume_text.ReadError, match="couldn't be read"):
        resume_text.extract("resume.docx", bomb)


def test_a_word_file_with_hundreds_of_parts_is_refused_and_an_ordinary_one_is_not():
    many = _zip({f"part{n}.xml": b"x" for n in range(resume_text.MAX_DOCX_MEMBERS + 1)})
    with pytest.raises(resume_text.ReadError, match="couldn't be read"):
        resume_text.extract("resume.docx", many)
    with zipfile.ZipFile(io.BytesIO(docx_resume())) as archive:
        assert len(archive.infolist()) < resume_text.MAX_DOCX_MEMBERS
    assert resume_text.extract("resume.docx", docx_resume())


def test_a_password_protected_pdf_cannot_be_read():
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_resume())))
    writer.encrypt("secret")
    with pytest.raises(resume_text.ReadError, match="couldn't be read"):
        resume_text.extract("locked.pdf", _saved(writer))


@pytest.mark.parametrize("name", ["resume.doc", "resume.rtf", "resume.pages", "resume", "resume.docx.exe", ""])
def test_only_docx_pdf_and_txt_are_read(name):
    with pytest.raises(resume_text.ReadError) as error:
        resume_text.extract(name, b"x" * 100)
    assert str(error.value) == resume_text.WRONG_KIND and ".docx, .pdf or .txt" in resume_text.WRONG_KIND


def test_a_file_with_too_little_text_or_too_big_is_refused_in_words():
    with pytest.raises(resume_text.ReadError, match="too little text"):
        resume_text.extract("resume.txt", b"Pat Example, a resume")
    with pytest.raises(resume_text.ReadError, match="no text to read"):
        resume_text.extract("resume.txt", b" \n \n")
    with pytest.raises(resume_text.ReadError, match="no text to read"):
        resume_text.extract("resume.txt", b"")
    with pytest.raises(resume_text.ReadError, match="over 5 MB"):
        resume_text.extract("resume.txt", b"x" * (resume_text.MAX_BYTES + 1))
    assert resume_text.extract("resume.txt", b"x" * 50) == "x" * 50


def test_a_file_with_a_lot_of_text_is_read_whole_and_the_route_checks_its_room():
    """There is no most on a file's text here: how much an agent can be sent with it is said when the file is chosen
    (`parse_routes`), with the size, never by cutting it."""
    assert resume_text.extract("resume.txt", b"word " * 9000) == ("word " * 9000).strip()
    assert resume_text.extract("resume.txt", b"word " * 60000) == ("word " * 60000).strip()
