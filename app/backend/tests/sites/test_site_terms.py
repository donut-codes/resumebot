"""The known site terms list (docs/PRD.md 55.10): the shipped file is well formed and its evidence is sound, only the
forbidding entries leave the module, and `GET /sites` carries them with each job system's domains. Every host in the
fixtures is made up (example.test); no network."""

import json
from datetime import date
from importlib import resources
from urllib.parse import urlsplit

import pytest

from app import db
from app.sites import avoid, job_systems, site_terms
from tests.jobsearch.example_world import FEEDSYS, example_world  # noqa: F401
from tests.support import OWNER, load_profile


@pytest.fixture
def fresh():
    """The loader's remembered answer is cleared before and after, so no test sees another's list."""
    load = site_terms._load  # held now: a test may replace it, and the real one is what is cleared
    load.cache_clear()
    yield
    load.cache_clear()


def _shipped() -> list[dict]:
    return json.loads(resources.files("app.sites").joinpath("site_terms.json").read_text(encoding="utf-8"))


def _entry(name: str, hosts: list[str], verdict: str, **extra) -> dict:
    return {"name": name, "hosts": hosts, "terms_url": f"https://{hosts[0]}/terms", "date_read": "2026-11-14",
            "quote": "Automated tools may not be used on the site.", "verdict": verdict, **extra}


#: The same case list as frontend/src/components/search/termsNote.test.ts: the page matches by the server's rule.
MATCH_CASES = [
    ("example.test", "example.test", True),
    ("https://www.example.test/jobs?x=1", "example.test", True),
    ("jobs.example.test", "example.test", True),
    ("Example.TEST.", "example.test", True),
    ("https://jobs.example.test:8443/a", "example.test", True),
    ("notexample.test", "example.test", False),
    ("example.test.other.test", "example.test", False),
    ("example.test@other.test", "example.test", False),
    ("other.test", "jobs.example.test", False),
    ("", "example.test", False),
]


@pytest.mark.parametrize(("address", "listed", "covered"), MATCH_CASES)
def test_the_matching_rule_the_page_copies_is_the_servers(address, listed, covered):
    host = avoid.host_of(address)
    assert (host is not None and avoid.covers(listed, host)) is covered


def test_the_shipped_list_is_well_formed_and_its_evidence_is_sound(fresh):
    entries = _shipped()
    assert entries, "a list with nothing on it is a lost file"
    site_terms._load()  # the loader's own validation passes
    hosts = []
    for e in entries:
        assert e["verdict"] in ("allows", "silent", "forbids"), e["name"]
        assert urlsplit(e["terms_url"]).scheme == "https", e["name"]
        assert e["hosts"] and all(h == h.lower() and "/" not in h for h in e["hosts"]), e["name"]
        date.fromisoformat(e["date_read"])
        assert len(e["quote"].split()) <= 25, f"{e['name']}: the quote is at most 25 words"
        if e["verdict"] == "forbids":
            assert len(e["quote"]) >= 40, f"{e['name']}: a forbidding entry's quote is real evidence"
            host = avoid.host_of(e["terms_url"])
            assert host is not None
            assert any(avoid.related(host, h) for h in e["hosts"]) or any(
                avoid.related(host, h) for other in entries if other is not e for h in other["hosts"]), (
                f"{e['name']}: the terms address is on the site's own host, or on another entry's")
        hosts += [(e["name"], h) for h in e["hosts"]]
    for i, (name_a, a) in enumerate(hosts):
        for name_b, b in hosts[i + 1:]:
            assert name_a == name_b or not avoid.related(a, b), f"{name_a} and {name_b} cover each other's hosts"


def test_forbidding_returns_only_the_forbidding_entries_with_their_terms_link(monkeypatch, fresh):
    listed = [_entry("Alpha", ["alpha.example.test"], "forbids"), _entry("Beta", ["beta.example.test"], "allows"),
              _entry("Gamma", ["gamma.example.test"], "silent", quote="")]
    monkeypatch.setattr(site_terms, "_load", lambda: tuple(listed))
    assert site_terms.forbidding() == [{"hosts": ["alpha.example.test"], "terms_url": "https://alpha.example.test/terms"}]


@pytest.mark.parametrize("bad", [
    {"verdict": "maybe"}, {"terms_url": "http://alpha.example.test/terms"}, {"hosts": []},
    {"verdict": "forbids", "quote": " "},
])
def test_a_bad_entry_raises_on_the_first_use(monkeypatch, fresh, bad):
    text = json.dumps([_entry("Alpha", ["alpha.example.test"], "forbids") | bad])

    class Fake:
        def joinpath(self, _name):
            return self

        def read_text(self, encoding):
            return text

    monkeypatch.setattr(site_terms.resources, "files", lambda _pkg: Fake())
    with pytest.raises(ValueError, match="site terms: Alpha"):
        site_terms.forbidding()


@pytest.mark.usefixtures("example_world")
async def test_the_sites_answer_carries_the_forbidding_list_and_each_systems_domains(client, monkeypatch, fresh):
    monkeypatch.setattr(site_terms, "_load", lambda: (_entry("Alpha", ["alpha.example.test"], "forbids"),
                                                      _entry("Beta", ["beta.example.test"], "silent", quote="")))
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
        listed = await job_systems.listing(session, OWNER, FEEDSYS)
    page = (await client.get("/api/v1/sites")).json()
    assert page["terms"] == [{"hosts": ["alpha.example.test"], "terms_url": "https://alpha.example.test/terms"}]
    assert listed[0]["domains"] == ["feedsys.test"]
