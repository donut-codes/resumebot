"""The owner's onboarding actions (docs/PRD.md path 50.14): what a new owner's own Claude needs to set the app up by
talking with them, each one thin over what the page or the command line already does.

- `resume.import` starts the page's "Import your resume" on a file and answers where to confirm it. The owner looks the
  reading over on My Resume and confirms it there; the door never saves a word of the master resume.
- `profile.read` and `profile.save` read the profile and save a part of it as a new version, as a save on screen does
  (the store's `save`, then `after_save.run`). `save` is the one write of the door that sets what the owner looks for,
  so it holds back the owner's own switch of the robots-exempt read, which the door never changes (50.8).
- `tracker.import` and `employers.import` bring in the owner's tracker spreadsheet and employer list, through the
  functions the command line calls (`imports/bring_in.py`).
- `mail.state` says whether a mail login is saved and what the owner does to save one. The password goes in on the page
  only: the door has no action that takes one and none that opens the mailbox.

The first search needs no action here: it is `mission.run` with the scout's mission. A file is named by its whole path
on this computer, never its content: what the owner's files hold is not kept in the job record, which keeps every
argument."""

import asyncio
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from sqlalchemy import func, select

from app import db, errors
from app.errors import NotFound, Unprocessable
from app.jobsearch.actors import CLAUDE
from app.jobsearch.imports import bring_in
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.mail import login as mail_login
from app.jobsearch.owner_actions.registry import Args, BadArguments, Ctx, NoArgs, Outcome, register
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile import views
from app.jobsearch.profile.base import KINDS
from app.jobsearch.resume import text as resume_text
from app.jobsearch.work import engine_calls
from app.serve import DEFAULT_PORT, HOST
from app.settings import settings


def page(path: str = "") -> str:
    """An address in the app's page, for the owner to open. `JOBSEARCH_PUBLIC_URL` when the install names one, else
    the address `resumebot serve` listens on by default."""
    base = settings.public_url.strip().rstrip("/") or f"http://{HOST}:{DEFAULT_PORT}"
    return f"{base}/{path}"


class FileArgs(Args):
    path: str = Field(description="the whole path of the file on this computer (starting at the drive, or /)")

    @field_validator("path")
    @classmethod
    def _whole(cls, v: str) -> str:
        try:
            path = Path(v.strip().strip("\"'")).expanduser()  # a path copied from Explorer comes in quotes
        except RuntimeError:  # `~name` of a home that is not there
            path = Path()
        if not path.is_absolute():
            raise ValueError("send the file's whole path, starting at the drive (or /), not a name or a short path")
        return str(path)


def _file(path: str, kinds: tuple[str, ...]) -> Path:
    """The file named, once it is one of the kinds this action reads and is there."""
    found = Path(path)
    if found.suffix.lower() not in kinds:
        raise BadArguments(f"{found.name} is not a {' or '.join(kinds)} file: only those are read here")
    if not found.is_file():
        raise NotFound(f"no file at {path}: send the whole path of a file on this computer")
    return found


# --- resume.import ----------------------------------------------------------------------------------------------------


@register("resume.import", "Start reading a resume file (.docx, .pdf or .txt, by its whole path) into the master "
          "resume's parts, as Import your resume on My Resume does. An agent reads it, then the owner looks over what "
          "would be added and confirms it on My Resume at the address the answer gives (confirm_at): nothing is saved "
          "until they do, and nothing they wrote is removed or reworded. Never confirm it for them. Refused in words "
          "when no agent can take it now.", FileArgs, writes=True)
async def import_resume(ctx: Ctx, args: FileArgs) -> Outcome:
    s, owner = ctx.session, ctx.owner
    path = _file(args.path, resume_text.KINDS)
    if path.stat().st_size > resume_text.MAX_BYTES:
        raise errors.TooLarge(resume_text.TOO_BIG)
    data = await asyncio.to_thread(path.read_bytes)
    text = await asyncio.to_thread(resume_text.extract, path.name, data)  # a ReadError is in words
    await role_parse.refuse_resume_over_room(s, owner, text, what="This file's text",
                                             fix="Check that it is the right file.")
    if ctx.dry_run:
        ok, why = await role_parse.ready(s, owner)
        if not ok:
            raise errors.NotReady(why or "No agent can take this now.")
        return Outcome({"file": path.name, "characters": len(text)},
                       would=f"start an agent reading {path.name} ({len(text):,} characters) into the master resume's "
                             "parts, for the owner to look over and confirm on My Resume")
    try:
        job = await role_parse.start(s, owner, text, kind=role_parse.RESUME)
    except role_parse.NotReady as exc:
        raise errors.NotReady(exc.why) from None
    job_id = str(job.id)

    async def knock() -> None:
        await engine_calls.knock(job_id, reason=engine_calls.PARSE_ASKED)

    return Outcome({
        "parse_job": job_id, "state": "waiting", "confirm_at": page(f"#/me?import={job_id}"),
        "how": "An agent is reading the file. Tell the owner to open confirm_at: it shows the reading, then what would "
               "be added to the master resume, and nothing is saved until they confirm it there. The reading is kept "
               "for a day. Do not confirm it for them."},
        did=f"started reading the owner's resume file {path.name} (the owner confirms it on My Resume)", after=[knock])


# --- profile.read and profile.save ------------------------------------------------------------------------------------


@register("profile.read", "The owner's profile as saved: every field of each part (criteria: the target roles and the "
          "kind of employer; gates: where they will work and the pay they need; scoring; settings), whether there is "
          "anything to search for (is_set), the versions, and each gate and ask as the sentence the page shows "
          "(says). Read it before profile.save and read it back to the owner in plain words.", NoArgs)
async def read_profile(ctx: Ctx, args: NoArgs) -> Outcome:
    profile = await profile_store.current(ctx.session, ctx.owner)
    counts = await profile_store.counts(ctx.session, ctx.owner)
    return Outcome({"is_set": profile.is_set(), "versions": {k: counts[k] for k in KINDS},
                    "profile": profile.model_dump(mode="json"), "says": views.says(profile)})


SaveKind = Literal["criteria", "gates", "scoring", "settings"]
#: The one setting the owner turns on themselves: the door never changes it (docs/PRD.md 50.8).
OWNERS_OWN = "read_robots_exempt"


class SaveArgs(Args):
    kind: SaveKind = Field(description="the part to save")
    data: dict[str, Any] = Field(description="the fields to set, each replacing that field whole; the part's other "
                                             "fields are kept as they are")
    why: str = Field("", description="why, in the owner's words, for the part's history")

    @field_validator("data")
    @classmethod
    def _something(cls, v: dict) -> dict:
        if not v:
            raise ValueError("send at least one field to set (profile.read shows them)")
        return v


async def _planned(ctx: Ctx, args: SaveArgs) -> tuple[dict, dict, dict]:
    """What a save of these fields would store, checked as a save on screen is: the stored part and the fields sent put
    together, a line only a reader could judge or a negative score refused, a field that does not fit the part refused
    in words, and the owner's own switch left as it is. Returns the data to save, the part as it would be stored and the
    part as it is stored now."""
    s, owner, kind = ctx.session, ctx.owner, args.kind
    stored = await profile_store.stored_data(s, owner, kind) or {}
    merged = {**stored, **args.data}
    await profile_store.refuse_what_a_save_refuses(s, owner, kind, merged)
    doc = profile_store.checked(kind, merged)
    if kind == "settings" and getattr(doc, OWNERS_OWN) != getattr(profile_store.checked(kind, stored), OWNERS_OWN):
        raise profile_store.ProfileError(
            f"{OWNERS_OWN} is the owner's own switch: they turn it on or off in the app (Agent Settings), the door "
            "never changes it")
    return merged, doc.model_dump(mode="json"), stored


def _said(changes: list[str]) -> str:
    return "; ".join(changes) or "nothing changes"


@register("profile.save", "Save fields of a part of the profile (kind: criteria, gates, scoring or settings; data: the "
          "fields, each replacing that field whole, the rest kept) as a new version, as a save on screen does, "
          "then run what a save on screen runs (a new Hard pass checks every posting and role again). Only what the "
          "owner has told you: read profile.read first, and never invent a target, a place or a pay figure. A field "
          "that does not fit, a line only a reader could judge and a negative score are refused in words, as on "
          "screen. read_robots_exempt is never changed here. A dry run says what would change.", SaveArgs,
          writes=True, long=True, refuses="a profile save is running already", one_at_a_time=True)
async def save_profile(ctx: Ctx, args: SaveArgs) -> Outcome:
    from app.jobsearch.profile import after_save  # engine-side: loaded when called

    s, owner, kind = ctx.session, ctx.owner, args.kind
    merged, stored_after, stored = await _planned(ctx, args)
    changes = profile_store.changes(stored, stored_after)
    in_force = await profile_store.newest_version(s, owner, kind)
    if ctx.dry_run:
        same = stored == stored_after
        return Outcome({"kind": kind, "in_force": in_force, "version": None if same else in_force + 1,
                        "changes": changes},
                       would=(f"save nothing: the {kind} would be the same as version {in_force}" if same else
                              f"save version {in_force + 1} of the {kind} ({_said(changes)}), then run what a save on "
                              "screen runs"))
    said: list[str] = []
    version = await profile_store.save(s, owner, kind, merged, by=CLAUDE,
                                       why=args.why.strip() or "set on the owner's ask", said=said,
                                       defer_works_out=True)
    await after_save.run(s, owner, kind, version, "was saved on the owner's ask", said)
    await db.release(s)  # the version is saved: the scores are worked out a page to a turn, with the session free
    works_out = await profile_store.works_out_again(s, owner, kind, version)
    if works_out:
        said.insert(0, works_out)  # first, where a save has always said it
    result = {"kind": kind, "version": version, "changes": changes, "after_save": " ".join(said)}
    if version is None:
        return Outcome(result, did=f"found the {kind} already as asked: nothing was saved")
    return Outcome(result, did=f"saved version {version} of the {kind} ({_said(changes)})")


# --- tracker.import and employers.import ------------------------------------------------------------------------------


@register("tracker.import", "Read the owner's tracker spreadsheet (.xlsx, by its whole path) into the roles, as the "
          "tracker import does: a role the app has worked on stays as the app has it, the file is only read, and "
          "what it brought in and nobody researched is researched by the catch-up it starts. Answers counts. Run it "
          "again whenever the owner likes. A dry run reads the file and says the counts and how many roles would be "
          "researched.", FileArgs, writes=True, long=True, refuses="a tracker import is running already",
          one_at_a_time=True)
async def import_tracker_file(ctx: Ctx, args: FileArgs) -> Outcome:
    path = _file(args.path, (".xlsx",))
    brought = await bring_in.tracker_file(ctx.session, ctx.owner, path, dry_run=ctx.dry_run)
    counts = brought.counts
    result: dict[str, Any] = {"counts": counts, "notes": brought.notes}
    said = f"{counts['added']:,} new roles, {counts['updated']:,} brought up to date"
    if ctx.dry_run:
        result["would_research"] = brought.would_research
        return Outcome(result, would=f"read {counts['rows']:,} rows of {path.name}: {said}, and research "
                                     f"{brought.would_research:,} roles")
    after = []
    if brought.started is not None:
        result["catch_up"] = brought.started.words
        knocks = list(brought.started.knocks)

        async def knock() -> None:
            for key, reason in knocks:  # the scout, once the work is saved
                await engine_calls.knock(key, reason)

        after.append(knock)
    return Outcome(result, did=f"read the tracker {path.name}: {said}", after=after)


@register("employers.import", "Keep the names and websites of the owner's employer list (a .csv by its whole path, "
          "with a heading row, a name column and a website column) as the scout's first clue to an employer's job "
          "board, as the employer list import does. It makes no employer and researches nothing; loading a file of "
          "the same name again replaces its rows. Answers counts. A dry run saves nothing.", FileArgs, writes=True)
async def import_employer_list(ctx: Ctx, args: FileArgs) -> Outcome:
    path = _file(args.path, (".csv",))
    try:
        text = await asyncio.to_thread(path.read_text, "utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise Unprocessable(f"the file could not be read ({getattr(exc, 'strerror', None) or type(exc).__name__})") \
            from None
    brought = await bring_in.employer_list_file(ctx.session, ctx.owner, path.name, text, dry_run=ctx.dry_run)
    read, found = brought.read, brought.found
    result = {"rows": read.rows, "kept": len(read.entries), "marked_drop": read.dropped, "unusable": read.unusable,
              "repeats": read.repeats, "employers_in_it": found.employers, "employers": found.of,
              "waiting_in_it": found.waiting, "waiting_for_the_scout": brought.waiting,
              "kept_rows_naming_an_employer": found.rows}
    said = (f"{len(read.entries):,} of {read.rows:,} rows of {path.name}; {found.employers:,} of the app's "
            f"{found.of:,} employers are in it")
    return Outcome(result, did=f"kept {said}", would=f"keep {said}")


# --- mail.state -------------------------------------------------------------------------------------------------------


@register("mail.state", "Whether a mail login is saved and as whom, with each provider's steps for an app password and "
          "the address of the page where the owner saves one (connect_at). The password is typed there and never into "
          "a chat; nothing here opens the mailbox. Never ask the owner for it.", NoArgs)
async def mail_state(ctx: Ctx, args: NoArgs) -> Outcome:
    seen = await mail_login.view(ctx.session, ctx.owner)
    return Outcome({**seen, "connect_at": page("#/agents"),
                    "how": "The owner types their mail address and an app password into the Your email part of Agent "
                           "Settings at connect_at. It is read-only and optional. Ask again here afterwards to see "
                           "that it is saved."})


# --- onboarding.state -------------------------------------------------------------------------------------------------


@register("onboarding.state", "Where each onboarding step stands (the resume, the tracker, the employer list, the "
          "target roles, the first search, mail) and which is next: the same lines and the same next step that "
          "`resumebot status` prints, made by the same code. The tracker and the employer list are optional and never "
          "the next step. Read it at the start and after each step.", NoArgs)
async def onboarding_state(ctx: Ctx, args: NoArgs) -> Outcome:
    from app.jobsearch.missions import core as missions
    from app.jobsearch.models import ORIGIN_TRACKER, EmployerListEntry, Opportunity
    from app.setup_cli import SEARCH_MISSION, step_report

    s, owner = ctx.session, ctx.owner
    profile = await profile_store.current(s, owner)
    counts = await profile_store.counts(s, owner)
    listed = await missions.listing(s, owner)
    mail = await mail_login.view(s, owner)
    tracker = await s.scalar(select(func.count()).select_from(Opportunity).where(
        Opportunity.owner == owner, Opportunity.origin == ORIGIN_TRACKER))
    entries = await s.scalar(select(func.count()).select_from(EmployerListEntry).where(
        EmployerListEntry.owner == owner))
    facts: dict[str, bool | None] = {
        "resume": counts["resume"] > 0, "tracker": bool(tracker), "employer list": bool(entries),
        "targets": profile.is_set(),
        "first search": any(m.get("key") == SEARCH_MISSION and m.get("last_run") for m in listed),
        "mail": bool(mail["connected"]),
    }
    lines, next_step = step_report(facts)
    return Outcome({"steps": facts, "lines": lines, "next": next_step})
