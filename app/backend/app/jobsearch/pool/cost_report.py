"""The nightly cost benchmark: what a night's Claude sessions cost at API
prices, by kind of session and per unit of output, from the lanes' Claude Code session transcripts, joined to the
database for the units (roles analysed, packets, postings read, items handed back, gate-fact reads) and for what each
role came to (reached Ready to Apply, or was gated out and at which step).

A night is the owner's off-hours window that starts on a date (`Settings.off_hours_start` to `off_hours_end`, in the
owner's time zone). The sessions that started inside it are the night's. One row is stored for each night
(`js_cost_nights`), because the lanes prune their transcripts after a while; the Agents page shows the last night
and the last seven.

Prices are the API's per million tokens; a session's dollars are Claude Code's own figure for it (the `cost-state`
line a transcript ends with, which includes the side calls WebSearch and WebFetch make), or, for a transcript without
one, the tokens priced here. Each message is priced by its own model (Sonnet or Haiku) and its cache writes by their
lifetime.

Pure parsing is separate from the database: `read_night` reads transcripts, `build` joins them to the database. Services
flush; the route or command that owns the request commits.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import jsonsql
from app.jobsearch.browser import pace as browser_pace
from app.jobsearch.models import (
    BrowserAttempt,
    CostNight,
    Opportunity,
    Packet,
    StageMove,
)
from app.jobsearch.profile import store as profile_store
from app.jobsearch.stages import GATED_OUT, READY_TO_LAUNCH
from app.jobsearch.work.hours import clock
from app.models.work import TrailEvent
from app.timeutil import owner_zone, utc_now

#: Dollars per million tokens: fresh input, cache read, cache write (5 minutes, 1 hour), output.
PRICES = {
    "sonnet": {"input": 2.0, "cache_read": 0.2, "write_5m": 2.5, "write_1h": 4.0, "output": 10.0},
    "haiku": {"input": 0.1, "cache_read": 0.01, "write_5m": 0.125, "write_1h": 0.2, "output": 0.5},
}
#: What one web search costs, dollars.
WEB_SEARCH_USD = 0.01
#: The nights the Agents page lists.
NIGHTS_SHOWN = 7

#: The kinds of session, in the order they are listed, with what each hands on (the unit its cost is divided by).
KINDS = ("analyst", "browser_reader", "writer", "reader", "employer", "titles", "parser", "scout", "pathfinder", "site",
         "other")
UNIT_NAMES = {
    "analyst": "role analysed", "browser_reader": "item handed back", "writer": "packet", "reader": "posting read",
    "employer": "employer researched", "titles": "role read", "pathfinder": "site mapped",
}

_ROLE_ID = re.compile(r'"role":\{"id":"([0-9a-f-]{36})"')
_PACKET_ID = re.compile(r'"packet":"([0-9a-f-]{36})"')
_STARTED_BY = re.compile(r"^This run was started by:[^.]*\.\s*")
#: The calls that hand work back (an answer), and what a refusal of one looks like in its reply (door.py's `refusal`).
_ANSWER_TOOL = re.compile(r"report|submit|hand_back|board_found|not_found|propose|answer")
_REFUSED = re.compile(r'^\s*\{\s*"ok"\s*:\s*false')
#: A lane's own log line for a check-in for work that timed out (app/scout/scout.mjs), after its ISO time.
_ASK_TIMED_OUT = re.compile(r"^(\S+) asking for work failed: .*timeout")


def kind_of(first_prompt: str, tools: set[str]) -> str:
    """The kind of a session, from the first thing it was told and the tools it called."""
    body = _STARTED_BY.sub("", first_prompt or "")
    joined = " ".join(tools)
    if "browser_skill" in joined or "Map the owner" in body:
        return "pathfinder"
    if body.startswith("Work the owner's browser items") or "resumebot-browser__browser_next_item" in tools:
        return "browser_reader"
    if body.startswith("Call jobs.analysis"):
        return "analyst"
    if body.startswith("Call jobs.packet"):
        return "writer"
    if body.startswith("Call jobs.employer"):
        return "employer"
    if body.startswith("Call jobs.titles_next"):
        return "titles"
    if body.startswith("Call jobs.parse_next"):
        return "parser"
    if "find job boa" in body:
        return "scout"
    if body.startswith("Call jobs.profile for your task, then jobs.read"):
        return "reader"
    if "task_next" in joined or "plan_propose" in joined:
        return "site"
    return "other"


def model_class(model: str | None) -> str:
    """Which price list a model is on: Haiku's, else Sonnet's."""
    return "haiku" if "haiku" in (model or "").lower() else "sonnet"


def message_usd(model: str | None, usage: dict) -> float:
    """What one API message cost at API prices, from its usage."""
    p = PRICES[model_class(model)]
    created = usage.get("cache_creation") or {}
    w1h = created.get("ephemeral_1h_input_tokens") or 0
    w5m = created.get("ephemeral_5m_input_tokens") or 0
    # A message that gives no split is a 5-minute write for what the split does not account for.
    w5m += max((usage.get("cache_creation_input_tokens") or 0) - w1h - w5m, 0)
    total = ((usage.get("input_tokens") or 0) * p["input"]
             + (usage.get("cache_read_input_tokens") or 0) * p["cache_read"]
             + w5m * p["write_5m"] + w1h * p["write_1h"] + (usage.get("output_tokens") or 0) * p["output"])
    searches = (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    searches = searches if isinstance(searches, int) else len(searches)
    return total / 1e6 + searches * WEB_SEARCH_USD


def _when(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
    except ValueError:
        return None


def read_session(path: Path) -> dict | None:
    """One transcript as its totals: kind, start, API calls (messages counted once), tokens, dollars, and the role or
    packet its first tool reply named. None when it holds no start time."""
    seen: dict[str, tuple[str | None, dict]] = {}
    first, start, cost_state = None, None, None
    tools: set[str] = set()
    names: dict[str, str] = {}
    answered = refused = 0
    role_id = packet_id = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            start = start or _when(d.get("timestamp"))
            kind = d.get("type")
            if kind == "cost-state":
                cost_state = d.get("totalCostUSD")
            message: dict = d["message"] if isinstance(d.get("message"), dict) else {}
            content = message.get("content")
            if kind == "user" and first is None and isinstance(content, str):
                first = content
            if kind == "assistant" and message.get("id"):
                seen[message["id"]] = (message.get("model"), message.get("usage") or {})  # the last copy wins
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tools.add(str(block.get("name", "")).replace("mcp__", ""))
                        names[str(block.get("id"))] = str(block.get("name", ""))
                    elif block.get("type") == "tool_result":
                        text = block.get("content")
                        if isinstance(text, list):
                            text = " ".join(str(x.get("text", "")) for x in text if isinstance(x, dict))
                        if _ANSWER_TOOL.search(names.get(str(block.get("tool_use_id")), "")):
                            answered += 1
                            refused += bool(isinstance(text, str) and _REFUSED.match(text))
                        if role_id is not None and packet_id is not None:
                            continue
                        head = text[:4000] if isinstance(text, str) else ""
                        if role_id is None and (m := _ROLE_ID.search(head)):
                            role_id = m.group(1)
                        if packet_id is None and (m := _PACKET_ID.search(head[:300])):
                            packet_id = m.group(1)
    if start is None:
        return None
    tokens = defaultdict(int)
    models: dict[str, int] = defaultdict(int)
    computed = 0.0
    for model, usage in seen.values():
        models[model_class(model)] += 1
        tokens["input"] += usage.get("input_tokens") or 0
        tokens["cache_read"] += usage.get("cache_read_input_tokens") or 0
        tokens["output"] += usage.get("output_tokens") or 0
        created = usage.get("cache_creation") or {}
        w1h = created.get("ephemeral_1h_input_tokens") or 0
        w5m = (created.get("ephemeral_5m_input_tokens") or 0)
        w5m += max((usage.get("cache_creation_input_tokens") or 0) - w1h - w5m, 0)
        tokens["write_1h"] += w1h
        tokens["write_5m"] += w5m
        computed += message_usd(model, usage)
    return {
        "id": path.stem, "kind": kind_of(first or "", tools), "start": start, "turns": len(seen),
        "tokens": dict(tokens), "models": dict(models),
        "usd": float(cost_state) if isinstance(cost_state, int | float) else computed,
        "computed_usd": computed, "role_id": role_id, "packet_id": packet_id,
        # 47.6: a session that never handed anything back found nothing to do; a refusal is an answer the app refused.
        "found_nothing": answered == 0, "refused": refused,
    }


def transcript_files(data: Path) -> list[Path]:
    """Every lane's session files: <data>/claude/<lane>/projects/<working folder>/*.jsonl (app/scout/lanes-local.mjs
    makes a Claude folder for each lane there)."""
    return sorted((data / "claude").glob("*/projects/*/*.jsonl"))


def read_night(data: Path, start: datetime, end: datetime) -> list[dict]:
    """The sessions that started in [start, end)."""
    out = []
    for path in transcript_files(data):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < start:
                continue  # last written before the window opened: not tonight's
            session = read_session(path)
        except OSError:
            continue
        if session and start <= session["start"] < end:
            out.append(session)
    return out


def asks_timed_out(data: Path, start: datetime, end: datetime) -> int:
    """How many check-ins for work timed out in [start, end), from the lanes' own logs (<data>/logs/runs*.log). The
    ask comes before any session, so it belongs to no kind of work."""
    n = 0
    for path in sorted((data / "logs").glob("runs*.log")):
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    m = _ASK_TIMED_OUT.match(line)
                    if m and (at := _when(m.group(1))) and start <= at < end:
                        n += 1
        except OSError:
            continue
    return n


def window(night: date, off_start: str, off_end: str, zone: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """The off-hours window that starts on `night`, as UTC times (a window that ends at or before its start ends the
    next day; the same time twice is the whole day)."""
    zone = zone or owner_zone()
    a, b = clock(off_start) or time(1, 0), clock(off_end) or time(7, 0)
    start = datetime.combine(night, a, zone)
    end = datetime.combine(night, b, zone)
    if end <= start:
        end += timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def last_finished_night(off_start: str, off_end: str, now: datetime | None = None) -> date:
    """The newest night whose window has ended."""
    now = now or utc_now()
    day = now.astimezone(owner_zone()).date()
    for back in range(0, 4):
        d = day - timedelta(days=back)
        if window(d, off_start, off_end)[1] <= now:
            return d
    return day - timedelta(days=4)


def _r(x: float) -> float:
    return round(x, 4)


def _kind_totals(sessions: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in sessions:
        k = out.setdefault(s["kind"], {
            "sessions": 0, "turns": 0, "input": 0, "cache_write": 0, "cache_write_1h": 0, "cache_read": 0, "output": 0,
            "usd": 0.0, "haiku_turns": 0, "found_nothing": 0, "refused": 0})
        t = s["tokens"]
        k["sessions"] += 1
        k["turns"] += s["turns"]
        k["input"] += t.get("input", 0)
        k["cache_write"] += t.get("write_5m", 0) + t.get("write_1h", 0)
        k["cache_write_1h"] += t.get("write_1h", 0)
        k["cache_read"] += t.get("cache_read", 0)
        k["output"] += t.get("output", 0)
        k["usd"] += s["usd"]
        k["haiku_turns"] += s["models"].get("haiku", 0)
        k["found_nothing"] += bool(s.get("found_nothing"))
        k["refused"] += s.get("refused", 0)
    for k in out.values():
        k["usd"] = _r(k["usd"])
    return out


async def _count(session: AsyncSession, query) -> int:
    return int((await session.execute(query)).scalar() or 0)


async def unit_counts(session: AsyncSession, owner: str, start: datetime, end: datetime) -> dict[str, int]:
    """What the night produced, by the unit each kind of session is measured in, from the database."""
    def events(name: str):
        return select(func.count(func.distinct(TrailEvent.link_id))).where(
            TrailEvent.owner == owner, TrailEvent.event == name, TrailEvent.at >= start, TrailEvent.at < end)

    reported = (await session.execute(select(TrailEvent.sentence).where(
        TrailEvent.owner == owner, TrailEvent.event == "scout_reported", TrailEvent.at >= start,
        TrailEvent.at < end))).scalars().all()
    read = sum(int(m.group(1)) for s in reported if (m := re.search(r"reported (\d+)", s or "")))
    # Only what a Claude session handed back: the app's own code-first workers (`browser_pace.is_code`, a session id
    # `code-...`) cost no Claude, so their attempts are in neither browser count.
    by_session = or_(BrowserAttempt.session_id.is_(None), not_(or_(
        BrowserAttempt.session_id == browser_pace.CODE, BrowserAttempt.session_id.like(browser_pace.CODE + "-%"))))
    handed = await _count(session, select(func.count()).select_from(BrowserAttempt).where(
        BrowserAttempt.owner == owner, BrowserAttempt.at >= start, BrowserAttempt.at < end,
        BrowserAttempt.outcome != "no_result", BrowserAttempt.kind != "map", by_session))
    mapped = await _count(session, select(func.count()).select_from(BrowserAttempt).where(
        BrowserAttempt.owner == owner, BrowserAttempt.at >= start, BrowserAttempt.at < end,
        BrowserAttempt.kind == "map", BrowserAttempt.outcome == "done", by_session))
    # The titles kind reads a role's header lines for the gate facts (`gating/gate_facts.py`) and tags roles with a
    # target role (`work/tagging.py`). Only the reads carry a time of their own (`gate_facts.haiku.answered`, an ISO
    # time in UTC); a tag keeps no time column, so tags are not counted here.
    first, last = start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()
    answered = jsonsql.field(Opportunity.gate_facts, "haiku", "answered")
    titles = await _count(session, select(func.count()).select_from(Opportunity).where(
        Opportunity.owner == owner, answered >= first, answered < last))
    ready = await _count(session, select(func.count(func.distinct(StageMove.role_id))).where(
        StageMove.owner == owner, StageMove.to_status == READY_TO_LAUNCH, StageMove.at >= start, StageMove.at < end))
    return {
        "analyst": await _count(session, events("analysed")), "writer": await _count(session, events("packet_ready")),
        "reader": read, "browser_reader": handed, "titles": titles, "pathfinder": mapped,
        "employer": await _count(session, events("employer_researched")), "ready": ready,
    }


async def build(session: AsyncSession, owner: str, night: date, sessions: list[dict], start: datetime,
                end: datetime, timed_out: int = 0) -> dict:
    """One night's report: totals, each kind with its cost per unit, and what the roles came to."""
    kinds = _kind_totals(sessions)
    units = await unit_counts(session, owner, start, end)
    for name, k in kinds.items():
        n = units.get(name, 0)
        if name in UNIT_NAMES:
            k["unit"], k["units"] = UNIT_NAMES[name], n
            k["per_unit_usd"] = _r(k["usd"] / n) if n else None
    total = round(sum(k["usd"] for k in kinds.values()), 2)

    # What each role cost: the analyst's session names the role; the writer's names a packet, which names its role.
    packets = {}
    packet_ids = [uuid.UUID(s["packet_id"]) for s in sessions if s["packet_id"]]
    if packet_ids:
        packets = {str(p): str(r) for p, r in (await session.execute(
            select(Packet.id, Packet.role_id).where(Packet.id.in_(packet_ids)))).all()}
    cost_by_role: dict[str, float] = defaultdict(float)
    for s in sessions:
        if s["kind"] == "analyst" and s["role_id"]:
            cost_by_role[s["role_id"]] += s["usd"]
        elif s["kind"] == "writer" and s["packet_id"] in packets:
            cost_by_role[packets[s["packet_id"]]] += s["usd"]
    ids = [uuid.UUID(r) for r in cost_by_role]
    state = {}
    if ids:
        state = {str(i): (st, step, gate) for i, st, step, gate in (await session.execute(
            select(Opportunity.id, Opportunity.status, Opportunity.screened_out_step, Opportunity.screened_out_by)
            .where(Opportunity.id.in_(ids)))).all()}
    ready_ids = {str(r) for (r,) in (await session.execute(select(StageMove.role_id).where(and_(
        StageMove.owner == owner, StageMove.to_status == READY_TO_LAUNCH, StageMove.at >= start,
        StageMove.at < end)))).all()}
    wasted: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0.0])
    for role, usd in cost_by_role.items():
        status, step, gate = state.get(role, (None, None, None))
        if status == GATED_OUT:
            row = wasted[(str(step or "unknown"), str(gate or "unknown"))]
            row[0] += 1
            row[1] += usd
    direct = sum(u for r, u in cost_by_role.items() if r in ready_ids)
    ready = units["ready"]
    gated_usd = sum(v[1] for v in wasted.values())
    return {
        "night": night.isoformat(), "from": start.isoformat(), "to": end.isoformat(),
        "total": {
            "usd": total, "sessions": len(sessions), "turns": sum(k["turns"] for k in kinds.values()),
            "input": sum(k["input"] for k in kinds.values()),
            "cache_write": sum(k["cache_write"] for k in kinds.values()),
            "cache_read": sum(k["cache_read"] for k in kinds.values()),
            "output": sum(k["output"] for k in kinds.values()),
            "found_nothing": sum(k["found_nothing"] for k in kinds.values()),
            "refused": sum(k["refused"] for k in kinds.values()),
            "asks_timed_out": timed_out,
        },
        "kinds": {k: kinds[k] for k in KINDS if k in kinds} | {k: v for k, v in kinds.items() if k not in KINDS},
        "roles": {
            "analysed": units["analyst"], "ready": ready,
            "usd_per_ready": _r(total / ready) if ready else None,
            "direct_usd_per_ready": _r(direct / len(ready_ids)) if ready_ids else None,
            "gated_usd": _r(gated_usd), "gated_roles": sum(v[0] for v in wasted.values()),
            "gated_by_stage": [
                {"stage": step, "gate": gate, "roles": n, "usd": _r(usd)}
                for (step, gate), (n, usd) in sorted(wasted.items(), key=lambda x: -x[1][1])],
        },
    }


async def settings_for(session: AsyncSession, owner: str) -> tuple[str, str]:
    """The off-hours window in force, as the two clock strings."""
    s = (await profile_store.current(session, owner)).settings
    return s.off_hours_start, s.off_hours_end


async def report_night(
    session: AsyncSession, owner: str, night: date, data: Path, *, store: bool = True,
) -> dict | None:
    """Read the night's transcripts, join them to the database, and store the night's row (replacing the one there;
    `store=False` only reads). None, storing nothing, when no session started in the window."""
    off_start, off_end = await settings_for(session, owner)
    start, end = window(night, off_start, off_end)
    sessions = await asyncio.to_thread(read_night, data, start, end)
    if not sessions:
        return None
    timed_out = await asyncio.to_thread(asks_timed_out, data, start, end)
    report = await build(session, owner, night, sessions, start, end, timed_out)
    if not store:
        return report
    row = (await session.execute(select(CostNight).where(CostNight.owner == owner, CostNight.night == night))).scalar()
    if row is None:
        row = CostNight(owner=owner, night=night)
        session.add(row)
    row.total_usd = report["total"]["usd"]
    row.sessions = report["total"]["sessions"]
    row.data = report
    row.computed_at = utc_now()
    await session.flush()
    return report


async def listing(session: AsyncSession, owner: str, limit: int = NIGHTS_SHOWN) -> list[dict]:
    """The newest nights, newest first."""
    rows = (await session.execute(select(CostNight).where(CostNight.owner == owner).order_by(
        CostNight.night.desc()).limit(limit))).scalars().all()
    return [r.data for r in rows]


def summary(report: dict) -> str:
    """The report in plain lines, for the command line."""
    t, roles = report["total"], report["roles"]
    lines = [f"night {report['night']} ({report['from']} to {report['to']})",
             f"total ${t['usd']:.2f}: {t['sessions']} sessions, {t['turns']} turns; input {t['input']:,}, cache write "
             f"{t['cache_write']:,}, cache read {t['cache_read']:,}, output {t['output']:,} tokens"]
    for name, k in report["kinds"].items():
        per = f"; {k['units']} {k['unit']}, ${k['per_unit_usd']:.3f} each" if k.get("per_unit_usd") is not None else ""
        bad = [f"{k[key]} {what}" for key, what in (("found_nothing", "found nothing"), ("refused", "answers refused"))
               if k.get(key)]
        lines.append(f"  {name}: ${k['usd']:.2f}, {k['sessions']} sessions, {k['turns']} turns{per}"
                     + ("; " + ", ".join(bad) if bad else ""))
    lines.append(f"  asks timed out: {t.get('asks_timed_out', 0)}; sessions that found nothing: "
                 f"{t.get('found_nothing', 0)}; answers refused: {t.get('refused', 0)}")
    ready = f"${roles['usd_per_ready']:.2f}" if roles["usd_per_ready"] is not None else "none reached"
    lines.append(f"roles: {roles['analysed']} analysed, {roles['ready']} reached Ready to Apply (night's cost per "
                 f"ready role: {ready}); ${roles['gated_usd']:.2f} spent on {roles['gated_roles']} roles since "
                 "gated out")
    lines += [f"  gated out at {g['stage']}/{g['gate']}: {g['roles']} roles, ${g['usd']:.2f}"
              for g in roles["gated_by_stage"]]
    return "\n".join(lines)


async def note_night(session: AsyncSession, owner: str, data: Path | None) -> dict | None:
    """After the off-hours window: the newest finished night, worked out once. Nothing when its row is there or the
    install keeps no transcripts."""
    if data is None or not (data / "claude").is_dir():
        return None
    off_start, off_end = await settings_for(session, owner)
    night = last_finished_night(off_start, off_end)
    have = await _count(session, select(func.count()).select_from(CostNight).where(
        CostNight.owner == owner, CostNight.night == night))
    return None if have else await report_night(session, owner, night, data)
