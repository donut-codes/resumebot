"""A role the owner applied to or is in talks about is built out (the owner, 2026-11-05): it holds its
posting's text and its full read. A role in Active Discussion with no text of its posting has its posting found and read
as the owner's own ask, at any hour, and is researched when the text comes; one in Applied is the backlog, in the off
hours. A role whose research was marked done with no text counts as not done, so it is researched when its text comes
(docs/PRD.md 32.6, 39.30). Every name, link and sentence is made up, and no test reaches the network."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import queue as bq
from app.jobsearch.browser import takeable
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, Opportunity, posting_not_found
from app.jobsearch.pipeline import built_out
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.research import analysis, line
from app.models.work import TrailEvent
from tests.builders import (
    ONE,
    WORKER,
    analysis_setup,
    browser_world,
    findings,
    hand_back,
    in_session,
    items,
    lease,
    submit_findings,
)
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import DEFAULT_OWNER, OWNER, POSTING_WORDS

pytestmark = pytest.mark.usefixtures("example_world")

NOW = datetime(2026, 11, 5, 22, 0, tzinfo=UTC)
FEEDSYS = "https://boards.feedsys.test/initech{n}/jobs/40{n}"
EMPLOYER = "https://careers.northwind{n}.test/jobs/{n}"
#: The stages this is about, and a stage on either side of them.
BUILT_OUT = (stages.APPLIED, stages.ACTIVE_DISCUSSION)


async def _role(n: int, status: str = stages.ACTIVE_DISCUSSION, owner: str = DEFAULT_OWNER, **fields) -> uuid.UUID:
    """A role the owner came to by the tracker, found on the made-up board `ONE`, not Sourced, with no text of its
    posting unless the test says so."""
    async with db.session_factory()() as session:
        role = Opportunity(**{
            "owner": owner, "slug": f"role-{n}", "company": f"Initech {n}", "title": f"Programs Director {n}",
            "match_key": match_key(f"Initech {n}", f"Programs Director {n}"), "origin": "tracker",
            "status": status, "found": date(2026, 10, 7), "url": f"https://www.{ONE}/jobs/view/{1000 + n}",
            "board_url": f"https://www.{ONE}/jobs/view/{1000 + n}", "sourced_at": None, **fields})
        session.add(role)
        await session.commit()
        return role.id


def _sourced(n: int) -> dict:
    """The fields of a role Sourced to its employer's own page, which the app's code could not read."""
    return {"url": EMPLOYER.format(n=n), "source_url": EMPLOYER.format(n=n), "source_kind": "employer",
            "sourced_at": NOW}


async def _get(role_id: uuid.UUID) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _refresh(now: datetime = NOW) -> None:
    await in_session(WORKER, lambda s: bq.refresh(s, DEFAULT_OWNER, now))


async def _posting(role_id: uuid.UUID, n: int, *, ats: str, text: str) -> None:
    async with db.session_factory()() as session:
        session.add(JobPosting(
            owner=DEFAULT_OWNER, employer_id=uuid.uuid4(), ats=ats, ext_id=f"posting-{n}", company=f"Initech {n}",
            title=f"Programs Director {n}", match_key=match_key(f"Initech {n}", f"Programs Director {n}"),
            url=FEEDSYS.format(n=n), links=[FEEDSYS.format(n=n)], first_seen=NOW, last_seen=NOW,
            filtered_at=NOW, route="titles", opportunity_id=role_id, text=text))
        await session.commit()


# --- the browser's queue ------------------------------------------------------------------------------------------


async def test_a_role_in_active_discussion_with_no_text_is_sourced_first_and_at_any_hour(js):
    """A role in Active Discussion had no text, no source and a research marked done, and no
    reader asked for it, since the readers took only the stages before the application."""
    await browser_world()
    talks = await _role(1)
    await _refresh()
    (row,) = await items(kind="source")
    assert row.target == str(talks) and row.link == f"https://www.{ONE}/jobs/view/1001"
    assert row.priority == 0 and takeable.owners_item(row), "the owner's own: taken at any hour"


async def test_a_role_in_applied_with_no_text_is_sourced_as_the_backlog_in_the_off_hours(js):
    await browser_world()
    applied = await _role(1, stages.APPLIED)
    await _refresh()
    (row,) = await items(kind="source")
    assert row.target == str(applied) and row.priority == bp.BACKLOG and not takeable.owners_item(row)


@pytest.mark.parametrize("status", BUILT_OUT)
async def test_a_role_that_holds_its_text_has_no_work_whatever_holds_it(js, status):
    await browser_world()
    await _role(1, status, description=POSTING_WORDS, description_at=NOW)
    holds = await _role(2, status)
    await _posting(holds, 2, ats="feedsys", text=POSTING_WORDS)  # a job system's posting of the role
    await _role(3, status, **_sourced(3), description=POSTING_WORDS, description_at=NOW)
    await _refresh()
    assert [row.target for row in await items() if row.kind in ("source", "read")] == []


@pytest.mark.parametrize("status", BUILT_OUT)
async def test_a_snippet_a_tracker_import_gave_is_no_text(js, status):
    """Postings of Applied roles that came from the tracker import hold no text at all."""
    await browser_world()
    snippet = await _role(1, status, description="Lead programs for the board.")
    await _posting(snippet, 1, ats="import", text="Lead programs for the board.")
    await _refresh()
    assert {row.target for row in await items(kind="source")} == {str(snippet)}


async def test_a_sourced_role_in_applied_or_active_discussion_with_no_text_is_read_at_its_source(js):
    await browser_world()
    talks, applied = await _role(1, **_sourced(1)), await _role(2, stages.APPLIED, **_sourced(2))
    await _role(3, **_sourced(3), description=POSTING_WORDS, description_at=NOW)  # it holds its text
    await _refresh()
    by = {row.target: row for row in await items(kind="read")}
    assert set(by) == {str(talks), str(applied)}
    assert by[str(talks)].link == EMPLOYER.format(n=1) and by[str(applied)].link == EMPLOYER.format(n=2)
    assert (by[str(talks)].priority, by[str(applied)].priority) == (0, bp.BACKLOG)
    assert await items(kind="source") == []


@pytest.mark.parametrize("status", BUILT_OUT)
async def test_a_role_that_holds_its_employers_link_is_sourced_in_code_and_read_never_found_by_a_session(js, status):
    """One list for the queue and the code's own sourcing step: a role that already carries one posting on the
    employer's job system is sourced from it, and only its text is left to read."""
    await browser_world()
    held = await _role(1, status, url=FEEDSYS.format(n=1), apply_url=FEEDSYS.format(n=1), board_url=None)
    open_one = await _role(2, status)
    await _refresh()
    role = await _get(held)
    assert (role.source_url, role.source_kind) == (FEEDSYS.format(n=1), "employer") and role.sourced_at is not None
    assert {row.target for row in await items(kind="source")} == {str(open_one)}
    assert {row.target for row in await items(kind="read")} == {str(held)}


@pytest.mark.parametrize("status", BUILT_OUT)
async def test_a_role_the_app_gave_up_on_is_chased_no_more(js, status):
    await browser_world()
    await _role(1, status, analysis={"posting_not_found": True})
    await _role(2, status, **_sourced(2), analysis={"posting_not_found": True})
    await _refresh()
    assert [row for row in await items() if row.kind in ("source", "read")] == []


async def test_the_owner_asked_for_research_of_an_applied_role_is_the_owners_item(js):
    await browser_world()
    asked = await _role(1, stages.APPLIED, analysis={analysis.ASKED_BY: analysis.ASKED_BY_OWNER})
    await _role(2, stages.APPLIED)
    await _refresh()
    by = {row.target: row.priority for row in await items(kind="source")}
    assert by[str(asked)] == 0 and sorted(by.values()) == [0, bp.BACKLOG]


# --- a posting that is gone ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("sourced", [True, False], ids=["its source is read", "its posting is looked for"])
@pytest.mark.parametrize("status", BUILT_OUT)
async def test_a_gone_posting_ends_the_item_with_its_reason_and_the_role_stays_where_the_owner_put_it(
        js, status, sourced):
    """The owner applied, or is in talks: a posting taken down is no reason to move the role (`quiet.end_gone` ends a
    role before the application only). The app gives up on its posting, so no read asks for it again."""
    await browser_world()
    role_id = await _role(1, status, **(_sourced(1) if sourced else {}))
    kind = "read" if sourced else "source"
    item_id = await lease(kind, str(role_id), now=NOW)
    await hand_back(WORKER, item_id, "gone", now=NOW, note="No longer accepting applications")
    role = await _get(role_id)
    assert role.status == status and role.ended_reason is None
    assert posting_not_found(role), "no reader asks for a posting the app gave up on"
    (row,) = await items(kind=kind)
    assert (row.state, row.last_result, row.last_note) == ("done", "gone", "No longer accepting applications")
    async with db.session_factory()() as session:
        (line,) = (await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == role_id, TrailEvent.event == "posting_removed"))).all()
    assert "No longer accepting applications" in line.detail and "stays" in line.sentence
    await _refresh(NOW + timedelta(minutes=5))
    assert [r.state for r in await items(kind=kind)] == ["done"], "the item is not opened again"


async def test_a_gone_posting_still_ends_a_role_before_the_application(js):
    await browser_world()
    role_id = await _role(1, stages.READY_TO_LAUNCH, **_sourced(1))
    item_id = await lease("read", str(role_id), now=NOW)
    await hand_back(WORKER, item_id, "gone", now=NOW, note="No longer accepting applications")
    role = await _get(role_id)
    assert role.status == stages.GONE_QUIET and role.ended_reason == stages.POSTING_GONE


# --- the pipeline's step ------------------------------------------------------------------------------------------

#: What research an old analysis (before 2026-10-15, when a role with no text stopped being researched) left.
OLD_RESEARCH = {"at": "2026-10-13T21:42:54+00:00", "required": [{"item": "Five years of programs", "class": "P"}],
                "pay": {"band": None}, "notes": "Read from a summary of the page."}
DONE_ON_NOTHING = {"analysis_state": analysis.DONE, "analysed_at": datetime(2026, 10, 13, 21, 42, tzinfo=UTC),
                   "fit": 27, "analysis": OLD_RESEARCH}


async def _advance(owner: str = OWNER) -> int:
    async with db.session_factory()() as session:
        done = await built_out.advance(session, owner)
        await session.commit()
    return done


async def _lines(role_id: uuid.UUID) -> list[str]:
    async with db.session_factory()() as session:
        return list(await session.scalars(select(TrailEvent.event).where(TrailEvent.link_id == role_id)))


async def _roles(status: str, n: int = 1, **fields) -> list[uuid.UUID]:
    """Fabrikam-like roles of the owner `OWNER` in `status`, with no text, from the analysis set-up's three."""
    ids = list((await analysis_setup(text=None)).values())[:n]
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).where(Opportunity.id.in_(ids)).values(
            status=status, sourced_at=None, source_url=None, source_kind=None, **fields))
        await session.commit()
    return ids


async def test_a_role_in_active_discussion_with_no_text_waits_for_it_as_the_owners_ask(js):
    [role_id] = await _roles(stages.ACTIVE_DISCUSSION)
    assert await _advance() == 1
    role = await _get(role_id)
    assert role.analysis_state == analysis.NO_TEXT and role.status == stages.ACTIVE_DISCUSSION
    assert role.analysis[analysis.ASKED_BY] == analysis.ASKED_BY_OWNER, "first in the analyst's line with the text"
    assert "waiting_for_text" in await _lines(role_id)


async def test_a_role_in_applied_with_no_text_waits_for_it_and_is_not_the_owners_ask(js):
    [role_id] = await _roles(stages.APPLIED)
    assert await _advance() == 1
    role = await _get(role_id)
    assert role.analysis_state == analysis.NO_TEXT and role.status == stages.APPLIED
    assert analysis.ASKED_BY not in (role.analysis or {})


@pytest.mark.parametrize("status", BUILT_OUT)
async def test_a_research_marked_done_with_no_text_counts_as_not_done_and_is_kept_for_the_research_to_replace(
        js, status):
    """Applied roles of the tracker's import were researched before research
    needed the posting's text, and hold none: they count as waiting for it, with what was stored still there."""
    [role_id] = await _roles(status, **DONE_ON_NOTHING)
    assert await _advance() == 1
    role = await _get(role_id)
    assert role.analysis_state == analysis.NO_TEXT
    assert role.analysis["required"] == OLD_RESEARCH["required"] and role.analysis["at"] == OLD_RESEARCH["at"]
    assert role.fit == 27 and role.analysed_at == DONE_ON_NOTHING["analysed_at"]


async def test_the_step_changes_nothing_for_a_role_that_holds_its_text_or_is_not_one_it_is_for(js):
    ids = await _roles(stages.APPLIED, 3, **DONE_ON_NOTHING)
    async with db.session_factory()() as session:
        holds, closed, early = [await session.get(Opportunity, i) for i in ids]
        holds.description, holds.description_at = POSTING_WORDS, NOW  # it holds its text
        closed.status = stages.GONE_QUIET  # not in play
        early.status = stages.FIRST_IMPRESSION  # its stage has its own readers
        await session.commit()
    assert await _advance() == 0
    got = [await _get(i) for i in ids]
    assert [r.analysis_state for r in got] == [analysis.DONE] * 3


async def test_the_step_leaves_a_role_in_the_analysts_line_and_one_the_app_gave_up_on(js):
    waiting, given_up = await _roles(stages.ACTIVE_DISCUSSION, 2)
    async with db.session_factory()() as session:
        (await session.get(Opportunity, waiting)).analysis_state = analysis.WAITING
        (await session.get(Opportunity, given_up)).analysis = {"posting_not_found": True}
        await session.commit()
    assert await _advance() == 0
    assert (await _get(waiting)).analysis_state == analysis.WAITING
    assert (await _get(given_up)).analysis_state is None


async def test_the_step_is_done_once_and_a_second_look_adds_nothing(js):
    applied, talks = await _roles(stages.APPLIED, 2)
    async with db.session_factory()() as session:
        (await session.get(Opportunity, talks)).status = stages.ACTIVE_DISCUSSION
        await session.commit()
    assert await _advance() == 2
    before = [await _lines(r) for r in (applied, talks)]
    assert await _advance() == 0
    assert [await _lines(r) for r in (applied, talks)] == before


async def test_a_role_waiting_for_its_text_that_reaches_active_discussion_becomes_the_owners_ask(js):
    """A role in Active Discussion that waits for its text with no owner ask (an Applied role moved there) gets it."""
    [role_id] = await _roles(stages.APPLIED)
    assert await _advance() == 1
    assert analysis.ASKED_BY not in ((await _get(role_id)).analysis or {})
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).status = stages.ACTIVE_DISCUSSION
        await session.commit()
    assert await _advance() == 1
    role = await _get(role_id)
    assert role.analysis_state == analysis.NO_TEXT and role.analysis[analysis.ASKED_BY] == analysis.ASKED_BY_OWNER
    assert await _advance() == 0


async def test_the_pipelines_turn_builds_a_role_out(js):
    [role_id] = await _roles(stages.ACTIVE_DISCUSSION, **DONE_ON_NOTHING)
    async with db.session_factory()() as session:
        await pipeline.run(session, OWNER)
        await session.commit()
    assert (await _get(role_id)).analysis_state == analysis.NO_TEXT


@pytest.mark.parametrize("status", BUILT_OUT)
async def test_the_role_is_researched_when_its_text_comes_and_keeps_its_stage(client, status):
    """(a) and (b): the text comes (the browser's read or the app's), the role goes in the analyst's line, the owner's
    ask first, and the findings replace the old research; Applied or Active Discussion is where the owner left it."""
    [role_id] = await _roles(status, **DONE_ON_NOTHING)
    await _advance()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.description, role.description_at = POSTING_WORDS, datetime.now(UTC)
        role.sourced_at, role.source_url, role.source_kind = datetime.now(UTC), EMPLOYER.format(n=1), "employer"
        assert await analysis.resume_waiting(session, OWNER) == 1
        await session.commit()
    role = await _get(role_id)
    assert role.analysis_state == analysis.WAITING
    assert line.asked_by_owner(role) == (status == stages.ACTIVE_DISCUSSION)
    assert role.analysis["required"] == OLD_RESEARCH["required"], "the old research stays until the new replaces it"
    reply = await submit_findings(findings(role_id))
    assert reply["ok"] and reply["stage"] == status
    role = await _get(role_id)
    assert role.analysis_state == analysis.DONE and role.status == status
    assert role.analysis["required"] != OLD_RESEARCH["required"] and role.analysis.get(analysis.DECK_READ) is True


# --- the read, from the item to the research ---------------------------------------------------------------------

#: What a tracker's import or a listing leaves as a role's description: a few words, no posting.
SNIPPET = "Lead programs for the board."


@pytest.mark.parametrize("held", [None, SNIPPET], ids=["no description", "a snippet"])
@pytest.mark.parametrize("status", BUILT_OUT)
async def test_the_text_a_read_hands_back_builds_the_role_out_and_the_item_is_not_opened_again(js, status, held):
    """The whole way, for each stage: the step makes the role wait for its text, the read item is leased and handed
    back with the posting, the role holds it and is in the analyst's line (the owner's ask first in talks), and the next
    refresh does not open the item again. Found on review 2026-11-05: a role whose description was a snippet (unbuilt,
    so it had a read item) threw the text the browser brought away and marked the item done, and the next refresh
    opened it again, every refresh, at any hour in talks."""
    await browser_world()
    role_id = await _role(1, status, **_sourced(1), description=held, description_at=NOW if held else None,
                          **DONE_ON_NOTHING)
    assert await _advance(DEFAULT_OWNER) == 1
    item_id = await lease("read", str(role_id), now=NOW)
    assert (await hand_back(WORKER, item_id, "text", now=NOW, text=POSTING_WORDS))["item"] == "done"
    role = await _get(role_id)
    assert role.description == POSTING_WORDS and role.status == status
    assert role.analysis_state == analysis.WAITING, "the role is in the analyst's line"
    assert line.asked_by_owner(role) == (status == stages.ACTIVE_DISCUSSION)
    await _refresh(NOW + timedelta(minutes=5))
    assert [row.state for row in await items(kind="read")] == ["done"], "the item is not opened again"


async def test_a_text_read_for_a_role_before_the_application_that_holds_only_a_snippet_replaces_it(js):
    """The same hand-back is every stage's: a role in Second Impression with a snippet has a read item too (the
    readers take a snippet for no text), and the text the browser brings replaces it."""
    await browser_world()
    role_id = await _role(1, stages.SECOND_IMPRESSION, **_sourced(1), description=SNIPPET, description_at=NOW)
    item_id = await lease("read", str(role_id), now=NOW)
    await hand_back(WORKER, item_id, "text", now=NOW, text=POSTING_WORDS)
    assert (await _get(role_id)).description == POSTING_WORDS
    await _refresh(NOW + timedelta(minutes=5))
    assert [row.state for row in await items(kind="read")] == ["done"], "the item is not opened again"


@pytest.mark.parametrize("status", BUILT_OUT)
async def test_a_read_nobody_can_finish_is_stuck_with_its_reason_and_is_not_opened_again(js, status):
    """Three tries that read nothing end the item as stuck, the reason on the role; the role stays where the owner put
    it, waiting for its text."""
    await browser_world()
    role_id = await _role(1, status, **_sourced(1), **DONE_ON_NOTHING)
    await _advance(DEFAULT_OWNER)
    for day in range(3):
        at = NOW + timedelta(days=day)
        item_id = await lease("read", str(role_id), now=at)
        await hand_back(WORKER, item_id, "unreadable", now=at, note="only a preview shows")
    (row,) = await items(kind="read")
    assert (row.state, row.tries) == ("stuck", 3)
    role = await _get(role_id)
    assert "only a preview shows" in role.browser_note
    assert role.status == status and role.analysis_state == analysis.NO_TEXT
    await _refresh(NOW + timedelta(days=4))
    assert [row.state for row in await items(kind="read")] == ["stuck"], "a stuck item is not opened again"


async def _waiting_line(role_id: uuid.UUID) -> TrailEvent:
    async with db.session_factory()() as session:
        return (await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == role_id, TrailEvent.event == "waiting_for_text"))).one()


@pytest.mark.parametrize("fields, looked_for", [
    ({}, True),
    ({"url": None, "board_url": None}, False),
    ({"source_url": f"https://www.{ONE}/jobs/view/1001", "source_kind": "board", "sourced_at": NOW}, False),
], ids=["a link to read", "no link at all", "an easy apply"])
async def test_the_history_promises_a_read_only_to_a_role_something_can_read(js, fields, looked_for):
    """A role with no link to open, or an Easy Apply (its listing was the source and is never read again), waits for
    its text all the same (a research marked done on no text counts as not done) but no item is ever made for it, so
    its history does not say the app finds and reads its posting."""
    await browser_world()
    role_id = await _role(1, stages.APPLIED, **fields, **DONE_ON_NOTHING)
    assert await _advance(DEFAULT_OWNER) == 1
    assert (await _get(role_id)).analysis_state == analysis.NO_TEXT
    assert ("finds its posting" in (await _waiting_line(role_id)).detail) is looked_for
    await _refresh()
    assert bool([row for row in await items() if row.kind in ("source", "read")]) is looked_for


# --- an analysis is never marked done on no text ------------------------------------------------------------------


async def test_findings_for_a_role_that_holds_only_a_snippet_are_not_taken(client):
    """`held` gives a role's own snippet when it has nothing better (what its page shows), and a snippet is not the
    posting's text (docs/PRD.md 39.29): a role holding one is a role with no text, whatever the line it is in."""
    [role_id] = await _roles(stages.SECOND_IMPRESSION)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.description, role.description_at = "Lead the programs team.", NOW
        role.analysis_state, role.analysis_asked_at = analysis.WAITING, NOW
        await session.commit()
    async with db.session_factory()() as session:
        reply = await analysis.submit(session, OWNER, findings(role_id))
    assert reply == {"ok": False, "taken": False, "reason": analysis.NO_TEXT_REASON}
    assert (await _get(role_id)).analysis_state == analysis.WAITING


def test_the_stages_the_analysis_may_send_a_text_back_in_are_the_stages_a_reader_reads():
    """`may_send_back` lets a text go only where a reader asks for it again: one list kept in two modules, since the
    analysis cannot import the browser's reader."""
    from app.jobsearch.browser import reader

    assert analysis.READ_STAGES == reader.READ_IN_FULL_STAGES
