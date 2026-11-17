"""What a posting's address is (docs/PRD.md 32.6): three pure functions that judge an address and nothing else.

- `is_board_link(url, hosts)`: the address goes to a job board, one the app knows by name (`ats.board_named`) or one of
  the owner's job sites (`hosts`, data the caller read). Never an employer's own posting on a board's host
  (`ats.own_careers_posting`).
- `is_single_posting(url)`: the address opens ONE posting: a job system's own page for it, a job board's page for one
  job, or an employer's page whose address names one. Never a careers home page, a board's front page, a search or a
  listing page, a sign-in page or anything that is not a public web address. It never reads `hosts`.
- `posting_page(link)`: the address a link is judged and stored as: an apply form whose posting page follows from it
  by a fixed rule (`System.apply_page`) is rewritten to that page, any other link comes back as given.

Synchronous, database-free, total: None, a non-string or garbage gives False (or the stripped text for `posting_page`),
and nothing is raised. A caller words its own refusal. The order of the checks in `is_single_posting` is behaviour (a
sign-in, then an employer's own site on a board's host, then the job system's own answer, then a board's rule, then the
vendor shapes below in their order, then the generic rule); a shape answers only for what it names, so an address it
does not name keeps its answer (docs/PRD.md 32.6). What a site's own addresses look like is its runbook's
(`address_rules`), not this module's. This module imports only `ats`, `adapters`, `avoid`, `registry` and
`skills.public_url`, so `sourced`, `follow` and `go_checks` sit above it."""

import re
from urllib.parse import parse_qs, urlparse

from app.jobsearch import ats
from app.jobsearch.adapters import Address, id_like
from app.sites import address_rules, avoid, registry
from app.sites.skills import public_url


def is_board_link(url: object, hosts: frozenset[str] | set[str] = frozenset()) -> bool:
    """Whether a link goes to a job board: one the app knows by name (`ats.board_named`), or one of the owner's job
    sites (`hosts`). Never one posting on an employer's own careers site that lives on a board's host
    (`ats.own_careers_posting`). `hosts` changes this answer only, never `is_single_posting`'s."""
    host = avoid.host_of(url)
    if not host:
        return False
    labels = host.split(".")
    covered = ats.board_named(host) or (bool(hosts) and any(".".join(labels[i:]) in hosts for i in range(len(labels))))
    return covered and not ats.own_careers_posting(url)


# --- one posting, not a front page ------------------------------------------------------------------------------------

#: Path words of a sign-in or sign-up page.
_LOGIN = frozenset({"login", "log-in", "signin", "sign-in", "sign_in", "signup", "sign-up", "register", "registration",
                    "authwall", "uas", "checkpoint", "sso", "oauth", "oauth2", "auth", "authenticate", "password",
                    "forgot-password", "createaccount", "create-account"})
_LOGIN_HOSTS = ("login.", "signin.", "accounts.", "auth.", "sso.", "id.")
#: Path words of a search or a listing: a page of many postings.
_LISTING = frozenset({"search", "results", "searchresults", "jobsearch", "job-search", "find", "listings", "all",
                      "all-jobs", "joblist", "job-list", "list", "jobs-list", "browse", "alljobs", "category",
                      "categories", "teams", "locations"})
#: Query words a page uses for the posting it shows (an id the employer's site or job system puts in the address).
_ID_KEYS = frozenset({"jobid", "job_id", "jid", "gh_jid", "reqid", "req_id", "requisitionid", "requisition_id",
                      "jobreqid", "career_job_req_id", "positionid", "position_id", "postingid", "posting_id",
                      "opportunityid", "pid", "jk", "jobcode", "jobnumber", "job"})
#: Path words that say "the postings are below": the section a single posting sits in.
_MARKERS = frozenset({"career", "careers", "position", "positions", "opening", "openings", "opportunity",
                      "opportunities", "vacancy", "vacancies", "requisition", "requisitions", "posting", "postings",
                      "role", "roles", "employment", "apply", "details", "detail"})
#: Of those, the ones that are the employer's careers section in general: a short name after one is a team or a place,
#: so it takes an id, or a title of three words, to say it is one posting (two words do after the others: a job's slug).
_GENERAL = frozenset({"career", "careers", "employment", "apply", "role", "roles", "details", "detail"})
_EXTENSION = re.compile(r"\.(html?|aspx?|php|jsp|cfm)$", re.I)


def is_marker(segment: str) -> bool:
    """Whether a path segment says "the postings are below" (`_MARKERS`, or a word that begins `job`):
    `sourced.careers_root` finds the section of an employer's site by it."""
    return segment in _MARKERS or segment.startswith("job")


#: The one word a posting's own page is named by on an employer's site that has a single detail page, whose address
#: takes the posting's id in its query (`/CareerDescription?id=...`).
_DETAIL_PAGES = frozenset({"careerdescription", "jobdescription", "jobdetail", "jobdetails", "careerdetail",
                           "careerdetails", "positiondetail", "postingdetail"})
_HEX32 = re.compile(r"[0-9a-f]{32}")


#: What `_owned` answers when no shape of the site's own is asked at this point: the generic shapes go on.
class _GoOn:
    pass


_GO_ON = _GoOn()


def _owned_rules(host: str) -> tuple[address_rules.Rule, ...]:
    """The shapes the site of this host names one posting by (its runbook's `address.one_posting`), or none."""
    runbook = registry.for_host(host)
    return (address_rules.rules(runbook, "one_posting") or ()) if runbook and runbook.kind == "host" else ()


def _owned(rules: tuple[address_rules.Rule, ...], rank: int, host: str, parts: list[str],
           query: dict[str, str]) -> "bool | None | _GoOn":
    """The site's own shapes of this rank: True when one holds, None when the site has some of this rank and none holds
    (the generic rule decides, and no generic shape after this one is asked), `_GO_ON` when it has none. The rank is
    where among the generic shapes of `_shaped` and `_job_system_shape` the site's is asked."""
    if not address_rules.has_rank(rules, rank):
        return _GO_ON
    return True if address_rules.first(rules, address_rules.Subject(host, tuple(parts), query), rank) else None


def _shaped(host: str, parts: list[str], query: dict[str, str]) -> bool | None:
    """The shapes of an address that name one posting although a word in it (`search`, `apply`) or a query key (`id`)
    would read as a listing to the generic rule. True for such a shape, None for any other address (the generic rule
    decides). Each is a host's own shape; none of them reorders the generic rule: a listing or a search on those hosts
    has none of them and stays refused."""
    own = _owned_rules(host)
    if not isinstance(found := _owned(own, 0, host, parts, query), _GoOn):  # a site's page of a job is its search page
        return found
    if len(parts) == 1 and parts[0] in _DETAIL_PAGES:  # an employer's one detail page, the posting by its id
        return id_like(query.get("id", "")) or None
    if parts and parts[-1] in _DETAIL_PAGES and id_like(query.get("id", "")):  # the same page, deeper in the path
        return True
    if not isinstance(found := _owned(own, 1, host, parts, query), _GoOn):
        return found
    if (len(parts) >= 3 and parts[-1] == "job" and _HEX32.fullmatch(parts[-2])
            and host.split(".")[0] in ("careers", "career", "jobs")):  # a job-site platform's /<place>/<slug>/<id>/job
        return True
    return _job_system_shape(host, parts, query, own)


def _digits(text: str) -> bool:
    return bool(text) and text.isascii() and text.isdigit()


#: Query keys of a search or a listing: a page that has one is not one posting, whatever else is in its address.
_SEARCH_KEYS = ("q", "keyword", "keywords", "search", "query", "department", "location")
#: The first labels of a host a job-site platform serves an employer's careers site on.
_CAREERS_LABELS = ("careers", "career", "jobs", "werkenbij")


def _job_system_shape(host: str, parts: list[str], query: dict[str, str],
                      own: tuple[address_rules.Rule, ...] = ()) -> bool | None:
    """The posting pages of the common job systems, on an employer's own host (a PeopleSoft page, an applicant system's
    embed, a custom domain) or on a vendor's that has no `System` here (`own`: its runbook's shapes). True for the shape
    of one posting, None for anything else, so no address the shapes do not name changes its answer (docs/PRD.md 32.6).
    An application form is not one: it is refused."""
    on_board = ats.board_named(host)  # without the own-careers exception: a `gh_jid` link on a board's host is refused
    if parts and parts[-1].endswith(".gbl"):  # PeopleSoft: the posting's page, by its opening's number
        if (query.get("page", "").upper() in ("HRS_APP_JBPST", "HRS_APP_JBPST_FL")
                and _digits(query.get("jobopeningid", "")) and "icaction" not in query):
            return True
        return None
    if _digits(query.get("gh_jid", "")) and not on_board and not any(query.get(k) for k in _SEARCH_KEYS):
        return True  # an applicant system's board embedded in an employer's page names its post in `gh_jid`
    if (len(parts) == 2 and parts[0] == "o" and "-" in parts[1] and not on_board
            and host.split(".")[0] in _CAREERS_LABELS):  # a hosted careers site on the employer's own domain
        return True
    if not isinstance(found := _owned(own, 2, host, parts, query), _GoOn):
        return found
    if parts and parts[-1] == "jobdetails.asp" and query.get("jo_num"):  # SmartSearch
        return True
    if parts and parts[-1] == "index.smpl" and query.get("post_id") and query.get("arg") == "view":
        return True
    if len(parts) >= 2 and parts[0] == "jb" and id_like(parts[1]):
        return True
    if not isinstance(found := _owned(own, 3, host, parts, query), _GoOn):
        return found
    if len(parts) >= 2 and parts[-2] == "job":  # a /job/<token>, the token long, with a digit and a letter
        token = parts[-1]
        if _HEX32.fullmatch(token) or (len(token) >= 8 and token.isascii() and token.isalnum()
                                       and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)):
            return True
    return None



def _generic(host: str, parts: list[str], query: dict[str, str]) -> bool:
    """An employer's own site (or any host nothing more is known of): one posting when its address names one."""
    shaped = _shaped(host, parts, query)
    if shaped is not None:
        return shaped
    if any(p in _LISTING for p in parts):
        return False
    if any(query.get(k) for k in _ID_KEYS):
        return True
    if len(parts) < 2:
        return False
    markers = [i for i, p in enumerate(parts[:-1]) if is_marker(p)]
    if not markers:
        return False
    last = _EXTENSION.sub("", parts[-1])
    name = parts[markers[-1]]
    if id_like(last):
        return True
    words = [w for w in re.split(r"[-_+ ]+", last) if w]
    return len(words) >= (3 if name in _GENERAL else 2)


def _board(host: str, parts: list[str], query: dict[str, str]) -> bool | None:
    """A link on a job board the app knows by name: one posting when it has the shape the board gives one's own page
    (never its front page, a search or a listing), by its runbook's `address.one_posting`. None for a board with no
    rule here."""
    runbook = registry.for_host(host)
    found = address_rules.rules(runbook, "one_posting") if runbook and runbook.kind == "board" else None
    if found is None:
        return None
    return address_rules.first(found, address_rules.Subject(host, tuple(parts), query)) is not None



def followed_board_posting(url: str) -> bool:
    """Whether an https address is one posting on a followed board (a runbook's `follow_to_employer`), by that
    runbook's `address.one_posting`."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    runbook = registry.for_host(host) if host and parsed.scheme == "https" else None
    if runbook is None or runbook.kind != "board" or not runbook.follow_to_employer:
        return False
    found = address_rules.rules(runbook, "one_posting")
    parts = tuple(p.lower() for p in parsed.path.split("/") if p)
    query = {k.lower(): v[0] for k, v in parse_qs(parsed.query).items() if v}
    return bool(found) and address_rules.first(found, address_rules.Subject(host, parts, query)) is not None


def is_single_posting(url: object) -> bool:
    """True only for an address that opens ONE posting: a job system's own page for it (`ats.parse(url).one_posting`:
    the shape the system gives a posting's page, which is not always what its reader reads), a job board's page for one
    job, or an employer's page whose address names one (an id, or a title under its jobs section). Never a careers
    home page, a job board's front page, a search or a listing page, a sign-in page, an address with a sign-in in it, or
    anything that is not a public web address. Pure: it reads the address and nothing else."""
    text = (url or "").strip() if isinstance(url, str) else ""
    if not text or not public_url(text):
        return False
    try:
        parsed = urlparse(text if "://" in text else "https://" + text)
    except ValueError:
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return False
    parts = [p.lower() for p in parsed.path.split("/") if p]
    query = {k.lower(): v[0] for k, v in parse_qs(parsed.query).items() if v}
    if host.startswith(_LOGIN_HOSTS) or any(p in _LOGIN for p in parts):
        return False
    if ats.own_careers_posting(text):  # an employer's own site on a board's host: its postings are one each
        return True
    found = ats.parse(text)
    if found and found.board:
        return found.one_posting
    if ats.board_named(host):
        ruled = _board(host, parts, query)
        return _generic(host, parts, query) if ruled is None else ruled
    return _generic(host, parts, query)


# --- the page an apply form belongs to --------------------------------------------------------------------------------

#: A system's apply-form address and the posting page that follows from it by a fixed rule: the same host, the same
#: tenant and the same id. Each rule returns the posting's address, or None for an address it does not match.
def _apply_rules():
    """Every apply-form rule, in order: each job system's own (`System.apply_page`), then those of the runbooks of sites
    that have no `System` here (`address.apply_page`)."""
    def of(runbook, found):
        def apply(a: Address) -> str | None:
            subject = address_rules.Subject(a.host, tuple(a.parts), a.query)
            hit = address_rules.first(found, subject) if a.under(runbook.hosts) else None
            if hit is None or not hit[0].template:
                return None
            return hit[0].template.format(host=a.host, **address_rules.captured(hit[0], hit[1], subject))
        return apply

    own = [of(r, found) for r in registry.current().runbooks if (found := address_rules.rules(r, "apply_page"))]
    return (*(s.apply_page for s in ats.SYSTEMS.values()), *own)



def posting_page(link: object) -> str:
    """The address a link is to be judged and stored as. An apply form whose posting page follows from it by a fixed
    rule (`System.apply_page`: the same system, tenant and id, nothing guessed) is rewritten to that page (docs/PRD.md
    32.6); any other link comes back as given, less the spaces around it. A rule's result stands only when it is
    one posting (`is_single_posting`; some systems' form addresses already pass it, and are stored as the page all the
    same): an apply form with no id, a listing, a search and
    another tenant's path match no rule and keep the answer they had. Pure: it reads the address and nothing else."""
    given = (link or "").strip() if isinstance(link, str) else ""
    if not given:
        return given
    a = Address.of(given)
    if a is None:
        return given
    for rule in _apply_rules():
        if (page := rule(a)) is not None and is_single_posting(page):
            return page
    return given
