"""The room an agent leaves for the owner's own text (`jobsearch/owner_text.py`). An agent is sent at most
`REPLY_CEILING_CHARS` at once; a text the owner writes for an agent to read whole, that would not fit with what is
sent beside it, is refused in words and never cut. Synthetic data only."""

import pytest

from app.errors import TooLarge
from app.jobsearch.owner_text import (
    RESERVE_CHARS,
    ROOM_CHARS,
    TextTooLarge,
    refuse_over_room,
    too_large_words,
)
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, REPLY_CEILING_CHARS


def test_a_text_that_fills_the_room_passes_and_one_more_character_is_refused_in_words():
    """Test 1."""
    refuse_over_room("x" * ROOM_CHARS, "Your resume text")  # exactly the room: no refusal
    with pytest.raises(TextTooLarge) as refused:
        refuse_over_room(
            "x" * (ROOM_CHARS + 1), "Your resume text",
            result="it was not sent. Nothing was cut and nothing was saved.", fix="Check that it is your resume.")
    assert isinstance(refused.value, TooLarge) and refused.value.status == 413
    words = str(refused.value)
    assert f"{ROOM_CHARS + 1:,}" in words and "160,000" in words, "the size and the room, with thousands separators"
    assert "it was not sent. Nothing was cut and nothing was saved." in words
    assert "Check that it is your resume." in words
    assert refused.value.body() == {"detail": words}, "answered 413 with the sentence as `detail`"


def test_the_room_is_the_reply_ceiling_less_a_reserve_for_what_rides_beside_the_text():
    """Test 2."""
    assert ROOM_CHARS == REPLY_CEILING_CHARS - RESERVE_CHARS == 160_000
    assert RESERVE_CHARS == AGENT_PAYLOAD_BUDGET_CHARS + 10_000


def test_a_text_is_checked_against_the_room_less_the_text_sent_beside_it():
    """Test 2b."""
    beside = ("the file sent with it", 20_000)
    refuse_over_room("x" * 140_000, "This file", beside=beside)  # 140,000 + 20,000 is the room
    with pytest.raises(TextTooLarge) as refused:
        refuse_over_room("x" * 140_001, "This file", beside=beside)
    words = str(refused.value)
    assert "140,001" in words and "20,000" in words, "both sizes are named"
    assert "of the two" in words
    with pytest.raises(TextTooLarge) as alone:
        refuse_over_room("x" * (ROOM_CHARS + 1), "This file")
    assert "of the two" not in str(alone.value), "without a text beside it the sentence names one size"


def test_the_sentence_takes_the_room_of_the_reply_it_is_for():
    """Test 3: the packet's reply (the whole ceiling) and the coach's (150,000) each give their own room."""
    packet = too_large_words("The writer's brief for this packet", 214_000, room=REPLY_CEILING_CHARS)
    assert "at most 200,000 at once" in packet
    coach = too_large_words("The talk", 214_000, room=150_000)
    assert "at most 150,000 at once" in coach
    with pytest.raises(TextTooLarge) as refused:
        refuse_over_room("x" * 150_001, "The talk", room=150_000)
    assert "150,000" in str(refused.value)
    refuse_over_room("x" * 150_000, "The talk", room=150_000)


def test_the_refusal_is_never_a_cut():
    """Whole or refused: the text given is left as it was."""
    text = "word " * (ROOM_CHARS // 5 + 10)
    with pytest.raises(TextTooLarge):
        refuse_over_room(text, "This file")
    assert len(text) > ROOM_CHARS


def test_every_sentence_of_the_screens_and_words_table_is_what_too_large_words_makes():
    """Test 3b: sentences 1 to 7 of the spec's "Screens and words", word for word, built from their sizes."""
    saved_unsent = "it was not sent. Nothing was cut and nothing was saved."
    assert too_large_words(
        "Your resume text", 214_000, beside=("the answer shape sent with it", 1_706), result=saved_unsent,
        fix="Check that it is your resume.",
    ) == (
        "Your resume text is 214,000 characters, and the answer shape sent with it is 1,706. An agent can be sent at "
        "most 160,000 of the two at once, so it was not sent. Nothing was cut and nothing was saved. Check that it is "
        "your resume.")
    assert too_large_words(
        "This file's text", 214_000, beside=("the answer shape sent with it", 1_706), result=saved_unsent,
        fix="Check that it is the right file.",
    ) == (
        "This file's text is 214,000 characters, and the answer shape sent with it is 1,706. An agent can be sent at "
        "most 160,000 of the two at once, so it was not sent. Nothing was cut and nothing was saved. Check that it is "
        "the right file.")
    assert too_large_words(
        "This file", 150_000, beside=("the file sent with it", 20_000), result="it was not saved. Nothing was cut.",
        fix="Shorten one of them.",
    ) == (
        "This file is 150,000 characters, and the file sent with it is 20,000. An agent can be sent at most 160,000 of "
        "the two at once, so it was not saved. Nothing was cut. Shorten one of them.")
    assert too_large_words("This file", 214_000, result="it was not saved. Nothing was cut.") == (
        "This file is 214,000 characters. An agent can be sent at most 160,000 at once, so it was not saved. Nothing "
        "was cut. Shorten it.")
    assert too_large_words("What you wrote", 214_000) == (
        "What you wrote is 214,000 characters. An agent can be sent at most 160,000 at once, so it was not sent. "
        "Nothing was cut and nothing was saved. Shorten it.")
    assert too_large_words(
        "Your answer", 214_000, beside=("the file sent with it", 3_282),
    ) == (
        "Your answer is 214,000 characters, and the file sent with it is 3,282. An agent can be sent at most 160,000 "
        "of the two at once, so it was not sent. Nothing was cut and nothing was saved. Shorten it.")
    assert too_large_words(
        "The writer's brief for this packet", 214_000, room=REPLY_CEILING_CHARS,
        result="no packet was started. Nothing was cut and nothing was saved.",
        fix="Shorten what you asked for, or the writer's files under How they work.",
    ) == (
        "The writer's brief for this packet is 214,000 characters. An agent can be sent at most 200,000 at once, so no "
        "packet was started. Nothing was cut and nothing was saved. Shorten what you asked for, or the writer's files "
        "under How they work.")
    assert too_large_words(
        "The writer's brief for this packet", 214_000, room=REPLY_CEILING_CHARS,
        result="no packet was started. Nothing was cut and nothing was saved.",
        fix="Shorten the writer's files under How they work.",
    ) == (
        "The writer's brief for this packet is 214,000 characters. An agent can be sent at most 200,000 at once, so no "
        "packet was started. Nothing was cut and nothing was saved. Shorten the writer's files under How they work.")
