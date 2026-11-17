"""A posting's posted day is read off the page the app reads (the owner, 2026-10-17): "Posted 3 days ago", "Posted on
October 7, 2026", the schema.org `datePosted`, the job system's own date. A relative day counts from the owner's day of
the read, a day in the future is never kept, and the job system's own day stands over a vague "30+ days ago". Every
employer, address and posting is made up, and no test reaches the network."""


import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.adapters import page_text
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import dates as pd
from app.jobsearch.postings import repairs
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis
from tests.builders import analysis_setup, put_in_line
from tests.jobsearch.test_posting_text_fetch import CAREER_PAGE, MARKUP, WHOLE, _role, web
from tests.support import OWNER, pin_clock

READ = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


#: 8 pm on 5 November in New York (EST, an example zone), which is 1 am on the 6th in UTC; and noon UTC the day
#: before it, 7 am in New York, when the owner's day and the UTC day are the same.
EVENING = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)


MIDDAY = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("text, expected", [
    ("Posted 3 days ago", date(2026, 10, 12)),
    ("Sr. Product Owner\nOakfield, ST\nFull time\nPosted 10 Days Ago\nR-100123", date(2026, 10, 5)),
    ("Posted on October 7, 2026", date(2026, 10, 7)),
    ("Posted: Oct 7th, 2026", date(2026, 10, 7)),
    ("Posted on 7 October 2026", date(2026, 10, 7)),
    ("Date posted: 2026-10-07", date(2026, 10, 7)),
    ("Date Posted - 10/7/2026", date(2026, 10, 7)),
    ("Posted today", date(2026, 10, 15)),
    ("Posted yesterday", date(2026, 10, 14)),
    ("Just posted", date(2026, 10, 15)),
    ("Reposted 2 weeks ago", date(2026, 10, 1)),
    ("Posted 5 hours ago", date(2026, 10, 15)),
    ("Programs Lead\nAcme Oakfield, ST\n6 days ago 53 applicants\nApply", date(2026, 10, 9)),
])
def test_each_way_a_page_says_when_a_posting_was_posted_gives_its_day(text, expected):
    assert pd.day_of(text, READ) == expected


def test_thirty_plus_days_ago_is_a_vague_day_and_the_pages_chrome_names_none():
    got = pd.find("Posted 30+ days ago", READ)
    assert got == pd.Posted(date(2026, 9, 15), vague=True)
    assert pd.find("We posted a role on our blog. Apply today to join us.", READ) is None
    assert pd.find("", READ) is None and pd.find(None, READ) is None


def test_a_relative_day_counts_from_the_read_not_from_today():
    text = "Programs Lead\nPosted 3 days ago"
    assert pd.day_of(text, datetime(2026, 9, 10, 8, 0, tzinfo=UTC)) == date(2026, 9, 7)
    assert pd.day_of(text, datetime(2026, 10, 15, 8, 0, tzinfo=UTC)) == date(2026, 10, 12)


@pytest.mark.parametrize("zone, read, owners_day", [
    ("America/New_York", EVENING, date(2026, 11, 5)),
    ("America/New_York", MIDDAY, date(2026, 11, 5)),
    ("UTC", EVENING, date(2026, 11, 6)),
    ("UTC", MIDDAY, date(2026, 11, 5)),
])
def test_a_relative_day_counts_from_the_owners_day_of_the_read_not_its_utc_day(monkeypatch, zone, read, owners_day):
    """A read between 7 pm and midnight in New York is already the next day in
    UTC, so "Posted 3 days ago" read then was dated a day too young and "Posted today" was tomorrow's date. The owner's
    day is `timeutil.owner_today` (docs/PRD.md path 40, "the local day")."""
    pin_clock(monkeypatch, read, zone)
    text = "Programs Lead\n{}"
    for said, expected in (("Posted 3 days ago", owners_day - timedelta(days=3)), ("Posted today", owners_day),
                           ("Posted yesterday", owners_day - timedelta(days=1))):
        assert pd.day_of(text.format(said), read) == expected, said
    assert pd.find("Posted 30+ days ago", read) == pd.Posted(owners_day - timedelta(days=30), vague=True)
    assert pd.find("Just posted", read) == pd.Posted(owners_day)
    assert pd.find("Programs Lead\n6 days ago 53 applicants", read) == pd.Posted(owners_day - timedelta(days=6))


#: 2 am on 7 November in Denver (MST, UTC-7), which is 9 am in UTC; noon there; and 2 am in Auckland (NZDT, UTC+13),
#: which is 1 pm in UTC on the 6th.
SMALL_HOURS = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


NOON = datetime(2026, 11, 7, 19, 0, tzinfo=UTC)


AUCKLAND_NIGHT = datetime(2026, 11, 6, 13, 0, tzinfo=UTC)


#: 2 am and noon in UTC itself, where the owner's day is the UTC day.
UTC_SMALL_HOURS = datetime(2026, 11, 7, 2, 0, tzinfo=UTC)


UTC_NOON = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("said, zone, read, expected", [
    ("Posted 8 hours ago", "America/Denver", SMALL_HOURS, date(2026, 11, 6)),
    ("Posted 1 hour ago", "America/Denver", SMALL_HOURS, date(2026, 11, 7)),
    ("Posted 90 minutes ago", "America/Denver", SMALL_HOURS, date(2026, 11, 7)),
    ("Posted 150 minutes ago", "America/Denver", SMALL_HOURS, date(2026, 11, 6)),
    ("Posted 30 hrs ago", "America/Denver", SMALL_HOURS, date(2026, 11, 5)),
    ("Posted 8 hours ago", "America/Denver", NOON, date(2026, 11, 7)),
    ("Posted 15 hours ago", "America/Denver", NOON, date(2026, 11, 6)),
    ("Posted 8 hours ago", "Pacific/Auckland", AUCKLAND_NIGHT, date(2026, 11, 6)),
    ("Posted 1 hour ago", "Pacific/Auckland", AUCKLAND_NIGHT, date(2026, 11, 7)),
    ("Posted 3 days ago", "America/Denver", SMALL_HOURS, date(2026, 11, 4)),
    ("Posted 8 hours ago", "UTC", UTC_SMALL_HOURS, date(2026, 11, 6)),
    ("Posted 8 hours ago", "UTC", UTC_NOON, date(2026, 11, 7)),
])
def test_posted_hours_or_minutes_ago_is_the_owners_day_of_that_moment_not_always_today(monkeypatch, said, zone, read,
                                                                                       expected):
    """Regression case: "Posted 8 hours ago" read at 2 am was stored as today, but it was posted
    the evening before. An hour or a minute count is taken from the instant of the read, then the owner's day of what
    is left; days, weeks and months still count whole days from the owner's day of the read."""
    pin_clock(monkeypatch, read, zone)
    assert pd.day_of(f"Programs Lead\n{said}", read) == expected
    assert pd.parse(said, read) == pd.Posted(expected)


def test_a_phrase_read_with_only_a_day_still_counts_hours_as_that_day(monkeypatch):
    """Callers that know only the day of the read (`parse(phrase, day)`) keep what they had: no instant, no crossing."""
    pin_clock(monkeypatch, SMALL_HOURS, "America/Denver")
    assert pd.parse("Posted 8 hours ago", date(2026, 11, 7)) == pd.Posted(date(2026, 11, 7))
    assert pd.parse("Posted 2 days ago", date(2026, 11, 7)) == pd.Posted(date(2026, 11, 5))


@pytest.mark.parametrize("said", [
    "Posted 99999999999 hours ago", "Posted 99999999 hours ago", "Posted 99999999999 minutes ago",
    "Posted 99999999999 days ago", "Posted 10000000 days ago", "Posted 99999999999 weeks ago",
    "Posted 99999999999 months ago", "Posted " + "9" * 5000 + " hours ago", "Posted " + "9" * 5000 + " days ago",
], ids=lambda said: said if len(said) < 40 else "a run of 5000 digits")
def test_a_count_too_big_for_the_calendar_names_no_day_instead_of_raising(monkeypatch, said):
    """Regression case: the page's words are untrusted data, and a count of any number of
    digits reached `timedelta` and the date subtraction, which raise OverflowError, up through `find` and `day_of` to
    the read. A day the calendar cannot hold is no day at all, as a day that is no date is (`_day`)."""
    pin_clock(monkeypatch, READ, "America/Denver")
    assert pd.parse(said, READ) is None
    assert pd.find(f"Programs Lead\n{said}", READ) is None
    assert pd.day_of(f"Programs Lead\n{said}", READ) is None
    assert pd.parse("Posted 2 days ago", READ) == pd.Posted(date(2026, 10, 13)), "an ordinary count is still read"


@pytest.mark.parametrize("said", ["Posted 99999999999 days ago", "Posted 10000000 days ago"])
def test_a_count_of_days_too_big_for_the_calendar_names_no_day_when_the_read_is_only_a_day(said):
    assert pd.parse(said, READ.date()) is None


@pytest.mark.parametrize("zone, read, owners_day", [
    ("America/New_York", EVENING, date(2026, 11, 5)),
    ("America/New_York", MIDDAY, date(2026, 11, 5)),
    ("UTC", EVENING, date(2026, 11, 6)),
    ("UTC", MIDDAY, date(2026, 11, 5)),
])
def test_a_day_after_the_owners_day_of_the_read_is_in_the_future_whatever_the_utc_day_is(monkeypatch, zone, read,
                                                                                         owners_day):
    pin_clock(monkeypatch, read, zone)
    tomorrow = owners_day + timedelta(days=1)
    assert pd.day_of(f"Posted on {tomorrow.isoformat()}", read) is None
    assert pd.day_of(f"Posted on {owners_day.isoformat()}", read) == owners_day
    role = SimpleNamespace(posted=None, found=None)
    assert pd.apply(role, [], system=tomorrow.isoformat(), read_at=read) is False and role.posted is None
    assert pd.apply(role, [], system=owners_day.isoformat(), read_at=read) and role.posted == owners_day


@pytest.mark.parametrize("zone, kept", [("America/New_York", None), ("UTC", date(2026, 11, 7))])
def test_the_day_a_role_was_found_is_the_owners_day_of_the_first_time_its_posting_was_seen(monkeypatch, zone, kept):
    """First seen at 8 pm on the 5th in New York (1 am on the 6th in UTC), read on the 7th: "Posted today" is two days
    after the owner's found day, which is past the one day of slack; in UTC the found day is the 6th and it is not."""
    pin_clock(monkeypatch, datetime(2026, 11, 7, 14, 0, tzinfo=UTC), zone)
    role = SimpleNamespace(posted=None, found=None)
    posting = SimpleNamespace(posted=None, first_seen=EVENING)
    pd.apply(role, [posting], text="Programs Lead\nPosted today", read_at=datetime(2026, 11, 7, 14, 0, tzinfo=UTC))
    assert role.posted == kept


def test_a_role_with_none_takes_no_day_a_posting_holds_that_is_after_the_owners_day_of_the_read(monkeypatch):
    pin_clock(monkeypatch, EVENING, "America/New_York")
    role = SimpleNamespace(posted=None, found=None)
    posting = SimpleNamespace(posted=date(2026, 11, 6), first_seen=EVENING)  # the 6th is tomorrow for the owner
    assert pd.apply(role, [posting], read_at=EVENING) is False and role.posted is None
    today = SimpleNamespace(posted=date(2026, 11, 5), first_seen=EVENING)
    assert pd.apply(role, [today], read_at=EVENING) and role.posted == date(2026, 11, 5)


def test_a_day_in_the_future_is_never_kept():
    assert pd.day_of("Posted on 2026-11-01", READ) is None
    role = SimpleNamespace(posted=None, found=None)
    assert pd.apply(role, [], system="2026-11-01", read_at=READ) is False and role.posted is None


def test_a_day_read_off_the_words_more_than_a_day_after_the_found_day_is_refused():
    assert pd.day_of("Posted today", READ, found=date(2026, 10, 13)) is None
    assert pd.day_of("Posted yesterday", READ, found=date(2026, 10, 14)) == date(2026, 10, 14)
    assert pd.day_of("Posted today", READ, found=date(2026, 10, 15)) == date(2026, 10, 15)
    assert pd.day_of("Posted 3 days ago", READ, found=date(2026, 10, 13)) == date(2026, 10, 12)


def test_the_job_systems_own_day_is_preferred_to_the_pages_words_and_to_a_vague_day():
    role = SimpleNamespace(posted=None, found=date(2026, 10, 15))
    posting = SimpleNamespace(posted=None, first_seen=READ)
    assert pd.apply(role, [posting], text="Posted 30+ days ago", system="2026-10-07", read_at=READ)
    assert role.posted == posting.posted == date(2026, 10, 7)
    held = SimpleNamespace(posted=date(2026, 10, 7), found=date(2026, 10, 15))
    assert pd.apply(held, [], text="Posted 30+ days ago", read_at=READ) is False, "a day already there stays"
    assert held.posted == date(2026, 10, 7)
    # the system's day is the employer's own record: it may be older than the day the app found the role
    older = SimpleNamespace(posted=None, found=date(2026, 10, 2))
    assert pd.apply(older, [], system="2026-10-07", read_at=READ) and older.posted == date(2026, 10, 7)


def test_a_role_with_none_takes_the_day_a_posting_of_its_own_holds():
    role = SimpleNamespace(posted=None, found=date(2026, 10, 15))
    posting = SimpleNamespace(posted=date(2026, 10, 9), first_seen=READ)
    assert pd.apply(role, [posting], read_at=READ) and role.posted == date(2026, 10, 9)


HEADER_PAGE = """<html><head><title>Careers at Example</title></head><body>
<a href="#main">Skip to content</a>
<nav><ul><li>Jobs</li><li>Teams</li></ul></nav>
<header><a href="/login">Log in</a><div class="lang"><button>English</button></div></header>
<main>
  <div class="breadcrumbs">Home / Jobs / Programs Lead</div>
  <h1>Programs Lead</h1>
  <ul class="meta"><li>Oakfield, ST</li><li>Full time</li><li>Permanent</li><li>Posted 10 Days Ago</li>
  <li>R-100123</li><li>$100,000 - $120,000</li></ul>
  <h2>Job Description:</h2>
  <p>Lead the programs that matter, and say what that takes in a few plain words.</p>
  <div class="similar-jobs"><h3>Similar jobs</h3><p>Other Role</p></div>
</main>
<footer><p>Copyright Example. Privacy. Cookie settings.</p></footer>
<div class="cookie-banner">We use cookies.</div>
</body></html>"""


def test_a_pages_text_keeps_the_header_lines_from_the_title_down_and_drops_the_chrome():
    text = page_text(HEADER_PAGE)
    lines = text.splitlines()
    assert lines[0] == "Programs Lead", "it starts at the job title"
    for kept in ("Oakfield, ST", "Full time", "Permanent", "Posted 10 Days Ago", "R-100123", "$100,000 - $120,000",
                 "Job Description:", "Lead the programs that matter, and say what that takes in a few plain words."):
        assert kept in text or any(kept in x for x in lines), kept
    for chrome in ("Skip to content", "Log in", "English", "Jobs", "Home / Jobs", "Similar jobs", "Other Role",
                   "Copyright", "cookies", "Careers at Example"):
        assert chrome not in text, chrome


def test_a_title_outside_the_main_content_is_put_in_front_of_it():
    page = ("<html><body><div class='top'><h1>Programs Lead</h1></div><main><p>Posted 2 days ago</p>"
            "<p>Lead the programs that matter, and say what that takes.</p></main></body></html>")
    assert page_text(page).splitlines()[:2] == ["Programs Lead", "Posted 2 days ago"]


def test_an_embedded_job_postings_description_gets_the_pages_header_block_in_front_of_it():
    ld = json.dumps({"@type": "JobPosting", "description": "<p>Lead the programs that matter.</p>"})
    page = (f'<html><head><script type="application/ld+json">{ld}</script></head><body><nav>Jobs</nav>'
            "<h1>Programs Lead</h1><ul><li>Oakfield, ST</li><li>Full time</li><li>Posted 10 Days Ago</li></ul>"
            "<div><p>Lead the programs that matter.</p></div><footer>Privacy</footer></body></html>")
    assert page_text(page) == ("Programs Lead\n- Oakfield, ST\n- Full time\n- Posted 10 Days Ago\n\n"
                               "Lead the programs that matter.")
    shell = (f'<html><head><script type="application/ld+json">{ld}</script></head>'
             '<body><div id="app"></div></body></html>')
    assert page_text(shell) == "Lead the programs that matter.", "a page that draws no header adds none"


def _page(body: str):
    return lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=body)


async def test_a_career_page_read_sets_the_posted_day_from_its_header_counted_from_the_read(js, monkeypatch):
    ids = await analysis_setup(text=None)
    web(monkeypatch, _page(HEADER_PAGE.replace("Lead the programs that matter, and say what that takes in a few "
                                               "plain words.", MARKUP)))
    today = datetime.now(UTC).date()
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE, found=today, posted=None)
    role = await _role(ids["Fabrikam"])
    assert role.description.startswith("Programs Lead") and "Posted 10 Days Ago" in role.description
    assert role.posted == today.fromordinal(today.toordinal() - 10)


async def test_the_schema_org_date_posted_sets_the_day_and_a_found_day_does_not_hold_it_back(js, monkeypatch):
    ids = await analysis_setup(text=None)
    ld = json.dumps({"@type": "JobPosting", "title": "Programs Lead", "description": MARKUP,
                     "datePosted": "2026-10-07"})
    web(monkeypatch, _page(f'<html><head><script type="application/ld+json">{ld}</script></head><body></body></html>'))
    await put_in_line(ids["Fabrikam"], url=CAREER_PAGE, source_url=CAREER_PAGE, found=date(2026, 10, 2), posted=None)
    role = await _role(ids["Fabrikam"])
    assert role.description == WHOLE and role.posted == date(2026, 10, 7)


async def test_a_capture_of_a_page_by_the_browser_sets_the_posted_day_counted_from_its_read(js):
    ids = await analysis_setup(text=None)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.found, role.posted = date(2026, 10, 15), None
        role.source_url, role.source_kind = CAREER_PAGE, "employer"
        body = "Programs Lead\nOakfield, ST\nPosted 3 days ago\n" + WHOLE
        assert await posting_text.take(session, role, posting_text.Capture(body),
                                       now=datetime(2026, 10, 15, 9, 0, tzinfo=UTC)) == posting_text.FETCHED
        assert role.posted == date(2026, 10, 12)


async def test_the_backfill_fills_roles_with_none_from_their_postings_and_texts_and_a_dry_run_changes_nothing(js):
    ids = await analysis_setup(text=None)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.posted, role.found = None, date(2026, 10, 14)
        role.description, role.description_at = "Programs Lead\nPosted 3 days ago\n" + WHOLE, READ
        await session.commit()
    async with db.session_factory()() as session:
        dry = await repairs.backfill_posted(session, OWNER, dry_run=True)
        assert dry.filled >= 1
    assert (await _role(ids["Fabrikam"])).posted is None
    async with db.session_factory()() as session:
        done = await repairs.backfill_posted(session, OWNER)
        await session.commit()
        assert done.filled == dry.filled
    assert (await _role(ids["Fabrikam"])).posted == date(2026, 10, 12)
    async with db.session_factory()() as session:
        left = await session.scalars(select(Opportunity).where(
            Opportunity.owner == OWNER, Opportunity.posted.is_(None)))
        assert all(not (r.description or "").strip() for r in left)


async def test_a_header_only_change_needs_no_research_again_and_a_changed_description_does(js, monkeypatch):
    ids = await analysis_setup(text=None)
    body = HEADER_PAGE.replace("Lead the programs that matter, and say what that takes in a few plain words.", MARKUP)
    new = page_text(body)
    old = "Job Description:" + new.split("Job Description:", 1)[1]
    changed = "Job Description:\nWords of an older posting that the page no longer says."
    today = datetime.now(UTC).date()
    async with db.session_factory()() as session:
        for company, text in (("Fabrikam", old), ("Initech", changed)):
            role = await session.get(Opportunity, ids[company])
            role.description, role.description_at = text, READ
            role.analysis_state, role.analysed_at = analysis.DONE, READ + timedelta(hours=1)
            role.found, role.posted = today, None
        await session.commit()
    web(monkeypatch, _page(body))
    async with db.session_factory()() as session:
        dry = await repairs.reextract_posting_header(session, OWNER, dry_run=True)
        assert (dry.candidates, dry.header_only, dry.reanalysed) == (2, 1, 1)
    assert (await _role(ids["Fabrikam"])).description == old, "a dry run changes nothing"
    async with db.session_factory()() as session:
        done = await repairs.reextract_posting_header(session, OWNER)
        await session.commit()
    assert (done.header_only, done.reanalysed, done.kept) == (1, 1, 0)
    kept = await _role(ids["Fabrikam"])
    assert kept.description == new and kept.description_at == READ, "the same description, read when it was read"
    assert kept.analysis_state == analysis.DONE, "no research again"
    assert kept.posted == today - timedelta(days=10), "the posted day comes from the kept header"
    moved = await _role(ids["Initech"])
    assert moved.description == new and moved.analysis_state == analysis.WAITING, "a changed description is queued"


async def test_the_header_re_read_keeps_the_text_a_role_holds_when_the_page_is_another_jobs(js, monkeypatch):
    """The page a role's source gives is the role's text only when it names the role, here too: a
    longer text from another job's page does not replace the posting the role holds (docs/PRD.md 39.29)."""
    ids = await analysis_setup(text=None)
    old = "Programs Lead\nJob Description:\nWords of the posting the role holds."
    other = HEADER_PAGE.replace("<h1>Programs Lead</h1>", "<h1>Warehouse Associate</h1>").replace(
        "Lead the programs that matter, and say what that takes in a few plain words.", MARKUP)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.description, role.description_at = old, READ
        await session.commit()
    web(monkeypatch, _page(other))
    async with db.session_factory()() as session:
        done = await repairs.reextract_posting_header(session, OWNER)
        await session.commit()
    assert (done.header_only, done.reanalysed, done.kept) == (0, 0, 1)
    assert (await _role(ids["Fabrikam"])).description == old
