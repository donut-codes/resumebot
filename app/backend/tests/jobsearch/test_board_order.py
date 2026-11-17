"""The order of the Pipeline's columns and the first 72 hours (docs/PRD.md 24.6 to 24.9, the owner, 2026-10-16).

Where the work is ahead (First Impression, Second Impression, Ready to Launch) roles are ordered by their rank
(`js_role_ranks`) plus `GOLDEN_BONUS` points while under 72 hours old, less `STALE_PENALTY` (21) once the posting is
over 30 days old, the highest first, ties to the younger, a role with no rank after the ranked ones. A role with a
Second Impression is ordered by its fit instead of its rank. Everywhere else the youngest posting comes first. Cards of
the roles ahead under 72 hours old are marked. Ready to Launch shows every role.
Every role here is made up."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app import db
from app.jobsearch.imports import tracker
from app.jobsearch.models import Opportunity, StageMove
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import row as role_row
from app.timeutil import owner_today, utc_now
from tests.builders import counted, rank_roles
from tests.support import OWNER, load_profile, tracker_row, tracker_workbook

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")


def make(slug, rank=None, status="second_impression", posted_days=None, found_days=None, hours_old=None, fit=3,
         **extra):
    """A role. `rank` is its first-impression rank (None: unranked), `posted_days` the employer's date that many days
    back, `found_days` the tracker's day found, and `hours_old` the hour the app made the row (the scout's `found_at`)
    that many hours back. `fit` is the old score, which orders nothing now."""
    today = owner_today()
    more = {"origin": "scout", **extra}
    if posted_days is not None:
        more["posted"] = today - timedelta(days=posted_days)
    if found_days is not None:
        more["found"] = today - timedelta(days=found_days)
    if hours_old is not None:
        more["created_at"] = utc_now() - timedelta(hours=hours_old)
    made = Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", fit=fit, status=status,
                       match_key=f"co {slug}|role {slug}", **more)
    made.rank_fit = rank  # not a column: `_add` turns it into the role's rank row
    return made


async def _add(*roles, profile=True):
    async with db.session_factory()() as session:
        if profile:
            await load_profile(session, OWNER)  # its old scale is 0 to 5, so a fit of 3 is 60%
        session.add_all(roles)
        await session.flush()
        await rank_roles(session, {r.id: r.rank_fit for r in roles if getattr(r, "rank_fit", None) is not None})
        await session.commit()


async def _titles(client, column, **params):
    body = (await client.get("/api/v1/board", params={"column": column, **params})).json()
    return [c["title"] for c in body["columns"][0]["cards"]]


async def _card(client, column, title):
    body = (await client.get("/api/v1/board", params={"column": column})).json()
    return next(c for c in body["columns"][0]["cards"] if c["title"] == title)


# --- where the work is ahead: the first 72 hours, then the rank ----------------------------------------------------


async def test_rank_plus_the_first_hours_bonus_less_the_stale_penalty_orders_the_roles_ties_to_the_younger(client):
    await _add(
        make("today20", rank=20, posted_days=0), make("yesterday100", rank=100, posted_days=1),
        make("twodays40", rank=40, posted_days=2),
        make("three100", rank=100, posted_days=3),  # 72 hours: not under, so no bonus
        make("old100", rank=100, posted_days=40),  # over 30 days: 21 off
        make("ten80", rank=80, posted_days=10), make("old20", rank=20, posted_days=2000),
    )
    assert pipeline.GOLDEN_BONUS == 20 and pipeline.STALE_PENALTY == 21
    assert await _titles(client, "second_impression") == [
        "Role yesterday100",  # 100 + 20
        "Role three100",  # 100
        "Role ten80",  # 80
        "Role old100",  # 100 - 21
        "Role twodays40", "Role today20",  # 40 + 20 and 20 + 20
        "Role old20"]  # 20 - 21


async def test_a_fresh_45_ranks_below_an_old_82_and_a_fresh_70_above_it(client):
    await _add(make("old82", rank=82, posted_days=10), make("fresh45", rank=45, posted_days=0),
               make("fresh70", rank=70, posted_days=1))
    assert await _titles(client, "second_impression") == ["Role fresh70", "Role old82", "Role fresh45"]


async def test_the_same_order_holds_in_first_impression_and_ready_to_launch(client):
    await _add(*[make(f"{status}_{slug}", rank=rank, status=status, posted_days=days)
                 for status in ("first_impression", "ready_to_launch")
                 for slug, rank, days in (("old100", 100, 30), ("new20", 20, 0), ("mid60", 60, 9))])
    for status in ("first_impression", "ready_to_launch"):
        assert await _titles(client, status) == [f"Role {status}_{slug}" for slug in ("old100", "mid60", "new20")]


async def test_second_impression_and_ready_to_launch_show_every_role_the_others_keep_a_page(client, monkeypatch):
    monkeypatch.setattr(role_row, "SHOWN", 2)
    await _add(*[make(f"r{n}", rank=50 + n, status="ready_to_launch", posted_days=n + 5) for n in range(5)],
               *[make(f"d{n}", rank=50 + n) for n in range(4)],
               *[make(f"f{n}", rank=50 + n, status="first_impression") for n in range(4)])
    cols = {c["key"]: c for c in (await client.get("/api/v1/board")).json()["columns"]}
    assert cols["ready_to_launch"]["count"] == 5 and len(cols["ready_to_launch"]["cards"]) == 5
    assert cols["second_impression"]["count"] == 4 and len(cols["second_impression"]["cards"]) == 4
    assert cols["first_impression"]["count"] == 4 and len(cols["first_impression"]["cards"]) == 2


async def test_the_same_age_goes_to_the_better_rank_and_the_same_hour_is_not_an_age_apart(client):
    await _add(
        make("posted_low", rank=20, posted_days=1), make("posted_high", rank=100, posted_days=1),
        make("found_a", rank=40, hours_old=5.2), make("found_b", rank=80, hours_old=5.8),  # the same hour
        make("found_c", rank=100, hours_old=30),
    )
    assert await _titles(client, "second_impression") == [
        "Role posted_high", "Role found_c",  # 120 each: the younger (24 hours, not 30) first
        "Role found_b",  # 100
        "Role found_a",  # 60
        "Role posted_low"]  # 40


async def test_a_role_is_under_72_hours_by_the_employers_date_else_when_the_app_found_it_else_the_day_found(client):
    await _add(
        make("found71", rank=20, hours_old=71.5), make("found73", rank=100, hours_old=73),
        # The employer's date says old whatever hour the app made the row.
        make("dated_old", rank=100, posted_days=10, hours_old=1),
        # And it says young for a row made long ago.
        make("dated_young", rank=20, posted_days=0, hours_old=500),
        # A tracker's role has a day found and no hour.
        make("tracker2", rank=20, origin="tracker", found_days=2, hours_old=500),
        make("tracker3", rank=100, origin="tracker", found_days=3, hours_old=500),
        make("undated", rank=100, origin="tracker", hours_old=1),
    )
    assert await _titles(client, "second_impression") == [
        # Full rank and not under 72 hours: 100 each, the younger first, and a role nothing dates is the oldest.
        "Role tracker3", "Role found73", "Role dated_old", "Role undated",
        "Role dated_young", "Role tracker2", "Role found71"]  # a rank of 20 and the bonus: 0, 48 and 71 hours


async def test_a_role_with_no_rank_comes_after_the_ranked_ones_and_the_younger_of_them_first(client):
    await _add(
        make("n_young", posted_days=1), make("s_young", rank=40, posted_days=1),
        make("n_older", posted_days=20), make("s_older", rank=20, posted_days=30),
        make("n_oldest", posted_days=90), make("s_oldest", rank=30, posted_days=300),
    )
    assert await _titles(client, "second_impression") == [
        "Role s_young", "Role s_older", "Role s_oldest",  # the ranked first, by rank plus the bonus
        "Role n_young", "Role n_older", "Role n_oldest"]  # then those with no rank, the younger first


async def test_a_card_carries_the_fit_of_a_read_role_and_the_rank_of_the_others_as_its_score(client):
    # Edited in 1.137.3: the score was the one the order uses, so these just-found roles carried the fresh bonus. Edited
    # again for docs/PRD.md 24.11: the card shows the score alone, the bonus only places the role in the order. Edited
    # for 24.15 (2026-11-06): the card shows the number the list is sorted by again, the bonus of these new roles in
    # it: 40 is 60, 90 is 110, 70 is 90, 55 is 75. (The first build kept it to 100 and showed 100 for the 110; the
    # owner's "the list is sorted by exactly the number the card shows" takes the cap off the card.)
    worked = {"answers": [], "asks": [], "possible": 5, "decided": 5, "percent": 40, "total": 2, "math": 2}
    await _add(
        make("read_ready", rank=90, status="ready_to_launch", fit=2, fit_breakdown=worked, **counted()),
        make("old_ready", rank=90, status="ready_to_launch", fit=2, fit_breakdown=worked),  # no Second Impression
        make("read_deck", rank=90, fit=2, fit_breakdown=worked, **counted()),
        make("ranked_deck", rank=70), make("first", rank=55, status="first_impression"),
        make("unranked", status="first_impression"))
    ready = {t: await _card(client, "ready_to_launch", t) for t in ("Role read_ready", "Role old_ready")}
    assert ready["Role read_ready"]["score"] == 60, "the Ready lane shows the Second Impression's 40 and the bonus"
    assert ready["Role old_ready"]["score"] == 110, "research without the stamp does not count: the rank and the 20"
    deck = {t: await _card(client, "second_impression", t) for t in ("Role read_deck", "Role ranked_deck")}
    assert deck["Role read_deck"]["score"] == 60
    assert deck["Role ranked_deck"]["score"] == 90
    first, unranked = await _card(client, "first_impression", "Role first"), await _card(
        client, "first_impression", "Role unranked")
    assert first["score"] == 75, "the rank as written, and the bonus: the role is new"
    assert unranked["score"] is None


async def test_a_deck_role_with_a_read_is_ordered_by_its_fit_not_its_rank(client):
    worked = {"answers": [], "asks": [], "possible": 5, "decided": 5, "percent": 95, "total": 5, "math": 2}
    await _add(make("high_rank", rank=90, posted_days=10), make("high_fit", rank=30, posted_days=10, fit=5,
                                                                 fit_breakdown=worked, **counted()))
    assert await _titles(client, "second_impression") == ["Role high_fit", "Role high_rank"]


async def test_a_role_scored_with_no_percent_has_no_fit_on_its_card(client):
    nothing = {"answers": [], "asks": [], "possible": 20, "decided": 0, "percent": None, "total": 9, "math": 2}
    worked = {"answers": [], "asks": [], "possible": 5, "decided": 5, "percent": 40, "total": 2, "math": 2}
    await _add(make("undecided", fit=9, posted_days=10, fit_breakdown=nothing),
               make("scored", fit=2, posted_days=20, fit_breakdown=worked))
    undecided = await _card(client, "second_impression", "Role undecided")
    assert undecided["fit_pct"] is None, "the dash a role with no fit gets, not '9'"
    assert (await _card(client, "second_impression", "Role scored"))["fit_pct"] == 40


async def test_the_old_fit_orders_nothing_only_the_rank_does(client):
    await _add(make("high_fit_low_rank", fit=6, rank=30, posted_days=20),
               make("low_fit_high_rank", fit=1, rank=50, posted_days=20),
               make("high_fit_unranked", fit=6, posted_days=20))
    assert await _titles(client, "second_impression") == [
        "Role low_fit_high_rank", "Role high_fit_low_rank", "Role high_fit_unranked"]


async def test_the_order_is_the_same_on_the_first_page_as_on_the_whole_column(client, monkeypatch):
    monkeypatch.setattr(role_row, "SHOWN", 3)
    await _add(*[make(f"r{n}", rank=(n % 6) * 15 + 10, posted_days=n * 2, status="first_impression") for n in range(8)])
    whole = await _titles(client, "first_impression")
    first = next(c for c in (await client.get("/api/v1/board")).json()["columns"] if c["key"] == "first_impression")
    assert [c["title"] for c in first["cards"]] == whole[:3] and first["count"] == 8


# --- the gold edge -------------------------------------------------------------------------------------------------


async def test_a_card_ahead_of_the_application_under_72_hours_old_is_new_and_no_other_is(client):
    await _add(
        make("f_young", status="first_impression", posted_days=2),
        make("f_old", status="first_impression", posted_days=3),
        make("s_young", posted_days=2), make("s_old", posted_days=3), make("s_found", hours_old=71),
        make("s_found_old", hours_old=72.5),
        make("r_young", status="ready_to_launch", posted_days=0),
        make("r_old", status="ready_to_launch", posted_days=5),
    )
    new = {
        ("first_impression", "Role f_young"): True, ("first_impression", "Role f_old"): False,
        ("second_impression", "Role s_young"): True, ("second_impression", "Role s_old"): False,
        ("second_impression", "Role s_found"): True, ("second_impression", "Role s_found_old"): False,
        ("ready_to_launch", "Role r_young"): True, ("ready_to_launch", "Role r_old"): False}
    for (column, title), want in new.items():
        assert (await _card(client, column, title))["new"] is want, (column, title)


async def _settings(**more):
    """The made-up profile's settings with these owner settings changed, saved as the next version."""
    from app.jobsearch.profile import store as profile_store
    from tests.support import PROFILE

    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "settings", {**PROFILE["settings"], **more}, by="test")
        await session.commit()


async def test_the_owners_stale_and_golden_settings_change_the_order_the_cards_and_the_gold(client):
    await _add(
        make("old", rank=60, posted_days=12), make("mid", rank=50, posted_days=5),
        make("day2", rank=40, posted_days=2, status="ready_to_launch"))
    await _settings(stale_days=10, stale_penalty=30, golden_hours=24, golden_bonus=5)
    assert await _titles(client, "second_impression") == ["Role mid", "Role old"], "60 less 30 under 50"
    old = await _card(client, "second_impression", "Role old")
    assert old["score"] == 30
    assert (await _card(client, "second_impression", "Role mid"))["score"] == 50, "5 days: not stale at 10"
    day2 = await _card(client, "ready_to_launch", "Role day2")
    assert (day2["new"], day2["score"]) == (False, 40), "2 days is over 24 hours: no bonus"


async def test_the_owners_golden_bonus_orders_a_new_role_first_and_shows_on_its_card(client):
    # Edited for docs/PRD.md 24.11: the card showed 40 and the 35, 75. It shows 40; the 35 still puts it first.
    # Edited for 24.15 (2026-11-06): the card shows the number the list is sorted by, so it shows 75 again, and the
    # list reads down in the numbers the cards print.
    await _add(make("fresh", rank=40, posted_days=0, status="ready_to_launch"),
               make("older", rank=60, posted_days=5, status="ready_to_launch"))
    await _settings(golden_bonus=35)
    assert await _titles(client, "ready_to_launch") == ["Role fresh", "Role older"], "40 and 35 beats 60"
    fresh = await _card(client, "ready_to_launch", "Role fresh")
    assert (fresh["score"], fresh["new"]) == (75, True), "its score and the owner's 35, and the New tag says so"
    assert (await _card(client, "ready_to_launch", "Role older"))["score"] == 60


async def test_the_board_has_no_drafting_bar(client):
    await _add(make("one"))
    body = (await client.get("/api/v1/board")).json()
    assert "drafting_bar" not in body, "the Pipeline has no fit filter to start on the drafting bar"


# --- everywhere else: by age too, newest first ------------------------------------------------------


async def _stamp(moves):
    """Say when each role (by slug) last changed: every stage move it has is set to that many hours back."""
    async with db.session_factory()() as session:
        ids = {r.slug: r.id for r in (await session.scalars(select(Opportunity))).all()}
        for slug, hours in moves.items():
            when = utc_now() - timedelta(hours=hours)
            await session.execute(update(StageMove).where(StageMove.role_id == ids[slug]).values(at=when))
        await session.commit()


async def test_applied_active_and_closed_go_by_age_newest_first_with_no_gold(client):
    """The same age as the lanes ahead (the posting date if known, else the found date), not the last change (the
    owner, 2026-10-15). Closed is one lane: the ended roles and the gated out ones together."""
    await _add(
        make("old_posting_moved_now", status="applied", posted_days=90),
        make("new_posting", status="applied", posted_days=0), make("middle", status="applied", posted_days=30),
        make("talk_old", status="active_discussion", posted_days=50),
        make("talk_new", status="active_discussion", posted_days=1),
        make("end_a", status="rejected", posted_days=1), make("end_b", status="withdrew", posted_days=60),
        make("end_c", status="gone_quiet", posted_days=3),
        make("gate_a", status="gated_out", posted_days=2, screened_out_reason="pay"),
        make("gate_b", status="gated_out", posted_days=40, screened_out_reason="place"),
    )
    # The last changes are not the order:
    await _stamp({"old_posting_moved_now": 1, "new_posting": 500, "end_b": 3, "gate_b": 6})
    assert await _titles(client, "applied") == ["Role new_posting", "Role middle", "Role old_posting_moved_now"]
    assert await _titles(client, "active_discussion") == ["Role talk_new", "Role talk_old"]
    assert await _titles(client, "closed") == [
        "Role end_a", "Role gate_a", "Role end_c", "Role gate_b", "Role end_b"]


async def test_a_role_with_no_posting_date_goes_in_those_columns_by_when_it_was_found(client):
    await _add(
        make("made_two_days_ago", status="applied", hours_old=48),
        make("made_ten_days_ago", status="applied", hours_old=24 * 10),
        make("made_an_hour_ago", status="applied", hours_old=1),
        make("posted_yesterday", status="applied", posted_days=1))
    assert await _titles(client, "applied") == [
        "Role made_an_hour_ago", "Role posted_yesterday", "Role made_two_days_ago", "Role made_ten_days_ago"]


async def test_a_tracker_import_goes_by_the_day_each_role_was_found_and_one_with_no_day_by_the_day_it_ran(client):
    """An import is a bulk insert: it writes no stage move, and every role in it shares the one moment it ran, so the
    day the tracker says each was found is what tells them apart (the scout's roles keep a move and an hour). The
    posting the import logs for a role is first seen on the day the tracker says it was found, and the moment the
    import ran when it says none: that is the day the gates count for the role (`freshness.posted_basis`), so a role
    with no day goes with the day of the import, not last (docs/PRD.md 40.1; before 2026-11-04 it went last)."""
    today = owner_today()
    days = {"ten": 10, "two": 2, "thirty": 30, "five": 5}
    rows = [tracker_row("Co", f"{status} {slug}", status=status, found=today - timedelta(days=n))
            for status in ("Applied", "Active Discussion", "Rejected") for slug, n in days.items()]
    # No day found: the app first saw it when the import ran.
    rows += [tracker_row("Co", f"{status} undated", status=status) for status in ("Applied", "Rejected")]
    async with db.session_factory()() as session:
        counts = await tracker.import_tracker(session, OWNER, tracker_workbook(rows))
        await session.commit()
    assert counts["added"] == len(rows)
    async with db.session_factory()() as session:
        # What the order has to work from: no stage move for any of them, and one `created_at` for all.
        assert await session.scalar(select(func.count()).select_from(StageMove)) == 0
        assert len({r.created_at for r in (await session.scalars(select(Opportunity))).all()}) == 1

    def by_day(status):
        return [f"{status} {slug}" for slug in ("two", "five", "ten", "thirty")]

    assert await _titles(client, "applied") == ["Applied undated", *by_day("Applied")]
    assert await _titles(client, "active_discussion") == by_day("Active Discussion")
    assert await _titles(client, "closed") == ["Rejected undated", *by_day("Rejected")]


async def test_a_tracker_role_moved_by_hand_goes_by_its_found_day_among_the_ones_the_tracker_dated(client):
    today = owner_today()
    async with db.session_factory()() as session:
        await tracker.import_tracker(session, OWNER, tracker_workbook([
            tracker_row("Co", "old", status="Applied", found=today - timedelta(days=40)),
            tracker_row("Co", "newer", status="Applied", found=today - timedelta(days=3)),
            tracker_row("Co", "waiting", status="Found", found=today - timedelta(days=60)),
        ]))
        await session.commit()
        waiting = (await session.scalars(select(Opportunity.id).where(Opportunity.title == "waiting"))).one()
    assert await _titles(client, "applied") == ["newer", "old"]
    assert (await client.post(f"/api/v1/roles/{waiting}/move", json={"to": "applied"})).status_code == 200
    assert await _titles(client, "applied") == ["newer", "old", "waiting"]


# --- age: the posting date, else the found date ---------------------------------------------------------------------


async def test_the_age_is_the_posting_date_when_known_else_the_found_date(client):
    await _add(make("posted", posted_days=30, hours_old=2), make("unposted", hours_old=2, found_days=2))
    posted = await _card(client, "second_impression", "Role posted")
    unposted = await _card(client, "second_impression", "Role unposted")
    assert (posted["age"]["from"], posted["age"]["days"]) == ("posted", 30)
    assert (unposted["age"]["from"], unposted["age"]["days"]) == ("found", 2)


async def test_new_follows_the_posting_date_not_the_day_found(client):
    await _add(make("fresh_post", posted_days=1), make("stale_post", posted_days=30, hours_old=1))
    assert (await _card(client, "second_impression", "Role fresh_post"))["new"] is True
    assert (await _card(client, "second_impression", "Role stale_post"))["new"] is False


async def test_a_card_shows_the_score_the_queue_uses_and_the_age_effect_only_when_age_changes_it(client):
    worked = {"answers": [], "asks": [], "possible": 5, "decided": 5, "percent": 60, "total": 3, "math": 2}
    await _add(
        make("old_fit", rank=10, status="ready_to_launch", posted_days=40, fit=3, fit_breakdown=worked, **counted()),
        make("mid_fit", rank=10, status="ready_to_launch", posted_days=10, fit=3, fit_breakdown=worked, **counted()),
        make("new_fit", rank=10, status="ready_to_launch", posted_days=0, hours_old=1, fit=3, fit_breakdown=worked,
             **counted()),
        make("old_rank", rank=70, posted_days=45), make("tiny_old", rank=5, posted_days=45))
    old = await _card(client, "ready_to_launch", "Role old_fit")
    mid = await _card(client, "ready_to_launch", "Role mid_fit")
    new = await _card(client, "ready_to_launch", "Role new_fit")
    assert old["score"] == 60 - pipeline.STALE_PENALTY
    assert mid["score"] == 60, "ten days old: age changes nothing"
    # Old (24.11, 2026-11-03): 60, the bonus ordered it first and was not in its number. New (24.15): 80, the bonus is.
    assert new["score"] == 80, "an hour old: the bonus orders it first and is in its number"
    assert await _titles(client, "ready_to_launch") == ["Role new_fit", "Role mid_fit", "Role old_fit"]
    assert (await _card(client, "second_impression", "Role old_rank"))["score"] == 70 - pipeline.STALE_PENALTY
    assert (await _card(client, "second_impression", "Role tiny_old"))["score"] == 0, "never below 0"
