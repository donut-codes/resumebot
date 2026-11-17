"""No character limit on what the owner writes on a role's page (the owner, 2026-10-14: a length limit on what they type
is "a terrible feature"): the reason for a corrected requirement, the note on a hand move and what they ask the writer
to change. Each is kept whole, shown whole (the role's detail, its History) and, for the feedback, handed to the
composer's seat whole. Synthetic data only."""

import io

import openpyxl
from sqlalchemy import select

from app import db
from app.jobsearch.imports import tracker
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.research import analysis
from app.jobsearch.writing import seats
from app.models.work import TrailEvent
from tests.builders import analysis_setup, export_workbook, findings, packet_setup
from tests.support import OWNER

LONG, BIG = 20_000, 120_000


def _words(tag: str, n: int = LONG) -> str:
    """About `n` characters of made-up plain sentences, each different, so a cut anywhere shows."""
    out, i = [], 0
    while sum(len(s) + 1 for s in out) < n:
        i += 1
        out.append(f"{tag} sentence {i} says one more thing the owner wants.")
    return " ".join(out)[: n - 1] + "!"  # exactly n characters, and no space at either end for a save to trim


async def _event(kind: str) -> TrailEvent:
    async with db.session_factory()() as session:
        return (await session.scalars(select(TrailEvent).where(TrailEvent.event == kind))).one()


# --- a requirement's reason, a hand move's note ---


async def test_a_requirements_reason_is_kept_whole_and_folded_to_one_line_and_the_role_returns_it(client):  # 18
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await analysis.submit(session, OWNER, findings(ids["Fabrikam"]))
        await session.commit()
    said = _words("reason") + "  \n  and  more"
    folded = " ".join(said.split())
    assert len(folded) > LONG
    corrected = await client.post(f"/api/v1/roles/{ids['Fabrikam']}/requirements",
                                  json={"index": 1, "class": "P", "why": said})
    assert corrected.status_code == 200, corrected.text
    async with db.session_factory()() as session:
        stored = (await session.get(Opportunity, ids["Fabrikam"])).analysis["required"][1]["owner_why"]
    assert stored == folded, "whitespace is folded, nothing is cut"
    detail = (await client.get(f"/api/v1/roles/{ids['Fabrikam']}")).json()
    assert detail["requirements"][1]["owner_why"] == folded
    assert (await _event("requirement_corrected")).detail == said, "the History's Show more holds the reason whole"


async def test_a_hand_move_note_is_kept_whole_in_the_roles_notes_and_the_trails_detail(client):  # 19
    role_id = await packet_setup()
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).notes = "an earlier note"
        await session.commit()
    note = _words("note")
    moved = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "first_impression", "note": note})
    assert moved.status_code == 200, moved.text
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
    assert role.status == "first_impression"
    assert role.notes == f"an earlier note; {note}" and role.notes.endswith(note)
    assert (await _event("role_moved")).detail == note


async def test_a_role_whose_notes_pass_a_spreadsheet_cell_still_exports_and_keeps_its_notes_whole(js):  # 19b
    """Pins today's behaviour until the owner answers what the export should do with a cut note: the library
    cuts a cell at 32,767 characters without a word, the file opens, and the database keeps the whole note."""
    role_id = await packet_setup()
    notes = _words("note", 40_000)
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).notes = notes
        await session.commit()
    async with db.session_factory()() as session:
        data = await export_workbook(session, OWNER)
    sheet = openpyxl.load_workbook(io.BytesIO(data))[tracker.SHEET]
    cell = sheet.cell(row=2, column=tracker.COLUMNS.index("notes") + 1).value
    assert cell == notes[:32_767], "the library's cut: the first 32,767 characters"
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, role_id)).notes == notes


# --- the packet feedback and the composer's seat ---


async def test_packet_feedback_is_kept_whole_and_the_composers_seat_is_handed_it_whole(client, knocks):  # 20
    role_id = await packet_setup()
    feedback = _words("feedback", BIG)
    asked = await client.post(f"/api/v1/roles/{role_id}/packet", json={"feedback": feedback})
    assert asked.status_code == 200, asked.text
    assert asked.json()["feedback"] == feedback
    assert knocks[-1][1] == "packet_asked", "the writer is woken"
    async with db.session_factory()() as session:
        packet = (await session.scalars(select(Packet).where(Packet.role_id == role_id))).one()
        taken = (await seats.take(session, OWNER, "s-1"))["seat"]
    assert packet.feedback == feedback and len(packet.feedback) == BIG
    assert taken is not None and feedback in taken["system"], "the composer's seat is handed the ask whole"
    assert (await _event("packet_asked")).detail == feedback, "the History's Show more holds the ask whole"
