"""Made-up stand-ins for the sites the job-search tests need, so no test names one.

Two things are here.

`EXAMPLE_SYSTEM`, `OTHER_SYSTEM` and `THIRD_SYSTEM` are the keys of three job systems the install ships a runbook for
(the first three whose terms were read as open to a reader, in the order the runbooks are asked): what a test says of
"a job system's posting", and of a second and a third one, where it does not care which. A posting from a system the
registry does not know is never treated as a job system's whole text, so the keys come from the registry.

`example_world` is a fixture: for the length of the test, the registry also holds made-up job boards and job systems
(`.test` hosts, in the shapes the real ones have: a slug in the path, a tenant in the host, a host that is the
customer's) and `ats.parse` knows the systems. Nothing here is a real site's name, address or rule; what the real
runbooks say is the restricted tier's frozen reference."""

import pytest

from app.jobsearch import adapters, ats
from app.jobsearch.adapters.feeds import adapter_for
from app.jobsearch.adapters.unread import DataSystem
from app.sites import registry
from app.sites.registry import Registry
from app.sites.schema import Runbook
from tests.support import EXAMPLE_SYSTEM

_OTHERS = [r.key for r in registry.current().runbooks
           if r.kind == "job_system" and r.key != EXAMPLE_SYSTEM
           and registry.current().verdict(r.hosts[0]) in ("allows", "silent")]
OTHER_SYSTEM, THIRD_SYSTEM = _OTHERS[:2]

# --- the made-up job boards -------------------------------------------------------------------------------------------

#: A job board whose page of one job is `jobs/view/<id>`, with a regional host (`uk.board.test`).
BOARD = "board.test"
#: A second job board: one job is `viewjob?jk=<id>`.
SECOND_BOARD = "listings.test"
#: A search site that also holds employers' own careers pages, under one path.
SEARCH_SITE = "searchsite.test"
#: A third job board: one job is `job-detail/<id>`.
THIRD_BOARD = "thirdboard.test"
OWN_CAREERS_AT_SEARCH_SITE = "https://www.searchsite.test/about/careers/applications/jobs/results/123456789-program-manager"

# --- the made-up job systems ------------------------------------------------------------------------------------------

#: A system whose board is a slug in the path and whose one posting is `jobs/<digits>`.
FEEDSYS = "feedsys"
#: A system whose board is a slug in the path and whose one posting is a GUID after it.
SLUGSYS = "slugsys"
#: A system whose board is a tenant (the first name of the host) and a site (the first part of the path).
TENANTSYS = "tenantsys"
#: A system whose board is the customer's own host.
HOSTSYS = "hostsys"
#: A system whose board is a slug in the path and whose application forms are rewritten to the posting's page.
APPLYSYS = "applysys"
#: A system whose posting is `<slug>/<id>-<title>`.
PATHIDSYS = "pathidsys"
#: A system that only has an address a careers page hands its openings off to (`portal`): no rule of its own.
PORTALSYS = "portalsys"
#: A made-up client key (32 hex characters, as a real one is) and the employer's own listing on `PORTALSYS` that a
#: careers page hands its openings off to (docs/PRD.md 37.12): the portal of the site `ONE` of `tests.builders`.
PORTAL_KEY = "0123456789ABCDEF0123456789ABCDEF"
PORTAL_HOST = "portalsys.test"
PORTAL = f"https://www.{PORTAL_HOST}/v4/ats/web.php/portal/{PORTAL_KEY}/career-page"

#: What the made-up feed system reads: its list, one posting, and the probe of a board by an employer's name.
FEED_EXTRAS = {'read': {'kind': 'request',
                       'url': 'https://boards-api.feedsys.test/v1/boards/{slug}/jobs?content=true',
                       'items': 'jobs',
                       'fields': {'ext_id': 'id',
                                  'title': 'title',
                                  'location': ['location.name', 'location'],
                                  'posted': ['first_published', 'updated_at'],
                                  'url': 'absolute_url',
                                  'text': {'html': 'content'},
                                  'department': 'departments.0.name'},
                       'work_mode': {},
                       'comp': {'range': {'path': 'pay_input_ranges.0',
                                          'min': 'min_cents',
                                          'max': 'max_cents',
                                          'currency': 'currency_type',
                                          'period_is': 'year',
                                          'unit': 100,
                                          'writes_text': True}},
                       'detail': {'url': 'https://boards-api.feedsys.test/v1/boards/{slug}/jobs/{ext_id}',
                                  'fields': {'text': {'html': 'content'},
                                             'posted': ['first_published', 'updated_at'],
                                             'location': ['location.name', 'location']},
                                  'work_mode': {}}},
              'flags': {'drawn': True, 'probe_names': True},
              'probe': {'url': 'https://boards-api.feedsys.test/v1/boards/{slug}', 'name': 'name'}}

DOCS: list[dict] = [
    {"key": "exboard", "name": "Exboard", "kind": "board", "hosts": [BOARD], "stop": "until_next_off_hours",
     "follow_to_employer": True,
     "address": {"one_posting": [{"path": "jobs/view/.+"}, {"path": "comm/jobs/view/.+"}]}},
    {"key": "exlistings", "name": "Exlistings", "kind": "board", "hosts": [SECOND_BOARD],
     "address": {"one_posting": [{"path": "(?:.+/)?(?:viewjob|clk)", "query": {"jk": "any"}}]}},
    {"key": "exsearch", "name": "Exsearch", "kind": "board", "hosts": [SEARCH_SITE],
     "address": {"own_careers_paths": ["about", "careers", "applications", "jobs", "results"], "one_posting": []}},
    {"key": FEEDSYS, "name": "Feedsys", "kind": "job_system", "hosts": ["feedsys.test"], "order": 1,
     "address": {
         "board": [
             {"host": "(?:job-)?boards(?:\\.eu)?\\.feedsys\\.test",
              "path": "(?:embed/)*(?-i:job_board|job_app)(?:/(?s:.*))?", "query": {"for": "any"},
              "capture": {"slug": "query:for"}},
             {"host": "(?:job-)?boards(?:\\.eu)?\\.feedsys\\.test",
              "path": "(?:embed/)*(?!embed(?:/|$))(?!(?-i:job_board|job_app)(?:/|$))(?P<slug>[^/]+)(?:/(?s:.*))?",
              "capture": {"slug": "group:slug"}},
             {"host": "boards-api\\.feedsys\\.test", "path": "[^/]+/[^/]+/(?P<slug>[^/]+)(?:/(?s:.*))?",
              "capture": {"slug": "group:slug"}}],
         "one_posting": [
             {"path": "(?:(?!jobs/)[^/]+/)*jobs/(?P<ext_id>\\d+)(?:/(?s:.*))?", "capture": {"ext_id": "group:ext_id"}},
             {"path": "(?:(?!jobs/)[^/]+/)*[^/]*", "query": {"token": "digits"}, "capture": {"ext_id": "query:token"}}],
         "board_url": [{"template": "https://job-boards.feedsys.test/{slug}", "needs": ["slug"]}]},
     **FEED_EXTRAS},
    {"key": SLUGSYS, "name": "Slugsys", "kind": "job_system", "hosts": ["slugsys.test"], "order": 2,
     "address": {
         "board": [{"host": "jobs(?:\\.eu)?\\.slugsys\\.test", "path": "(?P<slug>[^/]+)(?:/(?s:.*))?",
                    "capture": {"slug": "group:slug"}}],
         "one_posting": [{"host": "jobs(?:\\.eu)?\\.slugsys\\.test",
                          "path": "[^/]+/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                                  "(?:/(?s:.*))?"}],
         "board_url": [{"template": "https://jobs.slugsys.test/{slug}", "needs": ["slug"]}]}},
    {"key": TENANTSYS, "name": "Tenantsys", "kind": "job_system", "hosts": ["tenantsys.test"], "order": 0,
     "flags": {"needs": ["tenant", "site"]},
     "address": {
         "board": [{"host": "[^.]+\\.wd\\d+\\.tenantsys\\.test", "skip_locale": True,
                    "path": "(?P<site>[^/]+)(?:/(?s:.*))?", "capture": {"tenant": "host_name", "site": "group:site"}},
                   {"host": "[^.]+\\.wd\\d+\\.tenantsys\\.test", "capture": {"tenant": "host_name"}}],
         "one_posting": [{"host": "[^.]+\\.wd\\d+\\.tenantsys\\.test", "skip_locale": True,
                          "path": "[^/]+/job/.+"}],
         "board_url": [{"template": "https://{tenant}.wd5.tenantsys.test/{site}", "needs": ["tenant", "site"]}]}},
    {"key": HOSTSYS, "name": "Hostsys", "kind": "job_system", "hosts": ["hostsys.test"], "order": 3,
     "flags": {"needs": ["host", "section"], "customer_host": True},
     "address": {
         "board": [{"host": r".+\.hostsys\.test", "customer_host": True,
                    "path": r"section/(?P<section>[^/]+)(?:/(?s:.*))?",
                    "capture": {"host": "host", "section": "group:section"}},
                   {"host": r".+\.hostsys\.test", "customer_host": True, "capture": {"host": "host"}}],
         "one_posting": [{"path": r"section/(?:[^/]+/)*detail", "query": {"job": "word"},
                          "capture": {"ext_id": "query:job"}}],
         "board_url": [{"needs": ["host", "section"], "template": "https://{host}/section/{section}/search"}]}},
    {"key": "exthird", "name": "Exthird", "kind": "board", "hosts": [THIRD_BOARD], "code_reads_postings": True,
     "address": {"one_posting": [{"path": "job-detail/.+"}]}},
    {"key": APPLYSYS, "name": "Applysys", "kind": "job_system", "hosts": ["applysys.test"], "order": 4,
     "probe": {"proves_board": False},
     "address": {
         "board": [{"host": r"apply\.applysys\.test", "path": r"j(?:/(?s:.*))?"},
                   {"host": r"apply\.applysys\.test", "path": r"(?P<slug>(?!j(?:/|$))[^/]+)(?:/(?s:.*))?",
                    "capture": {"slug": "group:slug"}}],
         "one_posting": [{"host": r"apply\.applysys\.test", "path": r"j/[^/]+"},
                         {"path": r"[^/]+/(?:[^/]+/)*?j/[^/]+(?:/(?s:.*))?"}],
         "apply_page": [{"host": r"apply\.applysys\.test",
                         "path": r"(?!j/)(?P<tenant>[^/]+)/j/(?P<id>[^\W_]+)/apply(?:/(?s:.*))?",
                         "capture": {"tenant": "group:tenant", "id": "group:id"},
                         "template": "https://{host}/{tenant}/j/{id}/"}],
         "board_url": [{"template": "https://apply.applysys.test/{slug}/", "needs": ["slug"]}]}},
    {"key": PORTALSYS, "name": "Portalsys", "kind": "job_system", "hosts": [PORTAL_HOST], "order": 6,
     "address": {}},
    {"key": PATHIDSYS, "name": "Pathidsys", "kind": "job_system", "hosts": ["pathidsys.test"], "order": 5,
     "address": {
         "board": [{"host": r"jobs\.pathidsys\.test", "path": r"(?P<slug>[^/]+)(?:/(?s:.*))?",
                    "capture": {"slug": "group:slug"}}],
         "one_posting": [{"host": r"jobs\.pathidsys\.test", "path": r"[^/]+/\d{6,}-.+"}],
         "board_url": [{"template": "https://jobs.pathidsys.test/{slug}", "needs": ["slug"]}]}},
]
SYSTEM_KEYS = (FEEDSYS, SLUGSYS, TENANTSYS, HOSTSYS, APPLYSYS, PATHIDSYS, PORTALSYS)


def _probing_runbook():
    """The first shipped job system, open to a reader, whose feed names the employer in its answer to a probe."""
    reg = registry.current()
    return next(r for r in reg.runbooks
                if r.kind == "job_system" and reg.verdict(r.hosts[0]) in ("allows", "silent")
                and r.probe is not None and r.probe.url and r.probe.name and r.flags.probe_names)


_PROBING = _probing_runbook()
#: The key of a shipped job system whose open feed answers a probe for a board with the employer's name.
PROBING_SYSTEM = _PROBING.key


def probe_url(slug: str) -> str:
    """The address the probe of `PROBING_SYSTEM` asks for the board `slug`."""
    assert _PROBING.probe is not None and _PROBING.probe.url
    return _PROBING.probe.url.format(slug=slug)


def probe_answer(name: str) -> dict:
    """What its feed answers when the board is the named employer's."""
    assert _PROBING.probe is not None and _PROBING.probe.name
    return {_PROBING.probe.name: name}


def portal_card(n: int = 1, **fields) -> dict:
    """One listing read on the portal of `ONE`: `site` is still the careers site's host, `url` is on the portal."""
    from tests.builders import browser_card

    return browser_card(n, url=f"https://www.{PORTAL_HOST}/v4/ats/web.php/portal/{PORTAL_KEY}/jobs/{500 + n}", **fields)


def made_up_registry() -> Registry:
    """The registry of the install with the made-up boards and systems added, and their terms (open to a reader)."""
    current = registry.current()
    docs = [Runbook.model_validate({"schema": 1, **doc}) for doc in DOCS]
    terms = [{"name": d["key"], "hosts": d["hosts"], "verdict": "allows"} for d in DOCS]
    return Registry([*current.runbooks, *docs], current.problems, restricted_present=current.restricted_present,
                    terms=[*current._terms, *terms],  # pyright: ignore[reportPrivateUsage]
                    hook_folder=current._hook_folder)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def example_world(monkeypatch):
    """The made-up boards and systems are in the registry, and `ats.parse` knows the systems, for the test."""
    reg = made_up_registry()
    with registry.replaced(reg):
        systems, readers = ats._loaded()  # pyright: ignore[reportPrivateUsage]
        made = {r.key: DataSystem(r) for r in reg.runbooks if r.kind == "job_system" and r.key in SYSTEM_KEYS
                and r.key != FEEDSYS}
        reader = adapter_for(next(r for r in reg.runbooks if r.key == FEEDSYS))
        monkeypatch.setitem(adapters.ADAPTERS, FEEDSYS, reader)
        every = {FEEDSYS: reader, **made, **systems}
        monkeypatch.setattr(ats, "_loaded", lambda: (every, {FEEDSYS: reader, **readers}))
        yield reg


__all__ = ["APPLYSYS", "BOARD", "DOCS", "EXAMPLE_SYSTEM", "FEEDSYS", "HOSTSYS", "OTHER_SYSTEM", "PATHIDSYS", "PORTAL",
           "PORTALSYS", "PORTAL_HOST", "PORTAL_KEY", "PROBING_SYSTEM", "SECOND_BOARD", "SEARCH_SITE", "SLUGSYS",
           "TENANTSYS", "THIRD_BOARD", "THIRD_SYSTEM", "example_world", "portal_card", "probe_answer", "probe_url"]
