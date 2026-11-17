"""The nightly cost benchmark (`cost_report.py`): fixture transcripts with known tokens give known dollars, the join to
the database gives the units and what each role came to, the summary prints, and the Agents page's route lists the
nights. Made-up sessions and roles only."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import BrowserAttempt, BrowserItem, CostNight, Opportunity, Packet, StageMove
from app.jobsearch.pool import cost_report
from app.models.work import TrailEvent
from app.settings import settings
from tests.support import OWNER

NIGHT = date(2026, 10, 18)
#: 01:00 to 07:00 in New York on that date.
START = datetime(2026, 10, 18, 5, 0, tzinfo=UTC)
INSIDE = START + timedelta(minutes=30)


@pytest.fixture(autouse=True)
def owner_in_new_york(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")


def _line(kind: str, at: datetime | None = None, **more) -> str:
    body = {"type": kind, **more}
    if at:
        body["timestamp"] = at.isoformat().replace("+00:00", "Z")
    return json.dumps(body)


def _assistant(mid: str, model: str, *, inp=0, out=0, read=0, w5m=0, w1h=0, at=INSIDE, content=None, searches=0):
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
             "cache_creation_input_tokens": w5m + w1h, "cache_creation": {
                 "ephemeral_5m_input_tokens": w5m, "ephemeral_1h_input_tokens": w1h}}
    if searches:
        usage["server_tool_use"] = {"web_search_requests": searches}
    return _line("assistant", at, message={"id": mid, "model": model, "usage": usage, "content": content or []})


def _session(data, lane: str, name: str, lines: list[str]):
    folder = data / "claude" / lane / "projects" / "work"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _prompt(text: str, at=INSIDE) -> str:
    return _line("user", at, message={"role": "user", "content": text})


def _tool_use(name: str) -> list[dict]:
    return [{"type": "tool_use", "id": "t1", "name": name, "input": {}}]


def _tool_result(text: str, at=INSIDE) -> str:
    return _line("user", at, message={"content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]})


SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"
ANALYST_PROMPT = "This run was started by: more work: analysis. Call jobs.analysis for the role waiting."


# --- prices and sessions ----------------------------------------------------------------------------------------------


def test_a_message_is_priced_by_its_model_and_its_cache_write_by_its_lifetime():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000},
             "cache_creation_input_tokens": 2_000_000}
    assert cost_report.message_usd(SONNET, usage) == pytest.approx(2 + 0.2 + 2.5 + 4 + 10)
    assert cost_report.message_usd(HAIKU, usage) == pytest.approx(0.1 + 0.01 + 0.125 + 0.2 + 0.5)
    # No split given: the whole write is a 5-minute one. A web search is a cent.
    plain = {"cache_creation_input_tokens": 1_000_000, "server_tool_use": {"web_search_requests": 2}}
    assert cost_report.message_usd(SONNET, plain) == pytest.approx(2.5 + 0.02)


def test_a_session_counts_each_message_once_and_prices_what_it_has(tmp_path):
    path = _session(tmp_path, "analysis-1", "s1", [
        _prompt(ANALYST_PROMPT),
        _assistant("m1", SONNET, inp=10, out=100, read=1000, w1h=5000),
        _assistant("m1", SONNET, inp=10, out=300, read=1000, w1h=5000),  # the same message again: the last copy counts
        _assistant("m2", HAIKU, out=1000, read=10_000),
    ])
    s = cost_report.read_session(path)
    assert (s["kind"], s["turns"], s["models"]) == ("analyst", 2, {"sonnet": 1, "haiku": 1})
    assert s["tokens"] == {"input": 10, "cache_read": 11_000, "output": 1300, "write_5m": 0, "write_1h": 5000}
    expected = (10 * 2 + 300 * 10 + 1000 * 0.2 + 5000 * 4) / 1e6 + (1000 * 0.5 + 10_000 * 0.01) / 1e6
    assert s["usd"] == pytest.approx(expected)


def test_claude_codes_own_figure_wins_when_the_transcript_has_one(tmp_path):
    path = _session(tmp_path, "analysis-1", "s1", [
        _prompt(ANALYST_PROMPT), _assistant("m1", SONNET, out=1000), _line("cost-state", totalCostUSD=0.5)])
    s = cost_report.read_session(path)
    assert s["usd"] == 0.5 and s["computed_usd"] == pytest.approx(0.01)


@pytest.mark.parametrize("prompt, tools, kind", [
    (ANALYST_PROMPT, set(), "analyst"),
    ("Call jobs.packet for the packet waiting", set(), "writer"),
    ("Call jobs.employer for one", set(), "employer"),
    ("Call jobs.titles_next for ten", set(), "titles"),
    ("Call jobs.profile for your task, then jobs.read the postings", set(), "reader"),
    ("find job boards for the owner", set(), "scout"),
    ("Work the owner's browser items", set(), "browser_reader"),
    ("anything", {"resumebot-browser__browser_next_item"}, "browser_reader"),
    ("Map the owner's site", set(), "pathfinder"),
    ("anything", {"jobsearch__task_next"}, "site"),
    ("This run was started by: scheduled check-in. Call jobs.parse_next for the posting or resume the owner gave, "
     "split it into its fields", {"jobsearch__jobs_parse_next"}, "parser"),
    ("hello", set(), "other"),
])
def test_the_kind_of_a_session_comes_from_what_it_was_told(prompt, tools, kind):
    assert cost_report.kind_of(prompt, tools) == kind


def test_the_window_is_the_off_hours_in_the_owners_zone():
    assert cost_report.window(NIGHT, "01:00", "07:00") == (START, START + timedelta(hours=6))
    # Past midnight: ends the next day.
    start, end = cost_report.window(NIGHT, "22:00", "02:00")
    assert end - start == timedelta(hours=4)
    # The newest finished night: at 06:00 New York time on the 19th the night of the 19th is still running.
    assert cost_report.last_finished_night("01:00", "07:00", datetime(2026, 10, 19, 10, 0, tzinfo=UTC)) == NIGHT
    noon = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    assert cost_report.last_finished_night("01:00", "07:00", noon) == date(2026, 10, 19)


def test_only_sessions_that_started_in_the_window_are_the_nights(tmp_path):
    for name, at in (("in", INSIDE), ("early", START - timedelta(hours=2)), ("late", START + timedelta(hours=7))):
        _session(tmp_path, "analysis-1", name, [_prompt(ANALYST_PROMPT, at), _assistant("m", SONNET, out=1, at=at)])
    got = cost_report.read_night(tmp_path, START, START + timedelta(hours=6))
    assert [s["id"] for s in got] == ["in"]


# --- the join to the database -----------------------------------------------------------------------------------------


def _role(slug: str, status: str, **more) -> Opportunity:
    return Opportunity(owner=OWNER, slug=slug, company="Acme", title=f"Analyst {slug}", status=status, origin="scout",
                       match_key=match_key("Acme", f"Analyst {slug}"), **more)


def _read_at(at: datetime) -> dict:
    """A role's stored gate facts after Haiku answered its header-lines read at `at` (as `gate_facts` writes them)."""
    return {"state": "settled", "at": at.isoformat(), "haiku": {"answered": at.isoformat()}}


async def _world(tmp_path):
    """Three analysed roles (one reached Ready to Apply with a packet, one gated out on place, one on the way), a packet
    written, two gate-fact reads in the night and one before it, a browser item handed back and two scout
    reports; the transcripts that cost them."""
    async with db.session_factory()() as session:
        ready = _role("ready", stages.READY_TO_LAUNCH, gate_facts=_read_at(INSIDE))
        gated = _role("gated", stages.GATED_OUT, screened_out_step="gate", screened_out_by="location",
                      gate_facts=_read_at(START - timedelta(hours=1)))  # read before the night: not counted
        found = _role("found", stages.SECOND_IMPRESSION, gate_facts=_read_at(INSIDE))
        session.add_all([ready, gated, found])
        await session.flush()
        packet = Packet(owner=OWNER, role_id=ready.id, version=1, status="ready", written_at=INSIDE)
        session.add(packet)
        await session.flush()
        every = [ready.id, gated.id, found.id]
        await session.execute(update(StageMove).where(StageMove.role_id.in_(every)).values(at=INSIDE))
        await session.execute(update(StageMove).where(StageMove.role_id == ready.id, StageMove.from_status.is_(None))
                              .values(to_status=stages.READY_TO_LAUNCH))
        for role in (ready, gated, found):
            session.add(TrailEvent(owner=OWNER, at=INSIDE, actor="agent", who="Analyst", event="analysed",
                                   sentence="x", link_kind="opportunity", link_id=role.id))
        session.add(TrailEvent(owner=OWNER, at=INSIDE, actor="agent", who="Writer", event="packet_ready", sentence="x",
                               link_kind="opportunity", link_id=ready.id))
        for text in ("The scout reported 23 roles.", "The scout reported 17 roles."):
            session.add(TrailEvent(owner=OWNER, at=INSIDE, actor="agent", who="Reader", event="scout_reported",
                                   sentence=text))
        item = BrowserItem(owner=OWNER, kind="source", target="https://example.test/job", domain="example.test",
                           next_at=INSIDE)
        session.add(item)
        await session.flush()
        session.add(BrowserAttempt(owner=OWNER, item_id=item.id, kind="source", at=INSIDE, outcome="found",
                                  session_id="worker-1a2b"))
        # The app's own code handed back items too (a code-first worker): no Claude, so not the unit.
        for outcome, kind in (("found", "source"), ("done", "map")):
            session.add(BrowserAttempt(owner=OWNER, item_id=item.id, kind=kind, at=INSIDE, outcome=outcome,
                                       session_id="code-9f8e"))
        session.add(BrowserAttempt(owner=OWNER, item_id=item.id, kind="source", at=INSIDE, outcome="no_result"))
        await session.commit()
        ids = {"ready": ready.id, "gated": gated.id, "packet": packet.id}

    def analyst(name, role_id, **usage):
        _session(tmp_path, "analysis-1", name, [
            _prompt(ANALYST_PROMPT),
            _assistant("m1", SONNET, content=_tool_use("jobsearch__jobs_analysis"), **usage),
            _tool_result('{"ok":true,"role":{"id":"' + str(role_id) + '","title":"t"}}'),
        ])
    analyst("a-ready", ids["ready"], out=100_000)  # $1.00
    analyst("a-gated", ids["gated"], out=50_000)  # $0.50
    _session(tmp_path, "writer", "w1", [
        _prompt("Call jobs.packet for the packet waiting"),
        _assistant("m1", SONNET, content=_tool_use("jobsearch__jobs_packet"), w1h=500_000),  # $2.00
        _tool_result('{"ok":true,"packet":"' + str(ids["packet"]) + '","version":1}'),
    ])
    _session(tmp_path, "scout", "b1", [
        _prompt("Work the owner's browser items"), _assistant("m1", SONNET, out=20_000)])  # $0.20
    _session(tmp_path, "scout", "t1", [
        _prompt("Call jobs.titles_next for ten"), _assistant("m1", HAIKU, out=2_000_000)])  # $1.00
    return ids


async def test_the_night_is_priced_by_kind_and_per_unit_and_per_role(js, tmp_path):
    await _world(tmp_path)
    async with db.session_factory()() as session:
        report = await cost_report.report_night(session, OWNER, NIGHT, tmp_path, store=False)
    assert report["total"]["usd"] == pytest.approx(4.7)
    assert report["total"]["sessions"] == 5 and report["total"]["turns"] == 5
    kinds = report["kinds"]
    assert [k for k in kinds] == ["analyst", "browser_reader", "writer", "titles"]
    assert kinds["analyst"]["usd"] == pytest.approx(1.5) and kinds["analyst"]["units"] == 3
    assert kinds["analyst"]["per_unit_usd"] == pytest.approx(0.5)
    assert kinds["writer"]["units"] == 1 and kinds["writer"]["per_unit_usd"] == pytest.approx(2.0)
    assert kinds["writer"]["cache_write_1h"] == 500_000
    assert kinds["browser_reader"]["units"] == 1 and kinds["browser_reader"]["per_unit_usd"] == pytest.approx(0.2)
    assert kinds["titles"]["haiku_turns"] == 1
    assert (kinds["titles"]["unit"], kinds["titles"]["units"]) == ("role read", 2)
    assert kinds["titles"]["per_unit_usd"] == pytest.approx(0.5)  # the $1.00 session over the two reads in the night
    roles = report["roles"]
    assert (roles["analysed"], roles["ready"]) == (3, 1)
    assert roles["usd_per_ready"] == pytest.approx(4.7)
    assert roles["direct_usd_per_ready"] == pytest.approx(3.0)  # the analyst's $1.00 and the writer's $2.00
    assert roles["gated_usd"] == pytest.approx(0.5) and roles["gated_roles"] == 1
    assert roles["gated_by_stage"] == [{"stage": "gate", "gate": "location", "roles": 1, "usd": 0.5}]
    assert "reader" not in kinds  # no reader session that night


async def test_the_reader_is_counted_in_postings_read(js, tmp_path):
    await _world(tmp_path)
    _session(tmp_path, "scout", "r1", [
        _prompt("Call jobs.profile for your task, then jobs.read the postings"), _assistant("m1", HAIKU, out=400_000)])
    async with db.session_factory()() as session:
        report = await cost_report.report_night(session, OWNER, NIGHT, tmp_path, store=False)
    reader = report["kinds"]["reader"]
    assert reader["units"] == 40 and reader["per_unit_usd"] == pytest.approx(0.005)


async def test_a_night_is_stored_once_and_worked_out_again_replaces_it(js, tmp_path):
    await _world(tmp_path)
    for _ in range(2):
        async with db.session_factory()() as session:
            await cost_report.report_night(session, OWNER, NIGHT, tmp_path)
            await session.commit()
    async with db.session_factory()() as session:
        rows = (await session.execute(select(CostNight))).scalars().all()
        assert len(rows) == 1 and rows[0].total_usd == pytest.approx(4.7) and rows[0].sessions == 5
        assert (await cost_report.listing(session, OWNER))[0]["night"] == "2026-10-18"


async def test_a_night_with_no_session_stores_nothing(js, tmp_path):
    async with db.session_factory()() as session:
        assert await cost_report.report_night(session, OWNER, NIGHT, tmp_path) is None
        assert (await session.execute(select(CostNight))).first() is None


async def test_the_newest_finished_night_is_worked_out_once_and_only_with_transcripts(js, tmp_path, monkeypatch):
    monkeypatch.setattr(cost_report, "utc_now", lambda: datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    await _world(tmp_path)
    async with db.session_factory()() as session:
        assert await cost_report.note_night(session, OWNER, None) is None
        assert await cost_report.note_night(session, OWNER, tmp_path / "nowhere") is None
        first = await cost_report.note_night(session, OWNER, tmp_path)
        await session.commit()
        assert first and first["night"] == "2026-10-18"
        assert await cost_report.note_night(session, OWNER, tmp_path) is None


async def test_only_the_last_seven_nights_are_listed_newest_first(js):
    async with db.session_factory()() as session:
        for n in range(9):
            day = NIGHT - timedelta(days=n)
            session.add(CostNight(owner=OWNER, night=day, total_usd=n, sessions=1, data={"night": day.isoformat()}))
        await session.commit()
        nights = await cost_report.listing(session, OWNER)
    assert [n["night"] for n in nights] == [(NIGHT - timedelta(days=n)).isoformat() for n in range(7)]


async def test_the_page_route_lists_the_nights(js, client):
    async with db.session_factory()() as session:
        session.add(CostNight(owner=OWNER, night=NIGHT, total_usd=4.7, sessions=5, data={"night": "2026-10-18"}))
        await session.commit()
    assert (await client.get("/api/v1/cost-nights")).json() == [{"night": "2026-10-18"}]


# --- the summary ------------------------------------------------------------------------------------------------------


async def test_a_dry_run_works_out_the_night_and_stores_nothing(js, tmp_path):
    await _world(tmp_path)
    async with db.session_factory()() as session:
        report = await cost_report.report_night(session, OWNER, NIGHT, tmp_path, store=False)
    out = cost_report.summary(report)
    assert "total $4.70: 5 sessions, 5 turns" in out
    assert "analyst: $1.50, 2 sessions" in out and "3 role analysed, $0.500 each" in out
    assert "1 reached Ready to Apply" in out and "gated out at gate/location: 1 roles, $0.50" in out
    async with db.session_factory()() as session:
        assert (await session.execute(select(CostNight))).first() is None


# --- 47.6: what went wrong in the night, by kind ----------------------------------------------------------------------


def _answer(name: str, ok: bool) -> list:
    """A session that calls `name` and is answered ok or refused."""
    call = _assistant("m1", SONNET, out=1, content=_tool_use(name))
    refusal = '{"ok":false,"reason":"already held","how_to_fix":"x"}'
    return [call, _tool_result('{"ok":true,"taken":true}' if ok else refusal)]


def test_a_session_that_handed_nothing_back_found_nothing_and_a_refused_answer_is_counted(tmp_path):
    empty = cost_report.read_session(_session(tmp_path, "analysis-1", "e", [
        _prompt("Call jobs.titles_next for ten"),
        _assistant("m0", SONNET, out=1, content=_tool_use("jobsearch__jobs_titles_next"))]))
    worked = cost_report.read_session(_session(tmp_path, "analysis-1", "w", [
        _prompt("Call jobs.titles_next for ten"), *_answer("jobsearch__jobs_titles_report", True)]))
    refused = cost_report.read_session(_session(tmp_path, "analysis-1", "r", [
        _prompt("Call jobs.titles_next for ten"), *_answer("jobsearch__jobs_titles_report", False)]))
    assert (empty["found_nothing"], empty["refused"]) == (True, 0)
    assert (worked["found_nothing"], worked["refused"]) == (False, 0)
    assert (refused["found_nothing"], refused["refused"]) == (False, 1)  # it did try to answer


def test_a_refusal_of_a_call_that_is_not_an_answer_is_not_counted(tmp_path):
    s = cost_report.read_session(_session(tmp_path, "analysis-1", "x", [
        _prompt("hello"), *_answer("jobsearch__jobs_profile", False)]))
    assert s["refused"] == 0


def test_the_kinds_total_their_empty_and_refused_sessions():
    base = {"turns": 1, "tokens": {}, "models": {}, "usd": 0.0}
    got = cost_report._kind_totals([
        {**base, "kind": "titles", "found_nothing": True, "refused": 0},
        {**base, "kind": "titles", "found_nothing": False, "refused": 2},
        {**base, "kind": "analyst", "found_nothing": False, "refused": 0}])
    assert (got["titles"]["found_nothing"], got["titles"]["refused"]) == (1, 2)
    assert (got["analyst"]["found_nothing"], got["analyst"]["refused"]) == (0, 0)


def _lane_log(data, lane: str, lines: list[str]):
    folder = data / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"runs-{lane}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stamp(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_asks_that_timed_out_are_counted_from_the_lane_logs_inside_the_window_only(tmp_path):
    slow = "asking for work failed: The operation was aborted due to timeout"
    _lane_log(tmp_path, "owner", [
        f"{_stamp(INSIDE)} {slow}", f"{_stamp(INSIDE)} {slow}",
        f"{_stamp(INSIDE)} heartbeat alive failed: The operation was aborted due to timeout",  # not an ask
        f"{_stamp(INSIDE)} nothing waiting (scheduled check-in)",
        f"{_stamp(START - timedelta(minutes=1))} {slow}",  # the minute before the window
        f"{_stamp(START + timedelta(hours=6))} {slow}",  # the window's end is outside it
        "not a log line"])
    _lane_log(tmp_path, "writer", [f"{_stamp(INSIDE)} {slow}"])
    assert cost_report.asks_timed_out(tmp_path, START, START + timedelta(hours=6)) == 3
    assert cost_report.asks_timed_out(tmp_path / "none", START, START + timedelta(hours=6)) == 0


async def test_the_night_report_carries_the_47_6_counts_and_the_summary_prints_them(js, tmp_path):
    _session(tmp_path, "analysis-1", "empty", [
        _prompt("Call jobs.titles_next for ten"), _assistant("m0", SONNET, out=1)])
    _session(tmp_path, "analysis-1", "refused", [
        _prompt("Call jobs.titles_next for ten"), *_answer("jobsearch__jobs_titles_report", False)])
    _lane_log(tmp_path, "owner", [f"{_stamp(INSIDE)} asking for work failed: The operation was aborted due to timeout"])
    async with db.session_factory()() as session:
        report = await cost_report.report_night(session, OWNER, NIGHT, tmp_path, store=False)
    out = cost_report.summary(report)
    assert "titles: $0.00, 2 sessions, 2 turns; 1 found nothing, 1 answers refused" in out
    assert "asks timed out: 1" in out


async def test_a_stored_night_from_before_47_6_still_prints():
    old = {"night": "2026-10-01", "from": "a", "to": "b",
           "total": {"usd": 1.0, "sessions": 1, "turns": 1, "input": 1, "cache_write": 1, "cache_read": 1, "output": 1},
           "kinds": {"titles": {"usd": 1.0, "sessions": 1, "turns": 1}},
           "roles": {"analysed": 0, "ready": 0, "usd_per_ready": None, "gated_usd": 0.0, "gated_roles": 0,
                     "gated_by_stage": []}}
    assert "titles: $1.00, 1 sessions" in cost_report.summary(old)
