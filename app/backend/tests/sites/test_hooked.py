"""A job system read by a hook (`adapters/hooked.py`): the hook is a module with a `Reader`, and the app asks the
reader the runbook makes of it. Each test below is for what the app is promised of a hook, with a made-up module and a
made-up runbook (`hooks.example.test`): the runbook's facts are the reader's, its reads are given a `Ctx` where they
were given a fetcher, and a `Ctx` goes only to the runbook's own hosts. Nothing here names a real site."""

import types

import httpx
import pytest

from app.jobsearch.adapters import Adapter, AdapterError, Listing
from app.jobsearch.adapters.hooked import Ctx, adapter_for
from app.jobsearch.fetch.polite import Fetcher
from app.sites.schema import Runbook


class Reader(Adapter):
    how = "pages"
    filters = True

    def parse_item(self, r, board):
        return None

    async def read(self, fetch, board):
        reply = await fetch.get(f"https://{board['host']}/jobs", headers={"X-Test": "1"})
        return Listing([{"ext_id": reply.text}], truncated=fetch.time_up())

    async def detail(self, fetch, board, posting):
        reply = await fetch.post(posting["url"], json={})
        return {"text": reply.text, "name": self.name}


def made(**more) -> Runbook:
    return Runbook.model_validate({
        "schema": 1, "key": "example_hook", "name": "Example Hook", "kind": "job_system",
        "hosts": ["hooks.example.test"], "order": 90, "reader": "example_hook",
        "flags": {"needs": ["host"], "customer_host": True, "drawn": True, "places": True, "probe_names": False},
        **more})


def fetcher(seen: list[httpx.Request]) -> Fetcher:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(request)
        return httpx.Response(200, text=request.url.host)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)


def reader(**more) -> Adapter:
    return adapter_for(made(**more), types.SimpleNamespace(Reader=Reader))


def test_the_runbooks_facts_are_the_readers_and_what_the_hook_says_of_itself_stays_its_own():
    got = reader()
    assert isinstance(got, Reader) and isinstance(got, Adapter)
    assert (got.key, got.name, got.domains, got.needs, got.customer_host, got.drawn, got.places, got.probe_names,
            got.own_parts, got.why_not) == (
        "example_hook", "Example Hook", ("hooks.example.test",), ("host",), True, True, True, False, None, None)
    assert (got.how, got.filters) == ("pages", True)


async def test_a_read_is_given_a_ctx_that_goes_through_the_fetcher_it_was_asked_with():
    seen: list[httpx.Request] = []
    got = await reader().read(fetcher(seen), {"host": "x.hooks.example.test"})
    assert [p["ext_id"] for p in got.postings] == ["x.hooks.example.test"] and seen[0].headers["x-test"] == "1"
    assert got.truncated is False


async def test_a_ctx_goes_only_to_the_runbooks_own_hosts_and_a_feed_host_the_runbook_adds_is_one_of_them():
    seen: list[httpx.Request] = []
    for host in ("evil.example.test", "hooks.example.test.evil.test", "example.test"):
        with pytest.raises(AdapterError, match="read only from its own hosts"):
            await reader().read(fetcher(seen), {"host": host})
    assert seen == []
    posting = {"url": "https://feeds.example.test/x"}
    with pytest.raises(AdapterError, match="own hosts"):
        await reader().detail(fetcher(seen), {}, posting)
    got = await reader(read_hosts=["feeds.example.test"]).detail(fetcher(seen), {}, posting)
    assert got == {"text": "feeds.example.test", "name": "Example Hook"} and seen[0].method == "POST"


def test_a_ctx_is_not_wrapped_again_and_says_when_the_time_has_run_out():
    fetch = fetcher([])
    ctx = Ctx(fetch, ("hooks.example.test",), "Example Hook")
    assert Ctx.of(ctx, ("other.example.test",), "Other") is ctx
    assert Ctx.of(fetch, ("hooks.example.test",), "Example Hook").deadline is None and not ctx.time_up()
    fetch.deadline = 1.0  # long ago
    assert ctx.time_up() and ctx.deadline == 1.0


def test_a_reader_leaves_alone_what_it_takes_from_the_base_so_the_base_asks_the_readers_own():
    assert type(reader()).probe is Adapter.probe and type(reader()).read is not Adapter.read


def test_read_hosts_go_only_with_a_reader():
    with pytest.raises(ValueError, match="read_hosts go with a reader"):
        Runbook.model_validate({"schema": 1, "key": "x", "name": "X", "kind": "board", "hosts": ["x.example.test"],
                                "read_hosts": ["y.example.test"]})
