"""The board: every role in its column, best or newest first, and moving a role between columns (written to the trail).
Synthetic roles only."""

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update

from app import db
from app.jobsearch import ats, stages
from app.jobsearch.models import ADDED_LINK, JobPosting, Opportunity, StageMove
from app.jobsearch.pipeline import board
from app.jobsearch.pipeline import row as role_row
from app.models.work import TrailEvent
from tests.builders import counted, rank_roles
from tests.support import OWNER, fake_go_window


def role(slug, fit=3, days_old=1, **extra):
    today = date.today()
    return Opportunity(
        owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", fit=fit, origin="scout",
        match_key=f"co {slug}|role {slug}", posted=today - timedelta(days=days_old),
        found=today - timedelta(days=days_old),
        **{"status": "second_impression", "source_kind": "board", **extra})


async def _add(*roles):
    async with db.session_factory()() as session:
        session.add_all(roles)
        await session.commit()


async def _rank(by_slug: dict[str, int]):
    """Give the roles (by slug) their first-impression rank."""
    async with db.session_factory()() as session:
        ids = {r.slug: r.id for r in (await session.scalars(select(Opportunity))).all()}
        await rank_roles(session, {ids[slug]: fit for slug, fit in by_slug.items()})
        await session.commit()


async def _id_of(slug):
    async with db.session_factory()() as session:
        return (await session.scalars(select(Opportunity.id).where(Opportunity.slug == slug))).one()


def _waiting(n, **extra):
    """A job the owner added by its link that nothing has named yet: the only posting that waits in Added links
    (a named posting that passed the filter becomes a role by `role_from_posting.make_roles`)."""
    now = datetime.now(UTC)
    return JobPosting(
        owner=OWNER, employer_id=uuid.uuid4(), ats=ats.OWNER, ext_id=f"w{n}", company=ADDED_LINK,
        title=f"Posting {n}", match_key=f"firm {n}|posting {n}", location="Remote", work_mode="remote",
        url=f"https://jobs.example.test/{n}",
        first_seen=now - timedelta(hours=n), last_seen=now, filtered_at=now, links=[], route="roles", **extra)


async def test_added_links_comes_before_followup_needed_with_the_waiting_postings_newest_first(client):
    today = date.today()
    await _add(*[_waiting(n) for n in range(1, 56)],
               _waiting(100, posted=today - timedelta(days=30)),  # an old posting date puts it last
               _waiting(101, closed_at=datetime.now(UTC)),  # closed: not waiting
               role("found-one"))
    body = (await client.get("/api/v1/board")).json()
    assert [c["key"] for c in body["columns"]][:2] == ["added_links", "followup_needed"]
    col = body["columns"][0]
    assert col["label"] == "Added links" and col["count"] == 56
    assert len(col["cards"]) == 50
    assert [c["title"] for c in col["cards"][:3]] == ["Posting 1", "Posting 2", "Posting 3"]
    assert "Posting 100" not in [c["title"] for c in col["cards"]]
    first = col["cards"][0]
    assert first["url"] == "https://jobs.example.test/1" and first["work"]["words"] == "Remote"
    assert first["company"] == ADDED_LINK and first["age"]["from"] == "found" and first["pay"] == "Not posted"
    more = (await client.get("/api/v1/board?column=added_links&limit=60")).json()["columns"]
    assert [c["key"] for c in more] == ["added_links"] and len(more[0]["cards"]) == 56
    assert more[0]["cards"][-1]["title"] == "Posting 100"


async def test_limit_sizes_added_links_on_the_whole_board_too(client):
    await _add(*[_waiting(n) for n in range(1, 6)], role("found-one"))
    whole = (await client.get("/api/v1/board", params={"limit": 2})).json()["columns"]
    assert [c["key"] for c in whole][:2] == ["added_links", "followup_needed"]
    assert (whole[0]["count"], len(whole[0]["cards"])) == (5, 2), "the board's Added links holds `limit` cards"
    alone = (await client.get("/api/v1/board", params={"column": "added_links", "limit": 3})).json()["columns"]
    assert [c["key"] for c in alone] == ["added_links"] and len(alone[0]["cards"]) == 3


async def test_the_favorites_are_no_column_of_the_board(client):
    await _add(role("starred", favorited_at=datetime.now(UTC)))
    assert (await client.get("/api/v1/board", params={"column": "favorites"})).status_code == 404
    assert (await client.get("/api/v1/favorites")).json()["count"] == 1, "they have a route of their own"


async def test_an_added_link_that_became_a_role_leaves_added_links(client):
    await _add(_waiting(1), _waiting(2))
    async with db.session_factory()() as session:
        posting = (await session.scalars(select(JobPosting).where(JobPosting.ext_id == "w1"))).one()
        made = role("match")
        session.add(made)
        await session.flush()
        posting.opportunity_id = made.id  # the link was read and the posting became a role
        await session.commit()
    cols = {c["key"]: c for c in (await client.get("/api/v1/board")).json()["columns"]}
    assert cols["added_links"]["count"] == 1
    assert [c["title"] for c in cols["added_links"]["cards"]] == ["Posting 2"]
    assert [c["title"] for c in cols["second_impression"]["cards"]] == ["Role match"]


async def _stage_counts(client):
    return {c["key"]: [x["title"] for x in c["cards"]] for c in (await client.get("/api/v1/board")).json()["columns"]}


async def test_a_role_is_in_the_column_of_its_status_and_changes_column_when_its_status_changes(client):
    await _add(*[role(s, status=s) for s in stages.STAGES if s not in stages.CLOSED], _waiting(1))
    cols = await _stage_counts(client)
    assert cols["added_links"] == ["Posting 1"]
    for status in (*stages.PRE_READY, stages.READY_TO_LAUNCH, stages.APPLIED, stages.ACTIVE_DISCUSSION):
        assert cols[status] == [f"Role {status}"], status
    assert cols["closed"] == []
    async with db.session_factory()() as session:
        mine = {r.slug: r for r in (await session.scalars(select(Opportunity))).all()}
        mine["cleansed_data"].status = "first_impression"  # its rank came in
        mine["landing_zone"].status = "gated_out"
        await session.commit()
    cols = await _stage_counts(client)
    assert cols["cleansed_data"] == [] and cols["landing_zone"] == []
    assert sorted(cols["first_impression"]) == ["Role cleansed_data", "Role first_impression"]
    assert cols["closed"] == ["Role landing_zone"]


async def test_the_moves_a_role_is_offered_are_the_statuses_a_person_decides(client):
    await _add(role("m"))
    body = (await client.get(f"/api/v1/roles/{await _id_of('m')}")).json()
    assert [m["status"] for m in body["moves_to"]] == list(board.MOVES_TO)
    assert list(board.MOVES_TO) == ["first_impression", "second_impression", "ready_to_launch", "applied",
                                    "active_discussion", "rejected", "gone_quiet", "withdrew"]
    assert {m["status"]: m["label"] for m in body["moves_to"]}["ready_to_launch"] == "Ready to Launch"


async def test_go_refuses_a_request_that_is_not_from_this_machine(client, monkeypatch):
    from app.jobsearch.browser import go as apply_go
    from app.jobsearch.browser import go_status

    opened = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    monkeypatch.setattr(go_status, "THIS_MACHINE", ("10.0.0.9",))
    reply = await client.post("/api/v1/apply/go")
    assert reply.status_code == 403 and opened == []


async def test_each_column_holds_its_roles_in_its_order(client):
    """The order itself is tested in test_board_order.py; this is each column's place in the board."""
    await _add(
        role("low", fit=2, days_old=5), role("high", fit=6), role("old", fit=6, days_old=40),
        role("a1", status="applied", days_old=5), role("a2", status="applied", days_old=1),
        role("r", status="rejected"), role("d", status="withdrew"),
        role("s", status="gated_out", screened_out_reason="pay under the floor"),
    )
    await _rank({"low": 20, "high": 60, "old": 60})
    async with db.session_factory()() as session:
        ids = {r.slug: r.id for r in (await session.scalars(select(Opportunity))).all()}
        for slug, days in (("a2", 1), ("a1", 5)):  # when each last changed
            await session.execute(update(StageMove).where(StageMove.role_id == ids[slug]).values(
                at=datetime.now(UTC) - timedelta(days=days)))
        await session.commit()
    body = (await client.get("/api/v1/board")).json()
    cols = {c["key"]: c for c in body["columns"]}
    assert [c["key"] for c in body["columns"]] == [
        "added_links", "followup_needed", "landing_zone", "cleansed_data", "first_impression", "second_impression",
        "ready_to_launch", "applied", "active_discussion", "closed"]
    assert [c["title"] for c in cols["second_impression"]["cards"]] == ["Role high", "Role old", "Role low"], (
        "the better rank first; the old one's 21 points off for its stale posting still leave it above the low rank")
    assert [c["title"] for c in cols["applied"]["cards"]] == ["Role a2", "Role a1"], "the younger posting first"
    assert cols["closed"]["count"] == 3
    assert {c["status_label"] for c in cols["closed"]["cards"]} == {"Rejected", "Withdrew", "Gated out"}
    gated = next(c for c in cols["closed"]["cards"] if c["status"] == "gated_out")
    assert gated["reason"] == "pay under the floor"
    # Old: "Analyst reads it next: it is queued as soon as a session is free", then "Its first hours add 20". New
    # (docs/PRD.md 24.18): a role waiting for the analyst is the normal run, and the row says nothing of it.
    assert cols["second_impression"]["cards"][0]["next"] is None, "read on need: no mission, no time, nothing said"


async def test_a_column_shows_a_page_and_can_be_read_whole(client, monkeypatch):
    monkeypatch.setattr(role_row, "SHOWN", 2)
    await _add(*[role(f"c{n}", status="rejected", days_old=n) for n in range(5)])
    closed = next(c for c in (await client.get("/api/v1/board")).json()["columns"] if c["key"] == "closed")
    assert closed["count"] == 5 and len(closed["cards"]) == 2
    whole = (await client.get("/api/v1/board", params={"column": "closed"})).json()
    assert [c["key"] for c in whole["columns"]] == ["closed"] and len(whole["columns"][0]["cards"]) == 5
    assert (await client.get("/api/v1/board", params={"column": "wishes"})).status_code == 404


async def test_a_role_filter_shows_only_the_roles_found_for_that_target_role_counts_included(client, monkeypatch):
    monkeypatch.setattr(role_row, "SHOWN", 2)
    await _add(
        role("b1", fit=6, target_role="builders", status="first_impression"),
        role("b2", fit=5, target_role="builders", status="first_impression"),
        role("b3", fit=4, target_role="builders", status="first_impression"),
        role("b4", status="applied", target_role="builders"),
        role("b5", status="gated_out", target_role="builders", screened_out_reason="pay under the floor"),
        role("o1", target_role="ops"), role("o2", status="applied", target_role="ops"), role("none"),
    )
    await _rank({"b1": 60, "b2": 50, "b3": 40})
    cols = {c["key"]: c for c in (await client.get("/api/v1/board", params={"role": "builders"})).json()["columns"]}
    assert cols["first_impression"]["count"] == 3
    assert [c["title"] for c in cols["first_impression"]["cards"]] == ["Role b1", "Role b2"]
    assert cols["applied"]["count"] == 1 and cols["closed"]["count"] == 1
    assert cols["second_impression"]["count"] == 0
    assert {c["title"] for k in cols for c in cols[k]["cards"]} == {"Role b1", "Role b2", "Role b4", "Role b5"}

    # One column whole, still for that role; and another role has its own.
    asked = {"role": "builders", "column": "first_impression"}
    whole = (await client.get("/api/v1/board", params=asked)).json()["columns"][0]
    assert [c["title"] for c in whole["cards"]] == ["Role b1", "Role b2", "Role b3"]
    ops = {c["key"]: c["count"] for c in (await client.get("/api/v1/board", params={"role": "ops"})).json()["columns"]}
    assert ops["second_impression"] == 1 and ops["applied"] == 1

    # No filter (or an empty one) is every role, and an id that is no target role has none.
    for params in ({}, {"role": ""}):
        every = {c["key"]: c["count"] for c in (await client.get("/api/v1/board", params=params)).json()["columns"]}
        assert every["first_impression"] == 3 and every["second_impression"] == 2 and every["applied"] == 2
    nobody = (await client.get("/api/v1/board", params={"role": "interns"})).json()["columns"]
    assert all(c["count"] == 0 and c["cards"] == [] for c in nobody)
    assert (await client.get("/api/v1/board", params={"role": "x" * 81})).status_code == 422


async def test_moving_a_role_changes_its_column_and_is_written_to_the_trail(client):
    await _add(role("m"))
    role_id = await _id_of("m")
    moved = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "applied", "note": "applied on their site"})
    assert moved.status_code == 200
    # The move answers with the card as its new column shows it (`board.row`): an applied role says nothing (24.18).
    assert moved.json()["status"] == "applied" and moved.json()["next"] is None
    async with db.session_factory()() as session:
        row = await session.get(Opportunity, role_id)
        events = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "role_moved"))).all()
    assert row.status == "applied" and "applied on their site" in row.notes
    assert len(events) == 1 and events[0].sentence == "You moved Role m at Co m from Second Impression to Applied."


async def test_moving_a_read_role_to_ready_to_launch_needs_no_packet(client):
    await _add(role("nopacket", **counted()))
    role_id = await _id_of("nopacket")
    moved = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "ready_to_launch"})
    assert moved.status_code == 200 and moved.json()["status"] == "ready_to_launch" and moved.json()["packet"] is None


async def test_a_gated_role_marked_applied_by_hand_keeps_why_it_was_gated(client):
    await _add(role("s", status="gated_out", screened_out_reason="location"))
    role_id = await _id_of("s")
    await client.post(f"/api/v1/roles/{role_id}/move", json={"to": "applied"})
    async with db.session_factory()() as session:
        row = await session.get(Opportunity, role_id)
    assert row.status == "applied" and "gated out first: location" in row.notes


async def test_a_move_that_cannot_be_made_says_why(client):
    await _add(role("x"))
    role_id = await _id_of("x")
    for not_the_owners in ("gated_out", "not_a_stage", "landing_zone"):
        bad = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": not_the_owners})
        assert bad.status_code == 409, not_the_owners
        assert "can be moved to first_impression, second_impression" in bad.json()["detail"], not_the_owners
    missing = await client.post("/api/v1/roles/00000000-0000-0000-0000-000000000000/move", json={"to": "applied"})
    assert missing.status_code == 404


async def test_a_card_carries_the_predicted_ask_of_a_researched_role_and_none_for_one_not_yet(client):
    researched = role("rs", comp="$90,000 - $120,000", analysis={"ask": {"ask": 100000, "below_floor": True}})
    await _add(researched, role("nw", comp="$90,000 - $120,000"))
    body = (await client.get("/api/v1/board", params={"column": "second_impression"})).json()
    cards = {c["title"]: c for c in body["columns"][0]["cards"]}
    assert cards["Role rs"]["ask"] == 100000 and cards["Role rs"]["pay"] == "$90K–$120K"
    assert cards["Role nw"]["ask"] is None and cards["Role nw"]["pay"] == "$90K–$120K"


# --- Closed: one lane, read by its reason ---------------------------------------------------------------------------


def test_a_closed_role_is_read_under_the_reason_it_closed_for():
    key = board.closed_reason_key
    assert key("gated_out", None) == "gated_out" and key("gated_out", "they_said_no") == "gated_out"
    assert key("rejected", "they_said_no") == "rejected"
    assert key("withdrew", "posting_gone") == "expired"
    assert key("gone_quiet", "received_offer") == "received_offer"
    assert key("withdrew", "not_my_field") == "not_my_field" and key("withdrew", "wrong_level") == "wrong_level"
    assert key("withdrew", "the_employer") == "employer" and key("withdrew", "pay_too_low") == "pay"
    assert key("withdrew", "location_or_mode") == "location_or_mode"
    for unplaced in (None, "unknown", "no_reply", "duplicate", "something_else", "i_passed"):
        assert key("gone_quiet", unplaced) == "other", unplaced


async def _closed_roles():
    await _add(
        role("gate1", status="gated_out", screened_out_reason="pay"), role("gate2", status="gated_out"),
        role("no", status="rejected", ended_reason="they_said_no"),
        role("gone", status="withdrew", ended_reason="posting_gone"),
        role("offer", status="withdrew", ended_reason="received_offer"),
        role("pay", status="withdrew", ended_reason="pay_too_low", target_role="builders"),
        role("quiet", status="gone_quiet", ended_reason="unknown"),
        role("live"))


async def test_the_closed_column_read_by_a_reason_holds_only_the_roles_under_it(client):
    await _closed_roles()
    for reason, titles in (("gated_out", {"Role gate1", "Role gate2"}), ("expired", {"Role gone"}),
                           ("rejected", {"Role no"}), ("other", {"Role quiet"}), ("employer", set())):
        body = (await client.get("/api/v1/board", params={"column": "closed", "reason": reason})).json()
        column = body["columns"][0]
        assert {c["title"] for c in column["cards"]} == titles and column["count"] == len(titles), reason
    everything = (await client.get("/api/v1/board", params={"column": "closed"})).json()["columns"][0]
    assert everything["count"] == 7


# --- moving a closed role back ---------------------------------------------------------------------------------------


async def test_a_closed_role_moves_back_to_where_it_was_with_its_reason_and_its_gates_stop_gone(client):
    await _add(role("g", status="landing_zone"), role("n", status="ready_to_launch", **counted()))
    gated, ended = await _id_of("g"), await _id_of("n")
    async with db.session_factory()() as session:
        row = await session.get(Opportunity, gated)
        row.status, row.screened_out_reason = "gated_out", "pay under the floor"
        await session.commit()
    await client.post(f"/api/v1/roles/{ended}/move", json={"to": "rejected"})
    back = await client.post(f"/api/v1/roles/{gated}/move-back")
    assert back.status_code == 200 and back.json()["status"] == "landing_zone" and back.json()["reason"] is None
    back = await client.post(f"/api/v1/roles/{ended}/move-back")
    assert back.json()["status"] == "ready_to_launch" and back.json()["ended_reason"] is None
    async with db.session_factory()() as session:
        row = await session.get(Opportunity, gated)
        assert row.screened_out_reason is None and "gated out first: pay under the floor" in row.notes
        sentences = [e.sentence for e in (await session.scalars(select(TrailEvent).where(
            TrailEvent.event == "role_moved"))).all()]
    assert "You moved Role g at Co g from Gated out to Landing Zone." in sentences


async def test_a_closed_role_with_no_earlier_status_moves_back_to_first_impression(client):
    await _add(role("born_closed", status="rejected"))
    role_id = await _id_of("born_closed")
    async with db.session_factory()() as session:  # no move records where it came from
        await session.execute(update(StageMove).where(StageMove.role_id == role_id).values(from_status=None))
        await session.commit()
    back = await client.post(f"/api/v1/roles/{role_id}/move-back")
    assert back.status_code == 200 and back.json()["status"] == "first_impression"


async def test_only_a_closed_role_can_be_moved_back(client):
    await _add(role("open"))
    refused = await client.post(f"/api/v1/roles/{await _id_of('open')}/move-back")
    assert refused.status_code == 409 and "is not closed" in refused.json()["detail"]
    missing = await client.post("/api/v1/roles/00000000-0000-0000-0000-000000000000/move-back")
    assert missing.status_code == 404


async def test_a_column_asked_for_by_page_gives_that_page_in_rank_order_with_page_and_pages(client):
    await _add(*[role(f"fi{n:02d}", status="first_impression") for n in range(1, 8)])
    await _rank({f"fi{n:02d}": 100 - n for n in range(1, 8)})
    first = (await client.get("/api/v1/board?column=first_impression&page=1&size=3")).json()["columns"][0]
    last = (await client.get("/api/v1/board?column=first_impression&page=3&size=3")).json()["columns"][0]
    assert (first["count"], first["page"], first["pages"]) == (7, 1, 3)
    assert [c["title"] for c in first["cards"]] == ["Role fi01", "Role fi02", "Role fi03"]
    assert (last["page"], [c["title"] for c in last["cards"]]) == (3, ["Role fi07"])
    whole = (await client.get("/api/v1/board?column=first_impression")).json()["columns"][0]
    assert len(whole["cards"]) == 7 and "page" not in whole


async def test_the_board_loads_a_page_of_each_list_but_second_impression_and_ready_to_launch_in_full(client):
    await _add(*[role(f"fi{n:02d}", status="first_impression") for n in range(60)],
               *[role(f"si{n:02d}", status="second_impression") for n in range(55)],
               *[role(f"rl{n:02d}", status="ready_to_launch") for n in range(53)])
    cols = {c["key"]: c for c in (await client.get("/api/v1/board")).json()["columns"]}
    assert (cols["first_impression"]["count"], len(cols["first_impression"]["cards"])) == (60, 50)
    assert len(cols["second_impression"]["cards"]) == 55 and len(cols["ready_to_launch"]["cards"]) == 53


async def test_a_closed_reason_is_paged_too(client):
    await _add(*[role(f"c{n:02d}", status="rejected") for n in range(7)])
    got = (await client.get("/api/v1/board?column=closed&reason=rejected&page=2&size=3")).json()["columns"][0]
    assert (got["count"], got["page"], got["pages"], len(got["cards"])) == (7, 2, 3, 3)
