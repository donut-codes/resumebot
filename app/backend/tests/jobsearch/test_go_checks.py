"""Go's checks before the tabs open (`go_checks.py`, `mail/reader.py`): the owner's mail for an application already sent
(a fake mailbox), the posting for whether it is still open (fixture pages), the refill to the batch's size, mail not set
up, and the night's pre-check. Synthetic roles, employers and mail only."""

from datetime import UTC, date, datetime, time, timedelta
from email.utils import format_datetime

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import go_checks
from app.jobsearch.browser.code import BrowserUnavailable, Page
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.mail import login as mail_login
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.mail import sync as mail_sync
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.pipeline import board
from app.models.work import TrailEvent
from app.timeutil import owner_zone
from tests.builders import counted, rank_roles
from tests.employer_builders import make_employer
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import OWNER, fake_go_window, pin_clock

pytestmark = pytest.mark.usefixtures("example_world")

TODAY = date.today()

OPEN_PAGE = "<html><body><h1>Product Lead</h1>" + "<p>Lead the product. Apply below.</p>" * 40 + "</body></html>"
GONE_TEXT_PAGE = ("<html><body><h1>Product Lead</h1><p>This job is no longer accepting applications.</p>"
                  "</body></html>")
EXPIRED_LD_PAGE = ('<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "Product Lead", '
                   f'"validThrough": "{TODAY - timedelta(days=3):%Y-%m-%d}T00:00"}}</script></head>'
                   "<body><p>Product Lead</p></body></html>")
LIVE_LD_PAGE = ('<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "Product Lead", '
                f'"validThrough": "{TODAY + timedelta(days=30):%Y-%m-%d}"}}</script></head>'
                "<body><p>Product Lead</p></body></html>")
PAGES = {"/open": (200, OPEN_PAGE), "/gone": (404, "Not found"), "/removed": (410, "Gone"),
         "/closed-text": (200, GONE_TEXT_PAGE), "/expired": (200, EXPIRED_LD_PAGE), "/live": (200, LIVE_LD_PAGE),
         "/blocked": (403, "Forbidden")}


def fixture_internet(asked: list[str] | None = None):
    """A fetcher factory answering from `PAGES` (no robots.txt); records each page asked for in `asked`."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if asked is not None:
            asked.append(request.url.path)
        status, body = PAGES.get(request.url.path, (200, OPEN_PAGE))
        return httpx.Response(status, html=body)

    return lambda: Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True),
                           per_host_seconds=0)


def role(slug, fit=3, **extra):
    """A role in Ready to Launch with a Second Impression that graded its posting, its score ten times its `fit` (Launch
    opens no other: the owner, 2026-11-03; `board.ready_line`). A role given another status is not read unless the test
    reads it."""
    read = {**counted(), "fit_breakdown": {"math": 1, "percent": (fit or 0) * 10, "asks": []}}
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": f"Firm {slug}", "title": f"Product Lead {slug}", "fit": fit,
        "origin": "tracker", "match_key": f"firm {slug}|product lead {slug}", "posted": TODAY, "found": TODAY,
        "status": "ready_to_launch", "apply_url": f"https://careers.example.test/open/{slug}",
        "source_kind": "board", **(read if extra.get("status", "ready_to_launch") == "ready_to_launch" else {}),
        **extra})


async def test_launch_offers_no_role_that_has_no_read_that_graded_its_posting(js):
    """A role in Ready to Launch with no Second Impression, or one that graded nothing, is not in the line Go walks, so
    no tab is opened for it (the owner, 2026-11-03: one was, for a posting that was not the role's)."""
    await _add(role("read"), role("unread", analysis_state=None, analysis={}),
               role("no-text", analysis_state="no_text"), role("nothing-graded", **counted(required=[])),
               role("opened", go_opened_at=datetime.now(UTC), analysis_state=None, analysis={}))
    async with db.session_factory()() as session:
        line, target = await board.ready_line(session, OWNER)
    assert [r.slug for r in line] == ["read"] and target == 15


async def _add(*roles):
    """Add the roles; each is ranked 10 times its `fit`, which is the order Ready to Launch walks them in."""
    async with db.session_factory()() as session:
        session.add_all(roles)
        await session.flush()
        await rank_roles(session, {r.id: r.fit * 10 for r in roles if r.fit})
        await session.commit()


async def _packet(slug: str, status: str = "ready", version: int = 1, **more) -> None:
    """A packet for the role, in the state the writer and the checks left it (a role with a finished packet opens)."""
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity).where(Opportunity.slug == slug))).one()
        session.add(Packet(owner=OWNER, role_id=found.id, version=version, status=status, brief={}, resume={},
                           letter={}, comp={}, reasoning={}, checks=[], **more))
        await session.commit()


async def _roles() -> dict[str, Opportunity]:
    async with db.session_factory()() as session:
        return {r.slug: r for r in (await session.scalars(select(Opportunity))).all()}


async def _trail(event: str) -> list[TrailEvent]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(TrailEvent.event == event))).all())


async def _prepare(**kw) -> go_checks.Outcome:
    async with db.session_factory()() as session:
        out = await go_checks.prepare(session, OWNER, **kw)
        await session.commit()
        return out


def _mail(n: int, subject: str, sender: str, body: str, day: date | None = None) -> bytes:
    when = datetime.combine(day or TODAY, time(9), UTC)
    return (f"From: {sender}\r\nSubject: {subject}\r\nDate: {format_datetime(when)}\r\n"
            f"Message-ID: <m{n}@mail.example.test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n").encode()


class FakeInbox:
    """A mailbox that holds `messages` and answers the reader's two reads as a server would (read-only)."""

    def __init__(self, messages: list[bytes]) -> None:
        self.messages = messages
        self.opened = 0

    def __call__(self, login):
        self.opened += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read_headers(self, since, on_message, on_progress=None, *, flavour=None, stems=(), words=()):
        for uid, raw in enumerate(self.messages, start=1):
            on_message(raw, ("INBOX", uid))
        return len(self.messages)

    def read_bodies(self, wanted, on_message):
        for folder, uids in wanted.items():
            for uid in uids:
                on_message(self.messages[uid - 1], (folder, uid))
        return sum(len(u) for u in wanted.values())


@pytest.fixture
def mailbox(monkeypatch):
    """Mail is set up, and the mailbox is a fake one; the test fills `mailbox.messages`."""
    box = FakeInbox([])

    async def found(session, owner):
        return go_mail.Login("imap.example.test", 993, "me", "secret")

    monkeypatch.setattr(mail_login, "find", found)
    monkeypatch.setattr(go_mail, "open_inbox", box)
    return box


def test_a_confirmation_counts_by_the_owners_day_not_the_day_on_the_senders_clock(mailbox, monkeypatch):
    """Regression case: a confirmation sent at 11 pm on the 5th in Los Angeles is the 6th for an
    owner in Auckland (UTC+13); counted by the sender's day it fell outside a window that starts on the 6th."""
    pin_clock(monkeypatch, datetime(2026, 11, 7, 12, 0, tzinfo=UTC), "Pacific/Auckland")
    mailbox.messages = [
        b"From: Firm <no-reply@firma.example.test>\r\nSubject: Thank you for applying\r\n"
        b"Date: Thu, 05 Nov 2026 23:00:00 -0800\r\nMessage-ID: <m1@mail.example.test>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nWe received your application.\r\n"]
    login = go_mail.Login("imap.example.test", 993, "me", "secret")
    found = go_mail.read_confirmations(login, date(2026, 11, 6) + timedelta(days=go_mail.DAYS))
    assert [seen.day for seen in found] == [date(2026, 11, 6)]


def test_mail_before_the_window_does_not_use_up_the_messages_the_window_may_read(mailbox, monkeypatch):
    """Regression case: the search starts a day early (the server counts days its own way), and the
    header step counted that extra day's mail toward the most messages read, then threw its text away."""
    pin_clock(monkeypatch, datetime(2026, 11, 7, 12, 0, tzinfo=UTC), "Pacific/Auckland")
    monkeypatch.setattr(go_mail, "MOST_MESSAGES", 1)
    sender = "Firm <no-reply@firma.example.test>"
    mailbox.messages = [
        _mail(1, "Thank you for applying", sender, "We received your application.", date(2026, 11, 5)),
        _mail(2, "Thank you for applying", sender, "We received your application.", date(2026, 11, 7))]
    login = go_mail.Login("imap.example.test", 993, "me", "secret")
    found = go_mail.read_confirmations(login, date(2026, 11, 6) + timedelta(days=go_mail.DAYS))
    assert [seen.day for seen in found] == [date(2026, 11, 7)]


@pytest.fixture
def pages(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(polite, "make", fixture_internet(asked))
    monkeypatch.setattr(go_checks, "start_browser", _chrome_up)
    monkeypatch.setattr(go_checks, "browser_page", _no_browser)
    return asked


async def _no_browser(link):
    raise BrowserUnavailable("no browser in this test")


# --- the mail ---------------------------------------------------------------------------------------------------------


async def test_a_confirmation_in_the_mail_moves_the_role_to_applied_with_its_evidence(js, mailbox, pages):
    mailbox.messages = [
        _mail(1, "Thank you for applying to Product Lead a", "Firm a Careers <no-reply@firma.example.test>",
              "We received your application for Product Lead a at Firm a.", TODAY - timedelta(days=4)),
        _mail(2, "Your weekly digest", "News <news@digest.example.test>", "Nothing about applications."),
    ]
    await _add(role("a", fit=6), role("b", fit=5))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["b"]
    assert [s["title"] for s in out.skipped] == ["Product Lead a"]
    assert "already applied" in out.skipped[0]["why"]
    roles = await _roles()
    assert roles["a"].status == "applied" and roles["b"].status == "ready_to_launch"
    [line] = await _trail(go_checks.APPLIED_EVENT)
    assert line.link_id == roles["a"].id
    assert "Thank you for applying to Product Lead a" in line.detail
    assert f"{TODAY - timedelta(days=4):%d %B %Y}" in line.detail
    assert "secret" not in (line.detail + line.sentence)


@pytest.fixture
def job_system(monkeypatch):
    """A made-up job system that sends confirmations from its own domain, as the real ones in `SYSTEM_SENDERS` do
    (fixtures name no real domain)."""
    monkeypatch.setattr(go_mail, "SYSTEM_SENDERS", (*go_mail.SYSTEM_SENDERS, JOB_SYSTEM))


JOB_SYSTEM = "jobsystem.invalid"


async def test_a_job_systems_confirmation_names_the_employer_in_its_text(js, mailbox, pages, job_system):
    mailbox.messages = [_mail(1, "Application received", f"Job System <noreply@{JOB_SYSTEM}>",
                              "Firm a has received your application for the Product Lead a position.")]
    await _add(role("a"))
    out = await _prepare()
    assert out.ready == [] and (await _roles())["a"].status == "applied"


async def test_mail_from_another_employer_or_without_a_confirmation_moves_nothing(js, mailbox, pages):
    mailbox.messages = [
        _mail(1, "Thank you for applying", "Other <jobs@other.example.test>",
              "Thanks for applying to Product Lead a at Other Corp."),
        _mail(2, "Product Lead a at Firm a: new roles for you", "Alerts <alerts@board.example.test>",
              "Roles like Product Lead a at Firm a."),
    ]
    await _add(role("a"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a"] and out.skipped == [] and out.notes == []
    assert (await _roles())["a"].status == "ready_to_launch"
    assert await _trail(go_checks.APPLIED_EVENT) == [] and await _trail(go_checks.UNSURE_EVENT) == []


async def test_an_unsure_match_blocks_nothing_and_is_noted_on_the_role(js, mailbox, pages):
    mailbox.messages = [_mail(1, "Thanks for applying to Firm a", "Firm a <talent@firma.example.test>",
                              "We have received your application for Data Analyst.")]
    await _add(role("a"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a"]
    assert (await _roles())["a"].status == "ready_to_launch"
    [line] = await _trail(go_checks.UNSURE_EVENT)
    assert "nothing moved" in line.detail


async def test_a_word_of_the_employers_name_in_another_companys_mail_is_not_a_maybe_applied(js, mailbox, pages):
    mailbox.messages = [_mail(1, "Thank you for applying", "Harbor Northfield <jobs@harbornorthfield.example>",
                              "Harbor Northfield has received your application.")]
    await _add(role("a", company="Northfield Group, Inc."))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a"] and out.skipped == []
    assert await _trail(go_checks.UNSURE_EVENT) == []


async def _employer_of(name: str, aliases: list[str]):
    async with db.session_factory()() as session:
        employer = await make_employer(session, name, aliases=aliases)
        await session.commit()
        return employer.id


async def test_an_alias_of_one_word_in_a_mail_without_the_role_is_not_a_maybe_applied(js, mailbox, pages):
    employer_id = await _employer_of("Quillon Insurance Group", ["Quillon"])
    mailbox.messages = [_mail(1, "Thank you for applying", "Careers <no-reply@hr.example.test>",
                              "We have received your application to Quillon.")]
    await _add(role("a", company="Quillon Insurance Group", employer_id=employer_id))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a"] and out.skipped == []
    assert await _trail(go_checks.UNSURE_EVENT) == []


async def test_an_alias_of_one_word_with_the_role_in_the_mail_is_a_maybe_applied(js, mailbox, pages):
    employer_id = await _employer_of("Quillon Insurance Group", ["Quillon"])
    mailbox.messages = [_mail(1, "Thank you for applying", "Careers <no-reply@hr.example.test>",
                              "We have received your application for Product Lead at Quillon.")]
    await _add(role("a", company="Quillon Insurance Group", employer_id=employer_id))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a"]
    [line] = await _trail(go_checks.UNSURE_EVENT)
    assert "nothing moved" in line.detail


async def test_mail_not_set_up_is_said_in_one_line_and_go_still_opens(js, monkeypatch):
    from app.jobsearch.browser import go as apply_go

    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    monkeypatch.setattr(apply_go, "PAUSE", 0)
    await _add(role("a"))
    await _packet("a")
    body = await apply_go.go(OWNER, poll=0, wait=0)
    assert opened == ["https://careers.example.test/open/a"]
    assert body["notes"] == [mail_sync.NO_MAILBOX]


async def test_a_mailbox_that_cannot_be_read_is_said_and_go_goes_on(js, monkeypatch, pages):
    from app.mailmine import imap

    def refuse(login):
        raise imap.MailReadError("The inbox didn't accept that login.")

    async def found(session, owner):
        return go_mail.Login("imap.example.test", 993, "me", "secret")

    monkeypatch.setattr(mail_login, "find", found)
    monkeypatch.setattr(go_mail, "open_inbox", refuse)
    await _add(role("a"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a"]
    assert out.notes == ["Mail wasn't checked: The inbox didn't accept that login."]


# --- the posting ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "state"), [
    ("/open", go_checks.UNKNOWN), ("/live", go_checks.OPEN), ("/gone", go_checks.CLOSED),
    ("/removed", go_checks.CLOSED), ("/closed-text", go_checks.CLOSED), ("/expired", go_checks.CLOSED),
    ("/blocked", go_checks.UNREADABLE),
])
async def test_the_posting_check_reads_the_page_in_code(path, state):
    fetch = fixture_internet()()
    try:
        found, said = await go_checks.open_state(fetch, f"https://careers.example.test{path}", TODAY)
    finally:
        await fetch.close()
    assert found == state
    assert bool(said) == (state in (go_checks.CLOSED, go_checks.UNREADABLE))


async def test_a_job_boards_page_is_never_fetched_it_goes_to_the_browser():
    asked: list[str] = []
    fetch = fixture_internet(asked)()
    try:
        found, _ = await go_checks.open_state(fetch, "https://www.board.test/jobs/view/123", TODAY)
    finally:
        await fetch.close()
    assert found == go_checks.UNREADABLE and asked == []


async def test_an_employers_own_careers_page_on_a_boards_host_is_read_in_code_but_the_hosts_search_is_not():
    """Any address on a host that is also a job board counted as the board's page."""
    asked: list[str] = []
    fetch = fixture_internet(asked)()
    try:
        search, _ = await go_checks.open_state(fetch, "https://www.searchsite.test/search?q=program+manager", TODAY)
        assert search == go_checks.UNREADABLE and asked == []
        await go_checks.open_state(
            fetch, "https://www.searchsite.test/about/careers/applications/jobs/results/123456789-program-manager",
            TODAY)
    finally:
        await fetch.close()
    assert [a for a in asked if "careers" in a], "the posting's own page is asked for, as any employer's page is"


async def test_a_closed_posting_ends_the_role_and_a_page_code_cannot_read_becomes_a_browser_check(js, pages):
    await _add(role("a", fit=6, apply_url="https://careers.example.test/gone"),
               role("b", fit=5, apply_url="https://www.board.test/jobs/view/42"),
               role("c", fit=4, apply_url="https://careers.example.test/open"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["b", "c"], "unknown and unreadable count as open"
    assert out.skipped == [{"id": out.skipped[0]["id"], "title": "Product Lead a", "company": "Firm a",
                            "why": "The posting is closed; moved to Gone Quiet."}]
    roles = await _roles()
    assert (roles["a"].status, roles["a"].ended_reason) == ("gone_quiet", "posting_gone")
    assert roles["b"].go_check == go_checks.BROWSER and roles["c"].go_check == go_checks.OPEN
    async with db.session_factory()() as session:
        assert await go_checks.browser_checks(session, OWNER) == [(roles["b"].id, "https://www.board.test/jobs/view/42")]
        later = datetime.now(UTC) + go_checks.BROWSER_LIFE + timedelta(minutes=1)
        assert await go_checks.browser_checks(session, OWNER, later) == [], "a browser check has a short life"


# --- the refill -------------------------------------------------------------------------------------------------------


async def test_go_walks_ready_to_launch_until_the_batch_holds_its_number(js, mailbox, pages, monkeypatch,
                                                                              job_system):
    monkeypatch.setattr(board, "APPLY_BATCH_DEFAULT", 2)
    mailbox.messages = [_mail(1, "Your application was sent to Firm b", f"Job Board <jobs-noreply@{JOB_SYSTEM}>",
                              "Your application was sent to Firm b. Product Lead b, Firm b.")]
    await _add(role("a", fit=9, apply_url="https://careers.example.test/expired"), role("b", fit=8),
               role("c", fit=7, apply_url=None, url=None), role("d", fit=6), role("e", fit=5), role("f", fit=4))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["d", "e"]
    assert [s["title"] for s in out.skipped] == ["Product Lead a", "Product Lead b", "Product Lead c"]
    assert (await _roles())["f"].go_checked_at is None, "the line is walked only as far as it needs"


async def test_the_line_running_out_opens_what_passed(js, pages, monkeypatch):
    monkeypatch.setattr(board, "APPLY_BATCH_DEFAULT", 3)
    await _add(role("a", fit=6, apply_url="https://careers.example.test/removed"), role("b", fit=5))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["b"] and len(out.skipped) == 1


# --- the night's pre-check --------------------------------------------------------------------------------------------


def test_the_pre_check_is_due_once_a_day_at_the_end_of_the_off_hours_window():
    zone = owner_zone()
    early = datetime.combine(TODAY, time(6, 0), zone)
    on_time = datetime.combine(TODAY, time(6, 50), zone)
    assert not go_checks.precheck_due(OWNER, early)
    assert go_checks.precheck_due(OWNER, on_time)
    go_checks._prechecked[OWNER] = TODAY
    assert not go_checks.precheck_due(OWNER, on_time), "once a day"
    assert go_checks.precheck_due(OWNER, on_time + timedelta(days=1))


async def test_the_pre_check_checks_again_and_go_then_skips_what_it_checked(js, pages):
    recently = datetime.now(UTC) - timedelta(hours=1)
    await _add(role("a", fit=6, apply_url="https://careers.example.test/gone", go_check="open", go_checked_at=recently),
               role("b", fit=5, apply_url="https://careers.example.test/open"))
    out = await go_checks.precheck(OWNER)
    assert [r.slug for r in out.ready] == ["b"], "the night checks even a role checked an hour ago"
    assert (await _roles())["a"].status == "gone_quiet"
    pages.clear()
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["b"] and pages == [], "Go re-checks only roles not checked in 12 hours"


async def _chrome_up():
    return ""


async def test_an_opened_role_becomes_applied_when_the_mail_confirms_it_and_stays_when_it_does_not(js, mailbox, pages):
    mailbox.messages = [
        _mail(1, "Thank you for applying to Product Lead a", "Firm a Careers <no-reply@firma.example.test>",
              "We received your application for Product Lead a at Firm a."),
    ]
    await _add(role("a", fit=6), role("b", fit=5), role("c", fit=4))
    async with db.session_factory()() as session:  # Go opened a and b; c was not opened
        for row in (await session.scalars(select(Opportunity).where(Opportunity.slug.in_(("a", "b"))))).all():
            row.go_opened_at = datetime.now(UTC)
        await session.commit()
    async with db.session_factory()() as session:
        assert await go_checks.applied_by_mail(session, OWNER) == 1
        await session.commit()
    roles = await _roles()
    assert roles["a"].status == "applied", "its confirmation came"
    assert roles["b"].status == "ready_to_launch" and roles["c"].status == "ready_to_launch"
    [line] = await _trail(go_checks.APPLIED_EVENT)
    assert line.link_id == roles["a"].id


async def test_an_opened_role_that_left_ready_to_launch_unread_still_becomes_applied_when_the_mail_confirms_it(
        js, mailbox, pages):
    """A role Go opened leaves Ready to Launch when its read is lost (its re-read failed: docs/PRD.md 24.13), but the
    application it was opened for was still sent: the mail's confirmation moves it to Applied wherever before the
    application it sits now. A role Go never opened, and one that is closed, stay where they are."""
    mailbox.messages = [
        _mail(n, f"Thank you for applying to Product Lead {slug}",
              f"Firm {slug} Careers <no-reply@firm{slug}.example.test>",
              f"We received your application for Product Lead {slug} at Firm {slug}.") for n, slug in enumerate("abcd")]
    opened = datetime.now(UTC)
    await _add(role("a", status="first_impression", go_opened_at=opened, analysis_state="failed"),
               role("b", status="second_impression", go_opened_at=opened),
               role("c", status="first_impression"),
               role("d", status="rejected", go_opened_at=opened))
    async with db.session_factory()() as session:
        assert await go_checks.applied_by_mail(session, OWNER) == 2
        await session.commit()
    roles = await _roles()
    assert {s: r.status for s, r in roles.items()} == {
        "a": "applied", "b": "applied", "c": "first_impression", "d": "rejected"}
    assert {line.link_id for line in await _trail(go_checks.APPLIED_EVENT)} == {roles["a"].id, roles["b"].id}


async def test_the_checks_tell_the_run_how_many_postings_they_look_at_and_how_many_they_have_looked_at(
        js, mailbox, pages):
    await _add(role("a", fit=9), role("b", fit=8), role("c", fit=7))
    state = {"checked": 0, "check_total": 0}
    out = await _prepare(progress=state)
    assert len(out.ready) == 3
    assert state == {"checked": 3, "check_total": 3}


async def test_a_posting_on_a_job_system_whose_pages_are_not_a_shell_is_judged_from_its_page_not_its_reader():
    """Only the systems that draw their postings in a browser are judged by their reader's answer, which is the one
    thing a page of theirs cannot say. Every other system's page is read, here a posting that says it is closed: the
    fetch is asked for the link itself and nothing else."""
    link = "https://careers-acme.hostsys.test/jobs/1234/product-lead/job"
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        asked.append(str(request.url))
        return httpx.Response(200, html=GONE_TEXT_PAGE)

    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True), per_host_seconds=0)
    try:
        found, said = await go_checks.open_state(fetch, link, TODAY)
    finally:
        await fetch.close()
    assert found == go_checks.CLOSED and "no longer accepting applications" in said
    assert asked == [link]


# --- a page too short to judge (the owner, 2026-11-13; PRD 24.16) -------------------------------------------------


def test_a_short_page_with_no_posting_data_is_unknown_not_open():
    link = "https://careers.example.test/jobs/1"
    held = Opportunity(owner=OWNER, slug="held", company="Firm", title="Product Lead", source_url=link,
                       description="Lead the product team. " * 30)
    short = Page(url=link, status=200, html="<html><body><p>" + "word " * 12 + "</p></body></html>")
    assert go_checks.page_verdict(short, link, TODAY, held)[0] == go_checks.UNKNOWN
    assert go_checks.page_verdict(short, link, TODAY)[0] == go_checks.UNKNOWN
    long = Page(url=link, status=200, html="<html><body><p>" + "word " * 60 + "</p></body></html>")
    assert go_checks.page_verdict(long, link, TODAY)[0] == go_checks.OPEN, "a page with something on it stays open"
    data = ('<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "Product Lead"}'
            "</script></head><body><p>Product Lead</p></body></html>")
    assert go_checks.page_verdict(Page(url=link, status=200, html=data), link, TODAY)[0] == go_checks.OPEN


async def test_a_short_browser_page_keeps_a_page_code_could_not_read_a_browser_check(js, pages, monkeypatch):
    link = "https://www.board.test/jobs/view/42"

    async def short_page(url):
        return Page(url=url, status=200, html="<html><body><p>Product Lead</p></body></html>")

    monkeypatch.setattr(go_checks, "browser_page", short_page)
    await _add(role("a", fit=6, apply_url=link))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a"], "unknown still opens, as it did"
    got = (await _roles())["a"]
    assert got.go_check == go_checks.BROWSER
    async with db.session_factory()() as session:
        assert await go_checks.browser_checks(session, OWNER) == [(got.id, link)], "one read item for the role"
    await _prepare()
    async with db.session_factory()() as session:
        assert await go_checks.browser_checks(session, OWNER) == [(got.id, link)], "a second check adds none"


async def test_a_role_whose_entry_check_ends_unknown_is_marked_unclear(js, pages):
    await _add(role("a", fit=6, apply_url="https://careers.example.test/open"))
    async with db.session_factory()() as session:
        assert await go_checks.check_entered(session, OWNER) == 1
        await session.commit()
    assert (await _roles())["a"].go_check == go_checks.UNCLEAR
