"""A loop of pure work gives the event loop back on a clock (`postings/coop.py`): once a few milliseconds have gone,
never per item, so a step of a millisecond and a step of fifty are both given back inside the bound."""

import asyncio

from app.jobsearch.postings.coop import EVERY, Cooperate


async def _others_run_during(coop: Cooperate, steps: int) -> int:
    ran = 0

    async def other():
        nonlocal ran
        while True:
            await asyncio.sleep(0)
            ran += 1

    task = asyncio.get_running_loop().create_task(other())
    try:
        for _ in range(steps):
            await coop()
    finally:
        task.cancel()
    return ran


async def test_it_gives_the_loop_back_at_every_step_when_no_time_is_allowed():
    assert await _others_run_during(Cooperate(every=0.0), 20) >= 15


async def test_it_does_not_give_the_loop_back_before_its_time_is_up():
    assert await _others_run_during(Cooperate(every=60.0), 200) == 0


async def test_a_step_that_takes_longer_than_its_time_is_followed_by_a_yield():
    import time

    coop = Cooperate(every=0.01)
    await coop()
    time.sleep(0.02)  # a step that holds the loop: the next call gives it back
    ran = False

    async def other():
        nonlocal ran
        ran = True

    task = asyncio.get_running_loop().create_task(other())
    await coop()
    await task
    assert ran


def test_the_default_is_a_few_milliseconds():
    assert 0.001 <= EVERY <= 0.01
