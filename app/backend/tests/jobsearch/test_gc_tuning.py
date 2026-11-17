"""The start raises the garbage collector's third threshold (`startup.tune_gc_threshold`): with a large heap each
full collection stops the one event loop, and the default runs many of them."""

import gc

import pytest

from app.jobsearch import main
from app.jobsearch.loops import startup


def test_tune_gc_threshold_sets_the_full_collection_threshold_to_1000():
    before = gc.get_threshold()
    try:
        startup.tune_gc_threshold()
        assert gc.get_threshold() == (700, 10, 1000)
    finally:
        gc.set_threshold(*before)


async def test_the_start_tunes_the_collector_before_any_other_step(monkeypatch):
    class Reached(Exception):
        pass

    def recorder():
        raise Reached  # stops the start at its first line, so nothing else of it runs

    monkeypatch.setattr(startup, "tune_gc_threshold", recorder)
    with pytest.raises(Reached):
        async with main._running():
            pass
