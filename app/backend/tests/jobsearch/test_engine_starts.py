"""The engine door's starts (`engine_calls.register` and `engine_calls.start`): a request path that begins a background
task asks for it by name, and the door in this process runs the starter registered under that name, as
`asyncio.create_task` did. The sites that start tasks this way have tests of their own (test_go_starts_by_name.py and
the files beside it)."""

import pytest

from app.jobsearch.work import engine_calls


@pytest.fixture
def probe():
    """A name of the test's own in the registry; gone after the test."""
    yield "probe"
    engine_calls._starters.pop("probe", None)


def test_the_local_door_runs_the_registered_starter_once_with_the_key(probe):
    seen: list = []
    engine_calls.register(probe, seen.append)
    engine_calls.start(probe, ("owner", 3))
    assert seen == [("owner", 3)]


def test_registering_a_name_again_replaces_its_starter(probe):
    first: list = []
    second: list = []
    engine_calls.register(probe, first.append)
    engine_calls.register(probe, second.append)
    engine_calls.start(probe, "k")
    assert first == [] and second == ["k"]


def test_the_local_door_refuses_a_name_nobody_registered(probe):
    with pytest.raises(LookupError):
        engine_calls.start(probe, "k")


def test_a_recording_door_runs_nothing_and_keeps_each_start(probe, fake_starts):
    seen: list = []
    engine_calls.register(probe, seen.append)
    engine_calls.start(probe, "a")
    engine_calls.start(probe, "b")
    assert seen == []
    assert fake_starts.starts == [(probe, "a"), (probe, "b")]
