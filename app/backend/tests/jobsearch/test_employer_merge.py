"""Merging two employer records and splitting roles out of one (docs/PRD.md 36.5 and 36.7): a merge moves every role,
posting and site and keeps the hard pass, the owner's own word and both names; it can be asked again and a failure part
way changes nothing; a split moves chosen roles with their postings and names; the employer page does both by hand
after showing what will move. Everything is made up."""

import uuid

import pytest
from sqlalchemy import select, update

from app import db
from app.inbox import service as inbox
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.employers import records as employer_records
from app.jobsearch.employers.core import EmployerError, NoSuchEmployer
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.postings import same_role
from app.jobsearch.profile import store as profile_store
from app.models.work import TASK_DONE, Notification, Task, TrailEvent
from app.sites import auto_watch, watch_record, where
from app.sites.models import Site, SiteSkill
from app.timeutil import utc_now
from app.trail import service as trail
from tests.employer_builders import BY_YOU, make_employer, make_posting, make_role, make_site
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("example_world")


def _named(name: str, owned: bool = False) -> Employer:
    return Employer(name=name, owner_fields=["name"] if owned else [])


@pytest.mark.parametrize(("keep", "away", "expected"), [
    # the real name, not the acronym that is in the parenthesis
    (_named("GRID (Grand River Independent Distributor)"),
     _named("Grand River Independent Distributor (GRID)"), "Grand River Independent Distributor (GRID)"),
    (_named("ZENO a Family of Builder Companies"), _named("ZENO (a Family of Builder Companies)"),
     "ZENO a Family of Builder Companies"),
    (_named("ZENO (a Family of Builder Companies)"), _named("ZENO a Family of Builder Companies"),
     "ZENO a Family of Builder Companies"),
    # the legal name over the short one, and over a "via" tag
    (_named("Harbor Mutual"), _named("Harbor Mutual Insurance"), "Harbor Mutual Insurance"),
    (_named("PartsDepot"), _named("PartsDepot.com"), "PartsDepot.com"),
    (_named("Rivera Pharma (via Staffco)"), _named("Rivera Pharma and Company"), "Rivera Pharma and Company"),
    (_named("Rivera Pharma and Company"), _named("Rivera Pharma (via Staffco)"), "Rivera Pharma and Company"),
    # the same: the record that stays
    (_named("Brightwater"), _named("Brightwater"), "Brightwater"),
    # the owner's own name, whichever record has it
    (_named("Acme", owned=True), _named("Acme Industries International"), "Acme"),
    (_named("Acme Industries International"), _named("Acme", owned=True), "Acme"),
    (_named("Acme Co", owned=True), _named("Acme Industries", owned=True), "Acme Co"),
])
def test_the_one_record_left_takes_the_fuller_name_unless_the_owner_named_one(keep, away, expected):
    assert employer_records.chosen_name(keep, away) == expected


# --- merging ----------------------------------------------------------------------------------------------------------


async def test_a_merge_moves_everything_keeps_the_hard_pass_and_both_names(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Harbor Mutual", website="https://www.harbormutual.test")
        away = await make_employer(
            session, "Harbor Mutual Insurance", gated=True, gated_reason="never again", aliases=["Harbor"],
            notes="Their note", headquarters="Boston", offices=["Oakfield, ST"],
            events=[{"kind": "layoff", "date": "2025-06", "note": "cut"}],
            sources={"headquarters": {"source": "its site", "at": "2026-10-01T00:00:00+00:00", "by": "scout"}})
        session.add_all([
            make_role(keep, "Director A"), make_role(keep, "Director B", status=stages.READY_TO_LAUNCH),
            make_role(away, "Director C"), make_role(away, "Director D", status=stages.APPLIED),
            make_posting(away, "p1"), make_posting(away, "p2"),
            make_site(away, "https://careers.harbormutual.test", on=True)])
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        done = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    assert (done.merged, done.roles, done.postings, done.sites, done.gated_out) == (True, 2, 2, 1, 3)
    async with db.session_factory()() as session:
        assert await session.get(Employer, away_id) is None, "the other record is gone"
        merged = await session.get(Employer, keep_id)
        assert merged is not None
        assert (merged.name, merged.key) == ("Harbor Mutual Insurance", "harbor mutual insurance")
        assert sorted(merged.aliases) == ["Harbor", "Harbor Mutual"]
        found = await employers.find(session, OWNER, "Harbor Mutual")
        assert found is not None and found.id == keep_id, "a role found under the old name finds the record left"
        assert merged.gated and merged.gated_reason == "never again", "a hard pass on either holds on the result"
        roles = {r.title: r for r in (await session.scalars(select(Opportunity))).all()}
        assert {r.employer_id for r in roles.values()} == {keep_id}, "every role is kept, at the record left"
        assert [roles[t].status for t in ("Director A", "Director B", "Director C")] == [stages.GATED_OUT] * 3
        assert roles["Director A"].screened_out_reason == "employer gated: never again"
        assert roles["Director D"].status == stages.APPLIED, "a role already applied to is left alone"
        assert merged.website == "https://www.harbormutual.test"
        assert merged.headquarters == "Boston" and merged.sources["headquarters"]["source"] == "its site"
        assert merged.offices == ["Oakfield, ST"] and merged.notes == "Their note"
        assert merged.events[0]["kind"] == "layoff"
        assert {p.employer_id for p in (await session.scalars(select(JobPosting))).all()} == {keep_id}
        assert (await session.scalar(select(Site))).employer_id == keep_id
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).one()
        assert line.sentence == "Merged Harbor Mutual Insurance into Harbor Mutual: a test"
        assert line.link_kind == "employer" and line.link_id == keep_id and line.actor == "person"
        assert "2 roles, 2 postings and 1 site of Harbor Mutual Insurance moved onto Harbor Mutual, now called " \
            "Harbor Mutual Insurance" in (line.detail or "")


async def test_the_owners_word_wins_then_the_record_kept_then_the_other(js):
    async with db.session_factory()() as session:
        keep = await make_employer(
            session, "Alpha", website="https://alpha.test", industry="Software", founded=2000, owner_fields=["website"],
            sources={"website": BY_YOU, "industry": {"source": "its site", "at": "2026-09-01T00:00:00+00:00",
                                                     "by": "x"}})
        away = await make_employer(
            session, "Alpha Labs", website="https://other.test", industry="Finance", employees=500, founded=1990,
            ownership="private", local=True, check_directly=True, owner_fields=["website", "industry", "local"],
            sources={"website": BY_YOU, "industry": BY_YOU, "employees": {"source": "a filing", "at": "x", "by": "y"}})
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        merged = await session.get(Employer, keep_id)
        assert merged is not None
        assert merged.website == "https://alpha.test", "both the owner's: the record that is kept"
        assert merged.industry == "Finance" and merged.sources["industry"]["source"] == "you", "the owner's over found"
        assert merged.employees == 500 and merged.sources["employees"]["source"] == "a filing"
        assert merged.founded == 2000, "found against found: the record that is kept"
        assert merged.ownership == "private", "the kept record had none"
        assert merged.local is True and merged.check_directly is True
        assert merged.owner_fields == ["industry", "local", "website"]
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).one()
        assert "Your own word was kept over: website: https://other.test." in (line.detail or "")


async def test_the_job_system_comes_whole_from_the_record_that_knows_it_best(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech", ats="feedsys", board={"slug": "initech"},
                                   board_url="https://job-boards.feedsys.test/initech", fetch_ok=True)
        away = await make_employer(session, "Initech Systems", ats="slugsys", board={"slug": "initechsystems"},
                                   ats_found_by="owner", owner_fields=["ats"], fetch_note="read")
        other = await make_employer(session, "Hooli", ats="tenantsys", board={"tenant": "x"})  # a partial address
        third = await make_employer(session, "Hooli Labs", ats="slugsys", board={"slug": "hooli"})
        same = await make_employer(session, "Raviga", ats="feedsys", board={"slug": "raviga"})
        guess = await make_employer(session, "Raviga Capital", ats="slugsys", board={"slug": "ravigacapital",
                                                                                    "confirmed": False})
        session.add_all([make_posting(away, "7", ats="slugsys"), make_posting(third, "8", ats="slugsys")])
        await session.commit()
        ids = [(keep.id, away.id), (other.id, third.id), (same.id, guess.id)]
    async with db.session_factory()() as session:
        for keep_id, away_id in ids:
            await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        first, second, third = [await session.get(Employer, k) for k, _ in ids]
        assert first is not None and second is not None and third is not None
        assert (first.ats, first.board, first.ats_found_by) == ("slugsys", {"slug": "initechsystems"}, "owner"), (
            "the owner's own choice of board is the better known")
        assert "ats" in first.owner_fields
        assert (second.ats, second.board) == ("slugsys", {"slug": "hooli"}), "a whole address over a partial one"
        assert (third.ats, third.board) == ("feedsys", {"slug": "raviga"}), "a confirmed board over a guessed one"
        assert {p.employer_id for p in (await session.scalars(select(JobPosting))).all()} == {ids[0][0], ids[1][0]}


async def test_a_name_the_owner_gave_is_kept_and_the_key_it_frees_is_taken(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Acme Industries")
        away = await make_employer(session, "Acme", owner_fields=["name"], sources={"name": BY_YOU})
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        merged = await session.get(Employer, keep_id)
        assert merged is not None
        assert (merged.name, merged.key, merged.aliases) == ("Acme", "acme", ["Acme Industries"])
        assert "name" in merged.owner_fields and merged.sources["name"]["source"] == "you"


async def test_a_name_whose_key_another_record_has_is_not_taken(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Globex")
        away = await make_employer(session, "Globex Labs")
        await make_employer(session, "Globex Labs International")  # a third record holds this name's key
        away.name = "Globex Labs International"  # (a record whose name was changed after its key was made)
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        merged = await session.get(Employer, keep_id)
        assert merged is not None and merged.name == "Globex", "it keeps the name it had"
        assert "Globex Labs International" in merged.aliases


async def test_a_merge_can_be_asked_again_and_a_missing_record_is_nothing_to_do(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech")
        away = await make_employer(session, "Initech Systems")
        session.add(make_role(away, "Lead"))
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        first = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        again = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        stranger = await employer_records.merge(session, OWNER, keep_id, uuid.uuid4(), why="a test")
        await session.commit()
        with pytest.raises(NoSuchEmployer):
            await employer_records.merge(session, OWNER, uuid.uuid4(), away_id, why="a test")
        with pytest.raises(EmployerError, match="same employer"):
            await employer_records.merge(session, OWNER, keep_id, keep_id, why="a test")
        lines = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).all()
    assert first.merged and not again.merged and not stranger.merged
    assert again.employer.id == keep_id and len(lines) == 1, "asked again, nothing more is written"
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Opportunity))).one().employer_id == keep_id


async def test_a_posting_read_under_both_records_becomes_one_and_keeps_the_role_it_made(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech")
        away = await make_employer(session, "Initech Systems")
        role = make_role(away, "Lead")
        session.add(role)
        await session.flush()
        session.add_all([
            make_posting(keep, "100"), make_posting(away, "100", opportunity_id=role.id, owner_override=True),
            make_posting(away, "200"), make_posting(keep, "300"),
            make_posting(keep, "900", ats="slugsys"), make_posting(away, "900", ats="tenantsys")])
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        done = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    assert (done.postings, done.folded) == (2, 1)
    async with db.session_factory()() as session:
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
        assert sorted(rows) == ["100", "200", "300", "900", "tenantsys:900"], "the same id in another system is another"
        assert {p.employer_id for p in rows.values()} == {keep_id}
        assert rows["100"].opportunity_id is not None and rows["100"].owner_override, "the role it made is kept"


async def test_a_merge_that_fails_part_way_changes_nothing(js, monkeypatch):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech")
        away = await make_employer(session, "Initech Systems", gated=True, headquarters="Boston")
        session.add_all([make_role(away, "Lead"), make_posting(away, "1"),
                         make_site(away, "https://initech.test/jobs")])
        await session.commit()
        keep_id, away_id = keep.id, away.id

    async def boom(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(trail, "record", boom)  # the last thing a merge does
    async with db.session_factory()() as session:
        with pytest.raises(RuntimeError):
            await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()  # whatever the failed merge left would be saved here
    async with db.session_factory()() as session:
        assert await session.get(Employer, away_id) is not None, "the other record is still there"
        kept = await session.get(Employer, keep_id)
        assert kept is not None and not kept.gated and kept.headquarters is None and kept.aliases == []
        assert (await session.scalars(select(Opportunity))).one().employer_id == away_id
        assert (await session.scalars(select(JobPosting))).one().employer_id == away_id
        assert (await session.scalars(select(Site))).one().employer_id == away_id


async def test_the_sites_of_the_record_merged_away_go_to_the_one_kept(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech")
        away = await make_employer(session, "Initech Systems")
        session.add(make_site(away, "https://initech.test/jobs", on=True))
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        done = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    assert done.sites == 1
    async with db.session_factory()() as session:
        site = (await session.scalars(select(Site))).one()
        assert site.employer_id == keep_id and site.on, "its watch switch comes with it"


async def test_two_sites_the_app_made_are_one_and_the_watch_carries_over(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech")
        away = await make_employer(session, "Initech Systems", check_directly=True)
        mine = make_site(keep, "https://initech.test/careers")
        theirs = make_site(away, "https://initech.test/jobs", on=True)
        session.add_all([mine, theirs])
        await session.flush()
        session.add(SiteSkill(owner=OWNER, site_id=theirs.id, version=1, created_at=utc_now(),
                              recipe={"kind": "adapter", "ats": "feedsys", "board": {"slug": "initech"}}))
        await session.commit()
        keep_id, away_id, mine_id = keep.id, away.id, mine.id
    async with db.session_factory()() as session:
        await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        site = (await session.scalars(select(Site))).one()
        assert site.id == mine_id and site.on, "the kept record's own site, now watched"
        assert (await session.get(Employer, keep_id)).check_directly is True  # type: ignore[union-attr]
        assert (await session.scalars(select(SiteSkill))).all() == [], "the folded site's reader went with it"


async def test_a_site_with_a_skill_the_scout_learned_is_kept(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech")
        away = await make_employer(session, "Initech Systems")
        theirs = make_site(away, "https://initech.test/jobs")
        session.add_all([make_site(keep, "https://initech.test/careers"), theirs])
        await session.flush()
        session.add(SiteSkill(owner=OWNER, site_id=theirs.id, version=1, created_at=utc_now(),
                              recipe={"kind": "list_page", "start_url": "https://initech.test/jobs"}))
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        done = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    assert done.sites == 1
    async with db.session_factory()() as session:
        sites = (await session.scalars(select(Site))).all()
        assert len(sites) == 2 and {s.employer_id for s in sites} == {keep_id}, "what it learned is not thrown away"
        assert len((await session.scalars(select(SiteSkill))).all()) == 1


async def test_what_points_at_the_record_merged_away_points_at_the_one_kept(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech")
        away = await make_employer(session, "Initech Systems")
        third = await make_employer(session, "Hooli")
        fourth = await make_employer(session, "Pied Piper")
        fifth = await make_employer(session, "Raviga")
        options = [{"id": "merge", "label": "Merge"}, {"id": "keep_separate", "label": "Keep separate"}]
        for one, other in ((keep, away), (away, third)):  # asked about, not answered
            await inbox.raise_task(session, OWNER, key=employer_pairs.pair_key(one.id, other.id),
                                   kind=employer_pairs.KIND_MERGE, title="same?", options=options,
                                   link=("employer", one.id))
        for one, other in ((away, fourth), (away, fifth), (keep, fifth)):  # said to keep separate
            task = await inbox.raise_task(session, OWNER, key=employer_pairs.pair_key(one.id, other.id),
                                          kind=employer_pairs.KIND_MERGE, title="same?", options=options)
            task.state, task.answer = TASK_DONE, "keep_separate"
        about = await inbox.raise_task(session, OWNER, key=None, kind="note", title="Look at them",
                                       link=("employer", away.id))
        await inbox.notify(session, OWNER, key=None, kind="note", title="Told", link=("employer", away.id))
        await trail.record(session, OWNER, event="note", sentence="Something", link=("employer", away.id))
        await session.commit()
        made = {"keep": keep, "away": away, "third": third, "fourth": fourth, "fifth": fifth}
        ids = {n: e.id for n, e in made.items()}
        about_id = about.id
    async with db.session_factory()() as session:
        await employer_records.merge(session, OWNER, ids["keep"], ids["away"], why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        asked = (await session.scalars(select(Task).where(Task.kind == employer_pairs.KIND_MERGE))).all()
        # the open questions are withdrawn; what was said of the record merged away is now said of the one kept
        assert sorted(t.key for t in asked) == sorted([
            employer_pairs.pair_key(ids["keep"], ids["fourth"]), employer_pairs.pair_key(ids["keep"], ids["fifth"])])
        assert (await session.get(Task, about_id)).link_id == ids["keep"]  # type: ignore[union-attr]
        assert (await session.scalars(select(Notification))).one().link_id == ids["keep"]
        assert all(t.link_id == ids["keep"] for t in (await session.scalars(
            select(TrailEvent).where(TrailEvent.event == "note"))).all())


# --- the employer page: by hand ---------------------------------------------------------------------------------------


async def test_the_employer_page_shows_what_a_merge_moves_and_then_merges_by_hand(client):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Harbor Mutual")
        away = await make_employer(session, "Harbor Mutual Insurance", gated=True, aliases=["Harbor"])
        session.add_all([make_role(keep, "Director A"), make_role(away, "Director B"), make_role(away, "Director C"),
                         make_posting(away, "p1"), make_posting(keep, "p1"), make_posting(away, "p2"),
                         make_site(away, "https://careers.harbormutual.test")])
        await session.commit()
        keep_id, away_id = str(keep.id), str(away.id)
    shown = await client.get(f"/api/v1/employers/{away_id}/merge", params={"into": keep_id})
    assert shown.status_code == 200
    assert shown.json() == {
        "keep": {"id": keep_id, "name": "Harbor Mutual", "roles": 1, "postings": 1, "sites": 0},
        "away": {"id": away_id, "name": "Harbor Mutual Insurance", "roles": 2, "postings": 2, "sites": 1},
        "name": "Harbor Mutual Insurance", "names": ["Harbor Mutual", "Harbor"],
        "roles": 2, "postings": 1, "folded": 1, "sites": 1, "hard_pass": True, "to_gated_out": 3}
    assert (await client.get(f"/api/v1/employers/{away_id}")).status_code == 200, "looking changes nothing"
    done = await client.post(f"/api/v1/employers/{away_id}/merge", json={"into": keep_id})
    assert done.status_code == 200 and done.json()["merged"] is True
    assert done.json()["employer"]["id"] == keep_id and done.json()["employer"]["name"] == "Harbor Mutual Insurance"
    assert len(done.json()["employer"]["roles"]) == 3 and done.json()["employer"]["gated"] is True
    assert (await client.get(f"/api/v1/employers/{away_id}")).status_code == 404
    again = await client.post(f"/api/v1/employers/{away_id}/merge", json={"into": keep_id})
    assert again.status_code == 200 and again.json()["merged"] is False and again.json()["employer"]["id"] == keep_id


async def test_the_employer_page_refuses_a_merge_into_itself_or_into_nothing(client):
    async with db.session_factory()() as session:
        one = await make_employer(session, "Initech")
        await session.commit()
        one_id = str(one.id)
    ghost = str(uuid.uuid4())
    assert (await client.post(f"/api/v1/employers/{one_id}/merge", json={"into": one_id})).status_code == 422
    assert (await client.get(f"/api/v1/employers/{one_id}/merge", params={"into": one_id})).status_code == 422
    assert (await client.post(f"/api/v1/employers/{one_id}/merge", json={"into": ghost})).status_code == 404
    assert (await client.get(f"/api/v1/employers/{one_id}/merge", params={"into": ghost})).status_code == 404
    assert (await client.post(f"/api/v1/employers/{ghost}/merge", json={"into": one_id})).json()["merged"] is False
    assert (await client.post(f"/api/v1/employers/{one_id}/merge", json={})).status_code == 422
    assert (await client.get(f"/api/v1/employers/{one_id}")).status_code == 200


# --- splitting --------------------------------------------------------------------------------------------------------


async def _fabrikam_inside_northwind(session, *, gated: bool = False):
    """Northwind Labs, which an alias folded another company's roles into: two roles found under "Fabrikam" and one
    under its own name, and a posting for each of the first two and one more."""
    north = await make_employer(session, "Northwind Labs", aliases=["Fabrikam"], gated=gated)
    own = make_role(north, "Lead C")
    folded = [make_role(north, "Lead A", company="Fabrikam"),
              make_role(north, "Lead B", company="Fabrikam", status=stages.GATED_OUT)]
    session.add_all([own, *folded])
    await session.flush()
    session.add_all([make_posting(north, "1", opportunity_id=folded[0].id),
                     make_posting(north, "2", opportunity_id=folded[1].id),
                     make_posting(north, "3", opportunity_id=own.id), make_posting(north, "4")])
    await session.commit()
    return north.id, [r.id for r in folded], own.id


async def test_a_split_moves_the_chosen_roles_their_postings_and_the_names_they_were_found_under(js):
    async with db.session_factory()() as session:
        north_id, folded, _own = await _fabrikam_inside_northwind(session)
    async with db.session_factory()() as session:
        done = await employer_records.split(
            session, OWNER, north_id, role_ids=folded, new_name="Fabrikam", why="a test")
        await session.commit()
        new_id = done.employer.id
    assert (done.created, done.roles, done.postings, done.names) == (True, 2, 2, ("Fabrikam",))
    async with db.session_factory()() as session:
        by_title = {r.title: r for r in (await session.scalars(select(Opportunity))).all()}
        assert by_title["Lead A"].employer_id == by_title["Lead B"].employer_id == new_id
        assert by_title["Lead C"].employer_id == north_id, "the roles not chosen stay"
        assert by_title["Lead A"].company == "Fabrikam", "what the role says of itself is not touched"
        where_posting = {p.ext_id: p.employer_id for p in (await session.scalars(select(JobPosting))).all()}
        assert where_posting == {"1": new_id, "2": new_id, "3": north_id, "4": north_id}
        north, new = await session.get(Employer, north_id), await session.get(Employer, new_id)
        assert north is not None and new is not None
        assert north.aliases == [] and (new.name, new.owner_fields) == ("Fabrikam", ["name"])
        found = await employers.find(session, OWNER, "Fabrikam")
        assert found is not None and found.id == new_id, "a later find under the name lands on the new record"
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_split"))).one()
        assert line.sentence == "Split 2 roles out of Northwind Labs into Fabrikam: a test" and line.link_id == new_id
    async with db.session_factory()() as session:  # asked again, it does nothing more
        again = await employer_records.split(
            session, OWNER, north_id, role_ids=folded, new_name="Fabrikam", why="a test")
        await session.commit()
    assert (again.created, again.roles, again.postings) == (False, 0, 0) and again.employer.id == new_id
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_split"))).all()) == 1


async def test_a_split_can_move_roles_to_a_record_that_exists_and_take_named_sites_and_names(js):
    async with db.session_factory()() as session:
        north_id, folded, _own = await _fabrikam_inside_northwind(session)
        contoso = await make_employer(session, "Contoso")
        north = await session.get(Employer, north_id)
        assert north is not None
        north.aliases = ["Fabrikam", "Fab"]
        site = make_site(north, "https://fabrikam.test/jobs")
        session.add(site)
        await session.commit()
        contoso_id, site_id = contoso.id, site.id
    async with db.session_factory()() as session:
        done = await employer_records.split(
            session, OWNER, north_id, role_ids=folded[:1], site_ids=[site_id], names=["Fab"], into_id=contoso_id,
            why="a test", by="employer research")
        await session.commit()
    assert (done.created, done.roles, done.sites, done.names) == (False, 1, 1, ("Fab",))
    async with db.session_factory()() as session:
        north, contoso = await session.get(Employer, north_id), await session.get(Employer, contoso_id)
        assert north is not None and contoso is not None
        assert north.aliases == ["Fabrikam"], "another role is still found under it"
        assert contoso.aliases == ["Fab"]
        assert (await session.get(Site, site_id)).employer_id == contoso_id  # type: ignore[union-attr]
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_split"))).one()
        assert (line.actor, line.who) == ("agent", "employer research")


async def test_a_split_can_take_postings_by_name_and_refuses_one_that_is_not_at_the_record(js):
    async with db.session_factory()() as session:
        north_id, folded, _own = await _fabrikam_inside_northwind(session)
        four = await session.scalar(select(JobPosting.id).where(JobPosting.ext_id == "4"))
        other = await make_employer(session, "Hooli")
        stray = make_posting(other, "9")
        session.add(stray)
        await session.commit()
        stray_id = stray.id
    async with db.session_factory()() as session:
        with pytest.raises(EmployerError, match="a posting is not at Northwind Labs"):
            await employer_records.split(session, OWNER, north_id, role_ids=folded, posting_ids=[stray_id],
                                         new_name="Fabrikam", why="a test")
        done = await employer_records.split(session, OWNER, north_id, role_ids=folded, posting_ids=[four],
                                            new_name="Fabrikam", why="a test")
        await session.commit()
    assert done.postings == 3, "the two that became the roles, and the one named"
    async with db.session_factory()() as session:
        where_posting = {p.ext_id: p.employer_id for p in (await session.scalars(select(JobPosting))).all()}
        assert where_posting["4"] == where_posting["1"] == done.employer.id
        assert where_posting["3"] == north_id and where_posting["9"] != done.employer.id


async def test_a_name_a_role_left_behind_was_found_under_stays_with_the_old_record(js):
    async with db.session_factory()() as session:
        north_id, folded, _own = await _fabrikam_inside_northwind(session)
    async with db.session_factory()() as session:
        done = await employer_records.split(session, OWNER, north_id, role_ids=folded[:1], new_name="Fabrikam Works",
                                            why="a test")
        await session.commit()
    assert done.names == (), "another role found under Fabrikam stayed, so the name did"
    async with db.session_factory()() as session:
        north = await session.get(Employer, north_id)
        assert north is not None and north.aliases == ["Fabrikam"]


async def test_roles_in_gated_out_for_the_old_records_hard_pass_stay_there_and_are_counted(js):
    async with db.session_factory()() as session:
        north_id, folded, _own = await _fabrikam_inside_northwind(session, gated=True)
    async with db.session_factory()() as session:
        done = await employer_records.split(
            session, OWNER, north_id, role_ids=folded, new_name="Fabrikam", why="a test")
        await session.commit()
    assert done.still_gated_out == 1
    async with db.session_factory()() as session:
        new = await session.get(Employer, done.employer.id)
        assert new is not None and not new.gated, "the hard pass is the old record's"
        statuses = {r.title: r.status for r in (await session.scalars(select(Opportunity))).all()}
        assert statuses["Lead B"] == stages.GATED_OUT


async def test_roles_split_into_a_hard_passed_employer_go_to_gated_out(js):
    async with db.session_factory()() as session:
        north_id, folded, _own = await _fabrikam_inside_northwind(session)
        fab = await make_employer(session, "Fabrikam", gated=True, gated_reason="never")
        await session.commit()
        fab_id = fab.id
    async with db.session_factory()() as session:
        await employer_records.split(session, OWNER, north_id, role_ids=folded[:1], into_id=fab_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        moved = (await session.scalars(select(Opportunity).where(Opportunity.title == "Lead A"))).one()
        assert (moved.employer_id, moved.status) == (fab_id, stages.GATED_OUT)
        assert moved.screened_out_reason == "employer gated: never"


async def test_a_split_that_cannot_be_done_is_refused_and_changes_nothing(js):
    async with db.session_factory()() as session:
        north_id, folded, _own = await _fabrikam_inside_northwind(session)
        other = await make_employer(session, "Hooli")
        stray = make_role(other, "Elsewhere")
        session.add(stray)
        await session.commit()
        stray_id = stray.id
    async with db.session_factory()() as session:
        async def refused(match, **kw):
            with pytest.raises(EmployerError, match=match):
                await employer_records.split(session, OWNER, north_id, why="a test", **kw)
        await refused("is not at Northwind Labs", role_ids=[stray_id], new_name="Fabrikam")
        await refused("no such role", role_ids=[uuid.uuid4()], new_name="Fabrikam")
        await refused("is not an id", role_ids=["nonsense"], new_name="Fabrikam")
        await refused("choose another name", role_ids=folded, new_name="Northwind Labs")
        await refused("choose another name", role_ids=folded, into_id=north_id)
        await refused("which employer they move to", role_ids=folded)
        await refused("which employer they move to", role_ids=folded, new_name="Fabrikam", into_id=uuid.uuid4())
        await refused("give the employer a name", role_ids=folded, new_name="The Inc.")
        await refused("no such employer", role_ids=folded, into_id=uuid.uuid4())
        with pytest.raises(NoSuchEmployer):
            await employer_records.split(session, OWNER, uuid.uuid4(), role_ids=folded, new_name="Fabrikam",
                                         why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        roles = (await session.scalars(select(Opportunity).where(Opportunity.title != "Elsewhere"))).all()
        assert {r.employer_id for r in roles} == {north_id}
        assert len((await session.scalars(select(Employer))).all()) == 2, "no record was made on the way"


async def test_the_employer_page_shows_what_a_split_moves_and_then_splits_by_hand(client):
    async with db.session_factory()() as session:
        north_id, folded, _own = await _fabrikam_inside_northwind(session)
    roles = ",".join(str(r) for r in folded)
    shown = await client.get(f"/api/v1/employers/{north_id}/split", params={"roles": roles, "name": "Fabrikam"})
    assert shown.status_code == 200 and shown.json() == {
        "from": {"id": str(north_id), "name": "Northwind Labs"},
        "to": {"id": None, "name": "Fabrikam", "exists": False},
        "roles": 2, "postings": 2, "sites": 0, "names": ["Fabrikam"], "still_gated_out": 0}
    body = {"roles": [str(r) for r in folded], "name": "Fabrikam"}
    done = await client.post(f"/api/v1/employers/{north_id}/split", json=body)
    assert done.status_code == 200
    moved = done.json()
    assert (moved["created"], moved["roles"], moved["postings"], moved["names"]) == (True, 2, 2, ["Fabrikam"])
    assert moved["employer"]["name"] == "Fabrikam" and len(moved["employer"]["roles"]) == 2
    again = await client.post(f"/api/v1/employers/{north_id}/split", json=body)
    assert again.status_code == 200 and again.json()["created"] is False and again.json()["roles"] == 0
    listed = {e["name"]: e for e in (await client.get("/api/v1/employers")).json()["employers"]}
    assert (listed["Fabrikam"]["roles_total"], listed["Northwind Labs"]["roles_total"]) == (2, 1)


async def test_the_employer_page_refuses_a_split_with_nothing_chosen_or_a_name_it_already_has(client):
    async with db.session_factory()() as session:
        north_id, folded, _own = await _fabrikam_inside_northwind(session)
    roles = ",".join(str(r) for r in folded)
    path = f"/api/v1/employers/{north_id}/split"
    assert (await client.get(path, params={"roles": "", "name": "Fabrikam"})).status_code == 422
    assert (await client.post(path, json={"roles": [], "name": "Fabrikam"})).status_code == 422
    assert (await client.post(path, json={"roles": [str(folded[0])], "name": " "})).status_code == 422
    same = await client.get(f"/api/v1/employers/{north_id}/split", params={"roles": roles, "name": "Northwind Labs"})
    assert same.status_code == 422 and "choose another name" in same.json()["detail"]
    assert (await client.post(f"/api/v1/employers/{uuid.uuid4()}/split", json={
        "roles": [str(folded[0])], "name": "Fabrikam"})).status_code == 404


# --- the watch of an employer with a role in play (docs/PRD.md 29.9) --------------------------------------------------


async def _asking_to_watch(session, on: bool = True) -> None:
    """The owner's setting "watch every employer with a role in play", saved and not yet settled."""
    current = (await profile_store.current(session, OWNER)).settings
    await profile_store.save(
        session, OWNER, "settings", {**current.model_dump(mode="json"), "watch_in_play": on}, by="test")


async def _watched(session) -> list[str]:
    """The names of the employers whose site is switched on."""
    return list((await session.scalars(
        select(Employer.name).join(Site, Site.employer_id == Employer.id).where(Site.on.is_(True))
        .order_by(Employer.name))).all())


async def _leave_play(session) -> None:
    """Every role of the owner's is over (Rejected), so none is in play."""
    await session.execute(update(Opportunity).where(Opportunity.owner == OWNER).values(status=stages.REJECTED))


async def test_a_merge_settles_whom_the_app_watches_for_a_role_in_play(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech Systems", careers_url="https://careers.initech-systems.test/jobs")
        away = await make_employer(session, "Initech", careers_url="https://careers.initech.test/jobs")
        session.add_all([make_role(keep, "Old", status=stages.REJECTED), make_role(away, "Lead")])
        await session.flush()
        await where.seed_employer_sites(session, OWNER)
        await _asking_to_watch(session)  # asked for, and nothing settled since
        assert await _watched(session) == []
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        merged = await session.get(Employer, keep_id)
        assert merged is not None
        assert await _watched(session) == ["Initech Systems"], "the role that came over is in play: watched now"
        assert merged.check_directly is True and watch_record.set_by(merged) == watch_record.BY_PLAY
        assert (await employers.detail(session, OWNER, keep_id))["site"]["watched_for"] == "in_play"


async def test_a_split_settles_whom_the_app_watches_for_a_role_in_play(js):
    async with db.session_factory()() as session:
        acme = await make_employer(session, "Acme", careers_url="https://careers.acme.test/jobs")
        borealis = await make_employer(session, "Borealis", careers_url="https://careers.borealis.test/jobs")
        lead_a, lead_b = make_role(acme, "Lead A"), make_role(borealis, "Lead B")
        session.add_all([lead_a, lead_b, make_role(acme, "Old", status=stages.REJECTED)])
        await session.flush()
        await where.seed_employer_sites(session, OWNER)
        theirs = await session.scalar(select(Site).where(Site.employer_id == borealis.id))
        assert theirs is not None
        await where.switch(session, OWNER, theirs.id, True)  # the owner's own watch of Borealis
        await auto_watch.set_on(session, OWNER, True)  # and the app's, for the role in play at Acme
        assert await _watched(session) == ["Acme", "Borealis"]
        await session.commit()
        acme_id, borealis_id, lead_a_id, lead_b_id = acme.id, borealis.id, lead_a.id, lead_b.id
    async with db.session_factory()() as session:
        await employer_records.split(session, OWNER, acme_id, role_ids=[lead_a_id], new_name="Acme Labs", why="a test")
        await employer_records.split(
            session, OWNER, borealis_id, role_ids=[lead_b_id], new_name="Borealis Labs", why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        assert await _watched(session) == ["Borealis"], "the app unwatches what it watched; the owner's watch stays"
        acme = await session.get(Employer, acme_id)
        assert acme is not None
        assert acme.check_directly is False and watch_record.set_by(acme) == watch_record.BY_PLAY


async def test_a_merge_leaves_a_watch_the_owner_set_the_owners_whoever_watched_the_other(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech Systems", careers_url="https://careers.initech-systems.test/jobs")
        away = await make_employer(session, "Initech", careers_url="https://careers.initech.test/jobs")
        session.add_all([make_role(keep, "Lead A"), make_role(away, "Lead B")])
        await session.flush()
        await where.seed_employer_sites(session, OWNER)
        theirs = await session.scalar(select(Site).where(Site.employer_id == away.id))
        assert theirs is not None
        await where.switch(session, OWNER, theirs.id, True)  # the owner watches Initech
        await auto_watch.set_on(session, OWNER, True)  # the app watches Initech Systems, for its role in play
        assert await _watched(session) == ["Initech", "Initech Systems"]
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await _leave_play(session)
        await auto_watch.settle(session, OWNER)  # nothing is in play now: the app unwatches what it watched
        await session.commit()
    async with db.session_factory()() as session:
        merged = await session.get(Employer, keep_id)
        assert merged is not None
        assert await _watched(session) == ["Initech Systems"], "the owner's watch of one of them is not undone"
        assert watch_record.set_by(merged) == watch_record.BY_OWNER


async def test_a_merge_leaves_a_watch_the_app_set_the_apps_when_it_comes_from_the_other_record(js):
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Initech Systems", careers_url="https://careers.initech-systems.test/jobs")
        away = await make_employer(session, "Initech", careers_url="https://careers.initech.test/jobs")
        session.add_all([make_role(keep, "Old", status=stages.REJECTED), make_role(away, "Lead")])
        await session.flush()
        await where.seed_employer_sites(session, OWNER)
        await auto_watch.set_on(session, OWNER, True)  # the app watches Initech, for its role in play
        assert await _watched(session) == ["Initech"]
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        merged = await session.get(Employer, keep_id)
        assert merged is not None
        assert await _watched(session) == ["Initech Systems"], "its site is the one that is watched now"
        assert merged.check_directly is True and watch_record.set_by(merged) == watch_record.BY_PLAY
        await _leave_play(session)
        await auto_watch.settle(session, OWNER)
        assert await _watched(session) == [], "the app's own watch goes when no role is in play"



# --- the roles a merge leaves under another name ---------------------------------------------------------------------


async def test_a_role_is_known_under_the_name_a_merge_gave_its_employer_and_every_name_it_goes_by(js):
    """A merge renames the record it keeps and leaves each role's company as it was found, while a board is read under
    the record's name: a posting of a job the owner already has came filed under a name the role did not carry, and was
    taken for a new role. The employer's names are the employer's only: a link is still one posting only at one
    employer, and another employer with a similar name has none of them."""
    link = "https://jobs.example.test/harbor/job/3912345678"
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Harbor Mutual")
        away = await make_employer(session, "Harbor Mutual Insurance", aliases=["Harbor Insurance"])
        other = await make_employer(session, "Harbor Health")
        role = make_role(keep, "Director, Programs", status=stages.APPLIED, url=link)
        health_role = make_role(other, "Director, Programs")
        session.add_all([role, health_role])
        await session.commit()
        keep_id, away_id, role_id, health_id = keep.id, away.id, role.id, health_role.id
    async with db.session_factory()() as session:
        known = await same_role.known_roles(session, OWNER)
        assert known.find("Harbor Mutual Insurance", "Director, Programs") is None, "two employers until merged"
    async with db.session_factory()() as session:
        done = await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
        assert done.employer.name == "Harbor Mutual Insurance"
    async with db.session_factory()() as session:
        stored = await session.get(Opportunity, role_id)
        assert stored is not None and stored.company == "Harbor Mutual", "the role keeps the company it was found under"
        known = await same_role.known_roles(session, OWNER)
    assert known.find("Harbor Mutual Insurance", "Director, Programs") == role_id, "the name the record has now"
    assert known.find("Harbor Mutual Insurance, Inc.", "Director — Programs") == role_id, "in any writing of it"
    assert known.find("Harbor Insurance", "Director, Programs") == role_id, "a name it is also called"
    assert known.find("Harbor Mutual", "Director, Programs") == role_id, "the company the role was found under"
    assert known.find("Harbor Mutual Insurance", "Head of Delivery", [f"{link}?utm_source=x"]) == role_id, (
        "the same posting by its link, under the new name")
    assert known.find("Harbor Mutual Insurance", "Senior Director, Programs") is None, "another job is another role"
    assert known.find("Harbor Health", "Director, Programs") == health_id, "another employer keeps its own roles"
    assert known.find("Harbor Health", "Head of Delivery", [link]) is None, "a link is one posting at one employer"
    assert known.find("Harbor Mutual Insurance Partners", "Director, Programs") is None, "a similar name is not it"


async def test_a_name_an_employer_is_also_called_never_takes_a_key_from_a_role_found_under_it(js):
    """Every role is put in under its own company before any employer's names, so the names only add matches: the older
    role at an employer that is also called "Harbor" does not take the key of a newer role found under "Harbor"."""
    async with db.session_factory()() as session:
        called = await make_employer(session, "Harbor Mutual Insurance", aliases=["Harbor"])
        older = make_role(called, "Director, Programs")
        session.add(older)
        await session.commit()
        older_id = older.id
    async with db.session_factory()() as session:
        harbor = await make_employer(session, "Harbor")
        newer = make_role(harbor, "Director, Programs")
        session.add(newer)
        await session.commit()
        newer_id = newer.id
    async with db.session_factory()() as session:
        known = await same_role.known_roles(session, OWNER)
    assert known.find("Harbor", "Director, Programs") == newer_id
    assert known.find("Harbor Mutual Insurance", "Director, Programs") == older_id
