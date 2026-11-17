"""Stand-ins for the engine in the tests of the writes whose work the engine does. `Told` is the
engine door (`engine_calls.Backend`): it writes down what it is told and rings nothing. With `answers=True` each
nudge makes the engine look once, as the dispatcher does for an ask: it answers every ask that has no answer yet,
and the page's wait ends. The engine's code runs in the test's own process, so while it looks the process is the whole
app (`program.PROGRAM` is `all`): some engine code (`hand_outs.lock`, `regate.run`) refuses to run in the page.
`the_engine` puts one in place for a block. Everything here is made up."""

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any

from app import program
from app.jobsearch.work import engine_asks, engine_calls
from app.jobsearch.work.queue import topics
from app.settings import settings
from app.timeutil import utc_now
from tests.support import OWNER


async def the_engine_looks() -> int:
    """The engine looks once: it answers every ask that has no answer yet (`engine_asks.answer_due`) and returns
    how many it answered. While it looks this process is the whole app, because the engine's code runs here and some
    of it refuses to run in the page; the page's own value comes back after."""
    page_is = program.PROGRAM
    program.PROGRAM = "all"
    try:
        return await engine_asks.answer_due(OWNER, [topics.ANY], utc_now())
    finally:
        program.PROGRAM = page_is


class Told:
    """A backend that only writes down what it was asked (the same calls `engine_calls.Backend` names)."""

    def __init__(self, answers: bool = True) -> None:
        self.calls: list[tuple] = []
        self.answers = answers
        self.looks: list[asyncio.Task] = []

    async def knock(self, run_id: str, reason: str) -> bool:
        self.calls.append(("knock", run_id, reason))
        return True

    async def dispatch_now(self) -> int:
        self.calls.append(("dispatch_now",))
        return 0

    async def knocks(self, pending: list) -> None:
        self.calls.append(("knocks", list(pending)))

    async def send_wakes(self, wakes: Any) -> list[str]:
        self.calls.append(("send_wakes", list(wakes)))
        return []

    def nudge(self) -> None:
        self.calls.append(("nudge",))
        if self.answers:
            # Each nudge makes the engine look once more, after the look before it has ended (two looks never run at
            # once). A commit's own hook nudges too, and may do so before the ask is saved: that look finds nothing,
            # and the ask's own nudge makes the one that answers.
            before = self.looks[-1] if self.looks else None
            self.looks.append(asyncio.get_running_loop().create_task(self._look(before)))

    async def _look(self, before: asyncio.Task | None) -> None:
        if before is not None:
            await asyncio.gather(before, return_exceptions=True)
        await the_engine_looks()

    def start(self, name: str, key: Any) -> None:
        self.calls.append(("start", name))

    async def settle(self) -> None:
        """Wait for every look the engine made, and for the looks those made (an answer's commit nudges again)."""
        waited = 0
        while waited < len(self.looks):
            batch = self.looks[waited:]
            waited = len(self.looks)
            await asyncio.gather(*batch)

    def told(self) -> list[tuple]:
        """What the engine was told, without the nudges (a nudge is only a hint to look)."""
        return [call for call in self.calls if call[0] != "nudge"]


@contextlib.contextmanager
def the_engine(answers: bool = True) -> Iterator[Told]:
    """The engine door is a `Told` for the block; the door that was there comes back after it."""
    told = Told(answers)
    before = engine_calls._backend
    engine_calls.install(told)
    try:
        yield told
    finally:
        engine_calls.install(before)


def as_the_page(monkeypatch: Any) -> None:
    """This process is the page program for the test, acting for the owner of the tests: `engine_calls.ask` then saves
    the ask and waits, as it does in the instance. (The tests' own client is the whole app, where an ask is answered
    on the spot.)"""
    monkeypatch.setattr(program, "PROGRAM", "page")
    monkeypatch.setattr(settings, "owner", OWNER)
