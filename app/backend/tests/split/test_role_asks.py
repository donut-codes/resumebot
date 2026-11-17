"""The engine's asks for the role and employer routes: `role.research`, `role.settle`,
`role.read_first`, `role.fast_track` and `employer.refresh`, and the four services that can leave their follow-through
to them (`board.move`, `favorites.set_favorite`, `analysis.correct_class`, `employer_records.merge` and `split`). The
services the asks call are stood in for (their own tests are in tests/jobsearch); what is pinned here is what each ask
passes them, commits, knocks and answers. Every name here is made up."""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import records as employer_records
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import board, favorites
from app.jobsearch.research import analysis, settle
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import engine_asks
from app.models.work import TrailEvent
from tests.employer_builders import make_employer, make_role
from tests.split.writes_support import engine_told, knocked
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("the_clock_says_noon", "js")

NOON = datetime(2026, 11, 4, 12, tzinfo=UTC)


def _role(slug: str = "r1", **fields) -> Opportunity:
    return Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", fit=3, origin="scout",
                       match_key=f"co {slug}|role {slug}", found=date(2026, 11, 4), source_kind="board",
                       **{"status": stages.SECOND_IMPRESSION, **fields})


async def _add(slug: str = "r1", **fields) -> uuid.UUID:
    async with db.session_factory()() as session:
        role = _role(slug, **fields)
        session.add(role)
        await session.flush()
        role_id = role.id
        await session.commit()
    return role_id


async def _ask(name: str, args: dict) -> dict:
    """The ask run on the spot in a writer's session of its own, as `engine_calls.ask` runs it in the whole app."""
    async with db.session_factory()() as session:
        return await engine_asks.run_here(session, OWNER, name, args)


def test_the_five_asks_are_registered_under_the_names_the_routes_use():
    names = {"role.research", "role.settle", "role.read_first", "role.fast_track", "employer.refresh"}
    assert names <= set(engine_asks._asks)


# --- what the asks wake ---------------------------------------------------------------------------------------------


async def test_wake_for_knocks_the_analyst_for_new_research_the_writer_for_a_packet_and_asks_for_one_pass():
    role_id = uuid.UUID(int=1)
    with engine_told() as told:
        await engine_asks.wake_for(role_id, {"research": {"already": False}, "source": False, "rank": False,
                                             "packet": "p-1"})
    assert told.calls == [("knock", str(role_id), "analysis_asked"), ("knock", "p-1", "packet_asked"),
                          ("dispatch_now",)]


async def test_wake_for_wakes_nobody_for_research_that_was_asked_already_and_only_the_pass_for_a_sourcing():
    role_id = uuid.UUID(int=1)
    with engine_told() as told:
        await engine_asks.wake_for(role_id, {"research": {"already": True}, "source": False, "rank": False,
                                             "packet": None})
    assert told.calls == []
    with engine_told() as told:
        await engine_asks.wake_for(role_id, {"research": None, "source": True, "rank": False})
    assert told.calls == [("dispatch_now",)]


# --- role.research --------------------------------------------------------------------------------------------------


async def test_the_research_ask_asks_the_analyst_commits_and_knocks_once(monkeypatch):
    role_id = await _add()
    seen: list[tuple] = []

    async def ask(session, owner, rid, *, dry_run=False):
        seen.append((owner, rid))
        return {"role": str(rid), "state": "waiting", "already": False, "again": False, "said": "Queued."}

    monkeypatch.setattr(analysis, "ask", ask)
    with engine_told() as told:
        answer = await _ask("role.research", {"role_id": str(role_id)})
    assert answer == {"asked": {"role": str(role_id), "state": "waiting", "already": False, "again": False,
                                "said": "Queued."}}
    assert seen == [(OWNER, role_id)]
    assert knocked(told) == [("knock", str(role_id), "analysis_asked")]


async def test_the_research_ask_knocks_nobody_for_a_role_that_is_in_the_line_already(monkeypatch):
    role_id = await _add()

    async def ask(session, owner, rid, *, dry_run=False):
        return {"role": str(rid), "state": "waiting", "already": True, "again": False, "said": "Queued already."}

    monkeypatch.setattr(analysis, "ask", ask)
    with engine_told() as told:
        answer = await _ask("role.research", {"role_id": str(role_id)})
    assert answer["asked"]["already"] is True
    assert knocked(told) == []


async def test_the_research_ask_answers_a_refusal_with_its_status_and_words_and_knocks_nobody(monkeypatch):
    async def ask(session, owner, rid, *, dry_run=False):
        raise analysis.NoSuchRole("no such role")

    monkeypatch.setattr(analysis, "ask", ask)
    with engine_told() as told:
        answer = await _ask("role.research", {"role_id": str(uuid.uuid4())})
    assert answer == {"refused": {"status": 404, "message": "no such role"}}
    assert knocked(told) == []


# --- role.settle ----------------------------------------------------------------------------------------------------


async def test_the_settle_ask_settles_the_role_and_answers_the_line_the_gates_wrote(monkeypatch):
    role_id = await _add()
    seen: list[tuple] = []

    async def roles(session, rows, *, cause, keep_gated_out=False):
        rows = list(rows)
        seen.append(([r.id for r in rows], cause, keep_gated_out))
        return settle.Settled(1, 0, {rows[0].id: "A line the gates wrote."})

    monkeypatch.setattr(settle, "roles", roles)
    answer = await _ask("role.settle", {"role_id": str(role_id), "cause": "You did a thing"})
    assert answer == {"said": "A line the gates wrote."}
    assert seen == [([role_id], "You did a thing", False)]


async def test_the_settle_ask_for_a_role_that_is_gone_settles_nothing(monkeypatch):
    async def roles(*args, **kwargs):
        raise AssertionError("settled a role that is gone")

    monkeypatch.setattr(settle, "roles", roles)
    assert await _ask("role.settle", {"role_id": str(uuid.uuid4()), "cause": "x"}) == {"said": None}


# --- role.read_first ------------------------------------------------------------------------------------------------


async def test_the_read_first_ask_asks_the_reads_of_a_role_held_for_its_read_and_wakes_what_was_asked(monkeypatch):
    held = await _add(ready_asked_at=NOON)
    free = await _add("r2")
    seen: list[uuid.UUID] = []

    async def ask_reads(session, owner, role, done=None):
        seen.append(role.id)
        return {"moved": False, "source": False, "rank": True, "research": None, "packet": None}

    monkeypatch.setattr(favorites, "ask_reads", ask_reads)
    with engine_told() as told:
        answer = await _ask("role.read_first", {"role_id": str(held)})
        nothing = await _ask("role.read_first", {"role_id": str(free)})
        gone = await _ask("role.read_first", {"role_id": str(uuid.uuid4())})
    assert answer == {"done": {"moved": False, "source": False, "rank": True, "research": None, "packet": None}}
    assert nothing == {"done": None} and gone == {"done": None}
    assert seen == [held], "a role not held for its read, and a role that is gone, are asked nothing"
    assert knocked(told) == [("dispatch_now",)]


# --- role.fast_track ------------------------------------------------------------------------------------------------


async def test_the_fast_track_ask_fast_tracks_a_favorite_and_wakes_the_analyst_the_writer_and_the_pass(monkeypatch):
    star = await _add(favorited_at=NOON)
    plain = await _add("r2")
    seen: list[uuid.UUID] = []

    async def fast_track(session, owner, role):
        seen.append(role.id)
        return {"moved": True, "source": False, "rank": False, "research": {"already": False, "role": str(role.id)},
                "packet": "p-9"}

    monkeypatch.setattr(favorites, "fast_track", fast_track)
    with engine_told() as told:
        answer = await _ask("role.fast_track", {"role_id": str(star)})
        nothing = await _ask("role.fast_track", {"role_id": str(plain)})
    assert answer["done"]["moved"] is True and answer["done"]["packet"] == "p-9"
    assert nothing == {"done": None}
    assert seen == [star], "a role that is not starred is fast-tracked by nobody"
    assert knocked(told) == [("knock", str(star), "analysis_asked"), ("knock", "p-9", "packet_asked"),
                             ("dispatch_now",)]


# --- employer.refresh -----------------------------------------------------------------------------------------------


async def test_the_employer_refresh_ask_works_the_gates_and_scores_of_the_roles_out_again(monkeypatch):
    seen: list[tuple] = []

    async def after_a_change(session, owner, employer_ids, *, cause, keep_gated_out=False):
        seen.append((owner, list(employer_ids), cause, keep_gated_out))

    monkeypatch.setattr(employer_research, "after_a_change", after_a_change)
    employer_id = uuid.UUID(int=7)
    answer = await _ask("employer.refresh", {"employer_ids": [str(employer_id)], "cause": "A was merged with B",
                                             "keep_gated_out": True})
    assert answer == {"refreshed": True}
    assert seen == [(OWNER, [employer_id], "A was merged with B", True)]


# --- the services that leave their follow-through -------------------------------------------------------------------


async def test_a_move_to_ready_to_launch_that_defers_its_reads_marks_the_ask_and_asks_for_none(monkeypatch):
    role_id = await _add()
    calls: list[uuid.UUID] = []

    async def ask_reads(session, owner, role, done=None):
        calls.append(role.id)
        return {"moved": False, "source": False, "rank": True, "research": None, "packet": None}

    monkeypatch.setattr(favorites, "ask_reads", ask_reads)
    async with db.session_factory()() as session:
        done = await board.move(session, OWNER, role_id, stages.READY_TO_LAUNCH, defer_reads=True)
        await session.commit()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
    assert done == {"asked": True, "source": False, "rank": False, "research": None}
    assert calls == []
    assert role is not None and role.status == stages.SECOND_IMPRESSION and role.ready_asked_at is not None


async def test_a_move_to_ready_to_launch_that_does_not_defer_asks_the_reads_as_it_always_did(monkeypatch):
    role_id = await _add()
    calls: list[uuid.UUID] = []

    async def ask_reads(session, owner, role, done=None):
        calls.append(role.id)
        return {"moved": False, "source": False, "rank": True, "research": None, "packet": None}

    monkeypatch.setattr(favorites, "ask_reads", ask_reads)
    async with db.session_factory()() as session:
        done = await board.move(session, OWNER, role_id, stages.READY_TO_LAUNCH)
        await session.commit()
    assert calls == [role_id]
    assert done["asked"] is True and done["rank"] is True


async def test_a_move_back_hands_defer_reads_on_to_the_move(monkeypatch):
    role_id = await _add(status=stages.REJECTED)
    seen: list[bool] = []

    async def move(session, owner, rid, to, note=None, ended_reason=None, allowed=board.MOVES_TO, *,
                   defer_reads=False):
        seen.append(defer_reads)
        return {"asked": False, "source": False, "rank": False, "research": None}

    monkeypatch.setattr(board, "move", move)
    async with db.session_factory()() as session:
        await board.move_back(session, OWNER, role_id, defer_reads=True)
        await board.move_back(session, OWNER, role_id)
    assert seen == [True, False]


async def test_a_star_that_defers_its_fast_track_flips_the_flag_and_fast_tracks_nothing(monkeypatch):
    role_id = await _add()

    async def fast_track(session, owner, role):
        raise AssertionError("fast-tracked in the page")

    monkeypatch.setattr(favorites, "fast_track", fast_track)
    async with db.session_factory()() as session:
        role, done = await favorites.set_favorite(session, OWNER, role_id, True, defer_fast_track=True)
        starred = role.favorited_at is not None
        await session.commit()
    assert starred
    assert done == {"favorite": True, "changed": True, "moved": False, "source": False, "rank": False,
                    "research": None, "packet": None}


async def test_a_star_that_does_not_defer_fast_tracks_as_it_always_did(monkeypatch):
    role_id = await _add()

    async def fast_track(session, owner, role):
        return {"moved": True, "source": False, "rank": False, "research": None, "packet": None}

    monkeypatch.setattr(favorites, "fast_track", fast_track)
    async with db.session_factory()() as session:
        _, done = await favorites.set_favorite(session, OWNER, role_id, True)
        await session.commit()
    assert done["moved"] is True


async def test_a_corrected_class_that_defers_its_settle_is_saved_with_its_line_and_settles_nothing(monkeypatch):
    role_id = await _add(analysis_state=analysis.DONE,
                         analysis={"required": [{"item": "Ten years of testing", "class": "T"}]})

    async def roles(*args, **kwargs):
        raise AssertionError("settled in the page")

    monkeypatch.setattr(settle, "roles", roles)
    async with db.session_factory()() as session:
        shown = await analysis.correct_class(session, OWNER, role_id, 0, "P", "I did it", defer_settle=True)
        await session.commit()
    assert shown["required"][0]["class"] == "P" and shown["required"][0]["by"] == "owner"
    assert "said" not in shown
    async with db.session_factory()() as session:
        events = list(await session.scalars(select(TrailEvent.event).where(TrailEvent.link_id == role_id)))
    assert events.count("requirement_corrected") == 1
    assert analysis.CORRECTED_CAUSE == "You corrected a required item's class"


async def _two_employers() -> tuple[uuid.UUID, uuid.UUID]:
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech")
        away = await make_employer(session, "Globex")
        session.add(make_role(away, "Lead"))
        await session.flush()
        ids = keep.id, away.id
        await session.commit()
    return ids


async def test_a_merge_that_defers_its_refresh_hands_the_ask_back_and_works_nothing_out(monkeypatch):
    keep_id, away_id = await _two_employers()

    async def after_a_change(*args, **kwargs):
        raise AssertionError("refreshed in the page")

    monkeypatch.setattr(employer_research, "after_a_change", after_a_change)
    async with db.session_factory()() as session:
        done = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test", defer_refresh=True)
        name = done.employer.name
        await session.commit()
        again = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test", defer_refresh=True)
    assert done.merged is True
    assert done.refresh == {"employer_ids": [str(keep_id)], "cause": f"{name} was merged with another record of it",
                            "keep_gated_out": False}
    assert again.merged is False and again.refresh is None, "a merge asked again has nothing to refresh"


async def test_a_merge_that_does_not_defer_works_the_roles_out_as_it_always_did(monkeypatch):
    keep_id, away_id = await _two_employers()
    seen: list[tuple] = []

    async def after_a_change(session, owner, employer_ids, *, cause, keep_gated_out=False):
        seen.append((list(employer_ids), cause, keep_gated_out))

    monkeypatch.setattr(employer_research, "after_a_change", after_a_change)
    async with db.session_factory()() as session:
        done = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        name = done.employer.name
        await session.commit()
    assert seen == [([keep_id], f"{name} was merged with another record of it", False)]
    assert done.refresh is None


async def test_a_split_that_defers_its_refresh_hands_the_ask_back_and_a_split_that_does_not_works_it_out(monkeypatch):
    async with db.session_factory()() as session:
        source = await make_employer(session, "Initech")
        role = make_role(source, "Lead")
        other = make_role(source, "Analyst")
        session.add_all([role, other])
        await session.flush()
        source_id, role_id, other_id = source.id, role.id, other.id
        await session.commit()
    seen: list[tuple] = []

    async def after_a_change(session, owner, employer_ids, *, cause, keep_gated_out=False):
        seen.append((list(employer_ids), cause, keep_gated_out))

    monkeypatch.setattr(employer_research, "after_a_change", after_a_change)
    async with db.session_factory()() as session:
        done = await employer_records.split(session, OWNER, source_id, role_ids=[role_id], new_name="Fabrikam",
                                            why="a test", defer_refresh=True)
        target_id, target_name = done.employer.id, done.employer.name
        await session.commit()
    assert done.roles == 1 and seen == []
    assert done.refresh == {"employer_ids": [str(target_id)],
                            "cause": f"Roles were split out of Initech into {target_name}", "keep_gated_out": True}
    async with db.session_factory()() as session:
        plain = await employer_records.split(session, OWNER, source_id, role_ids=[other_id], new_name="Fabrikam",
                                             why="a test")
        await session.commit()
    assert plain.refresh is None
    assert seen == [([target_id], f"Roles were split out of Initech into {target_name}", True)]
