"""Made-up employer records for the tests of merging, splitting and finding duplicates (docs/PRD.md path 36): an
employer with the facts a test names, and the roles, postings and sites at it. Nothing here is anyone's data."""

from datetime import UTC, date, datetime

from app.jobsearch import stages
from app.jobsearch.employers.core import key_of
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.sites.models import Site
from app.timeutil import utc_now
from tests.support import EXAMPLE_SYSTEM, OWNER

#: A fixed instant for the postings made here.
THEN = datetime(2026, 10, 1, tzinfo=UTC)
#: How a fact the owner set is sourced.
BY_YOU = {"source": "you", "at": "2026-10-01T00:00:00+00:00", "by": "owner"}


async def make_employer(session, name: str, **fields) -> Employer:
    """An employer record with this name (and any fact given), saved."""
    fields = {"aliases": [], "offices": [], "sectors": [], "events": [], "sources": {}, "owner_fields": [], **fields}
    employer = Employer(owner=OWNER, key=key_of(name), name=name, **fields)
    session.add(employer)
    await session.flush()
    return employer


def make_role(employer: Employer, title: str, status: str = stages.SECOND_IMPRESSION, company: str | None = None,
              **fields) -> Opportunity:
    """A role at the employer, found under `company` (the employer's name unless a test says another)."""
    company = company or employer.name
    return Opportunity(owner=OWNER, slug=f"{company}-{title}".lower().replace(" ", "-"), company=company, title=title,
                       fit=3, origin="scout", match_key=f"{company}|{title}".lower(), status=status,
                       found=date.today(), employer_id=employer.id, **fields)


def make_posting(employer: Employer, ext_id: str, ats: str = EXAMPLE_SYSTEM, **fields) -> JobPosting:
    """A posting read from the employer's board."""
    return JobPosting(owner=OWNER, employer_id=employer.id, ats=ats, ext_id=ext_id, company=employer.name,
                      title=f"Title {ext_id}", match_key=f"{employer.name}|{ext_id}".lower(), first_seen=THEN,
                      last_seen=THEN, links=[], **fields)


def make_site(employer: Employer, url: str, on: bool = False, **fields) -> Site:
    """The employer's own site, as the app makes one (off unless a test says on)."""
    return Site(owner=OWNER, url=url, kind="employer", on=on, employer_id=employer.id, label=employer.name,
                added_by="app", created_at=utc_now(), **fields)
