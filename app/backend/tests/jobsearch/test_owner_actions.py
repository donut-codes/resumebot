"""The owner's action door's actions (docs/PRD.md path 50, `jobsearch/owner_actions`): the registry and its documented
set, every write as a dry run on a seeded database changing nothing but its own row, every read changing nothing, each
action calling the service it names, each conflict a 409 naming its holder, the Activity line and `started_by`, and the
job that outlasts the wait. Everything is made up; no network, no model calls, no sleeping."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from app import db
from app import settings as app_settings
from app.errors import Conflict, DomainError
from app.jobsearch import stages
from app.jobsearch.actors import CLAUDE, YOU
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.gating import regate
from app.jobsearch.missions import core as missions
from app.jobsearch.models import ActionRun, Mission, MissionRun, Opportunity, Packet
from app.jobsearch.owner_actions import REGISTRY, Action, runner
from app.jobsearch.owner_actions.registry import Args, Ctx, Outcome
from app.jobsearch.pipeline import reanalysis
from app.jobsearch.pool import cost_report
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, requeue
from app.jobsearch.work import wake
from app.jobsearch.writing import packets
from app.models.work import TrailEvent
from app.settings import settings
from app.sites.models import Site, SiteRecipe, SiteRecording
from app.timeutil import owner_today
from tests.builders import analysis_setup
from tests.support import OWNER, let_go, load_profile, migration_head

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
SITE = "https://careers.example.test/jobs"


@pytest.fixture(autouse=True)
def acts_for_the_tester(monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)


async def _seed() -> dict[str, uuid.UUID]:
    """Three researched roles with a profile, documents and missions; a site with a recipe in force."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert role is not None
        role.analysis_state = analysis.DONE
        role.analysis = {"required": [{"item": "Programs", "class": "P", "from": []}], "preferred": [], "at": "x"}
        role.analysed_at = NOW
        site = Site(owner=OWNER, url=SITE, kind="employer", read_by="app", created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        session.add(SiteRecipe(owner=OWNER, site_id=site.id, version=1, recipe={"kind": "request"}, state="live",
                               created_at=NOW))
        await session.commit()
    return ids


async def _do(name: str, args: dict | None = None, dry: bool = False) -> dict:
    return await runner.do(OWNER, name, args, dry)


async def _snapshot(skip: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Every row of every table, as text: what a read or a dry run must leave as it found it."""
    async with db.session_factory()() as session:
        listed = await session.execute(text(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by 1"))
        tables = listed.scalars().all()
        found = {}
        for table in tables:
            if table in skip:
                continue
            rows = (await session.execute(text(f'select * from "{table}"'))).all()
            found[table] = sorted(repr(tuple(r)) for r in rows)
        return found


# --- the registry -----------------------------------------------------------------------------------------------------

#: What path 50.5 documents: the action table's first column.
DOCUMENTED = {"health", "queues", "night.summary", "role.state", "stops.list", "reanalysis.compare", "mission.run",
              "board_feed.read", "role.research", "role.packet", "requeue", "regate", "recipe.record", "recipe.retire",
              "cost.report", "profile.restore", "resume.import", "profile.read", "profile.save", "tracker.import",
              "employers.import", "mail.state", "onboarding.state", "resume.read", "resume.propose",
              "postings.sample"}


def test_the_registry_is_the_documented_set_and_actions_list_shows_every_one():
    shown = {a["name"] for a in runner.listing()["actions"]}
    assert set(REGISTRY) == shown == DOCUMENTED, "an action is registered, listed and documented, or none"
    for entry in runner.listing()["actions"]:
        assert entry["does"] and "inputs" in entry and entry["writes"] is REGISTRY[entry["name"]].writes
        assert entry["dry_run"] is entry["writes"], "every write can be asked as a dry run, no read can"


def test_the_reads_are_the_six_the_first_release_has_the_onboarding_reads_and_the_two_resume_help_reads():
    reads = {n for n, a in REGISTRY.items() if not a.writes}
    assert reads == {"health", "queues", "night.summary", "role.state", "stops.list", "reanalysis.compare",
                     "profile.read", "mail.state", "onboarding.state", "resume.read", "postings.sample"}


# One valid call of every write, in every scope it has: a new write fails the first test until it is here.
DRY = [
    ("requeue", {"scope": "stale"}), ("requeue", {"scope": "ready"}), ("requeue", {"scope": "second_impression"}),
    ("requeue", {"scope": "all"}),
    ("recipe.record", {"site": SITE}), ("recipe.record", {"site": SITE, "kind": "page"}),
    ("recipe.record", {"hosts": ["careers.example.test"], "kind": "posting"}),
    ("recipe.retire", {"site": SITE, "why": "it read the wrong links"}), ("cost.report", {}),
    ("mission.run", {"mission": "sweep_stuck"}), ("mission.run", {"mission": "read_boards"}),
    ("mission.run", {"mission": "scout_boards"}), ("mission.run", {"mission": "analyse_finds"}),
    ("mission.run", {"mission": "research_employers"}), ("mission.run", {"mission": "read_browser"}),
    ("board_feed.read", {}), ("regate", {"scope": "everything"}),
    # the seeded profile has a version 1 of each; the test saves a version 2 of the criteria first, so one has a change
    # (the seeded gates and scoring hold lines a save refuses, so a restore of them is refused: a dry run says so)
    ("profile.restore", {"kind": "gates", "version": 1}), ("profile.restore", {"kind": "criteria", "version": 1}),
    ("profile.restore", {"kind": "scoring", "version": 1}), ("profile.restore", {"kind": "settings", "version": 1}),
    ("profile.restore", {"kind": "about", "version": 1}), ("profile.restore", {"kind": "writing", "version": 1}),
    # the onboarding actions (`test_owner_onboarding.py` has each with a real file); a file that is not there is a
    # refusal a dry run says it would make
    ("resume.import", {"path": str(Path(__file__).with_name("no-such-resume.txt").resolve())}),
    ("profile.save", {"kind": "criteria", "data": {"company": "A very different place."}}),
    ("tracker.import", {"path": str(Path(__file__).with_name("no-such-tracker.xlsx").resolve())}),
    ("employers.import", {"path": str(Path(__file__).with_name("no-such-list.csv").resolve())}),
    # the owner's own words, cut at its ends, for the made-up master's one job: stored nothing, on My Resume only
    ("resume.propose", {"lines": [{"job": "northwind-lead", "kind": "new_line", "text": "ran the risk review",
                                   "said": "I ran the risk review each quarter."}]}),
]


def test_every_write_has_a_dry_run_case_here():
    assert {name for name, _ in DRY} | {"role.research", "role.packet"} == {n for n, a in REGISTRY.items() if a.writes}


@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    """A data folder with the lanes' Claude folder, empty: no session to cost."""
    (tmp_path / "claude").mkdir()
    monkeypatch.setattr(app_settings, "data_dir", lambda: tmp_path)
    return tmp_path


async def test_a_dry_run_of_every_write_changes_no_row_but_its_own_and_writes_no_activity(js, transcripts):
    ids = await _seed()
    async with db.session_factory()() as session:
        criteria = (await profile_store.current(session, OWNER)).criteria.model_dump(mode="json")
        await profile_store.save(session, OWNER, "criteria", {**criteria, "company": "A very different place."},
                                 by="owner")
        await session.commit()
    DRY_ROLE = [("role.research", {"role": str(ids["Fabrikam"])}), ("role.packet", {"role": str(ids["Hooli"])}),
                ("role.packet", {"role": str(ids["Hooli"]), "feedback": "shorter"}),
                ("role.research", {"role": str(ids["Initech"])})]
    before = await _snapshot(skip=("js_action_runs",))
    for name, args in [*DRY, *DRY_ROLE]:
        answer = await _do(name, args, dry=True)
        assert answer["ok"] is True and answer["dry_run"] is True, (name, args, answer)
        assert answer["said"].startswith("Nothing was changed"), (name, answer["said"])
        assert list(answer)[1] == "said", "what was said comes first"
        assert await _snapshot(skip=("js_action_runs",)) == before, (name, args)
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(ActionRun))).all()
        assert len(rows) == len(DRY) + len(DRY_ROLE) and all(r.dry_run and r.state == "done" for r in rows)
        assert await session.scalar(select(func.count()).select_from(TrailEvent).where(
            TrailEvent.actor == "claude")) == 0, "a dry run adds no Activity line, only its job row"


async def test_a_dry_run_says_what_a_real_call_would_do_with_counts_and_ids(js, transcripts):
    ids = await _seed()
    got = await _do("requeue", {"scope": "all"}, dry=True)
    assert got["result"]["roles"] == 1 and got["result"]["role_ids"] == [str(ids["Fabrikam"])]
    assert "would requeue 1 role for research" in got["said"]
    asked = await _do("role.research", {"role": str(ids["Fabrikam"])}, dry=True)
    assert "would" in asked["said"] and asked["result"]["already"] is False
    real = await _do("requeue", {"scope": "all"})
    assert real["result"]["roles"] == 1 and real["said"].startswith("Requeued 1 role for research")


async def test_a_dry_run_of_a_refused_call_says_it_would_be_refused_and_changes_nothing(js):
    await _seed()
    gone = await _do("role.research", {"role": str(uuid.uuid4())}, dry=True)
    assert gone["ok"] is True and "would be refused: no such role" in gone["said"]
    assert gone["result"]["would_be_refused"] == "no such role"
    busy = await _do("mission.run", {"mission": "read_browser"}, dry=True)
    assert "would be refused" in busy["said"], "the browser mission is never started by the app"


async def test_a_real_call_the_service_refuses_is_a_failed_job_in_the_services_words_and_no_activity(js):
    await _seed()
    got = await _do("role.research", {"role": str(uuid.uuid4())})
    assert got["ok"] is False and got["reason"] == "no such role" and got["state"] == "failed"
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(TrailEvent).where(
            TrailEvent.actor == "claude")) == 0


async def test_a_read_changes_nothing_and_leaves_no_row(js):
    ids = await _seed()
    before = await _snapshot()
    calls = [("health", {}), ("queues", {}), ("night.summary", {}), ("night.summary", {"night": "2026-10-08"}),
             ("role.state", {"role": str(ids["Fabrikam"])}), ("stops.list", {}), ("reanalysis.compare", {})]
    for name, args in calls:
        async with db.session_factory()() as session:
            got = await runner.read(session, OWNER, name, args)
            await session.commit()
        assert got["ok"] is True and got["action"] == name, (name, got)
    assert await _snapshot() == before, "not even a bring-up-to-date a service makes while it reads is kept"


async def test_a_role_is_found_by_its_id_its_link_or_its_job_id(js):
    ids = await _seed()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Initech"])
        assert role is not None
        role_id, link = str(role.id), role.url
        for ref in (role_id, link):
            got = await runner.read(session, OWNER, "role.state", {"role": ref})
            assert got["result"]["id"] == role_id
        with pytest.raises(DomainError, match="no role of yours"):
            await runner.read(session, OWNER, "role.state", {"role": "https://nowhere.example.test/x"})


async def test_what_an_agent_reads_of_a_role_carries_the_facts_the_row_leaves_to_the_screen_as_stored(js):
    nothing = {"answers": [], "asks": [], "possible": 20, "decided": 0, "percent": None, "total": 9, "math": 2}
    worked = {"answers": [], "asks": [], "possible": 5, "decided": 5, "percent": 40, "total": 2, "math": 2}
    day = owner_today() - timedelta(days=10)

    def role(slug, status=stages.FIRST_IMPRESSION, **more):
        return Opportunity(**{"owner": OWNER, "slug": slug, "company": f"Co {slug}", "title": f"Role {slug}",
                              "status": status, "origin": "scout", "match_key": f"co {slug}|{slug}",
                              "posted": day, "found": day, **more})

    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        made = {r.slug: r for r in (
            role("scored", fit=2, fit_breakdown=worked, fit_evidence="a stretch in the second duty",
                 comp="$90,000 - $120,000", location="Riverton, ST", move_type="hybrid",
                 browser_note="the page would not load"),
            role("undecided", fit=9, fit_breakdown=nothing),
            role("tracked", stages.APPLIED, origin="tracker", browser_note="from before it was applied to"))}
        session.add_all(made.values())
        await session.commit()  # a read rolls its session back
    seen = {}
    for slug, r in made.items():
        async with db.session_factory()() as session:
            seen[slug] = (await runner.read(session, OWNER, "role.state", {"role": str(r.id)}))["result"]["role"]
    scored, undecided, tracked = seen["scored"], seen["undecided"], seen["tracked"]
    assert (scored["fit"], scored["fit_pct"]) == (2, 40)
    assert (undecided["fit"], undecided["fit_pct"]) == (None, None), "the dash a role with no percent gets, not '9'"
    assert scored["fit_evidence"] == "a stretch in the second duty" and undecided["fit_evidence"] is None
    assert (scored["comp"], scored["where"]) == ("$90,000 - $120,000", "Riverton, ST · hybrid")
    assert (undecided["comp"], undecided["where"]) == (None, None)
    assert scored["browser_note"] == "the page would not load", "said while the role is still being got ready"
    assert tracked["browser_note"] is None, "and no longer once it has moved on"
    found = datetime.fromisoformat(scored["found_at"])
    assert timedelta(0) <= datetime.now(UTC) - found < timedelta(minutes=5), "the row was made as the scout found it"
    assert tracked["found_at"] is None, "a tracker role's found is a date only, so its hour is not known"


def test_arguments_an_action_does_not_take_are_refused_not_ignored():
    with pytest.raises(DomainError, match="requeue was sent arguments it does not take"):
        runner.check_args(REGISTRY["requeue"], {"scope": "stale", "everything": True})
    with pytest.raises(DomainError, match="scope"):
        runner.check_args(REGISTRY["requeue"], {"scope": "nonsense"})
    with pytest.raises(DomainError, match="open_apply_tabs"):
        runner.check_args(REGISTRY["mission.run"], {"mission": "open_apply_tabs"})
    with pytest.raises(DomainError, match="needs a site"):
        runner.check_args(REGISTRY["recipe.record"], {})
    with pytest.raises(DomainError, match="use act.do"):
        runner.action_named("requeue", writes=False)
    with pytest.raises(DomainError, match="use act.read"):
        runner.action_named("health", writes=True)


# --- wiring: each action calls the service it names -------------------------------------------------------------------


def _spy(monkeypatch, module, name: str) -> list[tuple]:
    """Record each call of `module.name` and let it through."""
    real, calls = getattr(module, name), []

    async def spy(*a, **kw):
        calls.append((a, kw))
        return await real(*a, **kw)

    monkeypatch.setattr(module, name, spy)
    return calls


WIRED = [
    ("requeue", {"scope": "stale"}, requeue, "reanalyse_stale"),
    ("requeue", {"scope": "all"}, requeue, "reanalyse_all"),
    ("requeue", {"scope": "ready"}, reanalysis, "mark_stage"),
    ("requeue", {"scope": "second_impression"}, reanalysis, "mark_stage"),
    ("recipe.record", {"site": SITE}, recipe_record, "request"),
    ("recipe.record", {"hosts": ["careers.example.test"], "kind": "posting"}, recipe_record, "request_host"),
    ("recipe.retire", {"site": SITE}, recipe_record, "retire"),
    ("cost.report", {}, cost_report, "report_night"),
    ("mission.run", {"mission": "sweep_stuck"}, missions, "run_now"),
    ("regate", {"scope": "everything"}, regate, "run"),
    ("profile.restore", {"kind": "criteria", "version": 1}, profile_store, "restore"),
]


@pytest.mark.parametrize(("name", "args", "module", "service"), WIRED, ids=lambda v: v if isinstance(v, str) else "")
async def test_each_action_calls_the_service_it_names(js, transcripts, monkeypatch, name, args, module, service):
    await _seed()
    calls = _spy(monkeypatch, module, service)
    got = await _do(name, args)
    assert got["ok"] is True, got
    assert len(calls) == 1, f"{name} calls {module.__name__}.{service} once"


async def test_role_research_calls_analysis_ask_and_wakes_the_analyst_once_committed(js, monkeypatch, knocks):
    ids = await _seed()
    calls = _spy(monkeypatch, analysis, "ask")
    got = await _do("role.research", {"role": str(ids["Hooli"])})
    assert got["ok"] is True and len(calls) == 1 and calls[0][0][2] == ids["Hooli"]
    assert got["result"]["state"] == analysis.WAITING and knocks == [(str(ids["Hooli"]), wake.ANALYSIS_ASKED)]
    again = await _do("role.research", {"role": str(ids["Hooli"])})
    assert again["result"]["already"] is True and len(knocks) == 1, "asking twice wakes no one the second time"


async def test_role_packet_calls_the_packet_ask_the_route_calls_and_wakes_the_writer(js, monkeypatch, knocks):
    ids = await _seed()
    calls = _spy(monkeypatch, packets, "ask")
    got = await _do("role.packet", {"role": str(ids["Hooli"]), "feedback": "shorter"})
    assert got["ok"] is True and len(calls) == 1 and got["result"]["version"] == 1
    assert knocks == [(got["result"]["packet"], "packet_asked")]
    async with db.session_factory()() as session:
        packet = await session.get(Packet, uuid.UUID(got["result"]["packet"]))
        assert packet is not None and packet.feedback == "shorter" and packet.status == "writing"


async def test_the_route_and_the_action_start_the_same_run(client):
    ids = await analysis_setup()
    del ids
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        await session.commit()
        mission = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.SWEEP))
        assert mission is not None
        mission_id = mission.id
    page = (await client.post(f"/api/v1/missions/{mission_id}/run")).json()
    got = (await _do("mission.run", {"mission": "sweep_stuck"}))["result"]["run"]
    assert set(page) == set(got) and page["outcome"] == got["outcome"] == "done"
    assert (page["started_by"], got["started_by"]) == (YOU, CLAUDE) == ("you", "claude")
    async with db.session_factory()() as session:
        by = (await session.scalars(select(MissionRun.started_by).where(MissionRun.mission_id == mission_id))).all()
        assert sorted(by) == ["claude", "you"]


async def test_the_actions_own_refusals_are_the_services_own(js):
    """Claude work inside the peak hours the owner chose to avoid is refused as the route refuses it."""
    await _seed()
    async with db.session_factory()() as session:
        current = (await profile_store.current(session, OWNER)).settings.model_dump(mode="json")
        await profile_store.save(session, OWNER, "settings", {**current, "avoid_peak": True, "peak_start": "00:00",
                                                              "peak_end": "23:59", "peak_zone": "UTC"}, by="test")
        await session.commit()
    got = await _do("mission.run", {"mission": "scout_boards"})
    assert got["ok"] is False and "peak hours" in got["reason"].lower()


# --- conflicts: a second of the same thing is a 409 that names what holds it ------------------------------------------


async def _running_job(action: str, **fields) -> uuid.UUID:
    async with db.session_factory()() as session:
        row = ActionRun(owner=OWNER, action=action, arguments={}, dry_run=False, by=CLAUDE, state="running",
                        started=NOW, **fields)
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.parametrize(("name", "args"), [
    ("requeue", {"scope": "stale"}), ("regate", {"scope": "everything"}), ("cost.report", {}), ("board_feed.read", {}),
    ("profile.restore", {"kind": "gates", "version": 1})])
async def test_a_second_of_the_same_action_is_refused_naming_the_job_that_runs(js, transcripts, name, args):
    await _seed()
    job = await _running_job(name)
    with pytest.raises(Conflict) as refused:
        await _do(name, args)
    assert refused.value.status == 409 and str(job) in str(refused.value) and "running already" in str(refused.value)
    dry = await _do(name, args, dry=True)
    assert dry["ok"] is True and "would be refused" in dry["said"] and str(job) in dry["said"]
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(ActionRun).where(
            ActionRun.action == name, ActionRun.dry_run.is_(False))) == 1, "a refused call leaves no row"




async def test_a_mission_already_running_is_a_409_naming_its_run(js):
    await _seed()
    async with db.session_factory()() as session:
        mission = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.READ))
        assert mission is not None
        run = MissionRun(owner=OWNER, mission_id=mission.id, started_by="schedule", started_at=datetime.now(UTC),
                         outcome="running", summary="Reading.", started={"reading": True})
        session.add(run)
        await session.commit()
        run_id = run.id
    with pytest.raises(Conflict) as refused:
        await _do("mission.run", {"mission": "read_boards"})
    assert refused.value.status == 409 and str(run_id) in str(refused.value) and "running already" in str(refused.value)
    assert (await _do("mission.run", {"mission": "read_boards"}, dry=True))["result"]["would_be_refused"]


async def test_a_packet_already_being_written_is_a_409_naming_it(js):
    ids = await _seed()
    first = await _do("role.packet", {"role": str(ids["Hooli"])})
    with pytest.raises(Conflict) as refused:
        await _do("role.packet", {"role": str(ids["Hooli"])})
    assert refused.value.status == 409 and first["result"]["packet"] in str(refused.value)
    assert "being written already" in str(refused.value)


async def test_a_recording_already_asked_is_a_409_naming_the_site(js):
    await _seed()
    assert (await _do("recipe.record", {"site": SITE}))["ok"] is True
    with pytest.raises(Conflict) as refused:
        await _do("recipe.record", {"site": SITE})
    assert refused.value.status == 409 and SITE in str(refused.value) and "already asked" in str(refused.value)
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(SiteRecording)) == 1
    host = {"hosts": ["careers.example.test"], "kind": "posting"}
    async with db.session_factory()() as session:
        session.add(SiteRecording(owner=OWNER, site_id=None, host="careers.example.test", reason="first",
                                  state="requested", requested_at=NOW, url=SITE))
        await session.commit()
    with pytest.raises(Conflict, match="careers.example.test"):
        await _do("recipe.record", host)


# --- Activity and who started it --------------------------------------------------------------------------------------


async def test_a_real_write_adds_one_activity_line_as_claude_with_the_job_in_its_detail(js):
    ids = await _seed()
    got = await _do("requeue", {"scope": "all"})
    async with db.session_factory()() as session:
        lines = (await session.scalars(select(TrailEvent).where(TrailEvent.actor == "claude"))).all()
        [line] = lines
        assert (line.who, line.actor) == ("Claude (on demand)", "claude")
        assert line.sentence == "Claude (on demand) requeued 1 role for research (all)"
        assert got["job"] in (line.detail or "") and "requeue" in (line.detail or "")
        job = await session.get(ActionRun, uuid.UUID(got["job"]))
        assert job is not None and (job.by, job.state, job.dry_run) == ("claude", "done", False)
        assert job.ended is not None and job.result["roles"] == 1 and job.arguments == {"scope": "all"}
    role_line = await _do("role.research", {"role": str(ids["Hooli"])})
    async with db.session_factory()() as session:
        mine = (await session.scalars(select(TrailEvent).where(
            TrailEvent.actor == "claude", TrailEvent.link_id == ids["Hooli"]))).all()
        assert len(mine) == 1 and role_line["job"] in (mine[0].detail or "")


# --- jobs: the wait, the progress, the restart ------------------------------------------------------------------------


class _NoArgs(Args):
    pass


@pytest.fixture
def slow_action(monkeypatch):
    """A made-up write that waits for the test to let it go; the wait for an answer is nothing."""
    gate, begun = asyncio.Event(), asyncio.Event()

    async def run(ctx: Ctx, args: _NoArgs) -> Outcome:
        await ctx.say("halfway")
        begun.set()
        await gate.wait()
        return Outcome({"done": 1}, did="finished the slow thing", would="finish the slow thing")

    monkeypatch.setitem(REGISTRY, "test.slow", Action("test.slow", "slow", _NoArgs, run, writes=True, long=True))
    monkeypatch.setattr(runner, "WAIT_SECONDS", 0.0)
    return SimpleNamespace(gate=gate, begun=begun)


async def test_a_long_action_answers_with_a_job_and_act_job_follows_it_to_the_result(js, slow_action):
    got = await _do("test.slow")
    assert got["state"] == "running" and "not done yet" in got["said"] and got["job"]
    await slow_action.begun.wait()
    async with db.session_factory()() as session:
        shown = await runner.job(session, OWNER, got["job"])
        assert shown["state"] == "running" and shown["progress"] == "halfway"
    slow_action.gate.set()
    await runner.settle()
    async with db.session_factory()() as session:
        shown = await runner.job(session, OWNER, got["job"])
        listed = await runner.job(session, OWNER, None)
    assert (shown["ok"], shown["state"], shown["said"]) == (True, "done", "Finished the slow thing.")
    assert shown["result"] == {"changed": True, "done": 1} and shown["ended"]
    assert [j["job"] for j in listed["jobs"]] == [got["job"]]


async def test_a_job_that_fails_on_the_apps_side_says_so_and_is_failed(js, monkeypatch):
    async def run(ctx: Ctx, args: _NoArgs) -> Outcome:
        raise ValueError("a bug")

    monkeypatch.setitem(REGISTRY, "test.bug", Action("test.bug", "bug", _NoArgs, run, writes=True))
    got = await _do("test.bug")
    assert got["ok"] is False and got["state"] == "failed" and "on its side" in got["reason"]
    assert "a bug" not in got["reason"], "the cause is in the log"


async def test_a_restart_ends_a_running_job_as_failed(js):
    job = await _running_job("board_feed.read")
    async with db.session_factory()() as session:
        assert await runner.fail_interrupted(session, OWNER) == 1
        await session.commit()
        shown = await runner.job(session, OWNER, str(job))
    assert shown["state"] == "failed" and "restarted" in shown["reason"]


async def test_a_missions_job_follows_the_runs_own_progress_and_ends_with_the_run(js):
    await _seed()
    async with db.session_factory()() as session:
        mission = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.SWEEP))
        assert mission is not None
        run = MissionRun(owner=OWNER, mission_id=mission.id, started_by="claude", started_at=datetime.now(UTC),
                         outcome="running", summary="Sweeping.", started={"target": 10, "done": 4})
        session.add(run)
        await session.flush()
        view = missions.run_view(run, missions.SWEEP)
        row = ActionRun(owner=OWNER, action="mission.run", arguments={}, by=CLAUDE, state="running", started=NOW,
                        result={"run": view})
        session.add(row)
        await session.commit()
        job_id, run_id = row.id, run.id
    assert REGISTRY["mission.run"].follow is not None
    async with db.session_factory()() as session:
        shown = await runner.job(session, OWNER, str(job_id))
        await session.commit()
    assert shown["state"] == "running" and shown["progress"] == "4 of 10"
    async with db.session_factory()() as session:
        entry = await session.get(MissionRun, run_id)
        assert entry is not None
        entry.outcome, entry.ended_at = "done", datetime.now(UTC)
        await session.commit()
    async with db.session_factory()() as session:
        shown = await runner.job(session, OWNER, str(job_id))
        await session.commit()
    assert shown["state"] == "done" and shown["ended"] and shown["result"]["run"]["outcome"] == "done"


# --- the health read's backup line ------------------------------------------------------------


def _backup_file(folder: Path, name: str, at: datetime) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"x")
    os.utime(folder / name, (at.timestamp(), at.timestamp()))


def test_health_sees_a_sqlite_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "data_dir", lambda: tmp_path)
    _backup_file(tmp_path / "backups", "resumebot-20261101-120000.sqlite3", NOW - timedelta(hours=2))
    from app.jobsearch.owner_actions import reads

    found = reads._last_backup(NOW)
    assert found["found"] is True and found["age_hours"] == 2.0


def test_health_takes_the_newest_backup_of_either_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "data_dir", lambda: tmp_path)
    _backup_file(tmp_path / "backups", "resumebot-20261101-120000.dump", NOW - timedelta(hours=30))
    _backup_file(tmp_path / "backups", "resumebot-20261102-120000.sqlite3", NOW - timedelta(hours=5))
    from app.jobsearch.owner_actions import reads

    assert reads._last_backup(NOW)["age_hours"] == 5.0
    _backup_file(tmp_path / "backups", "resumebot-20261103-120000.dump", NOW - timedelta(hours=1))
    assert reads._last_backup(NOW)["age_hours"] == 1.0


# --- a backup is restorable ------------------------------------------------------------


async def test_a_sqlite_backup_restores_into_a_fresh_folder_and_opens(schema_template, tmp_path, monkeypatch):
    """Backup, restore and smoke on a copy. The source is a file the app's own schema made; the backup
    and restore are the scripts' own code; the restored file is opened through the app's db layer."""
    scripts = str(Path(__file__).resolve().parents[4] / "scripts")
    monkeypatch.syspath_prepend(scripts)
    import backup_local
    import restore_local

    backups = tmp_path / "backups"
    backups.mkdir()
    backup = backup_local.backup_sqlite(schema_template, backups, 3, lambda _line: None)

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    live = fresh / "app.db"
    (fresh / ".env").write_text(f"JOBSEARCH_DATABASE_URL=sqlite+aiosqlite:///{live.as_posix()}\n", encoding="utf-8")
    assert restore_local.restore_once(fresh, backup, lambda _line: None, lambda _url: False) == 0

    engine = db.configure(db.sqlite_url(live))
    try:
        assert await db.ping()
        async with db.session_factory()() as session:
            tables = set((await session.scalars(text("SELECT name FROM sqlite_master WHERE type = 'table'"))).all())
            assert db.OWNED_TABLES <= tables
            assert await session.scalar(select(func.count(Opportunity.id))) == 0
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == migration_head()
    finally:
        await engine.dispose()
        await let_go()
