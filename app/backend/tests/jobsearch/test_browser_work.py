"""The browser's work as a queue of small items the app owns (the browser work spec, `browser/items.py`): what the
queue is made of, the leases, the pace per site, what each result does, the retries, and the session's run. The pool's
side (how many workers, in what order, the drain) is test_browser_work_pool.py. Every name, address and sentence is made
up, and no test reaches the network."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app import db
from app.jobsearch import stages
from app.jobsearch.address import is_single_posting
from app.jobsearch.browser import handback as bh
from app.jobsearch.browser import items as bd
from app.jobsearch.browser import leases as bl
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import queue as bq
from app.jobsearch.browser import reader as br
from app.jobsearch.browser import takeable
from app.jobsearch.browser.takeable import Taker
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import (
    BrowserAttempt,
    BrowserDomain,
    BrowserItem,
    JobPosting,
    MissionRun,
    Opportunity,
    Todo,
)
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.models.work import Task
from app.settings import settings
from app.sites import skills
from app.sites.models import Site
from app.timeutil import owner_today
from tests.builders import (
    ONE,
    PATHFINDER,
    TWO,
    WORKER,
    WORKER_B,
    browser_card,
    browser_world,
    hand_back,
    in_session,
    items,
    lease,
    next_item,
    scan_hand_in,
)
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import DEFAULT_OWNER as OWNER
from tests.support import POSTING_WORDS, PROFILE, before_the_lock, committed, learn_sites, reads_on

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out", "example_world")

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)

_SESSION = Taker.session(OWNER, bp.WORKER, owners_only=False)


def _code_taker(kinds: tuple[str, ...]) -> Taker:
    """The app's code-first worker over these kinds, whatever the hour (`owners_only` False)."""
    return Taker(OWNER, bp.WORKER, kinds, code_first=True, owners_only=False)


def _asks(taker: Taker, now: datetime = NOW):
    """What `in_session` runs: `takeable.ask` for this taker."""
    return lambda s: takeable.ask(s, taker, now)

LATER = NOW + timedelta(seconds=20)  # past the page gap the tests fix at 8 seconds
#: The employer's own posting on a job system, and one on a job system the app reads no single posting of.
FEEDSYS = "https://boards.feedsys.test/initech{n}/jobs/40{n}"
HOSTSYS_PAGE = "https://myjobs.hostsys.test/initech{n}careers/cx/job-listing?reqId=50{n}"


async def _role(n: int, **fields) -> uuid.UUID:
    """A Found role found on the made-up board `ONE`, not Sourced unless the test says so."""
    async with db.session_factory()() as session:
        role = Opportunity(**{
            "owner": OWNER, "slug": f"role-{n}", "company": f"Initech {n}", "title": f"Programs Director {n}",
            "match_key": match_key(f"Initech {n}", f"Programs Director {n}"), "origin": "scout",
            "status": "second_impression", "found": date(2026, 10, n), "url": f"https://www.{ONE}/jobs/view/{1000 + n}",
            "sourced_at": None,
            **fields})
        session.add(role)
        await session.commit()
        return role.id


async def _sourced(n: int, link: str = "https://careers.northwind{n}.test/jobs/{n}", **fields) -> uuid.UUID:
    """A Sourced role whose original posting the app's code could not read: a `read` item. Its own link is the
    original posting's once it is sourced, and the board's is kept beside it (`employer_posting.adopt`)."""
    return await _role(n, url=link.format(n=n), board_url=f"https://www.{ONE}/jobs/view/{1000 + n}",
                       source_url=link.format(n=n), source_kind="employer", sourced_at=NOW,
                       analysis_state=analysis.NO_TEXT, **fields)


async def _get(model, key):
    async with db.session_factory()() as session:
        return await session.get(model, key)


#: The owner's Freshness hard pass of 15 days: the Freshness Must dial, the days since the role was posted, at most 15.
FRESH_PASS = {"id": "must-freshness", "kind": "job_fact", "section": "posting", "fact": "posted_days", "op": "<=",
              "value": 15}


async def _hard_pass(*lines: dict, **settings) -> None:
    """The owner's Hard pass with these lines added to what the world has, and these Settings numbers changed."""
    gates = {**PROFILE["gates"], "other": [*PROFILE["gates"]["other"], *lines]}
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates, by="test")
        if settings:
            current = (await profile_store.current(session, OWNER)).settings.model_dump(mode="json")
            await profile_store.save(session, OWNER, "settings", {**current, **settings}, by="test")
        await session.commit()


def _days_ago(days: int) -> date:
    return owner_today() - timedelta(days=days)


async def _posting_of(role_id: uuid.UUID, n: int, *, first_seen_days_ago: int, posted: date | None) -> None:
    """A posting of the role in the index: the employer's date for it, and when the app first saw it."""
    seen = datetime.now(UTC) - timedelta(days=first_seen_days_ago)
    async with db.session_factory()() as session:
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id=f"posting-{n}", company=f"Initech {n}",
            title=f"Programs Director {n}", match_key=match_key(f"Initech {n}", f"Programs Director {n}"),
            url=FEEDSYS.format(n=n), links=[FEEDSYS.format(n=n)], first_seen=seen, last_seen=seen,
            filtered_at=seen, route="titles", opportunity_id=role_id, posted=posted))
        await session.commit()


async def _refresh(now: datetime = NOW) -> None:
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, now))


async def _site(host: str, *, learned: bool = True) -> uuid.UUID:
    """One more site read in the browser, on a registrable domain of its own."""
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=f"https://{host}/jobs", kind="job_board", read_by="browser",
                    created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        if learned:
            await learn_sites(session, site)
        await session.commit()
        return site.id


async def _refused(sid: str, item_id: str, result: str, match: str, **fields) -> None:
    with pytest.raises(br.BrowserError, match=match):
        await hand_back(sid, item_id, result, now=NOW, **fields)


# --- what the queue is made of ------------------------------------------------------------------------------------


async def test_the_queue_is_seeded_with_a_source_item_for_each_board_role_and_a_scan_for_each_mapped_site(js):
    await browser_world()
    one, two = await _role(1), await _role(2)
    await _role(3, status="applied", description=POSTING_WORDS, description_at=NOW)  # it holds its text: no work
    await _refresh()
    by = {(row.kind, row.target): row for row in await items()}
    assert set(by) == {("source", str(one)), ("source", str(two)), ("scan", ONE), ("scan", TWO)}
    source = by[("source", str(one))]
    assert (source.state, source.domain, source.link, source.tries) == (
        "waiting", "example.test", f"https://www.{ONE}/jobs/view/1001", 0)
    assert by[("scan", ONE)].site == ONE and by[("scan", ONE)].link == f"https://www.{ONE}/jobs/"


async def test_a_role_whose_stored_link_is_already_one_employer_posting_is_sourced_in_code_and_never_queued(js):
    """Many roles went on deck at once and the browser opened each before the
    code's sourcing step reached it, though the role's own link was already one posting on the employer's job system
    (its session only "found" that same link). The queue now sources such a role from what it holds, and queues only
    the roles code cannot source."""
    await browser_world()
    held = await _role(1, url=FEEDSYS.format(n=1), apply_url=FEEDSYS.format(n=1), board_url=None)
    open_one = await _role(2)  # only the board's link: a session must follow its Apply button
    await _refresh()
    role = await _get(Opportunity, held)
    assert (role.source_url, role.source_kind) == (FEEDSYS.format(n=1), "employer") and role.sourced_at is not None
    assert {row.target for row in await items(kind="source")} == {str(open_one)}


async def test_a_role_in_followup_needed_is_sourced_by_the_browser_and_handed_out_after_the_rest(js):
    """Many roles sat in Followup needed for good. A role with no text of its posting is not
    ranked, and only a ranked role went on deck to be sourced, so nothing ever followed such a role to the employer's
    own posting, where its text is. It is now sourced like a role on deck, behind everything else: the starred first,
    then the roles on deck, the reads and the scans, then these (priority 2; PRD 32.6)."""
    await browser_world()
    deck = await _role(1)
    waiting = await _role(2, status="followup_needed")
    starred = await _role(3, status="followup_needed", favorited_at=NOW)
    await _role(4, status="first_impression")  # it has its text: nothing to source
    read = await _role(5, status="followup_needed", sourced_at=NOW, source_url=FEEDSYS.format(n=5),
                       source_kind="employer", analysis_state=analysis.NO_TEXT)  # sourced: its text is a read's
    await _role(6, status="followup_needed", url=None, board_url=None)  # no link to open
    await _refresh()
    by = {row.target: row for row in await items(kind="source")}
    assert set(by) == {str(deck), str(waiting), str(starred)}
    assert (by[str(starred)].priority, by[str(deck)].priority, by[str(waiting)].priority) == (0, 1, 2)
    ready = await in_session(WORKER, _asks(_SESSION), commit=False)
    # Old: the backlog's item was handed to a session last. New (PRD 32.6, the owner, 2026-11-13): a role in Followup
    # needed is sourced by code only, so no session is offered it; the code worker still lists it.
    assert [(row.kind, row.target) for row, _ in ready] == [
        ("source", str(starred)), ("source", str(deck)), ("read", str(read)), ("scan", ONE), ("scan", TWO)]
    assert by[str(waiting)].code_only is True and by[str(starred)].code_only is False
    for_code = await in_session(WORKER, _asks(_code_taker(("source",))), commit=False)
    assert str(waiting) in [row.target for row, _ in for_code]


async def test_a_followup_needed_source_item_is_never_handed_to_a_session(js, monkeypatch):
    """The owner, 2026-11-13: "code-only for followup needed". The item is the app's code's alone, on any domain: no
    hand-out, no count of work for a worker, no free domain for one; the code worker still takes it."""
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: True)
    await browser_world(extra_sites=False)
    waiting = await _role(1, status="followup_needed", url="https://jobs.thirdboard.test/view/1", board_url=None)
    await _refresh()
    [item] = await items(kind="source")
    assert (item.target, item.code_only, item.priority) == (str(waiting), True, bp.BACKLOG)
    got = await next_item(WORKER, LATER)
    assert got.get("item", {}).get("kind") != "source", "a session is offered the scan, never the role"
    assert (await in_session(WORKER, lambda s: bq.waiting_count(s, OWNER, bp.WORK[bp.WORKER]), commit=False)) == 0
    assert (await in_session(WORKER, lambda s: takeable.backlog(s, OWNER, bp.WORKER, NOW), commit=False)).count == 0
    assert await in_session(WORKER, lambda s: bq.free(s, OWNER, bp.WORKER, NOW), commit=False) == []
    taken = await in_session(bp.CODE + "-t1", lambda s: bd.code_take(s, OWNER, bp.CODE + "-t1", NOW))
    assert taken is not None and str(taken.id) == str(item.id), "the code worker takes it, whatever its domain"


async def test_a_role_in_followup_needed_the_owner_has_a_hand_in_keeps_its_session_fallback(js):
    """Not code-only: a starred role, a role of the owner's own data, a role on deck (Second Impression) and a role
    asked to be researched. These keep the Claude fallback, unchanged."""
    await browser_world(extra_sites=False)
    starred = await _role(1, status="followup_needed", favorited_at=NOW)
    tracker = await _role(2, status="followup_needed", origin="tracker")
    asked = await _role(3, status="followup_needed", analysis={analysis.ASKED_BY: analysis.ASKED_BY_OWNER})
    deck = await _role(4)
    plain = await _role(5, status="followup_needed")
    await _refresh()
    by = {row.target: row.code_only for row in await items(kind="source")}
    assert by == {str(starred): False, str(tracker): False, str(asked): False, str(deck): False, str(plain): True}
    ready = await in_session(WORKER, _asks(_SESSION), commit=False)
    assert {row.target for row, _ in ready if row.kind == "source"} == {
        str(starred), str(tracker), str(asked), str(deck)}


async def test_a_stuck_followup_item_a_session_gave_up_on_is_tried_again_once_by_code(js):
    """Stuck source items of Followup needed roles were judged by sessions
    under shapes the code did not read. The first plan after the rule opens each once (code_only was false), so the
    code tries it with the fixes and the close follows; a second plan leaves it alone."""
    await browser_world(extra_sites=False)
    role_id = await _role(1, status="followup_needed")
    await _refresh()
    async with db.session_factory()() as session:
        row = (await session.scalars(select(BrowserItem).where(BrowserItem.kind == "source"))).one()
        row.state, row.tries, row.last_result, row.code_only = "stuck", 3, "not_found", False
        await session.commit()
    await _refresh()
    [item] = await items(kind="source")
    assert (item.state, item.tries, item.code_only, item.target) == ("waiting", 0, True, str(role_id))
    async with db.session_factory()() as session:
        (await session.get(BrowserItem, item.id)).state = "stuck"
        await session.commit()
    await _refresh()
    assert (await items(kind="source"))[0].state == "stuck", "once: a stuck item that is code-only already stays"


async def test_the_roles_the_owner_asked_for_are_queued_as_the_owners_items_and_the_rest_as_the_backlog(js):
    """The backlog waits for 1 to 7 am; training new sites can happen anytime (the owner, 2026-11-04). What is handed
    out at any hour is `owners_item`: priority 0 (a star, a move to Ready to Launch before the role was read, a research
    the owner asked for: for the sourcing and for the reading of the role), and a map. A role's source or read the app
    queued by itself, and a site's scan, wait for the off hours (PRD 52.9)."""
    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://jobs.otherboard.test/", kind="job_board", read_by="browser",
                         created_at=NOW))
        await session.commit()
    asked = {analysis.ASKED_BY: analysis.ASKED_BY_OWNER}
    plain, plain_read = await _role(1), await _sourced(5)
    starred, starred_read = await _role(2, favorited_at=NOW), await _sourced(6, favorited_at=NOW)
    ready_asked, ready_asked_read = await _role(3, ready_asked_at=NOW), await _sourced(7, ready_asked_at=NOW)
    asked_deck, asked_read = await _role(4, analysis=asked), await _sourced(8, analysis=asked)
    await _refresh()
    rows = {(row.kind, row.target): row for row in await items()}
    owners = {key for key, row in rows.items() if takeable.owners_item(row)}
    assert {key for key, row in rows.items() if row.priority == 0} == {
        ("source", str(starred)), ("source", str(ready_asked)), ("source", str(asked_deck)),
        ("read", str(starred_read)), ("read", str(ready_asked_read)), ("read", str(asked_read))}
    assert owners - {key for key, row in rows.items() if row.priority == 0} == {
        key for key in rows if key[0] == "map"}, "the owner's items are those, and the mapping of a site"
    assert {("source", str(plain)), ("read", str(plain_read)), ("scan", ONE)} <= set(rows) - owners, "the backlog"


async def test_by_day_the_apps_code_takes_only_the_owners_items_and_by_night_the_rest(js, monkeypatch):
    """The code-first worker keeps to the same window as the sessions (`owners_only`), so by day it reads no posting of
    the backlog, and the 1 to 7 AM run still drains it (PRD 52.9)."""
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await browser_world(extra_sites=False)
    backlog, starred = await _sourced(3), await _sourced(4, favorited_at=NOW)

    async def take() -> str | None:
        async with db.session_factory()() as session:
            row = await bd.code_take(session, OWNER, "code-test0001", NOW)
            target = row.target if row is not None else None
            await session.commit()
        return target

    assert await take() == str(starred), "the owner's own"
    assert await take() is None, "the backlog waits for the night"
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: True)
    assert await take() == str(backlog), "and the night drains it"


async def test_a_role_in_followup_needed_that_holds_its_employers_link_is_sourced_in_code_and_never_queued(js):
    """The roles asked for are one list (`models.wants_sourcing`): the code's own sourcing step looks at the same
    roles the queue does, so a role in Followup needed that already carries one posting on the employer's job system is
    sourced from it and no session is sent to find it."""
    await browser_world()
    held = await _role(1, status="followup_needed", url=FEEDSYS.format(n=1), apply_url=FEEDSYS.format(n=1),
                       board_url=None)
    open_one = await _role(2, status="followup_needed")
    await _refresh()
    role = await _get(Opportunity, held)
    assert (role.source_url, role.source_kind) == (FEEDSYS.format(n=1), "employer") and role.sourced_at is not None
    assert {row.target for row in await items(kind="source")} == {str(open_one)}


async def test_the_source_items_dropped_while_followup_roles_were_left_out_are_opened_again_at_the_back(js):
    """Source items of roles in Followup needed were dropped with no hand-back when the queue
    stopped asking for such roles. The queue's own refresh, at every start and every 30 seconds, opens a
    dropped item again when its role is asked for again, with its tries cleared: no repair to run."""
    await browser_world(extra_sites=False)
    await _role(1, status="followup_needed")
    await _refresh()
    async with db.session_factory()() as session:
        [row] = (await session.scalars(select(BrowserItem).where(BrowserItem.kind == "source"))).all()
        row.state, row.tries, row.priority, row.last_note = "dropped", 1, 1, None  # as the refresh of 10-22 left it
        await session.commit()
    await _refresh(NOW + timedelta(minutes=1))
    [row] = await items(kind="source")
    assert (row.state, row.tries, row.priority, row.done_at) == ("waiting", 0, bp.BACKLOG, None)


async def test_a_role_in_followup_needed_is_chased_only_while_it_would_still_be_fresh_three_days_on(js):
    """The owner (2026-11-04): chase the posting only for roles that will still be fresh after 3 days. With a Freshness
    hard pass of 15 days that is a role posted at most 12 days ago; a role past that waits where it is (PRD 32.6)."""
    await browser_world()
    await _hard_pass(FRESH_PASS)
    today = await _role(1, status="followup_needed", posted=_days_ago(0))
    edge = await _role(2, status="followup_needed", posted=_days_ago(12))
    over = await _role(3, status="followup_needed", posted=_days_ago(13))
    old = await _role(4, status="followup_needed", posted=_days_ago(40))
    unknown = await _role(5, status="followup_needed", posted=None, found=None)  # nothing dates it: nothing says old
    await _refresh()
    assert {row.target for row in await items(kind="source")} == {str(today), str(edge), str(unknown)}
    assert [(await _get(Opportunity, r)).status for r in (over, old)] == ["followup_needed"] * 2, "waiting, not lost"


async def test_with_no_freshness_hard_pass_every_role_in_followup_needed_is_chased_whatever_its_age(js):
    await browser_world()
    ancient = await _role(1, status="followup_needed", posted=_days_ago(400))
    await _refresh()
    assert {row.target for row in await items(kind="source")} == {str(ancient)}


async def test_a_role_on_deck_and_a_role_read_first_are_chased_whatever_their_age(js):
    """A role on deck, a starred role and a role the owner moved to Ready to Launch before it was read stay wanted at
    any age; the same age in Followup needed, with nothing read first about it, is not."""
    await browser_world()
    await _hard_pass(FRESH_PASS)
    deck = await _role(1, status="second_impression", posted=_days_ago(100))
    starred = await _role(2, status="followup_needed", posted=_days_ago(100), favorited_at=NOW)
    asked = await _role(3, status="followup_needed", posted=_days_ago(100), ready_asked_at=NOW)
    await _role(4, status="followup_needed", posted=_days_ago(100))
    await _refresh()
    assert {row.target for row in await items(kind="source")} == {str(deck), str(starred), str(asked)}


async def test_the_margin_the_owner_sets_moves_the_line_a_role_is_chased_up_to(js):
    await browser_world()
    await _hard_pass(FRESH_PASS, chase_fresh_days=10)
    ages = {n: await _role(n, status="followup_needed", posted=_days_ago(n)) for n in (5, 6, 15, 16)}
    await _refresh()
    assert {row.target for row in await items(kind="source")} == {str(ages[5])}, "5 and 10 days are the 15 of the pass"
    await _hard_pass(FRESH_PASS, chase_fresh_days=0)
    await _refresh(NOW + timedelta(minutes=1))
    assert {row.target for row in await items(kind="source")} == {str(ages[n]) for n in (5, 6, 15)}, "no margin: 15"


async def test_a_role_that_ages_out_of_the_chase_loses_its_waiting_item_and_gets_it_back_when_the_pass_allows(js):
    await browser_world()
    role = await _role(1, status="followup_needed", posted=_days_ago(12))
    await _hard_pass(FRESH_PASS)
    await _refresh()
    assert [row.state for row in await items(kind="source")] == ["waiting"]
    await _hard_pass(FRESH_PASS, chase_fresh_days=4)  # 12 days and 4 more are over the 15 of the pass
    await _refresh(NOW + timedelta(minutes=1))
    assert [row.state for row in await items(kind="source")] == ["dropped"]
    assert (await _get(Opportunity, role)).status == "followup_needed", "it waits: its age alone closes nothing"
    await _hard_pass(FRESH_PASS, chase_fresh_days=3)
    await _refresh(NOW + timedelta(minutes=2))
    assert [row.state for row in await items(kind="source")] == ["waiting"]


async def test_the_age_of_a_role_is_counted_the_way_every_gate_counts_it(js):
    """`freshness.posted_basis`: a role's date is held to the day the app first saw its posting, so a role a board dated
    two days ago that the app has had in the index for 20 days is 20 days old, not 2, and is not chased; the same role
    first seen 2 days ago is."""
    await browser_world()
    await _hard_pass(FRESH_PASS)
    redated = await _role(1, status="followup_needed", posted=_days_ago(2))
    young = await _role(2, status="followup_needed", posted=_days_ago(2))
    await _posting_of(redated, 1, first_seen_days_ago=20, posted=_days_ago(2))
    await _posting_of(young, 2, first_seen_days_ago=2, posted=_days_ago(2))
    await _refresh()
    assert {row.target for row in await items(kind="source")} == {str(young)}


async def test_the_codes_own_sourcing_from_a_held_link_skips_the_roles_the_queue_does_not_chase(js):
    """One list: the code's step reads what the queue reads, so a role too old to chase is not sourced from its held
    link either, and a role fresh enough is, and neither is queued."""
    await browser_world()
    await _hard_pass(FRESH_PASS)
    fresh = await _role(1, status="followup_needed", posted=_days_ago(5), url=FEEDSYS.format(n=1),
                        apply_url=FEEDSYS.format(n=1), board_url=None)
    old = await _role(2, status="followup_needed", posted=_days_ago(40), url=FEEDSYS.format(n=2),
                      apply_url=FEEDSYS.format(n=2), board_url=None)
    await _refresh()
    assert (await _get(Opportunity, fresh)).sourced_at is not None
    assert (await _get(Opportunity, old)).sourced_at is None
    assert not await items(kind="source")


async def test_a_site_with_no_runbook_is_a_map_item_and_no_scan(js):
    await browser_world(learned=False)
    await _refresh()
    assert sorted((row.kind, row.target) for row in await items()) == sorted([("map", ONE), ("map", TWO)])
    assert not await items(kind="scan")


async def test_a_sourced_role_with_no_text_is_a_read_item_and_work_no_longer_needed_is_dropped(js):
    await browser_world()
    reading, sourcing_role = await _sourced(1), await _role(2)
    await _refresh()
    assert {(r.kind, r.target) for r in await items(state="waiting") if r.kind != "scan"} == {
        ("read", str(reading)), ("source", str(sourcing_role))}
    read = (await items(kind="read"))[0]
    assert (read.link, read.domain) == ("https://careers.northwind1.test/jobs/1", "northwind1.test")
    async with db.session_factory()() as session:  # sourced and read another way
        role = await session.get(Opportunity, sourcing_role)
        role.status = "gone_quiet"  # out of play (an applied role with no text is work now: PRD 32.6)
        await session.commit()
    await _refresh(NOW + timedelta(minutes=1))
    assert (await items(kind="source"))[0].state == "dropped"


async def test_a_job_the_owner_added_by_its_link_is_read_before_any_role(js):
    await browser_world()
    await _sourced(1)
    async with db.session_factory()() as session:
        posting = JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="owner", ext_id="added", company="Added link",
            title="www.elsewhere.example.test/jobs/77", match_key="added link|www.elsewhere.example.test/jobs/77",
            url="https://www.elsewhere.example.test/jobs/77", links=["https://www.elsewhere.example.test/jobs/77"],
            first_seen=NOW, last_seen=NOW, filtered_at=NOW, route="titles")
        session.add(posting)
        await session.commit()
        posting_id = posting.id
    got = await next_item(WORKER, NOW)
    assert got["item"]["kind"] == "read" and got["item"]["added_by_link"] is True
    assert got["item"]["link"] == "https://www.elsewhere.example.test/jobs/77"
    await _refused(WORKER, got["item"]["item_id"], "text", "title and the company", text=POSTING_WORDS)
    reply = await hand_back(WORKER, got["item"]["item_id"], "text", now=NOW, text=POSTING_WORDS,
                            title="Programs Director", company="Umbrella Works")
    assert reply["item"] == "done"
    posting = await _get(JobPosting, posting_id)
    assert (posting.title, posting.company, posting.text) == ("Programs Director", "Umbrella Works", POSTING_WORDS)


async def test_a_scan_comes_back_eight_hours_after_the_last_one(js):
    await browser_world(extra_sites=False)
    item_id = await lease("scan", ONE, now=NOW)
    await hand_back(WORKER, item_id, "done", now=NOW)
    await _refresh(NOW + timedelta(hours=7))
    assert (await items(kind="scan"))[0].state == "done"
    await _refresh(NOW + timedelta(hours=8, minutes=1))
    assert (await items(kind="scan"))[0].state == "waiting"


# --- handing out items, one at a time -----------------------------------------------------------------------------


async def test_a_worker_gets_one_item_with_its_instructions_first_and_the_same_item_while_it_holds_it(js):
    await browser_world()
    role = await _role(1)
    first = await next_item(WORKER, NOW)
    item = first["item"]
    assert item["kind"] == "source" and item["role"]["title"] == "Programs Director 1"
    assert item["results"] == ["found", "easy_apply", "not_found", "gone", "blocked"]
    assert "Scouting a website for jobs" in first["instructions"] and "browser.hand_back" in first["instructions"]
    assert "postings:" in first["response_template"]
    assert item["runbook"]["kind"] == "browser", "the board's runbook, for its section on the original posting"
    again = await next_item(WORKER, NOW)
    assert again["item"]["item_id"] == item["item_id"] and "hold this item" in again["note"]
    assert "instructions" not in again, "the instructions come once"
    row = (await items(kind="source"))[0]
    assert (row.state, row.leased_by, row.target) == ("leased", WORKER, str(role))
    assert row.lease_until == NOW + bp.LEASE


async def test_source_comes_before_read_and_read_before_scan(js):
    await browser_world(extra_sites=False)
    await _sourced(1)
    await _role(2)
    kinds = []
    for sid in ("browser_reader-1", "browser_reader-2", "browser_reader-3"):
        got = await next_item(sid, NOW)
        kinds.append(got["item"]["kind"] if "item" in got else "none")
    # source and scan are both on the board's domain, so only one of them goes at a time; read is on its own
    assert kinds == ["source", "read", "none"]


async def test_the_youngest_role_is_sourced_first(js):
    await browser_world(extra_sites=False)
    old, young = await _role(2), await _role(8)
    got = await next_item(WORKER, NOW)
    assert got["item"]["role"]["title"] == "Programs Director 8"
    assert {old, young}


async def test_a_session_that_started_on_role_work_never_takes_a_scan_and_the_pathfinder_takes_only_maps(js):
    await browser_world(extra_sites=False)
    await _sourced(1)
    first = await next_item(WORKER, NOW)
    assert first["item"]["kind"] == "read"
    await hand_back(WORKER, first["item"]["item_id"], "unreadable", now=NOW, note="the page shows only a preview")
    done = await next_item(WORKER, LATER)
    assert done["stop"] is True, "the scan waits for a session of its own"
    assert (await items(kind="scan"))[0].state == "waiting"
    assert (await next_item(PATHFINDER, LATER))["stop"] is True, "nothing to map"
    scan = await next_item(WORKER_B, LATER)
    assert scan["item"]["kind"] == "scan"


async def test_a_session_with_nothing_it_can_take_is_told_to_stop_and_its_run_closes(js):
    await browser_world(learned=False, extra_sites=False)
    got = await next_item(WORKER, NOW)
    assert got["stop"] is True and "nothing more" in got["why"]
    async with db.session_factory()() as session:
        run = (await session.scalars(select(MissionRun))).one()
    assert run.outcome == "done" and run.ended_at is not None and run.summary == "Handed back nothing."


async def test_a_browser_session_is_stopped_after_its_share_of_items_and_a_fresh_one_continues(js, monkeypatch):
    """Every page a session read stays in its context, so a long one costs more a turn. After `MAX_ITEMS_PER_SESSION`
    the session is told to stop, its run closes, and the next session takes the rest."""
    monkeypatch.setattr(bd, "MAX_ITEMS_PER_SESSION", 2)
    await browser_world(extra_sites=False)
    for n in (1, 2, 3):
        await _role(n)
    at = NOW
    for _ in range(2):
        got = await next_item(WORKER, at)
        assert got["item"]["kind"] == "source" and "end_session" not in got
        await hand_back(WORKER, got["item"]["item_id"], "gone", now=at, note="No longer accepting applications")
        at += timedelta(seconds=20)
    stopped = await next_item(WORKER, at)
    assert stopped["stop"] is True and stopped["end_session"] is True and "2 items" in stopped["why"]
    async with db.session_factory()() as session:
        runs = (await session.scalars(select(MissionRun))).all()
    (run,) = [r for r in runs if (r.started or {}).get("session_id") == WORKER]
    assert run.outcome == "done" and run.ended_at is not None and run.started["taken"] == 2
    fresh = await next_item(WORKER_B, at)
    assert fresh["item"]["kind"] == "source", "a fresh session continues where it stopped"
    assert [i.state for i in await items(kind="source")].count("waiting") == 0


async def test_the_pathfinder_has_no_cap_and_a_stop_with_nothing_to_take_ends_the_session(js, monkeypatch):
    monkeypatch.setattr(bd, "MAX_ITEMS_PER_SESSION", 0)
    await browser_world(learned=False, extra_sites=False)
    got = await next_item(PATHFINDER, NOW)
    assert got["item"]["kind"] == "map", "a map takes as long as a site needs: no cap"
    monkeypatch.setattr(bd, "MAX_ITEMS_PER_SESSION", 8)
    stopped = await next_item(WORKER, NOW)
    assert stopped["stop"] is True and stopped["end_session"] is True, "nothing to take: the last word"


async def test_a_call_that_names_no_session_is_refused(js):
    await browser_world()
    async with db.session_factory()() as session:
        with pytest.raises(br.BrowserError, match="names no session"):
            await bd.next_item(session, OWNER, NOW)


async def test_a_paused_mission_or_peak_hours_hand_out_nothing(js):
    await browser_world(resumed=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    assert got["stop"] is True and "paused" in got["why"]


# --- the pace per domain ------------------------------------------------------------------------------------------


async def test_one_worker_per_domain_and_workers_side_by_side_on_different_domains(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await _role(2)
    await _sourced(3)
    a = await next_item(WORKER, NOW)
    b = await next_item(WORKER_B, NOW)
    c = await next_item("browser_reader-cccc", NOW)
    assert a["item"]["domain"] == "example.test" and b["item"]["domain"] == "northwind3.test"
    assert c["stop"] is True, "the board's other role waits for the board to be free"


async def test_after_a_hand_back_the_domain_waits_its_page_gap_and_the_worker_is_told_to_wait(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await _role(2)
    first = await next_item(WORKER, NOW)
    await hand_back(WORKER, first["item"]["item_id"], "not_found", now=NOW, note="no posting on the careers site")
    wait = await next_item(WORKER, NOW + timedelta(seconds=2))
    assert wait["wait_seconds"] == 6, "8 seconds after the hand-back"
    assert (await next_item(WORKER, NOW + timedelta(seconds=9)))["item"]["kind"] == "source"


async def test_the_heartbeat_keeps_the_lease_and_spaces_the_pages_of_a_site(js):
    await browser_world(extra_sites=False)
    item_id = await lease("scan", ONE, now=NOW)
    beat = await in_session(WORKER, lambda s: bd.heartbeat(s, OWNER, NOW))
    assert beat["holding"] is True and beat["wait_ms"] == 0, "the domain is free: load now"
    beat = await in_session(WORKER, lambda s: bd.heartbeat(s, OWNER, NOW + timedelta(seconds=3)))
    assert beat["wait_ms"] == 5000, "8 seconds after the last page"
    row = await _get(BrowserItem, uuid.UUID(item_id))
    assert row.lease_until == NOW + timedelta(seconds=3) + bp.LEASE
    idle = await in_session(WORKER_B, lambda s: bd.heartbeat(s, OWNER, NOW))
    assert idle["holding"] is False


async def test_a_rate_limit_page_rests_the_domain_an_hour_then_eight_and_uses_no_try(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    reply = await hand_back(WORKER, got["item"]["item_id"], "blocked", now=NOW, note="a rate-limit page")
    assert reply["item"] == "waiting" and "rests until" in reply["note"]
    row = (await items(kind="source"))[0]
    assert row.tries == 0 and row.next_at == NOW + timedelta(hours=1)
    async with db.session_factory()() as session:
        pace = await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == "example.test"))
    assert pace.blocks == 1 and pace.cool_until == NOW + timedelta(hours=1)
    assert (await next_item(WORKER_B, NOW + timedelta(minutes=30)))["stop"] is True, "cooling"
    again = await next_item(WORKER_B, NOW + timedelta(hours=1, seconds=1))
    await hand_back(WORKER_B, again["item"]["item_id"], "blocked", now=NOW + timedelta(hours=1, seconds=1),
                    note="429 too many requests")
    async with db.session_factory()() as session:
        pace = await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == "example.test"))
    assert pace.blocks == 2 and pace.cool_until == NOW + timedelta(hours=9, seconds=1)


async def test_a_success_after_a_block_starts_the_count_again(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await _role(2)
    got = await next_item(WORKER, NOW)
    await hand_back(WORKER, got["item"]["item_id"], "blocked", now=NOW, note="a rate-limit page")
    after = NOW + timedelta(hours=1, seconds=1)
    got = await next_item(WORKER, after)
    await hand_back(WORKER, got["item"]["item_id"], "not_found", now=after, note="no posting on the careers site")
    async with db.session_factory()() as session:
        pace = await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == "example.test"))
    assert pace.blocks == 0


# --- leases ---------------------------------------------------------------------------------------------------------


async def test_a_lease_that_runs_out_returns_the_item_logged_as_no_result(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    other = await next_item(WORKER_B, NOW + timedelta(minutes=11))
    assert other["item"]["item_id"] == got["item"]["item_id"], "back in the queue, and taken again"
    async with db.session_factory()() as session:
        attempt = (await session.scalars(select(BrowserAttempt))).one()
    assert (attempt.outcome, attempt.session_id) == ("no_result", WORKER) and "lease ran out" in attempt.note
    with pytest.raises(br.BrowserError, match="not the item you hold"):
        await hand_back(WORKER, got["item"]["item_id"], "not_found", now=NOW + timedelta(minutes=12), note="none here")


async def test_activity_renews_the_lease(js):
    await browser_world(extra_sites=False)
    await scan_hand_in([browser_card(1)], now=NOW)
    await scan_hand_in([browser_card(2)], now=NOW + timedelta(minutes=8))
    assert (await next_item(WORKER_B, NOW + timedelta(minutes=12)))["stop"] is True, "the scan is still the first's"
    row = (await items(kind="scan", target=ONE))[0]
    assert row.state == "leased" and row.lease_until == NOW + timedelta(minutes=18)


async def test_a_session_that_ends_gives_its_item_back_at_once_and_its_run_closes(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await next_item(WORKER, NOW)
    await in_session(WORKER, lambda s: bl.release_session(s, OWNER, WORKER, "the session ended", NOW))
    row = (await items(kind="source"))[0]
    assert row.state == "waiting" and row.leased_by is None
    async with db.session_factory()() as session:
        run = (await session.scalars(select(MissionRun))).one()
    assert run.outcome == "failed" and "ended holding an item" in run.summary


async def test_a_session_that_hands_nothing_back_for_ten_minutes_is_ended(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await next_item(WORKER, NOW)
    quiet = await in_session(WORKER, lambda s: bq.silent(s, OWNER, WORKER, NOW + timedelta(minutes=5)))
    assert quiet is False
    quiet = await in_session(WORKER, lambda s: bq.silent(s, OWNER, WORKER, NOW + timedelta(minutes=11)))
    assert quiet is True and (await items(kind="source"))[0].state == "waiting"


# --- what each result does ------------------------------------------------------------------------------------------


async def test_found_sources_the_role_at_the_employers_posting(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    got = await next_item(WORKER, NOW)
    reply = await hand_back(WORKER, got["item"]["item_id"], "found", now=NOW, url=FEEDSYS.format(n=1))
    assert reply["item"] == "done"
    role = await _get(Opportunity, role_id)
    assert role.sourced_at is not None and role.source_url == FEEDSYS.format(n=1) and role.source_kind == "employer"
    await _refresh(NOW + timedelta(minutes=1))
    read = await items(kind="read")
    assert [r.target for r in read] == [str(role_id)], "no text came by code: a read item follows"


async def test_a_board_address_is_never_found_and_easy_apply_is_the_listing(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    got = await next_item(WORKER, NOW)
    item_id = got["item"]["item_id"]
    for board in ("https://www.board.test/jobs/view/4472056376", "https://www.thirdboard.test/job-detail/abc",
                  f"https://www.{ONE}/jobs/view/1001"):
        await _refused(WORKER, item_id, "found", "not a source", url=board)
    await _refused(WORKER, item_id, "easy_apply", "off the board is found", url=FEEDSYS.format(n=1),
                   text=POSTING_WORDS)
    await _refused(WORKER, item_id, "easy_apply", "text is required", url=f"https://www.{ONE}/jobs/view/1001")
    reply = await hand_back(WORKER, item_id, "easy_apply", now=NOW, url=f"https://www.{ONE}/jobs/view/1001",
                            text=POSTING_WORDS)
    assert reply["item"] == "done"
    role = await _get(Opportunity, role_id)
    assert role.source_kind == "board" and role.description == POSTING_WORDS


async def test_an_employers_posting_on_a_boards_host_is_taken_as_found(js):
    """A careers site on a host that is also a job board's was refused as "a job board's address",
    and 19 source items ended with that note."""
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    posting = "https://www.searchsite.test/about/careers/applications/jobs/results/123456789-program-manager"
    got = await next_item(WORKER, NOW)
    item_id = got["item"]["item_id"]
    await _refused(WORKER, item_id, "found", "not a source", url="https://www.searchsite.test/search?q=programs+director")
    assert (await hand_back(WORKER, item_id, "found", now=NOW, url=posting))["item"] == "done"
    role = await _get(Opportunity, role_id)
    assert (role.source_url, role.source_kind) == (posting, "employer") and role.sourced_at is not None


async def test_a_job_system_address_is_always_taken_as_found(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    assert not is_single_posting(HOSTSYS_PAGE.format(n=1)), "a page the app's own rule would refuse"
    got = await next_item(WORKER, NOW)
    await hand_back(WORKER, got["item"]["item_id"], "found", now=NOW, url=HOSTSYS_PAGE.format(n=1))
    assert (await _get(Opportunity, role_id)).source_url == HOSTSYS_PAGE.format(n=1)


async def test_a_job_system_address_the_system_lists_as_another_job_is_refused_and_the_item_stays_held(js):
    """A session's `found` was taken because the address was on a known job system, though it opened
    another job's requisition. The system's own list names that job, so the address is refused with what to fix."""
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    link = FEEDSYS.format(n=1)
    async with db.session_factory()() as session:
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="feedsys", ext_id="401", company="Initech 1",
            title="Field Logistics Manager", match_key=match_key("Initech 1", "Field Logistics Manager"), url=link,
            first_seen=NOW, last_seen=NOW, links=[]))
        await session.commit()
    got = await next_item(WORKER, NOW)
    item_id = got["item"]["item_id"]
    await _refused(WORKER, item_id, "found", "not taken as this role's posting.*Field Logistics Manager", url=link)
    role = await _get(Opportunity, role_id)
    assert (role.source_url, role.sourced_at) == (None, None)
    reply = await hand_back(WORKER, item_id, "not_found", now=NOW, note="no posting with this title on the site")
    assert reply["ok"] is True, "the item stayed the session's: it could still be handed back"


async def test_not_found_waits_a_day_and_after_three_tries_the_reason_is_on_the_roles_card(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    at = NOW
    for n in range(1, 4):
        got = await next_item(WORKER, at)
        assert got["item"]["kind"] == "source", n
        reply = await hand_back(WORKER, got["item"]["item_id"], "not_found", now=at,
                                note=f"no posting with this title on the careers site ({n})")
        row = (await items(kind="source"))[0]
        assert row.tries == n
        if n < 3:
            assert reply["item"] == "waiting" and row.next_at == at + timedelta(hours=24)
            assert (await next_item(WORKER, at + timedelta(hours=23)))["stop"] is True, "it waits its day"
        at += timedelta(hours=24, seconds=1)
    assert reply["item"] == "stuck" and "last try" in reply["note"]
    role = await _get(Opportunity, role_id)
    assert role.status == stages.SECOND_IMPRESSION and role.sourced_at is None
    assert role.browser_note == "no posting with this title on the careers site (3)"
    await _refresh(at + timedelta(days=2))
    assert (await items(kind="source"))[0].state == "stuck", "it stays for the owner"


async def test_a_not_found_because_the_browser_would_not_open_a_tab_uses_no_try_and_goes_back(js):
    """Every item a session took while tabs would not open used one of its three tries."""
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    reply = await hand_back(WORKER, got["item"]["item_id"], "not_found", now=NOW,
                            note="the browser wouldn't open any tab (Target.createTarget failed), page never seen")
    row = (await items(kind="source"))[0]
    assert row.tries == 0 and row.state == "waiting" and row.next_at == NOW + bh.TAB_RETRY
    assert "used no try" in reply["note"]
    # The app's own digest saying the tab did not open counts too, whatever the note says.
    again = await next_item(WORKER, NOW + bh.TAB_RETRY + timedelta(seconds=1))
    bh.note_tab_failed(again["item"]["item_id"])
    await hand_back(WORKER, again["item"]["item_id"], "not_found", now=NOW + bh.TAB_RETRY + timedelta(seconds=1),
                    note="no posting could be looked at for this listing")
    assert (await items(kind="source"))[0].tries == 0
    # A plain not_found still uses its try.
    last = await next_item(WORKER, NOW + 2 * bh.TAB_RETRY + timedelta(seconds=2))
    await hand_back(WORKER, last["item"]["item_id"], "not_found", now=NOW + 2 * bh.TAB_RETRY + timedelta(seconds=2),
                    note="no posting with this title on the careers site")
    assert (await items(kind="source"))[0].tries == 1


async def test_the_card_shows_why_the_browser_gave_up(js):
    from app.jobsearch.pipeline import row as role_row

    await browser_world(extra_sites=False)
    role_id = await _role(1, browser_note="no posting with this title on the careers site")
    async with db.session_factory()() as session:
        card = await role_row.row(session, OWNER, role_id)
    assert "no posting with this title" in card["next"]


async def test_gone_ends_the_role_as_posting_gone(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    got = await next_item(WORKER, NOW)
    await _refused(WORKER, got["item"]["item_id"], "gone", "words the page showed")
    await hand_back(WORKER, got["item"]["item_id"], "gone", now=NOW, note="No longer accepting applications")
    role = await _get(Opportunity, role_id)
    assert role.status == "gone_quiet" and role.ended_reason == "posting_gone"


async def test_text_keeps_the_whole_posting_and_a_shortened_or_placeholder_text_is_refused(js):
    await browser_world(extra_sites=False)
    role_id = await _sourced(1)
    got = await next_item(WORKER, NOW)
    item_id = got["item"]["item_id"]
    for bad, why in ((POSTING_WORDS + "\n\n[... rest of the description omitted ...]", "shortened"),
                     (POSTING_WORDS + "\n\n(truncated)", "shortened"),
                     ("Programs Director. Apply now.", "a snippet"),
                     ("placeholder", "a snippet")):
        await _refused(WORKER, item_id, "text", why, text=bad)
    reply = await hand_back(WORKER, item_id, "text", now=NOW, text=POSTING_WORDS)
    assert reply["item"] == "done"
    role = await _get(Opportunity, role_id)
    assert role.description == POSTING_WORDS


@pytest.mark.parametrize("held", [POSTING_WORDS, "Lead programs for the board."], ids=["a whole text", "a snippet"])
async def test_a_text_handed_back_for_a_role_under_gos_browser_check_settles_the_check(js, held):
    """Go's check that a posting is still open rides on a read item (`go_checks.browser_checks`): a text handed back
    says the page still shows the posting, whatever the role held. A whole text is kept; a snippet gives way to the
    posting, as it does for any read (a snippet's role must not throw the text away)."""
    await browser_world(extra_sites=False)
    role_id = await _sourced(1, status=stages.READY_TO_LAUNCH, description=held, go_check="browser",
                             go_checked_at=datetime.now(UTC))
    item_id = await lease("read", str(role_id), now=NOW)
    assert (await hand_back(WORKER, item_id, "text", now=NOW, text=POSTING_WORDS))["item"] == "done"
    role = await _get(Opportunity, role_id)
    assert role.go_check == "open" and role.description == POSTING_WORDS


async def test_a_text_handed_back_for_a_role_the_check_on_entering_ready_left_to_the_browser_settles_that_check(js):
    """The check made when a role comes into Ready to Launch (docs/PRD.md 24.16) rides on the same read item, at
    priority 0, any hour: a text handed back says the page still shows the posting, and the role stays marked as the
    entry check, which Go's own run does not take as its check."""
    await browser_world(extra_sites=False)
    role_id = await _sourced(1, status=stages.READY_TO_LAUNCH, description=POSTING_WORDS, go_check="looking",
                             go_checked_at=datetime.now(UTC))
    item_id = await lease("read", str(role_id), now=NOW)
    assert (await hand_back(WORKER, item_id, "text", now=NOW, text=POSTING_WORDS))["item"] == "done"
    assert (await _get(Opportunity, role_id)).go_check == "entered"


async def test_a_placeholder_note_is_refused(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    for note in ("placeholder", "tbd", "...", "n/a", ""):
        await _refused(WORKER, got["item"]["item_id"], "not_found", "note", note=note)


async def test_unreadable_waits_a_day_and_does_not_end_the_role(js):
    await browser_world(extra_sites=False)
    role_id = await _sourced(1)
    got = await next_item(WORKER, NOW)
    reply = await hand_back(WORKER, got["item"]["item_id"], "unreadable", now=NOW, note="only a preview shows")
    assert reply["item"] == "waiting" and "tomorrow" in reply["note"]
    role = await _get(Opportunity, role_id)
    assert role.status == stages.SECOND_IMPRESSION and role.description is None


async def test_a_result_the_item_does_not_take_is_refused(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    await _refused(WORKER, got["item"]["item_id"], "text", "one of found, easy_apply")


async def test_every_attempt_is_logged_with_who_when_what_and_a_note(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    await hand_back(WORKER, got["item"]["item_id"], "not_found", now=NOW, note="no posting on the careers site")
    async with db.session_factory()() as session:
        row = (await session.scalars(select(BrowserAttempt))).one()
    assert (row.session_id, row.at, row.outcome, row.note, row.kind) == (
        WORKER, NOW, "not_found", "no posting on the careers site", "source")


# --- scans and maps -------------------------------------------------------------------------------------------------


async def test_a_scan_hands_in_listings_with_no_cap_and_is_done(js):
    await browser_world(extra_sites=False)
    reply = None
    for batch in range(8):  # 200 postings: more than the old cap of 150
        reply = await scan_hand_in([browser_card(batch * 25 + n) for n in range(25)], now=NOW)
    assert reply["handed"] == 25
    item_id = str((await items(kind="scan"))[0].id)
    await hand_back(WORKER, item_id, "done", now=NOW)
    async with db.session_factory()() as session:
        run = (await session.scalars(select(MissionRun))).one()
        count = len((await session.scalars(select(JobPosting))).all())
    assert count == 200
    assert "Scanned Listings." in run.summary and "Handed in 200 postings, 200 new." in run.summary


async def test_postings_go_only_with_a_scan_or_map_item_and_only_for_its_site(js):
    await browser_world()
    async with db.session_factory()() as session:
        from tests.builders import as_session

        with as_session(WORKER), pytest.raises(br.BrowserError, match="scan or map item you hold"):
            await bd.postings(session, OWNER, [browser_card(1)], now=NOW)
    with pytest.raises(br.BrowserError, match="is not one of the sites"):
        await scan_hand_in([browser_card(1, site=TWO)], site=ONE, now=NOW)


async def test_a_scan_whose_runbook_no_longer_fits_sends_the_site_to_the_pathfinder(js):
    ids = await browser_world(extra_sites=False)
    item_id = await lease("scan", ONE, now=NOW)
    await hand_back(WORKER, item_id, "failed", now=NOW, note="the Date posted menu is gone")
    async with db.session_factory()() as session:
        site = await session.get(Site, ids[ONE])
        assert await skills.current_skill(session, site.id) is None
        assert site.learn_note == "the Date posted menu is gone"
    await _refresh(NOW + timedelta(minutes=1))
    assert [r.target for r in await items(kind="map", state="waiting")] == [ONE]


async def test_a_map_is_done_only_after_its_runbook_is_kept(js):
    from tests.support import browser_recipe

    ids = await browser_world(learned=False, extra_sites=False)
    got = await next_item(PATHFINDER, NOW)
    item = got["item"]
    assert item["kind"] == "map" and item["site"] == ONE and "runbook_template" in item
    assert "You are the Pathfinder" in got["instructions"]
    await _refused(PATHFINDER, item["item_id"], "done", "browser.skill first")
    await in_session(PATHFINDER, lambda s: bd.postings(s, OWNER, [browser_card(1)], now=NOW))
    kept = await in_session(PATHFINDER, lambda s: bd.skill(s, OWNER, ONE, browser_recipe(ONE), now=NOW))
    assert kept["kept"] is True
    await hand_back(PATHFINDER, item["item_id"], "done", now=NOW)
    async with db.session_factory()() as session:
        assert await skills.current_skill(session, ids[ONE]) is not None


async def test_a_failed_map_counts_a_try_waits_a_day_and_the_fifth_switches_the_site_off(js):
    ids = await browser_world(learned=False, extra_sites=False)
    at = NOW
    for n in range(1, 6):
        got = await next_item(PATHFINDER, at)
        assert got["item"]["kind"] == "map", n
        await hand_back(PATHFINDER, got["item"]["item_id"], "failed", now=at, note="the results never load")
        at += timedelta(hours=24, seconds=1)
    async with db.session_factory()() as session:
        site = await session.get(Site, ids[ONE])
    assert site.learn_tries == 5 and site.on is False and site.off_by == "app"


# --- the run's line, item by item -------------------------------------------------------------------------------------


async def test_the_runs_line_says_what_was_handed_back_item_by_item(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await _role(2)
    await _role(3)
    at = NOW
    for result, fields in (("found", {"url": FEEDSYS.format(n=1)}),
                           ("not_found", {"note": "no posting on the careers site"}),
                           ("gone", {"note": "No longer accepting applications"})):
        got = await next_item(WORKER, at)
        await hand_back(WORKER, got["item"]["item_id"], result, now=at, **fields)
        at += timedelta(seconds=20)
    # the role sourced with no text came by code is read next, at its own posting
    follow = await next_item(WORKER, at)
    assert follow["item"]["kind"] == "read" and follow["item"]["link"] == FEEDSYS.format(n=1)
    await hand_back(WORKER, follow["item"]["item_id"], "unreadable", now=at, note="only a preview shows")
    stop = await next_item(WORKER, at + timedelta(seconds=20))
    assert stop["stop"] is True
    async with db.session_factory()() as session:
        run = (await session.scalars(select(MissionRun))).one()
    assert run.outcome == "done"
    assert run.summary == ("Sourced 1 role. 1 posting had been taken down; its role ended. Couldn't find the original "
                           "posting of 1 role: no posting on the careers site. Couldn't read 1 posting in full: only a "
                           "preview shows.")


def test_a_runs_line_never_says_no_postings_when_roles_were_sourced():
    words = bl.summary_words({"items": [{"kind": "source", "result": "found", "domain": "board.test"}] * 9})
    assert words == "Sourced 9 roles." and "no postings" not in words.lower()


# --- what the owner sees ------------------------------------------------------------------------------------------


async def test_the_strip_says_each_workers_item_by_domain_and_role(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await _sourced(2)
    await next_item(WORKER, NOW)
    await next_item(WORKER_B, NOW)
    doing = await in_session(WORKER, lambda s: bq.doing(s, OWNER), commit=False)
    assert doing == {
        WORKER: "example.test: finding the original posting of Programs Director 1 at Initech 1",
        WORKER_B: "northwind2.test: reading Programs Director 2 at Initech 2"}


async def test_the_health_line_counts_items_done_in_the_hour_and_the_oldest_waiting(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await _role(2)
    got = await next_item(WORKER, NOW)
    await hand_back(WORKER, got["item"]["item_id"], "not_found", now=NOW, note="no posting on the careers site")
    line = await in_session(WORKER, lambda s: bq.health(s, OWNER, bp.WORKER, NOW + timedelta(minutes=10)))
    assert line == "Done in the last hour: 1 source, 0 read, 0 scan. 2 waiting, the oldest for 10 minutes."
    assert await in_session(WORKER, lambda s: bq.health(s, OWNER, bp.PATHFINDER, NOW)) is None


async def test_a_stall_is_shown_after_thirty_minutes_and_told_to_the_owner_only_when_it_lasts(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await _refresh()
    line = await in_session(WORKER, lambda s: bq.health(s, OWNER, bp.WORKER, NOW + timedelta(minutes=40)))
    assert "Stalled: nothing handed back for 40 minutes" in line
    await in_session(WORKER, lambda s: bq.watch(s, OWNER, NOW + timedelta(minutes=40)))
    async with db.session_factory()() as session:
        assert not (await session.scalars(select(Task))).all(), "not yet the owner's"
    await in_session(WORKER, lambda s: bq.watch(s, OWNER, NOW + timedelta(hours=2, minutes=1)))
    async with db.session_factory()() as session:
        task = (await session.scalars(select(Task))).one()
    assert task.state == "open" and "stalled" in task.title
    got = await next_item(WORKER, NOW + timedelta(hours=2, minutes=2))
    await hand_back(WORKER, got["item"]["item_id"], "not_found", now=NOW + timedelta(hours=2, minutes=2),
                    note="no posting on the careers site")
    await in_session(WORKER, lambda s: bq.watch(s, OWNER, NOW + timedelta(hours=2, minutes=3)))
    async with db.session_factory()() as session:
        task = (await session.scalars(select(Task))).one()
    assert task.state == "done", "work moves again"


async def test_work_waiting_on_a_job_board_stopped_until_the_next_night_is_no_stall_and_tells_the_owner_nothing(
        js, monkeypatch):
    """A refusal at Exboard stops it until the owner's next off hours (PRD 48.14 d), so its items wait for hours with
    nothing a worker may take: the Agents page must not say "Stalled" and the Inbox must not be told for it. (A starred
    role's item, which is handed out by day: the only kind that could be a stall then.)"""
    from app.jobsearch.fetch import host_stops
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await browser_world(extra_sites=False)
    link = "https://www.board.test/jobs/view/7001/"
    await _role(1, url=link, board_url=link, favorited_at=NOW)
    await _refresh()
    assert [(i.domain, i.state) for i in await items(kind="source")] == [("board.test", "waiting")]
    assert "Stalled" in await _health(NOW + timedelta(minutes=40)), "the control: without the stop it is a stall"
    await in_session(WORKER, lambda s: host_stops.stop(s, OWNER, "board.test", NOW, "a security check"))
    for hours in (1, 2):  # the made-up window opens at 1 AM, three hours after NOW
        assert "Stalled" not in await _health(NOW + timedelta(hours=hours))
        await in_session(WORKER, lambda s, h=hours: bq.watch(s, OWNER, NOW + timedelta(hours=h)))
    async with db.session_factory()() as session:
        assert not (await session.scalars(select(Task))).all(), "and nothing is told to the owner"


async def _scan_waits_ready(monkeypatch, *, by_day: bool) -> None:
    """The made-up site's scan is in the queue and ready since `NOW`, and nothing else is."""
    from app.jobsearch.work import hours as night

    if by_day:
        monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await browser_world(extra_sites=False)
    await _refresh()
    assert [(i.kind, i.state) for i in await items()] == [("scan", "waiting")]


async def _health(at: datetime) -> str:
    return await in_session(WORKER, lambda s: bq.health(s, OWNER, bp.WORKER, at)) or ""


async def test_a_scan_that_waits_for_the_off_hours_is_no_stall_by_day(js, monkeypatch):
    """The Agents page said "Stalled: nothing handed back for 8 hours" by day, and the app
    logged it at every restart, while the only items ready were the night's site scans. A scan, a role's source or read
    the app queued by itself and a recording's repair are the backlog (a map goes at any hour: PRD 52.9): the pool
    hands them out only in the owner's off hours (`owners_only`), so by day nothing may take them, and work that
    nothing may take is not stalled."""
    await _scan_waits_ready(monkeypatch, by_day=True)
    for hours in (1, 8):
        assert "Stalled" not in await _health(NOW + timedelta(hours=hours))
        await in_session(WORKER, lambda s, h=hours: bq.watch(s, OWNER, NOW + timedelta(hours=h)))
    async with db.session_factory()() as session:
        assert not (await session.scalars(select(Task))).all(), "and nothing is told to the owner"


async def test_a_scan_that_waits_in_the_off_hours_is_a_stall(js, monkeypatch):
    await _scan_waits_ready(monkeypatch, by_day=False)
    line = await _health(NOW + timedelta(hours=1))
    assert "1 waiting, the oldest for 1 hour." in line and "Stalled: nothing handed back for 1 hour" in line


async def test_a_scan_ready_since_the_afternoon_has_waited_for_a_worker_only_since_the_night_began(js, monkeypatch):
    """Without this the night began with "nothing handed back for 12 hours" (and an Inbox item) until its first scan
    came back: the clock of a scheduled item starts when the owner's off hours (1 AM here) do."""
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    await _scan_waits_ready(monkeypatch, by_day=False)  # ready since 18:00 local; the night began at 01:00 local
    began = NOW + timedelta(hours=7)  # 05:00 UTC the next day: 01:00 in New York
    assert "Stalled" not in await _health(began + timedelta(minutes=10)), "ten minutes into the night"
    assert "Stalled: nothing handed back for 40 minutes" in await _health(began + timedelta(minutes=40))


async def test_a_scan_the_owner_asked_for_has_waited_for_a_worker_only_since_they_asked(js, monkeypatch):
    """Run now and Wake up make the kind's work the owner's, handed out at any hour (`pool.owner_running`)."""
    from app.jobsearch.pool import core as pool

    await _scan_waits_ready(monkeypatch, by_day=True)
    asked = NOW + timedelta(hours=3)
    await committed(pool.owner_started_mission, OWNER, "read_browser", asked)
    assert "Stalled" not in await _health(asked + timedelta(minutes=10)), "ten minutes after Run now"
    assert "Stalled: nothing handed back for 40 minutes" in await _health(asked + timedelta(minutes=40))


async def test_a_starred_roles_item_that_waits_by_day_is_a_stall(js, monkeypatch):
    """What the owner starred is handed out at any hour (`owners_item`), so it waiting by day is a stall."""
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await browser_world(extra_sites=False)
    await _role(1, favorited_at=NOW)
    await _refresh()
    assert "Stalled: nothing handed back for 40 minutes" in await _health(NOW + timedelta(minutes=40))


async def test_a_backlog_role_item_that_waits_by_day_is_no_stall(js, monkeypatch):
    """Changed 2026-11-04 (the owner: "backlog should wait until 1-7am", PRD 52.9): a role's source or read item the app
    queued by itself waits for the off hours like a scan (`owners_only`), so by day nothing may take it, and work that
    nothing may take is not stalled. It was a stall while a source or read was handed out at any hour."""
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await browser_world(extra_sites=False)
    await _role(1)
    await _role(2, status="followup_needed")
    await _sourced(3)
    await _refresh()
    assert "Stalled" not in await _health(NOW + timedelta(hours=8))
    await in_session(WORKER, lambda s: bq.watch(s, OWNER, NOW + timedelta(hours=8)))
    async with db.session_factory()() as session:
        assert not (await session.scalars(select(Task))).all(), "and nothing is told to the owner"


async def _pathfinder_health(at: datetime) -> str:
    return await in_session(PATHFINDER, lambda s: bq.health(s, OWNER, bp.PATHFINDER, at)) or ""


async def _no_stall_told(at: datetime) -> None:
    await in_session(WORKER, lambda s: bq.watch(s, OWNER, at))
    async with db.session_factory()() as session:
        assert not (await session.scalars(select(Task))).all(), "and nothing is told to the owner"


async def test_a_site_to_map_that_waits_while_peak_hours_hold_the_pool_is_no_stall(js, monkeypatch):
    """A map is handed out at any hour (`owners_item`), but while Claude's peak hours hold
    the pool no session starts, so a site added in the morning read "Stalled" after 30 minutes and raised the Inbox item
    after 2 hours. `open_work` already leaves work the pool is held from out; the stall now does too."""
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    monkeypatch.setattr(night, "in_peak", lambda s, now=None: True)
    await browser_world(learned=False, extra_sites=False)
    await _refresh()
    assert [(i.kind, i.state) for i in await items()] == [("map", "waiting")]
    for hours in (1, 3):
        assert "Stalled" not in await _pathfinder_health(NOW + timedelta(hours=hours))
    await _no_stall_told(NOW + timedelta(hours=3))


async def test_a_starred_roles_item_that_waits_while_peak_hours_hold_the_pool_is_no_stall(js, monkeypatch):
    """The same gap, found with the base: a starred role's item is the owner's (any hour), and peak hours still hold the
    sessions that would take it."""
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    monkeypatch.setattr(night, "in_peak", lambda s, now=None: True)
    await browser_world(extra_sites=False)
    await _role(1, favorited_at=NOW)
    await _refresh()
    assert "Stalled" not in await _health(NOW + timedelta(hours=3))
    await _no_stall_told(NOW + timedelta(hours=3))


async def test_a_map_waits_for_a_worker_only_since_the_peak_hours_that_held_it_ended(js, monkeypatch):
    """Without this the first look after the peak window found "nothing handed back for 3 hours" (and an Inbox item):
    work the pool was held from has waited for a worker only since the hold lifted (`availability.counts_from`)."""
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await browser_world(learned=False, extra_sites=False)
    await _hard_pass(avoid_peak=True)  # the owner avoids Claude's peak hours (the test world's profile does not)
    morning = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)  # 8 AM in Los Angeles, a Friday: inside the default window
    await _refresh(morning)
    ended = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)  # 11 AM there: the window ended
    assert "Stalled" not in await _pathfinder_health(ended + timedelta(minutes=10)), "ten minutes after the hold"
    assert "Stalled: nothing handed back for 40 minutes" in await _pathfinder_health(ended + timedelta(minutes=40))


async def test_a_kind_the_owner_paused_raises_no_stall_and_a_raised_one_is_closed(js):
    """The browser reader, paused by the owner, raised "The browser work has stalled"
    in the Inbox some hours later. Work a pause holds is not stalled."""
    from app.jobsearch.pool import core as pool

    await browser_world(extra_sites=False)
    await _role(1)
    await _refresh()
    await in_session(WORKER, lambda s: bq.watch(s, OWNER, NOW + timedelta(hours=2, minutes=1)))
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Task))).one().state == "open"
        await pool.pause(session, OWNER, pool.BROWSER_READER)
        await session.commit()
    await in_session(WORKER, lambda s: bq.watch(s, OWNER, NOW + timedelta(hours=5)))
    async with db.session_factory()() as session:
        task = (await session.scalars(select(Task))).one()
    assert task.state == "done" and task.note == "You paused this work.", "and no new one while it is paused"


# --- pausing ----------------------------------------------------------------------------------------------------------


async def test_pausing_a_kind_lets_a_worker_finish_its_item_and_then_tells_it_to_stop(js):
    from app.jobsearch.pool import core as pool

    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    async with db.session_factory()() as session:
        await pool.pause(session, OWNER, pool.BROWSER_READER)
        await session.commit()
    assert (await items(kind="source"))[0].state == "leased", "the item stays the worker's"
    assert (await next_item(WORKER_B, NOW))["stop"] is True, "no other is handed one"
    held = await next_item(WORKER, NOW)
    assert held["stop"] is True and "Hand back the item you hold first" in held["next"]
    reply = await hand_back(WORKER, got["item"]["item_id"], "not_found", now=NOW, note="no posting on the careers site")
    assert reply["item"] == "waiting"
    assert (await next_item(WORKER, LATER))["stop"] is True


async def test_peak_hours_stop_a_browser_session_only_once_it_holds_no_item(js, monkeypatch):
    from app.jobsearch.work import hours as night
    from app.jobsearch.work import session_lifecycle

    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER)  # the heartbeat reads the real clock
    monkeypatch.setattr(night, "blackout", lambda s, now=None: "Claude's peak hours.")
    beat = {"session_id": WORKER, "state": "alive", "lane": "any", "runner": "session-2"}

    async def heartbeat():
        async with db.session_factory()() as session:
            stop = await session_lifecycle.settle(session, OWNER, beat)
            await session.commit()
            return stop

    assert await heartbeat() is False, "it finishes the item it holds"
    await hand_back(WORKER, got["item"]["item_id"], "not_found", note="no posting on the careers site")
    assert await heartbeat() is True
    assert (await next_item(WORKER))["stop"] is True


async def test_a_stuck_item_whose_role_was_sourced_another_way_is_dropped_and_the_note_cleared(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    await _refresh()
    async with db.session_factory()() as session:
        row = await session.scalar(select(BrowserItem).where(BrowserItem.kind == "source"))
        row.state = "stuck"
        role = await session.get(Opportunity, role_id)
        role.browser_note = "no posting on the careers site"
        role.sourced_at, role.source_url, role.source_kind = NOW, FEEDSYS.format(n=1), "employer"
        await session.commit()
    await _refresh(NOW + timedelta(minutes=1))
    assert (await items(kind="source"))[0].state == "dropped"
    assert (await _get(Opportunity, role_id)).browser_note is None


async def test_items_on_a_site_of_its_own_domain_go_to_another_worker(js):
    await browser_world(extra_sites=False)
    await _site("jobs.otherboard.test")
    a = await next_item(WORKER, NOW)
    b = await next_item(WORKER_B, NOW)
    assert {a["item"]["domain"], b["item"]["domain"]} == {"example.test", "otherboard.test"}


# --- a block cools the site that blocked -----------------------------------------------------------


async def test_where_apply_leads_is_found_even_when_it_answers_403_and_a_block_there_is_refused(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    got = await next_item(WORKER, NOW)
    employer = "https://isba-jobs.careerwebsite.test/job/director/86310730/"
    await _refused(WORKER, got["item"]["item_id"], "blocked", "hand it back as found", url=employer,
                   note="403 Forbidden")
    await hand_back(WORKER, got["item"]["item_id"], "found", now=NOW, url=employer)
    assert (await _get(Opportunity, role_id)).source_url == employer
    async with db.session_factory()() as session:
        cooling = (await session.scalars(select(BrowserDomain).where(BrowserDomain.cool_until.is_not(None)))).all()
    assert cooling == [], "nothing rests: the board did not block, and the employer's page is read as its own item"


async def test_a_rate_limit_rests_the_domain_of_the_page_that_limited_not_the_items(js):
    await browser_world(extra_sites=False)
    await _sourced(1)
    got = await next_item(WORKER, NOW)
    assert got["item"]["domain"] == "northwind1.test"
    reply = await hand_back(WORKER, got["item"]["item_id"], "blocked", now=NOW, note="a rate-limit page",
                            url="https://login.othersso.test/authorize?x=1")
    assert reply["note"].startswith("othersso.test rests until")
    async with db.session_factory()() as session:
        paces = {p.domain: p for p in (await session.scalars(select(BrowserDomain))).all()}
        attempt = (await session.scalars(select(BrowserAttempt))).one()
    assert paces["othersso.test"].cool_until == NOW + timedelta(hours=1) and paces["othersso.test"].blocks == 1
    assert paces["northwind1.test"].cool_until is None and paces["northwind1.test"].blocks == 0
    assert "(at https://login.othersso.test/authorize?x=1)" in attempt.note


async def test_a_rate_limit_on_the_listings_own_site_still_rests_the_board(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    await hand_back(WORKER, got["item"]["item_id"], "blocked", now=NOW, note="a rate-limit page",
                    url=f"https://www.{ONE}/authwall")
    async with db.session_factory()() as session:
        pace = await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == "example.test"))
    assert pace.cool_until == NOW + timedelta(hours=1)


# --- every role in Fact checking has its item ---------------------------------


async def test_every_fact_checking_role_with_a_link_has_a_source_item_and_one_with_none_says_why(js):
    await browser_world(extra_sites=False)
    employer = await _role(1, url="https://careers.stripe-like.test/jobs/77")  # on no site read in the browser
    elsewhere = await _role(2, url="https://www.fourthboard.test/c/initech/job/abc")  # a board nobody marked
    nolink = await _role(3, url=None)
    await _refresh()
    sources = {r.target: r for r in await items(kind="source")}
    assert str(employer) in sources and str(elsewhere) in sources and str(nolink) not in sources
    assert sources[str(employer)].site is None and sources[str(employer)].domain == "stripe-like.test"
    assert (await _get(Opportunity, nolink)).browser_note == br.NO_LINK
    async with db.session_factory()() as session:
        (await session.get(Opportunity, nolink)).url = "https://careers.northwind3.test/jobs/3"
        await session.commit()
    await _refresh(NOW + timedelta(minutes=1))
    assert (await _get(Opportunity, nolink)).browser_note is None
    assert str(nolink) in {r.target for r in await items(kind="source")}


async def test_two_sessions_asking_at_once_never_deadlock_on_the_missions_row(js, monkeypatch):
    """Found 2026-10-23: `browser.next_item` failed with a Postgres deadlock on the mission row.
    A session's first call inserts its run, which holds a KEY SHARE lock on the mission row (the run's foreign key)
    until the commit; the hand-out then takes that row FOR UPDATE, and FOR UPDATE waits for every other KEY SHARE. Two
    sessions starting at once each held one and wanted the other's gone. The row is now locked before anything else."""

    await browser_world(extra_sites=False)
    await _sourced(1)
    await _role(2)
    arrived = []
    both_in = asyncio.Event()
    real_refresh = bq.refresh

    async def meet_then_refresh(session, owner, now=None):
        arrived.append(1)
        if len(arrived) >= 2:
            both_in.set()
        try:  # fixed: the second session waits for the first's commit and never gets here, so it gives up waiting
            await asyncio.wait_for(both_in.wait(), 2)
        except TimeoutError:
            pass
        return await real_refresh(session, owner, now)

    monkeypatch.setattr(bq, "refresh", meet_then_refresh)
    # How long the second call waits for the first's turn is the app's own limit (`db.LOCK_TIMEOUT_MS`, 5 s): past it a
    # call is told to wait and opens no run, as it should. That is not what this test is about, and it made the test
    # fail once on a machine at a twentieth of its speed (the first call held the turn for
    # over 5 s, the second was told to wait, and one run was found, not two). The wait is long here, so two calls that
    # both get through open two runs however long the first takes. A deadlock is still caught: Postgres raises it, or
    # the gather's own limit does.
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 300_000)
    got = await asyncio.wait_for(asyncio.gather(next_item("browser_reader-1", NOW), next_item("browser_reader-2", NOW)),
                                 120)
    assert all(g["ok"] for g in got)
    assert len(await _runs()) == 2


async def _runs() -> list[MissionRun]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(MissionRun).where(MissionRun.owner == OWNER))).all())


# --- the queue is made without the mission's row, and applied only to rows still as they were read ------------------
# (the hand-out once held the mission's row while the queue was made)


async def test_a_refresh_never_drops_an_item_a_session_leased_meanwhile(js, monkeypatch):
    await browser_world()
    gone = await _role(1)
    await _refresh()
    (item,) = await items(kind="source", target=str(gone))
    async with db.session_factory()() as session:
        (await session.get(Opportunity, gone)).status = "gone_quiet"  # no longer work: the next refresh drops its item
        await session.commit()
    real_apply = bq._apply

    async def a_session_leases_it_after_the_plan_was_read(*args, **kwargs):
        async with db.session_factory()() as other:
            row = await other.get(BrowserItem, item.id)
            row.state, row.leased_by, row.lease_until = bp.LEASED, WORKER_B, NOW + bp.LEASE
            await other.commit()
        return await real_apply(*args, **kwargs)

    monkeypatch.setattr(bq, "_apply", a_session_leases_it_after_the_plan_was_read)
    await _refresh()
    (after,) = await items(kind="source", target=str(gone))
    assert (after.state, after.leased_by) == (bp.LEASED, WORKER_B), "the lease was kept, not dropped"


async def test_the_items_a_refresh_changes_are_locked_in_one_id_order_whatever_the_order_of_the_plan(js, monkeypatch):
    """Every pass takes rows in id order (`as_read.lock`), so none is half of a cycle of waits with another: the items a
    refresh drops are locked in id order across its statements, not in id order within each chunk of the plan's own."""
    await browser_world()
    roles = [await _role(n) for n in range(1, 8)]
    await _refresh()
    async with db.session_factory()() as session:
        for role in roles:
            (await session.get(Opportunity, role)).status = "gone_quiet"  # no longer work: the refresh drops its item
        await session.commit()
    monkeypatch.setattr(bq, "CHUNK", 2)
    with reads_on("js_browser_items") as reads:
        await _refresh()
    asked = [i for _s, ids in reads for i in ids]
    assert len(asked) == 7 and asked == sorted(asked), "the seven items are locked in id order, a statement at a time"
    assert {item.state for item in await items(kind="source")} == {bp.DROPPED}


async def test_a_hand_out_holds_the_mission_lock_for_the_lease_only(js, transactions, monkeypatch):
    """The queue was made (every role and every item of the owner read) with the
    mission's row locked, so every other hand-out waited about a second behind it."""
    import re

    await browser_world()
    await _role(1)
    await _sourced(2)
    transactions.clear()
    marker = "-- the mission's turn was taken"
    real_lock = bp.lock_mission

    async def lock_and_note(session, owner):
        await real_lock(session, owner)
        transactions.timeline.append(marker)  # after the real turn is taken: what follows is sent holding it

    monkeypatch.setattr(bp, "lock_mission", lock_and_note)
    got = await next_item(WORKER, NOW)
    assert got["ok"] and got["item"]["kind"] in ("source", "read"), got
    seen = [re.sub(r"\s+", " ", s) for s in transactions.timeline if s != transactions.END]
    lock = seen.index(marker)
    held = seen[lock:]
    assert [s for s in seen[:lock] if "FROM js_browser_items" in s], "the queue was made first, with no row locked"
    assert not [s for s in held if "FROM js_opportunities" in s and "js_opportunities.id = " not in s], (
        "no load of the roles while the mission's row is locked")
    assert not [s for s in held if re.search(r"FROM js_browser_items WHERE js_browser_items.owner = \S+$", s)], (
        "no load of every item while the mission's row is locked")


async def _three_waiting_items() -> list[BrowserItem]:
    await browser_world()
    for n in (1, 2, 3):
        await _role(n)
    await _refresh()
    return sorted(await items(kind="source"), key=lambda item: item.target)


async def test_marking_the_held_items_leaves_an_item_a_session_leased_since_it_was_read(js, monkeypatch):
    """The note of a leased item is its worker's: an item that left Waiting between the read and the lock is not marked
    (the lock's `where` on its state)."""
    free, leased, _ = await _three_waiting_items()
    monkeypatch.setattr(settings, "browser_domains", "elsewhere.test")  # every item is for another domain: held back

    async def a_session_leases_it():
        async with db.session_factory()() as other:
            row = await other.get(BrowserItem, leased.id)
            row.state, row.leased_by, row.lease_until = bp.LEASED, WORKER_B, NOW + bp.LEASE
            await other.commit()

    before_the_lock(monkeypatch, a_session_leases_it)
    async with db.session_factory()() as session:
        await bp.mark_held(session, OWNER)
        await session.commit()
    rows = {item.id: item for item in await items(kind="source")}
    assert rows[free.id].last_note == bp.held_note()
    assert (rows[leased.id].state, rows[leased.id].last_note) == (bp.LEASED, None), "the leased item is left as it is"


async def test_a_note_the_queue_clears_asks_the_deck_to_look_again_so_the_role_rejoins_its_walk(js):
    from tests.jobsearch.work_queue.support import posted_now

    await browser_world(extra_sites=False)
    role_id = await _role(1)
    await _refresh()
    async with db.session_factory()() as session:
        row = await session.scalar(select(BrowserItem).where(BrowserItem.kind == "source"))
        row.state = "stuck"
        role = await session.get(Opportunity, role_id)
        role.browser_note = "no posting on the careers site"
        role.sourced_at, role.source_url, role.source_kind = NOW, FEEDSYS.format(n=1), "employer"
        await session.commit()
        await session.execute(delete(Todo))
        await session.commit()
    await _refresh(NOW + timedelta(minutes=1))
    assert (await _get(Opportunity, role_id)).browser_note is None
    assert ("deck.dirty", "") in await posted_now(), "the note is read by the deck now: a role given up rejoins"












# --- the page read of an Exboard role: what the queue does with a `listing` item (the owner, 2026-11-05; 48.14) -----
# A role in Followup needed found by the nightly Exboard search has its page opened by the app's code, for the text
# that stage, gates and rank read; the employer's own posting is looked for later, at the read-next mark
# (`models.SOURCE_NEXT`). Items of the kind `listing` are the code's alone: no session takes one.

THIRD_JOB = "https://www.thirdboard.test/job-detail/abc{n}"






async def _board_role(n: int, link: str, **fields) -> uuid.UUID:
    """A role in Followup needed found on a board, with no Exboard search posting of its own."""
    return await _role(n, status="followup_needed", url=link, board_url=link, **fields)


async def _listing_item(role_id: uuid.UUID, posting_id: uuid.UUID, n: int, *, order_at: datetime | None = None) -> None:
    """The item of a role's page, as the queue makes it."""
    async with db.session_factory()() as session:
        session.add(BrowserItem(
            owner=OWNER, kind=bp.LISTING, target=str(role_id), role_id=role_id, posting_id=posting_id,
            link=f"https://www.board.test/jobs/view/{7200 + n}/", domain="board.test", state="waiting", tries=0,
            next_at=NOW, priority=bp.BACKLOG, order_at=order_at, created_at=NOW))
        await session.commit()
















# --- the queue makes them: an Exboard role in Followup needed is read, not followed (the owner, 2026-11-05; 48.14) --

LI_ASK = {analysis.ASKED_BY: analysis.ASKED_BY_OWNER}


















# --- the page read of a role whose employer's text is out of reach (the owner, 2026-11-05; 48.14 f) ------------------
# Exboard's copy stands in where the employer's own posting cannot be read, so the page of such a role is read,
# whatever its stage and whichever link it holds: the nightly search's posting, or the tracker's import. In talks it is
# the owner's own, at any hour; every other stage is the backlog. A role the employer's text can still come to is not
# read.








