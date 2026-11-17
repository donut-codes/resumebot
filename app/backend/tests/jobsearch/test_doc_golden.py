"""The Word and PDF documents render as they always have: the text and structure of each, for made-up packets, are
compared with a golden file (`golden/documents.json`) written from the code that had the layouts in Python. A layout
change on purpose is a template edit (`app/templates/document.*.layout.yaml`) and a rewritten golden file:
`python -m tests.jobsearch.test_doc_golden --write` from `app/backend`."""

import functools
import io
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader

from app.jobsearch.writing import docs_render

GOLDEN = Path(__file__).parent / "golden" / "documents.json"
DAY = date(2026, 10, 5)

FULL = {
    "name": "Pat Example",
    "contact": ["Anytown, ST", "555-0100", "pat@example.test"],
    "headline": "Operations Leader | Process | Analytics",
    "summary": "Runs teams that ship. Café “quotes” and dashes – kept.",
    "competencies": [{"area": "Operations", "items": ["Planning", "Budgets", "Vendors"]},
                     {"area": "Tools", "items": ["SQL"]}, {"area": "Empty", "items": []}],
    "achievements": [{"text": "Cut cycle time by a third."}, {"text": "Opened two sites."}, {}],
    "roles": [
        {"employer": "Globex Corp", "location": "Anytown, ST", "title": "Director", "start": "Jan 2020",
         "end": "Present", "descriptor": "A regional manufacturer.", "intro": "Led the whole plant.",
         "bullets": [{"text": "Built the plan."}, {"text": "Hired nine people."}]},
        {"employer": "Initech", "title": "Manager", "start": "2015", "bullets": []},
        {"employer": "", "title": "Analyst", "end": "2014"},
    ],
    "education": [{"degree": "BS, Economics", "school": "State University"}, {"degree": "Certificate"}],
}
SPARSE = {"name": "Sam Sample", "roles": [{"employer": "Acme", "title": "Clerk"}]}
NOTHING: dict = {}
LONG = {**FULL, "roles": FULL["roles"] * 8, "achievements": FULL["achievements"] * 12}

LETTERS = {
    "full": {"paragraphs": ["Dear Ms. Rivera,", "First paragraph.", "Second paragraph.", "Sincerely,", "Pat Example"]},
    "bare": {"paragraphs": ["Only a body paragraph."]},
    "closing_only": {"paragraphs": ["Body.", "Best regards,"]},
    "empty": {},
}

RESUME_CASES = {"full": (FULL, None), "full_margin": (FULL, 1.0), "sparse": (SPARSE, None), "nothing": (NOTHING, None),
                "long": (LONG, 0.5)}
LETTER_CASES = {
    "company_role": ("full", FULL, "Globex", "Senior Planner", None),
    "company": ("bare", FULL, "Globex", "", 0.9),
    "neither": ("full", FULL, "", "", None),
    "role_only": ("closing_only", FULL, "", "Planner", None),
    "no_name": ("full", SPARSE | {"name": ""}, "Globex", "", None),
    "no_contact": ("bare", SPARSE, "Initech", "Clerk", None),
    "empty_letter": ("empty", NOTHING, "", "", None),
}


def _border(p) -> bool:
    return p._p.pPr is not None and p._p.pPr.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pBdr") is not None


def _pt(value) -> float | None:
    return None if value is None else round(value.pt, 2)


def docx_view(data: bytes) -> dict:
    """The structure of a Word file: margins, the base style, and each paragraph's text, runs and spacing."""
    doc = Document(io.BytesIO(data))
    normal = doc.styles["Normal"]
    props = doc.core_properties
    return {
        "margins": [[round(m.inches, 3) for m in (s.left_margin, s.right_margin, s.top_margin, s.bottom_margin)]
                    for s in doc.sections],
        "normal": [normal.font.name, _pt(normal.font.size), _pt(normal.paragraph_format.space_after)],
        "meta": [props.author, props.last_modified_by, props.comments],
        "tables": len(doc.tables),
        "paragraphs": [{
            "style": p.style.name, "align": None if p.alignment is None else int(p.alignment), "text": p.text,
            "runs": [[r.text, r.bold, r.italic, _pt(r.font.size)] for r in p.runs],
            "before": _pt(p.paragraph_format.space_before), "after": _pt(p.paragraph_format.space_after),
            "keep": p.paragraph_format.keep_with_next, "rule": _border(p),
        } for p in doc.paragraphs],
    }


def pdf_view(data: bytes) -> dict:
    reader = PdfReader(io.BytesIO(data))
    return {"pages": [page.extract_text() for page in reader.pages]}


@functools.cache
def render_all() -> dict:
    out: dict = {}
    for key, (resume, margin) in RESUME_CASES.items():
        out[f"resume:{key}"] = {"docx": docx_view(docs_render.resume_docx(resume, margin_inches=margin)),
                                "pdf": pdf_view(docs_render.resume_pdf(resume, margin_inches=margin))}
    for key, (letter, resume, company, role, margin) in LETTER_CASES.items():
        args = (LETTERS[letter], resume, company, DAY)
        kw = {"margin_inches": margin, "role_title": role}
        out[f"letter:{key}"] = {"docx": docx_view(docs_render.letter_docx(*args, **kw)),
                                "pdf": pdf_view(docs_render.letter_pdf(*args, **kw))}
    return out


_GOLDEN = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


@pytest.mark.parametrize("case", sorted(_GOLDEN))
def test_the_document_renders_as_it_always_has(case):
    assert render_all()[case] == _GOLDEN[case]


def test_every_case_has_a_golden_entry():
    assert sorted(render_all()) == sorted(_GOLDEN)


if __name__ == "__main__" and "--write" in sys.argv:
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(render_all(), indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print("wrote", GOLDEN)
