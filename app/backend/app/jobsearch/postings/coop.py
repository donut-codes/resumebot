"""Giving the loop back from a loop that does no waiting of its own.

The app runs on one event loop, and a loop of pure work (hashing a board's items, cleaning copies, judging postings, a
role index) with no `await` in it holds the loop until it ends: every other task, a transaction's next statement
among them, waits, and the transaction it belongs to is held that long (storing a big board, training the classifier and
building the roles' index are each a long stretch). `asyncio.to_thread` does not help (the GIL keeps the one core); a
yield does: the loop runs what is waiting and comes back.

    coop = Cooperate()
    for item in items:
        await coop()      # a yield once `EVERY` seconds have gone since the last
        ...pure work...

It is a clock, not a count, so a step that costs a millisecond and one that costs fifty are both given back inside the
bound."""

import asyncio
import time

#: How long pure work runs before it lets the loop go (seconds).
EVERY = 0.005


class Cooperate:
    """`await` an instance at each step of a loop of pure work: it yields the event loop (`asyncio.sleep(0)`) once
    `every` seconds have passed since it last did, and costs one clock read otherwise."""

    def __init__(self, every: float = EVERY) -> None:
        self.every = every
        self._at = time.perf_counter()

    async def __call__(self) -> None:
        if time.perf_counter() - self._at >= self.every:
            await asyncio.sleep(0)
            self._at = time.perf_counter()
