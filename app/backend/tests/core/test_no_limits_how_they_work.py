"""No character limit on what the owner writes on How they work (the owner's rule, 2026-10-14). The reason for a process
edit or a rollback, a file's text and the reason for a file's version are kept whole. The one refusal that remains is
the room an agent has: a file that, with the file an agent is sent beside it, would pass what one reply leaves for the
two is refused, in words, with both sizes, and nothing is saved. Synthetic data only."""

import pytest
from process_engine import versions
from process_engine.models import ProcVersion
from sqlalchemy import select

from app import db
from app.agents import process_steps, shipped_rule
from app.jobsearch.models import InstructionFile
from app.jobsearch.owner_text import ROOM_CHARS, too_large_words
from app.jobsearch.work import instruction_files
from tests.support import OWNER

ROUND = "jobsearch_round"
#: A writer-lane step's pair: the fixer (the packet's Fix and Final_fix seats) and the template its reply follows.
WRITER, PACKET = "jobsearch.fixer", "jobsearch.fix"
SCOUT, SCOUT_REPORT = "jobsearch.scout", "jobsearch.scout.report"
SCORING = "jobsearch.scoring"
PLAN, PLAN_RESPONSE = "projects.plan.write", "projects.plan.response"


def _words(tag: str, n: int) -> str:
    """About `n` characters of made-up plain sentences, each different, so a cut anywhere shows; exactly `n` characters
    with no space at either end, so a save's trimming leaves it as it is."""
    out, i = [], 0
    while sum(len(s) + 1 for s in out) < n:
        i += 1
        out.append(f"{tag} sentence {i} says one more thing the owner wants.")
    return " ".join(out)[: n - 1] + "!"


def _messy(text: str) -> str:
    """`text` with runs of spaces and line breaks where its single spaces were, and spaces around it: a one-line why
    folds them back (`" ".join(...split())`), so it reads as `text` again."""
    return "  " + text.replace(" says ", "  says\n", 5).replace(" one more ", " \t one more\n\n", 5) + "\n "


def process_url(key: str, tail: str) -> str:
    return f"/api/v1/harness/processes/{key}/{tail}"


def file_url(kind: str, name: str, tail: str = "") -> str:
    return f"/api/v1/harness/files/{kind}/{name}{tail}"


def renamed(xml: str, was: str = "Read the gates", now: str = "Read the owner's gates") -> str:
    assert f'name="{was}"' in xml
    return xml.replace(f'name="{was}"', f'name="{now}"')


@pytest.fixture
async def seeded(client):
    """The first start: each shipped process file is kept as version 1."""
    async with db.session_factory()() as session:
        await shipped_rule.settle(session)
        await session.commit()
    return client


async def save_file(client, kind: str, name: str, text: str, why: str = "a test edit"):
    return await client.put(file_url(kind, name), json={"text": text, "why": why})


async def saved(kind: str, name: str) -> str:
    """The file as an agent would be sent it now: the newest saved version, else the file shipped."""
    async with db.session_factory()() as session:
        return await instruction_files.read(session, OWNER, kind, name)


def not_saved(size: int, beside: int | None) -> str:
    """Sentence 3 (a file with the file sent beside it) or 3b (a file no step pairs), as the spec words them."""
    if beside is None:
        return (f"This file is {size:,} characters. An agent can be sent at most {ROOM_CHARS:,} at once, so it was "
                "not saved. Nothing was cut. Shorten it.")
    return (f"This file is {size:,} characters, and the file sent with it is {beside:,}. An agent can be sent at most "
            f"{ROOM_CHARS:,} of the two at once, so it was not saved. Nothing was cut. Shorten one of them.")


async def versions_of(client, kind: str, name: str) -> list[int]:
    history = (await client.get(file_url(kind, name))).json()["history"]
    return [v["version"] for v in history["versions"]]


async def nothing_written() -> bool:
    async with db.session_factory()() as session:
        return (await session.scalars(select(InstructionFile))).all() == []


# --- a process: the reason for an edit and for a rollback (test 8) --------------------------------------------------


async def test_a_process_edit_and_a_rollback_keep_a_5000_character_why_whole(seeded):
    xml = versions.shipped_xml(ROUND)
    why, back = _words("edit", 5_000), _words("rollback", 5_000)
    edited = await seeded.post(process_url(ROUND, "edits"), json={"xml": renamed(xml), "why": _messy(why)})
    assert edited.status_code == 200, edited.text
    assert edited.json()["versions"][0]["why"] == why, "the reason is kept whole, its spaces folded"

    rolled = await seeded.post(process_url(ROUND, "rollback"), json={"to": 1, "why": _messy(back)})
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["versions"][0]["why"] == back

    listed = (await seeded.get(process_url(ROUND, "versions"))).json()["versions"]
    assert [v["why"] for v in listed[:2]] == [back, why], "the history returns both whole"
    async with db.session_factory()() as session:
        rows = (await session.scalars(
            select(ProcVersion.wording).where(ProcVersion.process_key == ROUND).order_by(ProcVersion.number)
        )).all()
    assert list(rows[-2:]) == [why, back], "and so does the version's own wording"


async def test_a_rollback_with_no_reason_still_says_which_version_it_went_back_to(seeded):
    xml = versions.shipped_xml(ROUND)
    edited = await seeded.post(process_url(ROUND, "edits"), json={"xml": renamed(xml), "why": "Say it"})
    assert edited.status_code == 200
    rolled = await seeded.post(process_url(ROUND, "rollback"), json={"to": 1})
    assert rolled.json()["versions"][0]["why"] == "Went back to version 1."


# --- an instruction file: text and reason kept whole (test 9) -------------------------------------------------------


async def test_an_instruction_file_of_100000_characters_and_a_5000_character_why_are_kept_whole(client):
    text, why = _words("file", 100_000), _words("because", 5_000)
    res = await save_file(client, "instructions", SCOUT, text, _messy(why))
    assert res.status_code == 200, res.text
    body = (await client.get(file_url("instructions", SCOUT))).json()
    assert body["file"]["text"] == text and body["file"]["chars"] == 100_000
    assert body["history"]["versions"][0]["why"] == why
    async with db.session_factory()() as session:
        row = await session.scalar(select(InstructionFile).where(
            InstructionFile.kind == "instructions", InstructionFile.name == SCOUT, InstructionFile.version == 2,
        ))
    assert row is not None and row.why == why and row.text == text, "the database holds both whole"


# --- the room an agent has for a file and the file sent beside it (tests 10, 10b, 10c) ------------------------------


async def test_a_file_is_refused_over_the_room_left_beside_its_template_and_saves_at_the_room(client):
    template = len(await saved("templates", PACKET))
    over = await save_file(client, "instructions", WRITER, "x" * (ROOM_CHARS - template + 1))
    assert over.status_code == 413
    assert over.json()["detail"] == not_saved(ROOM_CHARS - template + 1, template)
    assert over.json()["detail"] == too_large_words(
        "This file", ROOM_CHARS - template + 1, beside=("the file sent with it", template),
        result="it was not saved. Nothing was cut.", fix="Shorten one of them.",
    )
    assert await versions_of(client, "instructions", WRITER) == [1], "no version was saved"
    assert await nothing_written(), "nothing was written, not even the shipped text as version 1"

    # The room is measured on the text as it is kept: line breaks as \n, outer space trimmed.
    fits = await save_file(client, "instructions", WRITER, "\r\n  " + "x" * (ROOM_CHARS - template) + "  \r\n")
    assert fits.status_code == 200, fits.text
    assert await versions_of(client, "instructions", WRITER) == [2, 1]


async def test_the_template_is_checked_against_the_instructions_the_same_way_in_the_other_direction(client):
    assert (await save_file(client, "instructions", WRITER, "x" * 100_000)).status_code == 200
    over = await save_file(client, "templates", PACKET, "y" * (ROOM_CHARS - 100_000 + 1))
    assert over.status_code == 413
    assert over.json()["detail"] == not_saved(ROOM_CHARS - 100_000 + 1, 100_000)
    assert await versions_of(client, "templates", PACKET) == [1]
    assert (await save_file(client, "templates", PACKET, "y" * (ROOM_CHARS - 100_000))).status_code == 200


async def test_a_file_no_step_names_is_checked_alone(client):
    assert process_steps.paired_names("instructions", SCORING) == []
    over = await save_file(client, "instructions", SCORING, "x" * (ROOM_CHARS + 1))
    assert over.status_code == 413
    assert over.json()["detail"] == not_saved(ROOM_CHARS + 1, None)
    assert "the file sent with it" not in over.json()["detail"] and "of the two" not in over.json()["detail"]
    assert await nothing_written()
    assert (await save_file(client, "instructions", SCORING, "x" * ROOM_CHARS)).status_code == 200


async def test_the_files_of_a_site_step_are_paired_too(client):
    plan = len(await saved("instructions", PLAN))
    over = await save_file(client, "templates", PLAN_RESPONSE, "y" * (ROOM_CHARS - plan + 1))
    assert over.status_code == 413
    assert over.json()["detail"] == not_saved(ROOM_CHARS - plan + 1, plan)
    assert (await save_file(client, "templates", PLAN_RESPONSE, "y" * (ROOM_CHARS - plan))).status_code == 200


def test_each_step_pairs_its_instructions_with_its_template_and_the_reverse():
    assert process_steps.paired_names("instructions", WRITER) == [PACKET]
    assert process_steps.paired_names("templates", PACKET) == [WRITER]
    assert process_steps.paired_names("instructions", SCOUT) == [SCOUT_REPORT]
    assert process_steps.paired_names("templates", SCOUT_REPORT) == [SCOUT]
    assert process_steps.paired_names("instructions", PLAN) == [PLAN_RESPONSE]
    assert process_steps.paired_names("templates", PLAN_RESPONSE) == [PLAN]
    # a file shared by two steps (the analyst's, in two processes) has one partner, named once
    assert process_steps.paired_names("instructions", "jobsearch.analyst") == ["jobsearch.analysis"]
    # a file is found by the name the process file stores it under, too
    assert process_steps.paired_names("instructions", "instructions/jobsearch.fixer.instructions.md") == [PACKET]
    assert process_steps.paired_names("instructions", "nothing") == []


async def test_restoring_an_earlier_version_is_checked_against_the_paired_file(client):
    assert (await save_file(client, "instructions", WRITER, "x" * 100_000, "large")).status_code == 200  # version 2
    assert (await save_file(client, "instructions", WRITER, "mine, short", "short")).status_code == 200  # version 3
    # the template now takes so much that version 2 no longer fits beside it
    template = ROOM_CHARS - 50_000
    assert (await save_file(client, "templates", PACKET, "y" * template, "large template")).status_code == 200
    refused = await client.post(file_url("instructions", WRITER, "/restore"), json={"version": 2})
    assert refused.status_code == 413
    assert refused.json()["detail"] == not_saved(100_000, template)
    assert await versions_of(client, "instructions", WRITER) == [3, 2, 1], "nothing was saved"
    assert (await saved("instructions", WRITER)) == "mine, short"

    # a restore that fits is still a restore
    assert (await save_file(client, "templates", PACKET, "ok", "small again")).status_code == 200
    restored = await client.post(file_url("instructions", WRITER, "/restore"), json={"version": 2})
    assert restored.status_code == 200, restored.text
    assert restored.json()["file"]["text"] == "x" * 100_000
