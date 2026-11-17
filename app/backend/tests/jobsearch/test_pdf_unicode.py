"""The PDF sets text in an embedded Unicode font: an accented name, curly quotes and dashes come out as written,
not flattened to plain marks or "?". Made-up data only."""

import io

from pypdf import PdfReader

from app.jobsearch.writing import docs_render

NAME = "Zoë Łukasiewicz-Ñandú"
SUMMARY = "She said “ship it” – it’s done — Škoda, Dvořák, Øresund"


def _text(data: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def test_an_accented_name_and_curly_quotes_come_out_of_the_resume_pdf_intact():
    text = _text(docs_render.resume_pdf({"name": NAME, "contact": [], "summary": SUMMARY, "roles": []}))
    assert NAME in text
    for mark in "“”–’—":
        assert mark in text
    assert "Škoda, Dvořák, Øresund" in text
    assert "?" not in text


def test_the_pdf_font_is_embedded_not_helvetica():
    data = docs_render.resume_pdf({"name": NAME, "summary": "x", "roles": []})
    fonts = {str(f["/BaseFont"]) for page in PdfReader(io.BytesIO(data)).pages
             for f in (r.get_object() for r in page["/Resources"]["/Font"].values())}
    assert any("DejaVuSans" in f for f in fonts)
    assert not any("Helvetica" in f for f in fonts)
