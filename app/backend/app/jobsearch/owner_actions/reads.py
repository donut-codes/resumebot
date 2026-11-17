"""The owner's read actions (docs/PRD.md path 50.5): they change nothing and have no dry run. The door rolls the session
back after one, so not even a bring-up-to-date a service makes while it reads is kept. Each answers plain data, small
enough to read in a turn."""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, or_, select

from app import db
from app import settings as app_settings
from app.agents import roster
from app.errors import NotFound
from app.jobsearch import facts, stages
from app.jobsearch.fetch import host_stops
from app.jobsearch.fit import scoring
from app.jobsearch.missions import record
from app.jobsearch.models import (
    ORIGIN_SCOUT,
    BrowserDomain,
    BrowserItem,
    CostNight,
    HandOut,
    JobPosting,
    Mission,
    MissionRun,
    Opportunity,
    Packet,
)
from app.jobsearch.owner_actions.registry import Args, BadArguments, Ctx, NoArgs, Outcome, register
from app.jobsearch.pipeline import page as roles
from app.jobsearch.pipeline import reanalysis
from app.jobsearch.pool import core as pool
from app.jobsearch.pool import cost_report
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work import availability, hand_outs
from app.jobsearch.work import hours as night
from app.models.work import Notification, TrailEvent
from app.settings import VERSION, settings
from app.timeutil import iso_or_none, utc_now
from app.trail import service as trail

#: The most rows a read gives of a long list (the rest is counted): a reply is small enough to read in a turn.
MOST_ROWS = 60


def _when(at: datetime | None) -> str | None:
    return iso_or_none(at)


# --- health -----------------------------------------------------------------------------------------------------------


def _last_backup(now: datetime) -> dict:
    """The newest backup in the data folder's backups folder (`scripts/backup_local.py`: a SQLite copy or a Postgres
    dump, whichever is newer), and how old it is."""
    folder = app_settings.data_dir()
    backups = folder / "backups" if folder and (folder / "backups").is_dir() else None
    found = [p for kind in ("sqlite3", "dump") for p in backups.glob(f"resumebot-*.{kind}")] if backups else []
    if not found:
        return {"found": False, "age_hours": None}
    made = datetime.fromtimestamp(max(p.stat().st_mtime for p in found), UTC)
    return {"found": True, "at": _when(made), "age_hours": round((now - made).total_seconds() / 3600, 1)}


@register("health", "Whether the app is up and working: the database, the version, the schedule, when housekeeping and "
          "the dispatcher last passed, the runners, Claude's usage hold and the age of the last backup.")
async def health(ctx: Ctx, args: NoArgs) -> Outcome:
    now = utc_now()
    rows = await roster.runner_rows(ctx.session, ctx.owner)
    runners = await roster.runners_view(ctx.session, ctx.owner, rows, now)
    hold = await availability.limit_until(ctx.session, ctx.owner, now=now)
    on = (await ctx.session.scalars(select(Mission).where(Mission.owner == ctx.owner, Mission.state == "on"))).all()
    stamps = await facts.get_many(ctx.session, ctx.owner, "last_pass:")
    return Outcome({
        "version": VERSION, "database": "ok" if await db.ping() else "unreachable",
        "schedule": {"running": settings.run_schedule, "missions_on": sorted(m.key for m in on)},
        "housekeeping_last_pass": stamps.get("last_pass:housekeeping"),
        "dispatcher_last_pass": stamps.get("last_pass:dispatcher"),
        "runners": [{k: r[k] for k in ("name", "state", "task", "seen_at")} for r in runners],
        "usage_hold_until": _when(hold), "backup": _last_backup(now)})


# --- queues -----------------------------------------------------------------------------------------------------------


@register("queues", "What waits: the pool's waiting counts by kind, claims held for sessions, the browser queue, the "
          "analyst's line, packets being written and the mission runs open.")
async def queues(ctx: Ctx, args: NoArgs) -> Outcome:
    s, owner = ctx.session, ctx.owner
    held_kinds = (await s.scalars(select(HandOut.kind).where(
        HandOut.owner == owner, HandOut.session_id.is_(None), HandOut.lease_until > utc_now()).distinct())).all()
    browser = (await s.execute(select(BrowserItem.kind, func.count()).where(
        BrowserItem.owner == owner, BrowserItem.state == "waiting").group_by(BrowserItem.kind))).all()
    line = (await s.execute(select(
        func.count(), func.count().filter(Opportunity.analysis_claimed_at.is_not(None))).where(
        Opportunity.owner == owner, Opportunity.analysis_state == analysis.WAITING))).one()
    packets_writing = await s.scalar(select(func.count(Packet.id)).where(
        Packet.owner == owner, Packet.status == "writing"))
    open_runs = (await s.execute(
        select(MissionRun, Mission.key).join(Mission, Mission.id == MissionRun.mission_id).where(
            MissionRun.owner == owner, MissionRun.outcome == "running").order_by(MissionRun.started_at))).all()
    return Outcome({
        "pool_waiting": await pool.waiting_counts(s, owner),
        "claims_held": {kind: len(await hand_outs.held(s, owner, kind)) for kind in sorted(held_kinds)},
        "browser_waiting": {kind: n for kind, n in browser},
        "analyst_line": {"waiting": int(line[0]), "with_a_session": int(line[1])},
        "packets_being_written": int(packets_writing or 0),
        "open_mission_runs": [{"mission": key, "run": str(run.id), "started_by": run.started_by,
                               "started": _when(run.started_at), "summary": (run.summary or "")[:160]}
                              for run, key in open_runs]})


# --- night.summary ----------------------------------------------------------------------------------------------------


class NightArgs(Args):
    night: date | None = None


@register("night.summary", "One night's report: the mission runs in its off-hours window, postings seen, new, kept and "
          "screened out by gate, the board feed's requests and stop, notices raised and the stored cost row. night is "
          "a date (default: the last finished night).", NightArgs)
async def night_summary(ctx: Ctx, args: NightArgs) -> Outcome:
    s, owner = ctx.session, ctx.owner
    start_s, end_s = await cost_report.settings_for(s, owner)
    day = args.night or cost_report.last_finished_night(start_s, end_s)
    start, end = cost_report.window(day, start_s, end_s)
    runs = (await s.execute(
        select(MissionRun, Mission.key).join(Mission, Mission.id == MissionRun.mission_id).where(
            MissionRun.owner == owner, MissionRun.started_at >= start, MissionRun.started_at < end)
        .order_by(MissionRun.started_at))).all()
    mine = [JobPosting.owner == owner]
    new = [*mine, JobPosting.first_seen >= start, JobPosting.first_seen < end]
    seen = int(await s.scalar(select(func.count()).select_from(JobPosting).where(
        *mine, JobPosting.last_seen >= start, JobPosting.last_seen < end)) or 0)
    created = int(await s.scalar(select(func.count()).select_from(JobPosting).where(*new)) or 0)
    kept = int(await s.scalar(select(func.count()).select_from(JobPosting).where(
        *new, JobPosting.opportunity_id.is_not(None))) or 0)
    gates = (await s.execute(select(JobPosting.stop_gate, func.count()).where(
        *new, JobPosting.opportunity_id.is_(None), JobPosting.filtered_at.is_not(None))
        .group_by(JobPosting.stop_gate))).all()
    reads = [record.board_feed_of(run) for run, key in runs if key == record.READ]
    fed = next((r for r in reversed(reads) if r), None)
    notices = int(await s.scalar(select(func.count()).select_from(Notification).where(
        Notification.owner == owner, Notification.created_at >= start, Notification.created_at < end)) or 0)
    cost = await s.scalar(select(CostNight).where(CostNight.owner == owner, CostNight.night == day))
    return Outcome({
        "night": day.isoformat(), "from": _when(start), "to": _when(end),
        "runs": [{"mission": key, "run": str(run.id), "started_by": run.started_by, "outcome": run.outcome,
                  "summary": (run.summary or "")[:200], "started": _when(run.started_at), "ended": _when(run.ended_at)}
                 for run, key in runs],
        "postings": {"seen": seen, "new": created, "kept": kept,
                     "screened_out": {(gate or "unknown"): n for gate, n in gates}},
        "board_feed": ({k: fed.get(k) for k in ("requests", "searches", "searches_read", "skipped", "stopped", "mode")}
                       if fed else None),
        "notifications_raised": notices,
        "cost": ({"total_usd": cost.total_usd, "sessions": cost.sessions, "computed_at": _when(cost.computed_at)}
                 if cost else None)})


# --- role.state -------------------------------------------------------------------------------------------------------


class RoleArgs(Args):
    role: str


async def find_role(session, owner: str, ref: str) -> Opportunity:
    """The owner's role that `ref` names: a role id, a posting's link, or a job id (a posting's id on its board)."""
    ref = (ref or "").strip()
    try:
        found = await session.get(Opportunity, uuid.UUID(ref))
        if found is not None and found.owner == owner:
            return found
    except ValueError:
        pass
    if not ref:
        raise BadArguments("name a role: its id, a posting's link or a job id")
    plain = ref.rstrip("/")
    role = await session.scalar(select(Opportunity).where(
        Opportunity.owner == owner, or_(Opportunity.url == ref, Opportunity.url == plain,
                                        Opportunity.source_url == ref, Opportunity.source_url == plain)).limit(1))
    if role is None:
        role = await session.scalar(select(Opportunity).join(
            JobPosting, JobPosting.opportunity_id == Opportunity.id).where(
            JobPosting.owner == owner, or_(JobPosting.ext_id == ref, JobPosting.url == ref, JobPosting.url == plain))
            .limit(1))
    if role is None:
        raise NotFound("no role of yours matches that id, link or job id")
    return role


@register("role.state", "One role as the app holds it: stage, fit, the gate that stopped it, research and claim state, "
          "its packets and its latest Activity lines. role is a role id, a posting link or a job id.", RoleArgs)
async def role_state(ctx: Ctx, args: RoleArgs) -> Outcome:
    s = ctx.session
    role = await find_role(s, ctx.owner, args.role)
    shown = await roles.detail(s, ctx.owner, role.id)
    facts = {k: v for k, v in shown["role"].items() if k != "description"}
    # What the role's row (`role_row.Row`) leaves to the screen, kept for the agent: the facts as stored.
    facts.update({
        "fit": None if scoring.has_no_percent(role) else role.fit, "fit_evidence": role.fit_evidence,
        "comp": role.comp, "where": " · ".join(x for x in (role.location, role.move_type) if x) or None,
        "found_at": iso_or_none(role.created_at) if role.origin == ORIGIN_SCOUT else None,
        "source_url": role.source_url, "source_kind": role.source_kind,
        "browser_note": role.browser_note if role.status in stages.PRE_READY else None})
    packets = (await s.scalars(select(Packet).where(Packet.role_id == role.id).order_by(Packet.version.desc()))).all()
    lines = (await s.scalars(select(TrailEvent).where(
        TrailEvent.owner == ctx.owner, TrailEvent.link_kind == "opportunity", TrailEvent.link_id == role.id)
        .order_by(TrailEvent.at.desc(), TrailEvent.id.desc()).limit(20))).all()
    return Outcome({
        "id": str(role.id), "role": facts, "stage": stages.LABELS.get(role.status, role.status),
        "stopped": ({"gate": role.screened_out_by, "step": role.screened_out_step, "why": role.screened_out_reason}
                    if role.status == stages.GATED_OUT else None),
        "research": {"state": role.analysis_state, "asked": _when(role.analysis_asked_at),
                     "claimed": _when(role.analysis_claimed_at), "done": _when(role.analysed_at)},
        "sourced": role.sourced_at is not None, "gates": shown["gates"], "summary": shown["summary"],
        "packets": [{"id": str(p.id), "version": p.version, "status": p.status, "made": _when(p.created_at)}
                    for p in packets],
        "activity": [{**trail.entry(r), "details": None} for r in lines]})


# --- stops.list -------------------------------------------------------------------------------------------------------


@register("stops.list", "What is stopped or held: the hosts a refusal stopped (host, when, what answered, and until "
          "when for a stop that ends), peak and off hours, and the paused missions.")
async def stops_list(ctx: Ctx, args: NoArgs) -> Outcome:
    s, owner = ctx.session, ctx.owner
    now = utc_now()
    held = (await s.scalars(select(BrowserDomain).where(
        BrowserDomain.owner == owner, BrowserDomain.stopped_at.is_not(None)).order_by(BrowserDomain.stopped_at))).all()
    stopped = [d for d in held if host_stops.in_force(d, now)]  # a feed's ends at the owner's next off hours start
    paused = (await s.scalars(select(Mission).where(Mission.owner == owner, Mission.state == "paused"))).all()
    prefs = (await profile_store.current(s, owner)).settings
    return Outcome({
        "hosts": [{"host": d.domain, "since": _when(d.stopped_at), "answered": d.stopped_for,
                   "until": _when(d.stopped_until)} for d in stopped],
        "hours": {**night.peak_view(prefs), "off_hours": [prefs.off_hours_start, prefs.off_hours_end],
                  "in_off_hours": night.in_off_hours(prefs, utc_now())},
        "paused_missions": [{"mission": m.key, "name": m.name, "until": _when(m.paused_until)} for m in paused]})


# --- reanalysis.compare -----------------------------------------------------------------------------------------------


@register("reanalysis.compare", f"The old and the new fit of the roles marked to be researched again, highest old fit "
          f"first (the first {MOST_ROWS} rows; the rest counted).")
async def reanalysis_compare(ctx: Ctx, args: NoArgs) -> Outcome:
    rows = await reanalysis.compare(ctx.session, ctx.owner)
    waiting = sum(1 for r in rows if r["after"] is None)
    return Outcome({"roles": len(rows), "not_researched_again_yet": waiting, "rows": rows[:MOST_ROWS],
                    "more": max(0, len(rows) - MOST_ROWS)})
