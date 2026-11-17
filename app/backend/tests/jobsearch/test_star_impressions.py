"""A star runs the impressions first (the owner, 2026-10-28): a starred role may jump to Ready,
but it runs First and Second Impression first. A starred role is put at the front of whatever it still
lacks, its rank (First Impression) and then its research (Second Impression), and moves to Ready to Launch only once
both are done; a role that is read already goes up at once; a hard pass that stops it sends it to Gated out.
Everything is made up."""

import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.browser import takeable
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.employers.core import find_or_make
from app.jobsearch.missions import core as missions
from app.jobsearch.models import BrowserItem, CleansedPosting, JobPosting, Opportunity, Packet, ParseJob, RawPosting
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.postings import raw_copies
from app.jobsearch.research import analysis, line, rank
from app.models.work import TrailEvent
from app.settings import settings
from tests.builders import (
    MINUTE,
    counted,
    findings,
    mission_of,
    parse_runner,
    pipeline_turn,
    rank_roles,
    save_documents,
    submit_findings,
)
from tests.support import EXAMPLE_SYSTEM, FIVE, OWNER, TOKEN, load_profile

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
TEXT = "\n".join(f"Paragraph {i} of the posting says one thing about the work and nothing else." for i in range(1, 12))


@pytest.fixture
async def world(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    await parse_runner(seen=MINUTE)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        await session.commit()


@pytest.fixture
def door():
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


async def _role(
    slug: str, status: str = stages.CLEANSED_DATA, *, text: str = TEXT, pay: tuple[int, int] | None = None, **more
) -> uuid.UUID:
    """A role that is Sourced, with a posting that holds `text` entered as a cleansed copy, and no rank."""
    async with db.session_factory()() as session:
        role = Opportunity(
            owner=OWNER,
            slug=slug,
            company=f"Firm {slug}",
            title=f"Programs Lead {slug}",
            fit=3,
            origin="scout",
            match_key=f"firm {slug}|programs lead {slug}",
            status=status,
            found=date.today(),
            posted=date.today(),
            location="Remote, US",
            move_type="remote",
            description=text,
            **{
                "source_url": f"https://jobs.example.test/{slug}",
                "source_kind": "employer",
                "sourced_at": datetime.now(UTC),
                **more,
            },
        )
        session.add(role)
        await session.flush()
        employer = await find_or_make(session, OWNER, role.company)
        raw = RawPosting(
            owner=OWNER,
            source_id=employer.id,
            reader="legacy",
            kind="legacy",
            ext_id=slug,
            fetched_at=NOW,
            body=b"x",
            body_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            body_bytes=1,
            no_raw=True,
        )
        session.add(raw)
        await session.flush()
        session.add(
            JobPosting(
                owner=OWNER,
                employer_id=employer.id,
                ats=EXAMPLE_SYSTEM,
                ext_id=slug,
                company=role.company,
                title=role.title,
                match_key=role.match_key,
                first_seen=NOW,
                last_seen=NOW,
                links=[],
                opportunity_id=role.id,
            )
        )
        pay_range = {"pay_min": pay[0], "pay_max": pay[1]} if pay else {}
        session.add(
            CleansedPosting(
                owner=OWNER,
                raw_id=raw.id,
                cleaner_version=1,
                title=role.title,
                employer=role.company,
                locations=[],
                text_clean=text,
                **pay_range,
            )
        )
        await session.flush()
        await raw_copies.repoint(session, OWNER)  # the pointer the code that writes copies keeps (`raw_copies._point`)
        await session.commit()
        return role.id


async def _row(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _star(client, role_id) -> dict:
    answer = await client.put(f"/api/v1/roles/{role_id}/favorite", json={"favorite": True})
    assert answer.status_code == 200
    return answer.json()


async def _turn() -> None:
    await pipeline_turn()


async def _jobs() -> list[ParseJob]:
    async with db.session_factory()() as session:
        return list(
            (await session.scalars(select(ParseJob).where(ParseJob.kind == "rank").order_by(ParseJob.created_at))).all()
        )


async def _rank_report(door, fits: dict[int, int]) -> dict:
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["kind"] == "rank"
    answer = {
        "ranks": [{"n": n, "fit": fit, "why": [f"Posting {n}.", "It decides on duties."]} for n, fit in fits.items()]
    }
    return await mcp_server.jobs_parse_report(door, taken["id"], answer)


async def _packets(role_id) -> list[Packet]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Packet).where(Packet.role_id == role_id))).all())


async def test_an_unranked_star_asks_its_rank_first_in_line_then_its_research_and_reaches_ready_only_once_read(
    world, client, door
):
    other = await _role("other")
    async with db.session_factory()() as session:  # the pool's ordinary batch is waiting already, for the other role
        await rank.queue(session, OWNER, rank.RANK_BATCH)
        await session.commit()
    star = await _role("star")

    body = await _star(client, star)
    assert body["moved"] is False and body["rank"] is True and body["research"] is None, "its rank comes first"
    assert body["role"]["status"] != stages.READY_TO_LAUNCH
    assert body["role"]["next"] is None, "what a star waits for is the normal run: the row says nothing (24.18)"
    jobs = await _jobs()
    assert len(jobs) == 2 and [len((j.fields or {})["items"]) for j in jobs] == [1, 1]

    # The session is handed the star's job before the batch that was queued earlier.
    taken = await mcp_server.jobs_parse_next(door)
    assert [i["role_id"] for i in (await _job(taken["id"])).fields["items"]] == [str(star)]
    await mcp_server.jobs_parse_report(door, taken["id"], {"ranks": [{"n": 1, "fit": 70, "why": ["a", "b"]}]})
    assert (await _row(star)).analysis_state is None, "the research is asked by the pipeline's turn, not the report"

    # Ranked, it is still not in Ready; the turn puts it first in the analyst's line.
    await _turn()
    role = await _row(star)
    assert role.status == stages.FIRST_IMPRESSION and role.analysis_state == analysis.WAITING
    assert line.asked_by_owner(role)
    assert (await _row(other)).analysis_state is None
    assert await _packets(star) == []

    # Researched, it goes up and its packet is asked.
    await submit_findings(findings(star))
    role = await _row(star)
    assert role.status == stages.READY_TO_LAUNCH and analysis.counts(role)
    assert [p.status for p in await _packets(star)] == ["writing"]


async def _job(job_id) -> ParseJob:
    async with db.session_factory()() as session:
        return await session.get(ParseJob, uuid.UUID(job_id))


async def test_an_unread_ranked_star_is_not_in_ready_until_the_analyst_has_read_it(world, client):
    star = await _role("star", stages.SECOND_IMPRESSION)
    async with db.session_factory()() as session:
        await rank_roles(session, {star: 40})
        await session.commit()
    body = await _star(client, star)
    assert body["moved"] is False and body["rank"] is False
    assert body["research"] and body["research"]["already"] is False
    role = await _row(star)
    assert role.status == stages.SECOND_IMPRESSION and line.asked_by_owner(role)
    await _turn()
    assert (await _row(star)).status != stages.READY_TO_LAUNCH, "a turn of the pipeline does not give it the place"
    assert await _next(star) is None, "waiting for the analyst is the normal run: the row says nothing of it"

    await submit_findings(findings(star))
    assert (await _row(star)).status == stages.READY_TO_LAUNCH


async def _next(role_id) -> str | None:
    """What the role's row says is wrong or waits on the owner; None when it says nothing (docs/PRD.md 24.18)."""
    async with db.session_factory()() as session:
        return (await role_row.row(session, OWNER, role_id))["next"]


async def test_a_star_on_a_role_that_is_read_goes_to_ready_at_once_and_its_packet_is_asked(world, client):
    star = await _role("star", stages.FIRST_IMPRESSION, **counted())
    async with db.session_factory()() as session:
        await rank_roles(session, {star: 40})
        await session.commit()
    body = await _star(client, star)
    assert body["moved"] is True and body["rank"] is False and body["research"] is None
    assert body["packet"] and body["role"]["status"] == stages.READY_TO_LAUNCH
    assert (await _row(star)).status == stages.READY_TO_LAUNCH


async def test_a_read_star_whose_research_is_out_of_date_is_read_again_before_it_goes_up(world, client):
    star = await _role("star", stages.FIRST_IMPRESSION, analysis_made_under={"instructions": 999}, **counted())
    async with db.session_factory()() as session:
        await rank_roles(session, {star: 40})
        await session.commit()
    body = await _star(client, star)
    role = await _row(star)
    assert body["moved"] is False and body["research"] and body["packet"] is None
    assert role.status != stages.READY_TO_LAUNCH and role.analysis_state == analysis.WAITING


async def test_a_read_star_that_a_hard_pass_stops_goes_to_gated_out_with_its_reason_not_to_ready(world, client):
    async with db.session_factory()() as session:
        await load_profile(
            session, OWNER, scoring=FIVE, gates={"compensation": {"amount": 95_000, "against": "maximum"}}
        )
        await session.commit()
    star = await _role("star", stages.FIRST_IMPRESSION, pay=(60_000, 70_000), **counted())
    body = await _star(client, star)
    role = await _row(star)
    assert role.status == stages.GATED_OUT and role.screened_out_reason.startswith("Pay must be at least $95,000")
    assert body["moved"] is False and body["packet"] is None
    assert body["role"]["status"] == stages.GATED_OUT


async def test_an_unread_star_that_fails_a_gate_at_cleansed_data_is_gated_out_by_the_turn_with_its_reason(
    world, client
):
    async with db.session_factory()() as session:
        await load_profile(
            session, OWNER, scoring=FIVE, gates={"compensation": {"amount": 95_000, "against": "maximum"}}
        )
        await session.commit()
    star = await _role("star", stages.CLEANSED_DATA, pay=(60_000, 70_000))
    await _star(client, star)
    await _turn()
    role = await _row(star)
    assert role.status == stages.GATED_OUT and role.screened_out_reason.startswith("Pay must be at least $95,000")


async def test_the_pipeline_turn_asks_the_research_of_a_ranked_star_nobody_has_asked_for(world, client):
    star = await _role("star", stages.FIRST_IMPRESSION, favorited_at=datetime.now(UTC))
    async with db.session_factory()() as session:
        await rank_roles(session, {star: 55})
        await session.commit()
    await _turn()
    role = await _row(star)
    assert role.analysis_state == analysis.WAITING and line.asked_by_owner(role)
    assert role.status == stages.FIRST_IMPRESSION
    await _turn()  # a second turn asks nothing more
    assert (await _row(star)).analysis_asked_at == role.analysis_asked_at


async def test_a_starred_role_in_ready_with_no_research_goes_back_to_be_read(world, client):
    star = await _role("star", stages.READY_TO_LAUNCH, favorited_at=datetime.now(UTC))
    async with db.session_factory()() as session:
        await rank_roles(session, {star: 55})
        await session.commit()
    await _turn()
    role = await _row(star)
    assert role.status == stages.FIRST_IMPRESSION and role.analysis_state == analysis.WAITING


async def _unsourced(slug: str, status: str = stages.CLEANSED_DATA, **more) -> uuid.UUID:
    """A role with only the link to its listing on a board: not Sourced."""
    return await _role(
        slug,
        status,
        source_url=None,
        source_kind=None,
        sourced_at=None,
        url=f"https://www.board.example.test/jobs/view/{slug}",
        **more,
    )


async def _browser_items(role_id) -> list:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(BrowserItem).where(BrowserItem.role_id == role_id))).all())


async def _mission_on() -> None:
    async with db.session_factory()() as session:
        await missions.resume(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        await session.commit()


async def test_an_unsourced_star_has_its_sourcing_put_first_in_the_browser_queue_before_rank_and_research(
    world, client
):
    await _mission_on()
    older = await _unsourced("older", stages.SECOND_IMPRESSION)  # on deck, unsourced: sourced in the ordinary way
    star = await _unsourced("star")
    plain = await _unsourced("plain")  # not starred, before the deck: no item (the second pass's job)
    async with db.session_factory()() as session:
        await browser_queue.refresh(session, OWNER)
        await session.commit()
    assert await _browser_items(star) == [] and await _browser_items(plain) == []

    body = await _star(client, star)
    assert body["source"] is True and body["rank"] is False and body["research"] is None and body["moved"] is False
    (item,) = await _browser_items(star)
    assert item.kind == "source" and item.priority == 0 and item.state == "waiting"
    assert await _browser_items(plain) == []
    (other,) = await _browser_items(older)
    assert takeable._order(item) < takeable._order(other), "the star's sourcing is handed out first"
    assert await _jobs() == [], "no rank before its posting is found"
    assert (await _row(star)).analysis_state is None


async def test_a_star_that_was_just_sourced_is_ranked_next_by_the_turn_and_then_read(world, client):
    star = await _unsourced("star")
    await _star(client, star)
    async with db.session_factory()() as session:
        await session.execute(
            update(Opportunity)
            .where(Opportunity.id == star)
            .values(sourced_at=datetime.now(UTC), source_url="https://jobs.example.test/star", source_kind="employer")
        )
        await session.commit()
    await _turn()
    (job,) = await _jobs()
    assert [i["role_id"] for i in job.fields["items"]] == [str(star)] and job.fields.get(rank.FIRST) is True


async def test_the_row_of_a_star_says_nothing_of_what_it_waits_for(world, client):
    """Old: "Starred: waits for its original posting to be found, first in line, then it is ranked and read" and
    "Starred: waits for its First Impression rank, then the analyst reads it first". New (docs/PRD.md 51.6, 24.18): the
    star on the row is the owner's own and what a star waits for is the normal run, so the row says nothing."""
    star = await _unsourced("star")
    await _star(client, star)
    assert await _next(star) is None
    ranked = await _role("ranked")
    await _star(client, ranked)
    assert await _next(ranked) is None


# --- a move to Ready to Launch asked before the read (the owner, 2026-11-03; docs/PRD.md 24.13) -------------------
# No role sits in Ready to Launch without a Second Impression that graded its posting. The owner's move of such a role
# there is not made yet: it runs its impressions first as a star does, and the move is made once it is read.


async def _move_to_ready(client, role_id) -> dict:
    answer = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": stages.READY_TO_LAUNCH})
    assert answer.status_code == 200, answer.text
    return answer.json()


async def _lines(role_id, event: str) -> list:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == role_id, TrailEvent.event == event).order_by(TrailEvent.at))).all())


async def test_moving_an_unread_role_to_ready_runs_its_impressions_first_and_it_takes_its_place_once_read(
    world, client, door
):
    role_id = await _role("hand")
    body = await _move_to_ready(client, role_id)
    assert body["status"] == stages.CLEANSED_DATA, "not placed: no unread role sits in Ready to Launch"
    assert body["next"] is None, "the row says nothing of what the ask waits for (docs/PRD.md 24.18)"
    role = await _row(role_id)
    assert role.ready_asked_at is not None and role.favorited_at is None, "the ask is not a star"
    [asked] = await _lines(role_id, "ready_asked")
    assert asked.actor == "person" and asked.sentence == (
        "You asked for Programs Lead hand at Firm hand in Ready to Launch: it is read first.")
    assert await _lines(role_id, "role_moved") == [], "no move was made, so no hand placed it"
    (job,) = await _jobs()
    assert [i["role_id"] for i in job.fields["items"]] == [str(role_id)], "its rank is asked first"

    await mcp_server.jobs_parse_report(
        door, (await mcp_server.jobs_parse_next(door))["id"], {"ranks": [{"n": 1, "fit": 70, "why": ["a", "b"]}]}
    )
    await _turn()
    role = await _row(role_id)
    assert role.status == stages.FIRST_IMPRESSION and role.analysis_state == analysis.WAITING
    assert line.asked_by_owner(role), "the pipeline's turn asks its research, ahead of every role nobody asked for"

    await submit_findings(findings(role_id))
    role = await _row(role_id)
    assert role.status == stages.READY_TO_LAUNCH and role.ready_asked_at is None, "read: the move is made"
    [moved] = await _lines(role_id, "role_moved")
    assert moved.actor == "person" and moved.sentence.endswith("from First Impression to Ready to Launch.")
    assert await _packets(role_id) == [], "Launch builds the packet, as for any role moved there"


async def test_moving_a_role_whose_read_graded_nothing_to_ready_asks_for_its_research_again(world, client):
    role_id = await _role("empty", stages.SECOND_IMPRESSION, **counted(required=[]))
    body = await _move_to_ready(client, role_id)
    role = await _row(role_id)
    assert body["status"] == stages.SECOND_IMPRESSION and role.ready_asked_at is not None
    assert role.analysis_state == analysis.WAITING and line.asked_by_owner(role)


async def test_an_unsourced_role_moved_to_ready_has_its_sourcing_put_first_in_the_browser_queue(world, client):
    await _mission_on()
    role_id = await _unsourced("hand")
    await _move_to_ready(client, role_id)
    (item,) = await _browser_items(role_id)
    assert item.kind == "source" and item.priority == 0 and item.state == "waiting"


async def test_the_row_of_a_role_moved_to_ready_before_its_read_says_nothing_of_the_ask(world, client):
    """Old: "Being read for Ready to Launch: waits for its original posting to be found, first in line", "Being read
    for Ready to Launch · With the analyst" and, for a star, "Starred: waits ...". New (docs/PRD.md 24.13, 24.18): the
    ask is the owner's own and what it waits for is the normal run, so the row says nothing."""
    unsourced = await _unsourced("a")
    await _move_to_ready(client, unsourced)
    assert await _next(unsourced) is None
    deck = await _role("b", stages.SECOND_IMPRESSION)
    async with db.session_factory()() as session:
        await rank_roles(session, {deck: 40})
        await session.commit()
    await _move_to_ready(client, deck)
    assert await _next(deck) is None
    starred = await _role("c")
    await _star(client, starred)
    await _move_to_ready(client, starred)
    assert await _next(starred) is None


async def test_the_owners_next_move_of_a_role_asked_for_in_ready_withdraws_the_ask(world, client):
    role_id = await _role("hand")
    await _move_to_ready(client, role_id)
    assert (await _row(role_id)).ready_asked_at is not None
    moved = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": stages.FIRST_IMPRESSION})
    assert moved.status_code == 200
    assert (await _row(role_id)).ready_asked_at is None
    assert await _next(role_id) is None, "back in First Impression the row says nothing of an ask that is gone"
