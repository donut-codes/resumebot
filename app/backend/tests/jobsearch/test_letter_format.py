import io
from datetime import date

from docx import Document
from docx.shared import Pt

from app.jobsearch.writing import docs_render

PERSON = {"name": "Pat Example", "contact": ["Anytown, ST", "555-0100", "pat@example.com"], "roles": []}
LETTER = {"paragraphs": ["Dear Hiring Manager,", "First paragraph.", "Second paragraph.", "Sincerely,", "Pat Example"]}
DAY = date(2026, 10, 5)


def _docx(**kw):
    return Document(io.BytesIO(docs_render.letter_docx(LETTER, PERSON, "Globex", DAY, **kw)))


def test_the_letter_reads_in_order_with_the_signature_block_once():
    texts = [p.text for p in _docx(role_title="Director").paragraphs]
    assert texts == ["Pat Example", "Anytown, ST | 555-0100 | pat@example.com", "October 5, 2026", "Globex",
                     "Re: Director", "Dear Hiring Manager,", "First paragraph.", "Second paragraph.", "Sincerely,",
                     "Pat Example"]


def test_a_letter_with_no_salutation_or_closing_gets_both():
    doc = Document(io.BytesIO(docs_render.letter_docx({"paragraphs": ["Body."]}, PERSON, "Globex", DAY)))
    texts = [p.text for p in doc.paragraphs]
    assert texts[-4:] == ["Dear Hiring Manager,", "Body.", "Sincerely,", "Pat Example"]
    assert "Re:" not in " ".join(texts)


def test_the_letterhead_matches_the_resume_and_the_file_has_no_tables():
    doc = _docx()
    name, contact = doc.paragraphs[0], doc.paragraphs[1]
    assert name.alignment == contact.alignment == 1 and name.runs[0].bold
    assert name.runs[0].font.size == Pt(16)
    assert doc.styles["Normal"].font.name == "Calibri"
    assert not doc.tables and "<w:txbxContent" not in doc.element.xml


def test_the_pdf_carries_the_same_lines_in_the_same_order():
    from pypdf import PdfReader
    pdf = docs_render.letter_pdf(LETTER, PERSON, "Globex", DAY, role_title="Director")
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    order = ["Pat Example", "Anytown, ST", "October 5, 2026", "Globex", "Re: Director", "Dear Hiring Manager,",
             "First paragraph.", "Second paragraph.", "Sincerely,"]
    positions = [text.index(x) for x in order]
    assert positions == sorted(positions)
    assert text.count("Sincerely,") == 1 and text.count("Dear Hiring Manager,") == 1
