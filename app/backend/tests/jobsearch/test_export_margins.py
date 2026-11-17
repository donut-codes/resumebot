"""The Word export uses the owner's page margins. The margin is one number in the writing rules (`page_margin_inches`,
under the resume rules, Global): read back from the Word files, the export's own margins when it is empty, what is
refused, and what an older version of the writing rules does without it. Made-up data only."""

import io

import pytest
from docx import Document
from pydantic import ValidationError

from app import db
from app.jobsearch.models import ProfileVersion
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.documents import Writing
from app.jobsearch.writing import docs_render
from tests.builders import LETTER, written_packet
from tests.support import OWNER

REFUSED = "page margins must be between 0.25 and 1.5 inches"
#: What the export has when no margin is set: left and right, top and bottom, as they have always been.
OWN = (0.7, 0.7, 0.6, 0.6)
PERSON = {"name": "Pat Example", "contact": ["pat@example.test"], "roles": []}
def margins(data: bytes) -> list[tuple[float, ...]]:
    """(left, right, top, bottom), in inches, of each section of a Word file."""
    return [tuple(round(side.inches, 3) for side in (s.left_margin, s.right_margin, s.top_margin, s.bottom_margin))
            for s in Document(io.BytesIO(data)).sections]


async def _store_raw(data: dict, version: int = 1) -> None:
    """A version of the writing rules as an older install saved it: put in as it is, not read through the model."""
    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner=OWNER, kind="writing", version=version, data=data, saved_by="test", why=""))
        await session.commit()


async def _downloads(client, packet_id: str) -> tuple[bytes, bytes]:
    """The packet's resume and cover letter as Word files."""
    resume = await client.get(f"/api/v1/packets/{packet_id}/resume.docx")
    letter = await client.get(f"/api/v1/packets/{packet_id}/letter.docx")
    assert resume.status_code == 200 and letter.status_code == 200
    return resume.content, letter.content


# --- the margin reaches the Word file ---------------------------------------------------------------------------------


@pytest.mark.parametrize("inches", [0.25, 0.5, 1, 1.5])
def test_the_margin_is_all_four_sides_of_the_resume_and_of_the_letter(inches):
    assert margins(docs_render.resume_docx(PERSON, margin_inches=inches)) == [(inches,) * 4]
    assert margins(docs_render.letter_docx(LETTER, PERSON, "Globex", margin_inches=inches)) == [(inches,) * 4]


def test_with_no_margin_the_export_keeps_its_own_margins_exactly():
    assert margins(docs_render.resume_docx(PERSON)) == [OWN]
    assert margins(docs_render.letter_docx(LETTER, PERSON, "Globex")) == [OWN]
    assert margins(docs_render.resume_docx(PERSON, margin_inches=None)) == [OWN]
    assert margins(docs_render.letter_docx(LETTER, PERSON, "Globex", margin_inches=None)) == [OWN]


async def test_the_downloads_of_a_packet_use_the_margin_in_the_writing_rules_in_force(client):
    packet_id = await written_packet(client)
    # No margin set yet: the export's own.
    assert [margins(d) for d in await _downloads(client, packet_id)] == [[OWN], [OWN]]
    # Set in the writing rules, as the page saves it: the next download has it, on both documents.
    saved = (await client.get("/api/v1/documents")).json()["writing"]

    async def set_margin(inches):
        put = await client.put("/api/v1/profile/writing", json={"data": {**saved, "page_margin_inches": inches}})
        assert put.status_code == 200

    await set_margin(0.5)
    assert [margins(d) for d in await _downloads(client, packet_id)] == [[(0.5,) * 4], [(0.5,) * 4]]
    # Changed again: the packet, written before, follows the rules as they are now.
    await set_margin(1.25)
    assert [margins(d) for d in await _downloads(client, packet_id)] == [[(1.25,) * 4], [(1.25,) * 4]]
    # Emptied: back to the export's own.
    await set_margin(None)
    assert [margins(d) for d in await _downloads(client, packet_id)] == [[OWN], [OWN]]


# --- what is accepted and what is refused -----------------------------------------------------------------------------


def test_a_new_install_has_no_margin_set():
    assert Writing().page_margin_inches is None
    assert Writing(page_margin_inches=None).page_margin_inches is None


@pytest.mark.parametrize("inches", [0.25, 0.3, 0.5, 0.7, 1, 1.5])
def test_a_margin_from_a_quarter_inch_to_an_inch_and_a_half_is_accepted(inches):
    assert Writing(page_margin_inches=inches).page_margin_inches == inches


@pytest.mark.parametrize("inches", [0, -0.5, 0.1, 0.24, 1.51, 2, 8, float("nan"), float("inf")])
def test_a_margin_outside_that_is_refused_in_plain_words(inches):
    with pytest.raises(ValidationError, match=REFUSED):
        Writing(page_margin_inches=inches)


async def test_a_margin_outside_the_range_is_refused_by_the_save_and_nothing_is_stored(client):
    for inches in (0.2, 1.6, 3, -1):
        refused = await client.put("/api/v1/profile/writing", json={"data": {"page_margin_inches": inches}})
        assert refused.status_code == 422
        assert refused.json()["detail"] == f"the writing don't fit: page_margin_inches: {REFUSED}"
    assert (await client.get("/api/v1/documents")).json()["versions"]["writing"] == 0


async def test_the_margin_saves_and_is_read_back_and_can_be_emptied(client):
    saved = await client.put("/api/v1/profile/writing", json={"data": {"page_margin_inches": 0.5}})
    assert saved.status_code == 200 and saved.json()["version"] == 1
    assert (await client.get("/api/v1/documents")).json()["writing"]["page_margin_inches"] == 0.5
    emptied = await client.put("/api/v1/profile/writing", json={"data": {"page_margin_inches": None}})
    assert emptied.json()["version"] == 2
    assert (await client.get("/api/v1/documents")).json()["writing"]["page_margin_inches"] is None
    versions = (await client.get("/api/v1/profile/writing/history")).json()["versions"]
    assert [v["changed"] for v in versions] == [["cleared page margin inches"], ["set page margin inches"]]


async def test_a_margin_saved_with_the_rules_leaves_the_rules_as_they_were(client):
    rules = {"resume_global": [{"id": "one-page", "says": "One page unless the posting asks for more."}]}
    await client.put("/api/v1/profile/writing", json={"data": rules})
    await client.put("/api/v1/profile/writing", json={"data": {**rules, "page_margin_inches": 0.5}})
    writing = (await client.get("/api/v1/documents")).json()["writing"]
    assert writing["resume_global"] == rules["resume_global"] and writing["page_margin_inches"] == 0.5


# --- a version saved before the field ---------------------------------------------------------------------------------


async def test_a_version_saved_before_the_margin_loads_without_one(client):
    await _store_raw({"resume_global": [{"id": "one-page", "says": "One page."}], "banned": []}, version=1)
    docs = (await client.get("/api/v1/documents")).json()
    assert docs["writing"]["page_margin_inches"] is None
    assert [r["says"] for r in docs["writing"]["resume_global"]] == ["One page."]
    async with db.session_factory()() as session:
        read = await profile_store.version(session, OWNER, "writing", 1)
    assert read["data"]["resume_global"] == [{"id": "one-page", "says": "One page."}]


async def test_a_packet_downloads_with_the_default_margins_under_a_version_saved_before_the_margin(client):
    packet_id = await written_packet(client)
    await _store_raw({"voice_rules": [{"id": "plain", "says": "Plain."}]}, version=2)
    assert [margins(d) for d in await _downloads(client, packet_id)] == [[OWN], [OWN]]
