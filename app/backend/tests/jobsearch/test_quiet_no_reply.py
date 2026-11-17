"""Rule 2 of the quiet rules: an Applied role with no word for the
owner's number of days moves to Gone Quiet, "No word came back". The days count, in the owner's days, from the later of
the day the role became Applied and the day of the last mail matched to it; a hand move into Applied starts the count
again; blank means never. Mail does not exist yet, so `quiet.last_matched_mail` is stood in. Synthetic data only."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.mail import kinds as mail_kinds
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, MailMessage, Opportunity, StageMove
from app.jobsearch.pipeline import ended, quiet
from app.models.work import TrailEvent
from app.settings import settings
from app.sites import where
from app.timeutil import owner_today
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile, pin_clock

TODAY = date(2026, 10, 14)
NOON = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
TITLE = "Operations Analyst"


@pytest.fixture(autouse=True)
def owner_days_are_utc(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")


@pytest.fixture
def settled(monkeypatch):
    calls: list[str] = []

    async def settle_watch(session, owner):
        calls.append(owner)

    monkeypatch.setattr(where, "settle_watch", settle_watch)
    return calls


def _ago(days: int, *, hour: int = 12) -> datetime:
    return datetime(2026, 10, 14, hour, 0, tzinfo=UTC) - timedelta(days=days)


async def _number(session, days) -> None:
    """The owner's number, saved as a version of Settings."""
    await load_profile(session, OWNER, settings={**PROFILE["settings"], "no_reply_days": days})


def _role(slug: str, status: str = stages.APPLIED, **more) -> Opportunity:
    return Opportunity(
        owner=OWNER, slug=slug, company="Acme", title=more.pop("title", f"{TITLE} {slug}"), status=status,
        match_key=match_key("Acme", f"{TITLE} {slug}"), origin="scout", fit=3, **more)


async def _applied(session, slug: str, days_ago: int, **more) -> Opportunity:
    """An Applied role that became Applied that many days ago (its stage move says so)."""
    role = _role(slug, **more)
    session.add(role)
    await session.flush()
    await session.execute(update(StageMove).where(StageMove.role_id == role.id).values(at=_ago(days_ago)))
    return role


async def _imported(session, slug: str, created_days_ago: int, status: str = stages.APPLIED) -> Opportunity:
    """A role as the tracker import leaves it: in a stage with no stage move at all, first held that many days ago."""
    role = _role(slug, status, created_at=_ago(created_days_ago))
    session.add(role)
    await session.flush()
    await session.execute(delete(StageMove).where(StageMove.role_id == role.id))
    return role


async def _state(session, role: Opportunity) -> tuple[str, str | None]:
    await session.refresh(role)
    return role.status, role.ended_reason


async def _lines(session, role: Opportunity, event: str = quiet.NO_REPLY_EVENT) -> list[TrailEvent]:
    return list((await session.scalars(select(TrailEvent).where(
        TrailEvent.owner == OWNER, TrailEvent.link_id == role.id, TrailEvent.event == event))).all())


def _mail(monkeypatch, received: dict) -> None:
    """The mail seam stood in: each role's latest matched mail, by the mail's own received day (a date, as the real one
    answers)."""
    async def last_matched_mail(session, owner, role_ids):
        return {role_id: received[role_id].date() for role_id in role_ids if role_id in received}

    monkeypatch.setattr(quiet, "last_matched_mail", last_matched_mail)


# --- the count --------------------------------------------------------------------------------------------------------


async def test_with_30_days_a_role_applied_31_or_30_days_ago_moves_and_one_29_days_ago_does_not(js):
    async with db.session_factory()() as session:
        await _number(session, 30)
        old, edge, young = (await _applied(session, "old", 31), await _applied(session, "edge", 30),
                            await _applied(session, "young", 29))
        result = await quiet.run(session, OWNER, today=TODAY)
        assert (result.went, result.silent, result.days) == (0, 2, 30)
        assert await _state(session, old) == ("gone_quiet", "no_reply")
        assert await _state(session, edge) == ("gone_quiet", "no_reply")
        assert await _state(session, young) == ("applied", None)
        (line,) = await _lines(session, old)
        assert await _lines(session, young) == []
        assert ended.words(old) == "No word came back"
    assert line.sentence == "Gone Quiet: no word came back in 30 days."
    assert (line.actor, line.tier, line.link_kind) == ("app", "result", "opportunity")
    assert line.detail == ("It became Applied on Sep 13. No mail has been matched to it. "
                           "Your number is 30 days (Missions, Read the employers' boards).")


async def test_one_day_is_said_in_the_singular(js):
    async with db.session_factory()() as session:
        await _number(session, 1)
        role = await _applied(session, "a", 1)
        result = await quiet.run(session, OWNER, today=TODAY)
        assert (result.silent, result.days) == (1, 1)
        (line,) = await _lines(session, role)
    assert line.sentence == "Gone Quiet: no word came back in 1 day."
    assert "Your number is 1 day (" in (line.detail or "")


@pytest.mark.parametrize("blank", [None, 0, -1])
async def test_with_the_number_blank_zero_or_negative_nothing_moves(js, blank):
    async with db.session_factory()() as session:
        await _number(session, blank)
        role = await _applied(session, "a", 400)
        result = await quiet.run(session, OWNER, today=TODAY)
        assert (result.went, result.silent, result.days) == (0, 0, None)
        assert await _state(session, role) == ("applied", None)
        assert await _lines(session, role) == []


async def test_a_mail_matched_to_the_role_restarts_the_count_from_its_day(js, monkeypatch):
    async with db.session_factory()() as session:
        await _number(session, 30)
        heard, silent = await _applied(session, "heard", 40), await _applied(session, "silent", 40)
        _mail(monkeypatch, {heard.id: _ago(10), silent.id: _ago(31)})
        assert (await quiet.run(session, OWNER, today=TODAY)).silent == 1
        assert await _state(session, heard) == ("applied", None), "mail 10 days ago, 30 is the number"
        assert await _state(session, silent) == ("gone_quiet", "no_reply")
        (line,) = await _lines(session, silent)
    assert line.detail == ("It became Applied on Sep 4. The last mail matched to it came on Sep 13. "
                           "Your number is 30 days (Missions, Read the employers' boards).")


async def test_the_count_uses_the_day_the_mail_came_not_the_day_the_app_read_it(js, monkeypatch):
    """The seam answers with the received date (R2.3). A mail the app classed today but that came 40 days ago does not
    shorten the count; the stand-in holds both dates and gives back the received one, as the mail spec's does."""
    async with db.session_factory()() as session:
        await _number(session, 30)
        role = await _applied(session, "a", 90)
        received, classed = _ago(40), NOON
        _mail(monkeypatch, {role.id: received})
        assert received < classed
        result = await quiet.run(session, OWNER, today=TODAY)
        assert result.silent == 1, "40 days since it came, not 0 since it was read"
        assert await _state(session, role) == ("gone_quiet", "no_reply")


async def test_a_mails_day_counts_as_that_day_where_the_owners_clock_is_a_day_ahead_of_utc(js, monkeypatch):
    """Regression case: the mail's day was made a moment at noon UTC and read back in the owner's
    zone, which is the next day from UTC+12 on (here Auckland, UTC+13), so a mail of the 15th counted as the 16th and
    the role went quiet a day late."""
    pin_clock(monkeypatch, NOON, "Pacific/Auckland")  # 1 am on the 15th there
    async with db.session_factory()() as session:
        await _number(session, 30)
        role = await _applied(session, "a", 90)
        session.add(MailMessage(
            owner=OWNER, message_key="m1", sender="Acme", sender_key="acme.example", subject="Hello", state="acted",
            day=owner_today() - timedelta(days=30), kind=mail_kinds.CONFIRMATION, role_id=role.id))
        await session.flush()
        assert (await quiet.last_matched_mail(session, OWNER, [role.id])) == {role.id: date(2026, 9, 15)}
        assert (await quiet.run(session, OWNER)).silent == 1, "30 days since the mail's own day, with 30 the number"
        (line,) = await _lines(session, role)
    assert "The last mail matched to it came on Sep 15." in (line.detail or "")


async def test_a_role_moved_back_to_applied_by_hand_five_days_ago_starts_a_new_count(js):
    async with db.session_factory()() as session:
        await _number(session, 30)
        role = _role("a")
        session.add(role)
        await session.flush()
        await session.execute(delete(StageMove).where(StageMove.role_id == role.id))
        session.add_all([
            StageMove(owner=OWNER, role_id=role.id, from_status=None, to_status="applied", at=_ago(60)),
            StageMove(owner=OWNER, role_id=role.id, from_status="applied", to_status="gone_quiet", at=_ago(20)),
            StageMove(owner=OWNER, role_id=role.id, from_status="gone_quiet", to_status="applied", at=_ago(5))])
        await session.flush()
        assert (await quiet.run(session, OWNER, today=TODAY)).silent == 0
        assert await _state(session, role) == ("applied", None)


async def test_an_imported_role_with_no_stage_move_counts_from_the_day_the_app_first_held_it(js):
    async with db.session_factory()() as session:
        await _number(session, 30)
        old, young = await _imported(session, "old", 31), await _imported(session, "young", 29)
        assert await session.scalar(select(StageMove.id).where(StageMove.role_id.in_([old.id, young.id]))) is None
        assert (await quiet.run(session, OWNER, today=TODAY)).silent == 1
        assert await _state(session, old) == ("gone_quiet", "no_reply")
        assert await _state(session, young) == ("applied", None)
        (line,) = await _lines(session, old)
    assert (line.detail or "").startswith("It became Applied on Sep 13. ")


async def test_only_applied_roles_move(js):
    async with db.session_factory()() as session:
        await _number(session, 30)
        roles = {stage: await _imported(session, stage, 200, stage) for stage in stages.STAGES
                 if stage != stages.APPLIED}
        before = {stage: await _state(session, role) for stage, role in roles.items()}
        assert (await quiet.run(session, OWNER, today=TODAY)).silent == 0
        assert {stage: await _state(session, role) for stage, role in roles.items()} == before
        assert [await _lines(session, role) for role in roles.values()] == [[]] * len(roles)


async def test_another_owners_applied_role_is_not_touched(js):
    async with db.session_factory()() as session:
        await _number(session, 30)
        mine = await _applied(session, "mine", 90)
        theirs = Opportunity(owner="someone-else", slug="t", company="Acme", title=TITLE, status="applied",
                             match_key="acme|t", origin="scout", fit=3, created_at=_ago(90))
        session.add(theirs)
        await session.flush()
        await session.execute(delete(StageMove).where(StageMove.role_id == theirs.id))
        assert (await quiet.run(session, OWNER, today=TODAY)).silent == 1
        assert await _state(session, mine) == ("gone_quiet", "no_reply")
        assert await _state(session, theirs) == ("applied", None)


async def test_a_role_rule_1_moved_that_night_is_not_moved_again_by_rule_2(js):
    async with db.session_factory()() as session:
        await _number(session, 30)
        role = await _applied(session, "a", 90)
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id="p", company="Acme", title=TITLE,
            match_key="acme|p", first_seen=_ago(80), last_seen=_ago(10), closed_at=_ago(5), links=[],
            opportunity_id=role.id, url="https://boards.example.test/acme/jobs/1"))
        other = await _applied(session, "b", 90)
        await session.flush()
        result = await quiet.run(session, OWNER, today=TODAY)
        assert (result.went, result.silent) == (1, 1), "each role is ended once, for the better evidence"
        assert await _state(session, role) == ("gone_quiet", "posting_gone")
        assert await _state(session, other) == ("gone_quiet", "no_reply")
        assert await _lines(session, role) == [] and len(await _lines(session, role, "posting_went")) == 1
        assert len(await _lines(session, other)) == 1


async def test_days_are_the_owners_days_not_utc_days(js, monkeypatch):
    """Applied at 23:30 on Sep 14 in Los Angeles, which is already Sep 15 in UTC: that is Sep 14 to the owner, 30 days
    before Oct 14, so it moves; one applied an hour and a half later (01:00 on Sep 15 local) does not."""
    monkeypatch.setattr(settings, "timezone", "America/Los_Angeles")
    async with db.session_factory()() as session:
        await _number(session, 30)
        late, later = _role("late"), _role("later")
        session.add_all([late, later])
        await session.flush()
        for role, at in ((late, datetime(2026, 9, 15, 6, 30, tzinfo=UTC)),
                         (later, datetime(2026, 9, 15, 8, 0, tzinfo=UTC))):
            await session.execute(update(StageMove).where(StageMove.role_id == role.id).values(at=at))
        assert (await quiet.run(session, OWNER, today=TODAY)).silent == 1
        assert await _state(session, late) == ("gone_quiet", "no_reply")
        assert await _state(session, later) == ("applied", None)
        (line,) = await _lines(session, late)
    assert (line.detail or "").startswith("It became Applied on Sep 14. ")


async def test_today_is_the_owners_day_when_none_is_given(js, monkeypatch):
    monkeypatch.setattr(quiet, "owner_today", lambda: TODAY)
    async with db.session_factory()() as session:
        await _number(session, 30)
        role = await _applied(session, "a", 31)
        assert (await quiet.run(session, OWNER)).silent == 1
        assert await _state(session, role) == ("gone_quiet", "no_reply")


# --- the hand --------------------------------------------------------------------------------------------------------


async def test_asking_for_a_packet_is_not_word_from_the_employer(js):
    """14.7 counts from the Applied day and the last matched mail only: a `packet_asked` line after the Applied day does
    not exempt the role and does not restart the count. A move into Applied does (the case above)."""
    async with db.session_factory()() as session:
        await _number(session, 30)
        role = await _applied(session, "a", 40)
        session.add(TrailEvent(owner=OWNER, at=_ago(5), actor="person", who="Tester", event="packet_asked",
                               sentence="You asked for a packet.", link_kind="opportunity", link_id=role.id))
        await session.flush()
        assert (await quiet.run(session, OWNER, today=TODAY)).silent == 1
        assert await _state(session, role) == ("gone_quiet", "no_reply")


# --- the step's output ------------------------------------------------------------------------------------------------


def test_the_report_names_rule_2s_moves_on_a_line_of_its_own_and_prints_nothing_for_none():
    assert quiet.report_lines({"went": 0, "silent": 2, "days": 30}) == [
        "2 Applied roles moved to Gone Quiet: no word in 30 days."]
    assert quiet.report_lines({"went": 0, "silent": 1, "days": 1}) == [
        "1 Applied role moved to Gone Quiet: no word in 1 day."]
    assert quiet.report_lines({"went": 3, "silent": 0, "days": 30}) == [], "rule 1 has no line"
    assert quiet.report_lines({"went": 0, "silent": 2, "days": None}) == [], "no number, no line"
    assert quiet.report_lines({"went": 0, "silent": 2, "days": 0}) == []
    assert quiet.report_lines({}) == [] and quiet.report_lines(None) == []
    assert quiet.report_lines({"silent": 0, "days": 30}) == []


def test_days_of_reads_blank_zero_and_negative_as_never():
    assert [quiet.days_of(n) for n in (None, 0, -1, -30, 1, 30, 3650)] == [None, None, None, None, 1, 30, 3650]
    assert quiet.days_of(True) is None, "a flag is no number of days"


async def test_the_result_carries_what_the_step_did_and_the_watch_is_settled_once(js, settled):
    async with db.session_factory()() as session:
        await _number(session, 30)
        await _applied(session, "a", 45)
        await _applied(session, "b", 45)
        result = await quiet.run(session, OWNER, today=TODAY)
        assert result == quiet.Result(went=0, silent=2, days=30)
        assert quiet.report_lines({"went": result.went, "silent": result.silent, "days": result.days}) == [
            "2 Applied roles moved to Gone Quiet: no word in 30 days."]
        assert await quiet.run(session, OWNER, today=TODAY) == quiet.Result(went=0, silent=0, days=30)
    assert settled == [OWNER], "once for the run, not once for each role, and not on a run that moved nothing"


async def test_a_run_that_moves_nothing_does_not_settle_the_watch(js, settled):
    async with db.session_factory()() as session:
        await _number(session, 30)
        await _applied(session, "a", 3)
        assert await quiet.run(session, OWNER, today=TODAY) == quiet.Result(went=0, silent=0, days=30)
    assert settled == []


async def test_with_no_profile_saved_nothing_moves_and_nothing_fails(js):
    async with db.session_factory()() as session:
        role = await _applied(session, "a", 400)
        assert await quiet.run(session, OWNER, today=TODAY) == quiet.Result(went=0, silent=0, days=None)
        assert await _state(session, role) == ("applied", None)
