"""The TLS context is built once per process, however many transports are made (`jobsearch/fetch/polite.py`)."""

import httpx
import pytest

from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, GuardedTransport, NotPublic


@pytest.mark.asyncio
async def test_two_transports_build_the_ssl_context_once(monkeypatch):
    polite._ssl_context.cache_clear()
    calls = []
    real = httpx.create_ssl_context
    monkeypatch.setattr(httpx, "create_ssl_context", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = GuardedTransport()
    second = GuardedTransport()
    Fetcher(httpx.AsyncClient(transport=first))
    Fetcher(httpx.AsyncClient(transport=second))
    assert len(calls) == 1
    polite._ssl_context.cache_clear()


@pytest.mark.asyncio
async def test_the_shared_context_transport_still_blocks_a_private_address():
    fetch = Fetcher(httpx.AsyncClient(transport=GuardedTransport()), per_host_seconds=0)
    with pytest.raises(NotPublic):
        await fetch.get("http://127.0.0.1/x")
