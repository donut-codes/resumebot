"""The one fetcher every read of another site goes through (`jobsearch/fetch/polite.py`): only public addresses (written
as
numbers in any form, or named so they resolve to a private one), redirects followed one checked hop at a time,
robots.txt read with its wildcards and Crawl-delay, and an answer's size and time capped. No network: a mock transport
answers, and a stub resolver stands in for DNS. Synthetic data only."""

import asyncio
import gzip
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import (
    Fetcher,
    GuardedBackend,
    GuardedTransport,
    NotPublic,
    Refused,
    Robots,
    RobotsRefused,
    TooLarge,
    public_url,
)

# --- the address, as written -------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://jobs.example.test/x?y=1", "http://93.184.216.34/x", "https://[2606:4700:4700::1111]/x",
    "https://xn--bcher-kva.example/jobs", "https://a-b.c-d.example/"])
def test_a_public_address_is_one(url):
    assert public_url(url)


@pytest.mark.parametrize("url", [
    None, "", 5, "jobs.example.test", "ftp://jobs.example.test/x", "http:///x", "file:///etc/passwd",
    "https://user:secret@jobs.example.test/x",
    # the plain forms
    "http://localhost/x", "http://localhost./x", "http://app.localhost/x", "http://127.0.0.1/x", "http://[::1]/x",
    "http://10.0.0.5/x", "http://192.168.1.5/x", "http://172.16.0.1/x", "http://0.0.0.0/x",
    "http://169.254.169.254/latest/meta-data", "http://[fe80::1]/x", "http://[fd00::1]/x",
    "http://intranet/x", "http://redis/x", "http://wiki.internal/x", "http://nas.local/x", "http://printer.lan/x",
    # a resolver reads these as the loopback address
    "http://127.1/x", "http://127.0.1/x", "http://0x7f.1/x", "http://0x7f.0.0.1/x", "http://0x7f000001/x",
    "http://2130706433/x", "http://017700000001/x", "http://0177.0.0.1/x", "http://127.0.0.1./x",
    # shared (carrier-grade NAT) and reserved
    "http://100.64.0.1/x", "http://100.127.255.254/x", "http://198.18.0.1/x", "http://240.0.0.1/x",
    "http://192.0.0.8/x", "http://224.0.0.1/x",
    # IPv6 that carries an IPv4 address inside
    "http://[::ffff:127.0.0.1]/x", "http://[::ffff:7f00:1]/x", "http://[::127.0.0.1]/x", "http://[::7f00:1]/x",
    "http://[64:ff9b::7f00:1]/x", "http://[2002:7f00:1::]/x", "http://[2002:a00:1::]/x",
    # not a host name
    "http://exa mple.com/x"])
def test_every_form_of_a_local_or_private_address_is_refused(url):
    assert not public_url(url)


def test_a_public_address_has_no_limit_on_its_length():
    assert public_url("https://jobs.example.test/" + "x" * 5000)


# --- the address, as resolved --------------------------------------------------------------------------------------


def _resolver(table: dict, seen: list | None = None):
    async def resolve(host: str, port: int) -> list[str]:
        if seen is not None:
            seen.append(host)
        if host not in table:
            raise OSError("name not found")
        return table[host]
    return resolve


@pytest.fixture
def connected(monkeypatch):
    """What the real connection is asked for (it is never made)."""
    asked: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        asked.append(host)
        return object()
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
    return asked


@pytest.mark.parametrize("answer", [
    ["127.0.0.1"], ["10.1.2.3"], ["169.254.169.254"], ["100.64.0.9"], ["::1"], ["::ffff:10.0.0.1"],
    ["93.184.216.34", "10.0.0.1"],  # one private address among public ones is enough to refuse
    ["10.0.0.1", "93.184.216.34"], []])
async def test_a_name_that_resolves_to_a_private_address_is_never_connected_to(connected, answer):
    backend = GuardedBackend(_resolver({"rebind.example": answer}))
    with pytest.raises(NotPublic):
        await backend.connect_tcp("rebind.example", 443)
    assert connected == []


async def test_a_name_that_does_not_resolve_is_a_connect_error_not_a_crash(connected):
    with pytest.raises(httpcore.ConnectError):
        await GuardedBackend(_resolver({})).connect_tcp("nowhere.example", 443)


async def test_the_connection_goes_to_the_address_that_was_checked_not_to_the_name(connected):
    seen: list[str] = []
    backend = GuardedBackend(_resolver({"jobs.example": ["93.184.216.34", "2606:4700:4700::1111"]}, seen))
    await backend.connect_tcp("jobs.example", 443)
    assert connected == ["93.184.216.34"] and seen == ["jobs.example"], "resolved once, connected to the address"


async def test_the_fetcher_refuses_a_name_that_points_inside_and_never_reaches_it(connected):
    seen: list[str] = []
    transport = GuardedTransport(_resolver({"127.0.0.1.nip.example": ["127.0.0.1"], "ok.example": ["93.184.216.34"]},
                                           seen))
    fetch = Fetcher(httpx.AsyncClient(transport=transport), per_host_seconds=0)
    with pytest.raises(NotPublic):
        await fetch.get("http://127.0.0.1.nip.example/jobs")
    assert connected == []
    await fetch.close()


@pytest.mark.parametrize("status", [404, 410])
def test_a_page_or_a_job_system_that_answers_404_or_410_says_the_posting_is_gone(status):
    said = polite.gone("https://jobs.example.test/1", status)
    assert said is not None and "https://jobs.example.test/1" in said and str(status) in said


@pytest.mark.parametrize("status", [200, 301, 401, 403, 429, 500, 503, None])
def test_a_block_a_rate_limit_a_server_error_or_no_answer_never_says_the_posting_is_gone(status):
    assert polite.gone("https://jobs.example.test/1", status) is None


@pytest.fixture(autouse=True)
def reads_no_web():
    """This module tests the real factory (`polite.make`): the suite-wide stand-in (conftest's fixture of this name) is
    left off here."""


async def test_the_fetcher_the_night_builds_connects_only_through_the_guard():
    fetch = polite.make()
    assert isinstance(fetch.client._transport, GuardedTransport)
    assert isinstance(fetch.client._transport._pool._network_backend, GuardedBackend)
    assert fetch.client.follow_redirects is False
    await fetch.close()


async def test_a_number_written_as_a_host_is_refused_before_anything_is_resolved(connected):
    seen: list[str] = []
    fetch = Fetcher(httpx.AsyncClient(transport=GuardedTransport(_resolver({}, seen))), per_host_seconds=0)
    for url in ("http://127.1/", "http://0x7f.1/", "http://2130706433/", "http://[::ffff:7f00:1]/"):
        with pytest.raises(NotPublic):
            await fetch.get(url)
    assert seen == [] and connected == [] and fetch.requests == 0
    await fetch.close()


# --- redirects ---------------------------------------------------------------------------------------------------


def _site(routes: dict, seen: list | None = None):
    """A mock internet: `routes` maps (host, path) to a response or a function of the request; robots.txt is absent
    unless routed."""
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        route = routes.get((request.url.host, request.url.path))
        if route is None:
            return httpx.Response(404)
        if callable(route):
            return route(request)
        return httpx.Response(route.status_code, headers=route.headers, content=route.content)  # a fresh one each time
    return handle


def _fetcher(handle) -> Fetcher:
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)


def _to(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


async def test_a_redirect_is_followed_and_the_answer_remembers_the_way_it_came():
    fetch = _fetcher(_site({("a.example", "/jobs"): _to("https://b.example/list"),
                            ("b.example", "/list"): httpx.Response(200, text="the list")}))
    reply = await fetch.get("https://a.example/jobs")
    assert reply.text == "the list" and str(reply.url) == "https://b.example/list"
    assert [str(r.url) for r in reply.history] == ["https://a.example/jobs"]


@pytest.mark.parametrize("where", [
    "http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data", "http://localhost:8430/api",
    "http://0x7f.1/", "http://2130706433/", "https://10.0.0.5/", "http://[::1]/", "http://intranet/x"])
async def test_a_redirect_to_a_private_address_is_refused_without_a_request_to_it(where):
    seen: list = []
    fetch = _fetcher(_site({("a.example", "/jobs"): _to(where)}, seen))
    with pytest.raises(NotPublic):
        await fetch.get("https://a.example/jobs")
    assert {r.url.host for r in seen} == {"a.example"}


async def test_a_redirect_to_another_host_is_read_against_that_hosts_robots_txt():
    seen: list = []
    fetch = _fetcher(_site({
        ("a.example", "/jobs"): _to("https://b.example/private/list"),
        ("b.example", "/robots.txt"): httpx.Response(200, text="User-agent: *\nDisallow: /private/"),
        ("b.example", "/private/list"): httpx.Response(200, text="not for us")}, seen))
    with pytest.raises(RobotsRefused, match="b.example"):
        await fetch.get("https://a.example/jobs")
    assert "/private/list" not in [r.url.path for r in seen], "the page itself was never asked for"


async def test_a_redirect_to_a_host_that_refused_us_earlier_is_a_stop():
    fetch = _fetcher(_site({
        ("b.example", "/blocked"): httpx.Response(403),
        ("a.example", "/jobs"): _to("https://b.example/list"),
        ("b.example", "/list"): httpx.Response(200, text="ok")}))
    with pytest.raises(Refused, match="403"):
        await fetch.get("https://b.example/blocked")
    with pytest.raises(Refused, match="earlier request"):
        await fetch.get("https://a.example/jobs")


async def test_each_hop_is_counted_and_paced_like_a_first_request():
    fetch = _fetcher(_site({("a.example", "/jobs"): _to("https://b.example/list"),
                            ("b.example", "/list"): httpx.Response(200, text="x")}))
    await fetch.get("https://a.example/jobs")
    assert fetch.requests == 4, "robots.txt and the page, on each of the two hosts"
    assert set(fetch._last) == {"a.example", "b.example"}


async def test_a_redirect_from_https_to_http_is_refused():
    seen: list = []
    fetch = _fetcher(_site({("a.example", "/jobs"): _to("http://a.example/jobs"),
                            ("a.example", "/jobs2"): httpx.Response(200)}, seen))
    with pytest.raises(Refused, match="https to http"):
        await fetch.get("https://a.example/jobs")


async def test_a_redirect_from_http_to_https_is_followed():
    def moved(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return _to("https://a.example/jobs", 301)
        return httpx.Response(200, text="secure")
    fetch = _fetcher(_site({("a.example", "/jobs"): moved}))
    assert (await fetch.get("http://a.example/jobs")).text == "secure"


async def test_a_chain_of_more_than_five_redirects_is_given_up():
    pages: list = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        pages.append(request)
        return _to(f"https://a.example/{len(pages)}")
    with pytest.raises(httpx.TooManyRedirects):
        await _fetcher(handle).get("https://a.example/start")
    assert len(pages) == polite.MAX_HOPS + 1


async def test_a_redirect_that_loops_back_is_given_up_too():
    fetch = _fetcher(_site({("a.example", "/x"): _to("https://a.example/y"), ("a.example", "/y"): _to("/x")}))
    with pytest.raises(httpx.TooManyRedirects):
        await fetch.get("https://a.example/x")


@pytest.mark.parametrize(("status", "method_after", "body_kept"), [
    (301, "GET", False), (302, "GET", False), (303, "GET", False), (307, "POST", True), (308, "POST", True)])
async def test_a_redirect_changes_a_post_the_way_a_browser_does(status, method_after, body_kept):
    seen: list = []
    fetch = _fetcher(_site({("a.example", "/search"): _to("https://a.example/results", status),
                            ("a.example", "/results"): httpx.Response(200, text="ok")}, seen))
    await fetch.post("https://a.example/search", json={"q": "x"})
    last = [r for r in seen if r.url.path == "/results"][0]
    assert last.method == method_after and bool(last.content) is body_kept


async def test_a_sign_in_header_does_not_follow_a_redirect_to_another_host():
    seen: list = []
    fetch = _fetcher(_site({("a.example", "/jobs"): _to("https://b.example/list"),
                            ("b.example", "/list"): httpx.Response(200, text="x")}, seen))
    await fetch.get("https://a.example/jobs", headers={"Authorization": "Bearer t", "X-Other": "1"})
    first, last = [r for r in seen if r.url.path in ("/jobs", "/list")]
    assert first.headers["authorization"] == "Bearer t"
    assert "authorization" not in last.headers and last.headers["x-other"] == "1"
    assert last.headers["user-agent"] == polite.USER_AGENT


async def test_a_page_called_with_a_query_does_not_carry_it_to_the_page_it_redirects_to():
    seen: list = []
    fetch = _fetcher(_site({("a.example", "/jobs"): _to("/moved"), ("a.example", "/moved"): httpx.Response(200)}, seen))
    await fetch.get("https://a.example/jobs", params={"page": 2})
    assert [str(r.url) for r in seen if r.url.path != "/robots.txt"] == ["https://a.example/jobs?page=2",
                                                                          "https://a.example/moved"]


# --- robots.txt --------------------------------------------------------------------------------------------------


def _allows(text: str, path: str) -> bool:
    return Robots(text).allows(f"https://a.example{path}")


def test_a_robots_rule_is_a_prefix():
    assert not _allows("User-agent: *\nDisallow: /private", "/private/x")
    assert not _allows("User-agent: *\nDisallow: /private", "/privateer")
    assert _allows("User-agent: *\nDisallow: /private", "/public")
    assert _allows("User-agent: *\nDisallow:", "/anything"), "an empty Disallow allows everything"
    assert not _allows("User-agent: *\nDisallow: /", "/")


def test_a_robots_star_matches_any_run_of_characters():
    rules = "User-agent: *\nDisallow: /*/private\nDisallow: /*?session=\nDisallow: /a*b*c"
    assert not _allows(rules, "/team/private/x")
    assert not _allows(rules, "/team/sub/private")
    assert _allows(rules, "/team/public")
    assert not _allows(rules, "/jobs?session=abc")
    assert _allows(rules, "/jobs?page=2")
    assert not _allows(rules, "/aXXbYYcZZ") and _allows(rules, "/aXXcYYb")


def test_a_robots_dollar_matches_the_end_of_the_address():
    rules = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /exact$"
    assert not _allows(rules, "/files/cv.pdf")
    assert _allows(rules, "/files/cv.pdf?x=1") and _allows(rules, "/files/cv.pdfx")
    assert not _allows(rules, "/exact") and _allows(rules, "/exact/more")


def test_the_longest_matching_robots_rule_wins_and_allow_wins_a_tie():
    rules = "User-agent: *\nDisallow: /jobs\nAllow: /jobs/open\nDisallow: /jobs/open/internal\nAllow: /x\nDisallow: /x"
    assert not _allows(rules, "/jobs/list")
    assert _allows(rules, "/jobs/open/1")
    assert not _allows(rules, "/jobs/open/internal/1")
    assert _allows(rules, "/x")


def test_the_group_that_names_this_app_wins_over_the_star_group():
    text = ("User-agent: *\nDisallow: /\n\n"
            "User-agent: otherbot\nDisallow: /nothing\n\n"
            f"User-agent: {polite.USER_AGENT.split('/')[0].upper()}\nAllow: /\nDisallow: /secret")
    assert _allows(text, "/jobs") and not _allows(text, "/secret/x")
    assert not _allows("User-agent: otherbot\nAllow: /\n\nUser-agent: *\nDisallow: /", "/jobs")


def test_several_user_agent_lines_share_one_group_and_comments_and_case_are_ignored():
    text = "# robots\nUSER-AGENT: otherbot\nuser-agent: *  # everyone else\nDISALLOW: /no # not here\nAllow:/yes\n"
    assert not _allows(text, "/no/x") and _allows(text, "/yes")


def test_a_robots_crawl_delay_is_read_from_the_group_that_applies():
    assert Robots("User-agent: *\nCrawl-delay: 2.5\nDisallow: /x").delay == 2.5
    assert Robots("User-agent: *\nCrawl-delay: soon").delay is None
    assert Robots("User-agent: *\nCrawl-delay: -4").delay is None
    assert Robots(f"User-agent: *\nCrawl-delay: 9\n\nUser-agent: {polite.PRODUCT_TOKEN}\nCrawl-delay: 3").delay == 3
    assert Robots("").delay is None and Robots("").allows("https://a.example/x")


@pytest.fixture
def slept(monkeypatch):
    """The waits asked for (nothing really waits)."""
    waits: list[float] = []

    async def sleep(seconds, *args):
        waits.append(seconds)
    monkeypatch.setattr(polite.asyncio, "sleep", sleep)
    return waits


async def test_a_crawl_delay_sets_the_pace_for_that_host(slept):
    fetch = _fetcher(_site({("a.example", "/robots.txt"): httpx.Response(200, text="User-agent: *\nCrawl-delay: 4"),
                            ("a.example", "/one"): httpx.Response(200), ("a.example", "/two"): httpx.Response(200),
                            ("b.example", "/one"): httpx.Response(200), ("b.example", "/two"): httpx.Response(200)}))
    await fetch.get("https://a.example/one")
    await fetch.get("https://a.example/two")
    assert any(3 < w <= 4 for w in slept), slept
    slept.clear()
    await fetch.get("https://b.example/one")
    await fetch.get("https://b.example/two")
    assert not any(w > 0.5 for w in slept), "another host is not held to it"


async def test_a_crawl_delay_longer_than_the_app_waits_leaves_the_host_alone():
    seen: list = []
    fetch = _fetcher(_site({("a.example", "/robots.txt"): httpx.Response(200, text="User-agent: *\nCrawl-delay: 600"),
                            ("a.example", "/one"): httpx.Response(200)}, seen))
    with pytest.raises(RobotsRefused, match="more time between requests") as caught:
        await fetch.get("https://a.example/one")
    assert not caught.value.unreachable and "/one" not in [r.url.path for r in seen]


async def test_a_wildcard_disallow_stops_the_read():
    fetch = _fetcher(_site({("a.example", "/robots.txt"): httpx.Response(200, text="User-agent: *\nDisallow: /*/apply"),
                            ("a.example", "/jobs/1/apply"): httpx.Response(200),
                            ("a.example", "/jobs/1"): httpx.Response(200)}))
    assert (await fetch.get("https://a.example/jobs/1")).status_code == 200
    with pytest.raises(RobotsRefused, match="asks not to read"):
        await fetch.get("https://a.example/jobs/1/apply")


async def test_robots_txt_that_cannot_be_had_is_told_apart_from_robots_txt_that_says_no():
    for status, unreachable, reads in ((404, None, True), (410, None, True), (500, True, False), (503, True, False)):
        fetch = _fetcher(_site({("a.example", "/robots.txt"): httpx.Response(status),
                                ("a.example", "/jobs"): httpx.Response(200, text="ok")}))
        if reads:
            assert (await fetch.get("https://a.example/jobs")).text == "ok"
        else:
            with pytest.raises(RobotsRefused) as caught:
                await fetch.get("https://a.example/jobs")
            assert caught.value.unreachable is unreachable


# --- a robots.txt that redirects (RFC 9309, 2.3.1.2) ------------------------------------------------------------------


async def test_a_robots_txt_that_redirects_to_plain_http_is_read_and_its_rules_apply_to_the_host_asked_about():
    """A site that sends its robots.txt to a script on plain http is not unreachable: its rules are followed."""
    seen: list = []
    fetch = _fetcher(_site({
        ("a.example", "/robots.txt"): _to("http://a.example/seo?target=robots"),
        ("a.example", "/seo"): httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
        ("a.example", "/jobs"): httpx.Response(200, text="ok"),
        ("a.example", "/private/x"): httpx.Response(200, text="not for us")}, seen))
    assert (await fetch.get("https://a.example/jobs")).text == "ok"
    with pytest.raises(RobotsRefused, match="asks not to read /private/x") as caught:
        await fetch.get("https://a.example/private/x")
    assert not caught.value.unreachable, "the rules were had, and they say no"
    assert [(r.url.scheme, r.url.path) for r in seen[:2]] == [("https", "/robots.txt"), ("http", "/seo")]
    assert "/private/x" not in [r.url.path for r in seen]


async def test_a_page_still_never_redirects_from_https_to_http_though_its_robots_txt_may():
    fetch = _fetcher(_site({
        ("a.example", "/robots.txt"): _to("http://a.example/seo?target=robots"),
        ("a.example", "/seo"): httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
        ("a.example", "/jobs"): _to("http://a.example/plain-jobs"),
        ("a.example", "/plain-jobs"): httpx.Response(200, text="plain")}))
    with pytest.raises(Refused, match="https to http"):
        await fetch.get("https://a.example/jobs")


async def test_a_robots_txt_on_another_host_holds_the_rules_of_the_host_that_sent_us_there():
    seen: list = []
    fetch = _fetcher(_site({
        ("a.example", "/robots.txt"): _to("https://b.example/shared-robots.txt", 301),
        ("b.example", "/shared-robots.txt"): httpx.Response(200, text="User-agent: *\nDisallow: /jobs/secret\n"),
        ("a.example", "/jobs/open"): httpx.Response(200, text="ok"),
        ("a.example", "/jobs/secret"): httpx.Response(200, text="no")}, seen))
    assert (await fetch.get("https://a.example/jobs/open")).text == "ok"
    with pytest.raises(RobotsRefused, match="a.example asks not to read /jobs/secret"):
        await fetch.get("https://a.example/jobs/secret")
    assert {r.url.host for r in seen} == {"a.example", "b.example"}
    assert set(fetch._last) == {"a.example", "b.example"}, "each hop is paced on its own host"


@pytest.mark.parametrize(("redirects", "reads"), [(1, True), (5, True), (6, False)])
async def test_five_redirects_of_a_robots_txt_are_followed_and_a_longer_chain_leaves_the_host_alone(redirects, reads):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt" or path.startswith("/r"):
            n = 0 if path == "/robots.txt" else int(path[2:])
            return _to(f"/r{n + 1}" if n + 1 < redirects else "/final")
        if path == "/final":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="ok")
    fetch = _fetcher(handle)
    if reads:
        assert (await fetch.get("https://a.example/jobs")).text == "ok"
        with pytest.raises(RobotsRefused, match="asks not to read /private") as caught:
            await fetch.get("https://a.example/private")
        assert not caught.value.unreachable
    else:
        with pytest.raises(RobotsRefused) as caught:
            await fetch.get("https://a.example/jobs")
        assert caught.value.unreachable, "more than five redirects: the rules could not be had"


async def test_a_robots_txt_that_redirects_to_a_private_address_is_never_followed_there():
    seen: list = []
    fetch = _fetcher(_site({("a.example", "/robots.txt"): _to("http://127.0.0.1/robots.txt")}, seen))
    with pytest.raises(NotPublic):
        await fetch.get("https://a.example/jobs")
    assert {r.url.host for r in seen} == {"a.example"}


@pytest.mark.parametrize(("status", "reads"), [(404, True), (410, True), (500, False), (503, False)])
async def test_a_redirected_robots_txt_that_ends_in_an_error_is_told_as_one_that_ends_in_it_without_a_redirect(
        status, reads):
    fetch = _fetcher(_site({("a.example", "/robots.txt"): _to("http://a.example/seo?target=robots"),
                            ("a.example", "/seo"): httpx.Response(status),
                            ("a.example", "/jobs"): httpx.Response(200, text="ok")}))
    if reads:
        assert (await fetch.get("https://a.example/jobs")).text == "ok"
    else:
        with pytest.raises(RobotsRefused) as caught:
            await fetch.get("https://a.example/jobs")
        assert caught.value.unreachable


# --- size and time -----------------------------------------------------------------------------------------------


async def test_an_answer_bigger_than_the_cap_is_given_up(monkeypatch):
    monkeypatch.setattr(polite, "MAX_BYTES", 10_000)
    fetch = _fetcher(_site({("a.example", "/big"): httpx.Response(200, content=b"x" * 10_001),
                            ("a.example", "/ok"): httpx.Response(200, content=b"x" * 10_000)}))
    with pytest.raises(TooLarge, match="a.example"):
        await fetch.get("https://a.example/big")
    assert len((await fetch.get("https://a.example/ok")).content) == 10_000
    assert isinstance(TooLarge("x"), httpx.HTTPError), "the readers already treat it as a network fault"


def test_the_cap_holds_a_big_boards_whole_job_feed():
    # A job board's list with every description can run to ten megabytes. A cap that refused it left that board unread,
    # so the cap is held above what was seen with room to spare.
    assert polite.MAX_BYTES >= 20_000_000


def _compressed(body: bytes, kind: str = "text/plain") -> httpx.Response:
    """An answer still compressed on the wire (its body is decoded as it is read, as a real one is)."""
    return httpx.Response(200, headers={"Content-Encoding": "gzip", "Content-Type": kind},
                          stream=httpx.ByteStream(body))


async def test_a_compressed_answer_is_capped_once_decoded(monkeypatch):
    monkeypatch.setattr(polite, "MAX_BYTES", 100_000)
    bomb = gzip.compress(b"0" * 5_000_000)
    assert len(bomb) < 20_000
    fetch = _fetcher(_site({("a.example", "/bomb"): lambda r: _compressed(bomb)}))
    with pytest.raises(TooLarge):
        await fetch.get("https://a.example/bomb")


async def test_a_compressed_answer_under_the_cap_comes_back_decoded():
    body = gzip.compress(b'{"jobs": [1, 2]}')
    fetch = _fetcher(_site({("a.example", "/ok"): lambda r: _compressed(body, "application/json")}))
    reply = await fetch.get("https://a.example/ok")
    assert reply.json() == {"jobs": [1, 2]} and reply.headers["content-type"] == "application/json"


async def test_a_robots_txt_bigger_than_its_cap_is_unreadable_not_unlimited(monkeypatch):
    monkeypatch.setattr(polite, "MAX_ROBOTS_BYTES", 1_000)
    fetch = _fetcher(_site({("a.example", "/robots.txt"): httpx.Response(200, text="# x\n" * 1_000),
                            ("a.example", "/jobs"): httpx.Response(200)}))
    with pytest.raises(RobotsRefused) as caught:
        await fetch.get("https://a.example/jobs")
    assert caught.value.unreachable


async def test_a_site_that_answers_slowly_is_given_up_after_the_time_cap(monkeypatch):
    monkeypatch.setattr(polite, "REQUEST_SECONDS", 0.2)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)
    fetch = _fetcher(lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else slow(r))
    with pytest.raises(httpx.ReadTimeout, match="took longer"):
        await fetch.get("https://a.example/slow")


async def test_a_site_that_drips_its_answer_is_given_up_after_the_time_cap(monkeypatch):
    monkeypatch.setattr(polite, "REQUEST_SECONDS", 0.3)

    async def drip():
        for _ in range(100):
            await asyncio.sleep(0.1)
            yield b"x"

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, content=drip())
    with pytest.raises(httpx.ReadTimeout):
        await _fetcher(handle).get("https://a.example/drip")


# --- pace, patience and a refusal --------------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch):
    """A clock the fetcher reads and a sleep that moves it, so a wait costs nothing and shows what was waited. Only the
    fetcher's own `time` is replaced: the event loop keeps the real one."""
    now = SimpleNamespace(value=1000.0, slept=[])

    async def sleep(seconds, *args):
        now.slept.append(seconds)
        now.value += seconds
    monkeypatch.setattr(polite.asyncio, "sleep", sleep)
    monkeypatch.setattr(polite, "time", SimpleNamespace(monotonic=lambda: now.value))
    return now


async def test_a_host_is_asked_no_more_than_once_a_second_and_another_host_does_not_wait(clock):
    asked: list[tuple[str, str, float]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.host, request.url.path, clock.value))
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200)
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert fetch.per_host_seconds == 1.0, "the default is a request a second"
    for url in ("https://a.example/one", "https://a.example/two", "https://b.example/one"):
        await fetch.get(url)
    at = {(host, path): when for host, path, when in asked}
    assert at[("a.example", "/one")] - at[("a.example", "/robots.txt")] == 1.0
    assert at[("a.example", "/two")] - at[("a.example", "/one")] == 1.0
    assert at[("b.example", "/robots.txt")] == at[("a.example", "/two")], "the first ask of a new host does not wait"
    assert at[("b.example", "/one")] - at[("b.example", "/robots.txt")] == 1.0


async def test_only_what_is_left_of_the_second_is_waited(clock):
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(200))))
    await fetch.get("https://a.example/one")
    clock.value += 0.4  # the code between the two asks spent part of the second
    await fetch.get("https://a.example/two")
    assert clock.slept == [1.0, pytest.approx(0.6)]


def _answers(*replies):
    """A route that gives each ask the next of `replies` (the last one again after that), and counts the asks."""
    asked = []

    def route(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        reply = replies[min(len(asked), len(replies)) - 1]
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)
    route.asked = asked
    return route


def _too_many(retry_after: str | None = None) -> httpx.Response:
    return httpx.Response(429, headers={} if retry_after is None else {"Retry-After": retry_after})


@pytest.mark.parametrize("header, waited", [("7", 7.0), ("60", 60.0), (None, 5.0), ("soon", 5.0), ("0", 0.0)])
async def test_too_many_requests_is_waited_out_once_for_as_long_as_the_site_says_and_then_asked_again(
        clock, header, waited):
    route = _answers(_too_many(header), httpx.Response(200, text="through"))
    fetch = _fetcher(_site({("a.example", "/jobs"): route}))
    reply = await fetch.get("https://a.example/jobs")
    assert (reply.status_code, reply.text) == (200, "through") and len(route.asked) == 2
    assert clock.slept == [waited]


async def test_a_wait_longer_than_a_minute_is_not_taken_and_the_answer_comes_back_at_once(clock):
    route = _answers(_too_many("61"), httpx.Response(200))
    fetch = _fetcher(_site({("a.example", "/jobs"): route}))
    assert (await fetch.get("https://a.example/jobs")).status_code == 429
    assert len(route.asked) == 1 and clock.slept == []


async def test_a_quick_test_of_a_site_does_not_wait_out_too_many_requests(clock):
    route = _answers(_too_many("3"), httpx.Response(200))
    fetch = _fetcher(_site({("a.example", "/jobs"): route}))
    assert (await fetch.get("https://a.example/jobs", patient=False)).status_code == 429
    assert len(route.asked) == 1 and clock.slept == []


async def test_a_second_too_many_requests_is_the_answer_and_the_site_is_not_asked_a_third_time(clock):
    route = _answers(_too_many("2"), _too_many("2"), httpx.Response(200))
    fetch = _fetcher(_site({("a.example", "/jobs"): route}))
    assert (await fetch.get("https://a.example/jobs")).status_code == 429
    assert len(route.asked) == 2 and clock.slept == [2.0]


async def test_a_403_stops_the_host_for_the_rest_of_the_run_and_no_other_host(clock):
    seen: list = []
    fetch = _fetcher(_site({("a.example", "/jobs"): httpx.Response(403),
                            ("a.example", "/other"): httpx.Response(200),
                            ("b.example", "/jobs"): httpx.Response(200, text="fine")}, seen))
    with pytest.raises(Refused, match="refused the request .403."):
        await fetch.get("https://a.example/jobs")
    asked_of_a = len([r for r in seen if r.url.host == "a.example"])
    with pytest.raises(Refused, match="earlier request"):
        await fetch.get("https://a.example/other")
    assert len([r for r in seen if r.url.host == "a.example"]) == asked_of_a, "nothing more was sent to it"
    assert (await fetch.get("https://b.example/jobs")).text == "fine"


async def test_every_ask_says_who_is_asking_robots_txt_included():
    seen: list = []
    fetch = _fetcher(_site({("a.example", "/jobs"): httpx.Response(200)}, seen))
    await fetch.get("https://a.example/jobs")
    assert [r.url.path for r in seen] == ["/robots.txt", "/jobs"]
    assert {r.headers["user-agent"] for r in seen} == {polite.USER_AGENT}
    assert polite.USER_AGENT.startswith(f"{polite.APP_NAME}/") and "reads public job listings only" in polite.USER_AGENT


# --- how many requests at once (docs/PRD.md 28.17) -------------------------------------------------------------------


class Open:
    """A site that takes a moment to answer, and what it saw: the most requests open at once to one host and to one
    platform (`polite.platform_of`), and the order the requests began and ended in."""

    def __init__(self, delay: float = 0.03, slow: tuple[str, ...] = (), fail: tuple[str, ...] = ()) -> None:
        self.delay, self.slow, self.fail = delay, slow, fail
        self.open_host: dict[str, int] = {}
        self.open_platform: dict[str, int] = {}
        self.host_peak: dict[str, int] = {}
        self.platform_peak: dict[str, int] = {}
        self.events: list[tuple[str, str]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        host = request.url.host
        platform = polite.platform_of(host)
        self.events.append(("began", host))
        for key, open_now, peaks in ((host, self.open_host, self.host_peak),
                                     (platform, self.open_platform, self.platform_peak)):
            open_now[key] = open_now.get(key, 0) + 1
            peaks[key] = max(peaks.get(key, 0), open_now[key])
        try:
            await asyncio.sleep(self.delay * (4 if host in self.slow else 1))
            if host in self.fail:
                raise httpx.ConnectError("the connection dropped", request=request)
            return httpx.Response(200, text="answered")
        finally:
            self.events.append(("ended", host))
            self.open_host[host] -= 1
            self.open_platform[platform] -= 1


def _fast(site: Callable[[httpx.Request], Awaitable[httpx.Response]]) -> Fetcher:
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(site)), per_host_seconds=0)


@pytest.mark.parametrize("host, platform", [
    ("acme.wd5.tenantone.test", "tenantone.test"), ("beta.wd1.tenantone.test", "tenantone.test"),
    ("boards-api.feedone.test", "feedone.test"), ("careers-acme.pagesone.test", "pagesone.test"),
    ("jobs.example.co.uk", "example.co.uk"), ("careers.example.com.au", "example.com.au"),
    ("careers.example.test", "example.test"), ("example.test", "example.test"), ("JOBS.Example.Test.", "example.test"),
    ("localhost", "localhost"), ("93.184.216.34", "93.184.216.34"), ("2606:4700:4700::1111", "2606:4700:4700::1111")])
def test_a_platform_is_the_domain_its_hosts_share(host, platform):
    assert polite.platform_of(host) == platform


async def test_a_host_is_never_asked_by_two_requests_at_once_however_slowly_it_answers():
    site = Open()
    fetch = _fast(site)
    replies = await asyncio.gather(*(fetch.get(f"https://boards-api.feedone.test/v1/boards/b{n}/jobs")
                                     for n in range(6)))
    assert [r.status_code for r in replies] == [200] * 6
    assert site.host_peak == {"boards-api.feedone.test": 1}


async def test_the_hosts_of_one_platform_are_asked_together_but_never_more_than_four_at_once():
    site = Open(fail=("t3.wd5.tenantone.test", "t7.wd5.tenantone.test"))
    fetch = _fast(site)
    asked = [fetch.get(f"https://t{n}.wd5.tenantone.test/wday/cxs/t{n}/Site/jobs") for n in range(12)]
    results = await asyncio.gather(*asked, return_exceptions=True)
    assert sum(isinstance(r, httpx.ConnectError) for r in results) == 2, "a request that fails still gives its place up"
    assert sum(getattr(r, "status_code", 0) == 200 for r in results) == 10
    assert polite.PLATFORM_AT_ONCE == 4 and site.platform_peak == {"tenantone.test": 4}
    assert max(site.host_peak.values()) == 1, "each of the twelve hosts was asked one at a time, as ever"


async def test_a_platform_at_its_limit_does_not_hold_up_another_platform():
    slow = tuple(f"t{n}.wd5.tenantone.test" for n in range(5))
    site = Open(slow=slow)
    fetch = _fast(site)
    asked = [fetch.get(f"https://t{n}.wd5.tenantone.test/jobs") for n in range(5)]
    asked.append(fetch.get("https://boards-api.feedone.test/v1/boards/acme/jobs"))
    await asyncio.gather(*asked)
    def place_of(what: str, host: str) -> int:
        return next(i for i, event in enumerate(site.events) if event == (what, host))

    first_end = next(i for i, (what, _host) in enumerate(site.events) if what == "ended")
    assert place_of("began", "boards-api.feedone.test") < first_end, "the other platform began with the first four"
    assert place_of("began", slow[4]) > first_end, "the fifth host of the full platform waited for a place"


# --- how fast, and how often robots.txt is asked (docs/PRD.md 28.17) -------------------------------------------------


class Timed:
    """A site that answers at once or after a moment, and what it saw: when each request began (by host), and how many
    times each host's robots.txt was asked for."""

    def __init__(self, delay: float = 0.0, robots: int = 404) -> None:
        self.delay, self.robots_status = delay, robots
        self.began: list[tuple[float, str]] = []
        self.robots: dict[str, int] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path == "/robots.txt":
            self.robots[host] = self.robots.get(host, 0) + 1
            await asyncio.sleep(self.delay)
            return httpx.Response(self.robots_status)
        self.began.append((time.monotonic(), host))
        await asyncio.sleep(self.delay)
        return httpx.Response(200, text="answered")

    def starts(self, host: str | None = None) -> list[float]:
        return sorted(at for at, h in self.began if host is None or h == host)


async def test_a_platform_is_asked_no_faster_than_four_hosts_at_their_pace_however_quickly_they_answer():
    """Docs/PRD.md 28.17. Four boards at once, each host at a request a pace, was the most a job system was asked: four
    a pace. Wide, a place freed by a quick answer goes to the next host at once, whose own pace is long met, so the
    limit on requests open at once alone let a job system be asked 2 to 6 times faster. Old: a limit on requests open
    at once (the eight hosts begin together, and the sixteen requests end in a few answers' time). New: no five requests
    of a platform begin within one pace."""
    pace = 0.1
    site = Timed(delay=0.005)
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(site)), per_host_seconds=pace)

    async def tenant(n: int) -> None:
        for page in range(2):
            await fetch.get(f"https://t{n}.wd5.tenantone.test/jobs?page={page}")

    await asyncio.gather(*(tenant(n) for n in range(8)))
    starts, most = site.starts(), polite.PLATFORM_AT_ONCE
    assert len(starts) == 16
    # a quarter of a pace is left over for a clock that ticks late (Windows: 16 ms) and a timer that wakes early
    assert all(starts[i + most] - starts[i] >= 0.75 * pace for i in range(len(starts) - most)), \
        "five requests of one platform began within a pace"


async def test_a_tenant_read_slowly_does_not_hold_the_platforms_other_tenants_up_for_its_pace():
    """Docs/PRD.md 28.17. The platform's rate is the fetcher's own pace; a host's slower pace (`slow_host`, a
    Crawl-delay) is kept in the host's own wait. Old: the platform's rate wait took the pace of the host that arrived,
    so a slowed tenant slept its long pace holding the platform's rate lock and a place, and every tenant of the
    platform behind it waited that long: three normal tenants began 0.4 s on instead of 0.04 s."""
    pace, slowed = 0.04, 0.4
    site = Timed(delay=0.005)
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(site)), per_host_seconds=pace)
    slow = "slow.wd5.tenantone.test"
    others = [f"t{n}.wd5.tenantone.test" for n in range(7)]
    for host in (slow, *others):
        fetch._exempt_robots(host, "/")  # no robots.txt request to count among the platform's starts
    assert fetch.slow_host(slow, factor=10, most=10) == pytest.approx(slowed)

    async def asked(host: str, times: int = 1) -> None:
        for page in range(times):
            await fetch.get(f"https://{host}/jobs?page={page}")

    # Four tenants fill the platform's rate; the slowed one arrives fifth, three more tenants behind it.
    await asyncio.gather(*(asked(host) for host in others[:4]), asked(slow, 2), *(asked(host) for host in others[4:]))
    first = min(site.starts())
    late = [at - first for host in others[4:] for at in site.starts(host)]
    assert len(late) == 3 and max(late) < slowed / 2, f"tenants of the platform waited {max(late):.2f} s for a slow one"
    own = site.starts(slow)
    assert len(own) == 2 and own[1] - own[0] >= 0.75 * slowed, "the slowed tenant is still asked at its own pace"


async def test_a_host_alone_is_still_asked_at_its_pace_and_never_waits_longer_for_the_platforms_sake():
    """The pace of a host is as it was (28.5): one request each pace after the last began, whatever the platform does. A
    lone host's place is held for the pace and its next request waits for the same pace, so the two add no wait."""
    pace = 0.1
    site = Timed(delay=0.01)
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(site)), per_host_seconds=pace)
    began = time.monotonic()
    for page in range(4):
        await fetch.get(f"https://acme.wd5.tenantone.test/jobs?page={page}")
    took = time.monotonic() - began
    starts = site.starts("acme.wd5.tenantone.test")
    assert all(b - a >= 0.75 * pace for a, b in zip(starts, starts[1:], strict=False)), "the pace is kept"
    assert took < 4 * pace + 0.1, "four requests took four paces and a bit: the place held for the pace added nothing"


async def test_two_boards_of_one_host_take_turns_at_the_hosts_pace():
    """Two reads of one host (Feedone's boards share one) are one request at a time, a pace apart."""
    pace = 0.1
    site = Timed(delay=0.02)
    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(site)), per_host_seconds=pace)

    async def board(slug: str) -> None:
        for _ in range(2):
            await fetch.get(f"https://boards-api.feedone.test/v1/boards/{slug}/jobs")

    await asyncio.gather(board("alpha"), board("beta"))
    starts = site.starts()
    assert len(starts) == 4 and all(b - a >= 0.75 * pace for a, b in zip(starts, starts[1:], strict=False))


async def test_a_hosts_robots_txt_is_read_once_however_many_requests_reach_it_together():
    """Docs/PRD.md 28.17. The first wave of a wide read (24 boards, or the first 24 descriptions of a slice) sends its
    requests to a host that has not been asked yet together; each used to read robots.txt for itself, a request each
    paced a second on, for a site that had not been asked a thing. Old: 24 reads of robots.txt. New: one a host."""
    site = Timed(delay=0.01)
    fetch = _fast(site)
    asked = [fetch.get(f"https://boards-api.feedone.test/v1/boards/b{n}/jobs") for n in range(24)]
    asked += [fetch.get(f"https://api.feedfour.test/v0/postings/b{n}") for n in range(8)]
    replies = await asyncio.gather(*asked)
    assert [r.status_code for r in replies] == [200] * 32
    assert site.robots == {"boards-api.feedone.test": 1, "api.feedfour.test": 1}


async def test_a_robots_txt_that_could_not_be_read_is_read_once_too_and_every_request_waiting_is_refused():
    site = Timed(robots=500)
    fetch = _fast(site)
    results = await asyncio.gather(*(fetch.get(f"https://boards-api.feedone.test/v1/boards/b{n}/jobs")
                                     for n in range(6)), return_exceptions=True)
    assert all(isinstance(r, RobotsRefused) and r.unreachable for r in results)
    assert site.robots == {"boards-api.feedone.test": 1} and site.began == []


async def test_requests_waiting_for_one_busy_host_hold_no_place_of_its_platform():
    """The host is waited for before the platform's place: six requests queued on one slow tenant must not take the
    platform's places from its other tenants. Old (a place taken first, then the host): the other tenant waited for the
    places the queue held, six answers' time."""
    site = Timed(delay=0.2)
    fetch = _fast(site)
    queue = [asyncio.create_task(fetch.get(f"https://roche.wd3.tenantone.test/jobs/{n}")) for n in range(6)]
    await asyncio.sleep(0.03)
    began = time.monotonic()
    await fetch.get("https://acme.wd5.tenantone.test/jobs")
    waited = time.monotonic() - began
    await asyncio.gather(*queue)
    assert waited < 0.5, f"the other tenant waited {waited:.2f} s behind another tenant's queue"
