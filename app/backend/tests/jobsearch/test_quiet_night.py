"""The night's step of the quiet rules:
`read_boards.bpmn` gains "Move roles that went quiet" between the filter and the descriptions, the action wraps
`quiet.run` in a savepoint so a pass that cannot finish never stops the night's read, and a whole read moves the roles
and counts them in its run. Adapters are stand-ins; no network. Synthetic data only: every employer, title and address
here is made up (reserved example domains)."""

import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest
from process_engine.model import SERVICE
from process_engine.models import COMPLETED, ProcHistory, ProcInstance
from sqlalchemy import select, update

from app import db
from app.config import settings as app_settings
from app.jobsearch import actions, adapters, process, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import boards_night
from app.jobsearch.missions import core as missions
from app.jobsearch.models import JobPosting, Opportunity, StageMove
from app.jobsearch.pipeline import quiet
from app.models.work import TrailEvent
from app.settings import settings
from app.sites import where
from app.timeutil import owner_today
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, gate_says, load_profile

TITLE = "Director, Data Governance"
SEEN = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
BPMN = Path(app_settings.playbooks_path) / "jobsearch" / "read_boards.bpmn"
NS = {"bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL"}
CLAUSE = "the roles whose posting the employer took down, or that heard nothing back, moved to Gone Quiet"


@pytest.fixture(autouse=True)
def the_day(monkeypatch) -> date:
    """The owner's days are UTC, and today is one fixed day for the whole test, so no test depends on the hour it runs
    (or on a midnight passing between two of its steps)."""
    monkeypatch.setattr(settings, "timezone", "UTC")
    day = owner_today()
    monkeypatch.setattr(quiet, "owner_today", lambda now=None: day)
    return day


# --- the playbook ----------------------------------------------------------------------------------------------------


def test_the_step_sits_between_the_filter_and_the_descriptions_with_the_flows_the_spec_names():
    library = process.build_library().get(process.READ_BOARDS)
    step = library.elements["Quiet_roles"]
    assert (step.kind, step.name, step.action, step.output) == (
        SERVICE, "Move roles that went quiet", "jobs.quiet_roles", "quiet")
    assert step.lane == library.elements["Filter_postings"].lane == library.elements["Read_descriptions"].lane
    flows = {f.id: (f.source, f.target) for f in library.flows.values()}
    assert flows["S_Filter_postings__Quiet_roles"] == ("Filter_postings", "Quiet_roles")
    assert flows["S_Quiet_roles__Read_descriptions"] == ("Quiet_roles", "Read_descriptions")
    assert "S_Filter_postings__Read_descriptions" not in flows, "the old flow is gone"
    assert ("Filter_postings", "Read_descriptions") not in set(flows.values())
    # The rest of the night is as it was.
    assert flows["S_Layoff_notices__Filter_postings"] == ("Layoff_notices", "Filter_postings")
    assert flows["S_Read_descriptions__End_ready"] == ("Read_descriptions", "End_ready")


def test_the_lane_lists_the_step_between_the_filter_and_the_descriptions():
    lane = ET.parse(BPMN).getroot().find(".//bpmn:lane[@id='Lane_read_app']", NS)
    assert lane is not None
    refs = [ref.text for ref in lane.findall("bpmn:flowNodeRef", NS)]
    at = refs.index("Filter_postings")
    assert refs[at:at + 3] == ["Filter_postings", "Quiet_roles", "Read_descriptions"]
    assert refs.count("Quiet_roles") == 1


def test_the_data_model_holds_what_the_step_answers():
    model = process.build_library().get(process.READ_BOARDS).data_model
    assert model.fields["quiet"].type == "object"
    for path in ("quiet.went", "quiet.silent", "quiet.days"):
        assert model.fields[path].type == "number", path
        assert model.fields[path].description, f"{path} says what it counts"


def test_the_step_the_process_and_the_head_of_the_file_say_in_words_what_the_night_now_does():
    library = process.build_library().get(process.READ_BOARDS)
    docs = library.elements["Quiet_roles"].documentation or ""
    assert docs.startswith(
        "Actor: Job search.\n"
        "Trigger: The new postings were filtered, so a posting that came back as a repost is linked to its role.\n"
        "Rules:\n1. ")
    assert "\nOutputs: `quiet.went`, `quiet.silent`, `quiet.days`.\nWhat the owner sees: " in docs
    assert CLAUSE in (library.documentation or ""), "the process's own words"
    raw = BPMN.read_text(encoding="utf-8")
    assert CLAUSE in raw[:raw.index("-->")], "and the comment at the head of the file"


def test_the_action_is_registered_with_the_owner_in_and_the_three_counts_out():
    spec = process.registry.get("jobs.quiet_roles")
    assert spec is not None
    assert spec.inputs == ("owner",) and spec.outputs == ("went", "silent", "days")
    assert spec.source == "backend/app/jobsearch/pipeline/quiet.py"
    assert spec.description == (
        "Moves a role to Gone Quiet when the employer took its posting down, or an Applied role has had no word for "
        "the owner's number of days. Code only.")


def test_the_mission_card_says_it_also_moves_roles_to_gone_quiet():
    purpose = missions.kind_of(missions.READ).purpose()
    assert ("It also moves a role to Gone Quiet when its posting has gone or an application has heard nothing back."
            in purpose)
    assert purpose.endswith("No Claude."), "and still ends on what it never uses"


# --- the action ------------------------------------------------------------------------------------------------------


def _role(slug: str, status: str = stages.APPLIED, **more) -> Opportunity:
    return Opportunity(
        owner=OWNER, slug=slug, company=more.pop("company", "Fabrikam"), title=more.pop("title", f"{TITLE} {slug}"),
        status=status, match_key=more.pop("key", f"fabrikam|{slug}"), origin="scout", fit=3, **more)


async def _number(session, days) -> None:
    await load_profile(session, OWNER, settings={**PROFILE["settings"], "no_reply_days": days})


async def _applied_long_ago(session, slug: str, days: int = 40) -> Opportunity:
    """An Applied role that became Applied that many of the owner's days ago (its stage move says so)."""
    role = _role(slug)
    session.add(role)
    await session.flush()
    at = datetime.combine(owner_today() - timedelta(days=days), time(12), tzinfo=UTC)
    await session.execute(update(StageMove).where(StageMove.role_id == role.id).values(at=at))
    return role


async def _status(slug: str) -> tuple[str, str | None]:
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity).where(Opportunity.owner == OWNER, Opportunity.slug == slug))
        assert role is not None
        return role.status, role.ended_reason


async def test_the_action_returns_the_three_counts_the_playbook_names(js):
    async with db.session_factory()() as session:
        await _number(session, 30)
        await _applied_long_ago(session, "silent")
        answer = await actions.quiet_roles(session, None, {"owner": OWNER})
        await session.commit()  # the night's driver commits after the step
    assert answer == {"went": 0, "silent": 1, "days": 30}
    assert await _status("silent") == ("gone_quiet", "no_reply")


async def test_a_pass_that_cannot_finish_is_logged_returns_zeros_and_leaves_the_roles_as_they_were(
        js, monkeypatch, caplog):
    async def ends_one_then_fails(session, owner, *, today=None):
        role = await session.scalar(select(Opportunity).where(Opportunity.owner == owner))
        await quiet.end_role(session, owner, role, reason="posting_gone", event="posting_went", sentence="x",
                             detail=None)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(quiet, "run", ends_one_then_fails)
    async with db.session_factory()() as session:
        await _number(session, 30)
        await _applied_long_ago(session, "waiting")
        await session.commit()
    async with db.session_factory()() as session:
        answer = await actions.quiet_roles(session, None, {"owner": OWNER})
        await session.commit()
    assert answer == {"went": 0, "silent": 0, "days": None}
    assert "moving the roles that went quiet failed" in caplog.text
    assert await _status("waiting") == ("applied", None), "what the pass did before it failed was rolled back"
    async with db.session_factory()() as session:
        assert (await session.scalars(select(TrailEvent).where(TrailEvent.event == "posting_went"))).all() == []


# --- the whole read --------------------------------------------------------------------------------------------------


@dataclass
class Listing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class Board:
    postings: list[dict] = field(default_factory=list)

    async def read(self, fetch, board):
        return Listing(self.postings)

    async def detail(self, fetch, board, posting):
        return {"text": f"The whole posting for {posting['title']}."}


def _listed(ext_id: str, title: str, **more) -> dict:
    return {"ext_id": ext_id, "title": title, "location": "Remote", "work_mode": "remote", "comp": None,
            "comp_min": None, "comp_max": None, "posted": None, "url": f"https://jobs.example.test/{ext_id}",
            "apply_url": None, "text": None, "department": None, **more}


def _fetch():
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


@pytest.fixture
def listed(monkeypatch):
    """Every step reads through a mock (no network), and the employer's board lists what a test puts in `postings`."""
    monkeypatch.setattr(polite, "make", _fetch)
    fake = Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: fake})
    return fake


async def _world(*, days: int | None = None) -> uuid.UUID:
    """An employer whose board the night reads, and the owner's profile, with the no-reply number set or blank."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"], "search_plan": ["data governance"]},
                           settings={**PROFILE["settings"], "no_reply_days": days})
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        fab.ats, fab.board = EXAMPLE_SYSTEM, {"slug": "fabrikam"}
        fab.board_url, fab.check_directly = "https://job-boards.feedone.test/fabrikam", True
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
        return fab.id


async def _applied_with_posting(slug: str, employer_id: uuid.UUID, ext_id: str) -> uuid.UUID:
    """An Applied role at the employer, linked to a posting its board listed when it was last read."""
    async with db.session_factory()() as session:
        role = _role(slug, title=TITLE, key=match_key("Fabrikam", TITLE), employer_id=employer_id)
        session.add(role)
        await session.flush()
        session.add(JobPosting(
            owner=OWNER, employer_id=employer_id, ats=EXAMPLE_SYSTEM, ext_id=ext_id, company="Fabrikam", title=TITLE,
            match_key=match_key("Fabrikam", TITLE), url=f"https://jobs.example.test/{ext_id}", first_seen=SEEN,
            last_seen=SEEN, links=[], opportunity_id=role.id))
        await session.commit()
        return role.id


async def _completed_steps(instance_id: str) -> list[str]:
    async with db.session_factory()() as session:
        return list(await session.scalars(
            select(ProcHistory.from_element).where(
                ProcHistory.instance_id == uuid.UUID(instance_id), ProcHistory.kind == "complete")
            .order_by(ProcHistory.seq)))


async def _instance() -> ProcInstance:
    async with db.session_factory()() as session:
        return (await session.scalars(select(ProcInstance).where(
            ProcInstance.process_key == process.READ_BOARDS))).one()


async def test_a_whole_read_runs_the_step_after_the_filter_and_before_the_descriptions(js, listed):
    await _world()
    listed.postings = [_listed("1", TITLE)]
    outcome, _summary, counts = await boards_night.read_boards(OWNER, details_per_night=5)
    assert outcome == "done"
    steps = await _completed_steps(counts["instances"][0])
    at = steps.index("Filter_postings")
    assert steps[at:at + 3] == ["Filter_postings", "Quiet_roles", "Read_descriptions"]
    instance = await _instance()
    assert instance.status == COMPLETED
    assert instance.data["quiet"] == {"went": 0, "silent": 0, "days": None}, "the answer, as the playbook names it"


async def test_a_repost_the_filter_links_to_its_role_leaves_the_role_where_it_is(js, listed):
    """The step runs after the filter because the filter is what links a repost to its role (R1.5): the old posting is
    gone from the board and the new one is the same job under another address."""
    fab = await _world()
    role_id = await _applied_with_posting("kept", fab, "old")
    listed.postings = [_listed("new", TITLE), _listed("other", "Payroll Specialist")]
    outcome, _summary, counts = await boards_night.read_boards(OWNER, details_per_night=5)
    assert outcome == "done"
    async with db.session_factory()() as session:
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert rows["old"].closed_at is not None, "the read that could have returned it no longer finds it"
    assert rows["new"].opportunity_id == role_id and rows["new"].closed_at is None, "the filter linked the repost"
    assert await _status("kept") == ("applied", None)
    assert counts["posting_went"] == 0


async def test_a_role_whose_posting_is_only_gone_moves_with_its_evidence_and_the_report_has_no_line_for_it(js, listed):
    fab = await _world()
    await _applied_with_posting("gone", fab, "old")
    listed.postings = [_listed("other", "Payroll Specialist")]
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=5)
    assert outcome == "done"
    assert await _status("gone") == ("gone_quiet", "posting_gone")
    assert (counts["posting_went"], counts["no_reply"]) == (1, 0)
    assert "moved" not in summary and "Gone Quiet" not in summary, "rule 1 has no line in the run's report"
    async with db.session_factory()() as session:
        (line,) = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "posting_went"))).all()
    assert line.sentence == "Gone Quiet: the posting went."
    assert line.detail is not None and f'no longer lists "{TITLE}"' in line.detail


async def test_applied_roles_with_no_word_move_and_the_report_says_so_before_what_was_stopped(js, listed):
    await _world(days=30)
    async with db.session_factory()() as session:
        await _applied_long_ago(session, "one")
        await _applied_long_ago(session, "two", 31)
        await _applied_long_ago(session, "recent", 29)
        await session.commit()
    listed.postings = [_listed("1", TITLE), _listed("3", "Data Governance Lead", location="Greater Gotham",
                                                    work_mode="onsite")]
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=5)
    assert outcome == "done"
    assert [await _status(s) for s in ("one", "two", "recent")] == [
        ("gone_quiet", "no_reply"), ("gone_quiet", "no_reply"), ("applied", None)]
    assert (counts["posting_went"], counts["no_reply"], counts["no_reply_days"]) == (0, 2, 30)
    assert summary == (
        "Read 1 employer board: 2 new postings.\n1 passed the filter.\n"
        f"2 Applied roles moved to Gone Quiet: no word in 30 days.\nStopped:\n1 {gate_says()['location']}")


async def test_with_the_number_blank_no_applied_role_moves_and_the_report_says_nothing_of_it(js, listed):
    await _world(days=None)
    async with db.session_factory()() as session:
        await _applied_long_ago(session, "old", 400)
        await session.commit()
    listed.postings = [_listed("1", TITLE)]
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=5)
    assert outcome == "done" and await _status("old") == ("applied", None)
    assert (counts["posting_went"], counts["no_reply"], counts["no_reply_days"]) == (0, 0, None)
    assert "Gone Quiet" not in summary


async def test_a_step_that_raises_leaves_the_nights_read_done_and_the_roles_unchanged(js, listed, monkeypatch, caplog):
    await _world(days=30)
    async with db.session_factory()() as session:
        await _applied_long_ago(session, "waiting")
        await session.commit()

    async def ends_one_then_fails(session, owner, *, today=None):
        role = await session.scalar(select(Opportunity).where(Opportunity.owner == owner))
        await quiet.end_role(session, owner, role, reason="no_reply", event="no_reply", sentence="x", detail=None)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(quiet, "run", ends_one_then_fails)
    listed.postings = [_listed("1", TITLE)]
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=5)
    assert outcome == "done", "the read goes on"
    assert counts["details"] == 1, "and the step after it did its work"
    assert "moving the roles that went quiet failed" in caplog.text
    assert await _status("waiting") == ("applied", None)
    assert (counts["posting_went"], counts["no_reply"]) == (0, 0)
    assert "Gone Quiet" not in summary
    assert (await _instance()).status == COMPLETED
