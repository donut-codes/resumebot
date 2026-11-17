"""The days since a role was posted mean one thing, worked out one way, wherever they are asked (the owner, 2026-11-03).

When the freshness Must is made stricter, the re-gate stopped roles
that are 15 days old or younger and released roles that are older than the Must allows, though the Must had only got
stricter. The role gate read the posted date of the role's cleansed copy (the posting with the most text, ties broken by
a random id drawn afresh at every nightly re-read), then `role.posted`, then the day the role was found, so one role
with a dated posting on the employer's system and an undated copy of the same req on a job site was 80 days old on the
night one copy won and 10 on the night the other did, and the owner's gate stopped it, released it and stopped it again.

The rule, in `fit/freshness.py` (`posted_basis`): a role was posted on the newest employer date among its own and its
postings' (a posting's date held to the day the app first saw it: a board that re-dates a posting on every read does
not make it new); with none, on the earliest day a posting was first seen or the role was found. Everything here is
made up."""

import contextlib
import copy
import uuid
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import freshness
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts, regate
from app.jobsearch.gating.role_gates import gate_results
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting
from app.jobsearch.pipeline import deck
from app.jobsearch.postings import cleansed, raw_copies
from app.jobsearch.profile import schema as p
from app.jobsearch.research import prioritize
from app.settings import settings
from app.sites.models import Site
from app.timeutil import owner_today, owner_zone, utc_now
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.support import FEW_ARGUMENTS, OWNER, POSTING_WORDS, PROFILE, bound_arguments, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon", "made_up_boards")

TODAY = date(2026, 11, 3)
#: The owner's Must on freshness, as the app saves it: the days since it was posted are within 15.
DIAL = {"id": "dial-freshness-must", "kind": "job_fact", "fact": "posted_days", "op": "<=", "value": 15}
GATES = {"location": {}, "other": [DIAL]}


def ago(days: int, today: date = TODAY) -> date:
    return today - timedelta(days=days)


def seen(day: date) -> datetime:
    """Noon on the owner's local `day`, as a time the app first saw a posting."""
    return datetime.combine(day, time(12), tzinfo=owner_zone())


# --- the rule ------------------------------------------------------------------------------------------------------


def test_the_posted_day_is_the_newest_employer_date_among_the_roles_own_and_its_postings():
    got = freshness.posted_basis(
        ago(40), ago(5), [(ago(35), seen(ago(30))), (ago(12), seen(ago(12))), (None, seen(ago(5)))])
    assert (got.day, got.basis) == (ago(12), freshness.POSTED)


def test_a_roles_own_date_counts_beside_its_postings_dates():
    got = freshness.posted_basis(ago(32), ago(5), [(ago(35), seen(ago(30)))])
    assert (got.day, got.basis) == (ago(32), freshness.POSTED), "the role's own date is the newest here"


def test_a_roles_own_date_after_the_day_its_postings_were_first_seen_is_held_to_that_day():
    """A role is made from a posting and copies its date (`pipeline/from_posting.py`), so a board that dates the posting
    again is on the role too: the role is no younger for it, and its day is the one the hard pass gave the posting"""
    redated = [(ago(2), seen(ago(20)))]
    at_the_hard_pass = freshness.posted_basis(None, None, redated)
    as_a_role = freshness.posted_basis(ago(2), ago(1), redated)
    assert as_a_role == at_the_hard_pass == freshness.PostedDay(ago(20), freshness.SEEN)
    # the board dated the posting again on a later read: the role's copy is of an earlier read, and no younger either
    assert freshness.posted_basis(ago(5), ago(1), redated) == at_the_hard_pass
    # a date no posting of the role holds any more, after the day they were first seen, is held the same way
    assert freshness.posted_basis(ago(3), ago(1), [(None, seen(ago(20)))]) == at_the_hard_pass


def test_a_role_made_younger_by_a_posting_that_really_is_new_is_younger():
    """Held to the day it was first seen, a posting that is dated on or before that day still counts: a new req of the
    same role, seen and dated two days ago, is the role's newest date whatever the first one says."""
    got = freshness.posted_basis(ago(2), ago(1), [(ago(2), seen(ago(20))), (ago(2), seen(ago(2)))])
    assert (got.day, got.basis) == (ago(2), freshness.POSTED)


def test_a_postings_date_after_the_day_the_app_first_saw_it_counts_as_the_day_it_was_first_seen():
    """A board that dates a posting again on every read (a posting dated more than a week after the
    app first saw it): the posting was not made new by it."""
    got = freshness.posted_basis(None, ago(1), [(ago(2), seen(ago(20)))])
    assert (got.day, got.basis) == (ago(20), freshness.SEEN)


def test_an_undated_posting_never_makes_a_role_younger_than_a_date_one_of_its_postings_gives():
    """The mirror pair: the employer's own posting, dated 80 days ago, and an undated copy of the same req on a job
    site, first seen ten days ago."""
    got = freshness.posted_basis(None, ago(10), [(ago(80), seen(ago(10))), (None, seen(ago(10)))])
    assert (got.day, got.basis) == (ago(80), freshness.POSTED)
    flipped = freshness.posted_basis(None, ago(10), [(None, seen(ago(10))), (ago(80), seen(ago(10)))])
    assert flipped == got, "the order of the postings decides nothing"


def test_with_no_employer_date_the_day_is_the_earliest_a_posting_was_first_seen_or_the_role_found():
    two = freshness.posted_basis(None, ago(2), [(None, seen(ago(9))), (None, seen(ago(4)))])
    assert (two.day, two.basis) == (ago(9), freshness.SEEN)
    earlier = freshness.posted_basis(None, ago(20), [(None, seen(ago(9)))])
    assert (earlier.day, earlier.basis) == (ago(20), freshness.FOUND)
    lone = freshness.posted_basis(None, ago(7), [])
    assert (lone.day, lone.basis) == (ago(7), freshness.FOUND), "a tracker's role has no posting"


def test_a_role_nothing_dates_has_no_posted_day():
    got = freshness.posted_basis(None, None, [(None, None)])
    assert (got.day, got.basis, got.posted, got.found) == (None, None, None, None)


def test_a_time_is_counted_on_the_owners_local_day(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    evening = datetime(2026, 11, 3, 2, 30, tzinfo=UTC)  # half past nine on the evening of the 2nd, local
    got = freshness.posted_basis(None, None, [(None, evening)])
    assert got.day == date(2026, 11, 2)
    assert freshness.posted_basis(None, None, [(date(2026, 11, 3), evening)]).day == date(2026, 11, 2), (
        "a date after the local day it was first seen is held to that day")


def test_the_two_columns_a_posted_day_gives_the_gates_are_the_employers_date_or_the_day_found():
    employer = freshness.PostedDay(ago(12), freshness.POSTED)
    assert (employer.posted, employer.found) == (ago(12), None)
    for basis in (freshness.SEEN, freshness.FOUND):
        other = freshness.PostedDay(ago(12), basis)
        assert (other.posted, other.found) == (None, ago(12))


# --- a job board's date --------------------------------------------------------------------------------------------
# (the owner, 2026-11-05, docs/PRD.md 40.1) Found in use: roles sourced to the employer's own posting said a few
# days old on their Boardone card and were weeks old by the employer's posting. A job board re-lists one req
# under a new id and a new date, and the newest date won.


def board(day: date, first: date | None = None) -> tuple[date, datetime, bool]:
    """A posting a job board gave, as `posted_basis` is handed it: its date, the time the app first saw it, and True
    (a board's). `first` is the day it was first seen, by default the day it is dated."""
    return day, seen(first or day), True


def test_a_job_boards_date_never_makes_a_role_newer_than_the_employers():
    """The case: the employer's own posting is dated 40 days ago, the card of the same req on a job board 3."""
    employer = (ago(40), seen(ago(40)))
    got = freshness.posted_basis(None, ago(3), [employer, board(ago(3))])
    assert (got.day, got.basis) == (ago(40), freshness.POSTED)
    assert freshness.posted_basis(None, ago(3), [board(ago(3)), employer]) == got, "the order decides nothing"


def test_every_card_of_a_reposted_req_is_held_to_the_employers_date_not_only_the_newest():
    cards = [board(ago(9)), board(ago(3)), board(ago(1))]
    got = freshness.posted_basis(None, ago(1), [(ago(40), seen(ago(40))), *cards])
    assert (got.day, got.basis) == (ago(40), freshness.POSTED)


def test_a_job_boards_older_date_does_not_make_a_role_older_than_the_employers_newest():
    """The employer dated its posting 3 days ago and the board's card says 20: the employer's date is the one counted.
    A board's older date is no reason to count a role from it, and with several reqs of the employer's the newest still
    wins."""
    got = freshness.posted_basis(None, ago(3), [(ago(3), seen(ago(3))), board(ago(20))])
    assert (got.day, got.basis) == (ago(3), freshness.POSTED)
    reqs = [(ago(40), seen(ago(40))), (ago(12), seen(ago(12)))]
    assert freshness.posted_basis(None, ago(3), [*reqs, board(ago(20))]) == freshness.PostedDay(
        ago(12), freshness.POSTED)


def test_the_employers_newest_date_stands_whatever_a_job_board_says():
    """Several reqs of one employer are still counted from the newest (`posted_basis`, 40.1): a board's card of the
    newest one, or of a newer day, says nothing newer."""
    reqs = [(ago(40), seen(ago(40))), (ago(12), seen(ago(12)))]
    newest = freshness.PostedDay(ago(12), freshness.POSTED)
    assert freshness.posted_basis(None, ago(2), [*reqs, board(ago(2))]) == newest
    assert freshness.posted_basis(None, ago(2), [*reqs, board(ago(12))]) == newest


def test_with_no_employer_date_a_job_boards_date_counts_as_it_always_did():
    """No employer date, so nothing to hold the board to: its newest card is the day (a lone board's card, a role the
    employer's undated posting is a copy of), as before."""
    lone = freshness.posted_basis(None, ago(2), [board(ago(5)), board(ago(2))])
    assert (lone.day, lone.basis) == (ago(2), freshness.POSTED)
    undated = freshness.posted_basis(None, ago(5), [(None, seen(ago(30))), board(ago(5))])
    assert (undated.day, undated.basis) == (ago(5), freshness.POSTED), "an undated employer posting is no date"
    redated = freshness.posted_basis(None, ago(1), [board(ago(2), first=ago(20))])
    assert (redated.day, redated.basis) == (ago(20), freshness.SEEN), "still held to the day it was first seen"


def test_a_role_made_from_a_job_boards_posting_holds_that_boards_date_as_its_own():
    """A role copies the date of the posting it was made from (`pipeline/from_posting.py`): from a job board's card it
    is the board's date, so it is held to the employer's like the card's, and with no employer date it stands."""
    employer = (ago(40), seen(ago(1)))  # the employer's posting, dated long ago, first seen after the card
    got = freshness.posted_basis(ago(3), ago(3), [employer, board(ago(3))], posted_from_board=True)
    assert (got.day, got.basis) == (ago(40), freshness.POSTED)
    alone = freshness.posted_basis(ago(3), ago(3), [board(ago(3))], posted_from_board=True)
    assert (alone.day, alone.basis) == (ago(3), freshness.POSTED), "no employer date: the board's stands"
    own = freshness.posted_basis(ago(3), ago(3), [employer], posted_from_board=True)
    assert own == freshness.PostedDay(ago(40), freshness.POSTED), "the role's copy of a board's date is no newer"


def test_a_roles_own_date_that_is_no_boards_holds_a_job_boards_card_to_it():
    """A role made from the employer's own posting, or dated by the employer's page after it was made, holds the
    employer's date, which no posting of it need still carry."""
    got = freshness.posted_basis(ago(40), ago(3), [board(ago(3))])
    assert (got.day, got.basis) == (ago(40), freshness.POSTED)


# --- the subject the gates read ------------------------------------------------------------------------------------


def _role(**over) -> Opportunity:
    return Opportunity(**{"owner": OWNER, "company": "Fabrikam", "title": "Programs Lead", "location": "Remote, US",
                          "move_type": "remote", "found": ago(1), **over})


def test_a_roles_subject_reads_the_posted_day_it_is_given_and_never_the_date_of_its_cleansed_copy():
    stated = SimpleNamespace(locations=[], employer=None, work_mode=None, posted_date=ago(40), pay_min=None,
                             pay_max=None)
    subject = gating.role_subject(_role(), None, stated, posted=freshness.PostedDay(ago(12), freshness.POSTED))
    assert (subject.posted, subject.found, subject.age_from) == (ago(12), None, freshness.POSTED)
    other = gating.role_subject(_role(), None, stated, posted=freshness.PostedDay(ago(9), freshness.SEEN))
    assert (other.posted, other.found, other.age_from) == (None, ago(9), freshness.SEEN)


def test_a_roles_subject_with_no_day_given_counts_from_what_the_role_itself_holds():
    own = gating.role_subject(_role(posted=ago(6)), None)
    assert (own.posted, own.found, own.age_from) == (ago(6), None, freshness.POSTED)
    found = gating.role_subject(_role(), None)
    assert (found.posted, found.found, found.age_from) == (None, ago(1), freshness.FOUND)


def test_a_postings_subject_is_the_roles_rule_over_the_one_posting():
    posting = SimpleNamespace(company="Fabrikam", location="Remote", work_mode="remote", comp=None, comp_min=None,
                              comp_max=None, posted=ago(40), first_seen=seen(ago(30)), target_role=None, url=None,
                              apply_url=None, title="Programs Lead")
    got = gating.posting_subject(posting, None)
    assert (got.posted, got.found, got.age_from) == (ago(40), None, freshness.POSTED)
    redated = SimpleNamespace(**{**posting.__dict__, "posted": ago(9)})
    got = gating.posting_subject(redated, None)
    assert (got.posted, got.found, got.age_from) == (None, ago(30), freshness.SEEN), "held to the day first seen"
    undated = SimpleNamespace(**{**posting.__dict__, "posted": None})
    got = gating.posting_subject(undated, None)
    assert (got.posted, got.found, got.age_from) == (None, ago(30), freshness.SEEN)


@pytest.mark.parametrize(("basis", "words"), [
    (freshness.POSTED, "the employer's date"), (freshness.SEEN, "the day the app first saw it"),
    (freshness.FOUND, "the day the role was found")])
def test_the_sentence_says_where_the_days_were_counted_from(basis, words):
    subject = gating.role_subject(_role(), None, posted=freshness.PostedDay(ago(28), basis))
    profile = _profile()
    stop = gating.check(subject, gating.Rules.of(profile, today=TODAY, hard_pass=1, criteria=1))
    assert stop is not None and stop.gate_id == "dial-freshness-must"
    assert stop.role_words == (f"The days since it was posted is within 15 days "
                               f"(the days since it was posted 28, counted from {words}).")


def _profile():
    data = copy.deepcopy(PROFILE)
    data["gates"] = GATES
    return p.Profile.model_validate(data)


# --- the roles, in a database --------------------------------------------------------------------------------------


async def _employer(session, name: str):
    return await employers.find_or_make(session, OWNER, name)


async def make_role(session, name: str, *, posted: date | None = None, found_days: int = 3,
                    status: str = stages.FIRST_IMPRESSION, board_url: str | None = None) -> Opportunity:
    today = owner_today()
    role = Opportunity(
        owner=OWNER, slug=name, company=name, title="Programs Lead", origin="scout", fit=3, board_url=board_url,
        match_key=f"{name}|programs lead", status=status, found=ago(found_days, today), posted=posted,
        url=f"https://jobs.example.test/{name}", description=POSTING_WORDS, location="Remote, US", move_type="remote",
        source_url=f"https://jobs.example.test/{name}", source_kind="employer", sourced_at=utc_now())
    session.add(role)
    await session.flush()
    return role


async def add_posting(session, role: Opportunity, ext: str, *, posted_days: int | None, seen_days: int,
                      size: int | None = None, ats: str = "tenantone", url: str | None = None) -> JobPosting:
    """One posting of the role: dated `posted_days` ago (none: undated), first seen `seen_days` ago, and, with `size`,
    a cleansed copy of that many characters (the posting with the most text is the role's copy). `ats` is its system
    (the employer's job system by default; `job_site` and `browser` for a read of a site) and `url` its address (the
    role's by default)."""
    today = owner_today()
    employer = await _employer(session, role.company)
    when = seen(ago(seen_days, today))
    posting = JobPosting(
        owner=OWNER, employer_id=employer.id, ats=ats, ext_id=f"{role.slug}-{ext}", company=role.company,
        title=role.title, match_key=role.match_key, first_seen=when, last_seen=when, links=[], location="Remote",
        url=url or role.url, opportunity_id=role.id,
        posted=ago(posted_days, today) if posted_days is not None else None)
    session.add(posting)
    if size is not None:
        raw = RawPosting(owner=OWNER, source_id=employer.id, reader="legacy", kind="legacy", ext_id=posting.ext_id,
                         fetched_at=when, body=b"x", body_sha256=posting.ext_id.ljust(64, "0"), body_bytes=1,
                         no_raw=True)
        session.add(raw)
        await session.flush()
        session.add(CleansedPosting(owner=OWNER, raw_id=raw.id, cleaner_version=1, locations=[], text_clean="x" * size,
                                    posted_date=posting.posted))
    await session.flush()
    await raw_copies.repoint(session, OWNER)  # the pointer the code that writes copies keeps (`raw_copies._point`)
    return posting


async def mirror_pair(session, name: str, winner: str) -> Opportunity:
    """The live case: a dated posting on the employer's system (80 days old) and an undated copy of the same req on a
    job site, both first seen ten days ago, the role found ten days ago and holding no date of its own. `winner` is the
    copy with the most text, which the role's cleansed copy is."""
    role = await make_role(session, name, found_days=10)
    await add_posting(session, role, "own", posted_days=80, seen_days=10, size=3407 if winner == "dated" else 3406)
    await add_posting(session, role, "site", posted_days=None, seen_days=10, size=3407 if winner == "undated" else 3406)
    return role


async def _grow(session, role: Opportunity, ext: str) -> None:
    """Make the posting `ext`'s cleansed copy the biggest of the role's."""
    posting = await session.scalar(select(JobPosting).where(
        JobPosting.opportunity_id == role.id, JobPosting.ext_id == f"{role.slug}-{ext}"))
    raw = await session.scalar(select(RawPosting).where(RawPosting.ext_id == posting.ext_id))
    biggest = await session.scalar(select(CleansedPosting).where(CleansedPosting.raw_id == raw.id))
    biggest.text_clean = "x" * 5000
    await session.flush()


async def test_a_role_gated_for_its_age_does_not_pass_later_because_another_copy_of_its_posting_is_the_biggest(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        role = await mirror_pair(session, "Fabrikam", "dated")
        first = await regate.run(session, OWNER, cause="The Must on freshness changed")
        await session.refresh(role)
        assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "dial-freshness-must"
        assert first.roles_stopped == 1
        was = role.screened_out_reason
        biggest = await cleansed.copy_of(session, OWNER, role.id)
        assert biggest is not None and biggest.posted_date == ago(80, owner_today()), "the dated copy wins at first"

        await _grow(session, role, "site")  # the night's re-read: the undated copy of the same req is the biggest
        biggest = await cleansed.copy_of(session, OWNER, role.id)
        assert biggest is not None and biggest.posted_date is None, "the undated copy is the biggest after the flip"
        again = await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
        await session.refresh(role)

        assert role.status == stages.GATED_OUT, "released because the other copy of the same posting became the copy"
        assert again.roles_returned == 0
        assert role.screened_out_reason == was


@pytest.mark.parametrize("winner", ["dated", "undated"])
async def test_the_freshness_stop_is_the_same_whichever_copy_of_the_posting_is_the_biggest(js, winner):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        role = await mirror_pair(session, "Fabrikam", winner)
        await regate.run(session, OWNER, cause="The Must on freshness changed")
        await session.refresh(role)
        assert role.status == stages.GATED_OUT, f"not stopped when the {winner} copy is the role's copy"
        assert role.screened_out_reason == (
            "The days since it was posted is within 15 days "
            "(the days since it was posted 80, counted from the employer's date).")


async def test_a_role_dated_young_is_not_stopped_for_the_age_of_an_older_req_among_its_postings(js):
    """The role's own date is 12 days old; one of its many postings, the biggest, is another req dated 35 days ago."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        role = await make_role(session, "Fabrikam", posted=ago(12, owner_today()), found_days=12)
        await add_posting(session, role, "old", posted_days=35, seen_days=35, size=4000)
        await add_posting(session, role, "new", posted_days=12, seen_days=12, size=3000)
        report = await regate.run(session, OWNER, cause="The Must on freshness changed")
        await session.refresh(role)
        assert role.status == stages.FIRST_IMPRESSION and report.roles_stopped == 0


async def test_a_role_with_a_posting_dated_young_is_not_stopped_for_the_older_one_it_was_first_seen_with(js):
    """The live fourteen: the earliest posting was first seen 20 days ago and is dated then; another posting of the
    role is dated two days ago."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        role = await make_role(session, "Fabrikam", found_days=20)
        await add_posting(session, role, "old", posted_days=20, seen_days=20, size=4000)
        await add_posting(session, role, "new", posted_days=2, seen_days=2, size=3000)
        report = await regate.run(session, OWNER, cause="The Must on freshness changed")
        await session.refresh(role)
        assert role.status == stages.FIRST_IMPRESSION and report.roles_stopped == 0


async def test_a_posting_a_board_dated_again_after_the_app_saw_it_does_not_free_a_role(js):
    """The live single-posting case: first seen 26 days ago, the board dated it again two days ago."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        role = await make_role(session, "Fabrikam", posted=ago(40, owner_today()), found_days=26)
        await add_posting(session, role, "only", posted_days=2, seen_days=26, size=4000)
        await regate.run(session, OWNER, cause="The Must on freshness changed")
        await session.refresh(role)
        assert role.status == stages.GATED_OUT
        assert role.screened_out_reason == (
            "The days since it was posted is within 15 days "
            "(the days since it was posted 26, counted from the day the app first saw it).")


async def test_the_date_a_role_copied_from_a_posting_a_board_dated_again_does_not_free_it(js):
    """A role is made from a posting and takes its date (`pipeline/from_posting.py`): the board dated the posting two
    days ago, the app first saw it 20 days ago and made the role today. The hard pass said 20 days of the posting; the
    re-gate must say the same of the role, not the two days the role copied."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        today = owner_today()
        role = await make_role(session, "Fabrikam", posted=ago(2, today), found_days=0)
        posting = await add_posting(session, role, "only", posted_days=2, seen_days=20, size=4000)
        words = "(the days since it was posted 20, counted from the day the app first saw it)"
        stop = gating.check(gating.posting_subject(posting, None), await gating.rules(session, OWNER))
        assert stop is not None and words in stop.role_words, "stopped at the hard pass"

        await regate.run(session, OWNER, cause="The Must on freshness changed")
        await session.refresh(role)
        assert role.status == stages.GATED_OUT
        assert words in role.screened_out_reason, role.screened_out_reason


async def test_a_role_released_for_its_age_never_happens_when_the_must_only_got_stricter(js):
    """The Must is tightened from 28 to 15 days: nothing it stopped at 28 is released at 15."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"location": {}, "other": [{**DIAL, "value": 28}]})
        roles = [await mirror_pair(session, f"Fabrikam{n}", winner)
                 for n, winner in enumerate(("dated", "undated", "dated", "undated"))]
        await regate.run(session, OWNER, cause="The Must on freshness changed")
        for role in roles:
            await session.refresh(role)
            assert role.status == stages.GATED_OUT, "stopped at 28 days (the posting is 80 days old)"
        await load_profile(session, OWNER, gates=GATES)
        report = await regate.run(session, OWNER, cause="The Must on freshness changed")
        assert report.roles_returned == 0
        for role in roles:
            await session.refresh(role)
            assert role.status == stages.GATED_OUT


BOARD_CARDS = [("job_site", "https://www.boardone.test/jobs/view/4400000001"),
               ("browser", "https://www.boardthree.test/job-detail/4400000001")]
EMPLOYER_OWN = "https://careers.fabrikam.test/jobs/42"


async def _reposted(session, name: str, *, system: str = "job_site", card: str = BOARD_CARDS[0][1],
                    status: str = stages.FIRST_IMPRESSION) -> Opportunity:
    """The case: a role found on a job board, made from the card dated 3 days ago (so it holds the card's date),
    whose employer's own posting, which the app first saw yesterday, is dated 40 days ago."""
    today = owner_today()
    role = await make_role(session, name, posted=ago(3, today), found_days=3, status=status, board_url=card)
    await add_posting(session, role, "own", posted_days=40, seen_days=1, size=3000, url=EMPLOYER_OWN)
    await add_posting(session, role, "card", posted_days=3, seen_days=3, size=3100, ats=system, url=card)
    return role


@pytest.mark.parametrize(("system", "card"), BOARD_CARDS)
async def test_a_role_a_job_board_re_listed_is_as_old_as_the_employers_posting_says(js, system, card):
    async with db.session_factory()() as session:
        role = await _reposted(session, "Fabrikam", system=system, card=card)
        assert await freshness.posted_of(session, role) == freshness.PostedDay(ago(40, owner_today()), freshness.POSTED)


async def test_a_role_with_only_job_board_postings_is_counted_from_the_newest_of_them_as_before(js):
    async with db.session_factory()() as session:
        card = BOARD_CARDS[0][1]
        role = await make_role(session, "Fabrikam", posted=ago(3, owner_today()), found_days=3, board_url=card)
        await add_posting(session, role, "old", posted_days=9, seen_days=9, ats="job_site", url=card)
        await add_posting(session, role, "new", posted_days=3, seen_days=3, ats="job_site", url=card)
        assert await freshness.posted_of(session, role) == freshness.PostedDay(ago(3, owner_today()), freshness.POSTED)


async def test_a_role_the_employers_posting_does_not_date_is_counted_from_the_job_boards_card_as_before(js):
    async with db.session_factory()() as session:
        card = BOARD_CARDS[0][1]
        role = await make_role(session, "Fabrikam", posted=ago(3, owner_today()), found_days=3, board_url=card)
        await add_posting(session, role, "own", posted_days=None, seen_days=1, url=EMPLOYER_OWN)
        await add_posting(session, role, "card", posted_days=3, seen_days=3, ats="job_site", url=card)
        assert await freshness.posted_of(session, role) == freshness.PostedDay(ago(3, owner_today()), freshness.POSTED)


async def test_a_job_sites_posting_at_an_employers_own_address_is_the_employers_not_a_boards(js):
    """`job_site` and `browser` are ways of reading a site, not a kind of site: a read of an employer's own careers
    page is dated by the employer, and a newer date of its own counts."""
    async with db.session_factory()() as session:
        role = await make_role(session, "Fabrikam", found_days=3)
        await add_posting(session, role, "system", posted_days=40, seen_days=40)
        await add_posting(session, role, "page", posted_days=3, seen_days=3, ats="job_site", url=EMPLOYER_OWN)
        assert await freshness.posted_of(session, role) == freshness.PostedDay(ago(3, owner_today()), freshness.POSTED)


async def test_a_job_site_the_owner_searches_is_a_board_whatever_it_is_called(js):
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://jobs.sitebox.example.test/search", kind="job_board",
                         created_at=utc_now()))
        card = "https://jobs.sitebox.example.test/view/7"
        role = await make_role(session, "Fabrikam", found_days=3, board_url=card)
        await add_posting(session, role, "own", posted_days=40, seen_days=1, url=EMPLOYER_OWN)
        await add_posting(session, role, "card", posted_days=3, seen_days=3, ats="browser", url=card)
        assert await freshness.posted_of(session, role) == freshness.PostedDay(ago(40, owner_today()), freshness.POSTED)


async def test_the_freshness_must_stops_a_role_a_job_board_re_listed_for_the_employers_age(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        role = await _reposted(session, "Fabrikam")
        report = await regate.run(session, OWNER, cause="The Must on freshness changed")
        await session.refresh(role)
        assert role.status == stages.GATED_OUT and report.roles_stopped == 1
        assert role.screened_out_gate_id == "dial-freshness-must"
        assert "the days since it was posted 40, counted from the employer's date" in role.screened_out_reason


# --- one rule wherever it is asked ---------------------------------------------------------------------------------

#: What every path says of the role below: 20 days, counted from the employer's date.
TWENTY = "the days since it was posted 20, counted from the employer's date"


async def _old_dated_and_young_undated(session, name: str, status: str) -> Opportunity:
    """The role holds no date of its own and was found three days ago; one posting is dated 20 days ago, an undated
    copy of the same req was first seen three days ago and is the biggest, the role's cleansed copy."""
    role = await make_role(session, name, found_days=3, status=status)
    await add_posting(session, role, "own", posted_days=20, seen_days=3, size=3000)
    await add_posting(session, role, "site", posted_days=None, seen_days=3, size=3100)
    return role


async def test_the_re_gate_the_take_time_check_and_the_cleansed_data_gate_count_the_same_days(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        regated = await _old_dated_and_young_undated(session, "Regated", stages.FIRST_IMPRESSION)
        taken = await _old_dated_and_young_undated(session, "Taken", stages.FIRST_IMPRESSION)
        at_cleansed = await _old_dated_and_young_undated(session, "Cleansed", stages.CLEANSED_DATA)

        assert await deck.gate(session, OWNER) == 1
        assert await gate_facts.stop_if_failed(session, taken, "before a session takes it") is True
        await regate.one_role(session, OWNER, regated, cause="The Must on freshness changed")

        for role in (regated, taken, at_cleansed):
            assert role.status == stages.GATED_OUT, f"{role.company} passed the freshness gate"
            assert role.screened_out_gate_id == "dial-freshness-must"
            assert TWENTY in role.screened_out_reason, role.screened_out_reason


async def test_the_role_page_shows_the_gate_with_the_days_the_re_gate_counts(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        role = await _old_dated_and_young_undated(session, "Paged", stages.FIRST_IMPRESSION)
        role_id = role.id
        await session.commit()

    body = (await client.get(f"/api/v1/roles/{role_id}")).json()
    line = {g["id"]: g for g in body["gates"]}["dial-freshness-must"]
    assert line["result"] == "fail" and line["fact"] == TWENTY


async def test_the_role_page_gate_line_and_the_gate_results_of_a_role_with_no_postings_agree(js):
    """A role with no posting in the index (a tracker's) is counted from the day it was found, as it always was."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        role = await make_role(session, "Tracked", found_days=20)
        profile = _profile()
        line = {x["id"]: x for x in gate_results(role, profile, owner_today())}["dial-freshness-must"]
        assert line["result"] == "fail"
        assert line["fact"] == "the days since it was posted 20, counted from the day the role was found"


async def test_the_posted_days_of_many_roles_are_read_in_one_grouped_read(js):
    async with db.session_factory()() as session:
        mirror = await mirror_pair(session, "Mirror", "undated")
        own = await make_role(session, "Own", posted=ago(6, owner_today()), found_days=2)
        bare = await make_role(session, "Bare", found_days=9)
        got = await freshness.posted_by_role(session, OWNER, [mirror, own, bare])
        today = owner_today()
        assert got[mirror.id] == freshness.PostedDay(ago(80, today), freshness.POSTED)
        assert got[own.id] == freshness.PostedDay(ago(6, today), freshness.POSTED)
        assert got[bare.id] == freshness.PostedDay(ago(9, today), freshness.FOUND)
        assert await freshness.posted_of(session, mirror) == got[mirror.id]
        assert await freshness.posted_by_role(session, OWNER, []) == {}


def test_the_score_counts_the_same_days_as_the_gates():
    """The age an ask on freshness reads (`prioritize.facts_of`) and the freshness dial's fact (`known_of`, from
    `job_facts`) are the posted day the gates count, not the role's own columns."""
    data = copy.deepcopy(PROFILE)
    data["scoring"] = {"asks": [{"id": "ask-fresh", "section": "posting", "says": "Posted within 15 days", "points": 2,
                                 "checked_by": "app", "fact": "posted_days", "op": "<=", "value": 15}]}
    profile = p.Profile.model_validate(data)
    role = _role(found=owner_today())  # found today, holding no date of its own
    day = freshness.PostedDay(owner_today() - timedelta(days=20), freshness.POSTED)

    facts = prioritize.facts_of(role, profile, None, day)
    assert (facts["age_days"], facts["age_from"]) == (20, "posted")
    assert prioritize.known_of(role, profile, None, day)["posted_days"] == 20
    seen_on = freshness.PostedDay(owner_today() - timedelta(days=5), freshness.SEEN)
    assert prioritize.facts_of(role, profile, None, seen_on)["age_from"] == "first seen"
    assert prioritize.facts_of(role, profile)["age_days"] == 0, "with no day given, the role's own columns"
    assert prioritize.known_of(role, profile, None)["posted_days"] == 0


async def test_many_roles_are_read_for_the_owner_and_picked_out_as_few_are_by_their_ids(js, monkeypatch):
    async with db.session_factory()() as session:
        mirror = await mirror_pair(session, "Mirror", "undated")
        bare = await make_role(session, "Bare", found_days=9)
        few = await freshness.posted_by_role(session, OWNER, [mirror, bare])
        monkeypatch.setattr(freshness, "NARROW_AT", 1)
        many = await freshness.posted_by_role(session, OWNER, [mirror, bare])
        assert many == few and few[mirror.id].basis == freshness.POSTED


@contextlib.contextmanager
def _statements():
    seen: list[str] = []

    def sent(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        seen.append(" ".join(statement.split()))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", sent)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", sent)


async def _roles_with_a_posting_each(session, count: int) -> list[Opportunity]:
    now = utc_now()
    roles = [Opportunity(owner=OWNER, slug=f"r{n}", company=f"Co {n}", title="Programs Lead", origin="scout",
                         match_key=f"co {n}|programs lead", status=stages.FIRST_IMPRESSION, found=owner_today())
             for n in range(count)]
    session.add_all(roles)
    await session.flush()
    session.add_all([JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="tenantone", ext_id=f"e{n}",
                                company=r.company,
                                title=r.title, match_key=r.match_key, first_seen=now, last_seen=now, links=[],
                                opportunity_id=r.id, posted=ago(n % 30, owner_today()))
                     for n, r in enumerate(roles)])
    await session.flush()
    return roles


async def test_the_postings_of_a_few_hundred_roles_are_read_by_the_roles_ids_and_not_for_the_owner(js):
    """The postings of any list of more than 200 roles were read for every
    role of the owner and picked out. With the
    ids as one argument (`db.among`) a long list is no long statement, so the roles' own postings are read."""
    async with db.session_factory()() as session:
        roles = await _roles_with_a_posting_each(session, 201)
        await session.commit()
    async with db.session_factory()() as session:
        with _statements() as seen:
            got = await freshness.posted_by_role(session, OWNER, roles)
        postings = [q for q in seen if "FROM js_postings" in q]
        assert len(postings) == 1, postings
        assert "js_postings.opportunity_id IN (SELECT" in postings[0] and "json_each" in postings[0], postings
        every = await freshness.held_postings(session, OWNER)
        assert got == await freshness.posted_by_role(session, OWNER, roles, every), "the same days as from the owner's"
        assert len(got) == 201 and len({d for d in got.values()}) > 1


async def test_the_postings_of_a_list_of_roles_are_read_with_a_few_arguments_whatever_their_number(js):
    """`held_postings` by ids carried one argument for each id, and asyncpg refuses a statement of more than 32,767."""
    async with db.session_factory()() as session:
        roles = await _roles_with_a_posting_each(session, FEW_ARGUMENTS + 1)
        await session.commit()
    async with db.session_factory()() as session:
        with bound_arguments() as bound:
            held = await freshness.held_postings(session, OWNER, [r.id for r in roles])
    assert set(held) == {r.id for r in roles}
    assert bound and max(bound) <= FEW_ARGUMENTS
