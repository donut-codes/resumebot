"""One role whole: its facts, its history with the reasoning behind each event, its packets, and a packet as Word
documents. Made-up data only."""

import io
import uuid
from datetime import UTC, date, datetime

from docx import Document
from pypdf import PdfReader

from app import db
from app.jobsearch.models import Opportunity
from app.jobsearch.writing import docs_render, packets
from app.jobsearch.writing.routes import _filename, owner_file_name
from tests.builders import RESEARCH, save_documents, write_packet
from tests.support import OWNER, POSTING_WORDS, load_profile


async def _role(**extra):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        role = Opportunity(owner=OWNER, slug="globex", company="Globex", title="Programs Lead", fit=6, origin="scout",
                           match_key="globex|programs lead", found=date.today(), board="Boardtwo",
                           url="https://jobs.example.test/1",
                           **{"status": "second_impression", "analysis": RESEARCH,
                              "fit_evidence": "base 4; promotion +1; healthcare +1 = 6", **extra})
        session.add(role)
        await session.commit()
        return role.id


async def test_a_role_page_has_its_facts_history_and_packets(client):
    role_id = await _role()
    await client.post(f"/api/v1/roles/{role_id}/packet", json={})
    await write_packet()
    await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "applied", "note": "sent it"})
    body = (await client.get(f"/api/v1/roles/{role_id}")).json()
    assert body["role"]["title"] == "Programs Lead" and body["role"]["status"] == "applied"
    assert body["role"]["packet"]["status"] == "ready"
    kinds = [e["kind"] for e in body["timeline"]]
    assert kinds[-2:] == ["arrived", "scored"] or kinds[-2:] == ["scored", "arrived"]
    assert {"packet_asked", "packet_written", "packet_ready", "role_moved"} <= set(kinds)
    scored = next(e for e in body["timeline"] if e["kind"] == "scored")
    assert scored["detail"] == "base 4; promotion +1; healthcare +1 = 6"
    assert next(e for e in body["timeline"] if e["kind"] == "packet_ready")["who"] == "Writer"
    written = next(e for e in body["timeline"] if e["kind"] == "packet_written")
    assert written["detail"] == "The problem it answers: They need programs that run."
    assert [p["version"] for p in body["packets"]] == [1]
    assert (await client.get("/api/v1/roles/00000000-0000-0000-0000-000000000000")).status_code == 404


READ = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)
#: The scout's line for a role it could not read, and the note the app adds to every role whose place it could not tell.
NO_TEXT = "Text missing (sign-in job site); scored from title only."
NO_PLACE = "place not stated: check the work location before applying"


async def _scored_detail(client, role_id):
    body = (await client.get(f"/api/v1/roles/{role_id}")).json()
    return next(e for e in body["timeline"] if e["kind"] == "scored")["detail"]


async def test_the_scout_saying_the_text_was_missing_is_shown_while_the_text_is_missing(client):
    role_id = await _role(fit_evidence=f"{NO_TEXT} | {NO_PLACE}")
    assert await _scored_detail(client, role_id) == f"{NO_TEXT} | {NO_PLACE}"


async def test_the_scout_saying_the_text_was_missing_is_not_shown_once_the_text_is_read(client):
    role_id = await _role(fit_evidence=f"{NO_TEXT} | {NO_PLACE}", description=POSTING_WORDS, description_at=READ)
    assert await _scored_detail(client, role_id) == NO_PLACE


async def test_a_scout_line_that_says_nothing_of_missing_text_is_kept_when_the_text_is_read(client):
    role_id = await _role(description=POSTING_WORDS, description_at=READ)
    assert await _scored_detail(client, role_id) == "base 4; promotion +1; healthcare +1 = 6"


async def test_a_screened_out_role_says_which_gate(client):
    role_id = await _role(status="gated_out", screened_out_reason="pay under the floor")
    body = (await client.get(f"/api/v1/roles/{role_id}")).json()
    assert any(e["kind"] == "screened" and e["detail"] == "pay under the floor" for e in body["timeline"])


async def test_a_packet_downloads_as_word_files_and_a_packet_with_no_letter_has_none(client):
    role_id = await _role()
    asked = (await client.post(f"/api/v1/roles/{role_id}/packet", json={})).json()
    await write_packet()
    resume = await client.get(f"/api/v1/packets/{asked['id']}/resume.docx")
    assert resume.status_code == 200 and "Pat_Example_Resume.docx" in resume.headers["content-disposition"]
    text = "\n".join(p.text for p in Document(io.BytesIO(resume.content)).paragraphs)
    assert "Pat Example" in text and "Programs Leader" in text and "Northwind Labs, Oakfield, ST" in text
    assert "Senior Manager | 2022 to 2026" in text and "Led the risk working group" in text
    letter = await client.get(f"/api/v1/packets/{asked['id']}/letter.docx")
    assert letter.status_code == 200 and "Pat_Example_CoverLetter.docx" in letter.headers["content-disposition"]
    words = "\n".join(p.text for p in Document(io.BytesIO(letter.content)).paragraphs)
    assert "I would like to help you run programs." in words
    assert (await client.get("/api/v1/packets/00000000-0000-0000-0000-000000000000/resume.docx")).status_code == 404


def _pdf_text(data: bytes) -> str:
    """The text of a PDF as one line of single-spaced words (a wrapped line is the same words)."""
    return " ".join(" ".join(page.extract_text().split()) for page in PdfReader(io.BytesIO(data)).pages)


def _word_lines(data: bytes) -> list[str]:
    return [" ".join(p.text.split()) for p in Document(io.BytesIO(data)).paragraphs if p.text.strip()]


async def test_a_packet_downloads_as_pdf_files_with_the_words_of_the_word_files(client):
    role_id = await _role()
    asked = (await client.post(f"/api/v1/roles/{role_id}/packet", json={})).json()
    await write_packet()
    for kind, name in (("resume", "Pat_Example_Resume.pdf"), ("letter", "Pat_Example_CoverLetter.pdf")):
        pdf = await client.get(f"/api/v1/packets/{asked['id']}/{kind}.pdf")
        word = await client.get(f"/api/v1/packets/{asked['id']}/{kind}.docx")
        assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
        assert pdf.headers["content-disposition"] == f'attachment; filename="{name}"'
        assert pdf.content.startswith(b"%PDF-")
        text = _pdf_text(pdf.content)
        lines = _word_lines(word.content)
        assert len(lines) > 3
        assert all(line in text for line in lines), [line for line in lines if line not in text]
    assert "Senior Manager | 2022 to 2026" in _pdf_text(
        (await client.get(f"/api/v1/packets/{asked['id']}/resume.pdf")).content)
    assert (await client.get("/api/v1/packets/00000000-0000-0000-0000-000000000000/resume.pdf")).status_code == 404
    assert (await client.get("/api/v1/packets/00000000-0000-0000-0000-000000000000/letter.pdf")).status_code == 404


async def test_an_old_packet_with_no_letter_has_no_letter_pdf(client):
    role_id = await _role()
    asked = (await client.post(f"/api/v1/roles/{role_id}/packet", json={})).json()
    await write_packet()
    async with db.session_factory()() as session:
        (await session.get(packets.Packet, uuid.UUID(asked["id"]))).letter = {}  # written before every packet had one
        await session.commit()
    assert (await client.get(f"/api/v1/packets/{asked['id']}/resume.pdf")).status_code == 200
    assert (await client.get(f"/api/v1/packets/{asked['id']}/letter.pdf")).status_code == 404


def test_the_pdf_keeps_curly_marks_and_accents_and_never_fails_on_a_letter_the_font_lacks():
    resume = {"name": "Zoë Łukasz", "summary": "It’s a “plan” – and more… 中",
              "competencies": [{"area": "Area", "items": ["One", "Two"]}], "roles": []}
    text = _pdf_text(docs_render.resume_pdf(resume))
    assert "Zoë Łukasz" in text and "It’s a “plan” – and more…" in text and "One · Two" in text
    assert _pdf_text(docs_render.letter_pdf({"paragraphs": ["Hello."]}, resume, "Globex")).count("Hello.") == 1


def test_the_word_file_carries_no_one_s_name_in_its_metadata():
    data = docs_render.resume_docx({"name": "Pat Example", "roles": []})
    props = Document(io.BytesIO(data)).core_properties
    assert props.author == "" and props.last_modified_by == ""


def test_the_file_name_comes_from_the_owners_name():
    assert owner_file_name("Mary Ann O'Neil") == "Mary_Ann_ONeil"
    assert owner_file_name("Zoë  Smíth-Jones, Jr.") == "Zoe_Smith_Jones_Jr"
    assert owner_file_name("") == "" and owner_file_name("日本") == ""
    packet = type("P", (), {"resume": {"name": "Mary Ann O'Neil"}, "version": 3})()
    assert _filename(packet, None, "Resume") == "Mary_Ann_ONeil_Resume.docx"
    assert _filename(packet, None, "CoverLetter") == "Mary_Ann_ONeil_CoverLetter.docx"
    assert _filename(packet, None, "Resume", "pdf") == "Mary_Ann_ONeil_Resume.pdf"


def test_with_no_name_the_file_keeps_its_older_name():
    packet = type("P", (), {"resume": {"name": ""}, "version": 3})()
    role = type("R", (), {"company": "Globex Inc."})()
    assert _filename(packet, role, "Resume") == "resume_GlobexInc_Resume_v3.docx"

