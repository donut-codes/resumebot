"""The `changed` message (live_read.py, live.py): a run of items of the kind
`changed` becomes one message for each area, with the ids of that area, built from no row. Everything is made up."""

import pytest

from app import db
from app.jobsearch.pipeline import live, live_read
from tests.support import OWNER


def test_changed_is_a_topic_a_stream_can_ask_for_and_a_kind_that_can_be_built():
    assert "changed" in live.TOPICS and "changed" in live_read.KINDS
    assert live_read.KINDS == live.TOPICS
    assert live.parse_topics("changed, task") == frozenset({"changed", "task"})


def test_the_areas_are_these_eight_in_this_order():
    assert live_read.AREAS == ("jobs", "gaps", "agents", "missions", "harness", "prep", "go", "ready")


def test_the_ids_are_grouped_by_area_once_each_in_the_order_of_the_areas():
    got = live_read.changed_messages(["b", "a", "c", "b"], [("gaps",), ("jobs",), ("jobs",), ("gaps",)])
    assert got == [{"area": "jobs", "ids": ["a", "c"]}, {"area": "gaps", "ids": ["b"]}]


def test_a_tag_that_is_no_area_makes_no_message():
    assert live_read.changed_messages(["a"], [("nonsense",)]) == []
    assert live_read.changed_messages([], []) == []


async def test_build_gives_the_changed_messages_without_reading_a_row(js):
    async with db.read_session_factory()() as session:
        got = await live_read.build(session, OWNER, "changed", ["x", "y"], [("agents",), ("ready",)])
    assert got == [{"area": "agents", "ids": ["x"]}, {"area": "ready", "ids": ["y"]}]


async def test_build_still_wants_one_tags_tuple_for_each_id(js):
    async with db.read_session_factory()() as session:
        with pytest.raises(ValueError):
            await live_read.build(session, OWNER, "changed", ["x"], [])
