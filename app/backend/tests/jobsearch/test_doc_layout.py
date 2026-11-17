"""The layout files: a bad one is refused by name, and an edit to one changes the Word and the PDF alike."""

import io
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader

from app.config import settings
from app.jobsearch.writing import docs_render
from app.jobsearch.writing.doc_layout import LayoutError, flow, load_layout, parse_layout
from tests.jobsearch.test_doc_golden import FULL

RESUME_YAML = (Path(settings.templates_path) / "document.resume.layout.yaml").read_text(encoding="utf-8")


def test_a_missing_layout_file_is_refused_and_named(tmp_path):
    with pytest.raises(LayoutError, match="document.resume.layout.yaml"):
        load_layout("resume", tmp_path)


@pytest.mark.parametrize("bad", ["page: [", "blocks: []", "page: {font: x}\nblocks: []",
                                 RESUME_YAML.replace("{kind: line, field: name", "{kind: table, field: name"),
                                 RESUME_YAML.replace("field: name, size: 16", "field: name, size: big"),
                                 RESUME_YAML.replace("bold: true, align: center", "bold: true, align: right", 1),
                                 RESUME_YAML.replace("align: center, after: 0", "align: center, colour: red", 1)])
def test_a_malformed_layout_is_refused_and_named(bad):
    with pytest.raises(LayoutError, match=r"somewhere\.yaml"):
        parse_layout("resume", bad, "somewhere.yaml")


def test_the_shipped_layouts_have_one_column_and_no_tables_or_boxes():
    for layout in (docs_render.RESUME_LAYOUT, docs_render.LETTER_LAYOUT):
        assert {b["kind"] for b in layout.blocks} <= {"line", "paragraphs", "recipient", "section"}


def test_a_layout_edit_changes_the_word_file_and_the_pdf_alike(monkeypatch):
    edited = RESUME_YAML.replace("Professional Experience", "Work History")
    edited = edited.replace("field: name, size: 16", "field: name, size: 20")
    layout = parse_layout("resume", edited, "edited.yaml")
    monkeypatch.setattr(docs_render, "RESUME_LAYOUT", layout)
    docx = Document(io.BytesIO(docs_render.resume_docx(FULL)))
    assert "WORK HISTORY" in [p.text for p in docx.paragraphs]
    assert docx.paragraphs[0].runs[0].font.size.pt == 20
    pdf = "".join(page.extract_text() for page in PdfReader(io.BytesIO(docs_render.resume_pdf(FULL))).pages)
    assert "WORK HISTORY" in pdf
    assert [x.parts[0].text for x in flow(layout, FULL) if x.kind == "heading"][3] == "WORK HISTORY"
