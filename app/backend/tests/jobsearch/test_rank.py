"""The Haiku ranker, First Impression: the short profile is made from what the
owner saved and kept as versions; every role in play that has a cleansed copy is ranked in batches of 25 by parser
sessions, newest posting first, the score kept beside the fit percent; level and field exclusions are gates in the
profile; a rank moves the role to First Impression and the best go on deck (`pipeline.run`), and it closes no role.
Synthetic data only."""

import ast
import copy
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import stages
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.models import JobPosting, Opportunity, ParseJob, ProfileVersion, RoleRank, SearchRun
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.postings import cleansed, raw_copies
from app.jobsearch.profile import schema
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import rank
from app.settings import settings
from tests import builders
from tests.builders import MINUTE, parse_runner
from tests.support import EXAMPLE_SYSTEM, FEW_ARGUMENTS, OWNER, PROFILE, TOKEN, bound_arguments, load_profile

NIGHT = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
TEXT = (
    "Lead the programs team.\n"
    "Run the quarterly review cycle for the whole office.\n"
    "Own the yearly budget.\n"
    "Report to the head of operations.\n"
    "Work with every team on its plan for the year.\n"
    "Coach the managers and run the monthly staff meeting.\n"
    "Keep the vendor contracts current and on budget.\n"
    "Present results to the leadership group each quarter.\n"
    "You have ten years in operations or programs.")


@pytest.fixture
async def agent(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    await parse_runner(seen=MINUTE)


@pytest.fixture
def door():
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


async def _profile(**parts) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, **parts)
        await session.commit()


async def _role(slug: str, *, text: str | None = TEXT, status="second_impression", fit=5, ended_reason=None, days_ago=0,
                posted: date | None = None, company: str = "Globex") -> uuid.UUID:
    """A role with a posting that holds `text`, entered as a cleansed copy (the legacy way: no fetch needed)."""
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug=slug, company=company, title=f"Programs Lead {slug}", fit=fit,
                           origin="scout", match_key=f"{slug}|programs lead", status=status,
                           ended_reason=ended_reason, created_at=NIGHT - timedelta(days=days_ago), posted=posted,
                           found=(NIGHT - timedelta(days=days_ago)).date())
        session.add(role)
        await session.flush()
        if text is not None:
            session.add(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=slug,
                                   company=company, title=role.title, location="Remote", text=text,
                                   first_seen=NIGHT, last_seen=NIGHT, links=[], opportunity_id=role.id))
            await session.flush()
            await raw_copies.keep_legacy(session, OWNER)
        await session.commit()
        return role.id


async def _queue(n=None, dry_run=False) -> dict:
    async with db.session_factory()() as session:
        counts = await rank.queue(session, OWNER, n, dry_run=dry_run)
        await session.commit()
        return counts


async def _jobs() -> list[ParseJob]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(ParseJob).where(ParseJob.kind == "rank"))).all())


async def _ranks() -> list[RoleRank]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(RoleRank).order_by(RoleRank.fit))).all())


def _answer(job: dict, fits: dict[int, int]) -> dict:
    return {"ranks": [{"n": n, "fit": fit, "why": [f"Posting {n}.", "It decides on duties."]}
                      for n, fit in fits.items()]}


# --- the short profile ----------------------------------------------------------------------------------------------


def test_the_profile_is_made_from_the_saved_roles_and_gates_and_says_what_a_gate_is():
    text = rank.render_profile(schema.Profile.model_validate(PROFILE))
    assert "Builders" in text and "Director of Programs" in text and "Plans the year" in text
    assert text.index("Target roles") < text.index("Gates (each is a hard no")
    assert "95,000" in text, "the pay gate is one of the gates"


def test_level_and_field_exclusions_the_owner_saved_are_gates_not_ranked_preferences():
    profile = copy.deepcopy(PROFILE)
    profile["gates"]["other"] += [
        {"id": "no-sales", "kind": "rule", "section": "description", "text": "Never a sales job."},
        {"id": "not-junior", "kind": "rule", "section": "qualifications", "text": "Nothing below the senior level."}]
    text = rank.render_profile(schema.Profile.model_validate(profile))
    gates = text[text.index("Gates (each is a hard no"):]
    assert "- Never a sales job" in gates and "- Nothing below the senior level" in gates
    assert "never a reason to reject" not in text and "ranked" not in text


def test_a_profile_with_no_role_and_no_gate_makes_no_text():
    assert rank.render_profile(schema.Profile()) == ""


def test_a_gate_zeros_why_names_its_gate_on_the_line_that_starts_with_gate():
    assert rank.gate_line("Gate: pay is under the floor.\nA programs role at a bank.") == (
        "Gate: pay is under the floor.")
    assert rank.gate_line("A programs role at a bank.\nGate: the work is in person.") == (
        "Gate: the work is in person."), "the ranker has put it on the second line"
    assert rank.gate_line("Gate: pay is under the floor\nA programs role.") == "Gate: pay is under the floor."


async def test_a_changed_profile_is_the_next_version_and_an_unchanged_one_is_not_saved_again(js):
    async with db.session_factory()() as session:
        with pytest.raises(rank.RankError):
            await rank.ensure_profile(session, OWNER)
        await load_profile(session, OWNER)
        first = await rank.ensure_profile(session, OWNER)
        again = await rank.ensure_profile(session, OWNER)
        assert (first.version, again.id, first.kind) == (1, first.id, "rank_profile")
        saved = copy.deepcopy(PROFILE["criteria"])
        saved["target_roles"][0]["titles"].append("Head of Programs")
        await profile_store.save(session, OWNER, "criteria", saved, by="test")
        second = await rank.ensure_profile(session, OWNER)
        assert second.version == 2 and "Head of Programs" in second.data["text"]
        assert "Head of Programs" not in first.data["text"], "an earlier version is never changed"
        saved_versions = await session.scalars(select(ProfileVersion).where(ProfileVersion.kind == "rank_profile"))
        assert len(saved_versions.all()) == 2


# --- the jobs -------------------------------------------------------------------------------------------------------


async def test_every_role_with_a_cleansed_copy_waits_in_batches_of_25_and_none_is_queued_twice(agent):
    await _profile()
    for i in range(27):
        await _role(f"r{i}")
    await _role("no-copy", text=None)
    assert await _queue(dry_run=True) == {"version": 1, "roles": 27, "jobs": 2} and await _jobs() == []
    assert await _queue() == {"version": 1, "roles": 27, "jobs": 2}
    sizes = sorted(len(j.fields["items"]) for j in await _jobs())
    assert sizes == [2, 25] and all(j.state == "waiting" for j in await _jobs())
    assert await _queue() == {"version": 1, "roles": 0, "jobs": 0}, "the roles are in jobs already"


async def test_a_job_holds_the_profile_and_the_cleansed_postings_numbered_from_one(agent):
    await _profile()
    eeo = "We are an equal opportunity employer and do not discriminate at all."
    await _role("a", text=f"About the job.\n{eeo}\n{TEXT}")
    await _queue()
    (job,) = await _jobs()
    assert job.text.startswith("CANDIDATE PROFILE") and "### Posting 1" in job.text
    assert "Lead the programs team." in job.text
    assert "equal opportunity" not in job.text, "it reads the cleansed copy"
    assert [i["n"] for i in job.fields["items"]] == [1] and job.fields["profile_version"] == 1


async def test_a_session_ranks_the_batch_and_the_score_sits_beside_the_fit_percent_and_changes_no_fit(agent, door):
    await _profile()
    first, second = await _role("a", fit=5), await _role("b", fit=1)
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["kind"] == "rank" and taken["how"] == rank.HOW and "ranks:" in taken["response_template"]
    reported = await mcp_server.jobs_parse_report(door, taken["id"], _answer(taken, {1: 82, 2: 0}))
    assert reported["ok"] is True and reported["state"] == "done"
    stored = await _ranks()
    assert [(r.fit, r.why, r.profile_version) for r in stored] == [
        (0, "Posting 2.\nIt decides on duties.", 1), (82, "Posting 1.\nIt decides on duties.", 1)]
    async with db.session_factory()() as session:
        roles = {r.id: r for r in (await session.scalars(select(Opportunity))).all()}
        assert (roles[first].fit, roles[second].fit) == (5, 1)
    assert (await _jobs())[0].text == "" and await _queue() == {"version": 1, "roles": 0, "jobs": 0}


async def test_a_rank_already_kept_for_the_same_role_and_copy_stands_and_the_rest_of_the_report_is_kept(agent, door):
    """Two rank jobs had taken the same role, and the second report failed whole on the unique
    key (role, copy, profile version), so none of its other ranks were kept."""
    await _profile()
    first, second = await _role("a"), await _role("b")
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    n = {i["role_id"]: i["n"] for i in (await _jobs())[0].fields["items"]}
    async with db.session_factory()() as session:  # the other job's rank of the first role landed in the meantime
        job = (await session.scalars(select(ParseJob))).one()
        assert await rank.keep(session, OWNER, job, {n[str(first)]: (40, "Earlier. It decides.")}) == 1
        await session.commit()
    reported = await mcp_server.jobs_parse_report(door, taken["id"], _answer(taken, {n[str(first)]: 82,
                                                                                      n[str(second)]: 30}))
    assert reported["ok"] is True and reported["state"] == "done"
    assert sorted((r.role_id, r.fit) for r in await _ranks()) == sorted([(first, 40), (second, 30)])


async def test_ranks_are_kept_in_the_order_of_their_keys_so_two_reports_of_the_same_roles_cannot_deadlock(
        agent, door, monkeypatch):
    """Two rank reports over the same roles, each inserting in its own posting order, could wait on each other's
    unique key and deadlock. Both now take the keys in one order."""
    await _profile()
    roles = [await _role(f"r{i}") for i in range(6)]
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    n = {i["role_id"]: i["n"] for i in (await _jobs())[0].fields["items"]}
    seen: list[tuple[uuid.UUID, uuid.UUID]] = []
    real = rank.insert

    def spy(model):
        stmt = real(model)
        values = stmt.values

        def record(**kw):
            seen.append((kw["role_id"], kw["cleansed_id"]))
            return values(**kw)
        stmt.values = record
        return stmt
    monkeypatch.setattr(rank, "insert", spy)
    reported = await mcp_server.jobs_parse_report(door, taken["id"], _answer(taken, {n[str(r)]: 10 for r in roles}))
    assert reported["ok"] is True and len(seen) == 6
    assert seen == sorted(seen)


async def test_a_report_of_ranks_moves_the_role_to_first_impression_and_puts_the_best_on_deck(agent, door):
    """The deck holds the owner's daily number (here 1): the best rank goes up to Second Impression, the other waits in
    First Impression with its rank. Nothing is closed on a score, not even 0."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 1})
        await session.commit()
    # An employer the profile does not exclude: the turn reads the gate facts first (`schedule.read_gate_facts`).
    best = await _role("best", status=stages.CLEANSED_DATA, company="Northwind")
    worst = await _role("worst", status=stages.CLEANSED_DATA, company="Northwind")
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    n = {i["role_id"]: i["n"] for i in (await _jobs())[0].fields["items"]}
    await mcp_server.jobs_parse_report(door, taken["id"], _answer(taken, {n[str(best)]: 82, n[str(worst)]: 0}))
    async with db.session_factory()() as session:
        roles = {r.id: r.status for r in (await session.scalars(select(Opportunity))).all()}
    assert roles == {best: stages.CLEANSED_DATA, worst: stages.CLEANSED_DATA}, (
        "the report asks the pipeline's loop for a turn and runs none itself (the owner, 2026-10-23)")
    await builders.pipeline_turn()  # the turn the report asked for, as the app's loop runs it
    async with db.session_factory()() as session:
        roles = {r.id: r.status for r in (await session.scalars(select(Opportunity))).all()}
    # Replaced behaviour (the best-of queue): Second Impression holds only read roles, so the best rank is marked to be
    # read next and both roles stay in First Impression.
    assert roles == {best: stages.FIRST_IMPRESSION, worst: stages.FIRST_IMPRESSION}
    async with db.session_factory()() as session:
        assert await pipeline.marked_ids(session, OWNER) == {best}


async def test_a_role_at_cleansed_data_or_first_impression_with_a_copy_is_ranked_newest_posting_first(agent):
    """A posting's posted date decides, else the day the app found the role."""
    await _profile()
    old = await _role("old", status=stages.CLEANSED_DATA, posted=date(2026, 9, 1))
    new = await _role("new", status=stages.FIRST_IMPRESSION, posted=date(2026, 10, 6))
    undated_recent = await _role("undated", status=stages.CLEANSED_DATA, days_ago=1)  # found Oct 7, no posted date
    async with db.session_factory()() as session:
        todo = await rank.unranked(session, OWNER, 1)
    assert [r for r, _ in todo] == [undated_recent, new, old]


async def test_the_roles_to_rank_are_found_with_a_few_arguments_however_many_roles_have_a_copy(agent):
    """Pool pulls failed with asyncpg's "the number of query arguments cannot exceed
    32767" and no rank job was queued, because `unranked` bound the id of every role that has a copy (twice) and of
    every copy, and a large database passed that count. More roles wait for a rank here than a statement that binds
    no list of ids carries arguments (`FEW_ARGUMENTS`), each a real row with a copy (the ranker asks the database for
    them now, so there is no list in Python to make a crowd up in); every one is found, and no statement the ranker
    sends binds an argument for each of them."""
    await _profile()
    old = await _role("old", status=stages.CLEANSED_DATA, posted=date(2026, 9, 1))
    new = await _role("new", status=stages.FIRST_IMPRESSION, posted=date(2026, 10, 6))
    await _role("closed", status=stages.GATED_OUT)
    async with db.session_factory()() as session:
        crowd = [Opportunity(owner=OWNER, slug=f"crowd-{n}", company="Globex", title=f"Programs Lead {n}", fit=5,
                             origin="scout", match_key=f"crowd-{n}|programs lead", status=stages.FIRST_IMPRESSION,
                             created_at=NIGHT, posted=date(2026, 9, 15), found=NIGHT.date())
                 for n in range(FEW_ARGUMENTS + 1)]
        session.add_all(crowd)
        await session.flush()
        session.add_all(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=role.slug,
                                   company="Globex", title=role.title, location="Remote", text=TEXT, first_seen=NIGHT,
                                   last_seen=NIGHT, links=[], opportunity_id=role.id) for role in crowd)
        await session.flush()
        await raw_copies.keep_legacy(session, OWNER)
        await session.commit()
        crowded = {role.id for role in crowd}
    with bound_arguments() as bound:
        async with db.session_factory()() as session:
            todo = await rank.unranked(session, OWNER, 1)
    found = [r for r, _ in todo]
    assert found[0] == new and found[-1] == old and set(found[1:-1]) == crowded
    assert bound and max(bound) <= FEW_ARGUMENTS


async def test_a_role_a_gate_closed_or_one_past_the_deck_is_not_queued_for_ranking(agent):
    await _profile()
    waiting = [await _role(f"w-{status}", status=status) for status in stages.PRE_READY]
    for status in (stages.READY_TO_LAUNCH, stages.APPLIED, stages.ACTIVE_DISCUSSION, stages.GATED_OUT,
                   stages.REJECTED, stages.GONE_QUIET, stages.WITHDREW):
        await _role(f"x-{status}", status=status)
    assert await _queue() == {"version": 1, "roles": len(stages.PRE_READY), "jobs": 1}
    queued = {i["role_id"] for i in (await _jobs())[0].fields["items"]}
    assert queued == {str(r) for r in waiting}


async def test_a_report_with_a_bad_entry_names_every_problem_and_keeps_nothing(agent, door):
    await _profile()
    await _role("a"), await _role("b")
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    bad = {"ranks": [{"n": 1, "fit": 120, "why": ["a", "b"]}, {"n": 2, "fit": 40, "why": ["only", "too", "many"]},
                     {"n": 9, "fit": 10, "why": ["x"]}]}
    async with db.session_factory()() as session:
        with pytest.raises(role_parse.ParseError) as caught:
            await role_parse.report(session, OWNER, taken["id"], bad)
    said = str(caught.value)
    assert "ranks item 1: fit must be a number from 0 to 100" in said and "ranks item 2" in said
    assert "ranks item 3: n must be a posting's number, 1 to 2" in said
    assert await _ranks() == [] and (await _jobs())[0].state == "reading", "the job stays open for another try"


async def test_a_posting_the_session_left_out_is_not_an_error_and_is_ranked_by_the_next_job(agent, door):
    await _profile()
    # A role the rank leaves out is at Cleansed Data and meets the hard gates (the profile never works for Globex).
    await _role("a", company="Initech"), await _role("b", company="Initech")
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, taken["id"], _answer(taken, {1: 60}))
    assert len(await _ranks()) == 1
    assert (await _jobs())[0].fields["left_out"] == 1
    assert await _queue() == {"version": 1, "roles": 1, "jobs": 1}


async def test_a_changed_profile_ranks_every_role_again_and_keeps_the_earlier_ranks(agent, door):
    await _profile()
    await _role("a")
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, taken["id"], _answer(taken, {1: 50}))
    async with db.session_factory()() as session:
        saved = copy.deepcopy(PROFILE["criteria"])
        saved["company"] = "A public agency."
        await profile_store.save(session, OWNER, "criteria", saved, by="test")
        await session.commit()
    assert await _queue() == {"version": 2, "roles": 1, "jobs": 1}
    taken = await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, taken["id"], _answer(taken, {1: 70}))
    assert [(r.profile_version, r.fit) for r in sorted(await _ranks(), key=lambda r: r.profile_version)] == [
        (1, 50), (2, 70)]


# --- a posting is sent whole ----------------------------------------------------------------------------------------


def _long(slug: str, chars: int) -> str:
    """A posting of at least `chars` characters made of lines that are all different (the cleaner drops a repeated
    line), its last line naming `slug`: whoever is sent the posting cut short never sees that line."""
    lines, size, i = [], 0, 0
    while size < chars:
        line = f"Duty {i}: run program {i} for team {i} and report on it each quarter."
        lines.append(line)
        size += len(line) + 1
        i += 1
    return "\n".join([*lines, f"END OF POSTING {slug}."])


async def test_a_posting_is_sent_whole_however_long_it_is(agent):
    """The ranker was sent the first 4,000 characters of a posting (`TEXT_CHARS`, a budget for the one
    session); many cleansed copies are longer and many roles have their first requirement past the cut.
    An agent is never cut to fit a small-model budget (the owner, 2026-10-14)."""
    await _profile()
    await _role("long", text=_long("long", 12_000))
    await _queue()
    (job,) = await _jobs()
    assert "END OF POSTING long." in job.text, "the last line of the posting is sent"
    async with db.session_factory()() as session:
        (copy,) = (await cleansed.copies_by_role(session, OWNER)).values()
    assert len(copy.text_clean) > 12_000 and copy.text_clean in job.text, "the cleansed copy goes in whole"


async def test_a_batch_of_long_postings_holds_fewer_roles_and_none_of_them_is_cut(agent):
    await _profile()
    for i in range(5):
        await _role(f"p{i}", text=_long(f"p{i}", 30_000))
    assert await _queue(dry_run=True) == {"version": 1, "roles": 5, "jobs": 2} and await _jobs() == []
    assert await _queue() == {"version": 1, "roles": 5, "jobs": 2}
    jobs = await _jobs()
    assert sorted(len(j.fields["items"]) for j in jobs) == [2, 3], "five postings of 30,000 characters: 3 and 2"
    for job in jobs:
        assert len(job.text[job.text.index("JOB POSTINGS"):]) <= rank.BATCH_CHARS, "within one session's batch"
        assert job.text.count("END OF POSTING") == job.text.count("### Posting ") == len(job.fields["items"])
    assert await _queue() == {"version": 1, "roles": 0, "jobs": 0}, "every role is in a job already"


async def test_a_posting_too_big_alone_is_not_queued_says_why_in_the_log_and_is_never_cut(agent, monkeypatch, caplog):
    monkeypatch.setattr(rank, "BATCH_CHARS", 3_000)
    await _profile()
    big = await _role("big", text=_long("big", 5_000))
    small = await _role("small")
    with caplog.at_level("WARNING", logger=rank.log.name):
        assert await _queue() == {"version": 1, "roles": 1, "jobs": 1}
    (job,) = await _jobs()
    assert [i["role_id"] for i in job.fields["items"]] == [str(small)] and "END OF POSTING big." not in job.text
    refusals = [r.getMessage() for r in caplog.records if str(big) in r.getMessage()]
    assert len(refusals) == 1 and "not cut" in refusals[0] and "3,000" in refusals[0], refusals
    async with db.session_factory()() as session:
        assert await rank.ask_first(session, OWNER, await session.get(Opportunity, big)) is False, "nor for a star"
    assert len(await _jobs()) == 1


async def test_a_profile_that_leaves_no_room_for_a_posting_queues_nothing_and_cuts_nothing(agent, monkeypatch):
    """The reply that hands a job out may not pass `REPLY_CEILING_CHARS` (the door refuses it), so what the profile and
    the instructions take is taken from the room of the postings."""
    await _profile()
    await _role("a")
    monkeypatch.setattr(rank, "REPLY_RESERVE_CHARS", rank.REPLY_CEILING_CHARS - 100)
    assert await _queue() == {"version": 1, "roles": 0, "jobs": 0} and await _jobs() == []
    monkeypatch.setattr(rank, "REPLY_RESERVE_CHARS", 20_000)
    assert await _queue() == {"version": 1, "roles": 1, "jobs": 1}


# --- a rank made on a cut text is made again -------------------------------------------------------------------------


async def test_a_rank_made_on_a_cut_text_counts_as_unranked_and_the_whole_text_rank_replaces_it_in_place(agent, door):
    """The ranks made before the whole text was sent (migration 0144 marks them `text_cut`) are made again by the
    pool's ordinary ranking: the role waits as if unranked, its old rank stands in the deck until the new one lands,
    and the new one takes the old row's place under the same key (role, copy, profile version)."""
    await _profile()
    long, short = await _role("long", text=_long("long", 9_000)), await _role("short")
    async with db.session_factory()() as session:
        profile = await rank.ensure_profile(session, OWNER)
        copies = await cleansed.current_for_roles(session, OWNER)
        for role_id, cut in ((long, True), (short, False)):
            session.add(RoleRank(owner=OWNER, role_id=role_id, cleansed_id=copies[role_id],
                                 profile_version=profile.version, fit=33, why="Old.\nOn a cut text.", text_cut=cut))
        await session.commit()
        before = {r.role_id: r.id for r in (await session.scalars(select(RoleRank))).all()}
        assert [r for r, _ in await rank.unranked(session, OWNER, profile.version)] == [long]
    assert await _queue() == {"version": 1, "roles": 1, "jobs": 1}
    assert {r.fit for r in await _ranks()} == {33}, "the old rank stands until the new one lands"
    taken = await mcp_server.jobs_parse_next(door)
    reported = await mcp_server.jobs_parse_report(door, taken["id"], _answer(taken, {1: 77}))
    assert reported["ok"] is True and reported["state"] == "done"
    stored = {r.role_id: r for r in await _ranks()}
    assert (stored[long].fit, stored[long].text_cut, stored[long].id) == (77, False, before[long])
    assert (stored[short].fit, stored[short].text_cut, stored[short].id) == (33, False, before[short])
    assert len(stored) == 2 and await _queue() == {"version": 1, "roles": 0, "jobs": 0}


# --- the pool queues the batches itself ------------------------------------------------------------------------------


async def _only_the_ranker(reader):
    """The pull's look with everything to the ranker's right paused (research, employers) and no room for a search."""
    from app.jobsearch.work.session_kinds import RESEARCHER, SEARCH

    return {RESEARCHER, SEARCH}, False


async def _anything(reader):
    return set(), False


async def test_the_pool_queues_a_batch_for_each_pull_until_every_role_has_a_rank(agent):
    from app.jobsearch.pool import pull

    await _profile()
    for i in range(26):
        await _role(f"r{i}", status=stages.FIRST_IMPRESSION)
    async with db.session_factory()() as session:
        assert await pull.start_next(session, OWNER, _only_the_ranker) is True
        assert await pull.start_next(session, OWNER, _only_the_ranker) is True
        assert await pull.start_next(session, OWNER, _only_the_ranker) is False, "every role is in a batch"
        await session.commit()
    assert sorted(len(j.fields["items"]) for j in await _jobs()) == [1, 25]


async def test_the_pool_queues_one_batch_for_each_pull_though_long_postings_split_its_roles(agent):
    from app.jobsearch.pool import pull

    await _profile()
    for i in range(5):
        await _role(f"p{i}", text=_long(f"p{i}", 30_000), status=stages.FIRST_IMPRESSION)
    async with db.session_factory()() as session:
        assert await pull.start_next(session, OWNER, _only_the_ranker) is True
        await session.commit()
    assert len(await _jobs()) == 1, "one batch for the pull, whatever the postings' length"
    async with db.session_factory()() as session:
        assert await pull.start_next(session, OWNER, _only_the_ranker) is True
        assert await pull.start_next(session, OWNER, _only_the_ranker) is False, "every role is in a batch"
        await session.commit()
    assert sorted(len(j.fields["items"]) for j in await _jobs()) == [2, 3]


async def test_by_day_the_pool_hands_out_a_rank_job_and_the_decks_research_and_nothing_else_of_its_own(
        agent, monkeypatch):
    from app.jobsearch.pool import pull
    from app.jobsearch.work import hours as night
    from app.jobsearch.work import session_kinds

    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)  # by day
    await _profile()
    await _role("waiting", status=stages.FIRST_IMPRESSION)
    async with db.session_factory()() as session:
        assert await pull.start_next(session, OWNER, _only_the_ranker) is True, "the rank batch is queued"
        assert await pull.start_next(session, OWNER, _only_the_ranker) is False
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.PARSER, "and handed to a parser by day"
        await session.commit()
    assert len(await _jobs()) == 1
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(SearchRun)) == 0, "no search of its own by day"


async def test_a_deck_role_marked_for_a_fresh_read_is_queued_before_more_rank_batches(agent, monkeypatch):
    """Found when ranking batches (one for each pull) kept the deck's research from ever starting."""
    from app.jobsearch.pool import pull
    from app.jobsearch.research import analysis

    await _profile()
    deck = await _role("deck", status=stages.SECOND_IMPRESSION, company="Initech")
    await _role("unranked", status=stages.FIRST_IMPRESSION)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, deck)
        role.sourced_at, role.description, role.description_at = NIGHT, TEXT, NIGHT
        role.analysis_state, role.analysis = analysis.DONE, {analysis.REANALYSE: True}
        await session.commit()
    async with db.session_factory()() as session:
        assert await pull.start_next(session, OWNER, _anything) is True
        await session.commit()
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, deck)).analysis_state == analysis.WAITING, "the deck read comes first"
    assert await _jobs() == [], "the rank batch waits for the next pull"


async def test_the_pool_queues_nothing_for_roles_without_a_copy_or_an_owner_without_a_profile(agent):
    from app.jobsearch.pool import pull

    await _role("no-copy", text=None, status=stages.CLEANSED_DATA)
    async with db.session_factory()() as session:
        assert await pull.start_next(session, OWNER, _only_the_ranker) is False
        await session.commit()
    await _role("a", status=stages.CLEANSED_DATA)  # made between the two asks, in a session of its own
    async with db.session_factory()() as session:
        assert await pull.start_next(session, OWNER, _only_the_ranker) is False, "no target role or gate is saved"
        await session.commit()
    assert await _jobs() == []


# --- the one-way rule -----------------------------------------------------------------------------------------------


def test_the_ranker_reads_postings_only_through_the_cleansed_module_never_the_raw_model():
    here = Path(rank.__file__).parent.parent
    for name in ("research/rank.py", "postings/cleansed.py"):
        tree = ast.parse((here / name).read_text(encoding="utf-8"))
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not {"RawPosting", "raw_copies"} & imported, f"{name} must not read the raw copies"


async def test_a_role_whose_posting_has_no_real_text_is_not_ranked_until_it_has_some(js):
    """Found when a rank batch said it scored on the title alone: the cleansed copies of board listings are often a few
    header lines (19,457 of 37,602 under 300 characters). Such a role waits in Cleansed Data; once the description is
    read and cleansed again it is ranked, and one already ranked on a title alone is ranked again."""
    short = await _role("short", text="Programs Lead. Remote.", status=stages.CLEANSED_DATA)
    full = await _role("full", status=stages.CLEANSED_DATA)
    async with db.session_factory()() as session:
        await _profile()
        wanted = [r for r, _ in await rank.unranked(session, OWNER, 1)]
        assert wanted == [full], "the title alone is not ranked"
        posting = await session.scalar(select(JobPosting).where(JobPosting.ext_id == "short"))
        posting.text = TEXT
        # the text lands
        await raw_copies._keep_report(session, posting, TEXT, reader=f"adapter:{EXAMPLE_SYSTEM}", text=TEXT)
        await session.flush()
        assert {r for r, _ in await rank.unranked(session, OWNER, 1)} == {short, full}
