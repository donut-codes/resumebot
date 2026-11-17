"""Builders the tests share: the made-up documents, packets, rounds, postings and runs that several test files set up
the same way. A test file never imports another test file; what two of them need lives here. Nothing here is anyone's
data: every name, number and sentence is made up."""

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from process_engine.models import COMPLETED, ProcHistory
from sqlalchemy import delete, func, select

from app import db
from app.agents import roster
from app.jobsearch import judged, process, stages
from app.jobsearch.address import is_board_link
from app.jobsearch.browser import handback as bh
from app.jobsearch.browser import items as bd
from app.jobsearch.browser import leases as bl
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import queue as bq
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.imports import tracker
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.measures import pay_place as g
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import (
    BrowserItem,
    CleansedPosting,
    Employer,
    JobPosting,
    Mission,
    Opportunity,
    ParseJob,
    RawPosting,
    RoleRank,
    SearchRun,
    SeatRun,
)
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, prioritize
from app.jobsearch.work.door import Caller, caller
from app.sites.models import Site
from app.timeutil import owner_today
from tests.support import (
    DEFAULT_OWNER,
    EXAMPLE_SYSTEM,
    FIVE,
    OWNER,
    POSTING_WORDS,
    PROFILE,
    answered,
    learn_sites,
    load_profile,
    tracker_row,
    tracker_workbook,
)

#: The instant the made-up browser world starts at, and the one the postings made for the readers are dated from.
WORLD_NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
READ_NOW = datetime.now(UTC)


# --- the owner's documents and a written packet ---

RESUME = {
    "summary": "Builds programs that people use.",
    "achievements": [{"id": "a-statewide", "text": "Led a statewide program", "note": ""}],
    "competencies": [{"id": "gov", "area": "Governance", "items": ["Risk Frameworks"]}],
    "roles": [{
        "id": "northwind-lead", "employer": "Northwind Labs", "location": "Oakfield, ST", "title": "Senior Manager",
        "start": "2022", "end": "2026", "tools": ["Visio", "Miro"], "bullets": [
            {"id": "b-risk", "text": "Led the risk working group across client engagements", "note": ""},
            {"id": "b-kb", "text": "Built the practice knowledge base", "note": ""},
        ],
        "evidence": [{"id": "e-board", "title": "Board deck", "text": "Presented the risk register each quarter"}],
    }],
    "education": [{"id": "ms", "degree": "MS in Leadership", "school": "Example University"}],
}
ABOUT = {"name": "Pat Example", "location": "Oakfield, ST", "phone": "555-010-0199", "email": "pat@example.test"}
WRITING = {"voice_rules": [{"id": "plain", "says": "Plain."}],
           "banned": [{"id": "puff", "phrase": "spearheaded", "why": "puffery"}]}
LETTER = {"paragraphs": ["I would like to help.", "Thank you."]}


async def save_documents(session) -> None:
    """Save the made-up resume, contact details and writing rules for the owner, as the owner would in the app, with
    the story library already built from them."""
    for kind, data in {"resume": RESUME, "about": ABOUT, "writing": WRITING}.items():
        await profile_store.save(session, OWNER, kind, data, by="test")
    await _master_read(session)
    await library_built(session)


async def _master_read(session) -> None:
    """The master's structured record and its claims are built by sessions of the pool (waiting jobs). A test that is
    not about them gets them as already read (the claims as a record that holds none), so no parser job waits in the
    pool's way."""
    from app.jobsearch.postings.record import Field, Record
    from app.jobsearch.resume import cleansed_master, structured_profile

    version = await profile_store.newest_version(session, OWNER, "resume")
    if version and await structured_profile.stored(session, OWNER, version) is None:
        await structured_profile.keep(session, OWNER, version, Record({"title": Field(["x"], "code", "x")}))
    if version and await cleansed_master.stored(session, OWNER, version) is None:
        await cleansed_master.keep(session, OWNER, version, Record({"title": Field(["x"], "code", "x")}))
    await session.execute(delete(ParseJob).where(ParseJob.owner == OWNER, ParseJob.kind.in_(("profile", "claims"))))


#: The story library the made-up documents' tests write from (`library_built`): a scope line, a line for each bullet,
#: the achievement and one letter block, each citing its master lines.
LIBRARY = {
    "rules": {"protected": [], "approved": [], "roles": {}},
    "lines": [
        {"id": "S-northwind-lead", "kind": "scope", "role": "northwind-lead",
         "text": "Ran the practice's risk work across client engagements.", "from": ["b-risk"]},
        {"id": "L001", "kind": "bullet", "role": "northwind-lead", "entry": "b-risk", "work": "risk", "side": "client",
         "for": "programs", "text": "Led the risk working group across client engagements", "from": ["b-risk"]},
        {"id": "L002", "kind": "bullet", "role": "northwind-lead", "entry": "b-kb", "work": "kb", "side": "firm",
         "for": "programs", "text": "Built the practice knowledge base", "from": ["b-kb"]},
        {"id": "L003", "kind": "achievement", "role": None, "entry": "a-statewide", "work": "statewide", "side": "",
         "for": "programs", "text": "Led a statewide program", "from": ["a-statewide"]},
    ],
    "stories": [{"id": "risk", "title": "The risk group", "index": "Risk work that clients used",
                 "blocks": [{"id": "risk-a", "for": "programs", "sentences": [
                     {"text": "The risk working group met across client engagements.", "from": ["b-risk"]}]}]}],
}


def compose_reply(**over) -> dict:
    """What the composer seat sends for the made-up documents: every check accepts the packet it makes. `over`
    replaces top-level parts, and `resume` parts by their own names."""
    resume = {"headline": "Programs Leader",
              "summary": "Builds programs that people use, across client engagements.", "summary_from": ["L001"],
              "competencies": [{"area": "Governance", "items": [{"term": "Risk Frameworks", "backed_by": "L001"}]}],
              "achievements": [], "roles": [{"role": "northwind-lead", "intro": "", "lines": ["L001"]}]}
    resume |= {k: over.pop(k) for k in list(over) if k in resume}
    reply = {
        "thread": "They need programs that run.", "resume": resume, "blocks": ["risk-a"],
        "letter": {"paragraphs": [{"text": "Dear hiring team,", "from": []},
                                  {"text": "I would like to help you run programs.", "from": ["L001"]}]},
    }
    return {**reply, **over}


#: The facts gate's answer when it finds nothing.
FACTS_CLEAN = {"claims": [], "failures": [], "dismissed": []}


async def library_built(session, owner: str = OWNER, data: dict | None = None) -> None:
    """The story library as already built for the documents in force (a build a save started is closed first), so a
    test that is not about the library writes packets from `LIBRARY` at once."""
    from app.jobsearch.models import StoryLibrary
    from app.jobsearch.writing import library as story_library

    for row in (await session.scalars(select(StoryLibrary).where(
            StoryLibrary.owner == owner, StoryLibrary.status == "building"))).all():
        row.status = "failed"
        if row.instance_id:
            await process.engine().cancel(session, row.instance_id, "a test builds its own")
    resume = await profile_store.newest_version(session, owner, "resume")
    writing = await profile_store.newest_version(session, owner, "writing")
    docs = await profile_store.owner_documents(session, owner)
    version = (await session.scalar(select(func.max(StoryLibrary.version)).where(StoryLibrary.owner == owner)) or 0) + 1
    session.add(StoryLibrary(owner=owner, version=version, status="ready", resume_version=resume,
                             writing_version=writing, done_at=datetime.now(UTC),
                             data={**(data or LIBRARY), "sources": story_library.sources(docs.resume)}))
    await session.flush()


async def run_seats(replies: dict, owner: str = OWNER, *, most: int = 20) -> list[str]:
    """Answer every seat that waits, in the order the processes hand them out: each with `replies[element]` (a reply
    dict, a function of the seat's system text that returns one, or a string sent as the raw reply text). Returns the
    elements answered, in order."""
    from app.jobsearch.writing import seats

    done: list[str] = []
    for _ in range(most):
        async with db.session_factory()() as session:
            taken = (await seats.take(session, owner, "test-seat"))["seat"]
            await session.commit()
        if taken is None:
            return done
        async with db.session_factory()() as session:
            element = (await session.get(SeatRun, uuid.UUID(taken["id"]))).element
            reply = replies[element]
            reply = reply(taken["system"]) if callable(reply) else reply
            text = reply if isinstance(reply, str) else json.dumps(reply)
            await seats.answer(session, owner, taken["id"], {"text": text, "spend": {"cost_usd": 0.01}})
            await session.commit()
        done.append(element)
    return done


async def write_packet(**replies) -> list[str]:
    """Answer the seats of the packets waiting: by default a clean packet; `replies` replaces a seat's answer by its
    step's id."""
    clean = {"Compose": compose_reply(), "Facts_1": FACTS_CLEAN}
    return await run_seats({**clean, **replies})


#: A role's research that works out a pay ask (the packet's ask is worked out from it).
RESEARCH = {"pay": {"band": [120000, 150000]}, "required": [{"item": "Programs", "class": "P", "from": ["b-risk"]}]}


async def packet_setup(with_resume=True) -> uuid.UUID:
    """The profile (and, unless `with_resume` is false, the documents) and one Found role; returns the role's id."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        if with_resume:
            await save_documents(session)
        role = Opportunity(owner=OWNER, slug="globex-lead", company="Globex", title="Programs Lead", fit=5,
                           origin="scout", match_key="globex|programs lead", status="second_impression",
                           found=owner_today(),
                           url="https://jobs.example.test/1", description="Lead programs for the board. " * 2,
                           analysis=RESEARCH)
        session.add(role)
        await roster.ensure_roster(session, OWNER)
        await session.commit()
        return role.id


async def analysis_setup(text: str | None = POSTING_WORDS) -> dict[str, Opportunity]:
    """Three Found roles scored on the made-up asks worth 5: fit 4 (80%), fit 3 (60%) and fit 2 (40%), and ranked
    80, 60 and 40. Each is Sourced
    (a role that is not is never researched) and holds `text` as its posting's text (a role with none is never
    researched), or none when `text` is None."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        prof = await profile_store.current(session, OWNER)
        rows = {}
        for company, said in (("Fabrikam", ("met", "met", "not")), ("Initech", ("met", "not", "met")),
                              ("Hooli", ("met", "not", "not"))):
            breakdown = scoring.compute(answered(*said), prof)
            role = Opportunity(owner=OWNER, slug=company.lower(), company=company, title="Programs Lead",
                               fit=breakdown["total"], origin="scout", match_key=f"{company.lower()}|programs lead",
                               status=stages.SECOND_IMPRESSION, found=owner_today(), location="Remote, US",
                               move_type="remote",
                               url=f"https://jobs.example.test/{company.lower()}", fit_breakdown=breakdown,
                               description=text, source_url=f"https://jobs.example.test/{company.lower()}",
                               source_kind="employer", sourced_at=datetime.now(UTC))
            session.add(role)
            rows[company] = role
        await session.flush()
        # The Haiku rank is what orders the reads (the owner, 2026-10-21): the same order as the fit, best first.
        await rank_roles(session, {rows[c].id: rank for c, rank in (("Fabrikam", 80), ("Initech", 60), ("Hooli", 40))})
        await session.commit()
        return {k: v.id for k, v in rows.items()}


#: A career page that shows the whole posting: past the least a page may give, a line to each paragraph.
PARAGRAPHS = [f"Paragraph {i} of the posting says one thing about the work and nothing else." for i in range(1, 21)]
# The page names the role in its title, which is never part of the text.
PAGE = ("<html><head><title>Programs Lead - Acme Careers</title></head><body>"
        + "".join(f"<p>{p}</p>" for p in PARAGRAPHS) + "</body></html>")
WHOLE = "\n".join(PARAGRAPHS)


def finds_are_sourced(monkeypatch) -> None:
    """The roles a search round stores are Sourced and hold their posting's text by the time the analyst's line is
    planned (the sweep that follows the round's finds, `sourcing.sweep`, is replaced by one that does that), so the
    tests of the order the finds are researched in need no web of their own."""
    from sqlalchemy import update

    from app.jobsearch.postings import sourced as sourcing

    async def sweep(session, owner):
        done = await session.execute(update(Opportunity).where(
            Opportunity.owner == owner, Opportunity.sourced_at.is_(None), Opportunity.status.in_(sourcing.IN_PLAY))
            .values(sourced_at=datetime.now(UTC), source_kind="employer",
                    source_url="https://careers.example.test/jobs/1", description=POSTING_WORDS))
        return {"looked": done.rowcount, "sourced": done.rowcount}

    monkeypatch.setattr(sourcing, "sweep", sweep)


def serve_page(monkeypatch, body: str = PAGE, status: int = 200) -> SimpleNamespace:
    """Answer every address of the made-up web with this page (robots.txt apart: none). `pages` is what was asked."""
    seen = SimpleNamespace(pages=[])

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.pages.append(str(request.url))
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0))
    return seen


def findings(role_id, **over) -> dict:
    """The analyst's findings for a role. The made-up posting states no place, so they say so (`place_not_stated`): a
    test of the place sends its own `location`, or `place_not_stated=False` to see the analyst asked for it."""
    return {
        "role": str(role_id),
        "place_not_stated": True,
        "target_role": "none",  # the analyst says which target role the job matches, or none (`analysis.to_match`)
        "employer": {"employees": 300, "founded": 1990, "industry": "Software", "ownership": "private",
                     "sources": {"employees": "https://about.example.test"}},
        "pay": {"band": [150000, 180000], "band_source": "the posting"},
        "posting_text": "Lead programs. Requires five years.",
        "required": [{"item": "Five years of programs", "class": "P", "why": "the risk working group",
                      "from": ["b-risk"]},
                     {"item": "A certification", "class": "U"}],
        "notes": "A real programs role.",
        "for_owner": ["The posting asks for travel."],
        **over,
    }


async def submit_findings(body) -> dict:
    """Hand the analyst's findings in, as it does, and return the app's reply; then the pipeline's turn the settled
    research asks for runs, as the app's loop runs it (`schedule.pipeline_pass`), and the reply's `stage` is the one the
    role is in after it."""
    async with db.session_factory()() as session:
        reply = await analysis.submit(session, OWNER, body)
        await session.commit()
    await pipeline_turn()
    if isinstance(reply, dict) and "stage" in reply and body.get("role"):
        async with db.session_factory()() as session:
            role = await session.get(Opportunity, uuid.UUID(str(body["role"])))
            if role is not None:
                reply = {**reply, "stage": role.status}
    return reply


async def pipeline_turn() -> None:
    """The pipeline's turn that a settled research or a rank report asks for, run as the app's loop runs it."""
    from app.jobsearch.loops import schedule

    await schedule.pipeline_pass(OWNER)


@db.background_work
async def settle_housekeeping(owner: str = OWNER) -> dict[str, int]:
    """One whole pass of the housekeeping (`housekeeping.settle`), saved; returns what it did, by part. Like the old
    pass of the schedule, it stamps `housekeeping.last_pass` after the save."""
    from app.jobsearch.work import housekeeping

    async with db.session_factory()() as session:
        did = await housekeeping.settle(session, owner)
        await session.commit()
        housekeeping.last_pass = datetime.now(UTC)
    return did


async def put_in_line(role_id, **fields) -> None:
    """Set these fields on a role and put it in front of the analyst, as the analyst's line does (the step that
    fetches the posting's text runs as it starts)."""
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        for name, value in fields.items():
            setattr(role, name, value)
        await analysis.start(session, OWNER, role)
        await session.commit()


def made_up_words(tag: str, n: int) -> str:
    """Exactly `n` characters of made-up sentences, each different, so a cut anywhere shows."""
    out, i = [], 0
    while sum(len(s) + 1 for s in out) < n:
        i += 1
        out.append(f"{tag} sentence {i} says one more thing about the work.")
    return " ".join(out)[: n - 1] + "!"


async def written_packet(client) -> str:
    """A role with a written packet that has a cover letter, under the writing rules `WRITING` (no margin set)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        role = Opportunity(owner=OWNER, slug="globex", company="Globex", title="Programs Lead", fit=6, origin="scout",
                           match_key="globex|programs lead", found=owner_today(), status="second_impression",
                           url="https://jobs.example.test/1", analysis=RESEARCH)
        session.add(role)
        await session.commit()
        role_id = role.id
    asked = (await client.post(f"/api/v1/roles/{role_id}/packet", json={})).json()
    letter = {"letter": {"paragraphs": [{"text": t, "from": ["L001"]} for t in LETTER["paragraphs"]]}}
    await write_packet(Compose=compose_reply(**letter))
    return asked["id"]


# --- the app's address, the browser session's world ---

def api_url(path: str) -> str:
    """The address of an API path, as the tests call it."""
    return f"/api/v1/{path}"


#: The two sites the browser world reads in the owner's browser.
ONE, TWO = "listings.example.test", "hire.example.test"


def browser_card(n: int = 1, **fields) -> dict:
    """One listing as the session reads it off a results page."""
    return {"site": ONE, "url": f"https://www.{ONE}/jobs/view/{1000 + n}", "title": f"Programs Director {n}",
            "company": f"Initech {n}", "location": "Springdale", "snippet": f"Lead the programs team, number {n}.",
            **fields}


async def mission_of(session, owner: str, key: str) -> Mission:
    """The mission with this key, made first if this install has not (`ensure`)."""
    await missions.ensure(session, owner)
    return await missions.get(session, owner, key)


async def browser_world(*, resumed: bool = True, settings: dict | None = None, extra_sites: bool = True,
                        learned: bool = True) -> dict[str, uuid.UUID]:
    """A profile with two target roles (and an old search plan nobody reads now), the mission (resumed unless told not),
    two sites to read in the browser (each with a recipe a learn run kept, unless `learned` is false), and three that
    are not that: one the app reads, one on the list to avoid and one switched off. Returns the sites' ids by host (the
    three others by "app", "avoid" and "off")."""
    criteria = {**PROFILE["criteria"], "search_plan": ["no longer read"],
                "target_roles": [*PROFILE["criteria"]["target_roles"],
                                 {"id": "ops", "name": "Operations", "titles": ["Operations", "Program Operations"]}]}
    async with db.session_factory()() as session:
        await load_profile(session, DEFAULT_OWNER, criteria=criteria)
        mission = await mission_of(session, DEFAULT_OWNER, missions.BROWSER)
        if resumed:
            await missions.resume(session, mission, by="Tester")
        if settings:
            mission.settings = settings
        made = {
            ONE: Site(owner=DEFAULT_OWNER, url=f"https://www.{ONE}/jobs/", kind="job_board", label="Listings",
                      read_by="browser", created_at=WORLD_NOW),
        }
        if extra_sites:
            later = WORLD_NOW + timedelta(minutes=1)  # the order they were added in
            made[TWO] = Site(owner=DEFAULT_OWNER, url=f"https://{TWO}/search", kind="job_board", read_by="browser",
                             created_at=later)
            made["app"] = Site(owner=DEFAULT_OWNER, url="https://server-read.example.test/", kind="job_board",
                               created_at=later + timedelta(minutes=1))
            made["avoid"] = Site(owner=DEFAULT_OWNER, url="https://avoid.example.test/", kind="job_board",
                                 read_by="browser", list_name="avoid", created_at=later + timedelta(minutes=2))
            made["off"] = Site(owner=DEFAULT_OWNER, url="https://off.example.test/", kind="job_board",
                               read_by="browser", on=False, created_at=later + timedelta(minutes=3))
        session.add_all(made.values())
        await session.flush()
        if learned:
            await learn_sites(session, made[ONE], *([made[TWO]] if extra_sites else []))
        await session.commit()
        return {host: site.id for host, site in made.items()}


# --- the browser's queue (the browser work spec): sessions, items, hand-backs ---

#: A browser worker's session and a Pathfinder's, as the runner names them (`<kind>-<hex>`).
WORKER, WORKER_B, PATHFINDER = "browser_reader-aaaa1111", "browser_reader-bbbb2222", "pathfinder-cccc3333"


@contextmanager
def as_session(sid: str, claim: str | None = None):
    """Calls made inside are the browser door's calls of this session (`X-Session-Id`), started for `claim` if it names
    one (`X-Claim`)."""
    token = caller.set(Caller(sid, claim=claim))
    try:
        yield
    finally:
        caller.reset(token)


async def in_session(sid: str, work, *, commit: bool = True, claim: str | None = None):
    """Run `work(session)` as a call of this session; commits, and returns what it returned."""
    async with db.session_factory()() as session:
        with as_session(sid, claim):
            out = await work(session)
        if commit:
            await session.commit()
        return out


async def next_item(sid: str = WORKER, now: datetime | None = None, claim: str | None = None) -> dict:
    """The session asks the door for its next item (`claim`: the hand-out it was started for)."""
    return await in_session(sid, lambda s: bd.next_item(s, DEFAULT_OWNER, now=now), claim=claim)


async def hand_back(sid: str, item_id: str, result: str, now: datetime | None = None, **fields) -> dict:
    """The session hands its item back (refused: the BrowserError is raised and nothing is kept)."""
    return await in_session(sid, lambda s: bh.hand_back(s, DEFAULT_OWNER, item_id, result, now=now, **fields))


async def lease(kind: str, target: str, sid: str = WORKER, now: datetime | None = None) -> str:
    """Lease one exact item to a session, as `browser.next_item` would have (the queue made first). Returns its id."""
    now = now or datetime.now(UTC)

    async def take(session):
        await bq.refresh(session, DEFAULT_OWNER, now)
        row = await session.scalar(select(BrowserItem).where(
            BrowserItem.owner == DEFAULT_OWNER, BrowserItem.kind == kind, BrowserItem.target == target))
        assert row is not None, f"no {kind} item for {target}"
        entry = await bl.session_run(session, DEFAULT_OWNER, sid, now)
        row.state, row.leased_by, row.lease_until, row.run_id = bp.LEASED, sid, now + bp.LEASE, entry.id
        entry.started = {**(entry.started or {}), "track": bp.TRACK[kind], "instructed": True}
        return str(row.id)

    return await in_session(sid, take)


async def make_the_queue(now: datetime | None = None, owner: str = DEFAULT_OWNER) -> int:
    """The browser queue as the work queue's `browser.reconcile` makes it: forced, in a transaction of its own. Only
    that to-do, the start and the owner's Run now make it; the housekeeping pass, the schedule's look, the page's
    counts and the hand-outs only read it. Returns how many items it changed."""
    async with db.session_factory()() as session:
        changed = await bq.refresh(session, owner, now)
        await session.commit()
        return changed


async def settle_after_the_queue(session, owner: str = DEFAULT_OWNER) -> dict[str, int]:
    """The queue made as `make_the_queue` makes it, then the whole housekeeping pass, which no longer makes it."""
    from app.jobsearch.work import housekeeping

    await bq.refresh(session, owner)
    return await housekeeping.settle(session, owner)


async def scan_hand_in(postings, site: str = ONE, sid: str = WORKER, now: datetime | None = None, **kw) -> dict:
    """A worker scanning `site` hands these postings in (the scan item leased to it first, unless it holds it)."""
    async def held(session):
        return await session.scalar(select(BrowserItem).where(
            BrowserItem.leased_by == sid, BrowserItem.state == bp.LEASED))

    if (await in_session(sid, held, commit=False)) is None:
        await lease(bp.SCAN, site, sid, now)
    return await in_session(sid, lambda s: bd.postings(s, DEFAULT_OWNER, postings, now=now, **kw))


async def items(**where) -> list[BrowserItem]:
    """The queue's items (of a kind, a state ...), oldest first."""
    async with db.session_factory()() as session:
        query = select(BrowserItem).where(BrowserItem.owner == DEFAULT_OWNER).order_by(BrowserItem.created_at)
        for name, value in where.items():
            query = query.where(getattr(BrowserItem, name) == value)
        return list((await session.scalars(query)).all())


# --- the readers, the screen, a whole round ---

async def filtered_postings(session, readers_n=3, titles_n=0, owner=OWNER):
    """Postings through the filter: some headed for a reader, some for the title pass."""
    employer = await employers.find_or_make(session, owner, "Fabrikam")
    rows = []
    for kind, route, n in (("Reader", judged.READER, readers_n), ("Title", "titles", titles_n)):
        for i in range(1, n + 1):
            title = f"{kind} Role {i}"
            rows.append(JobPosting(
                owner=owner, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=f"{kind}-{i}", company=employer.name,
                title=title, match_key=match_key(employer.name, title), location="Remote", work_mode="remote",
                url=f"https://jobs.example.test/{kind}-{i}", text=f"The whole posting for {title}.", route=route,
                judged_by="titles" if route == judged.READER else None,  # a reader's posting a tab said yes to
                first_seen=READ_NOW + timedelta(seconds=len(rows)), last_seen=READ_NOW, filtered_at=READ_NOW, links=[]))
    session.add_all(rows)
    await session.flush()


async def report_to_run(run_id, roles, done=False):
    """The scout reports `roles` into a run (`done`: its last batch); returns the app's reply."""
    async with db.session_factory()() as session:
        reply = await run.stage_report(session, await session.get(SearchRun, run_id), roles, done)
        await session.commit()
        return reply


TODAY = owner_today()


async def screen(gates: g.Gates, **role):
    """Run one role through the process; returns (verdict, the history rows' decisions)."""
    raw = {"company": "Northwind", "title": "Director", "fit": 5, "location": "Remote", "work_mode": "remote",
           **role}
    facts = g.role_facts(raw, gates, TODAY)
    async with db.session_factory()() as session:
        instance = await process.engine().start(
            session, process.SCREEN, {"gates": gates.for_process(), "role": facts}, subject="test:screen")
        await session.commit()
        assert instance.status == COMPLETED, instance.error
        rows = (await session.scalars(
            select(ProcHistory).where(ProcHistory.instance_id == instance.id, ProcHistory.kind == "decision")
            .order_by(ProcHistory.seq))).all()
        return instance.data["verdict"], [r.flow_id for r in rows]


async def export_workbook(session, owner: str = OWNER) -> bytes:
    """The owner's roles as an .xlsx, made the way the export route makes it: the rows are read (`tracker.export_rows`),
    then the file is built (`tracker.build_workbook`)."""
    tracker_rows, screened_rows = await tracker.export_rows(session, owner)
    return await tracker.build_workbook(tracker_rows, screened_rows)


def pair_signals(first, second) -> dict[str, str]:
    """What two employer records have in common, by kind, as the night's pass over every record works it out
    (`employer_pairs.candidates`), with no word rare: the clauses `employer_match.decide` reads."""
    return employer_pairs._signals(employer_pairs._facts(first), employer_pairs._facts(second))


async def run_round(roles, *, known=(), done_in_batches=1, **parts):
    """Load the profile and the tracker's roles, start a search, report `roles`, and return the run id."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, **parts)
        if known:
            await tracker.import_tracker(session, OWNER, tracker_workbook([tracker_row(c, t) for c, t in known]))
        started = await run.start_round(session, OWNER, started_by="manual", focus="test focus")
        await session.commit()
        run_id = started.id
    size = max(1, -(-len(roles) // done_in_batches)) if roles else 1
    batches = [roles[i:i + size] for i in range(0, len(roles), size)] or [[]]
    for index, batch in enumerate(batches):
        async with db.session_factory()() as session:
            row = await session.get(SearchRun, run_id)
            await run.stage_report(session, row, batch, done=index == len(batches) - 1)
            await session.commit()
    return run_id


async def stored_by_title(run_id):
    """The roles a run stored, by title."""
    async with db.session_factory()() as session:
        rows = (await session.scalars(
            select(Opportunity).where(Opportunity.source_run == run_id).order_by(Opportunity.title))).all()
        return {r.title: r for r in rows}


async def score_like_research() -> dict[str, Opportunity]:
    """Work every stored role's score out, as the analyst's research does (docs/PRD.md 39.20): from the answers kept on
    it, its employer's record and the hour the app first saw its posting, under the
    Prioritization in force. A reader's report is stored with its answers and no score; this is the score that comes
    after. Returns the roles by title."""
    async with db.session_factory()() as session:
        profile = await profile_store.current(session, OWNER)
        version = await profile_store.newest_version(session, OWNER, "scoring")
        roles = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all()
        seen = await freshness.first_seen_by_role(session, OWNER)
        for role in roles:
            employer = await session.get(Employer, role.employer_id) if role.employer_id else None
            prioritize.work_out(role, profile, employer=employer, version=version, seen=seen.get(role.id))
        await session.commit()
        return {r.title: r for r in roles}


# --- the writing rules, in the sections and as they were saved before them ---

#: A made-up owner's writing rules: one rule in each section, and the QA parts.
WRITING_FULL = {
    "voice_rules": [{"id": "plain", "says": "Short sentences, plain words."}],
    "ats_rules": [{"id": "keywords", "says": "Use the posting's own words for skills."}],
    "resume_global": [{"id": "one-page", "says": "One page unless the posting asks for more."}],
    "resume_header": [{"id": "city-only", "says": "City and state only."}],
    "resume_summary": [{"id": "three-lines", "says": "Three or four lines, written for the posting."}],
    "resume_competencies": [{"id": "six-areas", "says": "No more than six areas."}],
    "resume_achievements": [{"id": "numbers", "says": "Each one carries a number."}],
    "resume_experience": [{"id": "verbs", "says": "Start each bullet with a verb."}],
    "resume_education": [{"id": "degrees-only", "says": "Degrees only."}],
    "role_rules": {"builders": [{"id": "lead-with-teams", "says": "Lead with team size."}],
                   "gone": [{"id": "kept", "says": "From a role since deleted."}]},
    "letter_global": [{"id": "three-paragraphs", "says": "Three paragraphs at most."}],
    "letter_header": [{"id": "no-address", "says": "No street address."}],
    "letter_greeting": [{"id": "by-name", "says": "Greet a named person."}],
    "letter_opening": [{"id": "problem-first", "says": "Open with the employer's problem."}],
    "letter_body": [{"id": "one-proof", "says": "One proof, with a number."}],
    "letter_closing": [{"id": "ask", "says": "End on a specific ask."}],
    "letter_signoff": [{"id": "plain-close", "says": "A plain close."}],
    "letter_role_rules": {"builders": [{"id": "name-the-build", "says": "Name what was built."}],
                          "gone": [{"id": "kept-letter", "says": "From a letter role since deleted."}]},
    "banned": [{"id": "puff", "phrase": "spearheaded", "why": "puffery"}],
    "legacy_patterns": [{"id": "years", "pattern": r"\b\d+\+?\s+years\b", "regex": True, "why": "dates the owner"}],
    "review_steps": [{"id": "sources", "says": "Read the sources yourself."}],
    "drift_patterns": [{"id": "scope", "name": "Scope creep", "looks_like": "led AI for the firm",
                        "fix": "Use the master's noun"}],
    "review_flags": [{"id": "gates", "says": "Hard gates in the posting."}],
    "humanizing": [{"id": "no-triples", "says": "No lists of three adjectives."}],
    "notes": [{"id": "kept", "text": "A note kept from before.", "source": "an old file"}],
}
#: The same owner's rules as they were saved before the sections: a voice paragraph and the content rules.
WRITING_OLDER = {
    "voice": "Plain, short and optimistic.\nNever breathless.",
    "rules": [{"id": "clients", "says": "Clients are never named."},
              {"id": "dates", "says": "Years go on the roles only."}],
    #: "Never write" as migration 0048 leaves it (plain phrases, and the regular expressions kept apart).
    "banned": [{"id": "puff", "phrase": "spearheaded", "why": "puffery"}],
    "legacy_patterns": [{"id": "years", "pattern": r"\b\d+\+?\s+years\b", "regex": True, "why": "dates the owner"}],
    "resume_format": WRITING_FULL["resume_global"], "letter_rules": WRITING_FULL["letter_global"],
    "review_steps": WRITING_FULL["review_steps"],
    "drift_patterns": WRITING_FULL["drift_patterns"], "review_flags": WRITING_FULL["review_flags"],
    "notes": WRITING_FULL["notes"],
}


# --- target roles' own texts (criteria documents) ---

QUALIFICATIONS = "A degree or the same in experience; no licence required."
PREFERRED = "Grant writing; a second language."
COMPENSATION = "A salary over the floor, health cover and four weeks off."
SHARED = {"qualifications": QUALIFICATIONS, "preferred": PREFERRED, "compensation": COMPENSATION}


def tab(ident, **texts):
    return {"id": ident, "name": ident.title(), "titles": [f"A {ident} title"], "description": f"The {ident} work.",
            "responsibilities": f"What a {ident} does each day.", **texts}


def criteria(*tabs, **doc) -> dict:
    return {"company": "A public agency.", "target_roles": list(tabs), **doc}


def parts(role) -> tuple:
    return role.qualifications, role.preferred, role.compensation


# --- a parse job (the posting a session splits into a role's fields) ---

POSTING_TEXT = "Operations Manager. " + "We need someone to run the programs and keep the books in order. " * 3
MINUTE = timedelta(minutes=1)
POSTING_FIELDS = {
    "name": "Operations manager",
    "titles": ["Operations Manager", "Manager of Operations"],
    "description": "Runs the programs and keeps the books in order.",
    "responsibilities": "Runs the programs. Keeps the books.",
    "qualifications": "Five years in operations.",
    "preferred": "",
    "compensation": "A salary and four weeks off.",
}


async def parse_runner(*, seen=None, checked_in=None, declared=None, kind=None, paused=False, now=None):
    """The runner's connection as the roster keeps it: each stamp that is given is that long before `now`."""
    now = now or datetime.now(UTC)
    async with db.session_factory()() as session:
        conn = await roster.ensure_roster(session, OWNER)
        conn.last_seen_at = None if seen is None else now - seen
        conn.checked_in_at = None if checked_in is None else now - checked_in
        conn.declared_at = None if declared is None else now - declared
        conn.session_kind, conn.paused = kind, paused
        await session.commit()


async def parse_job(state=role_parse.WAITING, *, age=timedelta(0), taken_ago=None, **more) -> str:
    """A parse job made `age` ago (and taken `taken_ago` ago), straight in the table."""
    now = datetime.now(UTC)
    job = ParseJob(owner=OWNER, text=POSTING_TEXT, state=state, created_at=now - age,
                   taken_at=None if taken_ago is None else now - taken_ago, **more)
    async with db.session_factory()() as session:
        session.add(job)
        await session.commit()
    return str(job.id)


async def parse_row(job_id) -> ParseJob | None:
    async with db.session_factory()() as session:
        return await session.get(ParseJob, uuid.UUID(job_id))


async def parse_get(client, job_id):
    return await client.get(f"/api/v1/profile/parse/{job_id}")


async def parse_sweep() -> None:
    """The sweep mission runs once, as it does every 15 minutes: the jobs that took too long are failed and saved."""
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        sweep = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.SWEEP))
        await missions.start(session, OWNER, sweep, "manual")
        await session.commit()


async def added_link_role(link: str) -> str:
    """A role in the shape the owner's added links took before they became postings (docs/PRD.md 24.10, until
    2026-10-15): in Found, origin `owner`, named by its link and "Added link", its link its posting link, sourced and
    put in the analyst's line when it is one posting that is not a job board's. Roles made so are still in the
    database, and the analyst names them (`analysis.name_added`). Returns the role's id."""
    from app.jobsearch import ats
    from app.jobsearch.browser import add_link
    from app.jobsearch.employers import follow as employer_posting
    from app.jobsearch.models import ADDED_LINK, ORIGIN_OWNER
    from app.jobsearch.postings import role_postings
    from app.jobsearch.postings import sourced as sourcing
    from tests.support import OWNER

    async with db.session_factory()() as session:
        key, address = add_link.read_link(link)
        boards = await employer_posting.job_board_hosts(session, OWNER)
        title = address.removeprefix("https://")
        values = {
            "owner": OWNER, "slug": tracker.slugify(ADDED_LINK, key), "company": ADDED_LINK, "title": title,
            "status": stages.SECOND_IMPRESSION, "found": owner_today(), "url": address, "origin": ORIGIN_OWNER,
            "board_url": address if is_board_link(address, boards) else None,
            "match_key": match_key(ADDED_LINK, title), "sourced_at": None,
        }
        role = Opportunity(id=uuid.uuid4(), **values)
        session.add(role)
        await session.flush()
        session.add(role_postings.posting_of({**values, "id": role.id, "description": None}, ats.OWNER,
                                             datetime.now(UTC)))
        await session.flush()
        if await sourcing.source_given(session, role, address, by="owner"):
            await analysis.ask(session, OWNER, role.id)
        await session.commit()
        return str(role.id)


#: A gate zero as the Haiku ranker sends it: fit 0, and the first line of why starts with "Gate:" (`rank_roles`).
GATE_ZERO = (0, "Gate: pay is under the floor.\nA programs role at a bank.")


async def rank_roles(session, ranks: dict, owner: str = OWNER) -> None:
    """Give each role a first-impression rank (a `js_role_ranks` row, 0 to 100) with the raw and cleansed copy the row
    needs: `ranks` maps a role id to its fit, or to `(fit, why)` for a rank with Haiku's lines of why (`GATE_ZERO` is
    a gate zero's). What the board and the pipeline order the roles ahead by."""
    now = datetime.now(UTC)
    for role_id, rank in ranks.items():
        fit, why = rank if isinstance(rank, tuple) else (rank, "")
        raw = RawPosting(owner=owner, source_id=uuid.uuid4(), reader="legacy", kind="legacy", ext_id=str(role_id),
                         fetched_at=now, body=b"x", body_sha256=uuid.uuid4().hex + uuid.uuid4().hex, body_bytes=1,
                         no_raw=True)
        session.add(raw)
        await session.flush()
        copy = CleansedPosting(owner=owner, raw_id=raw.id, cleaner_version=1)
        session.add(copy)
        await session.flush()
        session.add(RoleRank(owner=owner, role_id=role_id, cleansed_id=copy.id, profile_version=1, fit=fit,
                             why=why))
    await session.flush()


def counted(**analysis_more) -> dict:
    """The fields of a role whose research is a Second Impression (a read the analyst's findings made, stamped
    `analysis.DECK_READ`) that graded one requirement of its posting (a role with no graded item is not read enough
    for a place in Ready to Launch: `analysis.graded_read`), made under no recorded inputs so nothing makes it out of
    date: `Opportunity(**counted())`. `counted(required=[])` is a read that graded nothing."""
    return {"analysis_state": analysis.DONE, "analysis": {
        analysis.DECK_READ: True, "required": [{"item": "Programs", "class": "P"}], **analysis_more}}
