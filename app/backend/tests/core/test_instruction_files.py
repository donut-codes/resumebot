"""The agents' instruction and template files, editable and versioned (docs/PRD.md 16.1): edit and save, the next
hand-out carrying the saved text, the fall back to the file on disk, history and restore, a release that ships a
different file, and the hand-out naming the version it was sent. Synthetic data only."""

import shutil

import pytest
import yaml
from sqlalchemy import select

from app import db
from app.config import settings as base_settings
from app.harness import instructions as files
from app.jobsearch.doors import tools
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import InstructionFile, SearchRun
from app.jobsearch.work import instruction_files
from app.models.handout import TaskHandout
from tests.support import OWNER, load_profile, reported

SCOUT_FILE = "jobsearch.scout"
REPORT_TEMPLATE = "jobsearch.scout.report"


def harness_url(path: str = "") -> str:
    return f"/api/v1/harness{path}"


def file_url(kind: str, name: str, tail: str = "") -> str:
    return harness_url(f"/files/{kind}/{name}{tail}")


async def step_of(client, step_id: str) -> dict:
    """A step of any process on the How they work page."""
    page = (await client.get(harness_url())).json()
    return next(s for p in page["processes"] for s in p["steps"] if s["id"] == step_id)


async def example_of(client) -> dict:
    return (await client.get(harness_url("/steps/Scout_searches/example"))).json()["example"]


async def save(client, kind, name, text, why="a test edit"):
    return await client.put(file_url(kind, name), json={"text": text, "why": why})


def shipped(kind: str, name: str) -> str:
    return files.read(kind, name)


async def start_search(focus="banks"):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER, started_by="manual", focus=focus)
        await session.commit()
        return started.id


async def end_search(run_id):
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        await run.stage_report(session, row, [reported("Fabrikam", "Head of Delivery", fit=6)], done=True)
        await session.commit()


# --- edit and save, and the versions ------------------------------------------------------------------------------


async def test_a_file_nobody_changed_is_version_one_as_shipped(client):
    body = (await client.get(file_url("instructions", SCOUT_FILE))).json()
    assert body["file"]["version"] == 1 and body["file"]["edited"] is False and body["file"]["busy"] is None
    assert body["file"]["text"] == shipped("instructions", SCOUT_FILE)
    [only] = body["history"]["versions"]
    assert only["version"] == 1 and only["shipped"] is True and only["current"] is True and only["at"] is None
    async with db.session_factory()() as session:
        assert (await session.scalars(select(InstructionFile))).all() == [], "nothing is written until an edit is saved"
        assert await instruction_files.read(session, OWNER, "instructions", SCOUT_FILE) == body["file"]["text"]


async def test_saving_an_edit_makes_version_two_and_keeps_the_shipped_text_as_version_one(client):
    edited = shipped("instructions", SCOUT_FILE) + "\n\nAlways say which board you are on."
    res = await save(client, "instructions", SCOUT_FILE, edited, "  say the board \n now ")
    assert res.status_code == 200
    body = res.json()
    assert body["file"]["version"] == 2 and body["file"]["edited"] is True and body["file"]["text"] == edited
    assert [(v["version"], v["by"], v["current"]) for v in body["history"]["versions"]] == [
        (2, "You", True), (1, "Shipped with the app", False),
    ]
    assert body["history"]["versions"][0]["why"] == "say the board now", "the why is one line"
    assert body["history"]["versions"][0]["at"] and body["history"]["versions"][1]["at"] is None
    # How they work shows the edited text, version two, in the step that sends the file
    step = await step_of(client, "Scout_searches")
    assert step["sent"]["instruction"]["text"] == edited and step["sent"]["instruction"]["version"] == 2
    assert step["sent"]["instruction"]["edited"] is True and step["sent"]["template"]["version"] == 1
    # the file on disk is untouched: it is still version 1
    assert shipped("instructions", SCOUT_FILE) != edited


async def test_a_save_is_refused_when_it_changes_nothing_has_no_why_is_empty_or_adds_a_placeholder(client):
    text = shipped("instructions", SCOUT_FILE)
    assert (await save(client, "instructions", SCOUT_FILE, text)).status_code == 409, "nothing changed"
    assert (await save(client, "instructions", SCOUT_FILE, text + " more", "")).status_code == 409, "no why"
    assert (await save(client, "instructions", SCOUT_FILE, "   ")).status_code == 409
    refused = await save(client, "instructions", SCOUT_FILE, text + " {{made_up_value}}")
    assert refused.status_code == 409 and "made_up_value" in refused.json()["detail"]
    assert (await save(client, "instructions", "no.such.file", "x")).status_code == 404
    assert (await save(client, "elsewhere", SCOUT_FILE, "x")).status_code == 404
    async with db.session_factory()() as session:
        assert (await session.scalars(select(InstructionFile))).all() == []


async def test_the_next_handout_carries_the_saved_text_and_names_the_version_it_was_sent(client):
    template = shipped("templates", REPORT_TEMPLATE) + "\n\nEnd every report with the word done."
    assert (await save(client, "templates", REPORT_TEMPLATE, template, "end with done")).status_code == 200
    run_id = await start_search()
    async with db.session_factory()() as session:
        reply = await tools.profile_reply(session, OWNER)
        await session.commit()
    assert reply["task"]["response_template"] == template, "what the scout is sent is the saved version"
    async with db.session_factory()() as session:
        [row] = (await session.scalars(select(TaskHandout))).all()
    kept = {f["kind"]: f for f in row.files}
    assert kept["template"]["version"] == 2 and kept["instruction"]["version"] == 1
    example = await example_of(client)
    assert example["task_id"] == str(run_id) and example["changed_files"] == []
    assert {(f["name"], f["version"]) for f in example["files"]} == {(SCOUT_FILE, 1), (REPORT_TEMPLATE, 2)}
    # a later edit is named against the version it was sent with
    await end_search(run_id)
    tighter = await save(client, "templates", REPORT_TEMPLATE, template + "\nAnd nothing else.", "tighter")
    assert tighter.status_code == 200
    example = await example_of(client)
    assert example["changed_files"] == [{"kind": "template", "name": REPORT_TEMPLATE, "now": "changed", "version": 3}]
    assert {(f["name"], f["version"]) for f in example["files"]} == {(SCOUT_FILE, 1), (REPORT_TEMPLATE, 2)}, (
        "the copy still says which version it was sent with")


async def test_with_no_saved_version_the_harness_reads_the_file_on_disk(js):
    async with db.session_factory()() as session:
        for kind, name in (("instructions", SCOUT_FILE), ("templates", REPORT_TEMPLATE)):
            assert await instruction_files.read(session, OWNER, kind, name) == shipped(kind, name)
            assert (await instruction_files.resolve(session, OWNER, kind, name)).version == 1
        await load_profile(session, OWNER)
        await run.start_round(session, OWNER, started_by="manual")
        reply = await tools.profile_reply(session, OWNER)
        assert reply["task"]["response_template"] == shipped("templates", REPORT_TEMPLATE)
        with pytest.raises(files.UnknownName):
            await instruction_files.read(session, OWNER, "instructions", "no.such.file")
        # another owner's saved version is theirs: this owner still reads the disk
        await instruction_files.save(session, "someone-else", "instructions", SCOUT_FILE, "Their words.", "theirs")
        assert await instruction_files.read(session, OWNER, "instructions", SCOUT_FILE) == shipped(
            "instructions", SCOUT_FILE)


async def test_the_runners_door_gives_the_saved_version_else_the_shipped_file(client, monkeypatch):
    monkeypatch.setattr("app.jobsearch.routes.runner.token_ok", lambda header: True)
    edited = shipped("instructions", SCOUT_FILE) + "\n\nAn owner's line."
    assert (await save(client, "instructions", SCOUT_FILE, edited, "add a line")).status_code == 200
    names = f"{SCOUT_FILE}.instructions.md,jobsearch.analyst.instructions.md,nothing.instructions.md"
    got = (await client.get("/api/v1/agent/instructions", params={"names": names})).json()["files"]
    assert got[f"{SCOUT_FILE}.instructions.md"] == {"text": edited, "version": 2}
    analyst = got["jobsearch.analyst.instructions.md"]
    assert analyst == {"text": shipped("instructions", "jobsearch.analyst"), "version": 1}
    assert "nothing.instructions.md" not in got, "a name that is no file is left for the runner's own copy"
    monkeypatch.setattr("app.jobsearch.routes.runner.token_ok", lambda header: False)
    assert (await client.get("/api/v1/agent/instructions", params={"names": names})).status_code == 401


# --- history and restore -------------------------------------------------------------------------------------------


async def test_the_history_lists_every_version_an_older_one_can_be_read_and_restoring_saves_it_as_a_new_version(client):
    original = shipped("instructions", SCOUT_FILE)
    second, third = original + "\n\nTwo.", original + "\n\nThree."
    for text, why in ((second, "two"), (third, "three")):
        assert (await save(client, "instructions", SCOUT_FILE, text, why)).status_code == 200
    history = (await client.get(file_url("instructions", SCOUT_FILE))).json()["history"]
    assert [(v["version"], v["why"]) for v in history["versions"]] == [
        (3, "three"), (2, "two"), (1, "As shipped with the app."),
    ]
    assert history["current"] == 3
    old = (await client.get(file_url("instructions", SCOUT_FILE, "/versions/2"))).json()
    assert old["version"] == 2 and old["text"] == second and old["why"] == "two"
    assert (await client.get(file_url("instructions", SCOUT_FILE, "/versions/1"))).json()["text"] == original
    assert (await client.get(file_url("instructions", SCOUT_FILE, "/versions/9"))).status_code == 409

    restored = await client.post(file_url("instructions", SCOUT_FILE, "/restore"), json={"version": 2})
    assert restored.status_code == 200
    body = restored.json()
    assert body["file"]["version"] == 4 and body["file"]["text"] == second
    newest = [(v["version"], v["why"]) for v in body["history"]["versions"]][:2]
    assert newest == [(4, "Restored version 2."), (3, "three")]
    # the earlier versions are all still there: nothing was rewritten
    assert (await client.get(file_url("instructions", SCOUT_FILE, "/versions/3"))).json()["text"] == third
    assert (await client.post(file_url("instructions", SCOUT_FILE, "/restore"), json={"version": 4})).status_code == 409
    assert (await client.post(file_url("instructions", SCOUT_FILE, "/restore"), json={"version": 9})).status_code == 409
    # version one is the shipped text and can be brought back too
    back = (await client.post(file_url("instructions", SCOUT_FILE, "/restore"), json={"version": 1})).json()
    assert back["file"]["version"] == 5 and back["file"]["text"] == original


async def test_a_file_that_was_never_changed_has_nothing_to_restore(client):
    res = await client.post(file_url("instructions", SCOUT_FILE, "/restore"), json={"version": 1})
    assert res.status_code == 409


# --- an agent is working with the file -----------------------------------------------------------------------------


async def test_a_file_cant_be_saved_while_an_agent_that_was_sent_it_is_still_working_and_can_after(client):
    run_id = await start_search()
    async with db.session_factory()() as session:
        await tools.profile_reply(session, OWNER)  # the scout is handed its task, with the two files
        await session.commit()
    text = shipped("instructions", SCOUT_FILE) + "\n\nMid-search."
    refused = await save(client, "instructions", SCOUT_FILE, text)
    assert refused.status_code == 409 and refused.json()["detail"].startswith("Scout is working with this file")
    step = await step_of(client, "Scout_searches")
    assert step["sent"]["instruction"]["busy"].startswith("Scout is working")
    assert step["sent"]["template"]["busy"], "the template it was sent is held too"
    assert (await step_of(client, "Open_run"))["sent"] is None
    await end_search(run_id)
    assert (await save(client, "instructions", SCOUT_FILE, text, "after the search")).status_code == 200


# --- a release that ships a different file -------------------------------------------------------------------------


@pytest.fixture
def release(tmp_path, monkeypatch):
    """The instruction and template folders as copies a test can change, as a release does: returns a function that
    ships new text for a file."""
    for folder in ("instructions", "templates"):
        shutil.copytree(getattr(base_settings, f"{folder}_path"), tmp_path / folder)
        monkeypatch.setattr(base_settings, f"{folder}_path", tmp_path / folder)

    def ship(kind: str, name: str, text: str) -> None:
        suffix = files.INSTRUCTIONS_SUFFIX if kind == "instructions" else files.TEMPLATE_SUFFIX
        (tmp_path / kind / f"{name}{suffix}").write_text(text, encoding="utf-8")

    return ship


async def shipped_offers(client) -> list[dict]:
    items = (await client.get("/api/v1/inbox")).json()["items"]
    return [i for i in items if i["kind"] == "instruction_shipped"]  # the old kind of decision: never raised now


async def apply_shipped() -> int:
    async with db.session_factory()() as session:
        done = await instruction_files.apply_shipped(session, OWNER)
        await session.commit()
    return done


async def test_a_newer_shipped_file_replaces_the_owners_version_on_its_own_and_theirs_stays_in_the_history(
    client, release,
):
    mine = shipped("instructions", SCOUT_FILE) + "\n\nMy line."
    assert (await save(client, "instructions", SCOUT_FILE, mine, "my line")).status_code == 200
    assert await apply_shipped() == 0, "nothing shipped yet"

    release("instructions", SCOUT_FILE, "A new shipped scout.\n")
    release("instructions", "jobsearch.analyst", "A new shipped analyst.\n")  # a file the owner never changed
    assert await apply_shipped() == 1
    assert await apply_shipped() == 0, "once"
    assert await shipped_offers(client) == [], "nothing is asked"

    page = (await client.get(file_url("instructions", SCOUT_FILE))).json()
    assert page["file"]["text"] == "A new shipped scout." and page["file"]["version"] == 3
    assert page["file"]["shipped_newer"] is False
    assert [(v["version"], v["why"], v["by"]) for v in page["history"]["versions"][:2]] == [
        (3, "Shipped with the app.", "Shipped with the app"), (2, "my line", "You")]
    async with db.session_factory()() as session:
        old = await instruction_files.version_text(session, OWNER, "instructions", SCOUT_FILE, 2)
    assert old["text"] == mine.strip(), "the owner's version can still be read"


async def test_an_owner_edit_after_a_shipped_update_still_saves_and_a_file_in_use_waits_for_the_next_start(
    client, release,
):
    run_id = await start_search()
    analyst = "jobsearch.analyst"
    assert (await save(client, "instructions", analyst, shipped("instructions", analyst) + "\nMine.", "mine")
            ).status_code == 200
    assert (await save(client, "instructions", SCOUT_FILE, shipped("instructions", SCOUT_FILE) + "\nMine.", "mine")
            ).status_code == 200
    release("instructions", SCOUT_FILE, "Shipped, new.\n")
    release("instructions", analyst, "Analyst, new.\n")
    async with db.session_factory()() as session:
        await tools.profile_reply(session, OWNER)  # the scout is working with the scout file
        await session.commit()
    assert await apply_shipped() == 1, "the analyst's file is updated; the scout file is in use"
    async with db.session_factory()() as session:
        assert await instruction_files.read(session, OWNER, "instructions", SCOUT_FILE) != "Shipped, new."
    await end_search(run_id)
    assert await apply_shipped() == 1
    async with db.session_factory()() as session:
        assert await instruction_files.read(session, OWNER, "instructions", SCOUT_FILE) == "Shipped, new."

    res = await save(client, "instructions", SCOUT_FILE, "Shipped, new.\nMy edit after.", "my edit after")
    assert res.status_code == 200 and res.json()["file"]["version"] == 4
    assert res.json()["file"]["text"] == "Shipped, new.\nMy edit after."
    assert await apply_shipped() == 0, "their edit stands until a release ships different text again"


# --- the scoring text and the template ask for the two kinds of row (docs/PRD.md 30.9) ------------------------------

SCORING = "jobsearch.scoring"
ANALYSIS = "jobsearch.analysis"


def bullet(text: str, lead: str) -> str:
    """The one bullet of an instruction file that begins with this bold lead, as one string."""
    return next(line for line in text.splitlines() if line.startswith(f"- **{lead}"))


def test_the_scoring_text_says_the_statements_of_a_first_match_row_are_each_answered_and_never_the_row():
    said = bullet(shipped("instructions", SCORING), 'The statements of a "first match" row')
    assert "`part_of`" in said and "`first_match`" in said and "answer every one of them by its own `id`" in said
    assert "`met`, `not` or `unknown`" in said, "the same three words as an ask"
    assert "even when a statement above it is `met` or `unknown`" in said and "from the top" in said
    assert "never answer for the row" in said


def test_the_scoring_text_says_the_statements_of_an_any_of_row_are_answered_like_asks_and_never_the_row():
    said = bullet(shipped("instructions", SCORING), 'The statements of an "any of" row')
    assert "`part_of`" in said and "`any_of`" in said and "answer each by its own `id`" in said
    assert "`met`, `not` or `unknown`" in said, "the same three words as an ask"
    assert "never answer for the row" in said and "app's to work out" in said


def test_the_scoring_text_says_an_agent_is_not_asked_what_the_app_decides_itself():
    said = bullet(shipped("instructions", SCORING), "Leave out an ask, a statement or a row marked")
    assert "`who_checks: the app`" in said and "employer fact" in said and "classes" in said
    assert "not asked" in said and "One that is not marked is yours to answer" in said


def test_the_scoring_text_keeps_the_overlap_to_the_app_and_the_old_sentence_that_said_otherwise_is_gone():
    said = shipped("instructions", SCORING)
    assert "the app adds up each" not in said, "each ask no longer counts on its own: a row can hold statements"
    lead = bullet(said, "Answer every question on its own words")
    assert "both answered" in lead and "never add, drop or merge answers yourself" in lead


def test_the_scoring_text_uses_the_owners_two_words_for_a_row_and_no_others():
    said = shipped("instructions", SCORING)
    assert "Some asks come as a first-match row or as the statements of an \"any of\" row" in said.split("\n- ")[0]
    for word in ("all of", "ranked", "pick_one", "pick one", " box", " level"):
        assert word not in said.lower(), word


def test_the_scoring_texts_example_shows_a_first_match_answer_and_the_answer_to_a_statement_of_an_any_of():
    block = shipped("instructions", SCORING).split("fit_breakdown:")[-1]
    assert '{id: "stmt-c", answer: "unknown", evidence:' in block and "first-match row" in block
    assert '{id: "stmt-d", answer: "not", evidence:' in block and "'any of' row" in block


def example_asks(name: str) -> list[dict]:
    """The answers a template shows an agent, as the YAML it is."""
    body = yaml.safe_load(shipped("templates", name))
    breakdown = body["roles"][0]["fit_breakdown"] if "roles" in body else body["fit_breakdown"]
    return breakdown["asks"]


@pytest.mark.parametrize("name", [ANALYSIS])
def test_the_template_shows_the_statement_of_a_first_match_row_and_of_an_any_of_row_answered_like_an_ask(name):
    by_id = {a["id"]: a for a in example_asks(name)}
    assert by_id["stmt-c"]["answer"] == "unknown" and "statement" not in by_id["stmt-c"]
    assert by_id["stmt-d"]["answer"] == "met" and "statement" not in by_id["stmt-d"]
    assert "partly" not in {a.get("answer") for a in by_id.values()}, "no template shows an answer of partly"
    raw = shipped("templates", name)
    assert "a statement of a first-match row" in raw and "answer every statement of the row" in raw
    assert "a statement of an any-of row" in raw and "answered like an ask" in raw


# --- an owner who edited the scoring text, and a release that ships new words -----------------------------------------


async def test_the_scoring_text_a_release_changes_is_followed_when_the_owner_never_edited_it(client, release):
    release("instructions", SCORING, "New shipped scoring words.\n")
    async with db.session_factory()() as session:
        assert await instruction_files.read(session, OWNER, "instructions", SCORING) == "New shipped scoring words."
        assert await instruction_files.apply_shipped(session, OWNER) == 0, "nothing of theirs to replace"
    page = (await client.get(file_url("instructions", SCORING))).json()
    assert page["file"]["version"] == 1 and page["file"]["edited"] is False


async def test_the_scoring_text_a_release_changes_replaces_the_owners_edited_copy_without_asking(client, release):
    mine = shipped("instructions", SCORING) + "\n\nMy own line about the asks."
    assert (await save(client, "instructions", SCORING, mine, "my line")).status_code == 200
    release("instructions", SCORING, "New shipped scoring words.\n")
    assert await apply_shipped() == 1
    async with db.session_factory()() as session:
        assert await instruction_files.read(session, OWNER, "instructions", SCORING) == "New shipped scoring words."
    assert await shipped_offers(client) == []
