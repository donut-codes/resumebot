"""A packet as Word and PDF documents (docs/PRD.md 12.4): the tailored resume and the cover letter, from what the packet
holds, laid out the way applicant-tracking systems read best (one column, plain headings, the employer on its own line
with each title and its dates below it, bullets as Word list bullets, nothing in tables, headers or text boxes).

The layouts are data: `templates/document.resume.layout.yaml` and `document.letter.layout.yaml` (see `doc_layout.py`).
`doc_layout.flow` turns a layout and the packet's text into the lines of the document, and the Word and the PDF writer
below each draw that same list, so the two carry the same lines in the same order. Fonts, the PDF's glyph handling and
the letter's salutation and closing stay here."""

import io
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.jobsearch.writing.doc_layout import Layout, Line, flow, load_layout
from app.timeutil import owner_today

if TYPE_CHECKING:
    from docx.document import Document as DocumentType

# docx and fpdf are imported inside the functions that use them: the page loads this module at start (so a broken
# layout file stops it there) but should not hold the Word and PDF libraries until the first document is made.

# Read once, when the app starts: a missing or malformed layout file stops it, naming the file.
RESUME_LAYOUT = load_layout("resume")
LETTER_LAYOUT = load_layout("letter")


def _margins(layout: Layout, margin_inches: float | None) -> tuple[float, float]:
    """(left and right, top and bottom) in inches: the owner's page margin on all four sides, or the layout's own."""
    if margin_inches is None:
        return layout.page["side_margin"], layout.page["end_margin"]
    return margin_inches, margin_inches


# --- Word -------------------------------------------------------------------------------------------------------------


def _base(layout: Layout, margin_inches: float | None = None) -> "DocumentType":
    """A blank document with its page margins and base style."""
    from docx import Document
    from docx.shared import Inches, Pt

    doc = Document()
    side, end = _margins(layout, margin_inches)
    for section in doc.sections:
        section.left_margin = section.right_margin = Inches(side)
        section.top_margin = section.bottom_margin = Inches(end)
    style = doc.styles["Normal"]
    style.font.name = layout.page["font"]
    style.font.size = Pt(layout.page["size"])
    style.paragraph_format.space_after = Pt(layout.page["after"])
    # Clean metadata: nothing about who or what made the file.
    props = doc.core_properties
    props.author = props.last_modified_by = props.comments = ""
    return doc


def _rule_border(p, rule: dict[str, Any]) -> None:
    """A thin rule under a paragraph, drawn as its bottom border (no table, no shape)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    values = (("w:val", "single"), ("w:sz", str(round(rule.get("width", 0.75) * 8))),
              ("w:space", str(round(rule.get("space", 1)))), ("w:color", rule.get("color", "808080")))
    for key, value in values:
        bottom.set(qn(key), value)
    borders.append(bottom)
    p._p.get_or_add_pPr().append(borders)


def _draw_docx(doc: "DocumentType", line: Line) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    style = line.style
    if line.kind == "bullet":
        p = doc.add_paragraph(line.parts[0].text, style="List Bullet")
    else:
        p = doc.add_paragraph()
        for part in line.parts:
            if line.plain and not part.text:
                continue
            run = p.add_run(part.text)
            if line.plain:
                run.bold, run.italic = part.bold, part.italic
            elif part.bold:
                run.bold = True
            if style.get("size"):
                run.font.size = Pt(style["size"])
    if style.get("align") == "center":
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if "after" in style:
        p.paragraph_format.space_after = Pt(style["after"])
    if "before" in style:
        p.paragraph_format.space_before = Pt(style["before"])
    if style.get("keep"):
        p.paragraph_format.keep_with_next = True
    if "rule" in style:
        _rule_border(p, style["rule"])


def _docx(layout: Layout, values: dict[str, Any], margin_inches: float | None) -> bytes:
    doc = _base(layout, margin_inches)
    for line in flow(layout, values):
        _draw_docx(doc, line)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def resume_docx(resume: dict, *, margin_inches: float | None = None) -> bytes:
    return _docx(RESUME_LAYOUT, resume, margin_inches)


_CLOSINGS = ("sincerely", "best regards", "regards", "kind regards", "respectfully", "warm regards", "best",
             "yours truly", "yours sincerely", "thank you", "thanks")


def _letter_parts(letter: dict, name: str) -> tuple[str, list[str], str]:
    """The letter as it prints: the salutation, the body paragraphs, the closing. The writer's own first paragraph is
    the salutation when it opens "Dear", and its last is the closing when it is only a sign-off (with or without the
    name under it); either missing is supplied here, so the letter always opens and closes the same way."""
    paras = [str(t).strip() for t in letter.get("paragraphs", []) if str(t or "").strip()]
    greeting = "Dear Hiring Manager,"
    if paras and re.match(r"dear[ ,]", paras[0], re.I) and len(paras[0]) < 80:
        greeting = paras.pop(0)
    while paras and (paras[-1].lower().rstrip(",.! ") in _CLOSINGS or (name and paras[-1].strip() == name)):
        paras.pop()
    return greeting, paras, "Sincerely,"


def _date_line(today: date | None) -> str:
    return (today or owner_today()).strftime("%B %d, %Y").replace(" 0", " ")


def _letter_values(letter: dict, resume: dict, company: str, today: date | None, role_title: str) -> dict[str, Any]:
    """What the letter layout's blocks are filled from."""
    name = resume.get("name") or ""
    greeting, body, closing = _letter_parts(letter, name)
    return {"name": name, "contact": resume.get("contact") or [], "date": _date_line(today), "company": company,
            "role_title": role_title, "greeting": greeting, "body": body, "closing": closing, "signature": name}


def letter_docx(letter: dict, resume: dict, company: str = "", today: date | None = None, *,
                margin_inches: float | None = None, role_title: str = "") -> bytes:
    """The cover letter (`document.letter.layout.yaml`): the resume letterhead, the date, the company and an optional
    "Re:" line, the salutation, the body, the closing with room to sign, and the name. One column of plain paragraphs:
    no tables, no text boxes."""
    return _docx(LETTER_LAYOUT, _letter_values(letter, resume, company, today, role_title), margin_inches)


# --- the same documents as PDF ---------------------------------------------------------------------------------------
#
# fpdf2 is pure Python, so a PDF needs no Word, LibreOffice or system library. The text is set in DejaVu Sans, a
# Unicode font that travels with the app (`fonts/`, licence in PROVENANCE.md), so curly quotes, dashes and the letters
# of any name come out as written. Only marks that have no glyph or no business in a resume (zero-width and no-break
# spaces) are changed.

FONT_DIR = Path(__file__).parent / "fonts"
PDF_FONT = "DejaVu"
_PDF_FONT_FILES = {"": "DejaVuSans.ttf", "B": "DejaVuSans-Bold.ttf", "I": "DejaVuSans-Oblique.ttf",
                   "BI": "DejaVuSans-BoldOblique.ttf"}

_PDF_MARKS = str.maketrans({" ": " ", " ": " ", " ": " ", "​": ""})


def _pdf_text(text: str) -> str:
    """`text` as the PDF sets it: as written, minus the spaces no reader needs."""
    return str(text or "").translate(_PDF_MARKS)


class _Pdf:
    """A page-flowing document with the Word file's margins and sizes: DejaVu Sans, one column, no tables."""

    def __init__(self, layout: Layout, margin_inches: float | None = None) -> None:
        self.size = float(layout.page["size"])
        self.after = float(layout.page["after"])
        side, end = _margins(layout, margin_inches)
        from fpdf import FPDF

        self.pdf = FPDF(unit="pt", format="Letter")
        self.pdf.set_margins(left=side * 72, top=end * 72, right=side * 72)
        self.pdf.set_auto_page_break(auto=True, margin=end * 72)
        for style, name in _PDF_FONT_FILES.items():
            self.pdf.add_font(PDF_FONT, style, str(FONT_DIR / name))
        self.pdf.add_page()

    def lines(self, parts, *, size: float | None = None, center: bool = False, after: float | None = None,
              before: float = 0, indent: float = 0, keep: bool = False) -> None:
        """One paragraph. `parts` is a string, or a list of (text, style) with style "", "B", "I" or "BI"."""
        pdf = self.pdf
        size = self.size if size is None else size
        after = self.after if after is None else after
        if isinstance(parts, str):
            parts = [(parts, "")]
        parts = [(_pdf_text(text), style) for text, style in parts if text]
        if not parts:
            return
        height = size * 1.25
        if keep and pdf.will_page_break(height * 3 + before):  # a heading or an employer line stays with what follows
            pdf.add_page()
        pdf.set_y(pdf.get_y() + before)
        margin = pdf.l_margin
        pdf.set_left_margin(margin + indent)
        pdf.set_x(margin + indent)
        if center:
            text, style = parts[0]
            pdf.set_font(PDF_FONT, style, size)
            pdf.multi_cell(0, height, text, align="C", new_x="LMARGIN", new_y="NEXT")
        else:
            for text, style in parts:
                pdf.set_font(PDF_FONT, style, size)
                pdf.write(height, text)
            pdf.ln(height)
        pdf.set_left_margin(margin)
        pdf.set_y(pdf.get_y() + after)

    def rule(self, space: float, after: float, width: float, color: str) -> None:
        """A rule `space` below the text above it, then `after` of room below the rule."""
        pdf = self.pdf
        y = pdf.get_y() + space
        pdf.set_draw_color(*(int(color[i:i + 2], 16) for i in (0, 2, 4)))
        pdf.set_line_width(width)
        pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
        pdf.set_y(y + after)

    def bullet(self, text: str, after: float) -> None:
        pdf = self.pdf
        if pdf.will_page_break(14):
            pdf.add_page()
        pdf.set_font(PDF_FONT, "", self.size)
        pdf.set_xy(pdf.l_margin + 10, pdf.get_y())
        pdf.cell(10, 13, "-")
        pdf.set_xy(pdf.l_margin, pdf.get_y())
        self.lines(text, indent=24, after=after)

    def draw(self, line: Line) -> None:
        style = line.style
        after, before = style.get("after"), style.get("before", 0)
        if line.kind == "bullet":
            self.bullet(line.parts[0].text, self.after if after is None else after)
            return
        parts = [(p.text, ("B" if p.bold else "") + ("I" if p.italic else "")) for p in line.parts]
        rule = style.get("rule")
        center, size, keep = style.get("align") == "center", style.get("size"), bool(style.get("keep"))
        if rule:  # the line's own `after` is the room under the rule
            self.lines(parts, size=size, center=center, after=0, before=before, keep=keep)
            self.rule(rule.get("space", 1), self.after if after is None else after, rule.get("width", 0.75),
                      rule.get("color", "808080"))
        else:
            self.lines(parts, size=size, center=center, after=after, before=before, keep=keep)

    def bytes(self) -> bytes:
        return bytes(self.pdf.output())


def _pdf(layout: Layout, values: dict[str, Any], margin_inches: float | None) -> bytes:
    doc = _Pdf(layout, margin_inches)
    for line in flow(layout, values):
        doc.draw(line)
    return doc.bytes()


def resume_pdf(resume: dict, *, margin_inches: float | None = None) -> bytes:
    """The resume as a PDF: the lines `resume_docx` writes, in the same order."""
    return _pdf(RESUME_LAYOUT, resume, margin_inches)


def letter_pdf(letter: dict, resume: dict, company: str = "", today: date | None = None, *,
               margin_inches: float | None = None, role_title: str = "") -> bytes:
    """The cover letter as a PDF: the lines `letter_docx` writes, in the same order."""
    return _pdf(LETTER_LAYOUT, _letter_values(letter, resume, company, today, role_title), margin_inches)
