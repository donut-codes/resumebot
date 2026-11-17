"""The employer's "counting since" day: the owner's calendar day of the first posting seen,
not its UTC date. Adapters are stand-ins; no network. Synthetic data only."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app import db
from app.jobsearch import adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.postings import index as postings
from tests.support import EXAMPLE_SYSTEM, OWNER


@dataclass
class _Listing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class _Adapter:
    answers: list = field(default_factory=list)

    async def read(self, fetch, board):
        return self.answers.pop(0)

    async def detail(self, fetch, board, posting):
        return {}


def _posting(ext_id: str) -> dict:
    return {"ext_id": ext_id, "title": "Programs Lead", "location": "Remote", "work_mode": "remote", "comp": None,
            "comp_min": None, "comp_max": None, "posted": "2026-10-01", "url": f"https://jobs.example.test/{ext_id}",
            "apply_url": None, "text": "About the role.", "department": None}


async def _since(monkeypatch, zone: str, read_at: datetime) -> str | None:
    monkeypatch.setattr("app.timeutil.settings.timezone", zone)
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: _Adapter([_Listing([_posting("1")])])})
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)
    async with db.session_factory()() as session:
        acme = await employers.find_or_make(session, OWNER, "Acme")
        acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": "acme"}
        await session.flush()
        await postings.refresh(session, OWNER, fetch, now=read_at)
        return (await employers.hiring(session, acme))["since"]


async def test_counting_since_is_the_owners_day_of_the_first_posting_west_of_utc(js, monkeypatch):
    # 05:00 UTC on 3 October is still 2 October for an owner 11 hours behind.
    assert await _since(monkeypatch, "Pacific/Pago_Pago", datetime(2026, 10, 3, 5, 0, tzinfo=UTC)) == "2026-10-02"


async def test_counting_since_is_the_owners_day_of_the_first_posting_east_of_utc(js, monkeypatch):
    # 15:00 UTC on 3 October is already 4 October for an owner 14 hours ahead.
    assert await _since(monkeypatch, "Pacific/Kiritimati", datetime(2026, 10, 3, 15, 0, tzinfo=UTC)) == "2026-10-04"
