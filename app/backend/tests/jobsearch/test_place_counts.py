"""A count of places never takes the place of the towns a posting named (docs/PRD.md 46.7). A job system's nightly list
gives only "2 Locations" after its page named the towns; the cleansed copy folded the later count over the towns, and a
role born from the list held only the count, so the work location gate had no town to judge and passed it. The fold
keeps the towns, the heal makes the copies again from the kept rows, and the re-gate gives every open role the towns its
postings name before it judges. Made-up towns; no network."""

import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

from sqlalchemy import func, select, update

from app import db
from app.jobsearch import cli, stages
from app.jobsearch.gating import regate, stated_place
from app.jobsearch.models import CleansedPosting, Opportunity, RawPosting
from app.jobsearch.postings import raw_copies
from app.models.work import TrailEvent
from app.settings import settings
from tests.jobsearch.work_queue.support import make_employer, make_posting, make_role
from tests.support import OWNER, load_profile

NIGHT = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)
TOWNS = "Town A; Town B"
IN_TOWN_C = {"in_person": {"on": True, "locations": ["Town C"]}}


# --- the fold -------------------------------------------------------------------------------------------------------


def test_a_later_count_does_not_replace_the_towns_an_earlier_row_named():
    parsed = {"location": TOWNS}
    raw_copies._fold(parsed, {"location": "2 Locations", "title": "Lead"})
    assert parsed == {"location": TOWNS, "title": "Lead"}, "the other keys still fold"


def test_a_later_count_larger_than_the_towns_named_still_replaces_them():
    parsed = {"location": "Town A"}
    raw_copies._fold(parsed, {"location": "5 Locations"})
    assert parsed["location"] == "5 Locations"


def test_a_later_list_of_real_towns_replaces_the_earlier_one():
    parsed = {"location": TOWNS}
    raw_copies._fold(parsed, {"location": "Town C"})
    assert parsed["location"] == "Town C"


def test_a_nightly_item_that_gives_only_the_count_again_changes_nothing_the_cleaner_reads():
    item = raw_copies.Held(uuid.uuid4(), raw_copies.ITEM, "a", {"title": "Lead", "location": "2 Locations"})
    detail = raw_copies.Held(uuid.uuid4(), raw_copies.DETAIL, "b", {"location": TOWNS})
    newest = raw_copies.Held(uuid.uuid4(), raw_copies.ITEM, "c", {"title": "Lead", "location": "2 Locations"})
    rows = [item, detail, newest]
    assert raw_copies.cleaner_inputs(raw_copies._use(rows))["location"] == TOWNS
    assert raw_copies.unchanged(rows, {"title": "Lead", "location": "2 Locations"}, {newest.id}) is True


# --- the heal -------------------------------------------------------------------------------------------------------


async def _posting_with_rows(session, *, detail_places: str | None):
    """A posting whose item row says "2 Locations", its detail row (if any) says `detail_places`, and whose current copy
    is made count-only, as the nightly read leaves some of them."""
    employer = make_employer()
    session.add(employer)
    await session.flush()
    posting = make_posting(employer_id=employer.id, location="2 Locations", url="https://jobs.example.test/1")
    session.add(posting)
    await session.flush()
    await raw_copies._keep_report(session, posting, {"id": 1, "n": 1}, reader="browser", text="Lead the team.",
                                 kind=raw_copies.ITEM, now=NIGHT)
    if detail_places:
        posting.location = detail_places
        await raw_copies._keep_report(session, posting, {"id": 1, "page": 1}, reader="browser", text="Lead the team.",
                                     kind=raw_copies.DETAIL, now=NIGHT)
    posting.location = "2 Locations"
    await session.refresh(posting)  # the copy is pointed at by a statement: the row is read again for it
    await session.execute(update(CleansedPosting).where(CleansedPosting.id == posting.current_cleansed_id)
                          .values(locations=["2 Locations"]))
    await session.flush()
    return posting


async def _current_places(session, posting) -> list:
    await session.refresh(posting)
    return list(await session.scalar(select(CleansedPosting.locations).where(
        CleansedPosting.id == posting.current_cleansed_id)) or [])


async def test_the_heal_puts_the_towns_back_on_a_count_only_copy_and_a_second_run_changes_nothing(js):
    async with db.session_factory()() as session:
        posting = await _posting_with_rows(session, detail_places=TOWNS)
        raw_before = await session.scalar(select(func.count()).select_from(RawPosting))
        assert await _current_places(session, posting) == ["2 Locations"]
        counts = await raw_copies.heal_counted_places(session, OWNER, NIGHT)
        assert counts == {"looked_at": 1, "rebuilt": 1}
        assert await _current_places(session, posting) == ["Town A", "Town B"]
        assert await session.scalar(select(func.count()).select_from(RawPosting)) == raw_before, "no raw row touched"
        assert await raw_copies.heal_counted_places(session, OWNER, NIGHT) == {"looked_at": 0, "rebuilt": 0}
        assert await _current_places(session, posting) == ["Town A", "Town B"]


async def test_the_heal_leaves_a_count_only_copy_alone_when_no_raw_row_names_a_town(js):
    async with db.session_factory()() as session:
        posting = await _posting_with_rows(session, detail_places=None)
        before = posting.current_cleansed_id
        assert await raw_copies.heal_counted_places(session, OWNER, NIGHT) == {"looked_at": 1, "rebuilt": 0}
        await session.refresh(posting)
        assert posting.current_cleansed_id == before


async def test_the_heal_command_runs_the_heal_and_prints_counts_only(js, monkeypatch, capsys):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await _posting_with_rows(session, detail_places=TOWNS)
        await session.commit()
    capsys.readouterr()

    assert await cli._heal_counted_places(SimpleNamespace()) == 0
    assert capsys.readouterr().out == ("1 current copies state only a count of places: 1 made again with the towns "
                                       "the postings named\n")
    assert await cli._heal_counted_places(SimpleNamespace()) == 0
    assert capsys.readouterr().out == ("0 current copies state only a count of places: 0 made again with the towns "
                                       "the postings named\n")


# --- the roles in every open stage ----------------------------------------------------------------------------------


async def _role_with_posting(session, status: str, held: str, posting_place: str, **more) -> Opportunity:
    role = make_role(status=status, location=held, **more)
    employer = make_employer()
    session.add_all([role, employer])
    await session.flush()
    session.add(make_posting(employer_id=employer.id, opportunity_id=role.id, location=posting_place))
    await session.flush()
    return role


async def _stated(session, role) -> int:
    return await session.scalar(select(func.count()).select_from(TrailEvent).where(
        TrailEvent.link_id == role.id, TrailEvent.event == "place_stated")) or 0


async def test_a_gated_out_role_with_a_count_takes_the_towns_its_posting_names_once(js):
    async with db.session_factory()() as session:
        role = await _role_with_posting(session, stages.GATED_OUT, "2 Locations", TOWNS, screened_out_by="compensation",
                                        screened_out_step="gate", screened_out_gate_id="compensation")
        placed = await stated_place.settle_open_places(session, OWNER)
        assert [r.id for r in placed] == [role.id] and role.location == TOWNS
        assert await _stated(session, role) == 1
        assert await stated_place.settle_open_places(session, OWNER) == [], "a second run writes nothing"
        assert await _stated(session, role) == 1


async def test_a_role_whose_posting_names_fewer_towns_than_its_count_is_left_as_it_is(js):
    async with db.session_factory()() as session:
        role = await _role_with_posting(session, stages.GATED_OUT, "5 Locations", TOWNS)
        assert await stated_place.settle_open_places(session, OWNER) == []
        assert role.location == "5 Locations" and await _stated(session, role) == 0


async def test_a_cleansed_data_role_with_a_count_takes_the_towns_its_posting_names(js):
    async with db.session_factory()() as session:
        role = await _role_with_posting(session, stages.CLEANSED_DATA, "2 Locations", TOWNS)
        placed = await stated_place.settle_open_places(session, OWNER)
        assert [r.id for r in placed] == [role.id] and role.location == TOWNS
        assert await _stated(session, role) == 1


async def test_the_regate_keeps_a_role_out_on_place_when_only_its_posting_names_the_disallowed_towns(js):
    """A Gated out role stopped on place, its place only a count, is not released by the re-gate on the
    ground that an unknown place passes. The posting names towns that fail, so it stays Gated out."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"other": [], "location": IN_TOWN_C})
        role = await _role_with_posting(
            session, stages.GATED_OUT, "2 Locations", TOWNS, move_type="onsite", found=date(2026, 11, 1),
            screened_out_by="location", screened_out_step="gate", screened_out_gate_id="location",
            screened_out_reason="Work must be in person in Town C.")
        await session.commit()
    async with db.session_factory()() as session:
        await regate.run(session, OWNER, cause="a test", today=date(2026, 11, 12))
        await session.commit()
        row = await session.get(Opportunity, role.id, populate_existing=True)
        assert row is not None and row.location == TOWNS
        assert (row.status, row.screened_out_by) == (stages.GATED_OUT, "location")
