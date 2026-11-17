"""A refusal or a bot check stops the host that refused for good (the project rules: "a refusal is a stop"; PRD path
28), and a site whose runbook says `until_next_off_hours` until the owner's next off hours start (the project rules: "a
refusal of any kind stops it for the night"; PRD 48.14 d, 2026-11-05).

A stop applies to the host that refused (`key_of`: `job-boards.example.test`), never to its whole domain:
a shared platform's other hosts and other employers' boards did nothing. The pace and the one worker per site stay per
registrable domain.

The stop is kept on the host's record (`BrowserDomain.stopped_at`, `stopped_for`: when, what answered), so it
holds after a restart and for both ways the app reads a site: the browser's queue (its items end with the reason
instead of resting) and the polite fetcher (`polite.py`: it skips a stopped host in every later run). A 429 or a
rate-limit page is no stop, only a rest. Nothing here clears a stop for good: the owner who wants one lifted does it in
the data.

**A stop that ends** (a runbook's `stop: until_next_off_hours`, `BrowserDomain.stopped_until`): a refusal or security
check met while reading such a site, by whoever reads it (the queue's items, the hand-run read, the guest search through
the fetcher), stops its domain until the next start of the owner's off hours (`hours.next_off_hours_start`: tonight's
when it is met by day, the night after when it is met in the night), recorded on the domain's record so every reader
holds off; then it is read again by itself, with nothing to clear. Its waiting items wait for the end instead of being
ended with the reason (`end_waiting` ends only a stop for good), so no role is given a note for it. A stop is in force
while `in_force`; every reader asks that, never `stopped_at` alone. Every other host keeps the stop for good.

**One stop for the domain** (`stop_key`): a refusal at any host of a domain whose stop ends (`www`, a country's, `jobs`)
is kept under the domain, and every reader asks it there, so the rest of the site is not read after one host refused.
Every other host stays a stop of its own host. Which domains those are, and how fast each is read, is the registry's
(`app.sites.registry`), never a name in this module. **Every reader asks the record** (`refresh`): the stops this
process holds are loaded at its start and added to as it makes them, so a stop another process made (the hand-run
command's) is taken up from the record by the readers that start or take a turn (the night's read, the deck), never
cleared by it.

**Fails closed** (design 2.4): a host whose runbook did not load is refused with a plain reason (`stopped_reason`),
never read at the generic pace; and a stop made where no runbook is known for a domain the record holds an ending stop
for ends at the night too, never for good.

The fetcher is called from everywhere and holds no session, so the stopped domains are kept in this process
(`_stopped`, and `_ends` for the ones that end), loaded at every start (`load`) and added to when a stop is made. Until
loaded (a test with no database) nothing is stopped and nothing is written."""

import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import as_read
from app.jobsearch.models import BrowserDomain, BrowserItem, Opportunity
from app.sites import registry
from app.timeutil import aware, utc_now

log = logging.getLogger(__name__)

#: What a note says when the site only asked the app to slow down: a rest, not a stop.
RATE_LIMIT = re.compile(r"\b429\b|rate[- ]?limit|too many requests|slow(?:ing)? down|try again (?:later|in)", re.I)

#: What a note says when the app's own browser failed (a tab that would not open, a DevTools or connection error, a
#: timeout on our side): the site never answered, so it is neither a stop nor a rest.
OWN_FAILURE = re.compile(
    r"Target\.createTarget|DevTools|\bCDP\b|websocket|"
    r"\b(?:tab|tabs|browser|chrome)\b[^.;]{0,40}\b(?:did not|didn't|would not|wouldn't|could not|couldn't|cannot|"
    r"can't|won't|failed to|is not running|not running)\b|"
    r"\b(?:tab|browser|chrome)\b[^.;]{0,40}time(?:d)?[- ]?out", re.I)

#: What a note says when all a session met was a cookie or consent banner (the owner, 2026-11-04: never a block): it
#: names one, and once what a banner does to a page ("a cookie notice blocked the page", "hides the posting") is taken
#: out, nothing in it is a real stop's word.
COOKIE_NOTICE = re.compile(
    r"\bcookies?\b|\bconsent\b|\bgdpr\b|\bccpa\b|tracking (?:choices|preferences)|"
    r"privacy (?:choices|preferences|settings)", re.I)
_BANNER = (r"(?:(?:cookies?|consent|gdpr|ccpa|privacy|tracking)[ -]+){1,3}"
           r"(?:notices?|banners?|walls?|pop-?ups?|dialogs?|dialogues?|overlays?|prompts?|bars?|messages?|modals?)")
_COVERS = r"(?:blocks?|blocked|blocking|covers?|covered|covering|hides?|hid|hidden|hiding|obscur(?:es|ed|ing))"
#: A banner doing what banners do to a page: "the cookie notice blocked the page", "blocked by a cookie banner".
BANNER_COVER = re.compile(
    rf"\b{_BANNER}\s+(?:(?:is|are|was|were|has|had|been|still|also|\w+ly)\s+){{0,2}}{_COVERS}\b|"
    rf"\b{_COVERS}\s+by\s+(?:an?\s+|the\s+)?{_BANNER}", re.I)
#: What a real refusal says, which a session that met one writes whatever else the page showed. It is a list of what a
#: refusal says, not of what a banner says, so it errs toward stopping: a missed stop makes the app keep going to a site
#: that refused it ("a refusal is a stop"), and a stop that was not needed is the cheaper mistake.
REAL_STOP = re.compile(
    r"captcha|\brobots?\b|\bbots?\b|\bhuman\b|\bautomat\w*|\bcrawl\w*|\bscrap(?:er|ers|ing)\b|"
    r"verify (?:that )?you|confirm (?:that )?you|prove you|are you a|press (?:and|&)? ?hold|"
    r"unusual (?:activity|traffic)|suspicious|abnormal|\bflagged\b|"
    r"denied|forbidden|prohibit\w*|not allowed|disallow\w*|robots\.txt|restricted|refus\w*|"
    r"turned? (?:\w+ )?away|unauthori[sz]ed|\bblock(?:s|ed|ing)?\b|\bban(?:s|ned|ning)?\b|\bblacklist\w*|"
    r"security (?:check|verification)|challenge|just a moment|checking your browser|attention required|"
    r"pardon our interruption|one more step|"
    r"incident|\bray id\b|reference (?:id|number)|support id|"
    r"cloudflare|akamai|imperva|incapsula|datadome|perimeterx|kasada|sucuri|shape security|"
    r"hcaptcha|turnstile|arkose|bot management|\byour ip\b|"
    r"sign(?:ed)?[- ]?in\b|log(?:ged)?[- ]?in\b|\bauthwall|\bsign up\b|\bjoin now\b|"
    r"\b[45]\d\d\b|\b999\b|rate[- ]?limit|too many requests|throttl\w*", re.I)

#: The domains stopped, with what answered; None until loaded.
_stopped: dict[str, str] | None = None
#: When each stop that ends ends (a runbook's `stop: until_next_off_hours`); a domain stopped with no entry here is
#: stopped for good.
_ends: dict[str, datetime] = {}
#: The domains the record holds a stop that ends for (`stopped_until` set), loaded with the stops. One that no runbook
#: covers (the restricted folder is absent) is still stopped until a night, never for good (design 2.4a).
_ending: set[str] = set()


def is_rate_limit(note: str | None) -> bool:
    return bool(RATE_LIMIT.search(note or ""))


def is_own_failure(note: str | None) -> bool:
    """Whether a note says the app's own browser failed, not that the site refused."""
    return bool(OWN_FAILURE.search(note or ""))


def is_cookie_notice(note: str | None) -> bool:
    """Whether a `blocked` note says only that a cookie or consent banner came up. That is no block: the session
    declines the banner and reads on, so the hand-back is refused (`handback.hand_back`), never a stop or a rest. A
    note with any word a real refusal uses, even beside a cookie one, is not (`REAL_STOP`); what a banner did to the
    page ("blocked the page") is taken out first (`BANNER_COVER`)."""
    note = note or ""
    return bool(COOKIE_NOTICE.search(note)) and not REAL_STOP.search(BANNER_COVER.sub(" ", note))


def domain_of(host: str) -> str:
    # Imported here: employer_posting reaches polite (through employers), and polite imports this module.
    from app.jobsearch.employers import follow as employer_posting

    return employer_posting.base_domain((host or "").lower())


def key_of(host: str | None) -> str:
    """What a stop is kept under: the host, lowercase, without `www.` or a trailing dot. Empty when there is none."""
    return (host or "").strip().lower().rstrip(".").removeprefix("www.")


def stop_key(host: str | None) -> str:
    """What a stop is kept and asked under: `key_of` (the host), but one key, the domain's, for every host of a domain
    whose stop ends (the registry's: `registry.stop_key`), so a refusal at one of its hosts (`www`, a country's) stops
    all of them. Kept under the host that refused, the rest of the site would go on being read. A host whose runbook did
    not load keeps its own host as the key: nothing reads it (`stopped_reason`)."""
    key = key_of(host)
    try:
        found = registry.stop_key(key)
    except registry.RunbookUnavailable:
        return key
    if found == key and registry.for_host(key) is None:  # no runbook: the record's ending stops still hold the domain
        for domain in _ending:
            if key == domain or key.endswith(f".{domain}"):
                return domain
    return found


def item_key(row: BrowserItem) -> str:
    """The host a browser item works on (its link's), else its domain: the key its stop is kept under (`stop_key`)."""
    return key_for(row.link, row.domain)


def key_for(link: str | None, domain: str) -> str:
    """`item_key` from the two columns it reads (the queue's plan reads only those)."""
    from urllib.parse import urlsplit

    try:
        host = urlsplit(link).hostname if link else None
    except ValueError:
        host = None
    return stop_key(host) or domain


def ends_with_the_night(host: str) -> bool:
    """Whether a stop of this host ends at the owner's next off hours start, not for good. A host whose runbook did not
    load, or a domain the record holds an ending stop for that no runbook covers, ends with the night: the way to stop
    it is not known, and a stop for good is never made on a guess."""
    key = stop_key(host)
    try:
        if registry.ends_with_the_night(key):
            return True
    except registry.RunbookUnavailable:
        return True
    return key in _ending and registry.for_host(key) is None


def in_force(row: BrowserDomain, now: datetime | None = None) -> bool:
    """Whether the domain's record says it is stopped now: it holds a stop, and the stop has no end (for good) or its
    end has not come. A stop whose end has come is no stop: the domain is read again."""
    if row.stopped_at is None:
        return False
    until = aware(row.stopped_until)
    return until is None or until > (now or utc_now())


def reset() -> None:
    global _stopped
    _stopped = None
    _ends.clear()
    _ending.clear()


async def load(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """Read the stopped domains from the data (at every start): the stops in force at `now`, for good or not yet at
    their end; returns how many."""
    global _stopped
    now = now or utc_now()
    rows = (await session.execute(select(BrowserDomain.domain, BrowserDomain.stopped_for, BrowserDomain.stopped_until)
                                  .where(BrowserDomain.owner == owner, BrowserDomain.stopped_at.is_not(None)))).all()
    live = [(d, why, aware(until)) for d, why, until in rows]
    live = [(d, why, until) for d, why, until in live if until is None or until > now]
    _stopped = {d: why or "" for d, why, _ in live}
    _ending.clear()
    _ending.update(await session.scalars(select(BrowserDomain.domain).where(
        BrowserDomain.owner == owner, BrowserDomain.stopped_until.is_not(None))))
    _ends.clear()
    _ends.update({d: until for d, _, until in live if until is not None})
    return len(_stopped)


def stopped_reason(host: str, now: datetime | None = None) -> str | None:
    """Why this host is stopped, else None. Its sister hosts on the same domain are not, and a stop that ends is none
    once its end has come."""
    if why := registry.current().refusal(host):  # its runbook did not load: not read until it does (design 2.4a)
        return why
    if not _stopped:
        return None
    key = stop_key(host)
    why = _stopped.get(key)
    if why is not None and (end := _ends.get(key)) is not None and end <= (now or utc_now()):
        return None
    return why


async def refresh(session: AsyncSession, owner: str, host: str, now: datetime | None = None) -> str | None:
    """Why this host is stopped now, else None, asking the domain's record in the data (`stop_key`): a stop in force
    there is taken into this process's snapshot (the fetcher and `stopped_reason` hold off from then on), so a stop
    another process made, the hand-run command's, is seen without a restart. Never clears one: where the record holds
    none in force, the answer is the snapshot's own (a stop only this process holds, or one that has run out). Reads
    only."""
    if why := registry.current().refusal(host):
        return why
    now = now or utc_now()
    key = stop_key(host)
    row = await session.scalar(select(BrowserDomain).where(BrowserDomain.owner == owner, BrowserDomain.domain == key))
    if row is None or not in_force(row, now):
        return stopped_reason(host, now)
    why = row.stopped_for or "refused"
    if _stopped is not None:
        _stopped[key] = why
        if (until := aware(row.stopped_until)) is not None:
            _ends[key] = until
        else:
            _ends.pop(key, None)
    return why


async def waiting_out(session: AsyncSession, owner: str, now: datetime) -> set[str]:
    """The domains stopped until a time that has not come: their items wait for it (nothing ends them), so no worker is
    kept waiting for one of them."""
    return set(await session.scalars(select(BrowserDomain.domain).where(
        BrowserDomain.owner == owner, BrowserDomain.stopped_at.is_not(None), BrowserDomain.stopped_until > now)))


async def _stopped_until(session: AsyncSession, owner: str, domain: str, now: datetime) -> datetime | None:
    """When a stop made at `now` ends: a site whose stop ends (`ends_with_the_night`), the next start of the owner's off
    hours; any other host's, never."""
    if not ends_with_the_night(domain):
        return None
    from app.jobsearch.profile import store as profile_store
    from app.jobsearch.work import hours

    return hours.next_off_hours_start((await profile_store.current(session, owner)).settings, now)


async def stop(session: AsyncSession, owner: str, domain: str, now: datetime, why: str) -> BrowserDomain:
    """Stop a domain (a stop in force keeps its first time, reason and end). Any host is stopped for good, but a domain
    whose stop ends (`ends_with_the_night`): that lasts to the owner's next off hours start. A stop whose end has come
    is made again. The caller commits."""
    from app.jobsearch.browser import pace as browser_pace

    domain = stop_key(domain) or domain
    pace = await browser_pace._pace(session, owner, domain)
    if not in_force(pace, now):
        pace.stopped_at, pace.stopped_for = now, (why or "refused")[:400]
        pace.stopped_until = until = await _stopped_until(session, owner, domain, now)
        log.warning("browser: %s is stopped %s: %s", domain, f"until {until.isoformat()}" if until else "for good",
                    pace.stopped_for)
    pace.cool_until = None  # a stop is not a rest
    if _stopped is not None:
        _stopped[domain] = pace.stopped_for or ""
        if (until := aware(pace.stopped_until)) is not None:
            _ends[domain] = until
            _ending.add(domain)
        else:
            _ends.pop(domain, None)
    return pace


async def record_fetch_refusal(host: str, status: int) -> None:
    """The polite fetcher met a refusal: stop the host's domain in the data, in a short transaction of its own. Does
    nothing until the stops are loaded, and never breaks the fetch."""
    if _stopped is None:
        return
    from app import db
    from app.settings import settings

    domain = stop_key(host)
    if not domain:
        return
    now = utc_now()
    if ends_with_the_night(domain):  # until the stop is kept with its end: a day at most, never for good by a failure
        _stopped[domain], _ends[domain] = f"{host} answered {status}", now + timedelta(days=1)
    else:
        _stopped.setdefault(domain, f"{host} answered {status}")
    try:
        async with db.session_factory()() as session:
            await stop(session, settings.owner, domain, now, f"{host} answered {status}")
            await session.commit()
    except Exception:
        log.exception("browser: could not keep the stop of %s", domain)


async def end_waiting(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """End the waiting items of every domain stopped for good, each with the reason (the role's note says it); returns
    how many. A stop that ends ends nothing: its items wait for it. A leased item finishes with its
    worker. The items are read with no lock, then locked as they are read again (`as_read.lock`: an item another
    transaction holds, one that left Waiting, or one whose link or domain, which name its host, changed since the read
    is left, and the next look ends it): the queue is made with no lock on the mission's row, so a session may be
    leasing one of them."""
    from app.jobsearch.browser import pace as browser_pace

    now = now or datetime.now(UTC)
    stopped = {p.domain: p for p in (await session.scalars(select(BrowserDomain).where(
        BrowserDomain.owner == owner, BrowserDomain.stopped_at.is_not(None),
        BrowserDomain.stopped_until.is_(None)))).all()}
    if not stopped:
        return 0
    rows = (await session.scalars(select(BrowserItem).where(
        BrowserItem.owner == owner, BrowserItem.state == browser_pace.WAITING,
        BrowserItem.domain.in_({domain_of(k) for k in stopped} | set(stopped))))).all()
    held = await as_read.lock(session, BrowserItem, [r for r in rows if item_key(r) in stopped],
                              stands=(BrowserItem.link, BrowserItem.domain),
                              where=(BrowserItem.state == browser_pace.WAITING,))
    for row in held.rows.values():
        why = f"{item_key(row)} is stopped: {stopped[item_key(row)].stopped_for}"
        row.state, row.done_at = browser_pace.STUCK, now
        row.last_result, row.last_note = browser_pace.BLOCKED, why
        if row.role_id is not None and (role := await session.get(Opportunity, row.role_id)) is not None:
            role.browser_note = why
    await session.flush()
    return len(held.rows)


__all__ = ["end_waiting", "ends_with_the_night", "in_force", "is_cookie_notice", "is_own_failure",
           "is_rate_limit", "item_key", "key_of", "load", "record_fetch_refusal", "refresh", "reset", "stop",
           "stop_key", "stopped_reason", "waiting_out"]
