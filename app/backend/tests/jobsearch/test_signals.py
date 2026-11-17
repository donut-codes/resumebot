"""State layoff notices (docs/PRD.md 28.12): matched to known employers by exact name, kept once as dated layoff
events with their source, so the "recent layoffs" fact comes from a record; an employer whose events the owner set is
left alone. No network: a mock transport answers. Synthetic data only."""

from datetime import date

import httpx

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import signals
from app.jobsearch.fetch.polite import Fetcher
from tests.support import OWNER

THIS_YEAR = date.today().year

CSV = (
    "id,state,company,company_canonical,company_dba,location,employees_affected,notice_date,effective_date,"
    "notice_type,first_seen\n"
    f"a1,ST,\"Fabrikam, Inc.\",Fabrikam,,Oakfield,120,{THIS_YEAR}-03-02,{THIS_YEAR}-05-01,Layoff,x\n"
    f"a2,ST,Fabrikam Robotics,Fabrikam Robotics,,Riverton,40,{THIS_YEAR}-04-10,,Closure,x\n"
    "a3,ST,Contoso,Contoso,,Maple Heights,15,2001-01-01,,Layoff,x\n"
)


def _fetch(body: str = CSV, status: int = 200) -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(status, text=body)
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)


async def test_a_notice_becomes_a_layoff_event_on_the_employer_it_names_once(js):
    async with db.session_factory()() as session:
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        contoso = await employers.find_or_make(session, OWNER, "Contoso")
        first = await signals.load_warn(session, OWNER, _fetch())
        again = await signals.load_warn(session, OWNER, _fetch())
        assert (first["read"], first["employers"], first["added"]) == (3, 1, 1)
        assert again["added"] == 0, "a notice is kept once"
        (event,) = fab.events
        assert event["kind"] == "layoff" and event["date"] == f"{THIS_YEAR}-03"
        assert event["note"] == "120 affected; Oakfield, ST; Layoff" and event["source"] == signals.WARN_SOURCE
        assert "years_since_layoff" in employers.facts(fab), "the fact the gates and modifiers read"
        assert contoso.events == [], "a notice from long ago is not kept"
        assert not any(e.get("note", "").startswith("40") for e in fab.events), "a near name is not a match"


async def test_events_the_owner_set_are_left_alone_and_a_failed_read_says_why(js):
    async with db.session_factory()() as session:
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        fab.owner_fields = ["events"]
        assert (await signals.load_warn(session, OWNER, _fetch()))["added"] == 0
        failed = await signals.load_warn(session, OWNER, _fetch(status=500))
        assert failed["error"] == "the notices answered 500"
