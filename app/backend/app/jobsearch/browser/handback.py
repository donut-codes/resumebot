"""Handing a browser item back: what each result does to the role, the posting or the site (see `browser/items.py`)."""

import json
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.actors import CODE_CHECKER, CODE_CHECKER_BY
from app.jobsearch.address import is_board_link
from app.jobsearch.browser import go_checks
from app.jobsearch.browser import leases as browser_leases
from app.jobsearch.browser import pace as browser_pace
from app.jobsearch.browser import reader as br
from app.jobsearch.browser.pace import (
    BLOCKED,
    COOLING,
    DONE,
    DROPPED,
    EASY_APPLY,
    FAILED,
    FINISHED,
    FOUND,
    GONE,
    LISTING,
    MAP,
    NOT_FOUND,
    NOTE_MAX,
    NOTE_MIN,
    READ,
    RECORD,
    RESULTS,
    RUN_ITEMS,
    SCAN,
    SOURCE,
    STUCK,
    TEXT,
    TRIES,
    UNREADABLE,
    WAITING,
    BrowserWorkError,
)
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.fetch import host_stops
from app.jobsearch.models import BrowserItem, JobPosting, MissionRun, Opportunity, is_board_copy
from app.jobsearch.pipeline import quiet
from app.jobsearch.postings import noise, whole
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.postings import text as posting_text
from app.jobsearch.work import availability
from app.sites import registry
from app.textutil import squash
from app.timeutil import aware

#: A note that says nothing ("placeholder", "tbd", "n/a", dots).
_PLACEHOLDER = re.compile(r"^\W*(?:placeholder|tbd|todo|n/?a|none|null|test|x+|\.+|-+|\?+)\W*$|\bplaceholder\b", re.I)

#: The items whose session found the app's browser would not open a tab for them (`note_tab_failed`, from the digest),
#: until a page of theirs loads (`note_tab_opened`).
_tab_failed: set[str] = set()
#: A `not_found` or `unreadable` because the browser would not open a tab is no try: the item goes back and waits this
#: long.
TAB_RETRY = timedelta(minutes=15)
#: A session's note that says the browser would not open a tab (its own `new_page`, which the app does not see).
_NO_TAB = re.compile(
    r"Target\.createTarget|\b(?:tab|tabs|browser)\b[^.;]{0,40}\b(?:did not|didn't|would not|wouldn't|could not|"
    r"couldn't|cannot|can't|won't|failed to)\s+(?:open|load)", re.I)


def note_tab_failed(item_id: str) -> None:
    _tab_failed.add(str(item_id))


def note_tab_opened(item_id: str) -> None:
    _tab_failed.discard(str(item_id))


def _no_tab(row: BrowserItem, note: str) -> bool:
    """Whether a `not_found` or `unreadable` came of the browser not opening a tab, not of the page."""
    return str(row.id) in _tab_failed or bool(_NO_TAB.search(note or ""))


def _note(note, required: str | None) -> str:
    """A note as kept, or a refusal: a few words that say something (never a placeholder)."""
    text = squash(note)
    if required is None and not text:
        return ""
    if not NOTE_MIN <= len(text) <= NOTE_MAX:
        raise BrowserWorkError(f"note: {required or 'say'} in a few words ({NOTE_MIN} to {NOTE_MAX} characters)")
    if _PLACEHOLDER.search(text):
        raise BrowserWorkError(f"note: {text[:40]!r} says nothing; {required or 'say'} in a few words of your own")
    return text


def _whole(text) -> str:
    """The text as kept, or a refusal: the whole posting, never shortened, a placeholder, or too long to hold."""
    body = br._text(text)
    if not body:
        raise BrowserWorkError("text is required: the posting's whole text, copied exactly as the page shows it")
    if why := whole.why_not(body):
        raise BrowserWorkError(f"text refused: {why}. Send the whole posting, never shortened or summarised; if you "
                               "cannot, hand the item back as unreadable (or not_found) with why")
    if too_long := whole.too_long(body, "text"):
        raise BrowserWorkError(too_long)
    return body


async def _source(session: AsyncSession, owner: str, row: BrowserItem, role: Opportunity, result: str, url, text,
                  note: str, source_kind, now: datetime, raw: bytes | None = None, final: bool = False) -> str:
    """A `source` item handed back: what it does to the role. Returns the item's new state. An item only the app's code
    takes (`code_only`: a role in Followup needed) that ends stuck on a try that found nothing closes the role as noise
    (`postings/noise.py`, the owner, 2026-11-13); `final`: the code found the answer certain (no address to open, a page
    that holds no apply data), so the item is stuck at once and not after three tries."""
    from app.jobsearch.research import analysis  # analysis reads the runs; imported here, never at the top

    if result in (FOUND, EASY_APPLY):
        address = br.web_address(squash(url))
        hosts = await employer_posting.job_board_hosts(session, owner)
        on_board = is_board_link(address, hosts)
        if result == FOUND and on_board:
            raise BrowserWorkError(
                "a job board's address is not a source: send the employer's own posting "
                "or its job system's; if the listing itself is where you apply, hand back easy_apply; else not_found")
        if result == EASY_APPLY and not on_board:
            raise BrowserWorkError("easy_apply is the board's own listing; an address off the board is found")
        body = _whole(text) if (text or result == EASY_APPLY) else ""
        kind = sourcing.BOARD if result == EASY_APPLY else (
            source_kind if source_kind in (sourcing.EMPLOYER, sourcing.RECRUITER) else sourcing.EMPLOYER)
        by = CODE_CHECKER_BY if browser_pace.is_code(row.leased_by) else br.SOURCE_BY
        said = await sourcing.accept_source(session, role, address, kind, body or None, bool(body), by,
                                            trust_system=True, page=(raw or None) if body else None)
        if said.startswith("rejected:"):
            why = said.removeprefix("rejected:")
            raise BrowserWorkError(f"that address is not taken as this role's posting: {why}")
        role.browser_note = None
        if body:
            await analysis.resume_waiting(session, owner, [role.id])
        return DONE
    if result == GONE:
        await quiet.end_gone(session, role, squash(url) or row.link or "", note,
                                    CODE_CHECKER if browser_pace.is_code(row.leased_by) else None)
        role.browser_note = None
        return DONE
    if final:
        row.tries = max(row.tries or 0, TRIES - 1)
    state = browser_leases._retry(row, now)
    if state == STUCK:
        role.browser_note = note
        if row.code_only and result in (NOT_FOUND, UNREADABLE) and browser_pace.is_code(row.leased_by):
            if await noise.close(session, role, note, link=row.link):
                role.browser_note = None
    return state


async def _read(session: AsyncSession, owner: str, row: BrowserItem, result: str, url, text, note: str, title,
                company, now: datetime, raw: bytes | None = None) -> str:
    """A `read` item handed back: the text kept, the role ended, or a retry. Returns the item's new state."""
    from app.jobsearch.research import analysis  # analysis reads the runs; imported here, never at the top

    role = await session.get(Opportunity, row.role_id) if row.role_id is not None else None
    posting = await br._added_posting(session, owner, row.posting_id) if row.posting_id is not None else None
    if result == TEXT:
        body = _whole(text)
        if posting is not None:
            if not await br._name_added(session, owner, posting, {"title": title, "company": company}, body, now):
                raise BrowserWorkError("send the title and the company exactly as the page shows them, both, with the "
                                       "text: this job has no name yet")
            return DONE
        if role is None:
            return DROPPED
        if role.go_check == go_checks.BROWSER:  # Go's check: the page still shows the posting
            role.go_check = go_checks.OPEN
        elif role.go_check == go_checks.LOOKING:  # the check made on entering Ready to Launch: so it does
            role.go_check = go_checks.ENTERED
        if not whole.thin(role.description) and not is_board_copy(role):  # a usable text is kept; a snippet and
            return DONE  # the board's copy give way to the posting (`take`)
        if await posting_text.take(session, role, posting_text.Capture(body, page=raw or None),
                                   now=now) == posting_text.FETCHED:
            role.browser_note = None
            await analysis.resume_waiting(session, owner, [role.id])
            return DONE
        note = "the app could not keep the text against the role's original posting"
        row.last_note = note
        state = browser_leases._retry(row, now)
        if state == STUCK:
            role.browser_note = note
        return state
    if result == GONE:
        if posting is not None:
            posting.closed_at = now
        elif role is not None:
            await quiet.end_gone(session, role, squash(url) or row.link or "", note,
                                        CODE_CHECKER if browser_pace.is_code(row.leased_by) else None)
            role.browser_note = None
        return DONE
    state = browser_leases._retry(row, now)
    if state == STUCK and role is not None:
        role.browser_note = note
    return state


async def _site_item(session: AsyncSession, owner: str, row: BrowserItem, entry: MissionRun, result: str, note: str,
                     now: datetime) -> str:
    """A `scan` or `map` item handed back. Returns the item's new state."""
    rows = (await br.browser_sites(session, owner)).get(row.site or "", [])
    if row.kind == SCAN:
        if result != FINISHED:
            await br.no_longer_fits(session, rows, note)  # failed: the runbook no longer fits; the site is mapped again
        return DONE
    if result == FINISHED:
        if row.site not in ((entry.started or {}).get("learned") or {}):
            raise BrowserWorkError("hand in the runbook with browser.skill first (it must be kept), or hand the item "
                                   "back as failed with why")
        return DONE
    await br.learn_failed(session, rows, note)
    return browser_leases._retry(row, now)


async def _record(session: AsyncSession, owner: str, row: BrowserItem, result: str, note: str, now: datetime) -> str:
    """A `record` item the app's code handed back: finished (the traffic is kept, or the page had none a recipe could
    repeat), or failed: tried again after `RETRY_AFTER`, and after `TRIES` the request for the recording ends as failed.
    Returns the item's new state."""
    from app.jobsearch.browser.recipes import record as recipe_record

    if result == FINISHED:
        return DONE
    state = browser_leases._retry(row, now)
    if state == STUCK:
        await recipe_record.give_up(session, owner, row.site or "", note)
    return state


async def _listing(session: AsyncSession, owner: str, row: BrowserItem, role: Opportunity, result: str, url, note: str,
                   raw: bytes | None, now: datetime) -> str:
    """A `listing` item the app's code handed back: a followed board's role's posting page, read in its Chrome
    (docs/PRD.md 48.14). Only the app's code hands one back (no session is given one), so each outcome is a result.

    - `text`: the page's read-out (JSON, `raw`: `capture.READ_KEYS`) is kept as the posting's own copy by
      `capture.keep`: its text is the posting's text where it holds none, its chip the posting's way of working. Never
      `posting_text.take` of the page as the employer's text: the description of the role stays empty, since the
      employer's posting is what is read, unless the employer's text is out of reach (the owner, 2026-11-05: its site
      refused, the posting is gone, the lookup found none), when the page's text becomes the role's description,
      labelled as the board's copy (the board-copy module's `apply`: only a role that needs it, which is what this item
      was made for when the role is not in Followup needed). A read-out the app would not keep (no whole text, a removed
      page, a posting that is not this role's) is refused, and the caller hands back `unreadable`; nothing is kept for
      it, no raw copy, because a raw copy by the page reader counts the posting as read and the item would drop with the
      role in Followup needed and no note.
    - `gone`: the role ends as `posting_gone`, with no raw copy.
    - `unreadable`: a try that found nothing, tried again after `RETRY_AFTER`; after `TRIES` the item is stuck, the
      reason on the role's card, and the role stays in Followup needed.

    Returns the item's new state."""
    from app.jobsearch.browser import code as browser_code  # it reads this module
    capture = registry.board_reader("capture")  # it reads the browser code, which reads this module
    board_copy = registry.board_reader("copy")

    if result == TEXT:
        if capture is None or board_copy is None:
            raise BrowserWorkError("the app has no reader of this board's pages")
        try:
            view = json.loads((raw or b"").decode("utf-8"))
        except ValueError:
            view = None
        if not isinstance(view, dict):
            raise BrowserWorkError("the page's read-out is not JSON: hand the item back as unreadable")
        posting = await session.get(JobPosting, row.posting_id) if row.posting_id is not None else None
        if posting is None or posting.opportunity_id != role.id:
            raise BrowserWorkError("the posting this page was read for is no longer this role's")
        text = (view.get("text") or "").strip()
        if view.get("state") == browser_code.STATE_REMOVED or not text:
            raise BrowserWorkError("the read-out holds no text, or says the page is removed: hand back unreadable "
                                   "or gone")
        if said := whole.refusal(text, f"the {browser_pace.site_name(row.domain)} posting's text"):
            raise BrowserWorkError(f"text refused: {said}")
        own = await capture.holds_employers_text(session, owner, role)
        await capture.keep(session, posting, view, employers_own=own, now=now)
        await board_copy.apply(session, role, now)  # a role whose employer's text is out of reach takes the copy
        role.browser_note = None
        return DONE
    if result == GONE:
        await quiet.end_gone(session, role, squash(url) or row.link or "", note,
                             CODE_CHECKER if browser_pace.is_code(row.leased_by) else None)
        role.browser_note = None
        return DONE
    state = browser_leases._retry(row, now)
    if state == STUCK:
        role.browser_note = note
        if result == UNREADABLE and await noise.close(session, role, note, link=row.link):
            role.browser_note = None  # the copy route has failed by code too: a Followup needed role is closed as noise
    return state


def _what(row: BrowserItem, role: Opportunity | None, names: dict[str, str]) -> str:
    if row.kind in (SCAN, MAP):
        return names.get(row.site or "", row.site or row.domain)
    if role is not None:
        return f"{role.title} at {role.company}"
    return row.link or row.domain


async def hand_back(session: AsyncSession, owner: str, item_id, result, url=None, text=None, note=None, title=None,
                    company=None, source_kind=None, now: datetime | None = None, *, sid: str | None = None,
                    raw: bytes | None = None, final: bool = False) -> dict:
    """What came of the item the calling session holds (the door's `browser.hand_back`), and what it does: see the
    module's text. A hand-back with a problem is refused with what to fix, and the item stays the session's. The
    domain's next page is held `PAGE_GAP` off; a block cools the domain instead (`COOLING`) and uses no try. `sid`:
    the app's own code-first worker (`browser/code.py`), which has no run of its own; else the calling session."""
    now = now or datetime.now(UTC)
    sid = sid or browser_pace._asker()
    code = browser_pace.is_code(sid)
    if not code:  # it may insert the session's run (a KEY SHARE on the mission's row): lock that row first
        await browser_pace.lock_mission(session, owner)
    # A result in hand is never thrown away: an item past its time box that nothing has timed out yet (the next page
    # load's heartbeat, any next_item, the schedule's look) is still taken.
    row = await browser_leases._held_item(session, owner, sid)
    if row is None or str(row.id) != squash(item_id):
        if not code and await browser_leases._timed_out_last(session, owner, sid):
            raise BrowserWorkError("that item ran past its time box and was handed back as timed out (it is tried "
                                   "again later): call browser.next_item")
        raise BrowserWorkError("that is not the item you hold: call browser.next_item")
    if result not in RESULTS[row.kind]:
        raise BrowserWorkError(f"result for a {row.kind} item is one of {', '.join(RESULTS[row.kind])}")
    # what an item of this kind hands back when it failed
    honest = {SOURCE: NOT_FOUND, READ: UNREADABLE, LISTING: UNREADABLE}.get(row.kind, FAILED)
    if result == BLOCKED and host_stops.is_cookie_notice(note):
        # A cookie or consent banner is no block and no stop (the owner, 2026-11-04): it is declined, and the page read.
        raise BrowserWorkError(
            "a cookie or consent banner is not a block: click its option that refuses non-essential cookies (Reject "
            "all, Decline, Necessary only, Only essential), or close it, then read the page and hand back what you "
            "found; a banner that offers only Accept is read past, never accepted. If the page still cannot be read, "
            f"hand back `{honest}` with why")
    if result == BLOCKED and host_stops.is_own_failure(note):
        # Our own browser failed (a tab that would not open, a DevTools error): the site never answered, so it is no
        # refusal, no stop and no rest. The item goes back as a plain failure, on the normal tries.
        result = honest
    needs = {NOT_FOUND: "say why no posting was found", UNREADABLE: "say why the page could not be read in full",
             BLOCKED: "say what the page showed (a CAPTCHA, a block or rate-limit page)",
             GONE: "give the words the page showed", FAILED: "say why"}
    kept_note = _note(note, needs.get(result))
    # A block cools the domain of the page that blocked (an employer's page that answered 403 after a
    # board item's Apply must not cool the board's domain). For `source`, where Apply leads is the result itself.
    blocked_at = row.domain
    stop_key = host_stops.item_key(row)  # a stop is the host that refused, not its domain (`host_stops.py`)
    if result == BLOCKED:
        address = br.web_address(squash(url)) if squash(url) else ""
        blocked_at = br.domain(address) or row.domain
        stop_key = host_stops.key_of(br.host_of(address)) if address else stop_key
        if host_stops.domain_of(stop_key) != blocked_at:
            stop_key = blocked_at
        if row.kind == SOURCE and blocked_at != row.domain:
            raise BrowserWorkError(
                "where Apply leads is this item's result: hand it back as found with that address, even when that "
                "page answers 403 or shows a CAPTCHA (reading its text is a separate read item); "
                "blocked is only for the listing's own site")
        if address:
            kept_note = f"{kept_note} (at {address})"[:NOTE_MAX + 200]
    entry = None if code else await browser_leases.session_run(session, owner, sid, now)
    role = await session.get(Opportunity, row.role_id) if row.role_id is not None else None
    stops = result == BLOCKED and not host_stops.is_rate_limit(kept_note)  # a refusal stops; a rate limit rests
    # A refusal or bot check is a stop for the domain (`host_stops.py`): for good, but where the site's runbook says its
    # stop ends, at the owner's next off hours start (PRD 48.14 d). Made first: the item and the role are settled by
    # what it came to.
    held = await host_stops.stop(session, owner, stop_key, now, kept_note) if stops else None
    ends_at = held.stopped_until if held is not None and host_stops.in_force(held, now) else None
    ends = ends_at is not None
    no_tab = result in (NOT_FOUND, UNREADABLE) and row.kind in (SOURCE, READ, LISTING) and _no_tab(row, kept_note)
    _tab_failed.discard(str(row.id))
    if row.kind in (SOURCE, LISTING) and role is None:
        state = DROPPED
    elif result == BLOCKED:
        # Only a rate-limit page is a rest. A stop that ends leaves the item waiting for its end, no try used, and no
        # note on the role (the item is not given up on); a stop for good ends it stuck with the reason on the role.
        state = STUCK if stops and not ends else WAITING
        if stops and not ends and role is not None:
            role.browser_note = f"{blocked_at} is stopped: {kept_note}"[:NOTE_MAX + 100]
    elif no_tab:
        state, row.next_at = WAITING, now + TAB_RETRY  # no try used: the page was never seen
    elif row.kind == SOURCE:
        assert role is not None
        state = await _source(session, owner, row, role, result, url, text, kept_note, source_kind, now, raw, final)
    elif row.kind == READ:
        state = await _read(session, owner, row, result, url, text, kept_note, title, company, now, raw)
    elif row.kind == LISTING:
        assert role is not None
        state = await _listing(session, owner, row, role, result, url, kept_note, raw, now)
    elif row.kind == RECORD:
        state = await _record(session, owner, row, result, kept_note, now)
    else:
        if entry is None:
            raise BrowserWorkError("the app's code hands back only source, read, record and listing items")
        state = await _site_item(session, owner, row, entry, result, kept_note, now)
    pace = await browser_pace._pace(session, owner, blocked_at)
    if result == BLOCKED:
        if not ends:  # a stop that ends is not a block in a row: a slow-down after it rests an hour, then eight
            pace.blocks = (pace.blocks or 0) + 1
        if ends_at is not None:
            row.next_at = ends_at
        elif stops:
            row.done_at = now
        else:
            cool_until = now + (COOLING[0] if pace.blocks == 1 else COOLING[1])
            pace.cool_until = pace.next_page_at = row.next_at = cool_until
        if blocked_at != row.domain:  # the item's own domain only keeps its page gap
            own = await browser_pace._pace(session, owner, row.domain)
            own.next_page_at = max(aware(own.next_page_at) or now, now + browser_pace.page_gap(row.domain))
    else:
        pace.blocks = 0
        pace.next_page_at = max(aware(pace.next_page_at) or now, now + browser_pace.page_gap(blocked_at))
    row.state, row.leased_by, row.lease_until, row.due_by = state, None, None, None
    if result == BLOCKED and stops:
        await session.flush()
        await host_stops.end_waiting(session, owner, now)
    row.last_result, row.last_note = result, kept_note or (squash(url) or None)
    if state in (DONE, DROPPED):
        row.done_at = now
    browser_leases._log(session, row, sid, result, kept_note or squash(url) or None, now)
    if entry is not None:
        started = dict(entry.started or {})
        names = {h: br.site_name(rs) for h, rs in (await br.browser_sites(session, owner)).items()}
        started["items"] = [*(started.get("items") or []), {
            "kind": row.kind, "result": result, "domain": row.domain,
            "blocked_at": blocked_at if result == BLOCKED else None, "what": _what(row, role, names),
            "note": kept_note or None}][-RUN_ITEMS:]
        started["last_call"] = now.isoformat()
        entry.started = started
        entry.summary = browser_leases.summary_words(started)
    await session.flush()
    reply: dict = {"ok": True, "result": result, "item": state}
    if result == BLOCKED and stops:
        reply["note"] = (
            f"{blocked_at} is stopped until {ends_at.isoformat()}, the owner's next off hours: a refusal is a stop. "
            "Its items wait for then." if ends_at is not None else
            f"{blocked_at} is stopped for good: a refusal is a stop. Its other items end with the reason.")
    elif state == STUCK:
        reply["note"] = f"That was its last try ({TRIES}): it is left for the owner, with your reason."
    elif no_tab:
        reply["note"] = ("The browser would not open a tab, so this used no try: it goes back in the queue. If tabs "
                         "still do not open, stop taking items.")
    elif state == WAITING and result != BLOCKED:
        reply["note"] = "It will be tried again tomorrow."
    elif result == BLOCKED:
        until = aware(pace.cool_until)
        reply["note"] = f"{blocked_at} rests until {until.isoformat() if until else 'later'}."
    reply["next"] = ("The app is restarting: you have finished. Reply with one line and stop."
                     if availability.draining(owner, now) else "Call browser.next_item for your next item.")
    return reply
