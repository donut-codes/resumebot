"""Fits none of my roles (docs/PRD.md 31.3): the roles the owner pursued with no target role and the postings found this
week whose role no target role claims, counted from the database; a role or posting marked fine as it is stays out of
the count; a draft target role made from one. Synthetic data only."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.jobs import fits_none
from app.jobsearch.main import app
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import FitsNoneOk, JobPosting, Opportunity
from app.settings import settings
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

NOW = datetime.now(UTC)
BUILDERS = PROFILE["criteria"]["target_roles"][0]
OPS = {"id": "ops", "name": "Operations", "titles": ["Operations Manager"],
       "description": "Keeps the organization running: facilities, vendors and the budget."}
TWO_ROLES = {**PROFILE["criteria"], "target_roles": [BUILDERS, OPS]}
DAY = timedelta(days=1)


@pytest.fixture
async def client(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _role(n: int, status: str, owner: str = OWNER, **more) -> Opportunity:
    """A role found `n` minutes ago (so a lower n is newer)."""
    return Opportunity(owner=owner, slug=f"co-{owner}-{n}", company=f"Company {n}", title=f"Role Title {n}",
                       status=status, fit=3, match_key=f"company {n}|role title {n}|{owner}", origin="scout",
                       created_at=NOW - timedelta(minutes=n), **more)


async def _scenario() -> dict[int, uuid.UUID]:
    """Roles 1, 2, 3, 4, 7, 9 and 10 moved forward; 5 (on deck), 6 (gated out) and 8 (gone quiet, never
    applied) did not.
    Roles 1, 4, 7 and 10 were found for no target role, so they are what fits none."""
    async with db.session_factory()() as session:
        rows = {
            1: _role(1, "applied"), 2: _role(2, "applied", target_role="builders"),
            3: _role(3, "ready_to_launch", target_role="ops"), 4: _role(4, "ready_to_launch"),
            5: _role(5, "second_impression"), 6: _role(6, "gated_out", screened_out_reason="the place"),
            7: _role(7, "applied"), 8: _role(8, "gone_quiet"),
            9: _role(9, "second_impression", packet_complete=True, target_role="builders"),
            10: _role(10, "active_discussion"),
        }
        session.add_all(rows.values())
        await session.commit()
        rows[7].status = "rejected"  # it reached Applied before it ended
        await session.commit()
        return {n: r.id for n, r in rows.items()}


FITS_NONE = {1, 4, 7, 10}


def _posting(employer, i: int, **more) -> JobPosting:
    """A posting first seen `i` hours ago (unless `first_seen` is given) that passed the filter, not yet a role."""
    title = f"Week Title {i}"
    fields = {"first_seen": NOW - timedelta(hours=i), "route": "roles",
              "text": f"The start of the posting for {title}. " + "More words. " * 40, **more}
    return JobPosting(
        owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=f"W-{i}", company=employer.name, title=title,
        match_key=match_key(employer.name, title), location="Remote", work_mode="remote",
        url=f"https://jobs.example.test/W-{i}", last_seen=NOW, filtered_at=NOW, links=[], **fields)


async def _found_posting(session, employer, i: int, **more) -> JobPosting:
    """A posting (see `_posting`) of a Found role that no target role claims: what this week lists. Added to the
    session."""
    role = _role(100 + i, "second_impression")
    session.add(role)
    await session.flush()
    posting = _posting(employer, i, opportunity_id=role.id, route=None, **more)
    session.add(posting)
    return posting


async def _week() -> dict[str, JobPosting]:
    """What this week holds, by letter. A, B and I fit none; the rest are each left out for a reason of their own."""
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        found, owned, gone, young, old = (_role(n, status, **more) for n, status, more in (
            (21, "second_impression", {}), (22, "second_impression", {"target_role": "ops"}), (23, "gated_out", {}),
            (24, "second_impression", {}), (25, "second_impression", {})))
        session.add_all([found, owned, gone, young, old])
        await session.flush()
        rows = {
            "A": _posting(employer, 24, opportunity_id=young.id, route=None),         # a Found role, no tab
            "B": _posting(employer, 72, opportunity_id=found.id, route=None),         # a Found role, no tab
            "C": _posting(employer, 25, opportunity_id=found.id, route=None, target_role="builders"),  # claimed
            "D": _posting(employer, 26, opportunity_id=found.id, route=None, first_seen=NOW - 8 * DAY),  # too old
            "E": _posting(employer, 27, filtered_by="not your kind of job: a sales job", route=None),  # stopped
            "F": _posting(employer, 28, opportunity_id=owned.id, route=None),        # its role has a tab
            "G": _posting(employer, 29, opportunity_id=gone.id, route=None),         # its role was gated out
            "H": _posting(employer, 30, route="titles"),                              # no role made of it yet
            "I": _posting(employer, 144, opportunity_id=old.id, route=None),          # a week old, still counts
            "K": _posting(employer, 31, route="reader"),                              # waits for a reader: no role
        }
        session.add_all(rows.values())
        await session.commit()
        return rows


async def _get(client, **params):
    res = await client.get("/api/v1/profile/fits-none", params=params)
    assert res.status_code == 200, res.text
    return res.json()


async def _counted_in_sql() -> tuple[int, int]:
    """The two counts as the database gives them, written out apart from the code that makes them."""
    async with db.session_factory()() as session:
        pursued = await session.scalar(text(
            "SELECT count(*) FROM js_opportunities WHERE owner = 'tester' AND target_role IS NULL"
            " AND status <> 'gated_out' AND ("
            " status IN ('ready_to_launch', 'applied', 'active_discussion') OR packet_complete"
            " OR (status IN ('rejected', 'gone_quiet', 'withdrew') AND id IN ("
            "  SELECT role_id FROM js_stage_moves WHERE owner = 'tester'"
            "  AND to_status IN ('applied', 'active_discussion')))"
            " OR (status IN ('rejected', 'gone_quiet', 'withdrew') AND ended_reason = 'they_said_no'))"
            " AND id NOT IN (SELECT subject_id FROM js_fits_none_ok)"))
        week = await session.scalar(text(
            "SELECT count(*) FROM js_postings p WHERE p.owner = 'tester'"
            " AND p.first_seen >= datetime('now', '-7 days') AND p.target_role IS NULL"
            " AND EXISTS (SELECT 1 FROM js_opportunities o WHERE o.id = p.opportunity_id"
            "  AND o.status <> 'gated_out' AND o.target_role IS NULL)"
            " AND p.id NOT IN (SELECT subject_id FROM js_fits_none_ok)"
            " AND (p.opportunity_id IS NULL OR p.opportunity_id NOT IN (SELECT subject_id FROM js_fits_none_ok))"))
        return pursued, week


# --- the roles the owner pursued ----------


async def test_the_pursued_count_is_the_roles_moved_forward_with_no_target_role_counted_in_the_database(client):
    ids = await _scenario()
    got = await _get(client)
    assert set(got) == {"pursued", "this_week"} and set(got["pursued"]) == {"count", "items"}
    pursued_sql, _week_sql = await _counted_in_sql()
    assert got["pursued"]["count"] == pursued_sql == len(FITS_NONE) == 4
    assert [i["id"] for i in got["pursued"]["items"]] == [str(ids[n]) for n in sorted(FITS_NONE)], "newest first"
    assert got["this_week"] == {"count": 0, "items": []}


async def test_an_item_says_what_the_role_is_where_it_stands_and_when_it_was_found(client):
    ids = await _scenario()
    items = {i["title"]: i for i in (await _get(client))["pursued"]["items"]}
    first = items["Role Title 1"]
    assert set(first) == {"id", "kind", "title", "company", "stage", "stage_label", "found_at"}
    assert first["id"] == str(ids[1]) and first["kind"] == "role" and first["company"] == "Company 1"
    assert (first["stage"], first["stage_label"]) == ("applied", "Applied")
    assert datetime.fromisoformat(first["found_at"]) == NOW - timedelta(minutes=1)
    assert (items["Role Title 4"]["stage"], items["Role Title 4"]["stage_label"]) == (
        "ready_to_launch", "Ready to Launch")
    assert items["Role Title 7"]["stage_label"] == "Rejected", "it ended, but only after it was applied for"
    assert items["Role Title 10"]["stage_label"] == "Active Discussion"


async def test_a_role_that_was_not_pursued_never_counts_whatever_its_target_role(client):
    async with db.session_factory()() as session:
        session.add_all([_role(1, "second_impression"), _role(2, "gated_out"),
                         _role(3, "withdrew", ended_reason="i_passed"), _role(4, "gone_quiet"),
                         _role(5, "withdrew"), _role(6, "second_impression", target_role=None),
                         _role(7, "applied", owner="someone-else")])
        await session.commit()
    got = await _get(client)
    assert got["pursued"] == {"count": 0, "items": []}
    assert (await _counted_in_sql())[0] == 0


async def test_a_role_an_employer_turned_down_was_pursued_with_no_history_but_one_the_owner_passed_on_was_not(client):
    """The classifier counts an employer's no as an application the owner made (docs/PRD.md 31.7), whatever history an
    imported role lacks; the count here is the same set."""
    async with db.session_factory()() as session:
        session.add_all([
            _role(1, "rejected", ended_reason="they_said_no"), _role(2, "withdrew", ended_reason="i_passed"),
            _role(3, "gone_quiet", ended_reason="posting_gone"), _role(4, "rejected", ended_reason="unknown"),
        ])
        await session.commit()
    got = await _get(client)
    assert [i["title"] for i in got["pursued"]["items"]] == ["Role Title 1"]
    assert got["pursued"]["count"] == (await _counted_in_sql())[0] == 1


async def test_a_role_that_has_a_target_role_is_not_a_miss_even_when_that_role_is_gone_from_the_tabs(client):
    async with db.session_factory()() as session:
        session.add_all([_role(1, "applied", target_role="builders"), _role(2, "applied", target_role="deleted-tab")])
        await session.commit()
    assert (await _get(client))["pursued"]["count"] == 0, "what it was found for is recorded, whatever became of it"


async def test_the_pursued_list_is_paged_and_the_count_is_always_whole(client):
    async with db.session_factory()() as session:
        session.add_all([_role(n, "applied") for n in range(1, 26)])
        await session.commit()
    first = await _get(client)
    assert first["pursued"]["count"] == 25 and len(first["pursued"]["items"]) == fits_none.PAGE == 10
    assert first["pursued"]["items"][0]["title"] == "Role Title 1"
    last = await _get(client, pursued_offset=20, limit=10)
    assert last["pursued"]["count"] == 25 and [i["title"] for i in last["pursued"]["items"]] == [
        f"Role Title {n}" for n in range(21, 26)]
    assert len((await _get(client, limit=200))["pursued"]["items"]) == 25
    for bad in ({"limit": 0}, {"limit": 201}, {"pursued_offset": -1}, {"this_week_offset": -1}):
        assert (await client.get("/api/v1/profile/fits-none", params=bad)).status_code == 422, bad


# --- the postings found this week ----------


async def test_this_week_is_the_postings_that_reached_found_and_that_no_target_role_claims(client):
    """A posting waiting for a reader (route `reader`) is not listed any more: no role is made of it, and the app no
    longer sends one there (docs/PRD.md 31.3, 39.14)."""
    rows = await _week()
    got = (await _get(client))["this_week"]
    _pursued_sql, week_sql = await _counted_in_sql()
    assert got["count"] == week_sql == 3
    assert [i["title"] for i in got["items"]] == ["Week Title 24", "Week Title 72", "Week Title 144"], "newest first"
    by_title = {i["title"]: i for i in got["items"]}
    a, b = by_title["Week Title 24"], by_title["Week Title 72"]
    assert set(a) == {"id", "kind", "title", "company", "stage", "stage_label", "found_at"}
    assert a["id"] == str(rows["A"].id) and a["kind"] == "posting" and a["company"] == "Fabrikam"
    assert (a["stage"], a["stage_label"]) == ("second_impression", "Second Impression"), "its role's stage"
    assert (b["stage"], b["stage_label"]) == ("second_impression", "Second Impression"), "its role's stage"
    assert datetime.fromisoformat(a["found_at"]) == rows["A"].first_seen
    left_out = {rows[k].title for k in "CDEFGHK"}
    assert not left_out & {i["title"] for i in got["items"]}


async def test_the_week_is_the_last_seven_days(client):
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        await _found_posting(session, employer, 1, first_seen=NOW - 6 * DAY - timedelta(hours=23))
        await _found_posting(session, employer, 2, first_seen=NOW - 7 * DAY - timedelta(hours=1))
        await session.commit()
    got = (await _get(client))["this_week"]
    assert [i["title"] for i in got["items"]] == ["Week Title 1"]


async def test_a_posting_counts_until_a_target_role_claims_it(client):
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        await _found_posting(session, employer, 5)
        await session.commit()
    assert (await _get(client))["this_week"]["count"] == 1
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting))).one()
        posting.target_role = "ops"
        await session.commit()
    assert (await _get(client))["this_week"]["count"] == 0


async def test_this_weeks_list_is_paged(client):
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        for n in range(1, 16):
            await _found_posting(session, employer, n)
        await session.commit()
    first = await _get(client, limit=5)
    assert first["this_week"]["count"] == 15 and len(first["this_week"]["items"]) == 5
    rest = await _get(client, this_week_offset=10, limit=10)
    assert [i["title"] for i in rest["this_week"]["items"]] == [f"Week Title {n}" for n in range(11, 16)]
    assert rest["pursued"]["items"] == []


# --- fine as it is ----------


async def test_a_role_marked_fine_is_stored_and_left_out_of_the_count_and_the_list(client):
    ids = await _scenario()
    done = await client.post(f"/api/v1/profile/fits-none/{ids[4]}/dismiss")
    assert done.status_code == 200
    assert done.json() == {"id": str(ids[4]), "kind": "role", "dismissed": True, "pursued": 3, "this_week": 0}
    got = (await _get(client))["pursued"]
    assert got["count"] == 3 == (await _counted_in_sql())[0]
    assert [i["title"] for i in got["items"]] == ["Role Title 1", "Role Title 7", "Role Title 10"]
    async with db.session_factory()() as session:
        marks = (await session.scalars(select(FitsNoneOk))).all()
        assert [(m.owner, m.subject_id, m.kind) for m in marks] == [(OWNER, ids[4], "role")], "stored"
        role = await session.get(Opportunity, ids[4])
        assert role.target_role is None and role.status == "ready_to_launch", "the role itself is untouched"


async def test_marking_a_role_fine_twice_changes_nothing_and_each_role_is_marked_on_its_own(client):
    ids = await _scenario()
    for _ in range(2):
        again = await client.post(f"/api/v1/profile/fits-none/{ids[1]}/dismiss")
        assert again.status_code == 200 and again.json()["pursued"] == 3
    other = await client.post(f"/api/v1/profile/fits-none/{ids[10]}/dismiss")
    assert other.json()["pursued"] == 2
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(FitsNoneOk)) == 2


async def test_a_role_or_posting_that_is_not_the_owners_cannot_be_marked_fine(client):
    async with db.session_factory()() as session:
        stranger = _role(1, "applied", owner="someone-else")
        session.add(stranger)
        await session.commit()
    for key in (str(stranger.id), str(uuid.uuid4()), "not-an-id"):
        res = await client.post(f"/api/v1/profile/fits-none/{key}/dismiss")
        assert res.status_code == 404 and res.json()["detail"] == "no role has that id", key
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(FitsNoneOk)) == 0


async def test_a_posting_marked_fine_leaves_this_weeks_count_and_so_does_a_posting_whose_role_was_marked(client):
    rows = await _week()
    done = await client.post(f"/api/v1/profile/fits-none/{rows['I'].id}/dismiss")
    assert done.json() == {"id": str(rows["I"].id), "kind": "posting", "dismissed": True, "pursued": 0, "this_week": 2}
    assert (await _counted_in_sql())[1] == 2
    assert [i["title"] for i in (await _get(client))["this_week"]["items"]] == ["Week Title 24", "Week Title 72"]
    role_id = rows["B"].opportunity_id
    again = await client.post(f"/api/v1/profile/fits-none/{role_id}/dismiss")
    assert again.json()["kind"] == "role" and again.json()["this_week"] == 1, "its posting is the same thing"
    assert (await _counted_in_sql())[1] == 1
    assert [i["title"] for i in (await _get(client))["this_week"]["items"]] == ["Week Title 24"]


# --- make this a role ----------


async def _draft(client, key):
    return await client.post(f"/api/v1/profile/roles/from-role/{key}")


async def test_a_draft_role_takes_its_name_example_title_and_description_from_the_role_and_saves_nothing(client):
    ids = await _scenario()
    sentence = "The work is to run the programs and keep the books in order. "
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids[1])
        role.title, role.description = "Director of Community Programs", sentence * 40
        await session.commit()
    versions = (await client.get("/api/v1/profile")).json()["versions"]
    res = await _draft(client, ids[1])
    assert res.status_code == 200
    draft = res.json()
    assert set(draft) == {"id", "name", "titles", "description", "responsibilities", "qualifications", "preferred",
                          "compensation"}
    assert draft["name"] == "Director of Community Programs" and draft["titles"] == ["Director of Community Programs"]
    assert draft["id"] == "director-of-community-programs"
    assert 0 < len(draft["description"]) <= 1000 and draft["description"].endswith("order."), "a whole sentence"
    assert draft["description"].startswith("The work is to run the programs") and "  " not in draft["description"]
    assert [draft[k] for k in ("responsibilities", "qualifications", "preferred", "compensation")] == [""] * 4
    after = (await client.get("/api/v1/profile")).json()
    assert after["versions"] == versions, "nothing was saved"
    assert [r["id"] for r in after["criteria"]["target_roles"]] == ["builders", "ops"]


async def test_a_draft_takes_its_text_from_the_posting_the_role_was_found_in_when_the_role_has_none(client):
    ids = await _scenario()
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(_posting(employer, 3, opportunity_id=ids[4], text="Plans events. Books venues. Runs the day."))
        await session.commit()
    draft = (await _draft(client, ids[4])).json()
    assert draft["name"] == "Role Title 4" and draft["description"] == "Plans events. Books venues. Runs the day."
    empty = (await _draft(client, ids[10])).json()
    assert empty["description"] == "" and empty["titles"] == ["Role Title 10"], "no text: only the title"


async def test_a_draft_id_is_one_no_saved_target_role_has_and_a_long_title_is_the_name_whole(client):
    ids = await _scenario()
    async with db.session_factory()() as session:
        one, two = await session.get(Opportunity, ids[1]), await session.get(Opportunity, ids[4])
        one.title = "Builders"  # the slug of a role the owner has already
        two.title = "A very long title " * 12
        await session.commit()
    assert (await _draft(client, ids[1])).json()["id"] == "builders-2"
    long = (await _draft(client, ids[4])).json()
    assert long["name"] == " ".join(["A very long title"] * 12), "a name has no limit on its length"
    assert long["titles"] == [long["name"]] and long["id"].startswith("a-very-long")


async def test_a_draft_can_be_made_from_a_posting_this_week_lists(client):
    rows = await _week()
    draft = (await _draft(client, rows["A"].id)).json()
    assert draft["name"] == "Week Title 24" and draft["titles"] == ["Week Title 24"]
    assert draft["description"].startswith("The start of the posting for Week Title 24.")
    assert draft["id"] == "week-title-24"


async def test_a_draft_for_something_that_is_not_the_owners_is_a_404(client):
    async with db.session_factory()() as session:
        stranger = _role(1, "applied", owner="someone-else")
        session.add(stranger)
        await session.commit()
    for key in (str(stranger.id), str(uuid.uuid4()), "not-an-id"):
        res = await _draft(client, key)
        assert res.status_code == 404 and res.json()["detail"] == "no role has that id", key
