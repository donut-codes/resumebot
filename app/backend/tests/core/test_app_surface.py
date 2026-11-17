"""The app's surface and its seams: closing stuck work and logging every cancel that fails, a
search or packet that cannot half-start, the schedule pass isolating one mission's failure, Run now refusing a second
run, the start-up steps failing soft, Activity lines keeping their order, the owner's day, the doors telling a bug from
a refusal, one filename header for any name, the secret check on paths and hex keys, and the analyst's findings capped.
Everything is made up."""

import io
import logging
import unicodedata
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from docx import Document
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app import db
from app.agents.security import find_secret
from app.errors import DomainError
from app.jobsearch import process
from app.jobsearch.actors import actor_name
from app.jobsearch.browser import mcp as browser_mcp
from app.jobsearch.browser import reader as browser_reader
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.loops.startup import run_startup_fixups
from app.jobsearch.missions import boards_night
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Mission, MissionRun, Opportunity, Packet, SearchRun
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work.door import UNEXPECTED
from app.jobsearch.writing import docs_render, packets
from app.jobsearch.writing.routes import attachment
from app.models.work import TrailEvent
from app.settings import settings
from app.timeutil import utc_now
from app.trail import service as trail
from tests.builders import analysis_setup, findings, packet_setup, written_packet
from tests.support import OWNER, load_profile, pin_clock


class _Engine:
    """The process engine with some of its calls replaced."""

    def __init__(self, real, **over):
        self._real, self._over = real, over

    def __getattr__(self, name):
        return self._over[name] if name in self._over else getattr(self._real, name)


def _engine_with(monkeypatch, **over):
    real = process.engine
    monkeypatch.setattr(process, "engine", lambda: _Engine(real(), **over))


async def _db_error(session, *args):
    """A cancel that fails the way a database does: a statement the server refuses."""
    await session.execute(text("SELECT * FROM no_such_table"))


def _later() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=500)


# --- stuck work is closed, a cancel that fails is logged and poisons nothing -----------------------------


async def test_a_cancel_that_fails_is_logged_and_the_sweep_still_closes_the_search(js, monkeypatch, caplog):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER)
        await session.commit()
        run_id = started.id
        _engine_with(monkeypatch, cancel=_db_error)
        with caplog.at_level(logging.ERROR):
            assert await run.settle_stuck(session, OWNER, now=_later()) == 1
        await session.commit()  # the refused statement left the transaction usable
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        assert row.status == "failed" and "didn't finish" in row.summary
    assert "couldn't cancel the process" in caplog.text and "no such table" in caplog.text


async def test_a_failed_cancel_of_an_analysis_or_a_packet_is_logged_and_both_are_closed(js, monkeypatch, caplog):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        await analysis.start(session, OWNER, role)
        analysis.claim(role)  # a session took it and never sent it
        await session.commit()
        _engine_with(monkeypatch, cancel=_db_error)
        with caplog.at_level(logging.ERROR):
            assert await analysis.settle_stuck(session, OWNER, now=_later()) == 1
        await session.commit()
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, ids["Fabrikam"])).analysis_state == analysis.FAILED
    assert "the analysis of role" in caplog.text
    caplog.clear()
    role_id = await packet_setup()
    async with db.session_factory()() as session:
        packet = await packets.start(session, OWNER, role_id)
        await session.commit()
        packet_id = packet.id
        with caplog.at_level(logging.ERROR):
            assert await packets.settle_stuck(session, OWNER, now=_later()) == 1
        await session.commit()
    async with db.session_factory()() as session:
        assert (await session.get(Packet, packet_id)).status == "failed"
    assert f"packet {packet_id}" in caplog.text


async def test_a_search_is_stuck_when_nothing_has_been_heard_of_it_not_when_it_is_long(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER)
        await session.commit()
        later = _later()
        started.session_seen_at = later - timedelta(minutes=10)  # a session of the round called in a moment ago
        await session.commit()
        assert await run.settle_stuck(session, OWNER, now=later) == 0, "a round of several sessions runs for hours"
        assert started.status == "open"
        started.session_seen_at = later - timedelta(minutes=200)
        await session.commit()
        assert await run.settle_stuck(session, OWNER, now=later) == 1


async def test_one_stuck_item_that_cannot_be_closed_does_not_stop_the_others(js, monkeypatch, caplog):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        for name in ("Fabrikam", "Initech"):
            role = await session.get(Opportunity, ids[name])
            await analysis.start(session, OWNER, role)
            analysis.claim(role)  # a session took each and never sent it
        await session.commit()
        real = analysis.trail.record
        calls = []

        async def record(session_, owner, **kw):
            calls.append(kw["sentence"])
            if len(calls) == 1:
                raise RuntimeError("the first one cannot be written")
            return await real(session_, owner, **kw)

        monkeypatch.setattr(analysis.trail, "record", record)
        with caplog.at_level(logging.ERROR):
            assert await analysis.settle_stuck(session, OWNER, now=_later()) == 1
        await session.commit()
    async with db.session_factory()() as session:
        states = sorted([(await session.get(Opportunity, ids[n])).analysis_state for n in ("Fabrikam", "Initech")])
    assert states == [analysis.FAILED, analysis.WAITING]
    assert "couldn't close stuck analysis" in caplog.text


# --- a search or packet that cannot start leaves nothing, and two cannot both start -----------------------


async def test_a_search_that_cannot_start_leaves_no_open_run_behind(js, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("the engine is down")

    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        _engine_with(monkeypatch, start=boom)
        with pytest.raises(RuntimeError):
            await run.start_round(session, OWNER)
        await session.commit()  # the pass commits whatever the failed start left
        assert await run.open_run(session, OWNER) is None
        _engine_with(monkeypatch, start=lambda *a, **k: _failed_instance())
        with pytest.raises(run.RunError, match="couldn't start"):
            await run.start_round(session, OWNER)
        await session.commit()
        assert await run.open_run(session, OWNER) is None, "an open run with no process would block every search"


async def _failed_instance():
    return SimpleNamespace(id=uuid.uuid4(), status="failed", error="no first step")


async def test_the_database_holds_one_open_search_and_one_packet_being_written(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await run.start_round(session, OWNER)
        await session.commit()
        # a second start that got past the check: the unique index stops it, as a plain refusal
        async def blind(*a, **k):
            return None

        monkeypatch.setattr(run, "open_run", blind)
        with pytest.raises(run.RunError, match="already running"):
            await run.start_round(session, OWNER)
        await session.commit()
        assert len((await session.scalars(select(SearchRun).where(SearchRun.status == "open"))).all()) == 1
    role_id = await packet_setup()
    async with db.session_factory()() as session:
        await packets.start(session, OWNER, role_id)
        await session.commit()
        for version in (7, 8):
            session.add(Packet(owner=OWNER, role_id=role_id, version=version, status="writing",
                               brief={}, resume={}, letter={}, comp={}, reasoning={}, checks=[]))
        with pytest.raises(IntegrityError):
            await session.flush()


# --- the schedule pass isolates each mission; Run now refuses a second run; the read starts after ---


def _due(*keys):
    async def due(session, mission, now=None):
        return mission.key in keys

    return due


async def test_one_mission_that_raises_is_recorded_as_failed_and_the_others_still_run(js, monkeypatch, caplog):
    # 3 AM: inside the owner's off hours, where a scout night starts. The pass reads the app's clock for its `now`
    # (the autouse off-hours default only covers a call with no time), so the real hour would decide the verdict.
    pin_clock(monkeypatch, datetime(2026, 11, 4, 3, 0, tzinfo=UTC))
    started = []
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: started.append(a))
    monkeypatch.setattr(missions.night, "in_peak", lambda *a, **k: False)

    async def broken(session, owner, mission, by):
        await session.execute(text("SELECT * FROM no_such_table"))

    monkeypatch.setattr(missions.kind_of(missions.SCOUT), "start", broken)
    monkeypatch.setattr(missions, "due", _due(missions.SCOUT, missions.READ))
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        for m in (await session.scalars(select(Mission).where(Mission.owner == OWNER))).all():
            m.state = "on"
        await session.commit()
        with caplog.at_level(logging.ERROR):
            await missions.schedule_pass(session, OWNER)
        await session.commit()
        rows = {m.key: m for m in (await session.scalars(select(Mission).where(Mission.owner == OWNER))).all()}
        scout, read = [
            (await session.scalars(select(MissionRun).where(MissionRun.mission_id == rows[key].id))).all()
            for key in (missions.SCOUT, missions.READ)]
    assert [r.outcome for r in scout] == ["failed"] and "couldn't start" in scout[0].summary
    assert [r.outcome for r in read] == ["running"], "the other mission ran"
    assert "the mission scout_boards failed in the schedule pass" in caplog.text


async def test_the_board_read_starts_only_once_its_run_is_saved(js, monkeypatch):
    started = []
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: started.append(a))
    monkeypatch.setattr(missions.night, "in_peak", lambda *a, **k: False)
    monkeypatch.setattr(missions, "due", _due(missions.READ))
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        await session.commit()
        await missions.schedule_pass(session, OWNER)  # each mission's turn is saved as it ends (`db.release_if_free`)
        async with db.read_session_factory()() as other:  # a reader sees only what was committed
            entry = await other.scalar(select(MissionRun))
        assert entry is not None, "the run was committed by the pass itself"
        await session.commit()
    assert len(started) == 1 and started[0][0] == entry.id, (
        "the read began once its run was committed (`db.after_commit`), and is told which run is its own")


async def test_the_read_task_does_nothing_for_a_run_that_was_never_saved(js, monkeypatch):
    called = []

    async def read(owner, details):
        called.append(owner)
        return "done", "x", {}

    monkeypatch.setattr(boards_night, "read_boards", read)
    await boards_night._finish(uuid.uuid4(), OWNER, 10)
    assert called == []


async def test_run_now_is_refused_while_the_mission_is_still_running(client, monkeypatch):
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: None)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        read = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.READ))
        await session.commit()
    first = await client.post(f"/api/v1/missions/{read.id}/run")
    assert first.status_code == 200 and first.json()["outcome"] == "running"
    second = await client.post(f"/api/v1/missions/{read.id}/run")
    assert second.status_code == 409 and "running already" in second.json()["detail"]
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(MissionRun))).all()) == 1


# --- the start-up steps fail soft; a read the restart cut short is closed at once ---------------------------


async def test_a_start_up_step_that_fails_is_logged_and_the_rest_still_run(js, monkeypatch, caplog):
    async def broken(session, owner):
        await session.execute(text("SELECT * FROM no_such_table"))

    monkeypatch.setattr("app.jobsearch.loops.startup.employers.settle", broken)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        with caplog.at_level(logging.ERROR):
            await run_startup_fixups(session, OWNER)
        await session.commit()
        assert {m.key for m in (await session.scalars(select(Mission).where(Mission.owner == OWNER))).all()} >= {
            missions.SCOUT, missions.SWEEP}, "the steps after the failed one ran"
    assert "start-up step employer records failed" in caplog.text


async def test_a_read_marked_running_for_too_long_at_start_up_was_cut_short_by_the_restart(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        read = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.READ))
        other = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.GO))
        cut = MissionRun(owner=OWNER, mission_id=read.id, started_by="schedule", outcome="running",
                         summary="Reading the employers' boards.", started={"reading": True},
                         started_at=utc_now() - timedelta(hours=boards_night.RESUME_HOURS, minutes=1))
        going = MissionRun(owner=OWNER, mission_id=other.id, started_by="schedule", outcome="running",
                           summary="Asked for 1 packet.", started={"packets": []})
        session.add_all([cut, going])
        await session.commit()
        await run_startup_fixups(session, OWNER)
        await session.commit()
        # one younger than `RESUME_HOURS` carries on instead (tests/jobsearch/test_boards_night.py)
        assert cut.outcome == "failed" and "restarted" in cut.summary and cut.ended_at is not None
        assert going.outcome == "running", "work that lives in the database is not cut short by a restart"


# --- Activity lines keep their order and paging neither repeats nor drops one ------------------------------


def test_an_activity_entry_names_no_project_or_goal_because_there_are_none():
    row = TrailEvent(id=uuid.uuid4(), owner=OWNER, at=utc_now(), actor="app", who="resumebot", event="e",
                     sentence="line", detail="a few words")
    shown = trail.entry(row)
    assert not {"project_id", "project_title", "goal_id"} & shown.keys()
    assert shown["details"]["words"] == "a few words"


async def test_lines_written_in_one_transaction_keep_their_order(js):
    async with db.session_factory()() as session:
        for n in range(6):
            await trail.record(session, OWNER, event="e", sentence=f"line {n}")
        await session.commit()
        rows = (await session.scalars(select(TrailEvent).order_by(TrailEvent.at))).all()
    assert [r.sentence for r in rows] == [f"line {n}" for n in range(6)]
    assert len({r.at for r in rows}) == 6, "each has its own time, not the time the transaction began"


async def test_lines_written_within_one_tick_of_a_coarse_clock_keep_their_order_in_activity(js, monkeypatch):
    """A clock that has not moved between the lines (Windows' ticks about once a millisecond) gave them one time, and
    Activity broke the tie by their random ids."""
    from app import timeutil

    class Stopped(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, 23, 0, tzinfo=UTC).astimezone(tz) if tz else datetime(2026, 10, 14, 23, 0)

    monkeypatch.setattr(timeutil, "datetime", Stopped)
    async with db.session_factory()() as session:
        for n in range(6):
            await trail.record(session, OWNER, event="e", sentence=f"line {n}")
        await session.commit()
        items = (await trail.page(session, OWNER))["items"]
    assert [i["sentence"] for i in items] == [f"line {n}" for n in reversed(range(6))], "the newest line is first"
    assert len({i["at"] for i in items}) == 6, "each has its own time"


async def test_paging_through_lines_that_share_a_time_repeats_and_drops_none(js):
    same = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    async with db.session_factory()() as session:
        for n in range(5):
            session.add(TrailEvent(owner=OWNER, at=same, actor="app", who="resumebot", event="e", sentence=f"tie {n}"))
        session.add(TrailEvent(owner=OWNER, at=same - timedelta(hours=1), actor="app", who="resumebot", event="e",
                               sentence="earlier"))
        await session.commit()
        seen, before = [], None
        for _ in range(10):
            page = await trail.page(session, OWNER, before=before, limit=2)
            seen += [i["sentence"] for i in page["items"]]
            before = page["next_before"]
            if before is None:
                break
    assert len(seen) == 6 and len(set(seen)) == 6 and seen[-1] == "earlier"


async def test_a_trail_page_takes_a_plain_time_as_before_and_refuses_what_is_neither(js):
    async with db.session_factory()() as session:
        assert (await trail.page(session, OWNER, before="2026-10-01T00:00:00+00:00"))["items"] == []
        with pytest.raises(trail.CursorError):
            await trail.page(session, OWNER, before="not a time")


# --- the owner's day -------------------------------------------------------------------------------------


@pytest.fixture
def evening_in_the_americas(monkeypatch):
    """02:00 UTC on the 13th: still the evening of the 12th for an owner in New York."""
    from app import timeutil

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 13, 2, 0, tzinfo=UTC).astimezone(tz) if tz else datetime(2026, 10, 13, 2, 0)

    monkeypatch.setattr(timeutil, "datetime", Frozen)
    monkeypatch.setattr(settings, "timezone", "America/New_York")


def test_the_owners_today_is_their_own_day_not_utc(evening_in_the_americas):
    from app.inbox.service import _today
    from app.timeutil import owner_today

    assert owner_today() == _today(None) == date(2026, 10, 12)
    assert _today(date(2026, 10, 20)) == date(2026, 10, 20), "a day that is given is kept"


def test_a_cover_letter_is_dated_with_the_owners_day(evening_in_the_americas):
    data = docs_render.letter_docx({"paragraphs": ["Hello."]}, {"name": "Pat Example", "contact": [], "roles": []},
                                   "Globex")
    assert "October 12, 2026" in [p.text for p in Document(io.BytesIO(data)).paragraphs]


async def test_the_owner_is_named_from_the_about_page_else_you(js, client):
    async with db.session_factory()() as session:
        assert await actor_name(session, OWNER) == "You"
        await profile_store.save(session, OWNER, "about", {"name": "Jane Doe"}, by="test")
        await session.commit()
        assert await actor_name(session, OWNER) == "Jane Doe", "not 'Jane doe'"


# --- the doors, and no database to fall back on ---------------------------------------------------------


async def test_the_scouts_door_refuses_a_domain_error_and_logs_a_bug(js, monkeypatch, caplog):
    monkeypatch.setattr(mcp_server, "token_ok", lambda header: True)
    ctx = SimpleNamespace(headers={})

    async def refused(session, owner):
        raise run.RunError("no search is open")

    async def bug(session, owner):
        raise ValueError("invalid literal for int() with base 10: 'x'")

    reply = await mcp_server.door.call(ctx, "jobs.test", refused)
    assert reply["ok"] is False and reply["reason"] == "no search is open"
    with caplog.at_level(logging.ERROR):
        reply = await mcp_server.door.call(ctx, "jobs.test", bug)
    assert reply["ok"] is False and reply["reason"] == UNEXPECTED, "a bug is not a polite refusal"
    assert "mcp jobs.test failed" in caplog.text and "invalid literal" in caplog.text


async def test_the_browser_door_refuses_a_domain_error_and_logs_a_bug(js, monkeypatch, caplog):
    monkeypatch.setattr(browser_mcp, "token_matches", lambda header, path: True)
    ctx = SimpleNamespace(headers={})

    async def refused(session, owner):
        raise browser_mcp.browser_pace.BrowserWorkError("that site is not one of yours")

    async def bug(session, owner):
        raise ValueError("a bug")

    assert (await browser_mcp.door.call(ctx, "browser.test", refused))["reason"] == "that site is not one of yours"
    with caplog.at_level(logging.ERROR):
        reply = await browser_mcp.door.call(ctx, "browser.test", bug)
    assert reply["reason"] == UNEXPECTED and "browser door browser.test failed" in caplog.text


def test_every_domain_error_the_app_raises_is_one_the_doors_answer():
    for error in (run.RunError, packets.PacketError, analysis.AnalysisError, missions.MissionError,
                  browser_mcp.browser_pace.BrowserWorkError,
                  browser_reader.BrowserError):
        assert issubclass(error, DomainError) and not issubclass(error, ValueError), error


def test_without_a_database_url_the_app_stops_instead_of_reaching_for_localhost(monkeypatch):
    monkeypatch.setattr(db.settings, "database_url", "")
    monkeypatch.setattr(db.base_settings, "database_url", "")
    with pytest.raises(RuntimeError, match="JOBSEARCH_DATABASE_URL"):
        db.database_url()
    monkeypatch.setattr(db.settings, "database_url", "postgresql+asyncpg://x:y@db/z")
    assert db.database_url() == "postgresql+asyncpg://x:y@db/z"


# --- any name makes a valid download header ----------------------------------------------------------------


def test_a_download_name_with_an_accent_a_cjk_letter_or_a_quote_is_a_valid_header():
    plain = attachment("Pat_Globex_Resume_v1.docx")
    assert plain == 'attachment; filename="Pat_Globex_Resume_v1.docx"'
    accented = attachment("Pat_Škoda_Resume_v1.docx")
    assert accented.startswith('attachment; filename="Pat_Skoda_Resume_v1.docx"')
    assert accented.endswith("filename*=UTF-8''Pat_%C5%A0koda_Resume_v1.docx")
    cjk = attachment("日本電気_v1.docx")
    cjk.encode("latin-1")  # a header is latin-1 on the wire
    assert "filename*=UTF-8''%E6%97%A5" in cjk
    quoted = attachment('Pat"; x=y\r\n_v1.docx')
    quoted.encode("latin-1")
    assert "\r" not in quoted and "\n" not in quoted and quoted.count('"') == 2


async def test_a_packet_for_an_employer_with_an_accented_name_downloads(client):
    packet_id = await written_packet(client)
    async with db.session_factory()() as session:
        role = await session.scalar(select(Opportunity))
        role.company = "Škoda 日本"
        await session.commit()
    for kind in ("resume", "letter"):
        got = await client.get(f"/api/v1/packets/{packet_id}/{kind}.docx")
        assert got.status_code == 200, "a name outside latin-1 was a 500"
        got.headers["content-disposition"].encode("latin-1")


# --- the secret check ----------------------------------------------------------------------------------------


def test_a_job_system_path_is_not_a_secret_and_a_hex_key_is():
    path = "en-US/External_Career_Site/job/Remote-USA/Director-of-Data-Governance_R-12345-2"
    assert len(path) > 70 and find_secret(path) is None, "a long path of words scores high on entropy only"
    assert find_secret(f"https://acme.jobs.example.test/{path}") is None
    sha256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    assert find_secret(f"the key is {sha256}") == "looks like a long random token"
    assert find_secret("d41d8cd98f00b204e9800998ecf8427e") == "looks like a long random token"
    assert find_secret("q8Zk3VhD0xT7mNwP2cRb9LfY6uJaE4sXgH1oKiB5tUe") == "looks like a long random token"
    assert find_secret("ab" * 20) is None and find_secret("0" * 40) is None, "a repeating run is not random"


# --- the analyst's findings are capped -----------------------------------------------------------------


@pytest.mark.parametrize("employer", [
    {"employees": 3_000_000_000}, {"employees": -1}, {"founded": 20250}, {"founded": 12},
    {"industry": "x" * 5000}, {"website": "https://x.test/" + "a" * 3000}, {"offices": ["a"] * 200},
    {"sectors": ["x" * 500]}, {"events": [{"kind": "layoff"}] * 200}, {"events": [{"note": "x" * 5000}]},
    {"sources": {f"f{i}": "x" for i in range(100)}}, {"sources": {"employees": "x" * 5000}},
])
def test_an_employer_fact_past_its_cap_is_refused(employer):
    with pytest.raises(ValidationError):
        analysis.Submission.model_validate(findings(uuid.uuid4(), employer=employer))


def test_a_long_requirements_list_or_item_is_refused_and_a_normal_submission_is_taken():
    role = uuid.uuid4()
    with pytest.raises(ValidationError):
        analysis.Submission.model_validate(findings(role, required=[{"item": "x", "class": "P"}] * 600))
    with pytest.raises(ValidationError):
        analysis.Submission.model_validate(findings(role, required=[{"item": "x" * 5000, "class": "U"}]))
    assert analysis.Submission.model_validate(findings(role)).employer.employees == 300


async def test_an_employee_count_past_a_database_integer_is_refused_in_words_not_a_500(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
        with pytest.raises(analysis.AnalysisError, match="employer.employees"):
            await analysis.submit(session, OWNER, findings(ids["Fabrikam"], employer={"employees": 3_000_000_000}))


# --- more seams -----------------------------------------------------------------------------------------------------


def test_the_resume_reader_has_no_invisible_characters_in_its_source():
    from pathlib import Path

    from app.jobsearch.resume import text as resume_text

    source = Path(resume_text.__file__).read_text(encoding="utf-8")
    assert not [c for c in source if unicodedata.category(c) in ("Cf", "Zs") and c != " "]




# --- the roster ---------------------------------------------------------------------------------------------
# (tests/core/test_harness_screens.py: test_a_session_with_no_search_run_still_shows_the_agent_busy)
