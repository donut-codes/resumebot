"""Where a role stands before it competes (`lifecycle.py`): one function of its facts, applied the same way on every
turn of the pipeline. The table below is exhaustive over the facts; the database tests are the roles that sat in Landing
Zone for ever. Made-up data."""


import asyncio
import itertools
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select

from app import db
from app.jobsearch import as_read, stages
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import lifecycle
from app.jobsearch.pipeline.lifecycle import Facts, stage_of
from app.jobsearch.postings import cleansed
from app.jobsearch.research import analysis
from app.models.work import TrailEvent
from app.timeutil import owner_today
from app.trail import service as trail
from tests.builders import counted, rank_roles
from tests.support import OWNER, PROFILE, load_profile

WHOLE, THIN = cleansed.MIN_TEXT, cleansed.MIN_TEXT - 1


#: Where something can hold a role (`placement` never holds one at Followup needed or Landing Zone, which code moves a
#: role through at once: test_placement).
HOLDABLE = (stages.CLEANSED_DATA, stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)


EVERY = [Facts(status=s, copy=c, raw=raw, no_text=nt, ranked=rk, read=rd, graded=gr, held=held)
         for s, c, raw, nt, rk, rd, gr, held in itertools.product(
             lifecycle.LOOKED_AT, (None, THIN, WHOLE), (False, True), (False, True), (False, True), (False, True),
             (False, True), (False, True)) if (s in HOLDABLE or not held) and (rd or not gr)]


def test_every_combination_of_facts_gets_one_stage_and_the_stage_is_stable():
    for f in EVERY:
        now = stage_of(f)
        assert now in lifecycle.LOOKED_AT, f
        assert stage_of(replace(f, status=now)) == now, f"not stable: {f} -> {now}"


def test_a_role_with_no_next_step_is_never_left_in_a_stage():
    for f in EVERY:
        now = stage_of(f)
        if f.held:
            assert now == f.status
            continue
        if now == stages.LANDING_ZONE:  # only for the moment between a raw copy and its cleansed copy
            assert f.raw and f.copy is None and not f.no_text and not f.ranked and not f.read, f
        if now == stages.CLEANSED_DATA:  # waiting for the rank: a whole copy and no rank
            assert f.copy is not None and f.copy >= WHOLE and not f.ranked and not f.read and not f.no_text, f
        if now == stages.FOLLOWUP_NEEDED:  # waiting for a read: no usable text, and not ranked or read
            assert not f.ranked and not f.read, f
            assert f.no_text or f.copy is None or f.copy < WHOLE, f
        if now == stages.FIRST_IMPRESSION:
            assert f.ranked or f.read, f
        if now in (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH):
            assert f.status == now and f.read, f"a place at the top needs a read or a hold: {f}"
        if now == stages.READY_TO_LAUNCH:
            assert f.graded, f"a place in Ready to Launch needs a read that graded the posting or a hold: {f}"


def test_the_facts_that_matter_decide_the_stage():
    g = stage_of
    assert g(Facts(stages.LANDING_ZONE)) == stages.FOLLOWUP_NEEDED, "no text at all: a read, not Landing Zone"
    assert g(Facts(stages.LANDING_ZONE, raw=True)) == stages.LANDING_ZONE, "a raw copy waiting to be cleansed"
    assert g(Facts(stages.LANDING_ZONE, copy=WHOLE)) == stages.CLEANSED_DATA
    assert g(Facts(stages.CLEANSED_DATA, copy=THIN)) == stages.FOLLOWUP_NEEDED, "a title and a header is no text"
    assert g(Facts(stages.FOLLOWUP_NEEDED, copy=WHOLE)) == stages.CLEANSED_DATA, "its read brought the text"
    assert g(Facts(stages.CLEANSED_DATA, copy=WHOLE, no_text=True)) == stages.FOLLOWUP_NEEDED
    assert g(Facts(stages.CLEANSED_DATA, copy=WHOLE, ranked=True)) == stages.FIRST_IMPRESSION
    assert g(Facts(stages.READY_TO_LAUNCH, copy=WHOLE, ranked=True)) == stages.FIRST_IMPRESSION, "unread: not Ready"
    assert g(Facts(stages.READY_TO_LAUNCH, copy=WHOLE, ranked=True, held=True)) == stages.READY_TO_LAUNCH, "held"
    assert g(Facts(stages.READY_TO_LAUNCH, ranked=True, held=True)) == stages.READY_TO_LAUNCH, "an import's own place"
    assert g(Facts(stages.READY_TO_LAUNCH, copy=WHOLE, read=True)) == stages.READY_TO_LAUNCH
    ungraded = Facts(stages.READY_TO_LAUNCH, copy=WHOLE, ranked=True, read=True, graded=False)
    assert g(ungraded) == stages.FIRST_IMPRESSION, "a read that graded nothing is not enough for Ready to Launch"
    assert g(replace(ungraded, held=True)) == stages.READY_TO_LAUNCH, "a hold that stands until the read is done"
    assert g(replace(ungraded, status=stages.SECOND_IMPRESSION)) == stages.SECOND_IMPRESSION, "the deck needs none"
    assert g(Facts(stages.SECOND_IMPRESSION, copy=WHOLE, read=True)) == stages.SECOND_IMPRESSION
    assert g(Facts(stages.FIRST_IMPRESSION, copy=WHOLE, read=True)) == stages.FIRST_IMPRESSION
    assert g(Facts(stages.FIRST_IMPRESSION, copy=WHOLE, ranked=True, held=True)) == stages.FIRST_IMPRESSION
    assert g(Facts(stages.APPLIED, ranked=True)) == stages.APPLIED, "not this function's stage"


def _role(slug: str, status: str = stages.LANDING_ZONE, **more) -> Opportunity:
    return Opportunity(**{"owner": OWNER, "slug": slug, "company": f"Firm {slug}", "title": f"Lead {slug}",
                          "origin": "scout", "match_key": f"firm {slug}|lead {slug}", "status": status,
                          "found": owner_today(), **more})


def _posting(role: Opportunity, source: uuid.UUID, text: str | None) -> JobPosting:
    now = datetime.now(UTC)
    return JobPosting(owner=OWNER, employer_id=source, ats="scout", ext_id=str(role.id), company=role.company,
                      title=role.title, match_key=role.match_key, first_seen=now, last_seen=now, links=[],
                      opportunity_id=role.id, text=text)


async def test_landing_zone_roles_are_cleansed_from_stored_text_or_sent_to_a_read(js):
    words = "A whole posting with real duties, asks and a place. " * 12
    source = uuid.uuid4()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        text = _role("text", description=words)  # text on the role and the posting, no raw copy (legacy)
        on_role = _role("onrole", description=words)  # text only on the role
        none = _role("none")  # no text at all
        thin = _role("thin", description="Lead role.")
        raw = _role("raw")  # a raw copy of the posting and no cleansed copy
        session.add_all([text, on_role, none, thin, raw])
        await session.flush()
        session.add_all([_posting(text, source, words), _posting(on_role, source, None), _posting(none, source, None),
                         _posting(thin, source, "Lead role."), _posting(raw, source, None)])
        session.add(RawPosting(
            owner=OWNER, source_id=source, ext_id=str(raw.id), reader="scout", kind="item",
            fetched_at=datetime.now(UTC), body=b"x", body_sha256=uuid.uuid4().hex + uuid.uuid4().hex, body_bytes=1,
            parsed={"title": raw.title, "company": raw.company, "text": words}))
        await session.commit()

    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 5
        await session.commit()
        by_slug = {r.slug: r.status for r in await session.scalars(select(Opportunity))}
        assert by_slug == {"text": stages.CLEANSED_DATA, "onrole": stages.CLEANSED_DATA, "raw": stages.CLEANSED_DATA,
                           "none": stages.FOLLOWUP_NEEDED, "thin": stages.FOLLOWUP_NEEDED}
        assert not [r for r in await session.scalars(select(Opportunity)) if r.status == stages.LANDING_ZONE]
        copies = list(await session.scalars(select(CleansedPosting)))
        assert len(copies) == 4 and sorted(c.no_raw for c in copies) == [False, True, True, True]
        assert await pipeline.advance(session, OWNER) == 0, "settled"
        assert await lifecycle.settle(session, OWNER, set()) == 0


async def test_a_role_whose_posting_got_its_whole_text_after_a_snippet_copy_takes_it_and_moves_on(js):
    """Found in use: browser postings held thousands of characters while their roles sat in Followup needed, because the
    role's copy had been made from the listing's snippet and nothing made a new one when the page was read."""
    words = "A whole posting with real duties, asks and a place. " * 12
    source = uuid.uuid4()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        read, unopened = _role("read", stages.FOLLOWUP_NEEDED), _role("unopened", stages.FOLLOWUP_NEEDED)
        session.add_all([read, unopened])
        await session.flush()
        for role in (read, unopened):
            session.add(replace_ats(_posting(role, source, "Lead role."), "browser"))
        await session.commit()
        assert await pipeline.advance(session, OWNER) == 0, "a snippet is no text"
        await session.commit()
        for posting in await session.scalars(select(JobPosting)):
            posting.text = words
            posting.detail_at = datetime.now(UTC) if posting.opportunity_id == read.id else None
        await session.commit()
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 1
        await session.commit()
        by_slug = {r.slug: r.status for r in await session.scalars(select(Opportunity))}
        assert by_slug == {"read": stages.CLEANSED_DATA, "unopened": stages.FOLLOWUP_NEEDED}, (
            "the page that was opened gave the text; a listing never opened is a snippet however long")
        assert await pipeline.advance(session, OWNER) == 0, "settled"


def replace_ats(posting: JobPosting, system: str) -> JobPosting:
    posting.ats = system
    return posting


async def test_a_role_that_holds_a_whole_description_while_its_only_copy_is_a_snippet_takes_it_and_moves_on(js):
    """Roles in Followup needed held a whole description
    and their only copy was the listing's snippet, because `keep_whole_text` read the posting's text and
    never the role's description. A posting never opened (a browser listing) stays a snippet however it is read; a
    description under `MIN_TEXT` is no text."""
    words = "A whole posting with real duties, asks and a place. " * 12
    source = uuid.uuid4()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        described, short = _role("described", stages.FOLLOWUP_NEEDED), _role("short", stages.FOLLOWUP_NEEDED)
        session.add_all([described, short])
        await session.flush()
        for role in (described, short):
            session.add(replace_ats(_posting(role, source, "Lead role."), "browser"))
        await session.commit()
        assert await pipeline.advance(session, OWNER) == 0, "a snippet is no text"
        await session.commit()
        for role in await session.scalars(select(Opportunity)):
            role.description = words if role.slug == "described" else "Lead role at Firm, in Town."
        await session.commit()
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 1
        await session.commit()
        assert await _statuses() == {"described": stages.CLEANSED_DATA, "short": stages.FOLLOWUP_NEEDED}
        role_id = await session.scalar(select(Opportunity.id).where(Opportunity.slug == "described"))
        current = (await cleansed.current_for_roles(session, OWNER))[role_id]
        copy = (await cleansed.copies(session, OWNER, [current]))[current]
        assert copy.text_clean.strip() == words.strip(), "word for word, none cut"
        assert await pipeline.advance(session, OWNER) == 0, "settled"


WORDS = "A whole posting with real duties, asks and a place. " * 12


async def _legacy_roles(*slugs: str, status: str = stages.FOLLOWUP_NEEDED, text: str | None = WORDS) -> None:
    """Roles at `status` that hold their posting's text and no raw or cleansed copy yet (`advance` makes them)."""
    source = uuid.uuid4()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        roles = [_role(slug, status, description=text) for slug in slugs]
        session.add_all(roles)
        await session.flush()
        session.add_all([_posting(role, source, text) for role in roles])
        await session.commit()


async def _statuses() -> dict[str, str]:
    async with db.session_factory()() as session:
        return {r.slug: r.status for r in await session.scalars(
            select(Opportunity).execution_options(populate_existing=True))}


async def test_the_stage_of_a_role_is_decided_with_no_transaction_open(js, monkeypatch):
    await _legacy_roles("a", "b")
    seen: list[bool] = []
    real = lifecycle.stage_of

    def watched(facts):
        seen.append(db.in_transaction())
        return real(facts)

    monkeypatch.setattr(lifecycle, "stage_of", watched)
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 2
        await session.commit()
    assert len(seen) == 2 and not any(seen), "each role's stage is worked out from what was read, not inside the read"


async def test_a_role_moved_meanwhile_is_not_moved_back_by_the_stage_rule(js, monkeypatch):
    await _legacy_roles("a", "b")
    real, moved = as_read.lock, []

    async def the_owner_ends_a(session, model, rows, **more):
        # the stages are decided and no transaction is open: the owner moves a role before the moves are locked
        if model is Opportunity and not moved:
            moved.append(1)
            async with db.session_factory()() as other:
                (await other.scalar(select(Opportunity).where(Opportunity.slug == "a"))).status = stages.REJECTED
                await other.commit()
        return await real(session, model, rows, **more)

    monkeypatch.setattr(as_read, "lock", the_owner_ends_a)
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 1
        await session.commit()
    assert await _statuses() == {"a": stages.REJECTED, "b": stages.CLEANSED_DATA}, (
        "the owner's move stands: the rule moved only the role still as it read it")


async def test_the_copies_a_pass_makes_are_committed_in_batches_and_a_settled_pass_writes_nothing(js, monkeypatch,
                                                                                                  transactions):
    await _legacy_roles("a", "b", "c", "d", "e")
    monkeypatch.setattr(lifecycle, "WRITE_BATCH", 2)
    transactions.clear()
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 5
        await session.commit()
    assert sum(transactions.writes(t) for t in transactions.ended) >= 4, (
        "three batches of copies (2, 2 and 1 role) and the moves, each its own transaction")
    transactions.clear()
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 0
        await session.commit()
    assert transactions.ended and not any(transactions.writes(t) for t in transactions.ended), "nothing to do"


async def test_the_current_copy_is_read_once_in_a_steady_turn(js, transactions):
    """A role with no text at all lacks a copy every turn, and a role in Followup needed whose copy is a snippet is
    asked about every turn (`keep_whole_text`): neither makes anything, so the copies are not read a second time."""
    await _legacy_roles("whole")
    await _legacy_roles("thin", text="Lead role.")
    await _legacy_roles("none", text=None)
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 1
        await session.commit()
    assert await _statuses() == {"whole": stages.CLEANSED_DATA, "thin": stages.FOLLOWUP_NEEDED,
                                 "none": stages.FOLLOWUP_NEEDED}
    transactions.clear()
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 0
        await session.commit()
    reads = [s for s in transactions.timeline if " AS sized" in s]
    assert len(reads) == 1, "the current copy of every role is one statement a turn"


async def test_a_ready_role_with_no_read_that_graded_its_posting_leaves_for_the_stage_its_facts_give_and_says_why(js):
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 3})
        roles = {slug: _role(slug, stages.READY_TO_LAUNCH, **more) for slug, more in {
            "opened": {"go_opened_at": now}, "placed": {}, "star": {"favorited_at": now},
            "nothing-graded": {"go_opened_at": now, **counted(required=[])},
            "no-text": {"go_opened_at": now, "analysis_state": analysis.NO_TEXT},
            "graded": {"go_opened_at": now, **counted()}}.items()}
        session.add_all(roles.values())
        await session.flush()
        await rank_roles(session, {r.id: 50 for slug, r in roles.items() if slug != "no-text"})
        await trail.record(session, OWNER, event="role_moved", actor="person", who="Owner", sentence="You moved it.",
                           link=("opportunity", roles["placed"].id))
        await session.commit()
        ids = {slug: r.id for slug, r in roles.items()}
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 5
        await session.commit()
    assert await _statuses() == {
        "opened": stages.FIRST_IMPRESSION, "placed": stages.FIRST_IMPRESSION, "star": stages.FIRST_IMPRESSION,
        "nothing-graded": stages.FIRST_IMPRESSION, "no-text": stages.FOLLOWUP_NEEDED, "graded": stages.READY_TO_LAUNCH}
    async with db.session_factory()() as session:
        lines = {slug: list(await session.scalars(select(TrailEvent.sentence).where(
            TrailEvent.link_id == role_id, TrailEvent.event == "left_ready_unread"))) for slug, role_id in ids.items()}
    assert lines["graded"] == []
    assert lines["opened"] == ["Lead opened at Firm opened left Ready to Launch for First Impression: it has no Second "
                               "Impression."]
    assert lines["nothing-graded"] == ["Lead nothing-graded at Firm nothing-graded left Ready to Launch for First "
                                       "Impression: its Second Impression graded nothing of the posting."]
    assert lines["no-text"] == ["Lead no-text at Firm no-text left Ready to Launch for Followup needed: the posting's "
                                "text is missing."]


async def test_the_roles_and_copies_are_read_without_the_writers_turn_and_the_moves_are_those_of_a_pass_inside_one(
        js, monkeypatch):
    """On a large database `settle` held the file's one writer turn for seconds at every round of a turn,
    from its first read to the end of the first batch of copies. Where the session holds nothing, the roles, their
    holds and their copies are read in a reader's session (a second writer gets through meanwhile) and the turn is
    taken for a batch of copies and for the moves. In a session that already has a write in it, it reads in that
    transaction as it always did, and moves exactly the same roles."""
    from sqlalchemy import update

    from app.jobsearch.pipeline import placement

    await _legacy_roles("a", "b", "c")
    seen: list[tuple[bool, bool]] = []  # (read in a reader's session, a second writer got through)

    def spied(owner_module, name):
        real = getattr(owner_module, name)

        async def wrapper(session, *args, **kwargs):
            got = False
            if db.is_reader(session):
                async with db.session_factory()() as other:
                    try:
                        await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.slug == "c")
                                                             .values(slug="c")), 1)
                        got = True
                    except TimeoutError:  # queued behind the settle: it still holds the writer's turn
                        pass
                    await other.rollback()
            seen.append((db.is_reader(session), got))
            return await real(session, *args, **kwargs)

        monkeypatch.setattr(owner_module, name, wrapper)

    spied(placement, "load")
    spied(cleansed, "current_for_roles")
    spied(cleansed, "short_ids")
    async with db.session_factory()() as session:
        assert await pipeline.advance(session, OWNER) == 3
        await session.commit()
    assert len(seen) >= 4 and all(read and got for read, got in seen), (
        "every read was made apart, and a second writer got through each")
    apart = await _statuses()
    assert apart == {"a": stages.CLEANSED_DATA, "b": stages.CLEANSED_DATA, "c": stages.CLEANSED_DATA}

    seen.clear()
    async with db.session_factory()() as session:  # put back, and settled in a session that holds a write already
        await session.execute(update(Opportunity).values(status=stages.FOLLOWUP_NEEDED))
        assert await pipeline.advance(session, OWNER) == 3
        await session.commit()
    assert seen and not any(read or got for read, got in seen), "a transaction with a write in it is read in itself"
    assert await _statuses() == apart
