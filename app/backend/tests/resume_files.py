"""Small resume files made in memory for the tests of reading a resume's text (Me, Import your resume): a Word file, a
PDF with text, a PDF that is only a picture, and the made-up resume they hold. Nothing here is anyone's resume: every
name, employer and sentence is invented."""

import io

#: What a made-up resume says, a line each, as a person would write it down.
RESUME_LINES = [
    "Pat Example",
    "Oakfield, ST | 555-0100 | pat@example.test | example.test/in/pat",
    "SUMMARY",
    "Builds programs that people use and the teams that run them.",
    "EXPERIENCE",
    "Director of Programs, Northwind Labs, Oakfield, ST, 2019 to Present",
    "Led the risk working group across four departments.",
    "Built the knowledge base the whole practice uses.",
    "EDUCATION",
    "MS in Leadership, Example University",
]


def docx_resume(
    lines: list[str] = RESUME_LINES, *, header: str | None = None, table: list[list[str]] | None = None
) -> bytes:
    """A Word file: a paragraph for each line, then `table` (rows of cells) when given, `header` in the page header."""
    from docx import Document

    document = Document()
    if header is not None:
        section = document.sections[0]
        section.header.is_linked_to_previous = False
        section.header.paragraphs[0].text = header
    for line in lines:
        document.add_paragraph(line)
    if table:
        grid = document.add_table(rows=len(table), cols=max(len(row) for row in table))
        for r, row in enumerate(table):
            for c, cell in enumerate(row):
                grid.cell(r, c).text = cell
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _escape(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def _pdf(streams: list[bytes]) -> bytes:
    """A PDF with one page for each content stream: the page's objects written by hand, with a Helvetica font and a
    one-pixel picture it may draw."""
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(len(streams)))
    body: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 "
        b"/Length 1 >>\nstream\n\xff\nendstream",
    ]
    for i, stream in enumerate(streams):
        body.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {6 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 4 0 R >> >> >>".encode())
        body.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(body, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(body) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(body) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def pdf_resume(lines: list[str] = RESUME_LINES, *, pages: int = 1) -> bytes:
    """A PDF whose text is `lines`, a line to each row, repeated on each of `pages` pages."""
    text = b"BT /F1 11 Tf 72 720 Td 14 TL " + b" T* ".join(b"(" + _escape(line) + b") Tj" for line in lines) + b" ET"
    return _pdf([text] * pages)


def scanned_pdf() -> bytes:
    """A PDF that is a picture of a page: it draws an image and has no text, as a scan has."""
    return _pdf([b"q 400 0 0 400 100 200 cm /Im1 Do Q"])
