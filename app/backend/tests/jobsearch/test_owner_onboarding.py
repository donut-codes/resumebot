"""The owner door's onboarding actions (docs/PRD.md path 50.14), the ones a new owner's own Claude uses to set the app
up by talking with them: `resume.import` (starts the page's import and answers where the owner confirms it, never
confirming it), `profile.read` and `profile.save` (a part of the profile as a new version, as a save on screen),
`tracker.import` and `employers.import` (the owner's files, through the functions the command line calls), and
`mail.state` (no password ever goes through the door). Everything is made up; no network, no model calls, no
sleeping."""

import csv
import io
from pathlib import Path
from typing import get_args

import pytest
from sqlalchemy import func, select

from app import db, setup_cli
from app.errors import Conflict, DomainError
from app.jobsearch.actors import CLAUDE
from app.jobsearch.employers import employer_list
from app.jobsearch.imports import bring_in
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.missions import catch_up, record
from app.jobsearch.models import ActionRun, EmployerListEntry, MailLogin, Opportunity, ParseJob, ProfileVersion
from app.jobsearch.owner_actions import REGISTRY, onboarding, runner
from app.jobsearch.profile import after_save
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.base import KINDS
from app.models.work import TrailEvent
from app.settings import settings
from tests.builders import MINUTE, parse_runner, save_documents
from tests.resume_files import RESUME_LINES, docx_resume
from tests.support import FIVE, OWNER, PROFILE, load_profile, tracker_row, tracker_workbook

RESUME = "\n".join(RESUME_LINES)


@pytest.fixture(autouse=True)
def acts_for_the_tester(monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)


async def _do(name: str, args: dict | None = None, dry: bool = False) -> dict:
    return await runner.do(OWNER, name, args, dry)


async def _read(name: str, args: dict | None = None) -> dict:
    async with db.session_factory()() as session:
        return await runner.read(session, OWNER, name, args)


async def _count(model) -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def _versions(kind: str) -> list[ProfileVersion]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(ProfileVersion).where(
            ProfileVersion.owner == OWNER, ProfileVersion.kind == kind).order_by(ProfileVersion.version))).all())


def _spy(monkeypatch, module, name: str) -> list[tuple]:
    """Record each call of `module.name` and let it through."""
    real, calls = getattr(module, name), []

    async def spy(*a, **kw):
        calls.append((a, kw))
        return await real(*a, **kw)

    monkeypatch.setattr(module, name, spy)
    return calls


def _write(tmp_path: Path, name: str, data: bytes | str) -> str:
    path = tmp_path / name
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return str(path)


# --- the registry -----------------------------------------------------------------------------------------------------

ONBOARDING = {"resume.import", "profile.read", "profile.save", "tracker.import", "employers.import", "mail.state"}


def test_the_onboarding_actions_are_registered_and_say_which_of_them_write():
    assert ONBOARDING <= set(REGISTRY)
    assert {n for n in ONBOARDING if REGISTRY[n].writes} == {"resume.import", "profile.save", "tracker.import",
                                                             "employers.import"}


def test_every_tool_the_status_lines_name_is_an_action_that_takes_what_they_say():
    """`resumebot status --for-claude` names the tool for each step; a name the door lacks sends Claude nowhere."""
    for tool in setup_cli.TOOLS.values():
        for name in tool.names:
            assert name in REGISTRY, f"{tool} names {name}, which is not an action"
    search = REGISTRY["mission.run"]
    assert runner.check_args(search, {"mission": setup_cli.SEARCH_MISSION})
    assert setup_cli.SEARCH_MISSION == record.SCOUT, "the first search is the mission the status reads a run of"


def test_the_kinds_profile_save_takes_are_the_profiles_four_and_never_a_document():
    assert get_args(onboarding.SaveKind) == KINDS
    for kind in ("resume", "about", "writing", "nonsense"):
        with pytest.raises(DomainError, match="kind"):
            runner.check_args(REGISTRY["profile.save"], {"kind": kind, "data": {"x": 1}})


def test_a_file_is_named_by_its_whole_path_and_nothing_else():
    for path in ("resume.docx", "docs/resume.docx", "../resume.docx"):
        with pytest.raises(DomainError, match="whole path"):
            runner.check_args(REGISTRY["resume.import"], {"path": path})
    with pytest.raises(DomainError, match="path"):
        runner.check_args(REGISTRY["resume.import"], {})
    with pytest.raises(DomainError, match="does not take"):
        runner.check_args(REGISTRY["resume.import"], {"path": str(Path(__file__).resolve()), "text": RESUME})


# --- resume.import ----------------------------------------------------------------------------------------------------


async def test_resume_import_starts_the_reading_wakes_the_runner_and_says_where_the_owner_confirms(
        js, tmp_path, knocks):
    await parse_runner(seen=MINUTE)
    path = _write(tmp_path, "resume.txt", RESUME)

    got = await _do("resume.import", {"path": path})

    assert got["ok"] is True and got["state"] == "done", got
    job_id = got["result"]["parse_job"]
    assert got["result"]["confirm_at"] == f"{onboarding.page()}#/me?import={job_id}"
    assert "Do not confirm it for them" in got["result"]["how"]
    async with db.session_factory()() as session:
        job = (await session.scalars(select(ParseJob))).one()
    assert (str(job.id), job.owner, job.kind, job.state, job.text) == (job_id, OWNER, "resume", "waiting", RESUME)
    assert knocks == [(job_id, "parse_asked")], "the runner is woken once the commit has landed"


async def test_resume_import_never_saves_the_resume_and_leaves_the_confirm_to_the_owner(js, tmp_path, knocks):
    await parse_runner(seen=MINUTE)
    got = await _do("resume.import", {"path": _write(tmp_path, "resume.txt", RESUME)})
    assert got["ok"] is True
    async with db.session_factory()() as session:
        assert await profile_store.newest_version(session, OWNER, "resume") == 0, "no version of the master was made"
        assert (await session.scalar(select(ParseJob.fields))) is None
    assert {n for n in REGISTRY if n.startswith("resume.")} == {"resume.import", "resume.read", "resume.propose"}
    assert not [n for n in REGISTRY if "accept" in n or n == "resume.save"], "no action saves the master resume"


async def test_resume_import_reads_a_word_file_as_the_page_does(js, tmp_path, knocks):
    await parse_runner(seen=MINUTE)
    got = await _do("resume.import", {"path": _write(tmp_path, "My Resume.docx", docx_resume())})
    assert got["ok"] is True, got
    async with db.session_factory()() as session:
        job = (await session.scalars(select(ParseJob))).one()
    assert "Builds programs that people use" in job.text and job.kind == "resume"


async def test_resume_import_is_one_line_in_activity_as_claudes_and_keeps_only_the_path_in_its_record(
        js, tmp_path, knocks):
    await parse_runner(seen=MINUTE)
    path = _write(tmp_path, "resume.txt", RESUME)
    await _do("resume.import", {"path": path})
    async with db.session_factory()() as session:
        row = (await session.scalars(select(ActionRun))).one()
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.actor == CLAUDE))).one()
    assert row.arguments == {"path": path} and RESUME_LINES[2] not in str(row.result)
    assert line.who == "Claude (on demand)" and "started reading the owner's resume file resume.txt" in line.sentence


async def test_resume_import_is_refused_in_words_when_no_agent_can_take_it_and_makes_no_job(js, tmp_path, knocks):
    got = await _do("resume.import", {"path": _write(tmp_path, "resume.txt", RESUME)})  # no runner has checked in
    assert got["ok"] is False and got["state"] == "failed" and got["reason"], got
    assert await _count(ParseJob) == 0 and knocks == []


async def test_resume_import_dry_run_says_what_it_would_do_and_makes_no_job(js, tmp_path, knocks):
    path = _write(tmp_path, "resume.txt", RESUME)
    refused = await _do("resume.import", {"path": path}, dry=True)
    assert "would be refused" in refused["said"], "no agent has checked in"
    await parse_runner(seen=MINUTE)
    got = await _do("resume.import", {"path": path}, dry=True)
    assert got["ok"] is True and got["said"].startswith("Nothing was changed")
    assert "would start an agent reading resume.txt" in got["said"] and got["result"]["characters"] == len(RESUME)
    assert await _count(ParseJob) == 0 and knocks == []


@pytest.mark.parametrize(("name", "data", "why"), [
    ("resume.exe", b"x" * 80, "not a .docx or .pdf or .txt file"),
    ("missing.txt", None, "no file at"),
    ("short.txt", b"too short to be a resume", "too little text"),
    ("empty.txt", b"", "no text"),
])
async def test_resume_import_refuses_a_file_it_cannot_read_in_words_and_makes_no_job(
        js, tmp_path, knocks, name, data, why):
    await parse_runner(seen=MINUTE)
    path = _write(tmp_path, name, data) if data is not None else str(tmp_path / name)
    got = await _do("resume.import", {"path": path})
    assert got["ok"] is False and why in got["reason"], got
    assert await _count(ParseJob) == 0 and knocks == []


async def test_resume_import_asks_the_same_room_check_the_page_does(js, tmp_path, monkeypatch, knocks):
    await parse_runner(seen=MINUTE)
    asked = _spy(monkeypatch, role_parse, "refuse_resume_over_room")
    started = _spy(monkeypatch, role_parse, "start")
    await _do("resume.import", {"path": _write(tmp_path, "resume.txt", RESUME)})
    assert len(asked) == 1 and len(started) == 1


# --- profile.read -----------------------------------------------------------------------------------------------------


async def test_profile_read_says_there_is_nothing_to_search_for_until_something_is_saved_and_then_what(js):
    empty = (await _read("profile.read"))["result"]
    assert empty["is_set"] is False and empty["versions"] == {k: 0 for k in KINDS}
    assert empty["profile"]["criteria"]["target_roles"] == [] and empty["says"]["gates"]["other"] == {}

    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    got = (await _read("profile.read"))["result"]
    assert got["is_set"] is True and got["versions"]["criteria"] == 1
    assert got["profile"]["criteria"]["company"] == PROFILE["criteria"]["company"]
    assert got["says"]["gates"]["location"] and got["says"]["gates"]["compensation"], "each gate as its sentence"


async def test_profile_read_changes_nothing_and_leaves_no_row(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    before = (await _count(ProfileVersion), await _count(ActionRun), await _count(TrailEvent))
    await _read("profile.read")
    assert (await _count(ProfileVersion), await _count(ActionRun), await _count(TrailEvent)) == before


# --- profile.save -----------------------------------------------------------------------------------------------------

ROLE = {"id": "programs-lead", "name": "Programs lead", "titles": ["Programs Lead", "Head of Programs"]}
LOCATION = {"remote": {"on": True, "locations": ["United States"]}}


async def test_profile_save_saves_the_fields_as_a_new_version_by_claude_and_keeps_the_part_s_other_fields(js):
    first = await _do("profile.save", {"kind": "criteria", "data": {"company": "A small nonprofit."},
                                       "why": "the owner said so"})
    assert first["ok"] is True and first["result"]["version"] == 1, first
    second = await _do("profile.save", {"kind": "criteria", "data": {"target_roles": [ROLE]}})
    assert second["ok"] is True and second["result"]["version"] == 2
    assert second["result"]["changes"] == ["added target roles: programs-lead"]
    rows = await _versions("criteria")
    assert [(r.version, r.saved_by) for r in rows] == [(1, "claude"), (2, "claude")]
    assert rows[0].why == "the owner said so" and rows[1].why == "set on the owner's ask"
    assert rows[1].data["company"] == "A small nonprofit.", "a field not sent is kept as it was"
    assert [r["id"] for r in rows[1].data["target_roles"]] == ["programs-lead"]
    async with db.session_factory()() as session:
        assert (await profile_store.current(session, OWNER)).is_set() is True


async def test_profile_save_runs_what_a_save_on_screen_runs_after_a_new_hard_pass(js, monkeypatch):
    ran = _spy(monkeypatch, after_save, "run")
    saved = _spy(monkeypatch, profile_store, "save")
    got = await _do("profile.save", {"kind": "gates", "data": {"location": LOCATION,
                                                               "compensation": {"amount": 100000}}})
    assert got["ok"] is True and got["result"]["version"] == 1, got
    assert len(saved) == 1 and saved[0][1]["by"] == CLAUDE
    assert len(ran) == 1 and ran[0][0][2:4] == ("gates", 1), "the page's after-save work, with the new version"
    assert got["result"]["after_save"], "what the gate check did, in a line"
    async with db.session_factory()() as session:
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.actor == CLAUDE))).one()
    assert line.sentence.startswith("Claude (on demand) saved version 1 of the gates")


async def test_profile_save_saves_nothing_when_the_fields_already_are_as_asked(js):
    await _do("profile.save", {"kind": "criteria", "data": {"company": "Same."}})
    again = await _do("profile.save", {"kind": "criteria", "data": {"company": "Same."}})
    assert again["ok"] is True and again["result"]["version"] is None and again["result"]["changes"] == []
    assert len(await _versions("criteria")) == 1


@pytest.mark.parametrize(("kind", "data", "why"), [
    ("criteria", {"target_roles": [{"name": "No id"}]}, "criteria"),
    ("criteria", {"nonsense": True}, "nonsense"),
    ("gates", {"location": {"remote": {"on": "maybe"}}}, "gates"),
    ("settings", {"stale_days": 0}, "settings"),
])
async def test_profile_save_refuses_a_field_that_does_not_fit_in_words_and_saves_nothing(js, kind, data, why):
    got = await _do("profile.save", {"kind": kind, "data": data})
    assert got["ok"] is False and got["state"] == "failed" and why in got["reason"], got
    assert await _versions(kind) == []


async def test_profile_save_refuses_what_a_save_on_screen_refuses(js):
    reader = {"other": [{"id": "words", "kind": "rule", "section": "description", "text": "A reader judges this."}]}
    got = await _do("profile.save", {"kind": "gates", "data": reader})
    assert got["ok"] is False and "no reader judges a line" in got["reason"] and "words" in got["reason"]
    ask = {"id": "minus", "section": "company", "says": "Made up", "points": -1, "checked_by": "app",
           "fact": "ownership", "op": "in", "value": "public"}
    got = await _do("profile.save", {"kind": "scoring", "data": {"asks": [ask]}})
    assert got["ok"] is False and "only adds points" in got["reason"]
    assert await _versions("gates") == [] and await _versions("scoring") == []


async def test_profile_save_never_changes_the_owners_own_switch_of_boardone_reading(js):
    got = await _do("profile.save", {"kind": "settings", "data": {"read_robots_exempt": True}})
    assert got["ok"] is False and "read_robots_exempt is the owner's own switch" in got["reason"], got
    assert await _versions("settings") == []
    fine = await _do("profile.save", {"kind": "settings", "data": {"stale_days": 45, "read_robots_exempt": False}})
    assert fine["ok"] is True and fine["result"]["version"] == 1, "leaving it as it is does not matter"
    async with db.session_factory()() as session:
        profile = await profile_store.current(session, OWNER)
        assert (profile.settings.stale_days, profile.settings.read_robots_exempt) == (45, False)
        on = {**profile.settings.model_dump(mode="json"), "read_robots_exempt": True}
        await profile_store.save(session, OWNER, "settings", on, by="owner")  # the owner turned it on themselves
        await session.commit()
    kept = await _do("profile.save", {"kind": "settings", "data": {"stale_days": 50}})
    assert kept["ok"] is True, "a save that leaves their switch as it is goes through"
    off = await _do("profile.save", {"kind": "settings", "data": {"read_robots_exempt": False}})
    assert off["ok"] is False, "and one that turns it off is theirs to do too"


def test_profile_save_wants_at_least_one_field():
    with pytest.raises(DomainError, match="at least one field"):
        runner.check_args(REGISTRY["profile.save"], {"kind": "criteria", "data": {}})


async def test_profile_save_dry_run_says_what_would_change_and_stores_nothing(js):
    await _do("profile.save", {"kind": "criteria", "data": {"company": "First."}})
    got = await _do("profile.save", {"kind": "criteria", "data": {"company": "Second.", "target_roles": [ROLE]}},
                    dry=True)
    assert got["ok"] is True and got["said"].startswith("Nothing was changed")
    assert "would save version 2 of the criteria" in got["said"]
    assert got["result"]["version"] == 2 and set(got["result"]["changes"]) == {
        "changed company", "added target roles: programs-lead"}
    assert len(await _versions("criteria")) == 1
    same = await _do("profile.save", {"kind": "criteria", "data": {"company": "First."}}, dry=True)
    assert "would save nothing" in same["said"] and same["result"]["version"] is None
    refused = await _do("profile.save", {"kind": "criteria", "data": {"nonsense": 1}}, dry=True)
    assert "would be refused" in refused["said"]


@pytest.mark.parametrize(("name", "args"), [
    ("profile.save", {"kind": "criteria", "data": {"company": "x"}}),
    ("tracker.import", {"path": str(Path(__file__).resolve())}),
])
async def test_a_second_of_a_long_import_or_save_is_refused_naming_the_one_running(js, name, args):
    async with db.session_factory()() as session:
        session.add(ActionRun(owner=OWNER, action=name, arguments={}, dry_run=False, by=CLAUDE, state="running"))
        await session.commit()
    with pytest.raises(Conflict, match=f"{name} is running already"):
        await _do(name, args)


# --- tracker.import ---------------------------------------------------------------------------------------------------

TRACKER = [
    tracker_row("Northwind", "Programs Lead", status="second_impression", fit=4,
                url="https://www.boardone.test/jobs/view/1"),
    tracker_row("Wingtip", "Programs Lead", status="ready", fit=2, url="https://www.boardone.test/jobs/view/2"),
    tracker_row("Initech", "Programs Lead", status="applied", fit=5, url="https://www.boardone.test/jobs/view/3"),
]


async def _with_a_profile() -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE)
        await save_documents(session)
        await session.commit()


def _tracker(tmp_path: Path, rows=TRACKER, name: str = "tracker.xlsx") -> str:
    return _write(tmp_path, name, tracker_workbook(rows).getvalue())


async def test_tracker_import_brings_in_the_roles_starts_the_catch_up_and_wakes_the_scout(js, tmp_path, knocks):
    await _with_a_profile()
    got = await _do("tracker.import", {"path": _tracker(tmp_path)})

    assert got["ok"] is True, got
    counts = got["result"]["counts"]
    assert (counts["rows"], counts["added"]) == (3, 3)
    assert got["result"]["catch_up"].startswith("catch-up:"), "what the command line says of it"
    assert got["said"] == "Read the tracker tracker.xlsx: 3 new roles, 0 brought up to date."
    assert await _count(Opportunity) == 3
    assert any(reason == "tag_roles" for _, reason in knocks), "the scout is woken for the matching, after the commit"


async def test_tracker_import_run_again_adds_nothing_and_starts_no_second_catch_up(js, tmp_path, knocks):
    await _with_a_profile()
    path = _tracker(tmp_path)
    await _do("tracker.import", {"path": path})
    woken = len(knocks)
    again = await _do("tracker.import", {"path": path})
    assert again["ok"] is True and again["result"]["counts"]["added"] == 0
    assert "catch_up" not in again["result"] and len(knocks) == woken
    assert await _count(Opportunity) == 3


async def test_tracker_import_dry_run_reads_the_file_says_the_counts_and_keeps_nothing(js, tmp_path, knocks):
    await _with_a_profile()
    got = await _do("tracker.import", {"path": _tracker(tmp_path)}, dry=True)
    assert got["ok"] is True and got["said"].startswith("Nothing was changed")
    assert got["result"]["counts"]["added"] == 3 and got["result"]["would_research"] >= 1
    assert "would read 3 rows of tracker.xlsx: 3 new roles, 0 brought up to date" in got["said"]
    assert await _count(Opportunity) == 0 and knocks == []


async def test_tracker_import_refuses_a_spreadsheet_that_is_not_the_tracker_in_words_and_saves_nothing(
        js, tmp_path, knocks):
    wrong = tracker_workbook([{"a": 1}], header=["a", "b"]).getvalue()
    got = await _do("tracker.import", {"path": _write(tmp_path, "other.xlsx", wrong)})
    assert got["ok"] is False and "header row isn't the 25 columns" in got["reason"], got
    broken = await _do("tracker.import", {"path": _write(tmp_path, "broken.xlsx", b"not a workbook")})
    assert broken["ok"] is False and "isn't a spreadsheet this app can read" in broken["reason"]
    csv_file = await _do("tracker.import", {"path": _write(tmp_path, "tracker.csv", "a,b")})
    assert csv_file["ok"] is False and "not a .xlsx file" in csv_file["reason"]
    gone = await _do("tracker.import", {"path": str(tmp_path / "none.xlsx")})
    assert gone["ok"] is False and "no file at" in gone["reason"]
    assert await _count(Opportunity) == 0 and knocks == []


async def test_tracker_import_that_cannot_start_the_catch_up_saves_nothing(js, tmp_path, monkeypatch, knocks):
    async def cannot(*_a, **_k):
        raise catch_up.CatchUpError("the made-up reason")

    monkeypatch.setattr(catch_up, "start", cannot)
    got = await _do("tracker.import", {"path": _tracker(tmp_path)})
    assert got["ok"] is False and got["reason"] == "the made-up reason"
    assert await _count(Opportunity) == 0 and knocks == [], "the import was not saved, and no one was woken"


async def test_tracker_import_calls_the_one_function_the_command_line_calls(js, tmp_path, monkeypatch, knocks):
    calls = _spy(monkeypatch, bring_in, "tracker_file")
    await _do("tracker.import", {"path": _tracker(tmp_path)})
    assert len(calls) == 1 and calls[0][0][1] == OWNER and calls[0][1] == {"dry_run": False}


# --- employers.import -------------------------------------------------------------------------------------------------


def _csv(tmp_path: Path, *rows, name: str = "made-up-list.csv", header=("name", "website", "verdict")) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return _write(tmp_path, name, out.getvalue())


async def _kept() -> list[tuple[str, str]]:
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(EmployerListEntry).where(EmployerListEntry.owner == OWNER))).all()
    return sorted((r.source, r.key) for r in rows)


async def test_employers_import_keeps_the_usable_rows_under_the_files_name_and_says_the_counts(js, tmp_path):
    path = _csv(tmp_path, ("Acme", "acme.example", "ok"), ("Fabrikam", "fabrikam.example", "drop?"),
                ("Globex", "globex.example", "check"), ("Initech", "n/a", "ok"))
    got = await _do("employers.import", {"path": path})
    assert got["ok"] is True, got
    assert got["result"] == {"rows": 4, "kept": 2, "marked_drop": 1, "unusable": 1, "repeats": 0,
                             "employers_in_it": 0, "employers": 0, "waiting_in_it": 0, "waiting_for_the_scout": 0,
                             "kept_rows_naming_an_employer": 0, "changed": True}
    assert got["said"] == "Kept 2 of 4 rows of made-up-list.csv; 0 of the app's 0 employers are in it."
    assert await _kept() == [("made-up-list.csv", "acme"), ("made-up-list.csv", "globex")]
    again = await _do("employers.import", {"path": path})
    assert again["ok"] is True and len(await _kept()) == 2, "the same file again replaces its rows"


async def test_employers_import_dry_run_saves_nothing(js, tmp_path):
    got = await _do("employers.import", {"path": _csv(tmp_path, ("Acme", "acme.example", "ok"))}, dry=True)
    assert got["ok"] is True and "would keep 1 of 1 rows of made-up-list.csv" in got["said"]
    assert await _kept() == []


async def test_employers_import_refuses_a_file_with_nothing_usable_in_words_and_changes_nothing(js, tmp_path):
    nothing = _csv(tmp_path, ("Acme", "n/a", "ok"), ("Globex", "g.example", "drop"))
    got = await _do("employers.import", {"path": nothing})
    assert got["ok"] is False
    assert "no usable row in the file (2 rows read: 1 marked drop, 1 with no usable" in got["reason"]
    wrong = await _do("employers.import", {"path": _csv(tmp_path, ("a", "b"), name="wrong.csv", header=("x", "y"))})
    assert wrong["ok"] is False and "needs a name column" in wrong["reason"]
    xlsx = await _do("employers.import", {"path": _write(tmp_path, "list.xlsx", b"x")})
    assert xlsx["ok"] is False and "not a .csv file" in xlsx["reason"]
    assert await _kept() == []


async def test_employers_import_calls_the_one_function_the_command_line_calls(js, tmp_path, monkeypatch):
    calls = _spy(monkeypatch, bring_in, "employer_list_file")
    kept = _spy(monkeypatch, employer_list, "replace")
    await _do("employers.import", {"path": _csv(tmp_path, ("Acme", "acme.example", "ok"))})
    assert len(calls) == 1 and len(kept) == 1 and kept[0][0][2] == "made-up-list.csv"


# --- mail.state -------------------------------------------------------------------------------------------------------


async def test_mail_state_says_none_is_saved_gives_each_providers_steps_and_where_the_owner_connects(js):
    got = (await _read("mail.state"))["result"]
    assert got["connected"] is False and got["username"] is None
    assert got["connect_at"] == f"{onboarding.page()}#/agents"
    assert got["providers"] and all(p["steps"] for p in got["providers"][:-1]), "an app password's steps for each"
    assert "password" not in {k for k in got if k != "how"}, "the door has no password to give"


async def test_mail_state_says_who_it_is_saved_for_and_never_the_password(js):
    async with db.session_factory()() as session:
        session.add(MailLogin(owner=OWNER, host="imap.example.test", port=993, username="pat@example.test",
                              password_sealed="SEALED-SECRET-VALUE"))
        await session.commit()
    got = await _read("mail.state")
    assert got["result"]["connected"] is True and got["result"]["username"] == "pat@example.test"
    assert "SEALED-SECRET-VALUE" not in str(got)


def test_nothing_in_the_door_takes_a_password_or_opens_the_mailbox():
    for name, action in REGISTRY.items():
        fields = set(action.inputs.model_json_schema().get("properties", {}))
        assert not {"password", "username", "host"} & fields, f"{name} takes a mail login"
    assert not [n for n in REGISTRY if n.startswith("mail.") and REGISTRY[n].writes]


# --- the page address -------------------------------------------------------------------------------------------------


def test_the_page_address_is_the_installs_public_address_else_the_one_the_app_listens_on(monkeypatch):
    monkeypatch.setattr(settings, "public_url", "")
    assert onboarding.page("#/me") == "http://127.0.0.1:8430/#/me"
    monkeypatch.setattr(settings, "public_url", "https://resumebot.example.test/")
    assert onboarding.page("#/me") == "https://resumebot.example.test/#/me"


# --- onboarding.state -------------------------------------------------------------------------------------------------


async def test_onboarding_state_is_a_read_that_gives_the_lines_and_the_next_step_status_prints(js):
    assert "onboarding.state" in REGISTRY and REGISTRY["onboarding.state"].writes is False
    got = (await _read("onboarding.state"))["result"]
    assert got["lines"] == [
        "resume: not imported yet", "tracker: not imported (optional: only if they have one)",
        "employer list: not imported (optional: only if they have one)", "targets: not set yet",
        "first search: not run yet", "mail: not connected"]
    assert got["next"] == setup_cli.TOOLS["resume"].said()
    assert got["steps"] == {"resume": False, "tracker": False, "employer list": False, "targets": False,
                            "first search": False, "mail": False}


async def test_onboarding_state_follows_the_database_and_names_the_same_next_step_as_the_shared_code(js, tmp_path):
    await _with_a_profile()
    await _do("tracker.import", {"path": _tracker(tmp_path)})
    await _do("employers.import", {"path": _csv(tmp_path, ("Acme", "acme.example", "ok"))})
    got = (await _read("onboarding.state"))["result"]
    assert got["steps"]["tracker"] is True and got["steps"]["employer list"] is True and got["steps"]["targets"]
    lines, next_step = setup_cli.step_report(got["steps"])
    assert (got["lines"], got["next"]) == (lines, next_step), "one implementation of the lines"
    assert "tracker: imported" in got["lines"] and "employer list: imported" in got["lines"]


async def test_onboarding_state_changes_nothing(js):
    models = (Opportunity, EmployerListEntry, ProfileVersion, ActionRun, TrailEvent)
    before = [await _count(m) for m in models]
    await _read("onboarding.state")
    assert [await _count(m) for m in models] == before
