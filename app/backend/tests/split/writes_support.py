"""What the tests of the page writes that ask the scout share: the page app called over ASGI, a stand-in for the engine
that keeps what it was told, and made-up ids of the right shape. Everything here is made up."""

import contextlib
from collections.abc import Iterator

from httpx import ASGITransport, AsyncClient, Response

from app import db
from app.jobsearch import page
from app.jobsearch.factory import build_app
from app.jobsearch.work import engine_calls
from tests.split.test_route_programs import _keys

#: Ids of the right shape that no row has.
ID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
THIRD = "33333333-3333-4333-8333-333333333333"


class Told:
    """Stands in for the engine (`engine_calls.Backend`): it keeps what it was told and does nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def knock(self, run_id: str, reason: str) -> bool:
        db.outside_transaction(f"a knock ({reason})")  # as the real one does: never inside a transaction
        self.calls.append(("knock", run_id, reason))
        return True

    async def dispatch_now(self) -> int:
        self.calls.append(("dispatch_now",))
        return 0

    async def knocks(self, pending: list) -> None:
        self.calls.append(("knocks", list(pending)))

    async def send_wakes(self, wakes) -> list[str]:
        self.calls.append(("send_wakes",))
        return []

    def nudge(self) -> None:
        self.calls.append(("nudge",))

    def start(self, name: str, key) -> None:
        self.calls.append(("start", name))


@contextlib.contextmanager
def engine_told() -> Iterator[Told]:
    """The engine door asks a `Told` for the length of the block; the door it had comes back after."""
    stand_in = Told()
    before = engine_calls._backend
    engine_calls.install(stand_in)
    try:
        yield stand_in
    finally:
        engine_calls.install(before)


def knocked(told: Told) -> list[tuple]:
    """Everything the page asked the engine for, but the nudges (a hint to look that needs no answer)."""
    return [call for call in told.calls if call[0] != "nudge"]


async def send(method: str, url: str, *, app=None, **request) -> Response:
    """One request straight on an ASGI app (the page's, unless `app` is given), without its lifespan."""
    async with AsyncClient(transport=ASGITransport(app=app or page.app), base_url="http://test") as http:
        return await http.request(method, url, **request)


def on_programs(rows: set[str]) -> tuple[list[str], list[str]]:
    """Of the manifest rows `rows` ("METHOD /path"): the ones the page app does not serve, and the ones the engine
    app still serves. A flipped row is in neither list."""
    page_keys, engine_keys = (set(_keys(build_app(role))) for role in ("page", "engine"))
    return sorted(rows - page_keys), sorted(rows & engine_keys)
