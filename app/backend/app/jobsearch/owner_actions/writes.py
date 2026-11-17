"""The owner's write actions (docs/PRD.md path 50.5). Each calls the service the page, a mission or the command line
calls, and each can be asked as a dry run: the same call with the write skipped (the service's own `dry_run`, or the
same checks and the same set computed, 50.7). The runner (`runner.py`) makes the job row, writes the Activity line and
commits; an action only does its work and says what it did (`did`) and what it would do (`would`).

A service's own refusal (a peak-hours refusal, a second run, an unknown site) is passed back as it is: the door adds no
rule of its own to what the app already keeps (50.9). What it adds is the conflicts it names to `Action.conflict`, which
turn a second run of the same thing into a 409 that names what holds it."""

import uuid
from collections import Counter
from datetime import date
from typing import Any, Literal, get_args

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app import settings as app_settings
from app.errors import NotFound
from app.jobsearch import stages
from app.jobsearch.actors import CLAUDE
from app.jobsearch.browser import reader as br
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import record
from app.jobsearch.models import Packet
from app.jobsearch.owner_actions.registry import Args, Ctx, Outcome, register
from app.jobsearch.pipeline import reanalysis
from app.jobsearch.pool import cost_report
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, requeue
from app.jobsearch.work import engine_calls
from app.jobsearch.writing import packets
from app.sites import registry
from app.sites.models import SiteRecording
from app.timeutil import iso_or_none, utc_now

#: The most role ids an answer lists (the count is always whole).
MOST_IDS = 100


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else many or one + 's'}"


def _by_stage(roles) -> dict[str, int]:
    return dict(Counter(str(stages.LABELS.get(r.status, r.status)) for r in roles))


# --- requeue ----------------------------------------------------------------------------------------------------------


class RequeueArgs(Args):
    scope: Literal["stale", "ready", "second_impression", "all"]


@register("requeue", "Mark roles to be researched again, by scope: stale (research older than its text or inputs), "
          "ready (the Ready to Launch batch, with a snapshot kept), second_impression (every role there, with a "
          "snapshot) or all (every researched active role, highest fit first).", RequeueArgs, writes=True, long=True,
          refuses="the same requeue is running", one_at_a_time=True)
async def requeue_roles(ctx: Ctx, args: RequeueArgs) -> Outcome:
    s, owner, dry = ctx.session, ctx.owner, ctx.dry_run
    if args.scope == "stale":
        count = await requeue.reanalyse_stale(s, owner, dry_run=dry)
        result: dict = {"scope": args.scope, "roles": count}
    else:
        roles = await {"ready": lambda: reanalysis.mark_stage(s, owner, stages.READY_TO_LAUNCH, dry_run=dry),
                       "second_impression": lambda: reanalysis.mark_stage(s, owner, stages.SECOND_IMPRESSION,
                                                                          dry_run=dry),
                       "all": lambda: requeue.reanalyse_all(s, owner, dry_run=dry)}[args.scope]()
        count = len(roles)
        result = {"scope": args.scope, "roles": count, "by_stage": _by_stage(roles),
                  "role_ids": [str(r.id) for r in roles[:MOST_IDS]], "snapshot_kept": args.scope != "all" and not dry}
    return Outcome(result, did=f"requeued {_plural(count, 'role')} for research ({args.scope})",
                   would=f"requeue {_plural(count, 'role')} for research ({args.scope})")


# --- recipes ----------------------------------------------------------------------------------------------------------


class RecordArgs(Args):
    site: str | None = None
    hosts: list[str] | None = None
    kind: Literal["first", "page", "posting"] = "first"

    @model_validator(mode="after")
    def _names_what_to_record(self):
        if self.kind == "posting" and not (self.hosts or self.site):
            raise ValueError("kind posting needs hosts (or a site whose host is meant)")
        if self.kind != "posting" and not self.site:
            raise ValueError(f"kind {self.kind} needs a site: its id, address or host")
        return self


def _hosts_of(args: RecordArgs) -> list[str]:
    return [h.strip().lower() for h in (args.hosts or [br.host_of(args.site or "") or (args.site or "")]) if h.strip()]


async def _recording_conflict(session: AsyncSession, owner: str, args: RecordArgs) -> str | None:
    from app.jobsearch.browser.recipes import record as recipe_record  # engine-side: loaded when called

    if args.kind == "posting":
        for host in _hosts_of(args):
            held = await session.scalar(select(SiteRecording).where(
                SiteRecording.owner == owner, SiteRecording.host == host, SiteRecording.state == "requested"))
            if held is not None:
                return f"a recording for {host} is already asked (since {iso_or_none(held.requested_at)})"
        return None
    site = await recipe_record.find_site(session, owner, args.site or "")
    if site is None:
        return None
    held = await session.scalar(select(SiteRecording).where(
        SiteRecording.owner == owner, SiteRecording.site_id == site.id, SiteRecording.state == "requested"))
    return None if held is None else (
        f"a recording for {site.url} is already asked (since {iso_or_none(held.requested_at)})")


@register("recipe.record", "Ask a recording of a site's job listings (kind first, or page for its page recipe; site is "
          "its id, address or host) or of one posting's page on each of hosts (kind posting). The app's next browser "
          "run makes it.", RecordArgs, writes=True, refuses="a recording for that site is already asked",
          conflict=_recording_conflict)
async def record_recipe(ctx: Ctx, args: RecordArgs) -> Outcome:
    from app.jobsearch.browser.recipes import record as recipe_record  # engine-side: loaded when called

    s, owner = ctx.session, ctx.owner
    if args.kind == "posting":
        asked, missing = [], []
        for host in _hosts_of(args):
            if ctx.dry_run:
                sample = await recipe_record.sample_posting(s, owner, host)
                (asked if sample else missing).append(host)
            else:
                (asked if await recipe_record.request_host(s, owner, host, "first") else missing).append(host)
        what = _plural(len(asked), "posting page")
        return Outcome({"kind": args.kind, "asked": asked, "no_posting_page_to_record": missing},
                       did=f"asked a recording of {what} ({', '.join(asked) or 'none'})",
                       would=f"ask a recording of {what} ({', '.join(asked) or 'none'})")
    site = await recipe_record.find_site(s, owner, args.site or "")
    if site is None:
        raise NotFound(f"no site to search matches {args.site}: add it under Where to look first")
    if not ctx.dry_run:
        await recipe_record.request(s, owner, site, "page" if args.kind == "page" else "first")
    return Outcome({"kind": args.kind, "site": site.url}, did=f"asked a recording of {site.url}",
                   would=f"ask a recording of {site.url}")


class RetireArgs(Args):
    site: str
    why: str = ""


@register("recipe.retire", "Retire the recipe in force on a site (site is its id, address or host; why is the reason). "
          "It is kept as the site's history; nothing is deleted.", RetireArgs, writes=True)
async def retire_recipe(ctx: Ctx, args: RetireArgs) -> Outcome:
    from app.jobsearch.browser.recipes import record as recipe_record  # engine-side: loaded when called

    site = await recipe_record.find_site(ctx.session, ctx.owner, args.site)
    if site is None:
        raise NotFound(f"no site to search matches {args.site}")
    rows = await recipe_record.retire(ctx.session, ctx.owner, site, args.why or "retired through the action door",
                                      dry_run=ctx.dry_run)
    versions = [r.version for r in rows]
    return Outcome({"site": site.url, "versions": versions},
                   did=f"retired the recipe that read {site.url}" if versions else
                   f"found no recipe in force on {site.url}",
                   would=f"retire recipe version {', '.join(map(str, versions))} of {site.url}" if versions else
                   f"retire nothing: {site.url} has no recipe in force")


# --- cost.report ------------------------------------------------------------------------------------------------------


class CostArgs(Args):
    night: date | None = None


@register("cost.report", "Work out one night's Claude cost from the lanes' transcripts and store the row (night: a "
          "date, default the last finished night).", CostArgs, writes=True, long=True,
          refuses="the same cost report is running", one_at_a_time=True)
async def cost_report_night(ctx: Ctx, args: CostArgs) -> Outcome:
    data = app_settings.data_dir()
    if data is None or not (data / "claude").is_dir():
        raise NotFound("no lane transcripts here: the data folder has no claude folder")
    start, end = await cost_report.settings_for(ctx.session, ctx.owner)
    night_ = args.night or cost_report.last_finished_night(start, end)
    report = await cost_report.report_night(ctx.session, ctx.owner, night_, data, store=not ctx.dry_run)
    if report is None:
        return Outcome({"night": night_.isoformat(), "stored": False, "why": "no session started in its window"},
                       did=f"found no session to cost on the night of {night_}",
                       would=f"store nothing: no session started in the window of {night_}")
    return Outcome({"night": night_.isoformat(), "stored": not ctx.dry_run, "total_usd": report["total"]["usd"],
                    "sessions": report["total"]["sessions"], "summary": cost_report.summary(report)},
                   did=f"stored the cost of the night of {night_} (${report['total']['usd']:.2f})",
                   would=f"store the cost of the night of {night_} (${report['total']['usd']:.2f})")


# --- mission.run ------------------------------------------------------------------------------------------------------

#: The missions that may be run now: every kind but the owner's own apply tabs (`record.GO`).
RUNNABLE = tuple(k for k in record.KINDS if k != record.GO)


class MissionArgs(Args):
    mission: str = Field(json_schema_extra={"enum": list(RUNNABLE)})

    @field_validator("mission")
    @classmethod
    def _is_runnable(cls, key: str) -> str:
        if key not in RUNNABLE:
            raise ValueError(f"mission is one of {', '.join(RUNNABLE)} (never {record.GO})")
        return key


async def _mission_conflict(session: AsyncSession, owner: str, args: MissionArgs) -> str | None:
    mission = await missions.get(session, owner, args.mission)
    try:
        await missions.refuse_a_second(session, mission)
    except missions.MissionBusy as busy:
        held = (f" (run {busy.run_id}, started by {busy.started_by} at {iso_or_none(busy.started_at)})"
                if busy.run_id else "")
        return f"{busy}{held}"
    return None


@register("mission.run", "Run a mission now, whatever its schedule and even when paused: "
          f"{', '.join(RUNNABLE[:-1])} or {RUNNABLE[-1]} (never {record.GO}). It is the run Run now makes, started by "
          "claude; Claude work wakes the lanes. act.job follows the run.", MissionArgs,
          writes=True, refuses="the mission is already running", conflict=_mission_conflict,
          follow=lambda session, row: _follow_mission(session, row))
async def run_mission(ctx: Ctx, args: MissionArgs) -> Outcome:
    mission = await missions.get(ctx.session, ctx.owner, args.mission)
    if ctx.dry_run:
        await missions.run_now(ctx.session, ctx.owner, mission, by=CLAUDE, dry_run=True)
        return Outcome({"mission": mission.key, "name": mission.name, "state": mission.state,
                        "purpose": mission.purpose[:200]},
                       would=f"start {mission.name} (started by claude; its lanes woken)")
    ran = await missions.run_now(ctx.session, ctx.owner, mission, by=CLAUDE)
    return Outcome({"mission": mission.key, "run": ran.run}, did=f"started {mission.name}",
                   after=[lambda: engine_calls.knocks(ran.wake)], follow=ran.run["outcome"] == "running")


async def _follow_mission(session: AsyncSession, row) -> None:
    """Brings the job of a mission's run up to date: its progress line from the run's own, and its end with the run."""
    run_id = ((row.result or {}).get("run") or {}).get("id")
    view = await missions.read_run(session, row.owner, uuid.UUID(run_id)) if run_id else None
    if view is None:
        return
    progress = view.get("progress")
    row.progress = (f"{progress['done']} of {progress['target']}" if isinstance(progress, dict) and "done" in progress
                    else str(progress or view["summary"] or ""))[:200]
    row.result = {**(row.result or {}), "run": view}
    if view["outcome"] != "running":
        row.state, row.ended = ("done" if view["outcome"] in ("done", "skipped") else "failed"), utc_now()


# --- board_feed.read --------------------------------------------------------------------------------------------------


async def _board_feed_conflict(session: AsyncSession, owner: str, args: Args) -> str | None:
    board = registry.board_reader("guest")
    holder = board.reader_now() if board is not None else None
    return f"the board's job search is being read already by {holder}" if holder else None


@register("board_feed.read", "Read a board's public signed-out job search now, at any hour: the night's own reader "
          "(the owner's searches, once each, its pace, its back-offs and its ceiling), its postings stored like any "
          "source's. Nothing is read when the owner's read_robots_exempt setting is off, or when no runbook carries "
          "robots; the door never changes it.",
          writes=True, long=True, one_at_a_time=True, conflict=_board_feed_conflict,
          refuses="a board's feed read is running, or the night's board read is in its board-feed step")
async def read_board_feed_now(ctx: Ctx, args: Args) -> Outcome:
    s, owner = ctx.session, ctx.owner
    board = registry.board_reader("guest")
    if board is None:
        return Outcome({"setting_on": False}, did="nothing to read: no site here is read though its robots.txt asks "
                                                  "automated readers not to")
    profile = await profile_store.current(s, owner)
    searches = board.queries(profile)
    on = board.reading_on(profile)
    if ctx.dry_run:
        return Outcome({"setting_on": on, "searches": [{"keywords": k, "place": p} for k, p in searches],
                        "most_requests": board.request_cap(searches)},
                       would=(f"read {_plural(len(searches), 'search', 'searches')} of a board's public job search"
                              if on and searches else "make no request: "
                              + ("read_robots_exempt is off" if not on else "there are no searches to make")))

    async def told(night_: Any, seen: int) -> None:
        await ctx.say(f"{night_.requests} requests made, {seen} postings seen")

    token = board.PROGRESS.set(told)
    try:
        got = await board.read_now(s, owner, profile=profile, by=f"the action door's job {ctx.job_id}")
    finally:
        board.PROGRESS.reset(token)
    if not on:
        return Outcome({"setting_on": False, **got},
                       did="asked for a board-feed read; read_robots_exempt is off, so nothing was read")
    return Outcome({"setting_on": True, **got},
                   did=f"read a board's public job search: {got['requests']} requests, {got['seen']} postings seen, "
                       f"{got['new']} new")


# --- role.research and role.packet ------------------------------------------------------------------------------------


class RoleArgs(Args):
    role: uuid.UUID


@register("role.research", "Put a role in the analyst's line, first (a role researched already is researched again; "
          "one already in the line answers that it is). role is the role's id.", RoleArgs, writes=True)
async def research_role(ctx: Ctx, args: RoleArgs) -> Outcome:
    asked = await analysis.ask(ctx.session, ctx.owner, args.role, dry_run=ctx.dry_run)
    after = []
    if not ctx.dry_run and not asked["already"] and asked["state"] == analysis.WAITING:
        async def knock() -> None:
            await engine_calls.knock(asked["role"], reason=engine_calls.ANALYSIS_ASKED)

        after.append(knock)
    return Outcome(asked, did=asked["said"].replace(" is queued", " was queued", 1) if not ctx.dry_run else "",
                   would=asked["said"], after=after, link=("opportunity", args.role))


class PacketArgs(Args):
    role: uuid.UUID
    feedback: str = ""


async def _packet_conflict(session: AsyncSession, owner: str, args: PacketArgs) -> str | None:
    writing = await session.scalar(select(Packet).where(
        Packet.owner == owner, Packet.role_id == args.role, Packet.status == "writing"))
    return None if writing is None else (
        f"a packet for this role is being written already (version {writing.version}, packet {writing.id}, asked "
        f"{iso_or_none(writing.created_at)})")


@register("role.packet", "Ask the next version of a role's packet (feedback: what to change from the version before). "
          "role is the role's id.", PacketArgs, writes=True, refuses="a packet for that role is already being written",
          conflict=_packet_conflict)
async def packet_for_role(ctx: Ctx, args: PacketArgs) -> Outcome:
    if ctx.dry_run:
        would = await packets.would_ask(ctx.session, ctx.owner, args.role, args.feedback)
        return Outcome(would, would=f"ask version {would['version']} of the packet for {would['name']}",
                       link=("opportunity", args.role))
    packet = await packets.ask(ctx.session, ctx.owner, args.role, args.feedback)
    packet_id = str(packet.id)

    async def knock() -> None:
        await engine_calls.knock(packet_id, reason=engine_calls.PACKET_ASKED)

    return Outcome({"packet": packet_id, "version": packet.version, "status": packet.status},
                   did=f"asked version {packet.version} of the packet for a role", after=[knock],
                   link=("opportunity", args.role))


# --- regate -----------------------------------------------------------------------------------------------------------


class RegateArgs(Args):
    scope: Literal["everything"]


@register("regate", "Run the one gate check again: everything (the whole log and every role against the gates now in "
          "force). Stops and stages move.", RegateArgs, writes=True, long=True, refuses="the same regate is running",
          one_at_a_time=True)
async def regate_gates(ctx: Ctx, args: RegateArgs) -> Outcome:
    from app.jobsearch.gating import regate  # engine-side: loaded when called

    report = await regate.run(ctx.session, ctx.owner, cause="Claude asked for the gates to be checked again on demand",
                              dry_run=ctx.dry_run)
    result = {"scope": args.scope, **report.as_dict()}
    result["summary"] = result.pop("said")
    said = report.said.rstrip(".")
    return Outcome(result, did=f"ran the gate check again ({args.scope}): {said}", would=f"run the gate check again "
                                                                                       f"({args.scope}): {said}")


# --- profile.restore --------------------------------------------------------------------------------------------------

#: The parts the owner edits that the door may bring an earlier version of back. Never the resume: the app never
#: rewords the master, and a change to it comes only from the owner (docs/PRD.md 50.13).
RestorableKind = Literal["criteria", "gates", "scoring", "settings", "about", "writing"]
RESTORABLE = get_args(RestorableKind)


class RestoreArgs(Args):
    kind: RestorableKind
    version: int = Field(ge=1)


def _changes_said(changes: list[str]) -> str:
    return "; ".join(changes) or "only what the app keeps for itself differs"


@register("profile.restore", "Bring back an earlier saved version of one part the owner edits (kind: criteria, gates, "
          "scoring, settings, about or writing; version: its number in the part's history). It is saved as a new "
          "version equal to that one, with why \"restored version N on the owner's ask\", and what a save on screen "
          "sets going runs after it (a restored Hard pass checks every posting and role again; a restored "
          "Prioritization works every score out again). Refused, as a save on screen is, when that version holds a "
          "line only a reader could judge or a line with negative points. Never the resume. A dry run says what would "
          "change.",
          RestoreArgs, writes=True, long=True, refuses="a restore is running already", one_at_a_time=True)
async def restore_profile(ctx: Ctx, args: RestoreArgs) -> Outcome:
    from app.jobsearch.profile import after_save  # engine-side: loaded when called

    s, owner, kind, number = ctx.session, ctx.owner, args.kind, args.version
    plan = await profile_store.restore_plan(s, owner, kind, number)
    in_force, new = plan["in_force"], plan["version"]
    result = {"kind": kind, "restored": number, "in_force": in_force, "version": new, "changes": plan["changes"]}
    if ctx.dry_run:
        same = (f"version {number} of the {kind} is the version in force" if number == in_force else
                f"version {number} of the {kind} is the same as the version in force (version {in_force})")
        return Outcome(result, would=f"save nothing: {same}" if new is None else
                       f"bring back version {number} of the {kind} as version {new} "
                       f"({_changes_said(plan['changes'])}), then run what a save on screen runs")
    said: list[str] = []
    saved = await profile_store.restore(s, owner, kind, number, by=CLAUDE,
                                        why=f"restored version {number} on the owner's ask", said=said,
                                        defer_works_out=True)
    await after_save.run(s, owner, kind, saved, f"was made by bringing version {number} back on the owner's ask", said)
    await db.release(s)  # the version is saved: the scores are worked out a page to a turn, with the session free
    works_out = await profile_store.works_out_again(s, owner, kind, saved)
    if works_out:
        said.insert(0, works_out)  # first, where a restore has always said it
    result |= {"version": saved, "after_save": " ".join(said)}
    if saved is None:
        already = (f"version {number} of the {kind} already in force" if number == in_force else
                   f"version {number} of the {kind} the same as version {in_force}, already in force")
        return Outcome(result, did=f"found {already}: nothing was saved")
    return Outcome(result, did=f"restored version {number} of the {kind} as version {saved}")
