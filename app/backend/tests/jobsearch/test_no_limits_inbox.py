"""No character limit on what the owner says in the Inbox.

The note beside Done, Set aside and a decision's answer is kept whole in `tasks.note` and shown whole in the history of
decisions. An answer to the scout's question reaches the scout whole, with its line breaks, in the escalation's
`data.escalation.answer.note`. A site answer that, with the instruction file the agent is sent beside it, would pass
what one reply leaves for the two is refused with sentence 5 of the spec and changes nothing.

The step that hands a task out (`sites/agent_handout.py`) measures it as the door will send it and holds it only past
`REPLY_CEILING_CHARS`, with its size. It is not held to the batch size (`AGENT_PAYLOAD_BUDGET_CHARS`), which is for
lists of items. Synthetic data only."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.inbox import service
from app.jobsearch.owner_text import ROOM_CHARS, too_large_words
from app.jobsearch.work import instruction_files
from app.models.work import Task
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, REPLY_CEILING_CHARS, payload_chars
from app.sites import agent_handout, asks, inbox_asks, site_check
from app.sites.models import PlanItemAction, PlanTask, PlanTaskStatus
from app.sites.tools import ANSWER_OPTION
from tests.sites.builders import item, new_project, propose, take
from tests.support import OWNER

LONG = 20_000


def _words(tag: str, n: int = LONG) -> str:
    """About `n` characters of made-up plain sentences, each different, so a cut anywhere shows."""
    out, i = [], 0
    while sum(len(s) + 1 for s in out) < n:
        i += 1
        out.append(f"{tag} sentence {i} says one more thing the owner wants.")
    return " ".join(out)[: n - 1] + "!"  # exactly n characters, and no space at either end for a save to trim


@pytest.fixture
def site_inbox():
    """The site loop's asks wired to the Inbox, as the app does at start-up; the default put back afterwards."""
    inbox_asks.install()
    yield
    asks.set_ask_owner(None)


@pytest.fixture
def check_says(monkeypatch):
    """Every item is on plan: the stand-in for `site.check`."""

    async def fake(session, instance, data):
        return {"on_plan": True, "difference": ""}

    monkeypatch.setattr(site_check, "site_check", fake)


async def _waiting_question(session, project, action=PlanItemAction.do):
    """The scout holds a task of `action` (an item, unless it says the plan) and asks the owner a question (what
    `task.escalate` leaves): `(the task, the Inbox task)`. The Inbox task's options are the box for the owner's
    answer."""
    conn = await roster.ensure_roster(session, OWNER)
    held = await take(session, project, conn)
    assert held.action == action
    held.status, held.checked_out_by = PlanTaskStatus.waiting, None
    held.data = {**(held.data or {}), "escalation": {
        "reason": "only_you_know", "question": "Which page lists the roles?", "options": [dict(ANSWER_OPTION)]}}
    ask = Task(
        owner=OWNER, key=f"escalate-{held.id}", title="Which page lists the roles?", kind=inbox_asks.ESCALATION_KIND,
        options=[dict(ANSWER_OPTION)], recommended="answer", link_kind="project", link_id=project.id)
    session.add(ask)
    await session.flush()
    return held, ask


async def _save_big_file(session, name: str, size: int) -> int:
    """The owner's newest version of an instruction file, `size` characters (saved straight, as the file routes do
    once they take any length)."""
    row = await instruction_files._add(session, OWNER, "instructions", name, _words("file", size), "made big", None)
    return len(row.text)


# --- test 4: the note beside done, skip and answer ------------------------------------------------------------------


async def test_done_skip_and_answer_each_keep_a_long_note_whole_and_the_history_shows_it_whole(client):
    note = _words("note")
    async with db.session_factory()() as session:
        done = await service.raise_task(session, OWNER, key="t-done", title="Look it over", kind="chore")
        skipped = await service.raise_task(session, OWNER, key="t-skip", title="Put it off", kind="chore")
        asked = await service.raise_task(
            session, OWNER, key="t-ask", title="Which way?", kind="decision", options=[{"id": "yes", "label": "Yes"}])
        await session.commit()
    sent = {
        "done": await client.post(f"/api/v1/tasks/{done.id}/done", json={"note": note}),
        "skip": await client.post(f"/api/v1/tasks/{skipped.id}/skip", json={"reason": note}),
        "answer": await client.post(f"/api/v1/tasks/{asked.id}/answer", json={"option": "yes", "note": note}),
    }
    for how, res in sent.items():
        assert res.status_code == 200, (how, res.text)
        assert res.json()["note"] == note and len(res.json()["note"]) == LONG, how
    async with db.session_factory()() as session:
        stored = (await session.scalars(select(Task.note).where(Task.key.in_(["t-done", "t-skip", "t-ask"])))).all()
    assert stored == [note] * 3
    history = (await client.get("/api/v1/inbox/history")).json()["items"]
    assert {h["title"]: h["why"] for h in history} == {
        "Look it over": note, "Put it off": note, "Which way?": note}


# --- test 5: the words that go to the agents -------------------------------------------------------------------------


async def test_an_answer_to_the_scouts_question_is_kept_whole_where_the_scout_reads_it(
        client, site_inbox, check_says, knocks):
    note = _words("answer", 4_000) + "\n" + _words("detail", 4_000)
    async with db.session_factory()() as session:
        project = await new_project(session)
        await propose(session, project, item("a"))
        held, ask = await _waiting_question(session, project)
        await session.commit()
    res = await client.post(f"/api/v1/tasks/{ask.id}/answer", json={"option": "answer", "note": note})
    assert res.status_code == 200, res.text
    async with db.session_factory()() as session:
        again = await session.get(PlanTask, held.id)
        assert again.status == PlanTaskStatus.todo
        assert again.data["escalation"]["answer"]["note"] == note
        assert (await session.get(Task, ask.id)).note == note


# --- test 6: refused when an agent could not be sent it ---------------------------------------------------------------


async def test_a_site_answer_that_with_the_planners_file_passes_the_room_is_refused_and_changes_nothing(
        client, site_inbox, check_says, knocks):
    async with db.session_factory()() as session:
        project = await new_project(session)
        held, ask = await _waiting_question(session, project, PlanItemAction.plan)
        size = await _save_big_file(session, "projects.plan.write", 20_000)
        await session.commit()
    over = _words("answer", ROOM_CHARS - size + 1)
    assert len(over) + size == ROOM_CHARS + 1
    res = await client.post(f"/api/v1/tasks/{ask.id}/answer", json={"option": "answer", "note": over})
    assert res.status_code == 413
    assert res.json() == {"detail": too_large_words("Your answer", len(over), beside=("the file sent with it", size))}
    assert "of the two" in res.json()["detail"] and "Nothing was cut and nothing was saved." in res.json()["detail"]
    async with db.session_factory()() as session:
        assert (await session.get(Task, ask.id)).state == "open" and (await session.get(Task, ask.id)).note is None
        assert (await session.get(PlanTask, held.id)).status == PlanTaskStatus.waiting, "the planner was not handed it"
    assert knocks == []
    async with db.session_factory()() as session:  # one character fewer is the room exactly: not refused
        again = await session.get(Task, ask.id)
        await inbox_asks.refuse_over_room(session, again, _words("answer", ROOM_CHARS - size))


async def test_an_answer_to_the_scouts_question_is_measured_against_the_file_of_the_item_the_scout_holds(
        client, site_inbox, check_says, knocks):
    async with db.session_factory()() as session:
        project = await new_project(session)
        await propose(session, project, item("a"))
        held, ask = await _waiting_question(session, project)
        size = await _save_big_file(session, "projects.do.item", 30_000)
        await session.commit()
    over = _words("answer", ROOM_CHARS - size + 1)
    res = await client.post(f"/api/v1/tasks/{ask.id}/answer", json={"option": "answer", "note": over})
    assert res.status_code == 413
    assert res.json() == {"detail": too_large_words("Your answer", len(over), beside=("the file sent with it", size))}
    async with db.session_factory()() as session:
        assert (await session.get(Task, ask.id)).state == "open"
        assert (await session.get(PlanTask, held.id)).status == PlanTaskStatus.waiting


# --- test 6b: the step that hands the words out is held only past the ceiling, never at a batch size -----------------


async def _write_task(session) -> PlanTask | None:
    """The "Write plan" task the scout has not taken yet."""
    return await session.scalar(
        select(PlanTask).where(PlanTask.action == PlanItemAction.plan, PlanTask.status == PlanTaskStatus.todo))


async def test_the_hand_out_is_held_at_the_ceiling_and_not_at_a_batch_and_says_its_size(js):
    wait = SimpleNamespace(element_id="PR-4", payload={"role": "scout", "name": "PR-4 Write the plan"})
    shell = payload_chars({"ok": True, "task": {"text": ""}})  # what the reply holds besides the text

    def view_of(size: int):
        async def build(session, task):
            return {"text": "x" * (size - shell)}

        return build

    async with db.session_factory()() as session:
        await new_project(session)
        write = await _write_task(session)
        spec = await agent_handout.hand_out(session, wait, write, view_of(AGENT_PAYLOAD_BUDGET_CHARS * 3))
        assert spec.role_word == "scout" and agent_handout.stamp_of(write) is not None, "three batches go out"
        await agent_handout.hand_out(session, wait, write, view_of(REPLY_CEILING_CHARS))  # exactly what the door sends
        with pytest.raises(agent_handout.HandOutError) as held:
            await agent_handout.hand_out(session, wait, write, view_of(REPLY_CEILING_CHARS + 1))
    assert str(held.value) == (
        f"what the scout would be sent for 'PR-4 Write the plan' is {REPLY_CEILING_CHARS + 1:,} characters, "
        f"over the {REPLY_CEILING_CHARS:,} an agent is sent at most; nothing was cut")
