"""A board's rows are parsed in a loop with no wait of its own; it gives the event loop back as it goes
(a large board once held the loop for a noticeable time). Rows and their order stay the same."""

import asyncio
import time

import pytest

from app.jobsearch.adapters.feeds import adapter_for
from app.jobsearch.postings.coop import EVERY
from app.sites.schema import Runbook

ROWS = 500
PARA = "<p>" + "Build things and ship them with the team, every day. " * 20 + "</p>"
HTML = PARA * (30_000 // len(PARA))


class _Reply:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Fetch:
    """Answers with a body already decoded, so the only work left in the read is parsing the rows."""

    def __init__(self, body):
        self.body = body

    async def get(self, url, **kw):
        return _Reply(self.body)


#: A job system that is only a runbook, on a made-up board.
EXAMPLE = adapter_for(Runbook.model_validate({
    "schema": 1, "key": "example_feed", "name": "Example Feed", "kind": "job_system", "hosts": ["example.test"],
    "order": 90, "read": {
        "kind": "request", "url": "https://api.example.test/{slug}/roles", "items": "roles",
        "fields": {"ext_id": "id", "title": "title", "location": ["location.name", "location"],
                   "url": "link", "text": {"html": "content"}, "department": "departments.0.name"},
        "work_mode": {}}}))


def _example():
    return {"roles": [{"id": i, "title": f"Engineer {i}", "location": {"name": "Remote"}, "content": HTML,
                       "link": f"https://example.test/jobs/{i}", "departments": [{"name": "Eng"}]}
                      for i in range(1, ROWS + 1)]}


@pytest.mark.parametrize("adapter,body", [(EXAMPLE, _example)], ids=["feed"])
async def test_a_boards_rows_are_parsed_while_the_loop_keeps_running(adapter, body):
    board, data = {"slug": "acme"}, body()
    ticks = 0
    stop = False

    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    before = ticks
    began = time.perf_counter()
    listing = await adapter.read(_Fetch(data), board)
    took = time.perf_counter() - began
    turns = ticks - before
    stop = True
    await task

    # The reader gives the loop back after every EVERY seconds of parsing (plus the one row in hand); the ticker gets
    # one turn per give-back. Counting turns, not timing a gap, holds on any timer. A reader that never gave the
    # loop back leaves 0 turns; the bound allows ten times EVERY between give-backs.
    assert turns >= 1
    assert turns >= int(took / (10 * EVERY))
    rows = data["roles"] if isinstance(data, dict) else data
    expected = [p for p in (adapter.parse_item(r, board) for r in rows) if p is not None]
    assert len(expected) == ROWS
    assert [p for p in listing.postings] == expected
