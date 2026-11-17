"""Reading in the app's own browser (docs/PRD.md 30.8, the browser work spec): what a scan or map item carries, the
postings a worker hands in (validated whole, stored in the posting index, de-duplicated by link and by company and
title, with no cap on how many), the runbooks the Pathfinder hands in, the portals, the instructions, and the mission.
The queue itself is test_browser_work.py and the door's token test_browser_door.py. Every site, company and posting here
is made up."""

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import db
from app.harness import instructions as hi
from app.jobsearch import actions
from app.jobsearch.browser import items as bd
from app.jobsearch.browser import leases as bl
from app.jobsearch.browser import queue as bq
from app.jobsearch.browser import reader as br
from app.jobsearch.gating import sift
from app.jobsearch.missions import core as missions
from app.jobsearch.models import BrowserItem, JobPosting, MissionRun, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.gates import location_says
from app.models.work import Notification, Task
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, payload_chars
from app.sites import skills, where, where_view
from app.sites.models import Lesson, Site
from tests.builders import (
    ONE,
    PATHFINDER,
    TWO,
    WORKER,
    browser_card,
    browser_world,
    hand_back,
    in_session,
    lease,
    mission_of,
    scan_hand_in,
)
from tests.jobsearch.example_world import PORTAL, PORTAL_HOST, PORTAL_KEY, example_world, portal_card  # noqa: F401
from tests.support import LOCATIONS, POSTING_WORDS, PROFILE, browser_recipe, learn_sites, load_profile, pin_clock

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out", "example_world")

OWNER = "owner"
#: What is typed into a site's location box: where the owner would work on site, then Remote.
PLACES = [*LOCATIONS, "Remote"]
NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
#: 8 pm on 5 November in New York, which is 1 am on the 6th in UTC: the owner's day is a day behind the UTC day.
EVENING = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)


def _site_of(card) -> str:
    return br.host_of(card.get("site")) if isinstance(card, dict) and card.get("site") else ONE


async def hand(postings=None, now: datetime = NOW, **kw) -> dict:
    """Hand these postings in from scans of their sites, each scan leased to a session of its own, and add up the
    replies."""
    groups: dict[str, list] = {}
    for card in postings or []:
        groups.setdefault(_site_of(card), []).append(card)
    total: dict = {"handed": 0, "new": 0, "known": 0, "open_these": []}
    for site, cards in groups.items():
        reply = await scan_hand_in(cards, site=site, sid=f"browser_reader-{uuid.uuid4().hex[:8]}", now=now, **kw)
        for key in ("handed", "new", "known"):
            total[key] += reply[key]
        total["open_these"] += reply["open_these"]
        total["next"] = reply["next"]
    return total


async def offered(kind: str, host: str, sid: str = WORKER, now: datetime = NOW) -> dict:
    """The item a session is sent for this site, as `browser.next_item` sends it."""
    item_id = await lease(kind, host, sid, now)

    async def offer(session):
        row = await session.get(BrowserItem, uuid.UUID(item_id))
        entry = await bl.session_run(session, OWNER, sid, now)
        entry.started = {**entry.started, "instructed": False}  # as a session's first item
        return await bd._offer(session, OWNER, row, entry, now)

    return await in_session(sid, offer)


async def mapping(host: str = ONE, now: datetime = NOW) -> str:
    """The Pathfinder holds the site's map item."""
    return await lease("map", host, PATHFINDER, now)


async def map_postings(postings, **kw) -> dict:
    return await in_session(PATHFINDER, lambda s: bd.postings(s, OWNER, postings, now=NOW, **kw))


async def map_skill(site, recipe, **kw) -> dict:
    return await in_session(PATHFINDER, lambda s: bd.skill(s, OWNER, site, recipe, now=NOW, **kw))


async def a_site(session, host: str, **fields) -> Site:
    """One more site to read in the browser (made up), switched on a given time ago."""
    site = Site(owner=OWNER, url=f"https://{host}/jobs", kind=fields.pop("kind", "job_board"), read_by="browser",
                created_at=NOW, **fields)
    session.add(site)
    await session.flush()
    return site


# --- addresses and dates ------------------------------------------------------------------------------------------


def test_a_posting_is_known_by_its_own_address_without_tracking():
    key, clean = br.canonical("HTTPS://WWW.Listings.Example.test/jobs/view/42/?utm_source=x&trackingId=ab&refId=9#top")
    assert clean == "https://listings.example.test/jobs/view/42" and key == "listings.example.test/jobs/view/42"
    # a parameter that names the posting stays, in a fixed order
    assert br.canonical("https://x.example.test/viewjob?vjk=b&jk=a&from=serp")[1] == (
        "https://x.example.test/viewjob?jk=a&vjk=b")
    for bad in ("", "listings.example.test/jobs", "ftp://x.example.test/a", "javascript:alert(1)"):
        with pytest.raises(br.BrowserError):
            br.canonical(bad)


def test_a_job_systems_client_id_is_part_of_its_postings_address_though_a_campaign_id_elsewhere_is_not():
    system = f"https://recruit.{PORTAL_HOST}/mascsr/default/mdf/recruitment/recruitment.html"
    kept = br.canonical(f"{system}?cid=0a1b2c3d&ccId=19000101_000001&jobId=7&utm_medium=x")[1]
    assert kept == f"{system}?ccId=19000101_000001&cid=0a1b2c3d&jobId=7", "cid names the employer: without it, no page"
    elsewhere = br.canonical("https://x.example.test/job/7?cid=newsletter&jobId=7")[1]
    assert elsewhere == "https://x.example.test/job/7?jobId=7"


@pytest.mark.parametrize("text, expected", [
    ("2026-10-07", date(2026, 10, 7)), ("Posted Oct 7, 2026", date(2026, 10, 7)), ("Today", date(2026, 10, 9)),
    ("Just now", date(2026, 10, 9)), ("Yesterday", date(2026, 10, 8)), ("3 hours ago", date(2026, 10, 9)),
    ("Posted 2 days ago", date(2026, 10, 7)), ("Reposted 1 week ago", date(2026, 10, 2)),
    ("30+ days ago", date(2026, 9, 9)), ("2 months ago", date(2026, 8, 10)), ("", None),
    ("Be an early applicant", None), ("2026-13-45", None),
])
def test_a_sites_posted_text_means_a_day_or_nothing(text, expected):
    assert br.posted_date(text, date(2026, 10, 9)) == expected


#: 2 am on 7 November in Denver (MST, UTC-7), which is 9 am in UTC.
SMALL_HOURS = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def test_a_sites_posted_hours_ago_are_counted_back_from_the_instant_of_the_read(monkeypatch):
    """Regression case: "8 hours ago" read at 2 am was the same day, but it was posted the evening
    before. An instant gives the owner's day of the moment the hours reach; a bare day keeps the old count."""
    pin_clock(monkeypatch, SMALL_HOURS, "America/Denver")
    assert br.posted_date("Posted 8 hours ago", SMALL_HOURS) == date(2026, 11, 6)
    assert br.posted_date("Posted 90 minutes ago", SMALL_HOURS) == date(2026, 11, 7)
    assert br.posted_date("Posted 8 hours ago", date(2026, 11, 7)) == date(2026, 11, 7)


def test_a_listing_read_at_two_in_the_morning_that_says_8_hours_ago_is_dated_the_day_before(monkeypatch):
    pin_clock(monkeypatch, SMALL_HOURS, "America/Denver")
    posting = SimpleNamespace(
        posted=None, text=None, detail_at=None, links=[], closed_at=None, not_checked_at=None, location=None,
        work_mode=None, comp=None, apply_url=None, first_seen=SMALL_HOURS, last_seen=SMALL_HOURS)
    sighting = {"location": None, "work_mode": None, "comp": None, "apply_url": None, "posted_text": "8 hours ago",
                "description": None, "snippet": None, "url": "https://jobs.example.test/1"}
    br._fill(posting, sighting, SMALL_HOURS, reopen=False)
    assert posting.posted == date(2026, 11, 6)


# --- what a scan item carries ---------------------------------------------------------------------------------------


async def test_a_scan_item_carries_the_site_its_runbook_the_search_and_the_links_seen(js):
    await browser_world()
    got = await offered("scan", ONE)
    item = got["item"]
    assert (item["site"], item["name"], item["start_urls"]) == (ONE, "Listings", [f"https://www.{ONE}/jobs/"])
    assert item["runbook"]["start_url"] == f"https://www.{ONE}/jobs"
    assert item["search"] == {"phrases": ["Director of Programs", "Operations", "Program Operations"],
                              "places": PLACES, "window_hours": 24}, (
        "the example titles of each target role, one role at a time; the old search plan is not read")
    assert item["seen_recently"] == [] and item["batch"] == 25 and item["results"] == ["done", "blocked", "failed"]
    assert "Easy Apply" in got["instructions"] and "never" in got["instructions"].lower()
    assert "browser.postings" in got["response_template"] or "postings:" in got["response_template"]
    assert payload_chars(got) < AGENT_PAYLOAD_BUDGET_CHARS


async def test_without_target_roles_there_are_no_phrases_and_remote_is_added_when_it_counts(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={"company": "A small agency.", "search_plan": ["no longer read"]})
        await missions.resume(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        site = Site(owner=OWNER, url=f"https://{ONE}/", kind="job_board", read_by="browser", created_at=NOW)
        session.add(site)
        await session.flush()
        await learn_sites(session, site)
        await session.commit()
    assert (await offered("scan", ONE))["item"]["search"] == {"phrases": [], "places": PLACES, "window_hours": 24}


async def test_remote_is_left_out_when_the_work_location_gate_turns_remote_off_and_the_places_come_from_it(js):
    location = {**PROFILE["gates"]["location"], "remote": {"on": False, "locations": []}}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={**PROFILE["gates"], "location": location})
        await missions.resume(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        site = Site(owner=OWNER, url=f"https://{ONE}/", kind="job_board", read_by="browser", created_at=NOW)
        session.add(site)
        await session.flush()
        await learn_sites(session, site)
        await session.commit()
    assert (await offered("scan", ONE))["item"]["search"]["places"] == [
        "Oakfield", "Riverton", "Maple Heights", "Springdale", "Lakeside, IL"], "the locations they would work from"


async def test_the_worker_is_never_sent_to_a_site_the_owner_avoids(js):
    await browser_world()
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://jobs.avoid.example.test/", kind="job_board", read_by="browser",
                         created_at=NOW + timedelta(hours=1)))  # a subdomain of the avoided host, wrongly marked
        await session.commit()
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, NOW))
    async with db.session_factory()() as session:
        scans = sorted((await session.scalars(select(BrowserItem.target).where(BrowserItem.kind == "scan"))).all())
    assert scans == sorted([ONE, TWO])
    with pytest.raises(br.BrowserError, match="is not one of the sites to read"):
        await scan_hand_in([browser_card(1, site="jobs.avoid.example.test", url="https://jobs.avoid.example.test/view/1")],
                           now=NOW)


async def test_a_scan_lists_links_seen_lately_newest_first_and_leaves_out_old_ones_and_other_sites(js):
    ids = await browser_world()
    async with db.session_factory()() as session:
        for n, age in enumerate((1, 3, 20)):  # the third was last seen over SEEN_DAYS ago
            session.add(JobPosting(
                owner=OWNER, employer_id=ids[ONE], ats="browser", ext_id=f"k{n}", match_key=f"c{n}|t",
                company=f"C{n}", title="T", url=f"https://{ONE}/jobs/view/{n}", first_seen=NOW - timedelta(days=30),
                last_seen=NOW - timedelta(days=age), links=[]))
        session.add(JobPosting(  # another site's: not this site's links
            owner=OWNER, employer_id=ids[TWO], ats="browser", ext_id="o", match_key="o|t", company="O", title="T",
            url=f"https://{TWO}/job/1", first_seen=NOW, last_seen=NOW, links=[]))
        await session.commit()
    assert (await offered("scan", ONE))["item"]["seen_recently"] == [
        f"https://{ONE}/jobs/view/0", f"https://{ONE}/jobs/view/1"]


async def test_a_long_list_of_seen_links_is_cut_to_what_an_item_may_hold(js):
    ids = await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        session.add_all(JobPosting(
            owner=OWNER, employer_id=ids[ONE], ats="browser", ext_id=f"k{n}", match_key=f"c{n}|t", company="C",
            title="T", url=f"https://{ONE}/jobs/view/{'9' * 300}{n}", first_seen=NOW,
            last_seen=NOW - timedelta(minutes=n), links=[]) for n in range(400))
        await session.commit()
    got = await offered("scan", ONE)
    assert 0 < len(got["item"]["seen_recently"]) <= br.SEEN_SHOWN
    assert payload_chars(got) <= AGENT_PAYLOAD_BUDGET_CHARS


# --- the instructions ---------------------------------------------------------------------------------------------


def test_the_instructions_name_the_item_loop_every_result_and_the_template_loads():
    text = hi.read("instructions", "browser.reader")
    for rule in ("register or manage an account", "Easy Apply", "CAPTCHA", "do not fill in forms", "seen_recently",
                 "open_these", "just text", "never judge whether a role fits"):
        assert rule.lower() in text.lower(), rule
    for tool in ("browser.next_item", "browser.hand_back", "browser.heartbeat", "browser.postings", "wait_seconds",
                 "`stop`", "`found`", "`easy_apply`", "`not_found`", "`gone`", "`blocked`", "`text`",
                 "`unreadable`", "`done`", "`failed`", "added_by_link", "`batch`", "`url=`",
                 "browser.page_digest", "take_snapshot", "timebox_minutes", "timed out"):
        assert tool in text, tool
    for gone in ("browser.plan", "browser.runbook", "browser.more_roles", "browser.texts", "browser.source",
                 "postings_left", "to_source", "read_in_full", "read_at_source", "off_recipe", "slowed",
                 "Take as long as you need"):
        assert gone not in text, gone
    assert "A job board's address is never `found`" in text
    assert "never sign in" not in text and "log in" not in text.lower()  # sign-in rules dropped, 2026-10-28
    assert hi.placeholders(text) == set() and hi.placeholders(hi.read("templates", "browser.reader.postings")) == set()
    assert hi.word_count(text) <= hi.REVIEW_WORDS
    learner = hi.read("instructions", "browser.learner")
    for rule in ("Sign up", "Easy Apply", "CAPTCHA", "fill in or submit a form", "real stop", "seen_recently",
                 "open_these", "browser.skill", "browser.next_item", "browser.hand_back", "browser.heartbeat",
                 "page_says", "lessons", "learn_note", "`done`", "`blocked`", "`failed`", "runbook_template",
                 "browser.page_digest", "take_snapshot", "no time box"):
        assert rule.lower() in learner.lower(), rule
    for gone in ("browser.plan", "browser.runbook", "off_recipe", "`slowed`"):
        assert gone not in learner, gone
    assert hi.placeholders(learner) == set() and hi.placeholders(hi.read("templates", "browser.learner.skill")) == set()
    assert hi.word_count(learner) <= hi.REVIEW_WORDS
    # a careers page that hands its openings off to a portal (docs/PRD.md 37.12): the Pathfinder is told how far to go
    for rule in ("portal", "portals", "job system", "once", "search engine", "job board", "another employer's listing",
                 "blocked"):
        assert rule.lower() in learner.lower(), rule
    assert "portals" in hi.read("templates", "browser.learner.skill")
    for rule in ("search address", "address bar", "{phrase}", "{place}", "not a block", "work type in the phrase",
                 "runbook (section 7)"):
        assert rule.lower() in learner.lower(), rule
    assert "bookmarks" in hi.read("templates", "browser.learner.skill")
    template = hi.read("templates", "browser.reader.postings")
    for word in ("item_id", "result", "easy_apply", "source_kind", "never a job board's", "never shortened"):
        assert word in template, word


def test_the_rules_the_cookie_notice_change_trimmed_the_text_around_are_all_still_there():
    """Found in review, 2026-11-04: making room for the cookie-notice rule under `REVIEW_WORDS` dropped three rules with
    the explanations; the room comes from the explanations only."""
    reader = hi.read("instructions", "browser.reader").lower()
    assert "never select, navigate or close a page you did not open" in reader
    assert "never open a new page per posting" in reader
    learner = hi.read("instructions", "browser.learner").lower()
    assert "never open a new page per posting" in learner
    assert "with `evaluate_script` and a `settimeout` promise), its only use" in learner
    assert "no time box or turn limit" in learner


def test_both_sessions_are_told_to_keep_the_apps_pace_and_never_to_disguise_the_browser():
    """The pace of a polite visitor (docs/PRD.md 37.14), kept by the app (the owner, 2026-10-16: one page every 5 to 12
    seconds per site): both texts call browser.heartbeat before every page load and wait what it says, one tab, one
    page at a time. Nothing in either text hides that a program is driving the browser."""
    for name in ("browser.reader", "browser.learner"):
        text = hi.read("instructions", name).lower()
        for rule in ("browser.heartbeat", "wait_ms", "one tab", "evaluate_script", "internal apis", "rate-limit",
                     "unusual activity", "never use `isolatedcontext`", "never save a snapshot", "close any extra tab"):
            assert rule in text, (name, rule)
        for hiding in ("user agent", "user-agent", "fingerprint", "proxy", "random mouse", "stealth", "evade"):
            assert hiding not in text, (name, hiding)
    reader = hi.read("instructions", "browser.reader").lower()
    for rule in ("50-150 ms", "don't retry", "rather than paging to the end", "one item at a time"):
        assert rule in reader, rule
    learner = hi.read("instructions", "browser.learner").lower()
    for rule in ("no page limit", "real stop", "is not a stop: try another way", "one page at a time",
                 "fully loaded"):
        assert rule in learner, rule
    assert "note it, do not read it" in learner and "without signing in" not in learner


# --- handing postings in ------------------------------------------------------------------------------------------


async def test_a_batch_with_bad_postings_is_refused_whole_with_every_problem_named(js):
    await browser_world()
    bad = [
        {**browser_card(1), "colour": "red", "work_mode": "sometimes"},
        {**browser_card(2), "title": ""},
        {**browser_card(3), "site": "elsewhere.example.test"},
        {**browser_card(4), "url": "https://other.example.test/jobs/view/1"},
        {**browser_card(5), "url": "not a link"},
        {**browser_card(6), "company": "x" * 201},
        {**browser_card(7), "apply_url": "javascript:void(0)"},
        "a string",
    ]
    with pytest.raises(br.BrowserError) as raised:
        await scan_hand_in(bad, now=NOW)
    words = str(raised.value)
    for fragment in ("posting 1: unknown field colour", "posting 1: work_mode 'sometimes'",
                     "posting 2: title is required", "posting 3: 'elsewhere.example.test' is not one of the sites",
                     f"posting 4: url is not on {ONE}", "posting 5: url:", "posting 6: company is over 200",
                     "posting 7: apply_url:", "posting 8: send each posting as an object"):
        assert fragment in words, fragment
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0, "refused whole: none stored"
    for batch in ([browser_card(n) for n in range(26)], [], "x"):
        with pytest.raises(br.BrowserError):
            await scan_hand_in(batch, now=NOW)


async def _posting_titled(title: str) -> JobPosting:
    async with db.session_factory()() as session:
        return (await session.scalars(select(JobPosting).where(JobPosting.title == title))).one()


async def test_a_listings_posted_words_count_from_the_owners_day_of_the_read_not_its_utc_day(js, monkeypatch):
    """At 8 pm in New York the UTC day is already tomorrow, so "2 days ago"
    read then said a day too young and a description that says "Posted today" was dated tomorrow (docs/PRD.md path
    40)."""
    pin_clock(monkeypatch, EVENING, "America/New_York")
    await browser_world()
    await hand([browser_card(1, posted="2 days ago"),
                browser_card(2, description="Programs Lead\nPosted today\n" + POSTING_WORDS)], now=EVENING)
    assert (await _posting_titled("Programs Director 1")).posted == date(2026, 11, 3)
    assert (await _posting_titled("Programs Director 2")).posted == date(2026, 11, 5)


async def test_a_posting_seen_again_counts_its_listings_words_from_the_owners_day_of_that_read(js, monkeypatch):
    pin_clock(monkeypatch, NOW, "America/New_York")
    await browser_world()
    await hand([browser_card(1)], now=NOW)
    assert (await _posting_titled("Programs Director 1")).posted is None
    pin_clock(monkeypatch, EVENING, "America/New_York")
    await hand([browser_card(1, posted="2 days ago")], now=EVENING)
    assert (await _posting_titled("Programs Director 1")).posted == date(2026, 11, 3)


async def test_a_posting_seen_again_is_held_to_the_owners_day_it_was_first_seen_not_its_utc_day(js, monkeypatch):
    """First seen at 8 pm on the 5th in New York (the 6th in UTC); read again on the 7th, a page that says "Posted
    today" is two owner's days after the first sighting, past the one day of slack, so it names no day."""
    pin_clock(monkeypatch, EVENING, "America/New_York")
    await browser_world()
    await hand([browser_card(1)], now=EVENING)
    again = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
    pin_clock(monkeypatch, again, "America/New_York")
    await hand([browser_card(1, description="Programs Lead\nPosted today\n" + POSTING_WORDS)], now=again)
    assert (await _posting_titled("Programs Director 1")).posted is None


async def test_a_new_listing_that_says_hours_ago_read_at_two_in_the_morning_is_stored_with_the_day_before(
        js, monkeypatch):
    """Regression case: the path every first sighting takes (`_store`) took its posted day
    from the read's day alone, so "8 hours ago" read at 2 am in Denver was stored as today. An instant counts the hours
    back across the owner's midnight (docs/PRD.md path 40); 90 minutes before 2 am is still the same day."""
    pin_clock(monkeypatch, SMALL_HOURS, "America/Denver")
    await browser_world()
    await hand([browser_card(1, posted="8 hours ago"), browser_card(2, posted="90 minutes ago")], now=SMALL_HOURS)
    assert (await _posting_titled("Programs Director 1")).posted == date(2026, 11, 6)
    assert (await _posting_titled("Programs Director 2")).posted == date(2026, 11, 7)


async def test_new_postings_enter_the_index_like_a_boards_with_the_link_the_site_gave_and_the_text_it_showed(js):
    ids = await browser_world()
    reply = await hand([
        browser_card(1, work_mode="Hybrid", pay="$120,000 - $150,000 a year", posted="2 days ago",
                     url=f"https://www.{ONE}/jobs/view/1001/?trackingId=zz#x", apply_url="https://apply.example.test/a?b=1"),
        browser_card(2, description=POSTING_WORDS.replace("\n\n", "\n\n\n\n")),
        {"site": f"https://www.{TWO}/search", "url": f"https://{TWO}/job/77", "title": "Operations Lead",
         "company": "Hooli"}])
    assert (reply["handed"], reply["new"], reply["known"]) == (3, 3, 0)
    async with db.session_factory()() as session:
        rows = {p.title: p for p in (await session.scalars(select(JobPosting))).all()}
    one, two, three = rows["Programs Director 1"], rows["Programs Director 2"], rows["Operations Lead"]
    assert one.ats == "browser" and one.employer_id == ids[ONE] and three.employer_id == ids[TWO]
    assert one.url == f"https://{ONE}/jobs/view/1001" and one.ext_id == f"{ONE}/jobs/view/1001"
    assert one.match_key == "initech 1|programs director 1" and one.company == "Initech 1"
    assert (one.work_mode, one.comp, one.location) == ("hybrid", "$120,000 - $150,000 a year", "Springdale")
    assert one.posted == date(2026, 10, 7) and one.apply_url == "https://apply.example.test/a?b=1"
    assert one.text == "Lead the programs team, number 1." and one.detail_at is None
    assert two.text == POSTING_WORDS and two.detail_at is not None, "kept whole, its blank runs folded"
    assert one.first_seen == one.last_seen == NOW and one.closed_at is None
    assert one.filtered_at is None and one.route is None, "left for the filter, like any posting"
    assert three.text is None and three.work_mode is None
    # a new posting with no description is worth opening; one handed in with its description is not
    assert reply["open_these"] == [one.url, three.url] and "open_these" in reply["next"]


async def test_the_same_posting_again_is_known_keeps_its_first_sighting_and_grows_its_text(js):
    await browser_world()
    await hand([browser_card(1, pay="$100 an hour")])
    later = NOW + timedelta(days=2)
    reply = await hand([browser_card(1, url=f"https://www.{ONE}/jobs/view/1001?refId=other", location="Elsewhere",
                                     pay="ignored", description=POSTING_WORDS)], now=later)
    async with db.session_factory()() as session:
        [posting] = (await session.scalars(select(JobPosting))).all()
    assert (reply["new"], reply["known"], reply["open_these"]) == (0, 1, [])
    assert posting.first_seen == NOW and posting.last_seen == later, "first seen stays, last seen moves"
    assert posting.text == POSTING_WORDS and posting.detail_at == later
    assert (posting.location, posting.comp) == ("Springdale", "$100 an hour"), "what it had is not overwritten"


async def test_links_already_held_are_dropped_before_the_role_lookups_a_new_posting_needs(js, monkeypatch):
    from app.jobsearch.postings import same_role

    await browser_world()
    await hand([browser_card(1), browser_card(2)])
    looked: list[str] = []
    real = same_role.known_roles

    async def known_roles(*a, **k):
        looked.append("roles")
        return await real(*a, **k)

    monkeypatch.setattr(same_role, "known_roles", known_roles)
    later = NOW + timedelta(days=1)
    again = await hand([browser_card(1), browser_card(2)], now=later)
    assert (again["new"], again["known"]) == (0, 2) and looked == [], "all known: no owner-role lookup at all"
    mixed = await hand([browser_card(1), browser_card(3)], now=later)
    assert (mixed["new"], mixed["known"]) == (1, 1) and looked == ["roles"], "one new: the lookup runs once"
    async with db.session_factory()() as session:
        seen = {p.title: p.last_seen for p in (await session.scalars(select(JobPosting))).all()}
    assert seen["Programs Director 1"] == later and seen["Programs Director 2"] == later


async def test_a_snippet_does_not_replace_a_description_and_a_longer_description_does(js):
    await browser_world()
    await hand([browser_card(1, description=POSTING_WORDS)])
    await hand([browser_card(1, snippet="A different snippet.")])
    async with db.session_factory()() as session:
        assert (await session.scalar(select(JobPosting))).text == POSTING_WORDS
    longer = POSTING_WORDS + "\n\nBenefits\nHealth cover, a pension and four weeks off every year."
    await hand([browser_card(1, description=longer)])
    async with db.session_factory()() as session:
        assert (await session.scalar(select(JobPosting))).text == longer


async def test_the_same_company_and_title_from_another_source_is_one_posting_with_one_more_link(js):
    ids = await browser_world()
    async with db.session_factory()() as session:
        session.add(JobPosting(  # the employer's own board read it first, and it has since closed there
            owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id="gh1",
            match_key="initech 1|programs director 1", company="Initech 1", title="Programs Director 1",
            url="https://boards.example.test/initech/1", first_seen=NOW - timedelta(days=9),
            last_seen=NOW - timedelta(days=3), closed_at=NOW - timedelta(days=2),
            links=["https://boards.example.test/initech/1"]))
        await session.commit()
    reply = await hand([browser_card(1, description=POSTING_WORDS)])
    assert (reply["new"], reply["known"]) == (0, 1)
    async with db.session_factory()() as session:
        [posting] = (await session.scalars(select(JobPosting))).all()
    assert posting.ats == "feedsys" and posting.employer_id != ids[ONE]
    assert posting.links == ["https://boards.example.test/initech/1", f"https://{ONE}/jobs/view/1001"]
    assert posting.last_seen == NOW and posting.first_seen == NOW - timedelta(days=9)
    assert posting.closed_at is not None, "another site still listing it does not reopen what its board closed"
    assert posting.text == POSTING_WORDS


async def test_a_role_the_owner_already_has_is_known_and_not_stored_again(js):
    await browser_world()
    async with db.session_factory()() as session:
        session.add(Opportunity(owner=OWNER, slug="initech-1", company="Initech 1", title="Programs Director 1",
                                match_key="initech 1|programs director 1", origin="scout"))
        await session.commit()
    reply = await hand([browser_card(1)])
    assert (reply["new"], reply["known"]) == (0, 1)
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0


async def test_a_role_the_owner_has_in_other_words_or_by_its_link_is_known_and_another_job_is_not(js):
    """The match key is exact, so a site's "Programs Director 1" was a new posting beside the owner's "Programs,
    Director 1" at "Initech 1, Inc."; a link with the same id at the same employer is the same posting whatever the
    title says, and the same id at another employer is not (a tracker row can carry the wrong employer's link)."""
    await browser_world()
    async with db.session_factory()() as session:
        session.add_all([
            Opportunity(owner=OWNER, slug="initech-1", company="Initech 1, Inc.", title="Programs, Director 1",
                        match_key="initech 1, inc.|programs, director 1", origin="tracker"),
            Opportunity(owner=OWNER, slug="other", company="Other Co", title="Something Else", origin="tracker",
                        match_key="other co|something else", url=f"https://www.{ONE}/jobs/view/500001"),
        ])
        await session.commit()
    reply = await hand([
        browser_card(1),  # the same words
        browser_card(2, company="Other Co, Inc.", title="Another Title",
                     url=f"https://www.{ONE}/jobs/view/500001/"),  # the same link, the same employer
        browser_card(3, company="Elsewhere", title="Another Title",
                     url=f"https://www.{ONE}/jobs/view/elsewhere-500001"),  # the same id, another employer
        browser_card(4, url=f"https://www.{ONE}/jobs/view/500002"),  # another job, another id
        browser_card(5, company="Initech 1", title="Senior Programs Director 1"),  # not the owner's title
    ])
    assert (reply["new"], reply["known"]) == (3, 2)
    async with db.session_factory()() as session:
        assert sorted(p.title for p in (await session.scalars(select(JobPosting))).all()) == [
            "Another Title", "Programs Director 4", "Senior Programs Director 1"]


async def test_a_job_the_owner_added_by_its_link_is_known_by_its_id_until_research_names_it(js):
    """The owner pasted a job's link and the analyst has not read it, so the role has no employer and no title but the
    link. The site lists the same job under its employer's name: it is the owner's role, not stored as a new posting
    (docs/PRD.md 24.10). Another job on the site, and the same id on another host, are new."""
    from app.jobsearch.models import ADDED_LINK, ORIGIN_OWNER

    await browser_world()
    async with db.session_factory()() as session:
        session.add(Opportunity(
            owner=OWNER, slug="added-link-1", company=ADDED_LINK, origin=ORIGIN_OWNER, status="second_impression",
            title=f"{ONE}/jobs/view/500001", match_key=f"added link|{ONE}/jobs/view/500001",
            url=f"https://{ONE}/jobs/view/500001"))
        await session.commit()
    reply = await hand([
        browser_card(1, url=f"https://www.{ONE}/jobs/view/500001/?trackingId=zz"),  # the same id on the same host
        browser_card(2, url=f"https://www.{ONE}/jobs/view/500002"),  # another job
        browser_card(3, site=TWO, url=f"https://{TWO}/jobs/view/500001"),  # the same id, another host
    ])
    assert (reply["new"], reply["known"]) == (2, 1)
    async with db.session_factory()() as session:
        assert sorted(p.title for p in (await session.scalars(select(JobPosting))).all()) == [
            "Programs Director 2", "Programs Director 3"]


async def test_the_same_company_and_title_in_other_words_from_another_source_is_one_posting(js):
    ids = await browser_world()
    async with db.session_factory()() as session:
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id="gh1",
            match_key="initech 1|programs, director 1", company="Initech 1", title="Programs, Director 1",
            url="https://boards.example.test/initech/1", first_seen=NOW - timedelta(days=9),
            last_seen=NOW - timedelta(days=3), links=["https://boards.example.test/initech/1"]))
        await session.commit()
    reply = await hand([browser_card(1, description=POSTING_WORDS), browser_card(2)])
    assert (reply["new"], reply["known"]) == (1, 1)
    async with db.session_factory()() as session:
        rows = {p.title: p for p in (await session.scalars(select(JobPosting))).all()}
    assert set(rows) == {"Programs, Director 1", "Programs Director 2"}
    board = rows["Programs, Director 1"]
    assert board.ats == "feedsys" and board.links == ["https://boards.example.test/initech/1",
                                                          f"https://{ONE}/jobs/view/1001"]
    assert board.text == POSTING_WORDS and rows["Programs Director 2"].employer_id == ids[ONE]


async def test_a_posting_twice_in_one_batch_is_stored_once(js):
    await browser_world()
    reply = await hand([browser_card(1), browser_card(1, url=f"https://{ONE}/jobs/view/1001/"), browser_card(2)])
    assert (reply["handed"], reply["new"], reply["known"]) == (3, 2, 1)
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 2


async def test_a_reappearing_posting_of_its_own_site_reopens(js):
    await browser_world()
    await hand([browser_card(1)])
    async with db.session_factory()() as session:
        (await session.scalar(select(JobPosting))).closed_at = NOW
        await session.commit()
    await hand([browser_card(1)])
    async with db.session_factory()() as session:
        assert (await session.scalar(select(JobPosting))).closed_at is None


async def test_every_new_posting_without_its_description_is_worth_opening_with_no_cap(js):
    await browser_world()
    reply = await hand([browser_card(n) for n in range(1, 21)])
    assert len(reply["open_these"]) == 20, "no limit on what is opened"
    reply = await hand([browser_card(21, description=POSTING_WORDS)])
    assert reply["open_these"] == [], "one handed in whole needs no opening"


async def test_a_description_that_is_not_whole_is_not_kept_with_a_new_posting(js):
    await browser_world()
    reply = await scan_hand_in([browser_card(1, description=POSTING_WORDS + "\n\n[... rest of the description "
                                                                            "omitted ...]")], now=NOW)
    assert reply["not_kept"][0]["url"] == f"https://{ONE}/jobs/view/1001"
    async with db.session_factory()() as session:
        assert (await session.scalar(select(JobPosting))).text == "Lead the programs team, number 1."


# --- handing in a runbook -----------------------------------------------------------------------------------------


async def test_a_runbook_sent_before_any_posting_from_the_site_is_refused_and_names_why(js):
    ids = await browser_world(learned=False)
    await mapping(ONE)
    with pytest.raises(br.BrowserError, match=f"no posting from {ONE} has been handed in this run"):
        await map_skill(ONE, browser_recipe(ONE))
    with pytest.raises(br.BrowserError, match=f"site must be {ONE}, the site of the item you hold"):
        await map_skill(TWO, browser_recipe(TWO))
    async with db.session_factory()() as session:
        assert await skills.current_skill(session, ids[ONE]) is None


async def test_a_runbook_is_kept_only_for_a_map_item(js):
    await browser_world()
    await lease("scan", ONE, WORKER, NOW)
    with pytest.raises(br.BrowserError, match="for a map item you hold"):
        await in_session(WORKER, lambda s: bd.skill(s, OWNER, ONE, browser_recipe(ONE), now=NOW))


async def test_a_runbook_whose_count_is_far_from_what_the_page_says_is_refused_and_a_near_one_is_kept(js):
    ids = await browser_world(learned=False, extra_sites=False)
    await mapping(ONE)
    await map_postings([browser_card(n) for n in (1, 2, 3)])
    with pytest.raises(br.BrowserError, match="the page says 30 results but 3 were handed in"):
        await map_skill(ONE, browser_recipe(ONE), page_says=30)
    with pytest.raises(br.BrowserError, match="page_says is the number of results"):
        await map_skill(ONE, browser_recipe(ONE), page_says="many")
    async with db.session_factory()() as session:
        assert await skills.current_skill(session, ids[ONE]) is None
    assert (await map_skill(ONE, browser_recipe(ONE), page_says=5))["kept"] is True, "within the slack"


async def test_a_bad_runbook_is_refused_with_every_reason_and_the_session_may_send_it_again(js):
    ids = await browser_world(learned=False, extra_sites=False)
    bad = browser_recipe(ONE, start_url="ftp://listings.example.test/jobs", code="x")
    bad["results"] = {**bad["results"], "fields": {"company": "the line under the title"}}
    await mapping(ONE)
    await map_postings([browser_card(1)])
    with pytest.raises(br.BrowserError) as raised:
        await map_skill(ONE, bad, lessons=["a", "b", "c", "d"])
    words = str(raised.value)
    for fragment in ("recipe: start_url", "recipe: results", "fields needs title", "code: is not part",
                     "lessons is a list of at most 3"):
        assert fragment in words, fragment
    with pytest.raises(br.BrowserError, match="kind must be browser"):
        await map_skill(ONE, {"kind": "list_page", "url": f"https://{ONE}/"})
    with pytest.raises(br.BrowserError, match="a recipe is a JSON object"):
        await map_skill(ONE, "text")
    with pytest.raises(br.BrowserError, match="lesson 1: one plain sentence"):
        await map_skill(ONE, browser_recipe(ONE), lessons=[" "])
    async with db.session_factory()() as session:
        assert await skills.current_skill(session, ids[ONE]) is None, "nothing kept"
    await map_skill(ONE, browser_recipe(ONE))
    async with db.session_factory()() as session:
        assert (await skills.current_skill(session, ids[ONE])).version == 1


async def test_an_accepted_runbook_is_the_sites_current_skill_with_its_lessons_and_the_tries_cleared(js):
    ids = await browser_world(learned=False)
    async with db.session_factory()() as session:
        site = await session.get(Site, ids[ONE])
        site.learn_tries, site.learn_note = 3, "the results never load"
        await session.commit()
    await mapping(ONE)
    await map_postings([browser_card(1), browser_card(2)])
    reply = await map_skill(ONE, browser_recipe(ONE), page_says=2,
                            lessons=["The location box wants a city.", " The location box wants a city. ",
                                     "Results show after Search is pressed."])
    assert reply["ok"] and reply["kept"] is True and reply["version"] == 1 and "hand_back" in reply["next"]
    async with db.session_factory()() as session:
        skill = await skills.current_skill(session, ids[ONE])
        assert (skill.version, skill.recipe["kind"], skill.made_by_project_id) == (1, "browser", None)
        site = await session.get(Site, ids[ONE])
        assert (site.learn_tries, site.learn_note) == (0, None)
        lessons = (await session.scalars(select(Lesson).order_by(Lesson.text))).all()
        assert [(m.text, m.applies_to, m.source, m.source_project_id) for m in lessons] == [
            ("Results show after Search is pressed.", "job_board", "learned in the browser 2026-10-09", None),
            ("The location box wants a city.", "job_board", "learned in the browser 2026-10-09", None)], (
            "the same sentence is recorded once")
        [run] = (await session.scalars(select(MissionRun))).all()
        assert run.started["learned"] == {ONE: 2}
    # a second runbook is the next version, and the first is kept as history, retired
    assert (await map_skill(ONE, browser_recipe(ONE, notes=["Changed."])))["version"] == 2
    async with db.session_factory()() as session:
        history = await skills.skills_of_site(session, ids[ONE])
    assert [(v.version, v.retired_at is not None) for v in history] == [(1, True), (2, False)]
    # the next map item hands the other site what was learned, as the lessons of its kind
    item = (await offered("map", TWO, "pathfinder-other"))["item"]
    assert sorted(item["lessons"]) == ["Results show after Search is pressed.", "The location box wants a city."]


async def test_a_runbook_whose_search_is_an_address_is_kept_and_a_scan_is_sent_it_as_written(js):
    """A site whose search boxes do not apply is learned by its own search address (docs/PRD.md 37.1, 37.3)."""
    ids = await browser_world(learned=False, extra_sites=False)
    address = f"https://www.{ONE}/jobs/search?keywords={{phrase}}&location={{place}}&f_TPR=r86400&sortBy=DD"
    search = {"how": "url", "url_template": address, "newest": "the address says sortBy=DD",
              "recency": {"control": "f_TPR in the address", "choices": ["r86400", "r604800"]}}
    notes = ["Add the word remote to each phrase: the Remote filter does not apply."]
    await mapping(ONE)
    await map_postings([browser_card(1)])
    with pytest.raises(br.BrowserError) as raised:  # the phrase has nowhere to go
        await map_skill(ONE, browser_recipe(
            ONE, search={**search, "url_template": f"https://www.{ONE}/jobs/search?location={{place}}"}))
    assert "url_template must hold {phrase}" in str(raised.value)
    async with db.session_factory()() as session:
        assert await skills.current_skill(session, ids[ONE]) is None, "nothing kept"
    kept = await map_skill(ONE, browser_recipe(ONE, search=search, notes=notes))
    assert kept["kept"] is True and kept["version"] == 1
    recipe = (await offered("scan", ONE))["item"]["runbook"]
    written = {k: v for k, v in recipe["search"].items() if v is not None}  # the boxes are left unset
    assert written == search and recipe["notes"] == notes


async def test_a_site_that_cannot_be_mapped_is_tried_five_times_and_then_the_app_switches_it_off_and_says_so(js):
    ids = await browser_world(learned=False, extra_sites=False)
    why = "the results never load"
    for day in range(4):
        at = NOW + timedelta(days=day)
        item_id = await mapping(ONE, at)
        await hand_back(PATHFINDER, item_id, "failed", now=at, note=why)
        async with db.session_factory()() as session:
            site = await session.get(Site, ids[ONE])
        assert (site.on, site.learn_tries, site.learn_note) == (True, day + 1, why)
    async with db.session_factory()() as session:
        [row] = await where_view.listing(session, OWNER, ids[ONE])
        assert row["status"] == f"Learning in your browser: {why}" and row["on"] is True
    at = NOW + timedelta(days=4)
    await hand_back(PATHFINDER, await mapping(ONE, at), "failed", now=at, note=why)
    async with db.session_factory()() as session:
        site = await session.get(Site, ids[ONE])
        assert (site.on, site.learn_tries, site.off_by) == (False, 5, "app")
        assert site.off_note == f"couldn't learn how to read it in your browser ({why})"
        assert (await session.scalars(select(Notification))).all() == [], "nothing the owner could do about it"
        assert await session.scalar(select(func.count()).select_from(Task)) == 0, "nothing is asked of the owner"
        [row] = await where_view.listing(session, OWNER, ids[ONE])
        assert row["status"] == f"Off: couldn't learn how to read it in your browser ({why})"
        assert await br.to_learn(session, OWNER) == {}, "an off site is not visited"
        # the owner switches it on: the count starts again and the next map tries
        await where.switch(session, OWNER, ids[ONE], True)
        await session.commit()
        site = await session.get(Site, ids[ONE])
        assert (site.on, site.learn_tries, site.learn_note, site.off_by, site.off_note) == (True, 0, None, None, None)
        assert list(await br.to_learn(session, OWNER)) == [ONE]


async def test_a_map_run_shows_on_the_missions_page_as_one(js):
    await browser_world(learned=False)
    item_id = await mapping(ONE)
    await map_postings([browser_card(1)])
    await map_skill(ONE, browser_recipe(ONE))
    await hand_back(PATHFINDER, item_id, "done", now=NOW)
    other = await mapping(TWO, NOW + timedelta(seconds=20))
    await hand_back(PATHFINDER, other, "failed", now=NOW + timedelta(seconds=20), note="no search box")
    async with db.session_factory()() as session:
        mission = await mission_of(session, OWNER, missions.BROWSER)
        detail = await missions.detail(session, OWNER, mission.id)
    assert detail["runs"][0]["summary"] == (f"Mapped Listings. Couldn't map {TWO}: no search box. Handed in 1 posting, "
                                            "1 new.")
    assert detail["runs"][0]["started_by"] == "browser" and detail["name"] == "Read in your browser"


async def test_a_scan_whose_runbook_no_longer_fits_retires_it_keeps_the_why_and_queues_a_map(js):
    ids = await browser_world()
    item_id = await lease("scan", ONE, WORKER, NOW)
    await hand_back(WORKER, item_id, "failed", now=NOW, note="the Date posted menu is gone")
    async with db.session_factory()() as session:
        assert await skills.current_skill(session, ids[ONE]) is None
        site = await session.get(Site, ids[ONE])
        assert site.learn_note == "the Date posted menu is gone" and site.learn_tries == 0
        assert list(await br.to_learn(session, OWNER)) == [ONE]
        [run] = (await session.scalars(select(MissionRun))).all()
    assert run.summary == "Listings's runbook didn't fit: the Date posted menu is gone; it will be mapped again."


# --- a careers page that hands its openings off to a portal (docs/PRD.md 37.12) -------------------------------------


async def test_a_careers_page_that_hands_off_to_a_portal_is_mapped_there_and_filed_under_the_site(js):
    ids = await browser_world(learned=False, extra_sites=False)
    await mapping(ONE)
    # until the session says the site hands off to a portal, what it reads there is not the site's
    with pytest.raises(br.BrowserError, match=f"url is not on {ONE}; send the posting's own address"):
        await map_postings([portal_card(1)])
    # it says so with the first batch: the site's host as the item gave it, the address as the page linked it
    reply = await map_postings([portal_card(1), portal_card(2)],
                               portals=[{"site": f"www.{ONE}", "url": f"{PORTAL}#openings"}])
    assert (reply["handed"], reply["new"], reply["known"]) == (2, 2, 0) and reply["portals"] == {ONE: PORTAL}
    async with db.session_factory()() as session:
        postings = (await session.scalars(select(JobPosting).order_by(JobPosting.url))).all()
        assert [p.url for p in postings] == [
            f"https://{PORTAL_HOST}/v4/ats/web.php/portal/{PORTAL_KEY}/jobs/{n}" for n in (501, 502)]
        assert {(p.employer_id, p.ats) for p in postings} == {(ids[ONE], "browser")}, "filed under the site, as ever"
        assert (await session.get(Site, ids[ONE])).portal_url is None, "recorded only when the runbook is kept"
    kept = await map_skill(ONE, browser_recipe(ONE), page_says=2)
    assert kept["kept"] is True and kept["portal"] == PORTAL
    async with db.session_factory()() as session:
        site = await session.get(Site, ids[ONE])
        assert site.portal_url == PORTAL and (site.learn_tries, site.learn_note) == (0, None)
        assert (await skills.current_skill(session, ids[ONE])).recipe["start_url"] == f"https://www.{ONE}/jobs"
    # the scan starts at the portal, with the links already seen there, and a scan is let in there
    later = NOW + timedelta(days=1)
    item = (await offered("scan", ONE, WORKER, later))["item"]
    assert (item["portal"], item["link"]) == (PORTAL, PORTAL) and item["start_urls"] == [f"https://www.{ONE}/jobs/"]
    assert item["domain"] == PORTAL_HOST, "the pace is kept where the pages load"
    assert set(item["seen_recently"]) == {p.url for p in postings}
    again = await scan_hand_in([portal_card(2), portal_card(3)], now=later)
    assert (again["new"], again["known"]) == (1, 1)


async def test_a_runbook_for_a_portal_is_proven_by_what_was_read_there_and_the_count_is_the_portals(js):
    ids = await browser_world(learned=False, extra_sites=False)
    await mapping(ONE)
    await map_postings([browser_card(1)], portals=[{"site": ONE, "url": PORTAL}])
    with pytest.raises(br.BrowserError, match=f"no posting from the portal {PORTAL_HOST} of {ONE} has been handed"):
        await map_skill(ONE, browser_recipe(ONE))
    await map_postings([portal_card(n) for n in (2, 3, 4)])
    with pytest.raises(br.BrowserError, match=f"30 results but 3 were handed in from the portal {PORTAL_HOST}"):
        await map_skill(ONE, browser_recipe(ONE), page_says=30)
    async with db.session_factory()() as session:
        assert await skills.current_skill(session, ids[ONE]) is None
        assert (await session.get(Site, ids[ONE])).portal_url is None, "nothing is recorded by a refusal"
    assert (await map_skill(ONE, browser_recipe(ONE), page_says=3))["kept"] is True
    async with db.session_factory()() as session:
        assert (await session.get(Site, ids[ONE])).portal_url == PORTAL


async def test_a_recorded_portal_needs_no_new_declaration_and_a_runbook_on_the_sites_own_pages_lets_it_go(js):
    ids = await browser_world(learned=False, extra_sites=False)
    async with db.session_factory()() as session:
        (await session.get(Site, ids[ONE])).portal_url = PORTAL  # left on the site by the runbook that stopped fitting
        await session.commit()
    assert (await offered("map", ONE, PATHFINDER))["item"]["portal"] == PORTAL, "the Pathfinder is told where it was"
    reply = await map_postings([portal_card(1)])  # nothing declared, and it is let in
    assert reply["new"] == 1 and "portals" not in reply
    assert (await map_skill(ONE, browser_recipe(ONE), page_says=1))["portal"] == PORTAL
    await map_postings([browser_card(1), browser_card(2)])  # now the employer lists them itself
    async with db.session_factory()() as session:
        entry = (await session.scalars(select(MissionRun))).one()
        entry.started = {**entry.started, "by_portal": {}, "portals": {}}
        (await session.get(Site, ids[ONE])).portal_url = PORTAL
        await session.commit()
    kept = await map_skill(ONE, browser_recipe(ONE), page_says=3)
    assert "portal" not in kept
    async with db.session_factory()() as session:
        assert (await session.get(Site, ids[ONE])).portal_url is None


async def test_a_portal_is_declared_only_while_mapping_and_only_on_a_job_system(js):
    await browser_world(learned=False)
    await mapping(ONE)

    async def refused(portals) -> str:
        with pytest.raises(br.BrowserError) as raised:
            await map_postings([], portals=portals)
        return str(raised.value)

    no_system = "is not a job system's address"
    wrong = {
        "a search engine": ("https://www.searchsite.test/search?q=initech+careers", no_system),
        "a job board": ("https://www.board.test/company/initech/jobs", no_system),
        "another employer's own site": ("https://careers.other.example.test/", no_system),
        "a look-alike": (f"https://{PORTAL_HOST}.example.test/v4/ats/web.php/jobs", no_system),
        "the site itself": (f"https://careers.{ONE}/openings", f"{ONE} is the site itself"),
        "no address": ("a job system portal", "is not a web address"),
        "a sign-in": ("https://tester:secret@portal.example.test/v4/ats/web.php/jobs", "user name or password"),
        "a token": (f"{PORTAL}?token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlc2lnbmF0dXJl",
                    "carries a token or key"),
    }
    for what, (url, fragment) in wrong.items():
        assert fragment in await refused([{"site": ONE, "url": url}]), what
    assert "portals 1: site must be one of" in await refused([{"site": TWO, "url": PORTAL}]), "not the item's site"
    assert "portals is a list of at most 10" in await refused([{"site": ONE, "url": PORTAL}] * 11)
    guid = "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"
    for url in (PORTAL, f"https://recruiting.{PORTAL_HOST}/EXA1000EXAMP/JobBoard/{guid}/?q=&o=postedDate",
                f"https://recruit.{PORTAL_HOST}/mascsr/default/mdf/recruitment/recruitment.html?cid={guid}&lang=en_US"):
        assert (await map_postings([], portals=[{"site": ONE, "url": url}]))["portals"] == {ONE: url}


async def test_a_scan_declares_no_portal(js):
    await browser_world()
    with pytest.raises(br.BrowserError, match="portals is for mapping a site"):
        await scan_hand_in([browser_card(1)], portals=[{"site": ONE, "url": PORTAL}], now=NOW)
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0


async def test_the_session_is_never_sent_to_a_portal_on_a_site_the_owner_avoids(js):
    ids = await browser_world(learned=False)
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url=f"https://{PORTAL_HOST}/", kind="job_board", read_by="browser",
                         list_name="avoid", created_at=NOW))
        await session.commit()
    await mapping(ONE)
    with pytest.raises(br.BrowserError, match=f"portals 1: {PORTAL_HOST} is a site the owner avoids"):
        await map_postings([], portals=[{"site": ONE, "url": PORTAL}])
    async with db.session_factory()() as session:  # and a site whose recorded portal is on one is left out
        (await session.get(Site, ids[ONE])).portal_url = PORTAL
        assert ONE not in await br.browser_sites(session, OWNER) and TWO in await br.browser_sites(session, OWNER)


# --- the order sites are mapped in (docs/PRD.md 37.13) ----------------------------------------------------------------


async def test_the_sites_never_tried_are_mapped_first_then_the_one_tried_longest_ago(js):
    await browser_world(learned=False, extra_sites=False)  # Listings: never tried
    day = timedelta(days=1)
    async with db.session_factory()() as session:
        for name, tried in (("failed-a", 2), ("failed-b", 5), ("fresh", None)):
            await a_site(session, f"{name}.example.test", kind="employer",
                         learn_tried_at=None if tried is None else NOW - tried * day)
        await session.commit()
    got = []
    for n in range(4):
        sid = f"pathfinder-{n}"
        reply = await in_session(sid, lambda s, n=n: bd.next_item(s, OWNER, NOW + n * timedelta(seconds=20)))
        if "item" in reply:
            got.append(reply["item"]["site"])
            await hand_back(sid, reply["item"]["item_id"], "failed", now=NOW + n * timedelta(seconds=20),
                            note="no search box")
    # one Pathfinder at a time on a domain: these made-up sites share one, so they go one after another
    assert got[0] in (ONE, "fresh.example.test") and got[-1] == "failed-a.example.test"
    assert got.index("failed-b.example.test") < got.index("failed-a.example.test"), "tried longest ago first"


# --- the mission itself -------------------------------------------------------------------------------------------


async def test_the_mission_starts_paused_with_no_limits_and_cannot_be_run_or_scheduled_here(js):
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        mission = await mission_of(session, OWNER, missions.BROWSER)
        assert (mission.state, mission.schedule, mission.playbook) == ("paused", {"kind": "outside"}, None)
        assert mission.settings == {}, "no posting cap and no run limit (the owner, 2026-10-16)"
        await missions.resume(session, mission, by="Tester")
        assert await missions.due(session, mission) is False, "even on, the clock never starts it"
        with pytest.raises(missions.MissionError, match="starts on your computer"):
            await missions.start(session, OWNER, mission, by="you")
        with pytest.raises(missions.MissionError, match="set on your computer"):
            await missions.change(session, mission, by="Tester", schedule={"kind": "daily", "at": "02:00"})
        assert missions.check_settings(missions.BROWSER, {"postings_a_run": 80, "opened_a_run": 4}) == {}, (
            "the old limits are dropped")


async def test_the_clock_never_starts_a_session_and_a_pause_from_the_screen_stops_the_next_one(js):
    await browser_world()
    async with db.session_factory()() as session:
        await missions.schedule_pass(session, OWNER)
        assert await br.open_runs(session, OWNER) == []
        await missions.pause(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester", why="away")
        await session.commit()
    stop = await in_session(WORKER, lambda s: bd.next_item(s, OWNER, NOW))
    assert stop["stop"] is True and "paused" in stop["why"]


# --- what the rest of the app does with a browser posting ------------------------------------------------------


async def test_the_filter_takes_a_browser_posting_like_any_other(js):
    await browser_world()
    await hand([browser_card(1, title="Director of Programs", location="Springdale", work_mode="onsite"),
                browser_card(2, title="Program Analyst", location="Greater Gotham", work_mode="onsite")])
    async with db.session_factory()() as session:
        profile = await profile_store.current(session, OWNER)
        result = await sift.sift(session, OWNER, profile, now=NOW)
        rows = {p.title: p for p in (await session.scalars(select(JobPosting))).all()}
    assert rows["Director of Programs"].filtered_by is None and rows["Director of Programs"].route == "roles"
    assert rows["Program Analyst"].filtered_by == location_says(profile.gates.location)
    assert result["looked_at"] == 2


async def test_a_posting_that_applies_on_an_avoided_site_is_kept_and_the_filter_stops_it_with_its_reason(js):
    await browser_world()
    await hand([browser_card(1, title="Director of Programs", apply_url="https://avoid.example.test/apply/1")])
    async with db.session_factory()() as session:
        await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)
        [posting] = (await session.scalars(select(JobPosting))).all()
    assert posting.filtered_by == "a site you avoid" and posting.route is None


async def test_a_browser_posting_with_no_text_is_not_fetched_by_the_app(js, monkeypatch):
    await browser_world()
    await hand([browser_card(1, snippet="")])
    async with db.session_factory()() as session:
        [posting] = (await session.scalars(select(JobPosting))).all()
        posting.filtered_at = NOW  # passed the filter with no text: a board's would be waiting for its description
        assert posting.text is None
        asked = []

        async def fill(_session, _fetch, waiting):
            asked.extend(waiting)
            return 0

        monkeypatch.setattr(actions.postings, "fill_details", fill)
        await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": 5})
    assert asked == [], "it would sit at the front of every night's list and keep the others out"
