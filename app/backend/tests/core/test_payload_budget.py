"""What one reply to an agent may hold (planengine/budget.py). The owner's own text goes whole and is never cut to fit:
not halved to a budget made for small local models. A list of
items is sent in batches of up to the batch size, and a reply over the ceiling is refused with its size. Synthetic data
only."""

from types import SimpleNamespace

from app import db
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.jobs import check as role_check
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.documents import WRITING_SECTIONS, rules_for
from app.jobsearch.writing import packets, seats
from app.planengine.budget import (
    AGENT_PAYLOAD_BUDGET_CHARS,
    ALWAYS_SHOWN_CHARS,
    REPLY_CEILING_CHARS,
    RESULT_SIZE_META,
    payload_chars,
)
from tests.builders import WRITING_FULL, library_built, packet_setup
from tests.support import OWNER

#: The share of what is always shown that a batch may take: what the wire adds to the characters counted here, or a few
#: more words in a reply, cannot tip it over.
MOST_OF_IT = 3 / 5
QA = WRITING_SECTIONS[-1][1]  # Settings for QA of documentation


def test_a_batch_of_items_is_well_under_what_claude_code_shows_without_the_annotation():
    assert AGENT_PAYLOAD_BUDGET_CHARS <= ALWAYS_SHOWN_CHARS * MOST_OF_IT


def test_a_role_check_batch_is_held_to_the_budget_when_its_items_are_many_and_long():
    """The batch is up to 100 items, as many as the budget holds: with long items that is fewer, and none is lost."""
    looks = {"company": "A made-up agency.", "target_roles": [
        {"id": "builders", "name": "Builders", "titles": ["Director of Programs"], "description": "Builds programs."}]}
    items = [{"title": f"Role Title {n}", "company": "Fabrikam", "place": "Remote", "text": "More words. " * 25}
             for n in range(250)]
    job = SimpleNamespace(looks=looks, answers={}, items=items)
    handed, left = [], role_check._unanswered(job)
    while left:
        batch = role_check._fit_batch(job, left)
        reply = {"ok": True, "what_to_look_for": job.looks, "items": batch, "more": 0, "how": role_check.CHECK_HOW}
        assert 0 < len(batch) < role_check.BATCH, "long items: the budget cuts a batch short of 100"
        assert payload_chars(reply) <= AGENT_PAYLOAD_BUDGET_CHARS
        handed += [item["id"] for item in batch]
        job.answers = {**job.answers, **{item["id"]: {} for item in batch}}
        left = role_check._unanswered(job)
    assert handed == [str(n) for n in range(1, 251)], "every item was handed, in order"


async def test_the_writers_rules_go_to_the_composer_whole_when_they_run_to_sixty_thousand_characters(js):
    """The owner's writing rules were 61,274 characters, 54 steps in the review every packet gets: the writer was handed
    27 of them, the rest cut to fit. Here a made-up set of that size, every list of it, goes out as it was saved."""
    role_id = await packet_setup()
    step = "Check that the draft says what its source says and nothing the source does not say, line by line. " * 7
    rule = "Keep the sentences short and the words plain, and say each thing once, where it belongs on the page. " * 3
    writing = {**WRITING_FULL,
               "review_steps": [{"id": f"step-{n}", "says": f"Step {n}: {step}"} for n in range(54)],
               "voice_rules": [{"id": f"voice-{n}", "says": f"Voice {n}: {rule}"} for n in range(60)],
               "ats_rules": [{"id": f"ats-{n}", "says": f"Keyword {n}: {rule}"} for n in range(60)]}
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "writing", writing, by="test")
        await library_built(session)
        await packets.start(session, OWNER, role_id, "")
        await session.commit()
    async with db.session_factory()() as session:
        owners = await profile_store.owner_documents(session, OWNER)
        taken = (await seats.take(session, OWNER, "s-1"))["seat"]
        await session.commit()
    expected = rules_for(owners.writing, "resume", "letter", "review", role=None)
    assert 58_000 < payload_chars(expected) < REPLY_CEILING_CHARS, "over what the writer's part used to be held to"
    system = taken["system"]
    assert "cut_to_fit" not in system
    assert all(f"Step {n}: " in system for n in range(54)), "every step of the review"
    assert all(f"Voice {n}: " in system and f"Keyword {n}: " in system for n in range(60)), "every rule, whole"
    assert len(expected[QA]["The review every packet gets"]) == 54


async def test_a_reply_over_the_ceiling_is_refused_with_its_size_and_nothing_is_cut(js, door):
    async def body(session, owner):
        return {"ok": True, "text": "x" * (REPLY_CEILING_CHARS + 1)}

    refused = await mcp_server.door.call(door, "jobs.big", body)
    assert refused["ok"] is False and "nothing was cut" in refused["reason"]
    assert f"{REPLY_CEILING_CHARS:,}" in refused["reason"]
    assert "text" not in refused

    async def fits(session, owner):
        return {"ok": True, "text": "x" * (REPLY_CEILING_CHARS - 100)}

    assert (await mcp_server.door.call(door, "jobs.big", fits))["ok"] is True


async def test_every_tool_tells_claude_code_a_reply_up_to_the_ceiling_is_to_be_shown_whole():
    """Without the annotation Claude Code saves a reply of over 50,000 characters to a file the session cannot read."""
    tools_listed = await mcp_server.door.mcp.list_tools()
    assert tools_listed and all(t.meta == RESULT_SIZE_META for t in tools_listed)
    limit = RESULT_SIZE_META["anthropic/maxResultSizeChars"]
    assert REPLY_CEILING_CHARS < limit <= 500_000, "within Claude Code's own maximum"
